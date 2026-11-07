"""Byte-level response parity for the /api/extract handler refactor.

WHY THIS EXISTS
===============
server.py::_handle_extract_body (997 lines, 18 `_send_json` exits at v0.3.0)
is being moved into rca_core/pipeline.py. A refactor of that size is only
trustworthy if the responses do not change AT ALL -- not "the tests still
pass", but the exact serialised bytes for every locally-reachable exit.

The golden in tests/fixtures/extract_responses_baseline.json was recorded
from the PRE-REFACTOR handler by scripts/audit/record_extract_baseline.py.
It contains 19 cases that all terminate before any model/network call (a
case that reached a live provider was removed after it produced a real
request_id / latency_ms -- see the script's comment).

Regenerating the golden is a deliberate act. If this test fails after a
refactor, the refactor changed behaviour; do not "update the fixture" until
you can name and justify the change.
"""
from __future__ import annotations

import ast
import json
import pathlib
import sys
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import server as srv  # noqa: E402

GOLDEN = ROOT / "tests" / "fixtures" / "extract_responses_baseline.json"


class _Recorder:
    """Duck-typed `self`; the handler touches only these three members."""

    def __init__(self, raw: bytes | None) -> None:
        self.raw = raw
        self.sent: list[dict[str, Any]] = []
        self._request_body_consumed = False

    def _read_body_with_deadline(self, length: int) -> bytes | None:
        return self.raw

    def _send_json(self, status: int, payload: dict, **kwargs) -> None:
        self.sent.append({
            "status": status,
            "payload": payload,
            "bytes": json.dumps(payload, ensure_ascii=False, allow_nan=False),
            "kwargs": {k: v for k, v in kwargs.items()
                       if isinstance(v, (str, int, float, bool, type(None)))},
        })


def _replay(cases: list[dict]) -> list[dict]:
    out: list[dict] = []
    for case in cases:
        raw = None if case["request"] is None else case["request"].encode("utf-8")
        rec = _Recorder(raw)
        error = None
        try:
            srv.Handler._handle_extract_body(rec, len(raw) if raw else 0)
        except Exception as exc:  # a rejection must never raise
            error = f"{type(exc).__name__}: {exc}"
        out.append({
            "name": case["name"],
            "responses": rec.sent,
            "exception": error,
            "body_consumed": rec._request_body_consumed,
        })
    return out


def test_golden_exists_and_covers_the_local_rejection_paths() -> None:
    assert GOLDEN.is_file(), (
        "response baseline missing; run scripts/audit/record_extract_baseline.py "
        "against the pre-refactor handler")
    data = json.loads(GOLDEN.read_text(encoding="utf-8"))
    cases = data["cases"]
    assert len(cases) >= 15, "baseline lost cases; refusing to trust a thin golden"
    keys = [r["payload"].get("error_key") for c in cases for r in c["responses"]]
    # These are the exits a refactor is most likely to reorder or shorten.
    for required in ("err.timeout", "err.parse", "err.badMode", "err.noKey",
                     "err.badEndpoint", "err.noImage", "err.imageInvalidBase64"):
        assert required in keys, f"baseline no longer covers {required}"
    # No golden case may carry live-provider artefacts: that would make the
    # parity test network-dependent and non-deterministic.
    blob = GOLDEN.read_text(encoding="utf-8")
    assert "request_id" not in blob and "latency_ms" not in blob, (
        "baseline contains a live model response; remove that case")


def test_responses_match_the_pre_refactor_baseline_byte_for_byte() -> None:
    data = json.loads(GOLDEN.read_text(encoding="utf-8"))
    expected = [{
        "name": c["name"],
        "responses": c["responses"],
        "exception": c["exception"],
        "body_consumed": c["body_consumed"],
    } for c in data["cases"]]
    actual = _replay(data["cases"])
    assert actual == expected, (
        "/api/extract responses changed. Diff (first 3):\n" + "\n".join(
            f"  {e['name']}:\n    expected={e}\n    actual={a}"
            for e, a in zip(expected, actual) if e != a
        )[:4000]
    )


def test_pipeline_module_is_the_single_orchestration_owner() -> None:
    """The refactor must actually move the logic, not just shorten a wrapper.

    Guards against the tempting fake: deleting the body or hiding it behind
    a `__getattr__` while leaving 997 lines in server.py.
    """
    pipeline = ROOT / "rca_core" / "pipeline.py"
    assert pipeline.is_file(), "rca_core/pipeline.py was not created"
    server_tree = ast.parse((ROOT / "server.py").read_text(encoding="utf-8"))
    handler = next(
        (n for n in ast.walk(server_tree)
         if isinstance(n, ast.FunctionDef) and n.name == "_handle_extract_body"),
        None)
    assert handler is not None
    lines = handler.end_lineno - handler.lineno + 1
    assert lines <= 120, (
        f"_handle_extract_body is still {lines} lines; the HTTP boundary "
        "should be a thin adapter over rca_core.pipeline")
    body_src = ast.get_source_segment(
        (ROOT / "server.py").read_text(encoding="utf-8"), handler) or ""
    assert "pipeline" in body_src, (
        "_handle_extract_body does not delegate to rca_core.pipeline")


__all__ = ["_Recorder", "_replay", "GOLDEN"]
