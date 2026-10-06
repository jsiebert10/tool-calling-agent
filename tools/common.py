"""Helpers shared by every tool: HTTP, geocoding, pace math, and the error type."""

import math
import re

import requests

# Nominatim and Overpass ask every client to identify itself.
HTTP = requests.Session()
HTTP.headers["User-Agent"] = "Cadence-running-agent/1.0 (Columbia class project)"

KM_PER_MI = 1.609344
M_PER_UNIT = {"mi": 1609.344, "km": 1000.0}

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
OPEN_METEO_GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"


class ToolError(Exception):
    """A problem the model can fix, e.g. a bad argument or missing plan.

    run_tool() turns it into {"error": message}, so write the message as an
    instruction to the model: what went wrong and what to do next.
    """


def geocode(place: str) -> dict:
    """Turn a neighborhood, address, or city into {name, lat, lon}."""
    try:
        hits = HTTP.get(
            NOMINATIM_URL, params={"q": place, "format": "json", "limit": 1}, timeout=10
        ).json()
        if hits:
            return {
                "name": hits[0]["display_name"].split(",")[0] + _city_suffix(hits[0]),
                "lat": float(hits[0]["lat"]),
                "lon": float(hits[0]["lon"]),
            }
    except (requests.RequestException, ValueError):
        pass  # Nominatim is rate limited; fall back to Open-Meteo's city search

    try:
        city = place.split(",")[0]
        hits = HTTP.get(OPEN_METEO_GEOCODE_URL, params={"name": city, "count": 1}, timeout=10).json()
    except requests.RequestException as e:
        raise ToolError(f"Geocoding services are unreachable ({e}). Try again in a moment.")
    if not hits.get("results"):
        raise ToolError(
            f"Couldn't find a place called '{place}'. Ask the runner for a more specific "
            "location, like 'Morningside Heights, New York' or a street address."
        )
    hit = hits["results"][0]
    return {"name": hit["name"], "lat": hit["latitude"], "lon": hit["longitude"]}


def _city_suffix(hit: dict) -> str:
    parts = [p.strip() for p in hit["display_name"].split(",")]
    return f", {parts[-3]}" if len(parts) >= 4 else ""


def parse_pace(pace: str, unit: str) -> int:
    """'8:30', '8:30/mi', '5:15 min/km' or '8.5' -> seconds per unit."""
    text = str(pace).strip().lower()
    match = re.match(r"^(\d{1,2}):(\d{2})", text)
    if match:
        seconds = int(match.group(1)) * 60 + int(match.group(2))
    else:
        try:
            seconds = round(float(re.match(r"^[\d.]+", text).group(0)) * 60)
        except (AttributeError, ValueError):
            raise ToolError(f"Couldn't read the pace '{pace}'. Pass it as minutes:seconds per {unit}, e.g. '8:30'.")

    low, high = (180, 1200) if unit == "mi" else (110, 750)
    if not low <= seconds <= high:
        raise ToolError(
            f"A pace of {fmt_pace(seconds)}/{unit} is outside the range of running paces "
            f"({fmt_pace(low)}-{fmt_pace(high)}/{unit}). Check whether the runner meant the other unit."
        )
    return seconds


def fmt_pace(seconds: float) -> str:
    seconds = round(seconds)
    return f"{seconds // 60}:{seconds % 60:02d}"


def fmt_minutes(minutes: float) -> str:
    return f"{int(minutes // 60)}h {round(minutes % 60):02d}m" if minutes >= 60 else f"{round(minutes)} min"


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371000 * math.asin(math.sqrt(a))
