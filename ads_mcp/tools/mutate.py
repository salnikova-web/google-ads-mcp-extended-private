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
"""

import time
from typing import Any, Dict, List, Optional

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from google.ads.googleads.errors import GoogleAdsException
from google.api_core import protobuf_helpers

import ads_mcp.utils as utils

mutate_mcp = FastMCP("mutate")

_MICROS = 1_000_000

_WRITE_ANNOTATIONS = ToolAnnotations(readOnlyHint=False, destructiveHint=False)

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


def _raise_tool_error(ex: GoogleAdsException) -> None:
    error_msgs = []
    for error in ex.failure.errors:
        field_path = ""
        if error.location and error.location.field_path_elements:
            field_path = ".".join(
                fpe.field_name for fpe in error.location.field_path_elements
            )
        code = str(error.error_code).strip().replace("\n", " ")
        msg = f"Google Ads API Error: {error.message} [{code}]"
        if field_path:
            msg += f" (field: {field_path})"
        error_msgs.append(msg)
    raise ToolError(f"Request ID: {ex.request_id}\n" + "\n".join(error_msgs))


def _to_micros(amount: float) -> int:
    return int(round(float(amount) * _MICROS))


def _clean_customer_id(customer_id: str) -> str:
    return str(customer_id).replace("-", "").strip()


def _preview_or_done(confirm: bool, action: str, details: Dict[str, Any]) -> Dict[str, Any]:
    if confirm:
        return {"applied": True, "action": action, **details}
    return {
        "applied": False,
        "validated": True,
        "action": action,
        "note": (
            "DRY-RUN: the operation was validated by Google Ads "
            "(validate_only=true) but NOT applied. Re-run the tool with "
            "confirm=true to apply it."
        ),
        **details,
    }


@mutate_mcp.tool(annotations=_WRITE_ANNOTATIONS)
def campaign_create(
    customer_id: str,
    name: str,
    daily_budget: float,
    channel_type: str = "SEARCH",
    bidding_strategy: str = "MAXIMIZE_CONVERSIONS",
    target_cpa: Optional[float] = None,
    target_roas: Optional[float] = None,
    tracking_url_template: Optional[str] = None,
    final_url_suffix: Optional[str] = None,
    status: str = "PAUSED",
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    confirm: bool = False,
) -> Dict[str, Any]:
    """Creates a new campaign together with a dedicated daily budget.

    Optional tracking_url_template / final_url_suffix set UTM tracking at
    creation (recommended for web funnels). NOTE: the campaign is
    created with ACCOUNT-DEFAULT conversion goals — attach the product's
    custom goal with campaign_set_custom_conversion_goal right after.

    SAFETY: by default runs in DRY-RUN mode (validate_only). Google Ads fully
    validates the request without creating anything. Re-run with confirm=true
    to actually create the campaign. New campaigns are created PAUSED unless
    status="ENABLED" is passed explicitly.

    Args:
        customer_id: The client account id (digits only, no hyphens).
        name: Campaign name (must be unique within the account).
        daily_budget: Daily budget in the account currency (e.g. 50.0 = €50
            for EUR accounts). Converted to micros automatically.
        channel_type: One of SEARCH, DISPLAY, SHOPPING, VIDEO,
            PERFORMANCE_MAX, DEMAND_GEN. Note: PERFORMANCE_MAX campaigns also
            require asset groups, which are not yet supported here — create
            the campaign PAUSED and finish setup in the UI.
        bidding_strategy: One of MAXIMIZE_CONVERSIONS,
            MAXIMIZE_CONVERSION_VALUE, MAXIMIZE_CLICKS, MANUAL_CPC.
        target_cpa: Optional target CPA in account currency
            (only with MAXIMIZE_CONVERSIONS).
        target_roas: Optional target ROAS as a decimal, e.g. 3.5 = 350%
            (only with MAXIMIZE_CONVERSION_VALUE).
        status: PAUSED (default, recommended) or ENABLED.
        confirm: False = dry-run preview (default), True = apply changes.

    Returns:
        Dict with the result: resource names when applied, or a validated
        preview when confirm=false.
    """
    customer_id = _clean_customer_id(customer_id)
    channel_type = channel_type.upper()
    bidding_strategy = bidding_strategy.upper()
    status = status.upper()

    if channel_type not in _ALLOWED_CHANNEL_TYPES:
        raise ToolError(
            f"channel_type must be one of {_ALLOWED_CHANNEL_TYPES}"
        )
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


@mutate_mcp.tool(annotations=_WRITE_ANNOTATIONS)
def campaign_update_status(
    customer_id: str,
    campaign_id: str,
    status: str,
    confirm: bool = False,
) -> Dict[str, Any]:
    """Pauses, enables or removes a campaign.

    SAFETY: by default runs in DRY-RUN mode (validate_only). Re-run with
    confirm=true to apply. REMOVED is irreversible — a removed campaign
    cannot be re-enabled.

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
def campaign_set_target_roas(
    customer_id: str,
    campaign_id: str,
    target_roas: float,
    confirm: bool = False,
) -> Dict[str, Any]:
    """Sets Target ROAS on a Maximize Conversion Value campaign.

    Use after creation for Standard Shopping (tROAS is often not accepted
    at create time). target_roas is a decimal, e.g. 1.7 = 170%.

    SAFETY: dry-run by default (validate_only); re-run with confirm=true.
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
        field_mask_pb2.FieldMask(paths=["maximize_conversion_value.target_roas"]),
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
    """Links a Merchant Center feed to an existing campaign (PMax/Shopping).

    NOTE: on Performance Max the Merchant Center id is often immutable
    after creation; if the API rejects this update, the campaign must be
    recreated with the merchant feed set at creation time
    (pmax_campaign_create merchant_id=...).

    feed_label: optional country/label filter; omit to use the whole feed.
    SAFETY: dry-run by default (validate_only); re-run with confirm=true.
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
    """Updates campaign network settings, geo target type and/or AI Max.

    Only the passed fields are changed. target_search_network=False turns
    OFF Google search partners. positive_geo_target_type: PRESENCE or
    PRESENCE_OR_INTEREST. enable_ai_max toggles AI Max for Search;
    text_customization / final_url_expansion toggle its sub-settings
    (asset automation OPTED_IN/OPTED_OUT).

    NOTE: the Demand Gen "Asset optimization" toggles (shorter/resized
    videos, landing page previews) are NOT campaign-level — they live on
    each video ad. Use ad_group_ad_update_asset_optimization for existing
    DG ads, or set them at creation via demandgen_ad_create_video.

    SAFETY: dry-run by default (validate_only); re-run with confirm=true.
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
        campaign.network_settings.target_content_network = target_content_network
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
    if any(x is not None for x in (text_customization, final_url_expansion,
            image_enhancement, image_extraction, video_enhancement)):
        ga_service = utils.get_googleads_service("GoogleAdsService")
        rows = ga_service.search(
            customer_id=customer_id,
            query=(
                "SELECT campaign.asset_automation_settings FROM campaign "
                f"WHERE campaign.id = {int(campaign_id)}"
            ),
        )
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
            _set(t_enum.FINAL_URL_EXPANSION_TEXT_ASSET_AUTOMATION,
                 final_url_expansion)
        if image_enhancement is not None:
            _set(t_enum.GENERATE_IMAGE_ENHANCEMENT, image_enhancement)
        if image_extraction is not None:
            _set(t_enum.GENERATE_IMAGE_EXTRACTION, image_extraction)
        if video_enhancement is not None:
            _set(t_enum.GENERATE_ENHANCED_YOUTUBE_VIDEOS, video_enhancement)
        setting_cls = type(campaign).AssetAutomationSetting
        for t, s in sorted(current.items()):
            campaign.asset_automation_settings.append(
                setting_cls(
                    asset_automation_type=t, asset_automation_status=s
                )
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
    """Renames an existing campaign (changes campaign.name).

    Campaign names must be unique within the account; a duplicate name is
    rejected by the API. The preview shows the current name and the new one.

    SAFETY: dry-run by default (validate_only); re-run with confirm=true.

    Args:
        customer_id: The client account id (digits only, no hyphens).
        campaign_id: The numeric id of the campaign to rename.
        name: The new campaign name.
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
    for row in ga_service.search(
        customer_id=customer_id,
        query=(
            "SELECT campaign.name FROM campaign "
            f"WHERE campaign.id = {int(campaign_id)}"
        ),
    ):
        old_name = row.campaign.name
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
    """Changes the daily budget of an existing campaign.

    Looks up the budget attached to the campaign and updates its amount.
    SAFETY: by default runs in DRY-RUN mode (validate_only) and returns the
    current vs new amount. Re-run with confirm=true to apply.

    Note: if the budget is shared between several campaigns
    (explicitly_shared=true), the change affects ALL campaigns using it —
    the preview will warn about this.

    Args:
        customer_id: The client account id (digits only, no hyphens).
        campaign_id: The numeric id of the campaign.
        new_daily_budget: New daily budget in account currency
            (e.g. 75.0 = €75). Converted to micros automatically.
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
        rows = list(
            ga_service.search(customer_id=customer_id, query=query)
        )
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
def ad_group_create(
    customer_id: str,
    campaign_id: str,
    name: str,
    cpc_bid: Optional[float] = None,
    status: str = "PAUSED",
    confirm: bool = False,
) -> Dict[str, Any]:
    """Creates a new ad group (SEARCH_STANDARD) inside an existing campaign.

    SAFETY: by default runs in DRY-RUN mode (validate_only). Re-run with
    confirm=true to apply. Created PAUSED unless status="ENABLED".

    Args:
        customer_id: The client account id (digits only, no hyphens).
        campaign_id: The numeric id of the parent campaign.
        name: Ad group name (unique within the campaign).
        cpc_bid: Optional max CPC bid in account currency (e.g. 1.5 = €1.50).
        status: PAUSED (default) or ENABLED.
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


