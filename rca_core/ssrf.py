"""Shared SSRF / endpoint validation for rca_core.

Centralises the URL validator previously duplicated between ``gui.py`` and
``server.py`` so every entry point (legacy textbox, full provider dict,
connection-test probe, web backend) speaks the same language.

The default policy is "https only, public IPs only" — loopback, link-local,
private RFC1918, and the various cloud metadata ranges (169.254/16,
100.64/10) are rejected. Operators that need to talk to an internal
gateway can override with the env var ``RCA_ALLOW_PRIVATE=1``.

Hardening notes:
* ``getaddrinfo`` may return multiple addresses. A DNS-rebinding attacker
  can return a public IP and a private IP in the same answer, hoping the
  validator samples the public one while the outbound request picks the
  private one. We check ALL addresses and reject if ANY is non-public.
* DNS resolution failure is treated as unsafe (fail closed). Don't let
  an attacker bypass the check by passing an unresolvable name.
* The check is performed at the moment of validation only; TOCTOU between
  validation and the actual outbound request is mitigated upstream by
  the llm.py layer where feasible.
"""
from __future__ import annotations

import ipaddress
import os
import socket
from urllib.parse import urlparse


_ALLOW_PRIVATE = os.environ.get("RCA_ALLOW_PRIVATE", "").strip() == "1"


def is_private_host(host: str) -> bool:
    """Return True if *host* resolves to a non-public IP address.

    Used by ``validate_endpoint`` to block SSRF. The check resolves the
    hostname via DNS so a name like ``localhost.example.com`` that
    resolves to 127.0.0.1 is also caught.
    """
    if not host:
        return True
    bare = host.strip("[]")
    # Try a literal IP parse first (avoids DNS entirely).
    try:
        ip = ipaddress.ip_address(bare)
        return not ip.is_global
    except ValueError:
        pass
    # Not a literal — resolve via DNS. If resolution fails we treat the
    # host as private (don't let an attacker bypass the check by passing
    # an unresolvable name).
    try:
        infos = socket.getaddrinfo(bare, None)
    except socket.gaierror:
        return True
    saw_addr = False
    for info in infos:
        try:
            addr = info[4][0]
            ip = ipaddress.ip_address(addr)
        except (ValueError, IndexError):
            return True
        saw_addr = True
        if not ip.is_global:
            return True
    return not saw_addr


def validate_endpoint(endpoint: str) -> tuple[bool, str]:
    """Validate a provider endpoint URL. Returns (ok, error_message).

    Rules:
      * non-empty
      * parses as a URL with http/https scheme
      * has a hostname
      * scheme must be https (cleartext API keys would leak)
      * hostname must resolve to a public IP unless ``RCA_ALLOW_PRIVATE=1``

    Returns ``(True, "")`` on success and ``(False, reason)`` otherwise.
    The reason is a short, human-readable string suitable for surfacing
    in a UI toast.
    """
    if not endpoint:
        return False, "empty endpoint"
    try:
        u = urlparse(endpoint)
    except ValueError as exc:
        return False, f"unparseable: {exc}"
    if u.scheme not in ("http", "https"):
        return False, f"scheme must be http/https, got {u.scheme!r}"
    if not u.hostname:
        return False, "missing host"
    if u.scheme != "https":
        return False, "https required (cleartext API keys would leak)"
    if not _ALLOW_PRIVATE and is_private_host(u.hostname):
        return False, (
            f"host {u.hostname!r} resolves to a non-public address. "
            "Set RCA_ALLOW_PRIVATE=1 to override (not recommended)."
        )
    return True, ""


class SSRFError(Exception):
    """Raised when an endpoint fails SSRF validation."""
    pass


def validate_endpoint_or_raise(endpoint: str) -> None:
    """Validate an endpoint URL; raises SSRFError on failure.

    Convenience wrapper around validate_endpoint() that raises instead of
    returning a tuple. Use this in call-sites that want try/except control
    flow rather than tuple unpacking.
    """
    ok, err_msg = validate_endpoint(endpoint)
    if not ok:
        raise SSRFError(err_msg)


__all__ = ["validate_endpoint", "validate_endpoint_or_raise", "is_private_host", "SSRFError"]