# Groundwork — project overview

Groundwork generates AI-built marketing websites for UK trades businesses. A user fills in an 8-step form; the submission is gated behind email verification before any Claude API call fires; once verified, the Flask backend calls the Anthropic API, and the generated single-file HTML is persisted to Postgres and served (watermarked, noindex) as a direct link the user opens in a new tab.

## Architecture

| Layer | Technology | Host |
|---|---|---|
| Backend API | Flask (`app.py`) | Railway |
| Database | Postgres via SQLAlchemy (`models.py`) | Railway |
| Email | Resend (`emails.py`) | — |
| Frontend | Static HTML + vanilla JS (`frontend/`) | Cloudflare Pages |
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

7. **My Account** (`/account/login`, `/account/<token>`) — the account/retrieval flow, server-rendered in `app.py` (styled to match the rest of the funnel via `_account_page()`/`_SITE_HEADER`/`_SITE_FOOTER`, since there's no shared CSS file). `/account/login` takes an email, and — only if that email has at least one `Generation` — emails a signed 24h token (`{"account_email": email}`) via Resend pointing at `/account/<token>`; always shows the same "check your email" confirmation either way, so it can't be used to enumerate accounts. `/account/<token>` validates the token and lists every generation for that email (business name, date, status, "View site" link, and a "Go live" link to `checkout.html` for anything not yet `status="live"`). Linked from "Sign In" in the nav on `index.html` and `build.html`, and from the preview/verify-error pages. This supersedes an earlier bare `/my-sites/<token>` + `frontend/resend.html` pair, which has been removed.

8. **Checkout** (`frontend/checkout.html`) — Stripe stub, untouched by this pass.

## Admin

- `/admin/login` — plain username/password form against `ADMIN_USERNAME`/`ADMIN_PASSWORD`, sets a Flask session flag. `/admin/logout` clears it.
- `/admin/generations` — table of every `Generation` row (business, email, created_at, status) with links to `/admin/generations/<id>/html` (rendered) and `/admin/generations/<id>/form-data` (raw JSON that produced it). Rows whose `Lead.is_test` is true show a "TEST" badge. All admin routes are session-gated via the `admin_required` decorator and are not linked from any public page/nav.
- `/admin/generate-test` (GET form, POST submits) — admin-only tool to generate test sites without burning a real verification email or hitting the one-generation-per-email block. It creates a `Lead` with `status="verified"` and `is_test=True` directly (skipping `/api/generate`'s repeat-generation check entirely, since that check lives solely in that one endpoint) and kicks off the same background Claude call as `/verify/<token>`. Not reachable or linked from anywhere public — this is intentionally not the same code path the public form uses, so the real block is never weakened.

## API model

- Model: `claude-sonnet-4-6`
- Tools: `web_search_20250305` (Anthropic server-side search)
- Max tokens: 16 000
- Logo (if uploaded) is read back from disk at verify-time and passed as a base64 image block before the text prompt — used for palette extraction.
- Portfolio photos are stored in `uploads/<lead.public_id>/` and served at `/api/generate/<id>/photos/<filename>`. **Known limitation:** this is local disk, which is not guaranteed to survive a Railway redeploy — logo/photo files (and therefore `<img>` tags pointing at them in older generated HTML) can go missing after a redeploy even though the HTML text itself is safely persisted in Postgres. Worth moving to object storage (S3/R2) if this becomes a problem.

## Jobs store

`_jobs` (in-memory dict in `app.py`) is now a live-progress cache only, keyed by `lead.public_id`. Completed generations are always persisted to the `generations` table by `_run_and_persist()` before the email goes out; `job_status`/`job_html` fall back to the DB when a `_jobs` entry is missing (e.g. after a restart).

## build_prompt.py

It expects these keys in `form_data`:
`business_name`, `trade`, `location`, `coverage_area`, `phone`, `email`, `logo_uploaded` (bool), `portfolio_uploaded` (bool), `work_split` (plain-language string, e.g. "30% domestic / 70% commercial"), `craft_prestige` (standard/mid/high), `team_size` (string), `large_commercial_contracts` (bool), `urgency` (high/low), `years_trading`, `claimed_accreditations`, `claimed_projects`, `other_notes`.

The footer instruction (Step 4.8) computes `current_year = datetime.now().year` in Python and interpolates the literal value directly into the prompt text — Claude is told the actual current year, not left to guess or copy a stale example. It's also told to use a real "Website by Groundwork" hyperlink to `https://groundworkbuild.com` rather than any placeholder agency name.

## Database migrations

There's no Alembic (or any migration framework) in this project. `models.py`'s `init_db()` calls `Base.metadata.create_all()` (which only creates brand-new tables, never alters existing ones) followed by a small dependency-free `_ensure_column()` helper that adds any columns present in the SQLAlchemy model but missing from the live table (checked via `sqlalchemy.inspect()`, applied via a plain `ALTER TABLE ... ADD COLUMN`). Runs on every startup; safe because it checks first. If a future column needs a real backfill/default beyond a static `DEFAULT`, this helper isn't sufficient — reach for a real migration tool at that point.

## Frontend API URL

`frontend/config.js` sets `window.GROUNDWORK_API`. In development (Flask serves both), leave it empty. For Cloudflare Pages production, set it to the Railway backend URL — either by editing `config.js` before deploying, or by using a Cloudflare Pages build variable to inject it.

## Brand / contact

- Accent: `#3B82F6` (blue). Hover: `#2563EB`. No amber (except the preview watermark CTA, which intentionally uses `#B8976A` to stand out as non-brand chrome).
- Contact email: `groundwork-build@outlook.com`
- Plans: **Starter** £99 setup + £24.99/mo. One-Man-Band and Director are coming soon stubs.

## Design source

Original design files (`.dc.html`) are in `frontend/` of the zip handoff. The `frontend/*.html` files are the working plain-HTML conversions of those designs.
