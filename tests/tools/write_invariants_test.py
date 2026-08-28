# Copyright 2026 the google-ads-mcp-extended contributors.
# Licensed under the Apache License, Version 2.0.

"""Safety invariants of the write layer, checked by reflection.

Every write tool (annotations with readOnlyHint=False) must be dry-run by
default, and a dry-run must send validate_only requests only. The checks
run against the underlying functions with the Google Ads client mocked at
the public ``ads_mcp.utils`` seams; the private ``_get_googleads_client``
is never patched, so no MagicMock can leak into the memoized cache.
"""

import ast
import asyncio
import importlib
import inspect
import pathlib
import pkgutil
import unittest
from unittest.mock import MagicMock, patch

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

import ads_mcp.tools as tools_pkg
import ads_mcp.utils as utils
from ads_mcp.tools import _write_common
from ads_mcp.tools import audiences, demand_gen, display, extensions
from ads_mcp.tools import mutate, negatives, optimize, pmax
from ads_mcp.tools import shopping, targeting, tracking, video

# Imported through mutate on purpose: the re-export is what keeps every
# `from ads_mcp.tools.mutate import _preview_or_done` in the wild working.
from ads_mcp.tools.mutate import _preview_or_done

# Imported as a module, not `from ... import TestNoApiDryRuns`: a TestCase
# bound into this namespace would be collected and run a second time.
from tests.tools import mutate_test


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


def _mounted_tool_name(fn) -> str:
    """The namespace-prefixed name ``fn`` is actually exposed under.

    Mirrors coordinator.py's mount step: the prefix is the .name of the
    module's own FastMCP sub-server (e.g. ads_mcp.tools.demand_gen mounts
    as "demandgen", not its file name), joined to the bare function name —
    every tool here is registered under its own name (no decorator ever
    passes ``name=``), so no separate lookup against tools/list is needed.
    """
    module = importlib.import_module(fn.__module__)
    for attr_name in dir(module):
        attr = getattr(module, attr_name)
        if isinstance(attr, FastMCP):
            return f"{attr.name}_{fn.__name__}"
    raise AssertionError(f"no FastMCP sub-server found in {fn.__module__}")


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

    def test_confirm_and_read_only_hint_agree(self):
        # Biconditional: a `confirm` parameter and readOnlyHint=False are
        # two spellings of "this tool writes". If they ever disagree, one
        # of two silent failures has happened — a write tool annotated
        # read-only escapes every check in this file, or a read-only tool
        # grew a confirm parameter that gates nothing.
        for module_name, tool in _collect_tools():
            with self.subTest(module=module_name, tool=tool.name):
                self.assertIsNotNone(tool.annotations)
                has_confirm = "confirm" in inspect.signature(tool.fn).parameters
                is_write = tool.annotations.readOnlyHint is False
                self.assertEqual(
                    has_confirm,
                    is_write,
                    f"{module_name}.{tool.name}: confirm parameter "
                    f"present={has_confirm} but readOnlyHint is "
                    f"{tool.annotations.readOnlyHint!r}; every write tool "
                    "needs both, every read-only tool neither",
                )


