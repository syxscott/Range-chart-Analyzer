"""Shared SSRF / endpoint validation for rca_core.

Centralises the URL validator previously duplicated between ``gui.py`` and
``server.py`` so every entry point (legacy textbox, full provider dict,
connection-test probe, web backend) speaks the same language.

The default policy is "https only, public IPs only" — loopback, link-local,
private RFC1918, and the various cloud metadata ranges (169.254/16,
100.64/10) are rejected. Operators that need to talk to an internal
gateway can override with the env var ``RCA_ALLOW_PRIVATE=1``.

NOTE (REVIEW-2026-11-07): ``RCA_ALLOW_PRIVATE=1`` is an INTENTIONAL
escape hatch, but it disables the ENTIRE private-IP check for every
endpoint in the process (read once at import, line ``_ALLOW_PRIVATE``).
It is not per-endpoint and it does not relax the https-only rule. Set it
in the environment of the launching process (server / GUI) before import
— setting it after ``import rca_core.ssrf`` has no effect.

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

import http.client
import ipaddress
import os
import socket
import ssl
import urllib.error
import urllib.request
from urllib.parse import urlparse


_ALLOW_PRIVATE = os.environ.get("RCA_ALLOW_PRIVATE", "").strip() == "1"


# NAT64 prefixes (RFC 6052). These embed an IPv4 destination inside an
# IPv6 address. Some CPython builds report ``is_global == True`` for them,
# but they can reach internal IPv4 services (e.g. ``64:ff9b::169.254.169.254``
# -> 169.254.169.254 cloud metadata), so the SSRF check MUST reject them
# explicitly rather than relying on ``is_global`` alone.
_NAT64_WELL_KNOWN = ipaddress.ip_network("64:ff9b::/96")
_NAT64_PREFIX_48 = ipaddress.ip_network("64:ff9b:1::/48")

# REVIEW-2026-09-20 (item 14): two more ways to write a private IPv4 address
# that ``ipaddress`` reports as GLOBAL, so they slipped through the old check:
#
#   * IPv4-compatible IPv6 (``::/96``, the deprecated precursor of v4-mapped):
#     ``::127.0.0.1`` and, worse, ``::169.254.169.254`` both have is_global ==
#     True on CPython 3.9-3.13, and glibc/Linux routes them to the embedded
#     IPv4 — a direct shot at loopback services and cloud metadata. The whole
#     block is rejected (see _is_non_public_ip), not just the private embeds:
#     no real provider endpoint is addressed this way.
#   * ``192.88.99.0/24`` — the deprecated 6to4 anycast relay prefix
#     (RFC 7526). ``is_global`` is True for it, but it is unassignable and was
#     historically abused to bounce traffic onward.
_IPV4_COMPATIBLE_V6 = ipaddress.ip_network("::/96")
_6TO4_RELAY_ANYCAST = ipaddress.ip_network("192.88.99.0/24")
_6TO4_PREFIX = ipaddress.ip_network("2002::/16")


def _is_non_public_ip(ip: "ipaddress.IPv4Address | ipaddress.IPv6Address") -> bool:
    """Return True if *ip* is unsafe to connect to (private/loopback/...).

    Single source of truth for "is this address public?". Augments
    ``ip.is_global`` with:
      * IPv4-mapped IPv6 (``::ffff:a.b.c.d``) — checks the embedded v4.
      * IPv4-compatible IPv6 (``::a.b.c.d``) — checks the embedded v4 and
        rejects the block outright (REVIEW-2026-09-20).
      * 6to4 (``2002:a.b.c.d::/48``) — checks the embedded v4
        (REVIEW-2026-09-20; CPython already flags the prefix as non-global,
        the explicit check keeps that true if the stdlib table ever changes).
      * NAT64 prefixes (``64:ff9b::/96``, ``64:ff9b:1::/48``) which embed a
        destination IPv4 and are NOT reliably flagged by ``is_global``.
      * ``192.88.99.0/24`` (deprecated 6to4 relay anycast).
    """
    if isinstance(ip, ipaddress.IPv6Address):
        # An IPv4-mapped address carries an embedded IPv4; reject if that
        # v4 is itself non-public.
        mapped = ip.ipv4_mapped
        if mapped is not None and _is_non_public_ip(mapped):
            return True
        # NAT64: the low 32 bits encode an IPv4 destination.
        if ip in _NAT64_WELL_KNOWN or ip in _NAT64_PREFIX_48:
            return True
        # REVIEW-2026-09-20: IPv4-compatible IPv6 (::/96). ``is_global`` says
        # "yes, global" for these, which is why ::169.254.169.254 used to pass
        # the whole validator chain.
        if ip in _IPV4_COMPATIBLE_V6:
            return True
        # REVIEW-2026-09-20: 6to4 embeds an IPv4 destination in bits 16..47.
        sixtofour = getattr(ip, "sixtofour", None)
        if sixtofour is not None and _is_non_public_ip(sixtofour):
            return True
        if ip in _6TO4_PREFIX:
            # Anything inside 2002::/16 that the helper above did not resolve
            # to a usable public v4 is unreachable at best.
            return True
        return not ip.is_global
    # REVIEW-2026-09-20: deprecated 6to4 relay anycast (is_global == True).
    if ip in _6TO4_RELAY_ANYCAST:
        return True
    return not ip.is_global


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
        return _is_non_public_ip(ip)
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
        if _is_non_public_ip(ip):
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


def validate_endpoint_local_ok(endpoint: str) -> tuple[bool, str]:
    """Like :func:`validate_endpoint`, but loopback hosts are permitted.

    REVIEW-2026-07-31: local LLM servers (e.g. Ollama at
    ``http://127.0.0.1:11434``) are a legitimate desktop use case, so the
    connection test, the live extract paths, and the GUI all use this
    single policy — previously ``test_llm_connection`` allowed loopback
    while ``_call_anthropic``/``_call_openai`` rejected it, so a local
    endpoint passed the test but every extraction failed.

    Loopback (127.0.0.0/8, ::1, localhost / *.localhost) is allowed on ANY
    scheme (local Ollama servers are plain http). Everything else goes
    through the strict https + public-IP rules of :func:`validate_endpoint`.
    """
    if not endpoint:
        return False, "empty endpoint"
    try:
        u = urlparse(endpoint)
        host = (u.hostname or "").strip("[]")
    except ValueError:
        return False, "unparseable endpoint"
    if host:
        try:
            ip = ipaddress.ip_address(host)
            if ip.is_loopback:
                return True, ""
        except ValueError:
            low = host.lower()
            if low == "localhost" or low.endswith(".localhost"):
                return True, ""
    return validate_endpoint(endpoint)


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


# ---------------------------------------------------------------------------
# DNS pinning (SSRF TOCTOU / DNS-rebinding mitigation)
# ---------------------------------------------------------------------------
# After the endpoint is validated, the outbound HTTPS request performs its
# OWN DNS resolution. A DNS-rebinding attacker can return a public IP to the
# validator and a private IP to the actual connect. ``pinned_endpoint_ip``
# resolves the hostname ONCE and the outbound connection is forced to that
# exact IP while still presenting the original hostname in SNI / Host so the
# TLS handshake and upstream auth succeed. This is the single shared
# implementation used by both ``server.py`` and ``rca_core/llm.py``.

def _is_loopback_host(host: str) -> bool:
    """Return True when *host* is a literal loopback IP or a localhost name.

    Sprint B (REVIEW-2026-09-04): mirrors the loopback policy of
    :func:`validate_endpoint_local_ok` (literal 127.0.0.0/8, ::1 and the
    RFC 6761 "localhost" / "*.localhost" names) so the DNS-pinning layer
    and the endpoint validator agree on what a local endpoint is.
    """
    if not host:
        return False
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        low = host.lower()
        return low == "localhost" or low.endswith(".localhost")


def pinned_endpoint_ip(endpoint: str) -> str:
    """Resolve *endpoint* to a single IP literal, validating it is public.

    Returns the pinned IP as a string. Raises ``ValueError`` if the host
    can't be resolved to a usable *public* IP (fail closed).

    Sprint B (REVIEW-2026-09-04): loopback hosts are EXEMPT from the
    public-IP requirement. ``validate_endpoint_local_ok`` explicitly allows
    loopback endpoints on any scheme (local Ollama-style servers), but this
    function then raised on the same host and the pinning opener turned that
    into ``URLError("SSRF: ...")`` — so an ``https://127.0.0.1:...`` endpoint
    passed validation yet every request failed. Loopback targets carry no
    SSRF risk (there is nothing "internal" beyond the caller's own machine),
    so they skip the public-IP pinning check here. Non-loopback private
    networks keep the exact previous behaviour (still fail closed).
    """
    u = urlparse(endpoint)
    bare = (u.hostname or "").strip("[]")
    if not bare:
        raise ValueError("missing host")
    loopback_ok = _is_loopback_host(bare)
    # Literal IPv4/IPv6.
    try:
        ip = ipaddress.ip_address(bare)
    except ValueError:
        ip = None
    if ip is not None:
        if not loopback_ok and not _ALLOW_PRIVATE and _is_non_public_ip(ip):
            raise ValueError(
                f"host {bare!r} is a non-public IP {ip}; "
                "set RCA_ALLOW_PRIVATE=1 to override"
            )
        return str(ip)
    try:
        infos = socket.getaddrinfo(bare, None)
    except socket.gaierror as exc:
        raise ValueError(f"DNS resolution failed for {bare!r}: {exc}") from exc
    # REVIEW-2026-09-20 (item 15): this used to ``return`` on the FIRST entry
    # it liked, so with an answer of [public, private] it handed back the
    # public one and never looked at the rest — while the module's own
    # documented policy (and ``is_private_host``) is "check ALL addresses and
    # reject if ANY is non-public". A rebinding server that returns a public
    # A record plus a private one therefore passed a check it was written to
    # fail, and any later consumer that re-resolves (or that iterates the
    # answer set itself) can pick the private one. Validate the whole set
    # first, then pin the first usable address.
    usable: "list[str]" = []
    for info in infos:
        try:
            addr = info[4][0]
            ip = ipaddress.ip_address(addr)
        except (ValueError, IndexError):
            # An entry we cannot classify is treated as unsafe (fail closed),
            # matching is_private_host()'s handling of the same shape.
            raise ValueError(
                f"host {bare!r} resolved to an unclassifiable address "
                f"{addr!r}")
        if not loopback_ok and not _ALLOW_PRIVATE and _is_non_public_ip(ip):
            raise ValueError(
                f"host {bare!r} resolves to non-public IP {ip}; "
                "set RCA_ALLOW_PRIVATE=1 to override"
            )
        usable.append(str(ip))
    if not usable:
        raise ValueError(f"no usable address for host {bare!r}")
    return usable[0]


def _looks_like_ip(s: str) -> bool:
    try:
        ipaddress.ip_address(s)
        return True
    except ValueError:
        return False


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse all 3xx redirects on outbound LLM calls (SSRF hardening).

    Without this, ``urlopen`` would transparently follow a
    ``302 Location: http://169.254.169.254/...`` (cloud metadata) and
    bypass the endpoint validator. Legitimate LLM APIs answer POSTs
    directly and never 3xx, so blocking redirects costs nothing.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(
            req.full_url, code,
            f"redirect to {newurl!r} refused (SSRF guard)",
            headers, fp,
        )


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS connection that pins the resolved IP but keeps SNI = hostname.

    ``connect()`` connects to the IP that the handler already resolved and
    validated via ``pinned_endpoint_ip`` (passed in as ``self.host`` after
    the handler rewrites the request), while presenting ``self._sni_host``
    as the SNI / TLS server name so certificate validation still works.
    A fresh DNS lookup is never performed, closing the TOCTOU window.
    """

    _sni_host: str = ""

    def connect(self):
        if getattr(self, "_tunnel_host", None):
            # HTTPS through a proxy: tunnel to the (already validator
            # approved) real host; never pin the proxy address.
            return super().connect()
        host = self.host
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            ip = ipaddress.ip_address(
                pinned_endpoint_ip(f"https://{host}:{self.port}")
            )
        sock = socket.create_connection(
            (str(ip), self.port), timeout=self.timeout,
            source_address=self.source_address,
        )
        try:
            self.sock = self._context.wrap_socket(
                sock, server_hostname=self._sni_host or host
            )
        except Exception:
            try:
                sock.close()
            except Exception:
                pass
            raise


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """Plain-HTTP twin of :class:`_PinnedHTTPSConnection` (no TLS wrapping).

    REVIEW-2026-09-20 (item 17): the pinning layer used to cover https only,
    because ``make_pinning_opener`` registered just ``_PinnedHTTPSHandler``.
    Every ``http://`` request the process made therefore re-resolved DNS at
    connect time and skipped the SSRF check entirely — which is exactly the
    shape of a local Ollama endpoint (``http://127.0.0.1:11434``, allowed on
    any scheme by ``validate_endpoint_local_ok``), and of any redirect-free
    cleartext call made through the installed opener. Cleartext is still
    rejected for non-loopback provider endpoints by ``validate_endpoint``;
    pinning adds the missing rebinding guard on top of that policy instead of
    changing it.
    """

    def connect(self):
        if self._tunnel_host:
            return super().connect()
        host = self.host
        try:
            ipaddress.ip_address(host)
            ip = host
        except ValueError:
            ip = pinned_endpoint_ip(f"http://{host}:{self.port}")
        self.sock = socket.create_connection(
            (str(ip), self.port), timeout=self.timeout,
            source_address=self.source_address,
        )


def make_pinning_opener():
    """Build a urllib opener that pins DNS and refuses 3xx redirects.

    For direct (non-proxied) requests the handler resolves the target host to
    a single validated IP *once* (via ``pinned_endpoint_ip``) and connects to
    that exact IP while keeping the original hostname visible as the SNI /
    Host header — closing the DNS-rebinding TOCTOU window. Requests that go
    through a proxy are handled by urllib's normal tunneling (the endpoint
    validator has already rejected private/loopback targets). The opener also
    refuses any 3xx redirect, so a ``302 Location: http://169.254.169.254/...``
    cannot bypass the endpoint validator.

    Both https (since Sprint B) and http (REVIEW-2026-09-20, item 17) are
    pinned.

    NOTE (REVIEW-2026-09-20, corrected FIX-2026-09-22 item 3):
    ``install_opener`` at the bottom of this module is a PROCESS-WIDE side
    effect, not scoped to rca_core: every ``urllib.request.urlopen`` in the
    interpreter — including any GBIF / mindat lookup that still uses the
    global opener — goes through this handler, so those calls are pinned and
    no-redirect too. It used to claim pinning "protects" those public-host
    lookups; the 2026-09-22 audit MEASURED the opposite: dialling the pinned
    IP for ``api.gbif.org`` keeps the CDN edge guessing and it answers
    ``404 "This server hosts..."``, silently degrading name validation to
    ``unavailable``. It means a name that resolves to a private/loopback
    address will also fail with ``URLError("SSRF: ...")``, and a 3xx answer
    fails instead of being followed. Any outbound caller to a shared-hosting
    / CDN API MUST build a private ``urllib.request.build_opener()`` (as
    ``rca_core/names.py::_private_open`` and ``scripts/update_ics.py`` now
    do); only callers that genuinely want the pin should ride the global
    ``urlopen``.
    """

    def _pin(req, scheme: str):
        """Rewrite *req* to dial the validated IP; return the SNI hostname.

        Raises ``urllib.error.URLError`` (never a bare ``ValueError``) so
        callers keep the documented ``urlopen`` failure contract.
        """
        host = req.host
        try:
            ip = pinned_endpoint_ip(f"{scheme}://{host}")
        except ValueError as exc:
            # Non-public or unresolvable direct target -> refuse.
            # (Loopback is exempt inside pinned_endpoint_ip, matching
            # validate_endpoint_local_ok — Sprint B REVIEW-2026-09-04.)
            raise urllib.error.URLError(f"SSRF: {exc}") from exc
        # Original hostname (strip port / brackets) for SNI + Host.
        orig_host = (
            host[1:host.index("]")] if host.startswith("[")
            else host.split(":")[0]
        )
        # REVIEW-2026-09-10: preserve an explicit port. `req.host` is
        # "host[:port]"; overwriting it with the bare IP silently reset
        # the port to 443, so an https endpoint on a non-default port was
        # unreachable — and the request (key, prompt, image) was delivered
        # to whatever happens to listen on 443 of that host, i.e. a
        # service the user never addressed.
        #
        # REVIEW-2026-09-20 (item 16): ``int()`` of the port half of
        # ``req.host`` is attacker-influenced (it comes from the URL), and the
        # ValueError it raises for e.g. "http://a.com:99999x/" escaped BOTH
        # except clauses the call sites use (they catch HTTPError/URLError),
        # so the guard that is supposed to answer "SSRF: ..." crashed the
        # caller with an unexpected exception type. Parse inside the try and
        # convert to URLError, i.e. the same fail-closed contract.
        orig_port = None
        try:
            if host.startswith("["):
                after_bracket = host.split("]", 1)[1]
                if after_bracket.startswith(":"):
                    orig_port = int(after_bracket[1:])
                elif after_bracket:
                    raise ValueError(f"bad host:port {host!r}")
            elif ":" in host:
                orig_port = int(host.rsplit(":", 1)[1])
        except ValueError as exc:
            raise urllib.error.URLError(
                f"SSRF: invalid port in target {host!r}: {exc}") from exc
        default_port = 443 if scheme == "https" else 80
        pinned = f"[{ip}]" if ":" in ip else ip
        if orig_port is not None and orig_port != default_port:
            req.host = f"{pinned}:{orig_port}"
            host_header = f"{orig_host}:{orig_port}"
        else:
            req.host = pinned
            host_header = orig_host
        # Rewrite the connection target to the pinned IP while keeping
        # the original hostname visible to the server (Host / SNI).
        req.add_unredirected_header("Host", host_header)
        return orig_host

    class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
        def https_open(self, req):
            # Only reached for direct requests; ProxyHandler handles proxied
            # ones before us, so req.host is the real target (host:port).
            sni = _pin(req, "https")

            def _conn_factory(host_arg, timeout=req.timeout, **kw):
                conn = _PinnedHTTPSConnection(host_arg, timeout=timeout,
                                              context=self._context)
                conn._sni_host = sni
                return conn

            return self.do_open(_conn_factory, req)

    class _PinnedHTTPHandler(urllib.request.HTTPHandler):
        def http_open(self, req):
            # REVIEW-2026-09-20 (item 17): plain HTTP is pinned with the exact
            # same code path as HTTPS (see _PinnedHTTPConnection).
            _pin(req, "http")

            def _conn_factory(host_arg, timeout=req.timeout, **kw):
                return _PinnedHTTPConnection(host_arg, timeout=timeout)

            return self.do_open(_conn_factory, req)

    return urllib.request.build_opener(
        _PinnedHTTPSHandler(), _PinnedHTTPHandler(), _NoRedirect())


__all__ = [
    "validate_endpoint",
    "validate_endpoint_or_raise",
    "is_private_host",
    "pinned_endpoint_ip",
    "make_pinning_opener",
    "SSRFError",
]