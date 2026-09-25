"""Multi-turn memory, end to end.

The loop's own history shape is covered in test_agent_loop.py. What is covered
here is the thing a user actually experiences: does turn two know what turn one
said, over HTTP, through the session store, after a reload, and after a turn that
spent its whole tool budget.

A scripted stand-in replaces the model, so these run for free and record exactly
what the model was shown on each call.
"""

import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import agent, config, main  # noqa: E402

# What the model was handed, one entry per call.
PROMPTS: list[list[dict]] = []


async def fake_complete(messages, tools=None, **kwargs):
    PROMPTS.append([dict(m) for m in messages])
    return {
        "message": {"content": "Noted."},
        "finish_reason": "stop",
        "usage": {"total_tokens": 10},
    }


@pytest.fixture(scope="module")
def client():
    """One app instance for the module: startup spawns the MCP servers."""
    original = agent.complete
    agent.complete = fake_complete
    with TestClient(main.app) as test_client:
        yield test_client
    agent.complete = original


@pytest.fixture(autouse=True)
def clean_state():
    main.sessions.clear()
    # Rate limit state too, or a module that drives many turns exhausts the
    # window and later tests receive a 429 instead of what they are testing.
    main._hits.clear()
    main._daily.clear()
    PROMPTS.clear()
    yield


def events(response):
    """The SSE frames of a /api/chat response."""
    frames = []
    for block in response.text.split("\n\n"):
        for line in block.split("\n"):
            if line.startswith("data: "):
                frames.append(json.loads(line[6:]))
    return frames


def ask(client, message, session_id=None, mode="planning"):
    body = {"message": message, "mode": mode}
    if session_id:
        body["session_id"] = session_id
    response = client.post("/api/chat", json=body)
    assert response.status_code == 200
    frames = events(response)
    return next(f["session_id"] for f in frames if f["type"] == "session"), frames


def spoken(prompt):
    """The user and assistant text in one recorded prompt."""
    return [m["content"] for m in prompt if m["role"] in ("user", "assistant") and m.get("content")]


# ---- the conversation carries -------------------------------------------


def test_a_second_message_on_the_same_session_sees_the_first(client):
    session_id, _ = ask(client, "Remember the number seven.")
    ask(client, "What number did I say?", session_id)

    assert "Remember the number seven." in spoken(PROMPTS[-1])
    assert len(main.sessions[session_id].history) == 4


def test_a_third_message_still_sees_the_first(client):
    """Two turns is not evidence of memory; drift shows up on the third."""
    session_id, _ = ask(client, "The city is Bengaluru.")
    ask(client, "The day is Saturday.", session_id)
    ask(client, "So where and when am I going?", session_id)

    said = spoken(PROMPTS[-1])
    assert "The city is Bengaluru." in said
    assert "The day is Saturday." in said


def test_omitting_the_session_id_starts_a_new_conversation(client):
    first, _ = ask(client, "Remember the number seven.")
    second, _ = ask(client, "What number did I say?")

    assert first != second
    assert "Remember the number seven." not in spoken(PROMPTS[-1])


def test_each_session_is_isolated_from_the_others(client):
    alice, _ = ask(client, "My city is Bengaluru.")
    bob, _ = ask(client, "My city is Munnar.")
    ask(client, "Which city?", alice)

    said = spoken(PROMPTS[-1])
    assert "My city is Bengaluru." in said
    assert "My city is Munnar." not in said
    assert alice != bob


# ---- reload and reset ---------------------------------------------------


def test_the_transcript_endpoint_replays_what_was_said(client):
    """What the page reloads from. A reload used to come back blank while the
    server still held the conversation, which reads as memory loss."""
    session_id, _ = ask(client, "Is badminton indoors feasible on Sunday?")
    ask(client, "And on Monday?", session_id)

    payload = client.get(f"/api/session/{session_id}").json()
    assert [t["role"] for t in payload["turns"]] == ["user", "assistant", "user", "assistant"]
    assert payload["turns"][0]["text"] == "Is badminton indoors feasible on Sunday?"


def test_the_transcript_leaves_out_tool_traffic(client):
    session_id, _ = ask(client, "hello")
    main.sessions[session_id].history.extend(
        [
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c1"}]},
            {"role": "tool", "tool_call_id": "c1", "content": '{"pm2_5": 40}'},
        ]
    )

    turns = client.get(f"/api/session/{session_id}").json()["turns"]
    assert all(turn["role"] in ("user", "assistant") for turn in turns)
    assert not any("pm2_5" in turn["text"] for turn in turns)


