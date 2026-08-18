"""Regression test for the Korean Stage suffix and the missing-comma bug.

REVIEW-2026-08-17 (P1-2 + P1-3):
  - P1-2: prompt.py line 123 used ``계 = Stage (階)``, but the Korean
    suffix for Stage is ``절`` (캄파절/Campanian Stage, 창싱절/Changhsingian
    Stage), not ``계``. The Stage and System suffixes must NOT collide —
    doing so reintroduces the exact "confuse chronostratigraphic and
    lithostratigraphic units" hazard the project's iron rule 2 forbids.
  - P1-3: prompt.py line 128 was a bare string literal without a trailing
    comma before the next list element, so Python's adjacent-string
    concatenation silently merged "PORTUGUESE: ... proper nouns" with
    "READ SPECIES NAMES CAREFULLY. ..." into ONE bullet, dropping the
    item boundary in the model-facing prompt.

Both fixes are validated by the same test: the prompt's per-language
bullet list must (a) contain the Korean ``절 = Stage`` mapping and (b)
contain "KOREAN, GERMAN, FRENCH, SPANISH, PORTUGUESE:" and
"READ SPECIES NAMES CAREFULLY." as TWO separate list elements (the
former ending with a period, the latter starting with "- READ"), not
one glued-together string.
"""

from __future__ import annotations

import re

from rca_core.prompt import RANGE_CHART_SYSTEM_PROMPT


def test_korean_stage_suffix_is_jeol_not_gye():
    """The Korean Stage suffix MUST be ``절``, not ``계``.

    Background: ICS Korean stratigraphic convention uses ``계`` for
    System (계 = 系) but ``절`` for Stage (절 = 階). Confusing them would
    cause the model to mislabel every "OO계" / "OO절" boundary in a
    Korean-language chart.
    """
    assert "절 = Stage" in RANGE_CHART_SYSTEM_PROMPT, (
        "Korean Stage suffix '절 = Stage' not found in RANGE_CHART_SYSTEM_PROMPT"
    )
    # Sanity: System is still mapped to 계 (this is the correct mapping).
    assert re.search(r"\b계\s*=\s*System", RANGE_CHART_SYSTEM_PROMPT), (
        "Korean System mapping '계 = System' missing"
    )


def test_korean_strat_terms_bullet_does_not_collapse_system_and_stage():
    """The KOREAN STRATIGRAPHIC TERMS bullet must NOT map BOTH System and
    Stage to the same Korean suffix. The previous code used ``계`` for
    both, which is the exact bug the test guards against."""
    # Split into lines and find the Korean bullet.
    lines = RANGE_CHART_SYSTEM_PROMPT.split("\n")
    korean_lines = [l for l in lines if "KOREAN" in l and "Stage" in l
                    and "STRATIGRAPHIC" in l]
    assert korean_lines, "KOREAN STRATIGRAPHIC TERMS bullet not found"
    # Count how many distinct Korean suffixes are mapped to non-Zone
    # ranks. We expect 계 (System), 절 (Stage), 통 (Series), 군 (Group),
    # 층 (Formation), 단 (Member) — six unique suffixes.
    korean_bullet = korean_lines[0]
    suffix_assignments = re.findall(r"([가-힣])\s*=\s*(System|Series|Stage|Group|Formation|Member|Zone)",
                                    korean_bullet)
    suffixes = [s for s, _ in suffix_assignments]
    assert len(set(suffixes)) == len(suffixes), (
        f"duplicate Korean suffix mapping: {suffix_assignments}"
    )


def test_latin_binomial_rule_is_separate_bullet_not_glued():
    """The "KOREAN, GERMAN, FRENCH, SPANISH, PORTUGUESE:" rule and the
    "READ SPECIES NAMES CAREFULLY" rule must be TWO separate list
    elements (two distinct lines starting with "- "). They were silently
    concatenated when prompt.py:128 was missing its trailing comma."""
    lines = RANGE_CHART_SYSTEM_PROMPT.split("\n")
    # The Latin-binomial rule line must end with a clean closing paren
    # (or proper-noun sentence), and the NEXT line must start with "- ".
    # In the broken state, the two were glued into one line.
    lang_line_idx = None
    for i, line in enumerate(lines):
        if line.startswith("- KOREAN, GERMAN, FRENCH, SPANISH, PORTUGUESE:"):
            lang_line_idx = i
            break
    assert lang_line_idx is not None, (
        "Latin-binomial rule ('KOREAN, GERMAN, FRENCH, SPANISH, PORTUGUESE:') "
        "not found as a standalone bullet"
    )
    # The Portuguese/last-language sentence must end with a period and
    # NOT continue onto "READ SPECIES NAMES CAREFULLY" mid-sentence.
    lang_line = lines[lang_line_idx]
    assert "READ SPECIES NAMES CAREFULLY" not in lang_line, (
        "READ SPECIES NAMES CAREFULLY was silently concatenated onto the "
        "previous bullet — comma was missing in the source list."
    )
    # And the next line after the lang bullet must be the species-names
    # bullet (or any other distinct bullet), starting with "- ".
    next_line = lines[lang_line_idx + 1] if lang_line_idx + 1 < len(lines) else ""
    assert next_line.startswith("- "), (
        f"Expected the next bullet to start with '- ', got: {next_line!r}"
    )
    assert "READ SPECIES NAMES" in next_line or "SPECIES NAMES" in next_line, (
        "The READ SPECIES NAMES bullet was not found on the line after the "
        "KOREAN, GERMAN, FRENCH, SPANISH, PORTUGUESE bullet — they may have "
        "been merged."
    )