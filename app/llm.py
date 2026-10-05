"""Model client.

Every supported provider speaks the OpenAI chat-completions shape, so only the
URL and the auth header differ.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from app import config

log = logging.getLogger("llm")


class LLMError(RuntimeError):
    pass


# Model families disagree about which tuning parameters they accept. Reasoning
# models (GPT-5, o-series) reject temperature; chat models reject
# reasoning_effort. Rather than maintain a table of model names that goes stale,
# send what is configured, and if the host names a parameter in a 400, drop that
# one for the rest of the process. Only the first call pays for the discovery.
OPTIONAL_PARAMS = ("temperature", "reasoning_effort")
_disabled: set[str] = set()


def _endpoint() -> tuple[str, dict[str, str]]:
    provider = config.PROVIDER
    if provider == "github":
        return (
            "https://models.github.ai/inference/chat/completions",
            {"Authorization": f"Bearer {config.GITHUB_TOKEN}"},
        )
    if provider == "foundry":
        # The /openai/v1 route: implicit versioning, so no api-version, and the
        # deployment name travels in the model field. The older /models route
        # belongs to the Azure AI Inference API, retired 26 August 2026.
        #
        # Both auth headers are sent because which one a resource accepts
        # depends on how it was created, and an ignored header is harmless.
        return (
            f"{config.AZURE_ENDPOINT}/openai/v1/chat/completions",
            {
                "api-key": config.AZURE_API_KEY,
                "Authorization": f"Bearer {config.AZURE_API_KEY}",
            },
        )
    if provider == "azure":
        # Legacy route for resources predating /openai/v1.
        return (
            f"{config.AZURE_ENDPOINT}/openai/deployments/{config.AZURE_DEPLOYMENT}"
            f"/chat/completions?api-version={config.AZURE_API_VERSION}",
            {"api-key": config.AZURE_API_KEY},
        )
    if provider == "ollama":
        return f"{config.OLLAMA_BASE_URL}/chat/completions", {}
    return (
        f"{config.OPENAI_BASE_URL}/chat/completions",
        {"Authorization": f"Bearer {config.OPENAI_API_KEY}"},
    )


def _model_name() -> str:
    """On Foundry the model field carries the deployment name, not the model id."""
    if config.PROVIDER == "foundry" and config.AZURE_DEPLOYMENT:
        return config.AZURE_DEPLOYMENT
    return config.MODEL


async def _post_once(url: str, headers: dict[str, str], body: dict[str, Any]) -> httpx.Response:
    try:
        async with httpx.AsyncClient(timeout=90) as client:
            return await client.post(url, json=body, headers=headers)
    except httpx.HTTPError as exc:
        raise LLMError(f"could not reach the model host: {exc}") from exc


def _retry_after(response: httpx.Response, attempt: int) -> float:
    """How long to wait, preferring the host's own instruction."""
    header = response.headers.get("retry-after", "")
    if header.strip().isdigit():
        # Respect it, but never stall a turn on a host asking for minutes.
        return min(float(header.strip()), 10.0)
    return 0.8 * (2**attempt)


async def _post(url: str, headers: dict[str, str], body: dict[str, Any]) -> httpx.Response:
    """POST with a bounded retry on transient failures.

    A 429 or a 5xx is worth trying again; a small deployment rate limiting one
    request out of a burst was ending the whole turn. Everything else, including
    a 400 or a 401, is returned as-is for the caller to interpret, because
    retrying will not change the answer.
    """
    last: httpx.Response | None = None
    for attempt in range(config.LLM_RETRY_ATTEMPTS):
        response = await _post_once(url, headers, body)
        if response.status_code != 429 and response.status_code < 500:
            return response

        last = response
        if attempt < config.LLM_RETRY_ATTEMPTS - 1:
            delay = _retry_after(response, attempt)
            log.info(
                "model host returned %s; retrying in %.1fs (attempt %d of %d)",
                response.status_code,
                delay,
                attempt + 2,
                config.LLM_RETRY_ATTEMPTS,
            )
            await asyncio.sleep(delay)

    return last if last is not None else response


async def complete(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    *,
    temperature: float | None = None,
) -> dict[str, Any]:
    """One model turn. Returns {"message": ..., "usage": ..., "finish_reason": ...}."""
    ready, hint = config.provider_ready()
    if not ready:
        raise LLMError(f"no model credentials configured. {hint}")

    url, headers = _endpoint()
    body: dict[str, Any] = {"model": _model_name(), "messages": messages}
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    if "temperature" not in _disabled:
        body["temperature"] = config.TEMPERATURE if temperature is None else temperature
    if config.REASONING_EFFORT and "reasoning_effort" not in _disabled:
        body["reasoning_effort"] = config.REASONING_EFFORT

    response = await _post(url, headers, body)

    if response.status_code == 400:
        rejected = [p for p in OPTIONAL_PARAMS if p in body and p in response.text]
        if rejected:
            for name in rejected:
                log.info("model rejected %s; omitting it from now on", name)
                _disabled.add(name)
                body.pop(name, None)
            response = await _post(url, headers, body)

    if response.status_code == 401:
        raise LLMError("the model host rejected the credentials; check the key in .env")
    if response.status_code == 429:
        raise LLMError(
            f"the model host is rate limiting, and still was after "
            f"{config.LLM_RETRY_ATTEMPTS} attempts; wait a moment and retry"
        )
    if response.status_code >= 400:
        # The body goes to the log, not into the error. The error reaches the
        # browser, and the session transcript, and a host's body is not for them.
        log.warning("model host returned %s: %s", response.status_code, response.text[:400])
        raise LLMError(f"model host returned {response.status_code}; the detail is in the log")

    # A host that is gone or misrouted can answer 200 with something that is not
    # JSON: the retired GitHub Models endpoint returns a plain "OK", and a proxy
    # returns an HTML page. Parsing that raised past every LLMError handler and
    # surfaced as an unexplained failure.
    try:
        payload = response.json()
    except ValueError:
        kind = response.headers.get("content-type", "unknown type")
        raise LLMError(
            f"model host returned {response.status_code} with a non-JSON body ({kind}); "
            "check the base url"
        ) from None
    if not isinstance(payload, dict):
        log.warning("model host returned an unexpected body: %s", str(payload)[:300])
        raise LLMError("model host returned an unexpected body; the detail is in the log")
    choices = payload.get("choices") or []
    if not choices:
        log.warning("model host returned no choices: %s", str(payload)[:300])
        raise LLMError("model host returned no choices; the detail is in the log")

    return {
        "message": choices[0].get("message", {}),
        "finish_reason": choices[0].get("finish_reason"),
        "usage": payload.get("usage", {}) or {},
    }
