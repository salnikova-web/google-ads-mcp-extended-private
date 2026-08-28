# Copyright 2026 the google-ads-mcp-extended contributors.
# Licensed under the Apache License, Version 2.0.

"""Audience tools: custom segments, remarketing user lists, attachments.

Safety model: ``confirm=False`` (default) = validate_only dry-run.
"""

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
)

audiences_mcp = FastMCP("audiences")

_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False)
_READ = ToolAnnotations(readOnlyHint=True)

# Mirrors targeting.py's own _GENDERS guard: pre-validate against the
# documented allow-list and name it in the error, rather than let an
# unknown value reach the enum lookup as a raw KeyError.
_GENDERS = ("MALE", "FEMALE", "UNDETERMINED")


@audiences_mcp.tool(annotations=_WRITE)
def create(
    customer_id: str,
    name: str,
    min_age: Optional[int] = None,
    genders: List[str] = [],
    user_list_ids: List[str] = [],
    user_interest_ids: List[str] = [],
    custom_audience_ids: List[str] = [],
    confirm: bool = False,
) -> Dict[str, Any]:
    """Creates a combined Audience from demographic + segment dimensions.

    Use to reproduce an asset-group persona as a reusable audience, then
    attach it as a PMax signal (pmax_signal_attach) or campaign audience.
    genders: MALE, FEMALE, UNDETERMINED. min_age: 18/25/35/45/55/65.

    SAFETY: dry-run by default (validate_only); re-run with confirm=true.
    """
    customer_id = _clean_customer_id(customer_id)
    client = utils.get_googleads_client()
    svc = utils.get_googleads_service("AudienceService")

    op = client.get_type("AudienceOperation")
    aud = op.create
    aud.name = name

    if min_age is not None:
        dim = client.get_type("AudienceDimension")
        seg = client.get_type("AgeSegment")
        seg.min_age = int(min_age)
        dim.age.age_ranges.append(seg)
        dim.age.include_undetermined = True
        aud.dimensions.append(dim)
    if genders:
        dim = client.get_type("AudienceDimension")
        for g in genders:
            key = str(g).upper()
            if key not in _GENDERS:
                raise ToolError(f"Unknown gender '{g}'; valid: {_GENDERS}")
            dim.gender.genders.append(client.enums.GenderTypeEnum[key])
        dim.gender.include_undetermined = True
        aud.dimensions.append(dim)

    seg_dim = client.get_type("AudienceDimension")
    has_seg = False
    for uid in user_list_ids:
        s = client.get_type("AudienceSegment")
        s.user_list.user_list = f"customers/{customer_id}/userLists/{uid}"
        seg_dim.audience_segments.segments.append(s)
        has_seg = True
    for iid in user_interest_ids:
        s = client.get_type("AudienceSegment")
        s.user_interest.user_interest_category = (
            f"customers/{customer_id}/userInterests/{iid}"
        )
        seg_dim.audience_segments.segments.append(s)
        has_seg = True
    for cid in custom_audience_ids:
        s = client.get_type("AudienceSegment")
        s.custom_audience.custom_audience = (
            f"customers/{customer_id}/customAudiences/{cid}"
        )
        seg_dim.audience_segments.segments.append(s)
        has_seg = True
    if has_seg:
        aud.dimensions.append(seg_dim)

    if not aud.dimensions:
        raise ToolError("Pass at least one dimension")

    request = client.get_type("MutateAudiencesRequest")
    request.customer_id = customer_id
    request.operations.append(op)
    request.validate_only = not confirm
    try:
        response = svc.mutate_audiences(request=request)
    except GoogleAdsException as ex:
        _raise_tool_error(ex)

    details: Dict[str, Any] = {
        "customer_id": customer_id,
        "name": name,
        "min_age": min_age,
        "genders": genders,
        "segments": len(user_list_ids)
        + len(user_interest_ids)
        + len(custom_audience_ids),
    }
    if confirm:
        rn = response.results[0].resource_name
        details["created_resource"] = rn
        details["audience_id"] = rn.split("/")[-1]
    return _preview_or_done(confirm, "audiences_create", details)


