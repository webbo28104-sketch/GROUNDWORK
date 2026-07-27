# Groundwork — project overview

Groundwork generates AI-built marketing websites for UK trades businesses. A user fills in an 8-step form; the submission is gated behind email verification before any Claude API call fires; once verified, the Flask backend calls the Anthropic API, and the generated single-file HTML is persisted to Postgres and served (watermarked, noindex) as a direct link the user opens in a new tab.

## Architecture

| Layer | Technology | Host |
|---|---|---|
| Backend API | Flask (`app.py`) | Railway |
| Database | Postgres via SQLAlchemy (`models.py`) | Railway |
| Email | Resend (`emails.py`) | — |
| Frontend | Static HTML + vanilla JS (`frontend/`) | Cloudflare Workers (Static Assets), git-linked, worker name `groundwork` |
| AI generation | Anthropic API via `build_prompt.py` | — |

## Key environment variables

- `ANTHROPIC_API_KEY` — must be set in Railway. Never hardcode.
- `DATABASE_URL` — Postgres connection string, set automatically by Railway's Postgres plugin. Falls back to a local `sqlite:///local_dev.db` if unset (dev only).
- `SECRET_KEY` — signs Flask sessions and magic-link tokens (`itsdangerous`). Must be set in production — the code falls back to an insecure dev default otherwise.
- `RESEND_API_KEY` / `RESEND_FROM_EMAIL` — for verification and resend emails via Resend. If `RESEND_API_KEY` is unset, sends are skipped and logged instead of failing. `RESEND_FROM_EMAIL` must be an address on a domain verified in Resend (DNS-verified) or sends will fail even with a valid API key.
- `ADMIN_USERNAME` / `ADMIN_PASSWORD` — credentials for `/admin/login`. Unset means admin login always fails.
- `PORKBUN_API_KEY` / `PORKBUN_SECRET_KEY` — Porkbun API credentials for the `/api/domain/search` endpoint. If unset, the endpoint returns an empty results array.
- `CLOUDFLARE_API_TOKEN` / `CLOUDFLARE_ZONE_ID` / `CLOUDFLARE_CNAME_TARGET` — Cloudflare for SaaS credentials used to connect a customer's purchased domain (see "Custom domain automation" below). `CLOUDFLARE_CNAME_TARGET` defaults to `connect.groundworkbuild.com`.
- `IP_RATE_LIMIT_PER_HOUR` — max form submissions per IP per hour (default `5`).
- `PORT` — set automatically by Railway.
- `OUTREACH_API_TOKEN` / `OUTREACH_API_TOKEN_GET` — bearer tokens for the outreach judgment API (see "Outreach pipeline" below). Two separate tokens so either can be rotated independently. Unset means those endpoints always 401.

## The generation flow (gate → verify → generate → store → retrieve)

1. **Build form** (`frontend/build.html`) — 8-step vanilla JS form. On step 8 submit, sends `multipart/form-data` to `POST /api/generate`. Fields: `business_name`, `trade`, `location`, `coverage_area`, `phone`, `email`, `commercial_split` (0–100, commercial share), `work_type` (standard/mix/bespoke), `team_size` (sole/small/company), `large_contracts` (yes/no), `urgency` (ahead/emergency), `years_trading`, `accreditations`, `past_clients`, `notes`. Logo as file field `logo`; portfolio photos as multiple file field `photos`.

2. **`POST /api/generate`** (`app.py`) — does *not* call Claude. It:
   - blocks the request (409) if this email already has a `Generation` row (repeat-generation guard),
   - rate-limits by IP (429 past `IP_RATE_LIMIT_PER_HOUR`),
   - creates a `Lead` row (or reuses an existing unverified one from the same email within 24h, to avoid duplicate rows / spam on resubmit), saving the mapped form data as JSON and any uploaded logo/photos to `uploads/<lead.public_id>/`,
   - signs a 24h token (`itsdangerous.URLSafeTimedSerializer`) encoding the lead id, emails a verification link via Resend, and returns `{"status": "check_email"}`.
   - Frontend redirects to `check-email.html?email=...`.

3. **`GET /verify/<token>`** — validates signature + 24h expiry. Invalid/expired → redirects to `verify-error.html?reason=invalid|expired`. Valid → marks the lead verified, rebuilds the prompt from the stored form data, and starts the same background-thread Claude call as before, keyed by `lead.public_id` (reused as the job id everywhere downstream) — then redirects to `loading.html?id=<public_id>`. If the lead already has a generation (token reused/idempotent), redirects straight to `preview.html?id=<public_id>`.

4. **Loading page** (`frontend/loading.html`) — unchanged: polls `GET /api/generate/<id>/status` every 2s, redirects to `preview.html?id=<id>` on `"done"`.

