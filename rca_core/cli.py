"""Headless extraction CLI: ``python main.py extract ...``.

WHY THIS EXISTS (GAP-ANALYSIS-2026-11-02, B1 / B2 / MVP-3)
==========================================================
Every extraction path used to require a GUI or a running HTTP server. A
researcher with 20 figures (or 200, or a script that feeds an R pipeline)
had no way to batch them, and no way to reproduce a run without clicking
the same sequence again. README's "40/40 end-to-end validation" was a
one-off MANUAL execution of scripts/_e2e_*.py, not a repeatable entry point.

This module is deliberately small: it is an ADAPTER over rca_core.extractor,
not a second extraction pipeline. It reuses the same provider store, the
same auto-mode resolver, the same evidence-chain report and the same
normalizers as the three GUIs. Divergence between "CLI output" and "GUI
output" is therefore structurally impossible.

No GUI toolkit, no display, no HTTP server. Pure stdlib + rca_core.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import mimetypes
import pathlib
import sys
from typing import Any

__all__ = ["run_cli"]

_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif",
                   ".tiff"}


def _media_type(path: pathlib.Path) -> str:
    guessed = mimetypes.guess_type(path.name)[0]
    return guessed or "image/png"


def _collect_images(values: list[str]) -> list[pathlib.Path]:
    out: list[pathlib.Path] = []
    for value in values:
        p = pathlib.Path(value).expanduser()
        if p.is_dir():
            out.extend(sorted(q for q in p.iterdir()
                              if q.suffix.lower() in _IMAGE_SUFFIXES))
        elif p.is_file():
            out.append(p)
        else:
            raise FileNotFoundError(f"image not found: {p}")
    # Stable, deduplicated order: the same command must produce the same
    # output twice (reproducibility is a research requirement, not a nicety).
    seen = set()
    unique = []
    for p in out:
        key = str(p.resolve())
        if key not in seen:
            seen.add(key)
            unique.append(p)
    return unique


def _provider():
    try:
        from .llm import ProviderStore
        return ProviderStore().load().get_current()
    except Exception as exc:  # noqa: BLE001 - report as a CLI error, not a crash
        raise RuntimeError(
            f"could not load provider configuration: {exc}. Configure an API "
            "key once in the GUI, or pass --api-key/--base-url/--model."
        ) from exc


def _write_one(dest: pathlib.Path, payload: dict[str, Any]) -> None:
    dest.write_text(json.dumps(payload, ensure_ascii=False, indent=2,
                               sort_keys=True) + "\n", encoding="utf-8")


def run_cli(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python main.py extract",
        description="Extract structured data from chart images without a GUI.",
    )
    ap.add_argument("--image", action="append", required=True, metavar="PATH",
                    help="image file or directory; repeatable")
    ap.add_argument("--out", required=True, metavar="DIR",
                    help="output directory (one <stem>.json per image)")
    ap.add_argument("--mode", default="auto",
                    help="chart mode, or 'auto' to classify each image")
    ap.add_argument("--caption", default="",
                    help="figure caption, interpolated into the prompt")
    ap.add_argument("--lang", default="auto", dest="chart_lang",
                    help="chart language hint")
    ap.add_argument("--timeout", type=int, default=120,
                    help="per-request timeout in seconds")
    ap.add_argument("--max-tokens", type=int, default=None)
    ap.add_argument("--api-key", default=None,
                    help="use a literal key instead of the stored provider")
    ap.add_argument("--base-url", default=None)
    ap.add_argument("--model", default=None)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    try:
        images = _collect_images(args.image)
    except (FileNotFoundError, OSError) as exc:
        print(f"[extract] {exc}", file=sys.stderr)
        return 2
    if not images:
        print("[extract] no images matched", file=sys.stderr)
        return 2

    out_dir = pathlib.Path(args.out).expanduser()
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"[extract] cannot create output directory: {exc}",
              file=sys.stderr)
        return 2

    # Import lazily so `--help` works on a machine with no optional deps.
    from . import __version__
    from .export_notes import as_dict as _data_dict
    from .extractor import extract, resolve_auto_mode
    from .report import build_extraction_report

    # JSON can carry the column semantics inline; CSV/TSV cannot (their bytes
    # are pinned), which is the only reason a sidecar exists for those formats.
    notes = _data_dict()

    provider = None
    if args.api_key is None:
        try:
            provider = _provider()
        except RuntimeError as exc:
            print(f"[extract] {exc}", file=sys.stderr)
            return 2
    else:
        from .llm import LlmProvider
        provider = LlmProvider(api_key=args.api_key,
                               endpoint=args.base_url or "",
                               model=args.model or "")

    failures = 0
    for index, path in enumerate(images, 1):
        try:
            raw = path.read_bytes()
            image_b64 = base64.b64encode(raw).decode("ascii")
            media_type = _media_type(path)
            mode = args.mode
            classify_note = None
            if mode == "auto":
                mode, extra = resolve_auto_mode(
                    caption=args.caption, filename=path.name,
                    image_b64=image_b64, media_type=media_type,
                    provider=provider, timeout_sec=args.timeout)
                classify_note = extra.raw if extra else None

            kwargs: dict[str, Any] = dict(
                mode=mode, image_b64=image_b64, media_type=media_type,
                caption=args.caption, chart_lang=args.chart_lang,
                timeout_sec=args.timeout, provider=provider,
                filename=path.name,
            )
            if args.max_tokens is not None:
                kwargs["max_tokens"] = args.max_tokens
            result = extract(**kwargs)

            report = None
            if result.ok and isinstance(result.data, dict):
                report = build_extraction_report(
                    data=result.data, mode=mode, lang=args.chart_lang,
                    image_sha256=result.image_sha256
                    or hashlib.sha256(raw).hexdigest(),
                    request_meta={"source": "cli", "version": __version__,
                                  "filename": path.name},
                )
            payload = {
                "image": str(path.resolve()),
                "image_sha256": hashlib.sha256(raw).hexdigest(),
                "mode": mode,
                "ok": bool(result.ok),
                "error_key": result.error_key,
                "data": result.data,
                "report": report,
                "classification_raw": classify_note,
                "confidence_note": notes["confidence_note"],
                "data_dictionary": notes["columns"],
                "version": __version__,
            }
            dest = out_dir / (path.stem + ".json")
            _write_one(dest, payload)
            if not result.ok:
                failures += 1
            if not args.quiet:
                state = "OK" if result.ok else f"FAILED ({result.error_key})"
                print(f"[{index}/{len(images)}] {path.name} -> {dest.name}: {state}")
        except Exception as exc:  # noqa: BLE001 - one bad figure must not stop a batch
            failures += 1
            print(f"[{index}/{len(images)}] {path.name}: ERROR "
                  f"{type(exc).__name__}: {exc}", file=sys.stderr)
    if not args.quiet:
        print(f"\n{len(images) - failures}/{len(images)} succeeded; "
              f"output: {out_dir}")
    return 1 if failures else 0
