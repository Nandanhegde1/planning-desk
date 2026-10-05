"""The feasibility policy, tested without an LLM, an API key, or a network."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.rules import assess_window, rank_alternatives  # noqa: E402


def hours(count=3, day=22, **overrides):
    base = {
        "temp": 26.0,
        "apparent_temp": 27.0,
        "precip_probability": 5,
        "precipitation": 0.0,
        "wind_gusts": 12.0,
        "pm2_5": 25.0,
        "uv_index": 4.0,
    }
    base.update(overrides)
    return [{**base, "time": f"2026-08-{day:02d}T{17 + i:02d}:00"} for i in range(count)]


def test_clear_conditions_pass():
    result = assess_window(hours(), activity="outdoor")
    assert result.verdict == "go"
    assert result.score == 100.0


def test_heavy_rain_blocks_outdoor_play():
    result = assess_window(hours(precip_probability=85, precipitation=6.0), activity="outdoor")
    assert result.verdict == "no_go"
    assert any(
        r.code == "precipitation_mm_per_h" and r.severity == "blocker" for r in result.reasons
    )


def test_same_rain_only_cautions_indoors():
    """Indoor play is a commute problem, not a weather problem."""
    result = assess_window(hours(precip_probability=85, precipitation=6.0), activity="indoor")
    assert result.verdict == "caution"


def test_an_ordinary_breeze_is_not_a_caution():
    """Gusts of 35 km/h are a fresh breeze. Flagging them made every window in
    monsoon Bengaluru a caution, which is the same as saying nothing."""
    assert assess_window(hours(wind_gusts=35), activity="outdoor").verdict == "go"


def test_bad_air_blocks_outdoor_exertion():
    result = assess_window(hours(pm2_5=140), activity="outdoor")
    assert result.verdict == "no_go"
    assert any(r.code == "pm2_5_ug_m3" for r in result.reasons)


def test_peak_not_average_decides():
    """One ruinous hour inside an otherwise fine window still blocks it."""
    window = hours(3)
    window[1]["wind_gusts"] = 70.0
    result = assess_window(window, activity="outdoor")
    assert result.verdict == "no_go"


def test_sunset_blocks_an_unlit_ground():
    result = assess_window(
        hours(),
        activity="outdoor",
        daylight={"sunrise": "2026-08-22T06:05", "sunset": "2026-08-22T18:30"},
        needs_daylight=True,
    )
    assert result.verdict == "no_go"
    assert any(r.code == "after_sunset" for r in result.reasons)


def test_sunset_is_irrelevant_when_the_ground_is_lit():
    result = assess_window(
        hours(),
        activity="outdoor",
        daylight={"sunrise": "2026-08-22T06:05", "sunset": "2026-08-22T18:30"},
        needs_daylight=False,
    )
    assert result.verdict == "go"


def test_climatology_never_produces_a_blocker():
    """Seasonal normals describe a distribution, so they cannot rule a day out."""
    result = assess_window(
        hours(precip_probability=95, precipitation=9.0),
        activity="outdoor",
        data_source="climatology",
    )
    assert result.verdict == "caution"
    assert any(r.code == "beyond_forecast_range" for r in result.reasons)


def test_the_forecast_fallback_is_treated_as_climatology():
    """When the forecast host cannot be reached the window is assessed from
    normals instead. That backup must inherit the same restraint: no blocker, and
    a note saying where the numbers came from. It used to be an exact string
    comparison, so a fallback source would have kept the blocker."""
    result = assess_window(
        hours(precip_probability=95, precipitation=9.0),
        activity="outdoor",
        data_source="climatology_fallback",
    )

    assert result.verdict == "caution"
    note = next(r for r in result.reasons if r.code == "beyond_forecast_range")
    assert "could not be reached" in note.detail, "the reason must say the forecast was down"


def test_the_two_climatology_sources_explain_themselves_differently():
    """One is a date past the horizon, the other is an outage. Both use normals,
    but a user should be told which."""
    horizon = assess_window(hours(), activity="outdoor", data_source="climatology")
    outage = assess_window(hours(), activity="outdoor", data_source="climatology_fallback")

    horizon_note = next(r for r in horizon.reasons if r.code == "beyond_forecast_range").detail
    outage_note = next(r for r in outage.reasons if r.code == "beyond_forecast_range").detail

    assert "16-day forecast horizon" in horizon_note
    assert "could not be reached" in outage_note
    assert horizon_note != outage_note


def test_a_clean_climatology_window_is_still_a_go():
    """The softening applies to blockers, not to the verdict as a whole."""
    assert assess_window(hours(), activity="outdoor", data_source="climatology").verdict == "go"


def test_empty_input_is_caution_not_go():
    assert assess_window([], activity="outdoor").verdict == "caution"


def test_alternatives_rank_the_calmest_window_first():
    """Fixture corrected, assertion unchanged. All three candidates previously
    carried the same timestamps, so they were three descriptions of one window
    and overlap suppression correctly collapsed them to one. They are now three
    separate days, which is what the test always meant to compare.

    All weekdays, so the weekend nudge cannot decide the order instead of the
    weather.
    """
    candidates = [
        {"label": "wet", "hours": hours(day=17, precip_probability=90, precipitation=5.0)},
        {"label": "fine", "hours": hours(day=18)},
        {"label": "breezy", "hours": hours(day=19, wind_gusts=48)},
    ]
    ranked = rank_alternatives(candidates, activity="outdoor", limit=3)

    assert len(ranked) == 3
    assert ranked[0]["label"] == "fine"
    assert ranked[-1]["label"] == "wet"
    assert ranked[0]["score"] >= ranked[1]["score"] >= ranked[2]["score"]


def test_a_missing_pm25_is_reported_not_passed():
    """Past the air-quality horizon PM2.5 is absent, and a skipped reading used to
    leave "Every reading in that window sits inside the comfortable band" on a
    Delhi plan whose air nobody had measured."""
    window = [{k: v for k, v in h.items() if k != "pm2_5"} for h in hours()]
    result = assess_window(window, activity="outdoor")

    assert result.verdict == "go", "a missing reading is not a caution"
    assert any(r.code == "pm2_5_not_assessed" for r in result.reasons)
    clear = next(r for r in result.reasons if r.code == "clear")
    assert "available" in clear.detail


def test_normals_do_not_carry_the_pm25_note():
    """Climatology never has PM2.5, and its own note already says it is not a
    forecast. Adding this one to every climatology day would be noise."""
    window = [{k: v for k, v in h.items() if k != "pm2_5"} for h in hours()]
    result = assess_window(window, activity="outdoor", data_source="climatology")

    assert not any(r.code == "pm2_5_not_assessed" for r in result.reasons)
