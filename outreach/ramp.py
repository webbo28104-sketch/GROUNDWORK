"""
Groundwork outreach — dynamic send ramp + circuit-breakers
(docs/outreach-pipeline-spec.md Section 15).

REAL STATE, CHECKED BEFORE WRITING THIS FILE:
  - Google Postmaster Tools API access: NOT wired up anywhere in this
    codebase. No credentials, no client, no polling job.
  - Twilio delivery-receipt data: NOT wired up anywhere in this codebase.
    No StatusCallback URL is registered on any Twilio send, so Twilio has
    nowhere to report delivery outcomes back to.
  - Resend bounce/complaint webhooks (the interim proxy Section 15 names
    for email): NOT wired up either — no /api/webhooks/resend-events route
    exists.

get_health_signal() below is the single seam where all of that would
plug in. Until it's wired, it always returns None ("unknown"), and the
ramp deliberately HOLDS FLAT rather than advancing on missing data —
advancing a send-volume ramp on fabricated health data would be worse than
not advancing at all. Wiring Postmaster/Twilio/Resend-events is a
prerequisite for this module ever doing anything beyond "stay at the
week-1 floor," not a nice-to-have.
"""
import os
import logging
from datetime import datetime, timedelta

from models import SessionLocal, RampState, DailySendCount

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


def get_health_signal(channel):
    """
    Returns a dict describing this channel's health, or None if unknown.

    NOT WIRED to any real data source (see module docstring) — always
    returns None right now. Structured this way (single function, clear
    return contract) so plugging in Postmaster Tools / Twilio / Resend
    events later is a one-function change, not a rewrite of this module.

    Expected shape once wired:
      email: {"spam_rate": float}
      sms:   {"delivery_rate": float, "delivery_rate_baseline": float,
              "opt_out_rate_today": float}
    """
    return None


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
        signal = get_health_signal(channel)
        state.last_checked_at = now

        if signal is None:
            logger.warning(
                "ramp[%s]: health signal unknown (Postmaster/Twilio/Resend-events not wired) "
                "— holding at %d/day, NOT advancing", channel, state.daily_volume
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
