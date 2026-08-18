"""Range Chart Analyzer backend server (stdlib only).

Serves the static web frontend AND a same-origin POST /api/extract endpoint.
Because the browser now calls this local server (same origin) instead of
MiniMax directly, the CORS problem disappears: the server makes the outbound
MiniMax call server-side with urllib, exactly like the GUI does.

Run:  python server.py [--port 8000] [--host 127.0.0.1]
Then open http://127.0.0.1:8000/
"""

from __future__ import annotations

import argparse
import base64
import collections
import concurrent.futures
import ipaddress
import json
import os
import re
import secrets
import socket
import sys
import time
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

# --- Sliding-window rate limiter (30 requests / minute per remote IP) ---
_RATE_WINDOW_SEC = 60
_RATE_MAX_REQUESTS = 30
_rate_history: dict[str, collections.deque] = {}
_rate_lock = threading.Lock()

# CSRF store cap — protects against unbounded memory growth from
# clients minting tokens faster than they use them.
_CSRF_STORE_MAX = 4096


# S8 fix: redact API-key-like patterns from error_body before echoing to the UI.
# Upstream servers may echo back request headers (including Authorization)
# or query params. We redact common key formats to prevent accidental leakage.
_API_KEY_RE = re.compile(
    r'(sk-|Bearer |x-api-key[:=]\s*)[a-zA-Z0-9_\-]{8,}',
    re.IGNORECASE,
)


def _redact_error_body(body):
    """Remove API-key-like tokens from a string before sending to the client."""
    if not isinstance(body, str):
        return ""
    return _API_KEY_RE.sub(r'\1[REDACTED]', body)


# Issue-2 fix: validate base64 format and size for image_b64.
# Rejects strings that are not valid base64 or decode to more than 10MB.
_MAX_IMAGE_B64_BYTES = 10_000_000  # 10 MB


def _validate_image_b64(data):
    """Validate image_b64: returns (ok, error_key_or_empty)."""
    if not data:
        return False, "err.noImage"
    # Quick length check: base64 is ~4/3 of raw bytes, so max b64 length ≈ 4/3 * 10MB ≈ 13.4MB
    if len(data) > 14_000_000:
        return False, "err.imageTooLarge"
    try:
        decoded = base64.b64decode(data, validate=True)
    except Exception:
        return False, "err.imageInvalidBase64"
    if len(decoded) > _MAX_IMAGE_B64_BYTES:
        return False, "err.imageTooLarge"
    return True, ""


# Allow running as `python server.py` from the project root.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from rca_core import extract  # noqa: E402
from rca_core.extractor import (  # noqa: E402
    DEFAULT_ENDPOINT,
    DEFAULT_MAX_TOKENS,
    DEFAULT_MODEL,
    DEFAULT_TIMEOUT_SEC,
    ExtractResult,
    clamp_max_tokens,
)
from rca_core.aggregate import (  # noqa: E402
    COLUMNAR_SECTION_SCHEMA,
    RANGE_CHART_SCHEMA,
    SCHEMA_BY_MODE,
    merge_results,
)
from rca_core.llm import ApiFormat, LlmProvider  # noqa: E402


def _safe_score_range_chart(data):
    """Attach advisory quality metadata without risking extraction delivery."""
    try:
        from rca_core.quality import score_range_chart
        return score_range_chart(data)
    except Exception as exc:  # noqa: BLE001 - quality is non-critical metadata
        print(
            f"[quality] scoring failed: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return {
            "score": 0.0,
            "grade": "F",
            "issues": [{
                "severity": "warning",
                "msg_key": "quality.scoring_failed",
                "params": {"dimension": "server", "error_type": type(exc).__name__},
            }],
        }


# P1-3 (REVIEW-2026-07-27): wire the server into HistoryStore so web
# extractions produce a real audit trail (previously only GUI extractions
# were persisted). The import is lazy-ish to avoid pulling sqlite if the
# server is run in a read-only environment.
try:
    from rca_core import Database, HistoryStore, HistoryRecord
    _HAS_HISTORY = True
except Exception:  # noqa: BLE001 — defensive
    _HAS_HISTORY = False


def _history_store_singleton():
    """Return a HistoryStore if history is available, else None.

    Created lazily to avoid a hard sqlite dependency at server import.
    Cached at module level so every extraction hits the same db.
    """
    if not _HAS_HISTORY:
        return None
    global _HISTORY_STORE_SINGLETON_CACHE
    try:
        store = _HISTORY_STORE_SINGLETON_CACHE
    except NameError:
        store = None
    if store is None:
        try:
            store = HistoryStore(db=Database())
            _HISTORY_STORE_SINGLETON_CACHE = store
        except Exception:
            return None
    return store


