"""Alternatives: what is offered, how far away, and how many times.

A football match at Cubbon Park came back no_go and the app offered Hampi,
Manali, Varanasi and Ooty. Manali is about 2000km from Bengaluru. Every number in
that answer was correct and the advice was useless.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "servers"))

from core import destinations  # noqa: E402
from core.rules import rank_alternatives  # noqa: E402

BENGALURU = (12.97, 77.59)


def window(day: int, hour: int, span: int = 3, rain: int = 5) -> dict:
    return {
        "label": f"{day:02d} {hour:02d}:00-{hour + span:02d}:00",
        "hours": [
            {
                "time": f"2026-08-{day:02d}T{h:02d}:00",
                "precip_probability": rain,
                "precipitation": 0.0,
                "apparent_temp": 24,
            }
            for h in range(hour, hour + span)
        ],
    }


def test_reach_grows_with_the_length_of_the_trip():
    """A night away buys roughly a day's travel each way."""
    assert destinations.reach_km(1) < destinations.reach_km(2)
    assert destinations.reach_km(2) < destinations.reach_km(4)
    assert destinations.reach_km(4) < destinations.reach_km(7)


def test_the_pool_is_drawn_by_proximity_when_an_origin_is_given():
    """It was drawn by price, so the pool was the cheapest entries nationwide and
    proximity entered at no stage."""
    near = destinations.shortlist(origin=BENGALURU, within_km=destinations.reach_km(4), limit=6)

    assert near, "there should be somewhere within reach"
    assert all(row["distance_km"] <= 1200 for row in near)
    assert [r["distance_km"] for r in near] == sorted(r["distance_km"] for r in near)


def test_a_short_trip_will_not_reach_as_far():
    def names(nights: int) -> set[str]:
        rows = destinations.shortlist(
            origin=BENGALURU, within_km=destinations.reach_km(nights), limit=20
        )
        return {row["name"] for row in rows}

    one, week = names(1), names(7)

    assert one < week, "a single night should reach strictly fewer places than a week"


def test_budget_browsing_without_an_origin_is_unchanged():
    """The no-origin path is what 'where can I go on 5000 a day' uses, and it
    should still come back cheapest first."""
    rows = destinations.shortlist(max_daily_inr=5000, limit=6)
    lows = [r["daily_budget_inr"][0] for r in rows]

    assert lows == sorted(lows)
    assert all("distance_km" not in r for r in rows)


@pytest.mark.asyncio
async def test_a_venue_activity_is_refused_rather_than_answered():
    """The catalogue is a holiday catalogue, so asked about a football pitch it
    could only ever answer with a holiday."""
    import importlib

    feasibility = importlib.import_module("feasibility_server")
    result = await feasibility.suggest_alternative_destinations(
        "Cubbon Park, Bengaluru", "2026-08-22", activity="outdoor"
    )

    assert result["applicable"] is False
    assert "another ground" in result["reason"]
    assert "alternatives" not in result


def test_overlapping_windows_are_collapsed():
    """The transcript offered Thursday 06:00-09:00, 07:00-10:00 and 08:00-11:00.
    One morning, shown three times."""
    same_morning = [window(20, 6), window(20, 7), window(20, 8)]
    best = rank_alternatives(same_morning + [window(19, 6)], activity="outdoor", limit=4)

    labels = [row["label"] for row in best]
    assert len(labels) == 2, f"one option per morning, got {labels}"


def test_a_weekend_is_nudged_ahead_of_an_equal_weekday():
    """6am Wednesday against a Saturday fixture is correct and useless."""
    thursday, saturday = window(20, 6), window(22, 6)
    best = rank_alternatives([thursday, saturday], activity="outdoor", limit=2)

    assert best[0]["label"].startswith("22")


def test_but_a_clearly_better_weekday_still_wins():
    """A nudge, deliberately, not a filter."""
    dry_thursday, wet_saturday = window(20, 6, rain=0), window(22, 6, rain=60)
    best = rank_alternatives([dry_thursday, wet_saturday], activity="outdoor", limit=2)

    assert best[0]["label"].startswith("20")


def test_the_ranking_no_longer_leaks_its_working():
    """The scratch keys used for overlap and weekend handling must not reach the
    tool response."""
    best = rank_alternatives([window(20, 6)], activity="outdoor", limit=1)

    assert not [k for k in best[0] if k.startswith("_")]
