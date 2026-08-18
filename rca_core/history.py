"""Extraction history backed by SQLite.

One ``HistoryRecord`` per completed extraction. Records hold the
normalized result (JSON), an image thumbnail, and run metadata so the
History page can render a preview without re-running anything.

Provider configuration stays in the JSON file
(``~/.range_chart_analyzer/providers.json``); only the runtime
extraction records live here.
"""

from __future__ import annotations

import base64
import io
import json
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

from .db import Database


# Bug-8 fix: cap thumbnail dimensions + byte size so the SQLite history
# table doesn't grow without bound. Each thumbnail contributes at most
# ~10 KB (200×200 JPEG q70 ≈ 5–10 KB). Combined with MAX_HISTORY_ROWS,
# the DB stays under a few MB even after a year of heavy use.
THUMBNAIL_MAX_EDGE = 200
THUMBNAIL_JPEG_QUALITY = 70
THUMBNAIL_MAX_BYTES = 20 * 1024
MAX_HISTORY_ROWS = 500

# P1-1 (REVIEW-2026-07-25): single source of truth for the lock file path.
# Previously app.py and gui_fluent_history_detail.py each hard-coded their own
# copy; any future path change would have to update all three sites. Centralise
# here so callers can import it directly.
import os
LOCK_PATH = os.path.join(os.path.expanduser("~"), ".range_chart_analyzer", "lock")


def make_thumbnail(image_bytes: bytes, *, mime_hint: str = "") -> bytes:
    """Return a small JPEG thumbnail of *image_bytes*.

    The previous design stored the full decoded preview at whatever size
    the caller happened to pass in. A 4000-px-wide preview can run to
    hundreds of KB; multiplied by hundreds of history rows the DB
    balloons into the GB range. This helper enforces a hard cap:

      * long edge ≤ ``THUMBNAIL_MAX_EDGE`` (200 px)
      * JPEG quality ``THUMBNAIL_JPEG_QUALITY`` (70)
      * final byte size ≤ ``THUMBNAIL_MAX_BYTES`` (20 KB)

    Falls back to the input bytes (truncated) when Pillow is missing or
    decode fails — the GUI must still display *something*.
    """
    if not image_bytes:
        return b""
    try:
        from PIL import Image  # type: ignore
    except Exception:
        # No Pillow — fall back to truncated raw bytes. The bytes are
        # truncated so a single broken file can't fill the DB.
        return image_bytes[:THUMBNAIL_MAX_BYTES]
    try:
        img = Image.open(io.BytesIO(image_bytes))
        # Normalize mode: PNG with RGBA, palette, etc. → RGB for JPEG.
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        # Resize in-place-ish: keep aspect, fit within the max edge.
        w, h = img.size
        long_edge = max(w, h)
        if long_edge > THUMBNAIL_MAX_EDGE:
            scale = THUMBNAIL_MAX_EDGE / long_edge
            nw, nh = max(1, int(round(w * scale))), max(1, int(round(h * scale)))
            img = img.resize((nw, nh), Image.LANCZOS)
        out = io.BytesIO()
        # Lower quality iteratively until we fit under the byte cap.
        # Quality 70 typically lands well under 20 KB at 200 px.
        quality = THUMBNAIL_JPEG_QUALITY
        while quality >= 30:
            out.seek(0); out.truncate(0)
            img.save(out, format="JPEG", quality=quality, optimize=True)
            if out.tell() <= THUMBNAIL_MAX_BYTES:
                return out.getvalue()
            quality -= 15
        return out.getvalue()
    except Exception:
        return image_bytes[:THUMBNAIL_MAX_BYTES]


