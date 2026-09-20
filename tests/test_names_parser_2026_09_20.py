"""BORROW-2026-09-20 (GBIF naming domain) - parser pre-resolution, multiple
equal matches, and the two NONE flavours in ``rca_core.names``.

Borrowed conclusion (offline, no network): in GBIF practice palaeontological
genus names (radiolarians in particular) frequently come back as
``matchType: "NONE"`` or ``"Multiple equal matches"`` (the same name across
kingdoms/phyla). The module must therefore

1. pre-parse the cleaned string through ``/v1/parsers/name`` and carry the
   structured genus / specificEpithet / infraspecificEpithet / authorship
   into the backbone stage (genus-only follow-up query), keeping the raw
   input string;
2. NEVER auto-pick a winner on "Multiple equal matches" - surface
   ``ambiguous: true`` + candidates (usageKey + rank + kingdom/phylum);
3. separate "malformed string refused locally" (zero network) from
   "parser accepts it, backbone does not" (``unmatched_reason``);
4. reuse the ``rca_core.cache.ResultCache`` duck-typed interface
   (make_key/get/put) for cross-run reuse of the verification - including
   the pre-parsed result - without touching cache.py itself.

All HTTP is mocked through the module's existing ``fetch(url)`` seam;
``base_url`` is injected so the tests never resolve a real host.
"""
from __future__ import annotations

import json

from rca_core.names import (
    DEFAULT_GBIF_BASE_URL,
    clean_name_for_lookup,
    looks_malformed_name,
    name_issues,
    parse_name_gbif,
    verify_name_gbif,
    verify_names,
)

BASE = "mock://gbif"


def _parser_body(genus="Ptereoconus", species=None, infra=None,
                 authorship="Hoenes, 1891", parsed=True, name=""):
    body = {
        "parsed": parsed,
        "genus": genus,
        "authorship": authorship,
        "type": "Species" if species else "Genus",
        "canonical": name,
    }
    if species:
        body["specificEpithet"] = species
    if infra:
        body["infraspecificEpithet"] = infra
    if parsed is False:
        body = {"parsed": False, "verbatim": name}
    return json.dumps(body)


def _match_body(match_type="EXACT", confidence=100, canonical="Clarkina yini",
                usage_key=8334137, rank="SPECIES", alternatives=None,
                status="ACCEPTED"):
    return json.dumps({
        "matchType": match_type,
        "confidence": confidence,
        "canonicalName": canonical,
        "scientificName": canonical,
        "usageKey": usage_key,
        "rank": rank,
        "status": status,
        "kingdom": "Animalia",
        "phylum": "Mollusca",
        "alternatives": alternatives or [],
    })


class GbifMock:
    """URL-dispatching offline GBIF double: parser vs match endpoints."""

    def __init__(self, parser=None, match=None):
        self.calls: list[str] = []
        self.parser = parser  # callable(name) -> body str, or body str
        self.match = match    # callable(name) -> body str, or body str

    @staticmethod
    def _q(url):
        # the mocked query is everything after "name="
        return url.split("name=", 1)[1] if "name=" in url else ""

    def __call__(self, url: str) -> str:
        self.calls.append(url)
        q = self._q(url)
        if "/v1/parsers/name" in url:
            target = self.parser
        elif "/v1/species/match" in url:
            target = self.match
        else:
            raise AssertionError(f"unexpected endpoint {url!r}")
        if target is None:
            return "{}"
        return target(q) if callable(target) else target


# ---------------------------------------------------------------------------
# 0. injectable base_url (production default preserved)
# ---------------------------------------------------------------------------


def test_default_base_url_is_gbif():
    assert DEFAULT_GBIF_BASE_URL == "https://api.gbif.org"


def test_base_url_injection_hits_both_endpoints_under_base():
    mock = GbifMock(parser=_parser_body(), match=_match_body())
    v = verify_name_gbif("Clarkina yini", fetch=mock, base_url=BASE)
    assert v["status"] == "ok"
    assert mock.calls[0].startswith(BASE + "/v1/parsers/name?name=")
    assert mock.calls[1].startswith(BASE + "/v1/species/match?verbose=true&name=")
    # The legacy default host must NOT leak into an injected-base call.
    assert all(DEFAULT_GBIF_BASE_URL not in u for u in mock.calls)


# ---------------------------------------------------------------------------
# 1. parser pre-resolution feeds the backbone stage
# ---------------------------------------------------------------------------


