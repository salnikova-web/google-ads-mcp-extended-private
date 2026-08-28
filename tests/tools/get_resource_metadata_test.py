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

"""Test cases for the metadata tools."""

import unittest
from unittest.mock import MagicMock, patch

from fastmcp.exceptions import ToolError

import ads_mcp.utils as utils
from ads_mcp.tools import get_resource_metadata


class FakeEnum:
    """Stand-in for a proto enum value, which exposes `.name`.

    A MagicMock cannot be used here: `.name` is the mock's own attribute.
    """

    def __init__(self, name):
        self.name = name

    def __str__(self):
        return self.name


class FakeField:
    """A typed stand-in for a google_ads_field row."""

    def __init__(
        self,
        name,
        selectable=False,
        filterable=False,
        sortable=False,
        category="ATTRIBUTE",
        data_type="STRING",
        is_repeated=False,
        enum_values=(),
        selectable_with=(),
    ):
        self.name = name
        self.selectable = selectable
        self.filterable = filterable
        self.sortable = sortable
        self.category = FakeEnum(category)
        self.data_type = FakeEnum(data_type)
        self.is_repeated = is_repeated
        self.enum_values = list(enum_values)
        self.selectable_with = list(selectable_with)


class MetadataToolTestCase(unittest.TestCase):
    """Patches only the public seams, as the repo's test rules require."""

    def setUp(self):
        utils.clear_googleads_cache()
        self.service = MagicMock(name="googleads_service")
        self.request = MagicMock(name="SearchGoogleAdsFieldsRequest")
        for target, value in (
            ("ads_mcp.utils.get_googleads_service", self.service),
            ("ads_mcp.utils.get_googleads_type", self.request),
        ):
            patcher = patch(target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)


class TestGetResourceMetadata(MetadataToolTestCase):
    """The compact grouped shape and its honesty flags."""

    def test_fields_are_grouped_with_flag_strings(self):
        self.service.search_google_ads_fields.side_effect = [
            [
                FakeField(
                    "campaign.id",
                    selectable=True,
                    filterable=True,
                    sortable=True,
                ),
                FakeField("campaign.name", selectable=True, sortable=True),
            ],
            [
                FakeField("metrics.clicks", selectable=True, filterable=True),
                FakeField(
                    "segments.date",
                    selectable=True,
                    filterable=True,
                    sortable=True,
                ),
            ],
        ]

        result = get_resource_metadata.get_resource_metadata("campaign")

        self.assertEqual(result["resource"], "campaign")
        self.assertEqual(
            result["attributes"],
            {"campaign.id": "SFO", "campaign.name": "SO"},
        )
        self.assertEqual(result["metrics"], {"metrics.clicks": "SF"})
        self.assertEqual(result["segments"], {"segments.date": "SFO"})
        self.assertIs(result["truncated"], False)
        self.assertNotIn("warnings", result)
        # The three repeated name lists are gone for good.
        for old_key in ("selectable", "filterable", "sortable"):
            self.assertNotIn(old_key, result)
        # The encoding travels with the response, which is meant to be cached.
        self.assertEqual(
            result["flags"], "S=selectable, F=filterable, O=sortable"
        )

    def test_filterable_but_not_selectable_survives(self):
        """The case the "selectable list + deltas" shape cannot express."""
        self.service.search_google_ads_fields.side_effect = [
            [FakeField("campaign.filter_only", filterable=True)],
            [],
        ]

        result = get_resource_metadata.get_resource_metadata("campaign")

        self.assertEqual(result["attributes"], {"campaign.filter_only": "F"})

    def test_flags_merge_when_a_name_appears_in_both_queries(self):
        self.service.search_google_ads_fields.side_effect = [
            [FakeField("campaign.id", selectable=True)],
            [FakeField("campaign.id", filterable=True, sortable=True)],
        ]

        result = get_resource_metadata.get_resource_metadata("campaign")

        self.assertEqual(result["attributes"], {"campaign.id": "SFO"})

    def test_attributes_fallback(self):
        """The category-filtered query failing falls back to a LIKE query."""
        self.service.search_google_ads_fields.side_effect = [
            Exception("API Error"),
            [FakeField("campaign.id", selectable=True, filterable=True)],
            [FakeField("metrics.clicks", selectable=True)],
        ]

        result = get_resource_metadata.get_resource_metadata("campaign")

        self.assertEqual(result["attributes"], {"campaign.id": "SF"})
        self.assertEqual(result["metrics"], {"metrics.clicks": "S"})
        self.assertIs(result["truncated"], False)

    def test_metrics_failure_warns_and_marks_the_result_incomplete(self):
        self.service.search_google_ads_fields.side_effect = [
            [FakeField("campaign.id", selectable=True)],
            Exception("Metrics Fail"),
        ]

        result = get_resource_metadata.get_resource_metadata("campaign")

        # The attributes still come back...
        self.assertEqual(result["attributes"], {"campaign.id": "S"})
        self.assertEqual(result["metrics"], {})
        # ...but the answer is not passed off as complete.
        self.assertIs(result["truncated"], True)
        self.assertEqual(len(result["warnings"]), 1)
        warning = result["warnings"][0]
        self.assertIn("metrics/segments lookup failed", warning)
        self.assertIn("Metrics Fail", warning)
        self.assertIn("INCOMPLETE", warning)

    def test_limit_truncation_is_not_silent(self):
        self.service.search_google_ads_fields.side_effect = [
            [
                FakeField("campaign.id", selectable=True),
                FakeField("campaign.name", selectable=True),
            ],
            [],
        ]

        result = get_resource_metadata.get_resource_metadata(
            "campaign", limit=1
        )

        self.assertIs(result["truncated"], True)
        self.assertIn("truncated at 1 items", result["warnings"][0])

    def test_bad_resource_name_is_rejected(self):
        with self.assertRaises(ToolError):
            get_resource_metadata.get_resource_metadata("campaign'; DROP")

    def test_both_attribute_queries_failing_raises(self):
        self.service.search_google_ads_fields.side_effect = [
            Exception("Fail 1"),
            Exception("Fail 2"),
        ]

        with self.assertRaises(RuntimeError) as cm:
            get_resource_metadata.get_resource_metadata("campaign")

        self.assertIn(
            "API call to search_google_ads_fields failed: Fail 2",
            str(cm.exception),
        )


