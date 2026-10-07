import difflib
import json
import os
import re
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
from tools.common import plain
from tools.routes import ASK_START

# --- Config ---

SYSTEM_PROMPT = """You are Cadence, a running coach for busy students and workers.
Today is {today}. The runner's local time is {now}.

You can:
- plan_running_week: fit their runs around class/work and the forecast. You need their
  location, the runs they want (type + distance), and their busy times. Before your
  FIRST call to this tool in a session, ALWAYS ask ONE combined question that covers
  whatever is missing PLUS whether they have a preferred time of day to run (morning,
  midday, or evening) — ask this even if nothing else is missing. Skip it only if they
  already stated a time-of-day preference unprompted. Once they reply (even "no
  preference"), call the tool and don't ask about time of day again. Expand "Mon-Fri
  9-5" into one busy block per day.
- get_todays_run: when they say things like "I want to do my run today", look up the
  saved plan. Then, unless they only asked what the workout is, get them a route for
  the workout's distance (see find_running_routes).
- find_running_routes: a route that matches the distance, out-and-back or one way.
  The runner must tell you where this run starts (a neighborhood, address, landmark,
  or cross streets, with the city). If they haven't said it for this run, ask "Where
  are you starting from?" and wait; don't call the tool yet. Never fill in the start
  yourself, not even from the week plan's location; you may offer it as a suggestion
  ("Starting from <the place they gave> again?"). If they name where they want to run
  ("toward Central Park"), pass it as `toward`. If they want to end at a place ("finish
  at Columbus Circle"), pass it as `finish`. If they say one way / point to point / not
  coming back, pass trip "one_way" (with or without a finish); if they want to go to the
  finish and come back, pass trip "out_and_back". The distance is the run they're doing that day: if
  they didn't say it, get it from get_todays_run or ask. If the tool says a finish is
  not plausible, tell them why with its numbers and offer its suggestion; don't offer
  to change their run's distance to fit the finish. Describe the
  route the tool returned, not one of your own.
- build_run_playlist: music whose BPM matches their cadence. For intervals, use the fast
  rep pace. Pass a genre only if the runner named one. If it returns genre_options, ask
  in one short line which genre they want (the card lists them), then call it again.

Style:
- Lead with the answer. Be concise and warm, like a coach texting an athlete.
- Use the tool results; never invent routes, songs, times or weather.
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
        reply = (
            litellm.completion(
                model=MODEL,
                vertex_location="global",
                messages=messages,
                tools=TOOLS,
            )
            .choices[0]
            .message
        )

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
                if call.function.name == "find_running_routes" and not _runner_said(
                    args.get("start"), messages
                ):
                    result = json.dumps(
                        {
                            "error": f"The runner never said where this run starts. {ASK_START}"
                        }
                    )
                else:
                    result = run_tool(call.function.name, args, state)
            except json.JSONDecodeError as e:
                args, result = (
                    {},
                    json.dumps(
                        {
                            "error": f"Arguments were not valid JSON ({e}). Call the tool again."
                        }
                    ),
                )
            tool_calls += [{"name": call.function.name, "args": args, "result": result}]

            messages += [{"role": "tool", "tool_call_id": call.id, "content": result}]

    return "Sorry, I hit my tool-call limit before finishing.", tool_calls


# Words that don't show the runner named this particular place.
GENERIC_PLACE_WORDS = {
    "city",
    "the",
    "and",
    "park",
    "ave",
    "avenue",
    "street",
    "road",
    "blvd",
    "boulevard",
    "university",
}


def _place_words(text: str) -> list[str]:
    """Lowercase words, with "West 119th" and "W 119" both reduced to "119"."""
    text = re.sub(r"\b(?:west|east|north|south|w|e|n|s)\s+(?=\d)", "", plain(text))
    return [
        re.sub(r"^(\d+)(?:st|nd|rd|th)$", r"\1", w)
        for w in re.findall(r"[a-z0-9]+", text)
    ]


def _runner_said(place: str | None, messages: list[dict]) -> bool:
    """Whether the runner typed this place: every distinctive word of its name, or its initials ("MSG").

    Only the part before the first comma counts (models add the city), and
    typos count ("asmterdam" is Amsterdam). Models fill in a start the runner
    never gave, even when told to ask, or swap in a nearby place when the
    runner's doesn't geocode. This catches both.
    """
    said = set(
        _place_words(
            " ".join(
                m["content"]
                for m in messages
                if m.get("role") == "user" and isinstance(m.get("content"), str)
            )
        )
    )
    name = _place_words(str(place or "").split(",")[0])
    if len(name) >= 2 and "".join(w[0] for w in name) in said:
        return True
    words = [
        w for w in name if (len(w) >= 3 or w.isdigit()) and w not in GENERIC_PLACE_WORDS
    ]
    return bool(words) and all(
        w in said or difflib.get_close_matches(w, said, n=1, cutoff=0.8) for w in words
    )


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
    timezone: str | None = (
        None  # the browser sends its IANA zone so "today" is the runner's today
    )


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
        sessions[session_id] = [
            {"role": "system", "content": _system_prompt(request.timezone)}
        ]
        session_state[session_id] = {}

    # Append user's message to the context
    sessions[session_id] += [{"role": "user", "content": request.message}]

    try:
        response, tool_calls = run_agent(
            sessions[session_id], session_state[session_id]
        )
    except Exception as e:
        # Auth, billing, a model that is not running: show it in the chat, not as a 500.
        response, tool_calls = (
            f"Model call failed: {type(e).__name__}: {str(e)[:300]}",
            [],
        )

    return ChatResponse(
        response=response or "", session_id=session_id, tool_calls=tool_calls
    )


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
        track = requests.get(
            f"https://api.deezer.com/track/isrc:{isrc}", timeout=8
        ).json()
    except (requests.RequestException, ValueError):
        return {"preview": None, "cover": None}
    return {
        "preview": track.get("preview") or None,
        "cover": (track.get("album") or {}).get("cover_small"),
    }


def _system_prompt(tz_name: str | None) -> str:
    try:
        tz = ZoneInfo(tz_name or "America/New_York")
    except Exception:
        tz = ZoneInfo("America/New_York")
    now = datetime.now(tz)
    return SYSTEM_PROMPT.format(
        today=now.strftime("%A, %B %-d, %Y"), now=now.strftime("%H:%M")
    )


if __name__ == "__main__":
    # Cloud Run tells us which port to listen on; locally, stay on localhost:8000.
    port = os.environ.get("PORT")
    uvicorn.run(app, host="0.0.0.0" if port else "127.0.0.1", port=int(port or 8000))
