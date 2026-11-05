# Range Chart Analyzer — 架构说明

> 建档：2026-11-05（ARCH-REVIEW-2026-11-02 评审轮）。
> 本文描述**代码现在的样子**，不是理想中的样子。所有数字可用文末命令复现。
> 完整评审与改造路线图见 `docs/ARCH-REVIEW-2026-11-02.md`；缺口与验收判据见
> `docs/GAP-ANALYSIS-2026-11-02.md`、`docs/ACCEPTANCE-REVIEW-2026-11-02.md`。

## 1. 一句话

把地层延限图图片交给多模态大模型，抽出可核对的结构化数据，人工在表格里复核后
导出 CSV / XLSX / PBDB / Darwin Core。**一套 Python 核心 + 三种交付外壳 + 一套
浏览器端的 JS 对等实现**，单机、本地数据、无服务端账号体系。

## 2. 三种外壳（不合并，各有不可替代的产品承诺）

| 外壳 | 入口 | 技术栈 | 为什么不能删 |
|---|---|---|---|
| Web | `server.py` (4,031 行) + `js/` | stdlib `http.server` + 原生 JS（**无构建步骤**） | 唯一能在无 Python 环境运行的形态；`proxy/`（Cloudflare Worker / Deno）只对接它 |
| Fluent 桌面 | `gui_fluent.py` (5,249 行) 及 5 个 `gui_fluent_*.py` | PySide6 + qfluentwidgets + QWebEngineView | phylo 树、历史详情页需要 QWebEngineView；`main.py` 默认启动它 |
| Tkinter 桌面 | `gui.py` (2,973 行) | **纯 stdlib tkinter** | 「没装 PySide6 也能用」的零依赖回退 |

`main.py`（306 行）是分发器。`app.py` 是 PyWebView 原生窗口壳，包在 Web 形态外面。
`proxy/` 两个文件是仓外部署物，只转发请求。

**裁决（ARCH-REVIEW-2026-11-02）**：不合并外壳。三端能力**已经在分化**
（Tkinter 端无取消、无进度中间态、无 GBIF 核验、无 ICS 导出），建议把它
**冻结为最小可用回退**（只修 bug 不加功能），并在 README 标注 legacy fallback。
要合并的是**逻辑**，不是**外壳**。

## 3. 分层与依赖方向

```
index.html / js/*.js  ←→  server.py  ←┐
gui.py (Tkinter)                    ├─→  rca_core/ (46 模块)
gui_fluent.py + QWebEngineView  ←──┘         ▲
        │                                     │
        └── 内联 js/table.js ──────────────────┘ (渲染契约，非 Python 依赖)
```

- **入口层 → 核心层是单向的**：`server.py` 引入 12 个 `rca_core` 模块，
  `gui_fluent.py` 10 个，`gui.py` 9 个。核心层**零** tkinter / PySide6 /
  http.server 依赖（由 `tests/test_rca_core_acyclic.py` 守住）。
- **`rca_core` 内部是平铺的工具箱，不是分层架构**：AST 实测 **54 条依赖边**
  （32 模块级 + 26 函数内延迟 import），其中 **2 个循环**：
  - `llm ↔ protocols`（17 处双向引用；适配器向下抓传输层的 14 个私有符号）
  - `extractor ↔ matrix`（`matrix.py:308` 取 `extraction_wall_clock_budget_sec`）
  两者都用函数内延迟 import 规避 import-time 死锁，**运行时安全但模块被焊死**。
  由 `tests/test_rca_core_acyclic.py` 以「已知环棘轮」方式守住：新增环立即失败，
  解掉一个环必须同步缩小 `KNOWN_CYCLES`。
- **`rca_core/__init__.py`** 是公开 API 门面（`from .exporter import ...` 等）。
  `tests/gen_frontend_parity_fixtures.py` 直接 import 9 个 `normalize_*`，
  所以任何拆分**必须保留 re-export**。

## 4. ⚠️ `js/table.js` 被两个前端共用（本仓最好的决策，以前没人写下来）

`gui_fluent_history_detail.py:69-71` 读取 `js/table.js` + `js/aggregate.js` +
`js/i18n.js`，内联进 `<script>`，在 QWebEngineView 里调 `rcaRenderResults`（`:239`）
画历史详情页。因此**桌面端的历史详情页其实是浏览器前端**。

同一个桌面应用里还有第二条渲染路径：`gui_fluent.py` 的 ExtractPage 用 Python 的
`build_table_export()` + `QTableWidget`（`:3426`），因为它是**可编辑**表格，而
`js/table.js` 是只读渲染器。这个不对称**是有意的，不是债**：

