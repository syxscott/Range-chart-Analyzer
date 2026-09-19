"""The Python half of the 2026-09-20 frontend-parity contract.

REVIEW-2026-09-20 (frontend parity): ``js/minimax.js`` mirrors the extraction
pipeline for the browser-only transport, and both the JS regression suite
(``tests_frontend.js``) and the differential replay
(``tests_diff_frontend_parity.js``) read ``rca_core`` as the single source of
truth. This module keeps that claim honest on the Python side:

  1. ``test_fixture_matches_python`` re-runs ``tests/gen_frontend_parity_fixtures.py``
     and compares it against the COMMITTED fixture, so the answers the JS
     replays cannot silently drift away from the code they were generated from.
  2. ``test_extract_guard_matrix`` records the unusable-payload guard answers
     (``ok`` / ``error_key`` / ``warning``) for the modes the browser can serve.
     Those exact triples are what ``tests_frontend.js`` asserts against the JS
     ``extractRangeChart``, so a Python-side change to the guard shows up here
     first instead of as an "unexplained" frontend failure.

The guard itself lives in two places, which is the part every earlier test got
wrong: ``normalize_result`` (range_chart only) tags a foreign root with
``truncated_or_unrecognized_payload`` and ``extract_range_chart`` turns that tag
into a hard error BEFORE the shared ``_ok_result``; the other modes get no such
tag from their normalizer, so they stay ``ok=True`` and only trip ``_ok_result``
when the payload produced literally nothing.
"""
from __future__ import annotations

import copy
import importlib.util
import json
import warnings
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]

_SPEC = importlib.util.spec_from_file_location(
    "gen_frontend_parity_fixtures",
    str(ROOT / "tests" / "gen_frontend_parity_fixtures.py"),
)
GEN = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(GEN)

from rca_core import extractor as E  # noqa: E402

# extractor.py:1359-1360 — the prose every mode puts in ``warning``.
TRUNCATION_PROSE = (
    "Result may be truncated (model hit max_tokens). "
    "Try raising the max_tokens setting and re-running."
)
_RESCUED = " | rescued inner object: unusable"
_NO_ROWS = " | truncated output rescued no usable records"
_USAGE = {"input_tokens": 1, "output_tokens": 1}


def _run(mode: str, text: str, truncated: bool) -> E.ExtractResult:
    """Call ``extract_<mode>`` with the transport stubbed to return ``text``."""
    with patch.object(E, "call_llm_api",
                      return_value=(text, truncated, 200, "", dict(_USAGE))):
        return getattr(E, "extract_" + mode)(
            api_key="k", image_b64="QUFB", media_type="image/png",
            base_url="https://example.com", model="m", max_tokens=100)


def _jsonable(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True))


# ---------------------------------------------------------------------------
# 1. Fixture drift guard
# ---------------------------------------------------------------------------

