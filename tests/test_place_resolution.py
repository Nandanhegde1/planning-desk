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


def _place(name, admin1, country, lat, lon, population):
    return {
        "name": name,
        "admin1": admin1,
        "country": country,
        "latitude": lat,
        "longitude": lon,
        "population": population,
        "resolved_by": "gazetteer",
    }


# Ordered as the live geocoder returned them on 2026-10-05, by population.
MANALI_TN = _place("Manali", "Tamil Nadu", "India", 13.16667, 80.26667, 35248)
MANALI_HP = _place("Manali", "Himachal Pradesh", "India", 32.2574, 77.17481, 8096)
SHARED_NAMES = {
    "manali": [MANALI_TN, MANALI_HP],
    "manali, tamil nadu": [MANALI_TN],
    "goa": [_place("Genoa", "Liguria", "Italy", 44.40478, 8.94439, 580097)],
    "kochi": [
        _place("Kochi", "Kochi", "Japan", 33.55, 133.53, 332059),
        _place("Kochi", "Kerala", "India", 9.93988, 76.26022, 633553),
    ],
    "aurangabad": [
        _place("Aurangabad", "Maharashtra", "India", 19.87757, 75.34226, 1175116),
        _place("Aurangabad", "Bihar", "India", 24.75204, 84.3742, 102244),
    ],
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
        if key in SHARED_NAMES:
            return [dict(row) for row in SHARED_NAMES[key]]
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
async def test_a_catalogue_destination_beats_the_geocoders_first_guess(server):
    """The geocoder ranks by population, and its first Manali is a Chennai suburb.
    A trip there was assessed as too hot and offered Pondicherry instead."""
    result = await server._resolve("Manali")
    assert result["resolved_by"] == "catalogue"
    assert result["region"] == "Himachal"
    assert result["latitude"] > 30, "Himachal, not Tamil Nadu at 13N"


@pytest.mark.asyncio
async def test_goa_is_not_genoa(server):
    result = await server._resolve("Goa")
    assert result["country"] == "India"
    assert result["resolved_by"] == "catalogue"


@pytest.mark.asyncio
async def test_a_named_state_is_respected_over_the_catalogue(server):
    """The catalogue is a default, not an override. Asked for the Tamil Nadu one,
    that is the one assessed."""
    result = await server._resolve("Manali, Tamil Nadu")
    assert result["admin1"] == "Tamil Nadu"
    assert result["resolved_by"] == "gazetteer"


def test_a_catalogue_qualifier_must_agree_with_the_entry():
    from core import destinations

    assert destinations.lookup("Leh, Ladakh")["name"] == "Leh, Ladakh"
    assert destinations.lookup("Leh")["name"] == "Leh, Ladakh"
    assert destinations.lookup("Coorg")["name"] == "Coorg (Madikeri)"
    assert destinations.lookup("Goa, India")["name"] == "Goa"
    assert destinations.lookup("Manali, Tamil Nadu") is None
    assert destinations.lookup("Cubbon Park, Bengaluru") is None


@pytest.mark.asyncio
async def test_a_large_namesake_is_flagged_without_blocking_the_answer(server):
    """Kochi in Japan is the geocoder's first match, and Kochi in Kerala is the
    larger of the two. The first is still used, so a verdict comes back, but the
    other is attached so the model can ask which was meant."""
    result = await server._resolve("Kochi")
    assert result["country"] == "Japan"
    assert result["other_matches"] == [{"name": "Kochi", "admin1": "Kerala", "country": "India"}]


@pytest.mark.asyncio
async def test_a_small_namesake_is_not_worth_a_question(server):
    """The Aurangabad in Bihar is 9% the size of the one in Maharashtra. Flagging
    every namesake would turn most Indian cities into a question."""
    result = await server._resolve("Aurangabad")
    assert result["admin1"] == "Maharashtra"
    assert "other_matches" not in result


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


def test_the_prompt_says_what_to_do_with_other_matches():
    """The flag is only useful if the model is told to use it."""
    from app import agent

    assert "other_matches" in agent.SYSTEM_PROMPT
