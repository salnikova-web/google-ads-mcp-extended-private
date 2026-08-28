# Copyright 2026 the google-ads-mcp-extended contributors.
# Licensed under the Apache License, Version 2.0.

"""Tests for the error-translating middleware.

Two levels are covered. The unit tests drive ``on_call_tool`` with a stub
``call_next`` that raises one exception class at a time. The mounted test
goes through a real parent server, which is the only way to prove the
translation survives FastMCP wrapping the tool's exception in a
``ToolError`` before any middleware runs.
"""

import logging
import unittest
from unittest.mock import MagicMock, patch

import grpc
import google.api_core.exceptions as api_exceptions
import google.auth.exceptions as auth_exceptions
import mcp.types as mt
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import MiddlewareContext
from google.ads.googleads.errors import GoogleAdsException

from ads_mcp import utils
from ads_mcp.config import ToolsConfig
from ads_mcp.coordinator import initialize_and_mount_tools
from ads_mcp.middleware import GoogleAdsErrorMiddleware


class FakeRpcError(grpc.RpcError):
    """A gRPC error with the accessors the middleware reads.

    Real ``_InactiveRpcError`` instances cannot be built outside a live
    channel, but they are only ever inspected through ``code()``,
    ``details()`` and ``debug_error_string()``.
    """

    def __init__(self, code, details="", debug_string=""):
        super().__init__(details or debug_string or str(code))
        self._code = code
        self._details = details
        self._debug_string = debug_string

    def code(self):
        return self._code

    def details(self):
        return self._details

    def debug_error_string(self):
        return self._debug_string


def make_google_ads_exception(
    message="Invalid field name",
    error_code="query_error: UNRECOGNIZED_FIELD",
    request_id="req-123",
):
    """Builds a GoogleAdsException with the attributes the formatter reads."""
    error = MagicMock()
    error.message = message
    error.error_code = error_code
    error.location.field_path_elements = []
    failure = MagicMock()
    failure.errors = [error]

    exception = GoogleAdsException(
        MagicMock(), MagicMock(), MagicMock(), MagicMock()
    )
    exception.failure = failure
    exception.request_id = request_id
    return exception


def make_context(name="search_search"):
    return MiddlewareContext(
        message=mt.CallToolRequestParams(name=name, arguments={}),
        method="tools/call",
    )


