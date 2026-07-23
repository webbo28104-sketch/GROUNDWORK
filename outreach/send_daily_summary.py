"""
Groundwork outreach — once-a-day admin summary (added 2026-07-23).

Replaces the old per-click "Magic link clicked" admin email (removed the
same day, app.py's _claim_generate_and_redirect — real-time pings on every
individual click had no signal value once volume grew past a handful a
day). This is a standalone Railway Cron script, same "one script, one cron
service" pattern as every other job in outreach/ (see CLAUDE.md) — meant to
run once daily, AFTER the last send-job-cron slot of the day (send-job-cron
fires every 15 min inside outreach/ramp.py's EMAIL_SEND_WINDOW_START_HOUR/
END_HOUR_EXCLUSIVE, currently 03:00-23:00 UTC — this should be scheduled
for something like 23:15 UTC to land safely after the window closes; check
that file if the window ever changes).

Reports, for the current UTC calendar day so far:
  - total emails sent (OutreachTouch rows, channel='email')
  - total prospects who clicked their magic link, and click % of sent
  - total prospects who signed up to the free trial (paid_at set), and
    signup % of sent

Usage:
    python -m outreach.send_daily_summary
"""
import os
import sys
from datetime import datetime, timedelta

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_THIS_DIR)
for _p in (_PROJECT_ROOT, _THIS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from models import SessionLocal, OutreachTouch, Prospect, init_db
from emails import send_admin_daily_summary_email


def _today_bounds(now=None):
    now = now or datetime.utcnow()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return start, end


def run(now=None):
    init_db()
    now = now or datetime.utcnow()
    start, end = _today_bounds(now)

    db = SessionLocal()
    try:
        emails_sent = db.query(OutreachTouch).filter(
            OutreachTouch.channel == "email",
            OutreachTouch.sent_at >= start,
            OutreachTouch.sent_at < end,
        ).count()

        clicked = db.query(Prospect).filter(
            Prospect.clicked_at >= start,
            Prospect.clicked_at < end,
        ).count()

        signed_up = db.query(Prospect).filter(
            Prospect.paid_at >= start,
            Prospect.paid_at < end,
        ).count()
    finally:
        db.close()

    click_pct = (clicked / emails_sent * 100) if emails_sent else 0.0
    signup_pct = (signed_up / emails_sent * 100) if emails_sent else 0.0
    day_label = start.strftime("%A %d %B %Y") + " (UTC)"

    send_admin_daily_summary_email(day_label, emails_sent, clicked, click_pct, signed_up, signup_pct)
    print(f"Daily summary sent — {emails_sent} sent, {clicked} clicked ({click_pct:.1f}%), "
          f"{signed_up} signed up ({signup_pct:.1f}%)")


if __name__ == "__main__":
    run()
