// prompt.js - MiniMax M3 system prompt for range-chart extraction.
// Ported verbatim from RLPE range_chart_extractor._RANGE_CHART_SYSTEM_PROMPT,
// extended to explicitly support charts in Chinese / English / Japanese /
// Russian and to always emit English scientific names.
'use strict';

// PROMPT_VERSION: dict of per-mode version strings. Keep in sync with
// rca_core/prompt.py PROMPT_VERSION. The cache layer includes this in its
// cache key so old cached results from a previous prompt version are not
// served after a prompt upgrade.
//
// P3 fix (2026-08-06): canonical keys are the extraction-mode strings that
// promptVersionForMode() is called with (e.g. 'abundance_diagram'). The old
// 'abundance' key never matched, so the version fell back to 'v3' and an
// abundance prompt upgrade never invalidated the cache. 'abundance' is kept
// as a legacy alias.
//
// BORROW-2026-09-20 (A+B): the four modes that now carry the coverage
// contract (response_kind / reason_codes) and the 0-999 axis positions were
// bumped, because a cached pre-contract answer can never be re-used as if the
// model had accounted for its negative answers: range_chart v4->v5,
// abundance_diagram v3->v4 (legacy alias 'abundance' with it),
// chemical_stratigraphy v1->v2, scatter_plot v1->v2. The modes without a
// parser-side contract consumer keep their versions. Mirrors
// rca_core/prompt.py PROMPT_VERSION.
const PROMPT_VERSION = {
  range_chart: 'v5',
  columnar_section: 'v3',
  abundance_diagram: 'v4',
  abundance: 'v4',        // legacy alias — canonical key is above
  phylogenetic_tree: 'v1',
  // NEW chart types
  chemical_stratigraphy: 'v2',
  paleomap: 'v2',
  scatter_plot: 'v2',
  // UI-REVIEW-2026-09-05: radiolarian biozonation / correlation charts.
  zonation_chart: 'v1',
  // UI-REVIEW-2026-09-07: vision chart-type classification for auto mode.
  chart_classify: 'v1',
};

function promptVersionForMode(mode) {
  return PROMPT_VERSION[mode] || 'v3';
}

// Teaches the model to degrade gracefully (low confidence + note) instead of
// hallucinating when a value is ambiguous/unreadable.
function _degradationClause() {
  return '- DEGRADE GRACEFULLY. If a species name, range boundary, bed number, or other value is ambiguous or partially unreadable, still emit your best guess but lower the per-row `confidence` field (e.g. 0.3–0.5) AND set the per-row `note` field to a short string such as "unclear" or "partially obscured". NEVER invent a plausible-looking value with high confidence — a low-confidence guess is far more useful than a confident fabrication. NEVER embed the note inside the species name or other structured fields; notes live in the dedicated `note` field only. If a value is completely unreadable, leave the field empty string, lower the row `confidence`, and put "unclear" in the row `note`.';
}

// ---------------------------------------------------------------------------
// BORROW-2026-09-20 (A+B) — the three-state answer contract and the
// PlotLift-style 0-999 normalised axis positions, mirrored from
// rca_core/prompt.py. Both clauses are GENERATED from the constants below —
// never transcribed into a prompt — exactly like the Python side, so a JS
// prompt can not advertise a reason code the parse side would drop.
//
// TODO(convergence): this slug list duplicates rca_core/reason_codes.py's
// REASON_CODES catalogue (and the three answer words). js/reason-codes.js —
// which will own it there, mirroring the Python module — is being created by
// another workstream; once it lands, prompt.js must import the vocabulary
// from it instead of carrying its own copy. Until then prompt.js deliberately
// does NOT require js/reason-codes.js.
// ---------------------------------------------------------------------------

/// The closed reason-code vocabulary, in catalogue order (mirrors the
/// REASON_CODES tuple of rca_core/reason_codes.py).
const REASON_CODES_SLUGS = [
  'not_drawn', 'uncertain', 'obscured', 'inferred', 'legend_only',
  'crosses_top', 'crosses_base', 'truncated', 'no_label', 'abbreviated',
  'low_confidence', 'out_of_scope',
];

/// The three-state answer words (mirror RESPONSE_EXTRACTED / RESPONSE_NOT_DRAWN
/// / RESPONSE_UNCERTAIN of rca_core/reason_codes.py).
const RESPONSE_EXTRACTED = 'extracted';
const RESPONSE_NOT_DRAWN = 'not_drawn';
const RESPONSE_UNCERTAIN = 'uncertain';

/// The closed vocabulary rendered in catalogue order.
const REASON_CODE_SLUG_LIST = REASON_CODES_SLUGS.join(', ');

/// The three-state answer rendered in the order the prompts introduce it.
const RESPONSE_KIND_LIST = [
  RESPONSE_EXTRACTED, RESPONSE_NOT_DRAWN, RESPONSE_UNCERTAIN].join(' | ');

/// The three-state answer contract (BORROW-2026-09-20 A). Mirrors
/// rca_core/prompt.py:_coverage_contract_clause byte-for-byte.
function _coverageContractClause(rowTable, unit, negativeExamples) {
  return '- ANSWER EVERY REQUESTED CELL, INCLUDING THE NEGATIVE ANSWERS. Every ' +
    '`' + rowTable + '` entry must state `response_kind`, exactly one of ' +
    '"' + RESPONSE_EXTRACTED + '" | "' + RESPONSE_NOT_DRAWN + '" | ' +
    '"' + RESPONSE_UNCERTAIN + '": ' +
    '"' + RESPONSE_EXTRACTED + '" = something is drawn for this ' + unit + ' and you ' +
    'read it; ' +
    '"' + RESPONSE_NOT_DRAWN + '" = the column and the level both exist in the ' +
    'figure but the author drew NOTHING at their intersection — ' +
    negativeExamples + '. That is a POSITIVE observation (\'absent here\'), so ' +
    'still emit the row with its value fields left empty and ' +
    '`response_kind: "' + RESPONSE_NOT_DRAWN + '"`; NEVER delete the row instead, ' +
    'and NEVER write "-" / "none" / "absent" into a value field to say ' +
    'the same thing; ' +
    '"' + RESPONSE_UNCERTAIN + '" = something is drawn but you cannot settle its ' +
    'value or its level. Omitting a cell you were asked about is the one ' +
    'answer nobody can audit: a run that honestly reports the dashes scores ' +
    'BETTER on coverage than a run that quietly skips those rows, so being ' +
    'complete beats being silent.';
}

/// The optional machine-readable `reason_codes` array (A). Mirrors
/// rca_core/prompt.py:_reason_code_clause byte-for-byte. Deliberately a
/// CLOSED list generated from REASON_CODES_SLUGS: the parser drops unknown
/// slugs instead of guessing.
function _reasonCodeClause() {
  return '- REASON CODES (optional, alongside `response_kind`). `reason_codes` is ' +
    'a JSON array of lowercase slugs copied VERBATIM from this closed list: ' +
    REASON_CODE_SLUG_LIST +
    '. Spelling counts — an unrecognised slug is ' +
    'dropped, not guessed at. Say WHY: `crosses_top` / `crosses_base` when ' +
    'the mark runs off the frame so the true limit is unknown, `truncated` ' +
    'when a panel edge or a page break cut it, `obscured` / `no_label` / ' +
    '`abbreviated` when the read was blocked, `legend_only` when the name ' +
    'appears only in the legend or caption and is not plotted, `inferred` ' +
    'when you interpolated from adjacent evidence instead of reading, ' +
    '`out_of_scope` when the cell belongs to another figure. Codes explain ' +
    'the answer, they do not replace it: a `not_drawn` row that still ' +
    'carries a readable value is read back as `extracted` and flagged, so ' +
    'keep the two consistent.';
}

