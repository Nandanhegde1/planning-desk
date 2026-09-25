# Planning desk

A chat interface that answers questions about plans by calling real tools over
MCP, and a second mode that pulls exchange-rate history and turns it into
spreadsheets, documents and charts.

Both assignments run on one codebase. They share the chat interface, the agent
loop and the MCP host; they differ only in which MCP server the model reaches
for.

No orchestration framework is used. The agent loop is one function, `run_turn` in
`app/agent.py`, about 150 lines including its four ceilings, and it was written by
hand, which is the point of the exercise.

Other documents: `SPECIFICATION.md` walks every function with the implementation
behind it, and is the one to read alongside a demo. `SETUP.md` is the slow install
guide, `HOSTING.md` covers deployment and the accepted limits.

---

## Run it

Requires Python 3.11 or newer. `SETUP.md` walks through the same thing at a
slower pace, with the reason for each step.

```bash
cp .env.example .env                 # then add a Gemini key
python -m venv .venv
source .venv/bin/activate            # .venv/Scripts/activate on Windows
pip install -r requirements.txt
python scripts/check_apis.py         # confirms every external service answers
python -m uvicorn app.main:app --reload --port 8000
```

`./run.sh` (or `.\run.ps1` on PowerShell) does all of that in one command.

The deployed provider is Gemini on Google's free tier, through the
OpenAI-compatible endpoint:

```
LLM_PROVIDER=openai
OPENAI_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai
OPENAI_API_KEY=<key from aistudio.google.com>
LLM_MODEL=gemini-3.5-flash-lite
LLM_TEMPERATURE=1
```

Switching providers is a few lines in `.env` and no code change, because every
supported host speaks the OpenAI chat-completions shape. It was built against
gpt-5-mini on Microsoft Foundry, and that provider is still supported:

```
LLM_PROVIDER=foundry
AZURE_OPENAI_ENDPOINT=https://<resource>.openai.azure.com
AZURE_OPENAI_API_KEY=<key from Keys and Endpoint>
AZURE_OPENAI_DEPLOYMENT=<the deployment name, not the model name>
```

The endpoint is the resource host only, with no path and no trailing slash. The
request goes to `/openai/v1/chat/completions`, which uses implicit versioning,
so no `api-version` parameter is sent and the deployment name travels in the
`model` field rather than the URL. The older `/models` route belongs to the
Azure AI Inference API, which Microsoft retired on 26 August 2026; the `azure`
provider keeps the legacy per-deployment path for resources created before the
v1 route existed. GitHub Models, once the free fallback, was retired on 30 July
2026.

Web search is optional. Tavily gives 1,000 credits a month with no card, which
is the reason it is the default rather than Brave: Brave withdrew its card-free
tier in February 2026, so pinning the project to it would mean anyone reviewing
this needs a card on file. With `SEARCH_PROVIDER=none` the search tool reports
itself unavailable and everything else still works.

---

## What it answers

Assignment 1, through the `feasibility` and `discovery` servers:

*Is an outdoor or indoor match on this date and time workable?* The forecast,
air quality and daylight for that exact window are fetched, scored against a
fixed threshold policy, and returned as a verdict of go, caution or no_go with
the readings that produced it.

*Is a trip to this place on these dates workable, and what would be better?*
Day-by-day verdicts across the range. "Better" splits two ways and both are
covered: a different time, from a scan of neighbouring windows ranked by score,
and a different place, from nearby destinations scored for the same month against
historical climate. Beyond the sixteen-day forecast horizon the tools fall back
to seasonal normals from five years of reanalysis, and say so.

*Which destinations suit this budget and season?* A shortlist filtered by daily
rupee budget and tags, where each candidate's month is scored against historical
climate data for its own coordinates.

Assignment 2, through the `fx` server:

Two years of daily USD to INR rates into a workbook, and a least-squares
trendline over them.

Then INR to GBP and INR to EUR alongside, on one comparable axis. This needed a
second tool rather than a wider call: USD/INR and INR/GBP do not share a base
currency, so a single base-and-quotes request cannot express the comparison.
`compare_pairs` takes arbitrary pairs, groups them by base to keep the request
count down, and merges them into one series so every chart and export describes
the same dataset.

