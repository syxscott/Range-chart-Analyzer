"""Regression / clarification tests for P1-7: rca_core.prompt.py
Japanese stratigraphic terminology mapping.

REVIEW-2026-07-25 P1-7 noted "Japanese geological terms are direct
Chinese translations; 組=Formation/段=Member contradict the actual
Japanese academic usage". This test verifies the prompt text actually
contains the Japanese-native forms (NATIVE KANJI in Japanese usage,
not the Chinese-simplified forms) so the LLM receives accurate
instructions.
"""
from __future__ import annotations

import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rca_core.prompt import RANGE_CHART_SYSTEM_PROMPT
from rca_core import prompt as _p


class TestPromptJapaneseTerminology:
    def test_japanese_block_present(self):
        """The range-chart system prompt must contain an explicit
        Japanese-stratigraphic-terms rule."""
        assert "JAPANESE STRATIGRAPHIC TERMS" in RANGE_CHART_SYSTEM_PROMPT

    def test_japanese_native_kanji_used(self):
        """Japanese terms must use native kanji: 統, 階, 組, 帯 — NOT
        the Chinese-simplified forms (统, 阶, 组, 带)."""
        jp_block = next(
            (line for line in RANGE_CHART_SYSTEM_PROMPT.splitlines()
             if "JAPANESE STRATIGRAPHIC TERMS" in line),
            "",
        )
        # Find the rule text (subsequent line(s)).
        rules = RANGE_CHART_SYSTEM_PROMPT
        # Native forms present
        for kanji in ("統", "階", "組", "段", "帯"):
            assert kanji in rules, (
                f"Japanese prompt must contain native kanji {kanji!r};"
                f" found only Chinese-simplified form instead?"
            )
        # Common Chinese-simplified counterparts should NOT appear in
        # the Japanese rule line.
        for ch_simp in ("统", "阶", "组", "带"):
            # Allow Chinese-form presence in the CHINESE rule line, but
            # they must NOT appear in the JAPANESE rule text.
            assert ch_simp not in next(
                (line for line in rules.splitlines()
                 if "JAPANESE STRATIGRAPHIC TERMS" in line),
                "",
            ), f"Chinese-simplified {ch_simp!r} leaked into Japanese rule"

    def test_formation_and_member_mapping_in_japanese(self):
        rules = RANGE_CHART_SYSTEM_PROMPT
        assert "組 = Formation" in rules, "組 should map to Formation in Japanese"
        assert "段 = Member" in rules, "段 should map to Member in Japanese"
        assert "階 = Stage" in rules, "階 should map to Stage in Japanese"
        assert "群 = Group" in rules, "群 should map to Group in Japanese"