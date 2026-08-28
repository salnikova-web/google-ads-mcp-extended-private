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

"""Performance Max tools for the Google Ads MCP server (write extension).

Covers the PMax workflow: campaign creation, full asset-group creation in a
single request (text assets + images + videos), editing asset groups
(add/remove assets, status, urls), and audience / search-theme signals.

Image and YouTube assets are shared account-wide — upload them with
demandgen_asset_upload_image / demandgen_asset_create_youtube_video and pass
the asset ids here.

Safety model: identical to ads_mcp.tools.mutate — every write tool accepts
``confirm`` (default ``False`` = validate_only dry-run preview).
"""

import time
from typing import Any, Dict, List, Optional

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from google.ads.googleads.errors import GoogleAdsException

import ads_mcp.utils as utils
from ads_mcp.tools.mutate import (
    _clean_customer_id,
    _preview_or_done,
    _raise_tool_error,
    _to_micros,
)

pmax_mcp = FastMCP("pmax")

_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False)
_READ = ToolAnnotations(readOnlyHint=True)

_TEXT_FIELD_TYPES = ("HEADLINE", "LONG_HEADLINE", "DESCRIPTION")
_MEDIA_FIELD_TYPES = (
    "MARKETING_IMAGE",
    "SQUARE_MARKETING_IMAGE",
    "PORTRAIT_MARKETING_IMAGE",
    "TALL_PORTRAIT_MARKETING_IMAGE",
    "LOGO",
    "YOUTUBE_VIDEO",
)

# Google raised the per-asset-group video cap from 5 to 15 (2025/26).
_MAX_ASSET_GROUP_VIDEOS = 15


def _check_len(items: List[str], max_len: int, label: str) -> None:
    bad = [i for i in items if len(i) > max_len]
    if bad:
        raise ToolError(f"{label} over {max_len} chars: {bad}")


def _link_asset_op(client, customer_id, asset_group_rn, asset_rn, field_type):
    op = client.get_type("MutateOperation")
    aga = op.asset_group_asset_operation.create
    aga.asset_group = asset_group_rn
    aga.asset = asset_rn
    aga.field_type = client.enums.AssetFieldTypeEnum[field_type]
    return op