Finally, any of five chart shapes on request, chosen by the model from that same
series: indexed, line, trend, monthly bar, or small multiples.

---

## Two entry points, one host

The brief describes two chat interfaces. Rather than two applications, this is
one host with two modes, chosen by tab or by URL: `/?mode=planning` and
`/?mode=currency`.

A mode narrows which servers' tools reach the model, 11 for plans and 12 for
currencies against 18 in total. That matters for accuracy as much as for
tidiness: a small model choosing between 18 tools makes more wrong turns than
one choosing between 11. `/?mode=all` exposes everything, which is the demo that
the servers compose.

The loop, the host, the interface and the servers are shared. Adding a mode is
one entry in `config.MODES`.

Requirement coverage is checked by a script rather than asserted here:

```bash
python scripts/check_coverage.py
```

## Architecture

```
browser (single HTML file, SSE)
      │
      ▼
FastAPI host ──────────────► model endpoint (Foundry / GitHub Models / Ollama)
      │  agent loop: tools → results → repeat, with hard ceilings
      ▼
MCP client ── stdio ──┬── feasibility server ── Open-Meteo forecast, air, archive
                      ├── fx server ─────────── Frankfurter (ECB reference rates)
                      └── discovery server ──── Nominatim, Brave or Tavily
```

Each MCP server is a separate process launched over stdio. The host completes
the initialize handshake, calls `tools/list`, and translates the returned JSON
Schema into the tool format the model expects. Nothing about the tools is
hardcoded in the host: adding a server is one line in `config.MCP_SERVERS`.

Discovery runs once at startup rather than per request, because spawning three
Python processes on every message would dominate response time.

```
app/     agent loop, MCP client, model client, HTTP surface, the interface
servers/ three MCP servers
core/    rules engine, weather, exchange rates, exports, destinations
tests/   95 tests, none of which need a network or an API key
scripts/ check_apis.py preflight
samples/ example output, generated from a synthetic fixture
```

---

## Decisions worth defending

**The model does not decide feasibility.** `core/rules.py` does, from a
threshold table anyone can read and argue with. The model chooses the tool,
supplies the arguments, and narrates the verdict it is handed. This buys three
things a chat wrapper cannot fake: the same question gives the same answer
twice; the policy is unit-testable without a model, a key or a network; and
every rejection comes with the reading and the line it crossed rather than an
opinion.

Peaks decide, not averages, because one ruinous hour ruins a match. PM2.5 lines
follow the CPCB national AQI breakpoints — 61 for moderate, 91 for poor.
A measured reading from seasonal normals cannot produce a blocker, only a
caution, because a normal is a distribution and not a prediction. Sunset is the
exception: it is astronomy rather than weather, so an unlit ground after dark is
blocked whatever the data source.

**MCP is used as a protocol, not as a label.** The servers run as child
processes and speak JSON-RPC over stdio. `tests/test_mcp_host.py` spawns all
three, lists their tools, calls them, and asserts the schemas survive the trip.
Calling the Python functions directly would have been simpler and would have
proved nothing.

**The loop is hand-written, and Foundry Agent Service was considered and
rejected.** Agent Service would manage the thread, the tool selection and the
loop, which is precisely the part under assessment; it expects MCP servers
reachable on the public internet or inside the same virtual network, so these
stdio servers would need deploying before anything worked; and its MCP support
has shipped as a gated preview limited to specific regions. Foundry is still
used, as the model host, which is the part of it that adds value here.

**Every loop has a ceiling.** Eight steps, twelve tool calls, sixty thousand
tokens per turn, forty-five seconds per tool. Each is a hard stop, each is
configurable in `.env`, and the interface shows which one fired.
`tests/test_agent_loop.py` drives a scripted model that tries to loop forever and
asserts it is stopped.

**Tool results are data, not instructions.** Search snippets and fetched pages
are third-party text. The system prompt states that anything resembling a
command inside a tool result is to be described, never obeyed. This is the
cheapest mitigation for prompt injection through retrieved content and it does
not depend on the model being well behaved.

