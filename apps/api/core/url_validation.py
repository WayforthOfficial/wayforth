"""url_validation.py — SSRF defense for user-supplied callback URLs (v0.9.0 fix: domain-name host guard).

Webhooks register URLs that the server will later POST to. Without validation,
a caller can register http(s)://169.254.169.254/... (cloud metadata) or
an internal-only host (e.g. a private database) and turn webhook delivery into a
blind-SSRF probe against our own infrastructure.

`validate_external_url` rejects internal targets at *registration* time so
they never reach _dispatch_webhooks. It must be called from any endpoint
that accepts a webhook/callback URL from an untrusted caller.
"""

import ipaddress
import socket
from urllib.parse import urlparse

from fastapi import HTTPException

# Hostnames we never resolve — short-circuit before DNS so a configured
# `localhost` resolver can't sneak past with a public-looking A record.
_FORBIDDEN_HOSTS = frozenset({
    "localhost",
    "ip6-localhost",
    "ip6-loopback",
    "metadata.google.internal",
})

# Suffix match — Railway internal services, K8s cluster DNS, mDNS, etc.
_FORBIDDEN_SUFFIXES = (
    ".internal",
    ".local",
    ".localhost",
    ".cluster.local",
    ".svc.cluster.local",
)


def _is_private_ip(ip_str: str) -> bool:
    """True if the parsed IP is private/loopback/link-local/multicast/unspecified.

    FINDING-008: also unwraps IPv4-mapped / IPv4-compatible IPv6 (e.g.
    ::ffff:169.254.169.254) and checks the embedded v4 address, which some
    stacks would otherwise route to the v4 target while `.is_private` on the v6
    form returns False.
    """
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True  # unparseable → treat as unsafe (fail closed)

    def _flagged(a) -> bool:
        return (
            a.is_private or a.is_loopback or a.is_link_local
            or a.is_multicast or a.is_unspecified or a.is_reserved
        )

    if _flagged(ip):
        return True
    # Unwrap embedded IPv4 (mapped or compat) and re-check.
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None and _flagged(mapped):
        return True
    compat = getattr(ip, "sixtofour", None)
    if compat is not None and _flagged(compat):
        return True
    return False


def validate_external_url(url: str, field_name: str = "url") -> list[str]:
    """Validate `url` (https:// only) and return the list of resolved public IPs.

    Raises HTTPException(422) if `url` is not a safe external https:// target.
    Blocks: non-https schemes, localhost/internal hostnames, hostnames that
    resolve to private/loopback/link-local IPs (incl. IPv4-mapped IPv6), and
    cloud-metadata endpoints.

    Returns the validated public IP set. NOTE: returning the IPs does not by
    itself prevent DNS rebinding — httpx re-resolves the hostname at connect
    time, so a 0-TTL record can still rebind to an internal IP between this
    check and the connection. Callers that perform a server-side request MUST
    send it through `post_pinned()` below, which connects to one of these
    validated IPs directly (no second DNS lookup) and is what actually closes
    the rebind TOCTOU (FINDING-008 / FINDING-102).
    """
    return _validate_external_url(url, field_name, allowed_schemes=("https",))


def validate_external_url_relaxed(url: str, field_name: str = "url") -> list[str]:
    """Like `validate_external_url` but also permits http://.

    DECISION 1 (v0.8.10): content-fetching adapters (Jina reader, Firecrawl
    scrape) legitimately receive http:// page URLs. This validator relaxes ONLY
    the scheme restriction — every private/loopback/link-local/internal IP and
    hostname block is identical, so the SSRF defense is unchanged. Do NOT use
    this for audio/webhook targets, which should stay https-only.
    """
    return _validate_external_url(url, field_name, allowed_schemes=("http", "https"))


