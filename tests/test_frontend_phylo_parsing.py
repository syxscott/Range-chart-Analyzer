"""Test P1-6: JS i18n status.phyloParsing exists in all three languages."""
import os
import pytest


class TestPhyloParsingI18n:
    """P1-6: status.phyloParsing must be present in all three JS language sections."""

    def test_phyloParsing_in_zh(self):
        """Chinese section must have status.phyloParsing."""
        js_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..",
            "js", "i18n.js"
        )
        with open(js_path, encoding="utf-8") as f:
            content = f.read()
        # Chinese section is inside "const RCA_I18N = { zh: {...} };"
        zh_block = "zh: {"
        zh_start = content.find(zh_block)
        zh_end = content.find("RCA_I18N.en")
        zh_section = content[zh_start:zh_end]
        assert "'status.phyloParsing'" in zh_section, (
            "Chinese (zh) section missing 'status.phyloParsing'"
        )
        assert "系统发育树" in zh_section, (
            "Chinese translation should contain '系统发育树'"
        )

    def test_phyloParsing_in_en(self):
        """English section must have status.phyloParsing."""
        js_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..",
            "js", "i18n.js"
        )
        with open(js_path, encoding="utf-8") as f:
            content = f.read()
        # English section is "RCA_I18N.en = {...};"
        en_start = content.find("RCA_I18N.en = {")
        ja_start = content.find("RCA_I18N.ja = {")
        en_section = content[en_start:ja_start]
        assert "'status.phyloParsing'" in en_section, (
            "English (en) section missing 'status.phyloParsing'"
        )
        assert "phylogenetic" in en_section.lower(), (
            "English translation should mention 'phylogenetic'"
        )

    def test_phyloParsing_in_ja(self):
        """Japanese section must have status.phyloParsing."""
        js_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..",
            "js", "i18n.js"
        )
        with open(js_path, encoding="utf-8") as f:
            content = f.read()
        # Japanese section is "RCA_I18N.ja = {...};"
        ja_start = content.find("RCA_I18N.ja = {")
        ja_section = content[ja_start:]
        assert "'status.phyloParsing'" in ja_section[:5000], (
            "Japanese (ja) section missing 'status.phyloParsing'"
        )
        assert "系統樹" in ja_section[:5000], (
            "Japanese translation should contain '系統樹'"
        )

    def test_all_three_translations_non_empty(self):
        """All three translations of status.phyloParsing must be non-empty."""
        js_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..",
            "js", "i18n.js"
        )
        with open(js_path, encoding="utf-8") as f:
            content = f.read()

        translations = [
            ("zh", "正在解析系统发育树……"),
            ("en", "Parsing phylogenetic tree..."),
            ("ja", "系統樹を解析中……"),
        ]
        for lang, expected_substr in translations:
            assert expected_substr in content, (
                f"Missing or changed {lang} translation for status.phyloParsing"
            )
