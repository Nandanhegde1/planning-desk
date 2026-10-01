"""Configuration. Everything tunable lives here, nothing is read from env elsewhere."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

# --- model -----------------------------------------------------------------
# openai  : any OpenAI-compatible base url, which is how Gemini is reached
# foundry : Azure AI Foundry models endpoint
# azure   : Azure OpenAI deployment endpoint
# ollama  : a local model
# github  : GitHub Models, retired on 30 July 2026. Kept so an old .env fails
#           with a clear message, but the endpoint no longer serves anything.
PROVIDER = os.environ.get("LLM_PROVIDER", "github").lower()
MODEL = os.environ.get("LLM_MODEL", "openai/gpt-4.1-mini")
TEMPERATURE = float(os.environ.get("LLM_TEMPERATURE", "0.2"))
# Reasoning models only. minimal or low keeps an agent loop responsive; medium
# and high spend thinking budget on tool selection, which does not need it.
# Left empty the parameter is not sent at all.
REASONING_EFFORT = os.environ.get("LLM_REASONING_EFFORT", "").strip().lower()

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
AZURE_ENDPOINT = os.environ.get("AZURE_OPENAI_ENDPOINT", "").rstrip("/")
AZURE_API_KEY = os.environ.get("AZURE_OPENAI_API_KEY", "")
AZURE_DEPLOYMENT = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "")
# Only the legacy "azure" provider needs this. The foundry provider uses the
# /openai/v1 route, which versions itself.
AZURE_API_VERSION = os.environ.get("AZURE_OPENAI_API_VERSION", "2024-10-21")
OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434/v1").rstrip("/")

# --- agent limits ----------------------------------------------------------
MAX_STEPS = int(os.environ.get("AGENT_MAX_STEPS", "8"))
MAX_TOOL_CALLS = int(os.environ.get("AGENT_MAX_TOOL_CALLS", "12"))
MAX_TOKENS_PER_TURN = int(os.environ.get("AGENT_MAX_TOKENS_PER_TURN", "60000"))
TOOL_TIMEOUT_SECONDS = float(os.environ.get("AGENT_TOOL_TIMEOUT", "45"))
MAX_TURN_SECONDS = float(os.environ.get("AGENT_MAX_TURN_SECONDS", "170"))
FINAL_ANSWER_RESERVE_SECONDS = float(os.environ.get("AGENT_FINAL_ANSWER_RESERVE", "50"))
LLM_RETRY_ATTEMPTS = int(os.environ.get("LLM_RETRY_ATTEMPTS", "3"))
MAX_HISTORY_TURNS = int(os.environ.get("AGENT_MAX_HISTORY_TURNS", "12"))
TOOL_DETAIL_TURNS = int(os.environ.get("AGENT_TOOL_DETAIL_TURNS", "2"))
STALE_TOOL_RESULT_CHARS = int(os.environ.get("AGENT_STALE_TOOL_RESULT_CHARS", "2000"))
MAX_HISTORY_MESSAGES = int(os.environ.get("AGENT_MAX_HISTORY", "200"))
MAX_TOOL_RESULT_CHARS = int(os.environ.get("AGENT_MAX_TOOL_RESULT_CHARS", "20000"))

# --- mcp servers -----------------------------------------------------------
MCP_SERVERS: dict[str, list[str]] = {
    "feasibility": [sys.executable, str(ROOT / "servers" / "feasibility_server.py")],
    "fx": [sys.executable, str(ROOT / "servers" / "fx_server.py")],
    "discovery": [sys.executable, str(ROOT / "servers" / "discovery_server.py")],
}

# Forwarded to the MCP servers. A stdio server starts with a minimal environment
# and inherits nothing else, so a key missing from this list is invisible to the
# server that needs it, however carefully it was set in .env.
SERVER_ENV_KEYS = [
    "SEARCH_PROVIDER",
    "BRAVE_API_KEY",
    "TAVILY_API_KEY",
    "HTTP_USER_AGENT",
    "HTTP_TIMEOUT_SECONDS",
    "CACHE_DIR",
    "OPEN_METEO_PROXY",
    # Read inside core/, which runs in the child rather than the host. Missing
    # here they were dead in the container, where .dockerignore excludes .env and
    # each server's own load_dotenv finds nothing. They matched their defaults, so
    # nothing misbehaved, but the knobs could not be turned in production.
    "CLIMATE_CONCURRENCY",
    "OUTPUT_RETENTION_HOURS",
    "MAX_OUTPUT_FILES",
]

# --- retention -------------------------------------------------------------
SESSION_TTL_MINUTES = int(os.environ.get("SESSION_TTL_MINUTES", "120"))
MAX_SESSIONS = int(os.environ.get("MAX_SESSIONS", "200"))
# A ceiling on what one caller can spend. /api/chat has no credential.
RATE_LIMIT_TURNS = int(os.environ.get("RATE_LIMIT_TURNS", "20"))
RATE_LIMIT_WINDOW_SECONDS = float(os.environ.get("RATE_LIMIT_WINDOW_SECONDS", "300"))
# A ceiling across every caller together. A free model tier gives one daily
# quota to the whole deployment, and a per-caller limit does not stop many
# callers, or one caller changing address, from spending all of it. Counted over
# a rolling 24 hours; 0 turns it off.
DAILY_TURN_CAP = int(os.environ.get("DAILY_TURN_CAP", "100"))
# One line shown in the status strip when set. Free tiers that may use inputs
# for training ask that nothing personal is submitted, so this is where to say it.
PUBLIC_NOTICE = os.environ.get("PUBLIC_NOTICE", "").strip()
OUTPUT_RETENTION_HOURS = float(os.environ.get("OUTPUT_RETENTION_HOURS", "6"))
MAX_OUTPUT_FILES = int(os.environ.get("MAX_OUTPUT_FILES", "200"))

# Two entry points over one host. A mode narrows which servers' tools reach the
# model: fewer tools means fewer wrong choices, and it keeps each assignment
# demonstrable on its own. The servers, the loop and the interface are shared.
MODES: dict[str, dict[str, Any]] = {
    "planning": {
        "label": "Plans",
        "servers": ["feasibility", "discovery"],
        "brief": "Feasibility of matches and trips, and destinations by cost and season.",
    },
    "currency": {
        "label": "Currencies",
        "servers": ["fx", "discovery"],
        "brief": "Exchange rate history, trendlines, spreadsheets and charts.",
    },
    "all": {
        "label": "Everything",
        "servers": ["feasibility", "fx", "discovery"],
        "brief": "Every tool at once.",
    },
}
DEFAULT_MODE = os.environ.get("DEFAULT_MODE", "planning")

OUTPUT_DIR = ROOT / "outputs"
OUTPUT_DIR.mkdir(exist_ok=True)
WEB_DIR = Path(__file__).resolve().parent / "web"


def provider_ready() -> tuple[bool, str]:
    """Whether a model can actually be called, and what to fix if not."""
    if PROVIDER == "github":
        return False, (
            "GitHub Models was retired on 30 July 2026. Set LLM_PROVIDER=openai and point "
            "OPENAI_BASE_URL at an OpenAI-compatible host, such as Gemini."
        )
    if PROVIDER == "foundry":
        ready = bool(AZURE_ENDPOINT and AZURE_API_KEY and AZURE_DEPLOYMENT)
        return ready, (
            "Set AZURE_OPENAI_ENDPOINT (ending in .openai.azure.com or "
            ".services.ai.azure.com, no trailing path), AZURE_OPENAI_API_KEY, and "
            "AZURE_OPENAI_DEPLOYMENT to your deployment name."
        )
    if PROVIDER == "azure":
        ready = bool(AZURE_ENDPOINT and AZURE_API_KEY and AZURE_DEPLOYMENT)
        return ready, "Set AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_API_KEY and AZURE_OPENAI_DEPLOYMENT."
    if PROVIDER == "openai":
        return bool(OPENAI_API_KEY), "Set OPENAI_API_KEY in .env."
    if PROVIDER == "ollama":
        return True, ""
    return False, f"LLM_PROVIDER={PROVIDER} is not one of github, foundry, azure, openai, ollama."
