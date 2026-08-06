"""
Groundwork Cashflow — Flask Blueprints.

Kept as its own module (unlike the rest of the app, which is one big
app.py) because this is the first feature with an external OAuth
integration and has almost no coupling to existing routes — only Account
and Generation.status are touched. This module deliberately doesn't import
anything from app.py (only models/os/stripe) to avoid depending on import
order at all — account-session gating is re-checked here directly against
Flask's session, same one-line check as app.py's account_required.

Two blueprints, registered independently in app.py:
- cashflow_bp: the real, public product surface (checkout, dashboard,
  pipeline, chat, Xero connect/callback, /cf/ click-tracking) —
  account-session-gated. NOT currently registered in app.py: the product
  is still being tested internally (see cashflow_admin_bp below), and
  nothing here should be reachable by a real customer until that's done.
  Register it (`app.register_blueprint(cashflow_bp)`) when ready to go
  live publicly.
- cashflow_admin_bp: the admin-preview sandbox — mock data only (no real
  Account/Xero/Stripe involved), admin-session-gated instead. This one IS
  registered, so /admin/cashflow-preview works today.

Xero OAuth (connect/callback/refresh) is NOT implemented in this file yet —
see xero_integration.py, blocked on a Xero Developer app being created.
Routes that need it return 503 with a clear message in the meantime, and
dashboard/pipeline endpoints fall back to fixture data via ?fixture=1 so the
frontend can be built and verified before that dependency is resolved.
"""
import os
from datetime import datetime
from functools import wraps

import stripe
from flask import Blueprint, request, jsonify, session, redirect

from models import (
    SessionLocal, Account, Generation, Prospect,
    XeroConnection, CashflowSnapshot, CashflowPipelineItem, CashflowFunnelEvent,
)
from forecast_engine import run_forecast_raw, run_forecast_fixture, ForecastError, CashFlowEngine
from build_cashflow_prompt import FIXTURE_INPUT

cashflow_bp = Blueprint("cashflow", __name__)
# Separate blueprint for the admin-preview sandbox (mock-data routes at the
# bottom of this file) so app.py can register just this one without also
# exposing the real public /api/cashflow/* surface (checkout, dashboard,
# pipeline, chat, xero) — e.g. while the product is still being tested
# internally and nothing should be reachable by real customers yet.
cashflow_admin_bp = Blueprint("cashflow_admin", __name__)

STRIPE_CASHFLOW_PRICE_ID = os.environ.get("STRIPE_CASHFLOW_PRICE_ID", "")
SITE_URL = os.environ.get("SITE_URL", "https://groundworkbuild.com")
XERO_CLIENT_ID = os.environ.get("XERO_CLIENT_ID", "")


def _account_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("account_email"):
            return jsonify({"error": "not_authenticated"}), 401
        return view(*args, **kwargs)
    return wrapped


