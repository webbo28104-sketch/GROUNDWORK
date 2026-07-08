"""
Prompt-caching before/after benchmark for Groundwork's generation loop.

Runs 3 representative multi-service trade business inputs through the exact
same messages structure as _run() in app.py, once WITHOUT caching and once
WITH caching, then prints a side-by-side cost comparison.

Usage:
    ANTHROPIC_API_KEY=sk-ant-... python test_caching.py

Costs real API tokens.  Each full generation is ~$0.30-0.60 depending on
length and number of tool-use turns.  3 inputs × 2 modes = ~$2-4 total.

Set DRY_RUN=1 to print the prompts and exit without calling the API.
"""

import os
import sys
import time
import json
import textwrap
from dataclasses import dataclass, field

import anthropic

sys.path.insert(0, os.path.dirname(__file__))
from build_prompt import build_prompt

# ── Pricing (Sonnet 4.6, $/million tokens) ───────────────────────────────────
INPUT_MTK      = 3.00    # standard input
OUTPUT_MTK     = 15.00   # output
CACHE_WRITE_MTK = 3.75   # cache write (25% premium)
CACHE_READ_MTK  = 0.30   # cache read (90% discount)

DRY_RUN = os.environ.get("DRY_RUN") == "1"

# ── Test inputs ───────────────────────────────────────────────────────────────
# Deliberately complex multi-service businesses — historically the kind most
# likely to hit max_tokens and need a continuation turn.

TEST_INPUTS = [
    {
        "label": "Apex Roofing Solutions (large commercial roofer)",
        "form": {
            "business_name": "Apex Roofing Solutions Ltd",
            "trade": "Roofing contractor",
            "location": "Birmingham",
            "coverage_area": "West Midlands, Worcestershire, Warwickshire",
            "phone": "0121 456 7890",
            "email": "info@apexroofing.co.uk",
            "logo_uploaded": False,
            "portfolio_uploaded": False,
            "work_split": "20% domestic / 80% commercial",
            "craft_prestige": "mid",
            "team_size": "company",
            "large_commercial_contracts": True,
            "urgency": "low",
            "years_trading": "18",
            "claimed_accreditations": "NFRC member, Velux Certified, Firestone certified flat roof installer",
            "claimed_projects": "Coventry Cathedral roofing restoration, multiple NHS Trusts, Severn Trent Water facilities",
            "other_notes": (
                "Services: pitched roof replacement, flat roof systems (GRP/EPDM/felt), "
                "roof repairs, lead work, guttering, fascias/soffits, commercial cladding, "
                "Velux skylights, green roofs, heritage lime mortar pointing. "
                "24/7 emergency call-out. All work guaranteed 10 years. "
                "Fleet of 12 vehicles, own scaffold division."
            ),
        },
    },
    {
        "label": "Thames Valley Plumbing & Heating (full-service M&E)",
        "form": {
            "business_name": "Thames Valley Plumbing & Heating",
            "trade": "Plumbing and heating engineer",
            "location": "Reading",
            "coverage_area": "Berkshire, Oxfordshire, Hampshire, Surrey",
            "phone": "0118 987 6543",
            "email": "enquiries@tvph.co.uk",
            "logo_uploaded": False,
            "portfolio_uploaded": False,
            "work_split": "35% domestic / 65% commercial",
            "craft_prestige": "mid",
            "team_size": "small",
            "large_commercial_contracts": True,
            "urgency": "high",
            "years_trading": "22",
            "claimed_accreditations": "Gas Safe registered, CIPHE member, Worcester Bosch Accredited Installer, Vaillant Advanced Installer",
            "claimed_projects": "Oracle HQ Reading, multiple Reading University buildings, private housing estates for Bellway Homes",
            "other_notes": (
                "Services: boiler installation/service/repair, central heating, underfloor heating, "
                "bathroom and en-suite installation, wet room installation, hot water cylinders, "
                "unvented systems, LPG/oil boilers, heat pumps, radiators, "
                "landlord gas safety certificates, commercial boiler plant rooms, "
                "CCTV drain surveys, drain unblocking, 24hr emergency plumber. "
                "All engineers DBS checked. Finance available."
            ),
        },
    },
    {
        "label": "Pemberton Building Contractors (large general builder)",
        "form": {
            "business_name": "Pemberton Building Contractors",
            "trade": "Building contractor",
            "location": "Manchester",
            "coverage_area": "Greater Manchester, Cheshire, Lancashire",
            "phone": "0161 234 5678",
            "email": "build@pembertonbc.co.uk",
            "logo_uploaded": False,
            "portfolio_uploaded": False,
            "work_split": "40% domestic / 60% commercial",
            "craft_prestige": "high",
            "team_size": "company",
            "large_commercial_contracts": True,
            "urgency": "low",
            "years_trading": "31",
            "claimed_accreditations": "FMB Master Builder, NHBC registered, Constructionline Gold, Safecontractor",
            "claimed_projects": "MediaCityUK fit-out works, Manchester Metropolitan University refurbishment, Salford Quays residential development",
            "other_notes": (
                "Services: house extensions (single/double/wrap-around), loft conversions (dormer/hip-to-gable/Velux), "
                "basement conversions, full property refurbishments, kitchen extensions, orangeries, "
                "garage conversions, new build residential, commercial fit-out, "
                "period property restoration, structural repairs, underpinning, "
                "planning application support, project management. "
                "In-house architectural technician. Own plant hire. RIBA-affiliated."
            ),
        },
    },
]

