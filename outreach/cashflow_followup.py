"""
Groundwork Cashflow — follow-up sequence.

Mirrors outreach/followup.py's timing/priority logic closely, reusing its
constants directly (MIN_DAYS_BY_SUBSTAGE, CATCH_ALL_MIN_DAYS/MAX_DAYS,
MAX_TOUCHES, EMAIL_REPLY_CAPTURE_READY/SMS_REPLY_CAPTURE_READY — the
reply-capture kill-switch gate is channel-level infrastructure, not
product-specific, so both campaigns share the exact same "must be verified
working before scheduled follow-ups fire" gate). Differences:

- Operates on Prospect.cashflow_* fields (cashflow_funnel_substage,
  cashflow_touch_count, cashflow_last_touch_at, etc.) instead of the
  website fields, and logs OutreachTouch(product="cashflow").
- No hail_mary stage, no branding_ps — see cashflow_templates.py's
  module docstring for why.
- Hard-gated on CASHFLOW_OUTREACH_LIVE — this is the actual "don't start
  marketing until the product's confirmed working" switch (see
  cashflow_send_job.py's module docstring for the full reasoning); this
  module is a no-op with nothing sent until the user sets it.
"""
import os
import logging
from datetime import datetime

from models import SessionLocal, Prospect, SmsDeliveryEvent, OutreachTouch
from emails import send_outreach_email
from outreach.sms import send_outreach_sms
from outreach.cashflow_templates import render_email, render_sms
from outreach.ramp import record_sends
from outreach.link_identity import ensure_link_identity, ensure_cashflow_link_identity
from outreach.email_verify import has_bounced_before, has_delivery_confirmed
from outreach.email_discovery import is_valid_email
from outreach.variant_selection import pick_variant, render_variant
from outreach.seed_variants import seed_baseline_variants
from outreach.followup import (
    MIN_DAYS_BY_SUBSTAGE, CATCH_ALL_MIN_DAYS, CATCH_ALL_MAX_DAYS, MAX_TOUCHES,
    EMAIL_REPLY_CAPTURE_READY, SMS_REPLY_CAPTURE_READY,
)

logger = logging.getLogger("outreach.cashflow_followup")

CASHFLOW_OUTREACH_LIVE = os.environ.get("CASHFLOW_OUTREACH_LIVE", "").lower() == "true"

# cashflow_funnel_substage -> follow-up stage letter it fires when due.
# Reuses website's same day-thresholds (MIN_DAYS_BY_SUBSTAGE keyed by these
# same substage names) — no reason for a different cadence yet.
STAGE_BY_SUBSTAGE = {
    "sent": "A",
    "opened": "B",
    "clicked": "C",
    "trial_started": "D",
}


def _days_since(dt, now):
    return None if dt is None else (now - dt).days


def _is_catch_all(p, now):
    days = _days_since(p.cashflow_sent_at, now)
    return days is not None and CATCH_ALL_MIN_DAYS <= days <= CATCH_ALL_MAX_DAYS


def _due_stage(p, now):
    substage_days = _days_since(p.cashflow_last_touch_at, now)
    min_days = MIN_DAYS_BY_SUBSTAGE.get(p.cashflow_funnel_substage)
    if min_days is not None and substage_days is not None and substage_days >= min_days:
        return STAGE_BY_SUBSTAGE[p.cashflow_funnel_substage]
    if _is_catch_all(p, now):
        return STAGE_BY_SUBSTAGE.get(p.cashflow_funnel_substage)
    return None


def _unsubscribe_link(p, base_url):
    return f"{base_url}/unsubscribe/{p.token}"


def _signup_link(p, base_url):
    return f"{base_url}/cf/{p.cashflow_short_code}"


