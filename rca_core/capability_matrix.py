"""Where each capability has actually landed, declared as data.

WHY THIS EXISTS
===============
ARCH-REVIEW-2026-11-02 (P2-14) and GAP-ANALYSIS-2026-11-02 (A5/A6) found the
same structural defect from two directions: the differential parity harness
locks "same input -> same output on both engines", but it CANNOT tell that a
new capability was wired into only one of the five endpoints. Two real
incidents came from exactly that:

  * GBIF name verification / fuzzy matching was wired into the browser only
    (js/app.js, js/table.js); server.py and both desktop GUIs never called it.
    README promised it as an ICZN safety net, and Fluent is the DEFAULT UI.
    CAPABILITY-2026-11 wired Fluent as an explicit, default-OFF opt-in; the
    historical incident is kept here because it is why this module exists.
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
        "landed": frozenset({"core", "browser", "fluent"}),
        "reason": "GBIF verification now runs in the browser (js/app.js via "
                  "rcaNameIssuesFromGbif) AND in the default Fluent desktop "
                  "UI (gui_fluent.py::_run_name_capabilities -> "
                  "rca_core.names.verify_names, inside a QThread worker). "
                  "Fluent's is an EXPLICIT, default-OFF opt-in because it "
                  "costs extra network requests; the switch lives on the "
                  "Settings page. server.py and gui.py still do not call it.",
        "waivers": {
            "server": "The server returns JSON to the browser, which runs the "
                      "GBIF lookup client-side; a server-side duplicate would "
                      "double the round-trip for every extraction.",
            "tk": "The zero-dependency Tk fallback renders plain tables and "
                  "makes no outbound GBIF request by design (G6 in "
                  "docs/OWNER_DECISIONS.md freezes its feature surface).",
        },
    },
    "names.fuzzy": {
        "probes": ("fuzzy",),
        "landed": frozenset({"core", "browser", "fluent"}),
        "reason": "Fuzzy matching renders in the browser table (js/table.js) "
                  "and now in the Fluent result panel: "
                  "collect_candidate_names() feeds verify_names(), and "
                  "name_issues() FUZZY suggestions are shown as candidates "
                  "with the suggested canonical name (gui_fluent.py). "
                  "Same default-OFF opt-in as names.verify. server.py and "
                  "gui.py still do not build a name-suggestion UI.",
        "waivers": {
            "server": "Fuzzy suggestions are a rendering concern; the server "
                      "returns raw rows and never builds a name-suggestion UI.",
            "tk": "The Tk table has no suggestion widget; adding one would "
                  "require a Tk-specific name index the core does not ship "
                  "(G6 freezes the Tk fallback's feature surface).",
        },
    },
    "grounding": {
        "probes": ("check_name_grounding",),
        "landed": frozenset({"core", "server", "fluent"}),
        "reason": "Name grounding (is the name actually PRINTED in the "
                  "figure?) is invoked by server.py and now by the default "
                  "Fluent desktop UI (gui_fluent.py::_run_name_capabilities "
                  "-> rca_core.grounding.check_name_grounding, on a QThread "
                  "worker with a queued signal). Fluent's is an EXPLICIT, "
                  "default-OFF opt-in because it costs a second vision-model "
                  "call; failures degrade to 'could not check', never to a "
                  "clean result, matching server.py's semantics. The browser "
                  "and the Tk fallback still do not run it.",
        "waivers": {
            "browser": "The browser can call the provider directly; that path "
                       "bypasses the server-side grounding step entirely.",
            "tk": "The Tk shell calls extract() without the grounding pass; "
                  "changing it affects the evidence-chain report (G6 freezes "
                  "the Tk fallback's feature surface).",
        },
    },
    "result_cache": {
        # P0-1 (2026-11, concurrent handler split): the server's extraction
        # body moved into rca_core/pipeline.py, which now calls
        # rca_core.cache.build_extraction_key instead of ResultCache.make_key.
        # `make_key` therefore disappeared from server.py, even though the
        # server endpoint still lands the cache. ``_cache_singleton_safe`` is
        # the stable, non-comment server-side symbol that survived the split
        # (server.py: def + dependency injection). Both are kept so the probe
        # works whether the handler lives in server.py or in the new module.
        "probes": ("make_key", "_cache_singleton_safe"),
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