def test_an_unknown_session_is_a_404_not_a_crash(client):
    assert client.get("/api/session/nope").status_code == 404


def test_reset_accepts_a_body_with_only_a_session_id(client):
    """Regression: reset shared the chat request model, so every call was
    rejected with a 422 for a missing message field."""
    session_id, _ = ask(client, "hello")
    response = client.post("/api/reset", json={"session_id": session_id})

    assert response.status_code == 200
    assert session_id not in main.sessions


def test_after_a_reset_the_next_message_starts_clean(client):
    session_id, _ = ask(client, "Remember the number seven.")
    client.post("/api/reset", json={"session_id": session_id})
    ask(client, "What number did I say?")

    assert "Remember the number seven." not in spoken(PROMPTS[-1])


# ---- trimming cannot cost the user their words --------------------------


def fx_summary(series_id="usd-inr-1", rows=24):
    """The shape servers/fx_server.py::_summary actually returns: a series_id,
    metadata, statistics and a downsampled 24-row sample. That comes to about
    1,270 characters, and the series_id is the only handle the export and chart
    tools accept. Pass a larger `rows` for a payload big enough to be shrunk."""
    return {
        "series_id": series_id,
        "columns": ["USD/INR"],
        "meta": {"first_published": "2024-08-17", "last_published": "2026-08-17"},
        "statistics": {"USD/INR": {"change_pct": 4.2, "r_squared": 0.81}},
        "sample": [{"date": f"2025-01-{d % 28 + 1:02d}", "USD/INR": 83.0 + d} for d in range(rows)],
        "sample_note": "Every 21st row of 512. The full series is held server-side.",
    }


def one_turn(user_text, calls_per_step=1, steps=1, payload=None):
    """The messages one real turn appends, matching run_turn's shape."""
    body = json.dumps(payload if payload is not None else {"rate": 1})
    messages = [{"role": "user", "content": user_text}]
    made = 0
    for _ in range(steps):
        batch = []
        for _ in range(calls_per_step):
            made += 1
            batch.append(
                {
                    "id": f"{user_text[:4]}-{made}",
                    "type": "function",
                    "function": {"name": "fx__get_rate_series", "arguments": "{}"},
                }
            )
        messages.append({"role": "assistant", "content": "", "tool_calls": batch})
        messages.extend(
            {"role": "tool", "tool_call_id": call["id"], "content": body} for call in batch
        )
    messages.append({"role": "assistant", "content": "Done."})
    return messages


def test_tool_heavy_turns_cannot_evict_what_the_user_said():
    """Three turns that each spend the full tool budget come to more messages
    than the old flat window held, so the first request was dropped while its
    tool output was kept."""
    history = []
    for text in ["first request", "second request", "third request"]:
        history += one_turn(text, calls_per_step=config.MAX_TOOL_CALLS, steps=1)

    assert len(history) > config.MAX_HISTORY_TURNS  # the flat window would have bitten
    said = spoken(agent.build_messages(history, "currency"))
    assert "first request" in said
    assert "second request" in said
    assert "third request" in said


def test_the_four_step_currency_conversation_keeps_its_subject():
    """Assignment 2 is a four-step conversation, and step three is "show that as
    a different graph". If step one is gone, "that" has no referent."""
    history = []
    history += one_turn("Build a two-year USD to INR spreadsheet.", 2, 2, fx_summary())
    history += one_turn("Add INR/GBP and INR/EUR to the comparison.", 3, 2, fx_summary("three-2"))
    history += one_turn("Show that as a monthly bar chart instead.", 2, 1, fx_summary("three-2"))
    history += one_turn("Put the whole comparison into a Word document.", 2, 1, fx_summary())

    said = spoken(agent.build_messages(history, "currency"))
    assert "Build a two-year USD to INR spreadsheet." in said
    assert "Add INR/GBP and INR/EUR to the comparison." in said


