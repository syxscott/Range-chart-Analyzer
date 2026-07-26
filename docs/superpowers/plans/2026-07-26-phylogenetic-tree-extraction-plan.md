# Phylogenetic Tree Extraction - Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans.

**Goal:** Extract structured JSON from radiolarian phylogenetic tree images and render as interactive D3.js tree in GUI.

**Architecture:** New extract_phylogenetic_tree() as fourth extraction mode; GUI adds D3.js + WebEngineView tree renderer, fully independent of existing table rendering code.

**Tech Stack:** PySide6 + qfluentwidgets (GUI), D3.js v7 (tree), PySide6 WebEngineView (embed), rca_core/extractor.py + llm.py (backend)

---

## Global Constraints

- Do NOT modify any existing function signatures
- progress_callback defaults to None, all existing callers unaffected
- _MODE_DISPATCH: add only new entries
- Tree renderer fully independent from _render_result()
- All translation keys added new, no existing keys modified

---

## Task Map

| # | Task | Files |
|---|---|---|
| 1 | Write PHYLOGENETIC_TREE_SYSTEM_PROMPT | rca_core/prompt.py |
| 2 | Add PHYLOGENETIC_TREE_SCHEMA | rca_core/aggregate.py |
| 3 | Implement extract_phylogenetic_tree() | rca_core/extractor.py |
| 4 | Register phylogenetic_tree in _MODE_DISPATCH | rca_core/extractor.py |
| 5 | Verify progress_callback wiring | gui_fluent.py |
| 6 | Add chart type dropdown option | gui_fluent.py + i18n.py |
| 7 | Write D3.js tree HTML template | rca_core/resources/phylogenetic_tree.html |
| 8 | Implement PhyloTreeWidget | gui_fluent.py |
| 9 | Wire phylogenetic tree in ExtractPage | gui_fluent.py |
| 10 | Add phylogenetic tree JSON export | gui_fluent.py |
| 11 | Add translation keys | rca_core/i18n.py |
| 12 | Integration test | tests/test_phylogenetic_tree.py |

---

## Task 1: Write PHYLOGENETIC_TREE_SYSTEM_PROMPT

**Files:** Modify: rca_core/prompt.py

At the end of rca_core/prompt.py, add the PHYLOGENETIC_TREE_SYSTEM_PROMPT string constant. The prompt instructs the model to output JSON matching the schema: version, metadata, nodes[] with id/name/support/support_confidence/branch_length/depth_range_m/depth_confidence/sequence_count/is_leaf/parent, root_ids, and legend.

- [ ] Step 1: Add PHYLOGENETIC_TREE_SYSTEM_PROMPT to rca_core/prompt.py
- [ ] Step 2: Run: python -m py_compile rca_core/prompt.py
- [ ] Step 3: Commit

---

## Task 2: Add PHYLOGENETIC_TREE_SCHEMA to aggregate.py

**Files:** Modify: rca_core/aggregate.py

Add PHYLOGENETIC_TREE_SCHEMA as a MergeSchema instance:
  primary_list_key = 'nodes'
  primary_id_keys = ['id']
  primary_str_mode_fields = ['name']
  extra_keys = ['metadata', 'root_ids', 'legend']

Add to SCHEMA_BY_MODE: 'phylogenetic_tree': PHYLOGENETIC_TREE_SCHEMA

- [ ] Step 1: Add PHYLOGENETIC_TREE_SCHEMA to rca_core/aggregate.py
- [ ] Step 2: Add phylogenetic_tree to SCHEMA_BY_MODE
- [ ] Step 3: Run: python -m py_compile rca_core/aggregate.py
- [ ] Step 4: Commit

---

## Task 3: Implement extract_phylogenetic_tree()

**Files:** Modify: rca_core/extractor.py

Add extract_phylogenetic_tree() after extract_abundance_diagram(). Same never-raises contract as other extractors. Returns ExtractResult(ok, data, raw, truncated, usage, latency_ms).
Key steps: import PHYLOGENETIC_TREE_SYSTEM_PROMPT, build provider, call call_llm_api with progress_callback, parse JSON, return result.

- [ ] Step 1: Add extract_phylogenetic_tree() to rca_core/extractor.py
- [ ] Step 2: Run: python -m py_compile rca_core/extractor.py
- [ ] Step 3: Commit

---

## Task 4: Register phylogenetic_tree in _MODE_DISPATCH

**Files:** Modify: rca_core/extractor.py

In _MODE_DISPATCH dict, add: 'phylogenetic_tree': extract_phylogenetic_tree,

