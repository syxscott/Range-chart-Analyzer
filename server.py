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
import shutil
import socket
import sys
import time
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

# --- Sliding-window rate limiter (30 requests / minute per remote IP) ---
_RATE_WINDOW_SEC = 60
_RATE_MAX_REQUESTS = 30
# REVIEW-2026-09-20: the CSRF-mint GET and the provenance GET used to consume
# the SAME bucket as POST /api/extract, so any web page could drive a victim's
# browser with 30 free GETs per minute (no CSRF token needed, no auth) and
# every later legitimate extraction then got 429 - the cheap request starves
# the expensive one. Each endpoint now has its own bucket key AND a higher
# cap, so token minting can never crowd out extractions.
_RATE_MAX_REQUESTS_GET = 120
_RATE_MAX_REQUESTS_PROV = 60
_rate_history: dict[str, collections.deque] = {}
_rate_lock = threading.Lock()
# REVIEW-2026-09-10: sweep fully-expired entries once the map is this large,
# so one-shot / rotating source IPs cannot grow it without bound.
_RATE_HISTORY_SWEEP_AT = 256

# CSRF store cap — protects against unbounded memory growth from
# clients minting tokens faster than they use them.
_CSRF_STORE_MAX = 4096
# REVIEW-2026-09-10: the cap above bounds the entry COUNT but not the entry
# SIZE. A GET may supply its own X-Session-Token, which is used verbatim as
# the store key (and echoed back), so a single request with a ~64 KB header
# value added ~64 KB that persisted for the TTL — 4096 of those is ~100 MB.
# Only accept a token shaped like the ones this server mints.
_CSRF_SESSION_TOKEN_MAX_LEN = 128
_SESSION_TOKEN_RE = re.compile(r"[A-Za-z0-9_\-]{8,128}")

# Sprint B (REVIEW-2026-09-04) #2: GET /api/history/<id>/provenance serves
# audit data with no authentication, so it now requires this custom header.
# A custom header forces any cross-origin *browser* request through a CORS
# preflight (which a malicious page cannot pass), and casual port scanners
# hitting the endpoint with curl never send it. Callers must send the exact
# value: ``X-RCA-Client: range-chart-analyzer``.
# NOTE (GUI follow-up, tracked by the review lead): the only current caller
# is gui_fluent_history_detail.py::_on_export_provenance — it must add this
# header to its GET (one line) or provenance export from the Fluent GUI
# will get 403 after this change.
_PROVENANCE_CLIENT_HEADER = "X-RCA-Client"
_PROVENANCE_CLIENT_VALUE = "range-chart-analyzer"

# REVIEW-2026-09-20: ``X-RCA-Client`` is a FIXED, source-visible string, so it
# is an anti-scanner measure, not authentication: any process that can reach
# the port can read the whole audit trail (image fingerprints, provider
# endpoints/models, per-slot LLM replies) with one curl. The endpoint now ALSO
# requires that the caller is either (a) connected over loopback - the only way
# the local GUI uses it - or (b) presenting the shared secret
# ``RCA_PROVENANCE_TOKEN`` (env) in ``X-Provenance-Token``, compared in
# constant time. With no token configured the endpoint stays loopback-only;
# with a token configured EVERY caller (loopback included) must present it, so
# an operator who deliberately exposes the server cannot leave the audit trail
# readable by the whole network.
_PROVENANCE_TOKEN_HEADER = "X-Provenance-Token"
_PROVENANCE_TOKEN_ENV = "RCA_PROVENANCE_TOKEN"
# REVIEW-2026-09-20: ``int(_prov_match.group(1))`` sat OUTSIDE the try, and the
# \d+ pattern accepted a 5000-digit id: ``int()`` on > 4300 digits raises
# ValueError (CPython integer-string limit), which escaped do_GET and closed the
# connection with no JSON response at all. 12 digits is far past any real
# history id (and ``int(...)`` on <= 12 digits can never hit the limit).
_PROVENANCE_ID_RE = re.compile(r"^/api/history/(\d{1,12})/provenance$")

# Sprint B (REVIEW-2026-09-04) #1: per-request timeout clamp + the slack
# added on top of timeout_sec to form the multi-run *batch* budget.
# Module-level so regression tests can shrink them (the clamps inline in
# the handler would otherwise force a >=20 s wait to reach the timeout
# path in tests).
_MIN_EXTRACT_TIMEOUT_SEC = 10
_MAX_EXTRACT_TIMEOUT_SEC = 300
_MULTI_RUN_TIMEOUT_SLACK_SEC = 10

# REVIEW-2026-09-20: the SINGLE-run path had no deadline at all. `Handler.timeout`
# (60 s) and _BODY_DEADLINE_SEC only cover the inbound socket; the outbound
# request is bounded by a per-RECV inactivity timeout, so a slow-drip provider
# (one byte every 299 s) kept a handler thread - and its 20 MB request body -
# alive forever, 30 of them per rate-limit window. The multi-run branch already
# had a batch budget (Sprint B #1); single-run gets the same treatment: a hard
# wall-clock cutoff of ``timeout_sec`` x the maximum number of outbound attempts
# the stack can spend on one extraction, plus slack.
#   attempts: llm.call_llm_api_with_retry (retries=3) + the extractor's
#   empty-reply re-ask + the "auto" mode classification round-trip + 1 spare.
_SINGLE_RUN_MAX_ATTEMPTS = 6
_SINGLE_RUN_DEADLINE_SLACK_SEC = 15


# S8 fix: redact API-key-like patterns from error_body before echoing to the UI.
# Upstream servers may echo back request headers (including Authorization)
# or query params. We redact common key formats to prevent accidental leakage.
#
# REVIEW-2026-09-10: the value character class excluded ".", "+", "/" and "="
# — exactly the characters a JWT or base64 key is made of — so
# "x-api-key=sk-abc.defghijklmnop" was left untouched and only the FIRST
# segment of a Bearer JWT was redacted (payload + signature survived). The
# prefix alternation also missed this repo's own third-party presets
# ("ccs-...", "pk-...") and the Google/AWS fixed formats. Expanded both.
_API_KEY_RE = re.compile(
    r"(?:sk-|ccs-|pk-|Bearer\s+|x-api-key\s*[:=]\s*|api[_-]?key\s*[:=]\s*)"
    r"[A-Za-z0-9._+\-/=]{8,}"
    r"|AIza[0-9A-Za-z_\-]{20,}"
    r"|AKIA[0-9A-Z]{16}",
    re.IGNORECASE,
)


def _redact_error_body(body):
    """Remove API-key-like tokens from a string before sending to the client."""
    if not isinstance(body, str):
        return ""
    # REVIEW-2026-09-10: the pattern no longer has a capture group (the
    # alternation covers whole tokens), so replace the whole match. Keeping
    # the old `\1` reference would raise "invalid group reference" on every
    # call.
    return _API_KEY_RE.sub('[REDACTED]', body)


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


# REVIEW-2026-09-10: pixel/edge caps for the enhancement path. The client
# downscales to maxImageEdge (js/config.js, default 4000) before upload, so a
# larger image is either an unusual client or a decompression bomb; either way
# the enhancement must not pay for it.
_MAX_ENHANCE_EDGE = 4000
_MAX_ENHANCE_PIXELS = 16_000_000


def _raw_text(entry) -> str:
    """Raw text from a per-run raw entry.

    REVIEW-2026-09-10: the multi-run pipeline records
    ``{"run_idx": <slot>, "text": <raw>}`` so the audit table can label each
    reply with its sampling slot; single-run callers still pass a bare string.
    """
    if isinstance(entry, dict):
        return entry.get("text", "") or ""
    return entry or ""


