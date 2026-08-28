# Copyright 2026 the google-ads-mcp-extended contributors.
# Licensed under the Apache License, Version 2.0.

"""Criteria-construction tests for the targeting write tools.

The largest write module had only guard-level coverage: the shared
invariants (dry-run by default) and the truncation envelope of
``targeting_list_criteria`` (tests/tools/list_truncation_test.py) were
checked, but nothing looked at the operations these tools actually build.
Five tools are covered here, the ones whose bodies branch:

  * ``set_locations`` — the positive/negative criterion split;
  * ``set_ad_schedule`` — day and hour validation, and the ADD-only
    semantics behind "the first window silently ends 24/7 serving";
  * ``set_demographics`` — the ad-group/campaign XOR that picks a
    different service, request type and parent field;
  * ``set_device_bid_modifiers`` — the modifier of 0 that means "exclude
    this device" and must never be dropped as a falsy value;
  * ``set_languages`` — the only tool here with a lookup, and the one
    that splices caller input into a GAQL string literal.

Only the public ``ads_mcp.utils`` seams are mocked (the fixture is shared
with mutate_test, imported as a module so its own TestCases are not
collected twice); the memoized ``_get_googleads_client`` is never
patched, and the cache is cleared in setUp.
"""

import unittest
from types import SimpleNamespace

from fastmcp.exceptions import ToolError

from ads_mcp.tools import targeting
from tests.tools import mutate_test
from tests.tools.middleware_test import make_google_ads_exception

CUSTOMER_ID = "1234567890"
CAMPAIGN_ID = "222"
AD_GROUP_ID = "333"
CAMPAIGN_RN = f"customers/{CUSTOMER_ID}/campaigns/{CAMPAIGN_ID}"
AD_GROUP_RN = f"customers/{CUSTOMER_ID}/adGroups/{AD_GROUP_ID}"


def mutate_response(*resource_names):
    """A typed stand-in for a MutateCampaignCriteriaResponse."""
    return SimpleNamespace(
        results=[SimpleNamespace(resource_name=rn) for rn in resource_names]
    )


def language_row(code, constant_id):
    """One typed row of set_languages' language_constant lookup."""
    return SimpleNamespace(
        language_constant=SimpleNamespace(code=code, id=int(constant_id))
    )


class TargetingTestCase(mutate_test.WriteToolTestCase):
    """Adds the accessors the criteria tools are read back through."""

    def criteria_operations(self, method=None):
        """The ``operation.create`` objects handed to a criteria mutate."""
        method = method or self.service.mutate_campaign_criteria
        _, operations = self.sent_operations(method)
        return [op.create for op in operations]

    def sent_request(self, method=None):
        method = method or self.service.mutate_campaign_criteria
        return method.call_args.kwargs["request"]

    def enum_keys(self, enum_name):
        """The names looked up on ``client.enums.<enum_name>[...]``.

        Every subscript of a MagicMock returns the same child, so the keys
        that were asked for are the only thing that distinguishes one enum
        member from another here.
        """
        enum = getattr(self.client.enums, enum_name)
        return [c.args[0] for c in enum.__getitem__.call_args_list]


