"""The cc-switch borrowings must survive a round trip, and must be real.

REVIEW-2026-10-20: nineteen things were taken from cc-switch's provider
UI -- a searchable model picker, the model metadata line, a full-URL
endpoint mode, an endpoint speed test, a JSON formatter, a note, a custom
icon, a profile, a collapsed advanced section, a full-screen wizard, a
quota chip on the card, the per-category counts, a profile filter, the
external-change guard, the usage query, the error boundary, the
environment banner and the what's-new note.

Two failure modes are covered here, and they are the two this codebase
keeps hitting:

1. A FIELD that the wizard's rebuild forgets. The wizard constructs a
   BRAND NEW ``LlmProvider`` from a hand-listed set of fields, so a field
   that is not listed is a field the user loses the moment they edit that
   provider -- with nothing on screen to explain why. The reasoning
   effort was exactly such a field once (REVIEW-2026-10-19), and the
   same argument applies to every field added here.

2. A CONTROL that does nothing. A quota chip that prints the USED share
   where the user reads it as the REMAINING share is worse than no chip
   at all, because the user acts on it.

The GUI parts need a display, so they run on Qt's offscreen platform and
skip CLEANLY where PySide6 is absent -- an ERROR there takes the whole
matrix job down with it. The file name carries "gui" so the ``qt`` job's
``-k "gui or fluent ..."`` filter picks it up.
"""
from __future__ import annotations

import json
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest                                                  # noqa: E402

from rca_core.llm import (LlmProvider, MODEL_METADATA,         # noqa: E402
                          model_metadata_for)


def _qt_available():
    """The same gate the other GUI suites use."""
    try:
        import PySide6            # noqa: F401
        import qfluentwidgets     # noqa: F401
        return True
    except Exception:
        return False


_requires_qt = pytest.mark.skipif(
    not _qt_available(), reason="PySide6/qfluentwidgets unavailable")


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


# ---------------------------------------------------------------------------
# Core: the new fields are FIELDS of the record
# ---------------------------------------------------------------------------

def test_the_new_fields_survive_the_store_round_trip():
    """A field that is not persisted is a field the user loses."""
    p = LlmProvider(name="Relay", endpoint="https://x.example",
                    notes="monthly, expires 2027-03", icon="R",
                    profile="work", full_url=True,
                    custom_endpoints=["https://y.example"])
    back = LlmProvider.from_dict(p.to_dict())
    assert back.notes == "monthly, expires 2027-03"
    assert back.icon == "R"
    assert back.profile == "work"
    assert back.full_url is True
    assert back.custom_endpoints == ["https://y.example"]


def test_the_custom_icon_has_a_default():
    """An empty icon means "derive it from the name"."""
    assert LlmProvider(name="MiniMax").icon == ""


def test_the_model_metadata_note_is_an_i18n_key_not_a_sentence():
    """A Chinese note in an English interface is a defect.

    The catalogue lives in the core, which has no translator, so the note
    carries a KEY and the GUI translates it. A prose note would have to be
    translated in three places and would silently be wrong in two of them.
    """
    from rca_core.i18n import TRANSLATIONS
    for name, meta in MODEL_METADATA.items():
        if not meta.note:
            continue
        assert "." in meta.note, (
            "%s's note %r is prose, not a translation key" % (name, meta.note))
        for lang in ("zh", "en", "ja"):
            assert meta.note in TRANSLATIONS[lang], (
                "%s has no %s translation" % (meta.note, lang))


def test_the_model_ceiling_is_the_measured_one():
    """524288 is MEASURED: the endpoint rejects a larger max_tokens."""
    meta = model_metadata_for("MiniMax-M3.1-Flash")
    assert meta is not None
    assert meta.max_output == 524288
    assert meta.context_window == 1000000


# ---------------------------------------------------------------------------
# Core: the environment check (no Qt needed)
# ---------------------------------------------------------------------------

def test_no_provider_is_an_environment_warning():
    from gui_fluent_diagnostics import environment_warnings
    assert "app.envNoProvider" in environment_warnings(
        provider_count=0, current_count=0, env={})


def test_a_provider_that_is_not_active_is_still_a_warning():
    from gui_fluent_diagnostics import environment_warnings
    assert "app.envNoProvider" in environment_warnings(
        provider_count=2, current_count=0, env={})


