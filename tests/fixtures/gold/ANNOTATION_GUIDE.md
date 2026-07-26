# Annotation Guide

This guide explains how to add new expert-annotated cases to the gold-standard test set.

## Annotation Workflow

1. **Select a chart** — choose a high-quality, well-documented chart from the literature
2. **Extract current GT** — run the current extraction pipeline to get a draft
3. **Expert review** — a paleontologist verifies/orrects the draft
4. **Double-blind validation** — a second annotator independently reviews
5. **Submit** — PR to the repository with the new case

## Directory Structure

Each case lives in a subdirectory:
```
tests/fixtures/gold/<type>/<case_id>/
├── image.png              # The chart image
├── ground_truth.json      # Expert-verified extraction result
└── source.json           # Citation, license, annotator info
```

## ground_truth.json Schema

```json
{
  "metadata": {
    "chart_type": "range_chart | columnar_section | abundance | phylogenetic_tree",
    "extraction_version": "1.x.y",
    "annotated_at": "2026-07-27",
    "annotator": "Dr. Jane Smith",
    "validated_by": "Dr. John Doe"
  },
  "sections": [...],
  "species_ranges": [...],
  "biozones": [...],
  "confidence": 0.95
}
```

### range_chart fields

```json
{
  "sections": [{
    "name": "Section A",
    "age_range": "Late Jurassic",
    "formations": ["Oxford Clay"],
    "coordinates": "N51.5° W1.0°"
  }],
  "species_ranges": [{
    "species": "Ammonites koslovensis",
    "section": "Section A",
    "range_top": "15",
    "range_base": "3",
    "biozone": "Aspidoceras Zone",
    "author": "Smith",
    "year": "1901",
    "author_year": "Smith, 1901",
    "range_top_bed": "Bed 15",
    "range_base_bed": "Bed 3",
    "endpoint_kind": "observed",
    "occurrence_mode": "in_situ"
  }],
  "biozones": [{
    "name": "Aspidoceras Zone",
    "section": "Section A",
    "age": "Late Jurassic",
    "zone_type": "assemblage_zone"
  }]
}
```

### columnar_section fields

```json
{
  "sections": [{
    "id": "LOCALITY-1",
    "group": "Main Section",
    "lithology_blocks": [{
      "pattern": "ss",
      "range_top_idx": 10,
      "range_base_idx": 1
    }],
    "age_units": [{
      "label": "Kimmeridgian",
      "range_top_idx": 10,
      "range_base_idx": 1
    }],
    "coordinates_text": "N51°30' W1°00'",
    "thickness_m": "12.5"
  }],
  "fossil_legend": [{
    "marker": "A",
    "meaning": "Ammonites"
  }],
  "lithology_legend": [{
    "pattern": "ss",
    "meaning": "Sandstone"
  }]
}
```

### abundance fields

```json
{
  "sites": [{
    "name": "Boreal Sea Core 1",
    "location": "North Sea",
    "age_range": "Early Pliocene",
    "depth_unit": "m"
  }],
  "abundances": [{
    "taxon": "G. bulloides",
    "site": "Boreal Sea Core 1",
    "level": "1",
    "depth": "10",
    "abundance": "15.3",
    "abundance_unit": "%"
  }],
  "zones": [{
    "name": "M plankton biozone",
    "age": "Early Pliocene",
    "level_range": "1-8"
  }]
}
```

### phylogenetic_tree fields

```json
{
  "metadata": {
    "title": "Ammonoid phylogeny",
    "tree_type": "cladogram",
    "scale": "Ma",
    "rooted": true
  },
  "root_ids": ["root"],
  "nodes": [
    {"id": "root", "parent": null, "name": "", "is_leaf": false, "branch_length": null, "support": null},
    {"id": "n1", "parent": "root", "name": "", "is_leaf": false, "branch_length": 0.3, "support": 95},
    {"id": "sp1", "parent": "n1", "name": "Koslovites", "is_leaf": true, "branch_length": 0.2, "support": null}
  ],
  "confidence": 0.92
}
```

## ICZN Qualifier Standardization

| Symbol | Meaning | Write As |
|--------|---------|---------|
| cf. | Conferred with (similar to) | `Genus cf. species` |
| aff. | Affinis (related to, distinct) | `Genus aff. species` |
| ? | Doubtful identification | `Genus? species` |
| sensu lato | In the broad sense | `Genus sp. sensu lato` |
| ex gr. | From a group | `Genus ex gr. species` |

## License Requirements

Only images/data that are:
- **CC-BY** (with attribution)
- **CC0** (public domain)
- Public domain in your jurisdiction

Do NOT use copyrighted material without explicit permission.

## DOI / Citation Format

```json
{
  "doi": "10.1000/xyz123",
  "citation": "Smith, J. 1901. Jurassic ammonites from the Oxford Clay. J. Paleontol. 45(3): 1-50.",
  "license": "CC-BY 4.0",
  "source_url": "https://example.org/chart"
}
```

## Double-Blind Consistency

Two annotators independently label the same chart. Compute Krippendorff's alpha:

```
alpha >= 0.80  → Acceptable
alpha >= 0.90  → Excellent (required for publication-grade GT)
alpha < 0.80   → Reannotate and reconcile differences
```

## Submitting New Cases

1. Fork the repository
2. Place new case in `gold_proposed/<type>/<your_case_id>/`
3. Include `source.json` with full citation and license
4. Open a PR with:
   - Description of the chart source
   - Why it was chosen (challenging case, coverage gap, etc.)
   - Any known extraction difficulties

## Review Criteria

Cases will be reviewed for:
- Image quality (min 300 DPI, readable labels)
- Completeness of ground truth
- Correct application of ICZN qualifiers
- Stratigraphic consistency (no impossible age sequences)
- License compliance
