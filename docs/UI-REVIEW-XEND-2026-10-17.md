# UI 跨端一致性审查（XEND）

审查日期：2026-10-17
范围：`rca_core/{exporter,aggregate,quality,report,editable,capabilities,modes}.py` 与三端 UI（`js/`、`gui.py`、`gui_fluent*.py`）的边界；三端同一功能的行为差异；内核 API 健壮性；依赖方向。
方法：grep 定位 + 构造真实输入实测 rca_core + Node/Python 双引擎对账。

---

## 1. 总体结论

**架构方向是对的，分层也基本干净**：实测确认 `rca_core` 没有任何一处 import UI 层（`gui`/`server`/`app`/`main`），`js/` 也没有 import 任何 Python，没有架构倒置。两个 GUI 入口（`main.py:165-232`）分发逻辑清晰，`run.bat` → `main.py` → 默认 Fluent（缺 PySide6 时回落 Tkinter），`run_gui.bat` 直连 Tkinter。`rca_core/capabilities.py:47-54` 已经是 `modes.py` 的投影而非手写表，这一点做得对。

**真正的债不在架构，而在「同一业务规则被复制 2~3 份」**。表格列定义（`exporter.py` TABLE_CONFIGS ↔ `js/table.js` 的 8 个 `rca*Configs`）、质量评分（`quality.py` 1730 行 ↔ `js/quality.js` 1848 行）、合并（`aggregate.py` 1786 行 ↔ `js/aggregate.js` 1512 行）是三套独立实现，本次实测在两处真的分叉了（见 X-01、X-02），其余靠注释和测试在维持。

**好消息**：本次对账中，质量评分与 CSV/TSV 序列化两个最容易漂移的地方，**实测字节级一致**（10/10 用例 score+issue code 完全相同；TSV 116 字节完全相同）。这部分维护是到位的。

**坏消息**：发现 2 个 P1、4 个 P2、3 个 P3。其中 X-01（`gui_fluent_pages.py:551` 必崩）和 X-03（Tkinter/Fluent 无逐表 CSV 导出）是用户立刻能撞上的。

---

## 2. 缺陷清单

### X-01 `gui_fluent_pages.py:551` 历史记录导出 CSV→JSON 分支必抛 TypeError【P1】

**位置**
- 内核：`rca_core/exporter.py:2174` `result_to_json(data, source_file, timestamp)`
- 调用方：`gui_fluent_pages.py:551` `f.write(result_to_json(rec.result))`
- 另两个调用方（本文件仅此一处非测试调用方）：`gui.py:2246`、`gui_fluent.py:2940` 都是自己手写 `json.dump`，绕开了内核

**实测**（不是读代码判断）
```
$ python -c "from rca_core import result_to_json; result_to_json({'species_ranges':[{'species':'A'}]})"
TypeError: result_to_json() missing 2 required positional arguments: 'source_file' and 'timestamp'
```
签名确认：`(data, source_file, timestamp)`，三者均无默认值。

**后果**
用户在 History 页点导出、选了 `.csv`（对话框同时提供 `Excel (*.xlsx);;JSON (*.json);;CSV (*.csv)`，`gui_fluent_pages.py:526-531`）→ 内核走 else 分支改存 `.json` → `TypeError` → 被 `gui_fluent_pages.py:565-566` 的 `except Exception` 捕获 → 用户只看到 `InfoBar.error("", "result_to_json() missing 2 required positional arguments: ...")`。**选 CSV 一个字节都写不出来**，且错误信息是英文 Python 内部签名泄漏。用户完全不知道自己选错/选对了什么格式。

这条路径的整段注释（`gui_fluent_pages.py:547-550`）说明作者认为它在正常工作，实际 100% 失败。

**严重度** P1（功能完全不可用，但不在主路径上——需要用户先存历史记录再选 CSV 导出）
**置信度** 实测确认

---

### X-02 三端 JSON 导出 payload schema 不一致【P1】

**位置**
- Web：`js/app.js:2046-2052` → `{extracted_at, source_file, result}`
- Tkinter：`gui.py:2245-2249` → `{source_file, result}`（**无 `extracted_at`**）
- Fluent：`gui_fluent.py:2938-2941` → `{source_file, result}`（**无 `extracted_at`**）
- 内核：`rca_core/exporter.py:2174-2181` → `{extracted_at, source_file, result}`（有）

**后果**
`extracted_at` 这个字段在 JS 侧只有 `js/app.js:2048` 一处写入，Python 侧只有内核一处写入，**没有任何消费方**（grep 全仓库：0 处读取）。但它出现在 Web 导出的 JSON 里、不出现在两个桌面端导出的 JSON 里。

对用户的实际影响：同一张图，Web 导出的 `result.json` 多了个时间戳字段，桌面端导出的没有。如果用户写了脚本 diff 两端产物、或把桌面端 JSON 喂给某个期待 `extracted_at` 的下游流程，行为不一致。而且这个字段恰恰是**唯一能说明这份结果何时提取的溯源信息**——两个桌面 GUI 明明都有 `time.strftime` 能力（`gui_fluent.py:2921` 的 phylo 分支就在用），却没写进标准 payload。

