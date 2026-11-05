# Range-chart-Analyzer 成熟度验收评审

评审人：许可研（场景产品策略师）　日期：2026-11-02　基线：`b0acebf`（v0.1.4 + 22 commits）
任务：#4 Range-chart-Analyzer 成熟度验收评审（测试验证）
配套文档：`docs/GAP-ANALYSIS-2026-11-02.md`（缺口裁定）

---

## 0. 结论摘要

**这个仓库的测试纪律是同类项目中少见的扎实**，但它守的是"代码没坏"，不是"数据是对的"。

三类核心结论：

1. **测试套件本身质量高**——4,360 pytest / 0 失败、2,576 Node 断言、1,363 差分 parity 全绿，且历史上真的抓到过严重缺陷（三端 ICS 分叉偏老 20 倍、PBDB 把 section 年龄当每个分类单元的 FAD/LAD、pytest 收集静默失效）。**这份测试资产应被保护，而不是被重写。**
2. **但"绿灯"存在三处系统性盲区**：① 准确率门禁只跑合成图，真实文献 E2E 不在 CI；② 可选依赖零覆盖，CI 绿灯不代表用户环境能跑；③ **发布版与 HEAD 差 22 个提交，数据正确性修复从未进入任何 release**。
3. **本评审新发现两处文档与实现不一致**（详见 §4）：gold README 的 "CI Usage" 小节描述的 marker 语义与调度均与实际不符（本评审初稿曾误判为"`pytest -m gold` 选不出用例"，经本机实测**已修正**——实际选中 29 项，但选中的是另一个文件）；`test_gold_smoke.py` docstring 的阈值与 `SPECIES_GATES` 常量漂移。两类同属"文档承诺与实现不符"。
4. **本机全量复跑**（pytest 8.3.4 / Python 3.13 / Windows）确认 137 项跳过的**分类健康**：Qt 门控约 72 项（CI qt job 覆盖）、床标签反例参数化 39 项（按设计跳过）、parity fall-through 7 项（文档化行为），无一属于"该跑没跑"。

**总体成熟度判定：接近"可用的研究原型"，距"可放心用于真实数据并发表"缺 3 件事**（详见 §6 验收清单）。

---

## 1. 成熟度检查项表（6 条，可被 CI/测试验证）

| # | 检查项 | 判定方式 | 当前状态 | 依据 | 差距 |
|---|---|---|---|---|---|
| **M1** | **发布版 = 仓库宣称的能力** | CI job：最新 tag 与 HEAD 的 diff 若触及 `rca_core/standards/`、`rca_core/exporter.py`、`rca_core/quality.py` 且 CHANGELOG 仍有 `Fixed` 条目 → 失败 | ❌ **不通过** | 最新 tag `v0.1.4`(2026-10-21)，HEAD 领先 **22 commits / 105 files / +7119 -694**；`git show v0.1.4:rca_core/standards/pbdb.py` 无 `range_base_label`、`grep not_drawn` **零命中**；无 `.github/workflows/release.yml` | **整个发布流程缺失**。一次 release 即可翻绿 |
| **M2** | **所有声明的可选依赖在 CI 里真实安装并冒烟** | CI job 装 `sv_ttk`/`pywebview`/`matplotlib` 各跑一个断言 | ❌ **不通过** | `grep "pip install" .github/workflows/*.yml` 只有 `pytest ruff`/`cryptography keyring openpyxl Pillow`/`numpy`/`PySide6*`；`grep -rln "sv_ttk" tests/` **零命中** | **3 个依赖零覆盖**。CHANGELOG 已承认 "their green PRs were not evidence" 但未落地为配置 |
| **M3** | **CI 绿灯覆盖真实安装路径** | CI 执行 `pip install -r requirements.txt` 并断言 import 成功 | ❌ **不通过** | `requirements.txt` 十个 marker 全为 `python_version >= "3.10"`，本身可在 CI 装；CI 却不读它 | **CI 与用户安装路径分叉**，加一个 job 即可 |
| **M4** | **三端行为一致** | 差分 parity（1,363 matched / 0 diverged）+ `test_ics_transport_table_parity.py` | ⚠️ **有守卫但有盲区** | 全绿；但学名 fuzzy **仅 Web 端接线**（桌面端 `grep -rn "fuzzy" gui_fluent.py gui.py` 零命中）；ICS 曾 98 vs 109 行分叉、偏老 20 倍 | **"新能力只在三端之一落地"无自动检测**。需"能力注册表 × 三端"断言 |
| **M5** | **导出物自带溯源** | 测试断言导出含 `image_sha256`；DwC 断言含 occurrenceID/basisOfRecord/locality | ⚠️ **部分通过** | `exporter.py:2574` 把 `image_sha256` 写进导出 provenance；`darwin_core.py:591-619` DwC term 映射完整 | **图级溯源 ✅ / 单元格级 ❌**（overlay 补齐） |
| **M6** | **开源协作基础设施齐备** | 文件存在性检查（`repo_health.py` 已做同类事） | ❌ **不通过** | `ls *.md` 仅 `CHANGELOG.md` `README.md`；无 `ISSUE_TEMPLATE` / `release.yml` / `CONTRIBUTING.md` / `SECURITY.md` | **4 项全缺**，M1 不通过的根因 |

