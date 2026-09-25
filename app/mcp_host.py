"""MCP client.

Each configured server runs as a child process. The host initializes, calls
tools/list, and converts the returned JSON Schema into the model's tool format,
so adding a server is one line in config.MCP_SERVERS. Discovery runs once at
startup.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import AsyncExitStack
from typing import Any

from mcp import ClientSession, StdioServerParameters, stdio_client
from mcp.client.stdio import get_default_environment

from app import config

log = logging.getLogger("mcp_host")

SEPARATOR = "__"


def _server_env() -> dict[str, str]:
    """Environment for a launched server.

    MCP starts children with PATH, HOME, TERM and little else, so anything from
    .env must be forwarded explicitly via config.SERVER_ENV_KEYS.
    """
    env = get_default_environment()
    for key in config.SERVER_ENV_KEYS:
        value = os.environ.get(key)
        if value:
            env[key] = value
    return env


class MCPHost:
    def __init__(self, servers: dict[str, list[str]] | None = None) -> None:
        self.servers = servers or config.MCP_SERVERS
        self._stack: AsyncExitStack | None = None
        self._sessions: dict[str, ClientSession] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self.tools: list[dict[str, Any]] = []
        self.failed: dict[str, str] = {}

    async def start(self) -> None:
        self._stack = AsyncExitStack()
        for name, command in self.servers.items():
            try:
                params = StdioServerParameters(
                    command=command[0], args=command[1:], env=_server_env()
                )
                read, write = await self._stack.enter_async_context(stdio_client(params))
                session = await self._stack.enter_async_context(ClientSession(read, write))
                await asyncio.wait_for(session.initialize(), timeout=30)
                listing = await session.list_tools()
            except Exception as exc:
                log.warning("mcp server %s failed to start: %s", name, exc)
                self.failed[name] = str(exc)
                continue

            self._sessions[name] = session
            self._locks[name] = asyncio.Lock()
            for tool in listing.tools:
                schema = getattr(tool, "input_schema", None) or getattr(tool, "inputSchema", None)
                self.tools.append(
                    {
                        "type": "function",
                        "function": {
                            "name": f"{name}{SEPARATOR}{tool.name}",
                            "description": (tool.description or "").strip(),
                            "parameters": schema or {"type": "object", "properties": {}},
                        },
                    }
                )
            log.info("mcp server %s ready with %d tools", name, len(listing.tools))

    async def stop(self) -> None:
        if self._stack:
            await self._stack.aclose()
            self._stack = None
        self._sessions.clear()
        self._locks.clear()

    # start() and stop() must run in the same task: the transport opens an anyio
    # cancel scope and anyio will not let another task close it. FastAPI's
    # lifespan satisfies this; a pytest fixture does not.
    async def __aenter__(self) -> MCPHost:
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.stop()

    @property
    def tool_names(self) -> list[str]:
        return [t["function"]["name"] for t in self.tools]

    def tools_for(self, mode: str) -> list[dict[str, Any]]:
        """The tools a mode exposes. An unknown mode gets everything."""
        wanted = config.MODES.get(mode, {}).get("servers")
        if not wanted:
            return self.tools
        prefixes = tuple(f"{name}{SEPARATOR}" for name in wanted)
        return [t for t in self.tools if t["function"]["name"].startswith(prefixes)]

    def describe(self) -> dict[str, Any]:
        by_server: dict[str, list[str]] = {}
        for name in self.tool_names:
            server, tool = name.split(SEPARATOR, 1)
            by_server.setdefault(server, []).append(tool)
        return {"servers": by_server, "failed": self.failed}

    async def call(
        self,
        qualified_name: str,
        arguments: dict[str, Any],
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Invoke one tool. Never raises; failures return as data.

        timeout overrides the configured tool timeout, so a caller with a whole
        turn to finish can hand over only the time it has left.
        """
        limit = config.TOOL_TIMEOUT_SECONDS if timeout is None else max(1.0, timeout)
        if SEPARATOR not in qualified_name:
            return {"error": f"unknown tool {qualified_name!r}"}
        server_name, tool_name = qualified_name.split(SEPARATOR, 1)
        session = self._sessions.get(server_name)
        if session is None:
            return {"error": f"mcp server {server_name!r} is not running"}

        async def queue_then_call() -> Any:
            # One lock per server: a ClientSession is a single request pipeline,
            # so two calls to the same server serialise here even when the caller
            # dispatched them together. Only calls to different servers overlap.
            async with self._locks[server_name]:
                return await session.call_tool(tool_name, arguments)

        try:
            # The timeout covers the wait for the lock as well as the call. With it
            # inside the lock, a second caller waited an unbounded time and then
            # started its own full timeout, so two queued calls could take twice
            # the limit between them and overrun the turn that granted it.
            result = await asyncio.wait_for(queue_then_call(), timeout=limit)
        except TimeoutError:
            return {"error": f"{qualified_name} timed out after {limit:.0f}s, queueing included"}
        except Exception as exc:
            return {"error": f"{qualified_name} failed: {exc}"}

        is_error = getattr(result, "is_error", None)
        if is_error is None:
            is_error = getattr(result, "isError", False)

        structured = getattr(result, "structured_content", None) or getattr(
            result, "structuredContent", None
        )
        if structured:
            payload = structured
        else:
            chunks = []
            for block in result.content or []:
                text = getattr(block, "text", None)
                if text:
                    chunks.append(text)
            joined = "\n".join(chunks)
            try:
                payload = json.loads(joined) if joined else {}
            except json.JSONDecodeError:
                payload = {"text": joined}

        if is_error:
            return {"error": payload}
        if not isinstance(payload, dict):
            # Callers treat a tool result as a mapping and call .get() on it. A
            # server returning a list or a bare string used to reach them as-is.
            return {"result": payload}
        return payload
