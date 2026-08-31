"""FastAPI + HTMX front end.

Runs on localhost only. No authentication, because there is nothing to
authenticate against -- the database is a file on your machine and the server
binds to 127.0.0.1. Do not expose this to a network.
"""
from __future__ import annotations

import re
import shutil
import tempfile
from contextlib import asynccontextmanager, contextmanager
from datetime import date
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Form, HTTPException, Request, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import analytics, config, coverage, db, income, remittance
from . import charts
from ..categorize import categorize_all, seed_rules, set_manual_category
from ..goals import Goal, add_goal, compare, load_goals, project
from ..importers import import_file

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.filters["money"] = lambda v: f"${v:,.0f}" if v is not None else "-"
templates.env.filters["money2"] = lambda v: f"${v:,.2f}" if v is not None else "-"


def _safe_filename(name: str) -> str:
    """Reduce an uploaded filename to a harmless leaf name."""
    leaf = Path(str(name).replace("\\", "/")).name.strip()
    leaf = re.sub(r"[^A-Za-z0-9._ -]", "_", leaf).lstrip(".")
    return leaf or "upload"


def _group_class(group: str) -> str:
    """CSS class for a spend group.

    Derived from the group NAME, never from its rank in the current month, so
    filtering or a change in the ordering never repaints the survivors.
    """
    slug = (group or config.UNGROUPED).lower().replace(" & ", "-").replace(" ", "-")
    return f"g-{slug}"


templates.env.filters["groupclass"] = _group_class
templates.env.filters["groupof"] = config.group_of
templates.env.globals["GROUP_ORDER"] = config.GROUP_ORDER
templates.env.globals["UNGROUPED"] = config.UNGROUPED

@asynccontextmanager
async def lifespan(_app: FastAPI):
    """One-time setup, instead of once per request.

    Creating the schema, running migrations and re-seeding ~190 rules on every
    single request is a lot of write traffic to serve a page that only reads,
    and it made two browser tabs contend for SQLite's write lock. It happens
    here instead, once, when the server starts.
    """
    with closing_conn() as conn:
        db.init_db(conn)
        seed_rules(conn)
        # Pairing internal transfers rewrites a column across the whole table,
        # so it does not belong in a GET either. It runs at startup and after
        # every import -- the only two moments the answer can change.
        coverage.match_internal_transfers(conn)
    yield


