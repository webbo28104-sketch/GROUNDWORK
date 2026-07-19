"""
Export the pending email-discovery batch AND push it to the repo, in one
step — replaces .github/workflows/export-pending-batch.yml's scheduled
trigger, which turned out to be unreliable (GitHub deprioritizes scheduled
Actions on low-traffic repos; observed running 2.5-3 hours late two nights
running, 2026-07-18 and 2026-07-19, which made the discovery routine read
a stale pending_batch.json since the fresh export hadn't landed by the
time it ran). Runs on Railway Cron instead (added 2026-07-19) — same
dedicated, reliably-on-time infrastructure every other job in this
pipeline already uses.

Pushes via the GitHub REST Contents API (not a local `git commit`/`push`)
so it doesn't depend on Railway's deploy container having a working git
remote — just a plain HTTPS PUT with a token, same dependency footprint
(requests) as everything else in this file.

Environment: DATABASE_URL, GITHUB_PUSH_TOKEN (a fine-grained GitHub PAT,
scoped to Contents: Read and write on webbo28104-sketch/GROUNDWORK only).

Runnable standalone:
    python outreach/export_and_push_pending_batch.py
"""
import os
import sys
import base64
import logging
from datetime import datetime

import requests

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_THIS_DIR)
for _p in (_PROJECT_ROOT, _THIS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_PROJECT_ROOT, ".env"))
except Exception:
    pass

try:
    from outreach.export_pending_batch import export_batch, OUTPUT_PATH
except ImportError:
    from export_pending_batch import export_batch, OUTPUT_PATH

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("outreach.export_and_push_pending_batch")

GITHUB_REPO = "webbo28104-sketch/GROUNDWORK"
GITHUB_FILE_PATH = "outreach/discovery_batches/pending_batch.json"
GITHUB_API_BASE = "https://api.github.com"
REQUEST_TIMEOUT = 30


def _push_via_github_api(token):
    with open(OUTPUT_PATH, "rb") as f:
        content_bytes = f.read()

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    url = f"{GITHUB_API_BASE}/repos/{GITHUB_REPO}/contents/{GITHUB_FILE_PATH}"

    get_resp = requests.get(url, headers=headers, params={"ref": "main"}, timeout=REQUEST_TIMEOUT)
    sha = get_resp.json().get("sha") if get_resp.status_code == 200 else None

    body = {
        "message": f"Nightly pending batch export ({datetime.utcnow().strftime('%Y-%m-%d')}) [Railway Cron]",
        "content": base64.b64encode(content_bytes).decode("ascii"),
        "branch": "main",
    }
    if sha:
        body["sha"] = sha

    put_resp = requests.put(url, headers=headers, json=body, timeout=REQUEST_TIMEOUT)
    put_resp.raise_for_status()
    logger.info("Pushed %s to GitHub (commit %s)", GITHUB_FILE_PATH, put_resp.json().get("commit", {}).get("sha", "?")[:8])


def main():
    token = os.environ.get("GITHUB_PUSH_TOKEN")
    if not token:
        logger.error("GITHUB_PUSH_TOKEN not set — cannot push, exiting")
        sys.exit(1)

    batch = export_batch(limit=2000)
    logger.info("Exported %d pending prospect(s)", len(batch))
    _push_via_github_api(token)


if __name__ == "__main__":
    main()
