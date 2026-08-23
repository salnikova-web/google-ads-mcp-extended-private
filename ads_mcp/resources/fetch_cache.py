# Copyright 2026 the google-ads-mcp-extended contributors.
# Licensed under the Apache License, Version 2.0.

"""Bounded, cached fetching shared by the documentation resources.

Every resource in this package proxies a large upstream page (the discovery
JSON alone is multi-megabyte), and MCP clients tend to read the same
resource repeatedly within a session. A small in-memory TTL cache keeps
those repeat reads from re-downloading identical content, while the
timeout and read cap ensure a hung or runaway upstream host fails the
single read instead of pinning a worker thread or exhausting memory.
"""

import threading
import time
import urllib.request

from fastmcp.exceptions import ResourceError

__all__ = [
    "CACHE_TTL_SECONDS",
    "FETCH_TIMEOUT_SECONDS",
    "clear_cache",
    "fetch_text",
]

# The upstream pages are reference documentation that changes at most a few
# times per day, so an hour of staleness is invisible to callers while it
# spares a multi-megabyte download on every repeat read.
CACHE_TTL_SECONDS = 60 * 60

# A hung upstream host should fail this one read, not block indefinitely.
FETCH_TIMEOUT_SECONDS = 30

_lock = threading.Lock()

# url -> (time.monotonic() at fetch, decoded body)
_entries: dict[str, tuple[float, str]] = {}


def fetch_text(url: str, max_bytes: int) -> str:
    """Returns the UTF-8 body of ``url``, cached for ``CACHE_TTL_SECONDS``.

    Args:
        url: The URL to fetch.
        max_bytes: Maximum accepted body size; a larger response is
            rejected without being buffered in full.

    Raises:
        ResourceError: If the response body exceeds ``max_bytes``.
    """
    with _lock:
        entry = _entries.get(url)
        if entry and time.monotonic() - entry[0] < CACHE_TTL_SECONDS:
            return entry[1]

    # The fetch happens outside the lock so one slow host cannot serialise
    # reads of the other resources; a concurrent duplicate fetch of the
    # same URL is harmless.
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0"},
    )
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_SECONDS) as response:
        # Reading one byte past the cap detects an oversized body without
        # buffering an unbounded response in memory.
        body = response.read(max_bytes + 1)
    if len(body) > max_bytes:
        raise ResourceError(
            f"Response from {url} exceeds the {max_bytes} byte limit; "
            "refusing to return a truncated document."
        )
    text = body.decode("utf-8")

    with _lock:
        _entries[url] = (time.monotonic(), text)
    return text


def clear_cache() -> None:
    """Drops all cached bodies so the next read refetches."""
    with _lock:
        _entries.clear()
