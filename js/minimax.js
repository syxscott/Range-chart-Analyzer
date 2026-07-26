// minimax.js - call MiniMax M3 vision API and normalize the result.
// Mirrors RLPE range_chart_extractor.extract_range_chart. Never throws:
// returns { ok, data, error, errorKey, raw, truncated }.
'use strict';

// Downscale an image File to a JPEG/PNG data URL whose long edge <= maxEdge.
// Returns { dataUrl, mime, width, height, resized }.
function rcaLoadAndMaybeResize(file, maxEdge) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error('imageRead'));
    reader.onload = () => {
      const originalDataUrl = reader.result;
      const img = new Image();
      img.onerror = () => reject(new Error('imageRead'));
      img.onload = () => {
        const w = img.naturalWidth;
        const h = img.naturalHeight;
        const longEdge = Math.max(w, h);
        if (!maxEdge || longEdge <= maxEdge) {
          resolve({
            dataUrl: originalDataUrl,
            mime: file.type || 'image/png',
            width: w,
            height: h,
            resized: false,
          });
          return;
        }
        const scale = maxEdge / longEdge;
        const nw = Math.round(w * scale);
        const nh = Math.round(h * scale);
        const canvas = document.createElement('canvas');
        canvas.width = nw;
        canvas.height = nh;
        const ctx = canvas.getContext('2d');
        ctx.drawImage(img, 0, 0, nw, nh);
        // Prefer lossless PNG for downscaled charts so the small italic
        // species names stay sharp — JPEG re-compression blurs dense chart
        // text and is a known cause of OCR misreads. Only keep JPEG when the
        // source is JPEG and the resized image is large (a lossless PNG would
        // be excessively big).
        const resizedIsLarge = (nw * nh) > (2500 * 2500);
        const outMime = (file.type === 'image/jpeg' && resizedIsLarge) ? 'image/jpeg' : 'image/png';
        const dataUrl = outMime === 'image/jpeg'
          ? canvas.toDataURL('image/jpeg', 0.95)
          : canvas.toDataURL('image/png');
        resolve({ dataUrl, mime: outMime, width: nw, height: nh, resized: true });
      };
      img.src = originalDataUrl;
    };
    reader.readAsDataURL(file);
  });
}

// Split a data URL into { mediaType, base64 }.
function rcaSplitDataUrl(dataUrl) {
  const m = /^data:([^;]+);base64,(.*)$/.exec(dataUrl);
  if (!m) return { mediaType: 'image/png', base64: '' };
  return { mediaType: m[1], base64: m[2] };
}

// H8 parity: copy any unknown keys under `_extras` so the model can emit
// extra context without it being silently dropped.
function rcaCarryExtras(item, known) {
  if (!item || typeof item !== 'object') return null;
  const out = {};
  for (const k of Object.keys(item)) {
    if (!known.includes(k)) out[k] = item[k];
  }
  return Object.keys(out).length ? out : null;
}

// Normalize the parsed JSON into the strict result shape.
// P0-5 (REVIEW-2026-07-25): If the model returned a top-level JSON array
// (already wrapped by json-utils.extractBalancedJsonArray as {_array_root:[...]}),
// distribute items into the correct tables by structural key, mirroring
// Python rca_core/extractor.py:440-470 (_classify_array_item).
function rcaUnwrapArrayRoot(parsed) {
  if (!parsed || typeof parsed !== 'object') return parsed;
  if (!Array.isArray(parsed._array_root)) return parsed;
  const dist = {
    sections: [], species_ranges: [], biozones: [], other_fossils: [],
  };
  for (const item of parsed._array_root) {
    if (!item || typeof item !== 'object') continue;
    const key = rcaClassifyArrayItem(item);
    if (key && dist[key]) dist[key].push(item);
  }
  // Preserve any other top-level fields the wrapper may have carried.
  const out = { ...dist };
  for (const k of Object.keys(parsed)) {
    if (k === '_array_root') continue;
    if (k in out) continue;  // already populated from array items
    out[k] = parsed[k];
  }
  return out;
}

// Lightweight re-implementation of Python _classify_array_item for the
// JS normalizers. Used only when unwrapping _array_root payloads.
// P0-4: explicit zone_type wins over all heuristics.
function rcaClassifyArrayItem(item) {
  if (!item || typeof item !== 'object') return null;
  // P0-4: explicit zone_type wins over all heuristics.
  const zt = item.zone_type;
  if (typeof zt === 'string') {
    const ztl = zt.trim().toLowerCase();
    if (['biozone', 'zone', 'assemblage_zone', 'interval_zone', 'lineage_zone',
         'acme_zone', 'oppel_zone', 'range_zone', 'subzone', 'zonule'].includes(ztl)) {
      return 'biozones';
    }
    if (['species_range', 'taxon_range', 'fad_lad'].includes(ztl)) {
      return 'species_ranges';
    }
    if (['section', 'measured_section', 'locality'].includes(ztl)) {
      return 'sections';
    }
  }
  // species: has species OR (range_top AND range_base together) — C-3 parity with Python
  if (item.species || (item.range_top && item.range_base)) {
    return 'species_ranges';
  }
  // biozone: requires name passes zone-label pattern AND age is present (H-3 parity)
  const biozoneText = String(item.name || item.label || '');
  const isZoneLabel = /\b(zone|zonule|assemblage|oppel|interval|lineage|range|acme)\b/i.test(biozoneText);
  if (item.name && item.age && isZoneLabel) {
    return 'biozones';
  }
  // section: has formations list or id+group or thickness_m only
  if (Array.isArray(item.formations) || item.id || item.thickness_m) {
    return 'sections';
  }
  // other_fossils (str or dict)
  if (item.label || item.species || item.taxon) {
    return 'other_fossils';
  }
  return null;
}

