#!/usr/bin/env python

# Copyright 2026 Google LLC.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Common utilities used by the MCP server."""

from typing import Any, NoReturn
import proto
from google.protobuf.message import Message as PbMessage
from google.protobuf.json_format import MessageToDict
import logging
from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException

from fastmcp.exceptions import ToolError
from google.ads.googleads.util import get_nested_attr
import google.auth
from ads_mcp.mcp_header_interceptor import MCPHeaderInterceptor
import collections
import hashlib
import os
import importlib.resources
import contextlib
import re
import subprocess
import threading
import time

# filename for generated field information used by search
_GAQL_FILENAME = "gaql_resources.txt"

logger = logging.getLogger(__name__)
# No basicConfig here: configuring the root logger is the host application's
# job. Doing it at import time would hijack logging for the whole process, and
# under the stdio transport anything written to stdout corrupts the JSON-RPC
# stream. A NullHandler keeps library logging silent until a host configures it.
logger.addHandler(logging.NullHandler())

# The only OAuth scope Google Ads publishes, and it is full read/write: there
# is no read-only variant to fall back on, and this server does use the write
# half (84 write tools across 13 of the 16 namespaces). Nothing here limits
# what a caller can change -- write safety comes from the dry-run-by-default
# layer in the tools themselves (`confirm=False` previews, `validate_only`
# requests), not from the credential.
_ADS_SCOPE = "https://www.googleapis.com/auth/adwords"


# --- GAQL literal helpers ---------------------------------------------------
#
# GAQL string literals are single-quoted, so any user-supplied value spliced
# into a query must have backslashes and quotes escaped, or an apostrophe ends
# the literal and the rest of the value is parsed as query syntax.

_GAQL_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_DIGITS_ONLY = re.compile(r"\A[0-9]+\Z")


def gaql_str(value: Any) -> str:
    """Escapes a value for use inside a single-quoted GAQL string literal.

    Returns the escaped *inner* text; the caller keeps its own quotes::

        f"WHERE campaign.name = '{utils.gaql_str(name)}'"
    """
    text = str(value)
    if _GAQL_CONTROL_CHARS.search(text):
        raise ToolError(
            "Value contains control characters and cannot be used in a query."
        )
    return text.replace("\\", "\\\\").replace("'", "\\'")


def gaql_id(value: Any) -> str:
    """Returns a numeric id as a string, rejecting anything else.

    Used for ids that end up inside resource names or query conditions, where
    a non-numeric value would be an injection vector.
    """
    text = str(value).strip()
    if not _DIGITS_ONLY.match(text):
        raise ToolError(f"Expected a numeric id, got: {value!r}")
    return text


# Serialises the window in which `subprocess.Popen` is swapped out below.
# Reentrant so a nested use on one thread restores in the right order instead
# of deadlocking; the enclosing context's wrapper becomes the inner one's
# "original".
_popen_patch_lock = threading.RLock()


@contextlib.contextmanager
def prevent_stdio_inheritance():
    """Prevents child processes from inheriting the parent's stdio handles.

    Fixes a deadlock on Windows where `google.auth.default()` spawns `gcloud`
    via subprocess without redirecting stdin, causing it to inherit the
    ProactorEventLoop's overlapping I/O handles used by MCP's stdio transport.

    The swap is process-global, so it is guarded by a lock: two threads
    building credentials at once (FastMCP serves tool calls from a thread
    pool) would otherwise interleave save and restore -- thread B saving the
    wrapper as its "original" while A restores the real Popen -- and leave the
    wrapper installed for the rest of the process's life. Holding the lock for
    the whole body means the credential-construction critical section is
    serialised and the swap is always undone.
    """
    with _popen_patch_lock:
        original_popen = subprocess.Popen

        def safe_popen(*args, **kwargs):
            if kwargs.get("stdin") is None:
                kwargs["stdin"] = subprocess.DEVNULL
            return original_popen(*args, **kwargs)

        subprocess.Popen = safe_popen
        try:
            yield
        finally:
            subprocess.Popen = original_popen


