"""Server-side extraction orchestration, decoupled from the HTTP socket.

WHY THIS MODULE EXISTS
======================
``server.py::Handler._handle_extract_body`` was 997 lines at v0.3.0: request
validation, provider resolution, auto-mode classification, single-run and
multi-run fan-out, cache lookup, merge/quality scoring, history writes and
response assembly all lived in one method. Every one of those decisions was
therefore untestable without constructing an HTTP handler.

This module holds that orchestration VERBATIM. It was produced by
``scripts/audit/split_extract_handler.py`` via AST-located text replacement,
not by retyping: the 18 response exits, the 17 early returns, the retry /
timeout / cancellation logic and every historical bug-fix comment are
byte-identical to the original method body. The only substitutions are:

  * ``self._send_json(...)``  -> ``emit(...)``          (injected callback)
  * ``self._read_body_with_deadline(length)`` -> ``raw`` (injected bytes)
  * ``self._request_body_consumed = True``    -> moved into the HTTP layer
  * 16 server.py-local names -> ``deps.<name>``         (injected namespace)

DEPENDENCY DIRECTION
====================
``rca_core`` must never import ``server.py``. Everything this module needs
from the server layer arrives through :class:`PipelineDeps`, built by
``server.build_pipeline_deps()``. Everything else is imported directly from
rca_core / the stdlib. There is no static edge back to the HTTP layer, so
the core import graph stays acyclic and the orchestration becomes callable
from a plain unit test with a duck-typed ``emit``.

BEHAVIOUR LOCK
==============
``tests/test_extract_pipeline_parity.py`` replays 19 locally-terminating
requests against the REAL handler and compares every response byte-for-byte
with a golden recorded before this migration. Do not change a response
without regenerating that golden deliberately.
"""
from __future__ import annotations

import concurrent.futures
import json
import time
from dataclasses import dataclass
from typing import Any, Callable

from rca_core.aggregate import RANGE_CHART_SCHEMA, SCHEMA_BY_MODE, merge_results
from rca_core.extractor import (
    DEFAULT_ENDPOINT,
    DEFAULT_MAX_TOKENS,
    DEFAULT_MODEL,
    DEFAULT_TIMEOUT_SEC,
    ExtractResult,
    clamp_max_tokens,
    resolve_auto_mode,
)
from rca_core.llm import ApiFormat, LlmProvider, normalize_reasoning_effort
from rca_core.modes import SERVER_MODE_NAMES as _REGISTRY_SERVER_MODES
from rca_core.redact import redact_error_body as _redact_error_body
from rca_core.report import build_extraction_report

Emit = Callable[..., None]


@dataclass(frozen=True)
class PipelineDeps:
    """Server.py-local symbols the orchestration needs, injected explicitly.

    Kept as a frozen dataclass rather than a module object so the contract is
    greppable and a unit test can substitute any single collaborator.
    """

    _MAX_EXTRACT_TIMEOUT_SEC: Any
    _MIN_EXTRACT_TIMEOUT_SEC: Any
    _SINGLE_RUN_MAX_ATTEMPTS: Any
    _cache_singleton_safe: Any
    _constrain_resolved_mode: Any
    _enhance_image_b64_with_mime: Any
    _extraction_wall_clock_budget_sec: Any
    _raw_text: Any
    _safe_ground_and_score: Any
    _safe_score_cached: Any
    _spawn_extract_thread: Any
    _validate_endpoint_safe: Any
    _validate_image_b64: Any
    _validate_provider_fields: Any
    _write_cache_hit_history: Any
    _write_history_record: Any


__all__ = ["PipelineDeps", "run_extraction_request"]


