# Copyright 2026 the google-ads-mcp-extended contributors.
# Licensed under the Apache License, Version 2.0.

"""Safety invariants of the write layer, checked by reflection.

Every write tool (annotations with readOnlyHint=False) must be dry-run by
default, and a dry-run must send validate_only requests only. The checks
run against the underlying functions with the Google Ads client mocked at
the public ``ads_mcp.utils`` seams; the private ``_get_googleads_client``
is never patched, so no MagicMock can leak into the memoized cache.
"""

import asyncio
import importlib
import inspect
import pkgutil
import unittest
from unittest.mock import MagicMock, patch

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

import ads_mcp.tools as tools_pkg
import ads_mcp.utils as utils
from ads_mcp.tools import demand_gen, mutate, pmax, tracking
from ads_mcp.tools.mutate import _preview_or_done


def _collect_tools():
    """Returns [(module_name, FunctionTool)] over every tool sub-server."""
    out = []
    seen = set()
    for _, module_name, _ in pkgutil.iter_modules(tools_pkg.__path__):
        module = importlib.import_module(f"ads_mcp.tools.{module_name}")
        for attr_name in dir(module):
            attr = getattr(module, attr_name)
            if not isinstance(attr, FastMCP) or id(attr) in seen:
                continue
            seen.add(id(attr))
            for tool in asyncio.run(attr.list_tools()):
                out.append((module_name, tool))
    return out


def _write_tools():
    return [
        (module_name, tool)
        for module_name, tool in _collect_tools()
        if tool.annotations is not None
        and tool.annotations.readOnlyHint is False
    ]


def _sent_requests(mock_service):
    """Returns every request object passed to any method of the service."""
    return [
        call.kwargs["request"]
        for call in mock_service.mock_calls
        if "request" in call.kwargs
    ]


class TestWriteToolInvariants(unittest.TestCase):
    """Reflection-based invariants over all 80+ write tools."""

    def test_every_tool_declares_annotations(self):
        # Write-tool detection keys off readOnlyHint; a tool without
        # annotations would silently escape every check below.
        for module_name, tool in _collect_tools():
            with self.subTest(module=module_name, tool=tool.name):
                self.assertIsNotNone(tool.annotations)

    def test_every_write_tool_is_dry_run_by_default(self):
        write_tools = _write_tools()
        # A drop here means write tools lost their annotations (or the
        # reflection broke) and the invariant no longer covers them.
        self.assertGreaterEqual(len(write_tools), 80)
        for module_name, tool in write_tools:
            with self.subTest(module=module_name, tool=tool.name):
                params = inspect.signature(tool.fn).parameters
                confirm = params.get("confirm")
                self.assertIsNotNone(
                    confirm,
                    f"{module_name}.{tool.name} has no confirm parameter",
                )
                self.assertIs(
                    confirm.default,
                    False,
                    f"{module_name}.{tool.name} must default to dry-run",
                )


