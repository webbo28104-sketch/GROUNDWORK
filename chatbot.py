"""
Groundwork Cashflow — conversational assistant.

DeepSeek's OpenAI-compatible tool-calling API answers free-text questions
about a customer's cash flow. The model is never trusted to invent a
number itself — every figure it states comes back from a tool call into
forecast_engine.py's deterministic CashFlowEngine (see that file's
docstring for why the forecast math itself isn't an LLM call at all).
DeepSeek's only job here is deciding which tool answers the question and
phrasing the result in plain English.

Data source: real Xero-synced data (XeroConnection + CashflowPipelineItem)
once xero_integration.py (Phase 2, blocked on Xero Developer credentials)
is live; falls back to the same fixture data set the dashboard's
?fixture=1 mode uses otherwise, so the chatbot is fully testable and
demoable before that dependency is resolved — see _account_forecast_data().
"""
import os
import json
import logging
from datetime import datetime

from openai import OpenAI

from models import SessionLocal, Account, XeroConnection, CashflowPipelineItem, CashflowFunnelEvent
from forecast_engine import CashFlowEngine, _add_invoice_from_dict
from build_cashflow_prompt import FIXTURE_INPUT
from emails import send_admin_cashflow_accountant_request_email

logger = logging.getLogger("chatbot")

DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_API_BASE = os.environ.get("DEEPSEEK_API_BASE", "https://api.deepseek.com")
DEEPSEEK_CHAT_MODEL = os.environ.get("DEEPSEEK_CHAT_MODEL", "deepseek-chat")
MAX_HISTORY_MESSAGES = 10

SYSTEM_PROMPT = """You are the company's financial advisor inside Groundwork Cashflow — a UK construction \
subcontractor's dedicated, always-on adviser for their cash position, not a generic chatbot. You know this \
business's numbers cold and talk about them the way a trusted advisor would in a five-minute catch-up: warm, \
direct, and useful — never a lecture.

No accounting jargon — never use words like EBITDA, gross margin, accruals, or working capital.

FORMATTING — this is read on a phone between jobs, so structure matters as much as content:
- Open with a one-line, direct answer to what was actually asked. Do not bury it in a preamble.
- Never write a single dense paragraph. Break your answer into short paragraphs (1-3 sentences each) or a
  short bulleted list — whichever reads faster for the specific answer.
- Bold the numbers and dates that matter most (**£12,400**, **14 September**) so they're scannable at a glance.
- Keep the whole reply tight — a few short paragraphs or bullets, not an essay. If there's a lot to say,
  say the headline first and offer to go deeper rather than dumping everything at once.

Rules:
1. Only use numbers/dates returned by your tools — never invent or estimate a figure yourself.
2. Be specific: cite the actual amount and date a tool gave you, and say which scenario (best/likely/worst)
   it came from when relevant.
3. Be actionable: if you spot a problem (overdue invoice, cash dipping below the safe-balance threshold),
   say what to do about it — and mention the Chase button or the quote's probability if that's the lever.
4. If the customer describes a serious cash flow worry (can't make payroll, might miss a big bill,
   considering not taking on work because of cash), offer to connect them with a real accountant using
   the talk_to_accountant tool — don't wait to be asked."""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_forecast",
            "description": "Get the current 60-day cash flow forecast as three scenarios — best (every open quote lands), likely (each quote weighted by its win probability), and worst (no open quote lands) — each with its own projected balance, cash runway, and money in/out. Always mention which scenario a figure comes from when answering.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_overdue_invoices",
            "description": "Get every overdue invoice, with days overdue and priority.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_payment_risks",
            "description": "Get invoices whose payment terms exceed 60 days (payment risk).",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_scenario",
            "description": "Run a what-if scenario on the forecast — e.g. material costs rising, or payments being delayed.",
            "parameters": {
                "type": "object",
                "properties": {
                    "material_cost_change": {"type": "number", "description": "Fractional change in outgoing costs, e.g. 0.10 for +10%"},
                    "payment_delay": {"type": "integer", "description": "Days to delay every incoming payment by"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "talk_to_accountant",
            "description": "Request a consultation with an accountant — use when the customer has a real cash flow concern.",
            "parameters": {
                "type": "object",
                "properties": {"reason": {"type": "string", "description": "Why the customer wants to talk to an accountant"}},
                "required": ["reason"],
            },
        },
    },
]


