"""find_running_routes: real routes from the runner's door, ranked with safety in mind.

1. Find parks and 400m tracks near the start (OpenStreetMap via Overpass).
2. Route there and around them on foot (OSRM foot router) to get real distances.
3. Measure climb (Open-Meteo elevation) so intervals land on flat ground.
4. In NYC and Chicago, count reported outdoor violent crime along each route
   (city open data) relative to the area average, and name the hotspots.
"""

import json
import math
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

import requests

from tools.common import HTTP, M_PER_UNIT, ToolError, geocode, haversine_m

OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]
OSRM_FOOT_URL = "https://routing.openstreetmap.de/routed-foot/route/v1/foot/"
ELEVATION_URL = "https://api.open-meteo.com/v1/elevation"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# Violent crimes that happen outdoors: the ones that matter to someone running past.
CRIME_SOURCES = {
    "New York City": {
        "bbox": (40.49, -74.27, 40.92, -73.68),
        "url": "https://data.cityofnewyork.us/resource/5uac-w243.json",
        "label": "NYPD complaints (NYC Open Data): robbery, felony assault, sex crimes on streets/parks",
        "select": "latitude,longitude,cmplnt_fr_tm AS time",
        "where": (
            "within_circle(lat_lon, {lat}, {lon}, {radius}) AND cmplnt_fr_dt > '{since}' AND "
            "ofns_desc in('ROBBERY','FELONY ASSAULT','RAPE','SEX CRIMES') AND "
            "prem_typ_desc in('STREET','PARK/PLAYGROUND','OPEN AREAS (OPEN LOTS)','HIGHWAY/PARKWAY','BRIDGE')"
        ),
    },
    "Chicago": {
        "bbox": (41.64, -87.94, 42.03, -87.52),
        "url": "https://data.cityofchicago.org/resource/ijzp-q8t2.json",
        "label": "Chicago Police reports (Chicago Data Portal): robbery, assault, battery, sexual assault outdoors",
        "select": "latitude,longitude,date AS time",
        "where": (
            "within_circle(location, {lat}, {lon}, {radius}) AND date > '{since}' AND "
            "primary_type in('ROBBERY','ASSAULT','BATTERY','CRIMINAL SEXUAL ASSAULT') AND "
            "location_description in('STREET','SIDEWALK','PARK PROPERTY','ALLEY')"
        ),
    },
}

CORRIDOR_M = 100  # incidents this close to the path count against a route


def find_running_routes(
    start: str,
    distance: float,
    unit: str = "mi",
    run_type: str = "easy",
    time_of_day: str | None = None,
) -> str:
    if unit not in M_PER_UNIT:
        raise ToolError("unit must be 'mi' or 'km'.")
    target_m = float(distance) * M_PER_UNIT[unit]
    if not 800 <= target_m <= 50_000:
        raise ToolError(f"{distance} {unit} is outside what I can route (0.5-30 mi). Check the distance and unit.")
    run_hour = _hour(time_of_day) if time_of_day else None

    origin = geocode(start)
    radius = int(min(6000, max(1500, target_m * 0.45)))
    candidates = _nearby_places(origin, radius)
    if not candidates:
        raise ToolError(
            f"No parks or running tracks within {radius / 1000:.1f} km of {origin['name']}. "
            "Suggest a simple out-and-back on a main road with sidewalks, or ask for a different start."
        )
    picks = _pick_candidates(candidates, origin, target_m, run_type)

    with ThreadPoolExecutor(max_workers=6) as pool:
        crime_future = pool.submit(_crime_near, origin, radius + 500)
        sun_future = pool.submit(_sun_times, origin)
        routed = list(pool.map(lambda c: _route(origin, c), picks))
    routes = [r for r in routed if r]
    if not routes:
        raise ToolError("The OSRM walking router is unreachable right now, so I can't measure routes. Try again in a minute.")
    # Drop places the straight-line guess made look close but the real path doesn't
    # (fenced tracks, parks across a river with no bridge nearby).
    routes = [r for r in routes if 2 * r["approach_m"] <= target_m * 1.05] or routes

    _add_elevation(routes)
    crime, crime_source = crime_future.result()
    sunrise, sunset = sun_future.result()
    after_dark = run_hour is not None and sunrise is not None and not (sunrise <= run_hour < sunset)

    hotspots = _hotspots(crime) if crime is not None else []
    for r in routes:
        _fit_distance(r, target_m, unit)
        r["safety"] = _route_safety(r, crime, radius + 500, hotspots, run_hour) if crime is not None else None
        r["fit"] = _suitability(r, run_type)

    # Best routes first: suitability, then safety, then how close to the target distance.
    def rank(r):
        safety = r["safety"]["vs_area_average"] if r["safety"] else 1.0
        return -r["fit"]["score"] + 20 * min(safety, 3) + abs(r["total_m"] - target_m) / target_m * 30

    routes.sort(key=rank)

    tips = []
    if after_dark:
        tips.append(f"Running after dark (sunset {sunset:.0f}:00ish): wear lights, pick the route with the fewest incidents, stay on lit, busy paths, and tell someone your route.")
    if crime is None:
        tips.append("Crime data is only wired up for New York City and Chicago, so these routes are ranked on terrain and distance only. Stick to busy, well-lit paths.")

    return json.dumps({
        "start": {"name": origin["name"], "lat": round(origin["lat"], 5), "lon": round(origin["lon"], 5)},
        "target": f"{distance:g} {unit}",
        "run_type": run_type,
        "crime_data": crime_source or "not available for this city",
        "routes": [_public(r, unit) for r in routes[:3]],
        "hotspots": [{"lat": h["lat"], "lon": h["lon"], "incidents": h["count"]} for h in hotspots[:10]],
        "tips": tips,
    })


