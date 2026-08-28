# Copyright 2026 the google-ads-mcp-extended contributors.
# Licensed under the Apache License, Version 2.0.

"""One invariant, checked over every list-style read that feeds an
exists-check: a cut list must say so out loud.

An agent uses these lists to decide whether something already exists
before creating it, so a silently truncated page turns "not on this page"
into "does not exist" and produces a duplicate campaign, list or asset.
The rule each spec below is checked against:

  * the cut is detected by a probe row (the query asks for cap + 1 and
    the extra row is dropped), never inferred from ``len == cap``;
  * the truncated flag is True and the page holds exactly ``cap`` items;
  * the payload carries a ``warning`` string saying the list is
    incomplete — sectioned, naming the cut sections, where the tool caps
    several sections independently;
  * an under-cap answer reports truncated False and carries NO warning
    key at all, so the warning's presence is itself the signal;
  * the query orders deterministically, so raising the limit extends the
    same page instead of reshuffling it.

Only the public ``ads_mcp.utils`` seams are mocked; the memoized
``_get_googleads_client`` is never patched, and the cache is cleared in
setUp. Row factories already ground-truthed against a tool's own tests
are reused from those test modules — imported as modules, not
``from ... import``, so their TestCases are not collected a second time.
"""

import dataclasses
import unittest
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional, Tuple
from unittest.mock import MagicMock, patch

import ads_mcp.utils as utils
from ads_mcp.tools import audiences, demand_gen, experiments, extensions
from ads_mcp.tools import mutate, negatives, optimize, pmax, targeting
from ads_mcp.tools import tracking
from tests.tools import demand_gen_test, mutate_test, optimize_test

CUSTOMER_ID = "1234567890"
CAMPAIGN_ID = "222"

# Small on purpose: every spec is exercised at the same cap, so the
# over-cap case feeds CAP + 1 rows (one probe row past the cut) and the
# under-cap case feeds CAP - 1.
CAP = 2

# A truncation warning must be recognisable as one. Either the shared
# wording from utils.truncation_warning, or change_history's variant for
# the API's own 10000-row ceiling on change_event.
_NEVER_SILENT_PHRASES = ("truncated", "at most 10000 rows")


# --- Row factories -------------------------------------------------------
#
# Field types match the proto (ints for ids, counts and hours), because
# the tools coerce them with int()/str() and a str id would hide a broken
# coercion behind a passing test.


def make_campaign_asset_row(index):
    """One row of extensions_list_campaign_assets' campaign_asset query."""
    return SimpleNamespace(
        campaign_asset=SimpleNamespace(
            asset=f"customers/{CUSTOMER_ID}/assets/{index}",
            field_type=SimpleNamespace(name="SITELINK"),
            status=SimpleNamespace(name="ENABLED"),
        ),
        asset=SimpleNamespace(
            id=int(index),
            name=f"Asset {index}",
            sitelink_asset=SimpleNamespace(link_text=f"Link {index}"),
            callout_asset=SimpleNamespace(callout_text=""),
            structured_snippet_asset=SimpleNamespace(header=""),
        ),
    )


def make_campaign_criterion_row(index):
    """One LOCATION row of targeting_list_criteria's query.

    The unused language/ad_schedule sub-messages are still present: the
    tool reads them from the same row object on its other branches.
    """
    return SimpleNamespace(
        campaign_criterion=SimpleNamespace(
            criterion_id=int(index),
            type_=SimpleNamespace(name="LOCATION"),
            negative=False,
            location=SimpleNamespace(
                geo_target_constant=f"geoTargetConstants/{2840 + index}"
            ),
            language=SimpleNamespace(language_constant=""),
            ad_schedule=SimpleNamespace(
                day_of_week=SimpleNamespace(name="MONDAY"),
                start_hour=int(9),
                end_hour=int(18),
            ),
        )
    )


def make_shared_set_row(index):
    """One row of negatives_list_shared_sets' shared_set query."""
    return SimpleNamespace(
        shared_set=SimpleNamespace(
            id=int(index),
            name=f"Negatives {index}",
            member_count=int(index * 10),
            reference_count=int(index),
            status=SimpleNamespace(name="ENABLED"),
        )
    )


