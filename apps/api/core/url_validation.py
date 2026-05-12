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
    """True if the parsed IP is private/loopback/link-local/multicast/unspecified."""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_unspecified
        or ip.is_reserved
    )


def validate_external_url(url: str, field_name: str = "url") -> None:
    """Raise HTTPException(422) if `url` is not a safe external https:// target.

    Blocks: non-https schemes, localhost/internal hostnames, hostnames that
    resolve to private/loopback/link-local IPs (incl. IPv6), and cloud-metadata
    endpoints (169.254.169.254, fd00:ec2::254).
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

    for info in infos:
        sockaddr = info[4]
        resolved_ip = sockaddr[0] if sockaddr else ""
        if _is_private_ip(resolved_ip):
            raise HTTPException(status_code=422, detail={
                "error": "internal_target_forbidden",
                "message": "Hostname resolves to a private or loopback address.",
            })