顺带：内核 `result_to_json` 是唯一同时处理三者的地方，但**没有任何 UI 端调用它**（见 X-01）。三端各写一遍 `json.dump`，这就是字段漂移的根因。

**严重度** P1
**置信度** 实测（读三处代码 + grep 确认无消费方）

---

### X-03 Fluent GUI 没有逐表 CSV/TSV 导出；Tkinter 有【P2】

**位置**
- Tkinter：`gui.py:2099-2103` 每张表两个按钮 `_copy_table`（TSV 到剪贴板）/ `_export_csv`；实现在 `gui.py:2181-2236`，**确实调用内核** `build_table_export` + `to_tsv`/`to_csv`
- Fluent：`gui_fluent.py:1179-1186` 只有两个全局按钮 `_export_json` / `_export_xlsx`。逐表操作区（`gui_fluent.py:2225` 起）**没有任何导出按钮**
- Web：`js/app.js:2059-2081` 每表一个 `data-csv` 按钮 + `data-copy` 按钮（TSV 到剪贴板），走 `rcaBuildTableExport` + `rcaToCsv`/`rcaToTsv`

**实测**（grep 全仓库非测试调用方）
```
build_table_export( 的调用方只有：gui.py:2184、gui.py:2209
to_csv( / to_tsv( 的调用方只有：gui.py:2185、gui.py:2220
```
`gui_fluent.py:59-60` import 了 `build_table_export, to_csv, to_tsv`，**但从未调用**——是纯死 import。

**后果**
Fluent 是 `main.py` 的默认启动目标（`run.bat` → `main.py` → `launch_fluent()`），也就是**大多数用户实际在用的那一端没有逐表导出**。一个 range_chart 结果有 4 张表（species_ranges / sections / biozones / other_fossils），Fluent 用户只能导出整个结果的 JSON 或整个工作簿的 XLSX，**拿不到单张表的 CSV**。想给同事只发"生物带表"，必须换到 Tkinter 或浏览器。

**严重度** P2（能力缺失，不是崩溃）
**置信度** 实测 grep 确认调用方唯一性

---

### X-04 Web 与 Tkinter/Fluent 的表格列定义分叉【P2】

**实测**（同一 payload 喂两端，同一 `get_configs_for_result` / `rcaTableConfigs`）

| payload | Python `get_configs_for_result` | JS `rcaTableConfigs` |
|---|---|---|
| `{'species_ranges':[...], 'sections':[...]}` | `sections`, `species_ranges` | `sections`, `species_ranges`, **`biozones`**, **`other_fossils`** |
| `{'sections':[{'fossil_content':[...]}]}` | `sections` | `sections`, **`species_ranges`**, **`biozones`**, **`other_fossils`** |

**位置**
- Python：`rca_core/exporter.py:1010-1013` `_nonempty_or_none(_range_chart_tables(data), data)` — 空表被剔除
- JS：`js/table.js:443-450` 无条件返回 4 个 range-chart config

**这是有意为之，且作者写明了理由**（`js/table.js:427-443`）：浏览器是编辑面，空表壳必须存在，否则用户无法给 `biozones` 添第一行。

**后果**
不是 bug，是产品取舍。但**后果是用户可见的**：同一个 payload，Web 显示 4 个表格 tab，桌面端只显示 2 个。桌面端用户看到 Web 截图里多出的 `other_fossils` 空表会以为功能缺失；Web 用户看到桌面端少 2 张表会以为数据丢了。更实际的是：Web 的逐表 CSV 导出（`js/app.js:2080`）会导出 `biozones.csv` 这种**只有表头、一行数据都没有**的文件，桌面端不会生成这个文件（`get_configs_for_result` 根本不给这个 cfg）。

**严重度** P2
**置信度** 实测双引擎对账

---

### X-05 Tkinter GUI 不展示 `chimera_warnings`【P2】

**位置**
- 内核产出：`rca_core/aggregate.py:1764`（丢弃行进 `chimera_warnings`）
- Fluent 展示：`gui_fluent.py:2015-2024` InfoBar.warning
- Web 展示：`js/app.js:1574-1583` showAlert('warning', ...)
- **Tkinter：无**（`grep -n "chimera_warnings" gui.py` → 0 命中）

**后果**
多轮提取时，某些行在任何单轮里都没出现过，被合并逻辑丢弃并记入 `chimera_warnings`。Fluent/Web 用户看到黄色告警，**Tkinter 用户看不到任何提示**，直接少了几行数据而无感知。对一篇要发表的地层图表，漏掉的类群无声消失是数据完整性问题。

**严重度** P2
**置信度** 读代码判断（grep 三端，逻辑直白）

---

### X-06 两个 GUI 都不展示审计报告 `result.report`【P2】

