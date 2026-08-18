"""ICS 2023/2024 International Chronostratigraphic Chart lookup.

Provides functions to map stage names to/from Ma ages, compare stage
order, and parse age range strings.
"""

from __future__ import annotations

import json
import re
import warnings
from pathlib import Path
from typing import Any, Optional


# M7(b) (REVIEW-2026-07-25): degrade gracefully when the bundled ICS 2024
# table is missing or corrupt. Previously a failing json.loads here crashed
# the whole module import, which took down every consumer (quality.py,
# darwin_core.py, pbdb.py). We now surface a warning and fall back to an
# empty table so age lookups simply no-op instead of breaking the app.
try:
    _ICS_PATH = Path(__file__).parent.parent / "resources" / "ics_2024.json"
    ICS_2024: dict[str, dict] = json.loads(_ICS_PATH.read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError) as _exc:  # pragma: no cover - resource integrity
    warnings.warn(f"ICS 2024 table unavailable; age lookups disabled: {_exc}")
    ICS_2024 = {}


def ics_stage_from_age(ma: float) -> Optional[str]:
    """Given a Ma value, return the corresponding ICS Stage name.


    Traverses all stages in ICS_2024 and returns the stage whose
    age range contains the given Ma value.
    In geological convention: base_ma is older (larger number),
    top_ma is younger (smaller number).
    Returns None if no stage matches.
    """
    for name, info in ICS_2024.items():
        top = info.get("top_ma", 0)
        base = info.get("base_ma", 0)
        # In geological convention, base > top (older > younger)
        # So we check: top <= ma <= base
        if top <= ma <= base:
            return name
    return None


def ics_age_compare(stage1: str, stage2: str) -> Optional[int]:
    """Compare two stages by relative stratigraphic order.

    
    Returns:
    -1 if stage1 is older (lower in section, older Ma value)
     0 if stage1 and stage2 are the same stage
     1 if stage1 is younger (higher in section, younger Ma value)
     None if either stage is unknown
    
    Note: In stratigraphic convention, OLDER rocks have HIGHER Ma values.
    So a stage with base_ma=1200 is older than one with top_ma=1000.
    """
    s1 = ICS_2024.get(stage1)
    s2 = ICS_2024.get(stage2)
    if s1 is None or s2 is None:
        return None
    # Compare by mid-point of age range. In stratigraphic convention
    # older = higher Ma. Note: for two adjacent stages in a continuous
    # ladder (e.g. Danian 61.6-66.0 vs Thanetian 56.0-59.2) midpoint is
    # an imperfect proxy; works correctly for non-adjacent stages and
    # in conjunction with the upstream quality.py `_score_cross_era_accuracy`
    # which iterates stages in text order.
    mid1 = (s1.get("base_ma", 0) + s1.get("top_ma", 0)) / 2
    mid2 = (s2.get("base_ma", 0) + s2.get("top_ma", 0)) / 2
    if mid1 > mid2:
        return -1  # stage1 is older (higher Ma)
    elif mid1 < mid2:
        return 1   # stage1 is younger (lower Ma)
    else:
        return 0


# M7(a) (REVIEW-2026-07-25): precompile one regex per stage name ONCE at
# module load instead of recompiling inside every ics_parse_age_range call
# (the old loop was O(stages^2) per invocation and dominated parsing cost).
_STAGE_PATTERNS: list[tuple[str, "re.Pattern[str]"]] = [
    (name, re.compile(r"\b" + re.escape(name) + r"\b", re.IGNORECASE))
    for name in ICS_2024.keys()
]


def ics_parse_age_range(text: str) -> list[str]:
    """Parse a free-text age_range string into a list of Stage names.


    Handles formats like:
    - "Late Permian (Wuchiapingian - Changhsingian)" -> ["Wuchiapingian", "Changhsingian"]
    - "Wuchiapingian" -> ["Wuchiapingian"]
    - "Early Triassic" -> [] (no specific stage)

    Returns a list of stage names found in the text (may be empty).
    The stages are returned in the order they appear in the text.
    """
    if not text:
        return []

    # Find all stage matches with their positions in the text.
    matches: list[tuple[int, str]] = []
    for stage_name, pattern in _STAGE_PATTERNS:
        for m in pattern.finditer(text):
            matches.append((m.start(), stage_name))

    # Sort by position in text (first appearance first)
    matches.sort(key=lambda x: x[0])
    return [name for _, name in matches]


