# Copyright 2026 Google LLC.
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

"""Tools for fetching metadata for Google Ads resources."""

import re
from typing import Any, Dict, Iterable, Optional, Set
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from google.ads.googleads.errors import GoogleAdsException
import ads_mcp.utils as utils

metadata_mcp = FastMCP("metadata")

_RESOURCE_NAME = re.compile(r"\A[a-z][a-z0-9_]*\Z")


def _raise_tool_error(ex: GoogleAdsException) -> None:
    """Reports a Google Ads failure without leaking the raw gRPC text."""
    utils.raise_tool_error(ex)


def _collect_fields(
    response: Iterable[Any],
    limit: int,
    selectable: Set[str],
    filterable: Set[str],
    sortable: Set[str],
    prefix: Optional[str] = None,
) -> bool:
    """Sorts the fields of a response into the selectable/... sets.

    Returns True when the response held more than ``limit`` rows, in which
    case the surplus is dropped. Stopping early also leaves the remaining
    pages of the response unfetched.

    Args:
        response: The rows returned by search_google_ads_fields.
        limit: Max rows to take from this response.
        selectable: Set collecting the names of selectable fields.
        filterable: Set collecting the names of filterable fields.
        sortable: Set collecting the names of sortable fields.
        prefix: Optional prefix a field name must start with to be kept.
    """
    kept = 0
    for field in response:
        if prefix is not None and not field.name.startswith(prefix):
            continue
        if kept >= limit:
            return True
        if field.selectable:
            selectable.add(field.name)
        if field.filterable:
            filterable.add(field.name)
        if field.sortable:
            sortable.add(field.name)
        kept += 1
    return False


@metadata_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def get_resource_metadata(
    resource_name: str, limit: int = 500
) -> Dict[str, Any]:
    """Retrieves the selectable, filterable, and sortable fields for a specific Google Ads resource,
    including compatible metrics and segments.

    Use this tool to find out which fields you can select, filter by, or sort by
    when querying a specific resource (e.g., 'campaign', 'ad_group').
    This tool also returns metrics and segments that can be selected with the resource.
    Their names start with 'metrics.' and 'segments.' respectively.

    Do not guess fields, you MUST use this tool to discover them before constructing a query for the
    `search` tool.

    The responses of this tool should be cached, as they don't change frequently.

    Args:
        resource_name: The name of the Google Ads resource (e.g., 'campaign', 'ad_group').
        limit: Max fields to take from each of the two catalog queries
            (attributes, and metrics/segments); default 500. When a query
            returns more, "truncated" is True in the result and the field
            lists are incomplete.
    """
    if not _RESOURCE_NAME.match(resource_name.strip()):
        raise ToolError(
            "resource_name must be a Google Ads resource such as 'campaign' "
            f"or 'ad_group', got: {resource_name!r}"
        )
    resource_name = resource_name.strip()

    ga_service = utils.get_googleads_service("GoogleAdsFieldService")
    request = utils.get_googleads_type("SearchGoogleAdsFieldsRequest")

    selectable = set()
    filterable = set()
    sortable = set()
    truncated = False
    resource = utils.gaql_str(resource_name)

    # Query 1: Get resource attributes
    attributes_query = f"SELECT name, selectable, filterable, sortable WHERE name LIKE '{resource}.%' AND category = 'ATTRIBUTE'"
    request.query = attributes_query
    try:
        attributes_response = ga_service.search_google_ads_fields(
            request=request
        )
        truncated = _collect_fields(
            attributes_response, limit, selectable, filterable, sortable
        )
    except Exception as e:
        utils.logger.warning(f"Failed attributes query: {e}")
        # Fallback to original behavior if category filter fails
        fallback_query = f"SELECT name, selectable, filterable, sortable WHERE name LIKE '{resource}.%'"
        request.query = fallback_query
        try:
            attributes_response = ga_service.search_google_ads_fields(
                request=request
            )
            truncated = _collect_fields(
                attributes_response,
                limit,
                selectable,
                filterable,
                sortable,
                prefix=f"{resource_name}.",
            )
        except GoogleAdsException as ex:
            utils.logger.error(f"Fallback attributes query failed: {ex}")
            _raise_tool_error(ex)
        except Exception as e2:
            utils.logger.error(f"Fallback attributes query failed: {e2}")
            raise RuntimeError(
                f"API call to search_google_ads_fields failed: {e2}"
            )

    # Query 2: Get selectable metrics and segments
    metrics_segments_query = f"SELECT name, selectable, filterable, sortable WHERE selectable_with CONTAINS ANY('{resource}')"
    request.query = metrics_segments_query
    try:
        metrics_segments_response = ga_service.search_google_ads_fields(
            request=request
        )
        truncated = (
            _collect_fields(
                metrics_segments_response,
                limit,
                selectable,
                filterable,
                sortable,
            )
            or truncated
        )
    except Exception as e:
        utils.logger.warning(f"Failed metrics/segments query: {e}")

    return {
        "resource": resource_name,
        "selectable": sorted(list(selectable)),
        "filterable": sorted(list(filterable)),
        "sortable": sorted(list(sortable)),
        "truncated": truncated,
    }
