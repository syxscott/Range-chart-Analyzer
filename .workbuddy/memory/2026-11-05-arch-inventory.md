# Range-chart-Analyzer 架构盘点纪要（主理人实测，2026-11-05）

> 本文是主理人**亲自跑命令实测**得到的事实。所有数字可用文末命令复现。
> 专家团成员的产出在此基础上做专业判断，不替代这些原始事实。

## 1. 规模

| 维度 | 数值 |
|---|---|
| Python 总行数 | 89,890 |
| JS 总行数 | 24,005 |
| `rca_core` 模块数 | 46 |
| 根目录被跟踪文件 | 22 |
| 提交总数 | 252（2026-07-14 起） |
| pytest 用例 | 4,360 / 0 失败 / 137 跳过 |
| Node 套件 | 15 个 / 2,576 断言 |
| 差分 parity | 1,363 matched / 0 diverged |
| CI job | 4（test 3.10 / 3.12 / qt / browser perf） |

## 2. 最大的模块

| 模块 | 行数 |
|---|---|
| `rca_core/extractor.py` | 7,055 |
| `gui_fluent.py` | 5,249 |
| `server.py` | 4,031 |
| `rca_core/llm.py` | 3,663 |
| `rca_core/exporter.py` | 3,640 |
| `gui.py` | 2,973 |
| `js/table.js` | 4,654 |
| `js/minimax.js` | 4,406 |

## 3. 依赖方向（实测）

**入口 → 核心，单向，无反向**：
- `server.py` → 12 个 rca_core 模块
- `gui_fluent.py` → 10 个
- `gui.py` → 9 个
- `gui_fluent_providers.py` → 4 个

**`rca_core` 内部只有 6 条边**：
`aggregate→modes/reason_codes`、`capabilities→modes`、`chart_mode→modes`、
`editable→exporter`、`eval_metrics→bed_parser/reason_codes`、`exporter→bed_parser`

**边界干净**：`rca_core` 内**无** tkinter、**无** http.server/flask/fastapi。

**`js/` 模块耦合热点**（引用的 `rca*` 符号数）：
`table.js` 161、`minimax.js` 127、`viz.js` 72、`app.js` 62、`aggregate.js` 27、
`history.js` 26、`reason-codes.js` 20

**`js/table.js` 单文件承载 5 种图表模式的表格渲染**：
mode 分支注释位于 505(abundance) / 546(columnar) / 668(phylogenetic) / 701(zonation) / 766(range_chart)

## 4. 架构上做得好的地方（正面证据）

### 4.1 `rca_core/modes.py` 是单一声明源

10 种模式 × 4 个能力维度（`server_supported` / `browser_supported` /
`export_supported` / `merge_wired`）+ `maturity` 三档（stable/candidate/assistant）。
文件 docstring 明确记载：**此前模式列表写在 9 个地方**，且"大多数遗漏不会报错"
（`SCHEMA_BY_MODE.get(mode, RANGE_CHART_SCHEMA)` 会静默套用错 schema，
产出"看似合理但确实错误"的结果）。

实测能力矩阵：

```
mode                      maturity    srv   brw   exp   mrg
range_chart               stable      True  True  True  True
columnar_section          stable      True  True  True  True
abundance_diagram         candidate   True  True  True  True
phylogenetic_tree         candidate   True  True  True  True
zonation_chart            candidate   True  True  True  True
chemical_stratigraphy     assistant   False False False False
paleomap                  assistant   False False True  False
scatter_plot              assistant   False False False False
occurrence_matrix         assistant   True  False True  True
generic_table             assistant   False False True  False
```

**三端模式覆盖"不一致"是设计意图，不是缺陷**——差异由能力矩阵显式声明。

### 4.2 Python 三端无大面积 UI 复制

四入口同名函数统计：`main`×3（各自 main），`save_config`/`load_config`/`_enhance_image_b`/
`_collect_extraction_futures`×2。**无成规模的同名同职责函数复制**。
导出逻辑确实下沉在核心（`exporter.to_xlsx` 被调用 2 次、`to_csv` 1 次；
`rca_core/exporter.py` 提供 `build_table_export` / `apply_table_edits` / `to_csv` /
`to_tsv` / `to_xlsx` / `to_newick_file` / `to_wpd` / `result_to_json`）。

