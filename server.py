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
import socket
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

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

MAX_BODY_BYTES = 40 * 1024 * 1024  # 40 MB cap on request bodies


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
    resolves to 127.0.0.1 is also caught (basic TOCTOU mitigation — the
    actual outbound request will resolve again and may differ).
    """
    if not host:
        return True
    # Bracketed IPv6 literal from a URL.
    bare = host.strip("[]")
    # Try a literal IP parse first (avoids DNS entirely).
    try:
        ip = ipaddress.ip_address(bare)
    except ValueError:
        # Not a literal — resolve via DNS. If resolution fails we treat
        # the host as private (don't let an attacker bypass the check
        # by passing an unresolvable name).
        try:
            infos = socket.getaddrinfo(bare, None)
        except socket.gaierror:
            return True
        try:
            ip = ipaddress.ip_address(infos[0][4][0])
        except (ValueError, IndexError):
            return True
    # is_global == False covers loopback, private, link-local,
    # multicast, reserved, and unspecified. We treat all of those as
    # private for SSRF purposes.
    return not ip.is_global


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
        # Acceptable: Origin matches Host, or Referer shares the host, or the
        # client sent a custom header (which browsers never do without CORS
        # preflight successfully passing).
        origin_ok = False
        if origin:
            # strip scheme; compare netloc to Host
            o_netloc = urlparse(origin).netloc
            if o_netloc and o_netloc == host_hdr:
                origin_ok = True
        if not origin_ok and referer:
            r_netloc = urlparse(referer).netloc
            if r_netloc and r_netloc == host_hdr:
                origin_ok = True
        if not origin_ok and not x_req:
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
            result = extract(mode=mode, **common)
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
                "error_body": result.error_body,
                "usage": result.usage or {},
                "latency_ms": result.latency_ms or 0,
            })
            return

        # Multi-run: extract N times IN PARALLEL via a thread pool, merge
        # the successful runs' data. Concurrent execution trims total wait
        # from O(N * latency) to ~O(latency) since urllib I/O releases the
        # GIL. If no run succeeds, return the last run's error so the UI
        # can surface it.
        ok_datas = []
        last_fail = None
        any_truncated = False
        partial_fails = 0  # M2
        merged_warning = ""   # M40: collected from the first run that has one
        raws = []
        total_in, total_out = 0, 0
        total_cr, total_cc = 0, 0
        est_in, est_out = False, False
        # Runs execute concurrently, so the meaningful latency is the wall-clock
        # time of the whole batch — not the sum of each run's latency (which
        # would over-count 3x for 3 parallel runs). We also track the slowest
        # single run for reference.
        max_run_latency = 0
        batch_t0 = time.perf_counter()
        # Per-future hard timeout: user's timeout_sec is the per-request LLM
        # timeout, plus a small grace window (10s) for Python/JIT overhead.
        per_future_timeout = timeout_sec + 10
        with concurrent.futures.ThreadPoolExecutor(max_workers=runs) as ex:
            futures = [ex.submit(extract, mode=mode, **common) for _ in range(runs)]
            for fut in concurrent.futures.as_completed(futures):
                try:
                    r = fut.result(timeout=per_future_timeout)
                except concurrent.futures.TimeoutError:
                    r = ExtractResult(
                        ok=False, error_key="err.timeout",
                        error_body=f"per-future timeout after {per_future_timeout}s",
                    )
                except Exception as exc:
                    # extract() never raises, but defend against unforeseen
                    # bugs in user code.
                    r = ExtractResult(ok=False, error_key="err.http", raw=str(exc))
                if r.ok and r.data is not None:
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
                    # M40: pick the first non-empty warning as the
                    # merged hint. All runs hit the same max_tokens
                    # ceiling if they truncated, so this collapses to a
                    # single user-facing message regardless of N.
                    if not merged_warning and getattr(r, "warning", ""):
                        merged_warning = r.warning
                else:
                    last_fail = r
                    partial_fails += 1
                    # Capture a failed run's warning too (e.g. parse
                    # error after a partial response) — same first-wins
                    # strategy so we surface at most one hint.
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
                "error_body": r.error_body if r else "",
                "usage": r.usage if r else {},
                "latency_ms": r.latency_ms if r else 0,
                "warning": getattr(r, "warning", "") if r else "",
            })
            return
        schema = SCHEMA_BY_MODE.get(mode, RANGE_CHART_SCHEMA)
        merged = merge_results(ok_datas, total_runs=runs, schema=schema)
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
