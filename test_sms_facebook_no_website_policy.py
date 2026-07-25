"""
Real, executed proof that the "SMS/Facebook only for no_website prospects"
policy (outreach/sms.py's sms_channel_eligible, added 2026-07-25) actually
blocks sends, not just that the code compiles — runs the real
send_initial_touch against a throwaway SQLite DB with send_outreach_sms/
send_outreach_email mocked, for all four combinations of (phone-only vs
email-track) x (has_website vs no_website).
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta

DB_PATH = tempfile.mktemp(suffix=".db")
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"
os.environ["EMAIL_REPLY_CAPTURE_READY"] = "true"
os.environ["SMS_REPLY_CAPTURE_READY"] = "true"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from models import Base, engine, SessionLocal, Prospect, OutreachTouch, EmailEventLog  # noqa: E402
Base.metadata.create_all(engine)

failures = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


from outreach.sms import sms_channel_eligible  # noqa: E402
import outreach.send_job as send_job_mod  # noqa: E402
import outreach.followup as followup_mod  # noqa: E402
from outreach.seed_variants import seed_baseline_variants  # noqa: E402

db = SessionLocal()
seed_baseline_variants(db)

sms_sent = []


def fake_send_outreach_sms(phone, body):
    sms_sent.append((phone, body))
    return "fake-sms-id"


def fake_send_outreach_email(to_email, subject, body, unsubscribe_link):
    return "fake-resend-id"


send_job_mod.send_outreach_sms = fake_send_outreach_sms
send_job_mod.send_outreach_email = fake_send_outreach_email
followup_mod.send_outreach_sms = fake_send_outreach_sms
followup_mod.send_outreach_email = fake_send_outreach_email

# ── sms_channel_eligible itself ──
p_has_website = Prospect(google_place_id="g1", business_name="Has Website Biz", phone="+447900000010",
                          website_status="has_website", email=None, email_found=False, score=50.0)
p_no_website = Prospect(google_place_id="g2", business_name="No Website Biz", phone="+447900000011",
                         website_status="no_website", email=None, email_found=False, score=50.0)
check("has_website prospect is NOT sms-eligible", sms_channel_eligible(p_has_website) is False)
check("no_website prospect IS sms-eligible", sms_channel_eligible(p_no_website) is True)

# ── Real send_initial_touch call: phone-only, has_website -> no SMS sent ──
db.add(p_has_website)
db.add(p_no_website)
db.commit()

now = datetime.utcnow()
remaining = {"email": 100, "sms": 100}
result_hw = send_job_mod.send_initial_touch(db, p_has_website, now, remaining)
check("phone-only has_website prospect: send_initial_touch does NOT touch them (SMS blocked)",
      result_hw["touched"] is False, str(result_hw))
check("no SMS was actually sent to the has_website prospect", len(sms_sent) == 0, str(sms_sent))

result_nw = send_job_mod.send_initial_touch(db, p_no_website, now, remaining)
check("phone-only no_website prospect: send_initial_touch DOES send SMS",
      result_nw["touched"] is True, str(result_nw))
check("exactly one SMS was sent, to the no_website prospect",
      len(sms_sent) == 1 and sms_sent[0][0] == "+447900000011", str(sms_sent))

# ── Email-track (has email) + has_website + has phone -> email only, no parallel SMS ──
p_email_track_hw = Prospect(google_place_id="g3", business_name="Email Track HW", phone="+447900000012",
                             website_status="has_website", email="owner@example.com", email_found=True, score=50.0)
db.add(p_email_track_hw)
db.commit()
sms_sent.clear()
result_et = send_job_mod.send_initial_touch(db, p_email_track_hw, now, remaining)
check("email-track has_website prospect with a phone: touched via email", result_et["touched"] is True)
check("NO parallel SMS sent to an email-track has_website prospect", len(sms_sent) == 0, str(sms_sent))

# Same but no_website -> should get BOTH email and SMS in parallel.
p_email_track_nw = Prospect(google_place_id="g4", business_name="Email Track NW", phone="+447900000013",
                             website_status="no_website", email="owner@example.com", email_found=True, score=50.0)
db.add(p_email_track_nw)
db.commit()
sms_sent.clear()
result_et2 = send_job_mod.send_initial_touch(db, p_email_track_nw, now, remaining)
check("email-track no_website prospect: touched", result_et2["touched"] is True)
check("parallel SMS DOES fire for an email-track no_website prospect", len(sms_sent) == 1, str(sms_sent))

db.commit()
db.close()
os.remove(DB_PATH)

print("")
print("=" * 56)
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
else:
    print("ALL CHECKS PASSED")
print("=" * 56)
sys.exit(1 if failures else 0)
