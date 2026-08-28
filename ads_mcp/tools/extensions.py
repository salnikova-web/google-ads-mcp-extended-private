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

# Ground truth for every field_type this module can attach. attach_assets
# accepts all eight; remove_campaign_asset and list_campaign_assets used to
# only cover the first three, so BUSINESS_NAME, BUSINESS_LOGO, AD_IMAGE,
# LOGO and LANDSCAPE_LOGO assets could be attached but never listed or
# detached again. Shared here so the three stay in sync by construction.
_ATTACHABLE_FIELD_TYPES = (
    "SITELINK",
    "CALLOUT",
    "STRUCTURED_SNIPPET",
    "BUSINESS_NAME",
    "BUSINESS_LOGO",
    "AD_IMAGE",
    "LOGO",
    "LANDSCAPE_LOGO",
)
_ATTACHABLE_FIELD_TYPES_GAQL = ", ".join(
    f"'{ft}'" for ft in _ATTACHABLE_FIELD_TYPES
)


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
    """Create sitelink assets and link them to a campaign.

    WHEN TO USE: new sitelinks. To reuse one that already exists in the
    account: extensions_attach_assets (no duplicate asset).
    PRECONDITIONS: the campaign must exist and holds at most 20 sitelinks;
    text and description lengths are checked locally first.
    SIDE EFFECTS: creates one asset per sitelink AND links each to the
    campaign, atomically. Google SHOWS sitelinks only from 2 up.
    DRY-RUN: confirm=false (default) validates remotely, changes nothing;
    confirm=true applies.

    Args:
        customer_id: The client account id (digits only, no hyphens).
        campaign_id: The numeric id of the campaign.
        sitelinks: Objects shaped
            {"text": "Pricing" (required, max 25 chars),
             "final_url": "https://..." (required),
             "description1": "..." (optional, max 35),
             "description2": "..." (optional, max 35)}.
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
    """Create callout assets (short USP phrases) and link them.

    WHEN TO USE: new callouts. To reuse existing ones:
    extensions_attach_assets.
    PRECONDITIONS: the campaign must exist; the 25-char limit is checked
    locally first.
    SIDE EFFECTS: creates one asset per phrase AND links each to the
    campaign, atomically. Callouts are not clickable and carry no URL.
    DRY-RUN: confirm=false (default) validates remotely, changes nothing;
    confirm=true applies.

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
    """Create ONE structured snippet asset and link it to a campaign.

    WHEN TO USE: listing variants of one thing under a Google header
    ("Services: X, Y, Z"). One call = one header.
    PRECONDITIONS: the campaign must exist; header must be a Google
    predefined header in the ACCOUNT language, and 3-10 values of max 25
    chars are checked locally.
    SIDE EFFECTS: creates one asset AND links it to the campaign,
    atomically.
    DRY-RUN: confirm=false (default) validates remotely, changes nothing;
    confirm=true applies.

    Args:
        customer_id: The client account id (digits only, no hyphens).
        campaign_id: The numeric id of the campaign.
        header: A Google-predefined header in the account language (English:
            Amenities, Brands, Courses, Degree programs, Destinations,
            Featured hotels, Insurance coverage, Models, Neighborhoods,
            Service catalog, Services, Shows, Styles, Types).
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
    """Link EXISTING assets (by id) to a campaign.

    WHEN TO USE: cloning a campaign — reuse the source asset instead of
    duplicating it. New extensions: extensions_add_sitelinks,
    extensions_add_callouts, extensions_add_structured_snippets.
    PRECONDITIONS: the assets must exist in the SAME account (ids from
    extensions_list_campaign_assets) and field_type must match what the
    asset is.
    SIDE EFFECTS: links only, creates nothing. The asset stays SHARED, so
    editing it later changes every campaign linked to it.
    DRY-RUN: confirm=false (default) validates remotely, changes nothing;
    confirm=true applies.
    UNITS & IDS: asset_ids are numeric asset ids, not resource names.

    Args:
        customer_id: The client account id (digits only, no hyphens).
        campaign_id: The numeric id of the campaign.
        asset_ids: Numeric ids of assets already in the account.
        field_type: SITELINK, CALLOUT, STRUCTURED_SNIPPET, BUSINESS_NAME,
            BUSINESS_LOGO, AD_IMAGE, LOGO or LANDSCAPE_LOGO.
        confirm: False = dry-run preview (default), True = apply.
    """
    customer_id = _clean_customer_id(customer_id)
    field_type = field_type.upper()
    if field_type not in _ATTACHABLE_FIELD_TYPES:
        raise ToolError(f"field_type must be one of {_ATTACHABLE_FIELD_TYPES}")
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
    """Unlink ONE extension asset from a campaign.

    WHEN TO USE: taking an extension off a campaign; the asset is KEPT and
    stays linked to any other campaign using it.
    PRECONDITIONS: asset_id and field_type from
    extensions_list_campaign_assets — the link is addressed
    campaignId~assetId~fieldType, so a wrong field_type finds nothing.
    SIDE EFFECTS: removes the link only; re-link with
    extensions_attach_assets.
    DRY-RUN: confirm=false (default) validates remotely, changes nothing;
    confirm=true applies.

    Args:
        customer_id: The client account id (digits only, no hyphens).
        campaign_id: The numeric id of the campaign.
        asset_id: The numeric id of the linked asset.
        field_type: SITELINK, CALLOUT, STRUCTURED_SNIPPET, BUSINESS_NAME,
            BUSINESS_LOGO, AD_IMAGE, LOGO or LANDSCAPE_LOGO.
        confirm: False = dry-run preview (default), True = apply.
    """
    customer_id = _clean_customer_id(customer_id)
    field_type = field_type.upper()
    if field_type not in _ATTACHABLE_FIELD_TYPES:
        raise ToolError(f"field_type must be one of {_ATTACHABLE_FIELD_TYPES}")

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
    """List extension assets linked to a campaign.

    WHEN TO USE: to get the asset ids needed by
    extensions_remove_campaign_asset, or to copy a campaign's extensions
    onto another with extensions_attach_assets. Only SITELINK, CALLOUT,
    STRUCTURED_SNIPPET, BUSINESS_NAME, BUSINESS_LOGO, AD_IMAGE, LOGO and
    LANDSCAPE_LOGO links are returned, REMOVED ones excluded.
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
        "campaign_asset.status, asset.id, asset.name, "
        "asset.sitelink_asset.link_text, "
        "asset.callout_asset.callout_text, "
        "asset.structured_snippet_asset.header "
        "FROM campaign_asset "
        f"WHERE campaign.id = {int(campaign_id)} "
        "AND campaign_asset.field_type IN "
        f"({_ATTACHABLE_FIELD_TYPES_GAQL}) "
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
            # sitelink/callout/structured_snippet assets carry their text
            # in that type's own sub-message; every other attachable type
            # (BUSINESS_NAME, BUSINESS_LOGO, AD_IMAGE, LOGO,
            # LANDSCAPE_LOGO) has no text sub-message at all, so asset.name
            # is the only human-readable label available for them.
            text = (
                row.asset.sitelink_asset.link_text
                or row.asset.callout_asset.callout_text
                or row.asset.structured_snippet_asset.header
                or row.asset.name
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
