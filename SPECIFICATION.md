# Planning desk — functional specification, with the implementation behind each part

This document walks through what the application does, in the order you would
demonstrate it, and puts the technical mechanism underneath each function. It is
written to be read alongside a live demo: the left-hand question is always "what
does a user get", and the right-hand answer is always "what actually produces
it, and where in the code".

Two assignments are served by one codebase. They share the chat interface, the
agent loop and the MCP host, and differ only in which MCP servers the model can
reach.

---

## 1. The shape of the thing

**Functionally.** One page. You type a question in plain language, the answer
streams back, and structured readings render as visuals rather than as prose. A
row of tabs picks which job you are doing.

**Technically.** A FastAPI process serves a single HTML file and one streaming
chat endpoint. Three MCP servers run as long-lived child processes of that
process, speaking JSON-RPC over stdio. The model is reached over the OpenAI
chat-completions shape, so the provider is a configuration line rather than a
code change.

```
browser (one HTML file, Server-Sent Events)
      │
      ▼
FastAPI host ──────────────► model endpoint (Foundry)
      │  agent loop: ask → run tools → feed results back → repeat
      ▼
MCP client ── stdio ──┬── feasibility server ── Open-Meteo forecast, air quality, archive
                      ├── fx server ─────────── Frankfurter, ECB reference rates
                      └── discovery server ──── Nominatim, Tavily
```

| Concern | Where it lives | 
|---|---|
| HTTP surface, sessions, streaming | `app/main.py` |
| The agent loop and its ceilings | `app/agent.py` |
| MCP client, tool discovery, dispatch | `app/mcp_host.py` |
| Model client, provider differences, retry | `app/llm.py` |
| All configuration | `app/config.py` |
| Threshold policy and verdicts | `core/rules.py` |
| Weather, rates, exports, destinations | `core/` |
| The three MCP servers | `servers/` |
| Interface | `app/web/index.html` |

No orchestration framework is used. The loop is one custom function,
`run_turn` in `app/agent.py`, about 150 lines including the four ceilings and the
event stream. Keeping it in plain code, with no framework, is the point of the exercise.

---

## 2. Assignment 1 — is this plan feasible

### 2.1 An indoor or outdoor activity at a place and time

**Functionally.** Ask "Is a football match at Cubbon Park, Bengaluru workable
this Saturday 5 to 8pm? No floodlights." You get a verdict of **go**, **caution**
or **no_go**, and for every reading that mattered, a gauge showing the measured
value against the threshold it was tested on. A daylight band shows your window
against sunrise and sunset. The prose says which one or two readings decided it
and what to do instead.

**Technically, and this is the part worth defending.** The model does not decide
feasibility. `core/rules.py` does, from a threshold table anyone can read and
argue with:

| Reading | Caution | Blocker |
|---|---|---|
| Rain probability | 45% | 70% |
| Rainfall | 1.0 mm/h | 4.0 mm/h |
| Wind gusts | 40 km/h | 60 km/h |
| Feels-like high | 34°C | 40°C |
| Feels-like low | 8°C | 2°C |
| PM2.5 | 61 µg/m³ | 91 µg/m³ |
| UV index | 9 | 11 |

Those are the outdoor numbers; indoor and travel each have their own column, so
rain stops mattering indoors while air quality still does. The PM2.5 lines follow
the CPCB national AQI breakpoints rather than being invented.

Peaks decide, not averages, because one ruinous hour ruins a match.

The model's job is to choose the tool, supply the arguments, and report the
verdict it was handed. It is explicitly forbidden from overturning one. That buys
three things a chat wrapper cannot: the same question gives the same answer
twice, the policy is unit-testable with no model and no network, and every
rejection arrives with the reading and the line it crossed.

Sunset handling is a separate reason code rather than a threshold, because "runs
past sunset with no floodlights" is a structural blocker, not a measurement.

### 2.2 A trip on given dates, and better alternatives

**Functionally.** Ask "I want four days in Munnar from next Friday. Is that
feasible, and where else could I go?" You get a day-by-day strip of verdicts
across the range, and "better" is answered in both senses it can mean: a
different **time**, from a scan of neighbouring windows ranked by score, and a
different **place**, from nearby destinations scored for the same month.

