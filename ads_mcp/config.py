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

"""Configuration management for the Google Ads MCP server."""

import os
import importlib.resources
from typing import Any, Dict
import yaml
import logging

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_FILE = "tools_config.yaml"

# Environment variable used to point the server at an explicit config file.
CONFIG_PATH_ENV_VAR = "GOOGLE_ADS_MCP_TOOLS_CONFIG"

# Default categories that are supported by the server
ALL_CATEGORIES = [
    "customers",
    "search",
    "metadata",
    "mutate",
    "demandgen",
    "pmax",
    "extensions",
    "targeting",
    "shopping",
    "video",
    "display",
    "tracking",
    "audiences",
    "optimize",
    "negatives",
    "experiments",
]


class ToolsConfig:
    """Manages tool registration configuration parsed from YAML."""

    def __init__(self, config_dict: Dict[str, Any] | None = None):
        self._config = config_dict or {}
        self._warn_unknown_namespaces()

    def _namespaces(self) -> Dict[str, Any] | None:
        """Returns the configured namespaces mapping, or None if unconfigured.

        Distinguishes "no configuration at all" from "configured to enable
        nothing": a missing ``namespaces`` key returns None (callers then fall
        back to their enable-everything default), while a key that is present
        but empty -- ``{}``, or ``namespaces:`` with no children, which YAML
        parses as None -- returns an empty mapping and therefore enables
        nothing.
        """
        if "namespaces" not in self._config:
            return None

        namespaces = self._config["namespaces"] or {}
        if not isinstance(namespaces, dict):
            raise ValueError(
                "'namespaces' must be a YAML mapping/dictionary, got "
                f"{type(namespaces).__name__}."
            )
        return namespaces

    def _warn_unknown_namespaces(self) -> None:
        """Logs namespace keys the server does not know about.

        A typo such as ``demand_gen`` is otherwise silently ignored, leaving
        the namespace the user meant to configure at its default.
        """
        namespaces = self._namespaces()
        if not namespaces:
            return

        unknown = [key for key in namespaces if key not in ALL_CATEGORIES]
        if unknown:
            logger.warning(
                "Ignoring unknown tool namespace(s) in configuration: %s. "
                "Known namespaces: %s.",
                ", ".join(str(key) for key in unknown),
                ", ".join(ALL_CATEGORIES),
            )

    @classmethod
    def _resolve_config_path(cls, filepath: str | None) -> str | None:
        """Resolves which config file to load.

        Resolution order:
          1. An explicit ``filepath`` argument, if provided.
          2. The ``GOOGLE_ADS_MCP_TOOLS_CONFIG`` environment variable.
          3. ``tools_config.yaml`` in the current working directory.
          4. The default ``tools_config.yaml`` bundled with the package.

        Returns the resolved path, or ``None`` if no config file can be found.
        """
        # 1 & 2: an explicitly requested config must exist; otherwise it is a
        # user error and we surface it rather than silently falling back.
        explicit = filepath or os.environ.get(CONFIG_PATH_ENV_VAR)
        if explicit:
            if not os.path.exists(explicit):
                raise FileNotFoundError(
                    f"Tools configuration file '{explicit}' not found."
                )
            logger.info(
                "Using explicitly requested tools configuration '%s'.",
                explicit,
            )
            return explicit

        # 3: a config file in the working directory acts as a user override.
        if os.path.exists(DEFAULT_CONFIG_FILE):
            logger.info(
                "Using tools configuration '%s' found in the working "
                "directory.",
                os.path.abspath(DEFAULT_CONFIG_FILE),
            )
            return DEFAULT_CONFIG_FILE

        # 4: fall back to the default config bundled with the package so that
        # installed deployments (e.g. ``pipx run``) work without extra setup.
        bundled = importlib.resources.files("ads_mcp").joinpath(
            DEFAULT_CONFIG_FILE
        )
        if bundled.is_file():
            logger.info(
                "No local '%s' found; using the bundled default "
                "configuration '%s'.",
                DEFAULT_CONFIG_FILE,
                bundled,
            )
            return str(bundled)

        return None

    @classmethod
    def load(cls, filepath: str | None = None) -> "ToolsConfig":
        """Loads configuration from a YAML file.

        Resolves the config path (explicit argument, ``GOOGLE_ADS_MCP_TOOLS_CONFIG``
        env var, working-directory file, then the bundled default). Raises if a
        resolved file is missing or corrupt. If no config can be resolved at all,
        falls back to enabling all default tool namespaces.
        """
        resolved = cls._resolve_config_path(filepath)
        if resolved is None:
            logger.warning(
                "No tools configuration file found; enabling all default tool "
                "namespaces (%s).",
                ", ".join(ALL_CATEGORIES),
            )
            return cls()

        try:
            with open(resolved, "r") as file:
                data = yaml.safe_load(file)
                if not isinstance(data, dict):
                    raise ValueError(
                        "Configuration root must be a YAML mapping/dictionary"
                    )
                return cls(data)
        except Exception as e:
            raise ValueError(
                f"Failed to parse configuration file '{resolved}': {e}"
            ) from e

    def is_namespace_enabled(self, category: str) -> bool:
        """Determines if a tool category/namespace is enabled."""
        namespaces = self._namespaces()
        if namespaces is None:
            # By default, if no config is specified, all known categories are enabled
            return category in ALL_CATEGORIES

        category_config = namespaces.get(category)
        if category_config is None:
            return False

        if isinstance(category_config, bool):
            return category_config

        if isinstance(category_config, str):
            return True

        if isinstance(category_config, dict):
            return category_config.get("enabled", True)

        return False

    def get_namespace_prefix(self, category: str) -> str | None:
        """Returns the prefix/namespace to use for the category.

        Returns None if no prefix should be applied.
        """
        namespaces = self._namespaces()
        if namespaces is None:
            return category

        category_config = namespaces.get(category)
        if isinstance(category_config, str):
            return category_config

        if isinstance(category_config, dict):
            # If explicit prefix is given, use it
            if "prefix" in category_config:
                return category_config["prefix"]
            # Default to category name if enabled_tools dict is provided
            return category

        if category_config is True:
            return category

        return None

    def is_tool_enabled(self, category: str, tool_name: str) -> bool:
        """Determines if a specific tool within a category is enabled."""
        if not self.is_namespace_enabled(category):
            return False

        namespaces = self._namespaces()
        if namespaces is None:
            return True

        category_config = namespaces.get(category)
        if not isinstance(category_config, dict):
            # If category is enabled as a simple boolean or string, all tools in it are enabled
            return True

        enabled_tools = category_config.get("enabled_tools")
        if enabled_tools is None:
            # No explicit enabled_tools filter means all are enabled
            return True

        # Handle list of dictionaries or list of strings
        # Format from proposal:
        # enabled_tools:
        #   - create_asset: true
        #   - upload_video: true
        if isinstance(enabled_tools, list):
            for item in enabled_tools:
                if isinstance(item, dict):
                    if tool_name in item:
                        return bool(item[tool_name])
                elif isinstance(item, str):
                    if item == tool_name:
                        return True
            return False

        # The natural YAML spelling (enabled_tools: {tool: false}) parses to a
        # mapping; treat it like the list form rather than silently enabling
        # everything, including the tool the user tried to disable.
        if isinstance(enabled_tools, dict):
            if tool_name in enabled_tools:
                return bool(enabled_tools[tool_name])
            return False

        raise ValueError(
            f"enabled_tools for namespace '{category}' must be a list or a "
            f"mapping, got {type(enabled_tools).__name__}"
        )
