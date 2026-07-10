"""
Email discovery helpers for the outreach pipeline (Track A).

The Claude web-search step has been removed. The pipeline now inserts a
PendingEmailDiscovery row for each prospect; Cowork performs the web search
within its own session and writes the result back via:
    python outreach/apply_result.py email <prospect_id> <found@email.com|null>

The validation helpers below are kept here so apply_result.py can import and
run them before accepting any submitted email address.

Hard rule (enforced in apply_result.py): only accept emails that were actually
found on a real web page. Never guess, pattern-match, or infer an address from
the business name or domain.
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
