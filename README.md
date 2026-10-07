# Cadence: a running coach that works around your life

Cadence is a web chat agent for runners with a busy schedule. Give it your week (classes,
work, the runs you want), and it slots each run into free time, picking the hours with the
best weather. Say *"I want to do my run today"* and it pulls up the workout, maps a route
of the right distance from your door, and builds a playlist whose BPM matches your stride.

Built on the class `gemini-web-tool-calling` starter: same harness loop, session store,
and `/chat` response shape (`response`, `session_id`, `tool_calls`). Gemini via LiteLLM.

## Sample queries

Run in order, in one session: each builds on the last.

1. **Plan the week:** *"I'm in Morningside Heights, NYC. I have class Mon and Wed
   10am-4pm and work Tue and Thu 9-5. This week I want an easy 4 miler, a 5 mile tempo, 6
   miles of intervals and a 10 mile long run. My easy pace is about 9:30. When should I
   run?"* → a 7-day grid with a time slot per run, rated against the forecast.
2. **Run today:** *"Hey, I want to do my run today. What is it and where should I go?"*
   → today's workout broken into paced segments, plus a map of an out-and-back route.
3. **Music:** *"Make me a playlist for that run."* → pick a genre that fits your cadence,
   get tracks at your target BPM with previews and a metronome.

## Tools

| Tool | What it does | External data |
| --- | --- | --- |
| `plan_running_week` ([tools/week_planner.py](tools/week_planner.py)) | Scores every free slot in the next 7 days around class/work, weighing rain, heat, wind, darkness, air quality, and avoiding back-to-back hard days. Saves the plan to the session. | Open-Meteo, Nominatim |
| `get_todays_run` ([tools/todays_run.py](tools/todays_run.py)) | Reads the saved plan for a given day, breaks the workout into paced segments, and re-checks the forecast. | Open-Meteo |
| `find_running_routes` ([tools/routes.py](tools/routes.py)) | A route on real walking paths for the requested distance: out-and-back or one way, toward a park or landmark the runner names or one nearby, or ending at a specific finish. | OpenStreetMap, OSRM, Valhalla |
| `build_run_playlist` ([tools/playlist.py](tools/playlist.py)) | Matches cadence to BPM (one step per beat, or two on half-time tracks), fills the run's duration with real tempo data. | ReccoBeats, Deezer (previews) |

Tool errors raise `ToolError` with an instruction for the model, so a bad argument or a
flaky API never crashes the chat. It just gets relayed back for a retry or a fix.

## Sessions & frontend

`sessions[session_id]` holds the message history; `session_state[session_id]` holds the
saved week plan. Separate sessions (or **New session**) get separate plans and history.

[index.html](index.html) renders each tool call as a card (week grid, route map, genre
picker, playlist with a metronome), each with a **raw call & result** drawer showing the
exact args and result.

## Run locally

1. A GCP project with billing and the Vertex AI API enabled.
2. `gcloud auth application-default login`
3. `uv run app.py`, then open http://localhost:8000

## Deploy

Cloud Run, built from the repo's `Dockerfile` (listens on `$PORT`). The service account
needs the **Vertex AI User** role.

## Limits

- Routes depend on what OpenStreetMap has mapped nearby for parks and landmarks, and a
  named finish must be reachable on foot within 5% of the run's distance.
- Cadence is estimated from pace unless you give your watch's number.
- Sessions live in memory, so a Cloud Run restart clears them.
- Previews and cover art come from Deezer, then iTunes as a fallback. Some niche tracks
  (ReccoBeats mirrors Spotify's catalog) aren't licensed for preview on either, so the
  play button stays disabled. The BPM match is still accurate; there's just no clip.
