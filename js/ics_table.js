// Bundled ICS table (stage → top_ma / base_ma / era) for JS-side quality
// checks. Rebuilt 2026-09-01 (Sprint A of CODE_REVIEW_2026-09-01) to the
// official ICS International Chronostratigraphic Chart v2024/12 values,
// cross-verified against stratigraphy.org, Macrostrat timescale #1 and the
// Paleobiology Database. Mirrors rca_core/resources/ics_2024.json exactly.
// Known newer revisions (ICS chart v2026-06, not yet adopted here):
//   Olenekian base 249.9 -> 250.8, Anisian base 246.7 -> 247.0,
//   Wuchiapingian base 259.51 -> 259.857.
// Used by scoreAccuracy (stage_order_reversed) and scoreConsistency
// (biozone_order_violation). Exposed as globalThis.RCA_ICS_TABLE on load.
// FE-FIX-2026-09-21: 11 rows present in rca_core/resources/ics_current.json
// were missing here (Aeronian, Rhuddanian, Telychian, Homerian, Gorstian,
// Sheinwoodian, Ludfordian, Greenlandian, Meghalayan, Northgrippian, Late
// Pleistocene), so viz.js rcaVizStageBounds returned null for them and they
// were unplaceable in the browser while Python placed them. Added with ages
// copied byte-for-byte from the JSON (authority). tests_frontend.js now pins
// full JSON<->table parity.
'use strict';

