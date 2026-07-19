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
    raw: str = ""            # raw model response (truncated to ~8KB on save)
    confidence: float = 0.0
    partial_failures: int = 0
    duration_ms: int = 0
    status_code: int | None = None
    notes: str = ""          # user-editable, used for tagging / commenting

    def to_dict(self) -> dict[str, Any]:
        """Serialize for JSON export. Thumbnail goes to base64."""
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
        }


def _row_to_record(row) -> HistoryRecord:
    result = json.loads(row["result_json"]) if row["result_json"] else {}
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
    )


# Cap raw response body stored per record to keep the DB small.
_MAX_RAW_BYTES = 8 * 1024


class HistoryStore:
    """CRUD over the history table."""

    def __init__(self, db: Database | None = None) -> None:
        self.db = db or Database()

    # ---- writes ----

    def add(self, rec: HistoryRecord) -> int:
        if not rec.timestamp:
            rec.timestamp = time.time()
        # Bug-8 fix: shrink the thumbnail to the standard size before
        # storage so a caller passing a giant preview doesn't blow up
        # the DB. Idempotent — already-small thumbnails pass through.
        if rec.image_thumbnail:
            rec.image_thumbnail = make_thumbnail(rec.image_thumbnail)
        raw = rec.raw if len(rec.raw) <= _MAX_RAW_BYTES else rec.raw[:_MAX_RAW_BYTES]
        cur = self.db.execute(
            """INSERT INTO history (
                timestamp, source_file, image_thumbnail, image_width, image_height,
                provider_id, provider_name, model, mode, runs,
                result_json, raw_json, confidence, partial_failures,
                duration_ms, status_code, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                rec.timestamp, rec.source_file, rec.image_thumbnail,
                rec.image_width, rec.image_height,
                rec.provider_id, rec.provider_name, rec.model, rec.mode, rec.runs,
                json.dumps(rec.result, ensure_ascii=False), raw,
                rec.confidence, rec.partial_failures,
                rec.duration_ms, rec.status_code, rec.notes,
            ),
        )
        rec.id = int(cur.lastrowid)
        # Bug-8 fix: LRU eviction. Keep at most MAX_HISTORY_ROWS rows;
        # delete the oldest when we exceed the cap. This caps total DB
        # size to roughly MAX_HISTORY_ROWS * THUMBNAIL_MAX_BYTES ≈ 10 MB
        # even after months of daily use.
        self._enforce_row_cap()
        return rec.id

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

    def update_result(self, record_id: int, result: dict[str, Any]) -> bool:
        cur = self.db.execute(
            "UPDATE history SET result_json = ? WHERE id = ?",
            (json.dumps(result, ensure_ascii=False), record_id),
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
