"""Exchange rates and trend statistics, from ECB reference data via Frankfurter.

A series holds named pair columns rather than one base with many quotes, so a
comparison can span bases: USD/INR against INR/GBP is two pairs, not one base
and two quotes.

ECB rates are euro reference rates published on business days only. Non-EUR
pairs are therefore crosses through EUR, and the series is reindexed onto every
calendar day with gaps carried forward.
"""

from __future__ import annotations

import asyncio
from datetime import date, timedelta
from typing import Any

import numpy as np

from core.http import FetchError, get_json

HOSTS = ["https://api.frankfurter.dev/v1", "https://api.frankfurter.app"]

CAVEAT = (
    "ECB reference rates via Frankfurter. Non-EUR pairs are crosses computed through EUR, "
    "suitable for trend analysis, not for pricing a transaction."
)


def parse_pair(pair: str) -> tuple[str, str]:
    """Accept USD/INR, USD-INR or USDINR."""
    cleaned = pair.upper().strip().replace("-", "/").replace(" ", "")
    if "/" not in cleaned and len(cleaned) == 6:
        cleaned = f"{cleaned[:3]}/{cleaned[3:]}"
    if "/" not in cleaned:
        raise ValueError(
            f"cannot read {pair!r} as a currency pair, expected something like USD/INR"
        )
    base, quote = cleaned.split("/", 1)
    if len(base) != 3 or len(quote) != 3:
        raise ValueError(f"{pair!r} does not look like two ISO currency codes")
    return base, quote


async def _fetch_one(base: str, quotes: list[str], start: date, end: date) -> dict[str, Any]:
    params = {"base": base, "symbols": ",".join(quotes)}
    path = f"/{start.isoformat()}..{end.isoformat()}"
    last_error: Exception | None = None
    for host in HOSTS:
        try:
            return await get_json(host + path, params, ttl_seconds=43200)
        except FetchError as exc:
            last_error = exc
    raise FetchError(f"no exchange-rate host answered: {last_error}")


def _build(by_column: dict[str, dict[str, float]], columns: list[str]) -> dict[str, Any]:
    """Merge per-column date maps onto one daily calendar, carrying gaps forward."""
    all_dates = sorted({d for column in by_column.values() for d in column})
    if not all_dates:
        raise FetchError("no rates were returned for that range")

    # De-duplicated, preserving order. The completeness check below compares a
    # dict of carried values against this list, so a repeated column made the two
    # lengths permanently unequal and the series came back empty with no error.
    columns = list(dict.fromkeys(columns))

    rows: list[dict[str, Any]] = []
    filled = 0
    carried: dict[str, float] = {}
    cursor = date.fromisoformat(all_dates[0])
    stop = date.fromisoformat(all_dates[-1])

    while cursor <= stop:
        key = cursor.isoformat()
        is_fill = True
        for column in columns:
            value = by_column.get(column, {}).get(key)
            if value is not None:
                carried[column] = value
                is_fill = False
        if len(carried) == len(columns):
            rows.append({"date": key, "carried_forward": is_fill, **carried})
            filled += is_fill
        cursor += timedelta(days=1)

    return {
        "columns": columns,
        "rows": rows,
        "meta": {
            "source": "European Central Bank via Frankfurter (api.frankfurter.dev)",
            "published_days": len(all_dates),
            "calendar_days": len(rows),
            "carried_forward_days": filled,
            # The first emitted row, not the first published date. Leading days are
            # skipped until every column has a value, so the two differ when one
            # pair starts later, and the interface claimed a start date absent
            # from its own table.
            "first_published": rows[0]["date"] if rows else all_dates[0],
            "first_published_any_column": all_dates[0],
            "last_published": all_dates[-1],
            "caveat": CAVEAT,
        },
    }


async def fetch_series(base: str, quotes: list[str], start: date, end: date) -> dict[str, Any]:
    """One base against several quotes, for example USD against INR and GBP."""
    base = base.upper()
    quotes = [q.upper() for q in quotes]
    payload = await _fetch_one(base, quotes, start, end)

    columns = [f"{base}/{q}" for q in quotes]
    by_column: dict[str, dict[str, float]] = {c: {} for c in columns}
    for day, values in (payload.get("rates", {}) or {}).items():
        for quote in quotes:
            if values.get(quote) is not None:
                by_column[f"{base}/{quote}"][day] = values[quote]

    # fetch_pairs guards on a missing key, but here by_column is pre-populated with
    # every column, so an unsupported symbol or a same-currency pair arrived as an
    # empty column and produced an empty series instead of an error.
    empty = [column for column, days in by_column.items() if not days]
    if empty:
        raise FetchError(f"no data returned for {', '.join(empty)}")

    series = _build(by_column, columns)
    series["meta"]["request"] = f"{base} against {', '.join(quotes)}"
    return series