5. **Preview page** (`frontend/preview.html`) — "View your website →" link opens `GET /api/generate/<id>/html` in a new tab (watermarked + noindex via `_inject_watermark()`, injected on the fly — stored HTML is never modified). "Go live" links to `checkout.html`.

6. **Persistence** — once a background generation finishes, `_run_and_persist()` writes a `Generation` row (`lead_id`, `email`, `business_name`, `html_content`, `status="draft"`) *before* anything else happens — the DB row is the source of truth, not the in-memory `_jobs` dict or any email. `job_status`/`job_html` check `_jobs` first (for live progress) and fall back to querying `Generation` by `lead.public_id`, so links keep working even after a process restart wipes `_jobs`.

7. **My Account — email + password, real sessions** (`/account/login`, `/account/verify/<token>`, `/account`). Server-rendered in `app.py`, styled to match the rest of the funnel via `_account_page()`/`_SITE_HEADER`/`_SITE_FOOTER` (no shared CSS file in this repo). `/account/login` is a single entry point: submit an email (`stage=email`) and the server branches three ways —
   - **Account already has a password** → password-login form (`stage=password`). Wrong password → clean inline error, no session set, no other enumeration hint.
   - **No password yet, but this email has at least one `Generation`** → straight to a "choose a password" form (`stage=set_password`), no re-verification email — we already know the address is real, since they clicked a verification link to generate their original site. Covers every account created before this change.
   - **No password and no `Generation` at all** (brand new signup) → signs a 24h token (`{"signup_email": email}`) via `itsdangerous`, emails a link via Resend (`send_resend_email`, reused as-is) to `/account/verify/<token>`, which — once clicked — shows the same "choose a password" form.
   Setting a password (`stage=set_password`) hashes it with `werkzeug.security.generate_password_hash` (min 8 chars) and upserts an `Account` row (see schema below), then sets `session["account_email"]` — the exact same Flask-session pattern `/admin/login` already uses (`session["is_admin"]`), not a second mechanism. `/account` (plain URL, no token) is the session-gated dashboard; `/account/logout` clears the session. `GET /api/account/session` returns `{"logged_in": bool, "email": ...}` for `build.html` to check on load.

   **Effect on the public form:** `frontend/build.html` calls `/api/account/session` on load; if logged in, it prefills and locks the email field and shows "Signed in — this generation will be saved to your account." `POST /api/generate` independently — not just via the UI lock — forces `email` to `session["account_email"]` whenever a session exists, ignoring whatever the client actually submitted, so this can't be bypassed with a raw API call carrying a spoofed email field. A logged-in submission also skips straight to generation (`Lead.status="verified"` immediately, `_kickoff_generation()` called directly, response `{"status": "generating", "id": ...}`) rather than creating a `pending_verification` lead and emailing a link — redundant, since they're already an authenticated, verified account. The existing one-generation-per-email 409 block (`_has_generation()`) still applies unchanged to logged-in submissions; only the redundant re-verification step is skipped.

   **Why a separate `Account` table instead of a column on `Lead`:** `leads.email` is deliberately *not* unique — one row per form submission, and a person can resubmit (creating multiple `Lead` rows for the same email over time). A password is an account-level concept, not a per-submission one, so `Account` (id, `email` unique, nullable `password_hash`, `created_at`) is its own table, loosely joined to `Lead`/`Generation` by matching email string — the same loose coupling those two already have with each other. No FK; nothing about `Lead`/`Generation` changed.

   This replaces the old bare `/my-sites/<token>` + `frontend/resend.html` pair (removed in an earlier pass) and the token-only `/account/<token>` dashboard-via-magic-link flow (removed in this pass — password login supersedes it).

   **Forgot password** (`/account/forgot-password`, `/account/reset-password/<token>`) — same shape as the rest of the auth flow: submit an email, and *only if* an `Account` row exists with a `password_hash` set, sign a token (`{"reset_email": email}`, `RESET_TOKEN_MAX_AGE` = 1h — shorter-lived than the other 24h tokens since it grants a password change) and email a reset link via `send_password_reset_email` (new template in `emails.py`, same `_wrapper()` styling as the others). Always shows the same "check your email" confirmation regardless of whether the account/password exists, so it can't be used to enumerate accounts (verified byte-identical responses in testing). The reset-password page re-derives the email from the token itself, not from any client-submitted field, and logs the user in immediately on success. Every password-entry form (login, set-password, reset-password) shares a `_password_field_html()` helper with a Show/Hide toggle button (`gwTogglePw()`, a few lines of vanilla JS in `_account_page()`'s shared `<script>`).

8. **Checkout** (`frontend/checkout.html`) — Stripe stub, untouched by this pass.

