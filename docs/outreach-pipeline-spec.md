# Groundwork Outreach Pipeline — Build Spec

## Goal

Automated daily pipeline: source UK trade businesses → qualify → generate a personalized site preview → email outreach → follow-up sequence → convert to paying customer. Target: 20 emails sent/day, reviewed each morning before send.

---

## 1. Prospect Sourcing

- **Source:** Google Places API (Text Search), Enterprise tier (~$35/1,000 calls — rating, website field, phone, business status)
- **Scope:** National, all trade categories, no regional/trade caps on final selection
- **Method:** Rotate through a grid of UK postcode areas × trade categories. Each day, run ~20–25 search queries covering different region/trade cells, prioritizing cells not searched recently, to build a raw daily candidate pool (target 200–500+ raw candidates/day)
- **Dedup:** Check every candidate against existing prospects table before processing — skip if already present

---

## 2. Gates (pass/fail — drop if any fail)

- Not marked "permanently closed" on Google
- A genuine, contactable email is found (see Section 4)

> **Note:** "has a website" is NOT a gate — see Section 3.
>
> **Note:** Business legal structure — sole trader vs. limited company — is NOT a gate. User has made a deliberate, informed decision to email all qualifying trade businesses regardless of structure, accepting PECR exposure for the sole-trader segment. Unsubscribe/opt-out handling (Section 11) must be immediate and strictly honored for all recipients given this.

---

## 3. Website Condition Check

For every candidate, tag as one of:

- `no_website`
- `has_website_dated`
- `has_website_modern`

**Method:** If a website exists, screenshot it and run a vision check against this checklist. Score 2+ = dated. Site fails to load/times out/cert error = automatic dated.

**Checklist:**

1. Fixed/non-responsive layout, squeezed or misaligned content
2. Outdated design cues — default template look, clashing colours, stretched/pixelated images, low-res logo
3. No clear call-to-action anywhere
4. Stale content — old copyright year, broken images, placeholder text, dead links
5. No reviews/testimonials shown despite the business having Google reviews
6. Fails to load / times out / security warning

`has_website_modern` candidates are effectively deprioritized (0 pts in scoring) but not hard-excluded.

---

## 4. Email Discovery (for gated/qualified candidates only, not full raw pool)

- **Method**: Claude web search — check sources in this order:
  1. The business's own website, if one exists (`mailto:` links, plain-text address on contact/about pages)
  2. Facebook Business Page
  3. UK trade directories: Checkatrade, Yell, TrustATrader, Rated People, Bark, MyBuilder, FreeIndex — these commonly list contact emails for businesses that don't have their own site, which is exactly the segment most likely to depend on directories rather than a website for bookings
  4. General web search as a final fallback
- **Critical for `no_website` prospects specifically**: since step 1 is unavailable for this segment by definition, discovery *must* actively check steps 2-3 rather than defaulting to a plain web search — this segment scores highest (Section 5) precisely because it's the strongest pitch, so a search method that systematically fails on it undermines the whole prioritization. If a no-website prospect's email discovery only attempted step 1 or a generic search, re-run it explicitly against directories before logging as `qualified_no_email`.
- **Hard rule**: Never generate/guess a plausible email (e.g. info@businessname.co.uk pattern-matching). Only extract emails actually found in a source. If none found after checking all four source types, log as `qualified_no_email` and exclude from that day's send — do not drop the record.
- **Not using a paid tool** (Hunter.io etc.) — these rely on a known domain, which no-website businesses don't have by definition. Free/agentic route only for now; revisit only if discovery rate proves too low.

---

## 5. Scoring (0–100, applied after gates + website check)

| Factor | Max pts | Breakdown |
|---|---|---|
| Rating | 30 | 4.0–4.4 = 15, 4.5+ = 30 |
| Website status | 25 | no_website = 25, has_website_dated = 20, has_website_modern = 0 |
| Trade type tier | 20 | High = 20, Medium = 12, Low = 5 (see Section 6) |
| Review count | 15 | 1 review = 3, 2–5 = 8, 6+ = 15 |
| Team size signal | 10 | Solo/small = 10 |

