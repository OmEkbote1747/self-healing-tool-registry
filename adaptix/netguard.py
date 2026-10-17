"""Egress guard for REAL API traffic (SSRF protection).

Every request to a real provider - schema discovery, tool calls, monitoring probes - goes through
`GuardedHTTP`. It enforces, in backend code:

* scheme: https only (plain http only when ADAPTIX_ALLOW_HTTP is explicitly enabled); no credentials in URLs
* host: must match the operator's allowlist (ADAPTIX_ALLOWED_HOSTS: exact names or "*.example.com")
* address: every resolved IP must be public; loopback, private, link-local, CGNAT, multicast, reserved
  and cloud-metadata addresses are rejected, as are well-known metadata host names
* DNS pinning: the connection goes to the validated IP (Host header and TLS SNI keep the real name, so the
  certificate is still verified against the host name) - a second DNS answer cannot redirect the request
* redirects: never followed automatically; each hop is re-validated (GET/HEAD only, bounded count)
* TLS: certificates are always verified; proxies from the environment are ignored
* politeness: at most one request per host per interval, and a host that answered 429 is left alone until
  its Retry-After has passed (no request is sent during the cooldown)
"""
from __future__ import annotations

import ipaddress
import socket
import threading
import time
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

METADATA_HOSTS = {"metadata", "metadata.google.internal", "metadata.goog", "instance-data",
                  "instance-data.ec2.internal", "metadata.azure.com", "169.254.169.254", "fd00:ec2::254"}
METADATA_IPS = {ipaddress.ip_address(a) for a in ("169.254.169.254", "169.254.170.2", "100.100.100.200",
                                                  "fd00:ec2::254", "192.0.0.192")}


class EgressDenied(RuntimeError):
    """The request was refused before anything was sent."""
    def __init__(self, category: str, message: str):
        super().__init__(message)
        self.category = category      # host_not_allowed | private_address | scheme_not_allowed | dns_failure |
        #                               invalid_url | redirect_denied | rate_limited_local | simulated_in_production


def host_allowed(host: str, allowed: tuple[str, ...]) -> bool:
    host = host.lower().rstrip(".")
    for a in allowed:
        if a.startswith("*."):
            if host.endswith(a[1:]) and host != a[2:]:
                return True
        elif host == a:
            return True
    return False


def _public(ip: ipaddress._BaseAddress) -> bool:
    if ip in METADATA_IPS:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        return _public(ip.ipv4_mapped)
    return ip.is_global and not (ip.is_multicast or ip.is_reserved or ip.is_loopback or ip.is_link_local
                                 or ip.is_private or ip.is_unspecified)


def resolve_public(host: str, port: int) -> list[str]:
    """Resolve `host` and require every address to be public. Returns the validated addresses."""
    try:
        literal = ipaddress.ip_address(host.strip("[]"))
        addrs = [literal]
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except socket.gaierror as e:
            raise EgressDenied("dns_failure", f"could not resolve {host}: {e.strerror or e}") from None
        addrs = [ipaddress.ip_address(i[4][0].split("%")[0]) for i in infos]
    if not addrs:
        raise EgressDenied("dns_failure", f"{host} has no addresses")
    bad = [str(a) for a in addrs if not _public(a)]
    if bad:
        raise EgressDenied("private_address", f"{host} resolves to a non-public address ({', '.join(bad[:3])})")
    return [str(a) for a in addrs]


@dataclass
class Target:
    url: str
    host: str
    port: int
    ip: str


def check_url(url: str, allowed: tuple[str, ...], allow_http: bool = False,
              exempt: frozenset = frozenset()) -> Target:
    """Validate a URL for real egress. Raises EgressDenied; sends nothing.

    `exempt` holds exact (host, port) pairs of the controlled test service that ADAPTIX itself runs on
    loopback (REAL_API_TEST environment). Nothing else can bypass the allowlist or the address checks."""
    if url.startswith("sim://"):
        raise EgressDenied("simulated_in_production", "sim:// endpoints are simulations and are never real APIs")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise EgressDenied("invalid_url", "the URL could not be parsed") from None
    exempt_hit = parts.scheme in ("http", "https") and ((parts.hostname or "").lower(),
                                                         port or (443 if parts.scheme == "https" else 80)) in exempt
    if parts.scheme not in ("https", "http") or (parts.scheme == "http" and not allow_http and not exempt_hit):
        raise EgressDenied("scheme_not_allowed", f"scheme '{parts.scheme}' is not allowed (https required)")
    if parts.username or parts.password:
        raise EgressDenied("invalid_url", "credentials embedded in URLs are not allowed")
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        raise EgressDenied("invalid_url", "the URL has no host")
    if exempt_hit:
        return Target(url, host, port or 80, host)
    if host in METADATA_HOSTS:
        raise EgressDenied("private_address", f"{host} is a cloud metadata endpoint")
    if not host_allowed(host, allowed):
        raise EgressDenied("host_not_allowed", f"{host} is not in the approved host list (ADAPTIX_ALLOWED_HOSTS)")
    port = port or (443 if parts.scheme == "https" else 80)
    ip = resolve_public(host, port)[0]
    return Target(url, host, port, ip)


