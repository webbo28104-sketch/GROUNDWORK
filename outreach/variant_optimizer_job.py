"""
Groundwork outreach — autonomous email-variant testing job
(docs/outreach-pipeline-spec.md Section 19, docs/cold-email-evidence-library.md).

Runnable as a module or script from the project root:
    python outreach/variant_optimizer_job.py
    python -m outreach.variant_optimizer_job --dry-run

Same architecture as the other outreach cron jobs (send_job.py,
domain_billing.py, email_discovery_job.py, pipeline.py) — a standalone
Python script pointed at by its own Railway Cron service, direct DB access.
All the DB-side work here (performance stats, promotion/pause, safety
checks, threshold gating) is plain code, zero API cost, so it stays hourly.

CANDIDATE GENERATION IS A DAILY CLAUDE CODE ROUTINE, NOT A DIRECT API CALL
(changed 2026-07-21, by request — avoids metered Anthropic API cost).
Cowork and scheduled Claude Code routines are both confirmed blocked from
writing to production (CLAUDE.md's outreach-pipeline pointer section), so
this job still can't hand DB writes to a routine directly — instead it
mirrors the nightly email-discovery routine's proven Drive-based handoff
exactly:
  1. When a stage needs a new candidate, this job reserves a variant_id as
     a status="pending_generation" placeholder row, then exports EVERY
     currently-pending request (across all stages) to
     outreach/discovery_batches/variant_candidate_request.json and pushes
     it via the GitHub REST Contents API (outreach/github_push.py) — a
     plain HTTPS PUT with a token, not a local git push (this container
     has no git remote configured).
  2. A separate Claude Code routine (daily — see docs/outreach-pipeline-
     spec.md Section 19 for its trigger id/schedule) reads that file plus
     docs/cold-email-evidence-library.md from its own git checkout, and
     writes one candidate per pending request to a Google Drive file
     (groundwork-variant-candidates.json) — it cannot reach the DB or push
     git either, same platform restriction as the discovery routine.
  3. This job's own next run picks up that Drive file (a plain read-only
     Drive API key, same as outreach/pickup_drive_results.py), runs every
     candidate through the placeholder/content-safety/isolation checks,
     and either fills in the reserved row (admitting it as a canary) or
     deletes it (so a fresh attempt can be queued, possibly with a
     different isolated variable).

THRESHOLD-GATED, NOT CALENDAR-GATED: intended to run hourly, but does
nothing meaningful until a stage has accumulated MIN_SAMPLE_SIZE new,
outcome-mature touches since the last time that stage was acted on
(VariantOptimizerState.last_processed_touch_id). At ~60 sends/day this
naturally no-ops almost every run; at ~8,000/month it naturally becomes
responsive within a run or two — no manual retuning of the schedule as
volume scales.

EYES-BEFORE-JUDGMENT THRESHOLD: MIN_SAMPLE_SIZE (30, Section 5b's existing
standard) gates not just "enough new samples to re-evaluate a stage at
all," but also, independently, "enough sends a SPECIFIC variant has had
before its own performance or deliverability is judged" — see the
`v_perf["sent"] < MIN_SAMPLE_SIZE` and `sample_size < MIN_SAMPLE_SIZE`
guards inside _process_stage below. A variant with 3 sends and 1 click
is not "33% click rate," it's noise; nothing acts on it either way until
it has real exposure.

GIT WRITE LIMITATION FOR SECTION 3 (unchanged from before this rework):
findings are written to the EvidenceFinding table, not appended to
docs/cold-email-evidence-library.md's Section 3 directly — see that
file's architecture note and /admin/variants. (Note: this job COULD now
push via the same GitHub Contents API mechanism used for the request
file above, since that turned out to work fine from a Railway container —
unlike the earlier assumption that no git write path existed at all. Not
done here to keep Section 3 as one clean, queryable table rather than
also parsing/appending markdown; revisit if you'd rather see it land in
the actual file.)

Per-run sequence, per stage (initial/A/B/C/D — hail_mary is not variant-
tested, see outreach/seed_variants.py):
  1. Pick up any new Drive candidate results from the daily routine first
     (independent of any single stage's threshold).
  2. Count new, outcome-mature touches since this stage's last-processed
     marker. Below MIN_SAMPLE_SIZE: no-op, log why.
  3. Otherwise: compute per-variant open/click/paid rates, run a two-
     proportion z-test for each canary (with its own real sample — see
     "eyes-before-judgment" above) against the current best active variant
     on that stage's primary metric (click for initial/A/B; paid for C/D).
  4. Confirmed winners get weight moved gradually toward the active pool
     (never an abrupt swap) and are promoted to "active" once they reach
     parity. Confirmed losers are paused. Any variant with a real sample
     whose own bounce/complaint rate breaches the same thresholds
     outreach/ramp.py uses channel-wide is auto-paused immediately,
     independent of performance.
  5. Every significant result gets an EvidenceFinding row.
  6. If there's no unresolved request/canary for the stage and it's under
     MAX_VARIANTS_PER_STAGE, reserve and queue exactly one new candidate
     request (see the Drive handoff above).
  7. Always writes one OptimizerRunLog row, whether or not anything happened.

Environment: DATABASE_URL, GITHUB_PUSH_TOKEN, GOOGLE_DRIVE_API_KEY,
GOOGLE_DRIVE_FOLDER_ID. No ANTHROPIC_API_KEY needed anymore.
"""
import os
import sys
import json
import logging
import argparse
from datetime import datetime, timedelta

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

