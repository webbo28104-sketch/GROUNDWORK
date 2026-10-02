"""
Groundwork Cashflow outreach — initial + follow-up template copy.

Mirrors outreach/templates.py's shape and interface exactly (INITIAL_EMAIL,
INITIAL_SMS, FOLLOWUP_EMAIL/FOLLOWUP_SMS dicts keyed A-D, PRE_CLICK_STAGES/
POST_CLICK_STAGES, render_email()/render_sms()) so outreach/cashflow_send_job.py
and outreach/cashflow_followup.py can be near-identical to their website
counterparts. Two differences from that file's approach, both deliberate:

1. No hail_mary/branding_ps — Cashflow has no logo/photo extraction step and
   this is a fast-follow product, not carrying over every website-specific
   mechanic on day one. MAX_TOUCHES (outreach/followup.py) still caps this
   sequence at 4 touches (initial + A/B/C/D minus whichever one is skipped
   by the pre/post-click split), same as website.
2. The shared HTML chrome (header/footer table markup) is factored into
   _email_shell() below rather than repeated verbatim in every dict, purely
   to keep this file a reasonable length — visually it renders identically
   to templates.py's per-stage copies. templates.py itself is untouched by
   this choice; that file's existing flat-dict style is left as-is.

Placeholders: {business_name}, {signup_link} (Cashflow's equivalent of
{preview_link} — points at frontend/cashflow/upgrade.html, not a generated
preview, since there's no per-prospect generation step here), {short_code},
{unsubscribe_link}.

Content accuracy: pre-click stages (A/B) must not claim the prospect already
has a trial/account running; post-click stages (C/D) may reference having
seen the dashboard. Same rule as templates.py's Section 10c, enforced by the
same outreach/content_safety.py gate (parameterized by product).

Every template below carries one light, factual cross-mention of the
website product, per the requirement that each product's copy lightly
references the other.
"""

_PREHEADER = '<div style="display:none;max-height:0;overflow:hidden;mso-hide:all;">{preheader}</div>'

_HEADER = """<tr><td style="padding:28px 32px 18px;border-bottom:2px solid #1E3A5F;">
    <table role="presentation" cellpadding="0" cellspacing="0" border="0">
      <tbody><tr>
        <td style="padding:0 9px 0 0;vertical-align:middle;">
          <img src="https://groundworkbuild.com/assets/email/groundwork-mark-22.png" width="22" height="22" alt="" style="display:block;border-radius:5px;">
        </td>
        <td style="vertical-align:middle;">
          <span style="font-family:Arial,Helvetica,sans-serif;font-size:17px;font-weight:bold;color:#1C1C1C;letter-spacing:-.01em;">Groundwork Cashflow</span>
        </td>
      </tr>
    </tbody></table>
  </td></tr>"""

_CTA = """<tr><td style="padding:0 0 34px;">
        <table role="presentation" cellpadding="0" cellspacing="0" border="0">
          <tbody><tr><td bgcolor="#1E3A5F" style="border-radius:8px;">
            <a href="{{signup_link}}" target="_blank" style="display:inline-block;padding:13px 24px;font-family:Arial,Helvetica,sans-serif;font-size:15px;font-weight:bold;color:#FFFFFF;text-decoration:none;border-radius:8px;">{cta_label}</a>
          </td></tr>
        </tbody></table>
      </td></tr>"""

_FOOTER = """<tr><td style="padding:26px 32px 28px;border-top:1px solid #E2E0DA;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
      <tbody><tr><td style="font-family:Arial,Helvetica,sans-serif;font-size:11.5px;font-weight:bold;letter-spacing:.06em;text-transform:uppercase;color:#9A9893;padding:0 0 8px;">
        Groundwork Cashflow
      </td></tr>
      <tr><td style="font-family:Arial,Helvetica,sans-serif;font-size:12.5px;line-height:1.65;color:#9A9893;">
        <a href="{unsubscribe_link}" style="color:#9A9893;text-decoration:underline;">Unsubscribe</a> or reply and let me know and I won't email again.
      </td></tr>
    </tbody></table>
  </td></tr>"""


def _email_shell(subject, preheader, body_paragraphs_html, cta_label, ps_line=""):
    ps_row = (
        f'<tr><td style="font-family:Arial,Helvetica,sans-serif;font-size:14px;'
        f'line-height:1.65;color:#5C5A56;padding:0 32px 22px;">P.S. — {ps_line}</td></tr>'
    ) if ps_line else ""
    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Type" content="text/html; charset=UTF-8">