def _create_credentials() -> google.auth.credentials.Credentials:
    """Returns Application Default Credentials with the Google Ads scope, or the FastMCP token if found."""
    from fastmcp.server.dependencies import get_access_token
    from google.oauth2.credentials import Credentials

    token_obj = get_access_token()
    if token_obj and token_obj.token:
        # Create credentials using the access token provided by FastMCP
        return Credentials(token=token_obj.token)

    with prevent_stdio_inheritance():
        credentials, _ = google.auth.default(scopes=[_ADS_SCOPE])
    return credentials


def _get_developer_token() -> str:
    """Returns the developer token from the environment variable GOOGLE_ADS_DEVELOPER_TOKEN."""
    dev_token = os.environ.get("GOOGLE_ADS_DEVELOPER_TOKEN")
    if dev_token is None:
        raise ValueError(
            "GOOGLE_ADS_DEVELOPER_TOKEN environment variable not set."
        )
    return dev_token


def _get_login_customer_id() -> str | None:
    """Returns login customer id, if set, from the environment variable GOOGLE_ADS_LOGIN_CUSTOMER_ID."""
    return os.environ.get("GOOGLE_ADS_LOGIN_CUSTOMER_ID")


def _build_googleads_client() -> GoogleAdsClient:
    args = {
        "credentials": _create_credentials(),
        "developer_token": _get_developer_token(),
        "use_proto_plus": True,
    }

    # If the login-customer-id is not set, avoid setting None.
    login_customer_id = _get_login_customer_id()

    if login_customer_id:
        args["login_customer_id"] = login_customer_id

    client = GoogleAdsClient(**args)

    return client


# --- client / service cache -------------------------------------------------
#
# Building a client per call re-reads Application Default Credentials from disk
# (and on Windows shells out to gcloud) and opens a fresh gRPC channel, which
# a single tool call may do three times over. Clients and services are cached
# instead, keyed by the identity of the credentials they were built with.
#
# Correctness rules this cache must never break:
#   * The key always includes the caller's identity. Under the HTTP transport
#     each request carries its own OAuth token, and a client built for one
#     token must never serve another user, so the key is recomputed on every
#     call and carries a hash of that token (never the token itself).
#   * Only clients and service stubs are cached. Values returned by
#     ``get_type`` are fresh, mutable protos that tools fill in; sharing one
#     across calls would let concurrent requests corrupt each other's payloads.
#   * Eviction (TTL expiry, LRU overflow, replacement) NEVER closes the
#     evicted service's gRPC transport. Nothing here can know whether another
#     thread is mid-RPC on that channel -- FastMCP dispatches tool calls from
#     a thread pool, and one parallel call is enough -- and closing it would
#     cancel that call in flight. For a ``confirm=true`` mutate the caller
#     would see a cancellation for a change that may well have landed, which
#     is the worst possible answer to give. So an evicted channel is simply
#     dropped: it stays alive as long as a live RPC holds a reference and is
#     finalized by the garbage collector afterwards. That is a deliberate
#     leak-to-GC, bounded by ``_CACHE_MAX_ENTRIES`` live entries plus whatever
#     in-flight calls still hold. ``clear_googleads_cache`` is the one place
#     that does close transports: it is a test/host-initiated teardown where
#     the caller is asserting there is nothing in flight.

_CACHE_MAX_ENTRIES = 64
# Comfortably below the ~1h lifetime of a Google OAuth access token, so an
# entry is retired before the token it was built from expires.
_CACHE_TTL_SECONDS = 45 * 60

_cache_lock = threading.Lock()
_cache: "collections.OrderedDict[tuple, tuple[float, Any]]" = (
    collections.OrderedDict()
)


def _credential_identity() -> tuple:
    """Returns a cache key fragment identifying the current caller.

    Under the HTTP transport this is a hash of the request's access token;
    under stdio there is no token and Application Default Credentials (which
    refresh themselves) are the single identity.
    """
    try:
        from fastmcp.server.dependencies import get_access_token

        token_obj = get_access_token()
    except Exception:  # no request context, or no auth configured
        token_obj = None

    if token_obj and token_obj.token:
        digest = hashlib.sha256(token_obj.token.encode("utf-8")).hexdigest()
        return ("oauth", digest)
    return ("adc",)


def _close_quietly(value: Any) -> None:
    """Closes a cached service's transport, ignoring failures.

    Only ``clear_googleads_cache`` may call this -- see the eviction rule in
    the section comment above.
    """
    transport = getattr(value, "transport", None)
    close = getattr(transport, "close", None)
    if close is None:
        return
    try:
        close()
    except Exception:  # pragma: no cover - best-effort cleanup
        logger.debug("Failed to close a cached transport", exc_info=True)


