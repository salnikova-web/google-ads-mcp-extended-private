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

import unittest
from unittest.mock import patch

from fastmcp import FastMCP

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
