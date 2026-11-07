"""Shared helpers/constants for the mode normalizers (Phase 3 / P1-4).

This is a LEAF module. Everything below was MOVED VERBATIM out of
``rca_core/extractor.py``: the 94 definitions are byte-identical AST-node
extracts in their original order, so every historical quirk survives.

The only module-level dependencies are:

* the standard library (``json`` / ``math`` / ``re`` / ``typing``), and
* two already-existing ``rca_core`` LEAF modules -- ``rca_core.reason_codes``
  and ``rca_core.row_flags`` -- which ``extractor.py`` itself already imported
  before this split. They contain no import of ``extractor`` (verified by
  ``tests/test_modes_impl_acyclic.py``), so there is NO reverse edge.

There is deliberately NO reverse import of the parent ``extractor`` module
here (neither module-level nor function-local). The dependency is strictly
one-way: ``extractor -> modes_impl._shared`` and
``modes_impl.<mode> -> modes_impl._shared``.

``extractor.py`` re-exports all 101 names from here at the END of the module,
so every existing ``rca_core.extractor.<name>`` lookup still resolves to the
SAME object (not a copy). See docs/DECISIONS.md.
"""
from __future__ import annotations

import json
import math
import re
from typing import Any, Iterable

from rca_core.reason_codes import (
    LOW_CONFIDENCE_CODE,
    RESPONSE_EXTRACTED,
    RESPONSE_NOT_DRAWN,
    has_value,
    normalize_codes,
    normalize_response_kind,
)
from rca_core.row_flags import add_row_flag as _add_row_flag

VALID_OCCURRENCE_MODES = frozenset({
    "unknown",
    "in_situ",
    "reworked",
    "transported",
    "cavity_fill",
    "bioturbated",
    "derived",
    "lag_deposit",
})


VALID_ENDPOINT_KINDS = frozenset({
    "unknown",
    "observed",
    "projected",
    "truncated",
})


_KNOWN_RANGE_CHART_KEYS = (
    "sections", "species_ranges", "biozones", "other_fossils", "confidence",
    # BORROW-2026-09-20 (B): the optional figure-level axis calibration is a
    # hoisted root key, not an _extras leftover.
    "axis_calibration",
)


_KNOWN_SECTION_KEYS = (
    "name", "age_range", "formations", "formation_thickness_m", "coordinates",
)


_KNOWN_SPECIES_KEYS = (
    "species", "section", "range_top", "range_base", "biozone",
    "author", "year",
    # HIGH fix: author_year is the combined string the prompt requests.
    "author_year", "reworked",
    # P0-3 (REVIEW-2026-07-25): the four fields below are NOW first-class
    # row keys written explicitly by _normalize_species_into, so they must
    # NOT be in _KNOWN_SPECIES_KEYS — otherwise _carry_extras would treat
    # any same-named dict keys emitted by the LLM as known and skip them
    # in the merge field pass (causing double-write via _extras).
    # Note: any leftover occurrences in the source dict are still captured
    # by _carry_extras through the "in _extras" branch — but since they are
    # also written to row[...] explicitly above, the row value wins.
)


_KNOWN_BIOZONE_KEYS = ("name", "section", "age", "thickness_m", "zone_type")


_IRON_RULE_ZONE_RE = re.compile(
    # AUDIT-2026-10-02: explicit ASCII lookarounds instead of `\b`, for the
    # same reason as the zone_type ladder below. Python's `\b` is Unicode-aware
    # on str patterns and js/minimax.js#_RCA_IRON_RULE_ZONE_RE's is not, so for
    # "图zone" the browser matched and this side did not -- and this is the
    # highest-stakes instance of that split, because the rule exists so a
    # zone-like name is never filed as a taxon. Measured on the `range_chart`
    # parity group: with the Unicode-aware spelling the desktop emitted NO
    # `iron_rule_zone_label` warning and no "[zone-mislabel-warning]" note,
    # while the browser emitted both, so a zone label was silently accepted as
    # a species on one endpoint and flagged on the other -- i.e. the
    # "fabricated FAD/LAD for a non-taxon" the comment above describes.
    #
    # A CJK glyph is not a continuation of an ASCII identifier, so ASCII is
    # the right notion of "adjacent identifier character" here. "zozone" and
    # "zoness" are still rejected on both engines, and a space, a hyphen and a
    # dot are still boundaries both engines agree on (rc_iron_rule_boundaries
    # pins that half).
    r"(?<![A-Za-z0-9_])(zone|zonule|assemblage|oppel|interval|lineage|range|acme)"
    r"(?![A-Za-z0-9_])",
    re.IGNORECASE,
)


_NAME_OPEN_NOMINATURE = frozenset({"sp.", "spp.", "sp", "ssp."})


_NAME_QUALIFIERS = frozenset({
    "cf.", "aff.", "subsp.", "var.", "s.l.", "s.str.", "sensu", "nov.",
    "n.sp.", "emend.", "et", "al.",
})


def _name_label_parts(name: Any) -> tuple[list[str], str, bool]:
    """Split a taxon label into ``(name tokens, authority, open_nomenclature)``.

    ``name tokens`` keeps the genus and every epithet that is part of the NAME,
    in print order. A trailing authority is peeled off -- parenthesised
    ("... (Nishimura)") or bare ("... carinatatum Ehrenberg") -- because an
    authority is not an epithet and must not make a name look binomial.

    The authority test is positional, not lexical: it can only START at the
    second token, since the first token is the genus and a genus is always
    capitalised. That is what keeps "Orbula sp." (one name token plus an
    open-nomenclature abbreviation) apart from "Bekoma Foreman" (a genus plus
    an authority), and it needs no authority lexicon, so it cannot mistake a
    capitalised surname for a species epithet.

    ``open_nomenclature`` reports whether an explicit ``sp.``/``spp.`` marked
    the label genus-level. Such a label is complete ON PURPOSE, so it must
    never be treated as a truncated one.
    """
    text = _stringify_scalar(name).strip()
    if not text:
        return [], "", False
    tokens = text.split()
    authority = ""
    # Parenthesised authority anywhere in the label ("... (Nishimura)").
    while tokens and tokens[-1].endswith(")") and tokens[-1].count("("):
        authority = (tokens[-1] + " " + authority).strip()
        tokens.pop()
    # Bare authority: a capitalised token from position 1 onwards, together
    # with the abbreviation run that commonly follows it ("Li and Matsuoka sp.
    # nov.").
    if len(tokens) >= 2 and tokens[1][:1].isupper():
        authority = " ".join(tokens[1:]).strip()
        tokens = tokens[:1]
    # Trailing qualifier run ("... cf.", "... aff.").
    while len(tokens) > 1 and tokens[-1].lower() in _NAME_QUALIFIERS:
        tokens.pop()
    open_nom = any(t.lower() in _NAME_OPEN_NOMINATURE for t in tokens[1:])
    return tokens, authority, open_nom


def _is_ascii_lowercase_start(token: str) -> bool:
    """True when *token* begins with an ASCII lowercase letter.

    The wrap repair reasons about Latin binomials ("robustus", "demissa"), and
    ASCII is the right notion of "identifier character" here for the same
    reason the iron rule and the zone_type ladder above spell their lookarounds
    explicitly: a CJK or Greek letter has no case, so ``str.islower()`` reads it
    as "not capitalised" and a genus like "图zone" would test as a bare epithet.
    """
    return bool(token[:1]) and "a" <= token[0] <= "z"


def _is_wrapped_header_fragment(name: Any) -> bool:
    """True when *name* is ``<epithet> <Authority>`` -- a wrapped header's tail.

    The authority is REQUIRED, and that is the sharpest line in this whole
    repair. A lone lowercase word is not enough evidence: it is exactly what
    the committed parity fixture ``rc_iron_rule_boundaries`` feeds the iron
    rule as a NON-zone ("zozone", "zoness"), so acting on the name alone would
    delete rows a pinned contract says must survive. Pairing an epithet with
    the capitalised surname that follows it is what makes the shape a
    fragment: no taxon is named "robustus Nishimura", but "robustus" and
    "Nishimura" are both real pieces of one wrapped column header.

    The remaining token must start with an ASCII lowercase letter, because a
    genus is capitalised by convention in every one of these payloads, so
    "Orbula sp." / "Reophax spp." and the bare genus "Reophax" are not
    fragments.
    """
    tokens, authority, open_nom = _name_label_parts(name)
    if open_nom or len(tokens) != 1 or not authority:
        return False
    return _is_ascii_lowercase_start(tokens[0])


def _is_bare_epithet(name: Any) -> bool:
    """True when *name* reduces to a lone Latin epithet.

    Weaker than :func:`_is_wrapped_header_fragment` on its own -- a lone
    lowercase word is also what the iron-rule fixtures use for a label that is
    not a zone. So this is only ever consulted as the TAIL of a repair, where
    the preceding row has already been proven to be a genus and nothing else
    and therefore has no other epithet the word could belong to.
    """
    tokens, _authority, open_nom = _name_label_parts(name)
    if open_nom or len(tokens) != 1:
        return False
    return _is_ascii_lowercase_start(tokens[0])


