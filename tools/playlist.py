"""build_run_playlist: electronic tracks whose BPM matches the runner's cadence.

Running to a beat works when one footstep lands on each beat, so the target BPM
is the runner's cadence (steps per minute). Cadence is estimated from pace
unless the runner knows theirs. Tempo data comes from ReccoBeats (free, no key),
which serves Spotify-style audio features for an artist's catalog.
"""

import json
import random
import time
from concurrent.futures import ThreadPoolExecutor

import requests

from tools.common import HTTP, M_PER_UNIT, ToolError, fmt_minutes, fmt_pace, parse_pace

RECCOBEATS_URL = "https://api.reccobeats.com/v1"

# Electronic artists grouped by the tempo range their tracks usually sit in.
GENRES = {
    "house": {"bpm": (118, 130), "artists": ["Fred again..", "FISHER", "Chris Lake", "John Summit", "Dom Dolla", "Disclosure"]},
    "techno": {"bpm": (124, 140), "artists": ["Charlotte de Witte", "Amelie Lens", "Adam Beyer", "Enrico Sangiuliano", "Reinier Zonneveld", "Boris Brejcha"]},
    "trance": {"bpm": (128, 142), "artists": ["Armin van Buuren", "Above & Beyond", "Eric Prydz", "ARTBAT", "Anyma"]},
    "hard techno": {"bpm": (140, 165), "artists": ["Sara Landry", "I Hate Models", "Indira Paganotto", "Nico Moreno", "Dax J", "Trym", "Funk Tribu"]},
    "hardstyle": {"bpm": (145, 160), "artists": ["Headhunterz", "Sub Zero Project", "Da Tweekaz", "Brennan Heart", "Wildstylez"]},
    "drum and bass": {"bpm": (160, 180), "artists": ["Pendulum", "Sub Focus", "Chase & Status", "Wilkinson", "Netsky", "Hybrid Minds", "Dimension", "High Contrast", "Culture Shock"]},
}

# artist name -> list of tracks with tempo. Catalogs don't change mid-semester.
_catalog_cache: dict[str, list[dict]] = {}

# ReccoBeats allows ~24 requests per burst and each new artist costs 3, so only
# look up a few uncached artists per playlist. The cache fills in over time.
MAX_NEW_ARTISTS = 6


def estimate_cadence(pace_sec_per_km: float) -> int:
    """Steps per minute rise roughly linearly with speed: ~157 spm at a 10:00/mi
    jog, ~172 at 7:00/mi, ~185 at 5:30/mi."""
    speed_kmh = 3600 / pace_sec_per_km
    return round(min(192, max(150, 155 + 3.5 * (speed_kmh - 9))))


def build_run_playlist(
    pace: str,
    distance: float | None = None,
    unit: str = "mi",
    duration_minutes: float | None = None,
    cadence: int | None = None,
    genre: str | None = None,
) -> str:
    if unit not in M_PER_UNIT:
        raise ToolError("unit must be 'mi' or 'km'.")
    if genre and genre not in GENRES:
        raise ToolError(f"Unknown genre '{genre}'. Choose one of {list(GENRES)} or leave it out.")
    pace_s = parse_pace(pace, unit)

    if duration_minutes:
        minutes = float(duration_minutes)
    elif distance:
        minutes = float(distance) * pace_s / 60
    else:
        raise ToolError("Pass either distance (with pace) or duration_minutes so I know how long the playlist should be.")
    if not 5 <= minutes <= 300:
        raise ToolError(f"A {minutes:.0f}-minute run is outside the 5-300 minute range. Check the distance and unit.")

    if cadence:
        if not 140 <= cadence <= 200:
            raise ToolError("cadence should be steps per minute, usually 150-190. Leave it out to estimate from pace.")
        target, cadence_source = int(cadence), "runner's own cadence"
    else:
        target = estimate_cadence(pace_s / (M_PER_UNIT[unit] / 1000))
        cadence_source = f"estimated from a {fmt_pace(pace_s)}/{unit} pace"

    # Genres whose usual tempo covers the target; the requested genre goes first.
    covering = [g for g, info in GENRES.items() if info["bpm"][0] - 4 <= target <= info["bpm"][1] + 4]
    if not covering:
        covering = [min(GENRES, key=lambda g: min(abs(target - b) for b in GENRES[g]["bpm"]))]
    genres = [genre] + [g for g in covering if g != genre] if genre else covering

    pool_artists = [a for g in genres for a in GENRES[g]["artists"]]
    uncached = [a for a in pool_artists if a not in _catalog_cache]
    fetch = set(random.sample(uncached, min(MAX_NEW_ARTISTS, len(uncached))))
    artists = [a for a in pool_artists if a in _catalog_cache or a in fetch]
    with ThreadPoolExecutor(max_workers=3) as pool:
        catalogs = list(pool.map(_artist_catalog, artists))
    if not any(catalogs):
        raise ToolError("The ReccoBeats music service didn't return any tracks right now. Try again in a minute.")

    # Widen the BPM window only if the tight window can't fill the run.
    for tolerance in (2, 4, 6, 8):
        picks = _pick_tracks(catalogs, genres, artists, target, tolerance, minutes)
        if sum(t["seconds"] for t in picks) >= minutes * 60:
            break

    total_min = sum(t["seconds"] for t in picks) / 60
    note = None
    if genre and genre not in covering:
        lo, hi = GENRES[genre]["bpm"]
        note = (
            f"{genre} usually runs {lo}-{hi} BPM, which doesn't match a {target} spm cadence, "
            f"so the playlist leans on {', '.join(g for g in genres if g != genre)} instead."
        )
    if total_min < minutes:
        note = (note + " " if note else "") + (
            f"Only found {total_min:.0f} of {minutes:.0f} minutes of music near {target} BPM; repeat the playlist to finish."
        )

    return json.dumps({
        "run_minutes": round(minutes, 1),
        "target_bpm": target,
        "cadence_source": cadence_source,
        "bpm_tolerance": tolerance,
        "genres_searched": genres,
        "playlist_length": fmt_minutes(total_min),
        "tracks": [
            {
                "title": t["title"],
                "artist": t["artist"],
                "genre": t["genre"],
                "bpm": t["bpm"],
                "match": t["match"],
                "length": fmt_pace(t["seconds"]),
                "energy": t["energy"],
                "spotify": t["spotify"],
                "isrc": t["isrc"],
            }
            for t in picks
        ],
        "note": note,
    })


