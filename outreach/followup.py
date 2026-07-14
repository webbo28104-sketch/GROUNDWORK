"""
Groundwork outreach — follow-up sequence (docs/outreach-pipeline-spec.md Section 11).

Runs nightly, after the day's ramp allowance for email and SMS is known
(Section 15, outreach/ramp.py). Follow-ups are first-priority consumers of
that allowance — call run_followups() with the day's full remaining ramp for
*each* channel before queuing any new initial sends, and pass what it
returns on to the initial-send job (outreach/send_job.py).

Email and SMS are tracked as two INDEPENDENT ramps (Section 15 has separate
tables and separate circuit-breakers per channel) — remaining_ramp is a dict
{"email": N, "sms": M}, not a single combined number.

Timing anchor: last_touch_at is updated both when we send a touch AND on
every funnel_substage transition (sent -> opened -> clicked_generated ->
account_created), so "days since last_touch_at" always means "days since
the prospect's last state-changing event," matching the timing rules below.
"""
import os
import logging
from datetime import datetime

from models import SessionLocal, Prospect, SmsDeliveryEvent
from emails import send_outreach_email
from outreach.sms import send_outreach_sms
from outreach.templates import render_email, render_sms, PRE_CLICK_STAGES
from outreach.ramp import record_sends
from outreach.link_identity import ensure_link_identity

logger = logging.getLogger("outreach.followup")

MAX_TOUCHES = 4  # initial + 3 follow-ups, hard cap

# Hard gate (docs/outreach-pipeline-spec.md Section 11a): reply-triggered
# kill-switch handling must exist and be tested on a channel before any
# scheduled follow-up is allowed to fire on it. Email and SMS are gated
# independently — email follow-ups can run as soon as email's flag is true,
# without waiting on SMS (and vice versa), since the two channels' inbound
# reply-capture transports are unrelated and typically go live at different
# times. Each flag defaults to blocked; set it to true only once that
# channel's reply capture is live and verified end-to-end (a real inbound
# reply observed to opt-out/pause a real test prospect on that channel), not
# just once the code merges.
EMAIL_REPLY_CAPTURE_READY = os.environ.get("EMAIL_REPLY_CAPTURE_READY", "").lower() == "true"
# SMS inbound is wired (app.py:/api/webhooks/sms-inbound -> outreach/reply_handling.py).
SMS_REPLY_CAPTURE_READY = os.environ.get("SMS_REPLY_CAPTURE_READY", "").lower() == "true"

# funnel_substage -> follow-up stage letter it fires when due
STAGE_BY_SUBSTAGE = {
    "sent": "A",
    "opened": "B",
    "clicked_generated": "C",
    "account_created": "D",
}

# funnel_substage -> minimum days since last_touch_at before the stage fires
MIN_DAYS_BY_SUBSTAGE = {
    "sent": 4,
    "opened": 4,
    "clicked_generated": 3,
    "account_created": 2,
}

CATCH_ALL_MIN_DAYS = 14
CATCH_ALL_MAX_DAYS = 21


def _days_since(dt, now):
    if dt is None:
        return None
    return (now - dt).days


def _due_stage(p, now):
    """Return the follow-up stage letter due for this prospect, or None."""
    substage_days = _days_since(p.last_touch_at, now)
    if substage_days is not None and substage_days >= MIN_DAYS_BY_SUBSTAGE[p.funnel_substage]:
        return STAGE_BY_SUBSTAGE[p.funnel_substage]

    # Catch-all: 14-21 days since the *original* send, still unpaid, and
    # nothing else has fired for this substage yet.
    first_send_days = _days_since(p.sent_at, now)
    if first_send_days is not None and CATCH_ALL_MIN_DAYS <= first_send_days <= CATCH_ALL_MAX_DAYS:
        return STAGE_BY_SUBSTAGE[p.funnel_substage]

    return None


def _is_catch_all(p, now):
    first_send_days = _days_since(p.sent_at, now)
    return first_send_days is not None and CATCH_ALL_MIN_DAYS <= first_send_days <= CATCH_ALL_MAX_DAYS


