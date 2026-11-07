"""Where each capability has actually landed, declared as data.

WHY THIS EXISTS
===============
ARCH-REVIEW-2026-11-02 (P2-14) and GAP-ANALYSIS-2026-11-02 (A5/A6) found the
same structural defect from two directions: the differential parity harness
locks "same input -> same output on both engines", but it CANNOT tell that a
new capability was wired into only one of the five endpoints. Two real
incidents came from exactly that:

  * GBIF name verification / fuzzy matching is wired into the browser only
    (js/app.js, js/table.js); server.py and both desktop GUIs never call it.
    README promised it as an ICZN safety net, and Fluent is the DEFAULT UI.
  * The ICS transport table once had 98 entries on the desktop and 109 in
    the browser; the desktop parsed "Late Pleistocene" as the whole epoch
    (2.58 Ma instead of 0.129 Ma -- 20x too old).

This module does NOT demand that every endpoint implement every capability --
a desktop app has no reason to offer browser-direct API calls. It demands
that EVERY unwired endpoint carry its OWN written waiver, so a missing wiring
is a line of data instead of a silent hole. A single blanket ``reason`` per
capability was not enough: one sentence about the browser could quietly leave
the default Fluent desktop UI unexplained (2026-11-07).

``tests/test_capability_matrix.py`` greps each endpoint's real files with
the probes below and fails when the declaration drifts from the code, and
fails CI when any unwired endpoint lacks a waiver. That is the part that
turns this from documentation into a guard.

DESIGN CONSTRAINT: DATA ONLY, NO SIBLING IMPORTS
================================================
Same rule as rca_core/modes.py. Importing anything from this module must
never pull in extraction, LLM or GUI code.
"""
from __future__ import annotations

from typing import Any

#: The five endpoints that can consume a core capability.
ENDPOINTS = ("core", "browser", "server", "tk", "fluent")

#: Files that constitute each endpoint for probe purposes.
ENDPOINT_FILES: dict[str, tuple[str, ...]] = {
    "core": ("rca_core/names.py", "rca_core/grounding.py", "rca_core/cache.py",
             "rca_core/standards/ics.py"),
    "browser": ("js/app.js", "js/table.js", "js/minimax.js", "js/ics_table.js"),
    "server": ("server.py",),
    "tk": ("gui.py",),
    "fluent": ("gui_fluent.py", "gui_fluent_pages.py",
               "gui_fluent_providers.py", "gui_fluent_history_detail.py"),
}

