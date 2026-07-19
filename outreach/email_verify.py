"""
Lightweight pre-send deliverability check — DNS MX lookup only.

Added 2026-07-17 after the first real send batch (10 sent, 3 bounced):
all 3 bounces were dead/typo'd domains an MX lookup would have caught for
free, before ever emailing them. No SMTP RCPT probing (unreliable — many
receiving servers accept-then-bounce or block probing outright, and it can
itself look like abuse) and no paid verification API (consistent with
docs/outreach-pipeline-spec.md Section 4's "free/agentic route only" rule,
and Hunter.io-style tools need a known domain anyway, which is exactly the
segment already handled by the discovery pipeline).

This only confirms mail addressed to the domain has somewhere to go — it
cannot confirm a specific mailbox exists. That's the real ceiling of what's
checkable without sending an actual message, and it's still a large
improvement over sending to a domain with no mail server at all.
"""
import re
import logging

import dns.resolver
from sqlalchemy import func

logger = logging.getLogger("outreach.email_verify")

_EMAIL_DOMAIN_RE = re.compile(r"^[^@\s]+@([^@\s]+\.[^@\s]+)$")

_resolver = dns.resolver.Resolver()
_resolver.timeout = 3
_resolver.lifetime = 5

# Per-process cache — many prospects can share a domain-agnostic directory
# (Checkatrade, Yell) as the found-on source but rarely the domain itself,
# so this mostly helps within one run/batch rather than across days, but
# it's free to keep.
_domain_cache = {}


def has_deliverable_domain(email):
    """True if the email's domain has an MX record, or (per RFC 5321
    section 5.1's implicit-MX fallback) at least an A/AAAA record. False
    only when DNS positively resolved the domain and found neither — e.g.
    NXDOMAIN, or a domain that answers but publishes no mail route at all.

    A DNS timeout/resolver error is treated as 'unconfirmed', not 'bad' —
    returns True so a flaky resolver never blocks a real send off the back
    of network noise it had nothing to do with. This function can only
    subtract confidence, never fabricate a false negative from a hiccup."""
    m = _EMAIL_DOMAIN_RE.match((email or "").strip())
    if not m:
        return False
    domain = m.group(1).lower()

    if domain in _domain_cache:
        return _domain_cache[domain]

    result = _lookup(domain)
    _domain_cache[domain] = result
    return result


def _lookup(domain):
    try:
        if len(_resolver.resolve(domain, "MX")) > 0:
            return True
    except dns.resolver.NXDOMAIN:
        return False  # domain itself doesn't exist — no fallback needed
    except dns.resolver.NoAnswer:
        pass  # domain exists, just no MX — fall through to A/AAAA check
    except Exception as e:
        logger.warning("MX lookup errored for domain '%s' (treating as unconfirmed): %s", domain, e)
        return True

    for rtype in ("A", "AAAA"):
        try:
            if len(_resolver.resolve(domain, rtype)) > 0:
                return True
        except Exception:
            continue
    return False


def has_delivery_confirmed(db, email):
    """True if this exact address has a real EmailEventLog 'delivered' row
    — i.e. the initial send actually reached the mailbox, not just that we
    successfully handed it to Resend's API. Added 2026-07-19: a prospect
    entering the 'sent' funnel_substage only means send_outreach_email
    returned an id; it says nothing about whether the message was ever
    actually delivered. Gating follow-ups on this (outreach/followup.py)
    stops a follow-up firing (and consuming ramp) for an address whose
    delivery status is still unknown or came back undelivered by some
    means other than a hard bounce.

    Safe to require rather than merely prefer: checked in production,
    every sent email reliably receives either a 'delivered' or 'bounced'
    event from Resend's webhook, and follow-ups don't fire until
    MIN_DAYS_BY_SUBSTAGE (>=2 days) after the initial send — comfortably
    past any webhook latency, so this doesn't strand a genuinely-delivered
    prospect in limbo waiting on a confirmation that already arrived."""
    from models import EmailEventLog  # deferred: avoids a circular import at module load
    if not email:
        return False
    return db.query(EmailEventLog).filter(
        func.lower(EmailEventLog.to_email) == email.strip().lower(),
        EmailEventLog.event_type.in_(["email.delivered", "delivered"]),
    ).first() is not None


def has_bounced_before(db, email):
    """True if this exact address has a real EmailEventLog bounce row,
    independent of whatever Prospect.email_unsubscribed/funnel_substage
    currently say. Added 2026-07-19 as defense-in-depth, checked right
    before every email send (not just once at bounce-time) — the normal
    path is app.py's Resend webhook setting email_unsubscribed=True the
    moment a bounce comes in, but that depends on a webhook actually
    firing and matching correctly; this is a second, independent check
    against the same source of truth so a missed/failed webhook doesn't
    silently leave a dead address looking sendable indefinitely. Shared by
    outreach/send_job.py (initial sends) and outreach/followup.py (follow-
    ups) rather than each hand-rolling its own query.

    Case-insensitive on purpose — see app.py's resend_events_webhook for
    why (Prospect.email is stored as-scraped, not normalized)."""
    from models import EmailEventLog  # deferred: avoids a circular import at module load
    if not email:
        return False
    return db.query(EmailEventLog).filter(
        func.lower(EmailEventLog.to_email) == email.strip().lower(),
        EmailEventLog.event_type.in_(["email.bounced", "bounced"]),
    ).first() is not None