class TestSetLocations(TargetingTestCase):

    def test_an_empty_location_list_is_refused_before_any_call(self):
        with self.assertRaises(ToolError):
            targeting.set_locations(CUSTOMER_ID, CAMPAIGN_ID, [])
        self.service.mutate_campaign_criteria.assert_not_called()

    def test_each_id_becomes_a_positive_location_criterion(self):
        targeting.set_locations(CUSTOMER_ID, CAMPAIGN_ID, ["2276", "2840"])
        criteria = self.criteria_operations()
        self.assertEqual(len(criteria), 2)
        self.assertEqual([c.campaign for c in criteria], [CAMPAIGN_RN] * 2)
        self.assertEqual(
            [c.location.geo_target_constant for c in criteria],
            ["geoTargetConstants/2276", "geoTargetConstants/2840"],
        )
        # negative is left at its proto default; setting it to False would
        # be the same thing, but the code must not set it at all.
        for criterion in criteria:
            self.assertIsNot(criterion.negative, True)

    def test_negative_turns_every_criterion_into_an_exclusion(self):
        targeting.set_locations(
            CUSTOMER_ID, CAMPAIGN_ID, ["2276"], negative=True
        )
        (criterion,) = self.criteria_operations()
        self.assertIs(criterion.negative, True)
        self.assertEqual(
            criterion.location.geo_target_constant, "geoTargetConstants/2276"
        )

    def test_the_default_call_is_a_validated_dry_run(self):
        result = targeting.set_locations(CUSTOMER_ID, CAMPAIGN_ID, ["2276"])
        self.assertIs(self.sent_request().validate_only, True)
        self.assertFalse(result["applied"])
        self.assertTrue(result["validated"])
        self.assertEqual(result["action"], "targeting_set_locations")
        self.assertNotIn("created_resources", result)

    def test_confirm_applies_and_reports_the_created_resources(self):
        self.service.mutate_campaign_criteria.return_value = mutate_response(
            f"customers/{CUSTOMER_ID}/campaignCriteria/{CAMPAIGN_ID}~1"
        )
        result = targeting.set_locations(
            CUSTOMER_ID, CAMPAIGN_ID, ["2276"], confirm=True
        )
        self.assertIs(self.sent_request().validate_only, False)
        self.assertTrue(result["applied"])
        self.assertEqual(
            result["created_resources"],
            [f"customers/{CUSTOMER_ID}/campaignCriteria/{CAMPAIGN_ID}~1"],
        )

    def test_a_hyphenated_customer_id_is_normalised_everywhere(self):
        targeting.set_locations("123-456-7890", CAMPAIGN_ID, ["2276"])
        (criterion,) = self.criteria_operations()
        self.assertEqual(self.sent_request().customer_id, CUSTOMER_ID)
        self.assertEqual(criterion.campaign, CAMPAIGN_RN)

    def test_a_non_numeric_customer_id_never_reaches_a_resource_name(self):
        with self.assertRaises(ToolError):
            targeting.set_locations("12ab", CAMPAIGN_ID, ["2276"])
        self.service.mutate_campaign_criteria.assert_not_called()

    def test_an_api_failure_is_reported_as_a_tool_error(self):
        self.service.mutate_campaign_criteria.side_effect = (
            make_google_ads_exception()
        )
        with self.assertRaises(ToolError) as caught:
            targeting.set_locations(CUSTOMER_ID, CAMPAIGN_ID, ["2276"])
        self.assertIn("Google Ads API Error", str(caught.exception))


