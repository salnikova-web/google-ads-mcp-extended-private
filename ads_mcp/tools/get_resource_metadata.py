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
from typing import Any, Dict, Iterable, List, Optional
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from google.ads.googleads.errors import GoogleAdsException
import ads_mcp.utils as utils

metadata_mcp = FastMCP("metadata")

_RESOURCE_NAME = re.compile(r"\A[a-z][a-z0-9_]*\Z")
_FIELD_NAME = re.compile(r"\A[a-z][a-z0-9_.]*\Z")

# Names per get_field_details call. The catalog query is one round trip
# whatever the count, but a wider call answers a question nobody asked and
# pays for it in context; 20 covers "the fields of one query I am unsure of".
_MAX_FIELD_NAMES = 20

# selectable_with for a common metric names every resource it can join, which
# is hundreds of entries — far more than a caller needs to see, so it is cut.
_SELECTABLE_WITH_CAP = 50

# Flag letters are emitted in this order, so "SF" reads as selectable +
# filterable + NOT sortable. A field the API reports as none of the three
# still appears, with an empty flag string.
_FLAGS = (("S", "selectable"), ("F", "filterable"), ("O", "sortable"))
_FLAG_LEGEND = "S=selectable, F=filterable, O=sortable"


def _raise_tool_error(ex: GoogleAdsException) -> None:
    """Reports a Google Ads failure without leaking the raw gRPC text."""
    utils.raise_tool_error(ex)


def _short_reason(ex: Exception) -> str:
    """A one-line cause for an in-band warning, without the gRPC dump.

    The raw text of a GoogleAdsException is kilobytes of serialized proto;
    what the agent needs is the message and the error code.
    """
    if isinstance(ex, GoogleAdsException):
        errors = list(getattr(getattr(ex, "failure", None), "errors", []))
        if errors:
            code = str(errors[0].error_code).strip().replace("\n", " ")
            return f"{errors[0].message} [{code}]"[:200]
        return "GoogleAdsException"
    text = " ".join(str(ex).split())
    return (f"{type(ex).__name__}: {text}" if text else type(ex).__name__)[:200]


def _enum_name(value: Any) -> str:
    """Returns the readable name of a proto enum value."""
    name = getattr(value, "name", None)
    return name if isinstance(name, str) else str(value)


def _collect_fields(
    response: Iterable[Any],
    limit: int,
    fields: Dict[str, str],
    prefix: Optional[str] = None,
) -> bool:
    """Merges a response into {field name: flag string}.

    A name can be returned by more than one catalog query, so flags are
    merged rather than overwritten: a field is selectable if ANY response
    said so.

    Returns True when the response held more than ``limit`` rows, in which
    case the surplus is dropped. Stopping early also leaves the remaining
    pages of the response unfetched.

    Args:
        response: The rows returned by search_google_ads_fields.
        limit: Max rows to take from this response.
        fields: Dict collecting field name -> flag string.
        prefix: Optional prefix a field name must start with to be kept.
    """
    kept = 0
    for field in response:
        if prefix is not None and not field.name.startswith(prefix):
            continue
        if kept >= limit:
            return True
        previous = fields.get(field.name, "")
        fields[field.name] = "".join(
            letter
            for letter, attribute in _FLAGS
            if letter in previous or getattr(field, attribute)
        )
        kept += 1
    return False


def _grouped(fields: Dict[str, str]) -> Dict[str, Dict[str, str]]:
    """Splits the flat field map into attributes / metrics / segments.

    Grouped by name prefix rather than by the catalog's `category`: metric
    and segment names always carry those prefixes, so the grouping costs no
    extra column in the query, and it matches what the agent has to type.
    """
    groups: Dict[str, Dict[str, str]] = {
        "attributes": {},
        "metrics": {},
        "segments": {},
    }
    for name in sorted(fields):
        if name.startswith("metrics."):
            group = "metrics"
        elif name.startswith("segments."):
            group = "segments"
        else:
            group = "attributes"
        groups[group][name] = fields[name]
    return groups


