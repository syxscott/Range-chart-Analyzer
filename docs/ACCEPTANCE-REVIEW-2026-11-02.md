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
3. **本评审新发现两处文档与实现不一致**（详见 §2、§3），都是"文档承诺了 CI 会做的事，但 CI 没做" —— 与 CHANGELOG 自述的 "their green PRs were not evidence" 同一类问题。

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

## 2. 137 项跳过测试的分类

`pytest` 4,360 项 / **137 项跳过**。跳过不是问题，**跳过的理由是否与"用户会遇到的失败"对齐**才是问题。

| 跳过原因 | 出现次数 | 用户会遇到吗 | 判定 |
|---|---|---|---|
| `PySide6/qfluentwidgets unavailable` | 5 处 `@skipif`（`test_fluent_ui_2026_09_05.py`、`test_gui_2026_09_27.py`、`test_gui_progress_thinking_2026_10_19.py`、`test_gui_provider_wizard_effort_2026_10_19.py`、`test_provider_gui_cc_switch_2026_10_20.py`）+ `test_qt_payload.py` | **会** —— 但**CI 有独立的 `qt` job 装 PySide6 跑它们** | ✅ **合理**。这是刻意设计：matrix job 不装 Qt，`qt` job 装。已闭环 |
| `Pillow is an optional dependency` | 3 处（`test_deskew_geometry_fixes_2026_09_22.py`、`test_geometry_deskew_2026_09_20.py` ×2） | **会** —— 但 **CI 装了 Pillow**（`pip install cryptography keyring openpyxl Pillow`） | ✅ **合理**。本地跳过、CI 跑 |
| `cryptography not installed` | 4 处 `pytest.skip`（`test_encryption_status_contract.py` ×2、`test_providers_encryption.py` ×3） | **会** —— 但 **CI 装了 cryptography** | ✅ **合理** |
| `node not available` | 2 处（`test_aggregate_nonfinite_confidence.py`、`test_aggregate_scalar_type_2026_10_17.py`、`test_quality_engine_parity.py`） | 开发者本地 | ✅ **合理**，CI 有 Node |
| `needs a real build's xref (CI does not build onefile)` | 2 处（`test_payload_policy.py`） | **不会**（PyInstaller onefile 产物检查） | ⚠️ **可接受但有代价**：`RangeChartAnalyzer.spec` 与 UPX 调优（258.8→234.7MB）**无 CI 验证**，打包只在本地人工做 |
| `the ICS table is unavailable` | `test_biozone_order_subscripts.py` | **会** —— 若 ICS 资源缺失 | ⚠️ **应视为 CI 失败而非跳过**。ICS 表是核心数据，`rca_core/resources/ics_2024.json` 缺失/损坏应让 CI 变红。**当前行为是跳过 → 绿灯** |
| `POSIX mode bits are advisory on Windows` | `test_config_dir_permissions.py` | 否 | ✅ **合理** |
| `the provenance column list is inline, not a constant` | `test_history_schema_migration.py` | 否 | ✅ **合理**（承认实现细节并跳过） |
| `single-column table` | `test_apply_row_isolation.py` | 否 | ✅ **合理** |
| `PySide6 not installed (optional dependency)` | `tests/standalone/tests_drag.py` | **会** | ⚠️ 脚本式套件，CI 的 `qt` job 有跑（`ci.yml:282-285` 跑 `tests_gui_fluent.py` `tests_drag.py` `tests_history.py` `tests_modern_ui.py`） | ✅ **合理** |

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

### 4.1 ❌ `tests/fixtures/gold/README.md` 声称的 gold 门禁调度不存在

**README 原文**（"CI Usage" 小节）：

> - `pytest -m gold_smoke` — 5 quick cases, every PR (these are fixture-only — they do not call the LLM).
> - `pytest -m gold` — all 8 cases, **weekly cron + release tag** (also fixture-only).
> - `RCA_OFFLINE_GOLD=1` — use cached responses, zero API cost.

**实际**：

```bash
grep -n "schedule\|cron\|tags:" .github/workflows/ci.yml   # 零命中
```

`ci.yml` 的触发器只有 `push: branches[main]` 和 `pull_request:` —— **没有 schedule，没有 tag 触发**。

更严重的是 **`pytest -m gold` 这个 marker 实际上永远选不出东西**：

```python
# tests/conftest.py:26-33
if "gold_smoke" in str(item.fspath):
    item.add_marker(pytest.mark.gold_smoke)
elif "gold_metrics" in str(item.fspath):
    item.add_marker(pytest.mark.gold)
elif "gold" in str(item.fspath):
    item.add_marker(pytest.mark.gold)
```

