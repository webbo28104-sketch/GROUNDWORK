"""
Groundwork — transactional email via Resend.

Templates: verification (magic link to trigger generation), resend (magic
link to view previously generated sites), and password reset. All plain,
single-CTA emails matching the funnel's existing brand.
"""
import os

import resend

ACCENT = "#3B82F6"


def _send(to_email: str, subject: str, html_content: str) -> None:
    api_key = os.environ.get("RESEND_API_KEY")
    from_email = os.environ.get("RESEND_FROM_EMAIL", "groundwork-build@outlook.com")

    if not api_key:
        print(f"[emails] RESEND_API_KEY not set — skipping send of '{subject}' to {to_email}")
        return

    resend.api_key = api_key
    try:
        resend.Emails.send({
            "from": from_email,
            "to": [to_email],
            "subject": subject,
            "html": html_content,
        })
    except Exception as exc:
        print(f"[emails] failed to send '{subject}' to {to_email}: {exc}")


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


def send_password_reset_email(to_email: str, reset_url: str) -> None:
    html = _wrapper(
        preheader="You're receiving this because a password reset was requested for this Groundwork account.",
        heading="Reset your password",
        body_html="Click the button below to choose a new password for your Groundwork account. This link expires in 1 hour. If you didn't request this, you can safely ignore this email — your password won't change.",
        cta_url=reset_url,
        cta_label="Reset my password →",
    )
    _send(to_email, "Reset your password — Groundwork", html)
