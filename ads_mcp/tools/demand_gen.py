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

"""Demand Gen tools for the Google Ads MCP server (write extension).

Covers the full Demand Gen workflow: uploading assets (images, YouTube
videos), creating campaigns and ad groups, attaching audiences, and creating
image / video ads.

Safety model: identical to ads_mcp.tools.mutate — every write tool accepts
``confirm`` (default ``False`` = validate_only dry-run preview).
"""

from typing import Annotated, Any, Dict, List, Optional

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field
from google.ads.googleads.errors import GoogleAdsException

import ads_mcp.safe_fetch as safe_fetch
import ads_mcp.utils as utils
from ads_mcp.tools.mutate import (
    _clean_customer_id,
    _preview_or_done,
    _raise_tool_error,
    _to_micros,
)

demandgen_mcp = FastMCP("demandgen")

_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False)
_READ = ToolAnnotations(readOnlyHint=True)

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
            ]
        }
    ),
]

_MAX_IMAGE_BYTES = 5 * 1024 * 1024  # Google Ads image asset limit (5 MB)


def _asset_rn(customer_id: str, asset_id: str) -> str:
    return f"customers/{customer_id}/assets/{asset_id}"


def _text_assets(client, texts: List[str]):
    out = []
    for t in texts:
        a = client.get_type("AdTextAsset")
        a.text = t
        out.append(a)
    return out


def _check_len(items: List[str], max_len: int, label: str) -> None:
    bad = [i for i in items if len(i) > max_len]
    if bad:
        raise ToolError(f"{label} over {max_len} chars: {bad}")


@demandgen_mcp.tool(annotations=_WRITE)
def asset_upload_image(
    customer_id: str,
    asset_name: str,
    image_source: str,
    confirm: bool = False,
) -> Dict[str, Any]:
    """Uploads an image as an Asset (for Demand Gen / PMax ads).

    SAFETY: dry-run by default (validate_only); re-run with confirm=true.

    Args:
        customer_id: The client account id (digits only, no hyphens).
        asset_name: Unique name for the asset in the account.
        image_source: HTTPS URL of the image. An absolute local file path is
            only read when the server has GOOGLE_ADS_MCP_ALLOW_LOCAL_FILES=1
            set. JPEG/PNG, max 5 MB. Recommended sizes: landscape 1200x628,
            square 1200x1200, logo 1200x1200.
        confirm: False = dry-run preview (default), True = apply.
    """
    customer_id = _clean_customer_id(customer_id)

    data = safe_fetch.read_image_source(image_source, _MAX_IMAGE_BYTES)

    client = utils.get_googleads_client()
    asset_service = utils.get_googleads_service("AssetService")

    operation = client.get_type("AssetOperation")
    asset = operation.create
    asset.name = asset_name
    asset.type_ = client.enums.AssetTypeEnum.IMAGE
    asset.image_asset.data = data

    request = client.get_type("MutateAssetsRequest")
    request.customer_id = customer_id
    request.operations.append(operation)
    request.validate_only = not confirm

    try:
        response = asset_service.mutate_assets(request=request)
    except GoogleAdsException as ex:
        _raise_tool_error(ex)

    details: Dict[str, Any] = {
        "customer_id": customer_id,
        "asset_name": asset_name,
        "size_bytes": len(data),
    }
    if confirm:
        details["created_resource"] = response.results[0].resource_name
    return _preview_or_done(confirm, "demandgen_asset_upload_image", details)


@demandgen_mcp.tool(annotations=_WRITE)
def asset_create_youtube_video(
    customer_id: str,
    asset_name: str,
    youtube_video_id: str,
    confirm: bool = False,
) -> Dict[str, Any]:
    """Registers a YouTube video as an Asset (for Demand Gen video ads).

    SAFETY: dry-run by default (validate_only); re-run with confirm=true.

    Args:
        customer_id: The client account id (digits only, no hyphens).
        asset_name: Unique name for the asset in the account.
        youtube_video_id: The 11-character YouTube video id (the part after
            v= in the URL, e.g. dQw4w9WgXcQ).
        confirm: False = dry-run preview (default), True = apply.
    """
    customer_id = _clean_customer_id(customer_id)

    client = utils.get_googleads_client()
    asset_service = utils.get_googleads_service("AssetService")

    operation = client.get_type("AssetOperation")
    asset = operation.create
    asset.name = asset_name
    asset.youtube_video_asset.youtube_video_id = youtube_video_id

    request = client.get_type("MutateAssetsRequest")
    request.customer_id = customer_id
    request.operations.append(operation)
    request.validate_only = not confirm

    try:
        response = asset_service.mutate_assets(request=request)
    except GoogleAdsException as ex:
        _raise_tool_error(ex)

    details: Dict[str, Any] = {
        "customer_id": customer_id,
        "asset_name": asset_name,
        "youtube_video_id": youtube_video_id,
    }
    if confirm:
        details["created_resource"] = response.results[0].resource_name
    return _preview_or_done(
        confirm, "demandgen_asset_create_youtube_video", details
    )


