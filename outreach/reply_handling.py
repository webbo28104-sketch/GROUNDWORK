"""
Groundwork outreach — reply-triggered kill-switch (docs/outreach-pipeline-spec.md
Section 11a).

Shared logic for both inbound channels: match the sender to a Prospect, then
either permanently opt them out (stop-intent keyword) or pause the automated
sequence for human review (any other reply). Channel-specific transport
(app.py's sms-inbound and email-inbound webhook routes) calls into this
module rather than duplicating the matching/decision logic — this module has
no provider-specific code in it at all (SMS provider moved from Twilio to
Esendex without any change here; see outreach/sms.py and app.py's
sms_inbound_webhook for what actually changed).
"""
import re
import logging
from datetime import datetime

from models import Prospect, InboundReply

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


def _apply_reply(db, prospect, body, channel, from_address=None):
    """Mutates prospect in place per the kill-switch rule, AND persists the
    actual message as an InboundReply row (added 2026-07-21 — previously
    the body was received here and then discarded once classified,
    leaving /admin/replies unable to show what anyone actually wrote).
    Caller commits."""
    stop = is_stop_intent(body)
    db.add(InboundReply(
        prospect_id=prospect.id, channel=channel, from_address=from_address,
        body=body, is_stop_intent=stop, received_at=datetime.utcnow(),
    ))
    if stop:
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
    _apply_reply(db, prospect, body, "sms", from_address=from_phone)
    db.commit()
    return prospect


def handle_forced_sms_stop(db, from_phone, body=None):
    """
    Esendex's webhook can classify a message as a "stop" event itself
    (its own opt-out detection, separate from ours) — when it does, honor
    that directly rather than re-running is_stop_intent() against the
    (possibly absent/reformatted) message body, since Esendex has already
    made the classification. Still needs to land in our own Prospect row
    so run_followups()/send_job.py actually respect it — Esendex opting
    someone out on its side doesn't by itself stop us from queuing a send.

    body is optional (2026-07-21) — sms_inbound_webhook already extracts it
    for every event before branching on event_id, so it's logged as an
    InboundReply here too when present, same as the other reply paths,
    rather than this being the one path where a real message never gets
    persisted.
    """
    prospect = find_prospect_by_phone(db, from_phone)
    if not prospect:
        logger.warning("Forced SMS stop from unmatched number %s — no prospect found", from_phone)
        return None
    if body:
        db.add(InboundReply(
            prospect_id=prospect.id, channel="sms", from_address=from_phone,
            body=body, is_stop_intent=True, received_at=datetime.utcnow(),
        ))
    prospect.sms_unsubscribed = True
    prospect.sms_unsubscribed_at = datetime.utcnow()
    logger.info("Prospect %s: Esendex-classified stop event — opted out permanently", prospect.id)
    db.commit()
    return prospect


def handle_inbound_email(db, from_email, body):
    """Wired via Cloudflare Email Routing -> frontend/_worker.js's email()
    handler -> POST /api/webhooks/email-inbound (app.py), which parses
    {from, text} and calls this. (Docstring corrected 2026-07-21 — this
    previously said "not wired to any transport yet", which stopped being
    true once that Worker/route shipped; the reply-body persistence below
    was added the same day, once it turned out the forwarded copy to
    groundwork-build@outlook.com wasn't a reliable enough place to actually
    read replies.)"""
    prospect = find_prospect_by_email(db, from_email)
    if not prospect:
        logger.warning("Inbound email from unmatched address %s — no prospect found", from_email)
        return None
    _apply_reply(db, prospect, body, "email", from_address=from_email)
    db.commit()
    return prospect
