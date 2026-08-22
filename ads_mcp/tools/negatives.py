# Copyright 2026 the google-ads-mcp-extended contributors.
# Licensed under the Apache License, Version 2.0.

"""Shared negative keyword lists: create, fill, attach to campaigns.

Safety model: ``confirm=False`` (default) = validate_only dry-run.
"""

from typing import Any, Dict, List

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

negatives_mcp = FastMCP("negatives")

_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False)
_READ = ToolAnnotations(readOnlyHint=True)


@negatives_mcp.tool(annotations=_WRITE)
def add_campaign_keywords(
    customer_id: str,
    campaign_id: str,
    keywords: List[str],
    match_type: str = "BROAD",
    confirm: bool = False,
) -> Dict[str, Any]:
    """Adds campaign-level NEGATIVE keywords directly to a campaign.

    Works for any channel including Performance Max (which has no ad
    groups). match_type: EXACT, PHRASE or BROAD.

    SAFETY: dry-run by default (validate_only); re-run with confirm=true.
    """
    customer_id = _clean_customer_id(customer_id)
    match_type = match_type.upper()
    if match_type not in ("EXACT", "PHRASE", "BROAD"):
        raise ToolError("match_type must be EXACT, PHRASE or BROAD")
    if not keywords:
        raise ToolError("keywords list is empty")

    client = utils.get_googleads_client()
    svc = utils.get_googleads_service("CampaignCriterionService")

    request = client.get_type("MutateCampaignCriteriaRequest")
    request.customer_id = customer_id
    request.validate_only = not confirm
    for text in keywords:
        op = client.get_type("CampaignCriterionOperation")
        crit = op.create
        crit.campaign = f"customers/{customer_id}/campaigns/{campaign_id}"
        crit.negative = True
        crit.keyword.text = text
        crit.keyword.match_type = client.enums.KeywordMatchTypeEnum[match_type]
        request.operations.append(op)

    try:
        response = svc.mutate_campaign_criteria(request=request)
    except GoogleAdsException as ex:
        _raise_tool_error(ex)

    details: Dict[str, Any] = {
        "customer_id": customer_id,
        "campaign_id": str(campaign_id),
        "keywords": keywords,
        "match_type": match_type,
        "count": len(keywords),
    }
    if confirm:
        details["created_resources"] = [
            r.resource_name for r in response.results
        ]
    return _preview_or_done(confirm, "negatives_add_campaign_keywords", details)


@negatives_mcp.tool(annotations=_WRITE)
def shared_set_create(
    customer_id: str,
    name: str,
    confirm: bool = False,
) -> Dict[str, Any]:
    """Creates a shared negative keyword list.

    SAFETY: dry-run by default; re-run with confirm=true.

    Args:
        customer_id: The client account id (digits only, no hyphens).
        name: List name (unique), e.g. "Brand exclusions global".
        confirm: False = dry-run preview (default), True = apply.
    """
    customer_id = _clean_customer_id(customer_id)
    client = utils.get_googleads_client()
    ss_service = utils.get_googleads_service("SharedSetService")

    operation = client.get_type("SharedSetOperation")
    shared_set = operation.create
    shared_set.name = name
    shared_set.type_ = client.enums.SharedSetTypeEnum.NEGATIVE_KEYWORDS

    request = client.get_type("MutateSharedSetsRequest")
    request.customer_id = customer_id
    request.operations.append(operation)
    request.validate_only = not confirm

    try:
        response = ss_service.mutate_shared_sets(request=request)
    except GoogleAdsException as ex:
        _raise_tool_error(ex)

    details: Dict[str, Any] = {"customer_id": customer_id, "list_name": name}
    if confirm:
        details["created_resource"] = response.results[0].resource_name
    return _preview_or_done(confirm, "negatives_shared_set_create", details)


