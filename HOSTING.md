# Hosting

## What is deployed

One free Render web service builds the Dockerfile in this repository and runs
it. The model is Gemini on Google's free tier, reached through the `openai`
provider. `render.yaml` holds the whole configuration.

```
host      Render web service, free plan, Singapore, Docker runtime
app       one worker, liveness probe on /api/health/live
model     gemini-3.5-flash-lite, free tier
```

Everything costs nothing, within these limits:

- **Render's free plan** gives 512 MB and 0.1 CPU. The app and its three MCP
  servers peaked near 400 MB when measured with charts, a workbook and a document
  rendered, so there is room but not much. The service sleeps after 15 minutes
  without a request and takes about a minute to wake, and sleeping clears every
  conversation. A workspace gets 750 free instance hours a month, enough for one
  service that never sleeps.
- **Gemini** does not publish free-tier quotas. They are per project and visible
  at aistudio.google.com/rate-limit. Flash-Lite is the model with room for a
  public demo; 3.8 Flash allows only a few turns a day for free.
- **The free tier may use inputs for training** and human reviewers may read
  them. `PUBLIC_NOTICE` puts a line in the status strip saying so. Google's terms
  also restrict offering free-tier apps to users in the EEA, Switzerland and the
  UK. That is a question for whoever runs the deployment, not something the code
  settles.

`DAILY_TURN_CAP` bounds what all visitors together can spend, because the quota
belongs to the whole deployment. A turn makes one to five model calls, so keep
the cap under a fifth of the model's free requests per day.

## Deploying

1. Sign in at render.com with GitHub. No card is needed for the free plan.
2. **New > Blueprint**, and pick this repository. Render reads `render.yaml`.
3. Paste the two values it asks for: `OPENAI_API_KEY`, a key from
   aistudio.google.com, and `TAVILY_API_KEY`, from tavily.com. Everything else
   is already in the file.
4. Apply. The first build takes a few minutes. Every push to `main` redeploys.

### Verify

```bash
curl https://<service>.onrender.com/api/health
```

Expect `"status": "ok"` and `"tool_count": 18`. Health returning ok means the
tools started and a key is present, not that the key works, so ask one real
question in the page as well.

### Keeping it awake

For a link that should answer at once, point a free uptime monitor, such as
UptimeRobot, at `/api/health/live` every five minutes. One service kept awake
all month uses about 744 of the 750 free hours. It also means conversations
survive between visits, since the process no longer restarts after every quiet
spell.

## One worker, always

Sessions and the three MCP servers live inside the app process. A second worker
or a second instance would hold its own separate conversations and its own
child servers, so the Dockerfile starts uvicorn with `--workers 1` and the free
plan runs one instance.

## Earlier deployments

Until September 2026 the app ran on Azure App Service
(`planning-desk-live.azurewebsites.net`) against a Foundry deployment of
gpt-5-mini. Both stopped when the subscription's free credit ran out, because
Azure disables the whole subscription at that point, not just the model. If the
subscription is ever reactivated, remove what is left of that deployment with:

```bash
az group delete --name planning-desk-rg2 --yes
```

Two other routes this file used to describe no longer work at $0. Hugging Face
now requires PRO to create a Docker Space and does not accept Indian cards for
it, and GitHub Models was retired on 30 July 2026.

## A tunnel, for a screen share

No deployment. Run locally and expose it.

```bash
# terminal one
python -m uvicorn app.main:app --port 8000

# terminal two
cloudflared tunnel --url http://localhost:8000
```

`cloudflared` prints a public `.trycloudflare.com` URL and needs no account. The
link dies when you stop the command.

## Known limits

Deliberate choices, not oversights.

**`/api/chat` is unauthenticated.** Anyone with the URL can spend the model
quota. The per-caller window (`RATE_LIMIT_TURNS` per `RATE_LIMIT_WINDOW_SECONDS`)
and `DAILY_TURN_CAP` bound the damage; the per-turn ceilings
(`AGENT_MAX_STEPS`, `AGENT_MAX_TOOL_CALLS`, `AGENT_MAX_TOKENS_PER_TURN`) bound
one turn.

**Generated files are served without authorization.** `/files/{name}` is
confined to the output directory and rejects traversal, but names are
`{prefix}-{timestamp}.{ext}`, so one user's workbook is fetchable by another who
guesses the name. Files are pruned after `OUTPUT_RETENTION_HOURS` (6 by
default). Add a random component in `core/exports.py::_stamp` if that matters.

**Conversations and generated files do not survive a restart.** Sessions are an
in-process dictionary and `outputs/` is the container filesystem, so a deploy,
a sleep or a crash starts everyone clean. The browser keeps only the session id
and repaints the transcript from `/api/session/{id}` while the process lives.
