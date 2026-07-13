"""
Groundwork outreach — SMS via Twilio.

Mirrors the guarded-skip pattern in emails.py's _send(): if credentials
aren't set, log and return instead of failing the caller. Same reasoning —
outreach code (nightly job, admin tools) shouldn't crash in dev/CI just
because Twilio isn't configured there.
"""
import os

BASE_URL = os.environ.get("GROUNDWORK_PUBLIC_URL", "https://groundworkbuild.com")


def send_outreach_sms(to_phone: str, body: str) -> None:
    account_sid = os.environ.get("TWILIO_ACCOUNT_SID")
    auth_token = os.environ.get("TWILIO_AUTH_TOKEN")
    from_number = os.environ.get("TWILIO_FROM_NUMBER")

    if not (account_sid and auth_token and from_number):
        print(f"[sms] Twilio not configured — skipping SMS send to {to_phone}: {body[:40]!r}...")
        return

    try:
        from twilio.rest import Client
    except ImportError:
        print(f"[sms] twilio package not installed — skipping SMS send to {to_phone}")
        return

    try:
        client = Client(account_sid, auth_token)
        # status_callback (Section 15's SMS health signal) — Twilio POSTs
        # delivery status (delivered/failed/undelivered/...) to this URL as
        # it learns it, verified there the same way as the inbound webhook
        # (app.py:sms_status_webhook). Feeds outreach/ramp.py's
        # get_health_signal("sms").
        client.messages.create(
            to=to_phone, from_=from_number, body=body,
            status_callback=f"{BASE_URL}/api/webhooks/sms-status",
        )
    except Exception as exc:
        print(f"[sms] failed to send SMS to {to_phone}: {exc}")
