# Changelog

All notable changes to Range Chart Analyzer are recorded here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
this project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.1] — 2026-10-16

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
- **The browser-unsupported guard ran BEFORE the backend dispatch.** Reproduced
  live: with the server running, connection mode 「自动」, and 出现矩阵 explicitly
  selected, Extract produced **ZERO network requests** and told the user to
  switch to 「经本地后端」 or start the server — advice already followed on all
  three counts. `extractRangeChart` refused before `rcaCallBackend` was ever
  reached, so the mode was unavailable in the browser even though the backend
  handles it perfectly well (`modes.py` `SERVER_MODE_NAMES`). The guard exists
  because the DIRECT path has no normalizer for those modes; on the backend path
  it was simply the wrong tool. It now sits after the dispatch and guards only
  what it is for. *This also corrects the claim in the entry above, which said
  the refusal happened before the backend dispatch — verified by execution, and
  the browser went on to prove it by sending the request.*
- **The guard was not re-checked after automatic classification.** It ran while
  `mode` was still the literal `auto` (not in `RCA_BROWSER_UNSUPPORTED_MODES`),
  and the classifier then rewrote `mode` to whatever it recognised. There is no
  prompt branch or normalizer below for `paleomap` / `scatter_plot` /
  `chemical_stratigraphy` / `occurrence_matrix`, so the request went out with
  `RANGE_CHART_SYSTEM_PROMPT` and the answer was normalized as a range chart —
  a confidently wrong result, reachable from the default path with no
  misconfiguration. Re-checked after the classification round, and the
  extraction request is refused rather than sent.
- **Cancelling left the results area blank.** `setBusy(true)` hides BOTH
  `#results-empty` and `#results-content`, and only the `!res.ok` branch put one
  back — so the four early returns around it (superseded extraction, cancelled
  twice) all returned to an empty card. With a prior result the user's data was
  still in memory and unreachable until they re-ran the extraction. The
  invariant is now stated once, in `updateActionButtons`, and every exit gets it
  because `setBusy(false)` routes through that function.
- **Extract could send the previous image while the new one was still
  decoding.** `handleFile` deliberately defers `state.file` / `state.dataUrl`
  until the decode resolves, so during that window both still describe the
  PREVIOUS image while the file input already shows the new one. Nothing keyed
  off the window — `updateActionButtons` looked only at `state.busy`, which
  `handleFile` never sets — so Extract and Ctrl+Enter were both live. Worse,
  the misattributed result SURVIVED: the load's token guard covers only the
  load, so once the new image arrived the rows rendered normally and every
  export named the new file. `state.loading` now locks the window.
- **`occurrence_matrix` had no merge schema in the browser.** Python has had
  `OCCURRENCE_MATRIX_SCHEMA` in `SCHEMA_BY_MODE` and `_auto_detect_schema`
  since 2026-10-15; `js/aggregate.js` mirrored neither, so a two-run matrix was
  merged with the range-chart schema's `species_ranges` primary — which this
  payload does not have. The consequence is data loss, not degradation: the
  merge produced nothing for the real rows, so a locality only the second run
  reported vanished and two localities of one genus collapsed into one record.
- **A `null` in a plain-string list became a phantom row.** `str(None)` is the
  literal `"None"` and `String(null)` is `"null"`, so one null in
  `other_fossils` produced a ghost taxon on the desktop AND a differently-spelled
  ghost in the browser. Both now emit nothing. The mixed-list branch had the
  guard on the JS side only.
- **A cancellation was retried like a transient failure.** The backoff loop
  caught every rejection alike, so pressing Cancel still went round it:
  measured through the real `extractRangeChart`, 4 fetch attempts and 4606 ms
  before the user's own command took effect, against 1 attempt and 303 ms for
  the control.
- **`in` walked the prototype chain when validating node ids.** `idToNode` is a
  plain `{}`, so `root_ids: ["toString"]` and a `parent` of `"constructor"`
  passed validation against keys the model chose, and the tree was built on a
  node that does not exist.
