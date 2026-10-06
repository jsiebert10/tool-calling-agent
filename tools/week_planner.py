"""plan_running_week: fit the runner's runs into their free time and the best weather.

1. Geocode the location and pull a 7-day hourly forecast + air quality (Open-Meteo).
2. For every day, find every free start time (outside class/work, with a buffer)
   and score it: rain, storms, heat, cold, wind, darkness, bad air.
3. Try every assignment of runs to days and keep the best total, penalizing
   hard workouts on back-to-back days.
The plan is saved in the session so get_todays_run can read it later.
"""

import json
from datetime import datetime, timedelta, timezone

import requests

from tools.common import HTTP, M_PER_UNIT, ToolError, fmt_minutes, fmt_pace, geocode, parse_pace

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
AIR_QUALITY_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
RUN_TYPES = ["easy", "recovery", "long", "tempo", "intervals", "race"]
HARD_TYPES = {"long", "tempo", "intervals", "race"}

# Seconds per mile relative to easy pace. Scaled for km below.
PACE_OFFSET_PER_MI = {"easy": 0, "recovery": 75, "long": 30, "tempo": -75, "intervals": -120, "race": -90}

BUFFER_MIN = 30  # to get changed and travel between a run and class/work


def plan_running_week(
    location: str,
    runs: list[dict],
    busy_blocks: list[dict] | None = None,
    unit: str = "mi",
    easy_pace: str | None = None,
    earliest: str = "06:00",
    latest: str = "21:00",
    preferred_time: str = "any",
    state: dict | None = None,
) -> str:
    if unit not in M_PER_UNIT:
        raise ToolError("unit must be 'mi' or 'km'.")
    if not runs:
        raise ToolError("runs is empty. Ask the runner which runs they want this week (type and distance).")
    if len(runs) > 7:
        raise ToolError("This planner fits at most one run per day (7 runs). Ask the runner which runs to combine or drop.")
    for r in runs:
        if r.get("type") not in RUN_TYPES:
            raise ToolError(f"Run type '{r.get('type')}' isn't supported. Use one of {RUN_TYPES}.")
        if not 0 < float(r.get("distance") or 0) <= 50:
            raise ToolError(f"Each run needs a distance between 0 and 50 {unit}. Got {r.get('distance')!r} for a {r['type']} run.")

    easy_s = parse_pace(easy_pace or ("10:00" if unit == "mi" else "6:15"), unit)
    day_start, day_end = _minutes(earliest), _minutes(latest)
    busy = _busy_by_weekday(busy_blocks or [])
    place = geocode(location)
    forecast = get_forecast(place["lat"], place["lon"], unit)

    workouts = [build_workout(r["type"], float(r["distance"]), unit, easy_s) for r in runs]

    # best[run][day] = best slot that day for that run, or None if it doesn't fit
    best = [
        [
            best_slot(forecast, day, w["est_minutes"], busy, day_start, day_end, preferred_time)
            for day in range(len(forecast["days"]))
        ]
        for w in workouts
    ]

    plan_days = _assign(workouts, best, forecast)

    days, unscheduled = [], []
    for day_index, day in enumerate(forecast["days"]):
        entry = {"date": day["date"], "weekday": day["weekday"], "run": None, "conditions": day["summary"]}
        for i, d in enumerate(plan_days):
            if d == day_index:
                entry["run"] = {**workouts[i], **best[i][d]}
        days.append(entry)
    for i, d in enumerate(plan_days):
        if d is None:
            unscheduled.append(
                f"{workouts[i]['title']} ({fmt_minutes(workouts[i]['est_minutes'])}) didn't fit: no free "
                f"window that long between {earliest} and {latest} on any open day. Ask the runner "
                "to free up time, shorten it, or widen earliest/latest."
            )

    if state is not None:
        state["plan"] = {
            "location": place,
            "unit": unit,
            "easy_pace_s": easy_s,
            "timezone": forecast["timezone"],
            "busy": busy,
            "window": [day_start, day_end],
            "preferred_time": preferred_time,
            "days": days,
        }

    return json.dumps({
        "location": place["name"],
        "unit": unit,
        "easy_pace": f"{fmt_pace(easy_s)}/{unit}",
        "week": [
            {
                "date": d["date"],
                "weekday": d["weekday"],
                "day_conditions": d["conditions"],
                "run": None if not d["run"] else {
                    "title": d["run"]["title"],
                    "type": d["run"]["type"],
                    "distance": d["run"]["distance"],
                    "start": d["run"]["start"],
                    "end": d["run"]["end"],
                    "weather": d["run"]["weather"],
                    "rating": d["run"]["rating"],
                    "warnings": d["run"]["warnings"],
                },
            }
            for d in days
        ],
        "unscheduled": unscheduled,
        "busy_blocks": busy_blocks or [],
        "saved": "Plan saved for this session; get_todays_run can read it.",
    })


