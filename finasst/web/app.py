"""FastAPI + HTMX front end.

Runs on localhost only. No authentication, because there is nothing to
authenticate against -- the database is a file on your machine and the server
binds to 127.0.0.1. Do not expose this to a network.
"""
from __future__ import annotations

import csv
import io
import re
import shutil
import tempfile
from contextlib import asynccontextmanager, contextmanager
from datetime import date
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Form, HTTPException, Request, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import (analytics, config, coverage, db, destinations, income, insights,
                llm, remittance, rules)
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
        # Declared destinations file their transactions as spending. Same
        # reasoning as the matcher above: a whole-table write does not belong
        # in a GET, and the answer only changes at startup and after an import.
        destinations.apply_all(conn)
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
        "top_insights": insights.all_insights(conn, limit=3),
    })


# --------------------------------------------------------------------------
# Transactions
# --------------------------------------------------------------------------

PER_PAGE = 100


@app.get("/transactions", response_class=HTMLResponse)
def transactions(
    request: Request,
    q: str = "",
    category: str = "",
    month: str = "",
    only_uncategorised: bool = False,
    page: int = 1,
    conn=Depends(get_conn),
):
    """The transaction ledger, one page at a time.

    It used to render every matching row in a single response, capped at 200.
    Two hundred rows is a document twenty thousand pixels tall, and the cap
    meant the 201st transaction simply did not exist as far as the page was
    concerned -- with nothing on screen to say so.
    """
    filters = _filters(q, category, month, only_uncategorised)
    total = _count_transactions(conn, filters)
    pages = max((total + PER_PAGE - 1) // PER_PAGE, 1)
    page = min(max(int(page or 1), 1), pages)
    rows = _query_transactions(conn, filters, limit=PER_PAGE, offset=(page - 1) * PER_PAGE)

    return templates.TemplateResponse(request, "transactions.html", {
        "page": "transactions",
        "rows": rows,
        "categories": config.CATEGORIES,
        "months_available": analytics.months_available(conn),
        "q": q, "category": category, "month": filters["month"],
        "only_uncategorised": only_uncategorised,
        "total": total,
        "page_number": page,
        "pages": pages,
        "per_page": PER_PAGE,
        "shown_from": 0 if not total else (page - 1) * PER_PAGE + 1,
        "shown_to": min(page * PER_PAGE, total),
        "query_string": _query_string(q, category, filters["month"], only_uncategorised),
        "filtered": bool(q or category or filters["month"] or only_uncategorised),
        "bulk_result": request.query_params.get("bulk"),
    })


def _filters(q: str, category: str, month: str, only_uncategorised: bool) -> dict:
    return {
        "q": (q or "").strip(),
        "category": category if category in config.CATEGORIES else "",
        "month": analytics.valid_month(month) or "",
        "only_uncategorised": bool(only_uncategorised),
    }


def _query_string(q, category, month, only_uncategorised) -> str:
    from urllib.parse import urlencode

    parts = {}
    if q:
        parts["q"] = q
    if category:
        parts["category"] = category
    if month:
        parts["month"] = month
    if only_uncategorised:
        parts["only_uncategorised"] = "true"
    return urlencode(parts)


def _like(text: str) -> str:
    r"""Escape a user's search text for LIKE.

    Without this, searching for "%" matches every row -- which was more than a
    surprising search result: the bulk-categorise guard checks that a filter is
    non-empty, so a one-character search of "%" satisfied the guard and then
    relabelled the entire database in one click.
    """
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _where(filters: dict) -> tuple[str, list]:
    sql = ["WHERE 1=1"]
    params: list = []
    if filters["q"]:
        sql.append(r"AND (t.description LIKE ? ESCAPE '\' "
                   r"OR t.raw_description LIKE ? ESCAPE '\')")
        params += [_like(filters["q"]), _like(filters["q"])]
    if filters["category"]:
        sql.append("AND t.category = ?")
        params.append(filters["category"])
    if filters["month"]:
        sql.append("AND substr(t.date, 1, 7) = ?")
        params.append(filters["month"])
    if filters["only_uncategorised"]:
        sql.append("AND t.category IS NULL")
    return " ".join(sql), params


def _count_transactions(conn, filters: dict) -> int:
    where, params = _where(filters)
    return int(conn.execute(
        f"SELECT COUNT(*) FROM transactions t {where}", params
    ).fetchone()[0])


def _query_transactions(conn, filters: dict, limit: int = PER_PAGE, offset: int = 0):
    where, params = _where(filters)
    return conn.execute(
        f"""SELECT t.*, a.name AS account_name FROM transactions t
            JOIN accounts a ON a.id = t.account_id {where}
            ORDER BY t.date DESC, t.id DESC LIMIT ? OFFSET ?""",
        [*params, limit, offset],
    ).fetchall()


@app.post("/transactions/bulk")
def bulk_categorise(
    category: str = Form(...),
    q: str = Form(""),
    category_filter: str = Form(""),
    month: str = Form(""),
    only_uncategorised: bool = Form(False),
    conn=Depends(get_conn),
):
    """Apply one category to everything the current filter matches.

    Clearing a backlog one dropdown at a time is the reason the backlog never
    gets cleared. This marks them all as manual corrections, because that is
    what they are -- a rule did not decide this, you did -- which also means a
    later re-run of the rules will not undo the work.
    """
    if category not in config.CATEGORIES:
        raise HTTPException(status_code=400, detail=f"{category!r} is not one of the categories.")
    filters = _filters(q, category_filter, month, only_uncategorised)
    if not any((filters["q"], filters["category"], filters["month"], filters["only_uncategorised"])):
        raise HTTPException(
            status_code=400,
            detail="Refusing to recategorise every transaction in the database. "
                   "Narrow the filter first.",
        )
    where, params = _where(filters)
    ids = [r["id"] for r in conn.execute(f"SELECT t.id FROM transactions t {where}", params)]
    if ids:
        conn.executemany(
            "UPDATE transactions SET category = ?, category_source = 'manual' WHERE id = ?",
            [(category, i) for i in ids],
        )
        conn.commit()
    query = _query_string(filters["q"], filters["category"], filters["month"],
                          filters["only_uncategorised"])
    suffix = f"&bulk={len(ids)}" if query else f"?bulk={len(ids)}"
    return RedirectResponse(f"/transactions?{query}{suffix}" if query else f"/transactions{suffix}",
                            status_code=303)


@app.get("/transactions.csv")
def export_transactions(
    q: str = "",
    category: str = "",
    month: str = "",
    only_uncategorised: bool = False,
    conn=Depends(get_conn),
):
    """The current view as a CSV, so the data is never trapped in this app."""
    filters = _filters(q, category, month, only_uncategorised)
    rows = _query_transactions(conn, filters, limit=1_000_000, offset=0)

    buffer = io.StringIO()
    writer = csv.writer(buffer)

    def safe(value) -> str:
        """Stop a merchant name being executed as a spreadsheet formula.

        A statement description is untrusted text. Excel and LibreOffice treat a
        cell starting = + - or @ as a formula, so exporting one verbatim turns a
        transaction description into code that runs when the file is opened.
        """
        text = "" if value is None else str(value)
        return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text
    writer.writerow(["date", "description", "raw_description", "account", "amount",
                     "currency", "category", "category_source", "source_file"])
    for r in rows:
        writer.writerow([
            r["date"], safe(r["description"]), safe(r["raw_description"]),
            safe(r["account_name"]), f"{r['amount']:.2f}", r["currency"],
            r["category"] or "", r["category_source"] or "", safe(r["source_file"]),
        ])
    stamp = date.today().isoformat()
    return Response(
        content=buffer.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="finasst-transactions-{stamp}.csv"'},
    )


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

def _batches(conn) -> list[dict]:
    """What each imported file put in the database, newest first."""
    rows = conn.execute(
        """SELECT source_file, COUNT(*) n, MIN(date) first, MAX(date) last,
                  MAX(imported_at) at, SUM(-amount) net
           FROM transactions WHERE source_file IS NOT NULL
           GROUP BY source_file ORDER BY at DESC"""
    ).fetchall()
    return [
        {"file": r["source_file"], "count": r["n"], "first": r["first"],
         "last": r["last"], "imported_at": r["at"], "net": round(float(r["net"] or 0), 2)}
        for r in rows
    ]


@app.get("/import", response_class=HTMLResponse)
def import_page(request: Request, conn=Depends(get_conn)):
    return templates.TemplateResponse(request, "import.html", {
        "page": "import",
        "accounts": db.accounts(conn), "results": None,
        "batches": _batches(conn),
        "notice": request.query_params.get("notice"),
    })


@app.post("/import", response_class=HTMLResponse)
def do_import(
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
        # New rows can complete a transfer pair, and can be rows a declared
        # destination already explains, so re-run both here -- the one place
        # other than startup where either answer can change.
        coverage.match_internal_transfers(conn)
        destinations.apply_all(conn)
    return templates.TemplateResponse(request, "import.html", {
        "page": "import",
        "accounts": db.accounts(conn),
        "results": results, "errors": errors, "stats": stats,
        "batches": _batches(conn),
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

# --------------------------------------------------------------------------
# Coverage: resolving the gap by hand
# --------------------------------------------------------------------------

@app.get("/coverage", response_class=HTMLResponse)
def coverage_page(request: Request, conn=Depends(get_conn)):
    cov = coverage.coverage(conn)
    res = destinations.resolution(conn)
    return templates.TemplateResponse(request, "coverage.html", {
        "page": "coverage",
        "coverage": cov,
        "resolution": res,
        "declarations": destinations.all_destinations(conn),
        "kinds": destinations.KINDS,
        "categories": [c for c in config.CATEGORIES if c not in config.NON_SPEND_CATEGORIES],
        "accounts": db.accounts(conn),
    })


@app.post("/coverage/destinations")
def declare_destination(
    pattern: str = Form(...),
    label: str = Form(...),
    kind: str = Form(...),
    category: str = Form(""),
    account_id: str = Form(""),
    notes: str = Form(""),
    conn=Depends(get_conn),
):
    try:
        dest = destinations.Destination(
            pattern=pattern.strip(), label=label.strip(), kind=kind,
            category=(category.strip() or None),
            account_id=int(account_id) if account_id.strip() else None,
            notes=notes,
        )
        destinations.add(conn, dest)
    except ValueError as exc:
        # The one that fires in practice: "genuinely left" with no category,
        # which would explain the outflow while still hiding the spending.
        raise HTTPException(status_code=400, detail=str(exc))
    destinations.apply_all(conn)
    return RedirectResponse("/coverage", status_code=303)


@app.post("/coverage/destinations/{dest_id}/delete")
def undeclare_destination(dest_id: int, conn=Depends(get_conn)):
    destinations.delete(conn, dest_id)
    return RedirectResponse("/coverage", status_code=303)


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


# --------------------------------------------------------------------------
# Insights
# --------------------------------------------------------------------------

@app.get("/insights", response_class=HTMLResponse)
def insights_page(request: Request, conn=Depends(get_conn)):
    found = insights.all_insights(conn)
    return templates.TemplateResponse(request, "insights.html", {
        "page": "insights",
        "insights": found,
        "by_severity": {
            level: [i for i in found if i.severity == level]
            for level in ("flag", "notable", "watch")
        },
        "forecast": insights.forecast(conn),
        "llm": llm.status(conn),
        "suggestions": llm.suggestions(conn),
        "summary": db.get_setting(conn, "llm_last_summary", ""),
        "notice": request.query_params.get("notice"),
        "error": request.query_params.get("error"),
    })


@app.post("/insights/suggest")
def run_suggestions(conn=Depends(get_conn)):
    """Ask the model about merchants no rule matched. One request, deduplicated."""
    try:
        made = llm.suggest_categories(conn)
    except llm.LLMError as exc:
        return RedirectResponse(f"/insights?error={exc}", status_code=303)
    labelled = [s for s in made if s.category]
    return RedirectResponse(
        f"/insights?notice=Asked about {len(made)} merchants in one request; "
        f"{len(labelled)} came back with a category. Nothing has been applied — "
        "accept the ones you agree with.",
        status_code=303,
    )


@app.post("/insights/suggest/{merchant_key}/accept")
def accept_suggestion(merchant_key: str, category: str = Form(""), conn=Depends(get_conn)):
    try:
        chosen = llm.accept(conn, merchant_key, category or None)
    except (llm.LLMError, rules.RuleError) as exc:
        return RedirectResponse(f"/insights?error={exc}", status_code=303)
    stats = rules.recategorise(conn)
    return RedirectResponse(
        f"/insights?notice=Added a rule: “{merchant_key}” is {chosen}. "
        f"{stats['unmatched']} transactions still need a look.",
        status_code=303,
    )


@app.post("/insights/suggest/{merchant_key}/reject")
def reject_suggestion(merchant_key: str, conn=Depends(get_conn)):
    llm.reject(conn, merchant_key)
    return RedirectResponse(
        f"/insights?notice=Rejected “{merchant_key}”. It will not be sent again.",
        status_code=303,
    )


@app.post("/insights/narrate")
def narrate_insights(conn=Depends(get_conn)):
    found = insights.all_insights(conn)
    try:
        text = llm.narrate(conn, found, insights.forecast(conn).basis)
    except llm.LLMError as exc:
        return RedirectResponse(f"/insights?error={exc}", status_code=303)
    db.set_setting(conn, "llm_last_summary", text)
    return RedirectResponse("/insights", status_code=303)


@app.post("/insights/llm-settings")
def save_llm_settings(
    llm_url: str = Form(""),
    llm_model: str = Form(""),
    llm_enabled: bool = Form(False),
    conn=Depends(get_conn),
):
    db.set_setting(conn, "llm_url", llm_url.strip())
    db.set_setting(conn, "llm_model", llm_model.strip())
    db.set_setting(conn, "llm_enabled", "1" if llm_enabled else "")
    return RedirectResponse("/insights?notice=Saved.", status_code=303)


# --------------------------------------------------------------------------
# Rules
# --------------------------------------------------------------------------

@app.get("/rules", response_class=HTMLResponse)
def rules_page(request: Request, test: str = "", conn=Depends(get_conn)):
    everything = rules.inventory(conn)
    mine = sorted([r for r in everything if r.is_user], key=lambda r: -r.matches)
    builtin = sorted([r for r in everything if not r.is_user], key=lambda r: -r.matches)
    return templates.TemplateResponse(request, "rules.html", {
        "page": "rules",
        "mine": mine,
        "builtin": builtin,
        "idle": [r for r in builtin if r.matches == 0],
        "categories": config.CATEGORIES,
        "test": test,
        "explanation": rules.explain(conn, test) if test.strip() else None,
        "uncategorised_count": analytics.uncategorised_count(conn),
        "top_insights": insights.all_insights(conn, limit=3),
        "notice": request.query_params.get("notice"),
    })


@app.post("/rules")
def create_rule(
    pattern: str = Form(...),
    category: str = Form(...),
    match_type: str = Form("words"),
    conn=Depends(get_conn),
):
    try:
        rules.add_rule(conn, pattern, category, match_type)
    except rules.RuleError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    stats = rules.recategorise(conn)
    return RedirectResponse(
        f"/rules?notice=Added “{pattern.strip()}”. {stats['matched']} transactions "
        f"re-filed, {stats['unmatched']} still need a look.",
        status_code=303,
    )


@app.post("/rules/{rule_id}/delete")
def drop_rule(rule_id: int, conn=Depends(get_conn)):
    try:
        removed = rules.remove_rule(conn, rule_id)
    except rules.RuleError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    stats = rules.recategorise(conn)
    return RedirectResponse(
        f"/rules?notice=Removed “{removed.pattern}”. {stats['unmatched']} "
        "transactions now need a look.",
        status_code=303,
    )


@app.post("/rules/recategorise")
def rerun_rules(conn=Depends(get_conn)):
    stats = rules.recategorise(conn)
    return RedirectResponse(
        f"/rules?notice=Re-ran every rule: {stats['matched']} filed, "
        f"{stats['unmatched']} left. Your manual corrections were not touched.",
        status_code=303,
    )


# --------------------------------------------------------------------------
# Import batches
# --------------------------------------------------------------------------

@app.post("/import/undo")
def undo_import(source_file: str = Form(...), conn=Depends(get_conn)):
    """Remove everything one file brought in.

    Importing the wrong file, or the right file against the wrong account, was
    previously permanent: source_file was recorded on every row and then never
    used for anything. Manual corrections go with it, which is the honest
    outcome -- they were corrections to rows that are being deleted.
    """
    cur = conn.execute("DELETE FROM transactions WHERE source_file = ?", (source_file,))
    conn.commit()
    coverage.match_internal_transfers(conn)
    return RedirectResponse(
        f"/import?notice=Removed {cur.rowcount} transactions imported from "
        f"“{source_file}”.",
        status_code=303,
    )


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
