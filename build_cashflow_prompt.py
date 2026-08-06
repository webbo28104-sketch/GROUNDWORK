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
    # sync_lag_hours: how stale each feed realistically is — a real Xero
    # bank feed isn't instantaneous, and a credit card typically lags a
    # current account. Turned into a real "as of" timestamp per account by
    # build_fixture_data() below, not shown as a static label, so it stays
    # honest about when each balance was actually last confirmed.
    {"id": "acc-current", "name": "Business Current Account", "institution": "Barclays", "type": "bank", "balance_gbp": 6814.37, "sync_lag_hours": 2},
    {"id": "acc-savings", "name": "Instant Access Savings", "institution": "Barclays", "type": "bank", "balance_gbp": 6193.52, "sync_lag_hours": 26},
    {"id": "acc-amex", "name": "Business Credit Card", "institution": "Amex", "type": "credit_card", "balance_gbp": -2007.89, "sync_lag_hours": 8},
]

# Recurring cost/liability templates — expanded into concrete dated bill
# line items by build_fixture_data() for every occurrence inside the
# forecast window. This is what "estimations on recurring expenses" means
# in practice: rent/payroll/subscriptions/asset finance/VAT don't show up
# as a single Xero bill each — Xero would surface rent/payroll/etc as a
# repeating bill template or bank rule (the real Xero integration, Phase 2,
# will read those directly) and VAT as a periodic liability building up on
# the VAT control account; the fixture approximates both by expanding
# simple monthly/quarterly templates here. frequency: "monthly" (day_of_month
# used) or "quarterly" (day_of_month used, one occurrence every 3 months).
FIXTURE_RECURRING = [
    {"id": "REC-RENT", "label": "Yard & office rent", "contact_name": "Riverside Estates Ltd", "amount_gbp": 1800.00, "day_of_month": 1, "frequency": "monthly"},
    {"id": "REC-PAYROLL", "label": "Payroll", "contact_name": "Staff wages", "amount_gbp": 7200.00, "day_of_month": 28, "frequency": "monthly"},
    {"id": "REC-SAAS", "label": "Software & subscriptions", "contact_name": "Various SaaS", "amount_gbp": 340.00, "day_of_month": 15, "frequency": "monthly"},
    {"id": "REC-VAN", "label": "Van lease", "contact_name": "Fleet Finance Ltd", "amount_gbp": 580.00, "day_of_month": 20, "frequency": "monthly"},
    # VAT: the quarterly liability directors get caught out by — it builds
    # up silently on the output side of every invoice and then takes one
    # lump sum out. Not itemised elsewhere in this fixture (no separate VAT
    # line on each invoice/bill — that level of modeling is a real Phase 2
    # Xero-data job), so this is a flat estimate standing in for "roughly
    # what HMRC will want this quarter."
    {"id": "REC-VAT", "label": "VAT payment", "contact_name": "HMRC — VAT", "amount_gbp": 6100.00, "day_of_month": 7, "frequency": "quarterly"},
]


