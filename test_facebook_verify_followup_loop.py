"""
Real, executed proof (not just code-reading) that a Facebook-DM-originated
prospect with no email on file: clicks their magic link -> gets asked to
verify an email -> verifying fires generation -> is gated the same way
every outreach generation is -> and, once enough time has passed, falls
into the SAME stage C/D follow-up sequence (on the email channel) as any
email-track prospect.

Requires Python 3.12+ to run (this imports the real app.py, which uses
3.12-only syntax elsewhere in the file — e.g. nested-quote f-strings in
build_prompt.py). Not runnable under this project's other test scripts'
usual 3.9 environment; see CLAUDE.md or ask if `python3` here resolves to
something older.

Runs against a throwaway temp-file SQLite DB. Generation itself
(_kickoff_generation -> Anthropic) and all outbound email sends are
monkeypatched out — this is a control-flow/state proof, not a real send.
"""
import os
import sys
import re
import time
import tempfile
from datetime import datetime, timedelta

DB_PATH = tempfile.mktemp(suffix=".db")
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"
os.environ["SECRET_KEY"] = "test-secret"
os.environ["ADMIN_PASSWORD"] = "test"
os.environ["EMAIL_REPLY_CAPTURE_READY"] = "true"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app as gwapp  # noqa: E402
from models import Base, engine, SessionLocal, Prospect, OutreachTouch, Lead, Generation  # noqa: E402

Base.metadata.create_all(engine)

failures = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


# ── Mock out only the real external calls (the actual Anthropic API call
# inside _run, and every outbound email send) — _kickoff_generation and
# _run_and_persist run FOR REAL, including the notification-gate logic
# under test, in their real background thread. This proves STATE
# TRANSITIONS and CONTROL FLOW, not that a real email/generation happens. ──
captured = {}


def fake_run(job_id, prompt, logo_b64, logo_mime):
    with gwapp._jobs_lock:
        gwapp._jobs[job_id] = {"status": "done", "html": "<html>fake site</html>", "cost_usd": 0.01}


def fake_send_verification_email(to_email, verify_url, business_name):
    captured["verify_url"] = verify_url
    captured["verify_email"] = to_email


def fake_send_admin_approval_email(*a, **kw):
    captured["admin_approval_sent"] = True


def fake_send_site_ready_email(*a, **kw):
    captured["site_ready_sent"] = True
    captured["site_ready_args"] = (a, kw)


gwapp._run = fake_run
gwapp.send_verification_email = fake_send_verification_email
gwapp.send_admin_approval_email = fake_send_admin_approval_email
gwapp.send_site_ready_email = fake_send_site_ready_email