globalThis.RCA_ICS_TABLE = {
  "Aalenian": {top_ma: 170.9, base_ma: 174.7, era: "Mesozoic"},
  "Aeronian": {top_ma: 438.6, base_ma: 440.5, era: "Paleozoic"}, // FE-FIX-2026-09-21: was missing vs ics_current.json (viz rcaVizStageBounds -> null)
  "Albian": {top_ma: 100.5, base_ma: 113.2, era: "Mesozoic"},
  "Anisian": {top_ma: 241.464, base_ma: 246.7, era: "Mesozoic"},
  "Aptian": {top_ma: 113.2, base_ma: 121.4, era: "Mesozoic"},
  "Aquitanian": {top_ma: 20.45, base_ma: 23.04, era: "Cenozoic"},
  "Artinskian": {top_ma: 283.3, base_ma: 290.1, era: "Paleozoic"},
  "Asselian": {top_ma: 293.52, base_ma: 298.9, era: "Paleozoic"},
  "Bajocian": {top_ma: 168.2, base_ma: 170.9, era: "Mesozoic"},
  "Barremian": {top_ma: 121.4, base_ma: 125.77, era: "Mesozoic"},
  "Bartonian": {top_ma: 37.71, base_ma: 41.03, era: "Cenozoic"},
  "Bashkirian": {top_ma: 315.2, base_ma: 323.4, era: "Paleozoic"},
  "Bathonian": {top_ma: 165.3, base_ma: 168.2, era: "Mesozoic"},
  "Berriasian": {top_ma: 137.05, base_ma: 143.1, era: "Mesozoic"},
  "Burdigalian": {top_ma: 15.98, base_ma: 20.45, era: "Cenozoic"},
  "Calabrian": {top_ma: 0.774, base_ma: 1.8, era: "Cenozoic"},
  "Callovian": {top_ma: 161.5, base_ma: 165.3, era: "Mesozoic"},
  "Campanian": {top_ma: 72.2, base_ma: 83.6, era: "Mesozoic"},
  "Capitanian": {top_ma: 259.51, base_ma: 264.28, era: "Paleozoic"},
  "Carnian": {top_ma: 227.3, base_ma: 237.0, era: "Mesozoic"},
  "Cenomanian": {top_ma: 93.9, base_ma: 100.5, era: "Mesozoic"},
  "Changhsingian": {top_ma: 251.902, base_ma: 254.14, era: "Paleozoic"},
  "Chattian": {top_ma: 23.04, base_ma: 27.3, era: "Cenozoic"},
  "Chibanian": {top_ma: 0.129, base_ma: 0.774, era: "Cenozoic"},
  "Coniacian": {top_ma: 85.7, base_ma: 89.8, era: "Mesozoic"},
  "Danian": {top_ma: 61.66, base_ma: 66.0, era: "Cenozoic"},
  "Dapingian": {top_ma: 469.4, base_ma: 471.3, era: "Paleozoic"},
  "Darriwilian": {top_ma: 458.2, base_ma: 469.4, era: "Paleozoic"},
  "Drumian": {top_ma: 500.5, base_ma: 504.5, era: "Paleozoic"},
  "Eifelian": {top_ma: 387.95, base_ma: 393.47, era: "Paleozoic"},
  "Emsian": {top_ma: 393.47, base_ma: 410.62, era: "Paleozoic"},
  "Famennian": {top_ma: 358.86, base_ma: 372.15, era: "Paleozoic"},
  "Floian": {top_ma: 471.3, base_ma: 477.1, era: "Paleozoic"},
  "Fortunian": {top_ma: 529.0, base_ma: 538.8, era: "Paleozoic"},
  "Frasnian": {top_ma: 372.15, base_ma: 382.31, era: "Paleozoic"},
  "Gelasian": {top_ma: 1.8, base_ma: 2.58, era: "Cenozoic"},
  "Givetian": {top_ma: 382.31, base_ma: 387.95, era: "Paleozoic"},
  "Gorstian": {top_ma: 425.0, base_ma: 426.7, era: "Paleozoic"}, // FE-FIX-2026-09-21: was missing vs ics_current.json
  "Greenlandian": {top_ma: 0.0082, base_ma: 0.0117, era: "Cenozoic"}, // FE-FIX-2026-09-21: was missing vs ics_current.json
  "Guzhangian": {top_ma: 497.0, base_ma: 500.5, era: "Paleozoic"},
  "Gzhelian": {top_ma: 298.9, base_ma: 303.7, era: "Paleozoic"},
  "Hauterivian": {top_ma: 125.77, base_ma: 132.6, era: "Mesozoic"},
  "Hettangian": {top_ma: 199.5, base_ma: 201.4, era: "Mesozoic"},
  "Hirnantian": {top_ma: 443.1, base_ma: 445.2, era: "Paleozoic"},
  "Holocene": {top_ma: 0.0, base_ma: 0.0117, era: "Cenozoic"},
  "Homerian": {top_ma: 426.7, base_ma: 430.6, era: "Paleozoic"}, // FE-FIX-2026-09-21: was missing vs ics_current.json
  "Induan": {top_ma: 249.9, base_ma: 251.902, era: "Mesozoic"},
  "Jiangshanian": {top_ma: 491.0, base_ma: 494.2, era: "Paleozoic"},
  "Kasimovian": {top_ma: 303.7, base_ma: 307.0, era: "Paleozoic"},
  "Katian": {top_ma: 445.2, base_ma: 452.8, era: "Paleozoic"},
  "Kimmeridgian": {top_ma: 149.2, base_ma: 154.8, era: "Mesozoic"},
  "Kungurian": {top_ma: 274.4, base_ma: 283.3, era: "Paleozoic"},
  "Ladinian": {top_ma: 237.0, base_ma: 241.464, era: "Mesozoic"},
  "Late Pleistocene": {top_ma: 0.0117, base_ma: 0.129, era: "Cenozoic"}, // FE-FIX-2026-09-21: was missing vs ics_current.json
  "Langhian": {top_ma: 13.82, base_ma: 15.98, era: "Cenozoic"},
  "Llandovery": {top_ma: 432.9, base_ma: 443.1, era: "Paleozoic"},
  "Lochkovian": {top_ma: 413.02, base_ma: 419.62, era: "Paleozoic"},
  "Ludfordian": {top_ma: 422.7, base_ma: 425.0, era: "Paleozoic"}, // FE-FIX-2026-09-21: was missing vs ics_current.json
  "Ludlow": {top_ma: 422.7, base_ma: 426.7, era: "Paleozoic"},
  "Lutetian": {top_ma: 41.03, base_ma: 48.07, era: "Cenozoic"},
  "Maastrichtian": {top_ma: 66.0, base_ma: 72.2, era: "Mesozoic"},
  "Meghalayan": {top_ma: 0.0, base_ma: 0.0042, era: "Cenozoic"}, // FE-FIX-2026-09-21: was missing vs ics_current.json
  "Messinian": {top_ma: 5.333, base_ma: 7.246, era: "Cenozoic"},
  "Moscovian": {top_ma: 307.0, base_ma: 315.2, era: "Paleozoic"},
  "Norian": {top_ma: 205.7, base_ma: 227.3, era: "Mesozoic"},
  "Northgrippian": {top_ma: 0.0042, base_ma: 0.0082, era: "Cenozoic"}, // FE-FIX-2026-09-21: was missing vs ics_current.json
  "Olenekian": {top_ma: 246.7, base_ma: 249.9, era: "Mesozoic"},
  "Oxfordian": {top_ma: 154.8, base_ma: 161.5, era: "Mesozoic"},
  "Paibian": {top_ma: 494.2, base_ma: 497.0, era: "Paleozoic"},
  "Piacenzian": {top_ma: 2.58, base_ma: 3.6, era: "Cenozoic"},
  "Pliensbachian": {top_ma: 184.2, base_ma: 192.9, era: "Mesozoic"},
  "Pragian": {top_ma: 410.62, base_ma: 413.02, era: "Paleozoic"},
  "Priabonian": {top_ma: 33.9, base_ma: 37.71, era: "Cenozoic"},
  "Pridoli": {top_ma: 419.62, base_ma: 422.7, era: "Paleozoic"},
  "Rhaetian": {top_ma: 201.4, base_ma: 205.7, era: "Mesozoic"},
  "Rhuddanian": {top_ma: 440.5, base_ma: 443.1, era: "Paleozoic"}, // FE-FIX-2026-09-21: was missing vs ics_current.json
  "Roadian": {top_ma: 266.9, base_ma: 274.4, era: "Paleozoic"},
  "Rupelian": {top_ma: 27.3, base_ma: 33.9, era: "Cenozoic"},
  "Sakmarian": {top_ma: 290.1, base_ma: 293.52, era: "Paleozoic"},
  "Sandbian": {top_ma: 452.8, base_ma: 458.2, era: "Paleozoic"},
  "Santonian": {top_ma: 83.6, base_ma: 85.7, era: "Mesozoic"},
  "Selandian": {top_ma: 59.24, base_ma: 61.66, era: "Cenozoic"},
  "Series 2": {top_ma: 506.5, base_ma: 521.0, era: "Paleozoic"},
  "Serpukhovian": {top_ma: 323.4, base_ma: 330.3, era: "Paleozoic"},
  "Serravallian": {top_ma: 11.63, base_ma: 13.82, era: "Cenozoic"},
  "Sheinwoodian": {top_ma: 430.6, base_ma: 432.9, era: "Paleozoic"}, // FE-FIX-2026-09-21: was missing vs ics_current.json
  "Sinemurian": {top_ma: 192.9, base_ma: 199.5, era: "Mesozoic"},
  "Stage 10": {top_ma: 486.85, base_ma: 491.0, era: "Paleozoic"},
  "Stage 2": {top_ma: 521.0, base_ma: 529.0, era: "Paleozoic"},
  "Stage 3": {top_ma: 514.5, base_ma: 521.0, era: "Paleozoic"},
  "Stage 4": {top_ma: 506.5, base_ma: 514.5, era: "Paleozoic"},
  "Stage 5": {top_ma: 504.5, base_ma: 506.5, era: "Paleozoic"},
  "Stage 9": {top_ma: 491.0, base_ma: 494.2, era: "Paleozoic"},
  "Telychian": {top_ma: 432.9, base_ma: 438.6, era: "Paleozoic"}, // FE-FIX-2026-09-21: was missing vs ics_current.json
  "Thanetian": {top_ma: 56.0, base_ma: 59.24, era: "Cenozoic"},
  "Tithonian": {top_ma: 143.1, base_ma: 149.2, era: "Mesozoic"},
  "Toarcian": {top_ma: 174.7, base_ma: 184.2, era: "Mesozoic"},
  "Tortonian": {top_ma: 7.246, base_ma: 11.63, era: "Cenozoic"},
  "Tournaisian": {top_ma: 346.7, base_ma: 358.86, era: "Paleozoic"},
  "Tremadocian": {top_ma: 477.1, base_ma: 486.85, era: "Paleozoic"},
  "Turonian": {top_ma: 89.8, base_ma: 93.9, era: "Mesozoic"},
  "Valanginian": {top_ma: 132.6, base_ma: 137.05, era: "Mesozoic"},
  "Visean": {top_ma: 330.3, base_ma: 346.7, era: "Paleozoic"},
  "Wenlock": {top_ma: 426.7, base_ma: 432.9, era: "Paleozoic"},
  "Wordian": {top_ma: 264.28, base_ma: 266.9, era: "Paleozoic"},
  "Wuchiapingian": {top_ma: 254.14, base_ma: 259.51, era: "Paleozoic"},
  "Wuliuan": {top_ma: 504.5, base_ma: 506.5, era: "Paleozoic"},
  "Ypresian": {top_ma: 48.07, base_ma: 56.0, era: "Cenozoic"},
  "Zanclean": {top_ma: 3.6, base_ma: 5.333, era: "Cenozoic"}
};