# --- Finding places ---


# (rounded lat, rounded lon, radius) -> places. Parks don't move.
_places_cache: dict[tuple, list[dict]] = {}


def _nearby_places(origin: dict, radius: int) -> list[dict]:
    key = (round(origin["lat"], 3), round(origin["lon"], 3), radius)
    if key not in _places_cache:
        _places_cache[key] = _parse_places(_overpass(origin, radius), origin)
    return _places_cache[key]


def _overpass(origin: dict, radius: int) -> dict:
    around = f"(around:{radius},{origin['lat']},{origin['lon']})"
    query = (
        f"[out:json][timeout:25];("
        f"way[leisure=track]{around};relation[leisure=track]{around};"
        f"way[leisure=park][name]{around};relation[leisure=park][name]{around};"
        f");out geom;"
    )
    # The public servers return 504 under load; it usually clears in seconds.
    for url in OVERPASS_URLS * 2:
        try:
            resp = HTTP.post(url, data={"data": query}, timeout=20)
            if resp.ok:
                return resp.json()
        except (requests.RequestException, ValueError):
            continue
    raise ToolError(
        "OpenStreetMap's Overpass servers are overloaded right now, so I can't look up parks and "
        "tracks. Tell the runner and offer to try again in a minute."
    )


def _parse_places(data: dict, origin: dict) -> list[dict]:
    places, parks = [], []
    for el in data["elements"]:
        tags = el.get("tags", {})
        if el["type"] == "way":
            rings = [el.get("geometry", [])]
        else:
            rings = [m.get("geometry", []) for m in el.get("members", []) if m.get("role") == "outer"]
        rings = [[(p["lat"], p["lon"]) for p in ring] for ring in rings if ring]
        coords = [c for ring in rings for c in ring]
        if len(coords) < 4:
            continue
        perimeter = sum(haversine_m(*a, *b) for ring in rings for a, b in zip(ring, ring[1:]))

        if tags.get("leisure") == "track":
            # A 400m track's outer edge measures ~400-480m. Short strips are sprint lanes.
            if el["type"] == "way" and coords[0] != coords[-1]:
                continue
            if not 330 <= perimeter <= 620 or tags.get("sport") not in (None, "running", "athletics", "multi"):
                continue
            kind, name = "track", tags.get("name")
        else:
            parks.append({"name": tags["name"], "lats": [c[0] for c in coords], "lons": [c[1] for c in coords]})
            if not 900 <= perimeter <= 25_000:
                continue  # pocket parks and playgrounds are too small to loop
            kind, name = "park", tags["name"]

        nearest = min(coords, key=lambda c: haversine_m(origin["lat"], origin["lon"], *c))
        places.append({
            "kind": kind,
            "name": name,
            "lit": tags.get("lit") == "yes",
            "coords": coords,
            "perimeter": perimeter,
            "nearest": nearest,
            "straight_m": haversine_m(origin["lat"], origin["lon"], *nearest),
        })

    # Most tracks are unnamed; name them after the park they sit in.
    for p in places:
        if p["kind"] == "track" and not p["name"]:
            lat, lon = p["nearest"]
            home = next((k for k in parks if min(k["lats"]) <= lat <= max(k["lats"]) and min(k["lons"]) <= lon <= max(k["lons"])), None)
            p["name"] = f"{home['name']} track" if home else "400m running track"
    return places