# --- Workouts ---


def build_workout(run_type: str, distance: float, unit: str, easy_s: int) -> dict:
    """Break a run into segments with target paces."""
    scale = 1 if unit == "mi" else 1 / 1.609344
    pace = lambda kind: easy_s + PACE_OFFSET_PER_MI[kind] * scale  # noqa: E731
    km = unit == "km"
    wu = round(min(max(distance * 0.2, 1.0 if not km else 1.5), 2 if not km else 3), 1)
    cd = round(min(max(distance * 0.15, 0.5 if not km else 1), 1.5 if not km else 2.5), 1)

    if run_type == "intervals" and distance - wu - cd >= (1.2 if not km else 2):
        rep, jog = (0.5, 0.25) if not km else (0.8, 0.4)
        reps = max(3, min(12, int((distance - wu - cd + jog) / (rep + jog))))
        segments = [
            {"name": "Warm-up", "distance": wu, "pace": pace("easy")},
            {"name": f"{reps} x 800m repeats", "distance": round(reps * rep, 2), "pace": pace("intervals"),
             "detail": f"{reps} x 800m fast, {reps - 1} x {int(jog * (1609 if not km else 1000))}m easy jog between"},
            {"name": "Recovery jogs", "distance": round((reps - 1) * jog, 2), "pace": pace("recovery")},
            {"name": "Cool-down", "distance": cd, "pace": pace("easy")},
        ]
        title = f"Intervals: {reps} x 800m"
    elif run_type == "tempo" and distance - wu - cd >= 1:
        block = round(distance - wu - cd, 1)
        segments = [
            {"name": "Warm-up", "distance": wu, "pace": pace("easy")},
            {"name": "Tempo block", "distance": block, "pace": pace("tempo"), "detail": "comfortably hard, could speak a few words"},
            {"name": "Cool-down", "distance": cd, "pace": pace("easy")},
        ]
        title = f"Tempo: {block:g} {unit} at threshold"
    else:
        notes = {
            "easy": "conversational, you could hold a full conversation",
            "recovery": "very easy, slower than you think",
            "long": "steady and relaxed; bring water and fuel if over 90 minutes",
            "race": "race effort. Warm up well first",
            "tempo": "too short for a full tempo, so run it steady",
            "intervals": "too short for repeats, so do 6 x 20s strides at the end",
        }
        kind = run_type if run_type in ("easy", "recovery", "long", "race") else "easy"
        segments = [{"name": run_type.capitalize() + " run", "distance": distance, "pace": pace(kind), "detail": notes[run_type]}]
        title = f"{run_type.capitalize()} run"

    seconds = sum(s["distance"] * s["pace"] for s in segments)
    for s in segments:
        s["pace"] = f"{fmt_pace(s['pace'])}/{unit}"
    return {
        "type": run_type,
        "title": f"{title} ({distance:g} {unit})",
        "distance": distance,
        "segments": segments,
        "est_minutes": round(seconds / 60 + 5),  # +5 for stopping at lights, water
    }


