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
    "verify_name_gbif",
    "verify_names",
    "name_issues",
    "DEFAULT_BATCH_BUDGET_SECONDS",
]

# GBIF species-match: free, no key, CORS-enabled, fuzzy matching with
# confidence scores. verbose=true returns alternatives for review.
_GBIF_MATCH_URL = (
    "https://api.gbif.org/v1/species/match?verbose=true&name="
)

_AUTHOR_PAREN = re.compile(r"\([^)]*\)")

# REVIEW-2026-09-20 (finding 5): wall-clock budget for a whole batch. One
# GBIF round-trip is ~0.3 s, so 50 names already sit close to a minute;
# without a budget a batch of a few hundred names on a slow network blocked
# the caller long after its own request timed out. Names left over when the
# budget is spent come back as ``unavailable`` (fail-open), never as an
# exception and never as a silently missing key.
DEFAULT_BATCH_BUDGET_SECONDS = 60.0


def _unavailable(reason: str) -> dict[str, Any]:
    """The fail-open result shape used everywhere in this module."""
    return {
        "status": "unavailable",
        "error": reason,
        "match_type": "NONE",
        "confidence": 0.0,
        "canonical": "",
        "accepted": "",
        "taxonomic_status": "",
        "alternatives": [],
    }


def _unmatched() -> dict[str, Any]:
    return {
        "status": "unmatched",
        "match_type": "NONE",
        "confidence": 0.0,
        "canonical": "",
        "accepted": "",
        "alternatives": [],
    }


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


def verify_name_gbif(
    name: str,
    timeout: float = 15.0,
    fetch: Optional[Callable[[str], str]] = None,
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
    """
    try:
        q = clean_name_for_lookup(name)
        if not q:
            return _unmatched()
        url = _GBIF_MATCH_URL + urllib.parse.quote(q)
        if fetch is None:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                body = resp.read().decode("utf-8", "replace")
        else:
            body = fetch(url)
        payload = json.loads(body)
        if not isinstance(payload, dict):
            # A JSON array / scalar is not a species-match document.
            return _unavailable(f"unexpected_payload_{type(payload).__name__}")
        alternates: list[dict[str, Any]] = []
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
        return {
            "status": "ok",
            "match_type": str(payload.get("matchType") or "NONE"),
            "confidence": _as_float(payload.get("confidence")),
            "canonical": str(payload.get("canonicalName") or ""),
            "accepted": str(payload.get("scientificName") or ""),
            "taxonomic_status": str(payload.get("status") or ""),
            "alternatives": alternates,
        }
    except Exception as exc:  # network / DNS / rate limit / bad JSON -> fail open
        return _unavailable(f"{type(exc).__name__}")


def verify_names(
    names: list[str],
    backend: str = "gbif",
    timeout: float = 15.0,
    fetch: Optional[Callable[[str], str]] = None,
    budget: float = DEFAULT_BATCH_BUDGET_SECONDS,
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
    """
    out: dict[str, dict[str, Any]] = {}
    cache: dict[str, dict[str, Any]] = {}
    started = time.monotonic()
    # Whole-batch allowance in seconds (checked before every round-trip; the
    # per-name ``timeout`` bounds a single request, this bounds their sum).
    batch_budget = budget if budget and budget > 0 else None
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
                out[original] = _unmatched()
                continue
            if query in cache:
                out[original] = dict(cache[query])
                continue
            if backend == "gbif":
                result = verify_name_gbif(query, timeout=timeout, fetch=fetch)
            else:  # unknown backend -> fail open (gnfinder adapter reserved)
                result = _unavailable(f"unknown backend {backend!r}")
        except Exception as exc:  # pragma: no cover - defensive belt
            result = _unavailable(f"{type(exc).__name__}")
        if query:
            cache[query] = dict(result)
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
        elif mt == "NONE":
            issues.append({
                "severity": "info",
                "msg_key": "names.unmatched",
                "name": name,
            })
    return issues
