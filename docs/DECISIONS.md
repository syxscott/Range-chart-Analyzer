# 决策点清单：同一个决策在几处实现

> ARCH-REVIEW-2026-11-02 §5 的收口建议。历史上每一轮审计发现的问题，几乎都出自「ONE decision, SEVERAL implementations, and a fix that landed on only one of them」（`tests/js/tests_diff_frontend_parity.js:35` 的原话）。本清单把这些决策点变成**可枚举数据**，每条标注当前状态与守护方式。

| 决策 | 实现处 | 状态 | 守护 |
|---|---|---|---|
| Python `float()` 词法（Unicode Nd、下划线、`inf`/`nan`） | `js/aggregate.js` `rcaPyFloat`、`js/minimax.js` `rcaPyFloatOrNull`、`js/table.js` `rcaPyFloatStr` | 3 份本地实现；**P0-1 分叉已修** | `tests/js/tests_pyfloat_parity.js`（CPython oracle + 两侧恒等）、`tests/js/tests_shared_js_parity.js`（源码恒等） |
| 模式形状探测 | `rca_core/shape.py`（唯一）、`js/table.js`（6 个镜像） | Python 已唯一化；JS 为跨语言必然双实现 | `scripts/gen_detector_parity.py` + `tests/js/tests_detector_parity.js`（24×6=144 项）+ `tests/test_detector_parity_fixture.py` |
| 排序用的物种键 / 名称清洗 | `rca_core/aggregate.py`、`rca_core/eval_metrics.py`、`rca_core/matrix.py` | **语义不同的三个函数**（不是复制品）：空白折叠 / ICZN 限定符 / 列名非字母数字删除 | 各自专项测试；此前的「_norm 有 4 份」是误判 |
| 缓存键字段集 | `rca_core/cache.py::build_extraction_key`（唯一） | 3 处手写调用点已收口，键字节不变 | `tests/test_extraction_key_factory.py` + `tests/test_cache_key_identity.py`（逐调用点 AST） |
| 提取扇出（超时/取消/进度） | `rca_core/fanout.py`（唯一） | 两个 GUI 薄适配层，保留各自返回契约 | `tests/test_gui_sprint_b.py` |
| 提取 wall-clock 预算 | `rca_core/budget.py`（核心）、`server.py:151`（刻意分叉） | **刻意分叉**：server 那份读可被测试改写的模块全局量，不是重复实现 | 各自测试；`docs/ARCHITECTURE.md` 已记录 |
| `err.*` 错误键 | `server.py` → `js/minimax.js` → `js/i18n.js` 三语 | 三处一致（17 / 33 / 38） | `tests/test_error_key_contract.py` |
| i18n 三语目录 + humanize fallback | `rca_core/i18n.py` + `js/i18n.js` | 跨语言双实现；fallback 规则已收进 `i18n.py` | `tests/js/tests_contrast.js` |
| ICS 地层表 | `rca_core/resources/ics_2024.json` + `js/ics_table.js` | 双实现（曾 98 vs 109 行分叉，偏老 20 倍） | `tests/test_ics_invariants.py` |
| `TABLE_CONFIGS` | `rca_core/exporter.py` + `js/table.js` | 双实现 | `tests/test_table_configs_parity.py` |
| 行级 `_warning` 标记形状 | `rca_core/row_flags.py`（唯一） | 单 flag=字符串 / 多 flag=列表 | `tests/test_grounding_parity.py` 等 |
| 导出列语义 / confidence 免责说明 | `rca_core/export_notes.py`（唯一） | 单一常量 + 机器可读字典 | `docs/DATA_DICTIONARY.md` |
| 能力落地到哪些端 | `rca_core/capability_matrix.py` | 数据声明 + probe 校验 | `tests/test_capability_matrix.py` |
| 模式能力矩阵 | `rca_core/modes.py`（唯一） | 10 模式 × 4 维度 | `tests/test_occurrence_matrix.py::test_d5` 等 |

## 明确裁决：三项「物理合并」被否决

| 评审建议 | 复核结论 | 理由 |
|---|---|---|
| A1 抽 `js/pyfloat.js` 共享文件 | **否决物理抽取，改恒等锁** | 仓库无模块系统、无构建步骤：`index.html` 用 14 个 `<script defer>`，Fluent 桌面把 3 个 JS 内联进 QWebEngineView，而 `tests_aggregate.js` **刻意单独加载 `js/aggregate.js`**（其文件头写明）。抽共享文件要改约 10 个加载点并破坏「每个 harness 能独立加载单文件」这一测试架构基石。架构评审里「改 1 行脚本列表」的前提不成立 |
| A5 抽 `js/warnings.js`（所谓 105 行重复） | **同上；且规模事实有误** | 实测是 `js/quality.js:105` 开始的 **6 行**函数，与 `js/table.js:2484` 逐字节相同（改名后）。已加源码恒等 + 行为恒等断言 |
| A12 把 `extractor.py` 的 `image_hash`/`cache` 模块级 import 改延迟 | **复核不成立，不改** | `compute_image_sha256_from_b64` 在 `extractor.py` 有 **12 处**引用（4 次真实调用），不是评审说的 1 次；`cache` 本来就是函数内延迟导入。为 1–2 次调用付一条模块级边才是可优化的情形，这里不是 |

## 仍然存在的真实重复（诚实记录）

- JS 与 Python 的 15,298 行孪生实现（占 JS 全量 64%）——跨语言无共享运行时，只能靠差分套件锁。
- 上述 3 份 Python 浮点词法实现——无构建步骤下的现实折中，已由恒等锁消除漂移风险。
- `server.py` 的 995 行 `_handle_extract_body` 与两个 GUI 的多轮编排——阶段 3 的 `pipeline.py` 工作，见下。

## 阶段 3 待办（本轮未做，附原因）

`transport.py` 解 `llm↔protocols` 环、`modes_impl/` 拆 7,055 行 extractor、`pipeline.py` 下沉 995 行 HTTP handler：三项都需要「新旧并行 + 差分验证」，一次提交做完会动摇 4,385 项测试的信任基础。按评审自身建议分批。
