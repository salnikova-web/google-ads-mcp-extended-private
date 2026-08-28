# Copyright 2026 the google-ads-mcp-extended contributors.
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

"""Test cases for the gRPC header interceptor.

These pin the header value the server actually puts on the wire, so the
version helper can be moved out of the class body without anyone having to
take "the header is unchanged" on trust.
"""

import collections
import importlib.metadata
import unittest
from unittest.mock import patch

from ads_mcp import mcp_header_interceptor
from ads_mcp.mcp_header_interceptor import MCPHeaderInterceptor

_CallDetails = collections.namedtuple(
    "_CallDetails", ["method", "timeout", "metadata", "credentials"]
)


def _details(metadata):
    return _CallDetails(
        method="/g.a.g.v24.services.GoogleAdsService/Search",
        timeout=None,
        metadata=metadata,
        credentials=None,
    )


class _Continuation:
    """Records the call details the interceptor forwards."""

    def __init__(self, response="response"):
        self.response = response
        self.seen = None

    def __call__(self, client_call_details, request):
        self.seen = client_call_details
        return self.response


class TestPackageVersion(unittest.TestCase):
    """The version helper is a module-level function, and callable."""

    def test_returns_the_installed_version(self):
        self.assertEqual(
            mcp_header_interceptor._get_package_version_with_fallback(),
            importlib.metadata.version("google-ads-mcp"),
        )

    def test_falls_back_to_unknown(self):
        with patch.object(
            mcp_header_interceptor.metadata,
            "version",
            side_effect=importlib.metadata.PackageNotFoundError,
        ):
            self.assertEqual(
                mcp_header_interceptor._get_package_version_with_fallback(),
                "unknown",
            )

    def test_extra_header_value(self):
        """The exact string appended to x-goog-api-client."""
        self.assertEqual(
            mcp_header_interceptor._MCP_EXTRA_HEADER,
            " google-ads-mcp/" + importlib.metadata.version("google-ads-mcp"),
        )


class TestMCPHeaderInterceptor(unittest.TestCase):
    """Test cases for the metadata rewriting."""

    def setUp(self):
        self.interceptor = MCPHeaderInterceptor()
        self.continuation = _Continuation()
        self.suffix = mcp_header_interceptor._MCP_EXTRA_HEADER

    def test_appends_to_the_api_client_header(self):
        details = _details(
            [
                ("x-goog-api-client", "gl-python/3.12 gapic/1.0"),
                ("developer-token", "REDACTED"),
            ]
        )

        result = self.interceptor.intercept_unary_unary(
            self.continuation, details, "request"
        )

        self.assertEqual(result, "response")
        self.assertEqual(
            self.continuation.seen.metadata,
            [
                (
                    "x-goog-api-client",
                    "gl-python/3.12 gapic/1.0" + self.suffix,
                ),
                ("developer-token", "REDACTED"),
            ],
        )

    def test_preserves_header_order(self):
        details = _details(
            [
                ("first", "1"),
                ("x-goog-api-client", "base"),
                ("last", "2"),
            ]
        )

        self.interceptor.intercept_unary_stream(
            self.continuation, details, "request"
        )

        self.assertEqual(
            [key for key, _ in self.continuation.seen.metadata],
            ["first", "x-goog-api-client", "last"],
        )
        self.assertEqual(
            self.continuation.seen.metadata[1][1], "base" + self.suffix
        )

    def test_does_not_append_twice(self):
        already = "base" + self.suffix
        details = _details([("x-goog-api-client", already)])

        self.interceptor.intercept_unary_unary(
            self.continuation, details, "request"
        )

        self.assertEqual(
            self.continuation.seen.metadata, [("x-goog-api-client", already)]
        )

    def test_no_metadata_at_all(self):
        self.interceptor.intercept_unary_unary(
            self.continuation, _details(None), "request"
        )
        self.assertEqual(self.continuation.seen.metadata, [])

    def test_other_headers_are_left_alone(self):
        details = _details([("x-goog-request-params", "customer_id=1")])

        self.interceptor.intercept_unary_unary(
            self.continuation, details, "request"
        )

        self.assertEqual(
            self.continuation.seen.metadata,
            [("x-goog-request-params", "customer_id=1")],
        )

    def test_a_broken_call_forwards_the_original_details(self):
        """A failure in the interceptor must never fail the RPC."""
        details = _details([("x-goog-api-client", object())])

        with self.assertLogs(mcp_header_interceptor.logger, "ERROR"):
            result = self.interceptor.intercept_unary_unary(
                self.continuation, details, "request"
            )

        self.assertEqual(result, "response")
        self.assertIs(self.continuation.seen, details)


if __name__ == "__main__":
    unittest.main()
