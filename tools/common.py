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


def geocode(place: str, near: dict | None = None) -> dict:
    """Turn a neighborhood, address, landmark, or cross streets into {name, lat, lon, rank, type}.

    `rank` is Nominatim's place_rank, how specific the match is: 4 country,
    8 state, ~10-16 city, ~19 neighborhood, 30 a building, landmark, or corner.
    None when the city-only fallback answered. `type` is the OSM type, e.g. "park".
    `near` ({lat, lon}) limits the search to ~20 km around that point, so
    "Central Park" means the one near the runner.
    """
    params = {"q": place, "format": "json", "limit": 1}
    if near:
        lat, lon = near["lat"], near["lon"]
        params |= {"viewbox": f"{lon - 0.2},{lat + 0.2},{lon + 0.2},{lat - 0.2}", "bounded": 1}
    try:
        hits = HTTP.get(NOMINATIM_URL, params=params, timeout=10).json()
        if hits:
            return {
                "name": hits[0]["display_name"].split(",")[0] + _city_suffix(hits[0]),
                "lat": float(hits[0]["lat"]),
                "lon": float(hits[0]["lon"]),
                "rank": hits[0].get("place_rank"),
                "type": hits[0].get("type"),
            }
    except (requests.RequestException, ValueError):
        pass  # Nominatim is rate limited; fall back below

    corner = _cross_streets(place)
    if corner:
        return corner

    not_found = ToolError(
        f"Couldn't find '{place}' on the map. Ask the runner for a more specific place "
        "(a nearby address, landmark, or cross streets with the city). Don't substitute one yourself."
    )
    if near:
        raise not_found  # Open-Meteo only knows cities, not places within one
    try:
        city = place.split(",")[0]
        hits = HTTP.get(OPEN_METEO_GEOCODE_URL, params={"name": city, "count": 1}, timeout=10).json()
    except requests.RequestException as e:
        raise ToolError(f"Geocoding services are unreachable ({e}). Try again in a moment.")
    if not hits.get("results"):
        raise not_found
    hit = hits["results"][0]
    return {"name": hit["name"], "lat": hit["latitude"], "lon": hit["longitude"], "rank": None, "type": "city"}


# "Amsterdam Ave & W 119th St, New York", "119th and Amsterdam, NYC"
CROSS_STREETS = re.compile(r"^\s*([^,]+?)\s*(?:&|/|\s(?:and|at)\s)\s*([^,]+?)\s*,\s*(.+)$", re.I)
# Words that differ between how people say a street and how OSM names it.
STREET_WORDS = {"ave", "avenue", "st", "street", "rd", "road", "blvd", "boulevard", "pl", "place",
                "dr", "drive", "w", "west", "e", "east", "n", "north", "s", "south", "the"}


def _cross_streets(place: str) -> dict | None:
    """Where two streets cross. Nominatim can't find corners, so ask OpenStreetMap directly."""
    match = CROSS_STREETS.match(place)
    if not match:
        return None
    a, b, city = match.groups()
    try:
        center = geocode(city)
    except ToolError:
        return None
    around = f"(around:25000,{center['lat']},{center['lon']})"
    nodes = overpass(
        f'[out:json][timeout:20];'
        f'way[highway][name~"{_street_pattern(a)}",i]{around}->.a;'
        f'way[highway][name~"{_street_pattern(b)}",i]{around}->.b;'
        f'node(w.a)(w.b);out 1;'
    )
    if not nodes:
        return None
    return {"name": f"{a} & {b}, {city}", "lat": nodes[0]["lat"], "lon": nodes[0]["lon"],
            "rank": 30, "type": "intersection"}


def _street_pattern(street: str) -> str:
    """A regex for OSM street names: "W 119th St" -> matches "West 119th Street"."""
    words = [w for w in re.findall(r"[a-z0-9]+", street.lower()) if w not in STREET_WORDS]
    for w in words:
        number = re.match(r"(\d+)(st|nd|rd|th)?$", w)
        if number:
            return f"(^|[^0-9]){number.group(1)}(st|nd|rd|th)?([^0-9]|$)"
    return " ".join(words) or "^$"


OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]
_OVERPASS_CACHE: dict = {}


def overpass(query: str) -> list | None:
    """Overpass results, or None if every mirror is down (they all 504 under load at once)."""
    if query not in _OVERPASS_CACHE:
        for url in OVERPASS_URLS:
            try:
                resp = HTTP.post(url, data={"data": query}, timeout=12)
                if resp.ok:
                    _OVERPASS_CACHE[query] = resp.json()["elements"]
                    break
            except (requests.RequestException, ValueError, KeyError):
                continue
    return _OVERPASS_CACHE.get(query)


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