import requests
from sqlalchemy import func

from models import (  # noqa: E402
    SessionLocal, OutreachTouch, Prospect, EmailEventLog, EmailVariant,
    EvidenceFinding, OptimizerRunLog, VariantOptimizerState,
    VariantCandidatePickupState, init_db,
)
from outreach.seed_variants import seed_baseline_variants
from outreach.stats_utils import two_proportion_z_test, is_significant
from outreach.content_safety import run_content_safety_gates, check_isolation
from outreach.ramp import EMAIL_BOUNCE_RATE_TRIGGER, EMAIL_SPAM_RATE_TRIGGER
from outreach.github_push import push_file_to_github

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("outreach.variant_optimizer_job")

STAGES = ["initial", "A", "B", "C", "D"]
# Primary metric per stage — the meaningful conversion event to test copy
# against. Pre-click stages (initial/A/B) haven't clicked yet by
# definition, so "clicked" (the magic-link click) is the real conversion
# event to optimize. Post-click stages (C/D) are cohorts that have ALREADY
# clicked (that's how they got to C/D — see outreach/followup.py's
# STAGE_BY_SUBSTAGE), so their meaningful event is "paid" instead.
PRIMARY_METRIC_BY_STAGE = {"initial": "clicked", "A": "clicked", "B": "clicked", "C": "paid", "D": "paid"}

# Section 5b's existing standard, reused here per the build spec — also
# the "eyes before judgment" threshold (see module docstring).
MIN_SAMPLE_SIZE = 30
# A touch needs to sit for a few days before "did it convert" is a fair
# question — a touch sent yesterday hasn't had time to be opened/clicked/
# paid yet, and counting it as a same-as-baseline "no" would bias every
# rate downward for no real reason. Matches this codebase's existing
# follow-up cadence (stages fire on a 2-4 day cycle), so 3 days is enough
# runway to see most real opens/clicks without waiting so long that recent
# volume never counts.
OUTCOME_MATURITY_DAYS = 3
# Safety cap on proliferation — an assumption, not something the build spec
# specified a number for. Six concurrent variants (baseline + up to 5
# challengers, most paused/promoted over time) is enough headroom without
# risking an unbounded table if generation ever loops unexpectedly.
MAX_VARIANTS_PER_STAGE = 6
CANARY_ALLOCATION_PCT = 0.10
# How much of the gap to the active pool's weight a confirmed winner closes
# per run it's reconfirmed significant — gradual reallocation, not a swap.
# 0.5 means "halve the remaining gap each time," so a canary approaches but
# never overshoots parity, and takes several confirmed-good runs to fully
# arrive rather than one lucky significant result promoting it instantly.
WEIGHT_CONVERGENCE_FACTOR = 0.5