function rcaNormalizeResult(parsed) {
  // P0-5 (REVIEW-2026-07-25): unwrap top-level array wrappers.
  parsed = rcaUnwrapArrayRoot(parsed) || parsed;
  const out = {
    sections: [],
    species_ranges: [],
    biozones: [],
    other_fossils: [],
    confidence: 0,
  };
  const asStr = (v) => (v === null || v === undefined ? '' : String(v));
  const SEC_KNOWN = ['name', 'age_range', 'formations', 'formation_thickness_m', 'coordinates'];
  // P1-6 (REVIEW-2026-07-25): align SP_KNOWN with rca_core/extractor.py
  // _KNOWN_SPECIES_KEYS (13 fields). The previous 5-field list silently
  // dropped author, year, author_year, range_top_bed, range_base_bed,
  // endpoint_kind, reworked into row._extras as a dict — which would
  // be coerced to "[object Object]" by the JS merge multi-run fallback.
  const SP_KNOWN = [
    'species', 'section', 'range_top', 'range_base', 'biozone',
    'author', 'year', 'author_year',
    'range_top_bed', 'range_base_bed',
    'endpoint_kind', 'reworked',
  ];
  // PARITY (extractor.py:250): biozones row includes `section` so the
  // aggregation layer can fold section into the row label and prevent two
  // biozones with the same name in different sections from collapsing into
  // a single merged row. Without this, browser runs lose the section axis
  // for biozones and exported tables diverge from the Python export shape.
  const BZ_KNOWN = ['name', 'section', 'age', 'thickness_m', 'zone_type'];
  const ROOT_KNOWN = ['sections', 'species_ranges', 'biozones', 'other_fossils', 'confidence'];

  for (const sec of Array.isArray(parsed.sections) ? parsed.sections : []) {
    if (!sec || typeof sec !== 'object') continue;
    const row = {
      name: asStr(sec.name),
      age_range: asStr(sec.age_range),
      formations: Array.isArray(sec.formations) ? sec.formations.map(asStr) : [],
      formation_thickness_m: asStr(sec.formation_thickness_m),
      coordinates: asStr(sec.coordinates),
    };
    const extras = rcaCarryExtras(sec, SEC_KNOWN);
    if (extras) row._extras = extras;
    out.sections.push(row);
  }
  for (const sp of Array.isArray(parsed.species_ranges) ? parsed.species_ranges : []) {
    if (!sp || typeof sp !== 'object') continue;
    const row = {
      species: asStr(sp.species),
      section: asStr(sp.section),
      range_top: asStr(sp.range_top),
      range_base: asStr(sp.range_base),
      biozone: asStr(sp.biozone),
      // P1-6 (REVIEW-2026-07-25): promote the 8 long-standing prompt
      // fields to first-class row keys so the JS↔Python parity test,
      // merge function, and exported CSV/JSON all see them.
      author: asStr(sp.author),
      year: asStr(sp.year),
      author_year: asStr(sp.author_year),
      range_top_bed: asStr(sp.range_top_bed),
      range_base_bed: asStr(sp.range_base_bed),
      endpoint_kind: asStr(sp.endpoint_kind),
      reworked: sp.reworked === true,
    };
    const extras = rcaCarryExtras(sp, SP_KNOWN);
    if (extras) row._extras = extras;
    // P0-4: defensive — if species name looks like a zone, flag & strip.
    const spName = row.species;
    if (spName && /\b(zone|zonule|assemblage|oppel|interval|lineage|range|acme)\b/i.test(spName)) {
      row.note = ((row.note && row.note !== 'null' ? row.note : '') + ' [zone-mislabel-warning]').trim();
    }
    out.species_ranges.push(row);
  }
  for (const bz of Array.isArray(parsed.biozones) ? parsed.biozones : []) {
    if (!bz || typeof bz !== 'object') continue;
    // P0-4: infer zone_type from name keywords when not explicitly provided.
    const bzName = asStr(bz.name).trim();
    let inferredZt = 'biozone';
    const bzNameLower = bzName.toLowerCase();
    if (bzNameLower.includes('assemblage') || bzNameLower.includes('ass.')) {
      inferredZt = 'assemblage_zone';
    } else if (bzNameLower.includes('acme')) {
      inferredZt = 'acme_zone';
    } else if (bzNameLower.includes('lineage')) {
      inferredZt = 'lineage_zone';
    } else if (/\bzonule\b/.test(bzNameLower)) {
      inferredZt = 'zonule';
    } else if (/\bsubzone\b/.test(bzNameLower)) {
      inferredZt = 'subzone';
    } else if (bzNameLower.includes('oppel')) {
      inferredZt = 'oppel_zone';
    } else if (bzNameLower.includes('interval')) {
      inferredZt = 'interval_zone';
    } else if (bzNameLower.includes('range zone') || bzNameLower.includes('taxon-range')) {
      inferredZt = 'range_zone';
    }
    const row = {
      name: bzName,
      // PARITY: section is a top-level field on the biozone row, mirroring
      // rca_core/extractor.py:371. Demoting it to _extras would hide it
      // from rcaAggNorm and break the section-folding dedup in aggregate.js.
      section: asStr(bz.section),
      age: asStr(bz.age),
      thickness_m: asStr(bz.thickness_m),
      // P0-4: enforce zone_type field.
      zone_type: asStr(bz.zone_type) || inferredZt,
    };
    const extras = rcaCarryExtras(bz, BZ_KNOWN);
    if (extras) row._extras = extras;
    out.biozones.push(row);
  }
  if (Array.isArray(parsed.other_fossils)) {
    out.other_fossils = parsed.other_fossils.map(asStr).filter((x) => x.trim());
  }
  const conf = Number(parsed.confidence);
  out.confidence = Number.isFinite(conf) ? Math.max(0, Math.min(1, conf)) : 0;
  const rootExtras = rcaCarryExtras(parsed || {}, ROOT_KNOWN);
  if (rootExtras) out._extras = rootExtras;
  return out;
}