def _admin_required(view):
    """Same session key app.py's admin_required checks (session["is_admin"]),
    duplicated here rather than imported to keep this module's "no import
    from app.py" rule (see module docstring) — it's a one-line check."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("is_admin"):
            return jsonify({"error": "not_authenticated"}), 401
        return view(*args, **kwargs)
    return wrapped


def cashflow_entitled(db, account: Account) -> bool:
    """True if this account should have Cashflow dashboard access — either a
    direct paid/trialing Cashflow subscription, or an active website
    subscription (Cashflow is included free with the website tier)."""
    if account.cashflow_subscription_status in ("trialing", "active"):
        return True
    return db.query(Generation).filter(
        Generation.email == account.email, Generation.status == "live"
    ).first() is not None


def _log_funnel_event(db, account_id: int, stage: str, source: str = None) -> None:
    db.add(CashflowFunnelEvent(account_id=account_id, stage=stage, source=source))


# --- Billing -----------------------------------------------------------

@cashflow_bp.route("/api/cashflow/checkout/session", methods=["POST"])
@_account_required
def create_cashflow_checkout_session():
    if not stripe.api_key:
        return jsonify({"error": "Stripe is not configured"}), 503
    if not STRIPE_CASHFLOW_PRICE_ID:
        return jsonify({"error": "Cashflow billing isn't configured yet"}), 503

    email = session["account_email"]
    db = SessionLocal()
    try:
        account = db.query(Account).filter(Account.email == email).first()
        if not account:
            return jsonify({"error": "not_found"}), 404
        if cashflow_entitled(db, account):
            return jsonify({"error": "already_entitled"}), 409

        cs = stripe.checkout.Session.create(
            mode="subscription",
            line_items=[{"price": STRIPE_CASHFLOW_PRICE_ID, "quantity": 1}],
            subscription_data={
                "trial_period_days": 30,
                "metadata": {"product": "cashflow", "account_id": str(account.id)},
            },
            metadata={"product": "cashflow", "account_id": str(account.id)},
            success_url=f"{SITE_URL}/cashflow/dashboard.html",
            cancel_url=f"{SITE_URL}/cashflow/upgrade.html",
        )
        _log_funnel_event(db, account.id, "signup", source="standalone_direct")
        db.commit()
    finally:
        db.close()
    return jsonify({"url": cs.url})


# --- Stripe webhook handlers, called from app.py's stripe_webhook() ----
# (imported there via a local import inside the function, not at module
# load time, matching the note above about avoiding import-order coupling.)

def handle_cashflow_checkout_completed(db, cs, metadata: dict) -> None:
    account_id = metadata.get("account_id")
    if not account_id:
        return
    account = db.query(Account).filter(Account.id == int(account_id)).first()
    if not account:
        return
    account.cashflow_stripe_customer_id = cs.customer
    account.cashflow_stripe_subscription_id = cs.subscription
    account.cashflow_subscription_status = "trialing"
    _log_funnel_event(db, account.id, "trial_started", source="standalone_direct")
    db.commit()


def handle_cashflow_subscription_updated(db, sub) -> None:
    account = db.query(Account).filter(Account.cashflow_stripe_subscription_id == sub.id).first()
    if not account:
        return
    account.cashflow_subscription_status = sub.status
    account.cashflow_cancel_at_period_end = bool(sub.cancel_at_period_end)
    if sub.current_period_end:
        account.cashflow_period_end = datetime.utcfromtimestamp(sub.current_period_end)
    if getattr(sub, "trial_end", None):
        account.cashflow_trial_end = datetime.utcfromtimestamp(sub.trial_end)
    if sub.status == "active" and account.cashflow_canceled_at is None:
        # First transition out of trialing into a real paid period —
        # dedupe against re-firing on every subsequent "updated" event by
        # checking there isn't already a "paid" stage logged for this account.
        already_paid = db.query(CashflowFunnelEvent).filter(
            CashflowFunnelEvent.account_id == account.id, CashflowFunnelEvent.stage == "paid"
        ).first()
        if not already_paid:
            _log_funnel_event(db, account.id, "paid", source="standalone_direct")
    db.commit()


def handle_cashflow_subscription_deleted(db, sub) -> None:
    account = db.query(Account).filter(Account.cashflow_stripe_subscription_id == sub.id).first()
    if not account or account.cashflow_canceled_at is not None:
        return
    account.cashflow_canceled_at = datetime.utcnow()
    account.cashflow_subscription_status = "canceled"
    db.commit()


def handle_cashflow_invoice_payment_failed(db, sub_id: str) -> bool:
    """Returns True if this subscription id belonged to Cashflow (so app.py's
    webhook knows not to also try matching it against Domain)."""
    account = db.query(Account).filter(Account.cashflow_stripe_subscription_id == sub_id).first()
    if not account:
        return False
    account.cashflow_subscription_status = "past_due"
    db.commit()
    return True


def handle_cashflow_charge_refunded(db, customer_id: str) -> bool:
    account = db.query(Account).filter(Account.cashflow_stripe_customer_id == customer_id).first()
    if not account:
        return False
    account.cashflow_canceled_at = account.cashflow_canceled_at or datetime.utcnow()
    account.cashflow_subscription_status = "canceled"
    db.commit()
    return True


# --- Dashboard / pipeline / alerts --------------------------------------

@cashflow_bp.route("/api/cashflow/dashboard")
@_account_required
def cashflow_dashboard():
    email = session["account_email"]
    db = SessionLocal()
    try:
        account = db.query(Account).filter(Account.email == email).first()
        if not account or not cashflow_entitled(db, account):
            return jsonify({"error": "not_entitled"}), 403

        if request.args.get("fixture") == "1":
            # Phase 1: lets the frontend be built/verified before a real
            # Xero connection exists. Not gated behind an env flag — it's
            # inert unless the caller explicitly asks for it, and it only
            # ever returns the same static fixture, never real account data.
            try:
                result = run_forecast_fixture()
            except ForecastError as exc:
                return jsonify({"error": str(exc)}), 502
            return jsonify(result)

        connection = db.query(XeroConnection).filter(
            XeroConnection.account_id == account.id, XeroConnection.status == "active"
        ).first()
        if not connection:
            return jsonify({"error": "xero_not_connected"}), 409

        snapshot = db.query(CashflowSnapshot).filter(
            CashflowSnapshot.account_id == account.id
        ).order_by(CashflowSnapshot.created_at.desc()).first()
        if not snapshot:
            return jsonify({"error": "no_forecast_yet"}), 404

        _mark_dashboard_viewed(db, account)

        return jsonify({
            "projected_balance_gbp": snapshot.projected_balance_gbp,
            "projection_date": snapshot.projection_date.strftime("%Y-%m-%d"),
            "runway_days": snapshot.runway_days,
            "traffic_light": snapshot.traffic_light,
            "money_in_60d_gbp": snapshot.money_in_60d_gbp,
            "money_out_60d_gbp": snapshot.money_out_60d_gbp,
            "daily_series": snapshot.daily_series_json,
        })
    finally:
        db.close()


def _mark_dashboard_viewed(db, account) -> None:
    already = db.query(CashflowFunnelEvent).filter(
        CashflowFunnelEvent.account_id == account.id, CashflowFunnelEvent.stage == "dashboard_viewed"
    ).first()
    if not already:
        _log_funnel_event(db, account.id, "dashboard_viewed")
        db.commit()


@cashflow_bp.route("/api/cashflow/pipeline")
@_account_required
def cashflow_pipeline():
    email = session["account_email"]
    db = SessionLocal()
    try:
        account = db.query(Account).filter(Account.email == email).first()
        if not account or not cashflow_entitled(db, account):
            return jsonify({"error": "not_entitled"}), 403

        if request.args.get("fixture") == "1":
            return jsonify({"items": [
                {"id": i, "name": item["contact_name"], "value_gbp": item["amount_gbp"],
                 "due_date": item["due_date"], "won": False}
                for i, item in enumerate(FIXTURE_INPUT["invoices"])
            ]})

        items = db.query(CashflowPipelineItem).filter(CashflowPipelineItem.account_id == account.id).all()
        return jsonify({"items": [
            {"id": it.id, "name": it.contact_name, "value_gbp": it.amount_gbp,
             "due_date": it.due_date.strftime("%Y-%m-%d") if it.due_date else None, "won": it.is_won}
            for it in items
        ]})
    finally:
        db.close()


@cashflow_bp.route("/api/cashflow/pipeline/<int:item_id>", methods=["PATCH"])
@_account_required
def cashflow_pipeline_toggle(item_id):
    email = session["account_email"]
    data = request.get_json(silent=True) or {}
    won = bool(data.get("won"))

    db = SessionLocal()
    try:
        account = db.query(Account).filter(Account.email == email).first()
        if not account or not cashflow_entitled(db, account):
            return jsonify({"error": "not_entitled"}), 403

        item = db.query(CashflowPipelineItem).filter(
            CashflowPipelineItem.id == item_id, CashflowPipelineItem.account_id == account.id
        ).first()
        if not item:
            return jsonify({"error": "not_found"}), 404

        item.is_won = won
        item.won_toggled_at = datetime.utcnow()
        db.commit()

        # Recompute is wired up once xero_integration.py can pull real
        # invoices/bills/balance for this account (Phase 2) — the dataset is
        # small enough (one SME's 60-day pipeline) to recompute synchronously
        # on every toggle rather than needing a background job.
        return jsonify({"ok": True, "recompute": "pending_xero_integration"})
    finally:
        db.close()


# --- Xero OAuth (stubs — full implementation is xero_integration.py, ------
# blocked on a Xero Developer app being created; see the plan checkpoint).
# Kept here, not 404ing, so the "Connect Xero" button in the dashboard has
# something to link to today and fails with a clear message instead of a
# broken link.

@cashflow_bp.route("/api/cashflow/xero/connect")
@_account_required
def cashflow_xero_connect():
    if not XERO_CLIENT_ID:
        return jsonify({"error": "Xero isn't connected yet — check back soon."}), 503
    from xero_integration import build_authorize_url
    return build_authorize_url(session["account_email"])


@cashflow_bp.route("/api/cashflow/xero/callback")
def cashflow_xero_callback():
    if not XERO_CLIENT_ID:
        return jsonify({"error": "Xero isn't connected yet"}), 503
    from xero_integration import handle_callback
    return handle_callback(request)


# --- Cold-outreach click tracking ----------------------------------------

@cashflow_bp.route("/cf/<short_code>")
def cashflow_short_link(short_code):
    """Cashflow's equivalent of app.py's /s/<short_code> — resolves an
    outreach send's short link, marks the prospect's cashflow funnel state
    as clicked (first click only), and sends them to the upgrade/signup
    page. No per-prospect generated preview exists for Cashflow (unlike the
    website funnel's /claim/<token>), so there's no token-based redirect
    step — this route IS the landing point."""
    db = SessionLocal()
    try:
        prospect = db.query(Prospect).filter(Prospect.cashflow_short_code == short_code).first()
        if not prospect:
            return redirect("/cashflow/upgrade.html")
        if prospect.cashflow_clicked_at is None:
            prospect.cashflow_clicked_at = datetime.utcnow()
            prospect.cashflow_funnel_substage = "clicked"
            prospect.cashflow_last_touch_at = datetime.utcnow()
            db.commit()
        return redirect(f"/cashflow/upgrade.html?ref={short_code}")
    finally:
        db.close()


# --- Chatbot -------------------------------------------------------------

@cashflow_bp.route("/api/cashflow/chat", methods=["POST"])
@_account_required
def cashflow_chat():
    email = session["account_email"]
    db = SessionLocal()
    try:
        account = db.query(Account).filter(Account.email == email).first()
        if not account or not cashflow_entitled(db, account):
            return jsonify({"error": "not_entitled"}), 403
    finally:
        db.close()

    data = request.get_json(silent=True) or {}
    query = (data.get("query") or "").strip()
    if not query:
        return jsonify({"error": "empty_query"}), 400
    if len(query) > 2000:
        return jsonify({"error": "query_too_long"}), 400
    history = data.get("history") or []

    from chatbot import get_chatbot_response
    try:
        result = get_chatbot_response(query, email, history)
    except RuntimeError as exc:
        # DEEPSEEK_API_KEY unset — same "fail with a clear message" pattern
        # as the Xero stubs above, not a 500.
        return jsonify({"error": str(exc)}), 503
    return jsonify(result)


# --- Admin preview ---------------------------------------------------------
# A sandbox for the founder to click around the dashboard/pipeline/chatbot
# against mock data before deciding this is worth committing at all —
# admin-session-gated (not account-session-gated, no real Account/Xero/
# Stripe involved anywhere below). Pipeline "won" toggles are held in the
# Flask session only (never the DB — CashflowPipelineItem stays untouched),
# so playing with it can't leave stray rows behind and resets automatically
# once the admin's session ends.

_ADMIN_PREVIEW_WON_KEY = "admin_cf_preview_won_ids"
_ADMIN_PREVIEW_LOST_KEY = "admin_cf_preview_lost_ids"
_ADMIN_PREVIEW_EXCLUDED_ACCOUNTS_KEY = "admin_cf_preview_excluded_accounts"
_ADMIN_PREVIEW_SAFE_BALANCE_KEY = "admin_cf_preview_safe_balance"
_ADMIN_PREVIEW_CUSTOM_QUOTES_KEY = "admin_cf_preview_custom_quotes"
_ADMIN_PREVIEW_CUSTOM_BILLS_KEY = "admin_cf_preview_custom_bills"
_ADMIN_PREVIEW_FORECAST_DAYS_KEY = "admin_cf_preview_forecast_days"
DEFAULT_FORECAST_DAYS = 60
MAX_FORECAST_DAYS = 730  # 2 years — generous ceiling, not a hard product limit


def _admin_preview_forecast_days():
    return session.get(_ADMIN_PREVIEW_FORECAST_DAYS_KEY, DEFAULT_FORECAST_DAYS)


def _admin_preview_won_ids():
    return set(session.get(_ADMIN_PREVIEW_WON_KEY, []))


def _admin_preview_lost_ids():
    return set(session.get(_ADMIN_PREVIEW_LOST_KEY, []))


def _admin_preview_excluded_accounts():
    return set(session.get(_ADMIN_PREVIEW_EXCLUDED_ACCOUNTS_KEY, []))


def _admin_preview_custom_quotes():
    """Quotes raised by hand in the admin preview — session-only, on top of
    the fixture's own pipeline_quotes. Appended after the fixture list, so
    their pipeline ids start at len(fixture invoices) and stay stable across
    requests as long as the fixture list itself doesn't change shape."""
    return session.get(_ADMIN_PREVIEW_CUSTOM_QUOTES_KEY, [])


def _admin_preview_custom_bills():
    """Expected expenses raised by hand — same pattern as custom quotes,
    merged into data["bills"] so they show up everywhere a fixture bill
    would (Expenses page, the "going out" breakdown, the chart)."""
    return session.get(_ADMIN_PREVIEW_CUSTOM_BILLS_KEY, [])


def _admin_preview_fixture_data():
    """Fresh (not frozen-at-import-time) fixture data, respecting the
    session's account-exclusion and safe-balance-threshold state — see
    build_fixture_data()'s docstring for why fresh matters (recurring-
    expense dates, overdue framing) and cashflow_routes.py's module
    docstring for why this whole sandbox never touches the real DB. Also
    merges in any quotes/expenses raised by hand (see the two functions
    above) — these live only in the session, never the fixture module or
    the DB, so they vanish with the admin's session same as every other
    preview toggle."""
    from build_cashflow_prompt import build_fixture_data
    kwargs = {
        "excluded_account_ids": _admin_preview_excluded_accounts(),
        "forecast_days": _admin_preview_forecast_days(),
    }
    if _ADMIN_PREVIEW_SAFE_BALANCE_KEY in session:
        kwargs["safe_balance_gbp"] = session[_ADMIN_PREVIEW_SAFE_BALANCE_KEY]
    data = build_fixture_data(**kwargs)
    data["invoices"] = data["invoices"] + _admin_preview_custom_quotes()
    data["bills"] = data["bills"] + _admin_preview_custom_bills()
    return data


_ADMIN_PREVIEW_PROB_KEY = "admin_cf_preview_probability_overrides"


def _admin_preview_probability_overrides():
    return {int(k): v for k, v in session.get(_ADMIN_PREVIEW_PROB_KEY, {}).items()}


def _admin_preview_quotes_with_overrides(data):
    """Pipeline quotes with the admin's manual overrides applied. Two
    independent controls, both plain admin INPUT — this app never infers or
    calculates a win probability on its own:
      - Won/Lost status forces probability to 100%/0% (certain either way).
      - Left "Open", the probability is whatever the admin typed into the
        per-quote number field (defaults to the fixture's starting
        estimate) — that's what feeds the "Likely" band's expected-value
        math (see forecast_engine.calculate_scenario_bands)."""
    won_ids, lost_ids = _admin_preview_won_ids(), _admin_preview_lost_ids()
    overrides = _admin_preview_probability_overrides()
    quotes = []
    for i, inv in enumerate(data["invoices"]):
        item = dict(inv)
        if i in won_ids:
            item["probability_pct"] = 100
        elif i in lost_ids:
            item["probability_pct"] = 0
        elif i in overrides:
            item["probability_pct"] = overrides[i]
        quotes.append(item)
    return quotes


@cashflow_admin_bp.route("/api/cashflow/admin-preview/dashboard")
@_admin_required
def cashflow_admin_preview_dashboard():
    data = _admin_preview_fixture_data()
    quotes = _admin_preview_quotes_with_overrides(data)
    all_invoices = data["confirmed_invoices"] + quotes

    forecast_days = _admin_preview_forecast_days()
    bands = run_forecast_raw(
        data["current_balance_gbp"], all_invoices, data["bills"], data["won_pipeline"],
        safe_balance_gbp=data["safe_balance_gbp"], forecast_days=forecast_days, bands=True,
    )
    overdue = CashFlowEngine(data["current_balance_gbp"]).detect_overdue_invoices(
        data["confirmed_invoices"] + data["won_pipeline"]
    )
    return jsonify({
        "bands": bands,
        "current_balance_gbp": data["current_balance_gbp"],
        "safe_balance_gbp": data["safe_balance_gbp"],
        "forecast_days": forecast_days,
        "overdue": overdue,
    })


def _admin_preview_fixture_quote_count():
    """How many pipeline quotes come from the fixture itself (not raised by
    hand) — the cutoff for which pipeline ids are safe to delete. Cheap:
    build_fixture_data() doesn't touch the network/DB, just recomputes the
    same in-memory dicts."""
    from build_cashflow_prompt import build_fixture_data
    return len(build_fixture_data()["invoices"])


@cashflow_admin_bp.route("/api/cashflow/admin-preview/pipeline")
@_admin_required
def cashflow_admin_preview_pipeline():
    data = _admin_preview_fixture_data()
    fixture_count = _admin_preview_fixture_quote_count()
    won_ids, lost_ids = _admin_preview_won_ids(), _admin_preview_lost_ids()
    overrides = _admin_preview_probability_overrides()
    return jsonify({"items": [
        {"id": i, "name": inv["contact_name"], "value_gbp": inv["amount_gbp"],
         "due_date": inv["due_date"], "xero_url": inv.get("xero_url"),
         "probability_pct": overrides.get(i, inv.get("probability_pct", 100)),
         "status": "won" if i in won_ids else "lost" if i in lost_ids else "open",
         "is_custom": i >= fixture_count}
        for i, inv in enumerate(data["invoices"])
    ]})


@cashflow_admin_bp.route("/api/cashflow/admin-preview/pipeline", methods=["POST"])
@_admin_required
def cashflow_admin_preview_pipeline_create():
    """Raise a new quote by hand — a real opportunity that isn't in Xero
    yet (or won't be until it's actually sent), but the director wants
    reflected in the forecast today. Session-only, same as every other
    preview toggle; see _admin_preview_custom_quotes()."""
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400
    try:
        amount = float(body["value_gbp"])
        due_date = datetime.fromisoformat(body["due_date"]).strftime("%Y-%m-%d")
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "value_gbp and due_date (YYYY-MM-DD) are required"}), 400
    probability_pct = max(0, min(100, int(body.get("probability_pct", 50))))

    quotes = _admin_preview_custom_quotes()
    quotes.append({
        "id": f"custom-quote-{len(quotes)}", "contact_name": name,
        "amount_gbp": amount, "due_date": due_date, "probability_pct": probability_pct,
    })
    session[_ADMIN_PREVIEW_CUSTOM_QUOTES_KEY] = quotes
    return jsonify({"ok": True}), 201


