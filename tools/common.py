"""Helpers shared by every tool: HTTP, geocoding, pace math, and the error type."""

import math
import re
import time
import unicodedata

import requests

# Nominatim and Overpass ask every client to identify itself.
HTTP = requests.Session()
HTTP.headers["User-Agent"] = "Cadence-running-agent/1.0 (Columbia class project)"

KM_PER_MI = 1.609344
M_PER_UNIT = {"mi": 1609.344, "km": 1000.0}

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
NOMINATIM_REVERSE_URL = "https://nominatim.openstreetmap.org/reverse"
OPEN_METEO_GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
PHOTON_SEARCH_URL = "https://photon.komoot.io/api/"


class ToolError(Exception):
    """A problem the model can fix, e.g. a bad argument or missing plan.

    run_tool() turns it into {"error": message}, so write the message as an
    instruction to the model: what went wrong and what to do next.
    """


def geocode(place: str, near: dict | None = None) -> dict:
    """Place name -> {name, lat, lon, rank, type, kind}, picking the best name match from Nominatim and Photon.

    `rank`: 4 country ... 30 building/corner. `near`: search ~50 km around it, closest match wins.
    """
    if CROSS_STREETS.match(place):  # a corner beats a match on just one of the streets
        corner = _cross_streets(place)
        if corner:
            return corner
    candidates = _nominatim_search(place, near) + _photon_search(place, near)
    if candidates:
        best = max(_name_match(place, c) for c in candidates)
        if best[1] == 0:
            return candidates[0]  # an address or nickname ("The Met"): trust Nominatim
        if best[1] < 0.5:  # only part of the name matches: a different place
            raise ToolError(
                f"Couldn't find '{place}' on the map; the closest name was '{candidates[0]['name']}', which isn't it. "
                "Ask the runner for another name for it (it may go by a local name), a nearby landmark, "
                "or cross streets with the city. Don't substitute one yourself."
            )
        tied = [c for c in candidates if _name_match(place, c) == best]
        if near:
            return min(tied, key=lambda c: haversine_m(near["lat"], near["lon"], c["lat"], c["lon"]))
        return tied[0]  # Nominatim's ranking first

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
    return {"name": hit["name"], "lat": hit["latitude"], "lon": hit["longitude"], "rank": None, "type": "city", "kind": "place"}


def street_at(lat: float, lon: float) -> str | None:
    """The street and neighborhood at a point, e.g. "South Street, Whitehall"."""
    try:
        address = HTTP.get(NOMINATIM_REVERSE_URL, params={"lat": lat, "lon": lon, "format": "json", "zoom": 17},
                           timeout=10).json()["address"]
    except (requests.RequestException, ValueError, KeyError):
        return None
    parts = [address.get("road"), address.get("neighbourhood") or address.get("suburb")]
    return ", ".join(p for p in parts if p) or None


def plain(text: str) -> str:
    """Lowercase, no accents or apostrophes: "Joe's Café" -> "joes cafe"."""
    text = unicodedata.normalize("NFKD", text.lower().replace("'", "").replace("’", ""))
    return "".join(c for c in text if not unicodedata.combining(c))


# Spelled-out words -> short form, so "Ave" matches "Avenue" and "St" matches "Street" or "Saint". "and" is dropped ("&").
SHORT = {"avenue": "ave", "street": "st", "saint": "st", "road": "rd", "boulevard": "blvd", "drive": "dr",
         "place": "pl", "lane": "ln", "parkway": "pkwy", "square": "sq", "mount": "mt", "fort": "ft",
         "west": "w", "east": "e", "north": "n", "south": "s", "and": "&"}


def _words(text: str) -> list[str]:
    return [SHORT.get(w, w) for w in re.findall(r"[a-z0-9]+", plain(text)) if w != "and"]


def _name_match(query: str, candidate: dict) -> tuple[float, float]:
    """(score, share of asked-for words in the name). +1 per matching word, -0.1 per extra; initials count ("MSG")."""
    want = set(_words(query.split(",")[0]))
    name = _words(candidate["label"])
    if len(name) >= 2 and "".join(w[0] for w in name) in want:
        return float(len(want)), 1.0
    hit = len(want & set(name))
    return hit - 0.1 * len(set(name) - want), hit / max(len(want), 1)


def _label(display_name: str) -> str:
    """The place's own name from a Nominatim address: "350, 5th Avenue, ..." -> "350 5th Avenue"."""
    parts = [p.strip() for p in display_name.split(",")]
    return " ".join(parts[:2]) if parts[0].isdigit() and len(parts) > 1 else parts[0]


def _nominatim_search(place: str, near: dict | None) -> list:
    params = {"q": place, "format": "json", "limit": 10}
    if near:
        lat, lon = near["lat"], near["lon"]
        params |= {"viewbox": f"{lon - 0.5},{lat + 0.5},{lon + 0.5},{lat - 0.5}", "bounded": 1}
    for attempt in range(2):
        try:
            resp = HTTP.get(NOMINATIM_URL, params=params, timeout=10)
            if resp.status_code == 429 and attempt == 0:
                time.sleep(1.5)  # Nominatim allows ~1 request a second
                continue
            return [{
                "label": _label(h["display_name"]),
                "name": h["display_name"].split(",")[0] + _city_suffix(h),
                "lat": float(h["lat"]), "lon": float(h["lon"]),
                "rank": h.get("place_rank"), "type": h.get("type"), "kind": h.get("class"),
            } for h in resp.json()]
        except (requests.RequestException, ValueError, KeyError):
            return []
    return []


# Photon has no place_rank; these stand in for it.
PHOTON_RANK = {"country": 4, "state": 8, "city": 16, "town": 16, "village": 18, "suburb": 20,
               "quarter": 20, "neighbourhood": 20, "borough": 18, "district": 18}


def _photon_search(place: str, near: dict | None) -> list:
    """Photon's matches: a second opinion that also lists every branch of a chain."""
    params = {"q": place, "limit": 10}
    if near:
        params |= {"lat": near["lat"], "lon": near["lon"]}
    try:
        features = HTTP.get(PHOTON_SEARCH_URL, params=params, timeout=10).json()["features"]
    except (requests.RequestException, ValueError, KeyError):
        return []
    out = []
    for f in features:
        props, (lon, lat) = f["properties"], f["geometry"]["coordinates"]
        if not props.get("name") or (near and haversine_m(near["lat"], near["lon"], lat, lon) > 50_000):
            continue
        where = props.get("city") or props.get("county") or props.get("state")
        rank = PHOTON_RANK.get(props.get("osm_value"), 30) if props.get("osm_key") == "place" else 30
        out.append({"label": props["name"], "name": props["name"] + (f", {where}" if where else ""), "lat": lat, "lon": lon,
                    "rank": rank, "type": props.get("osm_value"), "kind": props.get("osm_key")})
    return out


# "Amsterdam Ave & W 119th St, New York", "119th and Amsterdam, NYC"
CROSS_STREETS = re.compile(r"^\s*([^,]+?)\s*(?:&|/|\s(?:and|at)\s)\s*([^,]+?)\s*,\s*(.+)$", re.I)
# Words that differ between how people say a street and how OSM names it.
STREET_WORDS = {"ave", "avenue", "st", "street", "rd", "road", "blvd", "boulevard", "pl", "place",
                "dr", "drive", "w", "west", "e", "east", "n", "north", "s", "south", "the"}


def _cross_streets(place: str) -> dict | None:
    """Where two streets cross (Nominatim can't find corners)."""
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
            "rank": 30, "type": "intersection", "kind": "highway"}


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
    """Overpass results, or None if every mirror is down."""
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