def _fire_touch(db, p, stage, now, remaining_ramp, unsubscribe_link_fn, preview_link_fn, short_code_fn):
    """Send the due stage on whichever channels apply and still have ramp
    budget, then update state. Returns (email_used, sms_used) — 0/1 each."""
    email_used = 0
    sms_used = 0
    phone_only = not p.email_found

    # Defensive — should already exist from the initial send, but a legacy
    # row (or an initial send that predates link_identity) shouldn't 404.
    ensure_link_identity(db, p)

    if phone_only:
        # SMS-only track: only "sent" and "clicked_generated" are knowable
        # (no open tracking over SMS) — Stage A copy doubles as the single
        # pre-click follow-up, collapsing A/B into one per the spec.
        sms_stage = "A" if stage in PRE_CLICK_STAGES else stage
        if SMS_REPLY_CAPTURE_READY and not p.sms_unsubscribed and remaining_ramp["sms"] > 0:
            body = render_sms(sms_stage, business_name=p.business_name, short_code=short_code_fn(p))
            sms_id = send_outreach_sms(p.phone, body)
            if sms_id:
                db.add(SmsDeliveryEvent(message_sid=sms_id, to_phone=p.phone, status="submitted"))
            sms_used = 1
    else:
        if EMAIL_REPLY_CAPTURE_READY and not p.email_unsubscribed and remaining_ramp["email"] > 0:
            msg = render_email(
                stage,
                business_name=p.business_name,
                preview_link=preview_link_fn(p),
                unsubscribe_link=unsubscribe_link_fn(p),
            )
            send_outreach_email(p.email, msg["subject"], msg["body"], unsubscribe_link_fn(p))
            email_used = 1
        if SMS_REPLY_CAPTURE_READY and not p.sms_unsubscribed and p.phone and remaining_ramp["sms"] > 0:
            body = render_sms(stage, business_name=p.business_name, short_code=short_code_fn(p))
            sms_id = send_outreach_sms(p.phone, body)
            if sms_id:
                db.add(SmsDeliveryEvent(message_sid=sms_id, to_phone=p.phone, status="submitted"))
            sms_used = 1

    if email_used or sms_used:
        p.touch_count = (p.touch_count or 0) + 1
        p.last_touch_at = now
        if _is_catch_all(p, now):
            p.funnel_substage = "cold"

    return email_used, sms_used


def run_followups(remaining_ramp, unsubscribe_link_fn, preview_link_fn, short_code_fn, now=None):
    """
    Queue due follow-up touches, first-priority against remaining_ramp — a
    dict {"email": N, "sms": M}, how many sends of each channel are still
    allowed today under Section 15's (independent, per-channel) ramps.
    Mutated and returned in place so the caller can pass the same dict on to
    the initial-send job.

    unsubscribe_link_fn/preview_link_fn/short_code_fn: callables taking a
    Prospect and returning the per-prospect URL/code strings, since those
    depend on routing/token logic that lives outside this module.

    A prospect due for a touch where BOTH applicable channels are out of
    ramp budget is left untouched (still due) — it'll be picked up on a
    later run once budget resets. A prospect where only one of two
    applicable channels (email-track sends both) has budget still gets that
    one channel sent and counts as touched, per the "channels are
    independent" rule elsewhere in the spec.
    """
    if not EMAIL_REPLY_CAPTURE_READY and not SMS_REPLY_CAPTURE_READY:
        logger.error(
            "run_followups: blocked — neither EMAIL_REPLY_CAPTURE_READY nor "
            "SMS_REPLY_CAPTURE_READY is 'true'. Reply-triggered kill-switch handling "
            "must be live and tested on a channel before scheduled follow-ups can "
            "fire on it (Section 11a). No touches sent."
        )
        return remaining_ramp
    if not EMAIL_REPLY_CAPTURE_READY:
        logger.warning("run_followups: EMAIL_REPLY_CAPTURE_READY is not 'true' — email follow-ups withheld this run.")
    if not SMS_REPLY_CAPTURE_READY:
        logger.warning("run_followups: SMS_REPLY_CAPTURE_READY is not 'true' — SMS follow-ups withheld this run.")

    now = now or datetime.utcnow()
    db = SessionLocal()
    fired = 0
    try:
        active = db.query(Prospect).filter(
            Prospect.funnel_substage.in_(list(STAGE_BY_SUBSTAGE.keys())),
            Prospect.paid_at.is_(None),
            Prospect.touch_count < MAX_TOUCHES,
        ).all()

        # Deterministic priority order: oldest last_touch_at first, so if the
        # ramp runs out partway through, the most-overdue prospects go first.
        active.sort(key=lambda p: p.last_touch_at or datetime.min)

        for p in active:
            if remaining_ramp["email"] <= 0 and remaining_ramp["sms"] <= 0:
                break
            if p.email_unsubscribed and p.sms_unsubscribed:
                continue

            stage = _due_stage(p, now)
            if not stage:
                continue

            email_used, sms_used = _fire_touch(
                db, p, stage, now, remaining_ramp, unsubscribe_link_fn, preview_link_fn, short_code_fn
            )
            if not (email_used or sms_used):
                continue

            remaining_ramp["email"] -= email_used
            remaining_ramp["sms"] -= sms_used
            record_sends("email", email_used, now, db=db)
            record_sends("sms", sms_used, now, db=db)
            fired += 1
            db.commit()

        logger.info(
            "Follow-ups fired: %d, ramp remaining after — email: %d, sms: %d",
            fired, remaining_ramp["email"], remaining_ramp["sms"],
        )
    finally:
        db.close()

    return remaining_ramp
