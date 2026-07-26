"""Test P1-9: col.formations split into col.formation (组/Formation) and col.group (群/Group).

The i18n key col.formations is renamed to col.formation (Formation=组/地层)
and a new col.group (Group=群) key is added.
"""
import os
import pytest


class TestColFormationGroupI18n:
    """P1-9: i18n keys for Formation vs Group distinction."""

    def test_python_zh_has_col_formation(self):
        """Chinese Python i18n must have col.formation."""
        from rca_core.i18n import TRANSLATIONS
        assert "col.formation" in TRANSLATIONS["zh"], "zh must have col.formation"
        assert "col.group" in TRANSLATIONS["zh"], "zh must have col.group"
        # old col.formations must NOT exist
        assert "col.formations" not in TRANSLATIONS["zh"], "zh must NOT have old col.formations"

    def test_python_zh_col_formation_value(self):
        """Chinese col.formation should contain 组 or 地层."""
        from rca_core.i18n import TRANSLATIONS
        val = TRANSLATIONS["zh"]["col.formation"]
        assert "组" in val or "地层" in val

    def test_python_zh_col_group_value(self):
        """Chinese col.group should contain 群."""
        from rca_core.i18n import TRANSLATIONS
        val = TRANSLATIONS["zh"]["col.group"]
        assert "群" in val

    def test_python_en_has_col_formation(self):
        """English Python i18n must have col.formation and col.group."""
        from rca_core.i18n import TRANSLATIONS
        assert "col.formation" in TRANSLATIONS["en"], "en must have col.formation"
        assert "col.group" in TRANSLATIONS["en"], "en must have col.group"
        assert "col.formations" not in TRANSLATIONS["en"]

    def test_python_ja_has_col_formation(self):
        """Japanese Python i18n must have col.formation and col.group."""
        from rca_core.i18n import TRANSLATIONS
        assert "col.formation" in TRANSLATIONS["ja"], "ja must have col.formation"
        assert "col.group" in TRANSLATIONS["ja"], "ja must have col.group"
        assert "col.formations" not in TRANSLATIONS["ja"]

    def test_js_zh_has_col_formation(self):
        """JS zh section must have col.formation and col.group."""
        js_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..",
            "js", "i18n.js"
        )
        with open(js_path, encoding="utf-8") as f:
            content = f.read()
        zh_block = content.find("zh: {")
        en_block = content.find("RCA_I18N.en")
        zh_section = content[zh_block:en_block]
        assert "'col.formation'" in zh_section
        assert "'col.group'" in zh_section
        assert "'col.formations'" not in zh_section

    def test_js_en_has_col_formation(self):
        """JS en section must have col.formation and col.group."""
        js_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..",
            "js", "i18n.js"
        )
        with open(js_path, encoding="utf-8") as f:
            content = f.read()
        en_block = content.find("RCA_I18N.en = {")
        ja_block = content.find("RCA_I18N.ja = {")
        en_section = content[en_block:ja_block]
        assert "'col.formation'" in en_section
        assert "'col.group'" in en_section
        assert "'col.formations'" not in en_section

    def test_js_ja_has_col_formation(self):
        """JS ja section must have col.formation and col.group."""
        js_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..",
            "js", "i18n.js"
        )
        with open(js_path, encoding="utf-8") as f:
            content = f.read()
        ja_block = content.find("RCA_I18N.ja = {")
        ja_section = content[ja_block:]
        assert "'col.formation'" in ja_section
        assert "'col.group'" in ja_section
        assert "'col.formations'" not in ja_section
