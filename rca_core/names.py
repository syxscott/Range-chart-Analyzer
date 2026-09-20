"""Scientific-name verification for extracted species rows.

Borrowed from the GitHub survey (2026-09-07): gnames/gnfinder for name
finding/verification, with the GBIF species-match API as the DEFAULT
backend (gnfinder hosts were unreachable from some networks during the
survey; GBIF is globally reachable and covers fossil taxa well - the E2E
probe resolved even the conodont Hindeodus parvus).

Why: vision OCR of dense italic Latin names is frequently misspelled
(E2E example: "Psendotirolites" -> fuzzy-matched to the correct
"Pseudotirolites", confidence 85). This module attaches fuzzy-match
candidates so an operator can review - per ICZN ethics it NEVER rewrites
extracted data; open-nomenclature qualifiers (cf./aff./sp.) are stripped
only for the LOOKUP, the row keeps its original form.

Fail-open by design: network unreachable / API down / rate limited ->
results carry ``{"status": "unavailable"}`` and no error propagates.
"""

from __future__ import annotations

import json
import re
import time
import urllib.parse
import urllib.request
from typing import Any, Callable, Optional

__all__ = [
    "clean_name_for_lookup",
    "parse_name_gbif",
    "looks_malformed_name",
    "verify_name_gbif",
    "verify_names",
    "name_issues",
    "DEFAULT_BATCH_BUDGET_SECONDS",
    "DEFAULT_GBIF_BASE_URL",
]

# GBIF species-match: free, no key, CORS-enabled, fuzzy matching with
# confidence scores. verbose=true returns alternatives for review.
# BORROW-2026-09-20: the URLs are now built from an injectable base so the
# pre-parser and the backbone match share one host override (tests mock the
# transport with ``fetch``; a staging GBIF mirror only needs ``base_url``).
DEFAULT_GBIF_BASE_URL = "https://api.gbif.org"

_AUTHOR_PAREN = re.compile(r"\([^)]*\)")

# BORROW-2026-09-20 (3): GBIF practice for palaeontology (radiolarians,
# conodonts...) shows two very different NONE stories:
#   * the string is not a name at all (OCR prose, digits, stray glyphs) -
#     the name parser would refuse it, so we refuse it locally and save the
#     round-trip;
#   * the parser parses it but the backbone simply does not carry it.
# The first is an extraction-quality signal, the second is normal science,
# and the error message must keep them apart.
_MALFORMED_RESIDUE_RE = re.compile(r"[^A-Za-z .\-'×]+")

# BORROW-2026-09-20 (2): the literal matchType the GBIF backbone checker
# returns when one string resolves to several equally-good usages (same
# name across kingdoms/phyla - common for radiolarian genera).
_MULTIPLE_EQUAL_MATCHES = "Multiple equal matches"

# REVIEW-2026-09-20 (finding 5): wall-clock budget for a whole batch. One
# GBIF round-trip is ~0.3 s, so 50 names already sit close to a minute;
# without a budget a batch of a few hundred names on a slow network blocked
# the caller long after its own request timed out. Names left over when the
# budget is spent come back as ``unavailable`` (fail-open), never as an
# exception and never as a silently missing key.
DEFAULT_BATCH_BUDGET_SECONDS = 60.0


def _unavailable(reason: str) -> dict[str, Any]:
    """The fail-open result shape used everywhere in this module.

    BORROW-2026-09-20: gained the new public keys (``parsed`` /
    ``ambiguous`` / ``candidates`` / ``unmatched_reason`` / ``original``)
    with neutral defaults so every result dict has one stable shape;
    pre-existing keys are untouched (backward compatible).
    """
    return {
        "status": "unavailable",
        "error": reason,
        "match_type": "NONE",
        "confidence": 0.0,
        "canonical": "",
        "accepted": "",
        "taxonomic_status": "",
        "alternatives": [],
        "usage_key": None,
        "ambiguous": False,
        "candidates": [],
        "parsed": None,
        "genus_match": None,
        "unmatched_reason": "",
        "original": "",
    }