## Text editing (before and after going live)

`frontend/editor.html` — reached via an "Edit text" button on the account dashboard / preview page (`/editor.html?id=<public_id>`). Lists every `data-gw-text`-tagged element (`_extract_gw_text_fields`) in a sidebar; clicking one in the live iframe preview jumps to its textarea. Each field autosaves on a 1.2s debounce (`PATCH /api/generate/<id>/text`), and a top-nav **Save** button flushes all dirty fields immediately regardless of the debounce timer (badge shows the dirty-field count; a `beforeunload` guard warns if you navigate away with saves still pending).

- **Pre-launch** (`Generation.status != "live"`) — edits write straight to `html_content`, visible in the preview instantly. No approval step.
- **Post-launch** (`Generation.status == "live"`) — edits accumulate in `html_pending`; the live site (`html_content`) is untouched until applied. `GET /api/generate/<id>/text-fields` reads from `html_pending` first (falling back to `html_content`) so the editor always reflects the customer's latest requested state, not the stale live one.

**Applying pending edits to a live site** — `run_pending_edits_apply()` in `app.py` promotes `html_pending → html_content` for every live generation with pending edits and emails the customer (`send_changes_live_email`). This is a plain nightly job (`apply_pending_edits_job.py`, same "Railway Cron service + standalone script, nothing in-process schedules it" pattern as `outreach/domain_billing.py` and `outreach/email_discovery_job.py` — needs a Railway Cron service pointed at it), **not** an LLM/Claude Code step: by the time a row reaches this job, every field has already been validated as a literal string substitution at save time (`_update_gw_text_field`), so there's no judgement call left to make and no reason to pay for/wait on an API call. An admin can still force an immediate apply or discard pending edits from `/admin/generations/<id>/pending-changes` (e.g. to reject spam/garbage input) — that page is now a manual override, not the only path to going live.

## Admin

