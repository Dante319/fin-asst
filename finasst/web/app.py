"""FastAPI + HTMX front end.

Runs on localhost only. No authentication, because there is nothing to
authenticate against -- the database is a file on your machine and the server
binds to 127.0.0.1. Do not expose this to a network.
"""
from __future__ import annotations

import shutil
import tempfile
from datetime import date
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Form, Request, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import analytics, config, coverage, db, remittance
from ..categorize import categorize_all, seed_rules, set_manual_category
from ..goals import Goal, add_goal, compare, load_goals, project
from ..importers import import_file

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.filters["money"] = lambda v: f"${v:,.0f}" if v is not None else "-"
templates.env.filters["money2"] = lambda v: f"${v:,.2f}" if v is not None else "-"

app = FastAPI(title="fin-asst")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


def get_conn():
    conn = db.connect()
    db.init_db(conn)
    seed_rules(conn)
    return conn


def _settings(conn) -> dict:
    return {
        "annual_return_rate": float(db.get_setting(conn, "annual_return_rate", "0.04")),
        "annual_inflation": float(db.get_setting(conn, "annual_inflation", "0.025")),
        "lookback": int(db.get_setting(conn, "surplus_lookback_months", "3")),
        "manual_surplus": db.get_setting(conn, "manual_monthly_surplus", ""),
    }


def _surplus(conn) -> tuple[float, str, Optional[str]]:
    s = _settings(conn)
    if s["manual_surplus"].strip():
        return float(s["manual_surplus"]), "set by hand", None
    avg = analytics.average_surplus(conn, s["lookback"])
    basis = ", ".join(avg["basis"]) or "no complete months yet"
    return avg["surplus"], f"average of {basis}", avg.get("warning")


# --------------------------------------------------------------------------
# Dashboard
# --------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, month: Optional[str] = None, months: int = 1):
    conn = get_conn()
    available = analytics.months_available(conn)
    totals = analytics.monthly_totals(conn)
    selected = month or (available[0] if available else None)
    surplus, basis, warning = _surplus(conn)

    coverage.match_internal_transfers(conn)
    cov = coverage.coverage(conn)

    return templates.TemplateResponse(request, "dashboard.html", {
        "page": "dashboard",
        "coverage": cov,
        "income_warning": coverage.income_regime_warning(conn),
        "has_data": bool(totals),
        "months_available": available,
        "selected_month": selected,
        "window": months,
        "totals": totals[-12:],
        "categories": analytics.category_breakdown(conn, selected, months),
        "merchants": analytics.top_merchants(conn, selected, months, limit=10),
        "recurring": analytics.recurring_charges(conn)[:12],
        "surplus": surplus,
        "surplus_basis": basis,
        "surplus_warning": warning,
        "uncategorised_count": len(analytics.uncategorised(conn, limit=1000)),
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
):
    conn = get_conn()
    rows = _query_transactions(conn, q, category, month, only_uncategorised, limit)
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
def recategorise(request: Request, tx_id: int, category: str = Form(...), teach: bool = Form(False)):
    """Inline recategorisation. Returns just the updated row for HTMX to swap in."""
    conn = get_conn()
    learned = set_manual_category(conn, tx_id, category, teach=teach)
    row = conn.execute(
        """SELECT t.*, a.name AS account_name FROM transactions t
           JOIN accounts a ON a.id = t.account_id WHERE t.id = ?""",
        (tx_id,),
    ).fetchone()
    return templates.TemplateResponse(request, "_tx_row.html", {
        "tx": row, "categories": config.CATEGORIES, "learned": learned,
    })


# --------------------------------------------------------------------------
# Import
# --------------------------------------------------------------------------

@app.get("/import", response_class=HTMLResponse)
def import_page(request: Request):
    conn = get_conn()
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
):
    conn = get_conn()
    results, errors = [], []
    for upload in files:
        if not upload.filename:
            continue
        tmp = Path(tempfile.gettempdir()) / upload.filename
        with open(tmp, "wb") as fh:
            shutil.copyfileobj(upload.file, fh)
        try:
            results.append(import_file(
                conn, tmp,
                account_name=account.strip() or Path(upload.filename).stem,
                issuer=issuer or None, kind=kind,
            ))
        except Exception as exc:
            errors.append(f"{upload.filename}: {exc}")
        finally:
            tmp.unlink(missing_ok=True)

    stats = categorize_all(conn)
    return templates.TemplateResponse(request, "import.html", {
        "page": "import",
        "accounts": db.accounts(conn),
        "results": results, "errors": errors, "stats": stats,
    })


# --------------------------------------------------------------------------
# Remittances
# --------------------------------------------------------------------------

@app.get("/remittances", response_class=HTMLResponse)
def remittances_page(request: Request):
    conn = get_conn()
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
):
    conn = get_conn()
    remittance.sync_from_transactions(conn)
    remittance.set_received(conn, tx_id, received, provider=provider or None, currency=currency)
    return RedirectResponse("/remittances", status_code=303)


@app.post("/remittances/rates/fetch")
def fetch_reference_rates():
    """The only route in the app that touches the network, and only when clicked."""
    conn = get_conn()
    remittance.sync_from_transactions(conn)
    rep = remittance.report(conn)
    remittance.fetch_rates(conn, [t.date for t in rep.transfers])
    return RedirectResponse("/remittances", status_code=303)


# --------------------------------------------------------------------------
# Goals
# --------------------------------------------------------------------------

@app.get("/goals", response_class=HTMLResponse)
def goals_page(request: Request, surplus: Optional[float] = None, without: str = ""):
    conn = get_conn()
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
):
    conn = get_conn()
    add_goal(conn, Goal(
        name=name.strip(),
        target_amount=target_amount,
        target_date=date.fromisoformat(target_date) if target_date else None,
        saved_so_far=saved_so_far, priority=priority,
        monthly_min=monthly_min, notes=notes,
    ))
    return RedirectResponse("/goals", status_code=303)


@app.post("/goals/{goal_id}/delete")
def delete_goal(goal_id: int):
    conn = get_conn()
    conn.execute("DELETE FROM goals WHERE id = ?", (goal_id,))
    conn.commit()
    return RedirectResponse("/goals", status_code=303)


@app.post("/settings")
def update_settings(
    annual_return_rate: float = Form(...),
    annual_inflation: float = Form(...),
    surplus_lookback_months: int = Form(3),
    manual_monthly_surplus: str = Form(""),
):
    conn = get_conn()
    db.set_setting(conn, "annual_return_rate", annual_return_rate)
    db.set_setting(conn, "annual_inflation", annual_inflation)
    db.set_setting(conn, "surplus_lookback_months", surplus_lookback_months)
    db.set_setting(conn, "manual_monthly_surplus", manual_monthly_surplus.strip())
    return RedirectResponse("/goals", status_code=303)