/// The 0-999 normalised position instruction (B). Mirrors
/// rca_core/prompt.py:_axis_position_clause byte-for-byte. `fields` are
/// [name, what-it-locates] pairs from POS_FIELDS below.
function _axisPositionClause(rowTable, fields, axisLabel, zeroEnd,
                             nineNineNineEnd, calibrationShape, calibrationWhen) {
  var fieldText = fields.map(function (f) {
    return '`' + f[0] + '` = ' + f[1];
  }).join('; ');
  return '- NORMALISED AXIS POSITIONS (`pos_0_999`). For each `' + rowTable + '` row, ' +
    'also record WHERE the boundary actually sits on ' + axisLabel + ' as an ' +
    'INTEGER measured off the picture, on a fixed scale where 0 = ' + zeroEnd + ' and 999 = ' + nineNineNineEnd + '. Use exactly these field ' +
    'names and invent no others: ' + fieldText + '. Whole integers 0-999 only — ' +
    'never a decimal, never a percentage, never 1000, never a position ' +
    'written into a value field — and omit the field entirely when the ' +
    'position cannot be judged. This is an INDEPENDENT second read of the ' +
    'same boundary: the parser cross-checks it against the value you ' +
    'transcribed and discards (with a confidence penalty) any position that ' +
    'misses it by more than 15% of the axis span, so read the pixels and do ' +
    'not back-derive the number from the label.\n' +
    '- AXIS CALIBRATION (figure level, optional). ' + calibrationWhen + ' Emit ' +
    'ONE root-level block, e.g. `"axis_calibration": ' + calibrationShape + '`, where `at_0` is the value sitting at 0 ' +
    '(' + zeroEnd + ') and `at_999` the value at 999 (' + nineNineNineEnd + ') — ' +
    'the same two ends the positions above are measured against. Omit the ' +
    'block when an end is not legible; an uncalibrated position is still ' +
    'kept as evidence, it is simply not converted.';
}

/// (A) only: the two coverage-contract bullets joined. Mirrors
/// rca_core/prompt.py:_contract_clauses.
function _contractClauses(rowTable, unit, negativeExamples) {
  return _coverageContractClause(rowTable, unit, negativeExamples) +
    '\n' + _reasonCodeClause();
}

/// (B) The `*pos_0_999` fields each mode is told to emit, keyed by mode.
/// Mirrors POS_FIELDS of rca_core/prompt.py; tests lock that these names are
/// exactly the ones the parse side digests (extractor.POS_SCALE_KEY /
/// _GEOMETRY_VERTICAL_FIELDS / _GEOMETRY_AXIS_BY_FIELD).
const POS_FIELDS = {
  // species_ranges rows: one position per transcribed boundary.
  range_chart: [
    ['range_top_pos_0_999',
      'the position of `range_top` (the young limit) on the axis'],
    ['range_base_pos_0_999',
      'the position of `range_base` (the old limit) on the same axis'],
  ],
  // abundance rows: the sample level is the row's single vertical position.
  abundance_diagram: [
    ['pos_0_999',
      'the position of this level / sample point on the depth axis'],
  ],
  // chemical rows: one position per boundary, whichever of depth / age the
  // figure draws vertically.
  chemical_stratigraphy: [
    ['pos_0_999',
      'the position of this data point / event on the vertical axis'],
    ['top_pos_0_999',
      'on an `intervals` row: the position of its `top_depth_m` / `top_age_ma`'],
    ['base_pos_0_999',
      'on an `intervals` row: the position of its `base_depth_m` / `base_age_ma`'],
  ],
  // scatter points / outliers: two independent continuous axes.
  scatter_plot: [
    ['x_pos_0_999', 'the position of the point along the X axis'],
    ['y_pos_0_999', 'the position of the point along the Y axis'],
  ],
};

