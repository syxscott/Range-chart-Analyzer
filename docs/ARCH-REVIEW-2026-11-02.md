> **【主理人复核附注 · 2026-11-05】** 本报告归档自 `C:/Users/Administrator/ARCH_REVIEW_2026-11-02.md`。
> 主理人逐条复核后的两处更正：
> 1. **`normalizer_ref` 是死字段**——本报告称它"被消费者延迟解析、设计上预演过 modes_impl 搬家"，但全仓库搜不到任何解析代码（只有 modes.py 的声明）。阶段 3 的方案仍成立（字段已声明、未来接线时可用），但"预演"的说法撤回。
> 2. **JS 命名空间"污染"降级**——实测 659 个顶层全局符号、0 个跨文件重名、60% 带 rca 前缀。真问题是"无模块系统"的结构性事实，不是活跃 bug。
> 其余结论（54 条依赖边、2 个循环、P0-1 浮点分叉、三端裁决、四阶段路线图）均已独立复现成立。

# Range-chart-Analyzer 架构收口评审

评审日期：2026-11-02　评审人：系统架构师（梁承宇）
方法：全部结论基于本人对代码的只读实测（Read/Grep/Glob/Bash + AST 解析），行号可复核。

> **版本说明**：本文件为 2026-11-02 初版。团队 lead 随后复测并补充了若干"已实测事实"，其中 **两条与代码不符**，已在 §0 校正。终版以本文为准。

---

## 0. 评审前提：对既有"事实清单"的校正

委托方给出的实测事实中，有 **两条与代码实际不符**。我先校正，因为后续结论建立在这些之上。

| 委托方说法 | 实测结果 | 证据 |
|---|---|---|
| "`rca_core` 模块之间几乎不互相依赖，只有 6 条边" | **51 条边**，其中 **19 条是模块级 import、32 条是函数内延迟 import**。另有 **2 个真实循环** | AST 全量解析，见 §1.3 |
| "`_norm` 在 4 个模块里各自定义了一份（aggregate/eval_metrics/extractor/matrix）" | **`_norm` 这个精确符号只在 `rca_core/aggregate.py:153` 定义了一次**，其余三处是**语义不同的**函数（`_normalize_taxon` / `_norm_name` / `_normalize_section`）。**但重复是真的，只是重复的量级比描述的更大——重复的是"决策"而非"函数"** | 见 §2.1、§2.2 |
| （后续补充）"`rca_core` 内无 tkinter、无 web 专属代码，边界干净" | **基本成立**：实测零 `import tkinter` / 零 `import http.server`。但有 **3 处入口层语义漏进核心层**（`i18n.py` 的 docstring 自称 Tkinter 专属却�� `exporter`/`report` 依赖；`i18n.py` 里的英文 fallback 规则；`secrets_store.py` 的 `import keyring` 是桌面平台能力） | §0.1 |

### 0.1 「边界干净」的三处漏（补充校正）

| 位置 | 漏进来的东西 | 证据 |
|---|---|---|
| `rca_core/i18n.py:1` | docstring 自称 **"UI translations for the Tkinter GUI"** | 而 `exporter.py:919,931` 和 `report.py:127` 都 import 它 —— **它被两个核心领域模块依赖，不是桌面专属** |
| `rca_core/exporter.py:910,929` | 英文 humanize fallback 规则 | 属导出器语义，却实现在翻译模块里 |
| `rca_core/secrets_store.py` | `import keyring` | 系统钥匙串是**桌面平台能力**；服务端部署无 keyring，API Key 存储会退化 —— 这是一处真正的**部署形态分叉** |

同时补充两条委托方未提到、但对架构判断更关键的事实：

- **`js/table.js` 同时是浏览器端和桌面端的表格渲染实现**。`gui_fluent_history_detail.py:69` 直接读取 `js/table.js` 并内联进 QWebEngineView。桌面端并非纯 Python 实现。
- **入口层是 5 个而非 3 个**：`main.py` 分发到 `gui.py`(Tkinter) / `gui_fluent.py`(PySide6) / `server.py`(Web) / `app.py`(PyWebView 壳)。

这两点直接改变结论，见 §5（三端合并裁决）与 §2.4。

---

## 1. 当前架构的真实形态

### 1.1 实际依赖图

我先用 AST 重建了全量依赖图（含函数内延迟 import），而不是靠 grep 猜测。

```mermaid
graph TD
    subgraph ENTRY["入口层 (5 个进程内入口 + 2 个仓外代理)"]
        MAIN["main.py (306L)<br/>分发器"]
        GUI["gui.py (2973L)<br/>Tkinter"]
        GF["gui_fluent.py (5249L)<br/>PySide6"]
        SRV["server.py (4031L)<br/>http.server"]
        APP["app.py (815L)<br/>PyWebView 壳"]
        GHD["gui_fluent_history_detail.py<br/>QWebEngineView"]
        PROXY["proxy/cloudflare-worker.js<br/>proxy/deno-proxy.js"]
    end

    subgraph CORE["rca_core (46 模块, 纯 stdlib+可选Pillow)"]
        MODES["modes.py ★唯一声明源<br/>ModeSpec 注册表"]
        PROMPT["prompt.py"]
        LLMC["llm.py (3663L)<br/>传输+配置+配额"]
        PROTO["protocols.py<br/>3 个 API 适配器"]
        EXTR["extractor.py (7055L)<br/>上帝模块"]
        QUAL["quality.py"]
        AGG["aggregate.py"]
        EXP["exporter.py (3640L)"]
        EDIT["editable.py"]
        NAMES["names.py"]
        GRID["grid.py / matrix.py / deskew.py"]
        I18N["i18n.py (1914L)<br/>翻译目录"]
        OTHER["cache/history/usage/db<br/>ssrf/secrets/redact/...]
    end

    subgraph WEB["js/ (浏览器端对等实现, 24,005 行)"]
        MM["minimax.js (4406L)"]
        TBL["table.js (4654L) ★桌面端也用"]
        QJ["quality.js (1947L)"]
        AGJ["aggregate.js (1532L)"]
        PJ["prompt.js (1081L)"]
        JU["json-utils.js (742L)"]
        IJ["i18n.js (1249L)"]
    end

    MAIN --> GUI & GF & SRV & APP
    GUI --> CORE
    GF --> CORE
    SRV --> CORE
    GHD --> TBL
    APP --> SRV
    PROXY -.仅转发.-> LLMC

    MODES -.数据.-> PROMPT & EXTR & AGG & CAP
    LLMC <--> PROTO
    EXTR <--> MATRIX
    EXTR --> QUAL & AGG & EDIT & NAMES & GRID
    QUAL --> NAMES
    AGG --> EXP
    EXP --> EDIT
    REPORT --> QUAL
    EXTR --> PROMPT
    EDIT -.-> EXP

    MM -.镜像.-> EXTR
    TBL -.镜像.-> EXP
    QJ -.镜像.-> QUAL
    AGJ -.镜像.-> AGG
    PJ -.镜像.-> PROMPT
    JU -.镜像.-> JSONU
    IJ -.镜像.-> I18N
```

### 1.2 分层判定

| 层次 | 实际状况 | 判定 |
|---|---|---|
| 入口层 → 核心层 | **单向，干净**。`server.py` 只 `import rca_core.*`，从不反向 | ✅ 边界成立 |
| `rca_core` 内部 | 51 条边、2 个循环、无分层 | ⚠️ 是"平铺的工具箱"，不是分层架构 |
| 核心层 → JS | **反向依赖**：`gui_fluent_history_detail.py:69` 读 `js/table.js` | ⚠️ 边界漏了 |
| Python → JS 一致性 | 靠 **差分测试** 而非共享代码保证 | ⚠️ 本质是"双实现 + 契约测试" |

### 1.3 实测依赖边（全部 51 条）

**模块级 import（11 条，import 时即生效）**

```
aggregate   -> modes, reason_codes
capabilities-> modes
chart_mode  -> modes
editable    -> exporter
eval_metrics-> bed_parser, reason_codes
exporter    -> bed_parser
extractor   -> bed_parser, cache, deskew, grid, image_hash, json_utils,
               llm, matrix, modes, prompt, provider_failure, reason_codes
grounding   -> json_utils, llm
history     -> db
llm         -> model_capabilities, protocols, provider_failure, redact, ssrf
matrix      -> grid
prompt      -> modes, reason_codes
protocols   -> llm          ← 循环
quality     -> age_patterns, bed_parser, grounding, reason_codes
redraw      -> bed_parser
report      -> i18n, quality, reason_codes
usage       -> db
```

**函数内延迟 import（40 条）** —— 关键：`extractor`、`protocols`、`matrix`、`llm` 大量用延迟 import 规避循环。

**两个真实循环：**

1. `llm ↔ protocols`：`llm.py:2749` 导入 `protocols` 的适配器类；`protocols.py` 通过 12 处 `from .llm import` 反向依赖 llm 的 **14 个私有符号**（`_adapters` `_api_base` `_assemble_anthropic_payload` `_coerce_text` `_decode_err_body` `_gemini_path` `_is_safe_endpoint` `_post_json` `_read_response` `_read_response_stream` `_reasoning_request_patch` `_request_target` `_summarise_empty_payload` `parse_usage`）。
2. `extractor ↔ matrix`：`extractor.py:5706` 导入 `extract_matrix_two_stage`；`matrix.py:308` 反向导入 `extractor.extraction_wall_clock_budget_sec`。

### 1.4 内聚 / 上帝模块 / 孪生

