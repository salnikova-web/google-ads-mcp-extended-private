# Copyright 2026 the google-ads-mcp-extended contributors.
# Licensed under the Apache License, Version 2.0.

"""Targeted regression tests for demand_gen.list_assets' truncation
envelope: the probe row (cap+1), the ORDER BY added for deterministic
paging, and the explicit warning that truncation is never silent.
"""

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import ads_mcp.utils as utils
from ads_mcp.tools import demand_gen


def make_asset_row(asset_id, name, asset_type="IMAGE", youtube_video_id=""):
    """A typed stand-in for one row of the asset listing.

    Field types match the proto (ints for ids), because the tool coerces
    them with int()/str().
    """
    return SimpleNamespace(
        asset=SimpleNamespace(
            id=int(asset_id),
            name=name,
            type_=SimpleNamespace(name=asset_type),
            youtube_video_asset=SimpleNamespace(
                youtube_video_id=youtube_video_id
            ),
        )
    )


class TestListAssets(unittest.TestCase):

    def setUp(self):
        utils.clear_googleads_cache()
        self.service = MagicMock(name="googleads_service")
        patcher = patch(
            "ads_mcp.utils.get_googleads_service", return_value=self.service
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_query_orders_by_asset_id_and_probes_one_past_the_cap(self):
        self.service.search.return_value = []
        demand_gen.list_assets("1234567890", limit=50)
        query = self.service.search.call_args.kwargs["query"]
        self.assertIn("ORDER BY asset.id", query)
        self.assertIn("LIMIT 51", query)

    def test_over_cap_rows_are_truncated_with_a_warning(self):
        self.service.search.return_value = [
            make_asset_row(i, f"Asset {i}") for i in range(1, 4)
        ]
        result = demand_gen.list_assets("1234567890", limit=2)
        self.assertEqual(result["returned"], 2)
        self.assertEqual(len(result["items"]), 2)
        self.assertTrue(result["truncated"])
        self.assertIn("truncated", result["warning"])

    def test_under_cap_rows_are_not_truncated_and_carry_no_warning(self):
        self.service.search.return_value = [
            make_asset_row(i, f"Asset {i}") for i in range(1, 3)
        ]
        result = demand_gen.list_assets("1234567890", limit=5)
        self.assertFalse(result["truncated"])
        self.assertEqual(result["returned"], 2)
        self.assertNotIn("warning", result)


if __name__ == "__main__":
    unittest.main()
