# Groundwork — project overview

Groundwork generates AI-built marketing websites for UK trades businesses. A user fills in an 8-step form, the Flask backend calls the Anthropic API, and the generated single-file HTML is served (watermarked, noindex) as a direct link the user opens in a new tab.

## Architecture

| Layer | Technology | Host |
|---|---|---|
| Backend API | Flask (`app.py`) | Railway |
| Frontend | Static HTML + vanilla JS (`frontend/`) | Cloudflare Pages |
| AI generation | Anthropic API via `build_prompt.py` | — |

## Key environment variables

- `ANTHROPIC_API_KEY` — must be set in Railway. Never hardcode.
- `PORT` — set automatically by Railway.

## The generation flow

1. **Build form** (`frontend/build.html`) — 8-step vanilla JS form. On step 8 submit, sends `multipart/form-data` to `POST /api/generate` on the Flask backend. Fields: `business_name`, `trade`, `location`, `coverage_area`, `phone`, `email`, `commercial_split` (0–100, commercial share), `work_type` (standard/mix/bespoke), `team_size` (sole/small/company), `large_contracts` (yes/no), `urgency` (ahead/emergency), `years_trading`, `accreditations`, `past_clients`, `notes`. Logo uploaded as file field `logo`; portfolio photos as multiple file field `photos`.

2. **Flask** (`app.py`) — maps form fields to `build_prompt`'s expected keys (see `_map_form()`), calls `build_prompt.build_prompt(data)`, starts a background thread that calls the Anthropic API. Returns `{"id": "<10-char hex>"}` immediately.

3. **Loading page** (`frontend/loading.html`) — receives `?id=<id>` in URL, polls `GET /api/generate/<id>/status` every 2 seconds. On `"done"` redirects to `preview.html?id=<id>`. On `"error"` shows message.

4. **Preview page** (`frontend/preview.html`) — shows a "View your website →" link that opens `GET /api/generate/<id>/html` in a new tab (the response is watermarked with a preview bar + noindex meta tag, injected on the fly by `_inject_watermark()` in `app.py` — the stored HTML itself is never modified). "Make it live" and "Go live" buttons link to `checkout.html`.

5. **Checkout** (`frontend/checkout.html`) — Stripe stub. Not yet wired; email fallback only.

## API model

- Model: `claude-sonnet-4-6`
- Tools: `web_search_20250305` (Anthropic server-side search)
- Max tokens: 16 000
- Logo (if uploaded) is passed as a base64 image block before the text prompt — used for palette extraction.
- Portfolio photos are stored in `uploads/<job_id>/` and served at `/api/generate/<id>/photos/<filename>`. Their URLs are appended to the prompt so Claude can reference them as real `<img>` tags.

## Jobs store

Jobs live in an in-memory dict (`_jobs` in `app.py`). Status values: `pending` → `done` or `error`. **Restarting the Railway process clears all jobs.** Persistent storage (Redis, DB) is a future improvement.

## build_prompt.py

It expects these keys in `form_data`:
`business_name`, `trade`, `location`, `coverage_area`, `phone`, `email`, `logo_uploaded` (bool), `portfolio_uploaded` (bool), `work_split` (plain-language string, e.g. "30% domestic / 70% commercial"), `craft_prestige` (standard/mid/high), `team_size` (string), `large_commercial_contracts` (bool), `urgency` (high/low), `years_trading`, `claimed_accreditations`, `claimed_projects`, `other_notes`.

## Frontend API URL

`frontend/config.js` sets `window.GROUNDWORK_API`. In development (Flask serves both), leave it empty. For Cloudflare Pages production, set it to the Railway backend URL — either by editing `config.js` before deploying, or by using a Cloudflare Pages build variable to inject it.

## Brand / contact

- Accent: `#3B82F6` (blue). Hover: `#2563EB`. No amber.
- Contact email: `groundwork-build@outlook.com`
- Plans: **Starter** £99 setup + £24.99/mo. One-Man-Band and Director are coming soon stubs.

## Design source

Original design files (`.dc.html`) are in `frontend/` of the zip handoff. The `frontend/*.html` files are the working plain-HTML conversions of those designs.
