"""
Weighted variant selection for outreach/send_job.py and outreach/followup.py
— docs/outreach-pipeline-spec.md Section 19.

pick_variant(db, stage) draws from the active+canary pool for that stage,
weighted by EmailVariant.weight (paused variants are excluded entirely, not
just zero-weighted, so a stale weight column can't accidentally revive one).
Falls back to whatever variant exists at all for that stage (even paused) if
somehow nothing is active/canary — better to send the last-known-good copy
than to crash a send job — and logs loudly if that ever happens, since it
means every variant for a stage got paused (a real incident worth seeing).
"""
import logging
import random

from models import EmailVariant

logger = logging.getLogger("outreach.variant_selection")


def pick_variant(db, stage):
    pool = db.query(EmailVariant).filter(
        EmailVariant.stage == stage,
        EmailVariant.status.in_(["active", "canary"]),
        EmailVariant.weight > 0,
    ).all()

    if not pool:
        logger.error(
            "pick_variant: no active/canary variant for stage %r — every variant may be paused. "
            "Falling back to any existing variant for this stage.", stage
        )
        pool = db.query(EmailVariant).filter(EmailVariant.stage == stage).order_by(EmailVariant.created_at.asc()).all()
        if not pool:
            return None
        return pool[0]

    weights = [v.weight for v in pool]
    return random.choices(pool, weights=weights, k=1)[0]


def render_variant(variant, **kwargs):
    return {
        "subject": variant.subject.format(**kwargs),
        "body": variant.body.format(**kwargs),
    }
