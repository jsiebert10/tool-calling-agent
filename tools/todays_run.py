"""get_todays_run: "Hey, I want to do my run today" -> what's on the plan, rechecked.

Reads the week saved by plan_running_week, then pulls a fresh forecast: if the
weather at the planned time got worse since planning, it suggests a better
time today that still fits around the runner's schedule.
"""

import json
from datetime import date, timedelta

from tools.common import ToolError
from tools.week_planner import (
    WEEKDAYS,
    best_slot,
    describe_window,
    get_forecast,
    is_unsafe,
    temp_discomfort,
)


def get_todays_run(day: str = "today", state: dict | None = None) -> str:
    plan = (state or {}).get("plan")
    if not plan:
        raise ToolError(
            "No weekly plan exists in this session yet. Ask the runner for their location, their "
            "class/work schedule, and the runs they want this week, then call plan_running_week. "
            "If they just want to run now, ask for distance and run type and call find_running_routes."
        )

    forecast = get_forecast(
        plan["location"]["lat"], plan["location"]["lon"], plan["unit"]
    )
    target = _resolve_day(day, date.fromisoformat(forecast["today"]))
    days = {d["date"]: d for d in plan["days"]}
    entry = days.get(target)
    if entry is None:
        raise ToolError(
            f"{target} is outside the saved plan ({plan['days'][0]['date']} to {plan['days'][-1]['date']}). "
            "Offer to re-plan the week with plan_running_week."
        )

    if not entry["run"]:
        upcoming = [d for d in plan["days"] if d["date"] > target and d["run"]]
        return json.dumps(
            {
                "date": target,
                "weekday": entry["weekday"],
                "rest_day": True,
                "message": "Rest day on the plan. Easy walking or mobility is fine.",
                "next_run": {
                    "date": upcoming[0]["date"],
                    "weekday": upcoming[0]["weekday"],
                    "title": upcoming[0]["run"]["title"],
                    "start": upcoming[0]["run"]["start"],
                }
                if upcoming
                else None,
            }
        )

    run = entry["run"]
    day_index = next(i for i, d in enumerate(forecast["days"]) if d["date"] == target)
    fc_day = forecast["days"][day_index]
    start = _mins(run["start"])
    end = start + run["est_minutes"]

    result = {
        "date": target,
        "weekday": entry["weekday"],
        "workout": run["title"],
        "type": run["type"],
        "distance": run["distance"],
        "unit": plan["unit"],
        "segments": run["segments"],
        "estimated_time": f"{run['est_minutes']} min",
        "planned_start": run["start"],
        "start_location": plan["location"]["name"],
        "sunrise": fc_day["sunrise"],
        "sunset": fc_day["sunset"],
    }

    if target == forecast["today"] and start < forecast["now_minutes"]:
        result["planned_time_status"] = (
            "The planned start time has already passed today."
        )
    else:
        hours = fc_day["hours"][start // 60 : min(24, (end + 59) // 60)]
        unsafe = is_unsafe(hours)
        if unsafe:
            result["forecast_at_planned_time"] = {"weather": None, "warnings": unsafe}
            result["forecast_changed"] = True
        else:
            discomfort, _ = temp_discomfort(hours)
            summary, warnings = describe_window(hours, forecast["deg"])
            result["forecast_at_planned_time"] = {
                "weather": summary,
                "warnings": warnings,
            }
            # 8°F further outside the comfort band than when planned counts as "got worse"
            result["forecast_changed"] = discomfort > run.get("discomfort", 0) + 8

    # Suggest a better time if the planned one passed or got worse.
    if result.get("forecast_changed") or "planned_time_status" in result:
        alt, alt_hazards = best_slot(
            forecast,
            day_index,
            run["est_minutes"],
            plan["busy"],
            *plan["window"],
            plan.get("preferred_time", "any"),
        )
        if alt:
            result["better_time_today"] = alt
        elif alt_hazards:
            result["better_time_today"] = (
                f"No safe window left today: {', '.join(alt_hazards)}."
            )
        else:
            result["better_time_today"] = (
                "No free window with decent weather left today. Suggest moving it to tomorrow."
            )

    if run["type"] == "intervals":
        result["route_hint"] = (
            "Intervals: look for a 400m track or a flat, uninterrupted loop (call find_running_routes with run_type='intervals')."
        )
    return json.dumps(result)


def _resolve_day(day: str, today: date) -> str:
    text = (day or "today").strip().lower()
    if text == "today":
        return today.isoformat()
    if text == "tomorrow":
        return (today + timedelta(days=1)).isoformat()
    if text in WEEKDAYS:
        ahead = (WEEKDAYS.index(text) - today.weekday()) % 7
        return (today + timedelta(days=ahead)).isoformat()
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        raise ToolError(
            f"Couldn't read the day '{day}'. Use 'today', 'tomorrow', a weekday name, or YYYY-MM-DD."
        )


def _mins(clock: str) -> int:
    h, m = clock.split(":")
    return int(h) * 60 + int(m)


SCHEMA = {
    "type": "function",
    "function": {
        "name": "get_todays_run",
        "description": (
            "Look up the run scheduled for a day in the runner's saved weekly plan (from "
            "plan_running_week): the workout broken into segments with target paces, planned "
            "start time, and a fresh forecast for that time. If the weather got worse or the time "
            "passed, it suggests a better free time today. Call it when the runner says things like "
            "'I want to do my run today' or 'what's my workout tomorrow?'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "day": {
                    "type": "string",
                    "description": "'today' (default), 'tomorrow', a weekday name like 'friday', or YYYY-MM-DD.",
                },
            },
        },
    },
}
