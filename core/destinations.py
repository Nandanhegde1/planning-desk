"""Destination shortlisting by budget, with seasons scored from reanalysis.

Budget bands in the seed catalogue are estimates, not sourced prices, and are
labelled as such in every response.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from core.http import FetchError, get_json, open_meteo

SEED_PATH = Path(__file__).resolve().parent / "destinations_seed.json"
CLIMATE_URL = open_meteo("https://archive-api.open-meteo.com/v1/archive")

# A ceiling on archive requests in flight from this process. Scoring a shortlist
# fans out to a request per destination per year, and without a ceiling that is
# dozens at once against a free API. Wide enough to collapse the wait, narrow
# enough to stay a well-behaved client. Sixteen was chosen against a container in
# Central India, where a single archive request is slower than from a desktop, so
# eight left the tool timing out on a cold cache.
_UPSTREAM = asyncio.Semaphore(int(os.environ.get("CLIMATE_CONCURRENCY", "16")))

BUDGET_BANDS = {
    "budget": (0, 3000),
    "mid": (3000, 7000),
    "premium": (7000, 15000),
    "luxury": (15000, 10**9),
}

_seed = json.loads(SEED_PATH.read_text())
CATALOGUE: list[dict[str, Any]] = _seed["destinations"]
ABOUT: dict[str, Any] = _seed["_about"]


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance. Here rather than in a server so shortlist can rank
    by it without importing one."""
    dlat, dlon = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    )
    return 2 * 6371.0 * math.asin(math.sqrt(a))


def reach_km(nights: int) -> float:
    """How far is worth travelling for a trip of this length.

    A night away buys roughly a day's travel each way, so a single night stays
    regional and a week opens the country. Without this, distance was computed per
    candidate and then discarded by a sort on comfort alone.
    """
    if nights <= 1:
        return 350.0
    if nights <= 2:
        return 500.0
    if nights <= 4:
        return 1200.0
    return 2500.0


def lookup(place: str) -> dict[str, Any] | None:
    """The catalogue entry a place name means, if it names one.

    The geocoder ranks by population, which is wrong for several of this
    catalogue's own destinations: "Manali" came back as a Chennai suburb in Tamil
    Nadu, "Goa" as Genoa and "Leh" as Le Havre. A destination this catalogue
    lists is taken from here instead.

    Matched on the base name, without the qualifier in "Leh, Ladakh" or "Coorg
    (Madikeri)". A qualifier in the request must agree with the entry, so "Manali,
    Tamil Nadu" is left to the geocoder rather than sent to Himachal.
    """
    head, _, rest = (part.strip() for part in place.lower().partition(","))
    for row in CATALOGUE:
        name = row["name"].lower()
        base = name.split("(")[0].split(",")[0].strip()
        own = name[len(base) :].strip(" ,()")
        if head not in (base, name):
            continue
        if rest and rest not in (own, row["region"].lower(), row["country"].lower()):
            continue
        return {
            "name": row["name"],
            "region": row["region"],
            "country": row["country"],
            "latitude": row["lat"],
            "longitude": row["lon"],
            "resolved_by": "catalogue",
        }
    return None


def _band_for(low: int, high: int) -> str:
    midpoint = (low + high) / 2
    for name, (floor, ceiling) in BUDGET_BANDS.items():
        if floor <= midpoint < ceiling:
            return name
    return "luxury"


async def month_climate(
    lat: float, lon: float, month: int, *, years_back: int = 5
) -> dict[str, Any]:
    """Mean conditions for one calendar month at one point, from reanalysis."""
    this_year = date.today().year

    async def one_year(offset: int) -> dict[str, Any] | None:
        year = this_year - offset
        start = date(year, month, 1)
        end = date(year + (month == 12), (month % 12) + 1, 1)
        async with _UPSTREAM:
            try:
                return await get_json(
                    CLIMATE_URL,
                    {
                        "latitude": lat,
                        "longitude": lon,
                        "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum",
                        "timezone": "auto",
                        "start_date": start.isoformat(),
                        "end_date": (end - timedelta(days=1)).isoformat(),
                    },
                    ttl_seconds=86400 * 60,
                )
            except FetchError:
                return None

    # One request per year, fetched together. In series this was five round trips
    # per destination, and a shortlist of a dozen destinations made sixty: quick
    # against a warm cache, minutes against a cold one in a fresh container.
    payloads = await asyncio.gather(*(one_year(offset) for offset in range(1, years_back + 1)))

    temps: list[float] = []
    rain_days = 0
    total_days = 0
    for payload in payloads:
        if payload is None:
            continue
        daily = payload.get("daily", {})
        highs = daily.get("temperature_2m_max") or []
        rain = daily.get("precipitation_sum") or []
        temps.extend([t for t in highs if t is not None])
        rain_days += sum(1 for r in rain if r and r >= 2.5)
        total_days += len([r for r in rain if r is not None])

    if not temps:
        return {"available": False}

    mean_high = sum(temps) / len(temps)
    wet_share = rain_days / total_days if total_days else 0.0

    score = 100.0
    if mean_high > 32:
        score -= (mean_high - 32) * 6
    if mean_high < 12:
        score -= (12 - mean_high) * 4
    score -= wet_share * 90

    return {
        "available": True,
        "mean_daily_high_c": round(mean_high, 1),
        "wet_day_share": round(wet_share, 2),
        "comfort_score": round(max(0.0, min(100.0, score)), 1),
        "years_sampled": years_back,
        "source": "Open-Meteo historical reanalysis",
    }


def shortlist(
    *,
    max_daily_inr: int | None = None,
    min_daily_inr: int | None = None,
    band: str | None = None,
    tags: list[str] | None = None,
    country: str | None = None,
    limit: int = 8,
    origin: tuple[float, float] | None = None,
    within_km: float | None = None,
) -> list[dict[str, Any]]:
    """Filter the catalogue. Pure and offline, so it is unit-testable.

    With an origin the pool is drawn nearest first, and within_km drops anything
    beyond reach. Without one the ordering is cheapest first, which is what budget
    browsing wants. The default mattered: alternatives to a trip were drawn from
    the cheapest entries nationwide, so proximity never entered the selection and
    a Himalayan hill station could be offered against a southern city.
    """
    wanted = {t.lower() for t in (tags or [])}
    results = []
    for row in CATALOGUE:
        low, high = row["daily_budget_inr"]
        if max_daily_inr is not None and low > max_daily_inr:
            continue
        if min_daily_inr is not None and high < min_daily_inr:
            continue
        if band and _band_for(low, high) != band.lower():
            continue
        if country and country.lower() not in row["country"].lower():
            continue
        if wanted and not wanted & {t.lower() for t in row["tags"]}:
            continue
        results.append(
            {
                **row,
                "budget_band": _band_for(low, high),
                "budget_basis": ABOUT["budget_basis"],
                "budget_confidence": ABOUT["confidence"],
            }
        )
    if origin is not None:
        for row in results:
            row["distance_km"] = round(haversine_km(origin[0], origin[1], row["lat"], row["lon"]))
        if within_km is not None:
            results = [r for r in results if r["distance_km"] <= within_km]
        results.sort(key=lambda r: r["distance_km"])
    else:
        results.sort(key=lambda r: r["daily_budget_inr"][0])
    return results[:limit]