**位置**
- 内核：`rca_core/report.py:145` `build_extraction_report(...)`，产出 11 个字段（`coverage`/`empty_tables`/`rows`/`warnings`/`truncation`/`provenance`/…）
- **唯一调用方是 `server.py`**：`server.py:2937`、`server.py:3031`、`server.py:3388`
- Web 消费：`js/table.js:1265` `const rep = (data && typeof data.report === 'object') ? data.report : null`
- **Tkinter / Fluent：无**（grep `report` 只命中无关注释）

**后果**
两个桌面 GUI 走 `extract()` 本地管线（`gui.py:1917`、`gui_fluent.py` 的 worker），**从来不生成 `report`**。所以这不是"GUI 不展示"，是"GUI 端根本不存在这份审计数据"。用户拿不到 `coverage`（哪些列实际答了）、`empty_tables`（哪些表空了及原因）、`truncation`（是否被 max_tokens 截断）的结构化证据，只有 `confidence` 百分比一个粗粒度指标（`gui.py:2172-2178`、`gui_fluent.py:2778-2779`）。

**严重度** P2
**置信度** 读代码判断

---

### X-07 Tkinter GUI 完全不能编辑表格；Web 和 Fluent 能【P2】

**位置**
- Fluent 编辑：`gui_fluent.py:2626` `from rca_core import apply_table_edits`，`gui_fluent.py:2640` 调用
- Web 编辑：`js/table.js:2091` `rcaApplyEdits`（镜像 `rca_core/editable.py:383` `apply_edits`）
- **Tkinter：`grep -c "apply_table_edits" gui.py` → 0**

**后果**
Tkinter 表格是只读 `ttk.Treeview`（`gui.py:2105-2107`，`show="headings"`）。识别错了的物种名、填错的层位，在 Tkinter 下**只能改 JSON 文件或重跑提取**。这是三端里唯一不能纠正提取错误的端，而 `run_gui.bat` 和 README 里的 "Tkinter" 入口仍指向它。

**严重度** P2
**置信度** 读代码判断（grep 确认 0 命中）

---

### X-08 Tkinter 部分失败提示是硬编码英文，未走 i18n【P3】

**位置**
- Tkinter：`gui.py:2049` `status += f" - {pf} run(s) failed (only {total_runs - pf}/{total_runs} succeeded)"`
- Web：`js/app.js:1594` `t('results.partialFailure', { pf, total, succeeded })` — 已本地化（zh/en/ja）
- Fluent：`gui_fluent.py:2046-2047` 有对应处理

**后果**
Tkinter 切换中文/日文界面后，这条告警仍是英文，且是 f-string 直接拼接（不走 `_t()`）。属于**功能一致但文案未本地化**，用户看到的是混合语言界面。

**严重度** P3
**置信度** 读代码判断

---

### X-09 Web 的 CSV 缺尾行 CRLF，与 Python 差 2 字节【P3】

**实测**（字节级对账，同一 headers/rows）
```
py  (to_csv)         : 121 bytes, CRLF×6, LF×7, BOM=True
web (rcaToCsv+BOM)   : 119 bytes, CRLF×5, LF×6, BOM=True
差异: py 末尾多一个 \r\n
```

**位置**
- Python：`rca_core/exporter.py:2117-2121`，`csv.writer(lineterminator="\r\n")`，每行都带 CRLF 含最后一行
- JS：`js/export.js:51-58`，`lines.join('\r\n')`，最后一行无终止符

**后果**
CSV 在 RFC 4180 下最后一行不写终止符是**允许的**，Excel / pandas / `csv` 模块都能正确解析。实际影响：两端的 CSV 喂给 `diff` 或 `sha256sum` 会显示不同；用 `wc -l` 数行数会差 1。

**严重度** P3（不影响数据正确性）
**置信度** 实测

**附带通过的项**：TSV **完全字节一致**（`py == web` → True，116 字节）；CSV 的**内容部分**（引号转义、内嵌换行保留、公式注入 `'=` 前缀、`   =HYPERLINK(x)` 前导空格折叠后加前缀、CJK 原样）全部一致；BOM 两端都有（Python 内联，`exporter.py:2121`；Web 在 `js/export.js:96-98` 按 mime 补）。Windows 中文环境的 BOM/编码坑**没踩**。

---

### X-10 Tkinter 的 `_export_prefix` 与 Fluent 不一致【P3】

**位置**
- Tkinter：`gui.py:2190-2197` — 判断 `abundances` / `sections` 且无 `species_ranges` → 前缀 `abundance_diagram_` / `columnar_section_` / `range_chart_`。**只认 3 种**
- Fluent：`gui_fluent.py:2864-2875` — 走 `_current_mode()`，**认 5 种**，多 `phylogenetic_tree_` 和 `zonation_chart_`
- Web：`js/app.js:319-328` `rcaResultFilePrefix` — 走形状判断，认 5 种（**漏 `occurrence_matrix`**）

**后果**
Tkinter 用户导出系统发育树结果，文件名叫 `range_chart_result.json` 而 Fluent 叫 `phylogenetic_tree_result.json`。归档/脚本按前缀分类时两边对不上。