def _is_wrappable_header_base(name: Any) -> bool:
    """True when *name* is a genus and nothing else -- provably incomplete.

    A label with no epithet and no ``sp.``/``spp.`` is a genus on its own, so
    a bare epithet printed immediately after it can only be that genus's
    species: the pairing is forced by the plate, not guessed. "Orbula sp." is
    excluded on purpose -- the abbreviation says the genus is the whole
    determination, so nothing follows it.
    """
    tokens, _authority, open_nom = _name_label_parts(name)
    if open_nom or len(tokens) != 1:
        return False
    return bool(tokens[0][:1].isupper())


def _is_bare_authority(name: Any) -> bool:
    """True when *name* is a single unpunctuated capitalised word.

    That is the shape of an authority that lost its genus ("Foreman",
    "Nishimura") -- and it is AMBIGUOUS, because it is also exactly what a
    genus-level determination looks like. It is therefore never used to flag or
    drop a row: the committed parity fixture ``rc_string_row_biozones`` reads
    ``{"species": "Beta"}`` off a bare string and is a legitimate label, so any
    flag on this shape is a false positive by construction, and deleting on a
    coin-flip is the destructive half of this repository's rule that an
    unsupported row is worse than a missing one. It is used in exactly one
    place: :func:`_guard_wrapped_header_rows` refuses to build a repair on a
    base this ambiguous, so the one decision it drives can only ever refuse.

    A genus followed by ``?`` or ``sp.`` is punctuated or abbreviated and
    returns False.
    """
    tokens, authority, open_nom = _name_label_parts(name)
    if open_nom or authority or len(tokens) != 1:
        return False
    token = tokens[0]
    return bool(token[:1].isupper()) and token.isalpha()


def _row_is_iron_rule_rejected(row: dict[str, Any]) -> bool:
    """True when the iron-rule pass already rejected *row*'s name.

    A label it rejected is a zone label, not a taxon name, so it can be
    neither the tail of a wrapped header nor the genus one completes --
    "Postouwia Zone" reads as genus-plus-authority to the name parser and
    would otherwise swallow the fragment that follows it. The two rules answer
    different questions and the older, fixture-pinned one wins.

    Reads ``_warning`` in both shapes :func:`_add_row_flag` writes (one flag as
    a bare string, several as a list).
    """
    existing = row.get("_warning")
    if isinstance(existing, list):
        return "iron_rule_zone_label" in existing
    return existing == "iron_rule_zone_label"


def _guard_wrapped_header_rows(rows: list[dict[str, Any]],
                                root_warnings: list[str],
                                ) -> list[str]:
    """Repair or drop the rows a two-line column header was split into.

    Returns the text of every row that was removed WITHOUT being rejoined, for
    the caller to preserve under ``_extras`` -- the same "warn, and keep the
    raw text" contract ``_fold_axis_calibration`` uses for a calibration block
    it cannot use. A tail consumed by a repair is not reported, because its
    text is still on the plate, in the row that now carries it.

    Repair fires only when the preceding row is a genus and nothing else, so
    which row the fragment belongs to is decided by the plate rather than by
    list position alone (see :func:`_is_wrappable_header_base`). Everything
    else is dropped, because on the measured figure the two candidate columns
    carry the SAME stem ("Bekoma? cf. demissa" twice) and their endpoint
    readings straddle both columns' bars -- there is no evidence in the
    payload that picks one, and inventing a FAD/LAD would be worse than the
    missing row.

    Three ways a row can be neither dropped nor repaired, all of which leave
    it untouched: the iron rule already rejected it as a non-taxon; it is the
    first row, so there is no base to test it against (a lone epithet is not
    enough on its own); or its predecessor was itself consumed by an earlier
    repair, leaving no base.
    """
    dropped: list[str] = []
    joined = False
    doomed: set[int] = set()
    for i, row in enumerate(rows):
        name = _stringify_scalar(row.get("species"))
        if _row_is_iron_rule_rejected(row) or i == 0 or (i - 1) in doomed:
            continue
        base = rows[i - 1]
        base_name = _stringify_scalar(base.get("species"))
        repairable = (not _row_is_iron_rule_rejected(base)
                      and _is_wrappable_header_base(base_name)
                      and not _is_bare_authority(base_name))
        if repairable:
            if not _is_bare_epithet(name):
                continue
        elif not _is_wrapped_header_fragment(name):
            continue
        else:
            dropped.append(name)
            doomed.add(i)
            continue
        base["species"] = f"{base_name} {name}".strip()
        _add_row_flag(base, "wrapped_header_joined")
        joined = True
        doomed.add(i)
    if not doomed:
        return dropped
    rows[:] = [r for i, r in enumerate(rows) if i not in doomed]
    if joined and "wrapped_header_row_joined" not in root_warnings:
        root_warnings.append("wrapped_header_row_joined")
    if "wrapped_header_fragment_dropped" not in root_warnings:
        root_warnings.append("wrapped_header_fragment_dropped")
    return dropped


def _classify_array_item(item: dict[str, Any]) -> str | None:
    """Classify an item from a top-level array to the appropriate key.

    Returns the root key name (e.g. "sections", "species_ranges") or None if
    the item cannot be classified. Uses distinguishing keys to differentiate
    between section, species_range, biozone, and other_fossils entries.
    """
    if not isinstance(item, dict):
        return None
    # P0-4: explicit zone_type wins over all heuristics.
    zt = item.get("zone_type")
    if isinstance(zt, str):
        zt_lower = zt.strip().lower()
        if zt_lower in {"biozone", "zone", "assemblage_zone", "interval_zone",
                       "lineage_zone", "acme_zone", "oppel_zone", "range_zone",
                       "subzone", "zonule"}:
            return "biozones"
        if zt_lower in {"species_range", "taxon_range", "fad_lad"}:
            return "species_ranges"
        if zt_lower in {"section", "measured_section", "locality"}:
            return "sections"
    # Species ranges have "species" (the primary identifier) and range bounds.
    if "species" in item or ("range_top" in item and "range_base" in item):
        return "species_ranges"
    # P1-8 (REVIEW-2026-07-25): biozone identification now requires the
    # NAME to look like a zone/assemblage label (per prompt.py:80, 195
    # iron-rule markers) OR the item to declare ``zone_type``. Otherwise
    # a {name, age} item is likely a SECTION whose model confused
    # ``age_range`` with ``age`` — misclassifying it would silently move
    # a stratigraphic section into the biozones table, breaking all
    # biozone/range coupling validation downstream.
    name_str = (item.get("name") or "").strip()
    is_zone_label = bool(_IRON_RULE_ZONE_RE.search(name_str))
    if "name" in item and "age" in item and is_zone_label:
        return "biozones"
    # Sections have "name" and typically "age_range" or "formations".
    # ``age`` (without the ``_range`` suffix) is also accepted as a section
    # age signal — the prompt asks for ``age_range`` but lenient parsing
    # is the safer default.
    if "name" in item and ("age_range" in item or "formations" in item
                           or "age" in item):
        return "sections"
    # Fallback: if it has "name" but doesn't match biozone pattern, treat as section.
    if "name" in item:
        return "sections"
    return None


def _carry_extras(item: dict[str, Any], known: tuple[str, ...], out: dict[str, Any]) -> None:
    """H8: any non-known key the model emitted is preserved under a single
    ``_extras`` dict so downstream consumers (CSV/JSON export) can see it.

    P0-3 fix: also skip keys that are already top-level fields in ``out``.
    When the caller pre-populates ``out`` with explicit keys (e.g. endpoint_kind,
    occurrence_mode, range_top_bed), those fields must NOT also appear in
    _extras even if the raw item also carries them.
    """
    extras = {k: v for k, v in item.items() if k not in known and k not in out}
    if not extras:
        return
    existing = out.get("_extras")
    if isinstance(existing, dict):
        # Merge - existing keys (e.g. wrapper_key) win for collisions so
        # the structural hint from the caller takes precedence.
        merged = dict(existing)
        merged.update(extras)
        out["_extras"] = merged
    else:
        out["_extras"] = extras