**Technically.** Three tools cooperate: `check_travel_plan` for the range,
`suggest_better_windows` for the time axis, `suggest_alternative_destinations`
for the place axis. The system prompt requires that a caution or no_go verdict is
followed by looking for alternatives in the same turn, so the user is not made to
ask twice.

Beyond the sixteen-day forecast horizon the tools fall back to seasonal normals
built from five years of reanalysis data, and say so in the answer. A measured
blocker from normals is softened to caution, because a normal is a distribution
and not a prediction. Daylight is the exception. Sunrise and sunset are computed
locally in `core/solar.py` with the NOAA algorithm, so they never depend on a
weather service and never degrade with distance, and an unlit ground after dark
is blocked whatever the source.

If a venue is not in the place index, the tool falls back to the enclosing city
and the answer states which location was actually used. That is a result, not a
failure.

### 2.3 Destinations by cost and season

**Functionally.** Ask "Where can I go in December on ₹5,000 a day, near the
coast?" You get a shortlist with an estimated daily budget band and a comfort
score for that month, labelled as estimates.

**Technically.** A curated catalogue in `core/destinations_seed.json` is filtered
by budget and tags, and each surviving candidate's month is scored against
historical climate data for its own coordinates. The cost bands are rounded
planning estimates and the prompt requires them to be labelled as such, because
presenting a catalogue figure as a live price would be a fabrication.

---

## 3. Assignment 2 — exchange rates into artefacts

### 3.1 Two years of USD to INR, into a spreadsheet

**Functionally.** Ask "Build me a two-year USD to INR spreadsheet with a
trendline." You get a downloadable `.xlsx` and a summary of the fit.

**Technically.** Rates come from the Frankfurter API over European Central Bank
reference rates. The ECB publishes on working days only, so the series is
reindexed onto a real calendar with weekend values carried forward, and the
provenance line reports how many days were published versus carried. The
trendline is a least-squares fit computed in `core/fx.py`, not asked of the
model.

The workbook ships **live formulas**, not frozen numbers, so the trend recomputes
if you edit the data in Excel. That is the difference between a report and a
spreadsheet.

### 3.2 Three currencies on one comparable axis

**Functionally.** Ask to add INR to GBP and INR to EUR alongside. You get one
chart with all three and a note about how they were made comparable.

**Technically.** This needed a second tool rather than a wider call, and the
reason is the interesting part: USD/INR and INR/GBP do not share a base
currency, so a single base-and-quotes request cannot express the comparison.
`compare_pairs` accepts arbitrary pairs, groups them by base to keep the request
count down, and merges them into one series.

Pairs of very different magnitude are compared in **indexed** form, each series
rebased to 100 at its start, because plotting 83 against 0.0095 on one axis
communicates nothing. The answer says why, once.

### 3.3 A different graph on request

**Functionally.** Ask "Show that as a monthly bar chart instead." The chart
changes; the data does not.

**Technically.** Five chart shapes are available: indexed, line, trend, monthly
bar, and small multiples. The model picks one and calls `render_chart` against
the **same** `series_id`.

This is where conversation memory earns its place. Fetching once and reusing the
`series_id` is what guarantees every chart, workbook and document describes the
same dataset. If the id were lost, a follow-up would refetch, mint a new id, and
the artefacts would quietly stop agreeing with each other. Section 6.2 covers how
the id is kept alive.

### 3.4 A Word document

**Functionally.** Ask to put the comparison into a Word document. You get a
`.docx` with the charts embedded and the figures in a table.

**Technically.** `python-docx` builds it, `matplotlib` renders the charts with the
`Agg` backend so it works headless in a container, and matplotlib is imported
inside the render function rather than at module load, which keeps roughly 60 MB
of resident memory off the process until a chart is actually drawn.

---

## 4. The agent loop

**Functionally.** You see progress as it happens: which tool is running, how long
each took, and an expandable trace with the raw payloads. Nothing is hidden.

**Technically.** `app/agent.py` runs: ask the model, dispatch the tools it
requested, feed the results back, repeat until it answers or a ceiling is hit.
Events stream to the browser over SSE as they occur.

Four ceilings bound a turn, and the fourth exists because the first three
multiply out badly:

| Ceiling | Default | Why |
|---|---|---|
| Steps | 8 | A model that only ever calls tools must still terminate |
| Tool calls | 12 | Bounds cost and runaway fan-out within a turn |
| Tokens per turn | 60,000 | Bounds spend |
| **Wall clock** | **150s** | The other three allow over an hour in the worst case |