def _unmatched(reason: str = "", original: str = "") -> dict[str, Any]:
    """BORROW-2026-09-20 (3): ``reason`` separates the two NONE flavours -
    ``"malformed"`` (refused by the local gate, no network spent),
    ``"empty_after_clean"`` (nothing queryable left), ``"not_in_backbone"``
    (the parser accepts the string, GBIF simply has no usage) and
    ``"unparsable"`` (the GBIF parser itself refused to parse it).
    """
    return {
        "status": "unmatched",
        "match_type": "NONE",
        "confidence": 0.0,
        "canonical": "",
        "accepted": "",
        "taxonomic_status": "",
        "alternatives": [],
        "usage_key": None,
        "ambiguous": False,
        "candidates": [],
        "parsed": None,
        "genus_match": None,
        "unmatched_reason": reason,
        "original": original,
    }


def _match_endpoint(base_url: str, query: str) -> str:
    return (f"{base_url}/v1/species/match?verbose=true&name="
            + urllib.parse.quote(query))


def _parser_endpoint(base_url: str, query: str) -> str:
    return f"{base_url}/v1/parsers/name?name=" + urllib.parse.quote(query)


def _http_json(
    url: str,
    timeout: float,
    fetch: Optional[Callable[[str], str]],
) -> tuple[Any, str]:
    """GET ``url`` and decode JSON. Returns ``(payload, "")`` or
    ``(None, error_reason)`` - never raises (BORROW-2026-09-20)."""
    try:
        if fetch is None:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                body = resp.read().decode("utf-8", "replace")
        else:
            body = fetch(url)
        return json.loads(body), ""
    except Exception as exc:  # network / DNS / rate limit / bad JSON
        return None, type(exc).__name__


def looks_malformed_name(query: str) -> bool:
    """BORROW-2026-09-20 (3): local triage of strings that the GBIF name
    parser could never parse (digits, non-Latin glyphs, more than a
    quartet of tokens). Deliberately conservative - anything name-like
    passes so the real decision stays with the parser; a false PASS only
    costs one round-trip, a false REJECT would silence a real taxon.
    """
    s = str(query or "").strip()
    if not s:
        return True
    if len(s.split()) > 4:
        return True
    return bool(_MALFORMED_RESIDUE_RE.search(s))


def _empty_parse(status: str = "unavailable", error: str = "") -> dict[str, Any]:
    return {
        "status": status,
        "error": error,
        "parsed": False,
        "genus": "",
        "specific_epithet": "",
        "infraspecific_epithet": "",
        "authorship": "",
        "parser_type": "",
        "canonical": "",
    }


def parse_name_gbif(
    name: str,
    timeout: float = 15.0,
    fetch: Optional[Callable[[str], str]] = None,
    base_url: Optional[str] = None,
) -> dict[str, Any]:
    """BORROW-2026-09-20 (1): pre-parse one (cleaned) name with the GBIF
    name parser (``GET /v1/parsers/name?name=...``, the same endpoint the
    checker layer uses internally).

    Returns the parser verdict decomposed into reviewable fields::

        {"status": "ok"|"unavailable", "error", "parsed": bool,
         "genus", "specific_epithet", "infraspecific_epithet",
         "authorship", "parser_type", "canonical"}

    Never raises; an unreachable parser degrades to ``status
    "unavailable"`` and the caller falls back to the raw cleaned string.
    """
    out = _empty_parse()
    try:
        base = str(base_url or DEFAULT_GBIF_BASE_URL).rstrip("/")
        s = str(name or "").strip()
        if not s:
            out["error"] = "empty_name"
            return out
        payload, err = _http_json(_parser_endpoint(base, s), timeout, fetch)
        if err:
            out["error"] = err
            return out
        if not isinstance(payload, dict):
            out["error"] = f"unexpected_payload_{type(payload).__name__}"
            return out
        out["status"] = "ok"
        out["genus"] = str(payload.get("genus") or "")
        out["specific_epithet"] = str(payload.get("specificEpithet") or "")
        out["infraspecific_epithet"] = str(
            payload.get("infraspecificEpithet") or "")
        out["authorship"] = str(payload.get("authorship") or "")
        out["parser_type"] = str(
            payload.get("type") or payload.get("matchType") or "")
        out["canonical"] = str(payload.get("canonical") or "")
        parsed_flag = payload.get("parsed")
        if parsed_flag is None:
            # No explicit flag: treat "the parser saw a genus" as parsed.
            out["parsed"] = bool(out["genus"] or out["specific_epithet"])
        else:
            out["parsed"] = bool(parsed_flag)
        return out
    except Exception as exc:  # pragma: no cover - defensive belt
        out["error"] = type(exc).__name__
        return out