class TestValidateOnlySamples(unittest.TestCase):
    """A representative sample of write tools sends validate_only=True on
    dry-run and validate_only=False on confirm=True."""

    # (function, args, kwargs) — enough arguments to reach the API call.
    SAMPLES = [
        (mutate.campaign_create, ("1234567890", "Camp", 10.0), {}),
        (mutate.ad_group_update, ("1234567890", "111"), {"new_name": "New"}),
        (mutate.keywords_add, ("1234567890", "111", ["kw one"]), {}),
        (
            tracking.campaign_set_tracking,
            ("1234567890", "222"),
            {"final_url_suffix": "utm_source=google"},
        ),
        (
            demand_gen.ad_group_update_channels,
            ("1234567890", "111"),
            {"channels": ["DISPLAY"]},
        ),
        (
            demand_gen.campaign_update_bidding,
            ("1234567890", "222"),
            {"target_cpa": 5.0},
        ),
        (pmax.asset_group_update, ("1234567890", "333"), {"new_name": "New"}),
    ]

    def setUp(self):
        utils.clear_googleads_cache()
        self.client = MagicMock(name="googleads_client")
        # A fresh mock per get_type call: the real client returns a new
        # proto each time, and reusing one mock would conflate the
        # operation and request objects a tool builds.
        self.client.get_type.side_effect = lambda name: MagicMock(
            name=f"type:{name}"
        )
        self.service = MagicMock(name="googleads_service")
        for target, value in (
            ("ads_mcp.utils.get_googleads_client", self.client),
            ("ads_mcp.utils.get_googleads_service", self.service),
        ):
            patcher = patch(target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_dry_run_sends_validate_only(self):
        for fn, args, kwargs in self.SAMPLES:
            with self.subTest(tool=fn.__name__):
                self.service.reset_mock()
                result = fn(*args, **kwargs)
                requests = _sent_requests(self.service)
                self.assertTrue(
                    requests, f"{fn.__name__} sent no request on dry-run"
                )
                for request in requests:
                    self.assertIs(request.validate_only, True)
                self.assertIs(result["applied"], False)
                self.assertIs(result["validated"], True)
                leaked = [
                    key
                    for key in result
                    if key.startswith(("created_resource", "updated_resource"))
                ]
                self.assertEqual(
                    leaked,
                    [],
                    f"{fn.__name__} dry-run leaked resource keys: {leaked}",
                )

    def test_confirm_sends_real_request(self):
        for fn, args, kwargs in self.SAMPLES:
            with self.subTest(tool=fn.__name__):
                self.service.reset_mock()
                result = fn(*args, confirm=True, **kwargs)
                requests = _sent_requests(self.service)
                self.assertTrue(requests)
                for request in requests:
                    self.assertIs(request.validate_only, False)
                self.assertIs(result["applied"], True)


class TestGaqlHelpers(unittest.TestCase):
    """Escaping helpers used to splice values into GAQL queries."""

    def test_gaql_str_escapes_quotes_and_backslashes(self):
        self.assertEqual(utils.gaql_str("O'Brien"), "O\\'Brien")
        self.assertEqual(utils.gaql_str("back\\slash"), "back\\\\slash")
        self.assertEqual(
            utils.gaql_str("\\' break"),
            "\\\\\\' break",
        )
        self.assertEqual(utils.gaql_str("plain value"), "plain value")

    def test_gaql_str_rejects_control_characters(self):
        for bad in ("line\nbreak", "tab\there", "nul\x00", "del\x7f"):
            with self.subTest(value=bad):
                with self.assertRaises(ToolError):
                    utils.gaql_str(bad)

    def test_gaql_id_accepts_numeric_ids(self):
        self.assertEqual(utils.gaql_id("123"), "123")
        self.assertEqual(utils.gaql_id(456), "456")
        self.assertEqual(utils.gaql_id(" 789 "), "789")

    def test_gaql_id_rejects_non_numeric(self):
        bad_ids = [
            "12a",
            "1; DROP TABLE",
            "12-3",
            "",
            "1.5",
            "-1",
            "1'or'1",
            None,
        ]
        for bad in bad_ids:
            with self.subTest(value=bad):
                with self.assertRaises(ToolError):
                    utils.gaql_id(bad)


class TestPreviewOrDone(unittest.TestCase):
    """The result envelope must be authoritative about validation."""

    def test_stray_validated_key_cannot_override_flag(self):
        result = _preview_or_done(
            False,
            "action",
            {"validated": True, "other": 1},
            validated=False,
        )
        self.assertIs(result["validated"], False)
        self.assertIn("Nothing was sent to Google Ads", result["note"])
        self.assertEqual(result["other"], 1)

    def test_stray_key_cannot_downgrade_real_validation(self):
        result = _preview_or_done(False, "action", {"validated": False})
        self.assertIs(result["validated"], True)
        self.assertIn("validated by Google Ads", result["note"])


if __name__ == "__main__":
    unittest.main()
