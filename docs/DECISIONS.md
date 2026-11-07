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
- `server.py` 的 997 行 `_handle_extract_body` 与两个 GUI 的多轮编排——**HTTP 那份已在本轮下沉**（`rca_core/pipeline.py`），两个 GUI 仍各自保留薄适配层，见下。

## 阶段 3 · P1-4：`modes_impl/` 拆分与 `normalizer_ref` 状态（本轮落地）

**结论：`normalizer_ref` 仍是「声明但未接线」的前瞻性字段。拆分后 10 个引用已可解析，但调度点一行未动；接线属 G8，需人工裁定，本轮不做。**

- **落地方案 C（真叶子，零反向边）**：`rca_core/modes_impl/_shared.py` 是 LEAF 模块。从 17 个搬运函数出发求出的模块级依赖**传递闭包 101 个名字**中，94 个定义（55 个常量赋值 + 1 个 annassign + 38 个函数）按 AST 节点范围 **verbatim**（逐字、保持原文件顺序）搬进 `_shared.py`；另 7 个名字原本就是 extractor 从 `rca_core.reason_codes` / `rca_core.row_flags` 两个 stdlib-only 叶子模块 import 的，`_shared.py` 用绝对 import 从这两个叶子模块取（**同一个对象**，无复制）。`_shared.py` 没有任何 `from ..extractor import ...` / `import rca_core.extractor`，模块级依赖只有 stdlib + 这两个既有叶子模块。
- 依赖方向：`extractor → modes_impl._shared`、`modes_impl.<mode> → modes_impl._shared`，**单向、零反向边**。`extractor.py` 在文件末尾 `from .modes_impl._shared import <101 名>` + `from .modes_impl.<mode> import <17 名 + 别名>` 一次性 re-export，所有 `rca_core.X` / `rca_core.extractor.X` / 测试桩 / GUI / server 的查找仍是**同一个对象**（含 `E._normalize_phylogenetic_tree_into`）。末置原因：闭包定义搬到 `_shared.py` 后，extractor 模块体里已无这些名字，必须在体执行完（叶子模块可用）后再一次性绑回。
- **否决过的第一版设计（记录在案，防止回退）**：初版让 `_shared.py` 用 `from ..extractor import <61 名>` 做「命名空间桥」，同时 extractor 末尾 re-export 模式模块——这在**模块级**构成 `extractor → modes_impl.<mode> → _shared → extractor` 双向环，而 `tests/test_rca_core_acyclic.py` 的 `_import_graph()` 只扫 `CORE.glob("*.py")` 根层、看不到子包，会漏检。方案 C 用「闭包整体 verbatim 下移成叶子」消除该环。
- **护栏洞本身也补上了**：`tests/test_rca_core_acyclic.py` 的 `_import_graph()` / `_import_time_graph()` / `_edges_by_scope()` 已从 `CORE.glob("*.py")` 改为 `CORE.rglob("*.py")`（跳过 `__init__.py` 与 `__pycache__`），节点名改为相对 `rca_core/` 的点路径（`extractor`、`modes_impl._shared`），相对导入按 `level` 精确解析（`from .shape`→`shape`；`from .modes_impl.range_chart`→`modes_impl.range_chart`；`from ..extractor`→`extractor`），并一并识别绝对 `rca_core.*` 与 `from . import name` 别名形式。`KNOWN_CYCLES` **未改**（递归扫描 64 个节点后 `full-graph cycles` 仍恰为 `[['llm','protocols']]`、`import-time cycles: []`，未发现其它既存子包环），`test_import_time_graph_is_strictly_acyclic` **未放宽**。新增 `test_relative_import_targets_are_resolved_by_level`、`test_recursive_scan_sees_the_subpackages`、`test_modes_impl_has_no_edge_back_to_extractor` 三项钉住解析公式与零反向边；`test_core_layer_does_not_import_ui_or_web_frameworks` 也改为递归扫描（实测 `UI/web offenders: NONE`）。新建的 `tests/test_modes_impl_acyclic.py` 从包内视角再查一遍 (a)(b)(c) + 合并图零环。变异测试：临时插一行 `from ..extractor import _carry_extras` 后，两套守卫分别 `3 failed, 4 passed` 与 `5 failed, 1 passed`（**含原先看不见的 `test_import_time_graph_is_strictly_acyclic` 变红**），字节级恢复 + `cmp` 后全绿。
- 侦察数字（重算，比初报更精确）：11 个公开 `normalize_*` → 直接共享名 61 个（初报 72 是含未搬运函数的粗算）→ 沿 helper 递归的传递闭包 **101 个名字**（初报 112 同理）。19 个候选函数**均无 `global`/`nonlocal` 声明、无模块级可变状态写入**；闭包内定义对闭包外 extractor 全局的引用为 **NONE**（symtable 精确校验），模块级语句（含 class body）对闭包名的引用为 **NONE**，模块级 Call 只有 `logging.getLogger(__name__)` 与 `_MODE_SPECS` 推导两条（都不碰闭包），所以末尾 re-export 不会造成 import 期 NameError。
- 搬走的内容：`rca_core/extractor.py` 的 10 个模式归一化器 + 7 个私有 `_normalize_*` helper = **17 个函数体**，按 AST 抽取 **verbatim**（含历史 quirk，未重写），分别放进 `rca_core/modes_impl/<name>.py`；`normalize_phylogenetic_tree_result` 原本是 L4037 的模块级别名（不是 def），别名一并 verbatim 搬入 `phylogenetic_tree.py` 并由末尾 re-export 绑回。
- 留在 `extractor.py` 的：`normalize_chart_classification`（视觉分类器归一化器，被 `classify_chart_image` 调用）及其依赖 `KNOWN_CHART_TYPES`——两者不在闭包内。`normalize_pos_0_999` 虽是几何工具，但被闭包里的 `_geometry_point` / `pos_to_axis_value` 依赖，其定义也 verbatim 搬进 `_shared.py`，再由末尾 re-export 回到 `extractor.normalize_pos_0_999`（公开 API 不变）。其余 ~3,500 行提取逻辑不动。
- 唯一一处逐字例外（已实测，不是猜测）：`_normalize_species_into` 体内有**一条函数内相对导入** `from .bed_parser import label_ordinal, ordinal_kind`。函数搬到子包后该行改成 `from ..bed_parser import ...`（只多一个点），否则 `ModuleNotFoundError`；初次定向测试正是这样抓出来的（`test_extractor_species_fields.py`、`test_review_2026_09_10.py`、`test_frontend_parity_fixtures_2026_09_20.py` 等 6 处红），修完复跑全绿。AST 扫描确认 17 个搬运函数里只有这一条函数内 import，除此之外**没有一个字节**被改动。
- 已知的行为差异（诚实记录，方案 C 下依旧存在）：模式模块 `@import` 时把共享名绑进自己的命名空间，而 `extractor.py` 是末尾 re-export 绑进自己的命名空间；**若有人 monkeypatch `extractor._carry_extras` 之类私有 helper，留在 extractor 的函数会看到 patch，搬走的归一化器不会**（它们走 `_shared`）。当前全仓没有任何此类桩（实际桩只打在 `normalize_result`、`_MODE_DISPATCH`、`classify_chart_image`、`call_llm_api`、`_call_llm` 等顶层名字，re-export 后仍生效），生产与测试行为零变化；将来引入这类桩时必须同步评估。
- 随之更新的既有护栏：`tests/test_extractor_nonfinite_confidence.py` 的「恰好一个 confidence clamp」AST 守卫原本只扫 `extractor.py`，`_confidence_clamped` 搬到 `_shared.py` 后会误报 0；已改为**同时扫两个文件并仍断言全局恰好 1 个 clamp**（比原来更强：新模块里再出现第 14 个手写 clamp 也会红），不变量本身未放宽。
- 守护：`tests/test_normalizer_refs_resolve.py` 遍历 `MODES`，对每个非空 `normalizer_ref` 用 `importlib` 解析、断言可调用、断言 `getattr(extractor, name) is resolved`，并**反向锁住「调度点未使用此字段」**；`tests/test_modes_impl_acyclic.py`（新增）断言 modes_impl 内 (a) 模块级无指向 extractor 的 ImportFrom、(b) 模块级相对导入全在包内、(c) 无 `import rca_core.extractor`，并额外做「根层 + 子包合并的模块级导入图零环」检查与变异测试（临时插入 `from ..extractor import _carry_extras` 会 5 项红）。原有 `tests/test_occurrence_matrix.py::test_d5_every_registry_reference_resolves` 继续负责「字符串可解析」。