def _pick_candidates(places: list[dict], origin: dict, target_m: float, run_type: str) -> list[dict]:
    """Up to 4 places worth routing: close enough, big enough to loop, no duplicates."""
    def score(p):
        approach = p["straight_m"] * 1.3 * 2
        remaining = max(target_m - approach, 1)
        if p["kind"] == "track":
            # The jog to the track doubles as warm-up and cool-down.
            return (2.0 if run_type == "intervals" else 0.2) - approach / target_m
        lap_cover = min(p["perimeter"], remaining) / remaining
        weight = 1.5 if run_type == "long" else 1.0
        return weight * lap_cover - 0.6 * approach / target_m

    reachable = [p for p in places if p["straight_m"] * 1.25 * 2 <= target_m * (1.0 if p["kind"] == "track" else 0.9)]
    picks, names = [], set()
    tracks_taken = 0
    for p in sorted(reachable or places, key=score, reverse=True):
        key = p["name"].lower()
        if key in names or (p["kind"] == "track" and tracks_taken >= (2 if run_type == "intervals" else 1)):
            continue
        names.add(key)
        tracks_taken += p["kind"] == "track"
        picks.append(p)
        if len(picks) == 4:
            break
    return picks


# --- Routing ---


def _osrm(points: list[tuple[float, float]]) -> dict | None:
    coords = ";".join(f"{lon:.6f},{lat:.6f}" for lat, lon in points)
    try:
        resp = HTTP.get(
            OSRM_FOOT_URL + coords,
            params={"overview": "full", "geometries": "geojson", "steps": "true"},
            timeout=15,
        ).json()
        return resp["routes"][0] if resp.get("code") == "Ok" else None
    except (requests.RequestException, ValueError, KeyError, IndexError):
        return None


def _route(origin: dict, place: dict) -> dict | None:
    """Route from the start to the place, then one lap of it."""
    start = (origin["lat"], origin["lon"])
    approach = _osrm([start, place["nearest"]])
    if not approach:
        return None

    if place["kind"] == "track":
        # The outline is the outer edge; a lap in lane 1 is 400m by definition.
        loop_coords, lap_m, loop_steps = place["coords"], 400.0, []
    else:
        lats = [c[0] for c in place["coords"]]
        lons = [c[1] for c in place["coords"]]
        corners = [
            place["coords"][lats.index(max(lats))],  # north
            place["coords"][lons.index(max(lons))],  # east
            place["coords"][lats.index(min(lats))],  # south
            place["coords"][lons.index(min(lons))],  # west
        ]
        # Begin the lap at the corner closest to where the runner arrives.
        first = min(range(4), key=lambda i: haversine_m(*place["nearest"], *corners[i]))
        ring = corners[first:] + corners[:first]
        ring = [c for i, c in enumerate(ring) if i == 0 or haversine_m(*c, *ring[i - 1]) > 60]
        loop = _osrm([place["nearest"]] + ring + [place["nearest"]])
        if not loop:
            return None
        loop_coords = [(lat, lon) for lon, lat in loop["geometry"]["coordinates"]]
        lap_m, loop_steps = loop["distance"], _steps(loop)

    return {
        "place": place,
        "approach_m": approach["distance"],
        "approach_coords": [(lat, lon) for lon, lat in approach["geometry"]["coordinates"]],
        "lap_m": lap_m,
        "loop_coords": loop_coords,
        "steps": _steps(approach) + loop_steps,
    }


def _steps(route: dict) -> list[dict]:
    return [
        {"name": s["name"], "lat": s["maneuver"]["location"][1], "lon": s["maneuver"]["location"][0]}
        for leg in route["legs"]
        for s in leg["steps"]
        if s.get("name")
    ]


