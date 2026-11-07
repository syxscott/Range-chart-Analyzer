"""Mode normalizer: occurrence_matrix.

Phase 3 / P1-4: bodies MOVED VERBATIM out of ``rca_core/extractor.py``.
Nothing was rewritten. Shared helpers come from ``modes_impl._shared`` (a
leaf module); ``extractor.py`` re-exports these names at the end so every
existing import path and monkeypatch target is unchanged.
"""
from __future__ import annotations
from ._shared import (
    DEFAULT_OCCURRENCE_CELL_STATUS,
    OCCURRENCE_CELL_STATUSES,
    _CONTRACT_SOURCE_KEYS,
    _carry_extras,
    _confidence_clamped,
    _stringify_scalar,
    attach_row_contract,
)
from typing import Any
import re

def normalize_occurrence_matrix_result(
        parsed: dict[str, Any],
        cell_status: str = DEFAULT_OCCURRENCE_CELL_STATUS) -> dict[str, Any]:
    """Coerce the parsed taxon x LOCALITY matrix into the strict result shape.

    Shape: ``columns`` (the localities, left to right), ``legend``, a SPARSE
    ``occurrences`` long table - one row per FILLED cell - and ``cell_status``,
    which records HOW the cells were obtained (see
    ``OCCURRENCE_CELL_STATUSES``).

    ``cell_status`` is an argument, not something read off ``parsed``. The model
    has no way to know whether its own cell coordinates were checked against
    the figure's geometry, so letting it supply the field would mean the field
    says whatever the prompt hoped for. The caller passes the status its own
    code path earned.

    SPARSITY IS THE CONTRACT. The step-0 baseline run (a real occurrence matrix
    through the real model, asked to answer as an abundance_diagram) came back
    with 63 rows where roughly 60 were real: the model emitted a row for every
    blank cell too, with an empty ``abundance``. Those rows are not neutral -
    each one claims "this taxon occurs at this locality", which is exactly the
    assertion a systematic section gets cited for, and it inflates the record
    count until the real occurrences are indistinguishable from padding. A
    blank cell here means ABSENT, and absence is already recoverable: it is
    every cell that was NOT emitted. So blank rows are DROPPED here rather than
    passed downstream.

    The second thing this fixes, from the same baseline run: the rotated
    column headers came back as taxa and the row labels as localities, which
    produces a complete, well-formed, entirely wrong table. The header list is
    therefore authoritative - a row whose ``site`` matches no column label is
    kept but marked ``uncertain`` with ``unknown_column``, instead of being
    silently accepted into a mis-transposed table.

    ``row_kind`` keeps a stratigraphic comparison band ("Conodont level (see
    text)") out of the taxon rows: it is real data about the figure, but it is
    not a genus, and letting it into ``occurrences`` would pollute every
    taxon-level statistic computed from this table.
    """
    if not isinstance(parsed, dict):
        return {"columns": [], "legend": [], "occurrences": [],
                "strat_rows": [], "taxon_rows": [], "confidence": 0.0,
                "cell_status": cell_status}

    # H3-fix parity: a top-level array arrives wrapped as {"_array_root": [...]}.
    if "_array_root" in parsed and isinstance(parsed["_array_root"], list):
        for item in parsed["_array_root"]:
            if not isinstance(item, dict):
                continue
            if "label" in item and "index" in item and "cells" not in item:
                parsed.setdefault("columns", []).append(item)
            elif "taxon" in item or "site" in item:
                parsed.setdefault("occurrences", []).append(item)
            else:
                parsed.setdefault("_unclassified", []).append(item)

    def s(v: Any) -> str:
        return _stringify_scalar(v)

    warnings: list[str] = []
    out: dict[str, Any] = {
        "columns": [],
        "legend": [],
        "occurrences": [],
        "strat_rows": [],
        "taxon_rows": [],
        "confidence": 0.0,
        "cell_status": (cell_status if cell_status in OCCURRENCE_CELL_STATUSES
                        else DEFAULT_OCCURRENCE_CELL_STATUS),
    }

    # --- columns: the authoritative locality list -------------------------
    labels: list[str] = []
    for i, col in enumerate(parsed.get("columns") or []):
        if not isinstance(col, dict):
            continue
        label = s(col.get("label") or col.get("name") or "").strip()
        if not label:
            # A column with no header cannot be cited, and a row pointing at it
            # would have no verifiable identity. Keep the position, drop the
            # name, and say so.
            warnings.append(f"column {i} has no readable header; dropped")
            continue
        raw_index = col.get("index")
        try:
            index = int(raw_index)
        except (TypeError, ValueError):
            index = i
        labels.append(label)
        out["columns"].append({
            "index": index,
            "label": label,
            "kind": s(col.get("kind") or "locality").strip() or "locality",
        })

    label_set = set(labels)
    # Case/space-insensitive fallback: the same locality can be transcribed with
    # different spacing by different rows of one answer.
    norm_labels = {re.sub(r"\s+", " ", lb).casefold(): lb for lb in labels}

    # --- legend: code -> class, read from the figure, never assumed --------
    legend_by_code: dict[str, str] = {}
    for entry in parsed.get("legend") or []:
        if not isinstance(entry, dict):
            continue
        code = s(entry.get("code") or "").strip()
        klass = s(entry.get("class") or entry.get("meaning") or "").strip()
        if not code:
            continue
        out["legend"].append({"code": code, "class": klass})
        if klass:
            legend_by_code[code.casefold()] = klass

    # --- occurrences: the sparse long table ------------------------------
    def first_text(*vals: Any) -> str:
        """First value that is actually present, as text.

        NOT ``a or b or c``. A bare ``or`` chain treats the integer 0 as
        absent, so a row carrying the PRINTED value 0 — which is a reading,
        not a blank — short-circuits to the next field and then reads as an
        empty cell: the row is dropped and a false "dropped N row(s) for
        blank cells" warning is emitted. Only None and "" count as absent
        here; False and 0 are real readings and must survive.
        """
        for v in vals:
            if v is None:
                continue
            t = _stringify_scalar(v).strip()
            if t:
                return t
        return ""

    dropped_blank = 0
    unread_cells = 0
    unknown_column_rows = 0
    # FIX-2026-10-17 (P1-3): on the GEOMETRIC path an empty code does not
    # mean "blank cell" — it means "the ink test proved a mark is here and
    # the reader did not say what it was". Those are opposite claims and only
    # one of them may become a silently absent cell.
    #
    # rca_core/matrix.py:771 states the contract the two-stage path was built
    # around: "A tile the model did not answer is recorded with an empty code
    # rather than dropped: the CELL EXISTS, and dropping it would turn a read
    # failure into a silently wrong absence." This loop then dropped exactly
    # those rows, so the guarantee never reached the result: 3 inked cells with
    # 1 unanswered produced 2 occurrences, and the only trace was a warning
    # that described them as blank padding — which is the opposite of what they
    # were. `cell_status` is the discriminator, and it is exactly the right
    # one: it is an ARGUMENT this function receives (never read off the model
    # payload), and it is only `grid_cells` when every emitted cell came from a
    # pixel ink test.
    verified_geometry = (
        out["cell_status"] == "grid_cells")
    for raw_row in parsed.get("occurrences") or []:
        if not isinstance(raw_row, dict):
            continue
        code = first_text(raw_row.get("code"), raw_row.get("symbol"),
                          raw_row.get("value"))
        klass = first_text(raw_row.get("class"),
                           raw_row.get("abundance_class"))
        # Narrow recovery of the baseline shape: asked as an abundance diagram
        # the model puts the class letter in `abundance`. Accept it here so a
        # partially-migrated answer is still usable, but ONLY when there is no
        # dedicated `code`.
        if not code:
            code = first_text(raw_row.get("abundance"))

        # THE SPARSITY CONTRACT. No code and no class == a blank cell.
        #
        # ...except on the geometric path, where it means "inked but unread".
        # See `verified_geometry` above: there, dropping is what converts a
        # failed READING into a confident ABSENCE, and the count is reported
        # separately so the two are never conflated in the output either.
        if not code and not klass:
            if verified_geometry:
                unread_cells += 1
            else:
                dropped_blank += 1
                continue

        taxon = first_text(raw_row.get("taxon"))
        if not taxon:
            warnings.append("occurrence row with no taxon label; dropped")
            continue

        row_kind = first_text(raw_row.get("row_kind")) or "taxon"

        site = s(raw_row.get("site") or raw_row.get("locality")
                 or raw_row.get("column") or "").strip()
        if not klass and code:
            klass = legend_by_code.get(code.casefold(), "")

        row: dict[str, Any] = {
            "taxon": taxon,
            "authority": s(raw_row.get("authority") or "").strip(),
            "row_kind": row_kind,
            "site": site,
            "col_index": raw_row.get("col_index"),
            "code": code,
            "class": klass,
        }
        if verified_geometry and not code and not klass:
            # The cell EXISTS (a pixel ink test found a mark in it) and the
            # reader did not name it. That is a reading failure attached to a
            # known occurrence, and it has to be distinguishable downstream
            # from both a blank cell (never emitted) and a real code.
            row["cell_read"] = "unread"

        # --- column identity must be verifiable --------------------------
        col_index = row["col_index"]
        try:
            col_index = int(col_index)
        except (TypeError, ValueError):
            col_index = None

        resolved = None
        if site:
            resolved = site if site in label_set else norm_labels.get(
                re.sub(r"\s+", " ", site).casefold())
            if resolved:
                # Use the column's own spelling, so downstream grouping by
                # `site` cannot split one locality across two spellings.
                site = resolved

        known_index = {c["index"] for c in out["columns"]}
        if site and resolved is None:
            # A site that names no column is the transposition failure mode.
            # Keep the row (the taxon may still be readable) but flag it.
            unknown_column_rows += 1
            row["site"] = site
            row["col_index"] = col_index
            extra = attach_row_contract(raw_row, row)
            row["response_kind"] = "uncertain"
            row["reason_codes"] = sorted(
                set(list(row.get("reason_codes") or [])) | {"unknown_column"})
            _carry_extras(raw_row, _CONTRACT_SOURCE_KEYS + tuple(extra), row)
            warnings.append(
                f"occurrence row for {taxon!r} names site {site!r}, which is "
                f"not one of the {len(labels)} column headers")
        else:
            # `site` is the authoritative identity, so `col_index` is
            # reconciled to it - not merely range-checked. A row carrying
            # site="B" with col_index=0 (which IS a valid index, just the
            # wrong one) used to pass through untouched and into the export,
            # leaving a row whose two identity fields contradict each other
            # with no warning and no reason code. A contradictory pair means
            # one of the two was misread, which is precisely what
            # response_kind="uncertain" is for.
            site_index = next(
                (c["index"] for c in out["columns"] if c["label"] == site),
                None)
            if col_index is not None and site_index is not None \
                    and col_index != site_index:
                row["site"] = site
                row["col_index"] = col_index
                extra = attach_row_contract(raw_row, row)
                row["response_kind"] = "uncertain"
                row["reason_codes"] = sorted(
                    set(list(row.get("reason_codes") or []))
                    | {"column_index_conflict"})
                _carry_extras(raw_row, _CONTRACT_SOURCE_KEYS + tuple(extra),
                              row)
                warnings.append(
                    f"occurrence row for {taxon!r} claims col_index={col_index} "
                    f"but site {site!r} is column {site_index}; kept both and "
                    f"flagged uncertain")
            else:
                if col_index is None or col_index not in known_index:
                    col_index = site_index if site_index is not None \
                        else col_index
                row["site"] = site
                row["col_index"] = col_index
                extra = attach_row_contract(raw_row, row)
                _carry_extras(raw_row, _CONTRACT_SOURCE_KEYS + tuple(extra),
                              row)

        out["occurrences"].append(row)

    # --- taxon_rows: the labels-only path (cell_strategy "gated") ---------
    #
    # The gated strategy asks the model for headers, taxon labels and the
    # legend and explicitly NOT for grid cells, because those are the three
    # things it measurably reads well (19/22 taxa, 19/19 authorities, legend
    # all correct) while the cells are the thing it does not.
    #
    # These rows cannot go into `occurrences`. That list is SPARSE BY CONTRACT
    # - one row per FILLED cell - so a label with no code and no class is
    # indistinguishable from a blank cell and gets dropped by the rule above.
    # Folding them in would both empty the list and destroy exactly the
    # readings that were good.
    #
    # NO row_kind IS INFERRED HERE. The labels prompt transcribes a
    # stratigraphic comparison band ("Conodont level (see text)") into `taxon`
    # verbatim with an empty `authority`, as instructed, and there is no
    # reliable way to tell that row from a genus that simply has no named
    # authority. Guessing here would put a band into every taxon-level
    # statistic computed from this table. The band is carried faithfully and
    # its discriminator is documented rather than invented.
    for raw_row in parsed.get("rows") or []:
        if not isinstance(raw_row, dict):
            continue
        taxon = first_text(raw_row.get("taxon") or raw_row.get("label"))
        if not taxon:
            warnings.append("labels-only row with no taxon label; dropped")
            continue
        try:
            index = int(raw_row.get("index"))
        except (TypeError, ValueError):
            index = len(out["taxon_rows"])
        out["taxon_rows"].append({
            "index": index,
            "taxon": taxon,
            "authority": s(raw_row.get("authority") or "").strip(),
        })

    # --- strat_rows: the non-taxon comparison band -----------------------
    for band in parsed.get("strat_rows") or []:
        if not isinstance(band, dict):
            continue
        values = band.get("values")
        if not isinstance(values, list):
            values = []
        out["strat_rows"].append({
            "label": s(band.get("label") or "").strip(),
            "values": [s(v) for v in values],
        })

    if dropped_blank:
        # Loud, not silent: the count of rows this answer padding produced is
        # the single most useful thing to know when judging its quality.
        warnings.append(
            f"dropped {dropped_blank} row(s) for blank cells; this mode is "
            f"sparse by contract (one row per FILLED cell)")
    if unread_cells:
        # FIX-2026-10-17 (P1-3): a DIFFERENT failure from the one above, and
        # it is reported in different words on purpose. These cells were
        # proven filled by the ink test; the reader simply did not answer
        # them. They are kept, flagged `cell_read="unread"`, because the
        # alternative — omitting them — states "this taxon does not occur
        # here", which is the one claim this mode must never get wrong.
        warnings.append(
            f"{unread_cells} cell(s) carry a mark but no code was read for "
            f"them; kept as cell_read='unread' rather than reported as absent")

    # --- transposition, decided on the DISTRIBUTION not on a single row ----
    #
    # An occasional unknown_column row is one misread header, which the
    # per-row `uncertain` flag already reports honestly. But when MOST rows
    # name a site that is not a column, the whole table is rotated: the
    # baseline run of this mode did exactly that, returning the column headers
    # as taxa. That output is complete, well-formed and entirely wrong, which
    # is the failure shape this module's docstring calls worse than a refusal.
    #
    # The obvious alternative check - fewer taxa than columns - was rejected:
    # a 3-taxon x 18-locality matrix is an ordinary figure, so that ratio
    # says nothing about orientation.
    if (unknown_column_rows
            and len(out["occurrences"]) >= 3
            and len(out["columns"]) >= 3
            and unknown_column_rows * 2 > len(out["occurrences"])):
        warnings.append(
            f"{unknown_column_rows} of {len(out['occurrences'])} occurrence "
            f"rows name a site that is not one of the {len(out['columns'])} "
            f"column headers; the matrix may be transposed (rows and columns "
            f"swapped). Column labels and row labels stay usable; the "
            f"cell table should not be cited until this is resolved.")

    conf = parsed.get("confidence")
    try:
        out["confidence"] = _confidence_clamped(float(conf))
    except (TypeError, ValueError):
        out["confidence"] = 0.0

    _carry_extras(parsed, ("columns", "legend", "occurrences", "strat_rows",
                           "taxon_rows", "confidence", "cell_status"), out)
    if warnings:
        # FIX-2026-10-17 (P3-4): this wrote ``warnings``, while the other
        # eight normalizers write ``_warnings`` and every consumer reads that
        # one (``_payload_mismatch`` at :1350, ``_ok_result``, report.py,
        # aggregate.py, the JS mirror). Measured on the real normalizer, the
        # key simply did not exist downstream — so this mode's only two
        # diagnostics ("dropped N rows for blank cells", "matrix may be
        # transposed") reached no UI and were dropped again at merge time,
        # while the tests that assert on them read the local dict and passed.
        # Nothing else in the file writes a bare ``warnings`` now; the
        # compatibility shim below stays so a payload that already carries one
        # from an older caller still reaches the guard.
        out["_warnings"] = warnings
    return out