@demandgen_mcp.tool(annotations=_READ)
def list_assets(
    customer_id: str,
    asset_type: str = "IMAGE",
    name_contains: Optional[str] = None,
    limit: int = 50,
) -> Dict[str, Any]:
    """Lists assets (id, name, type) for reuse in DG ads; envelope, not a
    bare list.

    Returns {items, returned, truncated, warning?}. A missing asset may
    still exist — this list feeds asset-id selection before creating an
    ad, and re-uploading an asset that is merely off the page duplicates
    it. If truncated: raise limit before assuming the asset needs
    re-uploading, and tell the user the list is incomplete.

    Args:
        customer_id: The client account id (digits only, no hyphens).
        asset_type: IMAGE or YOUTUBE_VIDEO.
        name_contains: Optional case-sensitive substring filter on the name.
        limit: Max rows (default 50).
    """
    customer_id = _clean_customer_id(customer_id)
    asset_type = asset_type.upper()
    if asset_type not in ("IMAGE", "YOUTUBE_VIDEO"):
        raise ToolError("asset_type must be IMAGE or YOUTUBE_VIDEO")

    ga_service = utils.get_googleads_service("GoogleAdsService")
    where = f"WHERE asset.type = '{asset_type}'"
    if name_contains:
        where += f" AND asset.name LIKE '%{utils.gaql_str(name_contains)}%'"
    cap = int(limit)
    query = (
        "SELECT asset.id, asset.name, asset.type, "
        "asset.youtube_video_asset.youtube_video_id "
        f"FROM asset {where} ORDER BY asset.id LIMIT {cap + 1}"
    )
    try:
        rows = ga_service.search(customer_id=customer_id, query=query)
        out = []
        for row in rows:
            item = {
                "id": str(row.asset.id),
                "name": row.asset.name,
                "type": row.asset.type_.name,
            }
            if asset_type == "YOUTUBE_VIDEO":
                item["youtube_video_id"] = (
                    row.asset.youtube_video_asset.youtube_video_id
                )
            out.append(item)
        return utils.list_envelope(out, cap)
    except GoogleAdsException as ex:
        _raise_tool_error(ex)


@demandgen_mcp.tool(annotations=_WRITE)
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
    """Creates a Demand Gen campaign with a dedicated daily budget.

    Optional tracking_url_template / final_url_suffix set UTM tracking at
    creation (recommended for web funnels). NOTE: the campaign is
    created with ACCOUNT-DEFAULT conversion goals — attach the product's
    custom goal with mutate_campaign_set_custom_conversion_goal right
    after.

    Demand Gen serves across YouTube (in-feed, Shorts, in-stream), Discover
    and Gmail. Note: channel controls are set per AD GROUP, not on the
    campaign — restrict placements with
    demandgen_ad_group_create(channels=...) or
    demandgen_ad_group_update_channels.

    SAFETY: dry-run by default (validate_only); re-run with confirm=true.
    Created PAUSED unless status="ENABLED".

    Args:
        customer_id: The client account id (digits only, no hyphens).
        name: Campaign name (unique within the account).
        daily_budget: Daily budget in account currency.
        bidding_strategy: MAXIMIZE_CONVERSIONS (optionally with target_cpa),
            MAXIMIZE_CONVERSION_VALUE (optionally with target_roas) or
            MAXIMIZE_CLICKS.
        target_cpa: Optional target CPA in account currency.
        target_roas: Optional target ROAS as decimal (3.5 = 350%).
        start_date: "YYYY-MM-DD" (dashes required), account timezone;
            defaults to today.
        end_date: "YYYY-MM-DD" (dashes required), inclusive; omit for none.
        confirm: False = dry-run preview (default), True = apply.

    NOTE: the Demand Gen "Asset optimization" toggles (shorter videos,
    resized videos, landing page previews) are NOT campaign-level in the
    Google Ads API — they live on each video ad. Set them via
    demandgen_ad_create_video (asset_optimization / shorter_videos /
    resized_videos / landing_page_previews).
    """
    customer_id = _clean_customer_id(customer_id)
    bidding_strategy = bidding_strategy.upper()
    status = status.upper()
    if bidding_strategy not in (
        "MAXIMIZE_CONVERSIONS",
        "MAXIMIZE_CONVERSION_VALUE",
        "MAXIMIZE_CLICKS",
    ):
        raise ToolError(
            "bidding_strategy must be MAXIMIZE_CONVERSIONS, "
            "MAXIMIZE_CONVERSION_VALUE or MAXIMIZE_CLICKS"
        )
    if status not in ("PAUSED", "ENABLED"):
        raise ToolError("status must be PAUSED or ENABLED")
    if daily_budget <= 0:
        raise ToolError("daily_budget must be positive")

    import time as _time

    client = utils.get_googleads_client()
    ga_service = utils.get_googleads_service("GoogleAdsService")

    budget_temp_rn = f"customers/{customer_id}/campaignBudgets/-1"

    budget_op = client.get_type("MutateOperation")
    budget = budget_op.campaign_budget_operation.create
    budget.resource_name = budget_temp_rn
    budget.name = f"{name} budget {int(_time.time())}"
    budget.amount_micros = _to_micros(daily_budget)
    budget.delivery_method = client.enums.BudgetDeliveryMethodEnum.STANDARD
    budget.explicitly_shared = False

    campaign_op = client.get_type("MutateOperation")
    campaign = campaign_op.campaign_operation.create
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
    campaign.advertising_channel_type = (
        client.enums.AdvertisingChannelTypeEnum.DEMAND_GEN
    )
    # Match UI-created campaigns: keep geo/demographics at CAMPAIGN level.
    # API-created DG campaigns default to upgraded_targeting=true (ad-group
    # level targeting), which blocks campaign-level location criteria.
    campaign.demand_gen_campaign_settings.upgraded_targeting = False

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
    else:  # MAXIMIZE_CLICKS
        client.copy_from(campaign.target_spend, client.get_type("TargetSpend"))

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
        "channel_type": "DEMAND_GEN",
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
    return _preview_or_done(confirm, "demandgen_campaign_create", details)


