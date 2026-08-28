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

"""Test cases for the config module."""

import unittest
from unittest.mock import patch, mock_open
from ads_mcp.config import (
    ALL_CATEGORIES,
    OAUTH_CLIENT_ID_ENV_VAR,
    OAUTH_CLIENT_SECRET_ENV_VAR,
    ToolsConfig,
    oauth_configured,
)


class TestToolsConfig(unittest.TestCase):
    """Test cases for ToolsConfig parser."""

    def test_default_config_exposes_all_with_default_namespaces(self):
        """Tests that if no config is supplied, all default namespaces are enabled."""
        config = ToolsConfig()
        self.assertTrue(config.is_namespace_enabled("customers"))
        self.assertTrue(config.is_namespace_enabled("search"))
        self.assertTrue(config.is_namespace_enabled("metadata"))
        self.assertFalse(config.is_namespace_enabled("unknown_category"))

        self.assertEqual(config.get_namespace_prefix("customers"), "customers")
        self.assertEqual(config.get_namespace_prefix("search"), "search")

        self.assertTrue(config.is_tool_enabled("search", "search"))

    def test_absent_namespaces_key_enables_all_default_categories(self):
        """Tests that a config without a 'namespaces' key enables every category."""
        config = ToolsConfig({})
        for category in ALL_CATEGORIES:
            self.assertTrue(config.is_namespace_enabled(category))
            self.assertTrue(config.is_tool_enabled(category, "any_tool"))
        self.assertFalse(config.is_namespace_enabled("unknown_category"))
        self.assertFalse(config.is_tool_enabled("unknown_category", "any_tool"))

    def test_empty_namespaces_mapping_enables_nothing(self):
        """Tests that 'namespaces: {}' disables every category."""
        config = ToolsConfig({"namespaces": {}})
        for category in ALL_CATEGORIES:
            self.assertFalse(config.is_namespace_enabled(category))
            self.assertFalse(config.is_tool_enabled(category, "any_tool"))

    def test_null_namespaces_enables_nothing(self):
        """Tests that a bare 'namespaces:' key disables every category."""
        # YAML parses "namespaces:" with no children as None rather than {},
        # and both spellings must mean "enable nothing", not "enable all".
        config = ToolsConfig({"namespaces": None})
        for category in ALL_CATEGORIES:
            self.assertFalse(config.is_namespace_enabled(category))
            self.assertFalse(config.is_tool_enabled(category, "any_tool"))

    def test_non_mapping_namespaces_raises_value_error(self):
        """Tests that a non-mapping 'namespaces' value is rejected."""
        with self.assertRaises(ValueError):
            ToolsConfig({"namespaces": ["customers", "search"]})

    def test_boolean_namespaces(self):
        """Tests namespaces enabled via simple booleans."""
        data = {
            "namespaces": {
                "customers": True,
                "search": False,
            }
        }
        config = ToolsConfig(data)
        self.assertTrue(config.is_namespace_enabled("customers"))
        self.assertFalse(config.is_namespace_enabled("search"))
        # Not listed => False by default if namespaces dict is present
        self.assertFalse(config.is_namespace_enabled("metadata"))

        self.assertEqual(config.get_namespace_prefix("customers"), "customers")
        self.assertTrue(
            config.is_tool_enabled("customers", "list_accessible_customers")
        )
        self.assertFalse(config.is_tool_enabled("search", "search"))

    def test_custom_namespace_prefixes(self):
        """Tests namespaces configured with custom prefix strings."""
        data = {
            "namespaces": {
                "customers": "accounts",
                "search": "query_engine",
            }
        }
        config = ToolsConfig(data)
        self.assertTrue(config.is_namespace_enabled("customers"))
        self.assertTrue(config.is_namespace_enabled("search"))

        self.assertEqual(config.get_namespace_prefix("customers"), "accounts")
        self.assertEqual(config.get_namespace_prefix("search"), "query_engine")

    def test_fine_grained_tool_enablement(self):
        """Tests selectively enabling/disabling specific tools under a namespace."""
        data = {
            "namespaces": {
                "customers": {
                    "enabled": True,
                    "prefix": "users",
                    "enabled_tools": [
                        {"list_accessible_customers": True},
                        {"another_tool": False},
                    ],
                },
                "search": {
                    "enabled": True,
                    # No prefix specified, defaults to category name
                    "enabled_tools": ["search"],
                },
            }
        }
        config = ToolsConfig(data)
        self.assertTrue(config.is_namespace_enabled("customers"))
        self.assertTrue(config.is_namespace_enabled("search"))

        self.assertEqual(config.get_namespace_prefix("customers"), "users")
        self.assertEqual(config.get_namespace_prefix("search"), "search")

        self.assertTrue(
            config.is_tool_enabled("customers", "list_accessible_customers")
        )
        self.assertFalse(config.is_tool_enabled("customers", "another_tool"))
        self.assertFalse(config.is_tool_enabled("customers", "unlisted_tool"))

        self.assertTrue(config.is_tool_enabled("search", "search"))
        self.assertFalse(
            config.is_tool_enabled("search", "unlisted_search_tool")
        )

    def test_enabled_tools_mapping_filters_like_the_list_form(self):
        """Tests that an 'enabled_tools' mapping acts as a tool filter.

        The documented filter format is a list (of names, or of single-entry
        mappings), but the natural YAML spelling is a plain mapping; both
        must behave the same -- in particular, a tool explicitly marked
        False must be disabled, and unmentioned tools default to disabled.
        """
        data = {
            "namespaces": {
                "customers": {
                    "enabled": True,
                    "enabled_tools": {
                        "list_accessible_customers": True,
                        "another_tool": False,
                    },
                }
            }
        }
        config = ToolsConfig(data)
        self.assertTrue(config.is_namespace_enabled("customers"))
        self.assertTrue(
            config.is_tool_enabled("customers", "list_accessible_customers")
        )
        self.assertFalse(config.is_tool_enabled("customers", "another_tool"))
        self.assertFalse(config.is_tool_enabled("customers", "unlisted_tool"))

    def test_enabled_tools_invalid_type_raises_value_error_at_construction(
        self,
    ):
        """Tests that a non-list, non-mapping enabled_tools is rejected.

        This is now caught eagerly by ToolsConfig.__init__ (via
        _validate_enabled_tools_shapes), not lazily the first time
        is_tool_enabled happens to touch the malformed namespace -- so
        constructing the config is itself what raises.
        """
        data = {
            "namespaces": {
                "customers": {
                    "enabled": True,
                    "enabled_tools": "list_accessible_customers",
                }
            }
        }
        with self.assertRaises(ValueError):
            ToolsConfig(data)

    def test_valid_enabled_tools_shapes_construct_and_filter_lazily(self):
        """Regression test: valid list/mapping enabled_tools shapes must
        still construct without raising, and is_tool_enabled must still
        filter correctly -- the construction-time shape check must not
        reject the shapes it is supposed to accept.
        """
        data = {
            "namespaces": {
                "customers": {
                    "enabled": True,
                    "enabled_tools": [{"list_accessible_customers": True}],
                },
                "search": {
                    "enabled": True,
                    "enabled_tools": {"search": False},
                },
            }
        }
        config = ToolsConfig(data)  # must not raise
        self.assertTrue(
            config.is_tool_enabled("customers", "list_accessible_customers")
        )
        self.assertFalse(config.is_tool_enabled("customers", "other_tool"))
        self.assertFalse(config.is_tool_enabled("search", "search"))

    @patch("os.path.exists")
    def test_load_missing_file_raises_file_not_found(self, mock_exists):
        """Tests that load raises FileNotFoundError if the config file does not exist."""
        mock_exists.return_value = False
        with self.assertRaises(FileNotFoundError):
            ToolsConfig.load("missing.yaml")

    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open, read_data="invalid-yaml: {")
    def test_load_invalid_file_raises_value_error(self, mock_file, mock_exists):
        """Tests that load raises ValueError if the config file contains invalid YAML."""
        mock_exists.return_value = True
        with self.assertRaises(ValueError):
            ToolsConfig.load("invalid.yaml")

    @patch.dict("os.environ", {"GOOGLE_ADS_MCP_TOOLS_CONFIG": "/env/path.yaml"})
    @patch("os.path.exists", return_value=True)
    @patch(
        "builtins.open",
        new_callable=mock_open,
        read_data="namespaces:\n  customers: true\n",
    )
    def test_load_uses_env_var_path(self, mock_file, mock_exists):
        """Tests that the env var is honored when no explicit path is given."""
        config = ToolsConfig.load()
        mock_file.assert_called_once_with("/env/path.yaml", "r")
        self.assertTrue(config.is_namespace_enabled("customers"))

    @patch.dict(
        "os.environ", {"GOOGLE_ADS_MCP_TOOLS_CONFIG": "/env/missing.yaml"}
    )
    @patch("os.path.exists", return_value=False)
    def test_load_missing_env_var_path_raises(self, mock_exists):
        """Tests that a missing env-var-specified config raises FileNotFoundError."""
        with self.assertRaises(FileNotFoundError):
            ToolsConfig.load()

    @patch.dict("os.environ", {}, clear=True)
    @patch("os.path.exists", return_value=True)
    @patch(
        "builtins.open",
        new_callable=mock_open,
        read_data="namespaces:\n  search: true\n",
    )
    def test_load_cwd_fallback_warns(self, mock_file, mock_exists):
        """Tests that picking up tools_config.yaml from the cwd warns.

        An ads_mcp logger with no real handler attached only surfaces
        WARNING and above (see server._configure_stderr_logging), so this
        line has to be a warning, not info, to ever actually be seen by
        whoever is running the server -- silently picking up whatever
        tools_config.yaml happens to be sitting in the working directory
        is exactly the kind of thing they need to notice.
        """
        with self.assertLogs("ads_mcp.config", level="WARNING") as cm:
            config = ToolsConfig.load()
        self.assertTrue(config.is_namespace_enabled("search"))
        self.assertTrue(
            any("tools_config.yaml" in message for message in cm.output)
        )

    @patch.dict("os.environ", {}, clear=True)
    @patch("os.path.exists", return_value=False)
    @patch("ads_mcp.config.importlib.resources.files")
    def test_load_falls_back_to_bundled_default(self, mock_files, mock_exists):
        """Tests fallback to the package-bundled config when no local file exists."""
        bundled = mock_files.return_value.joinpath.return_value
        bundled.is_file.return_value = True
        bundled.__str__ = lambda self: "/bundled/tools_config.yaml"
        with patch(
            "builtins.open",
            new_callable=mock_open,
            read_data="namespaces:\n  search: true\n",
        ) as mock_file:
            config = ToolsConfig.load()
        mock_file.assert_called_once_with("/bundled/tools_config.yaml", "r")
        self.assertTrue(config.is_namespace_enabled("search"))

    @patch.dict("os.environ", {}, clear=True)
    @patch("os.path.exists", return_value=False)
    @patch("ads_mcp.config.importlib.resources.files")
    def test_load_without_any_config_enables_defaults(
        self, mock_files, mock_exists
    ):
        """Tests that load falls back to all default namespaces if nothing resolves."""
        mock_files.return_value.joinpath.return_value.is_file.return_value = (
            False
        )
        config = ToolsConfig.load()
        self.assertTrue(config.is_namespace_enabled("customers"))
        self.assertTrue(config.is_namespace_enabled("search"))
        self.assertTrue(config.is_namespace_enabled("metadata"))


