"""Lenient JSON extraction, ported from RLPE range_chart_extractor."""

from __future__ import annotations

import json
import re
from typing import Any


def extract_balanced_json_object(text: str) -> str | None:
    """Return the first balanced {...} JSON object substring, or None.

    Handles nested braces and braces inside string literals correctly.
    Fixed: escape sequence handling now correctly skips the character after
    a backslash so that "\\" (escaped backslash) and "\\"" (escaped quote)
    are processed correctly.
    """
    start = text.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escape = False
        i = start
        while i < len(text):
            c = text[i]
            if escape:
                # Any character immediately after a backslash is escaped —
                # skip it entirely and reset escape state. This correctly
                # handles \\" (escaped quote), \\\\" (escaped backslash + quote),
                # and any other escape sequence.
                escape = False
                i += 1
                continue
            if in_string:
                if c == "\\":
                    # Start of an escape sequence — mark and skip next char.
                    escape = True
                    i += 1
                    continue
                elif c == '"':
                    in_string = False
            else:
                if c == '"':
                    in_string = True
                elif c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        return text[start : i + 1]
            i += 1
        start = text.find("{", start + 1)
    return None


# --- Strict JSON parser (rejects NaN / Infinity, NaN / Infinity constants)
# Python's json.loads accepts NaN, Infinity, -Infinity by default (and even
# serializes them back as the same tokens) which is convenient for `eval`
# but is NOT valid JSON per RFC 8259 and is rejected by every standard
# JSON parser (including `JSON.parse` in the browser). To keep Python and
# JS on the same page we pass `parse_constant` so any non-standard
# numeric constant raises ValueError rather than being silently accepted.
def _strict_parse_constant(_const: str):  # pragma: no cover - exercised via safe_json_loads
    raise ValueError(
        f"non-standard JSON constant {_const!r} rejected (use null instead)"
    )


def _strict_json_loads(s: str) -> Any:
    """json.loads with strict reject for NaN / Infinity / -Infinity constants.

    This is the canonical parse used by safe_json_loads at every level so
    the Python and JS engines make identical accept/reject decisions for
    numeric constants. Mirrors `JSON.parse` semantics in js/json-utils.js.
    """
    return json.loads(s, parse_constant=_strict_parse_constant)


# --- F-1 HIGH: enumerate ALL balanced JSON objects, score, pick best
# Keys whose presence (at any level of nesting) marks an object as a
# "real payload" rather than a schema/example the model restated in prose.
# Adding to this list is safe; the scorer is strictly additive.
_PAYLOAD_KEYS: frozenset[str] = frozenset({
    "species_ranges",
    "sections",
    "biozones",
    "other_fossils",
    "confidence",
    "format",  # not a payload key, but a schema-key giveaway
    "schema_version",
    "$schema",
    "required",  # JSON-Schema's "required" array
    "properties",
    "example",
})


def _payload_score(parsed: Any) -> int:
    """Score a parsed JSON object on how payload-like it looks.

    Higher = more likely to be the real record. Schema/example objects
    have negative contributions so the real payload wins even when the
    schema is the larger object.
    """
    score = 0
    if not isinstance(parsed, dict):
        # Arrays are scored purely on key-recognition in their dict items.
        if isinstance(parsed, list):
            for item in parsed:
                if isinstance(item, dict):
                    score += _payload_score(item)
        return score
    keys = set(parsed.keys())
    payload_hits = keys & _PAYLOAD_KEYS
    # Real payload keys carry positive weight, schema-ish keys carry
    # negative weight (so a bare schema like {"format", "required"} loses
    # to {"species_ranges"}).
    score += len(payload_hits & {
        "species_ranges", "sections", "biozones",
        "other_fossils", "confidence",
    }) * 100
    score -= len(payload_hits & {
        "format", "schema_version", "$schema", "required", "properties", "example",
    }) * 50
    # Nesting: scan dict/list values for payload keys.
    for v in parsed.values():
        if isinstance(v, (dict, list)):
            score += max(0, _payload_score(v)) // 4
    return score


def _try_parse_object(s: str) -> Any | None:
    """Try strict parsing; return parsed dict/list, or None on any failure."""
    try:
        return _strict_json_loads(s)
    except Exception:
        return None


