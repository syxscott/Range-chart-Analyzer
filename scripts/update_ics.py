"""BORROW-2026-09-20: refresh the bundled ICS chronostratigraphy table from live upstreams.

Two independently sufficient channels are tried in order, and EITHER
success is enough:

  (a) Macrostrat defs API  - the clean pull pattern demonstrated by
      willgearty/deeptime: one HTTP GET, a flat interval list, CC-BY-4.0.
      The documented endpoint is ``/api/v2/defs/ages``; upstream currently
      answers that path with 404 ("Cannot GET /defs/ages"), so the same
      records are also fetched from ``/api/v2/defs/intervals?timescale_id=1``
      ("international ages", the ICS ladder Macrostrat mirrors). Both URLs
      are tried, first hit wins - see ``MACROSTRAT_AGE_URLS``.
  (b) ICS chart RDF        - i-c-stratigraphy/chart publishes the official
      chart as one Turtle document (``chart.ttl``, CC-BY-4.0) whose
      ``rank:Age`` concepts carry ``time:hasBeginning``/``hasEnd``,
      ``gts:ratifiedGSSP``/``gts:ratifiedGSSA`` and the ``skos:broader``
      epoch/period chain. Parsed here with a small stdlib scanner (no rdflib).

Output schema: STRICT SUPERSET of ``rca_core/resources/ics_2024.json``.
That file is consumed as ``dict[str, dict]`` and iterated wholesale by
``rca_core/standards/ics.py`` (``ICS_2024.items()``, one regex per key,
``_PERIOD_BOUNDS`` built from every row), so:
  * the document stays a FLAT name -> row mapping - no top-level metadata
    key, no nested "data" envelope; a stray non-row key would be handed to
    ``info.get("top_ma")`` and turned into a stage regex;
  * every row keeps the eight baseline fields (``rank``, ``abbrev``,
    ``top_ma``, ``base_ma``, ``period``, ``period_top_ma``,
    ``period_base_ma``, ``era``) and only ADDS provenance
    (``source``, ``license``, ``retrieved_at``, ``ics_version``) plus
    optional detail fields;
  * row keys are a superset of the baseline keys unless ``--no-carry-forward``
    is passed, so no consumer lookup silently disappears.

Nothing is overwritten by default: the result lands in
``rca_core/resources/ics_current.json`` and a diff summary against the
canonical ``ics_2024.json`` is printed. Promoting it takes an explicit
``--write-canonical``, which backs the old file up to ``ics_2024.json.bak``
first and refuses (without ``--force``) when the refresh would break the
data invariants guarded by ``tests/test_ics_invariants.py``.

Usage:
    python scripts/update_ics.py
    python scripts/update_ics.py --channel ics --export-csv outputs/ics_alignment.csv
    python scripts/update_ics.py --offline-fixture tests/fixtures/ics_macrostrat_mini.json
    python scripts/update_ics.py --write-canonical          # explicit promotion
    python scripts/update_ics.py --help

Stdlib only (urllib.request). No API key, no third-party dependency.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parent.parent
RESOURCES = ROOT / "rca_core" / "resources"
CANONICAL = RESOURCES / "ics_2024.json"
DEFAULT_OUT = RESOURCES / "ics_current.json"

USER_AGENT = "RangeChartAnalyzer-ICSUpdater/1.0 (+research tool; not a browser)"
TIMEOUT_SEC = 20.0

# (a) Macrostrat defs. Both spellings are tried: the documented ``ages``
# endpoint first, then the ``intervals`` form that serves the same data today.
MACROSTRAT_BASE = "https://macrostrat.org/api/v2"
MACROSTRAT_AGE_URLS: Tuple[Tuple[str, str], ...] = (
    ("defs/ages", MACROSTRAT_BASE + "/defs/ages?format=json&all=1"),
    ("defs/intervals?timescale_id=1",
     MACROSTRAT_BASE + "/defs/intervals?format=json&all=1&timescale_id=1"),
)
# Hierarchy companions: international periods / epochs. Optional - a missing
# one only costs detail, the baseline still anchors the period bounds.
MACROSTRAT_PERIODS_URL = MACROSTRAT_BASE + "/defs/intervals?format=json&all=1&timescale_id=3"
MACROSTRAT_EPOCHS_URL = MACROSTRAT_BASE + "/defs/intervals?format=json&all=1&timescale_id=2"

# (b) ICS chart RDF (Turtle) - the authoritative publication behind
# https://stratigraphy.org/ICSchart/, licensed CC-BY-4.0.
ICS_TTL_URLS: Tuple[str, ...] = (
    "https://raw.githubusercontent.com/i-c-stratigraphy/chart/main/chart.ttl",
    "https://data.stratigraphy.org/def/chart",
)

DEFAULT_LICENSE = {
    "macrostrat": "CC-BY-4.0 (Macrostrat defs API)",
    "ics": "CC-BY-4.0 (ICS International Chronostratigraphic Chart)",
}

# The bundled ladder is Phanerozoic and so is every consumer of it:
# tests/test_ics_invariants.py asserts ``era in {Paleozoic, Mesozoic,
# Cenozoic}`` for every row, and rca_core only ever resolves Phanerozoic
# labels. Older intervals are therefore dropped unless --all-intervals asks
# for them.
PHANEROZOIC_BASE_MA = 538.8

# Rank spellings across the two sources -> the schema's ``rank`` values.
# Anything coarser than a series is not emitted as a row: ``ics_stage_from_age``
# returns the first formal name whose span contains the age, so a Period row
# would make it answer "Jurassic" where the chart says "Callovian".
_RANK_MAP = {"age": "Stage", "stage": "Stage", "subage": "Stage",
             "epoch": "Series", "series": "Series", "superepoch": "Series",
             "period": "Period", "era": "Era", "eon": "Eon"}
_COARSE_RANKS = ("Period", "Era", "Eon")

_INFORMAL_STAGE_RE = re.compile(r"^\s*(?:unnumbered|unnamed|stage)\s+[\dxvi]+\s*$",
                                re.IGNORECASE)

_EPS = 1e-6
# Duplicate aliases in ICS land with IDENTICAL bounds (Wuliuan / "Stage 5");
# they are tolerated as warnings, not errors, exactly like the invariants test
# tolerates them.
_ALIAS_TOLERANCE = 1e-3

FetchFn = Callable[[str], str]


class IcsUpdateError(RuntimeError):
    """Actionable failure: the message tells the operator what to do next."""


# ---------------------------------------------------------------------------
# network layer
# ---------------------------------------------------------------------------

def fetch_url(url: str, *, timeout: float = TIMEOUT_SEC) -> str:
    """GET *url* and return the decoded body.

    HTTPS-only, hard timeout, explicit User-Agent. A *private* opener is
    built instead of using ``urllib.request.urlopen``: ``rca_core/llm.py``
    installs a process-global SSRF-pinning opener at import time, so a
    maintenance script must not depend on whether that import happened.
    """
    if not url.lower().startswith("https://"):
        raise IcsUpdateError(f"refusing non-HTTPS URL {url!r}")
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT,
                      "Accept": "application/json, text/turtle, */*"})
    opener = urllib.request.build_opener()
    try:
        with opener.open(request, timeout=timeout) as resp:
            headers = getattr(resp, "headers", None)
            charset = (headers.get_content_charset() if headers else None) or "utf-8"
            return resp.read().decode(charset, "replace")
    except urllib.error.HTTPError as exc:
        raise IcsUpdateError(
            f"HTTP {exc.code} from {url}: {exc.reason}. If the endpoint moved, "
            "adjust MACROSTRAT_AGE_URLS / ICS_TTL_URLS in this file; a 403 "
            "usually means the User-Agent or a rate limit."
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise IcsUpdateError(
            f"cannot reach {url}: {exc}. Offline or blocked egress? Re-run with "
            "--offline-fixture PATH (e.g. "
            "tests/fixtures/ics_macrostrat_mini.json) to exercise the whole "
            "pipeline without network, or export HTTPS_PROXY if this machine "
            "needs a proxy."
        ) from exc


def _load_json_rows(body: str, *, url: str) -> Tuple[list, Dict[str, Any]]:
    """Pull the record list out of a Macrostrat payload.

    Accepts every shape the API has shipped: ``{"success": {"data": [...]}}``,
    ``{"data": [...]}`` and a bare list.
    """
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise IcsUpdateError(
            f"{url} did not return JSON ({exc}); that looks like an HTML error "
            "page - check the endpoint or run with --channel ics."
        ) from exc
    meta: Dict[str, Any] = {}
    if isinstance(payload, dict):
        success = payload.get("success")
        if isinstance(success, dict):
            meta = {k: success.get(k) for k in ("license", "v", "total")}
            rows = success.get("data")
            if isinstance(rows, list):
                return [r for r in rows if isinstance(r, dict)], meta
        rows = payload.get("data")
        if isinstance(rows, list):
            return [r for r in rows if isinstance(r, dict)], meta
        return [], meta
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)], meta
    raise IcsUpdateError(
        f"{url} returned {type(payload).__name__}, expected a JSON object or list")


def _macrostrat_rows(body: str, *, url: str) -> list:
    """Normalise Macrostrat interval records into raw rows."""
    rows, meta = _load_json_rows(body, url=url)
    out: list = []
    for rec in rows:
        name = str(rec.get("name") or "").strip()
        if not name:
            continue
        out.append({
            "name": name,
            "t_age": _as_float(rec.get("t_age")),
            "b_age": _as_float(rec.get("b_age")),
            "rank": _RANK_MAP.get(str(rec.get("int_type") or "").strip().lower(), "Stage"),
            "source_id": rec.get("int_id"),
            "abbrev": rec.get("abbrev"),
            "color": rec.get("color"),
            "license": meta.get("license") or "",
        })
    return out


def acquire_macrostrat(fetch: FetchFn, *, timeout: float = TIMEOUT_SEC) -> Dict[str, Any]:
    """Channel (a): the age ladder, plus the period/epoch ladders if reachable."""
    last_error: Optional[Exception] = None
    body = ""
    label = ""
    for label, url in MACROSTRAT_AGE_URLS:
        try:
            body = fetch(url)
            break
        except Exception as exc:  # noqa: BLE001 - try the sibling endpoint first
            last_error = exc
            body = ""
    if not body:
        raise IcsUpdateError(
            "Macrostrat channel failed: "
            + (str(last_error) if last_error else "no response from any endpoint")
        )
    ages = _macrostrat_rows(body, url=dict(MACROSTRAT_AGE_URLS)[label])
    if not ages:
        raise IcsUpdateError(f"Macrostrat {label} returned no usable age records")
    return {
        "channel": "macrostrat",
        "rows": ages,
        "periods": _soft_rows(fetch, MACROSTRAT_PERIODS_URL),
        "epochs": _soft_rows(fetch, MACROSTRAT_EPOCHS_URL),
        "source": f"macrostrat:{label}",
        "license": _license_of(ages) or DEFAULT_LICENSE["macrostrat"],
        "ics_version": "",
    }


def _soft_rows(fetch: FetchFn, url: str) -> list:
    """Fetch a companion ladder; a missing one must not sink the refresh."""
    try:
        return _macrostrat_rows(fetch(url), url=url)
    except Exception:  # noqa: BLE001 - hierarchy is best effort
        return []


def _license_of(rows: Sequence[Dict[str, Any]]) -> str:
    for row in rows:
        if row.get("license"):
            return str(row["license"])
    return ""


# ---------------------------------------------------------------------------
# channel (b): ICS chart Turtle
# ---------------------------------------------------------------------------

# Long-Turtle serialisation of chart.ttl: every concept block starts at
# column 0 and ends with a lone ".". The scanner reads only the structural
# predicates the timescale needs and ignores the 30-language label lists.
_BLOCK_SPLIT = re.compile(r"(?m)^(?=\S)")
_SUBJECT_RE = re.compile(r"^(\S+)")
_RANK_RE = re.compile(r"gts:rank\s+rank:(\w+)")
_GSSP_RE = re.compile(r"gts:ratifiedGSSP\s+(true|false)")
_GSSA_RE = re.compile(r"gts:ratifiedGSSA\s+(true|false)")
_BROADER_RE = re.compile(r"skos:broader\s+(<[^>]+>|[\w:%.\-]+)")
_ORDER_RE = re.compile(r"sh:order\s+(\d+)")
_COLOR_RE = re.compile(r'schema:color\s+"(#[0-9A-Fa-f]{6})"')
_NOTATION_RE = re.compile(r'skos:notation\s+"([^"]+)"')
_MODIFIED_RE = re.compile(r'dcterms:modified\s+"([^"]+)"')
# The object list of a predicate runs to the next predicate line, the end of
# the block, or the end of the chunk the block splitter produced (the closing
# "." lands in the *next* chunk, so it cannot be relied on as the terminator).
_PREF_LABEL_RE = re.compile(r"skos:prefLabel\s+(.*?)(?=\n    \w|\n\.|$)", re.S)
_EN_LABEL_RE = re.compile(r'"([^"]*)"\s*@en\b')
_BEGIN_RE = re.compile(r"time:hasBeginning\s*\[(.*?)\]", re.S)
_END_RE = re.compile(r"time:hasEnd\s*\[(.*?)\]", re.S)
_MYA_RE = re.compile(r"gtsd:inMYA\s+([0-9.]+)")
_NOTE_RE = re.compile(r'skos:note\s+"([^"]*)"')
_TTL_RANK_MAP = {"Age": "Stage", "Epoch": "Series", "Series": "Series",
                 "Period": "Period", "Era": "Era", "Eon": "Eon"}
_VERSION_OBJ_RE = re.compile(r"owl:versionIRI\s+(<[^>]+>|[^\s;,]+)")
_YYMM_RE = re.compile(r"^\d{4}-\d{1,2}$")


def _version_of(text: str) -> str:
    """The chart release stamp, e.g. "2026-06" from ``owl:versionIRI gtsd:2026-06``.

    Works for both the prefixed form and a full ``<.../2026-06>`` IRI, and
    returns "" rather than a plausible-looking fragment of some other path.
    """
    match = _VERSION_OBJ_RE.search(text)
    if not match:
        return ""
    tail = re.split(r"[/:#]", match.group(1).strip("<>"))[-1]
    return tail if _YYMM_RE.match(tail) else ""


def parse_ics_turtle(text: str) -> Dict[str, Dict[str, Any]]:
    """Extract the chart concepts from an ICS chart.ttl document.

    Deliberately not a full Turtle parser: it reads the predicate set the
    timescale needs and tolerates annotation nodes in between, e.g.
    ``time:hasBeginning [ skos:note "uncertain" ; gtsd:inMYA 237 ; ]``.
    Returns a subject -> record map (``gtsd:Fortunian`` style keys).
    """
    found: Dict[str, Dict[str, Any]] = {}
    for block in _BLOCK_SPLIT.split(text):
        if not block or block[0].isspace():
            continue
        subject = _SUBJECT_RE.match(block)
        if not subject:
            continue
        key = subject.group(1)
        if not key.startswith("gtsd:"):
            continue
        rank = _RANK_RE.search(block)
        if not rank:
            continue
        beginning = _BEGIN_RE.search(block)
        end = _END_RE.search(block)
        note = _NOTE_RE.search(block)
        order = _ORDER_RE.search(block)
        color = _COLOR_RE.search(block)
        notation = _NOTATION_RE.search(block)
        found[key] = {
            "name": _en_label(block) or _local_name(key),
            # ICS: hasBeginning is the OLDER bound (base), hasEnd the YOUNGER.
            "t_age": _first_mya(end.group(1) if end else ""),
            "b_age": _first_mya(beginning.group(1) if beginning else ""),
            "rank": _TTL_RANK_MAP.get(rank.group(1), "Stage"),
            "broader": _bare(_BROADER_RE.search(block)),
            "gssp": _flag(_GSSP_RE.search(block)),
            "gssa": _flag(_GSSA_RE.search(block)),
            "note": note.group(1) if note else "",
            "order": _int_or_none(order.group(1)) if order else None,
            "color": color.group(1) if color else "",
            "abbrev": "",
            "ccgm": notation.group(1) if notation else "",
            "source_id": key,
        }
    return found


def _en_label(block: str) -> str:
    labels = _PREF_LABEL_RE.search(block)
    if not labels:
        return ""
    match = _EN_LABEL_RE.search(labels.group(1))
    return match.group(1).strip() if match else ""


def _local_name(iri: str) -> str:
    tail = re.split(r"[:/]", iri)[-1]
    return tail.replace("_", " ").replace("%20", " ").strip()


def _bare(match: Optional[Any]) -> str:
    return match.group(1) if match else ""


def _flag(match: Optional[Any]) -> Optional[bool]:
    return None if match is None else match.group(1) == "true"


def _first_mya(chunk: str) -> Optional[float]:
    match = _MYA_RE.search(chunk)
    return _as_float(match.group(1)) if match else None


def _int_or_none(value: Any) -> Optional[int]:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def acquire_ics(fetch: FetchFn, *, timeout: float = TIMEOUT_SEC) -> Dict[str, Any]:
    """Channel (b): the ICS chart RDF, including the version stamp it carries."""
    last_error: Optional[Exception] = None
    text = ""
    used = ""
    for url in ICS_TTL_URLS:
        try:
            text = fetch(url)
            used = url
            break
        except Exception as exc:  # noqa: BLE001 - try the mirror, then report
            last_error = exc
            text = ""
    if not text:
        raise IcsUpdateError("ICS chart channel failed: "
                             + (str(last_error) if last_error else "no response"))
    if "gts:rank" not in text:
        raise IcsUpdateError(
            f"{used} does not look like the ICS chart Turtle (no gts:rank "
            "predicate found) - the publication format may have changed; "
            "check github.com/i-c-stratigraphy/chart."
        )
    records = parse_ics_turtle(text)
    stages = [r for r in records.values()
              if r["rank"] in ("Stage", "Series")
              and r["t_age"] is not None and r["b_age"] is not None]
    if not stages:
        raise IcsUpdateError(f"{used} parsed but contained no dated rank:Age concepts")
    version = _version_of(text)
    modified = _MODIFIED_RE.search(text)
    return {
        "channel": "ics",
        "rows": sorted(stages, key=lambda r: (r.get("order") is None, r.get("order"))),
        "periods": [r for r in records.values() if r["rank"] == "Period"],
        "epochs": [r for r in records.values() if r["rank"] == "Series"],
        "ancestors": records,
        "source": f"ics-chart:{version or 'unknown'}",
        "license": DEFAULT_LICENSE["ics"],
        "ics_version": version,
        "modified": modified.group(1) if modified else "",
    }


# ---------------------------------------------------------------------------
# mapping onto the bundled schema
# ---------------------------------------------------------------------------

def _as_float(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# Period -> era. Static for the Phanerozoic (ICS has never moved these), and
# re-seeded from the canonical table on load so the two can never disagree.
_ERA_BY_PERIOD = {
    "Cambrian": "Paleozoic", "Ordovician": "Paleozoic", "Silurian": "Paleozoic",
    "Devonian": "Paleozoic", "Carboniferous": "Paleozoic", "Permian": "Paleozoic",
    "Triassic": "Mesozoic", "Jurassic": "Mesozoic", "Cretaceous": "Mesozoic",
    "Paleogene": "Cenozoic", "Neogene": "Cenozoic", "Quaternary": "Cenozoic",
}


def seed_era_map(baseline: Dict[str, Dict[str, Any]]) -> None:
    for info in baseline.values():
        if not isinstance(info, dict):
            continue
        period, era = info.get("period"), info.get("era")
        if period and era:
            _ERA_BY_PERIOD.setdefault(str(period), str(era))


def _era_for(period: str) -> str:
    return _ERA_BY_PERIOD.get(period, "")


def baseline_period_bounds(baseline: Dict[str, Dict[str, Any]]) -> Dict[str, Tuple[float, float]]:
    """Period name -> (top_ma, base_ma) as the bundled table knows it."""
    bounds: Dict[str, Tuple[float, float]] = {}
    for info in baseline.values():
        period = info.get("period")
        top = _as_float(info.get("period_top_ma"))
        base = _as_float(info.get("period_base_ma"))
        if period and top is not None and base is not None:
            bounds.setdefault(str(period), (top, base))
    return bounds


def assign_hierarchy(
    result: Dict[str, Any],
    *,
    baseline: Optional[Dict[str, Dict[str, Any]]] = None,
) -> None:
    """Attach period / series / era to every raw row, in place.

    Resolution order, strongest evidence first:
      1. the ICS channel's ``skos:broader`` chain (authoritative hierarchy);
      2. containment inside the source's own period / epoch ladders;
      3. the bundled table's period for a row of the same name;
      4. containment inside the bundled table's period bounds - this is what
         carries a brand-new stage (e.g. the Silurian ages the 2024 table
         only had at series level) to the right period.
    """
    baseline = baseline or {}
    rows = result["rows"]
    ancestors = result.get("ancestors") or {}
    periods = [r for r in result.get("periods") or [] if r.get("rank") == "Period"]
    epochs = [r for r in result.get("epochs") or [] if r.get("rank") == "Series"]
    period_bounds = _level_bounds(periods)
    fallback_bounds = baseline_period_bounds(baseline)
    for row in rows:
        series_name, period_name = "", ""
        parent = ancestors.get(row.get("broader") or "")
        if parent is not None:
            if parent.get("rank") == "Series":
                series_name = parent["name"]
                grand = ancestors.get(parent.get("broader") or "")
                if grand is not None:
                    period_name = grand["name"]
            else:
                period_name = parent["name"]
        if not period_name:
            enclosing = _enclosing(periods, row)
            period_name = enclosing["name"] if enclosing else ""
        if not period_name:
            known = baseline.get(row["name"])
            if isinstance(known, dict):
                period_name = str(known.get("period") or "")
        if not period_name:
            bounds = _enclosing_bounds(fallback_bounds, row)
            period_name = bounds or ""
        if not series_name:
            enclosing_epoch = _enclosing(epochs, row)
            if enclosing_epoch is not None and enclosing_epoch["name"] != period_name:
                series_name = enclosing_epoch["name"]
        bounds = period_bounds.get(period_name) or fallback_bounds.get(period_name)
        row["series"] = series_name
        row["period"] = period_name
        row["period_top_ma"], row["period_base_ma"] = bounds if bounds else (None, None)
        row["era"] = _era_for(period_name)


def _level_bounds(levels: Sequence[Dict[str, Any]]) -> Dict[str, Tuple[float, float]]:
    bounds: Dict[str, Tuple[float, float]] = {}
    for level in levels:
        if not level.get("name") or level["name"] in bounds:
            continue
        if level.get("t_age") is None or level.get("b_age") is None:
            continue
        bounds[level["name"]] = (level["t_age"], level["b_age"])
    return bounds


def _enclosing(levels: Sequence[Dict[str, Any]], row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The narrowest level whose span contains *row* (midpoint test)."""
    mid = _midpoint(row)
    if mid is None:
        return None
    best: Optional[Dict[str, Any]] = None
    for level in levels:
        if level.get("name") == row.get("name"):
            continue  # a ladder never names itself as its own period
        if level.get("t_age") is None or level.get("b_age") is None:
            continue
        if level["t_age"] - _EPS <= mid <= level["b_age"] + _EPS:
            if best is None or (level["b_age"] - level["t_age"]) < (best["b_age"] - best["t_age"]):
                best = level
    return best