Web 端 `js/app.js:319-328` 的 `rcaResultFilePrefix` 没有 `rcaLooksOccurrenceMatrix` 分支，Web 用户导出出现矩阵结果会得到 `range_chart_` 前缀（虽然 `occurrence_matrix` 在 `js/minimax.js:3700-3706` 的 `RCA_BROWSER_UNSUPPORTED_MODES` 里，direct 传输会被拒，但 backend 传输走 `js/minimax.js:3782` 提前 return，Web 确实能处理它）。

**严重度** P3
**置信度** 读代码判断

---

## 3. 业务逻辑下沉分析

**结论：内核化程度比预期好，但「三份实现」的重复真实存在。**

| 功能 | 内核位置 | 三端位置 | 重复代码量 | 不一致风险 |
|---|---|---|---|---|
| **表格列定义** | `exporter.py:366-1404`（TABLE_CONFIGS 系列，~1040 行） | Tk：`exporter.py:2072`（✓调用）<br>Fluent：`gui_fluent.py:2225,2519,2556`（✓调用）<br>Web：`js/table.js:99-903`（8 个 `rca*Configs`，**~805 行自实现**） | ~805 行 | **已发生**（X-04） |
| **质量评分** | `quality.py:1519` `score_range_chart`（1730 行） | Tk：**不调用**<br>Fluent：**不调用**<br>Web：`js/quality.js:1675`（1848 行自实现） | 1848 行 | **未发生**（实测 10/10 一致） |
| **CSV/TSV 序列化** | `exporter.py:2113/2122` `to_csv`/`to_tsv`（~40 行） | Tk：`gui.py:2185,2220`（✓）<br>Fluent：**import 未用**（X-03）<br>Web：`js/export.js:51,62`（~38 行自实现） | ~38 行 | **已发生**（X-09，尾行 CRLF） |
| **JSON 导出** | `exporter.py:2174` `result_to_json`（含 `_strip_nonfinite`） | Tk：`gui.py:2245` 手写<br>Fluent：`gui_fluent.py:2938` 手写<br>Web：`js/app.js:2046` 手写 | ~8 行 × 3 | **已发生**（X-02） |
| **XLSX 导出** | `exporter.py:2277` `to_xlsx`（~200 行） | Tk：**无此功能**<br>Fluent：`gui_fluent.py:2877`（✓）<br>Web：**无此功能** | 0（仅一处） | 无重复，但**功能覆盖不一致**（见 §4） |
| **多轮合并** | `aggregate.py:1498` `merge_results`（1786 行） | Tk：`gui.py:1977`（✓）<br>Fluent：`gui_fluent.py`（✓）<br>Web：`js/aggregate.js`（1512 行自实现） | 1512 行 | 未实测（超出本次范围） |
| **表格编辑 diff** | `editable.py:260/383` `capture_edits`/`apply_edits`（474 行） | Fluent：`gui_fluent.py:2626`（✓）<br>Web：`js/table.js:2069/2091`（~130 行自实现）<br>Tk：**无编辑功能**（X-07） | ~130 行 | 未实测 |
| **ICS 年龄解析** | `rca_core/standards/ics.py` `ics_parse_age_range` | `quality.py:972`（✓）<br>`js/quality.js:475` `_icsParseAgeRange`（~34 行镜像）<br>`js/table.js:2223` `rcaIcsLookupStage`（独立镜像） | ~60 行 | 未实测 |
| **Bed 编号解析** | `exporter.py:35` `_parse_bed` | `quality.py:254` `_parse_bed_n`<br>`js/quality.js:287` `rcaParseBed`<br>`js/table.js:2189` `_parseBedN` | ~45 行 × 3 | 未实测 |
| **图表形状判定** | `exporter.py:294-365` `_looks_*` | Tk/Fluent 间接<br>Web：`js/table.js:37-192` `rcaLooks*` | ~155 行 | **已发生**（X-04） |
| **审计报告** | `report.py:145`（324 行） | **只有 server.py 调用**（`server.py:2937/3031/3388`）<br>三端 UI 全不消费 | 0 | 见 X-06 |

### 关键结论

1. **`js/` 的重复是设计选择，不是疏忽**——浏览器不能 import Python，所以 `js/table.js`/`quality.js`/`aggregate.js` 必须有镜像。项目用三种手段维持一致性：`tests/gen_frontend_parity_fixtures.py`（差分 fixture）、`tests_export_parity.js`（91/91 通过，我实测跑了）、以及大量 `// REVIEW-YYYY-MM-DD: <原因>` 注释。**质量评分这次实测 10/10 完全一致，说明这套机制是有效的。**

2. **真正该下沉但没下沉的是 Python 侧的重复**，不是 JS 侧：
   - **X-02 JSON 导出**：三端各手写 `json.dump`，内核的 `result_to_json`（带 `_strip_nonfinite` NaN/Inf 保护）**没被任何 UI 调用**。后果是 Web 有 `extracted_at`、桌面端没有；且**桌面端导出的 JSON 不做 NaN/Inf 剥离**——如果 `confidence` 是 `Infinity`（内核 `exporter.py:2156-2172` 的 `_strip_nonfinite` 就是为这个写的），`gui.py:2255` 的 `json.dump` 会写出 `Infinity`，**这是非标准 JSON**，`json.load` 能读但 `JSON.parse` / jq 会报错。
   - **X-05/X-06 告警与报告的展示策略**：`chimera_warnings`、`report` 的消费逻辑散在 `gui_fluent.py:2015` 和 `js/app.js:1574` 两处，Tkinter 漏了。这不是"该下沉到 rca_core"，而是"该抽成共享的展示决策"，但三端各写一遍的代价已经显现。