Each tool call is additionally handed only the time the turn has **left**, so a
45-second tool timeout cannot overshoot a turn with 5 seconds remaining.

Tool failures are returned as data, never raised. A tool that times out or errors
produces a structured error the model must report, which is why an outage
surfaces as "the service failed" rather than as an invented number.

---

## 5. Two entry points over one host

**Functionally.** Three tabs. **Plans** covers assignment 1, **Currencies**
covers assignment 2, **Everything** exposes both. Switching tabs keeps your
conversation and says so.

**Technically.** A mode narrows which servers' tools reach the model: 11 tools
for Plans, 12 for Currencies, 18 for Everything. That is an accuracy measure as
much as a tidiness one, since a small model choosing among 18 tools makes more
wrong turns than one choosing among 11. Adding a mode is one entry in
`config.MODES`.

The mode travels with each request, so it changes which tools are offered on the
next question without disturbing the history.

---

## 6. Conversation memory

This is the part that took the most iteration, so it is worth being precise about
what was actually wrong.

### 6.1 What the user sees

Follow-ups work on pronouns. "What about Mumbai instead?" keeps the activity and
the time and changes only the city. "And if we started two hours earlier?" shifts
the window and keeps the city. A page reload continues the same conversation
rather than coming back blank. A **New conversation** button starts over
deliberately.

### 6.2 What was wrong, and what fixes it

Server-side memory was never broken; that was verified by driving two requests
through the HTTP layer and inspecting what the model was shown on the second. The
real faults were elsewhere:

- **The browser kept the session only in a variable.** A reload silently started
  a new conversation while the server still held the old one. The session id now
  persists, and `GET /api/session/{id}` replays what was said so the transcript
  is repainted. The mode persists with it, because restoring a currency
  conversation into the Plans tab left the fx tools unreachable.
- **History was trimmed by message count.** One turn can append a dozen tool
  messages, so a flat window evicted what the user said in order to keep tool
  output nobody would ask about again. Trimming is now by **turn**, and turns are
  dropped whole rather than mid-turn, which would orphan a tool result from the
  call that requested it.
- **Old tool results were replaced wholesale.** That discarded the `series_id`
  along with the numbers, so "put the comparison into a Word document" four turns
  later had to refetch and mint a new id. Results are now shrunk only when
  **bulky**, and shrinking preserves the handles a follow-up needs. A real fx
  summary is about 1,270 characters, under the threshold, so it survives whole.
- **Oversized results were cut mid-JSON.** A large payload reached the model as a
  broken document. Oversized results are now wrapped in a valid envelope that
  says plainly it is a fragment.

---

## 7. Guardrails

**Functionally.** Ask it to write a Python program and it declines in one
sentence and names what it can do instead. Ask a feasibility question in the
Currencies tab and it tells you which tab has that tool rather than improvising.

**Technically.** Both are enforced in the system prompt, and both were added
because live testing caught the opposite behaviour:

- **Scope.** The brief is two jobs. Asked for code it wrote code. The prompt now
  fences code, general knowledge, translation, drafting and self-explanation, and
  names the Python case explicitly rather than leaving it to inference.
- **No invented verdicts.** In Currencies mode, where the feasibility tools are
  not exposed, it ran a web search three times and issued its own "Caution" from
  scraped weather pages. The figures were real, so a naive grounding check passed
  it, but the **verdict** came from the model rather than the rules engine, which
  breaks the one guarantee the design rests on. A missing tool is now explicitly
  not something to work around, and `web_search` is fenced to venue facts such as
  opening hours, floodlights and booking.
- **No invented significance.** It named a 67% rain chance as a deciding reading
  for an **indoor** activity, and called PM2.5 of 51 "moderate" when the moderate
  line is 61. It now names only readings the tool flagged, and may not attach a
  band word the tool did not use.
- **Tool output is data, not instruction.** A search snippet containing something
  that looks like a command is described, never obeyed.

---

## 8. Operational behaviour

**Functionally.** Progress is visible, a turn can be stopped mid-flight, and a
failed or stopped turn offers to run again.

**Technically.**

- **Stop.** The Ask button becomes Stop while a turn is in flight. Aborting the
  fetch ends the SSE stream, which cancels the server-side generator with it. A
  deliberate stop is reported plainly rather than as an error.
