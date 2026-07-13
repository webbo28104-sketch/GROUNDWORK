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
"""

INITIAL_EMAIL = {
    "subject": "{business_name} — see a website built for you, no cost",
    "body": """Hi {business_name} team,

We're Groundwork — we build affordable websites for UK trade businesses, without the agency price tag or hassle.

See your website preview below, tailored to your trade and area — no cost, no obligation to take it further.

{preview_link}

If you like what you see, going live is £99 setup + £24.99/month, first month free — most other website services charge around £89 a month alone.

Have a look and see what you think — any questions, just reply to this email.

P.S. — here's a real site we've built recently, live now: sussexleadcraftltd.com

---
Don't want emails like this from us? Reply STOP or click here to unsubscribe: {unsubscribe_link}""",
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
        "subject": "{business_name} — did this land?",
        "body": """Hi {business_name} team,

Quick follow-up in case this got missed — click below and we'll build a free website preview for {business_name}, no cost:

{preview_link}

Any questions, just reply to this email.

---
Don't want emails like this from us? Reply STOP or click here to unsubscribe: {unsubscribe_link}""",
    },
    "B": {
        "subject": "Still there — {business_name}'s website preview",
        "body": """Hi {business_name} team,

Following up on your free website preview — click below and we'll build it for {business_name} right there, no cost:

{preview_link}

Any questions, just reply to this email.

---
Don't want emails like this from us? Reply STOP or click here to unsubscribe: {unsubscribe_link}""",
    },
    "C": {
        "subject": "Your website's ready, {business_name}",
        "body": """Hi {business_name} team,

Just checking in — your website's built and waiting:

{preview_link}

First month's free if you'd like to go live — £99 setup + £24.99/month after.

Any questions, just reply to this email.

---
Don't want emails like this from us? Reply STOP or click here to unsubscribe: {unsubscribe_link}""",
    },
    "D": {
        "subject": "One step left, {business_name}",
        "body": """Hi {business_name} team,

Your account's set up and your site's ready to go — just needs switching on:

{preview_link}

First month's free, £99 setup + £24.99/month after.

Any questions, just reply to this email.

---
Don't want emails like this from us? Reply STOP or click here to unsubscribe: {unsubscribe_link}""",
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