def test_parse_name_gbif_extracts_structured_fields():
    mock = GbifMock(parser=_parser_body(genus="Ptereoconus", species="longispinus",
                                        infra="minor",
                                        name="Ptereoconus longispinus minor"))
    out = parse_name_gbif("Ptereoconus longispinus minor", fetch=mock,
                          base_url=BASE)
    assert out["status"] == "ok"
    assert out["parsed"] is True
    assert out["genus"] == "Ptereoconus"
    assert out["specific_epithet"] == "longispinus"
    assert out["infraspecific_epithet"] == "minor"
    assert out["authorship"] == "Hoenes, 1891"
    assert "/v1/parsers/name" in mock.calls[0]


def test_parse_name_gbif_fail_open_on_network_error():
    out = parse_name_gbif("Clarkina yini", fetch=lambda url: (_ for _ in ()).throw(
        IOError("dns down")), base_url=BASE)
    assert out["status"] == "unavailable"
    assert out["error"] == "OSError"
    assert out["genus"] == ""


def test_parser_runs_before_match_and_is_kept_in_result():
    mock = GbifMock(parser=_parser_body(species="yini", name="Clarkina yini"),
                    match=_match_body())
    v = verify_name_gbif("Clarkina yini (Meitek, 1970)", fetch=mock,
                         base_url=BASE)
    assert mock.calls[0].startswith(BASE + "/v1/parsers/name")
    assert v["status"] == "ok"
    assert v["parsed"]["genus"] == "Ptereoconus"  # parser verdict recorded
    # the ORIGINAL input string is preserved alongside the queried one
    assert v["original"] == "Clarkina yini (Meitek, 1970)"


def test_parser_outage_does_not_block_the_backbone_query():
    mock = GbifMock(
        parser=lambda q: (_ for _ in ()).throw(RuntimeError("parser 500")),
        match=_match_body())
    v = verify_name_gbif("Clarkina yini", fetch=mock, base_url=BASE)
    assert v["status"] == "ok"                       # backbone still answered
    assert v["parsed"]["status"] == "unavailable"    # parser honestly degraded
    assert len(mock.calls) == 2


def test_unparsable_by_parser_is_reported_as_reason():
    mock = GbifMock(parser=_parser_body(parsed=False, name="zgx qwx"),
                    match=_match_body(match_type="NONE", confidence=0,
                                      canonical="", usage_key=None))
    v = verify_name_gbif("Zgx qwx", fetch=mock, base_url=BASE)
    assert v["status"] == "ok"
    assert v["match_type"] == "NONE"
    assert v["unmatched_reason"] == "unparsable"


# ---------------------------------------------------------------------------
# 2. genus-only follow-up improves backbone coverage (NONE branch)
# ---------------------------------------------------------------------------


def test_genus_fallback_query_on_binomen_miss():
    def match_by_query(q):
        from urllib.parse import unquote
        q = unquote(q)
        if q == "Ptereoconus":
            return _match_body(match_type="EXACT", canonical="Ptereoconus",
                               usage_key=42, rank="GENUS")
        return _match_body(match_type="NONE", confidence=0, canonical="",
                           usage_key=None)

    mock = GbifMock(parser=_parser_body(species="novosp",
                                        name="Ptereoconus novosp"),
                    match=match_by_query)
    v = verify_name_gbif("Ptereoconus novosp", fetch=mock, base_url=BASE)
    assert v["match_type"] == "NONE"
    assert v["unmatched_reason"] == "not_in_backbone"   # parser parsed it
    assert v["genus_match"] is not None
    assert v["genus_match"]["usage_key"] == 42
    assert v["genus_match"]["rank"] == "GENUS"
    assert any(u.endswith("name=Ptereoconus") for u in mock.calls)


def test_genus_fallback_skipped_for_bare_genus():
    mock = GbifMock(parser=_parser_body(name="Ptereoconus"),
                    match=_match_body(match_type="NONE", confidence=0,
                                      canonical="", usage_key=None))
    v = verify_name_gbif("Ptereoconus", fetch=mock, base_url=BASE)
    # genus == query -> no duplicated round-trip
    assert v["genus_match"] is None
    assert len(mock.calls) == 2


# ---------------------------------------------------------------------------
# 3. "Multiple equal matches" -> ambiguous, candidates, no arbitrary pick
# ---------------------------------------------------------------------------