async def fetch_pairs(pairs: list[str], start: date, end: date) -> dict[str, Any]:
    """Arbitrary pairs that need not share a base, for example USD/INR and INR/GBP.

    Pairs sharing a base are fetched in one request.
    """
    parsed = [parse_pair(p) for p in pairs]
    grouped: dict[str, list[str]] = {}
    for base, quote in parsed:
        grouped.setdefault(base, []).append(quote)

    payloads = await asyncio.gather(
        *(_fetch_one(base, quotes, start, end) for base, quotes in grouped.items())
    )

    by_column: dict[str, dict[str, float]] = {}
    for (base, quotes), payload in zip(grouped.items(), payloads, strict=True):
        for day, values in (payload.get("rates", {}) or {}).items():
            for quote in quotes:
                if values.get(quote) is not None:
                    by_column.setdefault(f"{base}/{quote}", {})[day] = values[quote]

    columns = [f"{b}/{q}" for b, q in parsed]
    missing = [c for c in columns if c not in by_column]
    if missing:
        raise FetchError(f"no data returned for {', '.join(missing)}")

    series = _build(by_column, columns)
    series["meta"]["request"] = ", ".join(columns)
    return series


def trendline(dates: list[str], values: list[float | None]) -> dict[str, Any]:
    """Least-squares fit, reported as change per year so it can be argued with."""
    clean = [(d, v) for d, v in zip(dates, values, strict=True) if v is not None]
    if len(clean) < 3:
        return {"error": "need at least three points to fit a trend"}

    dates_clean = [d for d, _ in clean]
    y = np.array([v for _, v in clean], dtype=float)
    origin = date.fromisoformat(dates_clean[0])
    x = np.array([(date.fromisoformat(d) - origin).days for d in dates_clean], dtype=float)

    slope, intercept = np.polyfit(x, y, 1)
    fitted = slope * x + intercept
    ss_res = float(np.sum((y - fitted) ** 2))
    ss_tot = float(np.sum((y - y.mean()) ** 2))

    return {
        "slope_per_day": float(slope),
        "slope_per_year": float(slope * 365.25),
        "intercept": float(intercept),
        "r_squared": round(1 - ss_res / ss_tot, 4) if ss_tot > 0 else 0.0,
        "fit_start": {"date": dates_clean[0], "value": round(float(fitted[0]), 6)},
        "fit_end": {"date": dates_clean[-1], "value": round(float(fitted[-1]), 6)},
        "observed_start": {"date": dates_clean[0], "value": round(float(y[0]), 6)},
        "observed_end": {"date": dates_clean[-1], "value": round(float(y[-1]), 6)},
        "change_pct": round(float((y[-1] - y[0]) / y[0] * 100), 2) if y[0] else None,
        "min": {"date": dates_clean[int(np.argmin(y))], "value": round(float(y.min()), 6)},
        "max": {"date": dates_clean[int(np.argmax(y))], "value": round(float(y.max()), 6)},
        "points": len(clean),
    }


def rebase_to_100(values: list[float | None]) -> list[float | None]:
    """Index a series to 100 at its first value.

    Needed for multi-pair charts: USD/INR is near 88 and INR/GBP near 0.0095,
    which share no axis.
    """
    first = next((v for v in values if v), None)
    if not first:
        return [None] * len(values)
    return [round(v / first * 100, 4) if v else None for v in values]


def monthly_average(rows: list[dict[str, Any]], column: str) -> list[dict[str, Any]]:
    """Mean over published days, not calendar days.

    The ECB publishes on working days, and the series carries the last rate
    forward across weekends and holidays so charts have a continuous axis. Averaging
    those carried rows counts every Friday three times, which is not what a monthly
    average means. Central banks publish theirs over business days, so a
    calendar-day mean labelled "average" is a quietly wrong number in a deliverable.
    """
    buckets: dict[str, list[float]] = {}
    for row in rows:
        if row.get("carried_forward"):
            continue
        value = row.get(column)
        if value is not None:
            buckets.setdefault(row["date"][:7], []).append(float(value))
    return [
        {"month": month, "average": round(sum(v) / len(v), 6), "days": len(v)}
        for month, v in sorted(buckets.items())
    ]


def moving_average(values: list[float | None], window: int = 30) -> list[float | None]:
    out: list[float | None] = []
    buffer: list[float] = []
    for value in values:
        if value is None:
            out.append(None)
            continue
        buffer.append(float(value))
        if len(buffer) > window:
            buffer.pop(0)
        out.append(round(sum(buffer) / len(buffer), 6) if len(buffer) == window else None)
    return out


def summarise(series: dict[str, Any], *, indexed: bool = False) -> dict[str, Any]:
    dates = [r["date"] for r in series["rows"]]
    out = {}
    for column in series["columns"]:
        values = [r.get(column) for r in series["rows"]]
        if indexed:
            values = rebase_to_100(values)
        out[column] = trendline(dates, values)
    return out
