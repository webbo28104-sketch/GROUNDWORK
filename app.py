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
from urllib.parse import urlparse as _urlparse

import anthropic
import stripe
from PIL import Image, ImageDraw, ImageFilter
from flask import Flask, request, jsonify, send_from_directory, session, redirect, url_for, render_template_string, abort
from flask_cors import CORS
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from markupsafe import escape
from werkzeug.security import generate_password_hash, check_password_hash

from build_prompt import build_prompt
from outreach.site_extract import extract_site_assets
from models import SessionLocal, Lead, Generation, Account, GenerationImage, Domain, Prospect, SearchCell, PendingEmailDiscovery, EmailEventLog, OutreachTouch, DailySendCount, RampState, SmsDeliveryEvent, SurveyResponse, DiscoveryRunLog, init_db
from emails import (send_verification_email, send_resend_email, send_password_reset_email,
                    send_support_message_email, send_enquiry_email,
                    send_domain_order_admin_email, send_domain_order_customer_email,
                    send_domain_setup_failed_email, send_domain_live_email,
                    send_admin_magic_link_clicked_email, send_admin_payment_received_email,
                    send_site_ready_email)
from outreach.reply_handling import handle_inbound_sms, handle_inbound_email, handle_forced_sms_stop
from outreach.templates import SURVEY_DISCOUNT_PERCENT
from outreach.followup import STAGE_LABELS
from outreach.ramp import (
    get_health_signal, get_remaining_ramp_today, EMAIL_SPAM_RATE_TRIGGER,
    EMAIL_BOUNCE_RATE_TRIGGER, MIN_EMAIL_SAMPLE_SIZE, CIRCUIT_BREAKER_RECOVERY_DAYS,
)

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
# STRIPE_SETUP_PRICE_ID   → the one-time £99 price          (price_...)
# STRIPE_MONTHLY_PRICE_ID → the £24.99/month recurring price (price_...)
# STRIPE_ANNUAL_PRICE_ID  → the £249.99/year recurring price (price_...)
# STRIPE_SECRET_KEY       → sk_live_... (or sk_test_... for testing)
# STRIPE_WEBHOOK_SECRET   → whsec_... from `stripe listen` or dashboard
# SITE_URL                → https://groundworkbuild.com (used for redirect URLs)
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
STRIPE_SETUP_PRICE_ID = os.environ.get("STRIPE_SETUP_PRICE_ID", "")
STRIPE_MONTHLY_PRICE_ID = os.environ.get("STRIPE_MONTHLY_PRICE_ID", "")
STRIPE_ANNUAL_PRICE_ID = os.environ.get("STRIPE_ANNUAL_PRICE_ID", "")
SITE_URL = os.environ.get("SITE_URL", "https://groundworkbuild.com")
stripe.api_key = STRIPE_SECRET_KEY

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
    """Lowercase and remove spaces only. Other characters are left intact so
    _subdomain_has_invalid_chars() can catch and flag them."""
    return business_name.lower().replace(" ", "")


def _subdomain_has_invalid_chars(slug: str) -> bool:
    """DNS labels only allow a-z, 0-9. Returns True if anything else is present."""
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


def _run(job_id, prompt, logo_b64, logo_mime):
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
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
                messages.append({"role": "user", "content": [{"type": "text", "text": "The response was cut off by the token limit. Please continue the HTML from exactly where you stopped — complete all remaining open tags and sections without repeating any content already written."}]})
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

        with _jobs_lock:
            _jobs[job_id] = {"status": "done", "html": html}

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
        send_site_ready_email(
            email,
            business_name,
            preview_url=f"{SITE_URL}/preview.html?id={job_id}",
            account_login_url=f"{SITE_URL}/account/login",
        )


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
"""

_GW_LOGO_SVG = '<svg viewBox="0 0 48 48" width="28" height="28" fill="none"><path d="M 37 13.1 A 17 17 0 1 0 41 24 L 27 24" stroke="#3B82F6" stroke-width="4.6" stroke-linecap="round" stroke-linejoin="round"/><path d="M 30.9 18.2 A 9 9 0 1 0 30.9 29.8" stroke="#3B82F6" stroke-width="4.6" stroke-linecap="round"/></svg>'



def _admin_page(title: str, content: str, active: str = "") -> str:
    """Wrap admin content in the shared dark-header shell."""
    nav_items = [
        ("Dashboard", "/admin",    "dashboard"),
        ("Sites",    "/admin/generations",    "generations"),
        ("Domains &amp; margins", "/admin/domains", "domains"),
        ("Outreach", "/admin/outreach",  "outreach"),
        ("Discovery", "/admin/discovery", "discovery"),
        ("Follow-ups", "/admin/followups", "followups"),
        ("Funnel", "/admin/funnel", "funnel"),
        ("Deliverability", "/admin/deliverability", "deliverability"),
        ("Replies", "/admin/replies", "replies"),
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
    return "Monthly subscription" if invoice.billing_reason == "subscription_cycle" else "Setup fee"


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


def _claim_generate_and_redirect(db, prospect):
    """
    Shared by /claim/<token> and /claim/<token>/email — the actual
    "generate on click, not upfront" moment. No password/account barrier:
    per the current design, a first-time claim goes straight into
    generation, and funnel_substage moves to clicked_generated here (not
    account_created — that only happens later, when a password is actually
    set, via the existing /account/login flow). Idempotent: a repeat visit
    for a prospect that's already generated just redirects to the result
    instead of creating a second Lead/generation.
    """
    if prospect.clicked_at is None:
        prospect.clicked_at = datetime.utcnow()
        # Admin notification (2026-07-19) — backgrounded so a slow Resend
        # call never adds latency to this redirect. Fires exactly once per
        # prospect, guarded by the same clicked_at-is-None check that gates
        # the timestamp write itself, so a repeat/idempotent visit never
        # sends a second notification.
        threading.Thread(
            target=send_admin_magic_link_clicked_email,
            args=(prospect.business_name, prospect.id, bool(prospect.email)),
            daemon=True,
        ).start()

    if prospect.lead_id:
        lead = db.get(Lead, prospect.lead_id)
        existing_gen = db.query(Generation).filter(Generation.lead_id == lead.id).first()
        db.commit()
        if existing_gen:
            return redirect(f"/api/generate/{lead.public_id}/html")
        return redirect(f"/loading.html?id={lead.public_id}")

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
            return redirect(f"/api/generate/{existing_lead.public_id}/html")

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

    db.commit()

    _kickoff_generation(lead)
    return redirect(f"/loading.html?id={lead.public_id}")


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

        if not prospect.email:
            # Phone-only prospect — no email on file to create a Lead/Account
            # against. Route to the email-capture page instead (Section 9a).
            return redirect(f"/claim/{token}/email")

        return _claim_generate_and_redirect(db, prospect)
    finally:
        db.close()


@app.route("/claim/<token>/email", methods=["GET", "POST"])
def claim_email(token):
    """
    Email-capture page for phone-only prospects (has_findable_email=false,
    i.e. email_found=false — SMS-only outreach, docs/outreach-pipeline-spec.md
    Section 9a). This is the SMS magic-link destination for that segment —
    /claim/<token> redirects here automatically when the prospect has no
    email on file, so /s/<short_code> needs no changes to reach this page.

    On submit: records the email on both Prospect and Account, flips
    email_found (== has_findable_email) to true — making this prospect
    eligible for the parallel email follow-up track going forward, not just
    SMS — then reuses _claim_generate_and_redirect, the same generation
    kickoff /claim/<token> uses, rather than a separate path.
    """
    db = SessionLocal()
    try:
        prospect = db.query(Prospect).filter(Prospect.token == token).first()
        if not prospect:
            return redirect("/verify-error.html?reason=invalid")

        if prospect.email:
            # Already has an email on file (e.g. discovered later, or this
            # page already ran once) — nothing left to capture here.
            return redirect(f"/claim/{token}")

        if request.method == "GET":
            return _claim_email_form(prospect)

        email = (request.form.get("email") or "").strip().lower()
        if not email or not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', email):
            return _claim_email_form(prospect, error="Enter a valid email address.")

        prospect.email = email
        prospect.email_found = True  # has_findable_email — see Section 11's schema note

        account = db.query(Account).filter(Account.email == email).first()
        if account is None:
            account = Account(email=email)
            db.add(account)
        db.commit()

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
    ("using_someone_else", "Already using/planning to use someone else"),
    ("still_deciding", "Still deciding, haven't ruled it out"),
    ("technical_issue", "Ran into a problem trying to go live"),
    ("design_not_right", "The site itself wasn't right for my business"),
    ("other", "Other"),
]
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
    Answer a few quick questions about your website preview and we'll take {SURVEY_DISCOUNT_PERCENT}% off your
    £99 setup fee — a one-time code, valid for {SURVEY_DISCOUNT_WINDOW_DAYS} days, no obligation either way.
  </p>
  {error_html}
  <form method="post">
    {_survey_radio_group("decision", [
        ("went_live", "I've already gone live"),
        ("not_yet", "Not yet, but considering it"),
        ("not_going_live", "I don't plan to go live"),
    ], "Where are you at with your Groundwork site?")}
    {_survey_radio_group("primary_reason", _SURVEY_PRIMARY_REASONS, "What's the main factor in that?")}
    <div class="gwsvy-field">
      <span class="gwsvy-label">Anything else you'd add? <span class="gwsvy-optional">(optional)</span></span>
      <textarea class="gwsvy-text" name="reason_detail" maxlength="1000"></textarea>
    </div>
    {_survey_radio_group("decision_maker", _SURVEY_DECISION_MAKERS, "Is going live your call to make?")}
    {_survey_radio_group("already_pays_for_website", [("yes", "Yes"), ("no", "No")], "Do you currently pay for a website or hosting elsewhere?")}
    {_survey_radio_group("how_get_customers", _SURVEY_CUSTOMER_SOURCES, "How do most of your customers find you today?")}
    {_survey_radio_group("timeline", _SURVEY_TIMELINES, "If you were to go live, what's the timeline?")}
    <div class="gwsvy-field">
      <span class="gwsvy-label">What would make going live an easy yes? <span class="gwsvy-optional">(optional)</span></span>
      <textarea class="gwsvy-text" name="what_would_change_mind" maxlength="1000"></textarea>
    </div>
    <button type="submit" class="acct-btn" style="width:100%;">Submit and get my code</button>
  </form>
</div>"""
    return render_template_string(_account_page(inner, "Quick survey"))