## 阶段 3 · P1-5：`transport.py` 解 `llm↔protocols` 环 —— 复核后**不做**

不是遗漏，是有证据的否决：`rca_core/protocols.py` 的 12 处 `from .llm import ...` 全部是**函数内延迟导入**，其目的正是保住运行时打桩。`tests/test_protocol_adapters_2026_10_20.py::test_the_adapters_reach_the_patched_transport` 直接把 `llm._post_json` 换成假函数并断言 adapter 走到它；`tests/test_review_2026_09_10.py`、`test_reasoning_effort_2026_10_19.py`、`test_stream_thinking_2026_10_19.py` 同样打在 `llm._read_response` / `_read_response_stream` 上。若把传输原语搬进 `transport.py` 并让 `protocols.py` 改从 `transport` 导入，这些桩全部打不到 adapter（import 时绑定的对象与 `llm` 命名空间里的替换无关）。此外 `protocols.adapter_for` 还依赖 `llm._adapters()` / `llm.ADAPTERS` / `llm.ApiFormat`，而 `test_a_fourth_format_is_one_class_and_one_line` 正是通过改 `llm.ADAPTERS` 验证插件性。在不改这些已固化契约、不引入 `sys.modules` 旁路或模块 `__setattr__` 魔法（等于「假解环」）的前提下，做不到「`protocols.py` 不再 import `llm`」与「`llm._*` 打桩仍可达」两者兼得。`tests/test_rca_core_acyclic.py` 的 `KNOWN_CYCLES` 棘轮因此**保持现状**，环仍被两条守卫钉在函数内导入上。

