"""MCP server: exchange rates, trend statistics and file generation.

Fetched series are kept in process so every chart and export describes the same
dataset and a re-chart costs no download.
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from mcp.server import MCPServer

from core import exports, fx
from core.http import FetchError

server = MCPServer("fx", version="1.0.0")

_STORE: dict[str, dict[str, Any]] = {}
_MAX_SERIES = 12
_COUNTER = 0

CHART_TYPES = exports.CHART_TYPES
RATE_UNAVAILABLE = "Say the rate service is unavailable. Never supply a rate from memory."


def _remember(series: dict[str, Any]) -> str:
    global _COUNTER
    _COUNTER += 1
    slug = "-".join(c.replace("/", "").lower() for c in series["columns"])[:40]
    series_id = f"{slug}-{_COUNTER}"
    if len(_STORE) >= _MAX_SERIES:
        _STORE.pop(next(iter(_STORE)))
    _STORE[series_id] = series
    return series_id


def _summary(series_id: str, series: dict[str, Any]) -> dict[str, Any]:
    rows = series["rows"]
    step = max(1, len(rows) // 24)
    return {
        "series_id": series_id,
        "columns": series["columns"],
        "meta": series["meta"],
        "statistics": fx.summarise(series),
        "sample": rows[::step][:24],
        "sample_note": (
            f"Every {step}th row of {len(rows)}. The full series is held server-side under "
            "series_id; pass that to the chart and export tools."
        ),
    }


def _dates(
    start_date_iso: str | None, end_date_iso: str | None
) -> tuple[date, date] | dict[str, str]:
    try:
        end = date.fromisoformat(end_date_iso) if end_date_iso else date.today()
        start = date.fromisoformat(start_date_iso) if start_date_iso else end - timedelta(days=730)
    except ValueError:
        return {"error": "dates must look like 2026-08-17"}
    if start >= end:
        return {"error": "the start date must fall before the end date"}
    return start, end


@server.tool(
    description=(
        "Fetch daily exchange rates for one base currency against one or more quote currencies "
        "from European Central Bank reference data. Defaults to the last two years. Returns a "
        "series_id that the chart and export tools accept, plus trend statistics per pair. "
        "Use ISO codes such as USD, INR, GBP, EUR."
    )
)
async def get_rate_series(
    base: str = "USD",
    quotes: list[str] | None = None,
    start_date_iso: str | None = None,
    end_date_iso: str | None = None,
) -> dict[str, Any]:
    window = _dates(start_date_iso, end_date_iso)
    if isinstance(window, dict):
        return window
    try:
        series = await fx.fetch_series(base, quotes or ["INR"], *window)
    except FetchError as exc:
        return {"error": str(exc), "advice": RATE_UNAVAILABLE}
    return _summary(_remember(series), series)


@server.tool(
    description=(
        "Fetch several currency pairs that do not share a base currency, so they can be compared "
        "on one chart. Pairs look like USD/INR or INR/GBP. This is the tool for a request such as "
        "'compare USD to INR against INR to GBP and INR to EUR', which get_rate_series cannot do "
        "because it takes a single base. Returns one series_id covering all pairs."
    )
)
async def compare_pairs(
    pairs: list[str],
    start_date_iso: str | None = None,
    end_date_iso: str | None = None,
) -> dict[str, Any]:
    if not pairs:
        return {"error": "give at least one pair, for example ['USD/INR', 'INR/GBP']"}
    if len(pairs) > 6:
        return {"error": "six pairs is the limit; a chart with more is unreadable"}

    window = _dates(start_date_iso, end_date_iso)
    if isinstance(window, dict):
        return window

    try:
        series = await fx.fetch_pairs(pairs, *window)
    except ValueError as exc:
        return {"error": str(exc)}
    except FetchError as exc:
        return {"error": str(exc), "advice": RATE_UNAVAILABLE}

    result = _summary(_remember(series), series)
    result["indexed_statistics"] = fx.summarise(series, indexed=True)
    result["comparison_note"] = (
        "These pairs differ by orders of magnitude, so the indexed statistics and the indexed "
        "chart are the fair comparison. Say so when presenting them."
    )
    return result


@server.tool(
    description=(
        "Recompute the least-squares trend for one pair in a fetched series. Set indexed to true "
        "for the form rebased to 100 at the series start, which is the comparable form when pairs "
        "have very different magnitudes."
    )
)
def compute_trendline(series_id: str, pair: str, indexed: bool = False) -> dict[str, Any]:
    series = _STORE.get(series_id)
    if not series:
        return {"error": f"unknown series_id {series_id!r}; fetch a series first"}

    wanted = pair.upper().replace("-", "/")
    if wanted not in series["columns"]:
        return {"error": f"{wanted} is not in this series; it holds {series['columns']}"}

    dates = [r["date"] for r in series["rows"]]
    values = [r.get(wanted) for r in series["rows"]]
    if indexed:
        values = fx.rebase_to_100(values)

    fit = fx.trendline(dates, values)
    fit["pair"] = wanted
    fit["form"] = "index, start = 100" if indexed else "rate"
    return fit


@server.tool(
    description=(
        "Write the series to an Excel workbook: a rates sheet with live SLOPE and INTERCEPT trend "
        "formulas, monthly averages, a methodology sheet, and a native comparison chart. Returns "
        "a download path."
    )
)
def export_spreadsheet(series_id: str) -> dict[str, Any]:
    series = _STORE.get(series_id)
    if not series:
        return {"error": f"unknown series_id {series_id!r}; fetch a series first"}
    path = exports.build_workbook(series)
    return {
        "file": f"/files/{path.name}",
        "filename": path.name,
        "kind": "xlsx",
        "sheets": ["Rates", "Monthly", "Methodology"],
        "note": (
            "Trend, index and 30-day average are formulas, so the workbook "
            "recalculates if rows change."
        ),
    }


@server.tool(
    description=(
        "Render a chart from a fetched series. chart_type is one of indexed, line, trend, "
        "monthly_bar or small_multiples. Choose indexed when comparing pairs of different "
        "magnitudes, trend to show a fit, monthly_bar for period-on-period movement, and "
        "small_multiples when each pair needs its own axis. Displays inline in the interface."
    )
)
def render_chart(
    series_id: str, chart_type: str = "indexed", title: str | None = None
) -> dict[str, Any]:
    series = _STORE.get(series_id)
    if not series:
        return {"error": f"unknown series_id {series_id!r}; fetch a series first"}
    if chart_type not in CHART_TYPES:
        return {"error": f"chart_type must be one of {CHART_TYPES}"}
    path = exports.render_chart(series, chart_type=chart_type, title=title)
    return {
        "file": f"/files/{path.name}",
        "filename": path.name,
        "kind": "image",
        "chart_type": chart_type,
    }


@server.tool(
    description=(
        "Write a Word report: a trend paragraph per pair, embedded charts, a monthly average "
        "table, and a method and limits section. Returns a download path."
    )
)
def export_document(series_id: str, chart_types: list[str] | None = None) -> dict[str, Any]:
    series = _STORE.get(series_id)
    if not series:
        return {"error": f"unknown series_id {series_id!r}; fetch a series first"}
    wanted = [c for c in (chart_types or ["indexed", "trend"]) if c in CHART_TYPES]
    charts = [exports.render_chart(series, chart_type=c) for c in wanted]
    path = exports.build_document(series, charts=charts)
    return {
        "file": f"/files/{path.name}",
        "filename": path.name,
        "kind": "docx",
        "charts_embedded": wanted,
    }


@server.tool(description="List the series fetched so far in this session, with their identifiers.")
def list_series() -> dict[str, Any]:
    return {
        "series": [
            {
                "series_id": key,
                "pairs": value["columns"],
                "from": value["meta"]["first_published"],
                "to": value["meta"]["last_published"],
                "rows": len(value["rows"]),
            }
            for key, value in _STORE.items()
        ],
        "chart_types": CHART_TYPES,
    }


if __name__ == "__main__":
    server.run("stdio")