def test_a_real_fx_summary_is_never_shrunk_at_all():
    """The export, chart and document tools accept only a series_id
    (servers/fx_server.py:171,197,219). An earlier version of the trimming
    replaced every tool result in an older turn, which dropped the series_id
    along with the numbers, so "put the whole comparison into a Word document"
    had to refetch and mint a new id and the artefacts stopped describing the
    same data.

    A real summary is about 1,270 characters, under the shrink threshold, so it
    now passes through whole no matter how old the turn is."""
    history = []
    for text in [
        "Build a two-year USD to INR spreadsheet.",
        "Add a trendline.",
        "Show it as a bar chart.",
        "Put the comparison into a Word document.",
    ]:
        history += one_turn(text, 1, 1, fx_summary("usd-inr-1"))

    built = agent.build_messages(history, "currency")
    tools = [m for m in built if m["role"] == "tool"]

    assert all("usd-inr-1" in m["content"] for m in tools)
    assert not any(agent.STALE_NOTE in m["content"] for m in tools)


def test_a_bulky_old_result_is_shrunk_but_keeps_its_handles():
    """What shrinking is actually for: a result far larger than a summary, in a
    turn old enough that the figures will not be read again. The series_id and
    the column list come across so a follow-up can still name the series."""
    history = (
        one_turn("old request", 1, 1, fx_summary("usd-inr-1", rows=280))
        + one_turn("newer request", 1, 1, fx_summary("usd-inr-1", rows=280))
        + one_turn("recent request", 1, 1, fx_summary("usd-inr-1", rows=280))
    )
    built = agent.build_messages(history, "currency")
    tools = [m for m in built if m["role"] == "tool"]

    shrunk = [m for m in tools if agent.STALE_NOTE in m["content"]]
    assert shrunk, "a bulky result in an older turn should be shrunk"
    for message in shrunk:
        carried = json.loads(message["content"])
        assert carried["series_id"] == "usd-inr-1"
        assert carried["columns"] == ["USD/INR"]
        assert "sample" not in carried, "the bulk is what goes"
    assert any("sample_note" in m["content"] for m in tools), "the recent turn keeps its detail"
    assert "old request" in spoken(built)


def test_a_small_old_result_is_left_completely_alone():
    """Age is not the test. A short result is almost certainly an identifier or a
    verdict, and it costs nothing to keep whole."""
    history = (
        one_turn("old request", 1, 1, {"series_id": "usd-inr-1"})
        + one_turn("newer request", 1, 1)
        + one_turn("recent request", 1, 1)
    )
    built = agent.build_messages(history, "currency")

    tools = [m for m in built if m["role"] == "tool"]
    assert any(m["content"] == '{"series_id": "usd-inr-1"}' for m in tools)
    assert not any(agent.STALE_NOTE in m["content"] for m in tools)


def test_the_shrink_note_points_at_a_tool_that_exists():
    """It used to say "call the tool again", which mints a new series_id. The fx
    server exposes list_series precisely so a stored one can be recovered."""
    shrunk = agent._shrink_tool_result(json.dumps(fx_summary()))
    assert "fx__list_series" in json.loads(shrunk)["note"]


def test_shrinking_survives_a_result_that_is_not_json():
    assert json.loads(agent._shrink_tool_result("not json at all"))["note"] == agent.STALE_NOTE


def test_trimming_still_never_opens_on_a_tool_result():
    history = [{"role": "tool", "tool_call_id": "x", "content": "{}"}, *one_turn("hi")]
    built = agent.build_messages(history)

    assert built[0]["role"] == "system"
    assert built[1]["role"] != "tool"


def test_turns_beyond_the_ceiling_are_dropped_whole(monkeypatch):
    """The window has to end somewhere; it must end on a turn boundary rather
    than mid-turn, or a tool result outlives the call that asked for it."""
    monkeypatch.setattr(config, "MAX_HISTORY_TURNS", 2)
    history = one_turn("ancient", 1, 1) + one_turn("recent", 1, 1) + one_turn("latest", 1, 1)
    built = agent.build_messages(history)

    said = spoken(built)
    assert "ancient" not in said
    assert "recent" in said and "latest" in said
    assert built[1]["role"] == "user"


# ---- oversized results stay readable ------------------------------------


def test_an_oversized_tool_result_is_still_valid_json(monkeypatch):
    """Slicing the serialised payload cut it mid-document, so a large series
    reached the model as broken JSON."""
    monkeypatch.setattr(config, "MAX_TOOL_RESULT_CHARS", 200)
    content = agent._tool_content({"series": [{"date": "2024-01-01", "rate": 83.1}] * 200})

    parsed = json.loads(content)  # would raise on a mid-document cut
    assert parsed["truncated"] is True
    assert parsed["original_bytes"] > 200


