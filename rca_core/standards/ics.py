"""ICS 2023/2024 International Chronostratigraphic Chart lookup.

Provides functions to map stage names to/from Ma ages, compare stage
order, and parse age range strings.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional


_ICS_PATH = Path(__file__).parent.parent / "resources" / "ics_2024.json"
ICS_2024: dict[str, dict] = json.loads(_ICS_PATH.read_text(encoding="utf-8"))


def ics_stage_from_age(ma: float) -> Optional[str]:
    """Given a Ma value, return the corresponding ICS Stage name.


    Traverses all stages in ICS_2024 and returns the stage whose
    age range contains the given Ma value.
    In geological convention: base_ma is older (larger number),
    top_ma is younger (smaller number).
    Returns None if no stage matches.
    """
    for name, info in ICS_2024.items():
        top = info.get("top_ma", 0)
        base = info.get("base_ma", 0)
        # In geological convention, base > top (older > younger)
        # So we check: top <= ma <= base
        if top <= ma <= base:
            return name
    return None


def ics_age_compare(stage1: str, stage2: str) -> Optional[int]:
    """Compare two stages by relative stratigraphic order.

    
    Returns:
    -1 if stage1 is older (lower in section, older Ma value)
     0 if stage1 and stage2 are the same stage
     1 if stage1 is younger (higher in section, younger Ma value)
     None if either stage is unknown
    
    Note: In stratigraphic convention, OLDER rocks have HIGHER Ma values.
    So a stage with base_ma=1200 is older than one with top_ma=1000.
    """
    s1 = ICS_2024.get(stage1)
    s2 = ICS_2024.get(stage2)
    if s1 is None or s2 is None:
        return None
    # Compare by mid-point of age range
    mid1 = (s1.get("base_ma", 0) + s1.get("top_ma", 0)) / 2
    mid2 = (s2.get("base_ma", 0) + s2.get("top_ma", 0)) / 2
    if mid1 > mid2:
        return -1  # stage1 is older (higher Ma)
    elif mid1 < mid2:
        return 1   # stage1 is younger (lower Ma)
    else:
        return 0


_STAGE_PATTERN = re.compile(
    r"(?:Stage\s+)?(\w+)", re.IGNORECASE
)

def ics_parse_age_range(text: str) -> list[str]:
    """Parse a free-text age_range string into a list of Stage names.


    Handles formats like:
    - "Late Permian (Wuchiapingian - Changhsingian)" -> ["Wuchiapingian", "Changhsingian"]
    - "Wuchiapingian" -> ["Wuchiapingian"]
    - "Early Triassic" -> [] (no specific stage)

    Returns a list of stage names found in the text (may be empty).
    The stages are returned in the order they appear in the text.
    """
    if not text:
        return []

    # Find all stage matches with their positions in the text
    matches: list[tuple[int, str]] = []
    for stage_name in ICS_2024.keys():
        pattern = re.compile(r"\b" + re.escape(stage_name) + r"\b", re.IGNORECASE)
        for m in pattern.finditer(text):
            matches.append((m.start(), stage_name))

    # Sort by position in text (first appearance first)
    matches.sort(key=lambda x: x[0])
    return [name for _, name in matches]


def ics_era(stage: str) -> Optional[str]:
    """Return the era for a given stage name.

    
    Returns one of: "Paleozoic", "Mesozoic", "Cenozoic", "Precambrian"
    or None if the stage is unknown.
    """
    info = ICS_2024.get(stage)
    if info is None:
        return None
    return info.get("era")


def ics_period(stage: str) -> Optional[str]:
    """Return the period for a given stage name.

    
    Returns the period name (e.g., "Permian", "Jurassic")
    or None if the stage is unknown.
    """
    info = ICS_2024.get(stage)
    if info is None:
        return None
    return info.get("period")