@pmax_mcp.tool(annotations=_WRITE)
def campaign_create(
    customer_id: str,
    name: str,
    daily_budget: float,
    bidding_strategy: str = "MAXIMIZE_CONVERSIONS",
    target_cpa: Optional[float] = None,
    target_roas: Optional[float] = None,
    business_name: Optional[str] = None,
    logo_asset_id: Optional[str] = None,
    merchant_id: Optional[str] = None,
    feed_label: Optional[str] = None,
    tracking_url_template: Optional[str] = None,
    final_url_suffix: Optional[str] = None,
    status: str = "PAUSED",
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    confirm: bool = False,
) -> Dict[str, Any]:
    """Creates a Performance Max campaign with a dedicated daily budget.

    Optional tracking_url_template / final_url_suffix set UTM tracking at
    creation (recommended for web funnels). NOTE: the campaign is
    created with ACCOUNT-DEFAULT conversion goals — attach the product's
    custom goal with campaign_set_custom_conversion_goal right after.

    NOTE: accounts with Brand Guidelines enabled (Google default since 2025)
    REQUIRE business_name and logo_asset_id (square logo, upload first via
    demandgen_asset_upload_image) at campaign creation.

    A PMax campaign needs at least one asset group before it can serve —
    create it next with pmax_asset_group_create. SAFETY: dry-run by default
    (validate_only); re-run with confirm=true. Created PAUSED by default.

    Args:
        customer_id: The client account id (digits only, no hyphens).
        name: Campaign name (unique within the account).
        daily_budget: Daily budget in account currency.
        bidding_strategy: MAXIMIZE_CONVERSIONS (optional target_cpa) or
            MAXIMIZE_CONVERSION_VALUE (optional target_roas).
        target_cpa: Optional target CPA in account currency.
        target_roas: Optional target ROAS as decimal (3.5 = 350%).
        business_name: Brand name (max 25 chars) — linked as campaign-level
            BUSINESS_NAME asset (required with Brand Guidelines).
        logo_asset_id: Asset id of a square logo image (required with
            Brand Guidelines).
        status: PAUSED (default) or ENABLED.
        confirm: False = dry-run preview (default), True = apply.
    """
    customer_id = _clean_customer_id(customer_id)
    bidding_strategy = bidding_strategy.upper()
    status = status.upper()
    if bidding_strategy not in (
        "MAXIMIZE_CONVERSIONS",
        "MAXIMIZE_CONVERSION_VALUE",
    ):
        raise ToolError(
            "PMax supports MAXIMIZE_CONVERSIONS or MAXIMIZE_CONVERSION_VALUE"
        )
    if status not in ("PAUSED", "ENABLED"):
        raise ToolError("status must be PAUSED or ENABLED")
    if daily_budget <= 0:
        raise ToolError("daily_budget must be positive")

    client = utils.get_googleads_client()
    ga_service = utils.get_googleads_service("GoogleAdsService")

    budget_temp_rn = f"customers/{customer_id}/campaignBudgets/-1"

    budget_op = client.get_type("MutateOperation")
    budget = budget_op.campaign_budget_operation.create
    budget.resource_name = budget_temp_rn
    budget.name = f"{name} budget {int(time.time())}"
    budget.amount_micros = _to_micros(daily_budget)
    budget.delivery_method = client.enums.BudgetDeliveryMethodEnum.STANDARD
    budget.explicitly_shared = False

    campaign_temp_rn = f"customers/{customer_id}/campaigns/-2"

    campaign_op = client.get_type("MutateOperation")
    campaign = campaign_op.campaign_operation.create
    campaign.resource_name = campaign_temp_rn
    campaign.name = name
    if merchant_id:
        campaign.shopping_setting.merchant_id = int(merchant_id)
        if feed_label:
            campaign.shopping_setting.feed_label = feed_label
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
    campaign.advertising_channel_type = (
        client.enums.AdvertisingChannelTypeEnum.PERFORMANCE_MAX
    )

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
    else:
        if target_roas is not None:
            campaign.maximize_conversion_value.target_roas = float(target_roas)
        else:
            client.copy_from(
                campaign.maximize_conversion_value,
                client.get_type("MaximizeConversionValue"),
            )

    operations = [budget_op, campaign_op]

    if business_name:
        if len(business_name) > 25:
            raise ToolError("business_name max 25 chars")
        bn_rn = f"customers/{customer_id}/assets/-3"
        bn_op = client.get_type("MutateOperation")
        bn_asset = bn_op.asset_operation.create
        bn_asset.resource_name = bn_rn
        bn_asset.text_asset.text = business_name
        bn_link_op = client.get_type("MutateOperation")
        bn_link = bn_link_op.campaign_asset_operation.create
        bn_link.campaign = campaign_temp_rn
        bn_link.asset = bn_rn
        bn_link.field_type = client.enums.AssetFieldTypeEnum.BUSINESS_NAME
        operations += [bn_op, bn_link_op]

    if logo_asset_id:
        logo_link_op = client.get_type("MutateOperation")
        logo_link = logo_link_op.campaign_asset_operation.create
        logo_link.campaign = campaign_temp_rn
        logo_link.asset = f"customers/{customer_id}/assets/{logo_asset_id}"
        logo_link.field_type = client.enums.AssetFieldTypeEnum.LOGO
        operations += [logo_link_op]

    request = client.get_type("MutateGoogleAdsRequest")
    request.customer_id = customer_id
    request.mutate_operations.extend(operations)
    request.validate_only = not confirm

    try:
        response = ga_service.mutate(request=request)
    except GoogleAdsException as ex:
        _raise_tool_error(ex)

    details: Dict[str, Any] = {
        "customer_id": customer_id,
        "campaign_name": name,
        "business_name": business_name,
        "logo_asset_id": logo_asset_id,
        "channel_type": "PERFORMANCE_MAX",
        "daily_budget": daily_budget,
        "bidding_strategy": bidding_strategy,
        "target_cpa": target_cpa,
        "target_roas": target_roas,
        "status": status,
        "next_step": "Create an asset group with pmax_asset_group_create",
    }
    if confirm:
        details["created_resources"] = [
            r.campaign_budget_result.resource_name
            or r.campaign_result.resource_name
            for r in response.mutate_operation_responses
        ]
    return _preview_or_done(confirm, "pmax_campaign_create", details)


@pmax_mcp.tool(annotations=_WRITE)
def campaign_update_bidding(
    customer_id: str,
    campaign_id: str,
    target_cpa: Optional[float] = None,
    target_roas: Optional[float] = None,
    confirm: bool = False,
) -> Dict[str, Any]:
    """Updates the bidding target (tCPA or tROAS) of a PMax campaign.

    Pass exactly one of target_cpa / target_roas; the target must be
    positive (a target cannot be cleared here — switch the strategy
    instead). SAFETY: dry-run by default; re-run with confirm=true.

    Args:
        customer_id: The client account id (digits only, no hyphens).
        campaign_id: The numeric id of the campaign.
        target_cpa: New target CPA in account currency.
        target_roas: New target ROAS as decimal (3.5 = 350%).
        confirm: False = dry-run preview (default), True = apply.
    """
    customer_id = _clean_customer_id(customer_id)
    if (target_cpa is None) == (target_roas is None):
        raise ToolError("Pass exactly one of target_cpa or target_roas")
    if target_cpa is not None and target_cpa <= 0:
        raise ToolError("target_cpa must be positive")
    if target_roas is not None and target_roas <= 0:
        raise ToolError("target_roas must be positive")

    client = utils.get_googleads_client()
    campaign_service = utils.get_googleads_service("CampaignService")

    operation = client.get_type("CampaignOperation")
    campaign = operation.update
    campaign.resource_name = f"customers/{customer_id}/campaigns/{campaign_id}"
    # An explicit leaf path is required: a value-derived mask drops fields
    # left at their proto default, so the update would silently no-op.
    if target_cpa is not None:
        campaign.maximize_conversions.target_cpa_micros = _to_micros(target_cpa)
        operation.update_mask.paths.append(
            "maximize_conversions.target_cpa_micros"
        )
    else:
        campaign.maximize_conversion_value.target_roas = float(target_roas)
        operation.update_mask.paths.append(
            "maximize_conversion_value.target_roas"
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
        "new_target_cpa": target_cpa,
        "new_target_roas": target_roas,
    }
    if confirm:
        details["updated_resource"] = response.results[0].resource_name
    return _preview_or_done(confirm, "pmax_campaign_update_bidding", details)