#: capability -> {"landed": set of endpoints, "probes": stable symbols,
#: "reason": one-line summary, "waivers": unwired endpoint -> why not}.
#: A waiver is REQUIRED for every endpoint in ENDPOINTS - landed, and no
#: waiver may be recorded for an endpoint that HAS landed the capability.
#:
#: ``probes`` is a tuple of plain substrings (not regexes): an endpoint
#: counts as landed when ANY probe appears in one of its files. Cross-language
#: mirrors legitimately use different symbol names, e.g. Python's
#: ``verify_names`` vs the browser's ``rcaNameIssuesFromGbif``. Keep probes
#: to STABLE symbol names, not comments -- comments rot silently.
CAPABILITIES: dict[str, dict[str, Any]] = {
    "names.verify": {
        "probes": ("verify_names", "rcaNameIssuesFromGbif"),
        "landed": frozenset({"core", "browser"}),
        "reason": "GBIF verification is wired only into the browser "
                  "(js/app.js, via the JS mirror rcaNameIssuesFromGbif). "
                  "server.py, gui.py and the Fluent GUI never call "
                  "rca_core.names.verify_names, so the default desktop UI "
                  "lacks the ICZN safety net the README promises.",
        "waivers": {
            "server": "The server returns JSON to the browser, which runs the "
                      "GBIF lookup client-side; a server-side duplicate would "
                      "double the round-trip for every extraction.",
            "tk": "The zero-dependency Tk fallback renders plain tables and "
                  "makes no outbound GBIF request by design.",
            "fluent": "Fluent is the DEFAULT desktop UI and its result/history "
                      "views never call verify_names. Wiring it needs an owner "
                      "decision about an extra blocking network call "
                      "(docs/OWNER_DECISIONS.md).",
        },
    },
    "names.fuzzy": {
        "probes": ("fuzzy",),
        "landed": frozenset({"core", "browser"}),
        "reason": "Same as names.verify: fuzzy matching renders only in the "
                  "browser table (js/table.js). The desktop endpoints have "
                  "zero occurrences of the feature.",
        "waivers": {
            "server": "Fuzzy suggestions are a rendering concern; the server "
                      "returns raw rows and never builds a name-suggestion UI.",
            "tk": "The Tk table has no suggestion widget; adding one would "
                  "require a Tk-specific name index the core does not ship.",
            "fluent": "The Fluent table has no suggestion widget; this is the "
                      "same gap as names.verify and belongs to the same owner "
                      "decision (docs/OWNER_DECISIONS.md).",
        },
    },
    "grounding": {
        "probes": ("check_name_grounding",),
        "landed": frozenset({"core", "server"}),
        "reason": "Name grounding (is the name actually PRINTED in the "
                  "figure?) is invoked only by server.py. The two desktop "
                  "endpoints and the browser never run it, so their results "
                  "cannot distinguish a read name from a recalled one.",
        "waivers": {
            "browser": "The browser can call the provider directly; that path "
                       "bypasses the server-side grounding step entirely.",
            "tk": "The Tk shell calls extract() without the grounding pass; "
                  "changing it affects the evidence-chain report.",
            "fluent": "The Fluent shell calls extract() without the grounding "
                      "pass; this is a scientific-correctness gap, not just a "
                      "missing button (docs/OWNER_DECISIONS.md).",
        },
    },
    "result_cache": {
        "probes": ("make_key",),
        "landed": frozenset({"core", "server"}),
        "reason": "The idempotent result cache is a server-side feature; "
                  "the browser talks to an API directly and the desktop "
                  "endpoints do not cache model replies. Intentional, but "
                  "worth stating: a repeated desktop extraction costs money.",
        "waivers": {
            "browser": "The browser holds no credentials of its own for a "
                       "cache layer and calls the provider directly.",
            "tk": "No local result cache; repeating an extraction re-bills "
                  "the provider. Accepted for the zero-dependency fallback.",
            "fluent": "No local result cache; repeating an extraction "
                      "re-bills the provider. Tracked as a cost gap, not a "
                      "correctness gap.",
        },
    },
    "ics_chart": {
        "probes": ("ICS_VERSION", "ics_table"),
        "landed": frozenset({"core", "browser"}),
        "reason": "The Python ICS chart (v2024/12) and its browser mirror "
                  "js/ics_table.js are the two age tables; the server and "
                  "both desktop GUIs consume them only through rca_core, "
                  "never directly. The 98-vs-109 incident showed this "
                  "asymmetry needs a machine-checked contract "
                  "(tests/test_ics_invariants.py).",
        "waivers": {
            "server": "Consumes the chart through rca_core.standards.ics; a "
                      "direct copy here would create a third source of truth.",
            "tk": "Consumes the chart through rca_core only. No duplicated "
                  "table is wanted in this shell.",
            "fluent": "Consumes the chart through rca_core only. The bundled "
                      "history-detail page inlines js/ics_table.js for display, "
                      "which is the browser mirror, not a Fluent-local copy.",
        },
    },
}


def unwired(capability: str) -> frozenset[str]:
    """Endpoints that have NOT landed a capability."""
    return frozenset(set(ENDPOINTS) - set(CAPABILITIES[capability]["landed"]))


def missing_waivers() -> list[str]:
    """Waiver-contract violations, as human-readable strings.

    Empty means compliant. Kept as a plain function (no imports, no I/O) so
    CI, the pytest guard and a future `doctor` line all ask the same question.
    """
    problems: list[str] = []
    for name, spec in CAPABILITIES.items():
        expected = set(unwired(name))
        declared = set(spec.get("waivers", {}))
        for endpoint in sorted(expected - declared):
            problems.append(f"{name}: endpoint '{endpoint}' is unwired with no waiver")
        for endpoint in sorted(declared - expected):
            problems.append(
                f"{name}: endpoint '{endpoint}' has a waiver but IS wired "
                "-- update 'landed' or delete the stale waiver")
        for endpoint in sorted(declared - set(ENDPOINTS)):
            problems.append(f"{name}: unknown endpoint '{endpoint}' in waivers")
        for endpoint in sorted(declared & expected):
            if not str(spec["waivers"][endpoint]).strip():
                problems.append(f"{name}: waiver for '{endpoint}' is empty")
    return problems


__all__ = ["ENDPOINTS", "ENDPOINT_FILES", "CAPABILITIES", "unwired",
           "missing_waivers"]