def make_customer_tracking_row():
    """The single account-level row tracking_list_tracking reads first."""
    return SimpleNamespace(
        customer=SimpleNamespace(
            tracking_url_template="https://example.com/acct?id={lpurl}",
            final_url_suffix="utm_medium=cpc",
        )
    )


def make_tracking_campaign_row(index):
    """One campaign row of tracking_list_tracking's second query.

    Both tracking fields are populated so the row survives the
    only_campaigns_with_tracking client-side filter as well.
    """
    return SimpleNamespace(
        campaign=SimpleNamespace(
            id=int(index),
            name=f"Camp {index}",
            tracking_url_template=f"https://example.com/{index}?id={{lpurl}}",
            final_url_suffix="utm_source=google",
        )
    )


def make_experiment_row(index):
    """One row of experiments_experiments_list' experiment query."""
    return SimpleNamespace(
        experiment=SimpleNamespace(
            experiment_id=int(index),
            name=f"Experiment {index}",
            status=SimpleNamespace(name="ENABLED"),
            type_=SimpleNamespace(name="SEARCH_CUSTOM"),
            start_date="2026-08-01",
            end_date="2026-08-31",
        )
    )


def make_asset_group_row(index):
    """One row of pmax_list_asset_groups' asset_group query."""
    return SimpleNamespace(
        asset_group=SimpleNamespace(
            id=int(index),
            name=f"Asset group {index}",
            status=SimpleNamespace(name="ENABLED"),
            ad_strength=SimpleNamespace(name="GOOD"),
            final_urls=[f"https://example.com/{index}"],
        ),
        campaign=SimpleNamespace(id=int(1000 + index), name=f"PMax {index}"),
    )


def make_audience_row(index):
    """One row of audiences_list_audiences' "audiences" section."""
    return SimpleNamespace(
        audience=SimpleNamespace(id=int(index), name=f"Audience {index}")
    )


def make_user_list_row(index):
    """One row of audiences_list_audiences' "user_lists" section."""
    return SimpleNamespace(
        user_list=SimpleNamespace(
            id=int(index),
            name=f"User list {index}",
            type_=SimpleNamespace(name="REMARKETING"),
            size_for_search=int(index * 100),
            size_for_display=int(index * 200),
        )
    )


def make_custom_audience_row(index):
    """One row of audiences_list_audiences' "custom_segments" section."""
    return SimpleNamespace(
        custom_audience=SimpleNamespace(
            id=int(index),
            name=f"Custom segment {index}",
            type_=SimpleNamespace(name="SEARCH"),
            status=SimpleNamespace(name="ENABLED"),
        )
    )


# --- How each tool is fed ------------------------------------------------


def _rows(make_row, count):
    return [make_row(index) for index in range(1, count + 1)]


def _search_returns(make_row):
    """Feeder for a tool whose whole answer comes from one search call."""

    def feed(service, count):
        service.search.return_value = _rows(make_row, count)

    return feed


def _feed_tracking(service, count):
    # Two queries: the account row first, then the campaigns the cap
    # applies to.
    service.search.side_effect = [
        [make_customer_tracking_row()],
        _rows(make_tracking_campaign_row, count),
    ]


def _feed_experiments(service, count):
    # The second query fetches the arms of the experiments that survived
    # the cut; an empty arm set keeps this file on the cut itself.
    service.search.side_effect = [_rows(make_experiment_row, count), []]


def _feed_audiences(service, count):
    # Three independent queries, each capped on its own.
    service.search.side_effect = [
        _rows(make_audience_row, count),
        _rows(make_user_list_row, count),
        _rows(make_custom_audience_row, count),
    ]


def _feed_keyword_ideas(service, count):
    # Not a GAQL read at all: the Keyword Planner service returns the
    # ideas directly, in its own order.
    service.generate_keyword_ideas.return_value = [
        mutate_test.make_keyword_idea(f"kw{index}")
        for index in range(1, count + 1)
    ]


