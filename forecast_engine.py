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
    # 1.0 = certain (an issued invoice, a signed contract, a recurring
    # bill) — every event defaults here and behaves exactly as before.
    # < 1.0 marks an UNWON quote: how it's treated depends on which of the
    # three scenario bands is being computed (see calculate_scenario_bands).
    probability: float = 1.0
    # Deep link to the source record in Xero (or a placeholder pre-Phase-2
    # — see build_cashflow_prompt.py's _xero_link) and the contact's email,
    # used by the "Chase" button on overdue alerts. Both purely cosmetic to
    # the math, carried through so the UI can render them per-transaction.
    xero_url: Optional[str] = None
    contact_email: Optional[str] = None


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

    def __init__(self, current_balance: float, forecast_days: int = 60, today: Optional[datetime] = None,
                 safe_balance_gbp: float = 0.0):
        self.current_balance = current_balance
        self.forecast_days = forecast_days
        self.today = (today or datetime.utcnow()).date()
        self.events: list[CashEvent] = []
        # The real "runway" question a director asks isn't "when do I hit
        # £0" — it's "when do I hit my overdraft limit / the buffer I never
        # want to go below." Defaults to 0 (old behavior) unless the caller
        # sets a real facility/comfort threshold — negative values (an
        # overdraft limit) are valid, e.g. -10000 for a £10k facility.
        self.safe_balance_gbp = safe_balance_gbp

    def add_invoice(self, due_date: datetime, amount: float, reference: str, contact: str,
                     probability: float = 1.0, cis_deduction_pct: float = 0.0,
                     retention_pct: float = 0.0, retention_release_days: int = 180,
                     xero_url: Optional[str] = None, contact_email: Optional[str] = None) -> None:
        """cis_deduction_pct: UK Construction Industry Scheme — a main
        contractor withholds this % as tax at source, so it never lands in
        the bank at all (not a timing issue, a permanent reduction).
        retention_pct: the % of the invoice a client contractually holds
        back until the defects liability period ends — modeled as a
        SEPARATE later event (retention_release_days after due_date), not
        a delay on the whole invoice, since only that slice is held."""
        net = abs(amount) * (1 - cis_deduction_pct / 100.0)
        if retention_pct > 0:
            held = net * (retention_pct / 100.0)
            upfront = net - held
            self.events.append(CashEvent(due_date, upfront, "incoming", reference, contact, probability, xero_url, contact_email))
            self.events.append(CashEvent(
                due_date + timedelta(days=retention_release_days), held, "incoming",
                f"{reference} (retention)", contact, probability, xero_url, contact_email,
            ))
        else:
            self.events.append(CashEvent(due_date, net, "incoming", reference, contact, probability, xero_url, contact_email))

    def add_staged_invoice(self, stages: list, reference: str, contact: str, probability: float = 1.0,
                            cis_deduction_pct: float = 0.0, retention_pct: float = 0.0,
                            retention_release_days: int = 180, xero_url: Optional[str] = None,
                            contact_email: Optional[str] = None) -> None:
        """A contract paid in milestones, not one lump sum — deposit, mid-
        stage, completion, etc. stages: [{"date": datetime, "amount": float,
        "label": str}, ...]. Each stage goes through the same CIS/retention
        treatment as a plain invoice."""
        for stage in stages:
            self.add_invoice(
                due_date=stage["date"], amount=stage["amount"],
                reference=f"{reference} — {stage.get('label', 'stage')}", contact=contact,
                probability=probability, cis_deduction_pct=cis_deduction_pct,
                retention_pct=retention_pct, retention_release_days=retention_release_days,
                xero_url=xero_url, contact_email=contact_email,
            )

    def add_bill(self, due_date: datetime, amount: float, reference: str, contact: str,
                 xero_url: Optional[str] = None) -> None:
        self.events.append(CashEvent(due_date, -abs(amount), "outgoing", reference, contact, xero_url=xero_url))

    def calculate_forecast(self) -> dict:
        """Returns the shape run_forecast_raw()/cashflow_routes.py expects:
        projected_balance_gbp, projection_date, runway_days, runway_date,
        daily_series ([{date,in,out,balance,events}], with a synthetic day-0
        "today" entry prepended for a date-scrubber UI to anchor against),
        money_in_60d_gbp, money_out_60d_gbp, traffic_light, transactions (a
        flat, chronological list of every event with a real UI hook: click
        "coming in"/"going out" to see exactly what and when).

        Events dated on/before today are applied immediately, into the
        day-0 point, rather than silently dropped — the day loop below only
        ever runs forward, so without this an overdue invoice a user marks
        "won"/received would have no visible effect on the forecast at all,
        which reads as broken, not as "correctly conservative"."""
        events_by_date = {}
        overdue_backlog = []
        for e in self.events:
            if e.date.date() <= self.today:
                overdue_backlog.append(e)
            else:
                events_by_date.setdefault(e.date.date(), []).append(e)

        balance = self.current_balance
        incoming_total = 0.0
        outgoing_total = 0.0
        transactions = []

        day0_in = day0_out = 0.0
        for e in overdue_backlog:
            balance += e.amount
            transactions.append(self._tx(e))
            if e.amount > 0:
                day0_in += e.amount
            else:
                day0_out += abs(e.amount)
        daily_series = [{
            "date": self.today.isoformat(), "balance": round(balance, 2),
            "in": round(day0_in, 2), "out": round(day0_out, 2), "events": [self._tx(e) for e in overdue_backlog],
        }]

        runway_days = None  # stays None if balance never goes negative in the horizon
        runway_date = None

        for day in range(1, self.forecast_days + 1):
            current_date = self.today + timedelta(days=day)
            day_events = events_by_date.get(current_date, [])
            day_in = day_out = 0.0
            for e in day_events:
                balance += e.amount
                transactions.append(self._tx(e))
                if e.amount > 0:
                    day_in += e.amount
                    incoming_total += e.amount
                else:
                    day_out += abs(e.amount)
                    outgoing_total += abs(e.amount)

            daily_series.append({
                "date": current_date.isoformat(), "balance": round(balance, 2),
                "in": round(day_in, 2), "out": round(day_out, 2),
                "events": [self._tx(e) for e in day_events],
            })
            if balance < self.safe_balance_gbp and runway_days is None:
                runway_days = day
                runway_date = current_date.isoformat()

        projected_balance = daily_series[-1]["balance"]
        projection_date = daily_series[-1]["date"]

        return {
            "current_balance_gbp": round(self.current_balance, 2),
            "safe_balance_gbp": round(self.safe_balance_gbp, 2),
            "projected_balance_gbp": projected_balance,
            "projection_date": projection_date,
            "runway_days": runway_days,
            "runway_date": runway_date,
            "daily_series": daily_series,
            "transactions": transactions,
            "money_in_60d_gbp": round(incoming_total, 2),
            "money_out_60d_gbp": round(outgoing_total, 2),
            "traffic_light": _traffic_light(runway_days),
        }

    def calculate_scenario_bands(self) -> dict:
        """The director's real question isn't "what's THE forecast" — quotes
        aren't binary, so there IS no single forecast, there's a range.
        Returns {"best": {...}, "likely": {...}, "worst": {...}}, each a
        full calculate_forecast()-shaped dict, computed from the SAME event
        list under three different treatments of any event with
        probability < 1.0 (an unwon quote — certain events, probability
        1.0, are identical in all three):
          - best:   every unwon quote lands, at full value
          - likely: every unwon quote counted at its probability-weighted
                     expected value (a 30% quote contributes 30% of its
                     amount) — the single most defensible "one number" if
                     you need one, but shown alongside the other two so it
                     isn't mistaken for a promise
          - worst:  every unwon quote excluded entirely
        """
        original_events = self.events
        results = {}
        try:
            for mode in ("best", "likely", "worst"):
                scaled = []
                for e in original_events:
                    if e.probability >= 1.0:
                        scaled.append(e)
                        continue
                    if mode == "best":
                        scaled.append(CashEvent(e.date, e.amount, e.event_type, e.reference, e.contact))
                    elif mode == "worst":
                        continue
                    else:
                        scaled.append(CashEvent(e.date, e.amount * e.probability, e.event_type, e.reference, e.contact))
                self.events = scaled
                results[mode] = self.calculate_forecast()
        finally:
            self.events = original_events
        return results

    @staticmethod
    def _tx(e: CashEvent) -> dict:
        return {
            "date": e.date.date().isoformat(), "amount_gbp": round(abs(e.amount), 2),
            "type": e.event_type, "reference": e.reference, "contact": e.contact,
            "probability": e.probability, "xero_url": e.xero_url, "contact_email": e.contact_email,
        }

    def detect_overdue_invoices(self, invoices: list) -> list:
        """invoices: list of {reference/id, contact_name, amount, due_date
        (ISO string), status}. Flags anything past due and not paid. Staged
        contracts (a `milestones` list instead of a single due_date) are
        skipped here — "overdue" isn't a meaningful concept against a whole
        multi-stage contract, only against an individual stage, which isn't
        surfaced as its own alert-able unit in this pass."""
        overdue = []
        for inv in invoices:
            if "due_date" not in inv:
                continue
            due = datetime.fromisoformat(inv["due_date"]).date()
            if due < self.today and inv.get("status", "").upper() != "PAID":
                days_overdue = (self.today - due).days
                overdue.append({
                    "reference": inv.get("reference") or inv.get("id", ""),
                    "contact": inv.get("contact_name", ""),
                    "contact_email": inv.get("contact_email"),
                    "amount": inv.get("amount_gbp", inv.get("amount", 0)),
                    "due_date": inv["due_date"],
                    "days_overdue": days_overdue,
                    "priority": "HIGH" if days_overdue > 30 else "MEDIUM" if days_overdue > 14 else "LOW",
                    "xero_url": inv.get("xero_url"),
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
                    "amount": inv.get("amount_gbp", inv.get("amount", 0)),
                    "terms": terms,
                    "due_date": inv.get("due_date", ""),
                    "risk": "HIGH" if terms > 90 else "MEDIUM",
                    "xero_url": inv.get("xero_url"),
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
        test_events = [
            CashEvent(e.date, e.amount, e.event_type, e.reference, e.contact, e.probability, e.xero_url, e.contact_email)
            for e in original_events
        ]

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


def _add_invoice_from_dict(engine: CashFlowEngine, inv: dict) -> None:
    """inv may carry any of: probability_pct (0-100, default 100 = certain),
    cis_deduction_pct, retention_pct, retention_release_days, or a
    `milestones` list ([{"date","amount","label"}]) for a staged contract —
    every field beyond due_date/amount_gbp/id/contact_name is optional and
    defaults to "plain single-payment invoice", so existing fixture/Xero
    data that doesn't set them behaves exactly as before."""
    probability = inv.get("probability_pct", 100) / 100.0
    cis = inv.get("cis_deduction_pct", 0.0)
    retention = inv.get("retention_pct", 0.0)
    retention_days = inv.get("retention_release_days", 180)
    xero_url = inv.get("xero_url")
    contact_email = inv.get("contact_email")
    if inv.get("milestones"):
        stages = [
            {"date": datetime.fromisoformat(m["date"]), "amount": m["amount_gbp"], "label": m.get("label", "stage")}
            for m in inv["milestones"]
        ]
        engine.add_staged_invoice(
            stages, reference=inv.get("id", ""), contact=inv.get("contact_name", ""),
            probability=probability, cis_deduction_pct=cis,
            retention_pct=retention, retention_release_days=retention_days,
            xero_url=xero_url, contact_email=contact_email,
        )
    else:
        engine.add_invoice(
            due_date=datetime.fromisoformat(inv["due_date"]), amount=inv["amount_gbp"],
            reference=inv.get("id", ""), contact=inv.get("contact_name", ""),
            probability=probability, cis_deduction_pct=cis,
            retention_pct=retention, retention_release_days=retention_days,
            xero_url=xero_url, contact_email=contact_email,
        )


def _build_engine(current_balance_gbp, invoices, bills, won_pipeline, forecast_days, today,
                   safe_balance_gbp: float = 0.0) -> CashFlowEngine:
    engine = CashFlowEngine(current_balance_gbp, forecast_days, today, safe_balance_gbp=safe_balance_gbp)
    for inv in invoices:
        _add_invoice_from_dict(engine, inv)
    for bill in bills:
        engine.add_bill(
            due_date=datetime.fromisoformat(bill["due_date"]), amount=bill["amount_gbp"],
            reference=bill.get("id", ""), contact=bill.get("contact_name", ""), xero_url=bill.get("xero_url"),
        )
    for item in won_pipeline:
        _add_invoice_from_dict(engine, item)
    return engine


def run_forecast_raw(current_balance_gbp: float, invoices: list, bills: list,
                      won_pipeline: list, forecast_days: int = 60,
                      today: datetime = None, safe_balance_gbp: float = 0.0,
                      bands: bool = False) -> dict:
    """Same signature/return shape as the previous DeepSeek-backed version —
    deterministic now, so no retry/validation-on-malformed-output logic is
    needed; a ValueError from bad input data (e.g. an unparseable date)
    surfaces as ForecastError instead of a raw traceback.

    bands=True returns calculate_scenario_bands()'s {"best","likely","worst"}
    shape instead of a single forecast — see that method's docstring."""
    try:
        engine = _build_engine(current_balance_gbp, invoices, bills, won_pipeline, forecast_days, today, safe_balance_gbp)
        return engine.calculate_scenario_bands() if bands else engine.calculate_forecast()
    except (KeyError, ValueError, TypeError) as exc:
        raise ForecastError(f"invalid forecast input: {exc}") from exc


def run_forecast_fixture(bands: bool = False) -> dict:
    """Runs the forecast against the hand-written fixture data in
    build_cashflow_prompt.py — for manual verification before a real Xero
    connection exists (no DB, no network needed at all now)."""
    from build_cashflow_prompt import FIXTURE_INPUT
    invoices = FIXTURE_INPUT.get("confirmed_invoices", []) + FIXTURE_INPUT["invoices"]
    return run_forecast_raw(
        FIXTURE_INPUT["current_balance_gbp"],
        invoices,
        FIXTURE_INPUT["bills"],
        FIXTURE_INPUT["won_pipeline"],
        safe_balance_gbp=FIXTURE_INPUT.get("safe_balance_gbp", 0.0),
        bands=bands,
    )


if __name__ == "__main__":
    result = run_forecast_fixture()
    print(json.dumps(result, indent=2))