Take top 20 by score from the day's qualified, email-found pool. No regional/trade caps.

---

## 5a. Review Queue & Approval Interface

- **No cap on the queue.** Task A (overnight sourcing) appends every qualified, scored prospect to a persistent pending queue regardless of size — review pace and sourcing volume are fully decoupled. A large backlog is intended behavior (lets the user batch-review or step away for a period without stalling the pipeline), not a problem to solve.
- **Interface:** dedicated page, separate from the main app — single-card, swipe/click yes-or-no review (Tinder-style), one prospect at a time. Each card shows: business name, trade, location, rating, review count, score, website status, and a link to their existing Google listing/site if available. Decision writes immediately to `approval_status` and `approved_at`, advances to next card.
- **Queue ordering — predicted approval likelihood, with a mandatory exploration slice.** Once the approval-rate model (Section 5b) has enough data, order the pending queue by predicted likelihood of a "yes" — but reserve roughly 15–20% of what's shown as lower-predicted prospects mixed in, rather than pure highest-predicted-first. Without this, the queue becomes self-reinforcing: only showing prospects the model already believes will be approved means it never gets tested on the ones it predicts will be rejected, so it can never discover or correct a wrong assumption. The exploration slice is what keeps the model actually learning rather than just confirming itself.
- Task B only ever pulls `approval_status = yes` records — queue size has no bearing on send volume, which stays governed by the ramp schedule (Section 15/Phase 3). If more prospects are approved than the day's send cap, the remainder rolls forward to the next day's send-priority ranking (see Section 5b) rather than being lost.

---

## 5b. Adaptive Scoring Feedback Loop

Goal: use the user's own yes/no approval decisions, and eventually real paid-conversion outcomes, to detect when the current point weights (Section 5) don't actually predict what matters — and to prioritize both the review queue and the send queue accordingly. Two distinct models, two distinct jobs:

- **Predicted approval likelihood** — drives review queue ordering (Section 5a). Trained on the user's own yes/no decisions against each scoring factor's value.
- **Predicted conversion likelihood** — drives send queue ordering (which of the approved backlog gets generated + emailed first, given the daily send cap). Falls back to the composite score (Section 5) until enough real paid-conversion data exists to train a proper conversion-rate-by-factor model, then gradually shifts weight toward it.

**Mechanics for both:**

- Log every scoring factor's value alongside every decision (approval yes/no, and later, paid yes/no) — not just the composite score. Composite-score analysis alone can't reveal which factor is miscalibrated; need per-factor breakdowns (e.g. approval rate for trade_tier = High vs Medium vs Low; by rating band; by website_status; etc.)
- Periodically (weekly, alongside the Friday review) compute rate per factor-tier and compare against what the current point allocation implies. Example: if trade_tier = Medium (12 pts) shows a 99% approval rate while rating 4.5+ (30 pts) shows a 1% approval rate, that's a strong signal the current weights don't reflect what's actually predictive.
- **Minimum sample size before acting:** require a meaningful number of decisions per factor-tier (e.g. 30+) before treating a divergence as real rather than noise.
- **Surface, don't auto-change.** Flagged divergences get presented as suggested reweightings during the Friday review, not applied silently — keeps a human check on the model rather than letting it drift on its own.
- Approval rate and conversion rate can diverge — approval reflects the user's judgment (fast, same-day, high-volume), conversion reflects actual market outcome (slow, ground truth). Use approval-rate analysis to iterate quickly early on; once enough paid-conversion data exists, cross-validate and let conversion data take precedence.

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

---

## 10a. SMS Channel

Runs as a parallel channel to email, same qualified prospect pool (no separate gating) — sent to companies and sole traders per the risk decision in Section 2.

