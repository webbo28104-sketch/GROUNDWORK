"""
Groundwork Cashflow — the send job (mirrors outreach/send_job.py).

**Marketing does not start until CASHFLOW_OUTREACH_LIVE=true is set** — the
very first thing this module does at run time is check that flag and no-op
if it isn't "true". This is deliberate: the user asked for this whole
engine to exist and be ready, but not to actually send anything until the
Cashflow product itself has been manually confirmed working end-to-end
(Xero connected, real dashboard verified) — see the plan file's checkpoint.
Setting the env var is necessary but not sufficient to go live in
production, too: no Railway Cron service points at this script yet, which
is the second, actually-load-bearing half of the safety gate (a flag with
nothing scheduled to read it sends nothing regardless).

Ramp sharing: draws from the SAME shared email/SMS deliverability ramp as
outreach/send_job.py (one sending domain, one reputation — see
outreach/ramp.py) — not a second independent ramp, which would let combined
volume exceed what's actually safe. Each job caps its own initial-send
allowance at CASHFLOW_OUTREACH_SHARE (default 0.5, i.e. an even split) of
the fixed per-slot/per-day ceiling, then clamps to whatever's genuinely
still remaining — see outreach/send_job.py's _website_share() docstring for
the full reasoning (this is that same mechanism, the other side of it).
Follow-ups (outreach/cashflow_followup.py) are NOT ramp-limited, same as
website's — see that module and outreach/followup.py's 2026-07-19 decision.
"""
import os
import sys
import math
import logging
from datetime import datetime

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_THIS_DIR)
for _p in (_PROJECT_ROOT, _THIS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from models import SessionLocal, Prospect, init_db
from emails import send_outreach_email
from outreach.sms import send_outreach_sms
from outreach.cashflow_templates import render_sms
from outreach.ramp import (
    advance_or_hold, get_remaining_ramp_today, get_remaining_ramp_this_hour,
    is_within_email_send_window, record_sends,
)
from outreach.cashflow_followup import run_cashflow_followups
from outreach.cashflow_scorer import is_cashflow_eligible
from outreach.link_identity import ensure_link_identity, ensure_cashflow_link_identity
from outreach.email_verify import has_deliverable_domain, has_bounced_before
from outreach.email_discovery import is_valid_email
from outreach.variant_selection import pick_variant, render_variant
from outreach.seed_variants import seed_baseline_variants
from models import OutreachTouch

logger = logging.getLogger("outreach.cashflow_send_job")

BASE_URL = os.environ.get("GROUNDWORK_PUBLIC_URL", "https://groundworkbuild.com")
CASHFLOW_OUTREACH_LIVE = os.environ.get("CASHFLOW_OUTREACH_LIVE", "").lower() == "true"
CASHFLOW_OUTREACH_SHARE = float(os.environ.get("CASHFLOW_OUTREACH_SHARE", "0.5"))


def _signup_link(p):
    return f"{BASE_URL}/cf/{p.cashflow_short_code}"


def _unsubscribe_link(p):
    return f"{BASE_URL}/unsubscribe/{p.token}"


def _eligible_initial_send_query(db):
    """Section 5a's equivalent for Cashflow: the same already-sourced/scored
    Prospect pool, filtered by is_cashflow_eligible() (has_website +
    established + valid email + not unsubscribed + not already contacted
    for THIS campaign — independent of the website campaign's own state on
    the same row). Ordered by review_count as an "established" tiebreak,
    not a full weighted score — see outreach/cashflow_scorer.py."""
    candidates = db.query(Prospect).filter(
        Prospect.website_status.in_(["has_website", "has_website_dated", "has_website_modern"]),
        Prospect.rating.isnot(None),
        Prospect.review_count.isnot(None),
        Prospect.cashflow_funnel_substage.is_(None),
    ).order_by(Prospect.review_count.desc()).all()
    return [p for p in candidates if is_cashflow_eligible(p)]


def send_initial_touch(db, p, now, remaining_ramp):
    ensure_link_identity(db, p)  # shared unsubscribe token
    ensure_cashflow_link_identity(db, p)
    touched = False
    email_id = None

    if p.email and not has_deliverable_domain(p.email):
        logger.warning("Prospect %s: email has no MX/A record — skipping Cashflow initial send", p.id)
        return {"touched": False, "email_id": None}
    if p.email and not is_valid_email(p.email):
        logger.warning("Prospect %s: email fails format validation — skipping Cashflow initial send", p.id)
        return {"touched": False, "email_id": None}

    if not p.email_unsubscribed and remaining_ramp["email"] > 0 and not has_bounced_before(db, p.email):
        variant = pick_variant(db, "initial", product="cashflow")
        msg = render_variant(
            variant, business_name=p.business_name,
            signup_link=_signup_link(p), unsubscribe_link=_unsubscribe_link(p),
        )
        email_id = send_outreach_email(p.email, msg["subject"], msg["body"], _unsubscribe_link(p))
        if email_id:
            db.add(OutreachTouch(prospect_id=p.id, stage="initial", channel="email", sent_at=now,
                                  variant_id=variant.variant_id if variant else None, product="cashflow"))
            remaining_ramp["email"] -= 1
            record_sends("email", 1, now, db=db)
            touched = True

    if p.phone and not p.sms_unsubscribed and remaining_ramp["sms"] > 0:
        body = render_sms("initial", business_name=p.business_name, short_code=p.cashflow_short_code)
        sms_id = send_outreach_sms(p.phone, body)
        if sms_id:
            db.add(OutreachTouch(prospect_id=p.id, stage="initial", channel="sms", sent_at=now, product="cashflow"))
            remaining_ramp["sms"] -= 1
            record_sends("sms", 1, now, db=db)
            touched = True

    if not touched:
        return {"touched": False, "email_id": None}

    p.cashflow_funnel_substage = "sent"
    p.cashflow_sent_at = now
    p.cashflow_last_touch_at = now
    p.cashflow_touch_count = 1
    db.commit()
    return {"touched": True, "email_id": email_id}


def fill_initial_sends(remaining_ramp, now):
    db = SessionLocal()
    sent = 0
    try:
        for p in _eligible_initial_send_query(db):
            if remaining_ramp["email"] <= 0 and remaining_ramp["sms"] <= 0:
                break
            result = send_initial_touch(db, p, now, remaining_ramp)
            if result["touched"]:
                sent += 1
        logger.info("Cashflow initial sends: %d, ramp remaining after — email: %d, sms: %d",
                    sent, remaining_ramp["email"], remaining_ramp["sms"])
    finally:
        db.close()
    return sent


def run_cashflow_send(now=None):
    if not CASHFLOW_OUTREACH_LIVE:
        logger.info("run_cashflow_send: CASHFLOW_OUTREACH_LIVE is not 'true' — no-op, nothing sent.")
        return {"live": False, "initial_sends": 0, "followups_sent": 0}

    now = now or datetime.utcnow()
    init_db()
    _seed_db = SessionLocal()
    try:
        seed_baseline_variants(_seed_db, product="cashflow")
    finally:
        _seed_db.close()

    email_volume = advance_or_hold("email", now)
    sms_volume = advance_or_hold("sms", now)
    in_window = is_within_email_send_window(now)

    remaining = {
        "email": min(get_remaining_ramp_this_hour("email", now), math.ceil(email_volume * CASHFLOW_OUTREACH_SHARE)),
        "sms": min(get_remaining_ramp_today("sms", now), math.ceil(sms_volume * CASHFLOW_OUTREACH_SHARE)),
    }
    logger.info(
        "Cashflow's share (%.0f%%) of this slot's ramp — email: %d remaining (%s), sms: %d remaining",
        CASHFLOW_OUTREACH_SHARE * 100, remaining["email"], "in window" if in_window else "OUTSIDE send window",
        remaining["sms"],
    )

    if os.environ.get("SMS_SENDS_PAUSED", "").lower() == "true":
        remaining["sms"] = 0

    _, n_followups = run_cashflow_followups(remaining, now, BASE_URL)
    n_initial = fill_initial_sends(remaining, now)

    summary = {"live": True, "email_volume": email_volume, "sms_volume": sms_volume,
               "remaining_after": dict(remaining), "followups_sent": n_followups, "initial_sends": n_initial}
    print(summary)
    return summary


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    run_cashflow_send()