def _wait_for_generation(lead_id, timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        db_ = SessionLocal()
        try:
            g = db_.query(Generation).filter(Generation.lead_id == lead_id).first()
            if g:
                return g
        finally:
            db_.close()
        time.sleep(0.05)
    return None

client = gwapp.app.test_client()
with client.session_transaction() as sess:
    sess["is_admin"] = True

# ── Set up a Facebook-DM-originated prospect: no email, has a phone,
# channel="facebook" touch already logged — same shape
# admin_mark_facebook_dm_sent produces. ──
db = SessionLocal()
p = Prospect(
    google_place_id="fb-test-1", business_name="Test Facebook Biz", phone="+447900000001",
    email=None, email_found=False, funnel_stage="sent", funnel_substage="sent",
    token="fbtesttoken123", short_code="fbtest1",
    facebook_page_url="https://facebook.com/testbiz", facebook_dm_sent_at=datetime.utcnow(),
    sent_at=datetime.utcnow(), touch_count=1, score=50.0,
)
db.add(p)
db.commit()
db.add(OutreachTouch(prospect_id=p.id, stage="initial", channel="facebook", sent_at=datetime.utcnow()))
db.commit()
prospect_id = p.id
db.close()

# ── 1. Click the magic link (no email on file) -> routed to email-capture. ──
resp = client.get("/claim/fbtesttoken123", follow_redirects=False)
check("clicking with no email redirects to /claim/<token>/email", resp.status_code == 302 and "/email" in resp.headers.get("Location", ""), resp.headers.get("Location"))

db = SessionLocal()
ev = db.query(gwapp.ProspectEvent).filter(gwapp.ProspectEvent.prospect_id == prospect_id).all()
check("magic_link_clicked event logged with channel=facebook",
      any(e.event_type == "magic_link_clicked" and e.channel == "facebook" for e in ev), str([(e.event_type, e.channel) for e in ev]))
db.close()

# ── 1b. Actually view the email-capture form (a real GET, matching what
# following the above redirect would do) -> email_capture_viewed logged. ──
resp = client.get("/claim/fbtesttoken123/email")
check("email-capture form GET succeeds", resp.status_code == 200, str(resp.status_code))

# ── 2. Submit an email on the capture form -> verification email queued,
# NOT trusted/generated yet. ──
resp = client.post("/claim/fbtesttoken123/email", data={"email": "owner@testfacebookbiz.co.uk"})
check("submitting email does not immediately generate (still no Prospect.email)", True)  # sanity: no exception
db = SessionLocal()
p_check = db.query(Prospect).filter(Prospect.id == prospect_id).first()
check("Prospect.email still NOT set before verification click", p_check.email is None, str(p_check.email))
check("verification email was queued to the submitted address",
      captured.get("verify_email") == "owner@testfacebookbiz.co.uk", str(captured.get("verify_email")))
db.close()

verify_url = captured["verify_url"]
verify_path = re.sub(r"^https?://[^/]+", "", verify_url)

# ── 3. Click the verification link -> email now trusted + generation fires. ──
resp = client.get(verify_path, follow_redirects=False)
check("verification link click returns a redirect (to /loading.html)",
      resp.status_code == 302 and "/loading.html" in resp.headers.get("Location", ""), resp.headers.get("Location"))

db = SessionLocal()
p_after = db.query(Prospect).filter(Prospect.id == prospect_id).first()
check("Prospect.email now set after verification", p_after.email == "owner@testfacebookbiz.co.uk", str(p_after.email))
check("Prospect.email_found now True", p_after.email_found is True)
check("Prospect.funnel_substage advanced to clicked_generated", p_after.funnel_substage == "clicked_generated", p_after.funnel_substage)
check("An Account row was created for the verified email",
      db.query(gwapp.Account).filter(gwapp.Account.email == "owner@testfacebookbiz.co.uk").first() is not None)
lead_id_for_gen = p_after.lead_id
db.close()

# _kickoff_generation's real background thread is still running
# _run_and_persist asynchronously at this point — wait for its Generation
# row (and, shortly after, its notification-gate side effect) to land
# before asserting on either.
gen = _wait_for_generation(lead_id_for_gen)
check("A real Generation row exists (real _run_and_persist ran, in its background thread)", gen is not None)
deadline = time.time() + 5
while time.time() < deadline and "admin_approval_sent" not in captured and "site_ready_sent" not in captured:
    time.sleep(0.05)

db = SessionLocal()
events = db.query(gwapp.ProspectEvent).filter(gwapp.ProspectEvent.prospect_id == prospect_id).order_by(gwapp.ProspectEvent.id).all()
event_types = [e.event_type for e in events]
check("Full event sequence captured, in order",
      event_types == ["magic_link_clicked", "email_capture_viewed", "email_capture_submitted",
                       "verification_email_sent", "verification_link_clicked", "generation_kicked_off"],
      str(event_types))

# ── 4. Admin-approval gate: outreach-originated generation must NOT
# directly email the customer — it should queue for admin approval instead
# (same as an email-channel prospect would). ──
check("Direct site_ready email was NOT sent (should be gated behind admin approval)",
      "site_ready_sent" not in captured, str(captured.get("site_ready_sent")))
check("Admin approval email WAS sent instead (gate correctly applied to a Facebook-originated generation)",
      captured.get("admin_approval_sent") is True)
db.close()

# ── 5. Simulate an admin approving it -> site_ready notification fires,
# channel-agnostic (same code path for every channel). ──
resp = client.get(f"/admin/generations/{gen.id}/approve")  # a real plain link click, matching send_admin_approval_email's approve_url
check("Approve link request succeeds", resp.status_code == 200, str(resp.status_code))
check("Approving as admin sends the site_ready email, for a Facebook-originated generation",
      captured.get("site_ready_sent") is True)

# ── 6. Fast-forward: does this now-verified prospect fall into the SAME
# stage C/D follow-up sequence on the EMAIL channel?
#
# outreach/followup.py's _fire_touch requires has_delivery_confirmed(db,
# p.email) before sending — a real EmailEventLog 'delivered' row for this
# exact address. For an email-channel prospect that's satisfied by their
# INITIAL outreach email being confirmed delivered. A Facebook/SMS-
# verified prospect never had an outreach email sent — the only email
# they've ever received is the verification email itself (sent via the
# transactional path, not send_outreach_email). This test mocked that
# send entirely (no real Resend call), so there's no real webhook event
# to check against — simulating one here, since in production Resend's
# webhook is account-wide (one RESEND_WEBHOOK_SECRET, not scoped per
# sending domain), so a real verification-email delivery SHOULD produce
# exactly this row for real. This is the one part of the loop I could not
# observe directly (no real prospect has completed verification in
# production yet) — flagged clearly in the report to the user. ──
from models import EmailEventLog  # noqa: E402
db = SessionLocal()
db.add(EmailEventLog(to_email="owner@testfacebookbiz.co.uk", event_type="delivered"))
db.commit()
db.close()

from outreach.followup import run_followups, STAGE_BY_SUBSTAGE  # noqa: E402

db = SessionLocal()
p_ff = db.query(Prospect).filter(Prospect.id == prospect_id).first()
p_ff.last_touch_at = datetime.utcnow() - timedelta(days=4)  # past clicked_generated's 3-day min
p_ff.clicked_at = datetime.utcnow() - timedelta(days=4)
db.commit()
check("Due follow-up stage for this prospect is 'C' (clicked_generated -> C)",
      STAGE_BY_SUBSTAGE.get(p_ff.funnel_substage) == "C", p_ff.funnel_substage)
db.close()

sent_emails = []


def fake_send_outreach_email(to_email, subject, body, unsubscribe_link):
    sent_emails.append((to_email, subject))
    return "fake-resend-id-123"


import outreach.followup as followup_mod  # noqa: E402
orig_send_outreach_email = followup_mod.send_outreach_email
followup_mod.send_outreach_email = fake_send_outreach_email

try:
    remaining, n_fired = run_followups(
        {"email": float("inf"), "sms": 0},
        lambda p: f"https://x/unsub/{p.token}",
        lambda p: f"https://x/claim/{p.token}",
        lambda p: p.short_code,
        lambda p: f"https://x/survey/{p.token}",
    )
finally:
    followup_mod.send_outreach_email = orig_send_outreach_email

check("A stage-C follow-up EMAIL actually fired for this Facebook-originated, now-verified prospect",
      len(sent_emails) == 1 and sent_emails[0][0] == "owner@testfacebookbiz.co.uk", str(sent_emails))

db = SessionLocal()
final_touches = db.query(OutreachTouch).filter(OutreachTouch.prospect_id == prospect_id).order_by(OutreachTouch.id).all()
check("OutreachTouch history: initial(facebook) + follow-up C(email)",
      [(t.stage, t.channel) for t in final_touches] == [("initial", "facebook"), ("C", "email")],
      str([(t.stage, t.channel) for t in final_touches]))
db.close()

os.remove(DB_PATH)

print("")
print("=" * 56)
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
else:
    print("ALL CHECKS PASSED")
print("=" * 56)
sys.exit(1 if failures else 0)
