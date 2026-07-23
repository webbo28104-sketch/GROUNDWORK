"""
One-off backfill: regenerate any lead/prospect that clicked its
verification or outreach magic link but never got a Generation, because
app.py and build_prompt.py briefly had import-time syntax/name errors
(see the commit that fixed them: "Fix syntax/name errors that crashed the
whole app on import"). While those errors were live, the Flask process
failed to import at all, so /verify/<token>, /claim/<token>, and
/claim/<token>/email never ran any code — clicks could still be recorded
where the click itself doesn't depend on app code (e.g. Prospect.clicked_at
stamped by an already-running process before a bad deploy took over), but
no Lead/Generation ever got created or completed downstream of that.

Finds:
  1. Leads with status="verified" and no Generation row for that lead
     (skipped if that email already has a Generation under a different
     lead row, per the one-generation-per-email rule the rest of this
     codebase enforces via _has_generation).
  2. Prospects with clicked_at set but either no lead_id, or a lead_id
     whose Lead has no Generation.

For each, this runs the same prompt-build + Claude call + persist path as
_kickoff_generation/_run_and_persist in app.py, synchronously (no
background thread), so the script blocks until every backfilled site is
actually done. The "your website is ready" email is not sent separately
here — it's already sent by _run_and_persist itself on completion (skipped
automatically for admin test leads, same as the live path).

Safe to re-run: anything that already has a Generation by the time this
runs again is skipped, so an interrupted run can just be re-run.

Needs the same environment as the live app to do anything real —
DATABASE_URL (production Postgres), ANTHROPIC_API_KEY, and
RESEND_API_KEY/RESEND_FROM_EMAIL to actually send the ready email (emails
are logged instead of sent if RESEND_API_KEY is unset, same as everywhere
else). Run this from an environment with those set, e.g. `railway run`.

Usage:
    python backfill_stuck_generations.py --dry-run   # list affected rows only, no API calls
    python backfill_stuck_generations.py             # actually regenerate
"""
import os
import sys
import base64
import uuid

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except Exception:
    pass

from app import (
    SessionLocal, Lead, Generation, Prospect,
    _has_generation, _build_media_placeholders, _run_and_persist,
    _prospect_to_form_data, _try_extract_prospect_assets, UPLOAD_DIR,
)
from build_prompt import build_prompt


def _generate_for_lead(lead_id):
    """Mirrors _kickoff_generation, but calls _run_and_persist directly
    instead of handing it to a background thread, so this script can block
    until the generation (and its "site ready" email) is actually done."""
    db = SessionLocal()
    try:
        lead = db.get(Lead, lead_id)
        job_dir = os.path.join(UPLOAD_DIR, lead.public_id)
        build_data = dict(lead.form_data or {})
        media_overrides, image_placeholders = _build_media_placeholders(job_dir, lead.logo_path)
        build_data.update(media_overrides)
        prompt = build_prompt(build_data)

        logo_b64 = None
        if lead.logo_path:
            logo_file_path = os.path.join(job_dir, lead.logo_path)
            if os.path.exists(logo_file_path):
                with open(logo_file_path, "rb") as f:
                    logo_b64 = base64.standard_b64encode(f.read()).decode()

        business_name = build_data.get("business_name", "")
        public_id = lead.public_id
        email = lead.email
        logo_mime = lead.logo_mime
    finally:
        db.close()

    print(f"  generating {public_id} ({business_name or email}) ...", flush=True)
    _run_and_persist(public_id, lead_id, email, business_name, prompt, logo_b64, logo_mime, image_placeholders)


def find_stuck_leads(db):
    stuck = []
    for lead in db.query(Lead).filter(Lead.status == "verified").all():
        if db.query(Generation).filter(Generation.lead_id == lead.id).first():
            continue
        if lead.email and _has_generation(db, lead.email):
            continue  # already generated under a different lead row for this email
        stuck.append(lead)
    return stuck


def find_stuck_prospects(db):
    stuck = []
    for p in db.query(Prospect).filter(Prospect.clicked_at.isnot(None)).all():
        if p.lead_id and db.query(Generation).filter(Generation.lead_id == p.lead_id).first():
            continue
        stuck.append(p)
    return stuck


def _ensure_lead_for_prospect(prospect_id):
    """Creates the Lead a claimed-but-never-generated prospect should have
    gotten (same shape as _claim_generate_and_redirect), or reuses an
    existing Generation for the prospect's email if one already exists.
    Returns the lead_id to generate for, or None if there's nothing to do."""
    db = SessionLocal()
    try:
        prospect = db.get(Prospect, prospect_id)
        if not prospect.email:
            print(f"  skipping prospect {prospect.id} — no email on file, can't backfill headlessly")
            return None

        if prospect.lead_id:
            return prospect.lead_id

        existing_gen = (
            db.query(Generation).filter(Generation.email == prospect.email)
            .order_by(Generation.created_at.desc()).first()
        )
        if existing_gen:
            prospect.lead_id = existing_gen.lead_id
            db.commit()
            return None  # already has a generation, nothing to kick off

        lead = Lead(
            public_id=uuid.uuid4().hex[:10],
            email=prospect.email,
            ip="",
            status="verified",
            form_data=_prospect_to_form_data(prospect),
        )
        db.add(lead)
        db.flush()
        prospect.lead_id = lead.id
        prospect.funnel_substage = "clicked_generated"

        job_dir = os.path.join(UPLOAD_DIR, lead.public_id)
        _try_extract_prospect_assets(prospect, lead, job_dir)

        db.commit()
        return lead.id
    finally:
        db.close()


def main():
    dry_run = "--dry-run" in sys.argv

    db = SessionLocal()
    try:
        stuck_leads = find_stuck_leads(db)
        stuck_prospects = find_stuck_prospects(db)
    finally:
        db.close()

    print(f"Stuck verified leads with no generation: {len(stuck_leads)}")
    for lead in stuck_leads:
        print(f"  - lead {lead.public_id} / {lead.email}")
    print(f"Stuck clicked prospects with no generation: {len(stuck_prospects)}")
    for p in stuck_prospects:
        print(f"  - prospect {p.id} / {p.business_name} / lead_id={p.lead_id}")

    if dry_run:
        print("Dry run — no generations started.")
        return

    for lead in stuck_leads:
        try:
            _generate_for_lead(lead.id)
        except Exception as exc:
            print(f"  FAILED lead {lead.public_id}: {exc}")

    for p in stuck_prospects:
        try:
            lead_id = _ensure_lead_for_prospect(p.id)
            if lead_id is None:
                continue
            _generate_for_lead(lead_id)
        except Exception as exc:
            print(f"  FAILED prospect {p.id}: {exc}")

    print("Backfill complete.")


if __name__ == "__main__":
    main()
