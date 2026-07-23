"""
Groundwork — database models.

Three tables:
- Lead: one row per form submission, created before email verification.
  Holds the mapped form data (as JSON) plus logo path, so generation can be
  kicked off later from /verify/<token> without asking the user to resubmit.
  NOTE: email is NOT unique here — a person can resubmit, creating multiple
  Lead rows for the same email. Don't key account/identity state off Lead.
- Generation: one row per completed site generation. This is the durable
  source of truth for generated HTML — the verification/resend emails are
  just notifications pointing back at rows in this table.
- Account: one row per email that has (or is setting up) password login.
  Deliberately separate from Lead/Generation, which are keyed loosely by an
  email string with no uniqueness guarantee — Account is the one place email
  is unique, since a password is an account-level concept, not a per-
  submission one.
- GenerationImage: one row per embedded image slot (the logo, or one
  portfolio photo) for a Generation, keyed by `slot` ("logo", "photo_0",
  "photo_1", ...). Generation.html_content only ever stores the *final*
  HTML with each image's data URI already substituted in — there's no
  token marker left afterwards, so nothing in html_content says which
  embedded data URI belongs to which slot. This table is what makes an
  image swap possible without parsing/guessing at the HTML: replacing an
  image is an UPDATE on this row's data_uri, followed by a single
  known-exact-string replace() of the *old* data_uri (read from this same
  row) for the new one in html_content — never a blind regex over the HTML.
  Only populated for generations created after this table was added; older
  generations have no rows here (see CLAUDE.md) and aren't retroactively
  editable — the original uploaded files no longer exist to backfill from.
"""
import os
from datetime import datetime

from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, ForeignKey, JSON, Boolean, Float, UniqueConstraint, inspect, text
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

Base = declarative_base()


class Lead(Base):
    __tablename__ = "leads"

    id = Column(Integer, primary_key=True)
    public_id = Column(String(20), unique=True, nullable=False, index=True)
    email = Column(String(255), nullable=False, index=True)
    ip = Column(String(64))
    form_data = Column(JSON, nullable=False, default=dict)
    logo_path = Column(String(255))
    logo_mime = Column(String(100))
    status = Column(String(30), nullable=False, default="pending_verification")
    is_test = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    generations = relationship("Generation", back_populates="lead")


class Generation(Base):
    __tablename__ = "generations"

    id = Column(Integer, primary_key=True)
    lead_id = Column(Integer, ForeignKey("leads.id"), nullable=False)
    email = Column(String(255), nullable=False, index=True)
    business_name = Column(String(255))
    html_content = Column(Text, nullable=False)
    html_pending = Column(Text, nullable=True)   # pending edits from a live site's customer
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    status = Column(String(30), nullable=False, default="draft")
    stripe_customer_id = Column(String(255))
    stripe_setup_invoice_id = Column(String(255))
    stripe_subscription_id = Column(String(255), nullable=True, index=True)
    subdomain = Column(String(100), nullable=True, index=True)
    # Cancellation / churn instrumentation (added 2026-07-14, cancellation-flow build).
    # canceled_at is the actual churn timestamp — written by the
    # customer.subscription.deleted webhook, once the subscription is
    # genuinely gone (not merely scheduled to cancel).
    canceled_at = Column(DateTime, nullable=True)
    # cancel_at_period_end / current_period_end are written by
    # customer.subscription.updated, so the account page can show an
    # "ending on <date>" state between "customer clicked cancel" and the
    # subscription actually being deleted at period end.
    cancel_at_period_end = Column(Boolean, nullable=True, default=False)
    subscription_period_end = Column(DateTime, nullable=True)
    # Enforces the retention offer's one-per-customer limit — set the
    # moment the free-month coupon is applied, checked before allowing it
    # to be applied again.
    retention_offer_used_at = Column(DateTime, nullable=True)
    # Set by the charge.refunded webhook — a Stripe-side refund doesn't
    # cancel the subscription by itself (that's a separate dashboard/API
    # action), so this is a visibility flag for admins to act on, not an
    # automatic status change.
    refunded_at = Column(DateTime, nullable=True)
    # Mirrors Domain.is_internal (added 2026-07-19) — flags Groundwork's own
    # personal/test purchases (paid for real via Stripe while testing a
    # flow, not a real customer) so they don't inflate domain-conversion or
    # churn metrics on the admin dashboard. Not the same thing as
    # Lead.is_test, which only covers /admin/generate-test's bypass path —
    # these rows went through the real checkout flow, they just aren't a
    # real customer.
    is_internal = Column(Boolean, nullable=False, default=False)

    # View/engagement tracking (added 2026-07-18) — closes the "clicked the
    # magic link, then what?" gap. Bumped on every real serve of the
    # generated HTML (GET /api/generate/<id>/html — see app.py's
    # _record_generation_view), so this applies retroactively to every
    # existing generation the next time its link is opened, not just future
    # ones (the injected tracking script lives in _inject_watermark(), which
    # already dynamically rewrites stored HTML at serve time rather than
    # baking anything in — same mechanism the watermark itself uses).
    view_count = Column(Integer, nullable=False, default=0)
    first_viewed_at = Column(DateTime, nullable=True)
    last_viewed_at = Column(DateTime, nullable=True)
    # Cumulative seconds across every visit (sendBeacon reports a delta
    # since its last report, not total-since-load, so tab-switching in and
    # out doesn't double-count — see the injected script in _inject_watermark).
    total_view_seconds = Column(Integer, nullable=False, default=0)
    # Deepest scroll position ever recorded, all-time across every visit
    # (0-100) — not per-visit history, which would need its own table.
    max_scroll_pct = Column(Integer, nullable=False, default=0)

    # Claude API cost of producing this site (added 2026-07-23), estimated
    # from token usage x published per-token pricing (claude-sonnet-4-6:
    # $3/$15/$3.75/$0.30 per MTok for input/output/cache-write/cache-read —
    # see app.py's _run()) rather than pulled from a live Anthropic usage/cost
    # API, since this repo has no Anthropic Admin key with that access.
    generation_cost_usd = Column(Float, nullable=True)

    # Engagement/funnel instrumentation (added 2026-07-23) — both are
    # "first time this happened" stamps (set once, never overwritten), used
    # by the admin Funnel page to split "didn't convert" into distinct
    # populations rather than one undifferentiated bucket:
    #   - text_edited_at: this customer made a real text edit (pre- or
    #     post-launch — PATCH /api/generate/<id>/text) — a high edit rate
    #     with low conversion points at pricing/checkout friction (people
    #     who don't like the site don't usually bother personalizing it
    #     first); a low edit rate points at the site/wait failing to land.
    #     Can't be reconstructed retroactively — pre-launch edits overwrite
    #     html_content in place with no prior audit trail, so generations
    #     edited before this column existed will show as "no edit" even if
    #     they genuinely had one.
    #   - checkout_started_at: a Stripe Checkout Session was actually
    #     created for this generation (app.py's create_checkout_session) —
    #     distinguishes "started checkout, abandoned" (trust/checkout-
    #     friction problem) from "never attempted checkout at all"
    #     (upstream of pricing entirely, e.g. never even looked at pricing).
    text_edited_at = Column(DateTime, nullable=True)
    checkout_started_at = Column(DateTime, nullable=True)

    lead = relationship("Lead", back_populates="generations")