## 阶段 3 · P0-1：`pipeline.py` 下沉 HTTP handler（本轮落地）

`server.py::Handler._handle_extract_body` 从 **997 行降到 24 行**，编排逐字搬进 `rca_core/pipeline.py::run_extraction_request`。**机械变换，不是重写**：24 处 `deps.<name>`、18 处 `self._send_json` → `emit`，外加 3 处 socket 事实（读 body / 标记 consumed / 发送响应）留在 `server.py`；由 `scripts/audit/split_extract_handler.py` 用 AST 定位的「行列替换」生成，而不是手工重打。server 本地协作者通过 frozen dataclass `PipelineDeps` 注入，`rca_core` 仍不 import HTTP 层（递归导入图实测 `pipeline` 是单向节点，无环）。

- **行为锁**：`tests/test_extract_pipeline_parity.py` 用 duck-typed recorder 驱动**真实 handler**（该方法只碰 `_read_body_with_deadline` / `_send_json` / `_request_body_consumed` 三个 `self` 成员），回放 19 条**本地终止**请求，与改动前录制的 golden **逐字节**比对（`scripts/audit/record_extract_baseline.py`）。golden 里原本有一条 `bad_api_format`，实测它真的打到了远端 provider（返回带真实 `request_id` / `latency_ms` 的 401），既不确定也依赖网络，已剔除——网络相关的 golden 不是 golden。
- **AST/文本等价性**：搬迁前后 `emit` 调用 18 处、`return` 17 处、`PipelineDeps` 16 个字段「全部使用且无多余」、函数自由名 0 个未解析，全部实测一致。
- **随之迁移的源码位置断言（不变量不变，只换解析对象，均未放宽）**：`tests/test_review_2026_07_31_core.py` 的「`merged_usage` 先于审计写入」改为解析 `pipeline.py`；`tests/test_audit_2026_09_27.py` 的「import 先于 use」改为解析 `run_extraction_request`；`tests/js/tests_frontend.js` 的 `cls-server-auto-whitelisted` 改为「在 `server.py` + `pipeline.py` 合并文本里找 registry import，在含 `_SUPPORTED_MODES` 的那个文件里找显式 `{"auto"}`」。

## 阶段 3 · P2：几何引擎与图版自动鉴定（部分落地，边界明确）

- **已落地（可验证）**：`rca_core/axis_detect.py` 确定性测刻度与曲线（合成图真值：刻度 ≤2 px、曲线 ≤4 px、拟合斜率误差 <1e-3；Otsu 极性判定支持黑底白线）；`rca_core/plate_segment.py` 确定性图版分割（3×3 合成图 9 个 crop、bbox 逐像素正确；`layout: grid|irregular|empty`，不规则版式可 `drop_irregular=True` 显式拒绝）。两者都**不 import 模型层**，有测试钉住这一条。
- **未落地（受外部资源限制，不是遗漏）**：分类器推理。仓库内没有任何放射虫分类权重（唯一的 `best.pt` 属于无关仓库）、也没有标注集，因此**无法测量精度**。按本项目「不交无法验证的接口」的标准，**不提供会返回标签的 `predict()`**；接入点是 `segment_plate()` 输出的 crop。
- **几何侧仍缺一步**：`pos_0_999 → 原图像素` 需要像素锚点。实测（`docs/OVERLAY-COORD-AUDIT.md`）：`pos → 数据值` 精确线性（60 字段残差 <5e-7），`pos → 像素` 数据不足。拟合能力 `rca_core/geometry.py` 已具备，缺的是采集入口。

## 阶段 3 收口状态

三项全部有结论：`modes_impl/` **已落地**（见 P1-4）、`pipeline.py` **已落地**（见 P0-1）、`transport.py` **有证据否决**（见 P1-5）。没有「待办但没说清原因」的残留项。
