"""
Groundwork outreach — one-off "we dropped the setup fee" win-back send.

Added 2026-07-23, by request: prospects who clicked their preview link
(a real generated site exists) but never paid, back when going live still
meant a £99 setup fee on top of monthly hosting. That fee is gone now
(app.py's checkout no longer charges it) — this tells that specific
audience, once, since they have the strongest reason of anyone to revisit
a decision they already made under the old, worse offer.

NOT part of the normal A-D drip and NOT a rotating EmailVariant (see that
model's docstring in models.py) — a single genuine announcement of a real
price change, sent through outreach.templates.PRICE_DROP_ANNOUNCEMENT_EMAIL
directly. Idempotent: logs its own OutreachTouch.stage="price_drop", and
skips anyone who already has one, so re-running this script is always safe.

Usage:
    python -m outreach.send_price_drop_announcement            # send for real
    python -m outreach.send_price_drop_announcement --dry-run  # list recipients only
"""
import os
import sys
import argparse
import logging
from datetime import datetime

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_THIS_DIR)
for _p in (_PROJECT_ROOT, _THIS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from models import SessionLocal, Prospect, OutreachTouch, init_db
from emails import send_outreach_email
from outreach.templates import render_email
from outreach.email_verify import has_bounced_before, has_delivery_confirmed
from outreach.email_discovery import is_valid_email

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("outreach.send_price_drop_announcement")

BASE_URL = os.environ.get("GROUNDWORK_PUBLIC_URL", "https://groundworkbuild.com")
STAGE = "price_drop"


def _eligible_prospects(db):
    """Clicked (a real site exists), never paid, has a real email, hasn't
    unsubscribed, and hasn't already received this specific announcement.
    Deliberately NOT limited to any funnel_stage — this targets everyone
    who clicked under the old pricing regardless of where they currently
    sit in the normal drip."""
    already_sent_ids = {
        row[0] for row in db.query(OutreachTouch.prospect_id).filter(OutreachTouch.stage == STAGE).all()
    }
    q = db.query(Prospect).filter(
        Prospect.clicked_at.isnot(None),
        Prospect.paid_at.is_(None),
        Prospect.email.isnot(None),
        Prospect.email != "",
        Prospect.email_unsubscribed.isnot(True),
    )
    return [p for p in q.all() if p.id not in already_sent_ids]


def run(dry_run=False):
    init_db()
    db = SessionLocal()
    sent = 0
    skipped = 0
    try:
        prospects = _eligible_prospects(db)
        logger.info("%d prospect(s) eligible for the price-drop announcement", len(prospects))

        for p in prospects:
            if not is_valid_email(p.email):
                logger.info("Skipping prospect %s (%s): not a valid-looking email", p.id, p.email)
                skipped += 1
                continue
            if has_bounced_before(db, p.email):
                logger.info("Skipping prospect %s (%s): bounced before", p.id, p.email)
                skipped += 1
                continue
            if not has_delivery_confirmed(db, p.email):
                logger.info("Skipping prospect %s (%s): no prior confirmed delivery", p.id, p.email)
                skipped += 1
                continue

            preview_link = f"{BASE_URL}/claim/{p.token}"
            unsubscribe_link = f"{BASE_URL}/unsubscribe/{p.token}"

            if dry_run:
                logger.info("[dry-run] would send to prospect %s: %s <%s>", p.id, p.business_name, p.email)
                continue

            msg = render_email(
                STAGE,
                business_name=p.business_name,
                preview_link=preview_link,
                unsubscribe_link=unsubscribe_link,
            )
            email_id = send_outreach_email(p.email, msg["subject"], msg["body"], unsubscribe_link)
            if email_id:
                db.add(OutreachTouch(prospect_id=p.id, stage=STAGE, channel="email", sent_at=datetime.utcnow()))
                db.commit()
                sent += 1
                logger.info("Sent price-drop announcement to prospect %s (%s), resend id=%s", p.id, p.email, email_id)
            else:
                logger.warning("Send failed for prospect %s (%s)", p.id, p.email)
                skipped += 1
    finally:
        db.close()

    print(f"\nPrice-drop announcement: {sent} sent, {skipped} skipped.\n")
    return {"sent": sent, "skipped": skipped}


def main():
    parser = argparse.ArgumentParser(description="Send the one-off setup-fee-removed win-back email")
    parser.add_argument("--dry-run", action="store_true", help="list recipients without sending")
    args = parser.parse_args()
    run(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