| 类别 | 模块 | 证据 |
|---|---|---|
| **内聚良好** | `modes.py`(459L) | 唯一声明源，`ModeSpec` 数据驱动，`DESIGN CONSTRAINT: DATA ONLY, NO SIBLING IMPORTS` 明确写出约束。**这是全仓最健康的模块，也是现成的改造模板** |
| | `ssrf.py`(664L)、`redact.py`、`provider_failure.py`(452L)、`bed_parser.py`(467L)、`image_hash.py`、`json_utils.py`(886L) | 单一职责、无反向依赖、纯 stdlib |
| **上帝模块** | `extractor.py`(7055L, 94 个顶层函数) | 6,047 行在 94 个函数里；单函数最大 `normalize_occurrence_matrix_result` 389L。**同时**承担：图像解码/增强、8 种模式的 extract、11 种 normalize、缓存键、模式分类、自动模式解析 |
| | `server.py`(4031L) | `Handler._handle_extract_body` = **995 行单方法**（2803–3797）；`do_GET` 254L |
| | `exporter.py`(3640L, 70 函数) | 表格配置 + CSV/TSV/JSON/XLSX/WPD 五种格式 + 表格编辑 + i18n 标签 |
| | `llm.py`(3663L) | 传输层 + 供应商配置存储 + 配额查询 + 推理档位 + 重试 + 取消，**6 类不相关职责** |
| **孪生实现（Python↔JS）** | `js/minimax.js` ↔ `extractor.py`；`js/table.js` ↔ `exporter.py`；`js/quality.js` ↔ `quality.py`；`js/aggregate.js` ↔ `aggregate.py`；`js/prompt.js` ↔ `prompt.py`；`js/json-utils.js` ↔ `json_utils.py`；`js/reason-codes.js` ↔ `reason_codes.py`；`js/i18n.js` ↔ `i18n.py`；`js/bed_parser.js` ↔ `bed_parser.py` | 9 对 |
| **孪生实现（Python↔Python）** | `aggregate.py::_looks_*` ↔ `exporter.py::_looks_*`；`gui.py::_collect_extraction_futures` ↔ `gui_fluent.py::_collect_extraction_futures`；`server.py` 两处 `cache.make_key` | 见 §2 |
| **孪生实现（JS↔JS）** | `js/quality.js:105 rcaWarningFlags` ≡ `js/table.js:2484 rcaEditWarningFlags`（**105 行逐字节相同，两处都在用**）；`js/aggregate.js:823 rcaPyFloat` / `js/minimax.js:822 rcaPyFloatOrNull` / `js/table.js:1076 rcaPyFloatStr` | 见 §2.2 |

---

## 2. 按严重度排序的问题清单

### P0-1　`js/aggregate.js` 与 `js/minimax.js` 的 Python-float 语义实��分叉，浏览器多轮合并会排错序

**代码证据**

- `js/aggregate.js:823 rcaPyFloat(v)`：正则 `_PY_FLOAT_RE`（`js/aggregate.js:823` 上方），**无 Unicode Nd 折叠**
- `js/minimax.js:822 rcaPyFloatOrNull(value)`：正则 `_RCA_PY_FLOAT_RE`，**有** `rcaPyFloatTransliterate`（`js/minimax.js:791`）
- `js/minimax.js:770` 的注释自己承认："kept as a local copy because aggregate.js and minimax.js each have to run standalone"

**我实测的 divergence（用仓内真实文件，vm 加载后调用）**

```
'１２７' -> js/aggregate.js rcaPyFloat    : null
'１２７' -> js/minimax.js rcaPyFloatOrNull: 127
'１２７' -> Python float()                : 127.0     ← aggregate.js 单独偏离 Python
```

即：**`js/aggregate.js` 对全角数字的返回值与 Python 引擎不一致**，`js/minimax.js` 才是对的。两边都自称是 Python `float()` 的镜像。

**可达性**：`js/aggregate.js:844 rcaSortTuple` → `js/aggregate.js:1172 merged.sort(...)`；`rcaMergeResults`（`js/aggregate.js:1338`）被 `js/app.js:1568` 调用。**浏览器端 runs>1 的合并排序路径**。

**为什么是问题**
- 全角数字在 CJK 图表标注里不罕见（"深度260 Ma" 的全角变体、OCR 产物）。
- Python 侧返回 `127.0` → 进入数值排序分支；JS 侧返回 `null` → 落入字符串分支（`rcaSortTuple` 返回 `[1, 0.0, s.toLowerCase(), s]`）。**同一批数据，服务端合并后顺序与浏览器合并后顺序不同**，且"数值降序"在浏览器上静默变成字符串降序。
- 这正是本仓已被咬过两次的形状：`tests/js/tests_diff_frontend_parity.js:35` 的注释明确写了这个 bug 类——"ONE decision, SEVERAL implementations, and a fix that landed on only one of them"。

**改造方案**
1. 把 `rcaPyFloatTransliterate` + `_rcaPyUnderscoresLegal` + 统一正则提到新的 `js/pyfloat.js`（单文件、纯函数、无 DOM 依赖）。
2. `js/aggregate.js` / `js/minimax.js` / `js/table.js` 全部改为从它取，删除 `aggregate.js` 的本地正则。
3. 在 `tests/js/tests_diff_frontend_parity.js` 的 `SCRIPTS` 列表里加入 `js/pyfloat.js`，并新增一个 group `py_float`，把 `'１２７'` `'٥٠٠'` `'1_2e3_4'` `'inf'` `'nan'` 作为用例。

**风险与验证**
- 风险：改 `aggregate.js` 的浮点判定会影响已有合并顺序。低风险，因为 `aggregate.js` 现状是**偏离 Python 的一方**，改后与 Python 对齐。
- 验证门：① `node tests/js/tests_diff_frontend_parity.js` 仍报 `0 diverged`；② 新 group 覆盖上述 5 个输入；③ 新增一条断言：`rcaPyFloat` 与 `rcaPyFloatOrNull` 对同一输入返回相同值（当前必然失败）。

**工作量**：0.5 人日。

---

### P0-2　缓存键构造在 3 处手写，字段集各不相同；`make_key` 的"漏字段即静默降级"语义无保护

**代码证据**

AST 提取全部 `make_key` 调用点的字段名：

| 调用点 | 字段数 | 字段集 |
|---|---|---|
| `server.py:3149`（单轮） | 13 | 缺 `run_idx` |
| `server.py:3449`（多轮） | 14 | 全集 |
| `rca_core/extractor.py:6668`（分类） | 11 | 缺 `max_tokens`、`media_type`、`run_idx` |

**为什么是问题**

- `rca_core/cache.py:203 make_key(**fields)` 是**可变关键字参数**：漏传一个字段，它就**静默地不在键里**，不报错、不告警。
- 仓库里已经因为这个形状出过事故，且写进了注释：`rca_core/extractor.py:6674` 的 AUDIT-2026-10-01 [item 9.9] 记录了分类键漏 `extra_headers`/`extra_body` 的事故。
- 现有的防护**打不中这个洞**：`tests/test_cache_key_identity.py:194 _key_field_sets()` 只断言"每个调用点都包含 `extra_headers` / `extra_body` / `reasoning_effort` 三个必需字段"，**没有断言"各调用点字段集一致"或"字段集等于某个声明的规范"**。`tests/test_cache_key_completeness.py` 则只测 `make_key` 本身，不涉及调用者——它的 docstring 第 3 行就承认这个 bug 是 "the server.py caller omitted extra_body"。
- 具体风险：`server.py:3149` 的单轮键缺 `run_idx`，`server.py:3449` 的多轮键有 `run_idx`。**一次单轮请求的缓存结果，和一次多轮请求的第 0 轮请求，键不同但语义几乎相同** → 缓存命中率被无谓地打散。更糟的情形：将来新增一个会改变请求形状的字段（例如 `seed`、`temperature`），如果只加到 `server.py` 一处，就复现 9.9。

**改造方案**
1. 在 `rca_core/cache.py` 新增 `CACHE_KEY_FIELDS: frozenset[str]`（规范字段集声明）。
2. 新增 `rca_core/cache.py::build_extraction_key(*, provider, mode, prompt_version, max_tokens, chart_lang, image_b64, caption, media_type, run_idx=0)` —— **唯一**的构造入口，内部显式列出全部字段。
3. `server.py` 两处、`extractor.py` 一处全部改为调用它。
4. `make_key` 保留原样（`names.py` 的 `kind="gbif_name"` 走的是另一套键，不受影响），但新增 `assert set(fields) == CACHE_KEY_FIELDS` 之类的守卫。

**风险与验证**
- 风险：缓存键变化 → 旧缓存全部失效（一次性，用户无感，因为缓存只是优化）。
- 验证门：① `tests/test_cache_key_completeness.py` 现有断言全绿（`make_key` 语义未变）；② 新增测试用 AST 断言"**所有** `make_key` 调用点的字段集 ⊆ `CACHE_KEY_FIELDS`，且 `server.py` 的两个调用点字段集相同"；③ 端到端：同一请求连发两次，第二次 `cache_hit=True`（现有 `tests/standalone/tests_server_hardening.py` 有类似断言可参照）。

**工作量**：0.5–1 人日。

---

### P1-3　`_looks_*` 模式探测器在 3 处各写一遍，且 `aggregate.py` 缺 `zonation`/`paleomap` 分支

**代码证据**

| 模块 | 探测器 | 行号 |
|---|---|---|
| `rca_core/aggregate.py` | `_looks_abundance` / `_looks_columnar` / `_looks_phylogenetic` / `_looks_occurrence_matrix` | 807 / 817 / 830 / 852 |
| `rca_core/exporter.py` | `_looks_columnar` / `_looks_abundance` / `_looks_phylogenetic_tree` / `_looks_zonation_chart` / `_looks_paleomap` / `_looks_occurrence_matrix` | 314 / 334 / 351 / 363 / 762 / 1043 |
| `js/table.js` | `rcaLooksAbundance` / `rcaLooksColumnar` / `rcaLooksColumnarEmptySections` / `rcaLooksPhylogeneticTree` / `rcaLooksZonationChart` / `rcaLooksPaleomap` / `rcaLooksOccurrenceMatrix` | 37 / 44 / 53 / 62 / 71 / 88 / 192 |

`aggregate.py:817` 的 docstring 自认："Mirror of rca_core.exporter._looks_columnar **kept private to this module** so the auto-detect stays schema-only and avoids any circular import back through exporter / extractor."

**为什么是问题**

- **探测器就是"这个 payload 是什么形状"的判定**。判定不一致 = 一个模块按 range_chart 渲染，另一个按 columnar 渲染。这已被记录为真实事故：`rca_core/exporter.py:1043 _looks_occurrence_matrix` 的 docstring 记录了"normalize_generic_table_result 也发出 `columns`，按 `columns` 判定把每个 generic_table 都分类成 occurrence matrix，于是导出时整个数据集消失，浏览器里却还看得见"。
- **`aggregate.py` 没有 `zonation`/`paleomap` 探测器**，但 `SCHEMA_BY_MODE` 里有 `zonation_chart`（`aggregate.py:779`）。`aggregate.py:785` 注释承认："extractor implements three more (chemical_stratigraphy / paleomap / …)"。也就是说 `merge_results(schema=None)` 走自动探测时，zonation payload 可能落到 range_chart 分支。
- 现有防护 `tests/test_detector_module_parity.py` 的 docstring 写明它只覆盖 "the two detectors"（`_looks_columnar` / `_looks_abundance`），**且明确说 zonation/paleomap 不在其中**（grep 确认无 `zonation`/`paleomap` 字样）。

