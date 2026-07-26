# Test: can this account's routines actually read the repo?

Paste this whole message into a Claude Code session on the **new** account:

---

Before anything else, run ToolSearch with query "select:RemoteTrigger" to load the full RemoteTrigger tool. Do not use any other create_trigger/fire_trigger tool — it's missing fields we need.

Then use RemoteTrigger action=create to make this routine, and action=run right after to fire it immediately:

- name: "GROUNDWORK git access test"
- job_config.ccr.environment_id: look up this account's own default environment ID first, don't guess
- job_config.ccr.session_context.sources: `[{"git_repository": {"url": "https://github.com/webbo28104-sketch/GROUNDWORK"}}]`
- job_config.ccr.session_context.allowed_tools: `["Read"]`
- mcp_connections: attach this account's own Google Drive connector (look up its connector_uuid first, don't guess)
- prompt: "Read the file CLAUDE.md from this repo's checkout. Then use the Google_Drive MCP connection's create_file tool to create a file titled exactly `groundwork-git-access-test-result.json`, parent_id `1gzp5oAuumxR-zVk_tAKdWTD_ktXdWiJb`, content_mime_type `application/json`, disable_conversion_to_google_type true, text_content: {\"repo_cloned\": true or false, \"first_line\": \"<first line of CLAUDE.md, or null>\", \"notes\": \"<anything relevant, e.g. the exact error if the repo wasn't there>\"}. Do this even if the repo wasn't found — just set repo_cloned to false and explain why in notes."

Once it's fired, just tell me it's done — I'll check the result file in Drive myself.

---

Why the change from before: writing straight to Drive means the result is checked directly, not relayed through chat (which doesn't survive between sessions on this setup).