def _as_float(value: Any) -> float:
    """Best-effort numeric read of an API field.

    GBIF sends ``confidence`` as a number, but a proxy / a differently shaped
    payload (string scores, ``null``, a list) used to raise ``ValueError``
    straight out of the "never raises" contract of this module.
    """
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    try:
        text = str(value or "").strip()
        return float(text) if text else 0.0
    except (TypeError, ValueError):
        return 0.0


def clean_name_for_lookup(species: str) -> str:
    """Reduce an extracted species string to the queryable binomen.

    ``"Pseudotirolites cf. P. asiaticus (Zheng, 1979)"`` ->
    ``"Pseudotirolites asiaticus"``. Genera-only entries (``"Genus sp."``)
    return the bare genus (GBIF resolves genus-rank names). Returns ""
    when nothing queryable remains.
    """
    s = str(species or "").strip()
    if not s:
        return ""
    # drop author/year parentheticals
    s = _AUTHOR_PAREN.sub(" ", s)
    # drop open-nomenclature qualifier tokens (the row itself is untouched)
    s = re.sub(r"\b(cf|aff|cf\.|aff\.)\s+", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\b(ex\s+gr\.?|gr\.?|s\.\s?l\.?|s\.\s?s\.?|sensu|near)(?![a-z])",
               " ", s, flags=re.IGNORECASE)
    s = re.sub(r"(?i)\bsp\.?$", "", s.strip())
    s = re.sub(r"\?", "", s)
    # abbreviated genus "P. asiaticus" -> the genus cannot be recovered,
    # drop the abbreviation instead of querying a broken binomen.
    s = re.sub(r"(^|\s)[A-Z]\.\s*(?=[a-z])", r"\1", s)
    s = re.sub(r"\s+", " ", s).strip(" .,-")
    # A "name" longer than this is prose, not a taxon - querying it wastes
    # a request and can never match.
    if len(s) > 100:
        return ""
    return s


def _candidate(usage: dict[str, Any]) -> dict[str, Any]:
    """BORROW-2026-09-20 (2): one disambiguation candidate as the report /
    export side needs it: usageKey + rank + higher taxonomy + match shape."""
    return {
        "usage_key": usage.get("usageKey"),
        "canonical": str(usage.get("canonicalName") or ""),
        "scientific_name": str(usage.get("scientificName") or ""),
        "rank": str(usage.get("rank") or ""),
        "kingdom": str(usage.get("kingdom") or ""),
        "phylum": str(usage.get("phylum") or ""),
        "match_type": str(usage.get("matchType") or ""),
        "confidence": _as_float(usage.get("confidence")),
        "status": str(usage.get("status") or ""),
    }