class TestSetAdSchedule(TargetingTestCase):

    WINDOW = {"day": "MONDAY", "start_hour": 8, "end_hour": 22}

    def test_an_empty_schedule_is_refused_before_any_call(self):
        with self.assertRaises(ToolError):
            targeting.set_ad_schedule(CUSTOMER_ID, CAMPAIGN_ID, [])
        self.service.mutate_campaign_criteria.assert_not_called()

    def test_an_unknown_day_is_refused(self):
        with self.assertRaises(ToolError) as caught:
            targeting.set_ad_schedule(
                CUSTOMER_ID,
                CAMPAIGN_ID,
                [{"day": "FUNDAY", "start_hour": 8, "end_hour": 22}],
            )
        self.assertIn("day must be one of", str(caught.exception))
        self.service.mutate_campaign_criteria.assert_not_called()

    def test_impossible_hour_ranges_are_refused(self):
        for window in (
            {"day": "MONDAY", "start_hour": 8, "end_hour": 8},
            {"day": "MONDAY", "start_hour": 22, "end_hour": 8},
            {"day": "MONDAY", "start_hour": -1, "end_hour": 8},
            {"day": "MONDAY", "start_hour": 0, "end_hour": 25},
            {"day": "MONDAY"},
        ):
            with self.subTest(window=window):
                with self.assertRaises(ToolError):
                    targeting.set_ad_schedule(
                        CUSTOMER_ID, CAMPAIGN_ID, [window]
                    )
        self.service.mutate_campaign_criteria.assert_not_called()

    def test_one_bad_window_stops_the_whole_schedule(self):
        # Validation runs over the full list before a single operation is
        # built, so a partial schedule can never be sent.
        with self.assertRaises(ToolError):
            targeting.set_ad_schedule(
                CUSTOMER_ID,
                CAMPAIGN_ID,
                [
                    self.WINDOW,
                    {"day": "MONDAY", "start_hour": 5, "end_hour": 5},
                ],
            )
        self.service.mutate_campaign_criteria.assert_not_called()

    def test_each_window_becomes_a_criterion_on_the_full_hour(self):
        targeting.set_ad_schedule(
            CUSTOMER_ID,
            CAMPAIGN_ID,
            [
                self.WINDOW,
                {"day": "saturday", "start_hour": 10, "end_hour": 18},
            ],
        )
        criteria = self.criteria_operations()
        self.assertEqual(len(criteria), 2)
        self.assertEqual([c.campaign for c in criteria], [CAMPAIGN_RN] * 2)
        # Lower-case day names are accepted and upper-cased for the enum.
        self.assertEqual(
            self.enum_keys("DayOfWeekEnum"), ["MONDAY", "SATURDAY"]
        )
        self.assertEqual(
            [
                (c.ad_schedule.start_hour, c.ad_schedule.end_hour)
                for c in criteria
            ],
            [(8, 22), (10, 18)],
        )
        zero = self.client.enums.MinuteOfHourEnum.ZERO
        for criterion in criteria:
            self.assertIs(criterion.ad_schedule.start_minute, zero)
            self.assertIs(criterion.ad_schedule.end_minute, zero)

    def test_hours_passed_as_strings_are_coerced_to_ints(self):
        targeting.set_ad_schedule(
            CUSTOMER_ID,
            CAMPAIGN_ID,
            [{"day": "MONDAY", "start_hour": "8", "end_hour": "22"}],
        )
        (criterion,) = self.criteria_operations()
        self.assertEqual(criterion.ad_schedule.start_hour, 8)
        self.assertEqual(criterion.ad_schedule.end_hour, 22)

    def test_windows_are_only_added_never_read_or_replaced(self):
        # The documented trap: the tool does not look at the existing
        # schedule and removes nothing, so the first window is what turns
        # 24/7 serving into "these hours only".
        targeting.set_ad_schedule(CUSTOMER_ID, CAMPAIGN_ID, [self.WINDOW])
        self.service.search.assert_not_called()
        self.assertEqual(self.service.mutate_campaign_criteria.call_count, 1)
        _, operations = self.sent_operations(
            self.service.mutate_campaign_criteria
        )
        for operation in operations:
            # remove_criterion is the only tool that fills this in (with a
            # resource-name string); an ADD-only tool must leave it alone.
            self.assertNotIsInstance(operation.remove, str)

    def test_the_default_call_is_a_validated_dry_run(self):
        result = targeting.set_ad_schedule(
            CUSTOMER_ID, CAMPAIGN_ID, [self.WINDOW]
        )
        self.assertIs(self.sent_request().validate_only, True)
        self.assertFalse(result["applied"])
        self.assertEqual(result["windows"], 1)
        self.assertEqual(result["action"], "targeting_set_ad_schedule")


