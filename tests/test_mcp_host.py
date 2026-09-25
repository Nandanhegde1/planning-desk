"""Proof the MCP layer is real: spawn the servers and talk to them over stdio.

These tests only exercise tools that need no network, so they pass on a laptop
with the wifi off and in CI.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.mcp_host import MCPHost  # noqa: E402


@pytest.mark.asyncio
async def test_host_discovers_tools_from_every_server():
    host = MCPHost()
    await host.start()
    try:
        assert not host.failed, f"servers failed to start: {host.failed}"
        names = host.tool_names
        assert "feasibility__check_activity_window" in names
        assert "fx__get_rate_series" in names
        assert "discovery__shortlist_destinations" in names
        # Schemas must survive the trip, or the model cannot call anything.
        tool = next(t for t in host.tools if t["function"]["name"] == "fx__render_chart")
        assert "series_id" in tool["function"]["parameters"]["properties"]
        assert tool["function"]["description"]
    finally:
        await host.stop()


@pytest.mark.asyncio
async def test_calling_a_tool_returns_parsed_json():
    host = MCPHost()
    await host.start()
    try:
        result = await host.call("feasibility__explain_thresholds", {"activity": "outdoor"})
        assert "thresholds" in result
        assert result["thresholds"]["pm2_5_ug_m3"]["blocker"] == 91
    finally:
        await host.stop()


@pytest.mark.asyncio
async def test_pure_tools_work_without_a_network():
    host = MCPHost()
    await host.start()
    try:
        result = await host.call(
            "discovery__shortlist_destinations", {"max_daily_inr": 4000, "tags": ["hills"]}
        )
        assert result["count"] > 0
        assert all(d["daily_budget_inr"][0] <= 4000 for d in result["destinations"])
        assert "estimate" in result["disclaimer"].lower()
    finally:
        await host.stop()


@pytest.mark.asyncio
async def test_an_unknown_tool_returns_an_error_instead_of_raising():
    host = MCPHost()
    await host.start()
    try:
        assert "error" in await host.call("feasibility__no_such_tool", {})
        assert "error" in await host.call("not_a_server__whatever", {})
    finally:
        await host.stop()


@pytest.mark.asyncio
async def test_bad_arguments_come_back_as_data():
    host = MCPHost()
    await host.start()
    try:
        result = await host.call(
            "feasibility__check_activity_window", {"place": "Bengaluru", "date_iso": "not-a-date"}
        )
        assert "error" in result
    finally:
        await host.stop()


@pytest.mark.asyncio
async def test_configuration_reaches_the_child_servers(monkeypatch):
    """Regression: a stdio server inherits almost no environment.

    MCP starts a child with a minimal set of variables, so a key set in .env
    reaches the host but not the servers unless it is forwarded deliberately.
    When that forwarding broke, search reported itself unconfigured while the
    key sat correctly in .env, which is a silent failure and the worst kind.
    """
    monkeypatch.setenv("SEARCH_PROVIDER", "tavily")
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-not-a-real-key")

    async with MCPHost() as host:
        result = await host.call("discovery__check_configuration", {})

    assert result["search_provider"] == "tavily"
    assert result["key_present_for_selected_provider"] is True
    assert result["status"] == "ready"
    # The diagnostic must never echo the value back.
    assert "tvly-not-a-real-key" not in str(result)


@pytest.mark.asyncio
async def test_unset_search_is_reported_as_disabled_not_broken(monkeypatch):
    monkeypatch.setenv("SEARCH_PROVIDER", "none")
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)

    async with MCPHost() as host:
        result = await host.call("discovery__check_configuration", {})

    assert result["status"] == "search disabled"


@pytest.mark.asyncio
async def test_the_cross_base_comparison_tool_is_exposed():
    """Assignment 2 step 3 compares USD/INR with INR/GBP and INR/EUR, which do
    not share a base, so get_rate_series alone cannot serve it."""
    async with MCPHost() as host:
        names = host.tool_names
        assert "fx__compare_pairs" in names
        assert "feasibility__suggest_alternative_destinations" in names

        tool = next(t for t in host.tools if t["function"]["name"] == "fx__compare_pairs")
        assert "pairs" in tool["function"]["parameters"]["properties"]


@pytest.mark.asyncio
async def test_chart_and_export_tools_reject_an_unknown_series():
    async with MCPHost() as host:
        for tool in ["fx__render_chart", "fx__export_spreadsheet", "fx__export_document"]:
            result = await host.call(tool, {"series_id": "does-not-exist"})
            assert "error" in result, tool


@pytest.mark.asyncio
async def test_compare_pairs_validates_before_reaching_the_network():
    async with MCPHost() as host:
        assert "error" in await host.call("fx__compare_pairs", {"pairs": []})
        assert "error" in await host.call(
            "fx__compare_pairs", {"pairs": ["USD/INR"], "start_date_iso": "not-a-date"}
        )
        assert "error" in await host.call("fx__compare_pairs", {"pairs": ["rupees"]})


def test_a_non_dict_tool_result_is_wrapped():
    """Callers treat a tool result as a mapping and call .get() on it, so a server
    returning a list or a bare string used to reach them unwrapped. The branch
    that was supposed to handle this had the same expression on both sides of its
    ternary, so it did nothing."""
    import inspect

    from app import mcp_host

    source = inspect.getsource(mcp_host.MCPHost.call)
    assert 'payload if isinstance(payload, str) else payload' not in source
    assert '{"result": payload}' in source