_RADIOLARIAN_ALTS = [
    {"usageKey": 111, "canonicalName": "Ptereoconus", "scientificName":
     "Ptereoconus Hoenes, 1891", "matchType": "EXACT", "confidence": 100,
     "rank": "GENUS", "kingdom": "Chromista", "phylum": "Radioolaria",
     "status": "ACCEPTED"},
    {"usageKey": 222, "canonicalName": "Ptereoconus", "scientificName":
     "Ptereoconus Cook", "matchType": "EXACT", "confidence": 100,
     "rank": "GENUS", "kingdom": "Animalia", "phylum": "Arthropoda",
     "status": "ACCEPTED"},
]


def test_multiple_equal_matches_flags_ambiguous():
    mock = GbifMock(
        parser=_parser_body(name="Ptereoconus"),
        match=_match_body(match_type="Multiple equal matches", confidence=100,
                          canonical="Ptereoconus", usage_key=None,
                          rank="GENUS", alternatives=_RADIOLARIAN_ALTS))
    v = verify_name_gbif("Ptereoconus", fetch=mock, base_url=BASE)
    assert v["status"] == "ok"
    assert v["ambiguous"] is True
    assert [c["usage_key"] for c in v["candidates"]] == [111, 222]
    # candidates carry the disambiguation fields the report/export need
    assert v["candidates"][0]["phylum"] == "Radioolaria"
    assert v["candidates"][1]["kingdom"] == "Animalia"
    issues = name_issues({"Ptereoconus": v})
    assert issues[0]["msg_key"] == "names.ambiguous"
    assert len(issues[0]["candidates"]) == 2
    # no arbitrary first-winner: the row is flagged, not silently resolved
    assert issues[0]["candidates"][0]["usage_key"] != issues[0]["candidates"][1]["usage_key"]


def test_genus_rank_hit_with_equal_alternative_is_ambiguous():
    alt = dict(_RADIOLARIAN_ALTS[1])
    mock = GbifMock(
        parser=_parser_body(name="Ptereoconus"),
        match=_match_body(match_type="EXACT", confidence=100, rank="GENUS",
                          alternatives=[alt]))
    v = verify_name_gbif("Ptereoconus", fetch=mock, base_url=BASE)
    assert v["ambiguous"] is True


def test_specific_epithet_match_with_clean_alternatives_is_not_ambiguous():
    mock = GbifMock(parser=_parser_body(species="yini", name="Clarkina yini"),
                    match=_match_body(alternatives=[]))
    v = verify_name_gbif("Clarkina yini", fetch=mock, base_url=BASE)
    assert v["ambiguous"] is False
    assert name_issues({"Clarkina yini": v}) == []


# ---------------------------------------------------------------------------
# 4. NONE flavours: local malformed gate vs real backbone miss
# ---------------------------------------------------------------------------


def test_looks_malformed_name_local_gate():
    assert looks_malformed_name("Bed 12 top")          # digits
    assert looks_malformed_name("Hindeodus 1979")
    assert looks_malformed_name("粗粒灰岩 Clarkina")     # non-Latin prose
    assert looks_malformed_name("A b c d e")            # more than a quartet
    assert looks_malformed_name("Pseudoalgovella?")     # stray interrogation
    assert looks_malformed_name("")
    assert not looks_malformed_name("Clarkina yini")
    assert not looks_malformed_name("Nankinella discoides")
    assert not looks_malformed_name("Hindeodus parvus")  # three-word trio OK


def test_malformed_string_costs_zero_network():
    mock = GbifMock(parser="{}", match="{}")
    v = verify_name_gbif("Bed 12 marl 3", fetch=mock, base_url=BASE)
    assert v["status"] == "unmatched"
    assert v["unmatched_reason"] == "malformed"
    assert mock.calls == []          # the local gate saved both round-trips
    issues = name_issues({"Bed 12 marl 3": v})
    assert issues[0]["msg_key"] == "names.unmatched"
    assert issues[0]["reason"] == "malformed"


def test_backbone_miss_reason_is_not_in_backbone():
    mock = GbifMock(parser=_parser_body(species="wonderi",
                                        name="Clarkina wonderi"),
                    match=_match_body(match_type="NONE", confidence=0,
                                      canonical="", usage_key=None))
    v = verify_name_gbif("Clarkina wonderi", fetch=mock, base_url=BASE)
    assert v["unmatched_reason"] == "not_in_backbone"
    issues = name_issues({"Clarkina wonderi": v})
    assert issues[0]["reason"] == "not_in_backbone"
    # the two NONE flavours are told apart in the message payload
    assert issues[0]["reason"] != "malformed"


