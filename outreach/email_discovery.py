"""
Email discovery for the outreach pipeline (Track A) — Tier 2 (AI-driven).

Tier 1 (outreach/email_scrape.py) is a plain-code, no-AI check of a
prospect's own website (mailto: links, plain-text address on the
homepage/contact page) and runs BEFORE this module — see
outreach/email_discovery_job.py. Only prospects where Tier 1 finds nothing
(including every no_website prospect, for which Tier 1 is a no-op) reach
find_email() here.

find_email() calls the Anthropic API (web_search tool) to search the
remaining sources from docs/outreach-pipeline-spec.md Section 4, in order:

  2. Facebook Business Page — "<business name> <location> Facebook", then
     the About/Contact section of any matching page.
  3. UK trade directories, in order: Checkatrade, Yell, TrustATrader,
     Rated People, Bark, MyBuilder, FreeIndex. These commonly list contact
     emails for businesses without their own site — exactly the no_website
     segment, which scores highest (Section 5) and therefore matters most.
  4. General web search as a final fallback.

(Step 1, the business's own site, is Tier 1's job — not repeated here.)

HARD STOP IS ENFORCED IN CODE, NOT JUST THE PROMPT. Each source above is a
SEPARATE, small Anthropic API call (its own tightly-bounded web_search
max_uses) — find_email() calls them in order and returns the moment one
yields a genuine, validated email, never issuing the next stage's call at
all. This replaced a single big call with max_uses=8 and a "stop as soon as
you find one" prompt instruction: real production data showed that
instruction alone doesn't reliably bound the model — successful calls were
taking 12-38 seconds, consistent with searching well past a first hit rather
than actually stopping. A prompt can ask the model to stop; only code that
never issues the next call can guarantee it.

HARD RULE, enforced both in the prompt and again here in Python: never
generate, guess, or pattern-match a plausible address (e.g.
info@businessname.co.uk). Only extract an email actually found in a real
source. is_valid_email/looks_like_guess below are the Python-side guard —
find_email() runs every stage's result through both before accepting it, the
same check apply_result.py applies to a human/Cowork-submitted result.
"""
import os
import re
import json
import logging

import anthropic

logger = logging.getLogger("outreach.email_discovery")

# Haiku, not Sonnet — this is a narrow, bounded extraction task (find one
# address in web_search results and return JSON), not open-ended reasoning.
# Switched 2026-07-15 to cut cost; paired with the per-source hard-stop
# below, which cuts the number of calls that ever run at all.
DISCOVERY_MODEL = "claude-haiku-4-5"


class EmailDiscoveryAPIError(Exception):
    """Raised when the Anthropic API call itself fails (billing/credit
    errors, rate limits, network errors, etc.) — distinct from a call that
    genuinely succeeded and found no email. A caller MUST NOT treat this the
    same as a real empty result: no search actually happened, so nothing
    about the prospect should be concluded from it. Added 2026-07-15 after a
    real incident where a "credit balance too low" 400 got silently folded
    into (None, None) — identical to a genuine miss — and a batch of ~55
    prospects got wrongly marked qualified_no_email/unreachable with no
    search ever having run against them."""
    pass


TRADE_DIRECTORIES = [
    "Checkatrade", "Yell", "TrustATrader", "Rated People", "Bark", "MyBuilder", "FreeIndex",
]

SYSTEM_PROMPT = (
    "You are an email finder for UK trade businesses. NEVER guess or generate an "
    "email address. ONLY return an email you actually found written on a real web "
    "page. If you can't find a genuine published email from the source(s) you're "
    "told to check, say so. Do not invent, pattern-match, or infer an address from "
    "the business name or domain — if you did not literally read it on a page, it "
    "does not count."
)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

_RESPONSE_FORMAT_INSTRUCTIONS = (
    "\n\nRespond with ONLY a JSON object, no other text:\n"
    '{"email": "found@email.com", "source": "facebook|checkatrade|yell|trustatrader|'
    'ratedpeople|bark|mybuilder|freeindex|web_search"}\n'
    "or, if no genuine published email was found from this source:\n"
    '{"email": null, "source": null}\n\n'
    "Remember: only return an email you actually saw written on a real page. Never guess or pattern-match one."
)

# Each stage is a fully separate, small API call — see module docstring for
# why. (name, max_uses, prompt_builder). Checked in this order; find_email()
# stops at the first one that returns a genuine, validated email.
_STAGE_FACEBOOK = "facebook"
_STAGE_DIRECTORIES = "directories"
_STAGE_GENERAL = "general"


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