def verify_name_gbif(
    name: str,
    timeout: float = 15.0,
    fetch: Optional[Callable[[str], str]] = None,
    base_url: Optional[str] = None,
    preparse: bool = True,
) -> dict[str, Any]:
    """Query the GBIF species-match API for one (cleaned) name.

    Returns ``{"status": "ok", "match_type", "confidence", "canonical",
    "accepted", "alternatives"}`` or ``{"status": "unavailable"}`` /
    ``{"status": "unmatched"}``. ``fetch`` injects the HTTP GET body
    (tests monkeypatch it); production uses urllib with a hard timeout.

    REVIEW-2026-09-20 (finding 5): NEVER raises. Previously the response was
    decoded inside the ``try`` but interpreted OUTSIDE it, so a payload that
    is a JSON list / string / number (an error page, a proxy body, an API
    shape change) escaped as ``AttributeError`` — or a non-numeric
    ``confidence`` as ``ValueError`` — and killed the whole batch. Parsing
    and every field conversion now live behind the same guard and degrade to
    ``unavailable``.

    Tail of the same finding (this round): the cleaning step and the URL
    build sat OUTSIDE the guard, so a name whose ``str()`` explodes, or one
    carrying a lone surrogate that ``urllib.parse.quote`` cannot encode
    (both reachable from a hand-edited / OCR'd payload), still escaped the
    "never raises" contract. Everything the function does now happens inside
    it; only the result shape is decided outside.

    BORROW-2026-09-20 (GBIF naming domain) adds, all backward compatible
    (only NEW keys, never a changed or removed one):
    * ``preparse`` (1): the name parser runs first; its structured genus
      feeds a follow-up backbone query so a misspelled epithet can still
      land on the right genus (radiolarian practice), and the verdict is
      mirrored in ``parsed``;
    * ``ambiguous`` + ``candidates`` (2): ``"Multiple equal matches"`` (and
      genus-rank matches with equal-strength alternatives) are reported as
      a candidate list instead of silently taking the first hit;
    * ``unmatched_reason`` (3): ``"malformed"`` (rejected locally, zero
      network) vs ``"not_in_backbone"`` / ``"unparsable"`` (parser verdict).
    ``base_url`` injects the GBIF host (defaults to
    :data:`DEFAULT_GBIF_BASE_URL`) for offline mocks or a mirror.
    """
    try:
        base = str(base_url or DEFAULT_GBIF_BASE_URL).rstrip("/")
        original = str(name or "")
        q = clean_name_for_lookup(original)
        if not q:
            return _unmatched("empty_after_clean", original)
        # BORROW-2026-09-20 (3): malformed strings never reach the network.
        if looks_malformed_name(q):
            return _unmatched("malformed", original)
        # BORROW-2026-09-20 (1): parser pre-resolution before the match.
        parsed: Optional[dict[str, Any]] = None
        if preparse:
            parsed = parse_name_gbif(q, timeout=timeout, fetch=fetch,
                                     base_url=base)
        payload, err = _http_json(_match_endpoint(base, q), timeout, fetch)
        if err:
            return _unavailable(err)
        if not isinstance(payload, dict):
            # A JSON array / scalar is not a species-match document.
            return _unavailable(f"unexpected_payload_{type(payload).__name__}")
        alternates: list[dict[str, Any]] = []
        candidates: list[dict[str, Any]] = []
        alternatives = payload.get("alternatives")
        if isinstance(alternatives, (list, tuple)):
            for alt in alternatives[:3]:
                if not isinstance(alt, dict):
                    continue
                alternates.append({
                    "canonical": str(alt.get("canonicalName") or ""),
                    "match_type": str(alt.get("matchType") or ""),
                    "confidence": _as_float(alt.get("confidence")),
                    "status": str(alt.get("status") or ""),
                })
                # BORROW-2026-09-20 (2): the richer candidate view.
                candidates.append(_candidate(alt))
        mt = str(payload.get("matchType") or "NONE")
        rank = str(payload.get("rank") or "")
        result: dict[str, Any] = {
            "status": "ok",
            "match_type": mt,
            "confidence": _as_float(payload.get("confidence")),
            "canonical": str(payload.get("canonicalName") or ""),
            "accepted": str(payload.get("scientificName") or ""),
            "taxonomic_status": str(payload.get("status") or ""),
            "alternatives": alternates,
            "usage_key": payload.get("usageKey"),
            "ambiguous": False,
            "candidates": candidates,
            "parsed": parsed,
            "genus_match": None,
            "unmatched_reason": "",
            "original": original,
        }
        # BORROW-2026-09-20 (2): multiple equal matches -> DO NOT pick one.
        # The literal checker verdict, or a genus-rank hit whose
        # alternatives are just as strong, flags the row for a human.
        if mt == _MULTIPLE_EQUAL_MATCHES:
            result["ambiguous"] = True
        elif (mt in ("EXACT", "HIGHERRANK", "SYNONYM", "DOUBTFUL")
                and rank.upper() == "GENUS" and len(candidates) >= 1):
            equal = [c for c in candidates
                     if c["confidence"] >= result["confidence"]
                     and c["match_type"] not in ("", "NONE")]
            if equal:
                result["ambiguous"] = True
        if result["ambiguous"] and not candidates and result["usage_key"]:
            candidates.append(_candidate(payload))
            result["candidates"] = candidates
        if mt == "NONE":
            parsed_ok = bool(parsed and parsed.get("status") == "ok")
            if parsed_ok and not parsed.get("parsed"):
                result["unmatched_reason"] = "unparsable"
            else:
                result["unmatched_reason"] = "not_in_backbone"
            # BORROW-2026-09-20 (1): a parsed genus queried on its own often
            # still hits the backbone even when the full binomen misses it
            # (new species in a known genus) - record that partial answer
            # instead of returning an empty NONE.
            genus = str((parsed or {}).get("genus") or "")
            if genus and genus.lower() != q.lower():
                g_payload, g_err = _http_json(
                    _match_endpoint(base, genus), timeout, fetch)
                if not g_err and isinstance(g_payload, dict):
                    g_mt = str(g_payload.get("matchType") or "NONE")
                    if g_mt != "NONE":
                        result["genus_match"] = _candidate(g_payload)
                        result["genus_match"]["match_type"] = g_mt
        return result
    except Exception as exc:  # network / DNS / rate limit / bad JSON -> fail open
        return _unavailable(f"{type(exc).__name__}")