def test_a_healthy_setup_has_no_warning():
    from gui_fluent_diagnostics import environment_warnings
    assert environment_warnings(
        provider_count=2, current_count=1, env={}) == []


def test_a_proxy_is_a_warning():
    """A local endpoint behind a proxy is a connection error, not a
    dead server -- and it looks exactly like one."""
    from gui_fluent_diagnostics import environment_warnings
    warnings = environment_warnings(
        provider_count=1, current_count=1,
        env={"HTTP_PROXY": "http://127.0.0.1:7890"})
    assert "app.envProxy" in warnings


def test_the_environment_check_is_quiet_without_a_proxy():
    from gui_fluent_diagnostics import environment_warnings
    assert "app.envProxy" not in environment_warnings(
        provider_count=1, current_count=1, env={"HTTP_PROXY": "  "})


# ---------------------------------------------------------------------------
# Core: the what's-new note
# ---------------------------------------------------------------------------

def test_the_changelog_parser_finds_the_unreleased_entries():
    """The note on the first launch after an upgrade is read out of the
    CHANGELOG, so it cannot drift from it."""
    from gui_fluent_onboarding import changelog_entries_since
    entries = changelog_entries_since("0.1.0")
    assert entries, "the changelog parser found nothing"
    assert all(isinstance(e, str) and e.strip() for e in entries)
    # A title, not a paragraph.
    assert max(len(e) for e in entries) < 200


def test_the_changelog_parser_is_version_ordered():
    from gui_fluent_onboarding import changelog_entries_since
    newer = changelog_entries_since("0.1.0")
    oldest = changelog_entries_since("0.1.2")
    assert len(newer) >= len(oldest)


def test_a_missing_changelog_yields_no_entries(monkeypatch):
    """A packaged build may not ship the CHANGELOG; that is not a crash."""
    import gui_fluent_onboarding as g
    monkeypatch.setattr(g, "changelog_path",
                        lambda: os.path.join("no", "such", "CHANGELOG.md"))
    assert g.changelog_entries_since("0.1.0") == []


def test_the_unreleased_section_counts_as_newer_than_every_release():
    """The unreleased section IS the next version's changelog."""
    from gui_fluent_onboarding import changelog_entries_since
    assert changelog_entries_since("99.0.0"), \
        "the unreleased section must count as newer than any release"


# ---------------------------------------------------------------------------
# GUI: the wizard
# ---------------------------------------------------------------------------

def _wizard(qapp, existing=None):
    from gui_fluent_providers import ProviderWizard
    return ProviderWizard(None, lambda key: key, existing=existing)


def _full_provider():
    return LlmProvider(
        id="p1", name="MiniMax", api_format="anthropic",
        endpoint="https://api.minimaxi.com/anthropic", api_key="sk-test",
        model="MiniMax-M3.1-Flash", reasoning_effort="low",
        notes="monthly, expires 2027-03", icon="M", profile="work",
        full_url=True, custom_endpoints=["https://alt.example"],
    )


@_requires_qt
def test_the_new_fields_survive_an_edit(qapp):
    """The rebuild must carry every field over."""
    w = _wizard(qapp, _full_provider())
    try:
        w._on_ok()
        p = w.created_provider
        assert p is not None, "the wizard refused to save"
        assert p.notes == "monthly, expires 2027-03"
        assert p.icon == "M"
        assert p.profile == "work"
        assert p.full_url is True
        assert p.custom_endpoints == ["https://alt.example"]
        # The field that was already there must still be there.
        assert p.reasoning_effort == "low"
    finally:
        w.close()


@_requires_qt
def test_the_existing_fields_are_shown_when_editing(qapp):
    """A field that is saved but not DISPLAYED reads as empty, and the
    user retypes what was already there."""
    w = _wizard(qapp, _full_provider())
    try:
        assert w._ipt_notes.text() == "monthly, expires 2027-03"
        assert w._ipt_icon.text() == "M"
        assert w._ipt_profile.text() == "work"
        assert w._chk_full_url.isChecked() is True
    finally:
        w.close()


