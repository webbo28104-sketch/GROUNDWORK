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
from datetime import datetime, timedelta

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
#
# Dates are generated relative to "today" (see build_fixture_data below)
# rather than hardcoded, so the admin preview never goes stale/unrealistic
# as real time passes — a hardcoded "due 2026-08-15" would silently drift
# into the past and stop demonstrating anything. Amounts are tuned so the
# DEFAULT view (nothing toggled) shows a real, non-trivial runway — a flat
# "always green, always safe" demo gives the founder nothing to react to
# when deciding whether the toggle/what-if mechanics are worth shipping.

FIXTURE_ACCOUNTS = [
    # balance_gbp sums to 11,000 — tuned (alongside the bills/recurring
    # figures below) so the DEFAULT view (nothing toggled) shows a real
    # runway that turns green once the pipeline quotes are toggled won —
    # a flat "always green" or "always red" demo gives nothing to react to
    # when deciding whether the toggle/what-if mechanics earn their keep.
    # type drives the icon/grouping in the accounts panel, not the math (a
    # credit card's negative balance already nets out correctly either way).
    {"id": "acc-current", "name": "Business Current Account", "institution": "Barclays", "type": "bank", "balance_gbp": 6800.00},
    {"id": "acc-savings", "name": "Instant Access Savings", "institution": "Barclays", "type": "bank", "balance_gbp": 6200.00},
    {"id": "acc-amex", "name": "Business Credit Card", "institution": "Amex", "type": "credit_card", "balance_gbp": -2000.00},
]

# Recurring cost templates — expanded into concrete dated bill line items
# by build_fixture_data() for every occurrence inside the forecast window.
# This is what "estimations on recurring expenses" means in practice: rent/
# payroll/subscriptions/asset finance don't show up as a single Xero bill
# each — Xero would surface them as a repeating bill template or bank rule,
# which the real Xero integration (Phase 2) will read directly; the fixture
# approximates that by expanding a simple monthly template here.
FIXTURE_RECURRING = [
    {"id": "REC-RENT", "label": "Yard & office rent", "contact_name": "Riverside Estates Ltd", "amount_gbp": 1800.00, "day_of_month": 1},
    {"id": "REC-PAYROLL", "label": "Payroll", "contact_name": "Staff wages", "amount_gbp": 7200.00, "day_of_month": 28},
    {"id": "REC-SAAS", "label": "Software & subscriptions", "contact_name": "Various SaaS", "amount_gbp": 340.00, "day_of_month": 15},
    {"id": "REC-VAN", "label": "Van lease", "contact_name": "Fleet Finance Ltd", "amount_gbp": 580.00, "day_of_month": 20},
]


def _add_months(d: datetime, n: int, day_of_month: int) -> datetime:
    month = d.month - 1 + n
    year = d.year + month // 12
    month = month % 12 + 1
    import calendar
    day = min(day_of_month, calendar.monthrange(year, month)[1])
    return d.replace(year=year, month=month, day=day)


def _expand_recurring(today: datetime, forecast_days: int) -> list:
    """One concrete, dated bill entry per occurrence of each recurring
    template that falls strictly after today and within the forecast
    window — e.g. two payroll runs and two rent payments over 60 days."""
    horizon = today + timedelta(days=forecast_days)
    expanded = []
    for tmpl in FIXTURE_RECURRING:
        month_offset = 0
        while True:
            occurrence = _add_months(today.replace(day=1), month_offset, tmpl["day_of_month"])
            if occurrence > horizon:
                break
            if occurrence.date() > today.date():
                expanded.append({
                    "id": f"{tmpl['id']}-{occurrence.strftime('%Y%m')}",
                    "contact_name": f"{tmpl['label']} — {occurrence.strftime('%b')}",
                    "amount_gbp": tmpl["amount_gbp"],
                    "due_date": occurrence.strftime("%Y-%m-%d"),
                    "recurring": True,
                })
            month_offset += 1
    return expanded


def build_fixture_data(today: datetime = None, forecast_days: int = 60,
                        excluded_account_ids: set = None) -> dict:
    """Builds the full fixture dataset fresh, relative to `today` — call
    this rather than reaching for a frozen constant wherever the caller can
    (cashflow_routes.py's admin-preview endpoints, chatbot.py's fixture
    path) so recurring-expense dates and overdue/upcoming framing stay
    accurate no matter when the admin preview is actually opened.

    excluded_account_ids: FIXTURE_ACCOUNTS ids to leave out of
    current_balance_gbp — the "unselect a bank account or credit card"
    control in the admin preview's accounts panel."""
    today = today or datetime.utcnow()
    excluded_account_ids = excluded_account_ids or set()

    current_balance_gbp = sum(
        a["balance_gbp"] for a in FIXTURE_ACCOUNTS if a["id"] not in excluded_account_ids
    )

    def d(offset_days):
        return (today + timedelta(days=offset_days)).strftime("%Y-%m-%d")

    invoices = [
        {"id": "INV-101", "contact_name": "Kelvin Roofing Ltd", "amount_gbp": 8200.00, "due_date": d(9)},
        {"id": "INV-102", "contact_name": "Retail Fitout Co", "amount_gbp": 15000.00, "due_date": d(-14)},
        {"id": "INV-103", "contact_name": "Marsh Construction", "amount_gbp": 4300.00, "due_date": d(26)},
    ]
    one_off_bills = [
        {"id": "BILL-51", "contact_name": "Plant Hire Direct", "amount_gbp": 2100.00, "due_date": d(-1)},
        {"id": "BILL-52", "contact_name": "Aggregates Supply Co", "amount_gbp": 6400.00, "due_date": d(14)},
        {"id": "BILL-53", "contact_name": "HMRC — PAYE", "amount_gbp": 5200.00, "due_date": d(16)},
    ]
    bills = one_off_bills + _expand_recurring(today, forecast_days)
    won_pipeline = [
        {"id": "QUOTE-9", "contact_name": "Smith Contract", "amount_gbp": 12000.00, "due_date": d(34)},
    ]

    return {
        "current_balance_gbp": current_balance_gbp,
        "accounts": FIXTURE_ACCOUNTS,
        "invoices": invoices,
        "bills": bills,
        "won_pipeline": won_pipeline,
    }


# Frozen default (today = actual import time) — kept for call sites that
# don't need per-request freshness. Prefer build_fixture_data() directly
# wherever "today" genuinely matters (see its docstring).
FIXTURE_INPUT = build_fixture_data()
