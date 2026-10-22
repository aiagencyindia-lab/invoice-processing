"""Is a webhook host inside our own network? Checked when an endpoint is
saved and again before every delivery, since a public name can be pointed
inward later.

ponytail: the check resolves, then `requests` resolves again to connect, so
a name rebinding inside that gap still gets through. Pin the checked address
in a transport adapter if an attacker-run DNS server is in scope.
"""

from __future__ import annotations

import ipaddress
import socket

from urllib3.util import parse_url


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