ISOLATED_VARIABLE_CANDIDATES = [
    "subject_length", "subject_format_question", "subject_personalization",
    "cta_wording", "personalization_depth", "paragraph_structure", "tone",
]

# ── Drive-based generation handoff (see module docstring) ──────────────
REQUEST_FILE_REPO_PATH = "outreach/discovery_batches/variant_candidate_request.json"
CANDIDATE_RESULTS_FILENAME = "groundwork-variant-candidates.json"
# Same shared folder the discovery routine already writes into.
DRIVE_FOLDER_ID_ENV = "GOOGLE_DRIVE_FOLDER_ID"
DRIVE_API_BASE = "https://www.googleapis.com/drive/v3"
DRIVE_REQUEST_TIMEOUT = 30


def _get_or_create_state(db, stage):
    state = db.query(VariantOptimizerState).filter(VariantOptimizerState.stage == stage).first()
    if not state:
        state = VariantOptimizerState(stage=stage, last_processed_touch_id=0)
        db.add(state)
        db.commit()
    return state


def _count_new_mature_touches(db, stage, since_id, now):
    maturity_cutoff = now - timedelta(days=OUTCOME_MATURITY_DAYS)
    q = db.query(OutreachTouch).filter(
        OutreachTouch.stage == stage,
        OutreachTouch.channel == "email",
        OutreachTouch.variant_id.isnot(None),
        OutreachTouch.id > since_id,
        OutreachTouch.sent_at <= maturity_cutoff,
    )
    max_id = db.query(func.max(OutreachTouch.id)).filter(
        OutreachTouch.stage == stage, OutreachTouch.channel == "email",
        OutreachTouch.variant_id.isnot(None), OutreachTouch.sent_at <= maturity_cutoff,
    ).scalar() or since_id
    return q.count(), max_id


def _variant_performance(db, stage, now):
    """{variant_id: {"sent":, "opened":, "clicked":, "paid":}} over every
    outcome-mature touch ever sent for this stage (not just the new batch —
    a fair comparison needs each variant's full track record)."""
    maturity_cutoff = now - timedelta(days=OUTCOME_MATURITY_DAYS)
    rows = db.query(
        OutreachTouch.variant_id, OutreachTouch.opened_at, OutreachTouch.clicked_at, OutreachTouch.paid_at
    ).filter(
        OutreachTouch.stage == stage, OutreachTouch.channel == "email",
        OutreachTouch.variant_id.isnot(None), OutreachTouch.sent_at <= maturity_cutoff,
    ).all()
    perf = {}
    for variant_id, opened_at, clicked_at, paid_at in rows:
        p = perf.setdefault(variant_id, {"sent": 0, "opened": 0, "clicked": 0, "paid": 0})
        p["sent"] += 1
        if opened_at:
            p["opened"] += 1
        if clicked_at:
            p["clicked"] += 1
        if paid_at:
            p["paid"] += 1
    return perf


def _variant_deliverability(db, variant_id):
    """(bounce_rate, complaint_rate, sample_size) for this variant's own
    sends — same per-source technique app.py's /admin/deliverability page
    already uses, applied per-variant instead of per-discovery-source."""
    emails = [
        e for (e,) in db.query(Prospect.email).join(
            OutreachTouch, OutreachTouch.prospect_id == Prospect.id
        ).filter(OutreachTouch.variant_id == variant_id, OutreachTouch.channel == "email").all()
        if e
    ]
    sample_size = len(emails)
    if sample_size == 0:
        return 0.0, 0.0, 0
    lowered = {e.strip().lower() for e in emails}
    bounced = db.query(EmailEventLog).filter(
        EmailEventLog.event_type.in_(["email.bounced", "bounced"]),
        func.lower(EmailEventLog.to_email).in_(lowered),
    ).count()
    complained = db.query(EmailEventLog).filter(
        EmailEventLog.event_type.in_(["email.complained", "complained"]),
        func.lower(EmailEventLog.to_email).in_(lowered),
    ).count()
    return bounced / sample_size, complained / sample_size, sample_size


