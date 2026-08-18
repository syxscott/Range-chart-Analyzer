# Range-chart Analyzer 代码与古生物科研严谨性审阅

**日期：** 2026-07-29  
**方式：** 主审静态审阅 + 动态复现 + 测试执行  
**代码状态：** 工作区存在大量未提交修改；本报告评价的是审阅时的工作树，而不是仅评价 `4989097` 提交。

## 结论

当前版本**不能认定为专业级、可直接发表/入库的科研软件**。它已经具备较成熟的工程骨架、较广的自动化测试和较好的溯源意识，可作为**研究辅助、预标注与人工复核工具**；但两个已复现的高危科学正确性缺陷足以阻止 DwC/PBDB 直接入库：

1. 床号会被当作 Ma 年龄解析，污染 Darwin Core / PBDB 年代字段。
2. 质量评分对混合“可解析床号 + 不可解析范围文本”会抛出 `TypeError`，可使一次成功抽取在返回前失败。

此外，未知的产状与端点状态被默认写成 `in_situ` / `observed`，逐行置信度和数值范围索引未成为一等字段，且目前没有真实、专家双盲标注的数据集来证明模型的实际准确度。综合成熟度约 **6.2/10**：工程实现约 7.5/10，科研验证约 4.5/10。

## 已证实问题

### P0 / Critical — 床号被误判为 Ma，导出年代严重错误

**位置：**
- `rca_core/standards/ics.py:117-153`
- `rca_core/standards/darwin_core.py:176-191`
- `rca_core/standards/pbdb.py:25-40, 110-138`

`ics_resolve_age_bound()` 对任意包含数字的文本先取第一个数字，并未要求 `Ma` / `Myr` 单位。于是 `Bed 9 (Yinkeng Fm base)` 被解析为 `9 Ma`，映射到 `Tortonian`；`Bed 7` 被映射为 `Messinian`。

**动态复现：**

```text
ics_resolve_age_bound("Bed 9 (Yinkeng Fm base)") -> ("Tortonian", 9.0)
DwC: Bed 7 / Bed 9 -> earliest=Messinian, latest=Tortonian
PBDB: early_interval=Messinian, late_interval=Tortonian,
      max_ma=7.0, min_ma=9.0
```

这不仅年代完全错误，PBDB 输出还出现 `max_ma < min_ma`。对二叠—三叠纪图件，它会凭空生成中新世年龄，属于会直接污染下游分析与数据库的阻断级缺陷。

**修复要求：**
- 只有文本明确带 `Ma` / `Myr` 等年龄单位时才解析数值年龄。
- `Bed 9`、`Sample 5`、层号等必须走床号逻辑；若只有床号而没有床—年龄标定，不得伪造绝对年龄。
- 导出前强制验证 `max_ma >= min_ma`；失败则留空并写入不确定性/错误字段。
- 优先使用显式 `range_top_idx/range_base_idx`，但仅作为相对层位，不能直接当 Ma。

### P0 / High — 质量评分可在成功抽取后崩溃

**位置：**
- `rca_core/quality.py:723-729`
- 调用链：`server.py:853-864, 952-959, 1054-1060`

`_score_biozone_order()` 使用 `_parse_bed_n(range_top)` 作为排序键。实际结果常混合 `Bed 2` 与 `unclear`、阶名或空值，排序时会比较 `int` 与 `None`：

```text
TypeError: '<' not supported between instances of 'NoneType' and 'int'
```

`server.py` 多处直接调用 `score_range_chart()`，没有隔离评分异常。因此 LLM 已经成功返回的数据可能因为附加质量评分失败而无法正常返回或命中缓存。

**修复要求：**
- 排序键改为 `(value is None, value or sentinel)`，或预过滤无法排序的行。
- `score_range_chart()` 应维持纯函数、永不抛出；服务器侧仍应以 `try/except` 隔离评分失败，抽取结果优先返回。
- 增加混合 `Bed N`、阶名、`unclear`、空值的回归测试；当前专项 24 项测试没有覆盖此情形。

### P1 / High — 未知科学状态被改写为确定事实

**位置：** `rca_core/extractor.py:438-457, 461-496`

缺失或非法 `occurrence_mode` 默认变成 `in_situ`；缺失或非法 `endpoint_kind` 默认变成 `observed`。动态复现表明，仅有 `{"species":"A"}` 的输入会自动得到：

```json
{"endpoint_kind":"observed", "occurrence_mode":"in_situ"}
```

“未知”与“原位/直接观察”科学含义不同。此默认值会系统性低估再沉积、搬运、截断或投影的不确定性，且随后会被 DwC 导出为机器可读字段。

**修复要求：** 增加 `unknown` 枚举或保留空值；只有模型明确读到图例/注记时才能写 `in_situ` 或 `observed`。

### P1 / High — Prompt 请求的关键数值与逐行置信度被降级到 `_extras`

**位置：**
- Prompt：`rca_core/prompt.py:81-89`
- 归一化：`rca_core/extractor.py:309-322, 461-496`
- 聚合：`rca_core/aggregate.py:481-499`

