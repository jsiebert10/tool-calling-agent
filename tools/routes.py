"""find_running_routes: a route on real walking paths that covers the run's distance.

Out-and-back toward a named place or a nearby park (turning at halfway), or a
route that ends at the runner's finish.
"""

import json
import math

import requests

from tools.common import HTTP, M_PER_UNIT, ToolError, geocode, haversine_m, overpass

PHOTON_URL = "https://photon.komoot.io/reverse"
OSRM_FOOT_URL = "https://routing.openstreetmap.de/routed-foot/route/v1/foot/"
VALHALLA_URL = "https://valhalla1.openstreetmap.de/route"

DETOUR = 1.25  # streets are ~25% longer than a straight line
_CACHE: dict = {}  # Photon searches, so repeat requests for the same area skip the network


def find_running_routes(
    start: str,
    distance: float,
    unit: str = "mi",
    toward: str | None = None,
    finish: str | None = None,
    there_and_back: bool = False,
) -> str:
    if unit not in M_PER_UNIT:
        raise ToolError("unit must be 'mi' or 'km'.")
    target_m = float(distance) * M_PER_UNIT[unit]
    if not 800 <= target_m <= 50_000:
        raise ToolError(f"{distance} {unit} is outside what I can route (0.5-30 mi). Check the distance and unit.")

    origin = _start_point(start)
    if finish:
        there_and_back = str(there_and_back).lower() in ("true", "1", "yes")  # models send "false" as text
        result = _to_finish(origin, finish, there_and_back, target_m, distance, unit)
        if result:
            return result

    half_m = target_m / 2
    route = _route(origin, half_m, toward)
    if not route:
        raise ToolError(
            f"Couldn't find a {distance:g} {unit} out-and-back from {origin['name']} that stays on land. "
            "Tell the runner and suggest a shorter distance or a different start."
        )
    name, way, path, out_m = route
    per = M_PER_UNIT[unit]
    return _result(origin, distance, unit, {
        "name": f"Out-and-back {way} {name}",
        "directions": f"Run {out_m / per:.2f} {unit} {way} {name}, turn around, and run back the same way.",
        "total": f"{2 * out_m / per:.1f} {unit}",
        "path": path,
        "one_way": False,
    })


def _to_finish(origin: dict, finish: str, there_and_back: bool, target_m: float, distance: float, unit: str) -> str | None:
    """Route ending at `finish` (or there and back); not plausible if over 5% off. None if finish is the start."""
    hit = geocode(finish, near=origin)
    if haversine_m(origin["lat"], origin["lon"], hit["lat"], hit["lon"]) < 200:
        return None
    name = hit["name"].split(",")[0]
    walk = _walk(origin, hit, limit_detour=False)
    if not walk:
        raise ToolError(
            f"{name} can't be reached on foot from {origin['name']} without a ferry. "
            "Tell the runner and ask for a different finish."
        )
    path, leg_m = walk
    per = M_PER_UNIT[unit]
    total_m = leg_m * (2 if there_and_back else 1)
    if abs(total_m - target_m) > target_m * TOLERANCE:
        trip = "there and back" if there_and_back else "there"
        other_trip, other_m = ("one way", leg_m) if there_and_back else ("there and back", 2 * leg_m)
        if abs(other_m - target_m) <= target_m * TOLERANCE:
            fix = f"Running {other_trip} would be {other_m / per:.1f} {unit}, which fits; offer that."
        else:
            fix = (
                f"Offer a finish about {target_m / per:.1f} {unit} away (one way) or "
                f"{target_m / 2 / per:.1f} {unit} away (there and back), or a route with no finish."
            )
        raise ToolError(
            f"Not plausible: {name} is {leg_m / per:.1f} {unit} from {origin['name']} on foot, so running "
            f"{trip} is {total_m / per:.1f} {unit}, but this run is {distance:g} {unit} (more than 5% off). "
            f"Tell the runner this finish doesn't fit today's run. {fix} Don't suggest changing the run's distance."
        )

    if there_and_back:
        route = {
            "name": f"There and back to {name}",
            "directions": f"Run {leg_m / per:.2f} {unit} to {name}, turn around, and run back the same way.",
        }
    else:
        route = {"name": f"To {name}", "directions": f"Run {leg_m / per:.2f} {unit} to {name} and finish there."}
    return _result(origin, distance, unit, route | {
        "total": f"{total_m / per:.1f} {unit}", "path": path, "one_way": not there_and_back,
    })


