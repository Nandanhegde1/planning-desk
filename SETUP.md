# Setup guide

This guide was written for the Microsoft Foundry setup the project was built
against. The deployed app now runs on Gemini's free tier instead: the model
settings are in `.env.example` and the hosting in `HOSTING.md`. Everything here
about the tools, the MCP servers and the checks still applies.

Read this once end to end before typing anything. It will make more sense than
following it blind.

---

## What you are actually setting up

Four things run on your machine and talk to each other.

**A web page.** An ordinary chat box in your browser. It sends what you type to
the server and shows what comes back.

**A server.** A Python program that receives your message, decides what to do,
and streams the answer back. Inside it is the agent loop: it asks the model a
question, and if the model says "I need the weather for Bengaluru", the server
goes and gets it, hands the answer back to the model, and repeats until the
model has enough to reply. That loop is the thing your assignment is really
testing, and it is written by hand in `app/agent.py`.

**Three tool servers.** Small separate programs, one for weather and
feasibility, one for exchange rates, one for destinations and web search. They
start automatically when the main server starts, and between them they offer 18
tools. They speak MCP, which is just an agreed format for "here are the tools I
have" and "run this tool with these inputs". The main server discovers what they
can do by asking them, not by having it hardcoded.

**Your Foundry model.** Lives in Azure, not on your machine. The server sends it
the conversation and the list of available tools; it replies with either an
answer or a request to use a tool.

Nothing else needs installing. The weather and exchange rate data come from free
public APIs that need no signup.

---

## Before you start

You need Python 3.11 or newer. Check with `python3 --version`.

You need your Foundry resource in the Azure portal, with a chat model already
deployed. gpt-4o-mini, gpt-4.1-mini and gpt-4o all work. An embeddings model
will not, because it cannot call tools.

The commands below assume a Unix-style shell: Git Bash or WSL on Windows, or
the built-in terminal on macOS and Linux. Where a Windows path differs, it is
called out. There is a `run.ps1` for PowerShell if you would rather stay there.

One thing to know up front: on Windows a virtualenv puts its executables in
`.venv/Scripts/`, and everywhere else in `.venv/bin/`. Nearly every setup
problem on Windows traces back to that one difference.

Budget about forty minutes.

---

## Part 1 — Get the code onto your machine

**Step 1.** Unzip `planning-desk.zip` somewhere you can find it, then open a
terminal in that folder.

```bash
cd planning-desk
```

**Step 2.** Make your settings file.

```bash
cp .env.example .env
```

*What this does.* It makes a copy of the settings template and calls it `.env`.

*Why.* The template is part of the project and is safe to share, because it has
blanks where the passwords go. Your copy is where you type the real key. The
project is set up to never upload `.env` anywhere, so your Azure key stays on
your machine. If you typed your key into the template instead, it would end up
in your git history the first time you push, and you would have to go and
regenerate the key in Azure.

---

## Part 2 — Tell the project about your Foundry model

You need three pieces of information from the Azure portal. Get all three before
you start typing.

**Step 3.** Open your Foundry resource in the Azure portal. Find **Keys and
Endpoint** in the left-hand menu. Copy two things:

- **KEY 1** — a long string of letters and numbers.
- **Endpoint** — a web address. It looks like
  `https://something.openai.azure.com` or
  `https://something.services.ai.azure.com`.

Important: the endpoint must be the address *only*. If what you copied has
anything after the `.com` part — like `/openai/v1` or `/api/projects/whatever` —
delete that bit. The code adds the rest itself, and if you leave it on you get a
confusing "invalid URL" error later.

**Step 4.** Now go to **Deployments** in the same resource. Copy the deployment
**name**.

This is the name *you* typed when you created the deployment. It is often not
the model name. If you deployed gpt-4o-mini and called it `chat-model-dev`, then
`chat-model-dev` is what you need here.

*Why this matters.* On the newer Foundry endpoint, the deployment name is how
Azure knows which model you want. Getting this wrong gives a "404 deployment not
found" error, and it is the single most common thing people get stuck on.

**Step 5.** Optional, two minutes. Go to tavily.com, sign up with Google or
GitHub, and copy the API key from the dashboard. It starts with `tvly-`.

*Why.* Your assignment asks for an external search API. Tavily gives 1,000
searches a month free with no credit card. Brave, the one named in your brief,
now asks for a card at signup, which is why the project uses Tavily instead and
says so in the README. If you would rather skip search entirely, you can — the
rest of the project works without it.

**Step 6.** Open `.env` in a text editor and fill in the values you just
collected. You are looking for these lines:

```
LLM_PROVIDER=foundry
AZURE_OPENAI_ENDPOINT=https://your-resource.openai.azure.com
AZURE_OPENAI_API_KEY=paste-key-1-here
AZURE_OPENAI_DEPLOYMENT=paste-deployment-name-here

SEARCH_PROVIDER=tavily
TAVILY_API_KEY=tvly-paste-here

HTTP_USER_AGENT=planning-desk/0.1 (your.email@example.com)
```

