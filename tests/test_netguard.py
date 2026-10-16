"""SSRF guard: allowlist, private/metadata addresses, schemes, redirects, pacing. DNS and HTTP are faked."""
import socket

import httpx
import pytest

from adaptix import netguard
from adaptix.netguard import EgressDenied, GuardedHTTP, check_url, host_allowed

ALLOWED = ("api.example.com", "*.partner.example")


@pytest.fixture
def dns(monkeypatch):
    table = {"api.example.com": "93.184.215.14", "x.partner.example": "93.184.215.15",
             "evil.example": "93.184.215.16", "internal.partner.example": "10.0.0.7",
             "rebind.partner.example": "127.0.0.1", "meta.partner.example": "169.254.169.254",
             "v6.partner.example": "::ffff:192.168.1.1"}

    def fake(host, port, *a, **k):
        if host not in table:
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        fam = socket.AF_INET6 if ":" in table[host] else socket.AF_INET
        return [(fam, socket.SOCK_STREAM, 6, "", (table[host], port))]
    monkeypatch.setattr(netguard.socket, "getaddrinfo", fake)
    return table


def denied(url, category, allowed=ALLOWED, **kw):
    with pytest.raises(EgressDenied) as e:
        check_url(url, allowed, **kw)
    assert e.value.category == category, e.value


def test_allowlist_matching():
    assert host_allowed("api.example.com", ALLOWED) and host_allowed("x.partner.example", ALLOWED)
    assert not host_allowed("partner.example", ALLOWED) and not host_allowed("api.example.com.evil", ALLOWED)
    assert not host_allowed("anything", ())


def test_url_rules(dns):
    assert check_url("https://api.example.com/v1", ALLOWED).ip == "93.184.215.14"
    denied("https://evil.example/", "host_not_allowed")
    denied("http://api.example.com/", "scheme_not_allowed")
    denied("ftp://api.example.com/", "scheme_not_allowed")
    denied("file:///etc/passwd", "scheme_not_allowed")
    denied("https://user:pw@api.example.com/", "invalid_url")
    denied("sim://anything/openapi.json", "simulated_in_production")
    denied("https://metadata.google.internal/computeMetadata/v1/", "private_address")
    denied("https://unknown.partner.example/", "dns_failure")


@pytest.mark.parametrize("host", ["internal.partner.example", "rebind.partner.example", "meta.partner.example",
                                  "v6.partner.example"])
def test_private_loopback_metadata_and_mapped_addresses_are_refused(dns, host):
    denied(f"https://{host}/", "private_address")


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.1.2.3", "172.16.0.1", "192.168.0.1", "169.254.169.254",
                                "100.64.0.1", "0.0.0.0", "::1", "fe80::1", "fd00:ec2::254"])
def test_ip_literals_are_refused_even_if_allowlisted(ip):
    host = f"[{ip}]" if ":" in ip else ip
    denied(f"https://{host}/", "private_address", allowed=(ip,))


def _guard(handler, sleeps=None):
    g = GuardedHTTP(ALLOWED, min_interval_s=0, sleep=(sleeps.append if sleeps is not None else lambda s: None))
    g._client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    return g


def test_requests_are_pinned_to_the_validated_ip_with_the_real_host_name(dns):
    seen = []

    def handler(req):
        seen.append((str(req.url), req.headers["host"], req.extensions.get("sni_hostname")))
        return httpx.Response(200, json={"ok": True})
    _guard(handler).request("GET", "https://api.example.com/v1/x?a=1")
    assert seen == [("https://93.184.215.14/v1/x?a=1", "api.example.com", "api.example.com")]


def test_redirects_are_revalidated_each_hop(dns):
    def to_evil(req):
        return httpx.Response(302, headers={"location": "https://evil.example/steal"})
    with pytest.raises(EgressDenied) as e:
        _guard(to_evil).request("GET", "https://api.example.com/")
    assert e.value.category == "host_not_allowed"

    def to_internal(req):
        return httpx.Response(301, headers={"location": "https://internal.partner.example/"})
    with pytest.raises(EgressDenied) as e:
        _guard(to_internal).request("GET", "https://api.example.com/")
    assert e.value.category == "private_address"

    hops = []

    def ok_hop(req):
        hops.append(req.headers["host"])
        if len(hops) == 1:
            return httpx.Response(302, headers={"location": "https://x.partner.example/next"})
        return httpx.Response(200, json={})
    assert _guard(ok_hop).request("GET", "https://api.example.com/").status_code == 200
    assert hops == ["api.example.com", "x.partner.example"]


def test_writes_are_never_redirected(dns):
    sent = []

    def handler(req):
        sent.append(req.method)
        return httpx.Response(307, headers={"location": "https://x.partner.example/"})
    r = _guard(handler).request("POST", "https://api.example.com/", json={"a": 1})
    assert r.status_code == 307 and sent == ["POST"]


def test_redirect_limit(dns):
    def loop(req):
        return httpx.Response(302, headers={"location": "https://api.example.com/again"})
    with pytest.raises(EgressDenied) as e:
        _guard(loop).request("GET", "https://api.example.com/")
    assert e.value.category == "redirect_denied"


def test_retry_after_cooldown_sends_nothing_until_it_expires(dns):
    sent = []

    def handler(req):
        sent.append(1)
        return httpx.Response(429, headers={"retry-after": "120"})
    g = _guard(handler)
    assert g.request("GET", "https://api.example.com/").status_code == 429
    with pytest.raises(EgressDenied) as e:
        g.request("GET", "https://api.example.com/")
    assert e.value.category == "rate_limited_local" and len(sent) == 1 and g.cooldown("api.example.com") > 100


def test_short_retry_after_is_waited_out_not_refused(dns):
    sent, sleeps = [], []

    def handler(req):
        sent.append(1)
        return httpx.Response(429, headers={"retry-after": "0.2"}) if len(sent) == 1 else httpx.Response(200, json={})
    g = _guard(handler, sleeps)
    assert g.request("GET", "https://api.example.com/").status_code == 429
    assert g.request("GET", "https://api.example.com/").status_code == 200
    assert len(sent) == 2 and sleeps and 0 < sleeps[-1] <= 0.2


def test_per_host_pacing(dns):
    sleeps = []
    g = GuardedHTTP(ALLOWED, min_interval_s=1.0, sleep=sleeps.append)
    g._client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))
    g.request("GET", "https://api.example.com/")
    g.request("GET", "https://api.example.com/")
    assert len(sleeps) == 1 and 0 < sleeps[0] <= 1.0


def test_tls_verification_is_on_and_env_proxies_ignored():
    g = GuardedHTTP(ALLOWED)
    assert g._client._transport._pool._ssl_context.verify_mode.name == "CERT_REQUIRED"
    assert g._client._trust_env is False and g._client.follow_redirects is False
