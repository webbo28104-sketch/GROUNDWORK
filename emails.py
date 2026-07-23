"""
Groundwork — transactional email via Resend.

Templates: verification (magic link to trigger generation), resend (magic
link to view previously generated sites), and password reset. All plain,
single-CTA emails matching the funnel's existing brand.
"""
import os
import re
from html import escape, unescape

import resend

ACCENT = "#3B82F6"

_HARDCODED_EMAIL_FALLBACK = "groundwork-build@outlook.com"


def _env_email_or_warn(key: str, default: str = _HARDCODED_EMAIL_FALLBACK) -> str:
    """os.environ.get(key, default), but logs a warning whenever the env var
    isn't set — silently falling back to a hardcoded address here means
    domain-order alerts, support messages, etc. go to the wrong inbox with
    no visible sign anything's wrong, until someone notices mail never
    arrived."""
    val = os.environ.get(key)
    if not val:
        print(f"[emails] WARNING: {key} not set — falling back to hardcoded {default!r}")
        return default
    return val


def _html_to_text(html_content: str) -> str:
    """Derive a plain-text alternative from an email's HTML body. Spam filters
    (mail-tester included) dock HTML-only emails with no text/plain part, so
    every send needs one — this isn't meant to be pretty, just a faithful,
    readable fallback for clients/filters that read it instead of the HTML."""
    text = re.sub(r'(?i)<(br|/p|/div|/h[1-6]|/tr|/li)\s*/?>', '\n', html_content)
    text = re.sub(r'(?s)<script.*?</script>|<style.*?</style>', '', text)
    text = re.sub(r'<[^>]+>', '', text)
    text = unescape(text)
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n\s*\n+', '\n\n', text)
    return text.strip()


def _send(to_email: str, subject: str, html_content: str, reply_to: str = None,
          raise_on_error: bool = False) -> None:
    api_key = os.environ.get("RESEND_API_KEY")
    from_email = _env_email_or_warn("RESEND_FROM_EMAIL")

    if not api_key:
        msg = f"[emails] RESEND_API_KEY not set — skipping send of '{subject}' to {to_email}"
        print(msg)
        if raise_on_error:
            raise RuntimeError(msg)
        return

    resend.api_key = api_key
    payload = {
        "from": from_email,
        "to": [to_email],
        "subject": subject,
        "html": html_content,
        "text": _html_to_text(html_content),
    }
    if reply_to:
        payload["reply_to"] = reply_to
    try:
        resend.Emails.send(payload)
    except Exception as exc:
        print(f"[emails] failed to send '{subject}' to {to_email}: {exc}")
        if raise_on_error:
            raise


def _wrapper(preheader: str, heading: str, body_html: str, cta_url: str, cta_label: str) -> str:
    return f"""<div style="font-family:Arial,Helvetica,sans-serif;background:#F5F3EE;padding:40px 20px;">
  <div style="max-width:480px;margin:0 auto;background:#fff;border-radius:12px;overflow:hidden;">
    <div style="background:#1C1C1C;padding:20px 28px;">
      <span style="color:{ACCENT};font-weight:800;font-size:18px;letter-spacing:-.03em;">Groundwork</span>
    </div>
    <div style="padding:32px 28px;">
      <h2 style="margin:0 0 14px;font-size:22px;color:#1C1C1C;">{heading}</h2>
      <div style="font-size:15px;line-height:1.6;color:#5C5A56;">{body_html}</div>
      <div style="margin-top:28px;">
        <a href="{cta_url}" style="display:inline-block;background:{ACCENT};color:#fff;font-weight:700;font-size:15px;text-decoration:none;padding:14px 26px;border-radius:8px;">{cta_label}</a>
      </div>
      <p style="margin-top:28px;font-size:12.5px;color:#9A9893;">If the button doesn't work, copy and paste this link into your browser:<br>{cta_url}</p>
    </div>
  </div>
  <p style="max-width:480px;margin:16px auto 0;font-size:11.5px;color:#9A9893;text-align:center;">{preheader}</p>
</div>"""