@cashflow_admin_bp.route("/api/cashflow/admin-preview/pipeline/<int:item_id>", methods=["PATCH", "DELETE"])
@_admin_required
def cashflow_admin_preview_pipeline_toggle(item_id):
    """PATCH accepts `status` ("won"/"lost"/"open") and/or `probability_pct`
    (0-100) — sent separately by the UI (status buttons vs. the probability
    number field), so either can change without touching the other. DELETE
    withdraws a quote raised by hand — fixture quotes can't be deleted,
    only marked Lost, since they represent something real in (mock) Xero."""
    data = _admin_preview_fixture_data()
    if item_id < 0 or item_id >= len(data["invoices"]):
        return jsonify({"error": "not_found"}), 404

    if request.method == "DELETE":
        fixture_count = _admin_preview_fixture_quote_count()
        if item_id < fixture_count:
            return jsonify({"error": "cannot delete a fixture quote — mark it Lost instead"}), 400
        quotes = _admin_preview_custom_quotes()
        del quotes[item_id - fixture_count]
        session[_ADMIN_PREVIEW_CUSTOM_QUOTES_KEY] = quotes
        return jsonify({"ok": True})

    body = request.get_json(silent=True) or {}

    if "status" in body:
        status = body["status"]  # "won" | "lost" | "open"
        won_ids, lost_ids = _admin_preview_won_ids(), _admin_preview_lost_ids()
        won_ids.discard(item_id)
        lost_ids.discard(item_id)
        if status == "won":
            won_ids.add(item_id)
        elif status == "lost":
            lost_ids.add(item_id)
        session[_ADMIN_PREVIEW_WON_KEY] = list(won_ids)
        session[_ADMIN_PREVIEW_LOST_KEY] = list(lost_ids)

    if "probability_pct" in body:
        try:
            pct = max(0, min(100, int(body["probability_pct"])))
        except (TypeError, ValueError):
            return jsonify({"error": "invalid probability_pct"}), 400
        overrides = _admin_preview_probability_overrides()
        overrides[item_id] = pct
        session[_ADMIN_PREVIEW_PROB_KEY] = overrides

    return jsonify({"ok": True})