// Normalize the parsed columnar-section JSON into the strict result shape.
// Mirrors rca_core.extractor.normalize_columnar_result (deep nested
// normalization + _extras carry + confidence fallback to top-level
// `confidence` when `overall_confidence` is absent).
function rcaNormalizeColumnarResult(parsed) {
  // P0-5 (REVIEW-2026-07-25): unwrap top-level array wrappers. For columnar
  // we distribute only items whose structural keys look like section rows
  // (id/group/lithology_blocks/...) into sections; other items are merged
  // into the appropriate legend/cross_beds buckets.
  if (parsed && Array.isArray(parsed._array_root)) {
    const items = parsed._array_root;
    const dist = {
      sections: [], fossil_legend: [], lithology_legend: [], cross_beds: [],
    };
    for (const item of items) {
      if (!item || typeof item !== 'object') continue;
      if (item.id || item.group || item.lithology_blocks || item.age_units || item.samples) {
        dist.sections.push(item);
      } else if (item.fossil || item.marker) {
        dist.fossil_legend.push(item);
      } else if (item.pattern) {
        dist.lithology_legend.push(item);
      } else if (item.from_section || item.to_section) {
        dist.cross_beds.push(item);
      }
    }
    parsed = { ...dist };
    for (const k of Object.keys(parsed || {})) {
      if (k === '_array_root') continue;
      if (k in dist) continue;
      parsed[k] = (arguments[0] || {})[k];
    }
  }
  const asStr = (v) => (v === null || v === undefined ? '' : String(v));
  const asInt = (v) => {
    if (v === null || v === undefined || v === '') return null;
    const n = parseInt(v, 10);
    return Number.isFinite(n) ? n : null;
  };
  const normList = (key) => (Array.isArray(parsed[key]) ? parsed[key] : []);
  const BLOCK_KNOWN = ['pattern', 'range_top_idx', 'range_base_idx'];
  const UNIT_KNOWN = ['label', 'range_top_idx', 'range_base_idx'];
  const SAMPLE_KNOWN = ['bed_idx', 'fossil_marker', 'ref'];
  const LEGEND_KNOWN = ['marker', 'pattern', 'meaning'];
  const CROSS_KNOWN = ['from_section', 'from_bed_idx', 'to_section', 'to_bed_idx'];
  const SECTION_KNOWN = ['id', 'group', 'lithology_blocks', 'age_units', 'samples',
                         'coordinates_text', 'thickness_m', 'confidence_by_section'];

  const normBlocks = (items) => items.filter((x) => x && typeof x === 'object').map((b) => {
    const row = {
      pattern: asStr(b.pattern),
      range_top_idx: asInt(b.range_top_idx),
      range_base_idx: asInt(b.range_base_idx),
    };
    const ex = rcaCarryExtras(b, BLOCK_KNOWN);
    if (ex) row._extras = ex;
    return row;
  });
  const normUnits = (items) => items.filter((x) => x && typeof x === 'object').map((u) => {
    const row = {
      label: asStr(u.label),
      range_top_idx: asInt(u.range_top_idx),
      range_base_idx: asInt(u.range_base_idx),
    };
    const ex = rcaCarryExtras(u, UNIT_KNOWN);
    if (ex) row._extras = ex;
    return row;
  });
  const normSamples = (items) => items.filter((x) => x && typeof x === 'object').map((s) => {
    const row = {
      bed_idx: asInt(s.bed_idx),
      fossil_marker: asStr(s.fossil_marker),
      ref: asStr(s.ref),
    };
    const ex = rcaCarryExtras(s, SAMPLE_KNOWN);
    if (ex) row._extras = ex;
    return row;
  });
  const normLegend = (items) => items.filter((x) => x && typeof x === 'object').map((x) => {
    const row = {
      marker: asStr(x.marker),
      pattern: asStr(x.pattern),
      meaning: asStr(x.meaning),
    };
    const ex = rcaCarryExtras(x, LEGEND_KNOWN);
    if (ex) row._extras = ex;
    return row;
  });
  const normCross = (items) => items.filter((x) => x && typeof x === 'object').map((x) => {
    const row = {
      from_section: asStr(x.from_section),
      from_bed_idx: asInt(x.from_bed_idx),
      to_section: asStr(x.to_section),
      to_bed_idx: asInt(x.to_bed_idx),
    };
    const ex = rcaCarryExtras(x, CROSS_KNOWN);
    if (ex) row._extras = ex;
    return row;
  });

  const sections = [];
  for (const sec of normList('sections')) {
    if (!sec || typeof sec !== 'object') continue;
    const confSec = Number(sec.confidence_by_section);
    const row = {
      id: asStr(sec.id),
      group: asStr(sec.group),
      lithology_blocks: normBlocks(sec.lithology_blocks || []),
      age_units: normUnits(sec.age_units || []),
      samples: normSamples(sec.samples || []),
      coordinates_text: asStr(sec.coordinates_text),
      thickness_m: asStr(sec.thickness_m),
      confidence_by_section: Number.isFinite(confSec) ? Math.max(0, Math.min(1, confSec)) : 0,
    };
    const ex = rcaCarryExtras(sec, SECTION_KNOWN);
    if (ex) row._extras = ex;
    sections.push(row);
  }
  // Some models emit root `confidence` instead of `overall_confidence`;
  // fall back so the value isn't silently zeroed.
  const overall = Number(parsed.overall_confidence != null ? parsed.overall_confidence : parsed.confidence);
  const ROOT_KNOWN = ['sections', 'fossil_legend', 'lithology_legend', 'cross_beds',
                     'overall_confidence', 'confidence'];
  const out = {
    sections,
    fossil_legend: normLegend(normList('fossil_legend')),
    lithology_legend: normLegend(normList('lithology_legend')),
    cross_beds: normCross(normList('cross_beds')),
    confidence: Number.isFinite(overall) ? Math.max(0, Math.min(1, overall)) : 0,
  };
  const rootEx = rcaCarryExtras(parsed || {}, ROOT_KNOWN);
  if (rootEx) out._extras = rootEx;
  return out;
}

