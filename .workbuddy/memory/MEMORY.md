# 项目长期约定

- 实现类任务默认由主流程独立完成、无需多 Agent 交叉复核；仅当用户明确要求（如 2026-09-01 的全面代码审查）时才组织多 Agent 团队。
- 宣称修复完成前必须以实际测试为依据：至少覆盖相关定向测试、全量 Pytest、独立核心测试、JavaScript 测试、编译/语法检查，并明确报告可选依赖或真实科研验证限制。

## 关键工程事实（供后续提问定位）

- 三端共用 `rca_core`（Fluent GUI / Tkinter GUI / server.py），浏览器 `js/` 是 Python 核心的对等实现（prompt、json_utils、normalize、aggregate、quality 均有 parity 测试锁定，改动必须双端同步）。
- 四提取模式：range_chart / columnar_section / abundance_diagram / phylogenetic_tree；prompt 版本号参与缓存键。
- 测试基线（2026-09-01 Sprint A 后实测）：pytest = 846 收集 / 0 失败 / exit 0（Anaconda python + pytest 8.3.4；shell 默认 python 是 WorkBuddy 托管 3.13.12，无 pytest）；node tests_frontend.js = 287 过；数据不变量锁在 tests/test_ics_invariants.py。
- ✅ P0 已修复（2026-09-01）：ics_2024.json 已按 **ICS v2024/12** 全面重建（98 条目，新增 Jiangshanian/Stage 2-4；约 70 个界线数值修正；Gelasian 已归 Quaternary；P–T 锚点 251.902），三方权威交叉验证（stratigraphy.org + Macrostrat #1 + PBDB），js/ics_table.js 与 ics.py 已双端同步，并补齐数据 invariant 测试。完整报告 docs/CODE_REVIEW_2026-09-01.md，修复记录 docs/FIX-2026-09-01-ICS-data.md。
- ⚠️ 教训（务必记住）：**测试可能锁定过期数据**。原 test_ics_cretaceous.py / test_ics_cenozoic.py 自称引用 ICS v2024/12，实际锁的是 GTS2016 值。任何"权威数据"改动都必须回到 stratigraphy.org 官方图表逐条核对，不能信测试里的自述版本。
- 待办：ICS v2026-06 偏移（Olenekian 底 250.8、Anisian 底 247.0、Wuchiapingian 底 259.857）待独立版本升级采纳；Roadian 底 274.4 在二叠纪分会内部有争议（Permophiles #78），涉瓜德鲁普统底界的研究需标注采用版本。Sprint B（P1 14 项，含 aggregate.py L586-604 species 限定词大小写 bug、提取流水线截断/缓存键、server 长任务阻塞、JS quality 评分漂移）。
- 数据目录：~/.range_chart_analyzer/（providers.json / rca.db / extract_cache.sqlite）。
- 领域数据权威源：rca_core/resources/ics_2024.json（与 js/ics_table.js 必须一致）。