> ⚠️ 我一度用宽 grep 得出"gui.py 有 25 处导出逻辑、gui_fluent.py 有 40 处"，
> 那是**误判**——那些匹配到的是 `_export_csv` 一类 UI 方法名。对核心的实际调用极少。

### 4.3 溯源链路存在

- `rca_core/report.py:323` `"provenance": request_meta or {}`
- `rca_core/exporter.py:2415-2419` provenance sheet（`_provenance_rows`）
- `rca_core/exporter.py:1126` 注释说明"provenance of the cells belongs"在表格层
- 人工修订标记进入导出的机制在 `exporter.py:1899-1914`（`edited` dict + `_row_identity`）

### 4.4 缓存键含 prompt 版本

`rca_core/cache.py:3,10` 明确 `PROMPT_VERSION` 参与缓存键，防止改 prompt 后命中旧缓存。
`rca_core/prompt.py:35-41` 有 per-mode 版本表，并要求与 `js/prompt.js` 同步。

## 5. 架构负债（实测）

### 5.1 🔴 JS 端 15,298 行是 Python 的对等重实现（占 JS 全量 64%）

| Python | 行数 | JS | 行数 |
|---|---|---|---|
| `rca_core/prompt.py` | 1,592 | `js/prompt.js` | 1,081 |
| `rca_core/json_utils.py` | 886 | `js/json-utils.js` | 742 |
| `rca_core/aggregate.py` | 1,786 | `js/aggregate.js` | 1,532 |
| `rca_core/quality.py` | 1,765 | `js/quality.js` | 1,947 |
| `rca_core/reason_codes.py` | 696 | `js/reason-codes.js` | 852 |
| `rca_core/bed_parser.py` | 467 | `js/bed_parser.js` | 229 |
| `rca_core/history.py` | 1,069 | `js/history.js` | 654 |

**这是本项目最大的架构约束**：改 Python 必须同步改 JS，否则 parity 分裂。

### 5.2 工具函数复制而非复用

`_norm` 在 **4 个模块各写一份**：`aggregate.py` / `eval_metrics.py` / `extractor.py` / `matrix.py`

### 5.3 变更热点（近 5 个月提交触及次数）

`rca_core/extractor.py` 39、`server.py` 38、`tests_frontend.js` 36、`rca_core/i18n.py` 34、
`rca_core/llm.py` 32、`js/i18n.js` 32、`gui_fluent.py` 31、`rca_core/exporter.py` 30、
`js/table.js` 30、`js/minimax.js` 30、`js/app.js` 30

→ 这 11 个文件是**架构上最需要保护边界**的地方。

### 5.4 `server.py` 的 wall-clock budget 有一份"刻意分叉"

`server.py:151 _extraction_wall_clock_budget_sec` 与
`rca_core/extractor.py:137 extraction_wall_clock_budget_sec` 逻辑相同但**故意分开**：
server 那份读模块全局量，测试可改写（注释写明"tests … read and write THESE module globals"），
核心那份给矩阵路径用。**这不是重复实现，是为可测性的依赖注入**——不应合并，但应加交叉引用注释。

### 5.5 `rca_core/i18n.py` 是 Tkinter 专属翻译却放在核心包

`rca_core/i18n.py`（1,914 行）docstring 自述为 "UI translations for the **Tkinter GUI**"，
而 Web 端有独立的 `js/i18n.js`（1,249 行）。核心包里放了一个只服务于 Tk 端的上层资产。

## 6. 复现命令

```bash
cd "D:/GIthub/Range-chart Analyzer"
git ls-files '*.py' | xargs wc -l | sort -rn | head -20
grep -oE "^(from|import) rca_core[.a-z_]*" rca_core/*.py
grep -hoE "^def [a-z_]+" gui.py gui_fluent.py gui_fluent_pages.py server.py | sort | uniq -c | sort -rn
grep -rn "range_chart\|columnar_section\|abundance_diagram\|phylogenetic_tree\|zonation_chart" js/table.js | head
git log --since="2026-06-01" --name-only --format="" | grep -v '^$' | sort | uniq -c | sort -rn | head
```
