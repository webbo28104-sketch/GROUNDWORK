"""
Import the nightly discovery routine's results from a JSON file the
routine commits to the repo, applying each one through the exact same
validated path outreach/apply_result.py's `email` command already uses
(format check, guess-detection, MX/deliverability check, scoring,
funnel-stage finalize) — no duplicated logic, no new trust boundary.

Added 2026-07-18 alongside outreach/export_pending_batch.py, replacing a
direct WebFetch call to the g/apply-email endpoint after that path proved
unreachable from the discovery routine's execution environment (see that
module's docstring, and docs/outreach-pipeline-spec.md Section 4a's later
addendum). This script runs somewhere with guaranteed DB access (a Railway
Cron service, or run manually) — it never touches groundworkbuild.com over
HTTP at all, so it's unaffected by whatever blocked the routine's own
WebFetch calls.

Expected input: outreach/discovery_batches/discovery_results.json, a JSON
array of objects:
    {"prospect_id": 485, "email": "info@example.co.uk", "source": "own_website"}
    {"prospect_id": 487, "email": null}   # explicit "nothing found" finalize

Runnable standalone:
    python outreach/import_discovery_results.py
    python outreach/import_discovery_results.py --file path/to/results.json
    python outreach/import_discovery_results.py --dry-run
"""
import os
import sys
import json
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

from models import SessionLocal, Prospect, PendingEmailDiscovery, DiscoveryRunLog, init_db  # noqa: E402

try:
    from outreach.email_discovery import is_valid_email, looks_like_guess
    from outreach.email_verify import has_deliverable_domain
    from outreach.scorer import score_prospect
except ImportError:
    from email_discovery import is_valid_email, looks_like_guess
    from email_verify import has_deliverable_domain
    from scorer import score_prospect

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("outreach.import_discovery_results")

DEFAULT_INPUT_PATH = os.path.join(_PROJECT_ROOT, "outreach", "discovery_batches", "discovery_results.json")


def _finalize(db, prospect):
    """Same rule as apply_result.py's _try_finalize — score and advance
    funnel_stage once the email-discovery queue is clear for this prospect."""
    still_pending = db.query(PendingEmailDiscovery).filter(
        PendingEmailDiscovery.prospect_id == prospect.id
    ).first()
    if still_pending:
        return
    from datetime import datetime
    prospect.score = score_prospect(prospect)
    if prospect.email_found:
        prospect.funnel_stage = "awaiting_approval"
        prospect.approval_status = "pending"
    elif prospect.phone:
        prospect.funnel_stage = "qualified_no_email"
        prospect.approval_status = "pending"
    else:
        prospect.funnel_stage = "unreachable"
        prospect.approval_status = "unreachable"
    prospect.processed_at = datetime.utcnow()
    db.commit()


def import_results(path=DEFAULT_INPUT_PATH, dry_run=False):
    init_db()
    if not os.path.exists(path):
        logger.error("No results file at %s — nothing to import", path)
        return {"error": "file_not_found"}

    with open(path) as f:
        results = json.load(f)

    counts = {"applied": 0, "rejected": 0, "finalized_null": 0, "stale": 0}
    db = SessionLocal()
    try:
        for entry in results:
            prospect_id = entry.get("prospect_id")
            email_raw = (entry.get("email") or "").strip()
            source = entry.get("source", "web_search")

            p = db.get(Prospect, prospect_id)
            if not p:
                logger.warning("Prospect %s not found — skipping", prospect_id)
                counts["stale"] += 1
                continue

            if dry_run:
                logger.info("[dry-run] prospect %s (%s) -> %s", prospect_id, p.business_name, email_raw or "null")
                continue

            if email_raw:
                if not is_valid_email(email_raw):
                    logger.warning("Rejected invalid email '%s' for prospect %s", email_raw, prospect_id)
                    counts["rejected"] += 1
                    email_raw = None
                elif looks_like_guess(email_raw, p.business_name, p.website):
                    logger.warning("Rejected guessed-looking email '%s' for prospect %s", email_raw, prospect_id)
                    counts["rejected"] += 1
                    email_raw = None
                elif not has_deliverable_domain(email_raw):
                    logger.warning("Rejected undeliverable domain '%s' for prospect %s", email_raw, prospect_id)
                    counts["rejected"] += 1
                    email_raw = None

            if email_raw:
                p.email = email_raw
                p.email_source = source
                p.email_found = True
                counts["applied"] += 1
                logger.info("Prospect %s (%s) -> %s (source: %s)", prospect_id, p.business_name, email_raw, source)
            else:
                p.email_found = False
                counts["finalized_null"] += 1

            db.query(PendingEmailDiscovery).filter(
                PendingEmailDiscovery.prospect_id == prospect_id
            ).delete()
            db.commit()
            _finalize(db, p)

        if not dry_run:
            db.add(DiscoveryRunLog(
                processed_n=len(results),
                found_n=counts["applied"],
                finalized_null_n=counts["finalized_null"],
                notes=f"Imported from {os.path.basename(path)} (file-relay path, not direct API)",
            ))
            db.commit()
    finally:
        db.close()

    print(f"Imported: {counts}")
    return counts


def main():
    parser = argparse.ArgumentParser(description="Import nightly discovery routine results from a repo JSON file")
    parser.add_argument("--file", default=DEFAULT_INPUT_PATH)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    import_results(path=args.file, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
