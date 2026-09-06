# ICS 数据权威源重建修复报告（Sprint A，2026-09-01）

对 `docs/CODE_REVIEW_2026-09-01.md` 中 **P0 级 ICS 数据事故** 的修复记录。
执行人：主审（GLM-5.3-Flash，古生物年代地层学 + AI 工程视角）。

---

## 1. 问题回顾

审查发现 `rca_core/resources/ics_2024.json` 是**多版本数据混合体**：约 20 条界线年龄与官方值不符，且存在 4 处以上的内部重叠/断裂。最严重的是：

| 内部矛盾 | 旧值 | 后果 |
|---|---|---|
| Induan 顶 249.7 vs Olenekian 底 251.2 | 差 1.5 Myr | 249.7–251.2 Ma 归属真空 |
| Norian 顶 208.5 vs Rhaetian 底 205.7 | 差 2.8 Myr | 晚三叠世阶序断裂 |
| Frasnian 顶 367.1 vs Famennian 底 372.15 | 差 5.05 Myr | F/F 界线错误 |
| Pragian 顶 401.8 vs Emsian 底 410.62 | 差 8.82 Myr | 泥盆纪下统重叠 |
| 泥盆系底双值 419.2 / 419.62 | 自相矛盾 | 系底界不一致 |
| Artinskian 顶 279.5 vs Kungurian 底 283.3 | 差 3.8 Myr | 乌拉尔统断裂 |
| Wordian 顶 265.1 vs Capitanian 底 264.28 | 差 0.82 Myr | 瓜德鲁普统断裂 |
| Stage 5 (504.2–509.0) vs Wuliuan (504.5–506.5) | 同一单位两套年龄 | 寒武系重复单元 |

**根因**：819 个测试全绿，但没有一个测试验证**数据本身**——测试测的是代码逻辑，年代数据从未被钉死到权威源。

---

## 2. 权威源与仲裁过程

单靠记忆无法定版（界线年龄随图表版本漂移），因此逐个分歧做三方交叉验证：

1. **stratigraphy.org 官方图表全文**（v2024/12）——主要基准
2. **Macrostrat timescale #1 "international ages"**（102 个阶级单元，内部无缝单调）
3. **Paleobiology Database "International Chronostratigraphic Timescale"**（引用 Cohen et al. 2024, ICS v2024/12）

补充证据：
- ICS 官方 changelog（2022/10）：侏罗系全部阶的年龄按 GTS2020 更新，逐条列出「was → is」
- Permophiles #78（SPS 2024 年报）：确认 Kungurian 底 283.5→283.3、Roadian 底 273.01→274.4 的官方改动

**仲裁结论（关键）**：三方完全一致。项目原有的两条回归测试
（`tests/test_ics_cretaceous.py`、`tests/test_ics_cenozoic.py`）虽然自称引用 ICS v2024/12，
实际锁定的是 **GTS2016 老值**（Berriasian 底 145.0、Campanian 底 72.1、Santonian/Coniacian 86.3、
Albian 底 113.0、Ordovician 中段 453.0/467.3/470.0/477.7、以及一整套古近系老值）。
**测试保护了过期数据**——这正是本次事故能长期隐匿的原因。

---

## 3. 修复内容

### 3.1 数据重建（`rca_core/resources/ics_2024.json`）

全部 12 个系的界线年龄统一到 **ICS v2024/12**，约 **70 个界线数值**被修正，新增 4 个条目
（Jiangshanian、Stage 2、Stage 3、Stage 4），最终 **98 个条目**，全链无缝单调。

分系统要点：

