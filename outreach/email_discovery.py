"""
Email discovery for the outreach pipeline (Track A).

find_email() calls the Anthropic API directly (web_search tool) to search for
a genuinely published contact email, in this exact source order — stopping as
soon as one is found (docs/outreach-pipeline-spec.md Section 4):

  1. The business's own website, if one exists — mailto: links and
     plain-text addresses on contact/about pages.
  2. Facebook Business Page — "<business name> <location> Facebook", then
     the About/Contact section of any matching page.
  3. UK trade directories, in order: Checkatrade, Yell, TrustATrader,
     Rated People, Bark, MyBuilder, FreeIndex. These commonly list contact
     emails for businesses without their own site — exactly the no_website
     segment, which scores highest (Section 5) and therefore matters most.
  4. General web search as a final fallback.

CRITICAL for no_website prospects: step 1 is unavailable by definition, so
steps 2-3 must be genuinely attempted, not skipped straight to a generic
search — this segment is the strongest pitch precisely because it has no
site, and a search method that systematically fails on it undermines the
whole prioritization.

HARD RULE, enforced both in the prompt and again here in Python: never
generate, guess, or pattern-match a plausible address (e.g.
info@businessname.co.uk). Only extract an email actually found in a real
source. is_valid_email/looks_like_guess below are the Python-side guard —
find_email() runs a submitted address through both before returning it, the
same check apply_result.py applies to a human/Cowork-submitted result.

This was originally a direct-API-call script, then routed through Cowork
(commit d70e464) so a human/AI-judgment session made the call instead of
spending API budget unattended. outreach/email_discovery_job.py (a real,
scheduled, unattended job) needs the direct-call version back — restored
here with the Section 4 ordered-source-list prompt in place of the original
plain 2-search version, reusing the JSON parsing / guess-detection this file
already had rather than rewriting it.
"""
import os
import re
import json
import logging

import anthropic

logger = logging.getLogger("outreach.email_discovery")

DISCOVERY_MODEL = "claude-sonnet-4-6"

TRADE_DIRECTORIES = [
    "Checkatrade", "Yell", "TrustATrader", "Rated People", "Bark", "MyBuilder", "FreeIndex",
]

SYSTEM_PROMPT = (
    "You are an email finder for UK trade businesses. NEVER guess or generate an "
    "email address. ONLY return an email you actually found written on a real web "
    "page. If you can't find a genuine published email after checking all the "
    "sources you're told to check, say so. Do not invent, pattern-match, or infer "
    "an address from the business name or domain — if you did not literally read "
    "it on a page, it does not count."
)

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


def _build_user_prompt(business_name, location, website):
    directories = ", ".join(TRADE_DIRECTORIES)
    lines = [
        f"Find the genuine published contact email address for this UK trade business.",
        "",
        f"Business name: {business_name}",
        f"Location: {location}",
        f"Known website: {website}" if website else "Known website: none — this business has no website on record.",
        "",
        "Check sources in this exact order. Stop as soon as you find a genuine email:",
        "",
    ]
    step = 1
    if website:
        lines.append(f'{step}. The business\'s own website ({website}) — look for mailto: links or a '
                      f'plain-text email on the contact/about page.')
        step += 1
    lines.append(f'{step}. Facebook Business Page — search "{business_name} {location} Facebook" and check '
                 f'the About/Contact section of any matching page.')
    step += 1
    lines.append(f'{step}. UK trade directories, in this order: {directories}. Search "{business_name} '
                 f'{location}" plus each directory name until you find a listing, and check its contact '
                 f'details for an email.')
    step += 1
    lines.append(f'{step}. General web search as a final fallback — "{business_name}" "{location}" email contact.')

    if not website:
        lines += [
            "",
            "IMPORTANT: this business has no website, so step 1 above is not available. You MUST genuinely "
            "check the Facebook and trade-directory steps rather than jumping straight to a generic search — "
            "directories are where businesses without a site most often have their contact details listed.",
        ]

    lines += [
        "",
        "Respond with ONLY a JSON object, no other text:",
        '{"email": "found@email.com", "source": "own_website|facebook|checkatrade|yell|trustatrader|'
        'ratedpeople|bark|mybuilder|freeindex|web_search"}',
        "or, if no genuine published email was found after checking every source above:",
        '{"email": null, "source": null}',
        "",
        "Remember: only return an email you actually saw written on a real page. Never guess or pattern-match one.",
    ]
    return "\n".join(lines)


def _extract_final_text(resp):
    parts = []
    for block in resp.content:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
    return "".join(parts).strip()


def _extract_json(text):
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except ValueError:
            return None
    return None


def find_email(business_name, location, website=None):
    """Return (email, source) for a real, genuinely-published contact email
    found via the Anthropic API + web_search tool, checking sources in the
    Section 4 order (own site -> Facebook -> UK trade directories -> general
    search). Returns (None, None) if nothing genuine was found, the API key
    is unset, or any error occurs — never raises, so a caller looping over a
    batch of prospects can't have one failure take down the run."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        logger.error("ANTHROPIC_API_KEY not set; skipping email discovery")
        return None, None

    try:
        client = anthropic.Anthropic(api_key=api_key)
        resp = client.messages.create(
            model=DISCOVERY_MODEL,
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 8}],
            messages=[{"role": "user", "content": _build_user_prompt(business_name, location, website)}],
        )
    except Exception as e:
        logger.error("Email discovery API call failed for '%s': %s", business_name, e)
        return None, None

    text = _extract_final_text(resp)
    parsed = _extract_json(text)
    if parsed is None:
        logger.info("No parseable email JSON for '%s' (text: %r)", business_name, text[:200])
        return None, None

    email = parsed.get("email")
    source = parsed.get("source") or "web_search"

    if not email:
        return None, None

    email = email.strip()
    if not is_valid_email(email):
        logger.info("Discarded invalid email '%s' for '%s'", email, business_name)
        return None, None

    if looks_like_guess(email, business_name, website):
        logger.warning("Discarded suspected guessed email '%s' for '%s' (no website)", email, business_name)
        return None, None

    logger.info("Found email for '%s': %s (source: %s)", business_name, email, source)
    return email, source