- [ ] Step 1: Update _MODE_DISPATCH
- [ ] Step 2: Run: python -c 'from rca_core.extractor import extract; print("OK")'
- [ ] Step 3: Commit

---

## Task 5: Verify progress_callback wiring

**Files:** Verify: gui_fluent.py

In ExtractWorker.run(), progress_callback is already added to params before the concurrent futures loop. Verify this is still correct after all changes.

- [ ] Step 1: Verify ExtractWorker.run() passes progress_callback
- [ ] Step 2: Commit if any change needed

---

## Task 6: Add chart type option to dropdown

**Files:** Modify: gui_fluent.py, rca_core/i18n.py

In gui_fluent.py _ctype_codes and _ctype_keys arrays, add 'phylogenetic_tree' and 'settings.chartType.phylogeneticTree'.
In i18n.py all three language sections, add the translation for settings.chartType.phylogeneticTree.

- [ ] Step 1: Update dropdown arrays in gui_fluent.py
- [ ] Step 2: Add translation keys in i18n.py (zh/en/ja)
- [ ] Step 3: Run: python -m py_compile gui_fluent.py rca_core/i18n.py
- [ ] Step 4: Commit

---

## Task 7: Write D3.js tree HTML template

**Files:** Create: rca_core/resources/phylogenetic_tree.html

Self-contained HTML + D3.js v7 tree renderer.
Requirements:
- Load D3.js v7 from CDN (https://d3js.org/d3.v7.min.js)
- Expose window.setTreeData(jsonData) function
- Use d3.tree() layout
- Color nodes by depth_range_m mapping (epipelagic/mesopelagic/bathypelagic)
- Size nodes by sequence_count
- Show support values on nodes (red if below 70)
- Hover tooltip with full node info
- Export SVG button

- [ ] Step 1: Create rca_core/resources/ directory
- [ ] Step 2: Write complete phylogenetic_tree.html
- [ ] Step 3: Test in browser with sample data
- [ ] Step 4: Commit

---

## Task 8: Implement PhyloTreeWidget

**Files:** Modify: gui_fluent.py

Add PhyloTreeWidget class wrapping QWebEngineView.
Loads the HTML template from rca_core/resources/phylogenetic_tree.html.
Exposes set_data(data) method that calls window.setTreeData() via runJavaScript().

- [ ] Step 1: Add PhyloTreeWidget class to gui_fluent.py
- [ ] Step 2: Import QWebEngineView and QWebEngineSettings
- [ ] Step 3: Run: python -m py_compile gui_fluent.py
- [ ] Step 4: Commit

---

## Task 9: Wire phylogenetic tree in ExtractPage

**Files:** Modify: gui_fluent.py

In ExtractPage.__init__: add self.phylotree = PhyloTreeWidget() instance.
In _render_result(): detect phylogenetic_tree mode, hide pivot/table, show PhyloTreeWidget.
Call self.phylotree.set_data(self.result) on successful result.

- [ ] Step 1: Add PhyloTreeWidget to ExtractPage.__init__
- [ ] Step 2: Update _render_result() to branch on phylogenetic_tree mode
- [ ] Step 3: Run: python -m py_compile gui_fluent.py
- [ ] Step 4: Commit

---

## Task 10: Add phylogenetic tree JSON export

**Files:** Modify: gui_fluent.py

In _export_json(): add branch for phylogenetic_tree mode, export self.result as formatted JSON, filename phylogenetic_tree_<timestamp>.json

- [ ] Step 1: Add export branch
- [ ] Step 2: Run: python -m py_compile gui_fluent.py
- [ ] Step 3: Commit

---

## Task 11: Add translation keys

**Files:** Modify: rca_core/i18n.py

Add in all three language sections (zh/en/ja):
- settings.chartType.phylogeneticTree (done in Task 6)
- results.phylogeneticTree: label for tree results
- status.phyloParsing: parsing status message

- [ ] Step 1: Add remaining translation keys
- [ ] Step 2: Run: python -m py_compile rca_core/i18n.py
- [ ] Step 3: Commit

---

## Task 12: Integration test

**Files:** Create: tests/test_phylogenetic_tree.py

Test extract_phylogenetic_tree() returns valid schema (nodes, metadata, root_ids, legend).
Test progress_callback is invoked with stage strings.
Test merge_results() with PHYLOGENETIC_TREE_SCHEMA.

- [ ] Step 1: Create tests/test_phylogenetic_tree.py
- [ ] Step 2: Write tests for schema validation
- [ ] Step 3: Run: pytest tests/test_phylogenetic_tree.py -v
- [ ] Step 4: Commit