def test_empty_after_clean_reason():
    v = verify_name_gbif("cf. aff. sp.", fetch=lambda url: "{}",
                         base_url=BASE)
    assert v["status"] == "unmatched"
    assert v["unmatched_reason"] == "empty_after_clean"


# ---------------------------------------------------------------------------
# 5. ResultCache-shaped cache reuse (make_key/get/put) - cache.py untouched
# ---------------------------------------------------------------------------


class FakeDiskCache:
    """Duck-typed rca_core.cache.ResultCache (same make_key/get/put trio)."""

    def __init__(self):
        self.store: dict[str, str] = {}
        self.hits = 0

    @staticmethod
    def make_key(**fields):
        import hashlib
        blob = json.dumps(fields, sort_keys=True)
        return hashlib.sha256(blob.encode()).hexdigest()

    def get(self, key):
        raw = self.store.get(key)
        if raw is None:
            return None
        self.hits += 1
        return json.loads(raw)

    def put(self, key, value):
        self.store[key] = json.dumps(value)


def test_second_batch_reuses_disk_cache_including_parser_result():
    disk = FakeDiskCache()
    mock1 = GbifMock(parser=_parser_body(species="yini", name="Clarkina yini"),
                     match=_match_body())
    v1 = verify_names(["Clarkina yini"], fetch=mock1, base_url=BASE,
                      cache=disk)
    assert v1["Clarkina yini"]["parsed"]["genus"] == "Ptereoconus"
    assert len(mock1.calls) == 2

    def exploding_fetch(url):
        raise AssertionError("cache hit must not touch the network")

    v2 = verify_names(["Clarkina yini"], fetch=exploding_fetch,
                      base_url=BASE, cache=disk)
    assert disk.hits == 1
    assert v2["Clarkina yini"]["status"] == "ok"
    # the cached pre-parse rides along
    assert v2["Clarkina yini"]["parsed"]["genus"] == "Ptereoconus"


def test_unavailable_results_are_not_cached():
    disk = FakeDiskCache()
    boom = GbifMock(parser=lambda u: (_ for _ in ()).throw(IOError("down")),
                    match=lambda u: (_ for _ in ()).throw(IOError("down")))
    v1 = verify_names(["Clarkina yini"], fetch=boom, base_url=BASE,
                      cache=disk)
    assert v1["Clarkina yini"]["status"] == "unavailable"
    assert disk.store == {}   # a transient outage must not poison the cache
    ok = GbifMock(parser=_parser_body(), match=_match_body())
    v2 = verify_names(["Clarkina yini"], fetch=ok, base_url=BASE, cache=disk)
    assert v2["Clarkina yini"]["status"] == "ok"
    assert disk.store != {}


def test_broken_cache_degrades_to_miss_not_error():
    class Broken(FakeDiskCache):
        def get(self, key):
            raise RuntimeError("database is locked")

        def put(self, key, value):
            raise RuntimeError("disk full")

    mock = GbifMock(parser=_parser_body(), match=_match_body())
    v = verify_names(["Clarkina yini"], fetch=mock, base_url=BASE,
                     cache=Broken())
    assert v["Clarkina yini"]["status"] == "ok"
    assert len(mock.calls) == 2


# ---------------------------------------------------------------------------
# 6. backward compatibility of the public contract
# ---------------------------------------------------------------------------


def test_old_keys_and_call_form_still_work():
    mock = GbifMock(
        parser=_parser_body(),
        match=_match_body(alternatives=[{
            "usageKey": 9, "canonicalName": "Clarkina cf yini",
            "matchType": "FUZZY", "confidence": 60, "status": "SYNONYM"}]))
    # old positional form (name, timeout, fetch) unchanged
    v = verify_name_gbif("Clarkina yini", 5.0, mock)
    for key in ("status", "match_type", "confidence", "canonical", "accepted",
                "alternatives", "taxonomic_status"):
        assert key in v, key
    assert v["alternatives"][0].keys() == {
        "canonical", "match_type", "confidence", "status"}
    # SPECIES-rank EXACT is not flagged by the genus-rank ambiguity rule
    assert v["ambiguous"] is False


def test_unmatched_and_unavailable_shapes_keep_old_keys():
    m = verify_names(["12 34 56"], fetch=lambda url: _match_body())
    r = m["12 34 56"]
    assert r["status"] == "unmatched" and r["match_type"] == "NONE"
    assert r["confidence"] == 0.0 and r["alternatives"] == []
    assert clean_name_for_lookup("Clarkina yini") == "Clarkina yini"
