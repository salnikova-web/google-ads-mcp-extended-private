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

"""Video campaign tools (YouTube) — write extension.

Video campaigns can no longer be CREATED through the API: Google migrated
conversion-focused video to Demand Gen, so ``video_campaign_create`` always
fails and points at ``demandgen_campaign_create``. What is left here works
on campaigns that already exist: register videos as assets
(demandgen_asset_create_youtube_video) -> video_ad_group_create ->
video_ad_create_responsive.

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
    _clean_customer_id,
    _preview_or_done,
    _raise_tool_error,
    _text_assets,
)

video_mcp = FastMCP("video")

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
            "enum": ["MAXIMIZE_CONVERSIONS", "MAXIMIZE_CONVERSION_VALUE"]
        }
    ),
]


@video_mcp.tool(annotations=_WRITE)
def campaign_create(
    customer_id: str,
    name: str,
    daily_budget: float,
    bidding_strategy: _BIDDING_ENUM = "MAXIMIZE_CONVERSIONS",
    target_cpa: Optional[float] = None,
    target_roas: Optional[float] = None,
    status: _STATUS_ENUM = "PAUSED",
    confirm: bool = False,
) -> Dict[str, Any]:
    """ALWAYS FAILS: video campaigns cannot be created via the API — use
    demandgen_campaign_create instead.

    Google migrated conversion-focused video ("Video Action") to Demand Gen
    and withdrew the create surface, so every call raises. The parameters
    below are kept only so the failure names the right replacement instead
    of an argument error. Existing Video campaigns can still be managed
    with video_ad_group_create, video_ad_create_responsive and the mutate_*
    tools.

    Args:
        customer_id: Unused; the call fails before it is read.
        name: Unused; the call fails before it is read.
        daily_budget: Unused; the call fails before it is read.
        bidding_strategy: Unused; the call fails before it is read.
        target_cpa: Unused; the call fails before it is read.
        target_roas: Unused; the call fails before it is read.
        status: Unused; the call fails before it is read.
        confirm: Unused; the call fails before it is read.
    """
    raise ToolError(
        "Google Ads API does not allow creating Video campaigns anymore: "
        "conversion-focused video (Video Action) was migrated to Demand Gen. "
        "Use demandgen_campaign_create instead (covers YouTube in-stream, "
        "in-feed and Shorts), or create the campaign in the UI. Existing "
        "Video campaigns can still be managed with video_ad_group_create, "
        "video_ad_create_responsive and the mutate_* tools."
    )


@video_mcp.tool(annotations=_WRITE)
def ad_group_create(
    customer_id: str,
    campaign_id: str,
    name: str,
    status: _STATUS_ENUM = "PAUSED",
    confirm: bool = False,
) -> Dict[str, Any]:
    """Creates a VIDEO_RESPONSIVE ad group in a Video campaign.

    SAFETY: dry-run by default (validate_only); re-run with confirm=true.

    Args:
        customer_id: The client account id (digits only, no hyphens).
        campaign_id: The numeric id of the Video campaign.
        name: Ad group name.
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
    ad_group.type_ = client.enums.AdGroupTypeEnum.VIDEO_RESPONSIVE

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
        "status": status,
    }
    if confirm:
        details["created_resource"] = response.results[0].resource_name
    return _preview_or_done(confirm, "video_ad_group_create", details)


@video_mcp.tool(annotations=_WRITE)
def ad_create_responsive(
    customer_id: str,
    ad_group_id: str,
    ad_name: str,
    final_url: str,
    video_asset_ids: List[str],
    headlines: List[str],
    long_headlines: List[str],
    descriptions: List[str],
    tracking_url_template: Optional[str] = None,
    status: _STATUS_ENUM = "PAUSED",
    confirm: bool = False,
) -> Dict[str, Any]:
    """Creates a responsive video ad in a Video ad group.

    Register videos first with demandgen_asset_create_youtube_video.
    SAFETY: dry-run by default (validate_only); re-run with confirm=true.

    Args:
        customer_id: The client account id (digits only, no hyphens).
        ad_group_id: The numeric id of the Video ad group.
        ad_name: Name of the ad.
        final_url: Landing page URL.
        video_asset_ids: 1-5 YouTube video asset ids.
        headlines: 1-5 short headlines (recommended max 30 chars).
        long_headlines: 1-5 long headlines (recommended max 90 chars).
        descriptions: 1-5 descriptions (recommended max 90 chars).
        confirm: False = dry-run preview (default), True = apply.
    """
    customer_id = _clean_customer_id(customer_id)
    status = status.upper()
    if status not in ("PAUSED", "ENABLED"):
        raise ToolError("status must be PAUSED or ENABLED")
    for lst, label in (
        (video_asset_ids, "video_asset_ids"),
        (headlines, "headlines"),
        (long_headlines, "long_headlines"),
        (descriptions, "descriptions"),
    ):
        if not (1 <= len(lst) <= 5):
            raise ToolError(f"{label}: 1-5 required")

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

    vra = ad.video_responsive_ad
    for a in _text_assets(client, headlines):
        vra.headlines.append(a)
    for a in _text_assets(client, long_headlines):
        vra.long_headlines.append(a)
    for a in _text_assets(client, descriptions):
        vra.descriptions.append(a)
    for asset_id in video_asset_ids:
        vid = client.get_type("AdVideoAsset")
        vid.asset = f"customers/{customer_id}/assets/{asset_id}"
        vra.videos.append(vid)

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
        "videos_count": len(video_asset_ids),
        "final_url": final_url,
        "status": status,
    }
    if confirm:
        details["created_resource"] = response.results[0].resource_name
    return _preview_or_done(confirm, "video_ad_create_responsive", details)
