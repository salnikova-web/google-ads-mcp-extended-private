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

"""Standard Shopping campaign tools (write extension).

Requires a Google Merchant Center account linked to the Google Ads account.
Workflow: shopping_campaign_create -> shopping_ad_group_create ->
shopping_ad_create_product -> shopping_ad_group_set_all_products.

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
    _to_micros,
    build_campaign_with_budget,
)

shopping_mcp = FastMCP("shopping")

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
                "MANUAL_CPC",
                "MAXIMIZE_CLICKS",
                "MAXIMIZE_CONVERSION_VALUE",
            ]
        }
    ),
]


@shopping_mcp.tool(annotations=_WRITE)
def campaign_create(
    customer_id: str,
    name: str,
    daily_budget: float,
    merchant_id: str,
    feed_label: Optional[str] = None,
    campaign_priority: int = 0,
    bidding_strategy: _BIDDING_ENUM = "MANUAL_CPC",
    target_roas: Optional[float] = None,
    tracking_url_template: Optional[str] = None,
    final_url_suffix: Optional[str] = None,
    status: _STATUS_ENUM = "PAUSED",
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    confirm: bool = False,
) -> Dict[str, Any]:
    """Creates a Standard Shopping campaign with a dedicated daily budget.

    Optional tracking_url_template / final_url_suffix set UTM tracking at
    creation (recommended for web funnels). NOTE: the campaign is
    created with ACCOUNT-DEFAULT conversion goals — attach the product's
    custom goal with mutate_campaign_set_custom_conversion_goal right
    after.

    Requires a linked Merchant Center account. SAFETY: dry-run by default
    (validate_only); re-run with confirm=true. Created PAUSED by default.

    Args:
        customer_id: The client account id (digits only, no hyphens).
        name: Campaign name (unique within the account).
        daily_budget: Daily budget in account currency.
        merchant_id: Merchant Center account id (digits only).
        feed_label: Optional feed label from Merchant Center (e.g. "US");
            omit to use all products.
        campaign_priority: 0 (low, default), 1 (medium) or 2 (high) —
            matters when several shopping campaigns cover the same products.
        bidding_strategy: MANUAL_CPC (default), MAXIMIZE_CLICKS or
            MAXIMIZE_CONVERSION_VALUE (optionally with target_roas).
        target_roas: Optional target ROAS as decimal (only with
            MAXIMIZE_CONVERSION_VALUE).
        start_date: "YYYY-MM-DD" (dashes required), account timezone;
            defaults to today.
        end_date: "YYYY-MM-DD" (dashes required), inclusive; omit for none.
        confirm: False = dry-run preview (default), True = apply.
    """
    customer_id = _clean_customer_id(customer_id)
    bidding_strategy = bidding_strategy.upper()
    status = status.upper()
    if bidding_strategy not in (
        "MANUAL_CPC",
        "MAXIMIZE_CLICKS",
        "MAXIMIZE_CONVERSION_VALUE",
    ):
        raise ToolError(
            "bidding_strategy must be MANUAL_CPC, MAXIMIZE_CLICKS or "
            "MAXIMIZE_CONVERSION_VALUE"
        )
    if status not in ("PAUSED", "ENABLED"):
        raise ToolError("status must be PAUSED or ENABLED")
    if campaign_priority not in (0, 1, 2):
        raise ToolError("campaign_priority must be 0, 1 or 2")
    if daily_budget <= 0:
        raise ToolError("daily_budget must be positive")

    client = utils.get_googleads_client()
    ga_service = utils.get_googleads_service("GoogleAdsService")

    budget_op, campaign_op, campaign = build_campaign_with_budget(
        client,
        customer_id,
        name,
        daily_budget,
        "SHOPPING",
        status,
        start_date=start_date,
        end_date=end_date,
        tracking_url_template=tracking_url_template,
        final_url_suffix=final_url_suffix,
    )
    campaign.shopping_setting.merchant_id = int(merchant_id)
    campaign.shopping_setting.campaign_priority = campaign_priority
    if feed_label:
        campaign.shopping_setting.feed_label = feed_label

    if bidding_strategy == "MANUAL_CPC":
        campaign.manual_cpc.enhanced_cpc_enabled = False
    elif bidding_strategy == "MAXIMIZE_CLICKS":
        client.copy_from(campaign.target_spend, client.get_type("TargetSpend"))
    else:
        if target_roas is not None:
            campaign.maximize_conversion_value.target_roas = float(target_roas)
        else:
            client.copy_from(
                campaign.maximize_conversion_value,
                client.get_type("MaximizeConversionValue"),
            )

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
        "channel_type": "SHOPPING",
        "merchant_id": merchant_id,
        "feed_label": feed_label,
        "campaign_priority": campaign_priority,
        "daily_budget": daily_budget,
        "bidding_strategy": bidding_strategy,
        "status": status,
        "next_step": (
            "Create an ad group (shopping_ad_group_create), a product ad "
            "(shopping_ad_create_product) and the root listing group "
            "(shopping_ad_group_set_all_products)"
        ),
    }
    if confirm:
        details["created_resources"] = [
            r.campaign_budget_result.resource_name
            or r.campaign_result.resource_name
            for r in response.mutate_operation_responses
        ]
    return _preview_or_done(confirm, "shopping_campaign_create", details)


@shopping_mcp.tool(annotations=_WRITE)
def ad_group_create(
    customer_id: str,
    campaign_id: str,
    name: str,
    cpc_bid: Optional[float] = None,
    status: _STATUS_ENUM = "PAUSED",
    confirm: bool = False,
) -> Dict[str, Any]:
    """Creates a SHOPPING_PRODUCT_ADS ad group in a Shopping campaign.

    SAFETY: dry-run by default (validate_only); re-run with confirm=true.

    Args:
        customer_id: The client account id (digits only, no hyphens).
        campaign_id: The numeric id of the Shopping campaign.
        name: Ad group name.
        cpc_bid: Optional max CPC in account currency (used with manual
            bidding).
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
    ad_group.type_ = client.enums.AdGroupTypeEnum.SHOPPING_PRODUCT_ADS
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
    return _preview_or_done(confirm, "shopping_ad_group_create", details)