const RANGE_CHART_SYSTEM_PROMPT = [
'You are an expert in radiolarian (and general micropaleontology) biostratigraphy reading a stratigraphic range chart (also called a species distribution chart).',
'',
'The chart shows measured sections on the vertical axis (top = young, bottom = old) and species on the horizontal axis. Each species has a vertical line/bar showing its range across one or more sections.',
'',
'The chart text may be in Chinese, English, Japanese, or Russian. Read whichever language is present. Regardless of the source language, ALWAYS return scientific taxon names as their standard Latin binomials (e.g. "Neoalbaillella optima"), and translate age/formation/biozone free-text fields into English while preserving any proper names.',
'',
'Extract every piece of geological information visible in the chart as strict JSON with these fields:',
'',
'{',
'  "sections": [',
'    {',
'      "name": "Pingdingshan" (string, the measured-section / locality name),',
'      "age_range": "Late Permian (Late Changhsingian) - Early Triassic" (string),',
'      "formations": ["Talung Formation", "Yinkeng Formation"] (array of strings),',
'      "formation_thickness_m": "Talung: ~9 m; Yinkeng: 2 m" (string, free text),',
'      "coordinates": "31N, 117E" or "Not visible in chart" (string)',
'    }',
'  ],',
'  "species_ranges": [',
'    {',
'      "species": "Neoalbaillella optima" (string, full Latin binomial; transcribe what is on the chart, never invent a plausible-looking taxon name),',
'      "author_year": "De Wever & Dumitrica, 2002" (string, the binomial authority + year as printed; empty if not visible),',
'      "section": "Pingdingshan" (string, must match a section.name),',
'      "range_top": "Bed 9 (Yinkeng Fm base)" (string, the YOUNG/upper limit; keep concise),',
'      "range_base": "Bed 7 (top Talung Fm)" (string, the OLD/lower limit; keep concise),',
'      "range_top_bed": "Bed 9" (string, the YOUNG/upper bed label exactly as printed; empty if no bed label),',
'      "range_base_bed": "Bed 7" (string, the OLD/lower bed label exactly as printed; empty if no bed label),',
'      "range_top_idx": 9 (int, the bed index of the young limit, 1-indexed from base; empty if the bed label is not numeric),',
'      "range_base_idx": 7 (int, the bed index of the old limit, 1-indexed from base; empty if not numeric),',
// BORROW-2026-09-20 (B): the same two boundaries as an independent 0-999
// pixel read, digested into the row's optional `geometry` sidecar.
'      "range_top_pos_0_999": 347 (int, 0-999 position of the young limit on the vertical axis: 0 = bottom/oldest edge of the frame, 999 = top/youngest edge; empty when not judgeable),',
'      "range_base_pos_0_999": 261 (int, the same scale for the old limit),',
'      "endpoint_kind": "observed" (string, one of "unknown", "observed", "projected", "truncated" — use "unknown" when the endpoint type cannot be determined),',
'      "occurrence_mode": "in_situ" (string, one of "unknown", "in_situ", "reworked", "transported", "cavity_fill", "bioturbated", "derived", "lag_deposit" — use "unknown" when the occurrence mode cannot be determined),',
'      "biozone": "N. optima Zone (latest Changhsingian)" (string, optional),',
'      "confidence": 0.0-1.0 (float, per-row certainty; lower when the row is ambiguous),',
'      "note": "" (string, short provenance / uncertainty remark such as "unclear" or "partially obscured"; NEVER embedded in `species` or other structured fields),',
// BORROW-2026-09-20 (A): the three-state answer is REQUIRED on every row;
// the codes are the optional machine-readable WHY.
'      "response_kind": "extracted" (string, REQUIRED: one of "extracted", "not_drawn", "uncertain" — see the ANSWER EVERY REQUESTED CELL rule below),',
'      "reason_codes": ["crosses_top"] (array of strings, optional; lowercase slugs from the closed list in the REASON CODES rule; empty when nothing applies)',
'    }',
'  ],',
'  "biozones": [',
'    {',
'      "name": "Neoalbaillella optima Zone" (string),',
'      "section": "Pingdingshan" (string, must match a section.name when the biozone is measured per-section; empty string if the zone spans the whole chart),',
'      "age": "Latest Changhsingian (Late Permian)" (string),',
'      "thickness_m": "~3 m" (string, the thickness of this biozone in THIS section)',
'    }',
'  ],',
'  "other_fossils": [',
'    "Ammonoid: Pleuronodoceras sp. (Pingdingshan, bed 9, Yinkeng Fm)" (free text entries)',
'  ],',
// BORROW-2026-09-20 (B): optional figure-level calibration that turns the
// rows' 0-999 positions into axis values (extractor.axis_domains_from).
'  "axis_calibration": {"vertical": {"at_0": 1, "at_999": 24, "unit": "bed"}} (optional object, ONLY when the vertical axis is numeric and both its printed ends are legible; omit otherwise — the positions stay uncalibrated evidence)',
'  "confidence": 0.0-1.0 reflecting your certainty in the extraction overall',
'}',
'',
'Rules:',
'- COLUMNS ARE SEPARATE. A range chart has DISTINCT columns: (a) taxon/species columns, where each species is one vertical range line marked with dots or a bar; and (b) a biozone / assemblage-zone column (often labelled with ammonoid or conodont zone names). ONLY the vertical species range lines go into `species_ranges`. Zone names — including ammonoid/conodont assemblage names and anything ending in \'Zone\' / \'Zonule\' / \'assemblage\' — go into `biozones`, and NEVER into `species_ranges`. Do not turn a zone label into a fake species.',
'- CHRONOSTRATIGRAPHY vs LITHOSTRATIGRAPHY. Distinguish chronostratigraphic units (System / Series / Stage) from lithostratigraphic units (Group / Formation / Member). Put ages and Stage names into `age_range`; put only Group/Formation/Member names into `formations`. A Stage is NOT a Formation.',
'- CHINESE STRATIGRAPHIC TERMS. Translate the Chinese suffixes precisely: 系 = System, 统 = Series, 阶 = Stage, 群 = Group, 组 = Formation, 段 = Member, 带 = Zone. For example \'吴家坪阶\' is the Wuchiapingian STAGE (goes in age_range), while \'大隆组\' is the Dalong FORMATION (goes in formations). Do NOT label a 阶 (Stage) as a Formation.',
'- JAPANESE STRATIGRAPHIC TERMS. Translate the Japanese suffixes precisely: 系 = System, 統 = Series, 階 = Stage, 群 = Group, 組 = Formation, 段 = Member, 帯 = Zone. For example \'長門階\' is the Nagato STAGE (goes in age_range), while \'日置亜層群\' is the Hioki SUBGROUP (goes in formations). Do NOT label a 階 (Stage) as a Formation.',
'- RUSSIAN STRATIGRAPHIC TERMS (approximate). Translate the Russian litho-/chronostratigraphic terms as: система (systema) = System, отдел (otdel) = Series, ярус (yarus) = Stage / Age, группа (gruppa) = Group, свита (svita) = Suite (a Formation-rank unit), толща (tolshcha) = Group-rank or thick unit, зона (zona) = Zone. For example \'казанский ярус\' is the Kazanian STAGE (goes in age_range), while \'свита Хосе\' is the Khose SUITE (goes in formations). Note: Russian stratigraphic usage differs from English (svita ≈ Suite ≈ Formation); preserve the Russian proper name but use the English rank.',
'- KOREAN STRATIGRAPHIC TERMS. Translate the Korean suffixes precisely: 계 = System (系), 통 = Series (統), 절 = Stage (階), 군 = Group (群), 층 = Formation (組), 단 = Member (段), 대 = Zone (帯). For example \'압록계\' is the Cambrian SYSTEM. Korean stratigraphic terms often mix Chinese characters with Hangul; translate based on the Chinese-character component.',
'- GERMAN STRATIGRAPHIC TERMS.Translate German terms precisely: System = System, Serie = Series, Stufe = Stage, Gruppe = Group, Formation = Formation, Member = Member, Zone = Zone. Note that \'Stufe\' is the German equivalent of Stage. German geological texts often use Latin stage names (e.g., \'Maastrichtium\') which should be translated to their standard English form.',
'- FRENCH STRATIGRAPHIC TERMS. Translate French terms precisely: système = System, série = Series, étage = Stage, groupe = Group, formation = Formation, membre = Member, zone = Zone. French geological texts may use \'étage\' for stage and preserve Latin-based international stage names.',
'- SPANISH STRATIGRAPHIC TERMS. Translate Spanish terms precisely: sistema = System, serie = Series, piso = Stage, grupo = Group, formación = Formation, miembro = Member, zona = Zone. Spanish texts often retain international stage names in Latin (e.g., \'Maastrichtiense\').',
'- PORTUGUESE STRATIGRAPHIC TERMS. Translate Portuguese terms precisely: sistema = System, série = Series, andar = Stage, grupo = Group, formação = Formation, membro = Member, zona = Zone. Brazilian Portuguese may use local stage names; translate to standard international terminology.',
'- KOREAN, GERMAN, FRENCH, SPANISH, PORTUGUESE: When encountering species names in these languages, ALWAYS output standard Latin binomials. Translate age and formation names to English while preserving proper nouns (locality names, formation names, etc.).',
'- READ SPECIES NAMES CAREFULLY. Names are small italic Latin binomials, often densely packed and rotated at an angle. OCR can produce slight misreads. Transcribe ONLY what you can actually read from the chart: if a name is unclear, leave the `species` field empty and put "unclear" in the per-row `note` field — never invent a plausible-looking taxon name to fill a gap. Do not merge two names, do not split one name, and do not invent names. Preserve open nomenclature qualifiers exactly as printed: sp., cf., aff., ?, spp. are meaningful taxonomic distinctions — do not strip or normalize them.',
'- AUTHOR AND YEAR. When the chart or its caption prints a binomial authority and year (e.g. "De Wever & Dumitrica, 2002"), capture them in the per-row `author_year` string. Leave `author_year` empty when not visible; do not fabricate authorities.',
'- BE COMPLETE. Extract EVERY visible species range line, including short single-bed ranges and faint lines. Do not skip a line just because its range is short or its text is faint.',
'- Only extract what you can READ from the chart. Do not invent data that is not present.',
'- UNCERTAINTY IS DATA. If endpoint_kind or occurrence_mode cannot be determined directly from the chart or caption, emit "unknown". NEVER default an unreadable or absent classification to "observed" or "in_situ".',
'- If a species appears in multiple sections, emit one entry per section.',
'- If a biozone appears in multiple sections with different thicknesses, emit one entry per section (set "section" and that section\'s "thickness_m"), so multi-section thickness data is not lost.',
'- Preserve bed/level numbers exactly as printed (e.g. \'Bed 23c\', \'Bed 27a\').',
'- If the chart is NOT a stratigraphic range/distribution chart, return all arrays empty and confidence 0.0.',
'- Return JSON only, no markdown fences, no commentary.',
// BORROW-2026-09-20 (A+B): the coverage contract + the 0-999 axis positions.
_contractClauses('species_ranges', 'species range line',
  'a dash or em-dash in the cell, a tick with no bar, a column that stops ' +
  'and leaves the level plainly empty'),
'\n' + _axisPositionClause('species_ranges', POS_FIELDS.range_chart,
  'the vertical stratigraphic axis of that section\'s column',
  'the bottom (oldest) edge of the plotted frame',
  'the top (youngest) edge of the plotted frame',
  '{"vertical": {"at_0": 1, "at_999": 24, "unit": "bed"}}',
  'Calibrate the vertical axis only when it carries readable numbers at ' +
  'both ends (bed indices count as numbers: 1, 2, 3 ...).'),
'- A `not_drawn` ROW IS STILL A ROW: keep its `species` and `section` filled ' +
'(the taxon is named in the column header and the section is the column it ' +
'sits in — that is what identifies the cell), and leave only the range / ' +
'biozone fields empty. A `not_drawn` row with no `species` cannot be ' +
'attributed and reads as a gap, which is exactly what you are asked to avoid.',
_degradationClause()
].join('\n');

// Optional per-chart-language hint appended to the user message.
const CHART_LANG_HINT = {
  auto: '',
  zh: 'The chart text is primarily in Chinese. ',
  en: 'The chart text is primarily in English. ',
  ja: 'The chart text is primarily in Japanese. ',
  ru: 'The chart text is primarily in Russian. ',
  de: 'The chart text is primarily in German. ',
  fr: 'The chart text is primarily in French. ',
  es: 'The chart text is primarily in Spanish. ',
  ko: 'The chart text is primarily in Korean. ',
  pt: 'The chart text is primarily in Portuguese. ',
};