def _client():
    if not DEEPSEEK_API_KEY:
        raise RuntimeError("DEEPSEEK_API_KEY is not configured")
    return OpenAI(api_key=DEEPSEEK_API_KEY, base_url=f"{DEEPSEEK_API_BASE}/v1")


def _account_forecast_data(db, account):
    """Real synced Xero data if a connection + pipeline rows exist;
    fixture data otherwise. current_balance_gbp has no real-data source yet
    (no bank-balance sync built — that's part of the same Phase 2 Xero
    dependency), so it falls back to the fixture figure even once a real
    connection exists, flagged via is_fixture so callers/copy can be
    honest about it if needed."""
    connection = db.query(XeroConnection).filter(
        XeroConnection.account_id == account.id, XeroConnection.status == "active"
    ).first()
    items = []
    if connection:
        items = db.query(CashflowPipelineItem).filter(CashflowPipelineItem.account_id == account.id).all()
    if not connection or not items:
        return FIXTURE_INPUT, True

    invoices = [
        {"id": i.xero_id, "contact_name": i.contact_name, "amount_gbp": i.amount_gbp,
         "due_date": i.due_date.strftime("%Y-%m-%d")}
        for i in items if i.xero_type == "invoice" and i.due_date
    ]
    won = [
        {"id": i.xero_id, "contact_name": i.contact_name, "amount_gbp": i.amount_gbp,
         "due_date": i.due_date.strftime("%Y-%m-%d")}
        for i in items if i.is_won and i.due_date
    ]
    return {
        "current_balance_gbp": FIXTURE_INPUT["current_balance_gbp"],
        "invoices": invoices, "bills": [], "won_pipeline": won,
    }, False


def _engine_from_data(data):
    """Builds a CashFlowEngine from any of: confirmed_invoices (certain,
    already-issued), invoices (pipeline quotes — may carry probability_pct/
    cis_deduction_pct/retention_pct), won_pipeline (confirmed contracts —
    may be staged via `milestones` instead of a single due_date). Uses
    forecast_engine's own _add_invoice_from_dict so every field this engine
    understands (CIS, retention, staging, probability) is honoured
    identically to the admin-preview dashboard endpoint — the chatbot's
    numbers must never diverge from what the chart is showing."""
    engine = CashFlowEngine(
        current_balance=data["current_balance_gbp"], forecast_days=60,
        safe_balance_gbp=data.get("safe_balance_gbp", 0.0),
    )
    for inv in data.get("confirmed_invoices", []) + data.get("invoices", []):
        _add_invoice_from_dict(engine, inv)
    for bill in data.get("bills", []):
        engine.add_bill(datetime.fromisoformat(bill["due_date"]), bill["amount_gbp"], bill.get("id", ""), bill.get("contact_name", ""))
    for item in data.get("won_pipeline", []):
        _add_invoice_from_dict(engine, item)
    return engine


def _engine_for_account(db, account):
    data, is_fixture = _account_forecast_data(db, account)
    return _engine_from_data(data), data


def _run_tool(tool_name, tool_args, db, account):
    if tool_name == "get_forecast":
        engine, _ = _engine_for_account(db, account)
        return engine.calculate_forecast()
    if tool_name == "get_overdue_invoices":
        engine, data = _engine_for_account(db, account)
        return {"overdue": engine.detect_overdue_invoices(data.get("confirmed_invoices", []) + data["invoices"])}
    if tool_name == "get_payment_risks":
        engine, data = _engine_for_account(db, account)
        return {"risks": engine.detect_payment_risks(data.get("confirmed_invoices", []) + data["invoices"])}
    if tool_name == "run_scenario":
        engine, _ = _engine_for_account(db, account)
        return engine.run_scenario(tool_args)
    if tool_name == "talk_to_accountant":
        reason = tool_args.get("reason", "")
        db.add(CashflowFunnelEvent(account_id=account.id, stage="accountant_requested"))
        db.commit()
        send_admin_cashflow_accountant_request_email(account.email, reason)
        return {"success": True, "message": "An accountant will contact you within 24 hours."}
    return {"error": f"unknown tool {tool_name!r}"}