def _result(origin: dict, distance: float, unit: str, route: dict) -> str:
    path = route["path"]
    thin = path[:: max(1, len(path) // 300)]  # enough points that the line follows the streets
    if thin[-1] != path[-1]:
        thin.append(path[-1])  # keep the exact turnaround or finish
    return json.dumps({
        "start": {"name": origin["name"], "lat": round(origin["lat"], 5), "lon": round(origin["lon"], 5)},
        "target": f"{distance:g} {unit}",
        "routes": [route | {"path": [[round(lat, 5), round(lon, 5)] for lat, lon in thin]}],
    })


ASK_START = (
    "Ask the runner where this run starts: a neighborhood, address, or landmark with "
    "its city, e.g. 'Santurce, Puerto Rico' or 'MSG, NYC'. Don't guess it."
)
# Words that mean "wherever I am", which the geocoder can't know.
VAGUE_STARTS = {"", "here", "near me", "nearby", "current location", "my location", "me"}


def _start_point(start: str) -> dict:
    """Geocode the start, refusing anything too vague to start a route from."""
    if start.strip().lower() in VAGUE_STARTS:
        raise ToolError(f"'{start}' isn't a place I can find on a map. {ASK_START}")
    origin = geocode(start)
    if origin["rank"] is not None and origin["rank"] < 16:  # a state, country, or whole metro
        raise ToolError(f"'{start}' is too big an area to start a route from. {ASK_START}")
    return origin


# --- Choosing where to go -------------------------------------------------

TOLERANCE = 0.05  # every route is within 5% of the run's distance
COMPASS = ["north", "northeast", "east", "southeast", "south", "southwest", "west", "northwest"]


def _route(origin: dict, half_m: float, toward: str | None) -> tuple[str, str, list, float] | None:
    """(name, way, path out, meters out). Tries the named place, then parks, landmarks, compass directions."""
    want_m = half_m / DETOUR
    if toward:
        place = _named_place(origin, toward)
        if place:
            route = _long_enough(origin, place, half_m, limit_detour=False)
            if not route:
                raise ToolError(
                    f"Can't make a sensible out-and-back of that length toward {place['name']} "
                    "(it would cross water or double back). Tell the runner and offer a route without `toward`."
                )
            return route
    for places in (_parks, _landmarks):
        for place in places(origin, want_m)[:5]:
            route = _long_enough(origin, place, half_m)
            if route:
                return route
    for i, heading in enumerate(COMPASS):  # nowhere to aim for: just pick a direction
        out = _out_leg(origin, _walk(origin, _ahead(origin, i * 45, want_m * 1.2)), half_m)
        if out:
            return heading, "heading", *out
    return None


def _long_enough(origin: dict, place: dict, half_m: float, limit_detour: bool = True) -> tuple[str, str, list, float] | None:
    """Out leg toward `place`; if it's too close, keep going past it (bending up to 60° around water)."""
    walk = _walk(origin, place, limit_detour=limit_detour)
    out = _out_leg(origin, walk, half_m)
    if out:
        return place["name"], "toward" if walk[1] > half_m else "to", *out
    if not walk or walk[1] >= half_m:
        return None  # unreachable, or long enough but not sensible
    bearing = _bearing(origin, place)
    for turn in (0, 30, -30, 60, -60):
        end = _ahead(origin, bearing + turn, half_m / DETOUR * 1.2)  # overshoot; cut at halfway
        out = _out_leg(origin, _walk(origin, end, via=place, limit_detour=limit_detour), half_m)
        if out:
            return place["name"], "past", *out
    return None


def _out_leg(origin: dict, walk: tuple[list, float] | None, half_m: float) -> tuple[list, float] | None:
    """First half of the run along `walk`: within 5% of halfway, turnaround at the far end (no doubling back)."""
    if not walk or walk[1] < half_m * (1 - TOLERANCE):
        return None
    out_m = min(walk[1], half_m)
    path = _cut(walk[0], out_m)
    away = [haversine_m(origin["lat"], origin["lon"], lat, lon) for lat, lon in path]
    if away[-1] < 0.7 * max(away):
        return None
    return path, out_m


def _named_place(origin: dict, toward: str) -> dict | None:
    """The runner's named place, or None if it's the start itself (models repeat it as `toward`)."""
    hit = geocode(toward, near=origin)
    if haversine_m(origin["lat"], origin["lon"], hit["lat"], hit["lon"]) < 50:  # the same spot, not just nearby
        return None
    return {"name": hit["name"].split(",")[0], "lat": hit["lat"], "lon": hit["lon"]}


# Flat-earth math is plenty accurate at running distances.
def _bearing(a: dict, b: dict) -> float:
    """Degrees clockwise from north, from a to b."""
    return math.degrees(math.atan2((b["lon"] - a["lon"]) * math.cos(math.radians(a["lat"])), b["lat"] - a["lat"]))


def _ahead(origin: dict, bearing: float, meters: float) -> dict:
    """The point `meters` from the start along `bearing`."""
    deg = meters / 111_320  # meters per degree of latitude
    b = math.radians(bearing)
    return {"lat": origin["lat"] + deg * math.cos(b),
            "lon": origin["lon"] + deg * math.sin(b) / math.cos(math.radians(origin["lat"]))}


def _parks(origin: dict, want_m: float) -> list:
    """Named parks big enough to run in, best fit for `want_m` first."""
    found = _search(origin, want_m, "nwr[leisure=park][name]{around};", ["leisure:park"])
    # Skip pocket parks and playgrounds: under ~250m corner to corner.
    parks = [p for p in found if p["size"] >= 250]
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
    # Within ~30% of the ideal distance, a landmark beats a food spot.
    off = lambda p: abs(haversine_m(origin["lat"], origin["lon"], p["lat"], p["lon"]) - want_m)
    return sorted(places, key=lambda p: (off(p) > want_m * 0.3, p["food"], off(p)))


def _by_fit(origin: dict, places: list, want_m: float) -> list:
    """Places sorted by how close their distance from the start is to `want_m`."""
    return sorted(places, key=lambda p: abs(haversine_m(origin["lat"], origin["lon"], p["lat"], p["lon"]) - want_m))


# --- Place search: Overpass, with Photon as the backup ---------------------

def _search(origin: dict, want_m: float, query: str, photon_tags: list[str]) -> list:
    """Named places near the start as [{name, lat, lon, size, food}], from Overpass or else Photon."""
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
    """Photon features near the start, for when Overpass is down."""
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

def _walk(origin: dict, dest: dict, via: dict | None = None, limit_detour: bool = True) -> tuple[list, float] | None:
    """(path, meters) on foot, or None if it needs a ferry or (with `limit_detour`) is over 2x the straight line."""
    stops = [origin] + ([via] if via else []) + [dest]
    walk = _osrm(stops)
    if not walk or walk[2]:  # unreachable, or it takes a ferry
        no_ferry = _valhalla(stops)
        if not walk and not no_ferry:
            raise ToolError("The walking routers are unreachable right now, so I can't measure a route. Try again in a minute.")
        if not no_ferry or no_ferry[2]:
            return None
        walk = no_ferry
    path, meters, _ = walk
    if limit_detour and meters > 2 * haversine_m(origin["lat"], origin["lon"], dest["lat"], dest["lon"]) + 500:
        return None
    return path, meters


def _osrm(stops: list) -> tuple[list, float, bool] | None:
    """(path, meters, takes a ferry) from the OSRM foot router, or None if it's down."""
    coords = ";".join(f"{p['lon']:.6f},{p['lat']:.6f}" for p in stops)
    try:
        route = HTTP.get(OSRM_FOOT_URL + coords, params={"overview": "full", "geometries": "geojson", "steps": "true"},
                         timeout=15).json()["routes"][0]
    except (requests.RequestException, ValueError, KeyError, IndexError):
        return None
    ferry = any(step.get("mode") == "ferry" for leg in route["legs"] for step in leg["steps"])
    return [(lat, lon) for lon, lat in route["geometry"]["coordinates"]], route["distance"], ferry


def _valhalla(stops: list) -> tuple[list, float, bool] | None:
    """(path, meters, takes a ferry) from Valhalla, avoiding ferries, or None if it's down."""
    try:
        trip = HTTP.post(VALHALLA_URL, json={
            "locations": [{"lat": p["lat"], "lon": p["lon"]} for p in stops],
            "costing": "pedestrian", "costing_options": {"pedestrian": {"use_ferry": 0}}, "units": "kilometers",
        }, timeout=20).json()["trip"]
    except (requests.RequestException, ValueError, KeyError):
        return None
    ferry = any(m["type"] in (28, 29) for leg in trip["legs"] for m in leg["maneuvers"])  # ferry enter/exit
    path = [point for leg in trip["legs"] for point in _decode_polyline6(leg["shape"])]
    return path, trip["summary"]["length"] * 1000, ferry


def _decode_polyline6(encoded: str) -> list:
    """Decode Valhalla's polyline (6 decimal places)."""
    points, i, lat, lon = [], 0, 0, 0
    while i < len(encoded):
        deltas = []
        for _ in range(2):
            shift = value = 0
            while True:
                b = ord(encoded[i]) - 63
                i += 1
                value |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            deltas.append(~(value >> 1) if value & 1 else value >> 1)
        lat, lon = lat + deltas[0], lon + deltas[1]
        points.append((lat / 1e6, lon / 1e6))
    return points


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
            "Suggest a running route of the requested distance on real walking paths. By default an "
            "out-and-back toward a nearby park (or landmark), turning around at halfway. With `finish`, "
            "a route that ends at that place (or goes there and back). Call it when the runner asks "
            "where to run or wants a route for today's workout."
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
                    "Brooklyn Bridge'): head there instead of a park the tool picks. Never the start itself. Leave out otherwise."
                )},
                "finish": {"type": "string", "description": (
                    "Only if the runner wants the route to end at (or turn around exactly at) a specific "
                    "place, e.g. 'Columbus Circle, New York'. The walking distance must match `distance` "
                    "within 5%, or the tool says it's not plausible."
                )},
                "there_and_back": {"type": "boolean", "description": (
                    "With `finish`: true = run to the finish and back to the start; false (default) = end at the finish."
                )},
            },
            "required": ["start", "distance"],
        },
    },
}