@shopping_mcp.tool(annotations=_WRITE)
def ad_create_product(
    customer_id: str,
    ad_group_id: str,
    status: _STATUS_ENUM = "PAUSED",
    confirm: bool = False,
) -> Dict[str, Any]:
    """Creates a product ad in a Shopping ad group.

    Product ads have no creative — Google renders them from the Merchant
    Center feed. SAFETY: dry-run by default; re-run with confirm=true.

    Args:
        customer_id: The client account id (digits only, no hyphens).
        ad_group_id: The numeric id of the Shopping ad group.
        confirm: False = dry-run preview (default), True = apply.
    """
    customer_id = _clean_customer_id(customer_id)
    status = status.upper()
    if status not in ("PAUSED", "ENABLED"):
        raise ToolError("status must be PAUSED or ENABLED")

    client = utils.get_googleads_client()
    ad_service = utils.get_googleads_service("AdGroupAdService")

    operation = client.get_type("AdGroupAdOperation")
    ad_group_ad = operation.create
    ad_group_ad.ad_group = f"customers/{customer_id}/adGroups/{ad_group_id}"
    ad_group_ad.status = client.enums.AdGroupAdStatusEnum[status]
    client.copy_from(
        ad_group_ad.ad.shopping_product_ad,
        client.get_type("ShoppingProductAdInfo"),
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
        "ad_type": "SHOPPING_PRODUCT_AD",
        "status": status,
    }
    if confirm:
        details["created_resource"] = response.results[0].resource_name
    return _preview_or_done(confirm, "shopping_ad_create_product", details)


