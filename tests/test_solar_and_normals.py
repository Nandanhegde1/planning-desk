"""Daylight, gusts and the forecast horizon.

Three faults that each produced a confident wrong verdict rather than an error,
which is the worst shape a bug can take in an app whose claim is that readings
are checkable.
"""

import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "servers"))

from core import solar, weather  # noqa: E402
from core.rules import assess_window  # noqa: E402

IST = 19800  # +05:30, which longitude / 15 cannot express


def test_sunset_matches_what_the_forecast_api_reported():
    """The app displayed 06:08 and 18:37 for Bengaluru from the forecast payload.
    Computing it locally has to agree, or the fallback would quietly disagree with
    the live path."""
    got = solar.daylight(12.97, 77.59, date(2026, 8, 22), IST)

    assert got["sunrise"].endswith("06:08")
    assert got["sunset"].endswith("18:37")


@pytest.mark.parametrize(
    "lat,lon,day,offset,sunrise,sunset",
    [
        (51.51, -0.13, date(2026, 6, 21), 3600, "04:42", "21:21"),  # London, BST
        (-33.87, 151.21, date(2026, 6, 21), 36000, "06:59", "16:53"),  # Sydney, winter
    ],
)
def test_it_holds_up_away_from_the_demo_city(lat, lon, day, offset, sunrise, sunset):
    got = solar.daylight(lat, lon, day, offset)

    assert got["sunrise"].endswith(sunrise)
    assert got["sunset"].endswith(sunset)


def test_polar_day_returns_no_sunset_rather_than_inventing_one():
    """Inside the arctic circle in June the sun does not set. That is a real
    answer and the caller should say so, not receive a fabricated time."""
    assert solar.daylight(69.65, 18.96, date(2026, 6, 21), 7200) is None
    assert solar.daylight(69.65, 18.96, date(2026, 12, 21), 3600) is None


def test_the_half_hour_zone_is_respected():
    """India is +05:30. Falling back to longitude / 15 rounds to +05:00 and puts
    every Indian sunset half an hour early."""
    with_offset = solar.daylight(12.97, 77.59, date(2026, 8, 22), IST)
    guessed = solar.daylight(12.97, 77.59, date(2026, 8, 22), None)

    assert with_offset["sunset"] != guessed["sunset"]


def test_an_unlit_ground_after_dark_is_blocked_without_a_forecast():
    """The whole point. Daylight used to come from the forecast payload, so past
    the horizon it was None, and the floodlights rule is guarded on having a
    sunset. An unlit ground at 17:00 to 20:00 came back go, 100 out of 100."""
    day = date(2026, 12, 17)
    sun = solar.daylight(12.97, 77.59, day, IST)
    hours = [
        {
            "time": f"{day.isoformat()}T{h:02d}:00",
            "precip_probability": 5,
            "precipitation": 0.0,
            "apparent_temp": 24,
        }
        for h in range(17, 21)
    ]

    verdict = assess_window(
        hours,
        activity="outdoor",
        daylight=sun,
        needs_daylight=True,
        data_source="climatology",
    )

    assert verdict.verdict == "no_go"
    assert any(r.code == "after_sunset" and r.severity == "blocker" for r in verdict.reasons)


def test_sunset_still_blocks_even_though_climatology_softens_readings():
    """Climatology softens a measured blocker to caution, because a normal is a
    distribution. Sunset is not a distribution, so it keeps its blocker."""
    day = date(2026, 12, 17)
    hours = [
        {"time": f"{day.isoformat()}T{h:02d}:00", "precip_probability": 5}
        for h in range(17, 21)
    ]

    softened = assess_window(
        [{**h, "precip_probability": 99, "precipitation": 20.0} for h in hours],
        activity="outdoor",
        data_source="climatology",
    )
    assert softened.verdict == "caution", "a measured blocker is softened"

    blocked = assess_window(
        hours,
        activity="outdoor",
        daylight=solar.daylight(12.97, 77.59, day, IST),
        needs_daylight=True,
        data_source="climatology",
    )
    assert blocked.verdict == "no_go", "darkness is not softened"


