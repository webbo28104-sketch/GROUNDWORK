"""
Fully unattended pickup for the "Groundwork Facebook-page tradesperson
sourcing" routine's results — a genuine SOURCING channel, not enrichment:
these are businesses found directly via a site:facebook.com-style
WebSearch, which may never have existed in Google Places' dataset at all
(the free-tier Places API budget is this pipeline's other real ceiling —
see docs/outreach-pipeline-spec.md's cost notes). Added 2026-07-25.

Same Drive-based handoff every other Claude Code routine in this pipeline
uses (the routine can't reach the DB or push git — confirmed platform
restrictions, see CLAUDE.md): the routine writes
groundwork-facebook-sourced.json to the shared Drive folder; this script,
run on a schedule by facebook-sourcing-pickup-cron, reads it with a plain
read-only Google Drive API key and imports whatever's new.

Idempotent: tracks the last-imported Drive file id in
FacebookSourcingPickupState (single row), same pattern as
outreach/pickup_drive_results.py.

Dedup: a Facebook-sourced business has no google_place_id (Places API
never saw it), so this can't rely on that column's uniqueness the way
outreach/sourcer.py's upsert_prospect does. Deduped instead on an exact
match of facebook_page_url (the strongest signal — the routine only ever
reports a URL it actually found), and secondarily on a case-insensitive
(business_name, location) match, to catch the same business already
sourced via Places under a slightly different facebook_page_url value
(or none at all). Neither is a true fuzzy-match — a genuine near-duplicate
under a differently-formatted name/location can still slip through; this
is a reasonable safety net, not a guarantee.

Environment: DATABASE_URL, GOOGLE_DRIVE_API_KEY, GOOGLE_DRIVE_FOLDER_ID.

Runnable standalone:
    python outreach/pickup_facebook_sourced.py
    python outreach/pickup_facebook_sourced.py --dry-run
"""
import os
import sys
import json
import logging
import argparse

import requests

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_THIS_DIR)
for _p in (_PROJECT_ROOT, _THIS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_PROJECT_ROOT, ".env"))
except Exception:
    pass

from models import SessionLocal, Prospect, FacebookSourcingPickupState, init_db  # noqa: E402

try:
    from outreach.trade_categories import get_trade_by_term, AREA_INCOME_TIER
    from outreach.scorer import score_prospect
    from outreach.email_discovery import is_valid_email, looks_like_guess
    from outreach.sourcing_channels import SOCIALS
except ImportError:
    from trade_categories import get_trade_by_term, AREA_INCOME_TIER
    from scorer import score_prospect
    from email_discovery import is_valid_email, looks_like_guess
    from sourcing_channels import SOCIALS

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("outreach.pickup_facebook_sourced")

DRIVE_API_BASE = "https://www.googleapis.com/drive/v3"
RESULTS_FILENAME = "groundwork-facebook-sourced.json"
REQUEST_TIMEOUT = 30