def _survey_confirmation_page(response):
    if response.discount_code_issued:
        code_html = f"""<div style="background:#F0F9FF;border:1px solid #BAE6FD;border-radius:10px;padding:18px 20px;margin:18px 0;">
      <p style="margin:0 0 6px;font-size:13px;font-weight:700;color:#0369A1;text-transform:uppercase;letter-spacing:.04em;">{SURVEY_DISCOUNT_PERCENT}% off your setup fee</p>
      <p style="margin:0 0 4px;font-size:22px;font-weight:800;letter-spacing:.02em;font-family:monospace;">{escape(response.discount_code_issued)}</p>
      <p style="margin:0;font-size:13.5px;color:#5C5A56;">Enter this code at checkout — valid until {response.discount_expires_at.strftime("%d %b %Y") if response.discount_expires_at else "soon"}.</p>
    </div>"""
    else:
        code_html = (
            '<p style="color:#B45309;">Thanks — got your answers. We hit a snag generating your code automatically; '
            'reply to any of our emails and we\'ll sort it out by hand.</p>'
        )
    inner = f"""<div class="acct-card" style="text-align:center;">
      <h1 style="margin:0 0 10px;font-weight:800;font-size:24px;letter-spacing:-.02em;">Thanks — that's genuinely useful</h1>
      <p style="margin:0;font-size:15px;color:#5C5A56;line-height:1.6;">We read every response.</p>
      {code_html}
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
            return _survey_confirmation_page(existing)

        if request.method == "GET":
            return _survey_form_page(prospect)

        decision = request.form.get("decision", "")
        primary_reason = request.form.get("primary_reason", "")
        if decision not in ("went_live", "not_yet", "not_going_live") or not primary_reason:
            return _survey_form_page(prospect, error="Please answer the first two questions to continue.")

        already_pays_raw = request.form.get("already_pays_for_website")
        response = SurveyResponse(
            prospect_id=prospect.id,
            decision=decision,
            primary_reason=primary_reason,
            reason_detail=(request.form.get("reason_detail") or "").strip()[:1000] or None,
            decision_maker=request.form.get("decision_maker") or None,
            already_pays_for_website=(already_pays_raw == "yes") if already_pays_raw in ("yes", "no") else None,
            how_get_customers=request.form.get("how_get_customers") or None,
            timeline=request.form.get("timeline") or None,
            what_would_change_mind=(request.form.get("what_would_change_mind") or "").strip()[:1000] or None,
        )
        db.add(response)
        db.flush()  # need response.id-adjacent prospect fields available before commit below

        try:
            code, expires_at = _issue_survey_discount_code(prospect)
            response.discount_code_issued = code
            response.discount_expires_at = expires_at
            prospect.discount_code = code
            prospect.discount_expiry = expires_at
        except Exception:
            app.logger.exception("prospect_survey: failed to issue Stripe promo code for prospect %s — "
                                  "response saved regardless, code needs manual follow-up", prospect.id)

        db.commit()
        db.refresh(response)
        return _survey_confirmation_page(response)
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


@app.route("/admin")
@admin_required
def admin_dashboard():
    """Top-level admin landing page — the 5-KPI strip, full-size, with a
    date filter (quick presets + explicit from/to) so week-by-week or
    month-by-month comparison is possible, not just "this month." The same
    _render_kpi_strip() component appears (smaller context, no filter) on
    /admin/funnel and /admin/domains too, so the same numbers stay visible
    wherever they're relevant, not just here."""
    now = datetime.utcnow()
    preset = request.args.get("preset", "").strip()
    from_str = request.args.get("from", "").strip()
    to_str = request.args.get("to", "").strip()

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
        kpis = _compute_kpis(db, range_from=range_from, range_to=range_to)
        strip = _render_kpi_strip(kpis)
    finally:
        db.close()

    banner = ""

    presets = [
        ("this_week", "This week"), ("last_week", "Last week"),
        ("this_month", "This month"), ("last_month", "Last month"),
        ("all_time", "All time"),
    ]
    preset_links = "".join(
        f'<a href="/admin?preset={key}" style="padding:6px 13px;border-radius:999px;font-size:12.5px;font-weight:700;'
        f'text-decoration:none;{"background:#1C1C1C;color:#fff;" if preset == key else "background:#fff;color:#5C5A56;border:1px solid #D8D5CE;"}">{label}</a>'
        for key, label in presets
    )

    content = f"""
<h1 class="adm-title">Dashboard</h1>
<p class="adm-sub">The 5 headline KPIs for <strong>{escape(kpis["period_label"])}</strong>, computed fresh from real data on every load.</p>

<form method="get" style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:14px;">
  {preset_links}
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
  <button type="submit" style="background:#3B82F6;color:#fff;border:0;font-weight:700;padding:9px 18px;border-radius:7px;font-size:13.5px;cursor:pointer;">Apply</button>
  <a href="/admin" style="font-size:13px;color:#807E79;text-decoration:none;padding:9px 4px;">Reset to default (this month)</a>
</form>

{banner}
{strip}
<div class="adm-card" style="padding:20px 22px;">
  <p style="margin:0;font-size:13.5px;color:#5C5A56;">
    See these broken down further: <a href="/admin/funnel">Funnel</a> (per-stage outreach detail) ·
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
    /admin/prospects/<id>, the same profile page a magic-link click lands
    admins on elsewhere (e.g. send_admin_magic_link_clicked_email's link).
    Replaces the old Tinder-style approve/pass card, which had no real
    effect on what gets sent (see module comment above)."""
    args = request.args

    def _arg(name):
        v = (args.get(name) or "").strip()
        return v or None

    trade_tier = _arg("trade_tier")
    income_tier = _arg("income_tier")
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
  <td style="padding:8px 10px;">{_fmt_dt(p.created_at) or "—"}</td>
