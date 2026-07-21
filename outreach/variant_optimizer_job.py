"""
Groundwork outreach — autonomous email-variant testing job
(docs/outreach-pipeline-spec.md Section 19, docs/cold-email-evidence-library.md).

Runnable as a module or script from the project root:
    python outreach/variant_optimizer_job.py
    python -m outreach.variant_optimizer_job --dry-run

Same architecture as the other outreach cron jobs (send_job.py,
domain_billing.py, email_discovery_job.py, pipeline.py) — a standalone
Python script pointed at by its own Railway Cron service, real
ANTHROPIC_API_KEY, direct DB access. Cowork and scheduled Claude Code
routines are both confirmed blocked from writing to production (see
CLAUDE.md's outreach-pipeline pointer section) — this cannot be either of
those.

THRESHOLD-GATED, NOT CALENDAR-GATED: intended to run hourly, but does
nothing meaningful until a stage has accumulated MIN_SAMPLE_SIZE new,
outcome-mature touches since the last time that stage was acted on
(VariantOptimizerState.last_processed_touch_id). At ~60 sends/day this
naturally no-ops almost every run; at ~8,000/month it naturally becomes
responsive within a run or two — no manual retuning of the schedule as
volume scales.

GIT WRITE LIMITATION (important, read before assuming Section 3 entries land
in docs/cold-email-evidence-library.md): this job runs on an ephemeral
Railway Cron container with no git credentials wired — it cannot commit an
appended finding to that file in production, the same constraint already
documented for the nightly discovery routine. Findings are written to the
EvidenceFinding table instead (the real, current Section 3 — see that
file's architecture note and /admin/variants).

Per-run sequence, per stage (initial/A/B/C/D — hail_mary is not variant-
tested, see outreach/seed_variants.py):
  1. Count new, outcome-mature touches (sent_at at least OUTCOME_MATURITY_DAYS
     ago, so opens/clicks/payments have had time to happen) since this
     stage's last-processed marker. Below MIN_SAMPLE_SIZE: no-op, log why.
  2. Otherwise: compute per-variant open/click/paid rates, run a two-
     proportion z-test for each canary against the current best active
     variant on that stage's primary metric (click for initial/A/B — the
     magic-link click IS the conversion event pre-payment; paid for C/D,
     since those cohorts have already clicked by definition).
  3. Confirmed winners get weight moved gradually toward the active pool
     (not an abrupt swap) and are promoted to "active" once they reach
     parity. Confirmed losers are paused. Any variant (regardless of
     significance testing) whose own bounce/complaint rate breaches the
     same thresholds outreach/ramp.py uses for the whole channel is
     auto-paused immediately — a deliverability safety action, independent
     of performance.
  4. Every significant result gets an EvidenceFinding row.
  5. If there's no existing unresolved canary for the stage and it's under
     MAX_VARIANTS_PER_STAGE, generate exactly one new candidate — citing
     either a Section 1 principle or a Section 3/EvidenceFinding entry,
     isolating exactly one variable (outreach/content_safety.py's isolation
     heuristic + all three content-safety gates must pass) — admitted at a
     weight equivalent to ~10% allocation, never a full swap.
  6. Always writes one OptimizerRunLog row, whether or not anything happened.

Environment: DATABASE_URL, ANTHROPIC_API_KEY.
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

import anthropic
from sqlalchemy import func

from models import (  # noqa: E402
    SessionLocal, OutreachTouch, Prospect, EmailEventLog, EmailVariant,
    EvidenceFinding, OptimizerRunLog, VariantOptimizerState, init_db,
)
from outreach.seed_variants import seed_baseline_variants
from outreach.stats_utils import two_proportion_z_test, is_significant
from outreach.content_safety import run_content_safety_gates, check_isolation
from outreach.ramp import EMAIL_BOUNCE_RATE_TRIGGER, EMAIL_SPAM_RATE_TRIGGER

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

# Section 5b's existing standard, reused here per the build spec.
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

_MODEL = "claude-sonnet-4-6"


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


def _read_evidence_library():
    path = os.path.join(_PROJECT_ROOT, "docs", "cold-email-evidence-library.md")
    try:
        with open(path, "r") as f:
            return f.read()
    except OSError:
        logger.error("Could not read %s — candidate generation cannot cite the evidence library", path)
        return ""


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


def _generate_candidate(db, stage, parent, isolated_variable, recent_findings):
    """Calls the Anthropic API for exactly one candidate variant. Returns
    dict {subject, body, rationale, cites} or None on any failure (API
    error, malformed response) — a failure here just means "try again next
    run," never a crash of the whole job."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        logger.error("ANTHROPIC_API_KEY not set — cannot generate a new candidate variant this run")
        return None

    evidence_md = _read_evidence_library()
    findings_text = "\n".join(
        f"- [{f.stage}] {f.finding} (sample size {f.sample_size}, isolated variable: {f.isolated_variable})"
        for f in recent_findings
    ) or "(no internal findings logged yet for this stage)"

    prompt = f"""You are generating exactly ONE new cold-outreach email copy variant for Groundwork, a UK company that builds websites for trade businesses.

Below is the full evidence library that must ground every change you make. Section 2 OVERRIDES Section 1 on any conflict.

{evidence_md}

Recent internal findings already logged for stage '{stage}':
{findings_text}

The PARENT variant you must change (stage '{stage}', variant_id '{parent.variant_id}') is this exact subject and HTML body:

SUBJECT:
{parent.subject}

BODY:
{parent.body}

Your task: produce ONE new candidate that changes EXACTLY ONE thing vs. the parent above — the isolated variable you must change is: '{isolated_variable}'. Do not change anything else — not the CTA wording, not the paragraph structure, not the personalization depth, not the tone — unless '{isolated_variable}' IS that thing. Cite either a Section 1 principle or one of the internal findings above in your rationale.

Hard requirements, non-negotiable:
- Preserve every {{placeholder}} token exactly as it appears in the parent (e.g. {{business_name}}, {{preview_link}}, {{unsubscribe_link}}{', {{branding_ps}}' if '{branding_ps}' in parent.body else ''}) — same set, same names, no new ones, none removed. Do not introduce any other literal curly brace {{ or }} character anywhere.
- Preserve the exact HTML table-based scaffold (this is an email client compatibility requirement, not a style choice) — only edit the specific text/structural element named by the isolated variable.
- Stage '{stage}' is {'pre-click — the site has NOT been generated yet, so the copy must never claim it already is (no "is built", "site is ready", etc.)' if stage in ('initial', 'A', 'B') else 'post-click — a real site has already been generated for this prospect, so it is accurate to say so'}.
- The only real prices are £99 (one-time setup) and £24.99 (monthly) — never invent a different figure.
- No spam-trigger phrases (bare "FREE" in caps, "act now", "100% free", excessive exclamation marks, etc).

Call the propose_variant tool with your answer."""

    client = anthropic.Anthropic(api_key=api_key)
    try:
        resp = client.messages.create(
            model=_MODEL,
            max_tokens=4096,
            tools=[{
                "name": "propose_variant",
                "description": "Propose one new email copy variant isolating exactly one changed element from its parent.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "subject": {"type": "string", "description": "The new subject line"},
                        "body": {"type": "string", "description": "The new full HTML body"},
                        "rationale": {"type": "string", "description": "Why this change, citing Section 1 or an internal finding"},
                        "cites": {"type": "string", "description": "The specific Section 1 principle or internal finding cited"},
                    },
                    "required": ["subject", "body", "rationale", "cites"],
                },
            }],
            tool_choice={"type": "tool", "name": "propose_variant"},
            messages=[{"role": "user", "content": prompt}],
        )
        tool_use = next((b for b in resp.content if b.type == "tool_use"), None)
        if not tool_use:
            logger.error("Anthropic response had no tool_use block for stage %r", stage)
            return None
        return dict(tool_use.input)
    except Exception:
        logger.exception("Anthropic API call failed while generating a candidate for stage %r", stage)
        return None


