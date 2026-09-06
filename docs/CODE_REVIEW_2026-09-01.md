# Range-chart Analyzer 专业级代码审查报告

**日期**：2026-09-01
**审查方式**：多 Agent 并行审查（5 路专项子代理：提取流水线 / 古生物领域正确性 / 聚合与质量 / server+GUI / JS 对等性）+ 主审（古生物年代地层学视角）亲读关键文件逐行交叉复核 + 全量测试实测。全程只读，未改动任何代码。
**主审模型**：GLM-5.3-Flash（WorkBuddy 智能体框架）
**审查范围**：`rca_core/`（26 个模块）、`server.py`、`gui*.py`、`js/`（13 个模块）、`tests/`、数据权威源 `rca_core/resources/ics_2024.json`

---

## 1. 执行摘要

| 项目 | 实测结果 |
|---|---|
| pytest（Anaconda Python 3.11 + pytest 8.3.4） | **819 收集 / 810 通过 / 9 跳过 / 0 失败**（36s） |
| node tests_frontend.js | **287 项全部通过**（历史基线记录为 138，测试规模显著增长） |
| 发现分级 | **P0×1、P1×14、P2×11、P3×10** |

**核心结论**：代码工程与领域语义设计**已达到科研软件水准**（防御性编码、fail-closed、大量历史 REVIEW 修复留痕、ICZN 开命名处理专业），但**数据权威源 `ics_2024.json` 存在 P0 级数据事故**——约 20 条界线年龄与 ICS 官方值不符、4 处内部重叠/断裂。**修复该文件之前，所有涉及"阶对齐 / 年代归属 / Steno 检验"的输出不可直接用于科研。**

> 最重要的一课：**819 个测试全绿，数据却是坏的。** 全部测试测的是代码逻辑，没有任何测试验证 ICS 数据本身的完整性。这是 P0 溜过的根本原因——科研软件需要"数据不变量（invariant）"测试层。

好消息：用户最关心的 **P–T 界线锚点（251.9 Ma）与 Changhsingian/Induan 底界内部自洽，K–Pg（66.0 Ma）正确**，P–T 主线科研场景的绝对锚点未损坏；受损的是三叠纪中晚期、泥盆纪—志留纪等多处的阶间对齐。

---

## 2. P0：ics_2024.json 年代数据事故（主审逐行复核确认）

### 2.1 内部一致性自检（证据为文件行号，无需外部依赖即成立）

| # | 矛盾 | 文件位置 | 后果 |
|---|---|---|---|
| 1 | Induan top 249.7 ≠ Olenekian base 251.2 | L485 / L476 | 249.7–251.2 Ma 出现 **1.5 Myr 归属真空** |
| 2 | Olenekian top 245.9 ≠ Anisian base 246.7 | L475 / L466 | **0.8 Myr 错位** |
| 3 | Norian top 208.5 ≠ Rhaetian base 205.7 | L435 / L426 | **2.8 Myr 断裂**（官方 Norian/Rhaetian 界线 = 205.7 Ma） |
| 4 | Frasnian top 367.1 ≠ Famennian base 372.15 | L665 / L656 | **5.05 Myr 重叠/断裂** |
| 5 | Pragian top 401.8 vs Emsian base 410.62 | L705 / L696 | **8.82 Myr 重叠** |
| 6 | 泥盆系底双值：419.62（L659/699/709/719）vs 419.2（L669、L725 Pridoli top） | 多处 | 同一界线两个值 |
| 7 | "Stage 5"（504.2–509.0, L892）与 "Wuliuan"（504.5–506.5, L882）并存 | L882–901 | 同一岩石地层单位两套年龄（Wuliuan 即原 Stage 5） |
| 8 | Gelasian `period: "Neogene"` | L7 | 2009 年 ICS 已将 Gelasian 划入第四系 |

### 2.2 与 ICS 官方值对照（domain-expert 子代理，stratigraphy.org ICS v2024 + Macrostrat/PBDB 双源交叉核验）

约 20 条界线与官方值不符，典型样例：

| 单位界线 | 文件值 (Ma) | ICS v2024 (Ma) | 偏差 |
|---|---|---|---|
| Olenekian 底 | 251.2 | 249.94 | +1.26 |
| Norian 顶 | 208.5 | 205.7 | +2.80 |
| Frasnian 顶 | 367.1 | 372.15 | −5.05 |
| 白垩系底 | 145.0 | 143.1 | +1.90 |
| 奥陶系底 | 485.4 | 486.85 | −1.45 |
| 志留系底 | 443.8 | 443.1 | +0.70 |

### 2.3 影响面（主审核定的污染传导链）

