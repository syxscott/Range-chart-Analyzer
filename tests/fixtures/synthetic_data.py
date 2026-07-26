"""Generate synthetic paleontology range charts with known ground truth.

Useful for unit-testing the extraction pipeline without expert-annotated
real data. Each generator returns (image_bytes, ground_truth_dict).

matplotlib is optional — if not available, returns a skip indicator.
"""
from __future__ import annotations

import io
import json
import random
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

FAKE_SPECIES = [
    "Ammonites koslovensis",
    "Koslovites flexionus",
    "Orthoceros regulare",
    "Terebratula magna",
    "Inoceramus elongatus",
    "Belemnites paxillosus",
    "Gryphaea arcuata",
    "Pentacrinites fossilis",
    "Posidonia buchii",
    "Dactylioceras commune",
]

FAKE_SECTIONS = ["Section X", "Section Y"]

ICZN_QUALIFIERS = ["cf.", "aff.", "?", "ex gr."]

BIOZONE_NAMES = [
    "Aspidoceras Zone",
    "Ring Zone",
    "Oxynotes Zone",
    "Koslovites Biozone",
]


def _skip_with_msg(msg: str) -> tuple[bytes, dict]:
    """Return an empty PNG placeholder and a skip indicator dict."""
    # Minimal 1x1 transparent PNG
    png_1x1 = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
        b"\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
        b"\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01"
        b"\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    return png_1x1, {"_skip": True, "reason": msg}


# ---------------------------------------------------------------------------
# Range Chart Generator
# ---------------------------------------------------------------------------


def gen_range_chart(seed: int = 0) -> tuple[bytes, dict]:
    """Generate a synthetic range chart with 5 species across 2 sections.

    Injects 1-2 known error patterns: species boundary offset and a
    biozone name mismatch. Returns (PNG bytes, ground truth dict).
    """
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.patches as mpatches
    except ImportError:
        return _skip_with_msg("matplotlib not available")

    random.seed(seed)
    rng = random.Random(seed)

    # Build ground-truth species data
    species_list = rng.sample(FAKE_SPECIES, 5)
    ranges: list[dict[str, Any]] = []
    bed_top_base: list[tuple[int, int]] = []

    for i, sp in enumerate(species_list):
        base = rng.randint(1, 10)
        top = base + rng.randint(2, 8)
        bed_top_base.append((top, base))

        # Inject one ICZN qualifier on the 3rd species
        name = sp
        if i == 2:
            qualifier = rng.choice(ICZN_QUALIFIERS)
            name = f"{sp.split()[0]} {qualifier} {sp.split()[1]}"

        section = rng.choice(FAKE_SECTIONS)
        biozone = rng.choice(BIOZONE_NAMES)

        # For seed 0 and 1: inject a boundary offset error on species index 1
        offset_error = 0
        if seed in (0, 1) and i == 1:
            offset_error = 1  # top will be off by +1 vs GT

        ranges.append(
            {
                "species": name,
                "section": section,
                "range_top": str(top + offset_error),
                "range_base": str(base),
                "biozone": biozone,
                "author": rng.choice(["Smith", "Jones", "Brown"]),
                "year": str(rng.randint(1880, 1990)),
                "author_year": f"{rng.choice(['Smith', 'Jones', 'Brown'])}, {rng.randint(1880, 1990)}",
                "range_top_bed": f"Bed {top + offset_error}",
                "range_base_bed": f"Bed {base}",
                "endpoint_kind": "observed",
                "occurrence_mode": "in_situ",
            }
        )

    # 1-2 biozones
    selected_biozones = rng.sample(BIOZONE_NAMES, 2)
    biozones = []
    for bz_name in selected_biozones:
        sec = rng.choice(FAKE_SECTIONS)
        # For seed 2: inject a biozone name error
        if seed == 2 and bz_name == "Aspidoceras Zone":
            bz_name = "Aspidoceras Zon"  # typo
        biozones.append(
            {
                "name": bz_name,
                "section": sec,
                "age": rng.choice(["Late Jurassic", "Early Cretaceous", "Middle Jurassic"]),
                "zone_type": "assemblage_zone",
            }
        )

    # Sections
    sections = []
    for sec_name in FAKE_SECTIONS:
        sections.append(
            {
                "name": sec_name,
                "age_range": rng.choice(["Late Jurassic", "Early Cretaceous", "Middle Jurassic"]),
                "formations": [rng.choice(["Oxford Clay", "Kimmeridge Clay", "Corallian"])],
                "coordinates": f"N{rng.randint(50,56)}.{rng.randint(0,59)}° W{rng.randint(0,2)}.{rng.randint(0,59)}°",
            }
        )

    # --- Render ---
    n_beds = 30
    fig, ax = plt.subplots(figsize=(12, 6))
    ax.set_xlim(0, n_beds + 1)
    ax.set_ylim(0, len(species_list) + 1)
    ax.set_xlabel("Bed Number (1 = oldest)", fontsize=9)
    ax.set_ylabel("Species", fontsize=9)
    ax.set_yticks(range(1, len(species_list) + 1))
    ax.set_yticklabels([r["species"] for r in ranges], fontsize=8)
    ax.set_title(f"Synthetic Range Chart (seed={seed})", fontsize=11)
    ax.invert_yaxis()
    ax.grid(axis="x", linestyle="--", alpha=0.3)

    # Color for sections
    section_colors = {"Section X": "#a8d5e2", "Section Y": "#ffd6a5"}

    for i, (sp_name, rtb) in enumerate(zip(species_list, bed_top_base)):
        top, base = rtb
        sec = ranges[i]["section"]
        color = section_colors.get(sec, "#cccccc")
        y = i + 1
        # Draw range bar
        ax.barh(
            y,
            top - base + 0.6,
            left=base - 0.3,
            height=0.6,
            color=color,
            edgecolor="black",
            linewidth=0.8,
        )
        # FAD dot
        ax.plot(base, y, "ko", markersize=4)
        # LAD dot
        ax.plot(top, y, "k^", markersize=4)

    # Legend
    legend_patches = [
        mpatches.Patch(color=section_colors["Section X"], label="Section X"),
        mpatches.Patch(color=section_colors["Section Y"], label="Section Y"),
    ]
    ax.legend(handles=legend_patches, loc="upper right", fontsize=8)

    plt.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=100)
    plt.close(fig)
    img_bytes = buf.getvalue()

    ground_truth = {
        "metadata": {
            "chart_type": "range_chart",
            "extraction_version": "1.0.0",
            "synthetic": True,
            "seed": seed,
        },
        "sections": sections,
        "species_ranges": ranges,
        "biozones": biozones,
        "confidence": 0.95,
    }
    return img_bytes, ground_truth


