"""
Content-safety gates for outreach/variant_optimizer_job.py — every generated
candidate variant must pass all of these before being admitted at canary
weight. Also doubles as the isolation-discipline check (a candidate must
change exactly one identifiable element vs. its parent variant).

Section 2 of docs/cold-email-evidence-library.md overrides Section 1 on any
conflict — these checks encode Section 2's constraints directly, since they
are non-negotiable regardless of what a generated variant's copy tries to do.

Gates here are deliberately conservative pattern/heuristic checks, not a
full NLP classifier — false positives (rejecting a fine variant) are cheap
(the job just tries again next run); false negatives (admitting something
that actually violates Section 2) are the real risk this exists to catch.
The seeded baseline templates (outreach/templates.py) are checked against
every gate in tests/test_content_safety.py — they must always pass, since a
gate that rejects the finalized-and-shipped baseline copy would itself be
the bug.
"""
import re
from html import unescape

from outreach.templates import PRE_CLICK_STAGES

# The canonical price actually charged (checkout.html / Stripe) — any
# other £ figure appearing in generated copy is either a typo or a made-up
# number, both of which are pricing-accuracy failures. No setup fee as of
# 2026-07-23 (removed until break-even) — "99" dropped accordingly.
CANONICAL_PRICES_GBP = {"24.99"}

# Phrases claiming the site already exists/is built — only legal on
# post-click stages (C/D), never on initial/A/B. Matched case-insensitively
# against the plain-text (tags stripped) body. Deliberately narrow (matches
# the actual finished-state phrasing the existing C/D copy uses) rather than
# banning the bare word "build" (used correctly in future tense — "we'll
# build a preview" — by every pre-click stage today).
_BUILT_CLAIM_PATTERNS = [
    r"\bis\s+(already\s+)?built\b",
    r"\'s\s+(already\s+)?built\b",
    r"\bhas\s+been\s+built\b",
    r"\bwebsite\s+is\s+ready\b",
    r"\bsite\s+is\s+ready\b",
    r"\bsite\'s\s+ready\b",
    r"\bbuilt\s+and\s+waiting\b",
]

# Classic ESP-flagged spam phrases — checked against the current baseline
# templates (tests/test_content_safety.py) to confirm none of these
# already appear in shipped copy before trusting the list.
_SPAM_PHRASES = [
    "act now", "buy now", "order now", "click here now",
    "100% free", "risk free", "risk-free", "limited time only",
    "congratulations", "you've won", "you have won", "guaranteed income",
    "we guarantee", "cash bonus", "no strings attached", "cheap",
    "urgent", "don't delete",
]

_CTA_TEXT_RE = re.compile(
    r'<a[^>]*style="[^"]*font-weight:bold[^"]*"[^>]*>([^<]*)</a>', re.IGNORECASE
)
_BODY_PARAGRAPH_RE = re.compile(
    r'font-size:15\.5px;line-height:1\.65;color:#2A2A28', re.IGNORECASE
)


def _strip_tags(html_body):
    text = re.sub(r"(?s)<[^>]+>", " ", html_body)
    text = unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def check_no_built_claim_preclick(stage, body):
    """Returns a list of violation strings (empty = passed)."""
    if stage not in PRE_CLICK_STAGES and stage != "initial":
        return []
    plain = _strip_tags(body).lower()
    violations = []
    for pattern in _BUILT_CLAIM_PATTERNS:
        if re.search(pattern, plain):
            violations.append(f"pre-click stage '{stage}' claims the site is already built (matched: {pattern!r})")
    return violations


_OWN_PRICE_RE = re.compile(r"£\s?(\d+(?:\.\d{1,2})?)\s*(?:setup|/\s?month|/\s?mo\b)", re.IGNORECASE)


def check_pricing_accuracy(subject, body):
    """Every £<number> mentioned AS GROUNDWORK'S OWN PRICE (immediately
    followed by "setup" or "/month") must be one of the two real prices.

    Deliberately scoped to that adjacency pattern, not every £ figure in
    the text — the shipped baseline INITIAL_EMAIL template legitimately
    cites a competitor's price for contrast ("most other website services
    charge around £89 a month alone"), which is not a claim about
    Groundwork's own pricing and must not be flagged as one. Found via a
    real test run: an earlier, unscoped version of this check rejected the
    actual shipped baseline template over that exact sentence."""
    text = f"{subject} {_strip_tags(body)}"
    figures = _OWN_PRICE_RE.findall(text)
    violations = []
    for fig in figures:
        if fig not in CANONICAL_PRICES_GBP:
            violations.append(f"states an own-price of £{fig}, not one of the real prices ({', '.join(sorted(CANONICAL_PRICES_GBP))})")
    return violations


