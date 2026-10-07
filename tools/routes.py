"""find_running_routes: an out-and-back from the runner's start to a nearby park.

1. Geocode the start.
2. Find named parks nearby (OpenStreetMap via Overpass) and pick the one about
   half the run away.
3. Get the real walking path there (OSRM foot router), turn around at half the
   distance, and run back the same way.
"""

import json

import requests

from tools.common import HTTP, M_PER_UNIT, ToolError, geocode, haversine_m

OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]
OSRM_FOOT_URL = "https://routing.openstreetmap.de/routed-foot/route/v1/foot/"

DETOUR = 1.25  # streets are ~25% longer than a straight line


def find_running_routes(start: str, distance: float, unit: str = "mi") -> str:
    if unit not in M_PER_UNIT:
        raise ToolError("unit must be 'mi' or 'km'.")
    target_m = float(distance) * M_PER_UNIT[unit]
    if not 800 <= target_m <= 50_000:
        raise ToolError(f"{distance} {unit} is outside what I can route (0.5-30 mi). Check the distance and unit.")

    origin = geocode(start)
    half_m = target_m / 2
    park = _park_about(origin, half_m / DETOUR)
    path, path_m = _walk(origin, park)

    per = M_PER_UNIT[unit]
    if path_m >= half_m:
        path = _cut(path, half_m)
        directions = f"Run {half_m / per:.2f} {unit} toward {park['name']}, turn around, and run back the same way."
    else:
        extra = (half_m - path_m) * 2
        directions = (
            f"Run {path_m / per:.2f} {unit} to {park['name']}, add {extra / per:.2f} {unit} "
            "inside the park, then run back the same way."
        )

    return json.dumps({
        "start": {"name": origin["name"], "lat": round(origin["lat"], 5), "lon": round(origin["lon"], 5)},
        "target": f"{distance:g} {unit}",
        "routes": [{
            "name": f"Out-and-back to {park['name']}",
            "directions": directions,
            "total": f"{target_m / per:.1f} {unit}",
            "path": [[round(lat, 5), round(lon, 5)] for lat, lon in path[:: max(1, len(path) // 60)]],
        }],
    })


def _park_about(origin: dict, want_m: float) -> dict:
    """The named park whose distance from the start is closest to `want_m`."""
    radius = int(min(8000, max(1000, want_m * 1.5)))
    query = (
        f"[out:json][timeout:25];"
        f"nwr[leisure=park][name](around:{radius},{origin['lat']},{origin['lon']});"
        f"out bb;"
    )
    for url in OVERPASS_URLS * 2:  # the public servers return 504 under load
        try:
            resp = HTTP.post(url, data={"data": query}, timeout=20)
            if resp.ok:
                elements = resp.json()["elements"]
                break
        except (requests.RequestException, ValueError, KeyError):
            continue
    else:
        raise ToolError("OpenStreetMap's servers are overloaded right now. Tell the runner and offer to try again in a minute.")

    parks = []
    for el in elements:
        box = el.get("bounds")
        if not box:
            continue  # parks mapped as a single point have no size
        # Skip pocket parks and playgrounds: under ~250m corner to corner.
        if haversine_m(box["minlat"], box["minlon"], box["maxlat"], box["maxlon"]) < 250:
            continue
        lat, lon = (box["minlat"] + box["maxlat"]) / 2, (box["minlon"] + box["maxlon"]) / 2
        parks.append({"name": el["tags"]["name"], "lat": lat, "lon": lon})
    if not parks:
        raise ToolError(
            f"No parks within {radius / 1000:.1f} km of {origin['name']}. "
            "Suggest an out-and-back on a main road with sidewalks, or ask for a different start."
        )
    return min(parks, key=lambda p: abs(haversine_m(origin["lat"], origin["lon"], p["lat"], p["lon"]) - want_m))


def _walk(origin: dict, park: dict) -> tuple[list, float]:
    """Real walking path from the start to the park, and its length in meters."""
    coords = f"{origin['lon']:.6f},{origin['lat']:.6f};{park['lon']:.6f},{park['lat']:.6f}"
    try:
        route = HTTP.get(OSRM_FOOT_URL + coords, params={"overview": "full", "geometries": "geojson"}, timeout=15).json()["routes"][0]
    except (requests.RequestException, ValueError, KeyError, IndexError):
        raise ToolError("The walking router is unreachable right now, so I can't measure a route. Try again in a minute.")
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
            "nearby park on real walking paths, turn around at halfway, run back. Call it when "
            "the runner asks where to run or wants a route for today's workout."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "start": {"type": "string", "description": "Where the run starts: address, landmark, or neighborhood with city, e.g. 'Columbia University, New York'."},
                "distance": {"type": "number", "description": "Total run distance in `unit`, there and back."},
                "unit": {"type": "string", "enum": ["mi", "km"], "description": "Default 'mi'."},
            },
            "required": ["start", "distance"],
        },
    },
}
