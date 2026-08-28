# Copyright 2026 the google-ads-mcp-extended contributors.
# Licensed under the Apache License, Version 2.0.

"""Guard tests for ads_mcp.safe_fetch, the SSRF fence in front of the
agent-supplied ``image_source`` of demandgen_asset_upload_image.

Every test is named after the attack it blocks. No socket is ever opened:
name resolution, ``socket.create_connection`` and the TLS context are
patched, and the connected socket is a canned-response stand-in that the
real ``http.client`` parser reads — so the status-code, redirect and
size-cap branches are exercised as written instead of being mocked away.
"""

import io
import ipaddress
import os
import socket
import ssl
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from fastmcp.exceptions import ToolError

import ads_mcp.safe_fetch as safe_fetch

JPEG = b"\xff\xd8\xff" + b"\x00" * 32
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
GIF = b"GIF89a" + b"\x00" * 32
HTML = b"<!doctype html><html>nope</html>"

PUBLIC_V4 = "93.184.216.34"
PUBLIC_V6 = "2606:2800:220:1:248:1893:25c8:1946"

# Addresses that must never be dialed, with the property that rejects each.
NON_PUBLIC_ADDRESSES = (
    ("127.0.0.1", "loopback"),
    ("127.1.2.3", "loopback (whole /8)"),
    ("10.0.0.7", "private RFC1918"),
    ("172.16.5.4", "private RFC1918"),
    ("192.168.1.1", "private RFC1918"),
    ("169.254.169.254", "link-local: the cloud metadata endpoint"),
    ("0.0.0.0", "unspecified"),
    ("224.0.0.1", "multicast"),
    ("240.0.0.1", "reserved"),
    ("198.18.0.1", "benchmarking range (reserved)"),
    ("::1", "IPv6 loopback"),
    ("::", "IPv6 unspecified"),
    ("fd00::1", "IPv6 unique-local"),
    ("fe80::1", "IPv6 link-local"),
    ("ff02::1", "IPv6 multicast"),
)


