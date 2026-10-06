# Cadence: a running coach that works around your life

Cadence is a web chat agent for runners with a busy schedule. You give it your week
(classes, work, the runs you want to do). It fits each run into the time you actually
have, picking the hours with the best weather. When it's time to go, you say *"hey, I
want to do my run today"*. It pulls up the workout, maps a real route from your door
(a track or flat loop for intervals, a big loop for long runs), then builds an
electronic playlist whose BPM matches your stride.

Built on the class `gemini-web-tool-calling` starter: same harness loop, session store
and `/chat` response shape (`response`, `session_id`, `tool_calls` with
`name`/`args`/`result`). Gemini (`vertex_ai/gemini-3.5-flash-lite`) via LiteLLM.

## Sample queries

Run these in order in one session (the second and third build on the first):

1. **Plan the week:** *"I'm in Morningside Heights, NYC. I have class Mon and Wed 10am-4pm
   and work Tue and Thu 9-5. This week I want an easy 4 miler, a 5 mile tempo, 6 miles of
   intervals and a 10 mile long run. My easy pace is about 9:30. When should I run?"*
   You get a 7-day grid with a time slot for each run, rated against the forecast.
   Hard days are never back-to-back.
2. **Run today:** *"Hey, I want to do my run today. What is it and where should I go?"*
   This shows today's workout with segments and paces and re-checks the forecast. Then a
   map of up to 3 real routes from the start point, with distance, laps and climb.
3. **Music:** *"Make me a playlist for that run."*
   You get tracks at your cadence's BPM, with 30-second previews and a metronome.

A one-off that needs no plan: *"I'm doing 5k at 5:20/km from Wicker Park, Chicago tonight at
8pm. Where's a good flat loop, and give me drum and bass for it."*

## Tools

Each tool lives in its own file under [tools/](tools/).

| Tool | What it does | External data |
| --- | --- | --- |
| `plan_running_week` ([week_planner.py](tools/week_planner.py)) | Scores every free 30-minute start time in the next 7 days, skipping class/work plus a 30-minute buffer. Scoring covers rain, storms, heat, cold, wind, darkness, UV and air quality. Then it searches every run→day assignment for the best week, penalizing hard workouts on back-to-back days. Saves the plan to the session. | Open-Meteo forecast + air quality, Nominatim geocoding |
| `get_todays_run` ([todays_run.py](tools/todays_run.py)) | Reads the saved plan for today/tomorrow/a weekday. Breaks the workout into segments with target paces (e.g. warm-up, 6 x 800m, cool-down). Re-checks the forecast and suggests a better free time if the weather got worse or the slot passed. | Open-Meteo |
| `find_running_routes` ([routes.py](tools/routes.py)) | Finds parks and 400m tracks near the start and walks real routes there and around them. Fits laps (or a turnaround) to the target distance and measures climb. Ranks routes for the workout: tracks/flat loops for intervals, big loops for long runs. Warns when the run starts after dark. | OpenStreetMap Overpass, OSRM foot router, Open-Meteo elevation and sunset |
| `build_run_playlist` ([playlist.py](tools/playlist.py)) | Estimates cadence (steps/min) from pace, or uses the runner's own. Picks electronic genres whose tempo sits there (house → techno → hard techno → hardstyle → drum & bass). Pulls real per-track tempo data and fills the run's duration. Half-time tracks count as 2 steps per beat. Energy builds through the playlist. | ReccoBeats (track tempo/energy), Deezer previews in the UI |

Error handling: tools raise `ToolError` with an instruction for the model (e.g. *"No
weekly plan exists in this session yet. Ask the runner for…"*). `run_tool` turns every
failure into an `{"error": ...}` result, so the chat never crashes. Flaky public APIs get
retries (Overpass mirrors, ReccoBeats rate limits) and in-memory caches.

## How sessions work

`sessions[session_id]` holds the message history, exactly as in the starter.
`session_state[session_id]` holds what tools remember: the saved week plan. Only
`plan_running_week` and `get_todays_run` receive it. The harness injects it, so the model
never sees or sends it. Separate sessions (or the **New session** button) get separate plans.

## Frontend

[index.html](index.html) renders each tool call as a card: a week grid, a workout bar, a
Leaflet route map, and a playlist with Deezer previews and a
metronome at the target BPM. Every card has a **raw call & result** drawer that shows the
exact args and result.

## Run locally

1. A GCP project with billing and the Vertex AI API enabled.
2. `gcloud auth application-default login`
3. `uv run app.py`, then open http://localhost:8000

## Deploy

Cloud Run, continuous deployment from GitHub. Build with the repo's `Dockerfile`; a
`Procfile` is there too for the buildpacks option. The app listens on `$PORT` when Cloud
Run sets it. The Cloud Run service account needs the **Vertex AI User** role.

## Limits

- Routes go to parks and tracks that OpenStreetMap knows about. Overpass, the public
  OpenStreetMap service, is sometimes overloaded; the tool retries and then tells the
  runner to try again.
- Cadence is estimated from pace (roughly 157 spm at 10:00/mi, 172 at 7:00/mi). Pass your
  watch's number for an exact match.
- Sessions live in memory, so a Cloud Run restart clears them.