app = FastAPI(title="fin-asst", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@contextmanager
def closing_conn():
    conn = db.connect()
    try:
        yield conn
    finally:
        conn.close()


def get_conn():
    """Request-scoped connection, closed when the response is done.

    Previously every request opened a connection and dropped the reference
    without closing it, leaking a file handle per page view.
    """
    conn = db.connect()
    try:
        yield conn
    finally:
        conn.close()


def _number(text: str, fallback):
    """Read a stored setting without letting a bad one take the app down.

    Settings are strings in a table, and one of them used to be free text from
    a form. Typing "2,500" into the surplus override stored it verbatim, and
    the float() on the way back out raised on every single page -- including
    the page with the field that would have fixed it. Now a value that will not
    parse is ignored and reported, not fatal.
    """
    try:
        return float(str(text).strip().replace(",", "").replace("$", ""))
    except (TypeError, ValueError):
        return fallback


def _settings(conn) -> dict:
    return {
        "annual_return_rate": _number(db.get_setting(conn, "annual_return_rate", "0.04"), 0.04),
        "annual_inflation": _number(db.get_setting(conn, "annual_inflation", "0.025"), 0.025),
        "lookback": int(_number(db.get_setting(conn, "surplus_lookback_months", "3"), 3)),
        "manual_surplus": db.get_setting(conn, "manual_monthly_surplus", ""),
    }


def _surplus(conn) -> tuple[float, str, Optional[str]]:
    s = _settings(conn)
    raw = (s["manual_surplus"] or "").strip()
    if raw:
        parsed = _number(raw, None)
        if parsed is not None:
            return parsed, "set by hand", None
        # Fall through to the computed surplus rather than failing the page.
        avg = analytics.average_surplus(conn, s["lookback"])
        return avg["surplus"], avg["basis_label"], (
            f"Your surplus override ({raw!r}) is not a number, so it is being ignored. "
            "Clear it or enter a plain figure."
        )
    avg = analytics.average_surplus(conn, s["lookback"])
    # basis_label, not a hardcoded "average of": when income is too new to
    # average, the surplus is declared pay less observed spending, and the
    # label has to say which of the two you are looking at.
    return avg["surplus"], avg["basis_label"], avg.get("warning")


# --------------------------------------------------------------------------
# Dashboard
# --------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, month: Optional[str] = None, months: int = 1,
              conn=Depends(get_conn)):
    available = analytics.months_available(conn)
    totals = analytics.monthly_totals(conn)
    selected = analytics.valid_month(month) or (available[0] if available else None)
    months = min(max(int(months or 1), 1), 24)
    surplus, basis, warning = _surplus(conn)

    # Transfer matching is a whole-table write; it runs at startup and after an
    # import, not on every page view. This route only reads its results.
    cov = coverage.coverage(conn)

    categories = analytics.category_breakdown(conn, selected, months)
    groups = analytics.group_breakdown(categories)
    shown = totals[-12:]

    # The month still in progress is charted, but flagged: a part-month bar
    # beside full ones reads as a collapse in spending if nothing says otherwise.
    this_month = date.today().strftime("%Y-%m")
    partial = this_month if any(m["month"] == this_month for m in shown) else None

    return templates.TemplateResponse(request, "dashboard.html", {
        "page": "dashboard",
        "coverage": cov,
        "cov_bars": charts.hbars(cov.destinations, label_key="destination",
                                 value_key="amount", group_key=None),
        "income_warning": coverage.income_regime_warning(conn),
        "regime": income.current_regime(conn),
        "has_data": bool(totals),
        "months_available": available,
        "selected_month": selected,
        "window": months,
        "totals": shown,
        "chart": charts.flow_chart(shown, current_month=partial),
        "partial_month": partial,
        "categories": categories,
        "cat_bars": charts.hbars(categories),
        "groups": groups,
        "top_group": groups[0] if groups else {"name": config.UNGROUPED, "spend": 0.0, "share": 0.0},
        "month_spend": round(sum(c["spend"] for c in categories), 2),
        "tx_count": sum(c["count"] for c in categories),
        "merchants": analytics.top_merchants(conn, selected, months, limit=10),
        "recurring": analytics.recurring_charges(conn)[:12],
        "fixed_cost": analytics.fixed_monthly_cost(conn),
        "surplus": surplus,
        "surplus_basis": basis,
        "surplus_warning": warning,
        "uncategorised_count": analytics.uncategorised_count(conn),
    })


# --------------------------------------------------------------------------
# Transactions
# --------------------------------------------------------------------------

@app.get("/transactions", response_class=HTMLResponse)
def transactions(
    request: Request,
    q: str = "",
    category: str = "",
    month: str = "",
    only_uncategorised: bool = False,
    limit: int = 200,
    conn=Depends(get_conn),
):
    limit = min(max(int(limit or 1), 1), 1000)
    rows = _query_transactions(conn, q, category, analytics.valid_month(month) or '',
                               only_uncategorised, limit)
    return templates.TemplateResponse(request, "transactions.html", {
        "page": "transactions",
        "rows": rows,
        "categories": config.CATEGORIES,
        "months_available": analytics.months_available(conn),
        "q": q, "category": category, "month": month,
        "only_uncategorised": only_uncategorised,
    })


def _query_transactions(conn, q, category, month, only_uncategorised, limit):
    sql = ["""SELECT t.*, a.name AS account_name FROM transactions t
              JOIN accounts a ON a.id = t.account_id WHERE 1=1"""]
    params: list = []
    if q:
        sql.append("AND (t.description LIKE ? OR t.raw_description LIKE ?)")
        params += [f"%{q}%", f"%{q}%"]
    if category:
        sql.append("AND t.category = ?")
        params.append(category)
    if month:
        sql.append("AND substr(t.date, 1, 7) = ?")
        params.append(month)
    if only_uncategorised:
        sql.append("AND t.category IS NULL")
    sql.append("ORDER BY t.date DESC, t.id DESC LIMIT ?")
    params.append(limit)
    return conn.execute(" ".join(sql), params).fetchall()