def _cache_get(key: tuple) -> Any:
    now = time.monotonic()
    with _cache_lock:
        entry = _cache.get(key)
        if entry is None:
            return None
        created, value = entry
        if now - created > _CACHE_TTL_SECONDS:
            # Dropped, not closed: a concurrent RPC may still be using it.
            del _cache[key]
            return None
        _cache.move_to_end(key)
        return value


def _cache_put(key: tuple, value: Any) -> None:
    with _cache_lock:
        # Replaced and LRU-overflowed entries are dropped, not closed: a
        # concurrent RPC may still be using them.
        _cache.pop(key, None)
        _cache[key] = (time.monotonic(), value)
        while len(_cache) > _CACHE_MAX_ENTRIES:
            _cache.popitem(last=False)


def clear_googleads_cache() -> None:
    """Drops every cached client and service.

    Exposed for tests and for hosts that need to force new credentials.
    """
    with _cache_lock:
        values = [value for _, value in _cache.values()]
        _cache.clear()
    for value in values:
        _close_quietly(value)


def _get_googleads_client() -> GoogleAdsClient:
    key = _credential_identity() + ("client", _get_login_customer_id())
    client = _cache_get(key)
    if client is None:
        client = _build_googleads_client()
        _cache_put(key, client)
    return client


def get_googleads_service(serviceName: str) -> Any:
    key = _credential_identity() + (
        "service",
        serviceName,
        _get_login_customer_id(),
    )
    service = _cache_get(key)
    if service is None:
        service = _get_googleads_client().get_service(
            serviceName, interceptors=[MCPHeaderInterceptor()]
        )
        _cache_put(key, service)
    return service


def get_googleads_type(typeName: str):
    # Deliberately not cached: callers mutate the message they get back.
    return _get_googleads_client().get_type(typeName)


def get_googleads_client():
    return _get_googleads_client()


def format_output_value(value: Any) -> Any:
    if isinstance(value, proto.Enum):
        return value.name
    elif isinstance(value, proto.Message):
        return proto.Message.to_dict(value)
    elif isinstance(value, PbMessage):
        return MessageToDict(value, preserving_proto_field_name=True)
    elif hasattr(value, "__iter__") and not isinstance(value, (str, bytes)):
        return [format_output_value(v) for v in value]
    else:
        return value


def format_output_row(row: proto.Message, attributes):
    return {
        attr: format_output_value(get_nested_attr(row, attr))
        for attr in attributes
    }


def get_gaql_resources_filepath():
    package_root = importlib.resources.files("ads_mcp")
    file_path = package_root.joinpath(_GAQL_FILENAME)
    return file_path


def truncation_warning(cap: int) -> str:
    """The standard warning for a list truncated at `cap` items.

    Truncation must never be silent: a missing item is not proof it does
    not exist, so every truncated list envelope carries this string and
    every truncating tool's docstring tells the agent to relay it.
    """
    return (
        f"list truncated at {cap} items — more exist beyond the cap; a "
        "missing item is NOT proof it does not exist. Raise 'limit' or "
        "narrow the filter before concluding absence, and tell the user "
        "the list is incomplete."
    )


def list_envelope(items: list[Any], cap: int) -> dict[str, Any]:
    """Wraps a probe-fetched list into the shared truncation envelope.

    `items` must already hold at most `cap + 1` rows (the caller fetches
    one extra "probe" row to detect overflow without a separate count
    query). Returns {"items", "returned", "truncated"} and, only when
    truncated, a "warning" key from `truncation_warning`.
    """
    truncated = len(items) > cap
    page = items[:cap] if truncated else items
    envelope: dict[str, Any] = {
        "items": page,
        "returned": len(page),
        "truncated": truncated,
    }
    if truncated:
        envelope["warning"] = truncation_warning(cap)
    return envelope


