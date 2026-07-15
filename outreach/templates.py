"""
Groundwork outreach — initial + follow-up template copy.

Placeholders substituted by the caller: {business_name}, {preview_link},
{short_code}, {unsubscribe_link}, {branding_ps} (stages C/D only — see
branding_ps_line() below).

Content accuracy rule (docs/outreach-pipeline-spec.md Section 10c): templates
for the "sent" and "opened" substages (pre-click) must never claim the site
is already built — generation only happens after a real click. Only
"clicked_generated" and "account_created" substages (post-click) may say
the site is built/ready. Do not paraphrase this distinction away when
editing copy — enforced by test_templates_content_accuracy in tests.

Bodies below are full HTML (finalized designs, reviewed 2026-07-14) —
send_outreach_email (emails.py) sends this HTML directly via Resend's
"html" field and derives the plain-text alternative automatically, rather
than escaping/wrapping plain text as it did before this format change.
"""

INITIAL_EMAIL = {
    "subject": '{business_name} — see a website built for you, no cost',
    "body": """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Type" content="text/html; charset=UTF-8">
<title>{business_name} — see a website built for you, no cost</title>

</head>
<body style="margin:0;padding:0;background:#EDEAE2;font-family:Arial,Helvetica,sans-serif;">
<!-- Preheader (hidden, shows in inbox preview text) -->
<div style="display:none;max-height:0;overflow:hidden;mso-hide:all;">We build affordable websites for UK trade businesses — click for a free preview.</div>

<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#EDEAE2;">
<tbody><tr><td align="center" style="padding:40px 16px;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="max-width:560px;background:#FFFFFF;">

  <!-- HEADER: small logo mark + wordmark, thin brand-blue rule underneath —
       the same accent treatment as the site's nav, without a heavy colour
       block. Solid-colour cells (no images beyond the 24px mark) keep this
       spam-filter-friendly. -->
  <tbody><tr><td style="padding:28px 32px 18px;border-bottom:2px solid #3B82F6;">
    <table role="presentation" cellpadding="0" cellspacing="0" border="0">
      <tbody><tr>
        <td style="padding:0 9px 0 0;vertical-align:middle;">
          <img src="https://groundworkbuild.com/assets/email/groundwork-mark-22.png" width="22" height="22" alt="" style="display:block;border-radius:5px;">
        </td>
        <td style="vertical-align:middle;">
          <span style="font-family:Arial,Helvetica,sans-serif;font-size:17px;font-weight:bold;color:#1C1C1C;letter-spacing:-.01em;">Groundwork</span>
        </td>
      </tr>
    </tbody></table>
  </td></tr>

  <!-- BODY -->
  <tr><td style="padding:32px 32px 8px;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
      <tbody><tr><td style="font-family:Arial,Helvetica,sans-serif;font-size:15.5px;line-height:1.65;color:#2A2A28;padding:0 0 18px;">
        Hi {business_name} team,
      </td></tr>
      <tr><td style="font-family:Arial,Helvetica,sans-serif;font-size:15.5px;line-height:1.65;color:#2A2A28;padding:0 0 18px;">
        We're Groundwork — we build affordable websites for UK trade businesses, without the agency price tag or hassle.
      </td></tr>
      <tr><td style="font-family:Arial,Helvetica,sans-serif;font-size:15.5px;line-height:1.65;color:#2A2A28;padding:0 0 30px;">
        Click below and we'll build a free preview for {business_name}, tailored to your trade and area — no cost, no obligation to take it further.
      </td></tr>

      <!-- CTA: single button, reused as-is across all 8 templates. Table-based
           bulletproof button — renders correctly in Outlook desktop, which
           ignores border-radius/padding on plain <a> and <button> tags. -->
      <tr><td style="padding:0 0 34px;">
        <table role="presentation" cellpadding="0" cellspacing="0" border="0">
          <tbody><tr><td bgcolor="#3B82F6" style="border-radius:8px;">
            <a href="{preview_link}" target="_blank" style="display:inline-block;padding:13px 24px;font-family:Arial,Helvetica,sans-serif;font-size:15px;font-weight:bold;color:#FFFFFF;text-decoration:none;border-radius:8px;">See your free preview</a>
          </td></tr>
        </tbody></table>
      </td></tr>

      <tr><td style="font-family:Arial,Helvetica,sans-serif;font-size:15.5px;line-height:1.65;color:#2A2A28;padding:0 0 8px;">
        If you like what you see, going live is £99 setup + £24.99/month, first month free — most other website services charge around £89 a month alone. Any questions, just reply to this email.
      </td></tr>
      <tr><td style="padding:6px 0 26px;">
        <table role="presentation" cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse;font-family:Arial,Helvetica,sans-serif;">
        <tbody><tr>
          <td valign="middle" style="padding:0 18px 0 0;vertical-align:middle;">
            <img src="https://groundworkbuild.com/assets/email/groundwork-mark-48.png" width="48" height="48" alt="Groundwork" style="display:block;border-radius:10px;">
          </td>
          <td valign="middle" style="border-left:2px solid #3B82F6;padding:0 0 0 18px;vertical-align:middle;">
            <table role="presentation" cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse;">
              <tbody><tr><td style="font-family:Arial,Helvetica,sans-serif;font-size:16px;font-weight:bold;color:#1C1C1C;padding:0 0 2px;line-height:1.3;">Charlie</td></tr>
              <tr><td style="font-family:Arial,Helvetica,sans-serif;font-size:13px;color:#5C5A56;padding:0 0 10px;line-height:1.3;">Founder, Groundwork</td></tr>
              <tr><td style="font-family:Arial,Helvetica,sans-serif;font-size:13px;line-height:1.3;padding:0;">
                <a href="https://groundworkbuild.com" style="color:#2257CC;text-decoration:none;">groundworkbuild.com</a><span style="color:#D9D7D0;padding:0 8px;">|</span><a href="mailto:reply@groundworkbuild.com" style="color:#2257CC;text-decoration:none;">reply@groundworkbuild.com</a>
              </td></tr>
            </tbody></table>
      </td></tr>
    </tbody></table>
  </td></tr>

  <!-- FOOTER: understated, matches the site footer's muted-label treatment —
       small caps, quiet colour, thin top rule as the only separator. -->
  <tr><td style="font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.65;color:#5C5A56;padding:0 32px 22px;">P.S. — here's a real site we've built recently, live now: <a href="https://sussexleadcraftltd.com" style="color:#2257CC;text-decoration:none;">sussexleadcraftltd.com</a></td></tr>
<tr><td style="padding:26px 32px 28px;border-top:1px solid #E2E0DA;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
      <tbody><tr><td style="font-family:Arial,Helvetica,sans-serif;font-size:11.5px;font-weight:bold;letter-spacing:.06em;text-transform:uppercase;color:#9A9893;padding:0 0 8px;">
        Groundwork
      </td></tr>
      <tr><td style="font-family:Arial,Helvetica,sans-serif;font-size:12.5px;line-height:1.65;color:#9A9893;">
                <a href="{unsubscribe_link}" style="color:#9A9893;text-decoration:underline;">Unsubscribe</a> or reply and let me know and I won't email again.
      </td></tr>
    </tbody></table>
  </td></tr>

</tbody></table>
</td></tr>
</tbody></table>



</td></tr></tbody></table></body></html>""",
}

