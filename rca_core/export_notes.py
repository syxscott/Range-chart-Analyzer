"""The wording that MUST travel with every export containing a confidence column.

GAP-ANALYSIS-2026-11-02 A4 / #10 (B4). A per-row `confidence` value is the
MODEL'S OWN self-report. Nothing in this project calibrates it against
correctness: a row marked 0.9 has never been shown to be right 90% of the
time. Shipping the bare number invites a reader (or a reviewer) to treat it
as a probability and use it as a publication filter.

The XLSX exporter already stated this in its provenance sheet, but the JSON,
WPD and CSV/TSV paths did not. This module makes the wording ONE constant and
provides the machine-readable data dictionary (B8) so every format can carry
it without four slightly different sentences drifting apart.

CSV / TSV NOTE: those two formats are byte-pinned by
tests/test_export_render_parity.js (including the trailing CRLF). Adding a
comment row would silently break downstream parsers, so they keep their
exact bytes and their column semantics live in docs/DATA_DICTIONARY.md
(document-level sidecar). JSON outputs -- both result_to_json() and
`python main.py extract` -- have no byte pin and therefore carry the
dictionary INLINE, so a downstream pipeline only needs one file per figure.
This is a deliberate, documented decision -- not an oversight.
"""
from __future__ import annotations

from typing import Any

CONFIDENCE_NOTE = (
    "the per-row confidence column is the model's own self-report, not a "
    "calibrated probability; this project publishes no calibration curve "
    "for it"
)

#: Column -> (meaning, value domain, source). Kept as data so it can be
#: emitted into JSON / a sidecar file / documentation from one place.
DATA_DICTIONARY: dict[str, dict[str, str]] = {
    "confidence": {
        "meaning": "Model's self-reported confidence for this row.",
        "domain": "0.0 - 1.0 float, or null when the model did not answer.",
        "source": "VLM response; NOT calibrated against correctness.",
        "warning": CONFIDENCE_NOTE,
    },
    "reason_codes": {
        "meaning": "Why a coverage cell was unanswered / uncertain / not drawn.",
        "domain": "Stable slugs from js/reason-codes.js RCA_REASON_CODES.",
        "source": "Deterministic coverage ledger (rca_core/reason_codes.py).",
    },
    "range_base_label": {
        "meaning": "Verbatim stratigraphic label of the range's BASE as printed.",
        "domain": "Free text; keep the figure's spelling, do not substitute a stage.",
        "source": "VLM reading of the figure's axis labels.",
    },
    "range_top_label": {
        "meaning": "Verbatim stratigraphic label of the range's TOP as printed.",
        "domain": "Free text.",
        "source": "VLM reading of the figure's axis labels.",
    },
    "not_drawn": {
        "meaning": "The model inspected the figure cell but found NO occurrence.",
        "domain": "Boolean / count in the coverage ledger.",
        "source": "Distinct from 'gap' (cell never asked) and from a real "
                  "occurrence. v0.1.4 wrongly published it as a presence.",
    },
    "image_sha256": {
        "meaning": "SHA-256 of the source image bytes.",
        "domain": "64 lowercase hex characters.",
        "source": "Computed locally before the image leaves the machine.",
    },
    "ics_version": {
        "meaning": "Which International Chronostratigraphic Chart stamped the ages.",
        "domain": 'Currently "ICS v2024/12".',
        "source": "rca_core/standards/ics.py; cite the exact version in papers.",
    },
    "agreement_count": {
        "meaning": "How many runs agreed on this merged row.",
        "domain": "Integer <= total_runs.",
        "source": "rca_core/aggregate.py merge.",
    },
    "edit_count": {
        "meaning": "How many cells a human changed after extraction.",
        "domain": "Non-negative integer.",
        "source": "Editable result / history record.",
    },
}


def as_dict() -> dict[str, Any]:
    """Serialisable form for result_to_json / sidecar files / docs generation."""
    return {
        "confidence_note": CONFIDENCE_NOTE,
        "columns": DATA_DICTIONARY,
    }


__all__ = ["CONFIDENCE_NOTE", "DATA_DICTIONARY", "as_dict"]
