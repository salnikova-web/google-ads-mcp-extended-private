# Copyright 2026 the google-ads-mcp-extended contributors.
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

"""Tool namespaces for the MCP server.

Deliberately empty of imports. ``ads_mcp.coordinator`` discovers namespaces by
walking this package with ``pkgutil.iter_modules`` and importing each module it
finds, so importing sub-modules here would only make that order harder to
reason about.

The file exists so this is a regular package like ``ads_mcp.resources`` rather
than an implicit namespace package -- the latter works, but it makes the
package's contents depend on every ``ads_mcp/tools`` directory on the path and
leaves setuptools' package discovery to guess.
"""