// Normalize the parsed abundance-diagram JSON into the strict result shape.
// Mirrors rca_core.extractor.normalize_abundance_result (with _extras carry).
function rcaNormalizeAbundanceResult(parsed) {
  // P0-5 (REVIEW-2026-07-25): unwrap top-level array wrappers.
  if (parsed && Array.isArray(parsed._array_root)) {
    const items = parsed._array_root;
    const dist = { sites: [], abundances: [], zones: [] };
    for (const item of items) {
      if (!item || typeof item !== 'object') continue;
      if (item.site_id || item.site_name || item.location) {
        dist.sites.push(item);
      } else if (item.abundance || item.count || item.percentage) {
        dist.abundances.push(item);
      } else if (item.zone || item.assemblage) {
        dist.zones.push(item);
      }
    }
    parsed = { ...dist };
  }
  // original body follows
  const asStr = (v) => (v === null || v === undefined ? '' : String(v));
  const normList = (key) => (Array.isArray(parsed[key]) ? parsed[key] : []);
  const SITE_KNOWN = ['name', 'location', 'age_range', 'depth_unit'];
  const AB_KNOWN = ['taxon', 'site', 'level', 'depth', 'abundance', 'abundance_unit'];
  const ZONE_KNOWN = ['name', 'age', 'level_range'];
  const ROOT_KNOWN = ['sites', 'abundances', 'zones', 'confidence'];

  const sites = [];
  for (const s of normList('sites')) {
    if (!s || typeof s !== 'object') continue;
    const row = {
      name: asStr(s.name),
      location: asStr(s.location),
      age_range: asStr(s.age_range),
      depth_unit: asStr(s.depth_unit),
    };
    const ex = rcaCarryExtras(s, SITE_KNOWN);
    if (ex) row._extras = ex;
    sites.push(row);
  }
  const abundances = [];
  for (const a of normList('abundances')) {
    if (!a || typeof a !== 'object') continue;
    const row = {
      taxon: asStr(a.taxon),
      site: asStr(a.site),
      level: asStr(a.level),
      depth: asStr(a.depth),
      abundance: asStr(a.abundance),
      abundance_unit: asStr(a.abundance_unit),
    };
    const ex = rcaCarryExtras(a, AB_KNOWN);
    if (ex) row._extras = ex;
    abundances.push(row);
  }
  const zones = [];
  for (const z of normList('zones')) {
    if (!z || typeof z !== 'object') continue;
    const row = {
      name: asStr(z.name),
      age: asStr(z.age),
      level_range: asStr(z.level_range),
    };
    const ex = rcaCarryExtras(z, ZONE_KNOWN);
    if (ex) row._extras = ex;
    zones.push(row);
  }
  const conf = Number(parsed.confidence);
  const out = {
    sites,
    abundances,
    zones,
    confidence: Number.isFinite(conf) ? Math.max(0, Math.min(1, conf)) : 0,
  };
  const rootEx = rcaCarryExtras(parsed || {}, ROOT_KNOWN);
  if (rootEx) out._extras = rootEx;
  return out;
}