def _coerce_bool_flag(value: Any, default: bool = True) -> bool:
    """Coerce a model-emitted flag to a real bool.

    REVIEW-2026-09-10: ``bool(value)`` inverts the meaning of the string
    ``"false"`` (non-empty string -> True) and turns ``None`` into False.
    Strings are parsed explicitly; anything unrecognisable falls back to
    ``default`` rather than to a silently-wrong value. Mirrors the JS
    mirror's ``metaRaw.rooted !== false`` intent (absent/null -> default).
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in ("false", "0", "no", "n", "off", "none", "null"):
        return False
    if text in ("true", "1", "yes", "y", "on", ""):
        return True
    return default


def _row_from_string(item: str, kind: str) -> dict[str, Any] | None:
    """Coerce a bare-string list entry into the mode's row shape.

    REVIEW-2026-09-10: ``{"sections": ["Pingdingshan"]}`` / ``{"biozones":
    ["Clarkina postbitteri Zone"]}`` / ``{"species_ranges": ["Clarkina
    yini"]}`` previously dropped every entry silently (both engines). The
    natural reading is one row whose primary identifier is the string, so
    the record is preserved (and flagged by the caller) instead of lost.
    Returns None when the string is blank.
    """
    text = str(item).strip()
    if not text:
        return None
    if kind in ("sections", "biozones", "sites", "zones", "zonations"):
        return {"name": text}
    if kind == "species_ranges":
        return {"species": text}
    # AUDIT-2026-09-27 P1: the paleomap / scatter / chemical tables were
    # reached only through `_dict_rows`, which skipped bare strings outright,
    # so a whole table could vanish with ok=True and NO warning. Only tables
    # that actually have a single natural identifier are listed: a scatter
    # point or a chemical data point needs a coordinate pair to mean anything,
    # so a bare string there is junk, exactly like an abundance row.
    if kind in ("continents", "oceans_seas", "tectonic_features",
                "biogeographic_realms", "fossil_sites", "groups",
                "events", "intervals"):
        return {"name": text}
    if kind == "paleolatitude_indicators":
        return {"type": text}
    if kind == "data_points":
        return {"sample_id": text}
    # Deliberately NOT abundances: a taxon row needs a level/abundance to mean
    # anything, so a bare string there is far more likely to be junk than a
    # record — tests_core pins that it is dropped, and coercing it would
    # invent a taxon with no associated measurement. Same reasoning for
    # scatter ``points`` / ``outliers`` and for ``correlations`` / ``nodes``,
    # which need a second field to identify anything.
    return None


def _single_record_from_dict(raw: Any, kind: str | None) -> dict | None:
    """Return *raw* as ONE record, or None if it is a wrapper-keyed mapping.

    FIX-2026-10-01 (HIGH). A dict-shaped named array has two distinct shapes
    and the old code only understood one:

        (a) a wrapper-keyed mapping  {"Pingdingshan": {...}, "Gaoyuzhuang": {...}}
        (b) a SINGLE record returned where the schema asked for an array
            {"species": "Clarkina yini", "range_base": "Bed 1", ...}

    Both arrive as a ``dict``, so the recovery loop for (a) ran on (b), every
    value was a scalar, every one hit ``if not isinstance(inner, dict): continue``,
    and the ENTIRE table came back empty -- with ``ok=True``, the model's
    declared confidence preserved, no warning, no retry, and one billed API
    call. Measured across all eight modes: sections, species_ranges, sites,
    zones, data_points and points all emptied; ``chemical_stratigraphy``
    additionally FABRICATED an empty row when one value happened to be a dict.

    Discriminator: in shape (b) the kind's primary identifier is a KEY of
    ``raw``; in shape (a) it is not. A value that is itself a dict means we are
    looking at shape (a) even when the wrapper key happens to equal the
    identifier name, so that case is excluded too.
    """
    if not isinstance(raw, dict) or not kind or not raw:
        return None
    id_key = _PRIMARY_ID_KEYS.get(kind)
    if not id_key or id_key not in raw:
        return None
    if isinstance(raw.get(id_key), dict):
        return None  # wrapper-keyed mapping that happens to use the id name
    return dict(raw)


def _iter_rows(raw: Any, kind: str, warnings: list[str] | None = None):
    """Yield dict rows from a list- or dict-shaped named array.

    Generalises the range-chart ``_coerce_list_or_dict`` recovery for the
    other chart modes:
      * list  -> dict items pass through; bare strings are coerced to a row
        whose primary identifier is the string (REVIEW-2026-09-10: they used
        to be skipped with no warning at all).
      * dict  -> each ``{wrapper_key: {...}}`` value is yielded, with the
        wrapper key injected as the primary identifier when that identifier
        is *absent* (a present-but-empty value is the model honestly saying
        "unreadable" and must NOT be overwritten - review finding).
    """
    def _flag(tag: str) -> None:
        if warnings is not None and tag not in warnings:
            warnings.append(tag)

    if isinstance(raw, dict):
        # REVIEW-2026-09-20 #5: a dict-shaped named array is always a repair,
        # even when every record carries its own identifier and no wrapper key
        # had to be injected - the previous code only flagged the injection,
        # so the shape change went unreported.
        _flag("dict_shaped_array")
        # FIX-2026-10-01: a single record returned where an array was asked
        # for. Without this the loop below treated the record's own field
        # values as wrapper entries, every one was a scalar, and the table
        # came back empty. See _single_record_from_dict.
        single = _single_record_from_dict(raw, kind)
        if single is not None:
            yield single
            return
        for wrapper_key, inner in raw.items():
            if not isinstance(inner, dict):
                continue
            row = dict(inner)
            id_key = _PRIMARY_ID_KEYS.get(kind)
            # FIX-2026-10-01: do not invent an identifier from a bare index.
            # A dict-shaped `correlations` keyed by position ({"0": {...},
            # "1": {...}}) is the common emission, and "0"/"1" is not a
            # biozone name -- injecting it produced a FABRICATED `from_zone`
            # that exported to JSON/CSV/XLSX and was indistinguishable from a
            # real zone on the plate. The same shape reached `species`
            # (finding A3) and was only half-fixed: "key absent" is the right
            # test, but an index is not a name either. Leave the field empty
            # (the model honestly said it could not read it) and flag the
            # repair so the operator knows a row lost its identifier.
            if id_key and id_key not in row and not str(wrapper_key).strip().isdigit():
                row[id_key] = str(wrapper_key)
            elif id_key and id_key not in row:
                _flag("identifier_not_recovered_from_index")
            yield row
    elif isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                yield item
            elif isinstance(item, str):
                row = _row_from_string(item, kind)
                if row is not None:
                    yield row
                    _flag("string_row_coerced")


def _dict_rows(raw: Any, kind: str | None = None, warnings: list[str] | None = None):
    """Yield dict rows from a list- OR dict-shaped named array.

    REVIEW-2026-09-10: every non-range-chart normalizer did
    ``for x in (parsed.get(k) or [])`` and skipped non-dicts, so a
    dict-shaped emission (``{"sections": {"Ki-1": {...}}}``) produced [] and
    silently discarded every record. Iterating a dict's values recovers them.

    AUDIT-2026-09-27 P1: ``kind`` (optional, and off by default so the existing
    call sites are untouched) additionally coerces a bare-string entry into a
    single-field row and records ``string_row_coerced``, which is what
    ``_iter_rows`` already did for range_chart / abundance / zonation. Without
    it, ``{"continents": ["Laurasia", "Pangaea"]}`` came back as six empty
    tables with ``ok=True`` and no warning at all - the project's own
    definition of silent data loss. Tables with no natural single-field row
    (abundances, scatter points/outliers, correlations, nodes) still skip bare
    strings; see ``_row_from_string``.
    """
    def _flag(tag: str) -> None:
        if warnings is not None and tag not in warnings:
            warnings.append(tag)

    if isinstance(raw, dict):
        # An empty dict is not a record and not a mapping: it carried no
        # records before this branch existed and must still carry none. (The
        # "no dict values" rule below would otherwise turn ``{}`` into one
        # blank row -- found by the edge-case sweep, not by the suites.)
        if not raw:
            return
        # FIX-2026-10-01: a single record returned where an array was asked for
        # has no dict values at all ({"sample_id": "S1", "depth_m": 1.0}), so
        # the loop below yielded nothing and the table came back empty with no
        # warning. A wrapper-keyed mapping necessarily has dict values (each
        # one is a record), so "no dict values" is a kind-INDEPENDENT
        # discriminator between the two shapes and needs no per-kind key map.
        # The record is still a shape repair, so it is flagged the same way.
        has_dict_value = any(isinstance(v, dict) for v in raw.values())
        if not has_dict_value:
            _flag("dict_shaped_array")
            yield dict(raw)
            return
        for value in raw.values():
            if isinstance(value, dict):
                yield value
    elif isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                yield item
            elif isinstance(item, str) and kind:
                row = _row_from_string(item, kind)
                if row is not None:
                    yield row
                    _flag("string_row_coerced")


_PRIMARY_ID_KEYS = {
    # REVIEW-2026-09-20 #5: with no entry for "abundances" a dict-shaped
    # abundance field ({"Pinus": {"level": ..., "abundance": ...}}, which the
    # abundance prompt explicitly asks for) lost its taxon to the wrapper key
    # and normalised into an empty-taxon junk row.
    "abundances": "taxon",
    "sections": "name",
    "biozones": "name",
    "species_ranges": "species",
    "sites": "name",
    "zones": "name",
    "zonations": "name",
    "correlations": "from_zone",
    "nodes": "id",
    "data_points": "sample_id",
}


def _merge_other_fossils(existing: Any, raw: Any) -> list[str]:
    """Combine already-collected labels with a parsed ``other_fossils`` field.

    REVIEW-2026-09-20 #1: ``normalize_result`` collected bare strings from an
    ``_array_root`` payload into ``out["other_fossils"]`` and then assigned
    ``_other_fossils_from(parsed["other_fossils"])`` over the top of that
    list, so the salvaged labels disappeared as soon as the payload also
    carried an ``other_fossils`` field. Merge instead: order preserved, exact
    duplicates skipped.
    """
    merged: list[str] = []
    seen: set = set()
    already = [x for x in (existing or []) if isinstance(x, str)]
    for text in already + _other_fossils_from(raw):
        t = (text or "").strip()
        if t and t not in seen:
            seen.add(t)
            merged.append(t)
    return merged


def _other_fossils_from(raw: Any) -> list[str]:
    """Normalise ``other_fossils`` to a list of display strings.

    REVIEW-2026-09-10: dict-shaped entries were dropped by the Python
    normalizer while the browser (js/minimax.js M1 fix) already lifted
    ``label``/``species``/``taxon``/``name`` - so a fossil record with a
    label was visible in the browser and absent from the server/CSV/XLSX.
    Both shapes are now accepted on both sides.
    """
    def _lift(item: Any) -> str:
        if item is None:
            return ""
        if isinstance(item, str):
            return item.strip()
        if isinstance(item, dict):
            for key in ("label", "species", "taxon", "name", "fossil", "text"):
                value = item.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            # No recognisable label: keep the record as JSON text rather
            # than dropping it (_stringify_scalar renders dicts as "").
            #
            # AUDIT-2026-09-29: the `extras` dict built here was assigned and
            # never read (ruff F841) -- a leftover from an earlier design that
            # folded unrecognised keys into a sidecar. The behaviour below is
            # the intended one, so the dead binding is simply gone.
            try:
                return json.dumps(item, ensure_ascii=False, sort_keys=True)
            except (TypeError, ValueError):
                return _stringify_scalar(item)
        return _stringify_scalar(item)

    if isinstance(raw, str):
        return [raw.strip()] if raw.strip() else []
    if isinstance(raw, dict):
        # {"ammonoid": {...}} / {"1": "string"} - iterate the values.
        out: list[str] = []
        for value in raw.values():
            text = _lift(value)
            if text:
                out.append(text)
        return out
    if isinstance(raw, list):
        out = []
        for item in raw:
            text = _lift(item)
            if text:
                out.append(text)
        return out
    return []


def _stringify_scalar(value: Any) -> str:
    """Stringify only scalars; containers return "" instead of a Python repr.

    REVIEW-2026-09-10: ``str({"name": "x"})`` produced
    ``"{'name': 'x'}"`` inside scientific identifier fields (species,
    fossil_marker), i.e. a fabricated-looking value. Containers are handled
    by the callers (lifted or carried into ``_extras``); here they collapse
    to an empty string rather than leaking a repr.

    FIX-2026-10-01: a LIST of scalars is joined rather than emptied. The
    empty-string branch assumed every container would be lifted or rescued by
    ``_carry_extras``, but that only rescues UNKNOWN keys -- and these are
    KNOWN ones (``fossils`` in _KNOWN_PALEOMAP_SITE_KEYS, ``fossil_marker``
    in _KNOWN_SAMPLE_KEYS, ``characteristic_fauna``), so the whole value
    vanished. ``{"fossils": ["radiolaria", "algae"]}`` exported as an empty
    cell, which reads as the affirmative claim "this site records no fossil
    assemblage" -- a worse outcome than dropping the row, because it is a
    false statement about the figure rather than a gap. A comma-joined list
    keeps the information and stays a single scalar cell. A dict still
    returns "" (no faithful scalar rendering, and nested records are lifted by
    the callers).
    """
    if value is None:
        return ""
    if isinstance(value, (list, tuple, set)):
        parts = [str(v).strip() for v in value
                 if v is not None and not isinstance(v, (dict, list, tuple, set))
                 and str(v).strip()]
        return ", ".join(parts)
    if isinstance(value, dict):
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _pop_array_root_extras(extras: dict[str, Any]) -> dict[str, Any]:
    """Drop ``_array_root`` / ``_note`` from a top-level ``_extras`` payload.

    REVIEW-2026-09-10: only the range-chart normalizer stripped these, so
    every other mode duplicated the whole raw payload into the result JSON
    (and inflated quality.py's ``_extras``-ratio check).
    """
    extras.pop("_array_root", None)
    extras.pop("_note", None)
    return extras


def _extracted_any(out: dict[str, Any]) -> bool:
    """True when a normalizer salvaged at least one record / non-empty field.

    Used by the "unusable payload" guard (REVIEW-2026-09-20 #7): a rescued
    fragment that still produced rows is worth keeping, one that produced
    nothing but noise is not.

    FIX-2026-10-17 (P1-2): METADATA IS NOT CONTENT. The loop used to accept
    any non-empty value, and several normalizers emit bookkeeping of their own
    (``warnings`` / ``_warnings``, ``confidence``, ``cell_status``). So a
    payload in which the model produced NOTHING but the normalizer had
    something to say about it counted as "extracted", which is the exact
    opposite of what this predicate is for — and ``_payload_mismatch`` opens
    with ``if _extracted_any(data): return ""``, so the guard it gates could
    not fire at all. Measured on the real occurrence-matrix normalizer:

        occurrences == []                       # every cell read failed
        warnings == ['dropped 1 row(s) for blank cells; ...']
        _payload_mismatch(out, truncated=True) -> ''      # should be an error
        _payload_mismatch(normalize_result({}), truncated=True)
            -> 'truncated output rescued no usable records'   # no bookkeeping

    The list is explicit rather than a prefix rule, and it deliberately does
    NOT contain ``_extras``: a non-empty ``_extras`` is the model handing us
    SOMETHING (``{"totally_unrelated_key": 1}`` parks there for the other
    eight modes, and tests/test_frontend_parity_fixtures_2026_09_20.py pins
    that as a successful extraction), so treating it as bookkeeping inverted a
    documented contract and broke 12 tests when tried.
    """
    for key, value in out.items():
        if key in _NON_CONTENT_KEYS:
            continue
        if isinstance(value, (list, dict)):
            if len(value) > 0:
                return True
        elif value not in (None, "", 0, 0.0, False):
            return True
    return False


_NON_CONTENT_KEYS: frozenset[str] = frozenset({
    # Diagnostic prose the normalizer wrote about its own work.
    "_warnings", "warnings", "note", "_note",
    # Scalars every mode fills in regardless of content.
    "confidence", "confidence_by_section", "confidence_detail",
    # Markers, not records.
    "cell_status", "truncated",
})


def _unwrap_array_root_into(parsed: dict[str, Any], out: dict[str, Any],
                            spec: tuple[tuple[str, tuple[str, ...], Any], ...],
                            warnings: list[str] | None = None) -> None:
    """Distribute a list-shaped root (``_array_root``) into the named arrays.

    REVIEW-2026-09-20 #6: every normalizer but the chemical one unwrapped the
    ``{"_array_root": [...]}`` shape that json_utils produces when the model
    emits a bare array; the chemical mode threw the whole payload away
    (``parsed.get("events")`` was empty), so a perfectly good set of isotope
    points produced an empty table.

    ``spec`` is ``(target_key, distinguishing_keys, target_list)``. The
    distinguishing keys are matched FIRST: ``_classify_array_item`` is a
    range-chart heuristic that maps any dict with a ``name`` onto "sections",
    which would mislabel another mode's rows. Modes whose rows have no stable
    signature pass an empty tuple and get the shared classifier.

    REVIEW-2026-09-20 #7: json_utils only records a truncation flag inside
    ``_extras["_json_recovery"]``, which no consumer reads, so a truncated
    emission that rescued a single INNER row (one data point of a curve) came
    back as ``ok=True`` with everything else empty. The truncation is mirrored
    onto the normalized data and, when nothing could be salvaged, flagged.
    """
    items = parsed.get("_array_root")
    if not isinstance(items, list):
        return
    extras = parsed.get("_extras")
    if isinstance(extras, dict) and extras.get("_json_recovery"):
        out["truncated"] = True
    keys_in_spec = {key for key, _probe, _target in spec}
    for item in items:
        if not isinstance(item, dict):
            continue
        kind = None
        for key, probe, _target in spec:
            if probe and all(k in item for k in probe):
                kind = key
                break
        if kind is None:
            classified = _classify_array_item(item)
            if classified in keys_in_spec:
                kind = classified
        if kind is None:
            # Nothing identifies the row for THIS mode: keep it under
            # ``_unclassified`` rather than dropping it silently.
            out.setdefault("_unclassified", []).append(item)
            continue
        for key, _probe, target in spec:
            if key == kind:
                target.append(item)
                break
    if warnings is not None and not _extracted_any(out):
        _append_warning(warnings, "truncated_or_unrecognized_payload")


def _append_warning(warnings: list[str], tag: str) -> None:
    if tag not in warnings:
        warnings.append(tag)


def _rehydrate_array_root(parsed: dict[str, Any], spec_keys: tuple[tuple[str, tuple[str, ...]], ...],
                          warnings: list[str] | None = None) -> bool:
    """Distribute ``_array_root`` back into ``parsed`` so the normalizer sees it.

    AUDIT-2026-09-27 P0-1: ``_unwrap_array_root_into`` appends the RAW items to
    the target list, which is what the chemical mode wants (it has no
    per-table loop to re-normalize them). The paleomap and scatter normalizers
    DO have per-table loops that emit coerced rows, so the raw items have to
    travel back through ``parsed`` or they would bypass coercion. Handing each
    table a FRESH bucket and only publishing non-empty ones keeps the tables
    that the model answered normally completely untouched, and - critically -
    avoids handing a normalizer's own output list back to its own reader.

    Returns True when an array root was present and distributed.
    """
    items = parsed.get("_array_root")
    if not isinstance(items, list) or not items:
        return False
    buckets: dict[str, list] = {}
    out: dict[str, Any] = {}
    for key, probe in spec_keys:
        buckets.setdefault(key, [])
        out[key] = buckets[key]
    _unwrap_array_root_into(parsed, out, tuple(
        (key, probe, buckets[key]) for key, probe in spec_keys), warnings)
    published = False
    for key, rows in buckets.items():
        if rows:
            parsed[key] = rows
            published = True
    return published


def _confidence_clamped(value: float) -> float:
    """Clamp a confidence to [0, 1]; NaN becomes 0.0.

    AUDIT-2026-09-30: this is the single gate for every ROOT confidence in this
    module, and it exists because the JS mirror already had one.

    Python's min()/max() do not propagate NaN -- ``min(1.0, nan)`` is 1.0,
    because ``nan < 1.0`` is False -- so a model that emitted
    ``"confidence": "NaN"`` was reported as a PERFECT 1.0 in every mode
    (13 hand-copied unguarded clamps across all 10 normalizers, found by AST
    enumeration rather than grep). js/minimax.js routes all eight of its root
    call sites through ONE function, ``rcaConfidenceClamped``, which guards
    ``Number.isNaN(n) -> return 0.0``; the same payload showed 0 in the
    browser and 1.0 on the desktop.

    Only NaN is remapped. +Infinity still clamps to 1.0 and -Infinity to 0.0,
    which is what BOTH engines already did and what the JS helper does too --
    writing ``if not math.isfinite(v): return 0.0`` here would silently create
    a NEW divergence on +Infinity, so the guard is deliberately isnan, not
    isfinite.

    Honest reporting is also the project's own stance: tests_real_corpus.py
    INV5 treats "empty arrays + confidence 0" as a correct refusal. A NaN is
    the same kind of "the model could not say", and 1.0 asserts the opposite.
    """
    if value != value:  # NaN
        return 0.0
    return max(0.0, min(1.0, value))


POS_SCALE_KEY = "pos_0_999"


POS_SUFFIX = "_pos_0_999"


POS_MIN = 0


POS_MAX = 999


GEOMETRY_VERSION = 1


GEOMETRY_SCALE = "pos_0_999"


GEOMETRY_TOLERANCE = 0.15


_CONTRACT_SOURCE_KEYS = ("response_kind", "reason_codes", "reason_code")


_GEOMETRY_AXIS_BY_FIELD = {"x_pos_0_999": "x", "y_pos_0_999": "y"}


_GEOMETRY_VERTICAL_FIELDS = frozenset({
    "pos_0_999", "depth_pos_0_999", "level_pos_0_999", "age_pos_0_999",
    "range_top_pos_0_999", "range_base_pos_0_999",
    "top_pos_0_999", "base_pos_0_999",
})


def _geometry_axis_for(field: str,
                       axes: dict[str, dict[str, Any]] | None
                       ) -> str | None:
    """Which calibrated axis a ``*pos_0_999`` field belongs to (or None)."""
    if field in _GEOMETRY_AXIS_BY_FIELD:
        return _GEOMETRY_AXIS_BY_FIELD[field]
    if field in _GEOMETRY_VERTICAL_FIELDS or field == POS_SCALE_KEY:
        return "vertical"
    if field.endswith(POS_SUFFIX):
        prefix = field[: -len(POS_SUFFIX)]
        if prefix and (axes or {}).get(prefix) is not None:
            return prefix
    return None


_GEOMETRY_SEMANTIC_KEYS = {
    "pos_0_999": ("depth", "depth_m", "age_ma", "y", "level"),
    "y_pos_0_999": ("y",),
    "x_pos_0_999": ("x",),
    "range_top_pos_0_999": ("range_top_idx", "range_top"),
    "range_base_pos_0_999": ("range_base_idx", "range_base"),
    "depth_pos_0_999": ("depth", "depth_m"),
    "age_pos_0_999": ("age_ma", "age"),
    "level_pos_0_999": ("level", "depth", "depth_m"),
    # FIX-2026-09-22 (audit item 5): the chemical-stratigraphy interval rows
    # use top_pos_0_999 / base_pos_0_999 (both are in _GEOMETRY_VERTICAL_FIELDS)
    # against top_depth_m/top_age_ma and base_depth_m/base_age_ma.  Without the
    # pairing the anti-self-echo contradiction test silently never ran for that
    # mode, so a geometry block contradicting the transcribed interval was kept.
    "top_pos_0_999": ("top_depth_m", "top_age_ma"),
    "base_pos_0_999": ("base_depth_m", "base_age_ma"),
}


def _to_float_opt(value: Any) -> float | None:
    """Best-effort float for a scientific value; non-numeric text -> ``None``.

    AUDIT-2026-09-30: js/viz.js#rcaVizNum is documented as the mirror of this
    function, and the two disagreed on 11 of 42 probed inputs. The worst was
    ``"260 Ma"`` -- the most ordinary age notation there is -- which this side
    refused while the browser read 260 and drew the endpoint. That is not a
    rounding difference: ``_axis_domain`` returns None when EITHER end fails
    to parse, so a model that wrote ``{"at_0": "0 Ma", "at_999": "120 Ma"}``
    produced a calibrated chart in the browser and an UNcalibrated one on the
    desktop, with the row's point geometry dropped for want of a domain.

    The mirror was also the stricter side on plain numeric text: ``"1.2e2"``,
    ``"1e5"``, ``".5"`` and ``"5."`` all parse here and returned null there.
    The browser's mirror now tries ``Number()`` first for the same reason.

    So each side now accepts what the other already accepted, and nothing
    more: the number grammar is Python's, and the unit suffix is the mirror's
    explicit ``ma | m.a. | megaa`` list, which is age notation this module
    legitimately meets in ``at_0`` / ``top_age_ma`` positions.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        # AUDIT-2026-09-30: a non-finite number is not a number, and the
        # mirror already said so -- js/viz.js#rcaVizNum returns null for NaN
        # and +-Infinity because it tests isFinite. Here they went straight
        # through `float(value)`, and this function feeds the plot-frame
        # maths: one NaN endpoint poisons _axis_domain, the whole calibration
        # is dropped, and a NaN that does survive into a position breaks the
        # render rather than degrading it. Same class as the confidence-clamp
        # and the phylo metadata defects, one layer down.
        f = float(value)
        return f if math.isfinite(f) else None
    if not isinstance(value, str):
        return None
    # FIX-2026-10-01: a comma is only a DECIMAL separator when it is not a
    # thousands separator. The old line was a blind `.replace(",", ".")`, so
    # "1,000" became "1.000" and float() read it as 1.0 -- measured on both
    # engines (`_to_float_opt("1,000")` -> 1.0, and js/viz.js#rcaVizNum gave the
    # same 1). Since a range_base/range_top_idx carrying "1,000" then plotted
    # Bed 1000 as Bed 1, one row spanned the entire stratigraphic axis and the
    # domain was truncated, with no warning anywhere. The rule is the standard
    # one: a comma/underscore/space followed by EXACTLY three digits (and not
    # another digit) is a group separator; any comma left over is a European
    # decimal point, which is why the replace was there in the first place.
    # "1,5" -> 1.5 and "12,5" -> 12.5 are unchanged.
    # Ambiguity accepted and documented: "3,141" is read as 3141 (a group), not
    # as a European decimal with three places. Both engines apply the identical
    # rule, so they stay in parity.
    text = re.sub(r"([,_  ])(\d{3})(?!\d)", r"\2", value.strip())
    text = text.replace(",", ".")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        # "260 Ma" / "120 m.a." / "1e5 Ma" / "1_000 Ma": float() rejects the
        # unit suffix, and the mirror accepts it. Numbers with an exponent or
        # an underscore separator are spelled the way float() already takes
        # them, so they only need the suffix stripped.
        m = re.fullmatch(
            r"\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?(?:_\d+)*)"
            r"\s*(?:ma|m\.a\.|megaa)?\s*",
            text, re.IGNORECASE)
        return float(m.group(1)) if m else None


def normalize_pos_0_999(value: Any) -> int | None:
    """Strictly parse a PlotLift position: an INTEGER in [0, 999], else None.

    Deliberately unforgiving — ``"712"`` is fine, ``712.5`` / ``1000`` /
    ``-1`` / ``True`` are not. A rejected value must not be silently clamped
    into a plausible-looking position.
    """
    if value is None or isinstance(value, bool):
        return None
    parsed: float | None = None
    if isinstance(value, int):
        parsed = float(value)
    elif isinstance(value, float):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = float(text)
        except ValueError:
            return None
    if parsed is None or not float(parsed).is_integer():
        return None
    pos = int(parsed)
    if pos < POS_MIN or pos > POS_MAX:
        return None
    return pos


def _axis_domain(raw: Any) -> dict[str, Any] | None:
    """Normalise one axis calibration entry to ``{at_0, at_999, unit}``.

    Accepted shapes (all optional, model-emitted)::

        {"vertical": {"at_0": 1, "at_999": 24, "unit": "bed"}}
        {"vertical": {"bottom": 1, "top": 24}}
        {"vertical": [1, 24]}

    ``at_0`` is the value at the BOTTOM (oldest) edge of the plot frame and
    ``at_999`` the value at the TOP (youngest) edge, i.e. exactly the ends the
    0-999 scale is defined against.  Returns None unless both ends are numeric.
    """
    if isinstance(raw, (list, tuple)) and len(raw) == 2:
        low, high = (_to_float_opt(raw[0]), _to_float_opt(raw[1]))
        return {"at_0": low, "at_999": high, "unit": ""} if (
            low is not None and high is not None) else None
    if not isinstance(raw, dict):
        return None
    low = high = None
    for key in ("at_0", "bottom", "base", "oldest", "min", "start", "0"):
        if key in raw:
            low = _to_float_opt(raw.get(key))
            if low is not None:
                break
    for key in ("at_999", "top", "ceiling", "youngest", "max", "end", "999"):
        if key in raw:
            high = _to_float_opt(raw.get(key))
            if high is not None:
                break
    if low is None or high is None:
        return None
    unit = raw.get("unit")
    # FE-FIX-2026-09-22 (item 7): bool is an int subclass, so True used to
    # sail through the isinstance gate and export the unit "True" - while
    # the JS mirror (js/minimax.js rcaAxisDomain) emitted "". Both engines
    # now REJECT a bool unit to "" (the auditor's cleaner of the two
    # conventions; a boolean is not a unit of measurement).
    if isinstance(unit, bool):
        unit = ""
    return {"at_0": low, "at_999": high,
            "unit": str(unit).strip() if isinstance(unit, (str, int, float)) else ""}


def _axis_calibration_blocks(payload: Any) -> list[dict[str, Any]]:
    """Every raw ``axis_calibration`` block a payload carries, in match order.

    Root, then ``metadata``, then ``_extras`` — models put figure-level blocks
    in whichever of the three they prefer. Non-dict / empty blocks are dropped
    (there is nothing to read out of them).
    """
    blocks: list[dict[str, Any]] = []
    if not isinstance(payload, dict):
        return blocks
    for holder in (payload, payload.get("metadata"), payload.get("_extras")):
        if not isinstance(holder, dict):
            continue
        candidate = holder.get("axis_calibration")
        if isinstance(candidate, dict) and candidate:
            blocks.append(candidate)
    return blocks


def axis_domains_from(payload: Any) -> dict[str, dict[str, Any]]:
    """Collect the optional ``axis_calibration`` block from a parsed result.

    Looks at the root, ``metadata`` and ``_extras`` — models put figure-level
    blocks in whichever of the three they prefer.  Unknown axis names are kept
    verbatim so a future axis (``"thickness"``) needs no code change here.
    """
    out: dict[str, dict[str, Any]] = {}
    for candidate in _axis_calibration_blocks(payload):
        for name, raw in candidate.items():
            domain = _axis_domain(raw)
            if domain and str(name) not in out:
                out[str(name)] = domain
    return out


def axis_calibration_unusable(payload: Any,
                              axes: dict[str, dict[str, Any]] | None = None,
                              ) -> bool:
    """True when the model claimed an axis calibration that cannot be honoured.

    FIX-2026-09-22 (audit item 7): two shapes used to vanish without a trace —
    the raw block was swallowed by the mode root-key whitelist (so it never
    reached ``_extras``) and no warning said why the geometry stayed raw:

    * a block exists but no axis survives :func:`_axis_domain`, e.g.
      ``{"vertical": {"at_0": 1}}`` where only one end is numeric;
    * every surviving axis is degenerate (``at_0 == at_999``), where the linear
      map would collapse the whole plot onto a single value (see
      :func:`_geometry_point`).
    """
    if not _axis_calibration_blocks(payload):
        return False
    if axes is None:
        axes = axis_domains_from(payload)
    if not axes:
        return True
    try:
        return all(float(d["at_0"]) == float(d["at_999"]) for d in axes.values())
    except (TypeError, KeyError, ValueError):
        return True


def _fold_axis_calibration(parsed: Any,
                           axes: dict[str, dict[str, Any]],
                           out: dict[str, Any],
                           extras_src: dict[str, Any],
                           warnings: list[str],
                           ) -> None:
    """Hoist a usable calibration; warn + keep the raw block when it is not.

    Shared by the four contract modes (range chart, abundance, chemical
    stratigraphy, scatter) so the root shape is defined once:

    * usable  -> ``out["axis_calibration"] = axes``;
    * unusable -> no hoist, an ``axis_calibration_unusable`` root warning and
      the raw block preserved under ``extras_src["axis_calibration"]`` so the
      operator still sees what the model claimed (audit item 7).
    """
    if axis_calibration_unusable(parsed, axes):
        if "axis_calibration_unusable" not in warnings:
            warnings.append("axis_calibration_unusable")
        raw = parsed.get("axis_calibration") if isinstance(parsed, dict) else None
        if isinstance(raw, dict) and raw:
            extras_src["axis_calibration"] = raw
        return
    if axes:
        out["axis_calibration"] = axes


def pos_to_axis_value(pos: int, domain: dict[str, Any] | None) -> float | None:
    """Linearly map a 0-999 frame position onto a calibrated axis."""
    if not domain or pos is None:
        return None
    # FIX-2026-09-22 (audit item 8): ``bool`` is an ``int`` subclass, so a
    # model-emitted ``true`` used to map as position 1 (and ``false`` as 0)
    # and produced a plausible-looking calibrated value.  A boolean is not a
    # position; same rejection as :func:`normalize_pos_0_999`.
    if isinstance(pos, bool) or not isinstance(pos, (int, float)):
        return None
    low, high = domain.get("at_0"), domain.get("at_999")
    if isinstance(low, bool) or isinstance(high, bool):
        return None
    if not isinstance(low, (int, float)) or not isinstance(high, (int, float)):
        return None
    return low + (float(pos) / float(POS_MAX)) * (float(high) - float(low))


def _geometry_point(field: str,
                    raw: Any,
                    src_row: dict[str, Any],
                    axes: dict[str, dict[str, Any]] | None
                    ) -> tuple[dict[str, Any] | None, bool, str | None]:
    """One ``*pos_0_999`` field -> ``(entry | None, rejected, axis name)``.

    ``rejected`` is True only for a value that CANNOT be trusted: a non-integer
    or out-of-scale position, or a converted value that contradicts the row's
    own transcribed boundary by more than :data:`GEOMETRY_TOLERANCE` of the
    axis span.  The check against the transcribed value is skipped when the two
    numbers are demonstrably on different scales (the semantic value lies
    outside the calibrated domain — e.g. bed indices against a metre axis), so
    a unit mismatch can never trigger a false rejection.
    """
    pos = normalize_pos_0_999(raw)
    if pos is None:
        return None, True, None
    axis = _geometry_axis_for(field, axes)
    domain = (axes or {}).get(axis) if axis else None
    if domain is None:
        domain = (axes or {}).get(field)
    entry: dict[str, Any] = {"pos": pos, "axis": axis or "uncalibrated"}
    if domain is None:
        # No calibration: the 0-999 integer survives as evidence, nothing more.
        return entry, False, None
    value = pos_to_axis_value(pos, domain)
    if value is None:
        return None, True, None
    lo = min(float(domain["at_0"]), float(domain["at_999"]))
    hi = max(float(domain["at_0"]), float(domain["at_999"]))
    span = hi - lo
    if span <= 0:
        # FIX-2026-09-22 (audit item 4): ``{at_0: 0, at_999: 0}`` is a legal
        # shape for _axis_domain but the linear map degenerates — every
        # position became 0.0 with ``calibrated: true``, and the contradiction
        # guards below (both keyed on ``span > 0``) never ran, so a row whose
        # transcribed depth said otherwise was accepted silently.  A
        # zero-span calibration carries no information: keep the raw 0-999
        # integer as evidence, mark the point rejected (=> low_confidence code)
        # and never write a value.
        return entry, True, None
    tol = GEOMETRY_TOLERANCE * span
    if not (lo - tol <= value <= hi + tol):
        return None, True, None
    for sem_key in _GEOMETRY_SEMANTIC_KEYS.get(field, ()):
        sem = _to_float_opt(src_row.get(sem_key))
        if sem is None or not (lo <= sem <= hi):
            continue
        if abs(value - sem) > tol:
            return None, True, None
        break
    entry["value"] = round(value, 6)
    if domain.get("unit"):
        entry["unit"] = domain["unit"]
    return entry, False, axis


def geometry_from_row(src: dict[str, Any],
                      axes: dict[str, dict[str, Any]] | None = None,
                      *,
                      row: dict[str, Any] | None = None
                      ) -> tuple[dict[str, Any] | None, bool, list[str]]:
    """Build the optional ``geometry`` sidecar for one row.

    Returns ``(geometry | None, rejected, consumed_keys)``:

    * ``geometry`` — ``{"version", "scale", "calibrated", "points", "axes"}``
      with one entry per ``*pos_0_999`` field: ``{"pos": int, "axis": str,
      "value": float?}``.  ``value`` is the engineering value derived from the
      axis calibration; it is absent when the result carries no calibration for
      that axis, in which case the raw 0-999 integer survives as evidence only.
    * ``rejected`` — True when at least one position was unusable, so the
      caller can add a ``low_confidence`` reason code.
    * ``consumed_keys`` — the ``*pos_0_999`` keys folded into the block, so the
      caller keeps them out of ``_extras`` instead of duplicating them.

    A bad point is dropped, the good ones in the same row survive; if nothing
    survives there is no ``geometry`` key at all.  The guard rails are
    one-directional on purpose: geometry can be dropped, it can never change a
    semantic value (the anti-self-echo rule).
    """
    src_row = row if isinstance(row, dict) else (src if isinstance(src, dict) else {})
    points: dict[str, Any] = {}
    used_axes: dict[str, Any] = {}
    consumed: list[str] = []
    rejected = False
    if not isinstance(src, dict):
        return None, False, consumed
    # AUDIT-2026-09-29 (ruff F402): the loop variable was named `field`,
    # shadowing dataclasses.field -- imported at the top of this module and
    # used by every dataclass here, including ExtractResult right above. Inside
    # this loop `field` is a column name, so nothing broke: there is no
    # field(...) call in these 16 lines. But a shadowed name that is "harmless
    # right now" is exactly the one that bites when someone later adds a
    # dataclass or a default_factory inside this function, and the failure
    # would be `'str' object is not callable` far from the cause. Renamed to
    # `name`, which is what it is.
    for name, raw in src.items():
        if not isinstance(name, str):
            continue
        if not (name == POS_SCALE_KEY or name.endswith(POS_SUFFIX)):
            continue
        consumed.append(name)
        entry, bad, axis = _geometry_point(name, raw, src_row, axes)
        if bad:
            rejected = True
        if entry is None:
            continue
        if axis and entry.get("value") is not None and axis not in used_axes:
            domain = (axes or {}).get(axis) or (axes or {}).get(name)
            if domain is not None:
                used_axes[axis] = domain
        points[name] = entry
    if not points:
        return None, rejected, consumed
    geometry: dict[str, Any] = {
        "version": GEOMETRY_VERSION,
        "scale": GEOMETRY_SCALE,
        "calibrated": bool(used_axes),
        "points": points,
    }
    if used_axes:
        geometry["axes"] = used_axes
    return geometry, rejected, consumed


def apply_coverage_contract(src: dict[str, Any],
                            row: dict[str, Any],
                            *,
                            extra_codes: Iterable[str] = ()) -> None:
    """Write the optional coverage fields onto a normalized row.

    Tolerant of every shape the model emits (list / comma string / dict) and
    silent when it emitted nothing at all, so legacy results keep their exact
    old key set.  A ``not_drawn`` claim that comes with a readable value is a
    contradiction the merge layer already resolves in favour of "drawn" — so
    the parser does too, and flags it instead of storing both.
    """
    if not isinstance(src, dict) or not isinstance(row, dict):
        return
    kind = normalize_response_kind(src.get("response_kind"))
    codes = normalize_codes(src.get("reason_codes") or src.get("reason_code"))
    for code in extra_codes:
        slug = normalize_codes([code])[0] if normalize_codes([code]) else None
        if slug and slug not in codes:
            codes.append(slug)
    if kind == RESPONSE_NOT_DRAWN and has_value(row):
        # "not drawn" plus a value = it WAS drawn (or the value is a real 0,
        # which is an answer, not an absence). extracted wins; flag the lie.
        kind = RESPONSE_EXTRACTED
        _add_row_flag(row, "response_kind_conflict")
    if kind:
        row["response_kind"] = kind
    if codes:
        row["reason_codes"] = codes


def attach_row_contract(src: dict[str, Any],
                        row: dict[str, Any],
                        *,
                        axes: dict[str, dict[str, Any]] | None = None,
                        ) -> tuple[str, ...]:
    """One call site per row normalizer: geometry + coverage, additively.

    Folds every ``*pos_0_999`` field of ``src`` into ``row["geometry"]`` (only
    when at least one position survives), writes the coverage contract, and
    returns the extra known-key tuple the caller must hand to
    ``_carry_extras`` so nothing is duplicated under ``_extras``.

    Used by all four continuous/table modes so the contract is defined once
    (BORROW-2026-09-20 A+B).
    """
    geometry, rejected, consumed = geometry_from_row(src, axes, row=row)
    if geometry is not None:
        row["geometry"] = geometry
    apply_coverage_contract(
        src, row, extra_codes=[LOW_CONFIDENCE_CODE] if rejected else ())
    return tuple(consumed)


_KNOWN_COLUMNAR_SECTION_KEYS = (
    "id", "group", "lithology_blocks", "age_units", "samples",
    "coordinates_text", "thickness_m", "confidence_by_section",
)


_KNOWN_BLOCK_KEYS = ("pattern", "range_top_idx", "range_base_idx")


_KNOWN_UNIT_KEYS = ("label", "range_top_idx", "range_base_idx")


_KNOWN_SAMPLE_KEYS = ("bed_idx", "fossil_marker", "ref")


_KNOWN_LEGEND_KEYS = ("marker", "pattern", "meaning")


_KNOWN_CROSS_KEYS = ("from_section", "from_bed_idx", "to_section", "to_bed_idx")


_KNOWN_COLUMNAR_ROOT_KEYS = (
    "sections", "fossil_legend", "lithology_legend", "cross_beds",
    "overall_confidence", "confidence",
)


_KNOWN_ABUNDANCE_ROOT_KEYS = ("sites", "abundances", "zones", "confidence",
                              # BORROW-2026-09-20 (B)
                              "axis_calibration")


_KNOWN_SITE_KEYS = ("name", "location", "age_range", "depth_unit")


_KNOWN_ABUNDANCE_KEYS = (
    "taxon", "site", "level", "depth", "abundance", "abundance_unit",
)


_KNOWN_ZONE_KEYS = ("name", "age", "level_range")


_KNOWN_CHEMICAL_STRAT_ROOT_KEYS = ("metadata", "data_points", "events", "intervals",
                                   "confidence",
                                   # BORROW-2026-09-20 (B)
                                   "axis_calibration")


_KNOWN_CHEMICAL_STRAT_DATA_POINT_KEYS = (
    "sample_id", "depth_m", "age_ma", "stage", "values", "lithology", "fossil_horizon", "note"
)


_KNOWN_CHEMICAL_STRAT_EVENT_KEYS = ("type", "depth_m", "age_ma", "name", "magnitude", "description")


_KNOWN_CHEMICAL_STRAT_INTERVAL_KEYS = (
    "name", "top_depth_m", "base_depth_m", "top_age_ma", "base_age_ma",
    "characteristic_values", "lithology"
)


_KNOWN_PALEOMAP_ROOT_KEYS = (
    "metadata", "continents", "oceans_seas", "tectonic_features",
    "biogeographic_realms", "fossil_sites", "paleolatitude_indicators", "confidence"
)


_KNOWN_PALEOMAP_CONTINENT_KEYS = ("name", "type", "coordinates", "paleolatitude", "note")


_KNOWN_PALEOMAP_SEA_KEYS = ("name", "type", "coordinates", "note")


_KNOWN_PALEOMAP_TECTONIC_KEYS = ("name", "type", "coordinates", "direction", "description")


_KNOWN_PALEOMAP_REALM_KEYS = ("name", "type", "coordinates", "characteristic_fauna")


_KNOWN_PALEOMAP_SITE_KEYS = ("name", "lat_lon", "age", "fossils", "marker_type")


_KNOWN_PALEOMAP_INDICATOR_KEYS = ("type", "coordinates")


_PALEOMAP_ARRAY_SPEC_KEYS = (
    ("paleolatitude_indicators", ("type", "coordinates")),
    ("fossil_sites", ("lat_lon",)),
    ("fossil_sites", ("marker_type",)),
    ("biogeographic_realms", ("characteristic_fauna",)),
    ("tectonic_features", ("direction",)),
    ("continents", ("paleolatitude",)),
    ("oceans_seas", ("name", "type", "coordinates")),
    ("continents", ("name",)),
)


def first_non_empty(candidates: Any) -> Any:
    """The first candidate that carries a value, else ``None``.

    AUDIT-2026-09-27 [item 7.1] (found by a repo-wide unbound-name scan).
    ``_one_point`` below has called this since paleomap was added, but the
    name was never defined in this module — so every paleomap row whose
    ``coordinates`` arrived as a dict raised ``NameError``. ``extract_paleomap``
    wraps the normaliser in ``except Exception`` and downgrades it to
    ``ok=False`` with a warning string, so the failure presented as
    "extraction failed" and the whole result was lost. It only escaped notice
    because the model often emits the other two coordinate shapes (a
    ``[lat, lon]`` list, or a ``"12N 30E"`` string) which take different
    branches — a data-loss bug whose trigger is model-output-dependent, i.e.
    the worst kind to catch by hand.

    "Carries a value" deliberately means *not None and not an empty
    container/string* — NOT truthiness. ``0.0`` is a real latitude (the
    equator) and longitude (the prime meridian), and a plain
    ``next((v for v in xs if v), None)`` would discard exactly those rows.
    The semantics match ``rca_core.exporter._nonempty``.
    """
    if candidates is None:
        return None
    try:
        iterable = list(candidates)
    except TypeError:
        return candidates if candidates is not None else None
    for v in iterable:
        if v is None:
            continue
        if isinstance(v, (list, dict, str, tuple, set)) and len(v) == 0:
            continue
        return v
    return None


_KNOWN_SCATTER_PLOT_ROOT_KEYS = (
    "metadata", "groups", "points", "outliers", "statistics", "confidence",
    # BORROW-2026-09-20 (B)
    "axis_calibration",
)


_KNOWN_SCATTER_GROUP_KEYS = ("name", "color", "marker", "n_points_visible", "description")


_KNOWN_SCATTER_POINT_KEYS = ("x", "y", "z", "group", "label", "note")


_KNOWN_SCATTER_OUTLIER_KEYS = ("x", "y", "group", "reason")


_SCATTER_ARRAY_SPEC_KEYS = (
    ("outliers", ("reason",)),
    ("points", ("x", "y")),
    ("groups", ("name",)),
)


_KNOWN_ZONATION_ROOT_KEYS = (
    "zonations", "zones", "correlations", "confidence",
)


_KNOWN_ZONATIONS_KEYS = ("name", "region", "framework", "reference")


_KNOWN_ZONATION_ZONE_KEYS = (
    "name", "zonation", "rank", "age_span", "base_age", "top_age",
    "stage", "defined_by", "note",
)


_KNOWN_CORRELATION_KEYS = (
    "from_zone", "to_zone", "from_zonation", "to_zonation", "basis", "note",
)


OCCURRENCE_CELL_STATUSES = (
    # Cells were read by the model in one pass, with no geometric check on
    # whether each one landed in the column it names. The historical default,
    # and the only status that can be wrong in an unverifiable way.
    "single_pass_unverified",
    # Cells were read against a locally detected grid, so every row's column
    # identity was checked against a pixel rectangle.
    "grid_cells",
    # The model was GIVEN the measured lattice so it could place the cells
    # better, but nothing checked where it put them: it is one call, like
    # single_pass, with the spans in the prompt instead of a pixel ink test.
    # It gets its own status because the difference from single_pass is real
    # and the difference from grid_cells is also real -- claiming grid_cells
    # here would let the normalizer keep the model's blank-cell padding as
    # occurrences, on a path where no ink test ever ran.
    "grid_guided",
    # The labels-only path was used and no cell reading was attempted at all.
    "not_attempted",
    # A cell reading was attempted and did not produce usable output.
    "failed",
)


DEFAULT_OCCURRENCE_CELL_STATUS = "single_pass_unverified"


__all__ = [
    '_add_row_flag',
    'normalize_response_kind',
    'has_value',
    'RESPONSE_EXTRACTED',
    'LOW_CONFIDENCE_CODE',
    'RESPONSE_NOT_DRAWN',
    'normalize_codes',
    'VALID_OCCURRENCE_MODES',
    'VALID_ENDPOINT_KINDS',
    '_KNOWN_RANGE_CHART_KEYS',
    '_KNOWN_SECTION_KEYS',
    '_KNOWN_SPECIES_KEYS',
    '_KNOWN_BIOZONE_KEYS',
    '_IRON_RULE_ZONE_RE',
    '_NAME_OPEN_NOMINATURE',
    '_NAME_QUALIFIERS',
    '_name_label_parts',
    '_is_ascii_lowercase_start',
    '_is_wrapped_header_fragment',
    '_is_bare_epithet',
    '_is_wrappable_header_base',
    '_is_bare_authority',
    '_row_is_iron_rule_rejected',
    '_guard_wrapped_header_rows',
    '_classify_array_item',
    '_carry_extras',
    '_coerce_bool_flag',
    '_row_from_string',
    '_single_record_from_dict',
    '_iter_rows',
    '_dict_rows',
    '_PRIMARY_ID_KEYS',
    '_merge_other_fossils',
    '_other_fossils_from',
    '_stringify_scalar',
    '_pop_array_root_extras',
    '_extracted_any',
    '_NON_CONTENT_KEYS',
    '_unwrap_array_root_into',
    '_append_warning',
    '_rehydrate_array_root',
    '_confidence_clamped',
    'POS_SCALE_KEY',
    'POS_SUFFIX',
    'POS_MIN',
    'POS_MAX',
    'GEOMETRY_VERSION',
    'GEOMETRY_SCALE',
    'GEOMETRY_TOLERANCE',
    '_CONTRACT_SOURCE_KEYS',
    '_GEOMETRY_AXIS_BY_FIELD',
    '_GEOMETRY_VERTICAL_FIELDS',
    '_geometry_axis_for',
    '_GEOMETRY_SEMANTIC_KEYS',
    '_to_float_opt',
    'normalize_pos_0_999',
    '_axis_domain',
    '_axis_calibration_blocks',
    'axis_domains_from',
    'axis_calibration_unusable',
    '_fold_axis_calibration',
    'pos_to_axis_value',
    '_geometry_point',
    'geometry_from_row',
    'apply_coverage_contract',
    'attach_row_contract',
    '_KNOWN_COLUMNAR_SECTION_KEYS',
    '_KNOWN_BLOCK_KEYS',
    '_KNOWN_UNIT_KEYS',
    '_KNOWN_SAMPLE_KEYS',
    '_KNOWN_LEGEND_KEYS',
    '_KNOWN_CROSS_KEYS',
    '_KNOWN_COLUMNAR_ROOT_KEYS',
    '_KNOWN_ABUNDANCE_ROOT_KEYS',
    '_KNOWN_SITE_KEYS',
    '_KNOWN_ABUNDANCE_KEYS',
    '_KNOWN_ZONE_KEYS',
    '_KNOWN_CHEMICAL_STRAT_ROOT_KEYS',
    '_KNOWN_CHEMICAL_STRAT_DATA_POINT_KEYS',
    '_KNOWN_CHEMICAL_STRAT_EVENT_KEYS',
    '_KNOWN_CHEMICAL_STRAT_INTERVAL_KEYS',
    '_KNOWN_PALEOMAP_ROOT_KEYS',
    '_KNOWN_PALEOMAP_CONTINENT_KEYS',
    '_KNOWN_PALEOMAP_SEA_KEYS',
    '_KNOWN_PALEOMAP_TECTONIC_KEYS',
    '_KNOWN_PALEOMAP_REALM_KEYS',
    '_KNOWN_PALEOMAP_SITE_KEYS',
    '_KNOWN_PALEOMAP_INDICATOR_KEYS',
    '_PALEOMAP_ARRAY_SPEC_KEYS',
    'first_non_empty',
    '_KNOWN_SCATTER_PLOT_ROOT_KEYS',
    '_KNOWN_SCATTER_GROUP_KEYS',
    '_KNOWN_SCATTER_POINT_KEYS',
    '_KNOWN_SCATTER_OUTLIER_KEYS',
    '_SCATTER_ARRAY_SPEC_KEYS',
    '_KNOWN_ZONATION_ROOT_KEYS',
    '_KNOWN_ZONATIONS_KEYS',
    '_KNOWN_ZONATION_ZONE_KEYS',
    '_KNOWN_CORRELATION_KEYS',
    'OCCURRENCE_CELL_STATUSES',
    'DEFAULT_OCCURRENCE_CELL_STATUS',
]
