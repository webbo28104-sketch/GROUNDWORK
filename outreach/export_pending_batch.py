"""
Export a batch of pending_email_discoveries rows to a JSON file in the
repo, for the nightly discovery routine to read via a plain `Read` call
against its own git checkout — no network request to groundworkbuild.com
needed for input.

Added 2026-07-18, replacing a direct WebFetch call to
GET /api/admin/outreach/g/pending after that endpoint proved unreachable
from the discovery routine's execution environment across many attempts
(a real, still only partially-understood Cloudflare/connectivity issue —
see docs/outreach-pipeline-spec.md Section 4a's later addendum). The
routine's own git-checkout `Read` calls, by contrast, worked reliably all
night (it successfully read CLAUDE.md and this spec doc mid-run) — so this
sidesteps the broken path entirely rather than trying to fix it further.

Runnable standalone:
    python outreach/export_pending_batch.py
    python outreach/export_pending_batch.py --limit 30

Writes outreach/discovery_batches/pending_batch.json. Does NOT commit/push
itself — call this from wherever has git push credentials (a manual run,
or a future Railway Cron service once one is set up with a deploy key;
none exists yet, so for now this is invoked ad hoc before each routine
run and the caller commits the result).
"""
import os
import sys
import json
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

OUTPUT_PATH = os.path.join(_PROJECT_ROOT, "outreach", "discovery_batches", "pending_batch.json")


def export_batch(limit=20):
    init_db()
    db = SessionLocal()
    try:
        rows = (
            db.query(PendingEmailDiscovery, Prospect)
            .join(Prospect, PendingEmailDiscovery.prospect_id == Prospect.id)
            .order_by(PendingEmailDiscovery.created_at.asc())
            .limit(limit)
            .all()
        )
        batch = [
            {
                "prospect_id": p.id,
                "business_name": p.business_name,
                "trade": p.trade,
                "location": p.location,
                "website": p.website,
                "website_status": p.website_status,
                "phone": p.phone,
                "queued_at": ed.created_at.isoformat() if ed.created_at else None,
            }
            for ed, p in rows
        ]
    finally:
        db.close()

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        json.dump(batch, f, indent=2)

    print(f"Exported {len(batch)} pending prospect(s) to {OUTPUT_PATH}")
    return batch


def main():
    parser = argparse.ArgumentParser(description="Export pending email-discovery rows to a repo JSON file")
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()
    export_batch(limit=args.limit)


if __name__ == "__main__":
    main()