def _enclosing_bounds(bounds: Dict[str, Tuple[float, float]], row: Dict[str, Any]) -> str:
    mid = _midpoint(row)
    if mid is None:
        return ""
    hits = [name for name, (top, base) in bounds.items()
            if top - _EPS <= mid <= base + _EPS]
    if not hits:
        return ""
    # Widest containing period wins (they do not overlap, so this is a tie-break).
    return max(hits, key=lambda n: bounds[n][1] - bounds[n][0])


def _midpoint(row: Dict[str, Any]) -> Optional[float]:
    top, base = row.get("t_age"), row.get("b_age")
    if top is None or base is None:
        return None
    return (top + base) / 2.0


def _abbrev_for(name: str, raw: Dict[str, Any], baseline: Dict[str, Dict[str, Any]]) -> str:
    known = baseline.get(name)
    if isinstance(known, dict) and known.get("abbrev"):
        return str(known["abbrev"])
    informal = re.match(r"^\s*stage\s+([\dxvi]+)\s*$", name, re.IGNORECASE)
    if informal:
        return informal.group(1)  # baseline convention: "Stage 2" -> "2"
    raw_abbrev = raw.get("abbrev")
    if isinstance(raw_abbrev, str) and raw_abbrev.strip():
        return raw_abbrev.strip()
    if len(name) >= 2:
        return name[0].upper() + name[1].lower()
    return name


