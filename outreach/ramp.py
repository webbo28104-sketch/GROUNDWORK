"""
Groundwork outreach — dynamic send ramp + circuit-breakers
(docs/outreach-pipeline-spec.md Section 15).

REAL STATE:
  - Twilio delivery-receipt data: WIRED. outreach/sms.py registers a
    status_callback on every send; app.py:sms_status_webhook logs each
    callback to SmsDeliveryEvent (Twilio-signature-verified).
  - Resend bounce/complaint webhooks: WIRED. app.py:resend_events_webhook
    (Svix-signature-verified) logs each event to EmailEventLog. Requires a
    webhook actually registered in the Resend dashboard pointing at
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

from models import SessionLocal, RampState, DailySendCount, SmsDeliveryEvent, EmailEventLog, Prospect

logger = logging.getLogger("outreach.ramp")

# Section 15 tables — daily volume by week number for each channel.
EMAIL_RAMP_TABLE = {1: 10, 2: 25, 3: 50}  # week 4+ doubles the prior week
SMS_RAMP_TABLE = {1: 20, 2: 50}  # week 3+ increases 50-75% (use 50% — the conservative end)

EMAIL_FLOOR = EMAIL_RAMP_TABLE[1]
SMS_FLOOR = SMS_RAMP_TABLE[1]

EMAIL_SPAM_RATE_TRIGGER = 0.001  # 0.1%
SMS_DELIVERY_DROP_TRIGGER_PP = 10  # percentage points below prior-week baseline
SMS_OPT_OUT_SPIKE_TRIGGER = 0.02  # 2% in a single day


def _volume_for_week(table, week_number, floor):
    if week_number in table:
        return table[week_number]
    # Beyond the table's last defined week: email doubles each week past
    # week 3, SMS grows 50% each week past week 2.
    last_defined_week = max(table.keys())
    last_volume = table[last_defined_week]
    extra_weeks = week_number - last_defined_week
    growth = 2.0 if table is EMAIL_RAMP_TABLE else 1.5
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

    email: {"spam_rate": float} — complaints (EmailEventLog) / total sent
      (DailySendCount) over the trailing 7 days.
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
            if total_sent == 0:
                return None
            complaints = db.query(EmailEventLog).filter(
                EmailEventLog.event_type.in_(["email.complained", "complained"]),
                EmailEventLog.created_at >= week_ago,
                EmailEventLog.created_at <= now,
            ).count()
            return {"spam_rate": complaints / total_sent}

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

        if signal is None:
            logger.warning(
                "ramp[%s]: health signal unknown (not enough real send/event data yet in the "
                "trailing window) — holding at %d/day, NOT advancing", channel, state.daily_volume
            )
            db.commit()
            return state.daily_volume

        breached = False
        if channel == "email":
            breached = signal.get("spam_rate", 0) >= EMAIL_SPAM_RATE_TRIGGER
        else:
            drop = signal.get("delivery_rate_baseline", 0) - signal.get("delivery_rate", 0)
            breached = (drop * 100 >= SMS_DELIVERY_DROP_TRIGGER_PP
                        or signal.get("opt_out_rate_today", 0) >= SMS_OPT_OUT_SPIKE_TRIGGER)

        if breached:
            state.circuit_breaker_tripped = True
            state.circuit_breaker_tripped_at = now
            state.daily_volume = floor
            state.week_number = 1
            state.week_started_at = now
            logger.error("ramp[%s]: circuit breaker TRIPPED — reset to floor (%d/day)", channel, floor)
            db.commit()
            return state.daily_volume

        week_elapsed = (now - state.week_started_at) >= timedelta(days=7)
        if state.circuit_breaker_tripped:
            # Recovery requires sustained clean signal — advance_or_hold
            # doesn't track the "N consecutive clean days" count itself
            # (no historical signal storage exists yet to compute that from);
            # this is a known gap, flagged rather than faked.
            logger.warning(
                "ramp[%s]: circuit breaker previously tripped — recovery requires "
                "consecutive-clean-day tracking that isn't built yet. Holding at floor.",
                channel
            )
            db.commit()
            return state.daily_volume

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
    approved daily_volume minus what's already gone out today."""
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


def record_sends(channel, n, now=None):
    """Increment today's send counter for a channel by n. Call this after
    every actual send (follow-up or initial) so get_remaining_ramp_today
    reflects reality."""
    if n <= 0:
        return
    now = now or datetime.utcnow()
    today = now.strftime("%Y-%m-%d")

    db = SessionLocal()
    try:
        row = db.query(DailySendCount).filter(
            DailySendCount.channel == channel, DailySendCount.send_date == today
        ).first()
        if not row:
            row = DailySendCount(channel=channel, send_date=today, count=0)
            db.add(row)
        row.count += n
        db.commit()
    finally:
        db.close()
