"""Standards export: Darwin Core Archive + the PBDB upload sheets.

AUDIT-2026-10-22. The README advertised "Darwin Core and PBDB standard fields"
as a deliverable, and the writers existed (``rca_core/standards/darwin_core.py``,
``rca_core/standards/pbdb.py``) -- but they were reachable only from Python.
The desktop GUI, which is the default entry point (``run.bat``), offered
JSON / XLSX / CSV / TSV and nothing else, so a user could not produce the
standards-conformant files the documentation told them to publish.

This module is the Qt-free core of that export: one call writes a
Darwin Core Archive and the three PBDB sheets into a directory, and reports
what it wrote plus anything it could not publish. The GUI button is a thin
wrapper over it, so the behaviour is testable without Qt.
"""
from __future__ import annotations

import os
from typing import Any, Optional


def export_standards(
    result: dict,
    out_dir: str,
    prefix: str = "",
) -> dict[str, Any]:
    """Write the Darwin Core Archive and the PBDB sheets into *out_dir*.

    Returns a dict::

        {
          "ok": bool,
          "files": {name: path, ...},
          "warnings": [ {reason, species, section, detail}, ... ],
          "errors": [str, ...],
        }

    ``warnings`` are the rows the standards writers deliberately did NOT
    publish -- a ``not_drawn`` row (the taxon was examined and not found) and
    an ``uncertain`` row (the name could not be read). They are reported
    rather than dropped silently, because a published dataset that is
    quietly missing taxa is the failure mode this whole export exists to
    prevent.
    """
    out: dict[str, Any] = {"ok": False, "files": {}, "warnings": [], "errors": []}
    if not isinstance(result, dict) or not result:
        out["errors"].append("no result to export")
        return out
    os.makedirs(out_dir, exist_ok=True)
    stem = (prefix or "rca-").rstrip("-_")
    stem = stem or "rca"

    # --- Darwin Core Archive ---------------------------------------------
    dwca_path = os.path.join(out_dir, stem + "darwin_core.zip")
    try:
        from .standards.darwin_core import to_darwin_core_archive
        to_darwin_core_archive(result, dwca_path, warnings_out=out["warnings"])
        out["files"]["darwin_core"] = dwca_path
    except Exception as exc:
        out["errors"].append("Darwin Core Archive: %s: %s"
                             % (type(exc).__name__, exc))

    # --- PBDB: one directory, three sheets --------------------------------
    try:
        from .standards.pbdb import to_pbdb_csv
        paths = to_pbdb_csv(result, out_dir, warnings_out=out["warnings"])
        for key, path in (paths or {}).items():
            out["files"]["pbdb_" + str(key)] = path
    except Exception as exc:
        out["errors"].append("PBDB sheets: %s: %s" % (type(exc).__name__, exc))

    out["ok"] = bool(out["files"]) and not out["errors"]
    # BOTH standards writers report the rows they did not publish, so the
    # same row arrives twice. A user-facing report lists it once, and says
    # that both standards dropped it.
    seen: set[tuple] = set()
    merged: list[dict] = []
    for w in out["warnings"]:
        key = (w.get("reason"), w.get("row_index"), w.get("species"))
        if key in seen:
            for prev in merged:
                if (prev.get("reason"), prev.get("row_index"),
                        prev.get("species")) == key:
                    prev["standards"] = list(prev.get("standards", [])) + ["pbdb"]
                    break
            continue
        seen.add(key)
        w = dict(w)
        w["standards"] = ["darwin_core"]
        merged.append(w)
    out["warnings"] = merged
    return out


def standards_available() -> tuple[bool, str]:
    """Whether the standards writers can be used at all, and why not.

    The ICS table is optional: without it the age columns come out empty, but
    the exports still work. A missing ``openpyxl`` does not affect these two
    writers.
    """
    try:
        from .standards import darwin_core, pbdb  # noqa: F401
    except Exception as exc:
        return False, "%s: %s" % (type(exc).__name__, exc)
    return True, ""


__all__ = ["export_standards", "standards_available"]