@audiences_mcp.tool(annotations=_WRITE)
def custom_segment_create(
    customer_id: str,
    name: str,
    keywords: List[str] = [],
    urls: List[str] = [],
    description: Optional[str] = None,
    confirm: bool = False,
) -> Dict[str, Any]:
    """Creates a custom segment (custom audience) from keywords and/or URLs.

    Custom segments target people by their search/browse interests. Usable
    in Demand Gen, Display, Video and as PMax signal. SAFETY: dry-run by
    default; re-run with confirm=true.

    Args:
        customer_id: The client account id (digits only, no hyphens).
        name: Segment name (unique).
        keywords: Interest/search keywords (e.g. ["weight loss app",
            "home workout"]).
        urls: Page URLs whose visitors' interests to match
            (e.g. ["https://example.com"]).
        description: Optional description.
        confirm: False = dry-run preview (default), True = apply.
    """
    customer_id = _clean_customer_id(customer_id)
    if not keywords and not urls:
        raise ToolError("Pass keywords and/or urls")

    client = utils.get_googleads_client()
    ca_service = utils.get_googleads_service("CustomAudienceService")

    operation = client.get_type("CustomAudienceOperation")
    ca = operation.create
    ca.name = name
    ca.type_ = client.enums.CustomAudienceTypeEnum.AUTO
    if description:
        ca.description = description
    for kw in keywords or []:
        member = client.get_type("CustomAudienceMember")
        member.member_type = client.enums.CustomAudienceMemberTypeEnum.KEYWORD
        member.keyword = kw
        ca.members.append(member)
    for url in urls or []:
        member = client.get_type("CustomAudienceMember")
        member.member_type = client.enums.CustomAudienceMemberTypeEnum.URL
        member.url = url
        ca.members.append(member)

    request = client.get_type("MutateCustomAudiencesRequest")
    request.customer_id = customer_id
    request.operations.append(operation)
    request.validate_only = not confirm

    try:
        response = ca_service.mutate_custom_audiences(request=request)
    except GoogleAdsException as ex:
        _raise_tool_error(ex)

    details: Dict[str, Any] = {
        "customer_id": customer_id,
        "segment_name": name,
        "keywords": keywords or [],
        "urls": urls or [],
    }
    if confirm:
        details["created_resource"] = response.results[0].resource_name
    return _preview_or_done(confirm, "audiences_custom_segment_create", details)


@audiences_mcp.tool(annotations=_WRITE)
def user_list_create_visitors(
    customer_id: str,
    name: str,
    url_contains: str,
    membership_days: int = 30,
    description: Optional[str] = None,
    confirm: bool = False,
) -> Dict[str, Any]:
    """Creates a rule-based remarketing list: visitors of pages whose URL
    contains the given string.

    Requires the Google Ads tag / GA4 link to actually populate. SAFETY:
    dry-run by default; re-run with confirm=true.

    Args:
        customer_id: The client account id (digits only, no hyphens).
        name: User list name (unique).
        url_contains: Substring of the page URL
            (e.g. "example.com/checkout").
        membership_days: How long visitors stay in the list, 1-540
            (default 30).
        description: Optional description.
        confirm: False = dry-run preview (default), True = apply.
    """
    customer_id = _clean_customer_id(customer_id)
    if not (1 <= membership_days <= 540):
        raise ToolError("membership_days must be 1-540")

    client = utils.get_googleads_client()
    ul_service = utils.get_googleads_service("UserListService")

    operation = client.get_type("UserListOperation")
    user_list = operation.create
    user_list.name = name
    if description:
        user_list.description = description
    user_list.membership_life_span = membership_days

    rule_item = client.get_type("UserListRuleItemInfo")
    rule_item.name = "url__"
    rule_item.string_rule_item.operator = (
        client.enums.UserListStringRuleItemOperatorEnum.CONTAINS
    )
    rule_item.string_rule_item.value = url_contains

    rule_item_group = client.get_type("UserListRuleItemGroupInfo")
    rule_item_group.rule_items.append(rule_item)

    operand = client.get_type("FlexibleRuleOperandInfo")
    operand.rule.rule_item_groups.append(rule_item_group)
    operand.lookback_window_days = membership_days

    flexible = user_list.rule_based_user_list.flexible_rule_user_list
    flexible.inclusive_rule_operator = (
        client.enums.UserListFlexibleRuleOperatorEnum.AND
    )
    flexible.inclusive_operands.append(operand)

    request = client.get_type("MutateUserListsRequest")
    request.customer_id = customer_id
    request.operations.append(operation)
    request.validate_only = not confirm

    try:
        response = ul_service.mutate_user_lists(request=request)
    except GoogleAdsException as ex:
        _raise_tool_error(ex)

    details: Dict[str, Any] = {
        "customer_id": customer_id,
        "user_list_name": name,
        "rule": f"page URL contains '{url_contains}'",
        "membership_days": membership_days,
    }
    if confirm:
        details["created_resource"] = response.results[0].resource_name
    return _preview_or_done(
        confirm, "audiences_user_list_create_visitors", details
    )


