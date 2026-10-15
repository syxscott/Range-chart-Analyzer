# Changelog

All notable changes to Range Chart Analyzer are recorded here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
this project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Nothing yet.

## [0.1.0] — 2026-10-15

First tagged release. The repository had no tags before this, so there is no
earlier version to compare against.

### Added

- **`occurrence_matrix` chart type.** Taxon × locality matrix figures now extract
  as a *sparse long table*: one record per FILLED cell, with a locality legend
  and the figure's non-taxon comparison band. Blank cells are dropped rather
  than emitted, and the drop is counted in the run's warnings.
- **`generic_table` chart type.** An honest fallback for tabular figures that
  match no known genre. Deliberately the opposite polarity to
  `occurrence_matrix`: dense, short rows padded to the column width, because a
  fallback must not invent sparsity it cannot see.
- **`rca_core/modes.py`.** A single declaration of all 10 chart modes, replacing
  mode strings that had been scattered across 9+ places in the Python core and 7
  in the browser. It is DATA ONLY with no sibling imports, so consumers resolve
  `"module:attr"` references lazily and the registry cannot form a cycle.
  Wiring it in was gated on 70 equivalence checks proving the derived values
  were byte-identical to the constants they replaced.
- **Packaging.** `RangeChartAnalyzer.spec` plus two one-file exes (window and
  console), published as Release assets.
- **PyInstaller exclude audits** (`scripts/audit_exclude_imports.py`,
  `scripts/audit_exclude_suite.py`), so the exclusion list stays verifiable
  instead of being a list someone trusted once.

### Changed

- `DEFAULT_MAX_TOKENS` 4000 → 16000, in the core and in `js/config.js`
  together. A 22 × 18 occurrence matrix needs 4417–5428 output tokens as one
  JSON record per filled cell; at 4000 the model was cut off mid-answer and the
  final row was never emitted, with `stop_reason == "max_tokens"` surfacing
  only as a warning string. `max_tokens` is a ceiling, not a reservation.
- exe size 681.5 MB → 308.5 MB, by excluding 27 packages the tool never calls
  (torch, polars, pyarrow, scipy, spacy, bitsandbytes, …). `QtWebEngine` is
  deliberately kept — `gui_fluent.py` embeds the web frontend with
  `QWebEngineView`, so it is not dead Chromium weight.
- Chart-mode autodetect reads its keyword tables from the registry rather than
  from 16 hand-maintained tuples in `chart_mode.py`.

### Fixed

- `_looks_occurrence_matrix` keyed off `columns`, which `generic_table` also
  emits — a generic table was silently exported as zero rows.
- The browser's quality detector disagreed with the core's about which mode a
  payload belonged to, because the branches were in a different order.
- An `or` chain short-circuited on the integer `0`, so a reading of `0` was
  treated as a blank cell, dropped, and reported as a false warning.
- A `col_index` that contradicted its `site` was accepted silently; it now
  raises `column_index_conflict` and marks the row uncertain.
- `modes.py` pointed `chemical_stratigraphy` at a normalizer that did not exist.
- The extraction prompt licensed name "correction": it asked for *standard
  Latin names*, which permits replacing what was read with what the model
  believes. It now says **transcribe the taxon name exactly as printed, letter
  by letter**, and to mark a row uncertain rather than guess.
- A contract clause saying "never delete the row" contradicted the sparse rule;
  replaced with one that applies only where the contract is dense.

### Known limitations

- On a dense 22 × 18 matrix the recall of filled cells is **51 %** (73 of 93
  measured against the figure's own grid). Sparse rows are essentially exact;
  the loss concentrates in the densest rows. It is not a pipeline defect —
  column mapping was verified against the figure by shift analysis, and
  upscaling the image made results worse, not better. The next step is tiled
  per-row extraction with a merge, which is not implemented.
- The genus printed `Capmuchosphaera` is read as `Capnuchosphaera`
  consistently. Small italic serif text at the figure's native resolution is
  the limit here.

[Unreleased]: https://github.com/syxscott/Range-chart-Analyzer/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/syxscott/Range-chart-Analyzer/releases/tag/v0.1.0
