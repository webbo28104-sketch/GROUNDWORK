"""
Groundwork outreach — reply-triggered kill-switch (docs/outreach-pipeline-spec.md
Section 11a).

Shared logic for both inbound channels: match the sender to a Prospect, then
either permanently opt them out (stop-intent keyword) or pause the automated
sequence for human review (any other reply). Channel-specific transport
(Twilio webhook route, future email-inbound transport) calls into this module
rather than duplicating the matching/decision logic.
"""
import re
import logging
from datetime import datetime

from models import Prospect

logger = logging.getLogger("outreach.reply_handling")

STOP_KEYWORDS = {"stop", "unsubscribe", "cancel", "quit", "opt out", "optout", "remove", "stopall"}


def is_stop_intent(body):
    normalized = re.sub(r"[^\w\s]", "", (body or "").strip().lower())
    return normalized in STOP_KEYWORDS


def _normalize_phone_key(raw):
    """Last 10 digits, so '020 7946 0958', '+442079460958', and
    '02079460958' all normalize to the same key regardless of country-code
    prefix or formatting."""
    digits = re.sub(r"\D", "", raw or "")
    return digits[-10:] if len(digits) >= 10 else digits


def find_prospect_by_phone(db, from_phone):
    key = _normalize_phone_key(from_phone)
    if not key:
        return None
    candidates = db.query(Prospect).filter(Prospect.phone.isnot(None)).all()
    for p in candidates:
        if _normalize_phone_key(p.phone) == key:
            return p
    return None


def find_prospect_by_email(db, from_email):
    if not from_email:
        return None
    return db.query(Prospect).filter(Prospect.email == from_email.strip().lower()).first()


def _apply_reply(prospect, body, channel):
    """Mutates prospect in place per the kill-switch rule. Caller commits."""
    if is_stop_intent(body):
        now = datetime.utcnow()
        if channel == "sms":
            prospect.sms_unsubscribed = True
            prospect.sms_unsubscribed_at = now
        else:
            prospect.email_unsubscribed = True
            prospect.email_unsubscribed_at = now
        logger.info("Prospect %s: stop-intent via %s — opted out permanently", prospect.id, channel)
    else:
        # Any other reply: a human should look at it, not keep getting
        # scripted follow-ups. "replied" is deliberately outside
        # STAGE_BY_SUBSTAGE (outreach/followup.py) so run_followups()'s
        # query already excludes it — no separate skip check needed there.
        prospect.funnel_substage = "replied"
        logger.info("Prospect %s: non-stop reply via %s — sequence paused for review", prospect.id, channel)


def handle_inbound_sms(db, from_phone, body):
    prospect = find_prospect_by_phone(db, from_phone)
    if not prospect:
        logger.warning("Inbound SMS from unmatched number %s — no prospect found", from_phone)
        return None
    _apply_reply(prospect, body, "sms")
    db.commit()
    return prospect


def handle_inbound_email(db, from_email, body):
    """Not wired to any transport yet — Resend has no inbound-parsing
    product (see docs/outreach-pipeline-spec.md Section 11a). Implemented
    now so whichever transport gets chosen (Cloudflare Email Routing worker,
    IMAP poll, a dedicated inbound-parsing service) only needs to call this
    function, not duplicate the matching/decision logic."""
    prospect = find_prospect_by_email(db, from_email)
    if not prospect:
        logger.warning("Inbound email from unmatched address %s — no prospect found", from_email)
        return None
    _apply_reply(prospect, body, "email")
    db.commit()
    return prospect
