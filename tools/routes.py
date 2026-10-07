"""find_running_routes: an out-and-back that turns around at half the distance.

1. Geocode the start.
2. Head toward the place the runner named, or else pick a named park about half
   the run away (a landmark or food spot if no park works). Places come from
   OpenStreetMap via Overpass, or Photon when Overpass is down.
3. Get the real walking path there (OSRM foot router). If our pick is closer
   than halfway, keep going past it in the same direction; if the runner's pick
   is, run laps there. Turn around at half the distance and run back the same way.
"""

import json
import math

import requests

from tools.common import HTTP, M_PER_UNIT, ToolError, geocode, haversine_m, overpass

PHOTON_URL = "https://photon.komoot.io/reverse"
OSRM_FOOT_URL = "https://routing.openstreetmap.de/routed-foot/route/v1/foot/"

DETOUR = 1.25  # streets are ~25% longer than a straight line
_CACHE: dict = {}  # Photon searches, so repeat requests for the same area skip the network


def find_running_routes(start: str, distance: float, unit: str = "mi", toward: str | None = None) -> str:
    if unit not in M_PER_UNIT:
        raise ToolError("unit must be 'mi' or 'km'.")
    target_m = float(distance) * M_PER_UNIT[unit]
    if not 800 <= target_m <= 50_000:
        raise ToolError(f"{distance} {unit} is outside what I can route (0.5-30 mi). Check the distance and unit.")

    origin = _start_point(start)
    half_m = target_m / 2
    want_m = half_m / DETOUR
    if toward:
        place, path, path_m = _named_place(origin, toward)
    else:
        spot = _first_walkable(origin, _parks(origin, want_m)) or _first_walkable(origin, _landmarks(origin, want_m))
        if not spot:
            raise ToolError(
                f"Couldn't find a park or landmark reachable on foot from {origin['name']}. "
                "Suggest an out-and-back on a main road with sidewalks, or ask for a different start."
            )
        place, path, path_m = spot

    way = "toward"
    if path_m < half_m and not toward:  # a place the runner named is where they want to be
        # Too close: keep running past it so the turnaround is half the run away.
        beyond = _past(origin, place, want_m * 1.2)  # overshoot; cut at halfway below
        if beyond:
            (path, path_m), way = beyond, "past"

    per = M_PER_UNIT[unit]
    if path_m >= half_m:
        path = _cut(path, half_m)
        directions = f"Run {half_m / per:.2f} {unit} {way} {place['name']}, turn around, and run back the same way."
    else:  # the runner's chosen place is close, or every direction past ours hits water
        around = "inside the park" if place["park"] else "looping the blocks around it"
        directions = (
            f"Run {path_m / per:.2f} {unit} to {place['name']}, add {(half_m - path_m) * 2 / per:.2f} {unit} "
            f"{around}, then run back the same way."
        )

    return json.dumps({
        "start": {"name": origin["name"], "lat": round(origin["lat"], 5), "lon": round(origin["lon"], 5)},
        "target": f"{distance:g} {unit}",
        "routes": [{
            "name": f"Out-and-back to {place['name']}",
            "directions": directions,
            "total": f"{target_m / per:.1f} {unit}",
            "path": [[round(lat, 5), round(lon, 5)] for lat, lon in path[:: max(1, len(path) // 60)]],
        }],
    })


ASK_START = (
    "Ask the runner where this run starts: a neighborhood, address, or landmark with "
    "its city, e.g. 'Santurce, Puerto Rico' or 'MSG, NYC'. Don't guess it."
)
# Words that mean "wherever I am", which the geocoder can't know (it matches a
# building called "My House" in France).
VAGUE_STARTS = {"", "here", "home", "my home", "my house", "my place", "my apartment",
                "my location", "current location", "near me", "nearby", "me"}


def _start_point(start: str) -> dict:
    """Geocode the start, refusing anything too vague to start a route from."""
    if start.strip().lower() in VAGUE_STARTS:
        raise ToolError(f"'{start}' isn't a place I can find on a map. {ASK_START}")
    origin = geocode(start)
    if origin["rank"] is not None and origin["rank"] < 16:  # a state, country, or whole metro
        raise ToolError(f"'{start}' is too big an area to start a route from. {ASK_START}")
    return origin


# --- Choosing where to go -------------------------------------------------

def _named_place(origin: dict, toward: str) -> tuple[dict, list, float]:
    """The place the runner asked to run to, near the start, and the walking path there."""
    hit = geocode(toward, near=origin)
    place = {"name": hit["name"].split(",")[0], "lat": hit["lat"], "lon": hit["lon"],
             "park": hit["type"] in ("park", "garden", "nature_reserve")}
    walk = _walk(origin, place)
    if not walk:
        raise ToolError(
            f"{place['name']} can't be reached on foot from {origin['name']} without a ferry or a long detour. "
            "Tell the runner and offer a route without `toward`."
        )
    return place, *walk


def _first_walkable(origin: dict, places: list) -> tuple[dict, list, float] | None:
    """The first of the best five places with a sensible walking path, plus that path."""
    for place in places[:5]:
        walk = _walk(origin, place)
        if walk:
            return place, *walk
    return None


def _past(origin: dict, place: dict, meters: float) -> tuple[list, float] | None:
    """A walking path through `place` to a point `meters` out in the same direction.

    Tries straight ahead, then 30 degrees either way, since a river or the edge
    of an island may be dead ahead.
    """
    # Flat-earth math is plenty accurate at running distances.
    kx = math.cos(math.radians(origin["lat"]))
    dx, dy = (place["lon"] - origin["lon"]) * kx, place["lat"] - origin["lat"]
    scale = meters / max(haversine_m(origin["lat"], origin["lon"], place["lat"], place["lon"]), 1)
    for turn in (0, 30, -30):
        t = math.radians(turn)
        rx, ry = dx * math.cos(t) - dy * math.sin(t), dx * math.sin(t) + dy * math.cos(t)
        end = {"lat": origin["lat"] + ry * scale, "lon": origin["lon"] + rx * scale / kx}
        walk = _walk(origin, end, via=place)
        if walk:
            return walk
    return None


def _parks(origin: dict, want_m: float) -> list:
    """Named parks big enough to run in, best fit for `want_m` first."""
    found = _search(origin, want_m, "nwr[leisure=park][name]{around};", ["leisure:park"])
    # Skip pocket parks and playgrounds: under ~250m corner to corner.
    parks = [p for p in found if p["size"] >= 250]
    for p in parks:
        p["park"] = True
    return _by_fit(origin, parks, want_m)


def _landmarks(origin: dict, want_m: float) -> list:
    """Recognizable named spots for when no park works: landmarks first, then food."""
    places = _search(
        origin, want_m,
        "nwr[tourism~'^(attraction|museum|viewpoint)$'][name]{around};"
        "nwr[historic][name]{around};"
        "nwr[amenity~'^(cafe|restaurant|fast_food|ice_cream)$'][name]{around};",
        ["tourism:museum", "tourism:attraction", "amenity:cafe", "amenity:restaurant"],
    )
    for p in places:
        p["park"] = False
    # Within ~30% of the ideal distance, a landmark beats a food spot.
    off = lambda p: abs(haversine_m(origin["lat"], origin["lon"], p["lat"], p["lon"]) - want_m)
    return sorted(places, key=lambda p: (off(p) > want_m * 0.3, p["food"], off(p)))


def _by_fit(origin: dict, places: list, want_m: float) -> list:
    """Places sorted by how close their distance from the start is to `want_m`."""
    return sorted(places, key=lambda p: abs(haversine_m(origin["lat"], origin["lon"], p["lat"], p["lon"]) - want_m))


# --- Place search: Overpass, with Photon as the backup ---------------------

def _search(origin: dict, want_m: float, query: str, photon_tags: list[str]) -> list:
    """Named places near the start as [{name, lat, lon, size, food}].

    `query` is Overpass QL with an {around} placeholder; `photon_tags` are the
    same kinds of place as OSM tags for Photon, used when Overpass is down.
    `size` is the corner-to-corner length of the place in meters (0 if unknown).
    """
    radius = int(min(8000, max(1000, want_m * 1.5)))
    around = f"(around:{radius},{origin['lat']},{origin['lon']})"
    elements = overpass(f"[out:json][timeout:25];({query.format(around=around)});out bb 400;")
    if elements is not None:
        return [_from_overpass(el) for el in elements if "tags" in el]
    return [_from_photon(f) for f in _photon(origin, radius, photon_tags)]


def _from_overpass(el: dict) -> dict:
    box = el.get("bounds")  # ways and relations; nodes have lat/lon instead
    if box:
        size = haversine_m(box["minlat"], box["minlon"], box["maxlat"], box["maxlon"])
        lat, lon = (box["minlat"] + box["maxlat"]) / 2, (box["minlon"] + box["maxlon"]) / 2
    else:
        size, lat, lon = 0, el["lat"], el["lon"]
    return {"name": el["tags"]["name"], "lat": lat, "lon": lon, "size": size, "food": "amenity" in el["tags"]}


def _from_photon(f: dict) -> dict:
    props, (lon, lat) = f["properties"], f["geometry"]["coordinates"]
    box = props.get("extent")  # [minlon, maxlat, maxlon, minlat]
    size = haversine_m(box[3], box[0], box[1], box[2]) if box else 0
    return {"name": props["name"], "lat": lat, "lon": lon,
            "size": size, "food": props.get("osm_key") == "amenity"}


def _photon(origin: dict, radius: int, tags: list[str]) -> list:
    """Photon (komoot) features near the start: a separate OSM index, for when Overpass is down."""
    features, reached = [], False
    for tag in tags:
        key = (round(origin["lat"], 3), round(origin["lon"], 3), radius, tag)
        if key not in _CACHE:
            try:
                resp = HTTP.get(PHOTON_URL, params={
                    "lat": origin["lat"], "lon": origin["lon"],
                    "osm_tag": tag, "radius": radius / 1000, "limit": 50,
                }, timeout=10)
                resp.raise_for_status()
                _CACHE[key] = resp.json()["features"]
            except (requests.RequestException, ValueError, KeyError):
                continue
        reached = True
        features += _CACHE[key]
    if not reached:
        raise ToolError("OpenStreetMap's servers are overloaded right now. Tell the runner and offer to try again in a minute.")
    return [f for f in features if f["properties"].get("name")]


# --- Walking paths ----------------------------------------------------------

def _walk(origin: dict, dest: dict, via: dict | None = None) -> tuple[list, float] | None:
    """Real walking path from the start to `dest` (through `via`) and its length in meters.

    None if the path takes a ferry or is more than twice the straight-line
    distance: places across a river look close, but the router reaches them by
    ferry or a far-off bridge.
    """
    stops = [origin] + ([via] if via else []) + [dest]
    coords = ";".join(f"{p['lon']:.6f},{p['lat']:.6f}" for p in stops)
    try:
        route = HTTP.get(OSRM_FOOT_URL + coords, params={"overview": "full", "geometries": "geojson", "steps": "true"}, timeout=15).json()["routes"][0]
    except (requests.RequestException, ValueError, KeyError, IndexError):
        raise ToolError("The walking router is unreachable right now, so I can't measure a route. Try again in a minute.")
    ferry = any(step.get("mode") == "ferry" for leg in route["legs"] for step in leg["steps"])
    straight = haversine_m(origin["lat"], origin["lon"], dest["lat"], dest["lon"])
    if ferry or route["distance"] > 2 * straight + 500:
        return None
    return [(lat, lon) for lon, lat in route["geometry"]["coordinates"]], route["distance"]


def _cut(path: list, meters: float) -> list:
    """The first `meters` of a path."""
    out, done = path[:1], 0.0
    for a, b in zip(path, path[1:]):
        done += haversine_m(*a, *b)
        out.append(b)
        if done >= meters:
            break
    return out


SCHEMA = {
    "type": "function",
    "function": {
        "name": "find_running_routes",
        "description": (
            "Suggest an out-and-back running route of the requested distance: run toward a "
            "nearby park (or a landmark/food spot if there's no park) on real walking paths, turn around at halfway, run back. Call it when "
            "the runner asks where to run or wants a route for today's workout."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "start": {"type": "string", "description": (
                    "Where the run starts, exactly as the runner said it: address, landmark, or "
                    "neighborhood with city, or cross streets like 'Amsterdam Ave & W 119th St, New York'. Never guess "
                    "or fill this in yourself; if the runner hasn't said, ask them first."
                )},
                "distance": {"type": "number", "description": "Total run distance in `unit`, there and back."},
                "unit": {"type": "string", "enum": ["mi", "km"], "description": "Default 'mi'."},
                "toward": {"type": "string", "description": (
                    "Only if the runner names where they want to run (e.g. 'Central Park', 'the "
                    "Brooklyn Bridge'): head there instead of a park the tool picks. Leave out otherwise."
                )},
            },
            "required": ["start", "distance"],
        },
    },
}
