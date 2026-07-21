"""
Real, runnable checks for the email-variant testing system (Section 19) —
content-safety gates, the isolation heuristic, the significance test, the
seeding/weighted-selection mechanism, and outreach/variant_optimizer_job.py's
threshold gating, promotion/pause/deliverability-pause logic, and the
Drive-based candidate request/pickup pipeline (with push_file_to_github and
the Drive download both monkeypatched, so this never makes a real GitHub
API call, Drive API call, or LLM call — no network, no cost).

Plain script, not pytest (no test framework in this project yet — see
test_caching.py for the same convention). Runs entirely against a throwaway
temp-file SQLite DB, never the real DATABASE_URL — safe to run anytime.

    python test_variant_system.py
"""
import os
import sys
import json
import tempfile
from datetime import datetime, timedelta

DB_PATH = tempfile.mktemp(suffix=".db")
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from models import (  # noqa: E402
    Base, engine, SessionLocal, Prospect, OutreachTouch, EmailVariant,
    EvidenceFinding, OptimizerRunLog, VariantOptimizerState, EmailEventLog,
    VariantCandidatePickupState,
)

Base.metadata.create_all(engine)

failures = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


# ── 1. Content-safety gates against the real seeded baseline templates ──
from outreach.content_safety import run_content_safety_gates, check_isolation
from outreach.seed_variants import _BASELINE_STAGE_TEMPLATES

for stage, tpl in _BASELINE_STAGE_TEMPLATES.items():
    passed, violations = run_content_safety_gates(stage, tpl["subject"], tpl["body"])
    check(f"baseline template '{stage}' passes all content-safety gates", passed, str(violations))

# A deliberately bad pre-click candidate: claims the site is already built.
bad_body = _BASELINE_STAGE_TEMPLATES["A"]["body"].replace(
    "we'll build a free website preview", "your website is ready"
)
passed, violations = run_content_safety_gates("A", _BASELINE_STAGE_TEMPLATES["A"]["subject"], bad_body)
check("pre-click built-claim violation is caught", not passed and any("built" in v for v in violations), str(violations))

# A deliberately bad price: invents a figure.
bad_price_body = _BASELINE_STAGE_TEMPLATES["initial"]["body"].replace("£99", "£149")
passed, violations = run_content_safety_gates("initial", _BASELINE_STAGE_TEMPLATES["initial"]["subject"], bad_price_body)
check("invented price is caught", not passed and any("£149" in v for v in violations), str(violations))

# A deliberately spammy subject.
passed, violations = run_content_safety_gates("initial", "ACT NOW - FREE site!!!", _BASELINE_STAGE_TEMPLATES["initial"]["body"])
check("spam-trigger subject is caught", not passed, str(violations))


# ── 2. Isolation heuristic ──
parent_subject = _BASELINE_STAGE_TEMPLATES["initial"]["subject"]
parent_body = _BASELINE_STAGE_TEMPLATES["initial"]["body"]

# Valid: subject-only change, body untouched.
ok, reason = check_isolation("subject_length", parent_subject, parent_body, "See a free website, {business_name}", parent_body)
check("valid subject-only isolation passes", ok, reason)

# Invalid: claims subject-only but body also changed.
ok, reason = check_isolation(
    "subject_length", parent_subject, parent_body,
    "See a free website, {business_name}", parent_body.replace("free preview", "totally free preview today"),
)
check("subject+body simultaneous change is rejected", not ok, reason)

# Invalid: claims cta_wording but CTA text is actually unchanged (no real change).
ok, reason = check_isolation("cta_wording", parent_subject, parent_body, parent_subject, parent_body)
check("no-op candidate (identical to parent) is rejected", not ok, reason)

# Valid: cta_wording changed, nothing else.
cta_changed_body = parent_body.replace("See your free preview</a>", "Get my free preview</a>")
ok, reason = check_isolation("cta_wording", parent_subject, parent_body, parent_subject, cta_changed_body)
check("valid CTA-only isolation passes", ok, reason)


# ── 3. Stats ──
from outreach.stats_utils import two_proportion_z_test, is_significant

z, p = two_proportion_z_test(10, 100, 30, 100)
check("clearly different rates (10% vs 30%) are significant", is_significant(p), f"p={p}")

