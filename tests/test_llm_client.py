"""Model client behaviour that does not need a live model host."""

import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config, llm

TEMPERATURE_REJECTION = json.dumps(
    {
        "error": {
            "message": (
                "Unsupported value: 'temperature' does not support 0.2 with this model. "
                "Only the default (1) value is supported."
            ),
            "param": "temperature",
        }
    }
)


def fake_transport(monkeypatch, responder, recorder):
    class FakeClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, json=None, headers=None):
            recorder.append({"url": url, "body": dict(json), "headers": headers or {}})
            return responder(json, url)

    monkeypatch.setattr(llm.httpx, "AsyncClient", FakeClient)


@pytest.fixture
def openai_provider(monkeypatch):
    monkeypatch.setattr(config, "PROVIDER", "openai")
    monkeypatch.setattr(config, "OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(config, "TEMPERATURE", 0.2)
    monkeypatch.setattr(llm, "_disabled", set())
    monkeypatch.setattr(config, "REASONING_EFFORT", "")


def ok(url):
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {"total_tokens": 5},
        },
        request=httpx.Request("POST", url),
    )


@pytest.mark.asyncio
async def test_a_model_that_rejects_temperature_is_retried_without_it(monkeypatch, openai_provider):
    """Reasoning models accept only the default temperature. One 400 should not
    end the turn."""
    sent = []

    def responder(body, url):
        if "temperature" in body:
            return httpx.Response(
                400, text=TEMPERATURE_REJECTION, request=httpx.Request("POST", url)
            )
        return ok(url)

    fake_transport(monkeypatch, responder, sent)
    result = await llm.complete([{"role": "user", "content": "hi"}])

    assert result["message"]["content"] == "ok"
    assert "temperature" in sent[0]["body"]
    assert "temperature" not in sent[1]["body"]


@pytest.mark.asyncio
async def test_the_parameter_is_dropped_for_the_rest_of_the_process(monkeypatch, openai_provider):
    """Only the first call pays for the discovery."""
    sent = []
    fake_transport(monkeypatch, lambda body, url: ok(url), sent)
    monkeypatch.setattr(llm, "_disabled", {"temperature"})

    await llm.complete([{"role": "user", "content": "hi"}])
    assert len(sent) == 1
    assert "temperature" not in sent[0]["body"]


@pytest.mark.asyncio
async def test_other_400s_are_not_silently_retried(monkeypatch, openai_provider):
    sent = []
    fake_transport(
        monkeypatch,
        lambda body, url: httpx.Response(
            400, text='{"error":"deployment not found"}', request=httpx.Request("POST", url)
        ),
        sent,
    )
    with pytest.raises(llm.LLMError, match="400"):
        await llm.complete([{"role": "user", "content": "hi"}])
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_credential_and_rate_limit_failures_are_readable(monkeypatch, openai_provider):
    for status, expected in [(401, "credentials"), (429, "rate limiting")]:
        sent = []
        fake_transport(
            monkeypatch,
            lambda body, url, s=status: httpx.Response(s, request=httpx.Request("POST", url)),
            sent,
        )
        with pytest.raises(llm.LLMError, match=expected):
            await llm.complete([{"role": "user", "content": "hi"}])


def test_foundry_targets_the_v1_route_with_the_deployment_as_model(monkeypatch):
    monkeypatch.setattr(config, "PROVIDER", "foundry")
    monkeypatch.setattr(config, "AZURE_ENDPOINT", "https://demo.openai.azure.com")
    monkeypatch.setattr(config, "AZURE_API_KEY", "k")
    monkeypatch.setattr(config, "AZURE_DEPLOYMENT", "my-deployment")

    url, headers = llm._endpoint()
    assert url == "https://demo.openai.azure.com/openai/v1/chat/completions"
    assert "api-version" not in url
    assert set(headers) == {"api-key", "Authorization"}
    assert llm._model_name() == "my-deployment"


@pytest.mark.asyncio
async def test_reasoning_effort_is_sent_only_when_configured(monkeypatch, openai_provider):
    sent = []
    fake_transport(monkeypatch, lambda body, url: ok(url), sent)

    await llm.complete([{"role": "user", "content": "hi"}])
    assert "reasoning_effort" not in sent[0]["body"]

    monkeypatch.setattr(config, "REASONING_EFFORT", "low")
    await llm.complete([{"role": "user", "content": "hi"}])
    assert sent[1]["body"]["reasoning_effort"] == "low"


@pytest.mark.asyncio
async def test_a_rate_limit_is_retried_before_giving_up(monkeypatch, openai_provider):
    """A small deployment rate limiting one request out of a burst was ending the
    whole turn. One 429 should cost a pause, not the answer."""
    monkeypatch.setattr(config, "LLM_RETRY_ATTEMPTS", 3)
    monkeypatch.setattr(llm.asyncio, "sleep", lambda _: asyncio_noop())
    sent = []

    def responder(body, url):
        if len(sent) == 1:
            return httpx.Response(429, request=httpx.Request("POST", url))
        return ok(url)

    fake_transport(monkeypatch, responder, sent)
    result = await llm.complete([{"role": "user", "content": "hi"}])

    assert result["message"]["content"] == "ok"
    assert len(sent) == 2, "one retry, not a storm of them"


