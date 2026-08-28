# Copyright 2026 the google-ads-mcp-extended contributors.
# Licensed under the Apache License, Version 2.0.

"""Translates infrastructure failures into actionable tool errors.

Only ``GoogleAdsException`` is handled inside the tool modules (see
``utils.raise_tool_error``). Everything below the API surface -- a dropped
gRPC channel, a deadline, an OAuth refresh token that was revoked, missing
Application Default Credentials -- used to reach the agent as a multi-KB
dump of the raw exception with no guidance, and was blind-retried. This
middleware turns each of those classes into one short instruction that says
whether the call is retryable, and logs the reason server-side so failures
are visible in the host's log instead of only in the agent's transcript.

Where the exception is caught
-----------------------------
FastMCP wraps any non-``FastMCPError`` raised by a tool before the
middleware chain ever runs: ``FastMCP.call_tool`` re-raises it as
``ToolError(...) from original`` (fastmcp/server/server.py). By the time
``on_call_tool`` sees a transport failure it is therefore a ``ToolError``
carrying the real exception in ``__cause__``, so this middleware inspects
the cause chain rather than the exception it caught. A ``ToolError`` raised
deliberately by a tool module has no ``__cause__`` and no translatable
exception anywhere in its chain, so it passes through untouched and is
never double-wrapped.

That last sentence is a rule, not an observation: **no module outside this
one may write** ``raise ToolError(msg) from <exception>``. Chaining a
hand-written ToolError to a translatable cause makes this middleware
discard that message, re-format a generic one from the cause, and log the
failure twice. Raise it bare (the original is still reachable through
``__context__``), or use ``from None``. The rule is enforced over the
source by ``tests/tools/middleware_test.py``.
"""

import logging
from typing import Any, Iterator

import grpc
import google.api_core.exceptions as api_exceptions
import google.auth.exceptions as auth_exceptions
from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from google.ads.googleads.errors import GoogleAdsException

import ads_mcp.utils as utils

logger = logging.getLogger(__name__)
# Configuring the root logger is the host application's job; see utils.
logger.addHandler(logging.NullHandler())


_OAUTH_MESSAGE = (
    "OAuth credentials expired or revoked — NOT retryable. Re-authenticate "
    "(refresh the ADC / refresh token) before any further Google Ads calls."
)

_NO_CREDENTIALS_MESSAGE = (
    "No Google credentials found — configure Application Default Credentials "
    "(gcloud auth application-default login) and restart the server. "
    "Not retryable."
)

# Substrings that identify a failed OAuth refresh. These surface mid-RPC:
# the auth plugin fails while the call is in flight, so the agent gets a
# transport-looking gRPC error rather than an authentication error.
_OAUTH_MARKERS = (
    "invalid_grant",
    "token has been expired or revoked",
    "getting metadata from plugin failed",
)

# gRPC status codes worth one retry.
_TRANSIENT_CODES = (
    grpc.StatusCode.UNAVAILABLE,
    grpc.StatusCode.DEADLINE_EXCEEDED,
    grpc.StatusCode.INTERNAL,
)

# The google-api-core equivalents, mapped to the status code they carry.
_TRANSIENT_API_CORE = (
    (api_exceptions.ServiceUnavailable, "UNAVAILABLE"),
    (api_exceptions.DeadlineExceeded, "DEADLINE_EXCEEDED"),
    (api_exceptions.InternalServerError, "INTERNAL"),
)

# Long enough to identify the failure, short enough that the agent reads it.
_MAX_DETAIL_CHARS = 200

# FastMCP wraps once; a couple of extra links guard against a nested mount.
_MAX_CAUSE_DEPTH = 5


def _transport_message(code: str, detail: str) -> str:
    return (
        f"Google Ads API transport error ({code}): {detail}. Transient — "
        "wait a few seconds and retry the same call ONCE. Caution: if this "
        "was a mutate with confirm=true, the change may or may not have "
        "been applied — verify state with a read (e.g. mutate_list_campaigns "
        "/ search_search) before re-applying."
    )


def _call_quietly(ex: BaseException, name: str) -> str:
    """Returns the result of a zero-argument accessor, or "" if unusable.

    ``details``/``debug_error_string`` are methods on gRPC errors but plain
    attributes elsewhere, and a synthetic error may not have them at all.
    """
    accessor = getattr(ex, name, None)
    if not callable(accessor):
        return ""
    try:
        return str(accessor() or "")
    except Exception:  # pragma: no cover - defensive
        return ""


