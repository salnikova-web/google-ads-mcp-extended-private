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

"""Test cases for the utils module."""

import subprocess
import threading
import unittest
from unittest.mock import MagicMock, patch

from google.ads.googleads.v24.enums.types.campaign_status import (
    CampaignStatusEnum,
)
from google.ads.googleads.v24.common.types.metrics import Metrics
from google.protobuf.field_mask_pb2 import FieldMask

from ads_mcp import utils


class TestUtils(unittest.TestCase):
    """Test cases for the utils module."""

    def test_format_output_value(self):
        """Tests that output values are formatted correctly."""

        self.assertEqual(
            utils.format_output_value(
                CampaignStatusEnum.CampaignStatus.ENABLED
            ),
            "ENABLED",
        )

    def test_format_output_value_primitive(self):
        """Tests that primitive values are returned as is."""
        self.assertEqual(utils.format_output_value(123), 123)
        self.assertEqual(utils.format_output_value("abc"), "abc")

    def test_format_output_value_message(self):
        """Tests that proto messages are converted to dict."""
        metrics = Metrics(clicks=10, impressions=100)
        formatted = utils.format_output_value(metrics)
        self.assertIsInstance(formatted, dict)
        self.assertEqual(formatted.get("clicks"), "10")
        self.assertEqual(formatted.get("impressions"), "100")

    def test_format_output_value_repeated_primitive(self):
        """Tests that repeated primitive values are formatted."""
        self.assertEqual(
            utils.format_output_value([1, 2, 3]),
            [1, 2, 3],
        )

    def test_format_output_value_repeated_message(self):
        """Tests that repeated proto messages are formatted."""
        metrics1 = Metrics(clicks=10)
        metrics2 = Metrics(clicks=20)
        formatted = utils.format_output_value([metrics1, metrics2])
        self.assertIsInstance(formatted, list)
        self.assertEqual(len(formatted), 2)
        self.assertEqual(formatted[0].get("clicks"), "10")
        self.assertEqual(formatted[1].get("clicks"), "20")

    def test_format_output_value_bare_protobuf(self):
        """Tests that bare protobuf messages are formatted correctly."""
        fm = FieldMask(paths=["foo", "bar"])
        formatted = utils.format_output_value(fm)
        self.assertEqual(formatted, "foo,bar")


class TestPreventStdioInheritance(unittest.TestCase):
    """Test cases for the subprocess.Popen swap in prevent_stdio_inheritance."""

    def test_prevent_stdio_inheritance(self):
        """Tests that prevent_stdio_inheritance sets stdin to DEVNULL if not specified."""
        from ads_mcp.utils import prevent_stdio_inheritance

        mock_popen = MagicMock()
        with patch("subprocess.Popen", mock_popen):
            with prevent_stdio_inheritance():
                subprocess.Popen(["mock_cmd"])

        mock_popen.assert_called_once_with(
            ["mock_cmd"], stdin=subprocess.DEVNULL
        )

    def test_prevent_stdio_inheritance_explicit_stdin(self):
        """Tests that prevent_stdio_inheritance preserves explicit stdin."""
        from ads_mcp.utils import prevent_stdio_inheritance

        mock_popen = MagicMock()
        with patch("subprocess.Popen", mock_popen):
            with prevent_stdio_inheritance():
                subprocess.Popen(["mock_cmd"], stdin=subprocess.PIPE)

        mock_popen.assert_called_once_with(["mock_cmd"], stdin=subprocess.PIPE)

    def test_restores_popen_on_exception(self):
        """The swap is undone even when the body raises."""
        original = subprocess.Popen
        with self.assertRaises(RuntimeError):
            with utils.prevent_stdio_inheritance():
                self.assertIsNot(subprocess.Popen, original)
                raise RuntimeError("boom")
        self.assertIs(subprocess.Popen, original)

    def test_serialises_concurrent_entries(self):
        """A second thread cannot enter while the first holds the context.

        Without the lock the two threads interleave save and restore: the
        second saves the *wrapper* as its "original" and restores it on exit,
        leaving `subprocess.Popen` wrapped for the life of the process.
        """
        original = subprocess.Popen
        first_inside = threading.Event()
        release_first = threading.Event()
        second_inside = threading.Event()

        def first():
            with utils.prevent_stdio_inheritance():
                first_inside.set()
                release_first.wait(5)

        def second():
            with utils.prevent_stdio_inheritance():
                second_inside.set()

        t1 = threading.Thread(target=first)
        t2 = threading.Thread(target=second)
        t1.start()
        self.assertTrue(first_inside.wait(5))
        t2.start()
        # Blocked on the lock, not merely slow to start: it stays out for as
        # long as the first thread holds the context.
        self.assertFalse(second_inside.wait(0.3))
        release_first.set()
        t1.join(5)
        t2.join(5)
        self.assertTrue(second_inside.is_set())
        self.assertIs(subprocess.Popen, original)

    def test_reentrant_on_one_thread(self):
        """Nesting on one thread restores in order instead of deadlocking."""
        original = subprocess.Popen
        with utils.prevent_stdio_inheritance():
            outer = subprocess.Popen
            with utils.prevent_stdio_inheritance():
                self.assertIsNot(subprocess.Popen, outer)
            self.assertIs(subprocess.Popen, outer)
        self.assertIs(subprocess.Popen, original)


