import os
import io
import re
import json
import uuid
import base64
import shutil
import threading
from collections import Counter
from datetime import datetime, timedelta
from functools import wraps

import anthropic
import stripe
from PIL import Image, ImageDraw, ImageFilter
from flask import Flask, request, jsonify, send_from_directory, session, redirect, url_for, render_template_string
from flask_cors import CORS
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from markupsafe import escape
from werkzeug.security import generate_password_hash, check_password_hash

from build_prompt import build_prompt
from models import SessionLocal, Lead, Generation, Account, GenerationImage, init_db
from emails import send_verification_email, send_resend_email, send_password_reset_email, send_support_message_email

app = Flask(__name__, static_folder="frontend", static_url_path="")
CORS(app)
app.secret_key = os.environ.get("SECRET_KEY", "dev-insecure-secret-change-me")
serializer = URLSafeTimedSerializer(app.secret_key)

TOKEN_MAX_AGE = 24 * 3600  # 24h magic-link expiry
RESET_TOKEN_MAX_AGE = 3600  # 1h — shorter-lived since it grants a password change
IP_RATE_LIMIT_PER_HOUR = int(os.environ.get("IP_RATE_LIMIT_PER_HOUR", "5"))

# Stripe — all values come from environment variables set in Railway.
# STRIPE_SETUP_PRICE_ID   → the one-time £99 price  (price_...)
# STRIPE_MONTHLY_PRICE_ID → the £24.99/month price   (price_...)
# STRIPE_SECRET_KEY       → sk_live_... (or sk_test_... for testing)
# STRIPE_WEBHOOK_SECRET   → whsec_... from `stripe listen` or dashboard
# SITE_URL                → https://groundworkbuild.com (used for redirect URLs)
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
STRIPE_SETUP_PRICE_ID = os.environ.get("STRIPE_SETUP_PRICE_ID", "")
STRIPE_MONTHLY_PRICE_ID = os.environ.get("STRIPE_MONTHLY_PRICE_ID", "")
SITE_URL = os.environ.get("SITE_URL", "https://groundworkbuild.com")
stripe.api_key = STRIPE_SECRET_KEY

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
    if image_placeholders:
        for token, data_uri in image_placeholders.items():
            html = html.replace(token, data_uri)
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
            'Logo/photo editing isn\'t available for this site — regenerate it to enable it.</p>'
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
                images = db.query(GenerationImage).filter(GenerationImage.generation_id == g.id).all()
                image_manager = _render_image_manager(g.id, images)
                card_parts.append(
                    '<div class="acct-card">'
                    '<div style="display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap;">'
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
                    + image_manager +
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
            subcopy = "View it any time, swap the logo or photos, or send us a message below if you'd like something changed."
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

        inner = f"""<div style="display:flex;justify-content:flex-end;margin-bottom:6px;">
          <a href="/account/logout" style="color:#807E79;font-size:13px;text-decoration:none;">Log out</a>
        </div>
        <div style="text-align:center;margin-bottom:28px;">
          <div style="color:#2257CC;font-size:12.5px;font-weight:700;letter-spacing:.12em;text-transform:uppercase;margin-bottom:10px;">Your account</div>
          <h1 style="margin:0 0 8px;font-weight:800;font-size:clamp(24px,3.4vw,32px);letter-spacing:-.02em;">{headline}</h1>
          <p style="margin:0;font-size:15.5px;color:#5C5A56;">{subcopy}</p>
        </div>
        {cards}
        {support_card}"""
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
    send_support_message_email(email, message)
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
                '<tr id="gen-row-' + str(g.id) + '" data-email="' + str(escape(g.email)) + '">'
                '<td>' + str(escape(g.business_name or "")) + test_badge + "</td><td>" + str(escape(g.email)) + "</td>"
                "<td>" + g.created_at.strftime("%d %b %Y %H:%M") + "</td><td>" + str(escape(g.status)) + "</td>"
                '<td><a href="/admin/generations/' + str(g.id) + '/html" target="_blank" rel="noopener">View HTML</a> · '
                '<a href="/admin/generations/' + str(g.id) + '/form-data" target="_blank" rel="noopener">Form data</a></td>'
                '<td><a href="#" title="Delete this ENTIRE account" '
                'onclick="return gwDeleteAccount(' + str(escape(json.dumps(g.email))) + ')" '
                'style="color:#9B2B1A;font-weight:800;text-decoration:none;">×</a></td></tr>'
            )
        rows = "".join(row_parts)
        return render_template_string(f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Admin — generations</title><link rel="icon" type="image/x-icon" href="/favicon.ico"><link rel="icon" type="image/png" sizes="192x192" href="/favicon-192.png"><style>{_PAGE_STYLE}</style></head>
<body><div class="wrap" style="max-width:1100px;">
<h1>All generations ({len(gens)})
<a href="/admin/generate-test" style="float:right;font-size:13px;margin-left:18px;">+ Generate test site</a>
<a href="/admin/logout" style="float:right;font-size:13px;">Log out</a></h1>
<p class="muted">× deletes the entire account for that email — login, every lead, and every generated site — as if they'd never signed up.</p>
<table><thead><tr><th>Business</th><th>Email</th><th>Created</th><th>Status</th><th>Links</th><th></th></tr></thead>
<tbody>{rows}</tbody></table></div>
<script>
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
</script>
</body></html>""")
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
    return """<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Admin — generate test site</title><link rel="icon" type="image/x-icon" href="/favicon.ico"><link rel="icon" type="image/png" sizes="192x192" href="/favicon-192.png">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=Plus+Jakarta+Sans:wght@700;800&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;}