def verify_names(
    names: list[str],
    backend: str = "gbif",
    timeout: float = 15.0,
    fetch: Optional[Callable[[str], str]] = None,
    budget: float = DEFAULT_BATCH_BUDGET_SECONDS,
    base_url: Optional[str] = None,
    cache: Any = None,
) -> dict[str, dict[str, Any]]:
    """Verify a batch of extracted species strings.

    Returns ``{original_name: verification_dict}``. Deduplicated and
    lookup-cleaned internally; results are keyed by the ORIGINAL string so
    callers can annotate rows without re-matching. Fail-open per name.

    REVIEW-2026-09-20 (finding 5):
    * every name is verified behind its own ``try`` — one malformed row
      (``{"species": {"evil": 1}}``, a list, an object whose ``str()``
      explodes) used to abort the whole batch and propagate out of a
      function documented never to raise;
    * the batch honours a wall-clock ``budget`` (seconds, default
      :data:`DEFAULT_BATCH_BUDGET_SECONDS`; ``None``/``<=0`` disables it) —
      once it is spent the remaining names return ``unavailable`` instead of
      issuing more network calls.

    BORROW-2026-09-20 (4): ``cache`` optionally accepts anything shaped like
    :class:`rca_core.cache.ResultCache` (``make_key``/``get``/``put``) —
    the pattern the server already uses via ``_cache_singleton_safe()``. The
    per-query verification (including the parser pre-resolution result) is
    reused across runs; cache trouble is swallowed (the cache stays an
    optimisation, never a 500, and ``unavailable`` results are never stored
    so a transient network outage cannot poison the cache). New keywords are
    optional; the old positional/keyword call form is unchanged.
    """
    out: dict[str, dict[str, Any]] = {}
    cache_memo: dict[str, dict[str, Any]] = {}
    started = time.monotonic()
    # Whole-batch allowance in seconds (checked before every round-trip; the
    # per-name ``timeout`` bounds a single request, this bounds their sum).
    batch_budget = budget if budget and budget > 0 else None

    def _disk_get(query: str) -> Optional[dict[str, Any]]:
        """BORROW-2026-09-20: persistent read; any cache defect = miss."""
        if cache is None:
            return None
        try:
            key = cache.make_key(kind="gbif_name", name=query,
                                 base_url=str(base_url or DEFAULT_GBIF_BASE_URL))
            hit = cache.get(key)
            return hit if isinstance(hit, dict) and hit.get("status") else None
        except Exception:
            return None

    def _disk_put(query: str, result: dict[str, Any]) -> None:
        if cache is None or result.get("status") == "unavailable":
            return
        try:
            key = cache.make_key(kind="gbif_name", name=query,
                                 base_url=str(base_url or DEFAULT_GBIF_BASE_URL))
            cache.put(key, dict(result))
        except Exception:
            pass

    for original in names:
        try:
            original = str(original or "").strip()
        except Exception:
            # Un-stringifiable object: keep the result set complete rather
            # than losing every later name to one bad entry.
            continue
        if not original or original in out:
            continue
        if batch_budget is not None and (time.monotonic() - started) >= batch_budget:
            # Budget exhausted: report the remaining names honestly.
            out[original] = _unavailable("batch_budget_exceeded")
            continue
        query = ""
        try:
            query = clean_name_for_lookup(original)
            if not query:
                out[original] = _unmatched("empty_after_clean", original)
                continue
            if query in cache_memo:
                out[original] = dict(cache_memo[query])
                continue
            disk_hit = _disk_get(query)
            if disk_hit is not None:
                cache_memo[query] = disk_hit
                out[original] = dict(disk_hit)
                continue
            if backend == "gbif":
                result = verify_name_gbif(query, timeout=timeout, fetch=fetch,
                                          base_url=base_url)
            else:  # unknown backend -> fail open (gnfinder adapter reserved)
                result = _unavailable(f"unknown backend {backend!r}")
        except Exception as exc:  # pragma: no cover - defensive belt
            result = _unavailable(f"{type(exc).__name__}")
        if query:
            cache_memo[query] = dict(result)
            _disk_put(query, result)
        out[original] = dict(result)
    return out