3. **`gui_fluent.py:59-60` 的死 import**（`to_csv`, `to_tsv`, `build_table_export`）是 X-03 的直接证据：Fluent 作者 import 了内核导出能力，但没接 UI。这不是重复，是**能力缺失**。

---

## 4. 三端一致性对账表

| 功能 | Tkinter (`gui.py`) | Fluent (`gui_fluent*.py`) | Web (`js/`+`index.html`) | 一致? | 差异后果 |
|---|---|---|---|---|---|
| **逐表 CSV 导出** | ✓ `gui.py:2099` / 实现 `2199` | ✗ 无（`gui_fluent.py:2225` 无按钮） | ✓ `js/app.js:2073` | **✗** | X-03：默认端拿不到单表 CSV |
| **逐表 TSV 到剪贴板** | ✓ `gui.py:2093` / 实现 `2181` | ✗ 无 | ✓ `js/app.js:2064` | **✗** | 同上 |
| **XLSX 导出** | ✗ 无 | ✓ `gui_fluent.py:2877` | ✗ 无 | **✗** | Tkinter/Web 用户只能 JSON |
| **全量 JSON 导出** | ✓ `gui.py:2238` | ✓ `gui_fluent.py:2913` | ✓ `js/app.js:2041` | **✓ 行为 / ✗ schema** | X-02：`extracted_at` 只有 Web 有 |
| **JSON NaN/Inf 剥离** | ✗ 手写 `json.dump` | ✗ 手写 | ✗ `JSON.stringify`（天然正确） | **✗** | 桌面端可能产出非标准 JSON |
| **表格编辑** | ✗ 只读 Treeview | ✓ `gui_fluent.py:2626` | ✓ `js/table.js:2091` | **✗** | X-07：Tkinter 无法纠错 |
| **多轮 agreement 列** | ✓ 内核 `exporter.py:444` | ✓ 同 | ✓ 同（实测 configs 一致） | **✓** | 通过（实测 `multi` 用例两端 cols 完全相同，含 `col.agreement`） |
| **质量评分/等级展示** | ✗ 不算不显示 | ✗ 不算不显示 | ✓ `js/quality.js:1675` + `js/table.js:1235-1257` badge | **✗** | 只有 Web 看到 88% A 这样的评分 |
| **`chimera_warnings` 告警** | ✗ | ✓ `gui_fluent.py:2015` | ✓ `js/app.js:1574` | **✗** | X-05：Tkinter 静默丢行 |
| **部分失败提示** | ✓ `gui.py:2043` 但**英文硬编码** | ✓ `gui_fluent.py:2046` | ✓ `js/app.js:1587` 本地化 | **✓ 行为 / ✗ 语言** | X-08 |
| **审计报告 `report`** | ✗ 不生成 | ✗ 不生成 | ✓（server 模式）`js/table.js:1265` | **✗** | X-06：桌面端无 coverage 证据 |
| **空表 shell（可加首行）** | ✗ 剔除空表 | ✗ 剔除空表 | ✓ 保留 4 表壳 | **✗** | X-04：同一 payload tab 数不同；Web 会导出空 CSV 文件 |
| **图种下拉选项** | 6 项 `gui.py:1031-1045` | 6 项 `gui_fluent.py:1092-1100` | **7 项** `index.html:236-244`（多 `occurrence_matrix`） | **✗** | 见下 |
| **导出文件前缀** | 3 种 `gui.py:2190` | 5 种 `gui_fluent.py:2864` | 5 种，**漏 occurrence_matrix** `js/app.js:319` | **✗** | X-10 |
| **发现/校验史** | ✗ | ✓ `rcaCore.history` | ✓ `rcaVerifySpeciesNamesAsync` `js/app.js:1602` | **✗** | 桌面端无物种名校验 |
| **表被丢弃提示** | ✗ | ✓ `gui_fluent.py:2300-2312`（`results.tablesNotShown`） | ✗ | **✗** | Fluent 独有 |
| **CSV 编码/BOM** | ✓ UTF-8-SIG（`gui.py:2224`，且正确剥掉内核内联 BOM） | N/A | ✓ `js/export.js:96-98` | **✓** | 通过 |
| **CSV 行终止符** | ✓ CRLF+尾行 | N/A | CRLF 无尾行 | **✗** | X-09：差 2 字节 |

### 图种差异的实证

