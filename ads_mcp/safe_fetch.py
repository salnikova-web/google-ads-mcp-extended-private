# Copyright 2026 the google-ads-mcp-extended contributors.
# Licensed under the Apache License, Version 2.0.

"""SSRF-resistant fetching of user-supplied image sources.

The image upload tools accept a source chosen by the caller. In a hosted
deployment that caller is any authenticated user, so a naive
``urllib.request.urlopen`` would let them reach the internal network, probe
metadata endpoints and read arbitrary container files.

The fetcher here is deliberately strict:

* HTTPS only -- plain ``http://`` is rejected.
* Every address the hostname resolves to must be public. Private, loopback,
  link-local (including the cloud metadata range), reserved, multicast and
  unspecified addresses are refused.
* The connection is made to the *validated* address, so a name that resolves
  differently the second time (DNS rebinding) cannot redirect the socket to an
  internal host. TLS still uses the original hostname for SNI and certificate
  validation.
* Redirects are never followed; a 3xx response is an error.
* The body is read with an explicit cap, so an oversized response is rejected
  without being buffered in full.
* The payload must start with JPEG or PNG magic bytes.

Local file paths are only honoured when ``GOOGLE_ADS_MCP_ALLOW_LOCAL_FILES``
is enabled, which is appropriate for a local stdio server and never for a
shared hosted one.
"""

import http.client
import ipaddress
import os
import re
import socket
import ssl
import urllib.parse

from fastmcp.exceptions import ToolError

__all__ = [
    "ALLOW_LOCAL_FILES_ENV",
    "local_files_allowed",
    "read_image_source",
]

ALLOW_LOCAL_FILES_ENV = "GOOGLE_ADS_MCP_ALLOW_LOCAL_FILES"

_DEFAULT_TIMEOUT = 30

# Magic bytes for the two formats Google Ads accepts for image assets.
_JPEG_MAGIC = b"\xff\xd8\xff"
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

_TRUTHY = {"1", "true", "yes", "on"}

# Any "scheme:" prefix, so sources like file:// or gopher:// are rejected
# explicitly instead of being mistaken for a relative path.
_URL_SCHEME = re.compile(r"\A([a-zA-Z][a-zA-Z0-9+.\-]*):")


def local_files_allowed() -> bool:
    """Returns True when reading local files has been explicitly enabled."""
    return os.environ.get(ALLOW_LOCAL_FILES_ENV, "").strip().lower() in _TRUTHY


def _assert_public_address(raw_address: str, host: str) -> None:
    """Raises ToolError unless the address is a routable public one."""
    address = ipaddress.ip_address(raw_address)
    if (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    ):
        raise ToolError(
            f"Refusing to fetch '{host}': it resolves to the non-public "
            f"address {raw_address}. Only public HTTPS hosts are allowed."
        )


def _resolve_public_address(host: str, port: int) -> str:
    """Resolves the host and returns one validated address to dial.

    Every address the name resolves to is validated, so a hostname that mixes
    public and internal records cannot be used to slip through.
    """
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise ToolError(f"Could not resolve host '{host}': {e}")

    if not infos:
        raise ToolError(f"Could not resolve host '{host}'.")

    for _, _, _, _, sockaddr in infos:
        _assert_public_address(sockaddr[0], host)

    _, _, _, _, first_sockaddr = infos[0]
    return first_sockaddr[0]


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS connection that dials a pre-validated address.

    ``self.host`` stays the original hostname so SNI and certificate
    verification behave normally; only the address we connect to is pinned.
    """

    def __init__(self, host, pinned_address, **kwargs):
        super().__init__(host, **kwargs)
        self._pinned_address = pinned_address

    def connect(self):
        self.sock = socket.create_connection(
            (self._pinned_address, self.port),
            self.timeout,
        )
        self.sock = self._context.wrap_socket(
            self.sock, server_hostname=self.host
        )


def _fetch_https(url: str, max_bytes: int, timeout: int) -> bytes:
    parsed = urllib.parse.urlsplit(url)

    if parsed.scheme != "https":
        raise ToolError(
            f"Only https:// image URLs are supported, got '{parsed.scheme}://'."
        )

    host = parsed.hostname
    if not host:
        raise ToolError(f"Image URL has no host: {url}")

    port = parsed.port or 443
    address = _resolve_public_address(host, port)

    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"

    connection = _PinnedHTTPSConnection(
        host,
        address,
        port=port,
        timeout=timeout,
        context=ssl.create_default_context(),
    )
    try:
        # No explicit Host header: http.client builds one from ``self.host``
        # and ``self.port`` (bracketing IPv6, omitting the default port). The
        # header used to be built from ``parsed.netloc``, which also carries
        # any ``user:password@`` userinfo -- that would have leaked the
        # credentials of a URL like https://u:p@host/img.png into the request
        # (and into any log on the far side) for no benefit.
        connection.request("GET", path)
        response = connection.getresponse()

        if 300 <= response.status < 400:
            location = response.getheader("Location", "")
            raise ToolError(
                f"Image URL redirected to '{location}'. Redirects are not "
                "followed; pass the final https URL instead."
            )
        if response.status != 200:
            raise ToolError(
                f"Could not download image: HTTP {response.status} "
                f"{response.reason}"
            )

        # Read one byte past the limit so oversized bodies are detected
        # without buffering the whole response.
        data = response.read(max_bytes + 1)
    except ToolError:
        raise
    except (OSError, http.client.HTTPException, ssl.SSLError) as e:
        raise ToolError(f"Could not download image: {e}")
    finally:
        connection.close()

    return data


def _read_local(path: str, max_bytes: int) -> bytes:
    if not local_files_allowed():
        raise ToolError(
            "Reading local files is disabled. Pass an https:// URL, or set "
            f"{ALLOW_LOCAL_FILES_ENV}=1 to allow local paths (only safe for a "
            "local stdio server, never for a hosted one)."
        )
    if not os.path.isfile(path):
        raise ToolError(f"File not found: {path}")
    with open(path, "rb") as f:
        return f.read(max_bytes + 1)


def read_image_source(
    image_source: str,
    max_bytes: int,
    timeout: int = _DEFAULT_TIMEOUT,
) -> bytes:
    """Returns the image bytes for a URL or local path, or raises ToolError.

    Args:
        image_source: An https:// URL, or an absolute local path when
            ``GOOGLE_ADS_MCP_ALLOW_LOCAL_FILES`` is enabled.
        max_bytes: Maximum accepted size; larger sources are rejected without
            being read in full.
        timeout: Socket timeout in seconds for the HTTPS case.
    """
    source = (image_source or "").strip()
    if not source:
        raise ToolError("image_source must not be empty.")

    lowered = source.lower()
    if lowered.startswith("https://"):
        data = _fetch_https(source, max_bytes, timeout)
    elif lowered.startswith("http://"):
        raise ToolError(
            "Plain http:// image URLs are not allowed; use https://."
        )
    elif _URL_SCHEME.match(source):
        scheme = _URL_SCHEME.match(source).group(1)
        raise ToolError(
            f"Unsupported image source scheme '{scheme}:'; use https://."
        )
    else:
        data = _read_local(source, max_bytes)

    if len(data) > max_bytes:
        raise ToolError(f"Image exceeds the {max_bytes} byte limit.")
    if not data:
        raise ToolError("Image source returned no data.")
    if not (data.startswith(_JPEG_MAGIC) or data.startswith(_PNG_MAGIC)):
        raise ToolError(
            "Image does not look like a JPEG or PNG file (unexpected magic "
            "bytes). Google Ads image assets must be JPEG or PNG."
        )
    return data