// P0-3: phylogenetic tree normalizer — mirrors Python
// rca_core.extractor._normalize_phylogenetic_tree_into().
function rcaUnwrapArrayRootPhylo(parsed) {
  if (!parsed || typeof parsed !== 'object') return parsed;
  if (!Array.isArray(parsed._array_root)) return parsed;
  for (const item of parsed._array_root) {
    if (item && typeof item === 'object' && Array.isArray(item.nodes)) {
      return item;
    }
  }
  return parsed;
}

function rcaNormalizePhylogeneticTreeResult(parsed) {
  parsed = rcaUnwrapArrayRootPhylo(parsed);
  const asStr = (v) => (v === null || v === undefined ? '' : String(v));
  const asFloat = (v) => {
    if (v === null || v === undefined) return null;
    const n = Number(v);
    return Number.isFinite(n) ? n : null;
  };

  const nodesIn = Array.isArray(parsed.nodes) ? parsed.nodes : [];
  // Build id→node lookup and children counts.
  const idToNode = {};
  const childrenCount = {};
  for (const n of nodesIn) {
    if (!n || typeof n !== 'object') continue;
    const nid = String(n.id || '');
    if (nid) {
      idToNode[nid] = n;
      childrenCount[nid] = 0;
    }
  }
  for (const n of nodesIn) {
    if (!n || typeof n !== 'object') continue;
    const parent = n.parent;
    if (parent != null) {
      const pid = String(parent);
      if (pid in childrenCount) childrenCount[pid] = (childrenCount[pid] || 0) + 1;
    }
  }

  const rootIdsRaw = parsed.root_ids || [];
  if (!rootIdsRaw.length) throw new Error('root_ids is empty');
  for (const rid of rootIdsRaw) {
    if (!(String(rid) in idToNode)) throw new Error('root_ids contains unknown node id: ' + rid);
  }

  const nodesOut = [];
  for (const n of nodesIn) {
    if (!n || typeof n !== 'object') continue;
    const nid = String(n.id || '');
    if (!nid) continue;

    const parentVal = n.parent;
    if (rootIdsRaw.indexOf(nid) === -1) {
      // Non-root
      if (parentVal == null) throw new Error('Non-root node ' + nid + ' must have a parent');
      if (!(String(parentVal) in idToNode)) throw new Error('Node ' + nid + ' references parent not in node ids');
    } else {
      // Root
      if (parentVal != null) throw new Error('Root node ' + nid + ' must have parent == null');
    }

    const isLeafInput = Boolean(n.is_leaf);
    const actualIsLeaf = (childrenCount[nid] || 0) === 0;
    const isLeaf = isLeafInput !== actualIsLeaf ? actualIsLeaf : isLeafInput;

    const supportRaw = n.support;
    const support = asFloat(supportRaw);
    if (support !== null && (support < 0 || support > 100)) {
      throw new Error('support must be in [0, 100] or null, got ' + support);
    }

    const row = {
      id: nid,
      parent: parentVal != null ? String(parentVal) : null,
      name: asStr(n.name),
      is_leaf: isLeaf,
      branch_length: asFloat(n.branch_length),
      node_age_ma: asFloat(n.node_age_ma),
      support: support,
    };
    // Carry extras (depth_range_m, sequence_count, support_confidence, etc.)
    const KNOWN = ['id', 'parent', 'name', 'is_leaf', 'branch_length', 'node_age_ma', 'support'];
    const extras = {};
    let hasExtras = false;
    for (const k of Object.keys(n)) {
      if (!KNOWN.includes(k)) {
        extras[k] = n[k];
        hasExtras = true;
      }
    }
    if (hasExtras) row.metadata = extras;
    nodesOut.push(row);
  }

  const metaRaw = parsed.metadata || {};
  const legendRaw = parsed.legend;
  const conf = Number(parsed.confidence);
  return {
    metadata: {
      title: asStr(metaRaw.title || ''),
      extraction_timestamp: asStr(metaRaw.extraction_timestamp || ''),
      tree_type: asStr(metaRaw.tree_type || ''),
      scale: asStr(metaRaw.scale || ''),
      rooted: Boolean(metaRaw.rooted !== false),
      source: asStr(metaRaw.source || metaRaw.image_source || ''),
    },
    root_ids: rootIdsRaw.map(String),
    nodes: nodesOut,
    legend: (legendRaw && typeof legendRaw === 'object') ? legendRaw : {},
    confidence: Number.isFinite(conf) ? Math.max(0, Math.min(1, conf)) : 0,
  };
}

