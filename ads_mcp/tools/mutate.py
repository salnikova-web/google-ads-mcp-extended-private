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

"""Write (mutate) tools for the Google Ads MCP server.

Write extension: adds campaign creation, status management and budget
updates on top of the read-only reporting tools.

Safety model: every tool accepts a ``confirm`` flag (default ``False``).
With ``confirm=False`` the request is sent with ``validate_only=True`` —
Google Ads fully validates the operation but changes nothing, and the tool
returns a preview. Call the tool again with ``confirm=True`` to apply.
Each preview reports what really happened in ``validated``: the two
conversion-goal tools validate only their first step remotely, and a few
tools elsewhere have no ``validate_only`` at all.
"""

import math
import time
from typing import Annotated, Any, Dict, List, Optional

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field
from google.ads.googleads.errors import GoogleAdsException
from google.api_core import protobuf_helpers

import ads_mcp.utils as utils

mutate_mcp = FastMCP("mutate")

_MICROS = 1_000_000

# Batch tools send one request per call, so the cap keeps a single request
# (and the approval prompt in front of it) reviewable.
_BATCH_MAX_ITEMS = 100

# An operation counts as applied only when the API hands back a resource
# name for it. partial_failure_error does not always pin a failure to an
# operation index (an error without an `operations` field path cannot be
# attributed, and a short results list has no entry to read), so "not in the
# failure map" alone would report an unapplied operation as succeeded.
_NO_RESULT_ERROR = (
    "no result returned for this operation - it was NOT applied; verify the "
    "current state with mutate_list_campaigns"
)

_WRITE_ANNOTATIONS = ToolAnnotations(readOnlyHint=False, destructiveHint=False)

# The two conversion-goal tools mutate in two steps: the goal-config-level
# switch is sent with validate_only, the per-category goal flips are not
# sent at all on a dry-run. Neither canned note in _preview_or_done fits, so
# both tools report the split explicitly.
_GOAL_FLIP_DRY_RUN_NOTE = (
    "DRY-RUN: only the goal-config-level switch was sent to Google Ads for "
    "validation (validate_only=true). The per-category conversion-goal "
    "flips were computed locally and NOT sent, so they are unvalidated and "
    "can still fail. Re-run the tool with confirm=true to apply."
)
_GOAL_FLIP_VALIDATION = {
    "goal_config_level": "validated remotely (validate_only=true)",
    "category_goal_flips": "previewed locally, NOT sent to Google Ads",
}

_ALLOWED_CHANNEL_TYPES = [
    "SEARCH",
    "DISPLAY",
    "SHOPPING",
    "VIDEO",
    "PERFORMANCE_MAX",
    "DEMAND_GEN",
]

_ALLOWED_BIDDING = [
    "MAXIMIZE_CONVERSIONS",
    "MAXIMIZE_CONVERSION_VALUE",
    "MAXIMIZE_CLICKS",
    "MANUAL_CPC",
]

_ALLOWED_CAMPAIGN_STATUSES = ["ENABLED", "PAUSED", "REMOVED"]

# Schema-only aliases: advertise the accepted values in tools/list via
# json_schema_extra while runtime validation stays the existing lax
# .upper() + explicit ToolError checks below (a true Literal would reject
# lowercase input that works today).
_CHANNEL_TYPE_ENUM = Annotated[
    str, Field(json_schema_extra={"enum": _ALLOWED_CHANNEL_TYPES})
]
_BIDDING_ENUM = Annotated[
    str, Field(json_schema_extra={"enum": _ALLOWED_BIDDING})
]
_STATUS_ENUM = Annotated[
    str, Field(json_schema_extra={"enum": ["PAUSED", "ENABLED"]})
]
_MATCH_TYPE_ENUM = Annotated[
    str, Field(json_schema_extra={"enum": ["EXACT", "PHRASE", "BROAD"]})
]


# Kept under this name: 12 write modules import it from here.
def _raise_tool_error(ex: GoogleAdsException) -> None:
    utils.raise_tool_error(ex)


def _to_micros(amount: float) -> int:
    return int(round(float(amount) * _MICROS))


def _clean_customer_id(customer_id: str) -> str:
    """Normalises a customer id to digits, rejecting anything else.

    Customer ids are spliced into resource names and query conditions, so a
    value that is not purely numeric must never get through.
    """
    return utils.gaql_id(str(customer_id).replace("-", "").strip())


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


def _check_batch_size(items: Any, param_name: str) -> None:
    """Rejects an empty or oversized batch before anything is built."""
    if not items:
        raise ToolError(f"{param_name} is empty")
    if len(items) > _BATCH_MAX_ITEMS:
        raise ToolError(
            f"{param_name} holds {len(items)} entries, but the batch tools "
            f"accept at most {_BATCH_MAX_ITEMS} per call. Split the work "
            "across several calls."
        )


def _partial_failure_errors(client, response) -> Dict[int, str]:
    """Maps operation index -> error message from partial_failure_error.

    Only meaningful on an apply (partial_failure=true); a dry-run is atomic,
    so its failures arrive as a GoogleAdsException instead. Sibling of the
    richer parser inside ``keywords_add``, which additionally digs out policy
    violation keys.
    """
    out: Dict[int, str] = {}
    pfe = getattr(response, "partial_failure_error", None)
    details = getattr(pfe, "details", None) if pfe else None
    if not details:
        return out
    failure_cls = type(client.get_type("GoogleAdsFailure"))
    for detail in details:
        failure = failure_cls.deserialize(detail.value)
        for error in failure.errors:
            idx = None
            for fpe in error.location.field_path_elements:
                if fpe.field_name == "operations":
                    idx = fpe.index
                    break
            if idx is None:
                continue
            message = error.message.strip()
            # One operation can collect several errors; keep them all,
            # otherwise the reported reason depends on iteration order.
            out[idx] = f"{out[idx]}; {message}" if idx in out else message
    return out


@mutate_mcp.tool(annotations=_WRITE_ANNOTATIONS)
def campaign_create(
    customer_id: str,
    name: str,
    daily_budget: float,
    channel_type: _CHANNEL_TYPE_ENUM = "SEARCH",
    bidding_strategy: _BIDDING_ENUM = "MAXIMIZE_CONVERSIONS",
    target_cpa: Optional[float] = None,
    target_roas: Optional[float] = None,
    tracking_url_template: Optional[str] = None,
    final_url_suffix: Optional[str] = None,
    status: _STATUS_ENUM = "PAUSED",
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    confirm: bool = False,
) -> Dict[str, Any]:
    """Create a campaign together with its own dedicated daily budget.

    WHEN TO USE: Search. Other channels: pmax_campaign_create,
    demandgen_campaign_create, shopping_campaign_create,
    display_campaign_create.
    PRECONDITIONS: the name must be free — check mutate_list_campaigns (a
    truncated list is not proof).
    SIDE EFFECTS: creates a budget AND a campaign, PAUSED unless
    status="ENABLED", on ACCOUNT-DEFAULT conversion goals — follow up
    with mutate_campaign_set_custom_conversion_goal.
    DRY-RUN: confirm=false (default) validates remotely, changes nothing;
    confirm=true applies.
    UNITS & IDS: money in account currency (micros internally).

    Args:
        customer_id: The client account id (digits only, no hyphens).
        name: Campaign name (must be unique within the account).
        daily_budget: Daily budget in account currency (50.0 = €50).
        channel_type: Advertising channel. For PERFORMANCE_MAX prefer
            pmax_campaign_create (merchant feed + PMax bidding); asset
            groups come from pmax_asset_group_create.
        bidding_strategy: Bidding strategy of the new campaign.
        target_cpa: Target CPA (only with MAXIMIZE_CONVERSIONS).
        target_roas: Target ROAS as a decimal, 3.5 = 350% (only with
            MAXIMIZE_CONVERSION_VALUE).
        tracking_url_template: Tracking template; MUST contain {lpurl}.
        final_url_suffix: Query string appended to final URLs, e.g.
            "utm_source=google&utm_medium=cpc".
        status: Status the campaign is created with.
        start_date: "YYYY-MM-DD" (dashes required), account timezone;
            defaults to today.
        end_date: "YYYY-MM-DD" (dashes required), inclusive; omit for none.
        confirm: False = dry-run preview (default), True = apply changes.
    """
    customer_id = _clean_customer_id(customer_id)
    channel_type = channel_type.upper()
    bidding_strategy = bidding_strategy.upper()
    status = status.upper()

    if channel_type not in _ALLOWED_CHANNEL_TYPES:
        raise ToolError(f"channel_type must be one of {_ALLOWED_CHANNEL_TYPES}")
    if bidding_strategy not in _ALLOWED_BIDDING:
        raise ToolError(f"bidding_strategy must be one of {_ALLOWED_BIDDING}")
    if status not in ("PAUSED", "ENABLED"):
        raise ToolError("status must be PAUSED or ENABLED")
    if daily_budget <= 0:
        raise ToolError("daily_budget must be positive")

    client = utils.get_googleads_client()
    ga_service = utils.get_googleads_service("GoogleAdsService")

    budget_temp_rn = f"customers/{customer_id}/campaignBudgets/-1"

    # Operation 1: the campaign budget.
    budget_mutate_op = client.get_type("MutateOperation")
    budget = budget_mutate_op.campaign_budget_operation.create
    budget.resource_name = budget_temp_rn
    budget.name = f"{name} budget {int(time.time())}"
    budget.amount_micros = _to_micros(daily_budget)
    budget.delivery_method = client.enums.BudgetDeliveryMethodEnum.STANDARD
    budget.explicitly_shared = False

    # Operation 2: the campaign itself.
    campaign_mutate_op = client.get_type("MutateOperation")
    campaign = campaign_mutate_op.campaign_operation.create
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

    if bidding_strategy == "MAXIMIZE_CONVERSIONS":
        if target_cpa is not None:
            campaign.maximize_conversions.target_cpa_micros = _to_micros(
                target_cpa
            )
        else:
            client.copy_from(
                campaign.maximize_conversions,
                client.get_type("MaximizeConversions"),
            )
    elif bidding_strategy == "MAXIMIZE_CONVERSION_VALUE":
        if target_roas is not None:
            campaign.maximize_conversion_value.target_roas = float(target_roas)
        else:
            client.copy_from(
                campaign.maximize_conversion_value,
                client.get_type("MaximizeConversionValue"),
            )
    elif bidding_strategy == "MAXIMIZE_CLICKS":
        client.copy_from(campaign.target_spend, client.get_type("TargetSpend"))
    elif bidding_strategy == "MANUAL_CPC":
        campaign.manual_cpc.enhanced_cpc_enabled = False

    if channel_type == "SEARCH":
        campaign.network_settings.target_google_search = True
        campaign.network_settings.target_search_network = True
        campaign.network_settings.target_content_network = False
        campaign.network_settings.target_partner_search_network = False

    request = client.get_type("MutateGoogleAdsRequest")
    request.customer_id = customer_id
    request.mutate_operations.extend([budget_mutate_op, campaign_mutate_op])
    request.validate_only = not confirm

    try:
        response = ga_service.mutate(request=request)
    except GoogleAdsException as ex:
        _raise_tool_error(ex)

    details: Dict[str, Any] = {
        "customer_id": customer_id,
        "campaign_name": name,
        "daily_budget": daily_budget,
        "channel_type": channel_type,
        "bidding_strategy": bidding_strategy,
        "status": status,
    }
    if confirm:
        details["created_resources"] = [
            r.campaign_budget_result.resource_name
            or r.campaign_result.resource_name
            for r in response.mutate_operation_responses
        ]
    return _preview_or_done(confirm, "campaign_create", details)


