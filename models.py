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
    subdomain = Column(String(100), nullable=True, index=True)

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
    latitude = Column(Float, nullable=True)
    longitude = Column(Float, nullable=True)
    types = Column(JSON, nullable=True)
    website_status = Column(String(30), nullable=True)
    score = Column(Float, nullable=True)
    email = Column(String(255), nullable=True)
    email_source = Column(String(50), nullable=True)
    email_found = Column(Boolean, default=False)
    funnel_stage = Column(String(50), default="sourced")
    approval_status = Column(String(20), default="pending")
    approved_at = Column(DateTime, nullable=True)
    token = Column(String(100), unique=True, nullable=True)
    account_created_at = Column(DateTime, nullable=True)
    screenshot_url = Column(String(500), nullable=True)
    gif_url = Column(String(500), nullable=True)
    email_version_sent = Column(String(50), nullable=True)
    sms_version_sent = Column(String(50), nullable=True)
    touch_count = Column(Integer, default=0)
    discount_code = Column(String(50), nullable=True)
    discount_expiry = Column(DateTime, nullable=True)
    sent_at = Column(DateTime, nullable=True)
    opened_at = Column(DateTime, nullable=True)
    clicked_at = Column(DateTime, nullable=True)
    paid_at = Column(DateTime, nullable=True)
    sms_sent_at = Column(DateTime, nullable=True)
    sms_delivered = Column(Boolean, nullable=True)
    email_unsubscribed = Column(Boolean, default=False)
    sms_unsubscribed = Column(Boolean, default=False)
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
    # Prospect / SearchCell columns — create_all() handles brand-new tables, but
    # these _ensure_column calls backfill columns onto an older prospects table
    # that predates a given field (same dependency-free migration pattern above).
    _ensure_column(Prospect.__tablename__, "website_status", "VARCHAR(30)")
    _ensure_column(Prospect.__tablename__, "score", "FLOAT")
    _ensure_column(Prospect.__tablename__, "email", "VARCHAR(255)")
    _ensure_column(Prospect.__tablename__, "email_source", "VARCHAR(50)")
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
    _ensure_column(Prospect.__tablename__, "sent_at", "TIMESTAMP")
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
