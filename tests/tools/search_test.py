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

"""Test cases for the search tool."""

import unittest
from unittest.mock import MagicMock, patch

from ads_mcp import utils
from ads_mcp.tools import search


class TestSearch(unittest.TestCase):
    """Test cases for the search tool."""

    @patch("ads_mcp.utils.get_googleads_service")
    @patch("ads_mcp.utils.format_output_row")
    def test_search_basic(self, mock_format_row, mock_get_service):
        """Tests that the search function constructs the correct query and processes results."""
        # Setup mock service and search stream
        mock_service = MagicMock()
        mock_get_service.return_value = mock_service

        # Mock result results
        mock_batch = MagicMock()
        mock_batch.results = [MagicMock(), MagicMock()]
        mock_batch.field_mask.paths = ["campaign.id", "campaign.name"]
        mock_service.search_stream.return_value = [mock_batch]

        mock_format_row.side_effect = [
            {"id": 1, "name": "C1"},
            {"id": 2, "name": "C2"},
        ]

        # Call search
        results = search.search(
            customer_id="1234567890",
            fields=["campaign.id", "campaign.name"],
            resource="campaign",
            conditions=["campaign.status = 'ENABLED'"],
            orderings=["campaign.name ASC"],
            limit=10,
        )

        # Verify query: LIMIT carries a +1 has_more probe (10 -> 11)
        expected_query = (
            "SELECT campaign.id,campaign.name FROM campaign "
            "WHERE campaign.status = 'ENABLED' "
            "ORDER BY campaign.name ASC "
            "LIMIT 11 "
            "PARAMETERS omit_unselected_resource_names=true"
        )
        mock_service.search_stream.assert_called_once_with(
            customer_id="1234567890", query=expected_query
        )

        # Verify pagination envelope
        self.assertEqual(results["count"], 2)
        self.assertEqual(results["offset"], 0)
        self.assertEqual(results["total"], 2)
        self.assertFalse(results["has_more"])
        self.assertIsNone(results["next_offset"])
        self.assertEqual(len(results["results"]), 2)
        self.assertEqual(results["results"][0]["id"], 1)
        self.assertEqual(results["results"][1]["name"], "C2")

    @patch("ads_mcp.utils.get_googleads_service")
    @patch("ads_mcp.utils.format_output_row")
    def test_search_pagination_has_more(
        self, mock_format_row, mock_get_service
    ):
        """A full probe page means has_more=True and a next_offset."""
        mock_service = MagicMock()
        mock_get_service.return_value = mock_service
        mock_batch = MagicMock()
        mock_batch.results = [MagicMock()] * 3
        mock_batch.field_mask.paths = ["campaign.id"]
        mock_service.search_stream.return_value = [mock_batch]
        mock_format_row.side_effect = [{"campaign.id": i} for i in range(3)]

        results = search.search(
            customer_id="1234567890",
            fields=["campaign.id"],
            resource="campaign",
            limit=2,
        )

        query = mock_service.search_stream.call_args.kwargs["query"]
        self.assertIn("LIMIT 3", query)
        self.assertEqual(results["count"], 2)
        self.assertTrue(results["has_more"])
        self.assertEqual(results["next_offset"], 2)
        self.assertIsNone(results["total"])
        self.assertEqual(
            results["results"], [{"campaign.id": 0}, {"campaign.id": 1}]
        )

    @patch("ads_mcp.utils.get_googleads_service")
    @patch("ads_mcp.utils.format_output_row")
    def test_search_pagination_offset_slices(
        self, mock_format_row, mock_get_service
    ):
        """offset skips rows; the probe LIMIT covers offset+limit+1."""
        mock_service = MagicMock()
        mock_get_service.return_value = mock_service
        mock_batch = MagicMock()
        mock_batch.results = [MagicMock()] * 5
        mock_batch.field_mask.paths = ["campaign.id"]
        mock_service.search_stream.return_value = [mock_batch]
        mock_format_row.side_effect = [{"campaign.id": i} for i in range(5)]

        results = search.search(
            customer_id="1234567890",
            fields=["campaign.id"],
            resource="campaign",
            limit=2,
            offset=2,
        )

        query = mock_service.search_stream.call_args.kwargs["query"]
        self.assertIn("LIMIT 5", query)
        self.assertEqual(results["offset"], 2)
        self.assertEqual(results["count"], 2)
        self.assertTrue(results["has_more"])
        self.assertEqual(results["next_offset"], 4)
        self.assertEqual(
            results["results"], [{"campaign.id": 2}, {"campaign.id": 3}]
        )

    @patch("ads_mcp.utils.get_googleads_service")
    @patch("ads_mcp.utils.format_output_row")
    def test_search_omitted_limit_applies_the_default(
        self, mock_format_row, mock_get_service
    ):
        """An omitted limit is a 1000-row page, not an unbounded export."""
        mock_service = MagicMock()
        mock_get_service.return_value = mock_service
        mock_batch = MagicMock()
        mock_batch.results = [MagicMock()] * 2
        mock_batch.field_mask.paths = ["campaign.id"]
        mock_service.search_stream.return_value = [mock_batch]
        mock_format_row.side_effect = [{"campaign.id": i} for i in range(2)]

        results = search.search(
            customer_id="1234567890",
            fields=["campaign.id"],
            resource="campaign",
        )

        query = mock_service.search_stream.call_args.kwargs["query"]
        # 1000 rows plus the has_more probe row.
        self.assertIn("LIMIT 1001", query)
        self.assertEqual(results["count"], 2)
        self.assertEqual(results["total"], 2)
        self.assertFalse(results["has_more"])
        self.assertIsNone(results["next_offset"])
        # Nothing was cut, so nothing is warned about.
        self.assertNotIn("warning", results)

    @patch("ads_mcp.utils.get_googleads_service")
    @patch("ads_mcp.utils.format_output_row")
    def test_search_explicit_null_limit_returns_all_with_total(
        self, mock_format_row, mock_get_service
    ):
        """Explicit limit=null stays the unbounded full-export hatch."""
        mock_service = MagicMock()
        mock_get_service.return_value = mock_service
        mock_batch = MagicMock()
        mock_batch.results = [MagicMock()] * 2
        mock_batch.field_mask.paths = ["campaign.id"]
        mock_service.search_stream.return_value = [mock_batch]
        mock_format_row.side_effect = [{"campaign.id": i} for i in range(2)]

        results = search.search(
            customer_id="1234567890",
            fields=["campaign.id"],
            resource="campaign",
            limit=None,
        )

        query = mock_service.search_stream.call_args.kwargs["query"]
        self.assertNotIn("LIMIT", query)
        self.assertEqual(results["count"], 2)
        self.assertEqual(results["total"], 2)
        self.assertFalse(results["has_more"])
        self.assertIsNone(results["next_offset"])
        self.assertNotIn("warning", results)

    @patch("ads_mcp.utils.get_googleads_service")
    @patch("ads_mcp.utils.format_output_row")
    def test_search_default_limit_truncation_warns_and_says_default(
        self, mock_format_row, mock_get_service
    ):
        """A default-limit cut names the default, so the agent can undo it."""
        mock_service = MagicMock()
        mock_get_service.return_value = mock_service
        mock_batch = MagicMock()
        mock_batch.results = [MagicMock()] * 1001
        mock_batch.field_mask.paths = ["campaign.id"]
        mock_service.search_stream.return_value = [mock_batch]
        mock_format_row.side_effect = [{"campaign.id": i} for i in range(1001)]

        results = search.search(
            customer_id="1234567890",
            fields=["campaign.id"],
            resource="campaign",
        )

        self.assertTrue(results["has_more"])
        self.assertEqual(results["next_offset"], 1000)
        self.assertIsNone(results["total"])
        warning = results["warning"]
        self.assertIn("Result INCOMPLETE: first 1000 rows shown", warning)
        self.assertIn("default limit — no limit was passed", warning)
        self.assertIn("limit=null", warning)

    @patch("ads_mcp.utils.get_googleads_service")
    @patch("ads_mcp.utils.format_output_row")
    def test_search_explicit_limit_truncation_warns_without_default(
        self, mock_format_row, mock_get_service
    ):
        """An explicit limit is still warned about, minus the default note."""
        mock_service = MagicMock()
        mock_get_service.return_value = mock_service
        mock_batch = MagicMock()
        mock_batch.results = [MagicMock()] * 3
        mock_batch.field_mask.paths = ["campaign.id"]
        mock_service.search_stream.return_value = [mock_batch]
        mock_format_row.side_effect = [{"campaign.id": i} for i in range(3)]

        results = search.search(
            customer_id="1234567890",
            fields=["campaign.id"],
            resource="campaign",
            limit=2,
        )

        warning = results["warning"]
        self.assertIn("Result INCOMPLETE: first 2 rows shown.", warning)
        self.assertNotIn("default limit", warning)

    @patch("ads_mcp.utils.get_googleads_service")
    @patch("ads_mcp.utils.format_output_row")
    def test_search_offset_past_end_reports_the_real_total(
        self, mock_format_row, mock_get_service
    ):
        """An offset past the end must not fabricate a total out of itself."""
        mock_service = MagicMock()
        mock_get_service.return_value = mock_service
        mock_batch = MagicMock()
        mock_batch.results = [MagicMock()] * 5
        mock_batch.field_mask.paths = ["campaign.id"]
        mock_service.search_stream.return_value = [mock_batch]
        mock_format_row.side_effect = [{"campaign.id": i} for i in range(5)]

        results = search.search(
            customer_id="1234567890",
            fields=["campaign.id"],
            resource="campaign",
            limit=2,
            offset=500,
        )

        self.assertEqual(results["count"], 0)
        self.assertFalse(results["has_more"])
        self.assertIsNone(results["next_offset"])
        # Used to report total=500, the offset itself.
        self.assertEqual(results["total"], 5)
        self.assertEqual(results["results"], [])
        self.assertNotIn("warning", results)

    @patch("ads_mcp.utils.get_googleads_service")
    @patch("ads_mcp.utils.format_output_row")
    def test_search_change_event_probe_capped(
        self, mock_format_row, mock_get_service
    ):
        """change_event refuses LIMIT > 10000 — the probe must be capped."""
        mock_service = MagicMock()
        mock_get_service.return_value = mock_service
        mock_batch = MagicMock()
        mock_batch.results = []
        mock_batch.field_mask.paths = ["change_event.resource_name"]
        mock_service.search_stream.return_value = [mock_batch]

        results = search.search(
            customer_id="1234567890",
            fields=["change_event.resource_name"],
            resource="change_event",
            limit=10000,
        )

        query = mock_service.search_stream.call_args.kwargs["query"]
        self.assertIn("LIMIT 10000", query)
        self.assertNotIn("LIMIT 10001", query)
        # The clamp bound, but no rows came back: the cap was not reached.
        self.assertNotIn("api_row_cap_hit", results)

    @patch("ads_mcp.utils.get_googleads_service")
    @patch("ads_mcp.utils.format_output_row")
    def test_search_change_event_row_cap_hit_is_flagged(
        self, mock_format_row, mock_get_service
    ):
        """A clamped change_event probe that fills up says so out loud."""
        mock_service = MagicMock()
        mock_get_service.return_value = mock_service
        mock_batch = MagicMock()
        mock_batch.results = [MagicMock()] * 10000
        mock_batch.field_mask.paths = ["change_event.resource_name"]
        mock_service.search_stream.return_value = [mock_batch]
        mock_format_row.side_effect = [
            {"change_event.resource_name": i} for i in range(10000)
        ]

        results = search.search(
            customer_id="1234567890",
            fields=["change_event.resource_name"],
            resource="change_event",
            limit=10000,
        )

        self.assertTrue(results["api_row_cap_hit"])
        self.assertIn("at most 10000 rows per query", results["note"])
        self.assertIn("change_date_time", results["note"])
        # has_more semantics are deliberately unchanged: the probe row
        # never fit, so the page simply looks complete.
        self.assertFalse(results["has_more"])
        self.assertIsNone(results["next_offset"])

    @patch("ads_mcp.utils.get_googleads_service")
    @patch("ads_mcp.utils.format_output_row")
    def test_search_unclamped_query_never_claims_the_row_cap(
        self, mock_format_row, mock_get_service
    ):
        """10000 rows off a non-clamped query is not a cap hit."""
        mock_service = MagicMock()
        mock_get_service.return_value = mock_service
        mock_batch = MagicMock()
        mock_batch.results = [MagicMock()] * 10000
        mock_batch.field_mask.paths = ["campaign.id"]
        mock_service.search_stream.return_value = [mock_batch]
        mock_format_row.side_effect = [{"campaign.id": i} for i in range(10000)]

        results = search.search(
            customer_id="1234567890",
            fields=["campaign.id"],
            resource="campaign",
            limit=None,
        )

        self.assertNotIn("api_row_cap_hit", results)
        self.assertNotIn("note", results)

    @patch("ads_mcp.utils.get_googleads_service")
    @patch("ads_mcp.utils.format_output_row")
    def test_search_markdown_format(self, mock_format_row, mock_get_service):
        """markdown mode replaces results with a rendered table."""
        mock_service = MagicMock()
        mock_get_service.return_value = mock_service
        mock_batch = MagicMock()
        mock_batch.results = [MagicMock()] * 2
        mock_batch.field_mask.paths = ["campaign.id", "campaign.name"]
        mock_service.search_stream.return_value = [mock_batch]
        mock_format_row.side_effect = [
            {"campaign.id": 1, "campaign.name": "C1"},
            {"campaign.id": 2, "campaign.name": "C2"},
        ]

        results = search.search(
            customer_id="1234567890",
            fields=["campaign.id", "campaign.name"],
            resource="campaign",
            response_format="markdown",
        )

        self.assertNotIn("results", results)
        table = results["results_markdown"]
        self.assertIn("| campaign.id | campaign.name |", table)
        self.assertIn("| 1 | C1 |", table)
        self.assertIn("| 2 | C2 |", table)

    def test_search_rejects_bad_response_format(self):
        from fastmcp.exceptions import ToolError

        with self.assertRaises(ToolError):
            search.search(
                customer_id="1234567890",
                fields=["campaign.id"],
                resource="campaign",
                response_format="xml",
            )

    def test_search_rejects_negative_offset(self):
        from fastmcp.exceptions import ToolError

        with self.assertRaises(ToolError):
            search.search(
                customer_id="1234567890",
                fields=["campaign.id"],
                resource="campaign",
                offset=-1,
            )

    def test_search_tool_description(self):
        """Tests that the tool description is generated correctly."""
        description = search._search_tool_description()
        self.assertIn("Language Grammar", description)
        # The curated shortlist and the pointer replace the inlined list.
        self.assertIn("metadata_get_resource_metadata", description)
        self.assertIn("search_term_view", description)
        self.assertIn("markdown for flat human-readable tables", description)

    def test_search_tool_description_does_not_inline_the_resource_file(self):
        """The ~3.9KB resource dump must stay out of every tools/list.

        It is also why the description no longer opens any file: nothing
        to read means nothing to fail to read.
        """
        resources = utils.get_gaql_resources_filepath().read_text()
        named_on_purpose = {name for name, _ in search._RESOURCE_SHORTLIST}
        # Named by the conversions hint, not by a dump of the whole list.
        named_on_purpose.add(
            "offline_conversion_upload_conversion_action_summary"
        )
        # Long names only: short ones like "ad" or "asset" collide with
        # ordinary prose, so a hit on those would not mean a leak.
        names = [
            line.strip()
            for line in resources.splitlines()
            if len(line.strip()) >= 18 and line.strip() not in named_on_purpose
        ]
        self.assertGreater(len(names), 50, "resource file looks unexpected")

        with patch("builtins.open", side_effect=AssertionError("no file")):
            description = search._search_tool_description()

        leaked = [name for name in names if name in description]
        self.assertEqual(leaked, [], f"resource list leaked back in: {leaked}")
        # Was 6249 bytes with the list inlined.
        self.assertLess(len(description.encode()), 5000)

    @patch("ads_mcp.utils.get_googleads_service")
    def test_search_google_ads_exception(self, mock_get_service):
        """Tests that search handles GoogleAdsException and returns an error dict."""
        from google.ads.googleads.errors import GoogleAdsException

        mock_service = MagicMock()
        mock_get_service.return_value = mock_service

        # Mock failure object
        mock_error = MagicMock()
        mock_error.message = "Invalid field name"
        mock_error.error_code = "query_error: UNRECOGNIZED_FIELD"
        mock_error.location.field_path_elements = []
        mock_failure = MagicMock()
        mock_failure.errors = [mock_error]

        # Instantiate real exception with dummy args
        mock_ex = GoogleAdsException(
            MagicMock(), MagicMock(), MagicMock(), MagicMock()
        )
        mock_ex.failure = mock_failure
        mock_ex.request_id = "req-123"

        mock_service.search_stream.side_effect = mock_ex

        # Call search and verify it raises ToolError
        from fastmcp.exceptions import ToolError

        with self.assertRaises(ToolError) as context:
            search.search(
                customer_id="1234567890",
                fields=["invalid_field"],
                resource="campaign",
            )

        # Verify error message: shared handler adds code + actionable hint
        self.assertIn(
            "Google Ads API Error: Invalid field name", str(context.exception)
        )
        self.assertIn("Request ID: req-123", str(context.exception))
        self.assertIn("get_resource_metadata", str(context.exception))

    @patch("ads_mcp.utils.get_googleads_service")
    def test_search_names_the_alternative_for_a_known_bad_field(
        self, mock_get_service
    ):
        """A field the account keeps asking for by name gets named advice.

        The generic "verify the field names" hint never said what to reach
        for instead, so the same query came back a minute later with the
        same field. The message text is the one the API really returns.
        """
        from google.ads.googleads.errors import GoogleAdsException
        from fastmcp.exceptions import ToolError

        mock_service = MagicMock()
        mock_get_service.return_value = mock_service

        mock_error = MagicMock()
        mock_error.message = (
            "Unrecognized field in the query: 'campaign.start_date'."
        )
        mock_error.error_code = "query_error: UNRECOGNIZED_FIELD"
        mock_error.location.field_path_elements = []
        mock_failure = MagicMock()
        mock_failure.errors = [mock_error]

        mock_ex = GoogleAdsException(
            MagicMock(), MagicMock(), MagicMock(), MagicMock()
        )
        mock_ex.failure = mock_failure
        mock_ex.request_id = "req-456"
        mock_service.search_stream.side_effect = mock_ex

        with self.assertRaises(ToolError) as context:
            search.search(
                customer_id="1234567890",
                fields=["campaign.start_date"],
                resource="campaign",
            )

        message = str(context.exception)
        self.assertIn("segments.date", message)
        self.assertIn("not selectable", message)
        # The generic field hint still fires alongside the specific one.
        self.assertIn("verify field names", message)