<title>{{business_name}} — {subject}</title>
</head>
<body style="margin:0;padding:0;background:#F8FAFC;font-family:Arial,Helvetica,sans-serif;">
{_PREHEADER.format(preheader=preheader)}
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#F8FAFC;">
<tbody><tr><td align="center" style="padding:40px 16px;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="max-width:560px;background:#FFFFFF;">
<tbody>
{_HEADER}
<tr><td style="padding:32px 32px 8px;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
      <tbody>
{body_paragraphs_html}
{_CTA.format(cta_label=cta_label)}
    </tbody></table>
  </td></tr>
{ps_row}
{_FOOTER}
</tbody></table>
</td></tr></tbody></table>
</body></html>"""


def _p(text):
    return (
        f'<tr><td style="font-family:Arial,Helvetica,sans-serif;font-size:15.5px;'
        f'line-height:1.65;color:#2A2A28;padding:0 0 18px;">{text}</td></tr>'
    )


INITIAL_EMAIL = {
    "subject": "{business_name} — see your cash flow, 60 days out",
    "body": _email_shell(
        subject="see your cash flow, 60 days out",
        preheader="A Xero-connected cash flow dashboard, built for UK site-based trades.",
        body_paragraphs_html=(
            _p("Hi {business_name} team,") +
            _p("We're Groundwork — you might know us for building trade websites, but we've also built "
               "Groundwork Cashflow: a dashboard that connects to your Xero account and shows exactly "
               "where your cash position is heading over the next 60 days.") +
            _p("No accounting jargon — just a plain-English answer to “can I take on this next job?”, "
               "which invoices to chase first, and an early warning before you run short.")
        ),
        cta_label="Start your free month",
    ),
}

INITIAL_SMS = (
    "Hi {business_name}, this is Groundwork Cashflow — see your projected cash position 60 days out, "
    "connected straight to Xero: groundworkbuild.com/cf/{short_code}\n"
    "£9.99/mo after a free month. We also build trade websites, if that's useful too.\n"
    "Reply STOP to opt out."
)

FOLLOWUP_EMAIL = {
    "A": {
        "subject": "{business_name} — did this land?",
        "body": _email_shell(
            subject="did this land?",
            preheader="Quick follow-up in case this got missed.",
            body_paragraphs_html=(
                _p("Hi {business_name} team,") +
                _p("Quick follow-up in case this got missed.") +
                _p("Groundwork Cashflow connects to your Xero account and shows your projected cash "
                   "position 60 days out — no cost to see it, a free month if you want to keep it.")
            ),
            cta_label="See your free month",
        ),
    },
    "B": {
        "subject": "{business_name} — 60 seconds to connect Xero",
        "body": _email_shell(
            subject="60 seconds to connect Xero",
            preheader="Connect Xero, see your cash flow forecast straight away.",
            body_paragraphs_html=(
                _p("Hi {business_name} team,") +
                _p("Connecting takes about a minute — click below, sign in to Xero, and you'll see your "
                   "60-day cash flow forecast straight away.") +
                _p("(We also build trade websites, if you don't have one yet — just ask.)")
            ),
            cta_label="Connect Xero — free month",
        ),
    },
    "C": {
        "subject": "{business_name} — your dashboard's ready",
        "body": _email_shell(
            subject="your dashboard's ready",
            preheader="Your cash flow dashboard is set up and waiting.",
            body_paragraphs_html=(
                _p("Hi {business_name} team,") +
                _p("Your Groundwork Cashflow dashboard is set up — have a look at your projected cash "
                   "position and this month's payment alerts.") +
                _p("Your free month's still running — no charge until it ends, cancel any time before then.")
            ),
            cta_label="Open your dashboard",
        ),
    },
    "D": {
        "subject": "{business_name} — one thing left",
        "body": _email_shell(
            subject="one thing left",
            preheader="Your account's set up, dashboard's ready.",
            body_paragraphs_html=(
                _p("Hi {business_name} team,") +
                _p("Your account's set up and your dashboard's ready to go — nothing left to do but "
                   "keep using it. Your free month runs until it ends, then it's £9.99/mo, cancel any time.") +
                _p("Already a Groundwork website customer? Cashflow's included free on your plan — no "
                   "need to pay separately, just check your account.")
            ),
            cta_label="Open your dashboard",
        ),
    },
}

FOLLOWUP_SMS = {
    "A": (
        "Hi {business_name}, quick follow-up in case this got missed — see your cash flow forecast: "
        "groundworkbuild.com/cf/{short_code}\n"
        "Reply STOP to opt out."
    ),
    "B": (
        "Hi {business_name}, connecting Xero takes about a minute and shows your 60-day cash forecast "
        "straight away: groundworkbuild.com/cf/{short_code}\n"
        "Reply STOP to opt out."
    ),
    "C": (
        "Hi {business_name}, your Cashflow dashboard's ready — have a look: "
        "groundworkbuild.com/cf/{short_code}\n"
        "Free month still running.\n"
        "Reply STOP to opt out."
    ),
    "D": (
        "Hi {business_name}, your dashboard's set up and ready — just keep using it: "
        "groundworkbuild.com/cf/{short_code}\n"
        "Free month, then £9.99/mo, cancel any time.\n"
        "Reply STOP to opt out."
    ),
}

# Stages before a real Xero connection — must never claim the dashboard is
# already showing real numbers. Mirrors templates.py's PRE_CLICK_STAGES.
PRE_CLICK_STAGES = ("A", "B")
# Stages after a real Xero connection.
POST_CLICK_STAGES = ("C", "D")


def render_email(stage_key, **kwargs):
    """stage_key: 'initial' or one of 'A'/'B'/'C'/'D'. No 'hail_mary' stage
    for Cashflow yet — see module docstring."""
    template = INITIAL_EMAIL if stage_key == "initial" else FOLLOWUP_EMAIL[stage_key]
    return {
        "subject": template["subject"].format(**kwargs),
        "body": template["body"].format(**kwargs),
    }


def render_sms(stage_key, **kwargs):
    """stage_key: 'initial' or one of 'A'/'B'/'C'/'D'."""
    template = INITIAL_SMS if stage_key == "initial" else FOLLOWUP_SMS[stage_key]
    return template.format(**kwargs)
