"""
Groundwork — database models.

Two tables:
- Lead: one row per form submission, created before email verification.
  Holds the mapped form data (as JSON) plus logo path, so generation can be
  kicked off later from /verify/<token> without asking the user to resubmit.
- Generation: one row per completed site generation. This is the durable
  source of truth for generated HTML — the verification/resend emails are
  just notifications pointing back at rows in this table.
"""
import os
from datetime import datetime

from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, ForeignKey, JSON, Boolean, inspect, text
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
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    status = Column(String(30), nullable=False, default="draft")
    stripe_customer_id = Column(String(255))

    lead = relationship("Lead", back_populates="generations")


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