// ---------------------------------------------------------------------------
// REVIEW-2026-07-31: chronostratigraphic label maps mirroring
// rca_core/standards/ics.py so the pure-frontend quality scorer resolves
// the same labels as the Python core (Chinese stage names, series/epoch
// terms, period names). Keep the values in sync with ics.py.
// ---------------------------------------------------------------------------

// Chinese stage-name aliases -> table key.
globalThis.RCA_ICS_CN_STAGES = {
  '吴家坪阶': 'Wuchiapingian', '长兴阶': 'Changhsingian',
  '卡匹敦阶': 'Capitanian', '沃德阶': 'Wordian', '罗德阶': 'Roadian',
  '空谷阶': 'Kungurian', '亚丁斯克阶': 'Artinskian',
  '萨克马尔阶': 'Sakmarian', '阿瑟尔阶': 'Asselian',
  '格舍尔阶': 'Gzhelian', '卡西莫夫阶': 'Kasimovian', '莫斯科阶': 'Moscovian',
  '巴什基尔阶': 'Bashkirian', '谢尔普霍夫阶': 'Serpukhovian',
  '维宪阶': 'Visean', '杜内阶': 'Tournaisian',
  '法门阶': 'Famennian', '弗拉斯阶': 'Frasnian', '吉维特阶': 'Givetian',
  '艾菲尔阶': 'Eifelian', '埃姆斯阶': 'Emsian', '布拉格阶': 'Pragian',
  '洛赫科夫阶': 'Lochkovian',
  '普里多利统': 'Pridoli', '拉德洛统': 'Ludlow', '文洛克统': 'Wenlock',
  '兰多维列统': 'Llandovery',
  '赫南特阶': 'Hirnantian', '凯迪阶': 'Katian', '桑比阶': 'Sandbian',
  '达瑞威尔阶': 'Darriwilian', '大坪阶': 'Dapingian', '弗洛阶': 'Floian',
  '特马豆克阶': 'Tremadocian',
  '排碧阶': 'Paibian', '江山阶': 'Jiangshanian', '古丈阶': 'Guzhangian',
  '鼓山阶': 'Drumian', '乌溜阶': 'Wuliuan',
  '格拉斯阶': 'Gelasian', '卡拉布里阶': 'Calabrian', '基班期': 'Chibanian',
  '皮亚琴察阶': 'Piacenzian', '赞克尔阶': 'Zanclean', '梅辛阶': 'Messinian',
  '托尔托纳阶': 'Tortonian', '塞拉瓦莱阶': 'Serravallian', '兰盖阶': 'Langhian',
  '布尔迪加尔阶': 'Burdigalian', '阿基坦阶': 'Aquitanian',
  '夏特阶': 'Chattian', '鲁培尔阶': 'Rupelian', '普里阿邦阶': 'Priabonian',
  '巴尔托阶': 'Bartonian', '卢泰特阶': 'Lutetian', '伊普雷斯阶': 'Ypresian',
  '塞兰特阶': 'Selandian', '丹尼阶': 'Danian',
  '马斯特里赫特阶': 'Maastrichtian', '坎潘阶': 'Campanian', '圣通阶': 'Santonian',
  '科尼亚克阶': 'Coniacian', '土伦阶': 'Turonian', '塞诺曼阶': 'Cenomanian',
  '阿尔布阶': 'Albian', '阿普特阶': 'Aptian', '巴雷姆阶': 'Barremian',
  '欧特里夫阶': 'Hauterivian', '瓦兰金阶': 'Valanginian', '贝里阿斯阶': 'Berriasian',
  '提通阶': 'Tithonian', '基默里奇阶': 'Kimmeridgian', '牛津阶': 'Oxfordian',
  '卡洛夫阶': 'Callovian', '巴通阶': 'Bathonian', '巴柔阶': 'Bajocian',
  '阿林阶': 'Aalenian', '托阿尔阶': 'Toarcian', '普林斯巴赫阶': 'Pliensbachian',
  '辛涅缪尔阶': 'Sinemurian', '赫塘阶': 'Hettangian',
  '瑞替阶': 'Rhaetian', '诺利阶': 'Norian', '卡尼阶': 'Carnian',
  '拉丁阶': 'Ladinian', '安尼阶': 'Anisian', '奥伦尼克阶': 'Olenekian',
  '印度阶': 'Induan',
};

