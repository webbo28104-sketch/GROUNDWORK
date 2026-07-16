"""
Pending text-edits apply job — nightly (added 2026-07-16).

Runnable as a script from the project root:
    python apply_pending_edits_job.py

What it does (see app.py's run_pending_edits_apply for the actual logic):
  For every live Generation with html_pending set (customer used the editor
  after going live, via /editor.html), promotes html_pending -> html_content
  and emails the customer that their changes are now live. These are plain
  text substitutions already validated field-by-field at save time
  (_update_gw_text_field) — no LLM involved, since there's no judgement call
  left to make by the time a row reaches this job.

Needs to run on a real recurring schedule (a Railway Cron service pointed at
this script) — there is no in-process scheduler in this codebase, so nothing
calls this automatically on its own.
"""
import os

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except Exception:
    pass

from app import run_pending_edits_apply  # noqa: E402


def main():
    run_pending_edits_apply()


if __name__ == "__main__":
    main()