_EXPLICIT_MA_PATTERN = re.compile(
    r"(?<![\w.])([+]?(?:\d+(?:\.\d*)?|\.\d+))\s*"
    r"(?:Ma|Myr|Mya|m\.\s*y\.?|million\s+years?(?:\s+ago)?)\b",
    re.IGNORECASE,
)

# REVIEW-2026-07-31: also match single-unit ranges like "259.51-254.14 Ma"
# or "255 to 250 Ma" so a bound written as an interval resolves to BOTH
# ends (callers pick the older end for FAD/base, the younger for LAD/top).
_EXPLICIT_MA_RANGE_PATTERN = re.compile(
    r"(?<![\w.])"
    r"([+]?(?:\d+(?:\.\d*)?|\.\d+))\s*"
    r"(?:[-–—]|\bto\b)\s*"
    r"([+]?(?:\d+(?:\.\d*)?|\.\d+))\s*"
    r"(?:Ma|Myr|Mya|m\.\s*y\.?|million\s+years?(?:\s+ago)?)\b",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# REVIEW-2026-07-31: curated chronostratigraphic label maps.
#
# The ICS table only carries stage names. Real range charts also label
# sections/species with series and epoch terms ("Late Permian",
# "Pleistocene"), Chinese stage names ("吴家坪阶"), and bare period names
# ("Permian"). These maps let the export path resolve those labels to
# numeric ages instead of silently blanking the fields.
# ---------------------------------------------------------------------------

# Chinese stage-name aliases -> canonical table key. Curated for the stages
# most common in Chinese-language range charts.
_CN_STAGE_ALIASES: dict[str, str] = {
    "吴家坪阶": "Wuchiapingian", "长兴阶": "Changhsingian",
    "卡匹敦阶": "Capitanian", "沃德阶": "Wordian", "罗德阶": "Roadian",
    "空谷阶": "Kungurian", "亚丁斯克阶": "Artinskian",
    "萨克马尔阶": "Sakmarian", "阿瑟尔阶": "Asselian",
    "格舍尔阶": "Gzhelian", "卡西莫夫阶": "Kasimovian", "莫斯科阶": "Moscovian",
    "巴什基尔阶": "Bashkirian", "谢尔普霍夫阶": "Serpukhovian",
    "维宪阶": "Visean", "杜内阶": "Tournaisian",
    "法门阶": "Famennian", "弗拉斯阶": "Frasnian", "吉维特阶": "Givetian",
    "艾菲尔阶": "Eifelian", "埃姆斯阶": "Emsian", "布拉格阶": "Pragian",
    "洛赫科夫阶": "Lochkovian",
    "普里多利统": "Pridoli", "拉德洛统": "Ludlow", "文洛克统": "Wenlock",
    "兰多维列统": "Llandovery",
    "赫南特阶": "Hirnantian", "凯迪阶": "Katian", "桑比阶": "Sandbian",
    "达瑞威尔阶": "Darriwilian", "大坪阶": "Dapingian", "弗洛阶": "Floian",
    "特马豆克阶": "Tremadocian",
    "排碧阶": "Paibian", "古丈阶": "Guzhangian", "鼓山阶": "Drumian",
    "乌溜阶": "Stage 5",
    "格拉斯阶": "Gelasian", "卡拉布里阶": "Calabrian", "基班期": "Chibanian",
    "皮亚琴察阶": "Piacenzian", "赞克尔阶": "Zanclean", "梅辛阶": "Messinian",
    "托尔托纳阶": "Tortonian", "塞拉瓦莱阶": "Serravallian", "兰盖阶": "Langhian",
    "布尔迪加尔阶": "Burdigalian", "阿基坦阶": "Aquitanian",
    "夏特阶": "Chattian", "鲁培尔阶": "Rupelian", "普里阿邦阶": "Priabonian",
    "巴尔托阶": "Bartonian", "卢泰特阶": "Lutetian", "伊普雷斯阶": "Ypresian",
    "塞兰特阶": "Selandian", "丹尼阶": "Danian",
    "马斯特里赫特阶": "Maastrichtian", "坎潘阶": "Campanian", "圣通阶": "Santonian",
    "科尼亚克阶": "Coniacian", "土伦阶": "Turonian", "塞诺曼阶": "Cenomanian",
    "阿尔布阶": "Albian", "阿普特阶": "Aptian", "巴雷姆阶": "Barremian",
    "欧特里夫阶": "Hauterivian", "瓦兰金阶": "Valanginian", "贝里阿斯阶": "Berriasian",
    "提通阶": "Tithonian", "基默里奇阶": "Kimmeridgian", "牛津阶": "Oxfordian",
    "卡洛夫阶": "Callovian", "巴通阶": "Bathonian", "巴柔阶": "Bajocian",
    "阿林阶": "Aalenian", "托阿尔阶": "Toarcian", "普林斯巴赫阶": "Pliensbachian",
    "辛涅缪尔阶": "Sinemurian", "赫塘阶": "Hettangian",
    "瑞替阶": "Rhaetian", "诺利阶": "Norian", "卡尼阶": "Carnian",
    "拉丁阶": "Ladinian", "安尼阶": "Anisian", "奥伦尼克阶": "Olenekian",
    "印度阶": "Induan",
}

# Series/epoch labels -> (canonical interval name, [stage names oldest first]).
# Bounds are computed from the table itself, so they stay consistent with
# the stage data above.
_SERIES_STAGE_LISTS: dict[str, tuple[str, list[str]]] = {
    "early permian": ("Cisuralian", ["Asselian", "Sakmarian", "Artinskian", "Kungurian"]),
    "middle permian": ("Guadalupian", ["Roadian", "Wordian", "Capitanian"]),
    "late permian": ("Lopingian", ["Wuchiapingian", "Changhsingian"]),
    "early triassic": ("Lower Triassic", ["Induan", "Olenekian"]),
    "middle triassic": ("Middle Triassic", ["Anisian", "Ladinian"]),
    "late triassic": ("Upper Triassic", ["Carnian", "Norian", "Rhaetian"]),
    "early jurassic": ("Lower Jurassic", ["Hettangian", "Sinemurian", "Pliensbachian", "Toarcian"]),
    "middle jurassic": ("Middle Jurassic", ["Aalenian", "Bajocian", "Bathonian", "Callovian"]),
    "late jurassic": ("Upper Jurassic", ["Oxfordian", "Kimmeridgian", "Tithonian"]),
    "lower cretaceous": ("Lower Cretaceous", ["Berriasian", "Valanginian", "Hauterivian", "Barremian", "Aptian", "Albian"]),
    "early cretaceous": ("Lower Cretaceous", ["Berriasian", "Valanginian", "Hauterivian", "Barremian", "Aptian", "Albian"]),
    "upper cretaceous": ("Upper Cretaceous", ["Cenomanian", "Turonian", "Coniacian", "Santonian", "Campanian", "Maastrichtian"]),
    "late cretaceous": ("Upper Cretaceous", ["Cenomanian", "Turonian", "Coniacian", "Santonian", "Campanian", "Maastrichtian"]),
    "paleocene": ("Paleocene", ["Danian", "Selandian", "Thanetian"]),
    "eocene": ("Eocene", ["Ypresian", "Lutetian", "Bartonian", "Priabonian"]),
    "oligocene": ("Oligocene", ["Rupelian", "Chattian"]),
    "miocene": ("Miocene", ["Aquitanian", "Burdigalian", "Langhian", "Serravallian", "Tortonian", "Messinian"]),
    "pliocene": ("Pliocene", ["Zanclean", "Piacenzian"]),
    "pleistocene": ("Pleistocene", ["Gelasian", "Calabrian", "Chibanian"]),
    "early ordovician": ("Lower Ordovician", ["Tremadocian", "Floian"]),
    "middle ordovician": ("Middle Ordovician", ["Dapingian", "Darriwilian"]),
    "late ordovician": ("Upper Ordovician", ["Sandbian", "Katian", "Hirnantian"]),
    "early devonian": ("Lower Devonian", ["Lochkovian", "Pragian", "Emsian"]),
    "middle devonian": ("Middle Devonian", ["Eifelian", "Givetian"]),
    "late devonian": ("Upper Devonian", ["Frasnian", "Famennian"]),
    "mississippian": ("Mississippian", ["Tournaisian", "Visean", "Serpukhovian"]),
    "early carboniferous": ("Mississippian", ["Tournaisian", "Visean", "Serpukhovian"]),
    "pennsylvanian": ("Pennsylvanian", ["Bashkirian", "Moscovian", "Kasimovian", "Gzhelian"]),
    "late carboniferous": ("Pennsylvanian", ["Bashkirian", "Moscovian", "Kasimovian", "Gzhelian"]),
    "early silurian": ("Llandovery", ["Llandovery"]),
    "middle silurian": ("Wenlock", ["Wenlock"]),
    "late silurian": ("Ludlow", ["Ludlow"]),
    "lower cambrian": ("Lower Cambrian", ["Fortunian", "Series 2"]),
    "early cambrian": ("Lower Cambrian", ["Fortunian", "Series 2"]),
    "middle cambrian": ("Middle Cambrian", ["Stage 5", "Drumian", "Guzhangian"]),
    "furongian": ("Furongian", ["Paibian", "Stage 9", "Stage 10"]),
    "late cambrian": ("Furongian", ["Paibian", "Stage 9", "Stage 10"]),
}

# REVIEW-2026-07-31: series/epoch labels whose bounds cannot be derived
# from named stages alone. The Pleistocene spans 2.588-0.0117 Ma, but its
# formally named stages (Gelasian, Calabrian, Chibanian) end at 0.129 Ma —
# the upper part (0.129-0.0117) has no formal stage. Without the override
# the series bounds would truncate the last 0.117 Myr.
_SERIES_EXPLICIT_BOUNDS: dict[str, tuple[float, float]] = {
    "pleistocene": (2.588, 0.0117),
}


def _series_bounds_for(label: str, stage_names: list[str]) -> tuple[Optional[float], Optional[float]]:
    """Return ``(older_ma, younger_ma)`` for a series/epoch label.

    Uses ``_SERIES_EXPLICIT_BOUNDS`` when present (Pleistocene spans
    2.588-0.0117 but its named stages end at 0.129), otherwise derives
    from the first stage's base and the last stage's top.
    """
    explicit = _SERIES_EXPLICIT_BOUNDS.get(label)
    if explicit is not None:
        return explicit
    first = ICS_2024.get(stage_names[0])
    last = ICS_2024.get(stage_names[-1])
    if first is None or last is None:
        return None, None
    return (first.get("base_ma", 0) or 0), (last.get("top_ma", 0) or 0)


_CN_SERIES_ALIASES: dict[str, str] = {
    "早二叠世": "early permian", "中二叠世": "middle permian", "晚二叠世": "late permian",
    "早三叠世": "early triassic", "中三叠世": "middle triassic", "晚三叠世": "late triassic",
    "早侏罗世": "early jurassic", "中侏罗世": "middle jurassic", "晚侏罗世": "late jurassic",
    "早白垩世": "early cretaceous", "晚白垩世": "late cretaceous",
    "古新世": "paleocene", "始新世": "eocene", "渐新世": "oligocene",
    "中新世": "miocene", "上新世": "pliocene", "更新世": "pleistocene",
    "早奥陶世": "early ordovician", "中奥陶世": "middle ordovician", "晚奥陶世": "late ordovician",
    "早泥盆世": "early devonian", "中泥盆世": "middle devonian", "晚泥盆世": "late devonian",
    "早石炭世": "early carboniferous", "晚石炭世": "late carboniferous",
    "早寒武世": "early cambrian", "中寒武世": "middle cambrian", "晚寒武世": "late cambrian",
}

# Period-level fallback: label -> canonical period name. Bounds come from the
# table's per-stage period_base_ma / period_top_ma fields.
_EN_PERIOD_ALIASES: dict[str, str] = {
    "permian": "Permian", "triassic": "Triassic", "jurassic": "Jurassic",
    "cretaceous": "Cretaceous", "paleogene": "Paleogene", "neogene": "Neogene",
    "quaternary": "Quaternary", "carboniferous": "Carboniferous",
    "devonian": "Devonian", "silurian": "Silurian", "ordovician": "Ordovician",
    "cambrian": "Cambrian",
}
_CN_PERIOD_ALIASES: dict[str, str] = {
    "二叠纪": "Permian", "三叠纪": "Triassic", "侏罗纪": "Jurassic",
    "白垩纪": "Cretaceous", "古近纪": "Paleogene", "新近纪": "Neogene",
    "第四纪": "Quaternary", "石炭纪": "Carboniferous", "泥盆纪": "Devonian",
    "志留纪": "Silurian", "奥陶纪": "Ordovician", "寒武纪": "Cambrian",
}

# period name -> (base_ma, top_ma) built once from the table.
_PERIOD_BOUNDS: dict[str, tuple[float, float]] = {}
for _name, _info in ICS_2024.items():
    _period = _info.get("period")
    if _period and _period not in _PERIOD_BOUNDS:
        _PERIOD_BOUNDS[_period] = (
            _info.get("period_base_ma", 0) or 0,
            _info.get("period_top_ma", 0) or 0,
        )


def _explicit_ma_values(text: str) -> list[float]:
    """All explicit numeric ages in *text* (unit required), range-aware.

    Collects both single values ("260 Ma") and both ends of single-unit
    ranges ("259.51-254.14 Ma", "255 to 250 Ma").
    """
    values: list[float] = []
    for m in _EXPLICIT_MA_PATTERN.finditer(text):
        try:
            values.append(float(m.group(1)))
        except ValueError:
            continue
    for m in _EXPLICIT_MA_RANGE_PATTERN.finditer(text):
        try:
            values.append(float(m.group(1)))
            values.append(float(m.group(2)))
        except ValueError:
            continue
    return values


def ics_resolve_age_bound(
    text: Any, prefer: str = "older"
) -> tuple[Optional[str], Optional[float]]:
    """Resolve a single age/stage label to ``(interval_name, ma)``.

    ``interval_name`` is a canonical ICS interval — a stage name
    ("Wuchiapingian"), a series/epoch name ("Lopingian", "Pleistocene"),
    or a period name ("Permian"). ``ma`` is the resolved numeric age.

    ``prefer`` selects which end of an interval literal to use when the
    label is a range: ``"older"`` (default) returns the older end
    (259.51 for "259.51-254.14 Ma"), ``"younger"`` the younger end.

    Numeric ages are accepted only when an explicit geologic-age unit is
    present (for example ``"260 Ma"`` or ``"260 Myr"``). Bare numbers are
    deliberately rejected because range-chart bounds commonly contain bed,
    sample, figure, and column identifiers that are not absolute ages.

    Resolution order:
      1. an explicit numeric age literal (single value or range);
      2. a Chinese stage-name alias (e.g. ``"吴家坪阶"``);
      3. a series/epoch label (English or Chinese, e.g. "Late Permian",
         ``"晚二叠世"``, "Pleistocene") -> interval name + the requested
         bound;
      4. an ICS stage name -> that stage, Ma as the stage midpoint;
      5. a period name (English or Chinese) -> period name + the requested
         period bound.

    Returns ``(None, None)`` when ``text`` is empty/unresolvable or the ICS
    table failed to load, so callers can leave unsupported fields blank.
    """
    if not text or not ICS_2024:
        return None, None
    text = str(text).strip()

    # 1. Explicit numeric ages (range-aware).
    values = _explicit_ma_values(text)
    if values:
        ma = max(values) if prefer == "older" else min(values)
        return ics_stage_from_age(ma), ma

    # 2. Chinese stage-name alias.
    for alias, stage in _CN_STAGE_ALIASES.items():
        if alias in text:
            info = ICS_2024.get(stage)
            if info:
                base = info.get("base_ma", 0) or 0
                top = info.get("top_ma", 0) or 0
                return stage, (base + top) / 2

    # 3. Series/epoch labels (English then Chinese).
    norm = text.lower()
    for label, (name, stages) in _SERIES_STAGE_LISTS.items():
        if re.search(r"\b" + re.escape(label) + r"\b", norm):
            older, younger = _series_bounds_for(label, stages)
            if older is not None:
                return name, older if prefer == "older" else younger
    for alias, label in _CN_SERIES_ALIASES.items():
        if alias in text:
            name, stages = _SERIES_STAGE_LISTS[label]
            older, younger = _series_bounds_for(label, stages)
            if older is not None:
                return name, older if prefer == "older" else younger

    # 4. ICS stage name(s) -> order-independent range bounds.
    # P0-2 fix (REVIEW-2026-08-17): a stage RANGE ("Wuchiapingian -
    # Changhsingian") must honor ``prefer`` and use the max-of-bases /
    # min-of-tops convention from ``ics_age_range_bounds``, NOT the
    # midpoint of whichever stage happens to appear first in the input.
    # The previous implementation returned stages[0]'s midpoint
    # regardless of prefer, so prefer="younger" on a reversed-order
    # text ("Changhsingian - Wuchiapingian") returned the OLDER stage's
    # midpoint — silently contaminating FAD/LAD Ma columns in DwC/PBDB
    # exports that call this function per species.
    stages = ics_parse_age_range(text)
    if stages:
        if len(stages) == 1:
            # Single stage: midpoint is the only sensible answer.
            stage = stages[0]
            info = ICS_2024.get(stage)
            if info:
                base = info.get("base_ma", 0) or 0
                top = info.get("top_ma", 0) or 0
                return stage, (base + top) / 2
        else:
            # Stage range: pick the boundary that matches ``prefer``.
            bases = [
                (s, ICS_2024[s].get("base_ma", 0) or 0)
                for s in stages if s in ICS_2024
            ]
            tops = [
                (s, ICS_2024[s].get("top_ma", 0) or 0)
                for s in stages if s in ICS_2024
            ]
            if bases and tops:
                # In geological convention, base_ma > top_ma and a LARGER
                # Ma is OLDER. So "older" = max(bases), "younger" = min(tops).
                if prefer == "older":
                    name, ma = max(bases, key=lambda x: x[1])
                else:
                    name, ma = min(tops, key=lambda x: x[1])
                return name, ma

    # 5. Period-level fallback (English then Chinese).
    for label, period in _EN_PERIOD_ALIASES.items():
        if re.search(r"\b" + re.escape(label) + r"\b", norm):
            base, top = _PERIOD_BOUNDS.get(period, (None, None))
            if base is not None:
                return period, base if prefer == "older" else top
    for alias, period in _CN_PERIOD_ALIASES.items():
        if alias in text:
            base, top = _PERIOD_BOUNDS.get(period, (None, None))
            if base is not None:
                return period, base if prefer == "older" else top
    return None, None


def ics_age_range_bounds(text: Any) -> tuple[Optional[float], Optional[float]]:
    """Resolve an age_range string to ``(older_ma, younger_ma)``.

    Order-independent: ``max`` of all stage bases / ``min`` of all stage
    tops, so "Wuchiapingian - Changhsingian" and "Changhsingian -
    Wuchiapingian" give the same (259.51, 251.9). Falls back to explicit Ma
    literals, then series/epoch labels, then period labels.

    Returns ``(None, None)`` when nothing resolves.
    """
    if not text or not ICS_2024:
        return None, None
    text = str(text).strip()

    values = _explicit_ma_values(text)
    if values:
        return max(values), min(values)

    stages = ics_parse_age_range(text)
    if stages:
        bases = [ICS_2024[s].get("base_ma", 0) or 0 for s in stages if s in ICS_2024]
        tops = [ICS_2024[s].get("top_ma", 0) or 0 for s in stages if s in ICS_2024]
        if bases and tops:
            return max(bases), min(tops)

    norm = text.lower()
    for label, (name, sl) in _SERIES_STAGE_LISTS.items():
        if re.search(r"\b" + re.escape(label) + r"\b", norm):
            return _series_bounds_for(label, sl)
    for alias, label in _CN_SERIES_ALIASES.items():
        if alias in text:
            return _series_bounds_for(label, _SERIES_STAGE_LISTS[label][1])
    for label, period in _EN_PERIOD_ALIASES.items():
        if re.search(r"\b" + re.escape(label) + r"\b", norm):
            return _PERIOD_BOUNDS.get(period)
    for alias, period in _CN_PERIOD_ALIASES.items():
        if alias in text:
            return _PERIOD_BOUNDS.get(period)
    return None, None


def ics_era(stage: str) -> Optional[str]:
    """Return the era for a given stage name.

    
    Returns one of: "Paleozoic", "Mesozoic", "Cenozoic", "Precambrian"
    or None if the stage is unknown.
    """
    info = ICS_2024.get(stage)
    if info is None:
        return None
    return info.get("era")


def ics_period(stage: str) -> Optional[str]:
    """Return the period for a given stage name.

    
    Returns the period name (e.g., "Permian", "Jurassic")
    or None if the stage is unknown.
    """
    info = ICS_2024.get(stage)
    if info is None:
        return None
    return info.get("period")