def _xero_link(section: str, record_id: str) -> str:
    """Placeholder Xero deep-link — the real Xero integration (Phase 2)
    would read the customer's actual tenant short code and record GUID
    back from the API and build a real https://go.xero.com/... URL; this
    stands in with the same shape so the UI hook (a "View in Xero" link on
    every bill/invoice/quote) is already wired and just needs a real URL
    swapped in once that integration exists — see xero_integration.py."""
    return f"https://go.xero.com/{section}/View.aspx?InvoiceID={record_id}"


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
    window — e.g. two payroll runs and two rent payments over 60 days, or
    one VAT payment every third month."""
    horizon = today + timedelta(days=forecast_days)
    expanded = []
    for tmpl in FIXTURE_RECURRING:
        step = 3 if tmpl.get("frequency") == "quarterly" else 1
        month_offset = 0
        while True:
            occurrence = _add_months(today.replace(day=1), month_offset, tmpl["day_of_month"])
            if occurrence > horizon:
                break
            if occurrence.date() > today.date():
                occurrence_id = f"{tmpl['id']}-{occurrence.strftime('%Y%m')}"
                expanded.append({
                    "id": occurrence_id,
                    "contact_name": f"{tmpl['label']} — {occurrence.strftime('%b')}",
                    "amount_gbp": tmpl["amount_gbp"],
                    "due_date": occurrence.strftime("%Y-%m-%d"),
                    "recurring": True,
                    "xero_url": _xero_link("AccountsPayable", occurrence_id),
                })
            month_offset += step
    return expanded


def build_fixture_data(today: datetime = None, forecast_days: int = 60,
                        excluded_account_ids: set = None, safe_balance_gbp: float = -5000.0) -> dict:
    """Builds the full fixture dataset fresh, relative to `today` — call
    this rather than reaching for a frozen constant wherever the caller can
    (cashflow_routes.py's admin-preview endpoints, chatbot.py's fixture
    path) so recurring-expense dates and overdue/upcoming framing stay
    accurate no matter when the admin preview is actually opened.

    excluded_account_ids: FIXTURE_ACCOUNTS ids to leave out of
    current_balance_gbp — the "unselect a bank account or credit card"
    control in the admin preview's accounts panel.

    safe_balance_gbp: the runway/traffic-light threshold — defaults to
    -£5,000 standing in for a modest overdraft facility, NOT £0, since
    "days until literally zero" is rarely the number that matters to a
    director; see forecast_engine.CashFlowEngine's docstring.

    confirmed_invoices vs pipeline_quotes: confirmed_invoices are real,
    already-issued Xero invoices — certain (probability 100%), always
    counted, and what detect_overdue_invoices checks. pipeline_quotes are
    genuinely uncertain — not yet won, each with its own probability_pct,
    and what the Contract Pipeline UI's Won/Not-yet toggle acts on. Mixing
    the two in one list (as an earlier version of this fixture did) meant
    an already-issued overdue invoice sat in the same "toggle to include"
    UI as a speculative quote, which is a real-money/maybe-money conflation
    a director would immediately distrust."""
    today = today or datetime.utcnow()
    excluded_account_ids = excluded_account_ids or set()

    current_balance_gbp = sum(
        a["balance_gbp"] for a in FIXTURE_ACCOUNTS if a["id"] not in excluded_account_ids
    )
    accounts = [
        {**a, "as_of": (today - timedelta(hours=a["sync_lag_hours"])).isoformat()}
        for a in FIXTURE_ACCOUNTS
    ]

    def d(offset_days):
        return (today + timedelta(days=offset_days)).strftime("%Y-%m-%d")

    confirmed_invoices = [
        # Already issued, overdue — real money owed, not a maybe. CIS
        # deducted at source since the client is a main contractor.
        {"id": "INV-102", "contact_name": "Retail Fitout Co", "contact_email": "accounts@retailfitout.example.com",
         "amount_gbp": 15000.00, "due_date": d(-14), "cis_deduction_pct": 20, "probability_pct": 100,
         "xero_url": _xero_link("AccountsReceivable", "INV-102")},
    ]
    pipeline_quotes = [
        # Genuinely uncertain — each carries its own confidence, editable
        # in the UI (an admin/director INPUT, not something the app
        # infers — see cashflow_routes.py's pipeline probability-edit
        # endpoint). "Won"/"Lost" toggling overrides this to 100%/0%; left
        # "Open", a quote still counts at probability_pct in the Likely
        # band (see forecast_engine.calculate_scenario_bands).
        #
        # source: "quote" (Xero's Quotes API — a real quote object, not yet
        # accepted) vs "draft_invoice" (Xero's Invoices API with
        # Status=DRAFT — raised but not yet approved/sent, so still
        # editable and not yet a real debt). Both are pullable from Xero
        # and both represent "not confirmed yet," which is why they sit in
        # the same pipeline here — the source tag is just for the UI badge
        # and for pointing xero_url at the right section (a draft invoice
        # lives under Accounts Receivable, not the Quotes list).
        {"id": "QUOTE-101", "contact_name": "Kelvin Roofing Ltd", "contact_email": "office@kelvinroofing.example.com",
         "amount_gbp": 8200.00, "due_date": d(9), "probability_pct": 70, "source": "quote",
         "xero_url": _xero_link("Quotes", "QUOTE-101")},
        {"id": "QUOTE-103", "contact_name": "Marsh Construction", "contact_email": "accounts@marshconstruction.example.com",
         "amount_gbp": 4300.00, "due_date": d(26), "probability_pct": 40, "source": "quote",
         "xero_url": _xero_link("Quotes", "QUOTE-103")},
        {"id": "QUOTE-104", "contact_name": "Oakfield Developments", "contact_email": "finance@oakfielddev.example.com",
         "amount_gbp": 22000.00, "due_date": d(45), "probability_pct": 20, "source": "quote",
         "xero_url": _xero_link("Quotes", "QUOTE-104")},
        {"id": "DRAFT-201", "contact_name": "Pemberton Homes", "contact_email": "accounts@pembertonhomes.example.com",
         "amount_gbp": 6750.00, "due_date": d(20), "probability_pct": 80, "source": "draft_invoice",
         "xero_url": _xero_link("AccountsReceivable", "DRAFT-201")},
    ]
    one_off_bills = [
        {"id": "BILL-51", "contact_name": "Plant Hire Direct", "amount_gbp": 2100.00, "due_date": d(-1),
         "xero_url": _xero_link("AccountsPayable", "BILL-51")},
        {"id": "BILL-52", "contact_name": "Aggregates Supply Co", "amount_gbp": 6400.00, "due_date": d(14),
         "xero_url": _xero_link("AccountsPayable", "BILL-52")},
        {"id": "BILL-53", "contact_name": "HMRC — PAYE", "amount_gbp": 5200.00, "due_date": d(16),
         "xero_url": _xero_link("AccountsPayable", "BILL-53")},
    ]
    bills = one_off_bills + _expand_recurring(today, forecast_days)
    won_pipeline = [
        # A real, signed, staged contract — paid in three milestones, 5%
        # retention held on each until the defects liability period ends.
        # This is the fixture's demonstration of both features at once.
        {
            "id": "QUOTE-9", "contact_name": "Smith Contract", "contact_email": "accounts@smithcontract.example.com",
            "probability_pct": 100, "retention_pct": 5, "retention_release_days": 180,
            "xero_url": _xero_link("Contracts", "QUOTE-9"),
            "milestones": [
                {"label": "deposit", "amount_gbp": 4000.00, "date": d(12)},
                {"label": "mid-stage", "amount_gbp": 4000.00, "date": d(34)},
                {"label": "completion", "amount_gbp": 4000.00, "date": d(50)},
            ],
        },
    ]

    return {
        "current_balance_gbp": current_balance_gbp,
        "safe_balance_gbp": safe_balance_gbp,
        "forecast_days": forecast_days,
        "accounts": accounts,
        "confirmed_invoices": confirmed_invoices,
        "invoices": pipeline_quotes,  # kept as "invoices" — the key name cashflow_routes.py's pipeline toggle already reads
        "bills": bills,
        "won_pipeline": won_pipeline,
    }


# Frozen default (today = actual import time) — kept for call sites that
# don't need per-request freshness. Prefer build_fixture_data() directly
# wherever "today" genuinely matters (see its docstring).
FIXTURE_INPUT = build_fixture_data()
