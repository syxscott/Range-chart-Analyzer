"""AUDIT-2026-10-01 [item 35]: a model REFUSAL is reported as a successful
extraction, on BOTH transports.

Finding 1 of docs/FRONTEND-REVIEW-2026-08-19.json (verdict CONFIRMED, HIGH,
parity) said the JS normalizers lacked the "unrecognized payload" guard and
that an empty rescue was returned as ok:true.  Half of that was fixed: both
sides now emit the ``truncated_or_unrecognized_payload`` warning.

The DECISION that warning feeds is still short-circuited, on both sides:

    rca_core/extractor.py::_extracted_any
        for value in out.values():
            if isinstance(value, (list, dict)) and len(value) > 0: return True
    js/minimax.js::rcaExtractedAny
        ... Object.keys(value).length > 0 ... return true

Neither excludes the catch-alls.  For a refusal payload the normalized object
is

    {sections: [], species_ranges: [], ..., confidence: 0,
     _warnings: ["truncated_or_unrecognized_payload"],   <- non-empty list
     _extras: {"message": "Sorry, I cannot read the text..."}}  <- non-empty dict

so ``_extracted_any`` returns True on BOTH counts -- and one of them is the
guard's own marker.  ``_payload_mismatch`` / ``rcaPayloadMismatch`` therefore
return "" at their first early-out, never reaching the warning check they were
written to make.

Why the docstring's intent and the code disagree: it says "True when a
normalizer salvaged at least one record", and "_extras is by definition the
keys it did NOT recognize" -- so counting it as evidence of extraction is
self-contradictory.  Same for ``_warnings``.

Measured consequence for research data: a VLM answering "Sorry, I cannot read
the text in this image clearly enough" yields zero taxa, a SUCCESS status, and
an exportable result.  A researcher can log "no taxa" for a figure that has
data, or hand over an empty CSV as a result.

NOT FIXED HERE.  Making the check ignore the catch-alls turns an empty
extraction from "success" into a parse failure, which is a user-visible
behaviour change and the maintainer's call.  This module pins the measured
state so the gap is a tracked fact rather than an absence of evidence; when it
is fixed, the two ``FIX_ME`` assertions below invert and become the standing
cross-transport guard.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from rca_core import extractor

REPO = Path(__file__).resolve().parent.parent

#: The two payloads finding 1 names.  A refusal is what a VLM actually returns
#: when it cannot read a figure; the inner object is what the lenient JSON
#: rescue hands back from a truncated response.
REFUSAL = {"message": "Sorry, I cannot read the text in this image clearly enough."}
RESCUED_INNER = {"species": "Orbicularia striata", "section": "S1",
                 "range_top": "9", "range_base": "7"}

FIX_ME = False  # flip to True when the catch-alls stop counting as evidence


def test_a_refusal_is_not_counted_as_a_successful_extraction():
    out = extractor.normalize_result(dict(REFUSAL))
    assert "truncated_or_unrecognized_payload" in (out.get("_warnings") or []), (
        "the guard no longer warns -- the whole question changed, re-read the "
        "normaliser before trusting this test")
    if FIX_ME:
        assert extractor._extracted_any(out) is False
        assert extractor._payload_mismatch(out, False), (
            "a refusal must be reported as a parse failure, not a success")
    else:
        assert extractor._extracted_any(out) is True, (
            "expected _extras/_warnings to still fool the guard; if they no "
            "longer do, set FIX_ME = True and tighten this test")
        assert extractor._payload_mismatch(out, False) == "", (
            "expected the mismatch check to be short-circuited by the "
            "catch-alls; it is not, so re-derive the expected behaviour")


def test_a_rescued_inner_object_is_not_counted_as_extraction():
    out = extractor.normalize_result(dict(RESCUED_INNER))
    assert "truncated_or_unrecognized_payload" in (out.get("_warnings") or [])
    if FIX_ME:
        assert extractor._extracted_any(out) is False
    else:
        assert extractor._extracted_any(out) is True
        assert extractor._payload_mismatch(out, False) == ""


def _js(payload: dict):
    """Evaluate js/minimax.js and return (normalized, payloadMismatch)."""
    script = (
        "var fs=require('fs'),vm=require('vm');"
        "var ctx=vm.createContext({console:console});"
        "vm.runInContext(fs.readFileSync('js/minimax.js','utf8'),ctx);"
        "var p=%s;"
        "var out=ctx.rcaNormalizeResult(p);"
        "process.stdout.write(JSON.stringify({"
        "  out: out,"
        "  mm: ctx.rcaPayloadMismatch?ctx.rcaPayloadMismatch(out,false):null"
        "}));" % json.dumps(payload)
    )
    proc = subprocess.run(["node", "-e", script], cwd=str(REPO),
                          capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    assert proc.returncode == 0, "could not evaluate js/minimax.js: %s" % (
        (proc.stderr or proc.stdout)[:400],)
    return json.loads(proc.stdout)


@pytest.mark.parametrize("name,payload", [
    ("refusal", REFUSAL), ("rescued inner object", RESCUED_INNER),
])
def test_the_browser_transport_has_the_same_blind_spot(name, payload):
    """Both sides, so fixing one without the other fails here."""
    got = _js(dict(payload))
    out, mm = got["out"], got["mm"]
    assert "truncated_or_unrecognized_payload" in (out.get("_warnings") or []), (
        "%s: the JS guard stopped warning -- re-read the normaliser" % name)
    if FIX_ME:
        assert mm, "%s: the browser must report a refusal as unusable" % name
    else:
        assert mm == "", (
            "%s: expected rcaPayloadMismatch to be short-circuited by the "
            "catch-alls, got %r -- the JS side may already be fixed, in "
            "which case set FIX_ME and check the Python side" % (name, mm))
