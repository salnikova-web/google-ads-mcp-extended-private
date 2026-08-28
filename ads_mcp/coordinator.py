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

"""Module declaring the singleton MCP instance.

The singleton allows other modules to register their tools with the same MCP
server using `@mcp.tool` annotations, thereby 'coordinating' the bootstrapping
of the server.
"""

import os
from typing import Sequence
from fastmcp import FastMCP
from fastmcp.server.auth.providers.google import GoogleProvider
from fastmcp.server.providers import FastMCPProvider
from fastmcp.server.transforms import Transform

from ads_mcp.middleware import GoogleAdsErrorMiddleware

_CLIENT_ID = os.environ.get("GOOGLE_ADS_MCP_OAUTH_CLIENT_ID")
_CLIENT_SECRET = os.environ.get("GOOGLE_ADS_MCP_OAUTH_CLIENT_SECRET")
_BASE_URL = os.environ.get("GOOGLE_ADS_MCP_BASE_URL", "http://localhost:8080")

if _CLIENT_ID and _CLIENT_SECRET:
    auth = GoogleProvider(
        client_id=_CLIENT_ID,
        client_secret=_CLIENT_SECRET,
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
    from ads_mcp.config import ToolsConfig
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

    config = ToolsConfig.load()

    for category, sub_mcp in sub_servers.items():
        if not config.is_namespace_enabled(category):
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
