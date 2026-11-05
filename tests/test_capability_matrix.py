"""The capability declarations must match the code, not just exist.

The failure mode this guards against is not "somebody forgot to update a
table" -- it is "a capability landed in one endpoint and nobody noticed it
was missing from the other four" (GAP-ANALYSIS A5/A6, ARCH-REVIEW P2-14).
Each endpoint is grepped for the capability's stable symbol; the measured
set must equal the declared set.

When you wire an existing capability into a new endpoint, update
rca_core/capability_matrix.py in the SAME commit (and edit its ``reason``
to reflect what is still unwired). When you ADD a capability, add a row --
an undeclared capability is exactly the hole this file exists to remove.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rca_core.capability_matrix import (  # noqa: E402
    CAPABILITIES, ENDPOINTS, ENDPOINT_FILES,
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
