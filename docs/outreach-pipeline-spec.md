# Groundwork Outreach Pipeline — Build Spec

## Goal

Automated daily pipeline: source UK trade businesses → qualify → generate a personalized site preview → email outreach → follow-up sequence → convert to paying customer. Target: starts at 5–10 emails/day, compounding weekly with no fixed ceiling — fully automated selection, no manual approval gate.

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

**⚠️ OPEN ISSUE — needs reconfiguration before scaling volume**: the checklist below currently judges only *visual* staleness (layout, design cues, CTA presence, stale content signals). It does not check *content depth* — several businesses tagged `dated` in review have turned out to have more detailed, complete sites (more service pages, real project portfolios, fuller copy) than Groundwork's generator currently produces. Pitching "we'll upgrade you" to a business whose existing site is actually more substantial than the replacement undermines the whole approach. The check needs a second dimension — comparing content completeness against what the generator actually delivers — not just visual polish, so outreach only targets genuinely sub-par sites, not merely visually outdated ones with real substance behind them.

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

Take the top N by score from the day's qualified, email-found pool, where N is the current ramp allowance (see Section 15). Selection is automated — no manual approval step. No regional/trade caps.

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

### SMS circuit-breaker

- **Monitor:** Twilio delivery receipts (delivery rate as a rolling daily %) and STOP reply rate
- **Trigger:** delivery rate drops more than 10 percentage points from the prior-week baseline, OR opt-out rate spikes above 2% in a single day
- **While triggered:** pause SMS sends; do not fall back to email (the channels are independent — a degraded SMS channel is not a reason to push more email volume)
- **Recovery:** delivery rate returns to within 5 percentage points of baseline for 5 consecutive days; opt-out rate flat or falling. Resume SMS from the week-1 floor of the SMS ramp (Section 15).

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
| `google_photos_count` | Integer | Photo count from Places API — proxy for how professionally the business presents itself online |
| `opening_hours_complete` | Boolean | Whether Places API returns complete opening hours — signal of profile investment |
| `website_status` | String | `no_website` / `has_website_dated` / `has_website_modern` |
| `vision_flag_layout` | Boolean | Vision checklist item 1: fixed/non-responsive layout |
| `vision_flag_design` | Boolean | Vision checklist item 2: outdated design cues |
| `vision_flag_cta` | Boolean | Vision checklist item 3: no clear call-to-action |
| `vision_flag_content` | Boolean | Vision checklist item 4: stale content |
| `vision_flag_reviews` | Boolean | Vision checklist item 5: no reviews shown despite having Google reviews |
| `vision_flag_load` | Boolean | Vision checklist item 6: fails to load / times out |
| `score` | Float | 0–100 |
| `email` | String | |
| `email_source` | String | `facebook` / `checkatrade` / `yell` / `directory` / `web_search` |
| `email_domain_type` | String | `business` (domain matches trade name or is a custom domain) / `personal` (gmail, hotmail, yahoo, etc.) — affects predicted responsiveness |
| `email_found` | Boolean | |
| `phone` | String | From Places API — no separate discovery needed |
| `competitor_density` | Integer | Count of same-trade prospects already sourced from this postcode area — high density may dilute response rate in a local market |
| `token` | String | UUID for magic link |
| `account_created_at` | DateTime | |
| `screenshot_url`, `gif_url` | String | |
| `funnel_stage` | String | `sourced` / `queued` / `awaiting_approval` / `qualified_no_email` / `sent` / `opened` / `clicked` / `paid` / `cold` |
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
| `email_unsubscribed` | Boolean | Tracked separately from SMS |
| `sms_unsubscribed` | Boolean | Tracked separately from email |

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

### Dynamic send ramp (email)

No fixed ceiling. Volume compounds weekly as long as email health metrics stay clean:

| Period | Daily email volume |
|---|---|
| Week 1 | 5–10/day |
| Week 2 | 15–25/day |
| Week 3 | 30–50/day |
| Week 4+ | double the prior week's volume each week |

**Advancement trigger:** spam rate (Postmaster Tools) stays below 0.1% for the full preceding week. If spam rate hits 0.1%, hold volume flat at the current level for another week before trying to advance. If it crosses the circuit-breaker threshold (Section 10b), pause email entirely and reset to week-1 floor on resume.

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