class TestErrorMiddleware(unittest.IsolatedAsyncioTestCase):
    """Drives the middleware hook directly with a failing call_next."""

    def setUp(self):
        utils.clear_googleads_cache()
        self.middleware = GoogleAdsErrorMiddleware()

    async def _raise_through(self, exception):
        """Runs the hook over a call_next that raises, returns the outcome."""

        async def call_next(context):
            raise exception

        with self.assertRaises(Exception) as caught:
            await self.middleware.on_call_tool(
                make_context(), call_next=call_next
            )
        return caught.exception

    async def _message_for(self, exception):
        raised = await self._raise_through(exception)
        self.assertIsInstance(raised, ToolError)
        return str(raised)

    async def test_successful_call_passes_result_through(self):
        async def call_next(context):
            return "ok"

        result = await self.middleware.on_call_tool(
            make_context(), call_next=call_next
        )
        self.assertEqual(result, "ok")

    async def test_google_ads_exception_uses_shared_formatter(self):
        message = await self._message_for(make_google_ads_exception())
        self.assertIn("Request ID: req-123", message)
        self.assertIn("Google Ads API Error: Invalid field name", message)
        self.assertIn("get_resource_metadata", message)

    async def test_wrapped_google_ads_exception_is_still_translated(self):
        """FastMCP wraps tool exceptions before middleware sees them."""
        original = make_google_ads_exception()
        wrapped = ToolError("Error calling tool 'search_search'")
        wrapped.__cause__ = original

        message = await self._message_for(wrapped)
        self.assertIn("Request ID: req-123", message)

    async def test_invalid_grant_rpc_error_is_not_retryable(self):
        error = FakeRpcError(
            grpc.StatusCode.UNKNOWN,
            details="Getting metadata from plugin failed with error: "
            "('invalid_grant: Token has been expired or revoked.', {})",
        )
        message = await self._message_for(error)
        self.assertIn("NOT retryable", message)
        self.assertIn("Re-authenticate", message)

    async def test_unauthenticated_rpc_error_is_not_retryable(self):
        error = FakeRpcError(
            grpc.StatusCode.UNAUTHENTICATED, details="Request had bad auth."
        )
        message = await self._message_for(error)
        self.assertIn("NOT retryable", message)

    async def test_invalid_grant_only_in_debug_string_is_matched(self):
        error = FakeRpcError(
            grpc.StatusCode.UNKNOWN,
            details="Stream removed",
            debug_string="UNKNOWN:Error received ... invalid_grant ...",
        )
        message = await self._message_for(error)
        self.assertIn("NOT retryable", message)

    async def test_refresh_error_is_not_retryable(self):
        message = await self._message_for(
            auth_exceptions.RefreshError("invalid_grant")
        )
        self.assertIn("NOT retryable", message)

    async def test_unavailable_is_transient_with_mutate_caution(self):
        error = FakeRpcError(
            grpc.StatusCode.UNAVAILABLE,
            details="failed to connect to all addresses",
        )
        message = await self._message_for(error)
        self.assertIn("transport error (UNAVAILABLE)", message)
        self.assertIn("failed to connect to all addresses", message)
        self.assertIn("Transient", message)
        self.assertIn("retry the same call ONCE", message)
        self.assertIn("confirm=true", message)
        self.assertIn("mutate_list_campaigns", message)

    async def test_deadline_exceeded_is_transient(self):
        error = FakeRpcError(
            grpc.StatusCode.DEADLINE_EXCEEDED, details="Deadline Exceeded"
        )
        message = await self._message_for(error)
        self.assertIn("transport error (DEADLINE_EXCEEDED)", message)
        self.assertIn("Transient", message)

    async def test_api_core_service_unavailable_is_transient(self):
        message = await self._message_for(
            api_exceptions.ServiceUnavailable("backend is down")
        )
        self.assertIn("transport error (UNAVAILABLE)", message)
        self.assertIn("Transient", message)

    async def test_api_core_deadline_exceeded_is_transient(self):
        message = await self._message_for(
            api_exceptions.DeadlineExceeded("too slow")
        )
        self.assertIn("transport error (DEADLINE_EXCEEDED)", message)

    async def test_api_core_internal_server_error_is_transient(self):
        message = await self._message_for(
            api_exceptions.InternalServerError("internal")
        )
        self.assertIn("transport error (INTERNAL)", message)

    async def test_long_detail_is_truncated(self):
        error = FakeRpcError(grpc.StatusCode.UNAVAILABLE, details="x" * 5000)
        message = await self._message_for(error)
        self.assertLess(len(message), 700)
        self.assertIn("...", message)

    async def test_default_credentials_error_is_explained(self):
        message = await self._message_for(
            auth_exceptions.DefaultCredentialsError("no ADC")
        )
        self.assertIn("Application Default Credentials", message)
        self.assertIn("Not retryable", message)

    async def test_deliberate_tool_error_passes_through_identically(self):
        original = ToolError("Expected a numeric id, got: 'abc'")
        raised = await self._raise_through(original)
        self.assertIs(raised, original)

    async def test_module_handler_output_is_never_double_wrapped(self):
        """A ToolError already formatted by utils must survive untouched.

        Reproduces the real shape: the tool module is inside an
        ``except GoogleAdsException`` block, so the ToolError carries the
        exception in ``__context__`` but not in ``__cause__``. Only the
        cause chain may be translated, or every handled failure would be
        reformatted and logged twice.
        """
        try:
            raise make_google_ads_exception()
        except GoogleAdsException as ads_error:
            try:
                utils.raise_tool_error(ads_error)
            except ToolError as formatted:
                original = formatted

        self.assertIsNone(original.__cause__)
        self.assertIsNotNone(original.__context__)

        with self.assertNoLogs("ads_mcp.middleware", level=logging.WARNING):
            raised = await self._raise_through(original)
        self.assertIs(raised, original)

    async def test_unrelated_grpc_error_is_left_alone(self):
        """A gRPC status the middleware has no advice for must not be hidden."""
        original = FakeRpcError(
            grpc.StatusCode.INVALID_ARGUMENT, details="bad request"
        )
        raised = await self._raise_through(original)
        self.assertIs(raised, original)

    async def test_value_error_is_re_raised_unchanged(self):
        original = ValueError("GOOGLE_ADS_DEVELOPER_TOKEN not set.")
        raised = await self._raise_through(original)
        self.assertIs(raised, original)

    async def test_translated_failure_is_logged_server_side(self):
        error = FakeRpcError(
            grpc.StatusCode.UNAVAILABLE, details="channel closed"
        )
        with self.assertLogs(
            "ads_mcp.middleware", level=logging.WARNING
        ) as logs:
            await self._raise_through(error)
        self.assertEqual(len(logs.records), 1)
        self.assertIn("google-ads tool error", logs.output[0])
        self.assertIn("channel closed", logs.output[0])

    async def test_untranslated_failure_is_not_logged_by_middleware(self):
        with self.assertNoLogs("ads_mcp.middleware", level=logging.WARNING):
            await self._raise_through(ValueError("boom"))