- `/admin/login` — plain username/password form against `ADMIN_USERNAME`/`ADMIN_PASSWORD`, sets a Flask session flag. `/admin/logout` clears it.
- `/admin/generations` — table of every `Generation` row (business, email, created_at, status) with links to `/admin/generations/<id>/html` (rendered) and `/admin/generations/<id>/form-data` (raw JSON that produced it). Rows whose `Lead.is_test` is true show a "TEST" badge. All admin routes are session-gated via the `admin_required` decorator and are not linked from any public page/nav.
- `/admin/generate-test` (GET form, POST submits) — admin-only tool to generate test sites without burning a real verification email or hitting the one-generation-per-email block. It creates a `Lead` with `status="verified"` and `is_test=True` directly (skipping `/api/generate`'s repeat-generation check entirely, since that check lives solely in that one endpoint) and kicks off the same background Claude call as `/verify/<token>`. Not reachable or linked from anywhere public — this is intentionally not the same code path the public form uses, so the real block is never weakened.

## API model

- Model: `claude-sonnet-4-6`
- Tools: `web_search_20250305` (Anthropic server-side search)
- Max tokens: 16 000
- Logo (if uploaded) is read back from disk at verify-time, full resolution, and passed as a base64 vision input block before the text prompt — used only for palette extraction. This is separate from the embedded logo image described below.

### Image persistence (logo + portfolio photos)

Uploaded logo/photos are saved to local disk (`uploads/<lead.public_id>/`) at submission time, same as before — but that disk is only ever used as **transient staging** between upload and generation, never as the long-term source for images that appear on the generated site. At generation time (`/verify/<token>` and `/admin/generate-test`), `_build_media_placeholders()` in `app.py`:

1. Reads each image file, downsizes it with Pillow if larger than a max dimension (480px for the logo, 1600px for portfolio photos — these are web display images, not originals), and re-encodes it as a `data:` URI (PNG if the source has real transparency, otherwise JPEG at quality 82, to keep the embedded HTML reasonably sized).
2. Assigns each image a short literal placeholder token (`GW_LOGO_SRC`, `GW_PHOTO_SRC_0`, `GW_PHOTO_SRC_1`, ...) and puts *only those tokens* — never the base64 data itself — into the prompt via `build_prompt.py`'s `MEDIA REFERENCES` section, instructing Claude to use them verbatim as `<img src="">` values. Claude never has to reproduce a long base64 string in its output, which would be slow and failure-prone at scale.
3. After generation, `_run_and_persist()` does a plain string substitution of each token for its real data URI before the HTML is written to the `generations` table — so the row in Postgres already contains the final, fully self-contained HTML with images baked in as data URIs.

This closes the gap that caused two related but distinct bugs previously: the **logo never rendered even immediately after generation**, because it was only ever sent to Claude as vision input for colour analysis — there was no `src` value for Claude to reference, so it had nothing valid to embed, ever. **Portfolio photos rendered at first but broke after a redeploy**, because they were referenced by external URL (`/api/generate/<id>/photos/<filename>`) pointing at the same ephemeral disk, which Railway wipes on every redeploy (confirmed via `railway volume list` — no volume is mounted on this service). Both are now fixed the same way: images live inside the persisted HTML itself, with no runtime dependency on disk surviving anything.

**Residual, much narrower caveat:** the upload files on disk still only need to survive from form submission until the user clicks the verification link (usually minutes). If a redeploy happens to land in that specific window, before generation has run, the not-yet-generated lead's images would be missing at generation time. This is a small, hard-to-hit edge case inherent to using local disk as any kind of staging, not a design flaw in the fix — true object storage (S3/R2) would close it entirely if it ever becomes a real problem.

**Old generations don't retroactively heal.** Any site generated before this fix (e.g. the G. Standing Roofing test) still has the old broken `<img>` references baked into its stored HTML — regenerating is the only way to pick up the fix for an existing row.

**Logo background processing** (`_process_logo()` in `app.py`, runs as part of the same Pillow pass, no extra API calls): if the logo's border (corners + edge midpoints) is near-uniform in colour, that background is flood-filled to transparent (from all four corners, so background colour trapped *inside* the mark survives, with a light alpha blur to soften the cutout edge) — this fixes uploaded logos rendering with a visible mismatched-colour box in the nav. If the border isn't uniform (photo/gradient background), it's left alone but baked into a small rounded-rect "chip" filled with the dominant border colour instead, so it reads as an intentional badge rather than a mismatched rectangle. Any failure or too-small/ambiguous image falls back to embedding as-is.

## Jobs store

`_jobs` (in-memory dict in `app.py`) is now a live-progress cache only, keyed by `lead.public_id`. Completed generations are always persisted to the `generations` table by `_run_and_persist()` before the email goes out; `job_status`/`job_html` fall back to the DB when a `_jobs` entry is missing (e.g. after a restart).

## build_prompt.py

It expects these keys in `form_data`:
`business_name`, `trade`, `location`, `coverage_area`, `phone`, `email`, `logo_uploaded` (bool), `portfolio_uploaded` (bool), `work_split` (plain-language string, e.g. "30% domestic / 70% commercial"), `craft_prestige` (standard/mid/high), `team_size` (string), `large_commercial_contracts` (bool), `urgency` (high/low), `years_trading`, `claimed_accreditations`, `claimed_projects`, `other_notes`, and optionally `logo_src_token` / `photo_src_tokens` (see Image persistence above) — these two are rendered in their own `MEDIA REFERENCES` section rather than the generic facts list.

The footer instruction (Step 4.8) computes `current_year = datetime.now().year` in Python and interpolates the literal value directly into the prompt text — Claude is told the actual current year, not left to guess or copy a stale example. It's also told to use a real "Website by Groundwork" hyperlink to `https://groundworkbuild.com` rather than any placeholder agency name.

## Database migrations

There's no Alembic (or any migration framework) in this project. `models.py`'s `init_db()` calls `Base.metadata.create_all()` (which only creates brand-new tables, never alters existing ones) followed by a small dependency-free `_ensure_column()` helper that adds any columns present in the SQLAlchemy model but missing from the live table (checked via `sqlalchemy.inspect()`, applied via a plain `ALTER TABLE ... ADD COLUMN`). Runs on every startup; safe because it checks first. If a future column needs a real backfill/default beyond a static `DEFAULT`, this helper isn't sufficient — reach for a real migration tool at that point.

## Frontend API URL

`frontend/config.js` sets `window.GROUNDWORK_API`. In development (Flask serves both), leave it empty. For production, set it to the Railway backend URL — either by editing `config.js` before deploying, or by using a Cloudflare build variable to inject it.

**Cookie/session-dependent calls are the exception** — they use a relative URL (e.g. `fetch('/api/generate', ...)`, not `window.GROUNDWORK_API + '/api/generate'`). `groundworkbuild.com` is served by a Cloudflare **Worker with Static Assets** (git-linked, auto-deploys via `npx wrangler deploy` on push — this is *not* classic Cloudflare Pages, which matters below), serving `frontend/` as the assets directory. Flask/Railway routes that aren't static files (`/api/*`, `/verify/*`, `/account`, `/account/*`, `/admin/*`) are proxied through to the Railway origin by `frontend/_worker.js` — its presence switches wrangler into "Advanced Mode," where the worker script sees every request first and explicitly falls back to `env.ASSETS.fetch(request)` for anything that isn't a backend path.

**This used to be a `frontend/_redirects` file** (the classic Cloudflare Pages proxy mechanism), which broke every deploy after it was added: `_redirects` on Workers Static Assets only supports *relative-path* proxy targets — pointing it at the Railway origin's absolute URL fails the build outright with `Proxy (200) redirects can only point to relative paths`, discovered via the Cloudflare dashboard's build log after "Sign In" (and everything else) started 404ing. `_worker.js` is the correct mechanism for this deployment type and has no such restriction, since it's a real `fetch()` call, not a declarative rule.

A relative fetch from a page on `groundworkbuild.com` goes through `_worker.js`'s proxy and carries the session cookie (scoped to `groundworkbuild.com`, since that's the origin the browser actually saw respond). The *absolute* Railway URL in `GROUNDWORK_API` is a genuinely different origin to the browser — a cookie scoped to `groundworkbuild.com` is never sent there. `build.html`'s account-session check and generate submission use relative URLs for exactly this reason; other calls that don't care about cookies (e.g. polling `/api/generate/<id>/status`, fetching `/api/generate/<id>/html`) keep using `GROUNDWORK_API` as before.