def _try_admit_candidate(db, stage, parent, isolated_variable, now, actions):
    findings = db.query(EvidenceFinding).filter(EvidenceFinding.stage == stage).order_by(
        EvidenceFinding.created_at.desc()
    ).limit(10).all()
    candidate = _generate_candidate(db, stage, parent, isolated_variable, findings)
    if not candidate:
        actions.append({"type": "generation_failed", "stage": stage, "reason": "API call failed or returned nothing usable"})
        return

    subject, body = candidate.get("subject", ""), candidate.get("body", "")
    if not subject or not body:
        actions.append({"type": "generation_rejected", "stage": stage, "reason": "empty subject or body"})
        return

    if _placeholders(subject) != _placeholders(parent.subject) or _placeholders(body) != _placeholders(parent.body):
        actions.append({"type": "generation_rejected", "stage": stage, "reason": "placeholder mismatch vs. parent — would crash at send time"})
        return

    passed, violations = run_content_safety_gates(stage, subject, body)
    if not passed:
        actions.append({"type": "generation_rejected", "stage": stage, "reason": f"content-safety gate failed: {violations}"})
        return

    iso_ok, iso_reason = check_isolation(isolated_variable, parent.subject, parent.body, subject, body)
    if not iso_ok:
        actions.append({"type": "generation_rejected", "stage": stage, "reason": f"isolation check failed: {iso_reason}"})
        return

    active_total_weight = sum(
        v.weight for v in db.query(EmailVariant).filter(
            EmailVariant.stage == stage, EmailVariant.status == "active"
        ).all()
    ) or 1.0
    canary_weight = active_total_weight * (CANARY_ALLOCATION_PCT / (1 - CANARY_ALLOCATION_PCT))

    existing_count = db.query(EmailVariant).filter(EmailVariant.stage == stage).count()
    new_variant_id = f"{stage}-v{existing_count + 1}"
    rationale = f"{candidate.get('rationale', '')} | cites: {candidate.get('cites', '')}"
    db.add(EmailVariant(
        stage=stage, variant_id=new_variant_id, parent_variant_id=parent.variant_id,
        subject=subject, body=body, status="canary", weight=canary_weight,
        rationale=rationale, isolated_variable=isolated_variable,
    ))
    db.commit()
    actions.append({
        "type": "new_variant", "stage": stage, "variant_id": new_variant_id,
        "reason": f"isolated_variable={isolated_variable}", "rationale": rationale,
    })
    logger.info("Admitted new candidate %s for stage %r (isolated_variable=%s)", new_variant_id, stage, isolated_variable)


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
    by_id = {v.variant_id: v for v in variants}

    active_variants = [v for v in variants if v.status == "active"]
    best_active = None
    if active_variants:
        best_active = max(
            active_variants,
            key=lambda v: (perf.get(v.variant_id, {}).get(metric, 0) / perf.get(v.variant_id, {"sent": 1})["sent"])
            if perf.get(v.variant_id, {}).get("sent", 0) else 0,
        )

    # ── Deliverability auto-pause — independent of performance testing ──
    for v in variants:
        if v.status == "paused":
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

    # ── Performance-based promotion / demotion of canaries ──
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

    # ── Generate one new candidate, if there's room and nothing unresolved ──
    variants = db.query(EmailVariant).filter(EmailVariant.stage == stage).all()
    unresolved_canary = any(v.status == "canary" for v in variants)
    if not unresolved_canary and len(variants) < MAX_VARIANTS_PER_STAGE:
        parent = max(
            [v for v in variants if v.status == "active"], key=lambda v: v.weight, default=None
        )
        if parent:
            isolated_variable = _least_explored_isolated_variable(db, stage)
            _try_admit_candidate(db, stage, parent, isolated_variable, now, actions)
    elif unresolved_canary:
        logger.info("Stage %r: an unresolved canary already exists — not generating another this run", stage)
    else:
        logger.info("Stage %r: at MAX_VARIANTS_PER_STAGE (%d) — not generating another", stage, MAX_VARIANTS_PER_STAGE)

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
            logger.info("[dry-run] would evaluate stages: %s", STAGES)
            return {"dry_run": True}

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
