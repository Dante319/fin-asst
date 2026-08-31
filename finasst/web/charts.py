"""Chart geometry, computed in Python rather than in the template.

Templates used to divide by a peak value taken straight from the data, which
crashes the page the moment that peak is zero -- a new database, a month with no
income, a filter that matches nothing. Working the geometry out here means the
degenerate cases are handled once, in code that can be tested, and the templates
just place rectangles.

Everything returned is plain floats in SVG user units. No colours: the stylesheet
owns those, so light and dark need no second code path.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence

# The three flow series, in fixed palette-slot order. Colour follows the series,
# never its size in a given month.
FLOW_SERIES = [
    ("income", "Income", "s1"),
    ("spend", "Spending", "s2"),
    ("surplus", "Surplus", "s3"),
]


PAD_LEFT = 58.0
PAD_RIGHT = 14.0
PAD_TOP = 14.0
PAD_BOTTOM = 30.0
BAR_GAP = 2.0          # the surface gap between adjacent bars, per the mark spec
GROUP_GAP_RATIO = 0.28  # share of a month slot left empty between groups


def nice_ceiling(value: float) -> float:
    """Round a maximum up to a round number a person would choose for an axis."""
    if value <= 0:
        return 1.0
    from math import floor, log10

    magnitude = 10 ** floor(log10(value))
    for step in (1, 1.5, 2, 2.5, 3, 4, 5, 7.5, 10):
        if value <= step * magnitude:
            return step * magnitude
    return 10 * magnitude


def _ticks(lo: float, hi: float, count: int = 4) -> list[float]:
    """Gridline values spanning lo..hi, always including zero when it is inside."""
    if hi <= lo:
        return [lo, hi]
    step = (hi - lo) / count
    values = [lo + i * step for i in range(count + 1)]
    if lo < 0 < hi and not any(abs(v) < 1e-9 for v in values):
        values.append(0.0)
    return sorted(set(values))


@dataclass
class Bar:
    x: float
    y: float
    w: float
    h: float
    series: str        # css class suffix: s1 | s2 | s3
    key: str           # income | spend | surplus
    label: str         # "Income"
    month: str
    value: float
    negative: bool = False


@dataclass
class Gridline:
    y: float
    value: float
    label: str
    is_zero: bool = False


@dataclass
class MonthTick:
    x: float          # centre of the month group
    left: float       # left edge of the hover band
    width: float      # width of the hover band
    label: str        # "07"
    full: str         # "2026-07"
    partial: bool = False


@dataclass
class FlowChart:
    width: float
    height: float
    plot_top: float
    plot_bottom: float
    plot_left: float = PAD_LEFT
    plot_right: float = 0.0
    label_x: float = PAD_LEFT - 12
    bars: list[Bar] = field(default_factory=list)
    gridlines: list[Gridline] = field(default_factory=list)
    months: list[MonthTick] = field(default_factory=list)
    zero_y: float = 0.0
    empty: bool = True


def _money_tick(value: float) -> str:
    a = abs(value)
    if a >= 1000:
        text = f"${a / 1000:,.1f}k".replace(".0k", "k")
    else:
        text = f"${a:,.0f}"
    return f"-{text}" if value < 0 else text


def flow_chart(
    totals: Sequence[dict],
    width: float = 1060.0,
    height: float = 300.0,
    current_month: Optional[str] = None,
) -> FlowChart:
    """Grouped bars: income, spending and surplus for each month with data.

    `current_month` marks the month still in progress, which is charted but
    flagged -- a part-month bar next to full ones invites exactly the wrong
    conclusion if nothing says so.
    """
    chart = FlowChart(
        width=width, height=height,
        plot_top=PAD_TOP, plot_bottom=height - PAD_BOTTOM,
        plot_left=PAD_LEFT, plot_right=width - PAD_RIGHT,
        label_x=PAD_LEFT - 12,
    )
    if not totals:
        chart.zero_y = height - PAD_BOTTOM
        return chart

    values = [v for m in totals for v in (m["income"], m["spend"], m["surplus"])]
    hi = nice_ceiling(max(values + [0.0]))
    lo_raw = min(values + [0.0])
    lo = -nice_ceiling(-lo_raw) if lo_raw < 0 else 0.0

    plot_h = chart.plot_bottom - chart.plot_top
    plot_w = width - PAD_LEFT - PAD_RIGHT
    span = (hi - lo) or 1.0

    def y_of(value: float) -> float:
        return chart.plot_bottom - (value - lo) / span * plot_h

    chart.zero_y = y_of(0.0)
    chart.empty = all(abs(v) < 0.005 for v in values)

    for tick in _ticks(lo, hi):
        chart.gridlines.append(Gridline(
            y=round(y_of(tick), 2), value=tick,
            label=_money_tick(tick), is_zero=abs(tick) < 1e-9,
        ))

    slot = plot_w / len(totals)
    group_w = slot * (1 - GROUP_GAP_RATIO)
    bar_w = max((group_w - BAR_GAP * (len(FLOW_SERIES) - 1)) / len(FLOW_SERIES), 1.0)

    for index, month in enumerate(totals):
        slot_left = PAD_LEFT + index * slot
        group_left = slot_left + (slot - group_w) / 2
        is_partial = current_month is not None and month["month"] == current_month
        chart.months.append(MonthTick(
            x=round(slot_left + slot / 2, 2),
            left=round(slot_left, 2), width=round(slot, 2),
            label=month["month"][5:], full=month["month"], partial=is_partial,
        ))
        for position, (key, label, series) in enumerate(FLOW_SERIES):
            value = float(month[key])
            top = y_of(max(value, 0.0))
            bottom = y_of(min(value, 0.0))
            chart.bars.append(Bar(
                x=round(group_left + position * (bar_w + BAR_GAP), 2),
                y=round(top, 2),
                w=round(bar_w, 2),
                h=round(max(bottom - top, 0.0), 2),
                series=series, key=key, label=label,
                month=month["month"], value=value, negative=value < 0,
            ))
    return chart


@dataclass
class HBar:
    label: str
    group: str
    value: float
    share: float       # percent of the total, 0-100
    width: float       # percent of the widest bar, 0-100
    count: int = 0
    detail: str = ""


def hbars(
    rows: Iterable[dict],
    label_key: str = "category",
    value_key: str = "spend",
    group_key: Optional[str] = "group",
) -> list[HBar]:
    """Scale rows to the widest one, without dividing by zero when all are zero."""
    rows = list(rows)
    if not rows:
        return []
    peak = max((abs(float(r[value_key])) for r in rows), default=0.0)
    total = sum(abs(float(r[value_key])) for r in rows)
    out = []
    for r in rows:
        value = float(r[value_key])
        out.append(HBar(
            label=str(r[label_key]),
            group=str(r.get(group_key) or "") if group_key else "",
            value=value,
            share=round(100 * abs(value) / total, 1) if total else 0.0,
            width=round(100 * abs(value) / peak, 2) if peak else 0.0,
            count=int(r.get("count") or 0),
            detail=str(r.get("detail") or ""),
        ))
    return out


def sparkline(values: Sequence[float], width: float = 96.0, height: float = 26.0) -> str:
    """An SVG polyline `points` string. Flat line when every value is equal."""
    values = [float(v) for v in values]
    if len(values) < 2:
        return ""
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1.0
    step = width / (len(values) - 1)
    pad = 2.0
    usable = height - 2 * pad
    return " ".join(
        f"{i * step:.1f},{pad + (1 - (v - lo) / span) * usable:.1f}"
        for i, v in enumerate(values)
    )