@mutate_mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True)
)
def campaign_update_status(
    customer_id: str,
    campaign_id: str,
    status: str,
    confirm: bool = False,
) -> Dict[str, Any]:
    """Pause, enable or remove ONE campaign.

    WHEN TO USE: one campaign, or any REMOVED. Three or more taking the
    same ENABLED/PAUSED: mutate_campaign_update_status_batch.
    PRECONDITIONS: the campaign must exist (mutate_list_campaigns).
    SIDE EFFECTS: serving changes at once. REMOVED is IRREVERSIBLE — a
    removed campaign can never be re-enabled.
    DRY-RUN: confirm=false (default) validates remotely, changes nothing;
    confirm=true applies.

    Args:
        customer_id: The client account id (digits only, no hyphens).
        campaign_id: The numeric id of the campaign.
        status: ENABLED, PAUSED or REMOVED.
        confirm: False = dry-run preview (default), True = apply changes.
    """
    customer_id = _clean_customer_id(customer_id)
    status = status.upper()
    if status not in ("ENABLED", "PAUSED", "REMOVED"):
        raise ToolError("status must be ENABLED, PAUSED or REMOVED")

    client = utils.get_googleads_client()
    campaign_service = utils.get_googleads_service("CampaignService")

    operation = client.get_type("CampaignOperation")
    resource_name = f"customers/{customer_id}/campaigns/{campaign_id}"
    if status == "REMOVED":
        operation.remove = resource_name
    else:
        campaign = operation.update
        campaign.resource_name = resource_name
        campaign.status = client.enums.CampaignStatusEnum[status]
        client.copy_from(
            operation.update_mask,
            protobuf_helpers.field_mask(None, campaign._pb),
        )

    request = client.get_type("MutateCampaignsRequest")
    request.customer_id = customer_id
    request.operations.append(operation)
    request.validate_only = not confirm

    try:
        response = campaign_service.mutate_campaigns(request=request)
    except GoogleAdsException as ex:
        _raise_tool_error(ex)

    details: Dict[str, Any] = {
        "customer_id": customer_id,
        "campaign_id": campaign_id,
        "new_status": status,
    }
    if confirm:
        details["updated_resource"] = response.results[0].resource_name
    return _preview_or_done(confirm, "campaign_update_status", details)


@mutate_mcp.tool(annotations=_WRITE_ANNOTATIONS)
def campaign_update_status_batch(
    customer_id: str,
    campaign_ids: List[str],
    status: _STATUS_ENUM,
    confirm: bool = False,
) -> Dict[str, Any]:
    """Pause or enable SEVERAL campaigns in one request.

    WHEN TO USE: three or more campaigns taking the same new status — one
    call, one approval. One campaign, or REMOVED (irreversible, refused
    here): mutate_campaign_update_status.
    PRECONDITIONS: ids must exist (mutate_list_campaigns). At most 100 per
    call; duplicates are dropped (first-seen order) and reported.
    SIDE EFFECTS: sets status on every listed campaign, nothing else.
    DRY-RUN: confirm=false (default) validates the batch remotely and
    ATOMICALLY, so one bad id fails the whole preview. confirm=true
    applies with partial_failure=true, so some campaigns can succeed
    while others fail — the result reports requested/succeeded/failed
    plus a per-campaign breakdown.

    Args:
        customer_id: The client account id (digits only, no hyphens).
        campaign_ids: Numeric campaign ids, up to 100.
        status: Applied to every listed campaign.
        confirm: False = dry-run preview (default), True = apply changes.
    """
    customer_id = _clean_customer_id(customer_id)
    status = status.upper()
    if status == "REMOVED":
        raise ToolError(
            "status REMOVED is not available in the batch tool: removing a "
            "campaign is irreversible, so it must be done one campaign at a "
            "time with mutate_campaign_update_status."
        )
    if status not in ("ENABLED", "PAUSED"):
        raise ToolError("status must be ENABLED or PAUSED")
    _check_batch_size(campaign_ids, "campaign_ids")

    ordered_ids: List[str] = []
    seen = set()
    duplicates: List[str] = []
    for raw_id in campaign_ids:
        campaign_id = utils.gaql_id(raw_id)
        if campaign_id in seen:
            if campaign_id not in duplicates:
                duplicates.append(campaign_id)
            continue
        seen.add(campaign_id)
        ordered_ids.append(campaign_id)

    client = utils.get_googleads_client()
    campaign_service = utils.get_googleads_service("CampaignService")

    request = client.get_type("MutateCampaignsRequest")
    request.customer_id = customer_id
    # The API rejects validate_only together with partial_failure: the
    # dry-run is atomic, the apply is per-operation.
    request.validate_only = not confirm
    request.partial_failure = bool(confirm)

    resource_names = [
        f"customers/{customer_id}/campaigns/{campaign_id}"
        for campaign_id in ordered_ids
    ]
    for resource_name in resource_names:
        operation = client.get_type("CampaignOperation")
        campaign = operation.update
        campaign.resource_name = resource_name
        campaign.status = client.enums.CampaignStatusEnum[status]
        # Explicit leaf path: a value-derived mask would drop the status of
        # whichever enum value happens to be the proto default.
        operation.update_mask.paths.append("status")
        request.operations.append(operation)

    try:
        response = campaign_service.mutate_campaigns(request=request)
    except GoogleAdsException as ex:
        _raise_tool_error(ex)

    details: Dict[str, Any] = {
        "customer_id": customer_id,
        "new_status": status,
        "requested": len(ordered_ids),
        "campaigns": [
            {
                "campaign_id": campaign_id,
                "resource_name": resource_name,
                "new_status": status,
            }
            for campaign_id, resource_name in zip(ordered_ids, resource_names)
        ],
    }
    if duplicates:
        details["duplicate_campaign_ids_ignored"] = duplicates
        details["warning"] = (
            "campaign_ids contained duplicate ids; each campaign is updated "
            "once."
        )

    if confirm:
        failures = _partial_failure_errors(client, response)
        results = list(response.results)
        succeeded: List[Dict[str, Any]] = []
        failed: List[Dict[str, Any]] = []
        for index, campaign_id in enumerate(ordered_ids):
            if index in failures:
                failed.append(
                    {"campaign_id": campaign_id, "error": failures[index]}
                )
                continue
            resource_name = (
                results[index].resource_name if index < len(results) else ""
            )
            if not resource_name:
                failed.append(
                    {"campaign_id": campaign_id, "error": _NO_RESULT_ERROR}
                )
                continue
            succeeded.append(
                {"campaign_id": campaign_id, "resource_name": resource_name}
            )
        details["succeeded"] = len(succeeded)
        details["failed"] = len(failed)
        details["succeeded_campaigns"] = succeeded
        details["failed_campaigns"] = failed
    return _preview_or_done(confirm, "campaign_update_status_batch", details)


@mutate_mcp.tool(annotations=_WRITE_ANNOTATIONS)
def campaign_set_target_roas(
    customer_id: str,
    campaign_id: str,
    target_roas: float,
    confirm: bool = False,
) -> Dict[str, Any]:
    """Set Target ROAS on a Maximize Conversion Value campaign.

    WHEN TO USE: after creation, above all Standard Shopping (tROAS is
    often refused at create time). PMax: pmax_campaign_update_bidding.
    PRECONDITIONS: the campaign must already bid on
    MAXIMIZE_CONVERSION_VALUE, or the API rejects the update.
    SIDE EFFECTS: replaces the tROAS target, no other bidding field.
    DRY-RUN: confirm=false (default) validates remotely, changes nothing;
    confirm=true applies.
    UNITS & IDS: target_roas is a decimal, 1.7 = 170%.

    Args:
        customer_id: The client account id (digits only, no hyphens).
        campaign_id: The numeric id of the campaign.
        target_roas: Target return on ad spend as a decimal.
        confirm: False = dry-run preview (default), True = apply changes.
    """
    from google.protobuf import field_mask_pb2

    customer_id = _clean_customer_id(customer_id)
    client = utils.get_googleads_client()
    campaign_service = utils.get_googleads_service("CampaignService")

    operation = client.get_type("CampaignOperation")
    campaign = operation.update
    campaign.resource_name = f"customers/{customer_id}/campaigns/{campaign_id}"
    campaign.maximize_conversion_value.target_roas = float(target_roas)
    client.copy_from(
        operation.update_mask,
        field_mask_pb2.FieldMask(
            paths=["maximize_conversion_value.target_roas"]
        ),
    )
    request = client.get_type("MutateCampaignsRequest")
    request.customer_id = customer_id
    request.operations.append(operation)
    request.validate_only = not confirm
    try:
        response = campaign_service.mutate_campaigns(request=request)
    except GoogleAdsException as ex:
        _raise_tool_error(ex)

    details: Dict[str, Any] = {
        "customer_id": customer_id,
        "campaign_id": str(campaign_id),
        "target_roas": float(target_roas),
    }
    if confirm:
        details["updated_resource"] = response.results[0].resource_name
    return _preview_or_done(confirm, "campaign_set_target_roas", details)


