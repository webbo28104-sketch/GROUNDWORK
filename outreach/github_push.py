"""
Push a file to this repo via the GitHub REST Contents API — a plain HTTPS
PUT with a token, not a local `git commit`/`push`. Same mechanism
outreach/export_and_push_pending_batch.py already uses, factored out here
so outreach/variant_optimizer_job.py can reuse it without duplicating the
GET-sha/PUT dance. Works from anywhere with network access (a Railway Cron
container has no git remote configured, but this doesn't need one).

Environment: GITHUB_PUSH_TOKEN (a fine-grained GitHub PAT, scoped to
Contents: Read and write on webbo28104-sketch/GROUNDWORK only).
"""
import os
import base64
import logging

import requests

logger = logging.getLogger("outreach.github_push")

GITHUB_REPO = "webbo28104-sketch/GROUNDWORK"
GITHUB_API_BASE = "https://api.github.com"
REQUEST_TIMEOUT = 30


def push_file_to_github(file_path, content_bytes, commit_message, branch="main"):
    """Returns True on success, False on any failure (missing token, HTTP
    error) — never raises, so a push failure degrades to "try again next
    run" rather than crashing the calling job."""
    token = os.environ.get("GITHUB_PUSH_TOKEN")
    if not token:
        logger.error("GITHUB_PUSH_TOKEN not set — cannot push %s", file_path)
        return False

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    url = f"{GITHUB_API_BASE}/repos/{GITHUB_REPO}/contents/{file_path}"

    try:
        get_resp = requests.get(url, headers=headers, params={"ref": branch}, timeout=REQUEST_TIMEOUT)
        sha = get_resp.json().get("sha") if get_resp.status_code == 200 else None

        body = {
            "message": commit_message,
            "content": base64.b64encode(content_bytes).decode("ascii"),
            "branch": branch,
        }
        if sha:
            body["sha"] = sha

        put_resp = requests.put(url, headers=headers, json=body, timeout=REQUEST_TIMEOUT)
        put_resp.raise_for_status()
        logger.info("Pushed %s to GitHub (commit %s)", file_path, put_resp.json().get("commit", {}).get("sha", "?")[:8])
        return True
    except Exception:
        logger.exception("Failed to push %s to GitHub", file_path)
        return False
