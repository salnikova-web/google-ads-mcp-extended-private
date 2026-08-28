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

"""Tools for exposing the API Search method to the MCP server."""

from typing import Any, Dict, List, Literal
from fastmcp import FastMCP
from fastmcp.tools import Tool
from mcp.types import ToolAnnotations

search_mcp = FastMCP("search")

import ads_mcp.utils as utils
from google.ads.googleads.errors import GoogleAdsException
from fastmcp.exceptions import ToolError

# Applied when the caller says nothing about `limit`. An unbounded search
# on a large account is the single easiest way to blow the context window,
# so the tool defaults to a page instead of to everything; explicit
# `limit=null` remains the full-export escape hatch.
_DEFAULT_LIMIT = 1000

# change_event refuses LIMIT > 10000, so the has_more probe cannot be
# appended past that point.
_CHANGE_EVENT_ROW_CAP = 10000

_CHANGE_EVENT_CAP_NOTE = (
    "change_event serves at most 10000 rows per query — narrow "
    "change_date_time instead of paginating."
)

# The resources that answer most questions, with a purpose short enough to
# pick from. Deliberately NOT the full list: naming all ~180 resources cost
# ~3.9KB of every tools/list and still could not name a single field, which
# is what an agent actually has to look up (see _search_tool_description).
_RESOURCE_SHORTLIST = (
    ("campaign", "settings, status and campaign totals"),
    ("ad_group", "ad group settings and metrics"),
    ("ad_group_ad", "individual ads and their metrics"),
    ("keyword_view", "per-keyword performance metrics"),
    ("search_term_view", "the queries that actually matched"),
    ("campaign_budget", "budget amount and delivery"),
    ("asset", "creatives shared across campaigns"),
    ("asset_group", "Performance Max asset groups"),
    ("customer", "account settings and account totals"),
    ("customer_client", "accounts under a manager"),
    ("change_event", "who changed what, recently"),
    ("campaign_criterion", "campaign targeting and negatives"),
    ("ad_group_criterion", "keywords and ad group targeting"),
    ("geographic_view", "performance split by location"),
    ("recommendation", "Google's pending optimization suggestions"),
)


def _incomplete_warning(shown: int, limit_was_default: bool) -> str:
    """The warning carried by every result a limit cut short.

    Truncation is never silent: a row that is not in the page is not
    proof the row does not exist, so the agent has to be told the result
    is partial before it concludes anything from it.
    """
    origin = (
        " (default limit — no limit was passed)" if limit_was_default else ""
    )
    return (
        f"Result INCOMPLETE: first {shown} rows shown{origin}. Tell the "
        "user the result is truncated; continue with offset=next_offset, "
        "or pass limit=null for a full export."
    )


def _rows_to_markdown(rows: List[Dict[str, Any]]) -> str:
    if not rows:
        return "(no rows)"
    columns: List[str] = []
    for row in rows:
        for key in row:
            if key not in columns:
                columns.append(key)

    def cell(value: Any) -> str:
        return str(value).replace("|", "\\|").replace("\n", " ")

    lines = [
        "| " + " | ".join(columns) + " |",
        "| " + " | ".join("---" for _ in columns) + " |",
    ]
    for row in rows:
        lines.append(
            "| " + " | ".join(cell(row.get(c, "")) for c in columns) + " |"
        )
    return "\n".join(lines)