def _find_latest_file(api_key, folder_id):
    query = f"'{folder_id}' in parents and name = '{RESULTS_FILENAME}' and trashed = false"
    resp = requests.get(
        f"{DRIVE_API_BASE}/files",
        params={"q": query, "key": api_key, "fields": "files(id,name,createdTime)",
                "orderBy": "createdTime desc", "pageSize": 1},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    files = resp.json().get("files", [])
    return files[0] if files else None


def _download_file(api_key, file_id):
    resp = requests.get(
        f"{DRIVE_API_BASE}/files/{file_id}", params={"alt": "media", "key": api_key}, timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.text


def _already_exists(db, facebook_page_url, business_name, location):
    if facebook_page_url:
        existing = db.query(Prospect).filter(Prospect.facebook_page_url == facebook_page_url).first()
        if existing:
            return existing
    if business_name and location:
        from sqlalchemy import func
        existing = db.query(Prospect).filter(
            func.lower(Prospect.business_name) == business_name.strip().lower(),
            func.lower(Prospect.location) == location.strip().lower(),
        ).first()
        if existing:
            return existing
    return None


def _import_one(db, entry):
    """Returns 'created' / 'duplicate' / 'skipped_invalid'."""
    business_name = (entry.get("business_name") or "").strip()
    facebook_page_url = (entry.get("facebook_page_url") or "").strip()
    location = (entry.get("location") or "").strip()

    if not business_name or not facebook_page_url or not facebook_page_url.lower().startswith(("http://", "https://")):
        logger.warning("Skipping invalid entry (missing business_name/facebook_page_url): %s", entry)
        return "skipped_invalid"

    existing = _already_exists(db, facebook_page_url, business_name, location)
    if existing:
        # Still worth capturing the Page URL on an already-known prospect
        # that didn't have one — same value the requeue backfill was
        # chasing, just via a different route (this routine found it
        # directly instead of the discovery routine finding it as a
        # side effect of email discovery).
        if not existing.facebook_page_url:
            existing.facebook_page_url = facebook_page_url
        return "duplicate"

    trade_term = (entry.get("trade") or "").strip()
    canonical_trade, tier = get_trade_by_term(trade_term) if trade_term else (None, "low")

    website = (entry.get("website") or "").strip() or None
    website_status = "has_website" if website else "no_website"

    email = (entry.get("email") or "").strip() or None
    if email:
        if not is_valid_email(email) or looks_like_guess(email, business_name, website):
            email = None

    postcode_area = (entry.get("postcode_area") or "").strip() or None

    prospect = Prospect(
        sourcing_channel=SOCIALS,
        business_name=business_name,
        trade=canonical_trade,
        trade_search_term=trade_term or None,
        trade_tier=tier,
        location=location or None,
        postcode_area=postcode_area,
        income_tier=AREA_INCOME_TIER.get(postcode_area) if postcode_area else None,
        phone=(entry.get("phone") or "").strip() or None,
        website=website,
        website_status=website_status,
        facebook_page_url=facebook_page_url,
        email=email,
        email_source="facebook_sourcing_routine" if email else None,
        email_found=bool(email),
        funnel_stage="queued",
        raw_data=entry,
    )
    prospect.score = score_prospect(prospect)
    db.add(prospect)
    return "created"


def run_pickup(dry_run=False):
    api_key = os.environ.get("GOOGLE_DRIVE_API_KEY")
    folder_id = os.environ.get("GOOGLE_DRIVE_FOLDER_ID")
    if not api_key or not folder_id:
        logger.error("GOOGLE_DRIVE_API_KEY and/or GOOGLE_DRIVE_FOLDER_ID not set — nothing to do")
        return {"error": "missing_config"}

    init_db()

    latest = _find_latest_file(api_key, folder_id)
    if not latest:
        logger.info("No '%s' file found in the Drive folder yet — nothing to import", RESULTS_FILENAME)
        return {"status": "no_file"}

    db = SessionLocal()
    try:
        state = db.query(FacebookSourcingPickupState).first()
        if state and state.last_drive_file_id == latest["id"]:
            logger.info("Latest Drive file (%s, created %s) already imported — nothing new",
                        latest["id"], latest["createdTime"])
            return {"status": "already_imported", "file_id": latest["id"]}

        logger.info("Found new Facebook-sourcing results file: %s (created %s)", latest["id"], latest["createdTime"])
        text = _download_file(api_key, latest["id"])
        entries = json.loads(text)
        logger.info("Downloaded %d entries", len(entries))

        if dry_run:
            logger.info("[dry-run] would import %d entries and mark file %s as imported", len(entries), latest["id"])
            return {"status": "dry_run", "entries": len(entries)}

        counts = {"created": 0, "duplicate": 0, "skipped_invalid": 0}
        for entry in entries:
            try:
                result = _import_one(db, entry)
                counts[result] = counts.get(result, 0) + 1
            except Exception:
                logger.exception("Error importing entry: %s", entry)
                counts["error"] = counts.get("error", 0) + 1
        db.commit()

        if not state:
            state = FacebookSourcingPickupState()
            db.add(state)
        state.last_drive_file_id = latest["id"]
        db.commit()

        logger.info("Import complete: %s", counts)
        return {"status": "ok", "file_id": latest["id"], "counts": counts}
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(description="Pick up and import the Facebook-sourcing routine's latest Drive results file")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    result = run_pickup(dry_run=args.dry_run)
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