</tr>""" for p in prospects)

        showing_note = (
            f"Showing top {len(prospects)} of {total_matching} matching prospect(s), by score."
            if total_matching > len(prospects)
            else f"{total_matching} matching prospect(s)."
        )

        content = f"""
<h1 class="adm-title">Outreach prospects</h1>
<p class="adm-sub">Every sourced prospect is auto-eligible for sending once qualified and scored — there's no
approve/pass step anymore. Filter by any metric the pipeline tracks, then click a row for the full profile
(score breakdown, funnel timeline, touches, delivery events).</p>

<form method="get" class="adm-card" style="padding:16px 20px;margin-bottom:18px;display:flex;gap:12px;flex-wrap:wrap;align-items:flex-end;">
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
  <th style="text-align:left;padding:6px 10px;">Sourced</th>
</tr></thead>
<tbody>{rows_html or '<tr><td colspan="12" style="padding:16px 10px;color:#9A9893;">No prospects match these filters.</td></tr>'}</tbody>
</table>
</div>
"""
        return render_template_string(_admin_page("Outreach prospects", content, active="outreach"))
    finally:
        db.close()


@app.route("/admin/discovery")
@admin_required
def admin_discovery():
    """Overnight free email-discovery routine's results (added 2026-07-18,
    docs/outreach-pipeline-spec.md Section 4a) — the routine itself is a
    scheduled Claude Code cloud agent, not code in this repo, so this page
    is the only place its results are visible without checking
    claude.ai/code/routines directly. It writes one DiscoveryRunLog row as
    the last step of each run; this page just displays those rows,
    freshest first."""
    db = SessionLocal()
    try:
        runs = db.query(DiscoveryRunLog).order_by(DiscoveryRunLog.run_at.desc()).limit(30).all()
        pending_n = db.query(PendingEmailDiscovery).count()

        if not runs:
            content = f"""
<h1 class="adm-title">Discovery</h1>
<p class="adm-sub">Nightly free email-discovery routine (WebSearch-based, replaces the deleted paid Tier 2 —
see docs/outreach-pipeline-spec.md Section 4a). Runs at 03:30 UTC, after the free Tier 1 sweep and well
before send-job-cron.</p>
<div class="adm-card" style="padding:20px;">
  <p style="margin:0;">No runs logged yet — {pending_n} prospect(s) currently waiting in the discovery queue.
  First scheduled run: tomorrow 03:30 UTC. You can also trigger a run manually any time from
  <a href="https://claude.ai/code/routines" target="_blank">claude.ai/code/routines</a>.</p>
</div>
"""
            return render_template_string(_admin_page("Discovery", content, active="discovery"))

        latest = runs[0]
        source_rows = "".join(
            f'<tr><td style="padding:6px 10px;">{escape(str(k))}</td><td style="padding:6px 10px;text-align:right;">{v}</td></tr>'
            for k, v in (latest.source_breakdown or {}).items()
        ) or '<tr><td colspan="2" style="padding:10px;color:#9A9893;">No breakdown recorded.</td></tr>'

        latest_html = f"""
<div class="adm-card" style="padding:20px 22px;margin-bottom:20px;">
  <p class="adm-sub" style="margin:0 0 10px;">Most recent run — {_fmt_dt(latest.run_at)}</p>
  <div style="display:flex;gap:32px;flex-wrap:wrap;margin-bottom:14px;">
    <div><div style="font-size:22px;font-weight:800;">{latest.processed_n}</div><div class="muted" style="font-size:12px;">Processed</div></div>
    <div><div style="font-size:22px;font-weight:800;color:#059669;">{latest.found_n}</div><div class="muted" style="font-size:12px;">Emails found</div></div>
    <div><div style="font-size:22px;font-weight:800;">{latest.website_rediscovered_n}</div><div class="muted" style="font-size:12px;">Websites re-discovered</div></div>
    <div><div style="font-size:22px;font-weight:800;color:#9A9893;">{latest.finalized_null_n}</div><div class="muted" style="font-size:12px;">Finalized (no email found)</div></div>
  </div>
  <table style="width:60%;min-width:260px;border-collapse:collapse;font-size:13.5px;">
    <thead><tr style="color:#9A9893;font-size:11px;text-transform:uppercase;"><th style="text-align:left;padding:6px 10px;">Source</th><th style="text-align:right;padding:6px 10px;">Found</th></tr></thead>
    <tbody>{source_rows}</tbody>
  </table>
  {f'<p class="muted" style="margin:14px 0 0;">{escape(latest.notes)}</p>' if latest.notes else ""}
</div>"""

        history_rows = "".join(
            f'<tr><td style="padding:6px 10px;">{_fmt_dt(r.run_at)}</td>'
            f'<td style="padding:6px 10px;text-align:right;">{r.processed_n}</td>'
            f'<td style="padding:6px 10px;text-align:right;color:#059669;">{r.found_n}</td>'
            f'<td style="padding:6px 10px;text-align:right;">{r.website_rediscovered_n}</td>'
            f'<td style="padding:6px 10px;text-align:right;color:#9A9893;">{r.finalized_null_n}</td></tr>'
            for r in runs
        )

        content = f"""