def _facebook_prompt(business_name, location, website):
    return (
        f"Find the genuine published contact email address for this UK trade business, "
        f"by checking its Facebook Business Page ONLY.\n\n"
        f"Business name: {business_name}\nLocation: {location}\n\n"
        f'Search for: "{business_name} {location} Facebook", open the most likely matching '
        f"page, and check its About/Contact section for a published email address."
        f"{_RESPONSE_FORMAT_INSTRUCTIONS}"
    )


def _directories_prompt(business_name, location, website):
    directories = ", ".join(TRADE_DIRECTORIES)
    no_website_note = (
        "\n\nThis business has no website on record — directories are where businesses "
        "like this most often have their contact details listed, so check thoroughly "
        "rather than giving up after one or two."
        if not website else ""
    )
    return (
        f"Find the genuine published contact email address for this UK trade business, "
        f"by checking UK trade directories ONLY.\n\n"
        f"Business name: {business_name}\nLocation: {location}\n\n"
        f'Check these directories, in this order, searching "{business_name} {location}" plus '
        f"each directory name: {directories}. Stop as soon as you find a listing with a "
        f"published contact email.{no_website_note}"
        f"{_RESPONSE_FORMAT_INSTRUCTIONS}"
    )


def _general_prompt(business_name, location, website):
    return (
        f"Find the genuine published contact email address for this UK trade business, "
        f"via a general web search (own site and Facebook/directories have already been "
        f"checked and found nothing).\n\n"
        f"Business name: {business_name}\nLocation: {location}\n\n"
        f'Search: "{business_name}" "{location}" email contact, and check the most '
        f"promising results for a published email address."
        f"{_RESPONSE_FORMAT_INSTRUCTIONS}"
    )


_STAGES = [
    (_STAGE_FACEBOOK, 2, _facebook_prompt),
    (_STAGE_DIRECTORIES, 4, _directories_prompt),
    (_STAGE_GENERAL, 2, _general_prompt),
]


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


def _run_stage(client, stage_name, max_uses, prompt):
    """One bounded API call for one source. Returns (email, source) — the
    email is None if this stage genuinely found nothing. Raises
    EmailDiscoveryAPIError if the call itself fails."""
    try:
        resp = client.messages.create(
            model=DISCOVERY_MODEL,
            max_tokens=512,
            system=SYSTEM_PROMPT,
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": max_uses}],
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:
        logger.error("Email discovery API call failed at stage '%s': %s", stage_name, e)
        raise EmailDiscoveryAPIError(str(e)) from e

    text = _extract_final_text(resp)
    parsed = _extract_json(text)
    if parsed is None:
        logger.info("No parseable email JSON at stage '%s' (text: %r)", stage_name, text[:200])
        return None, None

    email = parsed.get("email")
    source = parsed.get("source") or stage_name
    if not email:
        return None, None
    return email.strip(), source


def find_email(business_name, location, website=None):
    """Return (email, source) for a real, genuinely-published contact email,
    checking Facebook -> UK trade directories -> general web search, in that
    order — one small API call per stage, stopping in code the instant a
    stage yields a genuine, validated result (see module docstring).

    Returns (None, None) only when every stage's call genuinely completed
    and none found an email — a real empty result.

    Raises EmailDiscoveryAPIError if any stage's API call itself fails
    (billing/credit, rate limit, network, auth) or the API key is unset —
    NOT the same as a genuine empty result, and a caller must not write
    email_found=False / qualified_no_email off the back of one."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise EmailDiscoveryAPIError("ANTHROPIC_API_KEY not set")

    client = anthropic.Anthropic(api_key=api_key)

    for stage_name, max_uses, prompt_builder in _STAGES:
        prompt = prompt_builder(business_name, location, website)
        email, source = _run_stage(client, stage_name, max_uses, prompt)
        if not email:
            continue

        if not is_valid_email(email):
            logger.info("Discarded invalid email '%s' for '%s' (stage: %s)", email, business_name, stage_name)
            continue

        if looks_like_guess(email, business_name, website):
            logger.warning("Discarded suspected guessed email '%s' for '%s' (stage: %s)",
                            email, business_name, stage_name)
            continue

        logger.info("Found email for '%s': %s (source: %s, stage: %s)", business_name, email, source, stage_name)
        return email, source

    return None, None