def extract_all_balanced_json_objects(text: str) -> list[str]:
    """Return every balanced ``{...}`` JSON object substring in order.

    Used by ``safe_json_loads`` to score candidates and pick the most
    payload-like when the model left multiple JSON objects in its reply
    (e.g. a schema example in prose followed by the actual data).

    Adjacent balanced objects (no whitespace between them) are not merged.
    Invalid JSON substrings (e.g. unmatched braces) are skipped.
    Fixed: same escape-sequence bug as extract_balanced_json_object.
    """
    if not text:
        return []
    results: list[str] = []
    start = text.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escape = False
        i = start
        while i < len(text):
            c = text[i]
            if escape:
                escape = False
                i += 1
                continue
            if in_string:
                if c == "\\":
                    escape = True
                    i += 1
                    continue
                elif c == '"':
                    in_string = False
            else:
                if c == '"':
                    in_string = True
                elif c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        candidate = text[start: i + 1]
                        if _try_parse_object(candidate) is not None:
                            results.append(candidate)
                        break
            i += 1
        start = text.find("{", start + 1)
    return results


def extract_balanced_json_array(text: str) -> str | None:
    """Mirror of `extract_balanced_json_object` for top-level arrays.

    Some models occasionally wrap the response in `[…]` instead of `{…}`
    — usually a malformed attempt at returning multiple records. We still
    want to surface something useful rather than raise ValueError and
    leave the caller with nothing. The extracted substring is fed back
    to `json.loads`; non-array payloads will fail there with a clear
    error rather than silently being treated as an object.
    Fixed: same escape-sequence bug as extract_balanced_json_object.
    """
    start = text.find("[")
    while start != -1:
        depth = 0
        in_string = False
        escape = False
        i = start
        while i < len(text):
            c = text[i]
            if escape:
                # Any escaped character — skip it and reset.
                escape = False
                i += 1
                continue
            if in_string:
                if c == "\\":
                    escape = True
                    i += 1
                    continue
                elif c == '"':
                    in_string = False
            else:
                if c == '"':
                    in_string = True
                elif c == "[":
                    depth += 1
                elif c == "]":
                    depth -= 1
                    if depth == 0:
                        return text[start : i + 1]
            i += 1
        start = text.find("[", start + 1)
    return None


# Sprint B (REVIEW-2026-09-04): payload root keys used to tell a REAL data
# fence apart from a JSON-Schema / example fence the model restated in prose
# before its actual answer. This is the union of the per-mode root-key
# constants declared in rca_core/extractor.py:
#   * RANGE_CHART_ROOTS                (extractor.normalize_result)
#   * _KNOWN_COLUMNAR_ROOT_KEYS        (columnar-section mode)
#   * _KNOWN_ABUNDANCE_ROOT_KEYS       (abundance-diagram mode)
#   * _KNOWN_CHEMICAL_STRAT_ROOT_KEYS  (chemical-stratigraphy mode)
#   * _KNOWN_PALEOMAP_ROOT_KEYS        (paleomap mode)
#   * _KNOWN_SCATTER_PLOT_ROOT_KEYS    (scatter-plot mode)
#   * phylo-tree root keys             (_normalize_phylogenetic_tree_into:
#                                      metadata / nodes / root_ids / legend)
# Deliberately NOT part of the set:
#   * "_extras"    — an artifact the normalizers ATTACH to their output;
#                    it is never a root key of a raw model payload.
#   * "_array_root" — an internal safe_json_loads wrapper, likewise never
#                    present in the raw model text.
_KNOWN_ROOT_KEYS: frozenset[str] = frozenset({
    # range_chart (RANGE_CHART_ROOTS)
    "sections", "species_ranges", "biozones", "other_fossils", "confidence",
    # columnar_section (_KNOWN_COLUMNAR_ROOT_KEYS)
    "fossil_legend", "lithology_legend", "cross_beds", "overall_confidence",
    # abundance_diagram (_KNOWN_ABUNDANCE_ROOT_KEYS)
    "sites", "abundances", "zones",
    # chemical_stratigraphy (_KNOWN_CHEMICAL_STRAT_ROOT_KEYS)
    "data_points", "events", "intervals",
    # paleomap (_KNOWN_PALEOMAP_ROOT_KEYS)
    "continents", "oceans_seas", "tectonic_features", "biogeographic_realms",
    "fossil_sites", "paleolatitude_indicators",
    # scatter_plot (_KNOWN_SCATTER_PLOT_ROOT_KEYS)
    "groups", "points", "outliers", "statistics",
    # phylogenetic_tree (inline tuple in _normalize_phylogenetic_tree_into)
    "metadata", "nodes", "root_ids", "legend",
})