def _safe_confidence(result) -> float:
    """Best-effort numeric confidence of an ExtractResult for the audit row.

    REVIEW-2026-09-20: ``float((result.data or {}).get("confidence", 0.0))``
    assumed ``result.data`` is a dict. A provider that answers with a JSON
    array / string / number at the root (or an ``{"confidence": "high"}``) made
    the call raise AttributeError/TypeError/ValueError - inside
    ``_write_history_record`` that aborted the whole audit write (swallowed by
    its blanket except), so the extraction silently vanished from history.
    Any unusable value degrades to 0.0 and the record still gets written.
    """
    data = getattr(result, "data", None)
    if not isinstance(data, dict):
        return 0.0
    try:
        value = data.get("confidence", 0.0)
        if value is None:
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _validate_provider_fields(provider_raw: dict) -> str:
    """Type-check the provider sub-fields the request path dereferences.

    REVIEW-2026-09-10: ``provider: {"endpoint": 5}`` raised AttributeError
    inside rca_core/ssrf.py (whose try only catches ValueError), and
    ``"extra_headers": "oops"`` raised AttributeError at the
    ``.items()`` call in _stable_extra_headers. Both escaped do_POST, so the
    socket closed with no response at all. Returns an error message, or ""
    when the fields are usable.
    """
    endpoint = provider_raw.get("endpoint")
    if endpoint is not None and not isinstance(endpoint, str):
        return "field 'provider.endpoint' must be a string"
    api_key = provider_raw.get("api_key")
    if api_key is not None and not isinstance(api_key, str):
        return "field 'provider.api_key' must be a string"
    model = provider_raw.get("model")
    if model is not None and not isinstance(model, str):
        return "field 'provider.model' must be a string"
    for field in ("extra_headers", "extra_body"):
        value = provider_raw.get(field)
        if value is not None and not isinstance(value, dict):
            return f"field 'provider.{field}' must be an object or null"
    return ""


def _constrain_resolved_mode(mode: str, mode_source: str) -> tuple[str, str]:
    """Keep an auto-resolved mode inside the wired set.

    REVIEW-2026-09-10: the request whitelist only inspects the REQUESTED
    mode, so ``mode: "auto"`` was a way around it - the vision classifier can
    return chemical_stratigraphy / paleomap / scatter_plot
    (rca_core/extractor.KNOWN_CHART_TYPES), which have no MergeSchema and no
    exporter tables. ``merge_results`` then fell back to RANGE_CHART_SCHEMA
    and dropped every mode-specific row (continents, fossil_sites,
    data_points) while still returning ``ok=True`` - precisely the "WRONG
    merge schema with ok=True" failure the whitelist documents itself as
    preventing. The resolved mode now gets the same rule applied, falling
    back to range_chart (the documented default) and marking the source so
    the report and UI show that the fallback happened.

    Returns ``(mode, mode_source)``.
    """
    if mode not in WIRED_MODES:
        return "range_chart", "auto-fallback"
    return mode, mode_source


# Sprint B (REVIEW-2026-09-04) #12: the web UI sends ``enhance: true`` in
# the POST body when the user opts into image pre-processing, but the
# server silently dropped the field. This is the server-side equivalent of
# the GUI path's Pillow enhancement (reference: rca_core/extractor.py
# ``_enhance_image_pil`` — unsharp mask + gentle contrast boost; server.py
# must not import gui.py). Best-effort: any failure falls back to the
# original bytes so enhancement can never break an extraction.
def _enhance_image_b64_with_mime(image_b64: str) -> tuple[str, str]:
    """Return ``(image_b64, media_type)`` for the enhanced image.

    REVIEW-2026-09-10: the enhancement always re-encodes as PNG, but the
    caller kept forwarding the client's declared media_type - so a JPEG at or
    under the client's resize threshold was uploaded as "image/jpeg" while the
    bytes were PNG. Providers that validate the declared type against the
    payload reject that (the GUI path and the JS resize path both return the
    new mime alongside the bytes). ``media_type`` is "" when the input was
    returned unchanged, so the caller keeps its own value.

    Applies a light unsharp mask + contrast boost with Pillow's
    ImageEnhance/ImageFilter (same recipe as the GUI path). Pillow missing,
    undecodable bytes, or a re-encode that would blow past the request size
    budget all return the original input unchanged.
    """
    try:
        import io
        from PIL import Image, ImageEnhance, ImageFilter  # type: ignore
    except Exception:
        return image_b64, ""
    try:
        raw = base64.b64decode(image_b64, validate=True)
        img = Image.open(io.BytesIO(raw))
        # REVIEW-2026-09-10 (decompression bomb): _validate_image_b64 caps the
        # DECODED BYTE COUNT (10 MB) but not the pixel count, and Pillow only
        # emits a warning below 2x its MAX_IMAGE_PIXELS. A 151 KB PNG
        # expanding to 10000x10000 (100 Mpx) was decoded, unsharp-masked and
        # re-encoded here in ~1.3 s; the worst case is hundreds of MB of
        # intermediate buffers per request, multiplied by the handler pool.
        # Refuse anything larger than the GUI's own edge cap (DEFAULT_MAX_EDGE,
        # 4000 px) so a small upload cannot burn memory before the resize
        # stage the client is supposed to have applied.
        width, height = img.size
        if width * height > _MAX_ENHANCE_PIXELS or max(width, height) > _MAX_ENHANCE_EDGE:
            return image_b64, ""
        # Unsharp mask sharpens thin lines and small species names.
        img = img.filter(ImageFilter.UnsharpMask(radius=2, percent=80, threshold=3))
        # Gentle contrast boost helps faint pencil lines stand out.
        img = ImageEnhance.Contrast(img).enhance(1.15)
        out = io.BytesIO()
        img.save(out, format="PNG")
        enhanced = base64.b64encode(out.getvalue()).decode("ascii")
        # Keep the payload inside the same budget _validate_image_b64
        # enforces (~4/3 of the 10 MB decoded cap); otherwise prefer the
        # original image over a ballooned PNG re-encode.
        if len(enhanced) > 14_000_000:
            return image_b64, ""
        return enhanced, "image/png"
    except Exception:
        return image_b64, ""