---

## 2. 137 项跳过测试的分类（含本机全量复跑实测）

`pytest` 4,360 项 / **137 项跳过**。跳过不是问题，**跳过的理由是否与"用户会遇到的失败"对齐**才是问题。下表前 4 行按**本机全量复跑**（pytest 8.3.4 / Python 3.13 / Windows，`pytest tests/ -q -rs`）的 `-rs` 输出统计，其余按源码 grep。

| 跳过原因 | 本机实测数 | 用户会遇到吗 | 判定 |
|---|---|---|---|
| **Qt/PySide6 门控**（`test_provider_gui_cc_switch_2026_10_20.py` 16、`test_gui_progress_thinking_2026_10_19.py` 15、`test_gui_2026_09_27.py` 14、`test_gui_provider_wizard_effort_2026_10_19.py` 9、`test_qt_payload.py` 6、`test_fluent_ui_2026_09_05.py` 5、`test_gui_sprint_b.py` 4、`test_gui_fluent_low_fixes.py` 3、`test_gui_fluent_ssrf.py` 6 等，合计约 **72**） | ~72 | **会** —— 但**CI 有独立的 `qt` job 装 PySide6 跑它们** | ✅ **合理**。刻意设计：matrix job 不装 Qt，`qt` job 装 |
| **床标签反例参数化**（`test_single_bed_parser_invariant.py:268`：`"'abc' is not a bed label; bed_position declines it"` 等，**39** 项） | 39 | 否 —— 这是**按设计跳过**：验证 `bed_position` 拒绝非床标签（`"253 Ma"`、`"9 9"`、全角 `９`、`-9` 等），被拒绝的输入跳过比较，属正常路径 | ✅ **合理**。这是**负向用例的跳过**，不是被遮蔽的能力缺失 |
| **parity fall-through 文档化行为**（`test_table_configs_parity.py:262`："falls through to the range-chart/generic branch, where the browser keeps four empty editing shells on purpose"，**7** 项） | 7 | 否 | ✅ **合理**，skip 消息本身就是行为说明 |
| 平台相关（`test_config_dir_permissions.py` 2 项 POSIX mode bits；`test_server_path_resolution.py` 2 项） | 4 | 否 | ✅ **合理** |
| 打包相关（`test_payload_policy.py` ×2 "needs a real build's xref (CI does not build onefile)"） | 2 | **不会**（PyInstaller onefile 产物检查） | ⚠️ **可接受但有代价**：`RangeChartAnalyzer.spec` 与 UPX 调优（258.8→234.7MB）**无 CI 验证**，打包只在本地人工做 |
| `test_history_schema_migration.py` 1 项（"provenance column list is inline"） | 1 | 否 | ✅ **合理** |
| **ICS 缺失**（`test_biozone_order_subscripts.py` "the ICS table is unavailable"） | （本机未触发——ICS 资源在） | **会** —— 若 ICS 资源缺失 | ⚠️ **应视为 CI 失败而非跳过**。ICS 表是核心数据，`rca_core/resources/ics_2024.json` 缺失/损坏应让 CI 变红。**当前行为是跳过 → 绿灯** |