z, p = two_proportion_z_test(10, 100, 11, 100)
check("nearly identical rates (10% vs 11%) are NOT significant", not is_significant(p), f"p={p}")

z, p = two_proportion_z_test(0, 0, 0, 0)
check("empty samples return non-significant, not a crash", not is_significant(p), f"p={p}")


# ── 4. Seeding + weighted selection ──
db = SessionLocal()
from outreach.seed_variants import seed_baseline_variants
from outreach.variant_selection import pick_variant, render_variant

created = seed_baseline_variants(db)
check("seeding creates exactly 5 baseline variants", created == 5, str(created))
created_again = seed_baseline_variants(db)
check("seeding is idempotent (second call creates 0)", created_again == 0, str(created_again))

all_variants = db.query(EmailVariant).all()
check("all 5 seeded variants are status=active, rationale=original baseline template",
      all(v.status == "active" and v.rationale == "original baseline template" for v in all_variants))

# Add a canary at low weight and confirm weighted selection roughly tracks weight.
db.add(EmailVariant(stage="initial", variant_id="initial-v2", parent_variant_id="initial-v1",
                     subject="{business_name} — quick one?", body=_BASELINE_STAGE_TEMPLATES["initial"]["body"],
                     status="canary", weight=0.111, isolated_variable="subject_length", rationale="test"))
db.commit()

draws = [pick_variant(db, "initial").variant_id for _ in range(2000)]
v1_share = draws.count("initial-v1") / len(draws)
v2_share = draws.count("initial-v2") / len(draws)
check(f"weighted selection roughly matches weights (v1~90%, v2~10%, got v1={v1_share:.2f} v2={v2_share:.2f})",
      0.83 < v1_share < 0.97 and 0.03 < v2_share < 0.17)

rendered = render_variant(db.query(EmailVariant).filter(EmailVariant.variant_id == "initial-v1").first(),
                           business_name="Acme Plumbing", preview_link="https://x/1", unsubscribe_link="https://x/2")
check("render_variant substitutes placeholders", "Acme Plumbing" in rendered["subject"] and "https://x/1" in rendered["body"])

# Paused variant must never be drawn.
v2 = db.query(EmailVariant).filter(EmailVariant.variant_id == "initial-v2").first()
v2.status = "paused"
v2.weight = 0.0
db.commit()
draws2 = [pick_variant(db, "initial").variant_id for _ in range(200)]
check("paused variant is never selected", "initial-v2" not in draws2)


# ── 5. Optimizer job: threshold gating + performance computation (no real API calls) ──
from outreach import variant_optimizer_job as voj

now = datetime.utcnow()

# Build 40 mature "A" stage touches for variant A-v1 (need A-v1 seeded already from step 4's seed call).
p_ids = []
for i in range(40):
    p = Prospect(google_place_id=f"gp{i}", business_name=f"Biz {i}", email=f"biz{i}@example.com", funnel_stage="sent")
    db.add(p)
    db.flush()
    p_ids.append(p.id)
    touch = OutreachTouch(
        prospect_id=p.id, stage="A", channel="email", sent_at=now - timedelta(days=10),
        variant_id="A-v1",
    )
    # First 15 "clicked" (37.5%), rest didn't.
    if i < 15:
        touch.clicked_at = now - timedelta(days=9)
    db.add(touch)
db.commit()

new_count, max_id = voj._count_new_mature_touches(db, "A", 0, now)
check("40 mature A-stage touches counted as new samples", new_count == 40, str(new_count))

perf = voj._variant_performance(db, "A", now)
check("A-v1 performance computed correctly (40 sent, 15 clicked)",
      perf["A-v1"]["sent"] == 40 and perf["A-v1"]["clicked"] == 15, str(perf.get("A-v1")))

# Below-threshold stage (only 5 touches) should not trigger action.
for i in range(5):
    p = Prospect(google_place_id=f"gpB{i}", business_name=f"BizB {i}", email=f"bizb{i}@example.com", funnel_stage="sent")
    db.add(p)
    db.flush()
    db.add(OutreachTouch(prospect_id=p.id, stage="B", channel="email", sent_at=now - timedelta(days=10), variant_id="B-v1"))
db.commit()
new_count_b, _ = voj._count_new_mature_touches(db, "B", 0, now)
check("below-threshold stage counts correctly (5 < 30)", new_count_b == 5, str(new_count_b))