1. `chart_mode` 的阶对齐与 `range_top_idx` / `range_base_idx` 归位 → 年代归属系统性偏移；
2. ICS 坐标轴渲染；
3. **quality.py 的质量评分也被污染**：`_score_cross_era_accuracy`（L489–498）与 Steno 定序检查 `_score_biozone_order`（L698–804）直接调用 `ics_age_compare`，其判定基准正是这份坏数据 → 阶序颠倒的判定可能给出错误结论；
4. `js/ics_table.js` 与 json 为约定同源，浏览器端同步污染。

### 2.4 修复方案（Sprint A，最高优先级）

1. 以 stratigraphy.org 官方 v2024 数据重建 `ics_2024.json`（建议直接解析官方 CSV/GeoJSON 当源，杜绝手工誊抄）；
2. **新增数据 invariant 测试**：全表扫描断言 (a) 相邻阶 `top == next.base`（无缝），(b) 全表严格单调（单调），(c) `period_base_ma` 全周期一致，(d) 各 period 阶集合完整覆盖；
3. 修正 Gelasian 归属、合并 Stage 5/Wuliuan 冗余键、逐条修正乌溜阶等中文别名映射；
4. 同步重建 `js/ics_table.js`，重跑全量测试。

---

## 3. P1（14 项，须尽快修复）

### 领域 / ICS 数据（3 项）
| # | 问题 | 位置 | 说明 |
|---|---|---|---|
| 1 | 约 20 条界线年龄与 ICS v2024 不符 | ics_2024.json 全表 | 详见 §2.2（与 §2.1 内部矛盾互证） |
| 2 | Gelasian 误归 Neogene | ics_2024.json L7 | 第四纪/新近纪界线带归属错误 |
| 3 | 乌溜阶别名指向旧 "Stage 5" 条目 | standards/ics 别名表 | Wuliuan = 原 Stage 5，两键年龄并存（504.2–509.0 vs 504.5–506.5），会错算约 2 Myr |

### 提取流水线（2 项，core-pipeline 子代理）
| # | 问题 | 说明 |
|---|---|---|
| 4 | LLM 响应截断场景缺显式标志 | 部分字段静默缺失进入下游；建议截断检测 + 重试/降级标记 |
| 5 | 缓存键的模型维度不完整风险 | 不同模型结果可能互串命中；需核对 cache.py 键组成是否含 provider+model+prompt 版本+模式 |

### 聚合与质量（3 项，含主审亲核 2 项）
| # | 问题 | 位置 | 说明 |
|---|---|---|---|
| 6 | species 限定词恢复逻辑比较不对称 | aggregate.py L586–604 | `_norm(sp_val) == species_mode` 拿归一化值与**原始** mode 比较，大小写不等时静默失效 → 合并行展示的 "sp./cf./aff." 可能丢失（归组与去重不受影响，仅展示字段）。修复：`_norm(sp_val) == _norm(species_mode)` |
| 7 | 全轮次空退化输出缺一致字段/agreement 口径 | aggregate.py | 子代理发现：退化为空 primary list 时 agreement 信息不完整 |
| 8 | 质量评分继承污染 ICS 数据 | quality.py L489–498, L698–804 | Steno/Stage-order 判定基准为坏数据 → 修复 ICS 前这些检查结果不可信 |

### server / GUI（2 项，server-gui 子代理）
| # | 问题 | 说明 |
|---|---|---|
| 9 | 长任务同步阻塞 + 未捕获异常路径 | server.py LLM 调用数十秒，阻塞与异常裸奔路径需收敛 |
| 10 | GUI 后台线程直改 UI 控件竞态 | gui_fluent*.py 存在线程安全风险点（经典 Tkinter/Fluent 崩溃源） |

### JS 对等性（3 项，js-parity 子代理）
| # | 问题 | 说明 |
|---|---|---|
| 11 | js/quality.js 与 Python quality 存在权重/规则漂移 | 需按 parity 测试锁定修复 |
| 12 | js/json-utils.js 与 Python 宽松修复分支行为差异 | 尾逗号/单引号等修复路径两端不一致 |
| 13 | ics_table.js 同源污染 | 必须与 ics_2024.json 同步重建（§2.4） |

### 测试工程（1 项，主审）
| # | 问题 | 说明 |
|---|---|---|
| 14 | 缺数据 invariant 测试层 | 819 测试全绿但 ICS 数据损坏未被捕获——测试覆盖了逻辑未覆盖数据（P0 根因） |

---

## 4. P2 / P3 代表项（主审亲核，附行号）

**P2**
- aggregate.py L653：字符串/dict 混合轮次时走 dict 分支，字符串项被整体丢弃（潜在数据丢失）；
- aggregate.py L866–869：formations 去重用精确相等（大小写/空格敏感），易重复；
- quality.py L925–961：abundance sum 检查中空 `level` 聚为一组求和，可产生虚假"和≠100"违规；
- aggregate.py `_stable_typed_mode`：仅接受严格 `int`，提取端若产出 float 索引（3.0）会被静默丢弃 `range_top_idx`；
- 子代理 P2 共 11 项（流水线 6、server/GUI 3、JS 3 中的代表项），主题：异常静默吞掉、日志泄露 API key 风险点、超时边界、JS i18n 键漂移等。