@pmax_mcp.tool(annotations=_WRITE)
def asset_group_create(
    customer_id: str,
    campaign_id: str,
    name: str,
    final_url: str,
    headlines: List[str],
    long_headlines: List[str],
    descriptions: List[str],
    marketing_image_asset_ids: List[str],
    square_image_asset_ids: List[str],
    logo_asset_ids: List[str] = [],
    business_name: Optional[str] = None,
    youtube_video_asset_ids: List[str] = [],
    path1: Optional[str] = None,
    path2: Optional[str] = None,
    status: str = "PAUSED",
    confirm: bool = False,
) -> Dict[str, Any]:
    """Creates a complete PMax asset group in one request.

    Text assets (headlines, descriptions, business name) are created
    automatically; images/videos must be uploaded beforehand
    (demandgen_asset_upload_image / demandgen_asset_create_youtube_video).

    Google minimums enforced here: 3-15 headlines (max 30 chars), 1-5 long
    headlines (max 90), 2-5 descriptions (max 90, at least one under 60),
    business name (max 25), >=1 landscape image (1.91:1, min 600x314),
    >=1 square image (1:1, min 300x300), >=1 logo (1:1, min 128x128).
    Videos are optional (Google auto-generates one if omitted).

    SAFETY: dry-run by default (validate_only); re-run with confirm=true.

    Args:
        customer_id: The client account id (digits only, no hyphens).
        campaign_id: The numeric id of the PMax campaign.
        name: Asset group name (unique within the campaign).
        final_url: Landing page URL.
        headlines: 3-15 headlines, max 30 chars each.
        long_headlines: 1-5 long headlines, max 90 chars each.
        descriptions: 2-5 descriptions, max 90 chars each (one should be
            60 chars or less).
        business_name: Brand name, max 25 chars.
        marketing_image_asset_ids: Landscape image asset ids.
        square_image_asset_ids: Square image asset ids.
        logo_asset_ids: Logo image asset ids.
        youtube_video_asset_ids: Optional YouTube video asset ids
            (up to 15 per asset group — Google raised the cap from 5).
        path1: Optional display path 1 (max 15 chars).
        path2: Optional display path 2 (max 15 chars, requires path1).
        status: PAUSED (default) or ENABLED.
        confirm: False = dry-run preview (default), True = apply.
    """
    customer_id = _clean_customer_id(customer_id)
    status = status.upper()
    if status not in ("PAUSED", "ENABLED"):
        raise ToolError("status must be PAUSED or ENABLED")
    if not (3 <= len(headlines) <= 15):
        raise ToolError("headlines: 3-15 required")
    if not (1 <= len(long_headlines) <= 5):
        raise ToolError("long_headlines: 1-5 required")
    if not (2 <= len(descriptions) <= 5):
        raise ToolError("descriptions: 2-5 required")
    if not marketing_image_asset_ids or not square_image_asset_ids:
        raise ToolError(
            "At least one landscape AND one square image asset id required"
        )
    # NOTE: with Brand Guidelines (Google default since 2025) business name
    # and logo are linked at CAMPAIGN level and MUST NOT be linked at asset
    # group level. Leave business_name/logo_asset_ids empty in that case.
    if business_name and len(business_name) > 25:
        raise ToolError("business_name max 25 chars")
    _check_len(headlines, 30, "Headlines")
    _check_len(long_headlines, 90, "Long headlines")
    _check_len(descriptions, 90, "Descriptions")
    if not any(len(d) <= 60 for d in descriptions):
        raise ToolError("At least one description must be 60 chars or less")
    if len(youtube_video_asset_ids or []) > _MAX_ASSET_GROUP_VIDEOS:
        raise ToolError(
            f"youtube_video_asset_ids: up to {_MAX_ASSET_GROUP_VIDEOS} "
            "videos per asset group"
        )

    client = utils.get_googleads_client()
    ga_service = utils.get_googleads_service("GoogleAdsService")

    asset_group_rn = f"customers/{customer_id}/assetGroups/-1"
    # Google requires this exact operation order in the bulk request:
    # 1) new text assets, 2) the asset group, 3) ALL asset group asset links.
    asset_ops: List[Any] = []
    link_ops: List[Any] = []

    ag_op = client.get_type("MutateOperation")
    ag = ag_op.asset_group_operation.create
    ag.resource_name = asset_group_rn
    ag.campaign = f"customers/{customer_id}/campaigns/{campaign_id}"
    ag.name = name
    ag.final_urls.append(final_url)
    ag.status = client.enums.AssetGroupStatusEnum[status]
    if path1:
        ag.path1 = path1
        if path2:
            ag.path2 = path2

    temp_id = -2

    def _text_asset_ops(texts: List[str], field_type: str):
        nonlocal temp_id
        for text in texts:
            asset_rn = f"customers/{customer_id}/assets/{temp_id}"
            temp_id -= 1
            a_op = client.get_type("MutateOperation")
            asset = a_op.asset_operation.create
            asset.resource_name = asset_rn
            asset.text_asset.text = text
            asset_ops.append(a_op)
            link_ops.append(
                _link_asset_op(
                    client, customer_id, asset_group_rn, asset_rn, field_type
                )
            )

    _text_asset_ops(headlines, "HEADLINE")
    _text_asset_ops(long_headlines, "LONG_HEADLINE")
    _text_asset_ops(descriptions, "DESCRIPTION")
    if business_name:
        _text_asset_ops([business_name], "BUSINESS_NAME")

    # 3. Existing media assets links.
    def _media_links(asset_ids: List[str], field_type: str):
        for asset_id in asset_ids:
            asset_rn = f"customers/{customer_id}/assets/{asset_id}"
            link_ops.append(
                _link_asset_op(
                    client, customer_id, asset_group_rn, asset_rn, field_type
                )
            )

    _media_links(marketing_image_asset_ids, "MARKETING_IMAGE")
    _media_links(square_image_asset_ids, "SQUARE_MARKETING_IMAGE")
    if logo_asset_ids:
        _media_links(logo_asset_ids, "LOGO")
    if youtube_video_asset_ids:
        _media_links(youtube_video_asset_ids, "YOUTUBE_VIDEO")

    operations: List[Any] = asset_ops + [ag_op] + link_ops

    request = client.get_type("MutateGoogleAdsRequest")
    request.customer_id = customer_id
    request.mutate_operations.extend(operations)
    request.validate_only = not confirm

    try:
        response = ga_service.mutate(request=request)
    except GoogleAdsException as ex:
        _raise_tool_error(ex)

    details: Dict[str, Any] = {
        "customer_id": customer_id,
        "campaign_id": str(campaign_id),
        "asset_group_name": name,
        "final_url": final_url,
        "texts": {
            "headlines": len(headlines),
            "long_headlines": len(long_headlines),
            "descriptions": len(descriptions),
        },
        "media": {
            "landscape_images": len(marketing_image_asset_ids),
            "square_images": len(square_image_asset_ids),
            "logos": len(logo_asset_ids),
            "videos": len(youtube_video_asset_ids or []),
        },
        "status": status,
        "operations_count": len(operations),
    }
    if confirm:
        details["created_asset_group"] = response.mutate_operation_responses[
            0
        ].asset_group_result.resource_name
    return _preview_or_done(confirm, "pmax_asset_group_create", details)


