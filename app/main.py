"""HTTP surface: one page, one streaming chat endpoint, one file route.

Sessions and the MCP host live in this process, so run a single worker.
"""

from __future__ import annotations

import json
import logging
import secrets
import time
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    StreamingResponse,
)
from pydantic import BaseModel, Field

from app import config
from app.agent import repair_history, run_turn
from app.mcp_host import MCPHost
from core.exports import prune_outputs

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")

host = MCPHost()


@dataclass
class Session:
    history: list[dict[str, Any]] = field(default_factory=list)
    touched: float = field(default_factory=time.monotonic)


sessions: dict[str, Session] = {}


def _evict() -> None:
    """Make room in the store: expired sessions first, then the oldest.

    Belongs to the store rather than to the lookup. While it lived inside
    _get_session, minting a session and then reading it back could evict the one
    just created.
    """
    cutoff = time.monotonic() - config.SESSION_TTL_MINUTES * 60
    for key in [k for k, v in sessions.items() if v.touched < cutoff]:
        del sessions[key]

    if len(sessions) >= config.MAX_SESSIONS:
        for key in sorted(sessions, key=lambda k: sessions[k].touched)[
            : len(sessions) - config.MAX_SESSIONS + 1
        ]:
            del sessions[key]


def _get_session(session_id: str) -> Session:
    """Fetch a session, evicting expired and surplus ones first.

    The caller must already hold a known id. Creating one under whatever
    string arrived meant the id space was client-chosen, so a caller could
    seed a short guessable id and have the server keep it alive.
    """
    _evict()
    session = sessions.get(session_id)
    if session is None:
        raise KeyError(session_id)
    session.touched = time.monotonic()
    return session


def _new_session() -> str:
    """A server-minted id. token_urlsafe(32) is 256 bits, where the previous
    uuid4().hex[:12] was 48, and the id is the only thing guarding a
    conversation."""
    _evict()
    session_id = secrets.token_urlsafe(32)
    sessions[session_id] = Session()
    return session_id


@asynccontextmanager
async def lifespan(_: FastAPI):
    await host.start()
    removed = prune_outputs()
    if removed:
        log.info("removed %d stale output files", removed)

    ready, hint = config.provider_ready()
    if not ready:
        log.warning("model provider not configured: %s", hint)
    if host.failed:
        log.error("mcp servers that did not start: %s", host.failed)
    log.info("%d tools ready across %d servers", len(host.tools), len(host.describe()["servers"]))

    yield
    await host.stop()


app = FastAPI(title="Planning desk", version="1.0.0", lifespan=lifespan)

# The page is one self-contained file, so it is read once rather than per request.
# encoding is explicit: the copy contains en dashes and a rupee sign, and Windows
# would otherwise decode UTF-8 bytes as cp1252.
INDEX_HTML = (config.WEB_DIR / "index.html").read_text(encoding="utf-8")

# The page carries its own <style> and <script>, so script-src and style-src have
# to allow inline. Everything else is locked to same-origin, which is what actually
# matters here: no third-party origin can be contacted and no external script can
# run. The one place markup is built from model output escapes & < > first
# (see the answer branch in index.html), so there is no injection point.
SECURITY_HEADERS = [
    (
        b"content-security-policy",
        b"default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
        b"script-src 'self' 'unsafe-inline'; connect-src 'self'; font-src 'self'; "
        b"object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
    ),
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
    # Ignored over plain http, so it is safe to send unconditionally.
    (b"strict-transport-security", b"max-age=31536000; includeSubDomains"),
]


