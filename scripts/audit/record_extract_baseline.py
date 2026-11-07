#!/usr/bin/env python3
"""Record the byte-level /api/extract response baseline BEFORE refactoring.

WHY THIS EXISTS
===============
server.py::_handle_extract_body is 997 lines with 18 `_send_json` exits.
Moving it into rca_core/pipeline.py is only safe if every early-rejection
response stays byte-for-byte identical. This script drives the REAL handler
with a duck-typed recorder (the method only touches three `self` members:
_read_body_with_deadline, _send_json, _request_body_consumed), so no HTTP
server, no socket, no LLM and no network are involved.

The recorded baseline is committed and consumed by
tests/test_extract_pipeline_parity.py. Regenerate it ONLY when a response
change is deliberate, and say so in the commit message.

Usage::

    python scripts/audit/record_extract_baseline.py
    python scripts/audit/record_extract_baseline.py --out /path/to/other.json
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import server as srv  # noqa: E402

# A tiny but VALID PNG so _validate_image_b64 accepts it; nothing decodes it
# because every case below is rejected before a model call.
_PNG_1X1 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAF"
    "AAH/q842iQAAAABJRU5ErkJggg=="
)


def _cases() -> list[tuple[str, bytes | None, dict]]:
    """(name, raw body, extra env) — all stop before any LLM/network call."""
    def body(**kw) -> bytes:
        return json.dumps(kw, ensure_ascii=False).encode("utf-8")

    return [
        ("trickled_body", None, {}),
        ("invalid_json", b"not json at all", {}),
        ("non_dict_json", b"[1, 2, 3]", {}),
        ("string_json", b'"hello"', {}),
        ("field_image_b64_not_str", b'{"image_b64": 5}', {}),
        ("field_caption_not_str", b'{"caption": []}', {}),
        ("field_extra_headers_not_dict", b'{"extra_headers": []}', {}),
        ("field_extra_body_not_dict", b'{"extra_body": "x"}', {}),
        ("field_mode_not_str", b'{"mode": 3}', {}),
        ("empty_payload", b'{}', {}),
        ("image_b64_bad_base64", body(image_b64="!!!not-base64!!!"), {}),
        ("good_image_unsupported_mode",
         body(image_b64=_PNG_1X1, mode="not_a_mode"), {}),
        ("provider_not_dict", body(image_b64=_PNG_1X1, provider=[1, 2]), {}),
        ("provider_nested_bad_type",
         body(image_b64=_PNG_1X1, provider={"endpoint": 5}), {}),
        ("no_api_key", body(image_b64=_PNG_1X1), {}),
        # NOTE: an unknown api_format does NOT reliably stop before the model
        # call -- a measured run reached a live provider and returned
        # err.401 with a real request_id / latency_ms. That makes it both
        # network-dependent and non-deterministic, so it is deliberately NOT
        # a golden case. All cases below must terminate locally.
        ("https_private_endpoint_blocked",
         body(image_b64=_PNG_1X1, api_key="k",
              endpoint="https://10.0.0.1/v1"), {}),
        ("https_localhost_endpoint_blocked",
         body(image_b64=_PNG_1X1, api_key="k",
              endpoint="https://127.0.0.1:1/v1"), {}),
        ("cleartext_endpoint_rejected",
         body(image_b64=_PNG_1X1, api_key="k",
              endpoint="http://127.0.0.1:1/v1"), {}),
        ("extra_headers_not_dict_nested",
         body(image_b64=_PNG_1X1, api_key="k",
              provider={"api_key": "k", "extra_headers": "oops"}), {}),
    ]


class _Recorder:
    """Duck-typed `self` for Handler._handle_extract_body."""

    def __init__(self, raw: bytes | None) -> None:
        self.raw = raw
        self.sent: list[dict] = []
        self._request_body_consumed = False

    def _read_body_with_deadline(self, length: int) -> bytes | None:
        return self.raw

    def _send_json(self, status: int, payload: dict, **kwargs) -> None:
        # Serialise exactly like the real _send_json, including ensure_ascii
        # and allow_nan, so the golden is byte-comparable.
        text = json.dumps(payload, ensure_ascii=False, allow_nan=False)
        self.sent.append({
            "status": status,
            "payload": payload,
            "bytes": text,
            "kwargs": {k: v for k, v in kwargs.items()
                       if isinstance(v, (str, int, float, bool, type(None)))},
        })


def record() -> dict:
    out: dict = {"cases": [], "handler": "_handle_extract_body"}
    for name, raw, env in _cases():
        saved = {k: os.environ.get(k) for k in env}
        os.environ.update({k: str(v) for k, v in env.items()})
        try:
            rec = _Recorder(raw)
            try:
                srv.Handler._handle_extract_body(rec, len(raw) if raw else 0)
                error = None
            except Exception as exc:  # a rejection must never raise
                error = f"{type(exc).__name__}: {exc}"
            out["cases"].append({
                "name": name,
                "request": None if raw is None else raw.decode("utf-8", "replace"),
                "responses": rec.sent,
                "exception": error,
                "body_consumed": rec._request_body_consumed,
            })
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    default = ROOT / "tests" / "fixtures" / "extract_responses_baseline.json"
    ap.add_argument("--out", type=pathlib.Path, default=default)
    args = ap.parse_args(argv)
    data = record()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    print(f"wrote {len(data['cases'])} cases -> {args.out}")
    for case in data["cases"]:
        sent = case["responses"]
        brief = "no response" if not sent else ", ".join(
            f"{r['status']} {r['payload'].get('error_key') or 'ok'}" for r in sent)
        flag = "  EXC=" + case["exception"] if case["exception"] else ""
        print(f"  {case['name']:34s} {brief}{flag}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