def build_table(
    result: Dict[str, Any],
    *,
    baseline: Dict[str, Dict[str, Any]],
    retrieved_at: str,
    ics_version: str,
    carry_forward: bool = True,
    keep_all_intervals: bool = False,
) -> Tuple[Dict[str, Dict[str, Any]], list]:
    """Map raw rows onto the bundled schema. Returns (table, notes)."""
    assign_hierarchy(result, baseline=baseline)
    notes: list = []
    source = str(result.get("source") or result.get("channel") or "unknown")
    license_ = str(result.get("license")
                   or DEFAULT_LICENSE.get(str(result.get("channel")), "unspecified"))
    version = str(ics_version or result.get("ics_version") or "")
    table: Dict[str, Dict[str, Any]] = {}
    for raw in result["rows"]:
        name = str(raw.get("name") or "").strip()
        if not name:
            continue
        base = _as_float(raw.get("b_age"))
        top = _as_float(raw.get("t_age"))
        if base is None or top is None:
            notes.append(f"{name}: dropped, the source gives no numeric bounds")
            continue
        if not keep_all_intervals and base > PHANEROZOIC_BASE_MA + _EPS:
            continue  # Phanerozoic-only ladder, see PHANEROZOIC_BASE_MA
        era = str(raw.get("era") or "")
        if not era:
            notes.append(f"{name}: dropped, period {raw.get('period')!r} has no known era")
            continue
        rank = str(raw.get("rank") or "Stage")
        if rank in _COARSE_RANKS:
            notes.append(f"{name}: skipped ({rank}-level row would shadow stage lookups)")
            continue
        entry: Dict[str, Any] = {
            "rank": rank,
            "abbrev": _abbrev_for(name, raw, baseline),
            "top_ma": _round_age(top),
            "base_ma": _round_age(base),
            "period": str(raw.get("period") or ""),
            "period_top_ma": _round_age(raw.get("period_top_ma")),
            "period_base_ma": _round_age(raw.get("period_base_ma")),
            "era": era,
            # BORROW-2026-09-20 provenance: additive only, so the
            # ``dict[str, dict]`` contract of the consumers is untouched.
            "source": source,
            "license": license_,
            "retrieved_at": retrieved_at,
            "ics_version": version,
        }
        if raw.get("series"):
            entry["series"] = str(raw["series"])
        if raw.get("gssp") is not None:
            entry["gssp_ratified"] = bool(raw["gssp"])
        if raw.get("gssa") is not None:
            entry["gssa_ratified"] = bool(raw["gssa"])
        if raw.get("note"):
            entry["age_note"] = str(raw["note"])
        if raw.get("color"):
            entry["color"] = str(raw["color"])
        if raw.get("source_id") is not None:
            entry["source_id"] = raw["source_id"]
        if name in table:
            notes.append(f"{name}: duplicate row in the source, keeping the first")
            continue
        table[name] = entry
    prune_covered_series(table, notes)
    if carry_forward:
        table.update(carried_forward_rows(
            baseline, table, notes=notes, retrieved_at=retrieved_at,
            ics_version=version, source=source, license_=license_))
    return dict(sorted(table.items(), key=lambda kv: kv[0].casefold())), notes