// Columnar-section (stratigraphic column / columnar correlation chart)
// extraction prompt. Mirror of rca_core/prompt.py:COLUMNAR_SECTION_SYSTEM_PROMPT.
// The strings below MUST stay byte-identical with the Python side; both
// versions are exercised by tests_core.test_columnar_prompt to detect drift.
const COLUMNAR_SECTION_SYSTEM_PROMPT = [
'You are an expert structural geologist reading a COLUMNAR SECTION chart (also called a stratigraphic column, or a columnar-section correlation chart when several columns are stacked side by side).',
'',
'Each vertical column (section) in the figure represents a measured outcrop or composite section. The vertical axis is age/stratigraphic-position (top = young, bottom = old); lithology is shown by symbols/patterns; fossil sample horizons are marked by letters with brackets (e.g. J, T). Correlation lines may connect key beds between sections.',
'',
'The figure text may be in Chinese, English, Japanese, or Russian. Read whichever language is present. Translate age/formation/legend free-text fields into English while preserving proper names and section IDs exactly as printed.',
'',
'Extract every piece of geological information visible as strict JSON:',
'',
'{',
'  "sections": [',
'    {',
'      "id": "Ki-1" (string, the column / section label as printed, e.g. Ki-1, Column A, Section 3),',
'      "group": "Kurohone-Kiryu Complex | Lower" (string, the stratigraphic group / formation / complex this column is assigned to, or empty),',
'      "lithology_blocks": [',
'        {"pattern": "chert" or "sandstone" or "mudstone" or "limestone" (free text matches the legend key),',
'         "range_top_idx": 8 (int, the higher/younger block boundary, 1-indexed from bottom),',
'         "range_base_idx": 1 (int, the lower/older boundary)}',
'      ],',
'      "age_units": [',
'        {"label": "Lower" or "Upper Cambrian" (free text),',
'         "range_top_idx": 8, "range_base_idx": 4},',
'      ],',
'      "samples": [',
'        {"bed_idx": 5 (int, the bed/sampling layer index, 1-indexed from bottom),',
'         "fossil_marker": "J" or "T" or "J|T" (string, the legend marker as printed),',
'         "ref": "Kamata, 1996" (string, citation if visible, else empty)}',
'      ],',
'      "coordinates_text": "Not visible in chart" (string),',
'      "thickness_m": "500 m" (string, scale-bar reading if visible, else empty),',
'      "confidence_by_section": 0.7 (float)',
'    }',
'  ],',
'  "fossil_legend": [{"marker": "J", "meaning": "Jurassic radiolaria"}],',
'  "lithology_legend": [{"pattern": "chert" or description, "meaning": "Chert"}],',
'  "cross_beds": [{"from_section": "Ki-1", "from_bed_idx": 3, "to_section": "Ki-2", "to_bed_idx": 4}],',
'  "overall_confidence": 0.6 (float, 0.0-1.0 reflecting certainty across the whole figure)',
'}',
'',
'Rules:',
'- DO NOT invent sections or beds. Only extract what you can READ from the figure.',
'- Section IDs (the column labels at the top) MUST be preserved exactly as printed.',
'- Bed/horizon indices are 1-indexed from BOTTOM (oldest = 1).',
'- Preserve range_top_idx (higher/younger) >= range_base_idx (lower/older). If unknown, leave the index empty.',
'- The confidence_by_section float reflects the section-level extraction quality; overall_confidence is figure-wide.',
'- If the figure is NOT a columnar section / columnar correlation chart, return all arrays empty and confidence 0.0.',
'- Return JSON only, no markdown fences, no commentary.',
_degradationClause()
].join('\n');

// Abundance-diagram (pollen / percentage / relative-abundance diagram)
// extraction prompt. Mirror of rca_core/prompt.py:ABUNDANCE_DIAGRAM_SYSTEM_PROMPT.
// Kept roughly in sync with the Python side; tests_core.test_abundance_prompt
// checks both ends carry the same key tokens.
const ABUNDANCE_DIAGRAM_SYSTEM_PROMPT = [
'You are an expert palynologist / micropaleontologist reading a fossil ABUNDANCE DIAGRAM (also called a pollen diagram, relative-abundance diagram, or percentage diagram).',
'',
'The diagram plots stratigraphic level / depth on the VERTICAL axis (top = shallow/young, bottom = deep/old) and one taxon (or group) per COLUMN across the horizontal axis. Each column is a curve or a horizontal bar/silhouette whose width at a given level encodes that taxon\'s abundance (a percentage, count, or a relative categorical level) at that level. Pollen zones (assemblage zones) are often marked as horizontal bands on the right.',
'',
'The chart text may be in Chinese, English, Japanese, or Russian. Read whichever language is present. Regardless of the source language, ALWAYS return scientific taxon names as their standard Latin names (e.g. "Pinus", "Quercus", "Artemisia"), and translate site / age / zone free-text fields into English while preserving proper names.',
'',
'Extract every piece of information visible in the diagram as strict JSON with these fields:',
'',
'{',
'  "sites": [',
'    {',
'      "name": "Lake Suigetsu core SG06" (string, the core / site / locality name, or empty),',
'      "location": "35N, 135E" or "Not visible in chart" (string),',
'      "age_range": "Holocene - Late Glacial" (string),',
'      "depth_unit": "cm" or "m" (string, the unit of the depth axis)',
'    }',
'  ],',
'  "abundances": [',
'    {',
'      "taxon": "Pinus" (string, the taxon / pollen-type / group name for the column),',
'      "site": "Lake Suigetsu core SG06" (string, must match a site.name, or empty when only one site),',
'      "level": "120 cm" or "Sample 5" (string, the stratigraphic level / sample label as printed),',
'      "depth": "120" (string, the numeric depth if legible, else empty),',
'      "abundance": "35" (string, the value read at that level — a percentage, a count, or a relative level like "present"/"common"/"abundant"),',
'      "abundance_unit": "%" or "count" or "relative" (string, how the abundance is expressed),',
// BORROW-2026-09-20 (B): the row's level as an independent 0-999 read of the
// vertical axis (extractor.geometry_from_row).
'      "pos_0_999": 768 (int, 0-999 position of this level on the depth/level axis: 0 = bottom/oldest edge of the frame, 999 = top/youngest/shallowest edge; empty when not judgeable),',
// BORROW-2026-09-20 (A): the three-state answer + optional reason codes.
'      "response_kind": "extracted" (string, REQUIRED: one of "extracted", "not_drawn", "uncertain" — see the ANSWER EVERY REQUESTED CELL rule below),',
'      "reason_codes": [] (array of strings, optional; lowercase slugs from the closed list in the REASON CODES rule — e.g. ["not_drawn"] on a row the diagram leaves blank at this level; empty when nothing applies)',
'    }',
'  ],',
'  "zones": [',
'    {',
'      "name": "PAZ-3 (Pinus-Quercus)" (string, the pollen-assemblage-zone label),',
'      "age": "Early Holocene" (string),',
'      "level_range": "80-140 cm" (string, the depth / level span of the zone)',
'    }',
'  ],',
// BORROW-2026-09-20 (B): optional figure-level calibration of the depth axis.
'  "axis_calibration": {"vertical": {"at_0": 520, "at_999": 0, "unit": "cm"}} (optional object, ONLY when the depth/level axis carries a readable number at both ends of the plotted frame; omit otherwise — the rows\' positions stay uncalibrated evidence)',
'  "confidence": 0.0-1.0 reflecting your certainty in the extraction overall',
'}',
'',
'Rules:',
'- ONE COLUMN = ONE TAXON. Each abundance curve / bar column is a single taxon (or summary group like \'total trees\'). Read the taxon name from the (often vertically-rotated) column header. Do not merge two columns, do not split one column.',
'- READ VALUES PER LEVEL. Emit one `abundances` entry for each (taxon, level) intersection you can read. If a diagram is dense, prioritise the levels where the curve has a clear, readable value; do not invent values between plotted points.',
'- PERCENT vs COUNT vs RELATIVE. If the axis shows a percentage scale, set abundance_unit to "%"; if it shows raw counts, use "count"; if the column only shows presence/silhouette categories, use "relative" and put the category word into `abundance`.',
'- ZONES ARE NOT TAXA. Pollen-assemblage-zone labels (anything like \'PAZ\', \'Zone\', \'assemblage\') go into `zones`, NEVER into `abundances` as a taxon.',
'- Preserve level / sample labels exactly as printed (e.g. \'120 cm\', \'Sample 5\', \'Unit 2b\').',
'- Only extract what you can READ from the diagram. Do not invent data that is not present.',
'- If the figure is NOT a fossil abundance / pollen diagram, return all arrays empty and confidence 0.0.',
'- Return JSON only, no markdown fences, no commentary.',
// Sum-to-100 constraint for percentage diagrams (parity with rca_core/prompt.py:179-180)
'- SUM-TO-100. For each level (sample), the sum of all taxon percentages should equal 100 ± 5%. If your reading differs significantly, lower the row confidence and note the discrepancy in the row note field.',
// BORROW-2026-09-20 (A+B): the coverage contract + the 0-999 axis positions.
_contractClauses('abundances', 'taxon / level cell',
  'a dash in that column at a level the diagram does sample, a column ' +
  'that is simply blank there while its neighbours carry a curve'),
'\n' + _axisPositionClause('abundances', POS_FIELDS.abundance_diagram,
  'the vertical depth / level axis',
  'the bottom (deepest / oldest) edge of the plotted frame',
  'the top (shallowest / youngest) edge of the plotted frame',
  '{"vertical": {"at_0": 520, "at_999": 0, "unit": "cm"}}',
  'Calibrate the depth axis only when a number is legible at both ends; on ' +
  'a depth-below-surface axis the bottom end is the LARGER number, which is ' +
  'expected (at_0 = 520, at_999 = 0).'),
'- ZERO IS NOT A GAP, AND A GAP IS NOT ZERO. If the column shows this taxon ' +
'at that level with no bar / no curve, that is `"not_drawn"` with an empty ' +
'`abundance` — unless the diagram actually plots 0 %, which is a reading and ' +
'stays `"extracted"` with `abundance: "0"`. If the level was never ' +
'analysed for this taxon (a different counting scheme, a gap in the core), ' +
'emit the row as `"not_drawn"` and add the `out_of_scope` code plus a short ' +
'`note`, so the two cases stay separable downstream.',
_degradationClause()
].join('\n');