# ---------------------------------------------------------------------------
# Columnar Section Generator
# ---------------------------------------------------------------------------


def gen_columnar_section(seed: int = 1) -> tuple[bytes, dict]:
    """Generate a synthetic columnar section with 3 lithology blocks."""
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.patches as mpatches
    except ImportError:
        return _skip_with_msg("matplotlib not available")

    rng = random.Random(seed)

    lithology_patterns = [
        ("ss", "Sandstone", "#f4d03f"),
        ("sh", "Shale", "#5d6d7e"),
        ("ls", "Limestone", "#aed6f1"),
        ("mud", "Mudstone", "#935116"),
    ]

    # 3 lithology blocks
    blocks = []
    block_y = 1
    block_height = 3
    chosen = rng.sample(lithology_patterns, 3)
    for pattern, name, color in chosen:
        thickness = rng.randint(2, 6)
        blocks.append(
            {
                "pattern": pattern,
                "range_top_idx": block_y + block_height - 1,
                "range_base_idx": block_y,
                "thickness_m": str(thickness),
                "_color": color,
                "_name": name,
            }
        )
        block_y += block_height

    # Age units (slightly offset from lithology)
    age_units = [
        {"label": "Kimmeridgian", "range_top_idx": 7, "range_base_idx": 4},
        {"label": "Oxfordian", "range_top_idx": 4, "range_base_idx": 1},
    ]

    # Fossil markers
    fossil_legend = [
        {"marker": "A", "meaning": "Ammonites"},
        {"marker": "B", "meaning": "Belemnites"},
        {"marker": "T", "meaning": "Terebratulids"},
    ]

    # Samples
    samples = [
        {"bed_idx": 2, "fossil_marker": "A", "ref": "Fig. 3a"},
        {"bed_idx": 5, "fossil_marker": "B", "ref": "Fig. 3b"},
        {"bed_idx": 8, "fossil_marker": "T", "ref": "Fig. 3c"},
    ]

    # --- Render ---
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(10, 6), gridspec_kw={"width_ratios": [2, 1]})

    total_rows = 9
    for i, blk in enumerate(blocks):
        top = blk["range_top_idx"]
        base = blk["range_base_idx"]
        color = blk["_color"]
        ax1.add_patch(
            mpatches.Rectangle((0.1, base - 0.5), 0.8, top - base + 1, facecolor=color, edgecolor="black")
        )
        ax1.text(0.5, (top + base) / 2, blk["_name"], ha="center", va="center", fontsize=7)

    ax1.set_xlim(0, 1)
    ax1.set_ylim(0, total_rows)
    ax1.set_ylabel("Bed Index (1 = oldest)", fontsize=9)
    ax1.set_xticks([])
    ax1.set_title(f"Synthetic Columnar Section (seed={seed})", fontsize=10)
    ax1.invert_yaxis()

    # Legend on right
    for j, fl in enumerate(fossil_legend):
        ax2.text(0.1, j + 1, f"{fl['marker']} = {fl['meaning']}", fontsize=8)

    ax2.set_xlim(0, 1)
    ax2.set_ylim(0, len(fossil_legend) + 1)
    ax2.axis("off")
    ax2.set_title("Fossil Legend", fontsize=9)

    plt.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=100)
    plt.close(fig)
    img_bytes = buf.getvalue()

    ground_truth = {
        "metadata": {
            "chart_type": "columnar_section",
            "extraction_version": "1.0.0",
            "synthetic": True,
            "seed": seed,
        },
        "sections": [
            {
                "id": f"Locality-{seed}",
                "group": "Main Section",
                "lithology_blocks": [
                    {"pattern": b["pattern"], "range_top_idx": b["range_top_idx"], "range_base_idx": b["range_base_idx"]}
                    for b in blocks
                ],
                "age_units": age_units,
                "samples": samples,
                "coordinates_text": f"N{rng.randint(50,56)}.{rng.randint(0,59)}° W{rng.randint(0,2)}.{rng.randint(0,59)}°",
                "thickness_m": str(sum(int(b["thickness_m"]) for b in blocks)),
            }
        ],
        "fossil_legend": [{"marker": fl["marker"], "meaning": fl["meaning"]} for fl in fossil_legend],
        "lithology_legend": [{"pattern": b["pattern"], "meaning": b["_name"]} for b in blocks],
        "cross_beds": [],
        "confidence": 0.9,
    }
    return img_bytes, ground_truth