def _write_history_record(result, mode, runs, provider, max_tokens,
                          chart_lang, partial_failures=0,
                          duration_ms=None, raws=None):
    """Persist an ExtractResult to the audit DB. Best-effort — never raises.

    The audit trail captures:
      * image fingerprint (image_sha256, computed by compute_image_sha256_from_b64)
      * full sampling context (model, max_tokens, prompt_version, temperature,
        seed) — either from the live provider.extra_body or from the
        request_meta the extractor populated.
      * per-slot raw text when multi-run, so a researcher can reparse the
        exact LLM reply years later without re-running the API.
    """
    store = _history_store_singleton()
    if store is None:
        return
    try:
        from rca_core.image_hash import compute_image_sha256_from_b64
        meta = dict(getattr(result, "request_meta", None) or {})
        # request_meta was populated by extract_*(). If absent (e.g. an
        # older version, or a path that bypassed the success return),
        # build a minimal audit dict from the live provider.
        if not meta:
            from rca_core.prompt import prompt_version_for_mode
            meta = {
                "mode": mode,
                "max_tokens": max_tokens,
                "chart_lang": chart_lang,
                "prompt_version": prompt_version_for_mode(mode),
                "runs": runs,
            }
            if provider is not None:
                meta["model"] = provider.model
                meta["endpoint"] = provider.endpoint
                meta["api_format"] = provider.api_format.value
                eb = provider.extra_body or {}
                if "temperature" in eb:
                    meta["temperature"] = eb["temperature"]
                if "seed" in eb:
                    meta["seed"] = eb["seed"]
        # image_sha256: prefer the one already-computed by the extractor
        # (covers clipboard-paste paths), else hash now.
        sha = getattr(result, "image_sha256", "") or compute_image_sha256_from_b64(
            getattr(result, "_image_b64_for_audit", "")
        )
        meta.setdefault("image_sha256", sha)
        meta.setdefault("runs", runs)

        # Build per-run raw_responses entries.
        raw_responses = []
        if raws:
            for i, txt in enumerate(raws):
                if not txt:
                    continue
                raw_responses.append({
                    "run_idx": i,
                    "raw_text": txt,
                    "prompt_text": "",
                    "request_meta": {**meta, "run_idx": i},
                    "timestamp": int(time.time()),
                })

        rec = HistoryRecord(
            timestamp=time.time(),
            source_file="server:" + (provider.endpoint if provider else ""),
            image_thumbnail=None,
            image_width=0,
            image_height=0,
            provider_id=(provider.id if provider else "") or "",
            provider_name=(provider.name if provider else "") or "",
            model=(provider.model if provider else "") or "",
            mode=mode,
            runs=runs,
            result=result.data if result.ok and isinstance(result.data, dict) else {},
            raw=(result.raw or "")[:8192],
            confidence=float((result.data or {}).get("confidence", 0.0) or 0.0),
            partial_failures=partial_failures,
            duration_ms=duration_ms if duration_ms is not None else int(getattr(result, "latency_ms", 0) or 0),
            status_code=getattr(result, "status", None),
            notes="",
            image_sha256=sha,
            request_meta=meta,
        )
        store.add(rec, raw_responses=raw_responses if raw_responses else None)
    except Exception as exc:
        # Never propagate. The extraction result is the user-facing
        # signal; a history failure must not break extraction.
        try:
            sys.stderr.write("history write failed: %s\n" % exc)
        except Exception:
            pass


ROOT = os.path.dirname(os.path.abspath(__file__))

# Static asset root: which top-level entries under ROOT are served.
# Everything outside this set is rejected even if the extension whitelist
# would otherwise allow it. Prevents the server from leaking source
# files (server.py, *.py in rca_core/, *.db, .env, secrets/, etc.) via
# the same-origin static handler.
STATIC_ALLOWED_ENTRIES = (
    "index.html",
    "js",
    "css",
    "assets",
    "app",
    "references",
    "favicon.ico",
)

# Expected host allowlist — populated when the server starts based on
# --host / --port (or the app.py picked port). The CSRF Origin/Referer
# check compares the request's origin netloc against this set instead
# of the client-controlled ``Host`` header (which a DNS-rebinding
# attacker can spoof).
EXPECTED_HOSTS: set[str] = set()


def populate_expected_hosts(host: str, port: int) -> None:
    """Fill EXPECTED_HOSTS from the bind address + this machine's own IPs.

    REVIEW-2026-07-31: previously ``--host 0.0.0.0`` only added
    ``0.0.0.0:port`` / ``127.0.0.1:port``, so LAN clients browsing
    ``http://192.168.x.y:port/`` POSTed with an Origin netloc that was
    NOT in the allowlist — every extraction was 403-rejected. And the
    app.py (PyWebView) path never populated the set at all, silently
    falling back to comparing the Origin against the client-controlled
    ``Host`` header (a DNS-rebinding hole).

    This helper adds the wildcard host, loopback aliases, and every local
    interface IP (with the port), so both the CLI server and the PyWebView
    wrapper anchor the CSRF check to addresses this machine actually
    serves on. Call it from every startup path (``main()`` and
    ``app.py:_start_server``).
    """
    host = (host or "").strip()
    EXPECTED_HOSTS.add(f"localhost:{port}")
    EXPECTED_HOSTS.add(f"127.0.0.1:{port}")
    EXPECTED_HOSTS.add(f"[::1]:{port}")
    if host and host not in ("0.0.0.0", "::", ""):
        EXPECTED_HOSTS.add(f"{host}:{port}")
    if host in ("0.0.0.0", "::", ""):
        # Wildcard bind — enumerate this machine's own addresses so LAN
        # clients can reach the CSRF check with their real Origin.
        try:
            infos = socket.getaddrinfo(socket.gethostname(), None)
        except socket.gaierror:
            infos = []
        seen: set[str] = set()
        for info in infos:
            ip = info[4][0]
            if ip in seen:
                continue
            seen.add(ip)
            if ":" in ip:
                EXPECTED_HOSTS.add(f"[{ip}]:{port}")
            else:
                EXPECTED_HOSTS.add(f"{ip}:{port}")

# CSRF token storage: thread-safe dict mapping session tokens to CSRF tokens.
# In a production system you'd use a proper session store (Redis, DB, etc.).
# For this single-server application, an in-memory dict with a lock suffices.
_csrf_lock = threading.RLock()
_csrf_store: dict[str, tuple[str, float]] = {}  # session_token -> (csrf_token, created_at)
_CSRF_TOKEN_TTL_SEC = 3600  # 1 hour


def _generate_csrf_token() -> str:
    """Generate a cryptographically random CSRF token."""
    return secrets.token_urlsafe(32)


def _get_csrf_for_session(session_token: str) -> str | None:
    """Retrieve the CSRF token for a given session, or None if not found or expired."""
    with _csrf_lock:
        entry = _csrf_store.get(session_token)
        if entry is None:
            return None
        csrf_token, created_at = entry
        if time.time() - created_at > _CSRF_TOKEN_TTL_SEC:
            _csrf_store.pop(session_token, None)
            return None
        return csrf_token


def _set_csrf_for_session(session_token: str, csrf_token: str) -> None:
    """Store the CSRF token for a given session, evicting oldest entries
    when the store exceeds :data:`_CSRF_STORE_MAX` (FIFO bound).

    Keeps the in-memory dict from growing without bound when a misbehaving
    client keeps minting new session tokens.
    """
    with _csrf_lock:
        _csrf_store[session_token] = (csrf_token, time.time())
        # Evict if we exceed the cap. REVIEW-2026-11-07 (low): the pure
        # FIFO pop only triggered on new insertions, so EXPIRED entries
        # (TTL long past) sat in memory until a new GET happened to touch
        # them. Sweep expired entries first — they are the natural
        # eviction candidates — and only fall back to FIFO for the
        # still-valid ones if the store is genuinely over the cap.
        now = time.time()
        expired = [k for k, (_t, created) in _csrf_store.items()
                   if now - created > _CSRF_TOKEN_TTL_SEC]
        for k in expired:
            _csrf_store.pop(k, None)
        while len(_csrf_store) > _CSRF_STORE_MAX:
            # ``_csrf_store`` is an insertion-ordered dict so popping from
            # the head removes the oldest entries first.
            try:
                _csrf_store.pop(next(iter(_csrf_store)))
            except (KeyError, StopIteration):
                break