class TestRaiseToolErrorLogging(unittest.TestCase):
    """The shared GoogleAdsException formatter logs the reason too."""

    def test_raise_tool_error_logs_before_raising(self):
        with self.assertLogs("ads_mcp.utils", level=logging.WARNING) as logs:
            with self.assertRaises(ToolError):
                utils.raise_tool_error(make_google_ads_exception())
        self.assertIn("google-ads tool error", logs.output[0])
        self.assertIn("Request ID: req-123", logs.output[0])


class TestMiddlewareOnMountedServer(unittest.IsolatedAsyncioTestCase):
    """End-to-end: the translation must survive FastMCP's own wrapping."""

    def setUp(self):
        utils.clear_googleads_cache()

    def _mount(self):
        parent = FastMCP("Middleware Test Parent")
        with patch(
            "ads_mcp.config.ToolsConfig.load",
            return_value=ToolsConfig({"namespaces": {"customers": True}}),
        ):
            initialize_and_mount_tools(parent)
        return parent

    async def test_transport_error_from_handler_less_tool_is_translated(self):
        """core.py's list_accessible_customers has no error handling."""
        parent = self._mount()
        error = FakeRpcError(
            grpc.StatusCode.UNAVAILABLE,
            details="failed to connect to all addresses",
        )
        with patch("ads_mcp.utils.get_googleads_service", side_effect=error):
            with self.assertRaises(ToolError) as caught:
                await parent.call_tool(
                    "customers_list_accessible_customers", {}
                )

        message = str(caught.exception)
        self.assertIn("transport error (UNAVAILABLE)", message)
        self.assertIn("Transient", message)
        self.assertIn("confirm=true", message)

    async def test_oauth_failure_from_handler_less_tool_is_translated(self):
        parent = self._mount()
        error = auth_exceptions.RefreshError(
            "('invalid_grant: Token has been expired or revoked.', {})"
        )
        with patch("ads_mcp.utils.get_googleads_service", side_effect=error):
            with self.assertRaises(ToolError) as caught:
                await parent.call_tool(
                    "customers_list_accessible_customers", {}
                )

        self.assertIn("NOT retryable", str(caught.exception))

    async def test_middleware_is_registered_exactly_once(self):
        parent = self._mount()
        translators = [
            mw
            for mw in parent.middleware
            if isinstance(mw, GoogleAdsErrorMiddleware)
        ]
        self.assertEqual(len(translators), 1)

        # A second mount on the same server must not stack another copy.
        with patch(
            "ads_mcp.config.ToolsConfig.load",
            return_value=ToolsConfig({"namespaces": {"customers": True}}),
        ):
            initialize_and_mount_tools(parent)
        translators = [
            mw
            for mw in parent.middleware
            if isinstance(mw, GoogleAdsErrorMiddleware)
        ]
        self.assertEqual(len(translators), 1)


if __name__ == "__main__":
    unittest.main()