# ---------------------------------------------------------------------------
# Abundance Diagram Generator
# ---------------------------------------------------------------------------


def gen_abundance_diagram(seed: int = 2) -> tuple[bytes, dict]:
    """Generate a synthetic abundance diagram with 6 taxa across 8 samples."""
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return _skip_with_msg("matplotlib not available")

    rng = random.Random(seed)

    taxa = rng.sample(
        ["G. bulloides", "N. pachyderma", "P. obliquiloculata", "G. ruber", "O. universa", "S. universa"],
        6,
    )
    n_samples = 8

    # Generate percentages that sum to ~100 per sample
    abundances: list[dict[str, Any]] = []
    for level in range(1, n_samples + 1):
        depth = level * 10
        raw = [rng.randint(5, 40) for _ in range(6)]
        total = sum(raw)
        for taxon, pct in zip(taxa, raw):
            abundances.append(
                {
                    "taxon": taxon,
                    "site": "Synthetic Core",
                    "level": str(level),
                    "depth": str(depth),
                    "abundance": str(round(pct / total * 100, 1)),
                    "abundance_unit": "%",
                }
            )

    zones = [
        {"name": "M Biozone", "age": "Early Pliocene", "level_range": "1-4"},
        {"name": "N Biozone", "age": "Late Pliocene", "level_range": "5-8"},
    ]

    # --- Render as stacked bar ---
    fig, ax = plt.subplots(figsize=(10, 6))

    # Aggregate per level
    level_data: dict[int, dict[str, float]] = {}
    for ab in abundances:
        lvl = int(ab["level"])
        if lvl not in level_data:
            level_data[lvl] = {}
        level_data[lvl][ab["taxon"]] = float(ab["abundance"])

    x = sorted(level_data.keys())
    bottom_vals = [0.0] * len(x)
    colors = ["#e74c3c", "#3498db", "#2ecc71", "#f39c12", "#9b59b6", "#1abc9c"]

    for j, taxon in enumerate(taxa):
        vals = [level_data[l].get(taxon, 0.0) for l in x]
        ax.bar(x, vals, bottom=bottom_vals, label=taxon, color=colors[j % len(colors)], width=0.7)
        bottom_vals = [b + v for b, v in zip(bottom_vals, vals)]

    ax.set_xlabel("Sample Level", fontsize=9)
    ax.set_ylabel("Abundance (%)", fontsize=9)
    ax.set_title(f"Synthetic Abundance Diagram (seed={seed})", fontsize=11)
    ax.legend(loc="upper right", fontsize=7, ncol=2)
    plt.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=100)
    plt.close(fig)
    img_bytes = buf.getvalue()

    ground_truth = {
        "metadata": {
            "chart_type": "abundance",
            "extraction_version": "1.0.0",
            "synthetic": True,
            "seed": seed,
        },
        "sites": [
            {
                "name": "Synthetic Core",
                "location": "Laboratory",
                "age_range": "Pliocene",
                "depth_unit": "m",
            }
        ],
        "abundances": abundances,
        "zones": zones,
        "confidence": 0.92,
    }
    return img_bytes, ground_truth