**改造方案**
1. 新建 `rca_core/shape.py`，放**唯一**一份探测器：`looks_columnar` / `looks_abundance` / `looks_phylogenetic` / `looks_occurrence_matrix` / `looks_zonation` / `looks_paleomap`。
2. `aggregate.py`、`exporter.py` 改为 `from .shape import looks_*`。**注意依赖方向**：`shape.py` 必须零依赖（只依赖 stdlib），否则会引入新循环（`exporter → aggregate` 已存在）。
3. `aggregate.py` 补 `zonation`/`paleomap` 分支，或显式把这两个模式标为"不可自动探测"（在 `modes.py` 加 `auto_detect=False` 标志），并让 `merge_results` 对未探测到的模式**抛错**而非静默用 range_chart schema。
4. JS 侧：`js/table.js` 的 7 个 `rcaLooks*` 保持不变（跨语言无法共享代码），但**扩展 `tests/test_detector_module_parity.py` 到全部 6 个探测器 × 2 个 Python 模块**，并新增一条"JS 侧 7 个探测器与 Python 6 个探测器逐个对照"的断言。

**风险与验证**
- 风险：改判定逻辑会改变已有导出的分表结果。**这是真实风险**，必须用金标数据回归。
- 验证门：① `tests/test_detector_module_parity.py` 扩展后全绿；② `tests/fixtures/gold/` 的金标用例导出表数量与表名**逐字节不变**（用 `git diff` 比对 `tests/fixtures/gold/` 的期望产物）；③ `tests/js/tests_export_parity.js` 全绿。

**工作量**：1.5 人日。

---

### P1-4　`llm.py` ↔ `protocols.py` 循环依赖：所谓"可插拔协议适配器"的边界是反的

**代码证据**

- `rca_core/protocols.py:39`：`from .llm import ApiFormat, LlmProvider`（`TYPE_CHECKING` 下）+ **12 处运行时 `from .llm import ...`**
- `protocols.py` 从 `llm` 反向取走 **14 个私有符号**（含 `_post_json` `_read_response` `_read_response_stream` `_decode_err_body` `_api_base` `_gemini_path` `_request_target` `_assemble_anthropic_payload` `_coerce_text` `_summarise_empty_payload` `_reasoning_request_patch` `_is_safe_endpoint` `_adapters` + `parse_usage`）
- `rca_core/llm.py:2739 _adapters()` 的 docstring 自认："protocols.py needs the transport from this module, and this module needs the adapter classes from that one. The import is therefore resolved at CALL time rather than at import time"
- `rca_core/protocols.py:44` 的 `adapter_for` docstring："the two modules need each other, and a module-level import in both directions would be a cycle"

**为什么是问题**

- `protocols.py` 的设计意图是"一种 API 格式一个适配器"（`rca_core/llm.py` 顶部 docstring 与 `bf7627b Release 0.1.3: model capabilities with sources, pluggable protocols` 提交信息都这么说）。但**适配器拿到的是传输层的私有函数**，所以：
  - 新增一个 API 格式，必须同时改 `llm.py`（加 `ApiFormat` 枚举值 + `_adapters()` 注册）和 `protocols.py`（加类）。**插件点并没有真正开在协议层**。
  - 传输层（`_post_json` / `_read_response_stream`）的签名一改，**三个适配器全部要改**。`protocols.py` 里 14 个私有 import 就是一个隐藏的耦合面。
- 依赖方向被反转了：正确的应该是"传输层在下，适配器在上"。现在是适配器向下伸手抓传输层的私有实现。

**改造方案**

把**传输层**从 `llm.py` 抽出为独立模块，让依赖变成单向：

```
rca_core/transport.py   （新）—— _post_json / _read_response / _read_response_stream /
                           _decode_err_body / _coerce_text / _summarise_empty_payload /
                           _api_base / _gemini_path / _request_target /
                           _assemble_anthropic_payload / _reasoning_request_patch
rca_core/protocols.py   —— 依赖 transport（单向）
rca_core/llm.py         —— 依赖 protocols + transport（单向）
```

- `llm.py` 保留**公开** API（`call_llm_api` / `call_llm_api_with_retry` / `LlmProvider` / `ProviderStore` / `ApiFormat`），内部改为调用 `protocols.adapter_for(...)`。
- `_is_safe_endpoint` **不搬**——它属于 `ssrf.py` 的职责，`llm.py` 已经单向依赖 `ssrf`；应改为 `protocols.py` 直接依赖 `ssrf.py`。
- `parse_usage` 属于 token 计量 → 搬进 `transport.py` 或复用 `usage.py`（`usage.py` 已有 `parse_usage`，需核对是否同一实现，避免再造一份）。
- `Adapter` 基类改为**接收** transport 函数（依赖注入）而非 import。

**风险与验证**

- 风险：**中高**。`llm.py` 是全仓最热的路径，且 `protocols.py` 刚在 0.1.3 引入，重构可能踩到只有真实 API 调用才暴露的路径。
- 缓解：分两步做——**先纯搬家不改行为**（把私有函数剪切到 `transport.py`，`llm.py` 用 `from .transport import _post_json` 原样转发），此时 `protocols.py` 仍可 import `llm.py`。跑绿后，**再切断 `protocols → llm`**，改为 `protocols → transport`。
- 验证门：① 第一步后 `pytest tests/ -q` 全绿（4,360 项）；② 第二步后全绿；③ `tests/js/tests_diff_frontend_parity.js` 不受影响的 group 数不变；④ 手工跑一次真实 provider 的连通性测试（`rca_core/llm.py:3632 test_llm_connection`）；⑤ 用 `python -X importtime -c "import rca_core.llm"` 确认 import 图变单向（可加一条 CI 断言：用 AST 检查 `rca_core` 内无环）。

**工作量**：2–3 人日。

---

### P1-5　`extractor.py`（7055 行）是上帝模块，但它**可以**被安全切分——切割线已经现成

**代码证据**

`rca_core/extractor.py` 内部有清晰的分区（实测顶层函数分布）：

| 分区 | 代表函数 | 行数 |
|---|---|---|
| 图像 I/O + 增强 | `load_image_b64`(182L)、`_enhance_*` | 364–545 |
| 通用几何 | `_geometry_point`(56L)、`geometry_from_row`(67L) | 2111–2235 |
| 归一化公共件 | `_normalize_species_into`(114L)、`_normalize_biozone_into`(58L)、`_to_float_opt`(66L) | 1862–2465 |
| **8 种模式的 extract** | `extract_range_chart`(135L)、`extract_occurrence_matrix`(326L)、`extract_paleomap`(88L)… | 2757–6058 |
| **11 种模式的 normalize** | `normalize_result`(287L)、`normalize_occurrence_matrix_result`(389L)、`normalize_columnar_result`(298L)… | 2468–6593 |
| 视觉分类 + 自动模式 | `classify_chart_image`(150L)、`resolve_auto_mode`(61L) | 6619–6831 |
| 入口 | `extract`(147L) | 6909–7055 |

**为什么是问题**

- 94 个顶层函数、6,047 行在一个文件里。任何一个模式的改动都要在 7,055 行里定位、review、回归。
- **模块级依赖已重度超载**：`extractor` 在模块级就 import 了 `cache`、`deskew`、`grid`、`image_hash`、`json_utils`、`llm`、`matrix`、`modes`、`prompt`、`provider_failure`、`reason_codes` **共 11 个**。其中 `cache` 只用于 2 处（`6664`、`6755`）、`image_hash` 只用于 1 处（`compute_image_sha256_from_b64`）——**为 1–2 次调用付一个模块级 import 边**。
- 已经有 2 个循环与它相关（`extractor ↔ matrix`）。

**改造方案（低风险优先，只做"搬文件"不做"改逻辑"）**

**阶段 1（低风险，先摘掉重依赖）**：把 `image_hash` 和 `cache` 的 import 改为函数内延迟 import。
- 理由：`extractor.py:6664` 的 `from .cache import (...)` **已经在函数内**了；`image_hash` 是模块级（`extractor.py:62`）但只用 1 次。
- 收益：`import rca_core.extractor` 的模块加载面变小；不改变任何行为。
- 验证门：`pytest tests/ -q` 全绿 + `python -X importtime` 对比。

**阶段 2（切模式族，风险递增）**：新建 `rca_core/modes_impl/` 包，把 11 个 `normalize_*` 按模式族切开：
```
rca_core/modes_impl/range_chart.py       normalize_result
rca_core/modes_impl/columnar.py          normalize_columnar_result
rca_core/modes_impl/occurrence.py        normalize_occurrence_matrix_result
rca_core/modes_impl/phylo.py             normalize_phylogenetic_tree_result
rca_core/modes_impl/zonation.py          normalize_zonation_chart_result
...
```
- `rca_core/extractor.py` 保留 `extract` / `extract_*` / `load_image_b64` / 公共归一化件，转为 `from .modes_impl.range_chart import normalize_result` 再导出（保持 `rca_core/__init__.py` 的公开 API 不变）。
- ~~`rca_core/modes.py` 已经用 `normalizer_ref=...` 字符串引用，消费者延迟解析~~ **【2026-11-02 复核更正】**：`normalizer_ref` 在 `modes.py` 声明了 11 次，但**全仓库没有任何运行时解析代码**（grep 全仓只有 `modes.py` 的声明与 docstring，加上 `tests/test_occurrence_matrix.py:658 test_d5` 用 `importlib` 验证字符串可解析）。`extractor.py` 调用 normalizer 走的是**直接 import**，从不经过注册表 —— **`normalizer_ref` 目前是死字段**（team-lead 独立复核确认）。
  - **对阶段 3 的影响：不改变方案本身，但改变了论证**。搬家仍需同步改 3 处：① `extractor.py` 的 import 与调用点；② `normalizer_ref` 字符串（11 处，保持声明与事实一致，且 `test_d5` 会强制它可解析）；③ parity fixture 生成器的 import。"设计上被预演过"这个说法撤回——准确的说法是：**字段已声明且有解析验证测试，未来把调度切到注册表时可用，但今天它不被消费**。搬家工作量不变（3–4 人日），因为 ①③ 本来就在方案里。

**风险与验证**