def _run_tool_fixture(tool_name, tool_args, data):
    """Admin-preview equivalent of _run_tool — no DB, no Account. `data` is
    whatever cashflow_routes.py's /admin-preview/chat computed for THIS
    session (fresh build_fixture_data(), with the invoices list already
    narrowed to whichever pipeline items the admin has toggled won and the
    balance already reflecting any excluded accounts) — passed straight
    through as data_override rather than reaching for the frozen module-
    level FIXTURE_INPUT, so the chatbot's answers always match what the
    dashboard is showing right now, including any recurring-expense
    modeling (see build_cashflow_prompt.py) that's why this used to
    (incorrectly) say cash would "never" run out. talk_to_accountant is
    simulated (no real lead/email), since this path exists purely so the
    founder can click around a mock report before deciding this is worth
    committing at all."""
    engine = _engine_from_data(data)
    if tool_name == "get_forecast":
        # Bands, not a single line — the pipeline quotes in this dataset
        # carry probability_pct, so "the" forecast is actually a range
        # (best/likely/worst); see calculate_scenario_bands()'s docstring.
        return engine.calculate_scenario_bands()
    if tool_name == "get_overdue_invoices":
        confirmed = data.get("confirmed_invoices", [])
        return {"overdue": engine.detect_overdue_invoices(confirmed + data["invoices"] + data["won_pipeline"])}
    if tool_name == "get_payment_risks":
        return {"risks": engine.detect_payment_risks(data.get("confirmed_invoices", []) + data["invoices"])}
    if tool_name == "run_scenario":
        return engine.run_scenario(tool_args)
    if tool_name == "talk_to_accountant":
        return {"success": True, "message": "[Admin preview] An accountant would be notified here — no real lead created."}
    return {"error": f"unknown tool {tool_name!r}"}


def _converse(user_query: str, conversation_history: list, tool_runner) -> dict:
    """Shared DeepSeek round-trip for both the real and fixture paths.
    tool_runner(name, args) -> dict does the actual tool execution — the
    only thing that differs between get_chatbot_response (real Account/DB)
    and get_chatbot_response_fixture (mock data, no DB)."""
    client = _client()
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(conversation_history[-MAX_HISTORY_MESSAGES:])
    messages.append({"role": "user", "content": user_query})

    response = client.chat.completions.create(
        model=DEEPSEEK_CHAT_MODEL, messages=messages, tools=TOOLS,
        tool_choice="auto", temperature=0.3, max_tokens=500,
    )
    assistant_message = response.choices[0].message

    if not assistant_message.tool_calls:
        return {"response": assistant_message.content, "tool_calls": [], "actions": []}

    tool_results = []
    actions = []
    for tool_call in assistant_message.tool_calls:
        name = tool_call.function.name
        args = json.loads(tool_call.function.arguments or "{}")
        try:
            result = tool_runner(name, args)
        except Exception:
            logger.exception("chatbot tool %r failed", name)
            result = {"error": "something went wrong fetching that"}
        tool_results.append({"role": "tool", "tool_call_id": tool_call.id, "content": json.dumps(result)})
        actions.append({"tool": name, "args": args})

    messages.append(assistant_message)
    messages.extend(tool_results)
    final = client.chat.completions.create(
        model=DEEPSEEK_CHAT_MODEL, messages=messages, temperature=0.3, max_tokens=500,
    )
    return {
        "response": final.choices[0].message.content,
        "tool_calls": [tc.function.name for tc in assistant_message.tool_calls],
        "actions": actions,
    }


def get_chatbot_response(user_query: str, account_email: str, conversation_history: list = None) -> dict:
    """Returns {"response": str, "tool_calls": [str], "actions": [dict]}."""
    conversation_history = conversation_history or []
    db = SessionLocal()
    try:
        account = db.query(Account).filter(Account.email == account_email).first()
        if not account:
            return {"response": "Couldn't find your account — try refreshing the page.", "tool_calls": [], "actions": []}
        return _converse(user_query, conversation_history, lambda name, args: _run_tool(name, args, db, account))
    finally:
        db.close()


def get_chatbot_response_fixture(user_query: str, conversation_history: list = None,
                                  data_override: dict = None) -> dict:
    """Admin-preview path — no Account, no DB, always mock data (see
    _run_tool_fixture). Used by cashflow_routes.py's /api/cashflow/admin-
    preview/chat, which is admin-session-gated, not account-session-gated.
    data_override lets the caller pass session-specific fixture state
    (toggled pipeline items, excluded accounts) rather than the frozen
    default — falls back to a fresh build_fixture_data() if omitted."""
    conversation_history = conversation_history or []
    if data_override is None:
        from build_cashflow_prompt import build_fixture_data
        data_override = build_fixture_data()
    return _converse(user_query, conversation_history, lambda name, args: _run_tool_fixture(name, args, data_override))
