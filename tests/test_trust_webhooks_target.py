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


# -- the pinned send: resolve once, check, connect to what was checked --------


class FakePool:
    def __init__(self, scheme, address, port, name, timeout):
        self.opened = (scheme, address, port, name, timeout)
        self.sent = None

    def urlopen(self, method, path, body=None, headers=None, **kw):
        self.sent = (method, path, body, headers, kw)
        return type("R", (), {"status": 204})()


@pytest.fixture
def pools(monkeypatch):
    made = []

    def pool(*args):
        made.append(FakePool(*args))
        return made[-1]

    monkeypatch.setattr(target, "_pool", pool)
    return made


def test_it_connects_to_the_address_it_checked(pools):
    looked_up = []

    def resolve(host):
        looked_up.append(host)
        return ["93.184.216.34"]

    r = target.post(
        "https://hooks.example.com/in?x=1",
        data="{}",
        headers={"X-Trust-Event": "e"},
        timeout=10,
        resolve=resolve,
    )
    assert r.status_code == 204 and looked_up == ["hooks.example.com"]
    (pool,) = pools
    # The checked address, the real name for SNI and the certificate.
    assert pool.opened == (
        "https",
        "93.184.216.34",
        443,
        "hooks.example.com",
        10,
    )
    method, path, body, headers, kw = pool.sent
    assert (method, path, body) == ("POST", "/in?x=1", "{}")
    assert headers["Host"] == "hooks.example.com"
    assert headers["X-Trust-Event"] == "e"
    assert kw["redirect"] is False and kw["retries"] is False


def test_a_rebound_name_is_refused_without_connecting(pools):
    """The first lookup was public when the endpoint was saved; this one
    answers inward. Nothing is opened."""
    with pytest.raises(target.Refused):
        target.post(
            "https://hooks.example.com/in",
            data="{}",
            headers={},
            timeout=10,
            resolve=lambda h: ["93.184.216.34", "10.0.0.5"],
        )
    assert pools == []


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost/x",
        "http://169.254.169.254/latest",
        "ftp://hooks.example.com/x",
        "https://user:pw@hooks.example.com/x",
    ],
)
def test_internal_or_odd_urls_are_refused_without_connecting(pools, url):
    with pytest.raises(target.Refused):
        target.post(url, data="{}", headers={}, timeout=10,
                    resolve=lambda h: ["93.184.216.34"])  # fmt: skip
    assert pools == []


def test_a_port_and_plain_http_carry_through(pools):
    target.post(
        "http://hooks.example.com:8080/in",
        data="{}",
        headers={},
        timeout=5,
        resolve=lambda h: ["93.184.216.34"],
    )
    (pool,) = pools
    assert pool.opened == (
        "http",
        "93.184.216.34",
        8080,
        "hooks.example.com",
        5,
    )
    assert pool.sent[3]["Host"] == "hooks.example.com:8080"


def test_the_https_pool_checks_the_certificate_against_the_name():
    pool = target._pool("https", "93.184.216.34", 443, "hooks.example.com", 10)
    assert pool.host == "93.184.216.34"
    assert pool.conn_kw["server_hostname"] == "hooks.example.com"
    assert pool.assert_hostname == "hooks.example.com"
    assert pool.cert_reqs == "CERT_REQUIRED"