def send_verification_email(to_email: str, verify_url: str, business_name: str) -> None:
    name_bit = f" for {business_name}" if business_name else ""
    html = _wrapper(
        preheader="You're receiving this because this address was used to start a Groundwork website build.",
        heading="Confirm your email to build your website",
        body_html=f"Click the button below to verify this address and start generating your website{name_bit}. This link expires in 24 hours.",
        cta_url=verify_url,
        cta_label="Verify & build my website →",
    )
    _send(to_email, "Confirm your email to build your website — Groundwork", html)


def send_resend_email(to_email: str, my_sites_url: str) -> None:
    html = _wrapper(
        preheader="You're receiving this because you asked Groundwork to resend your site link.",
        heading="Here's your Groundwork link",
        body_html="Click the button below to view the website(s) you've generated with this email address. This link expires in 24 hours.",
        cta_url=my_sites_url,
        cta_label="View my website(s) →",
    )
    _send(to_email, "Your Groundwork website(s)", html)


def send_site_ready_email(to_email: str, business_name: str, preview_url: str, account_login_url: str) -> None:
    """Notify a customer their generated site is ready to preview, as soon as
    generation finishes — sent in addition to (not instead of) the loading
    page's own poll-and-redirect, since a real share of people who click the
    verification link don't sit through the ~3 minute build and bounce
    before ever seeing the result. Points at /account/login rather than the
    preview link directly, matching the existing account flow: entering this
    email there finds the just-created Generation and goes straight to
    "choose a password" (no re-verification needed, they already proved the
    address by clicking the original verification link)."""
    biz = escape(business_name) if business_name else "your website"
    html = _wrapper(
        preheader=f"Your Groundwork website for {business_name or 'your business'} has finished building.",
        heading="Your website is ready to preview",
        body_html=(
            f"Good news — <strong>{biz}</strong> has finished building. "
            f"Click below to preview it now, or come back any time and sign in with just this email "
            f"address at <a href=\"{account_login_url}\" style=\"color:{ACCENT};\">{account_login_url}</a> "
            f"to get back to your account."
        ),
        cta_url=preview_url,
        cta_label="Preview my website →",
    )
    _send(to_email, f"Your website is ready to preview — {business_name or 'Groundwork'}", html)


def send_password_reset_email(to_email: str, reset_url: str) -> None:
    html = _wrapper(
        preheader="You're receiving this because a password reset was requested for this Groundwork account.",
        heading="Reset your password",
        body_html="Click the button below to choose a new password for your Groundwork account. This link expires in 1 hour. If you didn't request this, you can safely ignore this email — your password won't change.",
        cta_url=reset_url,
        cta_label="Reset my password →",
    )
    _send(to_email, "Reset your password — Groundwork", html)


def send_enquiry_email(to_email: str, business_name: str, visitor_name: str,
                       visitor_email: str, visitor_phone: str, message: str) -> None:
    """Forward a contact form submission from a visitor to the business owner.
    Reply-to is set to the visitor's email so the owner can reply directly."""
    name_esc = escape(visitor_name)
    email_esc = escape(visitor_email)
    phone_esc = escape(visitor_phone) if visitor_phone else ""
    msg_esc = escape(message)
    biz_esc = escape(business_name) if business_name else "your Groundwork website"
    phone_row = f'<tr><td style="padding:6px 0;font-size:14px;color:#5C5A56;width:90px;">Phone</td><td style="padding:6px 0;font-size:14px;color:#1C1C1C;">{phone_esc}</td></tr>' if phone_esc else ""
    html = f"""<div style="font-family:Arial,Helvetica,sans-serif;background:#F5F3EE;padding:32px 20px;">
  <div style="max-width:500px;margin:0 auto;background:#fff;border-radius:12px;overflow:hidden;">
    <div style="background:#1C1C1C;padding:20px 28px;">
      <span style="color:{ACCENT};font-weight:800;font-size:18px;letter-spacing:-.03em;">Groundwork</span>
    </div>
    <div style="padding:28px;">
      <h2 style="margin:0 0 6px;font-size:19px;color:#1C1C1C;">New enquiry via {biz_esc}</h2>
      <p style="margin:0 0 20px;font-size:13.5px;color:#807E79;">Someone submitted the contact form on your website. Reply directly to this email to respond.</p>
      <table style="width:100%;border-collapse:collapse;margin-bottom:20px;">
        <tr><td style="padding:6px 0;font-size:14px;color:#5C5A56;width:90px;">Name</td><td style="padding:6px 0;font-size:14px;color:#1C1C1C;">{name_esc}</td></tr>
        <tr><td style="padding:6px 0;font-size:14px;color:#5C5A56;">Email</td><td style="padding:6px 0;font-size:14px;color:#1C1C1C;"><a href="mailto:{email_esc}" style="color:{ACCENT};">{email_esc}</a></td></tr>
        {phone_row}
      </table>
      <div style="background:#F5F3EE;border-radius:8px;padding:16px 18px;">
        <div style="font-size:12px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:#807E79;margin-bottom:10px;">Message</div>
        <div style="white-space:pre-wrap;font-size:15px;line-height:1.6;color:#1C1C1C;">{msg_esc}</div>
      </div>
    </div>
  </div>
</div>"""
    _send(to_email, f"New enquiry from {visitor_name} — {biz_esc}", html,
          reply_to=visitor_email, raise_on_error=True)