def name_issues(
    verification: dict[str, dict[str, Any]],
    fuzzy_min_confidence: float = 50.0,
) -> list[dict[str, Any]]:
    """Convert verification results into quality-style issue dicts.

    * FUZZY matches above ``fuzzy_min_confidence`` become ``info`` issues
      carrying the suggested canonical name (review hint, not a rewrite).
    * NONE matches become ``info`` issues (name absent from the reference
      database - common for endemic or poorly known taxa; NOT by itself
      evidence of an extraction error).
    * "unavailable" backends stay silent (no network -> no noise).

    BORROW-2026-09-20 (2)/(3): the NONE issue now carries
    ``reason`` (``malformed`` vs ``not_in_backbone`` / ``unparsable``) so
    the report can tell an extraction defect from an honest gap, and
    ambiguous backbone answers ("Multiple equal matches", same name in
    several kingdoms/phyla - radiolarian practice) surface as a
    ``names.ambiguous`` issue with the candidate list for manual
    disambiguation.
    """
    issues: list[dict[str, Any]] = []
    for name, v in verification.items():
        # REVIEW-2026-09-20 (finding 5): the mapping may come from an older
        # run or a hand-edited JSON — a non-dict value used to raise
        # AttributeError here, in a function that only reports hints.
        if not isinstance(v, dict):
            continue
        status = v.get("status")
        if status == "unavailable":
            continue
        mt = str(v.get("match_type") or "NONE")
        conf = _as_float(v.get("confidence"))
        canonical = v.get("canonical") or ""
        if mt == "FUZZY" and conf >= fuzzy_min_confidence and canonical:
            issues.append({
                "severity": "info",
                "msg_key": "names.fuzzy",
                "name": name,
                "suggestion": canonical,
                "confidence": conf,
            })
        elif mt == _MULTIPLE_EQUAL_MATCHES or v.get("ambiguous"):
            # BORROW-2026-09-20 (2): never pick one automatically - hand the
            # candidate list (usageKey + rank + kingdom/phylum) to the
            # reviewer / exporter instead.
            issues.append({
                "severity": "info",
                "msg_key": "names.ambiguous",
                "name": name,
                "suggestion": canonical,
                "candidates": list(v.get("candidates") or []),
            })
        elif mt == "NONE":
            issues.append({
                "severity": "info",
                "msg_key": "names.unmatched",
                "name": name,
                "reason": str(v.get("unmatched_reason") or ""),
            })
    return issues
