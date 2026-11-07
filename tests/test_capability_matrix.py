"""The capability declarations must match the code, not just exist.

The failure mode this guards against is not "somebody forgot to update a
table" -- it is "a capability landed in one endpoint and nobody noticed it
was missing from the other four" (GAP-ANALYSIS A5/A6, ARCH-REVIEW P2-14).
Each endpoint is grepped for the capability's stable symbol; the measured
set must equal the declared set.

When you wire an existing capability into a new endpoint, update
rca_core/capability_matrix.py in the SAME commit (move it into ``landed`` and
DELETE its now-stale ``waivers`` entry, then edit ``reason``). When you ADD a
capability, add a row AND a waiver for every endpoint you did not wire --
an undeclared capability is exactly the hole this file exists to remove.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rca_core.capability_matrix import (  # noqa: E402
    CAPABILITIES, ENDPOINTS, ENDPOINT_FILES, missing_waivers, unwired,
)


def _landed_by_probe(probes: tuple[str, ...]) -> set[str]:
    landed = set()
    for endpoint in ENDPOINTS:
        for rel in ENDPOINT_FILES[endpoint]:
            path = ROOT / rel
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            if any(probe in text for probe in probes):
                landed.add(endpoint)
                break
    return landed


def test_every_capability_declares_and_explains_its_gaps() -> None:
    assert CAPABILITIES, "capability matrix is empty"
    for name, spec in CAPABILITIES.items():
        assert spec.get("probes"), f"{name}: no probe symbol declared"
        assert isinstance(spec.get("probes"), tuple), f"{name}: probes must be a tuple"
        assert set(spec["landed"]) <= set(ENDPOINTS), f"{name}: unknown endpoint"
        assert spec["landed"], f"{name}: landed in no endpoint at all"
        # An unwired endpoint MUST carry a written reason. This is the whole
        # point of the file: "we did not do it" must be a decision, not a hole.
        assert spec.get("reason"), (
            f"{name}: some endpoints are unwired but no reason is recorded"
        )


def test_every_unwired_endpoint_has_its_own_waiver() -> None:
    """A blanket `reason` can hide ONE endpoint's gap. 2026-11-07.

    Every endpoint that has not landed a capability must carry its own
    written waiver, and a wired endpoint must NOT carry a stale one. This is
    the check that turns "we did not do it" into per-endpoint, reviewable data.
    """
    assert missing_waivers() == [], "\n".join(missing_waivers())
    for name, spec in CAPABILITIES.items():
        assert set(spec["waivers"]) == set(unwired(name)), (
            f"{name}: waivers do not cover exactly the unwired endpoints"
        )


def test_waiver_contract_detects_a_missing_waiver() -> None:
    """The guard must be able to fail, not merely describe a happy state."""
    import rca_core.capability_matrix as cm

    # CAPABILITY-2026-11: this fixture used to hard-code names.verify's
    # "fluent" waiver. Fluent is now LANDED for that capability (and its
    # waiver must therefore be deleted -- missing_waivers() rejects a stale
    # one), so NOTHING here may name a specific capability or endpoint.
    # Pick the first spec that has any waiver, then derive both the endpoint
    # to delete and an endpoint that IS wired purely from the data. The
    # three branches keep their original meaning: deleting a required
    # waiver, blanking it, and adding a stale one to a wired endpoint must
    # each be detected. Deriving instead of hard-coding means wiring more
    # capabilities into Fluent (or any other endpoint) can never turn this
    # into a StopIteration "test infrastructure broke" failure.
    spec = next((s for s in cm.CAPABILITIES.values()
                 if s.get("waivers")), None)
    assert spec is not None, (
        "no capability carries a waiver; the mutation test needs one")
    endpoint = sorted(spec["waivers"])[0]
    landed = sorted(spec["landed"])[0]
    saved = spec["waivers"]
    try:
        spec["waivers"] = dict(saved)
        del spec["waivers"][endpoint]
        assert any(f"'{endpoint}' is unwired with no waiver" in m
                   for m in cm.missing_waivers()), (
            f"missing_waivers() did not notice a deleted {endpoint!r} waiver")
        spec["waivers"][endpoint] = "   "
        assert any(f"waiver for '{endpoint}' is empty" in m
                   for m in cm.missing_waivers()), (
            "missing_waivers() did not notice a blank waiver")
        spec["waivers"][landed] = "stale"
        assert any("has a waiver but IS wired" in m
                   for m in cm.missing_waivers()), (
            "missing_waivers() did not notice a stale wired-endpoint waiver")
    finally:
        spec["waivers"] = saved
    assert cm.missing_waivers() == [], "waiver fixture was not restored"


def test_declared_landings_match_the_code() -> None:
    """The declaration is evidence only if it tracks the real files."""
    mismatches = []
    for name, spec in CAPABILITIES.items():
        measured = _landed_by_probe(spec["probes"])
        declared = set(spec["landed"])
        if measured != declared:
            mismatches.append(
                f"  {name}: declared={sorted(declared)} "
                f"measured={sorted(measured)} (probes {spec['probes']!r})"
            )
    assert not mismatches, (
        "capability matrix drifted from the code:\n" + "\n".join(mismatches)
        + "\nUpdate rca_core/capability_matrix.py in the same commit as the "
          "wiring change."
    )


def test_probes_are_real_symbols_not_stale_comments() -> None:
    """A probe that only appears in a comment would be a vacuous guard."""
    for name, spec in CAPABILITIES.items():
        hits = 0
        for endpoint in ENDPOINTS:
            for rel in ENDPOINT_FILES[endpoint]:
                path = ROOT / rel
                if not path.is_file():
                    continue
                for line in path.read_text(encoding="utf-8",
                                           errors="replace").splitlines():
                    stripped = line.strip()
                    if any(probe in stripped for probe in spec["probes"]) and not stripped.startswith(
                            ("#", "//", "*", "/*")):
                        hits += 1
        assert hits > 0, f"{name}: probes {spec['probes']!r} only appear in comments"