const PHYLOGENETIC_TREE_SYSTEM_PROMPT = [
'You are an expert evolutionary biologist / phylogeneticist reading a PHYLOGENETIC TREE figure (cladogram, phylogram, or molecular phylogeny) from a micropaleontology / paleontology paper.',
'',
'The figure is a rooted directed tree drawn left-to-right or top-to-bottom. Internal nodes represent hypothetical common ancestors; terminal nodes (leaves) are the observed taxa (species, genera, or higher clades). Each branch may carry a numeric SUPPORT value (bootstrap / posterior probability) printed at the node, an optional BRANCH LENGTH (in substitutions per site or My), and for marine plankton groups each terminal may be color-coded by DEPTH RANGE (e.g. epipelagic / mesopelagic / bathypelagic) with a legend.',
'',
'The figure text may be in Chinese, English, Japanese, or Russian. Read whichever language is present. Regardless of source language, ALWAYS return scientific taxon names as their standard Latin binomials (e.g. \"Spasmaria\", \"Polycystinea\"), and translate free-text legend / caption fields into English while preserving proper names.',
'',
'Extract every piece of information visible in the tree as strict JSON with these fields:',
'',
'{',
'  \"version\": \"1\" (string, schema version; always \"1\"),',
'  \"metadata\": {',
'    \"taxon_group\": \"Radiolaria\" (string, the higher taxon group the tree belongs to, or empty),',
'    \"extraction_timestamp\": \"2026-07-26THH:MM:SS\" (string, ISO-like timestamp at extraction),',
'    \"image_source\": \"<filename>\" (string, the source image filename or caption, or empty),',
'    \"total_nodes\": 42 (int, total number of nodes including root and leaves),',
'    \"root_name\": \"Spasmaria\" (string, the name printed at the root, or empty)',
'  },',
'  \"nodes\": [',
'    {',
'      \"id\": \"n0\" (string, unique node id, \"n0\" for the root, \"n1\", \"n2\", ... in traversal order),',
'      \"name\": \"Spasmaria\" (string, the taxon / clade name as printed; transcribe what is on the figure, never invent),',
'      \"support\": 89 (float, the numeric support value at the node — bootstrap %, posterior probability, or empty when not shown),',
'      \"support_confidence\": 0.95 (float, 0.0–1.0, your confidence in the support number; lower when the digit is ambiguous),',
'      \"branch_length\": 0.12 (float, the branch length above this node, or empty when not shown / cladogram),',
'      \"depth_range_m\": \"0-200\" (string, the depth band the color of this terminal maps to, e.g. \"0-200\"; empty for internal nodes),',
'      \"depth_confidence\": 0.88 (float, 0.0–1.0, your confidence in the color→depth mapping),',
'      \"sequence_count\": 15 (int, the number of sequences / terminals this clade represents as printed, or empty),',
'      \"is_leaf\": false (bool, true only for terminal taxa; false for internal ancestor nodes),',
'      \"parent\": \"n1\" (string, the id of the immediate parent node; null only for the root)',
'    }',
'  ],',
'  \"root_ids\": [\"n0\"] (array of strings, the ids of root nodes; usually one entry, multiple only when the figure is a forest),',
'  \"legend\": {',
'    \"depth_colors\": {',
'      \"epipelagic\":   {\"hex\": \"#003366\", \"depth_m\": \"0-200\"} (object, the depth-band name → hex color and depth range; only include bands visible in the legend),',
'      \"mesopelagic\":  {\"hex\": \"#00BFFF\", \"depth_m\": \"200-1000\"},',
'      \"bathypelagic\": {\"hex\": \"#90EE90\", \"depth_m\": \"1000-4000\"}',
'    },',
'    \"sequence_count_min\": 1 (int, smallest sequence count used for circle-size scaling, or empty),',
'    \"sequence_count_max\": 47 (int, largest sequence count used for circle-size scaling, or empty)',
'  },',
'  \"confidence\": 0.0-1.0 reflecting your certainty in the extraction overall',
'}',
'',
'Rules:',
'- READ THE TREE TOPOLOGY CAREFULLY. Transcribe the parent-child structure as drawn — do NOT re-root, do NOT collapse polytomies into bifurcations unless the figure shows them as bifurcations, and do NOT reorder clades. Traverse from the root outward; emit one entry per node.',
'- NODE IDS. Assign ids in traversal order: \"n0\" = root, then \"n1\", \"n2\", ... in the order the children first appear (left-to-right or top-to-bottom). Every internal node and every leaf gets exactly one entry. Refer to parents by id only.',
'- INTERNAL vs LEAF. A node is a leaf (`is_leaf: true`) iff it is a terminal taxon (no descendants drawn). All other nodes are internal (`is_leaf: false`) even when they carry a printed name.',
'- PARENT INVARIANT. Every ROOT\'s `parent` must be null. Every non-root node\'s `parent` must equal an existing node\'s `id`. Violating this rule produces an invalid tree.',
'- SUPPORT VALUES. When a number is printed at a node (e.g. \"89\", \"0.97\"), put it into `support` as a float and set `support_confidence` according to how clearly you could read the digit (0.95+ when crystal clear, 0.5–0.7 when ambiguous). If no support value is printed at the node, leave `support` and `support_confidence` as null — do not invent a plausible-looking number.',
'- BRANCH LENGTH. Only fill `branch_length` when the figure is a phylogram with an explicit scale bar AND the value can be read with confidence. For cladograms (no scale bar), leave it null.',
'- DEPTH RANGE COLOR. Marine-plankton trees often color terminals by depth band. If a color→depth legend is printed, map each terminal\'s color to the corresponding depth string (e.g. \"0-200\") in `depth_range_m`. Leave `depth_range_m` empty for internal nodes and for terminals whose color does not match any legend band. Use the `legend.depth_colors` object to record the legend bands you used.',
'- SEQUENCE COUNT. Some trees annotate clades with a circle whose size encodes the number of sequences / terminals. Put that number into `sequence_count` when printed in the figure (often in or beside the node). Otherwise leave it null.',
'- TAXON GROUP / ROOT NAME. Read the higher taxon group from the title or caption (e.g. \"Radiolaria\", \"Foraminifera\") into `metadata.taxon_group`. Read the name printed at the root into `metadata.root_name`.',
'- BE COMPLETE. Extract EVERY visible node, including unsampled / placeholder taxa and short side branches. Do not skip a leaf just because its label is faint — degrade gracefully instead.',
'- Only extract what you can READ from the figure. Do not invent topology, support values, or taxa that are not present.',
'- If the figure is NOT a phylogenetic tree / cladogram / phylogram, return all arrays empty and confidence 0.0.',
'- Return JSON only, no markdown fences, no commentary.',
// This slot previously held a hardcoded copy of the clause written with
// ASCII hyphens ("0.3-0.5"), which had drifted from the shared helper and
// from rca_core/prompt.py (which calls _degradation_clause here).
_degradationClause()
].join('\n');