def _fit_distance(r: dict, target_m: float, unit: str) -> None:
    """How many laps make the distance work, counting the run there and back.

    If one lap is longer than what's left (a 6 mi trail on a 3 mi run), run part
    of it and turn around instead.
    """
    out_back = 2 * r["approach_m"]
    left = target_m - out_back
    if r["place"]["kind"] == "park" and left < r["lap_m"] * 0.75:
        r["laps"] = 0
        r["turnaround_m"] = max(left / 2, 0)
        r["total_m"] = out_back + 2 * r["turnaround_m"]
        r["loop_coords"] = _cut(r["loop_coords"], r["turnaround_m"])
    else:
        exact = left / r["lap_m"]
        r["laps"] = max(0, round(exact)) if r["place"]["kind"] == "track" else max(1, round(exact))
        r["turnaround_m"] = None
        r["total_m"] = out_back + r["laps"] * r["lap_m"]

    diff = (target_m - r["total_m"]) / M_PER_UNIT[unit]
    if abs(diff) >= 0.15:
        verb = "extend" if diff > 0 else "shorten"
        r["adjust"] = f"{verb} the run there and back by {abs(diff) / 2:.1f} {unit} each way to hit the target"
    else:
        r["adjust"] = None


def _cut(coords: list, meters: float) -> list:
    """The first `meters` of a path."""
    out, done = coords[:1], 0.0
    for a, b in zip(coords, coords[1:]):
        done += haversine_m(*a, *b)
        out.append(b)
        if done >= meters:
            break
    return out


# --- Elevation ---


def _add_elevation(routes: list[dict]) -> None:
    """Climb per route from a 90m elevation model: enough to tell flat from hilly."""
    samples = []
    for r in routes:
        samples.append(_sample(r["approach_coords"], 12) + _sample(r["loop_coords"], 20))
    flat = [c for s in samples for c in s]
    try:
        elevations = []
        for i in range(0, len(flat), 100):  # the API takes 100 points per call
            batch = flat[i : i + 100]
            elevations += HTTP.get(
                ELEVATION_URL,
                params={"latitude": ",".join(f"{c[0]:.5f}" for c in batch), "longitude": ",".join(f"{c[1]:.5f}" for c in batch)},
                timeout=10,
            ).json()["elevation"]
    except (requests.RequestException, ValueError, KeyError):
        for r in routes:
            r["climb_m_per_lap"] = None
        return

    i = 0
    for r, s in zip(routes, samples):
        heights = elevations[i : i + len(s)]
        i += len(s)
        loop_heights = heights[12:] if len(s) > 12 else heights
        climb = round(sum(max(0, b - a) for a, b in zip(loop_heights, loop_heights[1:])))
        r["climb_m_per_lap"] = 0 if r["place"]["kind"] == "track" else climb


def _sample(coords: list, n: int) -> list:
    if len(coords) <= n:
        return list(coords)
    step = (len(coords) - 1) / (n - 1)
    return [coords[round(i * step)] for i in range(n)]


# --- Safety ---


def _crime_near(origin: dict, radius: int) -> tuple[list[dict] | None, str | None]:
    """Recent outdoor violent incidents around the start, or (None, None) if the
    city has no open data source wired up."""
    for city, src in CRIME_SOURCES.items():
        s, w, n, e = src["bbox"]
        if s <= origin["lat"] <= n and w <= origin["lon"] <= e:
            break
    else:
        return None, None

    since = (date.today() - timedelta(days=365)).isoformat()
    try:
        rows = HTTP.get(
            src["url"],
            params={
                "$select": src["select"],
                "$where": src["where"].format(lat=origin["lat"], lon=origin["lon"], radius=radius, since=since),
                "$limit": 10000,
            },
            timeout=25,
        ).json()
        incidents = [
            # NYC gives "19:45:00", Chicago "2026-06-01T19:45:00.000"
            {"lat": float(r["latitude"]), "lon": float(r["longitude"]), "hour": _hour(r.get("time", "").split("T")[-1])}
            for r in rows
            if r.get("latitude") and r.get("longitude")
        ]
    except (requests.RequestException, ValueError, KeyError, TypeError):
        return None, f"{city} crime data was unreachable, so safety isn't scored this time"
    return incidents, f"{src['label']}, last 12 months available"