@mutate_mcp.tool(annotations=_WRITE_ANNOTATIONS)
def campaign_set_merchant(
    customer_id: str,
    campaign_id: str,
    merchant_id: str,
    feed_label: Optional[str] = None,
    confirm: bool = False,
) -> Dict[str, Any]:
    """Link a Merchant Center feed to an existing campaign (PMax/Shopping).

    WHEN TO USE: a PMax or Shopping campaign with no feed yet. On PMax the
    Merchant Center id is often IMMUTABLE after creation — if the API
    rejects this, recreate with pmax_campaign_create(merchant_id=...).
    PRECONDITIONS: the Merchant Center account must already be linked to
    the Google Ads account.
    SIDE EFFECTS: sets merchant_id (and feed_label when given); products
    start serving from that feed.
    DRY-RUN: confirm=false (default) validates remotely, changes nothing;
    confirm=true applies.

    Args:
        customer_id: The client account id (digits only, no hyphens).
        campaign_id: The numeric id of the campaign.
        merchant_id: The Merchant Center account id (digits only).
        feed_label: Country/label filter; omit to use the whole feed.
        confirm: False = dry-run preview (default), True = apply changes.
    """
    customer_id = _clean_customer_id(customer_id)
    client = utils.get_googleads_client()
    campaign_service = utils.get_googleads_service("CampaignService")

    operation = client.get_type("CampaignOperation")
    campaign = operation.update
    campaign.resource_name = f"customers/{customer_id}/campaigns/{campaign_id}"
    campaign.shopping_setting.merchant_id = int(merchant_id)
    paths = ["shopping_setting.merchant_id"]
    if feed_label:
        campaign.shopping_setting.feed_label = feed_label
        paths.append("shopping_setting.feed_label")
    operation.update_mask.paths.extend(paths)

    request = client.get_type("MutateCampaignsRequest")
    request.customer_id = customer_id
    request.operations.append(operation)
    request.validate_only = not confirm
    try:
        response = campaign_service.mutate_campaigns(request=request)
    except GoogleAdsException as ex:
        _raise_tool_error(ex)

    details: Dict[str, Any] = {
        "customer_id": customer_id,
        "campaign_id": str(campaign_id),
        "merchant_id": str(merchant_id),
        "feed_label": feed_label,
    }
    if confirm:
        details["updated_resource"] = response.results[0].resource_name
    return _preview_or_done(confirm, "campaign_set_merchant", details)


@mutate_mcp.tool(annotations=_WRITE_ANNOTATIONS)
def campaign_update_settings(
    customer_id: str,
    campaign_id: str,
    target_google_search: Optional[bool] = None,
    target_search_network: Optional[bool] = None,
    target_content_network: Optional[bool] = None,
    positive_geo_target_type: Optional[str] = None,
    enable_ai_max: Optional[bool] = None,
    text_customization: Optional[bool] = None,
    final_url_expansion: Optional[bool] = None,
    image_enhancement: Optional[bool] = None,
    image_extraction: Optional[bool] = None,
    video_enhancement: Optional[bool] = None,
    confirm: bool = False,
) -> Dict[str, Any]:
    """Update campaign network settings, geo target type and/or AI Max.

    WHEN TO USE: network reach, geo match type, AI Max and asset
    automation. The Demand Gen "Asset optimization" toggles are NOT
    campaign-level — use demandgen_ad_update_asset_optimization, or set
    them at creation via demandgen_ad_create_video.
    PRECONDITIONS: pass at least one setting or the call is refused; the
    asset-automation toggles first READ the campaign's current settings.
    SIDE EFFECTS: only the fields you pass change. Turning a network off
    can cut reach sharply.
    DRY-RUN: confirm=false (default) validates remotely, changes nothing;
    confirm=true applies.

    Args:
        customer_id: The client account id (digits only, no hyphens).
        campaign_id: The numeric id of the campaign.
        target_google_search: Serve on Google search results.
        target_search_network: Serve on search partners; False turns them
            OFF.
        target_content_network: Serve on the Display network.
        positive_geo_target_type: PRESENCE (people in the location) or
            PRESENCE_OR_INTEREST (also people interested in it).
        enable_ai_max: Toggle AI Max for Search.
        text_customization: AI Max sub-setting: Google may customise ad
            text (asset automation OPTED_IN/OPTED_OUT).
        final_url_expansion: AI Max sub-setting: Google may send traffic to
            other pages of the site.
        image_enhancement: Google may enhance campaign images.
        image_extraction: Google may pull images from the landing page.
        video_enhancement: Google may generate enhanced YouTube videos.
        confirm: False = dry-run preview (default), True = apply changes.
    """
    customer_id = _clean_customer_id(customer_id)

    client = utils.get_googleads_client()
    campaign_service = utils.get_googleads_service("CampaignService")

    operation = client.get_type("CampaignOperation")
    campaign = operation.update
    campaign.resource_name = f"customers/{customer_id}/campaigns/{campaign_id}"

    paths = []
    if target_google_search is not None:
        campaign.network_settings.target_google_search = target_google_search
        paths.append("network_settings.target_google_search")
    if target_search_network is not None:
        campaign.network_settings.target_search_network = target_search_network
        paths.append("network_settings.target_search_network")
    if target_content_network is not None:
        campaign.network_settings.target_content_network = (
            target_content_network
        )
        paths.append("network_settings.target_content_network")
    if positive_geo_target_type is not None:
        geo = positive_geo_target_type.upper()
        if geo not in ("PRESENCE", "PRESENCE_OR_INTEREST"):
            raise ToolError(
                "positive_geo_target_type must be PRESENCE or PRESENCE_OR_INTEREST"
            )
        campaign.geo_target_type_setting.positive_geo_target_type = (
            client.enums.PositiveGeoTargetTypeEnum[geo]
        )
        paths.append("geo_target_type_setting.positive_geo_target_type")
    if enable_ai_max is not None:
        campaign.ai_max_setting.enable_ai_max = enable_ai_max
        paths.append("ai_max_setting.enable_ai_max")
    if any(
        x is not None
        for x in (
            text_customization,
            final_url_expansion,
            image_enhancement,
            image_extraction,
            video_enhancement,
        )
    ):
        ga_service = utils.get_googleads_service("GoogleAdsService")
        try:
            rows = list(
                ga_service.search(
                    customer_id=customer_id,
                    query=(
                        "SELECT campaign.asset_automation_settings FROM "
                        "campaign "
                        f"WHERE campaign.id = {int(campaign_id)}"
                    ),
                )
            )
        except GoogleAdsException as ex:
            _raise_tool_error(ex)
        current: Dict[int, int] = {}
        for row in rows:
            for s in row.campaign.asset_automation_settings:
                current[int(s.asset_automation_type)] = int(
                    s.asset_automation_status
                )
        t_enum = client.enums.AssetAutomationTypeEnum
        s_enum = client.enums.AssetAutomationStatusEnum

        def _set(auto_type, flag):
            current[int(auto_type)] = int(
                s_enum.OPTED_IN if flag else s_enum.OPTED_OUT
            )

        if text_customization is not None:
            _set(t_enum.TEXT_ASSET_AUTOMATION, text_customization)
        if final_url_expansion is not None:
            _set(
                t_enum.FINAL_URL_EXPANSION_TEXT_ASSET_AUTOMATION,
                final_url_expansion,
            )
        if image_enhancement is not None:
            _set(t_enum.GENERATE_IMAGE_ENHANCEMENT, image_enhancement)
        if image_extraction is not None:
            _set(t_enum.GENERATE_IMAGE_EXTRACTION, image_extraction)
        if video_enhancement is not None:
            _set(t_enum.GENERATE_ENHANCED_YOUTUBE_VIDEOS, video_enhancement)
        setting_cls = type(campaign).AssetAutomationSetting
        for t, s in sorted(current.items()):
            campaign.asset_automation_settings.append(
                setting_cls(asset_automation_type=t, asset_automation_status=s)
            )
        paths.append("asset_automation_settings")
    if not paths:
        raise ToolError("Pass at least one setting to update")
    operation.update_mask.paths.extend(paths)

    request = client.get_type("MutateCampaignsRequest")
    request.customer_id = customer_id
    request.operations.append(operation)
    request.validate_only = not confirm

    try:
        response = campaign_service.mutate_campaigns(request=request)
    except GoogleAdsException as ex:
        _raise_tool_error(ex)

    details: Dict[str, Any] = {
        "customer_id": customer_id,
        "campaign_id": str(campaign_id),
        "updated_fields": paths,
    }
    if confirm:
        details["updated_resource"] = response.results[0].resource_name
    return _preview_or_done(confirm, "campaign_update_settings", details)