@pytest.mark.asyncio
async def test_a_server_error_is_also_retried(monkeypatch, openai_provider):
    monkeypatch.setattr(config, "LLM_RETRY_ATTEMPTS", 3)
    monkeypatch.setattr(llm.asyncio, "sleep", lambda _: asyncio_noop())
    sent = []

    def responder(body, url):
        # sent is appended before the responder runs, so this is the 1st and 2nd call.
        if len(sent) <= 2:
            return httpx.Response(503, request=httpx.Request("POST", url))
        return ok(url)

    fake_transport(monkeypatch, responder, sent)
    assert (await llm.complete([{"role": "user", "content": "hi"}]))["message"]["content"] == "ok"
    assert len(sent) == 3, "two failures then success, inside the attempt budget"


@pytest.mark.asyncio
async def test_retries_are_bounded_and_then_reported(monkeypatch, openai_provider):
    monkeypatch.setattr(config, "LLM_RETRY_ATTEMPTS", 3)
    monkeypatch.setattr(llm.asyncio, "sleep", lambda _: asyncio_noop())
    sent = []
    fake_transport(
        monkeypatch,
        lambda body, url: httpx.Response(429, request=httpx.Request("POST", url)),
        sent,
    )

    with pytest.raises(llm.LLMError, match="rate limiting"):
        await llm.complete([{"role": "user", "content": "hi"}])
    assert len(sent) == 3, "exactly the configured attempts"


@pytest.mark.asyncio
async def test_a_bad_key_is_not_retried(monkeypatch, openai_provider):
    """Retrying a 401 wastes the user's time to reach the same answer."""
    monkeypatch.setattr(config, "LLM_RETRY_ATTEMPTS", 3)
    sent = []
    fake_transport(
        monkeypatch,
        lambda body, url: httpx.Response(401, request=httpx.Request("POST", url)),
        sent,
    )

    with pytest.raises(llm.LLMError, match="credentials"):
        await llm.complete([{"role": "user", "content": "hi"}])
    assert len(sent) == 1


def test_retry_after_is_respected_but_capped():
    """A host asking for four minutes should not hold a turn open that long."""
    response = httpx.Response(
        429, headers={"retry-after": "240"}, request=httpx.Request("POST", "http://x")
    )
    assert llm._retry_after(response, 0) == 10.0

    plain = httpx.Response(429, request=httpx.Request("POST", "http://x"))
    assert 0 < llm._retry_after(plain, 0) < 2


async def asyncio_noop():
    return None


@pytest.mark.asyncio
async def test_a_chat_model_rejecting_reasoning_effort_also_recovers(monkeypatch, openai_provider):
    """The fallback is not temperature-specific; any named optional parameter
    gets dropped and the call retried once."""
    monkeypatch.setattr(config, "REASONING_EFFORT", "low")
    sent = []

    def responder(body, url):
        if "reasoning_effort" in body:
            return httpx.Response(
                400,
                text=json.dumps({"error": {"message": "Unrecognized argument: reasoning_effort"}}),
                request=httpx.Request("POST", url),
            )
        return ok(url)

    fake_transport(monkeypatch, responder, sent)
    result = await llm.complete([{"role": "user", "content": "hi"}])

    assert result["message"]["content"] == "ok"
    assert "reasoning_effort" not in sent[1]["body"]
    assert "temperature" in sent[1]["body"]


@pytest.mark.asyncio
async def test_a_host_answering_with_something_other_than_json_is_a_readable_error(
    monkeypatch, openai_provider
):
    """The retired GitHub Models endpoint answers 200 with a plain "OK". Parsing
    that escaped every LLMError handler and surfaced as an unexplained failure."""
    fake_transport(
        monkeypatch,
        lambda body, url: httpx.Response(
            200,
            text="OK\r\n",
            headers={"content-type": "text/plain"},
            request=httpx.Request("POST", url),
        ),
        [],
    )
    with pytest.raises(llm.LLMError, match="non-JSON"):
        await llm.complete([{"role": "user", "content": "hi"}])


@pytest.mark.asyncio
async def test_a_json_body_that_is_not_an_object_is_a_readable_error(monkeypatch, openai_provider):
    """Gemini's error bodies are a JSON array, not an object."""
    fake_transport(
        monkeypatch,
        lambda body, url: httpx.Response(
            200, json=[{"error": {"code": 200}}], request=httpx.Request("POST", url)
        ),
        [],
    )
    with pytest.raises(llm.LLMError, match="unexpected body"):
        await llm.complete([{"role": "user", "content": "hi"}])


def test_the_retired_github_provider_explains_what_to_do(monkeypatch):
    monkeypatch.setattr(config, "PROVIDER", "github")
    monkeypatch.setattr(config, "GITHUB_TOKEN", "a-token-that-used-to-work")
    ready, hint = config.provider_ready()
    assert not ready
    assert "retired" in hint