def _hotspots(incidents: list[dict]) -> list[dict]:
    """~200m grid cells with unusually many incidents."""
    cells = Counter((round(i["lat"] / 0.002), round(i["lon"] / 0.0025)) for i in incidents)
    if not cells:
        return []
    threshold = max(4, 3 * sum(cells.values()) / len(cells))
    return [
        {"lat": round(la * 0.002, 4), "lon": round(lo * 0.0025, 4), "count": n}
        for (la, lo), n in cells.most_common()
        if n >= threshold
    ]


def _route_safety(r: dict, incidents: list[dict], radius: int, hotspots: list[dict], run_hour: int | None) -> dict:
    path = _densify(r["approach_coords"] + r["loop_coords"], 40)
    path_km = (r["approach_m"] + r["lap_m"]) / 1000

    # Grid lookup so each incident is checked against nearby path points only.
    grid = {}
    for lat, lon in path:
        grid.setdefault((round(lat / 0.001), round(lon / 0.0013)), []).append((lat, lon))

    def near_path(lat, lon, limit):
        gx, gy = round(lat / 0.001), round(lon / 0.0013)
        return any(
            haversine_m(lat, lon, *p) <= limit
            for dx in (-1, 0, 1)
            for dy in (-1, 0, 1)
            for p in grid.get((gx + dx, gy + dy), [])
        )

    hits = [i for i in incidents if near_path(i["lat"], i["lon"], CORRIDOR_M)]

    # What a random path this long would expect, from the area's incident density.
    area_km2 = math.pi * (radius / 1000) ** 2
    expected = len(incidents) / area_km2 * path_km * (2 * CORRIDOR_M / 1000)
    ratio = round(len(hits) / expected, 2) if expected else 0
    level = "lower than the area average" if ratio < 0.75 else "about the area average" if ratio <= 1.33 else "higher than the area average"

    passes = Counter()
    for h in hotspots:
        if near_path(h["lat"], h["lon"], 150):
            street = min(r["steps"], key=lambda s: haversine_m(h["lat"], h["lon"], s["lat"], s["lon"]), default=None)
            near = street["name"] if street and haversine_m(h["lat"], h["lon"], street["lat"], street["lon"]) < 400 else r["place"]["name"]
            passes[near] += h["count"]

    safety = {
        "incidents_within_100m": len(hits),
        "vs_area_average": ratio,
        "summary": f"Reported incidents along this route are {level}.",
        "hotspots_on_route": [{"near": n, "incidents": c} for n, c in passes.most_common(4)],
    }
    if run_hour is not None and hits:
        same_time = sum(1 for i in hits if i["hour"] is not None and min(abs(i["hour"] - run_hour), 24 - abs(i["hour"] - run_hour)) <= 2)
        safety["within_2h_of_run_time"] = same_time
    return safety