def _fire_touch(db, p, stage, now, remaining_ramp, base_url):
    email_used = sms_used = 0
    phone_only = not p.email_found

    ensure_link_identity(db, p)  # for the shared unsubscribe token
    ensure_cashflow_link_identity(db, p)

    if not phone_only and p.email and not is_valid_email(p.email):
        logger.warning("Prospect %s: email fails format validation — downgrading to phone-only for this touch", p.id)
        p.email_found = False
        phone_only = True

    if phone_only:
        if SMS_REPLY_CAPTURE_READY and not p.sms_unsubscribed and p.phone and remaining_ramp["sms"] > 0:
            sms_stage = "A" if stage in ("A", "B") else stage
            body = render_sms(sms_stage, business_name=p.business_name, short_code=p.cashflow_short_code)
            sms_id = send_outreach_sms(p.phone, body)
            if sms_id:
                db.add(SmsDeliveryEvent(message_sid=sms_id, to_phone=p.phone, status="submitted"))
                db.add(OutreachTouch(prospect_id=p.id, stage=sms_stage, channel="sms", sent_at=now, product="cashflow"))
                sms_used = 1
    else:
        if (EMAIL_REPLY_CAPTURE_READY and not p.email_unsubscribed and remaining_ramp["email"] > 0
                and not has_bounced_before(db, p.email) and has_delivery_confirmed(db, p.email)):
            variant = pick_variant(db, stage, product="cashflow")
            msg = render_variant(
                variant, business_name=p.business_name,
                signup_link=_signup_link(p, base_url), unsubscribe_link=_unsubscribe_link(p, base_url),
            )
            email_id = send_outreach_email(p.email, msg["subject"], msg["body"], _unsubscribe_link(p, base_url))
            if email_id:
                db.add(OutreachTouch(prospect_id=p.id, stage=stage, channel="email", sent_at=now,
                                      variant_id=variant.variant_id if variant else None, product="cashflow"))
                email_used = 1
        if SMS_REPLY_CAPTURE_READY and not p.sms_unsubscribed and p.phone and remaining_ramp["sms"] > 0:
            body = render_sms(stage, business_name=p.business_name, short_code=p.cashflow_short_code)
            sms_id = send_outreach_sms(p.phone, body)
            if sms_id:
                db.add(SmsDeliveryEvent(message_sid=sms_id, to_phone=p.phone, status="submitted"))
                db.add(OutreachTouch(prospect_id=p.id, stage=stage, channel="sms", sent_at=now, product="cashflow"))
                sms_used = 1

    if email_used or sms_used:
        p.cashflow_touch_count = (p.cashflow_touch_count or 0) + 1
        p.cashflow_last_touch_at = now
        if _is_catch_all(p, now):
            p.cashflow_funnel_substage = "cold"

    return email_used, sms_used


def run_cashflow_followups(remaining_ramp, now=None, base_url="https://groundworkbuild.com"):
    """Same shape as outreach.followup.run_followups: mutates and returns
    remaining_ramp, plus how many touches fired."""
    if not CASHFLOW_OUTREACH_LIVE:
        logger.info("run_cashflow_followups: CASHFLOW_OUTREACH_LIVE is not 'true' — no-op.")
        return remaining_ramp, 0
    if not EMAIL_REPLY_CAPTURE_READY and not SMS_REPLY_CAPTURE_READY:
        logger.error("run_cashflow_followups: blocked — neither reply-capture flag is 'true'. No touches sent.")
        return remaining_ramp, 0

    now = now or datetime.utcnow()
    db = SessionLocal()
    fired = 0
    try:
        seed_baseline_variants(db, product="cashflow")
        active = db.query(Prospect).filter(
            Prospect.cashflow_funnel_substage.in_(list(STAGE_BY_SUBSTAGE.keys())),
            Prospect.cashflow_paid_at.is_(None),
            Prospect.cashflow_touch_count < MAX_TOUCHES,
        ).all()
        active.sort(key=lambda p: p.cashflow_last_touch_at or datetime.min)

        for p in active:
            if remaining_ramp["email"] <= 0 and remaining_ramp["sms"] <= 0:
                break
            if p.email_unsubscribed and p.sms_unsubscribed:
                continue
            stage = _due_stage(p, now)
            if not stage:
                continue

            email_used, sms_used = _fire_touch(db, p, stage, now, remaining_ramp, base_url)
            if not (email_used or sms_used):
                continue

            remaining_ramp["email"] -= email_used
            remaining_ramp["sms"] -= sms_used
            record_sends("email", email_used, now, db=db)
            record_sends("sms", sms_used, now, db=db)
            fired += 1
            db.commit()

        logger.info("Cashflow follow-ups fired: %d, ramp remaining after — email: %d, sms: %d",
                    fired, remaining_ramp["email"], remaining_ramp["sms"])
    finally:
        db.close()

    return remaining_ramp, fired
