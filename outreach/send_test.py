"""
Groundwork outreach — manual single test send.

Separate from the automated daily batch (outreach/send_job.py): sends the
real initial template, through the real pipeline (ensure_link_identity,
render_email/render_sms, send_outreach_email/send_outreach_sms, the same
funnel-state updates fill_initial_sends makes), to exactly one prospect —
either an existing one by ID, or an ad-hoc test record built from raw
name/email/phone on the spot. NOT ramp-limited (a single manual test send
shouldn't consume or be blocked by the day's approved volume) and NOT
gated by anything else in send_job.py — this is meant to work even before
the automated batch is trusted against real prospects.

Usage:
    python -m outreach.send_test --prospect-id 42
    python -m outreach.send_test --business-name "Test Roofing Ltd" --email you@example.com
    python -m outreach.send_test --business-name "Test Roofing Ltd" --phone "+447900000000"

Real send — costs a real Resend/Twilio send just like production. Prints
the resulting /claim/<token> link so you can immediately click through it
yourself to test the full claim -> generation -> preview loop end to end.
"""
import sys
import argparse
from datetime import datetime

from models import SessionLocal, Prospect, init_db
from outreach.send_job import send_initial_touch, BASE_URL


def _get_or_create_prospect(db, args):
    if args.prospect_id is not None:
        p = db.get(Prospect, args.prospect_id)
        if not p:
            print(f"No prospect with id {args.prospect_id}", file=sys.stderr)
            sys.exit(1)
        return p

    if not args.email and not args.phone:
        print("Provide either --prospect-id, or --email and/or --phone for an ad-hoc test record.", file=sys.stderr)
        sys.exit(1)

    p = Prospect(
        business_name=args.business_name or "Test Business",
        email=(args.email or None),
        email_found=bool(args.email),
        phone=(args.phone or None),
        score=0,
        funnel_stage="test",
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    print(f"Created ad-hoc test prospect id={p.id}")
    return p


def main():
    parser = argparse.ArgumentParser(description="Send a single real test touch through the outreach pipeline")
    parser.add_argument("--prospect-id", type=int, help="send to an existing Prospect by id")
    parser.add_argument("--business-name", help="business_name for a new ad-hoc test prospect")
    parser.add_argument("--email", help="email for a new ad-hoc test prospect (email-track)")
    parser.add_argument("--phone", help="phone (E.164, e.g. +447900000000) for a new ad-hoc test prospect (SMS)")
    args = parser.parse_args()

    init_db()
    db = SessionLocal()
    try:
        p = _get_or_create_prospect(db, args)

        if p.email_unsubscribed and p.sms_unsubscribed:
            print(f"Prospect {p.id} is opted out of both channels — nothing to send.", file=sys.stderr)
            sys.exit(1)
        if not p.email and not p.phone:
            print(f"Prospect {p.id} has neither email nor phone on file — nothing to send.", file=sys.stderr)
            sys.exit(1)

        now = datetime.utcnow()
        sent = send_initial_touch(db, p, now, remaining_ramp=None)
        if not sent:
            print(f"Prospect {p.id}: no eligible channel to send on (opted out, or missing contact info).", file=sys.stderr)
            sys.exit(1)

        db.refresh(p)
        print(f"\nTest send complete for prospect {p.id} ({p.business_name}).")
        if p.email:
            print(f"  Email sent to: {p.email}")
        if p.phone:
            print(f"  SMS sent to:   {p.phone}")
        print(f"  Claim link:    {BASE_URL}/claim/{p.token}")
        if p.short_code:
            print(f"  Short link:    {BASE_URL}/s/{p.short_code}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