def _looks_like_payload_root(parsed: Any) -> bool:
    """True when *parsed* is a dict containing at least one known root key."""
    return isinstance(parsed, dict) and bool(_KNOWN_ROOT_KEYS & parsed.keys())


def strip_markdown_fence(text: str) -> str:
    """Strip markdown code fences (```json ... ```) from a model response.

    Models frequently wrap their JSON in fenced code blocks. This handles
    several variants:
      - ```json ... ```  (fence with language tag)
      - ``` ... ```       (bare fence)
      - leading ``` with no trailing fence (truncated response)
      - multiple fences (see below)

    Sprint B (REVIEW-2026-09-04): with MULTIPLE fences the previous
    non-greedy regex returned only the FIRST block, so a reply that restated
    a JSON-Schema example in one fence and put the real payload in a later
    fence had the schema evict the real data. Now ALL fenced blocks are
    collected and strictly parsed one by one; the FIRST block that parses to
    a dict containing one of the ``_KNOWN_ROOT_KEYS`` is returned. If no
    block qualifies (unparseable blocks, or dicts without any known root
    key) the original behaviour is preserved and the FIRST block is
    returned, so existing single-fence callers see no change.

    Returns the cleaned string unchanged if no fence is present.
    """
    if not text:
        return text
    s = str(text).strip()
    # If the whole text is one fenced block, extract its inner content.
    fence_block = re.match(
        r"^```(?:json)?\s*\n?(.*?)\n?```\s*$", s, re.DOTALL | re.IGNORECASE
    )
    if fence_block:
        return fence_block.group(1).strip()
    # Sprint B (REVIEW-2026-09-04): collect every fenced block in the text
    # and prefer the first one that actually looks like the real payload.
    blocks = [
        b.strip()
        for b in re.findall(
            r"```(?:json)?\s*\n?(.*?)\n?```", s, re.DOTALL | re.IGNORECASE
        )
    ]
    if blocks:
        for block in blocks:
            if _looks_like_payload_root(_try_parse_object(block)):
                return block
        # No block carries a known root key — keep the historical
        # first-block behaviour (safe_json_loads' later fallback chain
        # still gets a chance to rescue the right object).
        return blocks[0]
    # Otherwise strip any leading/trailing fence lines defensively.
    s = re.sub(r"^```(?:json)?\s*", "", s, flags=re.MULTILINE | re.IGNORECASE)
    s = re.sub(r"\s*```$", "", s, flags=re.MULTILINE)
    return s


def extract_json_like(text: str) -> str | None:
    """Find the first JSON object or array inside prose.

    Models sometimes embed JSON inside explanatory text, e.g.
      "Here is the result:\n{\"a\":1}\nLet me know if you need more."
    or wrap it in markdown. This locates the first `{` or `[` and extracts
    the balanced substring from there. Returns None if nothing balanced is
    found.
    """
    if not text:
        return None
    s = str(text).strip()
    # Prefer the first '{' (object); fall back to first '[' (array).
    for opener, extractor in (("{", extract_balanced_json_object),
                               ("[", extract_balanced_json_array)):
        start = s.find(opener)
        if start != -1:
            candidate = extractor(s[start:])
            if candidate is not None:
                return candidate
    return None


