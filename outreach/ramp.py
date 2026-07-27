"""
Groundwork outreach — dynamic send ramp + circuit-breakers
(docs/outreach-pipeline-spec.md Section 15).

REAL STATE:
  - SMS delivery-status data: WIRED, via Esendex (not Twilio — see
    outreach/sms.py's module docstring for the provider change and why
    this is a poll, not a push webhook). outreach/sms_status_poll.py logs
    to SmsDeliveryEvent the same way the old Twilio webhook did — this
    function's query logic below needed ZERO changes for the provider
    switch, since it only ever read from SmsDeliveryEvent, never from
    Twilio-specific fields.
  - Resend bounce/complaint webhooks: WIRED, and genuinely consumed here —
    app.py:resend_events_webhook (Svix-signature-verified) logs each event
    to EmailEventLog. get_health_signal("email") tracks bounce_rate and
    complaint_rate as SEPARATE numerators (previously folded into one
    "spam_rate" at the same 0.1% trigger — changed 2026-07-17 after that
    conflation tripped the breaker off 3 dead-domain bounces in the first
    10-email batch, a data-quality signal, not a reputation one; see
    EMAIL_BOUNCE_RATE_TRIGGER's comment below). Requires the webhook
    actually registered in the Resend dashboard pointing at
    /api/webhooks/resend-events, and RESEND_WEBHOOK_SECRET set to match —
    infrastructure-side steps outside this codebase, same category as the
    Cloudflare Email Routing rule in Section 11a.
  - Google Postmaster Tools API access: still NOT wired — needs manual
    domain verification in the Postmaster dashboard first (a human step,
    not a code change). Not used below; email health is computed entirely
    from the Resend interim proxy Section 15 already names for this.

get_health_signal() returns None ("unknown") whenever there isn't yet
enough real data to compute a rate from (e.g. zero sends logged in the
window) — the ramp deliberately HOLDS FLAT on unknown, rather than
advancing on fabricated/absent data. Real volume needs to actually flow
through the wired webhooks above before this stops returning None.
"""
import os
import logging
from datetime import datetime, timedelta

from sqlalchemy import func

from models import SessionLocal, RampState, DailySendCount, HourlySendCount, SmsDeliveryEvent, EmailEventLog, Prospect

logger = logging.getLogger("outreach.ramp")

# Section 15 tables — SMS ramp is still a daily volume by week number,
# unchanged. Email switched to an HOURLY volume on 2026-07-21, by request:
# instead of one big daily batch at a fixed time, email now sends across an
# 08:00-19:00 UTC window (EMAIL_SEND_WINDOW_START_HOUR/END_HOUR_EXCLUSIVE
# below) in even hourly slices, so we build our own data on which sending
# hours actually perform best rather than guessing. The genuine volume
# limiter is meant to be sourcing-cron (free to run as often as we like),
# NOT an artificial per-hour cap much lower than what we could safely send —
# so this table ramps the SAME conservative way the old daily table did
# (week 1 floor, then steady growth), just denominated per-hour instead of
# per-day. Week 1 floor is 42/hour x 12 hourly slots = 504/day to start.
#
# Floor raised 5 -> 42 on 2026-07-21, an explicit override to clear the
# ~464-prospect awaiting_approval backlog in one day ("500 spread over each
# hour tomorrow"), made WHILE the email circuit breaker was tripped
# (bounce_rate ~7.2% over the trailing 7 days vs. the 5% trigger, tripped
# 2026-07-19, not yet recovered). Flagging this explicitly because of how
# advance_or_hold() works while tripped: daily_volume is unconditionally
# re-clamped to EMAIL_FLOOR (this table's week-1 value) on every check, so
# there is no clean "just for one day" lever here — raising the floor is
# the only way to get above-floor volume while tripped, which means this
# is a real, persisting floor change (not a one-off), same as the earlier
# 10->20 floor raise this session. It stays in effect until the breaker
# clears (7 consecutive clean days) or someone lowers it back down.
EMAIL_HOURLY_RAMP_TABLE = {1: 42, 2: 67, 3: 101, 4: 151}  # week 5+ grows 50%/week (see _volume_for_week)
SMS_RAMP_TABLE = {1: 20, 2: 50}  # week 3+ increases 50-75% (use 50% — the conservative end)

