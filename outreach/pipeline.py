"""
Outreach pipeline runner — Track A (sourcing + queue population).

Runnable as a module or script from the project root:
    python outreach/pipeline.py --cells 25
    python -m outreach.pipeline --cells 25 --dry-run

What it does:
  1. Init the DB.
  2. Pick N pending SearchCells (never-searched first).
  3. For each cell: query Google Places, upsert new prospects, stamp the cell.
  4. For every newly sourced prospect:
       - If it has a website: take a Playwright screenshot and insert a
         PendingVisionCheck row (screenshot_path may be None if load failed).
       - If it has no website: set website_status="no_website" immediately
         (no screenshot or vision queue row needed).
       - Always insert a PendingEmailDiscovery row.
       - Advance funnel_stage to "queued".
  5. Print a summary showing how many prospects are queued for each step.

Scoring and approval-queue population happen AFTER Cowork clears both pending
queues via outreach/apply_result.py — not here. This separates the deterministic
sourcing work (bash / code) from the AI-judgment work (Cowork session).
"""
import os
import sys
import logging
import argparse
from datetime import datetime

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

from models import (  # noqa: E402
    SessionLocal, Prospect, PendingVisionCheck, PendingEmailDiscovery, init_db,
)

try:
    from outreach.sourcer import search_places, get_pending_cells, parse_place, upsert_prospect
    from outreach.vision_check import take_screenshot
except ImportError:
    from sourcer import search_places, get_pending_cells, parse_place, upsert_prospect
    from vision_check import take_screenshot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("outreach.pipeline")


def _source_cells(n_cells, dry_run):
    """Search up to n_cells pending cells and upsert new prospects.
    Returns (cells_searched, new_prospects)."""
    api_key = os.environ.get("GOOGLE_PLACES_API_KEY", "")
    if not api_key:
        logger.warning("GOOGLE_PLACES_API_KEY not set — sourcing will find nothing")

    cells_searched = 0
    new_prospects = 0

    db = SessionLocal()
    try:
        cells = get_pending_cells(db, limit=n_cells)
        logger.info("Loaded %d pending search cells", len(cells))

        for cell in cells:
            query = f"{cell.trade_search_term} in {cell.postcode_area}"
            if dry_run:
                logger.info("[dry-run] would search: %s", query)
                cells_searched += 1
                continue

            raw_results = search_places(query, api_key)

            for raw in raw_results:
                place_data = parse_place(raw, cell.trade_search_term, cell.postcode_area)
                prospect, created = upsert_prospect(db, place_data)
                if created and prospect and prospect.funnel_stage == "sourced":
                    new_prospects += 1

            cell.last_searched_at = datetime.utcnow()
            cell.search_count = (cell.search_count or 0) + 1
            cell.results_found = len(raw_results)
            cells_searched += 1
            db.commit()
    finally:
        db.close()

    return cells_searched, new_prospects


def _queue_pending(dry_run):
    """Screenshot websites and insert PendingVisionCheck / PendingEmailDiscovery
    rows for every prospect still at funnel_stage='sourced'.
    Returns n_queued."""
    n_queued = 0

    db = SessionLocal()
    try:
        sourced = db.query(Prospect).filter(Prospect.funnel_stage == "sourced").all()
        logger.info("Queuing %d newly sourced prospects", len(sourced))

        for p in sourced:
            if dry_run:
                logger.info("[dry-run] would queue: %s (%s)", p.business_name, p.location)
                continue

            try:
                p.funnel_stage = "gated"

                # Vision check ───────────────────────────────────────────────
                if p.website and str(p.website).strip():
                    # Take a screenshot now (deterministic code); the quality
                    # judgment runs later in Cowork.
                    screenshot_path = take_screenshot(p.id, p.website)
                    existing_vc = db.query(PendingVisionCheck).filter(
                        PendingVisionCheck.prospect_id == p.id
                    ).first()
                    if not existing_vc:
                        db.add(PendingVisionCheck(
                            prospect_id=p.id,
                            screenshot_path=screenshot_path,
                        ))
                else:
                    # No website on record — resolve immediately, no queue row.
                    p.website_status = "no_website"

                # Email discovery ─────────────────────────────────────────────
                existing_ed = db.query(PendingEmailDiscovery).filter(
                    PendingEmailDiscovery.prospect_id == p.id
                ).first()
                if not existing_ed:
                    db.add(PendingEmailDiscovery(
                        prospect_id=p.id,
                        business_name=p.business_name,
                        location=p.location or "",
                        website=p.website,
                    ))

                p.funnel_stage = "queued"
                n_queued += 1

            except Exception as e:
                logger.error("Error queuing prospect %s (%s): %s", p.id, p.business_name, e)
                p.error_notes = (p.error_notes or "") + f"\n[queue] {e}"

            db.commit()
    finally:
        db.close()

    return n_queued


def run_pipeline(n_cells=25, dry_run=False):
    """Run one full Track-A sourcing + queue-population pass."""
    logger.info("Starting outreach pipeline (n_cells=%d, dry_run=%s)", n_cells, dry_run)
    init_db()

    cells_searched, new_prospects = _source_cells(n_cells, dry_run)
    n_queued = _queue_pending(dry_run)

    # Count what's waiting for Cowork's judgment
    db = SessionLocal()
    try:
        pending_vision = db.query(PendingVisionCheck).count()
        pending_email = db.query(PendingEmailDiscovery).count()
        awaiting_approval = db.query(Prospect).filter(
            Prospect.funnel_stage == "awaiting_approval"
        ).count()
    finally:
        db.close()

    print("")
    print("=" * 56)
    print("Outreach pipeline summary")
    print("-" * 56)
    print(f"  Cells searched:               {cells_searched}")
    print(f"  New prospects sourced:        {new_prospects}")
    print(f"  Queued this run:              {n_queued}")
    print("-" * 56)
    print(f"  Pending vision checks (total):{pending_vision:>5}")
    print(f"  Pending email discovery (total):{pending_email:>3}")
    print(f"  Awaiting approval (total):    {awaiting_approval}")
    print("=" * 56)
    print("")
    print("Next steps:")
    print("  Review pending items:  python outreach/apply_result.py pending")
    print("  Apply a vision result: python outreach/apply_result.py vision <id> <verdict>")
    print("  Apply an email result: python outreach/apply_result.py email <id> <email|null>")

    return {
        "cells_searched": cells_searched,
        "new_prospects": new_prospects,
        "queued": n_queued,
        "pending_vision": pending_vision,
        "pending_email": pending_email,
        "awaiting_approval": awaiting_approval,
    }


def main():
    parser = argparse.ArgumentParser(description="Groundwork outreach pipeline (Track A)")
    parser.add_argument("--cells", type=int, default=25,
                        help="number of search cells to process (default: 25)")
    parser.add_argument("--dry-run", action="store_true",
                        help="log actions without calling APIs or writing prospect data")
    args = parser.parse_args()
    run_pipeline(n_cells=args.cells, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