def _check_rate_limit(ip: str) -> tuple[bool, int]:
    """Return (allowed, seconds_until_reset). Sliding window 30 req / 60 s."""
    now = time.time()
    with _rate_lock:
        window = _rate_history.get(ip)
        if window is None:
            _rate_history[ip] = collections.deque([now], maxlen=_RATE_MAX_REQUESTS)
            return True, 0
        cutoff = now - _RATE_WINDOW_SEC
        while window and window[0] < cutoff:
            window.popleft()
        if len(window) >= _RATE_MAX_REQUESTS:
            oldest = window[0]
            wait = int(oldest + _RATE_WINDOW_SEC - now) + 1
            return False, max(1, wait)
        window.append(now)
        return True, 0

# Static file whitelist: only these extensions are served, and only from
# within ROOT (path traversal is rejected).
_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}

MAX_BODY_BYTES = 20 * 1024 * 1024  # S9 fix: 20 MB cap (was 40 MB).
# Multi-run (runs<=5) means concurrent decoded images could use up to
# ~5 * 15 MB ≈ 75 MB; 20 MB cap keeps total memory safe for 2 GB machines.


# Bug-6 fix: client-controlled ``provider.endpoint`` is now validated
# against this allowlist before we make an outbound HTTP request. The
# default policy is "https only, public IPs only" — loopback, link-local,
# private RFC1918, and the various cloud metadata ranges (169.254/16,
# 100.64/10) are rejected. Operators that need to talk to an internal
# gateway can override with the env var ``RCA_ALLOW_PRIVATE=1``.
# Bug-6 fix (CONSOLIDATED): the SSRF / endpoint validator previously lived
# here as a duplicate of ``rca_core.ssrf``. It is now the single source of
# truth in ``rca_core.ssrf``; server.py re-exports the canonical names so
# existing call sites (and tests) keep working unchanged.
from rca_core.ssrf import (
    is_private_host as _is_private_host,
    validate_endpoint as _validate_endpoint,
    pinned_endpoint_ip as _pinned_endpoint_ip,
)


# The DNS-pinning + no-redirect opener now lives in ``rca_core.ssrf`` as
# ``make_pinning_opener`` (single source of truth, shared with llm.py).
from rca_core.ssrf import make_pinning_opener as _make_pinning_opener

# Install the pinning + no-redirect opener at import time so every outbound
# urllib.request.urlopen() pins the resolved IP (TOCTOU / DNS-rebinding
# mitigation) and refuses 3xx redirects.
import urllib.request  # noqa: E402

try:
    urllib.request.install_opener(_make_pinning_opener())
except Exception:
    # If opener construction fails, fall back to stdlib defaults. The
    # endpoint validator is still active as a first line of defence.
    pass