@app.post("/transactions/{tx_id}/category", response_class=HTMLResponse)
def recategorise(request: Request, tx_id: int, category: str = Form(...),
                 teach: bool = Form(False), conn=Depends(get_conn)):
    """Inline recategorisation. Returns just the updated row for HTMX to swap in."""
    taught = set_manual_category(conn, tx_id, category, teach=teach)
    row = conn.execute(
        """SELECT t.*, a.name AS account_name FROM transactions t
           JOIN accounts a ON a.id = t.account_id WHERE t.id = ?""",
        (tx_id,),
    ).fetchone()
    return templates.TemplateResponse(request, "_tx_row.html", {
        "tx": row, "categories": config.CATEGORIES, "taught": taught,
    })


# --------------------------------------------------------------------------
# Import
# --------------------------------------------------------------------------

@app.get("/import", response_class=HTMLResponse)
def import_page(request: Request, conn=Depends(get_conn)):
    return templates.TemplateResponse(request, "import.html", {
        "page": "import",
        "accounts": db.accounts(conn), "results": None,
    })


@app.post("/import", response_class=HTMLResponse)
async def do_import(
    request: Request,
    files: list[UploadFile] = File(...),
    account: str = Form(""),
    issuer: str = Form(""),
    kind: str = Form("credit"),
    conn=Depends(get_conn),
):
    results, errors = [], []
    with tempfile.TemporaryDirectory(prefix="finasst-import-") as staging:
        for upload in files:
            if not upload.filename:
                continue
            # The browser controls this string, and pathlib treats an absolute
            # right-hand operand as replacing the base entirely -- a filename of
            # "/Users/you/.zshrc" wrote the upload over that file and then
            # deleted it. Keep the last path component and nothing else.
            safe = _safe_filename(upload.filename)
            tmp = Path(staging) / safe
            with open(tmp, "wb") as fh:
                shutil.copyfileobj(upload.file, fh)
            try:
                results.append(import_file(
                    conn, tmp,
                    account_name=account.strip() or Path(safe).stem,
                    issuer=issuer or None, kind=kind,
                ))
            except Exception as exc:
                errors.append(f"{upload.filename}: {exc}")

    stats = categorize_all(conn)
    if results:
        # New rows can complete a transfer pair, so re-run the matcher here --
        # the one place other than startup where the answer can change.
        coverage.match_internal_transfers(conn)
    return templates.TemplateResponse(request, "import.html", {
        "page": "import",
        "accounts": db.accounts(conn),
        "results": results, "errors": errors, "stats": stats,
    })


# --------------------------------------------------------------------------
# Remittances
# --------------------------------------------------------------------------

@app.get("/remittances", response_class=HTMLResponse)
def remittances_page(request: Request, conn=Depends(get_conn)):
    remittance.sync_from_transactions(conn)
    rep = remittance.report(conn)
    return templates.TemplateResponse(request, "remittances.html", {
        "page": "remittances", "report": rep,
    })


@app.post("/remittances/{tx_id}")
def record_remittance(
    tx_id: int,
    received: float = Form(...),
    currency: str = Form("INR"),
    provider: str = Form(""),
    conn=Depends(get_conn),
):
    remittance.sync_from_transactions(conn)
    remittance.set_received(conn, tx_id, received, provider=provider or None, currency=currency)
    return RedirectResponse("/remittances", status_code=303)


@app.post("/remittances/rates/fetch")
def fetch_reference_rates(conn=Depends(get_conn)):
    """The only route in the app that touches the network, and only when clicked."""
    remittance.sync_from_transactions(conn)
    rep = remittance.report(conn)
    remittance.fetch_rates(conn, [t.date for t in rep.transfers])
    return RedirectResponse("/remittances", status_code=303)


# --------------------------------------------------------------------------
# Goals
# --------------------------------------------------------------------------

@app.get("/goals", response_class=HTMLResponse)
def goals_page(request: Request, surplus: Optional[float] = None, without: str = "",
               conn=Depends(get_conn)):
    goals = load_goals(conn)
    s = _settings(conn)
    computed, basis, warning = _surplus(conn)
    used = surplus if surplus is not None else computed

    projection = comparison = None
    if goals:
        projection = project(
            goals, used,
            annual_return_rate=s["annual_return_rate"],
            annual_inflation=s["annual_inflation"],
        )
        if without:
            comparison = compare(
                goals, used, without=without,
                annual_return_rate=s["annual_return_rate"],
                annual_inflation=s["annual_inflation"],
            )

    return templates.TemplateResponse(request, "goals.html", {
        "page": "goals",
        "goals": goals,
        "projection": projection,
        "comparison": comparison,
        "without": without,
        "surplus": used,
        "surplus_basis": "you set it on this page" if surplus is not None else basis,
        "surplus_warning": warning,
        "settings": s,
        "today": date.today(),
    })