实测 `rca_core/modes.py` 的能力矩阵：
```
mode                  maturity   export  merge   server  browser
range_chart           stable     True    True    True    True
columnar_section      stable     True    True    True    True
abundance_diagram     candidate  True    True    True    True
phylogenetic_tree     candidate  True    True    True    True
zonation_chart        candidate  True    True    True    True
occurrence_matrix     assistant  True    True    True    False  ← Web 下拉有，后端能跑
generic_table         assistant  True    False   False   False
chemical_stratigraphy assistant  False   False   False   False
paleomap              assistant  True    False   False   False
scatter_plot          assistant  False   False   False   False
```

Web 下拉里的 `occurrence_matrix`（`index.html:243`，i18n 三语齐全：`js/i18n.js:124/518/882`）是**唯一三端都能提的**：Tk/Fluent 的下拉没有它（`gui.py:1031-1045`、`gui_fluent.py:1092-1100` 都只列 6 项含 auto），所以桌面端**无论 auto 检测还是手动选择都无法产出 occurrence_matrix 结果**——而 `merge_wired=True`、后端 `server.py:2643` 白名单也放行。即：内核和 server 都支持合并与导出，两个桌面 GUI 都够不着这个模式。

Web 端的处理是**正确的**：`js/minimax.js:3770-3782` 在 `transport === 'backend'` 时提前 return 绕开 `RCA_BROWSER_UNSUPPORTED_MODES` 守卫，direct 传输才拒绝（并给出"切到后端"的可执行提示，不是裸错误码）。

---

## 5. 内核 API 健壮性实测结果

全部为**真实构造输入运行**，非读代码。

### `rca_core/exporter.py::to_xlsx`

| 输入 | 实际输出 | 判定 |
|---|---|---|
| `{}`（空结果） | `ValueError: to_xlsx: this result has no tabular structure (payload: dict); use the JSON export instead` | ✓ 清晰错误 |
| `{'species_ranges': []}` | `ValueError: ... (mode: unknown); use the JSON export instead` | ✓ |
| `species` 为 `''` | `ValueError: export invariants violated: [{'table':'species_ranges','row_index':0,'missing':'species'}]` | ✓ |
| `species` 含 `\x00` / `\x07` | **成功导出 5226 bytes**；`xl/worksheets/sheet1.xml` 实测 **不含 NUL、不含 BEL** | ✓ `_XLSX_ILLEGAL_CHARS_RE`（`exporter.py:2274`）正确剥离 |
| `species` 长 40000 字符 | 导出成功；**openpyxl 回读得 32767 字符** | ✓ openpyxl 自身静默截断到 Excel 上限（32767），**内核无警告但数据不丢**，Excel 单元格上限 |
| `species` 长 32767 / 32768 | 回读分别得 32767 / 32767 | ✓ 同上 |
| `top=None, bottom=None` | 导出成功 5225 bytes | ✓ |
| `top=float('nan')` | 导出成功 5225 bytes | ✓ `_sanitize_number_cell`（`exporter.py:2086`） |
| `file_or_path=None` | 返回 `bytes`（5231 bytes） | ✓ |
| `file_or_path=<path>` | 返回 `None`，文件存在 | ✓ |

**结论：`to_xlsx` 的边界处理很扎实**。空结果、非法字符、NaN/None 全部有明确行为，错误信息带可操作建议。唯一可提的是超长单元格被 openpyxl 静默截断到 32767 而内核不告知用户——但物种名超过 32767 字符在现实中不可能，降级为不报告。

### `rca_core/quality.py::score_range_chart`

9 组畸形输入，**全部不抛异常**，返回合法 score + issues：

| 输入 | score | issues 数 |
|---|---|---|
| `{}` | 0.0 | 1 |
| `None` | 0.0 | 1 |
| `[]`（list 而非 dict） | 0.0 | 1 |
| `{'sections':[{'name':None}]}` | 0.875 | 1 |
| `{'species_ranges':['Acacia','Zea']}`（list of str） | 0.89 | 4 |
| `range_base={'x':1}, range_top=[1,2]`（dict/list 当值） | 0.88 | 5 |
| `_warnings='not-a-list'`（类型错） | 0.88 | 5 |
| `confidence=float('nan')` | 0.94 | 4 |
| `species` 长 5000 字符 | 0.88 | 5 |

**结论：`score_range_chart` 健壮性优秀**。`quality.py:923` 的注释说明它捕获了 `.strip()` 的 `AttributeError`——实测确认对 `None`/`list`/字符串行都安全。

### `rca_core/aggregate.py::merge_results`

| 输入 | 实际返回 | 三端能否处理 |
|---|---|---|
| 单轮 `[r1]` | `{'confidence','runs':1,'species_ranges':[...含 agreement:'1/1']}` | ✓ |
| **全失败轮 `[]`** | `{'species_ranges':[],'runs':1,'sections':[],'biozones':[],'other_fossils':[],'confidence':0.0}` | ⚠ 见下 |
| `[{},{},{}]`（3 轮全空 dict） | `{'runs':3, ...全空列表..., 'confidence':0.0}` | ✓ |
| `[None, r1]`（含 None 元素） | 正常合并，None 被跳过，`runs:1` | ✓ 内核容错 |