class SecurityHeaders:
    """Pure ASGI, so response bodies are never buffered.

    BaseHTTPMiddleware would sit between the SSE generator and the client and can
    hold chunks; this only rewrites the response-start message.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: dict) -> None:
            if message["type"] == "http.response.start":
                message["headers"] = list(message.get("headers", [])) + SECURITY_HEADERS
            await send(message)

        await self.app(scope, receive, send_with_headers)


app.add_middleware(SecurityHeaders)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    session_id: str | None = Field(default=None, max_length=64)
    mode: str = Field(default=config.DEFAULT_MODE, max_length=32)


class SessionRequest(BaseModel):
    """Reset carries no message. Sharing ChatRequest made it reject every call
    with a 422 for a missing message field."""

    session_id: str | None = Field(default=None, max_length=64)


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return HTMLResponse(INDEX_HTML)


@app.get("/api/health/live")
async def liveness() -> JSONResponse:
    """Always 200 while the process is serving.

    Deliberately separate from /api/health, which reports 503 when the model
    provider is unconfigured. A probe pointed at that endpoint would turn a
    missing key into a revision that never activates.
    """
    return JSONResponse({"status": "alive"})


@app.get("/api/health")
async def health() -> JSONResponse:
    ready, hint = config.provider_ready()
    healthy = ready and not host.failed
    return JSONResponse(
        status_code=200 if healthy else 503,
        content={
            "status": "ok" if healthy else "degraded",
            "provider": config.PROVIDER,
            "model": (config.AZURE_DEPLOYMENT if config.PROVIDER == "foundry" else config.MODEL),
            "provider_ready": ready,
            "hint": "" if ready else hint,
            "notice": config.PUBLIC_NOTICE,
            "mcp": host.describe(),
            "tool_count": len(host.tools),
            "modes": {
                name: {"label": spec["label"], "tools": len(host.tools_for(name))}
                for name, spec in config.MODES.items()
            },
            "default_mode": config.DEFAULT_MODE,
            "sessions": len(sessions),
            # provider_ready only checks that a key is set, so it stays true through a
            # spent quota or a revoked key. These two say what a visitor would hit.
            # Reported, not judged: neither is a reason to restart the container, so
            # neither changes the status code. Both live in this process and reset
            # on a restart or a deploy.
            "last_model_error_at": _last_model_error_at,
            "daily_cap_reached": _daily_cap_reached(),
            "limits": {
                "max_steps": config.MAX_STEPS,
                "max_tool_calls": config.MAX_TOOL_CALLS,
                "max_tokens_per_turn": config.MAX_TOKENS_PER_TURN,
                "daily_turn_cap": config.DAILY_TURN_CAP,
            },
        },
    )


@app.get("/files/{filename}")
async def download(filename: str) -> FileResponse:
    # Confine to the output directory; a filename is user-influenced input.
    path = (config.OUTPUT_DIR / filename).resolve()
    if path.parent != config.OUTPUT_DIR.resolve() or not path.is_file():
        raise HTTPException(status_code=404, detail="no such file")
    return FileResponse(path)


# A fixed window per client. /api/chat is the one endpoint that spends model
# tokens and it carries no credential, so without this anyone holding the URL can
# empty the quota. Deliberately coarse: the aim is a ceiling on damage, not fair
# queueing.
_hits: dict[str, list[float]] = {}


def _client_key(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    return (forwarded.split(",")[0].strip() or (request.client.host if request.client else "?"))


def _over_limit(key: str) -> bool:
    now = time.monotonic()
    window = now - config.RATE_LIMIT_WINDOW_SECONDS
    recent = [t for t in _hits.get(key, []) if t > window]
    if len(_hits) > 4096:
        _hits.clear()
    recent.append(now)
    _hits[key] = recent
    return len(recent) > config.RATE_LIMIT_TURNS


# Every accepted turn from every caller, for the daily ceiling. A free model
# tier's quota belongs to the deployment, so the per-caller window above cannot
# protect it on its own.
_daily: deque[float] = deque()
DAY_SECONDS = 86400.0


def _turns_in_last_day() -> int:
    cutoff = time.monotonic() - DAY_SECONDS
    while _daily and _daily[0] <= cutoff:
        _daily.popleft()
    return len(_daily)


def _over_daily_cap() -> bool:
    if config.DAILY_TURN_CAP <= 0:
        return False
    if _turns_in_last_day() >= config.DAILY_TURN_CAP:
        return True
    _daily.append(time.monotonic())
    return False


def _daily_cap_reached() -> bool:
    """For /api/health. Unlike _over_daily_cap, asking does not count a turn."""
    return config.DAILY_TURN_CAP > 0 and _turns_in_last_day() >= config.DAILY_TURN_CAP


# When a model call last failed, in UTC, for /api/health.
_last_model_error_at: str | None = None


def _note_model_error() -> None:
    global _last_model_error_at
    _last_model_error_at = datetime.now(UTC).isoformat(timespec="seconds")


@app.post("/api/chat")
async def chat(request: ChatRequest, http_request: Request) -> StreamingResponse:
    if _over_limit(_client_key(http_request)):
        raise HTTPException(
            status_code=429,
            detail=(
                f"Too many questions in a short time. Wait a moment: the limit is "
                f"{config.RATE_LIMIT_TURNS} per "
                f"{config.RATE_LIMIT_WINDOW_SECONDS:.0f}s."
            ),
        )
    if _over_daily_cap():
        raise HTTPException(
            status_code=429,
            detail=(
                f"This demo has used its allowance of {config.DAILY_TURN_CAP} questions "
                "for the last 24 hours. Try again later."
            ),
        )

    # Resume only an id this process minted. Anything else starts a new
    # conversation rather than being taken at face value.
    session_id = request.session_id or ""
    try:
        session = _get_session(session_id)
    except KeyError:
        session_id = _new_session()
        session = sessions[session_id]

    async def stream():
        yield f"data: {json.dumps({'type': 'session', 'session_id': session_id})}\n\n"
        try:
            mode = request.mode if request.mode in config.MODES else config.DEFAULT_MODE
            async for event in run_turn(host, session.history, request.message, mode):
                if event.get("type") == "done" and event.get("reason") == "model_error":
                    _note_model_error()
                yield f"data: {json.dumps(event, default=str)}\n\n"
        except Exception:
            # Full detail to the log, a reference to the browser. Upstream
            # errors carry request URLs with keys in them, filesystem paths,
            # and up to 200 characters of third-party response body.
            reference = secrets.token_hex(4)
            log.exception(
                "turn failed for session %s [ref %s]", session_id, reference
            )
            safe = {
                "type": "error",
                "text": (
                    "Something went wrong on our side and the turn stopped. "
                    f"Quote reference {reference} if you report it."
                ),
            }
            yield f"data: {json.dumps(safe)}\n\n"
            yield f"data: {json.dumps({'type': 'done', 'reason': 'exception'})}\n\n"
        finally:
            # Runs on a clean finish, on an exception, and when the browser aborts
            # the stream, which is the case that matters: a turn cut between
            # requesting tools and recording their results leaves a history the
            # model API rejects, and every later message in the session then
            # failed until it was reset.
            dangling = repair_history(session.history)
            if dangling:
                log.info("session %s: closed %d interrupted tool calls", session_id, dangling)
            session.touched = time.monotonic()

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/session/{session_id}")
async def transcript(session_id: str) -> JSONResponse:
    """The conversation so far, so a reload can repaint it.

    The session id is the only thing the browser persists; without this the
    server still remembered the conversation but the page came back blank, which
    reads to the user as the chat having forgotten everything.

    Tool traffic is left out: only what was said is replayable.
    """
    session = sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="no such session")

    turns = [
        {"role": message["role"], "text": message["content"]}
        for message in session.history
        if message["role"] in ("user", "assistant")
        and message.get("content")
        and not message.get("tool_calls")
    ]
    return JSONResponse({"session_id": session_id, "turns": turns})


@app.post("/api/reset")
async def reset(request: SessionRequest) -> dict[str, str]:
    if request.session_id:
        sessions.pop(request.session_id, None)
    return {"status": "cleared"}
