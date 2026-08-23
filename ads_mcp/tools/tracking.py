# Copyright 2026 the google-ads-mcp-extended contributors.
# Licensed under the Apache License, Version 2.0.

"""Tracking template / UTM tools.

Manage tracking_url_template and final_url_suffix at account and campaign
level. Safety model: ``confirm=False`` (default) = validate_only dry-run.
"""

from typing import Any, Dict, Optional

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from google.ads.googleads.errors import GoogleAdsException

import ads_mcp.utils as utils
from ads_mcp.tools.mutate import (
    _clean_customer_id,
    _preview_or_done,
    _raise_tool_error,
)

tracking_mcp = FastMCP("tracking")

_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False)
_READ = ToolAnnotations(readOnlyHint=True)


@tracking_mcp.tool(annotations=_WRITE)
def campaign_set_tracking(
    customer_id: str,
    campaign_id: str,
    tracking_url_template: Optional[str] = None,
    final_url_suffix: Optional[str] = None,
    confirm: bool = False,
) -> Dict[str, Any]:
    """Sets the tracking URL template and/or final URL suffix of a campaign.

    Template example: "{lpurl}?utm_source=google&utm_campaign={campaignid}".
    Suffix example: "utm_source=google&utm_medium=cpc". Pass an empty
    string "" to clear a field; the dry-run preview lists the fields that
    would be wiped under "will_clear". SAFETY: dry-run by default; re-run
    with confirm=true.

    Args:
        customer_id: The client account id (digits only, no hyphens).
        campaign_id: The numeric id of the campaign.
        tracking_url_template: Optional new template (must contain {lpurl}).
        final_url_suffix: Optional new suffix (key=value pairs joined by &).
        confirm: False = dry-run preview (default), True = apply.
    """
    customer_id = _clean_customer_id(customer_id)
    if tracking_url_template is None and final_url_suffix is None:
        raise ToolError("Pass tracking_url_template and/or final_url_suffix")
    if tracking_url_template and "{lpurl}" not in tracking_url_template:
        raise ToolError("tracking_url_template must contain {lpurl}")

    client = utils.get_googleads_client()
    campaign_service = utils.get_googleads_service("CampaignService")

    operation = client.get_type("CampaignOperation")
    campaign = operation.update
    campaign.resource_name = f"customers/{customer_id}/campaigns/{campaign_id}"
    # Explicit paths, built only for the fields the caller passed: a
    # value-based mask drops "" and would never clear anything, while an
    # unconditional path list would wipe the field that was not passed.
    paths = []
    will_clear = []
    if tracking_url_template is not None:
        campaign.tracking_url_template = tracking_url_template
        paths.append("tracking_url_template")
        if not tracking_url_template:
            will_clear.append("tracking_url_template")
    if final_url_suffix is not None:
        campaign.final_url_suffix = final_url_suffix
        paths.append("final_url_suffix")
        if not final_url_suffix:
            will_clear.append("final_url_suffix")
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
        "tracking_url_template": tracking_url_template,
        "final_url_suffix": final_url_suffix,
    }
    if confirm:
        details["updated_resource"] = response.results[0].resource_name
    elif will_clear:
        details["will_clear"] = will_clear
    return _preview_or_done(confirm, "tracking_campaign_set", details)