- 风险：**中**。切分后 `normalize_*` 的调用方（`extractor.extract_*`、`aggregate.py`、`exporter.py`、`tests/gen_frontend_parity_fixtures.py`）都要跟着改 import。
- 关键约束：**`tests/gen_frontend_parity_fixtures.py` 直接 import 了 9 个 `normalize_*`**（实测：`normalize_abundance_result` / `normalize_chart_classification` / `normalize_columnar_result` / `normalize_result` / `normalize_zonation_chart_result` / `_normalize_phylogenetic_tree_into`）。**必须保留 `rca_core.extractor` 上的 re-export**，否则差分测试的 fixture 生成器要改，改了就动摇了 parity 契约的地基。
- 验证门：① 切分后 `rca_core.extractor` 的公开名字**一个不少**（写一条测试断言 `set(dir(rca_core.extractor))` 的差集为空，或更实际：对每个被搬走的名字，在 `extractor.py` 留一行 `from .modes_impl.X import Y  # noqa: F401  # re-export`）；② `pytest tests/ -q` 4,360 项全绿；③ `tests/fixtures/frontend_parity_2026_09_20.json` **重新生成后 `git diff` 为空**（这一步同时证明 Python 行为未变、且差分契约未被悄悄改动）；④ `node tests/js/tests_diff_frontend_parity.js` 仍 `0 diverged`。

**工作量**：阶段 1 = 0.5 人日；阶段 2 = 3–4 人日（11 个模式 × 搬 + 回归）。

---

### P1-6　`server.py` 的 `Handler._handle_extract_body` 是 995 行单方法，且**内含两份缓存键构造**

**代码证据**

- `server.py:2803–3797`：`_handle_extract_body`，**995 行**，内部有 8 个嵌套 `if` 分支块（`3120-3373` 单个 if 就 254 行）
- `server.py:3149` 与 `server.py:3449`：两份 `cache.make_key(...)`，字段集不同（见 P0-2）
- `server.py:3663`：`merged = merge_results(ok_datas, total_runs=runs, schema=schema)` —— **编排逻辑在 HTTP handler 里**
- `server.py:412-447`：server.py 只 import 12 个 `rca_core` 模块 —— 边界方向是对的，但**编排没下沉**

**为什么是问题**

- HTTP handler 里同时承担：请求校验 → CSRF/Origin → 图像加载 → 供应商解析 → 缓存探测 → **N 路并发扇出** → 合并 → 质量评分 → 报告生成 → 用量聚合 → 审计落库。
- `server.py:3443-3491` 的多轮缓存逻辑，与 `gui.py:1990-2045`、`gui_fluent.py:780-885` 的多轮扇出逻辑是**同一个决策的三份实现**（见 P2-7）。
- **`_handle_extract_body` 无法被单元测试直接调用**（它需要 `self.path`/`self.headers`/`self.rfile`），所以这 995 行的实际测试覆盖只能通过整条 HTTP 路径间接触达 —— 这是 `tests/standalone/tests_server_hardening.py` 这类"脚本式"套件存在的原因。

**改造方案**

1. **先下沉编排，不动 HTTP 语义**：新建 `rca_core/pipeline.py`，导出
   ```python
   def run_extraction_batch(*, provider, mode, image_b64, caption, media_type,
                            max_tokens, chart_lang, runs, cache, force_rerun,
                            ground_names, timeout_sec, progress=None) -> BatchOutcome
   ```
   承载：缓存键构造（复用 P0-2 的 `build_extraction_key`）、多轮槽位映射、并发扇出、合并、评分、报告、用量聚合。
2. `server.py::_handle_extract_body` 缩到 **≤120 行**：解析请求 → 调 `pipeline.run_extraction_batch` → 序列化响应。
3. `gui.py` / `gui_fluent.py` 的多轮逻辑改为调同一 `pipeline.run_extraction_batch`（**这一步同时解决 P2-7**）。

**风险与验证**

- 风险：**高**。这是最热的服务端路径，且扇出/预算/取消语义很微妙（`gui_fluent.py:790-830` 的"取消发生在启动期间"处理、`matrix.py:308` 的预算计算都依赖当前结构）。
- 缓解：**先在 `rca_core/pipeline.py` 里实现新函数，但 server.py 暂不改调用**，先用差分测试证明"新函数在 N 组输入下与旧路径输出逐字节相同"；确认后再切换。这叫**并行实现 + 差分验证**，是唯一能安全动 995 行方法的方式。
- 验证门：① 新旧实现对同一组 20 组请求参数产出**逐字节相同的 JSON**（用 `json.dumps(sort_keys=True)` 比较）；② `pytest tests/ -q` 全绿；③ `tests/standalone/tests_server_hardening.py` 全绿；④ 手工验证：CSRF 拒绝路径、缓存命中路径、取消路径各一次。

**工作量**：4–6 人日（这是本方案里最贵的一项，**但它一次性解决 P0-2 / P2-7 / 入口层过重三个问题**）。

---

### P2-7　多轮扇出编排（fan-out）有三份实现

**代码证据**

| 实现 | 位置 | 行数 |
|---|---|---|
| Tkinter | `gui.py:122 _collect_extraction_futures` + `gui.py:1990-2045` | ~70 |
| Fluent | `gui_fluent.py:333 _collect_extraction_futures` + `gui_fluent.py:780-885` | ~100 |
| Web | `server.py:3443-3700`（内嵌在 handler 里） | ~260 |

- `gui.py:122` 与 `gui_fluent.py:333` 是**同一个函数的两个副本**（docstring 几乎逐字相同："Sprint B (REVIEW-2026-09-04): collect extraction futures under a REAL whole-batch time budget"）。Fluent 版多两个参数（`should_cancel` / `on_batch`）并返回 `(results, cancelled)`。
- `gui.py:143` 的 docstring 明说："Deliberately Tk-free so the timeout semantics are unit-testable without a display... **Qt twin lives in gui_fluent.py**" —— **它自己承认这是孪生体**。
- `gui.py:2012` 与 `gui_fluent.py:789` 各自建 `ThreadPoolExecutor(max_workers=runs)`，各自算 `budget = timeout_sec + 10.0`，各自 `executor.shutdown(wait=False)`。

**为什么是问题**

- `gui.py` 版**没有取消能力**（无 `should_cancel`），Fluent 版有。**同一个"用户点取消"的动作，两个桌面端行为不同**。
- `gui.py` 版无 `on_batch` 回调 → **Tkinter 端没有进度中间态**，用户只能等最终结果。
- 三份实现各自维护预算/超时/取消/失败计数的语义。任何一次修复（例如"超时判定应该含最后一次重试"）要改三处。

**改造方案**

新建 `rca_core/fanout.py`：
```python
def collect_extraction_futures(futures, budget, *, should_cancel=None, on_batch=None)
    -> tuple[list[ExtractResult], bool]
```
- **直接采用 `gui_fluent.py:333` 的版本**（它是超集：多了取消与进度），语义向后兼容。
- `gui.py:122` 改为 `from rca_core.fanout import collect_extraction_futures as _collect_extraction_futures`（**保留旧名作为别名**，`tests/test_gui_sprint_b.py:80` 直接调用 `gui._collect_extraction_futures(futures, 0.15)`，别名为它省掉改测试）。
- `gui_fluent.py:333` 同样改为 re-export。
- `server.py` 的多轮部分在 P1-6 的 `pipeline.py` 改造中一并收敛。

**风险与验证**

- 风险：**低**。Tkinter 端的行为会从"无取消"变成"有取消"（行为增强，非回归）；且 Tkinter 端目前根本不传 `should_cancel`，等价于 `None` → 新实现与旧实现行为一致。
- 验证门：① `tests/test_gui_sprint_b.py` **一行不改**通过（它同时测两个 GUI 的同一函数，是天然的等价性证明）；② `tests/standalone/tests_gui_fluent.py` 全绿；③ 新增一条测试：Tkinter 端传入 `should_cancel` 时能在预算内提前返回（新增能力，不影响旧行为）。

**工作量**：1 人日。**这是全表性价比最高的一项。**

---

### P2-8　`rca_core/i18n.py`（1914 行）是**三语翻译目录 + 英文硬编码 fallback**，不是"Tkinter 专属"

**代码证据（纠正委托方的提法）**

- `rca_core/i18n.py:1` docstring 确实写 `"""UI translations for the Tkinter GUI: Chinese / English / Japanese."""` —— **但这句话是错的**。
- 实测消费者：`rca_core/exporter.py:919,931`（`from .i18n import TRANSLATIONS`）、`rca_core/report.py:127`（`from .i18n import Translator`）、`rca_core/__init__.py:71`（公开导出）、`gui_fluent_history_detail.py:43`。
- i18n.py 本身**零 Tkinter 依赖**（grep `tkinter|Tk|font|ttk` 只命中第 1 行的 docstring）。
- 它不只是翻译表，还实现了**英文 fallback 规则**：`exporter.py:910 _col_label` 与 `exporter.py:929 _title_label` ——"generic table 无法为任意列名备 i18n key，已存在的用 i18n key，其余 humanize，且 `Translator.t` 对未知 key 原样返回"。

**为什么是问题**

- **docstring 与事实相反**，会误导后来者把 i18n 当成"桌面专属、可以随手搬走"的东西。而它实际上被**导出器**和**报告生成器**（两个核心领域模块）依赖。
- 它是 `js/i18n.js`（1,249 行）的镜像，且**两侧的 parity 由 `tests/js/tests_contrast.js:578-640` 锁定**（会读 `rca_core/i18n.py` 的源码文本，与 `js/i18n.js` 逐 key 比对三语）。
- 翻译目录 1,914 行 + JS 1,249 行 = 3,163 行文案，**是全仓最容易 drift 且最难测的部分**。

**改造方案**

1. **改 docstring**，并把模块拆成两个职责：
   - `rca_core/i18n.py` —— 保留 `TRANSLATIONS`（三语目录）+ `Translator`（`rca_core/i18n.py:1896`），**继续留在核心**（因为 exporter/report 依赖）。
   - 把英文 humanize fallback 规则（`_col_label` / `_title_label` 的逻辑）从 `exporter.py` 抽到 `rca_core/i18n.py` 的 `column_label(key)` / `table_title_label(key)`，让"fallback 规则"和"fallback 规则的文案"在一起。**这会消除 `exporter.py` 对 i18n 的两处函数内 import**。
2. **不加新测试**——`tests/js/tests_contrast.js` 已经覆盖两侧一致性，本项只改 docstring + 挪两个函数。

**风险与验证**

- 风险：**低**（docstring 改动零风险；挪两个纯函数需确认 `exporter.py` 的调用点只有 `_col_label` / `_title_label` 两处，实测确认）。
- 验证门：① `pytest tests/ -q` 全绿；② `node tests/js/tests_contrast.js` 全绿（三语一致性）；③ `tests/test_table_configs_parity.py` 全绿（它同时用 `TRANSLATIONS` 和 `Translator`）。

**工作量**：0.5 人日。

---