@shopping_mcp.tool(annotations=_WRITE)
def ad_group_set_item_listing(
    customer_id: str,
    ad_group_id: str,
    item_ids: List[str],
    cpc_bid: float = 2.0,
    exclude_others: bool = True,
    confirm: bool = False,
) -> Dict[str, Any]:
    """Builds a Standard Shopping listing group tree partitioned by item id.

    Root SUBDIVISION + one UNIT per item id (with cpc_bid) + an
    "everything else" UNIT (excluded when exclude_others=true). Use to
    reproduce a curated product-level Shopping ad group instead of
    targeting all products.

    SAFETY: dry-run by default (validate_only); re-run with confirm=true.
    """
    customer_id = _clean_customer_id(customer_id)
    if not item_ids:
        raise ToolError("item_ids must not be empty")

    client = utils.get_googleads_client()
    svc = utils.get_googleads_service("AdGroupCriterionService")
    ag_rn = f"customers/{customer_id}/adGroups/{ad_group_id}"
    root_rn = f"customers/{customer_id}/adGroupCriteria/{ad_group_id}~-1"

    ops = []
    root_op = client.get_type("AdGroupCriterionOperation")
    root = root_op.create
    root.resource_name = root_rn
    root.ad_group = ag_rn
    root.status = client.enums.AdGroupCriterionStatusEnum.ENABLED
    root.listing_group.type_ = client.enums.ListingGroupTypeEnum.SUBDIVISION
    ops.append(root_op)

    bid = _to_micros(cpc_bid)
    for item in item_ids:
        op = client.get_type("AdGroupCriterionOperation")
        c = op.create
        c.ad_group = ag_rn
        c.status = client.enums.AdGroupCriterionStatusEnum.ENABLED
        c.listing_group.type_ = client.enums.ListingGroupTypeEnum.UNIT
        c.listing_group.parent_ad_group_criterion = root_rn
        c.listing_group.case_value.product_item_id.value = item
        c.cpc_bid_micros = bid
        ops.append(op)

    other_op = client.get_type("AdGroupCriterionOperation")
    oc = other_op.create
    oc.ad_group = ag_rn
    oc.listing_group.type_ = client.enums.ListingGroupTypeEnum.UNIT
    oc.listing_group.parent_ad_group_criterion = root_rn
    # "everything else" node: the product_item_id dimension must be PRESENT
    # but with NO value (empty value is rejected as TOO_SHORT). SetInParent
    # marks the oneof as set without assigning a value.
    oc.listing_group.case_value._pb.product_item_id.SetInParent()
    if exclude_others:
        oc.negative = True
    else:
        oc.status = client.enums.AdGroupCriterionStatusEnum.ENABLED
        oc.cpc_bid_micros = bid
    ops.append(other_op)

    request = client.get_type("MutateAdGroupCriteriaRequest")
    request.customer_id = customer_id
    request.operations.extend(ops)
    request.validate_only = not confirm
    try:
        svc.mutate_ad_group_criteria(request=request)
    except GoogleAdsException as ex:
        _raise_tool_error(ex)

    details: Dict[str, Any] = {
        "customer_id": customer_id,
        "ad_group_id": str(ad_group_id),
        "items": len(item_ids),
        "cpc_bid": cpc_bid,
        "exclude_others": exclude_others,
        "nodes": len(ops),
    }
    return _preview_or_done(
        confirm, "shopping_ad_group_set_item_listing", details
    )


@shopping_mcp.tool(annotations=_WRITE)
def ad_group_set_all_products(
    customer_id: str,
    ad_group_id: str,
    cpc_bid: Optional[float] = None,
    confirm: bool = False,
) -> Dict[str, Any]:
    """Creates the root 'All products' listing group for a Shopping ad group.

    Without a listing group the ad group serves nothing. SAFETY: dry-run by
    default (validate_only); re-run with confirm=true.

    Args:
        customer_id: The client account id (digits only, no hyphens).
        ad_group_id: The numeric id of the Shopping ad group.
        cpc_bid: Optional max CPC for all products (manual bidding).
        confirm: False = dry-run preview (default), True = apply.
    """
    customer_id = _clean_customer_id(customer_id)

    client = utils.get_googleads_client()
    criterion_service = utils.get_googleads_service("AdGroupCriterionService")

    operation = client.get_type("AdGroupCriterionOperation")
    criterion = operation.create
    criterion.ad_group = f"customers/{customer_id}/adGroups/{ad_group_id}"
    criterion.status = client.enums.AdGroupCriterionStatusEnum.ENABLED
    criterion.listing_group.type_ = client.enums.ListingGroupTypeEnum.UNIT
    if cpc_bid is not None:
        criterion.cpc_bid_micros = _to_micros(cpc_bid)

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
        "listing_group": "All products (root unit)",
        "cpc_bid": cpc_bid,
    }
    if confirm:
        details["created_resource"] = response.results[0].resource_name
    return _preview_or_done(
        confirm, "shopping_ad_group_set_all_products", details
    )