// Build a Newick string from a normalized phylogenetic tree.
// Mirrors Python rca_core.extractor.to_newick().
function rcaBuildNewickNode(nodeId, idToChildren, nodesDict) {
  const children = idToChildren[nodeId] || [];
  if (!children.length) {
    const n = nodesDict[nodeId] || {};
    const name = n.name || '';
    const bl = n.branch_length;
    const blStr = bl != null ? ':' + bl : '';
    const safeName = name.replace(/\(/g, '_').replace(/\)/g, '_').replace(/:/g, '_');
    return safeName + blStr;
  } else {
    const childParts = children.map((cid) => rcaBuildNewickNode(cid, idToChildren, nodesDict));
    const n = nodesDict[nodeId] || {};
    const support = n.support;
    const supportStr = support != null ? String(support) : '';
    const bl = n.branch_length;
    const blStr = bl != null ? ':' + bl : '';
    return '(' + childParts.join(',') + ')' + supportStr + blStr;
  }
}

function rcaToNewick(tree) {
  const nodes = Array.isArray(tree.nodes) ? tree.nodes : [];
  const rootIds = Array.isArray(tree.root_ids) ? tree.root_ids : [];

  const nodesDict = {};
  for (const n of nodes) {
    if (n && typeof n === 'object') {
      const nid = String(n.id || '');
      if (nid) nodesDict[nid] = n;
    }
  }

  const idToChildren = {};
  for (const rid of rootIds) idToChildren[String(rid)] = [];
  for (const n of nodes) {
    if (!n || typeof n !== 'object') continue;
    const pid = n.parent;
    if (pid != null) {
      const pidStr = String(pid);
      if (!(pidStr in idToChildren)) idToChildren[pidStr] = [];
      idToChildren[pidStr].push(String(n.id || ''));
    }
  }

  const parts = rootIds.map((rid) => rcaBuildNewickNode(String(rid), idToChildren, nodesDict));
  return '(' + parts.join(',') + ');';
}