def test_a_small_tool_result_is_passed_through_unchanged():
    payload = {"rate": 83.12, "pair": "USD/INR"}
    assert json.loads(agent._tool_content(payload)) == payload


# ---- deployment surface -------------------------------------------------


def test_liveness_is_200_even_when_the_model_is_unconfigured(client, monkeypatch):
    """A probe must not read a missing key as a dead container, or the revision
    never activates."""
    monkeypatch.setattr(config, "GITHUB_TOKEN", "")
    monkeypatch.setattr(config, "AZURE_API_KEY", "")

    assert client.get("/api/health/live").status_code == 200
    assert client.get("/api/health/live").json()["status"] == "alive"


def test_every_response_carries_the_security_headers(client):
    headers = client.get("/api/health/live").headers
    assert "default-src 'self'" in headers["content-security-policy"]
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["referrer-policy"] == "no-referrer"


def test_the_page_declares_no_third_party_origins(client):
    """A restricted-egress container must not depend on a font CDN to render."""
    body = client.get("/").text
    for origin in ["fonts.googleapis.com", "fonts.gstatic.com", "cdn.", "unpkg.com"]:
        assert origin not in body


def test_the_page_persists_both_the_session_and_the_mode(client):
    """Regression: the session id was persisted but the mode was not, so a
    reload of a bare url resumed a currency conversation with the planning tool
    set selected, and the next message could not reach the fx tools at all.
    Both keys have to survive a reload, and the mode has to be read back."""
    body = client.get("/").text

    assert "planning-desk-session" in body
    assert "planning-desk-mode" in body
    assert "sessionStorage.getItem(MODE_KEY)" in body
    assert "/api/session/" in body, "the page must be able to repaint a transcript"


def test_an_interrupted_turn_does_not_poison_the_session():
    """Pressing Stop left an assistant message whose tool_calls had no matching
    tool replies. The model API rejects that shape, so every later message in the
    session failed with a 400 and the error appeared to follow the user around
    until they started a new conversation."""
    history = [
        {"role": "user", "content": "is saturday workable"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{}"}},
                {"id": "c2", "type": "function", "function": {"name": "g", "arguments": "{}"}},
            ],
        },
        # c1 came back before the abort; c2 never did.
        {"role": "tool", "tool_call_id": "c1", "content": "{}"},
    ]

    assert agent.repair_history(history) == 1
    replies = {m["tool_call_id"] for m in history if m["role"] == "tool"}
    assert replies == {"c1", "c2"}
    assert json.loads(history[-1]["content"])["error"]

    # And it is idempotent, so a later clean turn adds nothing.
    assert agent.repair_history(history) == 0


def test_repairing_a_finished_turn_changes_nothing():
    history = [
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{}"}}
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "{}"},
        {"role": "assistant", "content": "Done."},
    ]
    before = len(history)

    assert agent.repair_history(history) == 0
    assert len(history) == before


def test_the_next_message_works_after_an_interrupted_turn(client):
    """End to end: a session left in the broken shape must serve the next turn."""
    session_id, _ = ask(client, "first question")
    main.sessions[session_id].history.append(
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "orphan", "type": "function", "function": {"name": "f", "arguments": "{}"}}
            ],
        }
    )

    # The chat endpoint repairs on teardown, so this second turn must succeed.
    ask(client, "second question", session_id)
    ids = {m.get("tool_call_id") for m in main.sessions[session_id].history if m["role"] == "tool"}
    assert "orphan" in ids


def test_the_turn_budget_stays_under_the_host_request_ceiling():
    """Azure App Service cuts a response at about 230s with no error and no final
    event. A budget above that had the platform kill the turn before the loop
    could wrap up, so the browser received a truncated stream and no answer."""
    assert config.MAX_TURN_SECONDS <= 200, "must leave headroom under the 230s platform ceiling"
    assert config.FINAL_ANSWER_RESERVE_SECONDS < config.MAX_TURN_SECONDS


def test_the_page_can_stop_and_retry_a_turn(client):
    """A turn that hangs needs an exit. Ask becomes Stop while a turn is in
    flight, aborting the fetch ends the SSE stream and cancels the server
    generator with it, and a failed or stopped turn offers to run again."""
    body = client.get("/").text

    assert "AbortController" in body
    assert "inFlight?.abort()" in body
    assert "offerRetry" in body
    assert "AbortError" in body, "a deliberate stop must not be reported as a fault"


