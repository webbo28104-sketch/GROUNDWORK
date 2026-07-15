"""
Best-effort logo + portfolio-photo extraction from a prospect's existing
website, run inside the click-triggered generation flow (see
_claim_generate_and_redirect / _kickoff_generation in app.py) for
has_website_dated / has_website_modern prospects.

Everything here is deliberately conservative: any failure, timeout, or
low-quality result returns None / [] rather than raising, so a bad
extraction never produces a worse site than the current default/generated
look. Callers must treat every return value as "may be empty."
"""

import io
import re
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from PIL import Image

USER_AGENT = (
    "Mozilla/5.0 (compatible; GroundworkBot/1.0; "
    "+https://groundworkbuild.com)"
)
REQUEST_TIMEOUT = 8
MAX_DOWNLOAD_BYTES = 8 * 1024 * 1024  # 8MB cap per image fetch
MAX_PHOTOS = 6

LOGO_MIN_DIMENSION = 24        # below this, almost certainly a favicon/icon-only fetch
LOGO_MAX_ASPECT_RATIO = 12.0   # reject absurdly thin strips (nav dividers, spacers)
FAVICON_MIN_DIMENSION = 48  # a 16-32px favicon reads as a blurry icon once
# scaled up to header size — better to fall back to no logo than ship that

PHOTO_MIN_WIDTH = 300
PHOTO_MIN_HEIGHT = 200
PHOTO_MIN_AREA = PHOTO_MIN_WIDTH * PHOTO_MIN_HEIGHT
# Accreditation badges, review-site graphics ("Checkatrade", NFRC/CHAS/FMB
# logos, etc.) are flat vector-style compositions: one dominant background
# colour covers a large share of the image. Real jobsite photos don't —
# even a flat overcast sky has enough gradient/noise to stay well below
# this. Measured against real extractions: badges landed at 0.48-0.68,
# genuine photos (including a hazy grey-sky one) topped out at 0.31.
PHOTO_MAX_DOMINANT_COLOR_FRACTION = 0.40

_ICON_HINTS = (
    "icon", "sprite", "badge", "social", "avatar", "arrow", "star",
    "rating", "flag", "payment", "visa", "mastercard", "paypal",
    "facebook", "twitter", "instagram", "linkedin", "youtube-icon",
    "loading", "spinner", "pixel", "tracking", "beacon", "1x1",
)
_STOCK_WATERMARK_HINTS = (
    "shutterstock", "istockphoto", "gettyimages", "123rf", "dreamstime",
    "watermark", "alamy", "depositphotos", "adobestock",
)
_GALLERY_CLASS_HINTS = ("gallery", "portfolio", "project", "work")


class ExtractedImage:
    __slots__ = ("bytes", "ext", "mime", "width", "height", "source_url")

    def __init__(self, data, ext, mime, width, height, source_url):
        self.bytes = data
        self.ext = ext
        self.mime = mime
        self.width = width
        self.height = height
        self.source_url = source_url


def extract_site_assets(url, max_photos=MAX_PHOTOS):
    """
    Best-effort. Returns {"logo": ExtractedImage|None, "photos": [ExtractedImage,...]}.
    Never raises — any error along the way just yields fewer/no results.
    """
    result = {"logo": None, "photos": []}
    try:
        final_url, html = _fetch_html(url)
    except Exception:
        return result
    if not html:
        return result

    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return result

    try:
        result["logo"] = _extract_logo(soup, final_url)
    except Exception:
        result["logo"] = None

    try:
        result["photos"] = _extract_photos(soup, final_url, max_photos)
    except Exception:
        result["photos"] = []

    return result


def _fetch_html(url):
    if not url or not re.match(r"^https?://", url, re.I):
        url = "https://" + (url or "").lstrip("/")
    resp = requests.get(
        url,
        headers={"User-Agent": USER_AGENT},
        timeout=REQUEST_TIMEOUT,
        allow_redirects=True,
        stream=True,
    )
    resp.raise_for_status()
    ctype = resp.headers.get("Content-Type", "")
    if "text/html" not in ctype and ctype:
        return resp.url, None
    chunks = []
    total = 0
    for chunk in resp.iter_content(chunk_size=65536):
        total += len(chunk)
        if total > 3 * 1024 * 1024:  # 3MB of HTML is plenty
            break
        chunks.append(chunk)
    return resp.url, b"".join(chunks).decode(resp.encoding or "utf-8", errors="ignore")


def _download_image(src_url, referer):
    try:
        resp = requests.get(
            src_url,
            headers={"User-Agent": USER_AGENT, "Referer": referer},
            timeout=REQUEST_TIMEOUT,
            stream=True,
        )
        resp.raise_for_status()
        ctype = resp.headers.get("Content-Type", "")
        if ctype and "image" not in ctype and "octet-stream" not in ctype:
            return None
        chunks = []
        total = 0
        for chunk in resp.iter_content(chunk_size=65536):
            total += len(chunk)
            if total > MAX_DOWNLOAD_BYTES:
                return None
            chunks.append(chunk)
        data = b"".join(chunks)
        if not data:
            return None
        img = Image.open(io.BytesIO(data))
        img.verify()
        # verify() invalidates the image object for further use — reopen
        img = Image.open(io.BytesIO(data))
        width, height = img.size
        fmt = (img.format or "").lower()
        if fmt not in ("png", "jpeg", "jpg", "gif", "webp", "bmp"):
            return None
        ext = ".png" if fmt == "png" else (".jpg" if fmt in ("jpeg", "jpg") else f".{fmt}")
        mime = f"image/{'jpeg' if fmt == 'jpg' else fmt}"
        return ExtractedImage(data, ext, mime, width, height, src_url)
    except Exception:
        return None


