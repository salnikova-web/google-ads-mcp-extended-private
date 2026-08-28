# Copyright 2026 the google-ads-mcp-extended contributors.
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

"""Shared plumbing for the write (mutate) tool modules.

This module deliberately declares NO ``FastMCP`` instance. The coordinator
discovers namespaces by importing every module under ``ads_mcp.tools`` and
picking up the ``FastMCP`` objects it finds, so a library module here is
loaded and then ignored — which is the point: the leading underscore says
"not a namespace" to a reader, and the missing sub-server says it to the
discovery loop.

``ads_mcp.tools.mutate`` used to double as this library and still re-exports
everything below, so ``from ads_mcp.tools.mutate import _preview_or_done``
keeps working for anything outside this package.
"""

import time
from typing import Any, Dict, List, NoReturn, Optional

from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from google.ads.googleads.errors import GoogleAdsException

import ads_mcp.utils as utils

_MICROS = 1_000_000

# One shared instance for the default write annotations. Thirteen write
# modules used to build an identical copy of this line, so a change to the
# safety hints had to be made thirteen times to take effect everywhere.
# Tools that are destructive (REMOVED is irreversible) still declare their
# own ToolAnnotations inline, next to the tool that is destructive.
_WRITE_ANNOTATIONS = ToolAnnotations(readOnlyHint=False, destructiveHint=False)


def _raise_tool_error(ex: GoogleAdsException) -> NoReturn:
    """Re-raises a Google Ads failure as a formatted ToolError.

    A bare-raise shim on purpose: ``utils.raise_tool_error`` raises inside
    the caller's ``except`` block, so the original exception lands in
    ``__context__`` and never in ``__cause__``. ``GoogleAdsErrorMiddleware``
    translates by walking ``__cause__``, so a chained
    ``raise ToolError(...) from ex`` here would throw the formatted message
    away (see tests.tools.middleware_test.TestChainedToolErrorInvariant).
    """
    utils.raise_tool_error(ex)


def _to_micros(amount: float) -> int:
    return int(round(float(amount) * _MICROS))


def _clean_customer_id(customer_id: str) -> str:
    """Normalises a customer id to digits, rejecting anything else.

    Customer ids are spliced into resource names and query conditions, so a
    value that is not purely numeric must never get through.
    """
    return utils.gaql_id(str(customer_id).replace("-", "").strip())


def _check_len(items: List[str], max_len: int, label: str) -> None:
    """Rejects any text asset over the field's character limit."""
    bad = [i for i in items if len(i) > max_len]
    if bad:
        raise ToolError(f"{label} over {max_len} chars: {bad}")


def _text_assets(client, texts: List[str]):
    """Wraps plain strings into inline AdTextAsset protos."""
    out = []
    for t in texts:
        a = client.get_type("AdTextAsset")
        a.text = t
        out.append(a)
    return out


def _preview_or_done(
    confirm: bool,
    action: str,
    details: Dict[str, Any],
    validated: bool = True,
    note: Optional[str] = None,
) -> Dict[str, Any]:
    """Builds the applied/dry-run result envelope.

    Args:
        confirm: True when the operation was actually applied.
        action: Tool-specific action name reported back to the caller.
        details: Extra fields to merge into the result.
        validated: Whether the dry-run really sent a validate_only request to
            Google Ads. Pass False from tools that skip the API entirely when
            confirm is false, so the preview does not claim a validation that
            never happened. ``details`` is merged first, so this flag always
            wins over a stray key of the same name.
        note: Replaces the standard dry-run note. Only for tools whose
            dry-run is neither "fully validated" nor "nothing sent" — a
            multi-step tool that validates one step and previews the rest
            locally, where both canned notes would be wrong. Ignored when
            confirm is true.
    """
    if confirm:
        return {**details, "applied": True, "action": action}
    if note is None:
        if validated:
            note = (
                "DRY-RUN: the operation was validated by Google Ads "
                "(validate_only=true) but NOT applied. Re-run the tool with "
                "confirm=true to apply it."
            )
        else:
            note = (
                "DRY-RUN: this preview was computed locally. Nothing was sent "
                "to Google Ads, so nothing was validated and the operation "
                "may still fail when applied. Re-run the tool with "
                "confirm=true to apply it."
            )
    return {
        **details,
        "applied": False,
        "validated": validated,
        "action": action,
        "note": note,
    }


def build_campaign_with_budget(
    client,
    customer_id: str,
    name: str,
    daily_budget: float,
    channel_type: str,
    status: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    tracking_url_template: Optional[str] = None,
    final_url_suffix: Optional[str] = None,
    campaign_resource_name: Optional[str] = None,
):
    """Builds the budget+campaign operation pair every create tool opens with.

    Five channel-specific ``campaign_create`` tools shared this block
    verbatim: a temporary ``campaignBudgets/-1`` budget, a campaign
    pointing at it by that temporary name, the EU political advertising
    declaration, the "YYYY-MM-DD" -> date-time normalisation and the
    ``{lpurl}`` check on the tracking template. What differs per channel —
    the bidding strategy branch, network settings, shopping/Demand Gen
    settings — stays with the caller: the returned campaign is still open
    for it.

    Args:
        client: The Google Ads client.
        customer_id: Client account id, already normalised to digits.
        name: Campaign name; also seeds the budget name.
        daily_budget: Daily budget in account currency (micros internally).
        channel_type: AdvertisingChannelTypeEnum member name.
        status: CampaignStatusEnum member name.
        start_date: "YYYY-MM-DD" or a full date-time; omit for today.
        end_date: "YYYY-MM-DD" or a full date-time; omit for no end.
        tracking_url_template: Tracking template; MUST contain {lpurl}.
        final_url_suffix: Query string appended to final URLs.
        campaign_resource_name: Temporary resource name for the campaign,
            for callers that link assets to it in the same request (PMax).

    Returns:
        (budget_operation, campaign_operation, campaign): the two
        MutateOperations to send, and the campaign the caller finishes.
    """
    budget_temp_rn = f"customers/{customer_id}/campaignBudgets/-1"

    budget_op = client.get_type("MutateOperation")
    budget = budget_op.campaign_budget_operation.create
    budget.resource_name = budget_temp_rn
    budget.name = f"{name} budget {int(time.time())}"
    budget.amount_micros = _to_micros(daily_budget)
    budget.delivery_method = client.enums.BudgetDeliveryMethodEnum.STANDARD
    budget.explicitly_shared = False

    campaign_op = client.get_type("MutateOperation")
    campaign = campaign_op.campaign_operation.create
    if campaign_resource_name:
        campaign.resource_name = campaign_resource_name
    campaign.name = name
    if start_date:
        campaign.start_date_time = (
            start_date if " " in start_date else f"{start_date} 00:00:00"
        )
    if end_date:
        campaign.end_date_time = (
            end_date if " " in end_date else f"{end_date} 23:59:59"
        )
    campaign.campaign_budget = budget_temp_rn
    campaign.contains_eu_political_advertising = (
        client.enums.EuPoliticalAdvertisingStatusEnum.DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING
    )
    if tracking_url_template:
        if "{lpurl}" not in tracking_url_template:
            raise ToolError("tracking_url_template must contain {lpurl}")
        campaign.tracking_url_template = tracking_url_template
    if final_url_suffix:
        campaign.final_url_suffix = final_url_suffix
    campaign.status = client.enums.CampaignStatusEnum[status]
    campaign.advertising_channel_type = client.enums.AdvertisingChannelTypeEnum[
        channel_type
    ]
    return budget_op, campaign_op, campaign