@dataclass
class HistoryRecord:
    id: int = 0
    timestamp: float = 0.0
    source_file: str = ""
    image_thumbnail: bytes | None = None  # small JPEG/PNG bytes
    image_width: int = 0
    image_height: int = 0
    provider_id: str = ""
    provider_name: str = ""
    model: str = ""
    mode: str = "range_chart"  # range_chart | columnar_section
    runs: int = 1
    result: dict[str, Any] = field(default_factory=dict)
    raw: str = ""            # raw model response (truncated to ~8KB on save; full text in raw_responses table)
    confidence: float = 0.0
    partial_failures: int = 0
    duration_ms: int = 0
    status_code: int | None = None
    notes: str = ""
    # P1-1/P1-2 (REVIEW-2026-07-27): mandatory image fingerprint and per-request metadata
    image_sha256: str = ""
    request_meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize for JSON export. Thumbnail goes to base64.

        Phase J fix: ``image_sha256`` and ``request_meta`` are
        also serialized so the audit trail survives a JSON
        export. Without them, exporting a row to share with a
        colleague drops the very fields the audit needs (image
        fingerprint + sampling context).
        """
        thumb_b64 = None
        if self.image_thumbnail:
            try:
                thumb_b64 = base64.b64encode(self.image_thumbnail).decode("ascii")
            except Exception:
                thumb_b64 = None
        return {
            "id": self.id,
            "timestamp": self.timestamp,
            "source_file": self.source_file,
            "image_thumbnail_b64": thumb_b64,
            "image_width": self.image_width,
            "image_height": self.image_height,
            "provider_id": self.provider_id,
            "provider_name": self.provider_name,
            "model": self.model,
            "mode": self.mode,
            "runs": self.runs,
            "result": self.result,
            "raw": self.raw,
            "confidence": self.confidence,
            "partial_failures": self.partial_failures,
            "duration_ms": self.duration_ms,
            "status_code": self.status_code,
            "notes": self.notes,
            "image_sha256": self.image_sha256 or "",
            "request_meta": dict(self.request_meta or {}),
        }


def _row_to_record(row) -> HistoryRecord:
    result = json.loads(row["result_json"]) if row["result_json"] else {}
    request_meta = {}
    if "request_meta" in row.keys() and row["request_meta"]:
        try:
            request_meta = json.loads(row["request_meta"])
            if not isinstance(request_meta, dict):
                request_meta = {}
        except Exception:
            request_meta = {}
    return HistoryRecord(
        id=row["id"],
        timestamp=row["timestamp"],
        source_file=row["source_file"] or "",
        image_thumbnail=row["image_thumbnail"],
        image_width=row["image_width"] or 0,
        image_height=row["image_height"] or 0,
        provider_id=row["provider_id"] or "",
        provider_name=row["provider_name"] or "",
        model=row["model"] or "",
        mode=row["mode"] or "range_chart",
        runs=row["runs"] or 1,
        result=result,
        raw=row["raw_json"] or "",
        confidence=row["confidence"] or 0.0,
        partial_failures=row["partial_failures"] or 0,
        duration_ms=row["duration_ms"] or 0,
        status_code=row["status_code"],
        notes=row["notes"] or "",
        image_sha256=(row["image_sha256"] or "") if "image_sha256" in row.keys() else "",
        request_meta=request_meta,
    )


# Cap raw response body stored per record to keep the DB small.
_MAX_RAW_BYTES = 8 * 1024


class HistoryStore:
    """CRUD over the history table."""

    def __init__(self, db: Database | None = None) -> None:
        self.db = db or Database()

    # ---- writes ----

    def add(self, rec: HistoryRecord, raw_responses: list[dict[str, Any]] | None = None) -> int:
        """Insert a history record.

        P2 (REVIEW-2026-07-27): ``raw_responses`` is a list of per-run
        dicts with keys ``run_idx``, ``raw_text`` (full text, no truncation),
        ``prompt_text``, ``request_meta`` (dict), ``timestamp``. Each entry
        is persisted into the ``raw_responses`` table so a 5-year audit can
        recover the exact LLM response even when the model's output exceeds
        the 8 KB cap on the legacy ``raw_json`` column.
        """
        if not rec.timestamp:
            rec.timestamp = time.time()
        # Bug-8 fix: shrink the thumbnail to the standard size before
        # storage so a caller passing a giant preview doesn't blow up
        # the DB. Idempotent — already-small thumbnails pass through.
        if rec.image_thumbnail:
            rec.image_thumbnail = make_thumbnail(rec.image_thumbnail)
        raw = rec.raw if len(rec.raw) <= _MAX_RAW_BYTES else rec.raw[:_MAX_RAW_BYTES]
        request_meta_json = json.dumps(rec.request_meta, ensure_ascii=False) if rec.request_meta else None
        cur = self.db.execute(
            """INSERT INTO history (
                timestamp, source_file, image_thumbnail, image_width, image_height,
                provider_id, provider_name, model, mode, runs,
                result_json, raw_json, confidence, partial_failures,
                duration_ms, status_code, notes,
                -- P1-5 (REVIEW-2026-07-25): provenance fields
                last_edited_at, last_editor, edit_count, edit_provenance,
                -- P1-1/P1-2 (REVIEW-2026-07-27): audit fields
                image_sha256, request_meta
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                      NULL, NULL, 0, NULL, ?, ?)""",
            (
                rec.timestamp, rec.source_file, rec.image_thumbnail,
                rec.image_width, rec.image_height,
                rec.provider_id, rec.provider_name, rec.model, rec.mode, rec.runs,
                json.dumps(rec.result, ensure_ascii=False), raw,
                rec.confidence, rec.partial_failures,
                rec.duration_ms, rec.status_code, rec.notes,
                rec.image_sha256 or None,
                request_meta_json,
            ),
        )
        rec.id = int(cur.lastrowid)
        # P2 (REVIEW-2026-07-27): full raw responses per run, no truncation
        if raw_responses:
            self._insert_raw_responses(rec.id, raw_responses)
        # Bug-8 fix: LRU eviction. Keep at most MAX_HISTORY_ROWS rows;
        # delete the oldest when we exceed the cap. This caps total DB
        # size to roughly MAX_HISTORY_ROWS * THUMBNAIL_MAX_BYTES ≈ 10 MB
        # even after months of daily use.
        self._enforce_row_cap()
        return rec.id

    def _insert_raw_responses(self, record_id: int, raw_responses: list[dict[str, Any]]) -> None:
        """Persist per-run raw responses to the raw_responses table."""
        rows = []
        for rr in raw_responses:
            rows.append((
                record_id,
                int(rr.get("run_idx", 0)),
                rr.get("raw_text", ""),
                rr.get("prompt_text", "") or "",
                json.dumps(rr.get("request_meta", {}), ensure_ascii=False),
                int(rr.get("timestamp", time.time())),
            ))
        if rows:
            self.db.executemany(
                "INSERT INTO raw_responses "
                "(record_id, run_idx, raw_text, prompt_text, request_meta, timestamp) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                rows,
            )

    def get_raw_responses(self, record_id: int, run_idx: int | None = None) -> list[dict[str, Any]]:
        """Return raw responses for a record. Filter by run_idx if given."""
        if run_idx is None:
            rows = self.db.query(
                "SELECT * FROM raw_responses WHERE record_id = ? ORDER BY run_idx ASC",
                (record_id,),
            )
        else:
            rows = self.db.query(
                "SELECT * FROM raw_responses WHERE record_id = ? AND run_idx = ?",
                (record_id, run_idx),
            )
        out = []
        for row in rows:
            request_meta = {}
            try:
                if row["request_meta"]:
                    request_meta = json.loads(row["request_meta"])
            except Exception:
                request_meta = {}
            out.append({
                "run_idx": row["run_idx"],
                "raw_text": row["raw_text"] or "",
                "prompt_text": row["prompt_text"] or "",
                "request_meta": request_meta,
                "timestamp": row["timestamp"],
            })
        return out

    def get_by_sha256(self, sha256: str) -> list[HistoryRecord]:
        """Return all records that came from the same image fingerprint."""
        if not sha256:
            return []
        rows = self.db.query(
            "SELECT * FROM history WHERE image_sha256 = ? ORDER BY timestamp DESC",
            (sha256,),
        )
        return [_row_to_record(r) for r in rows]

    def record_edit(
        self,
        record_id: int,
        editor: str,
        edit_type: str,
        row_idx: int | None = None,
        col_name: str | None = None,
        before: Any = None,
        after: Any = None,
    ) -> int:
        """Append an immutable audit entry to record_edits. Returns entry id."""
        cur = self.db.execute(
            "INSERT INTO record_edits (record_id, timestamp, editor, edit_type, "
            "row_idx, col_name, before, after) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record_id, int(time.time()), editor, edit_type,
                row_idx, col_name,
                json.dumps(before, ensure_ascii=False) if before is not None else None,
                json.dumps(after, ensure_ascii=False) if after is not None else None,
            ),
        )
        return int(cur.lastrowid)

    def get_edits(self, record_id: int) -> list[dict[str, Any]]:
        """Return all edits for a record, oldest first."""
        rows = self.db.query(
            "SELECT * FROM record_edits WHERE record_id = ? ORDER BY timestamp ASC, id ASC",
            (record_id,),
        )
        out = []
        for row in rows:
            before = None
            after = None
            try:
                if row["before"] is not None:
                    before = json.loads(row["before"])
            except Exception:
                before = None
            try:
                if row["after"] is not None:
                    after = json.loads(row["after"])
            except Exception:
                after = None
            out.append({
                "id": row["id"],
                "timestamp": row["timestamp"],
                "editor": row["editor"] or "",
                "edit_type": row["edit_type"],
                "row_idx": row["row_idx"],
                "col_name": row["col_name"],
                "before": before,
                "after": after,
            })
        return out

    def _enforce_row_cap(self) -> None:
        """Trim oldest rows when the table exceeds MAX_HISTORY_ROWS.

        Cheap COUNT + DELETE WHERE id IN (oldest excess). Best-effort:
        if the cap is way off (e.g. user manually edited the DB), the
        excess delete just runs in one statement and converges.
        """
        try:
            n = self.count()
            if n <= MAX_HISTORY_ROWS:
                return
            excess = n - MAX_HISTORY_ROWS
            self.db.execute(
                "DELETE FROM history WHERE id IN ("
                "  SELECT id FROM history ORDER BY timestamp ASC LIMIT ?"
                ")",
                (excess,),
            )
        except Exception:
            # Eviction must never fail the user's save. Swallow + log.
            import sys
            print(f"[history] LRU eviction failed: {sys.exc_info()[1]}", file=sys.stderr)

    def update_notes(self, record_id: int, notes: str) -> bool:
        cur = self.db.execute(
            "UPDATE history SET notes = ? WHERE id = ?",
            (notes, record_id),
        )
        return cur.rowcount > 0

    def update_result(self, record_id: int, result: dict[str, Any],
                      editor: str = "user") -> bool:
        """Update result_json AND append a provenance entry.

        P1-5 (REVIEW-2026-07-25): previously this overwrote result_json
        in place and lost the link between the original extraction and
        any user edit. Operators downstream could not tell which fields
        were model-emitted vs operator-edited. We now persist:
          * last_edited_at (ISO 8601)
          * last_editor  (e.g. 'user', 'gui_fluent', 'js/app.js')
          * edit_count   (incremented)
          * edit_provenance (JSON list of {at, editor, summary,
                                          prov_o_activity: {...}})
        P2-4 (REVIEW-2026-07-25): each edit also produces a PROV-O
        (https://www.w3.org/TR/prov-o/) shaped activity record, stored
        on the same chain. Consumers (CSL-Editor ingestion, repository
        archival) can map this directly to PROV-O triples.
        """
        import datetime as _dt
        with self.db.transaction():
            # REVIEW-2026-07-31: statements inside the transaction use
            # query_one() / run() instead of execute() — execute() commits
            # after every statement, which silently ended the BEGIN
            # IMMEDIATE block and split the UPDATE + audit INSERT into two
            # independent transactions (a failure between them could not be
            # rolled back).
            row = self.db.query_one(
                "SELECT result_json, edit_count, edit_provenance FROM history WHERE id = ?",
                (record_id,),
            )
            if row is None:
                return False
            prev_result_json = row[0]
            prev_count = int(row[1] or 0)
            prev_chain = []
            if row[2]:
                try:
                    prev_chain = json.loads(row[2])
                    if not isinstance(prev_chain, list):
                        prev_chain = []
                except Exception:
                    prev_chain = []
            new_count = prev_count + 1
            summary_keys = sorted(result.keys())[:20]
            prev_chain.append({
                "at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
                "editor": editor,
                "n_keys": len(summary_keys),
                # P2-4 (REVIEW-2026-07-25): PROV-O activity record. Each
                # edit becomes a wasAssociatedWith link from this row's
                # entity to an agent (gui_fluent / js/app.js / api) using a
                # prov:Activity whose type is "edit".
                "prov_o_activity": {
                    "type": "prov:Activity",
                    "id": f"rca:history:{record_id}:edit:{new_count}",
                    "prov:startedAtTime": _dt.datetime.now(
                        _dt.timezone.utc
                    ).isoformat(),
                    # An edit is instantaneous for our purposes.
                    "prov:endedAtTime": _dt.datetime.now(
                        _dt.timezone.utc
                    ).isoformat(),
                    "prov:wasAssociatedWith": {
                        "type": "prov:Agent",
                        "id": f"rca:agent:{editor}",
                    },
                    "prov:used": {
                        "type": "prov:Entity",
                        "id": f"rca:history:{record_id}",
                    },
                    "prov:generated": {
                        "type": "prov:Entity",
                        "id": f"rca:history:{record_id}:edit:{new_count}",
                    },
                },
            })
            cur = self.db.run(
                """UPDATE history SET result_json = ?, last_edited_at = ?,
                   last_editor = ?, edit_count = ?, edit_provenance = ?
                   WHERE id = ?""",
                (
                    json.dumps(result, ensure_ascii=False),
                    _dt.datetime.now(_dt.timezone.utc).isoformat(),
                    editor, new_count, json.dumps(prev_chain, ensure_ascii=False),
                    record_id,
                ),
            )
            # P3 (REVIEW-2026-07-27): append immutable audit row.
            # record_edits stores the BEFORE/AFTER snapshot so a 5-year
            # audit can reconstruct any historical state without trusting
            # the current result_json.
            before_dict = None
            if prev_result_json:
                try:
                    before_dict = json.loads(prev_result_json)
                except Exception:
                    before_dict = prev_result_json
            self.db.run(
                "INSERT INTO record_edits "
                "(record_id, timestamp, editor, edit_type, before, after) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    record_id,
                    int(time.time()),
                    editor,
                    "result_update",
                    json.dumps(before_dict, ensure_ascii=False) if before_dict is not None else None,
                    json.dumps(result, ensure_ascii=False),
                ),
            )
            return cur.rowcount > 0

    def delete(self, record_id: int) -> bool:
        cur = self.db.execute("DELETE FROM history WHERE id = ?", (record_id,))
        return cur.rowcount > 0

    def delete_many(self, record_ids: Iterable[int]) -> int:
        ids = list(record_ids)
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        cur = self.db.execute(
            f"DELETE FROM history WHERE id IN ({placeholders})",
            tuple(ids),
        )
        return cur.rowcount

    def clear(self) -> int:
        cur = self.db.execute("DELETE FROM history")
        return cur.rowcount

    # ---- reads ----

    def get(self, record_id: int) -> HistoryRecord | None:
        row = self.db.query_one("SELECT * FROM history WHERE id = ?", (record_id,))
        return _row_to_record(row) if row else None

    def list(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        mode: str | None = None,
        search: str | None = None,
    ) -> list[HistoryRecord]:
        sql = "SELECT * FROM history WHERE 1=1"
        params: list[Any] = []
        if mode:
            sql += " AND mode = ?"
            params.append(mode)
        if search:
            like = f"%{search}%"
            sql += " AND (source_file LIKE ? OR notes LIKE ? OR provider_name LIKE ? OR model LIKE ?)"
            params.extend([like, like, like, like])
        sql += " ORDER BY timestamp DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        return [_row_to_record(r) for r in self.db.query(sql, tuple(params))]

    def count(self) -> int:
        row = self.db.query_one("SELECT COUNT(*) AS n FROM history")
        return int(row["n"] if row else 0)

    def to_prov_jsonld(self, record_id: int) -> dict[str, Any]:
        """Generate a complete W3C PROV-O JSON-LD provenance document.

        Produces a valid JSON-LD document with:
        - @context with W3C PROV-O + custom RCA namespace
        - prov:Entity for the original image + result entities
        - prov:Activity for extraction, merge, and each edit
        - prov:Agent for user + software agents
        - prov:wasGeneratedBy, prov:used, prov:wasAssociatedWith links

        Args:
            record_id: the history record id

        Returns:
            dict ready for json.dumps(); caller serializes as needed.
            Returns empty document (only @context) if record not found.
        """
        import datetime as _dt

        record = self.get(record_id)
        if record is None:
            return {
                "@context": _PROV_CONTEXT,
            }

        # Build the edit_provenance chain
        edit_chain: list[dict[str, Any]] = []
        row = self.db.query_one(
            "SELECT edit_provenance FROM history WHERE id = ?", (record_id,)
        )
        if row and row["edit_provenance"]:
            try:
                edit_chain = json.loads(row["edit_provenance"])
            except Exception:
                edit_chain = []

        doc: dict[str, Any] = {
            "@context": _PROV_CONTEXT,
        }

        # ---- Entities ----

        # Original image entity (rca:image:{sha256_of_source})
        source_path = record.source_file or ""
        # REVIEW-2026-11-07 (low): when the source path is missing (e.g. a
        # clipboard paste), most records still carry the content hash
        # (image_sha256). Prefer it over record_id — the content hash is
        # the stable identity and keeps the entity id meaningful; record_id
        # stays only as the last resort.
        if source_path:
            image_entity_id = f"rca:image:{_sha256(source_path)}"
        elif record.image_sha256:
            image_entity_id = f"rca:image:{record.image_sha256}"
        else:
            image_entity_id = f"rca:image:{record_id}"
        doc.setdefault("prov:entity", []).append({
            "@id": image_entity_id,
            "prov:type": "prov:Entity",
            "prov:label": f"Source image ({source_path})",
        })

        # Raw/extracted result entity (rca:history:{id})
        history_entity_id = f"rca:history:{record_id}"
        doc.setdefault("prov:entity", []).append({
            "@id": history_entity_id,
            "prov:type": "prov:Entity",
            "prov:label": f"Extraction result record={record_id}",
            "prov:wasGeneratedBy": {"@id": f"rca:history:{record_id}:extract"},
        })

        # Each edit version entity (rca:history:{id}:v{N})
        for edit_idx, entry in enumerate(edit_chain, start=1):
            edit_entity_id = f"rca:history:{record_id}:edit:{edit_idx}"
            doc.setdefault("prov:entity", []).append({
                "@id": edit_entity_id,
                "prov:type": "prov:Entity",
                "prov:label": f"Edit v{edit_idx} on record={record_id}",
                "prov:wasGeneratedBy": {"@id": entry.get("prov_o_activity", {}).get("id", f"rca:history:{record_id}:edit:{edit_idx}")},
            })

        # ---- Activities ----

        # Extraction activity
        extract_activity_id = f"rca:history:{record_id}:extract"
        extract_activity: dict[str, Any] = {
            "@id": extract_activity_id,
            "prov:type": "prov:Activity",
            "prov:label": f"LLM extraction record={record_id}",
            "prov:used": {"@id": image_entity_id},
            "prov:wasAssociatedWith": {
                "@id": f"rca:agent:{record.provider_name}" if record.provider_name else "rca:agent:unknown",
                "prov:type": "prov:Agent",
            },
        }
        doc.setdefault("prov:activity", []).append(extract_activity)

        # Merge activity (only if runs > 1)
        if record.runs and record.runs > 1:
            merge_activity_id = f"rca:history:{record_id}:merge"
            doc.setdefault("prov:activity", []).append({
                "@id": merge_activity_id,
                "prov:type": "prov:Activity",
                "prov:label": f"Multi-run merge record={record_id}",
                "prov:wasAssociatedWith": {
                    "@id": f"rca:agent:{record.provider_name}",
                    "prov:type": "prov:Agent",
                },
            })

        # Edit activities (from edit_provenance chain)
        for edit_idx, entry in enumerate(edit_chain, start=1):
            prov_o = entry.get("prov_o_activity") or {}
            activity_id = prov_o.get("id", f"rca:history:{record_id}:edit:{edit_idx}")
            agent_ref = prov_o.get("prov:wasAssociatedWith", {})
            agent_id = agent_ref.get("id", f"rca:agent:{entry.get('editor', 'user')}")
            used_ref = prov_o.get("prov:used", {})
            used_id = used_ref.get("id", history_entity_id)
            generated_ref = prov_o.get("prov:generated", {})
            generated_id = generated_ref.get("id", f"rca:history:{record_id}:edit:{edit_idx}")

            edit_activity: dict[str, Any] = {
                "@id": activity_id,
                "prov:type": "prov:Activity",
                "prov:label": f"Edit {edit_idx} on record={record_id}",
            }
            started = prov_o.get("prov:startedAtTime")
            if started:
                edit_activity["prov:startedAtTime"] = started
            ended = prov_o.get("prov:endedAtTime")
            if ended:
                edit_activity["prov:endedAtTime"] = ended

            edit_activity["prov:used"] = {"@id": used_id}
            edit_activity["prov:wasAssociatedWith"] = {
                "@id": agent_id,
                "prov:type": "prov:Agent",
            }
            edit_activity["prov:wasGeneratedBy"] = {"@id": activity_id}

            doc.setdefault("prov:activity", []).append(edit_activity)

            # Link the edit entity back to the activity
            edit_entity = {
                "@id": generated_id,
                "prov:type": "prov:Entity",
                "prov:wasGeneratedBy": {"@id": activity_id},
            }
            # Avoid duplicate entries
            entities = doc.setdefault("prov:entity", [])
            if not any(e.get("@id") == generated_id for e in entities):
                entities.append(edit_entity)

        # ---- Agents ----

        # User agent
        doc.setdefault("prov:agent", []).append({
            "@id": "rca:agent:user",
            "prov:type": "prov:Agent",
            "prov:label": "Human operator",
        })

        # GUI agent
        doc.setdefault("prov:agent", []).append({
            "@id": "rca:agent:gui_fluent",
            "prov:type": "prov:SoftwareAgent",
            "prov:label": "PySide6 GUI (gui_fluent)",
        })

        # JS frontend agent
        doc.setdefault("prov:agent", []).append({
            "@id": "rca:agent:js/app.js",
            "prov:type": "prov:SoftwareAgent",
            "prov:label": "JavaScript frontend (js/app.js)",
        })

        # LLM provider agent
        if record.provider_name:
            doc.setdefault("prov:agent", []).append({
                "@id": f"rca:agent:{record.provider_name}",
                "prov:type": "prov:SoftwareAgent",
                "prov:label": f"LLM provider: {record.provider_name}",
            })

        return doc


# SHA-256 helper for image entity IDs
def _sha256(s: str) -> str:
    """Return lowercase hex SHA-256 of the input string."""
    import hashlib
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


# Shared PROV-O JSON-LD context used by to_prov_jsonld()
_PROV_CONTEXT = {
    "@prefix": True,
    "prov": "https://www.w3.org/ns/prov#",
    "rca": "https://range-chart-analyzer.github.io/ns/rca.jsonld",
    "prov:wasGeneratedBy": {"@type": "@id"},
    "prov:used": {"@type": "@id"},
    "prov:wasAssociatedWith": {"@type": "@id"},
    "prov:wasDerivedFrom": {"@type": "@id"},
    "prov:startedAtTime": {"@type": "xsd:dateTime"},
    "prov:endedAtTime": {"@type": "xsd:dateTime"},
    "xsd": "http://www.w3.org/2001/XMLSchema#",
    "rca:image": {"@type": "@id"},
    "rca:history": {"@type": "@id"},
    "rca:agent": {"@type": "@id"},
}