INITIAL_SMS = (
    "Hi {business_name}, this is Groundwork — we build affordable websites for UK trades. "
    "See a free preview built for you: groundworkbuild.com/s/{short_code}\n"
    "£99 setup + £24.99/mo after, 1st month free.\n"
    "Reply STOP to opt out."
)

# ── Follow-up stages ──────────────────────────────────────────────────────────
# Stage A: sent, never opened (pre-click — no "built" claim)
# Stage B: opened, never clicked (pre-click — no "built" claim)
# Stage C: clicked_generated, no payment (post-click — site is built)
# Stage D: account_created, no payment (post-click — site is built)

FOLLOWUP_EMAIL = {
    "A": {
        "subject": '{business_name} — did this land?',
        "body": """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Type" content="text/html; charset=UTF-8">
<title>{business_name} — did this land?</title>

</head>
<body style="margin:0;padding:0;background:#EDEAE2;font-family:Arial,Helvetica,sans-serif;">
<!-- Preheader (hidden, shows in inbox preview text) -->
<div style="display:none;max-height:0;overflow:hidden;mso-hide:all;">Quick follow-up in case this got missed.</div>

<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#EDEAE2;">
<tbody><tr><td align="center" style="padding:40px 16px;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="max-width:560px;background:#FFFFFF;">

  <!-- HEADER: small logo mark + wordmark, thin brand-blue rule underneath —
       the same accent treatment as the site's nav, without a heavy colour
       block. Solid-colour cells (no images beyond the 24px mark) keep this
       spam-filter-friendly. -->
  <tbody><tr><td style="padding:28px 32px 18px;border-bottom:2px solid #3B82F6;">
    <table role="presentation" cellpadding="0" cellspacing="0" border="0">
      <tbody><tr>
        <td style="padding:0 9px 0 0;vertical-align:middle;">
          <img src="https://groundworkbuild.com/assets/email/groundwork-mark-22.png" width="22" height="22" alt="" style="display:block;border-radius:5px;">
        </td>
        <td style="vertical-align:middle;">
          <span style="font-family:Arial,Helvetica,sans-serif;font-size:17px;font-weight:bold;color:#1C1C1C;letter-spacing:-.01em;">Groundwork</span>
        </td>
      </tr>
    </tbody></table>
  </td></tr>

  <!-- BODY -->
  <tr><td style="padding:32px 32px 8px;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
      <tbody><tr><td style="font-family:Arial,Helvetica,sans-serif;font-size:15.5px;line-height:1.65;color:#2A2A28;padding:0 0 18px;">
        Hi {business_name} team,
      </td></tr>
      <tr><td style="font-family:Arial,Helvetica,sans-serif;font-size:15.5px;line-height:1.65;color:#2A2A28;padding:0 0 18px;">
        Quick follow-up in case this got missed.
      </td></tr>
      <tr><td style="font-family:Arial,Helvetica,sans-serif;font-size:15.5px;line-height:1.65;color:#2A2A28;padding:0 0 30px;">
        Click below and we'll build a free website preview for {business_name}, no cost.
      </td></tr>

      <!-- CTA: single button, reused as-is across all 8 templates. Table-based
           bulletproof button — renders correctly in Outlook desktop, which
           ignores border-radius/padding on plain <a> and <button> tags. -->
      <tr><td style="padding:0 0 34px;">
        <table role="presentation" cellpadding="0" cellspacing="0" border="0">
          <tbody><tr><td bgcolor="#3B82F6" style="border-radius:8px;">
            <a href="{preview_link}" target="_blank" style="display:inline-block;padding:13px 24px;font-family:Arial,Helvetica,sans-serif;font-size:15px;font-weight:bold;color:#FFFFFF;text-decoration:none;border-radius:8px;">See your free preview</a>
          </td></tr>
        </tbody></table>
      </td></tr>

      <tr><td style="font-family:Arial,Helvetica,sans-serif;font-size:15.5px;line-height:1.65;color:#2A2A28;padding:0 0 8px;">
        Any questions, just reply to this email.
      </td></tr>
      <tr><td style="padding:6px 0 26px;">
        <table role="presentation" cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse;font-family:Arial,Helvetica,sans-serif;">
        <tbody><tr>
          <td valign="middle" style="padding:0 18px 0 0;vertical-align:middle;">
            <img src="https://groundworkbuild.com/assets/email/groundwork-mark-48.png" width="48" height="48" alt="Groundwork" style="display:block;border-radius:10px;">
          </td>
          <td valign="middle" style="border-left:2px solid #3B82F6;padding:0 0 0 18px;vertical-align:middle;">
            <table role="presentation" cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse;">
              <tbody><tr><td style="font-family:Arial,Helvetica,sans-serif;font-size:16px;font-weight:bold;color:#1C1C1C;padding:0 0 2px;line-height:1.3;">Charlie</td></tr>
              <tr><td style="font-family:Arial,Helvetica,sans-serif;font-size:13px;color:#5C5A56;padding:0 0 10px;line-height:1.3;">Founder, Groundwork</td></tr>
              <tr><td style="font-family:Arial,Helvetica,sans-serif;font-size:13px;line-height:1.3;padding:0;">
                <a href="https://groundworkbuild.com" style="color:#2257CC;text-decoration:none;">groundworkbuild.com</a><span style="color:#D9D7D0;padding:0 8px;">|</span><a href="mailto:reply@groundworkbuild.com" style="color:#2257CC;text-decoration:none;">reply@groundworkbuild.com</a>
              </td></tr>
            </tbody></table>
      </td></tr>
    </tbody></table>
  </td></tr>

  <!-- FOOTER: understated, matches the site footer's muted-label treatment —
       small caps, quiet colour, thin top rule as the only separator. -->
  <tr><td style="padding:26px 32px 28px;border-top:1px solid #E2E0DA;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
      <tbody><tr><td style="font-family:Arial,Helvetica,sans-serif;font-size:11.5px;font-weight:bold;letter-spacing:.06em;text-transform:uppercase;color:#9A9893;padding:0 0 8px;">
        Groundwork
      </td></tr>
      <tr><td style="font-family:Arial,Helvetica,sans-serif;font-size:12.5px;line-height:1.65;color:#9A9893;">
                <a href="{unsubscribe_link}" style="color:#9A9893;text-decoration:underline;">Unsubscribe</a> or reply and let me know and I won't email again.
      </td></tr>
    </tbody></table>
  </td></tr>

</tbody></table>
</td></tr>
</tbody></table>



</td></tr></tbody></table></body></html>""",
    },
    "B": {
        "subject": "Still there — {business_name}'s website preview",
        "body": """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Type" content="text/html; charset=UTF-8">
<title>Still there — {business_name}'s website preview</title>

</head>
<body style="margin:0;padding:0;background:#EDEAE2;font-family:Arial,Helvetica,sans-serif;">
<!-- Preheader (hidden, shows in inbox preview text) -->
<div style="display:none;max-height:0;overflow:hidden;mso-hide:all;">Following up on your free website preview.</div>

<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#EDEAE2;">
<tbody><tr><td align="center" style="padding:40px 16px;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="max-width:560px;background:#FFFFFF;">

  <!-- HEADER: small logo mark + wordmark, thin brand-blue rule underneath —
       the same accent treatment as the site's nav, without a heavy colour
       block. Solid-colour cells (no images beyond the 24px mark) keep this
       spam-filter-friendly. -->
  <tbody><tr><td style="padding:28px 32px 18px;border-bottom:2px solid #3B82F6;">
    <table role="presentation" cellpadding="0" cellspacing="0" border="0">
      <tbody><tr>
        <td style="padding:0 9px 0 0;vertical-align:middle;">
          <img src="https://groundworkbuild.com/assets/email/groundwork-mark-22.png" width="22" height="22" alt="" style="display:block;border-radius:5px;">
        </td>
        <td style="vertical-align:middle;">
          <span style="font-family:Arial,Helvetica,sans-serif;font-size:17px;font-weight:bold;color:#1C1C1C;letter-spacing:-.01em;">Groundwork</span>
        </td>
      </tr>
    </tbody></table>
  </td></tr>

  <!-- BODY -->
  <tr><td style="padding:32px 32px 8px;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
      <tbody><tr><td style="font-family:Arial,Helvetica,sans-serif;font-size:15.5px;line-height:1.65;color:#2A2A28;padding:0 0 18px;">
        Hi {business_name} team,
      </td></tr>
      <tr><td style="font-family:Arial,Helvetica,sans-serif;font-size:15.5px;line-height:1.65;color:#2A2A28;padding:0 0 18px;">
        Following up on your free website preview.
      </td></tr>
      <tr><td style="font-family:Arial,Helvetica,sans-serif;font-size:15.5px;line-height:1.65;color:#2A2A28;padding:0 0 30px;">
        Click below and we'll build it for {business_name} right there, no cost.
      </td></tr>

      <!-- CTA: single button, reused as-is across all 8 templates. Table-based
           bulletproof button — renders correctly in Outlook desktop, which
           ignores border-radius/padding on plain <a> and <button> tags. -->
      <tr><td style="padding:0 0 34px;">
        <table role="presentation" cellpadding="0" cellspacing="0" border="0">
          <tbody><tr><td bgcolor="#3B82F6" style="border-radius:8px;">
            <a href="{preview_link}" target="_blank" style="display:inline-block;padding:13px 24px;font-family:Arial,Helvetica,sans-serif;font-size:15px;font-weight:bold;color:#FFFFFF;text-decoration:none;border-radius:8px;">See your free preview</a>
          </td></tr>
        </tbody></table>
      </td></tr>

      <tr><td style="font-family:Arial,Helvetica,sans-serif;font-size:15.5px;line-height:1.65;color:#2A2A28;padding:0 0 8px;">
        Any questions, just reply to this email.
      </td></tr>
      <tr><td style="padding:6px 0 26px;">
        <table role="presentation" cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse;font-family:Arial,Helvetica,sans-serif;">
        <tbody><tr>
          <td valign="middle" style="padding:0 18px 0 0;vertical-align:middle;">
            <img src="https://groundworkbuild.com/assets/email/groundwork-mark-48.png" width="48" height="48" alt="Groundwork" style="display:block;border-radius:10px;">
          </td>
          <td valign="middle" style="border-left:2px solid #3B82F6;padding:0 0 0 18px;vertical-align:middle;">
            <table role="presentation" cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse;">
              <tbody><tr><td style="font-family:Arial,Helvetica,sans-serif;font-size:16px;font-weight:bold;color:#1C1C1C;padding:0 0 2px;line-height:1.3;">Charlie</td></tr>
              <tr><td style="font-family:Arial,Helvetica,sans-serif;font-size:13px;color:#5C5A56;padding:0 0 10px;line-height:1.3;">Founder, Groundwork</td></tr>
              <tr><td style="font-family:Arial,Helvetica,sans-serif;font-size:13px;line-height:1.3;padding:0;">
                <a href="https://groundworkbuild.com" style="color:#2257CC;text-decoration:none;">groundworkbuild.com</a><span style="color:#D9D7D0;padding:0 8px;">|</span><a href="mailto:reply@groundworkbuild.com" style="color:#2257CC;text-decoration:none;">reply@groundworkbuild.com</a>
              </td></tr>
            </tbody></table>
      </td></tr>
    </tbody></table>
  </td></tr>

  <!-- FOOTER: understated, matches the site footer's muted-label treatment —
       small caps, quiet colour, thin top rule as the only separator. -->
  <tr><td style="padding:26px 32px 28px;border-top:1px solid #E2E0DA;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
      <tbody><tr><td style="font-family:Arial,Helvetica,sans-serif;font-size:11.5px;font-weight:bold;letter-spacing:.06em;text-transform:uppercase;color:#9A9893;padding:0 0 8px;">
        Groundwork
      </td></tr>
      <tr><td style="font-family:Arial,Helvetica,sans-serif;font-size:12.5px;line-height:1.65;color:#9A9893;">
                <a href="{unsubscribe_link}" style="color:#9A9893;text-decoration:underline;">Unsubscribe</a> or reply and let me know and I won't email again.
      </td></tr>
    </tbody></table>
  </td></tr>

</tbody></table>
</td></tr>
</tbody></table>



</td></tr></tbody></table></body></html>""",
    },
    "C": {
        "subject": "Your website's ready, {business_name}",
        "body": """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Type" content="text/html; charset=UTF-8">
<title>Your website's ready, {business_name}</title>

</head>
<body style="margin:0;padding:0;background:#EDEAE2;font-family:Arial,Helvetica,sans-serif;">
<!-- Preheader (hidden, shows in inbox preview text) -->
<div style="display:none;max-height:0;overflow:hidden;mso-hide:all;">Your website's built and waiting.</div>

<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#EDEAE2;">
<tbody><tr><td align="center" style="padding:40px 16px;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="max-width:560px;background:#FFFFFF;">

  <!-- HEADER: small logo mark + wordmark, thin brand-blue rule underneath —
       the same accent treatment as the site's nav, without a heavy colour
       block. Solid-colour cells (no images beyond the 24px mark) keep this
       spam-filter-friendly. -->
  <tbody><tr><td style="padding:28px 32px 18px;border-bottom:2px solid #3B82F6;">
    <table role="presentation" cellpadding="0" cellspacing="0" border="0">
      <tbody><tr>
        <td style="padding:0 9px 0 0;vertical-align:middle;">
          <img src="https://groundworkbuild.com/assets/email/groundwork-mark-22.png" width="22" height="22" alt="" style="display:block;border-radius:5px;">
        </td>
        <td style="vertical-align:middle;">
          <span style="font-family:Arial,Helvetica,sans-serif;font-size:17px;font-weight:bold;color:#1C1C1C;letter-spacing:-.01em;">Groundwork</span>
        </td>
      </tr>
    </tbody></table>
  </td></tr>

  <!-- BODY -->
  <tr><td style="padding:32px 32px 8px;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
      <tbody><tr><td style="font-family:Arial,Helvetica,sans-serif;font-size:15.5px;line-height:1.65;color:#2A2A28;padding:0 0 18px;">
        Hi {business_name} team,
      </td></tr>
      <tr><td style="font-family:Arial,Helvetica,sans-serif;font-size:15.5px;line-height:1.65;color:#2A2A28;padding:0 0 18px;">
        Just checking in — your website's built and waiting.
      </td></tr>
      <tr><td style="font-family:Arial,Helvetica,sans-serif;font-size:15.5px;line-height:1.65;color:#2A2A28;padding:0 0 30px;">
        First month's free if you'd like to go live — £99 setup + £24.99/month after.
      </td></tr>

      <!-- CTA: single button, reused as-is across all 8 templates. Table-based
           bulletproof button — renders correctly in Outlook desktop, which
           ignores border-radius/padding on plain <a> and <button> tags. -->
      <tr><td style="padding:0 0 34px;">
        <table role="presentation" cellpadding="0" cellspacing="0" border="0">
          <tbody><tr><td bgcolor="#3B82F6" style="border-radius:8px;">
            <a href="{preview_link}" target="_blank" style="display:inline-block;padding:13px 24px;font-family:Arial,Helvetica,sans-serif;font-size:15px;font-weight:bold;color:#FFFFFF;text-decoration:none;border-radius:8px;">View your website</a>
          </td></tr>
        </tbody></table>
      </td></tr>

      <tr><td style="font-family:Arial,Helvetica,sans-serif;font-size:15.5px;line-height:1.65;color:#2A2A28;padding:0 0 8px;">
        Any questions, just reply to this email.
      </td></tr>
      {branding_ps}
      <tr><td style="padding:6px 0 26px;">
        <table role="presentation" cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse;font-family:Arial,Helvetica,sans-serif;">
        <tbody><tr>
          <td valign="middle" style="padding:0 18px 0 0;vertical-align:middle;">
            <img src="https://groundworkbuild.com/assets/email/groundwork-mark-48.png" width="48" height="48" alt="Groundwork" style="display:block;border-radius:10px;">
          </td>
          <td valign="middle" style="border-left:2px solid #3B82F6;padding:0 0 0 18px;vertical-align:middle;">
            <table role="presentation" cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse;">
              <tbody><tr><td style="font-family:Arial,Helvetica,sans-serif;font-size:16px;font-weight:bold;color:#1C1C1C;padding:0 0 2px;line-height:1.3;">Charlie</td></tr>
              <tr><td style="font-family:Arial,Helvetica,sans-serif;font-size:13px;color:#5C5A56;padding:0 0 10px;line-height:1.3;">Founder, Groundwork</td></tr>
              <tr><td style="font-family:Arial,Helvetica,sans-serif;font-size:13px;line-height:1.3;padding:0;">
                <a href="https://groundworkbuild.com" style="color:#2257CC;text-decoration:none;">groundworkbuild.com</a><span style="color:#D9D7D0;padding:0 8px;">|</span><a href="mailto:reply@groundworkbuild.com" style="color:#2257CC;text-decoration:none;">reply@groundworkbuild.com</a>
              </td></tr>
            </tbody></table>
      </td></tr>
    </tbody></table>
  </td></tr>

  <!-- FOOTER: understated, matches the site footer's muted-label treatment —
       small caps, quiet colour, thin top rule as the only separator. -->
  <tr><td style="padding:26px 32px 28px;border-top:1px solid #E2E0DA;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
      <tbody><tr><td style="font-family:Arial,Helvetica,sans-serif;font-size:11.5px;font-weight:bold;letter-spacing:.06em;text-transform:uppercase;color:#9A9893;padding:0 0 8px;">
        Groundwork
      </td></tr>
      <tr><td style="font-family:Arial,Helvetica,sans-serif;font-size:12.5px;line-height:1.65;color:#9A9893;">
                <a href="{unsubscribe_link}" style="color:#9A9893;text-decoration:underline;">Unsubscribe</a> or reply and let me know and I won't email again.
      </td></tr>
    </tbody></table>
  </td></tr>

</tbody></table>
</td></tr>
</tbody></table>



</td></tr></tbody></table></body></html>""",
    },
    "D": {
        "subject": 'One step left, {business_name}',
        "body": """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Type" content="text/html; charset=UTF-8">
<title>One step left, {business_name}</title>

</head>
<body style="margin:0;padding:0;background:#EDEAE2;font-family:Arial,Helvetica,sans-serif;">
<!-- Preheader (hidden, shows in inbox preview text) -->
<div style="display:none;max-height:0;overflow:hidden;mso-hide:all;">Your account's set up and your site's ready to go.</div>

<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#EDEAE2;">
<tbody><tr><td align="center" style="padding:40px 16px;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="max-width:560px;background:#FFFFFF;">

  <!-- HEADER: small logo mark + wordmark, thin brand-blue rule underneath —
       the same accent treatment as the site's nav, without a heavy colour
       block. Solid-colour cells (no images beyond the 24px mark) keep this
       spam-filter-friendly. -->
  <tbody><tr><td style="padding:28px 32px 18px;border-bottom:2px solid #3B82F6;">
    <table role="presentation" cellpadding="0" cellspacing="0" border="0">
      <tbody><tr>
        <td style="padding:0 9px 0 0;vertical-align:middle;">
          <img src="https://groundworkbuild.com/assets/email/groundwork-mark-22.png" width="22" height="22" alt="" style="display:block;border-radius:5px;">
        </td>
        <td style="vertical-align:middle;">
          <span style="font-family:Arial,Helvetica,sans-serif;font-size:17px;font-weight:bold;color:#1C1C1C;letter-spacing:-.01em;">Groundwork</span>
        </td>
      </tr>
    </tbody></table>
  </td></tr>

  <!-- BODY -->
  <tr><td style="padding:32px 32px 8px;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
      <tbody><tr><td style="font-family:Arial,Helvetica,sans-serif;font-size:15.5px;line-height:1.65;color:#2A2A28;padding:0 0 18px;">
        Hi {business_name} team,
      </td></tr>
      <tr><td style="font-family:Arial,Helvetica,sans-serif;font-size:15.5px;line-height:1.65;color:#2A2A28;padding:0 0 18px;">
        Your account's set up and your site's ready to go — just needs switching on.
      </td></tr>
      <tr><td style="font-family:Arial,Helvetica,sans-serif;font-size:15.5px;line-height:1.65;color:#2A2A28;padding:0 0 30px;">
        First month's free, £99 setup + £24.99/month after.
      </td></tr>

      <!-- CTA: single button, reused as-is across all 8 templates. Table-based
           bulletproof button — renders correctly in Outlook desktop, which
           ignores border-radius/padding on plain <a> and <button> tags. -->
      <tr><td style="padding:0 0 34px;">
        <table role="presentation" cellpadding="0" cellspacing="0" border="0">
          <tbody><tr><td bgcolor="#3B82F6" style="border-radius:8px;">
            <a href="{preview_link}" target="_blank" style="display:inline-block;padding:13px 24px;font-family:Arial,Helvetica,sans-serif;font-size:15px;font-weight:bold;color:#FFFFFF;text-decoration:none;border-radius:8px;">Go live</a>
          </td></tr>
        </tbody></table>
      </td></tr>

      <tr><td style="font-family:Arial,Helvetica,sans-serif;font-size:15.5px;line-height:1.65;color:#2A2A28;padding:0 0 8px;">
        Any questions, just reply to this email.
      </td></tr>
      {branding_ps}
      <tr><td style="padding:6px 0 26px;">
        <table role="presentation" cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse;font-family:Arial,Helvetica,sans-serif;">
        <tbody><tr>
          <td valign="middle" style="padding:0 18px 0 0;vertical-align:middle;">
            <img src="https://groundworkbuild.com/assets/email/groundwork-mark-48.png" width="48" height="48" alt="Groundwork" style="display:block;border-radius:10px;">
          </td>
          <td valign="middle" style="border-left:2px solid #3B82F6;padding:0 0 0 18px;vertical-align:middle;">
            <table role="presentation" cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse;">
              <tbody><tr><td style="font-family:Arial,Helvetica,sans-serif;font-size:16px;font-weight:bold;color:#1C1C1C;padding:0 0 2px;line-height:1.3;">Charlie</td></tr>
              <tr><td style="font-family:Arial,Helvetica,sans-serif;font-size:13px;color:#5C5A56;padding:0 0 10px;line-height:1.3;">Founder, Groundwork</td></tr>
              <tr><td style="font-family:Arial,Helvetica,sans-serif;font-size:13px;line-height:1.3;padding:0;">
                <a href="https://groundworkbuild.com" style="color:#2257CC;text-decoration:none;">groundworkbuild.com</a><span style="color:#D9D7D0;padding:0 8px;">|</span><a href="mailto:reply@groundworkbuild.com" style="color:#2257CC;text-decoration:none;">reply@groundworkbuild.com</a>
              </td></tr>
            </tbody></table>
      </td></tr>
    </tbody></table>
  </td></tr>

  <!-- FOOTER: understated, matches the site footer's muted-label treatment —
       small caps, quiet colour, thin top rule as the only separator. -->
  <tr><td style="padding:26px 32px 28px;border-top:1px solid #E2E0DA;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
      <tbody><tr><td style="font-family:Arial,Helvetica,sans-serif;font-size:11.5px;font-weight:bold;letter-spacing:.06em;text-transform:uppercase;color:#9A9893;padding:0 0 8px;">
        Groundwork
      </td></tr>
      <tr><td style="font-family:Arial,Helvetica,sans-serif;font-size:12.5px;line-height:1.65;color:#9A9893;">
                <a href="{unsubscribe_link}" style="color:#9A9893;text-decoration:underline;">Unsubscribe</a> or reply and let me know and I won't email again.
      </td></tr>
    </tbody></table>
  </td></tr>

</tbody></table>
</td></tr>
</tbody></table>



</td></tr></tbody></table></body></html>""",
    },
}