def send_changes_live_email(to_email: str, business_name: str) -> None:
    """Notify a customer that their requested text edits have been applied to their live site."""
    biz = business_name or "your website"
    html = _wrapper(
        preheader="Your requested changes are now live on your Groundwork website.",
        heading="Your changes are live",
        body_html=(
            f"Good news — the text changes you requested for <strong>{escape(biz)}</strong> "
            f"have been applied and are now showing on your live website. "
            f"If you'd like to make any further changes, just visit your account and use the editor."
        ),
        cta_url="https://groundworkbuild.com/account",
        cta_label="View your account →",
    )
    _send(to_email, f"Your changes are live — {escape(biz)}", html)


def send_domain_order_admin_email(domain: str, price_gbp: float, customer_email: str,
                                   site_id: str, automated: bool = False) -> None:
    """Audit trail to admin inbox when a domain order is processed. automated=True means
    registration ran automatically; False means it still needs manual action."""
    support_inbox = _env_email_or_warn("SUPPORT_INBOX_EMAIL")
    d = escape(domain)
    e = escape(customer_email)
    s = escape(site_id)
    if automated:
        heading = "Domain order — automatically processed"
        note = "Domain registration ran automatically. No action needed — this is an audit trail."
        badge_bg = "#D1FAE5"
        badge_color = "#065F46"
        badge_text = "Automated"
    else:
        heading = "Domain order — manual action required"
        note = "Automation did not run (or failed separately). Please register this domain manually via Porkbun."
        badge_bg = "#FEF3C7"
        badge_color = "#92400E"
        badge_text = "Manual"
    html = f"""<div style="font-family:Arial,Helvetica,sans-serif;background:#F5F3EE;padding:32px 20px;">
  <div style="max-width:480px;margin:0 auto;background:#fff;border-radius:12px;overflow:hidden;">
    <div style="background:#1C1C1C;padding:20px 28px;">
      <span style="color:{ACCENT};font-weight:800;font-size:18px;letter-spacing:-.03em;">Groundwork</span>
    </div>
    <div style="padding:28px;">
      <div style="display:inline-block;background:{badge_bg};color:{badge_color};font-size:12px;font-weight:700;padding:3px 10px;border-radius:20px;margin-bottom:12px;">{badge_text}</div>
      <h2 style="margin:0 0 8px;font-size:19px;color:#1C1C1C;">{heading}</h2>
      <p style="margin:0 0 20px;font-size:14px;color:#5C5A56;">{note}</p>
      <table style="width:100%;border-collapse:collapse;">
        <tr><td style="padding:8px 0;font-size:14px;color:#5C5A56;width:110px;">Domain</td><td style="padding:8px 0;font-size:14px;font-weight:700;color:#1C1C1C;">{d}</td></tr>
        <tr><td style="padding:8px 0;font-size:14px;color:#5C5A56;">Amount paid</td><td style="padding:8px 0;font-size:14px;color:#1C1C1C;">£{price_gbp:.2f}/yr</td></tr>
        <tr><td style="padding:8px 0;font-size:14px;color:#5C5A56;">Customer email</td><td style="padding:8px 0;font-size:14px;color:#1C1C1C;">{e}</td></tr>
        <tr><td style="padding:8px 0;font-size:14px;color:#5C5A56;">Site ID</td><td style="padding:8px 0;font-size:14px;color:#1C1C1C;">{s}</td></tr>
      </table>
    </div>
  </div>
</div>"""
    _send(support_inbox, f"Domain order: {domain}", html)


