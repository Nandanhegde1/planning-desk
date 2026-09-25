#!/usr/bin/env python3
"""Preflight: call every external dependency once and report what answered.

Run this before the app on a new machine. It settles "is it my code or is it the
network" in ten seconds.

    python scripts/check_apis.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    import dotenv  # noqa: F401
    import httpx  # noqa: F401
    import numpy  # noqa: F401
except ImportError as exc:
    activate = "Scripts" if os.name == "nt" else "bin"
    print(f"\nMissing dependency: {exc.name or exc}. Install them first:\n")
    print("    python -m venv .venv")
    print(f"    source .venv/{activate}/activate")
    print("    pip install -r requirements.txt\n")
    raise SystemExit(1) from None

from app import config  # noqa: E402
from core import destinations, fx, weather  # noqa: E402
from core.http import FetchError  # noqa: E402

PASS, FAIL, SKIP = "  ok  ", " FAIL ", " skip "


def line(status: str, name: str, detail: str = "") -> None:
    print(f"[{status}] {name:<34} {detail}")


async def check_geocoding() -> tuple[float, float] | None:
    try:
        matches = await weather.geocode("Bengaluru")
        if not matches:
            line(FAIL, "Open-Meteo geocoding", "no results for a known city")
            return None
        top = matches[0]
        line(PASS, "Open-Meteo geocoding", f"{top['name']}, {top['country']}")
        return top["latitude"], top["longitude"]
    except FetchError as exc:
        line(FAIL, "Open-Meteo geocoding", str(exc)[:80])
        return None


async def check_forecast(point: tuple[float, float]) -> None:
    try:
        today = date.today()
        bundle = await weather.forecast_hours(point[0], point[1], today, today + timedelta(days=2))
        line(PASS, "Open-Meteo forecast", f"{len(bundle['rows'])} hourly rows")
    except FetchError as exc:
        line(FAIL, "Open-Meteo forecast", str(exc)[:80])


async def check_air_quality(point: tuple[float, float]) -> None:
    today = date.today()
    readings = await weather.air_quality_hours(point[0], point[1], today, today)
    if readings:
        line(PASS, "Open-Meteo air quality", f"{len(readings)} PM2.5 readings")
    else:
        line(FAIL, "Open-Meteo air quality", "empty; verdicts will skip the air check")


async def check_archive(point: tuple[float, float]) -> None:
    try:
        climate = await destinations.month_climate(point[0], point[1], 12, years_back=2)
        if climate.get("available"):
            line(PASS, "Open-Meteo archive", f"December mean high {climate['mean_daily_high_c']}C")
        else:
            line(FAIL, "Open-Meteo archive", "no data returned")
    except FetchError as exc:
        line(FAIL, "Open-Meteo archive", str(exc)[:80])


async def check_rates() -> None:
    end = date.today()
    start = end - timedelta(days=30)
    try:
        series = await fx.fetch_series("USD", ["INR"], start, end)
        meta = series["meta"]
        line(PASS, "Exchange rates (single base)", f"latest {meta['last_published']}")
    except FetchError as exc:
        line(FAIL, "Exchange rates (single base)", str(exc)[:80])
        return

    # The three-currency comparison spans two bases, so it is worth its own check.
    try:
        series = await fx.fetch_pairs(["USD/INR", "INR/GBP", "INR/EUR"], start, end)
        line(PASS, "Exchange rates (cross base)", ", ".join(series["columns"]))
    except FetchError as exc:
        line(FAIL, "Exchange rates (cross base)", str(exc)[:80])


async def check_search() -> None:
    # Import through the host, not directly, so this checks what the server
    # process actually receives rather than what this process happens to hold.
    from app.mcp_host import MCPHost

    async with MCPHost() as host:
        state = await host.call("discovery__check_configuration", {})
    if state.get("status") == "ready":
        line(PASS, "Search config reached server", state["search_provider"])
    elif state.get("status") == "search disabled":
        line(SKIP, "Search config", "SEARCH_PROVIDER=none, a supported setting")
    else:
        line(FAIL, "Search config", state.get("status", "unknown"))

    from servers.discovery_server import PROVIDER, web_search

    if PROVIDER == "none":
        line(SKIP, "Web search", "SEARCH_PROVIDER=none, which is a supported configuration")
        return
    result = await web_search("exchange rate news", 3)
    if result.get("available"):
        line(PASS, f"Web search ({result['provider']})", f"{len(result['results'])} results")
    else:
        line(FAIL, "Web search", str(result.get("error") or result.get("reason"))[:80])


def check_model() -> None:
    ready, hint = config.provider_ready()
    if ready:
        line(PASS, f"Model ({config.PROVIDER})", config.MODEL)
    else:
        line(FAIL, f"Model ({config.PROVIDER})", hint)


async def main() -> int:
    print("\nChecking external dependencies\n" + "-" * 62)
    check_model()
    point = await check_geocoding()
    if point:
        await check_forecast(point)
        await check_air_quality(point)
        await check_archive(point)
    await check_rates()
    await check_search()
    print("-" * 62)
    print("Anything marked FAIL will degrade gracefully at runtime: the tool")
    print("returns an error and the assistant reports it rather than guessing.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