def _promote_wrapper(parsed: dict[str, Any]) -> dict[str, Any]:
    """Lift a payload nested one level under a common wrapper key.

    REVIEW-2026-07-31 (M2 regression): the nested-candidate filter keeps
    the OUTER object when a model wraps the real payload in ``{"data":
    {...}}`` — but for a PURE wrapper (no sibling metadata fields) the
    payload ended up nested and the normalizers saw an empty result. When
    the parsed object has a dict under ``data`` / ``result`` / ``payload``
    / ``response`` / ``output``, promote that inner dict's keys to the top
    level (wrapper fields are preserved; inner keys win only when absent).
    """
    if not isinstance(parsed, dict):
        return parsed
    for key in ("data", "result", "payload", "response", "output"):
        inner = parsed.get(key)
        if isinstance(inner, dict) and inner:
            promoted = dict(parsed)
            promoted.pop(key, None)
            for k, v in inner.items():
                promoted.setdefault(k, v)
            return promoted
    return parsed


def _repair_truncated_json(text: str) -> str | None:
    """Best-effort repair of a TRUNCATED JSON object/array.

    E2E finding (UI-REVIEW-2026-09-07, fig_19): when the model hits the
    max_tokens ceiling mid-array, Level 4's balanced-substring enumeration
    only sees the inner ROW objects and picks an arbitrary one — the
    dozens of completed rows above the cut were discarded with the
    unbalanced outer object.

    Strategy: single forward scan tracking the bracket/string state and
    recording every "element boundary" (position right after a ``}``, `]``,
    ``"``-closed string, or ``,``) together with the open-bracket stack at
    that point. Then, from the LONGEST boundary backwards, close the stack
    and strict-parse; the first repair that succeeds wins. ``null`` when
    nothing repairs.

    """
    s = text.strip()
    if not s or s[0] not in "{[":
        return None
    stack: list[str] = []
    in_string = False
    escape = False
    outer_ever_closed = False
    boundaries: list[tuple[int, tuple[str, ...]]] = []
    for i, c in enumerate(s):
        if escape:
            escape = False
            continue
        if in_string:
            if c == "\\":
                escape = True
            elif c == '"':
                in_string = False
                if not stack:
                    # UI-REVIEW-2026-09-07: the outer object already closed
                    # (only stray prose remains) - not a truncation; Level 4
                    # handles this correctly, so do not repair.
                    return None
                boundaries.append((i + 1, tuple(stack)))
            continue
        if c == '"':
            in_string = True
        elif c in "{[":
            stack.append(c)
        elif c in "}]":
            if stack:
                stack.pop()
            if not stack:
                # Outer object fully closed mid-text (stray prose after it):
                # not a truncation either.
                return None
            boundaries.append((i + 1, tuple(stack)))
        elif c == ",":
            boundaries.append((i + 1, tuple(stack)))
    if not boundaries:
        return None
    for idx, open_stack in reversed(boundaries):
        if not open_stack:
            # The prefix is already balanced — Level 3 would have parsed it.
            continue
        closers = "".join("}" if o == "{" else "]" for o in reversed(open_stack))
        candidate = s[:idx].rstrip().rstrip(",") + closers
        try:
            _strict_json_loads(candidate)
        except Exception:
            continue
        return candidate
    return None