| 系 | 主要修正 |
|---|---|
| 第四系 | 底界 2.588→2.58；**Gelasian 归属 Neogene → Quaternary**（2009 年起官方归第四系） |
| 新近系 | Langhian 顶 15.98、Burdigalian 15.98–20.45、Aquitanian 20.45–**23.04** |
| 古近系 | Chattian 23.04–**27.30**（旧 27.82）、Lutetian 41.03–48.07、Bartonian 41.03、Ypresian 48.07、Thanetian 59.24、Selandian 61.66、Danian 61.66 |
| 白垩系 | 底界 **143.1**（旧 145.0）、Berriasian 顶 137.05、Valanginian 132.6、Albian 底 113.2、**Coniacian/Santonian 界线 85.7**（旧 86.3）、Campanian 底 **72.2**（旧 72.1） |
| 侏罗系 | 全系统按 2022/10 官方 GTS2020 更新：Toarcian 顶 184.2、Pliensbachian 184.2–192.9、Sinemurian 顶 192.9、Bajocian 168.2、Bathonian 165.3–168.2、Callovian 165.3、Tithonian 顶 143.1 |
| 三叠系 | **P–T 界线精化为 251.902**（旧 251.9）、Induan 顶 249.9、Olenekian 246.7–249.9、Anisian 241.464–246.7、Ladinian 237.0–241.464、Carnian 227.3–237.0、**Norian 顶 205.7**（旧 208.5） |
| 二叠系 | Asselian 顶 293.52、Sakmarian 290.1–293.52、Artinskian 283.3–290.1、Kungurian 274.4–283.3、Roadian 266.9–274.4、Wordian 顶 264.28 |
| 石炭系 | Tournaisian 顶 358.86、Visean 底 330.3、Serpukhovian 323.4–330.3、Bashkirian 底 323.4 |
| 泥盆系 | 底界统一 **419.62**、Lochkovian 顶 413.02、Pragian 410.62–413.02、Emsian 顶 393.47、Eifelian 387.95–393.47、Givetian 382.31–387.95、**Frasnian 372.15–382.31**（旧顶 367.1）、Famennian 顶 358.86 |
| 志留系 | 底界 443.1（旧 443.8）；Llandovery 432.9–443.1、Wenlock 426.7–432.9、Ludlow 422.7–426.7、Pridoli 419.62–422.7 |
| 奥陶系 | 底界 **486.85**（旧 485.4）、Floian 471.3–477.1、Dapingian 469.4–471.3、Darriwilian 458.2–469.4、Sandbian 452.8–458.2、Katian 底 452.8、Hirnantian 顶 443.1 |
| 寒武系 | 补回缺失的 **Jiangshanian (491.0–494.2)**、**Stage 2/3/4**；Stage 5 改为与 Wuliuan 完全同值（504.5–506.5，消除重复单元矛盾）；Series 2 范围修正为 506.5–521.0；Fortunian 529.0–538.8；Paibian 顶 494.2；Stage 10 486.85–491.0 |

### 3.2 Python 侧（`rca_core/standards/ics.py`）

- **乌溜阶 → `Wuliuan`**（旧值指向过期的 `Stage 5` 条目，误差约 2 Myr）；新增 `江山阶 → Jiangshanian`
- 寒武系列映射按官方重建：
  - `lower/early cambrian` → `["Fortunian", "Stage 2"]`（原 `Series 2`，把纽芬兰统算错）
  - `middle cambrian` → `["Wuliuan", "Drumian", "Guzhangian"]`，名称 `Miaolingian`（原 `Middle Cambrian` + 旧 `Stage 5`，基类错为 509.0）
  - `furongian/late cambrian` → `["Paibian", "Jiangshanian", "Stage 10"]`
- 更新世显式边界 `2.588 → 2.58`

### 3.3 JS 侧（`js/ics_table.js`）

- `RCA_ICS_TABLE` 全量重建，与 JSON 逐值一致（新增 4 键）
- `RCA_ICS_CN_STAGES`：乌溜阶→Wuliuan，新增 江山阶→Jiangshanian
- `RCA_ICS_SERIES`：寒武三系列映射同步；Pleistocene 边界 2.58
- `RCA_ICS_PERIODS` / `RCA_ICS_CN_PERIODS`：12 个系边界全部更新

---

## 4. 测试工程：补上缺失的数据验证层

### 4.1 新增 `tests/test_ics_invariants.py`（7 类不变量，27 项）

这是本次修复最重要的长期价值——**让数据事故在 CI 中不可重现**：