def send_admin_payment_received_email(business_name: str, customer_email: str, job_id: str, was_reactivation: bool) -> None:
    """Notify the admin inbox whenever a checkout.session.completed webhook
    confirms real payment (added 2026-07-19) — covers both a first-time
    go-live and a reactivation of a previously-cancelled site, distinguished
    in the copy since they're different events worth knowing apart."""
    support_inbox = _env_email_or_warn("SUPPORT_INBOX_EMAIL")
    biz = escape(business_name or "Unknown business")
    e = escape(customer_email or "")
    heading = "Site reactivated" if was_reactivation else "New paying customer"
    html = f"""<div style="font-family:Arial,Helvetica,sans-serif;background:#F5F3EE;padding:32px 20px;">
  <div style="max-width:480px;margin:0 auto;background:#fff;border-radius:12px;overflow:hidden;">
    <div style="background:#1C1C1C;padding:20px 28px;">
      <span style="color:{ACCENT};font-weight:800;font-size:18px;letter-spacing:-.03em;">Groundwork</span>
    </div>
    <div style="padding:28px;">
      <div style="display:inline-block;background:#D1FAE5;color:#065F46;font-size:12px;font-weight:700;padding:3px 10px;border-radius:20px;margin-bottom:12px;">{"Reactivation" if was_reactivation else "New signup"}</div>
      <h2 style="margin:0 0 8px;font-size:19px;color:#1C1C1C;">{heading}</h2>
      <table style="width:100%;border-collapse:collapse;">
        <tr><td style="padding:8px 0;font-size:14px;color:#5C5A56;width:110px;">Business</td><td style="padding:8px 0;font-size:14px;font-weight:700;color:#1C1C1C;">{biz}</td></tr>
        <tr><td style="padding:8px 0;font-size:14px;color:#5C5A56;">Customer email</td><td style="padding:8px 0;font-size:14px;color:#1C1C1C;">{e}</td></tr>
      </table>
      <p style="margin:20px 0 0;"><a href="https://groundworkbuild.com/admin/generations" style="color:{ACCENT};font-size:14px;font-weight:600;text-decoration:none;">View sites →</a></p>
    </div>
  </div>
</div>"""
    _send(support_inbox, f"{heading} — {business_name or 'Unknown business'}", html)


