# 修复交付概览

## 已完成

- 修复床号/样品号被误解析为绝对年龄，并保护 Darwin Core / PBDB 地质年龄方向。
- 修复质量评分混合排序崩溃，增加核心逐维和服务端双层异常隔离。
- 将 `unknown`、范围索引和逐行置信度贯穿 Python/JavaScript 归一化与多运行聚合。
- `_extras` 现在保持结构化，不再字符串化。
- 分类学评估默认严格保留开放命名法限定词，并同时报告显式宽松指标。
- 修复 `js/prompt.js` 语法错误，range-chart Prompt 升级为 `v4`。

## 验证

- Pytest：661 collected，645 passed，16 skipped，0 failed。
- 独立核心测试：375 passed。
- 浏览器端测试：117 passed。
- JavaScript 聚合/对比测试：45 + 34 passed。
- Python 编译、核心入口导入、JavaScript 语法和 Git diff 空白检查均通过。

## 尚存限制

- 真实专家金标准仍不足，不能证明可无人复核地直接发表或入库。
- 当前环境缺少可选 `keyring` 与 `PySide6`；API Key 回退仅为混淆保护，Fluent GUI 未完成实际启动验证。
- Python/JavaScript 实现 Agent 均因 HTTP 403 鉴权失败，未产出内容；修复与验证由主流程完成。
- 工作区包含大量既有未提交修改，提交前应人工分组审阅。

详细说明见 `fix-report-2026-07-29.md`。
