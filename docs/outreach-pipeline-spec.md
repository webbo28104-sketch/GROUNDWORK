# Groundwork Outreach Pipeline — Build Spec

## Goal

Automated daily pipeline: source UK trade businesses → qualify → generate a personalized site preview → email outreach → follow-up sequence → convert to paying customer. Target: starts at 5–10 emails/day, compounding weekly with no fixed ceiling — fully automated selection, no manual approval gate.

---

## 1. Prospect Sourcing

- **Source:** Google Places API (Text Search), Enterprise tier (~$35/1,000 calls — rating, website field, phone, business status)
- **Scope:** National, all trade categories, no regional/trade caps on final selection
- **Method:** Rotate through a grid of UK postcode areas × trade categories. Each day, run ~20–25 search queries covering different region/trade cells, prioritizing cells not searched recently, to build a raw daily candidate pool (target 200–500+ raw candidates/day)
- **Dedup:** Check every candidate against existing prospects table before processing — skip if already present

### 1a. Sourcing grid capacity (expanded 2026-07-21)

Grid: 2,834 UK postcode districts × 44 trade categories = 124,696 cells (was 353 town names × 30 trades = 10,590; ~11.8x). See `outreach/trade_categories.py`'s module docstring for the full rationale — summary:

- **Geography**: replaced hand-typed town names with real UK postcode districts (e.g. "M1", "SW1A"), sourced from a public dataset and verified live against the Places API. A live test proved a *bare* district code is unreliable ("plumber in M1" alone returned Brighton, not Manchester — M1 also reads as the motorway to Google's geocoding), so every query is qualified with the district's real town (`AREA_SEARCH_QUALIFIER`), e.g. "plumber in M1, Manchester". Target was ~30x (per instruction, aiming as high as the real market supports) — 11.8x is the honest ceiling: there are only ~2,857 real UK postcode districts nationally, a fixed resource, and roughly 1,700 of the 2,834 used here are towns with *no* prior coverage (genuine new market) while a few hundred are finer subdivisions of the ~60 biggest cities that were already covered (bounded, diminishing-returns gain — mostly recovers results beyond position 20/60 of an existing whole-city search, not a new market).
- **Trades**: 30 → 44 (14 new genuinely distinct sub-trades: conservatory installer, uPVC window installer, loft conversion specialist, decking installer, artificial grass installer, CCTV/security installer, window cleaner, damp proofing specialist, insulation installer, solar panel installer, rendering specialist, garden room installer, rubbish clearance, pressure washing/driveway cleaning). This is the single most reliably *linear* lever — a new trade term hits businesses never searched before, everywhere, unlike geographic subdivision of an already-covered city.
- **Pagination**: `outreach/sourcer.py`'s `search_places()` now follows Places API v1's `nextPageToken` up to 60 results/query (3 pages), not just the first 20. Real production data at the time of this change showed 21% of already-searched cells (22/105) hit the 20-result cap exactly, confirming this isn't a rare edge case.
- **Periodic refresh**: `get_pending_cells()` now enforces `MIN_RESEARCH_INTERVAL_DAYS = 75` — a cell won't be re-searched sooner than ~75 days after its last search, once the never-searched pool is exhausted. Non-binding for a long time in practice: at the default 25 cells/day, a full first pass of the new 124,696-cell grid takes ~13.6 years.
- **Honest yield projection**: real per-cell yield at the time of this change was ~12.3 qualified prospects/cell (from only 105 of the old 10,590 cells searched so far — too early to be a stable long-run average). Applying that anchor with a haircut for smaller/rural new-town cells and heavy discounting for subdivided-city cells (which mostly overlap with the pagination gain on the same city, not a distinct new pool), the trade-expansion axis alone (14 new trades × 2,834 areas, first-time queries everywhere) is the largest single component — larger than the geography axis. Treat any total-pool number as a model, not a guarantee; `SearchCell.results_found` is the real ongoing measurement.

---

## 2. Gates (pass/fail — drop if any fail)

- Not marked "permanently closed" on Google
- A genuine, contactable email is found (see Section 4)

> **Note:** "has a website" is NOT a gate — see Section 3.
>
> **Note:** Business legal structure — sole trader vs. limited company — is NOT a gate. User has made a deliberate, informed decision to email all qualifying trade businesses regardless of structure, accepting PECR exposure for the sole-trader segment. Unsubscribe/opt-out handling (Section 11) must be immediate and strictly honored for all recipients given this.

---

## 3. Website Condition Check

**Replaced with a free binary check — no screenshot, no vision judgment.** The dated/modern distinction below (and the associated open issue about content-depth false positives) is retired for now: `outreach/pipeline.py` tags every candidate as one of

- `no_website`
- `has_website`

purely from whether Places' own `website` field is populated — no screenshot, no Cowork vision call, no per-candidate cost or latency. The `vision_flag_*` columns and the dated/modern checklist below are kept on the schema/doc for later, in case the content-depth-aware version of this check gets built, but nothing currently populates them.

<details>
<summary>Retired checklist (dated/modern vision judgment — not currently run)</summary>

**⚠️ OPEN ISSUE — needs reconfiguration before scaling volume**: the checklist below currently judges only *visual* staleness (layout, design cues, CTA presence, stale content signals). It does not check *content depth* — several businesses tagged `dated` in review have turned out to have more detailed, complete sites (more service pages, real project portfolios, fuller copy) than Groundwork's generator currently produces. Pitching "we'll upgrade you" to a business whose existing site is actually more substantial than the replacement undermines the whole approach. The check needs a second dimension — comparing content completeness against what the generator actually delivers — not just visual polish, so outreach only targets genuinely sub-par sites, not merely visually outdated ones with real substance behind them.

**Method:** If a website exists, screenshot it and run a vision check against this checklist. Score 2+ = dated. Site fails to load/times out/cert error = automatic dated.

**Checklist:**

1. Fixed/non-responsive layout, squeezed or misaligned content
2. Outdated design cues — default template look, clashing colours, stretched/pixelated images, low-res logo
3. No clear call-to-action anywhere
4. Stale content — old copyright year, broken images, placeholder text, dead links
5. No reviews/testimonials shown despite the business having Google reviews
6. Fails to load / times out / security warning

`has_website_modern` candidates were effectively deprioritized (0 pts in scoring) but not hard-excluded.

</details>

---

## 4. Email Discovery (for gated/qualified candidates only, not full raw pool)

- **Tier 1** (`outreach/email_scrape.py`) — plain code, no AI, zero cost: checks the business's own website (`mailto:` links, plain-text address on the homepage/contact page). Runs first, always, for every prospect with a `website` on file.
- **Hard rule**: Never generate/guess a plausible email (e.g. info@businessname.co.uk pattern-matching). Only extract emails actually found in a source. If none found: route to `qualified_no_email` if the prospect has a phone number (SMS-reachable), or `unreachable` if it has neither — surfaced as a filterable category in the Tinder review UI, not silently dropped or logged-only, either way.
- **MX/A-record check before an email is accepted** (added 2026-07-17, `outreach/email_verify.py`) — a free DNS lookup, not a paid tool. Discards any discovered address whose domain has no mail route at all (dead domain, typo) before it's ever written to a `Prospect` row, and is re-checked once more immediately before send (`outreach/send_job.py`) to catch anything that predates this check or died in between. Does not confirm a specific mailbox exists — no SMTP RCPT probing (unreliable, can look like abuse) — only that mail addressed to the domain has somewhere to go.

### 4a. Tier 2 (Anthropic API) — removed 2026-07-18

The original Tier 2 called the Anthropic API (web_search tool) to check Facebook Business Page → UK trade directories (Checkatrade, Yell, TrustATrader, Rated People, Bark, MyBuilder, FreeIndex) → general web search, in that order, whenever Tier 1 found nothing. **Deleted outright** (not disabled behind a flag) after a cost review: one overnight batch cost ~£15-20, and of the prospects that reached this tier, the majority (many `no_website` prospects specifically) never turned up an email at all — the API spend was going largely to searches that came up empty, not to genuine finds. `outreach/email_discovery.py` now contains only the pure-code validators (`is_valid_email`, `looks_like_guess`) that other parts of the pipeline still reuse; `find_email()` and everything that called the Anthropic API are gone from the file. The `email-discovery-cron` Railway service that ran this nightly at 02:00 UTC had its `ANTHROPIC_API_KEY` removed as a second, infrastructure-level safeguard — it now runs Tier 1 only and can't spend API credits even if the code were reverted.

**Replacement, not just a removal.** Live-tested during the 2026-07-18 build: Facebook Business pages (login wall) and the major UK trade directories (Cloudflare bot-challenge on Checkatrade/Yell, confirmed even via a real headless-Chromium browser, not just plain HTTP) actively block scraping — so no free, code-only replacement for Tier 2 was viable; those sites are specifically defended against this. What *does* work reliably without triggering any of that: `WebSearch`, which reads a search index rather than fetching the gated page directly. A scheduled Claude Code routine ("Groundwork nightly email discovery," `trig_018q5mtSkfo5AkmwywJq1psk`, cron `30 3 * * *` UTC — after `sourcing-cron` at 01:00 and the Tier-1-only `email-discovery-cron` at 02:00, well before `send-job-cron` at 08:00) processes the full pending backlog per night using WebSearch, applying validated results through the pipeline described below. This uses the Claude subscription's included tool access, not the metered Developer API — genuinely free, in contrast to the deleted Tier 2.

**The routine cannot reach groundworkbuild.com or push to git — confirmed, not a config gap.** Three separate designs were tried and ruled out across 2026-07-18/19, in order:

1. **Direct API calls** (WebFetch to `/api/admin/outreach/g/*`) — the routine's CCR sandbox has its own egress policy that blocks `groundworkbuild.com` outright (a 403 at the proxy's CONNECT-tunnel stage, confirmed via the sandbox's own `/__agentproxy/status` diagnostic endpoint — an environment-level policy denial, not a transient error or a Cloudflare issue on the app's side).
2. **Git push** (to a dedicated branch, with and without an `outcomes` grant in the routine's `job_config`) — every attempt across many nights and configurations returned 403, including via the GitHub App/MCP integration ("Resource not accessible by integration"). `git ls-remote` confirms no `claude/*` branch has ever actually landed on the remote from any CCR session. This looks like a platform-level restriction on CCR write access, not a per-repo permission that can be granted — nothing on either GitHub's side (Installed vs. Authorized GitHub Apps) or claude.ai's Connectors page exposed a fixable write-scope toggle.
3. **Google Drive** (current, working design) — the routine has a `Google_Drive` MCP connection attached (the same connector mechanism used for Calendar/Notion elsewhere), which goes through a dedicated connector transport rather than the sandboxed HTTP/git paths above — and it works reliably even on nights where WebFetch is broadly down for every other domain (confirmed via a control-fetch to bbc.co.uk failing in the same run).

**Current architecture, end to end:**
- **Input** — `.github/workflows/export-pending-batch.yml` (GitHub Actions, normal unrestricted network) exports the pending queue to `outreach/discovery_batches/pending_batch.json` and commits it, nightly at 03:00 UTC. The routine reads this file via a plain `Read` call against its own git checkout — no network request needed for input.
- **Processing** — same WebSearch-based method as before (own-site re-discovery → contact/Facebook search → apply, parallelized across sub-agents on disjoint slices of the batch).
- **Output** — the routine writes one JSON file per night, titled exactly `groundwork-discovery-results.json`, into a Drive folder shared read-only ("anyone with the link") so it can be read back with a plain API key — no OAuth/service-account needed.
- **Pickup** — `.github/workflows/pickup-discovery-results.yml` (GitHub Actions, `0 6 * * *` UTC — after the routine's ~1hr typical runtime, before `send-job-cron` at 08:00) runs `outreach/pickup_drive_results.py`, which finds the newest matching Drive file, checks it against `DiscoveryImportState.last_drive_file_id` (idempotency — never double-imports the same night's results), and applies it via `outreach/import_discovery_results.py`'s validated path (format check, guess-detection, MX check, scoring, funnel-stage finalize). Fully unattended — runs on schedule regardless of whether anyone opens a conversation that day.

A real, unexpected finding from testing this: WebSearch sometimes surfaces a genuine website URL that Places API missed entirely (`website_status: no_website` was wrong). The routine re-checks this first — if a real site turns up, it's recorded in the output entry's `website` field and applied on import (mirroring what the now-unreachable `/g/update-website` endpoint used to do live). In the sample tested, this found 2 real websites Places had missed out of 3 checked, though neither of those particular sites had a scrapable email either — the fix improves data quality (accurate `website_status`, which also feeds scoring) even on nights it doesn't directly yield an email.

**Honest expectation, not oversold**: direct WebSearch-based email finding for the `no_website` segment specifically found 0 of 3 in an early sample tested — these businesses generally don't publish an email anywhere searchable at all (phone + word of mouth, or a directory contact form that never exposes a raw address). This isn't a tooling gap to fix; SMS is the realistic channel for a real share of this segment, and the pipeline already treats it as first-class (Section 10a).

---

## 5. Scoring (0–100, applied after gates + website check)

**Redesigned 2026-07-18** after a full audit of what the live data actually looks like, prompted by a direct challenge to the original model's assumptions: does a high review count really mean a *better* prospect, and should having a website score any points at all? Querying the production `prospects` table (684 rows at the time) against the original 5-factor model found two real, evidence-backed problems, not just theoretical ones:

1. **`review_count`'s bucket edges (0 / 1 / 2–5 / 6+) didn't match the real distribution.** Real median review_count is **50** (IQR 19–108, max 964) — not single digits. 622 of 678 prospects (91.7%) already cleared the old "6+" ceiling, so this factor was maxed out for nearly the entire pool and differentiated almost nothing.
2. **The direction was backwards.** Grouping by `website_status`: `no_website` prospects have a median review_count of **21**; `has_website_modern` prospects have a median of **91** (~4x higher). High review count correlates with *already having invested in a better web presence*, not with wanting to invest in one now. Rewarding more reviews with more points was pushing the least-likely-to-convert segment to the top of the queue.

`rating` had a milder version of the same problem — 600/678 prospects (88.5%) are already 4.8+, so the old 4.5+ cutoff barely differentiated within the qualified pool. And `team_size`'s heuristic (`"ltd"` in the business name AND review_count > 100) only matched 30% of prospects and had no real evidentiary basis — dropped.

**What changed:**

| Factor | Old max | New max | Why |
|---|---|---|---|
| Website status | 25 | **40** | The single most direct "do they need this" signal — increased weight, and no longer a flat 10 for every `has_website` row (see below) |
| Trade tier | 20 | 20 | Unchanged — no evidence against it |
| Review count | 15 | 20 | Same weight-ish, but rebuilt as non-monotonic (see below) — this is where team_size's 10pts and part of rating's cut went |
| Rating | 30 | 20 | Reduced — real distribution barely differentiates within the qualified pool; still a legitimacy signal, just not a primary predictor |
| Team size | 10 | 0 (removed) | Unreliable string-match proxy, no real basis; folded into review count |

| Factor | Max pts | Breakdown |
|---|---|---|
| Website status | 40 | `no_website` = 40. `has_website` now reads a free staleness heuristic (`website_quality`, see below) instead of a flat value: `unreachable` (their own site doesn't load) = 38, `dated` = 24, `modern` = 6, not-yet-checked = 14. Legacy vision-judged rows: `has_website_dated` = 30, `has_website_modern` = 4 |
| Trade type tier | 20 | High = 20, Medium = 12, Low = 5 (see Section 6) |
| Review count | 20 | Non-monotonic — 0 = 4, 1–9 = 14, 10–49 = 20 (sweet spot), 50–149 = 10, 150+ = 4. Rewards proof of being real/active without rewarding scale, per the finding above |
| Rating | 20 | <4.3 = 6, 4.3–4.6 = 12, 4.6–4.8 = 16, 4.8+ = 20 (bucket edges reset to match the real distribution, not the old 4.0/4.5 cutoffs) |

**`website_quality` — a free replacement for the retired vision check.** Section 3's dated/modern vision judgment was deleted for cost in an earlier pass, leaving every `has_website` prospect scored identically regardless of whether their site is actually a 2026 template or a broken GeoCities-era stub — exactly the differentiation this factor most needs. Rather than pay for vision again, `outreach/email_scrape.py:assess_site_quality()` reads the HTML Tier 1 email discovery already fetches (or a dedicated fetch at sourcing time, `outreach/pipeline.py:_queue_pending`, if Tier 1 hasn't run yet) and checks four low-false-positive signals: missing viewport meta tag, plain HTTP instead of HTTPS, a stale copyright year (3+ years behind), and a suspiciously thin page (<3KB). Zero or one signal → `modern`/`unknown`; two or more → `dated`. A connection/DNS failure specifically (not a timeout or non-200, which could just mean the scraper's user-agent got blocked) → `unreachable`. Stored once on the `Prospect` row, never fetched live at score time — `score_prospect()` stays a pure function safe to call on every admin page render. See `outreach/rescore_all.py` for the one-off backfill that applied this to the existing prospect pool alongside the new weights.

Take the top N by score from the day's qualified, email-found pool, where N is the current ramp allowance (see Section 15). Selection is automated — no manual approval step. No regional/trade caps.

**Honest limitation:** this redesign is reasoned from the real *input data* distribution (rating/review_count/website_status), not from real *outcome* data — at the time of this audit the pipeline had only 20 sends, 1 click, 0 paid conversions, far below the 30-outcome minimum Section 5b already requires before trusting a correlation. Section 5b's adaptive feedback loop is the mechanism that will eventually confirm or correct these weights against real click/paid behavior — this pass fixes concrete, demonstrable bugs (stale bucket edges, backwards-direction reward, a flat score hiding a differentiable signal) rather than claiming to have found the provably optimal weights.

**Also audited, not currently usable:** `google_photos_count`, `opening_hours_complete`, `competitor_density`, and `email_domain_type` are all documented schema fields but are 100% unpopulated in production — the sourcing pipeline declares them but never writes them. They're not part of this scoring redesign; wiring them up (photo count and opening-hours completeness are already returned by the Places API call being made today, just not read into the row) is a reasonable future follow-up but out of scope here.

---

## 5a. Daily Send Selection (Automated)

- **No approval gate.** Qualified, email-found prospects (`email_found = true`) are automatically eligible for send without any manual review step — the daily send job picks the top N scored prospects, where N is today's ramp allowance (Section 15).
- **Send ordering** — top N by composite score (Section 5). Once enough click/paid data accumulates (Section 5b), shift toward predicted conversion likelihood for ordering within the eligible pool. Reserve roughly 15% of each day's volume as an exploration slice — prospects ranked below the pure top-N — to prevent self-reinforcing selection bias and give the correlation model a diverse sample to learn from.
- **The outreach queue UI remains as a monitoring and audit tool** — it shows prospects queued for pending vision/email checks, and sent/activity history — but it no longer blocks sends. A prospect can be manually excluded via the UI if needed, but inclusion is automatic.
- **Rollover** — unsent eligible prospects stay in the pool and are eligible again the next day, deprioritized below any newly qualified prospects of equal score.

---

## 5b. Adaptive Scoring Feedback Loop

Goal: use real click/paid conversion outcomes to detect when the current point weights (Section 5) don't predict what matters — and to prioritize the send queue accordingly. There is no approval data as an interim proxy; the feedback loop trains on click/paid from the start.

One model, one job:

- **Predicted conversion likelihood** — drives send ordering within the daily eligible pool (Section 5a). Falls back to the composite score (Section 5) until enough real outcome data exists, then gradually shifts weight toward per-factor conversion rates.

**Mechanics:**

- Log every scoring factor's value alongside every send and its eventual outcomes (clicked yes/no, paid yes/no) — not just the composite score. Composite-score analysis alone can't reveal which factor is miscalibrated; need per-factor breakdowns (e.g. click rate for `trade_tier = High` vs `Medium` vs `Low`; by rating band; by `website_status`; by `email_domain_type`; etc.)
- Also log the expanded prospect attributes captured at source time (Section 13) — `google_photos_count`, `opening_hours_complete`, `competitor_density`, etc. — even before they feed into the score, so they accumulate observations to evaluate.
- Compute rates per factor-tier daily (see Section 17 correlation view). Compare against what the current point allocation implies. Example: if `trade_tier = Medium` (12 pts) shows a 3× higher click rate than `trade_tier = High` (20 pts), that's a signal the current weights don't reflect actual market response.
- **Minimum sample size before acting:** require 30+ click/paid outcomes per factor-tier before treating a divergence as real rather than noise.
- **Surface, don't auto-change.** Flagged divergences appear as suggested reweightings in the Admin Dashboard (Section 17) and during the weekly Friday review — not applied silently.

---

## 6. Trade Type Tiers

| Tier | Points | Trades |
|---|---|---|
| **High** | 20 | Plumber, electrician, heating engineer, roofer, landscaper/gardener, decorator/painter, locksmith, domestic cleaner, pest control, tree surgeon, driveway/paving, fencing, guttering, handyman, kitchen/bathroom fitter, tiler, flooring fitter, glazier, garage door fitter, appliance repair, chimney sweep |
| **Medium** | 12 | General builder, carpenter/joiner, plasterer, bricklayer |
| **Low** | 5 | Specialist subcontractors (leadwork, scaffolding, groundworks, structural steel, cladding), demolition, commercial M&E, plant hire, project management |

---

## 7. Site Generation & Account Setup

- Feed qualified prospect's details (company name, trade, location, and — for `has_website` candidates — pulled logo/colours/copy from their existing site where available) into the existing generator
- Save under a pre-created account, no password set, keyed to a long, random, unguessable token (e.g. UUID)
- Site is watermarked/preview-only until payment

---

## 8. GIF Capture

- Headless browser (Playwright) opens the generated site, scrolls through in stages (hero → services → accreditations → contact form), screenshots each stage
- Stitch into a short (3–4 sec), compressed, low-frame-rate GIF
- Host publicly, save URL against the prospect record
- Design first frame to look acceptable alone (hero section) for clients that don't animate GIFs (older Outlook)

---

## 9. Magic Link / Auth

- Link format: `yoursite.com/claim/{token}`
- Never expires, works unlimited times
- On first click: prompted to set a password → then straight into the editor
- After that: normal email + password login
- Users can only edit text/wording — do not overpromise photo or colour editing in any copy

---

## 9a. Click-to-generation flow — implementation status

**Built and tested** (`app.py`, `models.py`, `outreach/link_identity.py`):

- **`Prospect.token`** (magic-link credential) and **`Prospect.short_code`** (for `/s/<short_code>`) are generated at the point a send is queued — `outreach/link_identity.py:ensure_link_identity()`, called from both `outreach/send_job.py:fill_initial_sends()` and `outreach/followup.py:_fire_touch()` (defensively, for any legacy row that reaches a follow-up without one).
- **`/s/<short_code>`** (`app.py:short_link`) — looks up the `Prospect` by `short_code`, 302-redirects to `/claim/<token>`. A plain server-side redirect, unchanged by the fixes below — it extends cleanly to the phone-only page too, since `/claim/<token>` is the one that branches, not the short link (see below).
- **`/claim/<token>`** (`app.py:claim`) — **no password/account barrier before seeing anything**, per the fix. Looks up the `Prospect` by `token`; if the prospect has no email on file (phone-only track), redirects to `/claim/<token>/email` instead. Otherwise calls the shared `_claim_generate_and_redirect()` helper directly: sets `clicked_at`, creates a `Lead` from the prospect's available fields (`_prospect_to_form_data`), sets `funnel_substage = "clicked_generated"`, calls the existing `_kickoff_generation()` — the same function `/verify/<token>` and `/admin/generate-test` already use — and redirects straight to `/loading.html?id=<lead.public_id>`. No session is set here; viewing the generating/generated site needs no login, same as the main form-submission flow. **Idempotent**: a repeat visit once `prospect.lead_id` is set redirects to the existing result (`/api/generate/<id>/html` if generation finished, `/loading.html` if still running) rather than creating a second `Lead`.
- **`/claim/<token>/email`** (`app.py:claim_email`) — the SMS magic-link destination for phone-only prospects (`has_findable_email` / `email_found = false`). Single required field (email). On submit: sets `prospect.email`, flips `email_found` (== `has_findable_email`) to `true` — making the prospect eligible for the parallel email follow-up track from that point on — creates/reuses an `Account` row for the email (no password yet), then calls the **same** `_claim_generate_and_redirect()` helper `/claim/<token>` uses, so generation kickoff and the `clicked_generated` transition are identical code, not a parallel implementation. If a prospect at this URL already has an email on file (e.g. discovered later some other way, or they've already submitted this form once), it just redirects to `/claim/<token>` — nothing left to capture.
- **Password is requested later, via the existing flow, not a new one.** `/account/login`'s `set_password` stage (used both for the "brand new signup" and "no password yet, but this email has a Generation" branches — this is the *existing* mechanism the fix asked to reuse) now additionally looks up any `Prospect` row(s) for that email with `account_created_at` still null and, if their `funnel_substage` is `clicked_generated`, flips it to `account_created` and stamps `account_created_at`. This is the one place that transition happens — added to existing code, not a new auth path.
- **Wired into `outreach/send_job.py`/`outreach/followup.py`** — `_preview_link`/`_short_code` point at these routes.

**Confirmed, not assumed** (tested against a scratch DB): `/claim/<token>` with an email on file skips straight to `/loading.html` with no password prompt, `funnel_substage` lands on `clicked_generated`, and a repeat visit is idempotent (no duplicate `Lead`); `/claim/<token>` with no email redirects to `/claim/<token>/email`, whose submission sets `email_found=true`, creates the `Account`, and reaches the same `_claim_generate_and_redirect` result; simulating a subsequent password-set via `/account/login`'s existing `set_password` stage correctly flips the linked `Prospect` to `account_created` with `account_created_at` stamped. Confirmed no route conflicts — `/claim/<token>`, `/claim/<token>/email`, and `/s/<short_code>` are three distinct Flask rules with no overlap, and the short-code system needed zero changes to reach the new email-capture page (it always resolves to `/claim/<token>`, which does the has-email branch itself).

**Fixed:** logging in via `/account/login` while generation is still mid-flight (before `_run_and_persist` writes the `Generation` row, so `_has_generation()` alone returns false) used to fall through to the "brand new signup, verify by email" branch — not an error or broken page, but a wrong-but-recoverable one: it sent a genuine, redundant email-confirmation link and showed "Check your email" instead of the set-password form the prospect actually needed, resolving correctly only after that extra email + click (and not at all if `RESEND_API_KEY` wasn't configured). Realistic enough to fix (someone opening the main site and clicking "Sign in" during the 150-300s generation window, not just a theoretical case — re-visiting the magic link itself doesn't trigger it, since `/claim/<token>` is idempotent). `account_login`'s `stage="email"` branch now also checks for a `Prospect` row with this email and `lead_id` already set (claim already happened, regardless of whether generation has finished) and treats that the same as `_has_generation` — verified against the exact mid-flight scenario (generation kicked off, no `Generation` row yet, login attempted) — goes straight to set-password, no redundant email sent.

### Open items (unchanged from before, still real gaps)

1. **Phone-only prospects can't be claimed without providing an email** — by design now (that's what `/claim/<token>/email` is for); once submitted they're no longer phone-only.
2. **`_prospect_to_form_data()` omits fields the form flow normally collects** (`work_split`, `craft_prestige`, `team_size`, `urgency`, `years_trading`, `claimed_accreditations`, `claimed_projects`, `other_notes`) — a `Prospect` row has none of these; `build_prompt.py` tolerates missing keys (`.get()`, skips falsy facts), so a prospect-sourced generation reads thinner than a form-submitted one. Inherent to cold outreach, not a bug.
3. **Section 7's "pulled logo/colours/copy from their existing site" step is NOT implemented.** No code anywhere downloads/extracts a logo or colour palette from a prospect's existing website — `_extract_logo_colors()` only operates on an already-uploaded image file from the form flow, which a `Prospect` never has. Separate, unbuilt feature.

### "Building your site..." progress messaging

The existing `loading.html` (shared with the main form-submission flow, not new) already has genuine staged messaging (7 milestone strings tied to a simulated progress bar), not a bare spinner — it wasn't starting from nothing. It was tuned assuming a ~160s average; given confirmed testing now puts real generation at **150-300s**, its previous pacing would reach 95% around 2:40 and then sit visibly stalled for up to another ~2:20 on a slow generation. **Retuned** (this pass) so it reaches 95% nearer 4:40, closer to the slow end of the confirmed range, reducing how long it can appear stuck.

**Flagged as its own follow-up, per the instruction to flag this:** this is still a *simulated* bar with no real backend step data behind it — it doesn't know what `_run()` (`app.py`) is actually doing at any moment, just elapsed time. A genuinely accurate progress indicator (the backend reporting which real step it's on — e.g. "verifying business," "writing copy," "running tools" — surfaced via the existing `/api/generate/<id>/status` poll) is a materially bigger change (instrumenting `_run()` itself to emit step state) and was not attempted here.

---

## 10. Email Templates

### Initial — No Website

- Subject: `{business_name} — take a look at your new website`
- Never says "live" — frames as a preview accessible via link, not a public site
- References actual built features: services breakdown, accreditations section, project gallery, quote enquiry form
- Includes scrolling GIF preview
- CTA: single link, repeated once
- Mentions: can edit wording; £99 setup + £24.99/month if they want to go live

### Initial — Has Website (Dated)

- Subject: `A modern version of {business_name}'s website`
- Same structure, references "kept your branding" (only if generation genuinely pulls their real logo/colours — verify before using this line)

### Finalized copy (implemented in `outreach/templates.py`)

Placeholders: `{business_name}`, `{preview_link}`, `{short_code}`, `{unsubscribe_link}`.

**Initial email** (all prospects with a findable email):

> Subject: `{business_name} — see a website built for you, no cost`
>
> Hi {business_name} team,
>
> We're Groundwork — we build affordable websites for UK trade businesses, without the agency price tag or hassle.
>
> See your website preview below, tailored to your trade and area — no cost, no obligation to take it further.
>
> {preview_link}
>
> If you like what you see, going live is £99 setup + £24.99/month, first month free — most other website services charge around £89 a month alone.
>
> Have a look and see what you think — any questions, just reply to this email.
>
> P.S. — here's a real site we've built recently, live now: sussexleadcraftltd.com
>
> ---
> Don't want emails like this from us? Reply STOP or click here to unsubscribe: {unsubscribe_link}

**Initial SMS** (phone-only, no email found — this is their only first touch):

> Hi {business_name}, this is Groundwork — we build affordable websites for UK trades. See a free preview built for you: groundworkbuild.com/s/{short_code}
> £99 setup + £24.99/mo after, 1st month free.
> Reply STOP to opt out.

See Section 11 for the follow-up Stage A/B/C/D copy (email + SMS).

---

## 10a. SMS Channel

Runs as a parallel channel to email, same qualified prospect pool (no separate gating) — sent to companies and sole traders per the risk decision in Section 2.

- **Source:** phone number already comes from the Places API Enterprise tier pull (Section 1) — no separate discovery step needed, unlike email
- **Provider:** Esendex (UK-based, ICO-registered) — the actual decision, corrected from an earlier build that was mistakenly implemented against Twilio (see "Provider correction" below). Plivo is a fallback only if Esendex's API proves difficult in practice — not built preemptively.
- **Compliance setup** (one-off, before first send): whatever sender-ID/registration Esendex itself requires for promotional SMS — not the Twilio A2P process this section originally named, which no longer applies
- **Content:** shorter version of the email templates — same core message (preview link, key features, £99+£24.99/month), single CTA link
- **Unsubscribe:** handled via STOP keyword reply, and Esendex's own "stop" webhook event — both feed the same Prospect.sms_unsubscribed flag (see `outreach/reply_handling.py:handle_forced_sms_stop` for the latter)
- Unsubscribe is channel-specific: an SMS opt-out does not imply an email opt-out, and vice versa — track separately (see schema, Section 13)
- **Follow-up sequence** (Section 11): same trigger logic and timing, but can route through SMS instead of/alongside email

### Provider correction: Twilio → Esendex

All SMS integration code (`outreach/sms.py`, the inbound webhook, stop-keyword detection, delivery-status feed) was originally built against Twilio's API in an earlier pass. That was a mistake — the actual decision is Esendex as primary, Plivo as a fallback only if Esendex proves difficult. **No Twilio account exists or will be set up.** Reworked entirely; here's what changed structurally versus what carried over untouched.

**Carried over completely unchanged — provider-agnostic by design, confirmed still true:**
- `outreach/reply_handling.py` — `STOP_KEYWORDS`, `is_stop_intent()`, `find_prospect_by_phone()`/`find_prospect_by_email()`, `_apply_reply()`, `handle_inbound_sms()`, `handle_inbound_email()`. None of this ever referenced Twilio; it operates on a phone number and a message body string, regardless of transport. Zero changes.
- `outreach/ramp.py:get_health_signal("sms")` — reads only from `SmsDeliveryEvent` (message_sid/status/created_at), never anything Twilio-specific. Zero changes.
- `send_job.py`/`followup.py`'s import of `send_outreach_sms` from `outreach.sms` — same function name, same call signature at the two send-sites (`(to_phone, body)` in, message id out). Callers didn't need touching beyond capturing the returned id for tracking (see below).
- The whole follow-up timing/channel-logic layer (`outreach/followup.py`'s stage/timing rules, email-track-gets-both-channels logic) — has no provider awareness at all.

**Changed structurally, not just re-pointed at a different API:**
1. **Sending** (`outreach/sms.py:send_outreach_sms`) — Esendex's Message Dispatcher (`POST https://api.esendex.com/v1.0/messagedispatcher`, HTTP Basic Auth, XML body/response) replaces Twilio's `Client.messages.create()`. Confirmed against Esendex's public docs. Requires `ESENDEX_USERNAME`, `ESENDEX_PASSWORD`, `ESENDEX_ACCOUNT_REFERENCE` env vars (not set anywhere yet — you'll need real Esendex credentials before any send works).
2. **Delivery status is now a POLL, not a PUSH.** This is the biggest structural difference. Twilio let you register a per-send `status_callback` URL; Esendex has no confirmed equivalent for plain SMS — its only documented "account"-product webhook events are `inbound` and `stop` (delivery events like `delivered`/`failed` are documented solely under the separate Rich Content API product, for RCS/WhatsApp, not the plain-SMS path used here). Built instead as `outreach/sms_status_poll.py`, which polls Esendex's Message Headers API (`GET /v1.0/messageheaders/{id}`) for any message whose last known status isn't terminal, and logs changes to the same `SmsDeliveryEvent` table the old webhook wrote to — `get_health_signal("sms")` needed zero changes as a result. Run this periodically (`python -m outreach.sms_status_poll`) — hourly is a reasonable cadence given Esendex typically resolves delivery within minutes; the old code polled/logged nothing between the once-daily `send_job.py` run and this is a real gap until scheduled.
3. **Inbound webhook auth changed.** Twilio's `X-Twilio-Signature` HMAC scheme has no confirmed Esendex equivalent — replaced with a shared-secret query parameter embedded in the callback URL you register with Esendex (`?secret=...`, checked against `ESENDEX_WEBHOOK_SECRET`), the same pattern already used for the Cloudflare email webhook's `EMAIL_INBOUND_SHARED_SECRET`. Fails closed if unset.
4. **Esendex's own "stop" classification is honored directly**, bypassing keyword matching — `outreach/reply_handling.py:handle_forced_sms_stop()` (new), called when Esendex's webhook reports `eventId: "stop"` rather than re-running `is_stop_intent()` against a message Esendex has already classified.

**Not fully confirmed — flagged, not guessed silently:** Esendex's webhook event *envelope* (`productId`/`eventId`/`eventVersion`/`eventTime`/`data`, and that `productId: "account"` covers `inbound`/`stop`) is confirmed from their public docs. The exact field names *inside* `data` for these two event types were **not** — Esendex doesn't publish a field-level schema for them. `app.py:sms_inbound_webhook` parses defensively (tries several plausible key names for the phone number and message body) and logs the full raw payload on every request specifically so this can be verified and tightened against a real captured payload before being trusted blindly — the same honesty pattern used for the Cloudflare email Worker's MIME parser (Section 11a).

**Also not done:** the Esendex webhook *subscription itself* isn't created by any code here — that's an account-side setup step (via Esendex's dashboard or a one-time API call once real credentials exist), the same category of "still on you" step as the Cloudflare Email Routing rule.

---

## 10b. Channel Circuit-Breakers

Two independent circuit-breakers, one per channel. Either can trip without affecting the other.

### Email circuit-breaker

Automated logic to protect outreach effectiveness if the email sending domain's reputation degrades:

- **Monitor:** Google Postmaster Tools spam complaint rate daily (Resend bounce/complaint webhooks as a faster interim proxy)
- **Trigger threshold:** spam rate crosses 0.1% (Gmail's hard ceiling is 0.3%, but degradation begins well before that — 0.1% is the working line)
- **While triggered:**
  - New prospects sourced during this period get SMS as the first touch instead of email
  - Email send volume is paused to allow recovery; ramp resets to the week-1 floor on resume
- **Recovery:** spam rate must hold below 0.1% for 7 consecutive days before standing is restored. Resume email-first routing only once metrics are consistently clean.

**Revised 2026-07-17 — bounce and complaint are separate signals, not one combined "spam rate", and both require a real sample before acting:**

The first production batch (10 sent, 3 bounced — all dead/typo'd domains a DNS lookup would have caught) tripped the breaker on a sample of 10, using the original combined-rate design above. Two real problems, not one:

1. **A hard bounce and a spam complaint are different severities of the same thing.** A complaint is a person marking real, delivered mail as spam — a direct reputation hit. A bounce is often just a bad address (typo, defunct domain, an AI-discovery miss) — a data-quality problem, not a "this domain sends spam" signal, though ISPs do still weight it somewhat. Folding both into one 0.1% trigger meant bounce noise could trip the breaker on data quality alone.
2. **0.1% of a 10-send sample is 0 or 1 events — not a rate, a coin flip.** Section 5b already establishes 30+ outcomes as the bar for trusting a per-factor rate over noise; the circuit-breaker signal needs the same bar and didn't have one.

**What actually shipped:** `complaint_rate` keeps the 0.1% trigger (true spam signal). `bounce_rate` gets its own, higher trigger (5% — in line with typical ESP guidance that under 2% is healthy, 5%+ is a real problem) and is tracked/trips independently. Neither is evaluated below 30 sends in the trailing 7-day window (`MIN_EMAIL_SAMPLE_SIZE`) — below that, the signal is `None` and the ramp holds flat, same as the existing "not enough data" behavior. **Pre-send mitigation, not just a better trigger:** `outreach/email_verify.py` now does a free DNS MX/A-record lookup before an address is ever accepted by discovery (`outreach/email_discovery_job.py`, `outreach/apply_result.py`) and again right before send (`outreach/send_job.py`) — catching the exact class of dead-domain bounce that caused the first incident before it ever costs a send, rather than only reacting to it after the fact. Not a full mailbox-existence check (no SMTP RCPT probing — unreliable and can itself look like abuse); it only confirms the domain has somewhere to route mail.

**Recovery, previously a known gap ("holds a tripped breaker at the floor indefinitely," Section 15) — now built.** `RampState.consecutive_clean_days` tracks how many consecutive nightly checks, while tripped, saw both rates clean at real sample size; a breach resets it to 0; reaching 7 clears the trip and resumes ramping from the floor. Because both rates are trailing-7-day windows, a single bad day naturally takes about a week to age out before the clean-day count can even start climbing — recovery from a real trip realistically takes noticeably longer than 7 days end-to-end, which is expected trailing-window behavior, not a bug.

### SMS circuit-breaker

- **Monitor:** Twilio delivery receipts (delivery rate as a rolling daily %) and STOP reply rate
- **Trigger:** delivery rate drops more than 10 percentage points from the prior-week baseline, OR opt-out rate spikes above 2% in a single day
- **While triggered:** pause SMS sends; do not fall back to email (the channels are independent — a degraded SMS channel is not a reason to push more email volume)
- **Recovery:** delivery rate returns to within 5 percentage points of baseline for 5 consecutive days; opt-out rate flat or falling. Resume SMS from the week-1 floor of the SMS ramp (Section 15).

---

## 11. Follow-Up Sequence

Implemented in `outreach/followup.py` (trigger + channel logic) and `outreach/templates.py` (copy). Triggered by a nightly job checking each active prospect's `funnel_substage` + days since `last_touch_at`. Hard cap: max 4 total touches per prospect (initial + 3 follow-ups). Never touch again after payment, unsubscribe, or reply.

`last_touch_at` is updated both when a touch is sent and on every `funnel_substage` transition (sent → opened → clicked_generated → account_created) — so "days since `last_touch_at`" always means "days since this prospect's last state-changing event," which is what the timing rules below key off.

### Timing rules (checked nightly, per active prospect)

| `funnel_substage` | Condition | Fires |
|---|---|---|
| `sent` | no open, 4 days since sent | Stage A |
| `opened` | no click, 4 days since opened | Stage B |
| `clicked_generated` | no payment, 3–4 days since click | Stage C |
| `account_created` | no payment, 2–3 days since account creation | Stage D |
| *(any)* | 14–21 days since first send, still unpaid | Final catch-all touch (reuses the copy for the prospect's current substage), then `funnel_substage = cold`, stop |

### Channel logic

- **`has_findable_email = true`, not opted out of either channel:** send BOTH the email and SMS version of the matching stage, same day — unless `sms_opted_out` or `email_opted_out` individually excludes one (an opt-out on one channel never suppresses the other).
- **`has_findable_email = false`** (phone-only, no email ever found): SMS only, and Stage A and Stage B collapse into a single pre-click follow-up — SMS has no "opened" tracking, so only "sent" and "clicked" are knowable for this segment. Stage A's copy is reused as that single pre-click follow-up.

> **Schema note:** `has_findable_email` is the same field as `email_found` (Section 13) — no separate column. `email_opted_out`/`sms_opted_out` are the same fields as `email_unsubscribed`/`sms_unsubscribed` (Section 13) — kept under their original column names to avoid duplicating existing, already-wired columns; "opted out" and "unsubscribed" mean the same thing here.

### Volume accounting

Follow-ups are first-priority consumers of each day's ramp-approved total (Section 15) — not additional on top of the deliverability ceiling. `run_followups()` takes the day's full remaining ramp allowance, queues due touches against it (most-overdue prospects first if the ramp runs out partway through), and returns whatever's left for the initial-send job to fill with new sends. **The ramp ceiling itself (spam-rate tracking, circuit-breaker state) isn't computed anywhere in code yet** — only described in Section 15 — so `run_followups()` currently takes that number as a plain argument; wire it to a real computation once Section 15's tracking is built.

### Content accuracy rule

Stage A and Stage B (pre-click — `sent`/`opened` substages) must never claim the site is already built. Only Stage C and Stage D (post-click — `clicked_generated`/`account_created` substages) may say that, since generation only happens after a real click. This is enforced structurally in `outreach/templates.py` (`PRE_CLICK_STAGES`/`POST_CLICK_STAGES`) — do not paraphrase this distinction away when editing copy.

### Finalized follow-up copy (implemented in `outreach/templates.py`)

**Stage A — email** (sent, never opened):

> Subject: `{business_name} — did this land?`
>
> Hi {business_name} team,
>
> Quick follow-up in case this got missed — click below and we'll build a free website preview for {business_name}, no cost:
>
> {preview_link}
>
> Any questions, just reply to this email.
>
> ---
> Don't want emails like this from us? Reply STOP or click here to unsubscribe: {unsubscribe_link}

**Stage A — SMS** (also the collapsed A/B stage for phone-only prospects):

> Hi {business_name}, quick follow-up in case this got missed: groundworkbuild.com/s/{short_code}
> Reply STOP to opt out.

**Stage B — email** (opened, never clicked):

> Subject: `Still there — {business_name}'s website preview`
>
> Hi {business_name} team,
>
> Following up on your free website preview — click below and we'll build it for {business_name} right there, no cost:
>
> {preview_link}
>
> Any questions, just reply to this email.
>
> ---
> Don't want emails like this from us? Reply STOP or click here to unsubscribe: {unsubscribe_link}

**Stage B — SMS:**

> Hi {business_name}, quick follow-up — click below and we'll build a free website preview for your business: groundworkbuild.com/s/{short_code}
> Reply STOP to opt out.

**Stage C — email** (clicked, site generated, no payment):

> Subject: `Your website's ready, {business_name}`
>
> Hi {business_name} team,
>
> Just checking in — your website's built and waiting:
>
> {preview_link}
>
> First month's free if you'd like to go live — £99 setup + £24.99/month after.
>
> Any questions, just reply to this email.
>
> ---
> Don't want emails like this from us? Reply STOP or click here to unsubscribe: {unsubscribe_link}

**Stage C — SMS:**

> Hi {business_name}, your website's built and waiting — have a look: groundworkbuild.com/s/{short_code}
> First month's free if you'd like to go live.
> Reply STOP to opt out.

**Stage D — email** (account created, no payment):

> Subject: `One step left, {business_name}`
>
> Hi {business_name} team,
>
> Your account's set up and your site's ready to go — just needs switching on:
>
> {preview_link}
>
> First month's free, £99 setup + £24.99/month after.
>
> Any questions, just reply to this email.
>
> ---
> Don't want emails like this from us? Reply STOP or click here to unsubscribe: {unsubscribe_link}

**Stage D — SMS:**

> Hi {business_name}, your account's set up and your site's ready — just need to go live: groundworkbuild.com/s/{short_code}
> First month's free, cancel anytime after.
> Reply STOP to opt out.

---

## 11a. Reply Capture (kill-switch) — blocks the sequence going live

On any reply, regardless of channel:

- **Stop-intent keyword** (STOP, UNSUBSCRIBE, CANCEL, QUIT, etc. — case-insensitive, see `outreach/reply_handling.py:STOP_KEYWORDS`) → permanent opt-out on that channel (`sms_unsubscribed` / `email_unsubscribed`, each stamped with a `*_unsubscribed_at` timestamp).
- **Any other reply** → `funnel_substage = "replied"`, which pauses the sequence — this value is deliberately outside the set `run_followups()` queries against, so a replied prospect is automatically excluded without a separate check. A human should look at these, not keep getting scripted follow-ups.

Matching logic lives in `outreach/reply_handling.py` (`handle_inbound_sms` / `handle_inbound_email`), shared across channels so channel-specific transports only need to call into it.

### SMS — built via Esendex webhooks (reworked from an earlier Twilio-based build — see Section 10a's "Provider correction")

`/api/webhooks/sms-inbound` (`app.py`) parses Esendex's webhook event array, routing `eventId: "inbound"` to `handle_inbound_sms` and `eventId: "stop"` to the new `handle_forced_sms_stop` (Esendex's own opt-out classification, honored directly rather than re-run through keyword matching). Secured by a shared-secret query parameter (`?secret=...` vs. `ESENDEX_WEBHOOK_SECRET`) rather than a signature scheme — Esendex has no documented HMAC equivalent to Twilio's `X-Twilio-Signature`. Fails closed if unset. See Section 10a for exactly which parts of this are confirmed against Esendex's docs versus defensively best-effort (the event envelope shape is confirmed; exact field names inside individual events' `data` are not).

### Email — built via Cloudflare Email Routing → Email Worker

Resend has no inbound-parsing product (confirmed directly — its product is outbound send plus webhooks about your own outbound sends), so this uses the Cloudflare option from the three previously reported: `frontend/_worker.js` (the existing, already git-linked/auto-deployed `groundwork` Worker — not a new project) now also exports an `email(message, env, ctx)` handler alongside its existing `fetch()` handler. On every inbound message, once a Cloudflare Email Routing rule points at this Worker (see "what's still on you" below), it:

1. Parses the raw MIME message into a plain-text body. **Deliberately dependency-free** — no `postal-mime` or similar — since this project has zero npm dependencies today (no `package.json`) and introducing one purely for this would add real risk to a deploy pipeline untestable from here. Handles the common case (single-part, or two-part `multipart/alternative` with `text/plain`+`text/html`, from Gmail/Outlook/Apple Mail), including quoted-printable/base64 decoding. **Not** a fully spec-compliant MIME parser — nested `multipart/mixed` with attachments or unusual charsets aren't handled. **Reviewed carefully but not executable-tested** — no Node/JS runtime was available in this environment to actually run it, only Python. Verify it against a real test reply before relying on it (see the test-send tool, Section 18).
2. POSTs `{from, text}` to `/api/webhooks/email-inbound` (`app.py`), authenticated with a shared secret (`X-Groundwork-Email-Secret` header vs. `EMAIL_INBOUND_SHARED_SECRET`) — Cloudflare Email Workers have no built-in request-signing equivalent to Twilio's, so a shared secret is the mechanism here. Fails closed (503) if the secret isn't set on the Railway side.
3. `email_inbound_webhook` calls `handle_inbound_email` — the same shared reply_handling logic SMS already uses.

**Replies land on the root domain, not the sending subdomain — deliberately.** `send_outreach_email` (`emails.py`) now sets an explicit `reply_to` (confirmed via a live test send that Resend's API genuinely supports a `reply_to` distinct from `from` — a real send succeeded with `from: hello@groundworkbuild.com` + `reply_to: reply@groundworkbuild.com` as two different addresses), defaulting to `OUTREACH_REPLY_TO_EMAIL` (set in Railway to `reply@groundworkbuild.com`), while `from` stays `hello@mail.groundworkbuild.com` (`OUTREACH_RESEND_FROM_EMAIL`, unchanged). This was a direct fix for a real dashboard limitation: Cloudflare's guided Email Routing rule-builder's domain picker **only offers the root zone**, not arbitrary subdomains — confirmed, not assumed — so a subdomain-specific routing rule for `mail.groundworkbuild.com` wasn't something the guided UI could actually do. Routing replies to an address on the root domain instead sidesteps that entirely. This is also the more correct design regardless of the dashboard limitation: replies aren't bulk/cold sends, so they don't carry the sending subdomain's reputation risk — there was never a real reason for them to land on the isolated subdomain in the first place.

**What's still on you (dashboard/account steps, not code):**

1. Enable Cloudflare Email Routing for `groundworkbuild.com` (the root domain — no subdomain-specific setup needed now).
2. Add a routing rule for `reply@groundworkbuild.com` (or a catch-all on the root, if preferred) → "Send to a Worker" → the existing `groundwork` Worker.
3. Set `EMAIL_INBOUND_SHARED_SECRET` as a Cloudflare Worker secret (`wrangler secret put`, or the dashboard's Variables/Secrets UI) **and** as a Railway env var, to the same value.
4. Set `RESEND_WEBHOOK_SECRET` similarly if not already done for Section 15's bounce/complaint webhook (see below) — separate secret, same idea.

**Still outstanding, unrelated to this fix:** `mail.groundworkbuild.com` (the `from` address) is not yet added/verified in Resend — confirmed via a live test send that failed with Resend's own "domain is not verified" error. Outreach email sends won't succeed until that's done (Resend dashboard → add domain → its SPF/DKIM/DMARC DNS records → wait for verification), independent of the reply-routing fix above.

**A known fidelity gap, not a bug:** `is_stop_intent()` (reused as-is, per instruction) does an exact match against the whole normalized message body — tuned for SMS's typical one-word replies. A verbose email reply ("Please stop emailing me. On Mon, ... wrote: > ...") won't match it, so it falls through to the "any other reply" branch instead of the stricter permanent-opt-out branch. This isn't dangerous — every reply still either opts out or pauses the sequence, never gets ignored — but a real "please stop" email won't auto-suppress future sends the way a bare "STOP" SMS does; a human reviewing "replied" prospects would need to notice and manually opt them out. Worth knowing before assuming stop-detection is equally reliable on both channels.

### The `EMAIL_REPLY_CAPTURE_READY` / `SMS_REPLY_CAPTURE_READY` flags — confirmed scope

**Per-channel, not a single combined flag** — `run_followups()` (`outreach/followup.py`) checks two independent booleans, one per channel, so email follow-ups can run as soon as email's flag is true without waiting on SMS (and vice versa). `_fire_touch()` gates each channel's send individually against its own flag. Flip a channel's flag to `true` only once that channel's reply-capture is actually deployed **and verified end-to-end** — a real inbound reply (e.g. via the test-send tool, Section 18) observed to actually opt-out/pause a real test prospect on that channel, not just once the code merges.

**A related gap, flagged rather than silently fixed:** these flags only gate `run_followups()` (scheduled follow-up touches) — they do **not** gate `fill_initial_sends()` (`outreach/send_job.py`), which sends initial emails/SMS regardless of either flag's value. A reply to an *initial* touch is just as much a signal a human should see as a reply to a follow-up, and initial sends can legitimately get replies too. Whether to also gate initial sends behind these flags is a real decision, not applied here without confirming first.

---

## 11b. One-click unsubscribe

Built and tested (`app.py:unsubscribe`, `outreach/send_job.py`, `emails.py`):

- **`/unsubscribe/<token>`** — reuses the same per-prospect `token` `/claim/<token>` uses (same prospect identity either way; a second token wasn't introduced for this). `GET` (the "click here to unsubscribe" text link in the email body) sets `email_unsubscribed = true` + `email_unsubscribed_at` immediately, no login, no confirmation click, then shows a plain landing page. `POST` (what a mail client sends automatically when it honors the `List-Unsubscribe-Post` header, RFC 8058) does the same thing and returns a bare 200, no page. An unknown/missing token gets the identical response either way — doesn't leak whether a token is valid.
- **`{unsubscribe_link}` resolves correctly everywhere it's used** — `outreach/send_job.py:_unsubscribe_link` and the identical function threaded through `outreach/followup.py` both build `{BASE_URL}/unsubscribe/{p.token}`; every template (Section 10/11) that renders `{unsubscribe_link}` gets this real URL.
- **`List-Unsubscribe`/`List-Unsubscribe-Post` headers** — already present on every `send_outreach_email` call (`emails.py`), pointing at the same URL passed as `unsubscribe_link`. No template/caller had to change for this.
- Verified: `GET` unsubscribes and shows the landing page; `POST` (the one-click header path) returns 200 with an empty body and has the same effect; an unknown token returns the same response as a valid one.

---

## 12. QA Gate (standing process, not one-time)

- Every template — all initial + all follow-up variants — sent to groundwork-build@outlook.com populated with real generated data (actual site, actual GIF, actual working link) before going live
- Check rendering across Gmail web, Outlook, and mobile
- Re-run this gate any time a template is edited (ties into the weekly Friday/Monday review cycle — new variants go through QA before replacing the live version)

---

## 13. Database Schema Additions (prospects table)

| Field | Type | Notes |
|---|---|---|
| `business_name`, `trade`, `trade_tier` | String | |
| `location`, `postcode_area` | String | |
| `rating`, `review_count`, `business_status` | Float/Int/String | From Places API |
| `google_photos_count` | Integer | Photo count from Places API — proxy for how professionally the business presents itself online |
| `opening_hours_complete` | Boolean | Whether Places API returns complete opening hours — signal of profile investment |
| `website_status` | String | `no_website` / `has_website` — set for free, directly off Places' own website field, at sourcing time (`outreach/pipeline.py`). No screenshot or Cowork vision judgment call. Legacy rows may still carry `has_website_dated` / `has_website_modern` from before this change; `outreach/scorer.py` still scores those values, they're just no longer written going forward. |
| `website_quality` | String | `modern` / `dated` / `unreachable` / null (not checked) — free code-only staleness heuristic for `has_website` prospects, computed once at sourcing time (`outreach/email_scrape.py:fetch_and_assess_quality`, added 2026-07-18 alongside the Section 5 scoring redesign). Read by `outreach/scorer.py`, never fetched live. |
| `vision_flag_layout` | Boolean | Unused since the vision-judgment step was replaced by the binary website check above. Column kept on the schema for potential future use, not populated by the current pipeline. Vision checklist item 1 (fixed/non-responsive layout) |
| `vision_flag_design` | Boolean | Unused, see above. Vision checklist item 2: outdated design cues |
| `vision_flag_cta` | Boolean | Unused, see above. Vision checklist item 3: no clear call-to-action |
| `vision_flag_content` | Boolean | Unused, see above. Vision checklist item 4: stale content |
| `vision_flag_reviews` | Boolean | Unused, see above. Vision checklist item 5: no reviews shown despite having Google reviews |
| `vision_flag_load` | Boolean | Unused, see above. Vision checklist item 6: fails to load / times out |
| `score` | Float | 0–100 |
| `email` | String | |
| `email_source` | String | `facebook` / `checkatrade` / `yell` / `directory` / `web_search` |
| `email_domain_type` | String | `business` (domain matches trade name or is a custom domain) / `personal` (gmail, hotmail, yahoo, etc.) — affects predicted responsiveness |
| `email_found` | Boolean | Also serves as `has_findable_email` (Section 11) — same field, no separate column |
| `phone` | String | From Places API — no separate discovery needed |
| `competitor_density` | Integer | Count of same-trade prospects already sourced from this postcode area — high density may dilute response rate in a local market |
| `token` | String | UUID for magic link (`/claim/<token>`). Generated at send-queue time by `outreach/link_identity.py`, not before |
| `short_code` | String | Short code for `/s/<short_code>` (Section 9a). Generated alongside `token`, same helper |
| `lead_id` | Integer (FK) | Set on first successful claim — links this prospect to the `Lead`/`Generation` its magic link produced |
| `account_created_at` | DateTime | |
| `screenshot_url`, `gif_url` | String | |
| `funnel_stage` | String | `sourced` / `queued` / `awaiting_approval` / `qualified_no_email` / `unreachable` (no findable email AND no phone — surfaced as a filterable category in the Tinder review UI rather than dropped/silently logged) / `sent` / `opened` / `clicked` / `paid` / `cold` |
| `funnel_substage` | String | Follow-up sequence state (Section 11): `sent` / `opened` / `clicked_generated` / `account_created` / `cold`. Distinct from `funnel_stage` — `funnel_substage` only exists to drive `outreach/followup.py`'s timing rules. |
| `last_touch_at` | DateTime | Updated on every touch sent AND on every `funnel_substage` transition — the anchor for Section 11's "days since" timing rules |
| `approval_status` | String | Retained for audit trail; no longer a send gate |
| `approved_at` | DateTime | |
| `email_version_sent`, `sms_version_sent` | String | |
| `touch_count` | Integer | |
| `discount_code`, `discount_expiry` | String/DateTime | |
| `sent_at`, `opened_at`, `clicked_at`, `paid_at` | DateTime | |
| `sent_at_dow` | Integer | Day-of-week of first send (0=Mon … 6=Sun) — for correlation analysis |
| `sent_at_hour` | Integer | Hour-of-day of first send (0–23, UTC) — for correlation analysis |
| `sms_sent_at` | DateTime | |
| `sms_delivered` | Boolean | |
| `email_unsubscribed` | Boolean | Tracked separately from SMS. Also serves as `email_opted_out` (Section 11) — same field |
| `sms_unsubscribed` | Boolean | Tracked separately from email. Also serves as `sms_opted_out` (Section 11) — same field |

---

## 14. Build Order

Two tracks run in parallel — sourcing/approval has no dependency on sending infrastructure.

### Track A — Sourcing (start immediately)

1. Google Cloud billing + Places API (Enterprise tier) — quick setup
2. Build: sourcing → gating → website check → scoring → email discovery → database schema → Admin Dashboard (Section 17)
3. Run Task A once (manually is fine initially) to seed the first batch — build a qualified backlog ready to go the moment sending is live
4. Once comfortable, wrap Task A as a proper nightly schedule

### Track B — Sending Infrastructure (start immediately, runs in background)

1. Cloudflare subdomain + SPF/DKIM/DMARC records — add now, let DNS propagate (24–48hrs)
2. Resend account set up against the subdomain
3. Esendex (or Plivo) account + start UK sender ID / SMS registration — longest lead time, start early
4. Google Postmaster Tools + Microsoft SNDS registration
5. Build: generation hookup → GIF capture → magic link/auth → email + SMS templates → follow-up sequence logic → circuit-breaker (Section 10b)
6. QA Gate (Section 12): test-inbox sends + spam-score checks on every template until approved

### Merge point

Once Track B is ready (DNS clean, templates QA-passed) — Task B goes live pulling directly from the qualified backlog Track A has already been building. The first real send starts with the top-scored prospects already accumulated, ordered by composite score (and later predicted conversion, per Section 5b).

---

## 15. Deliverability

The biggest risk to this pipeline is emails landing in spam, not generation cost.

### Implementation status — checked directly, not assumed

- **SMS delivery-status data: WIRED, via Esendex — as a poll, not a push.** No Twilio account exists (see Section 10a's "Provider correction"). `outreach/sms_status_poll.py` polls Esendex's Message Headers API for any recently-sent message not yet in a terminal status, logging changes to `SmsDeliveryEvent` — the same table, same schema, same downstream consumer (`get_health_signal`) as before the provider switch. **Needs to actually be scheduled to run periodically** (hourly is reasonable) — nothing currently invokes it automatically.
- **Resend bounce/complaint webhooks: WIRED**, but needs a dashboard step. `app.py:resend_events_webhook` (`/api/webhooks/resend-events`) verifies Resend's Svix-signed payloads (the `svix` package, not hand-rolled HMAC) and logs each event to a new `EmailEventLog` row. **This requires a webhook actually registered in the Resend dashboard** pointing at that URL, and `RESEND_WEBHOOK_SECRET` set to match the signing secret Resend gives you when you create it — a one-time dashboard action, same category as the Cloudflare Email Routing rule in Section 11a.
- **Google Postmaster Tools API access:** still NOT wired — needs manual domain verification in the Postmaster dashboard first (postmaster.google.com, DNS TXT record), which is a human step, not a code change. Email health below is computed entirely from the Resend interim proxy this section already names for it — not blocked on Postmaster.

`outreach/ramp.py:get_health_signal(channel)` now computes real rates from the above:

- **email:** `bounce_rate` and `complaint_rate` computed **separately** (`EmailEventLog` ÷ `DailySendCount`, trailing 7 days each) — revised 2026-07-17, see Section 10b's email circuit-breaker for why these were split and why a 30-send minimum sample now gates evaluation at all (previously any nonzero send count qualified).
- **sms:** delivered ÷ total distinct `message_sid`s (`SmsDeliveryEvent`) for the trailing 7 days (`delivery_rate`) vs. the 7 days before that (`delivery_rate_baseline`, what the circuit-breaker trigger compares against per Section 10b), plus today's opt-outs (`Prospect.sms_unsubscribed_at`) ÷ today's sends (`opt_out_rate_today`).
- Returns `None` ("unknown") whenever there isn't yet enough real data in the window — the ramp **holds flat rather than advancing on missing/insufficient data**. For email this is now "fewer than 30 sends in the trailing window," not just "zero."

**Recovery — was a known gap ("holds a tripped breaker at the floor indefinitely"), now built.** `RampState.consecutive_clean_days` (new column, `outreach/ramp.py`) counts consecutive nightly checks, while tripped, where both rates came back clean at real sample size; any breach resets it to 0; reaching 7 clears the trip and resumes the ramp from the floor. See Section 10b for the full account, including why trailing-window recovery realistically takes longer than 7 days end-to-end.

### Dynamic send ramp (email)

No fixed ceiling. Volume compounds weekly as long as email health metrics stay clean. Reconciled
2026-07-20 to match `EMAIL_RAMP_TABLE` in `outreach/ramp.py` — this table used to describe 5–10/
15–25/30–50 ranges that the code never actually implemented (it always used flat numbers); this is
now the real, currently-running schedule, not an aspirational one:

| Period | Daily email volume |
|---|---|
| Week 1 (floor) | 20/day |
| Week 2 | 25/day |
| Week 3 | 50/day |
| Week 4+ | double the prior week's volume each week (100, 200, 400, ...) |

**Advancement trigger:** evaluated nightly (`advance_or_hold()`, called at the start of every
send-job-cron run) against the trailing-7-day `bounce_rate` and `complaint_rate` from
`get_health_signal("email")` — not Postmaster Tools, which still isn't wired (see "Implementation
status" above); Resend's webhook-derived `EmailEventLog` data is the real signal in use today.
Below `MIN_EMAIL_SAMPLE_SIZE` (30 sends in the trailing window), the signal is `None` and the ramp
holds flat rather than advancing on insufficient data. Advances to the next week's volume once a
full 7 days have elapsed at the current week's rate with no breach. **Circuit breaker:** any single
day where `complaint_rate >= EMAIL_SPAM_RATE_TRIGGER` (0.1%) or `bounce_rate >= EMAIL_BOUNCE_RATE_TRIGGER`
(5%) trips immediately — no "hold flat and retry" grace period — resetting straight to the week-1
floor (20/day) and `week_number = 1`. Recovery requires `CIRCUIT_BREAKER_RECOVERY_DAYS` (7)
*consecutive* clean days while tripped (any breach during recovery resets the counter to 0); because
both rates are trailing-7-day windows, a single bad day takes about a week to fully age out before
the clean-day count can even start climbing, so real recovery end-to-end is meaningfully longer than
7 days. See Section 10b for the split-trigger rationale and the pre-send MX/A-record mitigation that
now runs before this ever needs to trip.

**Drip, not batch.** Spread the day's send allowance across the day rather than firing all at once — a sudden burst from a new domain is itself a spam signal. Space sends at randomised intervals across the waking hours.

### Dynamic send ramp (SMS)

Parallel track, independent of email:

| Period | Daily SMS volume |
|---|---|
| Week 1 | 10–20/day |
| Week 2 | 30–50/day |
| Week 3+ | increase by 50–75% per week |

**Advancement trigger:** delivery rate high (>90%) and opt-out rate low and flat vs prior week. Circuit-breaker (Section 10b) applies independently.

### Other deliverability requirements

1. **SPF/DKIM/DMARC must all pass and align.** Start DMARC at `p=none` (monitoring only) to catch misconfigurations without risking blocked mail, tighten later once confirmed clean.
2. **Healthy text-to-image ratio.** An email that's mostly a GIF with minimal real text is a classic spam signal — ensure genuine substantial body copy alongside the GIF.
3. **Cautious with sales-trigger language** in the initial cold email — discount/urgency phrasing is more spam-filter-sensitive on a first touch than in follow-ups to someone who's already engaged.
4. **Keep bounce rate near-zero.** A bounce damages sender reputation more than almost anything else — another reason the "never guess an email" rule from Section 4 matters for deliverability, not just accuracy.
5. **Monitor reputation directly:** register the sending domain with Google Postmaster Tools (free) and Microsoft SNDS (free) to see spam-rate and reputation data directly.
6. **Extend the QA Gate** (Section 12) to include a spam-score check — a free tool like mail-tester.com sends back a spam score and specific flagged issues before a template goes live.
7. **Unsubscribe link must be present and honored immediately** — required for compliance, and reduces spam complaints.
8. **SMS deliverability:** use a UK-registered sender ID via a reputable provider (Esendex/FireText/Plivo) rather than the cheapest "grey route" option. Honor STOP replies immediately.

---

## 17. Admin Dashboard

A single internal tool consolidating every monitoring and decision touchpoint in the pipeline:

1. **Funnel dashboard** — sent/opened/clicked/paid by channel (email/SMS) and by template version, feeding the Friday review
2. **Daily correlation view** — for each captured prospect attribute (see Section 13), shows click rate and paid rate broken down by factor tier. Examples: click rate for `no_website` vs `has_website_dated`; by `trade_tier`; by rating band; by `email_domain_type`; by `competitor_density` bucket; by `sent_at_dow`/`sent_at_hour`. Only displayed once a factor-tier has reached the minimum sample size (30+ outcomes, per Section 5b). Computed nightly — shows yesterday's snapshot each morning. Read-only: decisions about acting on divergences happen in the Friday review.
3. **Scoring reweight suggestions** (Section 5b) — weekly flagged divergences between predicted and actual click/paid rates, presented for the user to approve or dismiss, not auto-applied
4. **Ramp & circuit-breaker status** (Section 15) — current daily send allowance for email and SMS, current week's spam-rate and delivery-rate, circuit-breaker state (open/closed per channel)
5. **Template QA status** (Section 12) — which email/SMS variants have passed test-inbox + spam-score checks and are eligible to go live
6. **Outreach queue** (Section 5a) — monitoring view: prospects pending vision/email checks, recently sent, and the exploration-slice proportion of recent sends

Build the funnel dashboard and ramp status first — they're needed from day one of sending. The correlation view and reweight suggestions can be added once enough outcomes exist to populate them.

---

## 16. Working Assumptions (for tracking against real data)

| Metric | Assumption |
|---|---|
| Overall conversion (sent → paid) | 0.475% |
| Monthly churn | 5% |
| Volume at end of month 3 (if ramp holds clean) | ~1,200–1,800 sends/month email + SMS |
| Projected MRR at month-3 volume | ~£135–£200/mo (0.475% conversion, 5% churn) |
| Levers for faster growth | Healthy ramp (spam rate stays clean), improve conversion, reduce churn |

---

## 18. Manual Test Send

`outreach/send_test.py` — sends the real initial template through the real pipeline (`ensure_link_identity`, `render_email`/`render_sms`, `send_outreach_email`/`send_outreach_sms`, the same funnel-state updates the batch job makes) to exactly one prospect, independent of `send_job.py`'s ramp/eligibility logic — **not** ramp-limited, **not** gated by anything else in the batch:

```
python -m outreach.send_test --prospect-id 42
python -m outreach.send_test --business-name "Test Roofing Ltd" --email you@example.com
python -m outreach.send_test --business-name "Test Roofing Ltd" --phone "+447900000000"
```

A real send — costs a real Resend/Twilio send just like production. Prints the resulting `/claim/<token>` (and `/s/<short_code>`) link so you can immediately click through the full claim → generation → preview loop yourself.

**A real bug found and fixed while building this:** `outreach/ramp.py:record_sends()` used to always open its own DB session, which caused genuine `"database is locked"` failures under SQLite when called from within a caller (`send_job.py`, `followup.py`) that already had a session open mid-transaction — reproduced directly while testing this tool, not theoretical. Fixed by having `record_sends()` accept and reuse the caller's session (`db=` parameter), with every real call site updated to pass theirs through.