**P3**
- chimera 检测仅覆盖 species_ranges 元组，其他三种模式无 chimera 防护（aggregate.py L485）；
- `_BIOZONE_STAGE_MAP`（quality.py L814–841）覆盖极小（自述 intentional）→ Steno 检查对多数真实生物带标签为 no-op，注意向用户如实呈现覆盖范围；
- 子代理 P3 主题：建议性重构、注释与实现同步、可读性等 10 项。

---

## 5. 古生物学领域严谨性评价（主审专业意见）

**做得好的（达到或超过同类科研工具水准）：**
1. **Ma 方向语义全链路正确**（亲核）：quality.py FAD/LAD 检查——bed 分支 `top ≥ base`（1-indexed 自底向上）、Ma 分支 `base_ma ≥ top_ma`（L339–391），与"越年轻 Ma 越小"的地质约定一致；
2. **ICZN 开命名处理专业**：aggregate.py 保留 sp./cf./aff./ex gr./s.l./s.str./nom. dub. 等 10+ 标记进 dedup 键（B-1 修复注释明确记录了"塌缩 sp. 是严重数据完整性事故"的教训）；
3. **作者引证归一化符合 ICZN Art. 51.2**：Smith (1950)/Smith, 1950/Smith ex Jones 归一正确，且有意不合并同名不同缩写（无权威数据库时的正确保守行为）；
4. **生物地层学细节到位**：Clarkina 种级牙形石带映射（REVIEW-2026-07-31 修正了属级键把 Wuchiapingian 带误配 Changhsingian 的问题）、Otoceras/Ophiceras→Induan 映射正确；跨代边界剖面（P–Tr、K–Pg）合法跨代降权处理正确；
5. **科研可追溯性**：chimera 检测+"no single run observed the merged tuple" 警告、agreement_count/n、置信度合并文档诚实（M-1 注释承认简单平均而非宣称加权）；
6. **json_utils 工程扎实**：平衡括号扫描器转义处理正确、NaN/Infinity 严格拒绝与 JS 对齐、payload 评分从候选对象中选真身（F-1）。

**不足：**
1. ICS 数据权威源失效（P0）——这是"领域正确性"上唯一但致命的短板；
2. Steno 检查的生物带字典覆盖极小，易给用户虚假安全感；
3. 阶对齐输出缺少"数据版本戳"（哪版 ICS、何时校验）——建议在结果 JSON 中输出 `ics_version` 字段。

---

## 6. 结论：能否达到专业级科研软件水平？

| 维度 | 评分 | 依据 |
|---|---|---|
| 代码工程质量 | **A−** | fail-closed、防御性编码、历史 REVIEW 修复留痕（819 测试全绿） |
| 领域语义设计 | **A−** | FAD/LAD 方向、ICZN 开命名、生物地层映射均专业 |
| **数据权威性** | **F** | ics_2024.json 约 20 条界线错误 + 4 处内部矛盾 |
| 测试工程 | B+ | 规模大且全绿，但缺数据 invariant 层 |
| 可追溯性 | A− | chimera_warnings、agreement、诚实文档化 |
| 双端一致性 | B+ | parity 测试锁定机制有效（287 项通过），少量漂移待修 |

**总评**：软件的"骨架"（架构、代码、领域逻辑）已经是专业级科研软件的水准，多处设计比很多同类学术工具更严谨；但"血液"（年代数据）当前被污染。**完成 Sprint A（重建 ICS 数据 + invariant 测试）后，本软件可以负责任地称为专业级科研软件；在此之前，凡涉及阶对齐与年代归属的输出必须人工复核。**

---

## 7. 修复路线图

- **Sprint A（立即，P0）**：重建 ics_2024.json → 数据 invariant 测试 → 同步 js/ics_table.js → 重跑全量（预计消除 2/3 的下游年代偏差）；
- **Sprint B（P1，14 项）**：别名映射、缓存键维度、截断标志、aggregate.py L586 修复、server 阻塞/异常、GUI 线程、JS parity 漂移；
- **Sprint C（P2/P3）**：按 §4 清单与子代理明细逐项消化。

## 8. 审查方法与局限（诚实声明）

1. 子代理的 P2/P3 部分发现为主题级汇总（完整行号级明细在审查过程记录中）；
2. 未进行真实 LLM 端到端调用实测（需 API 与真实图像，建议作为 Sprint B 后的验证关卡）；
3. 13 个 JS 模块未全部逐行审计（parity 测试 + 重点模块抽查）；
4. ICS 官方值核验以 stratigraphy.org v2024 + Macrostrat/PBDB 双源为准；即使个别官方值存在版本差异，§2.1 的 4 处**内部矛盾**不依赖外部数据即成立，P0 结论不受影响。