**Failure degrades rather than fabricates.** A tool that cannot reach its API
returns a structured error, and the model is instructed to report the outage. A
missing forecast is a better answer than an invented one. Air quality is treated
as a secondary signal: if that service is down, the verdict is still produced
without it.

**The workbook ships formulas, not results.** Index, thirty-day average and the
fitted trend are Excel expressions over the rate column, computed with `SLOPE`
and `INTERCEPT`, so deleting a year of rows moves the statistics. A workbook of
frozen numbers is a screenshot with extra steps.

---

## Data sources, and what is wrong with them

Place lookup uses two indexes because they cover different things. Open-Meteo's
geocoder is a gazetteer of populated places: it answers "Bengaluru" and has no
entry for a park or a stadium. OpenStreetMap indexes those, so a venue the first
source cannot see is looked up there. If neither has it, the name is retried
against whatever encloses it, since weather at a park and at the city centre a
few kilometres away is the same forecast, and the response records which name
was actually used.

**Open-Meteo** for forecast, air quality, geocoding and historical reanalysis.
No key required. The forecast horizon is sixteen days; past that the code
switches to seasonal normals and labels every downstream result `climatology`.
The archive returns `wind_gusts_10m` alongside the rest. The climatology path used
to substitute mean wind speed for a gust, which is roughly a fifth of the real
peak, so the gust lines could never be reached from normals. Where a gust really
is absent the field is omitted and the rules skip it.

**Frankfurter**, republishing European Central Bank reference rates. No key, no
account, daily rates since 1999. Two properties shape everything built on it,
and both are stated in the workbook, the document and every chart:

The ECB publishes euro reference rates, so a USD/INR figure is a cross computed
through EUR rather than a traded quote. It is fine for trend work and wrong for
settling a transaction.

Rates exist for TARGET business days only; weekends and holidays have no row.
Plotting the raw series against a calendar axis silently compresses time, so the
series is reindexed onto every calendar day, gaps carry the previous rate
forward, and the fill count is reported.

**The destination catalogue** in `core/destinations_seed.json` supplies
candidates and coordinates. It deliberately carries no "best months" field,
because a hand-typed season list is an assertion nobody can check; the month is
scored instead against five years of reanalysis for that coordinate. The budget
bands are rounded planning estimates, are flagged as such in every response, and
should be replaced with a sourced dataset before anyone acts on them.

**Comparability.** USD/INR sits near 88 and INR/GBP near 0.0095. On a shared
axis the smaller line is a flat streak along the bottom, so the three-currency
comparison plots an index rebased to 100 at the series start. The shapes are the
question; the levels are not.

---

## Tests

```bash
python -m pip install -r requirements-dev.txt   # adds ruff
python -m pytest -q                             # 95 tests, about four minutes
python -m ruff check .
```

Nothing in the suite needs a network, an API key or a model. The rules engine is
driven with synthetic hourly readings; the trend maths is checked against a
series with a known slope; the exporters are run and the resulting files
reopened and inspected; the MCP tests spawn the real servers; and the loop tests
use a scripted stand-in for the model so budgets and history shape can be
asserted deterministically.

`tests/test_conversation_memory.py` covers multi-turn memory the way a user
meets it: two and three messages on one session, sessions isolated from each
other, the transcript a reload repaints, reset, and the trimming cases —
including three turns that each spend the whole tool budget, and the four-step
currency conversation whose third message is "show that as a different graph".
It drives the real HTTP endpoints through `TestClient` with a scripted model, so
it runs in about twelve seconds and records exactly what the model was shown on
each call. Run that file alone while working on the loop:

```bash
python -m pytest tests/test_conversation_memory.py -q
```

---

## Hosting

This will not run on Netlify, Vercel or any other static or function host, and
the reason is structural rather than a matter of configuration. The MCP servers
are long-lived child processes held open for the lifetime of the parent, tools
write files to disk that are then served back, and responses stream over SSE.
A serverless function is a short-lived process with an ephemeral filesystem and
no way to keep three subprocesses alive between invocations. Something that
holds a container is required.