def http_response(status=200, reason="OK", body=b"", headers=()):
    """Builds a raw HTTP/1.1 response for the fake socket to replay."""
    lines = [f"HTTP/1.1 {status} {reason}"]
    lines.extend(f"{name}: {value}" for name, value in headers)
    lines.append(f"Content-Length: {len(body)}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("ascii") + body


class ReplayStream(io.BytesIO):
    """A response stream that remembers how much of itself was consumed.

    ``connection.close()`` closes the stream before a test can look at it,
    so the read position is recorded as it moves.
    """

    def __init__(self, data):
        super().__init__(data)
        self.consumed = 0

    def read(self, *args, **kwargs):
        data = super().read(*args, **kwargs)
        self.consumed = self.tell()
        return data

    def readline(self, *args, **kwargs):
        line = super().readline(*args, **kwargs)
        self.consumed = self.tell()
        return line


class FakeSocket:
    """A connected socket that replays one canned HTTP response.

    ``http.client`` only ever asks a socket for ``sendall`` (the request)
    and ``makefile`` (the response stream), so this is enough to run the
    real request/response code path. ``unread`` is how the "read one byte
    past the cap instead of buffering the body" claim is checked.
    """

    def __init__(self, response=b""):
        self.sent = bytearray()
        self.response = response
        self.stream = ReplayStream(response)
        self.closed = False

    def sendall(self, data):
        self.sent.extend(data)

    def makefile(self, *args, **kwargs):
        return self.stream

    def settimeout(self, timeout):
        pass

    def close(self):
        self.closed = True

    @property
    def unread(self):
        return len(self.response) - self.stream.consumed

    @property
    def request_text(self):
        return bytes(self.sent).decode("latin-1")


class HttpsFetchTestCase(unittest.TestCase):
    """Base fixture: resolution, the socket and the TLS context are fakes.

    ``self.addresses`` is what the hostname "resolves" to, so a test can
    hand back an internal record without any DNS.
    """

    def setUp(self):
        self.addresses = [PUBLIC_V4]
        self.socket = FakeSocket(http_response(body=JPEG))

        resolve = patch(
            "ads_mcp.safe_fetch.socket.getaddrinfo",
            side_effect=self.fake_getaddrinfo,
        )
        self.mock_getaddrinfo = resolve.start()
        self.addCleanup(resolve.stop)

        connect = patch(
            "ads_mcp.safe_fetch.socket.create_connection",
            return_value=self.socket,
        )
        self.mock_create_connection = connect.start()
        self.addCleanup(connect.stop)

        self.context = MagicMock(name="ssl_context")
        # Hand the pinned socket straight through, so the assertions can
        # look at what was actually dialed and what SNI name was used.
        self.context.wrap_socket.side_effect = (
            lambda sock, server_hostname=None: sock
        )
        tls = patch(
            "ads_mcp.safe_fetch.ssl.create_default_context",
            return_value=self.context,
        )
        tls.start()
        self.addCleanup(tls.stop)

    def fake_getaddrinfo(self, host, port, **kwargs):
        infos = []
        for address in self.addresses:
            if ":" in address:
                sockaddr = (address, port, 0, 0)
                family = socket.AF_INET6
            else:
                sockaddr = (address, port)
                family = socket.AF_INET
            infos.append((family, socket.SOCK_STREAM, 6, "", sockaddr))
        return infos

    def fetch(self, url="https://images.example.com/logo.jpg", **kwargs):
        kwargs.setdefault("max_bytes", 1_000_000)
        return safe_fetch.read_image_source(url, **kwargs)

    def dialed_address(self):
        return self.mock_create_connection.call_args.args[0]


class TestAddressPinning(HttpsFetchTestCase):
    """The address the socket is opened to, and which addresses are legal."""

    def test_a_public_https_image_is_fetched(self):
        self.assertEqual(self.fetch(), JPEG)

    def test_dns_rebinding_cannot_move_the_socket_to_another_address(self):
        # The name is resolved once and the *validated* address is dialed;
        # a second lookup answering with an internal record never happens.
        self.fetch()
        self.assertEqual(self.mock_getaddrinfo.call_count, 1)
        self.assertEqual(self.dialed_address(), (PUBLIC_V4, 443))

    def test_non_public_addresses_are_refused(self):
        for address, why in NON_PUBLIC_ADDRESSES:
            with self.subTest(address=address, rejected_as=why):
                self.addresses = [address]
                with self.assertRaises(ToolError) as caught:
                    self.fetch()
                self.assertIn("non-public", str(caught.exception))
                self.assertIn(address, str(caught.exception))
                self.mock_create_connection.assert_not_called()

    def test_a_single_internal_record_poisons_the_whole_answer(self):
        # A name that mixes a public and an internal record must not be
        # usable: whichever record is dialed, the answer is refused.
        self.addresses = [PUBLIC_V4, "169.254.169.254"]
        with self.assertRaises(ToolError) as caught:
            self.fetch()
        self.assertIn("169.254.169.254", str(caught.exception))
        self.mock_create_connection.assert_not_called()

    def test_a_public_ipv6_address_is_allowed(self):
        self.addresses = [PUBLIC_V6]
        self.assertEqual(self.fetch(), JPEG)
        self.assertEqual(self.dialed_address(), (PUBLIC_V6, 443))

    @unittest.skipUnless(
        ipaddress.ip_address("::ffff:127.0.0.1").is_loopback
        or ipaddress.ip_address("::ffff:127.0.0.1").is_private,
        "IPv4-mapped classification lands only in CPython 3.9.20/3.10.15/"
        "3.11.10/3.12.5 and later; on an older patch release the guard "
        f"lets it through (running {sys.version.split()[0]})",
    )
    def test_ipv4_mapped_loopback_is_refused(self):
        self.addresses = ["::ffff:127.0.0.1"]
        with self.assertRaises(ToolError):
            self.fetch()

    def test_unresolvable_host_is_reported_not_dialed(self):
        self.mock_getaddrinfo.side_effect = socket.gaierror("no such host")
        with self.assertRaises(ToolError) as caught:
            self.fetch()
        self.assertIn("Could not resolve host", str(caught.exception))
        self.mock_create_connection.assert_not_called()

    def test_an_empty_dns_answer_is_refused(self):
        self.mock_getaddrinfo.side_effect = None
        self.mock_getaddrinfo.return_value = []
        with self.assertRaises(ToolError) as caught:
            self.fetch()
        self.assertIn("Could not resolve host", str(caught.exception))
        self.mock_create_connection.assert_not_called()


class TestRequestShape(HttpsFetchTestCase):
    """What the pinned connection sends once the address is cleared."""

    def test_sni_and_host_header_keep_the_hostname(self):
        self.fetch("https://images.example.com/logo.jpg")
        self.assertEqual(
            self.context.wrap_socket.call_args.kwargs["server_hostname"],
            "images.example.com",
        )
        self.assertIn("Host: images.example.com\r\n", self.socket.request_text)

    def test_url_credentials_do_not_leak_into_the_host_header(self):
        # The Host header used to be built from netloc, which carries any
        # user:password@ userinfo straight into the request.
        self.fetch("https://user:s3cret@images.example.com/logo.jpg")
        self.assertNotIn("s3cret", self.socket.request_text)
        self.assertIn("Host: images.example.com\r\n", self.socket.request_text)

    def test_the_query_string_is_kept_in_the_request_line(self):
        self.fetch("https://images.example.com/img?id=7&v=2")
        self.assertIn(
            "GET /img?id=7&v=2 HTTP/1.1\r\n", self.socket.request_text
        )

    def test_a_pathless_url_asks_for_the_root(self):
        self.fetch("https://images.example.com")
        self.assertIn("GET / HTTP/1.1\r\n", self.socket.request_text)

    def test_a_non_default_port_is_dialed_and_named_in_the_host_header(self):
        self.fetch("https://images.example.com:8443/logo.jpg")
        self.assertEqual(self.dialed_address(), (PUBLIC_V4, 8443))
        self.assertIn(
            "Host: images.example.com:8443\r\n", self.socket.request_text
        )

    def test_the_timeout_reaches_the_socket(self):
        self.fetch(timeout=7)
        self.assertEqual(self.mock_create_connection.call_args.args[1], 7)

    def test_the_default_timeout_is_used_when_none_is_passed(self):
        self.fetch()
        self.assertEqual(
            self.mock_create_connection.call_args.args[1],
            safe_fetch._DEFAULT_TIMEOUT,
        )


class TestResponseHandling(HttpsFetchTestCase):
    """Status codes, redirects and the size cap."""

    def test_a_redirect_is_refused_and_names_its_target(self):
        for status in (301, 302, 303, 307, 308):
            with self.subTest(status=status):
                self.socket = FakeSocket(
                    http_response(
                        status,
                        "Found",
                        headers=(("Location", "http://169.254.169.254/"),),
                    )
                )
                self.mock_create_connection.return_value = self.socket
                with self.assertRaises(ToolError) as caught:
                    self.fetch()
                message = str(caught.exception)
                self.assertIn("Redirects are not", message)
                self.assertIn("169.254.169.254", message)

    def test_a_non_200_status_is_reported(self):
        self.socket = FakeSocket(http_response(404, "Not Found"))
        self.mock_create_connection.return_value = self.socket
        with self.assertRaises(ToolError) as caught:
            self.fetch()
        self.assertIn("HTTP 404 Not Found", str(caught.exception))

    def test_an_oversized_body_is_refused_without_being_buffered(self):
        body = JPEG + b"\x00" * 5000
        self.socket = FakeSocket(http_response(body=body))
        self.mock_create_connection.return_value = self.socket
        with self.assertRaises(ToolError) as caught:
            self.fetch(max_bytes=100)
        self.assertIn("100 byte limit", str(caught.exception))
        # Only the cap plus the probe byte was read off the wire.
        self.assertGreater(self.socket.unread, 0)

    def test_a_body_exactly_at_the_cap_is_accepted(self):
        self.socket = FakeSocket(http_response(body=JPEG))
        self.mock_create_connection.return_value = self.socket
        self.assertEqual(self.fetch(max_bytes=len(JPEG)), JPEG)

    def test_an_empty_body_is_refused(self):
        self.socket = FakeSocket(http_response(body=b""))
        self.mock_create_connection.return_value = self.socket
        with self.assertRaises(ToolError) as caught:
            self.fetch()
        self.assertIn("no data", str(caught.exception))

    def test_a_connection_failure_is_reported_as_a_tool_error(self):
        self.mock_create_connection.side_effect = ConnectionRefusedError(
            "refused"
        )
        with self.assertRaises(ToolError) as caught:
            self.fetch()
        self.assertIn("Could not download image", str(caught.exception))

    def test_a_tls_failure_is_reported_as_a_tool_error(self):
        self.context.wrap_socket.side_effect = ssl.SSLError("handshake")
        with self.assertRaises(ToolError) as caught:
            self.fetch()
        self.assertIn("Could not download image", str(caught.exception))

    def test_a_guard_error_is_not_reworded_as_a_download_failure(self):
        # The redirect refusal is raised inside the same try block that
        # wraps transport errors; it must travel out untouched.
        self.socket = FakeSocket(
            http_response(302, "Found", headers=(("Location", "/other"),))
        )
        self.mock_create_connection.return_value = self.socket
        with self.assertRaises(ToolError) as caught:
            self.fetch()
        self.assertNotIn("Could not download image", str(caught.exception))

    def test_the_socket_is_closed_even_when_the_response_is_refused(self):
        self.socket = FakeSocket(http_response(500, "Server Error"))
        self.mock_create_connection.return_value = self.socket
        with self.assertRaises(ToolError):
            self.fetch()
        self.assertTrue(self.socket.closed)


class TestMagicBytes(HttpsFetchTestCase):
    """The payload has to look like an image, whatever the URL says."""

    def respond_with(self, body):
        self.socket = FakeSocket(http_response(body=body))
        self.mock_create_connection.return_value = self.socket

    def test_a_png_is_accepted(self):
        self.respond_with(PNG)
        self.assertEqual(self.fetch(), PNG)

    def test_a_png_served_from_a_jpg_url_is_accepted(self):
        # The extension is not the check; the magic bytes are.
        self.respond_with(PNG)
        self.assertEqual(self.fetch("https://images.example.com/a.jpg"), PNG)

    def test_html_dressed_up_as_a_jpg_is_refused(self):
        self.respond_with(HTML)
        with self.assertRaises(ToolError) as caught:
            self.fetch("https://images.example.com/logo.jpg")
        self.assertIn("magic bytes", str(caught.exception))

    def test_a_gif_is_refused(self):
        self.respond_with(GIF)
        with self.assertRaises(ToolError) as caught:
            self.fetch()
        self.assertIn("JPEG or PNG", str(caught.exception))

    def test_a_truncated_png_signature_is_refused(self):
        self.respond_with(PNG[:4] + b"\x00" * 16)
        with self.assertRaises(ToolError):
            self.fetch()


class TestSchemeGuards(unittest.TestCase):
    """Which sources are even allowed to reach the fetcher."""

    def read(self, source, max_bytes=1000):
        return safe_fetch.read_image_source(source, max_bytes)

    def test_an_empty_source_is_refused(self):
        for source in ("", "   ", None):
            with self.subTest(source=source):
                with self.assertRaises(ToolError) as caught:
                    self.read(source)
                self.assertIn("must not be empty", str(caught.exception))

    def test_plain_http_is_refused(self):
        with self.assertRaises(ToolError) as caught:
            self.read("http://images.example.com/logo.jpg")
        self.assertIn("not allowed", str(caught.exception))

    def test_other_schemes_are_refused_by_name(self):
        for source in (
            "file:///etc/passwd",
            "ftp://images.example.com/logo.jpg",
            "gopher://127.0.0.1:11211/_stats",
            "data:image/png;base64,AAAA",
            "dict://127.0.0.1:11211/",
        ):
            with self.subTest(source=source):
                with self.assertRaises(ToolError) as caught:
                    self.read(source)
                message = str(caught.exception)
                self.assertIn("Unsupported image source scheme", message)
                self.assertIn(source.split(":", 1)[0], message)

    def test_an_uppercase_https_scheme_still_goes_through_the_fetcher(self):
        with patch.object(safe_fetch, "_fetch_https") as fetch:
            fetch.return_value = JPEG
            self.assertEqual(
                self.read("HTTPS://images.example.com/logo.jpg"), JPEG
            )
        self.assertEqual(
            fetch.call_args.args[0], "HTTPS://images.example.com/logo.jpg"
        )

    def test_surrounding_whitespace_does_not_hide_a_bad_scheme(self):
        with self.assertRaises(ToolError) as caught:
            self.read("  file:///etc/passwd  ")
        self.assertIn("Unsupported image source scheme", str(caught.exception))

    def test_a_url_without_a_host_is_refused(self):
        with self.assertRaises(ToolError) as caught:
            self.read("https:///logo.jpg")
        self.assertIn("no host", str(caught.exception))

    def test_the_fetcher_rejects_a_non_https_url_on_its_own(self):
        # Unreachable through read_image_source, which dispatches on the
        # prefix first; kept as the fetcher's own defence in depth.
        with self.assertRaises(ToolError) as caught:
            safe_fetch._fetch_https("http://images.example.com/a.jpg", 10, 5)
        self.assertIn("Only https://", str(caught.exception))


class TestLocalFiles(unittest.TestCase):
    """Local paths: off unless the operator turns them on."""

    def setUp(self):
        # patch.dict restores the whole environment on cleanup, including
        # the key popped below.
        env = patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(safe_fetch.ALLOW_LOCAL_FILES_ENV, None)

        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.image = os.path.join(self.directory.name, "logo.jpg")
        with open(self.image, "wb") as f:
            f.write(JPEG)

    def allow_local_files(self, value="1"):
        os.environ[safe_fetch.ALLOW_LOCAL_FILES_ENV] = value

    def test_a_local_path_is_refused_while_the_switch_is_off(self):
        with self.assertRaises(ToolError) as caught:
            safe_fetch.read_image_source(self.image, 1000)
        message = str(caught.exception)
        self.assertIn("Reading local files is disabled", message)
        self.assertIn(safe_fetch.ALLOW_LOCAL_FILES_ENV, message)

    def test_a_local_path_is_read_once_the_switch_is_on(self):
        self.allow_local_files()
        self.assertEqual(safe_fetch.read_image_source(self.image, 1000), JPEG)

    def test_a_missing_file_is_reported(self):
        self.allow_local_files()
        with self.assertRaises(ToolError) as caught:
            safe_fetch.read_image_source(
                os.path.join(self.directory.name, "nope.jpg"), 1000
            )
        self.assertIn("File not found", str(caught.exception))

    def test_a_directory_is_not_readable_as_an_image(self):
        self.allow_local_files()
        with self.assertRaises(ToolError) as caught:
            safe_fetch.read_image_source(self.directory.name, 1000)
        self.assertIn("File not found", str(caught.exception))

    def test_an_oversized_local_file_is_refused(self):
        self.allow_local_files()
        with self.assertRaises(ToolError) as caught:
            safe_fetch.read_image_source(self.image, len(JPEG) - 1)
        self.assertIn("byte limit", str(caught.exception))

    def test_a_local_non_image_is_refused(self):
        self.allow_local_files()
        text = os.path.join(self.directory.name, "passwd.jpg")
        with open(text, "wb") as f:
            f.write(HTML)
        with self.assertRaises(ToolError) as caught:
            safe_fetch.read_image_source(text, 1000)
        self.assertIn("magic bytes", str(caught.exception))

    def test_an_empty_local_file_is_refused(self):
        self.allow_local_files()
        empty = os.path.join(self.directory.name, "empty.jpg")
        open(empty, "wb").close()
        with self.assertRaises(ToolError) as caught:
            safe_fetch.read_image_source(empty, 1000)
        self.assertIn("no data", str(caught.exception))

    def test_only_explicit_truthy_values_open_the_switch(self):
        for value in ("1", "true", "TRUE", " yes ", "on"):
            with self.subTest(value=value):
                self.allow_local_files(value)
                self.assertTrue(safe_fetch.local_files_allowed())
        for value in ("", "0", "false", "no", "off", "maybe"):
            with self.subTest(value=value):
                self.allow_local_files(value)
                self.assertFalse(safe_fetch.local_files_allowed())

    def test_the_switch_is_off_when_the_variable_is_unset(self):
        self.assertFalse(safe_fetch.local_files_allowed())


if __name__ == "__main__":
    unittest.main()
