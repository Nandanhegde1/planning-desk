"""The loop itself: tool dispatch, budgets, and history shape.

A scripted stand-in replaces the model so the loop can be tested for free and
deterministically. What is under test is the orchestration, not the model.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import agent, config  # noqa: E402
from app.mcp_host import MCPHost  # noqa: E402


def scripted(*turns):
    """Return a fake complete() that plays the given turns in order."""
    queue = list(turns)

    async def fake(messages, tools=None, **kwargs):
        message = queue.pop(0) if queue else {"content": "done"}
        return {"message": message, "finish_reason": "stop", "usage": {"total_tokens": 500}}

    return fake


def tool_call(call_id, name, arguments="{}"):
    return {
        "content": "",
        "tool_calls": [
            {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}
        ],
    }


async def collect(host, history, message):
    return [event async for event in agent.run_turn(host, history, message)]


@pytest.mark.asyncio
async def test_a_plain_answer_needs_one_step(monkeypatch):
    monkeypatch.setattr(agent, "complete", scripted({"content": "Hello."}))
    async with MCPHost() as host:
        events = await collect(host, [], "hi")
    assert [e["type"] for e in events if e["type"] == "tool_start"] == []
    done = events[-1]
    assert done["reason"] == "answered" and done["steps"] == 1


@pytest.mark.asyncio
async def test_a_tool_call_is_dispatched_and_fed_back(monkeypatch):
    monkeypatch.setattr(
        agent,
        "complete",
        scripted(
            tool_call("c1", "feasibility__explain_thresholds", '{"activity": "outdoor"}'),
            {"content": "PM2.5 blocks at 91."},
        ),
    )
    history = []
    async with MCPHost() as host:
        events = await collect(host, history, "why was it rejected")

    start = next(e for e in events if e["type"] == "tool_start")
    end = next(e for e in events if e["type"] == "tool_end")
    assert start["name"] == "feasibility__explain_thresholds"
    assert end["ok"] and end["bytes"] > 100

    # The history must read: user, assistant-with-tool_calls, tool, assistant.
    assert [m["role"] for m in history] == ["user", "assistant", "tool", "assistant"]
    assert history[2]["tool_call_id"] == "c1"


@pytest.mark.asyncio
async def test_generated_files_are_announced(monkeypatch):
    monkeypatch.setattr(
        agent,
        "complete",
        scripted(
            tool_call("c1", "fx__list_series", "{}"),
            {"content": "Nothing fetched yet."},
        ),
    )
    async with MCPHost() as host:
        events = await collect(host, [], "what have we got")
    assert any(e["type"] == "tool_end" for e in events)


@pytest.mark.asyncio
async def test_the_step_ceiling_stops_a_runaway_loop(monkeypatch):
    """A model that only ever calls tools must still terminate."""
    monkeypatch.setattr(config, "MAX_STEPS", 3)
    monkeypatch.setattr(
        agent,
        "complete",
        scripted(*[tool_call(f"c{i}", "feasibility__explain_thresholds") for i in range(10)]),
    )
    async with MCPHost() as host:
        events = await collect(host, [], "loop forever")
    assert events[-1]["reason"] == "step_limit"
    assert events[-1]["steps"] == 3


@pytest.mark.asyncio
async def test_the_tool_ceiling_is_enforced(monkeypatch):
    monkeypatch.setattr(config, "MAX_STEPS", 6)
    monkeypatch.setattr(config, "MAX_TOOL_CALLS", 2)
    monkeypatch.setattr(
        agent,
        "complete",
        scripted(*[tool_call(f"c{i}", "feasibility__explain_thresholds") for i in range(10)]),
    )
    async with MCPHost() as host:
        events = await collect(host, [], "call tools forever")
    assert len([e for e in events if e["type"] == "tool_start"]) == 2


@pytest.mark.asyncio
async def test_a_wall_clock_stops_a_turn_that_will_not_finish(monkeypatch):
    """The step, tool and token ceilings each bound one dimension and multiply out
    to over an hour in the worst case. The turn needs a clock of its own."""
    monkeypatch.setattr(config, "MAX_TURN_SECONDS", 0)
    monkeypatch.setattr(
        agent,
        "complete",
        scripted(*[tool_call(f"c{i}", "feasibility__explain_thresholds") for i in range(10)]),
    )
    async with MCPHost() as host:
        events = await collect(host, [], "take forever")

    assert events[-1]["reason"] == "time_budget"
    assert any(e["type"] == "error" and "Stopped after" in e["text"] for e in events)


def two_calls(first: str, second: str) -> dict:
    return {
        "content": "",
        "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": first, "arguments": "{}"}},
            {"id": "c2", "type": "function", "function": {"name": second, "arguments": "{}"}},
        ],
    }


@pytest.mark.asyncio
async def test_calls_to_different_servers_overlap(monkeypatch):
    """Separate servers are separate processes, so their calls genuinely run at
    the same time."""
    import asyncio
    import time as clock

    monkeypatch.setattr(
        agent,
        "complete",
        scripted(
            two_calls("feasibility__suggest_better_windows", "fx__list_series"),
            {"content": "done"},
        ),
    )

    async def slow(name, arguments, timeout=None):
        await asyncio.sleep(0.4)
        return {"ok": name}

    async with MCPHost() as host:
        monkeypatch.setattr(host, "call", slow)
        started = clock.perf_counter()
        events = await collect(host, [], "one from each")
        elapsed = clock.perf_counter() - started

    assert elapsed < 0.75, f"different servers should overlap, took {elapsed:.2f}s"
    assert len([e for e in events if e["type"] == "tool_end"]) == 2


@pytest.mark.asyncio
async def test_calls_to_one_server_are_taken_in_turn(monkeypatch):
    """A server is a single request pipeline, so its calls serialise inside the
    host whatever the caller does. Dispatching them together only meant the one
    that waited spent its whole allowance queueing and timed out, which is how a
    fast tool kept failing next to a slow one on the deployed app.

    Note this is why the earlier version of this test was wrong: it patched
    host.call, which bypasses the per-server lock, so it appeared to prove an
    overlap that never happened in production.
    """
    import asyncio
    import time as clock

    monkeypatch.setattr(
        agent,
        "complete",
        scripted(
            two_calls(
                "feasibility__suggest_better_windows",
                "feasibility__suggest_alternative_destinations",
            ),
            {"content": "done"},
        ),
    )

    granted: list[float | None] = []

    async def slow(name, arguments, timeout=None):
        granted.append(timeout)
        await asyncio.sleep(0.4)
        return {"ok": name}

    async with MCPHost() as host:
        monkeypatch.setattr(host, "call", slow)
        started = clock.perf_counter()
        await collect(host, [], "both from one server")
        elapsed = clock.perf_counter() - started

    assert elapsed >= 0.75, "same-server calls are taken in turn, not overlapped"
    # The second call's allowance is computed when its turn comes, so it is not
    # charged for the time it spent waiting.
    assert len(granted) == 2
    assert granted[1] < granted[0], "the later call sees less time left, not zero"
    assert granted[1] > 0


@pytest.mark.asyncio
async def test_results_are_recorded_in_the_order_the_model_asked(monkeypatch):
    """Concurrency must not reorder the tool messages, or they stop lining up with
    the tool_calls that requested them."""
    import asyncio

    calls = [
        {
            "id": f"c{i}",
            "type": "function",
            "function": {"name": "feasibility__explain_thresholds", "arguments": "{}"},
        }
        for i in range(3)
    ]
    monkeypatch.setattr(
        agent, "complete", scripted({"content": "", "tool_calls": calls}, {"content": "done"})
    )

    async def varied(name, arguments, timeout=None):
        # Finish out of order on purpose.
        await asyncio.sleep(0.3 if not varied.first else 0.05)
        varied.first = True
        return {"ok": True}

    varied.first = False
    history = []
    async with MCPHost() as host:
        monkeypatch.setattr(host, "call", varied)
        await collect(host, history, "three please")

    ids = [m["tool_call_id"] for m in history if m["role"] == "tool"]
    assert ids == ["c0", "c1", "c2"]


@pytest.mark.asyncio
async def test_the_last_of_the_budget_is_spent_answering_not_calling_tools(monkeypatch):
    """Hitting the deadline used to return no answer at all, which is worse than a
    slow one. Observed on the deployed app, where the app region and the model
    region differ. Inside the reserve the model is offered no tools, so it has to
    reply from what it already gathered."""
    monkeypatch.setattr(config, "MAX_TURN_SECONDS", 30)
    monkeypatch.setattr(config, "FINAL_ANSWER_RESERVE_SECONDS", 45)  # reserve > budget
    offered = []

    async def record(messages, tools=None, **kwargs):
        offered.append(tools)
        return {
            "message": {"content": "Partial answer from what I have."},
            "finish_reason": "stop",
            "usage": {"total_tokens": 10},
        }

    monkeypatch.setattr(agent, "complete", record)
    async with MCPHost() as host:
        events = await collect(host, [], "wrap it up")

    assert offered[0] is None, "no tools offered once inside the reserve"
    assert events[-1]["reason"] == "answered"
    assert any(e["type"] == "answer" for e in events)
    assert any(e.get("text") == "wrapping up" for e in events if e["type"] == "status")


@pytest.mark.asyncio
async def test_a_tool_never_outlasts_the_turn_it_belongs_to(monkeypatch):
    """A 45 second tool timeout inside a turn with 5 seconds left would blow the
    wall clock, so the remaining time is handed down to the call."""
    monkeypatch.setattr(config, "MAX_TURN_SECONDS", 4)
    monkeypatch.setattr(config, "TOOL_TIMEOUT_SECONDS", 45)
    seen = {}

    async def record(name, arguments, timeout=None):
        seen["timeout"] = timeout
        return {"ok": True}

    monkeypatch.setattr(agent, "complete", scripted(tool_call("c1", "feasibility__find_places")))
    async with MCPHost() as host:
        monkeypatch.setattr(host, "call", record)
        await collect(host, [], "quick now")

    assert seen["timeout"] is not None
    assert seen["timeout"] <= 4, "the tool got the time left, not the configured 45s"


@pytest.mark.asyncio
async def test_a_single_slow_tool_cannot_eat_the_answer_reserve(monkeypatch):
    """The reserve was only held back between steps, so one slow tool consumed the
    whole budget and the turn ended with nothing. Seen live, where a travel lookup
    ran 164 seconds against a cold cache."""
    monkeypatch.setattr(config, "MAX_TURN_SECONDS", 100)
    monkeypatch.setattr(config, "FINAL_ANSWER_RESERVE_SECONDS", 40)
    seen = {}

    async def record(name, arguments, timeout=None):
        seen["timeout"] = timeout
        return {"ok": True}

    monkeypatch.setattr(agent, "complete", scripted(tool_call("c1", "feasibility__find_places")))
    async with MCPHost() as host:
        monkeypatch.setattr(host, "call", record)
        await collect(host, [], "slow tool")

    assert seen["timeout"] <= 61, "the tool must not be given the reserve"
    assert seen["timeout"] > 0


@pytest.mark.asyncio
async def test_a_model_outage_ends_the_turn_cleanly(monkeypatch):
    async def broken(messages, tools=None, **kwargs):
        raise agent.LLMError("model host down")

    monkeypatch.setattr(agent, "complete", broken)
    async with MCPHost() as host:
        events = await collect(host, [], "hello")
    assert events[-1]["reason"] == "model_error"
    assert any(e["type"] == "error" for e in events)


@pytest.mark.asyncio
async def test_a_model_outage_reaches_the_server_log(monkeypatch, caplog):
    """The browser was the only place a spent quota or a revoked key showed up."""
    async def broken(messages, tools=None, **kwargs):
        raise agent.LLMError("model host returned 403; the detail is in the log")

    monkeypatch.setattr(agent, "complete", broken)
    async with MCPHost() as host:
        with caplog.at_level("WARNING", logger="agent"):
            await collect(host, [], "hello")

    assert "model call failed" in caplog.text
    assert "403" in caplog.text


class RefusingHost:
    """Every tool call fails. Enough of a host for run_turn, without servers."""

    def tools_for(self, mode):
        return []

    async def call(self, name, arguments, timeout=None):
        return {"error": "upstream refused: " + "x" * 500}


@pytest.mark.asyncio
async def test_a_failed_tool_call_is_logged_with_its_arguments_cut_short(monkeypatch, caplog):
    """A failed call used to exist only in the browser's trace. The arguments are
    visitor text, so the log keeps only the start of them."""
    long_place = "Cubbon Park, Bengaluru " * 20
    monkeypatch.setattr(
        agent,
        "complete",
        scripted(
            tool_call(
                "c1",
                "feasibility__suggest_better_windows",
                f'{{"place": "{long_place}", "date_iso": "2026-08-22"}}',
            ),
            {"content": "It failed."},
        ),
    )
    with caplog.at_level("WARNING", logger="agent"):
        await collect(RefusingHost(), [], "a match")

    line = next(r.getMessage() for r in caplog.records if "failed after" in r.getMessage())
    assert "feasibility__suggest_better_windows" in line
    assert "upstream refused" in line
    assert long_place not in line, "arguments must be truncated"
    assert len(line) < 700


@pytest.mark.asyncio
async def test_history_trimming_never_orphans_a_tool_result():
    history = [{"role": "tool", "tool_call_id": "x", "content": "{}"}] + [
        {"role": "user", "content": "hi"}
    ]
    built = agent.build_messages(history)
    assert built[0]["role"] == "system"
    assert built[1]["role"] != "tool"


def test_expired_sessions_are_evicted():
    """The session store must not grow for the life of the process.

    Rewritten: _get_session used to create a session under any id it was handed,
    so this called it with a new name to trigger the sweep. It now only resumes an
    id the server minted, so the sweep is triggered by minting one.
    """
    import time

    from app import main

    main.sessions.clear()
    main.sessions["stale"] = main.Session(touched=time.monotonic() - 999999)
    fresh = main._new_session()
    main._get_session(fresh)

    assert "stale" not in main.sessions
    assert fresh in main.sessions


def test_the_session_cap_evicts_the_oldest(monkeypatch):
    """Rewritten for the same reason as the test above."""
    import time

    from app import config, main

    monkeypatch.setattr(config, "MAX_SESSIONS", 3)
    main.sessions.clear()
    for index in range(3):
        main.sessions[f"s{index}"] = main.Session(touched=time.monotonic() + index)

    newcomer = main._new_session()

    assert len(main.sessions) <= 3
    assert "s0" not in main.sessions
    assert newcomer in main.sessions


def test_an_unknown_session_id_is_not_adopted():
    """The id space was client-chosen. Handing the server any string created a
    session under it, so a caller could seed a short guessable id and have the
    server keep it alive."""
    import pytest as _pytest

    from app import main

    main.sessions.clear()
    with _pytest.raises(KeyError):
        main._get_session("guessed-id")
    assert "guessed-id" not in main.sessions


def test_a_minted_id_is_long_enough_to_be_the_only_guard():
    """It is the sole credential on a conversation. uuid4().hex[:12] was 48 bits."""
    from app import main

    main.sessions.clear()
    minted = main._new_session()

    assert len(minted) >= 40
    assert len({main._new_session() for _ in range(50)}) == 50


@pytest.mark.asyncio
async def test_each_mode_exposes_only_its_own_servers():
    """The two assignments are separable entry points over one host."""
    async with MCPHost() as host:
        planning = {t["function"]["name"].split("__")[0] for t in host.tools_for("planning")}
        currency = {t["function"]["name"].split("__")[0] for t in host.tools_for("currency")}
        everything = {t["function"]["name"].split("__")[0] for t in host.tools_for("all")}

    assert planning == {"feasibility", "discovery"}
    assert currency == {"fx", "discovery"}
    assert everything == {"feasibility", "fx", "discovery"}


@pytest.mark.asyncio
async def test_an_unknown_mode_falls_back_to_every_tool():
    async with MCPHost() as host:
        assert len(host.tools_for("nonsense")) == len(host.tools)


@pytest.mark.asyncio
async def test_the_loop_only_offers_the_tools_for_its_mode(monkeypatch):
    seen = {}

    async def fake(messages, tools=None, **kwargs):
        seen["tools"] = tools
        seen["system"] = messages[0]["content"]
        return {
            "message": {"content": "ok"},
            "finish_reason": "stop",
            "usage": {"total_tokens": 10},
        }

    monkeypatch.setattr(agent, "complete", fake)
    async with MCPHost() as host:
        await collect_mode(host, "currency")

    names = {t["function"]["name"].split("__")[0] for t in seen["tools"]}
    assert "feasibility" not in names
    assert "Exchange rate history" in seen["system"]


async def collect_mode(host, mode):
    return [event async for event in agent.run_turn(host, [], "hello", mode)]