| 消费者 | 渲染器 | 可编辑 |
|---|---|---|
| ExtractPage（桌面）、所有导出格式 | Python `build_table_export` | ✅ |
| 历史详情页（桌面）、Extract 页（浏览器） | JS `rcaRenderResults` | ❌ |

两者共用**同一份 `TABLE_CONFIGS` 定义**（`rca_core/exporter.py` + `js/table.js` 镜像）
—— 这才是它们真正必须同步的契约。**不要试图把 ExtractPage 也切成 JS 渲染**，
那需要凭空造一个可编辑的 JS 表格。

这个依赖以前没有文档也没有测试：删掉 `js/table.js` 里一个函数，浏览器测试可能全绿，
而桌面历史详情页会**静默降级**到 TreeView（`:286`，不报错、只是变丑）。
现在由 `tests/test_frontend_assets.py` 守住。

## 5. 双实现与差分锁（本仓最本质的约束）

浏览器端 `js/` 是 Python 核心的**对等实现**（跨语言没有共享运行时）：

| Python | 行数 | JS | 行数 |
|---|---:|---|---:|
| `rca_core/prompt.py` | 1,592 | `js/prompt.js` | 1,081 |
| `rca_core/json_utils.py` | 886 | `js/json-utils.js` | 742 |
| `rca_core/aggregate.py` | 1,786 | `js/aggregate.js` | 1,532 |
| `rca_core/quality.py` | 1,765 | `js/quality.js` | 1,947 |
| `rca_core/reason_codes.py` | 696 | `js/reason-codes.js` | 852 |
| `rca_core/bed_parser.py` | 467 | `js/bed_parser.js` | 229 |
| `rca_core/history.py` | 1,069 | `js/history.js` | 654 |
| **合计** | **8,261** | | **7,037** |

**15,298 行孪生代码 = JS 全量（24,005 行）的 64%**。改 Python 必须同步改 JS。

三条路，只走了第三条：
- ❌ Python 编译到 WASM 跑浏览器 —— 体积几十 MB，`cv2`/`numpy` 的 WASM 构建，
  `modes.py` 的动态分发要重写
- ❌ 全放服务端、浏览器只做 UI —— 破坏「浏览器直连 API」，`proxy/` 的存在证明直连是设计
- ✅ **保留双实现 + 机器锁住** —— `tests/js/tests_diff_frontend_parity.js`
  （1,363 matched / 0 diverged）+ `tests/gen_frontend_parity_fixtures.py`（2,829 行）

**差分套件的已知盲区**：它测「函数对函数」，不测「决策对决策」。
`js/aggregate.js` 的 `rcaPyFloat` 与 Python 的全角数字语义分叉活了下来，
正因为 `aggregate` 组的 976 个用例没有一个覆盖全角数字。给每个
「同一决策多处实现」建可枚举清单，是下一轮架构收口的重点。

## 6. 模式注册表：本仓最健康的设计（改造模板）

`rca_core/modes.py`（459 行）是**唯一声明源**：10 个 `ModeSpec` × 4 个能力维度
（`server_supported` / `browser_supported` / `export_supported` / `merge_wired`）
+ `maturity` 三档（stable / candidate / assistant）。它的 docstring 记录了为什么存在：
模式列表曾经写在 **9 个地方**，且「大多数遗漏不会报错」——
`SCHEMA_BY_MODE.get(mode, RANGE_CHART_SCHEMA)` 会静默套用错 schema，产出
「看似合理但确实错误」的结果。

| mode | maturity | srv | brw | exp | mrg |
|---|---|:-:|:-:|:-:|:-:|
| range_chart | stable | ✅ | ✅ | ✅ | ✅ |
| columnar_section | stable | ✅ | ✅ | ✅ | ✅ |
| abundance_diagram | candidate | ✅ | ✅ | ✅ | ✅ |
| phylogenetic_tree | candidate | ✅ | ✅ | ✅ | ✅ |
| zonation_chart | candidate | ✅ | ✅ | ✅ | ✅ |
| chemical_stratigraphy | assistant | ❌ | ❌ | ❌ | ❌ |
| paleomap | assistant | ❌ | ❌ | ✅ | ❌ |
| scatter_plot | assistant | ❌ | ❌ | ❌ | ❌ |
| occurrence_matrix | assistant | ✅ | ❌ | ✅ | ✅ |
| generic_table | assistant | ❌ | ❌ | ✅ | ❌ |

**三端模式覆盖「不一致」是设计意图，不是缺陷**——差异由能力矩阵显式声明。
`modes.py` 的 `DESIGN CONSTRAINT: DATA ONLY, NO SIBLING IMPORTS` 约束值得复用：
`normalizer_ref` 用 import path 字符串引用以避免循环。注意：**该字段目前是死字段**
（全仓库无解析代码，只有 `tests/test_occurrence_matrix.py:658` 验证字符串可解析），
属于「前瞻性声明」，不是已接线的机制。

