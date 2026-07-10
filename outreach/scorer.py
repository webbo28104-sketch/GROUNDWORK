"""
Prospect scoring for the outreach pipeline.

score_prospect() returns a 0-100 float built from five weighted components:
  Rating         (max 30)
  Website status (max 25) — the biggest lever after rating; no website / dated
                            site is the whole pitch
  Trade tier     (max 20)
  Review count   (max 15)
  Team size      (max 10) — a proxy: assume solo/small (worth 10) unless the
                            name signals a limited company AND it has a large
                            review count, which reads as a bigger firm (0).

The function reads only attributes already on a Prospect row, so it can be
called any time after sourcing + website check.
"""


def _rating_points(rating):
    if rating is None:
        return 0
    if rating >= 4.5:
        return 30
    if rating >= 4.0:
        return 15
    return 0


def _website_points(website_status):
    if website_status == "no_website":
        return 25
    if website_status == "has_website_dated":
        return 20
    if website_status == "has_website_modern":
        return 0
    return 0  # None / unknown


def _tier_points(trade_tier):
    return {"high": 20, "medium": 12, "low": 5}.get(trade_tier, 0)


def _review_points(review_count):
    if review_count is None:
        return 0
    if review_count >= 6:
        return 15
    if review_count >= 2:
        return 8
    if review_count == 1:
        return 3
    return 0


def _team_size_points(business_name, review_count):
    name = (business_name or "").lower()
    is_company = ("ltd" in name) or ("limited" in name) or ("plc" in name)
    if is_company and (review_count or 0) > 100:
        return 0
    return 10


def score_prospect(prospect):
    """Return the total 0-100 score for a Prospect row."""
    total = 0
    total += _rating_points(prospect.rating)
    total += _website_points(prospect.website_status)
    total += _tier_points(prospect.trade_tier)
    total += _review_points(prospect.review_count)
    total += _team_size_points(prospect.business_name, prospect.review_count)
    return float(total)
