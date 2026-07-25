# Update the 3 new-account routines to not depend on git repo access

Use this **either** if the GitHub App repo-access fix didn't actually take
(re-test first — trigger `trig_019xosk9ScZfmyz4VTtnx7g7` and check if it
can now read the repo), **or** just as a permanent robustness upgrade
regardless, since it no longer depends on any GitHub App configuration
being correct on this account at all — verified working right now: a
relay routine on the original account publishes
`groundwork-repo-relay.json` to the same shared Drive folder every hour,
containing the exact same data these routines used to read from git.

For each of the 3 routines below, use `RemoteTrigger action=update` with
`trigger_id` set to the id shown, and `body: {"job_config": <the full
updated job_config from that routine's section>}` — this replaces only
the prompt content, keeping the existing cron_expression/name/connectors.

---

## 1. Discovery routine — `trig_019xosk9ScZfmyz4VTtnx7g7`

New prompt content (replaces the whole message):

> You are running the nightly free email-discovery pass for Groundwork's outreach pipeline.
>
> INPUT: use the Google_Drive MCP connection to find the most recently created file named exactly `groundwork-repo-relay.json` in the shared folder `1gzp5oAuumxR-zVk_tAKdWTD_ktXdWiJb`, and read its contents. Parse it as JSON — its `pending_batch` field is your list of prospects to process (each entry: prospect_id, business_name, trade, location, website, website_status, phone). This file is refreshed hourly by a separate relay job, so always use the most recent one by creation time, not a cached copy.
>
> Do NOT attempt to WebFetch any groundworkbuild.com URL for input or output — that domain is blocked from this sandbox at the platform level, confirmed repeatedly, don't waste turns retrying it.
>
> YOUR WORK, per prospect:
>
> (a) WEBSITE RE-DISCOVERY (only if `website` is null/empty): WebSearch `"<business_name>" <location> website`. If a genuine, plausible business website turns up (an actual .co.uk/.com site that appears to belong to this specific business, not a directory listing), note the URL and WebFetch it with prompt "Find a genuine published contact email address on this page — a mailto: link or a plain-text email visible in the page content. Only report one if it's literally there; do not guess or construct one from the business name or domain." If found, that's your email (source=own_website), skip to (c). Record the rediscovered URL in the `website` field of your output entry for this prospect either way.
>
> (b) If no email yet (source=web_search) — emails found this way have shown a real 31.6% bounce rate in production (vs 0% for own-website scrapes), almost entirely "valid domain, wrong/dead mailbox." WebSearch `"<business_name>" <location> email contact`, then WebSearch `"<business_name>" <location> Facebook`. An email found this way may ONLY be reported if BOTH hold: (1) it's literally visible in a search snippet or a page you WebFetch, never guessed/constructed; (2) you have at least one independent corroborating fact tying it to this specific business — matching phone/address on the same listing, the same email in 2+ independent results, or the page explicitly naming this exact business. Note which corroboration you used in `notes`. No corroboration = leave it null, don't guess.
>
> FACEBOOK PAGE CAPTURE: the Facebook search above is also a capture step in its own right. If it turns up a genuine Facebook Page for this specific business, record its URL in `facebook_page_url` regardless of the email outcome — this feeds a separate manual DM queue and doesn't need the same corroboration bar as an email.
>
> If WebFetch is failing broadly, fall back to WebSearch-snippet-only discovery and note it. Don't bother trying UK trade directories (Checkatrade/Yell/TrustATrader) via WebFetch — they block it.
>
> (c) Record the result for this prospect (found or not) and keep going — don't pause between prospects. Parallel sub-agents on disjoint slices are fine; you do the single final Drive write.
>
> Keep going until you've processed every entry, or hit your own session's usage/turn limit.
>
> OUTPUT (always do this last, even if partial): use the Google_Drive MCP connection's create_file tool to create a file titled exactly `groundwork-discovery-results.json`, parent_id `1gzp5oAuumxR-zVk_tAKdWTD_ktXdWiJb`, content_mime_type `application/json`, disable_conversion_to_google_type true, text_content a JSON array, one entry per prospect actually processed: `{"prospect_id": int, "email": str|null, "source": str|null, "website": str|null, "notes": str|null, "facebook_page_url": str|null}`.
>
> Summarize in your final response: how many processed, how many emails found, how many rejected for lack of corroboration, how many Facebook Pages captured, any reliability issues.