// Series/epoch labels -> {name, stages (oldest first), bounds (optional
// override when the named stages do not span the whole series)}.
globalThis.RCA_ICS_SERIES = {
  'early permian': {name: 'Cisuralian', stages: ['Asselian', 'Sakmarian', 'Artinskian', 'Kungurian'], bounds: null},
  'middle permian': {name: 'Guadalupian', stages: ['Roadian', 'Wordian', 'Capitanian'], bounds: null},
  'late permian': {name: 'Lopingian', stages: ['Wuchiapingian', 'Changhsingian'], bounds: null},
  'early triassic': {name: 'Lower Triassic', stages: ['Induan', 'Olenekian'], bounds: null},
  'middle triassic': {name: 'Middle Triassic', stages: ['Anisian', 'Ladinian'], bounds: null},
  'late triassic': {name: 'Upper Triassic', stages: ['Carnian', 'Norian', 'Rhaetian'], bounds: null},
  'early jurassic': {name: 'Lower Jurassic', stages: ['Hettangian', 'Sinemurian', 'Pliensbachian', 'Toarcian'], bounds: null},
  'middle jurassic': {name: 'Middle Jurassic', stages: ['Aalenian', 'Bajocian', 'Bathonian', 'Callovian'], bounds: null},
  'late jurassic': {name: 'Upper Jurassic', stages: ['Oxfordian', 'Kimmeridgian', 'Tithonian'], bounds: null},
  'lower cretaceous': {name: 'Lower Cretaceous', stages: ['Berriasian', 'Valanginian', 'Hauterivian', 'Barremian', 'Aptian', 'Albian'], bounds: null},
  'early cretaceous': {name: 'Lower Cretaceous', stages: ['Berriasian', 'Valanginian', 'Hauterivian', 'Barremian', 'Aptian', 'Albian'], bounds: null},
  'upper cretaceous': {name: 'Upper Cretaceous', stages: ['Cenomanian', 'Turonian', 'Coniacian', 'Santonian', 'Campanian', 'Maastrichtian'], bounds: null},
  'late cretaceous': {name: 'Upper Cretaceous', stages: ['Cenomanian', 'Turonian', 'Coniacian', 'Santonian', 'Campanian', 'Maastrichtian'], bounds: null},
  'paleocene': {name: 'Paleocene', stages: ['Danian', 'Selandian', 'Thanetian'], bounds: null},
  'eocene': {name: 'Eocene', stages: ['Ypresian', 'Lutetian', 'Bartonian', 'Priabonian'], bounds: null},
  'oligocene': {name: 'Oligocene', stages: ['Rupelian', 'Chattian'], bounds: null},
  'miocene': {name: 'Miocene', stages: ['Aquitanian', 'Burdigalian', 'Langhian', 'Serravallian', 'Tortonian', 'Messinian'], bounds: null},
  'pliocene': {name: 'Pliocene', stages: ['Zanclean', 'Piacenzian'], bounds: null},
  // Pleistocene's named stages end at 0.129; the epoch runs to 0.0117.
  'pleistocene': {name: 'Pleistocene', stages: ['Gelasian', 'Calabrian', 'Chibanian'], bounds: [2.58, 0.0117]},
  'early ordovician': {name: 'Lower Ordovician', stages: ['Tremadocian', 'Floian'], bounds: null},
  'middle ordovician': {name: 'Middle Ordovician', stages: ['Dapingian', 'Darriwilian'], bounds: null},
  'late ordovician': {name: 'Upper Ordovician', stages: ['Sandbian', 'Katian', 'Hirnantian'], bounds: null},
  'early devonian': {name: 'Lower Devonian', stages: ['Lochkovian', 'Pragian', 'Emsian'], bounds: null},
  'middle devonian': {name: 'Middle Devonian', stages: ['Eifelian', 'Givetian'], bounds: null},
  'late devonian': {name: 'Upper Devonian', stages: ['Frasnian', 'Famennian'], bounds: null},
  'mississippian': {name: 'Mississippian', stages: ['Tournaisian', 'Visean', 'Serpukhovian'], bounds: null},
  'early carboniferous': {name: 'Mississippian', stages: ['Tournaisian', 'Visean', 'Serpukhovian'], bounds: null},
  'pennsylvanian': {name: 'Pennsylvanian', stages: ['Bashkirian', 'Moscovian', 'Kasimovian', 'Gzhelian'], bounds: null},
  'late carboniferous': {name: 'Pennsylvanian', stages: ['Bashkirian', 'Moscovian', 'Kasimovian', 'Gzhelian'], bounds: null},
  'early silurian': {name: 'Llandovery', stages: ['Llandovery'], bounds: null},
  'middle silurian': {name: 'Wenlock', stages: ['Wenlock'], bounds: null},
  'late silurian': {name: 'Ludlow', stages: ['Ludlow'], bounds: null},
  'lower cambrian': {name: 'Lower Cambrian', stages: ['Fortunian', 'Stage 2'], bounds: null},
  'early cambrian': {name: 'Lower Cambrian', stages: ['Fortunian', 'Stage 2'], bounds: null},
  'middle cambrian': {name: 'Miaolingian', stages: ['Wuliuan', 'Drumian', 'Guzhangian'], bounds: null},
  'furongian': {name: 'Furongian', stages: ['Paibian', 'Jiangshanian', 'Stage 10'], bounds: null},
  'late cambrian': {name: 'Furongian', stages: ['Paibian', 'Jiangshanian', 'Stage 10'], bounds: null},
  // Sprint B (REVIEW-2026-09-04): rock-unit series terms ("Upper Permian",
  // "Lower Jurassic") are ubiquitous on published range charts. Without
  // these aliases such labels silently degraded to period-level bounds,
  // losing series precision. Mirrors rca_core/standards/ics.py
  // _SERIES_STAGE_LISTS (Sprint B 2026-09-05 block); each entry maps to the
  // same interval as its early/late sibling (series = time-translated
  // epoch), bounds: null = derived from the stage chain.
  'upper permian': {name: 'Lopingian', stages: ['Wuchiapingian', 'Changhsingian'], bounds: null},
  'lower permian': {name: 'Cisuralian', stages: ['Asselian', 'Sakmarian', 'Artinskian', 'Kungurian'], bounds: null},
  'upper triassic': {name: 'Upper Triassic', stages: ['Carnian', 'Norian', 'Rhaetian'], bounds: null},
  'lower triassic': {name: 'Lower Triassic', stages: ['Induan', 'Olenekian'], bounds: null},
  'upper jurassic': {name: 'Upper Jurassic', stages: ['Oxfordian', 'Kimmeridgian', 'Tithonian'], bounds: null},
  'lower jurassic': {name: 'Lower Jurassic', stages: ['Hettangian', 'Sinemurian', 'Pliensbachian', 'Toarcian'], bounds: null},
  'upper ordovician': {name: 'Upper Ordovician', stages: ['Sandbian', 'Katian', 'Hirnantian'], bounds: null},
  'lower ordovician': {name: 'Lower Ordovician', stages: ['Tremadocian', 'Floian'], bounds: null},
  'upper devonian': {name: 'Upper Devonian', stages: ['Frasnian', 'Famennian'], bounds: null},
  'lower devonian': {name: 'Lower Devonian', stages: ['Lochkovian', 'Pragian', 'Emsian'], bounds: null},
  'upper silurian': {name: 'Ludlow', stages: ['Ludlow'], bounds: null},
  'lower silurian': {name: 'Llandovery', stages: ['Llandovery'], bounds: null},
  'upper carboniferous': {name: 'Pennsylvanian', stages: ['Bashkirian', 'Moscovian', 'Kasimovian', 'Gzhelian'], bounds: null},
  'lower carboniferous': {name: 'Mississippian', stages: ['Tournaisian', 'Visean', 'Serpukhovian'], bounds: null},
  'upper cambrian': {name: 'Furongian', stages: ['Paibian', 'Jiangshanian', 'Stage 10'], bounds: null},
  // REVIEW-2026-09-20: the FORMAL series/epoch names must resolve too — the
  // entries above EMIT them as the canonical interval name ("late permian" ->
  // "Lopingian"), so a value that round-trips through an export, or a chart
  // labelled with the formal name, used to come back unresolved (null,null) on
  // both engines' export path. Mirrors rca_core/standards/ics.py
  // _SERIES_STAGE_LISTS' trailing REVIEW-2026-09-10 block: each alias shares
  // the informal key's stage list. Order matters — Python iterates the dict in
  // insertion order and returns the first \b match, so these go last.
  'lopingian': {name: 'Lopingian', stages: ['Wuchiapingian', 'Changhsingian'], bounds: null},
  'guadalupian': {name: 'Guadalupian', stages: ['Roadian', 'Wordian', 'Capitanian'], bounds: null},
  'cisuralian': {name: 'Cisuralian', stages: ['Asselian', 'Sakmarian', 'Artinskian', 'Kungurian'], bounds: null},
  'miaolingian': {name: 'Miaolingian', stages: ['Wuliuan', 'Drumian', 'Guzhangian'], bounds: null},
  'terreneuvian': {name: 'Terreneuvian', stages: ['Fortunian', 'Stage 2'], bounds: null},
};