- **Source:** phone number already comes from the Places API Enterprise tier pull (Section 1) — no separate discovery step needed, unlike email
- **Provider:** Twilio — ~4–5p per UK SMS, plus ~£1/month number rental
- **Compliance setup** (one-off, before first send): UK A2P sender registration with Twilio — required for promotional SMS
- **Content:** shorter version of the email templates — same core message (preview link, key features, £99+£24.99/month), single CTA link
- **Unsubscribe:** handled via STOP keyword reply — Twilio auto-suppresses future sends once registered, but the webhook must be caught and written back to the prospect record
- Unsubscribe is channel-specific: an SMS opt-out does not imply an email opt-out, and vice versa — track separately (see schema, Section 13)
- **Follow-up sequence** (Section 11): same trigger logic and timing, but can route through SMS instead of/alongside email

---

## 10b. Channel Circuit-Breaker (Email Health → SMS Fallback)

Automated logic to protect outreach effectiveness if the email sending domain's reputation degrades:

- **Monitor:** Google Postmaster Tools spam complaint rate daily (Resend bounce/complaint webhooks as a faster interim proxy)
- **Trigger threshold:** spam rate crosses 0.1% (Gmail's hard ceiling is 0.3%, but degradation begins well before that — 0.1% is the working line)
- **While triggered:**
  - New prospects sourced during this period get SMS as the first touch instead of email
  - Email send volume is paused or significantly cut to allow recovery
- **Recovery:** spam rate must hold below 0.3% for 7 consecutive days before standing is restored. Resume email-first routing and normal volume ramp only once metrics are consistently clean.

---

## 11. Follow-Up Sequence

Triggered by a daily job checking each prospect's funnel status + time since last touch. Hard cap: max 3 follow-ups after the initial (4 touches total). Kill sequence immediately on payment, unsubscribe, or reply.

| Trigger | Timing | Angle |
|---|---|---|
| Sent, never opened | +4 days | Fresh subject line, same offer |
| Opened, never clicked | +4 days from open | Short nudge, direct CTA |
| Clicked, no payment | +6–7 days from click | 25% off setup fee (£99 → £74.25), time-limited (consider 7-day code expiry) |
| Clicked + edited, no payment | +6–7 days from click | Same 25% offer, warmer tone — "your site's ready and waiting" |
| Still no action | +14–21 days from first send | Final nudge, discount still standing, then mark cold and stop |

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
| `website_status` | String | `no_website` / `has_website_dated` / `has_website_modern` |
| `score` | Float | 0–100 |
| `email` | String | |
| `email_source` | String | `facebook` / `companies_house` / `web_search` |
| `email_found` | Boolean | |
| `phone` | String | From Places API — no separate discovery needed |
| `token` | String | UUID for magic link |
| `account_created_at` | DateTime | |
| `screenshot_url`, `gif_url` | String | |
| `funnel_stage` | String | `sourced` / `gated` / `scored` / `email_found` / `awaiting_approval` / `approved` / `rejected` / `sent` / `opened` / `clicked` / `paid` / `cold` |
| `approval_status` | String | `pending` / `yes` / `no` |
| `approved_at` | DateTime | |
| `email_version_sent`, `sms_version_sent` | String | |
| `touch_count` | Integer | |
| `discount_code`, `discount_expiry` | String/DateTime | |
| `sent_at`, `opened_at`, `clicked_at`, `paid_at` | DateTime | |
| `sms_sent_at` | DateTime | |
| `sms_delivered` | Boolean | |
| `email_unsubscribed` | Boolean | Tracked separately from SMS |
| `sms_unsubscribed` | Boolean | Tracked separately from email |

---

## 14. Build Order

Two tracks run in parallel — sourcing/approval has no dependency on sending infrastructure.

### Track A — Sourcing & Approval (start immediately)

1. Google Cloud billing + Places API (Enterprise tier) — quick setup
2. Build: sourcing → gating → website check → scoring → email discovery → database schema → Admin Dashboard approval queue (Section 5a/17)
3. Run Task A once (manually is fine initially) to seed the first batch — start reviewing/approving prospects in the Tinder UI immediately, building an approved (`yes`) backlog ready to go the moment sending is live
4. Once comfortable, wrap Task A as a proper nightly schedule

### Track B — Sending Infrastructure (start immediately, runs in background)

1. Cloudflare subdomain + SPF/DKIM/DMARC records — add now, let DNS propagate (24–48hrs)
2. Resend account set up against the subdomain
3. Esendex (or Plivo) account + start UK sender ID / SMS registration — longest lead time, start early
4. Google Postmaster Tools + Microsoft SNDS registration
5. Build: generation hookup → GIF capture → magic link/auth → email + SMS templates → follow-up sequence logic → circuit-breaker (Section 10b)
6. QA Gate (Section 12): test-inbox sends + spam-score checks on every template until approved

### Merge point

Once Track B is ready (DNS clean, templates QA-passed) — Task B goes live pulling directly from the backlog Track A has already been building. The first real send starts with however many `yes` prospects have already accumulated, prioritized by predicted conversion (Section 5b).

---

## 15. Deliverability

The biggest risk to this pipeline is emails landing in spam, not generation cost. Priority order:

1. **Domain warm-up ramp:** start at 5–10/day for the first 1–2 weeks, increase by ~5–10/day each following week rather than jumping straight to 20/day. Brand-new sending domains have zero reputation with Gmail/Outlook.
2. **SPF/DKIM/DMARC must all pass and align.** Start DMARC at `p=none` (monitoring only) to catch misconfigurations without risking blocked mail, tighten later once confirmed clean.
3. **Healthy text-to-image ratio.** An email that's mostly a GIF with minimal real text is a classic spam signal — ensure genuine substantial body copy alongside the GIF.
4. **Cautious with sales-trigger language** in the initial cold email — discount/urgency phrasing is more spam-filter-sensitive on a first touch than in follow-ups to someone who's already engaged.
5. **Keep bounce rate near-zero.** A bounce damages sender reputation more than almost anything else — another reason the "never guess an email" rule from Section 4 matters for deliverability, not just accuracy.
6. **Monitor reputation directly:** register the sending domain with Google Postmaster Tools (free) and Microsoft SNDS (free) to see spam-rate and reputation data directly.
7. **Extend the QA Gate** (Section 12) to include a spam-score check — a free tool like mail-tester.com sends back a spam score and specific flagged issues before a template goes live.
8. **Unsubscribe link must be present and honored immediately** — required for compliance, and reduces spam complaints.
9. **SMS deliverability:** use a UK-registered sender ID via a reputable provider (Esendex/FireText/Plivo) rather than the cheapest "grey route" option. Honor STOP replies immediately. Start SMS volume small and scale based on delivery-receipt rates.

---

## 17. Admin Dashboard

A single internal tool consolidating every human touchpoint in the pipeline:

1. **Approval queue** (Section 5a) — the Tinder-style yes/no review interface
2. **Funnel dashboard** — sent/opened/clicked/paid by channel (email/SMS) and by template version, feeding the Friday review
3. **Scoring reweight suggestions** (Section 5b) — weekly flagged divergences between predicted and actual approval/conversion rates, presented for the user to approve or dismiss, not auto-applied
4. **Template QA status** (Section 12) — which email/SMS variants have passed test-inbox + spam-score checks and are eligible to go live
5. **Deliverability health** (Section 15) — current email spam-rate and SMS delivery-rate status, particularly relevant given the circuit-breaker logic in Section 10b

Build the approval queue first (Section 5a) — it's on the critical path for anything to be sent at all. The other four panels can be added incrementally once there's enough data to populate them.

---

## 16. Working Assumptions (for tracking against real data)

| Metric | Assumption |
|---|---|
| Overall conversion (sent → paid) | 0.475% |
| Monthly churn | 5% |
| Steady-state ceiling at current volume (600 sends/month) | ~57 active subscribers / ~£1,425 MRR |
| Levers to break the ceiling | Increase outreach volume, improve conversion, reduce churn |