### 结论

**跳过分类健康。** 唯一需要改的是 **`test_biozone_order_subscripts.py` 的 ICS 缺失应从 skip 改为 fail** —— 那是核心数据，不是可选依赖。
其余跳过全部对应"CI 里有 job 覆盖"或"纯开发者本地场景"。

---

## 3. CI 覆盖矩阵（4 个 job）

| 能力 | test 3.10 | test 3.12 | qt | browser perf | 盲区 |
|---|---|---|---|---|---|
| pytest 全量 `tests/` | ✅ | ✅ | ✅ | — | — |
| `tests/standalone/tests_core.py` | ✅ 脚本 + pytest 两种 | ✅ | — | — | — |
| 15 个 Node 套件（2,576 断言） | ✅ | ✅ | — | ✅ | — |
| 差分 parity（1,363 matched） | ✅ | — | — | — | **只在 3.10 跑**（`ci.yml:152` 在 test job） |
| PySide6 GUI 套件 | ❌ | ❌ | ✅ | — | — |
| `cryptography` / `keyring` / `openpyxl` / `Pillow` | ✅ | ✅ | ✅ | — | — |
| **`sv_ttk`** | ❌ | ❌ | ❌ | ❌ | **零覆盖（M2）** |
| **`pywebview`** | ❌ | ❌ | ❌ | ❌ | **零覆盖（M2）** |
| **`matplotlib`** | ❌ | ❌ | ❌ | ❌ | **零覆盖（M2）** |
| **`numpy`** | ✅（单独 `pip install numpy`） | — | — | — | 只在 3.10 |
| 真实文献 E2E（40 图） | ❌ | ❌ | ❌ | ❌ | **不在任何 CI job**（`grep -rn "_e2e" .github/workflows/*.yml` 零命中） |
| 打包（PyInstaller onefile） | ❌ | ❌ | ❌ | ❌ | **不在 CI**（`test_payload_policy.py` 因此跳过） |

---

## 4. 本评审新发现：两处"文档承诺 CI 会做，CI 没做"

这两处是本评审的独立产出，**不在原简报的遗留债清单里**。

### 4.1 ⚠️ `tests/fixtures/gold/README.md` 的 "CI Usage" 小节描述的 marker 语义与调度均与实际不符

> **评审过程更正声明**：本节初稿曾断言"`pytest -m gold` 选不出任何用例"。**经本机实测（pytest 8.3.4），该断言错误**：`pytest -m gold` 实际选中 **29 项**（来自 `test_gold_metrics.py`）。错误原因：初稿只看了 `tests/` 目录文件列表的一角，未确认 `test_gold_metrics.py` 的存在（它命中 `conftest.py` 的 `elif "gold_metrics" in ...` 分支）。以下为修正后的结论——问题仍然存在，但形态不同。

**README 原文**（"CI Usage" 小节）：

> - `pytest -m gold_smoke` — 5 quick cases, every PR (these are fixture-only — they do not call the LLM).
> - `pytest -m gold` — **all 8 cases, weekly cron + release tag** (also fixture-only).
> - `RCA_OFFLINE_GOLD=1` — use cached responses, zero API cost.

**实测结果**（本机，pytest 8.3.4）：