class TestGetFieldDetails(MetadataToolTestCase):
    """Types, enum values and compatibility for named fields."""

    def test_happy_path(self):
        self.service.search_google_ads_fields.return_value = [
            FakeField(
                "campaign.status",
                category="ATTRIBUTE",
                data_type="ENUM",
                enum_values=["ENABLED", "PAUSED", "REMOVED"],
                selectable_with=["campaign_budget", "ad_group"],
            ),
            FakeField(
                "metrics.clicks",
                category="METRIC",
                data_type="INT64",
                selectable_with=["campaign"],
            ),
        ]

        result = get_resource_metadata.get_field_details(
            ["metrics.clicks", "campaign.status"]
        )

        status, clicks = result["fields"]
        self.assertEqual(status["name"], "campaign.status")
        self.assertEqual(status["category"], "ATTRIBUTE")
        self.assertEqual(status["data_type"], "ENUM")
        self.assertIs(status["is_repeated"], False)
        self.assertEqual(
            status["enum_values"], ["ENABLED", "PAUSED", "REMOVED"]
        )
        self.assertEqual(
            status["selectable_with"], ["ad_group", "campaign_budget"]
        )

        self.assertEqual(clicks["name"], "metrics.clicks")
        self.assertEqual(clicks["data_type"], "INT64")
        # enum_values is carried ONLY by ENUM fields.
        self.assertNotIn("enum_values", clicks)

        self.assertEqual(result["not_found"], [])
        self.assertIs(result["truncated"], False)
        self.assertNotIn("warnings", result)

        # Both names reach the catalog in a single quoted IN clause.
        self.assertIn(
            "WHERE name IN ('metrics.clicks', 'campaign.status')",
            self.request.query,
        )

    def test_unknown_names_come_back_as_not_found(self):
        self.service.search_google_ads_fields.return_value = [
            FakeField("campaign.status", data_type="ENUM")
        ]

        result = get_resource_metadata.get_field_details(
            ["campaign.status", "campaign.conversions_value"]
        )

        self.assertEqual(len(result["fields"]), 1)
        self.assertEqual(result["not_found"], ["campaign.conversions_value"])

    def test_selectable_with_is_capped_and_flagged(self):
        self.service.search_google_ads_fields.return_value = [
            FakeField(
                "metrics.clicks",
                data_type="INT64",
                selectable_with=[f"resource_{i:03d}" for i in range(60)],
            )
        ]

        result = get_resource_metadata.get_field_details(["metrics.clicks"])

        entry = result["fields"][0]
        self.assertEqual(len(entry["selectable_with"]), 50)
        self.assertIs(entry["selectable_with_truncated"], True)
        self.assertIs(result["truncated"], True)
        self.assertIn("metrics.clicks", result["warnings"][0])
        self.assertIn("NOT proof", result["warnings"][0])

    def test_more_than_twenty_names_is_rejected(self):
        names = [f"campaign.f{i}" for i in range(21)]

        with self.assertRaises(ToolError) as cm:
            get_resource_metadata.get_field_details(names)

        self.assertIn("20", str(cm.exception))
        self.service.search_google_ads_fields.assert_not_called()

    def test_empty_list_is_rejected(self):
        with self.assertRaises(ToolError):
            get_resource_metadata.get_field_details([])

    def test_bad_field_name_is_rejected_by_name(self):
        for bad in ("Campaign.Status", "campaign.status'", "1campaign.id", ""):
            with self.subTest(name=bad):
                with self.assertRaises(ToolError) as cm:
                    get_resource_metadata.get_field_details(
                        ["campaign.status", bad]
                    )
                self.assertIn(repr(bad), str(cm.exception))
        self.service.search_google_ads_fields.assert_not_called()

    def test_api_failure_raises(self):
        self.service.search_google_ads_fields.side_effect = Exception("boom")

        with self.assertRaises(RuntimeError) as cm:
            get_resource_metadata.get_field_details(["campaign.status"])

        self.assertIn("boom", str(cm.exception))


class TestMetadataToolsAreMountedReadOnly(unittest.IsolatedAsyncioTestCase):
    """Both tools live on the same sub-server, so both get the prefix."""

    async def test_both_tools_are_exposed_read_only(self):
        tools = {
            tool.name: tool
            for tool in await get_resource_metadata.metadata_mcp.list_tools()
        }
        self.assertEqual(
            sorted(tools), ["get_field_details", "get_resource_metadata"]
        )
        for tool in tools.values():
            self.assertIs(tool.annotations.readOnlyHint, True)


if __name__ == "__main__":
    unittest.main()
