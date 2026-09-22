"""FIX-2026-09-22 - fix-agent B regression tests (rca_core.names + i18n).

Covers the four confirmed audit bugs in this domain, all OFFLINE (every
transport is mocked at the fetch/urlopen boundary and the mocks mirror the
LIVE GBIF contract verified during the 2026-09-22 audit):

1. ``/v1/parser/name`` (SINGULAR) returns an ARRAY of records with
   ``genusOrAbove`` / ``canonicalName`` - the old plural URL + dict +
   ``genus``/``canonical`` reads made parse_name_gbif permanently
   "unavailable", so the genus-fallback follow-up never fired.
2. Non-parenthesised author + year tails ("Ptereoconus hoenesi Hoenes,
   1891") were flagged ``malformed`` with zero network.
3. ``urllib.request.urlopen`` rides the PROCESS-GLOBAL opener; importing
   rca_core installs the SSRF-pinning opener there, and the pinned edge
   answers api.gbif.org with a vhost 404. names.py must use a PRIVATE
   opener (rca_core.names._private_open), like scripts/update_ics.py.
4. ``names.ambiguous`` was emitted with no i18n entry on any side (py +
   js) - the Python catalog must define it (and the whole coverage-ledger
   / reason_code catalog) in ALL THREE languages (item 5 on this side).
"""
from __future__ import annotations

import json
import urllib.request

import pytest

from rca_core import names as names_mod
from rca_core.names import (
    clean_name_for_lookup,
    looks_malformed_name,
    name_issues,
    parse_name_gbif,
    verify_name_gbif,
)

BASE = "mock://gbif"

# The exact key set the coverage round authored zh-only (bug item 5).
COVERAGE_KEYS = [
    "quality.coverage_ledger",
    "quality.coverage_reasons",  # display wrapper consumed by js/app.js
] + [f"reason_code.{slug}" for slug in (
    "not_drawn", "uncertain", "obscured", "inferred", "legend_only",
    "crosses_top", "crosses_base", "truncated", "no_label", "abbreviated",
    "low_confidence", "out_of_scope",
)]


def _parser_array_body(canonical="Ptereoconus hoenesi", genus="Ptereoconus",
                       species="hoenesi", authorship="Hoenes, 1891"):
    """The REAL live shape: a JSON ARRAY of parsed-name records."""
    return json.dumps([{
        "verbatim": canonical,
        "type": "Species",
        "genusOrAbove": genus,
        "specificEpithet": species,
        "canonicalName": canonical,
        "authorship": authorship,
        "parsed": True,
    }])


# ---------------------------------------------------------------------------
# 1. parser URL / response shape / field names
# ---------------------------------------------------------------------------


def test_parser_endpoint_is_singular_and_no_plural_remains():
    url = names_mod._parser_endpoint("https://api.gbif.org", "Clarkina yini")
    assert url.startswith("https://api.gbif.org/v1/parser/name?name=")
    assert "parsers" not in url  # the plural path is a permanent 404


def test_parse_name_gbif_accepts_real_array_contract():
    seen = []

    def fetch(url):
        seen.append(url)
        return _parser_array_body()

    out = parse_name_gbif("Ptereoconus hoenesi", fetch=fetch, base_url=BASE)
    assert out["status"] == "ok"
    assert out["parsed"] is True
    # genusOrAbove / canonicalName are the live field names; the old
    # genus / canonical reads silently produced "" forever.
    assert out["genus"] == "Ptereoconus"
    assert out["specific_epithet"] == "hoenesi"
    assert out["canonical"] == "Ptereoconus hoenesi"
    assert out["authorship"] == "Hoenes, 1891"
    assert "/v1/parser/name" in seen[0]


def test_parse_name_gbif_picks_exact_canonical_from_multi_record_array():
    body = json.dumps([
        {"verbatim": "junk", "type": None},
        {"genusOrAbove": "Clarkina", "canonicalName": "Clarkina yini",
         "specificEpithet": "yini", "type": "Species"},
        {"genusOrAbove": "Other", "canonicalName": "Other thing"},
    ])
    out = parse_name_gbif("Clarkina yini", fetch=lambda u: body, base_url=BASE)
    assert out["status"] == "ok"
    assert out["genus"] == "Clarkina"


def test_parse_name_gbif_empty_array_is_unparsable_not_ok():
    out = parse_name_gbif("zgx", fetch=lambda u: "[]", base_url=BASE)
    assert out["status"] == "unavailable"
    assert out["error"] == "empty_parse_result"