class TestSetDemographics(TargetingTestCase):

    def ad_group_criteria(self):
        return self.criteria_operations(self.service.mutate_ad_group_criteria)

    def test_exactly_one_parent_id_is_required(self):
        for kwargs in (
            {},
            {"ad_group_id": AD_GROUP_ID, "campaign_id": CAMPAIGN_ID},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ToolError) as caught:
                    targeting.set_demographics(
                        CUSTOMER_ID, exclude_genders=["MALE"], **kwargs
                    )
                self.assertIn("exactly one", str(caught.exception))
        self.service.mutate_ad_group_criteria.assert_not_called()
        self.service.mutate_campaign_criteria.assert_not_called()

    def test_at_least_one_exclusion_is_required(self):
        with self.assertRaises(ToolError):
            targeting.set_demographics(CUSTOMER_ID, ad_group_id=AD_GROUP_ID)
        self.service.mutate_ad_group_criteria.assert_not_called()

    def test_ad_group_exclusions_use_the_ad_group_criterion_service(self):
        targeting.set_demographics(
            CUSTOMER_ID, ad_group_id=AD_GROUP_ID, exclude_genders=["MALE"]
        )
        self.mock_get_googleads_service.assert_called_with(
            "AdGroupCriterionService"
        )
        self.client.get_type.assert_any_call("MutateAdGroupCriteriaRequest")
        self.service.mutate_campaign_criteria.assert_not_called()
        (criterion,) = self.ad_group_criteria()
        self.assertEqual(criterion.ad_group, AD_GROUP_RN)

    def test_campaign_exclusions_use_the_campaign_criterion_service(self):
        targeting.set_demographics(
            CUSTOMER_ID, campaign_id=CAMPAIGN_ID, exclude_genders=["MALE"]
        )
        self.mock_get_googleads_service.assert_called_with(
            "CampaignCriterionService"
        )
        self.client.get_type.assert_any_call("MutateCampaignCriteriaRequest")
        self.service.mutate_ad_group_criteria.assert_not_called()
        (criterion,) = self.criteria_operations()
        self.assertEqual(criterion.campaign, CAMPAIGN_RN)

    def test_every_operation_is_an_exclusion(self):
        # The tool cannot target a demographic positively; negative=True is
        # unconditional, and a criterion that lost it would TARGET the
        # group the caller asked to exclude.
        targeting.set_demographics(
            CUSTOMER_ID,
            ad_group_id=AD_GROUP_ID,
            exclude_age_ranges=["18_24"],
            exclude_genders=["MALE", "FEMALE"],
        )
        criteria = self.ad_group_criteria()
        self.assertEqual(len(criteria), 3)
        for criterion in criteria:
            self.assertIs(criterion.negative, True)

    def test_age_range_keys_are_normalised_before_the_enum_lookup(self):
        targeting.set_demographics(
            CUSTOMER_ID,
            ad_group_id=AD_GROUP_ID,
            exclude_age_ranges=["18-24", "65_up"],
        )
        self.assertEqual(
            self.enum_keys("AgeRangeTypeEnum"),
            ["AGE_RANGE_18_24", "AGE_RANGE_65_UP"],
        )

    def test_gender_names_are_upper_cased_before_the_enum_lookup(self):
        targeting.set_demographics(
            CUSTOMER_ID, ad_group_id=AD_GROUP_ID, exclude_genders=["male"]
        )
        self.assertEqual(self.enum_keys("GenderTypeEnum"), ["MALE"])

    def test_an_unknown_age_range_or_gender_is_refused(self):
        for kwargs in (
            {"exclude_age_ranges": ["12_17"]},
            {"exclude_genders": ["OTHER"]},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ToolError):
                    targeting.set_demographics(
                        CUSTOMER_ID, ad_group_id=AD_GROUP_ID, **kwargs
                    )
        self.service.mutate_ad_group_criteria.assert_not_called()

    def test_the_default_call_is_a_validated_dry_run(self):
        result = targeting.set_demographics(
            CUSTOMER_ID, ad_group_id=AD_GROUP_ID, exclude_genders=["MALE"]
        )
        request = self.sent_request(self.service.mutate_ad_group_criteria)
        self.assertIs(request.validate_only, True)
        self.assertFalse(result["applied"])
        self.assertEqual(result["ad_group_id"], AD_GROUP_ID)
        self.assertIsNone(result["campaign_id"])
        self.assertEqual(result["action"], "targeting_set_demographics")