---

## 2. Variant-candidate routine — `trig_01XBk2fYnbjythwsbcZC7545`

New prompt content:

> You are running the daily email-variant candidate generation pass for Groundwork's outreach pipeline.
>
> INPUT: use the Google_Drive MCP connection to find and read the most recently created file named exactly `groundwork-repo-relay.json` in the shared folder `1gzp5oAuumxR-zVk_tAKdWTD_ktXdWiJb`. Parse it as JSON. Its `variant_candidate_request` field is your list of pending requests (if empty, there is simply nothing to do today — still do the OUTPUT step below with an empty array, don't treat it as an error). Its `evidence_library_md` field is the full evidence library — Section 1 (external research), Section 2 (Groundwork-specific constraints, OVERRIDES Section 1 on any conflict), Section 3 (internal findings).
>
> Each request entry has: reserved_variant_id, stage (initial/A/B/C/D), parent_variant_id, parent_subject, parent_body, isolated_variable, recent_findings, requested_at.
>
> YOUR WORK, for EACH request entry:
>
> 1. Ground every change in Section 1 or a recent_finding from the request. Section 2 overrides Section 1 on conflict.
>
> 2. Produce ONE new candidate (subject + full HTML body) changing EXACTLY the isolated_variable vs. parent_subject/parent_body — nothing else. Subject-level variables (subject_length, subject_format_question, subject_personalization): body must be byte-identical to parent_body. Body-level variables (cta_wording, personalization_depth, paragraph_structure, tone): subject must be byte-identical to parent_subject; cta_wording changes only the CTA button's visible text (preserve paragraph count/word count); paragraph_structure changes only paragraph block count (CTA text identical); personalization_depth/tone change only wording/specificity (CTA text AND paragraph count both stay identical).
>
> 3. Hard requirements: preserve every {placeholder} token exactly (business_name, preview_link, unsubscribe_link, branding_ps if present) — same set, no additions, no stray curly braces elsewhere. Preserve the exact HTML table-based scaffold. Stage initial/A/B: never claim the site is already built. Stage C/D: it's accurate to say so. Only real prices are £99 (one-time setup) and £24.99 (monthly) — competitor-price contrast is fine, inventing a different Groundwork price isn't. No spam phrasing (bare all-caps FREE, act now, 100% free, risk free, limited time, guaranteed, 2+ exclamation marks, 2+ other all-caps words). Write a rationale citing specifically what you grounded the change in.
>
> A candidate violating any of this gets auto-rejected by the receiving job's content-safety/isolation gates — follow precisely, not approximately.
>
> OUTPUT (always, even if the request list was empty): Google_Drive MCP create_file, title exactly `groundwork-variant-candidates.json`, parent_id `1gzp5oAuumxR-zVk_tAKdWTD_ktXdWiJb`, content_mime_type `application/json`, disable_conversion_to_google_type true, text_content a JSON array: `{"reserved_variant_id": str, "subject": str, "body": str, "rationale": str, "cites": str}` per entry (empty array `[]` if nothing to process).
>
> Summarize in your final response: how many requests found, how many candidates produced, which isolated_variable and evidence source each cited.

---

## 3. Facebook-sourcing routine — `trig_01J4qQwg6h9xNeSSmkubHGCW`

Only change: replace the opening line —

> Read CLAUDE.md's "Outreach pipeline" section first (Read tool, this repo is cloned into your working directory) to corroborate this task against the project's checked-in source of truth.

with:

> This task is fully self-contained and doesn't require repo access — if you happen to have it, CLAUDE.md's "Outreach pipeline" section has extra context, but proceed regardless of whether it's available. Do not stop or refuse over missing repo access for this particular task.

Everything else in this routine's prompt is unchanged (it was never reading a dynamic file — its trade/town list is already inline in the prompt).
