"""
Groundwork Cashflow — DeepSeek forecast prompt.

Mirrors build_prompt.py's shape: a single build function that turns
structured input (here, Xero-shaped financial data) into the system/user
prompt pair sent to the model. Unlike build_prompt.py (Anthropic, site
generation), this targets DeepSeek's OpenAI-compatible chat completions
endpoint via forecast_engine.py.

Compliance note: each call processes exactly one customer's data for one
forecast request — nothing here aggregates data across customers or is
intended to train/fine-tune anything. That's a claim about how this
codebase calls the API, not something enforceable from the prompt text
itself; DeepSeek's own API terms govern their side of that.
"""
import json
from datetime import datetime

FORECAST_SYSTEM_PROMPT = """You are a cash flow forecasting assistant for UK construction subcontractors \
(groundworkers, roofers, scaffolders, and similar site-based trades).

Rules:
1. Use ONLY the data provided in the user message. Do not invent invoices, bills, or transactions.
2. Output MUST be valid JSON matching exactly this structure, nothing else:
{
  "projected_balance_gbp": number,
  "projection_date": "YYYY-MM-DD",
  "runway_days": number or null,
  "daily_series": [{"date": "YYYY-MM-DD", "in": number, "out": number, "balance": number}, ...],
  "money_in_60d_gbp": number,
  "money_out_60d_gbp": number,
  "summary_plain_english": string
}
3. daily_series must have one entry per day of the forecast period, in order, starting the day after "today".
4. Write summary_plain_english in plain English for a site-based tradesperson checking this on their phone. \
Never use accounting jargon — no EBITDA, gross margin, accruals, working capital, or similar terms.
5. runway_days is the number of days from today until the running balance first goes below zero, or null if it \
never does within the forecast period.
6. Base every number on simple addition/subtraction of the provided balance, invoices, bills, and won pipeline \
items landing on their due dates — do not apply any interest, fees, or assumptions not present in the data.
7. Output JSON only — no markdown fences, no commentary before or after it."""


def build_cashflow_prompt(current_balance_gbp: float, invoices: list, bills: list,
                           won_pipeline: list, forecast_days: int = 60,
                           today: datetime = None) -> tuple:
    """
    invoices / bills: list of {"id", "contact_name", "amount_gbp", "due_date" (YYYY-MM-DD)}
        — money coming in (invoices) / going out (bills), from Xero.
    won_pipeline: list of the same shape, drawn from CashflowPipelineItem rows
        where is_won is True — added to incoming on top of invoices.

    Returns (system_prompt, user_prompt).
    """
    today = today or datetime.utcnow()
    today_str = today.strftime("%Y-%m-%d")

    user_prompt = f"""Generate a {forecast_days}-day cash flow forecast for this UK construction subcontractor.

Today's date: {today_str}
Current bank balance: £{current_balance_gbp:,.2f}

Outstanding invoices (money coming in):
{json.dumps(invoices, indent=2)}

Outstanding bills (money going out):
{json.dumps(bills, indent=2)}

Won pipeline items — treat these as additional incoming money on their due date, on top of the invoices above:
{json.dumps(won_pipeline, indent=2)}

Produce the JSON forecast described in your instructions, covering {today_str} through \
{forecast_days} days ahead."""

    return FORECAST_SYSTEM_PROMPT, user_prompt


# --- Fixture data for testing without a live Xero connection (Phase 1) ---
FIXTURE_INPUT = {
    "current_balance_gbp": 18500.00,
    "invoices": [
        {"id": "INV-101", "contact_name": "Kelvin Roofing Ltd", "amount_gbp": 8200.00, "due_date": "2026-08-15"},
        {"id": "INV-102", "contact_name": "Retail Fitout Co", "amount_gbp": 15000.00, "due_date": "2026-07-23"},
        {"id": "INV-103", "contact_name": "Marsh Construction", "amount_gbp": 4300.00, "due_date": "2026-09-01"},
    ],
    "bills": [
        {"id": "BILL-51", "contact_name": "Plant Hire Direct", "amount_gbp": 2100.00, "due_date": "2026-08-05"},
        {"id": "BILL-52", "contact_name": "Aggregates Supply Co", "amount_gbp": 6400.00, "due_date": "2026-08-20"},
        {"id": "BILL-53", "contact_name": "HMRC — PAYE", "amount_gbp": 5200.00, "due_date": "2026-08-22"},
    ],
    "won_pipeline": [
        {"id": "QUOTE-9", "contact_name": "Smith Contract", "amount_gbp": 28000.00, "due_date": "2026-09-15"},
    ],
}