### P2-9　JS 层三份"Python 数值语义"实现，其中一份已确认偏离 Python

**代码证据**

- `js/aggregate.js:823 rcaPyFloat`（偏离，见 P0-1）
- `js/minimax.js:822 rcaPyFloatOrNull`（正确）
- `js/table.js:1076 rcaPyFloatStr`（Python `repr()` 语义，第 3 种行为：转字符串）
- `js/aggregate.js:823` 上方注释自述："Same rule as rcaPyFloat in js/aggregate.js — kept as a local copy because aggregate.js and minimax.js each have to run standalone (**tests_aggregate.js loads aggregate.js alone**)"
- `js/minimax.js:2343 rcaNormalizeResultOldDupA` —— **9 行死代码**（定义了但全仓无调用点，grep 确认）

**为什么是问题**

- `tests_aggregate.js` 单独加载 `aggregate.js` 的约束，导致每个文件都要自带一份语义实现 —— **测试架构倒逼了代码重复**。
- `js/table.js:1076 rcaPyFloatStr` 与另两份**行为不同**（返回字符串 vs 数字），不能简单合并；但它们的**共同前提**（Python float 的词法）必须一致。

**改造方案**

1. 新建 `js/pyfloat.js`，导出三个函数：`rcaPyFloat`（数字）/ `rcaPyFloatOrNull`（同 `rcaPyFloat`，保留旧名）/ `rcaPyFloatStr`（字符串）。
2. 让 `tests_aggregate.js` 加载 `js/pyfloat.js`（改 1 行脚本列表），然后三处改为 `from js/pyfloat.js`。
3. 删除 `js/minimax.js:2343 rcaNormalizeResultOldDupA`（死代码）。

**风险与验证**

- 风险：**低**。
- 验证门：① `node tests/js/tests_aggregate.js` 全绿（注意它需先加 `js/pyfloat.js` 到加载列表）；② `node tests/js/tests_diff_frontend_parity.js` 仍 `0 diverged`；③ 新增断言：`rcaPyFloat` 与 `rcaPyFloatOrNull` 对同一输入恒等（当前不成立）。

**工作量**：0.5 人日（与 P0-1 合并做，总 1 人日）。

---

### P3-10　`js/quality.js:105 rcaWarningFlags` 与 `js/table.js:2484 rcaEditWarningFlags` 是 105 行逐字节相同的重复，且两处都在用

**代码证据**

AST/正文归一化比对：两函数体**完全相同**（105 行）。调用点：
- `js/quality.js:972`、`js/quality.js:1334` → `rcaWarningFlags`
- `js/table.js:2509` → `rcaEditWarningFlags`

**为什么是问题**：不是 P0，但 105 行 × 2 = 210 行冗余，且这是"同一个决策两处实现"的形状——正是本仓最贵的 bug 类。

**改造方案**：提到 `js/warnings.js`（或并入 `js/pyfloat.js` 同类的 `js/pycompat.js` 共享层），两处改为调用同一函数；保留两个旧名作为别名以免改动调用点。

**风险与验证**：风险低。验证门：`node tests/js/tests_quality_frontend.js` + `tests_export_parity.js` + `tests_diff_frontend_parity.js` 全绿。

**工作量**：0.5 人日。

---

### P3-11　`rca_core` 有 2 个循环依赖，且 `modes.py` 的"无循环"约束是手写的、未被机器检查

**代码证据**

- 循环 1：`llm ↔ protocols`（见 P1-4）
- 循环 2：`extractor ↔ matrix`：`extractor.py:5706` ← → `matrix.py:308`
- `rca_core/modes.py` 的 docstring 明确写着："`prompt`, `extractor` and `aggregate` all import this module, so this module must not import them — that is a cycle." —— **约束是写在注释里的，不是被检查的**

**为什么是问题**：注释不是护栏。任何人加一行 `from .extractor import X` 到 `modes.py` 都不会有任何测试失败。

**改造方案**

1. **加一条 CI 检查**（新文件 `tests/test_rca_core_acyclic.py`，纯 AST，无需运行时）：构建 `rca_core` 内部依赖图（含函数内 import），断言**无环**。这是零风险、纯新增的测试。
2. 逐步解环：`matrix → extractor` 的唯一用途是 `extraction_wall_clock_budget_sec`（`matrix.py:308`）——把它搬到 `rca_core/budget.py`（或 `modes.py` 旁边），两边都依赖它，`extractor ↔ matrix` 环即消失。**这是一个 5 行的改动就能解掉一个环。**
3. `llm ↔ protocols` 的解环并入 P1-4。

**风险与验证**

- 风险：**低**（第 2 步）。`extraction_wall_clock_budget_sec` 是个纯函数，搬家后行为不变。
- 验证门：① 新测试 `test_rca_core_acyclic.py` 在**当前代码上应当失败**（因为还有 2 个环）—— 先写测试确认它抓得到，再修；② 修完后转绿；③ `pytest tests/ -q` 全绿。

**工作量**：0.5 人日（解 `extractor↔matrix`）+ 0.5 人日（加测试 + 与 P1-4 合并解 `llm↔protocols`）。

---

### P3-12　桌面端与浏览器端**共用** `js/table.js`——这是本仓最好的架构决策，但它没有被文档化，也没被测试锁住

**代码证据**

- `gui_fluent_history_detail.py:69`：`_JS_TABLE = _PROJECT_ROOT / "js" / "table.js"`
- `gui_fluent_history_detail.py:174 _build_webengine_html`：读取 `js/table.js` + `js/aggregate.js` + `js/i18n.js`，**内联**进 `<script>`，再调 `rcaRenderResults`（`gui_fluent_history_detail.py:239`）
- `gui_fluent_history_detail.py:9` 的 docstring："Result rendering is delegated to js/table.js's `rcaRenderResults` running inside QWebEngineView, so the detail view looks identical to the live Extract page"
- **但** `gui_fluent.py` 的 ExtractPage 走的是**另一条路**：`gui_fluent.py:3426 headers, rows = build_table_export(self.result, cfg["id"], self._t)` + `QTableWidget`（`gui_fluent.py:2954`）

**为什么是问题**

- **同一个桌面应用内部有两套表格渲染实现**：ExtractPage 用 Python 的 `build_table_export` + `QTableWidget`；HistoryDetailDialog 用 JS 的 `rcaRenderResults` + QWebEngineView。这两套的**列集合、排序、空值显示**可能不一致——而 `gui_fluent.py:2827` 的注释承认："exporter.py AND a matching branch in js/table.js (the browser …)"，即两套必须手工保持同步。
- 这个事实**没有写进任何架构文档**（`docs/` 下 24 个文件全是 review/fix 报告，无架构文档）。下一个维护者不会知道"桌面的历史详情页其实是浏览器前端"。
- **`js/table.js` 的 4,654 行被桌面端依赖**这件事，没有任何测试断言。删掉 `js/table.js` 里的一个函数，浏览器端测试可能绿，但桌面端历史详情页静默白屏（它有 fallback 到 TreeView，见 `gui_fluent_history_detail.py:286`，所以**不会报错，只会变丑**）。

**改造方案**

1. **写一份 `docs/ARCHITECTURE.md`**（本评审的核心产出之一），把三端共存、`js/table.js` 双端共用、`rca_core` 的模块清单与依赖图写进去。
2. **加一条测试**（`tests/test_frontend_assets.py`）：断言 `js/table.js`、`js/aggregate.js`、`js/i18n.js` 存在且非空；断言 `gui_fluent_history_detail.py` 引用的 `_JS_TABLE` 路径能解析到真实文件；断言 `js/table.js` 中 `rcaRenderResults` 与 `rcaTableConfigs` 两个符号存在（防止改名后桌面端静默降级）。
3. **明确一个决策**：ExtractPage 是否也切到 JS 渲染？
   - **我的建议：不要切**。理由：ExtractPage 需要**可编辑**的表格（`gui_fluent.py:3095` `table.setItem(...)` 支持单元格编辑），而 `js/table.js` 是只读渲染器。强行统一会引入一个"可编辑的 JS 表格"，成本远大于收益。
   - 但**必须记录这个不对称**，并在 `docs/ARCHITECTURE.md` 里写明"两条渲染路径的契约边界"：Python 侧 `build_table_export` 负责 ExtractPage 与所有导出格式；JS 侧 `rcaRenderResults` 负责历史详情页与浏览器页。两者共用**同一份 `TABLE_CONFIGS` 定义**（`rca_core/exporter.py:1471` + `js/table.js` 的镜像），这是它们必须同步的真正契约。

**风险与验证**：风险极低（纯新增测试 + 文档）。验证门：新测试在 CI 主 job（无需 Qt）通过。

**工作量**：0.5 人日（测试）+ 0.5 人日（文档）。

---

### P3-13　`rca_core` 内部无分层，`quality.py` / `aggregate.py` / `exporter.py` 互相知道对方的私有实现

**代码证据**

- `rca_core/editable.py:31`：`from .exporter import COL_TYPES, _find_cfg, get_config` —— **导入了两个私有符号**（`_find_cfg`）
- `rca_core/exporter.py:31`：`from .bed_parser import parse_bed as _parse_bed_impl  # noqa: E402 (post-import hook)` —— 注释说 "post-import hook"，说明它是被刻意后置的
- `rca_core/quality.py:66`：`from .grounding import (...)` —— quality 依赖 grounding（名字接地），而 grounding 又依赖 `extractor`（`grounding.py:601` 延迟 import `extractor._add_row_flag`）
- `rca_core/report.py:56`：`from .quality import (...)`

**为什么是问题**：模块之间通过**私有符号**（`_find_cfg`、`_add_row_flag`）耦合。私有符号意味着"不承诺稳定"，但跨模块依赖它时它事实上就是 API。

**改造方案**

1. 把 `exporter._find_cfg` 提升为 `exporter.find_cfg`（公开），`editable.py` 改用它。**保留 `_find_cfg` 别名**以免破坏其他调用点（先 grep 确认只有 `editable.py` 用）。
2. `grounding.py:601` 的 `extractor._add_row_flag` —— 查清这个函数的作用后，要么提升为公开函数，要么把 `_add_row_flag` 的逻辑下沉到 `grounding` 自己的模块内（需读实现确认依赖方向）。
3. `exporter.py:31` 的 "post-import hook" 注释 —— 读清它为什么必须后置，若是循环依赖则记入技术债台账（P3-11 的 AST 测试会抓到）。

**风险与验证**：风险低。验证门：`pytest tests/ -q` 全绿。

**工作量**：0.5–1 人日。

---

## P2-14 ★三端特性不对称（差分 parity 结构上守不住的那一类）