@audiences_mcp.tool(annotations=_READ)
def list_audiences(
    customer_id: str,
    limit: int = 100,
) -> Dict[str, Any]:
    """Lists audiences available in the account: combined Audiences,
    remarketing user lists and custom segments, with ids and sizes.

    Each of the three sections is capped at limit rows independently and
    the "truncated" map says which of them was cut short. A name missing
    from a truncated section means "not listed", NOT "does not exist" —
    raise limit before concluding an audience has to be created.

    Args:
        customer_id: The client account id (digits only, no hyphens).
        limit: Max rows per section (default 100).
    """
    customer_id = _clean_customer_id(customer_id)
    cap = int(limit)
    ga_service = utils.get_googleads_service("GoogleAdsService")

    out: Dict[str, Any] = {
        "audiences": [],
        "user_lists": [],
        "custom_segments": [],
        "truncated": {
            "audiences": False,
            "user_lists": False,
            "custom_segments": False,
        },
    }
    # Each query asks for one row past the cap: reading it back is how
    # truncation is detected, so the cut is reported instead of silently
    # applied.
    try:
        for row in ga_service.search(
            customer_id=customer_id,
            query=(
                "SELECT audience.id, audience.name, audience.description "
                f"FROM audience ORDER BY audience.name LIMIT {cap + 1}"
            ),
        ):
            out["audiences"].append(
                {"id": str(row.audience.id), "name": row.audience.name}
            )
        for row in ga_service.search(
            customer_id=customer_id,
            query=(
                "SELECT user_list.id, user_list.name, user_list.type, "
                "user_list.size_for_search, user_list.size_for_display "
                "FROM user_list WHERE user_list.membership_status = 'OPEN' "
                f"ORDER BY user_list.name LIMIT {cap + 1}"
            ),
        ):
            out["user_lists"].append(
                {
                    "id": str(row.user_list.id),
                    "name": row.user_list.name,
                    "type": row.user_list.type_.name,
                    "size_search": int(row.user_list.size_for_search),
                    "size_display": int(row.user_list.size_for_display),
                }
            )
        for row in ga_service.search(
            customer_id=customer_id,
            query=(
                "SELECT custom_audience.id, custom_audience.name, "
                "custom_audience.type, custom_audience.status "
                "FROM custom_audience "
                "WHERE custom_audience.status = 'ENABLED' "
                f"ORDER BY custom_audience.name LIMIT {cap + 1}"
            ),
        ):
            out["custom_segments"].append(
                {
                    "id": str(row.custom_audience.id),
                    "name": row.custom_audience.name,
                    "type": row.custom_audience.type_.name,
                }
            )
    except GoogleAdsException as ex:
        _raise_tool_error(ex)

    cut_sections = []
    for section in ("audiences", "user_lists", "custom_segments"):
        if len(out[section]) > cap:
            out[section] = out[section][:cap]
            out["truncated"][section] = True
            cut_sections.append(section)
    # The per-section flags live inside a nested map that is easy to skim
    # past, so one envelope-level warning names the sections that were cut
    # — truncation is never silent, whichever section it hit.
    if cut_sections:
        out["warning"] = (
            utils.truncation_warning(cap)
            + f" Sections cut: {', '.join(cut_sections)}."
        )
    return out


@audiences_mcp.tool(annotations=_WRITE)
def campaign_audience_attach(
    customer_id: str,
    campaign_id: str,
    user_list_id: Optional[str] = None,
    audience_id: Optional[str] = None,
    user_interest_id: Optional[str] = None,
    negative: bool = False,
    confirm: bool = False,
) -> Dict[str, Any]:
    """Attaches a remarketing user list OR a combined Audience to a campaign
    as targeting criterion.

    Pass exactly one of user_list_id / audience_id. SAFETY: dry-run by
    default; re-run with confirm=true.

    Args:
        customer_id: The client account id (digits only, no hyphens).
        campaign_id: The numeric id of the campaign.
        user_list_id: Id of a remarketing user list.
        audience_id: Id of a combined Audience resource.
        confirm: False = dry-run preview (default), True = apply.
    """
    customer_id = _clean_customer_id(customer_id)
    provided = [x for x in (user_list_id, audience_id, user_interest_id) if x]
    if len(provided) != 1:
        raise ToolError(
            "Pass exactly one of user_list_id / audience_id / user_interest_id"
        )

    client = utils.get_googleads_client()
    cc_service = utils.get_googleads_service("CampaignCriterionService")

    operation = client.get_type("CampaignCriterionOperation")
    criterion = operation.create
    criterion.campaign = f"customers/{customer_id}/campaigns/{campaign_id}"
    if user_list_id is not None:
        criterion.user_list.user_list = (
            f"customers/{customer_id}/userLists/{user_list_id}"
        )
    elif user_interest_id is not None:
        criterion.user_interest.user_interest_category = (
            f"customers/{customer_id}/userInterests/{user_interest_id}"
        )
    else:
        criterion.audience.audience = (
            f"customers/{customer_id}/audiences/{audience_id}"
        )
    if negative:
        criterion.negative = True

    request = client.get_type("MutateCampaignCriteriaRequest")
    request.customer_id = customer_id
    request.operations.append(operation)
    request.validate_only = not confirm

    try:
        response = cc_service.mutate_campaign_criteria(request=request)
    except GoogleAdsException as ex:
        _raise_tool_error(ex)

    details: Dict[str, Any] = {
        "customer_id": customer_id,
        "campaign_id": str(campaign_id),
        "user_list_id": user_list_id,
        "audience_id": audience_id,
        "user_interest_id": user_interest_id,
        "negative": negative,
    }
    if confirm:
        details["created_resource"] = response.results[0].resource_name
    return _preview_or_done(
        confirm, "audiences_campaign_audience_attach", details
    )