globalThis.RCA_ICS_CN_SERIES = {
  '早二叠世': 'early permian', '中二叠世': 'middle permian', '晚二叠世': 'late permian',
  '早三叠世': 'early triassic', '中三叠世': 'middle triassic', '晚三叠世': 'late triassic',
  '早侏罗世': 'early jurassic', '中侏罗世': 'middle jurassic', '晚侏罗世': 'late jurassic',
  '早白垩世': 'early cretaceous', '晚白垩世': 'late cretaceous',
  '古新世': 'paleocene', '始新世': 'eocene', '渐新世': 'oligocene',
  '中新世': 'miocene', '上新世': 'pliocene', '更新世': 'pleistocene',
  '早奥陶世': 'early ordovician', '中奥陶世': 'middle ordovician', '晚奥陶世': 'late ordovician',
  '早泥盆世': 'early devonian', '中泥盆世': 'middle devonian', '晚泥盆世': 'late devonian',
  '早石炭世': 'early carboniferous', '晚石炭世': 'late carboniferous',
  '早寒武世': 'early cambrian', '中寒武世': 'middle cambrian', '晚寒武世': 'late cambrian',
  // Sprint B (REVIEW-2026-09-04): rock-unit 统 forms (下X统/中X统/上X统) as
  // printed on Chinese range charts. Mirrors rca_core/standards/ics.py
  // _CN_SERIES_ALIASES (Sprint B 2026-09-05 block); same time spans as the
  // 世 forms above.
  '下二叠统': 'early permian', '中二叠统': 'middle permian', '上二叠统': 'late permian',
  '下三叠统': 'early triassic', '中三叠统': 'middle triassic', '上三叠统': 'late triassic',
  '下侏罗统': 'early jurassic', '中侏罗统': 'middle jurassic', '上侏罗统': 'late jurassic',
  '下白垩统': 'early cretaceous', '上白垩统': 'late cretaceous',
  '下奥陶统': 'early ordovician', '中奥陶统': 'middle ordovician', '上奥陶统': 'late ordovician',
  '下泥盆统': 'early devonian', '中泥盆统': 'middle devonian', '上泥盆统': 'late devonian',
  '下石炭统': 'early carboniferous', '上石炭统': 'late carboniferous',
  '下寒武统': 'early cambrian', '中寒武统': 'middle cambrian', '上寒武统': 'late cambrian',
};