// Backend mode: POST to the same-origin Python server, which performs the
// MiniMax call server-side and returns an already-normalized result.
async function rcaCallBackend(opts, base64) {
  let resp;
  const controller = new AbortController();
  // Allow extra time when the server runs the extraction multiple times.
  const runs = Math.max(1, Math.min(parseInt(opts.runs, 10) || 1, 5));
  const timer = setTimeout(() => controller.abort(), RCA_CONFIG.requestTimeoutMs * runs + 5000);
  // FIX-6: honor a caller-supplied cancel signal (user pressed Cancel).
  const onExtAbort = () => controller.abort();
  if (opts.signal) {
    if (opts.signal.aborted) controller.abort();
    else opts.signal.addEventListener('abort', onExtAbort);
  }

  // Fetch CSRF token before POST. Uses a persistent session token stored
  // in memory so subsequent requests reuse the same session.
  // FIX: pass `signal: controller.signal` so a user cancel or the run-aware
  // timeout fires for the CSRF GET too. Without it the GET could hang for
  // minutes on a stalled connection while the POST timeout already fired,
  // leaving the user staring at a spinner.
  let sessionToken = rcaCallBackend._sessionToken || '';
  try {
    const csrfResp = await fetch('/api/extract', {
      method: 'GET',
      headers: { 'X-Session-Token': sessionToken },
      signal: controller.signal,
    });
    if (csrfResp.ok) {
      const csrfData = await csrfResp.json();
      rcaCallBackend._sessionToken = csrfData.session_token;
      sessionToken = csrfData.session_token;
      rcaCallBackend._csrfToken = csrfData.csrf_token;
    }
  } catch (_e) {
    // Network error on token fetch — proceed without token; server will reject.
  }

  const csrfToken = rcaCallBackend._csrfToken || '';
  try {
    resp = await fetch('/api/extract', {
      method: 'POST',
      headers: {
        'content-type': 'application/json',
        'X-CSRF-Token': csrfToken,
        'X-Session-Token': sessionToken,
      },
      body: JSON.stringify({
        api_key: opts.apiKey,
        image_b64: base64,
        media_type: opts.mediaType || 'image/png',
        caption: opts.caption || '',
        chart_lang: opts.chartLang || 'auto',
        endpoint: opts.baseUrl,
        model: opts.model,
        max_tokens: opts.maxTokens,
        mode: opts.mode || 'range_chart',
        runs: runs,
        // FIX (force-rerun): forward the user-driven cache bypass. Server
        // reads this from req.get('force_rerun') in server.py:574/638. Was
        // silently dropped before, so "Force rerun" was a no-op in browser.
        force_rerun: !!opts.force_rerun,
        // FIX (enhance): forward the image-enhancement flag. Server already
        // accepts `enhance` in the payload; passing it lets the server-side
        // pre-processor boost OCR on thin lines / small text.
        enhance: opts.enhance === true,
      }),
      signal: controller.signal,
    });
  } catch (err) {
    clearTimeout(timer);
    if (opts.signal) opts.signal.removeEventListener('abort', onExtAbort);
    // Distinguish a user cancel from a timeout: both surface as AbortError.
    if (err && err.name === 'AbortError') {
      return { ok: false, errorKey: (opts.signal && opts.signal.aborted) ? 'err.cancelled' : 'err.timeout' };
    }
    return { ok: false, errorKey: 'err.network' };
  }
  clearTimeout(timer);
  if (opts.signal) opts.signal.removeEventListener('abort', onExtAbort);
  let payload;
  try {
    payload = await resp.json();
  } catch (_e) {
    return { ok: false, errorKey: 'err.parse' };
  }
  // The server mirrors the ExtractResult shape with snake_case keys.
  return {
    ok: !!payload.ok,
    data: payload.data,
    errorKey: payload.error_key,
    status: payload.status,
    raw: payload.raw || '',
    truncated: !!payload.truncated,
    // H7: upstream error body so the UI can show 5xx reasons.
    errorBody: payload.error_body || '',
    // M2: how many of the requested runs failed.
    partialFailures: payload.partial_failures || 0,
    // Usage and latency from the server.
    usage: payload.usage || null,
    latencyMs: payload.latency_ms || 0,
    // M40: surface server-side warning (e.g. partial-aggregation notice).
    // Previously dropped silently so the user never saw "2 of 3 runs
    // succeeded" style hints from the server's merge layer.
    warning: payload.warning || '',
  };
}

