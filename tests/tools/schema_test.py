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

"""Tests for tool schema correctness and type safety."""

import ast
import inspect
import pathlib
import re
import unittest
from unittest.mock import patch

from fastmcp import FastMCP

import ads_mcp.tools
from ads_mcp.config import ToolsConfig
from ads_mcp.coordinator import initialize_and_mount_tools, mcp

# Import server to ensure all tools are registered on the mcp object
from ads_mcp import server  # noqa: F401


class TestToolSchemas(unittest.IsolatedAsyncioTestCase):
    """Verifies that tool schemas are properly defined and prevent Zod errors."""

    async def test_optional_parameters_allow_null(self):
        """Verifies that any tool parameter with a default of None allows 'null' in its schema.

        This prevents 'Expected array, received string' or similar client-side Zod validation
        failures caused by schema type contradictions (e.g. type 'array' but default is 'null').
        """
        tools = await mcp.list_tools()
        self.assertGreater(
            len(tools), 0, "No tools are registered on the server"
        )

        for tool in tools:
            input_schema = tool.parameters
            properties = input_schema.get("properties", {})
            for param_name, param_schema in properties.items():
                # If a parameter has a default value of None (JSON null), the schema must permit null
                if (
                    "default" in param_schema
                    and param_schema["default"] is None
                ):
                    has_null_type = False

                    # Case 1: Schema uses anyOf (standard for Pydantic unions)
                    if "anyOf" in param_schema:
                        for option in param_schema["anyOf"]:
                            if option.get("type") == "null":
                                has_null_type = True
                                break

                    # Case 2: Schema uses oneOf
                    elif "oneOf" in param_schema:
                        for option in param_schema["oneOf"]:
                            if option.get("type") == "null":
                                has_null_type = True
                                break

                    # Case 3: Schema has list-based types or direct type 'null'
                    elif "type" in param_schema:
                        t = param_schema["type"]
                        if t == "null":
                            has_null_type = True
                        elif isinstance(t, list) and "null" in t:
                            has_null_type = True

                    self.assertTrue(
                        has_null_type,
                        f"Tool '{tool.name}' parameter '{param_name}' has default=None, "
                        f"but its JSON schema does not permit 'null'. Schema: {param_schema}",
                    )

    async def test_search_tool_array_parameters(self):
        """Verifies that search tool's array parameters are correctly typed with top-level 'array'."""
        tools = await mcp.list_tools()
        search_tool = next(
            (t for t in tools if t.name == "search_search"), None
        )
        self.assertIsNotNone(search_tool, "search tool not found")

        properties = search_tool.parameters.get("properties", {})

        for param in ["conditions", "orderings"]:
            schema = properties.get(param)
            self.assertIsNotNone(
                schema, f"Parameter '{param}' not found in search tool schema"
            )
            self.assertEqual(
                schema.get("type"),
                "array",
                f"Parameter '{param}' must have type 'array'",
            )
            self.assertEqual(
                schema.get("default"),
                [],
                f"Parameter '{param}' default must be an empty list",
            )


class TestEnumSchemaAdvertisement(unittest.IsolatedAsyncioTestCase):
    """Verifies Task 2.2: enum-like params advertise their accepted values.

    Uses Annotated[str, Field(json_schema_extra={"enum": [...]})] rather
    than Literal, so runtime validation stays the existing lax .upper() +
    explicit ToolError checks (a true Literal would reject lowercase input
    that works today). Mounts a fresh FastMCP (per the existing
    tools_mounting_test.py pattern) instead of relying on ads_mcp.server's
    singleton, so this test does not depend on load order.
    """

    @patch("ads_mcp.config.ToolsConfig.load")
    async def test_channel_type_and_match_type_enums_are_advertised(
        self, mock_load
    ):
        mock_load.return_value = ToolsConfig({"namespaces": {"mutate": True}})
        parent = FastMCP("Test Parent")
        initialize_and_mount_tools(parent)

        tools = {t.name: t for t in await parent.list_tools()}

        channel_type = tools["mutate_campaign_create"].parameters["properties"][
            "channel_type"
        ]
        self.assertEqual(
            channel_type["enum"],
            [
                "SEARCH",
                "DISPLAY",
                "SHOPPING",
                "VIDEO",
                "PERFORMANCE_MAX",
                "DEMAND_GEN",
            ],
        )

        match_type = tools["mutate_keywords_add"].parameters["properties"][
            "match_type"
        ]
        self.assertEqual(match_type["enum"], ["EXACT", "PHRASE", "BROAD"])