**If a new backend route is ever added that doesn't live under an existing prefix**, `BACKEND_PREFIXES` in `frontend/_worker.js` needs a matching entry, or it'll 404 on the custom domain even though it works fine hitting Railway directly — easy to miss since the failure mode looks identical to a real 404.

## Brand / contact

- Accent: `#3B82F6` (blue). Hover: `#2563EB`. No amber (except the preview watermark CTA, which intentionally uses `#B8976A` to stand out as non-brand chrome).
- Contact email: `groundwork-build@outlook.com`
- Plans: **Starter** — no setup fee (removed 2026-07-23, until break-even), £24.99/mo (first month of hosting free via a 30-day Stripe trial on the subscription). `STRIPE_MONTHLY_PRICE_ID` in Railway must point at a £24.99 Stripe Price object — that swap has to happen in the Stripe dashboard, this repo has no Stripe credentials to do it from code. One-Man-Band and Director are coming soon stubs.

## Marketing pages

`frontend/index.html` (Home), `pricing.html`, `about.html`, `contact.html` — plain static HTML, inline styles, no shared header/footer include file (same pattern as the rest of `frontend/`). Nav and footer markup is duplicated in each page's `<body>` on purpose, matching how the working funnel pages already do it. `index.html` embeds three real live generated sites as scaled `<iframe>`s (hero + two "Real examples" cards) rather than mockups or screenshots — a vanilla-JS `ResizeObserver` rescales each to its container width. `pricing.html` has a monthly/annual billing toggle (annual isn't wired to Stripe yet — no `STRIPE_ANNUAL_PRICE_ID` — it's a marketing-only display for now).

## Design source

Marketing pages were last refreshed from a Claude Design project (`claude.ai/design`), pulled via the `DesignSync` MCP tool. The project's `.dc.html` files are design references in that tool's own component format — not meant to be copied in as-is — recreated here as plain static HTML per the pattern above.

## Custom domain automation (Porkbun → Cloudflare for SaaS → Railway)

When a customer buys a domain (Stripe webhook → `_handle_domain_order_async` in `app.py`), three things happen in order, each recorded on a `Domain` row (`models.py`) so a partial failure can be resumed/diagnosed:

1. **Porkbun registration** (`_porkbun_register_domain`) — buys the domain.
2. **Cloudflare Custom Hostnames** (`_cloudflare_add_custom_hostname`) — registers the apex domain *and* `www.<domain>` as two separate Custom Hostname objects on our `groundworkbuild.com` zone (Cloudflare matches hostnames exactly; a single config does not cover both apex and www — wildcard custom hostnames are a separate paid tier we're not using). Uses DV SSL with HTTP validation, which Cloudflare's edge completes once the customer's DNS points at our CNAME target.
3. **Porkbun DNS** (`_porkbun_create_dns`) — same ALIAS-at-root + CNAME-at-www pattern as before, just pointed at `CLOUDFLARE_CNAME_TARGET` (e.g. `connect.groundworkbuild.com`) instead of a Railway-provided target.

**This replaced Railway's native custom domain feature** (`_railway_add_custom_domain`, now removed), because Railway's Hobby plan caps a service at 2 custom domains — Cloudflare for SaaS gives the first 100 free and $0.10/mo each after, with no meaningful cap for our volume. Cloudflare for SaaS is configured at the zone level with a Fallback Origin pointing at this Railway service, so once a Custom Hostname is active, Cloudflare terminates TLS and forwards matching requests straight to Railway — Railway itself doesn't know about the domain.

**Host-based routing now has to handle this app-side.** Previously, `handle_subdomain_request` (the `@app.before_request` hook) only matched `<slug>.groundworkbuild.com` subdomains against `Generation.subdomain` — there was no lookup for a purchased custom domain at all, because Railway's own native custom-domain feature routed that traffic straight to the app without Flask ever needing to inspect the Host header for it. Now that Cloudflare forwards *all* hosts (subdomain, custom apex, custom www) to the same Fallback Origin, the hook also looks up `request.host` (minus a `www.` prefix) against `Domain.domain` where `status == "active"`, and serves the linked `Generation` the same way. A `Domain` row only reaches `status="active"` once DNS is configured (`_handle_domain_order_async`'s step 3) — check the corresponding Custom Hostname shows **Active** in the Cloudflare dashboard if a connected domain 404s instead of serving the site.