def check_spam_trigger_words(subject, body):
    text = f"{subject} {_strip_tags(body)}"
    lower = text.lower()
    violations = []
    for phrase in _SPAM_PHRASES:
        if phrase in lower:
            violations.append(f"contains spam-trigger phrase '{phrase}'")
    if re.search(r"!{2,}", text):
        violations.append("contains a run of 2+ exclamation marks")
    if re.search(r"\bFREE\b", subject):
        violations.append("subject uses bare all-caps 'FREE'")
    # 3+ consecutive fully-capitalised words (excluding the brand name and
    # short acronyms) reads as shouting to spam filters and recipients alike.
    caps_words = re.findall(r"\b[A-Z]{4,}\b", text)
    if len(caps_words) >= 2:
        violations.append(f"multiple all-caps words: {caps_words}")
    return violations


def run_content_safety_gates(stage, subject, body):
    """Runs every gate. Returns (passed: bool, violations: list[str])."""
    violations = []
    violations += check_no_built_claim_preclick(stage, body)
    violations += check_pricing_accuracy(subject, body)
    violations += check_spam_trigger_words(subject, body)
    return (len(violations) == 0), violations


# ── Isolation discipline check ───────────────────────────────────────────
# Every candidate must change exactly one identifiable element vs. its
# parent (point 5 of the build request). This is a heuristic machine check
# on measurable axes (subject text, CTA button text, paragraph count, rough
# body length) — it catches gross violations (claims a subject-only change
# but the body also changed) but cannot verify semantic isolation an LLM
# might still smuggle past it (e.g. rewording a paragraph while keeping the
# same word count). The real discipline is the generation prompt itself;
# this is a backstop, not a proof.

_SUBJECT_VARIABLES = {"subject_length", "subject_format_question", "subject_personalization"}
_BODY_ONLY_VARIABLES = {"personalization_depth", "tone", "length_reduction"}
# cta_wording and paragraph_structure get their own explicit branches below.

_BODY_LENGTH_TOLERANCE_WORDS = 8


def _cta_text(body):
    m = _CTA_TEXT_RE.search(body)
    return m.group(1).strip() if m else None


def _paragraph_count(body):
    return len(_BODY_PARAGRAPH_RE.findall(body))


def _body_word_count(body):
    return len(_strip_tags(body).split())


def check_isolation(isolated_variable, parent_subject, parent_body, candidate_subject, candidate_body):
    """Returns (ok: bool, reason: str) — reason is '' when ok."""
    subject_changed = candidate_subject.strip() != parent_subject.strip()
    body_changed = candidate_body.strip() != parent_body.strip()

    if not subject_changed and not body_changed:
        return False, "candidate is byte-identical to its parent — no change to test"

    if isolated_variable in _SUBJECT_VARIABLES:
        if body_changed:
            return False, f"isolated_variable={isolated_variable!r} claims a subject-only change, but the body also changed"
        if not subject_changed:
            return False, f"isolated_variable={isolated_variable!r} but the subject didn't actually change"
        return True, ""

    if subject_changed:
        return False, f"isolated_variable={isolated_variable!r} is a body-level variable, but the subject also changed"

    cta_p, cta_c = _cta_text(parent_body), _cta_text(candidate_body)
    para_p, para_c = _paragraph_count(parent_body), _paragraph_count(candidate_body)
    words_p, words_c = _body_word_count(parent_body), _body_word_count(candidate_body)

    if isolated_variable == "cta_wording":
        if cta_p == cta_c:
            return False, "isolated_variable='cta_wording' but the CTA button text didn't change"
        if para_p != para_c:
            return False, "CTA changed, but paragraph structure changed too — not isolated"
        if abs(words_c - words_p) > _BODY_LENGTH_TOLERANCE_WORDS:
            return False, f"CTA changed, but body length shifted by {abs(words_c - words_p)} words — not isolated"
        return True, ""

    if isolated_variable == "paragraph_structure":
        if cta_p != cta_c:
            return False, "isolated_variable='paragraph_structure' but the CTA text changed too"
        if para_p == para_c:
            return False, "isolated_variable='paragraph_structure' but paragraph count didn't change"
        return True, ""

    if isolated_variable in _BODY_ONLY_VARIABLES:
        if cta_p != cta_c:
            return False, f"isolated_variable={isolated_variable!r} but the CTA text changed too"
        if para_p != para_c:
            return False, f"isolated_variable={isolated_variable!r} but paragraph count changed too"
        return True, ""

    return False, f"unrecognised isolated_variable {isolated_variable!r}"
