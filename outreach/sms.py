"""
Groundwork outreach — SMS via Esendex (docs/outreach-pipeline-spec.md Section 10a).

Provider decision: Esendex (UK-based, ICO-registered) is the primary SMS
provider — NOT Twilio. No Twilio account exists or will be set up; Plivo is
a fallback only if Esendex's API proves difficult in practice, not built
preemptively here.

CONFIRMED against Esendex's public developer docs (developers.esendex.com,
checked directly, not assumed):
  - Sending: Message Dispatcher — POST https://api.esendex.com/v1.0/messagedispatcher,
    HTTP Basic Auth (username:password, base64), XML request body, XML ack response.
  - Delivery status: NOT a push webhook for plain SMS — the only "account"-
    product webhook events documented are "inbound" and "stop"; "delivered"/
    "failed" webhook events are documented only under the separate Rich
    Content API product (RCS/WhatsApp), which isn't what's used here. Built
    as a POLL against the Message Headers API instead
    (GET /v1.0/messageheaders/{id}, status one of Submitted/Sent/Delivered/
    Expired/Failed) — see poll_delivery_statuses() below. This is a
    structural difference from Twilio's per-send status_callback push
    model, not a smaller version of the same thing.

NOT fully confirmed (flagged, not guessed silently) — see app.py's
sms_inbound_webhook for how this is handled defensively.
"""
import os
import base64
import logging
from xml.etree import ElementTree as ET

import requests

logger = logging.getLogger("outreach.sms")

API_BASE = "https://api.esendex.com/v1.0"


def sms_channel_eligible(prospect):
    """SMS (and, by the same policy, Facebook DM — see
    app.py:admin_facebook_outreach) is reserved for no_website prospects
    only, added 2026-07-25 by explicit request: a prospect with a website
    is reachable by email, which is the primary channel and already sends
    enough volume — SMS/Facebook shouldn't double up on them. Checked
    everywhere an SMS send is about to happen (outreach/send_job.py's
    initial send, both the phone-only track and the parallel SMS leg on
    the email track; outreach/followup.py's every SMS branch including
    hail_mary), not just at prospect-selection time, so this stays correct
    even if a prospect's website_status changes after they first entered
    the pipeline."""
    return prospect.website_status == "no_website"


def _auth_header():
    username = os.environ.get("ESENDEX_USERNAME")
    password = os.environ.get("ESENDEX_PASSWORD")
    if not (username and password):
        return None
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def _xml_escape(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def send_outreach_sms(to_phone: str, body: str):
    """
    Send one SMS via Esendex's Message Dispatcher.

    Returns the Esendex-assigned message id on success (needed later to
    correlate a delivery status via poll_delivery_statuses — Esendex has no
    per-send status_callback param the way Twilio did), or None on
    failure/skip. Mirrors send_outreach_email's return-the-id contract
    (emails.py) for the same reason: a caller needs to be able to prove a
    specific send happened, not just that no exception was raised.
    """
    account_ref = os.environ.get("ESENDEX_ACCOUNT_REFERENCE")
    headers = _auth_header()

    if not (account_ref and headers):
        print(f"[sms] Esendex not configured — skipping SMS send to {to_phone}: {body[:40]!r}...")
        return None

    xml_body = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<messages>"
        f"<accountreference>{_xml_escape(account_ref)}</accountreference>"
        "<message>"
        f"<to>{_xml_escape(to_phone)}</to>"
        f"<body>{_xml_escape(body)}</body>"
        "</message>"
        "</messages>"
    )

    try:
        resp = requests.post(
            f"{API_BASE}/messagedispatcher",
            data=xml_body.encode("utf-8"),
            headers={**headers, "Content-Type": "application/xml"},
            timeout=15,
        )
        resp.raise_for_status()
        root = ET.fromstring(resp.text)
        # Namespace-agnostic find — Esendex's XML responses are typically
        # namespaced; {*} matches the local tag name regardless of namespace.
        header_el = root.find(".//{*}messageheader")
        message_id = header_el.get("id") if header_el is not None else None
        print(f"[sms] sent via Esendex to {to_phone}: id={message_id}")
        return message_id
    except Exception as exc:
        print(f"[sms] failed to send SMS to {to_phone}: {exc}")
        return None


def poll_delivery_statuses(message_ids):
    """
    Query current status for the given Esendex message ids. Returns
    {message_id: status_or_None}. Call periodically (see
    outreach/sms_status_poll.py) against ids sent recently whose last known
    status wasn't yet terminal, and log changes to SmsDeliveryEvent — this
    is the SMS half of Section 15's health signal, structurally a poll
    rather than a push (see module docstring for why).
    """
    headers = _auth_header()
    if not headers:
        return {}

    statuses = {}
    for message_id in message_ids:
        try:
            resp = requests.get(f"{API_BASE}/messageheaders/{message_id}", headers=headers, timeout=15)
            resp.raise_for_status()
            root = ET.fromstring(resp.text)
            status_el = root.find(".//{*}status")
            statuses[message_id] = status_el.text.strip() if status_el is not None and status_el.text else None
        except Exception as exc:
            logger.warning("poll_delivery_statuses: failed for id %s: %s", message_id, exc)
            statuses[message_id] = None
    return statuses
