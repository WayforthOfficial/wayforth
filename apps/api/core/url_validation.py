"""url_validation.py — SSRF defense for user-supplied callback URLs.

Webhooks register URLs that the server will later POST to. Without validation,
a caller can register http(s)://169.254.169.254/... (cloud metadata) or
http(s)://postgres.railway.internal/... and turn webhook delivery into a
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
    """Validate `url` and return the list of resolved public IPs.

    Raises HTTPException(422) if `url` is not a safe external https:// target.
    Blocks: non-https schemes, localhost/internal hostnames, hostnames that
    resolve to private/loopback/link-local IPs (incl. IPv4-mapped IPv6), and
    cloud-metadata endpoints.

    FINDING-008: returns the validated IP set so the caller can pin delivery to
    the same address it validated, rather than letting httpx re-resolve (and
    potentially rebind to an internal IP) at connect time.
    """
    if not url:
        raise HTTPException(status_code=422, detail={
            "error": f"invalid_{field_name}",
            "message": f"{field_name} is required",
        })

    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise HTTPException(status_code=422, detail={
            "error": f"invalid_{field_name}",
            "message": f"{field_name} must use https://",
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
    if _is_private_ip(host):
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


def assert_still_external(url: str, field_name: str = "url") -> None:
    """Re-validate immediately before connecting (FINDING-008).

    Webhook delivery calls this right before the httpx request so that a
    hostname which has rebound to an internal IP since registration is rejected
    at the last possible moment. This does not fully close the sub-millisecond
    gap between this resolution and httpx's own — full closure requires pinning
    the socket to the returned IP — but it rejects any rebind that has settled
    by delivery time, which is the realistic attack.
    """
    validate_external_url(url, field_name=field_name)