@demandgen_mcp.tool(annotations=_WRITE)
def campaign_update_bidding(
    customer_id: str,
    campaign_id: str,
    target_cpa: Optional[float] = None,
    target_roas: Optional[float] = None,
    confirm: bool = False,
) -> Dict[str, Any]:
    """Updates the bidding target of an existing Demand Gen campaign.

    Pass target_cpa to set/change tCPA (Maximize Conversions) OR target_roas
    to set/change tROAS (Maximize Conversion Value) — exactly one of them.

    SAFETY: dry-run by default (validate_only); re-run with confirm=true.

    Args:
        customer_id: The client account id (digits only, no hyphens).
        campaign_id: The numeric id of the campaign.
        target_cpa: New target CPA in account currency (must be positive).
        target_roas: New target ROAS as decimal (3.5 = 350%, must be
            positive).
        confirm: False = dry-run preview (default), True = apply.
    """
    customer_id = _clean_customer_id(customer_id)
    if (target_cpa is None) == (target_roas is None):
        raise ToolError("Pass exactly one of target_cpa or target_roas")
    # A zero target would leave the bidding submessage at its proto default
    # and the update would silently do nothing; reject it instead.
    if target_cpa is not None and target_cpa <= 0:
        raise ToolError("target_cpa must be positive")
    if target_roas is not None and target_roas <= 0:
        raise ToolError("target_roas must be positive")

    client = utils.get_googleads_client()
    campaign_service = utils.get_googleads_service("CampaignService")

    operation = client.get_type("CampaignOperation")
    campaign = operation.update
    campaign.resource_name = f"customers/{customer_id}/campaigns/{campaign_id}"
    if target_cpa is not None:
        campaign.maximize_conversions.target_cpa_micros = _to_micros(target_cpa)
        path = "maximize_conversions.target_cpa_micros"
    else:
        campaign.maximize_conversion_value.target_roas = float(target_roas)
        path = "maximize_conversion_value.target_roas"
    operation.update_mask.paths.append(path)

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
    return _preview_or_done(
        confirm, "demandgen_campaign_update_bidding", details
    )


# Public channel name -> the matching bool on
# DemandGenChannelControls.selected_channels. Single source of truth so the
# update mask below cannot drift from what _apply_channels actually sets.
_DG_CHANNEL_FIELDS = {
    "YOUTUBE_IN_STREAM": "youtube_in_stream",
    "YOUTUBE_IN_FEED": "youtube_in_feed",
    "YOUTUBE_SHORTS": "youtube_shorts",
    "DISCOVER": "discover",
    "GMAIL": "gmail",
    "DISPLAY": "display",
}

_DG_CHANNELS = tuple(_DG_CHANNEL_FIELDS)

_CHANNEL_CONTROLS = "demand_gen_ad_group_settings.channel_controls"

# Schema-only aliases (see the block near _WRITE for why json_schema_extra
# rather than Literal): channels is a List[str] whose *item* type carries
# the enum so tools/list renders it under "items" rather than the list
# param itself.
_CHANNEL_ITEM_ENUM = Annotated[
    str, Field(json_schema_extra={"enum": list(_DG_CHANNELS)})
]
_CHANNEL_STRATEGY_VALUES = [
    "ALL_CHANNELS",
    "ALL_OWNED_AND_OPERATED_CHANNELS",
]
_CHANNEL_STRATEGY_ENUM = Annotated[
    Optional[str],
    Field(json_schema_extra={"enum": _CHANNEL_STRATEGY_VALUES}),
]


def _apply_channels(client, ad_group, channels: List[str]) -> None:
    channels = [c.upper() for c in channels]
    bad = [c for c in channels if c not in _DG_CHANNELS]
    if bad:
        raise ToolError(f"Unknown channels {bad}; valid: {_DG_CHANNELS}")
    cc = ad_group.demand_gen_ad_group_settings.channel_controls
    cc.channel_config = (
        client.enums.DemandGenChannelConfigEnum.SELECTED_CHANNELS
    )
    sel = cc.selected_channels
    for channel, field in _DG_CHANNEL_FIELDS.items():
        setattr(sel, field, channel in channels)