@mutate_mcp.tool(annotations=_WRITE_ANNOTATIONS)
def campaign_rename(
    customer_id: str,
    campaign_id: str,
    name: str,
    confirm: bool = False,
) -> Dict[str, Any]:
    """Rename an existing campaign (changes campaign.name only).

    WHEN TO USE: renaming only; budget/status/settings have their own
    mutate_campaign_* tools.
    PRECONDITIONS: the campaign must exist (its current name is read first)
    and the new name must be free — names are unique per account and a
    duplicate is rejected. Check mutate_list_campaigns.
    SIDE EFFECTS: only campaign.name changes; the preview shows old vs new.
    DRY-RUN: confirm=false (default) validates remotely, changes nothing;
    confirm=true applies.

    Args:
        customer_id: The client account id (digits only, no hyphens).
        campaign_id: The numeric id of the campaign to rename.
        name: The new campaign name (non-empty).
        confirm: False = dry-run preview (default), True = apply.
    """
    customer_id = _clean_customer_id(customer_id)
    new_name = (name or "").strip()
    if not new_name:
        raise ToolError("name must be a non-empty string")

    client = utils.get_googleads_client()
    campaign_service = utils.get_googleads_service("CampaignService")
    ga_service = utils.get_googleads_service("GoogleAdsService")

    old_name = None
    try:
        for row in ga_service.search(
            customer_id=customer_id,
            query=(
                "SELECT campaign.name FROM campaign "
                f"WHERE campaign.id = {int(campaign_id)}"
            ),
        ):
            old_name = row.campaign.name
    except GoogleAdsException as ex:
        _raise_tool_error(ex)
    if old_name is None:
        raise ToolError(f"Campaign {campaign_id} not found in {customer_id}")

    operation = client.get_type("CampaignOperation")
    campaign = operation.update
    campaign.resource_name = f"customers/{customer_id}/campaigns/{campaign_id}"
    campaign.name = new_name
    operation.update_mask.paths.append("name")

    request = client.get_type("MutateCampaignsRequest")
    request.customer_id = customer_id
    request.operations.append(operation)
    request.validate_only = not confirm

    try:
        response = campaign_service.mutate_campaigns(request=request)
    except GoogleAdsException as ex:
        _raise_tool_error(ex)

    details: Dict[str, Any] = {
        "customer_id": customer_id,
        "campaign_id": str(campaign_id),
        "old_name": old_name,
        "new_name": new_name,
    }
    if confirm:
        details["updated_resource"] = response.results[0].resource_name
    return _preview_or_done(confirm, "campaign_rename", details)


@mutate_mcp.tool(annotations=_WRITE_ANNOTATIONS)
def campaign_budget_update(
    customer_id: str,
    campaign_id: str,
    new_daily_budget: float,
    confirm: bool = False,
) -> Dict[str, Any]:
    """Change the daily budget of ONE existing campaign.

    WHEN TO USE: one campaign. Three or more:
    mutate_campaign_budget_update_batch (one call, one approval).
    PRECONDITIONS: the campaign must exist — its budget is resolved first
    and an unknown id fails. Find ids with mutate_list_campaigns.
    SIDE EFFECTS: the update lands on the BUDGET, so a shared budget
    (explicitly_shared=true) changes every campaign on it; the preview
    warns and shows current vs new amount.
    DRY-RUN: confirm=false (default) validates remotely, changes nothing;
    confirm=true applies.
    UNITS & IDS: money in account currency (micros internally).

    Args:
        customer_id: The client account id (digits only, no hyphens).
        campaign_id: The numeric id of the campaign.
        new_daily_budget: New daily budget in account currency (75.0 = €75).
        confirm: False = dry-run preview (default), True = apply changes.
    """
    customer_id = _clean_customer_id(customer_id)
    if new_daily_budget <= 0:
        raise ToolError("new_daily_budget must be positive")

    client = utils.get_googleads_client()
    ga_service = utils.get_googleads_service("GoogleAdsService")

    query = (
        "SELECT campaign.id, campaign.name, campaign.campaign_budget, "
        "campaign_budget.id, campaign_budget.amount_micros, "
        "campaign_budget.explicitly_shared "
        f"FROM campaign WHERE campaign.id = {int(campaign_id)}"
    )
    try:
        rows = list(ga_service.search(customer_id=customer_id, query=query))
    except GoogleAdsException as ex:
        _raise_tool_error(ex)

    if not rows:
        raise ToolError(
            f"Campaign {campaign_id} not found in account {customer_id}"
        )

    row = rows[0]
    budget_resource_name = row.campaign.campaign_budget
    current_amount = row.campaign_budget.amount_micros / _MICROS
    is_shared = bool(row.campaign_budget.explicitly_shared)

    budget_service = utils.get_googleads_service("CampaignBudgetService")
    operation = client.get_type("CampaignBudgetOperation")
    budget = operation.update
    budget.resource_name = budget_resource_name
    budget.amount_micros = _to_micros(new_daily_budget)
    client.copy_from(
        operation.update_mask,
        protobuf_helpers.field_mask(None, budget._pb),
    )

    request = client.get_type("MutateCampaignBudgetsRequest")
    request.customer_id = customer_id
    request.operations.append(operation)
    request.validate_only = not confirm

    try:
        response = budget_service.mutate_campaign_budgets(request=request)
    except GoogleAdsException as ex:
        _raise_tool_error(ex)

    details: Dict[str, Any] = {
        "customer_id": customer_id,
        "campaign_id": str(campaign_id),
        "campaign_name": row.campaign.name,
        "current_daily_budget": current_amount,
        "new_daily_budget": new_daily_budget,
        "budget_is_shared": is_shared,
    }
    if is_shared:
        details["warning"] = (
            "This budget is SHARED: changing it affects every campaign "
            "attached to it."
        )
    if confirm:
        details["updated_resource"] = response.results[0].resource_name
    return _preview_or_done(confirm, "campaign_budget_update", details)


@mutate_mcp.tool(annotations=_WRITE_ANNOTATIONS)
def campaign_budget_update_batch(
    customer_id: str,
    updates: List[Dict[str, Any]],
    confirm: bool = False,
) -> Dict[str, Any]:
    """Change the daily budget of SEVERAL campaigns in one request.

    WHEN TO USE: three or more campaigns needing a new budget — one call,
    one approval. One campaign: mutate_campaign_budget_update.
    PRECONDITIONS: every id is resolved first, so an unknown id fails the
    whole call before anything is written (mutate_list_campaigns). At
    most 100 entries; duplicate campaign_ids are rejected.
    SIDE EFFECTS: the update lands on the BUDGET, not the campaign, so a
    shared budget changes every campaign on it — each affected row warns.
    Two campaigns on one shared budget collapse into one operation at the
    same amount, and are rejected at different amounts.
    DRY-RUN: confirm=false (default) validates the batch remotely and
    ATOMICALLY, so nothing changes. confirm=true applies with
    partial_failure=true, so some budgets can succeed while others fail —
    the result reports requested/succeeded/failed plus a per-campaign
    breakdown.
    UNITS & IDS: money in account currency (75.0 = 75 EUR on a EUR
    account, micros internally).

    Args:
        customer_id: The client account id (digits only, no hyphens).
        updates: Up to 100 objects shaped
            {"campaign_id": "123", "new_daily_budget": 75.0}; the budget
            must be a positive number in account currency.
        confirm: False = dry-run preview (default), True = apply changes.
    """
    customer_id = _clean_customer_id(customer_id)
    _check_batch_size(updates, "updates")

    requested: List[Dict[str, Any]] = []
    seen: Dict[str, int] = {}
    for index, entry in enumerate(updates):
        if not isinstance(entry, dict):
            raise ToolError(
                f"updates[{index}] must be an object with campaign_id and "
                "new_daily_budget"
            )
        campaign_id = str(entry.get("campaign_id", "")).strip()
        if not campaign_id:
            raise ToolError(f"updates[{index}] is missing campaign_id")
        # isascii() as well as isdigit(): the latter also accepts non-ASCII
        # digits, which gaql_id rejects — but with a message that no longer
        # names the offending entry.
        if not (campaign_id.isascii() and campaign_id.isdigit()):
            raise ToolError(
                f"updates[{index}]: campaign_id must be a numeric campaign "
                f"id, got {entry.get('campaign_id')!r}"
            )
        campaign_id = utils.gaql_id(campaign_id)
        raw_amount = entry.get("new_daily_budget")
        if raw_amount is None:
            raise ToolError(f"updates[{index}] is missing new_daily_budget")
        # bool is an int subclass: True would silently become a 1.0 budget.
        if isinstance(raw_amount, bool):
            raise ToolError(
                f"updates[{index}]: new_daily_budget must be a number, got "
                f"{raw_amount!r}"
            )
        try:
            amount = float(raw_amount)
        except (TypeError, ValueError):
            raise ToolError(
                f"updates[{index}]: new_daily_budget must be a number, got "
                f"{raw_amount!r}"
            )
        # isfinite rules out NaN and infinity, which pass a plain "> 0"
        # check and then blow up inside _to_micros as a bare ValueError.
        if not math.isfinite(amount) or amount <= 0:
            raise ToolError(
                f"updates[{index}]: new_daily_budget must be a positive "
                f"number, got {raw_amount!r}"
            )
        if campaign_id in seen:
            raise ToolError(
                f"updates[{index}] repeats campaign_id {campaign_id} (first "
                f"seen at updates[{seen[campaign_id]}]). Send one budget per "
                "campaign."
            )
        seen[campaign_id] = index
        requested.append({"campaign_id": campaign_id, "amount": amount})

    client = utils.get_googleads_client()
    ga_service = utils.get_googleads_service("GoogleAdsService")

    ids_csv = ", ".join(item["campaign_id"] for item in requested)
    query = (
        "SELECT campaign.id, campaign.name, campaign.campaign_budget, "
        "campaign_budget.explicitly_shared, campaign_budget.amount_micros "
        f"FROM campaign WHERE campaign.id IN ({ids_csv})"
    )
    try:
        rows = {
            str(row.campaign.id): row
            for row in ga_service.search(customer_id=customer_id, query=query)
        }
    except GoogleAdsException as ex:
        _raise_tool_error(ex)

    missing = [
        item["campaign_id"]
        for item in requested
        if item["campaign_id"] not in rows
    ]
    if missing:
        raise ToolError(
            f"Campaigns not found in account {customer_id}: "
            f"{', '.join(missing)}. Nothing was changed."
        )

    # One operation per BUDGET resource, not per campaign: two campaigns on
    # one shared budget would otherwise send two operations against the same
    # resource in a single request.
    budgets: Dict[str, Dict[str, Any]] = {}
    rows_out: List[Dict[str, Any]] = []
    for item in requested:
        campaign_id = item["campaign_id"]
        row = rows[campaign_id]
        budget_resource = str(row.campaign.campaign_budget)
        new_micros = _to_micros(item["amount"])
        is_shared = bool(row.campaign_budget.explicitly_shared)
        group = budgets.get(budget_resource)
        if group is None:
            budgets[budget_resource] = {
                "amount_micros": new_micros,
                "campaign_ids": [campaign_id],
            }
        elif group["amount_micros"] != new_micros:
            other = ", ".join(group["campaign_ids"])
            raise ToolError(
                f"Campaigns {other} and {campaign_id} share campaign budget "
                f"{budget_resource}, but the request sets two different "
                f"amounts ({group['amount_micros'] / _MICROS} and "
                f"{item['amount']}). One budget can only hold one amount — "
                "send a single amount for the shared budget, or move a "
                "campaign onto its own budget first. Nothing was changed."
            )
        else:
            group["campaign_ids"].append(campaign_id)
        entry: Dict[str, Any] = {
            "campaign_id": campaign_id,
            "campaign_name": row.campaign.name,
            "budget_resource": budget_resource,
            "old_amount_micros": int(row.campaign_budget.amount_micros),
            "new_amount_micros": new_micros,
            "new_daily_budget": item["amount"],
            "shared": is_shared,
        }
        if is_shared:
            entry["warning"] = (
                "shared budget - the change affects every campaign using it"
            )
        rows_out.append(entry)

    budget_service = utils.get_googleads_service("CampaignBudgetService")
    request = client.get_type("MutateCampaignBudgetsRequest")
    request.customer_id = customer_id
    # The API rejects validate_only together with partial_failure: the
    # dry-run is atomic, the apply is per-operation.
    request.validate_only = not confirm
    request.partial_failure = bool(confirm)

    operation_budgets = list(budgets)
    for budget_resource in operation_budgets:
        operation = client.get_type("CampaignBudgetOperation")
        budget = operation.update
        budget.resource_name = budget_resource
        budget.amount_micros = budgets[budget_resource]["amount_micros"]
        # Explicit leaf path: a value-derived mask cannot express "only the
        # amount changed" reliably.
        operation.update_mask.paths.append("amount_micros")
        request.operations.append(operation)

    try:
        response = budget_service.mutate_campaign_budgets(request=request)
    except GoogleAdsException as ex:
        _raise_tool_error(ex)

    details: Dict[str, Any] = {
        "customer_id": customer_id,
        "requested": len(requested),
        "budget_operations": len(operation_budgets),
        "budgets": rows_out,
    }
    collapsed = [
        {
            "budget_resource": budget_resource,
            "campaign_ids": group["campaign_ids"],
            "new_daily_budget": group["amount_micros"] / _MICROS,
        }
        for budget_resource, group in budgets.items()
        if len(group["campaign_ids"]) > 1
    ]
    if collapsed:
        details["shared_budget_collapsed"] = collapsed
    if any(entry["shared"] for entry in rows_out):
        details["warning"] = (
            "Some of these budgets are SHARED: changing one affects every "
            "campaign attached to it, including campaigns not listed here."
        )

    if confirm:
        failures = _partial_failure_errors(client, response)
        results = list(response.results)
        succeeded: List[Dict[str, Any]] = []
        failed: List[Dict[str, Any]] = []
        for index, budget_resource in enumerate(operation_budgets):
            group = budgets[budget_resource]
            if index in failures:
                failed.extend(
                    {
                        "campaign_id": campaign_id,
                        "budget_resource": budget_resource,
                        "error": failures[index],
                    }
                    for campaign_id in group["campaign_ids"]
                )
                continue
            resource_name = (
                results[index].resource_name if index < len(results) else ""
            )
            if not resource_name:
                failed.extend(
                    {
                        "campaign_id": campaign_id,
                        "budget_resource": budget_resource,
                        "error": _NO_RESULT_ERROR,
                    }
                    for campaign_id in group["campaign_ids"]
                )
                continue
            succeeded.extend(
                {
                    "campaign_id": campaign_id,
                    "budget_resource": budget_resource,
                    "resource_name": resource_name,
                }
                for campaign_id in group["campaign_ids"]
            )
        details["succeeded"] = len(succeeded)
        details["failed"] = len(failed)
        details["succeeded_campaigns"] = succeeded
        details["failed_campaigns"] = failed
    return _preview_or_done(confirm, "campaign_budget_update_batch", details)