def _least_explored_isolated_variable(db, stage):
    used = db.query(EmailVariant.isolated_variable).filter(
        EmailVariant.stage == stage, EmailVariant.isolated_variable.isnot(None)
    ).all()
    counts = {v: 0 for v in ISOLATED_VARIABLE_CANDIDATES}
    for (val,) in used:
        if val in counts:
            counts[val] += 1
    return min(ISOLATED_VARIABLE_CANDIDATES, key=lambda v: counts[v])


def _placeholders(text):
    import re
    return set(re.findall(r"\{(\w+)\}", text))


# ── Outbound: queue a candidate request for the daily routine ──────────

def _queue_generation_request(db, stage, parent, isolated_variable, now, actions):
    """Reserves a variant_id as a pending_generation placeholder, then
    re-exports and pushes the FULL current set of pending requests (across
    every stage, not just this one) — the export is idempotent/self-
    cleaning: once a request is resolved (admitted or rejected), it's no
    longer a pending_generation row, so the next export naturally omits
    it, with no separate "mark as consumed" step needed on the request
    file itself."""
    existing_count = db.query(EmailVariant).filter(EmailVariant.stage == stage).count()
    reserved_variant_id = f"{stage}-v{existing_count + 1}"
    db.add(EmailVariant(
        stage=stage, variant_id=reserved_variant_id, parent_variant_id=parent.variant_id,
        subject="", body="", status="pending_generation", weight=0.0,
        rationale="awaiting routine-generated candidate", isolated_variable=isolated_variable,
    ))
    db.commit()
    logger.info("Reserved %s for stage %r (isolated_variable=%s) — queuing generation request",
                reserved_variant_id, stage, isolated_variable)
    actions.append({
        "type": "generation_requested", "stage": stage, "variant_id": reserved_variant_id,
        "reason": f"isolated_variable={isolated_variable}",
    })
    _export_and_push_requests(db)


def _export_and_push_requests(db):
    pending = db.query(EmailVariant).filter(EmailVariant.status == "pending_generation").all()
    requests_payload = []
    for p in pending:
        parent = db.query(EmailVariant).filter(EmailVariant.variant_id == p.parent_variant_id).first()
        if not parent:
            logger.error("Pending request %s has no resolvable parent %r — skipping in export",
                         p.variant_id, p.parent_variant_id)
            continue
        recent_findings = db.query(EvidenceFinding).filter(EvidenceFinding.stage == p.stage).order_by(
            EvidenceFinding.created_at.desc()
        ).limit(10).all()
        requests_payload.append({
            "reserved_variant_id": p.variant_id,
            "stage": p.stage,
            "parent_variant_id": parent.variant_id,
            "parent_subject": parent.subject,
            "parent_body": parent.body,
            "isolated_variable": p.isolated_variable,
            "recent_findings": [
                f"{f.finding} (sample size {f.sample_size}, isolated variable: {f.isolated_variable})"
                for f in recent_findings
            ],
            "requested_at": p.created_at.isoformat(),
        })

    content = json.dumps(requests_payload, indent=2).encode("utf-8")
    ok = push_file_to_github(
        REQUEST_FILE_REPO_PATH, content,
        f"Variant candidate request export ({datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}) [Railway Cron]",
    )
    if not ok:
        logger.error("Failed to push %s — the daily routine will see a stale/missing request file until this succeeds",
                     REQUEST_FILE_REPO_PATH)
    return ok


# ── Inbound: pick up the daily routine's Drive results ──────────────────

