"""Webhook targets must not point into our own network, as written or as
the name resolves (SSRF)."""

import pytest

from invoice_processing.trust.webhooks import target


@pytest.mark.parametrize(
    "host",
    [
        "localhost",
        "api.localhost",
        "127.0.0.1",
        "10.0.0.5",
        "::1",
        "169.254.169.254",
        "192.168.1.1",
        "fe80::1",
    ],
)
def test_internal_literals_are_internal(host):
    assert target.internal(host, resolve=lambda h: pytest.fail("resolved"))


@pytest.mark.parametrize(
    "addrs",
    [
        ["10.0.0.5"],
        ["8.8.8.8", "127.0.0.1"],
        ["fe80::1%eth0"],
        ["::ffff:10.0.0.1"],
    ],
)
def test_a_name_resolving_inward_is_internal(addrs):
    assert target.internal("erp.example", resolve=lambda h: addrs)


def test_a_name_resolving_only_outward_is_public():
    assert not target.internal("erp.example", resolve=lambda h: ["8.8.8.8"])


def test_an_unresolvable_name_is_left_to_fail_on_its_own():
    def gone(host):
        raise OSError("no such host")

    assert not target.internal("gone.example", resolve=gone)


def test_the_default_resolver_reads_getaddrinfo(monkeypatch):
    monkeypatch.setattr(
        target.socket,
        "getaddrinfo",
        lambda host, port, **kw: [(2, 1, 6, "", ("10.1.2.3", 0))],
    )
    assert target.addresses("erp.example") == ["10.1.2.3"]
    assert target.internal("erp.example")


@pytest.mark.parametrize(
    "url",
    [
        "https://10.0.0.1\\@example.com/",
        "https://127.0.0.1\\example.com/",
        "https://user:pw@example.com/",
        "https://[::1",
    ],
)
def test_a_url_requests_would_read_differently_is_refused(url):
    """The host is read the way the sender reads it (urllib3), and anything
    that parses ambiguously or carries credentials is refused outright."""
    assert target.refused(url, resolve=lambda h: ["8.8.8.8"])


def test_a_plain_public_url_is_not_refused():
    assert not target.refused(
        "https://ERP.example:8443/in?x=1", resolve=lambda h: ["8.8.8.8"]
    )
    assert target.host("https://ERP.example:8443/in") == "erp.example"