class TestDocstringBodyReachesTheDescription(unittest.IsolatedAsyncioTestCase):
    """Everything before Args: must survive into the tool description.

    FastMCP's Google-style docstring parser treats "LABEL:" followed by an
    INDENTED block as a section and silently drops it, so a docstring
    written as::

        WHEN TO USE: something
          continued, indented

    renders as nothing but its summary line — the safety text is gone from
    tools/list with no error anywhere. Continuation lines therefore stay at
    the docstring's own indent, and this test fails if anyone re-indents
    them.
    """

    _STOP = ("Args:", "Returns:", "Raises:", "Yields:")

    @classmethod
    def _source_bodies(cls):
        """{first docstring line: body} for every tool function in the
        package, read straight from source."""
        out = {}
        # ads_mcp.tools is a namespace package (no __init__.py), so
        # __file__ is None and __path__ is what locates it.
        tools_dir = pathlib.Path(list(ads_mcp.tools.__path__)[0])
        for path in sorted(tools_dir.glob("*.py")):
            tree = ast.parse(path.read_text())
            for node in tree.body:
                if not isinstance(
                    node, (ast.FunctionDef, ast.AsyncFunctionDef)
                ):
                    continue
                raw = ast.get_docstring(node, clean=False)
                if not raw:
                    continue
                lines = []
                for line in inspect.cleandoc(raw).splitlines():
                    if line.strip() in cls._STOP:
                        break
                    lines.append(line)
                body = "\n".join(lines).strip()
                if body:
                    out.setdefault(body.splitlines()[0].strip(), body)
        return out

    @patch("ads_mcp.config.ToolsConfig.load")
    async def test_no_section_is_swallowed_by_the_parser(self, mock_load):
        mock_load.return_value = ToolsConfig({})
        parent = FastMCP("Test Parent")
        initialize_and_mount_tools(parent)

        bodies = self._source_bodies()
        tools = await parent.list_tools()
        self.assertGreater(len(tools), 0, "no tools mounted")

        truncated = []
        blank = []
        unmatched = []
        checked = 0
        for tool in tools:
            description = (tool.description or "").strip()
            if not description:
                blank.append(tool.name)
                continue
            body = bodies.get(description.splitlines()[0].strip())
            if body is None:
                unmatched.append(tool.name)
                continue
            checked += 1
            # The parser reflows whitespace, so compare lengths rather than
            # demanding an exact match.
            if len(description) < len(body) * 0.85:
                truncated.append((tool.name, len(body), len(description)))

        # A tool this scan cannot line up with its source docstring is not
        # "fine", it is UNCHECKED: it would sail through even with its whole
        # body swallowed. Both escape hatches are therefore failures, not
        # skips, so the pin cannot go dark by half the surface quietly
        # dropping out of the comparison.
        self.assertEqual(
            blank, [], f"tools reached tools/list with no description: {blank}"
        )
        self.assertEqual(
            unmatched,
            [],
            "these tools' descriptions no longer start with their docstring's "
            "first line, so this test silently stopped covering them — either "
            "the rendering changed or the description is generated rather "
            f"than taken from the docstring: {unmatched}",
        )
        self.assertEqual(
            checked,
            len(tools),
            "every mounted tool must be compared against its source",
        )
        self.assertEqual(
            truncated,
            [],
            "docstring text never reached tools/list — un-indent the "
            f"continuation lines (tool, source, rendered): {truncated}",
        )


class TestCrossToolReferencesArePrefixed(unittest.IsolatedAsyncioTestCase):
    """A tool named inside another tool's text must be callable as written.

    Descriptions used to name tools by their bare function name
    (``geo_lookup``, ``list_campaigns``), which is NOT what tools/list
    exposes — the namespace prefix is added at mount time, so an agent
    following the text calls a tool that does not exist. One name
    (``ad_group_ad_update_asset_optimization``) never existed at all.

    Deliberately cheap: it pins the bare names that were actually wrong
    rather than every possible one, so prose like "search on resource
    campaign" does not have to be rewritten to satisfy it.
    """

    # bare name -> the prefixed name it must be written as.
    KNOWN_BAD = {
        "geo_lookup": "targeting_geo_lookup",
        "list_campaigns": "mutate_list_campaigns",
        "list_criteria": "targeting_list_criteria",
        "remove_criterion": "targeting_remove_criterion",
        "set_locations": "targeting_set_locations",
        "list_campaign_assets": "extensions_list_campaign_assets",
        "recommendations_list": "optimize_recommendations_list",
        "experiments_list": "experiments_experiments_list",
        "campaign_set_custom_conversion_goal": (
            "mutate_campaign_set_custom_conversion_goal"
        ),
        "audience_attach": "demandgen_audience_attach",
        "asset_upload_image": "demandgen_asset_upload_image",
        "asset_create_youtube_video": "demandgen_asset_create_youtube_video",
        "ad_group_update_channels": "demandgen_ad_group_update_channels",
    }

    # Never existed under any prefix: the real tool is
    # demandgen_ad_update_asset_optimization.
    NONEXISTENT = "ad_group_ad_update_asset_optimization"

    _TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

    @staticmethod
    def _texts(tool):
        """Every string of the tool that an agent reads: description +
        per-parameter descriptions."""
        yield tool.description or ""
        properties = (tool.parameters or {}).get("properties", {})
        for schema in properties.values():
            if isinstance(schema, dict) and schema.get("description"):
                yield schema["description"]

    @patch("ads_mcp.config.ToolsConfig.load")
    async def test_no_bare_or_nonexistent_tool_names(self, mock_load):
        # No "namespaces" key at all means every namespace is mounted, so
        # the scan covers the whole exposed surface.
        mock_load.return_value = ToolsConfig({})
        parent = FastMCP("Test Parent")
        initialize_and_mount_tools(parent)

        tools = await parent.list_tools()
        self.assertGreater(len(tools), 0, "no tools mounted")
        real_names = {tool.name for tool in tools}
        for prefixed in self.KNOWN_BAD.values():
            self.assertIn(prefixed, real_names)
        self.assertNotIn(self.NONEXISTENT, real_names)

        offenders = []
        for tool in tools:
            for text in self._texts(tool):
                if self.NONEXISTENT in text:
                    offenders.append(
                        (tool.name, self.NONEXISTENT, "(no such tool)")
                    )
                # Whole-token match: a prefixed name is a single token, so
                # "targeting_geo_lookup" never trips the "geo_lookup" rule.
                for token in set(self._TOKEN.findall(text)):
                    if token in self.KNOWN_BAD:
                        offenders.append(
                            (tool.name, token, self.KNOWN_BAD[token])
                        )
        self.assertEqual(
            offenders,
            [],
            "tool text names tools that cannot be called as written "
            f"(tool, written, should be): {offenders}",
        )