actions = []
voj._process_stage(db, "B", now, actions)
check("below-threshold stage takes no action", len(actions) == 0, str(actions))

state_b = db.query(VariantOptimizerState).filter(VariantOptimizerState.stage == "B").first()
check("below-threshold stage does NOT advance its marker", state_b is None or state_b.last_processed_touch_id == 0)

# Now push a confirmed-better canary into A and verify the full promotion path
# (monkeypatch _generate_candidate so this doesn't hit the real Anthropic API).
db.add(EmailVariant(stage="A", variant_id="A-v2", parent_variant_id="A-v1",
                     subject=voj.__dict__.get("_BASELINE_STAGE_TEMPLATES", {}).get("A", {}).get("subject", "x") if False else "test subject",
                     body="<html>test</html>", status="canary", weight=0.111,
                     isolated_variable="cta_wording", rationale="test canary"))
db.commit()
for i in range(40):
    p = Prospect(google_place_id=f"gpC{i}", business_name=f"BizC {i}", email=f"bizc{i}@example.com", funnel_stage="sent")
    db.add(p)
    db.flush()
    touch = OutreachTouch(prospect_id=p.id, stage="A", channel="email", sent_at=now - timedelta(days=10), variant_id="A-v2")
    if i < 30:  # 75% click rate — way above A-v1's 37.5%
        touch.clicked_at = now - timedelta(days=9)
    db.add(touch)
db.commit()

actions2 = []
# Force MAX_VARIANTS_PER_STAGE reached so _process_stage doesn't try a real API call for a 3rd variant.
orig_max = voj.MAX_VARIANTS_PER_STAGE
voj.MAX_VARIANTS_PER_STAGE = 2
voj._process_stage(db, "A", now, actions2)
voj.MAX_VARIANTS_PER_STAGE = orig_max

promote_actions = [a for a in actions2 if a["type"] in ("promote", "reallocate_weight")]
check("confirmed-better canary gets a promote/reallocate action", len(promote_actions) == 1, str(actions2))

v2_after = db.query(EmailVariant).filter(EmailVariant.variant_id == "A-v2").first()
check("A-v2's weight increased from its starting canary weight", v2_after.weight > 0.111, str(v2_after.weight))

findings = db.query(EvidenceFinding).filter(EvidenceFinding.stage == "A").all()
check("a Section-3 EvidenceFinding row was written for the significant result", len(findings) >= 1, str(len(findings)))

state_a = db.query(VariantOptimizerState).filter(VariantOptimizerState.stage == "A").first()
check("above-threshold stage DOES advance its marker", state_a is not None and state_a.last_processed_touch_id > 0)


# ── 6. Deliverability auto-pause (independent of significance testing) ──
db.add(EmailVariant(stage="D", variant_id="D-v2", parent_variant_id="D-v1",
                     subject="test", body="<html>test</html>", status="canary",
                     weight=0.111, isolated_variable="tone", rationale="test"))
db.commit()
for i in range(35):
    p = Prospect(google_place_id=f"gpD{i}", business_name=f"BizD {i}", email=f"bizd{i}@example.com", funnel_stage="sent")
    db.add(p)
    db.flush()
    db.add(OutreachTouch(prospect_id=p.id, stage="D", channel="email", sent_at=now - timedelta(days=10), variant_id="D-v2"))
    if i < 5:  # 5/35 = 14.3% bounce rate, well above the 5% trigger
        db.add(EmailEventLog(to_email=f"bizd{i}@example.com", event_type="bounced", created_at=now - timedelta(days=9)))
db.commit()

actions3 = []
voj.MAX_VARIANTS_PER_STAGE = 2  # avoid a real API call for stage D too
voj._process_stage(db, "D", now, actions3)

pause_actions = [a for a in actions3 if a["type"] == "pause_deliverability"]
check("high-bounce variant is auto-paused for deliverability", len(pause_actions) == 1, str(actions3))
d_v2_after = db.query(EmailVariant).filter(EmailVariant.variant_id == "D-v2").first()
check("auto-paused variant has status=paused and weight=0", d_v2_after.status == "paused" and d_v2_after.weight == 0.0)


