# Changelog

All notable changes to Range Chart Analyzer are recorded here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
this project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- **`occurrence_matrix` had no table configs in the browser.** `js/table.js`
  carried a `_looks_*` predicate for every mode except this one, so
  `rcaTableConfigs` fell through to the range-chart branch — which returns its
  four configs unconditionally. On a real 73-record result the browser produced
  **8** tables against the desktop's **3**: the three real ones plus four
  PHANTOM EMPTY ones (`sections` / `species_ranges` / `biozones` /
  `other_fossils`), and the real three arrived through the generic fallback with
  humanised, untranslatable headers. `rcaLooksOccurrenceMatrix` and
  `rcaOccurrenceMatrixConfigs` now mirror the Python twin, in the same branch
  position (above abundance, since a partially-migrated matrix answer carries
  abundance-shaped rows).
  *This was latent, not user-visible:* `js/minimax.js` refuses the mode inside
  `extractRangeChart` before the backend dispatch, the desktop renders through
  `exporter.py`, and the browser has no result-import path. Verified by
  execution, not by reading the guard. It was one commit from live, since
  flipping `browser_supported` to `True` is the documented way to enable it.
- **Ten i18n keys the occurrence-matrix tables referenced were never defined.**
  `Translator.t` returns an unknown key *verbatim*, so the desktop rendered
  `sec.occurrences (73)` and headers reading `col.authority` / `col.code` /
  `col.class` in a Chinese UI; the browser renders the same gap as `[?col.x]`.
  Added in zh/en/ja on both sides.
- **`col.index` meant two different things.** It is the ROW-NUMBER header
  (`build_table_export`'s `index_label`, and `js/table.js`'s
  `[t('col.index')].concat(cfg.cols)`) — and the GUI prepends its own `#`. The
  occurrence-matrix `columns` table also used it as a data column, so its header
  read `# | # | Label | Kind` in every language. The data column is now
  `col.columnIndex`.
- **Generic-table headers disagreed between engines.** `rcaColLabel` tested
  `t(key) !== key`, but `t()` renders a missing key as `[?col.topic]`, which is
  never equal to the key — so the test accepted *every* column and the browser
  headed an unrecognised column `col.topic` where the export wrote `Topic`. It
  now tests membership of the English table, exactly as `_col_label` does.

### Added

- **`tests/test_table_configs_parity.py`.** Cross-engine parity for the table
  configs themselves (ids and columns), which the harness never covered — it
  proved the two engines picked the same *branch* while nothing replayed
  `rcaTableConfigs` against `get_configs_for_result`. Pins the invariant that
  holds on every payload (the browser may add the four documented editing
  shells, but must never drop or reshape an exported table) separately from the
  one that holds only on specific branches.
- **occurrence-matrix shapes in `tests/test_table_config_detectors_parity.py`**,
  and the scope assertion no longer pins `len(PAIRS) == 5`. That count asserted
  the hand-written list against its own length instead of against the detectors
  the code actually has, which is precisely why a one-sided `_looks_*`
  addition passed unnoticed. The set is now enumerated from the exporter module
  and from what `js/table.js` really defines.

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