def _pick_tracks(catalogs, genres, artists, target, tolerance, minutes):
    genre_of = {a: g for g in reversed(genres) for a in GENRES[g]["artists"]}
    candidates = []
    for artist, catalog in zip(artists, catalogs):
        for t in catalog:
            # One step per beat, or two steps per beat for half-time tracks
            # (tempo detectors often read 174 BPM drum and bass as 87).
            if abs(t["tempo"] - target) <= tolerance:
                match = "1 step per beat"
            elif abs(t["tempo"] * 2 - target) <= tolerance:
                match = "2 steps per beat (half-time)"
            else:
                continue
            if t["energy"] < 0.55:
                continue  # ambient intros and breakdown edits kill momentum
            candidates.append({**t, "genre": genre_of[artist], "match": match, "bpm": round(t["tempo"])})

    random.shuffle(candidates)
    candidates.sort(key=lambda t: (genres.index(t["genre"]), t["match"] != "1 step per beat"))

    picks, per_artist, seen = [], {}, set()
    for cap in (3, 6):  # variety first; relax only if the run isn't covered yet
        for t in candidates:
            if sum(p["seconds"] for p in picks) >= minutes * 60:
                break
            key = t["title"].split(" - ")[0].lower()
            lead = t["artist"].split(", ")[0]  # count collaborations against the lead artist
            if key in seen or per_artist.get(lead, 0) >= cap:
                continue
            picks.append(t)
            seen.add(key)
            per_artist[lead] = per_artist.get(lead, 0) + 1
    # Build energy through the run: calmer tracks first, the most intense last.
    return sorted(picks, key=lambda t: t["energy"])


def _artist_catalog(name: str) -> list[dict]:
    """An artist's tracks with tempo and energy. Empty list if anything fails:
    one missing artist shouldn't sink the playlist."""
    if name in _catalog_cache:
        return _catalog_cache[name]
    try:
        found = _recco("/artist/search", searchText=name, size=5)
        matches = [a for a in found["content"] if a["name"].lower() == name.lower()]
        if not matches:
            _catalog_cache[name] = []
            return []
        tracks = _recco(f"/artist/{matches[0]['id']}/track", size=50)["content"]
        features = {}
        for i in range(0, len(tracks), 40):
            ids = ",".join(t["id"] for t in tracks[i : i + 40])
            features |= {f["id"]: f for f in _recco("/audio-features", ids=ids)["content"]}
    except (requests.RequestException, ValueError, KeyError):
        return []  # not cached, so the next playlist retries this artist

    catalog = []
    for t in tracks:
        f = features.get(t["id"])
        if not f or not f.get("tempo") or not 90_000 <= t["durationMs"] <= 600_000:
            continue
        catalog.append({
            "title": t["trackTitle"],
            "artist": ", ".join(a["name"] for a in t["artists"][:2]),
            "seconds": t["durationMs"] // 1000,
            "tempo": f["tempo"],
            "energy": round(f.get("energy", 0), 2),
            "spotify": t.get("href"),
            "isrc": t.get("isrc"),
        })
    _catalog_cache[name] = catalog
    return catalog


def _recco(path: str, **params) -> dict:
    """GET from ReccoBeats, waiting out its short rate-limit windows."""
    for _ in range(4):
        resp = HTTP.get(RECCOBEATS_URL + path, params=params, timeout=15)
        if resp.status_code != 429:
            resp.raise_for_status()
            return resp.json()
        time.sleep(float(resp.headers.get("Retry-After", 2)))
    resp.raise_for_status()


SCHEMA = {
    "type": "function",
    "function": {
        "name": "build_run_playlist",
        "description": (
            "Build an electronic music playlist whose BPM matches the runner's cadence (steps per "
            "minute), long enough to last the whole run. Uses real tempo data per track. Call it "
            "whenever the runner wants music for a run. For interval workouts, build it for the "
            "pace of the fast reps. Returns tracks with BPM, how they match the stride, and Spotify links."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "pace": {"type": "string", "description": "Planned pace as minutes:seconds per unit, e.g. '8:30'."},
                "distance": {"type": "number", "description": "Run distance in `unit`. Omit if duration_minutes is given."},
                "unit": {"type": "string", "enum": ["mi", "km"], "description": "Unit for distance and pace. Default 'mi'."},
                "duration_minutes": {"type": "number", "description": "Run length in minutes, if the runner gave a time instead of a distance."},
                "cadence": {"type": "integer", "description": "Only if the runner told you their cadence (steps per minute, e.g. from a watch). Never guess it; omit it and it is estimated from pace."},
                "genre": {
                    "type": "string",
                    "enum": list(GENRES),
                    "description": "Preferred electronic genre. Omit to pick whichever genres naturally sit at the target BPM.",
                },
            },
            "required": ["pace"],
        },
    },
}
