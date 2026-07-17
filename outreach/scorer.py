"""
Prospect scoring for the outreach pipeline.

Redesigned 2026-07-18 — see docs/outreach-pipeline-spec.md Section 5 for the
full audit/rationale. Previous version scored 5 factors calibrated against
assumptions that turned out not to match real production data (see that
section for the actual query results this rewrite is based on). The two
biggest problems fixed here:

1. `website_status` gave a flat 10pts to every "has_website" prospect
   regardless of whether that site actually looks like something worth
   replacing — no real differentiation on the single most direct "do they
   need this" signal. Now reads a free, code-only staleness heuristic
   (`website_quality`, computed once at sourcing time — see
   outreach/email_scrape.py:assess_site_quality) instead of a flat value.

2. `review_count` rewarded MORE reviews with MORE points, on the assumption
   more reviews = more established = better prospect. Production data shows
   the opposite: review_count correlates POSITIVELY with already having a
   modern-looking site (has_website_modern prospects have ~2.5x the median
   review count of no_website prospects) — high review count is a proxy for
   "already invested in web presence," not "wants to invest in it now."
   Rebuilt as a non-monotonic curve that rewards a light-to-moderate review
   history (proof of being real and active) and tapers off for very large
   counts (established players, least likely to want to switch).

Also removed: the `team_size` factor (an unreliable "ltd"-in-name string
match — only matched 30% of prospects and had no real predictive basis).
Its 10pts were folded into the review-count redesign above, which already
captures "how established is this business" more defensibly.

score_prospect() returns a 0-100 float built from four weighted components:
  Website status     (max 40) — no_website / has_website x quality heuristic
  Trade tier          (max 20) — unchanged, no evidence against it
  Review count        (max 20) — non-monotonic, see above
  Rating               (max 20) — reduced from 30; real distribution is
                                  heavily clustered at the top end (88% of
                                  prospects are already 4.8+), so it barely
                                  differentiates within the qualified pool —
                                  kept as a smaller legitimacy signal, not a
                                  primary "wants a website" predictor.

The function reads only attributes already on a Prospect row (all stored,
none fetched live), so it can still be called any time after sourcing +
website check, including on every admin-page render.
"""


def _rating_points(rating):
    if rating is None:
        return 0
    if rating >= 4.8:
        return 20
    if rating >= 4.6:
        return 16
    if rating >= 4.3:
        return 12
    return 6


def _website_points(website_status, website_quality=None):
    if website_status == "no_website":
        return 40
    if website_status == "has_website":
        if website_quality == "unreachable":
            # Their own site doesn't load at all — arguably an even
            # stronger pitch than no_website ("yours is down right now").
            return 38
        if website_quality == "dated":
            return 24
        if website_quality == "modern":
            return 6
        # Not yet checked (legacy row from before this heuristic existed,
        # or the check itself failed) — same flat midpoint the old scorer
        # used for every has_website prospect.
        return 14
    # Legacy vision-judged rows (Section 3's retired checklist) — no longer
    # written by the pipeline, but old rows may still carry them.
    if website_status == "has_website_dated":
        return 30
    if website_status == "has_website_modern":
        return 4
    return 0  # None / unknown


def _tier_points(trade_tier):
    return {"high": 20, "medium": 12, "low": 5}.get(trade_tier, 0)


def _review_points(review_count):
    """Non-monotonic: rewards a light-to-moderate review history (proof of
    being a real, active business) and tapers for very large counts
    (established players, least likely to want to switch) — see module
    docstring for the production-data basis. Bucket edges are set from the
    real distribution (median 50, IQR ~19-108), not arbitrary round numbers."""
    if review_count is None:
        return 0
    if review_count == 0:
        return 4  # exists but unproven — could be a brand-new listing
    if review_count < 10:
        return 14
    if review_count < 50:
        return 20  # sweet spot: established enough to be legitimate, not yet big
    if review_count < 150:
        return 10
    return 4  # large, established operation — least likely to feel urgency


def score_prospect(prospect):
    """Return the total 0-100 score for a Prospect row."""
    total = 0
    total += _website_points(prospect.website_status, getattr(prospect, "website_quality", None))
    total += _tier_points(prospect.trade_tier)
    total += _review_points(prospect.review_count)
    total += _rating_points(prospect.rating)
    return float(total)
