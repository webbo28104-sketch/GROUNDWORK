"""
Groundwork outreach — magic-link token + short-code generation
(docs/outreach-pipeline-spec.md Section 9a).

Shared by outreach/send_job.py (initial sends) and outreach/followup.py
(follow-up touches) so both call the same "ensure this prospect has a
working link" step rather than duplicating it. Prospect.token previously
had NO code path anywhere that ever set it — this module is that path.
"""
import uuid
import random
import string

from models import Prospect


def _generate_unique_short_code(db):
    for _ in range(10):
        code = "".join(random.choices(string.ascii_lowercase + string.digits, k=7))
        if not db.query(Prospect).filter(Prospect.short_code == code).first():
            return code
    raise RuntimeError("Could not generate a unique short_code after 10 attempts")


def ensure_link_identity(db, p):
    """Ensure a prospect has both a magic-link token (/claim/<token>) and a
    short code (/s/<short_code>) before a send goes out referencing them —
    generated at the point a send is queued, per Section 9a. Mutates p in
    place; caller is responsible for db.commit()."""
    if not p.token:
        p.token = uuid.uuid4().hex
    if not p.short_code:
        p.short_code = _generate_unique_short_code(db)