def prune_covered_series(table: Dict[str, Dict[str, Any]], notes: list) -> None:
    """Drop series rows whose interval is fully tiled by formal stages.

    A row at series granularity is only load-bearing when part of its span
    has no stage name - that is why the bundled table carries "Holocene" and
    the Silurian series but no "Lopingian". Keeping every upstream epoch as
    a row would break stage lookups: ``ics_stage_from_age`` walks all rows,
    so "Llandovery" would answer where the chart says "Aeronian".
    """
    stages = [info for info in table.values() if info.get("rank") == "Stage"]
    for name, info in list(table.items()):
        if info.get("rank") != "Series":
            continue
        top, base = _f(info.get("top_ma")), _f(info.get("base_ma"))
        spans = sorted(
            (max(top, _f(s.get("top_ma"))), min(base, _f(s.get("base_ma"))))
            for s in stages
            if s.get("period") == info.get("period")
            and _f(s.get("top_ma")) < base and _f(s.get("base_ma")) > top)
        covered, cursor = 0.0, None
        for lo, hi in spans:
            if cursor is None or lo > cursor:
                cursor = hi
                covered += hi - lo
            elif hi > cursor:
                covered += hi - cursor
                cursor = hi
        if covered >= (base - top) - _ALIAS_TOLERANCE:
            del table[name]
            notes.append(f"{name}: series row dropped - every part of it has a "
                         "formal stage name (a series row is only kept where it "
                         "is the only name)")


