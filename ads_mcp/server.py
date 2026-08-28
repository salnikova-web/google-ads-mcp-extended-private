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

"""Entry point for the MCP server."""

from ads_mcp.config import oauth_configured
from ads_mcp.coordinator import mcp

# The following imports are necessary to register the resources with the `mcp`
# object, even though they are not directly used in this file.
# Tools are loaded dynamically via reflection in coordinator.py.
# The `# noqa: F401` comment tells the linter to ignore the "unused import"
# warning.
from ads_mcp.resources import (
    discovery,
    metrics,
    release_notes,
    segments,
)  # noqa: F401


import importlib.metadata
import json
import logging
import os
import sys


def _configure_stderr_logging() -> None:
    """Gives ads_mcp's own warnings a real destination in this process.

    ads_mcp modules attach only a ``NullHandler`` to their loggers (see
    ``utils.py``, ``middleware.py``) because configuring logging is the
    host application's job, not a library's. This entrypoint IS that host,
    so it is the one place allowed to attach a real handler.

    Without this, a ``logger.warning`` call in ``ads_mcp.utils`` or
    ``ads_mcp.middleware`` never reaches stderr: fastmcp configures only
    its own "fastmcp" logger (with ``propagate=False``) and never touches
    the root logger, and the ``NullHandler`` already on the "ads_mcp.*"
    loggers counts as a handler having "found" the record, which silently
    suppresses logging's own ``lastResort`` fallback even though nothing
    ever actually printed the record anywhere.

    Guarded against attaching twice (e.g. ``run_server`` invoked more than
    once in the same process, such as under test).
    """
    package_logger = logging.getLogger("ads_mcp")
    already_configured = any(
        isinstance(existing, logging.StreamHandler)
        for existing in package_logger.handlers
    )
    if already_configured:
        return

    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(logging.WARNING)
    handler.setFormatter(
        logging.Formatter("%(name)s %(levelname)s: %(message)s")
    )
    package_logger.addHandler(handler)
    package_logger.setLevel(logging.WARNING)


def _build_startup_line() -> str:
    """Builds the one-line startup banner: "google-ads-mcp <version> (commit <commit>)".

    Both lookups are wrapped in try/except so any failure (a source checkout
    with no installed distribution metadata, a pip install -e . with no
    direct_url.json, etc.) degrades that field to "unknown" instead of
    preventing the server from starting.
    """
    try:
        version = importlib.metadata.version("google-ads-mcp")
    except Exception:
        version = "unknown"

    try:
        direct_url_text = importlib.metadata.distribution(
            "google-ads-mcp"
        ).read_text("direct_url.json")
        commit = json.loads(direct_url_text)["vcs_info"]["commit_id"]
    except Exception:
        commit = "unknown"

    return f"google-ads-mcp {version} (commit {commit})"


def run_server() -> None:
    port = int(os.environ.get("PORT", "8080"))

    # stdout carries JSON-RPC under the stdio transport; never print here.
    print(_build_startup_line(), file=sys.stderr)
    _configure_stderr_logging()

    if oauth_configured():
        mcp.run(
            transport="streamable-http",
            port=port,
            host="0.0.0.0",
            uvicorn_config={"access_log": False},
        )
    else:
        mcp.run()


if __name__ == "__main__":
    run_server()