def send_admin_daily_summary_email(day_label: str, emails_sent: int, clicked: int, click_pct: float,
                                    signed_up: int, signup_pct: float) -> None:
    """Once-a-day admin digest (added 2026-07-23), replacing the old
    per-click notification (removed the same day — real-time pings on
    every click had no signal value at real volume). Sent once, at the end
    of the day's send window, by outreach/send_daily_summary.py — not
    tied to any single event, so there's no idempotency guard here; that
    script's own cron-once-a-day schedule is what keeps this to one send."""
    support_inbox = _env_email_or_warn("SUPPORT_INBOX_EMAIL")
    html = f"""<div style="font-family:Arial,Helvetica,sans-serif;background:#F5F3EE;padding:32px 20px;">
  <div style="max-width:480px;margin:0 auto;background:#fff;border-radius:12px;overflow:hidden;">
    <div style="background:#1C1C1C;padding:20px 28px;">
      <span style="color:{ACCENT};font-weight:800;font-size:18px;letter-spacing:-.03em;">Groundwork</span>
    </div>
    <div style="padding:28px;">
      <h2 style="margin:0 0 4px;font-size:19px;color:#1C1C1C;">Today's outreach summary</h2>
      <p style="margin:0 0 20px;font-size:13px;color:#9A9893;">{escape(day_label)}</p>
      <table style="width:100%;border-collapse:collapse;">
        <tr><td style="padding:10px 0;font-size:14px;color:#5C5A56;border-bottom:1px solid #EDEBE5;">Emails sent</td><td style="padding:10px 0;font-size:16px;font-weight:700;color:#1C1C1C;text-align:right;border-bottom:1px solid #EDEBE5;">{emails_sent}</td></tr>
        <tr><td style="padding:10px 0;font-size:14px;color:#5C5A56;border-bottom:1px solid #EDEBE5;">Clicked magic link</td><td style="padding:10px 0;font-size:16px;font-weight:700;color:#1C1C1C;text-align:right;border-bottom:1px solid #EDEBE5;">{clicked} <span style="font-size:13px;font-weight:600;color:#5C5A56;">({click_pct:.1f}%)</span></td></tr>
        <tr><td style="padding:10px 0;font-size:14px;color:#5C5A56;">Signed up (free trial)</td><td style="padding:10px 0;font-size:16px;font-weight:700;color:#1C1C1C;text-align:right;">{signed_up} <span style="font-size:13px;font-weight:600;color:#5C5A56;">({signup_pct:.1f}%)</span></td></tr>
      </table>
      <p style="margin:20px 0 0;"><a href="https://groundworkbuild.com/admin/funnel" style="color:{ACCENT};font-size:14px;font-weight:600;text-decoration:none;">View full funnel →</a></p>
    </div>
  </div>
</div>"""
    _send(support_inbox, f"Daily summary — {emails_sent} sent, {clicked} clicked, {signed_up} signed up", html)


def send_domain_order_customer_email(to_email: str, domain: str, business_name: str) -> None:
    """Confirm to the customer that their domain order has been received."""
    biz = business_name or "your website"
    html = _wrapper(
        preheader=f"Your domain order for {domain} has been received.",
        heading="Domain order confirmed",
        body_html=(
            f"Thanks — we've received your payment for <strong>{escape(domain)}</strong>. "
            f"We'll register it and connect it to <strong>{escape(biz)}</strong> within 1 working day. "
            f"You'll hear from us once it's live."
        ),
        cta_url="https://groundworkbuild.com/account",
        cta_label="View your account →",
    )
    _send(to_email, f"Domain order confirmed — {escape(domain)}", html)


