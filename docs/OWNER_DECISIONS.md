# 需要项目所有者拍板的事项（不是开发任务）

> 2026-11-07 汇编。每项给出：为什么必须人工裁定、可执行的默认方案、裁定后对应哪段代码。在这些项裁定前，代码保持现状并显式标注版本/形态，**不猜测、不默默替用户决定**（科研工具的版本选择会影响已发表文献的可比性）。

| # | 决策 | 为什么不能由开发代拍 | 建议默认 | 裁定后要动的地方 |
|---|---|---|---|---|
| G1 | **Roadian 底界**（Permophiles #78 争议）→ 是否采纳 ICS v2026-06 三处偏移 | 采纳后标尺与部分已发表文献不一致；这是古生物学家的判断，不是代码问题 | **暂不采纳**，保持 `ICS v2024/12` 并在导出里写明版本戳；标尺变更单独发一个 release，便于论文分别引用 | `rca_core/standards/ics.py:47 ICS_VERSION`、`rca_core/resources/ics_2024.json`、两侧 `js/ics_table.js`、`scripts/update_ics.py` 已有完整管线（Macrostrat + ICS RDF 双通道、`--write-canonical` 显式晋级、拒绝 fixture 晋升） |
| G2 | **overlay 形态**：真 overlay（端点映射回原图坐标）vs 半真；两端都做还是先一端 | 成本 10–24 人日且取决于你实际复核工作流：是否真的需要点表格行跳到原图 | 先花 0.5 人日实测 `pos_0_999`（归一化轴位置，非像素）与真实像素的对应关系；架构师实测 `axis_calibration` 只存轴值、不存像素锚点，**当前数据不足以反查原图坐标**，所以先做半真（在归一化坐标系上标注）更稳妥 | `rca_core/geometry.py`、`rca_core/redraw.py`、`js/viz.js`、导出 schema 是否加坐标字段 |
| G3 | **server 部署形态**：自建 / 内网多用户 / 云 VM | 决定 `RCA_PROVENANCE_TOKEN` 非 loopback 集成测试是 0.5 天还是 2 天，以及是否要历史库多用户隔离 | 保持 loopback-only 默认；内网部署走 `proxy/` + token。注意 Fluent 端 `gui_fluent_history_detail.py:408` 已读同一 env var 并加头（v0.1.2 修过），**但客户端硬编码 `127.0.0.1` 且从 lock 文件只取端口**，非 loopback 绑定仍会失败 | `server.py` 绑定判断、`gui_fluent_history_detail.py` 的 host 读取、新增部署形态集成测试 |
| G4 | **目标用户是否真需要批量** | 决定 `extract` CLI 是 Must 还是 Should，以及要不要做断点续跑 / 并发 | 已按 Must 落地 `python main.py extract`（见 `rca_core/cli.py`）；若用户是「单篇论文 3–5 张图」，可降为便利功能而不再投入 | `rca_core/cli.py`（是否加 `--resume` / 并发） |
| G5 | **上游 RLPE 许可证能否内嵌/改写** | 法务判断。当前 `LICENSE` 写「inherits upstream terms」自然语言，`CITATION.cff` 的 license 字段同样不是 SPDX 标识，期刊/机构合规扫描器会视为「无标准许可证」 | 建议改成明确 SPDX（数据侧已是 CC0 1.0，与 `darwin_core.py` 写入的 `<distribution><license>` 一致）。**需先确认上游条款允许** | `LICENSE`、`CITATION.cff:14`、README 许可段 |
| G6 | **`gui.py` 产品定位** | 一等公民 vs 冻结为 legacy fallback，决定后续是否为它补齐 GBIF 核验 / grounding / 取消能力 | 建议**冻结功能面**（只修 bug 不加功能）+ README 标注 legacy fallback；删掉会破坏「零第三方依赖可用」这条产品承诺。当前能力已分化：Tk 端无取消、无进度中间态、无 GBIF 核验 | `docs/ARCHITECTURE.md`、`rca_core/capability_matrix.py` 的 `tk` 列、README |
| G7 | **`ExtractPage` 是否也切 JS 渲染** | 两条渲染路径并存是可编辑表格 vs 只读渲染器的取舍，不是技术债 | 建议**不切**：ExtractPage 需要 `QTableWidget` 可编辑，强统一要凭空造一个可编辑 JS 表格。把不对称记录为契约（已在 `docs/ARCHITECTURE.md` §4） | 若改，需同时动 `gui_fluent.py` ExtractPage 与 `js/table.js` |
| G8 | **`normalizer_ref` 死字段**：接线还是标注 | 它声明了 11 次但全仓无运行时解析代码（`test_d5` 只验证字符串可解析）。接线会改变模式调度，是有行为风险的架构改动 | 建议**先标注为「未接线的前瞻性声明」**，等阶段 3 的 `modes_impl/` 拆分时一并接线；已写入 `docs/DECISIONS.md` | `rca_core/modes.py`、`rca_core/extractor.py` 调度点 |

## 版本号策略补充

v0.2.0 已发布（0.1.x → 0.2.0，理由：目录重构 + 数据正确性修复，不只是 patch）。PM 建议 **ICS 标尺变更单独发版**，便于论文分别注明所用地层表版本——本清单采纳该建议，故 v0.2.0 不含 ICS 2026-06。

## 真实科研验证的边界（不能由 AI 代做）

- **真实文献 E2E 门禁**：8 个金标全部 `"synthetic": true`；`ANNOTATION_GUIDE.md` 定义了 Krippendorff's α ≥ 0.80 流程但零案例已采集；README 的 40/40 是一次性人工执行结果（`scripts/_e2e_*.py`），不是回归防护。要变成 CI 门禁需要真实版权语料 + 人工标注，这一步必须由研究者在合规前提下完成。本轮已把「合成图不能代表真实准确率」写入 `tests/fixtures/gold/README.md` 并保留诚实声明，**没有伪造任何真实案例**。
- **真实 provider 连通性**：`transport.py` / `pipeline.py` 的重构需要一次真实 API 调用验证，本轮未做（涉及付费调用与用户密钥）。
- **PySide6 真机 UI**：本机未安装 PySide6，所有 Fluent 端改动只能静态验证 + 内核通路实测；CI 的 `qt` job（offscreen + 真 PySide6）是权威验证渠道。