**`merge_results([])` 返回 `runs: 1` 而非真实轮数**——这是个语义瑕疵，但它**在三端都不可达**：
- `gui.py:1973-1975`：全失败时 `if not ok_datas: put(last_fail); return`，**根本不走 merge_results**
- Fluent 走同一模式（`gui_fluent.py` worker 的 `ok_datas` 检查）
- Web 的 `js/aggregate.js` 只在拿到结果后合并

所以**实际风险为零**，仅记录为内核 API 的语义粗糙（若未来有调用方直接传空列表，会得到"1 轮"而非"0 轮"）。

### `rca_core/report.py::build_extraction_report`

**签名是 keyword-only**：`(*, data, mode, mode_used=None, ..., lang='')`。6 组畸形输入（`{}`/`None`/`[]`/`{'_warnings':'x'}`/最小 payload/`'oops'` 字符串）**全部返回完整 11 字段结构**，无一抛异常。

**但发现一个 API 易用性陷阱**：`build_extraction_report(data)` 传位置参数会 `TypeError: takes 0 positional arguments but 1 was given`（实测）。这跟 X-01 的 `result_to_json` 是同类问题——**`rca_core` 里这两个导出相关的公开函数，一个必填 3 位置参数、一个强制 keyword-only，调用方都必须小心**。X-01 正是这个陷阱的实例。

### 跨引擎 parity 实测（Node vs Python，同一 JSON payload）

10 组用例，`score + issue code 列表` **逐字节相同**：

| 用例 | Python | JS | 一致 |
|---|---|---|---|
| `minimal` | 0.88 | 0.88 | ✓ |
| `inverted`（base 5 > top 2） | 0.66, 7 issues | 0.66, 7 issues | ✓ |
| `no_sections` | 0.82 | 0.82 | ✓ |
| `bed_sub`（`9a` vs `9`） | 0.66 | 0.66 | ✓ |
| `bed_word`（`Bed 3` vs `Bed 5`） | 0.88 | 0.88 | ✓ |
| `not_drawn` | 0.88 | 0.88 | ✓ |
| `neg_ages`（负数 Ma） | 0.88 | 0.88 | ✓ |
| `mode_col` | 0.875 | 0.875 | ✓ |
| `high_conf_lowq`（conf 0.99） | 0.94 | 0.94 | ✓ |
| `low_conf`（conf 0.1） | 0.94 | 0.94 | ✓ |

`gradeFor(0.9)='A'`、`gradeFor(0.4)='D'`，与 Python `_grade_for`（`quality.py:91`）一致。

---

## 6. 技术债清单（行为一致但重复的部分，按维护成本排序）

1. **`js/quality.js`（1848 行）↔ `rca_core/quality.py`（1730 行）** — 质量评分全量镜像。**当前实测完全一致**，且有 fixture + parity 测试保护。维护成本：任一侧新增一条 issue 规则（现在有 ~40 条 msg_key），必须同步改两个文件 + 更新 fixture。改漏了不会崩，只会静默给出不同分数。**风险已被测试网兜住，暂不需动。**

2. **`js/aggregate.js`（1512 行）↔ `rca_core/aggregate.py`（1786 行）** — 多轮合并全量镜像（ICZN 作者名规范化 `_norm_iczn_author`、qualifier 提取、schema 自动探测）。**本次未实测 parity**（超出范围）。维护成本同上但更大，因为合并逻辑分支更多。**建议补一次差分 fixture 实测。**

3. **`js/table.js` 的 8 个 `rca*Configs`（~805 行）↔ `exporter.py` TABLE_CONFIGS（~1040 行）** — **已实际分叉**（X-04）。额外成本：JS 侧要手写 `cols` + `data_keys` + `edit` 元数据（`js/table.js:449-490` `rcaEC`/`rcaColEditSpec`），三份元数据要对齐。`tests_frontend.js` 有断言覆盖 edit 数组与 cols 索引对齐。

4. **`js/export.js` 的 WPD 标量镜像（~115 行，`js/export.js:143-259`）↔ `exporter.py:2567-2795`** — `_wpd_num`/`_wpd_num_text`/`_wpd_slug` 三函数镜像。`js/export.js:146-156` 注明：完整的 `rcaToWpd` bundle 构建器**故意未镜像**（因为没有浏览器调用点）。`tests_export_parity.js` 91/91 通过（我实测跑了）。**当前无实际风险**——但注释说"等 WPD 导出入口落地时"要补，届时维护成本会跳升。

5. **JSON 导出 payload 三处手写** — 8 行 × 3 处，已分叉（X-02）。**修复成本极低**（把三处换成调用 `rca_core.result_to_json`），**收益立即消除一个已发生的不一致 + 恢复 NaN 保护**。这是本报告里投入产出比最高的一条。

6. **Bed/ICS 解析在 JS 侧有两份独立副本**：`js/quality.js:287` `rcaParseBed` + `js/quality.js:475` `_icsParseAgeRange`，`js/table.js:2189` `_parseBedN` + `js/table.js:2223` `rcaIcsLookupStage`。`js/table.js:2186-2188` 注明是**故意重复而非引用**（"table.js has no load-order dependency on quality.js"）。**本次未实测这两组的一致性。**

