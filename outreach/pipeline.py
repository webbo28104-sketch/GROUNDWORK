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
       - website_status is set immediately, for free, from Places' own website
         field: "has_website" if populated, "no_website" if not. No screenshot,
         no Cowork vision judgment — the site-quality-graded "dated"/"modern"
         fields still exist on the schema for later but are no longer populated
         by this pipeline.
       - Always insert a PendingEmailDiscovery row.
       - Advance funnel_stage to "queued".
  5. Print a summary showing how many prospects are queued for each step.

Scoring and approval-queue population happen AFTER Cowork clears the pending
email-discovery queue via outreach/apply_result.py — not here. This separates
the deterministic sourcing work (bash / code) from the AI-judgment work
(Cowork session), which is now solely email discovery.
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
    SessionLocal, Prospect, PendingEmailDiscovery, init_db,
)

try:
    from outreach.sourcer import search_places, get_pending_cells, parse_place, upsert_prospect
    from outreach.email_scrape import fetch_and_assess_quality
except ImportError:
    from sourcer import search_places, get_pending_cells, parse_place, upsert_prospect
    from email_scrape import fetch_and_assess_quality

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
    """Set website_status (free binary check, no screenshot) and insert
    PendingEmailDiscovery rows for every prospect still at
    funnel_stage='sourced'.
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

                # Website status — free binary check straight off Places' own
                # website field, no screenshot or vision judgment call.
                p.website_status = "has_website" if (p.website and str(p.website).strip()) else "no_website"

                # Website quality — free code-only staleness heuristic (see
                # outreach/site_quality assess in email_scrape.py), computed
                # once here so outreach/scorer.py can read it as a plain
                # stored attribute later without ever fetching live at score
                # time. Best-effort — never blocks queuing on a bad fetch.
                if p.website_status == "has_website":
                    try:
                        p.website_quality = fetch_and_assess_quality(p.website)
                    except Exception as e:
                        logger.warning("Website quality check failed for prospect %s: %s", p.id, e)

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

    # Count what's waiting for Cowork's judgment (email discovery only now)
    db = SessionLocal()
    try:
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
    print(f"  Pending email discovery (total):{pending_email:>3}")
    print(f"  Awaiting approval (total):    {awaiting_approval}")
    print("=" * 56)
    print("")
    print("No manual steps required — both queues drain automatically:")
    print("  - Pending email discovery is cleared overnight by the scheduled")
    print("    Claude Code routine (Drive -> pickup_drive_results.py import).")
    print("  - \"Awaiting approval\" is a legacy label only, kept for audit trail.")
    print("    There is no approval gate: send_job.py's cron picks the top-scored")
    print("    email-found prospects automatically, no human review needed.")
    print("")
    print("Manual override tools (optional, not part of normal operation):")
    print("  Review pending items:  python outreach/apply_result.py pending")
    print("  Apply an email result: python outreach/apply_result.py email <id> <email|null>")

    return {
        "cells_searched": cells_searched,
        "new_prospects": new_prospects,
        "queued": n_queued,
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