class GuardedHTTP:
    def __init__(self, allowed: tuple[str, ...], allow_http: bool = False, timeout_s: float = 10.0,
                 max_redirects: int = 3, min_interval_s: float = 1.0, sleep=time.sleep,
                 exempt: frozenset = frozenset()):
        self.allowed, self.allow_http, self.exempt = allowed, allow_http, frozenset(exempt)
        self.timeout_s, self.max_redirects, self.min_interval_s = timeout_s, max_redirects, min_interval_s
        self._sleep = sleep
        self._client = httpx.Client(timeout=httpx.Timeout(timeout_s), follow_redirects=False, verify=True,
                                    trust_env=False, limits=httpx.Limits(max_connections=20, max_keepalive_connections=5))
        self._lock = threading.Lock()
        self._next: dict[str, float] = {}         # host -> earliest next request (monotonic)
        self._cooldown: dict[str, float] = {}     # host -> 429 cooldown end (monotonic)

    MAX_COOLDOWN_WAIT_S = 5.0                  # wait out short Retry-After periods; refuse longer ones

    def _pace(self, host: str):
        with self._lock:
            now = time.monotonic()
            remaining = self._cooldown.get(host, 0) - now
            if remaining > self.MAX_COOLDOWN_WAIT_S:
                raise EgressDenied("rate_limited_local", f"{host} asked us to wait (Retry-After); "
                                                         f"{remaining:.0f}s remaining")
            wait = max(0.0, self._next.get(host, 0) - now, remaining)
            self._next[host] = now + wait + self.min_interval_s
        if wait:
            self._sleep(wait)

    def cooldown(self, host: str) -> float:
        return max(0.0, self._cooldown.get(host, 0) - time.monotonic())

    def _send(self, method: str, t: Target, **kw) -> httpx.Response:
        parts = urlsplit(t.url)
        ip_host = f"[{t.ip}]" if ":" in t.ip else t.ip
        netloc = ip_host + (f":{parts.port}" if parts.port else "")
        pinned = urlunsplit((parts.scheme, netloc, parts.path or "/", parts.query, ""))
        headers = dict(kw.pop("headers", None) or {})
        headers["Host"] = parts.netloc.split("@")[-1]
        ext = {"sni_hostname": t.host} if parts.scheme == "https" else {}
        timeout = kw.pop("timeout", None) or self.timeout_s
        max_bytes = kw.pop("max_bytes", None)
        if max_bytes is None:
            return self._client.request(method, pinned, headers=headers, extensions=ext, timeout=timeout, **kw)
        # size-capped read: stop downloading as soon as the cap is exceeded
        req = self._client.build_request(method, pinned, headers=headers, extensions=ext, timeout=timeout, **kw)
        r = self._client.send(req, stream=True)
        try:
            body = b""
            for chunk in r.iter_bytes():
                body += chunk
                if len(body) > max_bytes:
                    raise EgressDenied("response_too_large", f"response larger than {max_bytes} bytes")
        finally:
            r.close()
        return httpx.Response(r.status_code, headers=r.headers, content=body, request=req)

    def request(self, method: str, url: str, **kw) -> httpx.Response:
        """One guarded request. Redirects are re-validated hop by hop (GET/HEAD only)."""
        method = method.upper()
        for hop in range(self.max_redirects + 1):
            t = check_url(url, self.allowed, self.allow_http, self.exempt)
            key = t.host
            if (t.host, t.port) in self.exempt:      # the controlled test service hosts independent apps
                key = t.host + "/" + "/".join(urlsplit(url).path.split("/")[1:3])
            self._pace(key)
            r = self._send(method, t, **dict(kw))
            if r.status_code == 429:
                ra = r.headers.get("retry-after")
                try:
                    secs = float(ra) if ra is not None else 30.0
                except ValueError:
                    secs = 30.0
                with self._lock:                    # exactly what the provider asked for (bounded)
                    self._cooldown[key] = time.monotonic() + min(max(secs, 0.0), 3600.0)
            if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                if method not in ("GET", "HEAD"):
                    return r                       # never re-send a write to another location
                if hop == self.max_redirects:
                    raise EgressDenied("redirect_denied", f"more than {self.max_redirects} redirects")
                url = urljoin(url, r.headers["location"])
                kw.pop("params", None)             # the Location already carries its query
                continue
            return r
        raise EgressDenied("redirect_denied", "redirect loop")

    JSON_TYPES = ("application/json", "application/openapi+json", "application/vnd.oai.openapi+json", "text/plain")

    def get_json(self, url: str, max_bytes: int) -> tuple[int, object]:
        """GET a JSON document with a streamed size cap and a content-type check.
        Returns (status, parsed JSON) - JSON is None when the body is not a JSON document."""
        r = self.request("GET", url, headers={"Accept": "application/json"}, max_bytes=max_bytes)
        ctype = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
        if ctype and not (ctype in self.JSON_TYPES or ctype.endswith("+json")):
            return r.status_code, None                       # e.g. an HTML login or error page
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, None
