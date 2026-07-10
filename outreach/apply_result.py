"""
Write an AI judgment result back to a prospect and advance it through the pipeline.

Once BOTH the vision check and email discovery are resolved for a prospect,
this script automatically runs scoring and moves the prospect to
awaiting_approval (if an email was found) or qualified_no_email.

Commands
────────
List everything still pending (start here):
    python outreach/apply_result.py pending

Apply a website vision check verdict:
    python outreach/apply_result.py vision <prospect_id> <verdict>

    verdict must be one of:
        no_website           (no website found — score as no_website)
        has_website_dated    (site exists but looks old/poor)
        has_website_modern   (site exists and looks professional)

Apply an email discovery result:
    python outreach/apply_result.py email <prospect_id> <email_or_null>

    email_or_null: a real email address, or the literal word "null"
    Optional: --source web_search|facebook  (default: web_search)

    Hard rule: only submit emails you actually found on a real page.
    The script rejects addresses that look like pattern-match guesses.
    Use --force to bypass the guess check if you're certain it's real.

Examples:
    python outreach/apply_result.py pending
    python outreach/apply_result.py vision 42 has_website_dated
    python outreach/apply_result.py vision 43 no_website
    python outreach/apply_result.py email 42 hello@johnsmith.co.uk
    python outreach/apply_result.py email 43 null
    python outreach/apply_result.py email 44 info@biz.com --source facebook
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
    SessionLocal, Prospect, PendingVisionCheck, PendingEmailDiscovery, init_db,
)

try:
    from outreach.scorer import score_prospect
    from outreach.email_discovery import is_valid_email, looks_like_guess
except ImportError:
    from scorer import score_prospect
    from email_discovery import is_valid_email, looks_like_guess

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("outreach.apply_result")

VALID_VERDICTS = {"no_website", "has_website_dated", "has_website_modern"}


# ─── Finalization ────────────────────────────────────────────────────────────

def _try_finalize(db, prospect):
    """If no pending queue rows remain for this prospect, score it and advance
    its funnel_stage to awaiting_approval or qualified_no_email."""
    vision_still_pending = db.query(PendingVisionCheck).filter(
        PendingVisionCheck.prospect_id == prospect.id
    ).first()
    email_still_pending = db.query(PendingEmailDiscovery).filter(
        PendingEmailDiscovery.prospect_id == prospect.id
    ).first()

    if vision_still_pending or email_still_pending:
        remaining = []
        if vision_still_pending:
            remaining.append("vision")
        if email_still_pending:
            remaining.append("email")
        logger.info(
            "Prospect %s (%s): still waiting for %s",
            prospect.id, prospect.business_name, " + ".join(remaining),
        )
        return

    # Both results in — score and finalize.
    prospect.score = score_prospect(prospect)

    if prospect.email_found:
        prospect.funnel_stage = "awaiting_approval"
        prospect.approval_status = "pending"
        status_msg = "→ awaiting_approval (email found)"
    else:
        prospect.funnel_stage = "qualified_no_email"
        status_msg = "→ qualified_no_email (no email found)"

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
        vision_rows = (
            db.query(PendingVisionCheck, Prospect)
            .join(Prospect, PendingVisionCheck.prospect_id == Prospect.id)
            .order_by(PendingVisionCheck.id)
            .all()
        )
        email_rows = (
            db.query(PendingEmailDiscovery, Prospect)
            .join(Prospect, PendingEmailDiscovery.prospect_id == Prospect.id)
            .order_by(PendingEmailDiscovery.id)
            .all()
        )
    finally:
        db.close()

    print(f"\n{'='*70}")
    print(f"Pending vision checks ({len(vision_rows)})")
    print(f"{'-'*70}")
    if vision_rows:
        print(f"{'ID':<6} {'Business':<30} {'Location':<25} Screenshot")
        print(f"{'-'*6} {'-'*30} {'-'*25} {'-'*20}")
        for vc, p in vision_rows:
            shot = vc.screenshot_path or "(no screenshot — load failed)"
            print(f"{p.id:<6} {(p.business_name or '')[:30]:<30} {(p.location or '')[:25]:<25} {shot}")
    else:
        print("  (none)")

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
    print("  python outreach/apply_result.py vision <id> <verdict>")
    print("  python outreach/apply_result.py email <id> <email|null>\n")


def cmd_vision(prospect_id, verdict):
    if verdict not in VALID_VERDICTS:
        print(f"ERROR: verdict must be one of: {', '.join(sorted(VALID_VERDICTS))}", file=sys.stderr)
        sys.exit(1)

    db = SessionLocal()
    try:
        p = db.get(Prospect, prospect_id)
        if not p:
            print(f"ERROR: Prospect {prospect_id} not found", file=sys.stderr)
            sys.exit(1)

        p.website_status = verdict

        deleted = db.query(PendingVisionCheck).filter(
            PendingVisionCheck.prospect_id == prospect_id
        ).delete()
        db.commit()

        if not deleted:
            logger.warning("No PendingVisionCheck row found for prospect %s (already resolved?)", prospect_id)

        print(f"  Vision: prospect {prospect_id} ({p.business_name}) → {verdict}")
        _try_finalize(db, p)
    finally:
        db.close()


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


# ─── CLI ─────────────────────────────────────────────────────────────────────

def main():
    init_db()

    parser = argparse.ArgumentParser(
        description="Apply AI judgment results to outreach prospects",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python outreach/apply_result.py pending\n"
            "  python outreach/apply_result.py vision 42 has_website_dated\n"
            "  python outreach/apply_result.py email 42 hello@johnsmith.co.uk\n"
            "  python outreach/apply_result.py email 43 null\n"
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("pending", help="List all prospects awaiting AI judgment")

    v = sub.add_parser("vision", help="Apply a website vision check result")
    v.add_argument("prospect_id", type=int, help="Prospect ID from the pending list")
    v.add_argument(
        "verdict",
        choices=sorted(VALID_VERDICTS),
        help="Your judgment of the website quality",
    )

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

    args = parser.parse_args()

    if args.command == "pending":
        cmd_pending()
    elif args.command == "vision":
        cmd_vision(args.prospect_id, args.verdict)
    elif args.command == "email":
        cmd_email(args.prospect_id, args.email_or_null, source=args.source, force=args.force)


if __name__ == "__main__":
    main()
