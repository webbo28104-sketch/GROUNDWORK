"""
Groundwork outreach — initial + follow-up template copy.

Placeholders substituted by the caller: {business_name}, {preview_link},
{short_code}, {unsubscribe_link}.

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
          <img src="data:image/png;base64,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" width="22" height="22" alt="" style="display:block;border-radius:5px;">
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
            <img src="data:image/png;base64,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" width="48" height="48" alt="Groundwork" style="display:block;border-radius:10px;">
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
  <tr><td style="font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.65;color:#5C5A56;padding:0 32px 22px;">P.S. — here's a real site we've built recently, live now: sussexleadcraftltd.com</td></tr>
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
          <img src="data:image/png;base64,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" width="22" height="22" alt="" style="display:block;border-radius:5px;">
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
            <img src="data:image/png;base64,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" width="48" height="48" alt="Groundwork" style="display:block;border-radius:10px;">
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
          <img src="data:image/png;base64,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" width="22" height="22" alt="" style="display:block;border-radius:5px;">
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
            <img src="data:image/png;base64,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" width="48" height="48" alt="Groundwork" style="display:block;border-radius:10px;">
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
          <img src="data:image/png;base64,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" width="22" height="22" alt="" style="display:block;border-radius:5px;">
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
      <tr><td style="padding:6px 0 26px;">
        <table role="presentation" cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse;font-family:Arial,Helvetica,sans-serif;">
        <tbody><tr>
          <td valign="middle" style="padding:0 18px 0 0;vertical-align:middle;">
            <img src="data:image/png;base64,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" width="48" height="48" alt="Groundwork" style="display:block;border-radius:10px;">
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
          <img src="data:image/png;base64,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" width="22" height="22" alt="" style="display:block;border-radius:5px;">
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
      <tr><td style="padding:6px 0 26px;">
        <table role="presentation" cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse;font-family:Arial,Helvetica,sans-serif;">
        <tbody><tr>
          <td valign="middle" style="padding:0 18px 0 0;vertical-align:middle;">
            <img src="data:image/png;base64,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" width="48" height="48" alt="Groundwork" style="display:block;border-radius:10px;">
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