# --- Weather ---


def get_forecast(lat: float, lon: float, unit: str) -> dict:
    """7 days of hourly weather + air quality in the location's local time."""
    imperial = unit == "mi"
    try:
        wx = HTTP.get(
            FORECAST_URL,
            params={
                "latitude": lat,
                "longitude": lon,
                "hourly": "temperature_2m,apparent_temperature,precipitation_probability,precipitation,"
                "weather_code,wind_speed_10m,uv_index,is_day",
                "daily": "sunrise,sunset,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
                "temperature_unit": "fahrenheit" if imperial else "celsius",
                "wind_speed_unit": "mph" if imperial else "kmh",
                "timezone": "auto",
                "forecast_days": 7,
            },
            timeout=15,
        ).json()
        hourly = wx["hourly"]
    except (requests.RequestException, KeyError, ValueError) as e:
        raise ToolError(f"The Open-Meteo weather service failed ({e}). Try again shortly.")

    try:
        aq = HTTP.get(
            AIR_QUALITY_URL,
            params={"latitude": lat, "longitude": lon, "hourly": "us_aqi", "timezone": "auto", "forecast_days": 7},
            timeout=10,
        ).json()["hourly"]
        aqi_by_time = dict(zip(aq["time"], aq["us_aqi"]))
    except (requests.RequestException, KeyError, ValueError):
        aqi_by_time = {}  # air quality is a bonus; plan without it

    hours = []
    for i, t in enumerate(hourly["time"]):
        hours.append({
            "time": t,
            "feels": _to_f(hourly["apparent_temperature"][i], imperial),
            "temp": hourly["temperature_2m"][i],
            "rain_pct": hourly["precipitation_probability"][i] or 0,
            "rain_mm": hourly["precipitation"][i] or 0,
            "code": hourly["weather_code"][i] or 0,
            "wind": hourly["wind_speed_10m"][i] * (1 if imperial else 0.621),
            "uv": hourly["uv_index"][i] or 0,
            "is_day": hourly["is_day"][i],
            "aqi": aqi_by_time.get(t),
        })

    daily = wx["daily"]
    deg = "°F" if imperial else "°C"
    days = []
    for d, date in enumerate(daily["time"]):
        dt = datetime.fromisoformat(date)
        days.append({
            "date": date,
            "weekday": dt.strftime("%A"),
            "hours": hours[d * 24 : (d + 1) * 24],
            "sunrise": daily["sunrise"][d][11:],
            "sunset": daily["sunset"][d][11:],
            "summary": f"{round(daily['temperature_2m_min'][d])}-{round(daily['temperature_2m_max'][d])}{deg}, "
            f"up to {daily['precipitation_probability_max'][d] or 0}% rain chance",
        })

    now_local = datetime.now(timezone.utc) + timedelta(seconds=wx["utc_offset_seconds"])
    return {
        "timezone": wx["timezone"],
        "now_minutes": now_local.hour * 60 + now_local.minute,
        "today": now_local.date().isoformat(),
        "deg": deg,
        "days": days,
    }


