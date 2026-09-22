"""FIX-2026-09-22 audit item 5: pin rca_core/resources/ics_current.json.

ics_current.json is the update_ics.py refresh output and has ZERO runtime
consumers - switching rca_core/standards/ics.py to load it is a deliberate
research backlog item, NOT an accident to be fixed here. What must not
happen is the file drifting silently (a stage added to the js/ics_table.js
mirror but not the JSON, a row losing its color, a schema key renamed),
because it is the evidence the next canonical promotion is reviewed on.

These assertions are pure schema/invariant checks on the committed file -
offline, no network, no server.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CURRENT = ROOT / "rca_core" / "resources" / "ics_current.json"
BASELINE = ROOT / "rca_core" / "resources" / "ics_2024.json"

# The eight baseline fields of the bundled schema plus the four provenance
# fields scripts/update_ics.py adds (see its module docstring: the output is
# a STRICT SUPERSET of ics_2024.json rows).
REQUIRED_FIELDS = ("rank", "abbrev", "top_ma", "base_ma", "period",
                   "period_top_ma", "period_base_ma", "era",
                   "source", "license", "retrieved_at", "ics_version")
PHANEROZOIC_ERAS = {"Paleozoic", "Mesozoic", "Cenozoic"}
VALID_RANKS = {"Stage", "Series"}


def _load_pairs(path: Path):
    """Load preserving raw duplicate-key evidence (object_pairs_hook)."""
    duplicates: list = []

    def hook(pairs):
        seen = set()
        for key, _value in pairs:
            if key in seen:
                duplicates.append(key)
            seen.add(key)
        return dict(pairs)

    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=hook), duplicates


def test_current_table_row_count_is_pinned():
    """The refreshed ladder is 109 rows (98 baseline + 11 post-2024/12
    stages). A different count means a promotion or a hand-edit happened -
    review scripts/update_ics.py's gates ran, and update this pin + the js
    mirror + ICS_VERSION together."""
    table, _ = _load_pairs(CURRENT)
    assert len(table) == 109


def test_names_are_unique_case_insensitively():
    # ics.py compiles one IGNORECASE regex per key - two keys differing only
    # in case would resolve as one stage silently.
    table, duplicates = _load_pairs(CURRENT)
    assert duplicates == [], "raw JSON contains duplicate keys"
    folded: dict = {}
    for name in table:
        key = name.casefold()
        assert key not in folded, f"{name!r} collides with {folded[key]!r}"
        folded[key] = name


def test_document_is_a_flat_name_to_row_mapping():
    table, _ = _load_pairs(CURRENT)
    assert all(isinstance(name, str) and name.strip() for name in table)
    for name, row in table.items():
        # NO top-level metadata key, NO nested envelope: standards/ics.py
        # iterates every entry as a row.
        assert isinstance(row, dict), f"{name} is {type(row).__name__}, not a row"


def test_every_row_carries_the_full_schema():
    table, _ = _load_pairs(CURRENT)
    for name, row in table.items():
        missing = [f for f in REQUIRED_FIELDS if f not in row]
        assert not missing, f"{name}: missing {missing}"
        assert row["rank"] in VALID_RANKS, f"{name}: rank {row['rank']!r}"
        assert row["era"] in PHANEROZOIC_ERAS, f"{name}: era {row['era']!r}"
        assert row["period"], f"{name}: empty period"
        top, base = row["top_ma"], row["base_ma"]
        assert isinstance(top, float) and isinstance(base, float), \
            f"{name}: bounds must be floats, got {top!r}/{base!r}"
        assert base > top, f"{name}: base_ma {base} not older than top {top}"


def test_color_presence_is_consistent_with_the_carried_forward_flag():
    """Every live refresh row carries the source color; rows WITHOUT color
    must be exactly the carried-forward baseline rows (the flag is their
    explanation). Drift either way means provenance was rewritten by hand."""
    table, _ = _load_pairs(CURRENT)
    uncolored = {name for name, row in table.items() if not row.get("color")}
    carried = {name for name, row in table.items() if row.get("carried_forward")}
    assert uncolored == carried, (
        f"color-less rows {sorted(uncolored - carried)} lack the "
        f"carried_forward flag; flagged rows {sorted(carried - uncolored)} "
        "unexpectedly have a color")


def test_baseline_is_a_subset_of_the_refresh_output():
    """ics_current.json is generated WITH carry-forward by default; dropping
    a 2024/12 key here means stored range charts stop resolving it after a
    promotion."""
    table, _ = _load_pairs(CURRENT)
    baseline, _ = _load_pairs(BASELINE)
    lost = sorted(set(baseline) - set(table))
    assert not lost, f"baseline names missing from ics_current.json: {lost}"


def test_every_row_declares_a_provenance_source():
    table, _ = _load_pairs(CURRENT)
    for name, row in table.items():
        assert row["source"], f"{name}: no source"
        assert "offline-fixture" not in str(row["source"]), (
            f"{name}: ics_current.json was regenerated from a toy fixture - "
            "promotion-relevant files must come from a live channel")