def test_the_scope_fence_is_in_the_system_prompt():
    """The brief is two jobs. Asked for a Python program it used to write one."""
    prompt = agent.SYSTEM_PROMPT

    # Matched on single words, because the prompt is hard-wrapped.
    assert "out of scope" in prompt
    assert "writing code" in prompt
    assert "general assistant" in prompt
    assert "Python" in prompt, "the code case is named explicitly, not left to inference"


def test_a_verdict_may_not_be_invented_when_the_tools_are_absent():
    """The worst failure found in live testing. Asked a feasibility question in
    Currencies mode, where the feasibility tools are not exposed, the model ran
    web_search three times and issued its own "Caution" from scraped weather
    pages. That breaks the one guarantee the design rests on: verdicts come from
    the rules engine, not the model."""
    prompt = agent.SYSTEM_PROMPT

    lowered = prompt.lower()
    assert "never state an exchange rate" in lowered
    assert "never state a go, caution or no_go" in lowered
    assert "not in your tool list" in lowered
    assert "never something to work around" in lowered
    assert "venue facts only" in lowered, "web_search must not stand in for a reading"
    # Both directions named, because naming only one had it send fx users to Plans.
    assert "rates are in" in lowered
    assert "feasibility is in plans" in lowered


def test_a_local_match_is_not_offered_another_city():
    """A football match at Cubbon Park came back no_go and the app suggested
    Hampi, Manali, Varanasi and Ooty. Manali is about 2000km from Bengaluru.
    Alternative destinations belong to a trip, where the destination is what the
    user is choosing. For an activity at a named venue the alternative is a
    different time or a covered venue."""
    prompt = agent.SYSTEM_PROMPT.lower()

    assert "changes when, not where" in prompt
    assert "never offer another city" in prompt
    assert "suggest_better_windows only" in prompt
    # The trip case must still get both, so the fence cannot be a blanket ban.
    assert "the destination is the thing being chosen" in prompt


def test_narration_may_not_invent_significance_or_band_labels():
    """Two live findings. For an indoor activity it named a 67% rain chance as a
    deciding reading, when rain decides nothing indoors. And it called PM2.5 of
    51 "moderate" when the moderate line in core/rules.py is 61."""
    prompt = agent.SYSTEM_PROMPT

    assert "crossed nothing" in prompt
    assert "band word" in prompt


def test_the_pm25_bands_the_prose_must_not_invent_are_the_documented_ones():
    """Pins the numbers the narration fence exists to protect, so a threshold
    change cannot silently make the live findings stale."""
    from core.rules import THRESHOLDS

    assert THRESHOLDS["outdoor"]["pm2_5_ug_m3"] == {"caution": 61, "blocker": 91}


def test_the_status_strip_is_repainted_per_mode(client):
    """The tool count is per mode. It used to be written once from the health
    response, so once a conversation had started every mode showed the same
    figure and the same placeholder, which read as the modes doing nothing."""
    body = client.get("/").text

    # Defined once, and called again on a mode change rather than only on load.
    assert body.count("paintVitals") >= 3
    assert "if (!opening.isConnected) return;" in body, (
        "paintOpening must still update the composer placeholder once the "
        "opening has been replaced by a conversation"
    )


def test_the_server_honours_the_mode_sent_with_each_message(client):
    """The mode travels per request, which is why losing it client-side matters:
    the tools offered to the model change with it."""
    tools_by_mode = {}

    async def record_tools(messages, tools=None, **kwargs):
        tools_by_mode[record_tools.mode] = {t["function"]["name"].split("__")[0] for t in tools}
        return {"message": {"content": "ok"}, "finish_reason": "stop", "usage": {}}

    original = agent.complete
    agent.complete = record_tools
    try:
        record_tools.mode = "currency"
        ask(client, "a rate please", mode="currency")
        record_tools.mode = "planning"
        ask(client, "a match please", mode="planning")
    finally:
        agent.complete = original

    assert "fx" in tools_by_mode["currency"]
    assert "fx" not in tools_by_mode["planning"]
    assert "feasibility" in tools_by_mode["planning"]