The `needs_manual_setup` fallback email (`send_domain_setup_failed_email` in `emails.py`) now gives Cloudflare-specific manual steps (add Custom Hostnames in the Cloudflare dashboard, point Porkbun DNS at the Cloudflare target) instead of the old Railway-dashboard instructions.

## Growth roadmap

`docs/growth-roadmap.md` — living to-do list for expanding beyond cold
email/SMS (social presence + comment-to-DM automation, affiliate/referral
revenue, client-as-affiliate program, generation quality for thin-data
prospects). Read it at the start of any session touching growth/monetization
work, and update it at the end of one — don't let this planning live only in
chat history.

## Outreach pipeline (cold outreach → prospect → paid customer)

Separate from the inbound funnel above — a daily automated pipeline that sources UK trade businesses (Google Places API), scores/qualifies them, emails/texts a free generated-site preview via a magic link, and follows up. Full spec, schema, and design rationale: `docs/outreach-pipeline-spec.md` — this section is a pointer to that doc plus the parts most likely to trip up an agent working on this system without full context.

**Scheduled jobs are a mix of two different mechanisms — don't assume everything is a Railway Cron service:**
- `outreach/pipeline.py` (sourcing), `outreach/email_discovery_job.py` (Tier 1 email scraping, free/code-only), `outreach/send_job.py` (sends), `outreach/domain_billing.py`, `apply_pending_edits_job.py`, `outreach/variant_optimizer_job.py` (email-variant testing, Section 19), `outreach/send_daily_summary.py` (once-daily admin digest) — each is a standalone Python script pointed at by its own Railway Cron service, nothing in-process schedules them. **Live schedule, confirmed directly against Railway 2026-07-27 (source of truth — don't trust an older cron time quoted elsewhere in this repo without re-checking Railway, schedules here have changed more than once):**
  | Service | Script | Cron (UTC) |
  |---|---|---|
  | `sourcing-cron` | `outreach/pipeline.py` | `0 1 * * *` |
  | `email-discovery-cron` (Tier 1) | `outreach/email_discovery_job.py` | `0 2 * * *` |
  | `domain-billing-cron` | `outreach/domain_billing.py` | `0 3 * * *` |
  | `pending-edits-apply-cron` | `apply_pending_edits_job.py` | `0 4 * * *` |
  | `send-job-cron` | `outreach/send_job.py` | `*/15 4-22 * * *` |
  | `pending-batch-export-cron` | `outreach/export_and_push_pending_batch.py` | `5 * * * *` (hourly) |
  | `discovery-pickup-cron` | `outreach/pickup_drive_results.py` | `50 * * * *` (hourly) |
  | `facebook-sourcing-pickup-cron` | `outreach/pickup_facebook_sourced.py` | `20 * * * *` (hourly) |
  | `variant-optimizer-cron` | `outreach/variant_optimizer_job.py` | `0 * * * *` (hourly) |

  **The pending-batch export and both Drive pickups run hourly, not nightly/daily** — this matters for the Claude Code routines below: it means `pending_batch.json` is never more than ~1h stale when a discovery routine run reads it, and any Drive results file a routine writes gets picked up (and its `...ImportState`/`...PickupState` marker advanced) within the hour, well before a routine could plausibly fire again and overwrite it with a newer file. Each pickup script only ever imports the single newest matching Drive file (`_find_latest_file`) — safe today only *because* pickups run far more often than the routines that feed them fire. **If a routine's firing frequency is ever increased to less than ~1h between runs, this stops being safe** (a run's output could get silently superseded and never imported) — either slow the routine back down, speed up the matching pickup cron to match, or change the pickup script to process every new file since the last import instead of just the latest.