@cashflow_admin_bp.route("/api/cashflow/admin-preview/expenses")
@_admin_required
def cashflow_admin_preview_expenses():
    """Just the expected expenses raised by hand, for the Expenses page's
    "your raised expenses" list (separate from the full bills breakdown,
    which includes real fixture/recurring bills that can't be withdrawn).
    `id` here is the plain index into the custom-bills list itself (0..n-1)
    — matching what DELETE /expenses/<id> expects — NOT a composite offset
    against the fixture bills list, which would be unstable anyway since
    the fixture's recurring-bill count varies with the forecast horizon."""
    return jsonify({"items": [
        {"id": i, "name": b["contact_name"], "value_gbp": b["amount_gbp"], "due_date": b["due_date"]}
        for i, b in enumerate(_admin_preview_custom_bills())
    ]})


@cashflow_admin_bp.route("/api/cashflow/admin-preview/expenses", methods=["POST"])
@_admin_required
def cashflow_admin_preview_expenses_create():
    """Raise an expected expense by hand — a cost the director knows is
    coming (new kit, a one-off supplier payment) that isn't in Xero as a
    bill yet. One-off only for now — see module docstring on
    _admin_preview_custom_bills() for the recurring fast-follow note."""
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400
    try:
        amount = float(body["value_gbp"])
        due_date = datetime.fromisoformat(body["due_date"]).strftime("%Y-%m-%d")
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "value_gbp and due_date (YYYY-MM-DD) are required"}), 400

    bills = _admin_preview_custom_bills()
    bills.append({"id": f"custom-bill-{len(bills)}", "contact_name": name, "amount_gbp": amount, "due_date": due_date})
    session[_ADMIN_PREVIEW_CUSTOM_BILLS_KEY] = bills
    return jsonify({"ok": True}), 201