**代码证据**（实测）

| 能力 | Python 核心 | 浏览器 | 服务端 | Tkinter | Fluent |
|---|---|---|---|---|---|
| GBIF 学名核验 `verify_names`/`name_issues` | `names.py:571,675` | ✅ `js/app.js:1178` | ❌ **零命中** | ❌ | ❌ |
| fuzzy 匹配 | 同上 | ✅ | ❌ | ❌ | ❌ |
| 模型名接地 `check_name_grounding` | `grounding.py:477` | ❌ | ✅ `server.py:539` | ❌ | ❌ |
| 结果缓存 `cache.make_key` | `cache.py:203` | ❌ | ✅ `server.py:3149,3449` | ❌ | ❌ |
| ICS 标准化年龄 | `standards/ics.py` | ✅ `js/ics_table.js` | ✅ | ❌ | ❌ |

证据命令：
- `grep -rn "verify_names\|name_issues" gui.py gui_fluent.py gui_fluent_pages.py gui_fluent_providers.py gui_fluent_history_detail.py server.py` → **零命中**
- `grep -rn "check_name_grounding" --include=*.py .` → 仅 `server.py:539` + `grounding.py` 自身 + 测试

**为什么是问题**

**差分 parity 守的是"同端同输入同输出"，守不住"新能力是否三端都落地"。** 上表每一行 ❌ 都是一个"评审时靠人眼发现"的缺口 —— 与产品侧已判定的 A5（fuzzy 只在 Web 端）、A6（ICS 表桌面端少 11 行）同根，而且**范围比已列出的更广**：两套"名字核验"能力各自只落在一端。

**改造方案：把「能力 → 已落地端」变成可枚举数据 + CI 断言**

1. 新建 `rca_core/capability_matrix.py`（**数据 only、零 sibling import**，遵循 `modes.py` 已验证的模式）：
   ```python
   CAPABILITY_TIERS = {
       "names.verify": {"core": True, "browser": True, "server": False, "tk": False, "fluent": False},
       "names.fuzzy":  {"core": True, "browser": True, "server": False, "tk": False, "fluent": False},
       "grounding":    {"core": True, "browser": False, "server": True, "tk": False, "fluent": False},
       "ics.export":   {"core": True, "browser": True, "server": True, "tk": False, "fluent": False},
   }
   ```
2. **CI 断言**：对每条 `core=True` 的能力，若某端未落地，该端**必须有一条带原因的显式豁免**，否则 CI 失败。
3. **不要求三端都做**（不现实 —— 桌面端不需要浏览器直连，浏览器不需要服务端审计落库），而是要求**"没做"必须是一行显式声明**，而不是一个沉默的缺口。

**风险与验证**：风险低（纯新增数据 + 测试）。验证门：① 新测试在**当前代码上应当失败**（存在未声明的不对称）→ 逐条补声明或补实现 → 转绿；② 断言须能区分"有意不支持"与"漏了"。

**工作量**：1.5 人日（含 capability matrix 设计）。**一次性投入，之后每加一个能力只加一行。**

---

## 3. 被否决的备选方案

### 3.1 ❌ 否决：把三端合并成一个

**否决理由**：这是伪命题。三端不是同一件事的三种实现，而是**三种交付形态**，且它们各自的"外壳"不可替代：

| 端 | 外壳 | 不可替代性 |
|---|---|---|
| Web (`server.py` + `js/`) | `http.server` + 原生 JS | 唯一能在无 Python 环境下运行的形态；也是 `proxy/`（Cloudflare Worker / Deno）唯一能对接的形态 |
| Fluent 桌面 (`gui_fluent.py`) | PySide6 + qfluentwidgets | 需要 QWebEngineView 渲染 phylo 树（`gui_fluent.py:929 PhyloTreeWidget`）与历史详情页 |
| Tkinter 桌面 (`gui.py`) | stdlib `tkinter` | **零第三方依赖**——`main.py:80 launch_gui` 的存在意义就是"Python 没装 PySide6 也能用" |

**关键代价**：合并意味着要么牺牲无依赖回退（删 `gui.py`），要么做"同一个 UI 三套渲染后端"（成本更高）。而且 `gui.py` 与 `gui_fluent.py` 的**扇出能力已经不同**（P2-7：Tkinter 无取消、无进度中间态）——这正是"同一个产品三份外壳"在分化的证据。

**但是**——**"合并"有一个被误解的形式，它其实是对的**：合并**核心逻辑**，不合并**外壳**。这正是 `rca_core` 已经在做的事，只是做得不够彻底（见 P1-4/P1-5/P2-7）。**本方案的方向就是"继续合并逻辑，不动外壳"。**

**部分否决**：`gui.py`（Tkinter）**建议保留但降级为"最小可用回退"**，不再新增功能。目前 `gui.py` 2,973 行里与 Fluent 重叠的部分（提供商管理、结果表格、多轮扇出）是纯维护负担。**建议**：冻结 `gui.py` 的功能面（只修 bug，不加功能），并在 README 标注"legacy fallback"。**不做删除**——删掉会破坏"零依赖可用"这条产品承诺。

---

### 3.2 ❌ 否决：把 `rca_core` 拆成多个 pip 包

**否决理由**

1. **没有第二个消费者**。拆包的全部意义是"让不同项目复用不同部分"，而本项目只有一个消费者（自己）。拆了只会多出包间版本约束。
2. **会加剧而非缓解 parity 问题**。当前 `rca_core` 是一个目录，`js/` 镜像它的哪些模块一目了然；拆成 `rca-core-extract` / `rca-core-export` / `rca-core-llm` 之后，**JS 镜像要对应哪个包变成开放问题**，且发布/安装矩阵（`requirements.txt` + `RangeChartAnalyzer.spec` PyInstaller 配置 + `ci.yml` 的 4 个 job）都要改。
3. **CI 成本**。`ci.yml` 现在跑 4 个 job（matrix py3.10/3.12 + qt + repo-health）。多包意味着多版本矩阵。
4. **PyInstaller 打包**：`RangeChartAnalyzer.spec`（19KB）已经因为体积做过 3 轮优化（`dd50164 Release 0.1.4: 49.8 MiB less`、`3d4bb1c Take the Python side's ballast out too`、`aef8c2f Cut the one-file exe by 19.7%`）。拆包会让 PyInstaller 的 hidden-import 分析复杂化，而收益为零。

**替代方案（采用）**：保持单包，但**在包内做逻辑分层**（P1-5 的 `rca_core/modes_impl/`、`rca_core/transport.py`、`rca_core/fanout.py`、`rca_core/shape.py`）。分层靠**目录与 import 方向**表达，不靠**打包边界**表达。

---

### 3.3 ❌ 否决：用 monorepo 工具统一 JS/Python（如 Nx / Turborepo / Bazel）

**否决理由**

1. **两个生态之间没有共享的构建产物**。JS 侧无构建步骤（`index.html` 直接 `<script defer src="js/xxx.js">`，无 npm 构建、无 bundler、无 TypeScript 编译）。引入 monorepo 工具后，JS 侧仍然没有可编排的任务——**工具解决的是不存在的问题**。
2. **真正的痛点不是"工具缺失"，而是"决策重复"**。`js/minimax.js:770` 的注释说得很清楚：复制是因为"aggregate.js and minimax.js each have to run standalone (**tests_aggregate.js loads aggregate.js alone**)"。**痛点是测试加载方式**，不是缺少 monorepo。
3. **成本**：`node_modules/` 已经存在（说明有 JS 依赖），引入 Turborepo 要重写 CI 的 Node 部分，而 `.github/workflows/ci.yml` 的 Node job 现在只用 `node tests/js/tests_*.js` 逐个跑——**14 行 bash 就能覆盖，加了工具反而更重**。

**替代方案（采用）**：改**测试加载方式**而非引入工具。P0-1/P2-9 的做法是——把共享的 Python 语义实现放到一个**无依赖的独立文件**（`js/pyfloat.js`），让所有需要它的测试都能加载它。同时 `tests/js/tests_diff_frontend_parity.js` 已经展示了"用 `vm` 上下文加载多个脚本"的成熟手法（见其 `SCRIPTS` 列表 + `buildContext()`）。

---

### 3.4 ❌ 否决：插件化 / 事件总线 / 微服务化

**否决理由**

1. **插件化的前提是"扩展点被明确定义"**。本仓最接近插件化的是 `rca_core/protocols.py`（"一种 API 格式一个适配器"），而它的边界是**反的**（P1-4：适配器抓传输层的 14 个私有符号）。**先把已有扩展点修对（单向依赖 + 依赖注入），再谈插件化。**
2. **事件总线解决的是"解耦发布者与订阅者"**。本仓没有多订阅者场景——`usage` 聚合是单消费者，`quality` 是单消费者。引入总线只会把直接调用变成间接调用，**增加调试难度，收益为零**。
3. **微服务化完全不可行**：这是个单机科研工具，`rca_core` 的 `db`/`cache`/`history` 全部是 SQLite 文件（`rca_core/history.py` → `rca_core/db.py`），`ProviderStore` 是本地 JSON（`rca_core/llm.py:1482`），甚至 API Key 的加密存储也是本地 keyring（`rca_core/secrets_store.py`）。**没有任何跨进程边界的价值。**
4. **"审查—修复"循环的根因不是架构层级不够，而是"同一决策多处实现 + 缺少机器护栏"**（本仓自己的注释反复指向这一点：`tests/js/tests_diff_frontend_parity.js:35` 的 "ONE decision, SEVERAL implementations"；`tests/js/tests_contrast.js` 的 "a new suite landed at the root and nothing executed it"）。**加总线不加护栏，只会让同一个 bug 藏得更深。**

---

### 3.5 ❌ 否决：把 `js/` 的逻辑整体移到 TypeScript + 编译产物，或反过来把 Python 端"编译"成 JS（WASM）

**否决理由**

1. **TS 化**：会让 `index.html` 从"直接加载源码"变成"加载构建产物"，引入构建步骤 → 破坏"克隆即可运行"的特性（这是开源科研工具的核心 UX，`main.py` 一键启动、`proxy/README.md` 教用户粘贴源码）。而且**不会消除 parity 问题**，只会把它从"两份源码"变成"两份源码 + 一个构建步骤"。
2. **WASM**：把 Python 编译到 WASM 运行在浏览器里，技术上可行（Pyodide），但：(a) 包体积从当前的几 MB 变成几十 MB；(b) `opencv`/`numpy` 的 WASM 构建是硬骨头；(c) **`rca_core` 大量依赖 Python 动态特性**（`getattr` 驱动的模式分发、`modes.py` 的字符串 import path 延迟解析）；(d) 最致命的是——**这个工具的模型调用是发给远端 API 的，浏览器直接调 API 已经能工作**（`js/minimax.js`），WASM 带来的唯一增量是"在浏览器里跑归一化"，而那部分 JS 已经有了。