- **The occurrence sheet's title did not say where its cells came from.**
  `sec.occurrencesUnverified` existed in all three dictionaries with no
  consumer, so the browser headed identical data 「出现记录」 while the desktop
  exporter headed it 「出现记录 (格子未经图版网格核验)」. `cell_status` now drives
  the choice, defaulting to the CAVEATED title — absence is not proof, since
  merged results and every pre-field payload lack the key. Found by loading the
  real page: the diff-parity suite compares JS output against Python output, and
  a branch missing on BOTH sides compares equal.
- **`rca_core.__version__` read `2.0.0`** while the project shipped 0.1.0. It is
  not cosmetic: `exporter._package_version()` stamps it into the header of every
  CSV/TSV/JSON/XLSX exchange file, so every export claimed to come from a version
  of this software that has never existed. No test pinned it — nothing under
  `tests/` mentions `__version__` — which is how a number that was wrong from the
  start survived the whole release history. It now tracks the CHANGELOG.

### Added

- **`tests/test_occurrence_matrix_merge_parity_2026_10_16.py`.** The browser
  must merge occurrence matrices the way the desktop does. Two things make it
  real rather than decorative: it passes the keymap into `rcaMergeResults` the
  way `js/app.js:1318` does (that function does NOT auto-detect, while Python's
  `merge_results` does — comparing them without it compares a range-chart merge
  against an occurrence merge), and it compares the merged RESULT rather than
  the chosen primary, because two engines agreeing on a list name while writing
  different key sets is the divergence that costs data.
- **The `gated` payload shapes the occurrence detectors were missing.** Five
  shapes where `occurrences` is empty BY DESIGN and `taxon_rows` carries the
  names. Before them, every such shape classified as "not a matrix" on BOTH
  engines, so no shape in `tests/test_table_config_detectors_parity.py` could
  tell them apart and the one-sided fix exporter.py had just received passed
  that suite untouched.
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
- **`cell_status` on every occurrence-matrix result.** `single_pass_unverified` /
  `grid_cells` / `not_attempted` / `failed`. It records **where the cells came
  from**, not how confident they are, and it is taken from the caller's code path
  rather than from the model's payload — the model has no way to know whether its
  own column coordinates were checked against the figure's geometry. `single_pass`
  can never earn `grid_cells`, and a **missing** status means unverified, because
  `merge_results` drops scalar root keys and older payloads predate the field.
  The exporter turns it into the sheet title, so the caveat appears where a reader
  of the workbook looks first.
  *Why it exists:* on a real 22 × 18 matrix the labels scored 20/22 taxa, 7/8
  authorities, 11/11 conodont zones, while the cells scored 0.58 accuracy at 0.36
  coverage — and the result object looked identical in both cases.
- **An empty-response gate for `occurrence_matrix`.** A near-empty body is valid
  JSON, so it passed both the truncation check and the parser and reached the user
  as "this figure contains no occurrences". Two real runs measured 528 and 40
  output tokens where the same figure costs 4190–5587. Those are now rejected
  with the numbers in the message. The floor is **per strategy** and is `None`
  where it has not been measured: reusing the full-answer floor against a
  labels-only answer would reject correct output, and guessing one rejects good
  output silently.
- **`rca_core/grid.py`.** Deterministic ruling-line detection, so the model is
  not asked to locate cells by eye. Returns `None` rather than guessing —
  a borderless matrix is not exotic, and the caller degrades to the single-pass
  question and says so in `cell_status`. PIL + numpy only; `torch` and `scipy`
  were just excluded from the PyInstaller build.
- **`OCCURRENCE_LABELS_SYSTEM_PROMPT`.** A labels-only question (headers, taxon
  rows, legend) at **4546 characters against the matrix prompt's 7683 (−40.8 %)**.
  It *removes* the cell contract rather than adding rules to it, which is the
  opposite direction from the prompt change reverted earlier today. Mirrored to
  `js/prompt.js` with byte-equality guaranteed by construction.
