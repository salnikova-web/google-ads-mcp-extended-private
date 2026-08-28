# Copyright 2026 the google-ads-mcp-extended contributors.
# Licensed under the Apache License, Version 2.0.

"""Targeted regression tests for the write tools.

Each test builds the request through the real tool code against a mocked
Google Ads client/service and asserts on the request the tool constructed
— above all on the update masks, where a value-derived mask used to drop
fields left at their proto default and silently no-op the update.
"""

import inspect
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastmcp.exceptions import ToolError

import ads_mcp.utils as utils
from ads_mcp.tools import demand_gen, experiments, mutate, optimize
from ads_mcp.tools import pmax, tracking
from tests.tools.middleware_test import make_google_ads_exception


class WriteToolTestCase(unittest.TestCase):
    """Patches the public client/service accessors with MagicMocks.

    ``ads_mcp.utils._get_googleads_client`` must never be patched: the
    memoized cache would keep serving the MagicMock to later tests.
    """

    def setUp(self):
        utils.clear_googleads_cache()
        self.client = MagicMock(name="googleads_client")
        self.client.get_type.side_effect = lambda name: MagicMock(
            name=f"type:{name}"
        )
        self.service = MagicMock(name="googleads_service")
        for target, value in (
            ("ads_mcp.utils.get_googleads_client", self.client),
            ("ads_mcp.utils.get_googleads_service", self.service),
        ):
            patcher = patch(target, return_value=value)
            setattr(
                self,
                "mock_" + target.rsplit(".", 1)[1],
                patcher.start(),
            )
            self.addCleanup(patcher.stop)

    def sent_operation(self, method):
        """Returns (request, operation) captured by the given service
        method mock, assuming a single operations.append call."""
        request = method.call_args.kwargs["request"]
        operation = request.operations.append.call_args.args[0]
        return request, operation

    @staticmethod
    def extended_mask_paths(operation):
        """Paths passed to operation.update_mask.paths.extend(...)."""
        return list(operation.update_mask.paths.extend.call_args.args[0])

    @staticmethod
    def appended_mask_paths(operation):
        """Paths passed to operation.update_mask.paths.append(...)."""
        return [
            call.args[0]
            for call in operation.update_mask.paths.append.call_args_list
        ]


class TestAdGroupUpdate(WriteToolTestCase):

    def test_only_passed_fields_enter_the_mask(self):
        mutate.ad_group_update("1234567890", "111", new_name="Fresh name")
        _, operation = self.sent_operation(self.service.mutate_ad_groups)
        # status and cpc_bid were not passed, so they must stay out of the
        # mask — an unconditional path list would clear them on apply.
        self.assertEqual(self.extended_mask_paths(operation), ["name"])

    def test_zero_cpc_bid_is_rejected(self):
        with self.assertRaises(ToolError):
            mutate.ad_group_update("1234567890", "111", cpc_bid=0)
        self.service.mutate_ad_groups.assert_not_called()

    def test_blank_new_name_is_rejected(self):
        for blank in ("", "   "):
            with self.subTest(new_name=blank):
                with self.assertRaises(ToolError):
                    mutate.ad_group_update("1234567890", "111", new_name=blank)
        self.service.mutate_ad_groups.assert_not_called()


class TestCampaignSetTracking(WriteToolTestCase):

    def test_empty_suffix_alone_is_an_explicit_clear(self):
        result = tracking.campaign_set_tracking(
            "1234567890", "222", final_url_suffix=""
        )
        _, operation = self.sent_operation(self.service.mutate_campaigns)
        self.assertEqual(
            self.extended_mask_paths(operation), ["final_url_suffix"]
        )
        # The dry-run must announce the wipe before anyone confirms it.
        self.assertEqual(result["will_clear"], ["final_url_suffix"])

    def test_template_alone_leaves_suffix_out_of_the_mask(self):
        result = tracking.campaign_set_tracking(
            "1234567890",
            "222",
            tracking_url_template="{lpurl}?utm_source=google",
        )
        _, operation = self.sent_operation(self.service.mutate_campaigns)
        self.assertEqual(
            self.extended_mask_paths(operation), ["tracking_url_template"]
        )
        self.assertNotIn("will_clear", result)