def _validate_external_url(url: str, field_name: str, allowed_schemes: tuple[str, ...]) -> list[str]:
    """Shared implementation for the strict and relaxed validators. Only the set
    of permitted URL schemes differs; the IP/hostname screening is identical."""
    if not url:
        raise HTTPException(status_code=422, detail={
            "error": f"invalid_{field_name}",
            "message": f"{field_name} is required",
        })

    parsed = urlparse(url)
    if parsed.scheme not in allowed_schemes:
        _scheme_hint = " or ".join(f"{s}://" for s in allowed_schemes)
        raise HTTPException(status_code=422, detail={
            "error": f"invalid_{field_name}",
            "message": f"{field_name} must use {_scheme_hint}",
        })

    host = (parsed.hostname or "").lower().strip()
    if not host:
        raise HTTPException(status_code=422, detail={
            "error": f"invalid_{field_name}",
            "message": f"{field_name} is missing a hostname",
        })

    if host in _FORBIDDEN_HOSTS or any(host.endswith(s) for s in _FORBIDDEN_SUFFIXES):
        raise HTTPException(status_code=422, detail={
            "error": "internal_target_forbidden",
            "message": "Internal hostnames are not allowed as webhook targets.",
        })

    # Literal IP in URL → check directly, skip DNS.
    # _is_private_ip fails-closed on non-IPs, so only call it when host is a
    # valid IP literal. Domain names fall through to the DNS resolution below.
    try:
        ipaddress.ip_address(host)
        _host_is_ip = True
    except ValueError:
        _host_is_ip = False
    if _host_is_ip and _is_private_ip(host):
        raise HTTPException(status_code=422, detail={
            "error": "internal_target_forbidden",
            "message": "Private, loopback, or link-local addresses are not allowed.",
        })

    # Resolve to catch DNS-rebinding-style or attacker-controlled A records
    # that point at internal IPs (e.g. localtest.me, *.nip.io, evil DNS).
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        raise HTTPException(status_code=422, detail={
            "error": f"invalid_{field_name}",
            "message": f"{field_name} hostname does not resolve",
        })

    resolved_ips: list[str] = []
    for info in infos:
        sockaddr = info[4]
        resolved_ip = sockaddr[0] if sockaddr else ""
        if _is_private_ip(resolved_ip):
            raise HTTPException(status_code=422, detail={
                "error": "internal_target_forbidden",
                "message": "Hostname resolves to a private or loopback address.",
            })
        if resolved_ip:
            resolved_ips.append(resolved_ip)
    return resolved_ips


async def request_pinned(
    client,
    method: str,
    url: str,
    *,
    params=None,
    content=None,
    json=None,
    headers: dict | None = None,
    field_name: str = "url",
    allow_http: bool = False,
):
    """Validate `url`, then issue `method` with the TCP connection pinned to a
    validated IP — actually closing the DNS-rebind TOCTOU (FINDING-008/102, EXEC-1).

    `validate_external_url` resolves and screens every A/AAAA record. We then
    rewrite the URL host to that exact IP *literal* and connect to it. Because the
    host is now an IP literal, httpx/httpcore performs no second DNS lookup at
    connect time, so a hostname that rebinds to 127.0.0.1 / 169.254.169.254 after
    validation can never be reached. The original hostname is preserved for the
    TLS SNI, certificate verification, and the Host header.

    This is the ONLY safe way to make a server-side request to a user-supplied
    URL. `validate_external_url` alone does NOT stop rebinding (httpx re-resolves
    at connect), and `follow_redirects=False` only blocks redirect-based SSRF.

    `allow_http=True` permits http:// targets (relaxed validator) for legitimate
    content-fetch URLs; default is https-only. Raises HTTPException(422) if the
    URL fails validation (e.g. it has since rebound to an internal address).
    """
    validator = validate_external_url_relaxed if allow_http else validate_external_url
    ips = validator(url, field_name=field_name)
    parsed = urlparse(url)
    host = parsed.hostname or ""

    body_kwargs: dict = {}
    if params is not None:
        body_kwargs["params"] = params
    if content is not None:
        body_kwargs["content"] = content
    if json is not None:
        body_kwargs["json"] = json

    if not ips or not host:
        # Defensive fallback: the validator returns >=1 IP on success.
        return await client.request(method, url, headers=headers, **body_kwargs)

    ip = ips[0]
    ip_host = f"[{ip}]" if ":" in ip else ip  # bracket IPv6 literals
    netloc = f"{ip_host}:{parsed.port}" if parsed.port else ip_host
    pinned_url = parsed._replace(netloc=netloc).geturl()

    merged = dict(headers or {})
    # Preserve original Host (incl. non-default port) so the server routes by
    # hostname, not by the pinned IP.
    merged["Host"] = f"{host}:{parsed.port}" if parsed.port else host

    request = client.build_request(method, pinned_url, headers=merged, **body_kwargs)
    # TLS SNI + cert hostname verification use the real hostname, not the IP.
    request.extensions["sni_hostname"] = host
    return await client.send(request)


async def post_pinned(
    client,
    url: str,
    *,
    content=None,
    headers: dict | None = None,
    field_name: str = "url",
):
    """POST `url` with the connection pinned to a validated IP. Thin wrapper over
    request_pinned (kept for existing call sites)."""
    return await request_pinned(
        client, "POST", url, content=content, headers=headers, field_name=field_name
    )