@mutate_mcp.tool(annotations=_WRITE_ANNOTATIONS)
def ad_group_create(
    customer_id: str,
    campaign_id: str,
    name: str,
    cpc_bid: Optional[float] = None,
    status: _STATUS_ENUM = "PAUSED",
    confirm: bool = False,
) -> Dict[str, Any]:
    """Create a SEARCH_STANDARD ad group inside an existing campaign.

    WHEN TO USE: Search only. Other channels: demandgen_ad_group_create,
    display_ad_group_create, shopping_ad_group_create,
    video_ad_group_create; PMax has no ad groups (pmax_asset_group_create).
    PRECONDITIONS: the parent campaign must exist
    (mutate_list_campaigns) and the name must be free within it.
    SIDE EFFECTS: created PAUSED unless status="ENABLED", and serves
    nothing until mutate_keywords_add and mutate_ad_create_rsa run.
    DRY-RUN: confirm=false (default) validates remotely, changes nothing;
    confirm=true applies.
    UNITS & IDS: money in account currency (micros internally).

    Args:
        customer_id: The client account id (digits only, no hyphens).
        campaign_id: The numeric id of the parent campaign.
        name: Ad group name (unique within the campaign).
        cpc_bid: Max CPC bid in account currency (1.5 = €1.50); ignored by
            Smart Bidding campaigns.
        status: Status the ad group is created with.
        confirm: False = dry-run preview (default), True = apply changes.
    """
    customer_id = _clean_customer_id(customer_id)
    status = status.upper()
    if status not in ("PAUSED", "ENABLED"):
        raise ToolError("status must be PAUSED or ENABLED")

    client = utils.get_googleads_client()
    ad_group_service = utils.get_googleads_service("AdGroupService")

    operation = client.get_type("AdGroupOperation")
    ad_group = operation.create
    ad_group.name = name
    ad_group.campaign = f"customers/{customer_id}/campaigns/{campaign_id}"
    ad_group.status = client.enums.AdGroupStatusEnum[status]
    ad_group.type_ = client.enums.AdGroupTypeEnum.SEARCH_STANDARD
    if cpc_bid is not None:
        ad_group.cpc_bid_micros = _to_micros(cpc_bid)

    request = client.get_type("MutateAdGroupsRequest")
    request.customer_id = customer_id
    request.operations.append(operation)
    request.validate_only = not confirm

    try:
        response = ad_group_service.mutate_ad_groups(request=request)
    except GoogleAdsException as ex:
        _raise_tool_error(ex)

    details: Dict[str, Any] = {
        "customer_id": customer_id,
        "campaign_id": str(campaign_id),
        "ad_group_name": name,
        "cpc_bid": cpc_bid,
        "status": status,
    }
    if confirm:
        details["created_resource"] = response.results[0].resource_name
    return _preview_or_done(confirm, "ad_group_create", details)


@mutate_mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True)
)
def ad_group_update(
    customer_id: str,
    ad_group_id: str,
    status: Optional[str] = None,
    cpc_bid: Optional[float] = None,
    new_name: Optional[str] = None,
    confirm: bool = False,
) -> Dict[str, Any]:
    """Update an existing ad group: status, max CPC bid and/or name.

    WHEN TO USE: any ad group. Targeting mode lives in
    targeting_set_ad_group_target_restrictions instead.
    PRECONDITIONS: pass at least one of status, cpc_bid, new_name — an
    empty update is refused.
    SIDE EFFECTS: only the fields you pass are written. status="REMOVED"
    REMOVES the ad group and is IRREVERSIBLE.
    DRY-RUN: confirm=false (default) validates remotely, changes nothing;
    confirm=true applies.
    UNITS & IDS: money in account currency (micros internally).

    Args:
        customer_id: The client account id (digits only, no hyphens).
        ad_group_id: The numeric id of the ad group.
        status: Optional new status: ENABLED, PAUSED or REMOVED.
        cpc_bid: Optional new max CPC bid in account currency (positive).
        new_name: Optional new name (must not be blank).
        confirm: False = dry-run preview (default), True = apply changes.
    """
    customer_id = _clean_customer_id(customer_id)
    if status is None and cpc_bid is None and new_name is None:
        raise ToolError(
            "Nothing to update: pass at least one of status, cpc_bid, new_name"
        )
    if cpc_bid is not None and cpc_bid <= 0:
        raise ToolError("cpc_bid must be positive")
    if new_name is not None:
        new_name = new_name.strip()
        if not new_name:
            raise ToolError("new_name must be a non-empty string")

    client = utils.get_googleads_client()
    ad_group_service = utils.get_googleads_service("AdGroupService")

    operation = client.get_type("AdGroupOperation")
    if status is not None and status.upper() == "REMOVED":
        operation.remove = f"customers/{customer_id}/adGroups/{ad_group_id}"
        request = client.get_type("MutateAdGroupsRequest")
        request.customer_id = customer_id
        request.operations.append(operation)
        request.validate_only = not confirm
        try:
            response = ad_group_service.mutate_ad_groups(request=request)
        except GoogleAdsException as ex:
            _raise_tool_error(ex)
        details_removed: Dict[str, Any] = {
            "customer_id": customer_id,
            "ad_group_id": str(ad_group_id),
            "new_status": "REMOVED",
        }
        if confirm:
            details_removed["removed_resource"] = response.results[
                0
            ].resource_name
        return _preview_or_done(confirm, "ad_group_update", details_removed)
    ad_group = operation.update
    ad_group.resource_name = f"customers/{customer_id}/adGroups/{ad_group_id}"
    # Each path is appended inside its own branch: a field the caller did
    # not pass must never reach the mask, or the update would clear it.
    paths = []
    if status is not None:
        status = status.upper()
        if status not in ("ENABLED", "PAUSED"):
            raise ToolError("status must be ENABLED, PAUSED or REMOVED")
        ad_group.status = client.enums.AdGroupStatusEnum[status]
        paths.append("status")
    if cpc_bid is not None:
        ad_group.cpc_bid_micros = _to_micros(cpc_bid)
        paths.append("cpc_bid_micros")
    if new_name is not None:
        ad_group.name = new_name
        paths.append("name")
    operation.update_mask.paths.extend(paths)

    request = client.get_type("MutateAdGroupsRequest")
    request.customer_id = customer_id
    request.operations.append(operation)
    request.validate_only = not confirm

    try:
        response = ad_group_service.mutate_ad_groups(request=request)
    except GoogleAdsException as ex:
        _raise_tool_error(ex)

    details: Dict[str, Any] = {
        "customer_id": customer_id,
        "ad_group_id": str(ad_group_id),
        "new_status": status,
        "new_cpc_bid": cpc_bid,
        "new_name": new_name,
    }
    if confirm:
        details["updated_resource"] = response.results[0].resource_name
    return _preview_or_done(confirm, "ad_group_update", details)


