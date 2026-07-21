"""
Google Places API (v1, "New Places API") sourcing for the outreach pipeline.

This module is deliberately DB-session-agnostic: callers pass in an open
SQLAlchemy session (db) and are responsible for committing/closing it, matching
the try/finally db.close() pattern used everywhere else in the project.

Flow:
  get_pending_cells(db)  -> which (area x trade) combos to search next
  search_places(query)   -> raw place dicts from Google
  parse_place(raw, ...)  -> flat dict matching Prospect columns
  upsert_prospect(db, d) -> (Prospect | None, created_bool), deduped on place_id
"""
import logging
import time
from datetime import datetime, timedelta

import requests
from sqlalchemy import func

try:
    from models import Prospect, SearchCell
    from outreach.trade_categories import TRADE_CATEGORIES, UK_AREAS, AREA_INCOME_TIER, get_trade_by_term
except ImportError:  # pragma: no cover — supports `python outreach/sourcer.py` style imports
    from models import Prospect, SearchCell
    from trade_categories import TRADE_CATEGORIES, UK_AREAS, AREA_INCOME_TIER, get_trade_by_term

logger = logging.getLogger("outreach.sourcer")

PLACES_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"

# All fields we ask Google to return. Kept as one string so it's identical in
# the request header and easy to audit against parse_place() below.
FIELD_MASK = (
    "places.id,"
    "places.displayName,"
    "places.formattedAddress,"
    "places.location,"
    "places.types,"
    "places.businessStatus,"
    "places.rating,"
    "places.userRatingCount,"
    "places.websiteUri,"
    "places.nationalPhoneNumber"
)


# Places API v1 caps a single response at 20 results, but supports paging
# via nextPageToken/pageToken up to 60 total (3 pages) — added 2026-07-21
# after real production data showed 21% of already-searched cells (22/105)
# hit the 20-result cap exactly, meaning genuine additional results existed
# but were never fetched. Google's docs note a next-page token can take a
# moment to become valid; _PAGE_TOKEN_DELAY_SECONDS is the recommended
# short wait before using it (skipped on the last page, since there's no
# further request to make).
MAX_PAGES = 3
_PAGE_TOKEN_DELAY_SECONDS = 2


def _search_places_one_page(query, api_key, page_token=None):
    """One page of results. Returns (places, next_page_token) — next_page_token
    is None if there's no further page. Returns ([], None) on any failure
    (HTTP error, network error, malformed response) rather than raising, so
    one bad cell/page never kills a pipeline run."""
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": FIELD_MASK + ",nextPageToken",
    }
    body = {"textQuery": query, "regionCode": "GB", "maxResultCount": 20}
    if page_token:
        body["pageToken"] = page_token

    try:
        resp = requests.post(PLACES_SEARCH_URL, headers=headers, json=body, timeout=30)
    except requests.RequestException as e:
        logger.error("search_places network error for '%s': %s", query, e)
        return [], None

    if resp.status_code != 200:
        logger.error("search_places HTTP %s for '%s': %s", resp.status_code, query, resp.text[:300])
        return [], None

    try:
        data = resp.json()
    except ValueError as e:
        logger.error("search_places bad JSON for '%s': %s", query, e)
        return [], None

    return data.get("places", []) or [], data.get("nextPageToken")


def search_places(query, api_key):
    """POST a text query to the Places API and return the list of place
    dicts, following pagination up to MAX_PAGES (60 results total) whenever
    Google reports more are available. Returns [] on any failure (HTTP
    error, network error, malformed response) rather than raising, so one
    bad cell never kills a pipeline run.
    """
    if not api_key:
        logger.error("search_places called with no API key; returning []")
        return []

    all_places = []
    page_token = None
    for page_num in range(1, MAX_PAGES + 1):
        places, next_page_token = _search_places_one_page(query, api_key, page_token)
        all_places.extend(places)
        if not next_page_token or page_num == MAX_PAGES:
            break
        time.sleep(_PAGE_TOKEN_DELAY_SECONDS)
        page_token = next_page_token

    logger.info("search_places '%s' -> %d results (%d page(s))", query, len(all_places), page_num)
    return all_places


def _ensure_all_cells(db):
    """Lazily create any missing (postcode_area x trade_search_term) SearchCell
    rows. Uses one bulk existence check rather than a query per combination."""
    existing = {
        (c.postcode_area, c.trade_search_term)
        for c in db.query(SearchCell.postcode_area, SearchCell.trade_search_term).all()
    }
    created = 0
    for area in UK_AREAS:
        for term, _name, _tier in TRADE_CATEGORIES:
            if (area, term) not in existing:
                db.add(SearchCell(postcode_area=area, trade_search_term=term,
                                  search_count=0, results_found=0))
                created += 1
    if created:
        db.commit()
        logger.info("Lazily created %d missing search cells", created)


# Minimum days a cell must sit since its last search before it's eligible to
# be searched again — added 2026-07-21 so the pool keeps replenishing on a
# real, deliberate cadence rather than an emergent one. Before this, a cell
# became re-searchable the instant the never-searched pool ran dry, which
# happened to be fine ONLY because the grid (10,590 cells pre-expansion) was
# large relative to daily volume — at 25 cells/day a full first pass alone
# takes ~14 months, so the interval was never actually binding in practice.
# Explicit now so it stays correct if daily volume ever increases enough to
# make that emergent spacing too short.
MIN_RESEARCH_INTERVAL_DAYS = 75