Leave everything else as it is, apart from `LLM_PROVIDER`. `.env.example` sets it
to `openai`, for Gemini, so change it to `foundry`. With no value at all the app
falls back to `github`, which was retired and no longer answers.

That last line asks you to put your email in. The free map service the project
uses asks anyone calling it to say who they are, so they have someone to contact
if a program starts misbehaving. It is a courtesy, and it costs you nothing.

---

## Part 3 — Install the dependencies

**Step 7.** Create an isolated Python environment and install what the project
needs into it.

```bash
python -m venv .venv
```

Then activate it. The path differs by platform, which is the single most common
snag here:

```bash
source .venv/bin/activate        # macOS, Linux
source .venv/Scripts/activate    # Windows, in Git Bash or WSL
.\.venv\Scripts\activate         # Windows, in PowerShell
```

Your prompt should now start with `(.venv)`. Then:

```bash
python -m pip install -r requirements.txt
```

Use `python -m pip`, not bare `pip`. On Windows especially, `pip` and `python`
can resolve to different installations, and then packages land somewhere the
interpreter you are running never looks. The `python -m` form makes that
impossible: it installs into whichever Python is running the command.

*Why isolated.* So this project's libraries cannot clash with anything else
installed on your machine, and so the exact pinned versions are what run.

---

## Part 4 — Check every connection before running anything

**Step 8.**

```bash
python scripts/check_apis.py
```

*What this does.* It calls each outside service once and prints whether it
answered.

*Why do this first.* When something fails later, the useful question is "is it my
code or is it the network". This answers it in ten seconds instead of an hour.

You want to see `ok` against each line: your model, the map lookup, the weather
forecast, air quality, historical climate, exchange rates, and search.

One of those lines is worth understanding. "Search config reached server"
checks something subtle: the three tool programs are separate processes and do
not automatically receive the settings you typed into `.env`. Only the ones
listed in `app/config.py` are passed through. If you later add a setting of your
own and the tool servers cannot see it, that list is why.

If the model line fails, the message tells you which setting is wrong. Fix it
and run this again before moving on. If a weather or rates line fails but the
model works, you are probably behind a corporate network that blocks them — try
a personal connection or a phone hotspot.

---

## Part 5 — Start it

**Step 9.** With the virtualenv still active:

```bash
python -m uvicorn app.main:app --reload --port 8000
```

There are also scripts that do the whole thing — create the environment,
install, check the APIs, and start — if you are beginning from scratch on
another machine:

```bash
./run.sh          # macOS, Linux, Git Bash, WSL
.\run.ps1         # Windows PowerShell
```

**Step 10.** Open `http://localhost:8000` in your browser.

The two assignments are separate tabs across the top, and separate URLs:
`?mode=planning` for feasibility and destinations, `?mode=currency` for exchange
rates. Switching tabs keeps the conversation and changes which tools the model
can see. `?mode=all` gives it everything.

Look at the grey strip along the top. It should show your model name and the
tool count for the tab you are on: **11 tools** on Plans, **12** on Currencies
and **18** with `?mode=all`. If it shows fewer, or names a server as offline,
one of the three tool servers failed to start. The terminal says why.

**Step 11.** In a second terminal, activate the virtualenv again and run the
tests.

```bash
python -m pytest -q
```

Expect `187 passed` in about four minutes. These need no internet and no API key.
They check the feasibility rules, the trend maths, the file generation, the
three tool servers, and the safety limits on the loop.

*Why run them.* If you change anything later and something breaks, these tell
you what and where.

---

## Part 6 — See it work

Type these into the chat box in order. Watch the left-hand column of each
answer — that is the list of tools it called, in order, with how long each took.
Click any of them to see exactly what went in and what came back.

**Assignment 1, question 1 — is a match feasible.**

> Is a cricket match at Kanteerava Stadium in Bengaluru feasible this Saturday 5 to 8pm? The ground has no floodlights.

You should see `check_activity_window` fire and a verdict appear. Ask
*"why did you say that"* and it will call `explain_thresholds` and show you the
actual numbers behind the decision.

**Assignment 1, question 2 — is a trip feasible, and what is better.**

> I am planning to travel to Munnar for four days starting next Friday. Is that a good plan?

> What would be better?

Watch that second answer. "Better" means two different things and both tools
exist: `suggest_better_windows` changes *when*, and
`suggest_alternative_destinations` changes *where*. A good answer offers both.

**Assignment 1, question 3 — destinations by cost and season.**

> Where can I go in December on 5000 rupees a day near the coast?

The answer must say the cost figures are estimates. If it presents them as
prices, that is a bug worth reporting.

**Assignment 2, all four steps in one conversation so each builds on the last.**

> Build me a two-year USD to INR spreadsheet with a trendline.

> Show me that trendline as a chart.

> Now compare USD to INR against INR to GBP and INR to EUR on one chart.

That third one is the interesting step. Those pairs do not share a base
currency, so it should call `compare_pairs` rather than `get_rate_series`, and
the chart it produces should be indexed to 100 — because USD/INR is around 88
and INR/GBP is around 0.0095, and on a shared axis the second line would be flat
against the bottom. The answer should explain that rather than just do it.

