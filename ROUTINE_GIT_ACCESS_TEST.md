# Test: can this account's routines actually read the repo?

Paste this whole message into a Claude Code session on the **new** account:

---

Create a one-off routine using the RemoteTrigger tool, then run it immediately, then show me the result.

- name: "GROUNDWORK git access test"
- run_once_at: 5 minutes from now (UTC)
- job_config.ccr.environment_id: look up this account's own default environment ID first, don't guess
- job_config.ccr.session_context.sources: `[{"git_repository": {"url": "https://github.com/webbo28104-sketch/GROUNDWORK"}}]`
- job_config.ccr.session_context.allowed_tools: `["Read"]`
- prompt: "Read the file CLAUDE.md from this repo's checkout and report back its first line, plus confirm whether the repo was actually cloned into your working directory."

After it runs, tell me exactly what it reported — including if it failed to find/clone the repo at all.

---

If it reports the first line of CLAUDE.md → repo access works, we rebuild the 3 real routines next.
If it says the repo isn't there → repo access is still broken, and we know that's the actual blocker, not something else.