---

## 4. 分阶段改造路线图

### 关键路径

```
P1（护栏与低成本收敛）
 ├─ P2-7 fan-out 合并 ──┐
 ├─ P2-8 i18n 职责澄清 ─┤
 ├─ P2-9 + P0-1 JS 浮点统一 ─┤   ← 关键路径起点
 ├─ P3-10 JS 重复函数 ──┤
 ├─ P3-11 解环 + 环检测 ─┤
 └─ P0-2 缓存键收敛 ────┤
        ↓
P2（结构收敛）
 ├─ P1-3 shape.py 探测器统一
 ├─ P3-13 私有符号公开化
 └─ P1-4 llm/protocols 单向化
        ↓
P3（上帝模块切割，最贵，最后做）
 ├─ P1-5 extractor.py 拆分
 └─ P1-6 server.py pipeline 下沉
```

**关键路径**：P0-1 → P0-2 → P1-3 → P1-4 → P1-5 → P1-6。
**为什么这个顺序**：P1-4 和 P1-5 都要动 `rca_core` 的 import 图，**必须先有 P3-11 的环检测护栏**，否则重构过程中可能引入新环而无人察觉。而 P1-6 依赖 P0-2 的 `build_extraction_key` 已存在。

---

### 阶段 0：建立护栏（不做任何行为改动）

**目标**：让后续所有重构都有机器可查的"没变坏"证据。

| # | 动作 | 验证门 | 工作量 |
|---|---|---|---|
| 0.1 | 新增 `tests/test_rca_core_acyclic.py`：AST 构建 `rca_core` 依赖图，断言无环 | **该测试在当前代码上必须失败**（抓到 `llm↔protocols`、`extractor↔matrix`）；不失败说明测试写错了 | 0.5d |
| 0.2 | 新增 `tests/test_frontend_assets.py`：断言 `js/table.js`/`aggregate.js`/`i18n.js` 存在且 `rcaRenderResults`/`rcaTableConfigs` 符号存在 | 在主 CI job（无 Qt）通过 | 0.5d |
| 0.3 | 新增 `docs/ARCHITECTURE.md`：三端共存图 + `js/table.js` 双端共用 + `rca_core` 模块清单 | 人可读；至少一人确认无误导 | 0.5d |
| 0.4 | 记录基线：`git stash` 前先跑一遍 `pytest tests/ -q` + `node tests/js/tests_diff_frontend_parity.js`，把两个数字写进 PR 描述模板 | 基线数字 = 4360 passed / 137 skipped / 1363 matched 0 diverged | 0.5d |

**阶段出口条件**：`pytest tests/ -q` 仍 4,360 通过；新增测试按预期（0.1 失败、0.2 通过）。

---

### 阶段 1：低成本高收益收敛（全部为"搬代码，不改行为"）

| # | 动作 | 对应问题 | 验证门 | 工作量 |
|---|---|---|---|---|
| 1.1 | 建 `js/pyfloat.js`，统一三份 Python 数值语义；`aggregate.js` 改为对齐 Python | P0-1, P2-9 | 差分套件 `0 diverged`；新增 group `py_float` 覆盖 `'１２７'`/`'٥٠٠'`/`'1_2e3_4'`；新增"`rcaPyFloat` ≡ `rcaPyFloatOrNull`"断言 | 1d |
| 1.2 | 新增 `rca_core/cache.py::build_extraction_key` + `CACHE_KEY_FIELDS`；3 处调用点改用它 | P0-2 | `pytest` 全绿；新增 AST 断言"所有 `make_key` 调用点字段集 ⊆ `CACHE_KEY_FIELDS` 且 server.py 两处相同"；端到端缓存命中验证 | 1d |
| 1.3 | 建 `rca_core/fanout.py`，取 `gui_fluent.py:333` 版本；两个 GUI 改为 re-export（**保留旧名**） | P2-7 | `tests/test_gui_sprint_b.py` **一行不改**通过；`tests/standalone/tests_gui_fluent.py` 全绿 | 1d |
| 1.4 | 改 `rca_core/i18n.py` docstring；把 `_col_label`/`_title_label` 的 fallback 规则抽到 `i18n.py` | P2-8 | `pytest` 全绿；`node tests/js/tests_contrast.js` 全绿 | 0.5d |
| 1.5 | `js/quality.js`/`js/table.js` 的 105 行重复函数提到共享层（旧名保留为别名） | P3-10 | `tests_quality_frontend.js` + `tests_export_parity.js` + 差分套件全绿 | 0.5d |
| 1.6 | 解 `extractor ↔ matrix`：`extraction_wall_clock_budget_sec` 搬到 `rca_core/budget.py` | P3-11 | 阶段 0.1 的环检测测试**少抓一个环**；`pytest` 全绿 | 0.5d |
| 1.7 | 删死代码 `js/minimax.js:2343 rcaNormalizeResultOldDupA` | P2-9 | 差分套件全绿 | 0.1d |

**阶段出口条件**：`pytest` 4,360 全绿；差分套件 `1363 matched / 0 diverged`；**`tests/fixtures/frontend_parity_2026_09_20.json` 重新生成后 `git diff` 为空**（这条最强：它同时证明 Python 行为未变且 parity 契约未被偷改）。

**总工作量：约 5.5 人日**。

---

### 阶段 2：结构收敛（模式探测器统一 + 依赖单向化）

| # | 动作 | 对应问题 | 验证门 | 工作量 |
|---|---|---|---|---|
| 2.1 | 建 `rca_core/shape.py`（零依赖），6 个探测器唯一化；`aggregate.py`/`exporter.py` 改用它 | P1-3 | `tests/test_detector_module_parity.py` 扩展到 6 探测器 × 2 模块；**金标导出产物 `git diff` 为空**；`tests_export_parity.js` 全绿 | 1.5d |
| 2.2 | `aggregate.py` 补 `zonation`/`paleomap` 自动探测，或在 `modes.py` 标 `auto_detect=False` 并让 `merge_results` 对未探测模式抛错 | P1-3 | 新增测试：zonation payload 走 `merge_results(schema=None)` 不再落到 range_chart | 0.5d |
| 2.3 | `exporter._find_cfg` → 公开 `find_cfg`（保留别名）；`grounding → extractor._add_row_flag` 解除 | P3-13 | `pytest` 全绿 | 1d |
| 2.4 | **第一步**：`rca_core/transport.py` 承载 12 个传输层私有函数；`llm.py` 原样转发；`protocols.py` 暂不动 | P1-4 | `pytest` 全绿；`python -X importtime` 无异常 | 1.5d |
| 2.5 | **第二步**：`protocols.py` 改依赖 `transport.py`（+ `ssrf.py`），切断 `protocols → llm`；`llm.py → protocols` 单向 | P1-4 | 阶段 0.1 的环检测测试**全绿**（无环）；`pytest` 全绿；手工跑 `test_llm_connection` 一次真实 provider | 1.5d |
| 2.6 | 新增 `js/` ↔ Python 探测器对照断言（6 个探测器） | P1-3 | `tests_diff_frontend_parity.js` 新增 group，全绿 | 0.5d |

**阶段出口条件**：`pytest` 全绿；**环检测测试全绿（`rca_core` 内零环）**；金标产物不变；差分套件 0 diverged。

**总工作量：约 6.5 人日**。

---

### 阶段 3：上帝模块切割（最高价值，最贵，最后做）

| # | 动作 | 对应问题 | 验证门 | 工作量 |
|---|---|---|---|---|
| 3.1 | `extractor.py` 的 `image_hash`/`cache` 模块级 import 改延迟 | P1-5 阶段1 | `pytest` 全绿；`importtime` 变小 | 0.5d |
| 3.2 | 建 `rca_core/modes_impl/`，按模式切 11 个 `normalize_*`；`extractor.py` 保留 re-export | P1-5 阶段2 | ① `extractor` 公开名字**一个不少**（新增断言）；② `pytest` 全绿；③ **parity fixture 重新生成后 `git diff` 为空**；④ 差分套件 0 diverged | 3–4d |
| 3.3 | 建 `rca_core/pipeline.py`，承载服务端编排；**新旧并行实现，差分验证** | P1-6 | 20 组请求参数下，新旧实现输出 JSON **逐字节相同**（`sort_keys=True` 比较） | 3–4d |
| 3.4 | `server.py::_handle_extract_body` 切到 `pipeline`，缩到 ≤120 行 | P1-6 | `pytest` 全绿；`tests/standalone/tests_server_hardening.py` 全绿；手工验证 CSRF 拒绝/缓存命中/取消三条路径 | 1d |

**阶段出口条件**：`pytest` 4,360 全绿；差分套件 0 diverged；parity fixture 无 diff；`server.py` 中 `_handle_extract_body` ≤120 行。

**总工作量：约 8.5–9.5 人日**。

---

### 4. 总体估算

| 阶段 | 工作量 | 风险 | 累计 |
|---|---|---|---|
| 阶段 0 护栏 | 2 人日 | 无 | 2d |
| 阶段 1 低风险收敛 | 5.5 人日 | 低 | 7.5d |
| 阶段 2 结构收敛 | 6.5 人日 | 中 | 14d |
| 阶段 3 上帝模块切割 | 8.5–9.5 人日 | 中高 | **23–24 人日** |

**建议**：阶段 0 + 阶段 1 是一次性可交付的 PR 组（约 7.5 人日 ≈ 1.5 周单人），**收益/风险比最高**。阶段 2、3 按迭代节奏分批。

**如果只能做一件事**：做 **P0-1**（JS 浮点语义统一）——它是**已实测确认的活跃 divergence**，且 1 人日内可完成。

---

## 5. 明确裁决：三端架构该不该合并？

### 裁决：**不应该合并"三端"；应该合并的是"三端各自的逻辑"，且尚未合并彻底。**

分三层说清楚：

**第一层：外壳不合并。** 理由已在 §3.1 展开——三种交付形态（无依赖浏览器 / PySide6 桌面 / 零依赖 Tkinter 回退）各有不可替代的产品承诺。合并外壳 = 牺牲一条产品承诺，收益是"代码变少"，但代价是"用户少一条路"。**否决。**

