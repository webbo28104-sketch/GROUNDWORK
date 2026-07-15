"""
Email discovery job — Track A, unattended (Section 4).

Runnable as a module or script from the project root:
    python outreach/email_discovery_job.py
    python -m outreach.email_discovery_job --limit 50 --dry-run

What it does:
  1. Init the DB.
  2. Load pending PendingEmailDiscovery rows (oldest first, optionally capped
     by --limit).
  3. For each: call outreach.email_discovery.find_email() — a real Anthropic
     API call (web_search tool), checking sources in Section 4's order (own
     site -> Facebook -> UK trade directories -> general search). Never
     guesses or pattern-matches an email; only extracts one genuinely found
     in a source.
  4. Write the result straight to the Prospect row and delete the
     PendingEmailDiscovery row — the same fields, same funnel_stage
     transitions (awaiting_approval / qualified_no_email / unreachable) that
     outreach/apply_result.py's `email` command already applies for a
     human/Cowork-submitted result. Reuses that exact finalization logic
     (_try_finalize) rather than duplicating the scoring/funnel-stage rules
     in a second place.
  5. Print a summary: how many found a real email, how many came up empty
     and where they landed (qualified_no_email vs unreachable), how many
     errored.

Needs to run on a real recurring schedule (a Railway Cron service pointed at
this script, same as outreach/domain_billing.py and outreach/pipeline.py) —
there is no in-process scheduler in this codebase.

Environment: DATABASE_URL, ANTHROPIC_API_KEY. Nothing else — this script
never touches Resend, Stripe, Esendex, Porkbun, Cloudflare, etc.
"""
import os
import sys
import time
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

from models import (  # noqa: E402
    SessionLocal, Prospect, PendingEmailDiscovery, init_db,
)

try:
    from outreach.email_discovery import find_email, is_valid_email, looks_like_guess
    from outreach.apply_result import _try_finalize
except ImportError:
    from email_discovery import find_email, is_valid_email, looks_like_guess
    from apply_result import _try_finalize

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("outreach.email_discovery_job")


def _resolve_one(db, pending, dry_run):
    """Discover (or fail to discover) an email for one pending prospect and
    write the result straight to its Prospect row, same field/funnel-stage
    semantics as apply_result.py's `email` command. Returns a status string
    for the run summary."""
    prospect = db.get(Prospect, pending.prospect_id)
    if not prospect:
        logger.warning("PendingEmailDiscovery %s has no matching Prospect (id=%s) — deleting stale row",
                        pending.id, pending.prospect_id)
        if not dry_run:
            db.delete(pending)
            db.commit()
        return "stale"

    if dry_run:
        logger.info("[dry-run] would search for: %s (%s)", prospect.business_name, prospect.location)
        return "dry_run"

    email, source = find_email(prospect.business_name, prospect.location, prospect.website)

    if email:
        # Belt-and-braces: find_email() already validates/guess-checks
        # internally, but apply_result.py's `email` command re-checks
        # anything written to a Prospect row, so this job does the same
        # rather than trusting a single layer.
        if not is_valid_email(email):
            logger.warning("Discarding invalid email '%s' for prospect %s", email, prospect.id)
            email = None
        elif looks_like_guess(email, prospect.business_name, prospect.website):
            logger.warning("Discarding suspected guessed email '%s' for prospect %s", email, prospect.id)
            email = None

    if email:
        prospect.email = email
        prospect.email_source = source
        prospect.email_found = True
        status = "found"
        logger.info("Prospect %s (%s) -> %s (source: %s)", prospect.id, prospect.business_name, email, source)
    else:
        prospect.email_found = False
        status = "not_found"
        logger.info("Prospect %s (%s) -> no genuine email found", prospect.id, prospect.business_name)

    db.delete(pending)
    db.commit()

    # status is already decided and committed above — a failure in
    # finalize() (scoring/funnel-stage advance) must not downgrade a
    # genuinely successful discovery to "errored" in the run summary. Seen
    # in practice: a Windows console's cp1252 encoding chokes on the "→" in
    # _try_finalize's print() after its own commit() already succeeded —
    # harmless on Railway's Linux/UTF-8 container, but proof this status
    # value needs to be locked in before finalize runs, not after.
    try:
        _try_finalize(db, prospect)
    except Exception:
        logger.exception("finalize() failed for prospect %s after email result was already saved — "
                          "email/email_found is correct in the DB, but scoring/funnel_stage may be stale",
                          prospect.id)
    return status


def run_email_discovery(limit=None, dry_run=False, sleep_between=1.0):
    """Run one full pass over the pending email-discovery queue."""
    logger.info("Starting email discovery job (limit=%s, dry_run=%s)", limit, dry_run)
    init_db()

    if not os.environ.get("ANTHROPIC_API_KEY") and not dry_run:
        logger.error("ANTHROPIC_API_KEY not set — every discovery call will fail. Aborting.")
        return {"error": "ANTHROPIC_API_KEY not set"}

    db = SessionLocal()
    try:
        q = db.query(PendingEmailDiscovery).order_by(PendingEmailDiscovery.created_at.asc())
        if limit:
            q = q.limit(limit)
        pending_rows = q.all()
        logger.info("Loaded %d pending email-discovery rows", len(pending_rows))

        counts = {"found": 0, "not_found": 0, "stale": 0, "dry_run": 0}
        for i, pending in enumerate(pending_rows):
            try:
                status = _resolve_one(db, pending, dry_run)
                counts[status] = counts.get(status, 0) + 1
            except Exception:
                logger.exception("Error resolving PendingEmailDiscovery %s (prospect %s)",
                                  pending.id, pending.prospect_id)
                counts["error"] = counts.get("error", 0) + 1
            # Small pause between real API calls — not rate-limit-critical at
            # this volume, just avoids hammering the API back-to-back across
            # a batch that could be in the hundreds.
            if not dry_run and i < len(pending_rows) - 1:
                time.sleep(sleep_between)
    finally:
        db.close()

    print("")
    print("=" * 56)
    print("Email discovery job summary")
    print("-" * 56)
    print(f"  Rows processed:                {sum(counts.values())}")
    print(f"  Genuine email found:           {counts.get('found', 0)}")
    print(f"  No genuine email found:        {counts.get('not_found', 0)}")
    print(f"  Stale rows (no prospect left): {counts.get('stale', 0)}")
    if counts.get("error"):
        print(f"  Errored (see log above):       {counts.get('error', 0)}")
    print("=" * 56)
    print("")

    return counts


def main():
    parser = argparse.ArgumentParser(description="Groundwork outreach email discovery job (Track A)")
    parser.add_argument("--limit", type=int, default=None,
                        help="max pending rows to process this run (default: all)")
    parser.add_argument("--dry-run", action="store_true",
                        help="log what would be searched without calling the Anthropic API or writing data")
    parser.add_argument("--sleep", type=float, default=1.0,
                        help="seconds to pause between API calls (default: 1.0)")
    args = parser.parse_args()
    run_email_discovery(limit=args.limit, dry_run=args.dry_run, sleep_between=args.sleep)


if __name__ == "__main__":
    main()