1. **字段健全性**：`base_ma > top_ma`；era 合法；必填字段齐全
2. **系级无缝拼接**：12 个系按年龄首尾相接，**无缺口、无重叠**（Cambrian 538.8 → Quaternary 0.0）
3. **系字段一致性**：每个条目的 `period_base_ma / period_top_ma` 必须等于其所属系的边界
4. **无孤儿界线**：每个顶界/底界都必须能与相邻条目或系界配对
   （重复别名条目 Wuliuan/Stage 5、Jiangshanian/Stage 9 与系列级条目 `Series 2` 均被容忍）
   - **按值白名单**放行唯一的形式缺口：上更新统（0.129–0.0117 Ma，非正式 Tarantian 亚统）
     没有正式阶，因此 Chibanian 顶 0.129 与 Holocene 底 0.0117 是**刻意的空档**
     （REVIEW-2026-07-31 已删除覆盖该段的伪 "Pleistocene" 条目）
5. **科学锚点**：P–T 251.902、K–Pg 66.0、白垩系底 143.1、第四系底 2.58；
   并以地质学语言钉死当年审计发现的 4 处矛盾
6. **数值查表**：12 个 Ma → 阶 的正确归属（含 144 Ma→Tithonian 这类随底界变更而翻转的用例）
7. **双端 parity**：正则解析 `js/ics_table.js`，与 JSON 逐字段比对（键集合 + top/base/era）

### 4.2 修正「锁定旧数据」的测试

| 文件 | 处理 |
|---|---|
| `tests/test_ics_cretaceous.py` | 白垩系 12 阶 + 奥陶系 5 阶参考值更新为 v2024/12，并写明旧断言锁定的是 GTS2016 值 |
| `tests/test_ics_cenozoic.py` | 21 个新生代阶更新；**Gelasian 的 period 断言 Neogene → Quaternary** |
| `tests/test_resolve_age_bound_stages.py` | `CHANGSHINGIAN_TOP` 251.9 → 251.902 |
| `tests_frontend.js` | `Santonian base 86.3→85.7`、`Dapingian base 470.0→471.3` |

---

## 5. 验证结果

| 项目 | 结果 |
|---|---|
| 全量 pytest | **846 收集 / 0 失败 / exit 0**（较修复前 819 增加 27 项数据不变量测试） |
| 下游影响面 | chart_mode / exporter / Darwin Core / PBDB / GUI 全部测试通过，**无消费方被破坏** |
| node tests_frontend.js | **287 通过 / 0 失败** |
| 数据完整性 | 98 条目、12 系无缝拼接、零孤儿界线（除上更新统刻意空档） |

---

## 6. 遗留事项与后续

### 6.1 已知的更新版本偏移（ICS v2026-06，尚未采纳）

官方图表已更新到 v2026-06，其中 3 处与本次采用的 v2024/12 不同：

| 单元 | v2024/12（已采用） | v2026/06 |
|---|---|---|
| Olenekian 底 | 249.9 | 250.8 |
| Anisian 底 | 246.7 | 247.0 |
| Wuchiapingian 底 | 259.51 | 259.857 |

文件名与测试引用均锚定 v2024/12，故本次保持单一版本一致性。
建议后续以独立版本升级（`ics_2026.json` + 同步测试引用）方式采纳，已在
`js/ics_table.js` 头部注释中记录。

### 6.2 需注意的学术争议

**Roadian 底 274.4 Ma**：Permophiles #78 指出该值由 273.01（Shen et al. 2020）改为
GTS2020 的 274.4，差值 1.4 Myr，**二叠纪分会自身认为"值得进一步讨论"**。
本项目以官方图表为准采纳 274.4，但若用户的 P–T 相关研究涉及瓜德鲁普统底界，
建议在论文中标注所采用的版本。

### 6.3 Sprint B（P1 14 项，未启动）

审查报告中的 P1 项尚未处理，优先级建议：
1. `aggregate.py:586-604` species 限定词恢复逻辑大小写不对称（静默丢失 "sp./cf."）
2. 提取流水线：截断响应无标志、缓存键模型维度
3. server/GUI：长任务阻塞、线程安全
4. JS↔Python quality 评分权重漂移（补同输入分数 parity 测试）