def test_the_streaming_endpoint_is_not_buffered_by_middleware(client):
    """The security headers are added by pure ASGI for this reason: a wrapper
    that buffers the body would hold the whole turn and defeat streaming."""
    response = client.post("/api/chat", json={"message": "hello", "mode": "planning"})
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["x-accel-buffering"] == "no"
    assert "content-security-policy" in response.headers


def test_the_status_strip_is_built_from_nodes_not_markup(client):
    """Configuration values were interpolated into innerHTML. Operator-controlled,
    but the page's CSP permits inline script, so an unescaped deployment name is a
    needless edge."""
    body = client.get("/").text

    assert "vitals.innerHTML" not in body
    assert "vitals.appendChild" in body


def test_the_browser_never_receives_raw_exception_text(client):
    """Upstream errors carry request URLs with keys in them, filesystem paths, and
    up to 200 characters of third-party response body."""
    body = client.get("/").text  # noqa: F841
    import inspect

    source = inspect.getsource(main.chat)
    assert "str(exc)" not in source
    assert "secrets.token_hex" in source
    assert "Quote reference" in source


def test_a_burst_from_one_client_is_capped(client, monkeypatch):
    """/api/chat is the one endpoint that spends model tokens and it carries no
    credential, so without a ceiling anyone holding the url can empty the quota."""
    monkeypatch.setattr(config, "RATE_LIMIT_TURNS", 3)
    monkeypatch.setattr(config, "RATE_LIMIT_WINDOW_SECONDS", 300)
    main._hits.clear()

    codes = [
        client.post("/api/chat", json={"message": "hello", "mode": "planning"}).status_code
        for _ in range(5)
    ]

    assert codes[:3] == [200, 200, 200]
    assert codes[-1] == 429, f"the burst should be capped, got {codes}"


def test_the_limit_is_per_client_not_global(client, monkeypatch):
    """One noisy caller must not lock everyone else out."""
    monkeypatch.setattr(config, "RATE_LIMIT_TURNS", 2)
    main._hits.clear()

    for _ in range(3):
        client.post("/api/chat", json={"message": "hi", "mode": "planning"})
    blocked = client.post("/api/chat", json={"message": "hi", "mode": "planning"})
    other = client.post(
        "/api/chat",
        json={"message": "hi", "mode": "planning"},
        headers={"x-forwarded-for": "203.0.113.9"},
    )

    assert blocked.status_code == 429
    assert other.status_code == 200


def test_the_daily_cap_holds_across_every_caller(client, monkeypatch):
    """A free tier's quota belongs to the whole deployment. Callers on different
    addresses each stay under the per-caller window and still spend it together."""
    monkeypatch.setattr(config, "DAILY_TURN_CAP", 3)
    main._daily.clear()

    codes = [
        client.post(
            "/api/chat",
            json={"message": "hi", "mode": "planning"},
            headers={"x-forwarded-for": f"203.0.113.{n}"},
        ).status_code
        for n in range(5)
    ]

    assert codes == [200, 200, 200, 429, 429]


def test_the_daily_cap_says_why_it_refused(client, monkeypatch):
    monkeypatch.setattr(config, "DAILY_TURN_CAP", 1)
    main._daily.clear()

    client.post("/api/chat", json={"message": "hi", "mode": "planning"})
    refused = client.post("/api/chat", json={"message": "hi", "mode": "planning"})

    assert refused.status_code == 429
    assert "24 hours" in refused.json()["detail"]


def test_a_daily_cap_of_zero_turns_it_off(client, monkeypatch):
    monkeypatch.setattr(config, "DAILY_TURN_CAP", 0)
    main._daily.clear()

    codes = {
        client.post(
            "/api/chat",
            json={"message": "hi", "mode": "planning"},
            headers={"x-forwarded-for": f"198.51.100.{n}"},
        ).status_code
        for n in range(4)
    }

    assert codes == {200}
    assert not main._daily


def test_health_carries_the_public_notice(client, monkeypatch):
    monkeypatch.setattr(config, "PUBLIC_NOTICE", "Runs on a free model tier.")
    assert client.get("/api/health").json()["notice"] == "Runs on a free model tier."


def test_the_page_shows_a_refusal_instead_of_painting_nothing(client):
    """A 429 arrives as JSON, not as a stream. Read as a stream it produced no
    frames, so the turn silently showed nothing."""
    body = client.get("/").text
    assert "response.ok" in body
    assert "health.notice" in body
