# Cold Email Evidence Library

Grounding reference for `outreach/variant_optimizer_job.py`. Every new variant the system generates must cite either a principle from this library, or a documented internal finding from Groundwork's own data (see Section 3). No variant may be generated from unconstrained creativity alone.

Sources: Woodpecker (20M+ email analysis), Apollo, Overloop, Instantly's 2026 Benchmark Report, Martal, Autobound, Backlinko subject-line research, Reachly. All figures below are cross-referenced across 2+ independent sources where possible.

## 1. Established Principles (external research)

**Length**
- Optimal body length: 50-125 words; under 80 words for first-touch specifically
- 200+ words: reply rate falls to ~3.9%; 300+ words: collapses to ~2.1%
- Structure: 2-3 short paragraphs, 1-2 sentences each — visual whitespace outperforms a single dense block at equal word count

**Subject lines**
- 4-7 words is the optimal range; 5 words is the peak performer in isolated studies
- 36-50 character range gets ~32.7% more replies than very short subject lines (Backlinko)
- Question-format subject lines lift open rates by ~21%
- Avoid AI-sounding clichés ("Quick question," "Thought this might be useful") — now overused enough to signal automation
- Specific/contextual (references the business, their trade, their situation) outperforms generic

**Single CTA**
- Every additional CTA measurably reduces clarity and reply rate
- Specific, low-commitment asks ("Worth 15 minutes this week?") outperform vague ones ("Let's talk")
- Limit to one link per email where possible

**Personalization**
- Deep, context-specific personalization roughly doubles reply rate vs. generic (Sopro: 18% vs 9%)
- Fake/generic personalization ("Loved your recent work") reads as automated and damages trust — worse than no personalization attempt
- Personalized subject + generic body creates a mismatch that hurts conversion after the open

**Follow-ups**
- A single follow-up increases total replies by ~65.8%
- 58% of all replies come from the first email; the remaining 42% come from follow-ups — stopping at one email leaves nearly half of potential replies on the table
- Optimal sequence length: 4-5 touchpoints (some sources extend to 7); under 4 gives up too early, beyond 7 shows diminishing returns
- Each follow-up should change angle, not repeat the same ask

**Send timing**
- Thursday mornings, 9-11am: highest open rate (~44%) in aggregate data
- Tuesday: highest overall engagement
- Weekends and after-hours: consistently underperform

**List quality / deliverability**
- Bounce rate under 2% is healthy; above 5% risks ESP throttling/suspension
- If open rate is below 30%, the underlying issue is almost always deliverability infrastructure, not copy — fix that before optimizing subject lines
- (This directly matches Groundwork's own finding: `web_search`-sourced emails bounced at 50% vs. 0% for `own_website` — external research and internal data agree list quality dominates.)

**Benchmarks for context**
- Average cold email reply rate: 3.43%. 5%+ is good, 10%+ is excellent, well-targeted/personalized campaigns can reach 15-25%.

## 2. Constraints that override general best practice (Groundwork-specific, non-negotiable)

These exist regardless of what general research suggests, and no generated variant may violate them:
- Pre-click stages (Initial, Follow-up A, Follow-up B) must never claim the site is already "built" — only post-click stages (C, D) may
- No spam-trigger words as a standalone claim ("free" used bare, "act now," excessive urgency language) — reinforced by, not contradicted by, the research above
- Single-column, mobile-first plain structure (matches the "short paragraphs" finding directly)
- UK-specific: STOP/unsubscribe language and List-Unsubscribe header must be present on every send, non-negotiable regardless of variant

## 3. Internal Findings Log (populated over time by the optimizer job)

Format for each entry the job adds: **Finding** (what was observed) → **Sample size** → **Isolated variable** → **Adaptation tested** → **Rationale**. This is what "adaptation of something found to work" must cite — no entry may be added below the minimum sample threshold (30 per comparison group, matching Section 5b's existing standard).

**Architecture note:** `outreach/variant_optimizer_job.py` runs as a Railway Cron service — an ephemeral container with no git credentials wired (same constraint documented in `CLAUDE.md` for the nightly discovery routine). It cannot durably edit and commit this file from production. Findings are instead written to the `EvidenceFinding` database table in the same shape as below, and rendered live as this section's real content at `/admin/variants`. This file's copy of Section 3 stays a static stub in git; the database table is the actual, current Section 3 — treat `/admin/variants` as the source of truth, not this file.

*(Empty at creation in git — first entries will be added to the database once real variant data accumulates; see the architecture note above for why they don't land here.)*
