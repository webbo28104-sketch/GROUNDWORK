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

     If find_email() raises EmailDiscoveryAPIError (billing/credit, rate
     limit, network, auth — the API call itself failed, no search happened),
     the PendingEmailDiscovery row is left untouched for automatic retry
     next run, NOT deleted, and the Prospect is NOT written to. This is a
     hard-learned distinction (2026-07-15 incident: a "credit balance too
     low" 400 got silently folded into "genuinely searched, found nothing,"
     wrongly marking ~55 real prospects qualified_no_email/unreachable with
     no search ever having run). After 3 consecutive API errors, the run
     aborts entirely rather than grinding through the rest of the queue
     doing nothing but logging identical failures — same root cause,
     stopping fast instead of silently no-op'ing hundreds of rows.
  5. Print a summary: how many found a real email, how many came up empty
     and where they landed (qualified_no_email vs unreachable), how many
     hit an API-level error (left pending, not touched).

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
    from outreach.email_discovery import find_email, is_valid_email, looks_like_guess, EmailDiscoveryAPIError
    from outreach.email_scrape import scrape_website_email
    from outreach.apply_result import _try_finalize
except ImportError:
    from email_discovery import find_email, is_valid_email, looks_like_guess, EmailDiscoveryAPIError
    from email_scrape import scrape_website_email
    from apply_result import _try_finalize

MAX_CONSECUTIVE_API_ERRORS = 3

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

    # Tier 1: plain code, no AI call, no cost. Only prospects where this
    # finds nothing reach Tier 2 below.
    email, source = scrape_website_email(prospect.website)
    tier = "tier1" if email else None

    if not email:
        try:
            email, source = find_email(prospect.business_name, prospect.location, prospect.website)
            tier = "tier2" if email else None
        except EmailDiscoveryAPIError as e:
            # No search happened — do NOT touch the Prospect row and do NOT
            # delete the pending row, so this stays queued for a genuine retry
            # once whatever's wrong (credits, rate limit, etc.) is fixed.
            logger.error("API-level failure for prospect %s (%s) — leaving pending for retry: %s",
                         prospect.id, prospect.business_name, e)
            return "discovery_errored"

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
        status = "found_tier1" if tier == "tier1" else "found_tier2"
        logger.info("Prospect %s (%s) -> %s (source: %s, %s)",
                    prospect.id, prospect.business_name, email, source, tier)
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

        counts = {"found_tier1": 0, "found_tier2": 0, "not_found": 0, "stale": 0,
                  "dry_run": 0, "discovery_errored": 0}
        # Statuses that mean a real Tier 2 API call actually ran this
        # iteration — Tier 1 hits and stale rows never touched the API, so
        # pausing after them just wastes wall-clock time for nothing.
        _API_CALL_STATUSES = {"found_tier2", "not_found", "discovery_errored", "error"}
        consecutive_api_errors = 0
        aborted_early = False
        for i, pending in enumerate(pending_rows):
            try:
                status = _resolve_one(db, pending, dry_run)
                counts[status] = counts.get(status, 0) + 1
            except Exception:
                logger.exception("Error resolving PendingEmailDiscovery %s (prospect %s)",
                                  pending.id, pending.prospect_id)
                counts["error"] = counts.get("error", 0) + 1
                status = "error"

            if status == "discovery_errored":
                consecutive_api_errors += 1
                if consecutive_api_errors >= MAX_CONSECUTIVE_API_ERRORS:
                    logger.error(
                        "%d consecutive API-level failures — aborting the run rather than grinding "
                        "through the remaining %d pending rows doing nothing but logging identical "
                        "failures. Nothing further was touched; all remaining rows stay pending for "
                        "the next run.", consecutive_api_errors, len(pending_rows) - i - 1,
                    )
                    aborted_early = True
                    break
            else:
                consecutive_api_errors = 0

            # Small pause between real API calls only — not rate-limit-critical
            # at this volume, just avoids hammering the API back-to-back
            # across a batch that could be in the hundreds. Tier 1 hits made
            # zero API calls, so there's nothing to pace.
            if not dry_run and status in _API_CALL_STATUSES and i < len(pending_rows) - 1:
                time.sleep(sleep_between)
    finally:
        db.close()

    total = sum(counts.values())
    tier2_attempts = counts.get("found_tier2", 0) + counts.get("not_found", 0)
    tier1_of_resolved = counts.get("found_tier1", 0) / total if total else 0.0

    print("")
    print("=" * 56)
    print("Email discovery job summary")
    print("-" * 56)
    print(f"  Rows processed:                {total}")
    print(f"  Found at Tier 1 (no AI call):  {counts.get('found_tier1', 0)}"
          f"  ({tier1_of_resolved * 100:.0f}% of this run resolved with zero API cost)")
    print(f"  Found at Tier 2 (AI search):   {counts.get('found_tier2', 0)}")
    print(f"  No genuine email found:        {counts.get('not_found', 0)}")
    print(f"  Tier 2 API calls attempted:    {tier2_attempts}")
    print(f"  API-level errors (left pending, not touched): {counts.get('discovery_errored', 0)}")
    print(f"  Stale rows (no prospect left): {counts.get('stale', 0)}")
    if counts.get("error"):
        print(f"  Errored (see log above):       {counts.get('error', 0)}")
    if aborted_early:
        print(f"  ABORTED EARLY after {MAX_CONSECUTIVE_API_ERRORS} consecutive API errors — remaining "
              f"rows left untouched for next run.")
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