def safe_json_loads(text: str) -> dict[str, Any]:
    """Lenient JSON object parse with a 6-level fallback chain.

    The chain (each step only runs if the previous one failed):
      1. Strip markdown fences (```json ... ```).
      2. Strip raw control characters (0x00-0x1F except \\t\\r\\n).
      3. Strict ``json.loads`` — handles clean JSON and top-level arrays.
         Non-standard numeric constants (NaN/Infinity) are rejected to
         keep behaviour identical to JS ``JSON.parse``.
      4. Balanced-object scoring: enumerate ALL balanced ``{...}``
         substrings, parse each, score on payload-key hits vs.
         schema/example markers, and pick the highest-scored (tie:
         prefer the LARGER object, then the LAST one). Fixes the case
         where the model restates a JSON-Schema example in prose before
         returning the real payload.
      5. Balanced-bracket array extraction (first ``[...]``) → wrapped.
      6. Prose-embedded JSON (``extract_json_like``) — last resort.

    Raises ValueError only if every level fails.
    """
    if not text:
        raise ValueError("empty text")
    s = str(text).strip()

    # Level 1: strip markdown fences.
    s = strip_markdown_fence(s)

    # Level 2: strip raw control characters that json.loads rejects.
    _ctrl_re = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
    s = _ctrl_re.sub("", s)

    # Level 3: strict parse (rejects NaN/Infinity to match JSON.parse).
    try:
        parsed = _strict_json_loads(s)
        if isinstance(parsed, dict):
            return _promote_wrapper(parsed)
        # H3: a top-level array IS valid JSON; json.loads accepted it.
        # Surface it as a synthetic wrapper so the caller's normalize_*
        # functions (all of which assume a dict) still get something
        # they can introspect instead of crashing with TypeError on
        # `parsed.get(...)`.
        if isinstance(parsed, list):
            return {"_array_root": parsed,
                    "_note": "model returned a top-level array; wrapping for diagnostics"}
    except Exception:
        pass

    # Level 3.5 (UI-REVIEW-2026-09-07): truncated-payload repair. When the
    # model hit max_tokens mid-array, Level 3 fails and Level 4 would only
    # find the inner ROW objects of the (now unbalanced) outer payload —
    # discarding every completed row above the cut. Close the brackets at
    # the last complete element instead; only accept repairs that yield a
    # recognizable payload root (otherwise fall through to Level 4).
    if s and s[0] in "{[":
        repaired = _repair_truncated_json(s)
        if repaired is not None:
            try:
                parsed = _strict_json_loads(repaired)
            except Exception:
                parsed = None
            if isinstance(parsed, dict) and _looks_like_payload_root(parsed):
                return _promote_wrapper(parsed)

    # Level 4: balanced-object enumeration + scoring (HIGH FIX).
    candidates = extract_all_balanced_json_objects(s)
    if candidates:
        # M2 fix (REVIEW-2026-07-25): drop candidates that are nested inside
        # another candidate. When a model wraps the real payload in an outer
        # object (e.g. {"data": {...}, "confidence": ...}), the inner object
        # would otherwise win on payload score and the outer wrapper's
        # sibling fields (metadata / confidence / sections / extra) would be
        # silently dropped. Keeping only top-level (non-nested) objects
        # preserves them; the wrapper is what the caller's normalizer expects.
        candidates = [
            c for c in candidates
            if not any(c != d and c in d for d in candidates)
        ]
        # Parse each and keep only dicts (arrays are surfaced by Level 5).
        parsed_objects = []
        for c in candidates:
            p = _try_parse_object(c)
            if isinstance(p, dict):
                parsed_objects.append((p, c))
        if parsed_objects:
            # H8 fix (REVIEW-2026-07-25): score candidates on PAYLOAD
            # LIKELIHOOD only. The previous score baked string length in via
            # `_payload_score(p) * 10 + len(src)`, so a long schema/example
            # string could outscore a small real payload and the parser would
            # discard the real data. Length is now a TIEBREAKER ONLY: sort by
            # (payload_score, length, index) descending and pick the top.
            scored = []
            for idx, (p, src) in enumerate(parsed_objects):
                scored.append((_payload_score(p), len(src), idx, p))
            # Highest payload score wins; ties broken by longer source, then
            # by later occurrence in the text (the real payload usually
            # follows any schema/example prose).
            scored.sort(key=lambda t: (t[0], t[1], t[2]), reverse=True)
            return _promote_wrapper(scored[0][3])

    # Level 5: balanced-bracket array extraction → wrapped.
    candidate = extract_balanced_json_array(s)
    if candidate is not None:
        try:
            parsed = _strict_json_loads(candidate)
            if isinstance(parsed, list):
                return {"_array_root": parsed,
                        "_note": "model returned a top-level array; wrapping for diagnostics"}
        except Exception:
            pass

    # Level 6: prose-embedded JSON (last resort).
    candidate = extract_json_like(s)
    if candidate is not None:
        try:
            parsed = _strict_json_loads(candidate)
            if isinstance(parsed, dict):
                return _promote_wrapper(parsed)
            if isinstance(parsed, list):
                return {"_array_root": parsed,
                        "_note": "model returned a top-level array; wrapping for diagnostics"}
        except Exception:
            pass

    raise ValueError(f"no JSON object found in {s[:120]!r}")