def search(
    customer_id: str,
    fields: List[str],
    resource: str,
    conditions: List[str] = [],
    orderings: List[str] = [],
    limit: int | None = _DEFAULT_LIMIT,
    offset: int = 0,
    response_format: Literal["json", "markdown"] = "json",
) -> Dict[str, Any]:
    """Fetches data from the Google Ads API using the search method

    Returns at most 1000 rows unless `limit` says otherwise; pass
    limit=null (JSON null) for an unbounded full export.
    TRUNCATION IS NEVER SILENT: whenever a limit cut the result the
    envelope carries a `warning` string. Relay it to the user before
    drawing any conclusion from the rows — a row that is missing from a
    truncated page is NOT proof that the row does not exist.

    Args:
        customer_id: The id of the customer
        fields: The fields to fetch
        resource: The resource to return fields from
        conditions: List of conditions to filter the data, combined using AND clauses
        orderings: How the data is ordered
        limit: Maximum number of rows to return in this page; defaults to 1000, pass null for no limit at all
        offset: Number of leading rows to skip; pass next_offset from the previous page
        response_format: "json" (default) returns rows in `results`; "markdown" returns a rendered table in `results_markdown`

    Returns a pagination envelope:
        {count, offset, total, has_more, next_offset, results|results_markdown}.
        `total` is the size of the whole result set and is only known once
        that set is exhausted (has_more is false); until then it is null.
        When has_more is true the envelope also carries `warning`, and
        repeating the call with offset=next_offset returns the next page.
        A change_event query stopped by the API's own 10000-row ceiling
        rather than by exhaustion also reports `total` null, and carries
        `api_row_cap_hit` and `note`.
    """

    if offset < 0:
        raise ToolError("offset must be >= 0")
    if response_format not in ("json", "markdown"):
        raise ToolError('response_format must be "json" or "markdown"')

    ga_service = utils.get_googleads_service("GoogleAdsService")

    query_parts = [f"SELECT {','.join(fields)} FROM {resource}"]

    if conditions:
        query_parts.append(f" WHERE {' AND '.join(conditions)}")

    if orderings:
        query_parts.append(f" ORDER BY {','.join(orderings)}")

    row_cap_clamped = False
    if limit is not None:
        # GAQL has no OFFSET clause: fetch offset+limit rows plus one probe
        # row whose only job is to signal has_more, then slice locally.
        fetch_limit = offset + int(limit) + 1
        if resource == "change_event" and fetch_limit > _CHANGE_EVENT_ROW_CAP:
            # change_event rejects LIMIT > 10000; the probe degrades there.
            fetch_limit = _CHANGE_EVENT_ROW_CAP
            row_cap_clamped = True
        query_parts.append(f" LIMIT {fetch_limit}")

    query_parts.append(" PARAMETERS omit_unselected_resource_names=true")

    query = "".join(query_parts)
    # DEBUG, not INFO: conditions are caller-supplied and may carry data that
    # does not belong in default server output.
    utils.logger.debug(f"ads_mcp.search query {query}")

    rows: List[Dict[str, Any]] = []
    try:
        query_result = ga_service.search_stream(
            customer_id=customer_id, query=query
        )

        for batch in query_result:
            for row in batch.results:
                rows.append(
                    utils.format_output_row(row, batch.field_mask.paths)
                )
    except GoogleAdsException as ex:
        utils.raise_tool_error(ex)

    if limit is not None:
        page = rows[offset : offset + int(limit)]
        has_more = len(rows) > offset + int(limit)
        next_offset = offset + int(limit) if has_more else None
    else:
        page = rows[offset:] if offset else rows
        has_more = False
        next_offset = None
    # `total` counts every row fetched, never offset + len(page): an offset
    # past the end used to fabricate a total out of the offset itself
    # (offset=500 over a 5-row set reported total=500). With a finite limit
    # the fetch is capped at offset+limit+1, so once has_more is false
    # len(rows) IS the exact size of the result set; with no limit at all
    # every row was fetched, so it is exact there too.
    total = None if has_more else len(rows)

    envelope: Dict[str, Any] = {
        "count": len(page),
        "offset": offset,
        "total": total,
        "has_more": has_more,
        "next_offset": next_offset,
    }
    if has_more:
        # An explicit limit of exactly _DEFAULT_LIMIT is indistinguishable
        # from an omitted one, and reads the same either way: 1000 is the
        # default.
        envelope["warning"] = _incomplete_warning(
            len(page), limit == _DEFAULT_LIMIT
        )
    if row_cap_clamped and len(rows) == _CHANGE_EVENT_ROW_CAP:
        # The probe row could not be appended, so has_more stays false --
        # but the ceiling, not exhaustion, ended the page, so the set size
        # is unknown and `total` must not claim the ceiling as the answer.
        envelope["total"] = None
        envelope["api_row_cap_hit"] = True
        envelope["note"] = _CHANGE_EVENT_CAP_NOTE
    if response_format == "markdown":
        envelope["results_markdown"] = _rows_to_markdown(page)
    else:
        envelope["results"] = page
    return envelope


# Captured before the generated description overwrites search.__doc__ at
# the bottom of the module, so regenerating the description a second time
# cannot nest the previous one inside itself.
_SEARCH_DOCSTRING = search.__doc__


def _search_tool_description() -> str:
    """Returns the description for the `search` tool.

    The full 200-resource list used to be inlined here, ~3.9KB of names
    with no field information — enough to guess a resource, never enough
    to guess its fields, and paid for on every tools/list. It is replaced
    by a pointer at the metadata tool plus a shortlist of the resources
    that carry most real traffic. gaql_resources.txt itself is untouched;
    the metadata tooling and update_references still own it.
    """
    shortlist = "\n".join(
        f"    {name} — {purpose}" for name, purpose in _RESOURCE_SHORTLIST
    )
    return f"""
{_SEARCH_DOCSTRING}

### Resources
    For the full resource list and per-field metadata call
    metadata_get_resource_metadata — do not guess fields.
    The resources that answer most questions:
{shortlist}

### Hints for response_format
    markdown for flat human-readable tables; json (default) for nested
    fields or further processing

### Hints for paging
    Prefer one adequately-sized limit over many small pages: each page is
    a full round trip and a fresh query. The default limit is 1000 rows —
    when the envelope reports a `warning`, the result is INCOMPLETE and
    the user has to be told so.

### Hints
    Language Grammar can be found at https://developers.google.com/google-ads/api/docs/query/grammar
    All resources and descriptions are found at https://developers.google.com/google-ads/api/fields/latest/overview
    If the query fails, a ToolError will be raised with the error details.

    For Conversion issues try looking in offline_conversion_upload_conversion_action_summary

### Hint for customer_id
    should be a string of numbers without punctuation
    if presented in the form 123-456-7890 remove the hyphens and use 1234567890

### Hints for Dates
    All dates should be in the form YYYY-MM-DD and must include the dashes (-)
    Date ranges must be finite and must include a start and end date

### Hints for limits
    Requests to resource change_event must specify a LIMIT of less than or equal to 10000

### Hints for conversions questions
    https://developers.google.com/google-ads/api/docs/conversions/upload-summaries


### Hints for all resources
    To find out which specific fields (including compatible metrics and segments) you can select, filter by, or sort by for a given resource, you MUST use the `metadata_get_resource_metadata` tool.
    Do not guess the fields. Use the tool to look them up.
    Once you have the fields, ensure the whole field name is used (e.g., 'campaign.id', not just 'id'). Wildcards and partial fields are not allowed.
"""


# The `search` tool requires a more complex description that's generated at
# runtime. Uses the `add_tool` method instead of an annnotation since `add_tool`
# provides the flexibility needed to generate the description while also
# including the `search` method's docstring.
search.__doc__ = _search_tool_description()
search_mcp.add_tool(
    Tool.from_function(search, annotations=ToolAnnotations(readOnlyHint=True))
)