class Account(Base):
    __tablename__ = "accounts"

    id = Column(Integer, primary_key=True)
    email = Column(String(255), unique=True, nullable=False, index=True)
    password_hash = Column(String(255))  # nullable until the user sets a password
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class GenerationImage(Base):
    __tablename__ = "generation_images"

    id = Column(Integer, primary_key=True)
    generation_id = Column(Integer, ForeignKey("generations.id"), nullable=False, index=True)
    slot = Column(String(30), nullable=False)  # "logo", "photo_0", "photo_1", ...
    data_uri = Column(Text, nullable=False)
    mime = Column(String(100))
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    generation = relationship("Generation", back_populates="images")

    __table_args__ = (UniqueConstraint("generation_id", "slot", name="uq_generation_images_generation_slot"),)


Generation.images = relationship("GenerationImage", back_populates="generation", order_by="GenerationImage.slot")


class Domain(Base):
    __tablename__ = "domains"

    id = Column(Integer, primary_key=True)
    generation_id = Column(Integer, ForeignKey("generations.id"), nullable=True, index=True)
    domain = Column(String(255), unique=True, nullable=False, index=True)
    status = Column(String(50), nullable=False, default="pending")
    # statuses: pending, active, needs_manual_setup
    price_gbp = Column(Float)
    # wholesale_gbp/margin_gbp are snapshotted at purchase time (not recomputed
    # from the current TLD price table later), so historical margin stays
    # accurate even if pricing logic or Porkbun's wholesale prices change.
    wholesale_gbp = Column(Float, nullable=True)
    margin_gbp = Column(Float, nullable=True)
    stripe_payment_id = Column(String(255))
    customer_email = Column(String(255))
    registered_at = Column(DateTime, nullable=True)
    dns_configured_at = Column(DateTime, nullable=True)
    railway_connected_at = Column(DateTime, nullable=True)  # legacy, no longer written to
    cloudflare_connected_at = Column(DateTime, nullable=True)
    # Guards against ever sending "your domain is live" twice for the same
    # domain, independent of whatever caused a re-run (Stripe webhook retry,
    # manual reprocessing, a future retry-from-failed-step admin action).
    live_email_sent_at = Column(DateTime, nullable=True)
    error_step = Column(String(100), nullable=True)
    error_message = Column(Text, nullable=True)
    is_internal = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    # Yearly domain-renewal subscription (added 2026-07-14). Only populated
    # for domains purchased from this date forward — existing domains sold
    # as a one-time payment are deliberately grandfathered (None here) and
    # never migrated onto recurring billing without the customer's fresh
    # authorization.
    stripe_subscription_id = Column(String(255), nullable=True, index=True)
    # Guards the repricing job from re-running more than once for the same
    # renewal — set to the subscription's current_period_end (as of Stripe)
    # once that period's reprice has been applied, so "30 days before
    # renewal" only fires a single price update per year, not once per day
    # for the whole 30-day window.
    last_repriced_period_end = Column(DateTime, nullable=True)
    # Set by invoice.payment_failed on this domain's subscription, cleared
    # on the next successful invoice. Drives the grace-period-then-disable-
    # autorenew flow — see _domain_payment_failed_webhook handling.
    renewal_payment_failed_at = Column(DateTime, nullable=True)
    # Set by the charge.refunded webhook when the refunded charge's
    # payment_intent matches this domain's stripe_payment_id (the one-time
    # purchase charge — not the recurring renewal subscription).
    refunded_at = Column(DateTime, nullable=True)


