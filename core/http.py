"""Shared HTTP: timeouts, retries, disk cache and user agent."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import httpx

CACHE_DIR = Path(os.environ.get("CACHE_DIR", Path(__file__).resolve().parent.parent / ".cache"))
CACHE_DIR.mkdir(parents=True, exist_ok=True)

USER_AGENT = os.environ.get(
    "HTTP_USER_AGENT",
    "planning-desk/1.0 (set HTTP_USER_AGENT to your contact address)",
)
TIMEOUT = float(os.environ.get("HTTP_TIMEOUT_SECONDS", "20"))

# Nominatim's usage policy caps anonymous use at one request a second.
_HOST_MIN_INTERVAL = {"nominatim.openstreetmap.org": 1.1}
_last_call: dict[str, float] = {}
_host_locks: dict[str, asyncio.Lock] = {}


class FetchError(RuntimeError):
    """Upstream unreachable or returning a bad status. Tools catch this and
    return a structured error, so the model reports an outage rather than
    inventing a number."""


class PermanentFetchError(FetchError):
    """A 4xx that retrying cannot fix: a bad key, a missing resource, a malformed
    query. Subclasses FetchError so callers still catch one thing."""


def _cache_key(url: str, params: dict[str, Any] | None) -> Path:
    raw = url + "?" + json.dumps(params or {}, sort_keys=True)
    return CACHE_DIR / (hashlib.sha256(raw.encode()).hexdigest()[:32] + ".json")


async def _throttle(host: str) -> None:
    interval = _HOST_MIN_INTERVAL.get(host)
    if not interval:
        return
    lock = _host_locks.setdefault(host, asyncio.Lock())
    async with lock:
        wait = interval - (time.monotonic() - _last_call.get(host, 0.0))
        if wait > 0:
            await asyncio.sleep(wait)
        _last_call[host] = time.monotonic()



_client: httpx.AsyncClient | None = None
_client_lock = asyncio.Lock()


async def _shared_client() -> httpx.AsyncClient:
    """One client for the process.

    A client per request meant a fresh TLS handshake every time, which the
    climate fan-out pays sixteen times over on a cold cache.
    """
    global _client
    if _client is None or _client.is_closed:
        async with _client_lock:
            if _client is None or _client.is_closed:
                _client = httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True)
    return _client


def _write_cache(path: Path, payload: Any) -> None:
    """Write through a temporary file and replace.

    A direct write leaves a truncated file if the process dies mid-write, and the
    next read then throws away a cache entry that was only half saved.
    """
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        tmp.unlink(missing_ok=True)


async def get_json(
    url: str,
    params: dict[str, Any] | None = None,
    *,
    ttl_seconds: int = 3600,
    headers: dict[str, str] | None = None,
    attempts: int = 3,
) -> Any:
    """GET a JSON document, with cache and bounded retry.

    ttl_seconds=0 disables the cache for that call.
    """
    path = _cache_key(url, params)
    fresh = ttl_seconds > 0 and path.exists() and time.time() - path.stat().st_mtime < ttl_seconds
    if fresh:
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            path.unlink(missing_ok=True)

    host = httpx.URL(url).host
    merged = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    merged.update(headers or {})

    last_error: Exception | None = None
    for attempt in range(attempts):
        await _throttle(host)
        try:
            client = await _shared_client()
            response = await client.get(url, params=params, headers=merged)
            if response.status_code == 429 or response.status_code >= 500:
                # Why a limit was hit is only in the body and headers. Logged on
                # the server, so a per-minute limit can be told apart from a
                # spent daily quota; the error the tool returns stays short.
                print(
                    f"{host} returned {response.status_code} "
                    f"(retry-after={response.headers.get('retry-after')!r}): "
                    f"{response.text[:200]!r}",
                    file=sys.stderr,
                )
                raise FetchError(f"{host} returned {response.status_code}")
            if response.status_code >= 400:
                # Deterministic; retrying will not help.
                raise PermanentFetchError(
                    f"{host} returned {response.status_code}: {response.text[:200]}"
                )
            payload = response.json()
        except PermanentFetchError:
            # Must precede the retry branch: FetchError is caught there, so a
            # hard 4xx was being retried anyway and cost three attempts.
            raise
        except (httpx.HTTPError, json.JSONDecodeError, FetchError) as exc:
            last_error = exc
            if attempt < attempts - 1:
                await asyncio.sleep(0.6 * (2**attempt))
            continue

        if ttl_seconds > 0:
            _write_cache(path, payload)
        return payload

    # Upstream is down. A cached copy past its ttl is old, not wrong, and for
    # climate normals that hold for sixty days it is almost certainly still
    # accurate. Better than failing the tool outright.
    if ttl_seconds > 0 and path.exists():
        try:
            stale = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            stale = None
        if stale is not None:
            age_hours = (time.time() - path.stat().st_mtime) / 3600
            print(
                f"{host} unreachable; serving a cached copy {age_hours:.1f}h old",
                file=sys.stderr,
            )
            return stale

    raise FetchError(f"could not fetch {url}: {last_error}")
