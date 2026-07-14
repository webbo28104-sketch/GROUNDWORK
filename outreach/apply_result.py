"""
Write an AI judgment result back to a prospect and advance it through the pipeline.

website_status is no longer judged here — outreach/pipeline.py sets it for
free straight off Places' website field (has_website/no_website) at sourcing
time. Once email discovery is resolved for a prospect, this script
automatically runs scoring and moves the prospect to one of:
    awaiting_approval   (an email was found)
    qualified_no_email  (no email, but a phone number exists — SMS-reachable)
    unreachable         (neither an email nor a phone number could be found)

Commands
────────
List everything still pending (start here):
    python outreach/apply_result.py pending

Apply an email discovery result:
    python outreach/apply_result.py email <prospect_id> <email_or_null>

    email_or_null: a real email address, or the literal word "null"
    Optional: --source web_search|facebook|checkatrade|yell|directory  (default: web_search)

    Hard rule: only submit emails you actually found on a real page.
    The script rejects addresses that look like pattern-match guesses.
    Use --force to bypass the guess check if you're certain it's real.

Reset qualified_no_email prospects back into the pending email queue:
    python outreach/apply_result.py requeue-email <prospect_id>
    python outreach/apply_result.py requeue-email --all-no-email

    Clears the previous null result and inserts a fresh PendingEmailDiscovery
    row so the email search can be re-run (e.g. with updated directory-first
    logic). Vision check result and website_status are untouched.

Examples:
    python outreach/apply_result.py pending
    python outreach/apply_result.py email 42 hello@johnsmith.co.uk
    python outreach/apply_result.py email 43 null
    python outreach/apply_result.py email 44 info@biz.com --source checkatrade
    python outreach/apply_result.py requeue-email 43
    python outreach/apply_result.py requeue-email --all-no-email
"""
import sys, os, argparse, logging
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

from models import (
    SessionLocal, Prospect, PendingEmailDiscovery, init_db,
)

try:
    from outreach.scorer import score_prospect
    from outreach.email_discovery import is_valid_email, looks_like_guess
except ImportError:
    from scorer import score_prospect
    from email_discovery import is_valid_email, looks_like_guess

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("outreach.apply_result")


# ─── Finalization ────────────────────────────────────────────────────────────

def _try_finalize(db, prospect):
    """If no pending queue rows remain for this prospect, score it and advance
    its funnel_stage to awaiting_approval, qualified_no_email, or unreachable."""
    email_still_pending = db.query(PendingEmailDiscovery).filter(
        PendingEmailDiscovery.prospect_id == prospect.id
    ).first()

    if email_still_pending:
        logger.info(
            "Prospect %s (%s): still waiting for email",
            prospect.id, prospect.business_name,
        )
        return

    # Result in — score and finalize.
    prospect.score = score_prospect(prospect)

    if prospect.email_found:
        prospect.funnel_stage = "awaiting_approval"
        prospect.approval_status = "pending"
        status_msg = "→ awaiting_approval (email found)"
    elif prospect.phone:
        prospect.funnel_stage = "qualified_no_email"
        prospect.approval_status = "pending"
        status_msg = "→ qualified_no_email (no email, but phone found)"
    else:
        # Neither an email nor a phone number — not silently dropped, just
        # routed to a distinct stage so it can be surfaced (and filtered on)
        # in the Tinder review UI instead of vanishing from view.
        prospect.funnel_stage = "unreachable"
        prospect.approval_status = "unreachable"
        status_msg = "→ unreachable (no email, no phone)"

    prospect.processed_at = datetime.utcnow()
    db.commit()

    print(
        f"  Finalized: prospect {prospect.id} ({prospect.business_name}) "
        f"{status_msg}, score={prospect.score:.0f}"
    )


# ─── Commands ────────────────────────────────────────────────────────────────

def cmd_pending():
    """Print a table of everything Cowork still needs to judge."""
    db = SessionLocal()
    try:
        email_rows = (
            db.query(PendingEmailDiscovery, Prospect)
            .join(Prospect, PendingEmailDiscovery.prospect_id == Prospect.id)
            .order_by(PendingEmailDiscovery.id)
            .all()
        )
    finally:
        db.close()

    print(f"\n{'='*70}")
    print(f"Pending email discoveries ({len(email_rows)})")
    print(f"{'-'*70}")
    if email_rows:
        print(f"{'ID':<6} {'Business':<30} {'Location':<25} Website")
        print(f"{'-'*6} {'-'*30} {'-'*25} {'-'*20}")
        for ed, p in email_rows:
            site = p.website or "(no website)"
            print(f"{p.id:<6} {(p.business_name or '')[:30]:<30} {(p.location or '')[:25]:<25} {site}")
    else:
        print("  (none)")

    print(f"{'='*70}\n")
    print("Apply results with:")
    print("  python outreach/apply_result.py email <id> <email|null>\n")


