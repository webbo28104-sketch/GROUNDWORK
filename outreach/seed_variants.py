"""
Seeds the 5 finalized outreach/templates.py templates (initial/A/B/C/D) as
each stage's variant #1, idempotent — safe to call on every job start
(outreach/send_job.py, outreach/followup.py's callers, variant_optimizer_job.py)
rather than needing a one-off migration script someone has to remember to
run. hail_mary is deliberately excluded — it's a single fixed last-chance
offer, not a copy under test (see EmailVariant's docstring in models.py).
"""
import logging

from models import EmailVariant
from outreach.templates import INITIAL_EMAIL, FOLLOWUP_EMAIL

logger = logging.getLogger("outreach.seed_variants")

_BASELINE_STAGE_TEMPLATES = {
    "initial": INITIAL_EMAIL,
    "A": FOLLOWUP_EMAIL["A"],
    "B": FOLLOWUP_EMAIL["B"],
    "C": FOLLOWUP_EMAIL["C"],
    "D": FOLLOWUP_EMAIL["D"],
}


def seed_baseline_variants(db):
    """Insert variant #1 for any stage that doesn't have one yet. Returns
    how many were newly created (0 on every call after the first, in
    steady state)."""
    created = 0
    for stage, template in _BASELINE_STAGE_TEMPLATES.items():
        variant_id = f"{stage}-v1"
        existing = db.query(EmailVariant).filter(EmailVariant.variant_id == variant_id).first()
        if existing:
            continue
        db.add(EmailVariant(
            stage=stage,
            variant_id=variant_id,
            parent_variant_id=None,
            subject=template["subject"],
            body=template["body"],
            status="active",
            weight=1.0,
            rationale="original baseline template",
            isolated_variable=None,
        ))
        created += 1
        logger.info("Seeded baseline variant %s for stage %r", variant_id, stage)
    if created:
        db.commit()
    return created