7. **`_export_prefix` 三份实现**（`gui.py:2190` / `gui_fluent.py:2864` / `js/app.js:319`），覆盖范围 3/5/5 种。已分叉（X-10）。8 行 × 3 的体量，但它是**用户可见的文件名**。

---

## 7. 验证通过清单

- ✅ **依赖方向无倒置**：grep 确认 `rca_core/*.py` 无任何 `import gui|app|server|main|gui_fluent`；`js/*.js` 无 `require(`/`.py` import。
- ✅ **质量评分跨引擎 parity**：10/10 用例 score + issue code 完全一致（Node 实跑 vs Python 实跑）。
- ✅ **TSV 跨引擎字节一致**：116 bytes 完全相同（含 CJK、引号、内嵌换行折叠、公式注入前缀、前导空格折叠）。
- ✅ **CSV 内容跨引擎一致**：引号转义、内嵌换行保留、公式注入 `'=`、`   =HYPERLINK(x)` 折叠后加前缀、CJK 原样，全部一致。
- ✅ **CSV BOM 两端都有**：Python `exporter.py:2121` 内联；Web `js/export.js:96-98` 按 mime 补。**Windows 中文环境 Excel 乱码的坑没踩。**
- ✅ **Tkinter 正确处理双 BOM**：`gui.py:2220-2222` 剥掉内核内联的 `\ufeff` 再用 `utf-8-sig` 写，避免 Excel 首格显示 `""`。
- ✅ **多轮 agreement 列三端一致**：实测 `runs=3` payload，`get_configs_for_result` 与 `rcaTableConfigs` 的 `species_ranges` cols 完全相同，都含 `col.agreement`。
- ✅ **`to_xlsx` 边界健壮**：空结果 / 非法控制字符（实测 XML 中无 NUL/BEL）/ NaN / None / 超长单元格（32767 截断是 Excel 硬限制）/ 返回值形状，全部有合理行为。
- ✅ **`score_range_chart` 边界健壮**：9 组畸形输入（None/list/字符串行/dict 值/类型错的 `_warnings`/NaN confidence）全部返回合法结果，零异常。
- ✅ **`build_extraction_report` 边界健壮**：6 组畸形输入全部返回完整 11 字段结构，零异常。
- ✅ **`merge_results` 对 None 元素容错**：`[None, r1]` 正常合并。
- ✅ **Web 的 browser-unsupported 守卫设计正确**：`js/minimax.js:3770-3782` 在 backend 传输时绕开守卫，direct 传输拒绝并给可执行提示（不是裸错误码）。这正是 `occurrence_matrix` 能在 Web 下拉出现的前提。
- ✅ **`rca_core/capabilities.py` 是 `modes.py` 的投影**（`capabilities.py:47-54`），不是手写表——mode 新增不会漏注册。
- ✅ **`main.py` 分发清晰**：`main.py:215-232` 五路分发，默认 `launch_fluent()`；`launch_fluent`（`main.py:95-127`）把 `ImportError` 和意外异常分开报告，不会全部伪装成"PySide6 没装"。
- ✅ **现有测试通过**：`tests/test_exporter_xlsx.py` 17 passed；`node tests_export_parity.js` 91/91 checks passed。
- ✅ **`to_wpd` 在无浏览器调用点时未镜像**：无死代码、无半成品。

---

## 8. 无法验证的点

| 项 | 原因 |
|---|---|
| **`js/aggregate.js` ↔ `rca_core/aggregate.py` parity** | 需构造多轮结果 fixture 并双引擎对账，超出本次时间预算。这是最大的未验证重复（1512 行 vs 1786 行），建议列为下一轮重点。 |
| **`js/table.js` 编辑模型 ↔ `rca_core/editable.py` parity** | 同上（`rcaCaptureEdits`/`rcaApplyEdits` ~130 行）。 |
| **`js/table.js` Bed/ICS 双副本一致性**（`js/table.js:2189/2223` vs `js/quality.js:287/475`） | 未构造针对用例对账。 |
| **`server.py` ↔ 三端 UI 的结果传递** | server 是第四个消费面（Web backend 模式），本次只审三端 UI + rca_core。 |
| **GUI 运行时行为**（真实点击流、QWebEngine 加载、`rcaRefuseStaleExport` 的 stale 判定） | 需要起 GUI/浏览器环境，纯静态 + 内核级实测。 |
| **X-01 的端到端复现** | `gui_fluent_pages.py:551` 的 TypeError 是**函数签名层面实测确认**（直接调 `result_to_json` 抛错），但未在真实 Qt 应用里点一遍 History → Export → 选 `.csv`。该路径还需 `rec.result` 非空才能到达，实际触发条件是"先存历史记录"。 |
| **超长单元格截断的用户可见性** | 32767 截断由 openpyxl 静默完成，实测确认行为，但无法评估对真实用户是否构成问题（推测不构成）。 |