- **Per-click admin notifications were removed 2026-07-23** in favour of `outreach/send_daily_summary.py`'s once-a-day digest (emails sent, clicked, signed-up, with click%/signup% of sent) — `send_admin_magic_link_clicked_email` no longer exists. `send_admin_payment_received_email` is untouched (still fires in real time on `checkout.session.completed` — payment is rare and worth an instant ping, unlike clicks at real volume).
- **`send-job-cron` runs every 15 minutes**, window `04:00-22:00 UTC` (`outreach/ramp.py`'s `EMAIL_SEND_WINDOW_START_HOUR`/`END_HOUR_EXCLUSIVE` — start hour corrected 3→4 on 2026-07-27 to actually match this cron, which never fired at 03:00 despite the code-level guard formerly allowing it). Email is capped at a fixed **`EMAIL_DAILY_TOTAL` = 192/day** (changed 2026-07-27, by request, down from the prior fixed-5-per-slot design's ~380/day ceiling), split across 15-minute slots by `outreach/ramp.py`'s `_slot_plan()` — every slot gets a guaranteed `EMAIL_SLOT_FLOOR` (so no hour ever goes fully dark and stops collecting engagement data), plus the remaining budget weighted toward whichever slots have the best real open/click engagement rate so far (falls back to an even split until a slot has `MIN_SLOT_SAMPLE_SIZE` real sends of its own). Forced to 0 regardless of the computed cap if email's circuit breaker is tripped (`RampState.circuit_breaker_tripped`) — previously a tripped breaker didn't actually stop email volume, since the old fixed-5 cap didn't consult it; fixed alongside this change. `EMAIL_HOURLY_RAMP_TABLE` still exists and still drives SMS's daily ramp, but no longer anything on the email side. **Priority hold**: a prospect with `score >= PRIORITY_SCORE_THRESHOLD` (95) and a confirmed email is held back from a non-top-tier slot (`is_top_engagement_slot_now`, top `PRIORITY_TOP_TIER_PERCENTILE` = 25% of today's slot weights) rather than sent in strict score order, up to `PRIORITY_MAX_HOLD` (24h) — after that it sends regardless rather than going stale (`outreach/send_job.py`'s `fill_initial_sends`/`_is_priority_prospect`). SMS is untouched — still one daily budget via `get_remaining_ramp_today`, no window. `run_daily_send()` (the function name predates all of this) is still the entry point `send_job.py`'s `__main__` calls; every underlying check is idempotent per invocation regardless of firing frequency.
- **SMS is already restricted to `website_status == "no_website"` prospects** (`outreach/sms.py`'s `sms_channel_eligible`, added 2026-07-25) — checked at every SMS send site in both `send_job.py` and `followup.py`. Google Places sourcing surfaces plenty of no-website-but-has-phone prospects on its own; if SMS volume is ever too low, that's `sourcing-cron` under-supplying no-website leads, not a bug in this gate — don't loosen it to compensate.
- **`variant-optimizer-cron` runs HOURLY**, pointed at `outreach/variant_optimizer_job.py` — but is threshold-gated, not calendar-gated (see `docs/outreach-pipeline-spec.md` Section 19), so most hourly runs at current volume are genuine no-ops. All DB-side work (stats, promotion/pause) is plain code, zero API cost. Needs `DATABASE_URL`, `GITHUB_PUSH_TOKEN` (pushes the candidate-request file via the GitHub Contents API), `GOOGLE_DRIVE_API_KEY`/`GOOGLE_DRIVE_FOLDER_ID` (reads the routine's candidate results) — no `ANTHROPIC_API_KEY` (removed 2026-07-21; candidate generation moved off a direct metered call to the daily routine below, by request, to avoid API cost). **Cannot itself be a Claude Code routine or run via Cowork** — both are confirmed blocked from writing to production, same reason every other send/scoring job in this list is a Railway Cron script and not a routine.
- **"Groundwork daily email-variant candidate generation"** (`trig_01XBk2fYnbjythwsbcZC7545`, `0 10 * * *` UTC) — a genuine Claude Code routine, same Drive-based handoff pattern as the nightly email-discovery routine (reads `outreach/discovery_batches/variant_candidate_request.json` + `docs/cold-email-evidence-library.md` from its git checkout, writes `groundwork-variant-candidates.json` to the shared Drive folder). Only generates copy — never touches the DB or the ramp. `variant-optimizer-cron` above both feeds this routine its work (via the GitHub-pushed request file) and picks up its output (via Drive) — since it polls hourly, it isn't sensitive to exactly when in the day this routine fires.
- **The nightly email-discovery "Tier 2" (deeper lookup, for prospects Tier 1's own-website scrape can't find) is NOT a Railway Cron script.** It used to be (calling the Anthropic API directly) but that was deleted for cost reasons (~£15-20/night, most of it spent on searches that came up empty) — see `docs/outreach-pipeline-spec.md` Section 4a for the full account. It's now a **scheduled Claude Code cloud routine** ("Groundwork nightly email discovery," `trig_019xosk9ScZfmyz4VTtnx7g7`, cron `0 1,16 * * *` UTC — twice daily), using WebSearch under the Claude subscription rather than a metered API call. This is a real, deliberate exception to the "everything is a Railway Cron script" pattern above.
  - **The routine can reach neither groundworkbuild.com nor git — both confirmed platform-level restrictions on its CCR sandbox, not config gaps.** Direct API calls (WebFetch to the `/g/` endpoints below) are blocked by the sandbox's own egress policy (403 at the CONNECT-tunnel stage — confirmed via its `/__agentproxy/status` diagnostic, unrelated to anything on the app side). `git push` 403s every time regardless of branch/`outcomes` config or GitHub App state ("Resource not accessible by integration") — confirmed via `git ls-remote` that no `claude/*` branch has ever actually landed on the remote from any CCR session. **What actually works**: the routine reads `outreach/discovery_batches/pending_batch.json` from its git checkout (`Read` only, no network — refreshed hourly by `pending-batch-export-cron`, see table above), and writes results to a Google Drive file via its `Google_Drive` MCP connection — a different transport than the sandboxed WebFetch/git paths, and the only one that's been reliable (it worked even on a night WebFetch was broadly down for every other domain, confirmed via a bbc.co.uk control fetch). `discovery-pickup-cron` (hourly, see table above) reads that Drive file with a plain API key and imports it via `outreach/pickup_drive_results.py` — idempotent via `DiscoveryImportState`. If you're an agent debugging why a routine can't push or can't reach the app: stop, it's expected — don't try to "fix" it by routing around it, the Drive path is the sanctioned one.

- **"Groundwork Facebook-page tradesperson sourcing"** (`trig_01J4qQwg6h9xNeSSmkubHGCW`, `0 6,21 * * *` UTC — twice daily) — a second, separate routine, same Drive-handoff shape as the discovery routine above but a genuine SOURCING channel, not enrichment: it searches directly for UK trade businesses' Facebook Pages (`site:facebook.com`-style WebSearch + WebFetch of the actual Page) that may never exist in Google Places' dataset at all, sidestepping that API's 1,000-call/month free-tier ceiling entirely. Writes `groundwork-facebook-sourced.json` to the same shared Drive folder; `facebook-sourcing-pickup-cron` (hourly, see table above, `outreach/pickup_facebook_sourced.py`) imports it, idempotent via `FacebookSourcingPickupState`, deduping on `facebook_page_url` then a case-insensitive `business_name`+`location` match against existing prospects.
- All three CCR routines above currently run under the same personal Claude account as interactive sessions like this one — no separate routines-only account exists yet, despite an earlier note in this file suggesting a migration was in progress. If a migration to a dedicated account happens later, re-verify the trigger ids and schedule above against `RemoteTrigger action=list`, don't trust this table blindly past that point either.

**The outreach judgment API — GET-based, token-authenticated** (`app.py`, search `_check_outreach_get_token`). **Not currently reachable by the discovery routine itself** (see above) but still real and used elsewhere (e.g. manual/local calls, `outreach/import_discovery_results.py`'s underlying validation path):
- `GET /api/admin/outreach/g/pending?token=<OUTREACH_API_TOKEN_GET>&limit=<n, default 20, max 50>` — the oldest `limit` prospects still waiting on email discovery.
- `GET /api/admin/outreach/g/update-website?token=...&prospect_id=<id>&website=<url>` — corrects a prospect's website when Google Places' own field was empty/wrong.
- `GET /api/admin/outreach/g/apply-email?token=...&prospect_id=<id>&email=<email>&source=<source>` — applies a genuinely-found email (server re-validates format/guess-detection/MX-deliverability regardless of caller). Omit `email` to finalize as not-found.
- `GET /api/admin/outreach/g/log-run?token=...&processed=<n>&found=<n>&website_rediscovered=<n>&finalized_null=<n>&sources=<json>&notes=<text>` — logs one `DiscoveryRunLog` row, shown on `/admin/discovery`.
- Auth prefers an `X-Outreach-Token` header (`_check_outreach_get_token`), falling back to `?token=` in the URL (logged loudly with a warning when used) specifically because WebFetch-style callers can't set custom headers. This is intentional, not a vulnerability to "fix" by removing the fallback.
- There's an older sibling pair, `/api/admin/outreach/pending` / equivalent POST endpoints (`OUTREACH_API_TOKEN`, `Authorization: Bearer` only, no GET fallback) — predates the `/g/` versions, same underlying DB logic, kept for whatever originally used it.
- These are real, sanctioned, low-privilege endpoints — narrowly scoped to the outreach discovery queue only, not general admin/DB access. `/admin/*` (session-cookie, `admin_required`) remains the only interface for everything else in "Admin" above; this token-based API is deliberately separate and doesn't grant access to it.
