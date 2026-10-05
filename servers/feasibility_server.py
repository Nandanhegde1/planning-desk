"""MCP server: activity and travel feasibility, and alternatives.

Tools return structured readings plus a verdict from core.rules, never prose.
"""

from __future__ import annotations

import asyncio
import math
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Read .env directly so this server also runs standalone, not only via the host.
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from mcp.server import MCPServer

from core import destinations, solar, weather
from core.http import FetchError
from core.rules import THRESHOLDS, assess_window, rank_alternatives

server = MCPServer("feasibility", version="1.0.0")


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    dlat, dlon = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    )
    return 2 * 6371.0 * math.asin(math.sqrt(a))


# How large another place of the same name must be, as a share of the one chosen,
# before it is worth naming. Manali in Himachal is 23% of the Manali in Tamil
# Nadu; the Aurangabad in Bihar is 9% of the one in Maharashtra. Both from the
# live geocoder on 2026-10-05.
RIVAL_POPULATION_SHARE = 0.2


def _other_matches(matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Places sharing the chosen one's name that are big enough to be meant.

    The geocoder ranks by population, so the first match is a guess. A flag, not
    a stop: the verdict is still given, and the model can say which place it
    assumed. A village of the same name, with no population on record, is not a
    rival worth a question.
    """
    chosen = matches[0]
    floor = (chosen.get("population") or 0) * RIVAL_POPULATION_SHARE
    if not floor:
        return []
    return [
        {"name": m.get("name"), "admin1": m.get("admin1"), "country": m.get("country")}
        for m in matches[1:]
        if (m.get("population") or 0) >= floor
        and (m.get("admin1"), m.get("country")) != (chosen.get("admin1"), chosen.get("country"))
    ]


async def _resolve(place: str) -> dict[str, Any]:
    """Resolve a place, retrying against whatever encloses it.

    Weather is city-scale, so an unindexed venue falls back to its city rather
    than failing. The response records which name was used. A destination in the
    catalogue is taken from there before the geocoder is asked.
    """
    attempts = [place]
    parts = [p.strip() for p in place.split(",") if p.strip()]
    attempts += [", ".join(parts[i:]) for i in range(1, len(parts))]

    for index, attempt in enumerate(attempts):
        known = destinations.lookup(attempt)
        matches = [known] if known else await weather.geocode(attempt, count=5)
        if matches:
            resolved = dict(matches[0])
            resolved["requested"] = place
            # Only for a bare name. "Manali, Himachal Pradesh" already says which.
            rivals = _other_matches(matches) if len(parts) == 1 else []
            if rivals:
                resolved["other_matches"] = rivals
            if index > 0:
                resolved["note"] = (
                    f"{place!r} was not in either place index, so conditions are for "
                    f"{attempt!r}, which encloses it. Weather at this scale is the same."
                )
            return resolved

    raise FetchError(
        f"neither place index has an entry for {place!r}. Ask the user for the town or "
        "city it is in; weather is a city-scale quantity so that is enough."
    )


async def _readings(
    lat: float, lon: float, target: date, start: datetime, end: datetime
) -> tuple[list[dict[str, Any]], dict[str, str] | None, str]:
    """Hourly readings for a window, from forecast or climatology as appropriate.

    The forecast has a backup. Forecast and reanalysis are separate services on
    separate hosts, so when the forecast host cannot be reached the window is still
    assessable from seasonal normals. That is a weaker answer, not a wrong one: it
    is reported as climatology, which caps the verdict below a blocker and carries
    a note saying where the numbers came from.
    """
    if weather.within_forecast_horizon(target):
        # Fetched together: the two endpoints are independent, and a round trip
        # from the container is slow enough that doing them in turn was visible.
        # return_exceptions so a failing air quality call does not cancel the
        # forecast alongside it.
        bundle, pm25 = await asyncio.gather(
            weather.forecast_hours(lat, lon, target, target),
            weather.air_quality_hours(lat, lon, target, target),
            return_exceptions=True,
        )

        if isinstance(bundle, BaseException):
            print(f"forecast host unreachable ({bundle}); falling back to normals", file=sys.stderr)
            return await _from_normals(lat, lon, target, start, end, "climatology_fallback")

        rows = weather.slice_window(bundle["rows"], start, end)
        if isinstance(pm25, BaseException):
            # Air quality is one reading among several. Losing it should not lose
            # the rain, wind and temperature verdict with it.
            print(f"air quality unavailable ({pm25}); continuing without pm2.5", file=sys.stderr)
        else:
            rows = weather.merge_air_quality(rows, pm25)
        sun = bundle["daylight"].get(target.isoformat()) or solar.daylight(
            lat, lon, target, bundle.get("utc_offset_seconds")
        )
        return rows, sun, "forecast"

    return await _from_normals(lat, lon, target, start, end, "climatology")


async def _from_normals(
    lat: float, lon: float, target: date, start: datetime, end: datetime, source: str
) -> tuple[list[dict[str, Any]], dict[str, str] | None, str]:
    bundle = await weather.seasonal_normals(lat, lon, target)
    rows = [
        r
        for r in bundle["rows"]
        if start.hour <= datetime.fromisoformat(r["time"]).hour <= end.hour
    ]
    # Daylight is computed rather than taken from the payload. Returning None here
    # silently disabled the floodlights check, because the rule is guarded on
    # having a sunset, so an unlit ground at 17:00 came back as a go.
    return rows, solar.daylight(lat, lon, target, bundle.get("utc_offset_seconds")), source


@server.tool(
    description=(
        "Check whether a specific outdoor or indoor activity is workable at a place on a date "
        "and time window. Returns hourly readings and a verdict of go, caution or no_go computed "
        "by a fixed threshold policy. Use for sports, matches, and any plan tied to a venue."
    )
)
async def check_activity_window(
    place: str,
    date_iso: str,
    start_hour: int = 17,
    end_hour: int = 20,
    activity: str = "outdoor",
    needs_daylight: bool = False,
) -> dict[str, Any]:
    """activity is outdoor, indoor or travel. Hours are local, 0-23."""
    try:
        target = date.fromisoformat(date_iso)
    except ValueError:
        return {"error": f"date_iso must look like 2026-08-22, got {date_iso!r}"}
    if not 0 <= start_hour <= 23 or not 0 <= end_hour <= 23 or end_hour < start_hour:
        return {"error": "start_hour and end_hour must be 0-23 with end after start"}

    try:
        location = await _resolve(place)
        start = datetime.combine(target, datetime.min.time()).replace(hour=start_hour)
        end = datetime.combine(target, datetime.min.time()).replace(hour=end_hour)
        rows, daylight, source = await _readings(
            location["latitude"], location["longitude"], target, start, end
        )
    except FetchError as exc:
        return {"error": str(exc), "advice": "Report the outage; do not estimate the weather."}

    assessment = assess_window(
        rows,
        activity=activity,
        daylight=daylight,
        needs_daylight=needs_daylight,
        data_source=source,
    )
    return {
        "location": location,
        "requested": {
            "date": date_iso,
            "start_hour": start_hour,
            "end_hour": end_hour,
            "activity": activity,
            "needs_daylight": needs_daylight,
        },
        "assessment": assessment.as_dict(),
        "hourly": rows,
        "daylight": daylight,
        "data_source": source,
    }


@server.tool(
    description=(
        "Find better time slots near a plan that scored badly. Scans other hours on the same day "
        "and the same slot on nearby days, and returns the highest scoring windows. Call this "
        "after check_activity_window returns caution or no_go."
    )
)
async def suggest_better_windows(
    place: str,
    date_iso: str,
    start_hour: int = 17,
    duration_hours: int = 3,
    activity: str = "outdoor",
    needs_daylight: bool = False,
    days_either_side: int = 3,
) -> dict[str, Any]:
    try:
        target = date.fromisoformat(date_iso)
    except ValueError:
        return {"error": f"date_iso must look like 2026-08-22, got {date_iso!r}"}
    # Windows are drawn between 06:00 and 22:00 below, so nothing longer fits. A
    # trip sent here as 96 hours scored no windows and came back as an empty
    # success, and the answer quietly lost its better-time half.
    if not 1 <= duration_hours <= 16:
        return {
            "error": (
                "duration_hours must be 1-16, the span of the day searched here. For a trip of "
                "several days, the day-by-day verdicts from check_travel_plan say which dates work."
            )
        }

    try:
        location = await _resolve(place)
        first = max(date.today(), target - timedelta(days=days_either_side))
        last = min(target + timedelta(days=days_either_side), date.today() + timedelta(days=15))
        if last < first:
            return {"error": "that range falls outside the 16-day forecast horizon"}
        # Weather and air quality are separate endpoints with nothing to say to
        # each other, so they are fetched at the same time. In series this was two
        # round trips before any windowing could start, and a round trip from the
        # container is slow enough to matter.
        bundle, pm25 = await asyncio.gather(
            weather.forecast_hours(location["latitude"], location["longitude"], first, last),
            weather.air_quality_hours(location["latitude"], location["longitude"], first, last),
        )
        rows = weather.merge_air_quality(bundle["rows"], pm25)
    except FetchError as exc:
        return {"error": str(exc)}

    candidates = []
    day = first
    while day <= last:
        for hour in range(6, 22 - duration_hours + 1):
            start = datetime.combine(day, datetime.min.time()).replace(hour=hour)
            end = start + timedelta(hours=duration_hours)
            window = weather.slice_window(rows, start, end)
            if len(window) < duration_hours:
                continue
            candidates.append(
                {
                    "label": f"{day:%a %d %b} {hour:02d}:00-{(hour + duration_hours):02d}:00",
                    "hours": window,
                    "daylight": bundle["daylight"].get(day.isoformat()),
                }
            )
        day += timedelta(days=1)

    ranked = rank_alternatives(
        candidates, activity=activity, needs_daylight=needs_daylight, limit=5
    )
    return {
        "location": location,
        "searched": {
            "from": first.isoformat(),
            "to": last.isoformat(),
            "windows_scored": len(candidates),
            "duration_hours": duration_hours,
        },
        "best_windows": ranked,
    }


@server.tool(
    description=(
        "Assess a trip to a destination across a date range. Returns day-by-day verdicts. "
        "Works beyond the forecast horizon by falling back to seasonal normals, which is "
        "reported in data_source and must be stated to the user."
    )
)
async def check_travel_plan(
    place: str,
    start_date_iso: str,
    end_date_iso: str,
) -> dict[str, Any]:
    try:
        start = date.fromisoformat(start_date_iso)
        end = date.fromisoformat(end_date_iso)
    except ValueError:
        return {"error": "dates must look like 2026-12-24"}
    if end < start:
        return {"error": "end_date_iso is before start_date_iso"}
    if (end - start).days > 30:
        return {"error": "keep the trip window to 30 days or fewer"}

    try:
        location = await _resolve(place)
    except FetchError as exc:
        return {"error": str(exc)}

    dates = []
    cursor = start
    while cursor <= end:
        dates.append(cursor)
        cursor += timedelta(days=1)

    async def one_day(day: date) -> dict[str, Any]:
        try:
            rows, _daylight, source = await _readings(
                location["latitude"],
                location["longitude"],
                day,
                datetime.combine(day, datetime.min.time()).replace(hour=8),
                datetime.combine(day, datetime.min.time()).replace(hour=20),
            )
        except FetchError as exc:
            return {"date": day.isoformat(), "error": str(exc)}
        assessment = assess_window(rows, activity="travel", data_source=source)
        return {
            "date": day.isoformat(),
            "verdict": assessment.verdict,
            "score": round(assessment.score, 1),
            "reasons": [r.as_dict() for r in assessment.reasons],
            "data_source": source,
        }

    # One day per request, fetched concurrently. Each day is independent, and in
    # series a four day trip meant a dozen sequential upstream calls: fast against
    # a warm cache locally, minutes against a cold one in a container.
    days = list(await asyncio.gather(*(one_day(day) for day in dates)))

    scored = [d for d in days if "score" in d]
    return {
        "location": location,
        "days": days,
        "summary": {
            "days_assessed": len(scored),
            "go_days": sum(1 for d in scored if d["verdict"] == "go"),
            "caution_days": sum(1 for d in scored if d["verdict"] == "caution"),
            "no_go_days": sum(1 for d in scored if d["verdict"] == "no_go"),
            "mean_score": round(sum(d["score"] for d in scored) / len(scored), 1)
            if scored
            else None,
        },
    }


@server.tool(
    description=(
        "List places matching a name so an ambiguous one can be disambiguated with the user. "
        "Call this when a place name could plausibly mean more than one location."
    )
)
async def find_places(name: str) -> dict[str, Any]:
    try:
        return {"matches": await weather.geocode(name, count=5)}
    except FetchError as exc:
        return {"error": str(exc)}


@server.tool(
    description=(
        "Suggest other places to travel to when a trip scores badly and the destination is the "
        "thing being chosen. Scores nearby and similar destinations for the same dates using "
        "historical climate and returns them ranked against the original. Trips only. Do not call "
        "it for a match, a game or any activity at a named local venue: the answer there is a "
        "different time or a covered venue, never a different city."
    )
)
async def suggest_alternative_destinations(
    original_place: str,
    start_date_iso: str,
    nights: int = 3,
    max_daily_inr: int | None = None,
    tags: list[str] | None = None,
    limit: int = 5,
    activity: str = "travel",
) -> dict[str, Any]:
    if activity not in ("travel", "trip"):
        # The catalogue is a holiday catalogue, so asked about a football pitch it
        # can only ever answer with a holiday. Relocating a match means another
        # ground across town, which this has no data for.
        return {
            "applicable": False,
            "reason": (
                f"This suggests places to travel to, and the plan is a {activity} activity at a "
                "venue. Moving one of those means another ground or a covered court in the same "
                "city, not another destination. Offer a different time instead."
            ),
        }

    try:
        start = date.fromisoformat(start_date_iso)
    except ValueError:
        return {"error": "start_date_iso must look like 2026-12-24"}

    try:
        origin = await _resolve(original_place)
    except FetchError as exc:
        return {"error": str(exc)}

    # Drawn nearest first and capped by how far the trip length justifies. The pool
    # used to be the cheapest entries nationwide, so proximity never entered it.
    reach = destinations.reach_km(nights)
    candidates = destinations.shortlist(
        max_daily_inr=max_daily_inr,
        tags=tags,
        limit=limit * 3,
        origin=(origin["latitude"], origin["longitude"]),
        within_km=reach,
    )
    origin_climate = await destinations.month_climate(
        origin["latitude"], origin["longitude"], start.month
    )

    # By distance, not by name. "Cubbon Park, Bengaluru" starts with "cubbon",
    # which matches nothing in the catalogue, so the origin city was never excluded.
    wanted = [row for row in candidates if row.get("distance_km", 0) > 60]

    async def score(row: dict[str, Any]) -> dict[str, Any] | None:
        climate = await destinations.month_climate(row["lat"], row["lon"], start.month)
        if not climate.get("available"):
            return None
        return {
            "name": row["name"],
            "country": row["country"],
            "region": row["region"],
            "daily_budget_inr": row["daily_budget_inr"],
            "budget_band": row["budget_band"],
            "tags": row["tags"],
            "distance_km": round(
                _haversine_km(origin["latitude"], origin["longitude"], row["lat"], row["lon"])
            ),
            "comfort_score": climate["comfort_score"],
            "mean_daily_high_c": climate["mean_daily_high_c"],
            "wet_day_share": climate["wet_day_share"],
        }

    # Candidates scored together; in series this was the slowest tool in the app.
    scored = [row for row in await asyncio.gather(*(score(r) for r in wanted)) if row]
    # Distance is a cost, not a decoration. It was computed per candidate and then
    # thrown away one line later by a sort on comfort alone.
    for row in scored:
        row["travel_cost"] = round(12.0 * min(row["distance_km"] / reach, 1.0), 1)
        row["rank_score"] = round(row["comfort_score"] - row["travel_cost"], 1)
    scored.sort(key=lambda r: r["rank_score"], reverse=True)
    return {
        "original": {
            "place": original_place,
            "month": start.month,
            "comfort_score": origin_climate.get("comfort_score"),
            "available": origin_climate.get("available", False),
        },
        "alternatives": scored[:limit],
        "basis": (
            "Comfort scores come from five years of reanalysis for each coordinate in the month "
            "requested. Budget bands are rounded planning estimates and must be labelled as such."
        ),
    }


@server.tool(
    description=(
        "Return the exact threshold policy behind every verdict. Use when the user asks why a "
        "plan was rejected or what counts as too hot, too wet or too windy."
    )
)
def explain_thresholds(activity: str = "outdoor") -> dict[str, Any]:
    return {
        "activity": activity,
        "thresholds": THRESHOLDS.get(activity, THRESHOLDS["outdoor"]),
        "policy": (
            "Any reading past a blocker line makes the verdict no_go. Otherwise any reading past "
            "a caution line makes it caution. Peaks across the window are used, not averages, "
            "because one bad hour ruins a match. Verdicts from seasonal normals are capped at "
            "caution because a normal is a distribution, not a prediction."
        ),
        "air_quality_basis": (
            "PM2.5 lines follow the CPCB national AQI breakpoints: 61 moderate, 91 poor."
        ),
    }


if __name__ == "__main__":
    server.run("stdio")