marker 是**按文件路径字符串匹配**的。而 `tests/test_gold_smoke.py` 的路径里含 "gold_smoke" → 走第一个分支，**永远进不了 `elif "gold" in str(item.fspath)`**。所以：
- `pytest -m gold_smoke` → 选中 `test_gold_smoke.py` 的 2 个参数化测试 ✅
- `pytest -m gold` → **选不出任何用例**（`test_gold_metrics.py` 若存在才会命中 `gold_metrics` 分支）

我核实过 `tests/fixtures/gold/README.md` 还写着「The "real" VLM-vs-fixture integration test that the docs reference (`test_gold_smoke.py`) is `@pytest.mark.skip(reason="Requires live LLM API")`. Until un-skipped, it does **not** run in CI.」—— 但 `grep -rn "Requires live LLM API" tests/` **零命中**，`test_gold_smoke.py` 现在是**未跳过的、真正在跑的**离线门禁。**这段 README 已经过时了**（它描述的是一个已经被重写掉的旧状态）。

**影响**：一个科研用户（或维护者）照 README 跑 `pytest -m gold` 期待"8 个 case 全套"，**实际会得到 0 个用例**。这正是"可信度缺口"里的 C6（交付物说明与实现不符）的一个新实例。**修复成本 < 0.5 人日**。

**建议**：
1. 改 `tests/conftest.py` 的 marker 判定 —— 用显式 decorator 而不是路径字符串匹配，或把 `elif "gold" in ...` 改成 `elif "gold_metrics" in ... or "gold_full" in ...`。
2. 在 `ci.yml` 真正加 `schedule: cron`（`repo-health.yml` 已有 cron 写法可抄）。
3. 更新 `tests/fixtures/gold/README.md` 的 "CI Usage" 小节 —— 删掉不存在的 live-LLM 段落，改成"该测试已重写为离线门禁并在跑"。
4. `pytest.ini` 里 `gold: full gold-standard tests (run weekly / release)` 的描述也是**同一条不存在的调度**的复述，需一并改。

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
| **B-6 `gold` marker 选不出用例** | 🟡 **P2** | `conftest.py` 按路径字符串匹配，`test_gold_smoke.py` 永远命中第一个分支 | README 承诺的 `pytest -m gold` 实际选 0 个用例（见 §4.1） |
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
| A-9 | `pytest -m gold` 能选出用例 | `pytest -m gold --collect-only -q` 输出数 > 0 | ❌ |
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
| **P2 重要** | 能力退化或承诺落空，但不影响出数 | B-4（三端不对称）、B-5（打包无验证）、B-6（gold marker 失效）、B-7（ICS 缺失 skip 而非 fail） |
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

2. **给 gold 门禁一个真实的调度**（0.5 人日 + CI）
   修 `conftest.py` 的 marker 判定 → 在 `ci.yml` 加 `schedule: cron`（抄 `repo-health.yml`）→ 更新 `tests/fixtures/gold/README.md` 的 "CI Usage" 小节 + `pytest.ini` 的 marker 描述。

3. **把 ICS 缺失从 skip 改成 fail**（0.25 人日）
   核心数据缺失不应该让 CI 变绿。README 自述的历史教训正是这一类。

4. **加一个 PyInstaller 构建 job**（1–2 人日）
   一次构建 + `test_payload_policy.py` 解冻 + 一个"exe 能启动"的冒烟。UPX 调优结果从此有回归防护。

5. **保留现有测试资产，不要重写**（明确反对）
   4,360 pytest + 2,576 Node 断言 + 1,363 差分 parity 的规模与质量在同类项目中突出，且**真的抓到过严重缺陷**。任何"重构测试以提升可维护性"的提议，在当前优先级下都是净负收益。

---

## 9. 与其他评审的接口

- **对 devops-engineer（任务 #3，发布与运维就绪度）**：本评审的 §4.1、§4.2、§5 的 B-6 与 §8-2 直接是"发布流程与门禁调度"问题；`pytest -m gold` 选不出用例这条建议你们直接接手。
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

# §4.1 gold 调度不存在
grep -n "schedule\|cron\|tags:" .github/workflows/ci.yml    # 零命中
grep -rn "Requires live LLM API" tests/                       # 零命中（README 过时）

# §4.2 阈值不一致
grep -n "SPECIES_GATES" tests/test_gold_smoke.py              # recall 0.55 / f1 0.6
sed -n '15,16p' tests/test_gold_smoke.py                       # docstring: recall 0.75 / f1 0.7

# §2 跳过分类
grep -rn "pytest.mark.skipif\|pytest.skip(" --include=*.py tests/ -A2 | grep -i reason

# §3 CI 覆盖矩阵
grep -n "pip install" .github/workflows/*.yml
grep -rn "_e2e" .github/workflows/*.yml                        # 零命中
grep -rn "PyInstaller\|onefile" .github/workflows/*.yml        # 零命中

# B-7 ICS 缺失被 skip
grep -n "the ICS table is unavailable" tests/test_biozone_order_subscripts.py
```