def _densify(coords: list, step_m: float) -> list:
    out = []
    for a, b in zip(coords, coords[1:]):
        n = max(1, int(haversine_m(*a, *b) // step_m))
        out += [(a[0] + (b[0] - a[0]) * k / n, a[1] + (b[1] - a[1]) * k / n) for k in range(n)]
    return out + coords[-1:]


# --- Presentation ---


def _suitability(r: dict, run_type: str) -> dict:
    kind = r["place"]["kind"]
    climb = r.get("climb_m_per_lap")
    gain_per_km = climb / (r["lap_m"] / 1000) if climb is not None else None
    terrain = "unknown" if gain_per_km is None else "flat" if gain_per_km < 8 else "rolling" if gain_per_km < 20 else "hilly"
    if kind == "track":
        terrain = "flat"  # the 90m elevation grid can't resolve a track; they're flat

    score, why = 50, []
    if run_type == "intervals":
        if kind == "track":
            score, why = 95, ["measured 400m track: perfect for repeats", "the jog there is your warm-up"]
        elif terrain == "flat":
            score, why = 75, ["flat loop with no traffic lights inside the park"]
        else:
            score, why = 40, [f"{terrain} loop: repeats will be uneven"]
    elif run_type == "long":
        score = 60 + min(35, r["lap_m"] / 300)
        why = ["big loop: fewer repeats of the same scenery"] if r["lap_m"] > 4000 else ["several laps needed"]
    elif run_type == "tempo":
        score = 85 if terrain == "flat" and kind == "park" else 65
        why = [f"{terrain} and uninterrupted"] if kind == "park" else ["track works but laps get dull for a tempo"]
    else:
        score = 70 + (10 if kind == "park" else 0) - r["approach_m"] / 400
        why = ["green, car-free loop"] if kind == "park" else ["predictable, measured loop"]
    return {"score": round(score), "terrain": terrain, "why": why}


def _public(r: dict, unit: str) -> dict:
    per = M_PER_UNIT[unit]
    place = r["place"]
    bearing = _compass(r["approach_coords"][0], place["nearest"])
    return {
        "name": f"{place['name']} {'laps' if place['kind'] == 'track' else 'out-and-back' if r['turnaround_m'] is not None else 'loop'}",
        "kind": place["kind"],
        "directions": (
            f"Run {r['approach_m'] / per:.2f} {unit} {bearing} to {place['name']}, "
            + (
                f"follow its path {r['turnaround_m'] / per:.2f} {unit}, turn around, and run back the same way."
                if r["turnaround_m"] is not None
                else f"do {r['laps']} lap{'s' if r['laps'] != 1 else ''} ({r['lap_m'] / per:.2f} {unit} each), then run back the same way."
            )
        ),
        "total": f"{r['total_m'] / per:.1f} {unit}",
        "adjust": r["adjust"],
        "terrain": r["fit"]["terrain"],
        "climb_per_lap_m": r.get("climb_m_per_lap"),
        "why": r["fit"]["why"],
        "safety": r["safety"],
        "streets": list(dict.fromkeys(s["name"] for s in r["steps"]))[:6],
        "approach_path": [[round(a, 5), round(b, 5)] for a, b in _sample(r["approach_coords"], 30)],
        "loop_path": [[round(a, 5), round(b, 5)] for a, b in _sample(r["loop_coords"], 50)],
    }


def _compass(a, b) -> str:
    dy, dx = b[0] - a[0], (b[1] - a[1]) * math.cos(math.radians(a[0]))
    names = ["north", "northeast", "east", "southeast", "south", "southwest", "west", "northwest"]
    return names[round(math.degrees(math.atan2(dx, dy)) / 45) % 8]


def _sun_times(origin: dict) -> tuple[float | None, float | None]:
    try:
        daily = HTTP.get(
            FORECAST_URL,
            params={"latitude": origin["lat"], "longitude": origin["lon"], "daily": "sunrise,sunset", "timezone": "auto", "forecast_days": 1},
            timeout=10,
        ).json()["daily"]
        return _hour(daily["sunrise"][0][11:16]), _hour(daily["sunset"][0][11:16])
    except (requests.RequestException, ValueError, KeyError, IndexError):
        return None, None


def _hour(clock: str) -> float | None:
    try:
        h, m = clock.strip()[:5].split(":")
        return int(h) + int(m) / 60
    except (ValueError, AttributeError):
        return None


SCHEMA = {
    "type": "function",
    "function": {
        "name": "find_running_routes",
        "description": (
            "Suggest up to 3 real running routes from a starting point: run to a nearby park or "
            "400m track, do laps, run back. Distances come from a walking router, climb from "
            "elevation data. Picks tracks or flat loops for intervals and big loops for long runs. "
            "In New York City and Chicago it also counts reported outdoor violent crime along each "
            "route versus the area average and names hotspot streets to avoid. Call it when the "
            "runner asks where to run or wants a route for today's workout."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "start": {"type": "string", "description": "Where the run starts: address, intersection, landmark, or neighborhood with city, e.g. 'Columbia University, New York'."},
                "distance": {"type": "number", "description": "Total run distance in `unit`, including getting there and back."},
                "unit": {"type": "string", "enum": ["mi", "km"], "description": "Default 'mi'."},
                "run_type": {"type": "string", "enum": ["easy", "recovery", "long", "tempo", "intervals", "race"], "description": "The workout the runner named (or today's planned run type). If they didn't name one, use 'easy'."},
                "time_of_day": {"type": "string", "description": "Planned start time, 24-hour HH:MM. Enables after-dark warnings and time-of-day crime counts."},
            },
            "required": ["start", "distance"],
        },
    },
}
