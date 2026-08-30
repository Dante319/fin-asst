from datetime import date

import pytest

from finasst.goals import Goal, compare, project, required_monthly


def g(name, amount, target=None, saved=0.0, priority=100, monthly_min=0.0, inflate=False):
    goal = Goal(name=name, target_amount=amount, target_date=target,
                saved_so_far=saved, priority=priority, monthly_min=monthly_min)
    goal.inflate_target = inflate
    return goal


START = date(2026, 1, 1)


def test_required_monthly_with_no_return_is_simple_division():
    goal = g("Trip", 1200, date(2027, 1, 1))
    assert required_monthly(goal, START, 0.0, 0.0) == pytest.approx(100.0)


def test_existing_savings_reduce_the_required_contribution():
    goal = g("Trip", 1200, date(2027, 1, 1), saved=600)
    assert required_monthly(goal, START, 0.0, 0.0) == pytest.approx(50.0)


def test_a_single_goal_funded_exactly_lands_on_its_date():
    p = project([g("Trip", 1200, date(2027, 1, 1))], monthly_surplus=100,
                annual_return_rate=0, annual_inflation=0, start=START)
    assert p.goals[0].on_track
    assert p.goals[0].slip_months <= 0, "funded on schedule, so never late"
    assert p.goals[0].total_allocated == pytest.approx(1200, abs=1)


def test_goals_compete_so_the_second_one_slips():
    """Two goals that each need the whole surplus cannot both be on time."""
    goals = [g("First", 1200, date(2027, 1, 1), priority=10),
             g("Second", 1200, date(2027, 1, 1), priority=20)]
    p = project(goals, monthly_surplus=100, annual_return_rate=0,
                annual_inflation=0, start=START)
    first, second = p.goals
    assert first.on_track, "the higher-priority goal is protected"
    assert not second.on_track
    assert second.slip_months and second.slip_months > 0
    assert p.warnings, "an over-committed surplus must be called out"


def test_priority_order_decides_who_is_protected():
    goals = [g("Low", 1200, date(2027, 1, 1), priority=90),
             g("High", 1200, date(2027, 1, 1), priority=10)]
    p = project(goals, monthly_surplus=100, annual_return_rate=0,
                annual_inflation=0, start=START)
    by_name = {x.name: x for x in p.goals}
    assert by_name["High"].on_track and not by_name["Low"].on_track


def test_monthly_min_reserves_a_floor_for_a_lower_priority_goal():
    goals = [g("Greedy", 100000, date(2027, 1, 1), priority=10),
             g("Protected", 600, date(2027, 1, 1), priority=90, monthly_min=50)]
    p = project(goals, monthly_surplus=100, annual_return_rate=0,
                annual_inflation=0, start=START)
    protected = [x for x in p.goals if x.name == "Protected"][0]
    assert protected.total_allocated == pytest.approx(600, abs=1)


def test_inflation_raises_a_distant_target():
    """A 2036 target quoted in 2026 dollars must cost more by the time it lands.

    The surplus is set just above what the goal needs, so it runs close to its
    full term rather than finishing early and stopping the inflation clock.
    """
    goal = Goal(name="House", target_amount=100000, target_date=date(2036, 1, 1))
    goal.inflate_target = True
    p = project([goal], monthly_surplus=1150, annual_return_rate=0,
                annual_inflation=0.03, start=START)
    assert p.goals[0].target_amount > 130000
    assert p.goals[0].required_monthly > 100000 / 120, "inflation raises the monthly ask"


def test_zero_surplus_is_reported_not_hidden():
    p = project([g("Trip", 1200, date(2027, 1, 1))], monthly_surplus=0,
                annual_return_rate=0, annual_inflation=0, start=START)
    assert not p.goals[0].on_track
    assert p.warnings and "surplus" in p.warnings[0].lower()


def test_dropping_a_goal_pulls_the_others_forward():
    goals = [g("Trip", 2400, date(2027, 1, 1), priority=10),
             g("House", 24000, date(2030, 1, 1), priority=20)]
    result = compare(goals, monthly_surplus=1000, without="Trip",
                     annual_return_rate=0, annual_inflation=0, start=START)
    house = [d for d in result["deltas"] if d["goal"] == "House"][0]
    assert house["months_earlier"] > 0
