# 项目长期约定

- 用户明确不需要多 Agent 交叉复核；后续由主流程独立完成实现与严格验证即可。
- 宣称修复完成前必须以实际测试为依据：至少覆盖相关定向测试、全量 Pytest、独立核心测试、JavaScript 测试、编译/语法检查，并明确报告可选依赖或真实科研验证限制。

## 关键工程事实（供后续提问定位）

- 三端共用 `rca_core`（Fluent GUI / Tkinter GUI / server.py），浏览器 `js/` 是 Python 核心的对等实现（prompt、json_utils、normalize、aggregate、quality 均有 parity 测试锁定，改动必须双端同步）。
- 四提取模式：range_chart / columnar_section / abundance_diagram / phylogenetic_tree；prompt 版本号参与缓存键。
- 测试基线（2026-08-06 实测）：pytest tests/ + tests_core.py = 785 过（9 skip）；node tests_frontend.js = 138 过。
- 数据目录：~/.range_chart_analyzer/（providers.json / rca.db / extract_cache.sqlite）。
- 领域数据权威源：rca_core/resources/ics_2024.json（与 js/ics_table.js 必须一致）。
