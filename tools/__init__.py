"""The tools the harness can run, and the JSON that describes them to the model.

One tool per file, one owner per tool:
  playlist.py      build_run_playlist   - BPM-matched electronic music for a run
  week_planner.py  plan_running_week    - fit runs around class/work and the weather
  todays_run.py    get_todays_run       - "I want to do my run today"
  routes.py        find_running_routes  - real routes to parks and tracks, flat for intervals
"""

import inspect
import json

import requests

from tools import playlist, routes, todays_run, week_planner
from tools.common import ToolError

# What the model sees: the "set notes" in the screenplay.
TOOLS = [week_planner.SCHEMA, todays_run.SCHEMA, routes.SCHEMA, playlist.SCHEMA]

# What the harness runs: tool name -> Python function.
TOOL_MAP = {
    "plan_running_week": week_planner.plan_running_week,
    "get_todays_run": todays_run.get_todays_run,
    "find_running_routes": routes.find_running_routes,
    "build_run_playlist": playlist.build_run_playlist,
}

# Tools that read or write the session's saved plan. The harness passes them the
# session state; the model never sees or sends it.
STATEFUL = {"plan_running_week", "get_todays_run"}


def run_tool(name: str, args: dict, state: dict) -> str:
    """Run one tool call. Models invent tool names and arguments; never let that crash the loop."""
    if name not in TOOL_MAP:
        return json.dumps({"error": f"Unknown tool '{name}'. Available: {list(TOOL_MAP)}"})
    # Models sometimes add arguments that don't exist (e.g. "time_unit"). Drop them so
    # one stray key doesn't sink an otherwise good call; missing ones still error below.
    accepted = inspect.signature(TOOL_MAP[name]).parameters
    args = {k: v for k, v in args.items() if k in accepted and k != "state"}
    if name in STATEFUL:
        args["state"] = state
    try:
        return TOOL_MAP[name](**args)
    except ToolError as e:
        return json.dumps({"error": str(e)})
    except TypeError as e:
        return json.dumps({"error": f"Bad arguments for {name}: {e}. Check the parameter names in the tool schema."})
    except requests.RequestException as e:
        return json.dumps({"error": f"{name} couldn't reach an outside service ({type(e).__name__}). Tell the runner and offer to retry."})
    except Exception as e:  # a bug in a tool shouldn't take down the whole chat
        return json.dumps({"error": f"{name} failed unexpectedly: {type(e).__name__}: {e}"})