class TestDemandGenChannels(WriteToolTestCase):

    CONTROLS = "demand_gen_ad_group_settings.channel_controls"

    def test_disabling_a_channel_puts_false_leaves_in_the_mask(self):
        demand_gen.ad_group_update_channels(
            "1234567890", "111", channels=["DISPLAY"]
        )
        _, operation = self.sent_operation(self.service.mutate_ad_groups)
        expected = [f"{self.CONTROLS}.channel_config"] + [
            f"{self.CONTROLS}.selected_channels.{field}"
            for field in (
                "youtube_in_stream",
                "youtube_in_feed",
                "youtube_shorts",
                "discover",
                "gmail",
                "display",
            )
        ]
        # All six leaves, False ones included — without them a channel
        # could never be switched off.
        self.assertEqual(self.extended_mask_paths(operation), expected)

        settings = operation.update.demand_gen_ad_group_settings
        selected = settings.channel_controls.selected_channels
        self.assertIs(selected.display, True)
        for field in (
            "youtube_in_stream",
            "youtube_in_feed",
            "youtube_shorts",
            "discover",
            "gmail",
        ):
            self.assertIs(getattr(selected, field), False)


class TestCampaignUpdateBidding(WriteToolTestCase):

    def test_zero_or_negative_targets_are_rejected(self):
        cases = [
            (demand_gen.campaign_update_bidding, {"target_cpa": 0}),
            (demand_gen.campaign_update_bidding, {"target_roas": -1.0}),
            (pmax.campaign_update_bidding, {"target_cpa": 0}),
            (pmax.campaign_update_bidding, {"target_roas": -0.5}),
        ]
        for fn, kwargs in cases:
            with self.subTest(tool=fn.__module__, **kwargs):
                with self.assertRaises(ToolError):
                    fn("1234567890", "222", **kwargs)
        self.service.mutate_campaigns.assert_not_called()

    def test_positive_target_sets_the_single_leaf_path(self):
        demand_gen.campaign_update_bidding("1234567890", "222", target_cpa=5.0)
        _, operation = self.sent_operation(self.service.mutate_campaigns)
        self.assertEqual(
            self.appended_mask_paths(operation),
            ["maximize_conversions.target_cpa_micros"],
        )

        self.service.reset_mock()
        pmax.campaign_update_bidding("1234567890", "222", target_roas=3.5)
        _, operation = self.sent_operation(self.service.mutate_campaigns)
        self.assertEqual(
            self.appended_mask_paths(operation),
            ["maximize_conversion_value.target_roas"],
        )


class TestListCampaigns(WriteToolTestCase):

    def test_status_outside_allowlist_is_rejected(self):
        with self.assertRaises(ToolError):
            mutate.list_campaigns("1234567890", status="ENABLED'; DROP")
        self.service.search.assert_not_called()

    def test_lowercase_status_is_accepted(self):
        self.service.search.return_value = []
        result = mutate.list_campaigns("1234567890", status="enabled")
        self.assertEqual(result, [])
        query = self.service.search.call_args.kwargs["query"]
        self.assertIn("campaign.status = 'ENABLED'", query)


class TestKeywordsAdd(WriteToolTestCase):

    def test_auto_exempt_defaults_to_false(self):
        # Policy exemptions assert "false positive" on the account owner's
        # behalf; nothing may opt into that silently.
        parameters = inspect.signature(mutate.keywords_add).parameters
        self.assertIs(parameters["auto_exempt"].default, False)

    def test_dry_run_with_auto_exempt_carries_the_caveat(self):
        result = mutate.keywords_add(
            "1234567890", "111", ["kw one"], auto_exempt=True
        )
        self.assertIn("false positive", result["auto_exempt_note"])

    def test_dry_run_without_auto_exempt_has_no_caveat(self):
        result = mutate.keywords_add("1234567890", "111", ["kw one"])
        self.assertNotIn("auto_exempt_note", result)

    def test_policy_failed_entries_carry_exemptible_flag(self):
        # The tool deserializes GoogleAdsFailure through
        # type(client.get_type("GoogleAdsFailure")).deserialize, so the
        # mock has to hand back an instance whose class carries it.
        error = SimpleNamespace(
            message="Policy violation ",
            location=SimpleNamespace(
                field_path_elements=[
                    SimpleNamespace(field_name="operations", index=0)
                ]
            ),
            details=SimpleNamespace(
                policy_violation_details=SimpleNamespace(
                    is_exemptible=True,
                    key=SimpleNamespace(policy_name="HEALTH"),
                )
            ),
        )
        failure = SimpleNamespace(errors=[error])

        class FakeGoogleAdsFailure:
            @classmethod
            def deserialize(cls, value):
                return failure

        def get_type(name):
            if name == "GoogleAdsFailure":
                return FakeGoogleAdsFailure()
            return MagicMock(name=f"type:{name}")

        self.client.get_type.side_effect = get_type
        self.service.mutate_ad_group_criteria.return_value = SimpleNamespace(
            partial_failure_error=SimpleNamespace(
                details=[SimpleNamespace(value=b"serialized-failure")]
            ),
            results=[SimpleNamespace(resource_name="")],
        )

        result = mutate.keywords_add(
            "1234567890", "111", ["bad keyword"], confirm=True
        )
        self.assertEqual(
            result["policy_failed"],
            [
                {
                    "keyword": "bad keyword",
                    "reason": "Policy violation",
                    "exemptible": True,
                }
            ],
        )
        self.assertEqual(result["created_count"], 0)
        self.assertEqual(result["policy_exempted"], [])
        # auto_exempt defaults to False: no exemption retry may be sent.
        self.assertEqual(self.service.mutate_ad_group_criteria.call_count, 1)