# ---------------------------------------------------------------------------
# Phylogenetic Tree Generator
# ---------------------------------------------------------------------------


def gen_phylogenetic_tree(seed: int = 3) -> tuple[bytes, dict]:
    """Generate a synthetic phylogenetic tree with 4 leaves + 3 internal nodes."""
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return _skip_with_msg("matplotlib not available")

    rng = random.Random(seed)

    # Tree structure (fixed for all seeds):
    #          root
    #         /    \
    #       n1      n2
    #      /  \    /  \
    #    sp1  sp2 sp3 sp4
    nodes = [
        {"id": "root", "parent": None, "name": "", "is_leaf": False, "branch_length": None, "support": None},
        {"id": "n1", "parent": "root", "name": "", "is_leaf": False, "branch_length": round(rng.uniform(0.2, 0.4), 2), "support": rng.randint(75, 98)},
        {"id": "n2", "parent": "root", "name": "", "is_leaf": False, "branch_length": round(rng.uniform(0.2, 0.4), 2), "support": rng.randint(75, 98)},
        {"id": "n3", "parent": "n1", "name": "", "is_leaf": False, "branch_length": round(rng.uniform(0.1, 0.3), 2), "support": rng.randint(70, 95)},
        {
            "id": "sp1",
            "parent": "n3",
            "name": rng.choice(["Genus alpha", "Genus beta", "Genus gamma"]),
            "is_leaf": True,
            "branch_length": round(rng.uniform(0.1, 0.3), 2),
            "support": None,
        },
        {
            "id": "sp2",
            "parent": "n3",
            "name": rng.choice(["Genus delta", "Genus epsilon"]),
            "is_leaf": True,
            "branch_length": round(rng.uniform(0.1, 0.3), 2),
            "support": None,
        },
        {
            "id": "sp3",
            "parent": "n2",
            "name": rng.choice(["Genus zeta", "Genus eta"]),
            "is_leaf": True,
            "branch_length": round(rng.uniform(0.1, 0.3), 2),
            "support": None,
        },
        {
            "id": "sp4",
            "parent": "n2",
            "name": rng.choice(["Genus theta", "Genus iota"]),
            "is_leaf": True,
            "branch_length": round(rng.uniform(0.1, 0.3), 2),
            "support": None,
        },
    ]

    # --- Render using matplotlib dendrogram-style ---
    try:
        from ete3 import Tree
        # Build newick string
        def build_newick(node_id: str, id_to_children: dict[str, list[str]], nodes_dict: dict) -> str:
            children = id_to_children.get(node_id, [])
            if not children:
                n = nodes_dict.get(node_id, {})
                name = n.get("name", "").replace(" ", "_")
                bl = n.get("branch_length")
                bl_str = f":{bl}" if bl is not None else ""
                return f"{name}{bl_str}"
            child_parts = [build_newick(cid, id_to_children, nodes_dict) for cid in children]
            n = nodes_dict.get(node_id, {})
            support = n.get("support")
            support_str = f"{support}" if support is not None else ""
            bl = n.get("branch_length")
            bl_str = f":{bl}" if bl is not None else ""
            child_newick = ",".join(child_parts)
            return f"({child_newick}){support_str}{bl_str}"

        nodes_dict = {n["id"]: n for n in nodes}
        id_to_children: dict[str, list[str]] = {n["id"]: [] for n in nodes}
        for n in nodes:
            pid = n.get("parent")
            if pid is not None:
                id_to_children.setdefault(str(pid), []).append(n["id"])

        newick_str = build_newick("root", id_to_children, nodes_dict) + ";"
        t = Tree(newick_str, format=1)

        fig, ax = plt.subplots(figsize=(8, 6))
        t.render("%%anon", ax=ax)
        ax.set_title(f"Synthetic Phylogenetic Tree (seed={seed})", fontsize=11)
        plt.tight_layout()
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=100)
        plt.close(fig)
        img_bytes = buf.getvalue()
    except ImportError:
        # Fallback: draw a simple ASCII tree representation
        fig, ax = plt.subplots(figsize=(8, 6))
        ax.text(0.5, 0.5, "Phylogenetic Tree\n(ete3 not available for rendering)", ha="center", va="center")
        ax.axis("off")
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=100)
        plt.close(fig)
        img_bytes = buf.getvalue()

    ground_truth = {
        "metadata": {
            "title": "Synthetic Phylogenetic Tree",
            "extraction_timestamp": "2026-07-27T00:00:00Z",
            "tree_type": "cladogram",
            "scale": "substitutions/site",
            "rooted": True,
            "source": "synthetic",
        },
        "root_ids": ["root"],
        "nodes": [
            {k: v for k, v in n.items() if k != "_color"}
            for n in nodes
        ],
        "confidence": 0.88,
    }
    return img_bytes, ground_truth
