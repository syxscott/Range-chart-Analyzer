"""Make silent optional-dependency degradations visible in the evidence chain.

WHY THIS EXISTS
===============
ARCH-REVIEW-2026-11-02 §5.4 identified the failure mode that matters most
for a tool whose results get published: not a crash, but **a promised
verification step silently not running while the user believes it ran**.

README advertises "range-chart redraw verification" as a research-trust
feature. ``rca_core/redraw.py`` wraps its matplotlib import in
``except Exception: return None``. With matplotlib absent, the verification
never executes and nothing in the result says so. The same shape occurs in
grid/matrix (numpy), deskew/extractor (cv2), and secrets_store (cryptography
/ keyring).

The report already had the right pattern -- ``empty_tables`` honestly records
"we could not read this part of the figure" instead of inventing content.
This module extends that pattern to the ENVIRONMENT. Each entry says which
capability was lost, why, and which result dimension it affects, so a
researcher reading the evidence chain can tell "verified" from "unverifiable".

Pure stdlib, no imports of rca_core siblings (avoids adding import edges).
"""
from __future__ import annotations

import importlib.util
from typing import Any

#: (import name, code, impact, affected result dimensions)
_RULES = (
    ("matplotlib", "redraw.unavailable",
     "延限图重绘校验未执行；结果未经该校验核对", ("verification",)),
    ("numpy", "grid.geometry_unavailable",
     "规则网格几何检测降级为单遍路径；occurrence matrix 未经几何验证",
     ("geometry", "matrix")),
    ("cv2", "image.enhancement_unavailable",
     "倾斜校正与图像增强回退 PIL；手写/拍摄图版的小字识读可能变差",
     ("image_preprocessing",)),
    ("cryptography", "encryption.unavailable",
     "API Key 未做 Fernet 静态加密", ("security",)),
    ("keyring", "encryption.keyring_unavailable",
     "Fernet 主密钥存于本地文件而非 OS 凭据库", ("security",)),
    ("PIL", "image.pillow_unavailable",
     "缩略图、客户端压缩与图像增强不可用", ("image_preprocessing",)),
    ("openpyxl", "export.xlsx_unavailable",
     "XLSX 导出不可用", ("export",)),
)


def _installed(name: str) -> bool:
    """Presence check only -- never import the module (no side effects)."""
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def collect_degradations() -> list[dict[str, Any]]:
    """Return one entry per capability that is silently unavailable now.

    Returns an empty list on a fully provisioned machine. The list is part of
    the extraction report, so it travels with the result into history,
    exports and the evidence chain.
    """
    out: list[dict[str, Any]] = []
    for name, code, impact, affects in _RULES:
        if _installed(name):
            continue
        out.append({
            "code": code,
            "reason": f"optional dependency '{name}' not installed",
            "impact": impact,
            "affects": list(affects),
        })
    return out


__all__ = ["collect_degradations"]