```bash
pytest -m gold_smoke --collect-only -q   # → tests/test_gold_smoke.py: 16
pytest -m gold       --collect-only -q   # → tests/test_gold_metrics.py: 29
```

三处不符：

1. **数字不符**：README 说 gold_smoke 是 "5 quick cases"、gold 是 "all 8 cases"。实际 gold_smoke 选中 **16** 项、gold 选中 **29** 项。且 `-m gold` 选中 `test_gold_metrics.py` —— 那是**纯 metric 单元测试**（文件头自述 "mock-based, no real LLM"），**不是** README 说的 "all 8 cases"（8 个金标 case 的流水线门禁在 `test_gold_smoke.py` 里，带 `gold_smoke` marker）。
2. **调度不符**：README 说 gold 是 "weekly cron + release tag"。`grep -n "schedule\|cron\|tags:" .github/workflows/ci.yml` → **零命中**。`ci.yml` 触发器只有 `push: branches[main]` + `pull_request:`，仓库只有 `repo-health.yml` 有 cron（周一巡检，且不跑 pytest）。**"weekly cron + release tag" 是一个不存在的调度。**
3. **语义不符的根因**：`tests/conftest.py:26-33` 按文件路径字符串匹配打 marker —— `"gold_smoke" in path → gold_smoke`，`elif "gold_metrics" in path → gold`。于是 `-m gold` 选中的是 metric 单元测试，而**真正持金标流水线门禁的 `test_gold_smoke.py` 只有 `gold_smoke` marker、没有 `gold` marker**。两个 marker 划出的是两个不相交的文件，README 把它们当成了一个递进关系（smoke ⊂ gold）。

同节还有一处**过时描述**：README 写「The "real" VLM-vs-fixture integration test that the docs reference (`test_gold_smoke.py`) is `@pytest.mark.skip(reason="Requires live LLM API")`. Until un-skipped, it does **not** run in CI.」—— 但 `grep -rn "Requires live LLM API" tests/` **零命中**，`test_gold_smoke.py` 现在是未跳过的、真正在跑的离线门禁。这段描述的是已被重写掉的旧状态。

**影响**：照 README 操作的维护者会得到与文档承诺不同的测试集合（29 项单元测试而非 8 个金标 case），且"weekly cron + release tag"的门禁**根本不存在** —— 金标 gate 实际只在每次 PR 的 gold_smoke 里跑 16 项。这正是"绿灯比声称的弱"的又一实例。**修复成本 < 0.5 人日**。

**建议**：
1. **给 `test_gold_smoke.py` 同时打上 `gold` marker**（或在 conftest 的第一个分支里 `item.add_marker(pytest.mark.gold_smoke); item.add_marker(pytest.mark.gold)`），让 README 的 "smoke ⊂ gold" 语义成立；
2. 在 `ci.yml` 真正加 `schedule: cron`（`repo-health.yml` 已有 cron 写法可抄），或把 README 改成实际的调度事实；
3. 更新 `tests/fixtures/gold/README.md` 的 "CI Usage" 小节 —— 修正 5/8/16/29 的数字，删掉不存在的 live-LLM 段落；
4. `pytest.ini` 里 `gold: full gold-standard tests (run weekly / release)` 的描述同样与实际调度不符，需一并改。

### 4.2 ⚠️ gold 准确率门禁的文档阈值与实际阈值不一致

**`tests/test_gold_smoke.py` 的 docstring**：

> ```
> Gates:
>   * range_chart:      species precision >= 0.7, recall >= 0.75, f1 >= 0.7
>   * abundance:        abundance_sum_error <= 0.05 (verbatim canned)
> ```

**实际代码**（`tests/test_gold_smoke.py:49`）：

```python
SPECIES_GATES = {"min_precision": 0.7, "min_recall": 0.55, "min_f1": 0.6}
```

