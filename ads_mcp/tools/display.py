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

"""Display campaign tools (Google Display Network) — write extension.

Workflow: upload images (demandgen_asset_upload_image) ->
display_campaign_create -> display_ad_group_create ->
display_ad_create_responsive.

Safety model: identical to ads_mcp.tools.mutate — every write tool accepts
``confirm`` (default ``False`` = validate_only dry-run preview).
"""

from typing import Annotated, Any, Dict, List, Optional

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field
from google.ads.googleads.errors import GoogleAdsException

import ads_mcp.utils as utils
from ads_mcp.tools._write_common import (
    _WRITE_ANNOTATIONS as _WRITE,
    _check_len,
    _clean_customer_id,
    _preview_or_done,
    _raise_tool_error,
    _to_micros,
    build_campaign_with_budget,
)

display_mcp = FastMCP("display")

# Schema-only aliases: advertise the accepted values in tools/list via
# json_schema_extra while runtime validation stays the existing lax
# .upper() + explicit ToolError checks (a true Literal would reject
# lowercase input that works today).
_STATUS_ENUM = Annotated[
    str, Field(json_schema_extra={"enum": ["PAUSED", "ENABLED"]})
]
_BIDDING_ENUM = Annotated[
    str,
    Field(
        json_schema_extra={
            "enum": [
                "MAXIMIZE_CONVERSIONS",
                "MAXIMIZE_CONVERSION_VALUE",
                "MAXIMIZE_CLICKS",
                "MANUAL_CPC",
            ]
        }
    ),
]


