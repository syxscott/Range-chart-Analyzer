# 优化计划：提升 Range Chart Analyzer 提取准确度

针对实测那张放射虫延限图暴露的问题，进行有针对性的准确度优化。

## 问题根因回顾（来自实测）

| 问题 | 表现 | 根因 |
|------|------|------|
| **A. 菊石带被当成物种** | Hypophiceras、Pseudotirolites、Sinocelites 混入 species_ranges | Prompt 未强调"只提取物种列，忽略菊石带列" |
| **B. 年代阶当成组** | "吴家坪阶"被写成"吴家坪组(Formation)" | Prompt 未区分年代地层(Stage)与岩石地层(Formation) |
| **C. 密排斜体学名 OCR 误读** | Paracopicyntra→Paracipracytra、Trilonche→Triassocampe 等 | **2400px 下采样把小字压糊** + 单次识读随机误差 |
| **D. 漏提物种** | Entactinia sashidai 等 3+ 个未提取 | 分辨率不足 + 单次识读遗漏 |

## 已确认的三项决策

1. **图像**：提高下采样上限（2400→4000px）+ 可调开关
2. **多次一致性**：加入"运行 N 次取并集"功能
3. **术语纠错**：仅强化 Prompt（不建外部名库）

---

## 需求重述

在**不破坏现有三端（GUI/后端/纯前端）架构、不引入新依赖**的前提下：
1. 通过**保留更高图像分辨率**减少小字 OCR 误读（针对 C/D）
2. 通过**强化 System Prompt** 消除术语混淆（针对 A/B）并提升 OCR 自校正意识
3. 通过**多次运行取并集**降低单次随机误读（针对 C/D）
4. UI 增加对应控件与三语文案，保持体验一致

---

## 实施阶段

### Phase 1 — 图像分辨率优化（针对 C/D）
**目标**：小字保真，减少下采样导致的 OCR 误读。

- `rca_core/extractor.py`：`DEFAULT_MAX_EDGE` 2400 → **4000**；`load_image_b64` 增加可选 `max_edge` 已支持，确认参数透传。
- `js/config.js`：`maxImageEdge` 2400 → **4000**。
- 两端新增"图像分辨率上限"设置项（数值输入，默认 4000，范围 1000–8000，0=不压缩）。
- 压缩时若为 JPEG，提高 `quality` 0.92 → **0.95**（GUI Pillow 端同步）。
- `extractor.py` 压缩逻辑：**大图优先转 PNG 无损**（除非原图 JPEG 且超大），避免 JPEG 二次压缩糊化小字。
- 依赖关系：独立，可最先做。

### Phase 2 — 强化 System Prompt（针对 A/B + OCR 意识）
**目标**：消除术语混淆，提升识读纪律。修改 `rca_core/prompt.py` 与 `js/prompt.js`（两份逐字同步）。

新增/强化规则：
1. **物种 vs 生物带分列**："The chart has SEPARATE columns: taxon/species columns (each species = one vertical range line with dots) and a biozone/ammonoid-zone column. ONLY extract vertical species range lines into `species_ranges`. Zone names (e.g. ending in 'Zone', or ammonoid assemblage names) go into `biozones`, NEVER into species_ranges."
2. **年代 vs 岩石地层**："Distinguish chronostratigraphic units (System/Series/**Stage**, e.g. 'Wuchiapingian Stage / 吴家坪阶') from lithostratigraphic units (Group/**Formation**, e.g. 'Dalong Formation / 大隆组'). Put Stages in `age_range`, put Formations/Groups in `formations`. Do NOT label a Stage as a Formation."
3. **中文地层术语对照**：明确"组=Formation、群=Group、阶=Stage、统=Series、系=System"，避免把"阶"译成"Formation"。
4. **OCR 自校正**："Species names are printed as small italic Latin binomials, often densely packed and rotated — read carefully. When a genus name is ambiguous, prefer known radiolarian genera. Do not merge or invent names."
5. **完整性**："Extract EVERY species range line you can see, including short single-bed ranges. Do not skip faint or short lines."
6. 保留四语识读 + 拉丁学名统一输出的既有规则。

- 用现有 `tests_core`/combined 测试确认 prompt 常量四语关键词、JSON-only 规则仍在。
- 依赖：独立。

### Phase 3 — 多次运行取并集（针对 C/D）
**目标**：同图多跑 N 次，合并结果、标注一致性，降低单次随机误读。

