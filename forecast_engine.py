"""
Groundwork Cashflow — deterministic forecast engine.

Replaces the earlier DeepSeek-per-forecast design: cash flow math (running
balance, runway, overdue/risk detection, what-if scenarios) is arithmetic on
data Xero already gives us — there's nothing for an LLM to "figure out" here,
and every previous forecast run paid an API call plus carried the risk of a
malformed/hallucinated number for a calculation Python does exactly and for
free. DeepSeek is now used only where it earns its cost: the conversational
chatbot (chatbot.py) that answers free-text questions and calls back into
this module's pure functions as tools, never inventing numbers itself.

Public interface kept identical to the previous DeepSeek-backed version
(run_forecast_raw, run_forecast_fixture, ForecastError, same returned dict
shape) so cashflow_routes.py's dashboard endpoint didn't need to change.
"""
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional


class ForecastError(Exception):
    pass


@dataclass
class CashEvent:
    date: datetime
    amount: float  # positive = incoming, negative = outgoing
    event_type: str  # "incoming" | "outgoing"
    reference: str
    contact: str


def _traffic_light(runway_days: Optional[int]) -> str:
    if runway_days is None:
        return "green"
    if runway_days > 60:
        return "green"
    if runway_days >= 30:
        return "amber"
    return "red"


class CashFlowEngine:
    """Pure, deterministic — no DB, no API, no network. Directly testable
    against fixture data with no mocking required."""

    def __init__(self, current_balance: float, forecast_days: int = 60, today: Optional[datetime] = None):
        self.current_balance = current_balance
        self.forecast_days = forecast_days
        self.today = (today or datetime.utcnow()).date()
        self.events: list[CashEvent] = []

    def add_invoice(self, due_date: datetime, amount: float, reference: str, contact: str) -> None:
        self.events.append(CashEvent(due_date, abs(amount), "incoming", reference, contact))

    def add_bill(self, due_date: datetime, amount: float, reference: str, contact: str) -> None:
        self.events.append(CashEvent(due_date, -abs(amount), "outgoing", reference, contact))

    def calculate_forecast(self) -> dict:
        """Returns the shape run_forecast_raw()/cashflow_routes.py expects:
        projected_balance_gbp, projection_date, runway_days, daily_series
        ([{date,in,out,balance}]), money_in_60d_gbp, money_out_60d_gbp,
        traffic_light."""
        events_by_date = {}
        for e in self.events:
            events_by_date.setdefault(e.date.date(), []).append(e)

        balance = self.current_balance
        daily_series = []
        runway_days = None  # stays None if balance never goes negative in the horizon
        incoming_total = 0.0
        outgoing_total = 0.0

        for day in range(1, self.forecast_days + 1):
            current_date = self.today + timedelta(days=day)
            day_in = day_out = 0.0
            for e in events_by_date.get(current_date, []):
                balance += e.amount
                if e.amount > 0:
                    day_in += e.amount
                    incoming_total += e.amount
                else:
                    day_out += abs(e.amount)
                    outgoing_total += abs(e.amount)

            daily_series.append({
                "date": current_date.isoformat(), "balance": round(balance, 2),
                "in": round(day_in, 2), "out": round(day_out, 2),
            })
            if balance < 0 and runway_days is None:
                runway_days = day

        projected_balance = daily_series[-1]["balance"] if daily_series else round(self.current_balance, 2)
        projection_date = daily_series[-1]["date"] if daily_series else self.today.isoformat()

        return {
            "current_balance_gbp": round(self.current_balance, 2),
            "projected_balance_gbp": projected_balance,
            "projection_date": projection_date,
            "runway_days": runway_days,
            "daily_series": daily_series,
            "money_in_60d_gbp": round(incoming_total, 2),
            "money_out_60d_gbp": round(outgoing_total, 2),
            "traffic_light": _traffic_light(runway_days),
        }

    def detect_overdue_invoices(self, invoices: list) -> list:
        """invoices: list of {reference/id, contact_name, amount, due_date
        (ISO string), status}. Flags anything past due and not paid."""
        overdue = []
        for inv in invoices:
            due = datetime.fromisoformat(inv["due_date"]).date()
            if due < self.today and inv.get("status", "").upper() != "PAID":
                days_overdue = (self.today - due).days
                overdue.append({
                    "reference": inv.get("reference") or inv.get("id", ""),
                    "contact": inv.get("contact_name", ""),
                    "amount": inv.get("amount", 0),
                    "due_date": inv["due_date"],
                    "days_overdue": days_overdue,
                    "priority": "HIGH" if days_overdue > 30 else "MEDIUM" if days_overdue > 14 else "LOW",
                })
        return sorted(overdue, key=lambda x: x["days_overdue"], reverse=True)

    def detect_payment_risks(self, invoices: list) -> list:
        """Flags invoices whose payment terms exceed 60 days — the brief's
        'payment risk' alert. `payment_terms` (days) must be present on the
        invoice dict; invoices without it are silently skipped, not flagged
        (absence of data isn't evidence of risk)."""
        risks = []
        for inv in invoices:
            terms = inv.get("payment_terms")
            if terms and terms > 60:
                risks.append({
                    "reference": inv.get("reference") or inv.get("id", ""),
                    "contact": inv.get("contact_name", ""),
                    "amount": inv.get("amount", 0),
                    "terms": terms,
                    "due_date": inv.get("due_date", ""),
                    "risk": "HIGH" if terms > 90 else "MEDIUM",
                })
        return risks

    def run_scenario(self, scenario_changes: dict) -> dict:
        """What-if: 'material_cost_change' (float, e.g. 0.10 = +10% on every
        outgoing event) and/or 'payment_delay' (int days, delays every
        incoming event). 'contract_won' is a no-op here — a won pipeline
        item is already added as a normal incoming event by the caller
        (cashflow_routes.py's pipeline toggle) before this ever runs; this
        engine has no independent notion of "contracts", only cash events."""
        original_events = self.events
        test_events = [CashEvent(e.date, e.amount, e.event_type, e.reference, e.contact) for e in original_events]

        if "material_cost_change" in scenario_changes:
            pct = scenario_changes["material_cost_change"]
            for e in test_events:
                if e.event_type == "outgoing":
                    e.amount *= (1 + pct)
        if "payment_delay" in scenario_changes:
            delay = timedelta(days=scenario_changes["payment_delay"])
            for e in test_events:
                if e.event_type == "incoming":
                    e.date = e.date + delay

        self.events = test_events
        try:
            return self.calculate_forecast()
        finally:
            self.events = original_events