def _round_age(value: Any) -> float:
    return 0.0 if value is None else round(float(value), 6)


def carried_forward_rows(
    baseline: Dict[str, Dict[str, Any]],
    table: Dict[str, Dict[str, Any]],
    *,
    notes: list,
    retrieved_at: str,
    ics_version: str,
    source: str,
    license_: str,
) -> Dict[str, Dict[str, Any]]:
    """Keep baseline keys the upstream no longer names.

    Dropping them would silently break every stored range chart that cites
    the old name, so the row is carried forward and marked. It is NOT
    presented as fresh data: ``source`` says where it came from and
    ``carried_forward`` is set, which is also what the diff summary reports.
    """
    carried: Dict[str, Dict[str, Any]] = {}
    for name, info in baseline.items():
        if name in table or not isinstance(info, dict):
            continue
        row = dict(info)
        row["source"] = f"{source} (carried forward from ics_2024.json)"
        row["license"] = license_
        row["retrieved_at"] = retrieved_at
        row["ics_version"] = ics_version
        row["carried_forward"] = True
        carried[name] = row
        notes.append(f"{name}: not in the upstream ladder - carried forward "
                     "unchanged (--no-carry-forward to drop)")
    return carried


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------

def validate_table(table: Dict[str, Dict[str, Any]]) -> Tuple[list, list]:
    """GSSA / hierarchy / monotonicity checks. Returns (errors, warnings)."""
    errors: list = []
    warnings: list = []
    required = ("rank", "abbrev", "top_ma", "base_ma", "period",
                "period_top_ma", "period_base_ma", "era",
                "source", "license", "retrieved_at", "ics_version")
    for name, info in table.items():
        if not isinstance(info, dict):
            errors.append(f"{name}: row is {type(info).__name__}, expected an object")
            continue
        missing = [k for k in required if k not in info]
        if missing:
            errors.append(f"{name}: missing fields {missing}")
        if info.get("era") not in {"Paleozoic", "Mesozoic", "Cenozoic"}:
            errors.append(f"{name}: unknown era {info.get('era')!r}")
        if not info.get("period"):
            errors.append(f"{name}: no period assigned")
        top = _as_float(info.get("top_ma"))
        base = _as_float(info.get("base_ma"))
        if top is None or base is None:
            errors.append(f"{name}: non-numeric bounds")
            continue
        if base <= top + _EPS:
            errors.append(
                f"{name}: base_ma {base} is not older than top_ma {top} "
                "(convention: larger Ma = older)")
        # GSSA: an unratified placeholder name must never claim ratification.
        if _INFORMAL_STAGE_RE.match(name) and (info.get("gssp_ratified")
                                               or info.get("gssa_ratified")):
            errors.append(f"{name}: informal placeholder name claims a ratified GSSA/GSSP")
        if info.get("age_note"):
            warnings.append(f"{name}: source flags the boundary as {info['age_note']!r}")

    # Every stage of one period has to agree on where that period starts and
    # ends, and has to sit inside it.  Rows that declare no envelope (0.0) are
    # skipped: a source gap is not a contradiction, and the required-field and
    # ladder checks above already report it.
    bounds: Dict[str, Tuple[float, float]] = {}
    for name, info in table.items():
        period = info.get("period")
        base = _as_float(info.get("base_ma"))
        top = _as_float(info.get("top_ma"))
        p_base = _as_float(info.get("period_base_ma"))
        p_top = _as_float(info.get("period_top_ma"))
        if not period or not p_base or not p_top:
            continue
        pair = (p_base, p_top)
        if period in bounds and bounds[period] != pair:
            errors.append(
                f"{name}: period {period} bounds {pair} disagree with {bounds[period]}")
        bounds.setdefault(period, pair)
        if None in (base, top):
            continue
        if base > p_base + _EPS or top < p_top - _EPS:
            errors.append(
                f"{name}: [{top}, {base}] is not contained in its period "
                f"{period} [{p_top}, {p_base}]")

    # Stratigraphic ladder: walked from young to old, the ages must increase
    # and each stage's top must mate with the base of the stage below it.
    ladders: Dict[str, list] = {}
    for name, info in table.items():
        if info.get("rank") == "Stage" and info.get("period"):
            ladders.setdefault(str(info["period"]), []).append((name, info))
    for period, entries in sorted(ladders.items()):
        entries.sort(key=lambda kv: (_f(kv[1].get("top_ma")), _f(kv[1].get("base_ma"))))
        # The row owning the oldest boundary reached so far: every younger
        # stage's top must mate with exactly that number.
        holder: Optional[Tuple[str, Dict[str, Any]]] = None
        for name, info in entries:
            top, base = _f(info.get("top_ma")), _f(info.get("base_ma"))
            if holder is not None:
                holder_name, older = holder
                holder_base, holder_top = _f(older.get("base_ma")), _f(older.get("top_ma"))
                if abs(base - holder_base) <= _ALIAS_TOLERANCE and abs(top - holder_top) <= _ALIAS_TOLERANCE:
                    # Wuliuan / "Stage 5": one interval, two names. ICS keeps
                    # the informal synonym until it is formally retired, so it
                    # must not read as an overlap.
                    warnings.append(f"{name}: duplicate alias of {holder_name} "
                                    f"({period}) - same span, kept for lookups")
                elif top > holder_base + _EPS:
                    warnings.append(
                        f"{name}: {round(top - holder_base, 4)} Ma gap below "
                        f"{holder_name} in {period}")
                elif top < holder_base - _EPS:
                    errors.append(
                        f"{name}: overlaps {holder_name} in {period} - its top "
                        f"{top} is older than the base {holder_base} of the "
                        "stage above it (the ladder must not double-count time)")
                elif base < holder_base - _EPS:
                    errors.append(
                        f"{name}: ladder is not monotonic young -> old in "
                        f"{period} (base {base} after {holder_name} base {holder_base})")
            if holder is None or base > _f(holder[1].get("base_ma")):
                holder = (name, info)
    return errors, warnings