h1,h2,h3,h4{font-family:'Plus Jakarta Sans','Inter',sans-serif;}
body{margin:0;background:#F5F3EE;font-family:Inter,sans-serif;color:#1C1C1C;}
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
</style></head>
<body>
<div style="max-width:680px;margin:0 auto;padding:clamp(28px,4vw,48px) 24px clamp(40px,6vw,72px);">
<h1 style="font-weight:800;font-size:26px;letter-spacing:-.02em;margin:0 0 6px;">Generate a test site</h1>
<p style="margin:0 0 24px;color:#5C5A56;font-size:14.5px;">Admin-only — skips email verification and the one-generation-per-email limit. Flagged TEST in the generations list. Same inputs as the live form, all on one page.</p>

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
</script>
</body></html>"""


@app.route("/admin/generate-test", methods=["GET", "POST"])
@admin_required
def admin_generate_test():
    if request.method == "GET":
        return render_template_string(_admin_test_form_page())

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
    generation is done, then goes straight to the unwatermarked HTML view
    (/admin/generations/<gen_id>/html), skipping the "your preview is ready"
    landing page real signups see before their Go-live decision. That page is
    intentional conversion scaffolding for real users; admin testing has no
    purchase decision to make, so it's pure friction here.
    """
    return render_template_string(f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Admin — generating…</title><link rel="icon" type="image/x-icon" href="/favicon.ico"><link rel="icon" type="image/png" sizes="192x192" href="/favicon-192.png"><style>{_PAGE_STYLE}</style></head>
<body><div class="wrap" style="max-width:640px;text-align:center;">
<h1>Generating…</h1>
<p class="muted" id="status-msg">Building the test site — this usually takes under 3 minutes.</p>
</div>
<script>
async function poll() {{
  try {{
    const r = await fetch('/admin/generate-test/status/{public_id}');
    const data = await r.json();
    if (data.status === 'done' && data.gen_id) {{
      window.location.href = `/admin/generations/${{data.gen_id}}/html`;
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
</script>
</body></html>""")


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
    show_toast = request.args.get("new") == "1"
    with _jobs_lock:
        job = _jobs.get(job_id)
    if job:
        if job["status"] != "done":
            return jsonify({"error": "not ready", "status": job["status"]}), 409
        return _inject_watermark(job["html"], job_id, show_toast=show_toast), 200, {"Content-Type": "text/html; charset=utf-8"}

    db = SessionLocal()
    try:
        gen = db.query(Generation).join(Lead).filter(Lead.public_id == job_id).first()
        if gen:
            return _inject_watermark(gen.html_content, job_id, show_toast=show_toast), 200, {"Content-Type": "text/html; charset=utf-8"}
    finally:
        db.close()
    return jsonify({"error": "not found"}), 404


@app.route("/api/checkout/session", methods=["POST"])
def create_checkout_session():
    if not STRIPE_SECRET_KEY:
        return jsonify({"error": "Stripe is not configured"}), 503
    data = request.get_json(silent=True) or {}
    job_id = str(data.get("job_id", "")).strip()
    if not job_id:
        return jsonify({"error": "missing job_id"}), 400

    db = SessionLocal()
    try:
        gen = db.query(Generation).join(Lead).filter(Lead.public_id == job_id).first()
        if not gen:
            return jsonify({"error": "not found"}), 404
        if gen.status == "live":
            return jsonify({"error": "already live"}), 409
    finally:
        db.close()

    cs = stripe.checkout.Session.create(
        mode="subscription",
        line_items=[{"price": STRIPE_MONTHLY_PRICE_ID, "quantity": 1}],
        subscription_data={"add_invoice_items": [{"price": STRIPE_SETUP_PRICE_ID, "quantity": 1}]},
        client_reference_id=job_id,
        success_url=f"{SITE_URL}/live.html?id={job_id}",
        cancel_url=f"{SITE_URL}/api/generate/{job_id}/html",
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

    if event["type"] == "checkout.session.completed":
        cs = event["data"]["object"]
        job_id = cs.get("client_reference_id")
        customer_id = cs.get("customer")
        if job_id:
            db = SessionLocal()
            try:
                gen = db.query(Generation).join(Lead).filter(Lead.public_id == job_id).first()
                if gen and gen.status != "live":
                    gen.status = "live"
                    if customer_id:
                        gen.stripe_customer_id = customer_id
                    db.commit()
            finally:
                db.close()

    return "", 200


def _inject_watermark(html: str, job_id: str, *, show_toast: bool = False) -> str:
    checkout_url = f"/checkout.html?id={job_id}"

    watermark_bar = f"""<div id="gw-preview-bar" style="position:fixed;top:0;left:0;right:0;z-index:99999;background:#1C2630;color:#fff;font-family:sans-serif;font-size:13px;display:flex;align-items:center;justify-content:space-between;padding:10px 20px;box-shadow:0 2px 8px rgba(0,0,0,0.3);">
  <span>⚠ Preview — this site is unpublished and watermarked</span>
  <a href="{checkout_url}" style="background:#B8976A;color:#fff;padding:6px 16px;border-radius:4px;text-decoration:none;font-weight:600;">Go live — £99 + £24.99/mo →</a>
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

    robots_meta = '<meta name="robots" content="noindex, nofollow">'

    body_open = re.search(r"<body[^>]*>", html, re.IGNORECASE)
    if body_open:
        insert_at = body_open.end()
        html = html[:insert_at] + watermark_bar + toast_html + html[insert_at:]

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