// Period bounds [older, younger] (Ma) — mirrors the period_base_ma /
// period_top_ma fields of ics_2024.json.
globalThis.RCA_ICS_PERIODS = {
  permian: [298.9, 251.902],
  triassic: [251.902, 201.4],
  jurassic: [201.4, 143.1],
  cretaceous: [143.1, 66.0],
  paleogene: [66.0, 23.04],
  neogene: [23.04, 2.58],
  quaternary: [2.58, 0.0],
  carboniferous: [358.86, 298.9],
  devonian: [419.62, 358.86],
  silurian: [443.1, 419.62],
  ordovician: [486.85, 443.1],
  cambrian: [538.8, 486.85],
};
globalThis.RCA_ICS_CN_PERIODS = {
  '二叠纪': [298.9, 251.902], '三叠纪': [251.902, 201.4], '侏罗纪': [201.4, 143.1],
  '白垩纪': [143.1, 66.0], '古近纪': [66.0, 23.04], '新近纪': [23.04, 2.58],
  '第四纪': [2.58, 0.0], '石炭纪': [358.86, 298.9], '泥盆纪': [419.62, 358.86],
  '志留纪': [443.1, 419.62], '奥陶纪': [486.85, 443.1], '寒武纪': [538.8, 486.85],
};

// Canonical interval NAME a period label resolves to — mirrors
// rca_core/standards/ics.py:_EN_PERIOD_ALIASES (label -> canonical period).
// RCA_ICS_PERIODS above only carries the bounds, so a JS lookup used to return
// the lower-case label ("permian") where the Python side returns the canonical
// period name ("Permian"); the resolved name lands in exports and in the
// _resolveAgeBound parity contract, so both halves of the Python map are
// needed.
globalThis.RCA_ICS_PERIOD_NAMES = {
  permian: 'Permian', triassic: 'Triassic', jurassic: 'Jurassic',
  cretaceous: 'Cretaceous', paleogene: 'Paleogene', neogene: 'Neogene',
  quaternary: 'Quaternary', carboniferous: 'Carboniferous',
  devonian: 'Devonian', silurian: 'Silurian', ordovician: 'Ordovician',
  cambrian: 'Cambrian',
};
// Chinese period alias -> canonical period name (mirrors _CN_PERIOD_ALIASES).
globalThis.RCA_ICS_CN_PERIOD_NAMES = {
  '二叠纪': 'Permian', '三叠纪': 'Triassic', '侏罗纪': 'Jurassic',
  '白垩纪': 'Cretaceous', '古近纪': 'Paleogene', '新近纪': 'Neogene',
  '第四纪': 'Quaternary', '石炭纪': 'Carboniferous', '泥盆纪': 'Devonian',
  '志留纪': 'Silurian', '奥陶纪': 'Ordovician', '寒武纪': 'Cambrian',
};

// M-1 fix: backward-compat — older callers may read from window.* etc.
if (typeof window !== 'undefined') window.RCA_ICS_TABLE = globalThis.RCA_ICS_TABLE;