def cmd_email(prospect_id, email_raw, source="web_search", force=False):
    null_values = {"null", "none", ""}
    email = None if (email_raw or "").strip().lower() in null_values else email_raw.strip()

    db = SessionLocal()
    try:
        p = db.get(Prospect, prospect_id)
        if not p:
            print(f"ERROR: Prospect {prospect_id} not found", file=sys.stderr)
            sys.exit(1)

        if email:
            if not is_valid_email(email):
                print(f"ERROR: '{email}' doesn't look like a valid email address", file=sys.stderr)
                sys.exit(1)

            if not force and looks_like_guess(email, p.business_name, p.website):
                print(
                    f"ERROR: '{email}' looks like a pattern-match guess for '{p.business_name}' "
                    f"(no real website on record). Only submit emails you actually saw on a real "
                    f"page. Use --force to bypass this check if you're certain it was genuinely found.",
                    file=sys.stderr,
                )
                sys.exit(1)

            p.email = email
            p.email_source = source
            p.email_found = True
            print(f"  Email: prospect {prospect_id} ({p.business_name}) → {email} (source: {source})")
        else:
            p.email_found = False
            print(f"  Email: prospect {prospect_id} ({p.business_name}) → null (not found)")

        deleted = db.query(PendingEmailDiscovery).filter(
            PendingEmailDiscovery.prospect_id == prospect_id
        ).delete()
        db.commit()

        if not deleted:
            logger.warning("No PendingEmailDiscovery row found for prospect %s (already resolved?)", prospect_id)

        _try_finalize(db, p)
    finally:
        db.close()


def cmd_requeue_email(prospect_id=None, all_no_email=False):
    """Reset qualified_no_email prospects back into the pending email queue.

    Clears the old null result and inserts a fresh PendingEmailDiscovery row
    so the search can be re-run. Vision check result and website_status are
    left untouched — only the email side is reset.
    """
    db = SessionLocal()
    try:
        if all_no_email:
            prospects = (
                db.query(Prospect)
                .filter(Prospect.funnel_stage == "qualified_no_email")
                .order_by(Prospect.id)
                .all()
            )
            if not prospects:
                print("No qualified_no_email prospects found — nothing to requeue.")
                return
        else:
            p = db.get(Prospect, prospect_id)
            if not p:
                print(f"ERROR: Prospect {prospect_id} not found", file=sys.stderr)
                sys.exit(1)
            if p.funnel_stage != "qualified_no_email":
                print(
                    f"ERROR: Prospect {prospect_id} ({p.business_name}) is at stage "
                    f"'{p.funnel_stage}', not 'qualified_no_email' — nothing to requeue.",
                    file=sys.stderr,
                )
                sys.exit(1)
            prospects = [p]

        requeued = 0
        skipped = 0
        for p in prospects:
            # Skip if a PendingEmailDiscovery row already exists (already requeued).
            existing = db.query(PendingEmailDiscovery).filter(
                PendingEmailDiscovery.prospect_id == p.id
            ).first()
            if existing:
                print(f"  Skip: prospect {p.id} ({p.business_name}) — already in pending queue")
                skipped += 1
                continue

            # Clear the previous null result.
            p.email = None
            p.email_source = None
            p.email_found = False
            p.funnel_stage = "queued"
            p.processed_at = None

            db.add(PendingEmailDiscovery(
                prospect_id=p.id,
                business_name=p.business_name,
                location=p.location or "",
                website=p.website,
            ))
            requeued += 1
            print(f"  Requeued: prospect {p.id} ({p.business_name}) — website_status={p.website_status}")

        db.commit()

        print(f"\n{requeued} requeued, {skipped} skipped (already pending).")
        print("Run 'python outreach/apply_result.py pending' to see the updated queue.")
    finally:
        db.close()


# ─── CLI ─────────────────────────────────────────────────────────────────────

def main():
    init_db()

    parser = argparse.ArgumentParser(
        description="Apply AI judgment results to outreach prospects",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python outreach/apply_result.py pending\n"
            "  python outreach/apply_result.py email 42 hello@johnsmith.co.uk\n"
            "  python outreach/apply_result.py email 43 null\n"
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("pending", help="List all prospects awaiting AI judgment")

    e = sub.add_parser("email", help="Apply an email discovery result")
    e.add_argument("prospect_id", type=int, help="Prospect ID from the pending list")
    e.add_argument(
        "email_or_null",
        help="Found email address, or the literal word 'null' if nothing was found",
    )
    e.add_argument(
        "--source",
        default="web_search",
        help="Where the email was found (default: web_search)",
    )
    e.add_argument(
        "--force",
        action="store_true",
        help="Bypass the guess-detection check (use only when you're certain the email is real)",
    )

    rq = sub.add_parser(
        "requeue-email",
        help="Reset qualified_no_email prospects back into the pending email queue",
    )
    rq_target = rq.add_mutually_exclusive_group(required=True)
    rq_target.add_argument(
        "prospect_id",
        type=int,
        nargs="?",
        help="ID of a single qualified_no_email prospect to requeue",
    )
    rq_target.add_argument(
        "--all-no-email",
        action="store_true",
        help="Requeue every current qualified_no_email prospect at once",
    )

    args = parser.parse_args()

    if args.command == "pending":
        cmd_pending()
    elif args.command == "email":
        cmd_email(args.prospect_id, args.email_or_null, source=args.source, force=args.force)
    elif args.command == "requeue-email":
        cmd_requeue_email(
            prospect_id=args.prospect_id,
            all_no_email=args.all_no_email,
        )


if __name__ == "__main__":
    main()
