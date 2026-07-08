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
- `IP_RATE_LIMIT_PER_HOUR` — max form submissions per IP per hour (default `5`).
- `PORT` — set automatically by Railway.

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
- Plans: **Starter** £99 setup + £24.99/mo (first month of hosting free via a 30-day Stripe trial on the subscription). One-Man-Band and Director are coming soon stubs.

## Design source

Original design files (`.dc.html`) are in `frontend/` of the zip handoff. The `frontend/*.html` files are the working plain-HTML conversions of those designs.