<h1 class="adm-title">Discovery</h1>
<p class="adm-sub">Nightly free email-discovery routine (WebSearch-based, replaces the deleted paid Tier 2 —
see docs/outreach-pipeline-spec.md Section 4a). Runs at 03:30 UTC, after the free Tier 1 sweep and well
before send-job-cron. {pending_n} prospect(s) currently waiting in the discovery queue.</p>

{latest_html}

<h2 style="font-size:15px;font-weight:700;margin:28px 0 10px;">Run history ({len(runs)})</h2>
<div class="adm-card" style="overflow-x:auto;">
<table style="width:100%;border-collapse:collapse;font-size:13.5px;">
<thead><tr style="color:#9A9893;font-size:11px;text-transform:uppercase;border-bottom:1px solid #E6E3DC;">
  <th style="text-align:left;padding:6px 10px;">Run</th>
  <th style="text-align:right;padding:6px 10px;">Processed</th>
  <th style="text-align:right;padding:6px 10px;">Found</th>
  <th style="text-align:right;padding:6px 10px;">Websites re-discovered</th>
  <th style="text-align:right;padding:6px 10px;">Finalized (none)</th>
</tr></thead>
<tbody>{history_rows}</tbody>
</table>
</div>
"""
        return render_template_string(_admin_page("Discovery", content, active="discovery"))
    finally:
        db.close()


@app.route("/admin/followups")
@admin_required
def admin_followups():
    """Shows the actual queue of upcoming/overdue follow-up touches — added
    2026-07-19 because there was previously no way to see this outside of
    reading outreach/followup.py directly. Deliberately imports its
    constants and re-derives due/not-due the same way _due_stage() does,
    rather than hand-rolling separate logic that could silently drift from
    what run_followups() actually does."""
    from outreach.followup import (
        STAGE_BY_SUBSTAGE, MIN_DAYS_BY_SUBSTAGE, CATCH_ALL_MIN_DAYS, CATCH_ALL_MAX_DAYS,
        MAX_TOUCHES, EMAIL_REPLY_CAPTURE_READY, SMS_REPLY_CAPTURE_READY,
    )
    db = SessionLocal()
    try:
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

            rows.append({
                "id": p.id, "name": p.business_name or "—", "substage": p.funnel_substage,
                "stage_letter": stage_letter, "last_touch": p.last_touch_at,
                "due_label": due_label, "due_sort": due_sort,
                "touch_count": p.touch_count or 0, "channel": channel, "catch_all": catch_all_due,
            })

        rows.sort(key=lambda r: r["due_sort"])
        due_now_n = sum(1 for r in rows if r["due_sort"] == -1)

        gate_html = ""
        if not EMAIL_REPLY_CAPTURE_READY or not SMS_REPLY_CAPTURE_READY:
            missing = "email" if not EMAIL_REPLY_CAPTURE_READY else "SMS"
            gate_html = f"""
<div class="adm-card" style="padding:16px 24px;margin-bottom:20px;background:#FFFBEB;border-color:#FDE68A;">
  <p class="muted" style="margin:0;"><b>{missing.title()} follow-ups are withheld</b> — <code>{missing.upper()}_REPLY_CAPTURE_READY</code>
  isn't set to <code>true</code>. The other channel fires normally.</p>
</div>"""

        rows_html = "".join(f"""
<tr>
  <td style="padding:8px 10px;"><a href="/admin/prospects/{r['id']}" style="color:#2257CC;font-weight:600;text-decoration:none;">{r['name']}</a></td>
  <td style="padding:8px 10px;">{escape(STAGE_LABELS.get(r['stage_letter'], r['stage_letter']))}{' (catch-all)' if r['catch_all'] else ''}</td>
  <td style="padding:8px 10px;">{r['channel']}</td>
  <td style="padding:8px 10px;">{r['touch_count']}/{MAX_TOUCHES - 1}</td>
  <td style="padding:8px 10px;">{_fmt_dt(r['last_touch']) or '—'}</td>
  <td style="padding:8px 10px;font-weight:700;{'color:#B91C1C;' if r['due_sort'] == -1 else ''}">{r['due_label']}</td>
</tr>""" for r in rows)

        content = f"""
<h1 class="adm-title">Follow-ups</h1>
<p class="adm-sub">The live queue behind <code>run_followups()</code> — {len(rows)} prospect(s) still eligible for a follow-up touch,
{due_now_n} due right now. Runs nightly inside send-job-cron (08:00 UTC), first-priority against that day's ramp allowance,
before any new initial sends.</p>

{gate_html}

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
</div>
"""
        return render_template_string(_admin_page("Follow-ups", content, active="followups"))
    finally:
        db.close()


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

        touches = db.query(OutreachTouch).filter(
            OutreachTouch.prospect_id == p.id
        ).order_by(OutreachTouch.sent_at).all()
        touches_html = "".join(
            f'<tr><td style="padding:6px 10px;">{escape(t.stage)}</td>'
            f'<td style="padding:6px 10px;">{escape(t.channel)}</td>'
            f'<td style="padding:6px 10px;">{_fmt_dt(t.sent_at)}</td></tr>'
            for t in touches
        ) or '<tr><td colspan="3" style="padding:10px;color:#9A9893;">No logged touches.</td></tr>'

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

        generation_html = '<p class="muted">No claim yet — magic link not clicked.</p>'
        if lead:
            gen_links = ""
            if generation:
                if generation.view_count:
                    avg_seconds = round(generation.total_view_seconds / generation.view_count) if generation.view_count else 0
                    view_stats_html = (
                        f'<p class="muted" style="margin:6px 0 0;">'
                        f'<b style="color:#1C1C1C;">{generation.view_count}</b> view(s) · '
                        f'first {_fmt_dt(generation.first_viewed_at)} · last {_fmt_dt(generation.last_viewed_at)} · '
                        f'~{avg_seconds}s avg time on page · deepest scroll {generation.max_scroll_pct}%</p>'
                    )
                else:
                    view_stats_html = '<p class="muted" style="margin:6px 0 0;">Never actually viewed — generated but the link hasn\'t been opened (or was opened before this tracking existed and not since).</p>'
                gen_links = (
                    f'<a href="/admin/generations/{generation.id}/html" target="_blank">View generated site</a> · '
                    f'<a href="/admin/generations/{generation.id}/form-data" target="_blank">Raw form data</a>'
                    f'<p class="muted" style="margin:8px 0 0;">Status: <b style="color:#1C1C1C;">{escape(generation.status)}</b> · created {_fmt_dt(generation.created_at)}</p>'
                    f'{view_stats_html}'
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

<div class="adm-card" style="padding:16px 20px;">
  <p style="font-weight:700;margin:0 0 10px;">Survey response</p>
  {survey_html}
</div>
"""
        return render_template_string(_admin_page(escape(p.business_name or "Prospect"), content, active="funnel"))
    finally:
        db.close()


_KPI_INSTRUMENTATION_START = datetime(2026, 7, 14)


