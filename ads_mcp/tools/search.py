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

from typing import Any, Dict, List
from fastmcp import FastMCP
from fastmcp.tools import Tool
from mcp.types import ToolAnnotations

search_mcp = FastMCP("search")

import ads_mcp.utils as utils
from google.ads.googleads.errors import GoogleAdsException
from fastmcp.exceptions import ToolError


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
    limit: int | None = None,
    offset: int = 0,
    response_format: str = "json",
) -> Dict[str, Any]:
    """Fetches data from the Google Ads API using the search method

    Args:
        customer_id: The id of the customer
        fields: The fields to fetch
        resource: The resource to return fields from
        conditions: List of conditions to filter the data, combined using AND clauses
        orderings: How the data is ordered
        limit: The maximum number of rows to return in this page; omit to fetch everything
        offset: Number of leading rows to skip; pass next_offset from the previous page
        response_format: "json" (default) returns rows in `results`; "markdown" returns a rendered table in `results_markdown`

    Returns a pagination envelope:
        {count, offset, total, has_more, next_offset, results|results_markdown}.
        `total` is only known once the result set is exhausted (has_more is
        false); until then it is null. When has_more is true, repeat the call
        with offset=next_offset to get the next page.
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

    if limit is not None:
        # GAQL has no OFFSET clause: fetch offset+limit rows plus one probe
        # row whose only job is to signal has_more, then slice locally.
        fetch_limit = offset + int(limit) + 1
        if resource == "change_event" and fetch_limit > 10000:
            # change_event rejects LIMIT > 10000; the probe degrades there.
            fetch_limit = 10000
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
    total = None if has_more else offset + len(page)

    envelope: Dict[str, Any] = {
        "count": len(page),
        "offset": offset,
        "total": total,
        "has_more": has_more,
        "next_offset": next_offset,
    }
    if response_format == "markdown":
        envelope["results_markdown"] = _rows_to_markdown(page)
    else:
        envelope["results"] = page
    return envelope


def _search_tool_description() -> str:
    """Returns the description for the `search` tool."""
    # Add a warning that will be part of the description
    file_content = (
        "WARNING: The list of valid resources is missing. "
        "Tool may not function correctly."
    )

    try:
        with open(utils.get_gaql_resources_filepath(), "r") as file:
            file_content = file.read()
    except FileNotFoundError:
        utils.logger.error("The specified file was not found.")

    return f"""
{search.__doc__}

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
    To find out which specific fields (including compatible metrics and segments) you can select, filter by, or sort by for a given resource, you MUST use the `get_resource_metadata` tool.
    Do not guess the fields. Use the tool to look them up.
    Once you have the fields, ensure the whole field name is used (e.g., 'campaign.id', not just 'id'). Wildcards and partial fields are not allowed.

### Valid resources
    What follows is a list of valid resources that can be queried.
    {file_content}
"""


# The `search` tool requires a more complex description that's generated at
# runtime. Uses the `add_tool` method instead of an annnotation since `add_tool`
# provides the flexibility needed to generate the description while also
# including the `search` method's docstring.
search.__doc__ = _search_tool_description()
search_mcp.add_tool(
    Tool.from_function(search, annotations=ToolAnnotations(readOnlyHint=True))
)
