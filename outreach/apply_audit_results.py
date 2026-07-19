"""
Apply the one-off web_search email-backlog audit's results (2026-07-19).

Context: production data showed web_search-sourced emails bounce at 31.6%
(vs 0% for emails scraped directly off a business's own site). The nightly
discovery routine's prompt was tightened to require real corroboration
before accepting a web_search-sourced email going forward; this script
applies the same re-check retroactively to the existing backlog, via a
one-off routine run (see outreach/discovery_batches/web_search_audit_batch.json
for what was sent to it).

For each entry:
  keep=true  -> no change, the existing email stays as-is.
  keep=false -> the email is cleared (email=None, email_found=False) and
                the prospect is re-queued for a fresh discovery pass (a
                PendingEmailDiscovery row is added, same shape
                outreach/pipeline.py's _queue_pending creates), rather
                than left with a probably-wrong address on file forever.
                funnel_stage/funnel_substage are left untouched — a
                prospect already 'sent' stays marked as such (historical
                record), and _try_finalize (called once re-discovery
                resolves) will correctly re-derive its stage then.

Expected input: a JSON array of {"prospect_id": int, "keep": bool, "notes": str|null}.

Runnable standalone:
    python outreach/apply_audit_results.py --file path/to/groundwork-audit-results.json
    python outreach/apply_audit_results.py --file path/to/results.json --dry-run
"""
import os
import sys
import json
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

from models import SessionLocal, Prospect, PendingEmailDiscovery, init_db  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("outreach.apply_audit_results")


def apply_audit(path, dry_run=False):
    init_db()
    with open(path) as f:
        results = json.load(f)

    counts = {"kept": 0, "cleared": 0, "stale": 0, "already_pending": 0}
    db = SessionLocal()
    try:
        for entry in results:
            prospect_id = entry.get("prospect_id")
            keep = bool(entry.get("keep"))
            notes = entry.get("notes")

            p = db.get(Prospect, prospect_id)
            if not p:
                logger.warning("Prospect %s not found — skipping", prospect_id)
                counts["stale"] += 1
                continue

            if keep:
                logger.info("Prospect %s (%s) -> KEEP %s (%s)", prospect_id, p.business_name, p.email, notes or "")
                counts["kept"] += 1
                continue

            logger.info("Prospect %s (%s) -> CLEAR %s (%s)", prospect_id, p.business_name, p.email, notes or "")
            if dry_run:
                counts["cleared"] += 1
                continue

            p.email = None
            p.email_found = False

            existing = db.query(PendingEmailDiscovery).filter(
                PendingEmailDiscovery.prospect_id == prospect_id
            ).first()
            if existing:
                counts["already_pending"] += 1
            else:
                db.add(PendingEmailDiscovery(
                    prospect_id=p.id,
                    business_name=p.business_name,
                    location=p.location or "",
                    website=p.website,
                ))
            db.commit()
            counts["cleared"] += 1
    finally:
        db.close()

    print(f"Audit applied: {counts}")
    return counts


def main():
    parser = argparse.ArgumentParser(description="Apply the web_search email backlog audit's keep/clear results")
    parser.add_argument("--file", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    apply_audit(args.file, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