@mutate_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def keywords_ideas(
    customer_id: str,
    seed_keywords: List[str] = [],
    page_url: Optional[str] = None,
    language_id: str = "1000",
    geo_ids: List[str] = [],
    limit: int = 30,
) -> Dict[str, Any]:
    """Generates keyword ideas from Google Keyword Planner; envelope, not
    a bare list.

    Read-only. Returns {items, returned, truncated, warning?}: each item
    has text, avg monthly searches, competition and top-of-page bid range.
    Order is whatever the Keyword Planner API returns — not sorted by
    this tool. If truncated: raise limit before concluding an idea does
    not exist, and tell the user the list is incomplete.

    Args:
        customer_id: The client account id (digits only, no hyphens).
        seed_keywords: Seed keywords; pass these and/or page_url.
        page_url: Landing page URL to seed ideas from.
        language_id: Keyword Planner language constant id (1000=en,
            1002=fr, 1001=de...).
        geo_ids: Geo target constant ids, e.g. ["2840"] for US; empty =
            all locations.
        limit: Max ideas to return (default 30).
    """
    customer_id = _clean_customer_id(customer_id)
    if not seed_keywords and not page_url:
        raise ToolError("Pass seed_keywords and/or page_url")

    client = utils.get_googleads_client()
    svc = utils.get_googleads_service("KeywordPlanIdeaService")

    request = client.get_type("GenerateKeywordIdeasRequest")
    request.customer_id = customer_id
    request.language = f"languageConstants/{language_id}"
    request.geo_target_constants.extend(
        [f"geoTargetConstants/{g}" for g in geo_ids]
    )
    request.keyword_plan_network = (
        client.enums.KeywordPlanNetworkEnum.GOOGLE_SEARCH
    )
    request.include_adult_keywords = False
    if seed_keywords and page_url:
        request.keyword_and_url_seed.url = page_url
        request.keyword_and_url_seed.keywords.extend(seed_keywords)
    elif page_url:
        request.url_seed.url = page_url
    else:
        request.keyword_seed.keywords.extend(seed_keywords)

    try:
        response = svc.generate_keyword_ideas(request=request)
    except GoogleAdsException as ex:
        _raise_tool_error(ex)

    cap = int(limit)
    out: List[Dict[str, Any]] = []
    for idea in response:
        m = idea.keyword_idea_metrics
        out.append(
            {
                "keyword": idea.text,
                "avg_monthly_searches": int(m.avg_monthly_searches),
                "competition": m.competition.name,
                "top_of_page_bid_low": round(
                    m.low_top_of_page_bid_micros / 1_000_000, 2
                ),
                "top_of_page_bid_high": round(
                    m.high_top_of_page_bid_micros / 1_000_000, 2
                ),
            }
        )
        if len(out) >= cap + 1:
            break
    return utils.list_envelope(out, cap)


@mutate_mcp.tool(annotations=_WRITE_ANNOTATIONS)
def keywords_add(
    customer_id: str,
    ad_group_id: str,
    keywords: List[str],
    match_type: _MATCH_TYPE_ENUM = "BROAD",
    negative: bool = False,
    cpc_bid: Optional[float] = None,
    auto_exempt: bool = False,
    confirm: bool = False,
) -> Dict[str, Any]:
    """Add keywords (or ad-group negative keywords) to an ad group.

    WHEN TO USE: ad-group keywords. Campaign-level negatives:
    negatives_add_campaign_keywords; reusable list:
    negatives_shared_set_add_keywords; ideas: mutate_keywords_ideas.
    PRECONDITIONS: the ad group must exist; one call carries one
    match_type.
    SIDE EFFECTS: policy-blocked keywords come back in "policy_failed" with
    an "exemptible" flag. auto_exempt=true re-sends the exemptible ones
    asserting on the account owner's behalf that the violation is a false
    positive — leave it off unless those violations were reviewed.
    DRY-RUN: confirm=false (default) validates ALL keywords remotely and
    atomically (the API rejects partial_failure with validate_only), so
    one invalid keyword fails the whole preview. confirm=true applies
    with partial_failure=true, so per-keyword outcomes can differ from
    the preview.
    UNITS & IDS: money in account currency (micros internally).

    Args:
        customer_id: The client account id (digits only, no hyphens).
        ad_group_id: The numeric id of the ad group.
        keywords: List of keyword texts, e.g. ["weight loss app",
            "fitness plan"].
        match_type: Applies to all keywords in the call; make separate
            calls for different match types.
        negative: True to add as ad-group-level negative keywords.
        cpc_bid: Optional max CPC bid in account currency (ignored for
            negative keywords).
        auto_exempt: False (default) = policy-blocked keywords are only
            reported. True = re-send the exemptible ones with a policy
            exemption, which claims the violations are false positives.
        confirm: False = dry-run preview (default), True = apply changes.
    """
    customer_id = _clean_customer_id(customer_id)
    match_type = match_type.upper()
    if match_type not in ("EXACT", "PHRASE", "BROAD"):
        raise ToolError("match_type must be EXACT, PHRASE or BROAD")
    if not keywords:
        raise ToolError("keywords list is empty")

    client = utils.get_googleads_client()
    criterion_service = utils.get_googleads_service("AdGroupCriterionService")

    operations = []
    for text in keywords:
        operation = client.get_type("AdGroupCriterionOperation")
        criterion = operation.create
        criterion.ad_group = f"customers/{customer_id}/adGroups/{ad_group_id}"
        criterion.keyword.text = text
        criterion.keyword.match_type = client.enums.KeywordMatchTypeEnum[
            match_type
        ]
        if negative:
            criterion.negative = True
        else:
            criterion.status = client.enums.AdGroupCriterionStatusEnum.ENABLED
            if cpc_bid is not None:
                criterion.cpc_bid_micros = _to_micros(cpc_bid)
        operations.append(operation)

    def _send(ops, partial):
        request = client.get_type("MutateAdGroupCriteriaRequest")
        request.customer_id = customer_id
        request.validate_only = not confirm
        request.partial_failure = partial
        request.operations.extend(ops)
        try:
            return criterion_service.mutate_ad_group_criteria(request=request)
        except GoogleAdsException as ex:
            _raise_tool_error(ex)

    def _failures(response):
        """Returns {op_index: (message, exemptible_policy_key_or_None)}."""
        out = {}
        pfe = getattr(response, "partial_failure_error", None)
        if not pfe or not pfe.details:
            return out
        failure_cls = type(client.get_type("GoogleAdsFailure"))
        for detail in pfe.details:
            failure = failure_cls.deserialize(detail.value)
            for error in failure.errors:
                idx = None
                for fpe in error.location.field_path_elements:
                    if fpe.field_name == "operations":
                        idx = fpe.index
                        break
                if idx is None:
                    continue
                pvd = error.details.policy_violation_details
                key = None
                if pvd.is_exemptible and pvd.key.policy_name:
                    key = pvd.key
                prev = out.get(idx)
                if prev and prev[1] is None:
                    key = None  # any non-exemptible error blocks the op
                out[idx] = (error.message.strip(), key)
        return out

    created: List[str] = []
    exempted: List[str] = []
    policy_failed: List[Dict[str, Any]] = []

    if not confirm:
        _send(operations, False)  # validate_only, atomic
    else:
        response = _send(operations, True)
        fails = _failures(response)
        for i, r in enumerate(response.results):
            if r.resource_name and i not in fails:
                created.append(r.resource_name)
        retry_ops, retry_texts = [], []
        for idx, (msg, key) in sorted(fails.items()):
            if auto_exempt and key is not None:
                operations[idx].exempt_policy_violation_keys.append(key)
                retry_ops.append(operations[idx])
                retry_texts.append(keywords[idx])
            else:
                policy_failed.append(
                    {
                        "keyword": keywords[idx],
                        "reason": msg,
                        "exemptible": key is not None,
                    }
                )
        if retry_ops:
            response2 = _send(retry_ops, True)
            fails2 = _failures(response2)
            for i, r in enumerate(response2.results):
                if i in fails2:
                    policy_failed.append(
                        {
                            "keyword": retry_texts[i],
                            "reason": fails2[i][0],
                            "exemptible": fails2[i][1] is not None,
                        }
                    )
                elif r.resource_name:
                    created.append(r.resource_name)
                    exempted.append(retry_texts[i])

    details: Dict[str, Any] = {
        "customer_id": customer_id,
        "ad_group_id": str(ad_group_id),
        "match_type": match_type,
        "negative": negative,
        "cpc_bid": cpc_bid,
        "requested": len(keywords),
    }
    if confirm:
        details["created_count"] = len(created)
        details["policy_exempted"] = exempted
        details["policy_failed"] = policy_failed
    else:
        details["keywords"] = keywords
        if auto_exempt:
            details["auto_exempt_note"] = (
                "auto_exempt=true: on apply, keywords rejected for an "
                "exemptible policy violation will be re-sent with a policy "
                "exemption claiming the violation is a false positive. The "
                "dry-run cannot show which keywords that would affect."
            )
    return _preview_or_done(confirm, "keywords_add", details)


