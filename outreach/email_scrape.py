"""
Tier 1 deterministic email discovery — plain code, no AI call, no API cost.

For any prospect with a website, fetch the page with a normal HTTP request
(same requests + BeautifulSoup style as outreach/site_extract.py, the only
other place this codebase scrapes a prospect's own site) and check for a
mailto: link or a plain-text email address on the homepage, then a same-site
contact/about page if the homepage has nothing. Only prospects where this
finds nothing should ever reach the AI-driven multi-source search in
outreach/email_discovery.py.

Never raises — any error just means "nothing found at Tier 1", the same
never-guess, never-crash contract the rest of the discovery pipeline uses.
"""
import re
import logging
from urllib.parse import urljoin, unquote

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger("outreach.email_scrape")

USER_AGENT = (
    "Mozilla/5.0 (compatible; GroundworkBot/1.0; "
    "+https://groundworkbuild.com)"
)
REQUEST_TIMEOUT = 8
MAX_HTML_BYTES = 3 * 1024 * 1024

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")

# Third-party/tracking/asset domains and placeholder locals that show up in
# scraped HTML but are never a business's real contact address — filtered
# out before anything counts as "found". Not an AI guess-guard (that's
# looks_like_guess() in email_discovery.py, which only applies to AI-found
# results) — this is just noise rejection on a regex match.
_JUNK_DOMAINS = (
    "sentry.io", "wixpress.com", "godaddy.com", "cloudflare.com",
    "googleapis.com", "gstatic.com", "schema.org", "w3.org",
    "example.com", "example.org", "domain.com", "yourdomain.com",
    "gravatar.com", "fontawesome.com", "jsdelivr.net",
)
_JUNK_LOCAL_PREFIXES = (
    "noreply", "no-reply", "donotreply", "postmaster", "abuse",
    "you@", "your@", "name@", "email@",
)

CONTACT_LINK_HINTS = ("contact", "about", "get-in-touch", "enquir")


def _looks_junk(email):
    email_l = email.lower()
    if any(d in email_l for d in _JUNK_DOMAINS):
        return True
    local = email_l.split("@", 1)[0]
    if any(local == p.rstrip("@") or email_l.startswith(p) for p in _JUNK_LOCAL_PREFIXES):
        return True
    # Image/asset filename artefacts the regex can pick up, e.g. "logo@2x.png"
    if re.search(r"@\d+x\.(png|jpe?g|gif|svg|webp)$", email_l):
        return True
    return False


def _extract_email_from_html(html):
    """Return (email, source) for the first genuine-looking email found —
    a mailto: link (highest confidence) first, then a plain-text pattern
    match in the visible page text."""
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return None, None

    for a in soup.select('a[href^="mailto:"]'):
        addr = unquote(a["href"][len("mailto:"):].split("?")[0]).strip()
        if addr and not _looks_junk(addr):
            return addr, "own_website_mailto"

    text = soup.get_text(" ", strip=True)
    for match in EMAIL_RE.finditer(text):
        addr = match.group(0).strip().rstrip(".,;:")
        if not _looks_junk(addr):
            return addr, "own_website_text"

    return None, None


def _fetch(url):
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
    chunks, total = [], 0
    for chunk in resp.iter_content(chunk_size=65536):
        total += len(chunk)
        if total > MAX_HTML_BYTES:
            break
        chunks.append(chunk)
    return resp.url, b"".join(chunks).decode(resp.encoding or "utf-8", errors="ignore")


def _find_contact_link(html, base_url):
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return None
    for a in soup.find_all("a", href=True):
        href = a["href"]
        text = (a.get_text() or "").lower()
        hay = (href + " " + text).lower()
        if any(hint in hay for hint in CONTACT_LINK_HINTS):
            return urljoin(base_url, href)
    return None


def scrape_website_email(website):
    """Tier 1: plain-code check of a prospect's own website for a genuine
    published email — homepage first, then a same-site contact/about page
    if the homepage itself has nothing. Returns (email, source) or
    (None, None). Never raises, so a caller looping over a batch can't have
    one bad fetch take down the run."""
    if not website:
        return None, None

    try:
        final_url, html = _fetch(website)
    except Exception as e:
        logger.info("Tier 1 fetch failed for %s: %s", website, e)
        return None, None
    if not html:
        return None, None

    email, source = _extract_email_from_html(html)
    if email:
        logger.info("Tier 1 found email on homepage for %s: %s", website, email)
        return email, source

    contact_url = _find_contact_link(html, final_url)
    if contact_url and contact_url != final_url:
        try:
            _, contact_html = _fetch(contact_url)
        except Exception as e:
            logger.info("Tier 1 contact-page fetch failed for %s: %s", contact_url, e)
            return None, None
        if contact_html:
            email, source = _extract_email_from_html(contact_html)
            if email:
                logger.info("Tier 1 found email on contact page for %s: %s", contact_url, email)
                return email, "own_website_contact_page"

    return None, None
