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

import contextlib
import importlib.resources
import json
import subprocess
import sys
import threading
import os
from typing import Any, Dict, List, Optional

TOOLS_CONFIG_ENV_VAR = "GOOGLE_ADS_MCP_TOOLS_CONFIG"


def bundled_tools_config_path() -> str:
    """Returns the absolute path of the tools_config.yaml inside ads_mcp.

    Resolved through the package itself, so it points at whichever copy of
    ads_mcp is actually importable rather than at a path relative to this
    file or to the caller's working directory.
    """
    path = importlib.resources.files("ads_mcp").joinpath("tools_config.yaml")
    if not path.is_file():
        raise RuntimeError(
            "Bundled ads_mcp/tools_config.yaml not found; smoke runs cannot "
            "be pinned to a deterministic tool set."
        )
    return os.fspath(path)


def start_server_process() -> subprocess.Popen:
    """Starts the MCP server as a subprocess."""
    # Ensure the server runs in stdio mode by clearing OAuth proxy env vars.
    # Also pin the tools config to the bundled default: clearing the env var
    # is not enough, because the server then falls back to a
    # ./tools_config.yaml in whatever directory the run happens to start
    # from. Pinning the absolute bundled path removes both that cwd
    # fallback and any ambient GOOGLE_ADS_MCP_TOOLS_CONFIG, so the exposed
    # tool list — and therefore the goldens — cannot depend on the
    # developer's machine.
    env = os.environ.copy()
    env.pop("GOOGLE_ADS_MCP_OAUTH_CLIENT_ID", None)
    env.pop("GOOGLE_ADS_MCP_OAUTH_CLIENT_SECRET", None)
    env[TOOLS_CONFIG_ENV_VAR] = bundled_tools_config_path()

    return subprocess.Popen(
        [sys.executable, "-m", "ads_mcp.server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=sys.stderr,
        env=env,
        text=True,
        bufsize=0,  # Unbuffered
    )


def send_request(
    process: subprocess.Popen,
    method: str,
    params: Optional[Dict[str, Any]] = None,
    req_id: Optional[int] = 1,
) -> None:
    """Sends a JSON-RPC request or notification to the server."""
    request = {
        "jsonrpc": "2.0",
        "method": method,
    }
    if req_id is not None:
        request["id"] = req_id

    if params:
        request["params"] = params

    json_req = json.dumps(request)
    process.stdin.write(json_req + "\n")
    process.stdin.flush()


def read_response(process: subprocess.Popen) -> Dict[str, Any]:
    """Reads a JSON-RPC response from the server."""
    for line in process.stdout:
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    raise RuntimeError("Server closed connection without response")


@contextlib.contextmanager
def initialized_server():
    """Context manager that starts, initializes, and cleans up an MCP server process."""
    process = start_server_process()
    try:
        # Initialize
        send_request(
            process,
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "smoke-test", "version": "1.0"},
            },
            req_id=1,
        )

        # Read initialize response
        response = read_response(process)
        if "error" in response:
            raise RuntimeError(f"Initialize failed: {response['error']}")

        # Send initialized notification
        send_request(process, "notifications/initialized", req_id=None)

        yield process
    finally:
        if process.stdin:
            process.stdin.close()
        if process.stdout:
            process.stdout.close()
        process.terminate()
        process.wait()


def get_tools_list() -> Dict[str, Any]:
    """Runs the server and retrieves the list of tools."""
    with initialized_server() as process:
        send_request(process, "tools/list", req_id=2)
        response = read_response(process)

        if "error" in response:
            raise RuntimeError(f"tools/list failed: {response['error']}")

        return response["result"]


def get_resources_list() -> Dict[str, Any]:
    """Runs the server and retrieves the list of resources."""
    with initialized_server() as process:
        send_request(process, "resources/list", req_id=2)
        response = read_response(process)

        if "error" in response:
            raise RuntimeError(f"resources/list failed: {response['error']}")

        return response["result"]


def inject_customer_id(prompt: str) -> str:
    """Replaces {customer_id} placeholder with GOOGLE_ADS_CUSTOMER_ID env var."""
    customer_id = os.environ.get("GOOGLE_ADS_CUSTOMER_ID", "1234567890")
    return prompt.replace("{customer_id}", customer_id)


def call_tool(name: str, arguments: dict) -> Dict[str, Any]:
    """Runs the server and calls a specific tool."""
    with initialized_server() as process:
        send_request(
            process,
            "tools/call",
            {"name": name, "arguments": arguments},
            req_id=2,
        )
        response = read_response(process)

        if "error" in response:
            raise RuntimeError(f"tools/call failed: {response['error']}")

        return response["result"]