@_requires_qt
def test_the_advanced_section_opens_when_it_holds_something(qapp):
    w = _wizard(qapp, _full_provider())
    try:
        assert w.advanced_open is True, \
            "a provider with a note must show the note, or the user " \
            "retypes what was already saved"
    finally:
        w.close()


@_requires_qt
def test_the_advanced_section_starts_closed(qapp):
    w = _wizard(qapp, LlmProvider(name="x", model="MiniMax-M3.1-Flash"))
    try:
        assert w.advanced_open is False
        w._toggle_advanced()
        assert w.advanced_open is True
        w._toggle_advanced()
        assert w.advanced_open is False
    finally:
        w.close()


@_requires_qt
def test_the_fetched_models_are_all_offered(qapp):
    """A freshly fetched list narrowed by the model already in the field
    would show ONE entry for a provider that serves twenty."""
    w = _wizard(qapp, LlmProvider(name="x", model="MiniMax-M3.1-Flash"))
    try:
        w._on_models_fetched(None, ["MiniMax-M3.1-Flash", "MiniMax-M3",
                                    "MiniMax-M2.1"])
        items = [w._cmb_models.itemText(i)
                 for i in range(w._cmb_models.count())]
        assert items == ["MiniMax-M3.1-Flash", "MiniMax-M3", "MiniMax-M2.1"]
    finally:
        w.close()


@_requires_qt
def test_picking_a_model_fills_the_field_and_keeps_the_list(qapp):
    w = _wizard(qapp, LlmProvider(name="x"))
    try:
        w._on_models_fetched(None, ["MiniMax-M3.1-Flash", "MiniMax-M3"])
        w._cmb_models.setCurrentText("MiniMax-M3")
        assert w._ipt_model.text() == "MiniMax-M3"
        # The list must not collapse to the picked entry: the user is
        # comparing models, and a one-shot picker is not a picker.
        assert w._cmb_models.count() == 2
    finally:
        w.close()


@_requires_qt
def test_typing_narrows_the_model_list(qapp):
    w = _wizard(qapp, LlmProvider(name="x"))
    try:
        w._on_models_fetched(None, ["MiniMax-M3.1-Flash", "MiniMax-M3",
                                    "MiniMax-M2.1"])
        w._ipt_model.setText("M2")
        items = [w._cmb_models.itemText(i)
                 for i in range(w._cmb_models.count())]
        assert items == ["MiniMax-M2.1"]
    finally:
        w.close()


@_requires_qt
def test_the_json_formatter_reindents(qapp):
    w = _wizard(qapp, LlmProvider(name="x"))
    try:
        w._ipt_extra_headers.setText('{"b":2,"a":1}')
        w._format_extra_headers()
        text = w._ipt_extra_headers.text()
        assert "\n" in text, "the JSON was not re-indented"
        assert json.loads(text) == {"a": 1, "b": 2}
    finally:
        w.close()


@_requires_qt
def test_the_json_formatter_leaves_broken_json_alone(qapp):
    """Reformatting must never DESTROY what the user typed."""
    w = _wizard(qapp, LlmProvider(name="x"))
    try:
        w._ipt_extra_headers.setText("{not json")
        w._format_extra_headers()
        assert w._ipt_extra_headers.text() == "{not json"
    finally:
        w.close()


@_requires_qt
def test_the_model_metadata_is_shown(qapp):
    """The measured ceiling belongs on screen: typing 1000000 is a 400."""
    from rca_core.i18n import Translator
    from gui_fluent_providers import ProviderWizard
    w = ProviderWizard(None, Translator("en").t,
                       existing=LlmProvider(name="x",
                                           model="MiniMax-M3.1-Flash"))
    try:
        text = w._lbl_model_meta.text()
        assert "524,288" in text, (
            "the measured max-output ceiling is not on screen: %r" % text)
    finally:
        w.close()


