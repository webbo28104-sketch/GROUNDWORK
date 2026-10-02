"""
Prospect eligibility for Groundwork Cashflow cold outreach.

Deliberately NOT a rewrite of outreach/scorer.py's weighted 0-100 model —
Cashflow draws from the SAME already-sourced/scored Prospect pool used for
website outreach, just filtered on the opposite signal: website-gen wants
`no_website` prospects (something to sell them), Cashflow wants established
businesses that already run a real site and, per the target audience brief,
likely already use accounting software (rating/review-count as an
"established business" proxy, since neither Google Places nor this schema
has direct visibility into who runs Xero).

is_cashflow_eligible() is a pure boolean gate, not a score — right-sized for
an MVP campaign; if a future pass needs to prioritise WITHIN the eligible
pool, ORDER_BY review_count (as cashflow_send_job.py currently does) is a
reasonable proxy without needing a full weighted model like score_prospect().

A prospect can be eligible for Cashflow outreach independent of whatever
happened in the website campaign — the two funnels are tracked separately
(Prospect.cashflow_* fields, OutreachTouch.product) specifically so a
prospect who ignored/declined the website pitch can still be a good
Cashflow target, and vice versa.
"""

MIN_RATING = 4.0
MIN_REVIEW_COUNT = 10  # "established" proxy — see module docstring


def is_cashflow_eligible(prospect) -> bool:
    if prospect.website_status not in ("has_website", "has_website_dated", "has_website_modern"):
        return False
    if prospect.rating is None or prospect.rating < MIN_RATING:
        return False
    if prospect.review_count is None or prospect.review_count < MIN_REVIEW_COUNT:
        return False
    if not prospect.email or not prospect.email_found:
        return False
    if prospect.email_unsubscribed or prospect.sms_unsubscribed:
        return False
    if prospect.business_status == "CLOSED_PERMANENTLY":
        return False
    # Independent of the website funnel's funnel_stage/funnel_substage —
    # only this campaign's own state gates re-contact.
    if prospect.cashflow_funnel_substage is not None:
        return False
    return True
