# Copyright 2026 the google-ads-mcp-extended contributors.
# Licensed under the Apache License, Version 2.0.

"""Tests for the shared resource fetch cache."""

import unittest
from unittest import mock

from fastmcp.exceptions import ResourceError

from ads_mcp.resources import fetch_cache

_URL = "https://example.com/docs"


def _mock_response(body: bytes) -> mock.MagicMock:
    response = mock.MagicMock()
    response.read.return_value = body
    response.__enter__.return_value = response
    return response


class FetchCacheTest(unittest.TestCase):
    def setUp(self):
        fetch_cache.clear_cache()
        self.addCleanup(fetch_cache.clear_cache)

    @mock.patch("urllib.request.urlopen")
    def test_read_within_ttl_does_not_refetch(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response(b"cached body")

        first = fetch_cache.fetch_text(_URL, max_bytes=1024)
        second = fetch_cache.fetch_text(_URL, max_bytes=1024)

        self.assertEqual(first, "cached body")
        self.assertEqual(second, "cached body")
        mock_urlopen.assert_called_once()

    @mock.patch("urllib.request.urlopen")
    def test_clear_cache_refetches(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response(b"cached body")

        fetch_cache.fetch_text(_URL, max_bytes=1024)
        fetch_cache.clear_cache()
        fetch_cache.fetch_text(_URL, max_bytes=1024)

        self.assertEqual(mock_urlopen.call_count, 2)

    @mock.patch("ads_mcp.resources.fetch_cache.time")
    @mock.patch("urllib.request.urlopen")
    def test_expired_entry_refetches(self, mock_urlopen, mock_time):
        mock_urlopen.return_value = _mock_response(b"cached body")
        mock_time.monotonic.return_value = 1000.0

        fetch_cache.fetch_text(_URL, max_bytes=1024)

        # Just inside the TTL the cached body is served.
        mock_time.monotonic.return_value = (
            1000.0 + fetch_cache.CACHE_TTL_SECONDS - 1
        )
        fetch_cache.fetch_text(_URL, max_bytes=1024)
        mock_urlopen.assert_called_once()

        # At the TTL boundary the entry is stale and refetched.
        mock_time.monotonic.return_value = (
            1000.0 + fetch_cache.CACHE_TTL_SECONDS
        )
        fetch_cache.fetch_text(_URL, max_bytes=1024)
        self.assertEqual(mock_urlopen.call_count, 2)

    @mock.patch("urllib.request.urlopen")
    def test_oversized_response_is_rejected_and_not_cached(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response(b"x" * 11)

        with self.assertRaises(ResourceError):
            fetch_cache.fetch_text(_URL, max_bytes=10)

        # The rejected body must not be served from the cache later.
        mock_urlopen.return_value = _mock_response(b"small")
        self.assertEqual(fetch_cache.fetch_text(_URL, max_bytes=10), "small")
        self.assertEqual(mock_urlopen.call_count, 2)

    @mock.patch("urllib.request.urlopen")
    def test_bounded_read_and_timeout_are_used(self, mock_urlopen):
        response = _mock_response(b"body")
        mock_urlopen.return_value = response

        fetch_cache.fetch_text(_URL, max_bytes=1024)

        response.read.assert_called_once_with(1025)
        _, kwargs = mock_urlopen.call_args
        self.assertEqual(
            kwargs.get("timeout"), fetch_cache.FETCH_TIMEOUT_SECONDS
        )

    @mock.patch("urllib.request.urlopen")
    def test_entries_are_keyed_by_url(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response(b"body one")
        first = fetch_cache.fetch_text(_URL, max_bytes=1024)

        mock_urlopen.return_value = _mock_response(b"body two")
        other = fetch_cache.fetch_text(
            "https://example.com/other", max_bytes=1024
        )

        self.assertEqual(first, "body one")
        self.assertEqual(other, "body two")
        self.assertEqual(mock_urlopen.call_count, 2)


if __name__ == "__main__":
    unittest.main()
