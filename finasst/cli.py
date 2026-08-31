"""Command line interface.

    finasst init
    finasst import <file.csv|.xlsx|.pdf> --account "Amex SimplyCash" [--issuer amex]
    finasst categorize [--recategorize]
    finasst review
    finasst summary [--month 2026-07] [--months 3]
    finasst goal add "Chennai house" --amount 250000 --date 2031-06-01 --priority 20
    finasst goals
    finasst project [--surplus 2500]
    finasst whatif --without "Paris trip"
    finasst serve
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from . import analytics, config, coverage, db, remittance
from .categorize import categorize_all, seed_rules, set_manual_category
from .goals import Goal, add_goal, compare, load_goals, project
from .importers import import_file


def _money(x: float) -> str:
    return f"${x:,.2f}"


def _conn():
    conn = db.connect()
    db.init_db(conn)
    return conn


def cmd_init(args) -> int:
    conn = _conn()
    added = seed_rules(conn)
    print(f"Database ready at {config.DB_PATH}")
    if config.DATA_DIR == config.LEGACY_DATA_DIR:
        print("Note: this database sits inside the project folder. If that folder is "
              "synced\n      (iCloud Drive, Dropbox, OneDrive), move it -- see the macOS "
              "note in the README.")
    print(f"Seeded {added} categorisation rules.")
    return 0


def cmd_import(args) -> int:
    conn = _conn()
    seed_rules(conn)
    paths = [Path(p) for p in args.files]
    for path in paths:
        if not path.exists():
            print(f"  ! {path} does not exist", file=sys.stderr)
            continue
        result = import_file(
            conn, path, account_name=args.account or path.stem,
            issuer=args.issuer, kind=args.kind, currency=args.currency,
        )
        print("  " + result.summary)
    stats = categorize_all(conn)
    print(f"Categorised {stats['matched']}, {stats['unmatched']} need a look "
          f"(`finasst review`).")
    return 0


def cmd_categorize(args) -> int:
    conn = _conn()
    seed_rules(conn)
    stats = categorize_all(conn, recategorize=args.recategorize)
    print(f"Considered {stats['considered']}: {stats['matched']} categorised, "
          f"{stats['unmatched']} left.")
    return 0


def cmd_review(args) -> int:
    conn = _conn()
    rows = analytics.uncategorised(conn, limit=args.limit)
    if not rows:
        print("Nothing uncategorised.")
        return 0
    print(f"{len(rows)} uncategorised, largest first:\n")
    for r in rows:
        print(f"  [{r['id']:>5}] {r['date']}  {_money(r['amount']):>12}  "
              f"{r['description'][:48]:<48} ({r['account_name']})")
    print("\nCategorise with:  finasst set <id> \"<Category>\"")
    print("Categories: " + ", ".join(config.CATEGORIES))
    return 0


def cmd_set(args) -> int:
    conn = _conn()
    learned = set_manual_category(conn, args.tx_id, args.category, teach=not args.no_teach)
    print(f"Transaction {args.tx_id} -> {args.category}")
    if learned:
        print(f"Learned rule: anything matching '{learned}' is now {args.category}.")
    return 0


def cmd_summary(args) -> int:
    conn = _conn()
    totals = analytics.monthly_totals(conn)
    if not totals:
        print("No transactions yet. Try: finasst import <file.csv> --account \"...\"")
        return 1

    print("Month      Income        Spend       Saved      Surplus")
    print("-" * 58)
    for m in totals[-12:]:
        print(f"{m['month']}  {_money(m['income']):>11}  {_money(m['spend']):>11}  "
              f"{_money(m['saved']):>10}  {_money(m['surplus']):>11}")

    month = args.month or totals[-1]["month"]
    print(f"\nCategories -- {month}" + (f" and the {args.months - 1} months before" if args.months > 1 else ""))
    print("-" * 58)
    for row in analytics.category_breakdown(conn, month, args.months):
        bar = "#" * int(row["share"] / 2)
        print(f"{row['category']:<22} {_money(row['spend']):>11}  {row['share']:>5.1f}%  {bar}")

    avg = analytics.average_surplus(conn, int(db.get_setting(conn, "surplus_lookback_months", "3")))
    print(f"\nAverage monthly surplus ({', '.join(avg['basis']) or 'n/a'}): {_money(avg['surplus'])}")
    if avg.get("warning"):
        print(f"  Note: {avg['warning']}")

    coverage.match_internal_transfers(conn)
    cov = coverage.coverage(conn)
    if cov.unmatched_outflow > 0.01:
        print(f"\n  ! {cov.verdict()}")
        print("    Run `finasst coverage` for the detail.")
    regime = coverage.income_regime_warning(conn)
    if regime:
        print(f"\n  ! {regime}")
    return 0


def cmd_coverage(args) -> int:
    conn = _conn()
    match = coverage.match_internal_transfers(conn)
    cov = coverage.coverage(conn, since=args.since)
    print(f"Accounts imported: {', '.join(cov.accounts) or 'none'}")
    if match.matched_pairs:
        print(f"Matched {match.matched_pairs} internal transfers "
              f"({_money(match.matched_amount)}) between your own accounts.")
    print()
    print(cov.verdict())
    if cov.destinations:
        print("\nWhere unmatched money went:")
        for d in cov.destinations:
            print(f"  {_money(d['amount']):>12}  x{d['count']:<3} {d['destination'][:48]}")
        print("\nImport the account on the receiving end and these stop being a gap.")
    warning = coverage.income_regime_warning(conn)
    if warning:
        print(f"\nIncome: {warning}")
    return 0


def cmd_fx_list(args) -> int:
    conn = _conn()
    created = remittance.sync_from_transactions(conn)
    if created:
        print(f"Added {created} transfers to the ledger.\n")
    rep = remittance.report(conn)
    if not rep.transfers:
        print(rep.coverage_note())
        return 0
    print("   id  date        sent        received      rate    vs mid   cost")
    print("-" * 72)
    for t in rep.transfers:
        rate = f"{t.effective_rate:,.2f}" if t.effective_rate else "-"
        spread = f"{t.spread_pct:+.1f}%" if t.spread_pct is not None else "-"
        cost = _money(t.cost_cad) if t.cost_cad is not None else "-"
        recv = f"{t.received:,.0f} {t.currency}" if t.received else "not recorded"
        print(f"  {t.tx_id:>4}  {t.date}  {_money(t.sent):>9}  {recv:>16}  {rate:>8}  {spread:>7}  {cost:>8}")
    print()
    print(rep.coverage_note())
    return 0


def cmd_fx_set(args) -> int:
    conn = _conn()
    remittance.sync_from_transactions(conn)
    remittance.set_received(conn, args.tx_id, args.received, fee=args.fee,
                            provider=args.provider, currency=args.currency)
    rep = remittance.report(conn)
    t = next((x for x in rep.transfers if x.tx_id == args.tx_id), None)
    if t and t.effective_rate:
        line = f"Transfer {args.tx_id}: {_money(t.sent)} -> {t.received:,.0f} {t.currency} " \
               f"= {t.effective_rate:,.2f} {t.currency}/CAD"
        if t.spread_pct is not None:
            line += f", {t.spread_pct:+.1f}% against the reference rate ({_money(t.cost_cad)})"
        else:
            line += ". Run `finasst fx rates` for a mid-market comparison."
        print(line)
    return 0


def cmd_fx_rates(args) -> int:
    conn = _conn()
    remittance.sync_from_transactions(conn)
    rep = remittance.report(conn)
    dates = [t.date for t in rep.transfers]
    if not dates:
        print("No remittances to price.")
        return 0
    print(f"Fetching reference rates for {len(set(dates))} dates "
          f"(European Central Bank, via frankfurter.app -- free, no account)...")
    stored, errors = remittance.fetch_rates(conn, dates, quote=args.currency)
    print(f"Stored {stored} rates.")
    for e in errors[:5]:
        print(f"  ! {e}")
    if errors:
        print("  Reference rates are optional -- effective rates still work without them.")
    return 0


def cmd_fx_report(args) -> int:
    conn = _conn()
    remittance.sync_from_transactions(conn)
    rep = remittance.report(conn)
    print(f"Remittances: {len(rep.transfers)} transfers, {_money(rep.total_sent)} sent")
    if rep.priced:
        print(f"Received:    {rep.total_received:,.0f} {rep.priced[0].currency}")
        print(f"Blended rate: {rep.blended_rate:,.2f} {rep.priced[0].currency} per CAD")
        if rep.total_cost is not None:
            pct = 100 * rep.total_cost / sum(t.sent for t in rep.priced)
            print(f"Cost of the spread: {_money(rep.total_cost)} ({pct:.1f}% of what you sent)")
    print()
    print(rep.coverage_note())
    if rep.missing_received:
        ids = ", ".join(str(t.tx_id) for t in rep.missing_received[:8])
        print(f"Missing a received amount: {ids}{' ...' if len(rep.missing_received) > 8 else ''}")
    return 0


def cmd_goal_add(args) -> int:
    conn = _conn()
    goal = Goal(
        name=args.name,
        target_amount=args.amount,
        target_date=date.fromisoformat(args.date) if args.date else None,
        saved_so_far=args.saved,
        priority=args.priority,
        monthly_min=args.monthly_min,
        notes=args.notes or "",
    )
    add_goal(conn, goal)
    print(f"Saved goal: {goal.name} -- {_money(goal.target_amount)} by "
          f"{goal.target_date or 'no date'} (priority {goal.priority})")
    return 0


def cmd_goals(args) -> int:
    conn = _conn()
    goals = load_goals(conn)
    if not goals:
        print("No goals yet. Add one with: finasst goal add \"Name\" --amount 20000 --date 2027-06-01")
        return 0
    print("Pri  Goal                       Target        By           Saved")
    print("-" * 68)
    for g in goals:
        print(f"{g.priority:>3}  {g.name[:24]:<24}  {_money(g.target_amount):>11}  "
              f"{str(g.target_date or '-'):<11}  {_money(g.saved_so_far):>10}")
    return 0


def _surplus_for(conn, override: float | None) -> tuple[float, str]:
    if override is not None:
        return override, "you supplied it"
    manual = db.get_setting(conn, "manual_monthly_surplus", "")
    if manual.strip():
        return float(manual), "set in settings"
    lookback = int(db.get_setting(conn, "surplus_lookback_months", "3"))
    avg = analytics.average_surplus(conn, lookback)
    if avg.get("warning"):
        print(f"Note: {avg['warning']}")
    return avg["surplus"], f"average of {', '.join(avg['basis']) or 'no months'}"


def cmd_project(args) -> int:
    conn = _conn()
    goals = load_goals(conn)
    if not goals:
        print("No active goals to project.")
        return 1
    surplus, basis = _surplus_for(conn, args.surplus)
    p = project(
        goals, surplus,
        annual_return_rate=float(db.get_setting(conn, "annual_return_rate", "0.04")),
        annual_inflation=float(db.get_setting(conn, "annual_inflation", "0.025")),
    )
    print(f"Monthly surplus: {_money(p.monthly_surplus)}  ({basis})\n")
    print("Goal                      Target       Wanted by    Projected    Verdict")
    print("-" * 84)
    for g in p.goals:
        verdict = "on track" if g.on_track else (
            f"{g.slip_months} months late" if g.slip_months else "not reachable"
        )
        print(f"{g.name[:24]:<24}  {_money(g.target_amount):>11}  "
              f"{str(g.target_date or '-'):<11}  "
              f"{str(g.projected_date or '-'):<11}  {verdict}")
        if g.note:
            print(f"{'':<24}  {g.note}")
    if p.warnings:
        print()
        for w in p.warnings:
            print(f"  ! {w}")
    return 0


def cmd_whatif(args) -> int:
    conn = _conn()
    goals = load_goals(conn)
    surplus, _ = _surplus_for(conn, args.surplus)
    result = compare(goals, surplus, without=args.without, surplus_delta=args.surplus_delta,
                     annual_return_rate=float(db.get_setting(conn, "annual_return_rate", "0.04")),
                     annual_inflation=float(db.get_setting(conn, "annual_inflation", "0.025")))
    label = f"dropping '{args.without}'" if args.without else f"surplus {args.surplus_delta:+,.0f}/month"
    print(f"Effect of {label}:\n")
    print("Goal                      Baseline     Scenario     Change")
    print("-" * 66)
    for d in result["deltas"]:
        if d["months_earlier"] is None:
            change = "-"
        elif d["months_earlier"] > 0:
            change = f"{d['months_earlier']} months earlier"
        elif d["months_earlier"] < 0:
            change = f"{-d['months_earlier']} months later"
        else:
            change = "no change"
        print(f"{d['goal'][:24]:<24}  {str(d['baseline_date'] or '-'):<11}  "
              f"{str(d['variant_date'] or '-'):<11}  {change}")
    return 0


def cmd_serve(args) -> int:
    """Start the local web app.

    The port is checked before anything is printed: announcing a URL that turns
    out not to be listening sends you hunting through the browser instead of the
    one line of terminal output that explains it.
    """
    import socket
    import time

    t0 = time.monotonic()
    import uvicorn
    load = time.monotonic() - t0

    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        probe.bind((args.host, args.port))
    except OSError as exc:
        print(f"Cannot start on {args.host}:{args.port} -- {exc.strerror}.", file=sys.stderr)
        print(f"Something else is using that port. Try: finasst serve --port {args.port + 1}",
              file=sys.stderr)
        return 1
    finally:
        probe.close()

    if load > 2:
        print(f"(uvicorn took {load:.1f}s to import -- if that keeps happening, the "
              f"virtualenv is on a slow or synced filesystem)", flush=True)
    print(f"fin-asst starting on http://{args.host}:{args.port}", flush=True)
    print("Open that in a browser. Type the http:// prefix -- browsers that default",
          flush=True)
    print("to https will fail against a plain local server. Ctrl-C to stop.\n", flush=True)
    uvicorn.run("finasst.web.app:app", host=args.host, port=args.port,
                reload=args.reload, log_level="info")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="finasst", description="Local-first personal finance assistant")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create the database and seed rules").set_defaults(func=cmd_init)

    imp = sub.add_parser("import", help="import statements (CSV, Excel, or PDF)")
    imp.add_argument("files", nargs="+")
    imp.add_argument("--account", help="account name these rows belong to")
    imp.add_argument("--issuer", choices=["amex", "amex-yearend", "simplii", "simplii-cc-pdf",
                                           "simplii-chequing-pdf", "eqbank", "generic"],
                     help="force a parser instead of detecting it")
    imp.add_argument("--kind", default="credit", choices=["credit", "chequing", "savings"])
    imp.add_argument("--currency", default="CAD")
    imp.set_defaults(func=cmd_import)

    cat = sub.add_parser("categorize", help="apply rules to transactions")
    cat.add_argument("--recategorize", action="store_true", help="also redo rule-assigned ones")
    cat.set_defaults(func=cmd_categorize)

    rev = sub.add_parser("review", help="list transactions no rule matched")
    rev.add_argument("--limit", type=int, default=50)
    rev.set_defaults(func=cmd_review)

    st = sub.add_parser("set", help="categorise one transaction and learn the merchant")
    st.add_argument("tx_id", type=int)
    st.add_argument("category")
    st.add_argument("--no-teach", action="store_true", help="do not create a rule from this")
    st.set_defaults(func=cmd_set)

    cv = sub.add_parser("coverage", help="how much of your money the app can actually see")
    cv.add_argument("--since", help="only consider transactions on or after this date")
    cv.set_defaults(func=cmd_coverage)

    fx = sub.add_parser("fx", help="remittance and exchange-rate ledger")
    fxsub = fx.add_subparsers(dest="fx_command", required=True)
    fxl = fxsub.add_parser("list", help="every transfer, and what it really cost")
    fxl.set_defaults(func=cmd_fx_list)
    fxs = fxsub.add_parser("set", help="record what actually arrived at the other end")
    fxs.add_argument("tx_id", type=int)
    fxs.add_argument("--received", type=float, required=True, help="amount credited, in the destination currency")
    fxs.add_argument("--currency", default="INR")
    fxs.add_argument("--fee", type=float)
    fxs.add_argument("--provider")
    fxs.set_defaults(func=cmd_fx_set)
    fxr = fxsub.add_parser("rates", help="fetch reference mid-market rates (the only command that uses the network)")
    fxr.add_argument("--currency", default="INR")
    fxr.set_defaults(func=cmd_fx_rates)
    fxp = fxsub.add_parser("report", help="totals, blended rate, and what the spread cost")
    fxp.set_defaults(func=cmd_fx_report)

    sm = sub.add_parser("summary", help="monthly totals and category breakdown")
    sm.add_argument("--month")
    sm.add_argument("--months", type=int, default=1)
    sm.set_defaults(func=cmd_summary)

    goal = sub.add_parser("goal", help="manage goals")
    goalsub = goal.add_subparsers(dest="goal_command", required=True)
    ga = goalsub.add_parser("add", help="add or update a goal")
    ga.add_argument("name")
    ga.add_argument("--amount", type=float, required=True)
    ga.add_argument("--date", help="target date, YYYY-MM-DD")
    ga.add_argument("--saved", type=float, default=0.0)
    ga.add_argument("--priority", type=int, default=100, help="lower is funded first")
    ga.add_argument("--monthly-min", type=float, default=0.0)
    ga.add_argument("--notes")
    ga.set_defaults(func=cmd_goal_add)

    sub.add_parser("goals", help="list goals").set_defaults(func=cmd_goals)

    pr = sub.add_parser("project", help="project all goals against one surplus")
    pr.add_argument("--surplus", type=float)
    pr.set_defaults(func=cmd_project)

    wi = sub.add_parser("whatif", help="see what a change costs your other goals")
    wi.add_argument("--without", help="goal to drop in the scenario")
    wi.add_argument("--surplus-delta", type=float, default=0.0)
    wi.add_argument("--surplus", type=float)
    wi.set_defaults(func=cmd_whatif)

    sv = sub.add_parser("serve", help="run the local web app")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8000)
    sv.add_argument("--reload", action="store_true")
    sv.set_defaults(func=cmd_serve)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