> Show that as a bar chart instead.

> Now put everything in a Word document.

Download the spreadsheet and click a cell in one of the trend columns. You
should see a formula, not a number. That means the spreadsheet recalculates by
itself if someone edits the data — worth pointing out if you demo this. Check the
Methodology tab too; it records the source, the date pulled, and the fact that
non-euro pairs are crosses computed through the euro.

---

## Part 7 — Make it yours before you submit

This part matters more than the setup.

**Step 12.** Open `README.md` and rewrite the section called "Decisions worth
defending" in your own words. Especially the paragraph about why you did not use
Foundry Agent Service. That is the section an interviewer will ask about, and an
argument you cannot defend out loud is worse than no argument.

**Step 13.** Open `core/rules.py`. This is the file that decides whether a plan
is feasible. Near the top is a table of numbers — how much rain is too much, how
strong a wind is too strong, and so on. Change at least two of them to numbers
you would argue for.

*Why.* This file is the reason your project is not just a chat wrapper. The
model does not judge feasibility; this does. Being able to say "these are my
thresholds and here is why I picked them" is the strongest thing you can say
about the whole project.

After changing them, run `python -m pytest tests/test_rules.py`. If a test now
fails, that is the system working correctly — it is telling you a rule changed
behaviour.

**Step 14.** Open `core/destinations_seed.json`. The cost figures in it are
rough estimates I put there as placeholders. Either replace them with real
researched numbers, or leave them — the project already labels them as estimates
everywhere they appear. What you must not do is present them as real prices.

**Step 15.** Put it on GitHub.

```bash
git init
git add -A
git commit -m "MCP planning assistant"
git status
```

Before you push, check that `git status` does not mention `.env` anywhere. It
should not, because that file is already excluded. This is the check that stops
you publishing your Azure key.

---

## Part 8 — Put it online

First, something that will save you time: **this will not work on Netlify or
Vercel.** Those hosts run web pages and small short-lived functions. This
project keeps three tool programs running continuously and writes files to disk,
which those platforms cannot do. It is not a settings problem, so do not spend an
evening on it.

You need a host that runs a container. Two options.

**Render.** The live app runs here, on the free plan. `render.yaml` sets the
service up as a Blueprint, and `HOSTING.md` has the steps, including the
Cloudflare Worker that weather calls go through.

**Azure Container Apps** — makes sense because your model is already in Azure.

```bash
az login
az containerapp up --name planning-desk --resource-group YOUR-RG \
  --source . --ingress external --target-port 7860
```

Then in the portal, open the container app, go to **Secrets**, and add your
Foundry key there. Then go to **Containers**, edit, and add the environment
variables `LLM_PROVIDER`, `AZURE_OPENAI_ENDPOINT` and `AZURE_OPENAI_DEPLOYMENT`,
with the key pointing at the secret you just made. Set minimum replicas to zero
so it costs nothing while nobody is using it.

**Or, if it only needs to survive one demo call**, skip hosting entirely. Run it
on your laptop and open a temporary public link:

```bash
cloudflared tunnel --url http://localhost:8000
```

That gives you a web address anyone can open, for as long as you leave it
running.

---

## When something goes wrong

| What you see | What it means |
|---|---|
| `No module named 'dotenv'` or `No module named 'pytest'` | The interpreter running your command is not the one pip installed into. Use `python -m pip install -r requirements.txt`, which always targets the same Python. |
| "could not find a place called ..." for a place that exists | Name the town or city as well: "Cubbon Park, Bengaluru". Venue lookup uses OpenStreetMap and falls back to the enclosing city, but a bare venue name with no city is ambiguous everywhere. |
| `.venv/bin/activate: No such file` on Windows | Windows puts them in `Scripts`, not `bin`. Use `source .venv/Scripts/activate`. |
| 404, deployment not found | `AZURE_OPENAI_DEPLOYMENT` is not your deployment name. Check the Deployments page again. |
| 401 from the model | Wrong key, or your endpoint has extra path on the end. |
| Invalid URL | Endpoint has a trailing slash or still has `/openai/v1` attached. Trim it to the host. |
| Header shows fewer tools than expected | Normal: Plans exposes 11 and Currencies 12. Only "Everything" shows all 18. If a tab shows zero, a server crashed; the terminal says why. |
| Weather fails, model works | Network is blocking those addresses. Try a hotspot. |
| Runs out of memory when drawing a chart | Host has under 512 MB of RAM. |
| Model replies but never calls a tool | The deployed model does not support tool calling. Deploy gpt-4o-mini instead. |
| `'temperature' does not support 0.2 with this model` | You deployed a reasoning model. Set `LLM_TEMPERATURE=1` in `.env`, or take the current `app/llm.py`, which detects this and drops the parameter by itself. |
| Search says it is not configured, but your key is in `.env` | The tool servers are separate programs and receive only the settings listed in `SERVER_ENV_KEYS` in `app/config.py`. Add the variable there. |

If you get stuck, copy the exact output from `check_apis.py` or from the
terminal running the server, and that will point straight at the cause.