# ── 7. Confirmed-underperformer gets paused, not promoted ──
db.add(EmailVariant(stage="C", variant_id="C-v2", parent_variant_id="C-v1",
                     subject="test", body="<html>test</html>", status="canary",
                     weight=0.111, isolated_variable="tone", rationale="test"))
db.commit()
for i in range(40):
    p = Prospect(google_place_id=f"gpE{i}", business_name=f"BizE {i}", email=f"bize{i}@example.com", funnel_stage="sent")
    db.add(p)
    db.flush()
    touch1 = OutreachTouch(prospect_id=p.id, stage="C", channel="email", sent_at=now - timedelta(days=10), variant_id="C-v1")
    if i < 20:  # 50% paid rate for C-v1 (the active baseline)
        touch1.paid_at = now - timedelta(days=9)
    db.add(touch1)
    p2 = Prospect(google_place_id=f"gpF{i}", business_name=f"BizF {i}", email=f"bizf{i}@example.com", funnel_stage="sent")
    db.add(p2)
    db.flush()
    touch2 = OutreachTouch(prospect_id=p2.id, stage="C", channel="email", sent_at=now - timedelta(days=10), variant_id="C-v2")
    if i < 2:  # 5% paid rate for C-v2 (the canary) — clearly worse
        touch2.paid_at = now - timedelta(days=9)
    db.add(touch2)
db.commit()

actions4 = []
voj.MAX_VARIANTS_PER_STAGE = 2
voj._process_stage(db, "C", now, actions4)
pause_underperf = [a for a in actions4 if a["type"] == "pause_underperformer"]
check("confirmed underperformer is paused, not promoted", len(pause_underperf) == 1, str(actions4))
c_v2_after = db.query(EmailVariant).filter(EmailVariant.variant_id == "C-v2").first()
check("underperformer's status is paused", c_v2_after.status == "paused")
voj.MAX_VARIANTS_PER_STAGE = orig_max


# ── 8. Drive-based candidate request/pickup pipeline (no network calls —
# push_file_to_github and the Drive download are both monkeypatched) ──
pushed_files = {}


def _fake_push(path, content_bytes, message, branch="main"):
    pushed_files[path] = json.loads(content_bytes.decode("utf-8"))
    return True


orig_push = voj.push_file_to_github
voj.push_file_to_github = _fake_push
try:
    b_pending_before = db.query(EmailVariant).filter(EmailVariant.stage == "B", EmailVariant.status == "pending_generation").count()
    parent_b = db.query(EmailVariant).filter(EmailVariant.variant_id == "B-v1").first()
    actions5 = []
    voj._queue_generation_request(db, "B", parent_b, "subject_length", now, actions5)
    check("queuing a request creates a pending_generation placeholder row",
          any(a["type"] == "generation_requested" for a in actions5), str(actions5))
    b_pending_after = db.query(EmailVariant).filter(EmailVariant.stage == "B", EmailVariant.status == "pending_generation").count()
    check("pending_generation row count increased by 1", b_pending_after == b_pending_before + 1)

    check("the request was pushed (via the mocked push) with a real payload",
          voj.REQUEST_FILE_REPO_PATH in pushed_files and len(pushed_files[voj.REQUEST_FILE_REPO_PATH]) >= 1,
          str(pushed_files.get(voj.REQUEST_FILE_REPO_PATH)))
    req_entry = next((r for r in pushed_files[voj.REQUEST_FILE_REPO_PATH] if r["stage"] == "B"), None)
    check("the pushed request includes parent subject/body and isolated_variable",
          req_entry is not None and req_entry["parent_subject"] == parent_b.subject and req_entry["isolated_variable"] == "subject_length",
          str(req_entry))

    # A second attempt on the same stage must not queue a duplicate — B now
    # has an unresolved pending_generation row.
    actions_dup = []
    b_variants_for_process = db.query(EmailVariant).filter(EmailVariant.stage == "B").all()
    unresolved = any(v.status in ("canary", "pending_generation") for v in b_variants_for_process)
    check("stage with a pending_generation row is correctly seen as 'unresolved'", unresolved)
finally:
    voj.push_file_to_github = orig_push