FOLLOWUP_SMS = {
    # Also used as the single collapsed pre-click follow-up for phone-only
    # (has_findable_email=False) prospects — SMS has no "opened" tracking.
    "A": (
        "Hi {business_name}, quick follow-up in case this got missed: "
        "groundworkbuild.com/s/{short_code}\n"
        "Reply STOP to opt out."
    ),
    "B": (
        "Hi {business_name}, quick follow-up — click below and we'll build a free website "
        "preview for your business: groundworkbuild.com/s/{short_code}\n"
        "Reply STOP to opt out."
    ),
    "C": (
        "Hi {business_name}, your website's built and waiting — have a look: "
        "groundworkbuild.com/s/{short_code}\n"
        "First month's free if you'd like to go live.\n"
        "Reply STOP to opt out."
    ),
    "D": (
        "Hi {business_name}, your account's set up and your site's ready — just need to go live: "
        "groundworkbuild.com/s/{short_code}\n"
        "First month's free, cancel anytime after.\n"
        "Reply STOP to opt out."
    ),
}

# Stages before a real click — must never claim the site is built.
PRE_CLICK_STAGES = ("A", "B")
# Stages after a real click — site generation has actually happened.
POST_CLICK_STAGES = ("C", "D")

_BRANDING_PS_ROW = (
    '<tr><td style="font-family:Arial,Helvetica,sans-serif;font-size:14px;'
    'line-height:1.65;color:#5C5A56;padding:0 0 18px;">P.S. — {text}</td></tr>'
)