@_requires_qt
def test_the_preset_switch_asks_before_overwriting(qapp):
    """A preset replaces what the user typed; the two look the same in
    the UI and only one of them destroys work."""
    import gui_fluent_providers as gfp
    from gui_fluent_providers import ProviderWizard

    asked = {"n": 0}

    class _FakeBox:
        def __init__(self, *a, **k):
            asked["n"] += 1

        def exec(self):
            return False       # the user says no

    w = ProviderWizard(None, lambda key: key)
    try:
        w._ipt_endpoint.setText("https://typed.example")
        orig = gfp.MessageBox
        gfp.MessageBox = _FakeBox
        try:
            from rca_core import PROVIDER_PRESETS
            w._pick_preset(PROVIDER_PRESETS[0])
        finally:
            gfp.MessageBox = orig
        assert asked["n"] == 1, "the switch was not confirmed"
        assert w._ipt_endpoint.text() == "https://typed.example", \
            "the preset overwrote the typed endpoint even though the " \
            "user said no"
    finally:
        w.close()


# ---------------------------------------------------------------------------
# GUI: the card
# ---------------------------------------------------------------------------

@_requires_qt
def test_the_quota_chip_shows_what_is_LEFT(qapp):
    """The API reports the USED share. A chip that says "1% used" where
    the user reads "1% left" is a sign error the user acts on."""
    from rca_core.i18n import Translator
    from rca_core.llm import QuotaResult, QuotaTier
    from gui_fluent_providers import ProviderCard
    card = ProviderCard(LlmProvider(name="MiniMax", endpoint="https://x"),
                         False, Translator("en").t)
    try:
        card.set_quota(QuotaResult(ok=True, tiers=[
            QuotaTier(name="five_hour", used_percent=1.0),
            QuotaTier(name="weekly_limit", used_percent=18.0),
        ]))
        text = card.lbl_quota.text()
        assert "99" in text, "the 5-hour window should read 99 left: " + text
        assert "82" in text, "the weekly window should read 82 left: " + text
        assert "1%" not in text, "the USED share was shown as the left one"
    finally:
        card.deleteLater()


@_requires_qt
def test_a_provider_with_no_quota_shows_nothing(qapp):
    """No quota is not an error; there is simply nothing to say."""
    from rca_core.llm import QuotaResult
    from gui_fluent_providers import ProviderCard
    card = ProviderCard(LlmProvider(name="x", endpoint="https://x"),
                         False, lambda key: key)
    try:
        card.set_quota(QuotaResult(ok=False, error="no quota"))
        assert card.lbl_quota.isVisible() is False
    finally:
        card.deleteLater()


@_requires_qt
def test_the_custom_icon_is_used(qapp):
    from gui_fluent_providers import ProviderCard
    card = ProviderCard(LlmProvider(name="MiniMax", endpoint="https://x",
                                     icon="M"), False, lambda key: key)
    try:
        assert card.icon_tile.text() == "M"
    finally:
        card.deleteLater()


@_requires_qt
def test_without_a_custom_icon_the_name_is_used(qapp):
    """Without a custom glyph the tile falls back to the name's letters."""
    from gui_fluent_providers import ProviderCard, _icon_char
    card = ProviderCard(LlmProvider(name="MiniMax", endpoint="https://x"),
                         False, lambda key: key)
    try:
        assert card.icon_tile.text() == _icon_char("MiniMax")
        assert card.icon_tile.text() != ""
    finally:
        card.deleteLater()


@_requires_qt
def test_the_note_is_on_the_card(qapp):
    from gui_fluent_providers import ProviderCard
    card = ProviderCard(LlmProvider(name="x", endpoint="https://x",
                                     notes="monthly plan"),
                         False, lambda key: key)
    try:
        assert card.lbl_note.text() == "monthly plan"
    finally:
        card.deleteLater()


# ---------------------------------------------------------------------------
# The CI gate that this round tripped over
# ---------------------------------------------------------------------------

def test_optional_is_imported_in_the_llm_module():
    """``Optional`` is used in the quota catalogue's annotations.

    ``from __future__ import annotations`` means the annotations are not
    evaluated at import time, so the missing import did NOT break the
    tests -- it broke the CI lint gate (ruff's F821), and it would break
    ``typing.get_type_hints()`` for anything that introspects the
    dataclass.
    """
    import rca_core.llm as llm
    assert hasattr(llm, "Optional"), \
        "llm.py uses Optional in annotations without importing it"
    import typing
    hints = typing.get_type_hints(llm.QuotaTier)
    assert hints["resets_at"] == typing.Optional[int]