class TestOauthConfigured(unittest.TestCase):
    """Test cases for oauth_configured()."""

    def test_neither_set_returns_false_silently(self):
        """Tests that leaving OAuth entirely unset is not itself a warning."""
        with patch.dict("os.environ", {}, clear=True):
            with self.assertNoLogs("ads_mcp.config", level="WARNING"):
                self.assertFalse(oauth_configured())

    def test_both_set_returns_true(self):
        """Tests that setting both env vars enables OAuth."""
        with patch.dict(
            "os.environ",
            {
                OAUTH_CLIENT_ID_ENV_VAR: "a-client-id",
                OAUTH_CLIENT_SECRET_ENV_VAR: "a-client-secret",
            },
            clear=True,
        ):
            with self.assertNoLogs("ads_mcp.config", level="WARNING"):
                self.assertTrue(oauth_configured())

    def test_only_client_id_set_warns_and_returns_false(self):
        """Tests that a half-configured OAuth setup is flagged, not silently
        treated the same as OAuth being off on purpose."""
        with patch.dict(
            "os.environ",
            {OAUTH_CLIENT_ID_ENV_VAR: "a-client-id"},
            clear=True,
        ):
            with self.assertLogs("ads_mcp.config", level="WARNING") as cm:
                self.assertFalse(oauth_configured())
        self.assertTrue(
            any(OAUTH_CLIENT_SECRET_ENV_VAR in message for message in cm.output)
        )

    def test_only_client_secret_set_warns_and_returns_false(self):
        """Tests the other half-configured direction (secret without ID)."""
        with patch.dict(
            "os.environ",
            {OAUTH_CLIENT_SECRET_ENV_VAR: "a-client-secret"},
            clear=True,
        ):
            with self.assertLogs("ads_mcp.config", level="WARNING") as cm:
                self.assertFalse(oauth_configured())
        self.assertTrue(
            any(OAUTH_CLIENT_ID_ENV_VAR in message for message in cm.output)
        )