| 指标 | docstring 声称 | 实际阈值 | 差 |
|---|---|---|---|
| precision | ≥ 0.7 | ≥ 0.7 | 一致 ✅ |
| **recall** | **≥ 0.75** | **≥ 0.55** | **低 0.20** ⚠️ |
| **f1** | **≥ 0.7** | **≥ 0.6** | **低 0.10** ⚠️ |

**实际阈值是有意的**，同文件 44-48 行解释了原因：

> "rc_synth_* have 5 true species; the canned degradation drops 1 row and typos 1 of the remaining 4, so the expected strict scores are precision = 3/4 = 0.75, **recall = 3/5 = 0.6**, **f1 = 0.667**. Gates sit below those with margin so pipeline regressions (not the seeded degradation) are what fails the test."

**即：docstring 是从旧版本复制过来没同步，代码是对的。** 这是纯粹的文档漂移。

**影响**：**低**（不影响测试行为）。但一个审计者读 docstring 会以为门禁比实际严 0.20，这是"绿灯比声称的弱"的另一个实例。**修复成本 < 0.25 人日**，改 docstring 即可。

### 4.3 ✅ 一个应当被表扬的诚实

`tests/fixtures/gold/README.md` 开头写着：

> "**M-1 fix (REVIEW-2026-07-25): The original README claimed these were 'expert-annotated real charts' but every entry in `_index.json` has `"synthetic": true`. This rewrite accurately reflects that the 8 cases are SYNTHETIC images generated by matplotlib, used to unit-test the metric math in `rca_core.eval_metrics` and `rca_core.quality` — NOT to measure real-world extraction accuracy.**"

并且明确写出：

> "A VLM that scores 0.95 here may score 0.55 on real published charts."
> "**What is missing**: We need **real, expert-annotated** range charts from published literature... **no real cases have been collected yet**."

**这个仓库对自己的局限性的诚实度高于绝大多数开源科研工具。** 我在缺口盘点里没有把"缺少真实金标"列为 Must，**正是因为它已经被明确识别并写在了正确的位置**（README 路线图第 5 项「E2E 40 图升级为带人工核对基准的公开回归门禁」也已列）。

---

## 5. 测试盲区（按"会不会让用户拿到错数据"排序）

| 盲区 | 严重性 | 现状 | 用户影响 |
|---|---|---|---|
| **B-1 发布版 ≠ HEAD** | 🔴 **P0** | 22 commits / +7119 行 / 105 文件未发布；含 2 个数据正确性修复 | **已发布版本会让已发表数据出错**。PBDB 把 section 年龄当每个分类单元的 FAD/LAD（41 个 occurrence 塌成同一区间）；`not_drawn` 被当实存分布 |
| **B-2 无真实文献准确率门禁** | 🟠 **P1** | 8 个金标**全部合成**（`_index.json` 8 个 `"synthetic": true`）；真实文献 E2E 不在 CI；`ANNOTATION_GUIDE.md` 定义了 Krippendorff's α ≥ 0.80 流程但**零案例已采集** | 用户无法知道"真实图上的准确率是多少"。README 的 40/40 端到端验证是**一次性人工执行结果**（`scripts/_e2e_*.py`，`grep -rn "_e2e" .github/workflows/` 零命中），不是回归防护 |
| **B-3 可选依赖零覆盖** | 🟠 **P1** | `sv_ttk`/`pywebview`/`matplotlib` 在 CI 零覆盖 | **CI 绿灯不代表用户环境能跑**。用户装完发现 Usage 页白屏 / 窗口打不开，回不到 CI 查 |
| **B-4 三端能力落地不对称无检测** | 🟡 **P2** | fuzzy 仅 Web 端；ICS 曾 98 vs 109 行分叉 | 同一张图在两端产出**不同科学结论**（ICS 那次偏老 20 倍）。差分 parity 守的是"同端一致"，守不住"新能力三端都有" |
| **B-5 打包产物无 CI 验证** | 🟡 **P2** | `test_payload_policy.py` 因 "CI does not build onefile" 跳过；无 PyInstaller job | `RangeChartAnalyzer.spec` 19.8KB + UPX 调优结果（258.8→234.7MB）**只由本地人工保证**。用户拿到打不开的 exe 时无拦截 |
| **B-6 gold marker 语义/调度与文档不符** | 🟡 **P2** | `pytest -m gold` 实选 29 项纯 metric 单元测试，而非 README 承诺的 8 个金标 case；"weekly cron + release tag" 调度不存在（见 §4.1） | 维护者照 README 跑 gold 期待全套金标门禁，实际得到的是另一组测试；8 个金标 case 实际只随 gold_smoke（每 PR 16 项）跑 |
| **B-7 ICS 缺失被 skip 而非 fail** | 🟡 **P2** | `test_biozone_order_subscripts.py` "the ICS table is unavailable" → skip | 核心年代数据损坏/缺失时 CI **仍绿**。README 自述历史教训「"819 个测试全绿但年代数据损坏"」正是这类 |
| **B-8 无 a11y 自动化断言** | ⚪ **P3** | `grep -rni "aria-\|a11y\|accessib"` 在 `tests/` 零命中 | 评审是一次性的；已点名的"视觉中文/读屏英文"双通道不一致未修（`docs/UI-REVIEW-WEB-2026-10-17.md:110`） |