# --- The parametrization -------------------------------------------------


@dataclasses.dataclass(frozen=True)
class ListToolSpec:
    """One list-style read, and how to drive and read it.

    Attributes:
        name: The mounted tool name, used as the subTest label.
        call: Invokes the tool with the given cap.
        feed: Loads that many rows into the mocked service.
        pages: Every capped item list in the result — more than one for a
            tool that caps several sections independently.
        truncated: Reads the tool's truncated signal as a single bool.
        order_by: GAQL fragments that must appear across the issued
            queries. Empty means the tool issues no GAQL at all (a
            documented exception, asserted as such).
        returned_key: The count key, or None for the two payload shapes
            that never had one.
        warning_contains: Extra fragments the warning must name.
        probe_limit: True when the cut is detected by a cap+1 LIMIT;
            False for a cap applied client-side, where no LIMIT is sent.
    """

    name: str
    call: Callable[[int], Dict[str, Any]]
    feed: Callable[[MagicMock, int], None]
    pages: Callable[[Dict[str, Any]], List[List[Any]]]
    order_by: Tuple[str, ...]
    truncated: Callable[[Dict[str, Any]], bool] = lambda r: r["truncated"]
    returned_key: Optional[str] = "returned"
    warning_contains: Tuple[str, ...] = ()
    probe_limit: bool = True


SPECS = (
    # --- the seven older envelopes, aligned in this wave ---
    ListToolSpec(
        name="extensions_list_campaign_assets",
        call=lambda cap: extensions.list_campaign_assets(
            CUSTOMER_ID, CAMPAIGN_ID, limit=cap
        ),
        feed=_search_returns(make_campaign_asset_row),
        pages=lambda r: [r["items"]],
        order_by=("ORDER BY asset.id",),
    ),
    ListToolSpec(
        name="targeting_list_criteria",
        call=lambda cap: targeting.list_criteria(
            CUSTOMER_ID, CAMPAIGN_ID, limit=cap
        ),
        feed=_search_returns(make_campaign_criterion_row),
        pages=lambda r: [r["items"]],
        order_by=("ORDER BY campaign_criterion.criterion_id",),
    ),
    ListToolSpec(
        name="negatives_list_shared_sets",
        call=lambda cap: negatives.list_shared_sets(CUSTOMER_ID, limit=cap),
        feed=_search_returns(make_shared_set_row),
        pages=lambda r: [r["items"]],
        order_by=("ORDER BY shared_set.name",),
    ),
    ListToolSpec(
        name="tracking_list_tracking",
        call=lambda cap: tracking.list_tracking(CUSTOMER_ID, limit=cap),
        feed=_feed_tracking,
        pages=lambda r: [r["campaigns"]],
        order_by=("ORDER BY campaign.name",),
        # only_campaigns_with_tracking filters client-side (GAQL has no
        # OR), so the cap counts kept rows and no LIMIT is sent.
        probe_limit=False,
        returned_key=None,
    ),
    ListToolSpec(
        name="experiments_experiments_list",
        call=lambda cap: experiments.experiments_list(CUSTOMER_ID, limit=cap),
        feed=_feed_experiments,
        pages=lambda r: [r["experiments"]],
        order_by=("ORDER BY experiment.name",),
    ),
    ListToolSpec(
        name="pmax_list_asset_groups",
        call=lambda cap: pmax.list_asset_groups(CUSTOMER_ID, limit=cap),
        feed=_search_returns(make_asset_group_row),
        pages=lambda r: [r["asset_groups"]],
        order_by=("ORDER BY asset_group.name",),
    ),
    ListToolSpec(
        name="audiences_list_audiences",
        call=lambda cap: audiences.list_audiences(CUSTOMER_ID, limit=cap),
        feed=_feed_audiences,
        # Three sections, each capped on its own: all three must be cut
        # and all three must be named in the one warning.
        pages=lambda r: [
            r["audiences"],
            r["user_lists"],
            r["custom_segments"],
        ],
        truncated=lambda r: any(r["truncated"].values()),
        order_by=(
            "ORDER BY audience.name",
            "ORDER BY user_list.name",
            "ORDER BY custom_audience.name",
        ),
        returned_key=None,
        warning_contains=("audiences", "user_lists", "custom_segments"),
    ),
    # --- the five wave-2.1 envelopes, the reference behaviour ---
    ListToolSpec(
        name="mutate_list_campaigns",
        call=lambda cap: mutate.list_campaigns(CUSTOMER_ID, limit=cap),
        feed=_search_returns(
            lambda index: mutate_test.make_campaign_list_row(
                index, f"Camp {index}"
            )
        ),
        pages=lambda r: [r["items"]],
        order_by=("ORDER BY campaign.status ASC, campaign.name ASC",),
    ),
    ListToolSpec(
        name="demandgen_list_assets",
        call=lambda cap: demand_gen.list_assets(CUSTOMER_ID, limit=cap),
        feed=_search_returns(
            lambda index: demand_gen_test.make_asset_row(
                index, f"Asset {index}"
            )
        ),
        pages=lambda r: [r["items"]],
        order_by=("ORDER BY asset.id",),
    ),
    ListToolSpec(
        name="optimize_recommendations_list",
        call=lambda cap: optimize.recommendations_list(CUSTOMER_ID, limit=cap),
        feed=_search_returns(optimize_test.make_recommendation_row),
        pages=lambda r: [r["items"]],
        order_by=("ORDER BY recommendation.resource_name",),
    ),
    ListToolSpec(
        name="optimize_change_history",
        call=lambda cap: optimize.change_history(CUSTOMER_ID, limit=cap),
        feed=_search_returns(optimize_test.make_change_event_row),
        pages=lambda r: [r["items"]],
        order_by=("ORDER BY change_event.change_date_time DESC",),
    ),
    ListToolSpec(
        name="mutate_keywords_ideas",
        call=lambda cap: mutate.keywords_ideas(
            CUSTOMER_ID, seed_keywords=["running shoes"], limit=cap
        ),
        feed=_feed_keyword_ideas,
        pages=lambda r: [r["items"]],
        # Documented exception: this is the Keyword Planner endpoint, not
        # GAQL. There is no query to order, and the docstring says the
        # order is whatever the API returns.
        order_by=(),
        probe_limit=False,
    ),
)

