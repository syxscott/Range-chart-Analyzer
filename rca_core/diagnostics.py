"""``python main.py doctor`` — a read-only health report for the user's install.

WHY THIS EXISTS
===============
Three problems shared one missing surface (ARCH-REVIEW-2026-11-02 §5/§7,
GAP-ANALYSIS A8/A9):

1. Seven optional dependencies degrade SILENTLY. The worst is matplotlib:
   README advertises "range-chart redraw verification" as a research-trust
   feature, but when matplotlib is absent the verification simply does not
   run and the user has no way to know. `rca_core/grid.py` similarly falls
   back to a single-pass path when numpy is missing. The user sees a result,
   not a warning.
2. ``secrets_store.encryption_status()`` exists and its own docstring says
   the RuntimeWarning is "invisible in a windowed app" -- but no UI called
   it, so a user whose API keys are protected only by a local key file was
   never told.
3. The data directory has no diagnostics. A corrupt ``rca.db`` is swallowed
   by an ``except Exception`` upstream and shows up as an EMPTY history
   list, so the user believes the records were deleted rather than unreadable.

The report is deliberately stdlib-only and NEVER creates directories, files,
keys, or database connections in read-write mode. It is safe to run on a
machine you are about to publish results from.
"""
from __future__ import annotations

import importlib.util
import os
import sqlite3
import sys
from pathlib import Path

#: (import name, what is lost when absent, is it a scientific-correctness risk)
DEPENDENCIES = (
    ("numpy", "规则网格几何检测 / occurrence matrix 两阶段", True),
    ("cv2", "倾斜校正与图像增强（回退 PIL）", False),
    ("matplotlib", "延限图重绘校验 / Usage 图表", True),
    ("PIL", "缩略图、客户端压缩、图像增强", False),
    ("openpyxl", "XLSX 导出", False),
    ("cryptography", "API Key 的 Fernet 静态加密", True),
    ("keyring", "OS 凭据库存放主密钥", True),
    ("sv_ttk", "Tkinter Fluent 主题", False),
    ("webview", "原生窗口模式", False),
    ("PySide6", "Fluent 桌面 GUI", False),
)

#: Files in ~/.range_chart_analyzer/, with what is lost if they are unreadable.
DATA_FILES = (
    ("providers.json", "provider 配置与加密后的 API Key", True),
    ("fernet_key.fek", "Fernet 主密钥（丢失 = 所有已存 API Key 无法解密）", True),
    ("rca.db", "历史记录、缩略图、图像指纹、证据链报告", True),
    ("extract_cache.sqlite", "提取缓存（可重建，只是重跑要花钱）", False),
    ("lock", "文件锁", False),
    ("salt", "legacy 密钥派生 salt", False),
)


def _data_dir() -> Path:
    return Path(os.path.expanduser("~")) / ".range_chart_analyzer"


def _sqlite_status(path: Path) -> str:
    """Read-only PRAGMA quick_check; never creates or repairs the file."""
    if not path.is_file():
        return "不存在（首次运行时创建）"
    try:
        uri = f"file:{path.as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=2)
        try:
            row = conn.execute("PRAGMA quick_check").fetchone()
            return "可读" if row and row[0] == "ok" else f"损坏：{row[0] if row else '无响应'}"
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 - report, never crash the doctor
        return f"无法读取：{type(exc).__name__}: {exc}"


