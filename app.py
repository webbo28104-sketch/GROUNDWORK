import os
import io
import re
import uuid
import base64
import threading
from datetime import datetime, timedelta
from functools import wraps

import anthropic
from PIL import Image, ImageDraw, ImageFilter
from flask import Flask, request, jsonify, send_from_directory, session, redirect, url_for, render_template_string
from flask_cors import CORS
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from markupsafe import escape
from werkzeug.security import generate_password_hash, check_password_hash

from build_prompt import build_prompt
from models import SessionLocal, Lead, Generation, Account, init_db
from emails import send_verification_email, send_resend_email, send_password_reset_email

app = Flask(__name__, static_folder="frontend", static_url_path="")
CORS(app)
app.secret_key = os.environ.get("SECRET_KEY", "dev-insecure-secret-change-me")
serializer = URLSafeTimedSerializer(app.secret_key)

TOKEN_MAX_AGE = 24 * 3600  # 24h magic-link expiry
RESET_TOKEN_MAX_AGE = 3600  # 1h — shorter-lived since it grants a password change
IP_RATE_LIMIT_PER_HOUR = int(os.environ.get("IP_RATE_LIMIT_PER_HOUR", "5"))

init_db()

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


def _average_colour(samples):
    n = len(samples)
    return tuple(sum(s[c] for s in samples) // n for c in range(3))


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
      filled with the dominant border colour, so a deliberately different
      background reads as an intentional badge rather than a mismatched
      rectangle.
    - Any failure, or an image too small/ambiguous to trust, falls back to
      today's plain behaviour (resize + encode as-is) rather than risking a
      mangled result.

    Returns (mode, PIL.Image) where mode is "as_is", "transparent", or "chip",
    purely for the caller/tests to report which path was taken.
    """
    try:
        with Image.open(path) as raw:
            img = raw.convert("RGBA") if raw.mode in ("RGBA", "LA", "P") else raw.convert("RGB")

        w, h = img.size
        if min(w, h) < _MIN_LOGO_DIMENSION:
            return "as_is", img

        already_transparent = img.mode == "RGBA" and img.getchannel("A").getextrema()[0] < 255
        if already_transparent:
            return "as_is", img

        img_rgb = img.convert("RGB")
        samples = _sample_border_points(img_rgb)
        spread = _channelwise_spread(samples)
        bg_colour = _average_colour(samples)

        if spread > _BG_UNIFORM_TOLERANCE:
            # Busy/gradient background — badge it instead of cutting it out.
            return "chip", _make_logo_chip(img, bg_colour, max_dimension)

        # Uniform background — flood-fill it away from all four corners.
        marker = (1, 2, 3)
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
            return "as_is", img

        alpha = alpha.filter(ImageFilter.GaussianBlur(radius=1.0))
        out = img.convert("RGBA")
        out.putalpha(alpha)
        return "transparent", out

    except Exception:
        with Image.open(path) as raw:
            return "as_is", raw.convert("RGBA") if raw.mode in ("RGBA", "LA", "P") else raw.convert("RGB")


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
    encoding, then encodes the result. Returns (data_uri, mode) where mode is
    "as_is" / "transparent" / "chip" — useful for logging/testing which path
    was taken; callers that don't care can just use the data_uri.
    """
    mode, img = _process_logo(path, max_dimension)
    return _encode_pil_image_to_data_uri(img, max_dimension, jpeg_quality=90), mode


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
            data_uri, _mode = _logo_file_to_data_uri(logo_file, max_dimension=480)
            image_placeholders[token] = data_uri
            build_overrides["logo_src_token"] = token

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
        content.append({"type": "text", "text": prompt})

        messages = [{"role": "user", "content": content}]
        accumulated_text = ""

        for _ in range(15):
            resp = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=16000,
                tools=[{"type": "web_search_20250305", "name": "web_search"}],
                messages=messages,
            )

            # Collect any text from this turn
            for block in resp.content:
                if hasattr(block, "text"):
                    accumulated_text += block.text

            if resp.stop_reason == "end_turn":
                break

            # Continue conversation for tool_use turns
            messages.append({"role": "assistant", "content": resp.content})
            tool_results = [
                {"type": "tool_result", "tool_use_id": b.id, "content": ""}
                for b in resp.content
                if getattr(b, "type", "") == "tool_use"
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


def _run_and_persist(job_id, lead_id, email, business_name, prompt, logo_b64, logo_mime, image_placeholders=None):
    _run(job_id, prompt, logo_b64, logo_mime)
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job or job.get("status") != "done":
        return

    html = job["html"]
    if image_placeholders:
        for token, data_uri in image_placeholders.items():
            html = html.replace(token, data_uri)
        with _jobs_lock:
            _jobs[job_id]["html"] = html

    db = SessionLocal()
    try:
        db.add(Generation(
            lead_id=lead_id,
            email=email,
            business_name=business_name,
            html_content=html,
            status="draft",
        ))
        db.commit()
    finally:
        db.close()


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
            return redirect(f"/preview.html?id={lead.public_id}")

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

# Shared nav/footer markup so /account and other Flask-rendered pages match the
# static frontend pages' look, since there's no shared CSS file in this repo —
# every page (including frontend/index.html) inlines its own styles.
_SITE_HEADER = """<header style="position:sticky;top:0;z-index:100;background:#1C1C1C;border-bottom:1px solid #2C2C2C;">
  <nav style="max-width:1200px;margin:0 auto;padding:0 24px;height:68px;display:flex;align-items:center;justify-content:space-between;gap:24px;">
    <a href="/index.html" style="display:flex;align-items:center;gap:11px;text-decoration:none;">
      <svg viewBox="0 0 48 48" width="32" height="32" fill="none"><path d="M 37 13.1 A 17 17 0 1 0 41 24 L 27 24" stroke="#3B82F6" stroke-width="4.6" stroke-linecap="round" stroke-linejoin="round"/><path d="M 30.9 18.2 A 9 9 0 1 0 30.9 29.8" stroke="#3B82F6" stroke-width="4.6" stroke-linecap="round"/></svg>
      <span style="color:#fff;font-weight:800;font-size:20px;letter-spacing:-.03em;">Groundwork</span>
    </a>
    <a href="/build.html" style="background:#3B82F6;color:#fff;font-weight:700;font-size:15px;text-decoration:none;padding:10px 18px;border-radius:7px;">Get started</a>
  </nav>
</header>"""

_SITE_FOOTER = """<footer style="background:#1C1C1C;color:#9A9893;margin-top:56px;">
  <div style="max-width:1200px;margin:0 auto;padding:28px 24px;font-size:13px;color:#5E5C58;">© 2026 Groundwork Ltd. Made for people who build things.</div>
</footer>"""


def _account_page(inner_html: str, title: str) -> str:
    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} — Groundwork</title>
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
{_SITE_HEADER}
<div class="acct-wrap">{inner_html}</div>
{_SITE_FOOTER}
<script>
function gwTogglePw(id, btn){{
  const el = document.getElementById(id);
  const showing = el.type === 'text';
  el.type = showing ? 'password' : 'text';
  btn.textContent = showing ? 'Show' : 'Hide';
}}
</script>
</body></html>"""


def _render_dashboard(email: str) -> str:
    db = SessionLocal()
    try:
        gens = db.query(Generation).filter(Generation.email == email).order_by(Generation.created_at.desc()).all()

        if gens:
            card_parts = []
            for g in gens:
                business_label = g.business_name or "Untitled site"
                status_label = "Live" if g.status == "live" else "Draft — not yet published"
                go_live_link = ""
                if g.status != "live":
                    go_live_link = (
                        '<a href="/checkout.html?id=' + g.lead.public_id + '" '
                        'style="display:inline-block;background:#3B82F6;color:#fff;font-weight:700;font-size:14.5px;'
                        'text-decoration:none;padding:11px 18px;border-radius:9px;">Go live →</a>'
                    )
                card_parts.append(
                    '<div class="acct-card" style="display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap;">'
                    '<div>'
                    '<div style="font-weight:700;font-size:17px;">' + business_label + '</div>'
                    '<div style="font-size:13.5px;color:#807E79;margin-top:3px;">Generated '
                    + g.created_at.strftime("%d %b %Y") + " · " + status_label + '</div>'
                    '</div>'
                    '<div style="display:flex;gap:10px;flex-wrap:wrap;">'
                    '<a href="/api/generate/' + g.lead.public_id + '/html" target="_blank" rel="noopener" '
                    'style="display:inline-block;background:#fff;color:#1C1C1C;font-weight:700;font-size:14.5px;'
                    'text-decoration:none;border:1px solid #D9D7D0;padding:11px 18px;border-radius:9px;">View site →</a>'
                    + go_live_link +
                    '</div>'
                    '</div>'
                )
            cards = "".join(card_parts)
        else:
            cards = '<div class="acct-card"><p style="margin:0;color:#5C5A56;font-size:15px;">No sites found for this account yet.</p></div>'

        inner = f"""<div style="display:flex;justify-content:flex-end;margin-bottom:6px;">
          <a href="/account/logout" style="color:#807E79;font-size:13px;text-decoration:none;">Log out</a>
        </div>
        <div style="text-align:center;margin-bottom:28px;">
          <div style="color:#2257CC;font-size:12.5px;font-weight:700;letter-spacing:.12em;text-transform:uppercase;margin-bottom:10px;">Your account</div>
          <h1 style="margin:0 0 8px;font-weight:800;font-size:clamp(24px,3.4vw,32px);letter-spacing:-.02em;">Your sites, all in one place</h1>
          <p style="margin:0;font-size:15.5px;color:#5C5A56;">Every website you've generated with {escape(email)}, ready whenever you need it.</p>
        </div>
        {cards}"""
        return render_template_string(_account_page(inner, "Your account"))
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

        if _has_generation(db, email):
            # Real email (they already verified it once to generate a site) —
            # no need to re-verify, just let them set a password directly.
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


@app.route("/api/account/session")
def api_account_session():
    email = session.get("account_email")
    return jsonify({"logged_in": bool(email), "email": email})


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
        u = request.form.get("username", "")
        p = request.form.get("password", "")
        admin_user = os.environ.get("ADMIN_USERNAME")
        admin_pass = os.environ.get("ADMIN_PASSWORD")
        if admin_user and admin_pass and u == admin_user and p == admin_pass:
            session["is_admin"] = True
            return redirect(request.args.get("next") or url_for("admin_generations"))
        error = "Invalid credentials."
    error_html = f'<p class="err">{error}</p>' if error else ""
    return render_template_string(f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Admin login</title><style>{_PAGE_STYLE}</style></head>
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


@app.route("/admin/generations")
@admin_required
def admin_generations():
    db = SessionLocal()
    try:
        gens = db.query(Generation).order_by(Generation.created_at.desc()).all()
        row_parts = []
        for g in gens:
            test_badge = '<span class="badge-test">TEST</span>' if (g.lead and g.lead.is_test) else ""
            row_parts.append(
                "<tr><td>" + (g.business_name or "") + test_badge + "</td><td>" + g.email + "</td>"
                "<td>" + g.created_at.strftime("%d %b %Y %H:%M") + "</td><td>" + g.status + "</td>"
                '<td><a href="/admin/generations/' + str(g.id) + '/html" target="_blank" rel="noopener">View HTML</a> · '
                '<a href="/admin/generations/' + str(g.id) + '/form-data" target="_blank" rel="noopener">Form data</a></td></tr>'
            )
        rows = "".join(row_parts)
        return render_template_string(f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Admin — generations</title><style>{_PAGE_STYLE}</style></head>
<body><div class="wrap" style="max-width:1100px;">
<h1>All generations ({len(gens)})
<a href="/admin/generate-test" style="float:right;font-size:13px;margin-left:18px;">+ Generate test site</a>
<a href="/admin/logout" style="float:right;font-size:13px;">Log out</a></h1>
<table><thead><tr><th>Business</th><th>Email</th><th>Created</th><th>Status</th><th>Links</th></tr></thead>
<tbody>{rows}</tbody></table></div></body></html>""")
    finally:
        db.close()