@negatives_mcp.tool(annotations=_WRITE)
def shared_set_add_keywords(
    customer_id: str,
    shared_set_id: str,
    keywords: List[str],
    match_type: str = "BROAD",
    confirm: bool = False,
) -> Dict[str, Any]:
    """Adds negative keywords to a shared list.

    SAFETY: dry-run by default; re-run with confirm=true.

    Args:
        customer_id: The client account id (digits only, no hyphens).
        shared_set_id: The numeric id of the shared set.
        keywords: Keyword texts to exclude.
        match_type: EXACT, PHRASE or BROAD.
        confirm: False = dry-run preview (default), True = apply.
    """
    customer_id = _clean_customer_id(customer_id)
    match_type = match_type.upper()
    if match_type not in ("EXACT", "PHRASE", "BROAD"):
        raise ToolError("match_type must be EXACT, PHRASE or BROAD")
    if not keywords:
        raise ToolError("keywords list is empty")

    client = utils.get_googleads_client()
    sc_service = utils.get_googleads_service("SharedCriterionService")

    request = client.get_type("MutateSharedCriteriaRequest")
    request.customer_id = customer_id
    request.validate_only = not confirm

    for text in keywords:
        operation = client.get_type("SharedCriterionOperation")
        criterion = operation.create
        criterion.shared_set = (
            f"customers/{customer_id}/sharedSets/{shared_set_id}"
        )
        criterion.keyword.text = text
        criterion.keyword.match_type = client.enums.KeywordMatchTypeEnum[
            match_type
        ]
        request.operations.append(operation)

    try:
        response = sc_service.mutate_shared_criteria(request=request)
    except GoogleAdsException as ex:
        _raise_tool_error(ex)

    details: Dict[str, Any] = {
        "customer_id": customer_id,
        "shared_set_id": str(shared_set_id),
        "keywords": keywords,
        "match_type": match_type,
        "count": len(keywords),
    }
    if confirm:
        details["created_resources"] = [
            r.resource_name for r in response.results
        ]
    return _preview_or_done(
        confirm, "negatives_shared_set_add_keywords", details
    )


@negatives_mcp.tool(annotations=_WRITE)
def attach_to_campaigns(
    customer_id: str,
    shared_set_id: str,
    campaign_ids: List[str],
    confirm: bool = False,
) -> Dict[str, Any]:
    """Attaches a shared negative keyword list to campaigns.

    SAFETY: dry-run by default; re-run with confirm=true.

    Args:
        customer_id: The client account id (digits only, no hyphens).
        shared_set_id: The numeric id of the shared set.
        campaign_ids: Campaigns to attach the list to.
        confirm: False = dry-run preview (default), True = apply.
    """
    customer_id = _clean_customer_id(customer_id)
    if not campaign_ids:
        raise ToolError("campaign_ids list is empty")

    client = utils.get_googleads_client()
    css_service = utils.get_googleads_service("CampaignSharedSetService")

    request = client.get_type("MutateCampaignSharedSetsRequest")
    request.customer_id = customer_id
    request.validate_only = not confirm

    for cid in campaign_ids:
        operation = client.get_type("CampaignSharedSetOperation")
        css = operation.create
        css.campaign = f"customers/{customer_id}/campaigns/{cid}"
        css.shared_set = (
            f"customers/{customer_id}/sharedSets/{shared_set_id}"
        )
        request.operations.append(operation)

    try:
        response = css_service.mutate_campaign_shared_sets(request=request)
    except GoogleAdsException as ex:
        _raise_tool_error(ex)

    details: Dict[str, Any] = {
        "customer_id": customer_id,
        "shared_set_id": str(shared_set_id),
        "campaign_ids": campaign_ids,
    }
    if confirm:
        details["created_resources"] = [
            r.resource_name for r in response.results
        ]
    return _preview_or_done(confirm, "negatives_attach_to_campaigns", details)


@negatives_mcp.tool(annotations=_READ)
def list_shared_sets(
    customer_id: str,
) -> List[Dict[str, Any]]:
    """Lists shared negative keyword lists with ids and usage counts.

    Args:
        customer_id: The client account id (digits only, no hyphens).
    """
    customer_id = _clean_customer_id(customer_id)
    ga_service = utils.get_googleads_service("GoogleAdsService")
    query = (
        "SELECT shared_set.id, shared_set.name, shared_set.member_count, "
        "shared_set.reference_count, shared_set.status FROM shared_set "
        "WHERE shared_set.type = 'NEGATIVE_KEYWORDS' "
        "AND shared_set.status = 'ENABLED'"
    )
    try:
        rows = ga_service.search(customer_id=customer_id, query=query)
        return [
            {
                "id": str(row.shared_set.id),
                "name": row.shared_set.name,
                "keywords_count": int(row.shared_set.member_count),
                "attached_campaigns": int(row.shared_set.reference_count),
            }
            for row in rows
        ]
    except GoogleAdsException as ex:
        _raise_tool_error(ex)