@pmax_mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True)
)
def asset_group_update(
    customer_id: str,
    asset_group_id: str,
    status: Optional[str] = None,
    new_name: Optional[str] = None,
    final_url: Optional[str] = None,
    confirm: bool = False,
) -> Dict[str, Any]:
    """Updates an asset group: status, name and/or final URL.

    Pass only the fields to change. status="REMOVED" DELETES the asset
    group. SAFETY: dry-run by default; re-run with confirm=true.

    Args:
        customer_id: The client account id (digits only, no hyphens).
        asset_group_id: The numeric id of the asset group.
        status: Optional: ENABLED, PAUSED or REMOVED (REMOVED deletes it).
        new_name: Optional new name (must not be blank).
        final_url: Optional new landing page URL (replaces existing).
        confirm: False = dry-run preview (default), True = apply.
    """
    customer_id = _clean_customer_id(customer_id)
    if status is None and new_name is None and final_url is None:
        raise ToolError(
            "Nothing to update: pass at least one of status, new_name, "
            "final_url"
        )
    if new_name is not None and not new_name.strip():
        raise ToolError("new_name must be a non-empty string")

    client = utils.get_googleads_client()
    ag_service = utils.get_googleads_service("AssetGroupService")

    operation = client.get_type("AssetGroupOperation")
    if status is not None and status.upper() == "REMOVED":
        operation.remove = (
            f"customers/{customer_id}/assetGroups/{asset_group_id}"
        )
        request = client.get_type("MutateAssetGroupsRequest")
        request.customer_id = customer_id
        request.operations.append(operation)
        request.validate_only = not confirm
        try:
            response = ag_service.mutate_asset_groups(request=request)
        except GoogleAdsException as ex:
            _raise_tool_error(ex)
        removed: Dict[str, Any] = {
            "customer_id": customer_id,
            "asset_group_id": str(asset_group_id),
            "new_status": "REMOVED",
        }
        if confirm:
            removed["removed_resource"] = response.results[0].resource_name
        return _preview_or_done(confirm, "pmax_asset_group_update", removed)
    ag = operation.update
    ag.resource_name = f"customers/{customer_id}/assetGroups/{asset_group_id}"
    # Explicit leaf paths, built only for the fields actually passed: a
    # value-derived mask would drop anything left at its proto default (an
    # empty name), and an unconditional list would wipe untouched fields.
    if status is not None:
        status = status.upper()
        if status not in ("ENABLED", "PAUSED"):
            raise ToolError("status must be ENABLED, PAUSED or REMOVED")
        ag.status = client.enums.AssetGroupStatusEnum[status]
        operation.update_mask.paths.append("status")
    if new_name is not None:
        ag.name = new_name
        operation.update_mask.paths.append("name")
    if final_url is not None:
        ag.final_urls.append(final_url)
        operation.update_mask.paths.append("final_urls")

    request = client.get_type("MutateAssetGroupsRequest")
    request.customer_id = customer_id
    request.operations.append(operation)
    request.validate_only = not confirm

    try:
        response = ag_service.mutate_asset_groups(request=request)
    except GoogleAdsException as ex:
        _raise_tool_error(ex)

    details: Dict[str, Any] = {
        "customer_id": customer_id,
        "asset_group_id": str(asset_group_id),
        "new_status": status,
        "new_name": new_name,
        "new_final_url": final_url,
    }
    if confirm:
        details["updated_resource"] = response.results[0].resource_name
    return _preview_or_done(confirm, "pmax_asset_group_update", details)


