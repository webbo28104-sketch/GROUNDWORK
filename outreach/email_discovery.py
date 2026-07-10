"""
Email discovery helpers for the outreach pipeline (Track A).

The pipeline inserts a PendingEmailDiscovery row for each prospect; Cowork
performs the search within its own session and writes the result back via:
    python outreach/apply_result.py email <prospect_id> <found@email.com|null>

═══════════════════════════════════════════════════════════════════════════════
INSTRUCTIONS FOR COWORK — search source order
═══════════════════════════════════════════════════════════════════════════════

Check sources in this exact order. Stop as soon as a genuine email is found.

  1. The business's own website (if one exists)
     — look for mailto: links and plain-text addresses on contact/about pages.

  2. Facebook Business Page
     — search "< business name > < location > Facebook" and check the
       About/Contact section of any matching page.

  3. UK trade directories — in order:
       Checkatrade → Yell → TrustATrader → Rated People → Bark →
       MyBuilder → FreeIndex
     These commonly list contact emails for businesses without their own site,
     which is exactly the no_website segment (our highest-scoring prospects).

  4. General web search as a final fallback.

CRITICAL — no_website prospects (website field is null/empty):
  Step 1 is unavailable by definition. You MUST still check steps 2 and 3
  before concluding no email exists. A plain Google search alone is not enough
  for this segment — directories are where their contact details live.
  If a previous run only tried step 1 or a generic search for a no_website
  prospect, re-run it explicitly against directories before logging null.

HARD RULE: only submit emails you actually saw on a real page. Never
  guess, pattern-match, or infer an address (e.g. info@businessname.co.uk).
  After checking all four source types with nothing found → submit null.

═══════════════════════════════════════════════════════════════════════════════

The validation helpers below are used by apply_result.py before accepting
any submitted email address.
"""
import re
import logging

logger = logging.getLogger("outreach.email_discovery")

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
