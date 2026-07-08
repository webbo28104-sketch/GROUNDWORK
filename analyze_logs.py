#!/usr/bin/env python3
"""
Parse Railway logs from stdin and report prompt-caching cost metrics.

Usage
-----
Via Railway CLI (install once: npm install -g @railway/cli  then  railway login):

    # Pull last 2 000 log lines and analyse
    railway logs --limit 2000 | python analyze_logs.py

    # Filter to just the generation lines first (much faster on big logs)
    railway logs --limit 5000 | grep -E "turn usage|max_tokens hit" | python analyze_logs.py

    # Save to file, then analyse (useful if you want to re-run the script)
    railway logs --limit 5000 > /tmp/rail.log
    python analyze_logs.py < /tmp/rail.log

Via Railway dashboard
---------------------
  Dashboard → your service → Logs tab → search "turn usage" → copy all lines → paste into a file
  then:  python analyze_logs.py < pasted_lines.txt

What it parses
--------------
  INFO lines emitted by _run() in app.py:
    Generation <job_id> turn usage: in=N out=N cache_write=N cache_read=N
    Generation <job_id>: max_tokens hit, requesting continuation
"""

import re
import sys
from collections import defaultdict

# ── Pricing (Sonnet 4.6, $/token) ─────────────────────────────────────────────
INPUT_RATE       = 3.00  / 1_000_000
OUTPUT_RATE      = 15.00 / 1_000_000
CACHE_WRITE_RATE = 3.75  / 1_000_000
CACHE_READ_RATE  = 0.30  / 1_000_000

TURN_RE = re.compile(
    r'Generation (\S+) turn usage: in=(\d+) out=(\d+) cache_write=(\d+) cache_read=(\d+)'
)
CONTINUATION_RE = re.compile(
    r'Generation (\S+): max_tokens hit'
)

jobs = defaultdict(lambda: {
    'turns': 0,
    'continuations': 0,
    'input': 0,
    'output': 0,
    'cache_write': 0,
    'cache_read': 0,
})

for line in sys.stdin:
    m = TURN_RE.search(line)
    if m:
        job_id = m.group(1)
        j = jobs[job_id]
        j['turns']       += 1
        j['input']       += int(m.group(2))
        j['output']      += int(m.group(3))
        j['cache_write'] += int(m.group(4))
        j['cache_read']  += int(m.group(5))
        continue

    m = CONTINUATION_RE.search(line)
    if m:
        jobs[m.group(1)]['continuations'] += 1

if not jobs:
    print("No generation log lines found. Make sure you're piping Railway logs that contain:")
    print("  'Generation <id> turn usage: in=...'")
    print("Run: railway logs --limit 2000 | python analyze_logs.py")
    sys.exit(0)

# ── Per-job stats ─────────────────────────────────────────────────────────────

def costs(j):
    actual = (
        j['input']       * INPUT_RATE
        + j['output']      * OUTPUT_RATE
        + j['cache_write'] * CACHE_WRITE_RATE
        + j['cache_read']  * CACHE_READ_RATE
    )
    # What the same tokens would have cost with no caching at all
    uncached = (
        (j['input'] + j['cache_write'] + j['cache_read']) * INPUT_RATE
        + j['output'] * OUTPUT_RATE
    )
    return actual, uncached

print(f"\n{'Job ID':<22} {'Turns':>5} {'Conts':>5} {'Input':>8} {'Output':>8} {'CW':>8} {'CR':>8} {'Actual $':>9} {'No-cache $':>11} {'Saving':>8}")
print("-" * 105)

total_actual   = 0.0
total_uncached = 0.0
multi_turn     = 0

for job_id, j in sorted(jobs.items()):
    actual, uncached = costs(j)
    saving_pct = (uncached - actual) / uncached * 100 if uncached else 0.0
    total_actual   += actual
    total_uncached += uncached
    if j['turns'] > 1:
        multi_turn += 1
    print(
        f"  {job_id:<20} {j['turns']:>5} {j['continuations']:>5} "
        f"{j['input']:>8,} {j['output']:>8,} {j['cache_write']:>8,} {j['cache_read']:>8,} "
        f"  ${actual:>7.4f}   ${uncached:>8.4f}  {saving_pct:>+6.1f}%"
    )

n = len(jobs)
print("-" * 105)

total_saving = total_uncached - total_actual
avg_actual   = total_actual   / n
avg_uncached = total_uncached / n
pct_multi    = multi_turn / n * 100

print(f"\n  Jobs analysed           : {n}")
print(f"  Multi-turn (2+ turns)   : {multi_turn} ({pct_multi:.0f}%)")
print(f"  Avg cost/job  (actual)  : ${avg_actual:.4f}")
print(f"  Avg cost/job  (no-cache): ${avg_uncached:.4f}")
print(f"  Avg saving/job          : ${avg_actual - avg_uncached:+.4f}  ({(total_actual - total_uncached) / total_uncached * 100 if total_uncached else 0:+.1f}%)")
print(f"  Total saving across all : ${total_saving:.4f}")
print()
print("  Pricing: $3.00/MTok input · $15.00/MTok output · $3.75/MTok cache-write · $0.30/MTok cache-read")
print()

# ── Confidence note ───────────────────────────────────────────────────────────
if n < 10:
    note = f"Only {n} job(s) — directional only; run 25+ for reliable averages."
elif n < 25:
    note = f"{n} jobs — directional. ±10–15% error on multi-turn %. Run 25+ to tighten."
elif n < 100:
    note = f"{n} jobs — reasonable estimate (±5–10%). 100+ for publication-quality stats."
else:
    note = f"{n} jobs — solid dataset."
print(f"  Confidence: {note}")
print()
