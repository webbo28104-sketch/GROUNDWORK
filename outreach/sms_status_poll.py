"""
Groundwork outreach — SMS delivery-status polling (docs/outreach-pipeline-spec.md
Section 15).

Esendex has no per-send push callback for plain SMS delivery status (unlike
Twilio's status_callback) — the only confirmed "account"-product webhook
events are "inbound" and "stop" (see outreach/sms.py's module docstring for
what was checked). This module is the structural replacement: poll
Esendex's Message Headers API for any message we've sent whose last known
status isn't terminal yet, and log a new SmsDeliveryEvent row whenever it
changes — outreach/ramp.py:get_health_signal("sms") already aggregates
these rows exactly the way it did for the Twilio-webhook-fed version, no
changes needed there.

Run this periodically (e.g. hourly — more frequent than the once-daily
send_job.py, since delivery typically resolves within minutes and the ramp
health signal is only as fresh as the last poll):
    python -m outreach.sms_status_poll
"""
import logging
from datetime import datetime, timedelta

from models import SessionLocal, SmsDeliveryEvent, init_db
from outreach.sms import poll_delivery_statuses

logger = logging.getLogger("outreach.sms_status_poll")

TERMINAL_STATUSES = {"delivered", "expired", "failed"}
LOOKBACK_DAYS = 3  # don't bother polling messages older than this — Esendex
                   # resolves delivery within minutes to hours, not days


def _pending_message_ids(db, now):
    """Message ids whose most recent logged status isn't terminal yet,
    restricted to recent sends (older ones are assumed stuck/undeliverable
    rather than polled forever)."""
    cutoff = now - timedelta(days=LOOKBACK_DAYS)
    rows = db.query(SmsDeliveryEvent).filter(
        SmsDeliveryEvent.created_at >= cutoff
    ).order_by(SmsDeliveryEvent.message_sid, SmsDeliveryEvent.created_at.desc()).all()

    latest_status = {}
    for row in rows:
        if row.message_sid not in latest_status:
            latest_status[row.message_sid] = row.status

    return [mid for mid, status in latest_status.items() if (status or "").lower() not in TERMINAL_STATUSES]


def run_sms_status_poll(now=None):
    now = now or datetime.utcnow()
    init_db()

    db = SessionLocal()
    updated = 0
    try:
        pending_ids = _pending_message_ids(db, now)
        if not pending_ids:
            logger.info("sms_status_poll: nothing pending")
            return 0

        statuses = poll_delivery_statuses(pending_ids)
        for message_id, new_status in statuses.items():
            if not new_status:
                continue
            db.add(SmsDeliveryEvent(message_sid=message_id, to_phone="", status=new_status.lower()))
            updated += 1

        db.commit()
        logger.info("sms_status_poll: checked %d pending, %d status updates logged", len(pending_ids), updated)
    finally:
        db.close()

    return updated


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    run_sms_status_poll()
