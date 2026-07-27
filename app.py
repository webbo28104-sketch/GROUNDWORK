import os
import io
import re
import hmac
import math
import time
import json
import uuid
import base64
import shutil
import logging
import threading
import urllib.request
import email.utils as email_utils
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from sqlalchemy import func
from functools import wraps
from urllib.parse import urlparse as _urlparse, quote

import anthropic
import stripe
from PIL import Image, ImageDraw, ImageFilter
from flask import Flask, request, jsonify, send_from_directory, session, redirect, url_for, render_template_string, abort
from flask_cors import CORS
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from markupsafe import escape
from werkzeug.security import generate_password_hash, check_password_hash

from build_prompt import build_prompt, PROMPT_VERSION_HASH
from outreach.site_extract import extract_site_assets
from models import SessionLocal, Lead, Generation, Account, GenerationImage, Domain, Prospect, SearchCell, PendingEmailDiscovery, EmailEventLog, OutreachTouch, DailySendCount, RampState, SmsDeliveryEvent, SurveyResponse, PreGenSurveyResponse, DiscoveryRunLog, InboundReply, EmailVariant, EvidenceFinding, OptimizerRunLog, PromptApproval, ProspectEvent, init_db
from emails import (send_verification_email, send_resend_email, send_password_reset_email,
                    send_support_message_email, send_enquiry_email,
                    send_domain_order_admin_email, send_domain_order_customer_email,
                    send_domain_setup_failed_email, send_domain_live_email,
                    send_admin_payment_received_email, send_admin_magic_link_clicked_email,
                    send_site_ready_email, send_admin_approval_email)
from outreach.reply_handling import handle_inbound_sms, handle_inbound_email, handle_forced_sms_stop
from outreach.templates import SURVEY_DISCOUNT_PERCENT, render_facebook_dm
from outreach.link_identity import ensure_link_identity
from outreach.followup import STAGE_LABELS, STAGE_BY_SUBSTAGE
from outreach.ramp import (
    get_health_signal, get_remaining_ramp_today, EMAIL_SPAM_RATE_TRIGGER,
    EMAIL_BOUNCE_RATE_TRIGGER, MIN_EMAIL_SAMPLE_SIZE, CIRCUIT_BREAKER_RECOVERY_DAYS,
)
from outreach.sourcing_channels import SOURCING_CHANNEL_LABELS

app = Flask(__name__, static_folder="frontend", static_url_path="")
app.logger.setLevel(logging.INFO)
CORS(app)
app.secret_key = os.environ.get("SECRET_KEY", "dev-insecure-secret-change-me")
serializer = URLSafeTimedSerializer(app.secret_key)

# Session cookie config — explicit, not left to Flask/Werkzeug defaults.
# Verified against the installed versions (Flask 3.1, Werkzeug 3.1): Flask's
# own defaults are SESSION_COOKIE_SAMESITE=None (attribute omitted entirely
# — browsers then apply their own default, which is Lax as of Chrome 80+/
# Firefox 96+/Safari 13.1+, but that's a browser fallback this app doesn't
# control, not a guarantee) and SESSION_COOKIE_SECURE=False (cookie would be
# sent over plain HTTP too). SESSION_COOKIE_HTTPONLY already defaults to
# True, which is correct as-is.
#
# SameSite=Lax is set explicitly here as this app's actual CSRF defense: it
# blocks the session cookie from being attached to cross-site POST requests
# (the standard CSRF vector — a form or fetch on an attacker's page can't
# ride the victim's session), while still allowing normal top-level
# navigation (e.g. clicking an emailed magic link) to carry the cookie. No
# separate CSRF-token library is used; every session-mutating endpoint here
# is either a same-site form POST or a same-site fetch (see CLAUDE.md's
# "Frontend API URL" section on why cookie-dependent calls are always
# relative/same-origin), so there's no legitimate cross-site case Lax would
# need to allow through.
#
# SESSION_COOKIE_SECURE is forced True only when RAILWAY_ENVIRONMENT is set
# (i.e. actually running on Railway, always HTTPS there) so local dev over
# plain http:// still works without the cookie silently failing to be set.
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = bool(os.environ.get("RAILWAY_ENVIRONMENT"))

TOKEN_MAX_AGE = 24 * 3600  # 24h magic-link expiry
RESET_TOKEN_MAX_AGE = 3600  # 1h — shorter-lived since it grants a password change
IP_RATE_LIMIT_PER_HOUR = int(os.environ.get("IP_RATE_LIMIT_PER_HOUR", "5"))

# Stripe — all values come from environment variables set in Railway.
# STRIPE_MONTHLY_PRICE_ID → the £24.99/month recurring price (price_...) — no
#                           setup fee (removed 2026-07-23, until break-even;
#                           see docs/outreach-pipeline-spec.md). STRIPE_MONTHLY_PRICE_ID
#                           must point at a £24.99 Stripe Price object — that
#                           object still needs creating/swapping in the Stripe
#                           dashboard, this repo has no Stripe credentials to do
#                           it from code.
# STRIPE_ANNUAL_PRICE_ID  → the annual recurring price       (price_...)
# STRIPE_SECRET_KEY       → sk_live_... (or sk_test_... for testing)
# STRIPE_WEBHOOK_SECRET   → whsec_... from `stripe listen` or dashboard
# SITE_URL                → https://groundworkbuild.com (used for redirect URLs)
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
STRIPE_MONTHLY_PRICE_ID = os.environ.get("STRIPE_MONTHLY_PRICE_ID", "")
STRIPE_ANNUAL_PRICE_ID = os.environ.get("STRIPE_ANNUAL_PRICE_ID", "")
SITE_URL = os.environ.get("SITE_URL", "https://groundworkbuild.com")
stripe.api_key = STRIPE_SECRET_KEY

# view_count/first_viewed_at/last_viewed_at/total_view_seconds/max_scroll_pct
# were bulk-reset to 0/NULL on 2026-07-24 ~09:55 UTC (admin previews via the
# old public preview link had been inflating real customers' own view
# stats — see admin_generation_preview's docstring for the fix). A
# generation created before this cutoff with zero view stats is genuinely
# ambiguous — it could mean "never viewed" or "was viewed, but that's the
# data we just wiped" — so every display of these fields treats anything
# before this cutoff as "no data," never as a real zero. Only generations
# created after this point have trustworthy view stats.
_VIEW_STATS_RELIABLE_FROM = datetime(2026, 7, 24, 9, 55)

# Porkbun — domain registration and DNS.
PORKBUN_API_KEY    = os.environ.get("PORKBUN_API_KEY", "")
PORKBUN_SECRET_KEY = os.environ.get("PORKBUN_SECRET_KEY", "")

# Railway — GraphQL API for adding custom domains to the service.
# Retained only as legacy/reference; customer custom domains now go through
# Cloudflare for SaaS (see below) instead of Railway's native custom domains,
# since Railway's Hobby plan caps us at 2 domains per service.
# RAILWAY_SERVICE_ID and RAILWAY_ENVIRONMENT_ID are set automatically by Railway.
# RAILWAY_API_TOKEN must be added manually: Railway dashboard → Account → Tokens.
# RAILWAY_CNAME_TARGET: the CNAME target Railway provides for custom domains
#   (e.g. "roundhouse.proxy.rlwy.net") — find it in Railway Dashboard → Settings → Domains.
RAILWAY_API_URL        = "https://backboard.railway.app/graphql/v2"
RAILWAY_API_TOKEN      = os.environ.get("RAILWAY_API_TOKEN", "")
RAILWAY_SERVICE_ID     = os.environ.get("RAILWAY_SERVICE_ID", "")
RAILWAY_ENVIRONMENT_ID = os.environ.get("RAILWAY_ENVIRONMENT_ID", "")
RAILWAY_CNAME_TARGET   = os.environ.get("RAILWAY_CNAME_TARGET", "")

# Cloudflare for SaaS — Custom Hostnames API. Used to connect customer-owned
# domains to our Railway app through Cloudflare's edge (Fallback Origin points
# at Railway), instead of registering the domain directly with Railway.
# CLOUDFLARE_ZONE_ID: the zone ID for groundworkbuild.com.
# CLOUDFLARE_CNAME_TARGET: the CNAME target customers' DNS should point at
#   (e.g. "connect.groundworkbuild.com") — set up in Cloudflare for SaaS config.
#
# Multiple Cloudflare API tokens exist on this project, scoped for different
# purposes — DO NOT consolidate them into one, and do not replace/overwrite
# a token value in Railway even if it looks expired/wrong; add a new,
# distinctly-named env var instead and point the relevant code at it. This
# is deliberate policy (2026-07-14), not an oversight: an earlier in-session
# token swap on CLOUDFLARE_API_TOKEN silently broke every Custom Hostname
# call for days, because that var was assumed to be DNS-only and got
# replaced with a DNS-only-scoped token — but every actual usage of it in
# this file was Custom Hostname management, which needs a different scope
# entirely. Splitting by purpose into separately-named vars, as done below,
# is what prevents that class of mistake from recurring.
#
# CLOUDFLARE_API_TOKEN: legacy — kept as-is, untouched, not read by any
#   current code path (nothing in this file does plain Cloudflare DNS record
#   management; the one time that was needed it was done as a one-off
#   outside the app). Left in place for whatever future DNS-record code
#   ends up using it — do not repurpose or delete.
# CLOUDFLARE_API_TOKEN_CUSTOM_HOSTNAMES: the token actually used below, for
#   every Zone → Custom Hostnames / SSL and Certificates: Edit call
#   (creating, checking SSL status on, and deleting Custom Hostnames).
CLOUDFLARE_API_URL        = "https://api.cloudflare.com/client/v4"
CLOUDFLARE_API_TOKEN      = os.environ.get("CLOUDFLARE_API_TOKEN", "")
CLOUDFLARE_API_TOKEN_CUSTOM_HOSTNAMES = os.environ.get("CLOUDFLARE_API_TOKEN_CUSTOM_HOSTNAMES", "")
CLOUDFLARE_ZONE_ID        = os.environ.get("CLOUDFLARE_ZONE_ID", "")
CLOUDFLARE_CNAME_TARGET   = os.environ.get("CLOUDFLARE_CNAME_TARGET", "connect.groundworkbuild.com")

# Subdomain routing — every live customer gets <slug>.groundworkbuild.com.
# _SUBDOMAIN_BASE is derived from SITE_URL so local dev (localhost) doesn't
# accidentally try to serve subdomain routes.
_SUBDOMAIN_BASE = _urlparse(SITE_URL).hostname or "groundworkbuild.com"
_RESERVED_SUBDOMAINS = {"www", "mail", "api", "admin", "app", "static"}


def _make_subdomain(business_name: str) -> str:
    """Lowercase and strip everything except a-z0-9 — apostrophes, symbols,
    spaces, accents etc. are simply removed rather than left in place for
    _subdomain_has_invalid_chars() to flag later (that used to block
    checkout entirely on e.g. an apostrophe in the company name; see
    _resolve_subdomain, added 2026-07-23, for why that's gone)."""
    return re.sub(r'[^a-z0-9]', '', business_name.lower())


def _subdomain_has_invalid_chars(slug: str) -> bool:
    """DNS labels only allow a-z, 0-9. Returns True if anything else is
    present — can't actually happen for a slug that came out of
    _make_subdomain() any more (it only ever emits a-z0-9), but kept as a
    defensive check for the legacy-subdomain-migration path below, which
    predates the stripping approach."""
    return not re.match(r'^[a-z0-9]+$', slug)


def _subdomain_is_taken(slug: str, db, exclude_gen_id=None) -> bool:
    """True if any other live generation already holds this subdomain."""
    q = db.query(Generation).filter(
        Generation.subdomain == slug,
        Generation.status == "live",
    )
    if exclude_gen_id is not None:
        q = q.filter(Generation.id != exclude_gen_id)
    return q.first() is not None


def _resolve_subdomain(business_name: str, db, exclude_gen_id=None) -> str:
    """Turns a business name into a live, guaranteed-available subdomain —
    never blocks or rejects (added 2026-07-23, by request: going live free
    today should have zero friction; a slightly-off web address is a
    solvable-after-the-fact support request, not a reason to stop someone
    paying). Non a-z0-9 characters are stripped by _make_subdomain rather
    than flagged; if that leaves nothing usable at all (e.g. a business
    name in a non-Latin script), falls back to a random slug. A name
    collision with another live site is resolved with a numeric suffix
    (-2, -3, ...) rather than blocking — the customer can still ask
    support to tidy up their address afterwards if they want the bare name."""
    base = _make_subdomain(business_name) or f"site{uuid.uuid4().hex[:8]}"
    slug = base
    suffix = 2
    while _subdomain_is_taken(slug, db, exclude_gen_id=exclude_gen_id):
        slug = f"{base}{suffix}"
        suffix += 1
    return slug

init_db()

# Per-IP rate limiter for the public contact form endpoint.
# Stored in memory (not DB) — intentionally ephemeral; it resets on redeploy,
# which is acceptable since the honeypot is the primary spam defence.
_contact_submissions: dict[str, list[float]] = {}
_contact_submissions_lock = threading.Lock()
_CONTACT_RATE_PER_HOUR = 10


def _contact_rate_limited(ip: str) -> bool:
    import time as _time
    now = _time.time()
    cutoff = now - 3600
    with _contact_submissions_lock:
        times = [t for t in _contact_submissions.get(ip, []) if t > cutoff]
        if len(times) >= _CONTACT_RATE_PER_HOUR:
            return True
        times.append(now)
        _contact_submissions[ip] = times
    return False


# ---------------------------------------------------------------------------
# Generic in-memory sliding-window rate limiter, shared by admin login,
# account login, and domain search below. Same tradeoff as
# _contact_rate_limited above — ephemeral, resets on redeploy, which is
# acceptable since these are throttles against brute force / abuse, not a
# source of truth for anything.
# ---------------------------------------------------------------------------
_rate_buckets: dict[str, dict[str, list[float]]] = {}
_rate_buckets_lock = threading.Lock()


def _rate_limited(bucket_name: str, key: str, limit: int, window_seconds: int) -> bool:
    """True if `key` has hit `limit` events within `window_seconds` inside
    `bucket_name`'s namespace (and records this event only when False is
    returned, i.e. real attempts count, not the block itself)."""
    now = time.time()
    cutoff = now - window_seconds
    with _rate_buckets_lock:
        bucket = _rate_buckets.setdefault(bucket_name, {})
        times = [t for t in bucket.get(key, []) if t > cutoff]
        if len(times) >= limit:
            bucket[key] = times
            return True
        times.append(now)
        bucket[key] = times
    return False


# Admin login lockout — tracked separately from _rate_limited above because
# this counts only *failed* attempts (a correct login never adds to it, and
# a success clears it), whereas _rate_limited counts every request
# regardless of outcome. Keyed by a constant since there's a single admin
# account — this is a lockout on the account, not per-IP, so a distributed
# brute force (many IPs, same credentials) is still caught.
_admin_login_failures: list[float] = []
_admin_login_failures_lock = threading.Lock()
_ADMIN_LOGIN_MAX_FAILURES = 5
_ADMIN_LOGIN_LOCKOUT_WINDOW = 900  # 15 minutes


def _admin_login_locked_out() -> bool:
    now = time.time()
    cutoff = now - _ADMIN_LOGIN_LOCKOUT_WINDOW
    with _admin_login_failures_lock:
        _admin_login_failures[:] = [t for t in _admin_login_failures if t > cutoff]
        return len(_admin_login_failures) >= _ADMIN_LOGIN_MAX_FAILURES


def _record_admin_login_failure() -> None:
    with _admin_login_failures_lock:
        _admin_login_failures.append(time.time())


def _clear_admin_login_failures() -> None:
    with _admin_login_failures_lock:
        _admin_login_failures.clear()


def _logo_to_favicon(data_uri: str) -> str | None:
    """Generate a 32x32 PNG favicon from a logo data URI (center-crop then resize)."""
    try:
        _, b64 = data_uri.split(",", 1)
        img = Image.open(io.BytesIO(base64.b64decode(b64))).convert("RGBA")
        w, h = img.size
        side = min(w, h)
        img = img.crop(((w - side) // 2, (h - side) // 2, (w + side) // 2, (h + side) // 2))
        img = img.resize((32, 32), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return None


def _migrate_legacy_subdomains():
    """Assign subdomains to existing live non-test generations that don't have one yet.
    Runs on every startup but is a no-op once all rows are covered (subdomain IS NOT NULL).
    Oldest generation wins on a name collision; conflicts are logged for manual review."""
    db = SessionLocal()
    try:
        pending = (
            db.query(Generation)
            .join(Lead)
            .filter(
                Generation.status == "live",
                Generation.subdomain.is_(None),
                Lead.is_test.is_(False),
            )
            .order_by(Generation.created_at)
            .all()
        )
        for gen in pending:
            business_name = (gen.lead.form_data or {}).get("business_name", "")
            slug = _make_subdomain(business_name)
            if not slug or _subdomain_has_invalid_chars(slug):
                app.logger.warning(
                    f"Legacy subdomain skipped — invalid chars: gen {gen.id} {business_name!r}"
                )
                continue
            if _subdomain_is_taken(slug, db, exclude_gen_id=gen.id):
                app.logger.warning(
                    f"Legacy subdomain conflict: {slug!r} already taken, skipping gen {gen.id} ({business_name!r})"
                )
                continue
            gen.subdomain = slug
            app.logger.info(f"Assigned legacy subdomain {slug!r} to gen {gen.id}")
        db.commit()
    finally:
        db.close()


_migrate_legacy_subdomains()


def _migrate_favicons():
    """Backfill favicon into html_content for generations that have a logo in
    GenerationImage but no <link rel="icon"> yet. Idempotent — skips rows that
    already have any favicon tag. Runs on every startup but is a no-op once
    all rows are covered."""
    db = SessionLocal()
    try:
        logo_rows = (
            db.query(GenerationImage)
            .filter(GenerationImage.slot == "logo")
            .all()
        )
        patched = 0
        for img_row in logo_rows:
            gen = db.get(Generation, img_row.generation_id)
            if not gen or not gen.html_content:
                continue
            if 'rel="icon"' in gen.html_content:
                continue
            favicon_uri = _logo_to_favicon(img_row.data_uri)
            if not favicon_uri:
                app.logger.warning(f"Favicon migration: could not generate favicon for gen {gen.id}")
                continue
            favicon_tag = f'<link rel="icon" type="image/png" sizes="32x32" href="{favicon_uri}">'
            head_close = gen.html_content.find("</head>")
            if head_close != -1:
                gen.html_content = gen.html_content[:head_close] + favicon_tag + gen.html_content[head_close:]
            else:
                gen.html_content = favicon_tag + gen.html_content
            patched += 1
        db.commit()
        if patched:
            app.logger.info(f"Favicon migration: patched {patched} generation(s)")
    finally:
        db.close()


_migrate_favicons()


def _migrate_contact_forms():
    """Backfill working contact form submission into generations built before the
    fetch()-based form was introduced. Finds any stored HTML that contains a
    <form> but no reference to /api/contact, and injects a small self-contained
    script that intercepts the first textarea-containing form, adds the hidden
    site_id + honeypot fields, and POSTs to /api/contact via fetch().
    Idempotent — the /api/contact check ensures it never runs twice."""
    contact_url = f"{SITE_URL}/api/contact"
    script_template = (
        '<script>'
        '(function(){{'
        'var fs=document.querySelectorAll("form");'
        'for(var i=0;i<fs.length;i++){{'
        'if(fs[i].querySelector("textarea")){{'
        'var f=fs[i];'
        # inject site_id
        'var s=document.createElement("input");s.type="hidden";s.name="site_id";s.value="{job_id}";f.appendChild(s);'
        # inject honeypot
        'var h=document.createElement("input");h.type="text";h.name="website";h.setAttribute("tabindex","-1");h.setAttribute("autocomplete","off");h.style.cssText="position:absolute;left:-9999px;opacity:0;pointer-events:none;";h.setAttribute("aria-hidden","true");f.appendChild(h);'
        # intercept submit
        'f.addEventListener("submit",function(e){{'
        'e.preventDefault();'
        'var b=f.querySelector("button[type=submit],input[type=submit],button:not([type])");'
        'if(b)b.disabled=true;'
        'fetch("{contact_url}",{{method:"POST",body:new FormData(f)}})'
        '.then(function(r){{return r.json();}})'
        '.then(function(d){{'
        'if(d.ok){{'
        'f.innerHTML="<p style=\\"text-align:center;padding:24px 0;font-size:16px;\\">Thanks — we\'ll be in touch shortly.</p>";'
        '}}else{{'
        'var er=f.querySelector(".gw-ce");'
        'if(!er){{er=document.createElement("p");er.className="gw-ce";er.style.cssText="color:#B91C1C;font-size:14px;margin-top:10px;";f.appendChild(er);}}'
        'er.textContent=d.error||"Something went wrong. Please try calling us directly.";'
        'if(b)b.disabled=false;'
        '}}'
        '}})'
        '.catch(function(){{'
        'var er=f.querySelector(".gw-ce");'
        'if(!er){{er=document.createElement("p");er.className="gw-ce";er.style.cssText="color:#B91C1C;font-size:14px;margin-top:10px;";f.appendChild(er);}}'
        'er.textContent="Could not connect. Please try calling us directly.";'
        'if(b)b.disabled=false;'
        '}});'
        '}});'
        'break;'
        '}}'
        '}}'
        '}})();'
        '</script>'
    )

    db = SessionLocal()
    try:
        gens = db.query(Generation).filter(
            Generation.html_content.isnot(None),
        ).all()
        patched = 0
        for gen in gens:
            html = gen.html_content
            if '/api/contact' in html:
                continue
            if '<form' not in html or 'textarea' not in html:
                continue
            job_id = gen.lead.public_id if gen.lead else None
            if not job_id:
                continue
            script = script_template.format(job_id=job_id, contact_url=contact_url)
            body_close = html.rfind("</body>")
            if body_close != -1:
                gen.html_content = html[:body_close] + script + html[body_close:]
            else:
                gen.html_content = html + script
            patched += 1
        db.commit()
        if patched:
            app.logger.info(f"Contact form migration: patched {patched} generation(s)")
    finally:
        db.close()


_migrate_contact_forms()


def _migrate_truncated_html():
    """Fix generations whose HTML was cut off mid-output (max_tokens hit during
    generation). Symptoms: missing </html>, sections invisible due to fade-in
    JS never initialising. Injects a CSS override to make fade-in elements
    immediately visible, closes any dangling <script>, and appends </body></html>.
    Idempotent — skips any generation whose HTML already ends with </html>."""
    db = SessionLocal()
    try:
        gens = db.query(Generation).filter(
            Generation.html_content.isnot(None)
        ).all()
        patched = 0
        for gen in gens:
            html = gen.html_content
            if html.rstrip().lower().endswith("</html>"):
                continue
            # Determine whether there's an unclosed <script> tag
            open_scripts = html.lower().count("<script") - html.lower().count("</script>")
            closer = ""
            if open_scripts > 0:
                closer += "</script>"
            # Override fade-in invisibility and close the document properly
            closer += (
                '<style>.fade-in{opacity:1!important;transform:none!important}</style>'
                '</body></html>'
            )
            gen.html_content = html + closer
            patched += 1
            app.logger.info(f"Truncation fix applied to gen {gen.id} ({gen.business_name!r})")
        db.commit()
        if patched:
            app.logger.info(f"Truncation migration: fixed {patched} generation(s)")
    finally:
        db.close()


_migrate_truncated_html()


def _retrofit_gw_text_markers(html: str) -> str:
    """Inject data-gw-text markers into legacy HTML that has none.
    Splits out script/style/svg blocks to avoid false matches, detects section
    context from id= attributes and structural tags, then marks h1-h4, p, li,
    and button elements with unique IDs following the standard scheme."""
    if not html or 'data-gw-text=' in html:
        return html

    SECTION_IDS = {
        'hero': 'hero', 'about': 'about', 'services': 'services',
        'service': 'services', 'accreditations': 'accreditations',
        'credentials': 'accreditations', 'portfolio': 'portfolio',
        'gallery': 'gallery', 'contact': 'contact', 'footer': 'footer',
    }
    MARK_TAGS = {'h1', 'h2', 'h3', 'h4', 'p', 'li', 'button'}
    DESCRIPTORS = {
        'h1': 'heading', 'h2': 'heading', 'h3': 'heading', 'h4': 'heading',
        'p': 'body', 'li': 'item', 'button': 'cta',
    }

    counters: dict = {}
    current_section = ['main']  # mutable so nested fn can update it

    def next_id(section: str, descriptor: str) -> str:
        key = (section, descriptor)
        counters[key] = counters.get(key, 0) + 1
        return f'{section}-{descriptor}-{counters[key]}'

    skip_re = re.compile(
        r'(<(?:script|style|svg)(?:\s[^>]*)?>.*?</(?:script|style|svg)>)',
        re.DOTALL | re.IGNORECASE,
    )
    segments = skip_re.split(html)

    def process_seg(seg: str) -> str:
        result = []
        pos = 0
        while pos < len(seg):
            lt = seg.find('<', pos)
            if lt == -1:
                result.append(seg[pos:])
                break
            result.append(seg[pos:lt])
            gt = seg.find('>', lt)
            if gt == -1:
                result.append(seg[lt:])
                break
            tag_str = seg[lt:gt + 1]

            # Update section context from id= attribute
            id_m = re.search(r'\bid=["\']([^"\']+)["\']', tag_str)
            if id_m:
                eid = id_m.group(1).lower().strip()
                if eid in SECTION_IDS:
                    current_section[0] = SECTION_IDS[eid]

            tag_m = re.match(r'<([a-zA-Z][a-zA-Z0-9]*)', tag_str)
            tag_name = tag_m.group(1).lower() if tag_m else ''
            is_closing = tag_str.startswith('</')
            is_self_closing = tag_str.endswith('/>')

            if not is_closing:
                if tag_name == 'nav':
                    current_section[0] = 'nav'
                elif tag_name == 'footer':
                    current_section[0] = 'footer'

            if (not is_closing and not is_self_closing
                    and tag_name in MARK_TAGS
                    and 'data-gw-text=' not in tag_str):
                descriptor = DESCRIPTORS.get(tag_name, 'text')
                field_id = next_id(current_section[0], descriptor)
                tag_str = tag_str[:-1] + f' data-gw-text="{field_id}">'

            result.append(tag_str)
            pos = gt + 1
        return ''.join(result)

    out = []
    for i, seg in enumerate(segments):
        out.append(seg if i % 2 == 1 else process_seg(seg))
    return ''.join(out)


def _migrate_text_markers():
    """Retrofit data-gw-text markers into any generation that has none yet.
    Idempotent — _retrofit_gw_text_markers() skips HTML that already has them."""
    db = SessionLocal()
    try:
        gens = [g for g in db.query(Generation).filter(
            Generation.html_content.isnot(None)
        ).all() if 'data-gw-text=' not in (g.html_content or '')]
        if not gens:
            return
        updated = 0
        for g in gens:
            new_html = _retrofit_gw_text_markers(g.html_content)
            if new_html != g.html_content:
                g.html_content = new_html
                updated += 1
        if updated:
            db.commit()
            app.logger.info(f'[migrate_text_markers] retrofitted {updated} generation(s)')
    except Exception:
        app.logger.exception('[migrate_text_markers] failed')
        db.rollback()
    finally:
        db.close()


_migrate_text_markers()


@app.before_request
def handle_subdomain_request():
    """Serve a live customer's site when the request arrives on their subdomain,
    or on a custom domain connected via Cloudflare for SaaS.

    Custom domains used to be handled entirely by Railway's native custom
    domain feature, which terminated TLS and routed straight to this service
    without this app ever needing to know about the `domains` table. Now that
    Cloudflare for SaaS (Custom Hostnames) sits in front instead, Cloudflare
    forwards the request to our Fallback Origin (this Railway app) carrying
    whatever Host header the customer's browser sent — apex domain, www, or
    subdomain — so this app has to look the Host up itself to know which
    customer site to serve."""
    # When a customer's custom domain comes in via Cloudflare for SaaS, the
    # Cloudflare Transform Rule rewrites the Host header to groundworkbuild.com
    # (so Railway's edge routes it to this service) and stashes the real
    # hostname in X-Custom-Domain.  We only trust it when CF-Ray confirms the
    # request actually came through Cloudflare — prevents local spoofing.
    cf_custom = request.headers.get("X-Custom-Domain", "").lower().split(":")[0].strip()
    if cf_custom and request.headers.get("CF-Ray"):
        bare = cf_custom[4:] if cf_custom.startswith("www.") else cf_custom
        _db = SessionLocal()
        try:
            dom = _db.query(Domain).filter(Domain.domain == bare, Domain.status == "active").first()
            if dom and dom.generation_id:
                gen = _db.query(Generation).filter(
                    Generation.id == dom.generation_id, Generation.status == "live"
                ).first()
                if gen:
                    return _inject_badge(gen.html_content), 200, {"Content-Type": "text/html; charset=utf-8"}
        finally:
            _db.close()
        return  # custom domain header present but no active site — fall through to 404 handling

    host = request.host.split(":")[0].lower()
    suffix = "." + _SUBDOMAIN_BASE

    if host.endswith(suffix):
        slug = host[: -len(suffix)]
        if not slug or slug in _RESERVED_SUBDOMAINS:
            return  # main domain or unrelated host — normal routing continues
        db = SessionLocal()
        try:
            gen = db.query(Generation).filter(
                Generation.subdomain == slug,
                Generation.status == "live",
            ).first()
            if gen:
                return _inject_badge(gen.html_content), 200, {"Content-Type": "text/html; charset=utf-8"}
        finally:
            db.close()
        return (
            "<!DOCTYPE html><html><head><meta charset='utf-8'>"
            "<title>Site not found — Groundwork</title></head>"
            "<body style='font-family:sans-serif;padding:60px;text-align:center'>"
            f"<h2>No site found at {slug}.{_SUBDOMAIN_BASE}</h2>"
            "<p>This address doesn't belong to an active Groundwork site.</p>"
            "<p><a href='https://groundworkbuild.com'>groundworkbuild.com</a></p>"
            "</body></html>",
            404,
            {"Content-Type": "text/html; charset=utf-8"},
        )

    if host == _SUBDOMAIN_BASE or host == "www." + _SUBDOMAIN_BASE:
        return  # our own marketing domain — normal routing continues

    # Not a *.groundworkbuild.com host — check whether it's a customer's own
    # connected custom domain (apex or www) instead.
    bare_host = host[4:] if host.startswith("www.") else host
    db = SessionLocal()
    try:
        dom = db.query(Domain).filter(
            Domain.domain == bare_host,
            Domain.status == "active",
        ).first()
        if dom and dom.generation_id:
            gen = db.query(Generation).filter(
                Generation.id == dom.generation_id,
                Generation.status == "live",
            ).first()
            if gen:
                return _inject_badge(gen.html_content), 200, {"Content-Type": "text/html; charset=utf-8"}
    finally:
        db.close()
    return  # unrelated host (e.g. a Railway healthcheck) — normal routing continues


# In-memory job store for in-flight generations: id -> {status, html, error}
# This is a live-progress cache only — completed generations are persisted to
# the `generations` table and served from there once this entry ages out
# (e.g. after a process restart).
_jobs = {}
_jobs_lock = threading.Lock()

UPLOAD_DIR = os.path.join(os.path.dirname(__file__), "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)


def _map_form(form, logo_present, has_photos):
    prestige_map = {"standard": "standard", "mix": "mid", "bespoke": "high"}
    team_map = {"sole": "sole trader", "small": "small team", "company": "established company"}
    urgency_map = {"emergency": "high", "ahead": "low"}
    commercial = int(form.get("commercial_split", 50))
    domestic = 100 - commercial
    if commercial >= 60:
        commercial_lean = "commercial-majority"
    elif commercial <= 40:
        commercial_lean = "domestic-majority"
    else:
        commercial_lean = "balanced"

    data = {
        "business_name": form.get("business_name", ""),
        "trade": form.get("trade", ""),
        "location": form.get("location", ""),
        "coverage_area": form.get("coverage_area", ""),
        "phone": form.get("phone", ""),
        "email": form.get("email", ""),
        "logo_uploaded": bool(logo_present),
        "portfolio_uploaded": bool(has_photos),
        "work_split": f"{domestic}% domestic / {commercial}% commercial",
        "commercial_lean": commercial_lean,
        "craft_prestige": prestige_map.get(form.get("work_type", ""), "standard"),
        "team_size": team_map.get(form.get("team_size", ""), "sole trader"),
        "large_commercial_contracts": form.get("large_contracts") == "yes",
        "urgency": urgency_map.get(form.get("urgency", ""), "low"),
        "years_trading": form.get("years_trading", ""),
        "claimed_accreditations": form.get("accreditations", ""),
        "claimed_projects": form.get("past_clients", ""),
        "other_notes": form.get("notes", ""),
    }

    return data


_BG_UNIFORM_TOLERANCE = 14   # per-channel max deviation across sampled border points to call it "uniform"
_FLOODFILL_THRESH = 18       # per-channel tolerance for the flood-fill match itself
_MIN_LOGO_DIMENSION = 24     # below this, don't attempt background processing at all
_MAX_TRANSPARENT_FRACTION = 0.97  # if flood-fill eats almost the whole image, bail out — likely misdetection

# Dominant/secondary colour extraction (_extract_logo_colors) tuning — kept as
# named, tunable constants rather than hardcoded literals, since "what counts
# as a genuinely distinct secondary colour vs. anti-aliasing noise" is a
# judgment call, not something with one objectively correct value.
_QUANTIZE_BUCKET = 10             # RGB bucket size when histogramming — merges near-identical (anti-aliasing/compression) colours into one bucket
_MIN_SECONDARY_DISTANCE = 60      # min Euclidean RGB distance from the primary colour before a colour counts as a distinct secondary accent
_NEAR_WHITE_BLACK_THRESHOLD = 25  # channel distance from pure white/black before a colour still counts as "just text/background", not a brand accent


def _sample_border_points(img_rgb):
    """Corners plus edge midpoints — enough to catch a busy/gradient background
    without being fooled by a logo mark that happens to touch one corner."""
    w, h = img_rgb.size
    xs = [0, w // 2, w - 1]
    ys = [0, h // 2, h - 1]
    points = [(x, y) for x in xs for y in ys if (x, y) != (w // 2, h // 2)]
    px = img_rgb.load()
    return [px[x, y] for x, y in points]


def _channelwise_spread(samples):
    spread = 0
    for c in range(3):
        vals = [s[c] for s in samples]
        spread = max(spread, max(vals) - min(vals))
    return spread


def _quantize_pixel(pixel, bucket_size: int = _QUANTIZE_BUCKET) -> tuple:
    return tuple((c // bucket_size) * bucket_size for c in pixel)


def _color_distance(c1, c2) -> float:
    return sum((a - b) ** 2 for a, b in zip(c1, c2)) ** 0.5


def _is_near_white_or_black(color, threshold: int = _NEAR_WHITE_BLACK_THRESHOLD) -> bool:
    r, g, b = color
    return (r > 255 - threshold and g > 255 - threshold and b > 255 - threshold) or \
           (r < threshold and g < threshold and b < threshold)


def _extract_logo_colors(img_rgb, min_secondary_distance: int = _MIN_SECONDARY_DISTANCE):
    """
    Histogram-based colour extraction over the *whole* logo image, not just
    border/corner samples: primary is the single most common quantized
    colour bucket — the actual dominant colour, not an average/blend of a
    handful of sample points. Secondary is the next most common bucket that
    is both far enough from primary (min_secondary_distance) to be a
    genuinely distinct colour rather than anti-aliasing/compression noise,
    and not itself near-white/near-black — the 2nd-most-frequent colour in a
    logo is very often just body text or a plain background tint, not a
    brand accent, so those are explicitly excluded rather than picked by
    frequency rank alone. Returns (primary_hex, secondary_hex_or_None); both
    hexes are exact values taken straight from the image, never blended.
    """
    # NEAREST, not the default interpolating filter — this is flat-colour
    # logo art, not a photo. Bicubic/Lanczos resizing blends adjacent flat
    # colours together at every edge, manufacturing intermediate shades that
    # don't actually exist in the logo and can outrank (or masquerade as) a
    # genuine distinct colour in the histogram. Nearest-neighbour preserves
    # only colours that were literally present in the source image.
    img_small = img_rgb.resize((150, 150), Image.NEAREST)
    counts = Counter(_quantize_pixel(p) for p in img_small.getdata())
    ranked = counts.most_common()
    if not ranked:
        return None, None

    primary = ranked[0][0]
    primary_hex = "#{:02x}{:02x}{:02x}".format(*primary)

    secondary_hex = None
    for color, _count in ranked[1:]:
        if _color_distance(color, primary) < min_secondary_distance:
            continue
        if _is_near_white_or_black(color):
            continue
        secondary_hex = "#{:02x}{:02x}{:02x}".format(*color)
        break

    return primary_hex, secondary_hex


def _process_logo(path: str, max_dimension: int):
    """
    Logo-specific processing on top of the generic resize/encode path:
    - If the logo already has real transparency, leave it alone (already fine).
    - Else sample the border for a near-uniform background colour. If found,
      flood-fill it to transparent (from all four corners, so background
      trapped *inside* the mark — e.g. the hole in a letter "O" — survives),
      with a light blur on the alpha edge to avoid a harsh cutout ring.
    - If the border isn't uniform (photo/gradient/busy background), don't
      attempt removal — instead bake the logo into a small rounded-rect chip
      filled with the image's true dominant colour (via _extract_logo_colors'
      whole-image histogram, not a border-sample average/blend). That colour
      is also returned (as bg_hex) so the caller can force the generated
      site's nav background to the exact same hex — at that point the chip's
      fill and the nav behind it are identical, so the rounded-rect edge is
      invisible and the logo reads as part of the page rather than a
      pasted-in badge. A distinct secondary colour, when the logo has one
      (accent_hex), is also extracted for the caller to force as the site's
      accent colour, the same way.
    - Any failure, or an image too small/ambiguous to trust, falls back to
      today's plain behaviour (resize + encode as-is) rather than risking a
      mangled result.

    Returns (mode, PIL.Image, bg_hex, accent_hex) where mode is "as_is",
    "transparent", or "chip"; bg_hex/accent_hex are only set when
    mode == "chip" (accent_hex may still be None even then, if the logo has
    no colour distinct enough from bg_hex and from white/black to count).
    """
    try:
        with Image.open(path) as raw:
            img = raw.convert("RGBA") if raw.mode in ("RGBA", "LA", "P") else raw.convert("RGB")

        w, h = img.size
        if min(w, h) < _MIN_LOGO_DIMENSION:
            return "as_is", img, None, None

        already_transparent = img.mode == "RGBA" and img.getchannel("A").getextrema()[0] < 255
        if already_transparent:
            return "as_is", img, None, None

        img_rgb = img.convert("RGB")
        samples = _sample_border_points(img_rgb)
        spread = _channelwise_spread(samples)

        if spread > _BG_UNIFORM_TOLERANCE:
            # Busy/gradient background — badge it instead of cutting it out.
            bg_hex, accent_hex = _extract_logo_colors(img_rgb)
            if bg_hex is None:
                bg_hex = "#{:02x}{:02x}{:02x}".format(*_sample_border_points(img_rgb)[0])  # pathological empty-histogram fallback
            bg_colour = _hex_to_rgb(bg_hex)
            return "chip", _make_logo_chip(img, bg_colour, max_dimension), bg_hex, accent_hex

        # Uniform background — flood-fill it away from all four corners.
        # The marker colour must be far from the background colour: Pillow's
        # ImageDraw.floodfill(..., thresh=N) silently fills nothing at all if
        # the fill value is within `thresh` of the pixels being replaced (it
        # treats them as "already done"). A fixed near-black marker like
        # (1, 2, 3) works for light backgrounds but is a no-op for dark ones —
        # exactly the case here (a black/near-black logo background), which
        # is why floodfill removal was silently never happening for logos
        # like this and they were falling through to the "as_is" bailout with
        # no chip/nav-matching applied at all. Pick an obscure marker value
        # (never pure 0/0/0 or 255/255/255 — those are common real foreground
        # colours, e.g. white lettering, and would be wrongly erased too) on
        # whichever extreme is far from the background's own brightness.
        bg_sample_avg = tuple(sum(s[c] for s in samples) // len(samples) for c in range(3))
        marker = (254, 253, 252) if sum(bg_sample_avg) < 384 else (1, 2, 3)
        filled = img_rgb.copy()
        draw = ImageDraw.Draw(filled)
        for seed in [(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)]:
            try:
                ImageDraw.floodfill(filled, seed, marker, thresh=_FLOODFILL_THRESH)
            except Exception:
                pass

        filled_px = filled.load()
        alpha = Image.new("L", (w, h), 255)
        alpha_px = alpha.load()
        transparent_count = 0
        for y in range(h):
            for x in range(w):
                if filled_px[x, y] == marker:
                    alpha_px[x, y] = 0
                    transparent_count += 1

        if transparent_count == 0 or (transparent_count / (w * h)) > _MAX_TRANSPARENT_FRACTION:
            # Nothing removed, or removal ate almost the whole logo — misdetection, bail out safely.
            return "as_is", img, None, None

        alpha = alpha.filter(ImageFilter.GaussianBlur(radius=1.0))
        out = img.convert("RGBA")
        out.putalpha(alpha)
        return "transparent", out, None, None

    except Exception:
        with Image.open(path) as raw:
            img = raw.convert("RGBA") if raw.mode in ("RGBA", "LA", "P") else raw.convert("RGB")
            return "as_is", img, None, None


def _make_logo_chip(img, bg_colour: tuple, max_dimension: int) -> "Image.Image":
    w, h = img.size
    padding = max(int(0.15 * max(w, h)), 14)
    new_w, new_h = w + 2 * padding, h + 2 * padding
    radius = max(8, min(20, new_w // 6, new_h // 6))

    mask = Image.new("L", (new_w, new_h), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, new_w - 1, new_h - 1], radius=radius, fill=255)

    chip = Image.new("RGBA", (new_w, new_h), bg_colour + (255,))
    chip.putalpha(mask)

    logo_rgba = img.convert("RGBA")
    chip.paste(logo_rgba, (padding, padding), logo_rgba)

    if max(chip.size) > max_dimension:
        chip.thumbnail((max_dimension, max_dimension), Image.LANCZOS)
    return chip


def _encode_pil_image_to_data_uri(img, max_dimension: int, jpeg_quality: int = 82) -> str:
    """
    Downsizes a PIL image if larger than max_dimension on its longest side
    (these are web display images, not originals) and returns a data: URI.
    PNG is kept for images with real transparency; everything else is
    re-encoded as JPEG to keep the embedded HTML small.
    """
    has_alpha = img.mode == "RGBA" and img.getchannel("A").getextrema()[0] < 255

    if max(img.size) > max_dimension:
        img = img.copy()
        img.thumbnail((max_dimension, max_dimension), Image.LANCZOS)

    buf = io.BytesIO()
    if has_alpha:
        img.save(buf, format="PNG", optimize=True)
        mime = "image/png"
    else:
        img.convert("RGB").save(buf, format="JPEG", quality=jpeg_quality, optimize=True)
        mime = "image/jpeg"

    encoded = base64.standard_b64encode(buf.getvalue()).decode()
    return f"data:{mime};base64,{encoded}"


def _image_file_to_data_uri(path: str, max_dimension: int, jpeg_quality: int = 82) -> str:
    """Reads an image off disk and encodes it — see _encode_pil_image_to_data_uri."""
    with Image.open(path) as raw:
        img = raw.convert("RGBA") if raw.mode in ("RGBA", "LA", "P") else raw.convert("RGB")
        return _encode_pil_image_to_data_uri(img, max_dimension, jpeg_quality)


def _logo_file_to_data_uri(path: str, max_dimension: int):
    """
    Logo-specific: runs background detection/removal (_process_logo) before
    encoding, then encodes the result. Returns (data_uri, mode, bg_hex,
    accent_hex) where mode is "as_is" / "transparent" / "chip", and
    bg_hex/accent_hex are the chip's dominant/secondary colours (only set
    when mode == "chip", see _process_logo).
    """
    mode, img, bg_hex, accent_hex = _process_logo(path, max_dimension)
    return _encode_pil_image_to_data_uri(img, max_dimension, jpeg_quality=90), mode, bg_hex, accent_hex


def _build_media_placeholders(job_dir, logo_path):
    """
    Scans a lead's upload directory and builds:
    - build_data overrides (logo_src_token / photo_src_tokens) for the prompt
    - image_placeholders: token -> real data URI, substituted into the HTML
      after Claude generates it (Claude only ever sees the short token
      strings, never the base64 data itself, so it never has to reproduce
      long strings verbatim and the prompt stays small).
    """
    image_placeholders = {}
    build_overrides = {}

    if logo_path:
        logo_file = os.path.join(job_dir, logo_path)
        if os.path.exists(logo_file):
            token = "GW_LOGO_SRC"
            data_uri, mode, bg_hex, accent_hex = _logo_file_to_data_uri(logo_file, max_dimension=480)
            image_placeholders[token] = data_uri
            build_overrides["logo_src_token"] = token
            if mode == "chip" and bg_hex:
                # The logo was baked onto a solid-colour chip (busy/gradient
                # original background) — force the nav to that exact hex so
                # the chip's edge is invisible against it, not a mismatched box.
                build_overrides["logo_bg_hex"] = bg_hex
                if accent_hex:
                    # A genuinely distinct secondary colour was found in the
                    # logo (not near-white/black, not a near-duplicate of the
                    # dominant colour) — force it as the site's accent colour
                    # too, rather than letting Claude pick its own.
                    build_overrides["logo_accent_hex"] = accent_hex

    if os.path.isdir(job_dir):
        photo_files = sorted(f for f in os.listdir(job_dir) if f.startswith("photo_"))
        if photo_files:
            tokens = []
            for i, fname in enumerate(photo_files):
                token = f"GW_PHOTO_SRC_{i}"
                image_placeholders[token] = _image_file_to_data_uri(os.path.join(job_dir, fname), max_dimension=1600)
                tokens.append(token)
            build_overrides["photo_src_tokens"] = tokens

    return build_overrides, image_placeholders


# claude-sonnet-4-6 published per-token pricing, $/million tokens — used to
# estimate each generation's API cost (app.py has no Anthropic Admin API key
# to pull a real usage/cost figure, so this is computed from token counts
# already logged in _run() x these published rates).
_SONNET_PRICE_PER_MTOK = {
    "input": 3.00,
    "output": 15.00,
    "cache_write": 3.75,
    "cache_read": 0.30,
}


def _estimate_generation_cost_usd(usage_totals: dict) -> float:
    p = _SONNET_PRICE_PER_MTOK
    return (
        usage_totals.get("input_tokens", 0) * p["input"]
        + usage_totals.get("output_tokens", 0) * p["output"]
        + usage_totals.get("cache_creation_input_tokens", 0) * p["cache_write"]
        + usage_totals.get("cache_read_input_tokens", 0) * p["cache_read"]
    ) / 1_000_000


def _run(job_id, prompt, logo_b64, logo_mime):
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    usage_totals = {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
    try:
        content = []
        if logo_b64 and logo_mime:
            content.append({
                "type": "image",
                "source": {"type": "base64", "media_type": logo_mime, "data": logo_b64},
            })
        # Mark the prompt for caching.  Every subsequent turn in this loop
        # re-sends the full message history; without a cache breakpoint the
        # entire prompt is billed as fresh input on every continuation turn.
        # With caching: turn 1 pays cache_write (1.25× base cost), turns 2+
        # pay cache_read (0.1× base cost) — net saving starts at turn 3.
        content.append({"type": "text", "text": prompt, "cache_control": {"type": "ephemeral"}})

        messages = [{"role": "user", "content": content}]
        accumulated_text = ""

        for _ in range(15):
            resp = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=16000,
                tools=[{"type": "web_search_20250305", "name": "web_search"}],
                messages=messages,
            )

            u = resp.usage
            usage_totals["input_tokens"] += u.input_tokens
            usage_totals["output_tokens"] += u.output_tokens
            usage_totals["cache_creation_input_tokens"] += getattr(u, "cache_creation_input_tokens", 0) or 0
            usage_totals["cache_read_input_tokens"] += getattr(u, "cache_read_input_tokens", 0) or 0
            app.logger.info(
                f"Generation {job_id} turn usage: "
                f"in={u.input_tokens} out={u.output_tokens} "
                f"cache_write={getattr(u,'cache_creation_input_tokens',0)} "
                f"cache_read={getattr(u,'cache_read_input_tokens',0)}"
            )

            # Collect any text from this turn
            for block in resp.content:
                if hasattr(block, "text"):
                    accumulated_text += block.text

            if resp.stop_reason == "end_turn":
                break

            # Serialise content blocks to plain dicts so we can attach
            # cache_control.  Mark the last text block — typically the partial
            # HTML on a max_tokens turn — so the accumulated output is also
            # served from cache on the next continuation rather than re-billed
            # as fresh input tokens.
            assistant_blocks = []
            last_text_idx = None
            for i, block in enumerate(resp.content):
                b = block.model_dump(exclude_none=True) if hasattr(block, "model_dump") else dict(block)
                if b.get("type") == "text":
                    last_text_idx = i
                assistant_blocks.append(b)
            if last_text_idx is not None:
                assistant_blocks[last_text_idx]["cache_control"] = {"type": "ephemeral"}
            messages.append({"role": "assistant", "content": assistant_blocks})

            if resp.stop_reason == "max_tokens":
                # Output was cut off — ask Claude to continue from exactly where
                # it stopped. This handles long site generations that exceed the
                # per-turn token limit; the loop will keep asking until the HTML
                # is complete (end_turn) or the 15-turn ceiling is hit.
                app.logger.warning(f"Generation {job_id}: max_tokens hit, requesting continuation")
                messages.append({"role": "user", "content": [{"type": "text", "text": "The response was cut off by the token limit. Please continue the HTML from exactly where you stopped — complete all remaining open tags and sections without repeating any content already written. Your continuation is concatenated directly onto the end of what you already wrote, character for character, with nothing inserted between them — so if the cutoff happened mid-string, mid-attribute, or mid-token (e.g. right after a quote character inside a JS string literal), continue with the literal next character(s) that belong there, with zero leading whitespace, newline, or reformatting of any kind. A stray newline inserted inside a single-quoted JS string is invalid syntax and will break the entire script on the page — resume the exact raw text as if it had never been interrupted, not as a fresh line of output."}]})
                continue

            # Continue conversation for tool_use turns
            tool_results = [
                {"type": "tool_result", "tool_use_id": b["id"], "content": ""}
                for b in assistant_blocks
                if b.get("type") == "tool_use"
            ]
            if tool_results:
                messages.append({"role": "user", "content": tool_results})
            else:
                break

        # Extract HTML block
        lower = accumulated_text.lower()
        idx = lower.find("<!doctype html>")
        html = accumulated_text[idx:] if idx != -1 else accumulated_text

        # Truncate anything after the closing </html> — the model
        # sometimes appends its own trailing commentary/notes after
        # finishing the page (caught: a markdown note admitting a
        # testimonial card wasn't backed by a verified quote and should be
        # removed — commentary that should never reach stored html_content
        # at all, let alone ship instead of the model just fixing the page
        # itself). Keep only up to and including the real closing tag.
        html_lower = html.lower()
        close_idx = html_lower.rfind("</html>")
        if close_idx != -1:
            html = html[:close_idx + len("</html>")]

        # Strip stray markdown code-fence markers. Each continuation turn's
        # text is concatenated onto accumulated_text (see the loop above);
        # when a max_tokens cutoff lands mid-output, the model sometimes
        # opens a fresh ```html fence on the continuation as if starting a
        # new code block, leaving a literal ``` or ```html sitting wherever
        # the cutoff happened in the middle of the page (caught: one
        # appeared right after a contact form's "Your Name" label). Real
        # generated HTML never legitimately contains a bare triple-backtick
        # line, so this is always safe to strip.
        html = re.sub(r"```html", "", html, flags=re.IGNORECASE)
        html = html.replace("```", "")

        with _jobs_lock:
            _jobs[job_id] = {"status": "done", "html": html, "cost_usd": _estimate_generation_cost_usd(usage_totals)}

    except Exception as exc:
        with _jobs_lock:
            _jobs[job_id] = {"status": "error", "error": str(exc)}


def _hex_to_rgb(hex_color: str):
    h = hex_color.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _relative_luminance(rgb) -> float:
    def chan(c):
        c = c / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = rgb
    return 0.2126 * chan(r) + 0.7152 * chan(g) + 0.0722 * chan(b)


def _contrast_ratio(hex_a: str, hex_b: str) -> float:
    lum_a = _relative_luminance(_hex_to_rgb(hex_a))
    lum_b = _relative_luminance(_hex_to_rgb(hex_b))
    lighter, darker = max(lum_a, lum_b), min(lum_a, lum_b)
    return (lighter + 0.05) / (darker + 0.05)


def _adjust_to_contrast(hex_color: str, bg_hex: str, min_ratio: float = 4.5) -> str:
    """Darkens hex_color (or lightens it, against a dark bg) in HSL space,
    one step at a time, until it clears min_ratio against bg_hex. Gives up
    and returns the last value tried if it can't get there (near-black vs
    near-white text should always converge well before that)."""
    import colorsys
    r, g, b = _hex_to_rgb(hex_color)
    h, l, s = colorsys.rgb_to_hls(r / 255, g / 255, b / 255)
    darken = _relative_luminance(_hex_to_rgb(bg_hex)) > 0.5

    for _ in range(48):
        if _contrast_ratio(hex_color, bg_hex) >= min_ratio:
            break
        l = max(0.0, l - 0.02) if darken else min(1.0, l + 0.02)
        r2, g2, b2 = colorsys.hls_to_rgb(h, l, s)
        hex_color = "#{:02x}{:02x}{:02x}".format(round(r2 * 255), round(g2 * 255), round(b2 * 255))
        if l <= 0.0 or l >= 1.0:
            break
    return hex_color


_TEXT_COLOR_RE = re.compile(r"(?<![\w-])color:\s*(#[0-9a-fA-F]{6}|#[0-9a-fA-F]{3})\b", re.IGNORECASE)
_BODY_BG_RE = re.compile(r"<body[^>]*style=\"[^\"]*?background(?:-color)?:\s*(#[0-9a-fA-F]{6}|#[0-9a-fA-F]{3})", re.IGNORECASE)
_BODY_RULE_BG_RE = re.compile(r"body\s*\{[^}]*?background(?:-color)?:\s*(#[0-9a-fA-F]{6}|#[0-9a-fA-F]{3})", re.IGNORECASE)
_BACKGROUND_PROP_RE = re.compile(r"background(-color)?:", re.IGNORECASE)
_STYLE_ATTR_RE = re.compile(r'style="([^"]*)"')
_CSS_RULE_RE = re.compile(r"\{([^{}]*)\}")


def _fix_low_contrast_text(html: str, min_ratio: float = 4.5) -> str:
    """
    Best-effort WCAG AA contrast pass over the generated HTML. There's no
    contrast validation anywhere else in the pipeline — colours are otherwise
    entirely trusted from the model's output. This is not a full CSS cascade
    resolver: it assumes one dominant page background (true for these
    single-file, single-surface generated sites — no dark-mode toggle, no
    per-section theme switching per the design spec).

    To avoid "fixing" a colour pair that's actually fine — e.g. white nav
    text that's only low-contrast against the *page* background because it's
    actually sitting on the nav's own, different, deliberately-set
    background — a `color:` declaration is only checked/adjusted when the
    same inline style attribute or CSS rule block does NOT also set its own
    background. A rule with both is self-contained (its own local backdrop)
    and can't be safely judged against the page background at all, so it's
    left untouched rather than risk making it worse.

    Every failing `color:` found in an eligible (no local background) block
    is replaced with an adjusted hex (darkened/lightened in HSL space,
    preserving hue) that clears min_ratio, scoped to just that declaration —
    never a document-wide string replace of the hex value, since the same
    hex could legitimately appear elsewhere in an unrelated background/border.
    """
    bg_match = _BODY_BG_RE.search(html) or _BODY_RULE_BG_RE.search(html)
    bg_hex = bg_match.group(1) if bg_match else "#ffffff"

    def _fix_block(block: str) -> str:
        if _BACKGROUND_PROP_RE.search(block):
            return block

        def repl(m):
            hex_color = m.group(1)
            try:
                if _contrast_ratio(hex_color, bg_hex) >= min_ratio:
                    return m.group(0)
                fixed = _adjust_to_contrast(hex_color, bg_hex, min_ratio)
            except Exception:
                return m.group(0)
            return m.group(0).replace(hex_color, fixed)

        return _TEXT_COLOR_RE.sub(repl, block)

    html = _STYLE_ATTR_RE.sub(lambda m: 'style="' + _fix_block(m.group(1)) + '"', html)
    html = _CSS_RULE_RE.sub(lambda m: "{" + _fix_block(m.group(1)) + "}", html)
    return html


def _token_to_slot(token: str) -> str:
    """GW_LOGO_SRC -> "logo", GW_PHOTO_SRC_0 -> "photo_0" — the GenerationImage.slot value."""
    if token == "GW_LOGO_SRC":
        return "logo"
    if token.startswith("GW_PHOTO_SRC_"):
        return "photo_" + token[len("GW_PHOTO_SRC_"):]
    return token


def _data_uri_mime(data_uri: str) -> str:
    return data_uri.split(";", 1)[0].removeprefix("data:") if data_uri.startswith("data:") else ""


def _run_and_persist(job_id, lead_id, email, business_name, prompt, logo_b64, logo_mime, image_placeholders=None):
    _run(job_id, prompt, logo_b64, logo_mime)
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job or job.get("status") != "done":
        return

    html = _fix_low_contrast_text(job["html"])
    # Substitute contact form tokens before image tokens so GW_SITE_ID/GW_CONTACT_URL
    # are never accidentally embedded inside a data URI string.
    html = html.replace("GW_SITE_ID", job_id)
    html = html.replace("GW_CONTACT_URL", f"{SITE_URL}/api/contact")
    if image_placeholders:
        for token, data_uri in image_placeholders.items():
            html = html.replace(token, data_uri)
        logo_uri = image_placeholders.get("GW_LOGO_SRC")
        if logo_uri:
            favicon_uri = _logo_to_favicon(logo_uri)
            if favicon_uri:
                head_close = html.find("</head>")
                favicon_tag = f'<link rel="icon" type="image/png" sizes="32x32" href="{favicon_uri}">'
                if head_close != -1:
                    html = html[:head_close] + favicon_tag + html[head_close:]
                else:
                    html = favicon_tag + html
    with _jobs_lock:
        _jobs[job_id]["html"] = html

    db = SessionLocal()
    try:
        gen = Generation(
            lead_id=lead_id,
            email=email,
            business_name=business_name,
            html_content=html,
            status="draft",
            generation_cost_usd=job.get("cost_usd"),
            prompt_version_hash=PROMPT_VERSION_HASH,
        )
        db.add(gen)
        db.flush()  # assigns gen.id for the GenerationImage rows below

        for token, data_uri in (image_placeholders or {}).items():
            db.add(GenerationImage(
                generation_id=gen.id,
                slot=_token_to_slot(token),
                data_uri=data_uri,
                mime=_data_uri_mime(data_uri),
            ))

        db.commit()

        lead_is_test = db.get(Lead, lead_id).is_test
        # Outreach magic-link generations go through an admin approval gate
        # (added 2026-07-24) before the customer ever hears from us — added
        # after a bug let broken-image sites reach real prospects
        # unreviewed. Direct-signup generations (no Prospect behind this
        # lead) aren't gated; they've already verified their own email and
        # are waiting on their own site, same as always.
        #
        # The gate is per prompt version, not per generation: once an admin
        # approves any generation built from a given build_prompt.py hash
        # (PromptApproval row exists for it), every later generation sharing
        # that hash skips straight to notifying the customer. Only an actual
        # prompt change (a new hash, no PromptApproval row yet) re-arms it.
        is_outreach_generation = db.query(Prospect).filter(Prospect.lead_id == lead_id).first() is not None
        prompt_already_approved = db.get(PromptApproval, PROMPT_VERSION_HASH) is not None
        gen_id = gen.id
    finally:
        db.close()

    # Fired for every caller of _kickoff_generation (the public /verify/<token>
    # funnel, the outreach magic-link claim flow, and the logged-in fast path)
    # — a real share of people click the link and don't sit through the ~3
    # minute build, so the loading page's own poll-and-redirect alone loses
    # them. This is a second, independent notification once the site is
    # actually ready; it points at /account/login (email-only — they already
    # proved the address by clicking the original link) as well as the direct
    # preview link, so they can get back to it later even if they close the tab.
    # Skipped for admin test generations (/admin/generate-test) — those aren't
    # real customers and shouldn't get a "your website is ready" email.
    if email and not lead_is_test:
        if is_outreach_generation and not prompt_already_approved:
            send_admin_approval_email(
                business_name,
                email,
                # /admin/generations/<id>/preview, not the public
                # /api/generate/<job_id>/html the customer gets — that
                # route is untracked (see admin_generation_preview), so
                # reviewing it never contributes to the customer's own
                # view_count/engagement stats.
                preview_url=f"{SITE_URL}/admin/generations/{gen_id}/preview",
                approve_url=f"{SITE_URL}/admin/generations/{gen_id}/approve",
            )
        else:
            send_site_ready_email(
                email,
                business_name,
                preview_url=f"{SITE_URL}/preview.html?id={job_id}",
                account_login_url=f"{SITE_URL}/account/login",
            )
            # Fixed 2026-07-26: this auto-skip path (already-approved
            # prompt hash, or a non-outreach direct-signup generation)
            # sends the real "your site is ready" email but was never
            # marking customer_notified_at on the Generation row — so an
            # already-notified customer's generation still showed up in
            # the admin "awaiting approval" queue forever, indistinguishable
            # from one genuinely waiting on a first review. Confirmed via
            # real Resend delivery webhooks: several already-approved
            # generations had a real email.delivered event while sitting
            # in that queue. gen_id/db were already closed above, so this
            # needs its own short-lived session.
            db2 = SessionLocal()
            try:
                gen2 = db2.get(Generation, gen_id)
                if gen2 and not gen2.customer_notified_at:
                    gen2.customer_notified_at = datetime.utcnow()
                    db2.commit()
            finally:
                db2.close()


def _client_ip():
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote_addr or ""


def _has_generation(db, email: str) -> bool:
    """Single source of truth for "has this email already generated a site" —
    used by the public /api/generate 409 guard, the account sign-in branching
    logic, and anything else that needs to ask the same question, so they
    can't drift out of sync with each other."""
    return db.query(Generation).filter(Generation.email == email).first() is not None


@app.route("/api/generate", methods=["POST"])
def generate():
    form = request.form
    account_email = session.get("account_email")
    if account_email:
        # Logged-in users can't submit as anyone but their own account email,
        # regardless of what the (client-locked) form field actually contains —
        # enforced here, not just in the UI, so it holds even against a raw
        # API call with a spoofed email while a valid session cookie is sent.
        email = account_email
    else:
        email = (form.get("email") or "").strip().lower()
    if not email:
        return jsonify({"error": "invalid_email", "message": "A valid email address is required."}), 400

    ip = _client_ip()
    base_url = request.host_url.rstrip("/")

    db = SessionLocal()
    try:
        # Block repeat NEW generations from an email that already has one.
        if _has_generation(db, email):
            return jsonify({
                "error": "already_generated",
                "message": "You've already generated a site with this email. Check your inbox for the link, or sign in to your account to find it.",
            }), 409

        # Per-IP rate limit.
        if ip:
            window_start = datetime.utcnow() - timedelta(hours=1)
            recent_from_ip = db.query(Lead).filter(Lead.ip == ip, Lead.created_at >= window_start).count()
            if recent_from_ip >= IP_RATE_LIMIT_PER_HOUR:
                return jsonify({
                    "error": "rate_limited",
                    "message": "Too many submissions from this network recently. Please try again later.",
                }), 429

        # Reuse a still-pending lead for this email instead of creating a duplicate
        # (not applicable to the logged-in fast path, which always creates fresh —
        # an authenticated account has no "pending verification" concept).
        lead = None
        if not account_email:
            pending_window = datetime.utcnow() - timedelta(hours=24)
            lead = (
                db.query(Lead)
                .filter(Lead.email == email, Lead.status == "pending_verification", Lead.created_at >= pending_window)
                .order_by(Lead.created_at.desc())
                .first()
            )
        if lead is None:
            initial_status = "verified" if account_email else "pending_verification"
            lead = Lead(public_id=uuid.uuid4().hex[:10], email=email, ip=ip, status=initial_status, form_data={})
            db.add(lead)
            db.flush()

        job_dir = os.path.join(UPLOAD_DIR, lead.public_id)
        os.makedirs(job_dir, exist_ok=True)

        logo_file = request.files.get("logo")
        logo_path, logo_mime = lead.logo_path, lead.logo_mime
        if logo_file and logo_file.filename:
            ext = os.path.splitext(logo_file.filename)[1] or ".png"
            fname = f"logo{ext}"
            logo_file.save(os.path.join(job_dir, fname))
            logo_path = fname
            logo_mime = logo_file.content_type or "image/png"

        for i, pf in enumerate(request.files.getlist("photos")):
            if pf and pf.filename:
                ext = os.path.splitext(pf.filename)[1] or ".jpg"
                pf.save(os.path.join(job_dir, f"photo_{i}{ext}"))

        has_photos = any(fname.startswith("photo_") for fname in os.listdir(job_dir))

        build_data = _map_form(form, logo_path, has_photos)

        lead.email = email
        lead.ip = ip
        lead.form_data = build_data
        lead.logo_path = logo_path
        lead.logo_mime = logo_mime
        db.commit()

        if account_email:
            # Already an authenticated, verified account — a second email
            # verification round-trip would be redundant friction. Skip
            # straight to generation.
            _kickoff_generation(lead)
            return jsonify({"status": "generating", "id": lead.public_id})

        token = serializer.dumps({"lead_id": lead.id})
        verify_url = f"{base_url}/verify/{token}"
        send_verification_email(email, verify_url, build_data.get("business_name", ""))

        return jsonify({"status": "check_email", "email": email})
    finally:
        db.close()


def _kickoff_generation(lead):
    """
    Shared by /verify/<token>, /admin/generate-test, and the logged-in fast
    path in /api/generate: builds the prompt (with media placeholder tokens),
    reads the original-resolution logo for vision input, and starts the
    background generation thread. Assumes lead.status is already set
    appropriately and lead.form_data/logo_path are populated.
    """
    job_dir = os.path.join(UPLOAD_DIR, lead.public_id)
    build_data = dict(lead.form_data)
    media_overrides, image_placeholders = _build_media_placeholders(job_dir, lead.logo_path)
    build_data.update(media_overrides)
    prompt = build_prompt(build_data)

    # Original-resolution logo bytes, sent as vision input so Claude can
    # extract a real colour palette from it (separate from the resized/
    # background-processed data URI above, which is what actually gets
    # embedded in the HTML).
    logo_b64 = None
    if lead.logo_path:
        logo_file_path = os.path.join(job_dir, lead.logo_path)
        if os.path.exists(logo_file_path):
            with open(logo_file_path, "rb") as f:
                logo_b64 = base64.standard_b64encode(f.read()).decode()

    with _jobs_lock:
        _jobs[lead.public_id] = {"status": "pending"}

    t = threading.Thread(
        target=_run_and_persist,
        args=(lead.public_id, lead.id, lead.email, build_data.get("business_name", ""), prompt, logo_b64, lead.logo_mime, image_placeholders),
        daemon=True,
    )
    t.start()


@app.route("/verify/<token>")
def verify(token):
    try:
        data = serializer.loads(token, max_age=TOKEN_MAX_AGE)
    except SignatureExpired:
        return redirect("/verify-error.html?reason=expired")
    except BadSignature:
        return redirect("/verify-error.html?reason=invalid")

    db = SessionLocal()
    try:
        lead = db.get(Lead, data.get("lead_id"))
        if not lead:
            return redirect("/verify-error.html?reason=invalid")

        # Idempotent: if this lead already has a generation, just send them to it.
        existing_gen = db.query(Generation).filter(Generation.lead_id == lead.id).first()
        if existing_gen:
            with _jobs_lock:
                _jobs[lead.public_id] = {"status": "done", "html": existing_gen.html_content}
            return redirect(f"/api/generate/{lead.public_id}/html")

        lead.status = "verified"
        db.commit()

        _kickoff_generation(lead)

        return redirect(f"/loading.html?id={lead.public_id}")
    finally:
        db.close()


_PAGE_STYLE = """
*{box-sizing:border-box;}
body{margin:0;background:#F5F3EE;font-family:Inter,Arial,sans-serif;color:#1C1C1C;min-height:100vh;}
.wrap{max-width:640px;margin:0 auto;padding:56px 24px;}
h1{font-size:26px;font-weight:800;letter-spacing:-.02em;margin:0 0 16px;}
a.btn{display:inline-block;background:#3B82F6;color:#fff;font-weight:700;text-decoration:none;padding:12px 22px;border-radius:8px;margin-top:8px;}
.card{background:#fff;border:1px solid #E2E0DA;border-radius:12px;padding:20px 24px;margin-bottom:14px;}
.muted{color:#5C5A56;font-size:14px;}
table{width:100%;border-collapse:collapse;font-size:14px;}
th,td{text-align:left;padding:10px 12px;border-bottom:1px solid #E2E0DA;}
th{color:#5C5A56;font-weight:600;font-size:12.5px;text-transform:uppercase;letter-spacing:.04em;}
input[type=text],input[type=password],input[type=email]{width:100%;padding:11px 14px;border:1px solid #D8D5CE;border-radius:8px;font-size:15px;margin-bottom:12px;}
button{background:#3B82F6;color:#fff;border:0;font-weight:700;padding:12px 22px;border-radius:8px;font-size:15px;cursor:pointer;}
.err{color:#B42318;font-size:14px;margin-bottom:12px;}
.badge-test{display:inline-block;background:#B8976A;color:#fff;font-size:10.5px;font-weight:700;letter-spacing:.04em;padding:2px 7px;border-radius:4px;margin-left:8px;vertical-align:middle;}
"""

_ADMIN_STYLE = """
*{box-sizing:border-box;}
html,body{margin:0;padding:0;}
body{font-family:'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#F5F3EE;color:#1C1C1C;min-height:100vh;display:flex;flex-direction:column;}
/* header */
.adm-header{background:#1C1C1C;border-bottom:1px solid #2C2C2C;position:sticky;top:0;z-index:100;}
.adm-header-inner{max-width:1400px;margin:0 auto;padding:0 28px;height:64px;display:flex;align-items:center;justify-content:space-between;gap:20px;}
.adm-logo{display:flex;align-items:center;gap:10px;text-decoration:none;}
.adm-logo span{color:#fff;font-weight:800;font-size:19px;letter-spacing:-.03em;}
.adm-badge{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;background:#2C2C2C;color:#9A9893;padding:3px 7px;border-radius:5px;margin-left:2px;}
.adm-nav{display:flex;align-items:center;gap:4px;}
.adm-nav a{color:#9A9893;text-decoration:none;font-size:13px;font-weight:600;padding:6px 11px;border-radius:7px;transition:background .12s,color .12s;}
.adm-nav a:hover{background:#2C2C2C;color:#fff;}
.adm-nav a.active{background:#2C2C2C;color:#fff;}
.adm-nav .adm-logout{color:#6B7280;margin-left:6px;}
/* content */
.adm-main{flex:1;max-width:1400px;margin:0 auto;width:100%;padding:32px 28px 72px;}
.adm-title{font-size:22px;font-weight:800;letter-spacing:-.02em;margin:0 0 4px;}
.adm-sub{color:#5C5A56;font-size:13.5px;margin:0 0 24px;}
.adm-sub a{color:#3B82F6;text-decoration:none;font-weight:600;}
.adm-sub a:hover{text-decoration:underline;}
/* table card */
.adm-card{background:#fff;border:1px solid #E2E0DA;border-radius:14px;overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,.04);}
table{width:100%;border-collapse:collapse;font-size:13.5px;}
th{text-align:left;padding:11px 14px;background:#FAFAF8;border-bottom:1px solid #E2E0DA;color:#5C5A56;font-weight:600;font-size:11.5px;text-transform:uppercase;letter-spacing:.05em;white-space:nowrap;}
td{text-align:left;padding:11px 14px;border-bottom:1px solid #F0EDE8;vertical-align:middle;}
tr:last-child td{border-bottom:none;}
tr:hover td{background:#FAFAF8;}
a{color:#3B82F6;text-decoration:none;}
a:hover{text-decoration:underline;}
.muted{color:#9A9893;font-size:13px;}
.badge-test{display:inline-block;background:#B8976A;color:#fff;font-size:10px;font-weight:700;letter-spacing:.04em;padding:2px 7px;border-radius:4px;margin-left:6px;vertical-align:middle;}
.status-pill{display:inline-block;font-size:11px;font-weight:700;padding:3px 9px;border-radius:999px;letter-spacing:.02em;}
/* Safety net so any table not already wrapped in an explicit scroll
   container still scrolls instead of breaking the page layout on a
   narrow screen — added 2026-07-25 as part of a general mobile pass
   (real field use: running outreach from a phone away from the desk). */
.adm-card{max-width:100%;overflow-x:auto;-webkit-overflow-scrolling:touch;}
@media (max-width:760px){
  .adm-header-inner{padding:0 14px;height:56px;gap:10px;}
  .adm-logo span{font-size:16px;}
  .adm-badge{display:none;}
  .adm-nav{overflow-x:auto;-webkit-overflow-scrolling:touch;flex-wrap:nowrap;max-width:100%;scrollbar-width:none;}
  .adm-nav::-webkit-scrollbar{display:none;}
  .adm-nav a{white-space:nowrap;padding:7px 10px;font-size:12.5px;}
  .adm-nav .adm-logout{white-space:nowrap;}
  .adm-main{padding:16px 12px 56px;}
  .adm-title{font-size:19px;}
  .adm-sub{font-size:12.5px;}
  table{font-size:12.5px;}
  th,td{padding:8px 10px;}
  /* Forms built as a row of inline label/input blocks (filters, etc.)
     stack cleanly instead of squeezing — every admin filter form in this
     file uses inline flex styles with flex-wrap already set, so this
     just tightens the gap rather than fighting existing layout rules. */
  input[type=date],input[type=text],input[type=email],select{font-size:16px;}
}
"""

_GW_LOGO_SVG = '<svg viewBox="0 0 48 48" width="28" height="28" fill="none"><path d="M 37 13.1 A 17 17 0 1 0 41 24 L 27 24" stroke="#3B82F6" stroke-width="4.6" stroke-linecap="round" stroke-linejoin="round"/><path d="M 30.9 18.2 A 9 9 0 1 0 30.9 29.8" stroke="#3B82F6" stroke-width="4.6" stroke-linecap="round"/></svg>'



def _admin_page(title: str, content: str, active: str = "") -> str:
    """Wrap admin content in the shared dark-header shell."""
    # Condensed 2026-07-21 (was 10 tabs) — Discovery, Follow-ups, Send
    # timing, and Replies are now sections within Pipeline/Funnel/
    # Deliverability rather than separate tabs (their old routes redirect
    # to their new home). Outreach (the filterable prospect browser) stays
    # a real page but is linked from Pipeline instead of getting its own
    # top-level tab, since it's a drill-down tool, not a state-of-things
    # overview like the other five.
    nav_items = [
        ("Dashboard", "/admin",    "dashboard"),
        ("Sites",    "/admin/generations",    "generations"),
        ("Domains &amp; margins", "/admin/domains", "domains"),
        ("Pipeline", "/admin/pipeline", "pipeline"),
        ("Funnel", "/admin/funnel", "funnel"),
        ("Deliverability", "/admin/deliverability", "deliverability"),
        ("Variants", "/admin/variants", "variants"),
    ]
    nav_html = "".join(
        f'<a href="{href}" class="{"active" if active == key else ""}">{label}</a>'
        for label, href, key in nav_items
    )
    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} — Groundwork Admin</title>
<link rel="icon" type="image/x-icon" href="/favicon.ico">
<link rel="icon" type="image/png" sizes="192x192" href="/favicon-192.png">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>{_ADMIN_STYLE}</style></head>
<body>
<header class="adm-header">
  <div class="adm-header-inner">
    <div style="display:flex;align-items:center;gap:14px;">
      <a class="adm-logo" href="/index.html">{_GW_LOGO_SVG}<span>Groundwork</span></a>
      <span class="adm-badge">Admin</span>
    </div>
    <nav class="adm-nav">
      {nav_html}
      <a href="/admin/logout" class="adm-logout">Log out</a>
    </nav>
  </div>
</header>
<main class="adm-main">{content}</main>
</body></html>"""


def _admin_coming_soon(title: str, active: str) -> str:
    """Placeholder page for a reserved nav slot — the nav structure/routes
    exist now so real page content can be dropped in later without another
    round of nav wiring."""
    content = f"""
<h1 class="adm-title">{title}</h1>
<p class="adm-sub">Coming soon — this section isn't built yet.</p>
<div class="adm-card" style="padding:40px;text-align:center;color:#9A9893;font-size:14px;">
  Nothing here yet.
</div>"""
    return render_template_string(_admin_page(title, content, active=active))


# Shared nav/footer markup so /account and other Flask-rendered pages match the
# static frontend pages' look, since there's no shared CSS file in this repo —
# every page (including frontend/index.html) inlines its own styles.
def _site_header() -> str:
    signed_in = bool(session.get("account_email"))
    right_link = (
        '<a href="/account" style="color:#9A9893;text-decoration:none;font-size:15px;font-weight:500;">Dashboard</a>'
        if signed_in else
        '<a href="/account/login" style="color:#9A9893;text-decoration:none;font-size:15px;font-weight:500;">Sign in</a>'
    )
    cta_label = "New site" if signed_in else "Get started"
    return f"""<header style="position:sticky;top:0;z-index:100;background:#1C1C1C;border-bottom:1px solid #2C2C2C;">
  <nav style="max-width:1200px;margin:0 auto;padding:0 24px;height:68px;display:flex;align-items:center;justify-content:space-between;gap:24px;">
    <a href="/index.html" style="display:flex;align-items:center;gap:11px;text-decoration:none;">
      <svg viewBox="0 0 48 48" width="32" height="32" fill="none"><path d="M 37 13.1 A 17 17 0 1 0 41 24 L 27 24" stroke="#3B82F6" stroke-width="4.6" stroke-linecap="round" stroke-linejoin="round"/><path d="M 30.9 18.2 A 9 9 0 1 0 30.9 29.8" stroke="#3B82F6" stroke-width="4.6" stroke-linecap="round"/></svg>
      <span style="color:#fff;font-weight:800;font-size:20px;letter-spacing:-.03em;">Groundwork</span>
    </a>
    <div style="display:flex;align-items:center;gap:20px;">
      {right_link}
      <a href="/build.html" style="background:#3B82F6;color:#fff;font-weight:700;font-size:15px;text-decoration:none;padding:10px 18px;border-radius:7px;">{cta_label}</a>
    </div>
  </nav>
</header>"""


def _site_footer() -> str:
    return """<footer style="background:#1C1C1C;color:#9A9893;margin-top:56px;">
  <div style="max-width:1200px;margin:0 auto;padding:32px 24px 26px;display:flex;flex-wrap:wrap;gap:16px 32px;align-items:center;justify-content:space-between;">
    <a href="/index.html" style="display:flex;align-items:center;gap:10px;text-decoration:none;">
      <svg viewBox="0 0 48 48" width="22" height="22" fill="none"><path d="M 37 13.1 A 17 17 0 1 0 41 24 L 27 24" stroke="#3B82F6" stroke-width="4.6" stroke-linecap="round" stroke-linejoin="round"/><path d="M 30.9 18.2 A 9 9 0 1 0 30.9 29.8" stroke="#3B82F6" stroke-width="4.6" stroke-linecap="round"/></svg>
      <span style="color:#fff;font-weight:800;font-size:15px;letter-spacing:-.03em;">Groundwork</span>
    </a>
    <div style="display:flex;gap:22px;flex-wrap:wrap;">
      <a href="/pricing.html" style="color:#9A9893;text-decoration:none;font-size:13.5px;">Pricing</a>
      <a href="/about.html" style="color:#9A9893;text-decoration:none;font-size:13.5px;">About</a>
      <a href="/contact.html" style="color:#9A9893;text-decoration:none;font-size:13.5px;">Contact</a>
      <a href="mailto:groundwork-build@outlook.com" style="color:#9A9893;text-decoration:none;font-size:13.5px;">groundwork-build@outlook.com</a>
    </div>
    <span style="font-size:13px;color:#5E5C58;">© 2026 Groundwork Ltd.</span>
  </div>
</footer>"""


def _account_page(inner_html: str, title: str) -> str:
    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} — Groundwork</title><link rel="icon" type="image/x-icon" href="/favicon.ico"><link rel="icon" type="image/png" sizes="192x192" href="/favicon-192.png">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=Plus+Jakarta+Sans:wght@700;800&display=swap" rel="stylesheet">
<style>
*{{box-sizing:border-box;}}
h1,h2,h3{{font-family:'Plus Jakarta Sans','Inter',sans-serif;}}
body{{margin:0;background:#FAFAF8;font-family:Inter,sans-serif;color:#1C1C1C;}}
a:focus-visible,button:focus-visible,input:focus-visible{{outline:3px solid #3B82F6;outline-offset:2px;}}
.acct-wrap{{max-width:640px;margin:0 auto;padding:clamp(40px,6vw,64px) 24px;}}
.acct-card{{background:#fff;border:1px solid #E2E0DA;border-radius:14px;padding:24px 26px;margin-bottom:16px;}}
input[type=email]{{width:100%;padding:13px 16px;border:1px solid #D9D7D0;border-radius:10px;font-size:15.5px;margin:14px 0;font-family:Inter,sans-serif;}}
.acct-btn{{display:inline-block;background:#3B82F6;color:#fff;font-weight:700;font-size:15.5px;text-decoration:none;border:0;padding:14px 24px;border-radius:10px;cursor:pointer;}}
.acct-btn:hover{{background:#2563EB;}}
.pw-field{{position:relative;margin:14px 0;}}
.pw-field input[type=password],.pw-field input[type=text]{{width:100%;padding:13px 60px 13px 16px;border:1px solid #D9D7D0;border-radius:10px;font-size:15.5px;font-family:Inter,sans-serif;box-sizing:border-box;}}
.pw-toggle{{position:absolute;right:6px;top:6px;bottom:6px;background:none;border:0;color:#5C5A56;font-size:13px;font-weight:600;cursor:pointer;padding:0 10px;}}
.pw-toggle:hover{{color:#3B82F6;}}
</style>
</head><body>
{_site_header()}
<div class="acct-wrap">{inner_html}</div>
{_site_footer()}
<script>
function gwTogglePw(id, btn){{
  const el = document.getElementById(id);
  const showing = el.type === 'text';
  el.type = showing ? 'password' : 'text';
  btn.textContent = showing ? 'Show' : 'Hide';
}}

async function gwUploadImage(genId, slot, input){{
  const file = input.files[0];
  if (!file) return;
  const statusEl = document.getElementById(`gw-img-${{genId}}-${{slot}}-status`);
  const previewEl = document.getElementById(`gw-img-${{genId}}-${{slot}}-preview`);
  statusEl.textContent = 'Uploading…';
  const fd = new FormData();
  fd.append('image', file);
  try {{
    const r = await fetch(`/api/account/generations/${{genId}}/images/${{slot}}`, {{
      method: 'POST', body: fd, credentials: 'same-origin'
    }});
    const data = await r.json().catch(() => ({{}}));
    if (!r.ok) throw new Error(data.error || ('Upload failed (' + r.status + ')'));
    previewEl.src = data.data_uri;
    statusEl.textContent = 'Updated ✓';
    setTimeout(() => {{ statusEl.textContent = ''; }}, 2500);
  }} catch (err) {{
    statusEl.textContent = 'Failed — try again';
  }} finally {{
    input.value = '';
  }}
}}

async function gwSendSupportMessage(){{
  const ta = document.getElementById('support-message');
  const statusEl = document.getElementById('support-status');
  if (!ta) return;
  const message = ta.value.trim();
  if (!message) {{ statusEl.textContent = 'Write a message first.'; return; }}
  statusEl.textContent = 'Sending…';
  const fd = new FormData();
  fd.append('message', message);
  try {{
    const r = await fetch('/api/account/support-message', {{method:'POST', body: fd, credentials:'same-origin'}});
    const data = await r.json().catch(() => ({{}}));
    if (!r.ok) throw new Error(data.error || ('Failed (' + r.status + ')'));
    statusEl.textContent = "Sent — we'll reply by email.";
    ta.value = '';
  }} catch (err) {{
    statusEl.textContent = 'Something went wrong — try again.';
  }}
}}
</script>
</body></html>"""


_SLOT_LABELS = {"logo": "Logo"}


def _slot_label(slot: str) -> str:
    if slot in _SLOT_LABELS:
        return _SLOT_LABELS[slot]
    if slot.startswith("photo_"):
        return "Photo " + str(int(slot[len("photo_"):]) + 1)
    return slot


def _render_image_manager(gen_id: int, images) -> str:
    if not images:
        return (
            '<p style="margin:12px 0 0;font-size:13px;color:#807E79;">'
            'No logo or photos on file for this site yet — send us a message below and '
            'we\'ll add them for you.</p>'
        )
    tiles = []
    for img in images:
        input_id = f"gw-img-{gen_id}-{img.slot}"
        tiles.append(f"""<div style="text-align:center;">
          <img id="{input_id}-preview" src="{img.data_uri}" alt="{escape(_slot_label(img.slot))}"
               style="width:72px;height:72px;object-fit:cover;border-radius:8px;border:1px solid #E6E3DC;background:#fff;display:block;margin:0 auto 6px;">
          <div style="font-size:12px;color:#807E79;margin-bottom:6px;">{escape(_slot_label(img.slot))}</div>
          <label style="display:inline-block;font-size:12.5px;font-weight:600;color:#3B82F6;cursor:pointer;">
            Change
            <input id="{input_id}" type="file" accept="image/*" style="display:none;"
                   onchange="gwUploadImage({gen_id},'{img.slot}',this)">
          </label>
          <div id="{input_id}-status" style="font-size:11.5px;color:#807E79;margin-top:2px;"></div>
        </div>""")
    return (
        '<div style="display:flex;gap:18px;flex-wrap:wrap;margin-top:14px;padding-top:14px;border-top:1px solid #EDEBE5;">'
        + "".join(tiles) + "</div>"
    )


def _render_dashboard(email: str) -> str:
    db = SessionLocal()
    try:
        gens = db.query(Generation).filter(Generation.email == email).order_by(Generation.created_at.desc()).all()

        if gens:
            # Build a gen_id → most-recent active custom domain lookup so
            # "View site" can point at the real live URL rather than the preview.
            gen_ids = [g.id for g in gens]
            active_domains = (
                db.query(Domain)
                .filter(Domain.generation_id.in_(gen_ids), Domain.status == "active")
                .order_by(Domain.created_at.desc())
                .all()
            )
            gen_domain_map = {}
            for d in active_domains:
                if d.generation_id not in gen_domain_map:
                    gen_domain_map[d.generation_id] = d

            card_parts = []
            for g in gens:
                business_label = g.business_name or "Untitled site"
                if g.status == "live" and g.cancel_at_period_end:
                    ends_str = g.subscription_period_end.strftime("%d %b %Y") if g.subscription_period_end else "the end of your billing period"
                    status_label = f"Live — ending {ends_str}"
                elif g.status == "live":
                    status_label = "Live"
                elif g.status == "canceled":
                    status_label = "Paused — subscription cancelled"
                else:
                    status_label = "Draft — not yet published"
                go_live_link = ""
                if g.status == "canceled":
                    go_live_link = (
                        '<a href="/checkout.html?id=' + g.lead.public_id + '" '
                        'style="display:inline-block;background:#3B82F6;color:#fff;font-weight:700;font-size:14.5px;'
                        'text-decoration:none;padding:11px 18px;border-radius:9px;">Reactivate →</a>'
                    )
                elif g.status != "live":
                    go_live_link = (
                        '<a href="/checkout.html?id=' + g.lead.public_id + '" '
                        'style="display:inline-block;background:#3B82F6;color:#fff;font-weight:700;font-size:14.5px;'
                        'text-decoration:none;padding:11px 18px;border-radius:9px;">Go live →</a>'
                    )
                cancel_link = ""
                if g.status == "live" and not g.cancel_at_period_end:
                    cancel_link = (
                        '<a href="/account/subscription/' + str(g.id) + '/cancel" '
                        'style="display:inline-block;background:#fff;color:#807E79;font-weight:600;font-size:13.5px;'
                        'text-decoration:none;border:1px solid #E6E3DC;padding:11px 16px;border-radius:9px;">Cancel subscription</a>'
                    )
                edit_text_link = (
                    '<a href="/editor.html?id=' + g.lead.public_id + '" '
                    'style="display:inline-block;background:#fff;color:#1C1C1C;font-weight:600;font-size:14px;'
                    'text-decoration:none;border:1px solid #D9D7D0;padding:11px 18px;border-radius:9px;">Edit text</a>'
                )
                # For paid/live sites, link directly to their real web address.
                # Priority: custom domain → groundworkbuild.com subdomain → preview fallback.
                # Canceled sites go to the dedicated preserved-content route
                # (job_html_preserved) — never the old domain/subdomain (both
                # were disconnected on cancellation) and never the watermarked
                # preview route (misleading "unpublished, go live for £99" copy
                # for a site that already went live once).
                if g.status == "live":
                    active_dom = gen_domain_map.get(g.id)
                    if active_dom:
                        view_href = "https://" + active_dom.domain
                    elif g.subdomain:
                        view_href = "https://" + g.subdomain + ".groundworkbuild.com"
                    else:
                        view_href = "/api/generate/" + g.lead.public_id + "/html"
                    view_label = "Visit site →"
                elif g.status == "canceled":
                    view_href = "/api/generate/" + g.lead.public_id + "/preserved"
                    view_label = "View preserved site →"
                else:
                    view_href = "/api/generate/" + g.lead.public_id + "/html"
                    view_label = "View site →"
                card_parts.append(
                    '<div class="acct-card">'
                    '<div style="display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap;">'
                    '<div>'
                    '<div style="font-weight:700;font-size:17px;">' + business_label + '</div>'
                    '<div style="font-size:13.5px;color:#807E79;margin-top:3px;">Generated '
                    + g.created_at.strftime("%d %b %Y") + " · " + status_label + '</div>'
                    '</div>'
                    '<div style="display:flex;gap:10px;flex-wrap:wrap;">'
                    '<a href="' + view_href + '" target="_blank" rel="noopener" '
                    'style="display:inline-block;background:#fff;color:#1C1C1C;font-weight:700;font-size:14.5px;'
                    'text-decoration:none;border:1px solid #D9D7D0;padding:11px 18px;border-radius:9px;">' + view_label + '</a>'
                    + edit_text_link
                    + go_live_link
                    + cancel_link +
                    '</div>'
                    '</div>'
                    '</div>'
                )
            cards = "".join(card_parts)
        else:
            cards = '<div class="acct-card"><p style="margin:0;color:#5C5A56;font-size:15px;">No website yet — once you generate one, it\'ll show up here.</p></div>'

        # Copy is written for the common case — one account, one generated
        # site (enforced by the one-generation-per-email guard) — rather than
        # a generic "your sites" plural that's rarely true for a real user.
        if len(gens) == 1:
            business_label = gens[0].business_name or "Your website"
            headline = f"{escape(business_label)} is ready"
            subcopy = "View it any time, or send us a message if you'd like something changed."
        elif len(gens) > 1:
            headline = "Your sites, all in one place"
            subcopy = f"Every website you've generated with {escape(email)}, ready whenever you need it."
        else:
            headline = "Your Groundwork account"
            subcopy = f"Signed in as {escape(email)}."

        support_card = """<div class="acct-card">
          <div style="font-weight:700;font-size:17px;margin-bottom:4px;">Need something changed?</div>
          <p style="margin:0 0 14px;font-size:14px;color:#5C5A56;">A wording tweak, a new photo, a question about going live — send it straight to the Groundwork team.</p>
          <textarea id="support-message" rows="4" placeholder="What would you like changed?" style="width:100%;padding:13px 15px;border-radius:10px;border:1px solid #D9D7D0;font-size:15px;font-family:Inter,sans-serif;resize:vertical;"></textarea>
          <div style="display:flex;align-items:center;gap:14px;margin-top:10px;">
            <button type="button" class="acct-btn" style="width:auto;padding:12px 22px;" onclick="gwSendSupportMessage()">Send message</button>
            <span id="support-status" style="font-size:13.5px;color:#807E79;"></span>
          </div>
        </div>"""

        customer_ids = sorted({g.stripe_customer_id for g in gens if g.stripe_customer_id})
        billing_card = _render_billing_section(customer_ids)

        # Link straight to the main domain search page, scoped to the
        # customer's most recent site (live first, else first draft) via
        # ?id= so a purchase there still connects to the right generation.
        primary_gen = next((g for g in gens if g.status == "live"), gens[0] if gens else None)
        domain_site_id = primary_gen.lead.public_id if primary_gen else ""
        domain_search_href = "/domain-search.html" + (f"?id={domain_site_id}" if domain_site_id else "")

        # Domains purchased by this email
        purchased_domains = db.query(Domain).filter(
            Domain.customer_email == email,
            Domain.is_internal == False,
        ).order_by(Domain.created_at.desc()).all()

        domain_rows_html = ""
        for pd in purchased_domains:
            if pd.status == "active":
                badge = '<span style="background:#D1FAE5;color:#065F46;font-size:11.5px;font-weight:700;padding:3px 9px;border-radius:20px;">Live</span>'
            elif pd.status == "needs_manual_setup":
                badge = '<span style="background:#FEF3C7;color:#92400E;font-size:11.5px;font-weight:700;padding:3px 9px;border-radius:20px;">Setting up</span>'
            else:
                badge = '<span style="background:#EAF1FD;color:#1E40AF;font-size:11.5px;font-weight:700;padding:3px 9px;border-radius:20px;">Setting up</span>'
            domain_rows_html += (
                f'<div style="display:flex;align-items:center;justify-content:space-between;gap:12px;'
                f'padding:12px 0;border-bottom:1px solid #F0EDE7;">'
                f'<div style="display:flex;align-items:center;gap:10px;min-width:0;">'
                f'{badge}'
                f'<span style="font-weight:600;font-size:14.5px;word-break:break-all;">{escape(pd.domain)}</span>'
                f'</div>'
                f'<a href="/domain-status.html?domain={escape(pd.domain)}" '
                f'style="flex-shrink:0;font-size:13.5px;font-weight:600;color:#3B82F6;text-decoration:none;">View status →</a>'
                f'</div>'
            )

        if domain_rows_html:
            domain_card = f"""<div class="acct-card">
          <div style="display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:12px;">
            <div style="display:flex;align-items:center;gap:12px;">
              <div style="width:34px;height:34px;border-radius:9px;background:#EAF1FD;display:flex;align-items:center;justify-content:center;flex-shrink:0;">
                <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="#3B82F6" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M2 12h20M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/></svg>
              </div>
              <div style="font-weight:700;font-size:17px;">Your domains</div>
            </div>
            <a href="{domain_search_href}" style="font-size:13.5px;font-weight:600;color:#3B82F6;text-decoration:none;white-space:nowrap;">+ Add domain</a>
          </div>
          <div style="border-top:1px solid #F0EDE7;">{domain_rows_html}</div>
        </div>"""
        else:
            domain_card = f"""<div class="acct-card">
          <div style="display:flex;align-items:center;gap:12px;margin-bottom:4px;">
            <div style="width:34px;height:34px;border-radius:9px;background:#EAF1FD;display:flex;align-items:center;justify-content:center;flex-shrink:0;">
              <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="#3B82F6" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M2 12h20M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/></svg>
            </div>
            <div style="font-weight:700;font-size:17px;">Get a custom domain</div>
          </div>
          <p style="margin:0 0 14px;font-size:14px;color:#5C5A56;">Replace your Groundwork link with your own web address — e.g. <strong>apexroofing.co.uk</strong>.</p>
          <a href="{domain_search_href}" style="display:inline-block;background:#3B82F6;color:#fff;font-weight:700;font-size:14.5px;text-decoration:none;padding:11px 18px;border-radius:9px;">Find a domain →</a>
        </div>"""

        inner = f"""<div style="display:flex;justify-content:flex-end;margin-bottom:6px;">
          <a href="/account/logout" style="color:#807E79;font-size:13px;text-decoration:none;">Log out</a>
        </div>
        <div style="text-align:center;margin-bottom:28px;">
          <div style="color:#2257CC;font-size:12.5px;font-weight:700;letter-spacing:.12em;text-transform:uppercase;margin-bottom:10px;">Your account</div>
          <h1 style="margin:0 0 8px;font-weight:800;font-size:clamp(24px,3.4vw,32px);letter-spacing:-.02em;">{headline}</h1>
          <p style="margin:0;font-size:15.5px;color:#5C5A56;">{subcopy}</p>
        </div>
        {support_card}
        {cards}
        {domain_card}
        {billing_card}"""
        return render_template_string(_account_page(inner, "Your account"))
    finally:
        db.close()


def _describe_invoice(invoice) -> str:
    lines = list(invoice.lines.data) if invoice.lines else []
    descriptions = []
    for line in lines:
        desc = line.description or (line.price.nickname if line.price else None)
        if desc and desc not in descriptions:
            descriptions.append(desc)
    if descriptions:
        return ", ".join(descriptions)
    return "Monthly subscription" if invoice.billing_reason == "subscription_cycle" else "First invoice"


def _render_billing_section(customer_ids: list) -> str:
    if not customer_ids or not STRIPE_SECRET_KEY:
        return ""

    invoices = []
    try:
        for customer_id in customer_ids:
            for inv in stripe.Invoice.list(customer=customer_id, limit=100).auto_paging_iter():
                invoices.append(inv)
    except stripe.error.StripeError:
        pass

    if not invoices:
        return """<div class="acct-card">
          <div style="font-weight:700;font-size:17px;margin-bottom:4px;">Billing</div>
          <p style="margin:0;font-size:14px;color:#807E79;">No invoices yet.</p>
        </div>"""

    invoices.sort(key=lambda inv: inv.created, reverse=True)

    rows = []
    for inv in invoices:
        date_str = datetime.utcfromtimestamp(inv.created).strftime("%d %b %Y")
        description = escape(_describe_invoice(inv))
        amount = _format_gbp(inv.amount_paid if inv.status == "paid" else inv.amount_due)
        pdf_link = (
            f'<a href="{inv.invoice_pdf}" target="_blank" rel="noopener" style="color:#2257CC;font-weight:600;font-size:13.5px;text-decoration:none;">Download PDF</a>'
            if inv.invoice_pdf else
            '<span style="color:#9A9893;font-size:13.5px;">Generating…</span>'
        )
        rows.append(
            '<div style="display:flex;align-items:center;justify-content:space-between;gap:14px;padding:12px 0;border-top:1px solid #EDEBE5;flex-wrap:wrap;">'
            f'<div><div style="font-size:14.5px;font-weight:600;">{description}</div>'
            f'<div style="font-size:12.5px;color:#807E79;margin-top:2px;">{date_str}</div></div>'
            f'<div style="display:flex;align-items:center;gap:16px;">'
            f'<span style="font-weight:700;font-size:14.5px;">{amount}</span>{pdf_link}</div>'
            '</div>'
        )

    return f"""<div class="acct-card">
      <div style="font-weight:700;font-size:17px;margin-bottom:4px;">Billing</div>
      <p style="margin:0 0 4px;font-size:14px;color:#5C5A56;">Your full payment history.</p>
      {"".join(rows)}
    </div>"""


RETENTION_COUPON_ID = "groundwork-retention-1mo-free"


def _get_or_create_retention_coupon():
    """
    Idempotently fetch-or-create the 100%-off, one-time coupon used by the
    cancellation retention offer.

    Mechanism chosen after checking Stripe's currently-supported options:
    a duration="once" Coupon applied to the subscription is the standard,
    well-documented pattern for "your next invoice is free, everything else
    unchanged" — it zeroes exactly the next invoice generated for that
    subscription and then stops applying on its own, no code needed to
    track/expire it, no change to the subscription's billing anchor/dates,
    and no separate cleanup step. Alternatives considered and rejected:
    - Extending trial_end by ~30 days: works, but subscriptions here were
      already given a 30-day trial at signup (see create_checkout_session),
      so mutating trial_end on an already-live subscription is a less
      direct/obvious fit than a coupon, which is what Stripe's own docs
      recommend specifically for "one free month" retention offers.
    - pause_collection: pauses indefinitely until manually resumed, which
      is the wrong shape for "skip exactly one cycle then resume normally."
    A fixed coupon `id` makes this call idempotent — creating it twice just
    raises "already exists," handled below by fetching the existing one.
    """
    try:
        return stripe.Coupon.retrieve(RETENTION_COUPON_ID)
    except stripe.error.InvalidRequestError:
        return stripe.Coupon.create(
            id=RETENTION_COUPON_ID,
            percent_off=100,
            duration="once",
            name="One month free (retention offer)",
        )


SURVEY_DISCOUNT_WINDOW_DAYS = 7


def _issue_survey_discount_code(prospect):
    """Generate a single-use, time-limited setup-fee-discount code for this
    prospect. Returns (code, expires_at). SURVEY_DISCOUNT_PERCENT (50, as
    of 2026-07-18 — was a full 100% waiver at launch, reduced since this
    offer is a last-resort MRR push, not a routine incentive) is applied
    in create_checkout_session() as a dynamic price_data line item, not a
    Stripe Coupon/PromotionCode — tried that first and found, against the
    real live Stripe account, that Coupon.create's applies_to.products
    parameter is silently dropped (the created Coupon comes back with no
    applies_to field at all, confirmed by retrieving it straight back). A
    percent_off coupon with no product restriction discounts every line
    item in the Checkout Session, not just the setup fee — it would have
    discounted the recurring subscription too, a real money mistake on a
    live account, not a theoretical one. Redemption is instead fully
    app-side: the code and expiry are stored on
    Prospect.discount_code/discount_expiry (existing, previously-unused
    schema columns) and checked in create_checkout_session(), which — if
    the submitted code matches and hasn't expired — swaps the setup-fee
    line item for a discounted price_data line item rather than involving
    Stripe's discount system at all. No live-API call here, so this can't
    fail, and the exact amount charged is never ambiguous."""
    code = f"SETUP{SURVEY_DISCOUNT_PERCENT}{(prospect.token or str(prospect.id))[:6].upper()}"
    expires_at = datetime.utcnow() + timedelta(days=SURVEY_DISCOUNT_WINDOW_DAYS)
    return code, expires_at


def _resolve_subscription_id(gen, db):
    """
    Returns gen.stripe_subscription_id, backfilling it first if missing —
    covers every Generation that went live before this field existed (all
    5 real live customers today predate it, since checkout.session.completed
    only started capturing cs.subscription with this change). Looks up the
    customer's current subscription via Stripe directly rather than assuming
    the field will always be populated.
    """
    if gen.stripe_subscription_id:
        return gen.stripe_subscription_id
    if not gen.stripe_customer_id:
        return None
    subs = stripe.Subscription.list(customer=gen.stripe_customer_id, status="all", limit=10)
    # Prefer an active/trialing subscription; fall back to the most recent of any status.
    active = [s for s in subs.data if s.status in ("active", "trialing")]
    chosen = active[0] if active else (subs.data[0] if subs.data else None)
    if chosen:
        gen.stripe_subscription_id = chosen.id
        db.commit()
        return chosen.id
    return None


def _account_owns_generation(db, email, gen_id):
    gen = db.get(Generation, gen_id)
    if not gen or gen.email != email:
        return None
    return gen


def _render_cancel_offer_page(gen, error=None) -> str:
    error_html = f'<p class="err">{escape(error)}</p>' if error else ""
    business = escape(gen.business_name or "your site")
    already_used = gen.retention_offer_used_at is not None
    offer_block = "" if already_used else f"""
      <div style="background:#F2F6FF;border:1px solid #BFD7FE;border-radius:12px;padding:20px 22px;margin:18px 0;">
        <div style="font-weight:700;font-size:16px;margin-bottom:6px;">Before you go — have your next month free</div>
        <p style="margin:0 0 14px;font-size:14.5px;color:#3A3A38;line-height:1.5;">No changes to your plan, nothing to set up — your next billing cycle is simply £0. Everything else stays exactly as it is.</p>
        <form method="post" style="margin:0;">
          <input type="hidden" name="action" value="accept_offer">
          <button type="submit" class="acct-btn" style="width:auto;padding:12px 22px;">Yes, give me a free month</button>
        </form>
      </div>"""
    inner = f"""<div class="acct-card">
      <h1 style="margin:0 0 8px;font-weight:800;font-size:26px;letter-spacing:-.02em;">Cancel {business}?</h1>
      <p style="margin:0;font-size:15px;color:#5C5A56;line-height:1.55;">Sorry to see you go.</p>
      {error_html}
      {offer_block}
      <form method="post" style="margin-top:6px;">
        <input type="hidden" name="action" value="confirm_cancel">
        <button type="submit" style="width:100%;background:#fff;color:#B42318;border:1px solid #F3D4D0;font-weight:700;font-size:15px;padding:13px 0;border-radius:10px;cursor:pointer;">
          {"Cancel anyway" if not already_used else "Confirm cancellation"}
        </button>
      </form>
      <p style="margin:14px 0 0;text-align:center;"><a href="/account" style="color:#807E79;font-size:13.5px;text-decoration:none;">Never mind, keep my subscription</a></p>
    </div>"""
    return render_template_string(_account_page(inner, "Cancel subscription"))


@app.route("/account/subscription/<int:gen_id>/cancel", methods=["GET", "POST"])
def account_cancel_subscription(gen_id):
    email = session.get("account_email")
    if not email:
        return redirect("/account/login")

    db = SessionLocal()
    try:
        gen = _account_owns_generation(db, email, gen_id)
        if not gen:
            return "Not found", 404

        if request.method == "GET":
            return _render_cancel_offer_page(gen)

        action = request.form.get("action", "")
        sub_id = _resolve_subscription_id(gen, db)
        if not sub_id:
            return _render_cancel_offer_page(
                gen, error="We couldn't find an active subscription for this site — please contact us directly."
            )

        if action == "accept_offer":
            if gen.retention_offer_used_at is not None:
                return _render_cancel_offer_page(gen, error="The free-month offer has already been used on this subscription.")
            try:
                coupon = _get_or_create_retention_coupon()
                stripe.Subscription.modify(sub_id, coupon=coupon.id)
            except stripe.error.StripeError as exc:
                app.logger.error(f"account_cancel_subscription: failed to apply retention coupon for gen {gen_id}: {exc}")
                return _render_cancel_offer_page(gen, error="Something went wrong applying the offer — please try again or contact us.")
            gen.retention_offer_used_at = datetime.utcnow()
            db.commit()
            inner = f"""<div class="acct-card">
              <h1 style="margin:0 0 8px;font-weight:800;font-size:26px;letter-spacing:-.02em;">You're all set</h1>
              <p style="margin:0;font-size:15px;color:#5C5A56;line-height:1.55;">Your next billing cycle is free — nothing else has changed. Thanks for staying with Groundwork.</p>
              <p style="margin:16px 0 0;"><a href="/account" class="acct-btn" style="display:inline-block;text-decoration:none;">Back to your account</a></p>
            </div>"""
            return render_template_string(_account_page(inner, "Offer applied"))

        elif action == "confirm_cancel":
            try:
                stripe.Subscription.modify(sub_id, cancel_at_period_end=True)
            except stripe.error.StripeError as exc:
                app.logger.error(f"account_cancel_subscription: failed to cancel subscription for gen {gen_id}: {exc}")
                return _render_cancel_offer_page(gen, error="Something went wrong cancelling — please try again or contact us.")
            gen.cancel_at_period_end = True
            db.commit()
            inner = """<div class="acct-card">
              <h1 style="margin:0 0 8px;font-weight:800;font-size:26px;letter-spacing:-.02em;">Subscription cancelled</h1>
              <p style="margin:0;font-size:15px;color:#5C5A56;line-height:1.55;">You'll keep full access through the end of your current billing period — no further charges after that.</p>
              <p style="margin:16px 0 0;"><a href="/account" class="acct-btn" style="display:inline-block;text-decoration:none;">Back to your account</a></p>
            </div>"""
            return render_template_string(_account_page(inner, "Subscription cancelled"))

        return _render_cancel_offer_page(gen, error="Unrecognized action.")
    finally:
        db.close()


def _password_field_html(field_id: str, name: str, placeholder: str) -> str:
    """A password <input> with a Show/Hide toggle button, shared across every
    password-entry form (login, set-password, reset-password)."""
    return f"""<div class="pw-field">
        <input id="{field_id}" type="password" name="{name}" placeholder="{placeholder}" required autofocus minlength="8">
        <button type="button" class="pw-toggle" onclick="gwTogglePw('{field_id}', this)">Show</button>
      </div>"""


def _render_password_form(email: str, stage: str, error: str = None, heading: str = None, body: str = None) -> str:
    error_html = f'<p class="err">{error}</p>' if error else ""
    heading = heading or ("Choose a password" if stage == "set_password" else "Enter your password")
    body = body or (
        "Set a password for this account so you can sign in instantly next time."
        if stage == "set_password" else
        "Welcome back — enter your password to continue."
    )
    forgot_link = (
        '<p style="margin:12px 0 0;text-align:right;"><a href="/account/forgot-password" style="color:#807E79;font-size:13px;text-decoration:none;">Forgot password?</a></p>'
        if stage == "password" else ""
    )
    inner = f"""<div class="acct-card">
      <h1 style="margin:0 0 8px;font-weight:800;font-size:26px;letter-spacing:-.02em;">{heading}</h1>
      <p style="margin:0;font-size:15px;color:#5C5A56;line-height:1.55;">{body}</p>
      {error_html}
      <form method="post" action="/account/login">
        <input type="hidden" name="stage" value="{stage}">
        <input type="hidden" name="email" value="{escape(email)}">
        <p style="margin:14px 0 0;font-size:13.5px;color:#807E79;">{escape(email)}</p>
        {_password_field_html('pw-input', 'password', 'At least 8 characters' if stage == 'set_password' else 'Password')}
        <button type="submit" class="acct-btn" style="width:100%;">{'Set password &amp; sign in' if stage == 'set_password' else 'Sign in'}</button>
      </form>
      {forgot_link}
    </div>"""
    return render_template_string(_account_page(inner, "Sign in"))


def _render_email_form(error: str = None) -> str:
    error_html = f'<p class="err">{error}</p>' if error else ""
    inner = f"""<div class="acct-card">
      <h1 style="margin:0 0 8px;font-weight:800;font-size:26px;letter-spacing:-.02em;">Sign in to your account</h1>
      <p style="margin:0;font-size:15px;color:#5C5A56;line-height:1.55;">Enter the email you used to build your site.</p>
      {error_html}
      <form method="post" action="/account/login">
        <input type="hidden" name="stage" value="email">
        <input type="email" name="email" placeholder="you@yourbusiness.co.uk" required autofocus>
        <button type="submit" class="acct-btn" style="width:100%;">Continue</button>
      </form>
    </div>"""
    return render_template_string(_account_page(inner, "Sign in"))


@app.route("/account/login", methods=["GET", "POST"])
def account_login():
    if session.get("account_email"):
        return redirect("/account")

    if request.method == "GET":
        return _render_email_form()

    stage = request.form.get("stage", "email")
    email = (request.form.get("email") or "").strip().lower()

    if stage == "password":
        # Step 2: password-login attempt for an account that already has one set.
        ip = request.headers.get("X-Forwarded-For", request.remote_addr or "unknown").split(",")[0].strip()
        # Two buckets: per-IP (catches one attacker spraying many emails) and
        # per-email (catches many IPs targeting one account) — either alone
        # misses the other pattern.
        if (_rate_limited("account_login_ip", ip, limit=15, window_seconds=900)
                or _rate_limited("account_login_email", email, limit=8, window_seconds=900)):
            return _render_password_form(email, "password", error="Too many attempts. Please try again in a few minutes.")
        password = request.form.get("password", "")
        db = SessionLocal()
        try:
            account = db.query(Account).filter(Account.email == email).first()
            if account and account.password_hash and check_password_hash(account.password_hash, password):
                session["account_email"] = email
                return redirect("/account")
            return _render_password_form(email, "password", error="Incorrect password.")
        finally:
            db.close()

    if stage == "set_password":
        # Step 2: choosing a password, either because this email already has a
        # generation (no re-verification needed) or because they just clicked
        # a signup verification link.
        password = request.form.get("password", "")
        if len(password) < 8:
            return _render_password_form(email, "set_password", error="Password must be at least 8 characters.")
        db = SessionLocal()
        try:
            account = db.query(Account).filter(Account.email == email).first()
            if account is None:
                account = Account(email=email)
                db.add(account)
            account.password_hash = generate_password_hash(password)

            # Section 9a/11: this is the "account actually created" moment
            # for any outreach prospect(s) tied to this email that were
            # generated via /claim/<token> without ever setting a password.
            # Reuses this existing flow rather than a new one, per the fix —
            # /claim/<token> itself no longer asks for a password at all.
            outreach_prospects = db.query(Prospect).filter(
                Prospect.email == email, Prospect.account_created_at.is_(None)
            ).all()
            for p in outreach_prospects:
                p.account_created_at = datetime.utcnow()
                if p.funnel_substage == "clicked_generated":
                    p.funnel_substage = "account_created"

            db.commit()
            session["account_email"] = email
            return redirect("/account")
        finally:
            db.close()

    # stage == "email" (step 1): decide which of the three flows applies.
    if not email:
        return _render_email_form(error="Enter a valid email address.")

    db = SessionLocal()
    try:
        account = db.query(Account).filter(Account.email == email).first()
        if account and account.password_hash:
            return _render_password_form(email, "password")

        has_in_progress_claim = db.query(Prospect).filter(
            Prospect.email == email, Prospect.lead_id.isnot(None)
        ).first() is not None

        if _has_generation(db, email) or has_in_progress_claim:
            # Real email — either they already verified it once to generate a
            # site (_has_generation), or they just claimed an outreach magic
            # link and generation is still mid-flight (150-300s) so no
            # Generation row exists yet (_run_and_persist only writes it once
            # done). Either way, ownership of this email is already
            # established via /claim/<token> or the original form
            # submission — no need to re-verify, just let them set a
            # password directly instead of sending a redundant confirmation
            # email while they're still waiting on their site.
            return _render_password_form(email, "set_password")

        # Brand new email with no generation on record — verify it first.
        token = serializer.dumps({"signup_email": email})
        verify_url = f"{request.host_url.rstrip('/')}/account/verify/{token}"
        send_resend_email(email, verify_url)
        inner = """<div class="acct-card" style="text-align:center;">
          <h1 style="margin:0 0 10px;font-weight:800;font-size:24px;letter-spacing:-.02em;">Check your email</h1>
          <p style="margin:0;font-size:15px;color:#5C5A56;line-height:1.6;">Click the link we've sent to confirm your address and set a password. It expires in 24 hours.</p>
        </div>"""
        return render_template_string(_account_page(inner, "Check your email"))
    finally:
        db.close()


@app.route("/account/verify/<token>")
def account_verify(token):
    try:
        data = serializer.loads(token, max_age=TOKEN_MAX_AGE)
    except SignatureExpired:
        return redirect("/verify-error.html?reason=expired")
    except BadSignature:
        return redirect("/verify-error.html?reason=invalid")

    email = data.get("signup_email")
    if not email:
        return redirect("/verify-error.html?reason=invalid")

    return _render_password_form(
        email, "set_password",
        heading="Confirm your email — choose a password",
        body="Your address is confirmed. Set a password to finish creating your account.",
    )


@app.route("/account/forgot-password", methods=["GET", "POST"])
def account_forgot_password():
    if request.method == "GET":
        inner = """<div class="acct-card">
          <h1 style="margin:0 0 8px;font-weight:800;font-size:26px;letter-spacing:-.02em;">Reset your password</h1>
          <p style="margin:0;font-size:15px;color:#5C5A56;line-height:1.55;">Enter your account email and we'll send you a link to choose a new password.</p>
          <form method="post">
            <input type="email" name="email" placeholder="you@yourbusiness.co.uk" required autofocus>
            <button type="submit" class="acct-btn" style="width:100%;">Send reset link</button>
          </form>
        </div>"""
        return render_template_string(_account_page(inner, "Reset your password"))

    email = (request.form.get("email") or "").strip().lower()
    if email:
        db = SessionLocal()
        try:
            account = db.query(Account).filter(Account.email == email).first()
            if account and account.password_hash:
                token = serializer.dumps({"reset_email": email})
                reset_url = f"{request.host_url.rstrip('/')}/account/reset-password/{token}"
                send_password_reset_email(email, reset_url)
        finally:
            db.close()
    # Always show the same confirmation, regardless of whether the email has
    # a password-protected account — avoids leaking which addresses do.
    inner = """<div class="acct-card" style="text-align:center;">
      <h1 style="margin:0 0 10px;font-weight:800;font-size:24px;letter-spacing:-.02em;">Check your email</h1>
      <p style="margin:0;font-size:15px;color:#5C5A56;line-height:1.6;">If that address has a Groundwork account, we've sent a link to reset your password. It expires in 1 hour.</p>
    </div>"""
    return render_template_string(_account_page(inner, "Check your email"))


@app.route("/account/reset-password/<token>", methods=["GET", "POST"])
def account_reset_password(token):
    try:
        data = serializer.loads(token, max_age=RESET_TOKEN_MAX_AGE)
    except SignatureExpired:
        return redirect("/verify-error.html?reason=expired")
    except BadSignature:
        return redirect("/verify-error.html?reason=invalid")

    email = data.get("reset_email")
    if not email:
        return redirect("/verify-error.html?reason=invalid")

    def render_form(error=None):
        error_html = f'<p class="err">{error}</p>' if error else ""
        inner = f"""<div class="acct-card">
          <h1 style="margin:0 0 8px;font-weight:800;font-size:26px;letter-spacing:-.02em;">Choose a new password</h1>
          <p style="margin:0;font-size:15px;color:#5C5A56;line-height:1.55;">Setting a new password for {escape(email)}.</p>
          {error_html}
          <form method="post">
            {_password_field_html('pw-input', 'password', 'At least 8 characters')}
            <button type="submit" class="acct-btn" style="width:100%;">Set new password &amp; sign in</button>
          </form>
        </div>"""
        return render_template_string(_account_page(inner, "Reset your password"))

    if request.method == "GET":
        return render_form()

    password = request.form.get("password", "")
    if len(password) < 8:
        return render_form(error="Password must be at least 8 characters.")

    db = SessionLocal()
    try:
        # Token itself proves this email is the requester — re-validate the
        # account exists (it must, to have requested a reset) rather than
        # trusting any client-submitted email field.
        account = db.query(Account).filter(Account.email == email).first()
        if account is None:
            return redirect("/verify-error.html?reason=invalid")
        account.password_hash = generate_password_hash(password)
        db.commit()
        session["account_email"] = email
        return redirect("/account")
    finally:
        db.close()


@app.route("/account")
def account_home():
    email = session.get("account_email")
    if not email:
        return redirect("/account/login")
    return _render_dashboard(email)


@app.route("/account/logout")
def account_logout():
    session.pop("account_email", None)
    return redirect("/account/login")


def _prospect_to_form_data(p):
    """
    Maps whatever's on a Prospect row (Places API + scoring data — no form
    was ever filled in) onto build_prompt.py's expected keys. build_prompt
    reads form_data with .get() and skips any falsy fact, so keys with no
    prospect-side source (work_split, craft_prestige, team_size, urgency,
    years_trading, claimed_accreditations, claimed_projects, other_notes)
    are simply omitted rather than faked — the generated site will be
    thinner on specifics than one from the real form, which is inherent to
    cold outreach (see docs/outreach-pipeline-spec.md Section 9a).

    Section 7's "pulled logo/colours/copy from their existing site" step for
    has_website_dated/has_website_modern prospects: logo_uploaded/
    portfolio_uploaded below always start False/False, since form_data is
    built before extraction runs. _try_extract_prospect_assets() (called
    from _claim_generate_and_redirect, after this function) overwrites both
    flags on lead.form_data if it manages to pull a usable logo/photos from
    prospect.website — see that function's docstring. build_prompt's own
    Step 1 web-search verification still runs as normal regardless.
    """
    return {
        "business_name": p.business_name or "",
        "trade": p.trade or "",
        "location": p.location or "",
        "coverage_area": p.location or "",
        "phone": p.phone or "",
        "email": p.email or "",
        "logo_uploaded": False,
        "portfolio_uploaded": False,
        # Real Google Places data (Enterprise + Atmosphere tier, added
        # 2026-07-23) — see build_prompt.py for how each is used. Aggregate
        # rating/count are plain facts-list entries; reviews/opening_hours
        # are structured and get their own prompt sections.
        "google_rating": p.rating,
        "google_review_count": p.review_count,
        "google_primary_type": p.primary_type,
        "google_editorial_summary": p.editorial_summary,
        "google_earliest_review_date": p.earliest_review_date.strftime("%Y") if p.earliest_review_date else None,
        "google_reviews": p.reviews,
        "google_opening_hours": p.opening_hours,
    }


# website_status values that mean "this prospect has an existing site to pull
# from." has_website_dated/has_website_modern are the legacy values from the
# old Cowork vision-judgment step; has_website is what the current pipeline
# writes going forward. All three carry the same meaning for this purpose.
_HAS_EXISTING_WEBSITE_STATUSES = {"has_website_dated", "has_website_modern", "has_website"}


def _try_extract_prospect_assets(prospect, lead, job_dir):
    """
    Best-effort logo/portfolio-photo extraction from a has_website_dated /
    has_website_modern prospect's existing site, run at click-time (inside
    _claim_generate_and_redirect, before _kickoff_generation) so it only
    ever runs for prospects who actually click through.

    Saves files into job_dir using the exact same on-disk convention as
    manually-uploaded logos/photos (logo.<ext>, photo_0.<ext>, ...), so
    everything downstream (_build_media_placeholders, _process_logo, the
    Claude vision-input read in _kickoff_generation) works unchanged.

    Never raises and never partially corrupts lead state: any failure
    anywhere in extract_site_assets() results in no files being written and
    lead.logo_path staying None, which is exactly the current default
    behaviour (generated look, no photos) — a bad extraction can't produce
    a worse site than not extracting at all.

    Also sets prospect.extraction_quality ("full"/"partial"/"none") so the
    outcome is queryable later without re-running extraction — see
    docs on the Prospect model and the Funnel dashboard's breakdown panel.
    extraction_quality is only set when extraction actually runs (website
    present + matching status); it stays NULL otherwise, distinct from
    "none" (ran, found nothing usable).
    """
    if not prospect.website or prospect.website_status not in _HAS_EXISTING_WEBSITE_STATUSES:
        return

    try:
        assets = extract_site_assets(prospect.website)
    except Exception:
        logging.exception("Prospect site asset extraction failed for %s", prospect.website)
        prospect.extraction_quality = "none"
        return

    logo = assets.get("logo")
    photos = assets.get("photos") or []
    logo_persisted = False
    photos_persisted = False

    if logo or photos:
        try:
            os.makedirs(job_dir, exist_ok=True)

            if logo:
                fname = f"logo{logo.ext}"
                with open(os.path.join(job_dir, fname), "wb") as f:
                    f.write(logo.bytes)
                lead.logo_path = fname
                lead.logo_mime = logo.mime
                logo_persisted = True

            for i, photo in enumerate(photos):
                fname = f"photo_{i}{photo.ext}"
                with open(os.path.join(job_dir, fname), "wb") as f:
                    f.write(photo.bytes)
            photos_persisted = bool(photos)

            form_data = dict(lead.form_data or {})
            form_data["logo_uploaded"] = logo_persisted
            form_data["portfolio_uploaded"] = photos_persisted
            lead.form_data = form_data
        except Exception:
            logging.exception("Failed persisting extracted assets for prospect %s", prospect.id)
            # Best-effort cleanup so a half-written job_dir doesn't leave a
            # logo_path pointing at a partial/corrupt file.
            lead.logo_path = None
            lead.logo_mime = None
            logo_persisted = False
            photos_persisted = False

    if logo_persisted and photos_persisted:
        prospect.extraction_quality = "full"
    elif logo_persisted or photos_persisted:
        prospect.extraction_quality = "partial"
    else:
        prospect.extraction_quality = "none"


def _claim_email_form(prospect, error=None):
    error_html = f'<p class="err">{error}</p>' if error else ""
    name_bit = f" for {escape(prospect.business_name)}" if prospect.business_name else ""
    inner = f"""<div class="acct-card">
      <h1 style="margin:0 0 8px;font-weight:800;font-size:26px;letter-spacing:-.02em;">See your free website preview</h1>
      <p style="margin:0;font-size:15px;color:#5C5A56;line-height:1.55;">Enter your email and we'll build a website preview{name_bit} — no cost, no account needed to look.</p>
      {error_html}
      <form method="post" action="/claim/{escape(prospect.token)}/email">
        <input type="email" name="email" placeholder="you@yourbusiness.co.uk" required autofocus>
        <button type="submit" class="acct-btn" style="width:100%;">See my website preview</button>
      </form>
    </div>"""
    return render_template_string(_account_page(inner, "See your website"))


def _stamp_latest_touch_outcome(db, prospect_id, field, channel=None):
    """Attribute an opened/clicked/paid event to the specific OutreachTouch
    row it most likely belongs to — a last-touch attribution model (added
    2026-07-21 alongside the email-variant testing system, Section 19).

    Prospect.opened_at/clicked_at/paid_at are "ever happened, once" flags
    across the prospect's whole lifetime; they can't say WHICH of several
    sent variants a prospect actually acted on. This finds the most recent
    OutreachTouch for this prospect (optionally restricted to one channel —
    "opened" only makes sense for channel='email', since SMS has no open
    tracking) at or before now, and stamps `field` on it if not already
    set — same "guarded by is-None" idiom as the Prospect-level fields this
    mirrors. Honest limitation: this is last-touch attribution, not
    true causal attribution — if a prospect received touch N and later acts
    after also having seen touch N+1, the action is credited to N+1 even if
    N was the one that actually drove it. Standard tradeoff for this kind
    of tracking; no per-click-through-a-specific-email signal exists to do
    better without adding per-touch tracking links, which isn't built."""
    q = db.query(OutreachTouch).filter(OutreachTouch.prospect_id == prospect_id)
    if channel:
        q = q.filter(OutreachTouch.channel == channel)
    touch = q.order_by(OutreachTouch.sent_at.desc()).first()
    if touch and getattr(touch, field) is None:
        setattr(touch, field, datetime.utcnow())


def _prospect_last_touch_channel(db, prospect_id):
    touch = db.query(OutreachTouch).filter(OutreachTouch.prospect_id == prospect_id).order_by(
        OutreachTouch.sent_at.desc()
    ).first()
    return touch.channel if touch else None


def _log_prospect_event(db, prospect_id, event_type, channel=None, meta=None):
    """Records one granular funnel micro-event (models.ProspectEvent — see
    its docstring for the full event_type list and design rationale).
    Unlike _stamp_latest_touch_outcome, this is NOT guarded by "only once"
    — every call adds a new row, since repeat events (e.g. a second click
    on the same magic link weeks later) are themselves real signal, not
    noise to dedupe away. Doesn't commit — same convention as
    _stamp_latest_touch_outcome, the caller's existing commit covers it."""
    db.add(ProspectEvent(prospect_id=prospect_id, event_type=event_type, channel=channel, meta=meta))


def _needs_pregen_survey_gate(db, prospect):
    """True if this prospect should see the pre-generation survey
    (/claim/<token>/survey) instead of going straight into generation —
    added 2026-07-27, by request. Scoped to exactly one cohort: an
    existing website AND sourced via Google Places (see
    outreach/sourcing_channels.py) — the segment with the lowest observed
    click-to-generate conversion, where a real structured "why" from every
    clicker is worth more than an instant-gen click that mostly doesn't
    convert anyway. Only ever consulted for a prospect that has no lead_id
    yet (see _claim_generate_and_redirect) — a prospect already past their
    first claim never gets gated retroactively."""
    if prospect.website_status != "has_website" or prospect.sourcing_channel != "google_places":
        return False
    already_answered = db.query(PreGenSurveyResponse).filter(
        PreGenSurveyResponse.prospect_id == prospect.id
    ).first()
    return already_answered is None


def _claim_generate_and_redirect(db, prospect):
    """
    Shared by /claim/<token> and /claim/<token>/email — the actual
    "generate on click, not upfront" moment. No password/account barrier:
    per the current design, a first-time claim goes straight into
    generation (or, for the gated cohort below, into the pre-gen survey
    first), and funnel_substage moves to clicked_generated in
    _finish_claim (not account_created — that only happens later, when a
    password is actually set, via the existing /account/login flow).
    """
    if prospect.clicked_at is None:
        prospect.clicked_at = datetime.utcnow()
        _stamp_latest_touch_outcome(db, prospect.id, "clicked_at")
        # Per-click admin notification removed 2026-07-23, by request — at
        # real volume this was firing dozens of times a day with no signal
        # value per individual click; clicks are now rolled into
        # outreach/send_daily_summary.py's once-a-day totals instead (see
        # that module). Payment still gets its own real-time notification
        # (send_admin_payment_received_email, below) — that one's rare and
        # worth an instant ping.

    # Gate only ever applies pre-first-claim (no lead_id yet) — a repeat
    # visitor who already has a lead/generation in flight always falls
    # through to _finish_claim unchanged, regardless of website_status/
    # sourcing_channel, since they already passed (or were never subject
    # to) this check on their real first click.
    if prospect.lead_id is None and _needs_pregen_survey_gate(db, prospect):
        db.commit()
        return redirect(f"/claim/{prospect.token}/survey")

    return redirect(_finish_claim(db, prospect))


def _finish_claim(db, prospect):
    """The actual Lead-creation/generation-kickoff logic, factored out of
    _claim_generate_and_redirect (2026-07-27) so the pre-gen survey's POST
    handler can call this directly after saving a response, without going
    back through the gate check a second time. Returns the target URL as a
    plain string (not a Response) so both callers — one returning a real
    redirect, the survey's fetch()-based submit returning JSON — can use
    it as-is. Idempotent: a repeat visit for a prospect that's already
    generated just points at the result instead of creating a second
    Lead/generation.
    """
    if prospect.lead_id:
        lead = db.get(Lead, prospect.lead_id)
        existing_gen = db.query(Generation).filter(Generation.lead_id == lead.id).first()
        db.commit()
        if existing_gen:
            return f"/api/generate/{lead.public_id}/html"
        # A lead can exist with no Generation yet for two reasons: generation
        # is genuinely still running in its background thread, or a prior
        # attempt crashed before ever producing one (e.g. the build_prompt
        # NameError this exact bug class caused — lead_id gets set, then the
        # crash happens, leaving this prospect permanently stuck on repeat
        # clicks since nothing before this re-kicked generation off). Only
        # re-kick if there's no in-flight job for this id, so a genuinely
        # still-running generation isn't started a second time.
        with _jobs_lock:
            already_running = lead.public_id in _jobs
        if not already_running:
            # job_dir's on-disk logo/photos are transient staging (wiped by
            # any redeploy — see CLAUDE.md's "residual caveat" on Image
            # persistence) and may no longer be there by the time a stuck
            # lead gets re-kicked, potentially long after the original
            # click. Re-run extraction first so a redeploy in between
            # doesn't silently produce a site with broken image tokens.
            job_dir = os.path.join(UPLOAD_DIR, lead.public_id)
            _try_extract_prospect_assets(prospect, lead, job_dir)
            db.commit()
            _kickoff_generation(lead)
        return f"/loading.html?id={lead.public_id}"

    # First-time claim for this prospect, but the email itself may already
    # have a generation from an unrelated path (e.g. they used the direct
    # signup form before ever getting/clicking this outreach link) —
    # _has_generation is the same one-generation-per-email guard /api/generate
    # enforces; without this check a claim would silently kick off a second,
    # redundant (paid) Claude generation instead of just showing the one
    # that already exists.
    if prospect.email:
        existing_gen = (
            db.query(Generation).filter(Generation.email == prospect.email)
            .order_by(Generation.created_at.desc()).first()
        )
        if existing_gen:
            prospect.lead_id = existing_gen.lead_id
            db.commit()
            existing_lead = db.get(Lead, existing_gen.lead_id)
            return f"/api/generate/{existing_lead.public_id}/html"

    lead = Lead(
        public_id=uuid.uuid4().hex[:10],
        email=prospect.email or "",
        ip=_client_ip(),
        status="verified",
        form_data=_prospect_to_form_data(prospect),
    )
    db.add(lead)
    db.flush()
    prospect.lead_id = lead.id
    prospect.funnel_substage = "clicked_generated"

    job_dir = os.path.join(UPLOAD_DIR, lead.public_id)
    _try_extract_prospect_assets(prospect, lead, job_dir)

    _log_prospect_event(db, prospect.id, "generation_kicked_off", channel=_prospect_last_touch_channel(db, prospect.id))
    db.commit()

    _kickoff_generation(lead)

    # Admin notification — re-added 2026-07-27, by request (originally
    # removed 2026-07-23 for being pure noise at real volume; brought back
    # because a real GENERATION now always means one of two things worth
    # knowing about: a prospect clicked straight through (the majority,
    # unchanged), or the has_website/google_places cohort answered the
    # pre-gen survey first (PreGenSurveyResponse) — either way, this is the
    # actual "a generation just fired" moment, not raw click time, which is
    # why this lives here in _finish_claim rather than back in
    # _claim_generate_and_redirect's clicked_at guard: for the gated
    # cohort, a click alone doesn't yet mean a generation is happening —
    # only a submitted survey does. Backgrounded so a slow Resend call
    # never adds latency to this redirect. Fires once per prospect, since
    # this whole branch only runs once per prospect (the first-time-claim
    # Lead-creation branch, guarded above by `if prospect.lead_id:`).
    pregen_survey_gated = db.query(PreGenSurveyResponse).filter(
        PreGenSurveyResponse.prospect_id == prospect.id
    ).first() is not None
    threading.Thread(
        target=send_admin_magic_link_clicked_email,
        args=(prospect.business_name, prospect.id, bool(prospect.email), pregen_survey_gated),
        daemon=True,
    ).start()

    return f"/loading.html?id={lead.public_id}"


# Pre-generation survey question set (2026-07-27) — see
# PreGenSurveyResponse's docstring in models.py for the full rationale.
# Keyed by `key` (stable identifier stored in PreGenSurveyResponse.answers,
# independent of question wording so copy can be tweaked without breaking
# old rows) — multi-select, every question also allows a free-text "Other"
# on top of its listed options. Shared between the GET (renders these into
# the page) and POST (validates submitted answers against this exact set)
# handlers below so they can never drift apart.
_PREGEN_SURVEY_QUESTIONS = [
    {
        "key": "no_update_reason",
        "q": "What's the main reason you haven't updated your website recently?",
        "opts": ["Happy with what I have", "Too busy / not a priority", "Cost of a new site", "Don't know where to start", "Someone else handles it, not me"],
    },
    {
        "key": "who_manages",
        "q": "Who looks after your current website?",
        "opts": ["I do it myself", "A web designer / agency", "Family member or employee", "Nobody really — it's just there"],
    },
    {
        "key": "monthly_cost",
        "q": "Roughly what do you pay for your website each month?",
        "opts": ["Nothing / one-off only", "Under £15", "£15–£40", "£40+", "Not sure"],
    },
    {
        "key": "switch_trigger",
        "q": "What would actually make you consider switching?",
        "opts": ["A lower price", "It looking more modern/professional", "Better Google ranking", "Nothing — I'm happy as is"],
    },
    {
        "key": "satisfaction",
        "q": "How happy are you with your current website?",
        "opts": ["Very happy", "It's fine, not great", "Not happy but haven't dealt with it", "Actively looking to replace it"],
    },
]


def _render_pregen_survey_page(token):
    """The pre-gen survey itself — deliberately styled to match
    frontend/loading.html exactly (flat, borderless, dark, same tokens),
    since it visually leads straight into that page once submitted, and
    deliberately carries NO framing/instructional copy (no "before we
    build your site", no explanation of why it's here) — by request, just
    the question. Client-side only knows the question text/options/keys
    (embedded as JSON below); all validation happens again server-side in
    claim_pregen_survey's POST branch regardless of what this renders."""
    questions_json = json.dumps([{"key": q["key"], "q": q["q"], "opts": q["opts"]} for q in _PREGEN_SURVEY_QUESTIONS])
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Groundwork</title>
<link rel="icon" type="image/x-icon" href="/favicon.ico">
<link rel="icon" type="image/png" sizes="192x192" href="/favicon-192.png">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=Plus+Jakarta+Sans:wght@700;800&display=swap" rel="stylesheet">
<style>
*{{box-sizing:border-box;}}
html,body{{margin:0;height:100%;background:#1C1C1C;}}
h1,h2,h3,h4{{font-family:'Plus Jakarta Sans','Inter',sans-serif;}}
body{{font-family:Inter,sans-serif;background:#1C1C1C;min-height:100vh;display:flex;flex-direction:column;align-items:center;justify-content:center;padding:32px 24px;position:relative;color:#FAFAF8;}}
@keyframes gw-fade{{from{{opacity:0;transform:translateY(4px);}}to{{opacity:1;transform:translateY(0);}}}}
@keyframes gw-pop{{0%{{transform:scale(1);}}40%{{transform:scale(.97);}}100%{{transform:scale(1);}}}}
@media (prefers-reduced-motion:reduce){{*{{animation-duration:.001ms !important;animation-iteration-count:1 !important;transition-duration:.001ms !important;}}}}
.stage{{width:100%;max-width:480px;}}
.survey-top{{display:flex;align-items:center;justify-content:center;position:relative;margin-bottom:26px;}}
.survey-back{{position:absolute;left:0;width:28px;height:28px;border:0;background:transparent;color:#4A4A48;cursor:pointer;display:flex;align-items:center;justify-content:center;font-size:16px;visibility:hidden;}}
.survey-back.show{{visibility:visible;}}
.survey-back:hover{{color:#9A9893;}}
.dots{{display:flex;gap:6px;}}
.dot{{width:6px;height:6px;border-radius:999px;background:#33332F;transition:background .25s,width .25s;}}
.dot.done{{background:#3B82F6;}}
.dot.active{{background:#3B82F6;width:16px;}}
.q-title{{font-weight:400;font-size:clamp(20px,4vw,26px);line-height:1.4;color:#FAFAF8;margin:0 0 26px;letter-spacing:-.01em;text-align:center;}}
.opts{{display:flex;flex-direction:column;gap:9px;}}
.opt{{text-align:left;width:100%;background:#2C2C2C;border:0;color:#FAFAF8;border-radius:11px;padding:14px 17px;font-size:14.5px;font-family:inherit;cursor:pointer;transition:background .15s,transform .1s;display:flex;align-items:center;justify-content:space-between;gap:10px;}}
.opt:hover{{background:#3B82F61F;}}
.opt:focus-visible{{outline:2px solid #3B82F6;outline-offset:2px;}}
.opt.picked{{background:#3B82F61F;animation:gw-pop .28s ease;}}
.opt .tick{{width:17px;height:17px;border-radius:5px;border:1.5px solid #4A4A48;flex-shrink:0;position:relative;}}
.opt.picked .tick{{border-color:#3B82F6;background:#3B82F6;}}
.opt.picked .tick::after{{content:"";position:absolute;left:4.5px;top:1.5px;width:4px;height:8px;border-right:2px solid #1C1C1C;border-bottom:2px solid #1C1C1C;transform:rotate(40deg);}}
.other-wrap{{margin-top:9px;display:none;}}
.other-wrap.show{{display:block;animation:gw-fade .2s ease both;}}
.other-input{{width:100%;background:#2C2C2C;border:0;color:#FAFAF8;border-radius:10px;padding:12px 14px;font-size:14px;font-family:inherit;}}
.other-input::placeholder{{color:#4A4A48;}}
.other-input:focus{{outline:2px solid #3B82F6;outline-offset:1px;}}
.continue-row{{margin-top:18px;}}
.continue-btn{{width:100%;background:#3B82F6;color:#fff;border:0;border-radius:11px;padding:14px 16px;font-weight:700;font-size:14.5px;cursor:pointer;font-family:inherit;transition:background .15s,opacity .15s;}}
.continue-btn:hover:not(:disabled){{background:#2563EB;}}
.continue-btn:disabled{{opacity:.35;cursor:default;}}
.err-msg{{margin-top:14px;font-size:13px;color:#F87171;text-align:center;display:none;}}
.err-msg.show{{display:block;}}
.footer-brand{{position:absolute;bottom:26px;left:0;right:0;text-align:center;}}
.footer-brand span{{font-size:12.5px;color:#4A4A48;letter-spacing:.02em;}}
</style>
</head>
<body>
<div class="stage">
  <div id="survey">
    <div class="survey-top">
      <button class="survey-back" id="back-btn" type="button" aria-label="Previous question">←</button>
      <div class="dots" id="dots"></div>
    </div>
    <h2 class="q-title" id="q-title"></h2>
    <div class="opts" id="opts"></div>
    <div class="other-wrap" id="other-wrap">
      <input class="other-input" id="other-input" type="text" placeholder="Type your answer…" maxlength="140">
    </div>
    <div class="continue-row">
      <button class="continue-btn" id="continue-btn" type="button" disabled>Continue</button>
    </div>
    <p class="err-msg" id="err-msg"></p>
  </div>
</div>
<div class="footer-brand"><span>Powered by Groundwork</span></div>
<script>
const TOKEN = {json.dumps(token)};
const QUESTIONS = {questions_json};
let step = 0;
const answers = QUESTIONS.map(() => ({{picked: new Set(), other: ''}}));

const dotsEl = document.getElementById('dots');
const titleEl = document.getElementById('q-title');
const optsEl = document.getElementById('opts');
const otherWrap = document.getElementById('other-wrap');
const otherInput = document.getElementById('other-input');
const continueBtn = document.getElementById('continue-btn');
const backBtn = document.getElementById('back-btn');
const errEl = document.getElementById('err-msg');

function renderDots(){{
  dotsEl.innerHTML = '';
  QUESTIONS.forEach((_, i) => {{
    const d = document.createElement('div');
    d.className = 'dot' + (i < step ? ' done' : i === step ? ' active' : '');
    dotsEl.appendChild(d);
  }});
}}

function updateContinueState(){{
  const a = answers[step];
  const hasOther = a.picked.has('Other') && a.other.trim().length > 0;
  const hasNonOther = [...a.picked].some(v => v !== 'Other');
  continueBtn.disabled = !(hasNonOther || hasOther);
}}

function renderQuestion(){{
  const item = QUESTIONS[step];
  const a = answers[step];
  titleEl.textContent = item.q;
  optsEl.innerHTML = '';
  otherInput.value = a.other;
  otherWrap.classList.toggle('show', a.picked.has('Other'));
  backBtn.classList.toggle('show', step > 0);
  errEl.classList.remove('show');
  renderDots();

  [...item.opts, 'Other'].forEach(label => {{
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'opt' + (a.picked.has(label) ? ' picked' : '');
    b.innerHTML = `<span>${{label}}</span><span class="tick"></span>`;
    b.addEventListener('click', () => toggle(label, b));
    optsEl.appendChild(b);
  }});

  updateContinueState();
}}

function toggle(label, btnEl){{
  const a = answers[step];
  if (a.picked.has(label)) {{
    a.picked.delete(label);
    btnEl.classList.remove('picked');
  }} else {{
    a.picked.add(label);
    btnEl.classList.add('picked');
  }}
  if (label === 'Other') {{
    otherWrap.classList.toggle('show', a.picked.has('Other'));
    if (a.picked.has('Other')) otherInput.focus();
  }}
  updateContinueState();
}}

otherInput.addEventListener('input', () => {{
  answers[step].other = otherInput.value;
  updateContinueState();
}});

backBtn.addEventListener('click', () => {{
  if (step === 0) return;
  step -= 1;
  renderQuestion();
}});

continueBtn.addEventListener('click', () => {{
  if (step < QUESTIONS.length - 1) {{
    step += 1;
    renderQuestion();
  }} else {{
    submitSurvey();
  }}
}});

async function submitSurvey(){{
  continueBtn.disabled = true;
  continueBtn.textContent = 'Building your website…';
  errEl.classList.remove('show');

  const payload = {{}};
  QUESTIONS.forEach((q, i) => {{
    const a = answers[i];
    payload[q.key] = {{
      picked: [...a.picked],
      other: a.picked.has('Other') ? a.other.trim() : null,
    }};
  }});

  try {{
    const res = await fetch(`/claim/${{TOKEN}}/survey`, {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{answers: payload}}),
    }});
    const data = await res.json();
    if (res.ok && data.redirect) {{
      window.location.href = data.redirect;
    }} else {{
      throw new Error(data.error || 'Something went wrong.');
    }}
  }} catch (e) {{
    errEl.textContent = 'Something went wrong — please try again.';
    errEl.classList.add('show');
    continueBtn.disabled = false;
    continueBtn.textContent = 'Continue';
  }}
}}

renderQuestion();
</script>
</body>
</html>"""


@app.route("/claim/<token>/survey", methods=["GET", "POST"])
def claim_pregen_survey(token):
    """Pre-generation survey gate (added 2026-07-27, by request) — the
    ONLY entry point into this page is _claim_generate_and_redirect
    redirecting here for the gated cohort (see _needs_pregen_survey_gate);
    never linked directly from an outreach send. GET renders the
    interactive survey; POST validates + saves the submission
    (PreGenSurveyResponse) then calls _finish_claim directly — same
    Lead-creation/generation-kickoff logic a normal claim uses, just
    reached one step later — and hands the resulting URL back as JSON
    for the page's fetch()-based submit to navigate to (a plain redirect
    Response doesn't fit that flow the way it does for a normal form
    post/GET navigation)."""
    db = SessionLocal()
    try:
        prospect = db.query(Prospect).filter(Prospect.token == token).first()
        if not prospect:
            return redirect("/verify-error.html?reason=invalid")

        if not _needs_pregen_survey_gate(db, prospect):
            # Already answered, or doesn't actually qualify (e.g. the link
            # was hit directly rather than via the real gate) — fall
            # through to the normal claim flow instead of re-asking or
            # asking a prospect who was never supposed to see this.
            return redirect(_finish_claim(db, prospect))

        if request.method == "GET":
            _log_prospect_event(db, prospect.id, "pregen_survey_viewed", channel=_prospect_last_touch_channel(db, prospect.id))
            db.commit()
            return _render_pregen_survey_page(token)

        payload = request.get_json(silent=True) or {}
        raw_answers = payload.get("answers")
        if not isinstance(raw_answers, dict):
            return jsonify({"error": "Malformed submission"}), 400

        cleaned = {}
        for q in _PREGEN_SURVEY_QUESTIONS:
            entry = raw_answers.get(q["key"]) or {}
            valid_labels = set(q["opts"]) | {"Other"}
            picked = [p for p in (entry.get("picked") or []) if isinstance(p, str) and p in valid_labels]
            other = None
            if "Other" in picked:
                other = (entry.get("other") or "").strip()[:300] or None
                if not other:
                    picked = [p for p in picked if p != "Other"]
            cleaned[q["key"]] = {"picked": picked, "other": other}

        # Require at least one real answer somewhere in the survey — an
        # empty/malformed submission (bot, direct POST) shouldn't count as
        # "answered" and silently exempt this prospect from the gate going
        # forward.
        if not any(v["picked"] for v in cleaned.values()):
            return jsonify({"error": "Please answer at least one question."}), 400

        db.add(PreGenSurveyResponse(prospect_id=prospect.id, answers=cleaned))
        _log_prospect_event(
            db, prospect.id, "pregen_survey_submitted",
            channel=_prospect_last_touch_channel(db, prospect.id),
        )
        db.commit()

        target = _finish_claim(db, prospect)
        return jsonify({"redirect": target})
    finally:
        db.close()


@app.route("/claim/<token>")
def claim(token):
    """
    Magic-link entry point for outreach prospects (docs/outreach-pipeline-spec.md
    Section 9/9a). Never expires, works unlimited times, per Section 9.

    No password/account barrier before seeing anything — first visit kicks
    off real generation immediately and takes them straight to
    /loading.html. Password is only ever requested later, at the point the
    prospect would normally need to authenticate on the existing site (e.g.
    /account/login, or an action gated by account_required) — reusing that
    existing flow as-is (see account_login's "no password yet, but this
    email has a Generation" branch), not a new mechanism here.
    """
    db = SessionLocal()
    try:
        prospect = db.query(Prospect).filter(Prospect.token == token).first()
        if not prospect:
            return redirect("/verify-error.html?reason=invalid")

        _log_prospect_event(db, prospect.id, "magic_link_clicked", channel=_prospect_last_touch_channel(db, prospect.id))
        db.commit()

        if not prospect.email:
            # Phone-only prospect — no email on file to create a Lead/Account
            # against. Route to the email-capture page instead (Section 9a).
            return redirect(f"/claim/{token}/email")

        return _claim_generate_and_redirect(db, prospect)
    finally:
        db.close()


def _claim_check_email_page(token, email):
    inner = f"""<div class="acct-card" style="text-align:center;">
      <h1 style="margin:0 0 10px;font-weight:800;font-size:24px;letter-spacing:-.02em;">Check your inbox</h1>
      <p style="margin:0;font-size:15px;color:#5C5A56;line-height:1.6;">
        We've sent a confirmation link to <strong>{escape(email)}</strong>. Click it to verify this address
        and we'll start building your website preview — this link expires in 24 hours.
      </p>
      <p style="margin:16px 0 0;font-size:13px;color:#9A9893;">Didn't get it? Check spam, or
        <a href="/claim/{escape(token)}/email">try again with a different address</a>.</p>
    </div>"""
    return render_template_string(_account_page(inner, "Check your inbox"))


@app.route("/claim/<token>/email", methods=["GET", "POST"])
def claim_email(token):
    """
    Email-capture page for phone-only prospects (has_findable_email=false,
    i.e. email_found=false — SMS-only outreach, docs/outreach-pipeline-spec.md
    Section 9a) and, since 2026-07-25, Facebook DM prospects using the same
    no-email-on-file path. /claim/<token> redirects here automatically when
    the prospect has no email on file, so /s/<short_code> needs no changes
    to reach this page.

    Real verification (added 2026-07-25, by request): submitting this form
    does NOT trust the typed address immediately — it emails a confirmation
    link (same mechanism/copy as the direct-signup flow's send_verification_
    email, reusing TOKEN_MAX_AGE/serializer) and shows a "check your inbox"
    page. Only /claim/<token>/verify/<vtoken> below actually records the
    email on Prospect/Account and fires generation, once the link is
    clicked — proving they own the address before it's trusted (added to
    the prospect's profile, used for follow-ups, etc.), the same bar the
    direct-signup form already holds itself to.
    """
    db = SessionLocal()
    try:
        prospect = db.query(Prospect).filter(Prospect.token == token).first()
        if not prospect:
            return redirect("/verify-error.html?reason=invalid")

        if prospect.email:
            # Already has an email on file (e.g. discovered later, or
            # already verified) — nothing left to capture here.
            return redirect(f"/claim/{token}")

        if request.method == "GET":
            _log_prospect_event(db, prospect.id, "email_capture_viewed", channel=_prospect_last_touch_channel(db, prospect.id))
            db.commit()
            return _claim_email_form(prospect)

        email = (request.form.get("email") or "").strip().lower()
        if not email or not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', email):
            return _claim_email_form(prospect, error="Enter a valid email address.")

        channel = _prospect_last_touch_channel(db, prospect.id)
        _log_prospect_event(db, prospect.id, "email_capture_submitted", channel=channel, meta={"email": email})

        vtoken = serializer.dumps({"prospect_token": token, "email": email})
        base_url = request.host_url.rstrip("/")
        verify_url = f"{base_url}/claim/{token}/verify/{vtoken}"
        send_verification_email(email, verify_url, prospect.business_name or "")
        _log_prospect_event(db, prospect.id, "verification_email_sent", channel=channel, meta={"email": email})
        db.commit()

        return _claim_check_email_page(token, email)
    finally:
        db.close()


@app.route("/claim/<token>/verify/<vtoken>")
def claim_email_verify(token, vtoken):
    """Confirms a claim-flow email-capture link (see claim_email above),
    then records the address and fires generation via the same
    _claim_generate_and_redirect every other claim path uses."""
    try:
        data = serializer.loads(vtoken, max_age=TOKEN_MAX_AGE)
    except SignatureExpired:
        return redirect("/verify-error.html?reason=expired")
    except BadSignature:
        return redirect("/verify-error.html?reason=invalid")

    if data.get("prospect_token") != token:
        # The outer URL token and the token embedded in the signed vtoken
        # must agree — a cheap belt-and-braces check against a vtoken being
        # replayed against a different prospect's claim URL, on top of the
        # signature itself already proving it wasn't tampered with.
        return redirect("/verify-error.html?reason=invalid")

    email = (data.get("email") or "").strip().lower()

    db = SessionLocal()
    try:
        prospect = db.query(Prospect).filter(Prospect.token == token).first()
        if not prospect:
            return redirect("/verify-error.html?reason=invalid")

        channel = _prospect_last_touch_channel(db, prospect.id)
        _log_prospect_event(db, prospect.id, "verification_link_clicked", channel=channel, meta={"email": email})

        if not prospect.email:
            prospect.email = email
            prospect.email_found = True  # has_findable_email — see Section 11's schema note

            account = db.query(Account).filter(Account.email == email).first()
            if account is None:
                account = Account(email=email)
                db.add(account)
            db.commit()
        # else: already verified by an earlier click on this same link
        # (or a repeat visit) — idempotent, just proceed to generation.

        return _claim_generate_and_redirect(db, prospect)
    finally:
        db.close()


@app.route("/s/<short_code>")
def short_link(short_code):
    """Short redirect (Section 9a) — resolves to the real /claim/<token> URL
    server-side, so SMS copy can stay under length limits. short_code is
    generated per-prospect at the point a send is queued
    (outreach/send_job.py:_ensure_short_code)."""
    db = SessionLocal()
    try:
        prospect = db.query(Prospect).filter(Prospect.short_code == short_code).first()
        if not prospect:
            return redirect("/verify-error.html?reason=invalid")
        _log_prospect_event(db, prospect.id, "short_link_clicked", channel=_prospect_last_touch_channel(db, prospect.id))
        db.commit()
        return redirect(f"/claim/{prospect.token}")
    finally:
        db.close()


def _unsubscribe_landing_page():
    inner = """<div class="acct-card" style="text-align:center;">
      <h1 style="margin:0 0 10px;font-weight:800;font-size:24px;letter-spacing:-.02em;">You're unsubscribed</h1>
      <p style="margin:0;font-size:15px;color:#5C5A56;line-height:1.6;">You won't receive any more emails from Groundwork about this. If this was a mistake, just reply to any of our previous emails.</p>
    </div>"""
    return render_template_string(_account_page(inner, "Unsubscribed"))


@app.route("/unsubscribe/<token>", methods=["GET", "POST"])
def unsubscribe(token):
    """
    One-click email unsubscribe (docs/outreach-pipeline-spec.md Section 11b),
    RFC 8058 compliant. Reuses the same per-prospect token as /claim/<token>
    — it's the same prospect identity either way, so a second token wasn't
    introduced for this.

    GET: the "click here to unsubscribe" text link in the email body —
    unsubscribes immediately (no login, no confirmation click) and shows a
    landing page.
    POST: what a mail client sends automatically when it honors the
    List-Unsubscribe-Post header (RFC 8058) — same effect, no page needed,
    just a 200.

    Deliberately does not reveal whether the token matched anything — same
    response either way, so this can't be used to probe for valid tokens.
    """
    db = SessionLocal()
    try:
        prospect = db.query(Prospect).filter(Prospect.token == token).first()
        if prospect and not prospect.email_unsubscribed:
            prospect.email_unsubscribed = True
            prospect.email_unsubscribed_at = datetime.utcnow()
            db.commit()
    finally:
        db.close()

    if request.method == "POST":
        return "", 200
    return _unsubscribe_landing_page()


_SURVEY_STYLE = """<style>
.gwsvy-field{margin:0 0 22px;}
.gwsvy-label{display:block;font-weight:700;font-size:14.5px;margin-bottom:10px;}
.gwsvy-opts{display:flex;flex-direction:column;gap:8px;}
.gwsvy-opt{display:flex;align-items:center;gap:10px;padding:11px 14px;border:1px solid #E2E0DA;border-radius:9px;cursor:pointer;font-size:14.5px;}
.gwsvy-opt:hover{border-color:#B8B5AE;}
.gwsvy-opt input{margin:0;}
textarea.gwsvy-text{width:100%;padding:12px 14px;border:1px solid #D9D7D0;border-radius:9px;font-size:14.5px;font-family:Inter,sans-serif;min-height:70px;resize:vertical;box-sizing:border-box;}
.gwsvy-optional{color:#9A9893;font-weight:500;font-size:12.5px;}
</style>"""

_SURVEY_PRIMARY_REASONS = [
    ("price", "The price"),
    ("dont_see_need", "Didn't feel like I needed a new website"),
    ("already_has_website", "I already have a website"),
    ("using_someone_else", "Already using/planning to use someone else"),
    ("still_deciding", "Still deciding, haven't ruled it out"),
    ("technical_issue", "Ran into a problem trying to go live"),
    ("design_not_right", "The site itself wasn't right for my business"),
    ("other", "Other"),
]

# Trial-days-earned tiers, applied at checkout (create_checkout_session)
# instead of the old flat 30-days-for-everyone default. Only "price" earns
# a free-trial response — every other reason gets acknowledged but no
# discount, since a free month doesn't address "I don't have time" or "not
# sure this is legit" and offering one anyway reads as a generic bribe
# rather than a genuine answer to what was actually said.
_SURVEY_PRICE_TRIAL_DAYS = 30


def _apply_survey_answer_effects(db, prospect, primary_reason):
    """Shared by both survey entry points (the full /survey/<token> form
    and the one-click /claim/<token>/why buttons) so a given answer has
    the same real effect regardless of which path it came through.

    - "price": earns the real trial-days incentive, applied at checkout —
      never downgrades an existing higher value (e.g. don't undo hail-mary's
      90 days if this fires after it for some reason).
    - "already_has_website": the prospect is telling us directly that our
      website_status data is wrong (added 2026-07-26, same correction the
      admin "website found on Facebook page" button makes, just
      self-reported instead of admin-spotted — arguably higher-confidence
      since it's the business itself). Flips website_status to
      has_website, which — via the existing sms_channel_eligible/Facebook
      DM queue filters — correctly stops any further SMS/Facebook contact
      for this prospect going forward. Logged as a ProspectEvent for the
      same future pattern-analysis reason as the admin correction.
    Every other reason is recorded as-is with no side effect — the answer
    itself is the value, not a trigger for anything further."""
    if primary_reason == "price":
        prospect.trial_days_earned = max(prospect.trial_days_earned or 0, _SURVEY_PRICE_TRIAL_DAYS)
    elif primary_reason == "already_has_website" and prospect.website_status != "has_website":
        prospect.website_status = "has_website"
        _log_prospect_event(db, prospect.id, "website_found_manual", channel=None, meta={
            "source": "prospect_self_reported_survey",
        })
_SURVEY_DECISION_MAKERS = [("owner", "Yes, it's my decision"), ("employee", "No, someone else decides"), ("other", "Other")]
_SURVEY_CUSTOMER_SOURCES = [
    ("word_of_mouth", "Word of mouth / referrals"), ("google_search", "Google search"),
    ("social_media", "Social media"), ("directories", "Trade directories (Checkatrade, Yell, etc.)"),
    ("repeat_customers", "Repeat customers"), ("other", "Other"),
]
_SURVEY_TIMELINES = [
    ("this_week", "This week"), ("this_month", "This month"),
    ("not_sure", "Not sure yet"), ("no_plans", "No plans to go live"),
]


def _survey_radio_group(name, options, legend):
    opts_html = "".join(
        f'<label class="gwsvy-opt"><input type="radio" name="{name}" value="{val}" required>{escape(label)}</label>'
        for val, label in options
    )
    return f'<div class="gwsvy-field"><span class="gwsvy-label">{escape(legend)}</span><div class="gwsvy-opts">{opts_html}</div></div>'


def _survey_form_page(prospect, error=None):
    error_html = f'<p style="color:#DC2626;font-weight:600;margin:0 0 16px;">{escape(error)}</p>' if error else ""
    inner = f"""{_SURVEY_STYLE}
<div class="acct-card">
  <h1 style="margin:0 0 6px;font-weight:800;font-size:24px;letter-spacing:-.02em;">Quick favour, {escape(prospect.business_name or "there")}?</h1>
  <p style="margin:0 0 24px;font-size:15px;color:#5C5A56;line-height:1.6;">
    Answer a few quick questions about your website preview — genuinely helps us fix what's not working.
    No obligation either way, and your site's still free to go live on whenever you're ready.
  </p>
  {error_html}
  <form method="post">
    {_survey_radio_group("primary_reason", _SURVEY_PRIMARY_REASONS, "What's the main reason you haven't gone live yet?")}
    <div class="gwsvy-field">
      <span class="gwsvy-label">Anything else you'd add? <span class="gwsvy-optional">(optional)</span></span>
      <textarea class="gwsvy-text" name="reason_detail" maxlength="1000"></textarea>
    </div>
    {_survey_radio_group("decision_maker", _SURVEY_DECISION_MAKERS, "Is going live your call to make?")}
    {_survey_radio_group("how_get_customers", _SURVEY_CUSTOMER_SOURCES, "How do most of your customers find you today?")}
    {_survey_radio_group("timeline", _SURVEY_TIMELINES, "If you were to go live, what's the timeline?")}
    <div class="gwsvy-field">
      <span class="gwsvy-label">What would make going live an easy yes? <span class="gwsvy-optional">(optional)</span></span>
      <textarea class="gwsvy-text" name="what_would_change_mind" maxlength="1000"></textarea>
    </div>
    <button type="submit" class="acct-btn" style="width:100%;">Submit</button>
  </form>
</div>"""
    return render_template_string(_account_page(inner, "Quick survey"))


def _survey_confirmation_page(response, prospect=None):
    # No setup-fee discount code any more (the setup fee itself was removed
    # 2026-07-23) — this used to show an "X% off your setup fee" code here.
    # Real offer text now reflects prospect.trial_days_earned (set by
    # _apply_survey_answer_effects) instead of a blanket claim that used to
    # be shown to everyone regardless of what they answered.
    trial_days = (prospect.trial_days_earned if prospect else 0) or 0
    if trial_days >= 30:
        months = trial_days // 30
        offer_line = f"Since price was the thing — your site's free to go live on today, and now the first {months} month{'s' if months != 1 else ''} are free too, £24.99/month after that."
    else:
        offer_line = "Remember — your site's still free to go live on whenever you're ready, no setup fee, no obligation."
    inner = f"""<div class="acct-card" style="text-align:center;">
      <h1 style="margin:0 0 10px;font-weight:800;font-size:24px;letter-spacing:-.02em;">Thanks — that's genuinely useful</h1>
      <p style="margin:0 0 14px;font-size:15px;color:#5C5A56;line-height:1.6;">We read every response.</p>
      <p style="margin:0;font-size:15px;color:#5C5A56;line-height:1.6;">{offer_line}</p>
    </div>"""
    return render_template_string(_account_page(inner, "Thanks"))


@app.route("/survey/<token>", methods=["GET", "POST"])
def prospect_survey(token):
    """The 'why did/didn't you go live' survey (added 2026-07-17) —
    offered to prospects who've clicked their magic link (a real
    generated site exists) but haven't paid. Captures structured
    attributes the sourcing pipeline can't see on its own — decision-maker,
    existing website spend, acquisition channel, timeline, stated reason —
    real candidates for new scoring factors under Section 5b, which today
    only has Places-API-derived signals to work with. Not gating anything
    (the generated site is already visible via /claim/<token> regardless)
    — this is a separate, optional, incentivized ask, linked from Stage
    C/D follow-up copy (outreach/templates.py) since those are exactly the
    'clicked, no payment yet' / 'account created, no payment yet'
    audiences this is aimed at.

    Idempotent like /claim/<token>: a second visit after submitting shows
    the original confirmation (with the original code, not a new one —
    SurveyResponse.prospect_id is unique) rather than a fresh form."""
    db = SessionLocal()
    try:
        prospect = db.query(Prospect).filter(Prospect.token == token).first()
        if not prospect or not prospect.clicked_at:
            # No real generated site to have an opinion about — same "don't
            # reveal whether a token is valid" posture as /unsubscribe.
            abort(404)

        existing = db.query(SurveyResponse).filter(SurveyResponse.prospect_id == prospect.id).first()
        if existing:
            return _survey_confirmation_page(existing, prospect)

        if request.method == "GET":
            return _survey_form_page(prospect)

        primary_reason = request.form.get("primary_reason", "")
        if not primary_reason:
            return _survey_form_page(prospect, error="Please answer the first question to continue.")

        # decision and already_pays_for_website are no longer asked — both
        # are already known from data we already have, so asking again is
        # pure friction with no new signal:
        #   - decision: derivable from the prospect's own funnel timestamps
        #     (paid_at/account_created_at/clicked_at), tracked on Prospect
        #     already. Only unpaid prospects ever reach this survey (see
        #     outreach/followup.py's run_followups filter), so "went_live"
        #     only applies to the rare case Stripe-paid status and this
        #     touch race each other.
        #   - already_pays_for_website: this is exactly what
        #     Prospect.website_status (has_website/no_website) already
        #     captured at sourcing time, straight from Google Places.
        decision = (
            "went_live" if prospect.paid_at
            else "not_yet" if prospect.account_created_at
            else "not_yet"
        )
        already_pays = (
            True if prospect.website_status == "has_website"
            else False if prospect.website_status == "no_website"
            else None
        )
        response = SurveyResponse(
            prospect_id=prospect.id,
            decision=decision,
            primary_reason=primary_reason,
            reason_detail=(request.form.get("reason_detail") or "").strip()[:1000] or None,
            decision_maker=request.form.get("decision_maker") or None,
            already_pays_for_website=already_pays,
            how_get_customers=request.form.get("how_get_customers") or None,
            timeline=request.form.get("timeline") or None,
            what_would_change_mind=(request.form.get("what_would_change_mind") or "").strip()[:1000] or None,
        )
        db.add(response)
        _apply_survey_answer_effects(db, prospect, primary_reason)
        db.commit()
        db.refresh(response)
        return _survey_confirmation_page(response, prospect)
    finally:
        db.close()


# One-click "why not yet" question (added 2026-07-26) — a much lower-
# friction sibling to the full 6-question /survey/<token> form, meant to
# fire early (shortly after a prospect clicks and sees their preview, not
# only as a 14-21-day last resort like hail_mary) since the whole point is
# catching people while their reason is still fresh, not after it's faded.
# One click, no typing, writes into the same SurveyResponse table/unique
# constraint as the full form — either path answers the same underlying
# question, so they share one source of truth and the same
# _apply_survey_answer_effects side effects.
_QUICK_SURVEY_ANSWERS = {
    "too_expensive": {"label": "Too expensive", "primary_reason": "price"},
    "not_legit": {"label": "Not sure this is legit", "primary_reason": "trust_skepticism"},
    "no_time": {"label": "Don't have time right now", "primary_reason": "no_time"},
    "dont_need": {"label": "Don't think I need a website", "primary_reason": "dont_see_need"},
    "has_website": {"label": "I already have a website", "primary_reason": "already_has_website"},
    "other": {"label": "Something else", "primary_reason": "other"},
}


def _quick_survey_form_page(token, prospect):
    buttons = "".join(
        f'<a href="/claim/{escape(token)}/why/{key}" class="acct-btn" '
        f'style="display:block;width:100%;margin:0 0 10px;text-align:center;background:#fff;color:#1C1C1C;'
        f'border:1px solid #D9D7D0;font-weight:600;">{escape(a["label"])}</a>'
        for key, a in _QUICK_SURVEY_ANSWERS.items()
    )
    inner = f"""<div class="acct-card">
  <h1 style="margin:0 0 6px;font-weight:800;font-size:22px;letter-spacing:-.02em;">What's stopping you, {escape(prospect.business_name or "there")}?</h1>
  <p style="margin:0 0 20px;font-size:14.5px;color:#5C5A56;line-height:1.6;">One click, no typing — genuinely helps, no obligation either way.</p>
  {buttons}
</div>"""
    return render_template_string(_account_page(inner, "Quick question"))


def _quick_survey_other_form_page(token, error=None):
    error_html = f'<p style="color:#DC2626;font-weight:600;margin:0 0 16px;">{escape(error)}</p>' if error else ""
    inner = f"""<div class="acct-card">
  <h1 style="margin:0 0 6px;font-weight:800;font-size:22px;letter-spacing:-.02em;">What's the reason?</h1>
  {error_html}
  <form method="post">
    <textarea name="detail" maxlength="1000" rows="4" style="width:100%;padding:12px;border:1px solid #D9D7D0;border-radius:10px;font-family:inherit;font-size:15px;box-sizing:border-box;margin:0 0 14px;" placeholder="A sentence is plenty"></textarea>
    <button type="submit" class="acct-btn" style="width:100%;">Submit</button>
  </form>
</div>"""
    return render_template_string(_account_page(inner, "Quick question"))


def _quick_survey_confirmation_page(answer, response, prospect):
    """Branches the response by what was actually said — a discount only
    ever appears for "too_expensive", since money doesn't answer "not sure
    this is legit" or "don't have time", and offering one anyway would
    read as a blind bribe rather than a real answer (see
    _apply_survey_answer_effects's docstring)."""
    if answer == "too_expensive":
        months = (prospect.trial_days_earned or 0) // 30
        body = (f"No problem — your first {months} month{'s' if months != 1 else ''} are free instead of the usual 1, "
                f"£24.99/month after that, still no setup fee. <a href=\"/claim/{escape(prospect.token)}\">Take another look</a> whenever you're ready.")
    elif answer == "not_legit":
        body = ("Fair to be cautious — happy to talk it through directly, just reply to any of our emails or "
                "call/message us, a real person answers. Your preview isn't going anywhere either way.")
    elif answer == "no_time":
        body = "There's nothing left to do on your end — the site's already built. One click whenever you get a minute and it's live."
    elif answer == "dont_need":
        body = "Fair enough — no further emails pushing it. Your preview stays up if you ever change your mind."
    elif answer == "has_website":
        body = "Thanks for the heads up — updated our records so we've got that right for next time."
    else:
        body = "Thanks — genuinely useful, we read every one of these."
    inner = f"""<div class="acct-card" style="text-align:center;">
  <h1 style="margin:0 0 10px;font-weight:800;font-size:22px;letter-spacing:-.02em;">Thanks — got it</h1>
  <p style="margin:0;font-size:15px;color:#5C5A56;line-height:1.6;">{body}</p>
</div>"""
    return render_template_string(_account_page(inner, "Thanks"))


@app.route("/claim/<token>/why")
def claim_why(token):
    db = SessionLocal()
    try:
        prospect = db.query(Prospect).filter(Prospect.token == token).first()
        if not prospect or not prospect.clicked_at:
            abort(404)
        existing = db.query(SurveyResponse).filter(SurveyResponse.prospect_id == prospect.id).first()
        if existing:
            return _survey_confirmation_page(existing, prospect)
        return _quick_survey_form_page(token, prospect)
    finally:
        db.close()


@app.route("/claim/<token>/why/<answer>", methods=["GET", "POST"])
def claim_why_answer(token, answer):
    if answer not in _QUICK_SURVEY_ANSWERS:
        abort(404)
    db = SessionLocal()
    try:
        prospect = db.query(Prospect).filter(Prospect.token == token).first()
        if not prospect or not prospect.clicked_at:
            abort(404)
        existing = db.query(SurveyResponse).filter(SurveyResponse.prospect_id == prospect.id).first()
        if existing:
            return _survey_confirmation_page(existing, prospect)

        if answer == "other":
            if request.method == "GET":
                return _quick_survey_other_form_page(token)
            detail = (request.form.get("detail") or "").strip()[:1000] or None
        else:
            detail = None

        primary_reason = _QUICK_SURVEY_ANSWERS[answer]["primary_reason"]
        decision = "went_live" if prospect.paid_at else "not_yet"
        already_pays = (
            True if prospect.website_status == "has_website"
            else False if prospect.website_status == "no_website"
            else None
        )
        response = SurveyResponse(
            prospect_id=prospect.id, decision=decision, primary_reason=primary_reason,
            reason_detail=detail, already_pays_for_website=already_pays,
        )
        db.add(response)
        _apply_survey_answer_effects(db, prospect, primary_reason)
        db.commit()
        db.refresh(response)

        app.logger.info(f"Quick survey answered: prospect {prospect.id} ({prospect.business_name}) -> {answer}")
        return _quick_survey_confirmation_page(answer, response, prospect)
    finally:
        db.close()


@app.route("/api/account/session")
def api_account_session():
    email = session.get("account_email")
    return jsonify({"logged_in": bool(email), "email": email})


def account_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("account_email"):
            return jsonify({"error": "not_authenticated"}), 401
        return view(*args, **kwargs)
    return wrapped


@app.route("/api/account/support-message", methods=["POST"])
@account_required
def api_account_support_message():
    email = session["account_email"]
    message = (request.form.get("message") or "").strip()
    if not message:
        return jsonify({"error": "empty_message"}), 400
    if len(message) > 4000:
        return jsonify({"error": "message_too_long"}), 400
    db = SessionLocal()
    try:
        gen = db.query(Generation).filter(Generation.email == email).order_by(Generation.created_at.desc()).first()
        business_name = gen.business_name if gen else None
        subdomain = gen.subdomain if gen else None
    finally:
        db.close()
    send_support_message_email(email, message, business_name=business_name, subdomain=subdomain)
    return jsonify({"status": "sent"})


@app.route("/api/account/generations/<int:gen_id>/images")
@account_required
def api_generation_images(gen_id):
    """Current logo/photo slots for a generation the signed-in account owns —
    lets the dashboard show what's live without parsing html_content."""
    email = session["account_email"]
    db = SessionLocal()
    try:
        gen = db.query(Generation).filter(Generation.id == gen_id, Generation.email == email).first()
        if not gen:
            return jsonify({"error": "not_found"}), 404
        images = db.query(GenerationImage).filter(GenerationImage.generation_id == gen_id).all()
        return jsonify({"images": [{"slot": img.slot, "data_uri": img.data_uri} for img in images]})
    finally:
        db.close()


@app.route("/api/account/generations/<int:gen_id>/images/<slot>", methods=["POST"])
@account_required
def api_update_generation_image(gen_id, slot):
    """
    Replaces one image slot (logo / photo_N) on a generation the signed-in
    account owns. Reuses the exact same Pillow processing used at generation
    time (_logo_file_to_data_uri / _image_file_to_data_uri), then swaps the
    old data URI for the new one in html_content via a single exact-string
    replace() — safe because GenerationImage.data_uri is the literal string
    that was substituted into html_content in the first place, so we know
    precisely what to look for. No HTML parsing/regex involved.
    """
    email = session["account_email"]
    file = request.files.get("image")
    if not file or not file.filename:
        return jsonify({"error": "no_file"}), 400

    db = SessionLocal()
    try:
        gen = db.query(Generation).filter(Generation.id == gen_id, Generation.email == email).first()
        if not gen:
            return jsonify({"error": "not_found"}), 404

        img_row = db.query(GenerationImage).filter(
            GenerationImage.generation_id == gen_id, GenerationImage.slot == slot
        ).first()
        if not img_row:
            # No tracked row for this slot — either an old generation predating
            # this feature, or an unrecognised slot name. Nothing safe to swap.
            return jsonify({"error": "slot_not_editable"}), 404

        tmp_path = os.path.join(UPLOAD_DIR, f"_edit_{uuid.uuid4().hex}_{file.filename}")
        file.save(tmp_path)
        try:
            if slot == "logo":
                new_data_uri, _mode, _bg_hex, _accent_hex = _logo_file_to_data_uri(tmp_path, max_dimension=480)
            else:
                new_data_uri = _image_file_to_data_uri(tmp_path, max_dimension=1600)
        finally:
            try:
                os.remove(tmp_path)
            except OSError:
                pass

        gen.html_content = gen.html_content.replace(img_row.data_uri, new_data_uri)
        img_row.data_uri = new_data_uri
        img_row.mime = _data_uri_mime(new_data_uri)
        db.commit()

        return jsonify({"status": "ok", "slot": slot, "data_uri": new_data_uri})
    finally:
        db.close()


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("is_admin"):
            return redirect(url_for("admin_login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    error = None
    if request.method == "POST":
        ip = request.headers.get("X-Forwarded-For", request.remote_addr or "unknown").split(",")[0].strip()
        if _admin_login_locked_out():
            error = "Too many failed attempts. Try again in a few minutes."
        elif _rate_limited("admin_login", ip, limit=10, window_seconds=900):
            error = "Too many attempts from your network. Try again in a few minutes."
        else:
            u = request.form.get("username", "")
            p = request.form.get("password", "")
            admin_user = os.environ.get("ADMIN_USERNAME")
            admin_pass = os.environ.get("ADMIN_PASSWORD")
            if (admin_user and admin_pass
                    and hmac.compare_digest(u, admin_user)
                    and hmac.compare_digest(p, admin_pass)):
                _clear_admin_login_failures()
                session["is_admin"] = True
                return redirect(request.args.get("next") or url_for("admin_dashboard"))
            _record_admin_login_failure()
            error = "Invalid credentials."
    error_html = f'<p class="err">{error}</p>' if error else ""
    return render_template_string(f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Admin login</title><link rel="icon" type="image/x-icon" href="/favicon.ico"><link rel="icon" type="image/png" sizes="192x192" href="/favicon-192.png"><style>{_PAGE_STYLE}</style></head>
<body><div class="wrap" style="max-width:360px;"><h1>Admin login</h1>{error_html}
<form method="post">
<input type="text" name="username" placeholder="Username" autofocus>
<input type="password" name="password" placeholder="Password">
<button type="submit">Log in</button>
</form></div></body></html>""")


@app.route("/admin/logout")
def admin_logout():
    session.pop("is_admin", None)
    return redirect(url_for("admin_login"))


def _date_preset_range(preset: str, now: datetime):
    """Returns (from, to) datetimes for a named quick-preset, or (None, None)
    if unrecognized (falls back to the dashboard's no-filter default)."""
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if preset == "today":
        return today, now
    if preset == "this_week":
        start = today - timedelta(days=today.weekday())
        return start, now
    if preset == "last_week":
        this_week_start = today - timedelta(days=today.weekday())
        start = this_week_start - timedelta(days=7)
        end = this_week_start - timedelta(microseconds=1)
        return start, end
    if preset == "this_month":
        start = today.replace(day=1)
        return start, now
    if preset == "last_month":
        this_month_start = today.replace(day=1)
        last_month_end = this_month_start - timedelta(microseconds=1)
        last_month_start = last_month_end.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return last_month_start, last_month_end
    if preset == "all_time":
        return datetime(2020, 1, 1), now
    return None, None


# Shared by /admin and /admin/funnel (added 2026-07-20 — funnel previously
# had no preset buttons at all, only raw from/to inputs) so both pages'
# quick-filter buttons can never drift out of sync with each other or with
# _date_preset_range's own recognized keys.
_DATE_PRESETS = [
    ("today", "Today"),
    ("this_week", "This week"), ("last_week", "Last week"),
    ("this_month", "This month"), ("last_month", "Last month"),
    ("all_time", "All time"),
]


def _render_date_preset_links(base_url: str, active_preset: str, extra_params: str = "") -> str:
    """Pill-style quick-filter buttons, shared markup for /admin and
    /admin/funnel — extra_params (e.g. "&channel=email") lets a page
    preserve its own other filters when a preset is clicked."""
    return "".join(
        f'<a href="{base_url}?preset={key}{extra_params}" style="padding:6px 13px;border-radius:999px;font-size:12.5px;font-weight:700;'
        f'text-decoration:none;{"background:#1C1C1C;color:#fff;" if active_preset == key else "background:#fff;color:#5C5A56;border:1px solid #D8D5CE;"}">{label}</a>'
        for key, label in _DATE_PRESETS
    )


# Same "both"/"email"/"sms"/"facebook" values admin_funnel() already
# accepts — shared here so /admin's channel buttons and /admin/funnel's
# channel <select> can never drift apart on what values are valid.
# "Facebook" relabeled "Social DM" (2026-07-27) to read correctly now that
# a SEPARATE sourcing-channel axis exists (below) — this one is strictly
# outreach METHOD (how a prospect is contacted), not where they came from.
_CHANNEL_FILTERS = [
    ("both", "All channels"), ("email", "Email"), ("sms", "SMS"), ("facebook", "Social DM"),
]

# Sourcing-channel filter values, independent of outreach method above —
# "how was this prospect originally found" (Prospect.sourcing_channel).
# Sourced from outreach/sourcing_channels.py so a future sourcing method
# only needs registering there to show up here automatically.
_SOURCE_FILTERS = [("all", "All sources")] + list(SOURCING_CHANNEL_LABELS.items())


def _render_channel_filter_links(base_url: str, active_channel: str, extra_params: str = "") -> str:
    """Pill-style channel-filter buttons, same visual language as
    _render_date_preset_links above (added 2026-07-25 for /admin's
    dashboard, by request — sits above the date buttons, not beside them)."""
    return "".join(
        f'<a href="{base_url}?channel={key}{extra_params}" style="padding:6px 13px;border-radius:999px;font-size:12.5px;font-weight:700;'
        f'text-decoration:none;{"background:#3B82F6;color:#fff;" if active_channel == key else "background:#fff;color:#5C5A56;border:1px solid #D8D5CE;"}">{label}</a>'
        for key, label in _CHANNEL_FILTERS
    )


def _render_source_filter_links(base_url: str, active_source: str, extra_params: str = "") -> str:
    """Pill-style SOURCING-channel filter buttons (added 2026-07-27, by
    request) — sits above _render_channel_filter_links's outreach-method
    row, so the click order on the page matches how the two axes actually
    relate: pick where they came from first, then narrow by how they were
    contacted. Same visual language, a darker fill so the two rows read as
    a clear hierarchy rather than two identical-looking button groups."""
    return "".join(
        f'<a href="{base_url}?source={key}{extra_params}" style="padding:6px 13px;border-radius:999px;font-size:12.5px;font-weight:700;'
        f'text-decoration:none;{"background:#1C1C1C;color:#fff;" if active_source == key else "background:#fff;color:#5C5A56;border:1px solid #D8D5CE;"}">{label}</a>'
        for key, label in _SOURCE_FILTERS
    )


@app.route("/admin")
@admin_required
def admin_dashboard():
    """Top-level admin landing page — the headline KPI strip, full-size,
    with a channel filter (buttons, above the date filter) and a date
    filter (quick presets + explicit from/to) so week-by-week or month-by-
    month comparison is possible, not just "this month." The same
    _render_kpi_strip() component appears (smaller context, no filter) on
    /admin/funnel and /admin/domains too, so the same numbers stay visible
    wherever they're relevant, not just here.

    Channel filter + embedded funnel table added 2026-07-25, by request —
    every KPI (_compute_kpis' channel param) and the funnel table below it
    (_render_funnel_table_html, the same function /admin/funnel uses) both
    scope to whichever channel is selected, so this page is now a complete
    per-channel view, not just a summary that links out to the Funnel page
    for the breakdown."""
    now = datetime.utcnow()
    preset = request.args.get("preset", "").strip()
    from_str = request.args.get("from", "").strip()
    to_str = request.args.get("to", "").strip()
    channel = request.args.get("channel", "both").strip().lower()
    if channel not in ("email", "sms", "facebook", "both"):
        channel = "both"
    source = request.args.get("source", "all").strip().lower()
    if source not in SOURCING_CHANNEL_LABELS:
        source = "all"

    def _parse_date(s):
        try:
            return datetime.strptime(s, "%Y-%m-%d")
        except ValueError:
            return None

    range_from, range_to = (None, None)
    if preset:
        range_from, range_to = _date_preset_range(preset, now)
    elif from_str or to_str:
        range_from = _parse_date(from_str)
        range_to = _parse_date(to_str)
        if range_to:
            range_to = range_to.replace(hour=23, minute=59, second=59)

    db = SessionLocal()
    try:
        kpis = _compute_kpis(db, range_from=range_from, range_to=range_to, channel=channel, source=source)
        strip = _render_kpi_strip(kpis)
        funnel_table_html = _render_funnel_table_html(db, now, range_from, range_to, channel, source=source)
    finally:
        db.close()

    banner = ""

    _source_qs = f"&source={source}" if source != "all" else ""
    _channel_qs = (f"&channel={channel}" if channel != "both" else "") + _source_qs
    source_links = _render_source_filter_links("/admin", source, extra_params=(f"&preset={preset}" if preset else ""))
    channel_links = _render_channel_filter_links(
        "/admin", channel, extra_params=(f"&preset={preset}" if preset else "") + _source_qs
    )
    preset_links = _render_date_preset_links("/admin", preset, extra_params=_channel_qs)

    content = f"""
<h1 class="adm-title">Dashboard</h1>
<p class="adm-sub">Headline KPIs for <strong>{escape(kpis["period_label"])}</strong> · <strong>{escape(kpis["source_label"])}</strong> · <strong>{escape(kpis["channel_label"])}</strong>, computed fresh from real data on every load.</p>

<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:8px;">
  {source_links}
</div>
<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:10px;">
  {channel_links}
</div>
<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:14px;">
  {preset_links}
</div>
<form method="get" style="display:flex;gap:12px;align-items:flex-end;flex-wrap:wrap;margin-bottom:22px;">
  <input type="hidden" name="channel" value="{escape(channel)}">
  <input type="hidden" name="source" value="{escape(source)}">
  <div>
    <label style="display:block;font-size:12px;font-weight:600;color:#5C5A56;margin-bottom:4px;">From</label>
    <input type="date" name="from" value="{escape(from_str)}" style="padding:8px 10px;border:1px solid #D8D5CE;border-radius:7px;font-size:13.5px;">
  </div>
  <div>
    <label style="display:block;font-size:12px;font-weight:600;color:#5C5A56;margin-bottom:4px;">To</label>
    <input type="date" name="to" value="{escape(to_str)}" style="padding:8px 10px;border:1px solid #D8D5CE;border-radius:7px;font-size:13.5px;">
  </div>
  <button type="submit" style="background:#3B82F6;color:#fff;border:0;font-weight:700;padding:9px 18px;border-radius:7px;font-size:13.5px;cursor:pointer;">Apply</button>
  <a href="/admin" style="font-size:13px;color:#807E79;text-decoration:none;padding:9px 4px;">Reset to default (this month, all channels)</a>
</form>

{banner}
{strip}

<h2 style="font-size:15px;font-weight:700;margin:28px 0 10px;">Funnel — {escape(kpis["channel_label"])}</h2>
{funnel_table_html}

<div class="adm-card" style="padding:20px 22px;margin-top:20px;">
  <p style="margin:0;font-size:13.5px;color:#5C5A56;">
    See these broken down further: <a href="/admin/funnel">Funnel</a> (recent clicks, survey answers, send-timing detail) ·
    <a href="/admin/domains">Domains &amp; margins</a> (per-domain purchase/margin detail).
  </p>
</div>"""
    return render_template_string(_admin_page("Dashboard", content, active="dashboard"))


@app.route("/admin/generations")
@admin_required
def admin_generations():
    db = SessionLocal()
    try:
        gens = db.query(Generation).order_by(Generation.created_at.desc()).all()
        domains_by_gen = {}
        for d in db.query(Domain).filter(Domain.generation_id.isnot(None)).all():
            domains_by_gen.setdefault(d.generation_id, []).append(d)

        row_parts = []
        for g in gens:
            test_badge = '<span class="badge-test">TEST</span>' if (g.lead and g.lead.is_test) else ""
            pending_badge = ('<span style="background:#F59E0B;color:#fff;font-size:10px;font-weight:700;'
                             'padding:2px 6px;border-radius:4px;margin-left:6px;vertical-align:middle;">PENDING</span>'
                             if getattr(g, 'html_pending', None) else "")
            apply_link = ('<a href="/admin/generations/' + str(g.id) + '/pending-changes" '
                          'style="color:#D97706;font-weight:700;">Review changes →</a> · '
                          if getattr(g, 'html_pending', None) else "")
            live_link = ('<a href="https://' + str(g.subdomain) + '.' + _SUBDOMAIN_BASE + '" target="_blank" rel="noopener">Live site ↗</a> · '
                         if g.status == "live" and g.subdomain else "")

            gen_domains = domains_by_gen.get(g.id, [])
            if gen_domains:
                domain_cell = "<br>".join(
                    f'<a href="https://{escape(d.domain)}" target="_blank" rel="noopener">{escape(d.domain)}</a> '
                    f'<span style="font-size:11px;color:#9A9893;">({escape(d.status)})</span>'
                    for d in gen_domains
                )
            else:
                domain_cell = '<span class="muted">—</span>'

            row_parts.append(
                '<tr id="gen-row-' + str(g.id) + '" data-email="' + str(escape(g.email)) + '">'
                '<td>' + str(escape(g.business_name or "")) + test_badge + "</td><td>" + str(escape(g.email)) + "</td>"
                "<td>" + g.created_at.strftime("%d %b %Y %H:%M") + "</td>"
                "<td>" + str(escape(g.status)) + pending_badge + "</td>"
                "<td>" + domain_cell + "</td>"
                '<td>' + apply_link + live_link +
                '<a href="/admin/generations/' + str(g.id) + '/html" target="_blank" rel="noopener">View HTML</a> · '
                '<a href="/admin/generations/' + str(g.id) + '/form-data" target="_blank" rel="noopener">Form data</a> · '
                + ('<a href="/preview.html?id=' + str(g.lead.public_id) + '" target="_blank" rel="noopener">Preview</a> · '
                   '<a href="/editor.html?id=' + str(g.lead.public_id) + '" target="_blank" rel="noopener">Edit text</a>'
                   if g.lead else '') +
                '</td>'
                '<td>'
                '<a href="#" title="Delete only this generation" '
                'onclick="return gwDeleteGeneration(' + str(g.id) + ', ' + str(escape(json.dumps(g.business_name or "this site"))) + ')" '
                'style="color:#9B2B1A;font-weight:700;text-decoration:none;margin-right:10px;">Delete site</a>'
                '<a href="#" title="Delete this ENTIRE account" '
                'onclick="return gwDeleteAccount(' + str(escape(json.dumps(g.email))) + ')" '
                'style="color:#9B2B1A;font-weight:800;text-decoration:none;">×</a></td></tr>'
            )
        rows = "".join(row_parts)
        content = f"""
<h1 class="adm-title">All sites <span style="color:#9A9893;font-weight:600;font-size:17px;">({len(gens)})</span></h1>
<p class="adm-sub">
  "Delete site" removes just that one generation. × removes the entire account — login, every lead, and every generated site for that email (use with care: several rows can share the same email).
  To connect a domain, grab the public id from Preview/Edit below, then use
  <a href="{SITE_URL}/domain-search.html" target="_blank" rel="noopener">domain search →</a>
  &nbsp;·&nbsp;
  <a href="/admin/generate-test">+ Generate test site</a>
</p>
<div class="adm-card">
<table><thead><tr>
  <th>Business</th><th>Email</th><th>Created</th><th>Status</th><th>Domain</th><th>Links</th><th></th>
</tr></thead>
<tbody>{rows}</tbody></table>
</div>
<script>
async function gwDeleteGeneration(genId, businessName) {{
  if (!confirm(`Delete "${{businessName}}"? This removes just this one generation (and its lead, if it has no others) — cannot be undone.`)) return false;
  try {{
    const r = await fetch(`/admin/generations/${{genId}}`, {{method: 'DELETE', credentials: 'same-origin'}});
    if (!r.ok) throw new Error('Delete failed (' + r.status + ')');
    document.getElementById(`gen-row-${{genId}}`)?.remove();
  }} catch (err) {{
    alert(err.message);
  }}
  return false;
}}
async function gwDeleteAccount(email) {{
  if (!confirm(`Delete the ENTIRE account for ${{email}}? This removes their login, every lead, and every generated site for this email — cannot be undone.`)) return false;
  try {{
    const r = await fetch(`/admin/accounts/${{encodeURIComponent(email)}}`, {{method: 'DELETE', credentials: 'same-origin'}});
    if (!r.ok) throw new Error('Delete failed (' + r.status + ')');
    document.querySelectorAll(`tr[data-email="${{CSS.escape(email)}}"]`).forEach(tr => tr.remove());
  }} catch (err) {{
    alert(err.message);
  }}
  return false;
}}
</script>"""
        return render_template_string(_admin_page("Sites", content, active="generations"))
    finally:
        db.close()


@app.route("/admin/domains")
@admin_required
def admin_domains():
    """At-a-glance view of purchased customer domains: status, purchase date,
    sale price / wholesale cost / margin, and the Porkbun/Cloudflare setup
    timestamps.

    Sale price, wholesale cost and margin are all read straight off the
    Domain row (snapshotted at purchase time in _handle_domain_order_async),
    not recomputed from the current TLD price table — so historical margin
    stays accurate even if pricing logic or Porkbun's wholesale prices change
    later. Older rows purchased before wholesale_gbp/margin_gbp existed will
    show '—' for those columns rather than a misleading recomputed value.
    """
    db = SessionLocal()
    try:
        kpi_strip = _render_kpi_strip(_compute_kpis(db))
        # is_internal excludes Groundwork's own personal/test domain
        # purchases (paid for real via Stripe while testing a flow, not a
        # real customer) — added 2026-07-19 after every single domain row
        # turned out to belong to one of a handful of personal test emails,
        # not an actual customer.
        doms = db.query(Domain).filter(Domain.is_internal == False).order_by(Domain.created_at.desc()).all()
        status_colors = {
            "active": ("#DCFCE7", "#166534"),
            "pending": ("#FEF3C7", "#92400E"),
            "needs_manual_setup": ("#FEE2E2", "#991B1B"),
        }
        row_parts = []
        for d in doms:
            sale = d.price_gbp
            cost = d.wholesale_gbp
            margin_html = f"£{d.margin_gbp:.2f}" if d.margin_gbp is not None else "—"
            bg, fg = status_colors.get(d.status, ("#E6E3DC", "#3A3A38"))
            status_badge = (
                f'<span style="background:{bg};color:{fg};font-size:11px;font-weight:700;'
                f'padding:2px 8px;border-radius:20px;">{escape(d.status)}</span>'
            )
            error_note = (
                f'<div style="font-size:12px;color:#9B2B1A;margin-top:4px;">'
                f'{escape(d.error_step or "")}: {escape((d.error_message or "")[:200])}</div>'
                if d.status == "needs_manual_setup" and d.error_message else ""
            )

            def _ts(val):
                return val.strftime("%d %b %Y %H:%M") if val else "—"

            row_parts.append(
                "<tr>"
                f"<td>{escape(d.domain)}</td>"
                f"<td>{status_badge}{error_note}</td>"
                f"<td>{_ts(d.created_at)}</td>"
                f"<td>{'£%.2f' % sale if sale is not None else '—'}</td>"
                f"<td>{'£%.2f' % cost if cost is not None else '—'}</td>"
                f"<td>{margin_html}</td>"
                f"<td>{escape(d.customer_email or '')}</td>"
                f"<td>{_ts(d.registered_at)}</td>"
                f"<td>{_ts(d.cloudflare_connected_at)}</td>"
                f"<td>{_ts(d.dns_configured_at)}</td>"
                "</tr>"
            )
        rows = "".join(row_parts)
        total_sale = sum(d.price_gbp or 0 for d in doms)
        total_margin = sum(d.margin_gbp or 0 for d in doms)
        content = f"""
<h1 class="adm-title">Customer domains <span style="color:#9A9893;font-weight:600;font-size:17px;">({len(doms)})</span></h1>
<p class="adm-sub">
  Sale/cost/margin snapshotted at purchase time — not recomputed from current pricing.
  &nbsp;·&nbsp; <strong style="color:#1C1C1C;">£{total_sale:.2f}</strong> sold
  &nbsp;·&nbsp; <strong style="color:#1C1C1C;">£{total_margin:.2f}</strong> margin
</p>

{kpi_strip}

<div class="adm-card">
<table><thead><tr>
  <th>Domain</th><th>Status</th><th>Purchased</th>
  <th>Sale</th><th>Cost</th><th>Margin</th>
  <th>Customer</th><th>Registered</th><th>Cloudflare</th><th>DNS</th>
</tr></thead>
<tbody>{rows}</tbody></table>
</div>"""
        return render_template_string(_admin_page("Domains &amp; margins", content, active="domains"))
    finally:
        db.close()


def _admin_test_form_page() -> str:
    """
    Single-page admin equivalent of the live 8-step frontend/build.html form.
    Reuses that form's actual CSS classes (.field, .option-card, .toggle-btn,
    the range-slider gradient) and the same choice-button/slider components,
    just laid out flat with no step gating — the live form's step validation
    is entirely client-side JS (see build.html's validate()/advance()) and
    the server has never seen per-step state; it only ever receives one
    fully-assembled submission at the end. A flat page changes nothing about
    that contract: this form POSTs the exact same field names
    (business_name, trade, location, coverage_area, phone, email,
    commercial_split, work_type, team_size, large_contracts, urgency,
    years_trading, accreditations, past_clients, notes, logo, photos)
    straight to this same route's POST handler below — unchanged, no
    special-casing needed for the admin path.
    """
    return """<style>
h1,h2,h3,h4{font-family:'Plus Jakarta Sans','Inter',sans-serif;}
input,select,textarea,button{font-family:Inter,sans-serif;}
input[type=range]{accent-color:#3B82F6;width:100%;height:8px;cursor:pointer;}
.field{display:flex;flex-direction:column;gap:7px;margin-bottom:16px;}
.field label{font-size:14px;font-weight:600;color:#3A3A38;}
.field input,.field textarea{padding:13px 15px;border-radius:10px;font-size:15.5px;color:#1C1C1C;background:#fff;width:100%;border:1px solid #D9D7D0;}
.field input:focus,.field textarea:focus{outline:none;border-color:#3B82F6;}
.section{background:#fff;border:1px solid #E6E3DC;border-radius:16px;padding:26px;margin-bottom:20px;}
.section h2{margin:0 0 16px;font-size:15px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:#2257CC;}
.option-row{display:flex;flex-direction:column;gap:12px;margin-bottom:16px;}
.option-card{display:flex;align-items:flex-start;gap:14px;width:100%;text-align:left;cursor:pointer;padding:17px 18px;border-radius:13px;transition:all .12s;border:1.5px solid #E0DDD5;background:#fff;}
.option-card.sel{border:2px solid #3B82F6;background:#F2F6FF;box-shadow:0 6px 18px -10px rgba(59,130,246,.5);}
.option-dot{flex-shrink:0;width:24px;height:24px;border-radius:50%;margin-top:1px;display:flex;align-items:center;justify-content:center;border:2px solid #CFCCC4;background:transparent;}
.option-card.sel .option-dot{border:0;background:#3B82F6;}
.option-dot svg{opacity:0;}
.option-card.sel .option-dot svg{opacity:1;}
.option-label{font-weight:700;font-size:16.5px;color:#1C1C1C;display:block;}
.option-desc{font-size:13.5px;color:#5C5A56;margin-top:2px;display:block;}
.toggle-row{display:flex;gap:12px;}
.toggle-btn{flex:1;cursor:pointer;padding:14px 18px;border-radius:11px;font-size:15.5px;font-weight:700;transition:all .12s;border:1.5px solid #E0DDD5;background:#fff;color:#5C5A56;}
.toggle-btn.sel{border:2px solid #3B82F6;background:#F2F6FF;color:#1D4FB5;}
.btn-submit{width:100%;background:#3B82F6;color:#fff;font-weight:700;font-size:16.5px;border:0;padding:16px 22px;border-radius:11px;cursor:pointer;}
.btn-submit:hover{background:#2563EB;}
</style>
<div style="max-width:680px;margin:0 auto;padding:clamp(28px,4vw,48px) 24px clamp(40px,6vw,72px);">
<p style="margin:0 0 12px;"><a href="/admin/generations" style="color:#9A9893;font-size:13px;text-decoration:none;">← All sites</a></p>
<h1 class="adm-title" style="margin:0 0 6px;">Generate a test site</h1>
<p class="adm-sub" style="margin:0 0 24px;">Admin-only — skips email verification and the one-generation-per-email limit. Flagged TEST in the generations list. Same inputs as the live form, all on one page.</p>

<form method="post" enctype="multipart/form-data">

<div class="section">
<h2>Business basics</h2>
<div class="field"><label>Business name *</label><input type="text" name="business_name" required></div>
<div class="field"><label>Trade *</label><input type="text" name="trade" required></div>
<div class="field"><label>Town *</label><input type="text" name="location" required></div>
<div class="field"><label>Coverage area</label><input type="text" name="coverage_area"></div>
<div class="field"><label>Phone</label><input type="tel" name="phone"></div>
<div class="field"><label>Email *</label><input type="email" name="email" required></div>
</div>

<div class="section">
<h2>Logo &amp; photos</h2>
<div class="field"><label>Logo</label><input type="file" name="logo" accept="image/*"></div>
<div class="field"><label>Portfolio photos</label><input type="file" name="photos" accept="image/*" multiple></div>
</div>

<div class="section">
<h2>Work split</h2>
<div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:8px;">
  <div style="font-size:34px;font-weight:800;letter-spacing:-.03em;" id="com-pct">50%</div>
  <div style="font-size:13.5px;font-weight:600;color:#5C5A56;">Commercial</div>
</div>
<input id="split-slider" type="range" min="0" max="100" step="5" value="50" name="commercial_split" oninput="gwSplitInput(this.value)">
<div style="display:flex;justify-content:space-between;margin-top:9px;font-size:12.5px;color:#807E79;"><span>100% domestic</span><span>50 / 50</span><span>100% commercial</span></div>
</div>

<div class="section">
<h2>Type of work</h2>
<div class="option-row" id="work-type-row"></div>
<input type="hidden" name="work_type" id="work_type" value="standard">
</div>

<div class="section">
<h2>Your team</h2>
<div class="option-row" id="team-size-row"></div>
<input type="hidden" name="team_size" id="team_size" value="sole">
<div style="font-size:14px;font-weight:600;color:#3A3A38;margin-bottom:8px;">Take on large commercial contracts?</div>
<div class="toggle-row">
  <button type="button" class="toggle-btn sel" id="lc-no" onclick="gwPick('large_contracts','no')">No</button>
  <button type="button" class="toggle-btn" id="lc-yes" onclick="gwPick('large_contracts','yes')">Yes</button>
</div>
<input type="hidden" name="large_contracts" id="large_contracts" value="no">
</div>

<div class="section">
<h2>Reaching you</h2>
<div class="option-row" id="booking-row"></div>
<input type="hidden" name="urgency" id="urgency" value="ahead">
</div>

<div class="section">
<h2>Extras</h2>
<div class="field"><label>Years trading / founded</label><input type="text" name="years_trading"></div>
<div class="field"><label>Accreditations</label><input type="text" name="accreditations"></div>
<div class="field"><label>Past clients / projects</label><input type="text" name="past_clients"></div>
<div class="field"><label>Notes</label><textarea name="notes" rows="3"></textarea></div>
</div>

<button type="submit" class="btn-submit">Generate test site</button>
</form>
</div>

<script>
const WORK_TYPES = [
  {key:'standard',label:'Mostly standard / routine jobs',desc:'Repairs, installs and everyday work — the bread and butter.'},
  {key:'mix',label:'A mix of routine and specialist',desc:'Routine work plus the occasional bigger or one-off project.'},
  {key:'bespoke',label:'Mostly bespoke, listed or specialist work',desc:'Heritage, conservation, high-end or one-of-a-kind jobs.'},
];
const TEAM_SIZES = [
  {key:'sole',label:'Just me',desc:'A sole trader, out on the tools day to day.'},
  {key:'small',label:'Small team',desc:'A handful of us, hands-on day to day.'},
  {key:'company',label:'Established company',desc:'A settled team with office and field staff.'},
];
const BOOKINGS = [
  {key:'ahead',label:'Customers usually book ahead',desc:'Planned jobs, quotes and scheduled work.'},
  {key:'emergency',label:'Often same-day or emergency',desc:'Callouts, urgent repairs, fast response matters.'},
];
const DOT_SVG = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none"><path d="M5 13l4 4L19 7" stroke="#fff" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/></svg>';

function renderOptionRow(containerId, hiddenId, options, selected) {
  const el = document.getElementById(containerId);
  el.innerHTML = options.map(o => `
    <button type="button" class="option-card${o.key===selected?' sel':''}" onclick="gwPick('${hiddenId}','${o.key}')">
      <span class="option-dot">${DOT_SVG}</span>
      <span><span class="option-label">${o.label}</span><span class="option-desc">${o.desc}</span></span>
    </button>`).join('');
}

function gwPick(hiddenId, key) {
  document.getElementById(hiddenId).value = key;
  if (hiddenId === 'work_type') renderOptionRow('work-type-row', 'work_type', WORK_TYPES, key);
  if (hiddenId === 'team_size') renderOptionRow('team-size-row', 'team_size', TEAM_SIZES, key);
  if (hiddenId === 'urgency') renderOptionRow('booking-row', 'urgency', BOOKINGS, key);
  if (hiddenId === 'large_contracts') {
    document.getElementById('lc-no').classList.toggle('sel', key === 'no');
    document.getElementById('lc-yes').classList.toggle('sel', key === 'yes');
  }
}

function gwSplitInput(v) {
  document.getElementById('com-pct').textContent = v + '%';
}

renderOptionRow('work-type-row', 'work_type', WORK_TYPES, 'standard');
renderOptionRow('team-size-row', 'team_size', TEAM_SIZES, 'sole');
renderOptionRow('booking-row', 'urgency', BOOKINGS, 'ahead');
</script>"""


@app.route("/admin/generate-test", methods=["GET", "POST"])
@admin_required
def admin_generate_test():
    if request.method == "GET":
        return render_template_string(_admin_page("Generate test site", _admin_test_form_page(), active="generations"))

    # POST — build and kick off a generation immediately, bypassing verification
    # and the repeat-generation block. Admin-only route; never exposed publicly.
    form = request.form
    email = (form.get("email") or "").strip().lower()
    if not email:
        return jsonify({"error": "email required"}), 400

    db = SessionLocal()
    try:
        lead = Lead(
            public_id=uuid.uuid4().hex[:10],
            email=email,
            ip=_client_ip(),
            status="verified",
            form_data={},
            is_test=True,
        )
        db.add(lead)
        db.flush()

        job_dir = os.path.join(UPLOAD_DIR, lead.public_id)
        os.makedirs(job_dir, exist_ok=True)

        logo_file = request.files.get("logo")
        logo_path, logo_mime = None, None
        if logo_file and logo_file.filename:
            ext = os.path.splitext(logo_file.filename)[1] or ".png"
            fname = f"logo{ext}"
            logo_file.save(os.path.join(job_dir, fname))
            logo_path = fname
            logo_mime = logo_file.content_type or "image/png"

        for i, pf in enumerate(request.files.getlist("photos")):
            if pf and pf.filename:
                ext = os.path.splitext(pf.filename)[1] or ".jpg"
                pf.save(os.path.join(job_dir, f"photo_{i}{ext}"))

        has_photos = any(fname.startswith("photo_") for fname in os.listdir(job_dir))

        lead.form_data = _map_form(form, logo_path, has_photos)
        lead.logo_path = logo_path
        lead.logo_mime = logo_mime
        db.commit()

        _kickoff_generation(lead)

        return redirect(f"/admin/wait/{lead.public_id}")
    finally:
        db.close()


@app.route("/admin/wait/<public_id>")
@admin_required
def admin_wait(public_id):
    """
    Admin-only equivalent of frontend/loading.html — polls until the
    generation is done, then goes straight to the same live-preview route
    (/api/generate/<public_id>/html) that real signups land on, so admin
    test sites get the same watermark bar, Go-live link, and Edit button.
    """
    content = f"""<div style="max-width:640px;margin:0 auto;text-align:center;padding:40px 0;">
<h1 class="adm-title">Generating…</h1>
<p class="muted" id="status-msg">Building the test site — this usually takes under 3 minutes.</p>
</div>
<script>
async function poll() {{
  try {{
    const r = await fetch('/admin/generate-test/status/{public_id}');
    const data = await r.json();
    if (data.status === 'done' && data.gen_id) {{
      window.location.href = '/api/generate/{public_id}/html?new=1';
      return;
    }}
    if (data.status === 'error') {{
      document.getElementById('status-msg').textContent = 'Generation failed: ' + (data.error || 'unknown error');
      return;
    }}
  }} catch (e) {{}}
  setTimeout(poll, 2000);
}}
poll();
</script>"""
    return render_template_string(_admin_page("Generating…", content, active="generations"))


@app.route("/admin/generate-test/status/<public_id>")
@admin_required
def admin_generate_test_status(public_id):
    with _jobs_lock:
        job = _jobs.get(public_id)
    if job and job["status"] == "error":
        return jsonify({"status": "error", "error": job.get("error", "Unknown error")})

    db = SessionLocal()
    try:
        gen = db.query(Generation).join(Lead).filter(Lead.public_id == public_id).first()
        if gen:
            return jsonify({"status": "done", "gen_id": gen.id})
    finally:
        db.close()
    return jsonify({"status": "pending"})


@app.route("/admin/generations/<int:gen_id>/email", methods=["POST"])
@admin_required
def admin_update_generation_email(gen_id):
    """Update the destination email for a generation's contact form — used when
    the site was set up with a test/wrong email and needs correcting without
    regenerating the whole site."""
    new_email = (request.json or {}).get("email", "").strip().lower()
    if not new_email or "@" not in new_email:
        return jsonify({"error": "valid email required"}), 400
    db = SessionLocal()
    try:
        gen = db.get(Generation, gen_id)
        if not gen:
            return jsonify({"error": "not found"}), 404
        old_email = gen.email
        gen.email = new_email
        gen.lead.email = new_email
        db.commit()
        app.logger.info(f"Admin updated gen {gen_id} email: {old_email!r} → {new_email!r}")
        return jsonify({"ok": True, "old_email": old_email, "new_email": new_email})
    finally:
        db.close()


@app.route("/admin/generations/<int:gen_id>/approve")
@admin_required
def admin_approve_generation(gen_id):
    """One-click approval for an outreach magic-link generation (see the
    admin-approval gate in _run_and_persist, added 2026-07-24) — sends the
    customer's "your website is ready" email, which is otherwise withheld
    for outreach-originated generations until an admin reviews the
    preview. Idempotent: re-visiting an already-approved link is a no-op,
    since customer_notified_at being set is exactly what "already sent"
    means here.

    Approving also marks this generation's prompt_version_hash as approved
    (PromptApproval row, insert-if-absent) — every later generation sharing
    that same build_prompt.py hash then skips the gate entirely and
    notifies its customer immediately. Only an actual prompt change (a new
    hash with no PromptApproval row yet) re-arms review."""
    db = SessionLocal()
    try:
        gen = db.get(Generation, gen_id)
        if not gen:
            return "Generation not found.", 404
        if gen.customer_notified_at is not None:
            return render_template_string(_account_page(
                f'<div class="acct-card" style="text-align:center;"><h1 style="margin:0 0 10px;font-weight:800;font-size:22px;">Already sent</h1>'
                f'<p style="margin:0;color:#5C5A56;">{escape(gen.business_name or "This site")} was already approved and notified.</p></div>',
                "Already approved",
            ))
        job_id = gen.lead.public_id
        # Outreach magic-link generations must never link to preview.html —
        # that page is part of the direct-signup account funnel (checkout
        # upsell, account sign-in tie-in) and hangs on "Loading your
        # website..." forever for a prospect with no Account/session behind
        # them. _claim_generate_and_redirect itself always sends these
        # customers straight to /api/generate/<id>/html; this must match.
        is_outreach = db.query(Prospect).filter(Prospect.lead_id == gen.lead_id).first() is not None
        preview_url = f"{SITE_URL}/api/generate/{job_id}/html" if is_outreach else f"{SITE_URL}/preview.html?id={job_id}"
        send_site_ready_email(
            gen.email,
            gen.business_name,
            preview_url=preview_url,
            account_login_url=f"{SITE_URL}/account/login",
        )
        gen.customer_notified_at = datetime.utcnow()
        if gen.prompt_version_hash and db.get(PromptApproval, gen.prompt_version_hash) is None:
            db.add(PromptApproval(
                prompt_version_hash=gen.prompt_version_hash,
                approved_via_generation_id=gen.id,
            ))
        db.commit()
        return render_template_string(_account_page(
            f'<div class="acct-card" style="text-align:center;"><h1 style="margin:0 0 10px;font-weight:800;font-size:22px;">Approved &amp; sent</h1>'
            f'<p style="margin:0;color:#5C5A56;">{escape(gen.business_name or "This site")} — the customer has been emailed their preview link. '
            f'Future generations from this same prompt version will be sent automatically, without needing approval again.</p></div>',
            "Approved",
        ))
    finally:
        db.close()


@app.route("/admin/generations/<int:gen_id>/pending-changes")
@admin_required
def admin_pending_changes(gen_id):
    """Show a side-by-side diff of current live text vs customer's requested
    changes, with Apply and Discard actions."""
    db = SessionLocal()
    try:
        gen = db.get(Generation, gen_id)
        if not gen:
            return "Not found", 404
        if not gen.html_pending:
            return redirect(url_for("admin_generations"))

        live_fields = {f["id"]: f["content"] for f in _extract_gw_text_fields(gen.html_content or "")}
        pending_fields = {f["id"]: f["content"] for f in _extract_gw_text_fields(gen.html_pending or "")}

        # Only show fields that actually changed
        changed = []
        for fid, new_val in pending_fields.items():
            old_val = live_fields.get(fid, "")
            if new_val != old_val:
                changed.append({"id": fid, "old": old_val, "new": new_val})

        rows_html = ""
        if changed:
            for c in changed:
                label = c["id"].replace("-", " ").title()
                rows_html += (
                    '<tr>'
                    '<td style="width:180px;font-size:13px;color:#5C5A56;vertical-align:top;padding:14px 12px;">'
                    + escape(label) +
                    '</td>'
                    '<td style="vertical-align:top;padding:14px 12px;border-left:1px solid #E6E3DC;">'
                    '<div style="font-size:12px;font-weight:700;color:#9A9893;letter-spacing:.05em;text-transform:uppercase;margin-bottom:4px;">Current (live)</div>'
                    '<div style="font-size:14px;color:#1C1C1C;line-height:1.5;">' + escape(c["old"] or "(empty)") + '</div>'
                    '</td>'
                    '<td style="vertical-align:top;padding:14px 12px;border-left:1px solid #E6E3DC;background:#F0FDF4;">'
                    '<div style="font-size:12px;font-weight:700;color:#16A34A;letter-spacing:.05em;text-transform:uppercase;margin-bottom:4px;">Requested change</div>'
                    '<div style="font-size:14px;color:#1C1C1C;line-height:1.5;font-weight:500;">' + escape(c["new"]) + '</div>'
                    '</td>'
                    '</tr>'
                )
        else:
            rows_html = '<tr><td colspan="3" style="padding:24px;color:#807E79;font-size:14px;">No text differences detected — the content may be structurally identical.</td></tr>'

        biz = escape(gen.business_name or "Untitled")
        pub_id = gen.lead.public_id if gen.lead else ""
        content = f"""<style>
.diff-table{{width:100%;border-collapse:collapse;background:#fff;border:1px solid #E6E3DC;border-radius:10px;overflow:hidden;}}
.diff-table tr+tr{{border-top:1px solid #E6E3DC;}}
.action-bar{{display:flex;gap:12px;align-items:center;margin-top:24px;}}
.btn{{padding:11px 22px;border-radius:8px;font-size:14px;font-weight:700;border:none;cursor:pointer;text-decoration:none;display:inline-block;}}
.btn-green{{background:#16A34A;color:#fff;}}
.btn-red{{background:#fff;color:#B91C1C;border:1px solid #FECACA;}}
.btn:hover{{opacity:.88;}}
#status-msg{{font-size:14px;color:#5C5A56;}}
</style>
<p style="margin:0 0 6px;"><a href="/admin/generations" style="color:#807E79;font-size:13px;text-decoration:none;">← All generations</a></p>
<h1 class="adm-title" style="margin:0 0 4px;">{biz} — pending changes</h1>
<p class="adm-sub" style="margin:0 0 24px;">Customer: {escape(gen.email)} &nbsp;·&nbsp; {len(changed)} field(s) changed
{(' &nbsp;·&nbsp; <a href="/editor.html?id=' + pub_id + '" target="_blank" style="color:#2257CC;">Open in editor →</a>') if pub_id else ''}</p>

<table class="diff-table">
<thead><tr style="background:#F5F3EE;">
  <th style="text-align:left;padding:10px 12px;font-size:12px;letter-spacing:.06em;text-transform:uppercase;color:#9A9893;font-weight:700;">Field</th>
  <th style="text-align:left;padding:10px 12px;font-size:12px;letter-spacing:.06em;text-transform:uppercase;color:#9A9893;font-weight:700;border-left:1px solid #E6E3DC;">Current (live)</th>
  <th style="text-align:left;padding:10px 12px;font-size:12px;letter-spacing:.06em;text-transform:uppercase;color:#16A34A;font-weight:700;border-left:1px solid #E6E3DC;">Requested change</th>
</tr></thead>
<tbody>{rows_html}</tbody>
</table>

<div class="action-bar">
  <button class="btn btn-green" onclick="gwApply()">Apply now &amp; notify customer</button>
  <button class="btn btn-red" onclick="gwDiscard()">Discard pending changes</button>
  <span style="font-size:12.5px;color:#9A9893;">(applies automatically overnight otherwise)</span>
  <span id="status-msg"></span>
</div>

<script>
async function gwApply() {{
  if (!confirm('Apply all changes to the live site and email the customer?')) return;
  document.getElementById('status-msg').textContent = 'Applying…';
  try {{
    const r = await fetch('/admin/generations/{gen_id}/apply-pending', {{method:'POST',credentials:'same-origin'}});
    if (!r.ok) throw new Error(await r.text());
    document.getElementById('status-msg').textContent = '✓ Applied and customer notified.';
    document.querySelectorAll('.btn').forEach(b => b.disabled = true);
  }} catch(e) {{ document.getElementById('status-msg').textContent = 'Error: ' + e.message; }}
}}
async function gwDiscard() {{
  if (!confirm('Discard all pending changes? The customer will not be notified.')) return;
  document.getElementById('status-msg').textContent = 'Discarding…';
  try {{
    const r = await fetch('/admin/generations/{gen_id}/discard-pending', {{method:'POST',credentials:'same-origin'}});
    if (!r.ok) throw new Error(await r.text());
    window.location.href = '/admin/generations';
  }} catch(e) {{ document.getElementById('status-msg').textContent = 'Error: ' + e.message; }}
}}
</script>"""
        return render_template_string(_admin_page(f"Pending changes — {biz}", content, active="generations"))
    finally:
        db.close()


@app.route("/admin/generations/<int:gen_id>/apply-pending", methods=["POST"])
@admin_required
def admin_apply_pending(gen_id):
    """Promote html_pending → html_content for a live generation, then notify
    the customer that their requested changes are now live."""
    db = SessionLocal()
    try:
        gen = db.get(Generation, gen_id)
        if not gen:
            return jsonify({"error": "not found"}), 404
        if not gen.html_pending:
            return jsonify({"error": "No pending changes to apply"}), 400
        gen.html_content = gen.html_pending
        gen.html_pending = None
        db.commit()
        try:
            from emails import send_changes_live_email
            send_changes_live_email(gen.email, gen.business_name)
        except Exception:
            app.logger.exception(f"Failed to send changes-live email to {gen.email}")
        app.logger.info(f"Admin applied pending changes for gen {gen_id} ({gen.business_name!r})")
        return jsonify({"ok": True})
    finally:
        db.close()


def run_pending_edits_apply() -> None:
    """Nightly job: promote html_pending -> html_content for every live
    generation with pending text edits, then email the customer. Same
    promotion + notification logic as admin_apply_pending above, run
    unattended across all rows instead of one at a time by an admin click.

    Text edits are plain string substitutions already validated field-by-field
    at save time (_update_gw_text_field) — there's no judgement call to make
    here, so this applies them directly rather than routing through Claude.

    Needs to run on a real recurring schedule (a Railway Cron service pointed
    at apply_pending_edits_job.py) — there is no in-process scheduler in this
    codebase, so nothing calls this automatically on its own.
    """
    from emails import send_changes_live_email
    db = SessionLocal()
    try:
        pending_gens = (
            db.query(Generation)
            .filter(Generation.status == "live", Generation.html_pending.isnot(None))
            .all()
        )
        applied, failed = 0, 0
        for gen in pending_gens:
            try:
                gen.html_content = gen.html_pending
                gen.html_pending = None
                db.commit()
                try:
                    send_changes_live_email(gen.email, gen.business_name)
                except Exception:
                    app.logger.exception(f"Failed to send changes-live email to {gen.email}")
                applied += 1
            except Exception:
                db.rollback()
                app.logger.exception(f"Failed to apply pending edits for gen {gen.id} ({gen.business_name!r})")
                failed += 1
        app.logger.info(f"Nightly pending-edits job: applied {applied}, failed {failed}, of {len(pending_gens)} live generations with pending edits.")
    finally:
        db.close()


@app.route("/admin/generations/<int:gen_id>/discard-pending", methods=["POST"])
@admin_required
def admin_discard_pending(gen_id):
    """Clear html_pending without applying it — used when the requested changes
    should not go live (e.g. wrong content, spam, or admin will apply manually)."""
    db = SessionLocal()
    try:
        gen = db.get(Generation, gen_id)
        if not gen:
            return jsonify({"error": "not found"}), 404
        gen.html_pending = None
        db.commit()
        app.logger.info(f"Admin discarded pending changes for gen {gen_id} ({gen.business_name!r})")
        return jsonify({"ok": True})
    finally:
        db.close()


@app.route("/admin/accounts/<path:email>", methods=["DELETE"])
@admin_required
def admin_delete_account(email):
    """
    Wipes everything tied to this email — Account (login), every Lead, every
    Generation and GenerationImage row, and their upload directories — so the
    email is free to sign up and generate again as if it were brand new.
    """
    email = email.strip().lower()
    db = SessionLocal()
    try:
        leads = db.query(Lead).filter(Lead.email == email).all()
        lead_ids = [lead.id for lead in leads]

        if lead_ids:
            gen_ids = [
                row[0] for row in
                db.query(Generation.id).filter(Generation.lead_id.in_(lead_ids)).all()
            ]
            if gen_ids:
                db.query(GenerationImage).filter(GenerationImage.generation_id.in_(gen_ids)).delete(synchronize_session=False)
                db.query(Generation).filter(Generation.id.in_(gen_ids)).delete(synchronize_session=False)
            db.query(Lead).filter(Lead.id.in_(lead_ids)).delete(synchronize_session=False)

        db.query(Account).filter(Account.email == email).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()

    for lead in leads:
        shutil.rmtree(os.path.join(UPLOAD_DIR, lead.public_id), ignore_errors=True)

    return jsonify({"status": "deleted", "email": email})


@app.route("/admin/generations/<int:gen_id>", methods=["DELETE"])
@admin_required
def admin_delete_generation(gen_id):
    """
    Deletes exactly one Generation (+ its GenerationImage rows, + its own
    Lead if that Lead has no other Generations) — added because the
    existing per-account delete (admin_delete_account, above) deletes by
    EMAIL, which sweeps up every Generation sharing that address. Several
    real paying customers (e.g. Sussex Leadcraft Ltd's live site) share an
    email with leftover test/draft Generations created under the same
    address during development — using the per-email delete on one of
    those drafts would silently take the real live site down with it. This
    is the safe, surgical alternative: exactly one row, never touches
    anything else sharing that email.
    """
    db = SessionLocal()
    try:
        gen = db.get(Generation, gen_id)
        if not gen:
            return jsonify({"error": "not_found"}), 404

        lead_id = gen.lead_id
        db.query(GenerationImage).filter(GenerationImage.generation_id == gen_id).delete(synchronize_session=False)
        db.query(Generation).filter(Generation.id == gen_id).delete(synchronize_session=False)

        other_gens_on_lead = db.query(Generation).filter(Generation.lead_id == lead_id).count()
        lead_public_id = None
        if other_gens_on_lead == 0:
            lead = db.get(Lead, lead_id)
            if lead:
                lead_public_id = lead.public_id
            db.query(Lead).filter(Lead.id == lead_id).delete(synchronize_session=False)

        db.commit()
    finally:
        db.close()

    if lead_public_id:
        shutil.rmtree(os.path.join(UPLOAD_DIR, lead_public_id), ignore_errors=True)

    return jsonify({"status": "deleted", "gen_id": gen_id})


@app.route("/admin/generations/<int:gen_id>/html")
@admin_required
def admin_generation_html(gen_id):
    db = SessionLocal()
    try:
        gen = db.get(Generation, gen_id)
        if not gen:
            return "Not found", 404
        return gen.html_content, 200, {"Content-Type": "text/html; charset=utf-8"}
    finally:
        db.close()


@app.route("/admin/generations/<int:gen_id>/preview")
@admin_required
def admin_generation_preview(gen_id):
    """Admin-only mirror of exactly what the customer sees at
    /api/generate/<job_id>/html — same watermark bar, editor/checkout CTAs,
    the works — without ever touching the customer's own view stats.
    Added 2026-07-24: an admin previewing their own site through the real
    customer-facing link was inflating that customer's view_count,
    first_viewed_at/last_viewed_at, total_view_seconds and max_scroll_pct,
    making those numbers meaningless for judging real customer engagement.
    This route skips both _record_generation_view and the engagement
    beacon script entirely (track_engagement=False) — nothing here is
    reported anywhere. Linked from send_admin_approval_email instead of the
    public URL for exactly this reason."""
    db = SessionLocal()
    try:
        gen = db.get(Generation, gen_id)
        if not gen:
            return "Not found", 404
        job_id = gen.lead.public_id
        html = (gen.html_pending or gen.html_content) if gen.status == "live" else gen.html_content
        return _inject_watermark(html, job_id, track_engagement=False), 200, {"Content-Type": "text/html; charset=utf-8"}
    finally:
        db.close()


@app.route("/admin/generations/<int:gen_id>/form-data")
@admin_required
def admin_generation_form_data(gen_id):
    db = SessionLocal()
    try:
        gen = db.get(Generation, gen_id)
        if not gen:
            return jsonify({"error": "not found"}), 404
        return jsonify(gen.lead.form_data if gen.lead else {})
    finally:
        db.close()


@app.route("/admin/generations/<int:gen_id>/toggle-internal", methods=["POST"])
@admin_required
def admin_generation_toggle_internal(gen_id):
    """Flip Generation.is_internal from the prospect profile page's checkbox
    — the only setter for this flag until now; it existed on the model and
    was read by several KPIs (Generation -> Paid, domain conversion, churn)
    but had no UI to actually set it, so marking a self-paid test/demo
    generation meant a direct DB edit."""
    db = SessionLocal()
    try:
        gen = db.get(Generation, gen_id)
        if not gen:
            abort(404)
        gen.is_internal = request.form.get("is_internal") == "1"
        db.commit()
    finally:
        db.close()
    redirect_to = request.form.get("redirect_to") or "/admin/generations"
    if not redirect_to.startswith("/"):
        redirect_to = "/admin/generations"
    return redirect(redirect_to)


# ---------------------------------------------------------------------------
# Outreach prospect list — admin-only. Every prospect the pipeline has
# sourced is auto-eligible for sending once qualified/scored (Section 5a —
# approval_status is audit-only, not a send gate; see outreach/send_job.py's
# _eligible_initial_send_query docstring), so there's nothing to actually
# approve or reject here. This is a browse/filter view over Prospect, not a
# queue — /admin/prospects/<id> is the click-through profile page.
# ---------------------------------------------------------------------------
_OUTREACH_TRADE_TIERS = [("high", "High"), ("medium", "Medium"), ("low", "Low")]
_OUTREACH_INCOME_TIERS = [("high", "High"), ("medium", "Medium"), ("low", "Low")]
_OUTREACH_SOURCING_CHANNELS = list(SOURCING_CHANNEL_LABELS.items())
_OUTREACH_WEBSITE_STATUSES = [("no_website", "No website"), ("has_website", "Has website")]
_OUTREACH_WEBSITE_QUALITIES = [("modern", "Modern"), ("dated", "Dated")]
_OUTREACH_FUNNEL_STAGES = [
    ("sourced", "Sourced"), ("queued", "Queued"), ("gated", "Gated"),
    ("qualified_no_email", "Qualified (no email)"), ("awaiting_approval", "Awaiting approval"),
    ("approved", "Approved"), ("sent", "Sent"), ("rejected", "Rejected"),
    ("unreachable", "Unreachable"), ("excluded_closed", "Excluded (closed)"),
]
_OUTREACH_FUNNEL_SUBSTAGES = [
    ("sent", "Sent"), ("opened", "Opened"), ("clicked_generated", "Clicked/Generated"),
    ("account_created", "Account created"), ("replied", "Replied"),
    ("bounced", "Bounced"), ("cold", "Cold"),
]


@app.route("/admin/outreach")
@admin_required
def admin_outreach():
    """Filterable list of prospects — every metric the pipeline actually
    scores/tracks is a filter, and each row links straight to
    /admin/prospects/<id>, the same profile page outreach/send_daily_summary.py's
    click totals link back to. Replaces the old Tinder-style approve/pass card, which had no real
    effect on what gets sent (see module comment above)."""
    args = request.args

    def _arg(name):
        v = (args.get(name) or "").strip()
        return v or None

    trade_tier = _arg("trade_tier")
    income_tier = _arg("income_tier")
    sourcing_channel = _arg("sourcing_channel")
    website_status = _arg("website_status")
    website_quality = _arg("website_quality")
    funnel_stage = _arg("funnel_stage")
    funnel_substage = _arg("funnel_substage")
    trade = _arg("trade")
    location = _arg("location")
    email_filter = _arg("email")  # "yes" / "no" / None (any)
    phone_filter = _arg("phone")
    min_score = _arg("min_score")
    min_rating = _arg("min_rating")

    db = SessionLocal()
    try:
        q = db.query(Prospect)
        if trade_tier:
            q = q.filter(Prospect.trade_tier == trade_tier)
        if income_tier:
            q = q.filter(Prospect.income_tier == income_tier)
        if sourcing_channel:
            q = q.filter(Prospect.sourcing_channel == sourcing_channel)
        if website_status:
            q = q.filter(Prospect.website_status == website_status)
        if website_quality:
            q = q.filter(Prospect.website_quality == website_quality)
        if funnel_stage:
            q = q.filter(Prospect.funnel_stage == funnel_stage)
        if funnel_substage:
            q = q.filter(Prospect.funnel_substage == funnel_substage)
        if trade:
            q = q.filter(Prospect.trade.ilike(f"%{trade}%"))
        if location:
            q = q.filter(Prospect.location.ilike(f"%{location}%"))
        if email_filter == "yes":
            q = q.filter(Prospect.email.isnot(None), Prospect.email != "")
        elif email_filter == "no":
            q = q.filter((Prospect.email.is_(None)) | (Prospect.email == ""))
        if phone_filter == "yes":
            q = q.filter(Prospect.phone.isnot(None), Prospect.phone != "")
        elif phone_filter == "no":
            q = q.filter((Prospect.phone.is_(None)) | (Prospect.phone == ""))
        if min_score:
            try:
                q = q.filter(Prospect.score >= float(min_score))
            except ValueError:
                pass
        if min_rating:
            try:
                q = q.filter(Prospect.rating >= float(min_rating))
            except ValueError:
                pass

        total_matching = q.count()
        prospects = q.order_by(Prospect.score.desc().nullslast(), Prospect.created_at.desc()).limit(200).all()

        def opts(name, choices, current):
            html = '<option value="">Any</option>'
            for val, label in choices:
                sel = " selected" if current == val else ""
                html += f'<option value="{escape(val)}"{sel}>{escape(label)}</option>'
            return html

        def yn_opts(name, current):
            html = '<option value="">Any</option>'
            for val, label in [("yes", "Yes"), ("no", "No")]:
                sel = " selected" if current == val else ""
                html += f'<option value="{val}"{sel}>{label}</option>'
            return html

        rows_html = "".join(f"""
<tr onclick="window.location='/admin/prospects/{p.id}'" style="cursor:pointer;">
  <td style="padding:8px 10px;font-weight:600;">
    <a href="/admin/prospects/{p.id}" style="color:#2257CC;text-decoration:none;">{escape(p.business_name or "—")}</a>
  </td>
  <td style="padding:8px 10px;">{escape(p.trade or "—")}</td>
  <td style="padding:8px 10px;text-transform:capitalize;">{escape(p.trade_tier or "—")}</td>
  <td style="padding:8px 10px;">{escape(p.location or "—")}</td>
  <td style="padding:8px 10px;text-transform:capitalize;">{escape(p.income_tier or "—")}</td>
  <td style="padding:8px 10px;">{f"{p.rating:.1f} ({p.review_count or 0})" if p.rating is not None else "—"}</td>
  <td style="padding:8px 10px;">{escape(p.website_status or "—")}{f" ({escape(p.website_quality)})" if p.website_quality else ""}</td>
  <td style="padding:8px 10px;font-weight:700;">{round(p.score) if p.score is not None else "—"}</td>
  <td style="padding:8px 10px;">{escape(p.funnel_stage or "—")}{f" / {escape(p.funnel_substage)}" if p.funnel_substage else ""}</td>
  <td style="padding:8px 10px;">{"✓" if p.email else "—"}</td>
  <td style="padding:8px 10px;">{"✓" if p.phone else "—"}</td>
  <td style="padding:8px 10px;">{escape(SOURCING_CHANNEL_LABELS.get(p.sourcing_channel, p.sourcing_channel or "—"))}</td>
  <td style="padding:8px 10px;">{_fmt_dt(p.created_at) or "—"}</td>
</tr>""" for p in prospects)

        showing_note = (
            f"Showing top {len(prospects)} of {total_matching} matching prospect(s), by score."
            if total_matching > len(prospects)
            else f"{total_matching} matching prospect(s)."
        )

        content = f"""
<h1 class="adm-title">Outreach prospects</h1>
<p class="muted" style="font-size:12.5px;margin:0 0 12px;">Filter any tracked metric, click a row for the full profile. <a href="/admin/pipeline" style="color:#2257CC;">← Pipeline</a></p>

<form method="get" class="adm-card" style="padding:16px 20px;margin-bottom:18px;display:flex;gap:12px;flex-wrap:wrap;align-items:flex-end;">
  <div><label style="display:block;font-size:11px;color:#9A9893;margin-bottom:4px;">Sourcing channel</label>
    <select name="sourcing_channel">{opts("sourcing_channel", _OUTREACH_SOURCING_CHANNELS, sourcing_channel)}</select></div>
  <div><label style="display:block;font-size:11px;color:#9A9893;margin-bottom:4px;">Trade tier</label>
    <select name="trade_tier">{opts("trade_tier", _OUTREACH_TRADE_TIERS, trade_tier)}</select></div>
  <div><label style="display:block;font-size:11px;color:#9A9893;margin-bottom:4px;">Income tier (area)</label>
    <select name="income_tier">{opts("income_tier", _OUTREACH_INCOME_TIERS, income_tier)}</select></div>
  <div><label style="display:block;font-size:11px;color:#9A9893;margin-bottom:4px;">Website</label>
    <select name="website_status">{opts("website_status", _OUTREACH_WEBSITE_STATUSES, website_status)}</select></div>
  <div><label style="display:block;font-size:11px;color:#9A9893;margin-bottom:4px;">Website quality</label>
    <select name="website_quality">{opts("website_quality", _OUTREACH_WEBSITE_QUALITIES, website_quality)}</select></div>
  <div><label style="display:block;font-size:11px;color:#9A9893;margin-bottom:4px;">Funnel stage</label>
    <select name="funnel_stage">{opts("funnel_stage", _OUTREACH_FUNNEL_STAGES, funnel_stage)}</select></div>
  <div><label style="display:block;font-size:11px;color:#9A9893;margin-bottom:4px;">Funnel substage</label>
    <select name="funnel_substage">{opts("funnel_substage", _OUTREACH_FUNNEL_SUBSTAGES, funnel_substage)}</select></div>
  <div><label style="display:block;font-size:11px;color:#9A9893;margin-bottom:4px;">Has email</label>
    <select name="email">{yn_opts("email", email_filter)}</select></div>
  <div><label style="display:block;font-size:11px;color:#9A9893;margin-bottom:4px;">Has phone</label>
    <select name="phone">{yn_opts("phone", phone_filter)}</select></div>
  <div><label style="display:block;font-size:11px;color:#9A9893;margin-bottom:4px;">Trade contains</label>
    <input type="text" name="trade" value="{escape(trade or '')}" style="padding:6px 8px;border:1px solid #D8D5CE;border-radius:6px;"></div>
  <div><label style="display:block;font-size:11px;color:#9A9893;margin-bottom:4px;">Location contains</label>
    <input type="text" name="location" value="{escape(location or '')}" style="padding:6px 8px;border:1px solid #D8D5CE;border-radius:6px;"></div>
  <div><label style="display:block;font-size:11px;color:#9A9893;margin-bottom:4px;">Min score</label>
    <input type="number" name="min_score" value="{escape(min_score or '')}" min="0" max="100" style="width:70px;padding:6px 8px;border:1px solid #D8D5CE;border-radius:6px;"></div>
  <div><label style="display:block;font-size:11px;color:#9A9893;margin-bottom:4px;">Min rating</label>
    <input type="number" name="min_rating" value="{escape(min_rating or '')}" min="0" max="5" step="0.1" style="width:70px;padding:6px 8px;border:1px solid #D8D5CE;border-radius:6px;"></div>
  <button type="submit" style="padding:8px 18px;background:#1C1C1C;color:#fff;border:none;border-radius:6px;font-weight:700;cursor:pointer;">Filter</button>
  <a href="/admin/outreach" style="padding:8px 12px;color:#5C5A56;text-decoration:none;font-size:13px;">Clear</a>
</form>

<p class="adm-sub">{showing_note}</p>

<div class="adm-card" style="overflow-x:auto;">
<table style="width:100%;border-collapse:collapse;font-size:13.5px;">
<thead><tr style="color:#9A9893;font-size:11px;text-transform:uppercase;border-bottom:1px solid #E6E3DC;">
  <th style="text-align:left;padding:6px 10px;">Business</th>
  <th style="text-align:left;padding:6px 10px;">Trade</th>
  <th style="text-align:left;padding:6px 10px;">Tier</th>
  <th style="text-align:left;padding:6px 10px;">Location</th>
  <th style="text-align:left;padding:6px 10px;">Income tier</th>
  <th style="text-align:left;padding:6px 10px;">Rating</th>
  <th style="text-align:left;padding:6px 10px;">Website</th>
  <th style="text-align:left;padding:6px 10px;">Score</th>
  <th style="text-align:left;padding:6px 10px;">Funnel</th>
  <th style="text-align:left;padding:6px 10px;">Email</th>
  <th style="text-align:left;padding:6px 10px;">Phone</th>
  <th style="text-align:left;padding:6px 10px;">Source</th>
  <th style="text-align:left;padding:6px 10px;">Sourced</th>
</tr></thead>
<tbody>{rows_html or '<tr><td colspan="13" style="padding:16px 10px;color:#9A9893;">No prospects match these filters.</td></tr>'}</tbody>
</table>
</div>
"""
        return render_template_string(_admin_page("Outreach prospects", content, active="outreach"))
    finally:
        db.close()


@app.route("/admin/facebook-outreach")
@admin_required
def admin_facebook_outreach_redirect():
    """Old URL, kept as a redirect (renamed to /admin/socials-outreach
    2026-07-27, by request — "Facebook" on the nav/Pipeline page was
    conflating the outreach-method queue with a sourcing-channel concept;
    see outreach/sourcing_channels.py for the actual split). Any existing
    bookmark/link to the old path still works."""
    return redirect("/admin/socials-outreach")


@app.route("/admin/socials-outreach")
@admin_required
def admin_facebook_outreach():
    """Manual Facebook DM outreach queue (added 2026-07-24) — no_website
    prospects with a captured Facebook Page URL (see Prospect.facebook_page_url's
    docstring: found during the same nightly email-discovery search that
    already runs site:facebook.com queries) who haven't had a DM logged as
    sent or dismissed yet, AND don't already have a usable email on file.

    Briefly widened to all website statuses on 2026-07-25, then reverted
    the same day per an explicit policy call: SMS and Facebook are reserved
    for no_website prospects only — a prospect with a website is reachable
    by email, the primary channel that already carries enough volume, and
    shouldn't also get a supplementary Facebook DM. See
    outreach/sms.py's sms_channel_eligible() for the identical policy
    applied to SMS sends (both initial and follow-up).

    Fixed 2026-07-26: the email_found exclusion below was missing entirely —
    a no_website prospect with a genuinely corroborated email (e.g. found
    published on their own Facebook Page) is already reachable by the fully
    automated email channel (send_job.py's phone_only = not p.email_found
    check doesn't care about website_status), so showing them here too was
    the exact same "already reachable by email, don't also DM" mistake the
    docstring above already argues against for has_website prospects — it
    just wasn't applied to this case. Caught after the user manually sent 7
    DMs to prospects who already had an email captured on file.

    Deliberately NOT automated — no Messenger API, no browser automation
    driving a logged-in Facebook session. This page's only job is to make
    the manual find→copy→paste→send→log loop fast, on a phone as easily as
    a desktop: open the Page, copy a pre-filled message with their real
    magic link merged in, send it yourself inside Facebook, then log it
    here so it feeds the same funnel/touch tracking as email and SMS.
    Automating the actual send would risk the Facebook account, which
    isn't a tradeoff worth making for this channel."""
    db = SessionLocal()
    try:
        prospects = (
            db.query(Prospect)
            .filter(
                Prospect.website_status == "no_website",
                Prospect.facebook_page_url.isnot(None),
                Prospect.facebook_page_url != "",
                Prospect.facebook_dm_sent_at.is_(None),
                Prospect.facebook_dm_dismissed_at.is_(None),
                Prospect.email_found.isnot(True),
            )
            .order_by(Prospect.score.desc().nullslast())
            .limit(200)
            .all()
        )

        # Every prospect shown here needs a working magic link — generate
        # one now (ensure_link_identity is idempotent/a no-op if already
        # set) rather than requiring a send via another channel first,
        # since Facebook may be the only channel that ever reaches some of
        # these prospects.
        touched = False
        for p in prospects:
            if not p.token or not p.short_code:
                ensure_link_identity(db, p)
                touched = True
        if touched:
            db.commit()

        cards_html = ""
        for p in prospects:
            message = render_facebook_dm(business_name=p.business_name or "there", short_code=p.short_code)
            fb_url = escape(p.facebook_page_url)
            biz = escape(p.business_name or "—")
            trade = escape(p.trade or "—")
            location = escape(p.location or "—")
            website_badge = (
                '<span class="status-pill" style="background:#9A989322;color:#5C5A56;">has website</span>'
                if p.website_status == "has_website" else
                '<span class="status-pill" style="background:#3B82F622;color:#3B82F6;">no website</span>'
            )
            cards_html += f"""
<div class="adm-card fb-card">
  <div class="fb-card-head">
    <div>
      <a href="/admin/prospects/{p.id}" style="font-weight:700;font-size:14.5px;">{biz}</a>
      <div class="muted" style="font-size:12px;margin-top:2px;">{trade} · {location}</div>
    </div>
    <div style="display:flex;align-items:center;gap:8px;">
      {website_badge}
      <form method="post" action="/admin/prospects/{p.id}/facebook-dm-dismissed" onsubmit="return confirm('Remove this prospect from the Facebook queue? (Link invalid / not a real match — this does not delete the prospect.)');">
        <button type="submit" class="fb-x-btn" title="Remove — link invalid or not a match" aria-label="Remove from queue">✕</button>
      </form>
    </div>
  </div>
  <a href="{fb_url}" target="_blank" rel="noopener" class="fb-open-link">Open Facebook Page →</a>
  <textarea readonly id="msg-{p.id}" class="fb-msg">{escape(message)}</textarea>
  <div class="fb-card-actions">
    <button type="button" onclick="gwCopyMsg({p.id})" class="fb-copy-btn">Copy message</button>
    <form method="post" action="/admin/prospects/{p.id}/facebook-dm-sent" onsubmit="return confirm('Confirm you\\'ve actually sent this DM inside Facebook?');">
      <button type="submit" class="fb-sent-btn">Mark as sent</button>
    </form>
  </div>
  <form method="post" action="/admin/prospects/{p.id}/facebook-email-found" class="fb-email-form">
    <input type="email" name="email" placeholder="Saw an email on the page? Paste it here" class="fb-email-input" required>
    <button type="submit" class="fb-email-btn">Use email instead</button>
  </form>
  <form method="post" action="/admin/prospects/{p.id}/facebook-website-found" class="fb-email-form">
    <input type="text" name="website" placeholder="Actually has a website? Paste the URL" class="fb-email-input" required>
    <button type="submit" class="fb-email-btn">Website found</button>
  </form>
</div>"""

        content = f"""
<style>
.fb-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:14px;}}
.fb-card{{padding:14px;display:flex;flex-direction:column;gap:10px;}}
.fb-card-head{{display:flex;justify-content:space-between;align-items:flex-start;gap:8px;}}
.fb-x-btn{{background:#F5F3EE;color:#9A9893;border:1px solid #E2E0DA;border-radius:8px;width:32px;height:32px;font-size:15px;line-height:1;cursor:pointer;flex-shrink:0;}}
.fb-x-btn:hover{{background:#FEE2E2;color:#DC2626;border-color:#FCA5A5;}}
.fb-open-link{{font-size:13px;font-weight:600;}}
.fb-msg{{width:100%;min-height:100px;font-size:13px;font-family:inherit;padding:10px;border:1px solid #E2E0DA;border-radius:8px;resize:vertical;box-sizing:border-box;}}
.fb-card-actions{{display:flex;gap:8px;flex-wrap:wrap;}}
.fb-copy-btn{{background:#3B82F6;color:#fff;border:0;border-radius:8px;padding:10px 16px;font-size:13.5px;font-weight:600;cursor:pointer;flex:1;min-width:120px;}}
.fb-sent-btn{{background:#1C1C1C;color:#fff;border:0;border-radius:8px;padding:10px 16px;font-size:13.5px;font-weight:600;cursor:pointer;width:100%;}}
.fb-email-form{{display:flex;gap:8px;border-top:1px solid #E2E0DA;padding-top:10px;}}
.fb-email-input{{flex:1;min-width:0;font-size:13px;padding:9px 10px;border:1px solid #E2E0DA;border-radius:8px;box-sizing:border-box;}}
.fb-email-btn{{background:#fff;color:#1C1C1C;border:1px solid #1C1C1C;border-radius:8px;padding:9px 14px;font-size:13px;font-weight:600;cursor:pointer;white-space:nowrap;}}
@media (max-width:480px){{.fb-grid{{grid-template-columns:1fr;}}}}
</style>
<h1 class="adm-title">Socials outreach</h1>
<p class="adm-sub">{len(prospects)} no_website prospect(s) with a Facebook Page found and no DM logged/dismissed yet. Sending is manual by design — this page just makes find → copy → paste → send → log fast, from a phone or a desktop. If you can see a real email on the page yourself (Facebook blocks our automated tools from reading it, but not you), paste it in instead of sending a DM — it'll go through the normal automated email channel from then on. <a href="/admin/pipeline">← Back to Pipeline</a></p>
{f'<p style="background:#FEE2E2;color:#991B1B;padding:10px 14px;border-radius:8px;font-size:13.5px;margin-bottom:14px;">{escape(request.args.get("email_error"))}</p>' if request.args.get("email_error") else ""}
<div class="fb-grid">{cards_html or '<p class="muted" style="padding:16px 10px;">Nothing in the queue right now.</p>'}</div>
<script>
function gwCopyMsg(id) {{
  var el = document.getElementById('msg-' + id);
  el.select();
  navigator.clipboard && navigator.clipboard.writeText(el.value);
}}
</script>
"""
        return render_template_string(_admin_page("Socials outreach", content, active="pipeline"))
    finally:
        db.close()


@app.route("/admin/prospects/<int:prospect_id>/facebook-dm-sent", methods=["POST"])
@admin_required
def admin_mark_facebook_dm_sent(prospect_id):
    """Logs a manually-sent Facebook DM the same way every other outreach
    channel is tracked — a real OutreachTouch row (channel="facebook") plus
    the same generic touch bookkeeping (touch_count/last_touch_at) email
    and SMS sends already update, so Facebook can eventually be compared
    against them in the funnel. Only advances funnel_stage/funnel_substage
    to "sent" if this prospect hasn't been sent anything yet — a prospect
    already further along (e.g. already clicked their link via another
    channel) must not be regressed backward by a supplementary DM."""
    db = SessionLocal()
    try:
        p = db.get(Prospect, prospect_id)
        if not p:
            return jsonify({"error": "not found"}), 404

        now = datetime.utcnow()
        p.facebook_dm_sent_at = now
        db.add(OutreachTouch(prospect_id=p.id, stage="initial", channel="facebook", sent_at=now))

        PRE_SEND_STAGES = {"sourced", "gated", "excluded_closed", "queued", "awaiting_approval", "qualified_no_email", "unreachable"}
        if p.funnel_stage in PRE_SEND_STAGES:
            p.funnel_stage = "sent"
            p.funnel_substage = "sent"
            if not p.sent_at:
                p.sent_at = now

        p.last_touch_at = now
        p.touch_count = (p.touch_count or 0) + 1
        db.commit()

        app.logger.info(f"Facebook DM marked sent: prospect {prospect_id} ({p.business_name})")
        return redirect("/admin/socials-outreach")
    finally:
        db.close()


@app.route("/admin/prospects/<int:prospect_id>/facebook-dm-dismissed", methods=["POST"])
@admin_required
def admin_dismiss_facebook_dm(prospect_id):
    """Removes a prospect from the Facebook DM queue without sending
    anything — for a captured facebook_page_url that turns out to be
    invalid, a mismatch, or otherwise not usable (added 2026-07-25).
    Doesn't touch facebook_page_url itself or any other prospect field —
    purely excludes it from future /admin/facebook-outreach listings, and
    leaves a record of why (as opposed to silently deleting the prospect
    or clearing the URL, which would lose the fact that a match was ever
    found)."""
    db = SessionLocal()
    try:
        p = db.get(Prospect, prospect_id)
        if not p:
            return jsonify({"error": "not found"}), 404

        p.facebook_dm_dismissed_at = datetime.utcnow()
        db.commit()

        app.logger.info(f"Facebook DM dismissed: prospect {prospect_id} ({p.business_name})")
        return redirect("/admin/socials-outreach")
    finally:
        db.close()


@app.route("/admin/prospects/<int:prospect_id>/facebook-email-found", methods=["POST"])
@admin_required
def admin_facebook_email_found(prospect_id):
    """Lets an admin paste an email they spotted by eye on a Facebook Page
    (added 2026-07-26) — the one path around Facebook's login wall that
    blocks WebFetch from ever reading real Page content (confirmed via a
    real test: an unauthenticated fetch gets served Facebook's logged-out
    login screen, not the page). A human already logged into Facebook in
    their own browser sees the real thing.

    Reuses is_valid_email/has_deliverable_domain and _outreach_finalize
    from the CCR routines' /api/admin/outreach/g/apply-email path, but
    deliberately skips looks_like_guess — that heuristic exists to catch
    an LLM hallucinating a plausible-looking address it never actually
    saw published anywhere. It doesn't apply here: a human admin is
    reporting an address they personally read off the real page, which
    is exactly the kind of direct sighting the heuristic exists to
    approximate for an automated agent that can't do that. Real business
    emails routinely follow predictable patterns (enquiries@/info@ +
    business name) precisely because that's how businesses set them up
    — flagging that pattern here would reject genuine sightings, not
    guesses. Format/deliverability checks still apply since a fat-
    fingered email is possible even when typing something you can see.
    Setting email_found=True also removes this prospect from the
    Facebook DM queue on the next page load (admin_facebook_outreach's
    filter already excludes email_found=True), so it becomes a normal
    automated email send instead of a manual DM."""
    from outreach.email_discovery import is_valid_email, clean_email
    from outreach.email_verify import has_deliverable_domain

    db = SessionLocal()
    try:
        p = db.get(Prospect, prospect_id)
        if not p:
            return jsonify({"error": "not found"}), 404

        email = clean_email((request.form.get("email") or "").strip())
        if not email:
            return redirect("/admin/socials-outreach")

        error = None
        if not is_valid_email(email):
            error = f"'{email}' isn't a valid email address."
        elif not has_deliverable_domain(email):
            error = f"'{email}' has no MX or mail record — it would hard-bounce."

        if error:
            return redirect(f"/admin/socials-outreach?email_error={quote(error)}&pid={p.id}")

        p.email = email
        p.email_source = "facebook_page_manual"
        p.email_found = True
        db.query(PendingEmailDiscovery).filter(PendingEmailDiscovery.prospect_id == prospect_id).delete()
        _outreach_finalize(db, p)
        _log_prospect_event(db, p.id, "email_found_manual", channel="facebook", meta={"source": "facebook_page_manual"})
        db.commit()

        app.logger.info(f"Facebook Page email captured manually: prospect {prospect_id} ({p.business_name}) -> {email}")
        return redirect("/admin/socials-outreach")
    finally:
        db.close()


@app.route("/admin/prospects/<int:prospect_id>/facebook-website-found", methods=["POST"])
@admin_required
def admin_facebook_website_found(prospect_id):
    """Lets an admin flag that a Facebook-sourced "no_website" prospect
    actually has a real website — visible to a human on the Page (a
    link, a mention, a Linktree pointing to it) that the sourcing
    routine's WebSearch-only verification missed. Two purposes (both by
    request, 2026-07-26):

    1. Correct the record: website_status flips to has_website, which
       automatically removes this prospect from the Facebook DM queue
       (admin_facebook_outreach's filter already requires
       website_status == "no_website") and from SMS eligibility
       (sms_channel_eligible, same field) — matching the existing policy
       that a has_website prospect is an email-only prospect.
    2. Build a dataset for pattern-spotting: every correction logs a
       ProspectEvent (event_type="website_found_manual") with both the
       Facebook Page URL and the discovered website in its meta field.
       Nothing analyzes this automatically yet — the point is to
       accumulate real corrections so a future pass (human or an agent)
       can look for what these misses have in common (a Linktree URL
       pattern, a website mentioned in a photo caption rather than the
       About field, etc.) and improve the sourcing routine's own
       verification step accordingly, rather than guessing blind.

    Also queues a fresh PendingEmailDiscovery row if this prospect still
    has no email — a real website we didn't know about is a genuine new
    Tier-1 scrape target (outreach/email_discovery_job.py), not just a
    data point to file away."""
    db = SessionLocal()
    try:
        p = db.get(Prospect, prospect_id)
        if not p:
            return jsonify({"error": "not found"}), 404

        website = (request.form.get("website") or "").strip()
        if not website:
            return redirect("/admin/socials-outreach")
        if not website.lower().startswith(("http://", "https://")):
            website = "https://" + website
        parsed = _urlparse(website)
        if not parsed.netloc or "." not in parsed.netloc:
            error = f"'{website}' doesn't look like a real website URL."
            return redirect(f"/admin/socials-outreach?email_error={quote(error)}&pid={p.id}")

        old_website = p.website
        p.website = website
        p.website_status = "has_website"
        _log_prospect_event(db, p.id, "website_found_manual", channel="facebook", meta={
            "facebook_page_url": p.facebook_page_url,
            "website": website,
            "previous_website": old_website,
            "previous_website_status": "no_website",
        })

        if not p.email_found:
            existing_pending = db.query(PendingEmailDiscovery).filter(
                PendingEmailDiscovery.prospect_id == prospect_id).first()
            if not existing_pending:
                db.add(PendingEmailDiscovery(prospect_id=prospect_id))

        db.commit()

        app.logger.info(f"Facebook Page website corrected manually: prospect {prospect_id} ({p.business_name}) -> {website}")
        return redirect("/admin/socials-outreach")
    finally:
        db.close()


@app.route("/admin/survey-responses")
@admin_required
def admin_survey_responses():
    """Read-only view of every SurveyResponse row — both entry points
    write into this one table (the full 6-question /survey/<token> form
    and the one-click /claim/<token>/why buttons, added 2026-07-26 — see
    _apply_survey_answer_effects), so this page is genuinely the
    complete picture of "why didn't this prospect convert," not just one
    channel's slice of it. Didn't exist before this page — the data was
    accumulating with nowhere to actually look at it."""
    db = SessionLocal()
    try:
        responses = (
            db.query(SurveyResponse)
            .join(Prospect, Prospect.id == SurveyResponse.prospect_id)
            .order_by(SurveyResponse.id.desc())
            .limit(300)
            .all()
        )
        prospects_by_id = {p.id: p for p in db.query(Prospect).filter(
            Prospect.id.in_([r.prospect_id for r in responses])).all()} if responses else {}

        reason_counts = {}
        for r in responses:
            reason_counts[r.primary_reason] = reason_counts.get(r.primary_reason, 0) + 1
        summary_html = "".join(
            f'<span class="status-pill" style="background:#3B82F622;color:#3B82F6;margin:0 6px 6px 0;">{escape(reason)}: {count}</span>'
            for reason, count in sorted(reason_counts.items(), key=lambda x: -x[1])
        )

        rows_html = ""
        for r in responses:
            p = prospects_by_id.get(r.prospect_id)
            biz = escape(p.business_name or "—") if p else "—"
            detail = escape((r.reason_detail or "")[:200])
            trial = f'{p.trial_days_earned}d trial' if p and p.trial_days_earned else "—"
            rows_html += f"""<tr>
  <td>{r.id}</td>
  <td><a href="/admin/prospects/{r.prospect_id}">{biz}</a></td>
  <td>{escape(r.primary_reason or "—")}</td>
  <td style="max-width:280px;">{detail}</td>
  <td>{trial}</td>
</tr>"""

        content = f"""
<h1 class="adm-title">Survey responses</h1>
<p class="adm-sub">Every answer from both the full 6-question form and the one-click "why not yet" question — {len(responses)} total (most recent 300). <a href="/admin/pipeline">← Back to Pipeline</a></p>
<div style="margin:0 0 20px;">{summary_html or '<span class="muted">No responses yet.</span>'}</div>
<div class="adm-card" style="overflow-x:auto;">
<table>
<thead><tr><th>ID</th><th>Business</th><th>Reason</th><th>Detail</th><th>Trial earned</th></tr></thead>
<tbody>{rows_html or '<tr><td colspan="5" class="muted" style="padding:16px;">No survey responses yet.</td></tr>'}</tbody>
</table>
</div>
"""
        return render_template_string(_admin_page("Survey responses", content, active="pipeline"))
    finally:
        db.close()


@app.route("/admin/pregen-survey-responses")
@admin_required
def admin_pregen_survey_responses():
    """Read-only view of every PreGenSurveyResponse row (added 2026-07-27)
    — the pre-generation survey shown to the has_website/google_places
    cohort instead of firing generation immediately (see
    _needs_pregen_survey_gate). This is the answer to "where do I find
    these answers": every submission lands here, most recent first, plus
    the same breakdown on each individual prospect's profile page
    (/admin/prospects/<id>)."""
    db = SessionLocal()
    try:
        responses = (
            db.query(PreGenSurveyResponse)
            .order_by(PreGenSurveyResponse.id.desc())
            .limit(300)
            .all()
        )
        prospects_by_id = {p.id: p for p in db.query(Prospect).filter(
            Prospect.id.in_([r.prospect_id for r in responses])).all()} if responses else {}

        # Per-question, per-option counts across every response shown —
        # the quick "what are people actually saying" summary, same spirit
        # as the reason_counts pill strip on /admin/survey-responses.
        option_counts = {q["key"]: {} for q in _PREGEN_SURVEY_QUESTIONS}
        for r in responses:
            for q in _PREGEN_SURVEY_QUESTIONS:
                entry = (r.answers or {}).get(q["key"]) or {}
                for label in (entry.get("picked") or []):
                    if label == "Other":
                        continue
                    option_counts[q["key"]][label] = option_counts[q["key"]].get(label, 0) + 1

        summary_sections = ""
        for q in _PREGEN_SURVEY_QUESTIONS:
            counts = option_counts[q["key"]]
            if not counts:
                continue
            pills = "".join(
                f'<span class="status-pill" style="background:#3B82F622;color:#3B82F6;margin:0 6px 6px 0;">{escape(label)}: {n}</span>'
                for label, n in sorted(counts.items(), key=lambda x: -x[1])
            )
            summary_sections += (
                f'<p style="font-size:12.5px;font-weight:700;color:#5C5A56;margin:12px 0 6px;">{escape(q["q"])}</p>'
                f'<div>{pills}</div>'
            )

        rows_html = ""
        for r in responses:
            p = prospects_by_id.get(r.prospect_id)
            biz = escape(p.business_name or "—") if p else "—"
            answer_bits = []
            for q in _PREGEN_SURVEY_QUESTIONS:
                entry = (r.answers or {}).get(q["key"]) or {}
                picked = [v for v in (entry.get("picked") or []) if v != "Other"]
                if entry.get("other"):
                    picked = picked + [f'"{entry["other"]}"']
                if picked:
                    answer_bits.append(f'<b>{escape(q["key"])}:</b> {escape(", ".join(picked))}')
            answers_summary = "<br>".join(answer_bits) or "—"
            rows_html += f"""<tr>
  <td>{r.id}</td>
  <td><a href="/admin/prospects/{r.prospect_id}">{biz}</a></td>
  <td style="max-width:420px;font-size:12.5px;line-height:1.7;">{answers_summary}</td>
  <td>{_fmt_dt(r.created_at)}</td>
</tr>"""

        content = f"""
<h1 class="adm-title">Pre-generation survey responses</h1>
<p class="adm-sub">Shown to the has_website + Google Places cohort instead of firing generation immediately — {len(responses)} total (most recent 300). <a href="/admin/pipeline">← Back to Pipeline</a></p>
<div style="margin:0 0 20px;">{summary_sections or '<span class="muted">No responses yet.</span>'}</div>
<div class="adm-card" style="overflow-x:auto;">
<table>
<thead><tr><th>ID</th><th>Business</th><th>Answers</th><th>Submitted</th></tr></thead>
<tbody>{rows_html or '<tr><td colspan="4" class="muted" style="padding:16px;">No responses yet.</td></tr>'}</tbody>
</table>
</div>
"""
        return render_template_string(_admin_page("Pre-gen survey responses", content, active="pipeline"))
    finally:
        db.close()


@app.route("/admin/pipeline")
@admin_required
def admin_pipeline():
    """State of the outreach machine, one page: how many emails have gone
    out, how many are queued to go out next, and the two upstream feeder
    queues (email discovery, follow-ups) that determine what's sendable.
    Added 2026-07-21, folding in what were separately Discovery and
    Follow-ups pages (see their _render_*_section helpers) — same
    underlying data, one page instead of three tabs."""
    from outreach.ramp import get_remaining_ramp_this_hour, is_within_email_send_window
    now = datetime.utcnow()
    db = SessionLocal()
    try:
        today = now.strftime("%Y-%m-%d")
        email_sent_today = (db.query(DailySendCount).filter(
            DailySendCount.channel == "email", DailySendCount.send_date == today
        ).first() or type("", (), {"count": 0})()).count
        sms_sent_today = (db.query(DailySendCount).filter(
            DailySendCount.channel == "sms", DailySendCount.send_date == today
        ).first() or type("", (), {"count": 0})()).count
        email_sent_all_time = db.query(func.coalesce(func.sum(DailySendCount.count), 0)).filter(
            DailySendCount.channel == "email"
        ).scalar()
        sms_sent_all_time = db.query(func.coalesce(func.sum(DailySendCount.count), 0)).filter(
            DailySendCount.channel == "sms"
        ).scalar()

        email_ramp = db.query(RampState).filter(RampState.channel == "email").first()
        this_hour_cap = email_ramp.daily_volume if email_ramp else 0
        this_hour_remaining = get_remaining_ramp_this_hour("email", now)
        in_window = is_within_email_send_window(now)

        stage_counts = dict(
            db.query(Prospect.funnel_stage, func.count(Prospect.id)).group_by(Prospect.funnel_stage).all()
        )
        pending_discovery_n = db.query(PendingEmailDiscovery).count()
        prospects_total = db.query(func.count(Prospect.id)).scalar()

        def stat(value, label, color="#1C1C1C"):
            return (
                f'<div><div style="font-size:22px;font-weight:800;color:{color};">{value}</div>'
                f'<div class="muted" style="font-size:11.5px;">{label}</div></div>'
            )

        top_strip = f"""
<div style="display:flex;gap:32px;flex-wrap:wrap;margin-bottom:8px;">
  {stat(email_sent_today, "Emails sent today")}
  {stat(f"{this_hour_remaining}/{this_hour_cap}", "Left this hour" if in_window else "Left this hour (outside window)", "#9A9893" if not in_window else "#1C1C1C")}
  {stat(email_sent_all_time, "Emails sent (all-time)")}
  {stat(sms_sent_today, "SMS sent today")}
  {stat(sms_sent_all_time, "SMS sent (all-time)")}
</div>"""

        queue_labels = {
            "sourced": "Sourced (not yet queued)", "queued": "Queued", "awaiting_approval": "Awaiting send (email)",
            "qualified_no_email": "Awaiting send (SMS only)", "unreachable": "Unreachable (no email/phone)",
            "sent": "Sent", "approved": "Approved (manual)",
        }
        queue_order = ["awaiting_approval", "qualified_no_email", "queued", "sourced", "approved", "unreachable", "sent"]
        queue_strip = "".join(
            stat(stage_counts.get(k, 0), queue_labels[k], "#9A9893" if stage_counts.get(k, 0) == 0 else "#1C1C1C")
            for k in queue_order
        )

        content = f"""
<h1 class="adm-title">Pipeline</h1>
<p class="muted" style="font-size:12.5px;margin:0 0 16px;">{prospects_total} prospects sourced all-time · {pending_discovery_n} pending email discovery.</p>

{top_strip}

<h2 style="font-size:16px;font-weight:800;margin:24px 0 8px;">Send queue</h2>
<div style="display:flex;gap:28px;flex-wrap:wrap;margin-bottom:8px;">{queue_strip}</div>
<p class="muted" style="font-size:12px;margin:0 0 20px;"><a href="/admin/outreach" style="color:#2257CC;">Browse/filter all prospects →</a> · <a href="/admin/socials-outreach" style="color:#2257CC;">Socials queue →</a> · <a href="/admin/survey-responses" style="color:#2257CC;">Survey responses →</a> · <a href="/admin/pregen-survey-responses" style="color:#2257CC;">Pre-gen survey responses →</a></p>

{_render_discovery_section(db)}
{_render_followups_section(db)}
"""
        return render_template_string(_admin_page("Pipeline", content, active="pipeline"))
    finally:
        db.close()


def _render_discovery_section(db, history_limit=10):
    """Overnight free email-discovery routine's results (docs/outreach-
    pipeline-spec.md Section 4a) — the routine itself is a scheduled
    Claude Code cloud agent, not code in this repo, so this is the only
    place its results are visible without checking claude.ai/code/routines
    directly. Was its own /admin/discovery page; folded into
    /admin/pipeline 2026-07-21 as part of condensing the admin nav."""
    runs = db.query(DiscoveryRunLog).order_by(DiscoveryRunLog.run_at.desc()).limit(history_limit).all()
    pending_n = db.query(PendingEmailDiscovery).count()

    header = f'<h2 style="font-size:16px;font-weight:800;margin:28px 0 4px;">Discovery</h2>'

    if not runs:
        return f"""{header}
<p class="muted" style="font-size:12.5px;margin:0 0 10px;">Nightly WebSearch routine, 03:30 UTC · {pending_n} pending · no runs logged yet.</p>"""

    latest = runs[0]
    source_rows = "".join(
        f'<tr><td style="padding:6px 10px;">{escape(str(k))}</td><td style="padding:6px 10px;text-align:right;">{v}</td></tr>'
        for k, v in (latest.source_breakdown or {}).items()
    ) or '<tr><td colspan="2" style="padding:10px;color:#9A9893;">No breakdown recorded.</td></tr>'

    latest_html = f"""
<div class="adm-card" style="padding:16px 20px;margin-bottom:10px;">
  <p class="muted" style="margin:0 0 10px;font-size:12px;">Latest run — {_fmt_dt(latest.run_at)}</p>
  <div style="display:flex;gap:28px;flex-wrap:wrap;margin-bottom:12px;">
    <div><div style="font-size:20px;font-weight:800;">{latest.processed_n}</div><div class="muted" style="font-size:11px;">Processed</div></div>
    <div><div style="font-size:20px;font-weight:800;color:#059669;">{latest.found_n}</div><div class="muted" style="font-size:11px;">Found</div></div>
    <div><div style="font-size:20px;font-weight:800;">{latest.website_rediscovered_n}</div><div class="muted" style="font-size:11px;">Site re-found</div></div>
    <div><div style="font-size:20px;font-weight:800;color:#9A9893;">{latest.finalized_null_n}</div><div class="muted" style="font-size:11px;">None found</div></div>
  </div>
  <table style="width:60%;min-width:220px;border-collapse:collapse;font-size:13px;">
    <tbody>{source_rows}</tbody>
  </table>
</div>"""

    history_rows = "".join(
        f'<tr><td style="padding:6px 10px;">{_fmt_dt(r.run_at)}</td>'
        f'<td style="padding:6px 10px;text-align:right;">{r.processed_n}</td>'
        f'<td style="padding:6px 10px;text-align:right;color:#059669;">{r.found_n}</td></tr>'
        for r in runs
    )

    return f"""{header}
<p class="muted" style="font-size:12.5px;margin:0 0 10px;">Nightly WebSearch routine, 03:30 UTC · {pending_n} currently pending.</p>
{latest_html}
<div class="adm-card" style="overflow-x:auto;">
<table style="width:100%;border-collapse:collapse;font-size:13px;">
<thead><tr style="color:#9A9893;font-size:11px;text-transform:uppercase;border-bottom:1px solid #E6E3DC;">
  <th style="text-align:left;padding:6px 10px;">Run</th>
  <th style="text-align:right;padding:6px 10px;">Processed</th>
  <th style="text-align:right;padding:6px 10px;">Found</th>
</tr></thead>
<tbody>{history_rows}</tbody>
</table>
</div>"""


@app.route("/admin/discovery")
@admin_required
def admin_discovery():
    """Folded into /admin/pipeline 2026-07-21 — kept as a redirect."""
    return redirect("/admin/pipeline")


def _followups_queue_rows(db):
    """Shared aggregation behind the follow-ups queue — re-derives due/not-
    due the same way outreach/followup.py's _due_stage() does, rather than
    hand-rolling separate logic that could silently drift from what
    run_followups() actually does. Returns (rows, due_now_n)."""
    from outreach.followup import (
        STAGE_BY_SUBSTAGE, MIN_DAYS_BY_SUBSTAGE, CATCH_ALL_MIN_DAYS, CATCH_ALL_MAX_DAYS, MAX_TOUCHES,
    )
    now = datetime.utcnow()
    active = db.query(Prospect).filter(
        Prospect.funnel_substage.in_(list(STAGE_BY_SUBSTAGE.keys())),
        Prospect.paid_at.is_(None),
        Prospect.touch_count < MAX_TOUCHES,
    ).all()

    rows = []
    for p in active:
        if p.email_unsubscribed and p.sms_unsubscribed:
            continue
        substage_days = (now - p.last_touch_at).days if p.last_touch_at else None
        min_days = MIN_DAYS_BY_SUBSTAGE[p.funnel_substage]
        stage_letter = STAGE_BY_SUBSTAGE[p.funnel_substage]
        first_send_days = (now - p.sent_at).days if p.sent_at else None
        catch_all_due = first_send_days is not None and CATCH_ALL_MIN_DAYS <= first_send_days <= CATCH_ALL_MAX_DAYS

        due_now = (substage_days is not None and substage_days >= min_days) or catch_all_due

        if due_now:
            due_label, due_sort = "Due now", -1
        elif p.last_touch_at:
            days_until = min_days - substage_days
            due_label, due_sort = (now + timedelta(days=days_until)).strftime("%d %b"), days_until
        else:
            due_label, due_sort = "—", 9999

        channel = "SMS only" if not p.email_found else ("Email + SMS" if p.phone else "Email only")
        is_hail_mary = catch_all_due and p.clicked_at is not None

        rows.append({
            "id": p.id, "name": p.business_name or "—", "substage": p.funnel_substage,
            "stage_letter": stage_letter, "last_touch": p.last_touch_at,
            "due_label": due_label, "due_sort": due_sort,
            "touch_count": p.touch_count or 0, "channel": channel,
            "catch_all": catch_all_due, "hail_mary": is_hail_mary,
        })

    rows.sort(key=lambda r: r["due_sort"])
    due_now_n = sum(1 for r in rows if r["due_sort"] == -1)
    return rows, due_now_n


def _render_followups_section(db, limit=None):
    """Was its own /admin/followups page; folded into /admin/pipeline
    2026-07-21 as part of condensing the admin nav."""
    from outreach.followup import MAX_TOUCHES, EMAIL_REPLY_CAPTURE_READY, SMS_REPLY_CAPTURE_READY

    rows, due_now_n = _followups_queue_rows(db)
    shown = rows[:limit] if limit else rows

    gate_note = ""
    if not EMAIL_REPLY_CAPTURE_READY or not SMS_REPLY_CAPTURE_READY:
        missing = "email" if not EMAIL_REPLY_CAPTURE_READY else "SMS"
        gate_note = f' · <span style="color:#B45309;">{missing} withheld (reply-capture not live)</span>'

    rows_html = "".join(f"""
<tr>
  <td style="padding:8px 10px;"><a href="/admin/prospects/{r['id']}" style="color:#2257CC;font-weight:600;text-decoration:none;">{r['name']}</a></td>
  <td style="padding:8px 10px;">{escape(STAGE_LABELS['hail_mary']) if r['hail_mary'] else escape(STAGE_LABELS.get(r['stage_letter'], r['stage_letter'])) + (' (catch-all)' if r['catch_all'] else '')}</td>
  <td style="padding:8px 10px;">{r['channel']}</td>
  <td style="padding:8px 10px;">{r['touch_count']}/{MAX_TOUCHES - 1}</td>
  <td style="padding:8px 10px;">{_fmt_dt(r['last_touch']) or '—'}</td>
  <td style="padding:8px 10px;font-weight:700;{'color:#B91C1C;' if r['due_sort'] == -1 else ''}">{r['due_label']}</td>
</tr>""" for r in shown)

    return f"""
<h2 style="font-size:16px;font-weight:800;margin:28px 0 4px;">Follow-ups{f" (top {limit})" if limit else ""}</h2>
<p class="muted" style="font-size:12.5px;margin:0 0 10px;">{len(rows)} eligible, {due_now_n} due now{gate_note}.</p>

<div class="adm-card" style="overflow-x:auto;">
<table style="width:100%;border-collapse:collapse;font-size:13.5px;">
<thead><tr style="color:#9A9893;font-size:11px;text-transform:uppercase;border-bottom:1px solid #E6E3DC;">
  <th style="text-align:left;padding:6px 10px;">Business</th>
  <th style="text-align:left;padding:6px 10px;">Next stage</th>
  <th style="text-align:left;padding:6px 10px;">Channel</th>
  <th style="text-align:left;padding:6px 10px;">Touches used</th>
  <th style="text-align:left;padding:6px 10px;">Last touch</th>
  <th style="text-align:left;padding:6px 10px;">Due</th>
</tr></thead>
<tbody>{rows_html or '<tr><td colspan="6" style="padding:16px 10px;color:#9A9893;">No prospects currently eligible for a follow-up.</td></tr>'}</tbody>
</table>
</div>"""


@app.route("/admin/followups")
@admin_required
def admin_followups():
    """Folded into /admin/pipeline 2026-07-21 — kept as a redirect."""
    return redirect("/admin/pipeline")


def _prospect_score_breakdown(p):
    """Re-derive the per-factor point breakdown behind Prospect.score, using
    the same private helpers outreach/scorer.py's score_prospect() calls —
    so this can never silently drift from the actual scoring logic."""
    from outreach import scorer
    website_label = p.website_status or "—"
    if p.website_status == "has_website":
        website_label = f"has_website ({p.website_quality or 'unchecked'})"
    return [
        ("Website status", website_label, scorer._website_points(p.website_status, p.website_quality), 40),
        ("Trade tier", p.trade_tier or "—", scorer._tier_points(p.trade_tier), 20),
        ("Review count", p.review_count if p.review_count is not None else "—", scorer._review_points(p.review_count), 20),
        ("Rating", f"{p.rating:.1f}" if p.rating is not None else "—", scorer._rating_points(p.rating), 20),
    ]


def _fmt_dt(dt):
    return dt.strftime("%d %b %Y, %H:%M UTC") if dt else None


def _elapsed(a, b):
    """Human-readable gap between two datetimes (b after a), or None if
    either is missing."""
    if not a or not b:
        return None
    secs = (b - a).total_seconds()
    if secs < 0:
        return None
    if secs < 3600:
        return f"{int(secs // 60)}m"
    if secs < 86400:
        return f"{secs / 3600:.1f}h"
    return f"{secs / 86400:.1f}d"


@app.route("/admin/prospects/<int:prospect_id>")
@admin_required
def admin_prospect_detail(prospect_id):
    """Full context on one prospect — score breakdown, funnel timeline,
    every touch, every email/SMS delivery event, and the generation/lead
    it produced if it clicked. Built so contextualizing a click or
    conversion is a page load, not a DB query someone has to ask for."""
    db = SessionLocal()
    try:
        p = db.query(Prospect).filter(Prospect.id == prospect_id).first()
        if not p:
            abort(404)

        score_rows = _prospect_score_breakdown(p)
        score_rows_html = "".join(
            f'<tr><td style="padding:6px 10px;">{escape(str(label))}</td>'
            f'<td style="padding:6px 10px;color:#5C5A56;">{escape(str(val))}</td>'
            f'<td style="padding:6px 10px;text-align:right;font-weight:700;">{pts}</td>'
            f'<td style="padding:6px 10px;text-align:right;color:#9A9893;">/ {maxpts}</td></tr>'
            for label, val, pts, maxpts in score_rows
        )

        timeline_steps = [
            ("Sourced", p.created_at),
            ("Email/vision processed", p.processed_at),
            ("Approved", p.approved_at),
            ("Sent", p.sent_at),
            ("Opened", p.opened_at),
            ("Clicked (site generated)", p.clicked_at),
            ("Account created", p.account_created_at),
            ("Paid", p.paid_at),
        ]
        timeline_rows_html = ""
        prev_dt = None
        for label, dt in timeline_steps:
            if dt is None:
                timeline_rows_html += (
                    f'<tr style="opacity:.45;"><td style="padding:6px 10px;">{escape(label)}</td>'
                    f'<td style="padding:6px 10px;" colspan="2">— not reached —</td></tr>'
                )
                continue
            gap = _elapsed(prev_dt, dt)
            gap_html = f'<span style="color:#9A9893;"> (+{gap} later)</span>' if gap else ""
            timeline_rows_html += (
                f'<tr><td style="padding:6px 10px;font-weight:600;">{escape(label)}</td>'
                f'<td style="padding:6px 10px;" colspan="2">{_fmt_dt(dt)}{gap_html}</td></tr>'
            )
            prev_dt = dt

        # Granular event log (added 2026-07-25) — every ProspectEvent row,
        # most recent first. The coarse timeline above shows once-ever
        # milestones; this shows every micro-event, including repeats
        # (e.g. clicking the magic link twice) the coarse table can't.
        events = db.query(ProspectEvent).filter(
            ProspectEvent.prospect_id == p.id
        ).order_by(ProspectEvent.occurred_at.desc()).all()
        _EVENT_LABELS = {
            "short_link_clicked": "Short link clicked",
            "magic_link_clicked": "Magic link clicked",
            "email_capture_viewed": "Email-capture form viewed",
            "email_capture_submitted": "Email-capture form submitted",
            "verification_email_sent": "Verification email sent",
            "verification_link_clicked": "Verification link clicked",
            "generation_kicked_off": "Generation kicked off",
        }
        events_log_html = "".join(
            f'<tr><td style="padding:6px 10px;">{_fmt_dt(e.occurred_at)}</td>'
            f'<td style="padding:6px 10px;font-weight:600;">{escape(_EVENT_LABELS.get(e.event_type, e.event_type))}</td>'
            f'<td style="padding:6px 10px;">{escape(e.channel or "—")}</td>'
            f'<td style="padding:6px 10px;color:#9A9893;font-size:12px;">{escape(json.dumps(e.meta)) if e.meta else ""}</td></tr>'
            for e in events
        ) or '<tr><td colspan="4" style="padding:10px;color:#9A9893;">No granular events logged yet.</td></tr>'

        touches = db.query(OutreachTouch).filter(
            OutreachTouch.prospect_id == p.id
        ).order_by(OutreachTouch.sent_at).all()
        touches_html = "".join(
            f'<tr><td style="padding:6px 10px;">{escape(t.stage)}</td>'
            f'<td style="padding:6px 10px;">{escape(t.channel)}</td>'
            f'<td style="padding:6px 10px;">{_fmt_dt(t.sent_at)}</td></tr>'
            for t in touches
        ) or '<tr><td colspan="3" style="padding:10px;color:#9A9893;">No logged touches.</td></tr>'

        # Follow-up schedule — re-derives due/not-due the same way
        # admin_followups() and _due_stage() do, rather than hand-rolling
        # separate logic that could silently drift.
        from outreach.followup import (
            MIN_DAYS_BY_SUBSTAGE, CATCH_ALL_MIN_DAYS, CATCH_ALL_MAX_DAYS,
            MAX_TOUCHES, EMAIL_REPLY_CAPTURE_READY, SMS_REPLY_CAPTURE_READY,
        )
        now = datetime.utcnow()
        if p.paid_at:
            schedule_html = '<p class="muted">No further follow-ups — prospect has paid.</p>'
        elif p.email_unsubscribed and p.sms_unsubscribed:
            schedule_html = '<p class="muted">No further follow-ups — unsubscribed on both email and SMS.</p>'
        elif (p.touch_count or 0) >= MAX_TOUCHES:
            schedule_html = f'<p class="muted">No further follow-ups — reached the {MAX_TOUCHES}-touch cap ({MAX_TOUCHES - 1} follow-ups after the initial send).</p>'
        elif p.funnel_substage not in STAGE_BY_SUBSTAGE:
            schedule_html = f'<p class="muted">No follow-up scheduled — funnel substage <code>{escape(str(p.funnel_substage))}</code> isn\'t one of the follow-up-eligible substages.</p>'
        else:
            stage_letter = STAGE_BY_SUBSTAGE[p.funnel_substage]
            min_days = MIN_DAYS_BY_SUBSTAGE[p.funnel_substage]
            substage_days = (now - p.last_touch_at).days if p.last_touch_at else None
            first_send_days = (now - p.sent_at).days if p.sent_at else None
            catch_all_due = first_send_days is not None and CATCH_ALL_MIN_DAYS <= first_send_days <= CATCH_ALL_MAX_DAYS
            due_now = (substage_days is not None and substage_days >= min_days) or catch_all_due

            channel = "SMS only" if not p.email_found else ("Email + SMS" if p.phone else "Email only")
            is_hail_mary = catch_all_due and p.clicked_at is not None
            reply_gate = ""
            if channel != "SMS only" and not EMAIL_REPLY_CAPTURE_READY:
                reply_gate = ' <span style="color:#B45309;">(withheld — email reply-capture not yet live)</span>'
            elif channel == "SMS only" and not SMS_REPLY_CAPTURE_READY:
                reply_gate = ' <span style="color:#B45309;">(withheld — SMS reply-capture not yet live)</span>'

            if due_now:
                when_html = '<b style="color:#B91C1C;">Due now</b> — will fire on the next send-job-cron run (hourly, 08:00-19:00 UTC)'
            elif p.last_touch_at:
                days_until = min_days - substage_days
                eta = (now + timedelta(days=days_until)).strftime("%d %b %Y")
                when_html = f'<b>{eta}</b> (in {days_until}d, {min_days}d after last touch)'
            else:
                when_html = "— waiting on an initial send/state change before a due date can be projected —"

            next_stage_label = (
                escape(STAGE_LABELS["hail_mary"]) if is_hail_mary
                else escape(STAGE_LABELS.get(stage_letter, stage_letter)) + (' (catch-all window)' if catch_all_due else '')
            )
            schedule_html = f"""
<table style="width:100%;border-collapse:collapse;font-size:13.5px;">
  <tr><td style="padding:6px 10px;color:#9A9893;">Next stage</td>
      <td style="padding:6px 10px;">{next_stage_label}</td></tr>
  <tr><td style="padding:6px 10px;color:#9A9893;">Channel</td><td style="padding:6px 10px;">{channel}{reply_gate}</td></tr>
  <tr><td style="padding:6px 10px;color:#9A9893;">Touches used</td><td style="padding:6px 10px;">{p.touch_count or 0} / {MAX_TOUCHES - 1} follow-ups</td></tr>
  <tr><td style="padding:6px 10px;color:#9A9893;">Last touch</td><td style="padding:6px 10px;">{_fmt_dt(p.last_touch_at) or '—'}</td></tr>
  <tr><td style="padding:6px 10px;color:#9A9893;">Due</td><td style="padding:6px 10px;">{when_html}</td></tr>
</table>"""

        email_events_html = ""
        if p.email:
            events = db.query(EmailEventLog).filter(
                func.lower(EmailEventLog.to_email).like(f"%{p.email.lower()}%")
            ).order_by(EmailEventLog.created_at).all()
            email_events_html = "".join(
                f'<tr><td style="padding:6px 10px;">{escape(e.event_type or "—")}</td>'
                f'<td style="padding:6px 10px;">{_fmt_dt(e.created_at)}</td></tr>'
                for e in events
            ) or '<tr><td colspan="2" style="padding:10px;color:#9A9893;">No Resend events logged for this address.</td></tr>'

        sms_events_html = ""
        if p.phone:
            sms_events = db.query(SmsDeliveryEvent).filter(
                SmsDeliveryEvent.to_phone == p.phone
            ).order_by(SmsDeliveryEvent.created_at).all()
            sms_events_html = "".join(
                f'<tr><td style="padding:6px 10px;">{escape(s.status or "—")}</td>'
                f'<td style="padding:6px 10px;">{_fmt_dt(s.created_at)}</td></tr>'
                for s in sms_events
            ) or '<tr><td colspan="2" style="padding:10px;color:#9A9893;">No SMS delivery events logged for this number.</td></tr>'

        generation = None
        lead = None
        if p.lead_id:
            lead = db.query(Lead).filter(Lead.id == p.lead_id).first()
            generation = db.query(Generation).filter(Generation.lead_id == p.lead_id).first()

        replies = db.query(InboundReply).filter(InboundReply.prospect_id == p.id).order_by(InboundReply.received_at.desc()).all()
        if replies:
            replies_html = "".join(
                f'<div style="padding:10px 0;border-bottom:1px solid #EDEBE5;">'
                f'<p style="margin:0 0 4px;font-size:12.5px;color:#9A9893;">'
                f'{r.channel.upper()} · {_fmt_dt(r.received_at)}'
                f'{" · <span style=\'color:#DC2626;font-weight:700;\'>STOP</span>" if r.is_stop_intent else ""}</p>'
                f'<p style="margin:0;font-size:13.5px;white-space:pre-wrap;">{escape(r.body or "(empty message)")}</p>'
                f'</div>'
                for r in replies
            )
        else:
            replies_html = '<p class="muted">No replies captured (only tracked from 2026-07-21 on).</p>'

        survey = db.query(SurveyResponse).filter(SurveyResponse.prospect_id == p.id).first()
        survey_html = '<p class="muted">No survey response yet.</p>'
        if survey:
            survey_fields = [
                ("Decision", survey.decision), ("Primary reason", survey.primary_reason),
                ("Details", survey.reason_detail), ("Decision maker?", survey.decision_maker),
                ("Already pays for a website", survey.already_pays_for_website),
                ("How they get customers", survey.how_get_customers),
                ("Timeline", survey.timeline), ("What would change their mind", survey.what_would_change_mind),
                ("Discount code issued", survey.discount_code_issued),
                ("Submitted", _fmt_dt(survey.created_at)),
            ]
            survey_rows = "".join(
                f'<tr><td style="padding:6px 10px;color:#9A9893;">{escape(k)}</td>'
                f'<td style="padding:6px 10px;">{escape(str(v))}</td></tr>'
                for k, v in survey_fields if v not in (None, "")
            )
            survey_html = f'<table style="width:100%;border-collapse:collapse;font-size:13.5px;">{survey_rows}</table>'

        pregen_survey = db.query(PreGenSurveyResponse).filter(PreGenSurveyResponse.prospect_id == p.id).first()
        pregen_survey_html = '<p class="muted">No pre-gen survey response (not in the gated cohort, or hasn\'t clicked yet).</p>'
        if pregen_survey:
            pregen_rows = ""
            for q in _PREGEN_SURVEY_QUESTIONS:
                entry = (pregen_survey.answers or {}).get(q["key"]) or {}
                picked = entry.get("picked") or []
                other = entry.get("other")
                parts = [p2 for p2 in picked if p2 != "Other"]
                if other:
                    parts.append(f'Other: "{other}"')
                if not parts:
                    continue
                pregen_rows += (
                    f'<tr><td style="padding:6px 10px;color:#9A9893;max-width:220px;">{escape(q["q"])}</td>'
                    f'<td style="padding:6px 10px;">{escape(", ".join(parts))}</td></tr>'
                )
            pregen_rows += (
                f'<tr><td style="padding:6px 10px;color:#9A9893;">Submitted</td>'
                f'<td style="padding:6px 10px;">{_fmt_dt(pregen_survey.created_at)}</td></tr>'
            )
            pregen_survey_html = f'<table style="width:100%;border-collapse:collapse;font-size:13.5px;">{pregen_rows}</table>'

        generation_html = '<p class="muted">No claim yet — magic link not clicked.</p>'
        if lead:
            gen_links = ""
            if generation:
                if generation.created_at < _VIEW_STATS_RELIABLE_FROM:
                    # Pre-reset generation — view_count/etc were bulk-wiped
                    # (see _VIEW_STATS_RELIABLE_FROM's docstring), so a zero
                    # here doesn't mean "never viewed," it means "we don't
                    # know." Blank rather than a misleading real-looking stat.
                    view_stats_html = ""
                elif generation.view_count:
                    avg_seconds = round(generation.total_view_seconds / generation.view_count) if generation.view_count else 0
                    view_stats_html = (
                        f'<p class="muted" style="margin:6px 0 0;">'
                        f'<b style="color:#1C1C1C;">{generation.view_count}</b> view(s) · '
                        f'first {_fmt_dt(generation.first_viewed_at)} · last {_fmt_dt(generation.last_viewed_at)} · '
                        f'~{avg_seconds}s avg time on page · deepest scroll {generation.max_scroll_pct}%</p>'
                    )
                else:
                    view_stats_html = '<p class="muted" style="margin:6px 0 0;">Never actually viewed — generated but the link hasn\'t been opened.</p>'
                cost_html = (
                    f'<p class="muted" style="margin:6px 0 0;">Cost to generate: '
                    f'<b style="color:#1C1C1C;">${generation.generation_cost_usd:.4f}</b> (Claude API, estimated from token usage)</p>'
                    if generation.generation_cost_usd is not None
                    else '<p class="muted" style="margin:6px 0 0;">Cost to generate: not recorded (generated before cost tracking was added).</p>'
                )
                gen_links = (
                    f'<a href="/admin/generations/{generation.id}/html" target="_blank">View generated site</a> · '
                    f'<a href="/admin/generations/{generation.id}/form-data" target="_blank">Raw form data</a>'
                    f'<p class="muted" style="margin:8px 0 0;">Status: <b style="color:#1C1C1C;">{escape(generation.status)}</b> · created {_fmt_dt(generation.created_at)}</p>'
                    f'{view_stats_html}'
                    f'{cost_html}'
                    f'<form method="post" action="/admin/generations/{generation.id}/toggle-internal" style="margin:10px 0 0;">'
                    f'<input type="hidden" name="redirect_to" value="/admin/prospects/{p.id}">'
                    f'<label style="display:flex;align-items:center;gap:7px;font-size:13px;color:#5C5A56;cursor:pointer;">'
                    f'<input type="checkbox" name="is_internal" value="1" onchange="this.form.requestSubmit()"'
                    f'{" checked" if generation.is_internal else ""}> '
                    f'Internal (my own test/demo — excluded from Generation → Paid, domain conversion and churn KPIs)'
                    f'</label></form>'
                )
            else:
                gen_links = '<p class="muted">Lead exists but no Generation row yet — likely still generating or failed.</p>'
            generation_html = f'<p style="margin:0 0 6px;">Lead <code>{escape(lead.public_id)}</code></p>{gen_links}'

        magic_link = f"{SITE_URL}/claim/{p.token}" if p.token else "—"
        short_link = f"{SITE_URL}/s/{p.short_code}" if p.short_code else "—"

        facts = [
            ("Trade", p.trade), ("Trade tier", p.trade_tier),
            ("Location", p.location), ("Postcode area", p.postcode_area),
            ("Rating", f"{p.rating} ({p.review_count} reviews)" if p.rating else None),
            ("Business status", p.business_status),
            ("Website", p.website), ("Website status", p.website_status),
            ("Phone", p.phone),
            ("Email", p.email), ("Email source", p.email_source),
            ("Email domain type", p.email_domain_type),
            ("Competitor density", p.competitor_density),
            ("Google photos count", p.google_photos_count),
            ("Opening hours complete", p.opening_hours_complete),
            ("Funnel stage", p.funnel_stage), ("Funnel substage", p.funnel_substage),
            ("Touch count", p.touch_count),
            ("Extraction quality", p.extraction_quality),
            ("Email unsubscribed", "Yes" if p.email_unsubscribed else "No"),
            ("SMS unsubscribed", "Yes" if p.sms_unsubscribed else "No"),
            ("Sent day-of-week / hour", f"{p.sent_at_dow} / {p.sent_at_hour}" if p.sent_at_dow is not None else None),
        ]
        facts_html = "".join(
            f'<tr><td style="padding:6px 10px;color:#9A9893;">{escape(k)}</td>'
            f'<td style="padding:6px 10px;">{escape(str(v))}</td></tr>'
            for k, v in facts if v not in (None, "")
        )

        content = f"""
<a href="/admin/funnel" style="font-size:13px;color:#807E79;text-decoration:none;">&larr; Back to Funnel</a>
<h1 class="adm-title" style="margin-top:8px;">{escape(p.business_name or "Unknown business")}</h1>
<p class="adm-sub">Prospect #{p.id} · Score <b style="color:#1C1C1C;">{p.score if p.score is not None else "—"}</b></p>

<div style="display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-bottom:20px;">
  <div class="adm-card" style="padding:16px 20px;">
    <p style="font-weight:700;margin:0 0 10px;">Score breakdown</p>
    <table style="width:100%;border-collapse:collapse;font-size:13.5px;">{score_rows_html}</table>
  </div>
  <div class="adm-card" style="padding:16px 20px;">
    <p style="font-weight:700;margin:0 0 10px;">Prospect facts</p>
    <table style="width:100%;border-collapse:collapse;font-size:13.5px;">{facts_html}</table>
  </div>
</div>

<div class="adm-card" style="padding:16px 20px;margin-bottom:20px;">
  <p style="font-weight:700;margin:0 0 10px;">Funnel timeline</p>
  <table style="width:100%;border-collapse:collapse;font-size:13.5px;">{timeline_rows_html}</table>
</div>

<div class="adm-card" style="padding:16px 20px;margin-bottom:20px;overflow-x:auto;">
  <p style="font-weight:700;margin:0 0 4px;">Detailed event log ({len(events)})</p>
  <p class="muted" style="margin:0 0 10px;font-size:12px;">Every granular funnel micro-event, most recent first — includes repeats (e.g. clicking the same link twice) the timeline above can't show.</p>
  <table style="width:100%;border-collapse:collapse;font-size:13px;">
    <thead><tr style="color:#9A9893;font-size:11px;text-transform:uppercase;">
      <th style="text-align:left;padding:6px 10px;">When</th><th style="text-align:left;padding:6px 10px;">Event</th>
      <th style="text-align:left;padding:6px 10px;">Channel</th><th style="text-align:left;padding:6px 10px;">Detail</th>
    </tr></thead>
    <tbody>{events_log_html}</tbody>
  </table>
</div>

<div style="display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-bottom:20px;">
  <div class="adm-card" style="padding:16px 20px;">
    <p style="font-weight:700;margin:0 0 10px;">Touches sent</p>
    <table style="width:100%;border-collapse:collapse;font-size:13.5px;">
      <thead><tr style="color:#9A9893;font-size:11px;text-transform:uppercase;">
        <th style="text-align:left;padding:6px 10px;">Stage</th><th style="text-align:left;padding:6px 10px;">Channel</th><th style="text-align:left;padding:6px 10px;">Sent</th>
      </tr></thead>
      <tbody>{touches_html}</tbody>
    </table>
  </div>
  <div class="adm-card" style="padding:16px 20px;">
    <p style="font-weight:700;margin:0 0 10px;">Email delivery events</p>
    <table style="width:100%;border-collapse:collapse;font-size:13.5px;"><tbody>{email_events_html}</tbody></table>
    {f'<p style="font-weight:700;margin:16px 0 10px;">SMS delivery events</p><table style="width:100%;border-collapse:collapse;font-size:13.5px;"><tbody>{sms_events_html}</tbody></table>' if p.phone else ""}
  </div>
</div>

<div class="adm-card" style="padding:16px 20px;margin-bottom:20px;">
  <p style="font-weight:700;margin:0 0 10px;">Magic link</p>
  <p style="margin:0 0 4px;font-size:13.5px;"><a href="{escape(magic_link)}" target="_blank">{escape(magic_link)}</a></p>
  <p style="margin:0;font-size:13.5px;color:#9A9893;">Short link: <a href="{escape(short_link)}" target="_blank">{escape(short_link)}</a></p>
</div>

<div class="adm-card" style="padding:16px 20px;margin-bottom:20px;">
  <p style="font-weight:700;margin:0 0 10px;">Generated site</p>
  {generation_html}
</div>

<div class="adm-card" style="padding:16px 20px;margin-bottom:20px;">
  <p style="font-weight:700;margin:0 0 10px;">Follow-up schedule</p>
  {schedule_html}
</div>

<div class="adm-card" style="padding:16px 20px;margin-bottom:20px;">
  <p style="font-weight:700;margin:0 0 10px;">Replies</p>
  {replies_html}
</div>

<div class="adm-card" style="padding:16px 20px;margin-bottom:20px;">
  <p style="font-weight:700;margin:0 0 10px;">Survey response</p>
  {survey_html}
</div>

<div class="adm-card" style="padding:16px 20px;">
  <p style="font-weight:700;margin:0 0 10px;">Pre-generation survey <span class="muted" style="font-weight:400;">(has-website / Google Places cohort — see <a href="/admin/pregen-survey-responses">all responses</a>)</span></p>
  {pregen_survey_html}
</div>
"""
        return render_template_string(_admin_page(escape(p.business_name or "Prospect"), content, active="funnel"))
    finally:
        db.close()


_KPI_INSTRUMENTATION_START = datetime(2026, 7, 14)


def _compute_kpis(db, range_from: datetime = None, range_to: datetime = None, channel: str = "both",
                   source: str = "all") -> dict:
    """
    The 5 main KPIs, computed fresh every call (cheap — small tables, no
    caching needed yet). Each entry carries enough (value + raw counts) for
    the caller to render an honest low-N/empty state rather than a bare
    percentage — several of these are genuinely thin on real data right
    now, and that has to be visible, not hidden behind a number.

    range_from/range_to: optional explicit period (e.g. "this week" vs
    "last month" from the dashboard's date filter). When omitted, defaults
    to the current calendar month — this is the only mode Funnel/Domains
    use today (they call this with no range). When a range IS given, every
    metric is scoped to it consistently, using the same period-boundary
    definitions everywhere:
      - "sent/clicked/converted within the period" for the funnel-style
        metrics (emails sent, click rate, gen->paid)
      - a proper start-of-period active/churned cohort for churn rate,
        rather than the month-to-date approximation used when no range is
        given (that approximation exists ONLY because there's no complete
        prior period yet in the no-filter default case; an explicit range
        the user picked doesn't have that excuse — if they pick a period
        with real customers already active before it started, the strict
        definition applies and the "first month of data" caveat won't show).
    """
    now = datetime.utcnow()
    filtering = range_from is not None or range_to is not None
    period_start = range_from or now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    period_end = range_to or now

    # Channel scoping (added 2026-07-25, by request — the dashboard needed
    # to filter to one channel, same as /admin/funnel already could).
    # channel="both" (default — same sentinel /admin/funnel and
    # _render_funnel_table_html already use, kept consistent rather than
    # inventing a second "all" convention) preserves every metric's
    # original, channel-agnostic behavior exactly. A specific channel ("email"/"sms"/
    # "facebook") computes a CHANNEL MEMBERSHIP set once — every prospect
    # ever sent an "initial" touch on that channel, all-time, not scoped to
    # the period — and reuses it to scope every downstream KPI consistently:
    # prospect-level metrics (click rate, gen->paid) filter directly by
    # prospect id; Generation-level metrics (domain conversion, churn, edit
    # rate, checkout abandonment) filter by that prospect's lead_id, since
    # Generation has no channel field of its own. This is "which channel
    # first reached this person," not per-touch attribution — a prospect
    # touched on multiple channels counts toward each one's membership set,
    # same as /admin/funnel's per-channel cohort logic.
    # Sourcing-channel scoping (added 2026-07-27, by request) — a second,
    # independent axis alongside outreach-method (channel) above: "how was
    # this prospect originally found" (Prospect.sourcing_channel — see
    # outreach/sourcing_channels.py) rather than "how are they being
    # contacted." When both a channel and a source are selected, the two
    # membership sets are intersected below so every downstream metric
    # respects both filters at once, without any of the ~6 usage sites past
    # this point needing to know two filters exist — they just keep reading
    # channel_prospect_ids/channel_lead_ids as they always have.
    scoped = channel != "both" or source != "all"
    channel_prospect_ids = None
    channel_lead_ids = None
    if channel != "both":
        channel_prospect_ids = {
            pid for (pid,) in db.query(OutreachTouch.prospect_id).filter(
                OutreachTouch.channel == channel, OutreachTouch.stage == "initial",
            ).distinct().all()
        }
    if source != "all":
        source_prospect_ids = {
            pid for (pid,) in db.query(Prospect.id).filter(Prospect.sourcing_channel == source).all()
        }
        channel_prospect_ids = source_prospect_ids if channel_prospect_ids is None else (
            channel_prospect_ids & source_prospect_ids
        )
    if scoped:
        channel_lead_ids = {
            lid for (lid,) in db.query(Prospect.lead_id).filter(
                Prospect.id.in_(channel_prospect_ids), Prospect.lead_id.isnot(None),
            ).all()
        } if channel_prospect_ids else set()

    # 1. Clients reached out to (email, or the selected channel) in the
    # period — OutreachTouch, stage == "initial" only. This used to be
    # DailySendCount, which counts every send attempt (initial + follow-up
    # combined) with no way to tell them apart — so "Emails sent / month"
    # was silently inflated by every A/B/C/D follow-up touch on top of
    # first contact. OutreachTouch tags each row with its stage, so this
    # can filter to first-contact sends only.
    # Bounce exclusion only applies to email (the only channel Resend gives
    # a real bounce signal for) — a bounce is not a successfully sent
    # email (the message never reached an inbox), so bounced addresses
    # logged within the same period are subtracted, but ONLY when they
    # belong to a prospect actually IN this filtered cohort (added
    # 2026-07-27, fixing a real bug: this used to be a raw distinct-count
    # of every EmailEventLog bounce in the whole system for the period,
    # completely unscoped by channel/source — harmless-looking for the
    # unfiltered "both"/"all" default since that cohort really is
    # everyone, but badly wrong for any narrower filter, e.g. "socials +
    # email, all time" showed 13 attempted / 0 sent because it was
    # subtracting the GLOBAL bounce count for that date range, not the 13
    # sends' own bounce rate — same per-cohort-matching pattern
    # _render_funnel_table_html already used correctly, just missing here).
    sent_touch_q = db.query(OutreachTouch).filter(
        OutreachTouch.stage == "initial",
        OutreachTouch.sent_at >= period_start,
        OutreachTouch.sent_at <= period_end,
    )
    if channel != "both":
        sent_touch_q = sent_touch_q.filter(OutreachTouch.channel == channel)
    if source != "all":
        sent_touch_q = sent_touch_q.filter(OutreachTouch.prospect_id.in_(channel_prospect_ids or []))
    attempted_touches = sent_touch_q.all()
    emails_attempted = len(attempted_touches)
    if channel in ("both", "email"):
        attempted_email_prospect_ids = {t.prospect_id for t in attempted_touches if t.channel == "email"}
        attempted_emails = {
            (e or "").strip().lower()
            for (e,) in db.query(Prospect.email).filter(Prospect.id.in_(attempted_email_prospect_ids)).all()
            if e
        } if attempted_email_prospect_ids else set()
        bounced_addrs_in_period = {
            (email_utils.parseaddr(e.to_email)[1] or e.to_email or "").strip().lower()
            for e in db.query(EmailEventLog).filter(
                EmailEventLog.event_type.in_(["email.bounced", "bounced"]),
                EmailEventLog.created_at >= period_start,
                EmailEventLog.created_at <= period_end,
            ).all() if e.to_email
        }
        bounced_n = len(attempted_emails & bounced_addrs_in_period)
    else:
        bounced_n = 0
    emails_sent = max(0, emails_attempted - bounced_n)

    # 2. Magic link click rate (aggregate, not per-stage) — Prospect.sent_at
    # vs clicked_at, both real fields with history predating today's fixes.
    # Cohort = prospects sent to within the period; numerator = how many of
    # those have since clicked (clicked_at may fall after period_end — a
    # click is still credited to the send that produced it). Same bounce
    # exclusion as emails_sent above — a bounced address never reached an
    # inbox, so it can't be a fair denominator for "did they click" either
    # (was inflating the denominator and understating the real rate: e.g.
    # 1/20 shown when only 16 of those 20 sends actually landed, so the
    # honest rate is 1/16).
    bounced_emails_in_period = {
        (email_utils.parseaddr(e.to_email)[1] or e.to_email or "").strip().lower()
        for e in db.query(EmailEventLog).filter(
            EmailEventLog.event_type.in_(["email.bounced", "bounced"]),
            EmailEventLog.created_at >= period_start,
            EmailEventLog.created_at <= period_end,
        ).all() if e.to_email
    }
    sent_q = db.query(Prospect).filter(Prospect.sent_at.isnot(None))
    sent_q = sent_q.filter(Prospect.sent_at >= period_start, Prospect.sent_at <= period_end)
    if scoped:
        sent_q = sent_q.filter(Prospect.id.in_(channel_prospect_ids or []))
    if channel in ("all", "email") and bounced_emails_in_period:
        sent_q = sent_q.filter(~func.lower(Prospect.email).in_(bounced_emails_in_period))
    sent_n = sent_q.count()
    clicked_n = sent_q.filter(Prospect.clicked_at.isnot(None)).count()

    # 3. Generation -> Paid — cohort is prospects who reached clicked_at
    # within the period; numerator is how many of those also have paid_at
    # set. Confirmed in the cancellation-flow investigation that real
    # outreach-to-paid conversions are ~0 today (the 5 real paying
    # customers are all direct signups, not outreach-originated) — shown
    # honestly, not hidden.
    # Excludes prospects whose generation is Generation.is_internal — same
    # flag/reasoning as the domain-conversion fix below: paying for a
    # prospect's own site while testing a flow (or going live on one as a
    # demo) goes through the real Stripe checkout and sets a real paid_at,
    # but it isn't a paying customer and shouldn't count toward this rate
    # either way, not just be excluded from the numerator.
    internal_prospect_lead_ids = db.query(Generation.lead_id).filter(
        Generation.is_internal == True, Generation.lead_id.isnot(None)  # noqa: E712
    )
    gen_cohort_q = db.query(Prospect).filter(
        Prospect.clicked_at.isnot(None),
        ~Prospect.lead_id.in_(internal_prospect_lead_ids),
    )
    gen_cohort_q = gen_cohort_q.filter(Prospect.clicked_at >= period_start, Prospect.clicked_at <= period_end)
    if scoped:
        gen_cohort_q = gen_cohort_q.filter(Prospect.id.in_(channel_prospect_ids or []))
    gen_cohort_n = gen_cohort_q.count()
    gen_paid_n = gen_cohort_q.filter(Prospect.paid_at.isnot(None)).count()

    # 4. Custom domain conversion rate — "purchased" definition (a Domain
    # row exists at all, any status) over Generations that went live within
    # the period. Recomputed fresh every call rather than hardcoded, since
    # cancellations (or new domain purchases) change this number in real
    # time. is_internal domains are excluded from the numerator — those are
    # Groundwork's own internal/test domain rows, not a customer purchase
    # (found one live: sussexleadcraftltd.com was flagged is_internal=True
    # yet still counting toward "actual paid customers" here before this
    # fix).
    period_gens_q = db.query(Generation).filter(Generation.status == "live", Generation.is_internal == False)
    period_gens_q = period_gens_q.filter(Generation.created_at >= period_start, Generation.created_at <= period_end)
    if scoped:
        period_gens_q = period_gens_q.filter(Generation.lead_id.in_(channel_lead_ids or []))
    period_gens = period_gens_q.all()
    domain_conv_denom = len(period_gens)
    domain_conv_numer = sum(
        1 for g in period_gens
        if db.query(Domain).filter(
            Domain.generation_id == g.id, Domain.is_internal == False
        ).count() > 0
    )

    # 5. Churn rate. Two modes:
    #   - No explicit range (dashboard default): month-to-date, not a strict
    #     start-of-month cohort, since every real customer signed up this
    #     same calendar month and that denominator is genuinely 0 (not a
    #     bug — no complete prior period exists yet). Denominator is
    #     "customers active at some point this month."
    #   - Explicit range (user picked a week/month to compare): the real,
    #     strict definition — active at the START of the period, churned
    #     DURING it. This is what actually makes week-by-week/month-by-month
    #     comparison meaningful, rather than reusing the approximation.
    # is_internal excludes Groundwork's own personal/test purchases from
    # churn the same way domain_conv_rate above excludes them — added
    # 2026-07-19 after a personal test of the reactivation flow (paid,
    # then cancelled) was inflating churn_numer despite there being zero
    # real paying customers who've ever cancelled.
    def _scope_gen(q):
        """Applies the channel-membership lead_id filter to a Generation
        query when a specific channel is selected — small helper so the
        many Generation-based queries below (churn, edit rate, checkout
        abandonment) don't each repeat the same conditional."""
        if scoped:
            return q.filter(Generation.lead_id.in_(channel_lead_ids or []))
        return q

    if filtering:
        active_at_start = _scope_gen(db.query(Generation)).filter(
            Generation.status == "live",
            Generation.is_internal == False,
            Generation.created_at < period_start,
            (Generation.canceled_at.is_(None)) | (Generation.canceled_at >= period_start),
        ).count()
        churned_in_period = _scope_gen(db.query(Generation)).filter(
            Generation.is_internal == False,
            Generation.canceled_at.isnot(None),
            Generation.canceled_at >= period_start, Generation.canceled_at <= period_end,
        ).count()
        churn_denom = active_at_start
        churn_numer = churned_in_period
        has_full_month_baseline = active_at_start > 0
    else:
        active_now = _scope_gen(db.query(Generation)).filter(
            Generation.status == "live", Generation.is_internal == False
        ).count()
        churned_month = _scope_gen(db.query(Generation)).filter(
            Generation.is_internal == False,
            Generation.canceled_at.isnot(None), Generation.canceled_at >= period_start
        ).count()
        churn_denom = active_now + churned_month
        churn_numer = churned_month
        has_full_month_baseline = _scope_gen(db.query(Generation)).filter(
            Generation.status == "live", Generation.is_internal == False, Generation.created_at < period_start
        ).count() > 0

    # 6/7 instrumentation reliability window — text_edited_at and
    # checkout_started_at are both new columns, only stamped going forward
    # from the deploy that added them (2026-07-23, ~12:50 UTC). A "draft"
    # generation created BEFORE that deploy that genuinely started checkout
    # (or was genuinely edited) still has these fields NULL — indistin-
    # guishable from "never happened." Counting those rows as real zeros
    # would silently misreport "never attempted"/"never edited" for
    # generations we simply have no data for. So: clamp the cohort's start
    # to whichever is later, the requested period_start or this cutoff, and
    # treat a period that ends before the cutoff as having no reliable data
    # at all (same pattern as _OPENED_TRACKING_RELIABLE_FROM above).
    _EDIT_CHECKOUT_TRACKING_RELIABLE_FROM = datetime(2026, 7, 23, 12, 50)
    _edit_checkout_reliable = period_end >= _EDIT_CHECKOUT_TRACKING_RELIABLE_FROM
    _edit_checkout_cohort_start = max(period_start, _EDIT_CHECKOUT_TRACKING_RELIABLE_FROM)

    # 6. Made a real edit — engagement signal, added 2026-07-23. Cohort is
    # every non-internal Generation created in the (reliability-clamped)
    # period, regardless of status (draft/live/canceled all count — the
    # question is "did this person engage with their own site at all," not
    # "did they pay"). Numerator is Generation.text_edited_at set. See that
    # column's docstring in models.py for the interpretation (high edit
    # rate + low conversion -> pricing/checkout friction, not a quality
    # problem; low edit rate -> the site or the generation wait itself is
    # failing to land).
    if _edit_checkout_reliable:
        edit_cohort_q = _scope_gen(db.query(Generation)).filter(
            Generation.is_internal == False,  # noqa: E712
            Generation.created_at >= _edit_checkout_cohort_start, Generation.created_at <= period_end,
        )
        edit_denom = edit_cohort_q.count()
        edit_numer = edit_cohort_q.filter(Generation.text_edited_at.isnot(None)).count()
    else:
        edit_denom = edit_numer = 0

    # 7. Checkout started-and-abandoned vs never-attempted — added
    # 2026-07-23. Cohort is Generation.status == "draft" specifically
    # (never went live) rather than "not live" generally — a canceled
    # generation DID complete a real checkout at some point, so it belongs
    # in neither bucket here. See Generation.checkout_started_at's
    # docstring: "started, abandoned" is a trust/checkout-friction problem;
    # "never attempted" is upstream of pricing entirely. Same reliability
    # clamp as #6 above — a pre-cutoff draft's "never attempted" can't be
    # trusted, so it's excluded from the cohort entirely rather than guessed.
    if _edit_checkout_reliable:
        never_paid_q = _scope_gen(db.query(Generation)).filter(
            Generation.is_internal == False,  # noqa: E712
            Generation.status == "draft",
            Generation.created_at >= _edit_checkout_cohort_start, Generation.created_at <= period_end,
        )
        never_paid_denom = never_paid_q.count()
        checkout_abandoned_n = never_paid_q.filter(Generation.checkout_started_at.isnot(None)).count()
        checkout_never_attempted_n = never_paid_denom - checkout_abandoned_n
    else:
        never_paid_denom = checkout_abandoned_n = checkout_never_attempted_n = 0

    period_label = (
        f'{period_start.strftime("%d %b %Y")} – {period_end.strftime("%d %b %Y")}'
        if filtering else now.strftime("%B %Y")
    )
    _CHANNEL_LABELS = {"both": "all channels", "email": "email", "sms": "SMS", "facebook": "Social DM"}
    _source_labels = {"all": "all sourcing channels", **SOURCING_CHANNEL_LABELS}

    return {
        "period_label": period_label,
        "channel": channel,
        "channel_label": _CHANNEL_LABELS.get(channel, channel),
        "source": source,
        "source_label": _source_labels.get(source, source),
        "emails_sent_month": {
            "value": emails_sent,
            "attempted": emails_attempted,
            "bounced": bounced_n,
            "month_label": period_label,
        },
        "click_rate": {
            "pct": _funnel_pct(clicked_n, sent_n),
            "numer": clicked_n, "denom": sent_n,
        },
        "gen_paid_rate": {
            "pct": _funnel_pct(gen_paid_n, gen_cohort_n),
            "numer": gen_paid_n, "denom": gen_cohort_n,
        },
        "domain_conv_rate": {
            "pct": _funnel_pct(domain_conv_numer, domain_conv_denom),
            "numer": domain_conv_numer, "denom": domain_conv_denom,
        },
        "churn_rate": {
            "pct": _funnel_pct(churn_numer, churn_denom),
            "numer": churn_numer, "denom": churn_denom,
            "has_full_month_baseline": has_full_month_baseline,
            "month_label": period_label,
        },
        "edit_rate": {
            "pct": _funnel_pct(edit_numer, edit_denom),
            "numer": edit_numer, "denom": edit_denom,
            "reliable": _edit_checkout_reliable,
        },
        "checkout_abandon": {
            "abandoned_n": checkout_abandoned_n,
            "never_attempted_n": checkout_never_attempted_n,
            "denom": never_paid_denom,
            "abandoned_pct": _funnel_pct(checkout_abandoned_n, never_paid_denom),
            "never_attempted_pct": _funnel_pct(checkout_never_attempted_n, never_paid_denom),
            "reliable": _edit_checkout_reliable,
        },
    }


def _render_kpi_strip(kpis: dict) -> str:
    """Compact 5-tile KPI strip, reused verbatim on the main dashboard, the
    Funnel page, and the Domains page. Scoped CSS (kpi-* classes) rather
    than merged into _ADMIN_STYLE, same reasoning as the Funnel table and
    the outreach review card — a one-off component, not shared design
    system."""
    def tile(label, big, sub, goal_pct=None, actual_pct=None):
        # goal_pct/actual_pct: when set, shows progress against a stated
        # target (currently 10% for click rate and Generation -> Paid, set
        # 2026-07-20) directly on the tile instead of requiring someone to
        # ask what the current rate is relative to the goal each time.
        goal_html = ""
        if goal_pct is not None:
            met = actual_pct is not None and actual_pct >= goal_pct
            color = "#059669" if met else "#B45309"
            goal_html = (
                f'<div style="font-size:11.5px;font-weight:700;color:{color};margin-top:4px;">'
                f'Goal: {goal_pct:.0f}%{" ✓" if met else ""}</div>'
            )
        return f"""<div class="kpi-tile">
          <div class="kpi-label">{escape(label)}</div>
          <div class="kpi-value">{big}</div>
          <div class="kpi-sub">{sub}</div>
          {goal_html}
        </div>"""

    e = kpis["emails_sent_month"]
    c = kpis["click_rate"]
    g = kpis["gen_paid_rate"]
    d = kpis["domain_conv_rate"]
    ch = kpis["churn_rate"]

    tiles = ""
    sent_sub = e["month_label"]
    if e["bounced"]:
        sent_sub = f'{e["month_label"]} · {e["attempted"]} attempted, {e["bounced"]} bounced'
    tiles += tile(f'Clients reached out to ({escape(kpis.get("channel_label", "email"))})', str(e["value"]), sent_sub)
    tiles += tile(
        "Magic link click rate",
        f'{c["pct"]}%' if c["pct"] is not None else "—",
        f'{c["numer"]}/{c["denom"]} sent' if c["denom"] else "no sends yet",
        goal_pct=10.0, actual_pct=c["pct"],
    )
    tiles += tile(
        "Generation → Paid",
        f'{g["pct"]}%' if g["pct"] is not None else "—",
        f'{g["numer"]}/{g["denom"]} clicked' if g["denom"] else "no outreach conversions yet",
        goal_pct=10.0, actual_pct=g["pct"],
    )
    tiles += tile(
        "Custom domain conversion",
        f'{d["pct"]}%' if d["pct"] is not None else "—",
        f'{d["numer"]}/{d["denom"]} live sites' if d["denom"] else "no live sites yet",
    )
    ch_sub = f'{ch["numer"]} churned / {ch["denom"]} active this month'
    tiles += tile(
        "Churn rate (month-to-date)",
        f'{ch["pct"]}%' if ch["pct"] is not None else "—",
        ch_sub,
    )

    ed = kpis["edit_rate"]
    if not ed["reliable"]:
        ed_big, ed_sub = "—", "tracking started 23 Jul — pick a more recent range"
    elif ed["denom"]:
        ed_big, ed_sub = f'{ed["pct"]}%', f'{ed["numer"]}/{ed["denom"]} sites'
    else:
        ed_big, ed_sub = "—", "no sites yet"
    tiles += tile("Made a real edit", ed_big, ed_sub)

    ab = kpis["checkout_abandon"]
    ab_denom = ab["denom"]
    if not ab["reliable"]:
        ab1_big, ab1_sub = "—", "tracking started 23 Jul — pick a more recent range"
        ab2_big, ab2_sub = "—", "tracking started 23 Jul — pick a more recent range"
    elif ab_denom:
        ab1_big, ab1_sub = str(ab["abandoned_n"]), f'{ab["abandoned_pct"]}% of {ab_denom} never-paid'
        ab2_big, ab2_sub = str(ab["never_attempted_n"]), f'{ab["never_attempted_pct"]}% of {ab_denom} never-paid'
    else:
        ab1_big = ab2_big = "—"
        ab1_sub = ab2_sub = "no unpaid sites yet"
    tiles += tile("Started checkout, didn't pay", ab1_big, ab1_sub)
    tiles += tile("Never attempted checkout", ab2_big, ab2_sub)

    return f"""<style>
.kpi-strip{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:14px;margin-bottom:24px;}}
.kpi-tile{{background:#fff;border:1px solid #E2E0DA;border-radius:12px;padding:16px 18px;}}
.kpi-label{{font-size:11.5px;font-weight:700;text-transform:uppercase;letter-spacing:.05em;color:#9A9893;margin-bottom:8px;}}
.kpi-value{{font-size:26px;font-weight:800;letter-spacing:-.02em;color:#1C1C1C;line-height:1;margin-bottom:6px;}}
.kpi-sub{{font-size:12px;color:#807E79;}}
@media (max-width: 980px){{.kpi-strip{{grid-template-columns:repeat(2,1fr);}}}}
</style>
<div class="kpi-strip">{tiles}</div>"""


_FUNNEL_STAGES = list(STAGE_LABELS.items())

# "Opened" was unreliable before 2026-07-20 (open tracking wasn't verified
# in Resend, and a webhook bug gated opened_at behind funnel_substage ==
# "sent" so it couldn't fire for prospects already past that substage).
# Both fixed 2026-07-20 — that date is the real reliability cutoff, not a
# permanent on/off switch, so admin_funnel() now computes this per-request
# from the selected date range rather than using a single hardcoded flag:
# disabled whenever the range includes (or is unbounded back past) any
# date before the cutoff, enabled only when the whole selected range is
# on/after it. See _OPENED_TRACKING_RELIABLE_FROM below.
_OPENED_TRACKING_RELIABLE_FROM = datetime(2026, 7, 20)

# Every follow-up stage's cohort is defined by prospects already AT a given
# funnel_substage when the touch fired (STAGE_BY_SUBSTAGE) — so every column
# up to and including that substage reads ~100% by construction, not because
# the touch caused anything. E.g. stage C only ever fires for prospects
# already at clicked_generated, so "Opened" AND "Clicked/Generated" are both
# tautological for that row; only "Paid" is real signal. Audited 2026-07-20
# after the same issue was flagged for stage C's Clicked/Generated column
# specifically — generalized to every row instead of leaving it as a caveat
# in prose. Maps substage -> the _FUNNEL_STEPS index it corresponds to; a
# stage's tautological columns are every index from 1 up to and including
# its substage's index (index 0, "Sent", is always real — it's just the
# cohort's size, not a claim about them), EXCEPT _VIEWED_COLUMN_INDEX — see
# that constant's comment for why it's deliberately carved out of this
# range rather than included in it. "account_created" maps to the same
# boundary as "clicked_generated" (2) — the "Account Created" column itself
# was removed 2026-07-23 (zero prospects had ever reached it — outreach
# checkout never requires an account, see create_checkout_session), but a
# stage D cohort (defined by substage=="account_created") still has
# Opened/Clicked-Generated as real tautological columns, so this key can't
# just be deleted outright.
_SUBSTAGE_COLUMN_INDEX = {"opened": 1, "clicked_generated": 2, "account_created": 2}
_SUBSTAGE_BY_STAGE_LETTER = {letter: substage for substage, letter in STAGE_BY_SUBSTAGE.items()}

_FUNNEL_STEPS = ["Sent", "Opened", "Clicked/Generated", "Viewed", "Paid"]
# "Viewed" (added 2026-07-20) is never tautological, even for a
# clicked_generated/account_created-cohort row — reaching /claim/<token>
# and having a Generation row created (what "Clicked/Generated" means)
# does NOT guarantee the prospect actually stuck around through the
# 150-300s build to see the result. Confirmed with real data: 2 of 5
# clicked-and-generated prospects have Generation.view_count == 0 despite
# clicking 2+ hours earlier (long past any plausible still-generating
# window) — genuine abandonment during the wait, not a tracking gap
# (loading.html auto-navigates straight to the tracked /html route with
# no click-through required, so this isn't a missed-instrumentation bug).
_VIEWED_COLUMN_INDEX = _FUNNEL_STEPS.index("Viewed")


def _tautological_columns(stage_key):
    """Column indices into _FUNNEL_STEPS that are ~100% by cohort
    definition for this stage, not real signal. Empty for "initial" and
    for stage A (cohort = still at "sent", i.e. hasn't opened yet — nothing
    downstream of "Sent" is guaranteed for them)."""
    substage = _SUBSTAGE_BY_STAGE_LETTER.get(stage_key)
    boundary = _SUBSTAGE_COLUMN_INDEX.get(substage)
    if boundary is None:
        return []
    return [i for i in range(1, boundary + 1) if i != _VIEWED_COLUMN_INDEX]


def _funnel_pct(numer, denom):
    if not denom:
        return None
    return round(100.0 * numer / denom, 1)


def _render_extraction_quality_breakdown(db):
    """
    Conversion rate (account created, paid) by extraction_quality tier
    (full/partial/none), for prospects the click-triggered logo/photo
    extraction actually ran for (see _try_extract_prospect_assets).
    """
    # Aggregated in Python rather than SQL (small dataset, and avoids a
    # boolean->int CAST that behaves differently between SQLite dev and
    # Postgres prod).
    prospects = db.query(Prospect).filter(Prospect.extraction_quality.isnot(None)).all()
    by_tier = {}
    for p in prospects:
        t = by_tier.setdefault(p.extraction_quality, {"n": 0, "account_n": 0, "paid_n": 0})
        t["n"] += 1
        if p.account_created_at is not None:
            t["account_n"] += 1
        if p.paid_at is not None:
            t["paid_n"] += 1

    tier_order = [("full", "Full (logo + photos)"), ("partial", "Partial (one of the two)"), ("none", "None (fallback)")]
    body_rows = ""
    for key, label in tier_order:
        t = by_tier.get(key, {"n": 0, "account_n": 0, "paid_n": 0})
        account_pct = _funnel_pct(t["account_n"], t["n"])
        paid_pct = _funnel_pct(t["paid_n"], t["n"])
        body_rows += (
            f'<tr><td style="font-weight:600;">{escape(label)}</td>'
            f'<td style="text-align:center;">{t["n"]}</td>'
            f'<td style="text-align:center;">{f"{account_pct}%" if account_pct is not None else "—"}</td>'
            f'<td style="text-align:center;">{f"{paid_pct}%" if paid_pct is not None else "—"}</td>'
            f'</tr>'
        )

    return f"""
<h2 style="font-size:15px;font-weight:700;margin:28px 0 10px;">Conversion by extraction quality</h2>
<p class="adm-sub">For has_website_dated/has_website_modern prospects, whether the click-time logo/photo
extraction (outreach/site_extract.py) found a usable logo and photos, one of the two, or neither —
and how that cohort's account-creation and paid rates compare. Set once per prospect at claim-click time.</p>
<div class="adm-card" style="overflow-x:auto;">
<table>
<thead><tr><th>Extraction quality</th><th style="text-align:center;">Prospects</th><th style="text-align:center;">Account created</th><th style="text-align:center;">Paid</th></tr></thead>
<tbody>{body_rows}</tbody>
</table>
</div>
"""


_SURVEY_REASON_LABELS = dict([
    ("price", "The price"), ("dont_see_need", "Didn't feel like they needed it"),
    ("using_someone_else", "Using/planning to use someone else"), ("still_deciding", "Still deciding"),
    ("technical_issue", "Ran into a problem going live"), ("design_not_right", "Site wasn't right for them"),
    ("other", "Other"),
])
_SURVEY_DECISION_LABELS = dict([("went_live", "Already live"), ("not_yet", "Not yet, considering"), ("not_going_live", "Not planning to")])


def _render_survey_breakdown(db):
    """Why prospects do/don't go live, straight from the post-generation
    survey (added 2026-07-17, /survey/<token>) — real, structured answers
    instead of having to infer intent from silence. Purely observational;
    counts are shown as-is (no minimum-sample gate) since even a handful of
    real responses is more signal than the zero that existed before this."""
    responses = db.query(SurveyResponse).all()
    if not responses:
        return """
<h2 style="font-size:15px;font-weight:700;margin:28px 0 10px;">Why prospects do/don't go live</h2>
<div class="adm-card" style="padding:20px;"><p class="muted" style="margin:0;">No survey responses yet — link goes out
in Stage C/D follow-ups (docs/outreach-pipeline-spec.md Section 11), sent to clicked-but-unpaid prospects.</p></div>
"""
    decision_counts = Counter(r.decision for r in responses)
    reason_counts = Counter(r.primary_reason for r in responses)

    decision_rows = "".join(
        f'<tr><td>{escape(_SURVEY_DECISION_LABELS.get(k, k or "—"))}</td>'
        f'<td style="text-align:center;">{decision_counts[k]}</td></tr>'
        for k in sorted(decision_counts, key=lambda k: -decision_counts[k])
    )
    reason_rows = "".join(
        f'<tr><td>{escape(_SURVEY_REASON_LABELS.get(k, k or "—"))}</td>'
        f'<td style="text-align:center;">{reason_counts[k]}</td></tr>'
        for k in sorted(reason_counts, key=lambda k: -reason_counts[k])
    )
    return f"""
<h2 style="font-size:15px;font-weight:700;margin:28px 0 10px;">Why prospects do/don't go live ({len(responses)} response(s))</h2>
<div style="display:grid;grid-template-columns:1fr 1fr;gap:20px;">
  <div class="adm-card" style="overflow-x:auto;">
  <table><thead><tr><th>Status</th><th style="text-align:center;">Count</th></tr></thead><tbody>{decision_rows}</tbody></table>
  </div>
  <div class="adm-card" style="overflow-x:auto;">
  <table><thead><tr><th>Main reason</th><th style="text-align:center;">Count</th></tr></thead><tbody>{reason_rows}</tbody></table>
  </div>
</div>
"""


def _render_funnel_table_html(db, now, range_from, range_to, channel, source="all"):
    """Just the per-stage funnel table (<div class="adm-card">...<table>) —
    factored out of admin_funnel() 2026-07-25 so /admin (the dashboard) can
    embed the exact same table beneath its channel-filtered KPI cards,
    without duplicating this logic. channel: "both"/"email"/"sms"/
    "facebook", same values admin_funnel() already accepts — this is
    OUTREACH METHOD (how a prospect was contacted). source (added
    2026-07-27): "all" or one of outreach.sourcing_channels'
    SOURCING_CHANNEL_LABELS keys (currently "google_places"/"socials") —
    this is SOURCING CHANNEL (how the prospect was originally found), a
    fully independent axis from channel — see Prospect.sourcing_channel's
    docstring in models.py for why these two are deliberately kept apart."""
    _funnel_opened_disabled = range_from is None or range_from < _OPENED_TRACKING_RELIABLE_FROM
    opened_disabled_title = (
        f"Disabled — the selected range includes dates before {_OPENED_TRACKING_RELIABLE_FROM.strftime('%d %b %Y')}, "
        f"when open tracking wasn't yet reliable. Select a from-date on or after that to see real numbers."
    )

    bounce_event_q = db.query(EmailEventLog).filter(
        EmailEventLog.event_type.in_(["email.bounced", "bounced"])
    )
    if range_from:
        bounce_event_q = bounce_event_q.filter(EmailEventLog.created_at >= range_from)
    if range_to:
        bounce_event_q = bounce_event_q.filter(EmailEventLog.created_at <= range_to)
    bounced_emails_in_range = {
        (email_utils.parseaddr(e.to_email)[1] or e.to_email or "").strip().lower()
        for e in bounce_event_q.all() if e.to_email
    }

    rows_html = ""
    for stage_key, stage_label in _FUNNEL_STAGES:
        q = db.query(OutreachTouch).filter(OutreachTouch.stage == stage_key)
        if channel != "both":
            q = q.filter(OutreachTouch.channel == channel)
        if range_from:
            q = q.filter(OutreachTouch.sent_at >= range_from)
        if range_to:
            q = q.filter(OutreachTouch.sent_at <= range_to)
        if source != "all":
            q = q.join(Prospect, OutreachTouch.prospect_id == Prospect.id).filter(
                Prospect.sourcing_channel == source
            )
        touches = q.all()

        prospect_ids = sorted({t.prospect_id for t in touches})
        email_prospect_ids = {t.prospect_id for t in touches if t.channel == "email"}
        sent_n = len(prospect_ids)

        if sent_n:
            cohort = db.query(Prospect).filter(Prospect.id.in_(prospect_ids)).all()
            bounced_n = sum(
                1 for p in cohort
                if p.id in email_prospect_ids and p.email and p.email.strip().lower() in bounced_emails_in_range
            )
            opened_n = sum(1 for p in cohort if p.opened_at is not None)
            clicked_n = sum(1 for p in cohort if p.clicked_at is not None)
            cohort_lead_ids = [p.lead_id for p in cohort if p.lead_id is not None]
            viewed_n = (
                db.query(Generation).filter(
                    Generation.lead_id.in_(cohort_lead_ids), Generation.view_count > 0
                ).count()
                if cohort_lead_ids else 0
            )
            paid_n = sum(1 for p in cohort if p.paid_at is not None)
        else:
            bounced_n = opened_n = clicked_n = viewed_n = paid_n = 0

        delivered_n = sent_n - bounced_n
        counts = [delivered_n, opened_n, clicked_n, viewed_n, paid_n]
        pcts = [None] + [
            _funnel_pct(counts[i], counts[i - 1]) for i in range(1, len(counts))
        ]

        sent_sub_html = ""
        if bounced_n:
            bounce_pct = _funnel_pct(bounced_n, sent_n)
            sent_sub_html = (
                f'<div style="font-size:11px;color:#9A9893;margin-top:2px;">'
                f'{sent_n} attempted · <span style="color:#DC2626;">{bounced_n} bounced ({bounce_pct}%)</span></div>'
            )

        taut_cols = set(_tautological_columns(stage_key))
        cells = ""
        for i, (label, count) in enumerate(zip(_FUNNEL_STEPS, counts)):
            if label == "Opened" and _funnel_opened_disabled:
                cells += (
                    f'<td style="text-align:center;opacity:.45;" title="{escape(opened_disabled_title)}">'
                    f'<div style="font-size:15px;font-weight:700;">{count}</div>'
                    f'<div style="font-size:10.5px;color:#9A9893;margin-top:2px;">disabled</div></td>'
                )
                continue
            if i in taut_cols:
                cells += (
                    f'<td style="text-align:center;opacity:.45;" '
                    f'title="This cohort is defined by already having reached this stage — not a conversion this touch caused.">'
                    f'<div style="font-size:15px;font-weight:700;">{count}</div>'
                    f'<div style="font-size:10.5px;color:#9A9893;margin-top:2px;">cohort def.</div></td>'
                )
                continue
            pct_html = ""
            if i == 0:
                pct_html = sent_sub_html
            elif pcts[i] is not None:
                pct_html = f'<div style="font-size:11px;color:#9A9893;margin-top:2px;">{pcts[i]}%</div>'
            cells += (
                f'<td style="text-align:center;">'
                f'<div style="font-size:15px;font-weight:700;">{count}</div>'
                f'{pct_html}</td>'
            )

        rows_html += f'<tr><td style="font-weight:600;">{escape(stage_label)}</td>{cells}</tr>'

    header_cells = "".join(
        f'<th style="text-align:center;{"opacity:.45;" if s == "Opened" and _funnel_opened_disabled else ""}" '
        f'title="{escape(opened_disabled_title) if s == "Opened" and _funnel_opened_disabled else ""}">'
        f'{s}{" (disabled)" if s == "Opened" and _funnel_opened_disabled else ""}</th>'
        for s in _FUNNEL_STEPS
    )

    return f"""<div class="adm-card" style="overflow-x:auto;">
<table>
<thead><tr><th>Stage</th>{header_cells}</tr></thead>
<tbody>{rows_html}</tbody>
</table>
</div>"""


@app.route("/admin/funnel")
@admin_required
def admin_funnel():
    """
    Real per-stage, per-channel outreach funnel — built against genuinely
    tracked data only, per the 2026-07-14 instrumentation fixes:
      - Opened, when it fires, is real (resend_events_webhook advances
        opened_at on a real "email.opened" event) — but it depends on two
        things outside this codebase: open tracking enabled on the sending
        domain in the Resend dashboard (off by default), and the
        recipient's mail client actually loading remote images (many
        clients block them, or Apple Mail Privacy Protection may
        pre-fetch/inflate them — this signal is inherently noisier than
        clicks industry-wide, not just here). A prospect can click the
        magic link — a real, first-party navigation, tracked independent
        of any pixel — without ever registering an "opened" event first.
        Confirmed 2026-07-20: 62 delivered outreach emails, 0 real
        prospect opens logged — check the Resend dashboard's domain
        tracking settings before assuming this column is broken.
      - Paid is real (Stripe webhook now writes Prospect.paid_at directly,
        traced via client_reference_id -> Lead -> Prospect.lead_id).
      - Viewed (added 2026-07-20) is real and server-side: Generation.view_count
        is bumped by _record_generation_view on every actual serve of
        /api/generate/<id>/html, and loading.html auto-navigates straight to
        that route the moment generation finishes (no click-through required
        in either the outreach or normal form flow) — so a 0 here means the
        prospect genuinely never had the page load client-side, most likely
        because they closed the tab during the ~150-300s generation wait
        rather than any tracking gap. Confirmed against real data 2026-07-20:
        2 of 5 clicked-and-generated prospects had view_count=0 despite
        clicking 2+ hours earlier (long past any plausible still-generating
        window).
      - Per-stage rows are only real from OutreachTouch's creation date
        forward — there is no historical per-stage log before that, and
        nothing here pretends otherwise (see the banner below the filters).

    Each stage row is a COHORT, not a causal attribution, and — per
    STAGE_LABELS (outreach/followup.py) — each follow-up stage's cohort is
    DEFINED by the prospect's funnel_substage at send time, not by "1st/2nd/
    3rd/4th touch." E.g. "Follow-up — viewed site, no account" (stage C) is
    only ever sent to prospects already at clicked_generated, so its
    "Generated" column reads ~100% by construction — that isn't a
    conversion this stage caused, it's the cohort definition. "Opened" for
    that same row means "of these prospects, how many have opened_at set
    from ANY email, ever" — not "opened because of this specific touch",
    since opened_at/clicked_at/paid_at are single per-prospect timestamps,
    not per-message. That's the honest framing given what the schema
    actually stores.
    """
    now = datetime.utcnow()
    preset = request.args.get("preset", "").strip()
    from_str = request.args.get("from", "").strip()
    to_str = request.args.get("to", "").strip()
    channel = request.args.get("channel", "both").strip().lower()
    if channel not in ("email", "sms", "facebook", "both"):
        channel = "both"
    source = request.args.get("source", "all").strip().lower()
    if source not in SOURCING_CHANNEL_LABELS:
        source = "all"

    def _parse_date(s):
        try:
            return datetime.strptime(s, "%Y-%m-%d")
        except ValueError:
            return None

    # Same preset mechanism as /admin (_date_preset_range) — added
    # 2026-07-20 alongside the "Today" preset, previously only available
    # on the dashboard. Both paths normalize range_to to the actual end-
    # of-range timestamp (not just a bare date), matching /admin's
    # convention, so the query filters below can use a plain <= comparison
    # instead of the previous "+ timedelta(days=1)" bare-date workaround.
    range_from, range_to = (None, None)
    if preset:
        range_from, range_to = _date_preset_range(preset, now)
    elif from_str or to_str:
        range_from = _parse_date(from_str)
        range_to = _parse_date(to_str)
        if range_to:
            range_to = range_to.replace(hour=23, minute=59, second=59)

    db = SessionLocal()
    try:
        kpi_strip = _render_kpi_strip(_compute_kpis(db))
        extraction_quality_breakdown = _render_extraction_quality_breakdown(db)
        survey_breakdown = _render_survey_breakdown(db)

        # Avg generation cost, last 20 sites (added 2026-07-23) — shown in
        # the Recent clicks bar below. Claude API cost is estimated per
        # generation from token usage (Generation.generation_cost_usd — see
        # app.py's _run(); no Anthropic Admin API key available to pull a
        # real billed figure), averaged over the most recent 20 rows that
        # have a recorded cost (older generations predate cost tracking and
        # are simply skipped, not counted as $0). Converted to GBP using
        # today's live rate, not the static domain-pricing constant above.
        last_20_costs = [
            g.generation_cost_usd for g in
            db.query(Generation.generation_cost_usd)
            .filter(Generation.generation_cost_usd.isnot(None))
            .order_by(Generation.created_at.desc())
            .limit(20).all()
        ]
        if last_20_costs:
            avg_cost_usd = sum(last_20_costs) / len(last_20_costs)
            fx_rate = _live_usd_to_gbp_rate()
            avg_cost_gbp = avg_cost_usd * fx_rate
            avg_cost_html = (
                f'<span style="color:#9A9893;">Avg cost/site (last {len(last_20_costs)}):</span> '
                f'<b style="color:#1C1C1C;">${avg_cost_usd:.4f}</b> '
                f'<span style="color:#9A9893;">(£{avg_cost_gbp:.4f} @ {fx_rate:.4f})</span>'
            )
        else:
            avg_cost_html = '<span style="color:#9A9893;">Avg cost/site: no cost data recorded yet</span>'

        # Recent clicks — every prospect who has clicked their magic link,
        # most recent first, with enough context to see WHY at a glance
        # (score, trade, source, time-to-click) without a drill-down. Links
        # to /admin/prospects/<id> for the full picture. Excludes prospects
        # whose generation is Generation.is_internal — same flag/reasoning
        # as the KPIs (real incident: an already-unsubscribed prospect got
        # accidentally generated for via the admin opening their real magic
        # link — marking that generation internal via the profile-page
        # checkbox now also clears it from this table, not just the stats).
        internal_lead_ids = db.query(Generation.lead_id).filter(
            Generation.is_internal == True, Generation.lead_id.isnot(None)  # noqa: E712
        )
        recent_clicked = db.query(Prospect).filter(
            Prospect.clicked_at.isnot(None),
            ~Prospect.lead_id.in_(internal_lead_ids),
        ).order_by(Prospect.clicked_at.desc()).limit(25).all()
        recent_lead_ids = [cp.lead_id for cp in recent_clicked if cp.lead_id is not None]
        gen_by_lead_id = {
            g.lead_id: g for g in db.query(Generation).filter(Generation.lead_id.in_(recent_lead_ids)).all()
        } if recent_lead_ids else {}

        def _viewed_cell(cp):
            gen = gen_by_lead_id.get(cp.lead_id)
            if not gen:
                return '<span style="color:#9A9893;">Never viewed</span>'
            if gen.created_at < _VIEW_STATS_RELIABLE_FROM:
                # Pre-reset generation — a zero here means "we don't know,"
                # not "never viewed" (see _VIEW_STATS_RELIABLE_FROM).
                return '<span style="color:#9A9893;">No data</span>'
            if not gen.view_count:
                return '<span style="color:#9A9893;">Never viewed</span>'
            avg_s = round(gen.total_view_seconds / gen.view_count) if gen.view_count else 0
            return f'{gen.view_count}x · ~{avg_s}s avg · scroll {gen.max_scroll_pct}%'

        recent_clicks_rows = "".join(
            f'<tr>'
            f'<td style="padding:6px 10px;"><a href="/admin/prospects/{cp.id}">{escape(cp.business_name or "—")}</a></td>'
            f'<td style="padding:6px 10px;">{escape(cp.trade or "—")}</td>'
            f'<td style="padding:6px 10px;text-align:right;">{cp.score if cp.score is not None else "—"}</td>'
            f'<td style="padding:6px 10px;">{escape(cp.website_status or "—")}</td>'
            f'<td style="padding:6px 10px;">{escape(cp.email_source or "—")}</td>'
            f'<td style="padding:6px 10px;">{_elapsed(cp.sent_at, cp.clicked_at) or "—"}</td>'
            f'<td style="padding:6px 10px;">{_fmt_dt(cp.clicked_at)}</td>'
            f'<td style="padding:6px 10px;">{_viewed_cell(cp)}</td>'
            f'<td style="padding:6px 10px;">{"Paid" if cp.paid_at else ("Account created" if cp.account_created_at else "Not converted")}</td>'
            f'</tr>'
            for cp in recent_clicked
        ) or '<tr><td colspan="9" style="padding:10px;color:#9A9893;">No clicks yet.</td></tr>'
        recent_clicks_html = f"""
<div style="display:flex;align-items:baseline;justify-content:space-between;flex-wrap:wrap;gap:10px;margin:28px 0 10px;">
  <h2 style="font-size:15px;font-weight:700;margin:0;">Recent clicks ({len(recent_clicked)})</h2>
  <span style="font-size:13px;">{avg_cost_html}</span>
</div>
<div class="adm-card" style="overflow-x:auto;">
<table style="width:100%;border-collapse:collapse;font-size:13.5px;">
<thead><tr style="color:#9A9893;font-size:11px;text-transform:uppercase;border-bottom:1px solid #E6E3DC;">
  <th style="text-align:left;padding:6px 10px;">Business</th>
  <th style="text-align:left;padding:6px 10px;">Trade</th>
  <th style="text-align:right;padding:6px 10px;">Score</th>
  <th style="text-align:left;padding:6px 10px;">Website</th>
  <th style="text-align:left;padding:6px 10px;">Email source</th>
  <th style="text-align:left;padding:6px 10px;">Time to click</th>
  <th style="text-align:left;padding:6px 10px;">Clicked</th>
  <th style="text-align:left;padding:6px 10px;">Viewed site?</th>
  <th style="text-align:left;padding:6px 10px;">Outcome</th>
</tr></thead>
<tbody>{recent_clicks_rows}</tbody>
</table>
</div>"""

        funnel_table_html = _render_funnel_table_html(db, now, range_from, range_to, channel, source=source)

        _funnel_extra_qs = (f"&channel={channel}" if channel != "both" else "") + (f"&source={source}" if source != "all" else "")
        funnel_preset_links = _render_date_preset_links(
            "/admin/funnel", preset, extra_params=_funnel_extra_qs
        )

        # Summary strip: live snapshot of current funnel_substage distribution —
        # always real, independent of the date-range filter (it's "right now",
        # not historical).
        substage_counts = dict(
            db.query(Prospect.funnel_substage, func.count(Prospect.id))
            .group_by(Prospect.funnel_substage).all()
        )
        substage_order = ["sent", "opened", "clicked_generated", "account_created", "replied", "bounced", "cold", None]
        substage_labels = {
            "sent": "Sent", "opened": "Opened", "clicked_generated": "Clicked/Generated",
            "account_created": "Account created", "replied": "Replied", "bounced": "Bounced",
            "cold": "Cold", None: "No substage",
        }
        summary_html = "".join(
            f'<span class="stat"><b>{substage_counts.get(k, 0)}</b> {escape(substage_labels[k])}</span>'
            for k in substage_order
        )
        total_in_pipeline = sum(substage_counts.values())

        content = f"""
<style>
.statsbar{{display:flex;gap:16px;flex-wrap:wrap;align-items:center;}}
.statsbar .stat{{font-size:13px;color:#5C5A56;font-weight:600;}}
.statsbar .stat b{{color:#1C1C1C;font-weight:800;font-size:15px;margin-right:4px;}}
</style>
<h1 class="adm-title">Funnel</h1>
<p class="adm-sub muted" style="font-size:12.5px;">Per-stage outreach funnel. Cohort-defined columns (e.g. "viewed" on a stage that only fires post-click) are greyed — not real signal, just who it was sent to. Sent = delivered, attempted/bounced shown beneath.</p>

{kpi_strip}

<form method="get" style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:10px;">
  {funnel_preset_links}
</form>
<form method="get" style="display:flex;gap:12px;align-items:flex-end;flex-wrap:wrap;margin-bottom:22px;">
  <div>
    <label style="display:block;font-size:12px;font-weight:600;color:#5C5A56;margin-bottom:4px;">From</label>
    <input type="date" name="from" value="{escape(from_str)}" style="padding:8px 10px;border:1px solid #D8D5CE;border-radius:7px;font-size:13.5px;">
  </div>
  <div>
    <label style="display:block;font-size:12px;font-weight:600;color:#5C5A56;margin-bottom:4px;">To</label>
    <input type="date" name="to" value="{escape(to_str)}" style="padding:8px 10px;border:1px solid #D8D5CE;border-radius:7px;font-size:13.5px;">
  </div>
  <div>
    <label style="display:block;font-size:12px;font-weight:600;color:#5C5A56;margin-bottom:4px;">Sourcing channel</label>
    <select name="source" style="padding:8px 10px;border:1px solid #D8D5CE;border-radius:7px;font-size:13.5px;">
      {"".join(f'<option value="{key}" {"selected" if source == key else ""}>{escape(label)}</option>' for key, label in _SOURCE_FILTERS)}
    </select>
  </div>
  <div>
    <label style="display:block;font-size:12px;font-weight:600;color:#5C5A56;margin-bottom:4px;">Outreach method</label>
    <select name="channel" style="padding:8px 10px;border:1px solid #D8D5CE;border-radius:7px;font-size:13.5px;">
      <option value="both" {"selected" if channel == "both" else ""}>All methods</option>
      <option value="email" {"selected" if channel == "email" else ""}>Email only</option>
      <option value="sms" {"selected" if channel == "sms" else ""}>SMS only</option>
      <option value="facebook" {"selected" if channel == "facebook" else ""}>Social DM only</option>
    </select>
  </div>
  <button type="submit" style="background:#3B82F6;color:#fff;border:0;font-weight:700;padding:9px 18px;border-radius:7px;font-size:13.5px;cursor:pointer;">Apply</button>
  <a href="/admin/funnel" style="font-size:13px;color:#807E79;text-decoration:none;padding:9px 4px;">Reset to default (all time)</a>
</form>

{funnel_table_html}

<h2 style="font-size:15px;font-weight:700;margin:28px 0 10px;">Currently in the pipeline ({total_in_pipeline})</h2>
<div class="statsbar" style="justify-content:flex-start;text-align:left;">{summary_html}</div>

{recent_clicks_html}

{survey_breakdown}

{extraction_quality_breakdown}

{_render_send_timing_section(db)}
"""
        return render_template_string(_admin_page("Funnel", content, active="funnel"))
    finally:
        db.close()


_GMAIL_HARD_CEILING = 0.003  # 0.3% — not a circuit-breaker value in code (only
# EMAIL_SPAM_RATE_TRIGGER, 0.1%, actually trips anything), this is the "degradation
# begins well before this" reference ceiling docs/outreach-pipeline-spec.md
# Section 15 cites Gmail as using. Plotted for context only.

_BOUNCE_DETAIL_MISSING = "no detail captured (before 2026-07-21)"


def _extract_bounce_reason(detail):
    """Best-effort human string for a bounce/complaint EmailEventLog row's
    raw `detail` payload — see that column's comment in models.py for why
    this is parsed defensively rather than assuming one exact Resend
    schema. Tries the nested `bounce` object's message/type first (the
    shape Resend's docs describe), then a couple of flatter fallback keys
    other providers/versions have used, and only falls back to a generic
    string if nothing usable is found — never raises on an unexpected
    shape."""
    if not detail or not isinstance(detail, dict):
        return _BOUNCE_DETAIL_MISSING
    bounce = detail.get("bounce")
    if isinstance(bounce, dict):
        for key in ("message", "type", "subType"):
            val = bounce.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    for key in ("reason", "message"):
        val = detail.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return "reason not present in payload"


def _email_domain(addr):
    return (addr or "").rsplit("@", 1)[-1].strip().lower() or "unknown"


def _render_rate_chart(daily_points, trigger, ceiling):
    """Inline SVG bar chart, no external chart library (none is used anywhere
    else in this app) — daily_points is a list of (date_str, sent, harmful,
    rate_or_None) tuples, oldest first. Bars are only drawn for days with a
    real send count; days with zero sends are left empty rather than drawn
    as a false 0%, since "no data" and "0% harmful" are different facts."""
    if not daily_points:
        return '<div class="muted" style="padding:20px;">No data.</div>'

    w, h, pad_l, pad_b, pad_t = 900, 200, 46, 26, 14
    chart_w, chart_h = w - pad_l - 10, h - pad_b - pad_t
    real_rates = [p[3] for p in daily_points if p[3] is not None]
    max_rate = max(real_rates + [ceiling]) * 1.15
    bar_w = chart_w / len(daily_points)

    def y_of(rate):
        return pad_t + chart_h - (rate / max_rate) * chart_h

    bars = ""
    for i, (date_str, sent, harmful, rate) in enumerate(daily_points):
        x = pad_l + i * bar_w
        if rate is None:
            continue
        bar_h = (rate / max_rate) * chart_h
        color = "#DC2626" if rate >= trigger else "#3B82F6"
        bars += (
            f'<rect x="{x + bar_w * 0.15:.1f}" y="{y_of(rate):.1f}" '
            f'width="{bar_w * 0.7:.1f}" height="{bar_h:.1f}" fill="{color}" rx="1.5">'
            f'<title>{date_str}: {harmful}/{sent} = {rate * 100:.3f}%</title></rect>'
        )

    trigger_y = y_of(trigger)
    ceiling_y = y_of(ceiling) if ceiling <= max_rate else None
    lines = (
        f'<line x1="{pad_l}" y1="{trigger_y:.1f}" x2="{w - 10}" y2="{trigger_y:.1f}" '
        f'stroke="#B45309" stroke-width="1.5" stroke-dasharray="5,4"/>'
        f'<text x="{w - 10}" y="{trigger_y - 5:.1f}" text-anchor="end" font-size="11" '
        f'fill="#B45309" font-weight="700">0.1% circuit-breaker trigger</text>'
    )
    if ceiling_y is not None:
        lines += (
            f'<line x1="{pad_l}" y1="{ceiling_y:.1f}" x2="{w - 10}" y2="{ceiling_y:.1f}" '
            f'stroke="#9A9893" stroke-width="1.5" stroke-dasharray="2,3"/>'
            f'<text x="{w - 10}" y="{ceiling_y - 5:.1f}" text-anchor="end" font-size="11" '
            f'fill="#9A9893">0.3% Gmail hard ceiling (reference only)</text>'
        )

    first_label = daily_points[0][0]
    last_label = daily_points[-1][0]
    return f"""<svg viewBox="0 0 {w} {h}" style="width:100%;height:auto;display:block;">
      <line x1="{pad_l}" y1="{pad_t}" x2="{pad_l}" y2="{pad_t + chart_h}" stroke="#E2E0DA"/>
      <line x1="{pad_l}" y1="{pad_t + chart_h}" x2="{w - 10}" y2="{pad_t + chart_h}" stroke="#E2E0DA"/>
      {bars}
      {lines}
      <text x="{pad_l}" y="{h - 4}" font-size="11" fill="#9A9893">{first_label}</text>
      <text x="{w - 10}" y="{h - 4}" text-anchor="end" font-size="11" fill="#9A9893">{last_label}</text>
    </svg>"""


@app.route("/admin/deliverability")
@admin_required
def admin_deliverability():
    now = datetime.utcnow()
    db = SessionLocal()
    try:
        # ---- Email: 30-day daily complaint rate from real webhook data ----
        # Complaints only, not bounces — this chart's dashed line is labeled
        # "0.1% circuit-breaker trigger," which is only accurate for the
        # complaint-rate metric now that bounce_rate has its own, separate
        # 5% trigger (see outreach/ramp.py). Bounce trend lives in the
        # "Bounce rate by discovery source" table below and the summary
        # line above instead, rather than plotting two different-scale
        # rates against one trigger line.
        window_start = (now - timedelta(days=30)).replace(hour=0, minute=0, second=0, microsecond=0)
        sent_rows = db.query(DailySendCount).filter(
            DailySendCount.channel == "email",
            DailySendCount.send_date >= window_start.strftime("%Y-%m-%d"),
        ).all()
        sent_by_day = {r.send_date: r.count for r in sent_rows}

        event_rows = db.query(EmailEventLog).filter(
            EmailEventLog.event_type.in_(["email.complained", "complained"]),
            EmailEventLog.created_at >= window_start,
        ).all()
        harmful_by_day = Counter(e.created_at.strftime("%Y-%m-%d") for e in event_rows)

        daily_points = []
        for i in range(31):
            d = (window_start + timedelta(days=i)).strftime("%Y-%m-%d")
            sent = sent_by_day.get(d, 0)
            harmful = harmful_by_day.get(d, 0)
            daily_points.append((d, sent, harmful, (harmful / sent) if sent else None))

        email_chart = _render_rate_chart(daily_points, EMAIL_SPAM_RATE_TRIGGER, _GMAIL_HARD_CEILING)
        email_signal = get_health_signal("email")
        email_ramp = db.query(RampState).filter(RampState.channel == "email").first()
        email_remaining_today = get_remaining_ramp_today("email")

        if email_signal is None:
            email_signal_html = (
                f'<p class="muted">No signal yet — the trailing 7-day window has fewer than '
                f'{MIN_EMAIL_SAMPLE_SIZE} sends logged in DailySendCount (a smaller sample is too '
                f'noisy to trust — see MIN_EMAIL_SAMPLE_SIZE). The ramp holds flat rather than acting on this.</p>'
            )
        else:
            b_rate = email_signal["bounce_rate"]
            c_rate = email_signal["complaint_rate"]
            b_over = b_rate >= EMAIL_BOUNCE_RATE_TRIGGER
            c_over = c_rate >= EMAIL_SPAM_RATE_TRIGGER
            email_signal_html = (
                f'<p style="font-size:15px;">'
                f'<b style="color:{"#DC2626" if b_over else "#059669"};font-size:20px;">{b_rate * 100:.2f}%</b> '
                f'bounce rate vs. the <b>{EMAIL_BOUNCE_RATE_TRIGGER * 100:.0f}%</b> trigger'
                f'{" — <b>at or over the trigger</b>" if b_over else ""}'
                f' &nbsp;·&nbsp; '
                f'<b style="color:{"#DC2626" if c_over else "#059669"};font-size:20px;">{c_rate * 100:.3f}%</b> '
                f'complaint rate vs. the <b>{EMAIL_SPAM_RATE_TRIGGER * 100:.1f}%</b> trigger'
                f'{" — <b>at or over the trigger</b>" if c_over else ""}'
                f'<br><span class="muted" style="font-size:12.5px;">Based on {email_signal["sample_size"]} sends '
                f'in the trailing 7 days. Tracked separately since 2026-07-17 — a bounce (often a bad/dead '
                f'address, a data-quality issue) is a weaker, noisier signal than a spam complaint at this '
                f'volume, so it gets a higher threshold.</span></p>'
            )

        if email_ramp:
            if email_ramp.circuit_breaker_tripped:
                status = (
                    f'holding at floor (circuit breaker tripped'
                    f'{" " + email_ramp.circuit_breaker_tripped_at.strftime("%d %b") if email_ramp.circuit_breaker_tripped_at else ""}'
                    f' — {email_ramp.consecutive_clean_days or 0}/{CIRCUIT_BREAKER_RECOVERY_DAYS} consecutive clean days toward recovery)'
                )
            else:
                status = "advancing on schedule"
            email_ramp_html = (
                f'<p class="muted">Today\'s allowed volume: <b style="color:#1C1C1C;">{email_ramp.daily_volume}</b>'
                f' · week {email_ramp.week_number} · {status}'
                f' · {email_remaining_today} sends remaining today'
                f'{" · last checked " + email_ramp.last_checked_at.strftime("%d %b %H:%M UTC") if email_ramp.last_checked_at else " · never checked by the nightly ramp job"}'
                f'</p>'
            )
        else:
            email_ramp_html = '<p class="muted">No ramp state row yet for email — the ramp hasn\'t run.</p>'

        # ---- Bounce rate by discovery source (added 2026-07-16) ----
        # Pure visibility, NOT fed into scoring/selection — the first real
        # send showed a 50% bounce rate on email_source='web_search' vs 0%
        # on own_website/own_website_text (6 vs 4 sends, too small to act
        # on alone). Flagged here so the pattern can be watched over a
        # larger sample before deciding whether web_search-sourced emails
        # need a confidence penalty or a verification step —
        # _eligible_initial_send_query in outreach/send_job.py still treats
        # every email_source equally today.
        source_touches = (
            db.query(Prospect.email_source)
            .join(OutreachTouch, OutreachTouch.prospect_id == Prospect.id)
            .filter(OutreachTouch.channel == "email")
            .all()
        )
        sent_by_source = Counter((src or "unknown") for (src,) in source_touches)

        bounce_events = db.query(EmailEventLog).filter(
            EmailEventLog.event_type.in_(["email.complained", "complained", "email.bounced", "bounced"]),
        ).all()
        # Normalize the same way the bounce-webhook fix does — some logged
        # events predate that fix and may still carry Resend's quoted
        # '"addr" <addr>' form rather than a bare address.
        bounced_emails = {
            (email_utils.parseaddr(e.to_email)[1] or e.to_email or "").strip().lower()
            for e in bounce_events if e.to_email
        }
        email_to_source = {
            em.strip().lower(): (src or "unknown")
            for em, src in db.query(Prospect.email, Prospect.email_source).filter(Prospect.email.isnot(None)).all()
            if em
        }
        bounced_by_source = Counter(email_to_source[em] for em in bounced_emails if em in email_to_source)

        # A source needs a real sample before its rate means anything —
        # same 30-outcome convention used elsewhere (Section 5b), relaxed
        # to 15 here since this is a per-source breakdown, not a single
        # pooled rate, and would otherwise almost never accumulate enough
        # volume in any one source to ever flag anything.
        _SOURCE_ACTIONABLE_MIN_N = 15
        actionable_bad_sources = [
            src for src in sent_by_source
            if sent_by_source[src] >= _SOURCE_ACTIONABLE_MIN_N
            and (bounced_by_source.get(src, 0) / sent_by_source[src]) >= EMAIL_BOUNCE_RATE_TRIGGER
        ]

        _actionable_badge = ' <span style="color:#B91C1C;font-weight:700;">⚠ actionable</span>'
        source_rows_html = "".join(
            f'<tr><td style="padding:6px 10px;">{escape(src)}</td>'
            f'<td style="padding:6px 10px;text-align:right;">{sent_by_source[src]}</td>'
            f'<td style="padding:6px 10px;text-align:right;">{bounced_by_source.get(src, 0)}</td>'
            f'<td style="padding:6px 10px;text-align:right;color:'
            f'{"#DC2626" if (bounced_by_source.get(src, 0) / sent_by_source[src] * 100 if sent_by_source[src] else 0) >= 20 else ("#D97706" if bounced_by_source.get(src, 0) else "#059669")}'
            f';font-weight:700;">'
            f'{(bounced_by_source.get(src, 0) / sent_by_source[src] * 100 if sent_by_source[src] else 0):.0f}%'
            f'{_actionable_badge if src in actionable_bad_sources else ""}'
            f'</td></tr>'
            for src in sorted(sent_by_source.keys(), key=lambda s: -sent_by_source[s])
        )
        actionable_callout = ""
        if actionable_bad_sources:
            names = ", ".join(f"<code>{escape(s)}</code>" for s in actionable_bad_sources)
            actionable_callout = (
                f'<p class="muted" style="margin:0 0 10px;font-size:12.5px;">'
                f'<span style="color:#B91C1C;font-weight:700;">⚠ actionable:</span> {names} at/above the '
                f'{EMAIL_BOUNCE_RATE_TRIGGER * 100:.0f}% bounce trigger with a real sample (15+).</p>'
            )
        source_breakdown_html = f"""
        <div class="adm-card" style="padding:16px 20px;margin-top:10px;">
          <p style="font-weight:700;margin:0 0 6px;">Bounce rate by discovery source (all-time)</p>
          {actionable_callout}
          <table style="width:100%;border-collapse:collapse;font-size:13.5px;">
            <thead><tr style="border-bottom:1px solid #E6E3DC;">
              <th style="text-align:left;padding:6px 10px;color:#9A9893;font-size:11px;text-transform:uppercase;">Source</th>
              <th style="text-align:right;padding:6px 10px;color:#9A9893;font-size:11px;text-transform:uppercase;">Sent</th>
              <th style="text-align:right;padding:6px 10px;color:#9A9893;font-size:11px;text-transform:uppercase;">Bounced</th>
              <th style="text-align:right;padding:6px 10px;color:#9A9893;font-size:11px;text-transform:uppercase;">Rate</th>
            </tr></thead>
            <tbody>{source_rows_html or '<tr><td colspan="4" style="padding:10px;color:#9A9893;">No email sends recorded yet.</td></tr>'}</tbody>
          </table>
        </div>"""

        # ---- Bounce reasons, daily + by domain (added 2026-07-21) ----
        # Requested after a real bounce spike tripped the circuit breaker:
        # up to now a bounce told you THAT an address bounced, never WHY —
        # EmailEventLog.detail (models.py) now carries Resend's raw event
        # payload going forward, and _extract_bounce_reason parses out a
        # human string from it defensively. Events logged before this
        # column existed show _BOUNCE_DETAIL_MISSING instead of guessing.
        bounce_window_start = now - timedelta(days=14)
        recent_bounce_events = db.query(EmailEventLog).filter(
            EmailEventLog.event_type.in_(["email.bounced", "bounced"]),
            EmailEventLog.created_at >= bounce_window_start,
        ).order_by(EmailEventLog.created_at.desc()).all()

        by_day = {}
        by_domain = Counter()
        domain_reason_samples = {}
        for e in recent_bounce_events:
            addr = email_utils.parseaddr(e.to_email)[1] or e.to_email or "unknown"
            reason = _extract_bounce_reason(e.detail)
            day_key = e.created_at.strftime("%d %b")
            by_day.setdefault(day_key, []).append((addr, reason))
            domain = _email_domain(addr)
            by_domain[domain] += 1
            domain_reason_samples.setdefault(domain, reason)

        if recent_bounce_events:
            daily_rows_html = "".join(
                f'<tr><td style="padding:6px 10px;vertical-align:top;white-space:nowrap;">{escape(day)}</td>'
                f'<td style="padding:6px 10px;vertical-align:top;text-align:right;">{len(items)}</td>'
                f'<td style="padding:6px 10px;">'
                + "<br>".join(f'<code>{escape(addr)}</code> — <span class="muted">{escape(reason)}</span>' for addr, reason in items)
                + '</td></tr>'
                for day, items in by_day.items()
            )
            domain_rows_html = "".join(
                f'<tr><td style="padding:6px 10px;">{escape(dom)}</td>'
                f'<td style="padding:6px 10px;text-align:right;">{cnt}</td>'
                f'<td style="padding:6px 10px;"><span class="muted">{escape(domain_reason_samples[dom])}</span></td></tr>'
                for dom, cnt in by_domain.most_common()
            )
            bounce_reasons_html = f"""
        <div class="adm-card" style="padding:16px 20px;margin-top:16px;">
          <p style="font-weight:700;margin:0 0 6px;">Bounces by day — trailing 14 days ({len(recent_bounce_events)} total)</p>
          <p class="muted" style="margin:0 0 10px;">Every bounced address and Resend's reported reason, most recent day first — this is the
          "what are the main culprits" view. Reason shows "{escape(_BOUNCE_DETAIL_MISSING)}" for anything logged before payload capture shipped.</p>
          <table style="width:100%;border-collapse:collapse;font-size:13px;">
            <thead><tr style="border-bottom:1px solid #E6E3DC;">
              <th style="text-align:left;padding:6px 10px;color:#9A9893;font-size:11px;text-transform:uppercase;">Day</th>
              <th style="text-align:right;padding:6px 10px;color:#9A9893;font-size:11px;text-transform:uppercase;">Count</th>
              <th style="text-align:left;padding:6px 10px;color:#9A9893;font-size:11px;text-transform:uppercase;">Address — reason</th>
            </tr></thead>
            <tbody>{daily_rows_html}</tbody>
          </table>
        </div>
        <div class="adm-card" style="padding:16px 20px;margin-top:16px;">
          <p style="font-weight:700;margin:0 0 6px;">Bounces by recipient domain — trailing 14 days</p>
          <p class="muted" style="margin:0 0 10px;">Same window, grouped by domain instead of day — a repeat domain here is the clearest
          "this specific culprit is dragging the rate up" signal.</p>
          <table style="width:100%;border-collapse:collapse;font-size:13px;">
            <thead><tr style="border-bottom:1px solid #E6E3DC;">
              <th style="text-align:left;padding:6px 10px;color:#9A9893;font-size:11px;text-transform:uppercase;">Domain</th>
              <th style="text-align:right;padding:6px 10px;color:#9A9893;font-size:11px;text-transform:uppercase;">Bounces</th>
              <th style="text-align:left;padding:6px 10px;color:#9A9893;font-size:11px;text-transform:uppercase;">Sample reason</th>
            </tr></thead>
            <tbody>{domain_rows_html}</tbody>
          </table>
        </div>"""
        else:
            bounce_reasons_html = """
        <div class="adm-card" style="padding:16px 20px;margin-top:16px;">
          <p class="muted" style="margin:0;">No bounces in the trailing 14 days.</p>
        </div>"""

        # ---- SMS: honest "is there real data" check ----
        esendex_configured = bool(os.environ.get("ESENDEX_USERNAME") and os.environ.get("ESENDEX_PASSWORD"))
        sms_event_count = db.query(func.count(SmsDeliveryEvent.id)).scalar() or 0
        sms_signal = get_health_signal("sms")
        sms_ramp = db.query(RampState).filter(RampState.channel == "sms").first()

        if sms_event_count == 0:
            sms_body_html = f"""
            <div class="adm-card" style="padding:16px 20px;">
              <p class="muted" style="margin:0;">No delivery data — Esendex {"configured" if esendex_configured else "not configured"},
              status-poll job not scheduled.</p>
            </div>"""
        else:
            rate = sms_signal["delivery_rate"] if sms_signal else None
            sms_body_html = f"""
            <div class="adm-card" style="padding:24px;">
              <p class="muted">{sms_event_count} delivery events logged.
              {f'Current 7-day delivery rate: <b>{rate * 100:.1f}%</b> vs. baseline <b>{sms_signal["delivery_rate_baseline"] * 100:.1f}%</b>.' if sms_signal else 'Not enough data yet for a 7-day-vs-baseline comparison — the ramp holds flat.'}</p>
            </div>"""

        if sms_ramp:
            status = "holding at floor (circuit breaker tripped)" if sms_ramp.circuit_breaker_tripped else "advancing on schedule"
            sms_ramp_html = (
                f'<p class="muted">Today\'s allowed volume: <b style="color:#1C1C1C;">{sms_ramp.daily_volume}</b>'
                f' · week {sms_ramp.week_number} · {status}'
                f'{" · last checked " + sms_ramp.last_checked_at.strftime("%d %b %H:%M UTC") if sms_ramp.last_checked_at else " · never checked by the nightly ramp job"}'
                f'</p>'
            )
        else:
            sms_ramp_html = '<p class="muted">No ramp state row yet for SMS — the ramp hasn\'t run.</p>'

        content = f"""
<h1 class="adm-title">Deliverability</h1>
<p class="adm-sub muted" style="font-size:12.5px;">Circuit-breaker health for email and SMS.</p>

<h2 style="font-size:16px;font-weight:800;margin:24px 0 4px;">Email</h2>
<div class="adm-card" style="padding:20px 20px 8px;">{email_chart}</div>
{email_signal_html}
{email_ramp_html}
{source_breakdown_html}
{bounce_reasons_html}

<h2 style="font-size:16px;font-weight:800;margin:28px 0 4px;">SMS</h2>
{sms_body_html}
{sms_ramp_html}

<p class="muted" style="font-size:12px;margin:28px 0 0;">Google Postmaster Tools: not connected (needs manual domain verification).</p>

{_render_replies_section(db, limit=15)}
"""
        return render_template_string(_admin_page("Deliverability", content, active="deliverability"))
    finally:
        db.close()


def _variant_rate_cell(numerator, denominator):
    if not denominator:
        return '<span class="muted">—</span>'
    rate = numerator / denominator
    color = "#059669" if rate >= 0.10 else ("#D97706" if rate >= 0.03 else "#5C5A56")
    return f'<span style="color:{color};font-weight:700;">{rate * 100:.1f}%</span> <span class="muted">({numerator}/{denominator})</span>'


def _variant_status_pill(status):
    colors = {"active": "#059669", "canary": "#3B82F6", "paused": "#9A9893", "pending_generation": "#D97706"}
    color = colors.get(status, "#5C5A56")
    label = "awaiting routine" if status == "pending_generation" else status
    return f'<span class="status-pill" style="background:{color}22;color:{color};">{escape(label)}</span>'


@app.route("/admin/variants")
@admin_required
def admin_variants():
    """Email-variant testing dashboard — docs/outreach-pipeline-spec.md
    Section 19. Shows every variant per stage with real performance (last-
    touch-attributed open/click/paid rates, see _stamp_latest_touch_outcome),
    the internal findings log (EvidenceFinding — the DB-backed Section 3;
    see docs/cold-email-evidence-library.md's architecture note), and the
    optimizer job's recent run history (OptimizerRunLog) so this page stays
    checkable even if the job itself is misbehaving."""
    db = SessionLocal()
    try:
        stages = ["initial", "A", "B", "C", "D"]
        variants = db.query(EmailVariant).order_by(EmailVariant.stage.asc(), EmailVariant.created_at.asc()).all()
        by_stage = {}
        for v in variants:
            by_stage.setdefault(v.stage, []).append(v)

        touch_rows = db.query(
            OutreachTouch.variant_id, OutreachTouch.opened_at, OutreachTouch.clicked_at, OutreachTouch.paid_at
        ).filter(OutreachTouch.variant_id.isnot(None)).all()
        perf = {}
        for variant_id, opened_at, clicked_at, paid_at in touch_rows:
            p = perf.setdefault(variant_id, {"sent": 0, "opened": 0, "clicked": 0, "paid": 0})
            p["sent"] += 1
            if opened_at:
                p["opened"] += 1
            if clicked_at:
                p["clicked"] += 1
            if paid_at:
                p["paid"] += 1

        stage_sections = []
        for stage in stages:
            vs = by_stage.get(stage, [])
            rows = []
            for v in vs:
                p = perf.get(v.variant_id, {"sent": 0, "opened": 0, "clicked": 0, "paid": 0})
                rows.append(f"""<tr>
                  <td>{escape(v.variant_id)}{' <span class="muted">(baseline)</span>' if v.parent_variant_id is None else ''}</td>
                  <td>{_variant_status_pill(v.status)}</td>
                  <td style="text-align:right;">{v.weight:.2f}</td>
                  <td>{escape(v.isolated_variable or '—')}</td>
                  <td style="text-align:right;">{p['sent']}</td>
                  <td style="text-align:right;">{_variant_rate_cell(p['opened'], p['sent'])}</td>
                  <td style="text-align:right;">{_variant_rate_cell(p['clicked'], p['sent'])}</td>
                  <td style="text-align:right;">{_variant_rate_cell(p['paid'], p['sent'])}</td>
                  <td class="muted" style="max-width:260px;font-size:12px;">{escape((v.rationale or '')[:200])}</td>
                </tr>""")
            stage_sections.append(f"""
            <div class="adm-card" style="padding:16px 20px;margin-top:14px;">
              <p style="font-weight:700;margin:0 0 8px;">{escape(STAGE_LABELS.get(stage, stage))} <span class="muted" style="font-weight:400;">(stage {escape(stage)})</span></p>
              <table>
                <thead><tr>
                  <th>Variant</th><th>Status</th><th style="text-align:right;">Weight</th><th>Isolated variable</th>
                  <th style="text-align:right;">Sent</th><th style="text-align:right;">Opened</th>
                  <th style="text-align:right;">Clicked</th><th style="text-align:right;">Paid</th><th>Rationale</th>
                </tr></thead>
                <tbody>{''.join(rows) or '<tr><td colspan="9" class="muted" style="padding:14px;">No variants seeded yet for this stage.</td></tr>'}</tbody>
              </table>
            </div>""")

        findings = db.query(EvidenceFinding).order_by(EvidenceFinding.created_at.desc()).limit(30).all()
        findings_rows = "".join(
            f"""<tr>
              <td class="muted" style="white-space:nowrap;">{f.created_at.strftime('%d %b %Y')}</td>
              <td>{escape(f.stage)}</td>
              <td>{escape(f.finding)}</td>
              <td style="text-align:right;">{f.sample_size}</td>
              <td>{escape(f.isolated_variable or '—')}</td>
              <td class="muted">{escape(f.rationale)}</td>
            </tr>"""
            for f in findings
        )
        findings_html = f"""
        <div class="adm-card" style="padding:16px 20px;margin-top:22px;">
          <p style="font-weight:700;margin:0 0 4px;">Internal findings log (Section 3)</p>
          <p class="muted" style="margin:0 0 10px;font-size:12.5px;">
            The database-backed version of docs/cold-email-evidence-library.md's Section 3 — see that file's
            architecture note for why entries live here rather than being committed to the .md file
            (the optimizer job runs on a Railway Cron container with no git write access).</p>
          <table>
            <thead><tr><th>Date</th><th>Stage</th><th>Finding</th><th style="text-align:right;">Sample size</th><th>Isolated variable</th><th>Rationale</th></tr></thead>
            <tbody>{findings_rows or '<tr><td colspan="6" class="muted" style="padding:14px;">No findings yet — accumulates once a stage crosses the sample threshold.</td></tr>'}</tbody>
          </table>
        </div>"""

        run_logs = db.query(OptimizerRunLog).order_by(OptimizerRunLog.run_at.desc()).limit(40).all()
        run_rows = []
        for r in run_logs:
            details = r.details or {}
            actions = details.get("actions", []) if isinstance(details, dict) else []
            action_summary = "; ".join(
                f"{a.get('type', '?')} {a.get('variant_id', '')} ({a.get('reason', '')})" for a in actions
            ) if actions else ("no action — threshold not met" if r.action_taken == "no_action_threshold_not_met" else r.action_taken)
            run_rows.append(f"""<tr>
              <td class="muted" style="white-space:nowrap;">{r.run_at.strftime('%d %b %H:%M UTC')}</td>
              <td style="text-align:right;">{r.samples_processed}</td>
              <td>{escape(action_summary[:300])}</td>
            </tr>""")
        _no_runs_row = (
            '<tr><td colspan="3" class="muted" style="padding:14px;">'
            "No runs logged yet — outreach/variant_optimizer_job.py hasn't run, or its cron isn't scheduled yet.</td></tr>"
        )
        run_log_html = f"""
        <div class="adm-card" style="padding:16px 20px;margin-top:16px;">
          <p style="font-weight:700;margin:0 0 4px;">Optimizer run log</p>
          <p class="muted" style="margin:0 0 10px;font-size:12.5px;">Every hourly run of outreach/variant_optimizer_job.py, whether or not it took action — belt-and-suspenders visibility if this page ever breaks.</p>
          <table>
            <thead><tr><th>Run</th><th style="text-align:right;">New samples</th><th>Actions</th></tr></thead>
            <tbody>{''.join(run_rows) or _no_runs_row}</tbody>
          </table>
        </div>"""

        content = f"""
<h1 class="adm-title">Email variant testing</h1>
<p class="adm-sub muted" style="font-size:12.5px;">Autonomous, evidence-grounded variant testing per outreach stage — no approval gate. See
docs/cold-email-evidence-library.md and docs/outreach-pipeline-spec.md Section 19.
Rates below are last-touch-attributed (see app.py's _stamp_latest_touch_outcome) — a real, honest limitation, not a bug, if a number here looks
slightly off vs. the Funnel page's prospect-level rates.</p>
{''.join(stage_sections)}
{findings_html}
{run_log_html}
"""
        return render_template_string(_admin_page("Variants", content, active="variants"))
    finally:
        db.close()


def _render_dual_rate_chart(buckets, opened_disabled, label_stride=1):
    """Inline SVG dual-line chart (no external library, same convention as
    _render_rate_chart above) — buckets is a list of (label, sent, opened,
    generated) tuples in display order. Two line series against a shared
    y-axis: opened rate (blue) and generated/clicked rate (green).
    Changed from grouped bars to lines 2026-07-23, by request — a rate
    trending across hours/weekdays reads more naturally as a line than as
    side-by-side bar pairs. Buckets with sent=0 leave a gap in the line
    (no point plotted) rather than drawing a false 0%, same "no data isn't
    0%" principle the old bar chart used. opened_disabled greys out and
    dashes the opened series (with a tooltip) the same way admin_funnel's
    _FUNNEL_OPENED_DISABLED does, for the same reason — open tracking
    wasn't reliable before 2026-07-20 (see _OPENED_TRACKING_RELIABLE_FROM).
    label_stride only thins the x-axis TEXT labels (every Nth bucket) —
    every bucket still gets a plotted point; added for the 96-bucket
    15-min-slot chart, where a label on every bucket would be unreadable.

    Y-axis auto-scales to the real data's peak (2026-07-23, by request) —
    a fixed 0-100% axis flattened real peaks/troughs into a thin band near
    the bottom when actual rates are much lower than 100%. The ceiling is
    the actual max data point, rounded up to a "nice" step (5/10/25 — see
    _nice_ceiling below), with a small floor so a near-all-zero chart
    still gets a sane axis rather than compressing to almost nothing."""
    if not buckets:
        return '<div class="muted" style="padding:20px;">No data.</div>'

    w, h, pad_l, pad_b, pad_t = 900, 220, 40, 34, 14
    chart_w, chart_h = w - pad_l - 10, h - pad_b - pad_t
    n = len(buckets)
    bucket_w = chart_w / n

    def x_of(i):
        return pad_l + i * bucket_w + bucket_w / 2

    # Pass 1: compute raw rates and find the real peak before choosing a
    # y-axis ceiling — y_of (pass 2) depends on that ceiling.
    raw = []  # (label, sent, opened_pct_or_None, generated_pct_or_None)
    max_pct = 0.0
    for label, sent, opened, generated in buckets:
        if not sent:
            raw.append((label, sent, None, None))
            continue
        opened_pct = (opened / sent) * 100
        generated_pct = (generated / sent) * 100
        max_pct = max(max_pct, opened_pct, generated_pct)
        raw.append((label, sent, opened_pct, generated_pct))

    def _nice_ceiling(value):
        if value <= 10:
            step = 2
        elif value <= 25:
            step = 5
        elif value <= 60:
            step = 10
        else:
            step = 25
        ceiling = math.ceil(value / step) * step
        return max(ceiling, step)  # never a zero-height axis

    y_max = _nice_ceiling(max_pct)

    def y_of(pct):
        return pad_t + chart_h - (pct / y_max) * chart_h

    x_labels = ""
    opened_points = []   # (x, y, title) or None for a gap
    generated_points = []
    for i, (label, sent, opened_pct, generated_pct) in enumerate(raw):
        x = pad_l + i * bucket_w
        if i % label_stride == 0:
            x_labels += (
                f'<text x="{x + bucket_w / 2:.1f}" y="{h - 6}" text-anchor="middle" '
                f'font-size="10.5" fill="#9A9893">{escape(label)}</text>'
            )
        if opened_pct is None:
            opened_points.append(None)
            generated_points.append(None)
            continue
        opened_title = (
            f"{label}: opened tracking not reliable before {_OPENED_TRACKING_RELIABLE_FROM.strftime('%d %b %Y')}"
            if opened_disabled else f"{label}: opened = {opened_pct:.0f}% ({sent} sent)"
        )
        opened_points.append((x_of(i), y_of(opened_pct), opened_title))
        generated_points.append((x_of(i), y_of(generated_pct), f"{label}: generated = {generated_pct:.0f}% ({sent} sent)"))

    def render_series(points, color, dashed=False):
        # Connect consecutive non-gap points only — a None (no-data bucket)
        # breaks the line rather than interpolating across it or dropping
        # to 0.
        segments = []
        current = []
        for p in points:
            if p is None:
                if len(current) > 1:
                    segments.append(current)
                current = []
                continue
            current.append(p)
        if len(current) > 1:
            segments.append(current)

        dash_attr = ' stroke-dasharray="5,4"' if dashed else ""
        paths = "".join(
            '<path d="M ' + " L ".join(f"{x:.1f} {y:.1f}" for x, y, _ in seg) + '" '
            f'fill="none" stroke="{color}" stroke-width="2.5"{dash_attr} stroke-linejoin="round" stroke-linecap="round"/>'
            for seg in segments
        )
        dots = "".join(
            f'<circle cx="{p[0]:.1f}" cy="{p[1]:.1f}" r="3.5" fill="{color}"><title>{escape(p[2])}</title></circle>'
            for p in points if p is not None
        )
        return paths + dots

    opened_color = "#B9C4D6" if opened_disabled else "#3B82F6"
    lines = render_series(generated_points, "#10B981") + render_series(opened_points, opened_color, dashed=opened_disabled)

    gridlines = ""
    n_gridlines = 4  # 0%, y_max/4, y_max/2, 3*y_max/4, y_max
    for step in range(n_gridlines + 1):
        pct = y_max * step / n_gridlines
        gy = y_of(pct)
        gridlines += (
            f'<line x1="{pad_l}" y1="{gy:.1f}" x2="{w - 10}" y2="{gy:.1f}" stroke="#EDEBE5"/>'
            f'<text x="{pad_l - 6}" y="{gy + 3:.1f}" text-anchor="end" font-size="10" fill="#9A9893">{pct:.0f}%</text>'
        )

    return f"""<svg viewBox="0 0 {w} {h}" style="width:100%;height:auto;display:block;">
      {gridlines}
      {lines}
      {x_labels}
    </svg>"""


def _timing_buckets(db, window_start, now, group_by):
    """Shared aggregation behind the send-timing charts — group_by is
    "slot" (96 buckets, 15-min resolution across the day, from
    Prospect.sent_at_hour/sent_at_slot) or "dow" (0-6 Monday-first, from
    Prospect.sent_at_dow) — all stamped at send time already (see
    send_initial_touch in outreach/send_job.py) rather than recomputed
    here, so this always matches what the rest of the admin already shows
    for a given prospect. Returns a list of (label, sent, opened,
    generated) tuples in display order, one per bucket, covering every
    bucket even if empty (0 sent) so the chart's x-axis doesn't silently
    skip quiet slots/days.

    "slot" replaced the old 24-bucket "hour" grouping 2026-07-23, once
    sending itself moved to 15-min slots (outreach/ramp.py's
    EMAIL_SLOT_MINUTES) — an hourly rollup was too coarse to show anything
    a per-slot send schedule could actually act on. sent_at_slot is NULL
    for every send before that change, so this naturally only reflects
    real 15-min-era data (filtered below) rather than needing a hardcoded
    reliable-from cutoff."""
    prospects = db.query(Prospect).filter(
        Prospect.sent_at.isnot(None), Prospect.sent_at >= window_start, Prospect.sent_at <= now,
    ).all()

    if group_by == "slot":
        n_buckets = 24 * 4
        counts = [{"sent": 0, "opened": 0, "generated": 0} for _ in range(n_buckets)]
        for p in prospects:
            if p.sent_at_hour is None or p.sent_at_slot is None:
                continue  # pre-15-min-slot send — no slot data to bucket
            key = p.sent_at_hour * 4 + p.sent_at_slot
            counts[key]["sent"] += 1
            if p.opened_at is not None:
                counts[key]["opened"] += 1
            if p.clicked_at is not None:
                counts[key]["generated"] += 1
        labels = [f"{h:02d}:{m:02d}" for h in range(24) for m in (0, 15, 30, 45)]
    else:
        n_buckets = 7
        counts = [{"sent": 0, "opened": 0, "generated": 0} for _ in range(n_buckets)]
        for p in prospects:
            key = p.sent_at_dow
            if key is None:
                continue
            counts[key]["sent"] += 1
            if p.opened_at is not None:
                counts[key]["opened"] += 1
            if p.clicked_at is not None:
                counts[key]["generated"] += 1
        labels = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

    return [(labels[i], c["sent"], c["opened"], c["generated"]) for i, c in enumerate(counts)]


def _render_send_timing_section(db):
    """Optics on when to actually send — open rate and generated (real
    click) rate broken out by the hour of day / day of week the ORIGINAL
    send happened, not by when the open/click event itself landed — the
    hour/day you control is when you send, so that's the actionable lever,
    not what hour prospects happen to check their inbox. Trailing 7 days
    by hour, trailing 30 days by weekday. Was its own /admin/send-timing
    page; folded into the bottom of /admin/funnel 2026-07-21 as part of
    condensing the admin nav — same content, one less tab."""
    now = datetime.utcnow()
    week_start = now - timedelta(days=7)
    month_start = now - timedelta(days=30)

    slot_buckets = _timing_buckets(db, week_start, now, "slot")
    weekday_buckets = _timing_buckets(db, month_start, now, "dow")

    hourly_opened_disabled = week_start < _OPENED_TRACKING_RELIABLE_FROM
    weekday_opened_disabled = month_start < _OPENED_TRACKING_RELIABLE_FROM

    def _best_worst(buckets, min_n=10):
        real = [(label, sent, opened, gen) for label, sent, opened, gen in buckets if sent >= min_n]
        if not real:
            return None, None
        return max(real, key=lambda r: r[3] / r[1]), min(real, key=lambda r: r[3] / r[1])

    # No minimum-volume floor (removed 2026-07-23, by request) — a slot
    # with a single send is still real data, and with 96 buckets across a
    # 7-day window, waiting for any meaningful per-slot floor would show
    # "not enough volume" almost everywhere for a long time. min_n=1 (not
    # 0) only to keep the sent>=min_n check from ever admitting a
    # zero-sent bucket and dividing by zero below.
    slot_best, slot_worst = _best_worst(slot_buckets, min_n=1)
    weekday_best, weekday_worst = _best_worst(weekday_buckets, min_n=5)

    def _callout(best, worst, unit):
        if not best:
            return f'<p class="muted" style="margin:0 0 12px;font-size:12.5px;">Not enough volume per {unit} yet.</p>'
        best_label, best_sent, _, best_gen = best
        worst_label, worst_sent, _, worst_gen = worst
        if best_label == worst_label:
            return f'<p class="muted" style="margin:0 0 12px;font-size:12.5px;">Only {escape(best_label)} has enough volume so far ({best_gen}/{best_sent} = {best_gen / best_sent * 100:.0f}%).</p>'
        return (
            f'<p style="margin:0 0 12px;font-size:13px;color:#5C5A56;">Best: '
            f'<b style="color:#059669;">{escape(best_label)}</b> ({best_gen / best_sent * 100:.0f}%) · '
            f'Worst: <b style="color:#DC2626;">{escape(worst_label)}</b> ({worst_gen / worst_sent * 100:.0f}%)</p>'
        )

    slot_chart = _render_dual_rate_chart(slot_buckets, hourly_opened_disabled, label_stride=4)
    weekday_chart = _render_dual_rate_chart(weekday_buckets, weekday_opened_disabled)

    return f"""
<h2 style="font-size:16px;font-weight:800;margin:28px 0 4px;">Send timing</h2>
<p class="muted" style="font-size:12.5px;margin:0 0 10px;">By 15-min send slot (7d) / weekday (30d) — <span style="color:#3B82F6;">■</span> opened, <span style="color:#10B981;">■</span> generated.</p>

<p class="muted" style="font-size:12px;margin:0 0 4px;">By 15-min send slot — trailing 7 days (only reflects sends since the 15-min-slot cadence started — see Prospect.sent_at_slot)</p>
{_callout(slot_best, slot_worst, "slot")}
<div class="adm-card" style="padding:20px 20px 8px;margin-bottom:18px;">{slot_chart}</div>

<p class="muted" style="font-size:12px;margin:0 0 4px;">By day of week — trailing 30 days</p>
{_callout(weekday_best, weekday_worst, "day")}
<div class="adm-card" style="padding:20px 20px 8px;">{weekday_chart}</div>
"""


@app.route("/admin/send-timing")
@admin_required
def admin_send_timing():
    """Folded into /admin/funnel 2026-07-21 — kept as a redirect so any
    existing bookmarks/links still land somewhere real."""
    return redirect("/admin/funnel")


def _render_replies_section(db, limit=None):
    """The actual inbound message text (InboundReply, added 2026-07-21) —
    captured straight from the email-inbound/sms-inbound webhooks, not the
    best-effort forward to groundwork-build@outlook.com. Was its own
    /admin/replies page; folded into /admin/deliverability 2026-07-21 as
    part of condensing the admin nav. Only replies received after that
    date have text captured — greyed out rather than a warning banner."""
    q = db.query(InboundReply, Prospect).join(Prospect, InboundReply.prospect_id == Prospect.id).order_by(
        InboundReply.received_at.desc()
    )
    reply_rows = q.limit(limit).all() if limit else q.all()

    email_unsub_n = db.query(Prospect).filter(Prospect.email_unsubscribed == True).count()  # noqa: E712
    sms_unsub_n = db.query(Prospect).filter(Prospect.sms_unsubscribed == True).count()  # noqa: E712
    total_unsub_n = db.query(Prospect).filter(
        (Prospect.email_unsubscribed == True) | (Prospect.sms_unsubscribed == True)  # noqa: E712
    ).count()

    if not reply_rows:
        table_html = '<div class="adm-card" style="padding:24px;text-align:center;color:#9A9893;font-size:13.5px;">No replies captured yet.</div>'
    else:
        trs = ""
        for r, p in reply_rows:
            badge_color = "#DC2626" if r.is_stop_intent else "#B45309"
            classification = f"Stop ({r.channel.upper()})" if r.is_stop_intent else f"Reply ({r.channel.upper()})"
            when = r.received_at.strftime("%d %b %Y %H:%M UTC")
            trs += (
                f'<tr><td style="vertical-align:top;"><a href="/admin/prospects/{p.id}" style="color:#2257CC;font-weight:600;text-decoration:none;">{escape(p.business_name or "—")}</a></td>'
                f'<td style="vertical-align:top;">{escape(r.from_address or p.email or p.phone or "—")}</td>'
                f'<td style="vertical-align:top;"><span class="status-pill" style="background:{badge_color}22;color:{badge_color};">{escape(classification)}</span></td>'
                f'<td style="vertical-align:top;white-space:nowrap;">{when}</td>'
                f'<td style="max-width:420px;white-space:pre-wrap;">{escape(r.body or "(empty message)")}</td></tr>'
            )
        table_html = f"""<div class="adm-card" style="overflow-x:auto;">
<table><thead><tr><th>Business</th><th>Contact</th><th>Type</th><th>When</th><th>Message</th></tr></thead>
<tbody>{trs}</tbody></table></div>"""

    return f"""
<h2 style="font-size:16px;font-weight:800;margin:28px 0 4px;">Replies{f" (last {limit})" if limit else ""}</h2>
<p class="muted" style="font-size:12.5px;margin:0 0 10px;">
  {total_unsub_n} unsubscribed ({email_unsub_n} email, {sms_unsub_n} SMS) ·
  <span style="color:#9A9893;">only replies after 2026-07-21 have text captured</span>
</p>
{table_html}
"""


@app.route("/admin/replies")
@admin_required
def admin_replies():
    """Folded into /admin/deliverability 2026-07-21 — kept as a redirect."""
    return redirect("/admin/deliverability")


# ---------------------------------------------------------------------------
# Outreach judgment API — bearer-token-authenticated endpoints for Cowork to
# perform vision and email discovery judgments over plain HTTPS without needing
# local database or filesystem access.
#
# Auth: Authorization: Bearer <OUTREACH_API_TOKEN>
#   (separate from the session-cookie admin_required used by the dashboard UI)
# ---------------------------------------------------------------------------

def _check_outreach_token():
    """Return a 401 response if the request doesn't carry a valid bearer token,
    or None if auth passes. Call at the top of each judgment endpoint."""
    expected = os.environ.get("OUTREACH_API_TOKEN", "")
    if not expected:
        return jsonify({"error": "OUTREACH_API_TOKEN not configured on server"}), 500
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer ") or not hmac.compare_digest(auth[len("Bearer "):], expected):
        return jsonify({"error": "Unauthorized"}), 401
    return None


def _outreach_finalize(db, prospect):
    """Mirror of apply_result.py _try_finalize — score and advance stage once
    the email-discovery queue is clear for this prospect."""
    from outreach.scorer import score_prospect as _score
    email_pending = db.query(PendingEmailDiscovery).filter(
        PendingEmailDiscovery.prospect_id == prospect.id).first()
    if email_pending:
        return  # still waiting on email discovery
    prospect.score = _score(prospect)
    if prospect.email_found:
        prospect.funnel_stage = "awaiting_approval"
        prospect.approval_status = "pending"
    elif prospect.phone:
        prospect.funnel_stage = "qualified_no_email"
        prospect.approval_status = "pending"
    else:
        prospect.funnel_stage = "unreachable"
        prospect.approval_status = "unreachable"
    prospect.processed_at = datetime.utcnow()
    db.commit()


@app.route("/api/admin/outreach/pending")
def outreach_pending():
    """Return all prospects currently in the pending email-discovery queue,
    with enough detail for Cowork to judge them over HTTP. Website status is
    no longer judged here — it's set for free at sourcing time straight off
    Places' website field (see outreach/pipeline.py)."""
    denied = _check_outreach_token()
    if denied:
        return denied

    db = SessionLocal()
    try:
        email_rows = (
            db.query(PendingEmailDiscovery, Prospect)
            .join(Prospect, PendingEmailDiscovery.prospect_id == Prospect.id)
            .order_by(PendingEmailDiscovery.id)
            .all()
        )

        pending_email = [
            {
                "email_discovery_id": ed.id,
                "prospect_id": p.id,
                "business_name": p.business_name,
                "trade": p.trade,
                "trade_tier": p.trade_tier,
                "location": p.location,
                "postcode_area": p.postcode_area,
                "website": p.website,
                "website_status": p.website_status,
                "phone": p.phone,
                "google_place_id": p.google_place_id,
                "queued_at": ed.created_at.isoformat() if ed.created_at else None,
            }
            for ed, p in email_rows
        ]

        return jsonify({
            "pending_email": pending_email,
            "counts": {
                "pending_email": len(pending_email),
            },
        })
    finally:
        db.close()


@app.route("/api/admin/outreach/apply-email", methods=["POST"])
def outreach_apply_email():
    """Apply an email discovery result for a prospect.
    Body: {"prospect_id": <int>, "email": <str|null>, "source": <str>}
    Hard rule: only submit emails found on a real page — guessed addresses
    (pattern-matched from business name/domain) are rejected.
    """
    denied = _check_outreach_token()
    if denied:
        return denied

    from outreach.email_discovery import is_valid_email, looks_like_guess, clean_email

    body = request.get_json(silent=True) or {}
    prospect_id = body.get("prospect_id")
    email_raw = body.get("email")  # may be None/null
    source = body.get("source", "web_search")
    force = bool(body.get("force", False))

    if not prospect_id:
        return jsonify({"error": "prospect_id is required"}), 400

    email = None if not email_raw else clean_email(str(email_raw).strip())

    db = SessionLocal()
    try:
        p = db.get(Prospect, prospect_id)
        if not p:
            return jsonify({"error": f"Prospect {prospect_id} not found"}), 404

        if email:
            if not is_valid_email(email):
                return jsonify({"error": f"'{email}' is not a valid email address"}), 422
            if not force and looks_like_guess(email, p.business_name, p.website):
                return jsonify({
                    "error": (
                        f"'{email}' looks like a pattern-match guess for '{p.business_name}'. "
                        "Only submit addresses actually found on a real page. "
                        "Pass force=true to override."
                    )
                }), 422
            p.email = email
            p.email_source = source
            p.email_found = True
        else:
            p.email_found = False

        deleted = db.query(PendingEmailDiscovery).filter(
            PendingEmailDiscovery.prospect_id == prospect_id).delete()
        db.commit()

        _outreach_finalize(db, p)

        return jsonify({
            "status": "ok",
            "prospect_id": p.id,
            "business_name": p.business_name,
            "email": p.email,
            "email_found": p.email_found,
            "email_row_deleted": bool(deleted),
            "funnel_stage": p.funnel_stage,
        })
    finally:
        db.close()


# ---------------------------------------------------------------------------
# GET-based outreach judgment endpoints.
#
# These exist because Cowork's web-fetch tooling can only send plain GET
# requests. A separate env var (OUTREACH_API_TOKEN_GET) is used so this
# token can be rotated independently of OUTREACH_API_TOKEN.
#
# Preferred: X-Outreach-Token header (or Authorization: Bearer <token>) —
# never appears in URLs, so it's never written to access/proxy logs or
# browser history. A ?token=<token> query-string fallback still works and is
# logged loudly (app.logger.warning) whenever it's actually used, since it's
# the thing this header move is meant to get rid of — if Cowork's fetch
# tooling genuinely cannot set custom headers, that warning will fire on
# every real call and the fallback stays load-bearing indefinitely.
#
# GET /api/admin/outreach/g/pending  (X-Outreach-Token: <token>)
# GET /api/admin/outreach/g/apply-email?prospect_id=<id>&email=<e>&source=<s>  (X-Outreach-Token: <token>)
# ---------------------------------------------------------------------------

def _check_outreach_get_token():
    expected = os.environ.get("OUTREACH_API_TOKEN_GET", "")
    if not expected:
        return jsonify({"error": "OUTREACH_API_TOKEN_GET not configured on server"}), 500

    header_token = request.headers.get("X-Outreach-Token", "")
    if not header_token:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            header_token = auth[len("Bearer "):]
    if header_token:
        if not hmac.compare_digest(header_token, expected):
            return jsonify({"error": "Unauthorized"}), 401
        return None

    query_token = request.args.get("token", "")
    if query_token:
        app.logger.warning(
            f"{request.path}: authenticated via ?token= query string, not a header — "
            "this token is being written to access/proxy logs. Switch the caller to the "
            "X-Outreach-Token header."
        )
        if not hmac.compare_digest(query_token, expected):
            return jsonify({"error": "Unauthorized"}), 401
        return None

    return jsonify({"error": "Unauthorized"}), 401


@app.route("/api/admin/outreach/g/pending")
def outreach_get_pending():
    denied = _check_outreach_get_token()
    if denied:
        return denied

    # Added 2026-07-18: this used to return every pending row unpaginated
    # (up to 218+ in production) — fine for a raw JSON consumer, but the
    # discovery routine reads this via WebFetch (the only tool that could
    # actually reach this domain from its sandbox — see the 2026-07-18
    # incident where raw curl/Bash got a 403 policy denial and raw-TCP
    # Postgres was blocked outright), which runs the response through a
    # small summarizing model rather than returning exact bytes. A 218-item
    # JSON array through that path risks truncation/lossy relay. Capped,
    # small default keeps what the routine actually needs (processes ~20
    # a night) well within safe relay size.
    try:
        limit = min(int(request.args.get("limit", 20)), 50)
    except (ValueError, TypeError):
        limit = 20

    # Delegate to the same DB logic as the POST version.
    db = SessionLocal()
    try:
        email_rows = (
            db.query(PendingEmailDiscovery, Prospect)
            .join(Prospect, PendingEmailDiscovery.prospect_id == Prospect.id)
            .order_by(PendingEmailDiscovery.id)
            .limit(limit)
            .all()
        )
        pending_email = [
            {
                "email_discovery_id": ed.id, "prospect_id": p.id,
                "business_name": p.business_name, "trade": p.trade,
                "trade_tier": p.trade_tier, "location": p.location,
                "postcode_area": p.postcode_area, "website": p.website,
                "website_status": p.website_status, "phone": p.phone,
                "google_place_id": p.google_place_id,
                "queued_at": ed.created_at.isoformat() if ed.created_at else None,
            }
            for ed, p in email_rows
        ]
        total_pending = db.query(PendingEmailDiscovery).count()
        return jsonify({
            "pending_email": pending_email,
            "counts": {"returned": len(pending_email), "total_pending": total_pending},
        })
    finally:
        db.close()


@app.route("/api/admin/outreach/g/apply-email")
def outreach_get_apply_email():
    denied = _check_outreach_get_token()
    if denied:
        return denied

    from outreach.email_discovery import is_valid_email, looks_like_guess, clean_email
    from outreach.email_verify import has_deliverable_domain

    try:
        prospect_id = int(request.args.get("prospect_id", ""))
    except (ValueError, TypeError):
        return jsonify({"error": "prospect_id must be an integer"}), 400

    email_raw = clean_email(request.args.get("email", "").strip())
    email = email_raw if email_raw else None
    source = request.args.get("source", "web_search")
    force = request.args.get("force", "").lower() in ("1", "true", "yes")

    db = SessionLocal()
    try:
        p = db.get(Prospect, prospect_id)
        if not p:
            return jsonify({"error": f"Prospect {prospect_id} not found"}), 404
        if email:
            if not is_valid_email(email):
                return jsonify({"error": f"'{email}' is not a valid email address"}), 422
            if not force and looks_like_guess(email, p.business_name, p.website):
                return jsonify({
                    "error": (
                        f"'{email}' looks like a pattern-match guess for '{p.business_name}'. "
                        "Only submit addresses actually found on a real page. "
                        "Add &force=true to override."
                    )
                }), 422
            # Added 2026-07-18 — this GET endpoint predates the MX-check work
            # done elsewhere in the pipeline (outreach/apply_result.py's CLI
            # path, outreach/send_job.py's pre-send re-check) and never got
            # it; without this, an agent calling this endpoint directly
            # could write a guaranteed-bounce address that only gets caught
            # later at send time instead of right here.
            if not force and not has_deliverable_domain(email):
                return jsonify({
                    "error": (
                        f"'{email}' has no MX or A/AAAA record — mail to it would hard-bounce. "
                        "Add &force=true to submit anyway if you're certain this is right."
                    )
                }), 422
            p.email = email
            p.email_source = source
            p.email_found = True
        else:
            p.email_found = False
        deleted = db.query(PendingEmailDiscovery).filter(
            PendingEmailDiscovery.prospect_id == prospect_id).delete()
        db.commit()
        _outreach_finalize(db, p)
        return jsonify({
            "status": "ok", "prospect_id": p.id,
            "business_name": p.business_name,
            "email": p.email, "email_found": p.email_found,
            "email_row_deleted": bool(deleted),
            "funnel_stage": p.funnel_stage,
        })
    finally:
        db.close()


@app.route("/api/admin/outreach/g/update-website")
def outreach_get_update_website():
    """Added 2026-07-18 for the nightly WebSearch-based discovery routine's
    website-rediscovery step (docs/outreach-pipeline-spec.md Section 4a) —
    when Places API's website field was empty/wrong, this lets the routine
    correct it via the same scoped-token GET pattern as the two endpoints
    above, without needing raw database credentials. Deliberately narrow:
    only touches `website`/`website_status`, nothing else — re-scoring
    happens naturally the next time this prospect is finalized via
    apply-email, not here."""
    denied = _check_outreach_get_token()
    if denied:
        return denied

    try:
        prospect_id = int(request.args.get("prospect_id", ""))
    except (ValueError, TypeError):
        return jsonify({"error": "prospect_id must be an integer"}), 400

    website = request.args.get("website", "").strip()
    if not website or not website.lower().startswith(("http://", "https://")):
        return jsonify({"error": "website must be a real http(s) URL"}), 400

    db = SessionLocal()
    try:
        p = db.get(Prospect, prospect_id)
        if not p:
            return jsonify({"error": f"Prospect {prospect_id} not found"}), 404
        p.website = website
        p.website_status = "has_website"
        db.commit()
        return jsonify({
            "status": "ok", "prospect_id": p.id,
            "business_name": p.business_name, "website": p.website,
            "website_status": p.website_status,
        })
    finally:
        db.close()


@app.route("/api/admin/outreach/g/log-run", methods=["GET", "POST"])
def outreach_get_log_run():
    """Added 2026-07-18 — lets the nightly discovery routine log its own
    summary via the same scoped token, rather than relying on someone
    manually reading its final chat output. Powers /admin/discovery.
    Accepts GET (query string) or POST (JSON body) — GET for parity with
    the other g/ endpoints (simple for a curl-only agent), POST because
    `sources` is a JSON object and query strings are an awkward place for
    that if the caller prefers a real body."""
    denied = _check_outreach_get_token()
    if denied:
        return denied

    if request.method == "POST":
        data = request.get_json(silent=True) or {}
    else:
        data = request.args

    def _int(key):
        try:
            return int(data.get(key, 0))
        except (ValueError, TypeError):
            return 0

    sources_raw = data.get("sources")
    source_breakdown = None
    if sources_raw:
        if isinstance(sources_raw, dict):
            source_breakdown = sources_raw
        else:
            try:
                source_breakdown = json.loads(sources_raw)
            except (ValueError, TypeError):
                source_breakdown = None

    db = SessionLocal()
    try:
        log = DiscoveryRunLog(
            processed_n=_int("processed"),
            found_n=_int("found"),
            website_rediscovered_n=_int("website_rediscovered"),
            finalized_null_n=_int("finalized_null"),
            source_breakdown=source_breakdown,
            notes=(data.get("notes") or "").strip()[:1000] or None,
        )
        db.add(log)
        db.commit()
        return jsonify({"status": "ok", "run_id": log.id, "run_at": log.run_at.isoformat()})
    finally:
        db.close()


@app.route("/api/generate/<job_id>/status")
def job_status(job_id):
    with _jobs_lock:
        job = _jobs.get(job_id)
    if job:
        result = {"status": job["status"]}
        if job["status"] == "error":
            result["error"] = job.get("error", "Unknown error")
        return jsonify(result)

    db = SessionLocal()
    try:
        gen = db.query(Generation).join(Lead).filter(Lead.public_id == job_id).first()
        if gen:
            return jsonify({"status": "done"})
    finally:
        db.close()
    return jsonify({"status": "not_found"}), 404


def _record_generation_view(job_id):
    """Bump view_count/first_viewed_at/last_viewed_at for a real serve of
    the generated HTML (job_html below). Wrapped so a tracking failure can
    never break serving the actual site — that would be a much worse
    regression than a missed view count."""
    db = SessionLocal()
    try:
        gen = db.query(Generation).join(Lead).filter(Lead.public_id == job_id).first()
        if gen:
            now = datetime.utcnow()
            gen.view_count = (gen.view_count or 0) + 1
            if gen.first_viewed_at is None:
                gen.first_viewed_at = now
            gen.last_viewed_at = now
            db.commit()
    except Exception:
        app.logger.exception("_record_generation_view failed for job_id=%s — view not counted, site serve unaffected", job_id)
    finally:
        db.close()


@app.route("/api/generate/<job_id>/html")
def job_html(job_id):
    show_toast = request.args.get("new") == "1"
    with _jobs_lock:
        job = _jobs.get(job_id)
    if job:
        if job["status"] != "done":
            return jsonify({"error": "not ready", "status": job["status"]}), 409
        _record_generation_view(job_id)
        return _inject_watermark(job["html"], job_id, show_toast=show_toast), 200, {"Content-Type": "text/html; charset=utf-8"}

    db = SessionLocal()
    try:
        gen = db.query(Generation).join(Lead).filter(Lead.public_id == job_id).first()
        if gen:
            # For live sites, show the pending version in the editor preview so
            # the customer can see their accumulated change requests reflected.
            html = (gen.html_pending or gen.html_content) if gen.status == "live" else gen.html_content
            _record_generation_view(job_id)
            return _inject_watermark(html, job_id, show_toast=show_toast), 200, {"Content-Type": "text/html; charset=utf-8"}
    finally:
        db.close()
    return jsonify({"error": "not found"}), 404


@app.route("/api/generate/<job_id>/engagement", methods=["POST"])
def job_engagement(job_id):
    """Receives a navigator.sendBeacon() report from the tracking script
    _inject_watermark() embeds in every served generation (see that
    function) — fired on tab-hide/pagehide, reporting time spent and
    deepest scroll reached SINCE THE LAST report (a delta, not
    cumulative-since-load, specifically so repeated tab-switching in a
    single visit can't inflate total_view_seconds). No auth — same-origin
    browser beacon, nothing sensitive, worst case of abuse is noise in one
    generation's own stats, not a security issue."""
    data = request.get_json(silent=True) or {}
    try:
        seconds = max(0, min(int(data.get("seconds", 0)), 3600))
    except (TypeError, ValueError):
        seconds = 0
    try:
        scroll_pct = max(0, min(int(data.get("scroll_pct", 0)), 100))
    except (TypeError, ValueError):
        scroll_pct = 0

    db = SessionLocal()
    try:
        gen = db.query(Generation).join(Lead).filter(Lead.public_id == job_id).first()
        if gen:
            gen.total_view_seconds = (gen.total_view_seconds or 0) + seconds
            gen.max_scroll_pct = max(gen.max_scroll_pct or 0, scroll_pct)
            db.commit()
    finally:
        db.close()
    return "", 204


@app.route("/api/generate/<job_id>/preserved")
def job_html_preserved(job_id):
    """
    Serves a canceled subscription's site fully intact, at the same
    unguessable token (Lead.public_id) the rest of the app already uses for
    private links (/preview.html, /editor.html, etc.) — reusing that
    pattern rather than building a new one, since it's already the
    established "reachable only with this exact link" mechanism here.

    Deliberately a separate route from /api/generate/<job_id>/html, not a
    branch inside it: that route's _inject_watermark() banner says "this
    site is unpublished" with a "Get it live today, free" CTA, which is
    actively wrong for a site that WAS live and paid for. This route shows
    the real content with an accurate, distinct banner instead.

    Only serves anything for status=="canceled" — a live or draft site
    isn't reachable via this path, so this can't be used as a bypass around
    normal serving rules for anything else.
    """
    db = SessionLocal()
    try:
        gen = db.query(Generation).join(Lead).filter(Lead.public_id == job_id).first()
        if not gen or gen.status != "canceled":
            return jsonify({"error": "not found"}), 404
        # Deep-links straight to this site's checkout (same page/id format
        # the dashboard's own "Reactivate" card uses) rather than
        # /account/login — that used to drop job_id entirely and force an
        # extra unnecessary login hop through the generic dashboard before
        # the customer could get back to actually paying. No login is
        # needed to reach checkout.html at all (job_id is already the
        # capability token for this whole flow, same as /preview.html).
        banner = f"""<div style="position:fixed;top:0;left:0;right:0;z-index:99999;background:#1C2630;color:#fff;font-family:sans-serif;font-size:13px;display:flex;align-items:center;justify-content:space-between;padding:10px 20px;box-shadow:0 2px 8px rgba(0,0,0,0.3);">
  <span>This site's subscription is currently paused — your content is untouched.</span>
  <a href="/checkout.html?id={job_id}" style="background:#3B82F6;color:#fff;padding:6px 16px;border-radius:4px;text-decoration:none;font-weight:600;">Reactivate →</a>
</div>
<div style="height:44px;"></div>"""
        return banner + gen.html_content, 200, {"Content-Type": "text/html; charset=utf-8"}
    finally:
        db.close()


@app.route("/api/generate/<job_id>/info")
def job_info(job_id):
    """Return metadata used by checkout.html and live.html — status, and
    the assigned address itself once one exists (i.e. post-payment, once
    gen.subdomain is set).

    Deliberately does NOT return the pre-payment candidate address itself
    (neither the bare slug nor a preview URL) — the literal string is only
    revealed once payment actually goes through, per the 2026-07-19 change
    to stop showing it beforehand. No longer returns invalid-chars/taken
    booleans either (removed 2026-07-23) — _resolve_subdomain (app.py)
    guarantees a usable, available address by construction (stripping bad
    characters, suffixing on a collision) rather than checkout.html
    needing to grey out the pay button over it."""
    db = SessionLocal()
    try:
        gen = db.query(Generation).join(Lead).filter(Lead.public_id == job_id).first()
        if not gen:
            return jsonify({"error": "not found"}), 404
        business_name = (gen.lead.form_data or {}).get("business_name", "")
        assigned_url = (
            f"https://{gen.subdomain}.{_SUBDOMAIN_BASE}" if gen.subdomain else None
        )
        return jsonify({
            "status": gen.status,
            "business_name": business_name,
            "subdomain": gen.subdomain,
            "subdomain_url": assigned_url,
            "receipt_pdf_url": _fetch_invoice_pdf(gen.stripe_setup_invoice_id),
        })
    finally:
        db.close()


def _fetch_invoice_pdf(invoice_id: str) -> str | None:
    """Best-effort invoice_pdf lookup — returns None (never raises) if there's
    no invoice on file yet, Stripe isn't configured, or the PDF isn't ready."""
    if not invoice_id or not STRIPE_SECRET_KEY:
        return None
    try:
        invoice = stripe.Invoice.retrieve(invoice_id)
        return invoice.invoice_pdf or None
    except stripe.error.StripeError:
        return None


def _format_gbp(amount_cents: int) -> str:
    return f"£{amount_cents / 100:,.2f}"


@app.route("/api/generate/<job_id>/text-fields")
def get_text_fields(job_id):
    """Return all data-gw-text fields from a generation's stored HTML as JSON."""
    db = SessionLocal()
    try:
        gen = db.query(Generation).join(Lead).filter(Lead.public_id == job_id).first()
        if not gen:
            return jsonify({"error": "not found"}), 404
        # For live sites, extract from pending state if any so the editor reflects
        # the customer's already-requested edits rather than the current live HTML.
        html = (gen.html_pending or gen.html_content) if gen.status == "live" else gen.html_content
        fields = _extract_gw_text_fields(html or "")
        return jsonify({
            "fields": fields,
            "editable": bool(fields),
            "status": gen.status,
            "has_pending": bool(gen.html_pending),
        })
    finally:
        db.close()


@app.route("/api/generate/<job_id>/text", methods=["PATCH"])
def update_text_field(job_id):
    """Replace the text content of a single data-gw-text element.
    Draft sites: saves directly to html_content (immediately visible in preview).
    Live sites: saves to html_pending so the live subdomain is unchanged until
    admin reviews and applies the changes."""
    db = SessionLocal()
    try:
        gen = db.query(Generation).join(Lead).filter(Lead.public_id == job_id).first()
        if not gen:
            return jsonify({"error": "not found"}), 404
        data = request.get_json() or {}
        field_id = (data.get("id") or "").strip()
        new_text = (data.get("content") or "").strip()
        if not field_id:
            return jsonify({"error": "id required"}), 400
        if len(new_text) > 1000:
            return jsonify({"error": "Text too long (max 1000 characters)."}), 400

        if gen.status == "live":
            # Accumulate change requests in html_pending; live HTML is untouched.
            base_html = gen.html_pending or gen.html_content or ""
            new_html, ok = _update_gw_text_field(base_html, field_id, new_text)
            if not ok:
                return jsonify({"error": "Field not found in this generation."}), 404
            gen.html_pending = new_html
        else:
            new_html, ok = _update_gw_text_field(gen.html_content or "", field_id, new_text)
            if not ok:
                return jsonify({"error": "Field not found in this generation."}), 404
            gen.html_content = new_html
            with _jobs_lock:
                if job_id in _jobs and _jobs[job_id].get("status") == "done":
                    _jobs[job_id]["html"] = new_html

        # First-edit stamp for the Funnel page's engagement stat (see
        # Generation.text_edited_at's docstring) — set once, never
        # overwritten, so it always reflects when this customer FIRST made
        # a real edit rather than their most recent one.
        if gen.text_edited_at is None:
            gen.text_edited_at = datetime.utcnow()

        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


_PHOTO_MAX_UPLOAD_BYTES = 12 * 1024 * 1024  # 12MB — same order of magnitude as the build-form's own upload cap
_PHOTO_MAX_CARDS = 12  # sane ceiling so "add photo" can't be used to grow an unbounded page
_PHOTO_MAX_DIMENSION = 1600  # matches the portfolio-photo size used at generation time


def _gen_photo_html(gen):
    """Which HTML a photo-manager mutation should read/write — html_pending
    for a live site (same accumulate-until-applied model as text edits),
    html_content otherwise."""
    if gen.status == "live":
        return gen.html_pending or gen.html_content or ""
    return gen.html_content or ""


def _set_gen_photo_html(gen, job_id, new_html):
    if gen.status == "live":
        gen.html_pending = new_html
    else:
        gen.html_content = new_html
        with _jobs_lock:
            if job_id in _jobs and _jobs[job_id].get("status") == "done":
                _jobs[job_id]["html"] = new_html
    if gen.text_edited_at is None:
        gen.text_edited_at = datetime.utcnow()


@app.route("/api/generate/<job_id>/photos", methods=["GET"])
def get_photos(job_id):
    """List this generation's portfolio photos for the editor's photo
    manager. GenerationImage is the source of truth for listing (every
    photo/logo already gets a row there at persist time — see
    _run_and_persist) — no HTML scanning needed just to show the list.
    editable reflects whether the stored HTML actually has the
    data-gw-photo-grid marker build_prompt.py now bakes in — generations
    from before this feature have photos but no marker, so add/delete
    would have nowhere reliable to operate; the frontend shows a "not
    available for this site" state in that case, same pattern as text
    editing's old no-fields-state."""
    db = SessionLocal()
    try:
        gen = db.query(Generation).join(Lead).filter(Lead.public_id == job_id).first()
        if not gen:
            return jsonify({"error": "not found"}), 404
        photos = db.query(GenerationImage).filter(
            GenerationImage.generation_id == gen.id,
            GenerationImage.slot.like("photo_%"),
        ).order_by(GenerationImage.slot).all()
        html = _gen_photo_html(gen)
        return jsonify({
            "photos": [{"slot": p.slot, "data_uri": p.data_uri, "caption": p.caption or ""} for p in photos],
            "editable": "data-gw-photo-grid" in html,
            "can_add": _last_photo_card_slot(html) is not None and len(photos) < _PHOTO_MAX_CARDS,
            "status": gen.status,
        })
    finally:
        db.close()


@app.route("/api/generate/<job_id>/photos", methods=["POST"])
def add_photo(job_id):
    """Upload a new portfolio photo — resized/re-encoded the same way as a
    photo uploaded at generation time, cloned into a new card matching the
    site's existing card styling (see _add_gw_photo_card's docstring for
    why cloning, not hand-building, a card)."""
    if "photo" not in request.files or not request.files["photo"].filename:
        return jsonify({"error": "No photo file provided."}), 400
    file = request.files["photo"]
    caption = (request.form.get("caption") or "").strip()[:200]

    file.seek(0, os.SEEK_END)
    size = file.tell()
    file.seek(0)
    if size > _PHOTO_MAX_UPLOAD_BYTES:
        return jsonify({"error": "Photo too large (max 12MB)."}), 400

    try:
        raw = Image.open(file.stream)
        raw.load()  # force-read now — a truncated/invalid file raises here, not later
        img = raw.convert("RGBA") if raw.mode in ("RGBA", "LA", "P") else raw.convert("RGB")
        data_uri = _encode_pil_image_to_data_uri(img, _PHOTO_MAX_DIMENSION)
    except Exception:
        return jsonify({"error": "Couldn't read that as an image. Try a JPEG or PNG."}), 400

    db = SessionLocal()
    try:
        gen = db.query(Generation).join(Lead).filter(Lead.public_id == job_id).first()
        if not gen:
            return jsonify({"error": "not found"}), 404

        html = _gen_photo_html(gen)
        # Union of GenerationImage rows AND whatever's actually in the HTML
        # — these should always agree in practice (every card gets a row at
        # persist time), but computing the next free slot from the DB alone
        # would risk colliding with an existing card if they ever drifted.
        existing_slots = {
            row[0] for row in db.query(GenerationImage.slot).filter(
                GenerationImage.generation_id == gen.id, GenerationImage.slot.like("photo_%"),
            ).all()
        }
        existing_slots |= set(re.findall(r'data-gw-photo-card="(photo_\d+)"', html))
        if len(existing_slots) >= _PHOTO_MAX_CARDS:
            return jsonify({"error": f"Maximum {_PHOTO_MAX_CARDS} portfolio photos reached."}), 422
        n = 0
        while f"photo_{n}" in existing_slots:
            n += 1
        new_slot = f"photo_{n}"

        new_html, ok = _add_gw_photo_card(html, new_slot, data_uri, caption)
        if not ok:
            return jsonify({"error": "This site has no existing portfolio photo to use as a template — can't add one here."}), 422

        db.add(GenerationImage(generation_id=gen.id, slot=new_slot, data_uri=data_uri,
                                mime=_data_uri_mime(data_uri), caption=caption or None))
        _set_gen_photo_html(gen, job_id, new_html)
        db.commit()
        return jsonify({"ok": True, "slot": new_slot, "data_uri": data_uri, "caption": caption})
    finally:
        db.close()


@app.route("/api/generate/<job_id>/photos/<slot>", methods=["DELETE"])
def delete_photo(job_id, slot):
    """Remove a portfolio photo entirely — its card from the HTML and its
    GenerationImage row."""
    db = SessionLocal()
    try:
        gen = db.query(Generation).join(Lead).filter(Lead.public_id == job_id).first()
        if not gen:
            return jsonify({"error": "not found"}), 404

        html = _gen_photo_html(gen)
        new_html, ok = _remove_gw_photo_card(html, slot)
        if not ok:
            return jsonify({"error": "Photo not found in this generation."}), 404

        img_row = db.query(GenerationImage).filter(
            GenerationImage.generation_id == gen.id, GenerationImage.slot == slot,
        ).first()
        if img_row:
            db.delete(img_row)
        _set_gen_photo_html(gen, job_id, new_html)
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


@app.route("/api/generate/<job_id>/photos/<slot>/caption", methods=["PATCH"])
def update_photo_caption(job_id, slot):
    """Set/clear a single photo's caption — independent of deleting or
    replacing the photo itself."""
    data = request.get_json(silent=True) or {}
    new_caption = (data.get("caption") or "").strip()[:200]

    db = SessionLocal()
    try:
        gen = db.query(Generation).join(Lead).filter(Lead.public_id == job_id).first()
        if not gen:
            return jsonify({"error": "not found"}), 404

        html = _gen_photo_html(gen)
        new_html, ok = _update_gw_caption_field(html, slot, new_caption)
        if not ok:
            return jsonify({"error": "Photo not found in this generation."}), 404

        img_row = db.query(GenerationImage).filter(
            GenerationImage.generation_id == gen.id, GenerationImage.slot == slot,
        ).first()
        if img_row:
            img_row.caption = new_caption or None
        _set_gen_photo_html(gen, job_id, new_html)
        db.commit()
        return jsonify({"ok": True, "caption": new_caption})
    finally:
        db.close()


# Supported TLDs. All standard domains at these are flat-rate — no
# registry-level premiums for .co.uk/.com/.uk/.org.uk. Premium-sounding names
# (roofing.com) are already registered so WHOIS catches them as taken before
# payment is possible.
_SUPPORTED_TLDS = ["co.uk", "com", "uk", "org.uk", "net", "org", "biz", "uk.com"]

# USD→GBP conversion applied to Porkbun's (USD) wholesale prices. Porkbun's
# pricing/get API doesn't offer GBP directly, so this rate is the one manual
# number left in the pricing flow — everything else (the wholesale prices
# themselves) is fetched live below, not hardcoded.
_USD_TO_GBP = 0.79

# Static fallback wholesale costs (GBP), used only if a live Porkbun
# pricing/get call fails and no prior successful fetch is cached (e.g. cold
# start with Porkbun briefly down). Verified 2026-07-08 — expect these to
# drift; they're a safety net, not the source of truth.
_TLD_PRICE_GBP_FALLBACK = {
    "co.uk":  round(5.66 * _USD_TO_GBP, 2),
    "com":    round(11.08 * _USD_TO_GBP, 2),
    "uk":     round(5.66 * _USD_TO_GBP, 2),
    "org.uk": round(5.66 * _USD_TO_GBP, 2),
    "net":    round(12.52 * _USD_TO_GBP, 2),
    "org":    round(7.98 * _USD_TO_GBP, 2),
    "biz":    round(6.69 * _USD_TO_GBP, 2),
    "uk.com": round(22.63 * _USD_TO_GBP, 2),
}

_porkbun_pricing_cache = {"data": None, "fetched_at": 0.0}
_PORKBUN_PRICING_TTL_SECONDS = 6 * 3600  # 6h — pricing/get is a heavy-ish call covering every TLD

# Live USD→GBP rate for the admin funnel page's "avg generation cost"
# stat (added 2026-07-23) — deliberately separate from _USD_TO_GBP above,
# which is a manually-set constant for domain-margin math, not a live rate.
# frankfurter.app is ECB-sourced, free, and needs no API key. Cached for
# _FX_RATE_TTL_SECONDS so an admin page refresh doesn't hit it every time;
# falls back to _USD_TO_GBP (stale but safe) if the fetch fails.
_fx_rate_cache = {"rate": None, "fetched_at": 0.0}
_FX_RATE_TTL_SECONDS = 3600  # 1h


def _live_usd_to_gbp_rate() -> float:
    now = time.time()
    if _fx_rate_cache["rate"] and (now - _fx_rate_cache["fetched_at"] < _FX_RATE_TTL_SECONDS):
        return _fx_rate_cache["rate"]
    try:
        # frankfurter.app 403s on urllib's default User-Agent — confirmed
        # live, not a hypothetical — a real header value is required.
        req = urllib.request.Request(
            "https://api.frankfurter.app/latest?from=USD&to=GBP",
            headers={"User-Agent": "Mozilla/5.0 (compatible; GroundworkAdmin/1.0)"},
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode())
        rate = float(data["rates"]["GBP"])
        _fx_rate_cache["rate"] = rate
        _fx_rate_cache["fetched_at"] = now
        return rate
    except Exception as exc:
        app.logger.warning(f"Live USD->GBP rate fetch failed, using static fallback {_USD_TO_GBP}: {exc}")
        return _USD_TO_GBP


def _tld_price_gbp() -> dict:
    """Live wholesale cost per supported TLD (GBP), fetched from Porkbun's
    pricing/get API and cached for _PORKBUN_PRICING_TTL_SECONDS so a burst of
    domain searches doesn't hammer Porkbun. Falls back to the last
    successful fetch, then to the static _TLD_PRICE_GBP_FALLBACK table, if
    Porkbun is unreachable — search/checkout must never hard-fail just
    because a pricing refresh failed."""
    now = time.time()
    cached = _porkbun_pricing_cache["data"]
    if cached and (now - _porkbun_pricing_cache["fetched_at"] < _PORKBUN_PRICING_TTL_SECONDS):
        return cached
    try:
        result = _porkbun_post("pricing/get")
        if result.get("status") != "SUCCESS":
            raise RuntimeError(str(result))
        pricing = result.get("pricing", {})
        fresh = {}
        for tld in _SUPPORTED_TLDS:
            entry = pricing.get(tld)
            if not entry:
                continue
            usd = float(entry.get("registration", 0))
            fresh[tld] = round(usd * _USD_TO_GBP, 2)
        if fresh:
            _porkbun_pricing_cache["data"] = fresh
            _porkbun_pricing_cache["fetched_at"] = now
            return fresh
        raise RuntimeError("pricing/get returned no matching TLDs")
    except Exception as exc:
        app.logger.warning(f"Live Porkbun pricing fetch failed, using {'cached' if cached else 'static fallback'} prices: {exc}")
        return cached or _TLD_PRICE_GBP_FALLBACK


def _sale_price_gbp(wholesale_gbp: float) -> float:
    """Customer-facing sale price, derived live from wholesale cost rather than
    hardcoded per TLD: double the wholesale cost, then round up to the nearest
    value ending in .99. Guarantees >=100% margin on every domain and stays
    correct automatically as Porkbun's wholesale prices change (see
    _tld_price_gbp, which fetches those prices live)."""
    return math.floor(wholesale_gbp * 2) + 0.99


_WHOIS_SERVERS = {
    "co.uk":  "whois.nic.uk",
    "uk":     "whois.nic.uk",
    "org.uk": "whois.nic.uk",
    "com":    "whois.verisign-grs.com",
    "net":    "whois.verisign-grs.com",
    "org":    "whois.pir.org",
    "biz":    "whois.nic.biz",
    "uk.com": "whois.centralnic.com",
}

# .biz uses "no data found" for available; all others use "no match"/"not found"
_WHOIS_AVAILABLE_MARKERS = {
    "biz": ["no data found"],
}
_WHOIS_AVAILABLE_DEFAULT = ["no match", "not found"]


def _whois_available(domain: str) -> bool:
    """Authoritative availability check via WHOIS. Catches parked domains DNS misses."""
    import socket as _socket
    domain = domain.lower().strip()
    tld = ".".join(domain.split(".")[1:])
    server = _WHOIS_SERVERS.get(tld, "whois.iana.org")
    markers = _WHOIS_AVAILABLE_MARKERS.get(tld, _WHOIS_AVAILABLE_DEFAULT)
    try:
        s = _socket.create_connection((server, 43), timeout=8)
        s.sendall((domain + "\r\n").encode())
        resp = b""
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            resp += chunk
        s.close()
        text = resp.decode(errors="replace").lower()
        return any(m in text for m in markers)
    except Exception as exc:
        app.logger.warning(f"WHOIS check failed for {domain}: {exc}")
        return False  # fail safe — don't offer domains we couldn't verify


def _check_domain(domain: str) -> dict:
    """Return availability + sale price for one domain. Never raises — errors get error=True."""
    tld = ".".join(domain.split(".")[1:])
    price_gbp = _sale_price_gbp(_tld_price_gbp().get(tld, 0.0))
    try:
        available = _whois_available(domain)
        return {"domain": domain, "available": available, "price_gbp": price_gbp}
    except Exception as exc:
        app.logger.warning(f"Domain check failed for {domain}: {exc}")
        return {"domain": domain, "available": None, "price_gbp": price_gbp, "error": True}


@app.route("/api/domain/search")
def domain_search():
    """Check domain availability for a business name query via WHOIS.
    Accepts ?tlds=co.uk,com,uk (comma-separated). Checks all TLD × name variants in parallel."""
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "unknown").split(",")[0].strip()
    # Each request fans out up to ~16 parallel WHOIS lookups (candidates x
    # TLDs) — a generous per-request limit still caps sustained WHOIS load
    # from a single client to a level that can't realistically get us
    # rate-limited/blocked by upstream registries.
    if _rate_limited("domain_search", ip, limit=30, window_seconds=300):
        return jsonify({"error": "rate_limited", "message": "Too many domain searches — please wait a moment."}), 429
    query = request.args.get("q", "").strip()
    tlds_param = request.args.get("tlds", "").strip()

    if not query:
        return jsonify({"results": []})

    condensed  = re.sub(r'[^a-z0-9]', '', query.lower())
    hyphenated = re.sub(r'[^a-z0-9]+', '-', query.lower()).strip('-')

    if not condensed:
        return jsonify({"results": []})

    if tlds_param:
        tlds = [t.strip().lstrip('.') for t in tlds_param.split(',') if t.strip()]
        tlds = [t for t in tlds if t in _SUPPORTED_TLDS]
    else:
        tlds = ["co.uk", "com", "uk"]
    if not tlds:
        tlds = ["co.uk", "com", "uk"]

    seen = set()
    candidates = []
    for base in [condensed, hyphenated]:
        for tld in tlds:
            d = f"{base}.{tld}"
            if d not in seen:
                seen.add(d)
                candidates.append(d)

    with ThreadPoolExecutor(max_workers=min(len(candidates), 12)) as pool:
        checked = list(pool.map(_check_domain, candidates))

    # Mark the first available domain as best_match
    best_set = False
    for item in checked:
        if not best_set and item.get("available"):
            item["best_match"] = True
            best_set = True
        else:
            item.setdefault("best_match", False)

    return jsonify({"results": checked})


@app.route("/api/domain/confirm")
def domain_confirm():
    """Fresh WHOIS availability check + confirmed sale price. Source of truth for checkout.
    Availability is checked against the live domain regardless of pricing; the
    price returned to the customer is always the marked-up sale price, never
    the wholesale cost used internally for the availability/pricing lookup."""
    domain = request.args.get("domain", "").strip().lower()
    if not domain or "." not in domain:
        return jsonify({"error": "invalid domain"}), 400

    tld = ".".join(domain.split(".")[1:])
    wholesale_gbp = _tld_price_gbp().get(tld)
    if wholesale_gbp is None:
        return jsonify({"error": "unsupported TLD"}), 400
    price_gbp = _sale_price_gbp(wholesale_gbp)

    available = _whois_available(domain)
    return jsonify({"domain": domain, "available": available, "price_gbp": price_gbp})


@app.route("/api/domain/checkout/session", methods=["POST"])
def domain_checkout_session():
    """
    Stripe Checkout session for domain registration — a yearly recurring
    subscription as of 2026-07-14, not a one-time payment (existing domains
    sold before this change stay one-time/grandfathered; see
    docs/outreach-pipeline-spec.md's domain-billing notes and
    _domain_repricing_job for the repricing side of this).

    Uses inline price_data with recurring={"interval": "year"} rather than
    a pre-created Stripe Price object, since the amount varies per domain/
    TLD (unlike the fixed site-hosting plan prices) — Stripe Checkout
    supports a recurring price defined inline the same way it supports a
    one-time price inline, so no per-domain Product/Price has to be
    pre-provisioned.

    Re-confirms price/availability server-side, same as before.
    """
    if not STRIPE_SECRET_KEY:
        return jsonify({"error": "Stripe is not configured"}), 503

    data = request.get_json(silent=True) or {}
    domain  = str(data.get("domain", "")).strip().lower()
    site_id = str(data.get("site_id", "")).strip()

    if not domain or "." not in domain:
        return jsonify({"error": "invalid domain"}), 400

    tld = ".".join(domain.split(".")[1:])
    wholesale_gbp = _tld_price_gbp().get(tld)
    if wholesale_gbp is None:
        return jsonify({"error": "unsupported TLD"}), 400
    price_gbp = _sale_price_gbp(wholesale_gbp)

    if not _whois_available(domain):
        return jsonify({"error": "domain_taken",
                        "message": "This domain was registered by someone else just now. Please choose another."}), 409

    # Porkbun pre-flight dry run: validates availability, pricing, account funds,
    # and eligibility without charging or creating anything. If Porkbun would
    # reject the real registration, we don't let the customer pay for it.
    # Skipped if Porkbun keys aren't configured (dev/test environments).
    if PORKBUN_API_KEY and PORKBUN_SECRET_KEY:
        try:
            preflight_cents = _porkbun_check_price_cents(domain)
        except RuntimeError as exc:
            return jsonify({"error": "domain_unavailable", "message": str(exc)}), 409
        dry = _porkbun_post(f"domain/create/{domain}", {
            "cost": preflight_cents,
            "agreeToTerms": "yes",
            "dryRun": True,
        })
        if dry.get("status") != "SUCCESS" or not dry.get("wouldSucceed"):
            return jsonify({
                "error": "preflight_failed",
                "message": dry.get("message", "This domain cannot be registered right now. Please try a different domain."),
            }), 409

    cancel_url = f"{SITE_URL}/domain-checkout.html?domain={domain}"
    if site_id:
        cancel_url += f"&id={site_id}"

    cs = stripe.checkout.Session.create(
        mode="subscription",
        line_items=[{
            "price_data": {
                "currency": "gbp",
                "product_data": {
                    "name": f"Domain registration: {domain}",
                    "description": "Renews yearly via Groundwork. We'll connect it to your site automatically.",
                },
                "unit_amount": int(round(price_gbp * 100)),
                "recurring": {"interval": "year"},
            },
            "quantity": 1,
        }],
        metadata={"type": "domain", "domain": domain, "site_id": site_id,
                  "price_gbp": str(price_gbp), "wholesale_gbp": str(wholesale_gbp)},
        client_reference_id=site_id or domain,
        success_url=f"{SITE_URL}/domain-ordered.html?domain={domain}",
        cancel_url=cancel_url,
    )
    return jsonify({"url": cs.url})


@app.route("/api/domain/status")
def api_domain_status():
    """Public status endpoint for a domain row, keyed by domain name.
    Returns enough info for the status page to render the pipeline steps."""
    domain = request.args.get("domain", "").strip().lower()
    if not domain or "." not in domain:
        return jsonify({"error": "missing domain"}), 400
    db = SessionLocal()
    try:
        dom = db.query(Domain).filter(Domain.domain == domain).first()
        if not dom:
            return jsonify({"error": "not_found"}), 404
        return jsonify({
            "domain":                 dom.domain,
            "status":                 dom.status,
            "error_step":             dom.error_step,
            "error_message":          dom.error_message,
            "registered_at":          dom.registered_at.isoformat() if dom.registered_at else None,
            "cloudflare_connected_at": dom.cloudflare_connected_at.isoformat() if dom.cloudflare_connected_at else None,
            "dns_configured_at":      dom.dns_configured_at.isoformat() if dom.dns_configured_at else None,
            "live_email_sent_at":     dom.live_email_sent_at.isoformat() if dom.live_email_sent_at else None,
            "created_at":             dom.created_at.isoformat() if dom.created_at else None,
        })
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Domain registration automation (called from Stripe webhook)
# ---------------------------------------------------------------------------

def _porkbun_post(endpoint: str, extra: dict = None) -> dict:
    """POST to Porkbun API v3. Returns parsed JSON. Raises on HTTP error."""
    import urllib.error as _urlerr
    url = f"https://api.porkbun.com/api/json/v3/{endpoint}"
    payload = {"apikey": PORKBUN_API_KEY, "secretapikey": PORKBUN_SECRET_KEY}
    if extra:
        payload.update(extra)
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except _urlerr.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode()
        except Exception:
            pass
        raise RuntimeError(f"Porkbun HTTP {exc.code} on {endpoint}: {body or '(empty body)'}") from exc


def _porkbun_check_price_cents(domain: str) -> int:
    """Fresh domain/checkDomain call — returns current registration price in USD cents.
    Raises RuntimeError if the domain is unavailable or the API call fails.
    Domain is a URL path segment per Porkbun v3 API."""
    result = _porkbun_post(f"domain/checkDomain/{domain}")
    if result.get("status") != "SUCCESS":
        raise RuntimeError(f"Porkbun domain/checkDomain: {result.get('message', result)}")
    resp = result.get("response", {})
    if resp.get("avail") not in ("yes", True, 1):
        raise RuntimeError(f"Domain {domain} is not available according to Porkbun")
    price_usd = float(resp.get("price", 0))
    return int(round(price_usd * 100))


def _porkbun_register_domain(domain: str) -> None:
    """Register a domain for 1 year via Porkbun. Raises RuntimeError on failure.

    Gets a fresh price from checkDomain immediately before registering and
    passes it as `cost` (integer USD cents) so an unexpected price change
    between Stripe checkout and webhook fires a hard error, not a silent
    overcharge. Domain is a URL path segment per Porkbun v3 API."""
    cost_cents = _porkbun_check_price_cents(domain)
    result = _porkbun_post(f"domain/create/{domain}", {
        "cost": cost_cents,
        "agreeToTerms": "yes",
    })
    if result.get("status") != "SUCCESS":
        raise RuntimeError(f"Porkbun domain/create: {result.get('message', result)}")


def _porkbun_create_dns(domain: str, record_type: str, name: str, content: str) -> None:
    """Create a DNS record via Porkbun. Raises RuntimeError on failure."""
    result = _porkbun_post(f"dns/create/{domain}", {
        "type": record_type,
        "name": name,
        "content": content,
        "ttl": "300",
    })
    if result.get("status") != "SUCCESS":
        raise RuntimeError(f"Porkbun dns/create ({record_type} {name}): {result.get('message', result)}")


def _porkbun_set_autorenew(domain: str, on: bool) -> None:
    """
    Toggle Porkbun's auto-renewal for a registered domain — confirmed real
    via a live (non-mutating) probe against Porkbun's API before this was
    written: domain/updateAutoRenew/{domain} exists and responded with
    "You need to pass a status of on or off" when called with no body,
    which is Porkbun's own validation message, not a guess from docs.

    Turning this off on cancellation stops Groundwork's card from being
    charged to renew a domain nobody's using, without releasing the domain
    itself — the registration (and the customer's ownership of it) is
    untouched; it just won't silently renew when it expires. This is
    exactly the "grace window" the cancellation flow needs: DNS/Cloudflare
    disconnection happens immediately, but the domain asset itself isn't
    given up until a human decides not to renew it.

    Raises RuntimeError on failure.
    """
    result = _porkbun_post(f"domain/updateAutoRenew/{domain}", {
        "status": "on" if on else "off",
    })
    if result.get("status") != "SUCCESS":
        raise RuntimeError(f"Porkbun domain/updateAutoRenew ({domain}): {result.get('message', result)}")


def _cloudflare_add_custom_hostname(hostname: str) -> None:
    """Register a customer domain as a Custom Hostname on our Cloudflare zone
    (Cloudflare for SaaS), using standard DV (domain-validated) SSL validated
    over HTTP — this works because DNS for the hostname is pointed at our
    Cloudflare CNAME target, which lets Cloudflare's edge complete the HTTP
    validation request on the customer's behalf once DNS is live.

    Cloudflare matches custom hostnames on the exact hostname (apex vs. www
    are different hostnames to it; wildcard custom hostnames are a separate,
    paid SaaS tier) — so apex and www must each be registered as their own
    Custom Hostname. Call this once per hostname.

    Raises RuntimeError on failure."""
    if not CLOUDFLARE_API_TOKEN_CUSTOM_HOSTNAMES:
        raise RuntimeError("CLOUDFLARE_API_TOKEN_CUSTOM_HOSTNAMES not set — add it in Railway environment variables")
    if not CLOUDFLARE_ZONE_ID:
        raise RuntimeError("CLOUDFLARE_ZONE_ID not set — add it in Railway environment variables")

    payload = json.dumps({
        "hostname": hostname,
        "ssl": {
            "method": "http",
            "type": "dv",
        },
    }).encode()
    req = urllib.request.Request(
        f"{CLOUDFLARE_API_URL}/zones/{CLOUDFLARE_ZONE_ID}/custom_hostnames",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {CLOUDFLARE_API_TOKEN_CUSTOM_HOSTNAMES}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        raise RuntimeError(f"Cloudflare custom_hostnames ({hostname}): HTTP {exc.code}: {body}")

    if not result.get("success"):
        errors = result.get("errors", result)
        # Code 1406 = hostname already exists as a custom hostname on this zone
        # (e.g. a retry after a partial earlier failure) — treat as success.
        if any(e.get("code") == 1406 for e in result.get("errors", []) if isinstance(e, dict)):
            return
        raise RuntimeError(f"Cloudflare custom_hostnames ({hostname}): {errors}")


def _cloudflare_ssl_status(hostname: str) -> str | None:
    """Return the ssl.status string for a Custom Hostname, or None if not found.
    Cloudflare issues SSL async after hostname creation; poll until 'active'."""
    req = urllib.request.Request(
        f"{CLOUDFLARE_API_URL}/zones/{CLOUDFLARE_ZONE_ID}/custom_hostnames?hostname={hostname}",
        headers={
            "Authorization": f"Bearer {CLOUDFLARE_API_TOKEN_CUSTOM_HOSTNAMES}",
            "Content-Type": "application/json",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            result = json.loads(resp.read().decode())
    except Exception as exc:
        app.logger.warning(f"Cloudflare SSL status check failed for {hostname}: {exc}")
        return None
    results = result.get("result", [])
    if not results:
        return None
    return results[0].get("ssl", {}).get("status")


def _cloudflare_get_custom_hostname_id(hostname: str) -> str | None:
    """Return the Cloudflare-side Custom Hostname object id for a hostname,
    or None if it isn't registered. We don't store this id anywhere — it's
    looked up by hostname on demand, same pattern _cloudflare_ssl_status
    already uses."""
    req = urllib.request.Request(
        f"{CLOUDFLARE_API_URL}/zones/{CLOUDFLARE_ZONE_ID}/custom_hostnames?hostname={hostname}",
        headers={
            "Authorization": f"Bearer {CLOUDFLARE_API_TOKEN_CUSTOM_HOSTNAMES}",
            "Content-Type": "application/json",
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        result = json.loads(resp.read().decode())
    results = result.get("result", [])
    return results[0]["id"] if results else None


def _cloudflare_delete_custom_hostname(hostname: str) -> None:
    """
    Deregisters a Custom Hostname from our Cloudflare for SaaS zone — used
    on subscription cancellation to stop a custom domain from serving the
    site. What this does and doesn't do, confirmed against Cloudflare's own
    API/docs rather than assumed:

    - It removes the Custom Hostname object and its SSL certificate from
      OUR zone. Cloudflare will no longer terminate TLS or route traffic
      for that hostname to our Fallback Origin — this is what actually
      stops the site serving on the domain.
    - It does NOT touch the domain's own DNS records at the registrar
      (Porkbun). The customer's CNAME/ALIAS still points at our
      CLOUDFLARE_CNAME_TARGET exactly as before — that pointer is now just
      inert (resolves to Cloudflare's edge, but Cloudflare has nothing
      registered for that exact hostname to route it to). No DNS-side
      cleanup is required for the domain to stop serving.
    - It's fully reversible: re-running _cloudflare_add_custom_hostname()
      for the same hostname re-registers it and Cloudflare re-validates SSL
      the same way it did originally (DV over HTTP) — the DNS was never
      disturbed, so this is normally fast. This is what the "reinstate"
      flow uses.

    Treats "hostname not found" as success (idempotent — matches the
    already-exists-is-success handling in _cloudflare_add_custom_hostname).
    """
    if not CLOUDFLARE_API_TOKEN_CUSTOM_HOSTNAMES or not CLOUDFLARE_ZONE_ID:
        raise RuntimeError("CLOUDFLARE_API_TOKEN_CUSTOM_HOSTNAMES/CLOUDFLARE_ZONE_ID not set")

    hostname_id = _cloudflare_get_custom_hostname_id(hostname)
    if not hostname_id:
        return  # nothing registered — already effectively disconnected

    req = urllib.request.Request(
        f"{CLOUDFLARE_API_URL}/zones/{CLOUDFLARE_ZONE_ID}/custom_hostnames/{hostname_id}",
        headers={"Authorization": f"Bearer {CLOUDFLARE_API_TOKEN_CUSTOM_HOSTNAMES}"},
        method="DELETE",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        raise RuntimeError(f"Cloudflare custom_hostnames DELETE ({hostname}): HTTP {exc.code}: {body}")
    if not result.get("success"):
        raise RuntimeError(f"Cloudflare custom_hostnames DELETE ({hostname}): {result.get('errors', result)}")


def _cloudflare_wait_for_ssl(hostname: str, timeout_secs: int = 600, poll_interval: int = 15) -> None:
    """Block until the Custom Hostname's SSL certificate reaches 'active'.
    Must be called after DNS is configured so Cloudflare's HTTP validation
    request can reach our edge. Raises RuntimeError on timeout or terminal failure."""
    deadline = time.monotonic() + timeout_secs
    last_status = None
    while time.monotonic() < deadline:
        last_status = _cloudflare_ssl_status(hostname)
        app.logger.info(f"Cloudflare SSL status for {hostname}: {last_status}")
        if last_status == "active":
            return
        if last_status in ("expired", "deleted"):
            raise RuntimeError(f"Cloudflare SSL for {hostname} reached terminal state: {last_status}")
        time.sleep(poll_interval)
    raise RuntimeError(
        f"Cloudflare SSL for {hostname} did not become active within {timeout_secs // 60} minutes "
        f"(last status: {last_status})"
    )


DOMAIN_RENEWAL_GRACE_DAYS = 7  # days after a failed renewal invoice before we stop paying Porkbun to renew it


def _domain_reprice_if_due(dom, db) -> None:
    """
    Re-checks live Porkbun wholesale cost ~30 days before a domain
    subscription's renewal and updates the subscription's price if the
    guaranteed-margin formula (_sale_price_gbp — same one used at initial
    purchase) now works out differently, so a wholesale-cost increase over
    the year can never erode below the >=100% margin guarantee.

    Per instruction: reprices silently, no customer notice — the new price
    only takes effect at the NEXT invoice (proration_behavior="none"), never
    changes what they've already been charged.

    Guarded by last_repriced_period_end so this only fires once per renewal
    period, not once per day for the whole 30-day window.
    """
    if not dom.stripe_subscription_id:
        return  # grandfathered one-time-payment domain — not on a subscription at all

    try:
        sub = stripe.Subscription.retrieve(dom.stripe_subscription_id)
    except stripe.error.StripeError as exc:
        app.logger.error(f"_domain_reprice_if_due: could not retrieve subscription for {dom.domain}: {exc}")
        return

    if sub.status not in ("active", "trialing"):
        return  # already canceled/past_due/etc — not this job's concern

    period_end = datetime.utcfromtimestamp(sub.current_period_end)
    if dom.last_repriced_period_end == period_end:
        return  # already repriced for this exact renewal
    if (period_end - datetime.utcnow()) > timedelta(days=30):
        return  # not due yet

    tld = ".".join(dom.domain.split(".")[1:])
    fresh_pricing = _tld_price_gbp()
    wholesale_gbp = fresh_pricing.get(tld)
    if wholesale_gbp is None:
        app.logger.error(f"_domain_reprice_if_due: no live wholesale price for TLD of {dom.domain} — skipping, will retry next run")
        return
    new_price_gbp = _sale_price_gbp(wholesale_gbp)

    item = sub["items"]["data"][0]
    current_amount_gbp = item["price"]["unit_amount"] / 100.0
    if abs(new_price_gbp - current_amount_gbp) < 0.005:
        # No real change — still mark this period done so we don't recheck it daily.
        dom.last_repriced_period_end = period_end
        db.commit()
        return

    try:
        new_price = stripe.Price.create(
            currency="gbp",
            unit_amount=int(round(new_price_gbp * 100)),
            recurring={"interval": "year"},
            product=item["price"]["product"],
        )
        stripe.Subscription.modify(
            dom.stripe_subscription_id,
            items=[{"id": item["id"], "price": new_price.id}],
            proration_behavior="none",
        )
    except stripe.error.StripeError as exc:
        app.logger.error(f"_domain_reprice_if_due: failed to reprice {dom.domain}: {exc}")
        return

    dom.price_gbp = new_price_gbp
    dom.wholesale_gbp = wholesale_gbp
    dom.margin_gbp = round(new_price_gbp - wholesale_gbp, 2)
    dom.last_repriced_period_end = period_end
    db.commit()
    app.logger.info(
        f"_domain_reprice_if_due: {dom.domain} repriced £{current_amount_gbp:.2f} -> £{new_price_gbp:.2f} "
        f"for renewal on {period_end.date()}"
    )


def _domain_check_renewal_grace(dom, db) -> None:
    """
    If a domain subscription's renewal payment has been failing for more
    than DOMAIN_RENEWAL_GRACE_DAYS, proactively turn off Porkbun
    auto-renewal — Stripe's own dunning/retry schedule can span weeks, and
    we don't want to keep paying Porkbun to renew a domain during that
    whole window when the customer's card isn't going through. This runs
    ahead of (and independent from) customer.subscription.deleted, which
    only fires once Stripe finally gives up on the subscription entirely —
    by then we could have already paid for a renewal nobody's paying for.
    """
    if not dom.renewal_payment_failed_at or dom.stripe_subscription_id is None:
        return
    if datetime.utcnow() - dom.renewal_payment_failed_at < timedelta(days=DOMAIN_RENEWAL_GRACE_DAYS):
        return
    if dom.status == "renewal_lapsed":
        return  # already handled
    try:
        _porkbun_set_autorenew(dom.domain, on=False)
    except Exception as exc:
        app.logger.error(f"_domain_check_renewal_grace: Porkbun autorenew-off failed for {dom.domain}: {exc}")
        return
    dom.status = "renewal_lapsed"
    db.commit()
    app.logger.info(
        f"_domain_check_renewal_grace: {dom.domain} unpaid for {DOMAIN_RENEWAL_GRACE_DAYS}+ days — "
        f"Porkbun auto-renew disabled"
    )


def run_domain_billing_maintenance() -> None:
    """Daily entry point (see outreach/domain_billing.py) — reprices domains
    approaching renewal and disables auto-renew for domains stuck in
    payment failure past the grace period. Safe to run as often as daily;
    both checks are idempotent no-ops once handled for the current period."""
    db = SessionLocal()
    try:
        doms = db.query(Domain).filter(
            Domain.stripe_subscription_id.isnot(None),
            Domain.status.in_(["active", "pending", "renewal_lapsed"]),
        ).all()
        for dom in doms:
            _domain_reprice_if_due(dom, db)
            _domain_check_renewal_grace(dom, db)
    finally:
        db.close()


def _handle_domain_order_async(domain: str, site_id: str, customer_email: str,
                                price_gbp: float, business_name: str,
                                stripe_payment_id: str, wholesale_gbp: float = None,
                                stripe_subscription_id: str = None) -> None:
    """Orchestrate domain registration in a background thread.
    Steps: resolve customer/business info → send order-confirmed email →
    Porkbun register → Cloudflare custom hostnames → Porkbun DNS.
    On any failure: email admin, mark needs_manual_setup.

    Runs entirely off the webhook's request/response path — including the
    customer-facing "order confirmed" email — so a slow Resend/Porkbun/
    Cloudflare call here can never delay the webhook's 200 response and
    trigger a Stripe retry.

    business_name may be passed in already (e.g. from the manual-reprocess
    path) or resolved here from site_id if blank; customer_email is the raw
    value from the Stripe session and gets backfilled from the Generation's
    email if blank.

    wholesale_gbp and the resulting margin are stored on the Domain row at
    purchase time (rather than recomputed later from the current TLD price
    table) so historical margin stays accurate even if pricing logic or
    Porkbun's wholesale prices change afterwards."""
    if wholesale_gbp is None:
        tld = ".".join(domain.split(".")[1:])
        wholesale_gbp = _tld_price_gbp().get(tld, 0.0)
    margin_gbp = round(price_gbp - wholesale_gbp, 2)

    # Idempotency guard, checked again here (not just in the webhook) since
    # this function is also called directly from the manual-reprocess path
    # (bypassing the webhook's own check) — a Stripe retry landing between
    # that check and this one, or a second manual reprocess, must still be a
    # no-op rather than a duplicate email + a doomed duplicate-row insert.
    db = SessionLocal()
    try:
        already = db.query(Domain).filter(Domain.domain == domain).first()
    finally:
        db.close()
    if already:
        app.logger.info(
            f"_handle_domain_order_async: {domain} already has a Domain row "
            f"(id={already.id}, status={already.status}) — skipping duplicate run."
        )
        return

    # Resolve generation FK, and backfill customer_email/business_name from
    # it if not already known (the webhook no longer resolves these itself).
    gen_id = None
    if site_id:
        db = SessionLocal()
        try:
            gen = db.query(Generation).join(Lead).filter(Lead.public_id == site_id).first()
            if gen:
                gen_id = gen.id
                if not customer_email:
                    customer_email = gen.email
                if not business_name:
                    business_name = gen.business_name or ""
        finally:
            db.close()

    app.logger.info(f"Domain order paid: {domain} site={site_id} email={customer_email}")

    # Confirm to customer that order was received — backgrounded along with
    # everything else below (see docstring: this used to run synchronously
    # in the webhook handler, which was itself a latent cause of slow
    # responses triggering Stripe retries).
    if customer_email:
        try:
            send_domain_order_customer_email(customer_email, domain, business_name)
        except Exception as exc:
            app.logger.error(f"Failed to send domain customer email: {exc}")

    # Create Domain row
    dom_id = None
    db = SessionLocal()
    try:
        dom = Domain(
            generation_id=gen_id,
            domain=domain,
            status="pending",
            price_gbp=price_gbp,
            wholesale_gbp=wholesale_gbp,
            margin_gbp=margin_gbp,
            stripe_payment_id=stripe_payment_id,
            customer_email=customer_email,
            stripe_subscription_id=stripe_subscription_id,
        )
        db.add(dom)
        db.commit()
        dom_id = dom.id
    except Exception as exc:
        # Almost certainly the same unique-constraint race the idempotency
        # check above exists to prevent (two near-simultaneous deliveries
        # both passing that check before either commits) — genuinely rare,
        # but still surfaced to admin since it means this delivery's work
        # (Porkbun/Cloudflare/DNS) did NOT happen and needs a human to
        # confirm the other delivery's run actually completed.
        app.logger.error(f"Failed to create Domain record for {domain}: {exc}")
        try:
            send_domain_setup_failed_email(
                domain, site_id, customer_email, price_gbp, "db_create", str(exc))
        except Exception:
            pass
        return
    finally:
        db.close()

    def _update(status=None, error_step=None, error_msg=None, **kw):
        db2 = SessionLocal()
        try:
            rec = db2.query(Domain).filter(Domain.id == dom_id).first()
            if rec:
                if status:
                    rec.status = status
                if error_step:
                    rec.error_step = error_step
                if error_msg:
                    rec.error_message = error_msg
                for attr, val in kw.items():
                    setattr(rec, attr, val)
                db2.commit()
        except Exception as exc:
            app.logger.warning(f"Domain record update failed: {exc}")
        finally:
            db2.close()

    def _fail(step: str, error: str) -> None:
        _update(status="needs_manual_setup", error_step=step, error_msg=error)
        app.logger.error(f"Domain automation [{step}] failed for {domain}: {error}")
        try:
            send_domain_setup_failed_email(domain, site_id, customer_email, price_gbp, step, error)
        except Exception as exc2:
            app.logger.error(f"Failed to send domain failure email: {exc2}")

    # Step 1: Register domain with Porkbun
    try:
        _porkbun_register_domain(domain)
        _update(registered_at=datetime.utcnow())
        app.logger.info(f"Domain registered via Porkbun: {domain}")
    except Exception as exc:
        _fail("porkbun_register", str(exc))
        return

    # Step 2: Register domain + www as Custom Hostnames on our Cloudflare zone.
    # cloudflare_connected_at is NOT set here — SSL issuance is async and we
    # only mark it complete after polling confirms ssl.status == "active" (Step 4).
    cname_target = CLOUDFLARE_CNAME_TARGET
    try:
        _cloudflare_add_custom_hostname(domain)
        _cloudflare_add_custom_hostname("www." + domain)
        app.logger.info(f"Cloudflare custom hostnames added: {domain}, www.{domain} (cname: {cname_target})")
    except Exception as exc:
        _fail("cloudflare_connect", str(exc))
        return

    # Step 3: Configure DNS via Porkbun
    if not cname_target:
        _fail("dns_setup",
              "No CNAME target available. Set CLOUDFLARE_CNAME_TARGET env var "
              "(the Cloudflare for SaaS CNAME target, e.g. connect.groundworkbuild.com).")
        return

    try:
        _porkbun_create_dns(domain, "ALIAS", "", cname_target)   # apex/root
        _porkbun_create_dns(domain, "CNAME", "www", cname_target)
        _update(dns_configured_at=datetime.utcnow())
        app.logger.info(f"DNS configured for {domain} → {cname_target}")
    except Exception as exc:
        _fail("dns_setup", str(exc))
        return

    # Step 4: Wait for Cloudflare SSL to become active. HTTP validation requires
    # DNS to already be pointing at our Cloudflare zone (set in Step 3 above),
    # so this poll must come after DNS is configured — not immediately after
    # hostname creation. On timeout, fall back to needs_manual_setup so the admin
    # is notified; the domain/DNS are already live and SSL usually resolves on its
    # own, but the "active" status and live email are withheld until confirmed.
    try:
        _cloudflare_wait_for_ssl(domain, timeout_secs=600, poll_interval=15)
        _update(cloudflare_connected_at=datetime.utcnow(), status="active")
        app.logger.info(f"Cloudflare SSL active for {domain}")
    except Exception as exc:
        _fail("ssl_activation", str(exc))
        return

    # All steps succeeded
    app.logger.info(f"Domain automation complete: {domain}")

    # Belt-and-suspenders guard on top of the idempotency check earlier in
    # this function: even if some future code path re-enters this tail end
    # for a Domain row that already exists (e.g. a retry-from-last-failed-
    # step admin action), the "your domain is live" email must never go out
    # twice for the same domain. Check-and-set against the row itself, not
    # a local variable, since a local flag wouldn't survive across separate
    # invocations/threads.
    db3 = SessionLocal()
    try:
        rec = db3.query(Domain).filter(Domain.id == dom_id).first()
        already_sent = bool(rec and rec.live_email_sent_at)
    finally:
        db3.close()

    if already_sent:
        app.logger.info(f"Domain live email already sent for {domain} (dom_id={dom_id}) — skipping resend.")
    else:
        try:
            send_domain_live_email(customer_email, domain, business_name)
            _update(live_email_sent_at=datetime.utcnow())
        except Exception as exc:
            app.logger.error(f"Failed to send domain live email to {customer_email}: {exc}")

    try:
        send_domain_order_admin_email(domain, price_gbp, customer_email, site_id, automated=True)
    except Exception as exc:
        app.logger.error(f"Failed to send domain admin audit email: {exc}")


@app.route("/api/contact", methods=["POST"])
def contact_form():
    """Receive a contact form submission from a generated site and forward it
    to the business owner's email via Resend. Expects multipart/form-data with
    fields: site_id, name, email, message, phone (optional), website (honeypot)."""
    ip = _client_ip()

    # Honeypot — bots fill this in, real visitors never see it (hidden off-screen).
    if request.form.get("website", "").strip():
        return jsonify({"ok": True})  # silent accept to avoid teaching bots

    if _contact_rate_limited(ip):
        return jsonify({"ok": False, "error": "Too many submissions. Please try again later."}), 429

    site_id = request.form.get("site_id", "").strip()
    name = request.form.get("name", "").strip()
    email_addr = request.form.get("email", "").strip()
    phone = request.form.get("phone", "").strip()
    message = request.form.get("message", "").strip()

    if not site_id or not name or not email_addr or not message:
        return jsonify({"ok": False, "error": "Please fill in all required fields."}), 400

    if not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', email_addr):
        return jsonify({"ok": False, "error": "Please enter a valid email address."}), 400

    db = SessionLocal()
    try:
        gen = db.query(Generation).join(Lead).filter(Lead.public_id == site_id).first()
        if not gen:
            return jsonify({"ok": False, "error": "Site not found."}), 404
        business_name = gen.business_name or (gen.lead.form_data or {}).get("business_name", "")
        to_email = gen.email
    finally:
        db.close()

    app.logger.info(f"Contact form: site={site_id} to={to_email} from_visitor={email_addr}")
    try:
        send_enquiry_email(to_email, business_name, name, email_addr, phone, message)
        app.logger.info(f"Contact form: sent successfully to {to_email}")
        return jsonify({"ok": True})
    except Exception as exc:
        app.logger.error(f"Contact form send failed for site {site_id} to {to_email}: {exc}")
        return jsonify({"ok": False, "error": "Sorry, we couldn't send your message. Please call or email us directly."}), 500


@app.route("/api/checkout/session", methods=["POST"])
def create_checkout_session():
    if not STRIPE_SECRET_KEY:
        return jsonify({"error": "Stripe is not configured"}), 503
    data = request.get_json(silent=True) or {}
    job_id = str(data.get("job_id", "")).strip()
    if not job_id:
        return jsonify({"error": "missing job_id"}), 400

    plan = str(data.get("plan", "monthly")).strip().lower()
    if plan not in ("monthly", "annual"):
        return jsonify({"error": "invalid plan"}), 400
    if plan == "annual" and not STRIPE_ANNUAL_PRICE_ID:
        return jsonify({"error": "Annual billing isn't available yet — please choose monthly."}), 503
    recurring_price_id = STRIPE_ANNUAL_PRICE_ID if plan == "annual" else STRIPE_MONTHLY_PRICE_ID

    db = SessionLocal()
    try:
        gen = db.query(Generation).join(Lead).filter(Lead.public_id == job_id).first()
        if not gen:
            return jsonify({"error": "not found"}), 404
        # If the requester is signed into an account, that account must own
        # this job_id — stops a logged-in user from paying to activate a
        # different account's generation just by knowing/guessing its
        # job_id. No session at all (the normal guest-checkout path, before
        # anyone's ever set a password) is left unchanged: job_id is the
        # existing capability token for the whole pre-account funnel
        # (preview/editor links work the same way — see CLAUDE.md).
        session_email = session.get("account_email")
        if session_email and gen.email and session_email != gen.email:
            return jsonify({"error": "forbidden"}), 403
        if gen.status == "live":
            return jsonify({"error": "already live"}), 409

        # Reactivating a previously-live, later-cancelled site is NOT the
        # same purchase as going live for the first time — added
        # 2026-07-19 after this endpoint charged a fresh £99 setup fee +
        # 30-day trial on every reactivation, identical to a new signup,
        # despite job_html_preserved's own banner already telling the
        # customer "reinstating just resumes the same subscription." No
        # setup fee, no trial — see subscription_data/line_items below.
        is_reactivation = gen.status == "canceled"

        # No blocking here any more (removed 2026-07-23) — _resolve_subdomain
        # always produces a usable, available slug (stripping bad characters,
        # suffixing on a name collision) rather than making the customer
        # email support before they're allowed to pay. The actual subdomain
        # is assigned at webhook time (checkout.session.completed below),
        # same as before; this call just needs to not error.
        business_name = (gen.lead.form_data or {}).get("business_name", "")

        # Trial length is now real, per-prospect data (added 2026-07-26),
        # not a flat 30-days-for-everyone default — see
        # Prospect.trial_days_earned's docstring. An outreach-sourced
        # prospect (one with a Prospect row behind this lead) earns free
        # trial days only from a real answer (the quick "why not" survey's
        # "too expensive" -> 30, or reaching the hail-mary stage -> 90);
        # otherwise 0, full price from day one. A direct/organic self-signup
        # (no Prospect at all — the only channel with a real paying
        # customer so far) keeps the original 30-day default unchanged;
        # this restructure is specifically about outreach pricing, not
        # organic signups that were never part of this conversation.
        outreach_prospect = db.query(Prospect).filter(Prospect.lead_id == gen.lead_id).first()
        trial_days = outreach_prospect.trial_days_earned or 0 if outreach_prospect else 30
    finally:
        db.close()

    # No setup fee (removed 2026-07-23, until break-even). Reactivation and
    # first-time purchase are both just the recurring price now; the only
    # remaining difference is the trial, which only makes sense on a
    # genuine first-time signup.
    line_items = [{"price": recurring_price_id, "quantity": 1}]
    if is_reactivation:
        subscription_data = {}
    elif plan == "monthly" and trial_days > 0:
        # Trial is a monthly-only promo — annual customers already get the
        # discounted rate, so trial_period_days isn't stacked on top of
        # that (see design_handoff_marketing_consistency notes).
        subscription_data = {"trial_period_days": trial_days}
    else:
        subscription_data = {}

    cs = stripe.checkout.Session.create(
        mode="subscription",
        line_items=line_items,
        subscription_data=subscription_data,
        allow_promotion_codes=True,
        client_reference_id=job_id,
        success_url=f"{SITE_URL}/live.html?id={job_id}",
        cancel_url=(
            f"{SITE_URL}/api/generate/{job_id}/preserved" if is_reactivation
            else f"{SITE_URL}/api/generate/{job_id}/html"
        ),
    )

    # First-checkout-attempt stamp for the Funnel page (see
    # Generation.checkout_started_at's docstring) — set once, never
    # overwritten, so retrying checkout after a first abandoned attempt
    # doesn't erase "when they first got this far." A short separate
    # session, opened only now that the (potentially slow) Stripe call has
    # already succeeded — no reason to hold a DB connection open across it.
    db2 = SessionLocal()
    try:
        gen2 = db2.query(Generation).join(Lead).filter(Lead.public_id == job_id).first()
        if gen2 and gen2.checkout_started_at is None:
            gen2.checkout_started_at = datetime.utcnow()
            db2.commit()
    finally:
        db2.close()

    return jsonify({"url": cs.url})


@app.route("/webhook/stripe", methods=["POST"])
def stripe_webhook():
    payload = request.get_data()
    sig = request.headers.get("Stripe-Signature", "")
    try:
        event = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
    except (ValueError, stripe.error.SignatureVerificationError):
        return "", 400

    if event.type == "checkout.session.completed":
        cs = event.data.object
        # cs itself uses attribute access (StripeObject), but metadata is a
        # nested StripeObject too — and as of stripe-python v7+, StripeObject
        # no longer subclasses dict, so it has no .get(). Explicitly convert
        # to a plain dict before doing dict-style lookups on it.
        metadata = cs.metadata.to_dict() if cs.metadata else {}

        if metadata.get("type") == "domain":
            domain    = metadata.get("domain", "")
            site_id   = metadata.get("site_id", "")
            price_gbp = float(metadata.get("price_gbp") or 0)
            wholesale_gbp = metadata.get("wholesale_gbp")
            if wholesale_gbp is not None:
                wholesale_gbp = float(wholesale_gbp)
            else:
                # Fallback for older sessions created before wholesale_gbp was
                # added to metadata — recompute from live Porkbun pricing.
                _tld = ".".join(domain.split(".")[1:])
                wholesale_gbp = _tld_price_gbp().get(_tld, 0.0)
            stripe_payment_id = cs.payment_intent or cs.id or ""
            domain_subscription_id = cs.subscription
            raw_customer_email = cs.customer_details.email if cs.customer_details else ""

            # Idempotency guard — Stripe retries webhook deliveries that don't
            # get a fast 2xx (backoff spans hours; a retry can land long
            # after the original attempt, e.g. if this endpoint was crashing
            # at the time and got fixed afterwards). A Domain row already
            # existing for this domain means an earlier delivery already
            # claimed it, so this is a duplicate delivery of the same event
            # — do nothing (no emails, no Porkbun/Cloudflare/DB work) and ack
            # Stripe so it stops retrying. This one lookup is the only thing
            # that runs synchronously in the request path.
            db = SessionLocal()
            try:
                already = db.query(Domain).filter(Domain.domain == domain).first()
            finally:
                db.close()
            if already:
                app.logger.info(
                    f"Domain order webhook: {domain} already has a Domain row "
                    f"(id={already.id}, status={already.status}) — skipping duplicate delivery."
                )
                return "", 200

            # Everything else — customer/business lookup, the customer-facing
            # "order confirmed" email, and the Porkbun/Cloudflare/DNS pipeline
            # — runs in the background thread, not here. This function used
            # to send that customer email synchronously right here; if
            # Resend was ever slow, that alone could push us past Stripe's
            # response timeout and cause the exact kind of retry this guard
            # is protecting against, so it's backgrounded too now, along
            # with everything else in _handle_domain_order_async.
            t = threading.Thread(
                target=_handle_domain_order_async,
                args=(domain, site_id, raw_customer_email, price_gbp, "", stripe_payment_id, wholesale_gbp, domain_subscription_id),
                daemon=True,
            )
            t.start()

        else:
            # Standard site subscription checkout
            job_id = cs.client_reference_id
            customer_id = cs.customer
            invoice_id = cs.invoice
            subscription_id = cs.subscription
            if job_id:
                db = SessionLocal()
                try:
                    gen = db.query(Generation).join(Lead).filter(Lead.public_id == job_id).first()
                    if gen and gen.status != "live":
                        was_canceled = gen.status == "canceled"
                        gen.status = "live"
                        gen.canceled_at = None
                        if customer_id:
                            gen.stripe_customer_id = customer_id
                        if invoice_id:
                            gen.stripe_setup_invoice_id = invoice_id
                        if subscription_id:
                            gen.stripe_subscription_id = subscription_id

                        # Consume the survey-issued discount code, if this
                        # checkout used one (create_checkout_session stamped
                        # it into metadata) — cleared only on confirmed
                        # payment, not at session-creation time, so an
                        # abandoned checkout doesn't burn the one-time code.
                        redeemed_code = metadata.get("discount_code_redeemed")
                        if redeemed_code:
                            prospect = db.query(Prospect).filter(Prospect.lead_id == gen.lead_id).first()
                            if prospect and prospect.discount_code and prospect.discount_code.upper() == redeemed_code:
                                prospect.discount_code = None
                                prospect.discount_expiry = None

                        # Assign subdomain — _resolve_subdomain always returns
                        # a usable, available slug (see its docstring), so
                        # there's no invalid-chars/taken branch to handle
                        # here any more; collisions (including the rare
                        # simultaneous-payment race) are resolved with a
                        # numeric suffix, never left unassigned.
                        if not gen.subdomain:
                            business_name = (gen.lead.form_data or {}).get("business_name", "")
                            gen.subdomain = _resolve_subdomain(business_name, db, exclude_gen_id=gen.id)

                        # Reinstate: this is a resubscribe (was_canceled), not a
                        # first-time purchase — reconnect any domain(s) that were
                        # disconnected on cancellation, to the exact same
                        # preserved Generation row (no regeneration — html_content
                        # was never touched by cancellation). Re-registering the
                        # Custom Hostname re-validates SSL the same way it did
                        # originally (DV over HTTP); DNS was never disturbed, so
                        # this is normally fast. Auto-renewal is turned back on
                        # too, since the domain is in active use again.
                        if was_canceled:
                            disconnected_doms = db.query(Domain).filter(
                                Domain.generation_id == gen.id, Domain.status == "disconnected"
                            ).all()
                            for d in disconnected_doms:
                                try:
                                    _cloudflare_add_custom_hostname(d.domain)
                                    _cloudflare_add_custom_hostname("www." + d.domain)
                                    d.status = "pending"
                                except Exception as exc:
                                    app.logger.error(f"Reinstate: Cloudflare reconnect failed for {d.domain}: {exc}")
                                try:
                                    _porkbun_set_autorenew(d.domain, on=True)
                                except Exception as exc:
                                    app.logger.error(f"Reinstate: Porkbun autorenew-on failed for {d.domain}: {exc}")
                            if disconnected_doms:
                                app.logger.info(
                                    f"Stripe webhook: gen {gen.id} reinstated — "
                                    f"{len(disconnected_doms)} domain(s) reconnected"
                                )

                        # Real (not proxied) outreach "paid" signal — the checkout
                        # session's client_reference_id (job_id) is the Lead's
                        # public_id, which is exactly how the Generation above was
                        # just looked up, so the same chain (job_id -> Lead ->
                        # Prospect.lead_id) traces a payment straight back to the
                        # originating outreach prospect, if there is one. Direct
                        # (non-outreach) signups have no Prospect row for this
                        # lead_id, so this is a no-op for them.
                        prospect = db.query(Prospect).filter(Prospect.lead_id == gen.lead_id).first()
                        if prospect and prospect.paid_at is None:
                            prospect.paid_at = datetime.utcnow()
                            _stamp_latest_touch_outcome(db, prospect.id, "paid_at")
                            app.logger.info(
                                f"Stripe webhook: prospect {prospect.id} marked paid_at (job_id={job_id})"
                            )

                        # Admin notification (2026-07-19) — backgrounded, same
                        # reasoning as the domain-order email above: this
                        # request path has to stay fast enough for Stripe's
                        # own timeout, so a slow Resend call can't block it.
                        threading.Thread(
                            target=send_admin_payment_received_email,
                            args=(gen.business_name, gen.email, job_id, was_canceled),
                            daemon=True,
                        ).start()

                        db.commit()
                finally:
                    db.close()

    elif event.type == "customer.subscription.updated":
        # Catches cancel_at_period_end flipping true/false, and keeps
        # current_period_end in sync so the account page can show "Live —
        # ending <date>" between the customer clicking cancel and the
        # subscription actually being deleted. NOT a churn event by itself —
        # canceled_at is only ever written by customer.subscription.deleted
        # below, once the subscription is genuinely gone.
        sub = event.data.object
        db = SessionLocal()
        try:
            gen = db.query(Generation).filter(Generation.stripe_subscription_id == sub.id).first()
            if not gen and sub.customer:
                # Legacy generations predating stripe_subscription_id being
                # captured — fall back to customer id and backfill it.
                gen = db.query(Generation).filter(Generation.stripe_customer_id == sub.customer).first()
                if gen:
                    gen.stripe_subscription_id = sub.id
            if gen:
                gen.cancel_at_period_end = bool(sub.cancel_at_period_end)
                if sub.current_period_end:
                    gen.subscription_period_end = datetime.utcfromtimestamp(sub.current_period_end)
                db.commit()
                app.logger.info(
                    f"Stripe webhook: subscription.updated for gen {gen.id} — "
                    f"cancel_at_period_end={gen.cancel_at_period_end}"
                )
        finally:
            db.close()

    elif event.type == "customer.subscription.deleted":
        # The real churn event — the subscription is actually gone (either
        # cancel_at_period_end ran its course, or an immediate cancellation
        # happened via the Stripe dashboard). Writes Generation.canceled_at,
        # the field the monthly-churn calculation reads — and, new in this
        # change, actually winds the site down: stops it serving on any
        # domain (subdomain + custom), disconnects the Cloudflare Custom
        # Hostname for any purchased domain, and pauses Porkbun auto-renewal
        # on those domains so Groundwork stops paying to renew a domain
        # nobody's using. The Generation row itself, its html_content, and
        # any pending edits are never touched — this is a status change and
        # a routing/DNS-registration change, not a delete.
        sub = event.data.object
        db = SessionLocal()
        try:
            gen = db.query(Generation).filter(Generation.stripe_subscription_id == sub.id).first()
            if not gen and sub.customer:
                gen = db.query(Generation).filter(Generation.stripe_customer_id == sub.customer).first()
            if gen and gen.canceled_at is None:
                gen.canceled_at = datetime.utcnow()
                gen.status = "canceled"

                doms = db.query(Domain).filter(
                    Domain.generation_id == gen.id, Domain.status == "active"
                ).all()
                for d in doms:
                    try:
                        _cloudflare_delete_custom_hostname(d.domain)
                        _cloudflare_delete_custom_hostname("www." + d.domain)
                    except Exception as exc:
                        app.logger.error(f"subscription.deleted: Cloudflare disconnect failed for {d.domain}: {exc}")
                    try:
                        _porkbun_set_autorenew(d.domain, on=False)
                    except Exception as exc:
                        app.logger.error(f"subscription.deleted: Porkbun autorenew-off failed for {d.domain}: {exc}")
                    d.status = "disconnected"

                app.logger.info(
                    f"Stripe webhook: subscription.deleted — gen {gen.id} churned "
                    f"(canceled_at set, status=canceled, {len(doms)} domain(s) disconnected)"
                )
                db.commit()
            else:
                # Not a site-hosting subscription — check whether it's a
                # domain-renewal subscription instead (the two share this
                # same event type, so both have to be checked here).
                dom_deleted = db.query(Domain).filter(Domain.stripe_subscription_id == sub.id).first()
                if dom_deleted and dom_deleted.status != "renewal_lapsed":
                    try:
                        _porkbun_set_autorenew(dom_deleted.domain, on=False)
                    except Exception as exc:
                        app.logger.error(f"subscription.deleted: Porkbun autorenew-off failed for {dom_deleted.domain}: {exc}")
                    dom_deleted.status = "renewal_lapsed"
                    db.commit()
                    app.logger.info(
                        f"Stripe webhook: domain renewal subscription deleted for {dom_deleted.domain} — "
                        f"Porkbun auto-renew disabled"
                    )
        finally:
            db.close()

    elif event.type == "invoice.payment_failed":
        # Early warning for a domain renewal — Stripe's dunning/retry
        # schedule can span weeks before customer.subscription.deleted ever
        # fires, so this starts the grace-period clock
        # (_domain_check_renewal_grace) rather than waiting for the
        # subscription to be fully given up on.
        inv = event.data.object
        sub_id = inv.get("subscription") if isinstance(inv, dict) else getattr(inv, "subscription", None)
        if sub_id:
            db = SessionLocal()
            try:
                dom = db.query(Domain).filter(Domain.stripe_subscription_id == sub_id).first()
                if dom and dom.renewal_payment_failed_at is None:
                    dom.renewal_payment_failed_at = datetime.utcnow()
                    db.commit()
                    app.logger.info(f"Stripe webhook: renewal payment failed for {dom.domain} — grace period started")
            finally:
                db.close()

    elif event.type == "charge.refunded":
        # A refund doesn't cancel the subscription or take a site down by
        # itself (that's a separate action, either via customer.subscription.
        # deleted or a manual admin call) — this just records that money
        # went back out, so the database doesn't silently disagree with
        # Stripe about billing state. Matches against both possible charges:
        # a site subscription's setup-fee invoice (Generation) and a
        # one-time domain purchase (Domain).
        charge = event.data.object
        customer_id = charge.customer
        payment_intent_id = charge.payment_intent
        db = SessionLocal()
        try:
            matched = False
            if customer_id:
                gen = db.query(Generation).filter(Generation.stripe_customer_id == customer_id).first()
                if gen and gen.refunded_at is None:
                    gen.refunded_at = datetime.utcnow()
                    matched = True
                    app.logger.warning(
                        f"Stripe webhook: charge.refunded for gen {gen.id} ({gen.business_name!r}) — "
                        f"refunded_at set, status/site left untouched, needs admin review."
                    )
            if payment_intent_id:
                dom = db.query(Domain).filter(Domain.stripe_payment_id == payment_intent_id).first()
                if dom and dom.refunded_at is None:
                    dom.refunded_at = datetime.utcnow()
                    matched = True
                    app.logger.warning(
                        f"Stripe webhook: charge.refunded for domain {dom.domain} — "
                        f"refunded_at set, needs admin review."
                    )
            if matched:
                db.commit()
            else:
                app.logger.info(f"Stripe webhook: charge.refunded — no matching Generation/Domain for customer={customer_id}, payment_intent={payment_intent_id}")
        finally:
            db.close()

    elif event.type == "invoice.paid":
        # Clears the grace-period flag if a previously-failed renewal
        # invoice (or its retry) goes through — e.g. the customer updated
        # their card. Runs on every paid invoice, not just recoveries, but
        # is a no-op unless renewal_payment_failed_at was actually set.
        inv = event.data.object
        sub_id = inv.get("subscription") if isinstance(inv, dict) else getattr(inv, "subscription", None)
        if sub_id:
            db = SessionLocal()
            try:
                dom = db.query(Domain).filter(Domain.stripe_subscription_id == sub_id).first()
                if dom and dom.renewal_payment_failed_at is not None:
                    dom.renewal_payment_failed_at = None
                    db.commit()
                    app.logger.info(f"Stripe webhook: renewal payment recovered for {dom.domain}")
            finally:
                db.close()

    return "", 200


@app.route("/api/webhooks/sms-inbound", methods=["POST"])
def sms_inbound_webhook():
    """
    Esendex webhook receiver (docs/outreach-pipeline-spec.md Section 11a) —
    replaces the earlier Twilio-based implementation. No Twilio account
    exists; Esendex is the primary SMS provider.

    Esendex's webhook payload is a JSON array of events, e.g.:
      [{"productId": "account", "eventId": "inbound"|"stop",
        "eventVersion": "1", "eventTime": "...", "data": {...}}, ...]
    — confirmed from Esendex's public docs. The event ENVELOPE shape above
    is confirmed; the exact field names inside `data` for "inbound"/"stop"
    specifically were NOT confirmable from available documentation (Esendex
    doesn't publish a field-level schema for these). Parsing below tries
    several plausible key names and logs the full raw payload on every
    request — check that log after the first real inbound reply/stop event
    reaches this endpoint and tighten the key list to match reality; this
    is flagged, not silently guessed.

    Auth: no documented HMAC/signature scheme for Esendex webhooks (unlike
    Twilio's X-Twilio-Signature) — secured instead by a shared-secret query
    parameter embedded in the callback URL registered with Esendex
    (?secret=..., checked against ESENDEX_WEBHOOK_SECRET). Fails closed if
    unset.
    """
    shared_secret = os.environ.get("ESENDEX_WEBHOOK_SECRET")
    if not shared_secret:
        app.logger.error("sms_inbound_webhook: ESENDEX_WEBHOOK_SECRET not set — rejecting request")
        return "", 503

    if not hmac.compare_digest(request.args.get("secret", ""), shared_secret):
        app.logger.warning("sms_inbound_webhook: invalid or missing secret query param")
        return "", 403

    events = request.get_json(silent=True) or []
    app.logger.info(f"sms_inbound_webhook: raw payload: {events}")

    db = SessionLocal()
    try:
        for event in events if isinstance(events, list) else [events]:
            if not isinstance(event, dict) or event.get("productId") != "account":
                continue

            event_id = event.get("eventId")
            data = event.get("data") or {}
            from_phone = (
                data.get("originator") or data.get("from") or data.get("sender")
                or data.get("phoneNumber") or data.get("mobileNumber") or ""
            )
            body = data.get("body") or data.get("messageText") or data.get("message") or data.get("text") or ""

            if not from_phone:
                app.logger.warning(f"sms_inbound_webhook: could not extract a phone number from event: {event}")
                continue

            if event_id == "stop":
                prospect = handle_forced_sms_stop(db, from_phone, body)
            elif event_id == "inbound":
                prospect = handle_inbound_sms(db, from_phone, body)
            else:
                continue

            if not prospect:
                app.logger.warning(f"sms_inbound_webhook: no prospect matched for {from_phone}")
    finally:
        db.close()

    return jsonify({"status": "ok"}), 200


@app.route("/api/webhooks/email-inbound", methods=["POST"])
def email_inbound_webhook():
    """
    Receiver for the Cloudflare Email Worker (frontend/_worker.js:email()) —
    closes Section 11a's reply-capture gap. Cloudflare Email Routing (once a
    routing rule on the outreach sending domain points at that Worker, a
    dashboard step outside this codebase) invokes the Worker's email()
    handler for every inbound message; it POSTs the parsed {from, text}
    here as JSON.

    Authenticated with a shared secret (EMAIL_INBOUND_SHARED_SECRET, set as
    a Cloudflare Worker secret AND a Railway env var — both sides need the
    same value) rather than a signature scheme, since Cloudflare Email
    Workers have no built-in request-signing equivalent to Twilio's
    X-Twilio-Signature. Fails closed if unset, same as the SMS webhook.
    """
    shared_secret = os.environ.get("EMAIL_INBOUND_SHARED_SECRET")
    if not shared_secret:
        app.logger.error("email_inbound_webhook: EMAIL_INBOUND_SHARED_SECRET not set — rejecting request")
        return "", 503

    provided = request.headers.get("X-Groundwork-Email-Secret", "")
    if not provided or not hmac.compare_digest(provided, shared_secret):
        app.logger.warning("email_inbound_webhook: invalid or missing shared secret")
        return "", 403

    data = request.get_json(silent=True) or {}
    from_email = (data.get("from") or "").strip().lower()
    body = data.get("text") or ""

    db = SessionLocal()
    try:
        prospect = handle_inbound_email(db, from_email, body)
        if not prospect:
            app.logger.warning(f"email_inbound_webhook: no prospect matched for {from_email}")
    finally:
        db.close()

    return jsonify({"status": "ok"}), 200


@app.route("/api/webhooks/email-forward-log", methods=["POST"])
def email_forward_log():
    """
    Self-logging endpoint for frontend/_worker.js:email()'s message.forward()
    call to groundwork-build@outlook.com — that forward is best-effort
    (wrapped in its own try/catch so a failure there never blocks/rejects the
    message), which means a silent failure would otherwise be invisible:
    Cloudflare Worker console output is real-time-only (no historical
    retention here), so a failed forward from the past is unrecoverable once
    the tail session that would've shown it is gone. Logging the outcome
    here instead lands it in Railway's logs, which we can always query after
    the fact.

    Same shared-secret auth as /api/webhooks/email-inbound (reuses
    EMAIL_INBOUND_SHARED_SECRET — one Worker, one secret, not a second one to
    provision/rotate for what's really the same trust boundary).

    Body: {"from": <str>, "success": <bool>, "error": <str|null>}
    """
    shared_secret = os.environ.get("EMAIL_INBOUND_SHARED_SECRET")
    if not shared_secret:
        app.logger.error("email_forward_log: EMAIL_INBOUND_SHARED_SECRET not set — rejecting request")
        return "", 503

    provided = request.headers.get("X-Groundwork-Email-Secret", "")
    if not provided or not hmac.compare_digest(provided, shared_secret):
        app.logger.warning("email_forward_log: invalid or missing shared secret")
        return "", 403

    data = request.get_json(silent=True) or {}
    from_email = (data.get("from") or "").strip()
    success = bool(data.get("success"))
    error = data.get("error")

    if success:
        app.logger.info(f"email_forward_log: forward to groundwork-build@outlook.com succeeded for reply from {from_email}")
    else:
        app.logger.error(f"email_forward_log: forward to groundwork-build@outlook.com FAILED for reply from {from_email}: {error}")

    return jsonify({"status": "ok"}), 200


@app.route("/api/webhooks/resend-events", methods=["POST"])
def resend_events_webhook():
    """
    Resend webhook (Section 15's email health signal — the interim proxy
    for Postmaster Tools, PLUS real per-prospect open tracking added
    2026-07-14). Resend signs webhook payloads via Svix; verified with the
    `svix` package rather than hand-rolling HMAC, to avoid getting a
    security-relevant detail subtly wrong.

    Every event is still logged to EmailEventLog as before (outreach/ramp.py's
    get_health_signal("email") aggregates these into a rolling complaint
    rate) — that part is unchanged.

    NEW: on an "email.opened" event, look up the matching Prospect by email
    and, if found and still at the "sent" substage (never overwrite a more
    advanced state like clicked_generated/account_created/replied/cold —
    this is a one-way progression), write opened_at and advance
    funnel_substage to "opened". This is what makes Stage B of the
    follow-up sequence reachable at all (outreach/followup.py's
    STAGE_BY_SUBSTAGE maps "opened" -> "B") — before this, nothing ever
    wrote a prospect into the "opened" substage, so Stage B was dead code.

    NEW (2026-07-14): on an "email.bounced" event, pause that prospect's
    follow-up sequence the same way a real reply does — sets
    funnel_substage="bounced", which (like "replied"/"cold") isn't a key in
    STAGE_BY_SUBSTAGE, so outreach/followup.py's run_followups() query
    excludes it from consideration on every future run, on both channels.
    Also sets email_unsubscribed=True specifically, since a bounced address
    is dead — continuing to send there is pure waste and actively worsens
    the bounce rate that just fed this exact event. Unlike the "opened"
    handler, this always overrides funnel_substage regardless of the
    prospect's current state — there's no state a dead email address should
    still be emailed from. get_health_signal("email") in outreach/ramp.py
    also now folds bounces into the same spam-rate/circuit-breaker
    calculation as complaints (see that module's docstring).

    Requires two things outside this codebase to actually receive events:
    (1) open tracking enabled on the sending domain in the Resend dashboard
    (it's off by default — a tracking pixel has to be enabled per domain),
    and (2) a webhook subscription actually configured in Resend pointed at
    this endpoint with this same secret. Fails closed (503) if
    RESEND_WEBHOOK_SECRET isn't set, same as the other webhooks.
    """
    secret = os.environ.get("RESEND_WEBHOOK_SECRET")
    if not secret:
        app.logger.error("resend_events_webhook: RESEND_WEBHOOK_SECRET not set — rejecting request")
        return "", 503

    try:
        from svix.webhooks import Webhook, WebhookVerificationError
    except ImportError:
        app.logger.error("resend_events_webhook: svix package not installed — rejecting request")
        return "", 503

    try:
        payload = Webhook(secret).verify(request.get_data(), dict(request.headers))
    except WebhookVerificationError:
        app.logger.warning("resend_events_webhook: signature verification failed")
        return "", 403

    event_type = payload.get("type", "")
    data = payload.get("data", {}) or {}
    to_list = data.get("to") or []
    to_email_raw = (to_list[0] if to_list else data.get("email", "")) or ""
    # Resend doesn't always send a bare address here — confirmed 2026-07-16:
    # a bounce event for a prospect we sent to as a plain "user@domain"
    # string (see send_outreach_email — no display name is ever set on our
    # side) came back as '"user@domain" <user@domain>', apparently echoing
    # the receiving mail server's own bounce-report formatting. A raw
    # .strip().lower() match against Prospect.email (a bare-address column)
    # silently failed to find the prospect, so the bounce was logged to
    # EmailEventLog but never applied — email_unsubscribed stayed False and
    # a dead address kept looking sendable. email.utils.parseaddr handles
    # both the bare and quoted/display-name forms correctly.
    to_email = email_utils.parseaddr(to_email_raw)[1] or to_email_raw

    db = SessionLocal()
    try:
        db.add(EmailEventLog(
            resend_email_id=data.get("email_id"),
            to_email=to_email,
            event_type=event_type,
            # Raw payload, added 2026-07-21 so a bounce/complaint row
            # carries WHY, not just THAT — see EmailEventLog.detail's
            # comment in models.py. Cheap to store unconditionally rather
            # than branching on event_type.
            detail=data,
        ))

        if event_type == "email.opened" and to_email:
            # Case-insensitive on purpose (2026-07-19) — Prospect.email is
            # stored as-scraped by several write paths (own-site mailto/text
            # scrape, WebSearch discovery, manual apply), none of which
            # lowercase it, so a plain == match here silently misses any
            # bounce/open event for a prospect whose stored email has any
            # uppercase character. Same failure shape as the display-name
            # quoting bug fixed above (parseaddr) — matching against
            # whatever Resend happens to echo back, not what we stored.
            prospect = db.query(Prospect).filter(
                func.lower(Prospect.email) == to_email.strip().lower()
            ).first()
            if prospect:
                # opened_at is a factual timestamp ("this person opened the
                # email at least once") and is independent of funnel_substage
                # advancement — they're not the same operation. The original
                # code gated BOTH behind funnel_substage == "sent", so anyone
                # who clicked the magic link before the open webhook was
                # delivered (funnel_substage already "clicked_generated" by
                # then — webhook delivery isn't instant, and a real prospect
                # can click within seconds of opening) never got opened_at
                # written at all, despite genuinely having opened the email
                # — you can't click a link inside an email without opening
                # it first. Confirmed 2026-07-20: all 3 real clicked
                # prospects in production had opened_at still NULL (one
                # clicked 27 seconds after send). Substage still only
                # advances one-way from "sent", same as before — that part
                # wasn't wrong, only tying the timestamp write to it was.
                if prospect.opened_at is None:
                    prospect.opened_at = datetime.utcnow()
                    _stamp_latest_touch_outcome(db, prospect.id, "opened_at", channel="email")
                if prospect.funnel_substage == "sent":
                    prospect.funnel_substage = "opened"
                    app.logger.info(
                        f"resend_events_webhook: prospect {prospect.id} ({to_email}) "
                        f"opened — substage sent -> opened"
                    )
                else:
                    app.logger.info(
                        f"resend_events_webhook: prospect {prospect.id} ({to_email}) opened_at recorded, "
                        f"substage already {prospect.funnel_substage!r} — not regressing substage"
                    )
            else:
                app.logger.info(f"resend_events_webhook: open event for {to_email} — no matching prospect")

        elif event_type == "email.bounced" and to_email:
            # Case-insensitive on purpose (2026-07-19) — Prospect.email is
            # stored as-scraped by several write paths (own-site mailto/text
            # scrape, WebSearch discovery, manual apply), none of which
            # lowercase it, so a plain == match here silently misses any
            # bounce/open event for a prospect whose stored email has any
            # uppercase character. Same failure shape as the display-name
            # quoting bug fixed above (parseaddr) — matching against
            # whatever Resend happens to echo back, not what we stored.
            prospect = db.query(Prospect).filter(
                func.lower(Prospect.email) == to_email.strip().lower()
            ).first()
            if prospect:
                prospect.email_unsubscribed = True
                prospect.email_unsubscribed_at = datetime.utcnow()
                prospect.funnel_substage = "bounced"
                app.logger.info(
                    f"resend_events_webhook: prospect {prospect.id} ({to_email}) bounced — "
                    f"email_unsubscribed=True, funnel_substage=bounced, follow-ups paused"
                )
            else:
                app.logger.info(f"resend_events_webhook: bounce event for {to_email} — no matching prospect")

        db.commit()
    finally:
        db.close()

    return jsonify({"status": "ok"}), 200


def _extract_gw_text_fields(html: str) -> list:
    """Return [{id, tag, content}] for every data-gw-text element in html."""
    from html import unescape as _unescape
    fields = []
    seen = set()
    for m in re.finditer(r'\bdata-gw-text="([^"]+)"', html):
        field_id = m.group(1)
        if field_id in seen:
            continue
        seen.add(field_id)
        tag_start = html.rfind('<', 0, m.start())
        tag_match = re.match(r'<([a-zA-Z][a-zA-Z0-9]*)', html[tag_start:])
        if not tag_match:
            continue
        tag_name = tag_match.group(1)
        open_end = html.find('>', m.end()) + 1
        if open_end == 0:
            continue
        close_pos = html.lower().find(f'</{tag_name.lower()}>', open_end)
        if close_pos == -1:
            continue
        inner = html[open_end:close_pos]
        plain = re.sub(r'<[^>]+>', '', inner).strip()
        plain = _unescape(plain)
        if plain:
            fields.append({'id': field_id, 'tag': tag_name.lower(), 'content': plain})
    return [f for f in fields if not _is_groundwork_credit(f['content'])]


def _is_groundwork_credit(text: str) -> bool:
    """True if text is (or contains) the 'Website by Groundwork' builder credit —
    never editable/removable via the sidebar, whichever data-gw-text field it
    ends up baked into."""
    t = text.lower()
    return "groundwork" in t and ("website by" in t or "groundworkbuild.com" in t)


def _update_gw_text_field(html: str, field_id: str, new_text: str):
    """Replace text content of data-gw-text="field_id". Returns (new_html, success)."""
    from html import escape as _escape
    from html import unescape as _unescape
    attr = f'data-gw-text="{field_id}"'
    attr_pos = html.find(attr)
    if attr_pos == -1:
        return html, False
    tag_start = html.rfind('<', 0, attr_pos)
    tag_match = re.match(r'<([a-zA-Z][a-zA-Z0-9]*)', html[tag_start:])
    if not tag_match:
        return html, False
    tag_name = tag_match.group(1)
    open_end = html.find('>', attr_pos) + 1
    if open_end == 0:
        return html, False
    close_pos = html.lower().find(f'</{tag_name.lower()}>', open_end)
    if close_pos == -1:
        return html, False
    current_plain = _unescape(re.sub(r'<[^>]+>', '', html[open_end:close_pos])).strip()
    if _is_groundwork_credit(current_plain):
        return html, False
    return html[:open_end] + _escape(new_text) + html[close_pos:], True


# ── Photo manager (added 2026-07-23) ────────────────────────────────────────
# Mirrors the data-gw-text edit mechanism (_extract_gw_text_fields /
# _update_gw_text_field above) but for whole portfolio photo cards, which
# need to move/insert/delete as a unit (image + caption together), not as a
# single text node. Relies on markers build_prompt.py now bakes into every
# generation with real photos: data-gw-photo-grid="1" on the grid container,
# data-gw-photo-card="{slot}" on each card, data-gw-photo="{slot}" on the
# <img>, data-gw-caption="{slot}" on the caption element. Generations from
# before this change have none of these markers — every function below
# degrades to a clean (html, False) / None on missing markers rather than
# guessing at unmarked markup, and the API layer surfaces that as "photo
# editing not available for this site," same pattern as the old
# no-fields-state for text editing.

def _find_enclosing_tag(html: str, attr_pos: int):
    """Given the string position of an attribute match, return
    (tag_name_lower, open_start, open_end) for the tag that attribute is
    on — open_end is the index right after that tag's own '>'. None if the
    tag can't be parsed."""
    tag_start = html.rfind('<', 0, attr_pos)
    tag_match = re.match(r'<([a-zA-Z][a-zA-Z0-9]*)', html[tag_start:])
    if not tag_match:
        return None
    tag_name = tag_match.group(1).lower()
    open_end = html.find('>', attr_pos)
    if open_end == -1:
        return None
    return tag_name, tag_start, open_end + 1


def _find_matching_close(html: str, tag_name: str, search_from: int):
    """Depth-matched close position for the tag_name whose opening tag ends
    at search_from — only tracks nesting of THIS tag name against itself
    (a nested <img>/<figcaption>/<p>/<span> inside a <div> card doesn't
    affect the div's own depth count, so this doesn't need a full HTML
    parser). Returns (close_start, close_end) — close_end is right after
    the matching '</tag_name>' — or None if unbalanced/not found."""
    pattern = re.compile(rf'<(/?){tag_name}\b[^>]*?(/?)>', re.IGNORECASE)
    depth = 1
    for m in pattern.finditer(html, search_from):
        is_close = m.group(1) == '/'
        is_self_closing = m.group(2) == '/'
        if is_close:
            depth -= 1
            if depth == 0:
                return m.start(), m.end()
        elif not is_self_closing:
            depth += 1
    return None


def _find_photo_grid(html: str):
    """Returns (tag_name, grid_open_end, grid_close_start) for the
    data-gw-photo-grid="1" container, or None if absent."""
    m = re.search(r'data-gw-photo-grid="1"', html)
    if not m:
        return None
    enclosing = _find_enclosing_tag(html, m.start())
    if not enclosing:
        return None
    tag_name, _, open_end = enclosing
    close = _find_matching_close(html, tag_name, open_end)
    if not close:
        return None
    close_start, _ = close
    return tag_name, open_end, close_start


def _find_photo_card(html: str, slot: str):
    """Returns (tag_name, card_start, card_end) for the full
    data-gw-photo-card="{slot}" element (from its '<tag' through its
    matching '</tag>' inclusive), or None if not found/unbalanced."""
    m = re.search(rf'data-gw-photo-card="{re.escape(slot)}"', html)
    if not m:
        return None
    enclosing = _find_enclosing_tag(html, m.start())
    if not enclosing:
        return None
    tag_name, card_start, open_end = enclosing
    close = _find_matching_close(html, tag_name, open_end)
    if not close:
        return None
    _, close_end = close
    return tag_name, card_start, close_end


def _last_photo_card_slot(html: str):
    """The slot name of the last data-gw-photo-card in the grid (used as
    the clone template for adding a new photo) — None if there are no
    cards at all."""
    grid = _find_photo_grid(html)
    if not grid:
        return None
    _, open_end, close_start = grid
    slots = re.findall(r'data-gw-photo-card="([^"]+)"', html[open_end:close_start])
    return slots[-1] if slots else None


def _remove_gw_photo_card(html: str, slot: str):
    """Delete a photo card entirely. Returns (new_html, success)."""
    card = _find_photo_card(html, slot)
    if not card:
        return html, False
    _, card_start, card_end = card
    return html[:card_start] + html[card_end:], True


def _update_gw_caption_field(html: str, slot: str, new_caption: str):
    """Replace the inner text of data-gw-caption="{slot}". Returns
    (new_html, success) — same shape as _update_gw_text_field."""
    from html import escape as _escape
    m = re.search(rf'data-gw-caption="{re.escape(slot)}"', html)
    if not m:
        return html, False
    enclosing = _find_enclosing_tag(html, m.start())
    if not enclosing:
        return html, False
    tag_name, _, open_end = enclosing
    close = _find_matching_close(html, tag_name, open_end)
    if not close:
        return html, False
    close_start, close_end = close
    return html[:open_end] + _escape(new_caption) + html[close_start:], True


def _add_gw_photo_card(html: str, new_slot: str, new_data_uri: str, new_caption: str):
    """Clone the last existing photo card (preserving whatever styling/
    classes Claude gave it) and insert a copy — with the slot/src/caption
    swapped — as the new last card in the grid. Returns (new_html, success).
    Fails cleanly (no template to clone) if the site has no photo cards at
    all yet — e.g. it was generated with the no-photos placeholder state."""
    last_slot = _last_photo_card_slot(html)
    if not last_slot:
        return html, False
    card = _find_photo_card(html, last_slot)
    grid = _find_photo_grid(html)
    if not card or not grid:
        return html, False
    _, card_start, card_end = card
    _, _, grid_close_start = grid

    clone = html[card_start:card_end]
    # Slot markers: data-gw-photo-card / data-gw-photo / data-gw-caption
    clone = re.sub(rf'(data-gw-photo-card|data-gw-photo|data-gw-caption)="{re.escape(last_slot)}"',
                   rf'\1="{new_slot}"', clone)
    # The cloned <img>'s src — replace the first src="..." after this
    # clone's own data-gw-photo="{new_slot}" marker (already swapped above),
    # since that's what identifies which img in the clone is the photo.
    img_marker = f'data-gw-photo="{new_slot}"'
    marker_pos = clone.find(img_marker)
    if marker_pos != -1:
        src_match = re.search(r'src="[^"]*"', clone[marker_pos:])
        if src_match:
            s, e = marker_pos + src_match.start(), marker_pos + src_match.end()
            clone = clone[:s] + f'src="{new_data_uri}"' + clone[e:]
    # Caption — replace whatever text was in the cloned card's caption
    # element with the new one (escaped), same tag-depth approach as
    # _update_gw_caption_field but scoped to this clone fragment.
    cap_marker = re.search(rf'data-gw-caption="{re.escape(new_slot)}"', clone)
    if cap_marker:
        cap_enclosing = _find_enclosing_tag(clone, cap_marker.start())
        if cap_enclosing:
            cap_tag, _, cap_open_end = cap_enclosing
            cap_close = _find_matching_close(clone, cap_tag, cap_open_end)
            if cap_close:
                cap_close_start, _ = cap_close
                from html import escape as _escape
                clone = clone[:cap_open_end] + _escape(new_caption or "") + clone[cap_close_start:]

    return html[:grid_close_start] + clone + html[grid_close_start:], True


def _inject_badge(html: str) -> str:
    """Inject a fixed 'Powered by Groundwork' badge into a live customer's site.
    Only called from handle_subdomain_request — never on watermarked previews."""
    badge = (
        '<a href="https://groundworkbuild.com" target="_blank" rel="noopener" '
        'style="position:fixed;bottom:16px;right:16px;z-index:9999;'
        'background:rgba(28,28,28,0.82);color:#fff;font-family:sans-serif;'
        'font-size:11px;font-weight:500;padding:6px 11px;border-radius:6px;'
        'text-decoration:none;letter-spacing:.01em;opacity:0.85;'
        'backdrop-filter:blur(4px);-webkit-backdrop-filter:blur(4px);'
        'transition:opacity .2s;" '
        'onmouseover="this.style.opacity=\'1\'" onmouseout="this.style.opacity=\'0.85\'">'
        'Powered by <strong>Groundwork</strong>'
        '</a>'
    )
    insert = html.rfind("</body>")
    if insert != -1:
        return html[:insert] + badge + html[insert:]
    return html + badge


def _inject_watermark(html: str, job_id: str, *, show_toast: bool = False, track_engagement: bool = True) -> str:
    checkout_url = f"/checkout.html?id={job_id}"
    editor_url = f"/editor.html?id={job_id}"

    # Restyled 2026-07-23 (by request) to match the rest of the site's
    # design language (#1C1C1C nav, Inter, #3B82F6 accent — same treatment
    # as preview.html's own top bar) instead of the old #1C2630/sans-serif
    # look, and to say the actual offer explicitly rather than a vague
    # "Get it live free today" — first month is genuinely free, no setup
    # fee, so the bar says that outright instead of making someone click
    # through to find out.
    watermark_bar = f"""<div id="gw-preview-bar" style="position:fixed;top:0;left:0;right:0;z-index:99999;background:#1C1C1C;border-bottom:1px solid #2C2C2C;font-family:Inter,Arial,sans-serif;box-shadow:0 2px 10px rgba(0,0,0,.35);">
  <div style="max-width:1280px;margin:0 auto;padding:10px 20px;display:flex;align-items:center;justify-content:space-between;gap:14px;flex-wrap:wrap;">
    <span style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;">
      <span style="background:rgba(59,130,246,.16);color:#9DBEF8;font-weight:700;font-size:11px;letter-spacing:.06em;text-transform:uppercase;padding:4px 10px;border-radius:20px;white-space:nowrap;">Preview</span>
      <span style="color:#B8B6B0;font-size:13px;">This site isn't published yet — free to go live, first month on us.</span>
    </span>
    <span style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;">
      <a href="{editor_url}" style="color:#DAD8D3;padding:9px 16px;border-radius:8px;border:1px solid #3C3C3C;text-decoration:none;font-weight:600;font-size:13.5px;">Edit</a>
      <a href="{checkout_url}" style="background:#3B82F6;color:#fff;padding:9px 18px;border-radius:8px;text-decoration:none;font-weight:700;font-size:13.5px;box-shadow:0 4px 14px -4px rgba(59,130,246,.65);white-space:nowrap;">Go live free — first month on us →</a>
    </span>
  </div>
</div>
<div style="height:60px;"></div>
<script>
(function(){{
  // The height:60px spacer above only pushes normal document flow — it
  // does nothing for the generated site's OWN nav if that nav also uses
  // position:fixed (a common pattern the model is free to choose per
  // build_prompt.py's Nav section), since a fixed element ignores flow
  // entirely. Without this, our watermark bar (z-index 99999) simply
  // covers the site's real nav bar. Rather than trying to guess the
  // site's own nav selector, find any OTHER fixed element pinned at (or
  // very near) the very top of the page and push it down by our bar's
  // real height — robust across whatever markup a given generation used.
  // This script tag is injected right after <body> opens — the rest of
  // the generated page (including its own nav) hasn't been parsed into
  // the DOM yet at this point, so it must wait for the full document
  // (querySelectorAll('body *') run immediately here would find almost
  // nothing) before it can find and adjust anything.
  function pushDownFixedElements() {{
    var bar = document.getElementById('gw-preview-bar');
    if (!bar) return;
    var barHeight = bar.offsetHeight;
    var all = document.querySelectorAll('body *');
    for (var i = 0; i < all.length; i++) {{
      var el = all[i];
      if (el === bar || bar.contains(el)) continue;
      var cs = window.getComputedStyle(el);
      if (cs.position !== 'fixed') continue;
      var top = parseFloat(cs.top);
      if (isNaN(top) || top > 4) continue;
      el.style.top = (top + barHeight) + 'px';
    }}
  }}
  if (document.readyState === 'complete') {{
    pushDownFixedElements();
  }} else {{
    window.addEventListener('load', pushDownFixedElements);
  }}
}})();
</script>"""

    toast_html = ""
    if show_toast:
        toast_key = f"gw_toast_{job_id}"
        toast_html = f"""<div id="gw-saved-toast" style="position:fixed;bottom:20px;right:20px;z-index:100000;background:#1C1C1C;color:#fff;font-family:sans-serif;font-size:14px;line-height:1.5;padding:14px 16px 14px 18px;border-radius:10px;box-shadow:0 4px 24px rgba(0,0,0,.45);display:flex;align-items:flex-start;gap:14px;max-width:300px;animation:gw-toast-in .35s ease;">
  <span style="flex:1;">We&#39;ve saved this to your account — <a href="/account/login" style="color:#93C5FD;font-weight:600;text-decoration:none;">sign in anytime</a> to find it.</span>
  <button onclick="gwDismissToast()" aria-label="Dismiss" style="background:none;border:none;color:#807E79;cursor:pointer;font-size:20px;line-height:1;padding:0;flex-shrink:0;margin-top:-1px;">&#215;</button>
</div>
<style>@keyframes gw-toast-in{{from{{opacity:0;transform:translateY(8px)}}to{{opacity:1;transform:translateY(0)}}}}</style>
<script>
(function(){{
  var KEY='{toast_key}';
  function gwDismissToast(){{
    var t=document.getElementById('gw-saved-toast');
    if(t){{t.style.transition='opacity .3s ease';t.style.opacity='0';setTimeout(function(){{t.remove();}},300);}}
    try{{localStorage.setItem(KEY,'1');}}catch(e){{}}
  }}
  window.gwDismissToast=gwDismissToast;
  try{{if(localStorage.getItem(KEY)){{var t=document.getElementById('gw-saved-toast');if(t)t.remove();return;}}}}catch(e){{}}
  setTimeout(function(){{gwDismissToast();}},7000);
}})();
</script>"""

    # View/engagement tracking (added 2026-07-18) — reports time-on-page and
    # scroll depth via sendBeacon on tab-hide/pagehide, to
    # /api/generate/<job_id>/engagement (job_engagement). Reports a DELTA
    # since its own last report, not cumulative-since-load, so repeated
    # tab-switching within one visit can't inflate total_view_seconds
    # server-side (each beacon's "seconds" is added, not set). view_count
    # itself is bumped separately, server-side only, in
    # _record_generation_view (every real serve of this HTML) — this script
    # only ever adds engagement depth on top of that count, never the count
    # itself, so a beacon that never fires (JS disabled, sendBeacon
    # unsupported) still leaves an accurate view_count, just without
    # time/scroll detail. Applies to every generation the moment its link
    # is next opened, including ones generated before this was added — this
    # function already rewrites stored HTML at serve time, nothing baked
    # into the DB row itself.
    tracking_script = f"""<script>
(function(){{
  var JOB_ID = '{job_id}';
  var lastBeaconTime = Date.now();
  var maxScroll = 0;
  function scrollPct(){{
    var doc = document.documentElement;
    var scrollable = doc.scrollHeight - doc.clientHeight;
    if (scrollable <= 0) return 100;
    var pct = Math.round(((window.scrollY || doc.scrollTop) / scrollable) * 100);
    return Math.max(0, Math.min(100, pct));
  }}
  window.addEventListener('scroll', function(){{
    var pct = scrollPct();
    if (pct > maxScroll) maxScroll = pct;
  }}, {{passive: true}});
  function sendBeacon(){{
    var now = Date.now();
    var seconds = Math.round((now - lastBeaconTime) / 1000);
    lastBeaconTime = now;
    if (seconds <= 0 && maxScroll === 0) return;
    var payload = JSON.stringify({{seconds: seconds, scroll_pct: maxScroll}});
    try {{
      if (navigator.sendBeacon) {{
        navigator.sendBeacon('/api/generate/' + JOB_ID + '/engagement', new Blob([payload], {{type: 'application/json'}}));
      }} else {{
        fetch('/api/generate/' + JOB_ID + '/engagement', {{method: 'POST', headers: {{'Content-Type': 'application/json'}}, body: payload, keepalive: true}});
      }}
    }} catch (e) {{}}
  }}
  document.addEventListener('visibilitychange', function(){{
    if (document.visibilityState === 'hidden') sendBeacon();
  }});
  window.addEventListener('pagehide', sendBeacon);
}})();
</script>"""

    robots_meta = '<meta name="robots" content="noindex, nofollow">'

    # track_engagement=False (added 2026-07-24, for the admin preview route)
    # omits this script entirely — an admin viewing their own preview link
    # must never contribute a scroll/time-on-page beacon to a customer's
    # own engagement stats, same reasoning as skipping _record_generation_view
    # on that route.
    body_open = re.search(r"<body[^>]*>", html, re.IGNORECASE)
    if body_open:
        insert_at = body_open.end()
        engagement_script = tracking_script if track_engagement else ""
        html = html[:insert_at] + watermark_bar + toast_html + engagement_script + html[insert_at:]

    head_open = re.search(r"<head[^>]*>", html, re.IGNORECASE)
    if head_open:
        insert_at = head_open.end()
        html = html[:insert_at] + robots_meta + html[insert_at:]

    return html


@app.route("/api/generate/<job_id>/photos/<filename>")
def job_photo(job_id, filename):
    job_dir = os.path.join(UPLOAD_DIR, job_id)
    return send_from_directory(job_dir, filename)


# Serve frontend static files. Explicit routes above (api/verify/account/admin)
# take priority over this catch-all regardless of declaration order, since
# Werkzeug ranks static path segments above the <path:path> converter.
@app.route("/", defaults={"path": "index.html"})
@app.route("/<path:path>")
def frontend(path):
    frontend_dir = os.path.join(os.path.dirname(__file__), "frontend")
    if os.path.exists(os.path.join(frontend_dir, path)):
        return send_from_directory(frontend_dir, path)
    return send_from_directory(frontend_dir, "index.html")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
