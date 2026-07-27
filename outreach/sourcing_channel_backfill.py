"""
One-off backfill for Prospect.sourcing_channel on rows that predate the
column (added 2026-07-27). Every row created after this ships already gets
sourcing_channel set explicitly at creation time (outreach/sourcer.py,
outreach/pickup_facebook_sourced.py) — this script only fixes up history.

Rule, matching the explicit policy in models.py's Prospect.sourcing_channel
docstring: google_place_id present -> "google_places", full stop, even if
the row also has a facebook_page_url attached (e.g. from the nightly
email-discovery job's incidental site:facebook.com search) — sourcing
channel is about how the BUSINESS was found, not what else got attached to
it later. Only rows with no google_place_id at all, that DO have a
facebook_page_url, are inferred as "socials" — this is the only sourcing
path that has ever created facebook_page_url-only rows
(outreach/pickup_facebook_sourced.py). Anything matching neither is left
NULL (unknown history) rather than guessed.

Only ever touches rows where sourcing_channel IS NULL — safe to re-run.

Runnable standalone:
    python outreach/sourcing_channel_backfill.py --dry-run
    python outreach/sourcing_channel_backfill.py
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
from outreach.sourcing_channels import GOOGLE_PLACES, SOCIALS  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("outreach.sourcing_channel_backfill")


def run_backfill(dry_run=False):
    init_db()
    db = SessionLocal()
    try:
        pending = db.query(Prospect).filter(Prospect.sourcing_channel.is_(None)).all()

        counts = {GOOGLE_PLACES: 0, SOCIALS: 0, "unknown": 0}
        for p in pending:
            if p.google_place_id:
                value = GOOGLE_PLACES
            elif p.facebook_page_url:
                value = SOCIALS
            else:
                counts["unknown"] += 1
                continue
            counts[value] += 1
            if not dry_run:
                p.sourcing_channel = value

        if not dry_run:
            db.commit()

        logger.info(
            "%s%d rows scanned, %d -> google_places, %d -> socials, %d left unknown (no google_place_id or facebook_page_url)",
            "[dry-run] " if dry_run else "",
            len(pending), counts[GOOGLE_PLACES], counts[SOCIALS], counts["unknown"],
        )
        return counts
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(description="Backfill Prospect.sourcing_channel on pre-existing rows")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    run_backfill(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
