"""
Email discovery job — Track A, unattended, Tier 1 only (Section 4).

Runnable as a module or script from the project root:
    python outreach/email_discovery_job.py
    python -m outreach.email_discovery_job --limit 50 --dry-run

REMOVED 2026-07-18: this job used to fall back to a Tier 2 (Anthropic API,
web_search tool) pass for anything Tier 1 couldn't find — see
outreach/email_discovery.py's module docstring for why that was deleted
outright (cost, and it was the thing spending ~£15-20/night). This job is
now Tier 1 only: plain-code scraping of a prospect's own website
(outreach/email_scrape.py), zero API calls, zero cost, genuinely safe to
run nightly forever.

What it does:
  1. Init the DB.
  2. Load pending PendingEmailDiscovery rows (oldest first, optionally capped
     by --limit).
  3. For each: run outreach.email_scrape.scrape_website_email() — homepage,
     then a same-site contact/about page, checking for a mailto: link or
     plain-text address. Never guesses or pattern-matches.
  4. If found: write the result to the Prospect row (same funnel_stage
     transitions apply_result.py's `email` command already applies,
     reused via _try_finalize) and delete the PendingEmailDiscovery row.
     If NOT found: leave the PendingEmailDiscovery row in place, untouched
     — Tier 1 having nothing to say isn't the same as "no email exists
     anywhere," and the row needs to stay in the pending queue for the
     free-but-not-automatic Tier 2 replacement (a scheduled Claude Code
     routine using WebSearch — see docs/outreach-pipeline-spec.md Section
     4a) to pick up later. Deleting it here would silently drop every
     prospect Tier 1 can't handle into permanent qualified_no_email/
     unreachable with no further attempt ever made.
  5. Print a summary: how many found, how many still pending for the next
     (Claude-routine) pass.

Still needs to run on a real recurring schedule (a Railway Cron service,
same as outreach/domain_billing.py and outreach/pipeline.py) — there is no
in-process scheduler in this codebase. The email-discovery-cron service
that already runs this nightly at 02:00 UTC needed no schedule change,
only this code change (it had its ANTHROPIC_API_KEY removed as a second,
belt-and-braces safeguard — this job doesn't need that variable at all now).

Environment: DATABASE_URL. Nothing else — no API key needed anymore.
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

from models import (  # noqa: E402
    SessionLocal, Prospect, PendingEmailDiscovery, init_db,
)

try:
    from outreach.email_discovery import is_valid_email, looks_like_guess, clean_email
    from outreach.email_scrape import scrape_website_email
    from outreach.apply_result import _try_finalize
    from outreach.email_verify import has_deliverable_domain
except ImportError:
    from email_discovery import is_valid_email, looks_like_guess, clean_email
    from email_scrape import scrape_website_email
    from apply_result import _try_finalize
    from email_verify import has_deliverable_domain

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("outreach.email_discovery_job")


def _resolve_one(db, pending, dry_run):
    """Try Tier 1 for one pending prospect. Returns a status string for the
    run summary. Only ever deletes the PendingEmailDiscovery row on a
    genuine find — see module docstring for why a miss leaves it in place."""
    prospect = db.get(Prospect, pending.prospect_id)
    if not prospect:
        logger.warning("PendingEmailDiscovery %s has no matching Prospect (id=%s) — deleting stale row",
                        pending.id, pending.prospect_id)
        if not dry_run:
            db.delete(pending)
            db.commit()
        return "stale"

    if dry_run:
        logger.info("[dry-run] would check own-website scrape for: %s (%s)", prospect.business_name, prospect.website)
        return "dry_run"

    email, source = scrape_website_email(prospect.website)

    if email:
        email = clean_email(email)
        # Belt-and-braces: same validation apply_result.py applies to a
        # human/CLI-submitted result, run here too rather than trusting a
        # single layer.
        if not is_valid_email(email):
            logger.warning("Discarding invalid email '%s' for prospect %s", email, prospect.id)
            email = None
        elif looks_like_guess(email, prospect.business_name, prospect.website):
            logger.warning("Discarding suspected guessed email '%s' for prospect %s", email, prospect.id)
            email = None
        elif not has_deliverable_domain(email):
            logger.warning("Discarding undeliverable domain in '%s' for prospect %s (no MX/A record)",
                            email, prospect.id)
            email = None

    if not email:
        logger.info("Prospect %s (%s) -> Tier 1 found nothing, left pending for the Claude-routine pass",
                     prospect.id, prospect.business_name)
        return "still_pending"

    prospect.email = email
    prospect.email_source = source
    prospect.email_found = True
    logger.info("Prospect %s (%s) -> %s (source: %s)", prospect.id, prospect.business_name, email, source)

    db.delete(pending)
    db.commit()

    try:
        _try_finalize(db, prospect)
    except Exception:
        logger.exception("finalize() failed for prospect %s after email result was already saved — "
                          "email/email_found is correct in the DB, but scoring/funnel_stage may be stale",
                          prospect.id)
    return "found_tier1"


def run_email_discovery(limit=None, dry_run=False, sleep_between=None):
    """Run one full pass over the pending email-discovery queue, Tier 1 only.
    sleep_between is accepted for CLI/call-site backwards compatibility but
    unused — there's no API to rate-limit against anymore."""
    logger.info("Starting email discovery job (Tier 1 only, limit=%s, dry_run=%s)", limit, dry_run)
    init_db()

    db = SessionLocal()
    try:
        q = db.query(PendingEmailDiscovery).order_by(PendingEmailDiscovery.created_at.asc())
        if limit:
            q = q.limit(limit)
        pending_rows = q.all()
        logger.info("Loaded %d pending email-discovery rows", len(pending_rows))

        counts = {"found_tier1": 0, "still_pending": 0, "stale": 0, "dry_run": 0}
        for pending in pending_rows:
            try:
                status = _resolve_one(db, pending, dry_run)
                counts[status] = counts.get(status, 0) + 1
            except Exception:
                logger.exception("Error resolving PendingEmailDiscovery %s (prospect %s)",
                                  pending.id, pending.prospect_id)
                counts["error"] = counts.get("error", 0) + 1
    finally:
        db.close()

    total = sum(counts.values())

    print("")
    print("=" * 56)
    print("Email discovery job summary (Tier 1 only — zero API cost)")
    print("-" * 56)
    print(f"  Rows processed:                {total}")
    print(f"  Found on own website:          {counts.get('found_tier1', 0)}")
    print(f"  Still pending (left for the Claude-routine pass): {counts.get('still_pending', 0)}")
    print(f"  Stale rows (no prospect left): {counts.get('stale', 0)}")
    if counts.get("error"):
        print(f"  Errored (see log above):       {counts.get('error', 0)}")
    print("=" * 56)
    print("")

    return counts


def main():
    parser = argparse.ArgumentParser(description="Groundwork outreach email discovery job (Track A, Tier 1 only)")
    parser.add_argument("--limit", type=int, default=None,
                        help="max pending rows to process this run (default: all)")
    parser.add_argument("--dry-run", action="store_true",
                        help="log what would be checked without writing data")
    args = parser.parse_args()
    run_email_discovery(limit=args.limit, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