@mutate_mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True)
)
def keywords_remove(
    customer_id: str,
    ad_group_id: str,
    criterion_ids: List[str],
    confirm: bool = False,
) -> Dict[str, Any]:
    """Remove keywords from an ad group by criterion id. IRREVERSIBLE.

    WHEN TO USE: deleting keywords for good. To stop traffic without
    losing history add a negative (negatives_add_campaign_keywords).
    PRECONDITIONS: get criterion ids with search_search on resource
    ad_group_criterion (ad_group_criterion.criterion_id, .keyword.text).
    SIDE EFFECTS: the keywords and their history are gone — they can only
    be re-created.
    DRY-RUN: confirm=false (default) validates remotely, changes nothing;
    confirm=true applies.
    UNITS & IDS: criterion ids are per ad group, not global.

    Args:
        customer_id: The client account id (digits only, no hyphens).
        ad_group_id: The numeric id of the ad group.
        criterion_ids: List of numeric criterion ids to remove.
        confirm: False = dry-run preview (default), True = apply changes.
    """
    customer_id = _clean_customer_id(customer_id)
    if not criterion_ids:
        raise ToolError("criterion_ids list is empty")

    client = utils.get_googleads_client()
    criterion_service = utils.get_googleads_service("AdGroupCriterionService")

    request = client.get_type("MutateAdGroupCriteriaRequest")
    request.customer_id = customer_id
    request.validate_only = not confirm

    for crit_id in criterion_ids:
        operation = client.get_type("AdGroupCriterionOperation")
        operation.remove = (
            f"customers/{customer_id}/adGroupCriteria/"
            f"{ad_group_id}~{crit_id}"
        )
        request.operations.append(operation)

    try:
        response = criterion_service.mutate_ad_group_criteria(request=request)
    except GoogleAdsException as ex:
        _raise_tool_error(ex)

    details: Dict[str, Any] = {
        "customer_id": customer_id,
        "ad_group_id": str(ad_group_id),
        "criterion_ids": criterion_ids,
        "count": len(criterion_ids),
    }
    if confirm:
        details["removed_resources"] = [
            r.resource_name for r in response.results
        ]
    return _preview_or_done(confirm, "keywords_remove", details)


@mutate_mcp.tool(annotations=_WRITE_ANNOTATIONS)
def ad_create_rsa(
    customer_id: str,
    ad_group_id: str,
    headlines: List[str],
    descriptions: List[str],
    final_url: str,
    path1: Optional[str] = None,
    path2: Optional[str] = None,
    tracking_url_template: Optional[str] = None,
    status: _STATUS_ENUM = "PAUSED",
    confirm: bool = False,
) -> Dict[str, Any]:
    """Create a Responsive Search Ad (RSA) in an ad group.

    WHEN TO USE: Search ad groups. Other channels:
    display_ad_create_responsive, shopping_ad_create_product,
    demandgen_ad_create_image/_video/_carousel.
    PRECONDITIONS: the ad group must exist. Lengths are checked LOCALLY
    first: 3-15 headlines <=30 chars, 2-4 descriptions <=90 chars.
    SIDE EFFECTS: created PAUSED unless status="ENABLED", and still has to
    pass Google's policy review before it serves.
    DRY-RUN: confirm=false (default) validates remotely, changes nothing;
    confirm=true applies.

    Args:
        customer_id: The client account id (digits only, no hyphens).
        ad_group_id: The numeric id of the ad group.
        headlines: 3-15 headlines, max 30 characters each.
        descriptions: 2-4 descriptions, max 90 characters each.
        final_url: Landing page URL (put UTM parameters here if needed).
        path1: Display path 1 (max 15 chars).
        path2: Display path 2 (max 15 chars; ignored without path1).
        tracking_url_template: Tracking template; Google requires it to
            contain the {lpurl} placeholder.
        status: Status the ad is created with.
        confirm: False = dry-run preview (default), True = apply changes.
    """
    customer_id = _clean_customer_id(customer_id)
    status = status.upper()
    if status not in ("PAUSED", "ENABLED"):
        raise ToolError("status must be PAUSED or ENABLED")
    if not (3 <= len(headlines) <= 15):
        raise ToolError("RSA requires 3-15 headlines")
    if not (2 <= len(descriptions) <= 4):
        raise ToolError("RSA requires 2-4 descriptions")
    too_long_h = [h for h in headlines if len(h) > 30]
    if too_long_h:
        raise ToolError(f"Headlines over 30 chars: {too_long_h}")
    too_long_d = [d for d in descriptions if len(d) > 90]
    if too_long_d:
        raise ToolError(f"Descriptions over 90 chars: {too_long_d}")

    client = utils.get_googleads_client()
    ad_service = utils.get_googleads_service("AdGroupAdService")

    operation = client.get_type("AdGroupAdOperation")
    ad_group_ad = operation.create
    ad_group_ad.ad_group = f"customers/{customer_id}/adGroups/{ad_group_id}"
    ad_group_ad.status = client.enums.AdGroupAdStatusEnum[status]
    ad = ad_group_ad.ad
    ad.final_urls.append(final_url)
    if tracking_url_template:
        ad.tracking_url_template = tracking_url_template

    for text in headlines:
        asset = client.get_type("AdTextAsset")
        asset.text = text
        ad.responsive_search_ad.headlines.append(asset)
    for text in descriptions:
        asset = client.get_type("AdTextAsset")
        asset.text = text
        ad.responsive_search_ad.descriptions.append(asset)
    if path1:
        ad.responsive_search_ad.path1 = path1
        if path2:
            ad.responsive_search_ad.path2 = path2

    request = client.get_type("MutateAdGroupAdsRequest")
    request.customer_id = customer_id
    request.operations.append(operation)
    request.validate_only = not confirm

    try:
        response = ad_service.mutate_ad_group_ads(request=request)
    except GoogleAdsException as ex:
        _raise_tool_error(ex)

    details: Dict[str, Any] = {
        "customer_id": customer_id,
        "ad_group_id": str(ad_group_id),
        "headlines_count": len(headlines),
        "descriptions_count": len(descriptions),
        "final_url": final_url,
        "status": status,
    }
    if confirm:
        details["created_resource"] = response.results[0].resource_name
    return _preview_or_done(confirm, "ad_create_rsa", details)


@mutate_mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True)
)
def ad_update_status(
    customer_id: str,
    ad_group_id: str,
    ad_id: str,
    status: str,
    confirm: bool = False,
) -> Dict[str, Any]:
    """Pause, enable or remove one ad.

    WHEN TO USE: one ad. Whole ad group: mutate_ad_group_update; whole
    campaign: mutate_campaign_update_status.
    PRECONDITIONS: both ids are needed (an ad is addressed adGroupId~adId)
    — find them with search_search on resource ad_group_ad.
    SIDE EFFECTS: serving changes at once. REMOVED is IRREVERSIBLE — the
    ad can only be re-created.
    DRY-RUN: confirm=false (default) validates remotely, changes nothing;
    confirm=true applies.

    Args:
        customer_id: The client account id (digits only, no hyphens).
        ad_group_id: The numeric id of the ad group containing the ad.
        ad_id: The numeric id of the ad.
        status: ENABLED, PAUSED or REMOVED.
        confirm: False = dry-run preview (default), True = apply changes.
    """
    customer_id = _clean_customer_id(customer_id)
    status = status.upper()
    if status not in ("ENABLED", "PAUSED", "REMOVED"):
        raise ToolError("status must be ENABLED, PAUSED or REMOVED")

    client = utils.get_googleads_client()
    ad_service = utils.get_googleads_service("AdGroupAdService")

    operation = client.get_type("AdGroupAdOperation")
    resource_name = f"customers/{customer_id}/adGroupAds/{ad_group_id}~{ad_id}"
    if status == "REMOVED":
        operation.remove = resource_name
    else:
        ad_group_ad = operation.update
        ad_group_ad.resource_name = resource_name
        ad_group_ad.status = client.enums.AdGroupAdStatusEnum[status]
        client.copy_from(
            operation.update_mask,
            protobuf_helpers.field_mask(None, ad_group_ad._pb),
        )

    request = client.get_type("MutateAdGroupAdsRequest")
    request.customer_id = customer_id
    request.operations.append(operation)
    request.validate_only = not confirm

    try:
        response = ad_service.mutate_ad_group_ads(request=request)
    except GoogleAdsException as ex:
        _raise_tool_error(ex)

    details: Dict[str, Any] = {
        "customer_id": customer_id,
        "ad_group_id": str(ad_group_id),
        "ad_id": str(ad_id),
        "new_status": status,
    }
    if confirm:
        details["updated_resource"] = response.results[0].resource_name
    return _preview_or_done(confirm, "ad_update_status", details)


@mutate_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def list_campaigns(
    customer_id: str,
    status: Optional[str] = None,
    include_removed: bool = False,
    limit: int = 100,
) -> Dict[str, Any]:
    """Lists campaigns (id, name, status, budget); envelope, not a bare
    list.

    Useful before calling the write tools to find campaign ids. Returns
    {items, returned, truncated, warning?}. A campaign missing from a
    truncated list means "not on this page", NOT "does not exist" — this
    list feeds duplicate-name checks before mutate_campaign_create, so a
    truncated result must never be read as a clean name search. If
    truncated: raise limit or narrow the filter, and tell the user the
    list is incomplete before concluding a name is free.

    Args:
        customer_id: The client account id (digits only, no hyphens).
        status: Optional filter: ENABLED, PAUSED or REMOVED.
        include_removed: Include REMOVED campaigns (default False).
        limit: Max rows (default 100).
    """
    customer_id = _clean_customer_id(customer_id)
    ga_service = utils.get_googleads_service("GoogleAdsService")

    conditions = []
    if status:
        status = status.upper()
        if status not in _ALLOWED_CAMPAIGN_STATUSES:
            raise ToolError(
                f"status must be one of {_ALLOWED_CAMPAIGN_STATUSES}"
            )
        conditions.append(f"campaign.status = '{status}'")
    elif not include_removed:
        conditions.append("campaign.status != 'REMOVED'")
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    cap = int(limit)
    query = (
        "SELECT campaign.id, campaign.name, campaign.status, "
        "campaign.advertising_channel_type, campaign_budget.amount_micros "
        f"FROM campaign {where} ORDER BY campaign.status ASC, "
        f"campaign.name ASC LIMIT {cap + 1}"
    )
    try:
        rows = ga_service.search(customer_id=customer_id, query=query)
        items = [
            {
                "id": str(row.campaign.id),
                "name": row.campaign.name,
                "status": row.campaign.status.name,
                "channel_type": row.campaign.advertising_channel_type.name,
                "daily_budget": row.campaign_budget.amount_micros / _MICROS,
            }
            for row in rows
        ]
        return utils.list_envelope(items, cap)
    except GoogleAdsException as ex:
        _raise_tool_error(ex)


