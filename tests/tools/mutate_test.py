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

    def sent_operations(self, method):
        """Returns (request, [operations]) captured by a service method mock,
        for the batch tools that append several operations."""
        request = method.call_args.kwargs["request"]
        operations = [
            call.args[0] for call in request.operations.append.call_args_list
        ]
        return request, operations

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


def make_partial_failure(index, message):
    """Builds (get_type_side_effect, partial_failure_error) for one failed op.

    The tools deserialize GoogleAdsFailure through
    ``type(client.get_type("GoogleAdsFailure")).deserialize``, so the mock
    has to hand back an instance whose class carries that classmethod.
    """
    failure = SimpleNamespace(
        errors=[
            SimpleNamespace(
                message=message,
                location=SimpleNamespace(
                    field_path_elements=[
                        SimpleNamespace(field_name="operations", index=index)
                    ]
                ),
            )
        ]
    )

    class FakeGoogleAdsFailure:
        @classmethod
        def deserialize(cls, value):
            return failure

    def get_type(name):
        if name == "GoogleAdsFailure":
            return FakeGoogleAdsFailure()
        return MagicMock(name=f"type:{name}")

    error = SimpleNamespace(details=[SimpleNamespace(value=b"serialized")])
    return get_type, error


class TestCampaignUpdateStatusBatch(WriteToolTestCase):
    """One request for many campaigns: atomic dry-run, partial apply."""

    def test_dry_run_validates_atomically(self):
        result = mutate.campaign_update_status_batch(
            "123-456-7890", ["111", "222", "333"], "paused"
        )
        request, operations = self.sent_operations(
            self.service.mutate_campaigns
        )
        # validate_only and partial_failure are mutually exclusive in the
        # API: the preview must be the atomic one.
        self.assertIs(request.validate_only, True)
        self.assertIs(request.partial_failure, False)
        self.assertEqual(self.service.mutate_campaigns.call_count, 1)
        self.assertEqual(len(operations), 3)
        for operation, campaign_id in zip(operations, ["111", "222", "333"]):
            self.assertEqual(self.appended_mask_paths(operation), ["status"])
            self.assertEqual(
                operation.update.resource_name,
                f"customers/1234567890/campaigns/{campaign_id}",
            )
        self.assertIs(result["applied"], False)
        self.assertIs(result["validated"], True)
        self.assertEqual(result["new_status"], "PAUSED")
        self.assertEqual(result["requested"], 3)
        self.assertEqual(
            [row["campaign_id"] for row in result["campaigns"]],
            ["111", "222", "333"],
        )

    def test_apply_uses_partial_failure(self):
        self.service.mutate_campaigns.return_value = SimpleNamespace(
            partial_failure_error=None,
            results=[
                SimpleNamespace(
                    resource_name="customers/1234567890/campaigns/111"
                ),
                SimpleNamespace(
                    resource_name="customers/1234567890/campaigns/222"
                ),
            ],
        )
        result = mutate.campaign_update_status_batch(
            "1234567890", ["111", "222"], "ENABLED", confirm=True
        )
        request, operations = self.sent_operations(
            self.service.mutate_campaigns
        )
        self.assertIs(request.partial_failure, True)
        self.assertIs(request.validate_only, False)
        self.assertEqual(len(operations), 2)
        self.assertIs(result["applied"], True)
        self.assertEqual(result["requested"], 2)
        self.assertEqual(result["succeeded"], 2)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(result["failed_campaigns"], [])

    def test_apply_reports_per_campaign_failures(self):
        get_type, partial_failure_error = make_partial_failure(1, " boom ")
        self.client.get_type.side_effect = get_type
        self.service.mutate_campaigns.return_value = SimpleNamespace(
            partial_failure_error=partial_failure_error,
            results=[
                SimpleNamespace(
                    resource_name="customers/1234567890/campaigns/111"
                ),
                SimpleNamespace(resource_name=""),
            ],
        )
        result = mutate.campaign_update_status_batch(
            "1234567890", ["111", "222"], "PAUSED", confirm=True
        )
        # Partial success must be unmistakable: counts first, then rows.
        self.assertEqual(result["requested"], 2)
        self.assertEqual(result["succeeded"], 1)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(
            result["succeeded_campaigns"],
            [
                {
                    "campaign_id": "111",
                    "resource_name": "customers/1234567890/campaigns/111",
                }
            ],
        )
        self.assertEqual(
            result["failed_campaigns"],
            [{"campaign_id": "222", "error": "boom"}],
        )

    def test_operation_without_a_result_counts_as_failed(self):
        # partial_failure_error cannot always pin a failure to an operation
        # index, and a short results list has no entry to read. Counting
        # "not in the failure map" as success would report a campaign that
        # was never touched as applied.
        self.service.mutate_campaigns.return_value = SimpleNamespace(
            partial_failure_error=None,
            results=[
                SimpleNamespace(
                    resource_name="customers/1234567890/campaigns/111"
                )
            ],
        )
        result = mutate.campaign_update_status_batch(
            "1234567890", ["111", "222"], "PAUSED", confirm=True
        )
        self.assertEqual(result["requested"], 2)
        self.assertEqual(result["succeeded"], 1)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(
            [row["campaign_id"] for row in result["failed_campaigns"]], ["222"]
        )
        self.assertIn("NOT applied", result["failed_campaigns"][0]["error"])

    def test_empty_resource_name_counts_as_failed(self):
        self.service.mutate_campaigns.return_value = SimpleNamespace(
            partial_failure_error=None,
            results=[
                SimpleNamespace(resource_name=""),
                SimpleNamespace(
                    resource_name="customers/1234567890/campaigns/222"
                ),
            ],
        )
        result = mutate.campaign_update_status_batch(
            "1234567890", ["111", "222"], "PAUSED", confirm=True
        )
        self.assertEqual(result["succeeded"], 1)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(
            [row["campaign_id"] for row in result["succeeded_campaigns"]],
            ["222"],
        )
        self.assertEqual(
            [row["campaign_id"] for row in result["failed_campaigns"]], ["111"]
        )

    def test_removed_is_refused_and_points_at_the_single_tool(self):
        with self.assertRaises(ToolError) as caught:
            mutate.campaign_update_status_batch(
                "1234567890", ["111", "222"], "REMOVED"
            )
        self.assertIn("mutate_campaign_update_status", str(caught.exception))
        self.service.mutate_campaigns.assert_not_called()

    def test_unknown_status_and_empty_list_are_refused(self):
        with self.assertRaises(ToolError):
            mutate.campaign_update_status_batch("1234567890", ["111"], "DRAFT")
        with self.assertRaises(ToolError):
            mutate.campaign_update_status_batch("1234567890", [], "PAUSED")
        self.service.mutate_campaigns.assert_not_called()

    def test_over_the_cap_is_refused_naming_the_cap(self):
        with self.assertRaises(ToolError) as caught:
            mutate.campaign_update_status_batch(
                "1234567890", [str(i) for i in range(1, 102)], "PAUSED"
            )
        self.assertIn("100", str(caught.exception))
        self.service.mutate_campaigns.assert_not_called()

    def test_non_numeric_id_is_refused(self):
        with self.assertRaises(ToolError):
            mutate.campaign_update_status_batch(
                "1234567890", ["111", "1; DROP"], "PAUSED"
            )
        self.service.mutate_campaigns.assert_not_called()

    def test_duplicate_ids_are_deduped_and_reported(self):
        result = mutate.campaign_update_status_batch(
            "1234567890", ["111", "222", "111"], "PAUSED"
        )
        _, operations = self.sent_operations(self.service.mutate_campaigns)
        # One operation per campaign: two operations on the same campaign in
        # one request are rejected by the API.
        self.assertEqual(len(operations), 2)
        self.assertEqual(result["requested"], 2)
        self.assertEqual(result["duplicate_campaign_ids_ignored"], ["111"])
        self.assertIn("duplicate", result["warning"])