// NEW: Chemical Stratigraphy Chart (isotopic curves, elemental data)
// Supports: δ13C, δ18O, 87Sr/86Sr, carbon isotope, oxygen isotope curves
// Mirror of rca_core/prompt.py:CHEMICAL_STRATIGRAPHY_SYSTEM_PROMPT.
const CHEMICAL_STRATIGRAPHY_SYSTEM_PROMPT = [
'You are an expert in geochemistry and stratigraphy reading a CHEMICAL STRATIGRAPHY chart (also called an isotopic curve, elemental ratio diagram, or chemostratigraphic profile).',
'',
'The chart plots geochemical data (isotopic ratios, elemental concentrations, or mineralogical indices) against stratigraphic position. The vertical axis is depth/age (top = young, bottom = old); the horizontal axis shows the geochemical value. Common curves include:',
'  - Carbon isotopes: δ13C (per mil, ‰)',
'  - Oxygen isotopes: δ18O (per mil, ‰)',
'  - Strontium isotopes: 87Sr/86Sr',
'  - Other: δ15N, δ34S, major/trace elements',
'',
'The chart text may be in Chinese, English, Japanese, Russian, German, or French. Read whichever language is present. Translate geological terms into English while preserving proper names, sample labels, and unit notation exactly as printed.',
'',
'Extract every piece of information visible as strict JSON:',
'',
'{',
'  "metadata": {',
'    "section_name": "LC-01" (string, the measured section / core identifier),',
'    "location": "Cretaceous-Paleogene boundary, Zumaya section" (string, geographic location),',
'    "latitude": "43.3N" or "Not visible" (string),',
'    "longitude": "2.2W" or "Not visible" (string),',
'    "age_range": "Late Maastrichtian - Early Danian" (string, the age span covered),',
'    "curve_types": ["δ13C", "δ18O"] (array of strings, the geochemical parameters plotted)',
'  },',
'  "data_points": [',
'    {',
'      "sample_id": "Zum-23" (string, the sample/level identifier as printed),',
'      "depth_m": "12.5" (string, the depth in meters from section base, or empty if not visible),',
'      "age_ma": "66.1" (string, the numeric age in Ma if legible, else empty),',
'      "stage": "Maastrichtian" (string, the chronostratigraphic stage if legible, else empty),',
'      "values": {',
'        "δ13C": "-1.24" (string, the δ13C value in ‰, or empty if not measured at this level),',
'        "δ18O": "-4.5" (string, the δ18O value in ‰, or empty),',
'        "87Sr_86Sr": "0.707842" (string, the Sr isotope ratio, or empty),',
'        "other": {} (object, any additional geochemical values as key-value pairs)',
'      },',
'      "lithology": "chalk" (string, the lithology at this level if shown, else empty),',
'      "fossil_horizon": "CF2 nannofossil zone" (string, any biostratigraphic marker at this level, else empty),',
'      "note": "slight weathering" (string, any anomaly or observation, else empty),',
// BORROW-2026-09-20 (A+B): the three-state answer, the reason codes, and the
// independent 0-999 read of the vertical axis.
'      "response_kind": "extracted" (string, REQUIRED: one of "extracted", "not_drawn", "uncertain" — see the ANSWER EVERY REQUESTED CELL rule below),',
'      "reason_codes": ["inferred"] (array of strings, optional; lowercase slugs from the closed list in the REASON CODES rule; empty when nothing applies),',
'      "pos_0_999": 583 (int, 0-999 position of this sample on the vertical axis: 0 = bottom/oldest edge of the frame, 999 = top/youngest edge; empty when not judgeable)',
'    }',
'  ],',
'  "events": [',
'    {',
'      "type": "isotope_excursion" (string, one of: isotope_excursion, hiatus, reworked, contamination, other),',
'      "depth_m": "15.2" (string, depth at which the event occurs),',
'      "age_ma": "66.0" (string, age if known, else empty),',
'      "name": "CIE - Carbon Isotope Excursion" (string, the event name if applicable),',
'      "magnitude": "+2‰ shift in δ13C" (string, the magnitude of the excursion or anomaly),',
'      "description": "sharp negative shift marking the K-Pg boundary" (string, free-text description),',
// BORROW-2026-09-20 (B): the event horizon as a 0-999 position too.
'      "pos_0_999": 493 (int, 0-999 position of this event on the vertical axis, same scale as in `data_points`)',
'    }',
'  ],',
'  "intervals": [',
'    {',
'      "name": "Lower Maastrichtian" (string, the interval name as printed),',
'      "top_depth_m": "20.0" (string),',
'      "base_depth_m": "30.0" (string),',
'      "top_age_ma": "70.5" (string),',
'      "base_age_ma": "72.5" (string),',
// BORROW-2026-09-20 (B): the two interval boundaries, same 0-999 scale.
'      "top_pos_0_999": 333 (int, 0-999 position of the top boundary on the vertical axis; 0 = bottom/oldest edge, 999 = top/youngest edge),',
'      "base_pos_0_999": 0 (int, the same scale for the base boundary),',
'      "characteristic_values": "δ13C: -1.0 to -1.5‰" (string, the typical range in this interval),',
'      "lithology": "marl" (string)',
'    }',
'  ],',
// BORROW-2026-09-20 (B): optional figure-level calibration of the vertical axis.
'  "axis_calibration": {"vertical": {"at_0": 30.0, "at_999": 0.0, "unit": "m"}} (optional object, ONLY when the depth (or age) axis carries a readable number at both ends of the plotted frame; omit otherwise — the positions stay uncalibrated evidence)',
'  "confidence": 0.8 (float, 0.0-1.0 reflecting certainty across the whole figure)',
'}',
'',
'Rules:',
'- Read values at each plotted point or tick mark. If the curve is smooth and continuous, interpolate values at regular depth/age intervals (every 0.5m or every sample level).',
'- Preserve units exactly as shown: ‰ for per mil, Ma for million years, m for meters.',
'- For dual or multiple curves, extract values from EACH curve at matching depths.',
'- Mark any clear excursions, anomalies, or excursions from baseline as events.',
'- Identify lithology changes when shown on the margins.',
'- Only extract what you can READ from the chart. Do not invent data points.',
'- If the figure is NOT a chemical stratigraphy / isotopic curve chart, return all arrays empty and confidence 0.0.',
'- Return JSON only, no markdown fences, no commentary.',
// BORROW-2026-09-20 (A+B): the coverage contract + the 0-999 axis positions.
_contractClauses('data_points', 'sampled level',
  'a tick / sample marker with no value plotted beside it, a level listed ' +
  'on the depth axis whose curve has a visible gap there'),
'\n' + _axisPositionClause('data_points / events / intervals',
  POS_FIELDS.chemical_stratigraphy,
  'the vertical depth (or age) axis',
  'the bottom (deepest / oldest) edge of the plotted frame',
  'the top (shallowest / youngest) edge of the plotted frame',
  '{"vertical": {"at_0": 30.0, "at_999": 0.0, "unit": "m"}}',
  'Calibrate whichever quantity the vertical axis actually carries (depth ' +
  'or age) as "vertical", with the number printed at each end; on a ' +
  'depth-below-top axis the bottom end is the LARGER number. If both a ' +
  'depth and an age scale are drawn, calibrate the one the data points are ' +
  'positioned against.'),
'- A MISSING MEASUREMENT IS AN ANSWER, AN UNMEASURED LEVEL IS NOT THE SAME ' +
'ONE. When the profile samples this level but a curve has no value there, ' +
'emit the row with the value fields empty, `response_kind: "not_drawn"` and ' +
'the `no_label` / `truncated` code when one of them explains why; when the ' +
'whole interval is outside what the figure plots, add `out_of_scope`.',
_degradationClause()
].join('\n');