@mutate_mcp.tool(annotations=_WRITE_ANNOTATIONS)
def campaign_set_conversion_goals(
    customer_id: str,
    campaign_id: str,
    biddable_categories: List[str],
    confirm: bool = False,
) -> Dict[str, Any]:
    """Set CAMPAIGN-SPECIFIC standard conversion goals (e.g. Purchases).

    WHEN TO USE: standard category goals (the UI "Campaign-specific:
    <category>"). Custom goal: mutate_campaign_set_custom_conversion_goal.
    PRECONDITIONS: the categories must already exist as goals — this flips
    existing ones, it creates none. List them with search_search on
    resource campaign_conversion_goal.
    SIDE EFFECTS: switches the campaign to campaign-level goals AND makes
    the listed categories biddable, turning every other one off. Changing
    what a campaign bids on resets Smart Bidding learning.
    DRY-RUN: only the goal-config-level switch is sent for validation; the
    per-category flips are computed LOCALLY and never sent, so "changed"
    is a local diff and the apply can still fail. confirm=true applies.

    Args:
        customer_id: The client account id (digits only, no hyphens).
        campaign_id: The numeric id of the campaign.
        biddable_categories: Categories to bid on, e.g. ["PURCHASE"]; every
            category not listed is switched off.
        confirm: False = dry-run preview (default), True = apply.
    """
    from google.protobuf import field_mask_pb2

    customer_id = _clean_customer_id(customer_id)
    wanted = {c.upper() for c in biddable_categories}
    if not wanted:
        raise ToolError("biddable_categories must not be empty")

    client = utils.get_googleads_client()
    cfg_service = utils.get_googleads_service(
        "ConversionGoalCampaignConfigService"
    )

    # 1) switch config to campaign level
    op = client.get_type("ConversionGoalCampaignConfigOperation")
    cfg = op.update
    cfg.resource_name = (
        f"customers/{customer_id}/conversionGoalCampaignConfigs/{campaign_id}"
    )
    cfg.goal_config_level = client.enums.GoalConfigLevelEnum.CAMPAIGN
    client.copy_from(
        op.update_mask,
        field_mask_pb2.FieldMask(paths=["goal_config_level"]),
    )
    req = client.get_type("MutateConversionGoalCampaignConfigsRequest")
    req.customer_id = customer_id
    req.operations.append(op)
    req.validate_only = not confirm
    try:
        cfg_service.mutate_conversion_goal_campaign_configs(request=req)
    except GoogleAdsException as ex:
        _raise_tool_error(ex)

    # 2) flip the category goals. The current goals are read in both
    # branches so the dry-run reports the same diff the apply performs.
    ga_service = utils.get_googleads_service("GoogleAdsService")
    try:
        rows = list(
            ga_service.search(
                customer_id=customer_id,
                query=(
                    "SELECT campaign_conversion_goal.resource_name, "
                    "campaign_conversion_goal.category, "
                    "campaign_conversion_goal.origin, "
                    "campaign_conversion_goal.biddable "
                    "FROM campaign_conversion_goal "
                    f"WHERE campaign.id = {int(campaign_id)}"
                ),
            )
        )
    except GoogleAdsException as ex:
        _raise_tool_error(ex)

    changed: Dict[str, List[str]] = {"biddable_on": [], "biddable_off": []}
    ccg_req = client.get_type("MutateCampaignConversionGoalsRequest")
    ccg_req.customer_id = customer_id
    for row in rows:
        g = row.campaign_conversion_goal
        cat = g.category.name
        want = cat in wanted
        if bool(g.biddable) == want:
            continue
        o = client.get_type("CampaignConversionGoalOperation")
        gg = o.update
        gg.resource_name = g.resource_name
        gg.biddable = want
        client.copy_from(
            o.update_mask,
            field_mask_pb2.FieldMask(paths=["biddable"]),
        )
        ccg_req.operations.append(o)
        (changed["biddable_on"] if want else changed["biddable_off"]).append(
            cat
        )
    if confirm and ccg_req.operations:
        ccg_service = utils.get_googleads_service(
            "CampaignConversionGoalService"
        )
        try:
            ccg_service.mutate_campaign_conversion_goals(request=ccg_req)
        except GoogleAdsException as ex:
            _raise_tool_error(ex)

    details: Dict[str, Any] = {
        "customer_id": customer_id,
        "campaign_id": str(campaign_id),
        "goal_config_level": "CAMPAIGN",
        "biddable_categories": sorted(wanted),
        "changed": changed,
    }
    if not confirm:
        # Only step 1 round-trips with validate_only. The category flips in
        # "changed" are a local diff of the current goals: their operations
        # are built but never sent, so the envelope must not claim Google
        # validated them.
        details["validation"] = _GOAL_FLIP_VALIDATION
        return _preview_or_done(
            False,
            "campaign_set_conversion_goals",
            details,
            validated=False,
            note=_GOAL_FLIP_DRY_RUN_NOTE,
        )
    return _preview_or_done(True, "campaign_set_conversion_goals", details)


@mutate_mcp.tool(annotations=_WRITE_ANNOTATIONS)
def campaign_set_custom_conversion_goal(
    customer_id: str,
    campaign_id: str,
    custom_conversion_goal_id: str,
    confirm: bool = False,
) -> Dict[str, Any]:
    """Point a campaign at a CUSTOM conversion goal.

    WHEN TO USE: a product-specific custom goal, replacing the
    account-default goals a new campaign starts on. Standard categories:
    mutate_campaign_set_conversion_goals.
    PRECONDITIONS: the goal must exist — find ids with search_search on
    resource custom_conversion_goal, or copy a sibling campaign's via
    resource conversion_goal_campaign_config.
    SIDE EFFECTS: switches to campaign-level goals AND turns off every
    still-biddable standard category goal (counted in
    "disabled_category_goals"), as the UI does. Resets bidding learning.
    DRY-RUN: only the goal-config-level switch is sent for validation;
    disabling the category goals is computed LOCALLY and never sent, so
    the count is a local read and the apply can still fail. confirm=true
    applies.

    Args:
        customer_id: The client account id (digits only, no hyphens).
        campaign_id: The numeric id of the campaign.
        custom_conversion_goal_id: The numeric id of the custom conversion
            goal (e.g. from another campaign of the same product).
        confirm: False = dry-run preview (default), True = apply.
    """
    from google.protobuf import field_mask_pb2

    customer_id = _clean_customer_id(customer_id)

    client = utils.get_googleads_client()
    service = utils.get_googleads_service("ConversionGoalCampaignConfigService")

    operation = client.get_type("ConversionGoalCampaignConfigOperation")
    config = operation.update
    config.resource_name = (
        f"customers/{customer_id}/conversionGoalCampaignConfigs/{campaign_id}"
    )
    config.goal_config_level = client.enums.GoalConfigLevelEnum.CAMPAIGN
    config.custom_conversion_goal = (
        f"customers/{customer_id}/customConversionGoals/"
        f"{custom_conversion_goal_id}"
    )
    fm = field_mask_pb2.FieldMask(
        paths=["goal_config_level", "custom_conversion_goal"]
    )
    client.copy_from(operation.update_mask, fm)

    request = client.get_type("MutateConversionGoalCampaignConfigsRequest")
    request.customer_id = customer_id
    request.operations.append(operation)
    request.validate_only = not confirm

    try:
        response = service.mutate_conversion_goal_campaign_configs(
            request=request
        )
    except GoogleAdsException as ex:
        _raise_tool_error(ex)

    # Match UI behaviour: disable all standard category goals so only the
    # custom goal is used for bidding (the UI template does the same). The
    # read runs in both branches so the dry-run reports the real count.
    ga_service = utils.get_googleads_service("GoogleAdsService")
    try:
        rows = list(
            ga_service.search(
                customer_id=customer_id,
                query=(
                    "SELECT campaign_conversion_goal.resource_name, "
                    "campaign_conversion_goal.biddable "
                    "FROM campaign_conversion_goal "
                    f"WHERE campaign.id = {int(campaign_id)} "
                    "AND campaign_conversion_goal.biddable = true"
                ),
            )
        )
    except GoogleAdsException as ex:
        _raise_tool_error(ex)

    disabled_categories = len(rows)
    if confirm and rows:
        ccg_service = utils.get_googleads_service(
            "CampaignConversionGoalService"
        )
        ccg_request = client.get_type("MutateCampaignConversionGoalsRequest")
        ccg_request.customer_id = customer_id
        for row in rows:
            op = client.get_type("CampaignConversionGoalOperation")
            goal = op.update
            goal.resource_name = row.campaign_conversion_goal.resource_name
            goal.biddable = False
            client.copy_from(
                op.update_mask,
                field_mask_pb2.FieldMask(paths=["biddable"]),
            )
            ccg_request.operations.append(op)
        try:
            ccg_service.mutate_campaign_conversion_goals(request=ccg_request)
        except GoogleAdsException as ex:
            _raise_tool_error(ex)

    details: Dict[str, Any] = {
        "customer_id": customer_id,
        "campaign_id": str(campaign_id),
        "custom_conversion_goal_id": str(custom_conversion_goal_id),
        "goal_config_level": "CAMPAIGN",
        "disabled_category_goals": disabled_categories,
    }
    if not confirm:
        # Only the config-level switch round-trips with validate_only; the
        # category goals are merely counted here and disabled on apply.
        details["validation"] = _GOAL_FLIP_VALIDATION
        return _preview_or_done(
            False,
            "campaign_set_custom_conversion_goal",
            details,
            validated=False,
            note=_GOAL_FLIP_DRY_RUN_NOTE,
        )
    details["updated_resource"] = response.results[0].resource_name
    return _preview_or_done(
        True, "campaign_set_custom_conversion_goal", details
    )