@display_mcp.tool(annotations=_WRITE)
def campaign_create(
    customer_id: str,
    name: str,
    daily_budget: float,
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
    """Creates a Display campaign (GDN) with a dedicated daily budget.

    Optional tracking_url_template / final_url_suffix set UTM tracking at
    creation (recommended for web funnels). NOTE: the campaign is
    created with ACCOUNT-DEFAULT conversion goals — attach the product's
    custom goal with mutate_campaign_set_custom_conversion_goal right
    after.

    SAFETY: dry-run by default (validate_only); re-run with confirm=true.
    Created PAUSED by default.

    Args:
        customer_id: The client account id (digits only, no hyphens).
        name: Campaign name (unique within the account).
        daily_budget: Daily budget in account currency.
        bidding_strategy: MAXIMIZE_CONVERSIONS (optional target_cpa),
            MAXIMIZE_CONVERSION_VALUE (optional target_roas),
            MAXIMIZE_CLICKS or MANUAL_CPC.
        target_cpa: Optional target CPA in account currency.
        target_roas: Optional target ROAS as decimal.
        start_date: "YYYY-MM-DD" (dashes required), account timezone;
            defaults to today.
        end_date: "YYYY-MM-DD" (dashes required), inclusive; omit for none.
        confirm: False = dry-run preview (default), True = apply.
    """
    customer_id = _clean_customer_id(customer_id)
    bidding_strategy = bidding_strategy.upper()
    status = status.upper()
    if bidding_strategy not in (
        "MAXIMIZE_CONVERSIONS",
        "MAXIMIZE_CONVERSION_VALUE",
        "MAXIMIZE_CLICKS",
        "MANUAL_CPC",
    ):
        raise ToolError(
            "bidding_strategy must be MAXIMIZE_CONVERSIONS, "
            "MAXIMIZE_CONVERSION_VALUE, MAXIMIZE_CLICKS or MANUAL_CPC"
        )
    if status not in ("PAUSED", "ENABLED"):
        raise ToolError("status must be PAUSED or ENABLED")
    if daily_budget <= 0:
        raise ToolError("daily_budget must be positive")

    client = utils.get_googleads_client()
    ga_service = utils.get_googleads_service("GoogleAdsService")

    budget_op, campaign_op, campaign = build_campaign_with_budget(
        client,
        customer_id,
        name,
        daily_budget,
        "DISPLAY",
        status,
        start_date=start_date,
        end_date=end_date,
        tracking_url_template=tracking_url_template,
        final_url_suffix=final_url_suffix,
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
    else:
        campaign.manual_cpc.enhanced_cpc_enabled = False

    request = client.get_type("MutateGoogleAdsRequest")
    request.customer_id = customer_id
    request.mutate_operations.extend([budget_op, campaign_op])
    request.validate_only = not confirm

    try:
        response = ga_service.mutate(request=request)
    except GoogleAdsException as ex:
        _raise_tool_error(ex)

    details: Dict[str, Any] = {
        "customer_id": customer_id,
        "campaign_name": name,
        "channel_type": "DISPLAY",
        "daily_budget": daily_budget,
        "bidding_strategy": bidding_strategy,
        "target_cpa": target_cpa,
        "target_roas": target_roas,
        "status": status,
    }
    if confirm:
        details["created_resources"] = [
            r.campaign_budget_result.resource_name
            or r.campaign_result.resource_name
            for r in response.mutate_operation_responses
        ]
    return _preview_or_done(confirm, "display_campaign_create", details)


@display_mcp.tool(annotations=_WRITE)
def ad_group_create(
    customer_id: str,
    campaign_id: str,
    name: str,
    cpc_bid: Optional[float] = None,
    status: _STATUS_ENUM = "PAUSED",
    confirm: bool = False,
) -> Dict[str, Any]:
    """Creates a DISPLAY_STANDARD ad group in a Display campaign.

    SAFETY: dry-run by default (validate_only); re-run with confirm=true.

    Args:
        customer_id: The client account id (digits only, no hyphens).
        campaign_id: The numeric id of the Display campaign.
        name: Ad group name.
        cpc_bid: Optional max CPC in account currency.
        confirm: False = dry-run preview (default), True = apply.
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
    ad_group.type_ = client.enums.AdGroupTypeEnum.DISPLAY_STANDARD
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
    return _preview_or_done(confirm, "display_ad_group_create", details)


@display_mcp.tool(annotations=_WRITE)
def ad_create_responsive(
    customer_id: str,
    ad_group_id: str,
    ad_name: str,
    business_name: str,
    final_url: str,
    headlines: List[str],
    long_headline: str,
    descriptions: List[str],
    marketing_image_asset_ids: List[str],
    square_image_asset_ids: List[str],
    logo_image_asset_ids: List[str] = [],
    youtube_video_asset_ids: List[str] = [],
    call_to_action_text: Optional[str] = None,
    tracking_url_template: Optional[str] = None,
    status: _STATUS_ENUM = "PAUSED",
    confirm: bool = False,
) -> Dict[str, Any]:
    """Creates a Responsive Display Ad (RDA).

    Upload images first with demandgen_asset_upload_image. SAFETY: dry-run
    by default (validate_only); re-run with confirm=true.

    Args:
        customer_id: The client account id (digits only, no hyphens).
        ad_group_id: The numeric id of the Display ad group.
        ad_name: Name of the ad.
        business_name: Brand name (max 25 chars).
        final_url: Landing page URL.
        headlines: 1-5 short headlines, max 30 chars each.
        long_headline: Long headline, max 90 chars.
        descriptions: 1-5 descriptions, max 90 chars each.
        marketing_image_asset_ids: 1-15 landscape (1.91:1) image asset ids.
        square_image_asset_ids: 1-15 square (1:1) image asset ids.
        logo_image_asset_ids: Optional logos (1:1 or 4:1).
        youtube_video_asset_ids: Optional YouTube video asset ids.
        call_to_action_text: Optional CTA text.
        confirm: False = dry-run preview (default), True = apply.
    """
    customer_id = _clean_customer_id(customer_id)
    status = status.upper()
    if status not in ("PAUSED", "ENABLED"):
        raise ToolError("status must be PAUSED or ENABLED")
    if not (1 <= len(headlines) <= 5):
        raise ToolError("headlines: 1-5 required")
    if not (1 <= len(descriptions) <= 5):
        raise ToolError("descriptions: 1-5 required")
    if not marketing_image_asset_ids or not square_image_asset_ids:
        raise ToolError(
            "At least one landscape AND one square image asset id required"
        )
    if len(business_name) > 25:
        raise ToolError("business_name max 25 chars")
    if len(long_headline) > 90:
        raise ToolError("long_headline max 90 chars")
    _check_len(headlines, 30, "Headlines")
    _check_len(descriptions, 90, "Descriptions")

    client = utils.get_googleads_client()
    ad_service = utils.get_googleads_service("AdGroupAdService")

    operation = client.get_type("AdGroupAdOperation")
    ad_group_ad = operation.create
    ad_group_ad.ad_group = f"customers/{customer_id}/adGroups/{ad_group_id}"
    ad_group_ad.status = client.enums.AdGroupAdStatusEnum[status]
    ad = ad_group_ad.ad
    ad.name = ad_name
    ad.final_urls.append(final_url)
    if tracking_url_template:
        ad.tracking_url_template = tracking_url_template

    rda = ad.responsive_display_ad
    rda.business_name = business_name
    rda.long_headline.text = long_headline
    if call_to_action_text:
        rda.call_to_action_text = call_to_action_text
    for text in headlines:
        a = client.get_type("AdTextAsset")
        a.text = text
        rda.headlines.append(a)
    for text in descriptions:
        a = client.get_type("AdTextAsset")
        a.text = text
        rda.descriptions.append(a)

    def _img(asset_id):
        img = client.get_type("AdImageAsset")
        img.asset = f"customers/{customer_id}/assets/{asset_id}"
        return img

    for asset_id in marketing_image_asset_ids:
        rda.marketing_images.append(_img(asset_id))
    for asset_id in square_image_asset_ids:
        rda.square_marketing_images.append(_img(asset_id))
    for asset_id in logo_image_asset_ids or []:
        rda.logo_images.append(_img(asset_id))
    for asset_id in youtube_video_asset_ids or []:
        vid = client.get_type("AdVideoAsset")
        vid.asset = f"customers/{customer_id}/assets/{asset_id}"
        rda.youtube_videos.append(vid)

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
        "ad_name": ad_name,
        "headlines_count": len(headlines),
        "images": {
            "landscape": len(marketing_image_asset_ids),
            "square": len(square_image_asset_ids),
            "logos": len(logo_image_asset_ids or []),
            "videos": len(youtube_video_asset_ids or []),
        },
        "final_url": final_url,
        "status": status,
    }
    if confirm:
        details["created_resource"] = response.results[0].resource_name
    return _preview_or_done(confirm, "display_ad_create_responsive", details)