def score_window(hours: list[dict], deg: str) -> tuple[int, list[str], str]:
    """Score the hours a run covers from 0-100 and say why points were lost."""
    feels = max(h["feels"] for h in hours)
    coldest = min(h["feels"] for h in hours)
    rain_pct = max(h["rain_pct"] for h in hours)
    rain_mm = max(h["rain_mm"] for h in hours)
    wind = max(h["wind"] for h in hours)
    uv = max(h["uv"] for h in hours)
    aqis = [h["aqi"] for h in hours if h["aqi"] is not None]
    aqi = max(aqis) if aqis else None

    penalty, warnings = 0, []
    if any(h["code"] >= 95 for h in hours):
        penalty += 60
        warnings.append("thunderstorms forecast")
    if rain_mm >= 2.5 or rain_pct >= 70:
        penalty += 40
        warnings.append(f"heavy rain likely ({rain_pct}%)")
    elif rain_pct >= 40:
        penalty += 15
        warnings.append(f"{rain_pct}% chance of rain")
    if feels >= 90:
        penalty += 45
        warnings.append(f"dangerous heat (feels like {round(feels)}°F)")
    elif feels >= 80:
        penalty += round((feels - 80) * 2)
        warnings.append(f"hot (feels like {round(feels)}°F)")
    if coldest <= 10:
        penalty += 35
        warnings.append(f"severe cold (feels like {round(coldest)}°F)")
    elif coldest <= 25:
        penalty += round(25 - coldest)
        warnings.append(f"cold (feels like {round(coldest)}°F)")
    if wind >= 22:
        penalty += 12
        warnings.append(f"windy ({round(wind)} mph)")
    if any(not h["is_day"] for h in hours):
        penalty += 12
        warnings.append("dark: wear lights/reflective gear, stick to lit busy routes")
    if aqi is not None and aqi > 150:
        penalty += 40
        warnings.append(f"unhealthy air (AQI {aqi})")
    elif aqi is not None and aqi > 100:
        penalty += 15
        warnings.append(f"air unhealthy for sensitive groups (AQI {aqi})")
    if uv >= 8:
        penalty += 5
        warnings.append(f"very high UV ({round(uv)}): sunscreen")

    temp = round(sum(h["temp"] for h in hours) / len(hours))
    feels_avg = sum(h["feels"] for h in hours) / len(hours)
    feels_local = round(feels_avg if deg == "°F" else (feels_avg - 32) * 5 / 9)
    summary = f"{temp}{deg} (feels {feels_local}{deg}), {rain_pct}% rain, wind {round(wind)} mph"
    if aqi is not None:
        summary += f", AQI {aqi}"
    return max(0, 100 - penalty), warnings, summary


