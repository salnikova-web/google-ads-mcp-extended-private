# Copyright 2026 the google-ads-mcp-extended contributors.
# Licensed under the Apache License, Version 2.0.

"""Targeted regression tests for the optimize read tools' truncation
envelope: the probe row (cap+1), the ORDER BY added for deterministic
paging, and the explicit warning that truncation is never silent — plus
change_history's extra branch for the API's hard 10000-row cap on
change_event, which degrades the probe itself.
"""

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import ads_mcp.utils as utils
from ads_mcp.tools import optimize


def make_recommendation_row(index, rec_type="CALL_EXTENSION", dismissed=False):
    """A typed stand-in for one row of the recommendations listing."""
    return SimpleNamespace(
        recommendation=SimpleNamespace(
            resource_name=f"customers/1234567890/recommendations/rec{index}",
            type_=SimpleNamespace(name=rec_type),
            campaign="customers/1234567890/campaigns/1",
            dismissed=bool(dismissed),
        )
    )


def make_change_event_row(index):
    """A typed stand-in for one row of the change_event listing.

    changed_fields.paths is left empty so _changed_values (which reaches
    into old_resource/new_resource) is never exercised here — out of
    scope for the truncation envelope these tests target.
    """
    return SimpleNamespace(
        change_event=SimpleNamespace(
            change_date_time=f"2026-08-{(index % 27) + 1:02d} 00:00:00",
            user_email="user@example.com",
            client_type=SimpleNamespace(name="GOOGLE_ADS_WEB_CLIENT"),
            change_resource_type=SimpleNamespace(name="CAMPAIGN"),
            resource_change_operation=SimpleNamespace(name="UPDATE"),
            changed_fields=SimpleNamespace(paths=[]),
        )
    )


class OptimizeReadToolTestCase(unittest.TestCase):

    def setUp(self):
        utils.clear_googleads_cache()
        self.service = MagicMock(name="googleads_service")
        patcher = patch(
            "ads_mcp.utils.get_googleads_service", return_value=self.service
        )
        patcher.start()
        self.addCleanup(patcher.stop)


class TestRecommendationsList(OptimizeReadToolTestCase):

    def test_query_orders_by_resource_name_and_probes_one_past_the_cap(self):
        self.service.search.return_value = []
        optimize.recommendations_list("1234567890", limit=50)
        query = self.service.search.call_args.kwargs["query"]
        self.assertIn("ORDER BY recommendation.type", query)
        self.assertIn("LIMIT 51", query)

    def test_over_cap_rows_are_truncated_with_a_warning(self):
        self.service.search.return_value = [
            make_recommendation_row(i) for i in range(1, 4)
        ]
        result = optimize.recommendations_list("1234567890", limit=2)
        self.assertEqual(result["returned"], 2)
        self.assertEqual(len(result["items"]), 2)
        self.assertTrue(result["truncated"])
        self.assertIn("truncated", result["warning"])

    def test_under_cap_rows_are_not_truncated_and_carry_no_warning(self):
        self.service.search.return_value = [
            make_recommendation_row(i) for i in range(1, 3)
        ]
        result = optimize.recommendations_list("1234567890", limit=5)
        self.assertFalse(result["truncated"])
        self.assertEqual(result["returned"], 2)
        self.assertNotIn("warning", result)


class TestChangeHistory(OptimizeReadToolTestCase):

    def test_query_keeps_its_order_by_and_probes_one_past_the_cap(self):
        self.service.search.return_value = []
        optimize.change_history("1234567890", limit=100)
        query = self.service.search.call_args.kwargs["query"]
        self.assertIn("ORDER BY change_event.change_date_time DESC", query)
        self.assertIn("LIMIT 101", query)

    def test_over_cap_rows_are_truncated_with_a_warning(self):
        self.service.search.return_value = [
            make_change_event_row(i) for i in range(1, 4)
        ]
        result = optimize.change_history("1234567890", limit=2)
        self.assertEqual(result["returned"], 2)
        self.assertEqual(len(result["items"]), 2)
        self.assertTrue(result["truncated"])
        self.assertIn("truncated", result["warning"])

    def test_under_cap_rows_are_not_truncated_and_carry_no_warning(self):
        self.service.search.return_value = [
            make_change_event_row(i) for i in range(1, 3)
        ]
        result = optimize.change_history("1234567890", limit=5)
        self.assertFalse(result["truncated"])
        self.assertEqual(result["returned"], 2)
        self.assertNotIn("warning", result)

    def test_api_cap_binds_before_the_requested_limit_and_names_it(self):
        # limit=10000 pushes the cap+1 probe past the API's hard ceiling;
        # the query degrades to LIMIT 10000, and a full 10000-row answer
        # cannot be told apart from "there were exactly 10000" — so the
        # tool must warn with the API-cap wording, not the generic one.
        self.service.search.return_value = [
            make_change_event_row(i) for i in range(10000)
        ]
        result = optimize.change_history("1234567890", limit=10000)
        query = self.service.search.call_args.kwargs["query"]
        self.assertIn("LIMIT 10000", query)
        self.assertEqual(result["returned"], 10000)
        self.assertTrue(result["truncated"])
        self.assertIn("10000 rows", result["warning"])
        self.assertIn("change_date_time", result["warning"])
        self.assertNotIn("more exist beyond the cap", result["warning"])


if __name__ == "__main__":
    unittest.main()