def send_domain_setup_failed_email(domain: str, site_id: str, customer_email: str,
                                    price_gbp: float, failed_step: str, error_msg: str) -> None:
    """Alert admin that automated domain setup failed and manual intervention is needed."""
    support_inbox = _env_email_or_warn("SUPPORT_INBOX_EMAIL")
    d = escape(domain)
    e = escape(customer_email)
    s = escape(site_id)
    step_esc = escape(failed_step)
    err_esc = escape(error_msg[:500])
    html = f"""<div style="font-family:Arial,Helvetica,sans-serif;background:#F5F3EE;padding:32px 20px;">
  <div style="max-width:480px;margin:0 auto;background:#fff;border-radius:12px;overflow:hidden;">
    <div style="background:#1C1C1C;padding:20px 28px;">
      <span style="color:{ACCENT};font-weight:800;font-size:18px;letter-spacing:-.03em;">Groundwork</span>
    </div>
    <div style="padding:28px;">
      <div style="display:inline-block;background:#FEE2E2;color:#991B1B;font-size:12px;font-weight:700;padding:3px 10px;border-radius:20px;margin-bottom:12px;">Action required</div>
      <h2 style="margin:0 0 8px;font-size:19px;color:#1C1C1C;">Domain setup failed — manual action needed</h2>
      <p style="margin:0 0 20px;font-size:14px;color:#5C5A56;">Automated domain registration failed at step <strong>{step_esc}</strong>. The customer has paid — please complete setup manually.</p>
      <table style="width:100%;border-collapse:collapse;">
        <tr><td style="padding:8px 0;font-size:14px;color:#5C5A56;width:110px;">Domain</td><td style="padding:8px 0;font-size:14px;font-weight:700;color:#1C1C1C;">{d}</td></tr>
        <tr><td style="padding:8px 0;font-size:14px;color:#5C5A56;">Amount paid</td><td style="padding:8px 0;font-size:14px;color:#1C1C1C;">£{price_gbp:.2f}/yr</td></tr>
        <tr><td style="padding:8px 0;font-size:14px;color:#5C5A56;">Customer email</td><td style="padding:8px 0;font-size:14px;color:#1C1C1C;">{e}</td></tr>
        <tr><td style="padding:8px 0;font-size:14px;color:#5C5A56;">Site ID</td><td style="padding:8px 0;font-size:14px;color:#1C1C1C;">{s}</td></tr>
        <tr><td style="padding:8px 0;font-size:14px;color:#5C5A56;">Failed step</td><td style="padding:8px 0;font-size:14px;color:#DC2626;">{step_esc}</td></tr>
      </table>
      <div style="margin-top:16px;background:#FEF2F2;border-radius:8px;padding:14px 16px;">
        <div style="font-size:12px;font-weight:700;color:#9A9893;text-transform:uppercase;letter-spacing:.06em;margin-bottom:6px;">Error detail</div>
        <div style="font-size:13px;color:#1C1C1C;word-break:break-word;white-space:pre-wrap;">{err_esc}</div>
      </div>
      <div style="margin-top:16px;background:#F5F3EE;border-radius:8px;padding:14px 16px;">
        <div style="font-size:12px;font-weight:700;color:#9A9893;text-transform:uppercase;letter-spacing:.06em;margin-bottom:6px;">Manual setup steps</div>
        <div style="font-size:13px;color:#1C1C1C;line-height:1.6;">
          1. In the Cloudflare dashboard, go to the <strong>groundworkbuild.com</strong> zone → SSL/TLS → Custom Hostnames, and add <strong>{d}</strong> and <strong>www.{d}</strong> as separate Custom Hostnames (DV SSL, HTTP validation).<br>
          2. In Porkbun, point <strong>{d}</strong>'s DNS at our Cloudflare target: an ALIAS record at the root and a CNAME record for <strong>www</strong>, both targeting our Cloudflare for SaaS CNAME target (e.g. <strong>connect.groundworkbuild.com</strong>).<br>
          3. Wait for both Custom Hostnames to show <strong>Active</strong> in Cloudflare, then mark this Domain row's status <strong>active</strong> in the admin.
        </div>
      </div>
    </div>
  </div>
</div>"""
    _send(support_inbox, f"FAILED domain setup: {domain}", html)


def send_domain_live_email(to_email: str, domain: str, business_name: str) -> None:
    """Notify customer that their domain is registered and connected to their site."""
    biz = business_name or "your website"
    html = _wrapper(
        preheader=f"Your domain {domain} is now live and connected to your Groundwork site.",
        heading="Your domain is live",
        body_html=(
            f"Great news — <strong>{escape(domain)}</strong> has been registered and connected to "
            f"<strong>{escape(biz)}</strong>. DNS changes can take up to a few hours to propagate "
            f"globally, so your site may appear at the new address very soon if it doesn't already."
        ),
        cta_url="https://groundworkbuild.com/account",
        cta_label="View your account →",
    )
    _send(to_email, f"Your domain is live — {escape(domain)}", html)


