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

import requests

try:
    from models import Prospect, SearchCell
    from outreach.trade_categories import TRADE_CATEGORIES, UK_AREAS, get_trade_by_term
except ImportError:  # pragma: no cover — supports `python outreach/sourcer.py` style imports
    from models import Prospect, SearchCell
    from trade_categories import TRADE_CATEGORIES, UK_AREAS, get_trade_by_term

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


def search_places(query, api_key):
    """POST a text query to the Places API and return the list of place dicts.

    Returns [] on any failure (HTTP error, network error, malformed response)
    rather than raising, so one bad cell never kills a pipeline run.
    """
    if not api_key:
        logger.error("search_places called with no API key; returning []")
        return []

    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": FIELD_MASK,
    }
    body = {"textQuery": query, "regionCode": "GB", "maxResultCount": 20}

    try:
        resp = requests.post(PLACES_SEARCH_URL, headers=headers, json=body, timeout=30)
    except requests.RequestException as e:
        logger.error("search_places network error for '%s': %s", query, e)
        return []

    if resp.status_code != 200:
        logger.error("search_places HTTP %s for '%s': %s", resp.status_code, query, resp.text[:300])
        return []

    try:
        data = resp.json()
    except ValueError as e:
        logger.error("search_places bad JSON for '%s': %s", query, e)
        return []

    places = data.get("places", []) or []
    logger.info("search_places '%s' -> %d results", query, len(places))
    return places


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


def get_pending_cells(db, limit=25):
    """Return up to `limit` SearchCell rows to search next, never-searched
    first (last_searched_at NULLS FIRST), then oldest-searched. If the total
    number of cells on disk is smaller than the full grid, backfill the missing
    combinations first so coverage can grow over time."""
    total = db.query(SearchCell).count()
    if total < len(UK_AREAS) * len(TRADE_CATEGORIES):
        _ensure_all_cells(db)

    # NULLS FIRST so never-searched cells are always picked before any that
    # already have a timestamp. isnot(None) sort key: False (0) sorts before
    # True (1), i.e. NULLs first, then by the timestamp ascending.
    cells = (
        db.query(SearchCell)
        .order_by(
            SearchCell.last_searched_at.isnot(None),
            SearchCell.last_searched_at.asc(),
        )
        .limit(limit)
        .all()
    )
    return cells


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
