<div align="center">

# Range Chart Analyzer

**面向微体古生物学的地层延限图智能提取与结构化工具**

*LLM-powered extraction of stratigraphic range charts into analyzable data*

[![CI](https://github.com/syxscott/Range-chart-Analyzer/actions/workflows/ci.yml/badge.svg)](https://github.com/syxscott/Range-chart-Analyzer/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/Python-3.10%20%7C%203.12-3776AB?logo=python&logoColor=white)
![Node](https://img.shields.io/badge/Node-20-339933?logo=nodedotjs&logoColor=white)
![Platform](https://img.shields.io/badge/Platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey)
![i18n](https://img.shields.io/badge/UI-中%20%2F%20EN%20%2F%20日-blue)

*从地层沿线图（stratigraphic range chart / 种属延限图）中恢复"图上可见、可核对、可溯源"
的结构化数据：种属延限、生物带、岩性地层剖面、丰度剖面、生物带对比方案与更多图式——
三种交付形态共用同一套提取核心与质量保障。*

</div>

---

## 目录

- [研究背景与定位](#研究背景与定位)
- [功能总览](#功能总览)
- [提取模式与能力注册表](#提取模式与能力注册表)
- [工作原理](#工作原理)
- [数据可信度与证据链](#数据可信度与证据链)
- [真实文献端到端验证](#真实文献端到端验证)
- [快速开始](#快速开始)
- [提高提取精度的建议](#提高提取精度的建议)
- [提取字段](#提取字段)
- [配置参考](#配置参考)
- [项目结构](#项目结构)
- [测试与持续集成](#测试与持续集成)
- [安全与隐私](#安全与隐私)
- [借鉴与致谢](#借鉴与致谢)
- [路线图](#路线图)
- [引用](#引用)
- [许可](#许可)

---

## 研究背景与定位

延限图（range chart）是微体古生物学——尤其是放射虫生物地层学——承载分类单元
延限、样品分布与生物带划分信息的**核心图式**。然而这些图绝大多数以位图形式
存在于论文中，数据无法直接进入数据库或定量分析流程，人工转录既耗时又易引入
转录误差。

本项目以多模态大模型（默认 MiniMax-M3，兼容 100+ OpenAI / Anthropic 格式端点）
为读取引擎，围绕三个科研级设计目标构建：

1. **可溯源**——每条结果都能回答"来自哪张图、哪个模型、哪次运行、哪一版年代标尺"；
2. **不编造**——读不出的内容显式降级为空产出与行级说明，绝不以高置信度虚构数值；
3. **双端一致**——Python 核心与浏览器前端实现同一套提取契约（相同 system prompt、
   相同容错解析、相同结果字段），并有 parity 测试锁定。

> 提取逻辑移植自 [RLPE](https://github.com/syxscott/RLPE-Radiolarian-Plate-Extractor)
> 项目的 range-chart 视觉抽取模块。

---

## 功能总览

| 类别 | 能力 |
|---|---|
| **提取** | 8 种图表模式（见下表）；`auto` 模式两级识别（图注启发式 → 视觉分类）；中/英/日/俄四语图表 |
| **科研可信度** | 证据链报告（模式决策/截断/空表原因/ICS 版本戳）；多轮众数合并与一致性标注；嵌合行检测与丢弃；学名模糊验证（GBIF）；延限图重绘校验 |
| **结果交付** | 在线可编辑表格；JSON / CSV / TSV / XLSX 导出；Darwin Core 与 PBDB 标准字段（含 PBDB 上传扩展列，见「提取字段」）；`to_wpd` 导出 WebPlotDigitizer 交换包（每数据集一份 x,y CSV + `wpd_axes.json` 轴标定，见「提取字段」） |
| **工程** | 100+ LLM 提供商预设；本地后端（免 CORS、CSRF/SSRF/限流加固）；SQLite 历史（含 PROV-O 溯源导出）；Token 用量统计 |
| **界面** | Fluent 桌面（PySide6）/ Tkinter 回退 / Web + 本地后端 / 纯前端静态站；中/英/日三语 UI |

---

## 提取模式与能力注册表

成熟度分级借鉴 [thu-digitizer](https://github.com/Rimagination/thu-digitizer) 的
extractor_registry：**稳定** = 真实文献端到端覆盖；**候选** = 全栈就绪、在典型图式上
表现良好；**辅助** = 可产出结构化数据、但图式宽泛需预期部分产出。

| 模式 | 成熟度 | 说明 |
|---|---|---|
| `range_chart` 种属延限图 | 🟢 稳定 | 核心模式。种 × 样品延限矩阵、丰度标注、生物带列；真实文献 E2E 覆盖（22 行种属延限逐行核对） |
| `columnar_section` 岩性柱状图 | 🟢 稳定 | 地层柱、组/段、厚度、岩性图例与化石图例 |
| `abundance_diagram` 丰度图式 | 🟡 候选 | 深海丰度曲线/剖面表现良好；饼图与热图按设计降级为空产出 |
| `phylogenetic_tree` 系统发育树 | 🟡 候选 | Newick 结构化输出（parent/id + 分支属性） |
| `zonation_chart` 生物带对比图 | 🟡 候选 | 放射虫生物年代学经典图式：多方案带/亚带层级、定义事件、跨框架对比边。E2E 自三叠纪对比图恢复 32 带 × 3 方案 × 12 对比 |
| `chemical_stratigraphy` 地球化学图式 | ⚪ 辅助 | 元素/同位素曲线、年龄分布；图式宽泛，预期部分产出 |
| `paleomap` 古地理图 | ⚪ 辅助 | 古大陆/海洋/化石产地；现今地质图会被模型诚实拒绝 |
| `scatter_plot` 散点/双坐标图 | ⚪ 辅助 | 形态散点、环境相关；产出随图而异 |

> **auto 模式**：图注/文件名关键词启发式先行；无法判断时将图像交给视觉分类器
> （8 类 + unknown，置信度 ≥ 0.5 采信），识别结果以 `mode_used` 与结果工具栏的
> "自动识别"芯片向用户透明呈现。

---

## 工作原理

```
图表图片 (PNG / JPG / WEBP)
   │  上传（可选 Pillow 压缩与增强，保留小字细节）
   ▼
┌─────────────────────────────────────────────────────┐
│ 阶段 1 · 图表类型识别（auto 模式）                  │
│   图注/文件名关键词启发式 → 未命中则视觉分类(8 类) │
└─────────────────────────────────────────────────────┘
   ▼  按模式注入专属 system prompt（v1–v4，双端同源）
┌─────────────────────────────────────────────────────┐
│ 阶段 2 · 视觉提取                                   │
│   严格 JSON 契约 → 6 级容错解析（含截断修复：      │
│   max_tokens 截断时保住全部已完成行）               │
│   → 归一化：行级类型校正 + 未知键 _extras 保留     │
└─────────────────────────────────────────────────────┘
   ▼  多轮时：逐字段众数合并 + agreement 一致性
   ▼  嵌合行检测：无任何单轮观察过的合并行被丢弃
┌─────────────────────────────────────────────────────┐
│ 阶段 3 · 质量与证据链                               │
│   quality 评分 · 学名模糊验证(GBIF) · 证据链报告    │
│   （模式决策 / 截断 / 行数 / 空表原因 / ICS 版本） │
└─────────────────────────────────────────────────────┘
   ▼
在线表格（可编辑） · XLSX/CSV/TSV/JSON · DwC/PBDB 字段
SQLite 历史库（PROV-O 溯源导出） · Token 用量统计
```

---

## 数据可信度与证据链

科研工具的可信度取决于**降级与不确定性是否可见**。本项目为此提供：

### 证据链报告（`report`，schema v1）

每次提取附带结构化审计报告，可在不重跑模型的情况下回答全部"为什么"：

```json
{
  "schema_version": 1,
  "mode": { "requested": "auto", "used": "zonation_chart", "source": "vision",
            "auto": { "mode": "zonation_chart", "source": "vision" } },
  "input": { "image_sha256": "…", "runs": 1 },
  "truncation": { "truncated": false, "warning": "" },
  "rows": { "zonations": 3, "zones": 32, "correlations": 12 },
  "empty_tables": [
    { "key": "sections",
      "reason": "Table 1 columns are buried under the SEM plate strip…" }
  ],
  "warnings": [],
  "timescale": { "ics_version": "ICS v2024/12" },
  "provenance": { "model": "…", "max_tokens": 4000, "prompt_sha256": "…" }
}
```

### 年代标尺版本锁定

所有阶对齐与年代归属基于内置 **ICS v2024/12 国际年代地层表**（98 个单位，
12 系无缝拼接，含不变量测试与 Macrostrat 交叉验证），报告中的 `timescale.ics_version`
字段显式标注版本——论文使用时可直接注明所依据的标尺版本。已知的 v2026-06
官方更新（Olenekian 底 250.8、Anisian 底 247.0、Wuchiapingian 底 259.857）尚待采纳。

### 学名验证（可选，ICZN 安全）

视觉 OCR 对密排斜体拉丁学名的误读（E2E 实例：*Psendotirolites*）由
GBIF species-match 模糊匹配兜底：给出规范名候选与置信度供人工核对。
**只标注、不改写**——开放命名限定词（cf. / aff. / sp. / ex gr.）在查询时剥离、
在数据中完整保留，符合 ICZN 惯例。

### 多轮一致性

同一张图运行多次后按字段众数合并，逐行输出一致性（如 3/3、1/3）；低一致性行
以警告色标注；**没有任何单轮观察过的嵌合合并行会被检测并丢弃**。

---

## 真实文献端到端验证

2026-09-07 使用 MiniMax-M3 对**真实放射虫文献**（2021 新放射虫文献库）做了
自动化端到端验证：从 41 篇论文抽取 40 张信息图，以 `auto` 模式全量提取。

| 指标 | 结果 |
|---|---|
| 提取成功率 | **40 / 40**（零调用失败） |
| 模式判定分布 | 丰度 16 · 柱状 7 · 古地理 6 · 延限 6 · 带对比 3 · 地化 2 |
| 实质数据产出 | 25 / 40（其余为诚实降级：饼图/热图/照片图版不可读，见 `report.empty_tables`） |
| 代表案例 | 三叠纪带对比图恢复 **32 带 × 3 方案 × 12 对比边**；延限图修复后 **22 行种属延限逐行吻合**；截断场景恢复 **61 行丰度 + 3 站位** |

E2E 直接驱动了三项修复：混合图注（"columnar section with range chart… zonation"）
的判定例外、截断修复层（Level 3.5）、以及分类提示词补充图版类别。测试基础设施
（图源抽取与提取 harness）保留在 `scripts/_e2e_*.py`。

---

## 快速开始

### 0. 一键启动（最快）

```bash
python main.py           # 现代 Fluent GUI（默认，未装 PySide6 时自动回退 Tkinter）
python main.py gui       # 强制经典 Tkinter GUI
python main.py server    # Web + 本地后端，自动打开浏览器
```

Windows 可直接双击 `run.bat` / `run_gui.bat` / `run_server.bat`。

### 1. 桌面 GUI（最简单）

```bash
pip install -r requirements.txt   # 可选依赖（见下）
python gui.py
```

- 左侧配置 API Key 与提供商（内置 100+ 预设）、选图，点「开始提取」；
- 右侧多标签页展示结果，双击单元格可编辑，支持逐表复制/导出与「导出全部 JSON」；
- 设置自动保存至 `~/.range_chart_analyzer.json`。

### 2. Web + 本地后端（浏览器界面，无 CORS）

```bash
python server.py --port 8000
```

页面由本地服务器打开时自动走同源 `/api/extract`（CSRF/会话保护、缓存、
限流与证据链报告由后端注入）。可选参数：`--host 0.0.0.0`、`--max-threads`。

> ⚠️ `--host 0.0.0.0` 会把服务暴露给同网段的所有主机。Origin/CSRF 防护
> 只对浏览器有效，任何能连到该端口的非浏览器客户端都可以自行签发令牌并调用
> `/api/extract`（以本机为跳板转发请求、读取历史审计）。请仅在受信网络中使用；
> 非回环绑定会在启动日志打印警告。

### 3. 纯前端静态站（无后端）

直接打开 `index.html`（或任意静态服务器托管 `index.html + css/ + js/`）。
浏览器直连模型端点可能受 CORS 限制；需要时可自建代理
（见 [`proxy/README.md`](proxy/README.md)：Cloudflare Workers 与 Deno Deploy 部署方案 + Vercel Edge 适配指引）。

### 4. 原生窗口（可选）

```bash
pip install pywebview
python main.py --ui modern
```

WebView 引擎不可用时自动回退浏览器打开，退出码仍为 0。

### 可选依赖

| 包 | 用途 | 缺失时 |
|---|---|---|
| `openpyxl` | XLSX 导出 | 跳过该格式 |
| `matplotlib` | 用量图表 + 延限图重绘校验 | 回退纯文本 |
| `Pillow` | 缩略图 / 客户端压缩 / 图像增强 | 跳过相关功能 |
| `PySide6 (+Fluent-Widgets)` | 现代 Fluent 桌面 GUI | 回退 Tkinter |
| `pywebview` | 原生窗口模式 | 回退浏览器 |
| `sv_ttk` | Tkinter Fluent 主题 | 回退原生主题 |
| `cryptography` + `keyring` | API Key 静态加密 | 降级为混淆存储 |

核心提取与 CSV/JSON/TSV 导出**仅用标准库**；Python ≥ 3.10（CI 验证 3.10/3.12）。

---

## 提高提取精度的建议

1. **上传高清原图**——小字识读精度与分辨率直接相关；工具默认以无损 PNG 保留细节。
2. **调高「图像分辨率上限」**（默认 4000px）；设 0 则完全不压缩。
3. **开启多轮运行**（如 3 次）：众数合并 + 逐行一致性标注能有效压制单次随机误读。
4. **在图注框粘贴原文种名列表**，帮助模型校正拼写。
5. **人工核对低一致性 / 低置信度行**，以及自动识别芯片所示的模式是否与原图一致。
6. 区分**年代阶（Stage）**与**岩石组（Formation）**及生物带列——Prompt 已强化区分，仍建议复核。

*Upload high-resolution originals, raise the resolution cap, and enable multiple
runs — the agreement column flags rows needing review on dense italic names.*

---

## 提取字段

以 `range_chart` 为例（其余模式见各模式提示词与导出配置）：

| 表 | 字段 |
|---|---|
| Sections 地层剖面 | name, age_range, formations, formation_thickness_m, coordinates |
| Species Ranges 种属延限 | species, section, range_base（老）, range_top（新）, biozone, (+author_year / occurrence_mode / note 等可选列) |
| Biozones 生物带 | name, section, age, thickness_m |
| Other Fossils 其他化石 | free-text 记录 |
| PBDB 上传扩展列（`to_pbdb_csv`，追加在历史列之后） | 三名法拆分 genus / species / subspecies（含 cf./aff. 等开放命名限定词原样保留）；年代分辨率限定符 early_interval_reso / late_interval_reso / max_ma_reso / min_ma_reso（stage/series/system/era/measured/zone/informal）；丰度列 abund_value / abund_unit |

整体附带 `confidence`（0–1）。任何语言的图表统一输出**标准拉丁学名**与英文地质字段；
模型附加的未知字段按 H8 契约保留于行级 `_extras`，不丢弃。

### WebPlotDigitizer 交换导出（`to_wpd`，格式 `rca-wpd/1`）

`rca_core/exporter.py` 的 `to_wpd(result, source_file=…, output_dir=…, y_axis="auto")`
为一张提取结果产出可直接喂给 WebPlotDigitizer 的交换包（当前为 Python API，GUI/前端
尚未接导出按钮）：

- **数据集 CSV**：一个数据集一份纯两列 `x,y` 文件（LF、无 BOM、表头 `x,y`），命名
  `wpd_<图版>__<剖面>__<种名>.csv`——延限图每个种属延限一份、丰度图每条曲线一份、
  带对比图每个生物带一份；端点数值先经共享 bed 解析器与 ICS 标尺（`y_axis` 取
  `level` 层位索引或 `age` 年代，`auto` 按多数行可解析者选定）；
- **`wpd_axes.json`**：两轴的 name/unit/min/max、两个标定点（scale point）的值、
  数据集清单与使用说明，并附一份按 WPD 自身 JSON 交换书写的兼容块。

导入 WPD：Add Image → 按 `wpd_axes.json` 的 min/max 定义两轴并把标定点锚到图面上
对应位置 → Data → Import Data (CSV) 逐个选择清单列出的数据集文件。诚实边界：交换包
工作在**数据空间**（标签/索引/ICS 年龄）——提取结果只有标签、没有像素几何，因此像素
标定仍由用户在 WPD 中点取；解析不出数字的端点（如 "common"）记入 `warnings`，绝不编造。

---

## 配置参考

| 配置 | 默认 | 说明 |
|---|---|---|
| 图表类型 | 自动 | 手动指定 8 种模式之一；auto = 启发式 + 视觉分类 |
| 图表语言 | 自动检测 | 中/英/日/俄；辅助模型正确识读 |
| 图像分辨率上限 | 4000 px | 长边上限；0 = 不压缩 |
| 图像增强 | 关 | 3× 上采样 + 去噪（薄线/小字场景） |
| 运行次数 | 1（1–5） | 多轮众数合并，降低随机误读 |
| 最大输出 Token | 4000 | 大图可调高避免截断（截断时报告会标注） |
| 连接模式 | 自动 | 本地后端 / 浏览器直连 / 自建代理 |

---

## 项目结构

```
Range-chart Analyzer/
├── main.py                    # 统一入口（Fluent GUI / Tkinter / server）
├── gui.py                     # Tkinter 桌面 GUI
├── gui_fluent*.py             # PySide6 Fluent 桌面 GUI（页面/提供商/历史详情）
├── server.py                  # 本地后端：静态托管 + /api/extract（加固版）
├── index.html + css/ + js/    # 纯前端（与 Python 端同源契约）
├── rca_core/                  # 三端共用核心
│   ├── prompt.py              #   各模式系统提示（四语图表 + 拉丁学名）
│   ├── extractor.py           #   LLM 调用 + 图片处理 + 各模式归一化
│   ├── json_utils.py          #   容错 JSON 解析（6 级回退 + 截断修复）
│   ├── chart_mode.py          #   图表类型自动识别（共享启发式）
│   ├── names.py               #   学名模糊验证（GBIF 后端）
│   ├── report.py              #   证据链报告（schema v1）
│   ├── redraw.py              #   延限图重绘校验（matplotlib 可选）
│   ├── capabilities.py        #   模式成熟度注册表
│   ├── aggregate.py           #   多轮众数合并 + 嵌合检测
│   ├── quality.py             #   质量评分与检查项
│   ├── standards/ics.py       #   ICS v2024/12 年代标尺（98 单位）
│   ├── standards/darwin_core.py / pbdb.py   # 标准字段映射
│   ├── exporter.py            #   表配置 + CSV/TSV/JSON/XLSX
│   ├── history.py / db.py     #   SQLite 历史 + PROV-O 溯源
│   └── usage.py               #   Token 用量统计
├── js/                        # 浏览器端同源实现（minimax/aggregate/
│                              #   table/quality/ics_table/i18n/…）
├── proxy/                     # 可选 CORS 代理（Cloudflare / Deno）
├── tests/ + tests_core.py     # 1,100+ 项 pytest 用例
├── tests_frontend.js 等       # 450+ 项 Node 用例（含 parity 锁）
├── docs/                      # 审查报告与修复记录
└── scripts/                   # 真实文献 E2E harness（key 走仓库外 env）
```

---

## 测试与持续集成

```bash
python -m pytest tests/ tests_core.py -q      # Python 套件（CI 口径）
python -m pytest tests/ tests_core.py tests_exporter_xlsx.py …   # 全量口径
node tests_frontend.js                        # 前端 parity + 行为（358 项）
node tests_aggregate.js                       # 聚合逻辑（60 项）
```

- **规模**：Python ≈1,100 项、Node ≈450 项（含三端 parity 锁、ICS 数据不变量、
  金标离线管线门槛、截断修复真机夹具、GUI Qt 测试）；
- **CI**：GitHub Actions 在 Python 3.10 / 3.12 + Node 上运行上述套件；
- **金标门槛**：`tests/test_gold_smoke.py` 离线运行 `safe_json_loads → normalize →
  eval_metrics` 并断言精度/召回阈值；预录响应由
  `python tests/fixtures/gold/_gen_canned.py` 生成；
- **数据不变量**：`tests/test_ics_invariants.py` 锁定年代标尺的内部一致性——
  这一层是为回应"819 个测试全绿但年代数据损坏"的历史教训而建。

---

## 安全与隐私

- **数据不出本机**：图片与请求由你的设备直接发往你配置的模型端点（或自建代理），
  本项目不经过任何第三方服务。
- **密钥本地存储**：GUI 存于 `~/.range_chart_analyzer.json`（可关闭记忆）；
  Web 存于浏览器 `sessionStorage`；支持可选的静态加密。
- **后端仅监听本机**：`server.py` 默认绑定 `127.0.0.1`；对外暴露时可启用
  CSRF / 会话 / 限流 / Origin 校验等加固（默认启用），SSRF 防护拦截内网与
  云元数据地址。
- **溯源完整**：每条历史记录含图像 SHA-256、请求元数据与证据链报告，可复核、可复现。

---

## 借鉴与致谢

本项目在调研同类开源项目（2026-09-07）时借鉴了以下设计：

- **[thu-digitizer](https://github.com/Rimagination/thu-digitizer)**（MIT）——"证据优先"提取范式：证据链报告、行级置信状态、模式能力注册表与重绘校验均源于此。
- **[FigDataX](https://github.com/Shaowen-Ye/FigDataX)**——"确定性引擎测几何 + LLM 读语义"的分工与校准 RMSE 门槛，是丰度/化学模式几何引擎演进的方向。
- **[gnames/gnfinder](https://github.com/gnames/gnfinder)**——科学名查找与验证的思路来源（本项目学名验证默认走 GBIF 兜底，gnfinder 适配器预留）。
- **[equinor/scampi-benchmark](https://github.com/equinor/scampi-benchmark)**、**[plannapus/RadiolarianClassifier](https://github.com/plannapus/RadiolarianClassifier)**——微体化石图像自监督分类的开放权重与方法（图版自动鉴定的远期路线）。
- **[automeris-io/WebPlotDigitizer](https://github.com/automeris-io/WebPlotDigitizer)**——图表数字化的黄金标准与事实交换格式。
- **[plannapus/NSBcompanion](https://github.com/plannapus/NSBcompanion)**、**[ropensci/paleobioDB](https://github.com/ropensci/paleobioDB)**——钻孔微体化石数据库与 PBDB 接口的语义参考。

---

## 路线图

- [ ] 丰度/地球化学模式的**几何引擎 PoC**（确定性 CV 测刻度与曲线像素，LLM 只读语义——FigDataX 路线）
- [ ] 学名验证的 **UI 接线**（结果面板显示 `names.fuzzy` 候选）
- [ ] 延限图 **overlay 标注**（把提取端点映射回原图坐标供复核）
- [ ] 图版**自动鉴定**接入（scampi 开放权重 / RadiolarianClassifier 路线）
- [ ] E2E 40 图升级为带人工核对基准的**公开回归门禁**
- [ ] ICS v2026-06 标尺更新（Olenekian / Anisian / Wuchiapingian 三处界线）

---

## 引用

若本工具对您的研究有帮助，欢迎引用：

```bibtex
@software{range_chart_analyzer_2026,
  author  = {syxscott},
  title   = {Range Chart Analyzer: LLM-based extraction of stratigraphic
             range charts into structured data},
  year    = {2026},
  url     = {https://github.com/syxscott/Range-chart-Analyzer},
  note    = {ICS timescale v2024/12; validated end-to-end on 40 figures
             from the radiolarian literature}
}
```

---

## 许可

沿用来源项目 [RLPE](https://github.com/syxscott/RLPE-Radiolarian-Plate-Extractor)
的许可条款。仅供科研与教育用途。

<div align="center">
<sub>Built for micropalaeontologists who are tired of transcribing range charts by hand.</sub>
</div>
