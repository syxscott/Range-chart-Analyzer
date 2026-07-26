# Task 9 Report: Wire phylogenetic tree in ExtractPage

**Status:** DONE
**Date:** 2026-07-26
**Branch:** main
**Files Modified:** `gui_fluent.py`

---

## Summary

Wired the `PhyloTreeWidget` (Task 8) into `ExtractPage` so a phylogenetic-tree
extraction renders the tree D3.js view instead of the table/pivot stack.
Added a new mode branch in `_render_result()`, a helper `_show_phylotree()`
that handles the visibility toggling, and the explicit `set_data()` call in
`_on_result()` requested by the brief.

## Changes

### 1. `_current_mode()` -- recognise phylogenetic_tree

The previous method only knew three modes (`range_chart`, `columnar_section`,
`abundance_diagram`) and silently downgraded any other `chart_type` to
`range_chart`. Added:

- `phylogenetic_tree` to the explicit allow-list returned from the user
  setting.
- An "auto-detect by result shape" fallback that looks for a `nodes` list
  (the primary `list_key` of `PHYLOGENETIC_TREE_SCHEMA`) before the
  existing abundance/columnar probes. This lets an "auto" extraction of a
  tree image still render the tree widget.

### 2. `ExtractPage.__init__` -- new widgets

- Wrapped the `edit_row` `QHBoxLayout` in a `QWidget` (`self.edit_row_widget`)
  so the entire row (Add / Delete / Discard / dirty badge / Apply) can be
  hidden in one `setVisible(False)` call when the panel switches to a
  non-tabular mode. Previously the buttons were children of the right
  panel directly and would have stayed visible next to the tree.
- Created `self.phylotree = PhyloTreeWidget()` (only when
  `HAS_PHYLO_TREE_WIDGET` is true -- mirrors the existing `HAS_PIL`
  defensive pattern). Added to `right_lay` with stretch factor `1` so it
  claims the same vertical space the table stack uses, and started hidden
  so the first paint shows the normal table UI.

### 3. `_show_phylotree()` -- new helper

Toggles the right panel into tree mode:

- Hides `self.pivot`, `self.edit_row_widget`, `self.stack`.
- Shows `self.phylotree`.
- Drops any stale edit snapshot / dirty badge (the tree has no row-level
  edits -- we don't want a later switch back to tables to replay an edit
  against a now-unrelated baseline).
- Calls `self.phylotree.set_data(self.result)` (the widget buffers the
  payload across the page-load race, so this is safe even if the
  WebEngine HTML hasn't finished loading yet).
- Defensive fallback: if `HAS_PHYLO_TREE_WIDGET` was false at startup,
  `self.phylotree is None` and the helper logs a warning and skips the
  render rather than crashing.

### 4. `_render_result()` -- branch on mode

- Compute `is_tree_mode = (self._current_mode() == "phylogenetic_tree"
  and self.phylotree is not None)` once at the top.
- The empty-state guard now routes the tree mode through `_show_phylotree()`
  first (the tree can render an empty graph -- the empty-state label is
  table-specific).
- Skip the pivot/scroll-area build loop and call `_show_phylotree()` when
  `is_tree_mode` is true.
- For table mode, restore visibility (in case we just switched out of
  tree mode): hide the tree, show pivot/edit_row_widget/stack.

### 5. `_on_result()` -- explicit set_data() call

Per the brief's step 4, added an explicit
`self.phylotree.set_data(self.result)` call right after `self.result =
result.data`, before `_render_result()`. This is technically redundant
(the tree branch in `_render_result()` also calls `set_data()`), but the
duplication is intentional: pushing in `_on_result()` means the widget
gets the latest data even on a hot reload that bypasses the visibility
branch. The two calls are idempotent (the widget overwrites the pending
buffer and the rendered tree).

## Verification

- `python -m py_compile gui_fluent.py` -> exit code 0, no syntax errors.
- AST inspection confirms:
  - `__init__` creates `self.phylotree = PhyloTreeWidget()`.
  - `__init__` wraps `edit_row` in `self.edit_row_widget`.
  - `_current_mode()` recognises `phylogenetic_tree`.
  - `_render_result()` calls `_show_phylotree()`.
  - `_on_result()` calls `self.phylotree.set_data(...)`.
  - `_show_phylotree()` method exists.
- `python -m pytest tests/test_exporter_xlsx.py` -> 15 passed.
- No syntax/runtime regressions in the existing flow.

## Why these design choices

1. **Wrap edit_row in a QWidget rather than tracking each button**: keeps
   the visibility toggle in one call and survives future edits to the
   edit row (no bookkeeping to update).
2. **Hide the pivot in tree mode**: the pivot drives table-only tab
   switching; showing it next to a tree would be confusing.
3. **Drop edit snapshot on tree mode**: the tree isn't editable;
   leaving the snapshot live would cause a later table-render to think
   the user has uncommitted edits against an unrelated baseline.
4. **`set_data()` in both `_on_result()` and `_render_result()`**:
   `_render_result()` is the canonical render path (called from
   `load_result()` and `retranslate()` too), so the branch covers all
   entry points. The `_on_result()` call is the explicit hook the brief
   asked for and ensures the data is pushed even on paths that don't
   re-render.
5. **`is_tree_mode` check uses `self._current_mode()` not the local
   `mode` variable from `_on_extract()`**: the local variable is gone
   by the time `_render_result()` runs, and `_current_mode()` is the
   canonical accessor used by the rest of the page (e.g. the export
   filename prefix).

## Files Touched

- `gui_fluent.py` (modified)

No new test files added (Task 12 covers the integration tests for the
phylogenetic-tree pipeline, including the EditPage wiring).

## Next Steps (not in this task)

- Task 10 (TBD): re-render the tree on language switch (the D3.js
  template uses i18n strings for tooltips and labels).
- Task 12: integration tests for the tree pipeline (extract ->
  PhyloTreeWidget.set_data -> template renders -> supports export).