// NEW: Paleogeographic Map (paleolatitude, paleocontinental positions)
// Mirror of rca_core/prompt.py:PALEOMAP_SYSTEM_PROMPT.
const PALEOMAP_SYSTEM_PROMPT = [
'You are an expert in paleogeography and plate tectonics reading a PALEOGEOGRAPHIC MAP.',
'',
'The map shows the geographical distribution of land, sea, and key geological features at a specific geological time slice. Features may include:',
'  - Paleocontinents and terranes with labels',
'  - Ancient coastlines and epicontinental seas',
'  - Paleolatitude grid lines and equator indicator',
'  - Key tectonic features (ridges, trenches, faults)',
'  - Ancient biogeographic provinces or realms',
'  - Key fossil sites or localities marked',
'',
'The chart text may be in Chinese, English, Japanese, Russian, German, or French. Read whichever language is present. Translate place names and geological terms into English while preserving proper names exactly as printed.',
'',
'Extract every piece of information visible as strict JSON:',
'',
'{',
'  "metadata": {',
'    "time_slice": "Late Permian (Lopingian)" (string, the geological time period depicted),',
'    "approximate_age_ma": "255" (string, approximate age in Ma if stated, else empty),',
'    "map_title": "Paleogeography of Tethys Realm" (string, the map title or caption),',
'    "projection": "Equirectangular" or "Robinson" (string, map projection if shown, else empty),',
'    "scale": "1:50,000,000" (string, map scale if visible, else empty),',
'    "source": "Scotese, 2021" (string, map source/citation if visible, else empty)',
'  },',
'  "continents": [',
'    {',
'      "name": "Laurasia" (string, the continental block or terrane name),',
'      "type": "supercontinent" (string, one of: supercontinent, continent, terrane, islandarc, plateau),',
'      "coordinates": [[30, 60], [60, 90], ...] (array of [lat, lon] coordinate pairs forming the polygon outline, simplified),',
'      "paleolatitude": "30N" (string, approximate paleolatitude if indicated, else empty),',
'      "note": "includes present-day North America, Europe, Asia" (string, additional info)',
'    }',
'  ],',
'  "oceans_seas": [',
'    {',
'      "name": "Paleo-Tethys Ocean" (string),',
'      "type": "ocean" (string, one of: ocean, sea, epicontinental_sea, gulf, basin),',
'      "coordinates": [[10, 60], [20, 80], ...] (array of [lat, lon] pairs),',
'      "note": "closing northward" (string)',
'    }',
'  ],',
'  "tectonic_features": [',
'    {',
'      "name": "Mongolia-Okhotsk Ocean" (string, or empty if unnamed),',
'      "type": "suture" (string, one of: suture, fault, ridge, trench, rift, arc, other),',
'      "coordinates": [[45, 100], [55, 120]] (array of [lat, lon] pairs),',
'      "direction": "closing" (string, one of: opening, closing, active, passive, unknown),',
'      "description": "Collisional suture between Siberia and Mongolia" (string)',
'    }',
'  ],',
'  "biogeographic_realms": [',
'    {',
'      "name": "Tethys Realm" (string),',
'      "type": "marine_realm" (string, one of: marine_realm, terrestrial_realm, mixed),',
'      "coordinates": [[-30, 0], [40, 120]] (bounding box [lat_min, lon_min, lat_max, lon_max]),',
'      "characteristic_fauna": "radiolaria, ammonoids, conodonts" (string),',
'    }',
'  ],',
'  "fossil_sites": [',
'    {',
'      "name": "Gartnerkofel" (string, the locality name),',
'      "lat_lon": "46.5N, 13.5E" (string, approximate present-day coordinates),',
'      "age": "Late Permian" (string),',
'      "fossils": "radiolaria, algae" (string, the fossil assemblage found there),',
'      "marker_type": "star" (string, the symbol used on the map)',
'    }',
'  ],',
'  "paleolatitude_indicators": [',
'    {',
'      "type": "equator" (string, one of: equator, tropics, subtropics, temperate, polar),',
'      "coordinates": [[0, -180], [0, 0], [0, 180]] (array of [lat, lon] pairs defining the line)',
'    }',
'  ],',
'  "confidence": 0.75 (float, 0.0-1.0)',
'}',
'',
'Rules:',
'- Extract continental/ocean outlines as simplified coordinate polygons. Use present-day lat/lon reference frame.',
'- For complex coastlines, simplify to major vertices only (10-50 points per continent).',
'- Preserve all tectonic feature labels and directional indicators.',
'- Extract biogeographic boundaries when visible.',
'- Include all marked fossil localities with their associated information.',
'- Paleocoordinates are in present-day reference frame unless the map specifies otherwise.',
'- Only extract what you can READ from the map. Do not invent features or coordinates.',
'- THEMATIC DISTRIBUTION MAPS COUNT. If the figure is a map with sample / station dots coloured or sized by a value (radiolarian abundance, percentage, species richness), extract every labelled dot into `fossil_sites` (name = station / core label, location = printed coordinates if any) and put its value into the note field of that site - these are data, not decoration.',
'- If the figure is NOT a paleogeographic map, return all arrays empty and confidence 0.0.',
'- Return JSON only, no markdown fences, no commentary.',
_degradationClause()
].join('\n');

// NEW: Scatter Plot / Biplot (correlation diagrams, bivariate plots)
// Supports: taxonomy-environment scatter plots, morphospace plots, etc.
// Mirror of rca_core/prompt.py:SCATTER_PLOT_SYSTEM_PROMPT.
const SCATTER_PLOT_SYSTEM_PROMPT = [
'You are an expert in paleontology and biostratigraphy reading a SCATTER PLOT or BIPLOT diagram.',
'',
'The diagram plots two or more variables against each other (bivariate or multivariate plot). Each point represents an observation, specimen, or sample. Common uses in paleontology include:',
'  - Morphospace plots (shape variation along axes)',
'  - Taxonomy-environment scatter (e.g., diversity vs. latitude)',
'  - Elemental biplots (e.g., Sr/Ca vs. Mg/Ca)',
'  - Size-frequency distributions',
'  - Stratigraphic range plots',
'',
'The chart text may be in Chinese, English, Japanese, Russian, German, or French. Read whichever language is present. Translate labels into English while preserving values, units, and groupings exactly as printed.',
'',
'Extract every piece of information visible as strict JSON:',
'',
'{',
'  "metadata": {',
'    "title": "Morphospace of Late Triassic Radiolaria" (string, the plot title),',
'    "x_axis_label": "First axis (PC1) - 42.5%" (string, the X axis label with variance if shown),',
'    "y_axis_label": "Second axis (PC2) - 28.3%" (string, the Y axis label),',
'    "z_axis_label": "Third axis (PC3) - 15.2%" (string, Z axis if 3D plot, else empty),',
'    "x_unit": "dimensionless" (string, the unit of X axis, or "dimensionless"),',
'    "y_unit": "dimensionless" (string, the unit of Y axis),',
'    "n_points": 156 (int, approximate total number of plotted points if legible, else empty),',
'    "grouping_variable": "genus" (string, what the coloring/grouping represents, or empty)',
'  },',
'  "groups": [',
'    {',
'      "name": "Albaillella" (string, the group name as in the legend),',
'      "color": "#FF5733" (string, the color code from the legend),',
'      "marker": "circle" (string, one of: circle, square, triangle, diamond, cross, plus, other),',
'      "n_points_visible": 12 (int, approximate count of points in this group, else empty),',
'      "description": "typical Late Permian morphotype" (string, any notes)',
'    }',
'  ],',
'  "points": [',
'    {',
'      "x": "-2.34" (string, the X coordinate value),',
'      "y": "1.56" (string, the Y coordinate value),',
'      "z": "" (string, Z coordinate for 3D plots, else empty),',
'      "group": "Albaillella" (string, the group this point belongs to, or empty),',
'      "label": "Specimen A-12" (string, the point label if shown, else empty),',
'      "note": "juvenile form" (string, any annotation for this point, else empty),',
// BORROW-2026-09-20 (A+B): the three-state answer, the reason codes and the
// two independent 0-999 axis reads (x_pos / y_pos are the names the parser
// folds into the row's `geometry` sidecar).
'      "response_kind": "extracted" (string, REQUIRED: one of "extracted", "not_drawn", "uncertain" — see the ANSWER EVERY REQUESTED CELL rule below),',
'      "reason_codes": [] (array of strings, optional; lowercase slugs from the closed list in the REASON CODES rule; empty when nothing applies),',
'      "x_pos_0_999": 207 (int, 0-999 position of the point along the X axis: 0 = left edge of the plot frame, 999 = right edge; empty when not judgeable),',
'      "y_pos_0_999": 759 (int, 0-999 position along the Y axis: 0 = bottom edge, 999 = top edge)',
'    }',
'  ],',
'  "outliers": [',
'    {',
'      "x": "3.45" (string),',
'      "y": "-2.1" (string),',
'      "group": "Pseudoalbaillella" (string),',
'      "reason": "outside 95% confidence ellipse" (string),',
'      "response_kind": "extracted" (string, same three-state contract as `points`),',
'      "x_pos_0_999": 930 (int, same 0-999 X scale as in `points`),',
'      "y_pos_0_999": 150 (int, same 0-999 Y scale as in `points`)',
'    }',
'  ],',
'  "statistics": {',
'    "correlation": "r = 0.72" (string, correlation coefficient if shown),',
'    "regression_line": "y = 0.85x + 0.23" (string, regression equation if shown),',
'    "r_squared": "0.52" (string, R² value if shown),',
'    "p_value": "p < 0.001" (string, statistical significance if shown)',
'  },',
// BORROW-2026-09-20 (B): a scatter plot has TWO continuous axes, so both
// get their own optional calibration (extractor maps x_pos_0_999 -> "x").
'  "axis_calibration": {"x": {"at_0": -4.0, "at_999": 4.0, "unit": "PC1"}, "y": {"at_0": -3.0, "at_999": 3.0, "unit": "PC2"}} (optional object, one entry per continuous axis, ONLY when that axis prints a legible number at both ends of the plot frame; omit an axis you cannot read — its positions stay uncalibrated evidence)',
'  "confidence": 0.7 (float, 0.0-1.0)',
'}',
'',
'Rules:',
'- Extract a representative sample of points (every 5th-10th point if many) to keep output manageable while preserving the overall distribution.',
'- For dense point clouds, describe the density distribution rather than individual points.',
'- Preserve all group names, colors, and marker styles from the legend.',
'- For principal component plots, preserve the variance explained by each axis.',
'- Extract any statistical annotations (regression lines, confidence ellipses, correlation values).',
'- Only extract what you can READ from the plot. Do not invent points or statistics.',
'- If the figure is NOT a scatter plot or biplot, return all arrays empty and confidence 0.0.',
'- Return JSON only, no markdown fences, no commentary.',
// BORROW-2026-09-20 (A+B): the coverage contract + the two 0-999 axis reads.
_contractClauses('points', 'plotted point',
  'a labelled group in the legend that has no symbol anywhere in the ' +
  'frame, or a sample the plot itself marks as empty ("n/a", a filtered ' +
  'specimen)'),
'\n' + _axisPositionClause('points / outliers', POS_FIELDS.scatter_plot,
  'the two plotted axes',
  'the LEFT edge of the plot frame for `x_pos_0_999` and the BOTTOM edge ' +
  'for `y_pos_0_999` (0 = the oldest / smallest end of each axis)',
  'the RIGHT edge for `x_pos_0_999` and the TOP edge for `y_pos_0_999` ' +
  '(999 = the youngest / largest end)',
  '{"x": {"at_0": -4.0, "at_999": 4.0, "unit": "PC1"}, ' +
  '"y": {"at_0": -3.0, "at_999": 3.0, "unit": "PC2"}}',
  'Calibrate each axis separately under its own name ("x" and "y"), with ' +
  'the tick value printed at the two ends of the frame.'),
'- THINNING A DENSE CLOUD IS NOT A REFUSAL. The sampling rule above (every ' +
'5th-10th point) describes the SAME population, so do NOT invent rows for ' +
'points you did not sample. What the contract does bind is honesty about the ' +
'rows you do emit: two overlapping symbols you cannot separate are ' +
'`"uncertain"` (with the `obscured` code), never `"not_drawn"`.',
_degradationClause()
].join('\n');