- **核心逻辑放 `rca_core`**（GUI 与后端共用），纯前端可后续复用：
  - 新增 `rca_core/aggregate.py`：`merge_results(results: list[dict]) -> dict`
    - 按 `_norm(species)+section` 归一化 key 聚合 species_ranges；
    - 记录每条被命中的次数 `agreement`(如 2/3)；
    - `range_base/range_top/biozone` 取**众数**（多数一致值），分歧时保留众数并可选记录备选；
    - sections/biozones/other_fossils 同法去重合并；
    - confidence 取多次均值。
  - 复用 Phase-existing 的 `_norm` 思路（若无则在 aggregate.py 内实现轻量归一化：去 sp./cf.、小写、首2词）。
- **后端 `server.py`**：`/api/extract` 增加可选 `runs`(默认1) 参数；`runs>1` 时循环调用 `extract_range_chart` 并 `merge_results`，返回结果附 `agreement` 字段。
- **GUI `gui.py`**：设置区加"运行次数(1–5)"；提取时按次数循环（后台线程逐次调用，进度条显示"第 k/N 次"），完成后 `merge_results`。
- **纯前端 `js/minimax.js` + `app.js`**：直连/代理模式循环调用 N 次后在 JS 端合并（移植 aggregate 逻辑为 `js/aggregate.js`）；后端模式把 `runs` 传给 `/api/extract` 由服务端合并。
- **表格展示**：`table.js`/GUI Treeview 的 species 表增加"一致性"列（如 3/3、2/3），低一致性行以警告色标注，提示人工核对。
- 依赖：Phase 3 依赖已有提取链路；`js/aggregate.js` 与 `rca_core/aggregate.py` 逻辑对齐。

### Phase 4 — UI 控件、i18n 与文档
- **HTML/GUI 新增控件**：图像分辨率上限、运行次数。
- **i18n**：`js/i18n.js`（中/英/日）与 `rca_core/i18n.py` 各加约 6 个键（分辨率、运行次数、一致性列、低一致性提示等），保持键完全对齐（现有测试会校验）。
- **README**：新增"提高准确度的建议"小节：上传高清原图、调高分辨率上限、开启多次运行、人工核对低一致性行、必要时在图注框粘贴物种名列表。
- 依赖：最后做。

### Phase 5 — 测试与验证
- 扩展 `tests_core.py`：
  - `merge_results`：多次输入的并集/众数/一致性计数正确；空输入、单次输入退化正确。
  - prompt 常量：新增关键词（"biozone"、"Formation"、"Stage"、"ammonoid"）存在。
  - i18n 三语键对齐（现有校验自动覆盖新键）。
- Node 端 combined 测试：`js/aggregate.js` 与 py 版对同一输入产出一致结构。
- `py_compile` 全量 + 全 JS `node --check`。
- 无 API key 情况下用假 MiniMax（stdlib http）跑 `runs=3` 后端合并回归，确认 `agreement` 字段与合并计数正确。

---

## 依赖关系
- Phase 1、2 独立，可并行先做（见效最快，直接压制 A/B/C/D 主因）。
- Phase 3 依赖提取链路，产出 aggregate 双端实现。
- Phase 4 依赖 1–3 的新参数/字段。
- Phase 5 贯穿收尾。

## 风险与对策
| 风险 | 等级 | 对策 |
|------|------|------|
| 高分辨率致请求体过大/超时 | 中 | 上限设 8000 且默认 4000；保留可调；超大图仍压缩；超时提示已存在 |
| 多次运行成本×N | 中 | 默认 runs=1；UI 明示"每次一次 API 调用"；用户自选 |
| 合并逻辑双端(py/js)不一致 | 中 | 抽同一算法契约，combined 测试对同输入比对结构 |
| Prompt 变更引发其它图回归 | 低 | 规则为"分列/术语区分"，通用无害；保留 JSON-only 与四语规则 |
| i18n 键不齐导致启动/测试失败 | 低 | 现有 parity 测试强校验，新增键三语同步 |

## 复杂度评估：**中等**
- Phase 1：~1h　Phase 2：~1.5h　Phase 3：~3h（双端合并+UI）
- Phase 4：~1h　Phase 5：~1.5h　**合计约 8 小时**

## 明确不做
- 不引入 numpy/opencv/Pillow 以外的新依赖（合并/众数用标准库）。
- 不建外部放射虫名库（本轮仅 Prompt 纠错；名库模糊匹配列为未来增量）。
- 不做自动图像分栏切块（本轮靠分辨率+多次运行，不做 Phase 2 备选的切块方案）。
- 不改动三端共用契约字段名（仅新增 `agreement`、`runs`，向后兼容）。