Prompt 要求 `range_top_idx`、`range_base_idx`、逐行 `confidence`，但归一化行没有这些一等字段；它们被塞进 `_extras`。多次运行合并后 `_extras` 甚至可能被转为字符串，0.2 与 0.8 的逐行置信度无法形成可解释的聚合结果。

**科研影响：** 无法可靠执行基于层号的 FAD/LAD 校验、无法传播逐行不确定性，也不利于人工审阅和标准导出。

### P1 / High — 没有真实世界准确度证据

**位置：**
- `tests/fixtures/gold/README.md:24-84`
- `tests/test_gold_smoke.py:46-84`

现有 gold 数据是 **0 个真实案例 + 8 个合成案例**。真实 LLM smoke 测试被默认 `skip`，而且其断言只检查返回对象具有 `ok` 属性，并不验证抽取正确性。

因此当前测试只能证明指标数学和管线基本行为，不能证明：
- 旋转/斜体/低分辨率学名 OCR 的准确率；
- 中英日俄地层术语转换的领域正确率；
- 开放命名法、异名、再沉积与图例解释的真实表现；
- 质量等级 A/B/C 与专家判断之间的校准度。

### P1 / Medium — 分类学评估指标会把限定鉴定算作完全正确

**位置：** `rca_core/eval_metrics.py:18-57, 84-96`

`_normalize_taxon()` 去掉 `cf.`、`aff.`、`?` 等限定词。动态复现中，预测 `Genus cf. alpha` 对真实值 `Genus alpha` 得到 precision/recall/F1 全部 1.0。

聚合模块正确保留了这些限定词，但评估指标又把它们抹掉，导致报告的分类学准确率偏高。建议同时报告：严格匹配、去限定词宽松匹配、属级匹配、作者与年份匹配。

### P1 / Medium — 年代表缺少来源元数据和更新机制

**位置：** `rca_core/resources/ics_2024.json`、`rca_core/standards/ics.py:16-26`

JSON 表没有在文件内记录 ICS chart 版本、发布日期、下载 URL、校验和与生成脚本。虽然新生代边界已有专项测试，但测试值与代码仓库本身同源，不能替代对官方源数据的独立校验。科研发布前应提供可复现的来源链与版本锁定。

## 做得较好的部分

- `rca_core` 模块边界总体清楚，抽取、聚合、质量、历史、标准导出分工明确。
- 多次运行合并对重复行计数、嵌合共识、ICZN 限定词做了较多防御；`Genus cf. alpha` 与 `Genus alpha` 不会被错误合并。
- DwC-A 已使用 TAB，`basisOfRecord` 已改为 `MachineObservation`，并开始保留岩石地层与端点/产状信号。
- 安全层包含 SSRF、CSRF、公式注入、API Key 加密降级警告等设计；测试覆盖广。
- 历史/原始响应、prompt version、图片哈希等溯源方向正确。

## 测试与运行证据

- `compileall`：核心、入口、Tkinter/Fluent GUI 均通过。
- pytest：**687 collected；671 passed，16 skipped，1 warning，43.51s**。
- `tests_core.py` 独立运行：**375 passed，0 failed**。
- 质量专项：**24 passed**，但未覆盖已复现的混合排序崩溃。
- gold smoke：**5 passed，4 skipped**；被跳过的正是实时 LLM 集成部分。
- 警告：OS keyring 不可用时 API Key 加密仅为可逆混淆；同机用户可重建密钥。

README 声称全量约 815 项，但当前实际收集为 687 项，文档需同步。测试全绿并不反驳上述问题，因为对应输入组合和语义约束没有被覆盖。

## 多 Agent 执行情况与局限

按要求启动了四个独立角色：架构、古生物科学性、QA、界面集成。但四个子任务均在启动后因鉴权错误失败：**HTTP 403，接口仅允许 WorkBuddy 客户端**。它们没有返回任何审阅内容，本报告没有冒用或虚构它们的结论；全部结论来自主审的代码阅读、测试运行和动态复现。

若要满足严格意义上的“多 Agent 交叉审阅”，应在支持该鉴权接口的 WorkBuddy 客户端环境中重试，并让领域 Agent 独立复核本报告的 P0/P1 发现。

## 达到专业科研软件水平的最低门槛

1. 修复床号→Ma 污染与质量评分崩溃，并新增回归测试。
2. 未知状态不得默认成 observed/in_situ；逐行 confidence/note/idx 必须一等化并贯穿导出。
3. 建立真实、合法授权、跨期刊风格的专家金标准：至少双人盲标、冲突仲裁、记录一致性；分别报告严格与宽松分类学指标。
4. 用真实图件执行 provider/model/version 固定的基准测试，给出置信区间、错误分层和失败样例；校准质量分数而不是凭经验设 A/B/C 阈值。
5. DwC/PBDB 输出通过独立 schema/导入器验证；每次导出附软件版本、prompt、模型、时间标版本、原图哈希和人工修订记录。
6. 增加 CI、覆盖率门槛、依赖锁定、数据版本生成脚本、发布包验收与可重复构建。

在这些门槛完成前，建议产品明确标注：**“AI 辅助预提取；所有分类学、FAD/LAD、年代与再沉积解释须由专业人员复核；PBDB/DwC 导出暂不应直接入库。”**