It runs as a free Render web service built from the Dockerfile, configured
entirely by `render.yaml`, so the whole deployment costs nothing. `HOSTING.md`
has the steps, the free-tier limits that come with it, and what happened to the
earlier Azure deployment. Keys are set on the service when the Blueprint is
created, and nothing secret goes into the image.

For a link that only has to survive a demo call, run locally and put a
Cloudflare quick tunnel in front of it. No account, no deployment:

```bash
cloudflared tunnel --url http://localhost:8000
```

---

## Resource use

The parent process and the three MCP servers together hold about 300 MB
resident at rest, measured with all servers started and idle. matplotlib is
imported inside the render function rather than at module load, which keeps
roughly 60 MB off the fx server until a chart is actually drawn. A 512 MB
container is enough; anything smaller is not.

## What "production ready" means here, and what it does not

The code is linted clean under ruff with a broad ruleset, formatted, and covered
by 95 tests that need no network. Resources are bounded: the agent loop has step,
tool and token ceilings; the session store evicts on age and count; generated
files are pruned on age and count; every outbound call has a timeout, a retry
budget and a cache. Failures return structured errors rather than raising.

There are two health endpoints, and the split matters when deploying.
`/api/health/live` returns 200 whenever the process is serving, and is what a
container probe should watch. `/api/health` is the readiness view and reports 503
when the model provider or an MCP server is unavailable — pointing a probe at it
turns a missing key into a revision that never activates.

Every response carries a content security policy that confines the page to its
own origin, plus `nosniff`, `DENY` framing and `no-referrer`. The page makes no
third-party requests at all, so it renders inside a container with no egress.
The headers are applied by a pure-ASGI wrapper rather than
`BaseHTTPMiddleware`, which would sit between the SSE generator and the client
and can hold chunks.

Four things a production deployment would need that this deliberately does not
have, because each is a different system rather than a missing line:

**No authentication.** Every endpoint is open, including `/files`, so anyone who
can reach the host can read any generated file. Put it behind an identity proxy
before exposing it to anything real.

**Coarse rate limiting only.** A per-caller window and a daily cap across all
callers bound what anyone can spend, but there is no identity behind either, so
a determined caller can still use up the day's allowance for everyone.

**Single worker only.** Sessions and the MCP host live in this process. Running
more workers gives each one its own conversations and its own child servers.
Fixing this means moving sessions to Redis and the MCP host behind a service.

**No observability beyond logs.** There are no traces, no metrics and no request
IDs. For a demo the interface's own trace rail is the debugging surface.

## Known limits

Model calls are not streamed token by token. The loop needs the complete
response to know whether a tool was requested, and parsing streamed tool-call
deltas would have added meaningful complexity for a cosmetic gain. Progress is
streamed instead: every tool call appears in the interface as it happens.

Sessions live in process memory, so a restart loses the conversation and a
second worker would not share it. A single-process demo does not justify the
storage. A page reload does survive: the browser keeps the session id in
`sessionStorage` and repaints the conversation from `/api/session/{id}`, because
a reload that came back blank while the server still held the history was
indistinguishable from the chat having forgotten everything.

Conversation history is trimmed by turn, not by message. One turn can append a
dozen tool messages, so a flat message window evicts what the user said in order
to keep tool output nobody asked about again — which is exactly what breaks a
follow-up like "show that as a bar chart instead". Recent turns
(`AGENT_TOOL_DETAIL_TURNS`) keep their full tool payloads; older turns keep
every spoken message and stub their tool results; turns past
`AGENT_MAX_HISTORY_TURNS` are dropped whole, never mid-turn.

The page is read from disk once at startup rather than on every request, so
editing `app/web/index.html` against an already-running server shows no change
until it restarts. `run.sh` and `run.ps1` pass `--reload`, which restarts on
save, so this only surprises you when running uvicorn by hand.

`MCPHost.start` and `stop` must run in the same task, because the transport
opens an anyio cancel scope and anyio will not let another task close it.
FastAPI's lifespan satisfies this; a naive pytest fixture does not, which is why
the tests use the context manager inside the test body.

The venue side of question one is thin. Opening hours, floodlights and bookings
come from web search when a provider is configured, and from the user otherwise.
A places API with real venue records would be the next thing to add.
