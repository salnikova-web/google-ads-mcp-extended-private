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

"""Test cases for the server module."""

import unittest
from unittest.mock import patch


class TestUtils(unittest.TestCase):
    """Test cases for the server module."""

    def test_server_initialization(self):
        """Tests that the MCP server instance is initialized.

        This servers as a smoke test to confirm there are no obvious issues
        with initialization, such as missing imports.
        """
        from ads_mcp import server

        self.assertIsNotNone(server.mcp, "MCP server instance not initialized")

    def test_build_startup_line_returns_expected_prefix(self):
        """Tests that the startup line names the package."""
        from ads_mcp import server

        line = server._build_startup_line()
        self.assertTrue(line.startswith("google-ads-mcp "))

    @patch("ads_mcp.server.importlib.metadata.distribution")
    @patch("ads_mcp.server.importlib.metadata.version")
    def test_build_startup_line_degrades_to_unknown(
        self, mock_version, mock_distribution
    ):
        """Tests that lookup failures degrade to "unknown" without raising."""
        from ads_mcp import server

        mock_version.side_effect = Exception("no version metadata")
        mock_distribution.side_effect = Exception("no distribution metadata")

        line = server._build_startup_line()

        self.assertEqual(line, "google-ads-mcp unknown (commit unknown)")