@app.post("/goals")
def create_goal(
    name: str = Form(...),
    target_amount: float = Form(...),
    target_date: str = Form(""),
    saved_so_far: float = Form(0.0),
    priority: int = Form(100),
    monthly_min: float = Form(0.0),
    notes: str = Form(""),
    conn=Depends(get_conn),
):
    add_goal(conn, Goal(
        name=name.strip(),
        target_amount=target_amount,
        target_date=date.fromisoformat(target_date) if target_date else None,
        saved_so_far=saved_so_far, priority=priority,
        monthly_min=monthly_min, notes=notes,
    ))
    return RedirectResponse("/goals", status_code=303)


@app.post("/goals/{goal_id}/delete")
def delete_goal(goal_id: int, conn=Depends(get_conn)):
    conn.execute("DELETE FROM goals WHERE id = ?", (goal_id,))
    conn.commit()
    return RedirectResponse("/goals", status_code=303)


# --------------------------------------------------------------------------
# Income regimes
# --------------------------------------------------------------------------

@app.get("/income", response_class=HTMLResponse)
def income_page(request: Request, conn=Depends(get_conn)):
    rows = income.regimes(conn)
    current = income.current_regime(conn)
    totals = analytics.monthly_totals(conn)
    avg = analytics.average_surplus(conn, _settings(conn)["lookback"])
    return templates.TemplateResponse(request, "income.html", {
        "page": "income",
        "regimes": rows,
        "current": current,
        "today": date.today(),
        "declared_monthly": income.declared_monthly(conn),
        "drift": income.drift(conn, current, totals) if current else None,
        "surplus": avg,
        "frequencies": list(income.PERIODS_PER_YEAR),
        "frequency_labels": income.FREQUENCY_LABELS,
        "observed": [m for m in totals if m["month"] >= current.first_whole_month]
                    if current else [],
        # The month in progress is shown but never averaged, and a part-month
        # row beside full ones reads as a collapse in income if nothing says so.
        "partial_month": date.today().strftime("%Y-%m"),
    })


@app.post("/income")
def create_regime(
    name: str = Form(...),
    amount: float = Form(...),
    frequency: str = Form("biweekly"),
    started_on: str = Form(...),
    keep_previous: str = Form(""),
    notes: str = Form(""),
    conn=Depends(get_conn),
):
    try:
        freq = income.normalise_frequency(frequency)
        start = date.fromisoformat(started_on)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    income.add_regime(
        conn,
        income.Regime(name=name.strip(), started_on=start, amount=amount,
                      frequency=freq, notes=notes),
        close_previous=not keep_previous,
    )
    return RedirectResponse("/income", status_code=303)


@app.post("/income/{regime_id}/delete")
def remove_regime(regime_id: int, conn=Depends(get_conn)):
    income.delete_regime(conn, regime_id)
    return RedirectResponse("/income", status_code=303)


@app.post("/settings")
def update_settings(
    annual_return_rate: float = Form(...),
    annual_inflation: float = Form(...),
    surplus_lookback_months: int = Form(3),
    manual_monthly_surplus: str = Form(""),
    conn=Depends(get_conn),
):
    db.set_setting(conn, "annual_return_rate", annual_return_rate)
    db.set_setting(conn, "annual_inflation", annual_inflation)
    db.set_setting(conn, "surplus_lookback_months", surplus_lookback_months)
    raw = manual_monthly_surplus.strip()
    if raw and _number(raw, None) is None:
        raise HTTPException(
            status_code=400,
            detail=f"{raw!r} is not a number. Leave the surplus override blank to "
                   "compute it from your transactions, or enter a plain figure.",
        )
    db.set_setting(conn, "manual_monthly_surplus", raw)
    return RedirectResponse("/goals", status_code=303)