- **`cell_strategy` on `extract_occurrence_matrix`.** `single_pass` (default,
  unchanged) / `gated` / `cells`. `cells` injects the locally measured pixel
  spans into the matrix prompt; `gated` uses the labels prompt and attempts no
  cell reading. `taxon_rows` carries the labels-only path's taxon names, which
  cannot ride in `occurrences` because that list is sparse by contract and drops a
  row with no code.
- **157 tests** across `tests/test_grid.py` (35),
  `tests/test_occurrence_cell_status_2026_10_16.py` (48) and
  `tests/test_occurrence_strategy_defaults_2026_10_16.py` (4), each proven by
  mutation: 16/16, 15/15 and 10/10 red.

### Changed

- **exe 308.5 MB → 257.9 MB (−16.4%), with no loss of function.** Measured on
  the 0.1.1 baseline rather than estimated: 839.1 MB of uncompressed payload
  over 4342 entries, read from `build/…/PKG-00.toc`. (Site-packages directory
  sizes over-count by up to 3× — torch is 1874 MB on disk and 98.5 MB inside the
  exe — so the 0.1.0 exclusion audit's inputs and this one's are not the same
  quantity.)

  Two entries, 148.9 MB of payload:

  - **87.4 MB of QtWebEngine DevTools bulk** —
    `qtwebengine_devtools_resources.debug.pak` (75.8) and
    `qtwebengine_devtools_resources.pak` (11.6). These load only when a
    profile is opened with DevTools enabled; nothing here calls
    `showDevTools()` or sets a DevTools attribute. PyInstaller's
    `collect_qtwebengine_files()` sweeps the whole `resources/` directory with
    no filter, so debug builds ride along in every bundle.
  - **58.9 MB of opencv ffmpeg video decoders** — `opencv_videoio_ffmpeg500_64`
    (30.9) and `ffmpeg4110` (28.0), for a tool that extracts from still images.
    `deskew.py` and `extractor.py` call `resize` / `GaussianBlur` / `Canny` /
    `HoughLinesP` / `fastNlMeansDenoising` / `createCLAHE` / `cvtColor`, all
    imgproc or photo. Verified by moving both DLLs aside and running every call
    site in a fresh interpreter: 10/10 passed, `import cv2` still succeeded,
    restored afterwards by digest. The existing exclusion audits cover Python
    imports and cannot see a binary dropped from `a.binaries`, which is why
    this one needed its own probe.

- **Corrected a false claim that had been protecting 204.8 MB.** The build spec
  and the 0.1.0 release notes both stated that dropping QtWebEngine "removes
  79.8 MB of Chromium and breaks the main content area", because
  `gui_fluent.py` "embeds the web frontend in the native window". It does not.
  `QWebEngineView` is used in exactly two places — `PhyloTreeWidget`, the D3
  phylogenetic-tree renderer, and the rich History detail dialog — and both
  already have absence paths: `HAS_PHYLO_TREE_WIDGET = False` leaves
  `self.phylotree` as `None` and `_show_phylotree()` logs and returns, and
  `HistoryDetailDialog` falls back to a read-only `QTreeWidget`. A false claim
  that something is mandatory is the most expensive kind of wrong, because it
  reads as a constraint and nobody revisits it.

  **0.1.1 keeps QtWebEngine.** Dropping it would save a further ~68 MB; that was
  put to the user and declined, so the 204.8 MB above is Chromium that is
  demonstrably *not* free to remove — recorded so the next person measures
  instead of citing 0.1.0's reasoning. The functional cost of dropping it would
  be: no interactive D3 tree (the right panel falls back to the empty state) and
  no rich History detail (a plain `QTreeWidget` instead).

