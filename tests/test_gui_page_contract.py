"""Desktop GUI structural contract — enforced WITHOUT PySide6.

AUDIT-2026-09-27 [item 0.3]. The GUI restructure planned for this wave moves
widgets between pages, splits ``save_all``, and inverts the chart-type data
source. Before the work, the desktop GUI had **zero** effective assertion
coverage in CI, so every one of those edits was unguarded:

* ``tests_gui_fluent.py``'s ``check()`` only printed and had no autouse
  fixture, so under pytest every soft assertion passed unconditionally —
  including all four page-objectName pins;
* its two halves were disjoint (``run_all()`` never called any ``test_*``,
  pytest never called ``run_all()``), so no path ran both;
* ``ci.yml`` installs no PySide6, so the Qt-gated tests skip silently.

This module closes the third gap. It is **pure source-text / AST** — no Qt
import, no window, no display — so the main CI matrix runs it and a
refactor that renames a pinned attribute fails the build immediately. The
runtime behaviour is covered separately by
``tests/test_gui_2026_09_27.py`` (Qt-gated) and the reworked
``tests_gui_fluent.py`` (real assertions now).

The five constraints pinned here are the ones whose violation silently breaks
other suites rather than this one:
  C1  ``_render_result`` name + the ``_table_widgets = {}`` line budget
      (``tests/test_table_widgets_memory.py`` is a regex over the source and
      is the ONLY desktop test CI runs today)
  C2  the four ``win.<page>`` attribute names (unguarded ``getattr``-free
      access in test_gui_sprint_b.py / test_gui_fluent_low_fixes.py)
  C3  the four page objectNames
  C4  ``ProviderCard``'s 3-positional constructor + ``set_cards`` +
      ``orderChanged`` (``tests_drag.py`` addresses cards by geometry)
  C5  the six action-button attribute names + ``_update_result_actions`` as
      the single enable/disable gate (``tests/test_fluent_ui_2026_09_05.py``
      asserts ``isEnabled()``)
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
GUI_FL = REPO / "gui_fluent.py"
GUI_PROV = REPO / "gui_fluent_providers.py"
GUI_PAGES = REPO / "gui_fluent_pages.py"


def _src(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _tree(path: Path) -> ast.Module:
    return ast.parse(_src(path))


def _class(tree: ast.Module, name: str) -> ast.ClassDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    raise AssertionError("class %s not found in %s" % (name, tree))


def _method(class_node: ast.ClassDef, name: str) -> ast.FunctionDef:
    for node in class_node.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == name:
            return node
    raise AssertionError("method %s.%s not found" % (class_node.name, name))


def _self_attrs(fn: ast.FunctionDef) -> set:
    """Every ``self.<name>`` assigned or defined inside *fn*."""
    out = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Attribute) and \
                isinstance(node.value, ast.Name) and node.value.id == "self":
            out.add(node.attr)
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and node.col_offset == 4:
                    out.add(tgt.id)
    return out


# ---------------------------------------------------------------------------
# C1 — _render_result: name, and the _table_widgets reset budget
# ---------------------------------------------------------------------------
_RENDER_BODY_BUDGET = 35      # tests/test_table_widgets_memory.py:90-98


def test_c1_render_result_name_and_reset_budget():
    """``self._table_widgets = {}`` must survive inside the first 35 lines of
    ``_render_result``.

    ``tests/test_table_widgets_memory.py`` greps the SOURCE TEXT of
    ``gui_fluent.py`` for that literal and asserts it appears within the
    first 35 lines after ``def _render_result(self):``. It is the only
    desktop-GUI test CI executes today (it needs no Qt), so a restructure
    that adds setup code above the reset fails it. This module asserts the
    same budget structurally, so the failure names the real cause instead of
    surfacing as an opaque regex miss in another file.
    """
    src = _src(GUI_FL)
    m = re.search(r"\n    def _render_result\(self\):(.*?)(?=\n    def |\nclass |\Z)",
                  src, re.DOTALL)
    assert m, "def _render_result(self) not found in gui_fluent.py"
    body = m.group(1)
    lines = body.splitlines()
    idx = next((i for i, ln in enumerate(lines)
                if re.search(r"self\._table_widgets\s*=\s*\{\}", ln)), None)
    assert idx is not None, \
        "_render_result must reset self._table_widgets = {} (line budget test)"
    assert idx < _RENDER_BODY_BUDGET, (
        "self._table_widgets = {{}} is at relative line {} but "
        "tests/test_table_widgets_memory.py requires it within the first {} "
        "lines of _render_result. Move the new code BELOW the reset."
        .format(idx + 1, _RENDER_BODY_BUDGET))
    # The old widgets must also be released, in the same method.
    assert "deleteLater()" in body, \
        "_render_result must deleteLater() the old _table_widgets"


def test_c1_render_result_keeps_table_bookkeeping_consistent():
    """The table bookkeeping must stay consistent across ``_render_result``'s
    four exits (tree mode, zero rows, tree-before-tables, normal tail).

    ``_render_result`` rebuilds ``self._table_widgets``; ``_show_table`` owns
    ``self._active_table_id``. If the widget map is emptied but the id is
    left pointing at a table that no longer exists, ``_current_table()`` would
    hand back a widget from a destroyed stack. So: the map is maintained in
    the re-render, and the reader is defensive about a missing/empty id.
    """
    tree = _tree(GUI_FL)
    cls = _class(tree, "ExtractPage")
    render_attrs = _self_attrs(_method(cls, "_render_result"))
    assert "_table_widgets" in render_attrs, \
        "_render_result must maintain self._table_widgets"

    reader = ast.unparse(_method(cls, "_current_table"))
    assert "_active_table_id" in reader, \
        "_current_table must consult _active_table_id"
    assert "if not self._active_table_id" in reader, (
        "_current_table must return nothing for an empty _active_table_id, so "
        "a re-render that empties _table_widgets cannot yield a dead widget")


# ---------------------------------------------------------------------------
# C2 / C3 — page attributes and objectNames
# ---------------------------------------------------------------------------
_PAGE_ATTRS = ("extract_page", "settings_page", "providers_page", "history_page")
_PAGE_OBJECTNAMES = {
    "extractPage": "extract_page",
    "providersPage": "providers_page",
    "settingsPage": "settings_page",
    "aboutPage": "about_page",
}


@pytest.mark.parametrize("attr", _PAGE_ATTRS)
def test_c2_window_page_attribute_names(attr):
    """``win.<page>`` names are accessed WITHOUT getattr in
    ``tests/test_gui_sprint_b.py`` and ``tests/test_gui_fluent_low_fixes.py``
    (teardown helpers reach into ``win.extract_page.phylotree``), so renaming
    any of them raises AttributeError there rather than failing a name
    assertion here. Pin them.
    """
    tree = _tree(GUI_FL)
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) \
                and node.value.id == "self" and node.attr == attr:
            found.add(attr)
    assert attr in found, \
        "RangeChartFluentWindow must keep self.%s (C2)" % attr


@pytest.mark.parametrize("objectname,attr", sorted(_PAGE_OBJECTNAMES.items()))
def test_c3_page_objectnames(objectname, attr):
    """The four page objectNames are asserted by ``tests_gui_fluent.py``.

    They also double as the runtime identity a ``QStackedWidget`` uses, so a
    rename is a behaviour change, not a cosmetic one.
    """
    found = False
    for path, holder in ((GUI_FL, None), (GUI_PROV, "ProvidersPage"),
                         (GUI_PAGES, None)):
        src = _src(path)
        for node in ast.walk(ast.parse(src)):
            classes = [node] if isinstance(node, ast.ClassDef) else []
            if holder and not any(
                    isinstance(n, ast.ClassDef) and n.name == holder
                    for n in classes):
                continue
            for n in ast.walk(node):
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                        and n.func.attr == "setObjectName" and n.args:
                    first = n.args[0]
                    if isinstance(first, ast.Constant) and first.value == objectname:
                        found = True
    assert found, 'setObjectName("%s") not found (C3)' % objectname


# ---------------------------------------------------------------------------
# C4 — ProviderCard shape
# ---------------------------------------------------------------------------
def test_c4_provider_card_positional_constructor():
    """``tests_drag.py`` builds ``ProviderCard(p, is_active, translate)`` with
    three POSITIONAL args and then addresses ``cards[0].geometry()`` for the
    drop target. Any change to the parameter list or to the parent/child
    structure of the card list breaks that suite in a way that looks like a
    drag bug.
    """
    fn = _method(_class(_tree(GUI_PROV), "ProviderCard"), "__init__")
    positional = [a.arg for a in fn.args.args if a.arg != "self"]
    assert positional[:3] == ["provider", "is_active", "translate"], (
        "ProviderCard(provider, is_active, translate) must keep its three "
        "positional parameters (C4); got %r" % (positional,))
    assert not fn.args.kwonlyargs, \
        "ProviderCard must not grow keyword-only args (C4)"


def test_c4_provider_drag_list_public_surface():
    """``set_cards()`` + the ``orderChanged`` signal are what ``tests_drag.py``
    drives, and the cards must stay direct geometry-addressable children.
    """
    cls = _class(_tree(GUI_PROV), "ProviderDragList")
    names = {n.name for n in cls.body if isinstance(n, ast.FunctionDef)}
    assert "set_cards" in names, "ProviderDragList.set_cards must survive (C4)"
    for node in ast.walk(cls):
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == "orderChanged":
                    return
    raise AssertionError(
        "ProviderDragList.orderChanged signal must survive (C4)")


# ---------------------------------------------------------------------------
# C5 — the six action buttons and the single gate
# ---------------------------------------------------------------------------
_ACTION_BUTTONS = ("btn_export", "btn_export_xlsx", "btn_add_row",
                   "btn_del_row", "btn_discard", "btn_apply_edits")


@pytest.mark.parametrize("attr", _ACTION_BUTTONS)
def test_c5_action_button_attribute_names(attr):
    """``tests/test_fluent_ui_2026_09_05.py`` asserts these six are disabled
    on a fresh page and enabled after a result. The attribute names are the
    contract; hiding vs disabling is a visibility concern and stays free.
    """
    cls = _class(_tree(GUI_FL), "ExtractPage")
    for m in cls.body:
        if isinstance(m, ast.FunctionDef):
            if attr in _self_attrs(m):
                return
    raise AssertionError("ExtractPage must keep self.%s (C5)" % attr)


def test_c5_update_result_actions_is_the_single_gate():
    """All enable/disable of the six action buttons must go through
    ``_update_result_actions`` so the busy / no-tables / no-image conditions
    cannot be bypassed by a second, partial update site.
    """
    cls = _class(_tree(GUI_FL), "ExtractPage")
    gate = _method(cls, "_update_result_actions")
    gated = set(_ACTION_BUTTONS) & _self_attrs(gate)
    assert gated, "_update_result_actions must gate the action buttons (C5)"

    # No OTHER method may call setEnabled on one of those buttons; if it does,
    # the state machine has a second source of truth.
    offenders = []
    for m in cls.body:
        if not isinstance(m, ast.FunctionDef) or m.name == "_update_result_actions":
            continue
        for node in ast.walk(m):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and node.func.attr == "setEnabled":
                tgt = node.func.value
                if isinstance(tgt, ast.Attribute) and \
                        tgt.attr in _ACTION_BUTTONS and \
                        isinstance(tgt.value, ast.Name) and tgt.value.id == "self":
                    offenders.append("%s (%s)" % (m.name, tgt.attr))
    assert not offenders, (
        "these set an action button outside _update_result_actions, which "
        "re-creates the split state machine this gate exists to remove: %s"
        % ", ".join(offenders))


def test_c5_edit_row_is_hidden_not_deleted():
    """The plan hides the edit row until a result exists. It must be hidden
    with ``setVisible(False)`` and stay parented — ``deleteLater()`` or
    reparenting would break the ``isEnabled()`` contract above and drop the
    operator's undo history on a re-render.
    """
    src = _src(GUI_FL)
    assert "edit_row_widget" in src
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == "deleteLater":
            tgt = node.func.value
            if isinstance(tgt, ast.Attribute) and tgt.attr == "edit_row_widget":
                raise AssertionError(
                    "edit_row_widget must be hidden, never deleteLater()'d")


# ---------------------------------------------------------------------------
# Load-result contract (C6) — the anti-"ghost image" invariant
# ---------------------------------------------------------------------------
def test_c6_load_result_clears_the_extractable_image_state():
    """``tests/test_gui_sprint_b.py`` pins that after ``load_result()``
    ``image_b64`` / ``media_type`` / ``image_path`` are None and
    ``_img_dims`` is ``(0, 0, False)`` — a partial wipe let the user press
    Extract and silently re-send the PREVIOUS image. The display-only
    thumbnail introduced by this wave must therefore use a SEPARATE
    attribute, never ``image_b64``.
    """
    cls = _class(_tree(GUI_FL), "ExtractPage")
    fn = _method(cls, "load_result")
    src = ast.unparse(fn)
    for attr in ("self.image_b64 = None", "self.media_type = None",
                 "self.image_path = None"):
        assert attr in src, f"load_result must still do `{attr}` (C6)"
    assert "self._img_dims = (0, 0, False)" in src, \
        "load_result must reset _img_dims (C6)"
    # A thumbnail kwarg may exist, but it must not alias the extractable one.
    assert "thumbnail_b64" not in src or "self.image_b64 = thumbnail" not in src, \
        "the history thumbnail must NOT be written into self.image_b64 (C6)"


def test_c6_load_result_accepts_a_thumbnail_keyword():
    """The review use-case needs the stored ``image_thumbnail`` to reach the
    preview. It must arrive as a keyword with a default so the existing
    positional call sites in the test suite keep working.
    """
    fn = _method(_class(_tree(GUI_FL), "ExtractPage"), "load_result")
    defaults = [a.arg for a in fn.args.args[len(fn.args.args) - len(fn.args.defaults):]]
    assert "thumbnail_b64" in defaults, (
        "load_result must accept `thumbnail_b64=None` so a stored history "
        "thumbnail can be displayed without touching image_b64 (C6); "
        "defaults are %r" % (defaults,))


# ---------------------------------------------------------------------------
# C7 — chart type / language accessor equivalence
# ---------------------------------------------------------------------------
def test_c7_code_tables_exist_and_are_indexed_defensively():
    """``chart_type()`` / ``chart_lang()`` must resolve a code from a table
    AND survive ``currentIndex() == -1``.

    ``SettingsPage.retranslate`` calls ``clear()`` before re-adding items,
    which drives the index to -1; indexing the code list with it raised or
    silently returned the wrong mode. This wave also moves the code tables
    to ExtractPage, so pin the shape rather than the location.
    """
    src = _src(GUI_FL)
    for name in ("chart_type", "chart_lang"):
        cls = _class(ast.parse(src), "RangeChartFluentWindow")
        fn = _method(cls, name)
        body = ast.unparse(fn)
        assert "currentIndex()" in body, f"{name}() must read the combo index"
        assert "if 0 <=" in body, (
            f"{name}() must bound-check currentIndex() — retranslate's clear() "
            "drives it to -1 (C7)")
    for table in ("_ctype_codes", "_clang_codes"):
        assert table in src, f"{table} must still exist (C7)"
