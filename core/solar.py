"""Sunrise and sunset, computed locally.

Daylight used to come from the forecast payload, which meant it silently
disappeared whenever the forecast did: past the sixteen day horizon, or during an
upstream outage. The floodlights check is guarded on having a sunset, so an unlit
ground at 17:00 came back as a go.

Sunset is astronomy, not weather. It does not degrade with distance and it does
not need a network, so it is computed here with the NOAA solar position
algorithm and the feasibility tools always have it.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta

# The centre of the sun sits 0.833 degrees below the horizon at sunrise, which
# accounts for atmospheric refraction and the sun's own radius.
ZENITH_DEGREES = 90.833


def _fractional_year(day_of_year: int) -> float:
    return 2 * math.pi / 365.0 * (day_of_year - 1 + 0.5)


def _equation_of_time(gamma: float) -> float:
    """Minutes by which a sundial leads the clock."""
    return 229.18 * (
        0.000075
        + 0.001868 * math.cos(gamma)
        - 0.032077 * math.sin(gamma)
        - 0.014615 * math.cos(2 * gamma)
        - 0.040849 * math.sin(2 * gamma)
    )


def _declination(gamma: float) -> float:
    """Solar declination in radians."""
    return (
        0.006918
        - 0.399912 * math.cos(gamma)
        + 0.070257 * math.sin(gamma)
        - 0.006758 * math.cos(2 * gamma)
        + 0.000907 * math.sin(2 * gamma)
        - 0.002697 * math.cos(3 * gamma)
        + 0.001480 * math.sin(3 * gamma)
    )


def daylight(
    latitude: float, longitude: float, day: date, utc_offset_seconds: int | None = None
) -> dict[str, str] | None:
    """Sunrise and sunset as local ISO timestamps, or None where the sun does not set.

    utc_offset_seconds comes from the weather payload where it is available.
    Falling back to longitude / 15 cannot express a half hour zone, so India would
    land thirty minutes out.

    Returns None inside the polar circles when the sun stays up or stays down for
    the whole day. That is a real answer, not a failure, and the caller should say
    so rather than invent a sunset.
    """
    gamma = _fractional_year(day.timetuple().tm_yday)
    eqtime = _equation_of_time(gamma)
    decl = _declination(gamma)
    lat = math.radians(latitude)

    cos_ha = math.cos(math.radians(ZENITH_DEGREES)) / (
        math.cos(lat) * math.cos(decl)
    ) - math.tan(lat) * math.tan(decl)
    if not -1.0 <= cos_ha <= 1.0:
        return None

    hour_angle = math.degrees(math.acos(cos_ha))
    sunrise_utc = 720 - 4 * (longitude + hour_angle) - eqtime
    sunset_utc = 720 - 4 * (longitude - hour_angle) - eqtime

    offset = (
        utc_offset_seconds / 60.0
        if utc_offset_seconds is not None
        else round(longitude / 15.0) * 60.0
    )
    midnight = datetime.combine(day, datetime.min.time())

    def stamp(minutes_utc: float) -> str:
        return (midnight + timedelta(minutes=minutes_utc + offset)).isoformat(timespec="minutes")

    return {"sunrise": stamp(sunrise_utc), "sunset": stamp(sunset_utc)}