def _compute_kpis(db, range_from: datetime = None, range_to: datetime = None) -> dict:
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

    # 1. Clients reached out to (email) in the period — OutreachTouch,
    # stage == "initial" only. This used to be DailySendCount, which counts
    # every email send attempt (initial + follow-up combined) with no way to
    # tell them apart — so "Emails sent / month" was silently inflated by
    # every A/B/C/D follow-up touch on top of first contact. OutreachTouch
    # tags each row with its stage ("initial" vs "A"/"B"/"C"/"D"), so this
    # can now filter to first-contact sends only.
    # Bounce exclusion unchanged: a bounce is not a successfully sent email
    # (the message never reached an inbox), so distinct bounced-address
    # events (EmailEventLog) logged within the same period are subtracted.
    emails_attempted = db.query(OutreachTouch).filter(
        OutreachTouch.channel == "email",
        OutreachTouch.stage == "initial",
        OutreachTouch.sent_at >= period_start,
        OutreachTouch.sent_at <= period_end,
    ).count()
    bounced_n = db.query(EmailEventLog.resend_email_id).filter(
        EmailEventLog.event_type.in_(["email.bounced", "bounced"]),
        EmailEventLog.created_at >= period_start,
        EmailEventLog.created_at <= period_end,
    ).distinct().count()
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
    if bounced_emails_in_period:
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
    if filtering:
        active_at_start = db.query(Generation).filter(
            Generation.status == "live",
            Generation.is_internal == False,
            Generation.created_at < period_start,
            (Generation.canceled_at.is_(None)) | (Generation.canceled_at >= period_start),
        ).count()
        churned_in_period = db.query(Generation).filter(
            Generation.is_internal == False,
            Generation.canceled_at.isnot(None),
            Generation.canceled_at >= period_start, Generation.canceled_at <= period_end,
        ).count()
        churn_denom = active_at_start
        churn_numer = churned_in_period
        has_full_month_baseline = active_at_start > 0
    else:
        active_now = db.query(Generation).filter(
            Generation.status == "live", Generation.is_internal == False
        ).count()
        churned_month = db.query(Generation).filter(
            Generation.is_internal == False,
            Generation.canceled_at.isnot(None), Generation.canceled_at >= period_start
        ).count()
        churn_denom = active_now + churned_month
        churn_numer = churned_month
        has_full_month_baseline = db.query(Generation).filter(
            Generation.status == "live", Generation.is_internal == False, Generation.created_at < period_start
        ).count() > 0

    period_label = (
        f'{period_start.strftime("%d %b %Y")} – {period_end.strftime("%d %b %Y")}'
        if filtering else now.strftime("%B %Y")
    )

    return {
        "period_label": period_label,
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
    }


def _render_kpi_strip(kpis: dict) -> str:
    """Compact 5-tile KPI strip, reused verbatim on the main dashboard, the
    Funnel page, and the Domains page. Scoped CSS (kpi-* classes) rather
    than merged into _ADMIN_STYLE, same reasoning as the Funnel table and
    the outreach review card — a one-off component, not shared design
    system."""
    def tile(label, big, sub):
        return f"""<div class="kpi-tile">
          <div class="kpi-label">{escape(label)}</div>
          <div class="kpi-value">{big}</div>
          <div class="kpi-sub">{sub}</div>
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
    tiles += tile("Clients reached out to (email)", str(e["value"]), sent_sub)
    tiles += tile(
        "Magic link click rate",
        f'{c["pct"]}%' if c["pct"] is not None else "—",
        f'{c["numer"]}/{c["denom"]} sent' if c["denom"] else "no sends yet",
    )
    tiles += tile(
        "Generation → Paid",
        f'{g["pct"]}%' if g["pct"] is not None else "—",
        f'{g["numer"]}/{g["denom"]} clicked' if g["denom"] else "no outreach conversions yet",
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

    return f"""<style>