class TestValidateOnlySamples(unittest.TestCase):
    """A representative sample of write tools sends validate_only=True on
    dry-run and validate_only=False on confirm=True."""

    # (function, args, kwargs) — enough arguments to reach the API call.
    # Every write module must appear here (or, when the API offers no
    # validate_only, in mutate_test.TestNoApiDryRuns.CALLS); that coverage
    # is enforced by TestBehavioralSampleCoverage below. Tools are chosen
    # for a straight path to the mutate call; the one exception is
    # campaign_budget_update_batch, which resolves its campaigns first and
    # is fed the fake lookup row set up in setUp.
    SAMPLES = [
        (mutate.campaign_create, ("1234567890", "Camp", 10.0), {}),
        (mutate.ad_group_update, ("1234567890", "111"), {"new_name": "New"}),
        (mutate.keywords_add, ("1234567890", "111", ["kw one"]), {}),
        (
            mutate.campaign_update_status_batch,
            ("1234567890", ["111", "222"], "PAUSED"),
            {},
        ),
        (
            mutate.campaign_budget_update_batch,
            ("1234567890", [{"campaign_id": "111", "new_daily_budget": 15.0}]),
            {},
        ),
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
        # One of the two demand_gen tools whose action string was wrong
        # (bare name, no namespace prefix) until it was fixed; sampled so
        # the action-string check below actually covers it. Its sibling
        # asset_upload_image takes the same path but has to fetch and
        # read image bytes first, which is a mock this file does not
        # otherwise need.
        (
            demand_gen.asset_create_youtube_video,
            ("1234567890", "Promo video", "dQw4w9WgXcQ"),
            {},
        ),
        (pmax.asset_group_update, ("1234567890", "333"), {"new_name": "New"}),
        (
            audiences.create,
            ("1234567890", "Persona"),
            {"genders": ["FEMALE"]},
        ),
        (display.ad_group_create, ("1234567890", "222", "Display AG"), {}),
        (
            extensions.add_callouts,
            ("1234567890", "222", ["Free trial"]),
            {},
        ),
        (
            negatives.add_campaign_keywords,
            ("1234567890", "222", ["neg kw"]),
            {},
        ),
        # optimize_recommendation_apply/dismiss have no validate_only in
        # the API (their dry-run sends nothing at all and is covered by
        # mutate_test.TestNoApiDryRuns); label_create is the module's
        # validate_only-capable representative.
        (optimize.label_create, ("1234567890", "Automation"), {}),
        (shopping.ad_group_create, ("1234567890", "222", "Shopping AG"), {}),
        (
            targeting.set_content_exclusions,
            ("1234567890", "222", ["TRAGEDY"]),
            {},
        ),
        (video.ad_group_create, ("1234567890", "222", "Video AG"), {}),
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
        # campaign_budget_update_batch resolves every campaign before it
        # builds an operation, and refuses the whole batch when one is
        # missing. Its lookup gets a typed row (int ids and micros, bool
        # shared flag) so the tool's int()/str() coercions behave as they
        # do against real proto rows.
        self.service.search.return_value = [
            mutate_test.make_budget_row(
                111,
                "Camp A",
                "customers/1234567890/campaignBudgets/1",
                10_000_000,
            )
        ]
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

    def test_dry_run_action_matches_mounted_tool_name(self):
        """The envelope's action must name the tool a caller can actually
        invoke — the namespace-prefixed name tools/list exposes — not the
        bare function name the module defines it under.

        Reuses the SAMPLES plumbing above rather than adding new mocking:
        every dry-run result already carries an "action" key straight from
        _preview_or_done, so no source scan is needed to check it.

        The no-API dry-runs are checked here too. Their endpoints have no
        validate_only field, so they cannot join SAMPLES (their dry-run
        sends nothing at all — that is what mutate_test.TestNoApiDryRuns
        asserts), but their action strings are exactly as wrong-able, and
        experiments is a whole namespace that would otherwise go
        unchecked. Nothing is sent on a dry-run, so this needs no mocks
        beyond the ones setUp already installs.
        """
        entries = list(self.SAMPLES) + list(mutate_test.TestNoApiDryRuns.CALLS)
        for fn, args, kwargs in entries:
            with self.subTest(tool=fn.__name__):
                self.service.reset_mock()
                result = fn(*args, **kwargs)
                self.assertEqual(result.get("action"), _mounted_tool_name(fn))


class TestBehavioralSampleCoverage(unittest.TestCase):
    """Reflection alone only proves a write tool *has* a confirm flag.

    This meta-assertion makes sure the flag is also exercised: every write
    module needs at least one tool that is actually called on a mocked
    client and observed to be a dry-run. Without it a whole new write
    module could land carrying nothing but the signature checks.
    """

    @staticmethod
    def _sampled_modules():
        """Module names covered by a behavioral dry-run sample.

        Two lists count: the validate_only samples in this file, and the
        no-API dry-runs in mutate_test (tools whose API has no
        validate_only field, so their dry-run must send nothing at all).
        """
        entries = list(TestValidateOnlySamples.SAMPLES) + list(
            mutate_test.TestNoApiDryRuns.CALLS
        )
        return {fn.__module__.rsplit(".", 1)[-1] for fn, _, _ in entries}

    def test_every_write_module_has_a_behavioral_sample(self):
        write_modules = sorted({module for module, _ in _write_tools()})
        # Guards against the reflection silently returning nothing, which
        # would make the loop below vacuously pass.
        self.assertGreaterEqual(len(write_modules), 13)

        sampled = self._sampled_modules()
        for module_name in write_modules:
            with self.subTest(module=module_name):
                self.assertIn(
                    module_name,
                    sampled,
                    f"write module ads_mcp.tools.{module_name} has no "
                    "behavioral validate_only sample: add one of its "
                    "tools to TestValidateOnlySamples.SAMPLES so a dry-run "
                    "is proven to send validate_only=True. Only if that "
                    "module's API has no validate_only field, add it to "
                    "tests.tools.mutate_test.TestNoApiDryRuns.CALLS "
                    "instead, where the dry-run is proven to send nothing.",
                )

    def test_samples_reference_real_write_tools(self):
        # A sample pointing at a helper or a read-only function would
        # satisfy the coverage check above without testing any write path.
        write_functions = {tool.fn for _, tool in _write_tools()}
        entries = list(TestValidateOnlySamples.SAMPLES) + list(
            mutate_test.TestNoApiDryRuns.CALLS
        )
        for fn, _, _ in entries:
            with self.subTest(tool=f"{fn.__module__}.{fn.__name__}"):
                self.assertIn(
                    fn,
                    write_functions,
                    f"{fn.__module__}.{fn.__name__} is sampled as a write "
                    "tool but is not mounted as one",
                )


class TestWriteCommonIsTheOneCopy(unittest.TestCase):
    """The shared write plumbing lives in exactly one module.

    ``_write_common.py`` was extracted from thirteen write modules that had
    each grown their own copy of these helpers (and of the write
    annotations), so a fix to one copy left the other twelve wrong. Both
    halves of that are pinned: the helpers are defined once, and the module
    holding them stays invisible to the coordinator's namespace discovery.
    """

    SHARED_NAMES = (
        "_WRITE_ANNOTATIONS",
        "_check_len",
        "_clean_customer_id",
        "_preview_or_done",
        "_raise_tool_error",
        "_text_assets",
        "_to_micros",
        "build_campaign_with_budget",
    )

    # get_resource_metadata is a read-only namespace and keeps its own
    # one-line _raise_tool_error shim rather than importing the write
    # layer's module. The shared implementation both shims delegate to is
    # utils.raise_tool_error, so this is an alias, not a second
    # implementation — a visible hole in the rule instead of a silent one.
    EXEMPT = {"get_resource_metadata.py": {"_raise_tool_error"}}

    @staticmethod
    def _top_level_bindings(path):
        """{name: lineno} for module-level defs and plain assignments."""
        out = {}
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out[node.name] = node.lineno
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        out[target.id] = node.lineno
        return out

    def test_no_tool_module_redefines_a_shared_helper(self):
        tools_dir = pathlib.Path(list(tools_pkg.__path__)[0])
        paths = sorted(tools_dir.glob("*.py"))
        # Guards against the glob silently covering nothing.
        self.assertGreaterEqual(len(paths), 16)

        duplicates = []
        for path in paths:
            if path.name == "_write_common.py":
                continue
            bindings = self._top_level_bindings(path)
            exempt = self.EXEMPT.get(path.name, frozenset())
            for name in self.SHARED_NAMES:
                if name in bindings and name not in exempt:
                    duplicates.append(f"{path.name}:{bindings[name]} {name}")
        self.assertEqual(
            duplicates,
            [],
            "these modules define their own copy of a helper that lives in "
            "ads_mcp/tools/_write_common.py; import it from there instead "
            f"(file:line name): {duplicates}",
        )

    def test_write_common_defines_every_shared_name(self):
        # The check above is only meaningful while the one copy still exists
        # under these names.
        for name in self.SHARED_NAMES:
            with self.subTest(name=name):
                self.assertTrue(hasattr(_write_common, name))

    def test_the_exemption_still_describes_something_real(self):
        # An exemption for a copy that no longer exists would quietly widen
        # the hole for the next module that adds one.
        tools_dir = pathlib.Path(list(tools_pkg.__path__)[0])
        for filename, names in self.EXEMPT.items():
            bindings = self._top_level_bindings(tools_dir / filename)
            for name in names:
                with self.subTest(file=filename, name=name):
                    self.assertIn(name, bindings)

    def test_write_common_declares_no_sub_server(self):
        # coordinator.initialize_and_mount_tools imports every module under
        # ads_mcp.tools and mounts each FastMCP instance it finds by that
        # instance's own .name. A sub-server here would mount a namespace
        # out of a library module.
        found = [
            attr_name
            for attr_name in dir(_write_common)
            if isinstance(getattr(_write_common, attr_name), FastMCP)
        ]
        self.assertEqual(found, [])


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
