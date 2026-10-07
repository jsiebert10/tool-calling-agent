"""build_run_playlist: tracks whose BPM matches the runner's cadence.

Running to a beat works when footsteps land on the beat, so the target BPM is
the runner's cadence (steps per minute): one step per beat, or two steps per
beat on a half-time track. Cadence is estimated from pace unless the runner
knows theirs. When several genres fit, the runner picks one. Tempo data comes
from ReccoBeats (free, no key), which serves Spotify-style audio features for
an artist's catalog.
"""

import json
import random
import time
from concurrent.futures import ThreadPoolExecutor

import requests

from tools.common import HTTP, M_PER_UNIT, ToolError, fmt_minutes, fmt_pace, parse_pace
from tools.genres import GENRES

RECCOBEATS_URL = "https://api.reccobeats.com/v1"

ONE_STEP, HALF_TIME = "1 step per beat", "2 steps per beat (half-time)"
BPM_TOLERANCE = 8
MAX_PER_ARTIST = 3

# artist name -> list of tracks with tempo. Catalogs don't change mid-semester.
_catalog_cache: dict[str, list[dict]] = {}

# ReccoBeats allows ~24 requests per burst and each new artist costs 3, so only
# look up a few uncached artists per playlist. The cache fills in over time.
MAX_NEW_ARTISTS = 8


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
        raise ToolError(f"Unknown genre '{genre}'. Choose one of {list(GENRES)}.")
    pace_s = parse_pace(pace, unit)

    if cadence:
        if not 150 <= cadence <= 190:
            raise ToolError("cadence should be steps per minute, usually 150-190. Leave it out to estimate from pace.")
        target, cadence_source = int(cadence), "runner's own cadence"
    else:
        target = estimate_cadence(pace_s / (M_PER_UNIT[unit] / 1000))
        cadence_source = f"estimated from a {fmt_pace(pace_s)}/{unit} pace"

    # Several genres fit: let the runner choose before fetching any music.
    options = {g: beat for g in GENRES if (beat := _beat(g, target))}
    if not genre and len(options) > 1:
        return json.dumps({
            "target_bpm": target,
            "cadence_source": cadence_source,
            "genre_options": [{"genre": g, "examples": GENRES[g]["artists"][:4], "beat": b} for g, b in options.items()],
            "next_step": "Ask the runner which genre they want, then call build_run_playlist again with it.",
        })
    genre = genre or next(iter(options))
    if genre not in options:
        raise ToolError(f"{genre} doesn't fit a {target} spm cadence. Choose one of {list(options)}.")

    if duration_minutes:
        minutes = float(duration_minutes)
    elif distance:
        minutes = float(distance) * pace_s / 60
    else:
        raise ToolError("Pass either distance (with pace) or duration_minutes so I know how long the playlist should be.")
    if not 5 <= minutes <= 300:
        raise ToolError(f"A {minutes:.0f}-minute run is outside the 5-300 minute range. Check the distance and unit.")

    # Shuffled so repeat playlists vary.
    pool_artists = random.sample(GENRES[genre]["artists"], len(GENRES[genre]["artists"]))
    fetch = [a for a in pool_artists if a not in _catalog_cache][:MAX_NEW_ARTISTS]
    artists = [a for a in pool_artists if a in _catalog_cache or a in fetch]
    with ThreadPoolExecutor(max_workers=3) as pool:
        catalogs = list(pool.map(_artist_catalog, artists))
    if not any(catalogs):
        raise ToolError("The ReccoBeats music service didn't return any tracks right now. Try again in a minute.")

    picks = _pick_tracks(catalogs, target, minutes)
    total_min = sum(t["seconds"] for t in picks) / 60
    note = None
    if total_min < minutes:
        note = f"Only found {total_min:.0f} of {minutes:.0f} minutes of {genre} near {target} BPM; repeat the playlist to finish."

    return json.dumps({
        "run_minutes": round(minutes, 1),
        "target_bpm": target,
        "cadence_source": cadence_source,
        "genre": genre,
        "playlist_length": fmt_minutes(total_min),
        "tracks": [
            {
                "title": t["title"],
                "artist": t["artist"],
                "bpm": t["bpm"],
                "match": t["match"],
                "length": fmt_pace(t["seconds"]),
                "spotify": t["spotify"],
                "isrc": t["isrc"],
            }
            for t in picks
        ],
        "note": note,
    })


def _beat(genre: str, target: int) -> str | None:
    """How a genre fits a cadence: a beat per step, a beat every two steps, or not at all."""
    lo, hi = GENRES[genre]["bpm"]
    for beat, bpm in ((ONE_STEP, target), (HALF_TIME, target / 2)):
        if lo - 4 <= bpm <= hi + 4:
            return beat
    return None


def _pick_tracks(catalogs, target, minutes):
    """Tracks closest to the target BPM first, so only the best matches make the cut."""
    candidates = []
    for t in (t for catalog in catalogs for t in catalog):
        # Steps per minute off the cadence at one step per beat, or at two steps per beat
        # for half-time tracks (tempo detectors often read 174 BPM drum and bass as 87).
        match, off = min((ONE_STEP, abs(t["tempo"] - target)), (HALF_TIME, abs(t["tempo"] * 2 - target)), key=lambda m: m[1])
        if off <= BPM_TOLERANCE:
            candidates.append({**t, "match": match, "bpm": round(t["tempo"]), "off": off})

    random.shuffle(candidates)  # tracks equally close come in a different order each time
    candidates.sort(key=lambda t: round(t["off"]))

    picks, per_artist, seen = [], {}, set()
    for t in candidates:
        if sum(p["seconds"] for p in picks) >= minutes * 60:
            break
        key = t["title"].split(" - ")[0].lower()
        lead = t["artist"].split(", ")[0]  # count collaborations against the lead artist
        if key in seen or per_artist.get(lead, 0) >= MAX_PER_ARTIST:
            continue
        picks.append(t)
        seen.add(key)
        per_artist[lead] = per_artist.get(lead, 0) + 1
    return picks


def _artist_catalog(name: str) -> list[dict]:
    """An artist's tracks with tempo. Empty list if anything fails:
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
            "Build a playlist whose BPM matches the runner's cadence (steps per minute), long enough "
            "to last the whole run. Uses real tempo data per track. Call it whenever the runner wants "
            "music for a run. For interval workouts, build it for the pace of the fast reps. Without "
            "a genre, if several genres fit the cadence it returns genre_options instead of tracks: "
            "ask the runner to pick one, then call again with that genre. Returns tracks with BPM, "
            "how they match the stride, and Spotify links."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "pace": {"type": "string", "description": "Planned pace as minutes:seconds per unit, e.g. '8:30'."},
                "distance": {"type": "number", "description": "Run distance in `unit`. Pass it (or duration_minutes) on every call, including the first one without a genre."},
                "unit": {"type": "string", "enum": ["mi", "km"], "description": "Unit for distance and pace. Default 'mi'."},
                "duration_minutes": {"type": "number", "description": "Run length in minutes, if the runner gave a time instead of a distance."},
                "cadence": {"type": "integer", "description": "Only if the runner told you their cadence (steps per minute, e.g. from a watch). Never guess it; omit it and it is estimated from pace."},
                "genre": {
                    "type": "string",
                    "enum": list(GENRES),
                    "description": "Only once the runner has named or picked a genre. Omit it to get the genres that fit.",
                },
            },
            "required": ["pace"],
        },
    },
}