## 7. 上帝模块与变更热点

| 模块 | 规模 | 说明 |
|---|---|---|
| `rca_core/extractor.py` | 7,055 行 / 94 顶层函数 | 图像增强 + 8 种 extract + 11 种 normalize + 视觉分类 + 自动模式 |
| `gui_fluent.py` | 5,249 行 | `ExtractPage` 69 个方法 |
| `js/table.js` | 4,654 行 | 5 种图表模式的表格渲染（分支注释在 505/546/668/701/766） |
| `js/minimax.js` | 4,406 行 | 传输 + 11 个 normalize + 错误镜像 |
| `server.py` | 4,031 行 | `Handler._handle_extract_body` 单方法 **995 行** |
| `rca_core/llm.py` | 3,663 行 | 传输 + ProviderStore + 配额 + 推理档位 + 重试 + 取消（6 类职责） |
| `rca_core/exporter.py` | 3,640 行 / 70 函数 | 表格配置 + 5 种格式 + 编辑 + i18n 标签 |

近 5 个月改动最频繁的文件（提交触及次数）：`extractor.py` 39、`server.py` 38、
`rca_core/i18n.py` 34、`llm.py` 32、`js/i18n.js` 32、`gui_fluent.py` 31、
`exporter.py` / `js/table.js` / `js/minimax.js` / `js/app.js` 各 30。
**这 11 个文件是最需要保护边界的地方。**

## 8. 并发模型（Web 端）

`server.py` 用 `ThreadingHTTPServer`（不是单线程），提取跑在
`daemon=True` 工作线程（`_spawn_extract_thread`, `:1417`），
`BoundedSemaphore` 限制大请求体并发（`:1262`），多轮扇出用
`ThreadPoolExecutor`（`:3554`）。所谓「长任务阻塞」精确说是**阻塞单个请求线程**，
不是整个服务。

## 9. 必须同步的契约清单

| 契约 | 参与方 | 守护 |
|---|---|---|
| `err.*` 错误键 | `server.py` → `js/minimax.js` 白名单 → `js/i18n.js` 三语 | `tests/test_error_key_contract.py` |
| 数值/归一化/评分语义 | `rca_core` ↔ `js/` 镜像 | `tests/js/tests_diff_frontend_parity.js` + 30+ 个 parity 测试 |
| ICS 地层表 | `rca_core/resources/ics_2024.json` ↔ `js/ics_table.js` | `tests/test_ics_invariants.py` |
| i18n 三语 | `rca_core/i18n.py` ↔ `js/i18n.js` | `tests/js/tests_contrast.js` |
| `TABLE_CONFIGS` | `rca_core/exporter.py` ↔ `js/table.js` | `tests/test_table_configs_parity.py` |
| 桌面历史页资产 | `gui_fluent_history_detail.py` ↔ `js/table.js` 等 3 个 | `tests/test_frontend_assets.py` |
| 缓存键含 `PROMPT_VERSION` | `rca_core/cache.py` ↔ `prompt.py` | `tests/test_cache_key_*.py` |

## 10. 明确否决的方案

| 方案 | 否决理由 |
|---|---|
| 三端合并成一个 | 牺牲「零依赖回退」与「浏览器直连」两条产品承诺 |
| `rca_core` 拆多个 pip 包 | 无第二消费者；加剧 parity；影响 PyInstaller 打包（已有 3 轮减重史） |
| monorepo 工具（Nx/Turborepo） | JS 侧无构建步骤，工具解决不存在的问题 |
| 插件化 / 事件总线 / 微服务 | 已有扩展点（`protocols`）边界是反的，应先修；全本地 SQLite/JSON 无跨进程价值 |
| TypeScript 化 / WASM 化 | 破坏「克隆即运行」；不消除双实现 |
| 删除 `gui.py` | 破坏零依赖承诺；改为冻结功能面 |
| 重写前端框架 | 24,005 行 JS + 2,576 断言 + 差分锁全绿，对科研价值零贡献 |
| 再动目录结构 | 2026-10 刚做完 root 63→22 迁移并产出 3 个 bug，刚修完该稳定 |

## 11. 复现命令

```bash
# 依赖图与环（AST，必须含函数内 import）
python -c "import ast,pathlib; ..."   # 见 tests/test_rca_core_acyclic.py
python -m pytest tests/test_rca_core_acyclic.py tests/test_frontend_assets.py \
                 tests/test_error_key_contract.py -q

# 规模与热点
git ls-files '*.py' | xargs wc -l | sort -rn | head -20
git log --since="2026-06-01" --name-only --format="" | sort | uniq -c | sort -rn | head

# 差分锁
node tests/js/tests_diff_frontend_parity.js
```