def test_a_past_date_is_not_forecastable():
    """The check was one sided, so 2019 was inside the horizon. The date went to
    the forecast API, which rejected the range, and the tool failed instead of
    using the archive that does hold that day."""
    assert weather.within_forecast_horizon(date(2020, 1, 1)) is False
    assert weather.within_forecast_horizon(date.today() - timedelta(days=1)) is False
    assert weather.within_forecast_horizon(date.today()) is True
    assert weather.within_forecast_horizon(date.today() + timedelta(days=10)) is True
    assert weather.within_forecast_horizon(date.today() + timedelta(days=90)) is False


@pytest.mark.asyncio
async def test_climatology_reports_real_gusts_not_the_mean_wind(monkeypatch):
    """It reported the mean sustained wind as a gust, roughly a fifth of the real
    peak, so the 40 and 60 km/h gust lines could never be reached from
    climatology. The archive does return wind_gusts_10m."""
    asked = {}

    async def fake_get_json(url, params=None, **kwargs):
        asked["hourly"] = params.get("hourly", "")
        hours = [f"2024-12-17T{h:02d}:00" for h in range(24)]
        return {
            "utc_offset_seconds": IST,
            "hourly": {
                "time": hours,
                "temperature_2m": [24.0] * 24,
                "apparent_temperature": [24.0] * 24,
                "precipitation": [0.0] * 24,
                "wind_speed_10m": [10.0] * 24,
                "wind_gusts_10m": [48.0] * 24,
            },
        }

    monkeypatch.setattr(weather, "get_json", fake_get_json)
    bundle = await weather.seasonal_normals(12.97, 77.59, date(2026, 12, 17))

    assert "wind_gusts_10m" in asked["hourly"], "the archive must be asked for gusts"
    row = bundle["rows"][0]
    assert row["wind_gusts"] == 48.0
    assert row["wind_gusts"] != row["wind_speed"], "a gust is not the mean wind"
    assert bundle["utc_offset_seconds"] == IST


@pytest.mark.asyncio
async def test_a_missing_gust_is_omitted_rather_than_faked(monkeypatch):
    """Where the archive genuinely returns no gust the key is dropped, so the
    rules skip it. A missing reading is honest, a fabricated one reads as
    reassurance."""

    async def fake_get_json(url, params=None, **kwargs):
        hours = [f"2024-12-17T{h:02d}:00" for h in range(24)]
        return {
            "utc_offset_seconds": IST,
            "hourly": {
                "time": hours,
                "temperature_2m": [24.0] * 24,
                "precipitation": [0.0] * 24,
                "wind_speed_10m": [10.0] * 24,
            },
        }

    monkeypatch.setattr(weather, "get_json", fake_get_json)
    bundle = await weather.seasonal_normals(12.97, 77.59, date(2026, 12, 17))

    assert bundle["rows"], "rows should still be produced"
    assert "wind_gusts" not in bundle["rows"][0]


@pytest.mark.asyncio
async def test_readings_always_carry_daylight(monkeypatch):
    """Both paths, forecast and normals, must return a sunset. The floodlights
    rule is silently skipped without one."""
    import importlib

    feasibility = importlib.import_module("feasibility_server")

    async def normals(lat, lon, target):
        return {"rows": [], "utc_offset_seconds": IST}

    monkeypatch.setattr(feasibility.weather, "seasonal_normals", normals)
    day = date(2026, 12, 17)
    _rows, sun, _source = await feasibility._from_normals(
        12.97,
        77.59,
        day,
        datetime.combine(day, datetime.min.time()).replace(hour=17),
        datetime.combine(day, datetime.min.time()).replace(hour=20),
        "climatology",
    )

    assert sun is not None and sun["sunset"]