def best_slot(forecast, day_index, minutes, busy, day_start, day_end, preferred_time="any") -> dict | None:
    """The best-scoring free start time on one day for a run of `minutes`."""
    day = forecast["days"][day_index]
    blocks = busy.get(day["weekday"].lower(), [])
    best = None
    for start in range(day_start, day_end - minutes + 1, 30):
        end = start + minutes
        if day["date"] == forecast["today"] and start < forecast["now_minutes"] + 20:
            continue  # already passed
        if any(start < b_end + BUFFER_MIN and end > b_start - BUFFER_MIN for b_start, b_end in blocks):
            continue
        hours = day["hours"][start // 60 : min(24, (end + 59) // 60)]
        score, warnings, summary = score_window(hours, forecast["deg"])
        if preferred_time != "any" and _period(start) != preferred_time:
            score -= 8
        if best is None or score > best["score"]:
            best = {
                "start": _clock(start),
                "end": _clock(end),
                "score": score,
                "rating": "great" if score >= 85 else "good" if score >= 65 else "ok" if score >= 45 else "poor",
                "weather": summary,
                "warnings": warnings,
            }
    return best


def _assign(workouts, best, forecast) -> list[int | None]:
    """Search every way to put runs on distinct days and keep the best total.

    At most 7 runs x 7 days, so a backtracking search is instant. None means
    the run couldn't be placed; it costs so much it only happens when forced.
    """
    weekend = {i for i, d in enumerate(forecast["days"]) if d["weekday"] in ("Saturday", "Sunday")}
    hard = [w["type"] in HARD_TYPES for w in workouts]
    best_total, best_plan = float("-inf"), [None] * len(workouts)

    def total_for(days):
        total = 0
        for i, d in enumerate(days):
            if d is None:
                total -= 1000
            else:
                total += best[i][d]["score"] + (6 if workouts[i]["type"] == "long" and d in weekend else 0)
        hard_days = sorted(d for i, d in enumerate(days) if d is not None and hard[i])
        return total - 20 * sum(1 for a, b in zip(hard_days, hard_days[1:]) if b - a == 1)

    def search(i, days, used):
        nonlocal best_total, best_plan
        if i == len(workouts):
            total = total_for(days)
            if total > best_total:
                best_total, best_plan = total, list(days)
            return
        for d, slot in enumerate(best[i]):
            if slot is not None and d not in used:
                search(i + 1, days + [d], used | {d})
        search(i + 1, days + [None], used)

    search(0, [], frozenset())
    return best_plan


# --- Small helpers ---


def _busy_by_weekday(blocks: list[dict]) -> dict[str, list[tuple[int, int]]]:
    busy: dict[str, list[tuple[int, int]]] = {}
    for b in blocks:
        day = str(b.get("day", "")).lower()
        if day not in WEEKDAYS:
            raise ToolError(f"Busy block day '{b.get('day')}' must be a weekday name like 'monday'. Expand ranges like 'weekdays' into separate blocks.")
        start, end = _minutes(b.get("start", "")), _minutes(b.get("end", ""))
        if end <= start:
            raise ToolError(f"Busy block on {day} ends ({b.get('end')}) before it starts ({b.get('start')}). Use 24-hour HH:MM.")
        busy.setdefault(day, []).append((start, end))
    return busy


def _minutes(clock: str) -> int:
    try:
        h, m = str(clock).strip().split(":")
        return int(h) * 60 + int(m)
    except ValueError:
        raise ToolError(f"Couldn't read the time '{clock}'. Use 24-hour HH:MM, e.g. '17:30'.")


def _clock(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _period(start: int) -> str:
    return "morning" if start < 11 * 60 else "midday" if start < 15 * 60 else "evening"


def _to_f(value: float, imperial: bool) -> float:
    return value if imperial else value * 9 / 5 + 32


SCHEMA = {
    "type": "function",
    "function": {
        "name": "plan_running_week",
        "description": (
            "Schedule the runner's week: fits each requested run into free time around their "
            "class/work schedule and picks the hours with the best forecast (avoids heavy rain, "
            "storms, extreme heat/cold, darkness, bad air). Covers today plus the next 6 days, at "
            "most one run per day, and keeps hard workouts off back-to-back days. Saves the plan "
            "to the session for get_todays_run. Call it once you know the location, the runs, and "
            "the busy times (an empty schedule is fine if they say they're free)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "location": {"type": "string", "description": "Where they run, e.g. 'Morningside Heights, New York'."},
                "runs": {
                    "type": "array",
                    "description": "The runs they want this week.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "type": {"type": "string", "enum": RUN_TYPES, "description": "Kind of run."},
                            "distance": {"type": "number", "description": "Distance in `unit`."},
                        },
                        "required": ["type", "distance"],
                    },
                },
                "busy_blocks": {
                    "type": "array",
                    "description": "Recurring busy times (class, work, commitments). One entry per weekday; expand 'Mon-Fri 9-5' into five entries.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "day": {"type": "string", "enum": WEEKDAYS},
                            "start": {"type": "string", "description": "24-hour HH:MM, e.g. '09:00'."},
                            "end": {"type": "string", "description": "24-hour HH:MM, e.g. '17:00'."},
                        },
                        "required": ["day", "start", "end"],
                    },
                },
                "unit": {"type": "string", "enum": ["mi", "km"], "description": "Distance unit. Default 'mi'."},
                "easy_pace": {"type": "string", "description": "Their easy pace per unit as M:SS, e.g. '9:45'. Other paces are derived from it. Default 10:00/mi."},
                "earliest": {"type": "string", "description": "Earliest they'd start a run, HH:MM. Default '06:00'."},
                "latest": {"type": "string", "description": "Latest they'd finish a run, HH:MM. Default '21:00'."},
                "preferred_time": {"type": "string", "enum": ["any", "morning", "midday", "evening"], "description": "Time of day they prefer, if any."},
            },
            "required": ["location", "runs"],
        },
    },
}