@tracking_mcp.tool(annotations=_WRITE)
def account_set_tracking(
    customer_id: str,
    tracking_url_template: Optional[str] = None,
    final_url_suffix: Optional[str] = None,
    confirm: bool = False,
) -> Dict[str, Any]:
    """Sets account-level tracking template / final URL suffix.

    Account-level values apply to everything without a campaign-level
    override. Pass an empty string "" to clear a field; the dry-run preview
    lists the fields that would be wiped under "will_clear". SAFETY: dry-run
    by default; re-run with confirm=true.

    Args:
        customer_id: The client account id (digits only, no hyphens).
        tracking_url_template: Optional new template (must contain {lpurl}).
        final_url_suffix: Optional new suffix.
        confirm: False = dry-run preview (default), True = apply.
    """
    customer_id = _clean_customer_id(customer_id)
    if tracking_url_template is None and final_url_suffix is None:
        raise ToolError("Pass tracking_url_template and/or final_url_suffix")
    if tracking_url_template and "{lpurl}" not in tracking_url_template:
        raise ToolError("tracking_url_template must contain {lpurl}")

    client = utils.get_googleads_client()
    customer_service = utils.get_googleads_service("CustomerService")

    operation = client.get_type("CustomerOperation")
    customer = operation.update
    customer.resource_name = f"customers/{customer_id}"
    # See campaign_set_tracking: paths only for the fields actually passed.
    paths = []
    will_clear = []
    if tracking_url_template is not None:
        customer.tracking_url_template = tracking_url_template
        paths.append("tracking_url_template")
        if not tracking_url_template:
            will_clear.append("tracking_url_template")
    if final_url_suffix is not None:
        customer.final_url_suffix = final_url_suffix
        paths.append("final_url_suffix")
        if not final_url_suffix:
            will_clear.append("final_url_suffix")
    operation.update_mask.paths.extend(paths)

    request = client.get_type("MutateCustomerRequest")
    request.customer_id = customer_id
    request.operation = operation
    request.validate_only = not confirm

    try:
        response = customer_service.mutate_customer(request=request)
    except GoogleAdsException as ex:
        _raise_tool_error(ex)

    details: Dict[str, Any] = {
        "customer_id": customer_id,
        "tracking_url_template": tracking_url_template,
        "final_url_suffix": final_url_suffix,
    }
    if confirm:
        details["updated_resource"] = response.result.resource_name
    elif will_clear:
        details["will_clear"] = will_clear
    return _preview_or_done(confirm, "tracking_account_set", details)


@tracking_mcp.tool(annotations=_READ)
def list_tracking(
    customer_id: str,
    only_campaigns_with_tracking: bool = False,
    limit: int = 500,
) -> Dict[str, Any]:
    """Shows account-level and per-campaign tracking templates / suffixes.

    Campaigns are ordered by name and capped at limit; when more match, the
    returned "truncated" flag is True and the list is incomplete — re-run
    with a higher limit before concluding a campaign has no tracking.

    Args:
        customer_id: The client account id (digits only, no hyphens).
        only_campaigns_with_tracking: True to hide campaigns without any
            tracking settings.
        limit: Max campaigns to return (default 500).
    """
    customer_id = _clean_customer_id(customer_id)
    ga_service = utils.get_googleads_service("GoogleAdsService")

    try:
        acc_rows = list(
            ga_service.search(
                customer_id=customer_id,
                query=(
                    "SELECT customer.tracking_url_template, "
                    "customer.final_url_suffix FROM customer"
                ),
            )
        )
        camp_rows = ga_service.search(
            customer_id=customer_id,
            query=(
                "SELECT campaign.id, campaign.name, "
                "campaign.tracking_url_template, campaign.final_url_suffix "
                "FROM campaign WHERE campaign.status != 'REMOVED' "
                "ORDER BY campaign.name"
            ),
        )
    except GoogleAdsException as ex:
        _raise_tool_error(ex)

    campaigns = []
    truncated = False
    # The cap counts kept rows: only_campaigns_with_tracking is a client-side
    # filter (GAQL has no OR), so a LIMIT in the query would drop matching
    # campaigns before the filter ever saw them.
    for row in camp_rows:
        item = {
            "id": str(row.campaign.id),
            "name": row.campaign.name,
            "tracking_url_template": row.campaign.tracking_url_template,
            "final_url_suffix": row.campaign.final_url_suffix,
        }
        if only_campaigns_with_tracking and not (
            item["tracking_url_template"] or item["final_url_suffix"]
        ):
            continue
        if len(campaigns) >= limit:
            truncated = True
            break
        campaigns.append(item)

    account = {}
    if acc_rows:
        account = {
            "tracking_url_template": acc_rows[0].customer.tracking_url_template,
            "final_url_suffix": acc_rows[0].customer.final_url_suffix,
        }
    return {
        "account": account,
        "campaigns": campaigns,
        "truncated": truncated,
    }