- **UPX was considered and rejected on measurement, not taste.** PyInstaller's
  onefile archive is already zlib-compressed — 839.1 MB of payload shipped as
  323.5 MB, so 61% of the bytes were compressed before any outer tool ran. UPX
  over already-compressed data returns single-digit percent, so it was not worth
  adding a binary compressor and another build cycle to chase it. The lever that
  works here is payload, not compression ratio.

- **The truncation warning names its numbers.** It was copy-pasted identically
  into eleven extractors and reported no quantities at all, so a reader could not
  tell 16000 from 32000. That ambiguity had already been misread once — an
  `output_tokens=16058` run was taken as "the cap was hit, raising it will not
  help", when 16058 had only reached `DEFAULT_MAX_TOKENS` and `clamp_max_tokens`
  still allowed 32000. One shared helper now reports the actual usage, this run's
  limit, and the program ceiling.

### Known limitations

- **Filled cells are still unreliable, and the default strategy has not changed.**
  Measured once on the real 22 × 18 figure, same entry point and scorer:

  | | single_pass | gated | cells |
  |---|---|---|---|
  | output tokens | 5151 | 1103 | 6241 |
  | column headers | 9/15 | **12/15** | 11/15 |
  | taxa | 20/22 | 20/22 | 20/22 |
  | authorities | **7/8** | **7/8** | 6/8 |
  | conodont zones | **11/11** | **0/11** | 10/11 |
  | legend | 3 | **0** | 3 |
  | cell accuracy | 0.579 | — | 0.574 |
  | cell coverage | 0.355 | — | **0.565** |

  `gated` reads the most headers and costs a fifth of the tokens, but its schema
  has no `strat_rows` and it returned no zones and no legend — so it is **not** the
  default; `single_pass` keeps everything the others have. `cells`, with measured
  pixel spans injected, lifts correct-cell **coverage** 0.355 → 0.565 at flat
  accuracy, which matches the earlier shift-scan finding that the failure is
  column *identity* rather than reading ability. Offered as an opt-in: n=1 does not
  justify flipping a default, and 0.57 accuracy is not verification either way.
- **Tiled per-row extraction was tried and rejected**, so the "next step" recorded
  in 0.1.0 is not the way forward. Cutting the figure into 5-row strips with a
  repeated header reached full row coverage (22/22) but fabricated **four genera
  that do not appear in the figure** (`Archaeopyramisa engurensis`,
  `Archaeocathartes rectangularis`, and two more) — trading omission for
  hallucination, which is the worse failure for a table that gets cited.
  Upscaling was also measured and made cells *worse* (0.429 → 0.397 → 0.377 at
  1×/2×/3×) while improving only the labels.

### Verification

Python **3921 passed / 0 failed / 0 errors / 52 skipped**. Browser: 13 suites
green, `tests_frontend.js` at 825 assertions (from 760), differential parity
fixture at **1332 matched / 0 diverged**.

Every fix in this release was reverted in turn and required to turn its own
suite red — 8 mutations, 8 reds, files restored by content afterwards. The two
guards that could not have seen a one-sided change (differential parity compares
JS output against Python output, so a branch missing on BOTH sides compares
equal) were written as new cases instead: five `gated` payload shapes in the
detector suite, and a merge suite that passes the keymap the way `app.js` does
rather than the way neither engine's default does.

Browser, live: the exact state that produced **zero** network requests before
the fix now produces `GET 200` + `POST 200` on `/api/extract`, and the
occurrence sheet is headed 「出现记录 (格子未经图版网格核验)」 instead of the bare
「出现记录」.

Packaging: both exes were launched from this tag's source. The windowed build
opens a window titled `Range Chart Analyzer`; the console twin serves on a port
and answers `GET /` with 200. The shipped `js/` was read back *out of the
running exe* — `defaultMaxTokens = 16000`, `maxMaxTokens = 32000`,
`err.imageLoading`, `sec.taxonRows`, `data.cell_status`, the `taxon_rows`
conditional, and the post-classification guard were all confirmed present in
the binary rather than only in the repository.

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