// ---------------------------------------------------------------------------
// Mirror of rca_core/prompt.py:ZONATION_CHART_SYSTEM_PROMPT
// (UI-REVIEW-2026-09-05): biozonation / correlation charts — the canonical
// radiolarian biochronology figure (e.g. "Correlation of Triassic
// radiolarian zones", zonation tied to ammonoid/conodont zones).
// ---------------------------------------------------------------------------
const ZONATION_CHART_SYSTEM_PROMPT = [
'You are an expert micropalaeontologist reading a BIOSTRATIGRAPHIC zonation / correlation chart — the figure type where several biozonation schemes (each a vertical column of named zones and subzones) are drawn side by side and correlated against each other and against ammonoid / conodont zones, magnetostratigraphic chrons, or geologic-time-scale stages. Typical radiolarian examples: "Correlation of Triassic radiolarian zones and subzones", "Radiolarian zonation tied to ammonoid and conodont zones". The chart may also be a SINGLE zonation column with an age axis. The chart text may be in Chinese, English, Japanese, or Russian — read whichever language is present, and ALWAYS emit zone names and species names as printed (Latin names verbatim, never translated); translate free-text fields into English while preserving proper names (author names, region names).',
'',
'Extract every piece of information visible in the chart as strict JSON with these fields:',
'',
'{',
'  "zonations": [',
'    {',
'      "name": "Bragin (2018), Koryak Highlands" (string, the zonation scheme name of the column — usually author + year and/or region, as printed),',
'      "region": "Koryak Highlands, NE Russia" (string, or empty),',
'      "framework": "radiolarian" (string: radiolarian | ammonoid | conodont | nannofossil | magnetostratigraphic | chronostratigraphic | other),',
'      "reference": "Bragin, 2018" (string, full citation as printed, or empty)',
'    }',
'  ],',
'  "zones": [',
'    {',
'      "name": "Proparvicingula moniliformis Zone" (string, the zone / subzone / assemblage name EXACTLY as printed — keep "Zone"/"Subzone" suffixes and taxon capitalisation),',
'      "zonation": "Bragin (2018), Koryak Highlands" (string, must match a zonations.name, or empty when the chart has only one column),',
'      "rank": "zone" (string: zone | subzone | assemblage | superzone | other — infer from the name suffix or grouping lines),',
'      "age_span": "lower Rhaetian" or "Early Jurassic" (string, the age/epoch text printed beside the zone, or empty),',
'      "base_age": "203.6" (string, numeric base age in Ma ONLY if the axis carries absolute ages — otherwise empty),',
'      "top_age": "201.4" (string, numeric top age in Ma, same rule),',
'      "stage": "Rhaetian" (string, the stage / chronostratigraphic unit the zone is calibrated against, if a stage column exists),',
'      "defined_by": "FAD of Proparvicingula moniliformis" (string, the defining event / index taxon if stated, or empty),',
'      "note": "" (string, anything ambiguous about this row)',
'    }',
'  ],',
'  "correlations": [',
'    {',
'      "from_zone": "Proparvicingula moniliformis Zone" (string, the zone name on one side of a correlation line / band, exactly as printed),',
'      "to_zone": "Crassistephanus thuyensis Zone" (string, the zone on the other end),',
'      "from_zonation": "Bragin (2018)" (string, must match a zonations.name — the column from_zone belongs to; empty in single-column charts),',
'      "to_zonation": "Carter (1993), Queen Charlotte Islands" (string, the other column; empty in single-column charts),',
'      "basis": "direct correlation" (string: direct correlation | shared stage | shared ammonoid zone | shared conodont zone | magnetostratigraphic tie | other — read from the legend if present, else "direct correlation"),',
'      "note": "" (string)',
'    }',
'  ],',
'  "confidence": 0.0-1.0 reflecting your certainty in the extraction overall',
'}',
'',
'Rules:',
'- ZONES ARE ROWS. Every named unit in every column — zone, subzone, assemblage zone, Acme zone, interval of unzoned strata IF labelled — becomes one `zones` entry. Do not merge a zone with its subzones; emit each rank separately with its own `rank` value.',
'- INDEX TAXA ARE NOT ZONES. A zone NAMED after a species ("H. parvus Zone") is a zone; a bare species name in a species column of a RANGE chart is not a zone. Never invent a zone that is not drawn.',
'- CORRELATION LINES ARE DATA. Every horizontal tie line / band linking a zone boundary or zone body across two columns becomes one `correlations` entry. If a line links a zone to a stage column, the stage belongs in the `stage` field of that zone AND as a correlation with the stage name when the stage column is a labelled column of the chart.',
'- ABSOLUTE AGES ONLY FROM THE AXIS. Copy numeric Ma values only when the age axis of the chart prints them; never estimate ages from memory.',
'- Preserve author names, region names and citation text verbatim.',
'- Only extract what you can READ from the chart. Do not invent zones or correlations that are not drawn.',
'- If the figure is NOT a zonation / correlation chart, return all arrays empty and confidence 0.0.',
'- Return JSON only, no markdown fences, no commentary.',
_degradationClause()
].join('\n');


// ---------------------------------------------------------------------------
// Mirror of rca_core/prompt.py:CHART_CLASSIFY_SYSTEM_PROMPT
// (UI-REVIEW-2026-09-07): vision chart-type classification for the
// upgraded auto mode (direct transport only — the backend classifies
// server-side). Terse genre signatures; small max_tokens; strict JSON.
// ---------------------------------------------------------------------------
const CHART_CLASSIFY_SYSTEM_PROMPT = [
'You are a micropalaeontologist classifying the TYPE of a scientific figure (a stratigraphic / palaeontological chart) from a single image. Choose exactly one chart_type from this list:',
'',
'- range_chart: stratigraphic range chart — taxon names along one axis, vertical range lines / bars per taxon spanning stratigraphic columns or bed-number axes, with dots / circles marking occurrences (FAD/LAD).',
'- columnar_section: lithologic column — a single vertical column with pattern fills (bricks, dots, dashes), thickness scale, formation / member names.',
'- abundance_diagram: abundance / percentage diagram — one column per taxon, curves or horizontal bars whose width encodes counts or percentages against a depth / level axis (pollen-diagram style).',
'- phylogenetic_tree: branching tree / cladogram / dendrogram with taxa at the tips.',
'- zonation_chart: biozonation / correlation chart — vertical columns of NAMED ZONES (e.g. "... Zone") drawn side by side, correlated with horizontal tie lines, often against ammonoid / conodont zones or stages.',
'- chemical_stratigraphy: geochemical curves — element / oxide / isotope values plotted against depth or age.',
'- paleomap: palaeogeographic map — continents, oceans, coastlines, locality markers on a projected map.',
'- scatter_plot: x-y scatter / biplot — discrete points, optional regression lines, confidence ellipses.',
'- unknown: none of the above — including photomicrographs / SEM / STEM / thin-section image plates that show specimen photos but no readable data chart.',
'',
'Answer as strict JSON only (no markdown, no commentary):',
'',
'{"chart_type": "<one of the list above>", "reason": "<one short sentence, in English, citing what you see>", "confidence": 0.0-1.0}',
].join('\n');
