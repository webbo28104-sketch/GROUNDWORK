"""
Groundwork outreach — the daily send job (Track B).

Ties together the ramp (outreach/ramp.py), the follow-up sequence
(outreach/followup.py), and new initial sends into one nightly run:

  1. Advance/hold each channel's ramp for the day (Section 15).
  2. Run follow-ups first — they're first-priority consumers of the ramp.
  3. Fill whatever ramp remains with new initial sends, top-scored first
     (Section 5), across both the email-track and phone-only-track pools.

Click-to-generation infrastructure (/claim/<token>, /s/<short_code>) is now
built in app.py — see docs/outreach-pipeline-spec.md Section 9a for exactly
what's built/tested/assumed there. Token + short_code are generated here,
at the point a send is queued (ensure_link_identity), not before.
"""
import os
import logging
from datetime import datetime

from models import SessionLocal, Prospect, SmsDeliveryEvent, init_db
from emails import send_outreach_email
from outreach.sms import send_outreach_sms
from outreach.templates import render_email, render_sms
from outreach.ramp import advance_or_hold, get_remaining_ramp_today, record_sends
from outreach.followup import run_followups
from outreach.link_identity import ensure_link_identity

logger = logging.getLogger("outreach.send_job")

BASE_URL = os.environ.get("GROUNDWORK_PUBLIC_URL", "https://groundworkbuild.com")


def _preview_link(p):
    return f"{BASE_URL}/claim/{p.token}"


def _short_code(p):
    return p.short_code


def _unsubscribe_link(p):
    return f"{BASE_URL}/unsubscribe/{p.token}"


def _record_sms_submitted(db, message_id, to_phone):
    """
    Log the initial 'submitted' state for a sent SMS, keyed by Esendex's
    message id, so outreach/sms_status_poll.py has something to poll a
    later status against. Esendex has no per-send push callback the way
    Twilio did (see outreach/sms.py's module docstring) — this row is what
    makes the poll-based approach work at all; skipped entirely if the send
    itself failed (message_id is None).
    """
    if not message_id:
        return
    db.add(SmsDeliveryEvent(message_sid=message_id, to_phone=to_phone, status="submitted"))


def _eligible_initial_send_query(db):
    """Section 5a: qualified prospects, automatically eligible, no approval
    gate. Includes both the email-track (funnel_stage='awaiting_approval')
    and the phone-only track (funnel_stage='qualified_no_email', which under
    Section 10a's parallel-SMS-channel design is now a legitimate send
    target via SMS rather than a dead end — see the Section 4/10a
    reconciliation note in the spec)."""
    return db.query(Prospect).filter(
        Prospect.funnel_stage.in_(["awaiting_approval", "qualified_no_email"]),
        Prospect.score.isnot(None),
    ).order_by(Prospect.score.desc())


def send_initial_touch(db, p, now, remaining_ramp=None):
    """
    Send the initial-template touch to one prospect on whichever channel(s)
    apply, and mark it sent. Shared by fill_initial_sends (the automated
    daily batch, ramp-limited) and outreach/send_test.py (a manual one-off
    test send, NOT ramp-limited — pass remaining_ramp=None to skip the ramp
    check entirely, since a single manual test send shouldn't be blocked by
    or count against the day's approved volume).

    Returns {"touched": bool, "email_id": str|None} — email_id is the
    Resend-assigned id if an email actually sent (None if only SMS sent, or
    nothing sent). Callers that only care whether something sent should
    check result["touched"], not truthiness of the whole dict (always
    truthy, since it's a non-empty dict either way).
    """
    ensure_link_identity(db, p)
    phone_only = not p.email_found
    touched = False
    email_id = None
    unlimited = remaining_ramp is None

    if phone_only:
        if p.phone and not p.sms_unsubscribed and (unlimited or remaining_ramp["sms"] > 0):
            body = render_sms("initial", business_name=p.business_name, short_code=_short_code(p))
            sms_id = send_outreach_sms(p.phone, body)
            _record_sms_submitted(db, sms_id, p.phone)
            if not unlimited:
                remaining_ramp["sms"] -= 1
            record_sends("sms", 1, now, db=db)
            touched = True
    else:
        if not p.email_unsubscribed and (unlimited or remaining_ramp["email"] > 0):
            msg = render_email(
                "initial", business_name=p.business_name,
                preview_link=_preview_link(p), unsubscribe_link=_unsubscribe_link(p),
            )
            email_id = send_outreach_email(p.email, msg["subject"], msg["body"], _unsubscribe_link(p))
            if not unlimited:
                remaining_ramp["email"] -= 1
            record_sends("email", 1, now, db=db)
            touched = True
        # Email-track prospects get both channels in parallel, same as the
        # follow-up sequence's channel logic — SMS piggybacks on the same
        # touch if a phone number is on record.
        if p.phone and not p.sms_unsubscribed and (unlimited or remaining_ramp["sms"] > 0):
            body = render_sms("initial", business_name=p.business_name, short_code=_short_code(p))
            sms_id = send_outreach_sms(p.phone, body)
            _record_sms_submitted(db, sms_id, p.phone)
            if not unlimited:
                remaining_ramp["sms"] -= 1
            record_sends("sms", 1, now, db=db)
            touched = True

    if not touched:
        return {"touched": False, "email_id": None}

    p.funnel_stage = "sent"
    p.funnel_substage = "sent"
    p.sent_at = now
    p.sent_at_dow = now.weekday()
    p.sent_at_hour = now.hour
    p.last_touch_at = now
    p.touch_count = 1
    db.commit()
    return {"touched": True, "email_id": email_id}


def fill_initial_sends(remaining_ramp, now):
    """Consume whatever's left of remaining_ramp on new initial sends,
    top-scored first."""
    db = SessionLocal()
    sent = 0
    try:
        candidates = _eligible_initial_send_query(db).all()

        for p in candidates:
            if remaining_ramp["email"] <= 0 and remaining_ramp["sms"] <= 0:
                break
            result = send_initial_touch(db, p, now, remaining_ramp)
            if result["touched"]:
                sent += 1
                if result["email_id"]:
                    logger.info("Prospect %s: email sent, resend id=%s", p.id, result["email_id"])

        logger.info("Initial sends: %d, ramp remaining after — email: %d, sms: %d",
                    sent, remaining_ramp["email"], remaining_ramp["sms"])
    finally:
        db.close()

    return sent


def run_daily_send(now=None):
    now = now or datetime.utcnow()
    init_db()

    email_volume = advance_or_hold("email", now)
    sms_volume = advance_or_hold("sms", now)

    remaining = {
        "email": get_remaining_ramp_today("email", now),
        "sms": get_remaining_ramp_today("sms", now),
    }
    logger.info("Today's ramp — email: %d/day (%d remaining), sms: %d/day (%d remaining)",
                email_volume, remaining["email"], sms_volume, remaining["sms"])

    remaining = run_followups(remaining, _unsubscribe_link, _preview_link, _short_code, now)
    n_initial = fill_initial_sends(remaining, now)

    summary = {
        "email_volume": email_volume,
        "sms_volume": sms_volume,
        "remaining_after": dict(remaining),
        "initial_sends": n_initial,
    }
    print(summary)
    return summary


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    run_daily_send()
