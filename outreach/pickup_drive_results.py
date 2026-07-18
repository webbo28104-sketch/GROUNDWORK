"""
Fully unattended morning pickup for the nightly email-discovery routine's
results — added 2026-07-19 to close the loop the user asked for: "want
this to run even if I don't touch Groundwork for a week."

The routine (a scheduled Claude Code cloud agent, not code in this repo —
see docs/outreach-pipeline-spec.md Section 4a) can't reach
groundworkbuild.com or git-push from its sandbox (both confirmed platform
restrictions), so its last step is writing a JSON results file to a Google
Drive folder via its Google_Drive MCP connection instead. This script,
run on a schedule by .github/workflows/pickup-discovery-results.yml,
reads that folder with a plain Google Drive API key (read-only, no OAuth/
service-account needed — the folder is shared as "anyone with the link,
viewer") and imports whatever's new through the same validated path
outreach/import_discovery_results.py already uses.

Idempotent: tracks the last-imported Drive file id in
DiscoveryImportState (single row) so a job re-run (or a day the routine
didn't produce a new file) never double-applies results.

Environment: DATABASE_URL, GOOGLE_DRIVE_API_KEY, GOOGLE_DRIVE_FOLDER_ID.

Runnable standalone:
    python outreach/pickup_drive_results.py
    python outreach/pickup_drive_results.py --dry-run
"""
import os
import sys
import json
import logging
import argparse

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

from models import SessionLocal, DiscoveryImportState, init_db  # noqa: E402

try:
    from outreach.import_discovery_results import import_results_from_data
except ImportError:
    from import_discovery_results import import_results_from_data

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("outreach.pickup_drive_results")

DRIVE_API_BASE = "https://www.googleapis.com/drive/v3"
RESULTS_FILENAME = "groundwork-discovery-results.json"
REQUEST_TIMEOUT = 30


def _find_latest_file(api_key, folder_id):
    """Return the newest matching file's (id, name, createdTime), or None
    if the folder has no matching file yet (e.g. tonight's run hasn't
    finished, or hasn't run at all yet)."""
    query = f"'{folder_id}' in parents and name = '{RESULTS_FILENAME}' and trashed = false"
    resp = requests.get(
        f"{DRIVE_API_BASE}/files",
        params={
            "q": query,
            "key": api_key,
            "fields": "files(id,name,createdTime)",
            "orderBy": "createdTime desc",
            "pageSize": 1,
        },
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    files = resp.json().get("files", [])
    return files[0] if files else None


def _download_file(api_key, file_id):
    resp = requests.get(
        f"{DRIVE_API_BASE}/files/{file_id}",
        params={"alt": "media", "key": api_key},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.text


def run_pickup(dry_run=False):
    api_key = os.environ.get("GOOGLE_DRIVE_API_KEY")
    folder_id = os.environ.get("GOOGLE_DRIVE_FOLDER_ID")
    if not api_key or not folder_id:
        logger.error("GOOGLE_DRIVE_API_KEY and/or GOOGLE_DRIVE_FOLDER_ID not set — nothing to do")
        return {"error": "missing_config"}

    init_db()

    latest = _find_latest_file(api_key, folder_id)
    if not latest:
        logger.info("No '%s' file found in the Drive folder yet — nothing to import", RESULTS_FILENAME)
        return {"status": "no_file"}

    db = SessionLocal()
    try:
        state = db.query(DiscoveryImportState).first()
        if state and state.last_drive_file_id == latest["id"]:
            logger.info("Latest Drive file (%s, created %s) already imported — nothing new",
                        latest["id"], latest["createdTime"])
            return {"status": "already_imported", "file_id": latest["id"]}

        logger.info("Found new results file: %s (created %s)", latest["id"], latest["createdTime"])
        text = _download_file(api_key, latest["id"])
        results = json.loads(text)
        logger.info("Downloaded %d result entries", len(results))

        if dry_run:
            logger.info("[dry-run] would import %d entries and mark file %s as imported", len(results), latest["id"])
            return {"status": "dry_run", "entries": len(results)}

        counts = import_results_from_data(results, dry_run=False)

        if not state:
            state = DiscoveryImportState()
            db.add(state)
        state.last_drive_file_id = latest["id"]
        db.commit()

        logger.info("Import complete: %s", counts)
        return {"status": "ok", "file_id": latest["id"], "counts": counts}
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(description="Pick up and import the discovery routine's latest Drive results file")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    result = run_pickup(dry_run=args.dry_run)
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