@pmax_mcp.tool(annotations=_WRITE)
def asset_group_add_texts(
    customer_id: str,
    asset_group_id: str,
    texts: List[str],
    field_type: str,
    confirm: bool = False,
) -> Dict[str, Any]:
    """Adds new text assets (headlines/descriptions) to an existing asset group.

    Creates the text assets and links them in one request. Mind the totals:
    max 15 HEADLINE, 5 LONG_HEADLINE, 5 DESCRIPTION per asset group.

    SAFETY: dry-run by default; re-run with confirm=true.

    Args:
        customer_id: The client account id (digits only, no hyphens).
        asset_group_id: The numeric id of the asset group.
        texts: Text values to add.
        field_type: HEADLINE (max 30 chars), LONG_HEADLINE (max 90) or
            DESCRIPTION (max 90).
        confirm: False = dry-run preview (default), True = apply.
    """
    customer_id = _clean_customer_id(customer_id)
    field_type = field_type.upper()
    if field_type not in _TEXT_FIELD_TYPES:
        raise ToolError(f"field_type must be one of {_TEXT_FIELD_TYPES}")
    if not texts:
        raise ToolError("texts list is empty")
    max_len = 30 if field_type == "HEADLINE" else 90
    _check_len(texts, max_len, field_type)

    client = utils.get_googleads_client()
    ga_service = utils.get_googleads_service("GoogleAdsService")

    asset_group_rn = f"customers/{customer_id}/assetGroups/{asset_group_id}"
    operations = []
    temp_id = -1
    for text in texts:
        asset_rn = f"customers/{customer_id}/assets/{temp_id}"
        temp_id -= 1
        a_op = client.get_type("MutateOperation")
        asset = a_op.asset_operation.create
        asset.resource_name = asset_rn
        asset.text_asset.text = text
        operations.append(a_op)
        operations.append(
            _link_asset_op(
                client, customer_id, asset_group_rn, asset_rn, field_type
            )
        )

    request = client.get_type("MutateGoogleAdsRequest")
    request.customer_id = customer_id
    request.mutate_operations.extend(operations)
    request.validate_only = not confirm

    try:
        ga_service.mutate(request=request)
    except GoogleAdsException as ex:
        _raise_tool_error(ex)

    details: Dict[str, Any] = {
        "customer_id": customer_id,
        "asset_group_id": str(asset_group_id),
        "field_type": field_type,
        "texts": texts,
        "count": len(texts),
    }
    return _preview_or_done(confirm, "pmax_asset_group_add_texts", details)