@metadata_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def get_resource_metadata(
    resource_name: str, limit: int = 500
) -> Dict[str, Any]:
    """Lists the queryable fields of one Google Ads resource, with its compatible metrics and segments.

    WHEN TO USE: before composing any GAQL for the search_search tool. Do
    not guess field names, look them up here. For a single field's data
    type, its accepted enum values, or what it can be selected with, follow
    up with metadata_get_field_details.
    RESPONSE SHAPE: fields are grouped into "attributes", "metrics" and
    "segments"; each group maps a field name to a FLAG STRING where
    S=selectable, F=filterable, O=sortable, always in that order and a
    missing letter meaning false. So "SFO" is usable anywhere, "SF" cannot
    be used in ORDER BY, and "F" is filter-only and must NOT be SELECTed.
    NEVER SILENT: when "truncated" is true the lists are INCOMPLETE and
    "warnings" says why. A field missing from a truncated answer is NOT
    proof that the field does not exist — say so to the user instead of
    concluding absence, and do not cache that answer.
    CACHING: a complete answer (truncated false) changes rarely and can be
    reused for the rest of the session.

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

    fields: Dict[str, str] = {}
    warnings: List[str] = []
    limit_cut = False
    resource = utils.gaql_str(resource_name)

    # Query 1: Get resource attributes
    attributes_query = f"SELECT name, selectable, filterable, sortable WHERE name LIKE '{resource}.%' AND category = 'ATTRIBUTE'"
    request.query = attributes_query
    try:
        attributes_response = ga_service.search_google_ads_fields(
            request=request
        )
        limit_cut = _collect_fields(attributes_response, limit, fields)
    except Exception as e:
        utils.logger.warning(f"Failed attributes query: {e}")
        # Fallback to original behavior if category filter fails
        fallback_query = f"SELECT name, selectable, filterable, sortable WHERE name LIKE '{resource}.%'"
        request.query = fallback_query
        try:
            attributes_response = ga_service.search_google_ads_fields(
                request=request
            )
            limit_cut = _collect_fields(
                attributes_response,
                limit,
                fields,
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
    enrichment_failed = False
    try:
        metrics_segments_response = ga_service.search_google_ads_fields(
            request=request
        )
        limit_cut = (
            _collect_fields(metrics_segments_response, limit, fields)
            or limit_cut
        )
    except Exception as e:
        # Swallowing this used to return a falsely complete answer that the
        # docstring then told the client to cache: a resource would look
        # like it had no metrics at all, permanently. The attributes are
        # still worth returning, but the result is marked incomplete.
        utils.logger.warning(f"Failed metrics/segments query: {e}")
        enrichment_failed = True
        warnings.append(
            f"metrics/segments lookup failed: {_short_reason(e)} — those "
            "lists may be INCOMPLETE; retry, or verify a field name with "
            "metadata_get_field_details before concluding it does not "
            "exist. Do not cache this answer."
        )
    if limit_cut:
        warnings.append(utils.truncation_warning(limit))

    result: Dict[str, Any] = {
        "resource": resource_name,
        "flags": _FLAG_LEGEND,
    }
    result.update(_grouped(fields))
    result["truncated"] = limit_cut or enrichment_failed
    if warnings:
        result["warnings"] = warnings
    return result


@metadata_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def get_field_details(field_names: List[str]) -> Dict[str, Any]:
    """Looks up the data type, enum values and compatibility of specific Google Ads fields.

    WHEN TO USE: after metadata_get_resource_metadata, before composing
    GAQL with a field you have not used before. It answers "what values can
    this enum take", "what type is this field" and "which resources can I
    select it with" — the questions that otherwise turn into a failed query
    and a retry.
    RESPONSE SHAPE: one entry per field under "fields": name, category
    (ATTRIBUTE, METRIC or SEGMENT), data_type, is_repeated, selectable_with,
    plus enum_values ONLY when data_type is ENUM. Those enum values are the
    literals the API accepts in a WHERE clause; anything else is rejected.
    NOT FOUND IS NOT AN ERROR: names the catalog does not know come back in
    "not_found", so several candidate spellings can be probed in one call.
    LIMITS: 20 names per call at most, and "selectable_with" is cut at 50
    entries per field, flagged with "selectable_with_truncated" and with
    "truncated" on the envelope. A resource missing from a cut list is NOT
    proof the field cannot be selected with it.

    Args:
        field_names: Full lower-case field names, e.g. ["campaign.status", "metrics.clicks"]; 1 to 20 per call.
    """
    if not field_names:
        raise ToolError(
            "field_names must name at least one field, "
            "e.g. ['campaign.status']"
        )
    if len(field_names) > _MAX_FIELD_NAMES:
        raise ToolError(
            f"field_names takes at most {_MAX_FIELD_NAMES} names per call, "
            f"got {len(field_names)}; split them across several calls"
        )

    requested: List[str] = []
    for raw in field_names:
        name = str(raw).strip()
        if not _FIELD_NAME.match(name):
            raise ToolError(
                "field_names must be full lower-case Google Ads field names "
                f"such as 'campaign.status', got: {raw!r}"
            )
        if name not in requested:
            requested.append(name)

    ga_service = utils.get_googleads_service("GoogleAdsFieldService")
    request = utils.get_googleads_type("SearchGoogleAdsFieldsRequest")
    quoted = ", ".join(f"'{utils.gaql_str(name)}'" for name in requested)
    request.query = (
        "SELECT name, category, data_type, is_repeated, enum_values, "
        f"selectable_with WHERE name IN ({quoted})"
    )

    rows: List[Any] = []
    try:
        rows = list(ga_service.search_google_ads_fields(request=request))
    except GoogleAdsException as ex:
        utils.logger.error(f"Field details query failed: {ex}")
        _raise_tool_error(ex)
    except Exception as e:
        utils.logger.error(f"Field details query failed: {e}")
        raise RuntimeError(f"API call to search_google_ads_fields failed: {e}")

    details: List[Dict[str, Any]] = []
    found = set()
    capped: List[str] = []
    for row in rows:
        found.add(row.name)
        data_type = _enum_name(row.data_type)
        entry: Dict[str, Any] = {
            "name": row.name,
            "category": _enum_name(row.category),
            "data_type": data_type,
            "is_repeated": bool(row.is_repeated),
        }
        if data_type == "ENUM":
            entry["enum_values"] = list(row.enum_values)
        selectable_with = sorted(row.selectable_with)
        if len(selectable_with) > _SELECTABLE_WITH_CAP:
            entry["selectable_with"] = selectable_with[:_SELECTABLE_WITH_CAP]
            entry["selectable_with_truncated"] = True
            capped.append(row.name)
        else:
            entry["selectable_with"] = selectable_with
        details.append(entry)

    result: Dict[str, Any] = {
        "fields": sorted(details, key=lambda entry: entry["name"]),
        "not_found": [name for name in requested if name not in found],
        "truncated": bool(capped),
    }
    if capped:
        result["warnings"] = [
            f"selectable_with cut at {_SELECTABLE_WITH_CAP} entries for: "
            + ", ".join(sorted(capped))
            + " — a resource missing from a cut list is NOT proof the field "
            "cannot be selected with it; confirm that resource with "
            "metadata_get_resource_metadata."
        ]
    return result
