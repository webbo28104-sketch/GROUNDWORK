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
import unicodedata

# Deliberately excludes ":" and "/" from the local part now (2026-07-21) —
# the original `[^@\s]+` allowed anything but @ and whitespace, which two
# real production sends slipped through: "http://reallyhandy@gmail.com"
# (a URL scheme accidentally concatenated onto a scraped address somewhere
# upstream — root cause not fully pinned down, but a real email's local
# part never legitimately contains "://") matched this regex fine and only
# failed at Resend's API with "Invalid `to` field." Tightening the
# character class here, the single validator every discovery path
# (outreach/apply_result.py, email_discovery_job.py,
# import_discovery_results.py, app.py's admin apply-email routes) already
# calls before persisting an email, is the one-shot fix for all of them.
EMAIL_RE = re.compile(r"^[^@\s:/]+@[^@\s]+\.[^@\s]+$")

# Invisible/zero-width Unicode characters that a scrape can pick up from
# surrounding HTML (soft hyphens, zero-width spaces/joiners, BOM) without
# them ever being visible in a rendered page — .strip() only removes
# ordinary ASCII whitespace, not these, so a second real production
# example ("info@acleansweepltd.co.uk" + a trailing U+200B) passed the old
# is_valid_email() clean, then failed at Resend with "the email address
# contains non-ASCII characters" since the invisible character survived.
_ZERO_WIDTH_CHARS = "​‌‍⁠﻿­"


def _slugify(name):
    """Lowercase alphanumeric slug — used to detect pattern-matched guesses."""
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def clean_email(email):
    """Strip ordinary whitespace AND invisible zero-width Unicode characters
    (see _ZERO_WIDTH_CHARS above) that can survive a naive .strip() when
    scraped from HTML. Every caller that persists a scraped/found email
    should run it through this before storing, not just is_valid_email —
    validation alone doesn't fix a dirty-but-technically-invalid string,
    it just rejects it; this is what makes an address with a stray
    invisible character actually clean and sendable instead of a
    permanent silent-fail loop."""
    if not email or not isinstance(email, str):
        return email
    for ch in _ZERO_WIDTH_CHARS:
        email = email.replace(ch, "")
    return email.strip()


def is_valid_email(email):
    """Basic sanity check: has @, has a dot in domain, no spaces, no ":"/"/"
    in the local part (rules out a URL fragment ending up in the `to`
    field — see EMAIL_RE's comment), and pure ASCII (Resend rejects
    non-ASCII `to` addresses outright, and every real business email this
    pipeline targets is ASCII in practice)."""
    if not email or not isinstance(email, str):
        return False
    email = clean_email(email)
    if not email.isascii():
        return False
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
