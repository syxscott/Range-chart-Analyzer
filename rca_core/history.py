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
from typing import Any, Iterable, Optional

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


def _coerce_image_bytes(image: Any) -> bytes:
    """Accept bytes-like input OR a filesystem path and return bytes.

    REVIEW-2026-09-20 (finding 9): ``gui.py::_maybe_thumbnail`` calls
    ``make_thumbnail(self.image_path)`` with a PATH STRING. A str has no
    ``__buffer__``, so the Pillow open always failed and the old fallback
    returned ``path[:20480]`` — the source-file name, stored in the
    ``image_thumbnail`` BLOB column as text. Reading the file here turns that
    call site into a real thumbnail without touching the GUI.
    """
    if isinstance(image, (bytes, bytearray, memoryview)):
        return bytes(image)
    if isinstance(image, str) and image:
        try:
            if os.path.isfile(image):
                with open(image, "rb") as fh:
                    return fh.read()
        except OSError:
            return b""
    return b""


def make_thumbnail_with_size(
    image_bytes: Any, *, mime_hint: str = ""
) -> tuple[bytes, Optional[int], Optional[int]]:
    """Return ``(jpeg_bytes, width, height)`` for a capped thumbnail.

    ``make_thumbnail`` keeps the historical bytes-only contract for its
    callers; this companion also reports the size of the image that is
    actually being stored, which is what the history row's
    ``image_width`` / ``image_height`` must describe.

    REVIEW-2026-09-20 (finding 9):
    * every failure path (Pillow missing, undecodable input, encoder error)
      now returns ``b"", None, None``. The previous fallback stored the first
      20 KB of the ORIGINAL file — a truncated JPEG/PNG header, i.e. a blob
      that no decoder accepts, so the History page rendered an empty box and
      the recorded width/height claimed a size for an image that could not be
      shown. Storing nothing is honest, and the row cap keeps the DB small
      either way.
    * the Pillow path applies ``ImageOps.exif_transpose`` first: a phone
      photo carries its rotation in EXIF Orientation, and without this the
      thumbnail was landscape for a portrait shot (and its reported size was
      transposed relative to what viewers show).
    """
    raw = _coerce_image_bytes(image_bytes)
    if not raw:
        return b"", None, None
    try:
        from PIL import Image, ImageOps  # type: ignore
    except Exception:
        # No Pillow: no thumbnail at all rather than an undecodable fragment.
        return b"", None, None
    try:
        img = Image.open(io.BytesIO(raw))
        # Orientation first — resizing a transposed bitmap bakes the wrong
        # aspect ratio in. A corrupt EXIF block must not cost us the
        # thumbnail, so the transpose is best-effort.
        try:
            transposed = ImageOps.exif_transpose(img)
            if transposed is not None:
                img = transposed
        except Exception:
            pass
        # Normalize mode: PNG with RGBA, palette, etc. → RGB for JPEG.
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        # Resize in-place-ish: keep aspect, fit within the max edge.
        w, h = img.size
        long_edge = max(w, h)
        if long_edge > THUMBNAIL_MAX_EDGE:
            scale = THUMBNAIL_MAX_EDGE / long_edge
            w, h = max(1, int(round(w * scale))), max(1, int(round(h * scale)))
            img = img.resize((w, h), Image.LANCZOS)
        out = io.BytesIO()
        # Lower quality iteratively until we fit under the byte cap.
        # Quality 70 typically lands well under 20 KB at 200 px.
        quality = THUMBNAIL_JPEG_QUALITY
        while quality >= 30:
            out.seek(0); out.truncate(0)
            img.save(out, format="JPEG", quality=quality, optimize=True)
            if out.tell() <= THUMBNAIL_MAX_BYTES:
                return out.getvalue(), int(w), int(h)
            quality -= 15
        return out.getvalue(), int(w), int(h)
    except Exception:
        return b"", None, None


def make_thumbnail(image_bytes: Any, *, mime_hint: str = "") -> bytes:
    """Return a small JPEG thumbnail of *image_bytes*.

    ``image_bytes`` may be raw image bytes or a path to an image file
    (``gui.py`` passes a path). Returns ``b""`` when no decodable thumbnail
    can be produced — see :func:`make_thumbnail_with_size` for the full
    contract and the size the caller must store alongside it.

    Caps:

      * long edge ≤ ``THUMBNAIL_MAX_EDGE`` (200 px)
      * JPEG quality ``THUMBNAIL_JPEG_QUALITY`` (70)
      * final byte size ≤ ``THUMBNAIL_MAX_BYTES`` (20 KB)
    """
    return make_thumbnail_with_size(image_bytes, mime_hint=mime_hint)[0]


@dataclass
class HistoryRecord:
    id: int = 0
    timestamp: float = 0.0
    source_file: str = ""
    image_thumbnail: bytes | None = None  # small JPEG/PNG bytes
    # Source image size in pixels. May be None (``add`` stores NULL) when no
    # thumbnail could be produced, i.e. there is nothing to size.
    image_width: int | None = 0
    image_height: int | None = 0
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