# Back-compat alias — RampState/advance_or_hold's table lookup below is
# channel-generic and just needs *a* dict to key week_number into; this name
# is kept only because "EMAIL_RAMP_TABLE" is a more obvious symbol to grep
# for than the newer, more precise EMAIL_HOURLY_RAMP_TABLE.
EMAIL_RAMP_TABLE = EMAIL_HOURLY_RAMP_TABLE

EMAIL_FLOOR = EMAIL_RAMP_TABLE[1]
SMS_FLOOR = SMS_RAMP_TABLE[1]

# Email send window — no sends (initial or follow-up) fire on the email
# channel outside these hours, UTC. Widened 03:00-19:00 UTC/BST business-
# hours only) -> 03:00-22:00 UTC (04:00-23:00 BST) on 2026-07-21, by
# request, after two real clicks landed ~9-10pm BST — the point is
# specifically to gather real peak/trough click data across a much wider
# span of the day rather than assume a 9-5 business-hours pattern; 20
# hourly slots now instead of 12. send-job-cron's Railway cron schedule
# was updated to match ("0 3-22 * * *" UTC) — the window check here is a
# second, code-level belt-and-braces guard, not the only enforcement, but
# both need to agree or the cron simply won't invoke this code in the
# newly-added hours at all. 22 is exclusive, so this is 03:00 through
# 22:00 UTC inclusive-start.
# Start hour corrected 3 -> 4 (2026-07-27): send-job-cron's live Railway
# schedule is "*/15 4-22 * * *" — it has never actually fired during the
# 03:00 hour despite this code-level guard allowing it, so that hour's
# floor/weight allocation was being computed for a slot that could never
# send. Keep this in sync with the real cron schedule (checked via Railway,
# not assumed) rather than the other way around — the cron is what actually
# controls whether this code runs at all in a given hour.
EMAIL_SEND_WINDOW_START_HOUR = 4
EMAIL_SEND_WINDOW_END_HOUR_EXCLUSIVE = 23

# Send cadence (changed 2026-07-27, by request): email is now capped at a
# fixed EMAIL_DAILY_TOTAL for the whole day, split across 15-minute slots
# weighted by each slot's own real open/click engagement rate — instead of
# either one lump daily ramp figure (the original EMAIL_HOURLY_RAMP_TABLE
# design) or a flat per-slot cap applied evenly regardless of time of day
# (the 2026-07-23 fixed-5-per-slot design this replaces). Every slot still
# gets at least EMAIL_SLOT_FLOOR sends regardless of how it's performed —
# by request, so we keep collecting real data across the whole window
# rather than the engagement weighting starving a slot down to zero and
# making it permanently unable to prove itself. See _slot_plan() below for
# the actual allocation.
EMAIL_SLOT_MINUTES = 15
EMAIL_DAILY_TOTAL = 192
EMAIL_SLOT_FLOOR = 1
# A slot needs at least this many of its own real sends before its own
# engagement rate is trusted over the whole-window average — same principle
# as MIN_EMAIL_SAMPLE_SIZE below, just scoped to one slot instead of the
# whole channel.
MIN_SLOT_SAMPLE_SIZE = 10
# A slot counts as "top tier" (used to decide whether a priority prospect —
# see PRIORITY_SCORE_THRESHOLD — sends now or waits for a better slot) if
# its weight is at/above this percentile of today's slot weights.
PRIORITY_TOP_TIER_PERCENTILE = 0.75
# A score in this range from a genuinely human-sighted email (see
# outreach/send_job.py's _is_priority_prospect) is treated as too valuable
# to burn on a middling slot — held for the next top-tier slot instead of
# sent in strict arrival order. Never held indefinitely: after this long,
# send anyway rather than let a real, ready lead go stale chasing a
# statistically-better hour that may not come today.
PRIORITY_SCORE_THRESHOLD = 95
PRIORITY_MAX_HOLD = timedelta(hours=24)

