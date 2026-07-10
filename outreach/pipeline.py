"""
Outreach pipeline runner — Track A.

Runnable both as a module and as a script from the project root:
    python outreach/pipeline.py --cells 25
    python -m outreach.pipeline --cells 25 --dry-run

What it does (sourcing + qualification only — no sending, no site generation):
  1. Init the DB.
  2. Pick N pending SearchCells (never-searched first).
  3. For each cell: query Google Places, upsert new prospects, stamp the cell.
  4. For every prospect still at funnel_stage="sourced": run the website vision
     check, score it, discover an email, and route it to either
     "awaiting_approval" (email found) or "qualified_no_email".
  5. Print a summary.

Every DB write follows the project's try/finally db.close() pattern, and we
commit after each cell and after each prospect so a crash mid-run never loses
completed work or re-searches a cell.
"""
import os
import sys
import logging
import argparse
from datetime import datetime

# Make the project root importable whether run as a script or a module.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_THIS_DIR)
for _p in (_PROJECT_ROOT, _THIS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Load .env if python-dotenv is available; otherwise rely on the ambient env.
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_PROJECT_ROOT, ".env"))
except Exception:
    pass

from models import SessionLocal, Prospect, init_db  # noqa: E402

try:
    from outreach.sourcer import search_places, get_pending_cells, parse_place, upsert_prospect
    from outreach.vision_check import check_website_status
    from outreach.email_discovery import find_email
    from outreach.scorer import score_prospect
except ImportError:  # pragma: no cover — supports `python outreach/pipeline.py`
    from sourcer import search_places, get_pending_cells, parse_place, upsert_prospect
    from vision_check import check_website_status
    from email_discovery import find_email
    from scorer import score_prospect

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("outreach.pipeline")


def _source_cells(n_cells, dry_run):
    """Search up to n_cells pending cells and upsert new prospects.

    Returns the number of cells searched and the number of new prospects
    created."""
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
            db.commit()  # commit per cell so progress survives a crash
    finally:
        db.close()

    return cells_searched, new_prospects


def _process_sourced(dry_run):
    """Website-check, score and email-discover every prospect still at
    funnel_stage='sourced'. Returns (n_awaiting_approval, n_qualified_no_email)."""
    n_awaiting = 0
    n_no_email = 0

    db = SessionLocal()
    try:
        sourced = db.query(Prospect).filter(Prospect.funnel_stage == "sourced").all()
        logger.info("Processing %d newly sourced prospects", len(sourced))

        for p in sourced:
            if dry_run:
                logger.info("[dry-run] would process: %s (%s)", p.business_name, p.location)
                continue

            try:
                # Passed the closed-business gate at source time.
                p.funnel_stage = "gated"

                p.website_status = check_website_status(p.website)

                p.score = score_prospect(p)
                p.funnel_stage = "scored"

                email, source = find_email(p.business_name, p.location or "", p.website)
                if email:
                    p.email = email
                    p.email_source = source
                    p.email_found = True
                    p.funnel_stage = "awaiting_approval"
                    p.approval_status = "pending"
                    n_awaiting += 1
                else:
                    p.email_found = False
                    p.funnel_stage = "qualified_no_email"
                    n_no_email += 1
            except Exception as e:
                logger.error("Error processing prospect %s (%s): %s", p.id, p.business_name, e)
                p.error_notes = (p.error_notes or "") + f"\n[process] {e}"

            p.processed_at = datetime.utcnow()
            db.commit()  # commit per prospect so partial runs are durable
    finally:
        db.close()

    return n_awaiting, n_no_email


def run_pipeline(n_cells=25, dry_run=False):
    """Run one full Track-A pass. See module docstring."""
    logger.info("Starting outreach pipeline (n_cells=%d, dry_run=%s)", n_cells, dry_run)
    init_db()

    cells_searched, new_prospects = _source_cells(n_cells, dry_run)
    n_awaiting, n_no_email = _process_sourced(dry_run)

    print("")
    print("=" * 52)
    print("Outreach pipeline summary")
    print("-" * 52)
    print(f"  Cells searched:          {cells_searched}")
    print(f"  New prospects sourced:   {new_prospects}")
    print(f"  Now awaiting approval:   {n_awaiting}")
    print(f"  Qualified, no email:     {n_no_email}")
    print("=" * 52)

    return {
        "cells_searched": cells_searched,
        "new_prospects": new_prospects,
        "awaiting_approval": n_awaiting,
        "qualified_no_email": n_no_email,
    }


def main():
    parser = argparse.ArgumentParser(description="Groundwork outreach pipeline (Track A)")
    parser.add_argument("--cells", type=int, default=25, help="number of search cells to process")
    parser.add_argument("--dry-run", action="store_true", help="log actions without calling APIs or writing prospect data")
    args = parser.parse_args()
    run_pipeline(n_cells=args.cells, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