class Handler(BaseHTTPRequestHandler):
    server_version = "RangeChartAnalyzer/1.0"

    # Slowloris / slow-read DoS guard: BaseHTTPRequestHandler applies this
    # to the connection socket in setup(), so a client that opens a
    # connection (or declares a large Content-Length) then sends bytes at a
    # trickle is disconnected after `timeout` seconds of inactivity instead
    # of pinning a worker thread indefinitely. 60s is generous for a normal
    # request line + headers + JSON body upload.
    timeout = 60

    # --- helpers ---
    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _validate_csrf_and_origin(self) -> bool:
        """Returns True if CSRF/origin validation passes, False if request should be rejected.

        When False is returned, a response has already been sent.
        """
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            self._send_json(415, {
                "ok": False, "error_key": "err.badContentType",
                "error_body": f"Content-Type must be application/json (got {ctype!r})",
            })
            return False
        origin = (self.headers.get("Origin") or "").strip()
        referer = (self.headers.get("Referer") or "").strip()
        x_req = (self.headers.get("X-Requested-With") or "").strip()
        host_hdr = (self.headers.get("Host") or "").strip()

        def _netloc_matches(value: str) -> bool:
            try:
                nl = urlparse(value).netloc
            except ValueError:
                return False
            if not nl:
                return False
            # REVIEW-2026-07-31: fail CLOSED when the allowlist is empty.
            # The old fallback compared the Origin against the
            # client-controlled Host header, which a DNS-rebinding attacker
            # can spoof. Every startup path (main(), app.py) now calls
            # populate_expected_hosts(), so an empty set means the request
            # came from an uninitialized server — reject it.
            return nl in EXPECTED_HOSTS

        _localhost_patterns = ("localhost", "127.0.0.1", "::1")
        if origin:
            if not _netloc_matches(origin):
                self._send_json(403, {
                    "ok": False, "error_key": "err.forbidden",
                    "error_body": "Cross-origin POST rejected (Origin does not match this server).",
                })
                return False
        elif referer:
            if not _netloc_matches(referer):
                self._send_json(403, {
                    "ok": False, "error_key": "err.forbidden",
                    "error_body": "Cross-origin POST rejected (Referer does not match this server).",
                })
                return False
        elif x_req:
            try:
                client_host = self.client_address[0]
            except Exception:
                client_host = ""
            if client_host not in _localhost_patterns:
                self._send_json(403, {
                    "ok": False, "error_key": "err.forbidden",
                    "error_body": "X-Requested-With is only accepted from localhost.",
                })
                return False
        else:
            self._send_json(403, {
                "ok": False, "error_key": "err.forbidden",
                "error_body": "Cross-origin POST rejected (need matching Origin/Referer or X-Requested-With header).",
            })
            return False
        return True

    def _safe_local_path(self, url_path: str) -> str | None:
        """Resolve a URL path to a file inside ROOT, or None if unsafe.

        Restricts the served set to a whitelist of top-level entries
        (``STATIC_ALLOWED_ENTRIES``) so source files, ``rca_core/``,
        ``proxy/``, ``tests/``, lock files, secrets, etc. cannot be
        served even when their extension is on the content-type
        whitelist. Resolves symlinks via realpath so a symlink inside
        the allowed tree pointing outside the project tree cannot be
        served.
        """
        clean = unquote(urlparse(url_path).path)
        if clean == "/" or clean == "":
            clean = "/index.html"
        rel = clean.lstrip("/")
        # Reject any traversal outright before computing the target.
        if ".." in rel.split("/"):
            return None
        # First path segment must be in the allowed-entries whitelist.
        first = rel.split("/", 1)[0]
        if first not in STATIC_ALLOWED_ENTRIES:
            return None
        target = os.path.normpath(os.path.join(ROOT, rel))
        # Resolve symlinks + check the resolved path stays under ROOT.
        real_target = os.path.realpath(target)
        real_root = os.path.realpath(ROOT)
        try:
            common = os.path.commonpath([real_target, real_root])
        except ValueError:
            return None
        if common != real_root:
            return None
        return real_target

    # --- routing ---
    def do_GET(self) -> None:
        # CSRF token endpoint: issue a token for the client to use in subsequent POSTs.
        if urlparse(self.path).path.rstrip("/") == "/api/extract":
            # Apply the same per-IP rate limit as POST so a single client
            # cannot mint unbounded CSRF tokens (LOW: unauthenticated GET
            # was previously uncapped).
            try:
                client_ip = self.client_address[0]
            except Exception:
                client_ip = "unknown"
            allowed, wait_sec = _check_rate_limit(client_ip)
            if not allowed:
                self._send_json(429, {
                    "ok": False,
                    "error_key": "err.rateLimit",
                    "error_body": f"Rate limit exceeded. Retry after {wait_sec} seconds.",
                })
                return
            session_token = self.headers.get("X-Session-Token", "")
            if not session_token:
                # Generate a new session token if none provided.
                session_token = secrets.token_urlsafe(32)
            csrf_token = _generate_csrf_token()
            _set_csrf_for_session(session_token, csrf_token)
            self._send_json(200, {
                "csrf_token": csrf_token,
                "session_token": session_token,
            })
            return
        # P2-4 (REVIEW-2026-07-25): PROV-O JSON-LD provenance export endpoint.
        # Pattern: GET /api/history/<id>/provenance
        from urllib.parse import urlparse as _urlparse
        from rca_core import Database, HistoryStore
        _parsed_path = _urlparse(self.path).path
        _prov_match = re.match(r"^/api/history/(\d+)/provenance$", _parsed_path)
        if _prov_match:
            record_id = int(_prov_match.group(1))
            db = Database()
            store = HistoryStore(db=db)
            rec = store.get(record_id)
            if rec is None:
                self._send_json(404, {"error": "not found"})
                return
            try:
                prov_jsonld = store.to_prov_jsonld(record_id)
            except Exception:
                self._send_json(500, {"error": "provenance generation failed"})
                return
            body = json.dumps(prov_jsonld, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/ld+json; charset=utf-8")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
            self.send_header("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        target = self._safe_local_path(self.path)
        if target is None:
            self._send_json(403, {"error": "forbidden"})
            return
        if not os.path.isfile(target):
            self._send_json(404, {"error": "not found"})
            return
        ext = os.path.splitext(target)[1].lower()
        if ext not in _CONTENT_TYPES:
            self._send_json(403, {"error": "type not allowed"})
            return
        # 50 MB cap on static files so a runaway client cannot OOM the
        # server by requesting a multi-GB image.
        try:
            file_size = os.path.getsize(target)
        except OSError:
            self._send_json(500, {"error": "read error"})
            return
        if file_size > 50 * 1024 * 1024:
            self._send_json(413, {"error": "file too large"})
            return
        try:
            with open(target, "rb") as f:
                data = f.read()
        except OSError:
            self._send_json(500, {"error": "read error"})
            return
        self.send_response(200)
        self.send_header("Content-Type", _CONTENT_TYPES[ext])
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        # UI-REVIEW-2026-08-01 (B1): `default-src 'self'` alone blocks the
        # upload preview (data: URLs in <img src>) and strips the inline
        # style attributes the result renderer emits (raw-response <pre>,
        # empty-row padding), so the main upload->extract flow looked
        # broken under the default backend deployment. script-src still
        # falls back to 'self' (no inline scripts), keeping the security
        # posture for scripts intact.
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; "
            "style-src 'self' 'unsafe-inline'; frame-ancestors 'none'",
        )
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:
        # LOW-2: strip trailing slash so /api/extract/ is also accepted.
        if urlparse(self.path).path.rstrip("/") != "/api/extract":
            self._send_json(404, {"error": "not found"})
            return
        # RATE LIMIT: sliding window 30 req / 60 s per remote IP.
        try:
            client_ip = self.client_address[0]
        except Exception:
            client_ip = "unknown"
        allowed, wait_sec = _check_rate_limit(client_ip)
        if not allowed:
            self._send_json(429, {
                "ok": False,
                "error_key": "err.rateLimit",
                "error_body": f"Rate limit exceeded. Retry after {wait_sec} seconds.",
            })
            return
        # CSRF / same-origin validation
        if not self._validate_csrf_and_origin():
            return

        # CSRF token validation: require X-CSRF-Token + X-Session-Token headers.
        # This prevents the same-origin JSON-form bypass where an attacker crafts
        # a form with Content-Type: application/json - the browser won't forge
        # these custom headers cross-origin.
        csrf_token = (self.headers.get("X-CSRF-Token") or "").strip()
        session_token = (self.headers.get("X-Session-Token") or "").strip()
        if not csrf_token or not session_token:
            self._send_json(403, {
                "ok": False, "error_key": "err.forbidden",
                "error_body": "Missing CSRF token. Fetch /api/extract (GET) to obtain a valid token.",
            })
            return
        stored_csrf = _get_csrf_for_session(session_token)
        if stored_csrf is None or not secrets.compare_digest(csrf_token, stored_csrf):
            self._send_json(403, {
                "ok": False, "error_key": "err.forbidden",
                "error_body": "Invalid or expired CSRF token.",
            })
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY_BYTES:
            self._send_json(413, {
                "ok": False,
                "error_key": "err.bodyTooLarge",
                "error_body": f"Content-Length {length} exceeds limit of {MAX_BODY_BYTES}",
            })
            return
        try:
            raw = self.rfile.read(length)
            req = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            self._send_json(400, {
                "ok": False,
                "error_key": "err.parse",
                "error_body": "Invalid JSON payload",
            })
            return

        # MEDIUM (audit): strict type validation on top-level JSON fields.
        # Previously the handler assumed req was a dict and called .get() —
        # valid non-dict JSON (`[1,2]`, `"str"`, `5`) raised AttributeError
        # which crashed the worker. Now we reject any non-dict payload and
        # coerce known string/dict fields to the expected types.
        if not isinstance(req, dict):
            self._send_json(400, {
                "ok": False,
                "error_key": "err.parse",
                "error_body": "JSON body must be an object",
            })
            return
        for _field in ("image_b64", "caption", "media_type",
                       "chart_lang", "mode", "api_key", "endpoint",
                       "model", "api_format"):
            v = req.get(_field)
            if v is not None and not isinstance(v, str):
                self._send_json(400, {
                    "ok": False,
                    "error_key": "err.parse",
                    "error_body": f"field {_field!r} must be a string",
                })
                return
        for _field in ("extra_headers", "extra_body"):
            v = req.get(_field)
            if v is not None and not isinstance(v, dict):
                self._send_json(400, {
                    "ok": False,
                    "error_key": "err.parse",
                    "error_body": f"field {_field!r} must be an object",
                })
                return

        image_b64 = req.get("image_b64") or ""
        # Issue-2 fix: validate base64 format and size (10MB limit)
        ok, err_key = _validate_image_b64(image_b64)
        if not ok:
            self._send_json(200, {"ok": False, "error_key": err_key})
            return

        # P0-1 fix (REVIEW-2026-08-17): reject unsupported modes EARLY,
        # before any SSRF / provider validation. The extractor now also
        # implements chemical_stratigraphy / paleomap / scatter_plot but
        # those are not yet wired through the full stack (no MergeSchema,
        # no server-side quality scoring, no GUI entry point). Accepting
        # them here would silently fall through to range_chart, producing
        # the WRONG prompt + normalize + merge schema with ok=True — a P0
        # data-integrity failure. Reject unknown / not-yet-wired modes
        # with a clear 400 instead.
        _SUPPORTED_MODES = frozenset({
            'range_chart',
            'columnar_section',
            'abundance_diagram',
            'phylogenetic_tree',
        })
        _requested_mode = (req.get('mode') or 'range_chart').strip()
        if _requested_mode not in _SUPPORTED_MODES:
            self._send_json(400, {
                "ok": False,
                "error_key": "err.badMode",
                "error_body": (
                    f"unsupported mode {_requested_mode!r}; "
                    f"supported modes: {sorted(_SUPPORTED_MODES)}"
                ),
                "supported_modes": sorted(_SUPPORTED_MODES),
            })
            return

        # Build the LlmProvider. Prefer the explicit `provider` object if given;
        # otherwise fall back to legacy flat fields (api_key / endpoint / model).
        provider_raw = req.get("provider")
        if isinstance(provider_raw, dict):
            try:
                provider = LlmProvider.from_dict(provider_raw)
            except Exception:
                provider = None
        elif provider_raw is None:
            provider = None
        else:
            # Provider was provided but wasn't a dict (e.g. an array or a
            # string). Reject — falling back to legacy fields would silently
            # drop the user's provider choice.
            self._send_json(400, {
                "ok": False,
                "error_key": "err.parse",
                "error_body": "field 'provider' must be an object or null",
            })
            return

        # H3: a provider object with no api_key must fall through to the
        # legacy textbox path; otherwise the request goes out with no auth
        # header and the user gets a confusing 401.
        if provider is not None and not (provider.api_key or "").strip():
            provider = None

        if provider is None:
            api_key = (req.get("api_key") or "").strip()
            if not api_key:
                self._send_json(200, {"ok": False, "error_key": "err.noKey"})
                return
            fmt_raw = (req.get("api_format") or "anthropic").strip()
            try:
                fmt = ApiFormat(fmt_raw)
            except ValueError:
                fmt = ApiFormat.ANTHROPIC
            endpoint = (req.get("endpoint") or DEFAULT_ENDPOINT)
            # Bug-6 fix: validate the endpoint before issuing any
            # outbound HTTP request. A malicious client could otherwise
            # use this server as an SSRF probe for internal services.
            ok, why = _validate_endpoint(endpoint)
            if not ok:
                self._send_json(200, {
                    "ok": False,
                    "error_key": "err.badEndpoint",
                    "error_body": why,
                })
                return
            provider = LlmProvider(
                name="Legacy Anthropic-compatible",
                api_format=fmt,
                endpoint=endpoint,
                api_key=api_key,
                model=(req.get("model") or DEFAULT_MODEL),
                extra_headers=dict(req.get("extra_headers") or {}),
                extra_body=dict(req.get("extra_body") or {}),
            )
        else:
            # Bug-6 fix: also validate when the client supplied a full
            # provider dict — the legacy path above doesn't catch that.
            ok, why = _validate_endpoint(provider.endpoint)
            if not ok:
                self._send_json(200, {
                    "ok": False,
                    "error_key": "err.badEndpoint",
                    "error_body": why,
                })
                return

        try:
            # M5: clamp to [MIN, MAX] so a client can't demand 99M tokens.
            max_tokens = clamp_max_tokens(req.get("max_tokens"))
        except (TypeError, ValueError):
            max_tokens = DEFAULT_MAX_TOKENS

        try:
            runs = int(req.get("runs") or 1)
        except (TypeError, ValueError):
            runs = 1
        runs = max(1, min(runs, 5))

        try:
            timeout_sec = int(req.get("timeout_sec") or DEFAULT_TIMEOUT_SEC)
        except (TypeError, ValueError):
            timeout_sec = DEFAULT_TIMEOUT_SEC
        timeout_sec = max(10, min(timeout_sec, 300))

        mode = _requested_mode

        common = dict(
            image_b64=image_b64,
            media_type=req.get("media_type") or "image/png",
            caption=req.get("caption") or "",
            chart_lang=req.get("chart_lang") or "auto",
            max_tokens=max_tokens,
            provider=provider,
            timeout_sec=timeout_sec,
        )

        # HIGH (audit): cache key now includes caption, media_type, and
        # extra_headers VALUES (not just keys). The previous key omitted
        # caption so the same image with different captions returned a
        # cached result, and hashed only header names so flipping the
        # Authorization value still hit. ``run_idx`` is added for
        # multi-run slots below so each slot has a distinct key.
        def _stable_extra_headers(prov_obj):
            if not prov_obj:
                return []
            # Sort by key, return sorted (key, value) tuples so different
            # header VALUES produce different keys.
            return sorted(
                (k, str(v)) for k, v in (prov_obj.extra_headers or {}).items()
            )

        def _stable_extra_body(prov_obj):
            """P1-4 (REVIEW-2026-07-25): extra_body is a provider field
            that changes the LLM request shape (e.g. Anthropic prompt
            caching toggles, custom sampling parameters). Two requests
            with different extra_body MUST NOT share a cache entry."""
            if not prov_obj:
                return []
            return sorted(
                (k, str(v)) for k, v in (prov_obj.extra_body or {}).items()
            )

        if runs == 1:
            # FIX (cache): check the cache first so identical reruns are
            # free. The key includes image, provider, model, prompt version,
            # and extraction parameters. A hit returns immediately; a miss
            # falls through to the actual LLM call.
            force_rerun = bool(req.get("force_rerun"))
            cache_hit = None
            ckey = None
            if not force_rerun:
                from rca_core.cache import get_cache
                from rca_core.prompt import prompt_version_for_mode
                cache = get_cache()
                # C1 fix: use stable business fields only — never repr(provider)
                # (repr includes uuid4 id + time.time() which change on every
                # request, making the single-run cache命中率 ≈ 0).
                prov = common["provider"]
                ckey = cache.make_key(
                    endpoint=prov.endpoint if prov else "",
                    model=prov.model if prov else "",
                    api_format=prov.api_format.value if prov else "",
                    extra_headers=_stable_extra_headers(prov),
                    extra_body=_stable_extra_body(prov),
                    prompt_version=prompt_version_for_mode(mode),
                    max_tokens=common["max_tokens"],
                    chart_lang=common["chart_lang"],
                    mode=mode,
                    image_b64=common["image_b64"],
                    caption=common["caption"],
                    media_type=common["media_type"],
                )
                cache_hit = cache.get(ckey)
            if cache_hit is not None:
                # FIX (quality): ensure the quality badge is present even on
                # a cache hit. The cached payload may predate the quality
                # scorer if an older client wrote it.
                if isinstance(cache_hit, dict) and "quality" not in cache_hit:
                    cache_hit["quality"] = _safe_score_range_chart(cache_hit)
                self._send_json(200, {"ok": True, "data": cache_hit,
                                      "cached": True})
                return
            result = extract(mode=mode, **common)
            # FIX (quality): score single-run results too for a consistent
            # quality badge in the UI.
            if result.ok and result.data and isinstance(result.data, dict):
                result.data["quality"] = _safe_score_range_chart(result.data)
                # Write the scored result back to the cache.
                if not force_rerun:
                    cache.put(ckey, result.data)
            # P1-3 (REVIEW-2026-07-27): persist this single-run extract
            # as a HistoryRecord so the audit trail captures it. Skip
            # on cache hits (they're already a previous audit record).
            _write_history_record(
                result, mode=mode, runs=1, provider=provider,
                max_tokens=common["max_tokens"],
                chart_lang=common["chart_lang"],
                partial_failures=0,
                duration_ms=result.latency_ms,
                raws=[result.raw] if result.raw else None,
            )
            self._send_json(200, {
                "ok": result.ok,
                "data": result.data,
                "error_key": result.error_key,
                "status": result.status,
                "raw": result.raw,
                "truncated": result.truncated,
                # M40: surface partial-success warning (e.g. model hit
                # max_tokens) so the frontend can show "result may be
                # incomplete" without flipping ok=False.
                "warning": getattr(result, "warning", "") or "",
                # H7: surface upstream error body so the frontend can show
                # the real 5xx reason instead of a generic alert.
                "error_body": _redact_error_body(result.error_body),
                "usage": result.usage or {},
                "latency_ms": result.latency_ms or 0,
            })
            return

        # I5 fix (multi-run cache): check the cache before launching threads.
        # Cache hits are used directly; only cache misses invoke the LLM.
        # This avoids redundant LLM calls when the user re-runs with the same
        # parameters (e.g. tweaking caption and re-running).
        force_rerun = bool(req.get("force_rerun"))
        ok_datas = []
        # P0-4 (REVIEW-2026-07-25): slot-keyed result map so non-contiguous
        # cache hits do not cause the submission loop to skip the true miss.
        slot_results: dict[int, dict] = {}
        # P1 fix (2026-08-06): a merged multi-run result must keep the image
        # fingerprint + per-request metadata of the first successful run.
        # Previously the merged ExtractResult was built with image_sha256=""
        # (and the _write_history_record fallback read the never-assigned
        # ``_image_b64_for_audit`` attribute), so every multi-run history
        # record was stamped with the empty-bytes SHA-256 (e3b0c44...) and
        # get_by_sha256 grouped unrelated multi-run records as one image.
        # Cache-hit slots carry no fingerprint (those records were already
        # audited when first written), so we only capture from live runs.
        first_ok_sha = ""
        first_ok_meta = None
        last_fail = None
        any_truncated = False
        partial_fails = 0  # M2
        merged_warning = ""   # M40: collected from the first run that has one
        raws = []
        total_in, total_out = 0, 0
        total_cr, total_cc = 0, 0
        est_in, est_out = False, False
        max_run_latency = 0
        batch_t0 = time.perf_counter()
        per_future_timeout = timeout_sec + 10

        # CRITICAL fix (audit): each multi-run slot must have its OWN cache
        # key. Previously the per-slot loop produced identical keys
        # because the key inputs were loop-invariant, so all N cache
        # writes clobbered each other and only the first slot's result
        # was retained. We salt each slot's key with ``run_idx`` so the
        # slots are independently addressable.
        from rca_core.cache import get_cache
        from rca_core.prompt import prompt_version_for_mode
        prov = common["provider"]
        slot_keys = []
        for run_idx in range(runs):
            slot_ckey = get_cache().make_key(
                endpoint=prov.endpoint if prov else "",
                model=prov.model if prov else "",
                api_format=prov.api_format.value if prov else "",
                extra_headers=_stable_extra_headers(prov),
                extra_body=_stable_extra_body(prov),
                prompt_version=prompt_version_for_mode(mode),
                max_tokens=common["max_tokens"],
                chart_lang=common["chart_lang"],
                mode=mode,
                image_b64=common["image_b64"],
                caption=common["caption"],
                media_type=common["media_type"],
                run_idx=run_idx,
            )
            slot_keys.append(slot_ckey)

        if not force_rerun:
            cache = get_cache()
            for run_idx, ckey in enumerate(slot_keys):
                cached = cache.get(ckey)
                if cached is not None:
                    if isinstance(cached, dict) and "quality" not in cached:
                        cached["quality"] = _safe_score_range_chart(cached)
                    # P0-4 (REVIEW-2026-07-25): KEYED BY SLOT INDEX so
                    # non-contiguous cache hits do not cause the
                    # submission loop to skip the true miss or re-run
                    # an already-hit slot.
                    slot_results[run_idx] = cached

        # Build ok_datas in slot order for the merge call.
        ok_datas = [slot_results[i] for i in range(runs) if i in slot_results]

        misses = runs - len(slot_results)
        if misses <= 0:
            # All runs were cache hits — merge directly.
            pass  # falls through to merge
        else:
            # LOW fix (audit): ``as_completed`` cannot receive ``None``
            # futures — the previous code stored ``None`` for cache-hit
            # slots and then passed them all to ``as_completed``, which
            # raised ``TypeError: object NoneType can't be used in
            # 'await' expression``. Instead, build a parallel ``futures``
            # list (cache-hit slots contribute no entry) and iterate by
            # pairing ``(run_idx, future)`` so the original slot index
            # survives reordering.
            #
            # P0-4 (REVIEW-2026-07-25): ``if run_idx < len(ok_datas)`` was
            # incorrectly skipping true misses whenever cache hits were
            # non-contiguous (e.g. hits at slot 0 + 2 but miss at slot 1).
            # Now we check slot_results membership directly.
            with concurrent.futures.ThreadPoolExecutor(max_workers=misses) as ex:
                pending = []  # list of (run_idx, future)
                for run_idx in range(runs):
                    if run_idx in slot_results:
                        continue  # cache hit — already in slot_results
                    pending.append((run_idx, ex.submit(extract, mode=mode, **common)))
                # as_completed yields futures in completion order, not
                # original order. We use the stored run_idx to map back.
                futures = [f for _, f in pending]
                for fut in concurrent.futures.as_completed(futures):
                    # Reverse-lookup: find the run_idx for this future.
                    run_idx = next(
                        (ri for ri, f in pending if f is fut),
                        None,
                    )
                    if run_idx is None:
                        continue
                    try:
                        r = fut.result(timeout=per_future_timeout)
                    except concurrent.futures.TimeoutError:
                        r = ExtractResult(
                            ok=False, error_key="err.timeout",
                            error_body=f"per-future timeout after {per_future_timeout}s",
                        )
                    except Exception as exc:
                        r = ExtractResult(ok=False, error_key="err.http", raw=str(exc))
                    # H-1 fix: update max_run_latency regardless of r.ok — only
                    # skip when latency_ms is None/0 (no request was made).
                    if r.latency_ms not in (None, 0):
                        max_run_latency = max(max_run_latency, int(r.latency_ms))
                    if r.ok and r.data is not None:
                        if not force_rerun:
                            get_cache().put(slot_keys[run_idx], r.data)
                        # P0-4 (REVIEW-2026-07-25): store under slot index.
                        # ok_datas is rebuilt in slot order before merge.
                        slot_results[run_idx] = r.data
                        # P1 fix (2026-08-06): keep the fingerprint + request
                        # metadata from the first successful live run so the
                        # merged audit record is as complete as a single-run
                        # record.
                        if not first_ok_sha and getattr(r, "image_sha256", ""):
                            first_ok_sha = r.image_sha256
                        if first_ok_meta is None and getattr(r, "request_meta", None):
                            first_ok_meta = r.request_meta
                        ok_datas = [slot_results[i] for i in range(runs) if i in slot_results]
                        any_truncated = any_truncated or bool(r.truncated)
                        if r.raw:
                            raws.append(r.raw)
                        u = r.usage or {}
                        total_in += int(u.get("input_tokens") or 0)
                        total_out += int(u.get("output_tokens") or 0)
                        total_cr += int(u.get("cache_read_tokens") or 0)
                        total_cc += int(u.get("cache_creation_tokens") or 0)
                        est_in = est_in or bool(u.get("estimated"))
                        est_out = est_out or bool(u.get("estimated"))
                        if not merged_warning and getattr(r, "warning", ""):
                            merged_warning = r.warning
                    else:
                        last_fail = r
                        partial_fails += 1
                        if not merged_warning and getattr(r, "warning", ""):
                            merged_warning = r.warning
        total_latency = int((time.perf_counter() - batch_t0) * 1000)
        if not ok_datas:
            r = last_fail
            self._send_json(200, {
                "ok": False,
                "data": None,
                "error_key": r.error_key if r else "err.empty",
                "status": r.status if r else None,
                "raw": r.raw if r else "",
                "truncated": any_truncated,
                # Surface the upstream error body so the client can show
                # the real 5xx reason instead of a generic alert (H7).
                "error_body": _redact_error_body(r.error_body) if r else "",
                "usage": r.usage if r else {},
                "latency_ms": r.latency_ms if r else 0,
                "warning": getattr(r, "warning", "") if r else "",
            })
            return
        schema = SCHEMA_BY_MODE.get(mode, RANGE_CHART_SCHEMA)
        merged = merge_results(ok_datas, total_runs=runs, schema=schema)
        # FIX (quality): score the merged result so the UI can show a
        # quality badge ("0.87 / B-Good") and flag low-confidence rows.
        quality = _safe_score_range_chart(merged)
        merged["quality"] = quality
        # REVIEW-2026-07-31: build the aggregated usage BEFORE the audit
        # write. The previous code referenced ``merged_usage`` inside the
        # try below but only assigned it AFTER the except block, so every
        # multi-run extraction raised UnboundLocalError that the
        # ``except Exception: pass`` swallowed — the audit record was
        # never written (silent data loss).
        merged_usage = {
            "input_tokens": total_in,
            "output_tokens": total_out,
            "cache_read_tokens": total_cr,
            "cache_creation_tokens": total_cc,
        }
        if est_in or est_out:
            merged_usage["estimated"] = True
        # P1-3 (REVIEW-2026-07-27): persist this multi-run extraction to
        # the audit DB. The per-slot raw text is collected into the
        # raw_responses table so a researcher can repare the exact LLM
        # reply for each slot years later.
        try:
            from rca_core.extractor import ExtractResult
            multi_result = ExtractResult(
                ok=True,
                data=merged,
                error_key=None,
                status=200,
                raw=("\n---RUN---\n".join(raws))[:8000] if raws else "",
                truncated=any_truncated,
                partial_failures=partial_fails,
                usage=merged_usage,
                latency_ms=int((time.perf_counter() - batch_t0) * 1000),
                warning=merged_warning or "",
                # P1 fix (2026-08-06): propagate the real image fingerprint
                # captured from the first successful live run (was "").
                image_sha256=first_ok_sha,
            )
            # Build a complete request_meta for this multi-run batch.
            multi_meta = {
                "mode": mode,
                "runs": runs,
                "max_tokens": common["max_tokens"],
                "chart_lang": common["chart_lang"],
                "endpoint": provider.endpoint if provider else "",
                "model": provider.model if provider else "",
                "api_format": provider.api_format.value if provider else "",
                "prompt_version": prompt_version_for_mode(mode),
            }
            eb = (provider.extra_body or {}) if provider else {}
            if "temperature" in eb:
                multi_meta["temperature"] = eb["temperature"]
            if "seed" in eb:
                multi_meta["seed"] = eb["seed"]
            # P1 fix (2026-08-06): fold the first successful run's per-request
            # metadata (model, endpoint, temperature, seed, image_sha256, ...)
            # into the batch meta so the audit record matches the completeness
            # of a single-run record. The batch meta (explicit values above)
            # wins on key collisions.
            for _k, _v in (first_ok_meta or {}).items():
                multi_meta.setdefault(_k, _v)
            multi_result.request_meta = multi_meta
            _write_history_record(
                multi_result, mode=mode, runs=runs,
                provider=provider, max_tokens=common["max_tokens"],
                chart_lang=common["chart_lang"],
                partial_failures=partial_fails,
                duration_ms=int((time.perf_counter() - batch_t0) * 1000),
                raws=raws if raws else None,
            )
        except Exception:
            pass
        # M2: partial_failures is surfaced separately from truncated so the
        # frontend can distinguish a failed run from a truncated one.
        # (merged_usage was built above, before the audit write.)
        # M40: emit the aggregated warning (first non-empty from any run)
        # so the client can show "result may be incomplete" alongside
        # the data instead of just the boolean truncated flag.
        self._send_json(200, {
            "ok": True,
            "data": merged,
            "runs": runs,
            "error_key": None,
            "status": None,
            "raw": ("\n---RUN---\n".join(raws))[:8000],
            "truncated": any_truncated,
            "partial_failures": partial_fails,
            "warning": merged_warning,
            "usage": merged_usage,
            "latency_ms": total_latency,
        })

    # Quieter logging.
    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("[server] " + (fmt % args) + "\n")


def main() -> None:
    ap = argparse.ArgumentParser(description="Range Chart Analyzer backend server")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument(
        "--max-threads",
        type=int,
        default=int(os.environ.get("RCA_MAX_THREADS", "32")),
        help="Maximum concurrent request threads (default: 32).",
    )
    args = ap.parse_args()

    # Populate EXPECTED_HOSTS from the actual bind address so the CSRF
    # Origin/Referer check is anchored to the real server host rather
    # than the client-controlled ``Host`` header. populate_expected_hosts
    # also adds this machine's own LAN IPs, so ``--host 0.0.0.0`` works
    # for real browser clients (the old allowlist rejected every LAN
    # Origin with a 403).
    populate_expected_hosts(args.host, args.port)

    httpd = _make_bounded_server(args.host, args.port, args.max_threads)
    url = f"http://{args.host}:{args.port}/"
    print(f"Range Chart Analyzer server running at {url}")
    print(f"Max concurrent request threads: {args.max_threads}")
    print("Open that URL in your browser. Press Ctrl+C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        httpd.shutdown()


class _BoundedThreadingHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer with a hard cap on concurrent handler threads.

    LOW (audit): stdlib ThreadingHTTPServer spawns one daemon thread per
    accepted connection with no cap. An attacker can open thousands of
    connections in parallel and exhaust memory / thread slots. We use a
    ``concurrent.futures.ThreadPoolExecutor`` with ``max_workers``
    threads and submit each accepted connection to it; additional
    connections are queued (bounded by ``request_queue_size``) and the
    OS will refuse further SYN once the listen backlog fills.
    """

    daemon_threads = True

    def __init__(self, server_address, RequestHandlerClass,
                 max_workers: int = 32,
                 request_queue_size: int = 64):
        super().__init__(server_address, RequestHandlerClass,
                         bind_and_activate=True)
        # Cap the listen backlog so we don't hold thousands of half-open
        # connections in the kernel queue.
        self.request_queue_size = request_queue_size
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=max(max_workers, 2),
            thread_name_prefix="rca-http",
        )

    def process_request(self, request, client_address):
        self._executor.submit(self.process_request_thread,
                              request, client_address)

    def server_close(self):
        try:
            self._executor.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass
        super().server_close()


def _make_bounded_server(host: str, port: int, max_workers: int):
    """Build an HTTP server bound to (host, port) with a thread cap."""
    return _BoundedThreadingHTTPServer(
        (host, port), Handler,
        max_workers=max_workers,
    )


if __name__ == "__main__":
    main()
