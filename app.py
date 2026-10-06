import json
import os
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import litellm
import requests
import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse
from pydantic import BaseModel

from tools import TOOLS, run_tool

# --- Config ---

SYSTEM_PROMPT = """You are Cadence, a running coach for busy students and workers.
Today is {today}. The runner's local time is {now}.

You can:
- plan_running_week: fit their runs around class/work and the forecast. You need their
  location, the runs they want (type + distance), and their busy times. Ask for whatever
  is missing in ONE short question, then call it. Expand "Mon-Fri 9-5" into one busy
  block per day.
- get_todays_run: when they say things like "I want to do my run today", look up the
  saved plan. Then, unless they only asked what the workout is, call find_running_routes
  from the plan's start_location with the workout's distance, type and planned start time.
- find_running_routes: real routes to parks/tracks, flat ones for intervals, with reported
  crime along each route in NYC and Chicago.
- build_run_playlist: electronic music whose BPM matches their cadence. For intervals,
  use the fast rep pace.

Style:
- Lead with the answer. Be concise and warm, like a coach texting an athlete.
- Use the tool results; never invent routes, songs, times or weather.
- Present safety data factually: it counts reported incidents, not a guarantee. If a
  route passes a hotspot, name the street and suggest the safer option or daylight.
- Tool calls and their results are shown to the runner as cards (week grid, route map,
  playlist), so summarize them instead of repeating every number.
- If a tool returns an error, fix the arguments or ask the runner what's needed.
- Default to miles unless the runner uses km. Keep each distance in the unit it was
  given: "5k" is distance 5 with unit "km" (10k = 10 km, half marathon = 21.1 km)."""

MODEL = "vertex_ai/gemini-3.5-flash-lite"
MAX_TOOL_ROUNDS = 8  # "do my run today" chains plan lookup -> routes -> playlist

# --- The Harness ---


def run_agent(messages: list[dict], state: dict) -> tuple[str, list[dict]]:
    """Complete until the model answers without asking for a tool.

    Returns the final text and a record of every tool call made along the way.
    `state` is this session's memory beyond the transcript (the saved week plan).
    """
    tool_calls = []

    for _ in range(MAX_TOOL_ROUNDS):
        reply = litellm.completion(
            model=MODEL,
            vertex_location="global",
            messages=messages,
            tools=TOOLS,
        ).choices[0].message

        # Append assistant's reply (text, tool calls, or both) to the context.
        # model_dump() keeps it a plain dict: the raw object carries provider-specific
        # fields that trip Pydantic when LiteLLM re-serializes it next round.
        messages += [reply.model_dump()]

        if not reply.tool_calls:
            return reply.content, tool_calls

        # The harness, not the model, runs each tool and appends the result
        for call in reply.tool_calls:
            try:
                args = json.loads(call.function.arguments or "{}")
                result = run_tool(call.function.name, args, state)
            except json.JSONDecodeError as e:
                args, result = {}, json.dumps({"error": f"Arguments were not valid JSON ({e}). Call the tool again."})
            tool_calls += [{"name": call.function.name, "args": args, "result": result}]

            messages += [{"role": "tool", "tool_call_id": call.id, "content": result}]

    return "Sorry, I hit my tool-call limit before finishing.", tool_calls


# --- Session Store ---

# session_id -> list of messages. In-memory, single process.
sessions: dict[str, list] = {}
# session_id -> what tools remember for this runner (their weekly plan)
session_state: dict[str, dict] = {}

# --- FastAPI App ---

app = FastAPI()


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None
    timezone: str | None = None  # the browser sends its IANA zone so "today" is the runner's today


class ChatResponse(BaseModel):
    response: str
    session_id: str
    tool_calls: list[dict]


@app.get("/")
def index():
    return FileResponse(Path(__file__).parent / "index.html")


@app.post("/chat", response_model=ChatResponse)
def chat(request: ChatRequest):
    # Get or create the session
    session_id = request.session_id or str(uuid.uuid4())
    if session_id not in sessions:
        sessions[session_id] = [{"role": "system", "content": _system_prompt(request.timezone)}]
        session_state[session_id] = {}

    # Append user's message to the context
    sessions[session_id] += [{"role": "user", "content": request.message}]

    try:
        response, tool_calls = run_agent(sessions[session_id], session_state[session_id])
    except Exception as e:
        # Auth, billing, a model that is not running: show it in the chat, not as a 500.
        response, tool_calls = f"Model call failed: {type(e).__name__}: {str(e)[:300]}", []

    return ChatResponse(response=response or "", session_id=session_id, tool_calls=tool_calls)


@app.post("/clear")
def clear(session_id: str | None = None):
    sessions.pop(session_id, None)
    session_state.pop(session_id, None)
    return {"status": "ok"}


@app.get("/preview")
def preview(isrc: str):
    """30-second preview + cover art from Deezer for a playlist track (UI only).
    Deezer's API has no CORS headers, so the page asks us instead."""
    try:
        track = requests.get(f"https://api.deezer.com/track/isrc:{isrc}", timeout=8).json()
    except (requests.RequestException, ValueError):
        return {"preview": None, "cover": None}
    return {"preview": track.get("preview") or None, "cover": (track.get("album") or {}).get("cover_small")}


def _system_prompt(tz_name: str | None) -> str:
    try:
        tz = ZoneInfo(tz_name or "America/New_York")
    except Exception:
        tz = ZoneInfo("America/New_York")
    now = datetime.now(tz)
    return SYSTEM_PROMPT.format(today=now.strftime("%A, %B %-d, %Y"), now=now.strftime("%H:%M"))


if __name__ == "__main__":
    # Cloud Run tells us which port to listen on; locally, stay on localhost:8000.
    port = os.environ.get("PORT")
    uvicorn.run(app, host="0.0.0.0" if port else "127.0.0.1", port=int(port or 8000))
