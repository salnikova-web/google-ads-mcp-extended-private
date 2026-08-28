# Copyright 2026 the google-ads-mcp-extended contributors.
# Licensed under the Apache License, Version 2.0.

"""Tests for experiments_experiment_create's multi-step failure context.

The tool writes in three steps: the experiment shell, then its arms, then
the scheduling call. Only the first one creates anything, and it is what
claims the (unique) experiment name — so a failure in step 2 or 3 leaves
a SETUP experiment behind and makes the obvious recovery, re-running the
tool, fail on duplicate-name instead. These tests pin the context the
error carries in that window.

Only the public ``ads_mcp.utils`` seams are mocked; the fixture is shared
with mutate_test, imported as a module so its own TestCases are not
collected a second time.
"""

import unittest
from types import SimpleNamespace

from fastmcp.exceptions import ToolError

from ads_mcp.tools import experiments
from tests.tools import mutate_test
from tests.tools.middleware_test import make_google_ads_exception

CUSTOMER_ID = "1234567890"
CONTROL_CAMPAIGN_ID = "555"
EXPERIMENT_RN = f"customers/{CUSTOMER_ID}/experiments/999"


class ExperimentCreateTestCase(mutate_test.WriteToolTestCase):

    def setUp(self):
        super().setUp()
        # A typed response: the resource name is spliced into the error
        # message, so a MagicMock stand-in would hide a broken format.
        self.service.mutate_experiments.return_value = SimpleNamespace(
            results=[SimpleNamespace(resource_name=EXPERIMENT_RN)]
        )

    def create(self, **kwargs):
        return experiments.experiment_create(
            CUSTOMER_ID,
            "Winter test",
            CONTROL_CAMPAIGN_ID,
            confirm=True,
            **kwargs,
        )


class TestExperimentCreateSucceeds(ExperimentCreateTestCase):

    def test_all_three_steps_run_and_the_resource_is_returned(self):
        result = self.create()
        self.service.mutate_experiments.assert_called_once()
        self.service.mutate_experiment_arms.assert_called_once()
        self.service.schedule_experiment.assert_called_once()
        self.assertTrue(result["applied"])
        self.assertEqual(result["experiment_resource"], EXPERIMENT_RN)


class TestOrphanedExperimentContext(ExperimentCreateTestCase):

    def assert_orphan_reported(self, message):
        self.assertIn(EXPERIMENT_RN, message)
        self.assertIn("was created but arms/scheduling failed", message)
        self.assertIn("duplicate-name", message)
        # The API's own diagnosis is not replaced by the context line.
        self.assertIn("Google Ads API Error", message)

    def test_an_arms_failure_names_the_experiment_left_behind(self):
        self.service.mutate_experiment_arms.side_effect = (
            make_google_ads_exception("Traffic split is invalid")
        )
        with self.assertRaises(ToolError) as caught:
            self.create()
        self.assert_orphan_reported(str(caught.exception))
        self.assertIn("Traffic split is invalid", str(caught.exception))
        self.service.schedule_experiment.assert_not_called()

    def test_a_scheduling_failure_names_the_experiment_left_behind(self):
        self.service.schedule_experiment.side_effect = (
            make_google_ads_exception("Campaign is not eligible")
        )
        with self.assertRaises(ToolError) as caught:
            self.create()
        self.assert_orphan_reported(str(caught.exception))

    def test_the_context_message_is_not_chained_to_its_cause(self):
        # GoogleAdsErrorMiddleware translates by walking __cause__: a
        # chained raise here would throw this message away and re-format a
        # generic one (tests.tools.middleware_test invariant).
        self.service.schedule_experiment.side_effect = (
            make_google_ads_exception()
        )
        with self.assertRaises(ToolError) as caught:
            self.create()
        self.assertIsNone(caught.exception.__cause__)

    def test_a_first_step_failure_reports_no_orphan(self):
        # Nothing was created, so claiming an experiment is stuck in the
        # account would send the caller looking for something that is not
        # there.
        self.service.mutate_experiments.side_effect = make_google_ads_exception(
            "Duplicate experiment name"
        )
        with self.assertRaises(ToolError) as caught:
            self.create()
        message = str(caught.exception)
        self.assertIn("Duplicate experiment name", message)
        self.assertNotIn("was created but", message)
        self.service.mutate_experiment_arms.assert_not_called()

    def test_a_non_api_failure_keeps_its_type_and_logs_the_orphan(self):
        # A transport or auth error still has to reach
        # GoogleAdsErrorMiddleware as itself, so the context goes to the
        # log rather than into a ToolError that would hide it.
        self.service.mutate_experiment_arms.side_effect = RuntimeError(
            "connection reset"
        )
        with self.assertLogs(
            "ads_mcp.tools.experiments", level="WARNING"
        ) as logs:
            with self.assertRaises(RuntimeError):
                self.create()
        self.assertIn(EXPERIMENT_RN, "\n".join(logs.output))
        self.assertIn("duplicate-name", "\n".join(logs.output))


if __name__ == "__main__":
    unittest.main()