def make_budget_row(
    campaign_id,
    name,
    budget_resource,
    amount_micros,
    explicitly_shared=False,
):
    """A typed stand-in for one row of the batch budget lookup.

    Field types match the proto (ints for ids and micros, bool for the
    shared flag), because the tool coerces them with int()/str()/bool().
    """
    return SimpleNamespace(
        campaign=SimpleNamespace(
            id=int(campaign_id),
            name=name,
            campaign_budget=budget_resource,
        ),
        campaign_budget=SimpleNamespace(
            amount_micros=int(amount_micros),
            explicitly_shared=explicitly_shared,
        ),
    )


class TestCampaignBudgetUpdateBatch(WriteToolTestCase):
    """One lookup, one request; shared budgets collapse or fail loudly."""

    OWN_BUDGETS = [
        make_budget_row(
            111, "Camp A", "customers/1234567890/campaignBudgets/1", 10_000_000
        ),
        make_budget_row(
            222, "Camp B", "customers/1234567890/campaignBudgets/2", 20_000_000
        ),
    ]
    SHARED_BUDGET = [
        make_budget_row(
            111,
            "Camp A",
            "customers/1234567890/campaignBudgets/9",
            10_000_000,
            explicitly_shared=True,
        ),
        make_budget_row(
            222,
            "Camp B",
            "customers/1234567890/campaignBudgets/9",
            10_000_000,
            explicitly_shared=True,
        ),
    ]

    def test_dry_run_validates_atomically(self):
        self.service.search.return_value = self.OWN_BUDGETS
        result = mutate.campaign_budget_update_batch(
            "1234567890",
            [
                {"campaign_id": "111", "new_daily_budget": 15.0},
                {"campaign_id": "222", "new_daily_budget": 25.5},
            ],
        )
        query = self.service.search.call_args.kwargs["query"]
        self.assertIn("campaign.id IN (111, 222)", query)
        self.assertEqual(self.service.search.call_count, 1)

        request, operations = self.sent_operations(
            self.service.mutate_campaign_budgets
        )
        self.assertIs(request.validate_only, True)
        self.assertIs(request.partial_failure, False)
        self.assertEqual(len(operations), 2)
        for operation in operations:
            self.assertEqual(
                self.appended_mask_paths(operation), ["amount_micros"]
            )
        self.assertEqual(operations[0].update.amount_micros, 15_000_000)
        self.assertEqual(operations[1].update.amount_micros, 25_500_000)

        self.assertIs(result["applied"], False)
        self.assertEqual(result["requested"], 2)
        self.assertEqual(result["budget_operations"], 2)
        self.assertEqual(
            result["budgets"][0],
            {
                "campaign_id": "111",
                "campaign_name": "Camp A",
                "budget_resource": "customers/1234567890/campaignBudgets/1",
                "old_amount_micros": 10_000_000,
                "new_amount_micros": 15_000_000,
                "new_daily_budget": 15.0,
                "shared": False,
            },
        )
        self.assertNotIn("warning", result)

    def test_apply_uses_partial_failure(self):
        self.service.search.return_value = self.OWN_BUDGETS
        self.service.mutate_campaign_budgets.return_value = SimpleNamespace(
            partial_failure_error=None,
            results=[
                SimpleNamespace(
                    resource_name="customers/1234567890/campaignBudgets/1"
                ),
                SimpleNamespace(
                    resource_name="customers/1234567890/campaignBudgets/2"
                ),
            ],
        )
        result = mutate.campaign_budget_update_batch(
            "1234567890",
            [
                {"campaign_id": "111", "new_daily_budget": 15.0},
                {"campaign_id": "222", "new_daily_budget": 25.0},
            ],
            confirm=True,
        )
        request, _ = self.sent_operations(self.service.mutate_campaign_budgets)
        self.assertIs(request.partial_failure, True)
        self.assertIs(request.validate_only, False)
        self.assertIs(result["applied"], True)
        self.assertEqual(result["succeeded"], 2)
        self.assertEqual(result["failed"], 0)

    def test_apply_reports_per_campaign_failures(self):
        self.service.search.return_value = self.OWN_BUDGETS
        get_type, partial_failure_error = make_partial_failure(0, "too low")
        self.client.get_type.side_effect = get_type
        self.service.mutate_campaign_budgets.return_value = SimpleNamespace(
            partial_failure_error=partial_failure_error,
            results=[
                SimpleNamespace(resource_name=""),
                SimpleNamespace(
                    resource_name="customers/1234567890/campaignBudgets/2"
                ),
            ],
        )
        result = mutate.campaign_budget_update_batch(
            "1234567890",
            [
                {"campaign_id": "111", "new_daily_budget": 0.01},
                {"campaign_id": "222", "new_daily_budget": 25.0},
            ],
            confirm=True,
        )
        self.assertEqual(result["succeeded"], 1)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(
            result["failed_campaigns"],
            [
                {
                    "campaign_id": "111",
                    "budget_resource": (
                        "customers/1234567890/campaignBudgets/1"
                    ),
                    "error": "too low",
                }
            ],
        )

    def test_operation_without_a_result_counts_as_failed(self):
        # Same guard as the status batch: an operation the API did not
        # confirm must never inflate the succeeded count. On a shared budget
        # the miss has to land on every campaign in the group.
        self.service.search.return_value = self.SHARED_BUDGET
        self.service.mutate_campaign_budgets.return_value = SimpleNamespace(
            partial_failure_error=None,
            results=[SimpleNamespace(resource_name="")],
        )
        result = mutate.campaign_budget_update_batch(
            "1234567890",
            [
                {"campaign_id": "111", "new_daily_budget": 30.0},
                {"campaign_id": "222", "new_daily_budget": 30.0},
            ],
            confirm=True,
        )
        self.assertEqual(result["requested"], 2)
        self.assertEqual(result["budget_operations"], 1)
        self.assertEqual(result["succeeded"], 0)
        self.assertEqual(result["failed"], 2)
        self.assertEqual(result["succeeded_campaigns"], [])
        self.assertEqual(
            [row["campaign_id"] for row in result["failed_campaigns"]],
            ["111", "222"],
        )
        self.assertIn("NOT applied", result["failed_campaigns"][0]["error"])

    def test_missing_result_entry_counts_as_failed(self):
        self.service.search.return_value = self.OWN_BUDGETS
        self.service.mutate_campaign_budgets.return_value = SimpleNamespace(
            partial_failure_error=None,
            results=[
                SimpleNamespace(
                    resource_name="customers/1234567890/campaignBudgets/1"
                )
            ],
        )
        result = mutate.campaign_budget_update_batch(
            "1234567890",
            [
                {"campaign_id": "111", "new_daily_budget": 15.0},
                {"campaign_id": "222", "new_daily_budget": 25.0},
            ],
            confirm=True,
        )
        self.assertEqual(result["succeeded"], 1)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(
            [row["campaign_id"] for row in result["failed_campaigns"]], ["222"]
        )

    def test_same_amount_on_a_shared_budget_collapses_to_one_operation(self):
        self.service.search.return_value = self.SHARED_BUDGET
        result = mutate.campaign_budget_update_batch(
            "1234567890",
            [
                {"campaign_id": "111", "new_daily_budget": 30.0},
                {"campaign_id": "222", "new_daily_budget": 30.0},
            ],
        )
        _, operations = self.sent_operations(
            self.service.mutate_campaign_budgets
        )
        # Two operations on one budget resource in a single request are
        # rejected by the API, so the duplicate must collapse.
        self.assertEqual(len(operations), 1)
        self.assertEqual(result["requested"], 2)
        self.assertEqual(result["budget_operations"], 1)
        self.assertEqual(
            result["shared_budget_collapsed"],
            [
                {
                    "budget_resource": (
                        "customers/1234567890/campaignBudgets/9"
                    ),
                    "campaign_ids": ["111", "222"],
                    "new_daily_budget": 30.0,
                }
            ],
        )
        self.assertTrue(all(row["shared"] for row in result["budgets"]))
        self.assertIn("shared budget", result["budgets"][0]["warning"])
        self.assertIn("SHARED", result["warning"])

    def test_conflicting_amounts_on_a_shared_budget_are_refused(self):
        self.service.search.return_value = self.SHARED_BUDGET
        with self.assertRaises(ToolError) as caught:
            mutate.campaign_budget_update_batch(
                "1234567890",
                [
                    {"campaign_id": "111", "new_daily_budget": 30.0},
                    {"campaign_id": "222", "new_daily_budget": 40.0},
                ],
            )
        message = str(caught.exception)
        self.assertIn("campaignBudgets/9", message)
        self.assertIn("Nothing was changed", message)
        self.service.mutate_campaign_budgets.assert_not_called()

    def test_unknown_campaign_fails_before_any_mutate(self):
        self.service.search.return_value = self.OWN_BUDGETS[:1]
        with self.assertRaises(ToolError) as caught:
            mutate.campaign_budget_update_batch(
                "1234567890",
                [
                    {"campaign_id": "111", "new_daily_budget": 15.0},
                    {"campaign_id": "222", "new_daily_budget": 25.0},
                ],
            )
        self.assertIn("222", str(caught.exception))
        self.service.mutate_campaign_budgets.assert_not_called()

    def test_duplicate_campaign_id_is_refused(self):
        with self.assertRaises(ToolError) as caught:
            mutate.campaign_budget_update_batch(
                "1234567890",
                [
                    {"campaign_id": "111", "new_daily_budget": 15.0},
                    {"campaign_id": "111", "new_daily_budget": 25.0},
                ],
            )
        self.assertIn("updates[1]", str(caught.exception))
        self.service.search.assert_not_called()
        self.service.mutate_campaign_budgets.assert_not_called()

    def test_malformed_entries_name_their_index(self):
        cases = [
            [{"new_daily_budget": 15.0}],
            [{"campaign_id": "111"}],
            [{"campaign_id": "abc", "new_daily_budget": 15.0}],
            [{"campaign_id": "111", "new_daily_budget": 0}],
            [{"campaign_id": "111", "new_daily_budget": -5.0}],
            [{"campaign_id": "111", "new_daily_budget": "lots"}],
            # bool is an int subclass, and NaN/inf survive a plain "> 0"
            # check only to crash inside the micros conversion.
            [{"campaign_id": "111", "new_daily_budget": True}],
            [{"campaign_id": "111", "new_daily_budget": float("nan")}],
            [{"campaign_id": "111", "new_daily_budget": float("inf")}],
            ["111"],
        ]
        for updates in cases:
            with self.subTest(updates=updates):
                with self.assertRaises(ToolError) as caught:
                    mutate.campaign_budget_update_batch("1234567890", updates)
                self.assertIn("updates[0]", str(caught.exception))
        self.service.search.assert_not_called()
        self.service.mutate_campaign_budgets.assert_not_called()

    def test_empty_and_oversized_batches_are_refused(self):
        with self.assertRaises(ToolError):
            mutate.campaign_budget_update_batch("1234567890", [])
        with self.assertRaises(ToolError) as caught:
            mutate.campaign_budget_update_batch(
                "1234567890",
                [
                    {"campaign_id": str(i), "new_daily_budget": 10.0}
                    for i in range(1, 102)
                ],
            )
        self.assertIn("100", str(caught.exception))
        self.service.search.assert_not_called()


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