@pmax_mcp.tool(annotations=_WRITE)
def asset_group_add_media(
    customer_id: str,
    asset_group_id: str,
    asset_ids: List[str],
    field_type: str,
    confirm: bool = False,
) -> Dict[str, Any]:
    """Links existing image/video assets to an asset group.

    Upload images first with demandgen_asset_upload_image (or register
    videos with demandgen_asset_create_youtube_video). SAFETY: dry-run by
    default; re-run with confirm=true.

    Args:
        customer_id: The client account id (digits only, no hyphens).
        asset_group_id: The numeric id of the asset group.
        asset_ids: Asset ids to link.
        field_type: MARKETING_IMAGE, SQUARE_MARKETING_IMAGE,
            PORTRAIT_MARKETING_IMAGE, LOGO or YOUTUBE_VIDEO. For
            YOUTUBE_VIDEO the total (existing + new) may not exceed 15.
        confirm: False = dry-run preview (default), True = apply.
    """
    customer_id = _clean_customer_id(customer_id)
    # The resource name below is spliced into a GAQL literal, so the id has
    # to be numeric before it gets there.
    asset_group_id = utils.gaql_id(asset_group_id)
    field_type = field_type.upper()
    if field_type not in _MEDIA_FIELD_TYPES:
        raise ToolError(f"field_type must be one of {_MEDIA_FIELD_TYPES}")
    if not asset_ids:
        raise ToolError("asset_ids list is empty")

    client = utils.get_googleads_client()
    ga_service = utils.get_googleads_service("GoogleAdsService")

    asset_group_rn = f"customers/{customer_id}/assetGroups/{asset_group_id}"

    # Enforce the per-asset-group video cap (15) counting assets already
    # linked, so a partial batch doesn't fail cryptically mid-way.
    if field_type == "YOUTUBE_VIDEO":
        try:
            existing = sum(
                1
                for _ in ga_service.search(
                    customer_id=customer_id,
                    query=(
                        "SELECT asset_group_asset.asset "
                        "FROM asset_group_asset "
                        "WHERE asset_group_asset.asset_group = "
                        f"'{asset_group_rn}' "
                        "AND asset_group_asset.field_type = 'YOUTUBE_VIDEO' "
                        "AND asset_group_asset.status != 'REMOVED'"
                    ),
                )
            )
        except GoogleAdsException as ex:
            _raise_tool_error(ex)
        if existing + len(asset_ids) > _MAX_ASSET_GROUP_VIDEOS:
            raise ToolError(
                f"Asset group already has {existing} video(s); adding "
                f"{len(asset_ids)} would exceed the "
                f"{_MAX_ASSET_GROUP_VIDEOS}-video limit"
            )

    operations = []
    for asset_id in asset_ids:
        asset_rn = f"customers/{customer_id}/assets/{asset_id}"
        operations.append(
            _link_asset_op(
                client, customer_id, asset_group_rn, asset_rn, field_type
            )
        )

    request = client.get_type("MutateGoogleAdsRequest")
    request.customer_id = customer_id
    request.mutate_operations.extend(operations)
    request.validate_only = not confirm

    try:
        ga_service.mutate(request=request)
    except GoogleAdsException as ex:
        _raise_tool_error(ex)

    details: Dict[str, Any] = {
        "customer_id": customer_id,
        "asset_group_id": str(asset_group_id),
        "field_type": field_type,
        "asset_ids": asset_ids,
        "count": len(asset_ids),
    }
    return _preview_or_done(confirm, "pmax_asset_group_add_media", details)


@pmax_mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True)
)
def asset_group_remove_asset(
    customer_id: str,
    asset_group_id: str,
    asset_id: str,
    field_type: str,
    confirm: bool = False,
) -> Dict[str, Any]:
    """Unlinks an asset from an asset group (the asset itself is kept).

    Mind Google minimums — removing below the minimum (e.g. last logo)
    will be rejected. SAFETY: dry-run by default; re-run with confirm=true.

    Args:
        customer_id: The client account id (digits only, no hyphens).
        asset_group_id: The numeric id of the asset group.
        asset_id: The numeric id of the linked asset.
        field_type: The field type it is linked as (e.g. HEADLINE,
            MARKETING_IMAGE, LOGO, YOUTUBE_VIDEO...).
        confirm: False = dry-run preview (default), True = apply.
    """
    customer_id = _clean_customer_id(customer_id)
    field_type = field_type.upper()

    client = utils.get_googleads_client()
    aga_service = utils.get_googleads_service("AssetGroupAssetService")

    operation = client.get_type("AssetGroupAssetOperation")
    operation.remove = (
        f"customers/{customer_id}/assetGroupAssets/"
        f"{asset_group_id}~{asset_id}~{field_type}"
    )

    request = client.get_type("MutateAssetGroupAssetsRequest")
    request.customer_id = customer_id
    request.operations.append(operation)
    request.validate_only = not confirm

    try:
        response = aga_service.mutate_asset_group_assets(request=request)
    except GoogleAdsException as ex:
        _raise_tool_error(ex)

    details: Dict[str, Any] = {
        "customer_id": customer_id,
        "asset_group_id": str(asset_group_id),
        "asset_id": str(asset_id),
        "field_type": field_type,
    }
    if confirm:
        details["removed_resource"] = response.results[0].resource_name
    return _preview_or_done(confirm, "pmax_asset_group_remove_asset", details)


