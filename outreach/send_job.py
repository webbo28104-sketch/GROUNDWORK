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
import sys
import logging
from datetime import datetime

# Bootstrap sys.path with the project root before importing models/app-level
# modules — running `python outreach/send_job.py` puts sys.path[0] at this
# file's own directory (outreach/), not the project root, so `from models
# import ...` fails with ModuleNotFoundError unless this runs first. Same
# fix already applied in outreach/domain_billing.py, email_discovery_job.py,
# and pipeline.py — this file was missing it (found 2026-07-16: send-job-cron
# crashed on startup with this exact error before this was added).
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_THIS_DIR)
for _p in (_PROJECT_ROOT, _THIS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from models import SessionLocal, Prospect, SmsDeliveryEvent, OutreachTouch, init_db
from emails import send_outreach_email
from outreach.sms import send_outreach_sms
from outreach.templates import render_email, render_sms
from outreach.ramp import (
    advance_or_hold, get_remaining_ramp_today, get_remaining_ramp_this_hour,
    is_within_email_send_window, record_sends,
)
from outreach.followup import run_followups
from outreach.link_identity import ensure_link_identity
from outreach.email_verify import has_deliverable_domain, has_bounced_before
from outreach.email_discovery import is_valid_email

logger = logging.getLogger("outreach.send_job")

BASE_URL = os.environ.get("GROUNDWORK_PUBLIC_URL", "https://groundworkbuild.com")


def _preview_link(p):
    return f"{BASE_URL}/claim/{p.token}"


def _short_code(p):
    return p.short_code


def _unsubscribe_link(p):
    return f"{BASE_URL}/unsubscribe/{p.token}"


def _survey_link(p):
    return f"{BASE_URL}/survey/{p.token}"


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
    gate. Includes the email-track (funnel_stage='awaiting_approval'), the
    phone-only track (funnel_stage='qualified_no_email', which under
    Section 10a's parallel-SMS-channel design is now a legitimate send
    target via SMS rather than a dead end — see the Section 4/10a
    reconciliation note in the spec), and 'approved' — /admin/outreach's
    approve button (app.py:admin_outreach_approve) moves a prospect's
    funnel_stage to 'approved', a value this query didn't originally
    include, which meant every prospect a human approved became invisible
    to the real send job and would sit there forever (found 2026-07-15,
    while testing a redirected real single-send — 11 real prospects were
    stuck in this state). approval_status is audit-only per the spec (no
    longer a send gate), but funnel_stage='approved' is still a real state
    prospects land in via that UI action and must remain sendable."""
    return db.query(Prospect).filter(
        Prospect.funnel_stage.in_(["awaiting_approval", "qualified_no_email", "approved"]),
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

    # Belt-and-braces re-check right before send, not just at discovery
    # time — catches the ~600-row backlog that predates the MX check
    # (added 2026-07-17 after the first batch's 3 dead-domain bounces), and
    # any domain that genuinely died between discovery and send. Downgrades
    # rather than retrying forever: an undeliverable email is treated the
    # same as "no email found" from here on, so the prospect falls through
    # to the phone-only SMS branch below instead of burning a
    # guaranteed-bounce send every day it's eligible.
    if not phone_only and p.email and not has_deliverable_domain(p.email):
        logger.warning("Prospect %s: email '%s' has no MX/A record — downgrading to phone-only, skipping email send",
                        p.id, p.email)
        p.email_found = False
        p.error_notes = (
            f"{(p.error_notes + ' | ') if p.error_notes else ''}"
            f"email '{p.email}' failed pre-send MX check (undeliverable domain), downgraded to phone-only"
        )
        db.commit()
        phone_only = True

    # Same belt-and-braces idea, added 2026-07-21 after two real production
    # sends failed at Resend's API with "Invalid `to` field" (a URL scheme
    # concatenated onto a scraped address, and a stray zero-width Unicode
    # character surviving .strip()) — outreach/email_discovery.py's
    # is_valid_email() was tightened to reject both going forward, but this
    # catches any row that got a bad address stored BEFORE that fix, so it
    # downgrades once instead of failing this exact same way every day
    # forever (send_outreach_email returns None on failure, so nothing else
    # here would have ever caught or fixed it).
    if not phone_only and p.email and not is_valid_email(p.email):
        logger.warning("Prospect %s: email '%s' fails format validation — downgrading to phone-only, skipping email send",
                        p.id, p.email)
        p.email_found = False
        p.error_notes = (
            f"{(p.error_notes + ' | ') if p.error_notes else ''}"
            f"email '{p.email}' failed pre-send format validation, downgraded to phone-only"
        )
        db.commit()
        phone_only = True

    if phone_only:
        if p.phone and not p.sms_unsubscribed and (unlimited or remaining_ramp["sms"] > 0):
            body = render_sms("initial", business_name=p.business_name, short_code=_short_code(p))
            sms_id = send_outreach_sms(p.phone, body)
            # send_outreach_sms swallows its own failures and returns None
            # rather than raising (see outreach/sms.py) — that keeps a
            # provider outage from crashing the whole run, but it also means
            # a failed send looks identical to a skip unless checked here.
            # Only count this as a real touch (funnel_stage -> "sent", ramp
            # consumed, OutreachTouch written) when a message id actually
            # came back — otherwise a down/misconfigured Esendex would
            # permanently mark every phone-only prospect "sent" without a
            # single real message going out, and they'd never be retried.
            if sms_id:
                _record_sms_submitted(db, sms_id, p.phone)
                db.add(OutreachTouch(prospect_id=p.id, stage="initial", channel="sms", sent_at=now))
                if not unlimited:
                    remaining_ramp["sms"] -= 1
                record_sends("sms", 1, now, db=db)
                touched = True
    else:
        # has_bounced_before is a defense-in-depth check against
        # EmailEventLog directly (2026-07-19) — catches the same address
        # bouncing under a different Prospect row (a re-sourced duplicate,
        # or two listings sharing a generic mailbox), which
        # email_unsubscribed alone (per-prospect) can't see.
        if not p.email_unsubscribed and (unlimited or remaining_ramp["email"] > 0) and not has_bounced_before(db, p.email):
            msg = render_email(
                "initial", business_name=p.business_name,
                preview_link=_preview_link(p), unsubscribe_link=_unsubscribe_link(p),
            )
            email_id = send_outreach_email(p.email, msg["subject"], msg["body"], _unsubscribe_link(p))
            # Same reasoning as the SMS branch above — send_outreach_email
            # also returns None (not an exception) on failure.
            if email_id:
                db.add(OutreachTouch(prospect_id=p.id, stage="initial", channel="email", sent_at=now))
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
            if sms_id:
                _record_sms_submitted(db, sms_id, p.phone)
                db.add(OutreachTouch(prospect_id=p.id, stage="initial", channel="sms", sent_at=now))
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
    """Despite the name (kept for the existing send-job-cron entry point),
    this now runs hourly, not daily — Railway Cron needs its schedule
    updated to fire every hour, e.g. "0 8-19 * * *" (UTC), not once a day.
    Email sends (initial + follow-up) only happen inside the 08:00-19:00
    UTC window (outreach/ramp.py's EMAIL_SEND_WINDOW_*), evenly capped per
    hour via get_remaining_ramp_this_hour rather than one lump daily
    budget — the point is to spread sends across the day so we build real
    data on which hours perform best, with sourcing-cron (free to run as
    often as we like) as the actual volume limiter, not an artificially
    low per-hour cap. SMS is untouched: still one daily budget, no window."""
    now = now or datetime.utcnow()
    init_db()

    email_volume = advance_or_hold("email", now)
    sms_volume = advance_or_hold("sms", now)

    in_window = is_within_email_send_window(now)
    remaining = {
        "email": get_remaining_ramp_this_hour("email", now),
        "sms": get_remaining_ramp_today("sms", now),
    }
    logger.info(
        "This hour's ramp — email: %d/hour (%d remaining, %s), sms: %d/day (%d remaining)",
        email_volume, remaining["email"], "in window" if in_window else "OUTSIDE 08:00-19:00 UTC window",
        sms_volume, remaining["sms"],
    )

    # Manual kill switch for the SMS channel only — set SMS_SENDS_PAUSED=true
    # on this service to hold every SMS send (phone-only initial sends, the
    # parallel-SMS leg on email-track initial sends, and follow-up SMS) while
    # leaving email completely unaffected. Deliberately doesn't touch
    # RampState/DailySendCount — those drive advance_or_hold's week
    # progression and get_health_signal's spam/delivery-rate denominators,
    # and writing a fake "already sent" count into DailySendCount to zero
    # today's allowance would corrupt that accounting. This just caps
    # remaining["sms"] to 0 for this run; both fill_initial_sends and
    # run_followups already treat remaining_ramp["sms"] <= 0 as "no budget
    # left" for every SMS branch, so one flag covers all of them. Remove or
    # unset the var to resume — no other cleanup needed, since nothing
    # persistent was changed.
    if os.environ.get("SMS_SENDS_PAUSED", "").lower() == "true":
        logger.warning(
            "SMS_SENDS_PAUSED=true — holding SMS at 0 for today's run "
            "(was %d remaining). Email is unaffected.", remaining["sms"]
        )
        remaining["sms"] = 0

    # Follow-ups no longer draw against remaining_ramp at all (2026-07-19,
    # executive decision) — they get their own effectively-unlimited
    # budget, so a heavy follow-up day never eats into how many NEW
    # prospects get a first touch. SMS_SENDS_PAUSED above still applies to
    # follow-up SMS too (mirrored into this separate budget), since that's
    # a genuine kill switch, not a volume cap. Individual follow-up sends
    # are still logged via record_sends() inside run_followups (so bounce/
    # complaint-rate monitoring still sees them) and still gated per-send
    # on has_bounced_before/has_delivery_confirmed — this change is purely
    # about not competing with initial sends for the day's volume cap.
    # Email follow-ups are additionally held to 0 outside the 08:00-19:00
    # UTC window (2026-07-21) — "unlimited" budget still shouldn't mean
    # "any hour of the day" now that email sending has a deliberate window.
    followup_budget = {
        "email": float("inf") if in_window else 0,
        "sms": 0 if os.environ.get("SMS_SENDS_PAUSED", "").lower() == "true" else float("inf"),
    }
    _, n_followups = run_followups(followup_budget, _unsubscribe_link, _preview_link, _short_code, _survey_link, now)
    n_initial = fill_initial_sends(remaining, now)

    summary = {
        "email_volume": email_volume,
        "sms_volume": sms_volume,
        "remaining_after": dict(remaining),
        "followups_sent": n_followups,
        "initial_sends": n_initial,
    }
    print(summary)
    return summary


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    run_daily_send()
