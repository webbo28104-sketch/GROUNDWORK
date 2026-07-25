"""
One-off (but safely re-runnable) backfill: re-queues phone-only
(qualified_no_email) prospects that don't yet have a Facebook Page URL
captured, so the nightly email-discovery routine's existing
site:facebook.com search step gets a chance to find one for them.

Why this is needed: Prospect.facebook_page_url was only added 2026-07-24
(docs: models.py's Prospect docstring) — every prospect discovery-
processed BEFORE that date was never checked for a Facebook page at all,
even though the search step that finds it runs as a side effect of the
same email-discovery pass. qualified_no_email prospects (phone on file,
no email — currently ~1,563 in production) are the highest-priority
segment for this, since with SMS deactivated they have no other working
outreach channel until either an email or a Facebook Page is found for
them (see docs/outreach-pipeline-spec.md, "we can't lose these prospects").

Going forward this backfill isn't needed again — outreach/pipeline.py's
_queue_pending() already inserts a PendingEmailDiscovery row for every
newly-sourced prospect regardless of website_status, and the routine
looks for a Facebook page on every row it processes now.

Runnable standalone:
    python outreach/requeue_for_facebook_discovery.py
    python outreach/requeue_for_facebook_discovery.py --dry-run
"""
import os
import sys
import argparse
import logging

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

from models import SessionLocal, Prospect, PendingEmailDiscovery, init_db  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("outreach.requeue_for_facebook_discovery")


def run_requeue(dry_run=False):
    init_db()
    db = SessionLocal()
    try:
        candidates = db.query(Prospect).filter(
            Prospect.email_found.is_(False),
            Prospect.phone.isnot(None),
            Prospect.phone != "",
            (Prospect.facebook_page_url.is_(None)) | (Prospect.facebook_page_url == ""),
        ).all()
        logger.info("Found %d qualified_no_email-style prospect(s) with no Facebook Page captured yet", len(candidates))

        already_pending_ids = {
            pid for (pid,) in db.query(PendingEmailDiscovery.prospect_id).all()
        }

        queued = 0
        for p in candidates:
            if p.id in already_pending_ids:
                continue
            if dry_run:
                queued += 1
                continue
            db.add(PendingEmailDiscovery(
                prospect_id=p.id, business_name=p.business_name, location=p.location or "", website=p.website,
            ))
            queued += 1
        if not dry_run:
            db.commit()

        logger.info("%s %d prospect(s) for the nightly discovery routine's Facebook-page search",
                     "[dry-run] would queue" if dry_run else "Queued", queued)
        return {"candidates": len(candidates), "queued": queued}
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(description="Re-queue phone-only prospects for Facebook-page discovery")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    result = run_requeue(dry_run=args.dry_run)
    print(result)


if __name__ == "__main__":
    main()