# ── Metric tracking ───────────────────────────────────────────────────────────

@dataclass
class TurnUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0

@dataclass
class RunResult:
    label: str
    mode: str       # "uncached" | "cached"
    turns: int = 0
    usage: list = field(default_factory=list)   # list[TurnUsage]
    html_length: int = 0
    elapsed_s: float = 0.0
    error: str = ""

    @property
    def total_input(self):
        return sum(u.input_tokens for u in self.usage)

    @property
    def total_output(self):
        return sum(u.output_tokens for u in self.usage)

    @property
    def total_cache_write(self):
        return sum(u.cache_write_tokens for u in self.usage)

    @property
    def total_cache_read(self):
        return sum(u.cache_read_tokens for u in self.usage)

    @property
    def cost_usd(self):
        return (
            self.total_input      * INPUT_MTK       / 1_000_000
            + self.total_output   * OUTPUT_MTK      / 1_000_000
            + self.total_cache_write * CACHE_WRITE_MTK / 1_000_000
            + self.total_cache_read  * CACHE_READ_MTK  / 1_000_000
        )


# ── Core generation runner ────────────────────────────────────────────────────

def run_generation(label: str, form: dict, use_cache: bool, client: anthropic.Anthropic) -> RunResult:
    result = RunResult(label=label, mode="cached" if use_cache else "uncached")
    t0 = time.time()

    prompt = build_prompt(form)
    content = [
        {
            "type": "text",
            "text": prompt,
            **({"cache_control": {"type": "ephemeral"}} if use_cache else {}),
        }
    ]
    messages = [{"role": "user", "content": content}]
    accumulated_text = ""

    try:
        for turn_num in range(15):
            resp = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=16000,
                tools=[{"type": "web_search_20250305", "name": "web_search"}],
                messages=messages,
            )

            u = resp.usage
            turn_usage = TurnUsage(
                input_tokens=getattr(u, "input_tokens", 0),
                output_tokens=getattr(u, "output_tokens", 0),
                cache_write_tokens=getattr(u, "cache_creation_input_tokens", 0),
                cache_read_tokens=getattr(u, "cache_read_input_tokens", 0),
            )
            result.usage.append(turn_usage)
            result.turns += 1

            print(f"  Turn {turn_num+1}: in={turn_usage.input_tokens} out={turn_usage.output_tokens} "
                  f"cw={turn_usage.cache_write_tokens} cr={turn_usage.cache_read_tokens} "
                  f"stop={resp.stop_reason}")

            for block in resp.content:
                if hasattr(block, "text"):
                    accumulated_text += block.text

            if resp.stop_reason == "end_turn":
                break

            # Serialize assistant content, optionally marking last text block for caching
            assistant_blocks = []
            last_text_idx = None
            for i, block in enumerate(resp.content):
                b = block.model_dump(exclude_none=True) if hasattr(block, "model_dump") else dict(block)
                if b.get("type") == "text":
                    last_text_idx = i
                assistant_blocks.append(b)
            if use_cache and last_text_idx is not None:
                assistant_blocks[last_text_idx]["cache_control"] = {"type": "ephemeral"}
            messages.append({"role": "assistant", "content": assistant_blocks})

            if resp.stop_reason == "max_tokens":
                messages.append({"role": "user", "content": [{"type": "text", "text": (
                    "The response was cut off by the token limit. Please continue the HTML from exactly "
                    "where you stopped — complete all remaining open tags and sections without repeating "
                    "any content already written."
                )}]})
                continue

            tool_results = [
                {"type": "tool_result", "tool_use_id": b["id"], "content": ""}
                for b in assistant_blocks
                if b.get("type") == "tool_use"
            ]
            if tool_results:
                messages.append({"role": "user", "content": tool_results})
            else:
                break

    except Exception as exc:
        result.error = str(exc)
        print(f"  ERROR: {exc}")

    lower = accumulated_text.lower()
    idx = lower.find("<!doctype html>")
    html = accumulated_text[idx:] if idx != -1 else accumulated_text
    result.html_length = len(html)
    result.elapsed_s = time.time() - t0
    return result


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    if DRY_RUN:
        for t in TEST_INPUTS:
            p = build_prompt(t["form"])
            print(f"\n{'='*60}")
            print(f"INPUT: {t['label']}")
            print(f"Prompt length: {len(p)} chars (~{len(p)//4} tokens)")
            print(textwrap.shorten(p, 400))
        return

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY not set")
        sys.exit(1)

    client = anthropic.Anthropic(api_key=api_key)
    all_results: list[RunResult] = []

    for mode, use_cache in [("UNCACHED (before)", False), ("CACHED (after)", True)]:
        print(f"\n{'='*70}")
        print(f"MODE: {mode}")
        print(f"{'='*70}")
        for t in TEST_INPUTS:
            print(f"\n  ── {t['label']} ──")
            r = run_generation(t["label"], t["form"], use_cache, client)
            all_results.append(r)
            print(f"  Done: {r.turns} turns, {r.html_length:,} HTML chars, ${r.cost_usd:.4f}, {r.elapsed_s:.0f}s")
            if r.error:
                print(f"  Error: {r.error}")

    # ── Comparison table ──────────────────────────────────────────────────────
    print(f"\n\n{'='*70}")
    print("BEFORE / AFTER COMPARISON")
    print(f"{'='*70}")
    print(f"{'Input':<38} {'Mode':<10} {'Turns':>5} {'Input tok':>10} {'Out tok':>8} {'CW tok':>8} {'CR tok':>8} {'Cost $':>8}")
    print("-" * 103)

    total_before = 0.0
    total_after  = 0.0
    savings_pct_list = []

    for i, t in enumerate(TEST_INPUTS):
        before = next(r for r in all_results if r.label == t["label"] and r.mode == "uncached")
        after  = next(r for r in all_results if r.label == t["label"] and r.mode == "cached")
        short  = t["label"][:36]
        for r in (before, after):
            print(f"  {short:<36} {r.mode:<10} {r.turns:>5} {r.total_input:>10,} {r.total_output:>8,} "
                  f"{r.total_cache_write:>8,} {r.total_cache_read:>8,} {r.cost_usd:>8.4f}")
        pct = (before.cost_usd - after.cost_usd) / before.cost_usd * 100 if before.cost_usd else 0
        savings_pct_list.append(pct)
        total_before += before.cost_usd
        total_after  += after.cost_usd
        print(f"  {'':36} {'saving':>10}                                    "
              f"{'':>8} {pct:>+7.1f}%")
        print()

    avg_pct = sum(savings_pct_list) / len(savings_pct_list) if savings_pct_list else 0
    print("-" * 103)
    print(f"  {'TOTAL':36} {'before':>10} {'':>5} {'':>10} {'':>8} {'':>8} {'':>8} {total_before:>8.4f}")
    print(f"  {'':36} {'after':>10} {'':>5} {'':>10} {'':>8} {'':>8} {'':>8} {total_after:>8.4f}")
    print(f"  {'':36} {'saving':>10} {'':>5} {'':>10} {'':>8} {'':>8} {'':>8} {(total_before-total_after):>8.4f}")
    print(f"\n  Average cost reduction: {avg_pct:+.1f}%")
    print(f"  Pricing used: ${INPUT_MTK}/MTok in, ${OUTPUT_MTK}/MTok out, "
          f"${CACHE_WRITE_MTK}/MTok cache-write, ${CACHE_READ_MTK}/MTok cache-read")

    # ── Quality check ─────────────────────────────────────────────────────────
    print(f"\n{'='*70}")
    print("HTML OUTPUT QUALITY CHECK (length comparison — not a functional diff)")
    print(f"{'='*70}")
    for t in TEST_INPUTS:
        before = next(r for r in all_results if r.label == t["label"] and r.mode == "uncached")
        after  = next(r for r in all_results if r.label == t["label"] and r.mode == "cached")
        diff   = after.html_length - before.html_length
        print(f"  {t['label'][:50]}")
        print(f"    Before: {before.html_length:>8,} chars   After: {after.html_length:>8,} chars   Δ {diff:+,}")

    print("\nDone.")


if __name__ == "__main__":
    main()
