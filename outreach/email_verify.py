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
