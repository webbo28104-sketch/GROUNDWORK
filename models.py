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

from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, ForeignKey, JSON, Boolean, UniqueConstraint, inspect, text
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
