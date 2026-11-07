"""``python main.py backup --out FILE.zip`` — one-shot local data backup.

ARCH-REVIEW-2026-11-02 §7 / D3. The whole product state lives in
``~/.range_chart_analyzer/`` plus ``~/.range_chart_analyzer.json``. There was
no way to back them up as a unit, and the most important file -- the Fernet
master key -- is the one users would least think to copy. Losing it makes
EVERY stored API key permanently undecryptable, while the five rotating
``providers.json.bak`` copies are useless because they are ciphertext under
that same key.

Read-only: never writes to the data directory (only creates the destination
archive). Safe to run while the app is closed; SQLite files are copied as
bytes, so close the app first for a fully consistent snapshot.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import zipfile
from datetime import datetime

#: Files worth warning about if absent.
CRITICAL = ("fernet_key.fek", "providers.json", "rca.db")


def data_dir() -> pathlib.Path:
    return pathlib.Path(os.path.expanduser("~")) / ".range_chart_analyzer"


def create_backup(dest: str | pathlib.Path, *, home: pathlib.Path | None = None) -> dict:
    """Write the zip and return a machine-readable manifest."""
    home = home or pathlib.Path(os.path.expanduser("~"))
    src = data_dir() if home == pathlib.Path(os.path.expanduser("~")) \
        else home / ".range_chart_analyzer"
    out = pathlib.Path(dest).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)

    files: list[tuple[pathlib.Path, str]] = []
    if src.is_dir():
        for p in sorted(src.rglob("*")):
            if p.is_file():
                files.append((p, str(pathlib.Path(".range_chart_analyzer") /
                                    p.relative_to(src))))
    config = home / ".range_chart_analyzer.json"
    if config.is_file():
        files.append((config, config.name))

    manifest = {
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source": str(src),
        "files": [arc for _p, arc in files],
        "contains_fernet_key": any(
            arc.endswith("fernet_key.fek") for _p, arc in files),
        "missing_critical": [n for n in CRITICAL
                             if not any(arc.endswith(n) for _p, arc in files)],
        "warning": ("This archive contains the Fernet master key and encrypted "
                    "API keys. Treat it as a password: store it encrypted, "
                    "never commit it, never attach it to an issue."),
    }
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for p, arc in files:
            z.write(p, arc)
        z.writestr("BACKUP_MANIFEST.json",
                   json.dumps(manifest, ensure_ascii=False, indent=2))
    manifest["archive"] = str(out.resolve())
    manifest["size_bytes"] = out.stat().st_size
    return manifest


def run_backup(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python main.py backup",
                                 description="Back up all local Range Chart Analyzer data.")
    ap.add_argument("--out", required=True, metavar="FILE.zip")
    ap.add_argument("--home", default=None, metavar="DIR",
                    help="override HOME (mainly for tests)")
    args = ap.parse_args(argv)
    home = pathlib.Path(args.home).expanduser() if args.home else None
    try:
        manifest = create_backup(args.out, home=home)
    except OSError as exc:
        print(f"[backup] failed: {exc}")
        return 2
    print(f"[backup] wrote {manifest['archive']} "
          f"({manifest['size_bytes'] / 1024:.1f} KiB, "
          f"{len(manifest['files'])} files)")
    if manifest["missing_critical"]:
        print("[backup] WARNING missing: "
              + ", ".join(manifest["missing_critical"]))
    print("[backup] " + manifest["warning"])
    return 0


__all__ = ["create_backup", "run_backup", "data_dir"]