- **Retry.** Transient 429 and 5xx responses from the model host are retried with
  backoff, honouring `Retry-After` but capping the wait so a host asking for
  minutes cannot hold a turn open. A 400 or 401 fails immediately, because
  retrying cannot change it. A rate limit on a small deployment is the single
  likeliest failure in normal use.
- **Two health endpoints, and the split matters.** `/api/health/live` returns 200
  whenever the process is serving and is what a container probe should watch.
  `/api/health` is the readiness view and returns 503 when the provider or an MCP
  server is unavailable. Pointing a probe at the latter turns a missing key into
  a deployment that never activates.
- **Bounded resources.** Sessions evict on age and count, generated files are
  pruned on age and count, and every outbound call has a timeout, a retry budget
  and a disk cache. Nominatim is throttled to one request a second in line with
  its usage policy.
- **Security headers.** Every response carries a content security policy
  confining the page to its own origin, plus `nosniff`, `DENY` framing and
  `no-referrer`. The page makes no third-party requests at all, so it renders
  inside a container with no outbound internet. The headers are applied by a
  pure-ASGI wrapper rather than `BaseHTTPMiddleware`, which would sit between the
  SSE generator and the client and can hold chunks.

---

## 9. Testing

107 tests, none of which need a network, an API key or a model.

| Area | What is asserted |
|---|---|
| `test_rules.py` | The threshold policy, driven with synthetic hourly readings |
| `test_fx_and_exports.py` | Trend maths against a known slope; exports reopened and inspected |
| `test_mcp_host.py` | All three servers spawned, tools listed and called, schemas survive |
| `test_agent_loop.py` | Dispatch, every ceiling, history shape, the wall clock |
| `test_conversation_memory.py` | Multi-turn memory over the real HTTP endpoints |
| `test_place_resolution.py` | Venue fallback to the enclosing city |
| `test_llm_client.py` | Provider routing, parameter negotiation, retry policy |

The model is replaced by a scripted stand-in, so the orchestration is tested
rather than the model. `test_conversation_memory.py` is the one worth showing: it
drives `/api/chat` twice on one session and records exactly what the model was
handed on the second call, which is the check that would have caught the original
memory complaint.

**What tests cannot cover, honestly stated.** The guardrails in section 7 are
model behaviour. A test can only assert the fence is present in the prompt; the
real evidence is live runs. Every guardrail described here was verified by
driving live prompts through the deployed application and reading the outputs,
including a grounding audit that cross-checks every number in the prose against
the numbers the tools actually returned. That audit has a known blind spot: a
verdict fabricated from real scraped numbers passes it, which is exactly how the
section 7 bug survived until the outputs were read by hand.

---

## 10. Deployment

Runs on Azure App Service at https://planning-desk-live.azurewebsites.net, Linux B3,
Python 3.12, Central India, one worker with Always On.

**Why one worker is a correctness requirement rather than a cost setting.** The
session store is an in-process dictionary and the three MCP servers are child
processes of the web process. A second worker would hold its own separate
conversations and its own child servers, so a user's follow-up could land on a
worker that never saw the first question.

**Why not a container image.** `az containerapp up --source` builds through ACR
Tasks, and the subscription rejects every Tasks request with
`TasksOperationsNotAllowed`, a standing restriction on new free-credit
subscriptions. With no Docker daemon available to build locally either, there was
no route to an image, so the platform builds the Python app from source instead.
A `Dockerfile` is in the repository and is correct; it simply could not be built
on this subscription.

**Why it cannot run on a function host.** The MCP servers are long-lived child
processes held open for the lifetime of the parent, tools write files to disk that
are then served back, and responses stream over SSE. A short-lived process with an
ephemeral filesystem cannot keep three subprocesses alive between invocations.

**Known limits, deliberately accepted for an internal demo.** No authentication,
so anyone with the URL can spend the model quota. Generated files are served
without authorization and their names are guessable. Sessions and generated files
do not survive a restart. Each is a single-line change if the deployment stops
being a demo, and each is recorded in `HOSTING.md`.

**One deployment gotcha worth knowing.** `index.html` is read once at startup
rather than per request, so a code deploy alone does not update the interface —
the worker has to recycle. Immediately after a restart, `/api/health/live` still
answers from the warm old worker, so liveness is not evidence that new code is
live. Probe for a marker from the new build instead.
