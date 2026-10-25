"""Is a webhook host inside our own network? Checked when an endpoint is
saved and again at every delivery, since a public name can be pointed
inward later.

A delivery goes out through `post`, which resolves the name once, checks
every answer and connects to the address it checked: a name that rebinds
between a check and a second lookup has no second lookup to win.
"""

from __future__ import annotations

import ipaddress
import socket
from types import SimpleNamespace

import certifi
import urllib3
from urllib3.util import parse_url


class Refused(Exception):
    """The url is internal or ambiguous; nothing was sent."""


def addresses(host: str) -> list[str]:
    return [info[4][0] for info in socket.getaddrinfo(host, None)]


def _inward(address: str) -> bool:
    ip = ipaddress.ip_address(address.split("%", 1)[0])
    # An IPv4-mapped address counts as the IPv4 address it carries.
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return not ip.is_global


def internal(host: str, resolve=None) -> bool:
    """Loopback, private, link-local or reserved, as written or as resolved.
    A name that does not resolve is not refused: its POST fails anyway."""
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return _inward(host)
    except ValueError:
        pass
    try:
        found = (resolve or addresses)(host)
    except OSError:
        return False
    return any(_inward(a) for a in found)


def host(url: str) -> str:
    """The host as `requests` (urllib3) will connect to it; `urlsplit`
    disagrees on backslashes, so never check one parser's host and send
    with the other."""
    return (parse_url(url).host or "").strip("[]").lower()


def refused(url: str, resolve=None) -> bool:
    """A url to refuse outright: ambiguous to parse, carrying credentials,
    hostless, or internal."""
    if "\\" in url:
        return True
    try:
        parsed = parse_url(url)
    except ValueError:  # urllib3's LocationParseError is a ValueError
        return True
    if parsed.auth or not parsed.host:
        return True
    return internal(host(url), resolve)


def _pool(scheme: str, address: str, port: int, name: str, timeout: float):
    """A connection to the checked address that still speaks to `name`:
    TLS server name and certificate check against the real host."""
    if scheme == "https":
        return urllib3.HTTPSConnectionPool(
            address,
            port,
            timeout=timeout,
            retries=False,
            cert_reqs="CERT_REQUIRED",
            ca_certs=certifi.where(),
            server_hostname=name,
            assert_hostname=name,
        )
    return urllib3.HTTPConnectionPool(
        address, port, timeout=timeout, retries=False
    )


def post(url: str, *, data: str, headers: dict, timeout: float, resolve=None):
    """POST to `url` at the address this call checked. Raises `Refused`
    before connecting when the url is ambiguous or any answer is internal.
    Never follows a redirect; returns an object with `status_code`."""
    if refused(url, resolve=lambda h: []):  # parse-level checks only
        raise Refused(url)
    parsed = parse_url(url)
    if parsed.scheme not in ("http", "https"):
        raise Refused(url)
    name = host(url)
    try:
        found = [str(ipaddress.ip_address(name))]
    except ValueError:
        try:
            found = (resolve or addresses)(name)
        except OSError as exc:
            raise ConnectionError(f"{name} does not resolve") from exc
    if not found or any(_inward(a) for a in found):
        raise Refused(url)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    shown = f"[{name}]" if ":" in name else name
    response = _pool(parsed.scheme, found[0], port, name, timeout).urlopen(
        "POST",
        parsed.request_uri,
        body=data,
        headers={
            **headers,
            "Host": shown if parsed.port is None else f"{shown}:{port}",
        },
        redirect=False,
        retries=False,
    )
    return SimpleNamespace(status_code=response.status)