_ADMIN_TEST_FORM_FIELDS = [
    ("email", "Email", "email", True),
    ("business_name", "Business name", "text", True),
    ("trade", "Trade", "text", True),
    ("location", "Location", "text", True),
    ("coverage_area", "Coverage area", "text", False),
    ("phone", "Phone", "text", False),
    ("commercial_split", "Commercial split (0-100)", "number", False),
    ("work_type", "Work type (standard/mix/bespoke)", "text", False),
    ("team_size", "Team size (sole/small/company)", "text", False),
    ("large_contracts", "Large contracts (yes/no)", "text", False),
    ("urgency", "Urgency (ahead/emergency)", "text", False),
    ("years_trading", "Years trading", "text", False),
    ("accreditations", "Accreditations", "text", False),
    ("past_clients", "Past clients / projects", "text", False),
    ("notes", "Notes", "text", False),
]


@app.route("/admin/generate-test", methods=["GET", "POST"])
@admin_required
def admin_generate_test():
    if request.method == "GET":
        field_rows = "".join(
            f'<label style="display:block;font-size:13.5px;font-weight:600;margin-bottom:5px;">{label}{" *" if required else ""}</label>'
            f'<input type="{itype}" name="{name}" {"required" if required else ""} style="width:100%;padding:10px 12px;border:1px solid #D8D5CE;border-radius:8px;font-size:14.5px;margin-bottom:14px;">'
            for name, label, itype, required in _ADMIN_TEST_FORM_FIELDS
        )
        return render_template_string(f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Admin — generate test site</title><style>{_PAGE_STYLE}</style></head>
<body><div class="wrap" style="max-width:640px;">
<h1>Generate a test site</h1>
<p class="muted">Admin-only: skips email verification and the one-generation-per-email limit. Flagged as TEST in the generations list.</p>
<form method="post" enctype="multipart/form-data">
{field_rows}
<label style="display:block;font-size:13.5px;font-weight:600;margin-bottom:5px;">Logo</label>
<input type="file" name="logo" accept="image/*" style="margin-bottom:14px;">
<label style="display:block;font-size:13.5px;font-weight:600;margin-bottom:5px;">Photos</label>
<input type="file" name="photos" accept="image/*" multiple style="margin-bottom:18px;">
<button type="submit">Generate test site</button>
</form>
</div></body></html>""")

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

        return redirect(f"/loading.html?id={lead.public_id}")
    finally:
        db.close()


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


@app.route("/api/generate/<job_id>/html")
def job_html(job_id):
    with _jobs_lock:
        job = _jobs.get(job_id)
    if job:
        if job["status"] != "done":
            return jsonify({"error": "not ready", "status": job["status"]}), 409
        return _inject_watermark(job["html"], job_id), 200, {"Content-Type": "text/html; charset=utf-8"}

    db = SessionLocal()
    try:
        gen = db.query(Generation).join(Lead).filter(Lead.public_id == job_id).first()
        if gen:
            return _inject_watermark(gen.html_content, job_id), 200, {"Content-Type": "text/html; charset=utf-8"}
    finally:
        db.close()
    return jsonify({"error": "not found"}), 404


def _inject_watermark(html: str, job_id: str) -> str:
    checkout_url = f"/checkout.html?id={job_id}"

    watermark_bar = f"""<div id="gw-preview-bar" style="position:fixed;top:0;left:0;right:0;z-index:99999;background:#1C2630;color:#fff;font-family:sans-serif;font-size:13px;display:flex;align-items:center;justify-content:space-between;padding:10px 20px;box-shadow:0 2px 8px rgba(0,0,0,0.3);">
  <span>⚠ Preview — this site is unpublished and watermarked</span>
  <a href="{checkout_url}" style="background:#B8976A;color:#fff;padding:6px 16px;border-radius:4px;text-decoration:none;font-weight:600;">Go live — £99 + £24.99/mo →</a>
</div>
<div style="height:44px;"></div>"""

    robots_meta = '<meta name="robots" content="noindex, nofollow">'

    body_open = re.search(r"<body[^>]*>", html, re.IGNORECASE)
    if body_open:
        insert_at = body_open.end()
        html = html[:insert_at] + watermark_bar + html[insert_at:]

    head_open = re.search(r"<head[^>]*>", html, re.IGNORECASE)
    if head_open:
        insert_at = head_open.end()
        html = html[:insert_at] + robots_meta + html[insert_at:]

    return html


@app.route("/api/generate/<job_id>/photos/<filename>")
def job_photo(job_id, filename):
    job_dir = os.path.join(UPLOAD_DIR, job_id)
    return send_from_directory(job_dir, filename)


# Serve frontend static files (fallback for local dev)
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