@demandgen_mcp.tool(annotations=_WRITE)
def ad_group_create(
    customer_id: str,
    campaign_id: str,
    name: str,
    channels: List[_CHANNEL_ITEM_ENUM] = [],
    channel_strategy: Annotated[
        _CHANNEL_STRATEGY_ENUM,
        Field(
            description=(
                "Alternative to channels: ALL_CHANNELS (everything incl. "
                "Display) or ALL_OWNED_AND_OPERATED_CHANNELS "
                "(YouTube+Discover+Gmail, no Display)."
            )
        ),
    ] = None,
    status: _STATUS_ENUM = "PAUSED",
    confirm: bool = False,
) -> Dict[str, Any]:
    """Creates an ad group inside a Demand Gen campaign, optionally with
    channel controls (placement selection).

    Attach audiences afterwards with demandgen_audience_attach.
    SAFETY: dry-run by default (validate_only); re-run with confirm=true.

    Args:
        customer_id: The client account id (digits only, no hyphens).
        campaign_id: The numeric id of the Demand Gen campaign.
        name: Ad group name (unique within the campaign).
        channels: Optional list of placements to serve on. Omit to serve on
            all channels (Google default). Example Display-only:
            ["DISPLAY"].
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
    if channels and channel_strategy:
        raise ToolError("Pass either channels or channel_strategy, not both")
    if channel_strategy:
        channel_strategy = channel_strategy.upper()
        if channel_strategy not in (
            "ALL_CHANNELS",
            "ALL_OWNED_AND_OPERATED_CHANNELS",
        ):
            raise ToolError(
                "channel_strategy must be ALL_CHANNELS or "
                "ALL_OWNED_AND_OPERATED_CHANNELS"
            )
        cc = ad_group.demand_gen_ad_group_settings.channel_controls
        cc.channel_config = (
            client.enums.DemandGenChannelConfigEnum.CHANNEL_STRATEGY
        )
        cc.channel_strategy = client.enums.DemandGenChannelStrategyEnum[
            channel_strategy
        ]
    elif channels:
        _apply_channels(client, ad_group, channels)

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
        "channels": channels or "ALL (default)",
        "status": status,
    }
    if confirm:
        details["created_resource"] = response.results[0].resource_name
    return _preview_or_done(confirm, "demandgen_ad_group_create", details)


@demandgen_mcp.tool(annotations=_WRITE)
def ad_group_update_channels(
    customer_id: str,
    ad_group_id: str,
    channels: List[_CHANNEL_ITEM_ENUM] = [],
    channel_strategy: _CHANNEL_STRATEGY_ENUM = None,
    confirm: bool = False,
) -> Dict[str, Any]:
    """Changes the channel controls (placements) of an existing DG ad group.

    Pass EITHER channels (specific placements) OR channel_strategy.
    SAFETY: dry-run by default (validate_only); re-run with confirm=true.

    Args:
        customer_id: The client account id (digits only, no hyphens).
        ad_group_id: The numeric id of the DG ad group.
        channels: Placements to serve on.
        channel_strategy: Alternative to channels:
            ALL_CHANNELS (everything incl. Display) or
            ALL_OWNED_AND_OPERATED_CHANNELS (YouTube+Discover+Gmail,
            no Display).
        confirm: False = dry-run preview (default), True = apply.
    """
    customer_id = _clean_customer_id(customer_id)
    if bool(channels) == bool(channel_strategy):
        raise ToolError("Pass exactly one of channels or channel_strategy")

    client = utils.get_googleads_client()
    ad_group_service = utils.get_googleads_service("AdGroupService")

    operation = client.get_type("AdGroupOperation")
    ad_group = operation.update
    ad_group.resource_name = f"customers/{customer_id}/adGroups/{ad_group_id}"
    if channel_strategy:
        channel_strategy = channel_strategy.upper()
        if channel_strategy not in (
            "ALL_CHANNELS",
            "ALL_OWNED_AND_OPERATED_CHANNELS",
        ):
            raise ToolError(
                "channel_strategy must be ALL_CHANNELS or "
                "ALL_OWNED_AND_OPERATED_CHANNELS"
            )
        cc = ad_group.demand_gen_ad_group_settings.channel_controls
        cc.channel_config = (
            client.enums.DemandGenChannelConfigEnum.CHANNEL_STRATEGY
        )
        cc.channel_strategy = client.enums.DemandGenChannelStrategyEnum[
            channel_strategy
        ]
        paths = [
            f"{_CHANNEL_CONTROLS}.channel_config",
            f"{_CHANNEL_CONTROLS}.channel_strategy",
        ]
    else:
        _apply_channels(client, ad_group, channels)
        # Every channel leaf has to be listed, including the ones set to
        # False: a mask derived from the populated message would omit them
        # and a channel could never be turned off. Leaf paths only — the
        # API rejects the non-leaf parent with FIELD_HAS_SUBFIELDS.
        paths = [f"{_CHANNEL_CONTROLS}.channel_config"] + [
            f"{_CHANNEL_CONTROLS}.selected_channels.{field}"
            for field in _DG_CHANNEL_FIELDS.values()
        ]
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
        "new_channels": (
            channel_strategy
            if channel_strategy
            else [c.upper() for c in channels]
        ),
    }
    if confirm:
        details["updated_resource"] = response.results[0].resource_name
    return _preview_or_done(
        confirm, "demandgen_ad_group_update_channels", details
    )


@demandgen_mcp.tool(annotations=_WRITE)
def audience_attach(
    customer_id: str,
    ad_group_id: str,
    audience_id: str,
    negative: bool = False,
    confirm: bool = False,
) -> Dict[str, Any]:
    """Attaches an existing Audience to an ad group as targeting.

    Find audience ids via search_search on resource `audience` (fields:
    audience.id, audience.name). SAFETY: dry-run by default; re-run with
    confirm=true.

    Args:
        customer_id: The client account id (digits only, no hyphens).
        ad_group_id: The numeric id of the ad group.
        audience_id: The numeric id of the Audience resource.
        confirm: False = dry-run preview (default), True = apply.
    """
    customer_id = _clean_customer_id(customer_id)

    client = utils.get_googleads_client()
    criterion_service = utils.get_googleads_service("AdGroupCriterionService")

    operation = client.get_type("AdGroupCriterionOperation")
    criterion = operation.create
    criterion.ad_group = f"customers/{customer_id}/adGroups/{ad_group_id}"
    criterion.audience.audience = (
        f"customers/{customer_id}/audiences/{audience_id}"
    )
    if negative:
        criterion.negative = True

    request = client.get_type("MutateAdGroupCriteriaRequest")
    request.customer_id = customer_id
    request.operations.append(operation)
    request.validate_only = not confirm

    try:
        response = criterion_service.mutate_ad_group_criteria(request=request)
    except GoogleAdsException as ex:
        _raise_tool_error(ex)

    details: Dict[str, Any] = {
        "customer_id": customer_id,
        "ad_group_id": str(ad_group_id),
        "audience_id": str(audience_id),
        "negative": negative,
    }
    if confirm:
        details["created_resource"] = response.results[0].resource_name
    return _preview_or_done(confirm, "demandgen_audience_attach", details)


@demandgen_mcp.tool(annotations=_WRITE)
def ad_create_image(
    customer_id: str,
    ad_group_id: str,
    ad_name: str,
    business_name: str,
    final_url: str,
    headlines: List[str],
    descriptions: List[str],
    marketing_image_asset_ids: List[str],
    square_image_asset_ids: List[str],
    logo_image_asset_ids: List[str],
    portrait_image_asset_ids: List[str] = [],
    call_to_action_text: Optional[str] = None,
    tracking_url_template: Optional[str] = None,
    status: _STATUS_ENUM = "PAUSED",
    confirm: bool = False,
) -> Dict[str, Any]:
    """Creates a Demand Gen image ad (multi-asset).

    Upload images first with demandgen_asset_upload_image (landscape
    1200x628, square 1200x1200, logo 1200x1200) and pass their asset ids.
    SAFETY: dry-run by default (validate_only); re-run with confirm=true.

    Args:
        customer_id: The client account id (digits only, no hyphens).
        ad_group_id: The numeric id of the DG ad group.
        ad_name: Name of the ad.
        business_name: Brand name shown in the ad (max 25 chars).
        final_url: Landing page URL.
        headlines: 1-5 headlines, max 40 chars each.
        descriptions: 1-5 descriptions, max 90 chars each.
        marketing_image_asset_ids: 1-20 landscape (1.91:1) image asset ids.
        square_image_asset_ids: 1-20 square (1:1) image asset ids.
        logo_image_asset_ids: 1-5 logo (1:1) image asset ids.
        portrait_image_asset_ids: Optional portrait (4:5) image asset ids.
        call_to_action_text: Optional CTA text, e.g. "Sign up".
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
    _check_len(headlines, 40, "Headlines")
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

    dg = ad.demand_gen_multi_asset_ad
    dg.business_name = business_name
    if call_to_action_text:
        dg.call_to_action_text = call_to_action_text
    for a in _text_assets(client, headlines):
        dg.headlines.append(a)
    for a in _text_assets(client, descriptions):
        dg.descriptions.append(a)
    for asset_id in marketing_image_asset_ids:
        img = client.get_type("AdImageAsset")
        img.asset = _asset_rn(customer_id, asset_id)
        dg.marketing_images.append(img)
    for asset_id in square_image_asset_ids:
        img = client.get_type("AdImageAsset")
        img.asset = _asset_rn(customer_id, asset_id)
        dg.square_marketing_images.append(img)
    for asset_id in logo_image_asset_ids:
        img = client.get_type("AdImageAsset")
        img.asset = _asset_rn(customer_id, asset_id)
        dg.logo_images.append(img)
    for asset_id in portrait_image_asset_ids or []:
        img = client.get_type("AdImageAsset")
        img.asset = _asset_rn(customer_id, asset_id)
        dg.portrait_marketing_images.append(img)

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
        "descriptions_count": len(descriptions),
        "images": {
            "landscape": len(marketing_image_asset_ids),
            "square": len(square_image_asset_ids),
            "logos": len(logo_image_asset_ids),
            "portrait": len(portrait_image_asset_ids or []),
        },
        "final_url": final_url,
        "status": status,
    }
    if confirm:
        details["created_resource"] = response.results[0].resource_name
    return _preview_or_done(confirm, "demandgen_ad_create_image", details)


