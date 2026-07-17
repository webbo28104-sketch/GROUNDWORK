"""
One-off (but safely re-runnable) pass to apply the 2026-07-18 scoring
redesign (docs/outreach-pipeline-spec.md Section 5, outreach/scorer.py) to
every existing prospect, so the send queue re-orders under the new weights
without waiting for prospects to naturally re-score one at a time.

Two steps:
  1. Backfill `website_quality` for every "has_website" prospect that
     doesn't have one yet (existing rows predate the new sourcing-time
     check in outreach/pipeline.py). Live HTTP fetch per site, same
     never-crash contract as outreach/email_scrape.py.
  2. Recompute `score_prospect()` for every prospect and save. Cheap/pure —
     no network involved in this step. `outreach/send_job.py`'s eligible
     query already orders by `Prospect.score.desc()` live, so updating the
     stored score is the entire "re-order the queue" step — nothing to
     physically reorder.

Runnable standalone:
    python outreach/rescore_all.py
    python outreach/rescore_all.py --skip-quality-backfill
"""
import os
import sys
import logging
import argparse

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

from models import SessionLocal, Prospect, init_db  # noqa: E402

try:
    from outreach.email_scrape import fetch_and_assess_quality
    from outreach.scorer import score_prospect
except ImportError:
    from email_scrape import fetch_and_assess_quality
    from scorer import score_prospect

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("outreach.rescore_all")


def backfill_website_quality(db):
    rows = db.query(Prospect).filter(
        Prospect.website_status == "has_website",
        Prospect.website_quality.is_(None),
    ).all()
    logger.info("Backfilling website_quality for %d has_website prospect(s)", len(rows))
    counts = {}
    for i, p in enumerate(rows, 1):
        quality = fetch_and_assess_quality(p.website)
        p.website_quality = quality
        counts[quality] = counts.get(quality, 0) + 1
        db.commit()
        if i % 25 == 0:
            logger.info("  ...%d/%d checked", i, len(rows))
    logger.info("Website quality backfill done: %s", counts)
    return counts


def rescore_all(db):
    rows = db.query(Prospect).all()
    changed = 0
    for p in rows:
        new_score = score_prospect(p)
        if p.score != new_score:
            p.score = new_score
            changed += 1
    db.commit()
    logger.info("Rescored %d prospect(s), %d changed value", len(rows), changed)
    return len(rows), changed


def main():
    parser = argparse.ArgumentParser(description="Backfill website_quality + rescore every prospect under the new scoring model")
    parser.add_argument("--skip-quality-backfill", action="store_true",
                         help="skip the live website-fetch step, just recompute scores from existing data")
    args = parser.parse_args()

    init_db()
    db = SessionLocal()
    try:
        if not args.skip_quality_backfill:
            backfill_website_quality(db)
        total, changed = rescore_all(db)
    finally:
        db.close()

    print("")
    print("=" * 56)
    print("Rescore summary")
    print(f"  Prospects rescored: {total}")
    print(f"  Score changed:      {changed}")
    print("=" * 56)


if __name__ == "__main__":
    main()