// Main entry. opts: { apiKey, baseUrl, model, maxTokens, proxyUrl, mode,
// dataUrl, mediaType, caption, chartLang }.
// mode defaults to 'range_chart'; 'columnar_section' switches prompt and
// normalizer to the columnar-section variants.
async function extractRangeChart(opts) {
  const mode = (opts && opts.mode) || 'range_chart';
  const {
    apiKey,
    baseUrl,
    model,
    maxTokens,
    proxyUrl,
    dataUrl,
    mediaType,
    caption,
    chartLang,
  } = opts;

  const { base64 } = rcaSplitDataUrl(dataUrl);
  if (!base64) {
    return { ok: false, errorKey: 'err.imageRead' };
  }

  // Backend mode: when served by the Python server (http/https origin),
  // call the same-origin /api/extract so the outbound MiniMax request is
  // made server-side. This avoids the browser CORS restriction entirely.
  //
  // We dispatch on `opts.transport` (set by app.js), NOT on `opts.mode`,
  // because `mode` is the chart kind (range_chart / columnar_section).
  // Mixing them up was Bug C1 and made the backend path dead code.
  if (opts.transport === 'backend') {
    return rcaCallBackend(opts, base64);
  }

  const target = (proxyUrl && proxyUrl.trim())
    ? proxyUrl.trim().replace(/\/+$/, '')
    : String(baseUrl).replace(/\/+$/, '');
  // F-8: Enforce HTTPS for proxy URLs to prevent API key leakage via HTTP MITM
  if (target.startsWith('http://')) {
    console.error('Insecure proxy URL: HTTP is not allowed, falling back to direct connection');
    return rcaCallBackend(opts, base64);
  }
  const url = target + '/v1/messages';

  const langHint = (CHART_LANG_HINT && CHART_LANG_HINT[chartLang]) || '';
  let modeInstruction;
  if (mode === 'columnar_section') {
    modeInstruction = 'Extract the columnar-section information as the strict JSON contract.';
  } else if (mode === 'abundance_diagram') {
    modeInstruction = 'Extract the abundance-diagram information as the strict JSON contract.';
  } else if (mode === 'phylogenetic_tree') {
    modeInstruction = 'Extract the phylogenetic-tree information as the strict JSON contract.';
  } else {
    modeInstruction = 'Extract the geological information as the strict JSON contract.';
  }
  const userPrompt =
    'Caption:\n' + (caption && caption.trim() ? caption.trim() : '(no caption)') + '\n\n' +
    langHint + modeInstruction;

  let sysPrompt = RANGE_CHART_SYSTEM_PROMPT;
  if (mode === 'columnar_section' && typeof COLUMNAR_SECTION_SYSTEM_PROMPT !== 'undefined') {
    sysPrompt = COLUMNAR_SECTION_SYSTEM_PROMPT;
  } else if (mode === 'abundance_diagram' && typeof ABUNDANCE_DIAGRAM_SYSTEM_PROMPT !== 'undefined') {
    sysPrompt = ABUNDANCE_DIAGRAM_SYSTEM_PROMPT;
  } else if (mode === 'phylogenetic_tree' && typeof PHYLOGENETIC_TREE_SYSTEM_PROMPT !== 'undefined') {
    sysPrompt = PHYLOGENETIC_TREE_SYSTEM_PROMPT;
  }

  const body = {
    model: model,
    max_tokens: maxTokens || 4000,
    system: sysPrompt,
    messages: [
      {
        role: 'user',
        content: [
          {
            type: 'image',
            source: { type: 'base64', media_type: mediaType || 'image/png', data: base64 },
          },
          { type: 'text', text: userPrompt },
        ],
      },
    ],
  };

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), RCA_CONFIG.requestTimeoutMs);
  // FIX-6: honor a caller-supplied cancel signal (user pressed Cancel).
  const onExtAbort = () => controller.abort();
  if (opts.signal) {
    if (opts.signal.aborted) controller.abort();
    else opts.signal.addEventListener('abort', onExtAbort);
  }

  let resp;
  try {
    resp = await fetch(url, {
      method: 'POST',
      headers: {
        'x-api-key': apiKey,
        'anthropic-version': '2023-06-01',
        'content-type': 'application/json',
      },
      body: JSON.stringify(body),
      signal: controller.signal,
    });
  } catch (err) {
    clearTimeout(timer);
    if (opts.signal) opts.signal.removeEventListener('abort', onExtAbort);
    if (err && err.name === 'AbortError') {
      return { ok: false, errorKey: (opts.signal && opts.signal.aborted) ? 'err.cancelled' : 'err.timeout' };
    }
    // TypeError from fetch usually means a network/CORS failure.
    return { ok: false, errorKey: 'err.network' };
  }
  clearTimeout(timer);
  if (opts.signal) opts.signal.removeEventListener('abort', onExtAbort);

  if (!resp.ok) {
    let detail = '';
    try { detail = await resp.text(); } catch (_e) { /* ignore */ }
    let errorKey = 'err.http';
    if (resp.status === 401) errorKey = 'err.401';
    else if (resp.status === 403) errorKey = 'err.403';
    else if (resp.status === 429) errorKey = 'err.429';
    return { ok: false, errorKey, status: resp.status, raw: detail };
  }

  let payload;
  try {
    payload = await resp.json();
  } catch (_e) {
    return { ok: false, errorKey: 'err.parse', raw: '' };
  }

  // Extract the first text content block (Anthropic-compatible shape).
  let rawText = '';
  const content = Array.isArray(payload.content) ? payload.content : [];
  for (const c of content) {
    if (c && c.type === 'text') {
      rawText = c.text || '';
      break;
    }
  }
  // M10: detect truncation across API shapes. Anthropic uses
  // `stop_reason: "max_tokens"`; OpenAI uses `finish_reason: "length"`;
  // Gemini uses `candidates[].finishReason: "MAX_TOKENS" / "LENGTH"`.
  let truncated = false;
  if (payload && typeof payload === 'object') {
    if (payload.stop_reason === 'max_tokens') truncated = true;
    if (Array.isArray(payload.choices) && payload.choices[0] && payload.choices[0].finish_reason === 'length') truncated = true;
    if (Array.isArray(payload.candidates) && payload.candidates[0]) {
      const fr = payload.candidates[0].finishReason;
      if (fr === 'MAX_TOKENS' || fr === 'LENGTH') truncated = true;
    }
  }

  if (!rawText) {
    return { ok: false, errorKey: 'err.empty', raw: JSON.stringify(payload).slice(0, 2000), truncated };
  }

  let parsed;
  try {
    parsed = safeJsonLoads(rawText);
  } catch (_e) {
    return { ok: false, errorKey: 'err.parse', raw: rawText, truncated };
  }

  let data;
  if (mode === 'columnar_section' && typeof rcaNormalizeColumnarResult === 'function') {
    data = rcaNormalizeColumnarResult(parsed);
  } else if (mode === 'abundance_diagram' && typeof rcaNormalizeAbundanceResult === 'function') {
    data = rcaNormalizeAbundanceResult(parsed);
  } else if (mode === 'phylogenetic_tree' && typeof rcaNormalizePhylogeneticTreeResult === 'function') {
    data = rcaNormalizePhylogeneticTreeResult(parsed);
  } else {
    data = rcaNormalizeResult(parsed);
  }
  return { ok: true, data, raw: rawText, truncated };
}