def test_genus_fallback_query_fires_on_backbone_miss():
    """The production effect of bug 1: with the parser permanently
    unavailable the genus-only follow-up could NEVER fire. With the real
    contract restored, a NONE binomen must spend the extra genus query."""
    calls = []

    def fetch(url):
        calls.append(url)
        if "/v1/parser/name" in url:
            return _parser_array_body(canonical="Ptereoconus novosp",
                                      species="novosp")
        if url.endswith("name=Ptereoconus"):
            return json.dumps({"matchType": "EXACT", "confidence": 100,
                               "canonicalName": "Ptereoconus",
                               "scientificName": "Ptereoconus",
                               "usageKey": 42, "rank": "GENUS"})
        return json.dumps({"matchType": "NONE", "confidence": 0,
                           "canonicalName": "", "usageKey": None})

    v = verify_name_gbif("Ptereoconus novosp", fetch=fetch, base_url=BASE)
    assert v["status"] == "ok"
    assert v["parsed"]["status"] == "ok"        # not "unavailable"
    assert v["genus_match"] is not None          # fallback actually ran
    assert v["genus_match"]["usage_key"] == 42
    assert any("/v1/species/match" in u and u.endswith("name=Ptereoconus")
               for u in calls)


# ---------------------------------------------------------------------------
# 2. non-parenthesised author + year tails
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw,expected", [
    ("Ptereoconus hoenesi Hoenes, 1891", "Ptereoconus hoenesi"),
    ("Clarkina yini Jiang and Wang, 2001", "Clarkina yini"),
    ("Clarkina yini Jiang et al., 2001", "Clarkina yini"),
    ("Clarkina yini Jiang et al. 2001", "Clarkina yini"),  # no comma
    ("Clarkina yini Jiang, 2001a", "Clarkina yini"),
    ("Clarkina yini (Jiang, 2001)", "Clarkina yini"),      # parenthesised
    ("Clarkina yini", "Clarkina yini"),                    # untouched
])
def test_clean_strips_author_year_tails(raw, expected):
    assert clean_name_for_lookup(raw) == expected


def test_cited_name_reaches_the_network_not_the_malformed_gate():
    """The live repro: this used to return unmatched_reason='malformed'
    with ZERO calls despite GBIF parsing it fine."""
    calls = []

    def fetch(url):
        calls.append(url)
        if "/v1/parser/name" in url:
            assert "Hoenes" not in url, "the tail must be cleaned away"
            return _parser_array_body()
        return json.dumps({"matchType": "EXACT", "confidence": 100,
                           "canonicalName": "Ptereoconus hoenesi",
                           "scientificName": "Ptereoconus hoenesi",
                           "usageKey": 7, "rank": "SPECIES"})

    v = verify_name_gbif("Ptereoconus hoenesi Hoenes, 1891", fetch=fetch,
                         base_url=BASE)
    assert v["status"] == "ok"
    assert v["unmatched_reason"] == ""
    assert len(calls) == 2


def test_malformed_gate_stays_conservative():
    # genuine junk still fails closed (documented behaviour)
    assert looks_malformed_name("Bed 12 top")
    assert looks_malformed_name("Hindeodus 1979")   # bare year, no author
    assert looks_malformed_name("粗粒灰岩 Clarkina")
    assert looks_malformed_name("Clarkina yini, 2001")  # year, no surname
    assert not looks_malformed_name("Ptereoconus hoenesi Hoenes, 1891")
    # commas / parens alone are no longer a rejection signal
    assert not looks_malformed_name("Clarkina (yini)")


def test_verify_bare_year_junk_still_zero_network():
    mock_calls = []

    def fetch(url):
        mock_calls.append(url)
        return "{}"

    v = verify_name_gbif("Hindeodus 1979", fetch=fetch, base_url=BASE)
    assert v["status"] == "unmatched"
    assert v["unmatched_reason"] == "malformed"
    assert mock_calls == []


# ---------------------------------------------------------------------------
# 3. private opener vs the installed global (SSRF-pinned) opener
# ---------------------------------------------------------------------------


