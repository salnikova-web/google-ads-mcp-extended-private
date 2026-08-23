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

"""Ad extensions (assets) tools: sitelinks, callouts, structured snippets.

Extensions are created as assets and linked at campaign level. They work for
Search campaigns and are also picked up by PMax.

Safety model: identical to ads_mcp.tools.mutate — every write tool accepts
``confirm`` (default ``False`` = validate_only dry-run preview).
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

extensions_mcp = FastMCP("extensions")

_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False)
_READ = ToolAnnotations(readOnlyHint=True)


def _campaign_link_op(client, customer_id, campaign_id, asset_rn, field_type):
    op = client.get_type("MutateOperation")
    ca = op.campaign_asset_operation.create
    ca.campaign = f"customers/{customer_id}/campaigns/{campaign_id}"
    ca.asset = asset_rn
    ca.field_type = client.enums.AssetFieldTypeEnum[field_type]
    return op


def _bulk_mutate(customer_id: str, operations: List[Any], confirm: bool):
    client = utils.get_googleads_client()
    ga_service = utils.get_googleads_service("GoogleAdsService")
    request = client.get_type("MutateGoogleAdsRequest")
    request.customer_id = customer_id
    request.mutate_operations.extend(operations)
    request.validate_only = not confirm
    try:
        return ga_service.mutate(request=request)
    except GoogleAdsException as ex:
        _raise_tool_error(ex)


@extensions_mcp.tool(annotations=_WRITE)
def add_sitelinks(
    customer_id: str,
    campaign_id: str,
    sitelinks: List[Dict[str, str]],
    confirm: bool = False,
) -> Dict[str, Any]:
    """Adds sitelink extensions to a campaign.

    SAFETY: dry-run by default (validate_only); re-run with confirm=true.

    Args:
        customer_id: The client account id (digits only, no hyphens).
        campaign_id: The numeric id of the campaign.
        sitelinks: List of 2-20 dicts, each:
            {"text": "Pricing" (max 25 chars, required),
             "final_url": "https://..." (required),
             "description1": "..." (max 35, optional),
             "description2": "..." (max 35, optional)}.
            Google shows sitelinks only when a campaign has at least 2.
        confirm: False = dry-run preview (default), True = apply.
    """
    customer_id = _clean_customer_id(customer_id)
    if not sitelinks:
        raise ToolError("sitelinks list is empty")
    for sl in sitelinks:
        if not sl.get("text") or not sl.get("final_url"):
            raise ToolError(f"Each sitelink needs text and final_url: {sl}")
        if len(sl["text"]) > 25:
            raise ToolError(f"Sitelink text over 25 chars: {sl['text']}")
        for d in ("description1", "description2"):
            if sl.get(d) and len(sl[d]) > 35:
                raise ToolError(f"{d} over 35 chars: {sl[d]}")

    client = utils.get_googleads_client()
    operations: List[Any] = []
    temp_id = -1
    for sl in sitelinks:
        asset_rn = f"customers/{customer_id}/assets/{temp_id}"
        temp_id -= 1
        a_op = client.get_type("MutateOperation")
        asset = a_op.asset_operation.create
        asset.resource_name = asset_rn
        asset.final_urls.append(sl["final_url"])
        asset.sitelink_asset.link_text = sl["text"]
        if sl.get("description1"):
            asset.sitelink_asset.description1 = sl["description1"]
        if sl.get("description2"):
            asset.sitelink_asset.description2 = sl["description2"]
        operations.append(a_op)
        operations.append(
            _campaign_link_op(
                client, customer_id, campaign_id, asset_rn, "SITELINK"
            )
        )

    _bulk_mutate(customer_id, operations, confirm)

    details: Dict[str, Any] = {
        "customer_id": customer_id,
        "campaign_id": str(campaign_id),
        "sitelinks": [sl["text"] for sl in sitelinks],
        "count": len(sitelinks),
    }
    return _preview_or_done(confirm, "extensions_add_sitelinks", details)


@extensions_mcp.tool(annotations=_WRITE)
def add_callouts(
    customer_id: str,
    campaign_id: str,
    texts: List[str],
    confirm: bool = False,
) -> Dict[str, Any]:
    """Adds callout extensions (short USP phrases) to a campaign.

    SAFETY: dry-run by default (validate_only); re-run with confirm=true.

    Args:
        customer_id: The client account id (digits only, no hyphens).
        campaign_id: The numeric id of the campaign.
        texts: Callout phrases, max 25 chars each
            (e.g. ["Free trial", "Cancel anytime", "24/7 support"]).
        confirm: False = dry-run preview (default), True = apply.
    """
    customer_id = _clean_customer_id(customer_id)
    if not texts:
        raise ToolError("texts list is empty")
    bad = [t for t in texts if len(t) > 25]
    if bad:
        raise ToolError(f"Callouts over 25 chars: {bad}")

    client = utils.get_googleads_client()
    operations: List[Any] = []
    temp_id = -1
    for text in texts:
        asset_rn = f"customers/{customer_id}/assets/{temp_id}"
        temp_id -= 1
        a_op = client.get_type("MutateOperation")
        asset = a_op.asset_operation.create
        asset.resource_name = asset_rn
        asset.callout_asset.callout_text = text
        operations.append(a_op)
        operations.append(
            _campaign_link_op(
                client, customer_id, campaign_id, asset_rn, "CALLOUT"
            )
        )

    _bulk_mutate(customer_id, operations, confirm)

    details: Dict[str, Any] = {
        "customer_id": customer_id,
        "campaign_id": str(campaign_id),
        "callouts": texts,
        "count": len(texts),
    }
    return _preview_or_done(confirm, "extensions_add_callouts", details)


@extensions_mcp.tool(annotations=_WRITE)
def add_structured_snippets(
    customer_id: str,
    campaign_id: str,
    header: str,
    values: List[str],
    confirm: bool = False,
) -> Dict[str, Any]:
    """Adds a structured snippet extension to a campaign.

    SAFETY: dry-run by default (validate_only); re-run with confirm=true.

    Args:
        customer_id: The client account id (digits only, no hyphens).
        campaign_id: The numeric id of the campaign.
        header: One of Google's predefined headers, in the account language
            (e.g. English: Amenities, Brands, Courses, Degree programs,
            Destinations, Featured hotels, Insurance coverage, Models,
            Neighborhoods, Service catalog, Services, Shows, Styles, Types).
        values: 3-10 values, max 25 chars each.
        confirm: False = dry-run preview (default), True = apply.
    """
    customer_id = _clean_customer_id(customer_id)
    if not (3 <= len(values) <= 10):
        raise ToolError("values: 3-10 required")
    bad = [v for v in values if len(v) > 25]
    if bad:
        raise ToolError(f"Values over 25 chars: {bad}")

    client = utils.get_googleads_client()
    asset_rn = f"customers/{customer_id}/assets/-1"
    a_op = client.get_type("MutateOperation")
    asset = a_op.asset_operation.create
    asset.resource_name = asset_rn
    asset.structured_snippet_asset.header = header
    asset.structured_snippet_asset.values.extend(values)
    operations = [
        a_op,
        _campaign_link_op(
            client, customer_id, campaign_id, asset_rn, "STRUCTURED_SNIPPET"
        ),
    ]

    _bulk_mutate(customer_id, operations, confirm)

    details: Dict[str, Any] = {
        "customer_id": customer_id,
        "campaign_id": str(campaign_id),
        "header": header,
        "values": values,
    }
    return _preview_or_done(
        confirm, "extensions_add_structured_snippets", details
    )


@extensions_mcp.tool(annotations=_WRITE)
def attach_assets(
    customer_id: str,
    campaign_id: str,
    asset_ids: List[str],
    field_type: str,
    confirm: bool = False,
) -> Dict[str, Any]:
    """Links EXISTING assets (by id) to a campaign.

    Use when cloning a campaign: attaches the same asset the source
    campaign uses instead of creating a duplicate. field_type: SITELINK,
    CALLOUT, STRUCTURED_SNIPPET, BUSINESS_NAME, BUSINESS_LOGO, AD_IMAGE.

    SAFETY: dry-run by default (validate_only); re-run with confirm=true.
    """
    customer_id = _clean_customer_id(customer_id)
    field_type = field_type.upper()
    allowed = (
        "SITELINK",
        "CALLOUT",
        "STRUCTURED_SNIPPET",
        "BUSINESS_NAME",
        "BUSINESS_LOGO",
        "AD_IMAGE",
        "LOGO",
        "LANDSCAPE_LOGO",
    )
    if field_type not in allowed:
        raise ToolError(f"field_type must be one of {allowed}")
    if not asset_ids:
        raise ToolError("asset_ids must not be empty")

    client = utils.get_googleads_client()
    operations = []
    for asset_id in asset_ids:
        operations.append(
            _campaign_link_op(
                client,
                customer_id,
                campaign_id,
                f"customers/{customer_id}/assets/{asset_id}",
                field_type,
            )
        )
    _bulk_mutate(customer_id, operations, confirm)

    details: Dict[str, Any] = {
        "customer_id": customer_id,
        "campaign_id": str(campaign_id),
        "field_type": field_type,
        "asset_ids": [str(a) for a in asset_ids],
        "count": len(asset_ids),
    }
    return _preview_or_done(confirm, "extensions_attach_assets", details)


@extensions_mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True)
)
def remove_campaign_asset(
    customer_id: str,
    campaign_id: str,
    asset_id: str,
    field_type: str,
    confirm: bool = False,
) -> Dict[str, Any]:
    """Unlinks an extension asset from a campaign (asset itself is kept).

    Find asset ids with list_campaign_assets. SAFETY: dry-run by default;
    re-run with confirm=true.

    Args:
        customer_id: The client account id (digits only, no hyphens).
        campaign_id: The numeric id of the campaign.
        asset_id: The numeric id of the linked asset.
        field_type: SITELINK, CALLOUT or STRUCTURED_SNIPPET.
        confirm: False = dry-run preview (default), True = apply.
    """
    customer_id = _clean_customer_id(customer_id)
    field_type = field_type.upper()
    if field_type not in ("SITELINK", "CALLOUT", "STRUCTURED_SNIPPET"):
        raise ToolError(
            "field_type must be SITELINK, CALLOUT or STRUCTURED_SNIPPET"
        )

    client = utils.get_googleads_client()
    ca_service = utils.get_googleads_service("CampaignAssetService")

    operation = client.get_type("CampaignAssetOperation")
    operation.remove = (
        f"customers/{customer_id}/campaignAssets/"
        f"{campaign_id}~{asset_id}~{field_type}"
    )

    request = client.get_type("MutateCampaignAssetsRequest")
    request.customer_id = customer_id
    request.operations.append(operation)
    request.validate_only = not confirm

    try:
        response = ca_service.mutate_campaign_assets(request=request)
    except GoogleAdsException as ex:
        _raise_tool_error(ex)

    details: Dict[str, Any] = {
        "customer_id": customer_id,
        "campaign_id": str(campaign_id),
        "asset_id": str(asset_id),
        "field_type": field_type,
    }
    if confirm:
        details["removed_resource"] = response.results[0].resource_name
    return _preview_or_done(
        confirm, "extensions_remove_campaign_asset", details
    )


@extensions_mcp.tool(annotations=_READ)
def list_campaign_assets(
    customer_id: str,
    campaign_id: str,
    limit: int = 200,
) -> Dict[str, Any]:
    """Lists extension assets linked to a campaign (sitelinks, callouts,
    snippets) with asset ids needed for removal.

    Returns {"items": [...], "returned": n, "truncated": bool}. When
    truncated is true the campaign has more linked assets than limit, so an
    asset missing from items means "not listed", NOT "not linked" — raise
    limit before concluding an asset is already unlinked.

    Args:
        customer_id: The client account id (digits only, no hyphens).
        campaign_id: The numeric id of the campaign.
        limit: Max assets returned (default 200).
    """
    customer_id = _clean_customer_id(customer_id)
    cap = int(limit)
    ga_service = utils.get_googleads_service("GoogleAdsService")
    query = (
        "SELECT campaign_asset.asset, campaign_asset.field_type, "
        "campaign_asset.status, asset.id, asset.sitelink_asset.link_text, "
        "asset.callout_asset.callout_text, "
        "asset.structured_snippet_asset.header "
        "FROM campaign_asset "
        f"WHERE campaign.id = {int(campaign_id)} "
        "AND campaign_asset.field_type IN "
        "('SITELINK', 'CALLOUT', 'STRUCTURED_SNIPPET') "
        "AND campaign_asset.status != 'REMOVED' "
        "ORDER BY asset.id "
        # One row past the cap: reading it back is how truncation is
        # detected, so the cut is reported instead of silently applied.
        f"LIMIT {cap + 1}"
    )
    try:
        rows = ga_service.search(customer_id=customer_id, query=query)
        out = []
        for row in rows:
            ft = row.campaign_asset.field_type.name
            text = (
                row.asset.sitelink_asset.link_text
                or row.asset.callout_asset.callout_text
                or row.asset.structured_snippet_asset.header
            )
            out.append(
                {
                    "asset_id": str(row.asset.id),
                    "field_type": ft,
                    "text": text,
                    "status": row.campaign_asset.status.name,
                }
            )
        truncated = len(out) > cap
        items = out[:cap]
        return {
            "items": items,
            "returned": len(items),
            "truncated": truncated,
        }
    except GoogleAdsException as ex:
        _raise_tool_error(ex)
