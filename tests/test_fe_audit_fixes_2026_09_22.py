"""FE-AUDIT-2026-09-22 regressions (fix-agent Q) — Python side.

Three jobs, all OFFLINE:

1. (item 7) ``_axis_domain`` used to let a bool ``unit`` through the
   ``isinstance(unit, (str, int, float))`` gate (bool IS an int subclass)
   and export the unit ``"True"``, while the JS mirror (js/minimax.js
   ``rcaAxisDomain``) emitted ``""``. The chosen convention is to REJECT a
   bool unit on BOTH engines; this pins the Python half.

2. (item 6) ND_FLOAT_CASES drift guard: the case table embedded in
   tests_contract_mirror_2026_09_22.js is replayed here against CPython's
   own ``float()`` — the exact oracle the JS ``rcaPyFloatOrNull`` mirror is
   now supposed to follow for Unicode Nd digits ("٥٠٠" -> 500.0,
   "１２７" -> 127.0) and the underscore grammar ("１_２" -> 12.0,
   "_１２"/"１２_"/"1__2"/"1e_2" raise). Neither side can drift silently.

3. (item 4) MALFORMED_CASES drift guard: the (raw -> cleaned, malformed)
   table shared with tests_names_i18n_fixes_2026_09_22.js is recomputed
   against ``rca_core.names`` so the new JS ``rcaLooksMalformedName`` gate
   cannot diverge from ``looks_malformed_name`` (incl. the FIX-2026-09-22
   author-tail relaxation that keeps "Pteroconus Hoenes, 1891" queryable).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from rca_core.extractor import _axis_domain, normalize_result
from rca_core.names import clean_name_for_lookup, looks_malformed_name

ROOT = Path(__file__).resolve().parents[1]
MIRROR_JS = ROOT / "tests_contract_mirror_2026_09_22.js"
NAMES_JS = ROOT / "tests_names_i18n_fixes_2026_09_22.js"


def _marker_json(source: str, begin: str, end: str, const: str) -> list:
    """Pull a ``JSON.parse(`...`)`` table out of a marked JS test file."""
    m = re.search(
        re.escape(begin) + r"\s*(?://[^\n]*\n)*const " + re.escape(const)
        + r" = JSON\.parse\(`(.*?)`\)",
        source, re.S)
    assert m, f"{const} markers lost from {source[:40]!r}"
    return json.loads(m.group(1))


# ---------------------------------------------------------------------------
# 1. bool axis unit -> "" (mirror of js/minimax.js rcaAxisDomain)
# ---------------------------------------------------------------------------


def test_axis_domain_rejects_bool_unit():
    assert _axis_domain({"at_0": 0, "at_999": 25, "unit": True}) == \
        {"at_0": 0.0, "at_999": 25.0, "unit": ""}
    assert _axis_domain({"at_0": 0, "at_999": 25, "unit": False}) == \
        {"at_0": 0.0, "at_999": 25.0, "unit": ""}


def test_axis_domain_scalar_units_still_stringify():
    # the guard must not eat legitimate str/int/float units
    assert _axis_domain({"at_0": 0, "at_999": 1, "unit": "m"})["unit"] == "m"
    assert _axis_domain({"at_0": 0, "at_999": 1, "unit": 3})["unit"] == "3"
    assert _axis_domain({"at_0": 0, "at_999": 1, "unit": 2.5})["unit"] == "2.5"
    assert _axis_domain({"at_0": 0, "at_999": 1, "unit": ["m"]})["unit"] == ""


def test_normalize_result_bool_unit_exported_empty():
    payload = {
        "sections": [{"name": "S1"}],
        "species_ranges": [{"species": "A a", "section": "S1", "range_top": "9"}],
        "biozones": [], "other_fossils": [], "confidence": 0.8,
        "axis_calibration": {"vertical": {"at_0": 0, "at_999": 25, "unit": True}},
    }
    out = normalize_result(payload)
    assert out["axis_calibration"]["vertical"]["unit"] == ""


# ---------------------------------------------------------------------------
# 2. Nd-digit float grammar: the JS mirror's table IS CPython's behaviour
# ---------------------------------------------------------------------------


def _nd_cases() -> list:
    cases = _marker_json(MIRROR_JS.read_text(encoding="utf-8"),
                         "__RCA_ND_FLOAT_CASES_BEGIN__",
                         "__RCA_ND_FLOAT_CASES_END__", "ND_FLOAT_CASES")
    assert len(cases) >= 15, "the Nd fuzz table shrank below the audit's set"
    return cases


def test_nd_float_cases_match_cpython_float():
    for text, want in _nd_cases():
        try:
            got: float | None = float(text)
        except (ValueError, TypeError):
            got = None
        if want is None:
            assert got is None, ascii(text)
        else:
            assert got is not None and got == pytest.approx(want), ascii(text)


def test_nd_transliteration_covers_both_script_families():
    # Arabic-Indic AND fullwidth, digits AND exponents - the two families
    # the live probe dropped geometry points for.
    assert float("٥٠٠") == 500.0
    assert float("１２７") == 127.0
    assert float("١.٢e٣") == 1200.0
    with pytest.raises(ValueError):
        float("１ｅ５")  # fullwidth 'e' is Ll, not Nd — stays rejected


# ---------------------------------------------------------------------------
# 3. malformed-gate parity table (js/app.js rcaLooksMalformedName mirror)
# ---------------------------------------------------------------------------


def _malformed_cases() -> list:
    cases = _marker_json(NAMES_JS.read_text(encoding="utf-8"),
                         "__RCA_MALFORMED_CASES_BEGIN__",
                         "__RCA_MALFORMED_CASES_END__", "MALFORMED_CASES")
    assert len(cases) >= 15, "the adversarial name table shrank"
    return cases


def test_malformed_table_matches_python_pipeline():
    for raw, want_clean, want_mal in _malformed_cases():
        cleaned = clean_name_for_lookup(raw)
        assert cleaned == want_clean, ascii(raw)
        if cleaned:  # py: empty_after_clean never reaches the gate
            assert looks_malformed_name(cleaned) is want_mal, ascii(raw)


def test_malformed_names_never_reach_the_network():
    """The exact strings the FE audit caught the browser NETWORKING."""
    calls = []

    def fetch(url):  # pragma: no cover - must never be called
        calls.append(url)
        return "{}"

    from rca_core.names import verify_name_gbif
    for junk in ("Genus 1979", "Foo et al. 2001"):
        v = verify_name_gbif(junk, fetch=fetch, base_url="mock://gbif")
        assert v["status"] == "unmatched"
        assert v["unmatched_reason"] == "malformed"
    assert calls == []


def test_cited_author_tail_stays_queryable():
    """FIX-2026-09-22 item 2 relaxation, now pinned AGAINST the new gate:
    the gate must reject junk, not citations ('Hoenes, 1891' survives)."""
    assert not looks_malformed_name("Pteroconus Hoenes, 1891")
    assert not looks_malformed_name("Ptereoconus hoenesi Hoenes, 1891")
    assert looks_malformed_name("Ptereoconus hoenesi var novus thing")