def send_outreach_email(to_email: str, subject: str, html_body: str, unsubscribe_link: str,
                        reply_to: str = None) -> str:
    """
    Cold outreach / follow-up send (outreach/followup.py, outreach/templates.py).
    Sent from a separate subdomain (Section 15 deliverability) via
    OUTREACH_RESEND_FROM_EMAIL, not the transactional RESEND_FROM_EMAIL used
    by the rest of this file — keeps cold-outreach sender reputation isolated
    from the transactional generation-flow emails.

    reply_to defaults to OUTREACH_REPLY_TO_EMAIL (falling back to a literal
    reply@groundworkbuild.com on the root domain) — deliberately NOT the
    same address as `from`. Replies don't carry the sending subdomain's
    reputation risk (they're not bulk/cold sends), so there's no reason
    they need to land on the isolated subdomain — and landing them on the
    root domain instead means Cloudflare Email Routing only needs a rule
    on the root zone, which its guided rule-builder actually supports
    (confirmed: the domain picker there only offers the root zone, not
    arbitrary subdomains — see Section 11a).

    html_body is the finalized, fully-designed HTML template from
    outreach/templates.py (2026-07-14 format change from plain text) — sent
    to Resend's "html" field as-is, with a plain-text alternative derived
    automatically via _html_to_text so filters that read text/plain still
    get a faithful version (mail-tester docks HTML-only sends).

    Returns the Resend-assigned email id on success, or None on failure/skip
    — previously this was discarded entirely (resend.Emails.send(...) called
    but its return value never captured), so a caller had no way to prove a
    specific send actually happened beyond "no exception was raised." Every
    caller (outreach/send_job.py, outreach/followup.py, outreach/send_test.py)
    should log/print this id, not just call the function and move on.
    """
    api_key = os.environ.get("RESEND_API_KEY")
    outreach_from = os.environ.get("OUTREACH_RESEND_FROM_EMAIL")
    if outreach_from:
        from_email = outreach_from
    else:
        print("[emails] WARNING: OUTREACH_RESEND_FROM_EMAIL not set — falling back to RESEND_FROM_EMAIL "
              "(cold-outreach sends will share the transactional sender's reputation, defeating the point "
              "of the separate subdomain — see send_outreach_email's docstring)")
        from_email = _env_email_or_warn("RESEND_FROM_EMAIL")
    reply_to = reply_to or os.environ.get("OUTREACH_REPLY_TO_EMAIL", "reply@groundworkbuild.com")

    if not api_key:
        print(f"[emails] RESEND_API_KEY not set — skipping outreach send of '{subject}' to {to_email}")
        return None

    resend.api_key = api_key
    try:
        result = resend.Emails.send({
            "from": from_email,
            "to": [to_email],
            "subject": subject,
            "html": html_body,
            "text": _html_to_text(html_body),
            "reply_to": reply_to,
            "headers": {
                "List-Unsubscribe": f"<{unsubscribe_link}>",
                "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
            },
        })
        email_id = result.get("id") if isinstance(result, dict) else getattr(result, "id", None)
        print(f"[emails] outreach email sent to {to_email}: resend id={email_id}")
        return email_id
    except Exception as exc:
        print(f"[emails] failed to send outreach email '{subject}' to {to_email}: {exc}")
        return None


def send_support_message_email(from_email: str, message: str, *, business_name: str = None, subdomain: str = None) -> None:
    """
    Forwards a message submitted from the account dashboard straight to the
    support inbox, with reply_to set to the customer's email. Includes
    business name and site URL when available so the team has context.
    """
    support_inbox = _env_email_or_warn("SUPPORT_INBOX_EMAIL")
    meta_rows = f'<p style="margin:0 0 4px;font-size:13.5px;color:#807E79;">From: {escape(from_email)}</p>'
    if business_name:
        meta_rows += f'<p style="margin:0 0 4px;font-size:13.5px;color:#807E79;">Business: {escape(business_name)}</p>'
    if subdomain:
        site_url = f"{escape(subdomain)}.groundworkbuild.com"
        meta_rows += f'<p style="margin:0 0 4px;font-size:13.5px;color:#807E79;">Site: <a href="https://{site_url}" style="color:#3B82F6;">{site_url}</a></p>'
    subject_prefix = f"{business_name} — " if business_name else ""
    html = f"""<div style="font-family:Arial,Helvetica,sans-serif;background:#F5F3EE;padding:32px 20px;">
  <div style="max-width:480px;margin:0 auto;background:#fff;border-radius:12px;overflow:hidden;">
    <div style="background:#1C1C1C;padding:20px 28px;">
      <span style="color:{ACCENT};font-weight:800;font-size:18px;letter-spacing:-.03em;">Groundwork</span>
    </div>
    <div style="padding:28px;">
      <h2 style="margin:0 0 12px;font-size:19px;color:#1C1C1C;">New dashboard message</h2>
      {meta_rows}
      <div style="white-space:pre-wrap;font-size:15px;line-height:1.6;color:#1C1C1C;border-top:1px solid #E6E3DC;padding-top:16px;margin-top:14px;">{escape(message)}</div>
    </div>
  </div>
</div>"""
    _send(support_inbox, f"{subject_prefix}Dashboard message from {from_email}", html, reply_to=from_email)
