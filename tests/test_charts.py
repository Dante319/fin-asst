"""Chart geometry, and specifically the degenerate cases that used to crash.

The old templates divided by a peak taken straight from the data. Every test
here is a shape of data that made that peak zero or the list empty.
"""
from __future__ import annotations

from finasst.web import charts


def _month(ym, income=0.0, spend=0.0, surplus=0.0):
    return {"month": ym, "income": income, "spend": spend, "surplus": surplus, "saved": 0.0}


# --------------------------------------------------------------- flow chart --

def test_no_months_does_not_crash():
    c = charts.flow_chart([])
    assert c.bars == [] and c.months == []
    assert c.empty is True


def test_all_zero_month_does_not_divide_by_zero():
    c = charts.flow_chart([_month("2026-07")])
    assert c.empty is True
    assert len(c.bars) == 3
    assert all(b.h == 0 for b in c.bars)
    # A zero-height bar must still sit ON the baseline, not float.
    assert all(abs(b.y - c.zero_y) < 0.01 for b in c.bars)


def test_zero_income_but_real_spending_still_scales():
    c = charts.flow_chart([_month("2026-07", income=0, spend=1200, surplus=-1200)])
    assert c.empty is False
    spend = next(b for b in c.bars if b.key == "spend")
    assert spend.h > 0


def test_negative_surplus_hangs_below_the_zero_line():
    c = charts.flow_chart([_month("2026-07", income=1000, spend=1800, surplus=-800)])
    surplus = next(b for b in c.bars if b.key == "surplus")
    assert surplus.negative is True
    assert surplus.y >= c.zero_y - 0.01     # starts at the zero line
    assert surplus.h > 0                    # and extends downwards


def test_bars_never_overlap_within_a_month():
    c = charts.flow_chart([_month("2026-06", 8000, 5000, 3000), _month("2026-07", 8000, 5000, 3000)])
    for month in ("2026-06", "2026-07"):
        bars = sorted((b for b in c.bars if b.month == month), key=lambda b: b.x)
        for left, right in zip(bars, bars[1:]):
            assert left.x + left.w <= right.x + 0.01


def test_gridlines_include_zero_when_the_range_crosses_it():
    c = charts.flow_chart([_month("2026-07", 1000, 2000, -1000)])
    assert any(g.is_zero for g in c.gridlines)


def test_partial_month_is_flagged():
    c = charts.flow_chart([_month("2026-07", 100, 50, 50)], current_month="2026-07")
    assert c.months[0].partial is True


def test_nice_ceiling_rounds_up_to_readable_numbers():
    assert charts.nice_ceiling(0) == 1.0
    assert charts.nice_ceiling(-5) == 1.0
    assert charts.nice_ceiling(8300) == 10000
    assert charts.nice_ceiling(1200) == 1500


# ------------------------------------------------------------------- hbars --

def test_hbars_on_empty_input():
    assert charts.hbars([]) == []


def test_hbars_all_zero_values_do_not_divide_by_zero():
    rows = [{"category": "Groceries", "spend": 0.0, "count": 0, "group": "Food & drink"}]
    bars = charts.hbars(rows)
    assert bars[0].width == 0.0
    assert bars[0].share == 0.0


def test_hbars_scale_to_the_widest_row():
    rows = [
        {"category": "Housing", "spend": 2000.0, "count": 1, "group": "Essentials"},
        {"category": "Groceries", "spend": 500.0, "count": 4, "group": "Food & drink"},
    ]
    bars = charts.hbars(rows)
    assert bars[0].width == 100.0
    assert bars[1].width == 25.0
    assert bars[0].share == 80.0
    assert bars[1].group == "Food & drink"


# --------------------------------------------------------------- sparkline --

def test_sparkline_flat_series_does_not_divide_by_zero():
    points = charts.sparkline([5, 5, 5])
    assert points.count(",") == 3


def test_sparkline_needs_two_points():
    assert charts.sparkline([1]) == ""