@cashflow_admin_bp.route("/api/cashflow/admin-preview/expenses/<int:item_id>", methods=["DELETE"])
@_admin_required
def cashflow_admin_preview_expenses_delete(item_id):
    bills = _admin_preview_custom_bills()
    if item_id < 0 or item_id >= len(bills):
        return jsonify({"error": "not_found"}), 404
    del bills[item_id]
    session[_ADMIN_PREVIEW_CUSTOM_BILLS_KEY] = bills
    return jsonify({"ok": True})


@cashflow_admin_bp.route("/api/cashflow/admin-preview/settings", methods=["GET", "PATCH"])
@_admin_required
def cashflow_admin_preview_settings():
    """Director-configurable assumptions: the safe-balance threshold
    (overdraft limit / minimum comfort buffer) and the forecast horizon —
    how far ahead to look, not locked to 60 days. Both session-only, same
    as every other admin-preview toggle."""
    if request.method == "GET":
        data = _admin_preview_fixture_data()
        return jsonify({"safe_balance_gbp": data["safe_balance_gbp"], "forecast_days": _admin_preview_forecast_days()})
    body = request.get_json(silent=True) or {}
    if "safe_balance_gbp" in body:
        try:
            # An overdraft limit/comfort buffer is a floor BELOW zero by
            # definition — force negative here too, not just in the UI
            # (which sends a positive "limit" figure and negates it), so a
            # direct API call can't set a nonsensical positive threshold.
            session[_ADMIN_PREVIEW_SAFE_BALANCE_KEY] = -abs(float(body["safe_balance_gbp"]))
        except (TypeError, ValueError):
            return jsonify({"error": "invalid safe_balance_gbp"}), 400
    if "forecast_days" in body:
        try:
            days = int(body["forecast_days"])
        except (TypeError, ValueError):
            return jsonify({"error": "invalid forecast_days"}), 400
        if days < 1 or days > MAX_FORECAST_DAYS:
            return jsonify({"error": f"forecast_days must be between 1 and {MAX_FORECAST_DAYS}"}), 400
        session[_ADMIN_PREVIEW_FORECAST_DAYS_KEY] = days
    return jsonify({"ok": True})