@pmax_mcp.tool(annotations=_WRITE)
def signal_attach(
    customer_id: str,
    asset_group_id: str,
    audience_id: Optional[str] = None,
    search_theme: Optional[str] = None,
    confirm: bool = False,
) -> Dict[str, Any]:
    """Adds an audience signal OR a search theme to a PMax asset group.

    Pass exactly one of audience_id / search_theme. Find audience ids via
    search on resource `audience`. SAFETY: dry-run by default; re-run with
    confirm=true.

    Args:
        customer_id: The client account id (digits only, no hyphens).
        asset_group_id: The numeric id of the asset group.
        audience_id: The numeric id of an Audience resource.
        search_theme: A search theme phrase (like a broad keyword,
            e.g. "weight loss plan for women").
        confirm: False = dry-run preview (default), True = apply.
    """
    customer_id = _clean_customer_id(customer_id)
    if (audience_id is None) == (search_theme is None):
        raise ToolError("Pass exactly one of audience_id or search_theme")

    client = utils.get_googleads_client()
    ags_service = utils.get_googleads_service("AssetGroupSignalService")

    operation = client.get_type("AssetGroupSignalOperation")
    signal = operation.create
    signal.asset_group = f"customers/{customer_id}/assetGroups/{asset_group_id}"
    if audience_id is not None:
        signal.audience.audience = (
            f"customers/{customer_id}/audiences/{audience_id}"
        )
    else:
        signal.search_theme.text = search_theme

    request = client.get_type("MutateAssetGroupSignalsRequest")
    request.customer_id = customer_id
    request.operations.append(operation)
    request.validate_only = not confirm

    try:
        response = ags_service.mutate_asset_group_signals(request=request)
    except GoogleAdsException as ex:
        _raise_tool_error(ex)

    details: Dict[str, Any] = {
        "customer_id": customer_id,
        "asset_group_id": str(asset_group_id),
        "audience_id": audience_id,
        "search_theme": search_theme,
    }
    if confirm:
        details["created_resource"] = response.results[0].resource_name
    return _preview_or_done(confirm, "pmax_signal_attach", details)


@pmax_mcp.tool(annotations=_WRITE)
def asset_group_set_listing_filter(
    customer_id: str,
    asset_group_id: str,
    custom_label_index: int = 0,
    include_values: List[str] = [],
    exclude_others: bool = True,
    confirm: bool = False,
) -> Dict[str, Any]:
    """Builds a listing group filter tree subdivided by a product custom
    label (custom_label_0..4).

    Creates a root SUBDIVISION on the given custom label, one UNIT_INCLUDED
    per value in include_values, and (if exclude_others) a UNIT_EXCLUDED
    "everything else" node. Use to reproduce a collection-based retail PMax
    asset group instead of targeting the whole catalog.

    custom_label_index: 0-4 (0 = custom_label_0). SAFETY: dry-run by default
    (validate_only); re-run with confirm=true.
    """
    customer_id = _clean_customer_id(customer_id)
    if not include_values:
        raise ToolError("include_values must not be empty")
    if not (0 <= int(custom_label_index) <= 4):
        raise ToolError("custom_label_index must be 0-4")

    client = utils.get_googleads_client()
    lgf_service = utils.get_googleads_service(
        "AssetGroupListingGroupFilterService"
    )
    ag_rn = f"customers/{customer_id}/assetGroups/{asset_group_id}"
    idx_enum = client.enums.ListingGroupFilterCustomAttributeIndexEnum[
        f"INDEX{int(custom_label_index)}"
    ]

    operations = []
    root_rn = f"customers/{customer_id}/assetGroupListingGroupFilters/{asset_group_id}~-1"

    root_op = client.get_type("AssetGroupListingGroupFilterOperation")
    root = root_op.create
    root.resource_name = root_rn
    root.asset_group = ag_rn
    root.type_ = client.enums.ListingGroupFilterTypeEnum.SUBDIVISION
    root.listing_source = (
        client.enums.ListingGroupFilterListingSourceEnum.SHOPPING
    )
    operations.append(root_op)

    temp = -2
    for value in include_values:
        op = client.get_type("AssetGroupListingGroupFilterOperation")
        node = op.create
        node.resource_name = (
            f"customers/{customer_id}/assetGroupListingGroupFilters/"
            f"{asset_group_id}~{temp}"
        )
        temp -= 1
        node.asset_group = ag_rn
        node.parent_listing_group_filter = root_rn
        node.type_ = client.enums.ListingGroupFilterTypeEnum.UNIT_INCLUDED
        node.listing_source = (
            client.enums.ListingGroupFilterListingSourceEnum.SHOPPING
        )
        node.case_value.product_custom_attribute.index = idx_enum
        node.case_value.product_custom_attribute.value = value
        operations.append(op)

    # catch-all "everything else" node (empty value) — included or excluded
    other_op = client.get_type("AssetGroupListingGroupFilterOperation")
    other = other_op.create
    other.resource_name = (
        f"customers/{customer_id}/assetGroupListingGroupFilters/"
        f"{asset_group_id}~{temp}"
    )
    other.asset_group = ag_rn
    other.parent_listing_group_filter = root_rn
    other.listing_source = (
        client.enums.ListingGroupFilterListingSourceEnum.SHOPPING
    )
    other.type_ = (
        client.enums.ListingGroupFilterTypeEnum.UNIT_EXCLUDED
        if exclude_others
        else client.enums.ListingGroupFilterTypeEnum.UNIT_INCLUDED
    )
    other.case_value.product_custom_attribute.index = idx_enum
    operations.append(other_op)

    request = client.get_type("MutateAssetGroupListingGroupFiltersRequest")
    request.customer_id = customer_id
    request.operations.extend(operations)
    request.validate_only = not confirm
    try:
        response = lgf_service.mutate_asset_group_listing_group_filters(
            request=request
        )
    except GoogleAdsException as ex:
        _raise_tool_error(ex)

    details: Dict[str, Any] = {
        "customer_id": customer_id,
        "asset_group_id": str(asset_group_id),
        "custom_label_index": int(custom_label_index),
        "included_values": include_values,
        "exclude_others": exclude_others,
        "nodes": len(operations),
    }
    return _preview_or_done(
        confirm, "pmax_asset_group_set_listing_filter", details
    )