def run_extraction_request(raw, *, emit, deps) -> None:
    try:
        if raw is None:
            # REVIEW-2026-09-10: a trickled body is dropped, not parked.
            # See _read_body_with_deadline.
            emit(408, {
                "ok": False,
                "error_key": "err.timeout",
                "error_body": "request body not received within the upload deadline",
            })
            return
        req = json.loads(raw.decode("utf-8"))
    except Exception:
        # AUDIT-2026-09-29 (ruff F841): `as exc` bound a name nothing
        # read. The wording stays deliberately generic: this handler's
        # sibling at the top of the file documents why a client's own
        # bytes are not echoed back, and a json decoder message is the
        # same kind of detail.
        emit(400, {
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
        emit(400, {
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
            emit(400, {
                "ok": False,
                "error_key": "err.parse",
                "error_body": f"field {_field!r} must be a string",
            })
            return
    for _field in ("extra_headers", "extra_body"):
        v = req.get(_field)
        if v is not None and not isinstance(v, dict):
            emit(400, {
                "ok": False,
                "error_key": "err.parse",
                "error_body": f"field {_field!r} must be an object",
            })
            return

    image_b64 = req.get("image_b64") or ""
    # Issue-2 fix: validate base64 format and size (10MB limit)
    ok, err_key = deps._validate_image_b64(image_b64)
    if not ok:
        emit(200, {"ok": False, "error_key": err_key})
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
# DERIVED from rca_core/modes.py (2026-10-15) - the HTTP gate for an
    # explicitly requested mode. "auto" stays here rather than in the
    # registry because it is a request-level alias that resolves server-side
    # to a concrete mode, not a mode the classifier can return.
    _SUPPORTED_MODES = frozenset(_REGISTRY_SERVER_MODES) | {"auto"}
    _requested_mode = (req.get('mode') or 'range_chart').strip()
    if _requested_mode not in _SUPPORTED_MODES:
        emit(400, {
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
        _nested_error = deps._validate_provider_fields(provider_raw)
        if _nested_error:
            emit(400, {
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
        emit(400, {
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
            emit(200, {"ok": False, "error_key": "err.noKey"})
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
        ok, why = deps._validate_endpoint_safe(endpoint)
        if not ok:
            emit(200, {
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
        ok, why = deps._validate_endpoint_safe(provider.endpoint)
        if not ok:
            emit(200, {
                "ok": False,
                "error_key": "err.badEndpoint",
                "error_body": why,
            })
            return

    # FIX-2026-10-28: per-request reasoning-effort override. The web
    # frontend picks an effort level, but until now the POST body dropped
    # it and the server took the level only from the provider config
    # (`_stable_reasoning_effort(prov)`), so a backend-mode user who chose
    # effort=low silently ran at the provider default — effort=max on
    # M3.1, the exact configuration that times out on a dense figure.
    #
    # Validation mirrors the provider-config field exactly:
    # `normalize_reasoning_effort` keeps only the REASONING_LEVELS names
    # (stripped, lowercased) and DROPS anything else — the same
    # safe-failure an invalid value in a provider record gets, never a
    # 400. Absent / empty / invalid keeps today's behavior byte-for-byte
    # (the provider's own default applies).
    #
    # The override lands on the request-local provider BEFORE `common`
    # is built, so every downstream reader sees the effective value from
    # one place: the wire patch (`_reasoning_request_patch`), the auto
    # classification call, and BOTH cache-key sites (single-run and
    # multi-run read `_stable_reasoning_effort(common["provider"])`) —
    # two requests differing only by effort can no longer collide.
    _req_effort = normalize_reasoning_effort(req.get("effort"))
    if _req_effort and provider is not None:
        provider.reasoning_effort = _req_effort

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
    timeout_sec = max(deps._MIN_EXTRACT_TIMEOUT_SEC,
                      min(timeout_sec, deps._MAX_EXTRACT_TIMEOUT_SEC))

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
        mode, mode_source = deps._constrain_resolved_mode(mode, mode_source)

    # Sprint B (REVIEW-2026-09-04) #12: honour the optional ``enhance``
    # flag from the web UI (it was previously dropped silently). The
    # enhancement runs BEFORE the cache key is derived so enhanced and
    # un-enhanced variants of the same upload never share a cache
    # entry. Pillow missing or any enhancement failure falls back to
    # the original image silently.
    if req.get("enhance"):
        # The re-encode is PNG, so the declared media type must follow the
        # bytes (see _enhance_image_b64_with_mime).
        _enhanced_b64, _enhanced_mime = deps._enhance_image_b64_with_mime(image_b64)
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
    #
    # AUDIT-2026-10-01 [item 9.9]: the two helpers were nested defs here,
    # which is why extractor.py could not include the same fields in the
    # chart-classify key and did not -- a class of collision the project
    # had already named in _stable_extra_body's own docstring ("two
    # requests with different extra_body MUST NOT share a cache entry").
    # They now live in rca_core.cache next to make_key, which is where the
    # next key builder will look.
    from rca_core.cache import stable_extra_body as _stable_extra_body
    from rca_core.cache import build_extraction_key
    from rca_core.cache import stable_extra_headers as _stable_extra_headers
    # REVIEW-2026-10-19: the reasoning effort changes the request shape
    # the same way extra_body does -- it picks the model's reasoning
    # depth, and on MiniMax's M3.1 it decides whether the call answers at
    # all -- so it belongs in the cache key too. Without it a user who
    # switched the effort got the previous effort's answer back.
    from rca_core.cache import (
        stable_reasoning_effort as _stable_reasoning_effort)

    # Name grounding (rca_core/grounding.py) costs one extra model call per
    # extraction, so it is opt-out rather than opt-in. Defaulting it OFF
    # would leave the tool in exactly the state it is being fixed for -- a
    # confident A/1.0 grade on names that are not in the figure -- so a
    # silently-disabled check is not an acceptable default.
    # `"ground_names": false` turns it off.
    # Defined here, OUTSIDE the `runs == 1` branch: the merged multi-run
    # path scores too, and a name bound only inside the single-run branch
    # made that path raise NameError on every runs>=2 request.
    ground_names = req.get("ground_names") is not False

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
        cache = None if force_rerun else deps._cache_singleton_safe()
        if not force_rerun:
            # FIX-2026-09-22 (audit item 1): the cache-key prompt version
            # must come from the RESOLVED mode (auto was resolved above,
            # before this block) and via the STRICT helper — an unresolved
            # "auto" must never silently key results under the "v3"
            # fallback while the audit line stamps the resolved v5.
            from rca_core.prompt import prompt_version_for_cache_key
            if cache is not None:
                # C1 fix: use stable business fields only — never repr(provider)
                # (repr includes uuid4 id + time.time() which change on every
                # request, making the single-run cache命中率 ≈ 0).
                prov = common["provider"]
                try:
                    ckey = build_extraction_key(
                        endpoint=prov.endpoint if prov else "",
                        model=prov.model if prov else "",
                        api_format=prov.api_format.value if prov else "",
                        extra_headers=_stable_extra_headers(prov),
                        extra_body=_stable_extra_body(prov),
                        reasoning_effort=_stable_reasoning_effort(prov),
                        prompt_version=prompt_version_for_cache_key(mode),
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
                cache_hit["quality"] = deps._safe_score_cached(cache_hit)
            # REVIEW-2026-09-10: the truncation signal lives on
            # ExtractResult, never inside result.data, so a cache hit used
            # to drop it entirely - the user saw "result may be truncated"
            # on the first Extract and nothing on a re-click (or for the
            # next user of the same figure), while the attached report
            # asserted truncated: False. The flags ride along with the
            # cached payload under reserved keys and are stripped here.
            cached_truncated = cache_hit.pop("_cache_truncated", None)
            cached_warning = cache_hit.pop("_cache_warning", "") or ""
            # FIX-2026-10-01 (prior finding B6, still live): this only ever
            # SET the key, so a `_auto_mode` cached by an earlier
            # `mode: "auto"` request survived onto a LATER request that
            # named the mode explicitly. Measured: request 1 with
            # `mode: "auto"` -> 200, `_auto_mode = {mode:
            # zonation_chart, source: "text"}`; request 2, byte-identical
            # except `"mode": "zonation_chart"` -> 200, `cached: true`,
            # and the SAME `_auto_mode`. So a user who picked the mode by
            # hand was told the server chose it -- in the response AND in
            # the audit report, which is the artifact the mode decision is
            # supposed to be evidenced by. Pop first so the key reflects
            # THIS request's mode decision and nothing else. The multi-run
            # path is unaffected: its cache write stores the pre-merge
            # per-slot data, which never carries the key.
            cache_hit.pop("_auto_mode", None)
            if mode_source:
                cache_hit["_auto_mode"] = {"mode": mode, "source": mode_source}
            cache_hit["report"] = build_extraction_report(
                data=cache_hit, mode=mode, mode_used=mode,
                mode_source=mode_source,
                truncated=cached_truncated, warning=cached_warning)
            # AUDIT-2026-10-22: the audit trail used to end at this
            # return -- a served result recorded WHO extracted it the
            # first time but not who re-ran it, when, or under which
            # mode decision. The hit is now a (lightweight) history
            # record of its own.
            deps._write_cache_hit_history(
                cache_hit, mode=mode, mode_source=mode_source,
                provider=common["provider"],
                max_tokens=common["max_tokens"],
                chart_lang=common["chart_lang"],
                image_b64=common["image_b64"])
            emit(200, {"ok": True, "data": cache_hit,
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
        # REVIEW-2026-09-27 [item 2]: the budget now comes from the ONE
        # shared derivation (``_extraction_wall_clock_budget_sec``) that the
        # multi-run branch below uses too — the two branches had drifted ~6x
        # apart on the price of a single extraction, and the cheaper number
        # was the one that shipped for N-runs, so a batch slot that needed a
        # transport retry (2 attempts) plus a silent-miss re-ask (2 more) was
        # abandoned at ``timeout_sec + 10`` while the provider was still
        # working on it.
        run_t0 = time.perf_counter()
        run_deadline = deps._extraction_wall_clock_budget_sec(timeout_sec)
        fut = deps._spawn_extract_thread(mode, common)
        try:
            result = fut.result(timeout=run_deadline)
        except concurrent.futures.TimeoutError:
            fut.cancel()
            # Mirror the runs>=2 branch: a structured error JSON, never a
            # bare exception out of do_POST.
            emit(200, {
                "ok": False,
                "data": None,
                "error_key": "err.timeout",
                "status": None,
                "raw": "",
                "truncated": False,
                "error_body": _redact_error_body(
                    f"single-run deadline after {run_deadline}s "
                    f"(timeout_sec={timeout_sec} x "
                    f"{deps._SINGLE_RUN_MAX_ATTEMPTS} attempts + slack)"),
                "usage": {},
                # REVIEW-2026-09-27 [item 2]: this used to report
                # ``run_deadline * 1000`` - a FABRICATED figure, not a
                # measurement. It is always the budget, whatever actually
                # elapsed, so the audit trail's duration_ms (fed from
                # latency_ms) and the UI's timing disagreed with reality by
                # up to the whole budget, and a "0.9 s" run that timed out
                # was recorded as a 21-minute one. Report what was measured
                # - the same ``perf_counter`` arithmetic the multi-run branch
                # uses for its own total_latency.
                "latency_ms": int((time.perf_counter() - run_t0) * 1000),
                "warning": "",
            })
            return
        except Exception as exc:
            # extract() is documented to return ExtractResult, but a
            # monkeypatched/GUI-supplied callable may raise; keep the
            # "always JSON" contract instead of dropping the connection.
            fut.cancel()
            emit(200, {
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
            result.data["quality"] = deps._safe_ground_and_score(
                result.data, image_b64=image_b64,
                media_type=req.get("media_type") or "image/png",
                provider=provider,
                enabled=ground_names)
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
        deps._write_history_record(
            result, mode=mode, runs=1, provider=provider,
            max_tokens=common["max_tokens"],
            chart_lang=common["chart_lang"],
            partial_failures=0,
            duration_ms=result.latency_ms,
            raws=[result.raw] if result.raw else None,
        )
        emit(200, {
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
    # REVIEW-2026-09-27 [item 2]: this was ``timeout_sec +
    # _MULTI_RUN_TIMEOUT_SLACK_SEC`` - one outbound attempt plus slack for
    # the WHOLE batch, while a single slot can spend up to ~4 x
    # ``timeout_sec`` (a transport retry, then the extractor's silent-miss
    # re-ask, each of which is itself a full ``timeout_sec`` call). A
    # slow-but-successful slot was therefore abandoned and reported
    # ``err.timeout`` while its daemon thread went on to finish, was still
    # billed by the provider, and had its result dropped uncached, so the
    # user's retry paid for the same extraction twice. The budget is now
    # the single shared one (``_extraction_wall_clock_budget_sec``, the
    # same value the runs==1 branch above waits on): the slots run
    # CONCURRENTLY, so the batch is bounded by its SLOWEST slot, i.e. by
    # one slot's worst case - never by N of them added together.
    # Concurrency stays bounded by the POST rate limit (30 admitted
    # extractions per IP per window, each on a worker of the capped pool),
    # not by this timeout.
    per_future_timeout = deps._extraction_wall_clock_budget_sec(timeout_sec)

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
    # FIX-2026-09-22 (audit item 1): strict cache-key prompt version from
    # the RESOLVED mode — "auto" must never key results under the silent
    # "v3" fallback (see the single-run key site above).
    from rca_core.prompt import prompt_version_for_cache_key
    prov = common["provider"]
    from rca_core.cache import build_extraction_key
    cache = deps._cache_singleton_safe()
    slot_keys: list = []
    for run_idx in range(runs):
        slot_ckey = None
        if cache is not None:
            try:
                slot_ckey = build_extraction_key(
                    endpoint=prov.endpoint if prov else "",
                    model=prov.model if prov else "",
                    api_format=prov.api_format.value if prov else "",
                    extra_headers=_stable_extra_headers(prov),
                    extra_body=_stable_extra_body(prov),
                    reasoning_effort=_stable_reasoning_effort(prov),
                    prompt_version=prompt_version_for_cache_key(mode),
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
                    cached["quality"] = deps._safe_score_cached(cached)
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
            pending.append((run_idx, deps._spawn_extract_thread(
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
        emit(200, {
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
    quality = deps._safe_ground_and_score(
        merged, image_b64=image_b64,
        media_type=req.get("media_type") or "image/png",
        provider=provider, enabled=ground_names)
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
            raw=("\n---RUN---\n".join(deps._raw_text(t) for t in raws))[:8000] if raws else "",
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
        #
        # AUDIT-2026-09-29: prompt_version_for_mode was used below with no
        # binding in scope. The only import of it in this file lives inside
        # _write_history_record (line ~496), a function-local import, so the
        # server module namespace never had the name -- the multi-run branch
        # raised NameError while assembling request_meta, i.e. every batched
        # extraction (runs > 1) died after the runs had already been paid
        # for. A function-local import here matches how the two
        # prompt_version_for_cache_key sites in this same branch do it.
        from rca_core.prompt import prompt_version_for_mode
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
            deps._write_history_record(
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
    emit(200, {
        "ok": True,
        "data": merged,
        "runs": runs,
        "error_key": None,
        "status": None,
        "raw": ("\n---RUN---\n".join(deps._raw_text(t) for t in raws))[:8000],
        "truncated": any_truncated,
        "partial_failures": partial_fails,
        "warning": merged_warning,
        "usage": merged_usage,
        "latency_ms": total_latency,
    })