---

## 6. 验收清单（Definition of Done）

### 阶段一：止血（约 7 人日，可在一周内完成）

| # | 验收项 | 判定方式（可执行） | 状态 |
|---|---|---|---|
| A-1 | **发布 v0.1.5**，含已修好的 PBDB `not_drawn`/section-age 修复 | `git tag` 出现 `v0.1.5`；`git show v0.1.5:rca_core/standards/pbdb.py \| grep -c not_drawn` > 0；`grep -c range_base_label` > 0 | ❌ |
| A-2 | `CHANGELOG.md` 的 `[Unreleased]` 已定版，无遗留 `Fixed` 条目 | `awk '/^## \[Unreleased\]/,/^## \[0\.1\.[0-9]+\]/' CHANGELOG.md` 只剩空节 | ❌ |
| A-3 | `CITATION.cff` 的 `version` / `date-released` 与新 tag 一致 | `CITATION.cff` 的 version == `git tag` 最新 tag；date-released == tag 实际日期（**当前是 2026-10-22 vs 实际 2026-10-21**） | ❌ |
| A-4 | 存在 release workflow 或半自动发布脚本 | `.github/workflows/release.yml` 存在，或 `scripts/release.py` 存在 | ❌ |
| A-5 | CI 安装 `requirements.txt` 全部依赖 | `grep -q "requirements.txt" .github/workflows/ci.yml` → 真 | ❌ |
| A-6 | 三个可选依赖各有至少一个冒烟断言 | `grep -q sv_ttk .github/workflows/ci.yml` && `grep -q pywebview ...` && `grep -q matplotlib ...` | ❌ |
| A-7 | `CONTRIBUTING.md` / `SECURITY.md` / `ISSUE_TEMPLATE/` 存在 | 文件存在性检查（`repo_health.py` 已有同类 `finding()` 机制可复用） | ❌ |
| A-8 | 桌面端接入 `names.fuzzy`（三端一致） | `grep -c "fuzzy" gui_fluent.py gui.py` > 0 | ❌ |
| A-9 | gold marker 语义与 README 一致 | `pytest -m gold --collect-only -q` 选中的集合包含 `test_gold_smoke.py` 的 8 个金标 case（当前选中的是 29 项 metric 单元测试，不含金标 case） | ❌ |
| A-10 | `test_gold_smoke.py` docstring 阈值与 `SPECIES_GATES` 一致 | docstring 写 recall 0.55 / f1 0.6 | ❌ |

### 阶段二：提效（约 10–15 人日）

