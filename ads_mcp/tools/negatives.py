# Copyright 2026 the google-ads-mcp-extended contributors.
# Licensed under the Apache License, Version 2.0.

"""Shared negative keyword lists: create, fill, attach to campaigns.

Safety model: ``confirm=False`` (default) = validate_only dry-run.
"""

from typing import Annotated, Any, Dict, List

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field
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

# Schema-only alias shared by both match_type sites below: advertises the
# accepted values in tools/list while runtime validation stays the existing
# lax .upper() + explicit ToolError check (a true Literal would reject
# lowercase input that works today).
_MATCH_TYPE_ENUM = Annotated[
    str, Field(json_schema_extra={"enum": ["EXACT", "PHRASE", "BROAD"]})
]


@negatives_mcp.tool(annotations=_WRITE)
def add_campaign_keywords(
    customer_id: str,
    campaign_id: str,
    keywords: List[str],
    match_type: Annotated[
        _MATCH_TYPE_ENUM,
        Field(description="Applies to all keywords in the call."),
    ] = "BROAD",
    confirm: bool = False,
) -> Dict[str, Any]:
    """Add campaign-level NEGATIVE keywords directly to a campaign.

    WHEN TO USE: one campaign, any channel including PMax (no ad groups
    there). One ad group: mutate_keywords_add(negative=true); reusable
    across campaigns: negatives_shared_set_create.
    PRECONDITIONS: the campaign must exist (mutate_list_campaigns).
    SIDE EFFECTS: ADDS criteria, never removes any (a repeat is a
    duplicate). A broad negative blocks every search it matches.
    DRY-RUN: confirm=false (default) validates remotely, changes nothing;
    confirm=true applies.

    Args:
        customer_id: The client account id (digits only, no hyphens).
        campaign_id: The numeric id of the campaign.
        keywords: Keyword texts to exclude.
        match_type: Applies to all keywords in the call.
        confirm: False = dry-run preview (default), True = apply.
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
    """Create an empty shared negative keyword list.

    WHEN TO USE: negatives several campaigns should share; for one
    campaign negatives_add_campaign_keywords is fewer steps.
    PRECONDITIONS: the name must be free — check negatives_list_shared_sets
    (a truncated list is not proof).
    SIDE EFFECTS: creates an EMPTY list that blocks nothing. Fill it with
    negatives_shared_set_add_keywords, attach with
    negatives_attach_to_campaigns.
    DRY-RUN: confirm=false (default) validates remotely, changes nothing;
    confirm=true applies.

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
    match_type: _MATCH_TYPE_ENUM = "BROAD",
    confirm: bool = False,
) -> Dict[str, Any]:
    """Add negative keywords to an existing shared list.

    WHEN TO USE: filling a shared list; the negatives take effect in every
    campaign it is attached to.
    PRECONDITIONS: the list must exist (negatives_shared_set_create; ids
    from negatives_list_shared_sets).
    SIDE EFFECTS: ADDS criteria, never removes any, and the change hits
    every attached campaign at once.
    DRY-RUN: confirm=false (default) validates remotely, changes nothing;
    confirm=true applies.

    Args:
        customer_id: The client account id (digits only, no hyphens).
        shared_set_id: The numeric id of the shared set.
        keywords: Keyword texts to exclude.
        match_type: Applies to all keywords in the call.
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
    """Attach a shared negative keyword list to campaigns.

    WHEN TO USE: last step of the shared-list flow (create, fill, attach).
    Detaching is not exposed — do it in the UI.
    PRECONDITIONS: list and campaigns must exist
    (negatives_list_shared_sets, mutate_list_campaigns); attaching twice
    is a duplicate.
    SIDE EFFECTS: every keyword in the list starts blocking traffic in each
    campaign at once, and so does anything added to the list later.
    DRY-RUN: confirm=false (default) validates remotely, changes nothing;
    confirm=true applies.

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
        css.shared_set = f"customers/{customer_id}/sharedSets/{shared_set_id}"
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
    limit: int = 200,
) -> Dict[str, Any]:
    """Lists shared negative keyword lists with ids and usage counts.

    Returns {"items": [...], "returned": n, "truncated": bool}. When
    truncated is true the account has more lists than limit, so a name
    missing from items means "not listed", NOT "does not exist" — raise
    limit before creating a duplicate list or concluding an id is unusable.

    Args:
        customer_id: The client account id (digits only, no hyphens).
        limit: Max lists returned (default 200).
    """
    customer_id = _clean_customer_id(customer_id)
    cap = int(limit)
    ga_service = utils.get_googleads_service("GoogleAdsService")
    query = (
        "SELECT shared_set.id, shared_set.name, shared_set.member_count, "
        "shared_set.reference_count, shared_set.status FROM shared_set "
        "WHERE shared_set.type = 'NEGATIVE_KEYWORDS' "
        "AND shared_set.status = 'ENABLED' "
        "ORDER BY shared_set.name "
        # One row past the cap: reading it back is how truncation is
        # detected, so the cut is reported instead of silently applied.
        f"LIMIT {cap + 1}"
    )
    try:
        rows = ga_service.search(customer_id=customer_id, query=query)
        out = [
            {
                "id": str(row.shared_set.id),
                "name": row.shared_set.name,
                "keywords_count": int(row.shared_set.member_count),
                "attached_campaigns": int(row.shared_set.reference_count),
            }
            for row in rows
        ]
        # Same keys as before (items/returned/truncated); the shared
        # envelope adds the never-silent "warning" when the cut fires.
        return utils.list_envelope(out, cap)
    except GoogleAdsException as ex:
        _raise_tool_error(ex)
