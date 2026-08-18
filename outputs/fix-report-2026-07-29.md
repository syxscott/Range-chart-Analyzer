# Range-chart Analyzer 已知缺陷修复报告

## 完成范围

本轮完成了审阅中所有可直接通过代码修复的已知高优先级缺陷，并补充了对应回归测试。

### 1. 地质年龄与科研导出

- `rca_core/standards/ics.py` 只把带 `Ma`、`Myr`、`Mya`、`million years ago` 等显式单位的数字解析为绝对年龄。
- `Bed 9`、`Sample 7` 等床号/样品号不再被解释为 9 Ma / 7 Ma。
- `rca_core/standards/darwin_core.py` 拒绝导出方向反转的完整数值年龄对。
- `rca_core/standards/pbdb.py` 要求显式年龄单位，并强制 `max_ma >= min_ma`；不再拼接来源不同的单边年龄。

### 2. 质量评分稳定性

- `rca_core/quality.py` 跳过无法定位床位的记录，避免 `None` 与整数混合排序崩溃。
- 四个质量维度独立隔离；失败维度返回结构化告警，不再抛出并阻断评分。
- `server.py` 通过统一安全包装器隔离附加质量评分，成功抽取不会因评分异常而丢失。

### 3. 科研字段与不确定性传播

修改了 `rca_core/extractor.py`、`rca_core/aggregate.py`、`rca_core/prompt.py` 及 JavaScript 镜像：

- `endpoint_kind` 与 `occurrence_mode` 支持 `unknown`。
- 缺失或非法状态不再伪装成 `observed` / `in_situ`。
- 旧 `reworked: bool` 仍兼容映射为明确的 `reworked` / `in_situ`。
- `range_top_idx`、`range_base_idx` 成为正式 `int | null` 字段。
- 逐行 `confidence` 成为正式 `[0,1] float | null` 字段。
- 多次运行中索引使用保类型稳定众数；逐行置信度使用有效值平均。
- `_extras` 递归保持字典结构，不再产生 Python `str(dict)` 或 JavaScript `[object Object]`。
- range-chart Prompt 版本从 `v3` 升级到 `v4`，缓存会自动失效旧提示词结果。
- 修复了 `js/prompt.js` 末尾已有的换行字符串语法错误。

### 4. 分类学评估口径

- `rca_core/eval_metrics.py::_normalize_taxon()` 默认严格保留 `cf.`、`aff.`、`?`、`ex gr.`、`s.l.`、`s.str.`、`sp.`、`spp.`。
- `species_precision_recall()` 顶层结果现在是 strict 指标，并附带 `lenient` / `qualifier_insensitive` 指标。
- `Genus cf. alpha` 与 `Genus alpha` 在 strict 模式下不再得到虚假的满分；宽松匹配仍被明确报告。

## 验证结果

- 完整 Pytest：`661 collected`，`645 passed`，`16 skipped`，`0 failed`。
- 独立核心测试：`375 passed`，`0 failed`。
- 浏览器端前端测试：`117 passed`，`0 failed`。
- JavaScript 聚合测试：`45 passed`，`0 failed`。
- JavaScript 对比测试：`34 passed`，`0 failed`。
- 科研字段与 Python/JS parity 定向测试：`41 passed`。
- 分类学指标定向测试：`29 passed`。
- Prompt 与缓存版本测试：`16 passed`。
- Python `compileall`：通过。
- 基础入口与核心模块导入：通过。
- `js/prompt.js --check`：通过。
- `git diff --check`：通过；仅有 Git 的 LF/CRLF 转换提示。

## 未消除的限制与风险

1. **真实科研金标准不足**：现有 gold fixtures 主要是合成数据，不能证明跨期刊、跨语言、跨图件风格的真实准确率。
2. **API Key 静态保护**：当前环境缺少可选 `keyring`，测试会警告本地回退方案只是混淆保护。生产科研环境应安装并启用 OS keyring/keychain。
3. **Fluent GUI 可选依赖**：隔离运行时缺少 `PySide6`，因此 `gui_fluent.py` 未完成实际导入/启动验证；基础 `app.py`、`main.py`、`server.py`、`gui.py` 与核心模块已验证。
4. **多 Agent 复核未完成**：新启动的 Python 和 JavaScript 实现 Agent 均因 HTTP 403 鉴权失败，没有生成代码或结论；本轮修改与验证由主流程完成。
5. **工作区已有大量未提交修改**：本轮采取最小范围编辑，但当前 Git diff 仍包含用户此前的大量改动，提交前应人工分组审阅。

## 科研软件成熟度结论

已知的高危语义缺陷和稳定性缺陷已经修复，程序更适合作为科研辅助抽取、预标注和人工复核工具。但在真实专家双盲金标准、置信度校准、独立 DwC/PBDB 导入验证完成前，仍不应宣称可无人复核地直接发表或入库。