class TestSetDeviceBidModifiers(TargetingTestCase):

    def test_an_empty_modifier_map_is_refused_before_any_call(self):
        with self.assertRaises(ToolError):
            targeting.set_device_bid_modifiers(CUSTOMER_ID, CAMPAIGN_ID, {})
        self.service.mutate_campaign_criteria.assert_not_called()

    def test_an_unknown_device_is_refused(self):
        with self.assertRaises(ToolError) as caught:
            targeting.set_device_bid_modifiers(
                CUSTOMER_ID, CAMPAIGN_ID, {"WATCH": 1.2}
            )
        self.assertIn("Unknown device", str(caught.exception))
        self.service.mutate_campaign_criteria.assert_not_called()

    def test_each_device_becomes_a_criterion_with_its_multiplier(self):
        targeting.set_device_bid_modifiers(
            CUSTOMER_ID, CAMPAIGN_ID, {"MOBILE": 1.2, "desktop": 0.9}
        )
        criteria = self.criteria_operations()
        self.assertEqual([c.campaign for c in criteria], [CAMPAIGN_RN] * 2)
        self.assertEqual(self.enum_keys("DeviceEnum"), ["MOBILE", "DESKTOP"])
        self.assertEqual([c.bid_modifier for c in criteria], [1.2, 0.9])

    def test_a_zero_modifier_survives_as_a_real_zero(self):
        # 0 EXCLUDES the device. Anything that treats it as a falsy value
        # to skip would silently leave the device bidding normally.
        targeting.set_device_bid_modifiers(
            CUSTOMER_ID, CAMPAIGN_ID, {"TABLET": 0}
        )
        (criterion,) = self.criteria_operations()
        self.assertEqual(criterion.bid_modifier, 0.0)
        self.assertIsInstance(criterion.bid_modifier, float)

    def test_an_integer_modifier_is_stored_as_a_float(self):
        targeting.set_device_bid_modifiers(
            CUSTOMER_ID, CAMPAIGN_ID, {"MOBILE": 1}
        )
        (criterion,) = self.criteria_operations()
        self.assertIsInstance(criterion.bid_modifier, float)

    def test_the_default_call_is_a_validated_dry_run(self):
        result = targeting.set_device_bid_modifiers(
            CUSTOMER_ID, CAMPAIGN_ID, {"MOBILE": 1.2}
        )
        self.assertIs(self.sent_request().validate_only, True)
        self.assertFalse(result["applied"])
        self.assertEqual(result["modifiers"], {"MOBILE": 1.2})
        self.assertEqual(result["action"], "targeting_set_device_bid_modifiers")


class TestSetLanguages(TargetingTestCase):
    """The lookup tool: codes are spliced into a GAQL string literal."""

    def test_an_empty_code_list_is_refused_before_any_call(self):
        with self.assertRaises(ToolError):
            targeting.set_languages(CUSTOMER_ID, CAMPAIGN_ID, [])
        self.service.search.assert_not_called()

    def test_codes_are_canonicalised_for_the_lookup(self):
        self.service.search.return_value = [
            language_row("en", 1000),
            language_row("zh-CN", 1018),
        ]
        targeting.set_languages(CUSTOMER_ID, CAMPAIGN_ID, ["EN", "zh-cn"])
        query = self.service.search.call_args.kwargs["query"]
        self.assertIn("IN ('en', 'zh-CN')", query)

    def test_a_code_that_could_escape_the_gaql_literal_is_refused(self):
        for code in ("en' OR '1'='1", "en\\", "en';--", "e", "toolongcode"):
            with self.subTest(code=code):
                with self.assertRaises(ToolError) as caught:
                    targeting.set_languages(CUSTOMER_ID, CAMPAIGN_ID, [code])
                self.assertIn("Invalid language code", str(caught.exception))
        self.service.search.assert_not_called()

    def test_a_code_the_lookup_does_not_know_stops_the_write(self):
        self.service.search.return_value = [language_row("en", 1000)]
        with self.assertRaises(ToolError) as caught:
            targeting.set_languages(CUSTOMER_ID, CAMPAIGN_ID, ["en", "de"])
        self.assertIn("Unknown language codes: ['de']", str(caught.exception))
        self.service.mutate_campaign_criteria.assert_not_called()

    def test_each_found_code_becomes_a_language_criterion(self):
        self.service.search.return_value = [
            language_row("en", 1000),
            language_row("de", 1001),
        ]
        result = targeting.set_languages(CUSTOMER_ID, CAMPAIGN_ID, ["en", "de"])
        criteria = self.criteria_operations()
        self.assertEqual([c.campaign for c in criteria], [CAMPAIGN_RN] * 2)
        self.assertEqual(
            [c.language.language_constant for c in criteria],
            ["languageConstants/1000", "languageConstants/1001"],
        )
        self.assertIs(self.sent_request().validate_only, True)
        self.assertEqual(result["languages"], ["en", "de"])


if __name__ == "__main__":
    unittest.main()
