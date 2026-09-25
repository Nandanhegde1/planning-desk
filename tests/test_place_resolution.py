"""Place resolution, which is where the first version got stuck.

Open-Meteo's geocoder indexes populated places only, so a venue name returned
nothing and the assistant reported that a real park did not exist. Two fixes are
covered here: a second geocoder that knows venues, and a cascade to the enclosing
place when neither index has an entry.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "servers"))

from core import weather

GAZETTEER = {
    "bengaluru": {
        "name": "Bengaluru",
        "country": "India",
        "latitude": 12.97,
        "longitude": 77.59,
        "resolved_by": "gazetteer",
    }
}
OPENSTREETMAP = {
    "cubbon park, bengaluru": {
        "name": "Cubbon Park",
        "country": "India",
        "latitude": 12.976,
        "longitude": 77.593,
        "resolved_by": "openstreetmap",
    }
}


@pytest.fixture
def server(monkeypatch):
    import importlib.util

    path = Path(__file__).resolve().parent.parent / "servers" / "feasibility_server.py"
    spec = importlib.util.spec_from_file_location("feasibility_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    async def fake_geocode(place, count=5):
        key = place.lower().strip()
        if key in GAZETTEER:
            return [dict(GAZETTEER[key])]
        if key in OPENSTREETMAP:
            return [dict(OPENSTREETMAP[key])]
        return []

    monkeypatch.setattr(module.weather, "geocode", fake_geocode)
    return module


@pytest.mark.asyncio
async def test_a_venue_resolves_through_the_second_geocoder(server):
    result = await server._resolve("Cubbon Park, Bengaluru")
    assert result["name"] == "Cubbon Park"
    assert result["resolved_by"] == "openstreetmap"
    assert "note" not in result


@pytest.mark.asyncio
async def test_an_unknown_venue_falls_back_to_the_enclosing_city(server):
    """Weather is a city-scale quantity, so this is an answer, not a compromise."""
    result = await server._resolve("Kanteerava Stadium, Bengaluru")
    assert result["name"] == "Bengaluru"
    assert result["requested"] == "Kanteerava Stadium, Bengaluru"
    assert "Bengaluru" in result["note"]


@pytest.mark.asyncio
async def test_a_plain_city_resolves_without_a_note(server):
    result = await server._resolve("Bengaluru")
    assert result["name"] == "Bengaluru"
    assert "note" not in result


@pytest.mark.asyncio
async def test_nothing_resolvable_raises_with_actionable_advice(server):
    from core.http import FetchError

    with pytest.raises(FetchError, match="town or city"):
        await server._resolve("Somewhere That Does Not Exist")


@pytest.mark.asyncio
async def test_geocode_only_reaches_openstreetmap_when_the_gazetteer_is_empty(monkeypatch):
    calls = []

    async def fake_get_json(url, params=None, **kwargs):
        calls.append(url)
        if "open-meteo" in url:
            return {"results": [{"name": "Bengaluru", "latitude": 12.97, "longitude": 77.59}]}
        return []

    monkeypatch.setattr(weather, "get_json", fake_get_json)
    results = await weather.geocode("Bengaluru")

    assert results[0]["resolved_by"] == "gazetteer"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_openstreetmap_results_are_normalised_to_the_same_shape(monkeypatch):
    async def fake_get_json(url, params=None, **kwargs):
        if "open-meteo" in url:
            return {"results": []}
        return [
            {
                "name": "Cubbon Park",
                "lat": "12.9763",
                "lon": "77.5929",
                "addresstype": "park",
                "display_name": "Cubbon Park, Bengaluru, Karnataka, India",
                "address": {"state": "Karnataka", "country": "India", "country_code": "in"},
            }
        ]

    monkeypatch.setattr(weather, "get_json", fake_get_json)
    results = await weather.geocode("Cubbon Park")

    assert results[0]["name"] == "Cubbon Park"
    assert isinstance(results[0]["latitude"], float)
    assert results[0]["country_code"] == "IN"
    assert results[0]["kind"] == "park"


@pytest.mark.asyncio
async def test_a_forecast_outage_falls_back_to_normals(monkeypatch):
    """The forecast and the reanalysis archive are separate services on separate
    hosts, so an outage on one leaves the window still assessable from the other.
    Without this the whole check failed and suggest_better_windows returned an
    error, which is what was seen intermittently on the deployed app."""
    import importlib
    from datetime import date, datetime

    from core.http import FetchError

    feasibility = importlib.import_module("feasibility_server")

    async def dead_forecast(*args, **kwargs):
        raise FetchError("could not fetch https://api.open-meteo.com/v1/forecast: outage")

    async def normals(lat, lon, target):
        return {
            "rows": [
                {
                    "time": f"{target.isoformat()}T{hour:02d}:00",
                    "precip_probability": 10,
                    "precipitation": 0.0,
                    "wind_gusts": 12,
                    "apparent_temp": 24,
                }
                for hour in range(0, 24)
            ]
        }

    monkeypatch.setattr(feasibility.weather, "forecast_hours", dead_forecast)
    monkeypatch.setattr(feasibility.weather, "seasonal_normals", normals)
    monkeypatch.setattr(feasibility.weather, "within_forecast_horizon", lambda _: True)

    target = date(2026, 8, 22)
    rows, _daylight, source = await feasibility._readings(
        12.97, 77.59, target, datetime(2026, 8, 22, 17), datetime(2026, 8, 22, 20)
    )

    assert source == "climatology_fallback", "the outage must not fail the whole check"
    assert rows, "normals should have supplied readings"


@pytest.mark.asyncio
async def test_losing_air_quality_does_not_lose_the_verdict(monkeypatch):
    """PM2.5 is one reading among several. A failure there used to take the rain,
    wind and temperature assessment down with it."""
    import importlib
    from datetime import date, datetime

    from core.http import FetchError

    feasibility = importlib.import_module("feasibility_server")

    async def forecast(lat, lon, start, end):
        return {
            "rows": [
                {"time": f"2026-08-22T{h:02d}:00", "precip_probability": 5, "precipitation": 0.0}
                for h in range(24)
            ],
            "daylight": {
                "2026-08-22": {"sunrise": "2026-08-22T06:05", "sunset": "2026-08-22T18:37"}
            },
        }

    async def dead_air(*args, **kwargs):
        raise FetchError("air quality host unreachable")

    monkeypatch.setattr(feasibility.weather, "forecast_hours", forecast)
    monkeypatch.setattr(feasibility.weather, "air_quality_hours", dead_air)
    monkeypatch.setattr(feasibility.weather, "within_forecast_horizon", lambda _: True)

    rows, daylight, source = await feasibility._readings(
        12.97, 77.59, date(2026, 8, 22), datetime(2026, 8, 22, 17), datetime(2026, 8, 22, 20)
    )

    assert source == "forecast", "a missing pm2.5 is not a reason to abandon the forecast"
    assert rows
    assert daylight is not None