@pmax_mcp.tool(annotations=_WRITE)
def asset_group_set_all_products(
    customer_id: str,
    asset_group_id: str,
    confirm: bool = False,
) -> Dict[str, Any]:
    """Creates the root 'All products' listing group filter for a retail
    PMax asset group (required when the campaign is linked to a Merchant
    Center feed and you want to advertise the whole catalog).

    For subscription or lead-gen products this is normally NOT needed — only
    for e-commerce accounts with shopping feeds. SAFETY: dry-run by default
    (validate_only); re-run with confirm=true.

    Args:
        customer_id: The client account id (digits only, no hyphens).
        asset_group_id: The numeric id of the asset group.
        confirm: False = dry-run preview (default), True = apply.
    """
    customer_id = _clean_customer_id(customer_id)

    client = utils.get_googleads_client()
    lgf_service = utils.get_googleads_service(
        "AssetGroupListingGroupFilterService"
    )

    operation = client.get_type("AssetGroupListingGroupFilterOperation")
    lgf = operation.create
    lgf.asset_group = f"customers/{customer_id}/assetGroups/{asset_group_id}"
    lgf.type_ = client.enums.ListingGroupFilterTypeEnum.UNIT_INCLUDED
    lgf.listing_source = (
        client.enums.ListingGroupFilterListingSourceEnum.SHOPPING
    )

    request = client.get_type("MutateAssetGroupListingGroupFiltersRequest")
    request.customer_id = customer_id
    request.operations.append(operation)
    request.validate_only = not confirm

    try:
        response = lgf_service.mutate_asset_group_listing_group_filters(
            request=request
        )
    except GoogleAdsException as ex:
        _raise_tool_error(ex)

    details: Dict[str, Any] = {
        "customer_id": customer_id,
        "asset_group_id": str(asset_group_id),
        "listing_group": "All products (root, included)",
    }
    if confirm:
        details["created_resource"] = response.results[0].resource_name
    return _preview_or_done(
        confirm, "pmax_asset_group_set_all_products", details
    )


@pmax_mcp.tool(annotations=_READ)
def list_asset_groups(
    customer_id: str,
    campaign_id: Optional[str] = None,
    limit: int = 200,
) -> Dict[str, Any]:
    """Lists PMax asset groups: id, name, status, ad strength, final urls.

    Returns at most `limit` asset groups ordered by name. When the account
    has more, the list is cut and "truncated" is true — narrow it down with
    campaign_id or raise limit before concluding an asset group is missing.

    Args:
        customer_id: The client account id (digits only, no hyphens).
        campaign_id: Optional: only asset groups of this campaign.
        limit: Max asset groups returned (default 200).

    Returns:
        {"asset_groups": [...], "returned": n, "truncated": bool}
    """
    customer_id = _clean_customer_id(customer_id)
    limit = int(limit)
    if limit <= 0:
        raise ToolError("limit must be positive")
    ga_service = utils.get_googleads_service("GoogleAdsService")

    where = "WHERE asset_group.status != 'REMOVED'"
    if campaign_id:
        where += f" AND campaign.id = {int(campaign_id)}"
    # One row over the limit, so a cut list can be reported as such.
    query = (
        "SELECT asset_group.id, asset_group.name, asset_group.status, "
        "asset_group.ad_strength, asset_group.final_urls, campaign.id, "
        "campaign.name FROM asset_group " + where + " ORDER BY "
        f"asset_group.name ASC LIMIT {limit + 1}"
    )
    try:
        rows = list(ga_service.search(customer_id=customer_id, query=query))
    except GoogleAdsException as ex:
        _raise_tool_error(ex)

    asset_groups = [
        {
            "id": str(row.asset_group.id),
            "name": row.asset_group.name,
            "status": row.asset_group.status.name,
            "ad_strength": row.asset_group.ad_strength.name,
            "final_urls": list(row.asset_group.final_urls),
            "campaign_id": str(row.campaign.id),
            "campaign_name": row.campaign.name,
        }
        for row in rows[:limit]
    ]
    return {
        "asset_groups": asset_groups,
        "returned": len(asset_groups),
        "truncated": len(rows) > limit,
    }