def _f(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# diff
# ---------------------------------------------------------------------------

def diff_tables(old: Dict[str, Dict[str, Any]], new: Dict[str, Dict[str, Any]],
                *, rename_tolerance: float = _ALIAS_TOLERANCE) -> Dict[str, Any]:
    """Summarise what the refresh changes: added / renamed / moved ages."""
    added = [k for k in new if k not in old]
    removed = [k for k in old if k not in new]
    renames: list = []
    matched_added: set = set()
    for gone in removed:
        old_row = old[gone]
        if not isinstance(old_row, dict):
            continue
        for newcomer in added:
            if newcomer in matched_added:
                continue
            new_row = new[newcomer]
            if (new_row.get("period") == old_row.get("period")
                    and abs(_f(new_row.get("base_ma")) - _f(old_row.get("base_ma"))) <= rename_tolerance
                    and abs(_f(new_row.get("top_ma")) - _f(old_row.get("top_ma"))) <= rename_tolerance):
                renames.append((gone, newcomer, _f(old_row.get("base_ma")), _f(old_row.get("top_ma"))))
                matched_added.add(newcomer)
                break
    changed: list = []
    for name in sorted(set(old) & set(new)):
        old_row, new_row = old[name], new[name]
        if not (isinstance(old_row, dict) and isinstance(new_row, dict)):
            continue
        base_delta = round(_f(new_row.get("base_ma")) - _f(old_row.get("base_ma")), 6)
        top_delta = round(_f(new_row.get("top_ma")) - _f(old_row.get("top_ma")), 6)
        if abs(base_delta) > _EPS or abs(top_delta) > _EPS:
            changed.append({
                "name": name,
                "period": new_row.get("period") or old_row.get("period"),
                "old_base": _f(old_row.get("base_ma")), "new_base": _f(new_row.get("base_ma")),
                "old_top": _f(old_row.get("top_ma")), "new_top": _f(new_row.get("top_ma")),
                "base_delta": base_delta, "top_delta": top_delta,
            })
    renamed_from = {gone for gone, _, _, _ in renames}
    return {
        "added": sorted(k for k in added if k not in matched_added),
        "removed": sorted(k for k in removed if k not in renamed_from),
        "renamed": renames,
        "age_changed": changed,
        "carried_forward": sorted(k for k, v in new.items()
                                  if isinstance(v, dict) and v.get("carried_forward")),
        "unchanged": len(set(old) & set(new)) - len(changed),
    }


def render_diff(diff: Dict[str, Any], *, old_name: str, new_name: str) -> list:
    lines = [f"diff {old_name} -> {new_name}"]
    lines.append(f"  unchanged intervals ..... {diff['unchanged']}")
    if diff["renamed"]:
        lines.append(f"  renamed ({len(diff['renamed'])}):")
        for gone, newcomer, base, top in diff["renamed"]:
            lines.append(f"    - {gone} -> {newcomer} ({base} - {top} Ma, same span)")
    if diff["added"]:
        lines.append(f"  added ({len(diff['added'])}):")
        lines.extend(f"    + {name}" for name in diff["added"])
    if diff["removed"]:
        lines.append(f"  removed, no replacement found ({len(diff['removed'])}):")
        lines.extend(f"    - {name}" for name in diff["removed"])
    if diff["age_changed"]:
        lines.append(f"  age moved ({len(diff['age_changed'])}):")
        for row in diff["age_changed"]:
            lines.append(
                f"    ~ {row['name']} [{row['period']}] "
                f"base {row['old_base']} -> {row['new_base']} ({row['base_delta']:+g} Ma), "
                f"top {row['old_top']} -> {row['new_top']} ({row['top_delta']:+g} Ma)")
    if not any((diff["renamed"], diff["added"], diff["removed"], diff["age_changed"])):
        lines.append("  the refreshed table matches the bundled one exactly")
    return lines


# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------

def load_table(path: Path) -> Dict[str, Dict[str, Any]]:
    """Read a bundled table. A missing file is not an error - just no baseline."""
    path = Path(path)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise IcsUpdateError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise IcsUpdateError(f"{path} must contain an object of name -> row")
    return {str(k): v for k, v in data.items() if isinstance(v, dict)}


def write_json(path: Path, table: Dict[str, Dict[str, Any]]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(table, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")


def read_baseline_version(*ics_modules: Path) -> str:
    """ICS_VERSION from ``rca_core/standards/ics.py`` without importing it.

    ``import rca_core`` runs the package ``__init__`` (Pillow and the rest of
    the core) and ``rca_core.llm`` installs a global URL opener; a maintenance
    script must not need either. The literal is read from the source text of
    the first candidate that exists - normally the copy next to the canonical
    table, falling back to the one shipped in this repository.
    """
    for ics_module in ics_modules:
        try:
            text = Path(ics_module).read_text(encoding="utf-8")
        except OSError:
            continue
        match = re.search(r'^ICS_VERSION\s*=\s*"([^"]+)"', text, re.M)
        if match:
            return match.group(1)
    return ""


ALIGNMENT_FIELDS = ("rank", "abbrev", "series", "top_ma", "base_ma", "era",
                    "period", "period_top_ma", "period_base_ma",
                    "gssp_ratified", "gssa_ratified", "age_note",
                    "source", "license", "retrieved_at", "ics_version")


def export_alignment_csv(
    path: Path,
    table: Dict[str, Dict[str, Any]],
    baseline: Dict[str, Dict[str, Any]],
    diff: Dict[str, Any],
) -> int:
    """Name-alignment table: refreshed name vs the bundled key, per interval."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    renamed_from = {new: old for old, new, _, _ in diff["renamed"]}
    moved = {row["name"]: row for row in diff["age_changed"]}
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["name", "baseline_name", "status", "base_delta_ma",
                         "top_delta_ma", "changed_fields"] + list(ALIGNMENT_FIELDS))
        for name, row in table.items():
            old_key = renamed_from.get(name, name if name in baseline else "")
            status = "new"
            if row.get("carried_forward"):
                status = "carried-forward"
            elif name in baseline:
                status = "age-changed" if name in moved else "same"
            elif old_key:
                status = "renamed"
            move = moved.get(name) or moved.get(old_key)
            writer.writerow(
                [name, old_key, status,
                 move["base_delta"] if move else "",
                 move["top_delta"] if move else "",
                 _changed_fields(baseline.get(old_key or name), row)]
                + [row.get(field, "") for field in ALIGNMENT_FIELDS])
    return len(table)


def _changed_fields(old_row: Optional[Dict[str, Any]], new_row: Dict[str, Any]) -> str:
    """Semicolon list of the baseline fields whose value actually moved."""
    if not isinstance(old_row, dict):
        return ""
    changed = []
    for key in ("top_ma", "base_ma", "period", "era", "rank", "abbrev"):
        if key not in old_row:
            continue
        old_value, new_value = old_row.get(key), new_row.get(key)
        numeric = _as_float(old_value)
        if numeric is not None and _as_float(new_value) is not None:
            if abs(numeric - _as_float(new_value)) > _EPS:
                changed.append(key)
        elif str(old_value) != str(new_value):
            changed.append(key)
    return ";".join(changed)


# ---------------------------------------------------------------------------
# pipeline
# ---------------------------------------------------------------------------

def _cap(items: Sequence[Any], limit: int) -> list:
    """First *limit* items plus a count line, so 90 notes do not scroll away."""
    if len(items) <= limit:
        return list(items)
    return list(items[:limit]) + [f"... and {len(items) - limit} more"]


def _utc_now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_fixture(path: Path) -> Dict[str, Any]:
    """Turn a local fixture file into a channel result (CI / offline runs)."""
    path = Path(path)
    if not path.exists():
        raise IcsUpdateError(
            f"--offline-fixture path {path} does not exist "
            "(try tests/fixtures/ics_macrostrat_mini.json)")
    text = path.read_text(encoding="utf-8")
    looks_turtle = path.suffix.lower() in (".ttl", ".turtle") or "gts:rank" in text[:8000]
    if looks_turtle:
        result = acquire_ics(lambda _url: text)
    elif path.suffix.lower() in (".json", ""):
        result = acquire_macrostrat(lambda _url: text)
    else:
        raise IcsUpdateError(
            f"--offline-fixture {path} has an unsupported extension "
            "(use .json for a Macrostrat payload or .ttl for the ICS chart)")
    result["source"] = f"offline-fixture:{path.name}"
    return result


def acquire(
    channel: str,
    *,
    fixture: Optional[Path] = None,
    fetch: Optional[FetchFn] = None,
    timeout: float = TIMEOUT_SEC,
) -> Dict[str, Any]:
    """Run the channels in order; the first success wins."""
    if fixture is not None:
        return load_fixture(fixture)
    do_fetch: FetchFn = fetch or (lambda url: fetch_url(url, timeout=timeout))
    runners = {"macrostrat": acquire_macrostrat, "ics": acquire_ics}
    order: Sequence[str] = ("macrostrat", "ics") if channel == "auto" else (channel,)
    problems: list = []
    for name in order:
        try:
            return runners[name](do_fetch, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - report every channel tried
            problems.append(f"  [{name}] {exc}")
    raise IcsUpdateError(
        "no ICS channel could be reached:\n" + "\n".join(problems)
        + "\n\nNothing was written. Rerun when the network is up, force the "
        "channel you can reach with --channel ics|macrostrat, or work fully "
        "offline with --offline-fixture "
        "tests/fixtures/ics_macrostrat_mini.json.")


def run(
    *,
    channel: str = "auto",
    out: Path = DEFAULT_OUT,
    canonical: Path = CANONICAL,
    fixture: Optional[Path] = None,
    write_canonical: bool = False,
    force: bool = False,
    timeout: float = TIMEOUT_SEC,
    retrieved_at: Optional[str] = None,
    ics_version: Optional[str] = None,
    export_csv: Optional[Path] = None,
    carry_forward: bool = True,
    keep_all_intervals: bool = False,
    fetch: Optional[FetchFn] = None,
    reporter: Callable[[str], None] = print,
) -> Dict[str, Any]:
    """Full pipeline; returns a summary dict (this is what the tests assert)."""
    canonical = Path(canonical)
    baseline = load_table(canonical)
    seed_era_map(baseline)
    result = acquire(channel, fixture=fixture, fetch=fetch, timeout=timeout)
    stamp = retrieved_at or _utc_now_stamp()
    version = str(ics_version if ics_version is not None
                  else (result.get("ics_version")
                        or read_baseline_version(
                            canonical.parent.parent / "standards" / "ics.py",
                            ROOT / "rca_core" / "standards" / "ics.py")
                        or "unspecified"))
    table, notes = build_table(
        result, baseline=baseline, retrieved_at=stamp, ics_version=version,
        carry_forward=carry_forward, keep_all_intervals=keep_all_intervals)
    errors, warnings = validate_table(table)
    diff = diff_tables(baseline, table)

    reporter(f"channel: {result.get('channel')} ({result.get('source')})")
    reporter(f"ics_version: {version or 'unknown'}   retrieved_at: {stamp}"
             + (f"   upstream modified: {result['modified']}"
                if result.get("modified") else ""))
    reporter(f"intervals: {len(table)} rows (baseline {len(baseline)} rows)")
    for line in render_diff(diff, old_name=canonical.name, new_name=Path(out).name):
        reporter(line)
    if notes:
        reporter(f"mapping notes ({len(notes)}):")
        for note in _cap(notes, 12):
            reporter(f"  ! {note}")
    if warnings:
        reporter(f"validation warnings ({len(warnings)}):")
        for warning in _cap(warnings, 20):
            reporter(f"  ~ {warning}")
    if errors:
        reporter(f"VALIDATION ERRORS ({len(errors)}) - this table is not trustworthy:")
        for error in _cap(errors, 40):
            reporter(f"  x {error}")
    if not errors or force:
        write_json(out, table)
        reporter(f"wrote {out}")
    else:
        reporter(f"nothing written to {out}; fix the errors or pass --force")
    if export_csv is not None:
        rows = export_alignment_csv(export_csv, table, baseline, diff)
        reporter(f"alignment table: {rows} rows -> {export_csv}")

    if write_canonical:
        promoted = promote_to_canonical(canonical, table, errors=errors, diff=diff,
                                        force=force, reporter=reporter)["promoted"]
    else:
        promoted = False
        reporter(f"dry run: {canonical.name} untouched "
                 "(--write-canonical promotes it, backing it up to .bak first)")

    return {"table": table, "diff": diff, "errors": errors, "warnings": warnings,
            "notes": notes, "version": version, "retrieved_at": stamp,
            "result": result, "promoted": promoted, "baseline": baseline}


def promote_to_canonical(
    path: Path,
    table: Dict[str, Dict[str, Any]],
    *,
    errors: Sequence[str],
    diff: Dict[str, Any],
    force: bool,
    reporter: Callable[[str], None],
) -> Dict[str, Any]:
    """Back up and overwrite the canonical table, guarding the consumers."""
    path = Path(path)
    if errors and not force:
        reporter(f"REFUSING to write {path.name}: {len(errors)} validation "
                 "error(s) above. Re-run with --force only once they are checked.")
        return {"promoted": False, "reason": "validation errors"}
    lost = list(diff["removed"])
    if lost and not force:
        reporter(f"REFUSING to write {path.name}: {len(lost)} baseline name(s) "
                 f"have no row in the refresh: {', '.join(lost[:8])}")
        reporter("Baseline keys must survive or stored range charts stop "
                 "resolving them - keep the default carry-forward (drop "
                 "--no-carry-forward), or pass --force.")
        return {"promoted": False, "reason": "missing baseline keys"}
    backup = path.with_name(path.name + ".bak")
    if path.exists():
        backup.write_bytes(path.read_bytes())
        reporter(f"backed up {path.name} -> {backup.name}")
    write_json(path, table)
    reporter(f"WROTE CANONICAL {path}")
    reporter("reminder: js/ics_table.js mirrors this table and "
             "tests/test_ics_invariants.py pins the period bounds - update both "
             "in the same commit.")
    return {"promoted": True, "reason": "written"}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="update_ics.py",
        description="Refresh the bundled ICS chronostratigraphic table from "
                    "the Macrostrat defs API or the ICS chart RDF, keeping "
                    "the ics_2024.json schema as a strict superset.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--channel", choices=("auto", "macrostrat", "ics"),
                        default="auto", help="pull channel; auto = macrostrat then ics")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT,
                        help="where the refreshed table is written")
    parser.add_argument("--canonical", type=Path, default=CANONICAL,
                        help="baseline table to diff against")
    parser.add_argument("--write-canonical", action="store_true",
                        help="also overwrite the baseline file (backs it up to .bak)")
    parser.add_argument("--force", action="store_true",
                        help="allow writing despite validation errors / dropped keys")
    parser.add_argument("--offline-fixture", type=Path, default=None,
                        help="local JSON/TTL fixture instead of the network (CI)")
    parser.add_argument("--timeout", type=float, default=TIMEOUT_SEC,
                        help="per-request timeout in seconds")
    parser.add_argument("--retrieved-at", default=None,
                        help="pin the provenance timestamp (ISO 8601) for reproducible output")
    parser.add_argument("--ics-version", default=None,
                        help="override the chart version stamp")
    parser.add_argument("--export-csv", type=Path, default=None,
                        help="write the name-alignment table here")
    parser.add_argument("--no-carry-forward", action="store_true",
                        help="drop baseline names the upstream no longer lists")
    parser.add_argument("--all-intervals", action="store_true",
                        help="keep pre-Cambrian rows (breaks the Phanerozoic-only invariants)")
    return parser


def main(argv: Optional[list] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        summary = run(
            channel=args.channel, out=args.out, canonical=args.canonical,
            fixture=args.offline_fixture, write_canonical=args.write_canonical,
            force=args.force, timeout=args.timeout, retrieved_at=args.retrieved_at,
            ics_version=args.ics_version, export_csv=args.export_csv,
            carry_forward=not args.no_carry_forward,
            keep_all_intervals=args.all_intervals)
    except IcsUpdateError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3
    if summary["errors"] and not args.force:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