@mutate_mcp.tool(annotations=_WRITE_ANNOTATIONS)
def ad_group_update(
    customer_id: str,
    ad_group_id: str,
    status: Optional[str] = None,
    cpc_bid: Optional[float] = None,
    new_name: Optional[str] = None,
    confirm: bool = False,
) -> Dict[str, Any]:
    """Updates an existing ad group: status, max CPC bid and/or name.

    Pass only the fields you want to change. SAFETY: by default runs in
    DRY-RUN mode (validate_only). Re-run with confirm=true to apply.

    Args:
        customer_id: The client account id (digits only, no hyphens).
        ad_group_id: The numeric id of the ad group.
        status: Optional new status: ENABLED, PAUSED or REMOVED.
        cpc_bid: Optional new max CPC bid in account currency.
        new_name: Optional new name.
        confirm: False = dry-run preview (default), True = apply changes.
    """
    customer_id = _clean_customer_id(customer_id)
    if status is None and cpc_bid is None and new_name is None:
        raise ToolError(
            "Nothing to update: pass at least one of status, cpc_bid, new_name"
        )

    client = utils.get_googleads_client()
    ad_group_service = utils.get_googleads_service("AdGroupService")

    operation = client.get_type("AdGroupOperation")
    if status is not None and status.upper() == "REMOVED":
        operation.remove = (
            f"customers/{customer_id}/adGroups/{ad_group_id}"
        )
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
            details_removed["removed_resource"] = (
                response.results[0].resource_name
            )
        return _preview_or_done(confirm, "ad_group_update", details_removed)
    ad_group = operation.update
    ad_group.resource_name = (
        f"customers/{customer_id}/adGroups/{ad_group_id}"
    )
    if status is not None:
        status = status.upper()
        if status not in ("ENABLED", "PAUSED"):
            raise ToolError("status must be ENABLED, PAUSED or REMOVED")
        ad_group.status = client.enums.AdGroupStatusEnum[status]
    if cpc_bid is not None:
        ad_group.cpc_bid_micros = _to_micros(cpc_bid)
    if new_name is not None:
        ad_group.name = new_name
    client.copy_from(
        operation.update_mask,
        protobuf_helpers.field_mask(None, ad_group._pb),
    )

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
) -> List[Dict[str, Any]]:
    """Generates keyword ideas from Google Keyword Planner.

    Read-only. Seed with keywords and/or a landing page URL. language_id:
    1000=en, 1002=fr, 1001=de... geo_ids: e.g. ["2840"] for US; empty =
    all locations. Returns text, avg monthly searches, competition and
    top-of-page bid range, sorted by search volume.
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
        if len(out) >= int(limit):
            break
    return out


@mutate_mcp.tool(annotations=_WRITE_ANNOTATIONS)
def keywords_add(
    customer_id: str,
    ad_group_id: str,
    keywords: List[str],
    match_type: str = "BROAD",
    negative: bool = False,
    cpc_bid: Optional[float] = None,
    auto_exempt: bool = True,
    confirm: bool = False,
) -> Dict[str, Any]:
    """Adds keywords (or negative keywords) to an ad group.

    With confirm=true the request runs in partial-failure mode: valid
    keywords are created even if some fail. Keywords flagged by an
    EXEMPTIBLE policy violation are automatically retried with a policy
    exemption when auto_exempt=true (standard practice for false
    positives). Non-exemptible failures are returned in "policy_failed".

    SAFETY: by default runs in DRY-RUN mode (validate_only). Re-run with
    confirm=true to apply.

    Args:
        customer_id: The client account id (digits only, no hyphens).
        ad_group_id: The numeric id of the ad group.
        keywords: List of keyword texts, e.g. ["weight loss app",
            "fitness plan"].
        match_type: EXACT, PHRASE or BROAD (applies to all keywords in the
            call; make separate calls for different match types).
        negative: True to add as ad-group-level negative keywords.
        cpc_bid: Optional max CPC bid in account currency (ignored for
            negative keywords).
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
        criterion.ad_group = (
            f"customers/{customer_id}/adGroups/{ad_group_id}"
        )
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
    policy_failed: List[Dict[str, str]] = []

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
                    {"keyword": keywords[idx], "reason": msg}
                )
        if retry_ops:
            response2 = _send(retry_ops, True)
            fails2 = _failures(response2)
            for i, r in enumerate(response2.results):
                if i in fails2:
                    policy_failed.append(
                        {"keyword": retry_texts[i], "reason": fails2[i][0]}
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
    return _preview_or_done(confirm, "keywords_add", details)


@mutate_mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
def keywords_remove(
    customer_id: str,
    ad_group_id: str,
    criterion_ids: List[str],
    confirm: bool = False,
) -> Dict[str, Any]:
    """Removes keywords from an ad group by criterion id. IRREVERSIBLE.

    Find criterion ids first via search on resource ad_group_criterion
    (fields: ad_group_criterion.criterion_id, ad_group_criterion.keyword.text).
    SAFETY: by default runs in DRY-RUN mode (validate_only). Re-run with
    confirm=true to apply.

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
    status: str = "PAUSED",
    confirm: bool = False,
) -> Dict[str, Any]:
    """Creates a Responsive Search Ad (RSA) in an ad group.

    SAFETY: by default runs in DRY-RUN mode (validate_only). Re-run with
    confirm=true to apply. Created PAUSED unless status="ENABLED".

    Args:
        customer_id: The client account id (digits only, no hyphens).
        ad_group_id: The numeric id of the ad group.
        headlines: 3-15 headlines, max 30 characters each.
        descriptions: 2-4 descriptions, max 90 characters each.
        final_url: Landing page URL (include UTM parameters here if needed).
        path1: Optional display path 1 (max 15 chars).
        path2: Optional display path 2 (max 15 chars, requires path1).
        status: PAUSED (default) or ENABLED.
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
    ad_group_ad.ad_group = (
        f"customers/{customer_id}/adGroups/{ad_group_id}"
    )
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


@mutate_mcp.tool(annotations=_WRITE_ANNOTATIONS)
def ad_update_status(
    customer_id: str,
    ad_group_id: str,
    ad_id: str,
    status: str,
    confirm: bool = False,
) -> Dict[str, Any]:
    """Pauses, enables or removes an ad.

    SAFETY: by default runs in DRY-RUN mode (validate_only). Re-run with
    confirm=true to apply. REMOVED is irreversible.

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
    resource_name = (
        f"customers/{customer_id}/adGroupAds/{ad_group_id}~{ad_id}"
    )
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
) -> List[Dict[str, Any]]:
    """Convenience helper: lists campaigns with id, name, status, budget.

    Useful before calling the write tools to find campaign ids.

    Args:
        customer_id: The client account id (digits only, no hyphens).
        status: Optional filter: ENABLED or PAUSED.
        include_removed: Include REMOVED campaigns (default False).
        limit: Max rows (default 100).
    """
    customer_id = _clean_customer_id(customer_id)
    ga_service = utils.get_googleads_service("GoogleAdsService")

    conditions = []
    if status:
        conditions.append(f"campaign.status = '{status.upper()}'")
    elif not include_removed:
        conditions.append("campaign.status != 'REMOVED'")
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    query = (
        "SELECT campaign.id, campaign.name, campaign.status, "
        "campaign.advertising_channel_type, campaign_budget.amount_micros "
        f"FROM campaign {where} ORDER BY campaign.status ASC, "
        f"campaign.name ASC LIMIT {int(limit)}"
    )
    try:
        rows = ga_service.search(customer_id=customer_id, query=query)
        return [
            {
                "id": str(row.campaign.id),
                "name": row.campaign.name,
                "status": row.campaign.status.name,
                "channel_type": row.campaign.advertising_channel_type.name,
                "daily_budget": row.campaign_budget.amount_micros / _MICROS,
            }
            for row in rows
        ]
    except GoogleAdsException as ex:
        _raise_tool_error(ex)


@mutate_mcp.tool(annotations=_WRITE_ANNOTATIONS)
def campaign_set_conversion_goals(
    customer_id: str,
    campaign_id: str,
    biddable_categories: List[str],
    confirm: bool = False,
) -> Dict[str, Any]:
    """Sets CAMPAIGN-SPECIFIC standard conversion goals (e.g. Purchases).

    Switches the campaign to campaign-level goals and makes the given
    category goals biddable while turning the rest off — mirrors the UI
    "Campaign-specific: <category>" setting. biddable_categories: e.g.
    ["PURCHASE"], ["PURCHASE","ADD_TO_CART"].

    SAFETY: dry-run by default (validate_only); re-run with confirm=true.
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

    changed = {"biddable_on": [], "biddable_off": []}
    if confirm:
        ga_service = utils.get_googleads_service("GoogleAdsService")
        ccg_service = utils.get_googleads_service(
            "CampaignConversionGoalService"
        )
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
            (changed["biddable_on"] if want else changed["biddable_off"]).append(cat)
        if ccg_req.operations:
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
    return _preview_or_done(
        confirm, "campaign_set_conversion_goals", details
    )


@mutate_mcp.tool(annotations=_WRITE_ANNOTATIONS)
def campaign_set_custom_conversion_goal(
    customer_id: str,
    campaign_id: str,
    custom_conversion_goal_id: str,
    confirm: bool = False,
) -> Dict[str, Any]:
    """Points a campaign at a CUSTOM conversion goal (instead of the
    account-default goals).

    Find goal ids via search on resource custom_conversion_goal, or copy
    from a sibling campaign via resource conversion_goal_campaign_config.
    SAFETY: dry-run by default (validate_only); re-run with confirm=true.

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
    service = utils.get_googleads_service(
        "ConversionGoalCampaignConfigService"
    )

    operation = client.get_type("ConversionGoalCampaignConfigOperation")
    config = operation.update
    config.resource_name = (
        f"customers/{customer_id}/conversionGoalCampaignConfigs/{campaign_id}"
    )
    config.goal_config_level = (
        client.enums.GoalConfigLevelEnum.CAMPAIGN
    )
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
    # custom goal is used for bidding (the UI template does the same).
    disabled_categories = 0
    if confirm:
        ga_service = utils.get_googleads_service("GoogleAdsService")
        ccg_service = utils.get_googleads_service(
            "CampaignConversionGoalService"
        )
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
            if rows:
                ccg_request = client.get_type(
                    "MutateCampaignConversionGoalsRequest"
                )
                ccg_request.customer_id = customer_id
                for row in rows:
                    op = client.get_type("CampaignConversionGoalOperation")
                    goal = op.update
                    goal.resource_name = (
                        row.campaign_conversion_goal.resource_name
                    )
                    goal.biddable = False
                    client.copy_from(
                        op.update_mask,
                        field_mask_pb2.FieldMask(paths=["biddable"]),
                    )
                    ccg_request.operations.append(op)
                ccg_service.mutate_campaign_conversion_goals(
                    request=ccg_request
                )
                disabled_categories = len(rows)
        except GoogleAdsException as ex:
            _raise_tool_error(ex)

    details: Dict[str, Any] = {
        "customer_id": customer_id,
        "campaign_id": str(campaign_id),
        "custom_conversion_goal_id": str(custom_conversion_goal_id),
        "goal_config_level": "CAMPAIGN",
        "disabled_category_goals": disabled_categories,
    }
    if confirm:
        details["updated_resource"] = response.results[0].resource_name
    return _preview_or_done(
        confirm, "campaign_set_custom_conversion_goal", details
    )