.kpi-strip{{display:grid;grid-template-columns:repeat(5,1fr);gap:14px;margin-bottom:24px;}}
.kpi-tile{{background:#fff;border:1px solid #E2E0DA;border-radius:12px;padding:16px 18px;}}
.kpi-label{{font-size:11.5px;font-weight:700;text-transform:uppercase;letter-spacing:.05em;color:#9A9893;margin-bottom:8px;}}
.kpi-value{{font-size:26px;font-weight:800;letter-spacing:-.02em;color:#1C1C1C;line-height:1;margin-bottom:6px;}}
.kpi-sub{{font-size:12px;color:#807E79;}}
@media (max-width: 980px){{.kpi-strip{{grid-template-columns:repeat(2,1fr);}}}}
</style>
<div class="kpi-strip">{tiles}</div>"""


_FUNNEL_STAGES = list(STAGE_LABELS.items())

_FUNNEL_STEPS = ["Sent", "Opened", "Clicked", "Generated", "Account Created", "Paid"]


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
    from_str = request.args.get("from", "").strip()
    to_str = request.args.get("to", "").strip()
    channel = request.args.get("channel", "both").strip().lower()
    if channel not in ("email", "sms", "both"):
        channel = "both"

    def _parse_date(s):
        try:
            return datetime.strptime(s, "%Y-%m-%d")
        except ValueError:
            return None

    range_from = _parse_date(from_str)
    range_to = _parse_date(to_str)

    db = SessionLocal()
    try:
        kpi_strip = _render_kpi_strip(_compute_kpis(db))
        extraction_quality_breakdown = _render_extraction_quality_breakdown(db)
        survey_breakdown = _render_survey_breakdown(db)

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
            if not gen or not gen.view_count:
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
<h2 style="font-size:15px;font-weight:700;margin:28px 0 10px;">Recent clicks ({len(recent_clicked)})</h2>
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

        # Bounced addresses in scope for this table's date range — reused
        # across every stage row below, same normalization as the
        # deliverability page's source breakdown (Resend sometimes logs the
        # quoted '"addr" <addr>' form rather than a bare address).
        bounce_event_q = db.query(EmailEventLog).filter(
            EmailEventLog.event_type.in_(["email.bounced", "bounced"])
        )
        if range_from:
            bounce_event_q = bounce_event_q.filter(EmailEventLog.created_at >= range_from)
        if range_to:
            bounce_event_q = bounce_event_q.filter(EmailEventLog.created_at < range_to + timedelta(days=1))
        bounced_emails_in_range = {
            (email_utils.parseaddr(e.to_email)[1] or e.to_email or "").strip().lower()
            for e in bounce_event_q.all() if e.to_email
        }

        rows_html = ""
        any_sent_at_all = False

        for stage_key, stage_label in _FUNNEL_STAGES:
            q = db.query(OutreachTouch).filter(OutreachTouch.stage == stage_key)
            if channel != "both":
                q = q.filter(OutreachTouch.channel == channel)
            if range_from:
                q = q.filter(OutreachTouch.sent_at >= range_from)
            if range_to:
                q = q.filter(OutreachTouch.sent_at < range_to + timedelta(days=1))
            touches = q.all()

            prospect_ids = sorted({t.prospect_id for t in touches})
            email_prospect_ids = {t.prospect_id for t in touches if t.channel == "email"}
            sent_n = len(prospect_ids)
            if sent_n:
                any_sent_at_all = True

            if sent_n:
                cohort = db.query(Prospect).filter(Prospect.id.in_(prospect_ids)).all()
                bounced_n = sum(
                    1 for p in cohort
                    if p.id in email_prospect_ids and p.email and p.email.strip().lower() in bounced_emails_in_range
                )
                opened_n = sum(1 for p in cohort if p.opened_at is not None)
                clicked_n = sum(1 for p in cohort if p.clicked_at is not None)
                lead_ids = [p.lead_id for p in cohort if p.lead_id is not None]
                generated_lead_ids = set()
                if lead_ids:
                    generated_lead_ids = {
                        g.lead_id for g in db.query(Generation.lead_id)
                        .filter(Generation.lead_id.in_(lead_ids)).all()
                    }
                generated_n = sum(1 for p in cohort if p.lead_id in generated_lead_ids)
                account_created_n = sum(1 for p in cohort if p.account_created_at is not None)
                paid_n = sum(1 for p in cohort if p.paid_at is not None)
            else:
                bounced_n = opened_n = clicked_n = generated_n = account_created_n = paid_n = 0

            # delivered_n, not sent_n, is the headline number and the
            # denominator for every downstream rate — same principle as the
            # KPI strip's "emails sent" fix: a bounced send never reached an
            # inbox, so it can't be a fair base for "did they open/click"
            # either. sent_n (the raw OutreachTouch attempt count) is still
            # shown, in the sub-caption, not hidden — this table is meant to
            # carry more detail than the single KPI tile, not less.
            delivered_n = sent_n - bounced_n
            counts = [delivered_n, opened_n, clicked_n, generated_n, account_created_n, paid_n]
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

            cells = ""
            for i, (label, count) in enumerate(zip(_FUNNEL_STEPS, counts)):
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

        header_cells = "".join(f'<th style="text-align:center;">{s}</th>' for s in _FUNNEL_STEPS)

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
<p class="adm-sub">Per-stage, per-channel outreach funnel — how many of each email type went out in the
selected range, and what happened after. Each follow-up row's cohort is defined by the prospect's
funnel stage at send time (see row labels below), not by "1st/2nd/3rd touch" — so e.g. the "viewed
site, no account" row's Generated column will read ~100% by construction, since that's who it's sent
to, not a conversion it caused. "Opened" means "has an opened_at timestamp from any email, ever," not
"opened this specific touch" (opened_at is a single per-prospect field, not per-message). The
"Sent" column shows delivered count as the headline number, with attempted/bounced beneath it — a
bounced send never reached an inbox, so every rate to its right is based on delivered, not attempted.</p>
<p class="adm-sub" style="color:#B45309;background:#FEF3C7;padding:10px 14px;border-radius:8px;">
⚠ Opened relies on Resend's tracking pixel, which is off by default per sending domain and can also be
silently blocked by the recipient's mail client — a prospect can (and regularly does) click the real
magic link and generate a site without ever registering an "opened" event first. If every row here
shows 0 opens despite real sends/clicks, that's a strong signal open tracking isn't enabled on the
sending domain in the Resend dashboard, not that this table is broken — the webhook code that would
record a real open has been verified working.</p>

{kpi_strip}

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
    <label style="display:block;font-size:12px;font-weight:600;color:#5C5A56;margin-bottom:4px;">Channel</label>
    <select name="channel" style="padding:8px 10px;border:1px solid #D8D5CE;border-radius:7px;font-size:13.5px;">
      <option value="both" {"selected" if channel == "both" else ""}>Email + SMS</option>
      <option value="email" {"selected" if channel == "email" else ""}>Email only</option>
      <option value="sms" {"selected" if channel == "sms" else ""}>SMS only</option>
    </select>
  </div>
  <button type="submit" style="background:#3B82F6;color:#fff;border:0;font-weight:700;padding:9px 18px;border-radius:7px;font-size:13.5px;cursor:pointer;">Apply</button>
</form>

<div class="adm-card" style="overflow-x:auto;">
<table>
<thead><tr><th>Stage</th>{header_cells}</tr></thead>
<tbody>{rows_html}</tbody>
</table>
</div>

<h2 style="font-size:15px;font-weight:700;margin:28px 0 10px;">Currently in the pipeline ({total_in_pipeline})</h2>
<div class="statsbar" style="justify-content:flex-start;text-align:left;">{summary_html}</div>

{recent_clicks_html}

{survey_breakdown}

{extraction_quality_breakdown}
"""
        return render_template_string(_admin_page("Funnel", content, active="funnel"))
    finally:
        db.close()


_GMAIL_HARD_CEILING = 0.003  # 0.3% — not a circuit-breaker value in code (only
# EMAIL_SPAM_RATE_TRIGGER, 0.1%, actually trips anything), this is the "degradation
# begins well before this" reference ceiling docs/outreach-pipeline-spec.md
# Section 15 cites Gmail as using. Plotted for context only.


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
            actionable_callout = f"""
          <div style="background:#FEF2F2;border:1px solid #FCA5A5;border-radius:8px;padding:12px 14px;margin-bottom:12px;">
            <p style="margin:0;font-size:13.5px;color:#B91C1C;"><b>Past the point of "watch and see":</b> {names}
            {"has" if len(actionable_bad_sources) == 1 else "have"} a real sample size (15+) at or above the
            {EMAIL_BOUNCE_RATE_TRIGGER * 100:.0f}% bounce trigger. This is your system finding bad emails, not a
            deliverability/reputation problem — but every bounce still counts against the trailing bounce rate that
            holds the ramp at the floor. Worth downweighting or adding a stricter check for this source specifically,
            rather than raising the ramp's tolerance overall.</p>
          </div>"""
        source_breakdown_html = f"""
        <div class="adm-card" style="padding:16px 20px;margin-top:10px;">
          <p style="font-weight:700;margin:0 0 6px;">Bounce rate by discovery source (all-time)</p>
          <p class="muted" style="margin:0 0 10px;">Not fed into scoring/selection yet — still monitoring-only, but sources marked
          below have crossed a real sample size, so the rate shown is a genuine signal, not noise.</p>
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

        # ---- SMS: honest "is there real data" check ----
        esendex_configured = bool(os.environ.get("ESENDEX_USERNAME") and os.environ.get("ESENDEX_PASSWORD"))
        sms_event_count = db.query(func.count(SmsDeliveryEvent.id)).scalar() or 0
        sms_signal = get_health_signal("sms")
        sms_ramp = db.query(RampState).filter(RampState.channel == "sms").first()

        if sms_event_count == 0:
            sms_body_html = f"""
            <div class="adm-card" style="padding:24px;">
              <p style="font-weight:700;margin:0 0 8px;">Not yet configured — no real delivery data exists.</p>
              <p class="muted" style="margin:0 0 6px;">
                Esendex credentials {"are" if esendex_configured else "are <b>not</b>"} set in the environment,
                but <code>SmsDeliveryEvent</code> has zero rows. The poll job that would create them
                (<code>python -m outreach.sms_status_poll</code>, meant to run hourly per its own docstring)
                is <b>not scheduled anywhere</b> — confirmed 2026-07-19 against Railway directly: sourcing-cron,
                email-discovery-cron, send-job-cron, domain-billing-cron, and pending-edits-apply-cron all exist
                and run on schedule, but none of them call this poll job. Until it's on a schedule of its own,
                this section will stay empty no matter how many SMS sends go out.
              </p>
              <p class="muted" style="margin:0;">get_health_signal("sms") currently returns
                <code>{"a value" if sms_signal else "None"}</code> — {"unexpected given zero events; investigate" if sms_signal else "expected, given no delivery events have ever been logged"}.</p>
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
<p class="adm-sub">Circuit-breaker health for email and SMS sending, per docs/outreach-pipeline-spec.md Section 15.</p>

<h2 style="font-size:16px;font-weight:800;margin:24px 0 4px;">Email</h2>
<div class="adm-card" style="padding:20px 20px 8px;">{email_chart}</div>
{email_signal_html}
{email_ramp_html}
{source_breakdown_html}

<h2 style="font-size:16px;font-weight:800;margin:28px 0 4px;">SMS</h2>
{sms_body_html}
{sms_ramp_html}

<h2 style="font-size:16px;font-weight:800;margin:28px 0 4px;">Google Postmaster Tools</h2>
<div class="adm-card" style="padding:16px 20px;">
  <p class="muted" style="margin:0;">
    <b style="color:#1C1C1C;">Not connected.</b> Requires manual domain verification in the Postmaster
    dashboard first (a one-time human step, not a code change) before any API access exists. Email
    health above is computed entirely from the Resend webhook proxy in the meantime, per Section 15.
  </p>
</div>
"""
        return render_template_string(_admin_page("Deliverability", content, active="deliverability"))
    finally:
        db.close()


@app.route("/admin/replies")
@admin_required
def admin_replies():
    db = SessionLocal()
    try:
        replied = db.query(Prospect).filter(Prospect.funnel_substage == "replied").order_by(
            Prospect.id.desc()
        ).all()
        stopped = db.query(Prospect).filter(
            (Prospect.email_unsubscribed == True) | (Prospect.sms_unsubscribed == True)  # noqa: E712
        ).order_by(Prospect.id.desc()).all()

        email_unsub_n = db.query(Prospect).filter(Prospect.email_unsubscribed == True).count()  # noqa: E712
        sms_unsub_n = db.query(Prospect).filter(Prospect.sms_unsubscribed == True).count()  # noqa: E712
        total_unsub_n = len(stopped)  # distinct prospects — a prospect stopped on both channels counts once

        rows = []
        for p in replied:
            rows.append((p, "Non-stop (paused for review)", None))
        for p in stopped:
            channel = "SMS" if p.sms_unsubscribed else "Email"
            at = p.sms_unsubscribed_at if p.sms_unsubscribed else p.email_unsubscribed_at
            rows.append((p, f"Stop ({channel})", at))

        rows.sort(key=lambda r: r[2] or datetime.min, reverse=True)

        if not rows:
            table_html = '<div class="adm-card" style="padding:40px;text-align:center;color:#9A9893;font-size:14px;">No replies logged yet.</div>'
        else:
            trs = ""
            for p, classification, at in rows:
                badge_color = "#DC2626" if classification.startswith("Stop") else "#B45309"
                when = at.strftime("%d %b %Y %H:%M UTC") if at else '<span class="muted">not tracked</span>'
                trs += (
                    f'<tr><td>{escape(p.business_name or "—")}</td>'
                    f'<td>{escape(p.email or p.phone or "—")}</td>'
                    f'<td><span class="status-pill" style="background:{badge_color}22;color:{badge_color};">{escape(classification)}</span></td>'
                    f'<td>{when}</td></tr>'
                )
            table_html = f"""<div class="adm-card" style="overflow-x:auto;">
<table><thead><tr><th>Business</th><th>Contact</th><th>Classification</th><th>When</th></tr></thead>
<tbody>{trs}</tbody></table></div>"""

        content = f"""
<h1 class="adm-title">Replies</h1>
<p class="adm-sub">
  <b>The actual reply message text isn't stored anywhere in the app</b> — <code>handle_inbound_email()</code>
  only classifies each reply as stop-intent or not and updates the prospect's funnel state. The real
  message content only exists in the forwarded copy sitting in
  <a href="mailto:groundwork-build@outlook.com">groundwork-build@outlook.com</a>. Check that inbox to
  read what anyone actually wrote.
</p>

<div style="display:flex;gap:16px;flex-wrap:wrap;align-items:center;margin-bottom:16px;font-size:13px;color:#5C5A56;font-weight:600;">
  <span><b style="color:#1C1C1C;font-weight:800;font-size:15px;margin-right:4px;">{total_unsub_n}</b>unsubscribed (any channel)</span>
  <span><b style="color:#1C1C1C;font-weight:800;font-size:15px;margin-right:4px;">{email_unsub_n}</b>email</span>
  <span><b style="color:#1C1C1C;font-weight:800;font-size:15px;margin-right:4px;">{sms_unsub_n}</b>SMS</span>
</div>

<p class="adm-sub" style="color:#B45309;background:#FEF3C7;padding:10px 14px;border-radius:8px;">
  ⚠ Non-stop replies (paused for human review) have no timestamp in the schema — only stop-intent
  opt-outs record one (<code>email_unsubscribed_at</code>/<code>sms_unsubscribed_at</code>). Rows below
  marked "not tracked" are real gaps, not a rendering bug.
</p>

{table_html}
"""
        return render_template_string(_admin_page("Replies", content, active="replies"))
    finally:
        db.close()


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

    from outreach.email_discovery import is_valid_email, looks_like_guess

    body = request.get_json(silent=True) or {}
    prospect_id = body.get("prospect_id")
    email_raw = body.get("email")  # may be None/null
    source = body.get("source", "web_search")
    force = bool(body.get("force", False))

    if not prospect_id:
        return jsonify({"error": "prospect_id is required"}), 400

    email = None if not email_raw else str(email_raw).strip()

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

    from outreach.email_discovery import is_valid_email, looks_like_guess
    from outreach.email_verify import has_deliverable_domain

    try:
        prospect_id = int(request.args.get("prospect_id", ""))
    except (ValueError, TypeError):
        return jsonify({"error": "prospect_id must be an integer"}), 400

    email_raw = request.args.get("email", "").strip()
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
    site is unpublished" with a "Go live — £99 + £24.99/mo" CTA, which is
    actively wrong for a site that WAS live and paid for — re-showing the
    setup fee implies they'd be charged it again, which they wouldn't be
    (reinstating just resumes the same subscription). This route shows
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
    """Return metadata used by checkout.html and live.html — validity/taken
    checks for the auto-derived address, and the assigned address itself
    once one exists (i.e. post-payment, once gen.subdomain is set).

    Deliberately does NOT return the pre-payment candidate address itself
    (neither the bare slug nor a preview URL) — only the invalid/taken
    booleans. The address is computed and validated before checkout so a
    customer can't pay for one that's broken or already used, but the
    literal string is only revealed once payment actually goes through,
    per the 2026-07-19 change to stop showing it beforehand."""
    db = SessionLocal()
    try:
        gen = db.query(Generation).join(Lead).filter(Lead.public_id == job_id).first()
        if not gen:
            return jsonify({"error": "not found"}), 404
        business_name = (gen.lead.form_data or {}).get("business_name", "")
        preview_slug = _make_subdomain(business_name)
        invalid = _subdomain_has_invalid_chars(preview_slug) if preview_slug else True
        taken = (
            _subdomain_is_taken(preview_slug, db, exclude_gen_id=gen.id)
            if preview_slug and not invalid
            else False
        )
        assigned_url = (
            f"https://{gen.subdomain}.{_SUBDOMAIN_BASE}" if gen.subdomain else None
        )
        return jsonify({
            "status": gen.status,
            "business_name": business_name,
            "subdomain": gen.subdomain,
            "subdomain_url": assigned_url,
            "subdomain_invalid_chars": invalid,
            "subdomain_taken": taken,
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

        db.commit()
        return jsonify({"ok": True})
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

        business_name = (gen.lead.form_data or {}).get("business_name", "")
        slug = _make_subdomain(business_name)

        if not slug:
            return jsonify({"error": "Company name is missing — please contact us."}), 422

        if _subdomain_has_invalid_chars(slug):
            app.logger.warning(f"Checkout blocked — invalid subdomain chars: {business_name!r} → {slug!r}")
            return jsonify({
                "error": "Your company name contains characters (e.g. apostrophes or symbols) "
                         "that can't be used in a web address. Please email us at "
                         "groundwork-build@outlook.com and we'll sort out your address before you pay."
            }), 422

        if _subdomain_is_taken(slug, db, exclude_gen_id=gen.id):
            # Deliberately doesn't include the candidate slug in the message —
            # see job_info()'s docstring for why the address itself isn't
            # revealed before payment.
            app.logger.warning(f"Checkout blocked — subdomain taken: {business_name!r} → {slug!r}")
            return jsonify({
                "error": "There's a naming conflict with another company using a very similar name. "
                         "Please email us at groundwork-build@outlook.com and we'll help you sort it out before you pay."
            }), 409

        # Survey-issued setup-fee discount (2026-07-17, reduced from a full
        # waiver to SURVEY_DISCOUNT_PERCENT on 2026-07-18) — deliberately
        # checked and applied entirely app-side rather than via a Stripe
        # Coupon; see _issue_survey_discount_code's docstring for why (a
        # percent_off Coupon's applies_to restriction was found to be
        # silently dropped by the live API, which would have discounted the
        # whole checkout, not just the setup fee). apply_setup_discount is
        # the only effect a valid code has: the setup line item is swapped
        # for a discounted price_data line item below, nothing else changes.
        # Discount codes only ever discount the setup fee (see below) — moot
        # on a reactivation, since there isn't one. Skip validation entirely
        # rather than erroring, so a stale/irrelevant code in the field
        # can't block a reactivating customer from paying.
        submitted_code = (data.get("discount_code") or "").strip().upper()
        apply_setup_discount = False
        if submitted_code and not is_reactivation:
            prospect = db.query(Prospect).filter(Prospect.lead_id == gen.lead_id).first()
            if (prospect and prospect.discount_code
                    and prospect.discount_code.upper() == submitted_code
                    and prospect.discount_expiry and prospect.discount_expiry > datetime.utcnow()):
                apply_setup_discount = True
            else:
                return jsonify({"error": "That discount code isn't valid or has expired."}), 422
    finally:
        db.close()

    if is_reactivation:
        # No setup fee, no trial — this is a resubscribe, not a first-time
        # purchase. Charged the plan's recurring price immediately.
        line_items = [{"price": recurring_price_id, "quantity": 1}]
        subscription_data = {}
    else:
        # First-month-free trial is a monthly-only promo — annual customers
        # already get the discounted rate, so trial_period_days isn't stacked
        # on top of that (see design_handoff_marketing_consistency notes).
        subscription_data = {"trial_period_days": 30} if plan == "monthly" else {}

        if apply_setup_discount:
            # A dynamic price_data line item, not the fixed STRIPE_SETUP_PRICE_ID —
            # unit_amount/currency/product read live from the real setup Price so
            # this never drifts if that price ever changes, and the discounted
            # amount is computed here in code, not via any Stripe-side discount
            # mechanism (see the docstring above for why that's deliberate).
            setup_price = stripe.Price.retrieve(STRIPE_SETUP_PRICE_ID)
            discounted_amount = round(setup_price.unit_amount * (100 - SURVEY_DISCOUNT_PERCENT) / 100)
            setup_line_item = {
                "price_data": {
                    "currency": setup_price.currency,
                    "product": setup_price.product,
                    "unit_amount": discounted_amount,
                },
                "quantity": 1,
            }
        else:
            setup_line_item = {"price": STRIPE_SETUP_PRICE_ID, "quantity": 1}
        line_items = [setup_line_item, {"price": recurring_price_id, "quantity": 1}]

    cs = stripe.checkout.Session.create(
        mode="subscription",
        line_items=line_items,
        subscription_data=subscription_data,
        allow_promotion_codes=True,
        client_reference_id=job_id,
        metadata={"discount_code_redeemed": submitted_code} if apply_setup_discount else {},
        success_url=f"{SITE_URL}/live.html?id={job_id}",
        cancel_url=(
            f"{SITE_URL}/api/generate/{job_id}/preserved" if is_reactivation
            else f"{SITE_URL}/api/generate/{job_id}/html"
        ),
    )
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

                        # Assign subdomain (the checkout route already validated this;
                        # the double-check here guards the rare simultaneous-payment race).
                        if not gen.subdomain:
                            business_name = (gen.lead.form_data or {}).get("business_name", "")
                            slug = _make_subdomain(business_name)
                            if slug and not _subdomain_has_invalid_chars(slug):
                                if not _subdomain_is_taken(slug, db, exclude_gen_id=gen.id):
                                    gen.subdomain = slug
                                else:
                                    app.logger.error(
                                        f"Subdomain race: {slug!r} taken when webhook fired for job {job_id}"
                                    )

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
                prospect = handle_forced_sms_stop(db, from_phone)
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


def _inject_watermark(html: str, job_id: str, *, show_toast: bool = False) -> str:
    checkout_url = f"/checkout.html?id={job_id}"
    editor_url = f"/editor.html?id={job_id}"

    watermark_bar = f"""<div id="gw-preview-bar" style="position:fixed;top:0;left:0;right:0;z-index:99999;background:#1C2630;color:#fff;font-family:sans-serif;font-size:13px;display:flex;align-items:center;justify-content:space-between;padding:10px 20px;box-shadow:0 2px 8px rgba(0,0,0,0.3);">
  <span>⚠ Preview — this site is unpublished and watermarked</span>
  <span style="display:flex;align-items:center;gap:10px;">
    <a href="{editor_url}" style="background:transparent;color:#fff;padding:6px 16px;border-radius:4px;border:1px solid #3C4A5A;text-decoration:none;font-weight:600;">Edit</a>
    <a href="{checkout_url}" style="background:#B8976A;color:#fff;padding:6px 16px;border-radius:4px;text-decoration:none;font-weight:600;">Go live — £99 + £24.99/mo →</a>
  </span>
</div>
<div style="height:44px;"></div>"""

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

    body_open = re.search(r"<body[^>]*>", html, re.IGNORECASE)
    if body_open:
        insert_at = body_open.end()
        html = html[:insert_at] + watermark_bar + toast_html + tracking_script + html[insert_at:]

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