EMAIL_SPAM_RATE_TRIGGER = 0.001  # 0.1% — genuine spam complaints only, per Section 15.
# Bounces are tracked and trip the breaker separately from complaints (added
# 2026-07-17). They used to be folded into the same "spam_rate" as
# complaints, at the same 0.1% trigger — which meant 3 dead-domain bounces
# out of the first 10-email batch (a data-quality problem: bad addresses
# from AI-assisted discovery, not a reputation problem) tripped the breaker
# on a sample of 10. A hard bounce is still a real deliverability signal
# (ISPs do weight it), just a much noisier and less severe one than a spam
# complaint at small volume, so it gets its own, higher threshold — in line
# with typical ESP guidance (under 2% is healthy, 5%+ is a real problem).
EMAIL_BOUNCE_RATE_TRIGGER = 0.05  # 5%
# Neither rate is evaluated below this much real volume in the trailing
# window — same principle as Section 5b's 30-outcome minimum for trusting a
# per-factor rate over noise. Below this, get_health_signal returns None and
# the ramp holds flat (its existing behavior for "not enough data").
MIN_EMAIL_SAMPLE_SIZE = 30
# How many consecutive daily checks with a clean signal (both rates under
# trigger, real sample size) are required to clear a tripped breaker and
# resume ramping from the floor — Section 15's "7 consecutive days" rule.
CIRCUIT_BREAKER_RECOVERY_DAYS = 7

SMS_DELIVERY_DROP_TRIGGER_PP = 10  # percentage points below prior-week baseline
SMS_OPT_OUT_SPIKE_TRIGGER = 0.02  # 2% in a single day


def _volume_for_week(table, week_number, floor):
    if week_number in table:
        return table[week_number]
    # Beyond the table's last defined week: both channels grow 50% each
    # week past their last defined entry. Email used to double (when the
    # table was a per-day figure) — since it's now per-HOUR across 12
    # hourly slots (2026-07-21), doubling would compound too fast (a
    # doubled per-hour rate is really a 2x per-DAY jump given the same
    # number of slots), so it uses the same conservative 50%/week growth
    # SMS already used.
    last_defined_week = max(table.keys())
    last_volume = table[last_defined_week]
    extra_weeks = week_number - last_defined_week
    growth = 1.5
    return int(last_volume * (growth ** extra_weeks))


def _total_sent(db, channel, start, end):
    """Total sends of this channel recorded in DailySendCount within
    [start, end] inclusive — the denominator for a rate. Dates are compared
    as 'YYYY-MM-DD' strings, same format DailySendCount stores (day
    granularity, so the end date itself must be included, not excluded —
    a caller passing today as `end` means "through today")."""
    rows = db.query(DailySendCount).filter(
        DailySendCount.channel == channel,
        DailySendCount.send_date >= start.strftime("%Y-%m-%d"),
        DailySendCount.send_date <= end.strftime("%Y-%m-%d"),
    ).all()
    return sum(r.count for r in rows)


def get_health_signal(channel, now=None):
    """
    Returns a dict describing this channel's health, or None if there isn't
    yet enough real data in the trailing window to compute a rate from.

    email: {"bounce_rate": float, "complaint_rate": float, "sample_size": int}
      — each independently, over the trailing 7 days (EmailEventLog /
      DailySendCount). Returns None if fewer than MIN_EMAIL_SAMPLE_SIZE
      sends happened in the window, not just "zero" — a 3-bounce sample of
      10 sends is noise, not a signal (see MIN_EMAIL_SAMPLE_SIZE's
      docstring above for the incident that prompted this).
    sms: {"delivery_rate": float, "delivery_rate_baseline": float,
      "opt_out_rate_today": float} — delivered/total distinct message_sids
      (SmsDeliveryEvent) for the trailing 7 days vs. the 7 days before
      that (the "baseline" the circuit-breaker trigger compares against,
      per Section 15/10b), plus today's opt-outs / today's sends.
    """
    now = now or datetime.utcnow()
    db = SessionLocal()
    try:
        if channel == "email":
            week_ago = now - timedelta(days=7)
            total_sent = _total_sent(db, "email", week_ago, now)
            if total_sent < MIN_EMAIL_SAMPLE_SIZE:
                return None
            bounced = db.query(EmailEventLog).filter(
                EmailEventLog.event_type.in_(["email.bounced", "bounced"]),
                EmailEventLog.created_at >= week_ago,
                EmailEventLog.created_at <= now,
            ).count()
            complained = db.query(EmailEventLog).filter(
                EmailEventLog.event_type.in_(["email.complained", "complained"]),
                EmailEventLog.created_at >= week_ago,
                EmailEventLog.created_at <= now,
            ).count()
            return {
                "bounce_rate": bounced / total_sent,
                "complaint_rate": complained / total_sent,
                "sample_size": total_sent,
            }

        # sms
        def _delivery_rate(start, end):
            total = db.query(func.count(func.distinct(SmsDeliveryEvent.message_sid))).filter(
                SmsDeliveryEvent.created_at >= start, SmsDeliveryEvent.created_at <= end,
            ).scalar() or 0
            if total == 0:
                return None
            delivered = db.query(func.count(func.distinct(SmsDeliveryEvent.message_sid))).filter(
                SmsDeliveryEvent.created_at >= start, SmsDeliveryEvent.created_at <= end,
                SmsDeliveryEvent.status == "delivered",
            ).scalar() or 0
            return delivered / total

        week_ago = now - timedelta(days=7)
        two_weeks_ago = now - timedelta(days=14)
        current_rate = _delivery_rate(week_ago, now)
        baseline_rate = _delivery_rate(two_weeks_ago, week_ago)
        if current_rate is None or baseline_rate is None:
            # No baseline yet (e.g. still in week 1) — can't evaluate the
            # "dropped N points from baseline" trigger meaningfully.
            return None

        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        opt_outs_today = db.query(Prospect).filter(
            Prospect.sms_unsubscribed_at.isnot(None), Prospect.sms_unsubscribed_at >= today_start,
        ).count()
        sent_today = _total_sent(db, "sms", today_start, now)
        opt_out_rate_today = (opt_outs_today / sent_today) if sent_today else 0.0

        return {
            "delivery_rate": current_rate,
            "delivery_rate_baseline": baseline_rate,
            "opt_out_rate_today": opt_out_rate_today,
        }
    finally:
        db.close()