# Deliberately generic wording for "partial" — it's true whether we pulled
# the logo, the photos, or just one of the two, without claiming a specific
# asset we may not actually have used (see _try_extract_prospect_assets in
# app.py, which sets Prospect.extraction_quality but not which asset(s)
# succeeded).
_BRANDING_PS_TEXT = {
    "full": "we pulled your logo and photos straight from your current site, so it already looks like you.",
    "partial": "we used some of your existing branding when building this, so it already looks like you.",
}


def branding_ps_line(extraction_quality):
    """
    Renders the optional "kept your branding" P.S. row for the {branding_ps}
    placeholder in stages C/D — the only stages sent after a real site has
    been generated, so extraction (which runs at claim-click time, before
    generation) has already happened and Prospect.extraction_quality is set.

    Returns "" for "none"/None/unrecognised values — the row must never
    appear for a prospect nothing was actually pulled for; str.format()
    with an empty string for {branding_ps} just collapses to no extra row,
    since the placeholder sits alone on its own line in the template.
    """
    text = _BRANDING_PS_TEXT.get(extraction_quality)
    if not text:
        return ""
    return _BRANDING_PS_ROW.format(text=text)


def render_email(stage_key, **kwargs):
    """stage_key: 'initial' or one of 'A'/'B'/'C'/'D'."""
    template = INITIAL_EMAIL if stage_key == "initial" else FOLLOWUP_EMAIL[stage_key]
    return {
        "subject": template["subject"].format(**kwargs),
        "body": template["body"].format(**kwargs),
    }


def render_sms(stage_key, **kwargs):
    """stage_key: 'initial' or one of 'A'/'B'/'C'/'D'."""
    template = INITIAL_SMS if stage_key == "initial" else FOLLOWUP_SMS[stage_key]
    return template.format(**kwargs)
