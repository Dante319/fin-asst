"""Goal projection: several goals competing for one monthly surplus.

The honest premise of this module: you have one pot of money each month, and
every goal you add takes months away from the others. Rather than reporting
each goal in isolation (which always flatters you), it simulates them together,
month by month, in priority order, and tells you which ones slip.

Assumptions are explicit and adjustable: a nominal return on money already
saved, and inflation applied to targets quoted in today's dollars. Neither is
a prediction -- they are levers you set and can zero out.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date
from typing import Iterable, Optional

MAX_MONTHS = 480  # 40 years; past this a projection is fiction anyway

# A goal with no deadline still has to compete, or a high-priority undated goal
# (an emergency fund, say) would be starved forever by a low-priority dated one.
# It asks for its remaining gap spread over this many months.
UNDATED_HORIZON_MONTHS = 24


# --------------------------------------------------------------------------
# Goal records
# --------------------------------------------------------------------------

@dataclass
class Goal:
    name: str
    target_amount: float
    target_date: Optional[date] = None
    saved_so_far: float = 0.0
    priority: int = 100
    monthly_min: float = 0.0
    inflate_target: bool = True
    id: Optional[int] = None
    notes: str = ""

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Goal":
        return cls(
            id=int(row["id"]),
            name=row["name"],
            target_amount=float(row["target_amount"]),
            target_date=date.fromisoformat(row["target_date"]) if row["target_date"] else None,
            saved_so_far=float(row["saved_so_far"]),
            priority=int(row["priority"]),
            monthly_min=float(row["monthly_min"]),
            notes=row["notes"] or "",
        )


@dataclass
class GoalProjection:
    name: str
    target_amount: float          # nominal, after inflation, at the projected date
    target_date: Optional[date]
    projected_date: Optional[date]
    months_to_fund: Optional[int]
    total_allocated: float
    avg_monthly_allocation: float
    on_track: bool
    slip_months: Optional[int]
    shortfall_at_target_date: float
    required_monthly: Optional[float]
    note: str = ""


@dataclass
class Projection:
    monthly_surplus: float
    goals: list[GoalProjection]
    timeline: list[dict] = field(default_factory=list)
    unfunded: list[str] = field(default_factory=list)
    total_committed_monthly: float = 0.0
    warnings: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------

def add_goal(conn: sqlite3.Connection, goal: Goal) -> int:
    cur = conn.execute(
        """INSERT INTO goals(name, target_amount, target_date, saved_so_far,
                             priority, monthly_min, notes)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(name) DO UPDATE SET
             target_amount = excluded.target_amount,
             target_date   = excluded.target_date,
             saved_so_far  = excluded.saved_so_far,
             priority      = excluded.priority,
             monthly_min   = excluded.monthly_min,
             notes         = excluded.notes""",
        (goal.name, goal.target_amount,
         goal.target_date.isoformat() if goal.target_date else None,
         goal.saved_so_far, goal.priority, goal.monthly_min, goal.notes),
    )
    conn.commit()
    return int(cur.lastrowid)


def load_goals(conn: sqlite3.Connection, active_only: bool = True) -> list[Goal]:
    sql = "SELECT * FROM goals"
    if active_only:
        sql += " WHERE active = 1"
    sql += " ORDER BY priority ASC, target_date ASC"
    return [Goal.from_row(r) for r in conn.execute(sql).fetchall()]


# --------------------------------------------------------------------------
# The simulation
# --------------------------------------------------------------------------

def _months_between(a: date, b: date) -> int:
    return (b.year - a.year) * 12 + (b.month - a.month)


def _add_months(d: date, n: int) -> date:
    total = d.year * 12 + (d.month - 1) + n
    return date(total // 12, total % 12 + 1, 1)


def required_monthly(
    goal: Goal, start: date, monthly_return: float, monthly_inflation: float
) -> Optional[float]:
    """What this goal alone would need each month to land on its target date."""
    if not goal.target_date:
        return None
    n = _months_between(start, goal.target_date)
    if n <= 0:
        return None
    target = goal.target_amount
    if goal.inflate_target:
        target *= (1 + monthly_inflation) ** n
    # Future value of the existing balance, compounded to the target date.
    fv_existing = goal.saved_so_far * (1 + monthly_return) ** n
    needed = target - fv_existing
    if needed <= 0:
        return 0.0
    if monthly_return == 0:
        return needed / n
    # Future value of an ordinary annuity, solved for the payment.
    annuity_factor = ((1 + monthly_return) ** n - 1) / monthly_return
    return needed / annuity_factor


def project(
    goals: Iterable[Goal],
    monthly_surplus: float,
    annual_return_rate: float = 0.04,
    annual_inflation: float = 0.025,
    start: Optional[date] = None,
    horizon_months: int = MAX_MONTHS,
) -> Projection:
    """Simulate all goals drawing on one surplus, month by month.

    Allocation order within each month:
      1. every active goal gets its monthly_min floor, in priority order
      2. whatever is left is offered to goals in priority order, each taking
         up to what it needs to stay on schedule (an undated goal asks for its
         gap spread over UNDATED_HORIZON_MONTHS, so priority still means
         something for it)
      3. any remainder accelerates the highest-priority unfinished goal

    Priority is the whole story when money is short: goal 10 is fully funded
    before goal 30 sees a dollar beyond its monthly_min floor.
    """
    start = start or date.today().replace(day=1)
    r = annual_return_rate / 12
    infl = annual_inflation / 12

    goals = sorted(goals, key=lambda g: (g.priority, g.target_date or date.max))
    warnings: list[str] = []

    if monthly_surplus <= 0:
        warnings.append(
            "Monthly surplus is zero or negative, so no goal can be funded from "
            "cash flow. Everything below is what your existing balances do on their own."
        )

    state = {
        g.name: {
            "goal": g,
            "balance": g.saved_so_far,
            "allocated": 0.0,
            "months_funded": 0,
            "done_month": None,
            "target_nominal": g.target_amount,
        }
        for g in goals
    }

    timeline: list[dict] = []

    for m in range(horizon_months):
        current = _add_months(start, m)

        # Grow existing balances and inflate outstanding targets.
        for s in state.values():
            if s["done_month"] is None:
                s["balance"] *= (1 + r)
                if s["goal"].inflate_target:
                    s["target_nominal"] *= (1 + infl)

        available = monthly_surplus
        month_alloc: dict[str, float] = {}

        def give(s: dict, amount: float) -> float:
            amount = min(amount, s["target_nominal"] - s["balance"])
            if amount <= 0:
                return 0.0
            s["balance"] += amount
            s["allocated"] += amount
            month_alloc[s["goal"].name] = month_alloc.get(s["goal"].name, 0.0) + amount
            return amount

        open_goals = [s for s in state.values() if s["done_month"] is None]

        # 1. floors
        for s in open_goals:
            if available <= 0:
                break
            available -= give(s, min(s["goal"].monthly_min, available))

        # 2. schedule-keeping amounts for dated goals
        for s in open_goals:
            if available <= 0:
                break
            g = s["goal"]
            if g.target_date and current > g.target_date:
                # Deadline already missed: chase the remaining gap flat out.
                months_left = 1
            elif g.target_date:
                months_left = max(_months_between(current, g.target_date), 1)
            else:
                months_left = UNDATED_HORIZON_MONTHS
            gap = s["target_nominal"] - s["balance"]
            need = gap / months_left if r == 0 else (
                gap / (((1 + r) ** months_left - 1) / r) if months_left > 1 else gap
            )
            already = month_alloc.get(g.name, 0.0)
            available -= give(s, min(max(need - already, 0.0), available))

        # 3. spill the remainder onto the highest-priority unfinished goal
        for s in open_goals:
            if available <= 0:
                break
            available -= give(s, available)

        for s in state.values():
            if s["done_month"] is None:
                if month_alloc.get(s["goal"].name):
                    s["months_funded"] += 1
                if s["balance"] >= s["target_nominal"] - 0.01:
                    s["done_month"] = m

        if m < 120:  # only keep a decade of detail; enough for any chart
            timeline.append({
                "month": current.isoformat()[:7],
                "allocations": {k: round(v, 2) for k, v in month_alloc.items()},
                "balances": {k: round(v["balance"], 2) for k, v in state.items()},
                "unallocated": round(available, 2),
            })

        if all(s["done_month"] is not None for s in state.values()):
            break

    # ---- results ----
    projections: list[GoalProjection] = []
    unfunded: list[str] = []

    for g in goals:
        s = state[g.name]
        done = s["done_month"]
        projected_date = _add_months(start, done) if done is not None else None
        req = required_monthly(g, start, r, infl)

        slip = None
        on_track = True
        shortfall = 0.0
        note = ""

        if g.target_date:
            if projected_date is None:
                on_track = False
                slip = None
                note = "Not reachable within the projection horizon at this surplus."
            else:
                slip = _months_between(g.target_date, projected_date)
                on_track = slip <= 0
            # What the balance looks like on the target date itself.
            n = max(_months_between(start, g.target_date), 0)
            if n and (projected_date is None or projected_date > g.target_date):
                target_then = g.target_amount * ((1 + infl) ** n if g.inflate_target else 1)
                bal = 0.0
                for row in timeline[:n]:
                    bal = row["balances"].get(g.name, bal)
                shortfall = max(round(target_then - bal, 2), 0.0)
        else:
            on_track = projected_date is not None
            if projected_date is None:
                note = "No target date, and not funded within the horizon."

        if s["allocated"] < 0.01:
            unfunded.append(g.name)
            if not note:
                note = "Received nothing -- higher-priority goals consumed the whole surplus."

        projections.append(GoalProjection(
            name=g.name,
            target_amount=round(s["target_nominal"], 2),
            target_date=g.target_date,
            projected_date=projected_date,
            months_to_fund=done + 1 if done is not None else None,
            total_allocated=round(s["allocated"], 2),
            avg_monthly_allocation=round(s["allocated"] / s["months_funded"], 2) if s["months_funded"] else 0.0,
            on_track=on_track,
            slip_months=slip,
            shortfall_at_target_date=shortfall,
            required_monthly=round(req, 2) if req is not None else None,
            note=note,
        ))

    committed = sum(p.required_monthly or 0 for p in projections)
    if committed > monthly_surplus > 0:
        warnings.append(
            f"Your dated goals need about ${committed:,.0f}/month combined but the surplus is "
            f"${monthly_surplus:,.0f}. Something has to give -- the slips below show what."
        )

    return Projection(
        monthly_surplus=round(monthly_surplus, 2),
        goals=projections,
        timeline=timeline,
        unfunded=unfunded,
        total_committed_monthly=round(committed, 2),
        warnings=warnings,
    )


def compare(
    goals: list[Goal],
    monthly_surplus: float,
    without: Optional[str] = None,
    surplus_delta: float = 0.0,
    **kwargs,
) -> dict:
    """Answer 'what does funding X actually cost my other goals?'

    Runs the baseline and a variant (a goal removed, or the surplus changed)
    and reports the movement in every other goal's completion date.
    """
    base = project(goals, monthly_surplus, **kwargs)
    variant_goals = [g for g in goals if g.name != without] if without else list(goals)
    variant = project(variant_goals, monthly_surplus + surplus_delta, **kwargs)

    base_by = {p.name: p for p in base.goals}
    deltas = []
    for p in variant.goals:
        b = base_by.get(p.name)
        if not b:
            continue
        if b.months_to_fund and p.months_to_fund:
            shift = p.months_to_fund - b.months_to_fund
        else:
            shift = None
        deltas.append({
            "goal": p.name,
            "baseline_date": b.projected_date.isoformat() if b.projected_date else None,
            "variant_date": p.projected_date.isoformat() if p.projected_date else None,
            "months_earlier": -shift if shift is not None else None,
        })
    return {"baseline": base, "variant": variant, "deltas": deltas}