def _collapse(text: str) -> str:
    return " ".join(text.split())


def _short_detail(ex: BaseException) -> str:
    """Returns a one-line description of a transport failure."""
    text = _collapse(_call_quietly(ex, "details")) or _collapse(str(ex))
    if not text:
        return "no details"
    if len(text) > _MAX_DETAIL_CHARS:
        text = text[:_MAX_DETAIL_CHARS].rstrip() + "..."
    return text


def _rpc_haystack(ex: BaseException) -> str:
    """Returns the text of a gRPC error to match OAuth markers against."""
    parts = [
        _call_quietly(ex, "details"),
        _call_quietly(ex, "debug_error_string"),
        str(ex),
    ]
    return " ".join(parts).lower()


def _rpc_code(ex: BaseException) -> Any:
    """Returns a gRPC error's status code, or None if it has none."""
    code = getattr(ex, "code", None)
    if not callable(code):
        return None
    try:
        return code()
    except Exception:  # pragma: no cover - defensive
        return None


def _code_name(code: Any) -> str:
    return str(getattr(code, "name", None) or code)


def _translate(ex: BaseException) -> tuple[str, str | None] | None:
    """Returns (agent-facing message, log-only marker) for ``ex``, or None
    to leave it alone.

    The marker names which OAuth signal fired (a gRPC status-code name or
    the matched substring) and is for the server-side log line only: never
    the ``ToolError`` message the agent sees, and never the raw haystack it
    was matched against (which can otherwise carry OAuth error internals).
    It lets an operator tell which condition fired straight from the log
    line without re-deriving it from the fixed message text. Every branch
    that does not distinguish OAuth signals returns None for the marker.

    ``GoogleAdsException`` is deliberately excluded: it has its own formatter
    in ``utils.raise_tool_error`` (request id, per-error codes and field
    paths), which the caller delegates to instead.
    """
    if isinstance(ex, auth_exceptions.DefaultCredentialsError):
        return _NO_CREDENTIALS_MESSAGE, None

    if isinstance(ex, auth_exceptions.RefreshError):
        return _OAUTH_MESSAGE, type(ex).__name__

    if isinstance(ex, grpc.RpcError):
        code = _rpc_code(ex)
        haystack = _rpc_haystack(ex)
        if code is grpc.StatusCode.UNAUTHENTICATED:
            return _OAUTH_MESSAGE, _code_name(code)
        matched_marker = next(
            (marker for marker in _OAUTH_MARKERS if marker in haystack),
            None,
        )
        if matched_marker is not None:
            return _OAUTH_MESSAGE, matched_marker
        if code in _TRANSIENT_CODES:
            return (
                _transport_message(_code_name(code), _short_detail(ex)),
                None,
            )
        return None

    for error_type, code_name in _TRANSIENT_API_CORE:
        if isinstance(ex, error_type):
            return _transport_message(code_name, _short_detail(ex)), None

    return None


def _cause_chain(ex: BaseException) -> Iterator[BaseException]:
    """Yields ``ex`` and its ``__cause__`` chain, outermost first."""
    seen = set()
    current: BaseException | None = ex
    for _ in range(_MAX_CAUSE_DEPTH):
        if current is None or id(current) in seen:
            return
        seen.add(id(current))
        yield current
        current = current.__cause__


class GoogleAdsErrorMiddleware(Middleware):
    """Rewrites transport and credential failures as actionable ToolErrors.

    Registered once on the parent server (see
    ``coordinator.initialize_and_mount_tools``), so it covers every mounted
    namespace, including tools with no error handling of their own.
    """

    async def on_call_tool(
        self,
        context: MiddlewareContext[Any],
        call_next: CallNext[Any, Any],
    ) -> Any:
        try:
            return await call_next(context)
        except Exception as ex:
            for original in _cause_chain(ex):
                if isinstance(original, GoogleAdsException):
                    # Logs and raises with the request id and field paths.
                    utils.raise_tool_error(original)
                translated = _translate(original)
                if translated is not None:
                    message, log_marker = translated
                    if log_marker is not None:
                        logger.warning(
                            "google-ads tool error [%s]:\n%s",
                            log_marker,
                            message,
                        )
                    else:
                        logger.warning("google-ads tool error:\n%s", message)
                    raise ToolError(message) from ex
            # Not ours: a deliberate ToolError from a tool module, or a bug
            # that must keep its traceback.
            raise