def test_fixture_matches_python():
    """The committed differential fixture must be what Python answers today."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        fresh = GEN.build_fixture()
    stored = json.loads(GEN.FIXTURE_PATH.read_text(encoding="utf-8"))
    assert len(fresh["cases"]) == len(stored["cases"]), (
        "case list changed: re-run `python tests/gen_frontend_parity_fixtures.py`")
    for want, have in zip(fresh["cases"], stored["cases"]):
        assert want["id"] == have["id"]
        assert _jsonable(want) == _jsonable(have), (
            "%s: the fixture no longer matches rca_core — regenerate it before "
            "updating the JS mirrors" % want["id"])


# ---------------------------------------------------------------------------
# 2. Extract-level guard oracle (mirrored by tests_frontend.js)
# ---------------------------------------------------------------------------

_FOREIGN = json.dumps({"totally_unrelated_key": 1})
_EMPTY_RC = json.dumps({"sections": []})
_EMPTY_AB = json.dumps({"sites": [], "abundances": [], "zones": []})
_EMPTY_ZO = json.dumps({"zonations": [], "zones": [], "correlations": []})
_ARRAY_ROOT_JUNK = json.dumps({"_array_root": [{"zzz": 1}]})

GUARD_MATRIX = [
    # (mode, payload, transport-truncated, ok, error_key, warning suffix)
    ("range_chart", _FOREIGN, False, False, "err.parse", _RESCUED),
    ("columnar_section", _FOREIGN, False, True, None, ""),
    ("abundance_diagram", _FOREIGN, False, True, None, ""),
    ("zonation_chart", _FOREIGN, False, True, None, ""),
    # Nothing rescued at all -> the SHARED guard fires on every mode.
    ("range_chart", _EMPTY_RC, True, False, "err.parse", _NO_ROWS),
    ("columnar_section", _EMPTY_RC, True, False, "err.parse", _NO_ROWS),
    ("abundance_diagram", _EMPTY_AB, True, False, "err.parse", _NO_ROWS),
    ("zonation_chart", _EMPTY_ZO, True, False, "err.parse", _NO_ROWS),
    # ... but only when nothing was rescued: `_unclassified` counts as content.
    ("range_chart", _ARRAY_ROOT_JUNK, True, True, None, ""),
    ("range_chart", _EMPTY_RC, False, True, None, ""),
]


@pytest.mark.parametrize("mode,text,trunc,exp_ok,exp_key,exp_suffix", GUARD_MATRIX)
def test_extract_guard_matrix(mode, text, trunc, exp_ok, exp_key, exp_suffix):
    res = _run(mode, text, trunc)
    expected_warning = (TRUNCATION_PROSE + exp_suffix) if (
        exp_suffix or (exp_ok and trunc)) else ""
    assert res.ok is exp_ok, (mode, text, res.error_key, res.warning)
    assert res.error_key == exp_key
    assert res.warning == expected_warning
    if exp_ok and trunc:
        assert res.warning == TRUNCATION_PROSE


def test_foreign_range_chart_data_keeps_the_flag_and_the_keys():
    """The error result still carries the normalized payload: the tag in
    ``_warnings`` (what the UI greps for) and the foreign key in ``_extras``
    (H8: nothing is dropped on the floor)."""
    res = _run("range_chart", _FOREIGN, False)
    assert res.ok is False
    assert res.data["_warnings"] == ["truncated_or_unrecognized_payload"]
    assert res.data["_extras"] == {"totally_unrelated_key": 1}


def test_foreign_other_modes_are_not_flagged():
    """Only ``normalize_result`` raises the tag — the other normalizers park
    the foreign root in ``_extras`` and answer ok=True, unflagged."""
    for mode in ("columnar_section", "abundance_diagram", "zonation_chart"):
        res = _run(mode, _FOREIGN, False)
        assert res.ok is True, mode
        assert "_warnings" not in res.data, mode
        assert res.data["_extras"] == {"totally_unrelated_key": 1}, mode


# ---------------------------------------------------------------------------
# 3. Normalizer-level contracts the rewritten JS tests assert
# ---------------------------------------------------------------------------

def normalize_abundance(payload: dict) -> dict:
    """normalize_abundance_result on a COPY — it mutates the payload."""
    return E.normalize_abundance_result(copy.deepcopy(payload))


def test_abundance_array_root_bucketing_key_set():
    """Bucket on KEY PRESENCE (extractor.py:1941-1955), not on value names.

    ``site_id`` / ``count`` / ``zone`` are the spellings the browser copy used
    to accept; they are unclassifiable for Python and must survive under
    ``_extras._unclassified`` — and the documented spellings must bucket.
    """
    legacy = normalize_abundance({"_array_root": [
        {"site_id": "S1", "location": "Loc1"},
        {"abundance": "A", "count": 5},
        {"zone": "Z1", "assemblage": "ass"},
    ], "_note": "wrap"})
    assert legacy["sites"] == legacy["abundances"] == legacy["zones"] == []
    assert len(legacy["_extras"]["_unclassified"]) == 3
    # _pop_array_root_extras: the wrapper never duplicates into _extras.
    assert "_array_root" not in legacy["_extras"]
    assert "_note" not in legacy["_extras"]

    buckets = normalize_abundance({"_array_root": [
        {"name": "S1", "location": "Loc1"},
        {"name": "S2", "depth_unit": "m"},
        {"taxon": "Pinus", "level": "3"},
        {"abundance": "20%", "level": "3"},
        {"name": "Z1", "age": "Holocene"},
    ], "confidence": "0.5"})
    assert [s["name"] for s in buckets["sites"]] == ["S1", "S2"]
    assert len(buckets["abundances"]) == 2
    assert [z["name"] for z in buckets["zones"]] == ["Z1"]
    # float("0.5") — a STRING confidence parses, "90%" would not.
    assert buckets["confidence"] == 0.5


def test_phylo_metadata_reads_only_the_metadata_block():
    """extractor.py:2298-2319: the six canonical keys come from
    ``raw["metadata"]`` and so do the preserved legacy extras. A root-level
    sibling is an undocumented ROOT key -> ``_extras``, never ``metadata``."""
    at_root = E._normalize_phylogenetic_tree_into({
        "version": "1", "taxon_group": "Radiolaria", "total_nodes": 42,
        "root_ids": ["n0"],
        "nodes": [{"id": "n0", "parent": None, "name": "Spasmaria",
                   "is_leaf": False}],
    })
    meta = at_root["metadata"]
    assert sorted(meta) == ["extraction_timestamp", "rooted", "scale", "source",
                            "title", "tree_type"]
    assert meta["source"] == "" and meta["rooted"] is True
    assert at_root["_extras"] == {"version": "1", "taxon_group": "Radiolaria",
                                  "total_nodes": 42}

    inside = E._normalize_phylogenetic_tree_into({
        "metadata": {"taxon_group": "Radiolaria", "total_nodes": 42,
                     "image_source": "file.jpg"},
        "root_ids": ["n0"],
        "nodes": [{"id": "n0", "parent": None, "name": "A"}],
    })
    # `source` defaults to the metadata-level `image_source`, and the key
    # itself is preserved (it is not one of NEW_META_KEYS).
    assert inside["metadata"]["source"] == "file.jpg"
    assert inside["metadata"]["image_source"] == "file.jpg"
    assert inside["metadata"]["taxon_group"] == "Radiolaria"
    assert inside["metadata"]["total_nodes"] == 42
    assert "_extras" not in inside

    # An explicit null `source` wins over the fallback: `dict.get` only
    # defaults when the KEY IS ABSENT (REVIEW-2026-09-10).
    nulled = E._normalize_phylogenetic_tree_into({
        "metadata": {"source": None, "image_source": "img"},
        "root_ids": ["n0"], "nodes": [{"id": "n0", "parent": None, "name": "A"}],
    })
    assert nulled["metadata"]["source"] == ""
    assert nulled["metadata"]["image_source"] == "img"