def _decode_audit_value(value: Any) -> Any:
    """Read back a ``record_edits.before`` / ``.after`` column value.

    REVIEW-2026-09-20 (finding 8): these columns used to be declared ``JSON``,
    and a JSON-affinity column keeps the value's own storage class — so
    ``json.dumps(12)`` (the TEXT ``"12"``) came back as the INTEGER 12,
    ``json.dumps(12.5)`` as a REAL, and the unconditional ``json.loads()``
    raised ``TypeError`` on them. The bare ``except`` then reported the edit
    as having NO previous/new value: every numeric audit entry (bed index,
    token count, age) was lost from the history detail dialog.

    Only genuine text is decoded now; anything SQLite already stored as a
    number / blob / None IS the value and is adopted as written. Databases
    created before the column was retyped to TEXT keep their integer rows,
    and this reader makes them readable again, so no migration is needed.
    """
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray)):
        try:
            value = bytes(value).decode("utf-8")
        except Exception:
            return None
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            # Genuinely malformed legacy content: keep the raw text rather
            # than pretending the edit had no value.
            return value
    return value


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
        # REVIEW-2026-09-20 (finding 9): the helper now also reports the
        # dimensions of the image it actually produced. When it could not
        # produce one (no Pillow / undecodable bytes) the blob is dropped
        # instead of a truncated, non-decodable fragment being stored — and
        # the width/height that described that fragment go NULL, so no
        # consumer renders a phantom aspect ratio for a picture the row does
        # not carry.
        if rec.image_thumbnail:
            thumb, tw, th = make_thumbnail_with_size(rec.image_thumbnail)
            if thumb:
                rec.image_thumbnail = thumb
                # Fill in the pair when the caller did not know it (server.py
                # inserts 0/0) so the History preview can reserve its box;
                # never overwrite dimensions the caller measured itself.
                if not rec.image_width and tw:
                    rec.image_width = tw
                if not rec.image_height and th:
                    rec.image_height = th
            else:
                rec.image_thumbnail = None
                rec.image_width = None
                rec.image_height = None
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
            # REVIEW-2026-09-20 (finding 8): the old code always called
            # ``json.loads`` on the column value. SQLite gives JSON-affinity
            # columns (and TEXT columns holding valid JSON scalars) back as
            # native types, so an audit entry written as before=12 came back
            # as int 12 -> TypeError -> except -> None, silently losing the
            # value. _decode_audit_value only parses strings and adopts
            # already-decoded values as-is.
            before = _decode_audit_value(row["before"])
            after = _decode_audit_value(row["after"])
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

        # Original image entity (rca:image:{content_sha256})
        # REVIEW-2026-09-20 (finding 10): the two branches used to hash
        # DIFFERENT things — the source *path string* when ``source_file`` was
        # set, the *content* hash only for clipboard pastes. The same image
        # therefore produced two different entity ids depending on how it was
        # imported, so ``prov:used`` on the extraction activity could never be
        # joined across records and the id was not the content fingerprint its
        # name claims. Unify on the content hash: the stored ``image_sha256``
        # first, then the bytes on disk, and keep the path hash only as a
        # documented last-resort fallback.
        source_path = record.source_file or ""
        content_sha = record.image_sha256 or _file_content_sha256(source_path)
        if content_sha:
            image_entity_id = f"rca:image:{content_sha}"
        elif source_path:
            # Fallback (finding 10): the file is gone / unreadable and the
            # record carries no content hash. A path-string hash is still a
            # deterministic id, but it is NOT a content fingerprint — two
            # different images at the same path would collide, and the id is
            # indistinguishable from a content hash by shape alone. Kept only
            # so the entity id never degenerates into the bare record id.
            image_entity_id = f"rca:image:{_sha256(source_path)}"
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
            # REVIEW-2026-09-20 (finding 10): this used to also set
            # ``prov:wasGeneratedBy = activity_id`` on the ACTIVITY, i.e. the
            # edit generated itself — invalid PROV-O (wasGeneratedBy relates an
            # Entity to the Activity that generated it). The generated entity
            # already carries the correct back-link just below, so the
            # self-reference is dropped rather than moved.

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


def _file_content_sha256(path: str) -> str:
    """Return lowercase hex SHA-256 of the BYTES at ``path``; '' if unreadable.

    REVIEW-2026-09-20 (finding 10): lets ``to_prov_jsonld`` keep using a
    content fingerprint for the source-image entity even when the record was
    saved before ``image_sha256`` was populated. Streamed in 256 KB chunks so
    a phone-photo sized import cannot be blocked on memory; any OSError or
    permission error yields '' and the caller falls back explicitly.
    """
    if not path:
        return ""
    import hashlib
    try:
        if not os.path.isfile(path):
            return ""
        digest = hashlib.sha256()
        with open(path, "rb") as fh:
            while True:
                chunk = fh.read(256 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return ""
    except Exception:
        # Fail-open like the rest of the provenance builders: an exotic
        # filesystem error must not break History export.
        return ""


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