class Prospect(Base):
    __tablename__ = "prospects"

    id = Column(Integer, primary_key=True)
    google_place_id = Column(String(255), unique=True, index=True)
    business_name = Column(String(255))
    trade = Column(String(100))
    trade_search_term = Column(String(100))
    trade_tier = Column(String(20))
    location = Column(String(255))
    postcode_area = Column(String(20))
    # ONS-region-derived income tier ("high"/"medium"/"low") of postcode_area,
    # from outreach.trade_categories.AREA_INCOME_TIER — set once at sourcing
    # time so click-through/conversion can be analysed against geographic
    # economic data without joining back to that lookup (which region names
    # map to which tier could reasonably change over time; this freezes the
    # tier that was actually in effect when the prospect was sourced).
    income_tier = Column(String(10), nullable=True)
    rating = Column(Float, nullable=True)
    review_count = Column(Integer, nullable=True)
    business_status = Column(String(50))
    phone = Column(String(50), nullable=True)
    website = Column(String(500), nullable=True)
    competitor_density = Column(Integer, nullable=True)
    latitude = Column(Float, nullable=True)
    longitude = Column(Float, nullable=True)
    types = Column(JSON, nullable=True)
    # Places API (New) Enterprise + Atmosphere tier fields, added 2026-07-23
    # (see outreach/sourcer.py's FIELD_MASK comment for the billing-tier
    # tradeoff — same 1,000 free-calls/month allowance as before, just
    # +$5/1000 over that allowance).
    primary_type = Column(String(100), nullable=True)  # Places' single best-guess category, e.g. "plumber"
    editorial_summary = Column(Text, nullable=True)  # Google's own written blurb, when present
    opening_hours = Column(JSON, nullable=True)  # weekdayDescriptions list, e.g. ["Monday: 9AM-5PM", ...]
    reviews = Column(JSON, nullable=True)  # up to 5 {author, rating, text, publish_time} dicts
    earliest_review_date = Column(DateTime, nullable=True)  # proxy for listing age — see sourcer.py's docstring
    google_photos_count = Column(Integer, nullable=True)
    opening_hours_complete = Column(Boolean, nullable=True)
    website_status = Column(String(30), nullable=True)
    # Free, code-only staleness heuristic for has_website prospects — see
    # outreach/site_quality.py. "modern" / "dated" / "unreachable" / null
    # (not yet checked). Computed once at sourcing time (outreach/pipeline.py
    # :_queue_pending), read by outreach/scorer.py — never fetched live at
    # score time, so score_prospect() stays a pure/sync function.
    website_quality = Column(String(20), nullable=True)
    vision_flag_layout = Column(Boolean, nullable=True)
    vision_flag_design = Column(Boolean, nullable=True)
    vision_flag_cta = Column(Boolean, nullable=True)
    vision_flag_content = Column(Boolean, nullable=True)
    vision_flag_reviews = Column(Boolean, nullable=True)
    vision_flag_load = Column(Boolean, nullable=True)
    score = Column(Float, nullable=True)
    email = Column(String(255), nullable=True)
    email_source = Column(String(50), nullable=True)
    email_domain_type = Column(String(20), nullable=True)
    email_found = Column(Boolean, default=False)
    funnel_stage = Column(String(50), default="sourced")
    funnel_substage = Column(String(30), nullable=True)
    last_touch_at = Column(DateTime, nullable=True)
    approval_status = Column(String(20), default="pending")
    approved_at = Column(DateTime, nullable=True)
    token = Column(String(100), unique=True, nullable=True)
    short_code = Column(String(12), unique=True, nullable=True, index=True)
    lead_id = Column(Integer, ForeignKey("leads.id"), nullable=True)
    account_created_at = Column(DateTime, nullable=True)
    screenshot_url = Column(String(500), nullable=True)
    gif_url = Column(String(500), nullable=True)
    email_version_sent = Column(String(50), nullable=True)
    sms_version_sent = Column(String(50), nullable=True)
    touch_count = Column(Integer, default=0)
    discount_code = Column(String(50), nullable=True)
    discount_expiry = Column(DateTime, nullable=True)
    sent_at = Column(DateTime, nullable=True)
    sent_at_dow = Column(Integer, nullable=True)
    sent_at_hour = Column(Integer, nullable=True)
    # Which 15-min slot of sent_at_hour this send fired in (0-3), added
    # 2026-07-23 alongside the send cadence change to 15-min slots (see
    # outreach/ramp.py's EMAIL_SLOT_MINUTES). NULL for every send before
    # that change — the admin send-timing chart filters on this being
    # non-null rather than using a hardcoded reliable-from date, since the
    # column's own NULL-ness already is the "no data yet" signal.
    sent_at_slot = Column(Integer, nullable=True)
    opened_at = Column(DateTime, nullable=True)
    clicked_at = Column(DateTime, nullable=True)
    paid_at = Column(DateTime, nullable=True)
    # Set once, at claim-click time, by _try_extract_prospect_assets (app.py)
    # for has_website_dated/has_website_modern/has_website prospects:
    # "full" (logo + photos pulled), "partial" (one of the two), "none"
    # (extraction ran but nothing usable came back). Stays NULL for
    # prospects extraction never ran for (no existing site, or hasn't
    # clicked yet) — distinct from "none", which means it ran and failed.
    extraction_quality = Column(String(10), nullable=True)
    sms_sent_at = Column(DateTime, nullable=True)
    sms_delivered = Column(Boolean, nullable=True)
    email_unsubscribed = Column(Boolean, default=False)
    sms_unsubscribed = Column(Boolean, default=False)
    email_unsubscribed_at = Column(DateTime, nullable=True)
    sms_unsubscribed_at = Column(DateTime, nullable=True)
    raw_data = Column(JSON, nullable=True)
    error_notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    processed_at = Column(DateTime, nullable=True)


class SurveyResponse(Base):
    """One row per completed post-generation survey (added 2026-07-17) —
    the "why did/didn't you go live" form offered to prospects who've
    clicked their magic link (a site exists) but haven't paid. Captures
    real, structured attributes the sourcing pipeline (Places API +
    scraping) has no way to see on its own — decision-maker, existing
    website spend, acquisition channel, timeline, and stated reason —
    exactly the "objectifiable needle movers" Section 5b's adaptive
    scoring loop needs more of. One response per prospect; the survey
    route is idempotent (repeat visits show the already-submitted state,
    same pattern as /claim/<token>)."""
    __tablename__ = "survey_responses"

    id = Column(Integer, primary_key=True)
    prospect_id = Column(Integer, ForeignKey("prospects.id"), nullable=False, unique=True, index=True)
    decision = Column(String(20))  # went_live / not_yet / not_going_live
    primary_reason = Column(String(30))  # price / dont_see_need / using_someone_else / still_deciding / technical_issue / design_not_right / other
    reason_detail = Column(Text, nullable=True)
    decision_maker = Column(String(20), nullable=True)  # owner / employee / other
    already_pays_for_website = Column(Boolean, nullable=True)
    how_get_customers = Column(String(30), nullable=True)  # word_of_mouth / google_search / social_media / directories / repeat_customers / other
    timeline = Column(String(20), nullable=True)  # this_week / this_month / not_sure / no_plans
    what_would_change_mind = Column(Text, nullable=True)
    discount_code_issued = Column(String(50), nullable=True)
    discount_expires_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class DiscoveryRunLog(Base):
    """One row per run of the nightly free email-discovery routine (added
    2026-07-18, replaced the paid Tier 2 API-based discovery — see
    docs/outreach-pipeline-spec.md Section 4a). The routine itself
    (a scheduled Claude Code cloud agent, not code in this repo) writes
    this row as its last step, using its own Bash+DB access — this table
    is how its results become visible in the admin dashboard without
    having to check the routine's run history on claude.ai separately."""
    __tablename__ = "discovery_run_logs"

    id = Column(Integer, primary_key=True)
    run_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    processed_n = Column(Integer, default=0)
    found_n = Column(Integer, default=0)
    website_rediscovered_n = Column(Integer, default=0)
    finalized_null_n = Column(Integer, default=0)
    source_breakdown = Column(JSON, nullable=True)  # e.g. {"own_website": 2, "web_search": 1, "facebook": 0}
    notes = Column(Text, nullable=True)