| # | 验收项 | 判定方式 | 状态 |
|---|---|---|---|
| B-1 | 延限图 overlay：表格行 ↔ 原图标注双向联动 | 端到端测试：给定一张金标图，断言导出的标注坐标落在原图预期区域内 | ❌ |
| B-2 | `python main.py extract --image F.png --out D/` 可无 GUI 完成提取 | CLI 冒烟测试进 CI | ❌ |
| B-3 | ICS 标尺升级到 v2026-06，三处界线生效 | `rca_core/standards/ics.py` 的 `ICS_VERSION` 变更；Olenekian 底 250.8 / Anisian 底 247.0 / Wuchiapingian 底 259.857 三个断言 | ❌（待 Roadian 裁定） |
| B-4 | `confidence` 在**所有**导出路径标注"模型自报，非校准概率" | 每个导出格式各一个断言 | ❌ |
| B-5 | 部署态 `RCA_PROVENANCE_TOKEN` 溯源导出可成功 | 一个真实部署形态的集成测试（非 loopback） | ❌ |
| B-6 | "能力注册表 × 三端"一致性断言 | 每个注册能力断言在三端都有对应实现 | ❌ |

### 阶段三：长期（Should / Could，见缺口裁定优先级表）

- 真实文献金标（`ANNOTATION_GUIDE.md` 流程，Krippendorff's α ≥ 0.80）→ 变成 CI 门禁
- PyInstaller onefile 构建进 CI（解 B-5）
- ICS 缺失改为 fail（解 B-7）
- a11y 自动化断言 + 双通道语言一致性修复
- 导出数据字典随文件分发

---

## 7. 缺陷分级

| 级别 | 定义 | 本次识别项 |
|---|---|---|
| **P0 阻断** | 已交付物产生**科学性错误** | B-1（v0.1.4 含 PBDB section-age / not_drawn 缺陷） |
| **P1 严重** | 用户装完跑不起来，或拿不到可信的准确率 | B-2（无真实文献准确率门禁）、B-3（可选依赖零覆盖） |
| **P2 重要** | 能力退化或承诺落空，但不影响出数 | B-4（三端不对称）、B-5（打包无验证）、B-6（gold marker 语义/调度与文档不符）、B-7（ICS 缺失 skip 而非 fail） |
| **P3 改进** | 体验与一致性 | B-8（a11y）、docstring 阈值漂移、CITATION 双份维护 |

**说明**：本仓库**没有** P0 级别的"代码缺陷导致线上事故"（无生产环境），但 B-1 是**事实上的 P0**——它已经在 GitHub Release 上，且会让下游用户的论文数据出错。

---

## 8. 改进建议（按性价比排序，不重复优先级表）

1. **把成熟度检查项固化成 `repo_health.py` 的新检查**（0.5–1 人日）
   `scripts/repo_health.py` 已有 `finding(check, detail, fix)` + "ONE issue, updated in place" 的成熟机制，且设计说明明确写着它抓的是"测试套件结构上抓不到"的腐坏。**M1/M3/M6 恰好属于这一类**：
   - M1：最新 tag 与 HEAD 的 diff 若触及数据关键路径且 CHANGELOG 仍有 `Fixed` → finding
   - M3：`.github/workflows/*.yml` 中出现 `pip install` 但无 `requirements.txt` 引用 → finding
   - M6：`CONTRIBUTING.md` / `SECURITY.md` / `ISSUE_TEMPLATE/` 缺失 → finding

   **为什么这条价值最高**：它把"人工记得检查"变成"每周一自动提醒"，且**复用已有机制，不新增 CI 基础设施**。这是本评审最推荐的单点改动。