class _FakeTransport:
    def __init__(self):
        self.closed = 0

    def close(self):
        self.closed += 1


class _FakeService:
    """Stands in for a cached Google Ads service stub."""

    def __init__(self):
        self.transport = _FakeTransport()


class TestClientCache(unittest.TestCase):
    """Test cases for the client/service cache in utils.

    The invariant under test: eviction never closes a transport (another
    thread may be mid-RPC on that channel), only `clear_googleads_cache`
    does.
    """

    def setUp(self):
        utils.clear_googleads_cache()
        self.addCleanup(utils.clear_googleads_cache)

    def test_ttl_expiry_does_not_close_transport(self):
        service = _FakeService()
        utils._cache_put(("k",), service)
        with patch.object(utils, "_CACHE_TTL_SECONDS", -1):
            self.assertIsNone(utils._cache_get(("k",)))
        self.assertNotIn(("k",), utils._cache)
        self.assertEqual(service.transport.closed, 0)

    def test_lru_eviction_does_not_close_transport(self):
        with patch.object(utils, "_CACHE_MAX_ENTRIES", 2):
            services = [_FakeService() for _ in range(3)]
            for i, service in enumerate(services):
                utils._cache_put((i,), service)
            self.assertEqual(len(utils._cache), 2)
            # The oldest key is gone from the cache...
            self.assertNotIn((0,), utils._cache)
        # ...but its channel was left for the garbage collector.
        self.assertEqual([s.transport.closed for s in services], [0, 0, 0])

    def test_replacing_a_key_does_not_close_the_old_transport(self):
        old, new = _FakeService(), _FakeService()
        utils._cache_put(("k",), old)
        utils._cache_put(("k",), new)
        self.assertIs(utils._cache_get(("k",)), new)
        self.assertEqual(old.transport.closed, 0)

    def test_replacing_a_key_refreshes_lru_position(self):
        with patch.object(utils, "_CACHE_MAX_ENTRIES", 2):
            first, second, third = (_FakeService() for _ in range(3))
            utils._cache_put(("a",), first)
            utils._cache_put(("b",), second)
            # Re-putting "a" must move it to the end, so "b" is the oldest.
            utils._cache_put(("a",), first)
            utils._cache_put(("c",), third)
            self.assertEqual(sorted(utils._cache), [("a",), ("c",)])

    def test_clear_closes_every_transport(self):
        services = [_FakeService() for _ in range(3)]
        for i, service in enumerate(services):
            utils._cache_put((i,), service)
        utils.clear_googleads_cache()
        self.assertEqual(len(utils._cache), 0)
        self.assertEqual([s.transport.closed for s in services], [1, 1, 1])

    def test_clear_survives_a_transport_that_refuses_to_close(self):
        service = _FakeService()
        service.transport.close = MagicMock(side_effect=RuntimeError("nope"))
        utils._cache_put(("k",), service)
        utils.clear_googleads_cache()  # must not propagate
        self.assertEqual(len(utils._cache), 0)

    def test_cache_get_on_a_fresh_entry_returns_it(self):
        service = _FakeService()
        utils._cache_put(("k",), service)
        self.assertIs(utils._cache_get(("k",)), service)
        self.assertIsNone(utils._cache_get(("missing",)))