def test_default_transport_uses_private_opener_not_the_global_one(monkeypatch):
    """Repro of the production defect: importing rca_core already installs
    the SSRF-pinning opener GLOBALLY, so urlopen-based GBIF calls hit the
    pinned edge and get a vhost 404. names.py must build its own opener."""
    requests_seen = []

    class FakeResponse:
        body = json.dumps({
            "matchType": "EXACT", "confidence": 100,
            "canonicalName": "Clarkina yini",
            "scientificName": "Clarkina yini", "usageKey": 1,
            "rank": "SPECIES",
        }).encode()

        def read(self):
            return self.body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class FakeOpener:
        def open(self, req, timeout=None):
            requests_seen.append(req.full_url)
            return FakeResponse()

    def poisoned_global_open(*args, **kwargs):
        raise AssertionError("the process-global opener must not be used "
                             "for GBIF lookups (SSRF pinning breaks api.gbif.org)")

    real_build = urllib.request.build_opener
    monkeypatch.setattr(urllib.request, "build_opener",
                        lambda *a, **k: FakeOpener())
    monkeypatch.setattr(urllib.request, "urlopen", poisoned_global_open)
    try:
        # The global opener currently installed is the SSRF one (rca_core
        # import side effect) - simulate "it answers 404 from the edge" by
        # ALSO installing an opener that would explode if consulted.
        class ExplodingOpener(urllib.request.OpenerDirector):
            def open(self, req, *a, **k):
                raise AssertionError("global opener consulted")
        installed = urllib.request._opener
        urllib.request.install_opener(ExplodingOpener())
        try:
            v = verify_name_gbif("Clarkina yini")  # fetch=None -> real path
        finally:
            urllib.request._opener = installed
    finally:
        monkeypatch.undo()
        assert urllib.request.build_opener is real_build
    assert v["status"] == "ok"
    assert len(requests_seen) == 2  # parser + match, both private-opener
    assert all(u.startswith("https://api.gbif.org/v1/") for u in requests_seen)
    assert any("/v1/parser/name" in u for u in requests_seen)


def test_ssrf_comment_no_longer_claims_gbif_pinning_is_safe():
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "rca_core" / "ssrf.py"
           ).read_text(encoding="utf-8")
    assert "MEASURED the opposite" in src
    # the old assertion "pinning protects them" for the GBIF lookups is gone
    assert "pinning protects them from the\n    same rebinding attack" not in src


# ---------------------------------------------------------------------------
# 4/5. i18n completeness on the Python side (names.ambiguous + coverage set)
# ---------------------------------------------------------------------------


def test_names_ambiguous_defined_in_all_three_languages():
    from rca_core.i18n import TRANSLATIONS
    for lang in ("zh", "en", "ja"):
        text = TRANSLATIONS[lang].get("names.ambiguous")
        assert text and "{name}" in text, lang
        assert text != "names.ambiguous"


def test_coverage_catalog_complete_in_all_three_languages():
    from rca_core.i18n import TRANSLATIONS
    for key in COVERAGE_KEYS:
        for lang in ("zh", "en", "ja"):
            assert TRANSLATIONS[lang].get(key), f"{key} missing in {lang}"


def test_all_emitted_name_issue_keys_resolve():
    """Every msg_key name_issues() can emit must exist in every locale."""
    from rca_core.i18n import TRANSLATIONS
    verification = {
        "A a": {"status": "ok", "match_type": "FUZZY", "confidence": 90,
                "canonical": "B b"},
        "C c": {"status": "ok", "match_type": "Multiple equal matches",
                "confidence": 100, "canonical": "D d", "ambiguous": True,
                "candidates": []},
        "E e": {"status": "ok", "match_type": "NONE", "confidence": 0,
                "canonical": "", "unmatched_reason": "not_in_backbone"},
    }
    keys = {i["msg_key"] for i in name_issues(verification)}
    assert keys == {"names.fuzzy", "names.ambiguous", "names.unmatched"}
    for lang in ("zh", "en", "ja"):
        for key in keys:
            assert key in TRANSLATIONS[lang], f"{key} missing in {lang}"


def test_py_locale_key_sets_are_identical_for_locked_namespaces():
    """Completeness: no col/sec/quality/names/reason_code key may exist in
    only one locale (the lang->en->raw-key fallback leaks the dotted key)."""
    import re
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "rca_core" / "i18n.py"
           ).read_text(encoding="utf-8")
    rx = re.compile(r'^    "((?:col|sec|quality|names|reason_code)\.[A-Za-z0-9_]+)":',
                    re.M)
    per = {}
    for lang in ("zh", "en", "ja"):
        start = src.index(f'TRANSLATIONS["{lang}"] = {{')
        body = src[start:src.index("\n}", start)]
        per[lang] = {m.group(1) for m in rx.finditer(body)}
    all_keys = per["zh"] | per["en"] | per["ja"]
    assert all_keys
    for lang in ("zh", "en", "ja"):
        assert per[lang] == all_keys, (
            f"{lang} differs: "
            f"missing={sorted(all_keys - per[lang])} extra={sorted(per[lang] - all_keys)}")