def _schema_version_status(path: Path) -> tuple[str, str | None]:
    """Read-only comparison of the on-disk schema version with this build.

    P1-8 (2026-11): an older program opening a database written by a NEWER
    one used to lower the stored version, silently breaking the next
    upgrade's migration chain. ``rca_core.db.Database`` now refuses to do
    that and records ``schema_warning`` instead; doctor must be able to see
    the same condition WITHOUT opening the database read-write (which would
    itself run the migrations it is trying to warn about).

    Returns ``(line_to_print, issue_or_None)``. Returns ``("", None)`` when
    the file does not exist, has no ``_schema_version`` table (a legacy
    pre-versioning database), or cannot be read — the ``quick_check`` line
    already reports actual damage, and a missing version is not a downgrade.

    This function NEVER calls ``Database()`` and NEVER writes: the only
    SQLite access is a ``mode=ro`` URI on a file that already exists.
    """
    if not path.is_file():
        return "", None
    try:
        # Imported lazily and purely as data: rca_core.db has no import-time
        # side effects (no directory creation, no connect). Do NOT construct
        # a Database here — that would migrate the very file we are checking.
        from .db import Database
        target = int(Database._CURRENT_SCHEMA_VERSION)
    except Exception:  # noqa: BLE001 - doctor must never crash
        return "", None
    try:
        uri = f"file:{path.as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=2)
        try:
            row = conn.execute(
                "SELECT version FROM _schema_version LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 - no table / unreadable -> not a downgrade
        return "", None
    if row is None:
        return "", None
    try:
        current = int(row[0])
    except (TypeError, ValueError, IndexError):
        return "", None
    if current > target:
        msg = (f"磁盘 schema v{current} 高于本程序 v{target}（降级运行）"
               f" — 请升级到最新版，或备份 {path.name} 后不要继续写入")
        issue = (f"rca.db: 磁盘 schema v{current} > 本程序 v{target}，"
                 "当前程序版本较旧；已保留原版本号，请升级到最新版")
        return msg, issue
    return f"schema v{current} / 本程序 v{target}", None


def _encryption_status() -> str:
    try:
        from .secrets_store import encryption_status
        return encryption_status()
    except Exception as exc:  # noqa: BLE001
        return f"未知（{type(exc).__name__}）"


def run_doctor(stream=None) -> int:
    """Print the report. Returns 0 when nothing needs attention, 1 otherwise.

    ``stream`` is injectable so tests can capture the output without a GUI.
    """
    out = stream or sys.stdout
    issues: list[str] = []
    data_dir = _data_dir()

    print("=== Range Chart Analyzer 体检 ===", file=out)
    try:
        from . import __version__
    except Exception:
        __version__ = "?"
    print(f"版本：{__version__}   Python：{sys.version.split()[0]}   "
          f"平台：{sys.platform}", file=out)
    print(f"数据目录：{data_dir}", file=out)
    print("", file=out)

    print("--- 依赖（缺失时的科学影响）---", file=out)
    for name, purpose, correctness in DEPENDENCIES:
        present = importlib.util.find_spec(name) is not None
        mark = "OK " if present else ("!! " if correctness else "-- ")
        if present:
            print(f"  [{mark}] {name:14} 已安装 — {purpose}", file=out)
        else:
            note = " ← 影响结果可信度" if correctness else ""
            print(f"  [{mark}] {name:14} 未安装 — {purpose} 不可用{note}", file=out)
            if correctness:
                issues.append(f"缺少 {name}：{purpose} 不会执行且没有提示")
    print("", file=out)

    print("--- 数据文件 ---", file=out)
    if not data_dir.is_dir():
        print("  [--] 数据目录尚未创建（首次使用时建立）", file=out)
    else:
        for name, purpose, critical in DATA_FILES:
            path = data_dir / name
            if not path.exists():
                print(f"  [--] {name:24} 不存在 — {purpose}", file=out)
                continue
            state = (_sqlite_status(path) if name.endswith((".db", ".sqlite"))
                     else ("可读" if os.access(path, os.R_OK) else "不可读"))
            size = path.stat().st_size
            # secrets_store.py:348 refuses to overwrite an EMPTY key file
            # (it raises rather than silently regenerating and orphaning
            # every stored key). While keyring is active that empty file is
            # never read, so it is harmless today -- but if keyring ever
            # becomes unavailable the user hits a RuntimeError with no hint
            # about which file to remove. Say so instead of printing "可读".
            if name == "fernet_key.fek" and size == 0:
                state = "空文件（用 keyring 时无影响；keyring 失效需删除重建）"
            ok = state == "可读" or state.startswith(("不存在", "空文件"))
            if not ok and critical:
                issues.append(f"{name}: {state} — {purpose}")
            print(f"  [{'OK ' if ok else '!! '}] {name:24} {state}  "
                  f"{size / 1024:.1f} KiB — {purpose}", file=out)
        # P1-8: a NEWER on-disk schema than this build is a downgrade run.
        # The check is read-only and never creates/migrates the file, so it
        # is safe even on a machine whose history must not be touched.
        sch_line, sch_issue = _schema_version_status(data_dir / "rca.db")
        if sch_line:
            mark = "!! " if sch_issue else "OK "
            print(f"  [{mark}] {'rca.db schema':24} {sch_line}", file=out)
        if sch_issue:
            issues.append(sch_issue)
    print("", file=out)

    enc = _encryption_status()
    enc_note = {
        "keyring": "安全：主密钥在 OS 凭据库",
        "fingerprint": "较弱：主密钥是本地文件 fernet_key.fek，"
                       "同机其他进程可读；建议安装 keyring",
        "plaintext": "无加密：未安装 cryptography，API Key 仅做混淆",
    }.get(enc, "未知状态，请检查 rca_core/secrets_store.py")
    print("--- 凭据保护 ---", file=out)
    print(f"  encryption_status = {enc} — {enc_note}", file=out)
    if enc != "keyring":
        issues.append(f"API Key 保护级别：{enc}")
    print("", file=out)

    config = Path(os.path.expanduser("~")) / ".range_chart_analyzer.json"
    print("--- 设置 ---", file=out)
    print(f"  {config}: {'存在' if config.is_file() else '不存在'}", file=out)
    print("", file=out)

    if issues:
        print(f"=== 需要关注 {len(issues)} 项 ===", file=out)
        for i, issue in enumerate(issues, 1):
            print(f"  {i}. {issue}", file=out)
        print("\n依赖修复：pip install -r requirements.txt -c constraints.txt",
              file=out)
        print("历史数据库损坏时：先备份 ~/.range_chart_analyzer/rca.db，"
              "再删除并重启（历史记录会清空，无法自动恢复）；"
              "新版会自动把损坏文件保留为 rca.db.corrupt-<UTC> 而不删除", file=out)
        return 1
    print("=== 体检通过 ===", file=out)
    return 0


__all__ = ["run_doctor", "DEPENDENCIES", "DATA_FILES"]
