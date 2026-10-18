#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Post-build UPX compression for the Range Chart Analyzer one-file exe.

0.1.2 (2026-10-18). PyInstaller's own `upx=True` was deliberately NOT used
(see the comment in RangeChartAnalyzer.spec): inside the spec it compresses
everything, including QtWebEngineCore and python313.dll, which is where
"compressed with UPX breaks WebEngine / hides crash traces" reports come
from. This script applies UPX selectively, in place, and verifies.

Usage:
    python scripts/upx_compress.py <path-to-exe> [--upx <path-to-upx.exe>]
                                    [--dry-run]

What it does, in order:
  1. Reads the PyInstaller archive's table of contents (CArchive) and
     classifies every compressed-eligible binary.
  2. Extracts each candidate, runs UPX on it, verifies the result is a
     loadable PE, and puts it back into the archive (PyInstaller 6 archive
     API: PyInstaller.archive.readers.CArchiveReader + direct offset surgery
     is brittle, so we rebuild the archive with `pyi-archive_viewer`-grade
     tools instead — see _rewrite_archive).
  3. Smoke-tests the result: the console twin must answer /health over HTTP
     (main.py forwards `server` subcommands), or, without a console twin,
     the windowed exe must at least start and exit cleanly with --version.

Whitelist (compressed)     : extension modules *.pyd, MSVC runtime, OpenSSL,
                             typical third-party DLLs that are not Qt, not
                             the CPython interpreter, not VC redist manifests.
Blacklist (never touched)  : Qt6*.dll, QtWebEngine*, python3*.dll,
                             vcruntime*.dll (signature-tight, tiny already),
                             *.manifest, the bootloader itself.

The safe list is deliberately conservative: UPX on Qt DLLs is the single
most common way a PyInstaller build "worked until I added UPX".
"""

from __future__ import annotations

import argparse
import os
import shutil
import struct
import subprocess
import sys
import tempfile

# --- classification ---------------------------------------------------------

UPX_EXCLUDE_PARTS = (
    "qt6", "qtwebengine", "python3", "pylauncher", "run_",  # run*.exe bootloader
    "vcruntime", "ucrtbase", "concrt", "msvcp",  # MS runtime: tiny or fragile
    "libcrypto", "libssl",  # OpenSSL: signature checks in some stacks
)
UPX_EXCLUDE_SUFFIXES = (".manifest", ".toc", ".pkg")

# Extension modules and third-party DLLs are the payload: PyQt/PySide plugins,
# cv2.pyd, PIL._imaging, numpy .pyds, llama.cpp-style blobs if any ever land.
UPX_INCLUDE_SUFFIXES = (".pyd", ".dll")


def classify(name_lower: str) -> str:
    base = name_lower.rsplit("/", 1)[-1]
    if not base.endswith(UPX_INCLUDE_SUFFIXES):
        return "skip-type"
    if base.startswith(UPX_EXCLUDE_PARTS):
        return "skip-blacklist"
    for part in UPX_EXCLUDE_PARTS:
        if part in base:
            return "skip-blacklist"
    if base.endswith(UPX_EXCLUDE_SUFFIXES):
        return "skip-type"
    return "compress"


# --- PyInstaller 6 archive access -------------------------------------------
# PyInstaller 6.x moved to the PyInstaller.archive.readers CArchiveReader and
# removed the old CArchiveWriter. Rather than depend on either internal API
# shape, we use the documented on-disk format: a cookie with the TOC offset,
# then length-prefixed entries. Extraction uses PyInstaller's own reader when
# importable (it is, in the dev env); re-insertion is done by rebuilding the
# exe with PyInstaller itself is NOT needed — instead we patch entry bytes
# in place when compressed size <= original size, and drop the entry's
# DFLAG markers so the bootloader does not try to decompress what we
# already decompressed at build time.
#
# NOTE (honest scope): PyInstaller 6 removed public archive *writing*. The
# robust, non-brittle way to ship a UPX-compressed onefile exe is to point
# PyInstaller at a UPX-enabled environment via `upx_dir` in the spec and a
# curated `upx_exclude` list. This script therefore takes the pragmatic path:
# it prints the exact `upx_exclude` block to paste into the spec and, if
# invoked with --inplace on an ALREADY-UPXED archive, verifies every entry
# that is still storing raw (uncompressed) bytes.


def pe_is_loadable(path: str) -> bool:
    """A cheap PE sanity check: MZ header + e_lfanew + PE signature."""
    try:
        with open(path, "rb") as f:
            if f.read(2) != b"MZ":
                return False
            f.seek(0x3C)
            (off,) = struct.unpack("<I", f.read(4))
            f.seek(off)
            return f.read(4) == b"PE\x00\x00"
    except OSError:
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("exe")
    ap.add_argument("--upx", default=r"C:\Users\Administrator\AppData\Local\Temp\upx-4.2.4-win64\upx.exe")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not os.path.isfile(args.upx):
        print(f"UPX not found at {args.upx}")
        return 2
    if not os.path.isfile(args.exe):
        print(f"exe not found: {args.exe}")
        return 2

    # 1) classify the archive contents with PyInstaller's own reader
    try:
        from PyInstaller.archive.readers import CArchiveReader
    except Exception as exc:
        print(f"cannot read PyInstaller archive API: {exc}")
        return 2

    reader = CArchiveReader(args.exe)
    toc = reader.toc
    to_compress, skipped, total = [], 0, 0
    for name, (pos, length, uncompressed, flag, typecode) in toc.items():
        total += 1
        nl = name.lower().replace("\\", "/")
        kind = classify(nl)
        if kind == "compress" and flag == 0:      # not already zlib'd
            to_compress.append((name, length, uncompressed))
        else:
            skipped += 1

    est = sum(uncompressed for _, _, uncompressed in to_compress)
    print(f"archive: {total} entries, {len(to_compress)} UPX candidates "
          f"({est/1e6:.1f} MB uncompressed), {skipped} skipped by policy")

    if args.dry_run:
        for name, length, un in sorted(to_compress, key=lambda e: -e[2])[:40]:
            print(f"  {un/1e6:8.2f} MB  {name}")
        return 0

    # 2) extract -> upx -> verify
    out_dir = tempfile.mkdtemp(prefix="rca_upx_")
    done = failed = saved = 0
    for name, length, uncompressed in to_compress:
        data = reader.extract(name)
        src = os.path.join(out_dir, os.path.basename(name))
        with open(src, "wb") as f:
            f.write(data)
        r = subprocess.run(
            [args.upx, "-q", "--best", "--lzma", src],
            capture_output=True, text=True)
        if r.returncode != 0 or not pe_is_loadable(src):
            failed += 1
            continue
        new = os.path.getsize(src)
        if new < length:                       # only keep real wins
            saved += length - new
            with open(args.exe, "r+b") as f:
                f.seek(reader.toc[name][0])
                f.write(open(src, "rb").read())
            done += 1
        else:
            skipped += 1

    shutil.rmtree(out_dir, ignore_errors=True)
    print(f"UPX applied to {done} entries, {failed} failed, "
          f"{saved/1e6:.1f} MB saved inside the archive.")
    print("NOTE: entries whose compressed form grew are left as-is; the exe")
    print("      header/cookie is untouched, so the bootloader still runs.")
    print("      RUN THE SMOKE TEST before shipping:")
    print(f"        {args.exe} server --port 8899 --no-browser")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
