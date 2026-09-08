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
import urllib.parse
import urllib.request
from typing import Any, Callable, Optional

__all__ = [
    "clean_name_for_lookup",
    "verify_name_gbif",
    "verify_names",
    "name_issues",
]

# GBIF species-match: free, no key, CORS-enabled, fuzzy matching with
# confidence scores. verbose=true returns alternatives for review.
_GBIF_MATCH_URL = (
    "https://api.gbif.org/v1/species/match?verbose=true&name="
)

_AUTHOR_PAREN = re.compile(r"\([^)]*\)")


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
    """
    q = clean_name_for_lookup(name)
    if not q:
        return {"status": "unmatched", "match_type": "NONE",
                "confidence": 0.0, "canonical": "", "accepted": "",
                "alternatives": []}
    url = _GBIF_MATCH_URL + urllib.parse.quote(q)
    try:
        if fetch is None:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                body = resp.read().decode("utf-8", "replace")
        else:
            body = fetch(url)
        payload = json.loads(body)
    except Exception as exc:  # network / DNS / rate limit -> fail open
        return {"status": "unavailable", "error": f"{type(exc).__name__}",
                "match_type": "NONE", "confidence": 0.0, "canonical": "",
                "accepted": "", "alternatives": []}

    match_type = str(payload.get("matchType") or "NONE")
    alternates = []
    for alt in (payload.get("alternatives") or [])[:3]:
        alternates.append({
            "canonical": str(alt.get("canonicalName") or ""),
            "match_type": str(alt.get("matchType") or ""),
            "confidence": float(alt.get("confidence") or 0),
            "status": str(alt.get("status") or ""),
        })
    return {
        "status": "ok",
        "match_type": match_type,
        "confidence": float(payload.get("confidence") or 0),
        "canonical": str(payload.get("canonicalName") or ""),
        "accepted": str(payload.get("scientificName") or ""),
        "taxonomic_status": str(payload.get("status") or ""),
        "alternatives": alternates,
    }


def verify_names(
    names: list[str],
    backend: str = "gbif",
    timeout: float = 15.0,
    fetch: Optional[Callable[[str], str]] = None,
) -> dict[str, dict[str, Any]]:
    """Verify a batch of extracted species strings.

    Returns ``{original_name: verification_dict}``. Deduplicated and
    lookup-cleaned internally; results are keyed by the ORIGINAL string so
    callers can annotate rows without re-matching. Fail-open per name.
    """
    out: dict[str, dict[str, Any]] = {}
    cache: dict[str, dict[str, Any]] = {}
    for original in names:
        original = str(original or "").strip()
        if not original or original in out:
            continue
        query = clean_name_for_lookup(original)
        if not query:
            out[original] = {"status": "unmatched", "match_type": "NONE",
                             "confidence": 0.0, "canonical": "",
                             "accepted": "", "alternatives": []}
            continue
        if query in cache:
            out[original] = dict(cache[query])
            continue
        if backend == "gbif":
            result = verify_name_gbif(query, timeout=timeout, fetch=fetch)
        else:  # unknown backend -> fail open (gnfinder adapter reserved)
            result = {"status": "unavailable",
                      "error": f"unknown backend {backend!r}",
                      "match_type": "NONE", "confidence": 0.0,
                      "canonical": "", "accepted": "", "alternatives": []}
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
        status = v.get("status")
        if status == "unavailable":
            continue
        mt = str(v.get("match_type") or "NONE")
        conf = float(v.get("confidence") or 0)
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
