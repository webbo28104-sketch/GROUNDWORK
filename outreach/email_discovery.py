"""
Email discovery for the outreach pipeline.

find_email() asks Claude (with the web_search tool) to find a GENUINELY
PUBLISHED contact email for a business. The overriding rule — enforced both in
the system prompt and again in Python — is that we never send to a guessed
address. A pattern like info@<business-name>.co.uk for a business with no
website is exactly the kind of hallucinated guess we reject.

Returns (email, source) or (None, None). Never raises.
"""
import os
import re
import json
import logging

import anthropic

logger = logging.getLogger("outreach.email_discovery")

DISCOVERY_MODEL = "claude-sonnet-4-6"

SYSTEM_PROMPT = (
    "You are an email finder. NEVER guess or generate an email address. ONLY "
    "return an email you actually found in a real web page or search result. "
    "If you can't find a genuine published email, say so. Do not invent, "
    "pattern-match, or infer an address from the business name or domain — if "
    "you did not literally read it on a page, it does not count."
)

# Basic sanity regex — not a full RFC validator, just enough to reject obvious
# junk before the stronger heuristics below run.
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _slugify(name):
    """Lowercase alphanumeric slug of a business name, used to detect the
    guessed-domain pattern (info@<slug>.co.uk)."""
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def _is_valid_email(email):
    if not email or not isinstance(email, str):
        return False
    email = email.strip()
    if not EMAIL_RE.match(email):
        return False
    local, _, domain = email.partition("@")
    if "." not in domain or " " in email:
        return False
    return True


def _looks_like_guess(email, business_name, website):
    """Heuristic guard against a pattern-matched guess: an address whose domain
    is just the slugified business name, for a business we have NO real website
    for, is almost certainly invented rather than found."""
    if website:
        return False  # a real domain we actually have makes this a plausible real address
    try:
        _local, _, domain = email.partition("@")
        domain_root = domain.split(".")[0].lower()
    except Exception:
        return False
    slug = _slugify(business_name)
    if slug and domain_root and domain_root == slug:
        return True
    return False


def _build_user_prompt(business_name, location, website):
    lines = [
        "Find the genuine published contact email address for this UK trade business.",
        "",
        f"Business name: {business_name}",
        f"Location: {location}",
    ]
    if website:
        lines.append(f"Known website: {website}")
    lines += [
        "",
        "Do the following searches, then read the most promising results:",
        f'1. Search for: "{business_name}" "{location}" email contact',
    ]
    if website:
        lines.append(f'2. Search for: "{business_name}" contact page {website}')
    else:
        lines.append(f'2. Search for: "{business_name}" contact page')
    lines += [
        "",
        "Then respond with ONLY a JSON object, no other text:",
        '{"email": "found@email.com", "source": "web_search"}',
        "or, if you could not find a genuine published email:",
        '{"email": null, "source": null}',
        "",
        "Remember: only return an email you actually saw on a real page. Never guess.",
    ]
    return "\n".join(lines)


def _extract_final_text(resp):
    """Concatenate the text blocks of the final assistant message."""
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
    """Return (email, source) for a real published contact email, or
    (None, None) if none was genuinely found or on any error."""
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
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 4}],
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
    if not _is_valid_email(email):
        logger.info("Discarded invalid email '%s' for '%s'", email, business_name)
        return None, None

    if _looks_like_guess(email, business_name, website):
        logger.warning("Discarded suspected guessed email '%s' for '%s' (no website)", email, business_name)
        return None, None

    logger.info("Found email for '%s': %s", business_name, email)
    return email, source