# Frozen so a spec cannot quietly disappear and take a tool's coverage
# with it: every list-style read that feeds an exists-check belongs here.
COVERED_TOOLS = frozenset(
    {
        "audiences_list_audiences",
        "demandgen_list_assets",
        "experiments_experiments_list",
        "extensions_list_campaign_assets",
        "mutate_keywords_ideas",
        "mutate_list_campaigns",
        "negatives_list_shared_sets",
        "optimize_change_history",
        "optimize_recommendations_list",
        "pmax_list_asset_groups",
        "targeting_list_criteria",
        "tracking_list_tracking",
    }
)


class TestListTruncationInvariants(unittest.TestCase):
    """Every list-style read must report its own cut, out loud."""

    def setUp(self):
        utils.clear_googleads_cache()
        self.client = MagicMock(name="googleads_client")
        # A fresh mock per get_type call, as the real client returns a
        # fresh proto — keywords_ideas builds a request that way.
        self.client.get_type.side_effect = lambda name: MagicMock(
            name=f"type:{name}"
        )
        self.service = MagicMock(name="googleads_service")
        for target, value in (
            ("ads_mcp.utils.get_googleads_client", self.client),
            ("ads_mcp.utils.get_googleads_service", self.service),
        ):
            patcher = patch(target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def reset(self):
        """Drops any rows a previous spec loaded, side_effect included."""
        self.service.reset_mock(return_value=True, side_effect=True)

    def issued_queries(self):
        return [
            call.kwargs["query"]
            for call in self.service.search.call_args_list
            if "query" in call.kwargs
        ]

    def test_truncated_lists_carry_an_explicit_warning(self):
        for spec in SPECS:
            with self.subTest(tool=spec.name):
                self.reset()
                spec.feed(self.service, CAP + 1)
                result = spec.call(CAP)

                self.assertIs(
                    spec.truncated(result),
                    True,
                    f"{spec.name} did not report the cut",
                )
                for page in spec.pages(result):
                    self.assertEqual(len(page), CAP)
                if spec.returned_key:
                    self.assertEqual(result[spec.returned_key], CAP)

                warning = result.get("warning")
                self.assertIsInstance(
                    warning,
                    str,
                    f"{spec.name} truncated silently: no warning key",
                )
                self.assertTrue(
                    any(phrase in warning for phrase in _NEVER_SILENT_PHRASES),
                    f"{spec.name} warning does not say the list was cut: "
                    f"{warning!r}",
                )
                for fragment in spec.warning_contains:
                    self.assertIn(fragment, warning)

    def test_under_cap_lists_are_not_truncated_and_carry_no_warning(self):
        # The warning's presence is the signal, so an untruncated answer
        # must not carry the key at all.
        for spec in SPECS:
            with self.subTest(tool=spec.name):
                self.reset()
                spec.feed(self.service, CAP - 1)
                result = spec.call(CAP)

                self.assertIs(spec.truncated(result), False)
                self.assertNotIn("warning", result)
                for page in spec.pages(result):
                    self.assertEqual(len(page), CAP - 1)

    def test_exactly_cap_rows_is_a_full_page_not_a_cut(self):
        # The behavioural half of "the cut is probed, not inferred": an
        # account holding exactly `cap` items is complete, and calling it
        # truncated would send the agent chasing rows that do not exist.
        for spec in SPECS:
            with self.subTest(tool=spec.name):
                self.reset()
                spec.feed(self.service, CAP)
                result = spec.call(CAP)

                self.assertIs(
                    spec.truncated(result),
                    False,
                    f"{spec.name} calls a full page a cut — it is "
                    "inferring truncation from len == cap instead of "
                    "reading a probe row",
                )
                self.assertNotIn("warning", result)
                for page in spec.pages(result):
                    self.assertEqual(len(page), CAP)

    def test_gaql_lists_order_deterministically_and_probe_past_the_cap(self):
        for spec in SPECS:
            with self.subTest(tool=spec.name):
                self.reset()
                spec.feed(self.service, CAP + 1)
                spec.call(CAP)
                queries = self.issued_queries()

                if not spec.order_by:
                    self.assertEqual(
                        queries,
                        [],
                        f"{spec.name} is recorded as issuing no GAQL, but "
                        f"it sent: {queries}",
                    )
                    continue

                for fragment in spec.order_by:
                    self.assertTrue(
                        any(fragment in query for query in queries),
                        f"{spec.name} lost its {fragment!r}: without a "
                        "stable order, raising the limit reshuffles the "
                        f"page instead of extending it. Sent: {queries}",
                    )

    def test_the_cut_is_probed_not_inferred_from_a_full_page(self):
        # A tool that fetched exactly `cap` rows could only guess at
        # truncation from len == cap, which is wrong whenever the account
        # holds exactly that many.
        for spec in SPECS:
            with self.subTest(tool=spec.name):
                self.reset()
                spec.feed(self.service, CAP + 1)
                spec.call(CAP)
                queries = self.issued_queries()

                if spec.probe_limit:
                    self.assertTrue(
                        any(f"LIMIT {CAP + 1}" in query for query in queries),
                        f"{spec.name} does not fetch a probe row past the "
                        f"cap. Sent: {queries}",
                    )
                else:
                    self.assertFalse(
                        any("LIMIT" in query for query in queries),
                        f"{spec.name} is recorded as capping client-side "
                        f"but sent a LIMIT: {queries}",
                    )

    def test_every_list_style_read_is_parametrized(self):
        self.assertEqual({spec.name for spec in SPECS}, COVERED_TOOLS)
        self.assertEqual(len(SPECS), len(COVERED_TOOLS))


if __name__ == "__main__":
    unittest.main()