def _get_or_create_state(db, channel):
    state = db.query(RampState).filter(RampState.channel == channel).first()
    if not state:
        floor = EMAIL_FLOOR if channel == "email" else SMS_FLOOR
        state = RampState(channel=channel, daily_volume=floor, week_number=1,
                           week_started_at=datetime.utcnow())
        db.add(state)
        db.commit()
    return state


def advance_or_hold(channel, now=None):
    """
    Nightly ramp check for one channel. Advances to the next week's volume
    if a full week has elapsed AND the health signal is clean; trips the
    circuit breaker and resets to the floor if the signal is bad; holds
    flat (logging why) if the signal is unknown or the week hasn't elapsed.
    """
    now = now or datetime.utcnow()
    table = EMAIL_RAMP_TABLE if channel == "email" else SMS_RAMP_TABLE
    floor = EMAIL_FLOOR if channel == "email" else SMS_FLOOR

    db = SessionLocal()
    try:
        state = _get_or_create_state(db, channel)
        signal = get_health_signal(channel, now)
        state.last_checked_at = now

        # Re-clamped to `floor` on every check while tripped, BEFORE the
        # signal==None early return below — see the long comment further
        # down at "if state.circuit_breaker_tripped" for why this exists
        # (a row tripped before floor's meaning changed, per-day -> per-
        # hour for email, stayed frozen at its old, much larger value
        # forever). Doing it here too, not just in that later branch,
        # matters because a quiet/no-data day hits the `signal is None`
        # early return below and would otherwise skip the clamp entirely —
        # exactly the case that let a stale 150 "per-hour" cap survive
        # past this fix's first version and send ~140 emails in one hour.
        if state.circuit_breaker_tripped:
            state.daily_volume = floor

        if signal is None:
            logger.warning(
                "ramp[%s]: health signal unknown (not enough real send/event data yet in the "
                "trailing window) — holding at %d/day, NOT advancing", channel, state.daily_volume
            )
            db.commit()
            return state.daily_volume

        breached = False
        breach_reason = None
        if channel == "email":
            if signal.get("complaint_rate", 0) >= EMAIL_SPAM_RATE_TRIGGER:
                breached = True
                breach_reason = f'complaint_rate {signal["complaint_rate"] * 100:.3f}% >= {EMAIL_SPAM_RATE_TRIGGER * 100:.3f}%'
            elif signal.get("bounce_rate", 0) >= EMAIL_BOUNCE_RATE_TRIGGER:
                breached = True
                breach_reason = f'bounce_rate {signal["bounce_rate"] * 100:.1f}% >= {EMAIL_BOUNCE_RATE_TRIGGER * 100:.0f}%'
        else:
            drop = signal.get("delivery_rate_baseline", 0) - signal.get("delivery_rate", 0)
            if drop * 100 >= SMS_DELIVERY_DROP_TRIGGER_PP:
                breached = True
                breach_reason = f"delivery rate dropped {drop * 100:.1f}pp vs baseline"
            elif signal.get("opt_out_rate_today", 0) >= SMS_OPT_OUT_SPIKE_TRIGGER:
                breached = True
                breach_reason = f'opt_out_rate_today {signal["opt_out_rate_today"] * 100:.1f}% >= {SMS_OPT_OUT_SPIKE_TRIGGER * 100:.0f}%'

        if state.circuit_breaker_tripped:
            # Already tripped — evaluate today's signal toward recovery
            # rather than re-tripping (it's already at the floor — clamped
            # unconditionally near the top of this function, before this
            # point, on every check including the signal==None early
            # return; see that comment for the incident this fixed). A
            # clean day advances the consecutive-day counter; a breach
            # resets it; unknown/insufficient data neither advances nor
            # resets it (matches the existing "hold flat on missing data"
            # principle — a quiet week shouldn't either fast-track or
            # penalize recovery).
            if breached:
                state.consecutive_clean_days = 0
                logger.error("ramp[%s]: still breached while tripped (%s) — consecutive clean days reset to 0",
                             channel, breach_reason)
            else:
                state.consecutive_clean_days = (state.consecutive_clean_days or 0) + 1
                logger.info("ramp[%s]: clean day while tripped — %d/%d consecutive",
                            channel, state.consecutive_clean_days, CIRCUIT_BREAKER_RECOVERY_DAYS)
                if state.consecutive_clean_days >= CIRCUIT_BREAKER_RECOVERY_DAYS:
                    state.circuit_breaker_tripped = False
                    state.circuit_breaker_tripped_at = None
                    state.consecutive_clean_days = 0
                    state.daily_volume = floor
                    state.week_number = 1
                    state.week_started_at = now
                    logger.info("ramp[%s]: circuit breaker RECOVERED after %d consecutive clean days — "
                                "resuming ramp from floor (%d/day)", channel, CIRCUIT_BREAKER_RECOVERY_DAYS, floor)
            db.commit()
            return state.daily_volume

        if breached:
            state.circuit_breaker_tripped = True
            state.circuit_breaker_tripped_at = now
            state.consecutive_clean_days = 0
            state.daily_volume = floor
            state.week_number = 1
            state.week_started_at = now
            logger.error("ramp[%s]: circuit breaker TRIPPED (%s) — reset to floor (%d/day)",
                         channel, breach_reason, floor)
            db.commit()
            return state.daily_volume

        week_elapsed = (now - state.week_started_at) >= timedelta(days=7)
        if week_elapsed:
            state.week_number += 1
            state.daily_volume = _volume_for_week(table, state.week_number, floor)
            state.week_started_at = now
            logger.info("ramp[%s]: advanced to week %d — %d/day", channel, state.week_number, state.daily_volume)

        db.commit()
        return state.daily_volume
    finally:
        db.close()