def _build_engine(current_balance_gbp, invoices, bills, won_pipeline, forecast_days, today) -> CashFlowEngine:
    engine = CashFlowEngine(current_balance_gbp, forecast_days, today)
    for inv in invoices:
        engine.add_invoice(
            due_date=datetime.fromisoformat(inv["due_date"]), amount=inv["amount_gbp"],
            reference=inv.get("id", ""), contact=inv.get("contact_name", ""),
        )
    for bill in bills:
        engine.add_bill(
            due_date=datetime.fromisoformat(bill["due_date"]), amount=bill["amount_gbp"],
            reference=bill.get("id", ""), contact=bill.get("contact_name", ""),
        )
    for item in won_pipeline:
        engine.add_invoice(
            due_date=datetime.fromisoformat(item["due_date"]), amount=item["amount_gbp"],
            reference=item.get("id", ""), contact=item.get("contact_name", ""),
        )
    return engine


def run_forecast_raw(current_balance_gbp: float, invoices: list, bills: list,
                      won_pipeline: list, forecast_days: int = 60,
                      today: datetime = None) -> dict:
    """Same signature/return shape as the previous DeepSeek-backed version —
    deterministic now, so no retry/validation-on-malformed-output logic is
    needed; a ValueError from bad input data (e.g. an unparseable date)
    surfaces as ForecastError instead of a raw traceback."""
    try:
        engine = _build_engine(current_balance_gbp, invoices, bills, won_pipeline, forecast_days, today)
        return engine.calculate_forecast()
    except (KeyError, ValueError, TypeError) as exc:
        raise ForecastError(f"invalid forecast input: {exc}") from exc


def run_forecast_fixture() -> dict:
    """Runs the forecast against the hand-written fixture data in
    build_cashflow_prompt.py — for manual verification before a real Xero
    connection exists (no DB, no network needed at all now)."""
    from build_cashflow_prompt import FIXTURE_INPUT
    return run_forecast_raw(
        FIXTURE_INPUT["current_balance_gbp"],
        FIXTURE_INPUT["invoices"],
        FIXTURE_INPUT["bills"],
        FIXTURE_INPUT["won_pipeline"],
    )


if __name__ == "__main__":
    result = run_forecast_fixture()
    print(json.dumps(result, indent=2))
