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
    rating = Column(Float, nullable=True)
    review_count = Column(Integer, nullable=True)
    business_status = Column(String(50))
    phone = Column(String(50), nullable=True)
    website = Column(String(500), nullable=True)
    competitor_density = Column(Integer, nullable=True)
    latitude = Column(Float, nullable=True)
    longitude = Column(Float, nullable=True)
    types = Column(JSON, nullable=True)
    google_photos_count = Column(Integer, nullable=True)
    opening_hours_complete = Column(Boolean, nullable=True)
    website_status = Column(String(30), nullable=True)
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


class SearchCell(Base):
    __tablename__ = "search_cells"

    id = Column(Integer, primary_key=True)
    postcode_area = Column(String(20))
    trade_search_term = Column(String(100))
    last_searched_at = Column(DateTime, nullable=True)
    search_count = Column(Integer, default=0)
    results_found = Column(Integer, default=0)

    __table_args__ = (UniqueConstraint("postcode_area", "trade_search_term", name="uq_search_cells_area_term"),)


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


class OutreachTouch(Base):
    """One row per individual outreach send — going forward only, added
    2026-07-14. Before this table existed, only cumulative current-state
    fields on Prospect (touch_count, funnel_substage, last_touch_at) were
    written, with no history of which stage/channel each touch actually
    was — this is what makes a real per-stage, per-channel funnel
    breakdown possible from here on. Nothing before this table's creation
    date can be backfilled; there is no historical data to reconstruct it
    from (see docs/outreach-pipeline-spec.md's Funnel dashboard notes)."""
    __tablename__ = "outreach_touches"

    id = Column(Integer, primary_key=True)
    prospect_id = Column(Integer, ForeignKey("prospects.id"), nullable=False, index=True)
    stage = Column(String(10), nullable=False)  # "initial" / "A" / "B" / "C" / "D"
    channel = Column(String(10), nullable=False)  # "email" / "sms"
    sent_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)


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
    # Prospect / SearchCell columns — create_all() handles brand-new tables, but
    # these _ensure_column calls backfill columns onto an older prospects table
    # that predates a given field (same dependency-free migration pattern above).
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
    _ensure_column(SearchCell.__tablename__, "last_searched_at", "TIMESTAMP")
    _ensure_column(SearchCell.__tablename__, "search_count", "INTEGER DEFAULT 0")
    _ensure_column(SearchCell.__tablename__, "results_found", "INTEGER DEFAULT 0")
    _ensure_column(PendingVisionCheck.__tablename__, "screenshot_path", "VARCHAR(500)")
    _ensure_column(PendingEmailDiscovery.__tablename__, "website", "VARCHAR(500)")


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