def get_remaining_ramp_today(channel, now=None):
    """How many more sends of this channel are allowed today, i.e. today's
    approved daily_volume minus what's already gone out today. Still the
    real mechanism for SMS. For email, use get_remaining_ramp_this_hour
    instead — RampState.daily_volume for email is now a per-hour figure
    (see EMAIL_HOURLY_RAMP_TABLE), so this function would hugely
    overstate email's real remaining budget for the rest of the day."""
    now = now or datetime.utcnow()
    today = now.strftime("%Y-%m-%d")

    db = SessionLocal()
    try:
        state = _get_or_create_state(db, channel)
        row = db.query(DailySendCount).filter(
            DailySendCount.channel == channel, DailySendCount.send_date == today
        ).first()
        sent_today = row.count if row else 0
        return max(0, state.daily_volume - sent_today)
    finally:
        db.close()


def is_within_email_send_window(now=None):
    """True during the email send window — see
    EMAIL_SEND_WINDOW_START_HOUR/END_HOUR_EXCLUSIVE above. Outside this
    window, email initial sends and follow-ups are held entirely (not
    queued/delayed — the next hourly cron run inside the window picks up
    normally, same as any other hour's due check)."""
    now = now or datetime.utcnow()
    return EMAIL_SEND_WINDOW_START_HOUR <= now.hour < EMAIL_SEND_WINDOW_END_HOUR_EXCLUSIVE