def get_pending_cells(db, limit=25):
    """Return up to `limit` SearchCell rows to search next, never-searched
    first, then oldest-searched (but not sooner than
    MIN_RESEARCH_INTERVAL_DAYS since its last search — see that constant).
    If the total number of cells on disk is smaller than the full grid,
    backfill the missing combinations first so coverage can grow over time.

    Never-searched cells are shuffled (func.random()), not taken in
    insertion order. _ensure_all_cells() inserts the full (area x trade)
    grid in UK_AREAS order, which is grouped by region (all ~480 London
    cells first, then South East, ...) — insertion-order selection meant a
    fixed daily limit exhausted one region for weeks before ever reaching
    another, so click-through data only ever built up for whichever region
    happened to be first (real incident: ~19 days straight in London before
    reaching anywhere else, at 25 cells/day). A random draw from the full
    never-searched pool spans regions/income tiers from day one instead,
    without needing to hand-interleave the area list. Already-searched
    cells keep oldest-first ordering — no reason to shuffle re-searches.

    Can return fewer than `limit` rows if both the never-searched pool is
    empty AND every already-searched cell is still within its cooldown
    window — that's expected/correct, not a bug, once the whole grid has
    been touched more recently than MIN_RESEARCH_INTERVAL_DAYS."""
    total = db.query(SearchCell).count()
    if total < len(UK_AREAS) * len(TRADE_CATEGORIES):
        _ensure_all_cells(db)

    unsearched = (
        db.query(SearchCell)
        .filter(SearchCell.last_searched_at.is_(None))
        .order_by(func.random())
        .limit(limit)
        .all()
    )
    if len(unsearched) >= limit:
        return unsearched

    remaining = limit - len(unsearched)
    cooldown_cutoff = datetime.utcnow() - timedelta(days=MIN_RESEARCH_INTERVAL_DAYS)
    research = (
        db.query(SearchCell)
        .filter(SearchCell.last_searched_at.isnot(None), SearchCell.last_searched_at <= cooldown_cutoff)
        .order_by(SearchCell.last_searched_at.asc())
        .limit(remaining)
        .all()
    )
    return unsearched + research


def parse_place(raw, search_term, postcode_area):
    """Flatten a raw Places API place dict into a dict matching Prospect columns.

    Missing/absent fields degrade to None rather than raising, since the Places
    field mask is best-effort (not every business exposes a phone or website).
    """
    display = raw.get("displayName") or {}
    business_name = display.get("text") if isinstance(display, dict) else None

    location = raw.get("location") or {}
    latitude = location.get("latitude") if isinstance(location, dict) else None
    longitude = location.get("longitude") if isinstance(location, dict) else None

    canonical, tier = get_trade_by_term(search_term)

    return {
        "google_place_id": raw.get("id"),
        "business_name": business_name,
        "trade": canonical,
        "trade_search_term": search_term,
        "trade_tier": tier,
        "location": raw.get("formattedAddress"),
        "postcode_area": postcode_area,
        "income_tier": AREA_INCOME_TIER.get(postcode_area),
        "rating": raw.get("rating"),
        "review_count": raw.get("userRatingCount"),
        "business_status": raw.get("businessStatus"),
        "phone": raw.get("nationalPhoneNumber"),
        "website": raw.get("websiteUri"),
        "latitude": latitude,
        "longitude": longitude,
        "types": raw.get("types"),
        "raw_data": raw,
    }


def upsert_prospect(db, place_data):
    """Insert a Prospect for this place if we've never seen its place_id.

    Returns (prospect, created):
      - created=False and prospect=existing row if the place_id is already in DB
      - created=False and prospect=None if place_data has no usable place_id
      - permanently-closed businesses are still stored (funnel_stage=
        "excluded_closed") so we don't re-source and re-process them every run.
    """
    place_id = place_data.get("google_place_id")
    if not place_id:
        logger.warning("upsert_prospect skipping place with no id: %s", place_data.get("business_name"))
        return None, False

    existing = db.query(Prospect).filter(Prospect.google_place_id == place_id).first()
    if existing:
        return existing, False

    status = place_data.get("business_status")
    funnel_stage = "excluded_closed" if status == "CLOSED_PERMANENTLY" else "sourced"

    prospect = Prospect(
        google_place_id=place_id,
        business_name=place_data.get("business_name"),
        trade=place_data.get("trade"),
        trade_search_term=place_data.get("trade_search_term"),
        trade_tier=place_data.get("trade_tier"),
        location=place_data.get("location"),
        postcode_area=place_data.get("postcode_area"),
        income_tier=place_data.get("income_tier"),
        rating=place_data.get("rating"),
        review_count=place_data.get("review_count"),
        business_status=status,
        phone=place_data.get("phone"),
        website=place_data.get("website"),
        latitude=place_data.get("latitude"),
        longitude=place_data.get("longitude"),
        types=place_data.get("types"),
        raw_data=place_data.get("raw_data"),
        funnel_stage=funnel_stage,
    )
    db.add(prospect)
    db.flush()  # assign PK without committing; caller commits per-cell
    return prospect, True
