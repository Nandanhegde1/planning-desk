"""Deterministic feasibility scoring.

Pure functions over plain data. No I/O, no model.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

Verdict = Literal["go", "caution", "no_go"]
Severity = Literal["blocker", "caution", "note"]

# Bases for the numbers, so they can be argued with rather than guessed at.
# Wind follows the Beaufort scale: 29-38 km/h is a fresh breeze that outdoor
# sport is routinely played in, 39-49 is strong, 50-61 is near gale. PM2.5
# follows the CPCB national AQI breakpoints: 61 moderate, 91 poor. Rainfall of
# under 1 mm/h is drizzle.
THRESHOLDS: dict[str, Any] = {
    "outdoor": {
        "precip_probability_pct": {"caution": 45, "blocker": 70},
        "precipitation_mm_per_h": {"caution": 1.0, "blocker": 4.0},
        "wind_gusts_kmh": {"caution": 40, "blocker": 60},
        "apparent_temp_c_high": {"caution": 34, "blocker": 40},
        "apparent_temp_c_low": {"caution": 8, "blocker": 2},
        "pm2_5_ug_m3": {"caution": 61, "blocker": 91},
        "uv_index": {"caution": 9, "blocker": 11},
    },
    # Indoor play is weather-independent; the journey there is not.
    "indoor": {
        "precipitation_mm_per_h": {"caution": 4.0, "blocker": 12.0},
        "wind_gusts_kmh": {"caution": 60, "blocker": 85},
        "pm2_5_ug_m3": {"caution": 121, "blocker": 181},
    },
    "travel": {
        "precip_probability_pct": {"caution": 55, "blocker": 80},
        "precipitation_mm_per_h": {"caution": 2.0, "blocker": 7.5},
        "wind_gusts_kmh": {"caution": 45, "blocker": 65},
        "apparent_temp_c_high": {"caution": 36, "blocker": 42},
        "apparent_temp_c_low": {"caution": 2, "blocker": -5},
        "pm2_5_ug_m3": {"caution": 91, "blocker": 151},
    },
}

# Threshold key, the reading it inspects, and which direction is bad.
_CHECKS: list[tuple[str, str, str, str]] = [
    ("precip_probability_pct", "precip_probability", "above", "chance of rain"),
    ("precipitation_mm_per_h", "precipitation", "above", "rainfall"),
    ("wind_gusts_kmh", "wind_gusts", "above", "wind gusts"),
    ("apparent_temp_c_high", "apparent_temp", "above", "feels-like heat"),
    ("apparent_temp_c_low", "apparent_temp", "below", "feels-like cold"),
    ("pm2_5_ug_m3", "pm2_5", "above", "PM2.5"),
    ("uv_index", "uv_index", "above", "UV index"),
]

_UNITS = {
    "precip_probability": "%",
    "precipitation": " mm/h",
    "wind_gusts": " km/h",
    "apparent_temp": "\u00b0C",
    "pm2_5": " \u00b5g/m\u00b3",
    "uv_index": "",
}


@dataclass
class Reason:
    code: str
    severity: Severity
    detail: str
    reading: float | None = None
    threshold: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "detail": self.detail,
            "reading": self.reading,
            "threshold": self.threshold,
        }


@dataclass
class Assessment:
    verdict: Verdict
    score: float
    reasons: list[Reason] = field(default_factory=list)
    window: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "score": round(self.score, 1),
            "reasons": [r.as_dict() for r in self.reasons],
            "window": self.window,
        }


def _fmt(value: float, key: str) -> str:
    if value == int(value):
        return f"{int(value)}{_UNITS.get(key, '')}"
    return f"{value:.1f}{_UNITS.get(key, '')}"


def assess_window(
    hours: list[dict[str, Any]],
    *,
    activity: str = "outdoor",
    daylight: dict[str, str] | None = None,
    needs_daylight: bool = False,
    data_source: str = "forecast",
) -> Assessment:
    """Score a contiguous block of hourly readings.

    hours: dicts with any of precip_probability, precipitation, wind_gusts,
        apparent_temp, pm2_5, uv_index, plus a "time" ISO string.
    activity: outdoor, indoor or travel.
    daylight: {"sunrise": iso, "sunset": iso} for the day.
    needs_daylight: True for an outdoor match on an unlit ground.
    data_source: forecast, climatology for seasonal normals, or
        climatology_fallback when the forecast host was unreachable. Either
        climatology source softens a blocker down to caution, since a normal is a
        distribution and not a prediction, so it can never produce a no_go. A
        clean climatology window is still a go.
    """
    if not hours:
        return Assessment(
            verdict="caution",
            score=50.0,
            reasons=[Reason("no_data", "caution", "No readings were returned for that window.")],
        )

    policy = THRESHOLDS.get(activity, THRESHOLDS["outdoor"])
    reasons: list[Reason] = []
    penalty = 0.0

    for key, reading_key, direction, label in _CHECKS:
        limits = policy.get(key)
        if not limits:
            continue
        values = [h[reading_key] for h in hours if h.get(reading_key) is not None]
        if not values:
            continue
        peak = max(values) if direction == "above" else min(values)

        crossed: Severity | None = None
        limit = None
        if direction == "above":
            if peak >= limits["blocker"]:
                crossed, limit = "blocker", limits["blocker"]
            elif peak >= limits["caution"]:
                crossed, limit = "caution", limits["caution"]
        else:
            if peak <= limits["blocker"]:
                crossed, limit = "blocker", limits["blocker"]
            elif peak <= limits["caution"]:
                crossed, limit = "caution", limits["caution"]

        if crossed is None:
            continue
        # A normal describes a typical year, not this one. Any climatology source
        # counts, including the fallback used when the forecast host is down.
        if data_source.startswith("climatology") and crossed == "blocker":
            crossed = "caution"
        penalty += 45 if crossed == "blocker" else 18
        reasons.append(
            Reason(
                code=key,
                severity=crossed,
                detail=(
                    f"Peak {label} of {_fmt(peak, reading_key)} against a "
                    f"{crossed} line of {_fmt(limit, reading_key)}."
                ),
                reading=round(float(peak), 2),
                threshold=float(limit),
            )
        )

    if needs_daylight and daylight and daylight.get("sunset"):
        try:
            sunset = datetime.fromisoformat(daylight["sunset"])
            last_hour = datetime.fromisoformat(hours[-1]["time"])
            if last_hour >= sunset:
                penalty += 45
                reasons.append(
                    Reason(
                        code="after_sunset",
                        severity="blocker",
                        detail=(
                            f"The window runs past sunset at {sunset:%H:%M} and "
                            "you said the ground has no floodlights."
                        ),
                    )
                )
            elif (sunset - last_hour).total_seconds() < 3600:
                penalty += 18
                reasons.append(
                    Reason(
                        code="near_sunset",
                        severity="caution",
                        detail=(
                            "You get under an hour of light after that window; "
                            f"sunset is {sunset:%H:%M}."
                        ),
                    )
                )
        except (ValueError, KeyError, TypeError):
            pass

    if data_source.startswith("climatology"):
        reasons.append(
            Reason(
                code="beyond_forecast_range",
                severity="note",
                detail=(
                    "The forecast service could not be reached, so this falls back to seasonal "
                    "normals from past years. Treat it as typical conditions, not a forecast."
                    if data_source == "climatology_fallback"
                    else "That date is past the 16-day forecast horizon, so this uses seasonal "
                    "normals from past years, not a forecast."
                ),
            )
        )

    if any(r.severity == "blocker" for r in reasons):
        verdict: Verdict = "no_go"
    elif any(r.severity == "caution" for r in reasons):
        verdict = "caution"
    else:
        verdict = "go"

    if verdict == "go":
        reasons.append(
            Reason(
                "clear", "note", "Every reading in that window sits inside the comfortable band."
            )
        )

    return Assessment(
        verdict=verdict,
        score=max(0.0, 100.0 - penalty),
        reasons=reasons,
        window={
            "from": hours[0].get("time"),
            "to": hours[-1].get("time"),
            "hours_assessed": len(hours),
            "data_source": data_source,
        },
    )



WEEKEND_NUDGE = 6.0


def _is_weekend(iso: str) -> bool:
    try:
        return datetime.fromisoformat(iso).weekday() >= 5
    except (TypeError, ValueError):
        return False


def _overlaps(a: tuple[str, str] | None, b: tuple[str, str] | None) -> bool:
    """Whether two windows share any hour."""
    if not a or not b:
        return False
    return a[0] <= b[1] and b[0] <= a[1]


def rank_alternatives(
    candidates: list[dict[str, Any]],
    *,
    activity: str = "outdoor",
    needs_daylight: bool = False,
    limit: int = 3,
) -> list[dict[str, Any]]:
    """Score several candidate windows and return the best few.

    Each candidate is {"label": str, "hours": [...], "daylight": {...}}.
    """
    scored = []
    for candidate in candidates:
        assessment = assess_window(
            candidate.get("hours", []),
            activity=activity,
            daylight=candidate.get("daylight"),
            needs_daylight=needs_daylight,
            data_source=candidate.get("data_source", "forecast"),
        )
        hours = candidate.get("hours") or []
        starts = [h.get("time") for h in hours if h.get("time")]
        scored.append(
            {
                "label": candidate.get("label"),
                "verdict": assessment.verdict,
                "score": round(assessment.score, 1),
                "headline": assessment.reasons[0].detail if assessment.reasons else "",
                "window": assessment.window,
                "_span": (min(starts), max(starts)) if starts else None,
                "_weekend": _is_weekend(starts[0]) if starts else False,
            }
        )

    # A weekend nudge rather than a filter, so a decisively better weekday still
    # wins. Offering 6am Wednesday against a Saturday fixture is technically
    # correct and useless.
    for row in scored:
        row["_rank"] = row["score"] + (WEEKEND_NUDGE if row["_weekend"] else 0.0)
    scored.sort(key=lambda row: row["_rank"], reverse=True)

    # Greedy over the ranking, skipping anything overlapping a window already
    # taken. Three views of one morning, 06:00-09:00, 07:00-10:00 and 08:00-11:00,
    # is one option shown three times.
    chosen: list[dict[str, Any]] = []
    for row in scored:
        if any(_overlaps(row["_span"], taken["_span"]) for taken in chosen):
            continue
        chosen.append(row)
        if len(chosen) == limit:
            break

    return [{k: v for k, v in row.items() if not k.startswith("_")} for row in chosen]