def _looks_like_icon_or_junk(candidate_str):
    s = (candidate_str or "").lower()
    return any(hint in s for hint in _ICON_HINTS)


def _looks_like_stock_watermark(candidate_str):
    s = (candidate_str or "").lower()
    return any(hint in s for hint in _STOCK_WATERMARK_HINTS)


def _img_candidates(soup, base_url):
    """All <img> tags with resolved absolute src + surrounding metadata."""
    out = []
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or img.get("data-lazy-src")
        if not src or src.startswith("data:"):
            continue
        abs_url = urljoin(base_url, src)
        out.append({
            "tag": img,
            "url": abs_url,
            "class": " ".join(img.get("class", [])) if img.get("class") else "",
            "id": img.get("id", "") or "",
            "alt": img.get("alt", "") or "",
        })
    return out


def _extract_logo(soup, base_url):
    header = soup.find("header") or soup.find(class_=re.compile(r"header|navbar|nav-bar", re.I))
    nav = soup.find("nav")
    search_scopes = [scope for scope in (header, nav) if scope is not None]
    if not search_scopes:
        search_scopes = [soup]

    candidates = []
    for scope in search_scopes:
        for meta in _img_candidates(scope, base_url):
            haystack = f"{meta['class']} {meta['id']} {meta['alt']} {meta['url']}"
            score = 0
            if "logo" in haystack.lower():
                score += 10
            if scope is header:
                score += 3
            if scope is nav:
                score += 2
            candidates.append((score, meta))

    candidates.sort(key=lambda c: c[0], reverse=True)

    for score, meta in candidates:
        if score <= 0:
            continue
        if _looks_like_icon_or_junk(meta["url"]) and "logo" not in meta["url"].lower():
            continue
        img = _download_image(meta["url"], base_url)
        if img and _is_acceptable_logo(img):
            return img

    # No confident <img> match — try any header/nav image at all, even
    # without "logo" in its metadata, before falling back to favicon.
    for scope in search_scopes:
        for meta in _img_candidates(scope, base_url):
            if _looks_like_icon_or_junk(meta["url"] + meta["class"] + meta["id"]):
                continue
            img = _download_image(meta["url"], base_url)
            if img and _is_acceptable_logo(img):
                return img

    # Favicon fallback
    favicon_url = _find_favicon(soup, base_url)
    if favicon_url:
        img = _download_image(favicon_url, base_url)
        if img and _is_acceptable_logo(img, is_favicon=True):
            return img

    return None


def _find_favicon(soup, base_url):
    for rel_pattern in ("icon", "shortcut icon", "apple-touch-icon"):
        link = soup.find("link", rel=lambda v: v and rel_pattern in v.lower())
        if link and link.get("href"):
            return urljoin(base_url, link["href"])
    return urljoin(base_url, "/favicon.ico")


def _is_acceptable_logo(img, is_favicon=False):
    min_dim = FAVICON_MIN_DIMENSION if is_favicon else LOGO_MIN_DIMENSION
    if img.width < min_dim or img.height < min_dim:
        return False
    if img.width <= 2 or img.height <= 2:
        return False  # tracking pixel
    aspect = max(img.width, img.height) / max(1, min(img.width, img.height))
    if aspect > LOGO_MAX_ASPECT_RATIO:
        return False
    if _looks_like_stock_watermark(img.source_url):
        return False
    return True


def _extract_photos(soup, base_url, max_photos):
    gallery_imgs = []
    for el in soup.find_all(True):
        cls_id = " ".join(el.get("class", []) or []) + " " + (el.get("id", "") or "")
        if any(hint in cls_id.lower() for hint in _GALLERY_CLASS_HINTS):
            gallery_imgs.extend(_img_candidates(el, base_url))

    pool = gallery_imgs if gallery_imgs else _img_candidates(soup, base_url)

    seen_urls = set()
    scored = []
    for meta in pool:
        url = meta["url"]
        if url in seen_urls:
            continue
        seen_urls.add(url)
        haystack = f"{meta['class']} {meta['id']} {meta['alt']} {meta['url']}"
        if _looks_like_icon_or_junk(haystack) or "logo" in haystack.lower():
            continue
        if _looks_like_stock_watermark(haystack):
            continue
        scored.append(meta)

    results = []
    seen_hashes = set()
    for meta in scored:
        if len(results) >= max_photos:
            break
        img = _download_image(meta["url"], base_url)
        if not img or not _is_acceptable_photo(img):
            continue
        h = hash(img.bytes)
        if h in seen_hashes:
            continue
        seen_hashes.add(h)
        results.append(img)

    results.sort(key=lambda i: i.width * i.height, reverse=True)
    return results[:max_photos]


def _is_acceptable_photo(img):
    if img.width < PHOTO_MIN_WIDTH or img.height < PHOTO_MIN_HEIGHT:
        return False
    if (img.width * img.height) < PHOTO_MIN_AREA:
        return False
    if _looks_like_stock_watermark(img.source_url):
        return False
    aspect = max(img.width, img.height) / max(1, min(img.width, img.height))
    if aspect > 6.0:  # banners/dividers masquerading as content images
        return False
    if _dominant_color_fraction(img.bytes) > PHOTO_MAX_DOMINANT_COLOR_FRACTION:
        return False  # likely a badge/graphic, not a photo
    return True


def _dominant_color_fraction(data):
    try:
        thumb = Image.open(io.BytesIO(data)).convert("RGB").resize((64, 64))
        colors = thumb.getcolors(maxcolors=64 * 64)
        if not colors:
            return 0.0
        return max(c[0] for c in colors) / (64 * 64)
    except Exception:
        return 0.0
