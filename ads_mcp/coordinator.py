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

"""Declares the parent MCP instance and mounts the tool sub-servers onto it.

Each module under ``ads_mcp.tools`` defines its own ``FastMCP`` sub-server
(e.g. ``search_mcp = FastMCP("search")``) and registers its tools on that
sub-server -- not on this module's singleton via ``@mcp.tool``. There is no
central registry of sub-servers to keep in sync: ``initialize_and_mount_tools``
finds them by iterating the ``ads_mcp.tools`` package with ``pkgutil`` and
scanning each imported module's attributes for ``FastMCP`` instances. Every
discovered sub-server is then mounted onto the parent through a
``FastMCPProvider`` wrapped in a ``Transform`` (``_EnabledToolsFilter``) that
filters tools according to ``ToolsConfig`` -- enabled namespaces and
per-tool ``enabled_tools`` overrides -- at mount time, rather than by
removing tools from the (module-level, shared) sub-server object itself.
"""

import logging
import os
from typing import Sequence
from fastmcp import FastMCP
from fastmcp.server.auth.providers.google import GoogleProvider
from fastmcp.server.providers import FastMCPProvider
from fastmcp.server.transforms import Transform

from ads_mcp.config import (
    OAUTH_CLIENT_ID_ENV_VAR,
    OAUTH_CLIENT_SECRET_ENV_VAR,
    oauth_configured,
)
from ads_mcp.middleware import GoogleAdsErrorMiddleware

logger = logging.getLogger(__name__)

_BASE_URL = os.environ.get("GOOGLE_ADS_MCP_BASE_URL", "http://localhost:8080")

if oauth_configured():
    auth = GoogleProvider(
        client_id=os.environ[OAUTH_CLIENT_ID_ENV_VAR],
        client_secret=os.environ[OAUTH_CLIENT_SECRET_ENV_VAR],
        base_url=_BASE_URL,
        required_scopes=[
            "openid",
            "https://www.googleapis.com/auth/userinfo.email",
            "https://www.googleapis.com/auth/userinfo.profile",
            "https://www.googleapis.com/auth/adwords",
        ],
    )
    mcp = FastMCP("Google Ads Server", auth=auth)
else:
    mcp = FastMCP("Google Ads Server")


class _EnabledToolsFilter(Transform):
    """Hides the tools that the configuration disables for one category.

    The sub-servers are module-level singletons (``importlib`` caches the
    tool modules), so removing tools from them would leak one mount's
    configuration into every later mount in the same process. Filtering in
    the provider chain instead leaves the shared objects untouched: the
    filter applies to listing and to lookup, so a disabled tool can neither
    be seen nor called through the parent server.
    """

    def __init__(self, config, category: str):
        self._config = config
        self._category = category

    def _is_enabled(self, tool_name: str) -> bool:
        return self._config.is_tool_enabled(self._category, tool_name)

    async def list_tools(self, tools: Sequence) -> Sequence:
        return [tool for tool in tools if self._is_enabled(tool.name)]

    async def get_tool(self, name: str, call_next, *, version=None):
        tool = await call_next(name, version=version)
        if tool is None or not self._is_enabled(tool.name):
            return None
        return tool


def initialize_and_mount_tools(parent_mcp: FastMCP) -> None:
    """Loads the tools configuration and dynamically mounts the tools sub-servers."""
    from ads_mcp.config import ALL_CATEGORIES, ToolsConfig
    import importlib
    import pkgutil
    import ads_mcp.tools as tools_pkg

    # One error translator for the whole server: it runs on the parent's
    # tools/call chain, so it covers every namespace mounted below, including
    # tools that do no error handling of their own. Re-mounting an already
    # initialized server must not stack a second copy.
    if not any(
        isinstance(existing, GoogleAdsErrorMiddleware)
        for existing in parent_mcp.middleware
    ):
        parent_mcp.add_middleware(GoogleAdsErrorMiddleware())

    # Map of category name -> FastMCP sub-server
    sub_servers = {}

    # Discover and dynamically load all tool modules
    for _, module_name, _ in pkgutil.iter_modules(tools_pkg.__path__):
        full_module_name = f"ads_mcp.tools.{module_name}"
        module = importlib.import_module(full_module_name)

        # Find any FastMCP instances defined in the module
        for attr_name in dir(module):
            attr_val = getattr(module, attr_name)
            if isinstance(attr_val, FastMCP):
                category = attr_val.name
                sub_servers[category] = attr_val

    # Mount-time sanity check: every discovered sub-server should be a name
    # the rest of the config system knows about. This is a warning, not a
    # crash -- a fork adding a 17th tools module must still be able to start
    # up -- but the trap is real: a new category has to be registered in
    # three places (the module itself, ALL_CATEGORIES in config.py, and
    # tools_config.yaml) to ever become mountable, and it is easy to do the
    # first without the other two.
    unregistered = sorted(set(sub_servers) - set(ALL_CATEGORIES))
    if unregistered:
        logger.warning(
            "Discovered sub-server(s) not in ads_mcp.config.ALL_CATEGORIES: "
            "%s. A tools module must be registered in three places -- its "
            "own module, ALL_CATEGORIES, and tools_config.yaml -- or it "
            "will never be mountable.",
            ", ".join(unregistered),
        )

    config = ToolsConfig.load()

    for category, sub_mcp in sub_servers.items():
        if not config.is_namespace_enabled(category):
            is_unregistered = category not in ALL_CATEGORIES
            is_unmentioned = not config.is_namespace_mentioned(category)
            if is_unregistered and is_unmentioned:
                logger.warning(
                    "Sub-server %r discovered but not configured/enabled "
                    "-- it will not be mounted.",
                    category,
                )
            continue

        # Determine prefix/namespace
        namespace_prefix = config.get_namespace_prefix(category)

        # Mount through a provider that filters disabled tools instead of
        # removing them from the shared sub-server (see _EnabledToolsFilter).
        # This mirrors what FastMCP.mount() does internally, with the filter
        # inserted before the namespace is applied so the configuration can
        # keep matching on the original tool names.
        provider = FastMCPProvider(sub_mcp).wrap_transform(
            _EnabledToolsFilter(config, category)
        )
        parent_mcp.add_provider(provider, namespace=namespace_prefix or "")


# Automatically initialize and mount tools upon import
initialize_and_mount_tools(mcp)