@demandgen_mcp.tool(annotations=_WRITE)
def ad_create_video(
    customer_id: str,
    ad_group_id: str,
    ad_name: str,
    business_name: str,
    final_url: str,
    video_asset_ids: List[str],
    headlines: List[str],
    long_headlines: List[str],
    descriptions: List[str],
    logo_image_asset_ids: List[str],
    call_to_action: Optional[str] = None,
    tracking_url_template: Optional[str] = None,
    status: _STATUS_ENUM = "PAUSED",
    asset_optimization: Optional[bool] = None,
    shorter_videos: Optional[bool] = None,
    resized_videos: Optional[bool] = None,
    landing_page_previews: Optional[bool] = None,
    confirm: bool = False,
) -> Dict[str, Any]:
    """Creates a Demand Gen video responsive ad.

    call_to_action: optional CTA button, one of Google's enum values
    (e.g. LEARN_MORE, SIGN_UP, GET_STARTED, SUBSCRIBE, DOWNLOAD, SHOP_NOW,
    BOOK_NOW, CONTACT_US, APPLY_NOW). The value is checked against the enum
    on dry-run too, but the CTA asset itself is only created and linked when
    confirm=true — so the dry-run validates the ad payload WITHOUT the CTA
    asset link and does not fully cover what gets applied.

    Register videos first with demandgen_asset_create_youtube_video and
    pass their asset ids. SAFETY: dry-run by default (validate_only);
    re-run with confirm=true.

    Args:
        customer_id: The client account id (digits only, no hyphens).
        ad_group_id: The numeric id of the DG ad group.
        ad_name: Name of the ad.
        business_name: Brand name (max 25 chars).
        final_url: Landing page URL.
        video_asset_ids: 1-5 YouTube video asset ids.
        headlines: 1-5 short headlines, max 40 chars.
        long_headlines: 1-5 long headlines, max 90 chars.
        descriptions: 1-5 descriptions, max 90 chars.
        logo_image_asset_ids: 1-5 logo image asset ids.
        asset_optimization: Master toggle for this ad's "Asset optimization"
            (Demand Gen auto-generated assets). False opts OUT of shorter
            videos, resized/vertical videos and landing page previews; True
            opts in. None = Google defaults. Granular flags override it.
            NOTE: for Demand Gen these settings live on the AD (ad_group_ad),
            not the campaign — that is why they are set here.
        shorter_videos: Auto-generate shorter YouTube videos (False=off).
        resized_videos: Auto-generate resized/vertical videos (False=off).
        landing_page_previews: Auto-generate landing page previews (False=off).
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
        (logo_image_asset_ids, "logo_image_asset_ids"),
    ):
        if not (1 <= len(lst) <= 5):
            raise ToolError(f"{label}: 1-5 required")
    if len(business_name) > 25:
        raise ToolError("business_name max 25 chars")
    _check_len(headlines, 40, "Headlines")
    _check_len(long_headlines, 90, "Long headlines")
    _check_len(descriptions, 90, "Descriptions")

    client = utils.get_googleads_client()
    ad_service = utils.get_googleads_service("AdGroupAdService")

    # Resolved before the confirm branch so a mistyped CTA fails on dry-run
    # instead of only when the ad is applied.
    cta_enum = None
    if call_to_action:
        try:
            cta_enum = client.enums.CallToActionTypeEnum[call_to_action.upper()]
        except KeyError:
            raise ToolError(f"Unknown call_to_action: {call_to_action}")

    operation = client.get_type("AdGroupAdOperation")
    ad_group_ad = operation.create
    ad_group_ad.ad_group = f"customers/{customer_id}/adGroups/{ad_group_id}"
    ad_group_ad.status = client.enums.AdGroupAdStatusEnum[status]
    ad = ad_group_ad.ad
    ad.name = ad_name
    ad.final_urls.append(final_url)
    if tracking_url_template:
        ad.tracking_url_template = tracking_url_template

    dg = ad.demand_gen_video_responsive_ad
    dg.business_name.text = business_name
    for a in _text_assets(client, headlines):
        dg.headlines.append(a)
    for a in _text_assets(client, long_headlines):
        dg.long_headlines.append(a)
    for a in _text_assets(client, descriptions):
        dg.descriptions.append(a)
    for asset_id in video_asset_ids:
        vid = client.get_type("AdVideoAsset")
        vid.asset = _asset_rn(customer_id, asset_id)
        dg.videos.append(vid)
    for asset_id in logo_image_asset_ids:
        img = client.get_type("AdImageAsset")
        img.asset = _asset_rn(customer_id, asset_id)
        dg.logo_images.append(img)

    if cta_enum is not None and confirm:
        asset_service = utils.get_googleads_service("AssetService")
        a_op = client.get_type("AssetOperation")
        cta_asset = a_op.create
        cta_asset.call_to_action_asset.call_to_action = cta_enum
        a_req = client.get_type("MutateAssetsRequest")
        a_req.customer_id = customer_id
        a_req.operations.append(a_op)
        try:
            a_resp = asset_service.mutate_assets(request=a_req)
        except GoogleAdsException as ex:
            _raise_tool_error(ex)
        cta_ref = client.get_type("AdCallToActionAsset")
        cta_ref.asset = a_resp.results[0].resource_name
        dg.call_to_actions.append(cta_ref)

    # Ad-level "Asset optimization" (Demand Gen): opt in/out of the
    # auto-generated shorter/vertical videos and landing page previews.
    # These live on ad_group_ad, NOT the campaign (Google Ads API design).
    _sv, _rv, _lpp = shorter_videos, resized_videos, landing_page_previews
    if asset_optimization is not None:
        if _sv is None:
            _sv = asset_optimization
        if _rv is None:
            _rv = asset_optimization
        if _lpp is None:
            _lpp = asset_optimization
    if any(x is not None for x in (_sv, _rv, _lpp)):
        t_enum = client.enums.AssetAutomationTypeEnum
        s_enum = client.enums.AssetAutomationStatusEnum
        for auto_type, flag in (
            (t_enum.GENERATE_SHORTER_YOUTUBE_VIDEOS, _sv),
            (t_enum.GENERATE_VERTICAL_YOUTUBE_VIDEOS, _rv),
            (t_enum.GENERATE_LANDING_PAGE_PREVIEW, _lpp),
        ):
            if flag is None:
                continue
            aa = client.get_type("AdGroupAdAssetAutomationSetting")
            aa.asset_automation_type = auto_type
            aa.asset_automation_status = (
                s_enum.OPTED_IN if flag else s_enum.OPTED_OUT
            )
            ad_group_ad.ad_group_ad_asset_automation_settings.append(aa)

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
        "call_to_action": call_to_action,
        "final_url": final_url,
        "status": status,
        "asset_optimization": {
            "shorter_videos": _sv,
            "resized_videos": _rv,
            "landing_page_previews": _lpp,
        },
    }
    if confirm:
        details["created_resource"] = response.results[0].resource_name
    return _preview_or_done(confirm, "demandgen_ad_create_video", details)


@demandgen_mcp.tool(annotations=_WRITE)
def ad_update_asset_optimization(
    customer_id: str,
    ad_id: str,
    asset_optimization: Optional[bool] = None,
    shorter_videos: Optional[bool] = None,
    resized_videos: Optional[bool] = None,
    landing_page_previews: Optional[bool] = None,
    confirm: bool = False,
) -> Dict[str, Any]:
    """Turns the Demand Gen "Asset optimization" toggles on/off for an
    EXISTING video ad (ad_group_ad).

    In the Google Ads API these settings live on the ad, not the campaign,
    so pass the ad id (ad_group_ad.ad.id). asset_optimization is a master
    switch (False = turn all three off); shorter_videos / resized_videos /
    landing_page_previews override it per-toggle. The existing settings are
    read and merged, so untouched toggles are preserved.

    SAFETY: dry-run by default (validate_only); re-run with confirm=true.

    Args:
        customer_id: The client account id (digits only, no hyphens).
        ad_id: The numeric ad id (ad_group_ad.ad.id) of the DG video ad.
        asset_optimization: Master toggle; False turns all three off.
        shorter_videos: Shorter YouTube videos (False = off).
        resized_videos: Resized/vertical videos (False = off).
        landing_page_previews: Landing page previews (False = off).
        confirm: False = dry-run preview (default), True = apply.
    """
    customer_id = _clean_customer_id(customer_id)

    _sv, _rv, _lpp = shorter_videos, resized_videos, landing_page_previews
    if asset_optimization is not None:
        if _sv is None:
            _sv = asset_optimization
        if _rv is None:
            _rv = asset_optimization
        if _lpp is None:
            _lpp = asset_optimization
    if all(x is None for x in (_sv, _rv, _lpp)):
        raise ToolError(
            "Pass asset_optimization or at least one of shorter_videos / "
            "resized_videos / landing_page_previews"
        )

    client = utils.get_googleads_client()
    ga_service = utils.get_googleads_service("GoogleAdsService")
    ad_service = utils.get_googleads_service("AdGroupAdService")

    # Resolve the ad_group_ad resource and its current automation settings.
    ag_ad_rn = None
    current: Dict[int, int] = {}
    try:
        for row in ga_service.search(
            customer_id=customer_id,
            query=(
                "SELECT ad_group_ad.resource_name, "
                "ad_group_ad.ad_group_ad_asset_automation_settings "
                "FROM ad_group_ad "
                f"WHERE ad_group_ad.ad.id = {int(ad_id)}"
            ),
        ):
            ag_ad_rn = row.ad_group_ad.resource_name
            for s in row.ad_group_ad.ad_group_ad_asset_automation_settings:
                current[int(s.asset_automation_type)] = int(
                    s.asset_automation_status
                )
    except GoogleAdsException as ex:
        _raise_tool_error(ex)
    if ag_ad_rn is None:
        raise ToolError(f"Ad {ad_id} not found in {customer_id}")

    t_enum = client.enums.AssetAutomationTypeEnum
    s_enum = client.enums.AssetAutomationStatusEnum

    def _set(auto_type, flag):
        current[int(auto_type)] = int(
            s_enum.OPTED_IN if flag else s_enum.OPTED_OUT
        )

    if _sv is not None:
        _set(t_enum.GENERATE_SHORTER_YOUTUBE_VIDEOS, _sv)
    if _rv is not None:
        _set(t_enum.GENERATE_VERTICAL_YOUTUBE_VIDEOS, _rv)
    if _lpp is not None:
        _set(t_enum.GENERATE_LANDING_PAGE_PREVIEW, _lpp)

    operation = client.get_type("AdGroupAdOperation")
    ad_group_ad = operation.update
    ad_group_ad.resource_name = ag_ad_rn
    for auto_type, auto_status in sorted(current.items()):
        aa = client.get_type("AdGroupAdAssetAutomationSetting")
        aa.asset_automation_type = auto_type
        aa.asset_automation_status = auto_status
        ad_group_ad.ad_group_ad_asset_automation_settings.append(aa)
    operation.update_mask.paths.append("ad_group_ad_asset_automation_settings")

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
        "ad_id": str(ad_id),
        "asset_optimization": {
            "shorter_videos": _sv,
            "resized_videos": _rv,
            "landing_page_previews": _lpp,
        },
    }
    if confirm:
        details["updated_resource"] = response.results[0].resource_name
    return _preview_or_done(
        confirm, "demandgen_ad_update_asset_optimization", details
    )


@demandgen_mcp.tool(annotations=_WRITE)
def ad_create_carousel(
    customer_id: str,
    ad_group_id: str,
    ad_name: str,
    business_name: str,
    final_url: str,
    headline: str,
    description: str,
    logo_image_asset_id: str,
    cards: List[Dict[str, str]],
    call_to_action_text: Optional[str] = None,
    tracking_url_template: Optional[str] = None,
    status: _STATUS_ENUM = "PAUSED",
    confirm: bool = False,
) -> Dict[str, Any]:
    """Creates a Demand Gen carousel ad (2-10 swipeable cards).

    Card assets are created automatically from uploaded images. SAFETY:
    dry-run by default (validate_only); re-run with confirm=true.

    Args:
        customer_id: The client account id (digits only, no hyphens).
        ad_group_id: The numeric id of the DG ad group.
        ad_name: Name of the ad.
        business_name: Brand name (max 25 chars).
        final_url: Landing page URL.
        headline: Ad headline (max 40 chars).
        description: Ad description (max 90 chars).
        logo_image_asset_id: Logo image asset id (1:1).
        cards: 2-10 dicts, each:
            {"headline": "..." (max 40, required),
             "square_image_asset_id": "..." (1:1, required),
             "marketing_image_asset_id": "..." (1.91:1, optional),
             "call_to_action_text": "..." (optional)}.
        call_to_action_text: Optional ad-level CTA.
        confirm: False = dry-run preview (default), True = apply.
    """
    customer_id = _clean_customer_id(customer_id)
    status = status.upper()
    if status not in ("PAUSED", "ENABLED"):
        raise ToolError("status must be PAUSED or ENABLED")
    if not (2 <= len(cards) <= 10):
        raise ToolError("cards: 2-10 required")
    if len(business_name) > 25:
        raise ToolError("business_name max 25 chars")
    if len(headline) > 40:
        raise ToolError("headline max 40 chars")
    if len(description) > 90:
        raise ToolError("description max 90 chars")
    for card in cards:
        if not card.get("headline") or not card.get("square_image_asset_id"):
            raise ToolError(
                f"Each card needs headline and square_image_asset_id: {card}"
            )
        if len(card["headline"]) > 40:
            raise ToolError(f"Card headline max 40 chars: {card['headline']}")

    client = utils.get_googleads_client()
    ga_service = utils.get_googleads_service("GoogleAdsService")

    operations: List[Any] = []
    card_rns: List[str] = []
    temp_id = -1

    # 1. Create a carousel card asset per card.
    for card in cards:
        card_rn = f"customers/{customer_id}/assets/{temp_id}"
        temp_id -= 1
        a_op = client.get_type("MutateOperation")
        asset = a_op.asset_operation.create
        asset.resource_name = card_rn
        cc = asset.demand_gen_carousel_card_asset
        cc.headline = card["headline"]
        cc.square_marketing_image_asset = _asset_rn(
            customer_id, card["square_image_asset_id"]
        )
        if card.get("marketing_image_asset_id"):
            cc.marketing_image_asset = _asset_rn(
                customer_id, card["marketing_image_asset_id"]
            )
        if card.get("call_to_action_text"):
            cc.call_to_action_text = card["call_to_action_text"]
        operations.append(a_op)
        card_rns.append(card_rn)

    # 2. The carousel ad referencing the cards.
    ad_op = client.get_type("MutateOperation")
    ad_group_ad = ad_op.ad_group_ad_operation.create
    ad_group_ad.ad_group = f"customers/{customer_id}/adGroups/{ad_group_id}"
    ad_group_ad.status = client.enums.AdGroupAdStatusEnum[status]
    ad = ad_group_ad.ad
    ad.name = ad_name
    ad.final_urls.append(final_url)
    if tracking_url_template:
        ad.tracking_url_template = tracking_url_template

    dg = ad.demand_gen_carousel_ad
    dg.business_name = business_name
    dg.headline.text = headline
    dg.description.text = description
    dg.logo_image.asset = _asset_rn(customer_id, logo_image_asset_id)
    if call_to_action_text:
        dg.call_to_action_text = call_to_action_text
    for card_rn in card_rns:
        card_asset = client.get_type("AdDemandGenCarouselCardAsset")
        card_asset.asset = card_rn
        dg.carousel_cards.append(card_asset)
    operations.append(ad_op)

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
        "ad_group_id": str(ad_group_id),
        "ad_name": ad_name,
        "cards_count": len(cards),
        "final_url": final_url,
        "status": status,
    }
    if confirm:
        details["created_resources"] = [
            r.asset_result.resource_name or r.ad_group_ad_result.resource_name
            for r in response.mutate_operation_responses
        ]
    return _preview_or_done(confirm, "demandgen_ad_create_carousel", details)


@demandgen_mcp.tool(annotations=_WRITE)
def campaign_set_targeting_level(
    customer_id: str,
    campaign_id: str,
    upgraded_targeting: bool = False,
    confirm: bool = False,
) -> Dict[str, Any]:
    """Sets the Demand Gen targeting level of an existing campaign.

    upgraded_targeting=False -> geo/demographics at CAMPAIGN level (like
    UI-created campaigns); True -> at AD GROUP level. NOTE: Google may
    reject the change if the campaign already has conflicting criteria.

    SAFETY: dry-run by default (validate_only); re-run with confirm=true.

    Args:
        customer_id: The client account id (digits only, no hyphens).
        campaign_id: The numeric id of the DG campaign.
        upgraded_targeting: False = campaign-level targeting (default).
        confirm: False = dry-run preview (default), True = apply.
    """
    from google.protobuf import field_mask_pb2

    customer_id = _clean_customer_id(customer_id)

    client = utils.get_googleads_client()
    campaign_service = utils.get_googleads_service("CampaignService")

    operation = client.get_type("CampaignOperation")
    campaign = operation.update
    campaign.resource_name = f"customers/{customer_id}/campaigns/{campaign_id}"
    campaign.demand_gen_campaign_settings.upgraded_targeting = bool(
        upgraded_targeting
    )
    fm = field_mask_pb2.FieldMask(
        paths=["demand_gen_campaign_settings.upgraded_targeting"]
    )
    client.copy_from(operation.update_mask, fm)

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
        "upgraded_targeting": bool(upgraded_targeting),
        "targeting_level": ("AD_GROUP" if upgraded_targeting else "CAMPAIGN"),
    }
    if confirm:
        details["updated_resource"] = response.results[0].resource_name
    return _preview_or_done(
        confirm, "demandgen_campaign_set_targeting_level", details
    )