class TestNoApiDryRuns(WriteToolTestCase):
    """The five tools that skip the API on dry-run must say so honestly
    and must not touch the client at all."""

    CALLS = [
        (experiments.experiment_create, ("1234567890", "Exp", "555"), {}),
        (experiments.experiment_end, ("1234567890", "777"), {}),
        (experiments.experiment_promote, ("1234567890", "777"), {}),
        (
            optimize.recommendation_apply,
            ("1234567890", ["customers/1234567890/recommendations/rec1"]),
            {},
        ),
        (
            optimize.recommendation_dismiss,
            ("1234567890", ["customers/1234567890/recommendations/rec1"]),
            {},
        ),
    ]

    def test_dry_run_is_honest_and_sends_nothing(self):
        for fn, args, kwargs in self.CALLS:
            with self.subTest(tool=fn.__name__):
                self.mock_get_googleads_client.reset_mock()
                self.mock_get_googleads_service.reset_mock()
                self.service.reset_mock()

                result = fn(*args, **kwargs)

                self.assertIs(result["applied"], False)
                self.assertIs(result["validated"], False)
                self.assertIn("Nothing was sent to Google Ads", result["note"])
                self.mock_get_googleads_client.assert_not_called()
                self.mock_get_googleads_service.assert_not_called()
                self.assertEqual(self.service.mock_calls, [])


class TestGaqlReadErrorsAreTranslated(WriteToolTestCase):
    """A GoogleAdsException from a GAQL read used to escape these tools as
    a raw exception instead of the module's formatted ToolError, because
    the search() call or its row iteration sat outside any try/except.

    Each fake service yields one row before raising, so the exception
    happens during iteration (page 2+) rather than on the search() call
    itself — matching how gapic actually pages, and proving the fix wraps
    the iteration, not just the call.
    """

    @staticmethod
    def _raising_after_one_row(exception, row):
        def fake_search(*args, **kwargs):
            yield row
            raise exception

        return fake_search

    def test_campaign_update_settings_search_error_is_translated(self):
        exception = make_google_ads_exception()
        row = SimpleNamespace(
            campaign=SimpleNamespace(asset_automation_settings=[])
        )
        self.service.search.side_effect = self._raising_after_one_row(
            exception, row
        )
        with self.assertRaises(ToolError) as caught:
            mutate.campaign_update_settings(
                "1234567890", "222", text_customization=True
            )
        self.assertIn("Request ID", str(caught.exception))

    def test_campaign_rename_search_error_is_translated(self):
        exception = make_google_ads_exception()
        row = SimpleNamespace(campaign=SimpleNamespace(name="Old Name"))
        self.service.search.side_effect = self._raising_after_one_row(
            exception, row
        )
        with self.assertRaises(ToolError) as caught:
            mutate.campaign_rename("1234567890", "222", "New Name")
        self.assertIn("Request ID", str(caught.exception))

    def test_pmax_asset_group_add_media_search_error_is_translated(self):
        exception = make_google_ads_exception()
        self.service.search.side_effect = self._raising_after_one_row(
            exception, SimpleNamespace()
        )
        with self.assertRaises(ToolError) as caught:
            pmax.asset_group_add_media(
                "1234567890", "333", ["999"], "YOUTUBE_VIDEO"
            )
        self.assertIn("Request ID", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