def _enhance_image_b64(image_b64: str) -> str:
    """Bytes-only wrapper around :func:`_enhance_image_b64_with_mime`."""
    return _enhance_image_b64_with_mime(image_b64)[0]


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
    resolve_auto_mode,
)
from rca_core.report import build_extraction_report  # noqa: E402
from rca_core.aggregate import (  # noqa: E402
    COLUMNAR_SECTION_SCHEMA,
    RANGE_CHART_SCHEMA,
    SCHEMA_BY_MODE,
    WIRED_MODES,
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


# Sprint B (REVIEW-2026-09-04) #3: the lazy singleton used a bare
# ``try: ... except NameError`` pattern, which is not thread-safe — two
# request threads could race past the check and each construct a
# HistoryStore (each opening its own sqlite connection). Guard with a
# module-level lock (double-checked style, mirroring rca_core.cache.
# get_cache()). The cache variable is declared up-front so there is no
# NameError path at all.
_HISTORY_STORE_SINGLETON_CACHE: "HistoryStore | None" = None
_history_store_lock = threading.Lock()


def _history_store_singleton():
    """Return a HistoryStore if history is available, else None.

    Created lazily to avoid a hard sqlite dependency at server import.
    Cached at module level so every extraction hits the same db.
    """
    global _HISTORY_STORE_SINGLETON_CACHE
    if not _HAS_HISTORY:
        return None
    with _history_store_lock:
        if _HISTORY_STORE_SINGLETON_CACHE is None:
            try:
                _HISTORY_STORE_SINGLETON_CACHE = HistoryStore(db=Database())
            except Exception:
                return None
        return _HISTORY_STORE_SINGLETON_CACHE


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
        #
        # REVIEW-2026-09-20: when NEITHER is available the old code hashed the
        # EMPTY string, producing the e3b0c442... (sha256 of b"") sentinel.
        # HistoryStore.get_by_sha256 then grouped every such record as "the
        # same image", so an audit query for one figure returned unrelated
        # extractions. A missing fingerprint is now recorded as missing (the
        # column is nullable) instead of as a bogus shared digest.
        sha = getattr(result, "image_sha256", "") or ""
        if not sha:
            _audit_b64 = getattr(result, "_image_b64_for_audit", "") or ""
            if _audit_b64:
                sha = compute_image_sha256_from_b64(_audit_b64)
            else:
                meta["image_sha256_missing"] = True
        meta.setdefault("image_sha256", sha)
        meta.setdefault("runs", runs)

        # Build per-run raw_responses entries.
        raw_responses = []
        if raws:
            for i, entry in enumerate(raws):
                # REVIEW-2026-09-10: entries may carry the sampling SLOT index
                # ({'run_idx': n, 'text': ...}); a bare string is the legacy
                # single-run call shape.
                slot_idx = entry.get("run_idx", i) if isinstance(entry, dict) else i
                txt = entry.get("text", "") if isinstance(entry, dict) else entry
                if not txt:
                    continue
                raw_responses.append({
                    "run_idx": slot_idx,
                    "raw_text": txt,
                    "prompt_text": "",
                    "request_meta": {**meta, "run_idx": slot_idx},
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
            confidence=_safe_confidence(result),
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
    "favicon.ico",
    # REVIEW-2026-09-20: "references" (604 MB of research-paper PDFs and
    # markdown notes, vendored for the docs/ pipeline) used to be on this list.
    # Nothing in the web frontend, in app/ (the PyWebView loading screen) or in
    # gui_fluent_history_detail.py's embedded HTML requests it — verified by
    # grepping every JS/HTML/PY file for a ``/references`` URL — so serving it
    # only turned the same-origin static handler into a bulk file download of
    # non-app content (and one paper PDF is larger than the 50 MB guard, so the
    # handler paid a getsize + reject round-trip for nothing).
)

# Expected host allowlist — populated when the server starts based on
# --host / --port (or the app.py picked port). The CSRF Origin/Referer
# check compares the request's origin netloc against this set instead
# of the client-controlled ``Host`` header (which a DNS-rebinding
# attacker can spoof).
EXPECTED_HOSTS: set[str] = set()


def _host_forms(host: str, port: int) -> "list[str]":
    """netloc shapes a browser may present as its Origin for *host*:*port*.

    REVIEW-2026-09-20: only ``host:port`` was registered. A page loaded from
    the default port sends ``Origin: http://192.168.1.20`` — netloc WITHOUT a
    port — which never matched the allowlist, so every extraction 403'd. The
    port-less form is only added when it is genuinely the default port (80),
    so a mismatched ``http://host`` Origin cannot reach a server on 8000.
    """
    bare = f"[{host}]" if ":" in host else host
    forms = [f"{bare}:{port}"]
    if port == 80:
        forms.append(bare)
    return forms


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

    REVIEW-2026-09-20: the set is CLEARED first and every entry is added in
    both its ``host:port`` and (default-port only) port-less form. Previously
    the set only ever GREW, so a second ``populate_expected_hosts`` call on a
    different port (app.py probing 8000 then 8765, a test suite that starts
    several servers, a re-bind after ``--port`` reselection) left every
    earlier port allowlisted forever — the allowlist silently accumulated
    addresses the process no longer serves.
    """
    host = (host or "").strip()
    EXPECTED_HOSTS.clear()
    for alias in ("localhost", "127.0.0.1", "::1"):
        EXPECTED_HOSTS.update(_host_forms(alias, port))
    if host and host not in ("0.0.0.0", "::", ""):
        EXPECTED_HOSTS.update(_host_forms(host, port))
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
            EXPECTED_HOSTS.update(_host_forms(ip, port))


def is_loopback_bind(host: str) -> bool:
    """True when *host* binds loopback only (the safe default)."""
    host = (host or "").strip().strip("[]")
    if host in ("", "localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _is_loopback_addr(addr: str) -> bool:
    """True for the peer address of a connection from this very machine.

    Handles the IPv4-mapped IPv6 spelling (``::ffff:127.0.0.1``) a dual-stack
    ``ThreadingHTTPServer`` reports for loopback v4 clients, and the
    ``fe80::1%eth0`` scope-id form. Non-parseable addresses are NOT loopback
    (fail closed).
    """
    bare = (addr or "").split("%", 1)[0].strip("[]")
    try:
        ip = ipaddress.ip_address(bare)
    except ValueError:
        return False
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        return mapped.is_loopback
    return ip.is_loopback


# CSRF token storage: thread-safe dict mapping session tokens to CSRF tokens.
# In a production system you'd use a proper session store (Redis, DB, etc.).
# For this single-server application, an in-memory dict with a lock suffices.
_csrf_lock = threading.RLock()
_csrf_store: dict[str, tuple[str, float]] = {}  # session_token -> (csrf_token, last_seen_at)
_CSRF_TOKEN_TTL_SEC = 3600  # 1 hour


def _generate_csrf_token() -> str:
    """Generate a cryptographically random CSRF token."""
    return secrets.token_urlsafe(32)


def _get_csrf_for_session(session_token: str) -> str | None:
    """Retrieve the CSRF token for a given session, or None if not found or expired.

    REVIEW-2026-09-20: a successful lookup SLIDES the expiry forward (the stored
    timestamp is the last validated use, not the mint time). With the previous
    absolute TTL a page left open across the hour boundary failed its NEXT
    extraction with a bare 403 - and the client cannot silently repair it,
    because the token mint is a separate request the caller has already moved
    past. Sliding keeps an actively used session alive while an abandoned token
    still dies after the same idle period, which is the property the TTL exists
    for. The token is deliberately NOT consumed (single-use): the web client
    fires several parallel extractions with the same pair, so one-shot tokens
    would need a per-request mint the frontend does not implement.
    """
    with _csrf_lock:
        entry = _csrf_store.get(session_token)
        if entry is None:
            return None
        csrf_token, last_seen = entry
        now = time.time()
        if now - last_seen > _CSRF_TOKEN_TTL_SEC:
            _csrf_store.pop(session_token, None)
            return None
        # Slide: cheap dict write, keeps the entry from expiring mid-session.
        _csrf_store[session_token] = (csrf_token, now)
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
        # REVIEW-2026-09-20: the timestamp is now "last validated use" (see
        # _get_csrf_for_session), so this sweep evicts IDLE-out tokens.
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


def _rate_bucket(addr: str) -> str:
    """Map a socket address to its rate-limit key.

    REVIEW-2026-09-20: an IPv6 client owns at least a /64 by RFC 4291 and a
    privacy-extension host rotates its interface identifier every few hours
    (RFC 8981), so keying the sliding window on the FULL address let one
    machine mint a fresh 30-req/min budget per temporary address - the limit
    protected nobody. IPv6 keys are now aggregated to their /64 (the smallest
    allocation a site can receive); IPv4 stays exact, where a shared CGNAT
    /24 could otherwise punish innocent neighbours.
    """
    if not addr:
        return "unknown"
    # `client_address` may carry a scope id (fe80::1%eth0) - not part of the key.
    bare = addr.split("%", 1)[0].strip("[]")
    if ":" in bare:
        try:
            ip = ipaddress.ip_address(bare)
        except ValueError:
            return bare
        v4 = ip.ipv4_mapped
        if v4 is not None:
            return str(v4)
        try:
            return str(ipaddress.ip_network(f"{ip}/64", strict=False))
        except ValueError:  # pragma: no cover - defensive
            return str(ip)
    return bare


def _check_rate_limit(ip: str, bucket: str = "",
                      max_requests: int = _RATE_MAX_REQUESTS) -> tuple[bool, int]:
    """Return (allowed, seconds_until_reset). Sliding window per (bucket, IP).

    ``bucket`` separates the budgets of endpoints with different costs (the
    free CSRF-mint GET must not starve the paid POST); ``max_requests`` is the
    cap for that bucket.
    """
    now = time.time()
    key = _rate_bucket(ip)
    if bucket:
        key = f"{bucket}:{key}"
    with _rate_lock:
        # REVIEW-2026-09-10: an entry used to be deleted only when THAT SAME
        # ip returned after its window had slid fully empty, so a one-shot
        # source (or a rotating IPv6 /64) left a permanent entry and the map
        # grew without bound - the opposite of what the comment below claims.
        # Sweep fully-expired entries once the map is large enough to matter.
        if len(_rate_history) > _RATE_HISTORY_SWEEP_AT:
            cutoff_sweep = now - _RATE_WINDOW_SEC
            stale = [k for k, w in _rate_history.items()
                     if not w or w[-1] < cutoff_sweep]
            for k in stale:
                _rate_history.pop(k, None)
        window = _rate_history.get(key)
        if window is None:
            _rate_history[key] = collections.deque([now], maxlen=max_requests)
            return True, 0
        cutoff = now - _RATE_WINDOW_SEC
        while window and window[0] < cutoff:
            window.popleft()
        # Sprint B (REVIEW-2026-09-04) #7: when the window has slid fully
        # empty, recycle the entry so ``_rate_history`` cannot grow without
        # bound across many distinct (or rotating) client IPs.
        if not window:
            del _rate_history[key]
            return True, 0
        if len(window) >= max_requests:
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

# REVIEW-2026-09-20: static files are streamed in chunks of this size instead
# of being slurped into one bytes object (see do_GET).
_STATIC_CHUNK_BYTES = 64 * 1024

# REVIEW-2026-09-20: global memory budget. MAX_BODY_BYTES caps ONE request, and
# _BoundedThreadingHTTPServer caps one batch of connections at
# ``max_workers`` (default 32), so 32 simultaneous 20 MB uploads = 640 MB of
# raw bytes plus their base64/pillow decodes and enhancement copies - far past
# what the 2 GB target machines of this project tolerate, and a cheap
# self-DoS. Requests whose declared body is above the threshold below must hold
# a token from this counting semaphore for the whole time their bytes are
# resident (read -> decode -> enhance -> extract); the 5th concurrent big
# upload gets a structured 503 instead of pushing the process into swap.
_BIG_BODY_BYTES = 5 * 1024 * 1024
_BIG_BODY_CONCURRENCY = 4
_BIG_BODY_SLOTS = threading.BoundedSemaphore(_BIG_BODY_CONCURRENCY)


def _acquire_big_body_slot(length: int) -> bool:
    """Take a memory-budget token for a large body. Never blocks.

    Returns True when the caller holds a token (and must release it), False
    when the request should be rejected with 503. Small bodies return True
    immediately without consuming a token (see the paired release helper).
    """
    if length <= _BIG_BODY_BYTES:
        return True
    return _BIG_BODY_SLOTS.acquire(blocking=False)


def _release_big_body_slot(length: int) -> None:
    """Give back the token taken by :func:`_acquire_big_body_slot`."""
    if length > _BIG_BODY_BYTES:
        try:
            _BIG_BODY_SLOTS.release()
        except ValueError:
            # Cannot happen (one acquire per release) but never let a
            # bookkeeping slip take down a handler thread.
            pass


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


def _cache_singleton_safe():
    """Return the shared result cache, or None when it cannot be opened.

    REVIEW-2026-09-20: ``get_cache()`` itself was never wrapped, only its
    ``.get()``/``.put()`` calls. That is backwards: ``ResultCache()`` opens (and
    runs DDL against) ``~/.range_chart_analyzer/extract_cache.sqlite`` inside the
    constructor, so a corrupt DB, a read-only HOME, or a full disk raised out of
    ``get_cache()`` BEFORE any handler-level try covered it:

      * single-run: /api/extract answered nothing at all (the connection closed
        mid-request) for EVERY extraction, forever;
      * multi-run: same, and the pre-existing ``try: cache = get_cache()`` at
        the prefill site was unreachable dead code because the key-building
        loop above it already raised.

    The cache is an optimisation: failing to open it must degrade to "always
    miss", never to a broken connection.
    """
    try:
        from rca_core.cache import get_cache
        return get_cache()
    except Exception as exc:  # noqa: BLE001 - any cache failure = no cache
        try:
            sys.stderr.write(
                f"[cache] unavailable, continuing without cache: "
                f"{type(exc).__name__}: {exc}\n")
        except Exception:
            pass
        return None


def _spawn_extract_thread(mode: str, common: dict,
                         label: str = "extract") -> concurrent.futures.Future:
    """Run ``extract`` in a DAEMON thread and return its Future.

    REVIEW-2026-09-20: the multi-run fan-out used a per-request
    ``ThreadPoolExecutor``. Since CPython 3.9 its workers are NON-daemon and
    ``concurrent.futures.thread`` registers an atexit hook that JOINS them, so
    after Ctrl+C the interpreter waited for every in-flight (and every still
    QUEUED, the queue is FIFO ahead of the sentinel) LLM call - worst case
    several hundred seconds during which the console looks hung, exactly the
    window in which a researcher presses Ctrl+C twice and files a "server
    won't die" bug.

    The futures returned here are plain ``concurrent.futures.Future`` objects,
    so ``concurrent.futures.wait(...)`` / ``.result()`` at the call sites keep
    working unchanged - only the thread behind them becomes a daemon, which
    ``python server.py`` + Ctrl+C can then abandon. A request in flight is lost
    on exit; that was already true (the client socket goes away with the
    process).
    """
    fut: "concurrent.futures.Future[ExtractResult]" = concurrent.futures.Future()

    def _worker():
        if not fut.set_running_or_notify_cancel():
            return  # cancelled before we started
        try:
            # Resolve `extract` from module globals at CALL time so tests (and
            # the GUI embedding) can keep monkeypatching ``server.extract``.
            result = extract(mode=mode, **common)
        except BaseException as exc:  # noqa: BLE001 - re-raised via the future
            fut.set_exception(exc)
        else:
            fut.set_result(result)

    t = threading.Thread(target=_worker, name=f"rca-{label}", daemon=True)
    t.start()
    return fut


class Handler(BaseHTTPRequestHandler):
    server_version = "RangeChartAnalyzer/1.0"

    # Slowloris / slow-read DoS guard: BaseHTTPRequestHandler applies this
    # to the connection socket in setup(), so a client that opens a
    # connection (or declares a large Content-Length) then sends bytes at a
    # trickle is disconnected after `timeout` seconds of inactivity instead
    # of pinning a worker thread indefinitely. 60s is generous for a normal
    # request line + headers + JSON body upload.
    timeout = 60

    # REVIEW-2026-09-10: `timeout` is a per-recv INACTIVITY timeout (it maps
    # to socket.settimeout), not a request deadline. A client that sends one
    # byte just inside each interval keeps the buffered rfile.read() alive
    # indefinitely - 30 such connections (one rate-limit window, so no 429)
    # would pin 30 of the pool's 32 worker threads at a cost of ~30 bytes per
    # minute and starve every legitimate request. The body read therefore
    # enforces its own wall-clock deadline.
    _BODY_DEADLINE_SEC = 60

    def _read_body_with_deadline(self, length: int):
        """Read exactly ``length`` body bytes, or None past the deadline.

        Returns the bytes, or None when the upload did not complete within
        ``_BODY_DEADLINE_SEC`` of wall-clock time (the caller answers 408).

        Two details make this a real deadline rather than another inactivity
        timer:
          * ``rfile.read(n)`` blocks until it has all n bytes, so a trickle
            would sit inside a single call past every check - use ``read1``,
            which returns as soon as one chunk is available.
          * the socket timeout is narrowed to the remaining budget before each
            read, so a client that sends nothing at all is cut off at the
            deadline instead of at the (much longer) per-recv ``timeout``.
        """
        deadline = time.monotonic() + self._BODY_DEADLINE_SEC
        reader = getattr(self.rfile, "read1", None) or self.rfile.read
        chunks = []
        remaining = length
        try:
            while remaining > 0:
                left = deadline - time.monotonic()
                if left <= 0:
                    return None
                try:
                    self.connection.settimeout(max(0.05, min(left, 5.0)))
                except OSError:
                    pass
                try:
                    chunk = reader(min(remaining, 64 * 1024))
                except (socket.timeout, TimeoutError):
                    continue  # deadline check at the top decides
                except (OSError, ValueError):
                    return None
                if not chunk:
                    return None
                chunks.append(chunk)
                remaining -= len(chunk)
        finally:
            try:
                self.connection.settimeout(self.timeout)
            except OSError:
                pass
        return b"".join(chunks)

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
            # Apply a per-IP rate limit so a single client cannot mint
            # unbounded CSRF tokens (LOW: unauthenticated GET was previously
            # uncapped).
            #
            # REVIEW-2026-09-20: this used to consume the SAME bucket as the
            # extraction POST, so any web page the victim visited could drive
            # 30 free GETs per minute from their browser and every real
            # extraction that followed got 429 - a cross-site denial of service
            # with no CSRF token, no auth and no privileges needed. The mint now
            # has its OWN bucket key ("get:"+ip) with a higher cap, which both
            # keeps the token-flood bound and removes the cross-endpoint
            # starvation. (The alternative — requiring a custom header on the
            # GET — was rejected because it needs a js/minimax.js change; see
            # the report.)
            try:
                client_ip = self.client_address[0]
            except Exception:
                client_ip = "unknown"
            allowed, wait_sec = _check_rate_limit(
                client_ip, bucket="get",
                max_requests=_RATE_MAX_REQUESTS_GET)
            if not allowed:
                self._send_json(429, {
                    "ok": False,
                    "error_key": "err.rateLimit",
                    "error_body": f"Rate limit exceeded. Retry after {wait_sec} seconds.",
                })
                return
            session_token = self.headers.get("X-Session-Token", "")
            # REVIEW-2026-09-10: a client-supplied token becomes the store key
            # verbatim and is echoed back, so an over-long or oddly-shaped
            # value (a header can be ~64 KB) would be retained for the whole
            # TTL and multiplied by the store cap. Only reuse a value shaped
            # like the ones this server mints; otherwise issue a fresh one.
            if (not session_token
                    or len(session_token) > _CSRF_SESSION_TOKEN_MAX_LEN
                    or not _SESSION_TOKEN_RE.fullmatch(session_token)):
                # Generate a new session token if none (or an unusable one).
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
        #
        # Sprint B (REVIEW-2026-09-04) #2: this endpoint previously had NO
        # guards at all — any process that could reach the port could dump
        # the whole audit trail — and it built a fresh ``Database()`` per
        # request (running executescript DDL + commit every time, on a
        # thread-shared file). It now (a) requires the custom
        # ``X-RCA-Client`` header (blocks browser cross-origin reads via
        # preflight + trivial scanners), (b) shares the per-IP sliding
        # window rate limiter, and (c) reuses the thread-safe
        # ``_history_store_singleton()`` instead of per-request DDL.
        from urllib.parse import urlparse as _urlparse
        _parsed_path = _urlparse(self.path).path
        _prov_match = _PROVENANCE_ID_RE.match(_parsed_path)
        if _prov_match:
            client_header = (
                self.headers.get(_PROVENANCE_CLIENT_HEADER) or ""
            ).strip()
            if client_header != _PROVENANCE_CLIENT_VALUE:
                self._send_json(403, {"error": "forbidden"})
                return
            try:
                client_ip = self.client_address[0]
            except Exception:
                client_ip = "unknown"
            # REVIEW-2026-09-20: ``X-RCA-Client`` is a constant published in the
            # source, so it stops browsers/casual scanners but is NOT
            # authentication: on a ``--host 0.0.0.0`` deployment every machine
            # on the LAN could enumerate the audit trail (image SHA-256s,
            # provider endpoints and model names, per-slot LLM replies). The
            # caller must now also be loopback, or present the
            # ``RCA_PROVENANCE_TOKEN`` shared secret.
            configured_token = (os.environ.get(_PROVENANCE_TOKEN_ENV) or "").strip()
            provided_token = (self.headers.get(_PROVENANCE_TOKEN_HEADER) or "").strip()
            if configured_token:
                if not provided_token or not secrets.compare_digest(
                        provided_token.encode("utf-8"),
                        configured_token.encode("utf-8")):
                    self._send_json(403, {"error": "forbidden"})
                    return
            elif not _is_loopback_addr(client_ip):
                self._send_json(403, {
                    "error": "forbidden",
                    "hint": f"provenance is loopback-only unless "
                            f"{_PROVENANCE_TOKEN_ENV} is set",
                })
                return
            # Its own rate bucket too (same starvation argument as the mint GET).
            allowed, wait_sec = _check_rate_limit(
                client_ip, bucket="prov",
                max_requests=_RATE_MAX_REQUESTS_PROV)
            if not allowed:
                self._send_json(429, {
                    "ok": False,
                    "error_key": "err.rateLimit",
                    "error_body": f"Rate limit exceeded. Retry after {wait_sec} seconds.",
                })
                return
            store = _history_store_singleton()
            if store is None:
                self._send_json(503, {"error": "history store unavailable"})
                return
            # REVIEW-2026-09-20: the pattern caps the id at 12 digits and the
            # conversion is guarded, so a hostile id can neither raise
            # ValueError out of do_GET (no response at all, socket closed) nor
            # be an unbounded-length enumeration probe.
            try:
                record_id = int(_prov_match.group(1))
            except ValueError:
                self._send_json(404, {"error": "not found"})
                return
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
        #
        # REVIEW-2026-09-20: the body used to be ``f.read()`` into one bytes
        # object *and* written in one shot, so N concurrent clients each pinned
        # a up-to-50 MB buffer (plus a copy in ``wfile``); the size also came
        # from a separate ``os.path.getsize()`` that could race the read. The
        # file is now opened first (size from ``os.fstat`` of that fd, so no
        # TOCTOU) and streamed to the socket with ``shutil.copyfileobj`` in
        # 64 KB chunks, which caps the per-request memory at the chunk size
        # regardless of the file size.
        try:
            _fh = open(target, "rb")
        except OSError:
            self._send_json(500, {"error": "read error"})
            return
        try:
            with _fh as f:
                try:
                    file_size = os.fstat(f.fileno()).st_size
                except OSError:
                    self._send_json(500, {"error": "read error"})
                    return
                if file_size > 50 * 1024 * 1024:
                    self._send_json(413, {"error": "file too large"})
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
                self.send_header("Content-Length", str(file_size))
                self.end_headers()
                shutil.copyfileobj(f, self.wfile, _STATIC_CHUNK_BYTES)
        except (BrokenPipeError, ConnectionResetError, OSError):
            # A client that hangs up mid-download is not an error worth a
            # traceback; ``handle_one_request`` swallows the same classes.
            return

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
        # Sprint B (REVIEW-2026-09-04) #6: compare_digest(str, str) raises
        # TypeError on non-ASCII input (header values decode as latin-1, so
        # a client sending e.g. "café" crashed the handler and dropped the
        # connection). Compare bytes so any input yields a clean 403.
        if stored_csrf is None or not secrets.compare_digest(
                csrf_token.encode("utf-8"), stored_csrf.encode("utf-8")):
            self._send_json(403, {
                "ok": False, "error_key": "err.forbidden",
                "error_body": "Invalid or expired CSRF token.",
            })
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        # REVIEW-2026-09-10: a missing or zero Content-Length (an empty body,
        # or a chunked-encoding client that sends none) reported
        # "Content-Length 0 exceeds limit of 20971520", which is both false
        # and impossible to act on. Distinguish the two cases.
        if length <= 0:
            self._send_json(411, {
                "ok": False,
                "error_key": "err.badRequest",
                "error_body": ("Content-Length header is required and must be "
                               "greater than zero (chunked transfer encoding "
                               "is not supported)."),
            })
            return
        if length > MAX_BODY_BYTES:
            self._send_json(413, {
                "ok": False,
                "error_key": "err.bodyTooLarge",
                "error_body": f"Content-Length {length} exceeds limit of {MAX_BODY_BYTES}",
            })
            return

        # REVIEW-2026-09-20: global memory budget (item 9). A large upload costs
        # the server its bytes several times over (raw body -> decoded JPEG ->
        # PIL image -> base64 of the enhanced copy), and neither MAX_BODY_BYTES
        # (one request) nor the bounded thread pool (connections) bounded the
        # SUM across concurrent requests, so ~40 simultaneous 20 MB posts were
        # enough to drive a 2 GB box into swap. Big bodies must now hold a token
        # from the counting semaphore for the whole read/decode/enhance/extract
        # span, and the loser gets a structured 503 instead of an OOM.
        #
        # The token is taken AFTER rate limiting + CSRF + size validation on
        # purpose: unauthenticated or malformed traffic must never be able to
        # consume the budget (that would turn a memory guard into a trivial
        # extraction outage). Small bodies short-circuit the semaphore.
        if not _acquire_big_body_slot(length):
            self._send_json(503, {
                "ok": False,
                "error_key": "err.serverBusy",
                "error_body": ("Server is at its concurrent large-upload limit "
                               "(%d). Retry shortly." % _BIG_BODY_CONCURRENCY),
            })
            return
        try:
            self._handle_extract_body(length)
        finally:
            _release_big_body_slot(length)

    def _handle_extract_body(self, length: int) -> None:
        """Read, validate and run one /api/extract request.

        Split out of :meth:`do_POST` so the big-body memory token taken there
        can be released in a ``finally`` without re-indenting the whole handler.
        """
        try:
            raw = self._read_body_with_deadline(length)
            if raw is None:
                # REVIEW-2026-09-10: a trickled body is dropped, not parked.
                # See _read_body_with_deadline.
                self._send_json(408, {
                    "ok": False,
                    "error_key": "err.timeout",
                    "error_body": "request body not received within the upload deadline",
                })
                return
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
            # UI-REVIEW-2026-09-05: radiolarian biozonation / correlation
            # charts — wired through the full stack (MergeSchema, quality,
            # exporter tables, GUI + web entry points).
            'zonation_chart',
            # UI-REVIEW-2026-09-07: "auto" resolves server-side — caption
            # keyword heuristic first, vision classification as fallback —
            # before the runs loop dispatches to the concrete mode.
            'auto',
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
            # REVIEW-2026-09-10: only the TOP-LEVEL fields were type-checked
            # (below); a nested `endpoint`, `extra_headers` or `extra_body` of
            # the wrong type reached urlparse / dict.update and raised
            # AttributeError or ValueError out of do_POST, so the connection
            # closed with no response at all. Validate the nested fields the
            # provider is actually built from and reject with a clear 400.
            _nested_error = _validate_provider_fields(provider_raw)
            if _nested_error:
                self._send_json(400, {
                    "ok": False,
                    "error_key": "err.parse",
                    "error_body": _nested_error,
                })
                return
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
        # REVIEW-2026-09-10: OverflowError is NOT a TypeError/ValueError
        # subclass, and json.loads maps an oversized JSON number (1e400) to
        # float('inf'), so `int(...)` on such a field raised straight out of
        # do_POST: the socket closed with NO response at all and the client
        # saw a generic network error, breaking the documented "always JSON
        # with ok/error_key" contract.
        except (TypeError, ValueError, OverflowError):
            max_tokens = DEFAULT_MAX_TOKENS

        try:
            runs = int(req.get("runs") or 1)
        except (TypeError, ValueError, OverflowError):
            runs = 1
        runs = max(1, min(runs, 5))

        try:
            timeout_sec = int(req.get("timeout_sec") or DEFAULT_TIMEOUT_SEC)
        except (TypeError, ValueError, OverflowError):
            timeout_sec = DEFAULT_TIMEOUT_SEC
        timeout_sec = max(_MIN_EXTRACT_TIMEOUT_SEC,
                          min(timeout_sec, _MAX_EXTRACT_TIMEOUT_SEC))

        mode = _requested_mode
        mode_source = ""
        if mode == "auto":
            # UI-REVIEW-2026-09-07: resolve "auto" ONCE per request (not per
            # run): caption keyword heuristic first; when nothing matches,
            # classify the image itself with the cheap vision classifier.
            # The resolved mode then drives cache keys, extraction prompt,
            # merge schema and quality scoring for every run.
            caption_txt = req.get("caption") or ""
            mode, classify_result = resolve_auto_mode(
                caption=caption_txt,
                filename=str(req.get("source_file") or ""),
                image_b64=image_b64,
                media_type=req.get("media_type") or "image/png",
                provider=provider,
                # Honor the caller's timeout for the classification round-trip
                # too (REVIEW-2026-09-10).
                timeout_sec=timeout_sec,
            )
            mode_source = "vision" if classify_result is not None else "text"
            mode, mode_source = _constrain_resolved_mode(mode, mode_source)

        # Sprint B (REVIEW-2026-09-04) #12: honour the optional ``enhance``
        # flag from the web UI (it was previously dropped silently). The
        # enhancement runs BEFORE the cache key is derived so enhanced and
        # un-enhanced variants of the same upload never share a cache
        # entry. Pillow missing or any enhancement failure falls back to
        # the original image silently.
        if req.get("enhance"):
            # The re-encode is PNG, so the declared media type must follow the
            # bytes (see _enhance_image_b64_with_mime).
            _enhanced_b64, _enhanced_mime = _enhance_image_b64_with_mime(image_b64)
            image_b64 = _enhanced_b64
            if _enhanced_mime:
                req = dict(req)
                req["media_type"] = _enhanced_mime

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
            # REVIEW-2026-09-20 (item 1): ``get_cache()`` runs DDL against
            # ~/.range_chart_analyzer/extract_cache.sqlite in its constructor, so
            # the unguarded call below raised BEFORE any handler-level try could
            # cover it and /api/extract closed the connection for every single-run
            # request. Cache access now goes through the guarded singleton helper,
            # ``cache is None`` skips all key/get/put work, and the request
            # degrades to "always miss".
            cache = None if force_rerun else _cache_singleton_safe()
            if not force_rerun:
                from rca_core.prompt import prompt_version_for_mode
                if cache is not None:
                    # C1 fix: use stable business fields only — never repr(provider)
                    # (repr includes uuid4 id + time.time() which change on every
                    # request, making the single-run cache命中率 ≈ 0).
                    prov = common["provider"]
                    try:
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
                    except Exception:
                        ckey = None
                    # REVIEW-2026-09-10: the cache is an optimisation, so a
                    # corrupt or unopenable cache file must degrade to a miss.
                    # Previously a single bad cache left every request raising
                    # here (get_cache() only publishes the singleton after a
                    # successful construction, so each call re-raised) and the
                    # endpoint could never answer again, with no self-heal.
                    if ckey:
                        try:
                            cache_hit = cache.get(ckey)
                        except Exception:
                            cache_hit = None
            # REVIEW-2026-09-20 (item 6): ``cache.get()`` hands back whatever the
            # sqlite blob decoded to, and the key is not content-validated. A
            # non-dict entry (a stale writer, a truncated blob, a hand-edited DB)
            # used to reach ``cache_hit.pop(...)``/``["_auto_mode"]`` and raise
            # TypeError inside the hit branch — again killing the request that the
            # cache was supposed to make fast. Non-dict payloads count as a miss.
            if cache_hit is not None and not isinstance(cache_hit, dict):
                cache_hit = None
            if cache_hit is not None:
                # FIX (quality): ensure the quality badge is present even on
                # a cache hit. The cached payload may predate the quality
                # scorer if an older client wrote it.
                if "quality" not in cache_hit:
                    cache_hit["quality"] = _safe_score_range_chart(cache_hit)
                # REVIEW-2026-09-10: the truncation signal lives on
                # ExtractResult, never inside result.data, so a cache hit used
                # to drop it entirely - the user saw "result may be truncated"
                # on the first Extract and nothing on a re-click (or for the
                # next user of the same figure), while the attached report
                # asserted truncated: False. The flags ride along with the
                # cached payload under reserved keys and are stripped here.
                cached_truncated = cache_hit.pop("_cache_truncated", None)
                cached_warning = cache_hit.pop("_cache_warning", "") or ""
                if mode_source:
                    cache_hit["_auto_mode"] = {"mode": mode, "source": mode_source}
                cache_hit["report"] = build_extraction_report(
                    data=cache_hit, mode=mode, mode_used=mode,
                    mode_source=mode_source,
                    truncated=cached_truncated, warning=cached_warning)
                self._send_json(200, {"ok": True, "data": cache_hit,
                                      "cached": True,
                                      "truncated": cached_truncated,
                                      "warning": cached_warning})
                return
            # REVIEW-2026-09-20 (item 8): SINGLE-run had no wall-clock deadline.
            # ``Handler.timeout`` (60 s) and _BODY_DEADLINE_SEC bound the INBOUND
            # socket only; the outbound LLM call is bounded by a per-recv
            # inactivity timeout, so a provider that trickles one byte every
            # 299 s kept the handler thread - and its up-to-20 MB body, and its
            # big-body memory token - alive indefinitely, 30 times per rate-limit
            # window. The multi-run branch already had a batch budget (Sprint B
            # #1); single-run gets the same treatment, computed the same way:
            # timeout_sec x the maximum number of outbound attempts the stack can
            # spend on one extraction, plus slack. The worker runs in a daemon
            # thread (see _spawn_extract_thread) so abandoning a hung call costs
            # neither a leaked non-daemon thread nor a Ctrl+C hang.
            run_deadline = (timeout_sec * _SINGLE_RUN_MAX_ATTEMPTS
                            + _SINGLE_RUN_DEADLINE_SLACK_SEC)
            fut = _spawn_extract_thread(mode, common)
            try:
                result = fut.result(timeout=run_deadline)
            except concurrent.futures.TimeoutError:
                fut.cancel()
                # Mirror the runs>=2 branch: a structured error JSON, never a
                # bare exception out of do_POST.
                self._send_json(200, {
                    "ok": False,
                    "data": None,
                    "error_key": "err.timeout",
                    "status": None,
                    "raw": "",
                    "truncated": False,
                    "error_body": _redact_error_body(
                        f"single-run deadline after {run_deadline}s "
                        f"(timeout_sec={timeout_sec} x "
                        f"{_SINGLE_RUN_MAX_ATTEMPTS} attempts + slack)"),
                    "usage": {},
                    "latency_ms": int(run_deadline * 1000),
                    "warning": "",
                })
                return
            except Exception as exc:
                # extract() is documented to return ExtractResult, but a
                # monkeypatched/GUI-supplied callable may raise; keep the
                # "always JSON" contract instead of dropping the connection.
                fut.cancel()
                self._send_json(200, {
                    "ok": False,
                    "data": None,
                    "error_key": "err.http",
                    "status": None,
                    "raw": "",
                    "truncated": False,
                    "error_body": _redact_error_body(str(exc)),
                    "usage": {},
                    "latency_ms": 0,
                    "warning": "",
                })
                return
            # FIX (quality): score single-run results too for a consistent
            # quality badge in the UI.
            if result.ok and result.data and isinstance(result.data, dict):
                if mode_source:
                    result.data["_auto_mode"] = {"mode": mode, "source": mode_source}
                result.data["quality"] = _safe_score_range_chart(result.data)
                # UI-REVIEW-2026-09-07 (evidence chain): attach the audit
                # report (mode decision, truncation, row counts, empty-table
                # reasons, ICS version) - borrowed from thu-digitizer.
                result.data["report"] = build_extraction_report(
                    data=result.data, mode=mode, mode_used=mode,
                    mode_source=mode_source, truncated=result.truncated,
                    warning=result.warning,
                    image_sha256=result.image_sha256,
                    request_meta=result.request_meta, runs=1)
                # Write the scored result back to the cache.
                # REVIEW-2026-09-10: carry the truncation flags with the
                # payload (reserved keys, stripped on read) so a later cache
                # hit can still warn the user that the output was cut off.
                if not force_rerun:
                    to_cache = dict(result.data)
                    to_cache["_cache_truncated"] = bool(result.truncated)
                    to_cache["_cache_warning"] = result.warning or ""
                    try:
                        cache.put(ckey, to_cache)
                    except Exception:
                        # The cache is an optimisation: a corrupt/unwritable
                        # cache file must not turn a SUCCESSFUL extraction
                        # into a 500. (The pre-existing failure mode was
                        # fatal - every later request re-raised and the
                        # endpoint could never answer again.)
                        pass
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
        per_future_timeout = timeout_sec + _MULTI_RUN_TIMEOUT_SLACK_SEC

        # CRITICAL fix (audit): each multi-run slot must have its OWN cache
        # key. Previously the per-slot loop produced identical keys
        # because the key inputs were loop-invariant, so all N cache
        # writes clobbered each other and only the first slot's result
        # was retained. We salt each slot's key with ``run_idx`` so the
        # slots are independently addressable.
        #
        # REVIEW-2026-09-20 (item 1): the key loop below called
        # ``get_cache().make_key(...)`` once per slot with NO guard, which made
        # the ``try: cache = get_cache()`` that used to sit at the prefill site
        # unreachable dead code — with a broken cache DB every multi-run request
        # raised here and dropped the connection. One guarded probe now feeds
        # key building, prefill and write-back; ``cache is None`` (or a failed
        # ``make_key``) leaves the slot key as None, which means "no cache for
        # this slot", never "failed request".
        from rca_core.prompt import prompt_version_for_mode
        prov = common["provider"]
        cache = _cache_singleton_safe()
        slot_keys: list = []
        for run_idx in range(runs):
            slot_ckey = None
            if cache is not None:
                try:
                    slot_ckey = cache.make_key(
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
                except Exception:
                    slot_ckey = None
            slot_keys.append(slot_ckey)

        if not force_rerun:
            # REVIEW-2026-09-10: a broken cache degrades to a miss instead of
            # failing the whole request (see the single-run probe above).
            for run_idx, ckey in enumerate(slot_keys):
                cached = None
                if cache is not None and ckey:
                    try:
                        cached = cache.get(ckey)
                    except Exception:
                        cached = None
                # REVIEW-2026-09-20 (item 6): a non-dict entry would otherwise be
                # merged as if it were an extraction payload (see the single-run
                # guard for the full rationale); drop it as a miss.
                if cached is not None and not isinstance(cached, dict):
                    cached = None
                if cached is not None:
                    if "quality" not in cached:
                        cached["quality"] = _safe_score_range_chart(cached)
                    # P0-4 (REVIEW-2026-07-25): KEYED BY SLOT INDEX so
                    # non-contiguous cache hits do not cause the
                    # submission loop to skip the true miss or re-run
                    # an already-hit slot.
                    slot_results[run_idx] = cached

        # Build ok_datas in slot order for the merge call.
        ok_datas = [slot_results[i] for i in range(runs) if i in slot_results]

        misses = runs - len(slot_results)
        # REVIEW-2026-09-10 / REVIEW-2026-09-20 (item 5): how many LIVE (not
        # cache-served) slots actually produced a payload. The audit row's image
        # fingerprint and per-run request metadata can only come from a live run,
        # and the runs that filled the cache already wrote their own audit rows,
        # so the write at the end of this branch is gated on this counter.
        #
        # The previous gate was "did this request perform ANY live call"
        # (``misses > 0``), which was only HALF the condition: the mixed case -
        # some slots cached, every live slot FAILING - still reached the write
        # with ``first_ok_sha == ""`` because the merge succeeded on the cached
        # payloads, i.e. exactly the duplicate sentinel-SHA (e3b0c442...) row
        # that HistoryStore.get_by_sha256 later groups as "the same image".
        # "At least one successful live slot" covers both shapes.
        live_ok_count = 0
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
            # Sprint B (REVIEW-2026-09-04) #1: batch-level timeout. The old
            # loop called ``fut.result(timeout=...)`` while iterating
            # ``as_completed``, which only yields futures that are ALREADY
            # finished — the timeout argument never fired, so one hung
            # provider call stalled the whole request forever. We now wait
            # ONCE for the whole batch (``concurrent.futures.wait``) with a
            # budget measured from submission time, and synthesize an
            # ``err.timeout`` failure for every run that did not finish.
            #
            # REVIEW-2026-09-20 (item 13): the explicit
            # ``ThreadPoolExecutor(max_workers=misses)`` +
            # ``shutdown(wait=False, cancel_futures=True)`` was replaced by
            # daemon-thread futures (see _spawn_extract_thread). ``wait=False``
            # only avoids BLOCKING on the shutdown; it does not stop
            # ``concurrent.futures.thread`` from joining its NON-daemon workers
            # at interpreter exit, so after Ctrl+C the process still waited for
            # every in-flight AND every still-queued slot - up to
            # ``runs`` x 300 s of a console that looks hung. ``shutdown()`` also
            # cannot cancel a running worker, so the executor bought nothing here
            # beyond thread reuse: ``misses`` is bounded by ``runs`` (<= 5) and
            # one batch of futures is submitted per request. Same futures
            # interface, no exit-time join, no per-request pool object to leak.
            pending = []  # list of (run_idx, future)
            for run_idx in range(runs):
                if run_idx in slot_results:
                    continue  # cache hit — already in slot_results
                pending.append((run_idx, _spawn_extract_thread(
                    mode, common, label=f"extract-run-{run_idx}")))
            futures = [f for _, f in pending]
            _, not_done = concurrent.futures.wait(
                futures,
                timeout=per_future_timeout,
                return_when=concurrent.futures.ALL_COMPLETED,
            )
            for run_idx, fut in pending:
                if fut in not_done:
                    # Batch budget exhausted before this run finished —
                    # record a synthetic timeout failure instead of
                    # blocking the client indefinitely.
                    fut.cancel()
                    r = ExtractResult(
                        ok=False, error_key="err.timeout",
                        error_body=(
                            f"batch timeout after {per_future_timeout}s "
                            f"(run {run_idx} did not finish)"
                        ),
                    )
                else:
                    try:
                        r = fut.result()
                    except Exception as exc:
                        # Sprint B (REVIEW-2026-09-04) #1: align with
                        # the single-run error structure — exception
                        # text belongs in error_body (redacted before
                        # it reaches the client), not in raw.
                        r = ExtractResult(
                            ok=False, error_key="err.http",
                            error_body=str(exc),
                        )
                # H-1 fix: update max_run_latency regardless of r.ok — only
                # skip when latency_ms is None/0 (no request was made).
                if r.latency_ms not in (None, 0):
                    max_run_latency = max(max_run_latency, int(r.latency_ms))
                if r.ok and r.data is not None:
                    live_ok_count += 1
                    if not force_rerun:
                        # Cache writes are best-effort (see the
                        # single-run path): a broken cache must not turn
                        # a completed run into a failed request.
                        # REVIEW-2026-09-20 (item 1): the write used to call
                        # ``get_cache()`` again — unguarded construction inside
                        # the ``if``, which only the surrounding ``except`` hid,
                        # and which would have raised per slot when the cache
                        # singleton was unavailable. Reuse the probe from the
                        # key-building step and skip when there is no cache or
                        # no key for this slot.
                        if cache is not None and slot_keys[run_idx]:
                            try:
                                cache.put(slot_keys[run_idx], r.data)
                            except Exception:
                                pass
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
                        # REVIEW-2026-09-10: keep the SLOT index with the
                        # text. Appending positionally made
                        # raw_responses.run_idx the position among the
                        # SURVIVING runs, so a failed or cache-hit slot
                        # shifted every later label and the audit trail
                        # attributed one slot's reply to another.
                        raws.append({'run_idx': run_idx, 'text': r.raw})
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
        if mode_source:
            merged["_auto_mode"] = {"mode": mode, "source": mode_source}
        # REVIEW-2026-09-10: pass the full evidence set, exactly as the
        # single-run path does (server.py's single-run call passes all four).
        # Without them the report - which rides into history.result_json as
        # the documented audit artifact - asserted `truncation.truncated:
        # False` and carried an empty `input.image_sha256` / `provenance` for
        # every runs>=2 extraction, contradicting the same response's
        # top-level fields.
        merged["report"] = build_extraction_report(
            data=merged, mode=mode, mode_used=mode,
            mode_source=mode_source, runs=runs,
            truncated=any_truncated, warning=merged_warning or "",
            image_sha256=first_ok_sha, request_meta=first_ok_meta or {})
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
            # ExtractResult comes from the module-level import; a local
            # ``from ... import ExtractResult`` here would make the name
            # function-local for ALL of do_POST and break the earlier
            # executor block with UnboundLocalError (Sprint B #1).
            multi_result = ExtractResult(
                ok=True,
                data=merged,
                error_key=None,
                status=200,
                raw=("\n---RUN---\n".join(_raw_text(t) for t in raws))[:8000] if raws else "",
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
            if live_ok_count <= 0:
                # REVIEW-2026-09-20 (item 5): nothing in this response came from
                # a live call, so (a) the runs that filled the cache already
                # wrote their audit rows — a second record duplicates them — and
                # (b) there is no live run to supply image_sha256 /
                # request_meta, so the duplicate used to be stamped with the
                # empty-bytes sentinel SHA that HistoryStore.get_by_sha256 then
                # groups as "the same image". This covers the all-cache-hit case
                # AND the previously unguarded mixed case (some slots cached,
                # every live slot failed but the merge still succeeded).
                pass
            else:
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
            "raw": ("\n---RUN---\n".join(_raw_text(t) for t in raws))[:8000],
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

    # REVIEW-2026-09-20 (item 4): the default bind stays loopback-only, but
    # ``--host 0.0.0.0``/``::`` is a documented option, and this server has NO
    # authentication: /api/extract lets any peer spend the operator's LLM quota
    # and (with provider fields supplied from the request) drive outbound
    # requests, while the audit endpoints expose the local history. Make the
    # exposure explicit at startup instead of letting it be an accident, and
    # point at the two knobs that do exist. Deliberately a warning, not a
    # refusal, so scripted/LAN deployments keep working.
    if not is_loopback_bind(args.host):
        sys.stderr.write(
            "\n"
            "WARNING: binding to {host!r} exposes this server to EVERY peer that\n"
            "can reach port {port} - /api/extract is unauthenticated (CSRF/Origin\n"
            "checks only stop cross-site browsers), so any such peer can spend your\n"
            "LLM quota, read the audit endpoints, and drive outbound provider\n"
            "requests from this machine. Prefer the default 127.0.0.1 plus an SSH\n"
            "or reverse tunnel; otherwise terminate TLS and add authentication in\n"
            "front of it (see proxy/), and set RCA_PROVENANCE_TOKEN to keep the\n"
            "provenance endpoint closed.\n\n".format(host=args.host, port=args.port)
        )

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

    Sprint B (REVIEW-2026-09-04) #5: the executor's internal queue is
    itself unbounded, so "queued" previously meant unbounded memory. A
    ``threading.BoundedSemaphore`` now bounds outstanding submissions to
    ``max_workers + request_queue_size``; when the cap is reached,
    ``process_request`` answers the excess connection with an immediate
    503 and closes it instead of queueing it forever. The semaphore slot
    is released when the request's handler thread finishes. Also:
    ``request_queue_size`` is now assigned BEFORE ``super().__init__()``
    — the stdlib base class calls ``listen(self.request_queue_size)``
    internally, so the post-construction assignment never took effect
    (listen(5) always applied).
    """

    daemon_threads = True

    def __init__(self, server_address, RequestHandlerClass,
                 max_workers: int = 32,
                 request_queue_size: int = 64):
        # Must precede super().__init__(): TCPServer.__init__ runs
        # bind_and_activate → server_activate → listen(request_queue_size).
        self.request_queue_size = max(1, request_queue_size)
        super().__init__(server_address, RequestHandlerClass,
                         bind_and_activate=True)
        self._max_workers = max(max_workers, 2)
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=self._max_workers,
            thread_name_prefix="rca-http",
        )
        # One slot per in-flight or queued connection. Capacity matches the
        # documented model: max_workers running + request_queue_size queued.
        self._submit_slots = threading.BoundedSemaphore(
            self._max_workers + self.request_queue_size
        )

    def process_request(self, request, client_address):
        if not self._submit_slots.acquire(blocking=False):
            # Overload: refuse instead of queueing without bound. A raw
            # 503 is safe here — the socket is still untouched at this
            # point (finish_request has not run).
            try:
                request.sendall(
                    b"HTTP/1.1 503 Service Unavailable\r\n"
                    b"Content-Type: text/plain\r\n"
                    b"Content-Length: 0\r\n"
                    b"Connection: close\r\n"
                    b"\r\n"
                )
            except Exception:
                pass
            self.shutdown_request(request)
            return
        self._executor.submit(self._process_request_bounded,
                              request, client_address)

    def _process_request_bounded(self, request, client_address):
        try:
            self.process_request_thread(request, client_address)
        finally:
            try:
                self._submit_slots.release()
            except ValueError:
                # Defensive: release() beyond the initial value would raise;
                # cannot happen (one acquire per submit) but never let a
                # bookkeeping slip kill the worker thread.
                pass

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