class DiscoveryImportState(Base):
    """Single-row table tracking the last Google Drive file the automated
    morning pickup (outreach/pickup_drive_results.py) imported — lets that
    job be idempotent (safe to re-run without double-applying the same
    night's results) without needing any state on the Drive side, since
    the routine can only create new files, not track "already picked up"
    itself. Added 2026-07-19 alongside the Drive-based output redesign."""
    __tablename__ = "discovery_import_state"

    id = Column(Integer, primary_key=True)
    last_drive_file_id = Column(String(100), nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class SearchCell(Base):
    __tablename__ = "search_cells"

    id = Column(Integer, primary_key=True)
    postcode_area = Column(String(20))
    trade_search_term = Column(String(100))
    last_searched_at = Column(DateTime, nullable=True)
    search_count = Column(Integer, default=0)
    results_found = Column(Integer, default=0)

    __table_args__ = (UniqueConstraint("postcode_area", "trade_search_term", name="uq_search_cells_area_term"),)


class GooglePlacesApiUsage(Base):
    """Tracks actual billed Google Places API (New) Text Search calls per
    calendar month (added 2026-07-23) — one row per "YYYY-MM". Lets
    sourcing pace itself against the Enterprise SKU's free monthly
    allowance (1,000 calls, resets on the 1st, no rollover — see
    outreach/sourcer.py's search_places() FIELD_MASK, which requests fields
    that put every call in that billing tier) rather than burning the
    whole month's free quota in the first few days. calls_used counts every
    real HTTP request actually sent to Google (including extra pagination
    pages), not "cell searches" — a single cell search can cost up to
    MAX_PAGES billed calls. See outreach/sourcer.py's
    get_daily_places_api_budget()."""
    __tablename__ = "google_places_api_usage"

    id = Column(Integer, primary_key=True)
    month = Column(String(7), nullable=False, unique=True)  # "YYYY-MM"
    calls_used = Column(Integer, nullable=False, default=0)


class PendingVisionCheck(Base):
    """One row per prospect waiting for a website quality judgment.
    Cowork reads these rows, views the screenshot, then calls apply_result.py
    to write the verdict and delete the row."""
    __tablename__ = "pending_vision_checks"

    id = Column(Integer, primary_key=True)
    prospect_id = Column(Integer, ForeignKey("prospects.id"), unique=True, nullable=False, index=True)
    screenshot_path = Column(String(500), nullable=True)  # None if screenshot failed
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class PendingEmailDiscovery(Base):
    """One row per prospect waiting for an email address to be found.
    Cowork reads these rows, searches the web, then calls apply_result.py
    to write the result (found email or null) and delete the row."""
    __tablename__ = "pending_email_discoveries"

    id = Column(Integer, primary_key=True)
    prospect_id = Column(Integer, ForeignKey("prospects.id"), unique=True, nullable=False, index=True)
    business_name = Column(String(255))
    location = Column(String(255))
    website = Column(String(500), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class SmsDeliveryEvent(Base):
    """One row per observed Esendex message status — an initial 'submitted'
    row at send time, then later rows as outreach/sms_status_poll.py polls
    and observes changes (Esendex has no per-send push callback, unlike
    Twilio's status_callback this table was originally built against — see
    outreach/sms.py's module docstring). Section 15's SMS health signal.
    message_sid holds an Esendex message id despite the Twilio-era column
    name — left as-is to avoid a schema rename disrupting existing rows."""
    __tablename__ = "sms_delivery_events"

    id = Column(Integer, primary_key=True)
    message_sid = Column(String(64), index=True)
    to_phone = Column(String(50))
    status = Column(String(30))  # queued/sent/delivered/undelivered/failed
    created_at = Column(DateTime, default=datetime.utcnow)


class EmailEventLog(Base):
    """One row per Resend webhook event received (Section 15's email health
    signal — the interim proxy for Postmaster Tools). See
    app.py:resend_events_webhook, outreach/ramp.py."""
    __tablename__ = "email_event_log"

    id = Column(Integer, primary_key=True)
    resend_email_id = Column(String(100), index=True, nullable=True)
    to_email = Column(String(255))
    event_type = Column(String(30))  # delivered/bounced/complained/opened/clicked
    created_at = Column(DateTime, default=datetime.utcnow)
    # Raw Resend webhook `data` payload for this event — added 2026-07-21
    # so bounce/complaint reasons are actually visible (previously only
    # event_type/to_email were kept, so a bounce told you THAT it bounced
    # but never WHY). Stored as-is rather than parsed into named columns
    # up front, since Resend's exact bounce-detail key names aren't
    # something this codebase has verified against a real payload yet —
    # keeping the raw dict means nothing is lost/guessed-wrong regardless
    # of which keys it turns out to actually use; app.py's
    # _extract_bounce_reason() does the best-effort parsing for display.
    # Only populated going forward — events logged before this column
    # existed have detail=None, same "no historical backfill" convention
    # as OutreachTouch.
    detail = Column(JSON, nullable=True)


class RampState(Base):
    """One row per channel (email/sms) — tracks the current approved daily
    volume and circuit-breaker state for Section 15's dynamic send ramp.
    See outreach/ramp.py."""
    __tablename__ = "ramp_state"

    id = Column(Integer, primary_key=True)
    channel = Column(String(10), unique=True, nullable=False)  # "email" / "sms"
    daily_volume = Column(Integer, nullable=False)
    week_number = Column(Integer, nullable=False, default=1)
    week_started_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    circuit_breaker_tripped = Column(Boolean, default=False)
    circuit_breaker_tripped_at = Column(DateTime, nullable=True)
    # Consecutive nightly checks with a clean signal while tripped — added
    # 2026-07-17 alongside real circuit-breaker recovery (previously the
    # breaker held at the floor forever once tripped; see outreach/ramp.py).
    # Resets to 0 on any breach observed while tripped; clears the trip once
    # it reaches CIRCUIT_BREAKER_RECOVERY_DAYS.
    consecutive_clean_days = Column(Integer, default=0)
    last_checked_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class DailySendCount(Base):
    """One row per (channel, date) — how many sends (initial + follow-up,
    combined) have already gone out today, so the ramp allowance can be
    checked against actual usage rather than assumed."""
    __tablename__ = "daily_send_counts"

    id = Column(Integer, primary_key=True)
    channel = Column(String(10), nullable=False)  # "email" / "sms"
    send_date = Column(String(10), nullable=False)  # "YYYY-MM-DD", UTC
    count = Column(Integer, nullable=False, default=0)

    __table_args__ = (UniqueConstraint("channel", "send_date", name="uq_daily_send_counts_channel_date"),)


class HourlySendCount(Base):
    """One row per (channel, hour_bucket) — added 2026-07-21 alongside the
    hourly email ramp (Section 15's daily volume is still the 7-day health-
    signal denominator via DailySendCount, unchanged; this is a second,
    finer-grained counter purely for the per-hour send cap, so sending 60
    emails in the first hour of an 08:00-19:00 window can't happen just
    because the day's total budget hasn't been used up yet). hour_bucket was
    "YYYY-MM-DD-HH", UTC, until 2026-07-23, when email switched to a 15-min
    slot cadence (outreach/ramp.py's _slot_bucket) — email's bucket is now
    "YYYY-MM-DD-HH-S" (S = 0-3, 16 chars), SMS's is unchanged at 13. Column
    widened to fit both (String(13) crashed send-job-cron outright on the
    first email insert after the cadence change — StringDataRightTruncation,
    not caught/handled anywhere, so the whole job died)."""
    __tablename__ = "hourly_send_counts"

    id = Column(Integer, primary_key=True)
    channel = Column(String(10), nullable=False)  # "email" / "sms"
    hour_bucket = Column(String(20), nullable=False)  # "YYYY-MM-DD-HH" or "YYYY-MM-DD-HH-S", UTC
    count = Column(Integer, nullable=False, default=0)

    __table_args__ = (UniqueConstraint("channel", "hour_bucket", name="uq_hourly_send_counts_channel_hour"),)


class InboundReply(Base):
    """One row per real inbound message (email or SMS) received from a
    prospect — added 2026-07-21. Before this table existed,
    outreach/reply_handling.py's handle_inbound_email/handle_inbound_sms
    received the actual message body from the webhook payload but only
    ever used it to classify stop-intent vs. not; the text itself was
    discarded, so /admin/replies could show THAT someone replied but never
    WHAT they wrote — the admin had to dig through the
    groundwork-build@outlook.com forwarding inbox by hand, which then
    turned out to be missing anyway (see email_forward_log's docstring —
    the forward itself is best-effort and its failures are only logged,
    not retried). This table is the durable, in-app copy going forward.
    Multiple rows per prospect are expected (someone can reply more than
    once) — order by received_at, don't assume one row per prospect."""
    __tablename__ = "inbound_replies"

    id = Column(Integer, primary_key=True)
    prospect_id = Column(Integer, ForeignKey("prospects.id"), nullable=False, index=True)
    channel = Column(String(10), nullable=False)  # "email" / "sms"
    from_address = Column(String(255))
    body = Column(Text)
    is_stop_intent = Column(Boolean, default=False)
    received_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)


class OutreachTouch(Base):
    """One row per individual outreach send — going forward only, added
    2026-07-14. Before this table existed, only cumulative current-state
    fields on Prospect (touch_count, funnel_substage, last_touch_at) were
    written, with no history of which stage/channel each touch actually
    was — this is what makes a real per-stage, per-channel funnel
    breakdown possible from here on. Nothing before this table's creation
    date can be backfilled; there is no historical data to reconstruct it
    from (see docs/outreach-pipeline-spec.md's Funnel dashboard notes).

    variant_id/opened_at/clicked_at/paid_at added 2026-07-21 alongside the
    email-variant testing system (outreach/variant_optimizer_job.py, Section
    19). variant_id is null for SMS touches (variants are email-only) and
    for any touch predating this system. opened_at/clicked_at/paid_at mirror
    the same-named Prospect fields but at PER-TOUCH granularity — Prospect's
    versions are "ever happened, once, across the prospect's whole
    lifetime" flags, which can't tell you which of several sent variants a
    prospect actually opened/clicked/paid after. These are written by the
    same three call sites that write the Prospect-level fields (app.py:
    resend_events_webhook, _claim_generate_and_redirect, stripe_webhook),
    attributed to the LATEST touch for that prospect at event time (a
    standard last-touch attribution model — see that module's docstring for
    the honest limitations of this)."""
    __tablename__ = "outreach_touches"

    id = Column(Integer, primary_key=True)
    prospect_id = Column(Integer, ForeignKey("prospects.id"), nullable=False, index=True)
    stage = Column(String(10), nullable=False)  # "initial" / "A" / "B" / "C" / "D" / "hail_mary"
    channel = Column(String(10), nullable=False)  # "email" / "sms"
    sent_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    variant_id = Column(String(30), nullable=True, index=True)
    opened_at = Column(DateTime, nullable=True)
    clicked_at = Column(DateTime, nullable=True)
    paid_at = Column(DateTime, nullable=True)


class EmailVariant(Base):
    """One row per email-copy variant under test for a given outreach stage
    (docs/cold-email-evidence-library.md, docs/outreach-pipeline-spec.md
    Section 19). Added 2026-07-21 alongside outreach/variant_optimizer_job.py.

    stage: "initial" / "A" / "B" / "C" / "D" — matches OutreachTouch.stage
    and outreach/templates.py's stage keys. hail_mary is deliberately NOT
    variant-tested (a single fixed last-chance offer, not iterated copy).

    variant_id: human-readable, globally unique (e.g. "initial-v1",
    "A-v3") — stage prefix bakes in which stage it belongs to, so
    OutreachTouch.variant_id alone (without a join back to this table) is
    enough to identify both the stage and the specific copy that was sent.

    status: "active" (full rotation within its stage's weighted pool),
    "canary" (newly admitted, small weight, same pool — the two statuses
    differ only in weight/provenance, not in mechanism), "paused" (weight
    forced to 0, excluded from selection — either a deliberate demotion for
    confirmed underperformance, or a deliverability auto-pause; see `notes`
    for which), "pending_generation" (a placeholder row reserving this
    variant_id while a candidate has been requested from the daily
    generation routine but not yet written back — subject/body are empty
    strings until then; excluded from selection same as paused, via
    outreach/variant_selection.py's status.in_(["active","canary"])
    filter). Generation moved off a direct, metered Anthropic API call to
    this request/pickup flow 2026-07-21 — see
    outreach/variant_optimizer_job.py's module docstring for the full
    Drive-based mechanism, same pattern as the nightly email-discovery
    routine.

    weight: relative selection weight within its stage's active+canary
    pool (outreach/variant_selection.py) — NOT a 0-1 probability by itself,
    since it's only ever compared against sibling weights in the same
    stage. Not part of the user's original spec in the literal sense, but
    required to make "weighted probability" and "reallocate weight
    gradually" persistent/adjustable across job runs rather than
    recomputed from scratch on every selection.

    parent_variant_id: which variant this one was generated as a single-
    variable change from — null for the seeded baseline (variant #1 per
    stage), which has no parent. Lineage, not a foreign key constraint
    (a parent may later be paused/deleted-in-spirit — paused rows are kept,
    never deleted, so this never dangles in practice).

    isolated_variable: the one element that changed vs. parent_variant_id
    (e.g. "subject_length", "cta_wording", "personalization_depth",
    "paragraph_structure") — null only for the baseline (nothing changed
    from a parent it doesn't have). Never more than one axis per variant —
    see outreach/content_safety.py's isolation heuristic check, run before
    a candidate is admitted."""
    __tablename__ = "email_variants"

    id = Column(Integer, primary_key=True)
    stage = Column(String(10), nullable=False, index=True)
    variant_id = Column(String(30), unique=True, nullable=False, index=True)
    parent_variant_id = Column(String(30), nullable=True)
    subject = Column(Text, nullable=False)
    body = Column(Text, nullable=False)
    status = Column(String(20), nullable=False, default="active")  # active / canary / paused / pending_generation
    weight = Column(Float, nullable=False, default=1.0)
    rationale = Column(Text, nullable=True)
    isolated_variable = Column(String(50), nullable=True)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class EvidenceFinding(Base):
    """One row per Section 3 entry the optimizer job adds to the evidence
    library (docs/cold-email-evidence-library.md) — the DURABLE store for
    those findings. See that file's Section 3 architecture note: the
    optimizer job runs on a Railway Cron container with no git write
    access, so it cannot actually commit an appended line to the .md file
    in production. This table IS Section 3 going forward; /admin/variants
    renders it in the same Finding/Sample size/Isolated variable/Adaptation
    tested/Rationale shape the spec calls for."""
    __tablename__ = "evidence_findings"

    id = Column(Integer, primary_key=True)
    stage = Column(String(10), nullable=False)
    finding = Column(Text, nullable=False)
    sample_size = Column(Integer, nullable=False)
    isolated_variable = Column(String(50), nullable=True)
    adaptation_tested = Column(Text, nullable=True)
    rationale = Column(Text, nullable=False)
    variant_id = Column(String(30), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class OptimizerRunLog(Base):
    """One row per outreach/variant_optimizer_job.py run — belt-and-
    suspenders visibility per the spec ("checkable even if the dashboard
    route ever breaks"). `details` carries the full structured breakdown
    (per-stage sample counts, any promotions/pauses/new variants with their
    rationale) — this table doubles as both the daily-summary log the spec
    asked for AND the "recent actions" feed /admin/variants renders, rather
    than keeping two separate logs of the same events."""
    __tablename__ = "optimizer_run_logs"

    id = Column(Integer, primary_key=True)
    run_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    samples_processed = Column(Integer, nullable=False, default=0)
    action_taken = Column(String(30), nullable=False)  # "no_action_threshold_not_met" / "action_taken" / "error"
    details = Column(JSON, nullable=True)


class VariantOptimizerState(Base):
    """One row per stage — tracks the last OutreachTouch.id already counted
    toward that stage's sample threshold, so "enough NEW samples since the
    last action" (not "enough samples ever") is a real, incremental check
    rather than one that keeps re-triggering on the same already-seen
    sends."""
    __tablename__ = "variant_optimizer_state"

    id = Column(Integer, primary_key=True)
    stage = Column(String(10), unique=True, nullable=False)
    last_processed_touch_id = Column(Integer, nullable=False, default=0)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class VariantCandidatePickupState(Base):
    """Single-row table tracking the last Google Drive candidate-results
    file outreach/variant_optimizer_job.py has already imported — same
    idempotency purpose and shape as DiscoveryImportState, for the
    equivalent Drive-based handoff on the variant-generation side (added
    2026-07-21, replacing a direct Anthropic API call with a daily Claude
    Code routine — avoids metered API cost, mirrors the nightly email-
    discovery routine's architecture exactly)."""
    __tablename__ = "variant_candidate_pickup_state"

    id = Column(Integer, primary_key=True)
    last_drive_file_id = Column(String(100), nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL", "")
    # Railway/Heroku-style URLs use the postgres:// scheme; SQLAlchemy 2.x requires postgresql://
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    # Local dev fallback if no DB is configured
    return url or "sqlite:///local_dev.db"


engine = create_engine(_database_url(), pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def init_db():
    Base.metadata.create_all(engine)
    # Widen email_variants.status from its original VARCHAR(10) to fit
    # "pending_generation" (19 chars) — added 2026-07-21 alongside the
    # generation-request/pickup flow. _ensure_column only ADDS missing
    # columns, it can't widen an existing one, so this needs its own call;
    # done explicitly (not left to silently truncate) after the exact
    # StringDataRightTruncation incident this session already hit once on
    # search_cells.postcode_area for the same underlying reason.
    _ensure_column_width(EmailVariant.__tablename__, "status", 20)
    _ensure_column(Lead.__tablename__, "is_test", "BOOLEAN NOT NULL DEFAULT FALSE")
    _ensure_column(Generation.__tablename__, "stripe_customer_id", "VARCHAR(255)")
    _ensure_column(Generation.__tablename__, "stripe_setup_invoice_id", "VARCHAR(255)")
    _ensure_column(Generation.__tablename__, "subdomain", "VARCHAR(100)")
    _ensure_column(Generation.__tablename__, "html_pending", "TEXT")
    _ensure_column(Generation.__tablename__, "stripe_subscription_id", "VARCHAR(255)")
    _ensure_column(Generation.__tablename__, "canceled_at", "TIMESTAMP")
    _ensure_column(Generation.__tablename__, "cancel_at_period_end", "BOOLEAN DEFAULT FALSE")
    _ensure_column(Generation.__tablename__, "subscription_period_end", "TIMESTAMP")
    _ensure_column(Generation.__tablename__, "retention_offer_used_at", "TIMESTAMP")
    _ensure_column(Generation.__tablename__, "refunded_at", "TIMESTAMP")
    _ensure_column(Generation.__tablename__, "view_count", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(Generation.__tablename__, "first_viewed_at", "TIMESTAMP")
    _ensure_column(Generation.__tablename__, "last_viewed_at", "TIMESTAMP")
    _ensure_column(Generation.__tablename__, "total_view_seconds", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(Generation.__tablename__, "max_scroll_pct", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(Generation.__tablename__, "is_internal", "BOOLEAN NOT NULL DEFAULT FALSE")
    _ensure_column(Generation.__tablename__, "generation_cost_usd", "FLOAT")
    _ensure_column(Generation.__tablename__, "text_edited_at", "TIMESTAMP")
    _ensure_column(Generation.__tablename__, "checkout_started_at", "TIMESTAMP")
    _ensure_column(Domain.__tablename__, "registered_at", "TIMESTAMP")
    _ensure_column(Domain.__tablename__, "dns_configured_at", "TIMESTAMP")
    _ensure_column(Domain.__tablename__, "railway_connected_at", "TIMESTAMP")
    _ensure_column(Domain.__tablename__, "cloudflare_connected_at", "TIMESTAMP")
    _ensure_column(Domain.__tablename__, "live_email_sent_at", "TIMESTAMP")
    _ensure_column(Domain.__tablename__, "wholesale_gbp", "FLOAT")
    _ensure_column(Domain.__tablename__, "margin_gbp", "FLOAT")
    _ensure_column(Domain.__tablename__, "error_step", "VARCHAR(100)")
    _ensure_column(Domain.__tablename__, "error_message", "TEXT")
    _ensure_column(Domain.__tablename__, "is_internal", "BOOLEAN NOT NULL DEFAULT FALSE")
    _ensure_column(Domain.__tablename__, "stripe_subscription_id", "VARCHAR(255)")
    _ensure_column(Domain.__tablename__, "last_repriced_period_end", "TIMESTAMP")
    _ensure_column(Domain.__tablename__, "renewal_payment_failed_at", "TIMESTAMP")
    _ensure_column(Domain.__tablename__, "refunded_at", "TIMESTAMP")
    # Prospect / SearchCell columns — create_all() handles brand-new tables, but
    # these _ensure_column calls backfill columns onto an older prospects table
    # that predates a given field (same dependency-free migration pattern above).
    _ensure_column(Prospect.__tablename__, "primary_type", "VARCHAR(100)")
    _ensure_column(Prospect.__tablename__, "editorial_summary", "TEXT")
    _ensure_column(Prospect.__tablename__, "opening_hours", "JSON")
    _ensure_column(Prospect.__tablename__, "reviews", "JSON")
    _ensure_column(Prospect.__tablename__, "earliest_review_date", "TIMESTAMP")
    _ensure_column(Prospect.__tablename__, "google_photos_count", "INTEGER")
    _ensure_column(Prospect.__tablename__, "opening_hours_complete", "BOOLEAN")
    _ensure_column(Prospect.__tablename__, "website_status", "VARCHAR(30)")
    _ensure_column(Prospect.__tablename__, "vision_flag_layout", "BOOLEAN")
    _ensure_column(Prospect.__tablename__, "vision_flag_design", "BOOLEAN")
    _ensure_column(Prospect.__tablename__, "vision_flag_cta", "BOOLEAN")
    _ensure_column(Prospect.__tablename__, "vision_flag_content", "BOOLEAN")
    _ensure_column(Prospect.__tablename__, "vision_flag_reviews", "BOOLEAN")
    _ensure_column(Prospect.__tablename__, "vision_flag_load", "BOOLEAN")
    _ensure_column(Prospect.__tablename__, "score", "FLOAT")
    _ensure_column(Prospect.__tablename__, "email", "VARCHAR(255)")
    _ensure_column(Prospect.__tablename__, "email_source", "VARCHAR(50)")
    _ensure_column(Prospect.__tablename__, "email_domain_type", "VARCHAR(20)")
    _ensure_column(Prospect.__tablename__, "email_found", "BOOLEAN DEFAULT FALSE")
    _ensure_column(Prospect.__tablename__, "funnel_stage", "VARCHAR(50)")
    _ensure_column(Prospect.__tablename__, "approval_status", "VARCHAR(20)")
    _ensure_column(Prospect.__tablename__, "approved_at", "TIMESTAMP")
    _ensure_column(Prospect.__tablename__, "token", "VARCHAR(100)")
    _ensure_column(Prospect.__tablename__, "account_created_at", "TIMESTAMP")
    _ensure_column(Prospect.__tablename__, "screenshot_url", "VARCHAR(500)")
    _ensure_column(Prospect.__tablename__, "gif_url", "VARCHAR(500)")
    _ensure_column(Prospect.__tablename__, "email_version_sent", "VARCHAR(50)")
    _ensure_column(Prospect.__tablename__, "sms_version_sent", "VARCHAR(50)")
    _ensure_column(Prospect.__tablename__, "touch_count", "INTEGER DEFAULT 0")
    _ensure_column(Prospect.__tablename__, "discount_code", "VARCHAR(50)")
    _ensure_column(Prospect.__tablename__, "discount_expiry", "TIMESTAMP")
    _ensure_column(Prospect.__tablename__, "competitor_density", "INTEGER")
    _ensure_column(Prospect.__tablename__, "sent_at", "TIMESTAMP")
    _ensure_column(Prospect.__tablename__, "sent_at_dow", "INTEGER")
    _ensure_column(Prospect.__tablename__, "sent_at_hour", "INTEGER")
    _ensure_column(Prospect.__tablename__, "sent_at_slot", "INTEGER")
    _ensure_column(Prospect.__tablename__, "opened_at", "TIMESTAMP")
    _ensure_column(Prospect.__tablename__, "clicked_at", "TIMESTAMP")
    _ensure_column(Prospect.__tablename__, "paid_at", "TIMESTAMP")
    _ensure_column(Prospect.__tablename__, "sms_sent_at", "TIMESTAMP")
    _ensure_column(Prospect.__tablename__, "sms_delivered", "BOOLEAN")
    _ensure_column(Prospect.__tablename__, "email_unsubscribed", "BOOLEAN DEFAULT FALSE")
    _ensure_column(Prospect.__tablename__, "sms_unsubscribed", "BOOLEAN DEFAULT FALSE")
    _ensure_column(Prospect.__tablename__, "raw_data", "JSON")
    _ensure_column(Prospect.__tablename__, "error_notes", "TEXT")
    _ensure_column(Prospect.__tablename__, "processed_at", "TIMESTAMP")
    _ensure_column(Prospect.__tablename__, "funnel_substage", "VARCHAR(30)")
    _ensure_column(Prospect.__tablename__, "last_touch_at", "TIMESTAMP")
    _ensure_column(Prospect.__tablename__, "short_code", "VARCHAR(12)")
    _ensure_column(Prospect.__tablename__, "lead_id", "INTEGER")
    _ensure_column(Prospect.__tablename__, "email_unsubscribed_at", "TIMESTAMP")
    _ensure_column(Prospect.__tablename__, "sms_unsubscribed_at", "TIMESTAMP")
    _ensure_column(Prospect.__tablename__, "extraction_quality", "VARCHAR(10)")
    _ensure_column(Prospect.__tablename__, "website_quality", "VARCHAR(20)")
    _ensure_column(Prospect.__tablename__, "income_tier", "VARCHAR(10)")
    _ensure_column(SearchCell.__tablename__, "last_searched_at", "TIMESTAMP")
    _ensure_column(SearchCell.__tablename__, "search_count", "INTEGER DEFAULT 0")
    _ensure_column(SearchCell.__tablename__, "results_found", "INTEGER DEFAULT 0")
    _ensure_column(PendingVisionCheck.__tablename__, "screenshot_path", "VARCHAR(500)")
    _ensure_column(PendingEmailDiscovery.__tablename__, "website", "VARCHAR(500)")
    _ensure_column(RampState.__tablename__, "consecutive_clean_days", "INTEGER DEFAULT 0")
    _ensure_column(OutreachTouch.__tablename__, "variant_id", "VARCHAR(30)")
    _ensure_column(OutreachTouch.__tablename__, "opened_at", "TIMESTAMP")
    _ensure_column(OutreachTouch.__tablename__, "clicked_at", "TIMESTAMP")
    _ensure_column(OutreachTouch.__tablename__, "paid_at", "TIMESTAMP")
    _ensure_column_width(HourlySendCount.__tablename__, "hour_bucket", 20)


def _ensure_column(table_name: str, column_name: str, ddl_type: str) -> None:
    """
    Minimal, dependency-free auto-migration: adds a column to an existing table
    if it's missing. There's no Alembic in this project, and create_all() only
    creates brand-new tables — it never alters existing ones. Safe to call on
    every startup since it checks column existence first.
    """
    inspector = inspect(engine)
    if table_name not in inspector.get_table_names():
        return
    existing = {col["name"] for col in inspector.get_columns(table_name)}
    if column_name in existing:
        return
    with engine.begin() as conn:
        conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {ddl_type}"))


def _ensure_column_width(table_name: str, column_name: str, min_length: int) -> None:
    """Widens an existing VARCHAR(N) column to VARCHAR(min_length) if N is
    smaller — a companion to _ensure_column for the "column exists but is
    now too narrow for a new value" case (widening a VARCHAR is always a
    safe, instant metadata-only change in Postgres, no table rewrite/data
    loss risk, unlike narrowing). SQLite has no fixed-width VARCHAR
    enforcement at all, so this is a no-op there (local dev never hits the
    truncation error this exists to prevent in Postgres)."""
    if engine.dialect.name != "postgresql":
        return
    inspector = inspect(engine)
    if table_name not in inspector.get_table_names():
        return
    for col in inspector.get_columns(table_name):
        if col["name"] == column_name:
            current_length = getattr(col["type"], "length", None)
            if current_length is not None and current_length < min_length:
                with engine.begin() as conn:
                    conn.execute(text(
                        f"ALTER TABLE {table_name} ALTER COLUMN {column_name} TYPE VARCHAR({min_length})"
                    ))
            return
