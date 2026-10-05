"""Weather, air quality and place lookup via Open-Meteo and OpenStreetMap."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from core.http import FetchError, get_json, open_meteo

GEOCODE_URL = open_meteo("https://geocoding-api.open-meteo.com/v1/search")
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
FORECAST_URL = open_meteo("https://api.open-meteo.com/v1/forecast")
AIR_URL = open_meteo("https://air-quality-api.open-meteo.com/v1/air-quality")
ARCHIVE_URL = open_meteo("https://archive-api.open-meteo.com/v1/archive")

FORECAST_HORIZON_DAYS = 15
# The air-quality forecast is far shorter. On 2026-10-05 its API refused any
# end_date past 2026-10-11 with a 400, and inside that range PM2.5 stopped at
# 2026-10-10T05:00. A request reaching past the limit lost PM2.5 for every day in
# it, the near ones included, so requests stop here, a day short of where the
# readings end in case the host's date and the API's differ.
AIR_QUALITY_HORIZON_DAYS = 4

HOURLY_VARS = [
    "temperature_2m",
    "apparent_temperature",
    "precipitation_probability",
    "precipitation",
    "wind_speed_10m",
    "wind_gusts_10m",
    "uv_index",
    "relative_humidity_2m",
]

_FIELD_MAP = {
    "temperature_2m": "temp",
    "apparent_temperature": "apparent_temp",
    "precipitation_probability": "precip_probability",
    "precipitation": "precipitation",
    "wind_speed_10m": "wind_speed",
    "wind_gusts_10m": "wind_gusts",
    "uv_index": "uv_index",
    "relative_humidity_2m": "humidity",
}


async def _nominatim(place: str, count: int) -> list[dict[str, Any]]:
    """OpenStreetMap search. Rate limited to 1 req/s in core.http per Nominatim
    policy."""
    try:
        payload = await get_json(
            NOMINATIM_URL,
            {"q": place, "format": "jsonv2", "limit": count, "addressdetails": 1},
            ttl_seconds=86400 * 7,
        )
    except FetchError:
        return []

    results = []
    for row in payload or []:
        address = row.get("address", {}) or {}
        results.append(
            {
                "name": row.get("name") or row.get("display_name", "").split(",")[0],
                "admin1": address.get("state") or address.get("state_district"),
                "country": address.get("country"),
                "country_code": (address.get("country_code") or "").upper() or None,
                "latitude": float(row["lat"]),
                "longitude": float(row["lon"]),
                "timezone": None,
                "kind": row.get("addresstype") or row.get("type"),
                "display_name": row.get("display_name"),
                "resolved_by": "openstreetmap",
            }
        )
    return results


async def geocode(place: str, *, count: int = 5) -> list[dict[str, Any]]:
    """Resolve a place name to coordinates, returning every match.

    Open-Meteo indexes populated places, OpenStreetMap indexes venues. The second
    is queried only when the first returns nothing.
    """
    payload = await get_json(
        GEOCODE_URL,
        {"name": place, "count": count, "language": "en", "format": "json"},
        ttl_seconds=86400 * 7,
    )
    results = []
    for row in payload.get("results", []) or []:
        results.append(
            {
                "name": row.get("name"),
                "admin1": row.get("admin1"),
                "country": row.get("country"),
                "country_code": row.get("country_code"),
                "latitude": row.get("latitude"),
                "longitude": row.get("longitude"),
                "timezone": row.get("timezone"),
                "population": row.get("population"),
                "resolved_by": "gazetteer",
            }
        )

    if not results:
        results = await _nominatim(place, count)
    return results


def _rows_from_hourly(hourly: dict[str, Any]) -> list[dict[str, Any]]:
    times = hourly.get("time") or []
    rows: list[dict[str, Any]] = []
    for index, stamp in enumerate(times):
        row: dict[str, Any] = {"time": stamp}
        for source, target in _FIELD_MAP.items():
            series = hourly.get(source)
            if series is not None and index < len(series):
                row[target] = series[index]
        rows.append(row)
    return rows


async def forecast_hours(
    latitude: float,
    longitude: float,
    start: date,
    end: date,
) -> dict[str, Any]:
    """Hourly forecast plus sunrise/sunset for a date range inside the horizon."""
    payload = await get_json(
        FORECAST_URL,
        {
            "latitude": latitude,
            "longitude": longitude,
            "hourly": ",".join(HOURLY_VARS),
            "daily": "sunrise,sunset",
            "timezone": "auto",
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
        },
        ttl_seconds=1800,
    )
    daily = payload.get("daily", {})
    daylight = {
        day: {"sunrise": sr, "sunset": ss}
        for day, sr, ss in zip(
            daily.get("time", []),
            daily.get("sunrise", []),
            daily.get("sunset", []),
            strict=False,
        )
    }
    return {
        "rows": _rows_from_hourly(payload.get("hourly", {})),
        "daylight": daylight,
        "timezone": payload.get("timezone"),
        # Timestamps are local and carry no zone, so this is the only way to tell
        # what time it is there now. The archive path already passed it through.
        "utc_offset_seconds": payload.get("utc_offset_seconds"),
        "source": "open-meteo forecast",
    }


async def air_quality_hours(
    latitude: float, longitude: float, start: date, end: date
) -> dict[str, list[Any]]:
    """PM2.5 by hour, keyed by ISO timestamp. Empty if the service is down; air
    quality is a secondary signal and must not fail the request."""
    last = date.today() + timedelta(days=AIR_QUALITY_HORIZON_DAYS)
    if start > last:
        return {}
    end = min(end, last)
    try:
        payload = await get_json(
            AIR_URL,
            {
                "latitude": latitude,
                "longitude": longitude,
                "hourly": "pm2_5,pm10",
                "timezone": "auto",
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
            },
            ttl_seconds=3600,
        )
    except FetchError:
        return {}
    hourly = payload.get("hourly", {})
    times = hourly.get("time", [])
    pm25 = hourly.get("pm2_5", [])
    return {stamp: pm25[i] for i, stamp in enumerate(times) if i < len(pm25)}


async def seasonal_normals(
    latitude: float,
    longitude: float,
    target: date,
    *,
    years_back: int = 5,
    window_days: int = 3,
) -> dict[str, Any]:
    """Typical conditions for a calendar date, averaged over recent years.

    Used past the forecast horizon. Labelled climatology so downstream code
    cannot mistake it for a forecast.
    """
    samples: list[dict[str, Any]] = []
    utc_offset: int | None = None
    this_year = date.today().year
    for offset in range(1, years_back + 1):
        year = this_year - offset
        try:
            anchor = target.replace(year=year)
        except ValueError:  # 29 February in a non-leap year
            anchor = target.replace(year=year, day=28)
        start = anchor - timedelta(days=window_days)
        end = anchor + timedelta(days=window_days)
        try:
            payload = await get_json(
                ARCHIVE_URL,
                {
                    "latitude": latitude,
                    "longitude": longitude,
                    "hourly": (
                        "temperature_2m,apparent_temperature,precipitation,"
                        "wind_speed_10m,wind_gusts_10m"
                    ),
                    "timezone": "auto",
                    "start_date": start.isoformat(),
                    "end_date": end.isoformat(),
                },
                ttl_seconds=86400 * 30,
            )
        except FetchError:
            continue
        samples.extend(_rows_from_hourly(payload.get("hourly", {})))
        if utc_offset is None:
            utc_offset = payload.get("utc_offset_seconds")

    if not samples:
        return {"rows": [], "source": "climatology", "years_sampled": 0,
                "utc_offset_seconds": utc_offset}

    buckets: dict[int, list[dict[str, Any]]] = {}
    for row in samples:
        try:
            hour = datetime.fromisoformat(row["time"]).hour
        except (ValueError, KeyError):
            continue
        buckets.setdefault(hour, []).append(row)

    rows = []
    for hour in sorted(buckets):
        group = buckets[hour]

        def mean(key: str, rows: list[dict[str, Any]] = group) -> float | None:
            values = [r[key] for r in rows if r.get(key) is not None]
            return round(sum(values) / len(values), 2) if values else None

        wet = [g for g in group if (g.get("precipitation") or 0) > 0.1]
        rows.append(
            {
                "time": f"{target.isoformat()}T{hour:02d}:00",
                "temp": mean("temp"),
                "apparent_temp": mean("apparent_temp"),
                "precipitation": mean("precipitation"),
                "wind_speed": mean("wind_speed"),
                # The archive does return wind_gusts_10m. This used to report the mean
                # sustained wind as a gust, which is roughly a fifth of the real peak,
                # so the 40 and 60 km/h gust lines could never be reached from
                # climatology. Where a gust genuinely is absent the key is omitted
                # below, because the rules skip a missing reading and a missing one is
                # honest where a fabricated one reads as reassurance.
                "wind_gusts": mean("wind_gusts"),
                "precip_probability": round(100 * len(wet) / len(group)) if group else None,
            }
        )

    rows = [{k: v for k, v in row.items() if v is not None} for row in rows]

    return {
        "rows": rows,
        "source": "climatology",
        "years_sampled": years_back,
        "utc_offset_seconds": utc_offset,
        "note": (
            f"Hourly means for {target:%d %B} +/- {window_days} days "
            f"across the last {years_back} years."
        ),
    }


def merge_air_quality(rows: list[dict[str, Any]], pm25: dict[str, Any]) -> list[dict[str, Any]]:
    for row in rows:
        value = pm25.get(row.get("time"))
        if value is not None:
            row["pm2_5"] = value
    return rows


def slice_window(
    rows: list[dict[str, Any]], start: datetime, end: datetime
) -> list[dict[str, Any]]:
    """Keep the rows whose timestamp falls inside [start, end]."""
    kept = []
    for row in rows:
        try:
            stamp = datetime.fromisoformat(row["time"])
        except (ValueError, KeyError):
            continue
        if start <= stamp <= end:
            kept.append(row)
    return kept


def within_forecast_horizon(target: date) -> bool:
    """Both ends. A one sided check called 2019 forecastable, so a past date was
    sent to the forecast API, which rejected the range and failed the whole tool
    instead of falling back to the archive that does hold that day."""
    delta = (target - date.today()).days
    return 0 <= delta <= FORECAST_HORIZON_DAYS