@cashflow_admin_bp.route("/api/cashflow/admin-preview/accounts")
@_admin_required
def cashflow_admin_preview_accounts():
    """Bank accounts / credit cards feeding the mock current balance —
    unselecting one (PATCH below) removes it from current_balance_gbp and
    recomputes the whole forecast, same as unlinking an account in Xero
    would. FIXTURE_ACCOUNTS itself lives in build_cashflow_prompt.py."""
    from build_cashflow_prompt import FIXTURE_ACCOUNTS
    excluded = _admin_preview_excluded_accounts()
    return jsonify({"accounts": [
        {**a, "included": a["id"] not in excluded} for a in FIXTURE_ACCOUNTS
    ]})


@cashflow_admin_bp.route("/api/cashflow/admin-preview/accounts/<account_id>", methods=["PATCH"])
@_admin_required
def cashflow_admin_preview_account_toggle(account_id):
    from build_cashflow_prompt import FIXTURE_ACCOUNTS
    if account_id not in {a["id"] for a in FIXTURE_ACCOUNTS}:
        return jsonify({"error": "not_found"}), 404
    body = request.get_json(silent=True) or {}
    excluded = _admin_preview_excluded_accounts()
    if body.get("included", True):
        excluded.discard(account_id)
    else:
        excluded.add(account_id)
    session[_ADMIN_PREVIEW_EXCLUDED_ACCOUNTS_KEY] = list(excluded)
    return jsonify({"ok": True})


@cashflow_admin_bp.route("/api/cashflow/admin-preview/chat", methods=["POST"])
@_admin_required
def cashflow_admin_preview_chat():
    data = request.get_json(silent=True) or {}
    query = (data.get("query") or "").strip()
    if not query:
        return jsonify({"error": "empty_query"}), 400
    if len(query) > 2000:
        return jsonify({"error": "query_too_long"}), 400
    history = data.get("history") or []

    fixture_data = _admin_preview_fixture_data()
    fixture_data["invoices"] = _admin_preview_quotes_with_overrides(fixture_data)

    from chatbot import get_chatbot_response_fixture
    try:
        result = get_chatbot_response_fixture(query, history, data_override=fixture_data)
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 503
    return jsonify(result)