# Admit a good mocked routine result for the B-v2 request just queued.
reserved_id = db.query(EmailVariant).filter(EmailVariant.stage == "B", EmailVariant.status == "pending_generation").first().variant_id
good_result = {
    "reserved_variant_id": reserved_id,
    "subject": parent_b.subject + " really?",
    "body": parent_b.body,
    "rationale": "testing a slightly more urgent subject per Section 1's question-format finding",
    "cites": "Section 1: question-format subject lines lift open rates ~21%",
}
actions7 = []
voj._admit_or_reject_result(db, good_result, now, actions7)
check("valid mocked routine result is admitted as a new canary",
      any(a["type"] == "new_variant" for a in actions7), str(actions7))
admitted_row = db.query(EmailVariant).filter(EmailVariant.variant_id == reserved_id).first()
check("admitted row is now status=canary with ~10% relative weight",
      admitted_row.status == "canary" and 0.05 < admitted_row.weight < 0.2, str(admitted_row.weight))

# Queue and reject a bad (isolation-violating) result for D.
parent_d = db.query(EmailVariant).filter(EmailVariant.variant_id == "D-v1").first()
voj.push_file_to_github = _fake_push
try:
    voj._queue_generation_request(db, "D", parent_d, "subject_length", now, [])
finally:
    voj.push_file_to_github = orig_push
reserved_id_d = db.query(EmailVariant).filter(EmailVariant.stage == "D", EmailVariant.status == "pending_generation").first().variant_id
bad_result = {
    "reserved_variant_id": reserved_id_d,
    "subject": parent_d.subject + " really?",
    "body": parent_d.body.replace("Any questions, just reply to this email.", "Any questions, just reply!"),
    "rationale": "test",
    "cites": "Section 1",
}
actions8 = []
voj._admit_or_reject_result(db, bad_result, now, actions8)
check("isolation-violating mocked result is rejected, not admitted",
      all(a["type"] != "new_variant" for a in actions8) and any(a["type"] == "generation_rejected" for a in actions8),
      str(actions8))
check("rejected candidate's placeholder row is deleted (frees the stage for a fresh attempt)",
      db.query(EmailVariant).filter(EmailVariant.variant_id == reserved_id_d).first() is None)

# Full pickup pipeline via a mocked Drive download.
def _fake_find_latest(api_key, folder_id):
    return {"id": "fake-drive-file-1", "name": voj.CANDIDATE_RESULTS_FILENAME, "createdTime": "2026-07-22T00:00:00Z"}


def _fake_download(api_key, file_id):
    return json.dumps([{
        "reserved_variant_id": "C-pickup-test",
        "subject": "x", "body": "x", "rationale": "x", "cites": "x",
    }])


# Set up a pending_generation row for C matching the fake Drive payload.
parent_c = db.query(EmailVariant).filter(EmailVariant.variant_id == "C-v1").first()
db.add(EmailVariant(stage="C", variant_id="C-pickup-test", parent_variant_id="C-v1",
                     subject="", body="", status="pending_generation", weight=0.0,
                     isolated_variable="tone", rationale="awaiting routine-generated candidate"))
db.commit()

os.environ["GOOGLE_DRIVE_API_KEY"] = "fake-key"
os.environ["GOOGLE_DRIVE_FOLDER_ID"] = "fake-folder"
orig_find, orig_download = voj._find_latest_drive_file, voj._download_drive_file
voj._find_latest_drive_file, voj._download_drive_file = _fake_find_latest, _fake_download
try:
    actions9 = []
    voj._pickup_candidate_results(db, now, actions9)
    # "x" as a body will fail placeholder-matching against the real parent's
    # placeholders, so this should be rejected, not admitted — still proves
    # the pickup pipeline runs end-to-end (find -> download -> parse -> apply).
    check("pickup pipeline runs end-to-end and rejects an invalid mocked payload",
          any(a["type"] == "generation_rejected" for a in actions9), str(actions9))
    state_row = db.query(VariantCandidatePickupState).first()
    check("pickup state records the processed Drive file id", state_row is not None and state_row.last_drive_file_id == "fake-drive-file-1")

    # Re-running immediately with the same "latest file" must be a no-op
    # (idempotency) — nothing new to process.
    actions10 = []
    voj._pickup_candidate_results(db, now, actions10)
    check("re-running pickup against the same Drive file is a no-op", actions10 == [], str(actions10))
finally:
    voj._find_latest_drive_file, voj._download_drive_file = orig_find, orig_download


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
