"""
Email validation helpers for the outreach pipeline.

REMOVED 2026-07-18: this module used to also contain find_email(), which
called the Anthropic API (web_search tool, ~£15-20 per overnight batch —
see the 2026-07-17 cost investigation) to search Facebook/UK trade
directories/general web for an email when Tier 1 (outreach/email_scrape.py,
free, no-AI) found nothing. Deleted outright, not just disabled behind a
flag, per instruction: no code path in this repo should be able to spend
API credits on discovery again by accident. The email-discovery-cron
Railway service (ran this nightly at 02:00 UTC) has had its
ANTHROPIC_API_KEY removed as a second, infrastructure-level safeguard.

Replaced by a genuinely free alternative — see
docs/outreach-pipeline-spec.md Section 4a for what that is and why (live
tests during the 2026-07-18 build showed Facebook and the major UK trade
directories actively block scraping — Cloudflare bot-challenges, login
walls — even via a real headless browser, so no code-only replacement for
find_email() was viable; the replacement runs as a scheduled Claude Code
routine using WebSearch, which is included in the subscription rather than
metered per-call, not as Python code in this repo).

is_valid_email() / looks_like_guess() are still real, load-bearing
validators — reused by outreach/apply_result.py (human/CLI-submitted
results) and outreach/email_discovery_job.py (Tier 1's own result) to
reject anything that isn't a genuinely found, plausible address. Nothing
below calls out to any API; this file makes zero network requests.
"""
import re

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _slugify(name):
    """Lowercase alphanumeric slug — used to detect pattern-matched guesses."""
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def is_valid_email(email):
    """Basic sanity check: has @, has a dot in domain, no spaces."""
    if not email or not isinstance(email, str):
        return False
    email = email.strip()
    if not EMAIL_RE.match(email):
        return False
    _local, _, domain = email.partition("@")
    return "." in domain and " " not in email


def looks_like_guess(email, business_name, website):
    """Return True if the email looks like a pattern-match guess rather than
    a genuinely found address (e.g. info@johnsmith.co.uk for a business called
    John Smith with no known website). Always returns False when a real website
    URL is on record — a real domain makes the address plausible."""
    if website:
        return False
    try:
        _local, _, domain = email.partition("@")
        domain_root = domain.split(".")[0].lower()
    except Exception:
        return False
    slug = _slugify(business_name)
    return bool(slug and domain_root and domain_root == slug)
