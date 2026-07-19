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
import concurrent.futures
import ipaddress
import json
import os
import re
import socket
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse


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

ROOT = os.path.dirname(os.path.abspath(__file__))

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
_ALLOW_PRIVATE = os.environ.get("RCA_ALLOW_PRIVATE", "").strip() == "1"


def _is_private_host(host: str) -> bool:
    """Return True if *host* resolves to a non-public IP address.

    Used by the endpoint validator to block SSRF. The check resolves the
    hostname via DNS so a name like ``localhost.example.com`` that
    resolves to 127.0.0.1 is also caught.

    Hardening: ALL addresses returned by getaddrinfo are checked, not just
    the first. A DNS-rebinding attacker can return a public IP and a
    private IP in the same answer, hoping the validator samples the public
    one while the outbound request picks the private one. If any resolved
    address is non-public we reject the host outright.
    """
    if not host:
        return True
    # Bracketed IPv6 literal from a URL.
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
            # An address we can't parse is treated as unsafe.
            return True
        saw_addr = True
        # is_global == False covers loopback, private, link-local,
        # multicast, reserved, and unspecified.
        if not ip.is_global:
            return True
    # No usable address at all → treat as private (fail closed).
    return not saw_addr


def _validate_endpoint(endpoint: str) -> tuple[bool, str]:
    """Validate a provider endpoint URL. Returns (ok, error_message)."""
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
    if not _ALLOW_PRIVATE and _is_private_host(u.hostname):
        return False, (
            f"host {u.hostname!r} resolves to a non-public address. "
            "Set RCA_ALLOW_PRIVATE=1 to override (not recommended)."
        )
    return True, ""


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
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _safe_local_path(self, url_path: str) -> str | None:
        """Resolve a URL path to a file inside ROOT, or None if unsafe.

        Resolves symlinks via realpath so a symlink inside ROOT pointing
        outside the project tree cannot be served.
        """
        clean = unquote(urlparse(url_path).path)
        if clean == "/" or clean == "":
            clean = "/index.html"
        rel = clean.lstrip("/")
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
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:
        # LOW-2: strip trailing slash so /api/extract/ is also accepted.
        if urlparse(self.path).path.rstrip("/") != "/api/extract":
            self._send_json(404, {"error": "not found"})
            return
        # CSRF / same-origin: require Content-Type=application/json and either
        # an Origin/Referer matching this server's host or a custom header
        # that browsers cannot forge cross-origin without a successful
        # preflight. Without this, any web page the user visits could POST
        # to http://127.0.0.1:8000/api/extract from their browser (drive-by)
        # and make LLM calls using the user's stored provider credentials.
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            self._send_json(415, {
                "ok": False, "error_key": "err.badContentType",
                "error_body": f"Content-Type must be application/json (got {ctype!r})",
            })
            return
        origin = (self.headers.get("Origin") or "").strip()
        referer = (self.headers.get("Referer") or "").strip()
        x_req = (self.headers.get("X-Requested-With") or "").strip()
        host_hdr = (self.headers.get("Host") or "").strip()
        # CSRF policy (hardened):
        #  1. If Origin or Referer is present, it MUST match this server's
        #     Host. A present-but-mismatched Origin is a genuine cross-origin
        #     request and is rejected outright — it can NOT be rescued by
        #     tacking on an X-Requested-With header (the previous logic let
        #     `not origin_ok and not x_req` pass any request carrying an
        #     arbitrary XRW value, which defeated the check).
        #  2. X-Requested-With is only a fallback for clients that send
        #     neither Origin nor Referer (e.g. the test harness or a native
        #     script on the same host). Browsers cannot forge XRW
        #     cross-origin without a successful CORS preflight.
        def _netloc_matches(value: str) -> bool:
            try:
                nl = urlparse(value).netloc
            except ValueError:
                return False
            return bool(nl) and nl == host_hdr

        # CSRF policy (S1 fix — tightened XRW fallback):
        #  1. Origin present → must match Host (reject cross-origin).
        #  2. Origin absent, Referer present → must match Host.
        #  3. Both absent: X-Requested-With is accepted ONLY from localhost
        #     (127.0.0.1 / ::1 / localhost). This guards against a malicious
        #     page that POSTs from the browser with XRW but no Origin/Referer.
        #  4. Nothing present → reject.
        _localhost_patterns = ("localhost", "127.0.0.1", "[::1]")
        if origin:
            if not _netloc_matches(origin):
                self._send_json(403, {
                    "ok": False, "error_key": "err.forbidden",
                    "error_body": "Cross-origin POST rejected (Origin does not match Host).",
                })
                return
        elif referer:
            if not _netloc_matches(referer):
                self._send_json(403, {
                    "ok": False, "error_key": "err.forbidden",
                    "error_body": "Cross-origin POST rejected (Referer does not match Host).",
                })
                return
        elif x_req:
            # XRW only trusted when the connection is from localhost.
            try:
                client_host = self.client_address[0]
            except Exception:
                client_host = ""
            if client_host not in _localhost_patterns:
                self._send_json(403, {
                    "ok": False, "error_key": "err.forbidden",
                    "error_body": "X-Requested-With is only accepted from localhost.",
                })
                return
        else:
            self._send_json(403, {
                "ok": False, "error_key": "err.forbidden",
                "error_body": "Cross-origin POST rejected (need matching Origin/Referer or X-Requested-With header).",
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
            # Surface the parse error to the client so the frontend can show
            # *why* the body was unparseable (truncated upload, non-JSON
            # content-type, etc.) instead of a silent generic alert.
            self._send_json(400, {
                "ok": False,
                "error_key": "err.parse",
                "error_body": f"{type(exc).__name__}: {exc}",
            })
            return

        image_b64 = req.get("image_b64") or ""
        if not image_b64:
            self._send_json(200, {"ok": False, "error_key": "err.noImage"})
            return

        # Build the LlmProvider. Prefer the explicit `provider` object if given;
        # otherwise fall back to legacy flat fields (api_key / endpoint / model).
        provider_raw = req.get("provider")
        if isinstance(provider_raw, dict):
            try:
                provider = LlmProvider.from_dict(provider_raw)
            except Exception:
                provider = None
        else:
            provider = None

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

        mode = (req.get('mode') or 'range_chart').strip()
        if mode not in ('range_chart', 'columnar_section', 'abundance_diagram'):
            mode = 'range_chart'

        common = dict(
            image_b64=image_b64,
            media_type=req.get("media_type") or "image/png",
            caption=req.get("caption") or "",
            chart_lang=req.get("chart_lang") or "auto",
            max_tokens=max_tokens,
            provider=provider,
            timeout_sec=timeout_sec,
        )

        if runs == 1:
            # FIX (cache): check the cache first so identical reruns are
            # free. The key includes image, provider, model, prompt version,
            # and extraction parameters. A hit returns immediately; a miss
            # falls through to the actual LLM call.
            force_rerun = bool(req.get("force_rerun"))
            cache_hit = None
            if not force_rerun:
                from rca_core.cache import get_cache
                from rca_core.prompt import PROMPT_VERSION
                cache = get_cache()
                # C1 fix: use stable business fields only — never repr(provider)
                # (repr includes uuid4 id + time.time() which change on every
                # request, making the single-run cache命中率 ≈ 0).
                prov = common["provider"]
                ckey = cache.make_key(
                    endpoint=prov.endpoint if prov else "",
                    model=prov.model if prov else "",
                    api_format=prov.api_format.value if prov else "",
                    extra_headers=list(prov.extra_headers.keys()) if prov else [],
                    prompt_version=PROMPT_VERSION,
                    max_tokens=common["max_tokens"],
                    chart_lang=common["chart_lang"],
                    mode=mode,
                    image_b64=common["image_b64"],
                )
                cache_hit = cache.get(ckey)
            if cache_hit is not None:
                # FIX (quality): ensure the quality badge is present even on
                # a cache hit. The cached payload may predate the quality
                # scorer if an older client wrote it.
                if isinstance(cache_hit, dict) and "quality" not in cache_hit:
                    from rca_core.quality import score_range_chart
                    cache_hit["quality"] = score_range_chart(cache_hit)
                self._send_json(200, {"ok": True, "data": cache_hit,
                                      "cached": True})
                return
            result = extract(mode=mode, **common)
            # FIX (quality): score single-run results too for a consistent
            # quality badge in the UI.
            if result.ok and result.data and isinstance(result.data, dict):
                from rca_core.quality import score_range_chart
                result.data["quality"] = score_range_chart(result.data)
                # Write the scored result back to the cache.
                if not force_rerun:
                    cache.put(ckey, result.data)
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

        # Pre-compute per-slot cache keys using the same stable fields as
        # the single-run path (C1 fix). Each run slot gets its own entry so
        # that cache hits on different slots are independent.
        from rca_core.cache import get_cache
        from rca_core.prompt import PROMPT_VERSION as PROMPT_VERSION
        prov = common["provider"]
        slot_keys = []
        for _ in range(runs):
            prov_slot = LlmProvider(
                name=prov.name,
                api_format=prov.api_format,
                endpoint=prov.endpoint,
                api_key=prov.api_key,
                model=prov.model,
                extra_headers=dict(prov.extra_headers),
            )
            slot_ckey = get_cache().make_key(
                endpoint=prov_slot.endpoint,
                model=prov_slot.model,
                api_format=prov_slot.api_format.value,
                extra_headers=list(prov_slot.extra_headers.keys()),
                prompt_version=PROMPT_VERSION,
                max_tokens=common["max_tokens"],
                chart_lang=common["chart_lang"],
                mode=mode,
                image_b64=common["image_b64"],
            )
            slot_keys.append(slot_ckey)

        if not force_rerun:
            cache = get_cache()
            for ckey in slot_keys:
                cached = cache.get(ckey)
                if cached is not None:
                    if isinstance(cached, dict) and "quality" not in cached:
                        from rca_core.quality import score_range_chart
                        cached["quality"] = score_range_chart(cached)
                    ok_datas.append(cached)

        misses = runs - len(ok_datas)
        if misses <= 0:
            # All runs were cache hits — merge directly.
            pass  # falls through to merge
        else:
            # I5-fix: store (original_run_index, future_or_None) pairs so that
            # as_completed's completion-order index does NOT corrupt the cache key lookup.
            with concurrent.futures.ThreadPoolExecutor(max_workers=misses) as ex:
                pending = []  # list of (run_idx, future_or_None)
                for run_idx in range(runs):
                    if run_idx < len(ok_datas):
                        pending.append((run_idx, None))  # cache hit — result already in ok_datas
                    else:
                        pending.append((run_idx, ex.submit(extract, mode=mode, **common)))
                # as_completed yields futures in completion order, not original order.
                # We must use the stored run_idx, not the enumeration order.
                for run_idx, fut in concurrent.futures.as_completed([f for _, f in pending]):
                    if fut is None:
                        continue  # cache hit slot
                    try:
                        r = fut.result(timeout=per_future_timeout)
                    except concurrent.futures.TimeoutError:
                        r = ExtractResult(
                            ok=False, error_key="err.timeout",
                            error_body=f"per-future timeout after {per_future_timeout}s",
                        )
                    except Exception as exc:
                        r = ExtractResult(ok=False, error_key="err.http", raw=str(exc))
                    if r.ok and r.data is not None:
                        if not force_rerun:
                            get_cache().put(slot_keys[run_idx], r.data)
                        ok_datas.append(r.data)
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
                        max_run_latency = max(max_run_latency, int(r.latency_ms or 0))
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
        from rca_core.quality import score_range_chart
        quality = score_range_chart(merged)
        merged["quality"] = quality
        # M2: partial_failures is surfaced separately from truncated so the
        # frontend can distinguish a failed run from a truncated one.
        merged_usage = {
            "input_tokens": total_in,
            "output_tokens": total_out,
            "cache_read_tokens": total_cr,
            "cache_creation_tokens": total_cc,
        }
        if est_in or est_out:
            merged_usage["estimated"] = True
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
    args = ap.parse_args()

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}/"
    print(f"Range Chart Analyzer server running at {url}")
    print("Open that URL in your browser. Press Ctrl+C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        httpd.shutdown()


if __name__ == "__main__":
    main()