# Matched as substrings against "<error_code> <message>" uppercased; each
# matching hint is appended once to the raised ToolError.
#
# The first block keys on specific field names taken verbatim from real
# server logs: each of these was requested repeatedly across sessions,
# each failed the same way every time, and the generic "verify the field
# names" hint below never told the agent what to reach for instead. They
# come first so the specific advice is printed before the generic one.
_GOOGLE_ADS_ERROR_HINTS = (
    (
        # "Unrecognized field(s) in the query: 'campaign.start_date'[,
        # 'campaign.end_date']." - 8 occurrences.
        "'CAMPAIGN.START_DATE'",
        "campaign.start_date / campaign.end_date are not selectable - as "
        "a FILTER use segments.date conditions instead; as an ATTRIBUTE "
        "(the campaign's own launch date) look the campaign resource's "
        "real fields up with the metadata_get_resource_metadata tool - "
        "do not derive a launch date from metrics",
    ),
    (
        # "Unrecognized fields in the query: 'auction_insight.domain',
        # ..." - every auction_insight attempt, with metrics.* prefixes
        # too. The resource is not in the queryable resource list.
        "'AUCTION_INSIGHT.",
        "auction_insight is not a queryable GAQL resource in this API "
        "version - there is no search equivalent, Auction Insights has "
        "to be exported from the Google Ads UI",
    ),
    (
        # "Unrecognized field in the query: 'metrics.video_views'." -
        # seen on both the asset and the campaign resource.
        "'METRICS.VIDEO_VIEWS'",
        "metrics.video_views is not selectable for this resource - list "
        "the metrics the resource actually supports with the "
        "metadata_get_resource_metadata tool before re-querying",
    ),
    (
        # "Unrecognized field in the query:
        # 'campaign.url_expansion_opt_out'."
        "'CAMPAIGN.URL_EXPANSION_OPT_OUT'",
        "campaign.url_expansion_opt_out is not a selectable campaign "
        "field in this API version - look the campaign resource's real "
        "fields up with the metadata_get_resource_metadata tool rather "
        "than guessing the PMax URL-expansion setting's name",
    ),
    (
        "UNRECOGNIZED_FIELD",
        "verify field names with the metadata_get_resource_metadata tool",
    ),
    (
        "INVALID_FIELD",
        "verify field names with the metadata_get_resource_metadata tool",
    ),
    (
        "PROHIBITED_FIELD",
        "check field compatibility with the metadata_get_resource_metadata "
        "tool",
    ),
    (
        "AUTHENTICATION",
        "credential problem - not retryable, fix auth before retrying",
    ),
    (
        "AUTHORIZATION",
        "access problem - check customer_id / login customer, not retryable",
    ),
    (
        "QUOTA",
        "rate/quota exceeded - wait before retrying, reduce request size",
    ),
    (
        "RESOURCE_EXHAUSTED",
        "rate/quota exceeded - wait before retrying, reduce request size",
    ),
)


def raise_tool_error(ex: GoogleAdsException) -> NoReturn:
    """Raises a ToolError for a GoogleAdsException, shared by all modules.

    Per error: message + error code + offending field path; plus one
    deduplicated actionable hint line per matched failure class.

    ``NoReturn`` is the point of the annotation: the ~150 call sites are
    written as a bare ``raise_tool_error(ex)`` statement inside an ``except``
    block, and a type checker must know the function never falls through --
    otherwise every one of those handlers looks like it can continue and
    return ``None`` from a tool.
    """
    error_msgs = []
    hints = []
    for error in ex.failure.errors:
        field_path = ""
        if error.location and error.location.field_path_elements:
            field_path = ".".join(
                fpe.field_name for fpe in error.location.field_path_elements
            )
        code = str(error.error_code).strip().replace("\n", " ")
        msg = f"Google Ads API Error: {error.message} [{code}]"
        if field_path:
            msg += f" (field: {field_path})"
        error_msgs.append(msg)
        haystack = f"{code} {error.message}".upper()
        for needle, hint in _GOOGLE_ADS_ERROR_HINTS:
            if needle in haystack and hint not in hints:
                hints.append(hint)
    lines = [f"Request ID: {ex.request_id}"] + error_msgs
    lines.extend(f"Hint: {hint}." for hint in hints)
    message = "\n".join(lines)
    # The host records only the tool name when a call fails, so the reason
    # never reaches the server log. Record it here: request id, error codes
    # and field paths, none of which carry credentials.
    logger.warning("google-ads tool error:\n%s", message)
    raise ToolError(message)