**第二层：逻辑应当合并，且本仓已经在做，方向正确。** `rca_core` 的存在证明这个方向是对的：11 种模式的归一化、质量评分、导出、合并、供应商配置**只有一份**。真正的问题是这份"一份"还没有彻底收口：

| 未收口的部分 | 现状 | 应收口到 |
|---|---|---|
| 模式探测器 | 3 份（aggregate/exporter/js-table） | `rca_core/shape.py` |
| 数值语义 | 3 份 JS + 若干 Python | `js/pyfloat.js` + `rca_core` 单一实现 |
| 扇出编排 | 3 份（2 GUI + server） | `rca_core/fanout.py` + `rca_core/pipeline.py` |
| 缓存键 | 3 份手写 | `rca_core/cache.py::build_extraction_key` |
| 传输层 | `llm` ↔ `protocols` 互抓私有符号 | `rca_core/transport.py`（单向） |
| 模式归一化 | `extractor.py` 一个文件 11 个函数 | `rca_core/modes_impl/`（仍单实现，只拆文件） |
| 翻译目录 | `rca_core/i18n.py` + `js/i18n.js` 两份 | **保持两份**（跨语言无法共享代码），用差分测试锁 |

**第三层：`js/` 与 `rca_core/` 的"双实现"不能消除，只能锁住。** 这是本仓最本质的约束：**跨语言没有共享运行时**。可选方案只有三条：

| 方案 | 代价 | 判断 |
|---|---|---|
| (a) Python 编译到 WASM 跑在浏览器 | 包体几十 MB；`cv2`/`numpy` 的 WASM 构建；`modes.py` 的动态分发要重写 | **否决**（§3.5） |
| (b) 全部逻辑放服务端，浏览器只做 UI | 破坏"浏览器直连 API"（`js/minimax.js`）；且 `proxy/` 的存在说明直连是设计的一部分 | **否决** |
| (c) **保留双实现 + 机器锁住** | 需要持续维护差分套件 | **✅ 采用，且已经在做** |

关于 (c)，要给出两点肯定与两点批评：

- **肯定 1**：差分套件是本仓最好的工程实践。`tests/js/tests_diff_frontend_parity.js` 的 1,370 个 fixture + `tests/gen_frontend_parity_fixtures.py`（2,829 行）把"人读两个文件"变成了"机器跑两遍"。`tests_diff_frontend_parity.js:35` 那段关于 "ONE decision, SEVERAL implementations" 的注释，是我对这个仓最深的敬意。
- **肯定 2**：同形状的 bug 已被系统性地发现并修复（21 处 `AUDIT-2026-10-02` 标记，跨 `rca_core` 与 `js/`）。修复速度说明反馈回路是通的。
- **批评 1**：**差分套件有覆盖盲区，且盲区恰好在活跃路径上**。11 个 `normalize_*` 中，**`normalize_paleomap_result` / `normalize_chemical_stratigraphy_result` / `normalize_scatter_plot_result` / `normalize_occurrence_matrix_result` / `normalize_generic_table_result` 5 个不在差分套件里**（实测）。这 5 个的 `browser_supported=False`（`modes.py`），所以**当前不构成风险**——但 `occurrence_matrix` 的 `server_supported=True` 且是 2026-10-15 新增的模式，它的浏览器拒绝逻辑（`js/minimax.js:3799 RCA_BROWSER_UNSUPPORTED_MODES`）与 Python 侧的差异**没有任何差分测试守着**。
- **批评 2**：**差分套件测的是"函数对函数"，不是"决策对决策"**。`tests_diff_frontend_parity.js:56` 的注释自己承认："When adding a case here, ask which OTHER files make the same decision and assert they agree -- a per-consumer pin records today's answer and lets the next one drift." 这条规则写在注释里，但**没有工具强制**。P0-1 的 `js/aggregate.js` 浮点 divergence 之所以能活下来，正是因为差分套件里 `aggregate` 组的 976 个用例没有一个覆盖全角数字。

**因此，架构收口的正确结论是：**

> **不合并三端。合并逻辑，且用"单一 Python 实现 + 差分测试锁 JS 镜像"的模式一直做下去。**
> **把"人读两个文件"换成"机器读两个文件"这件事，已经做了；缺的是把"决策点的清单"也机器化——即给每个"同一决策多处实现"的形状建一个可枚举的清单，而不是靠下一个审计轮次偶然发现。**

**具体落地**：阶段 0.1 的 AST 环检测 + 阶段 1.1 的浮点统一 + 阶段 2.6 的探测器对照，都是"把决策点清单机器化"的起步。**更进一步建议（但不在本次改造范围）**：建一个 `docs/DECISIONS.md`，逐条登记"哪个决策在几个地方做"（浮点语义、模式探测、排序、名称清洗、年龄边界、i18n fallback…），每条标注差分覆盖状态。这份清单就是下一轮审计的检查表——**本仓历史上每一轮审计发现问题，都是因为有人手工维护了这样一份清单**（`docs/` 下 24 个 review 报告就是它的物化形式）。把它从文档变成数据，才是真正的架构收口。

---

## 6. 架构风险与待确认

### 6.1 明确**不建议做**的事（风险 > 收益）

| 方案 | 判定 | 理由 |
|---|---|---|
| 三端合并成一个 | ❌ 不建议 | 牺牲"零依赖回退"与"浏览器直连"两条产品承诺 |
| 拆多个 pip 包 | ❌ 不建议 | 无第二消费者；加剧 parity 难题；影响 PyInstaller 打包 |
| monorepo 工具（Nx/Turborepo） | ❌ 不建议 | JS 侧无构建步骤，工具解决不存在的问题 |
| 插件化 / 事件总线 | ❌ 不建议 | 已有扩展点（protocols）边界是反的，应先修而非加层 |
| 微服务化 | ❌ 不建议 | 全本地 SQLite/JSON，无跨进程边界价值 |
| TypeScript 化 / WASM 化 | ❌ 不建议 | 破坏"克隆即运行"；不消除双实现 |
| **删除 `gui.py`（Tkinter）** | ❌ 不建议 | 破坏"零第三方依赖可用"承诺。**建议改为：冻结功能面（只修 bug 不加功能）+ README 标注 legacy fallback** |
| **阶段 3 的全部内容一次做完** | ❌ 不建议 | 一次性动 7,055 行 extractor + 995 行 handler，风险远超收益。**必须按阶段 0→1→2→3 分批，每批独立可发布** |

### 6.2 本评审的固有局限（诚实标注）

| 局限 | 说明 |
|---|---|
| 未运行测试套件 | 本次评审为**只读**评审（委托明确要求"只读"）。所有"全绿"表述均指**改造后应达到的验证门**，非本次实测结果。基线数字（4,360 / 137 skipped / 1,363 matched）取自委托方说明 |
| 未运行真实 API 调用 | `llm.py` / `protocols.py` 的重构（P1-4）风险评估基于代码阅读，实际供应商行为需手工验证 |
| 未量化 Python↔JS 重复的总规模 | 本评审量化了**已定位**的重复点（P0-1、P1-3、P2-7、P2-8、P2-9、P3-10）。**全仓重复总量未系统统计**——需要一个跨语言语义比对工具（超出本次范围） |
| `exporter.py` / `quality.py` 的内部拆分未评估 | 本次只评估了 `extractor.py` / `server.py` / `llm.py` 三个上帝模块。`exporter.py`（3,640 行、70 函数、5 种导出格式）与 `quality.py`（1,765 行）可能同样可切，**但未做逐函数分析** |
| 打包体积影响未评估 | `RangeChartAnalyzer.spec` 有 3 轮减重历史。任何新增模块/文件都可能影响 PyInstaller 产物体积，**建议每次改造后跑一次体积对比** |
| `docs/` 下 24 份历史 review 报告未逐份通读 | 仅抽样读了 3–4 份。可能存在本评审未覆盖的历史决策与已知问题 |

### 6.3 【待确认】项

1. **`gui.py` 的产品定位**——是继续作为一等公民，还是明确降级为"最小回退"？这决定 P2-7 之后的投入方向，也决定 `gui.py` 2,973 行是否逐步冻结。**建议由产品侧拍板。**
2. **`proxy/`（Cloudflare Worker + Deno proxy，44KB JS）的维护承诺**——它是仓外部署物，与 `js/minimax.js` 的直连逻辑有接口耦合（`js/minimax.js:396-410` 解析自己的 `<script src>` 推导代理 URL）。这两份代理代码**无 CI 覆盖**（`ci.yml` 的 ruff 检查显式 `--exclude proxy`）。是否纳入维护范围？【待确认】
3. **`js/table.js` 4,654 行的双重身份是否长期保留**——当前被浏览器端与桌面端历史详情页共用（§2 P3-12），但 ExtractPage 走 Python 路径。这个不对称是**有意的**（可编辑表格 vs 只读渲染），还是有意的技术债遗留？**建议明确记录为契约而非债。**【待确认】
4. **`rca_core/i18n.py` 的三语目录与 `js/i18n.js` 的 3,163 行文案**是否应改为单一数据源（如 JSON 文件，两端各读）？这能消除一份重复，但会让 JS 需要 fetch 一个 JSON、或让 Python 需要读 `js/i18n.js` 的源码文本（`tests/js/tests_contrast.js:596` 现在就在用源码文本切片的方式读 `.py`，这个手法可以推广）。**这是个未评估的选项**，可能比 P0-1 更高收益。**建议作为阶段 2 的候选项单独评估。**【待确认】
5. **`tests/standalone/` 的 17 个脚本式套件**（pytest 不收集，靠退出码断言）是否要迁入 pytest？CI 已经为它们做了 `set -e` 循环（`ci.yml`），迁移收益主要是诊断质量而非覆盖度。**优先级低，不建议在本轮做。**【待确认】

---

## 附录：本次评审使用的实测方法

```bash
# 依赖图（AST，含函数内延迟 import）
python -c "import ast,os; ... # 见 §1.3 输出"

# 精确重复函数体检测
python -c "import ast,hashlib; ... # rca_core + js/"

# make_key 调用点字段集
python -c "import ast; ... # 见 §2 P0-2 表"

# JS 浮点 divergence（用仓内真实文件，vm 加载后调用）
node -e "const fs=require('fs'),vm=require('vm'); vm.runInContext(fs.readFileSync('js/aggregate.js','utf8'),ctx); console.log(ctx.rcaPyFloat('１２７'))"
#   → null      （Python float('１２７') = 127.0；js/minimax.js 同一输入 = 127）

# 循环依赖检测
#   rca_core 内：llm ↔ protocols、extractor ↔ matrix
```

**所有引用的行号均可在对应提交（`b0acebf`）上复核。**