2. **给 gold marker 正确语义 + 真实调度**（0.5 人日 + CI）
   给 `test_gold_smoke.py` 补 `gold` marker（让 `-m gold` 覆盖金标 case）→ 在 `ci.yml` 加 `schedule: cron`（抄 `repo-health.yml`）或如实修改文档 → 更新 `tests/fixtures/gold/README.md` 的 "CI Usage" 数字（5/8 → 16/29 实测值）+ `pytest.ini` 的 marker 描述。

3. **把 ICS 缺失从 skip 改成 fail**（0.25 人日）
   核心数据缺失不应该让 CI 变绿。README 自述的历史教训正是这一类。

4. **加一个 PyInstaller 构建 job**（1–2 人日）
   一次构建 + `test_payload_policy.py` 解冻 + 一个"exe 能启动"的冒烟。UPX 调优结果从此有回归防护。

5. **保留现有测试资产，不要重写**（明确反对）
   4,360 pytest + 2,576 Node 断言 + 1,363 差分 parity 的规模与质量在同类项目中突出，且**真的抓到过严重缺陷**。任何"重构测试以提升可维护性"的提议，在当前优先级下都是净负收益。

---

## 9. 与其他评审的接口

- **对 devops-engineer（任务 #3，发布与运维就绪度）**：本评审的 §4.1、§4.2、§5 的 B-6 与 §8-2 直接是"发布流程与门禁调度"问题；gold marker 语义修正（A-9）这条建议你们直接接手。
- **对 system-architect（任务 #1，架构收口）**：§6 阶段二 B-6（"能力注册表 × 三端"一致性断言）是架构层的通用机制需求；§4.1 的 marker 判定设计（路径字符串匹配 vs 显式 decorator）也是同一类"注册表应显式声明"的问题。
- **对 team-lead**：缺口裁定的结论已单独回传（MVP 三件事 / 成熟度检查项 / 优先级表 / 不建议做清单）。本评审**不推翻**那些结论，只补两条：(a) 新发现两处文档与实现不一致（§4.1、§4.2）；(b) 跳过的 137 项**分类健康**，不是问题源，不必投入。

---

## 附：可复现的证据命令

```bash
# B-1 发布版落后
git log --oneline v0.1.4..HEAD | wc -l                       # 22
git diff --stat v0.1.4..HEAD | tail -1                      # 105 files, +7119, -694
git show v0.1.4:rca_core/standards/pbdb.py | grep -c not_drawn    # 0
git show v0.1.4:rca_core/standards/pbdb.py | grep -c range_base_label  # 0

# §4.1 gold marker 实测（本机 pytest 8.3.4）
pytest -m gold_smoke --collect-only -q   # → tests/test_gold_smoke.py: 16（README 称 5）
pytest -m gold       --collect-only -q   # → tests/test_gold_metrics.py: 29（README 称 8 个金标 case）
grep -n "schedule\|cron\|tags:" .github/workflows/ci.yml    # 零命中（"weekly cron" 不存在）
grep -rn "Requires live LLM API" tests/                       # 零命中（README 过时）

# §4.2 阈值不一致
grep -n "SPECIES_GATES" tests/test_gold_smoke.py              # recall 0.55 / f1 0.6
sed -n '15,16p' tests/test_gold_smoke.py                       # docstring: recall 0.75 / f1 0.7

# §2 跳过分类（本机复跑）
pytest tests/ -q --tb=no -rs | grep -o "SKIPPED \[[0-9]*\] [^:]*" | sed 's/SKIPPED \[[0-9]*\] //' | sort | uniq -c | sort -rn
# → Qt 门控 ~72；test_single_bed_parser_invariant.py 39（负向参数化，按设计）；parity fall-through 7；...

# §3 CI 覆盖矩阵
grep -n "pip install" .github/workflows/*.yml
grep -rn "_e2e" .github/workflows/*.yml                        # 零命中
grep -rn "PyInstaller\|onefile" .github/workflows/*.yml        # 零命中

# B-7 ICS 缺失被 skip
grep -n "the ICS table is unavailable" tests/test_biozone_order_subscripts.py
```