def _slot_bucket(channel, now):
    """15-minute bucket key for email (e.g. '2026-07-23-14-2' = the third
    15-min slice of 14:00); hourly bucket for every other channel — SMS has
    no slot-level cap, so there's no reason to fragment its counter."""
    if channel == "email":
        slot = now.minute // EMAIL_SLOT_MINUTES
        return now.strftime("%Y-%m-%d-%H") + f"-{slot}"
    return now.strftime("%Y-%m-%d-%H")


def _slot_index(hour, minute):
    return hour * (60 // EMAIL_SLOT_MINUTES) + (minute // EMAIL_SLOT_MINUTES)


def _window_slot_indices():
    return [
        h * (60 // EMAIL_SLOT_MINUTES) + s
        for h in range(EMAIL_SEND_WINDOW_START_HOUR, EMAIL_SEND_WINDOW_END_HOUR_EXCLUSIVE)
        for s in range(60 // EMAIL_SLOT_MINUTES)
    ]


def _slot_plan(now=None):
    """Single source of truth for email's per-slot send cap AND for whether
    the current slot counts as "top tier" for priority-prospect holding
    (outreach/send_job.py) — one query, shared by both, so the two can never
    disagree about what "today's best slots" means.

    Cap: EMAIL_SLOT_FLOOR guaranteed per slot (so every hour keeps
    collecting real engagement data, never gets starved to zero), plus a
    share of (EMAIL_DAILY_TOTAL - floor*n_slots) proportional to this
    slot's own engagement rate (opened_at/clicked_at ever set, over all
    Prospect sends ever recorded in that slot) — falls back to the
    all-slots average rate for any slot that doesn't yet have
    MIN_SLOT_SAMPLE_SIZE real sends of its own, so early on (or for a
    rarely-hit slot) this degrades to roughly-even allocation rather than
    a confident-looking number built on noise, and naturally sharpens
    toward real peak/trough hours as more data comes in.

    Top tier: this slot's weight is at/above PRIORITY_TOP_TIER_PERCENTILE
    of today's full set of slot weights.
    """
    now = now or datetime.utcnow()
    window_indices = _window_slot_indices()

    db = SessionLocal()
    try:
        rows = db.query(
            Prospect.sent_at_hour, Prospect.sent_at_slot, Prospect.opened_at, Prospect.clicked_at
        ).filter(
            Prospect.sent_at.isnot(None),
            Prospect.sent_at_hour.isnot(None),
            Prospect.sent_at_slot.isnot(None),
        ).all()
    finally:
        db.close()

    per_slot = {}
    for hour, slot, opened, clicked in rows:
        idx = _slot_index(hour, slot * EMAIL_SLOT_MINUTES)
        counts = per_slot.setdefault(idx, [0, 0])  # [sent, engaged]
        counts[0] += 1
        if opened is not None or clicked is not None:
            counts[1] += 1

    total_sent = sum(c[0] for c in per_slot.values())
    total_engaged = sum(c[1] for c in per_slot.values())
    # No real data anywhere yet -> neutral weight (1.0 for everyone) so the
    # allocation is a plain even split until real engagement data exists.
    overall_rate = (total_engaged / total_sent) if total_sent else 1.0

    def _weight(idx):
        counts = per_slot.get(idx)
        if counts and counts[0] >= MIN_SLOT_SAMPLE_SIZE:
            return counts[1] / counts[0]
        return overall_rate

    weights = {idx: _weight(idx) for idx in window_indices}
    total_weight = sum(weights.values()) or 1.0
    n_slots = len(window_indices) or 1
    bonus_pool = max(0, EMAIL_DAILY_TOTAL - EMAIL_SLOT_FLOOR * n_slots)

    # Largest-remainder apportionment, not independent per-slot round() —
    # rounding each slot's share separately can (and, verified, does: with
    # no engagement data yet every slot ties on the same fractional share,
    # so every single one rounds the same direction) drift the whole day's
    # total away from EMAIL_DAILY_TOTAL by several slots' worth. This
    # guarantees sum(caps) == EMAIL_DAILY_TOTAL exactly: give every slot
    # its floor() share, then hand the few leftover units (bonus_pool -
    # sum of floors) one each to the slots with the largest fractional
    # remainder, highest first.
    raw_shares = {idx: bonus_pool * weights[idx] / total_weight for idx in window_indices}
    caps = {idx: int(raw_shares[idx]) for idx in window_indices}
    leftover = bonus_pool - sum(caps.values())
    if leftover > 0:
        by_remainder = sorted(window_indices, key=lambda idx: raw_shares[idx] - caps[idx], reverse=True)
        for idx in by_remainder[:leftover]:
            caps[idx] += 1
    caps = {idx: EMAIL_SLOT_FLOOR + caps[idx] for idx in window_indices}

    sorted_weights = sorted(weights.values())
    cutoff_pos = min(int(len(sorted_weights) * PRIORITY_TOP_TIER_PERCENTILE), len(sorted_weights) - 1)
    top_tier_cutoff = sorted_weights[cutoff_pos] if sorted_weights else overall_rate

    this_idx = _slot_index(now.hour, now.minute)
    this_weight = weights.get(this_idx, overall_rate)

    return {
        "cap": caps.get(this_idx, EMAIL_SLOT_FLOOR),
        "is_top_tier": this_weight >= top_tier_cutoff,
    }


def is_top_engagement_slot_now(now=None):
    """True if the current 15-minute slot is one of today's best-performing
    sending times so far (see _slot_plan) — used by outreach/send_job.py to
    decide whether a priority prospect (score >= PRIORITY_SCORE_THRESHOLD,
    manually-sighted email) should send now or wait for a better slot."""
    if not is_within_email_send_window(now):
        return False
    return _slot_plan(now)["is_top_tier"]


def get_remaining_ramp_this_hour(channel, now=None):
    """How many more sends of this channel are allowed right now. For
    email, this is the engagement-weighted per-slot cap from _slot_plan
    (out of EMAIL_DAILY_TOTAL/day total — see EMAIL_SLOT_MINUTES above),
    forced to 0 if email's circuit breaker is currently tripped regardless
    of the computed cap (a bounce/complaint spike should actually stop
    sends, not just reset a ramp-table number nothing else reads). For
    every other channel it's unchanged (RampState.daily_volume minus this
    hour's HourlySendCount bucket). Returns 0 outside the email send window
    regardless of remaining budget."""
    now = now or datetime.utcnow()
    if channel == "email" and not is_within_email_send_window(now):
        return 0

    bucket = _slot_bucket(channel, now)
    db = SessionLocal()
    try:
        row = db.query(HourlySendCount).filter(
            HourlySendCount.channel == channel, HourlySendCount.hour_bucket == bucket
        ).first()
        sent_this_slot = row.count if row else 0
        if channel == "email":
            state = _get_or_create_state(db, "email")
            if state.circuit_breaker_tripped:
                return 0
            cap = _slot_plan(now)["cap"]
            return max(0, cap - sent_this_slot)
        state = _get_or_create_state(db, channel)
        return max(0, state.daily_volume - sent_this_slot)
    finally:
        db.close()


def record_sends(channel, n, now=None, db=None):
    """Increment today's send counter for a channel by n, AND this hour's
    bucket (used only by get_remaining_ramp_this_hour, currently just
    email — harmless to track for SMS too). Call this after every actual
    send (follow-up or initial) so the remaining-ramp checks reflect
    reality.

    Accepts an optional existing db session — every real caller
    (outreach/send_job.py, outreach/followup.py) already holds one open
    for the prospect it's touching, and opening a second SessionLocal()
    here while that one is mid-transaction caused real "database is
    locked" failures under SQLite (found while testing outreach/send_test.py;
    reproducible, not a fluke). Falls back to managing its own session only
    when called standalone (e.g. directly from a shell/test)."""
    if n <= 0:
        return
    now = now or datetime.utcnow()
    today = now.strftime("%Y-%m-%d")
    hour_bucket = _slot_bucket(channel, now)

    owns_session = db is None
    db = db or SessionLocal()
    try:
        row = db.query(DailySendCount).filter(
            DailySendCount.channel == channel, DailySendCount.send_date == today
        ).first()
        if not row:
            row = DailySendCount(channel=channel, send_date=today, count=0)
            db.add(row)
        row.count += n

        hour_row = db.query(HourlySendCount).filter(
            HourlySendCount.channel == channel, HourlySendCount.hour_bucket == hour_bucket
        ).first()
        if not hour_row:
            hour_row = HourlySendCount(channel=channel, hour_bucket=hour_bucket, count=0)
            db.add(hour_row)
        hour_row.count += n

        db.commit()
    finally:
        if owns_session:
            db.close()