def _find_latest_drive_file(api_key, folder_id):
    query = f"'{folder_id}' in parents and name = '{CANDIDATE_RESULTS_FILENAME}' and trashed = false"
    resp = requests.get(
        f"{DRIVE_API_BASE}/files",
        params={"q": query, "key": api_key, "fields": "files(id,name,createdTime)",
                "orderBy": "createdTime desc", "pageSize": 1},
        timeout=DRIVE_REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    files = resp.json().get("files", [])
    return files[0] if files else None


def _download_drive_file(api_key, file_id):
    resp = requests.get(
        f"{DRIVE_API_BASE}/files/{file_id}", params={"alt": "media", "key": api_key}, timeout=DRIVE_REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.text


def _admit_or_reject_result(db, result, now, actions):
    reserved_variant_id = result.get("reserved_variant_id")
    row = db.query(EmailVariant).filter(
        EmailVariant.variant_id == reserved_variant_id, EmailVariant.status == "pending_generation"
    ).first()
    if not row:
        logger.warning("Drive result references %r, but no matching pending_generation row exists (already resolved, or stale) — skipping",
                        reserved_variant_id)
        return

    parent = db.query(EmailVariant).filter(EmailVariant.variant_id == row.parent_variant_id).first()
    subject, body = result.get("subject", ""), result.get("body", "")

    def _reject(reason):
        logger.warning("Rejecting candidate %s: %s", reserved_variant_id, reason)
        actions.append({"type": "generation_rejected", "stage": row.stage, "variant_id": reserved_variant_id, "reason": reason})
        db.delete(row)
        db.commit()

    if not parent:
        _reject(f"parent {row.parent_variant_id!r} no longer resolvable")
        return
    if not subject or not body:
        _reject("empty subject or body from the routine")
        return
    if _placeholders(subject) != _placeholders(parent.subject) or _placeholders(body) != _placeholders(parent.body):
        _reject("placeholder mismatch vs. parent — would crash at send time")
        return
    passed, violations = run_content_safety_gates(row.stage, subject, body)
    if not passed:
        _reject(f"content-safety gate failed: {violations}")
        return
    iso_ok, iso_reason = check_isolation(row.isolated_variable, parent.subject, parent.body, subject, body)
    if not iso_ok:
        _reject(f"isolation check failed: {iso_reason}")
        return

    active_total_weight = sum(
        v.weight for v in db.query(EmailVariant).filter(
            EmailVariant.stage == row.stage, EmailVariant.status == "active"
        ).all()
    ) or 1.0
    canary_weight = active_total_weight * (CANARY_ALLOCATION_PCT / (1 - CANARY_ALLOCATION_PCT))

    row.subject = subject
    row.body = body
    row.status = "canary"
    row.weight = canary_weight
    row.rationale = f"{result.get('rationale', '')} | cites: {result.get('cites', '')}"
    db.commit()
    actions.append({
        "type": "new_variant", "stage": row.stage, "variant_id": reserved_variant_id,
        "reason": f"isolated_variable={row.isolated_variable}", "rationale": row.rationale,
    })
    logger.info("Admitted candidate %s for stage %r (isolated_variable=%s)", reserved_variant_id, row.stage, row.isolated_variable)


def _pickup_candidate_results(db, now, actions):
    """Checks Google Drive for a new candidate-results file from the daily
    routine and applies every entry in it. Safe to call every hourly run —
    logs and returns quietly if there's nothing new (same idempotent
    pattern as outreach/pickup_drive_results.py)."""
    api_key = os.environ.get("GOOGLE_DRIVE_API_KEY")
    folder_id = os.environ.get(DRIVE_FOLDER_ID_ENV)
    if not api_key or not folder_id:
        logger.warning("GOOGLE_DRIVE_API_KEY/%s not set — cannot check for routine-generated candidates this run",
                        DRIVE_FOLDER_ID_ENV)
        return

    try:
        latest = _find_latest_drive_file(api_key, folder_id)
    except Exception:
        logger.exception("Drive lookup failed — will retry next run")
        return
    if not latest:
        return

    state = db.query(VariantCandidatePickupState).first()
    if state and state.last_drive_file_id == latest["id"]:
        return

    logger.info("Found new variant-candidate results file: %s (created %s)", latest["id"], latest["createdTime"])
    try:
        text = _download_drive_file(api_key, latest["id"])
        results = json.loads(text)
    except Exception:
        logger.exception("Failed to download/parse %s — will retry next run", latest["id"])
        return

    for result in results:
        try:
            _admit_or_reject_result(db, result, now, actions)
        except Exception:
            logger.exception("Error applying candidate result %s", result.get("reserved_variant_id"))

    if not state:
        state = VariantCandidatePickupState()
        db.add(state)
    state.last_drive_file_id = latest["id"]
    db.commit()


def _process_stage(db, stage, now, actions):
    """Returns the number of new mature samples counted for this stage this
    run (whether or not that crossed the threshold)."""
    state = _get_or_create_state(db, stage)
    new_count, max_id_seen = _count_new_mature_touches(db, stage, state.last_processed_touch_id, now)

    if new_count < MIN_SAMPLE_SIZE:
        logger.info("Stage %r: %d new mature samples since last action (threshold %d) — no action",
                    stage, new_count, MIN_SAMPLE_SIZE)
        return new_count

    logger.info("Stage %r: %d new mature samples (threshold %d met) — evaluating", stage, new_count, MIN_SAMPLE_SIZE)
    metric = PRIMARY_METRIC_BY_STAGE[stage]
    perf = _variant_performance(db, stage, now)
    variants = db.query(EmailVariant).filter(EmailVariant.stage == stage).all()

    active_variants = [v for v in variants if v.status == "active"]
    best_active = None
    if active_variants:
        best_active = max(
            active_variants,
            key=lambda v: (perf.get(v.variant_id, {}).get(metric, 0) / perf.get(v.variant_id, {"sent": 1})["sent"])
            if perf.get(v.variant_id, {}).get("sent", 0) else 0,
        )

    # ── Deliverability auto-pause — independent of performance testing.
    # Requires MIN_SAMPLE_SIZE real sends of THIS variant first (eyes-
    # before-judgment) — a single bounce out of 3 sends is not a signal. ──
    for v in variants:
        if v.status in ("paused", "pending_generation"):
            continue
        bounce_rate, complaint_rate, sample_size = _variant_deliverability(db, v.variant_id)
        if sample_size < MIN_SAMPLE_SIZE:
            continue
        if bounce_rate >= EMAIL_BOUNCE_RATE_TRIGGER or complaint_rate >= EMAIL_SPAM_RATE_TRIGGER:
            v.status = "paused"
            v.weight = 0.0
            v.notes = (
                f"{(v.notes + ' | ') if v.notes else ''}"
                f"Auto-paused {now.strftime('%Y-%m-%d')}: bounce_rate={bounce_rate:.3f}, "
                f"complaint_rate={complaint_rate:.3f} (sample {sample_size})"
            )
            db.commit()
            actions.append({
                "type": "pause_deliverability", "stage": stage, "variant_id": v.variant_id,
                "reason": f"bounce_rate={bounce_rate:.1%} or complaint_rate={complaint_rate:.2%} breached threshold",
            })
            logger.warning("Stage %r: auto-paused %s for deliverability (bounce=%.1f%%, complaint=%.2f%%)",
                            stage, v.variant_id, bounce_rate * 100, complaint_rate * 100)
            if v is best_active:
                best_active = None

    # ── Performance-based promotion / demotion of canaries — both sides
    # of the comparison need MIN_SAMPLE_SIZE real sends first. ──
    if best_active:
        best_perf = perf.get(best_active.variant_id, {"sent": 0})
        for v in [x for x in variants if x.status == "canary"]:
            v_perf = perf.get(v.variant_id, {"sent": 0, metric: 0})
            if v_perf["sent"] < MIN_SAMPLE_SIZE or best_perf["sent"] < MIN_SAMPLE_SIZE:
                continue
            z, p_value = two_proportion_z_test(
                best_perf.get(metric, 0), best_perf["sent"], v_perf.get(metric, 0), v_perf["sent"],
            )
            if not is_significant(p_value):
                continue

            v_rate = v_perf.get(metric, 0) / v_perf["sent"]
            best_rate = best_perf.get(metric, 0) / best_perf["sent"]
            finding_text = (
                f"Variant {v.variant_id} ({metric} rate {v_rate:.1%}, n={v_perf['sent']}) vs. "
                f"{best_active.variant_id} ({metric} rate {best_rate:.1%}, n={best_perf['sent']}) — p={p_value:.4f}"
            )
            if v_rate > best_rate:
                gap = best_active.weight - v.weight
                v.weight = v.weight + gap * WEIGHT_CONVERGENCE_FACTOR
                promoted = False
                if v.weight >= best_active.weight * 0.95:
                    v.status = "active"
                    promoted = True
                db.commit()
                actions.append({
                    "type": "promote" if promoted else "reallocate_weight", "stage": stage, "variant_id": v.variant_id,
                    "reason": finding_text,
                })
                db.add(EvidenceFinding(
                    stage=stage, finding=f"{finding_text} — {v.variant_id} significantly outperforms",
                    sample_size=v_perf["sent"] + best_perf["sent"], isolated_variable=v.isolated_variable,
                    adaptation_tested=f"weight moved toward {v.variant_id}" + (" and promoted to active" if promoted else ""),
                    rationale=v.rationale or "", variant_id=v.variant_id,
                ))
                db.commit()
            else:
                v.status = "paused"
                v.weight = 0.0
                v.notes = f"{(v.notes + ' | ') if v.notes else ''}Paused {now.strftime('%Y-%m-%d')}: confirmed underperformer ({finding_text})"
                db.commit()
                actions.append({"type": "pause_underperformer", "stage": stage, "variant_id": v.variant_id, "reason": finding_text})
                db.add(EvidenceFinding(
                    stage=stage, finding=f"{finding_text} — {v.variant_id} significantly underperforms, paused",
                    sample_size=v_perf["sent"] + best_perf["sent"], isolated_variable=v.isolated_variable,
                    adaptation_tested="paused, not promoted", rationale=v.rationale or "", variant_id=v.variant_id,
                ))
                db.commit()

    # ── Queue one new candidate request, if there's room and nothing
    # already in flight (canary awaiting judgment, or a request already
    # sent to the daily routine and not yet resolved). ──
    variants = db.query(EmailVariant).filter(EmailVariant.stage == stage).all()
    unresolved = any(v.status in ("canary", "pending_generation") for v in variants)
    if not unresolved and len(variants) < MAX_VARIANTS_PER_STAGE:
        parent = max(
            [v for v in variants if v.status == "active"], key=lambda v: v.weight, default=None
        )
        if parent:
            isolated_variable = _least_explored_isolated_variable(db, stage)
            _queue_generation_request(db, stage, parent, isolated_variable, now, actions)
    elif unresolved:
        logger.info("Stage %r: a canary or pending generation request already exists — not queuing another this run", stage)
    else:
        logger.info("Stage %r: at MAX_VARIANTS_PER_STAGE (%d) — not queuing another", stage, MAX_VARIANTS_PER_STAGE)

    state.last_processed_touch_id = max_id_seen
    state.updated_at = now
    db.commit()
    return new_count


def run_variant_optimizer(now=None, dry_run=False):
    now = now or datetime.utcnow()
    init_db()
    db = SessionLocal()
    total_new_samples = 0
    all_actions = []
    try:
        seed_baseline_variants(db)
        if dry_run:
            logger.info("[dry-run] would check for Drive results and evaluate stages: %s", STAGES)
            return {"dry_run": True}

        _pickup_candidate_results(db, now, all_actions)

        for stage in STAGES:
            try:
                total_new_samples += _process_stage(db, stage, now, all_actions)
            except Exception:
                logger.exception("Error processing stage %r — continuing with remaining stages", stage)
                all_actions.append({"type": "error", "stage": stage, "reason": "unhandled exception, see logs"})

        action_taken = "action_taken" if all_actions else "no_action_threshold_not_met"
        db.add(OptimizerRunLog(
            run_at=now, samples_processed=total_new_samples, action_taken=action_taken,
            details={"actions": all_actions, "stages_evaluated": STAGES},
        ))
        db.commit()

        print("")
        print("=" * 56)
        print("Variant optimizer run summary")
        print("-" * 56)
        print(f"  New mature samples this run: {total_new_samples}")
        print(f"  Actions taken: {len(all_actions)}")
        for a in all_actions:
            print(f"    - [{a.get('stage')}] {a.get('type')}: {a.get('variant_id', '')} — {a.get('reason', '')}")
        print("=" * 56)
        print("")
    finally:
        db.close()

    return {"samples_processed": total_new_samples, "actions": all_actions}


def main():
    parser = argparse.ArgumentParser(description="Groundwork autonomous email-variant testing job")
    parser.add_argument("--dry-run", action="store_true", help="log what would be evaluated without writing data")
    args = parser.parse_args()
    run_variant_optimizer(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
