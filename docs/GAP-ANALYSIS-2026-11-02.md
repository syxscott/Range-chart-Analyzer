# Range-chart-Analyzer 缺口裁定（场景产品策略师）

日期：2026-11-02　范围：syxscott/Range-chart-Analyzer @ b0acebf（v0.1.4 之后 22 个提交）
所有结论均来自仓库只读探查，证据给出文件:行号。

---

## 1. 缺口盘点

### (a) 可信度缺口 —— 会让用户怀疑结果对不对

#### A1. **已发布版本 v0.1.4 携带两个数据正确性缺陷**（最高优先级）

- **问题**：v0.1.4（2026-10-21 发布）的 PBDB 导出把**整个 section 的年龄区间当作每个分类单元的 FAD/LAD**；同时 `not_drawn`（模型看过但没找到）与 `uncertain`（名字读不清）被**当成实存分布**发布。
- **谁受影响**：所有用 v0.1.4 导出 PBDB 并投稿的科研用户。错误是**科学性错误**，不是排版错误。
- **现状证据**：
  - `git show v0.1.4:rca_core/standards/pbdb.py` —— 无 `range_base_label` / `range_top_label` 两个机器可读列，即修复尚未进入发布版；`_resolve_pbdb_bounds` 在 789 行仍存在旧路径，1070 行仍无条件套用 section 区间。
  - `git show v0.1.4:rca_core/standards/pbdb.py | grep not_drawn` —— **零命中**，即该版本根本没有 `not_drawn` 分流逻辑。
  - 修复在 HEAD：CHANGELOG `[Unreleased] / Fixed` 明确记载「all 41 occurrences came out as one identical interval (Wuchiapingian–Induan, 259.51–249.9 Ma)」，以及「A `not_drawn` row was published as a presence」。
  - `git log --oneline v0.1.4..HEAD` = **22 commits**，`git diff --stat v0.1.4..HEAD` = **105 files changed, +7119 / -694**。
- **影响**：这是**唯一一个会让已发表数据出错**的缺口。README 第 214 行的端到端验证表（40/40 成功、41 分类单元）跑在 HEAD 上，但用户从 GitHub Release 下载的是 v0.1.4。**发布版与 README 宣称的能力不一致**——这是科研工具最伤信任的一种不一致。
- **判定**：不是"缺功能"，是"发出去的版本有 bug"。

#### A2. **无法把一个表格单元格追溯回原图的具体位置**

- **问题**：用户拿到一列延限数据，知道"这个属的 FAD 在第 3 层"，但看不到"模型是在原图的哪个像素/哪一段读出来的"。复核只能靠肉眼重新找。
- **谁受影响**：所有需要人工校对表格行的用户 —— 也就是**全部**用户。这是本工具最核心的使用动作（README 强调"在表格里人工校对"）。
- **现状证据**：
  - `rca_core/redraw.py` 只做**重绘**（把抽出的数据画成一张新图，`redraw_range_chart`），不画回**原图**。
  - `rca_core/geometry.py:678` 有 `pixel_to_data` 的逆变换，但用于重绘定位，不是"从数据行反查原图坐标"。
  - `js/viz.js` / `js/app.js` 有 overlay，但那是**数据画布的图例/交互层**，不是原图叠加。
  - README 路线图第 3 项「延限图 **overlay 标注**（把提取端点映射回原图坐标供复核）」—— **官方自认未做**。
  - 证据链报告 `report`（schema v2）记录的是 `image_sha256`（整图哈希）、`prompt_sha256`、`coverage.decisions`、`reason_codes` —— 都是**图级/字段级**，**没有单元格级坐标**。
- **影响**：与用户画像直接冲突。用户的核心诉求是"每行要知道依据"，当前提供的依据粒度是"这一整张图 + 这次提示词"，**不足以定位单行错误**。修 A2 的收益高于加任何新功能。

#### A3. **发布版与 HEAD 的 ICS 标尺不一致**

- **问题**：README 说年代标尺锁定在 ICS v2024/12，但 v2026-06 官方更新的三处界线（Olenekian 底 250.8、Anisian 底 247.0、Wuchiapingian 底 259.857）未采纳。
- **谁受影响**：做三叠纪/二叠纪工作的用户（正是本项目 E2E 验证所用的放射虫文献的主要时代）。
- **现状证据**：`rca_core/standards/ics.py:47` → `ICS_VERSION = "ICS v2024/12"`；README「年代标尺版本锁定」小节明写「已知的 v2026-06 官方更新…尚待采纳」；路线图第 6 项同款未勾选。
  - **但**：`scripts/update_ics.py` 已经写好了完整的刷新管线（Macrostrat defs API + ICS chart RDF 双通道、纯 stdlib 解析、flat schema 校验、`--write-canonical` 显式晋级、`--offline-fixture` 拒绝晋升）。**工具已备好，只差跑一次 + 人工裁定**。
  - Roadian 底界在二叠纪分会有争议（Permophiles #78）—— 需要的是**决策**，不是开发。
- **影响**：年代值直接进导出表，是论文里的数字。停在 2024/12 不是 bug，但它让"数据可信"这件事停在了半路。

#### A4. **置信度的语义没在交付物里说清**

- **问题**：每行的 `confidence` 列是**模型自报**，不是校准后的概率。仓库自己知道这一点，但这个警告只在**某些导出路径**里出现。
- **谁受影响**：把 confidence 当作"可发表的筛选阈值"的统计用户。
- **现状证据**：`rca_core/exporter.py:2592` 的注释明写「AUDIT-2026-10-22: the per-row `confidence` column is the MODEL'S OWN ...」，`2598-2599` 出现 `confidence_note` 字段；`rca_core/aggregate.py:530` `_merge_confidence` 的文档说明「Average valid per-row confidence values without treating missing as zero」，并处理了 NaN 不被当成 1.0 的历史 bug。
- **影响**：这是"看似已具备、实则半具备"的陷阱。**风险不是缺功能，是用户误读**。低成本可修，但优先级在 A2 之后。

#### A5. **学名模糊匹配只在 Web 端接线，桌面端未接**

- **问题**：`rca_core/names.py` 有完整的 `verify_name_gbif` / `verify_names` / `name_issues`，README 承诺「GBIF species-match 模糊匹配兜底」，但**桌面端（gui_fluent.py / gui.py）没有任何一处消费 `names.fuzzy`**。
- **谁受影响**：用桌面 GUI 的用户（Fluent 端是默认启动路径，`main.py` 无参数即 `launch_fluent()`）——**这是主力用户群**。
- **现状证据**：
  - `grep -rn "fuzzy" gui_fluent.py gui_fluent_pages.py gui.py` → **零命中**。
  - `grep -rn "fuzzy" js/*.js index.html` → 命中 `js/app.js:1183`（构造 `msg_key: 'names.fuzzy'`）、`js/table.js:1636-1639`（渲染 🔍 图标与标签）、`js/i18n.js` 三语条目。**Web 端已接，桌面端未接**。
  - README 路线图第 2 项「学名验证的 **UI 接线**（结果面板显示 `names.fuzzy` 候选）」—— **未勾选**，且只提了"UI 接线"，没说明"Web 端已接、桌面端未接"这个不对称。
- **影响**：README 承诺的 ICZN 安全兜底，桌面用户拿不到。**能力声明与实现不一致**，且这是一条明确在路线图上、成本很低的路。

#### A6. ICS 表在不同端曾不一致（已修，但需持续守护）

- **现状证据**：README「年代标尺版本锁定」记载 AUDIT-2026-10-22 修复了桌面端 98 行 vs 浏览器端 109 行的分叉，最严重的是桌面端把 "Late Pleistocene" 解析成**更新世整世**（2.58 Ma vs 真值 0.129 Ma，**偏老 20 倍**）。
- **现状**：`tests/test_ics_transport_table_parity.py` 已作为永久守卫存在。
- **判定**：**已闭环**，不列为缺口。但它说明：**三端分叉是这个代码库的结构性风险**，任何新增的"只有一端有"的功能都会重演 A5。因此 A5 修复必须覆盖三端，而不是只补桌面端。

---

### (b) 可及性缺口 —— 会让用户根本用不起来

#### B1. **没有无 GUI 的命令行提取入口**

- **问题**：`main.py` 的 argparse 只有 `mode`（default/gui/tk/server/web/modern/fluent）和 `--ui` / `--host` / `--port` / `--no-browser`。**没有任何 `extract` 子命令**。提取必须走 GUI 或 HTTP API。
- **谁受影响**：
  - 需要**批量**处理的科研用户（一篇论文 10 张图、一个大项目 200 张图）；
  - 想把本工具接进自己分析流程（R/Python/Jupyter）的科研人员；
  - 远程/无显示器的服务器用户。
- **现状证据**：`main.py:222-258` 的完整参数列表确认无提取子命令；`grep -rn "def .*batch"` 全仓库 → **零命中**（`rca_core/` 里 batch 字样只出现在 `db.py`/`extractor.py`/`matrix.py`/`names.py` 的注释与循环语义里，没有批量入口）。
- **影响**：**这是可及性缺口里最贵的一项**，因为它卡住的是"从单图工具变成生产力工具"这一步。而且它直接触达用户画像里的"不懂工程"——他们不写脚本，但他们**要跑 20 张图**。

#### B2. **CI 完全没有覆盖 sv_ttk / pywebview / matplotlib 三个可选依赖**

- **问题**：三个可选依赖在 CI 里零覆盖，而它们各自的失败模式都是**运行时崩溃**（Tk 主题渲染崩溃、PyWebView 窗口打不开、Usage 页图表渲染失败）。
- **谁受影响**：Linux 用户（pywebview + GTK/WebKit2 路径）、不装 PySide6 的用户（回落到 Tkinter + sv_ttk）。
- **现状证据**：
  - `grep -n "pip install" .github/workflows/*.yml` → 只有 `pytest ruff` / `cryptography keyring openpyxl Pillow` / `numpy` / `PySide6 PySide6-Fluent-Widgets` / `PySide6-Addons`。**没有任何 workflow 读 `requirements.txt`**（与团队简报一致，本次复核确认）。
  - `requirements.txt` 全部十个 marker 是 `python_version >= "3.10"`，**因此 `pip install -r requirements.txt` 在 CI 上理论可行**，只是没人读它。
  - CHANGELOG `[Unreleased]` 自己承认：「CI only installs cryptography — unpinned, so it has been running 50.x for a while — and never installs the other three, **so their green PRs were not evidence**」。仓库已经用文字记录了这个认知，但**认知没有变成 CI 配置**。
  - 已有手工验证：`redraw_range_chart` 在 matplotlib 3.10.9 下渲染过 PNG、`secrets_store` 在 cryptography 50.0.2 下 round-trip 过、sv_ttk 主题读写验证过、pywebview 6.2.1 的 `create_window(url/width/height/min_size)` 签名验证过 —— **这些是一次性手工验证，不是回归防护**。
  - `grep -rln "sv_ttk" tests/` → **零命中**。
- **影响**：可选依赖是**安装期降级**（装不上也能跑），但**运行期降级**才是用户真正撞到的墙。"我按 README 装了全部依赖，Usage 页还是白屏"这类问题，现在没有任何 CI 能提前发现。

#### B3. **没有屏幕阅读器可达的路径保证**

- **问题**：Web 端有一批 a11y 修复和 `aria-live` 播报，但**没有自动化 a11y 检查**，也没有桌面端（Qt/Tk）的无障碍检查。
- **谁受影响**：视障用户；有认知负荷问题的用户；用读屏软件跑数据的实验室。
- **现状证据**：`js/` 与 `index.html` 存在 `aria-checked` / `aria-live` / `aria-labelledby`（`index.html` 有 `dz-aria`、`conn-mode-label`、`remember-key-label` 等 id 目标）；`docs/UI-REVIEW-WEB-2026-10-17.md` 记录了大量对比度实测（`.ios-switch[aria-checked=true]` 暗色 2.54:1 FAIL 等）。
  - **但**：`grep -rni "aria-|a11y|accessib|screen reader"` 在 `tests/` 下**零命中**（命中全在 `docs/` 与 `.workbuddy/memory/` 的评审记录里）。评审是**人工一次性**的，不是回归防护。
  - 桌面端：`docs/UI-REVIEW-GUI-2026-10-17.md` 扫描的是 `setAccessibleName` 等 API，**但同样没有自动化断言**。
- **影响**：**低于 B1/B2**。对一个"把论文图变成数据"的科研工具，屏幕阅读器是低频需求；但注意 `docs/UI-REVIEW-WEB-2026-10-17.md:110` 记录的"视觉文字中文 / 读屏播报英文"的双通道语言不一致 —— 这是**已经在评审里点名、但没修**的具体缺陷，成本低。

#### B4. **部署场景下溯源导出的 403 仍未修**

- **问题**：`RCA_PROVENANCE_TOKEN` 部署场景下，Fluent 端溯源导出 403。本地 loopback 不受影响。
- **谁受影响**：把 `server.py` 部署到服务器/内网、让多人远程用的用户 —— 恰好是**数据量最大、最需要批量复核**的那批用户。
- **现状证据**：`server.py:97` `_PROVENANCE_TOKEN_ENV = "RCA_PROVENANCE_TOKEN"`；`server.py:2367` 注释「``RCA_PROVENANCE_TOKEN`` shared secret」；`gui_fluent_history_detail.py:401-408` 读取该 env 并作为 `X-Provenance-Token` 发送。
- **影响**：**这是"部署场景被本地场景掩盖"的典型**。本地测试全绿，远程部署直接断。修复面窄，但会让"部署给实验室"这条路走不通。

#### B5. **ICS 刷新管线已经写好，但没有任何自动触发**

- **现状证据**：`scripts/update_ics.py` 具备双上游通道、纯 stdlib 解析、schema 校验、显式 `--write-canonical`、拒绝 fixture 晋升。
- **缺口**：**没有定时任务、没有 CI 检查标尺是否过期、没有"标尺落后多久"的提示**。
- **影响**：这是"机制已备、只差接线"的低成本项。仓库的 `repo-health.yml` 周巡检机制**正好是天然的挂载点**。

---

### (c) 交付缺口 —— 能用但不敢发给同事/发表的东西

#### C1. **CITATION.cff 版本与 release 不同步**

- **问题**：`CITATION.cff:12` `version: 0.1.4`、`date-released: "2026-10-22"`，但 v0.1.4 tag 的实际创建日期是 **2026-10-21**（`git for-each-ref`）。且 `CITATION.cff` 指向的 release 版本落后于 HEAD 22 个提交。
- **谁受影响**：按 CITATION.cff 引用本工具的论文作者 —— 引用的是**一个有数据正确性缺陷的版本**（见 A1）。
- **现状证据**：`CITATION.cff` 全文已读（1-47 行），version 与 date-released 均为手写常量；`git tag` 显示 v0.1.4 = 2026-10-21。
- **影响**：**引用元数据错位 + 引用到一个 bug 版本**。修 A1 的发布动作会顺带解决日期；版本同步需要一条 release 流程。

#### C2. **许可证是"继承式描述"，不是标准 OSI 标识**

- **问题**：`LICENSE` 写「The software is provided for RESEARCH AND EDUCATIONAL USE ONLY」并声明「inherits that project's license terms. The authoritative text lives with the upstream repository」。`CITATION.cff:14` 的 license 字段是一整段自然语言（**不是 SPDX 标识**）。
- **谁受影响**：期刊投稿系统、机构合规扫描器、GitHub 的 license 检测 —— 都会看到"无标准许可证"。
- **现状证据**：`LICENSE` 全文（1-53 行）确认：软件条款 = 上游 RLPE 的条款，**指向外部仓库而非内嵌文本**；数据 = CC0 1.0（这个是好的，且 `darwin_core.py` 会把 CC0 写进 `<distribution><license>`，与 LICENSE 一致）。
- **影响**：**"研究/教育用途"这一条对科研用户是真问题** —— 部分期刊和企业资助项目要求明确的许可条款。不过**这一项需要项目所有者拍板**（我不能替 syxscott 决定上游条款能否内嵌/改写），因此我把它列为**需要决策**而非**需要开发**。

#### C3. **没有 CONTRIBUTING.md / SECURITY.md / ISSUE_TEMPLATE**

- **问题**：仓库根目录只有 `CHANGELOG.md` 和 `README.md`。`.github/` 下只有 `dependabot.yml` 和 `workflows/`。
- **谁受影响**：
  - **维护者**：没有 CONTRIBUTING.md，别人不知道怎么贡献，也不知道该跑哪套测试（README 里有 4 条命令，但没写成"贡献流程"）；
  - **科研用户**：没有 SECURITY.md，用户不知道"我的 API key / 我的图怎么处理"该找谁；
  - **所有人**：没有 ISSUE_TEMPLATE，用户提 issue 时不知道该附什么（图？日志？三端哪个？）。
- **现状证据**：`ls *.md` → 仅 `CHANGELOG.md` `README.md`；`ls -R .github/` → `dependabot.yml` + `workflows/{ci,codeql,repo-health}.yml`；`.github/ISSUE_TEMPLATE` **不存在**。
- **影响**：**低成本、高杠杆**。注意 repo-health 机器人会**自动开 issue**，没有模板意味着机器人开的 issue 和用户 issue 混在一起无人认领。

#### C4. **没有 release 流程，CHANGELOG 的 Unreleased 无限积压**

- **问题**：版本号停在 v0.1.4（2026-10-21），而 `[Unreleased]` 里攒了 **4 大块**（仓库布局迁移、依赖地板抬高、自动化机器人、2026-10-28 修复约 20 项 + PBDB/not_drawn 两个数据正确性修复）。**22 个提交、105 文件、+7119 行**没有进入任何发布。
- **谁受影响**：所有下载 GitHub Release 的用户 —— 他们拿到的不是仓库宣称的能力。
- **现状证据**：`git tag` → v0.1.0…v0.1.4，全部在 2026-10-15…2026-10-21 两天内打完；`awk '/^## \[Unreleased\]/,/^## \[0\.1\.4\]/' CHANGELOG.md` → 内容巨大；**仓库无 `.github/workflows/release.yml`**。
- **影响**：这是**A1 的直接成因**。没有发布流程，好修复就永远停在 Unreleased，用户永远拿到坏版本。**修 A1 的前提是修 C4**。

#### C5. **CITATION/README 里的 BibTeX 与 CITATION.cff 双份维护，且不含版本号占位**

- **现状证据**：README 第 448 行起的 BibTeX 块是手工维护的第二份引用信息，与 `CITATION.cff` 并存；README 的 BibTeX `note` 字段写「ICS timescale v2024/12; validated end-to-end on 40 figures」，**没有"请替换为你的 release 版本"的提示**。LICENSE 第 24 行倒是有「please cite it as set out in CITATION.cff, and say which release you used」——**说法在 LICENSE 里，格式在 README 里，版本在 CITATION.cff 里，三处分离**。
- **影响**：论文作者大概率引用错版本或漏版本号。

#### C6. **导出数据缺少"数据字典 / 列语义规范"**

- **问题**：README「提取字段」小节存在，但导出物（CSV/XLSX/DwC/PBDB）本身**不带列定义**。DwC 走标准（`occurrenceID`/`basisOfRecord=MachineObservation`/`locality`），**这一层是好的**；但 CSV/XLSX 的自定义列（如 `range_base_label`、`range_top_label`、`confidence`、`reason_codes`）**没有随文件分发的 schema 说明**。
- **现状证据**：`rca_core/exporter.py:1627-1632` 定义了内部类型映射（`"confidence": "float"`）；`darwin_core.py:591-619` 有完整的 DwC term URL 映射 —— DwC 侧规范；`exporter.py:2574` 把 `image_sha256` 写进 provenance（**图片哈希进了导出，好**）。
- **影响**：中低。用户能靠 README 理解大部分字段，但 `confidence` 是模型自报、`reason_codes` 取值域是什么，README 没写全。

---

## 2. MVP 三件事

> 判定标准：**科研用户从"下载了"到"敢把数据放进论文"之间，卡点最大的三个。**

### MVP-1：**发布 v0.1.5，把已修好的数据正确性缺陷发出去**（3 人日）

- **内容**：跑一次 release 流程 —— 定版 CHANGELOG `[Unreleased]` → bump 版本 → build + smoke → tag v0.1.5 → 对齐 `CITATION.cff`（version + date-released）→ release notes。
- **取舍理由**：
  - 修复**已经写好并测过**（PBDB section-age、`not_drawn`/`uncertain` 分流、JS 质量漂移约 20 项），**边际成本极低但边际收益最高**：不需要写任何新代码，就消除了"已发表数据科学性错误"。
  - 这是**唯一一项能立刻阻止坏数据继续产生**的动作。其余所有项都是"让未来更好"，这一项是"止血"。
  - 它同时解掉 C1、C4、C5 三条交付缺口 —— **一份 CHANGELOG 工作量撬动三个交付缺口**。
  - **这是所有其他项的前置**：A3（ICS 2026-06）、A5（桌面端 fuzzy 接线）都应该进这个 release，而不是各自拖成下一个"未发布"。
- **为什么不是别的**：B1（CLI）价值高但不紧急 —— 用户可以一张张点，只是慢；A2（overlay）价值高但工作量大；发布是唯一"零新代码、立即消除真实伤害"的一项。

### MVP-2：**延限图 overlay 标注 —— 把抽取端点映射回原图坐标**（8–12 人日）

- **内容**：在原图上叠加每一行延限的起止位置与学名标签，点击表格行高亮对应标注，点标注回跳表格行。
- **取舍理由**：
  - **它就是用户的核心工作流**。README 通篇讲"人工校对表格"，而校对的前提是"能一眼看出这个数字对不对"。没有 overlay，校对 = 回到原图肉眼找 = 效率回到手抄。
  - **它是"证据链"的最后一块拼图**：现在有图级哈希、字段级 reason_code、覆盖契约账本 `coverage.decisions`，唯独缺**单元格级定位**。overlay 补上后，A2 与已有的 `grounding.py`（`check_name_grounding`）、`geometry.py`（`pixel_to_data` 逆变换）、`redraw.py` 形成闭环 —— **地基都在，缺的是接线**。
  - **它有别于"新增一个提取模式"**：新模式增加覆盖面，overlay 降低**每张图**的校对成本。工具已有 5 种模式 + auto 判定（`rca_core/modes.py`），覆盖面不是瓶颈；**复核成本才是**。
  - **它服务的是"每行要知道依据"这条不可协商的诉求**，且是**唯一**能把它从字段级推到坐标级的手段。
- **为什么不选 A5（桌面端 fuzzy 接线）先做**：A5 成本低（1–3 人日）、是 README 已经承诺却没兑现的承诺、必须修 —— 但它**只影响名字拼写这一类错误**，而 overlay 影响**所有类型的错误**。A5 应作为 MVP-1 release 的附带项（一并做，成本太小不值得单独立项）。

### MVP-3：**CI 覆盖全部可选依赖 + 一个 `extract` 命令行子命令**（5–8 人日）

- **内容 A（2–3 人日）**：新增一个 CI job，`pip install -r requirements.txt`（或分片装 sv_ttk / pywebview / matplotlib），跑对应的 smoke：sv_ttk 主题读写、pywebview `create_window` 签名、matplotlib 渲染一张 PNG。
- **内容 B（3–5 人日）**：`python main.py extract --image FIG.png --mode auto --out OUTDIR`，走现有 `rca_core.extractor`，复用现有缓存与历史库。
- **取舍理由**：
  - **B1（CLI）是"从单图工具到生产力工具"的唯一门槛**。用户画像是"不懂工程"，他们不会写脚本 —— 但他们**有一篇论文的 10 张图要跑**。GUI 点 10 次可以忍，200 张不能忍。有了 CLI 才能谈批量复核（而批量复核正是 B4 部署场景的前提）。
  - **A（CI 依赖覆盖）的收益被仓库自己低估了**。CHANGELOG 原话：「their green PRs were not evidence」。现在 CI 全绿，但**绿灯不覆盖用户会遇到的三个失败面**。这不是"工程洁癖"，这是**绿灯在说谎** —— 绿灯说谎对科研用户的伤害是"我以为 CI 过了就没事，结果装完跑不起来"。
  - **两者共用同一个基础设施**：都需要"在 CI 里装上真实环境"。合起来做边际成本低。
  - **注意**：这是**唯一**一条我建议"接 CLI 而非接 PyPI/插件体系"的地方 —— 后者是工程洁癖，见第 5 节。
- **为什么不单选 CI 依赖覆盖**：它防的是回归，不解决"用户今天怎么用"。CLI 解决"用户今天怎么用"。两者一起做，因为基础设施重叠。

> **补充说明**：三件事的排序不是按"用户价值"排的，是按**"止血 > 提效 > 防未来"**排的。如果项目所有者认为 A2（overlay）才是唯一值得投人的方向，把 MVP-1 做完发出去之后就该做 overlay —— **MVP-1 无论如何都要做，且要先做**。

---

## 3. 成熟软件检查项（可被 CI/测试验证）

| # | 检查项 | 判定方式 | 当前状态 | 依据 | 差距 |
|---|---|---|---|---|---|
| M1 | **发布版 = 仓库宣称的能力**：`git tag` 最新 tag 的 commit 必须包含所有已合入 main 的数据正确性修复 | CI job：比较最新 tag 与 HEAD 的 diff，若 diff 触及 `rca_core/standards/`、`rca_core/exporter.py`、`rca_core/quality.py` 且 CHANGELOG 仍有 `[Unreleased]` 的 `Fixed` 条目 → 失败 | ❌ **不通过** | 最新 tag v0.1.4 (2026-10-21)，HEAD 领先 22 commits / 105 files / +7119 行；`git show v0.1.4:rca_core/standards/pbdb.py` 无 `range_base_label`、`not_drawn` 零命中 | **整个发布流程缺失**。一次 release 即可翻绿 |
| M2 | **所有声明的可选依赖在 CI 里被真实安装并冒烟** | CI job 装 `sv_ttk` `pywebview` `matplotlib` 并各跑一个断言（主题读写 / `create_window` 签名 / 渲染 PNG）；或直接 `pip install -r requirements.txt` | ❌ **不通过** | `grep "pip install" .github/workflows/*.yml` 只有 `pytest ruff`/`cryptography keyring openpyxl Pillow`/`numpy`/`PySide6*`；无 workflow 读 `requirements.txt`；`grep -rln sv_ttk tests/` 零命中 | **3 个依赖零覆盖**。仓库已在 CHANGELOG 承认"green PRs were not evidence"，但未落地为配置 |
| M3 | **CI 绿灯覆盖真实安装路径**：CI 装出的依赖集 == 用户 `pip install -r requirements.txt` 得到的依赖集 | CI 中执行 `pip install -r requirements.txt` 并断言 import 成功（或显式断言"哪些依赖在 CI 里缺席"的白名单为空） | ❌ **不通过** | requirements.txt 十个 marker 全为 `python_version >= "3.10"`，本身可在 CI 装；CI 却不读它 | **CI 与用户安装路径分叉**。加一个 job 即可 |
| M4 | **三端行为一致**：同一输入在 Tkinter / Fluent / 浏览器三端产生同一结果 | 已有 parity 套件（差分 parity 1,363 matched / 0 diverged）+ `test_ics_transport_table_parity.py` | ⚠️ **有守卫但有已知盲区** | 差分 parity 全绿；但 A5 显示 **fuzzy 命名只在 Web 端接线**，桌面端零命中；A6 显示 ICS 曾 98 vs 109 行分叉、偏老 20 倍 | **"新功能只在三端之一落地"无自动化检测**。需要一个"能力注册表 × 三端"的一致性断言 |
| M5 | **导出物自带溯源**：每份导出文件能唯一定位回源图 | 测试：导出后断言文件中含 `image_sha256`；DwC 断言含 occurrenceID / basisOfRecord / locality | ⚠️ **部分通过** | `exporter.py:2574` 把 `image_sha256` 写进导出 provenance；`darwin_core.py:591-619` 有完整 DwC term 映射；但**无单元格级坐标**（见 A2） | **图级溯源 ✅ / 单元格级溯源 ❌**。overlay 补齐后者 |
| M6 | **仓库具备开源协作基础设施**：CONTRIBUTING / SECURITY / ISSUE_TEMPLATE / release workflow | 文件存在性检查（repo-health 脚本已做同类事） | ❌ **不通过** | `ls *.md` 仅 CHANGELOG + README；`.github/` 无 ISSUE_TEMPLATE；无 `release.yml`；无 CONTRIBUTING / SECURITY | **4 项全缺**。C4 是 M1 不通过的根因 |

---

## 4. 优先级表（MoSCoW + RICE 排序，12 项）

> RICE = Reach × Impact × Confidence ÷ Effort。Reach 以"月活跃科研用户数"粗估相对值（1–10）；Effort 为人日。Confidence 0.5/0.8/1.0。
> 影响对象：**科**=科研用户　**维**=维护者　**双**=两者。

| # | 项目 | 类别 | 影响对象 | MoSCoW | Reach | Impact | Conf | Effort | RICE | 依赖前置 |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | **发布 v0.1.5**（定版 Unreleased + bump + tag + 对齐 CITATION.cff） | 交付 | 科+维 | **Must** | 10 | 10 | 1.0 | 3 | **33.3** | 无（一切的前置） |
| 2 | **修 A1 的两个数据正确性缺陷的发布**（随 #1 一并发布） | 可信 | 科 | **Must** | 10 | 10 | 1.0 | 0（已修） | ∞ | 代码已在 HEAD |
| 3 | **延限图 overlay 标注**（端点映射回原图坐标，供复核） | 可信 | 科 | **Must** | 9 | 9 | 0.8 | 10 | **6.5** | #1 |
| 4 | **CI 覆盖全部可选依赖**（sv_ttk / pywebview / matplotlib 冒烟） | 可及 | 科+维 | **Must** | 7 | 7 | 1.0 | 2.5 | **19.6** | 无（可与 #1 并行） |
| 5 | **`extract` CLI 子命令**（无 GUI 批量提取） | 可及 | 科 | **Must** | 6 | 8 | 0.8 | 4 | **9.6** | 无（复用 rca_core.extractor） |
| 6 | **ICS 标尺升级到 v2026-06**（管线已就绪，需裁定 Roadian） | 可信 | 科 | **Should** | 6 | 7 | 0.8 | 2 | **16.8** | Roadian 底界的**业务决策**（Permophiles #78） |
| 7 | **桌面端接上 `names.fuzzy`**（补齐 README 承诺的三端一致性） | 可信 | 科 | **Should** | 5 | 6 | 1.0 | 2 | **15.0** | #1（随 release 发） |
| 8 | **CONTRIBUTING + SECURITY + ISSUE_TEMPLATE** | 交付 | 科+维 | **Should** | 5 | 6 | 1.0 | 1.5 | **20.0** | 无 |
| 9 | **修 B4 部署态溯源 403**（`RCA_PROVENANCE_TOKEN` 在 Fluent 端） | 可及 | 科 | **Should** | 3 | 7 | 0.8 | 1.5 | **11.2** | 需复现（本地 loopback 测不出来） |
| 10 | **`confidence` 语义在所有导出路径统一标注**（模型自报，非校准概率） | 可信 | 科 | **Should** | 4 | 5 | 1.0 | 1 | **20.0** | 无（`exporter.py:2592` 已有部分实现，需补齐其余导出路径） |
| 11 | **ICS 过期自动巡检**（repo-health 周检挂载点） | 交付 | 维 | **Could** | 2 | 4 | 0.8 | 1 | **6.4** | #6 |
| 12 | **导出数据字典**（列语义 + reason_codes 取值域随文件分发） | 交付 | 科 | **Could** | 3 | 5 | 0.8 | 3 | **4.0** | 无 |

**排序说明**：
- **#1、#2、#4、#8 可以在同一周内做完**（合计 ~7 人日），且直接覆盖 M1–M3、M6 四条成熟度判据中的四条。**这是本项目性价比最高的一周**。
- **#3（overlay）是唯一"重投入但决定核心体验"的一项**，必须投，但应排在发布之后。
- **#6 的工作量极小（2 人日）但卡在一个业务决策上**（Roadian 底界）—— 需要项目所有者或古生物学家拍板，不该由工程侧默认。
- **#9 影响面小（Reach 3）但严重性高**，因为它精确命中"实验室部署"这个最有价值的场景。

---

## 5. 明确"不建议做"清单

| 不建议项 | 理由 |
|---|---|
| **K8s / 灰度发布 / 多租户 / 服务网格** | 这是个人/小团队维护的开源科研工具。三个入口（`server.py` / `gui.py` / `gui_fluent.py`）共享一个 `rca_core`，本地单机运行是正确形态。引入这些只会增加维护面积、减少科研用户能拿到手的比例。 |
| **插件系统 / 可扩展 provider 架构** | `rca_core/llm.py` + `model_capabilities.py` + `rca_core/providers` 已有 provider 抽象，再抽象一层是工程洁癖。 |
| **重写前端框架（换 React/Vue）** | `js/` 24,005 行 + 15 个 Node 套件 2,576 断言 + 差分 parity 1,363 全绿。重写的收益是"看起来现代"，对"从论文图提取可追溯数据"零贡献。 |
| **把 `rca_core` 拆成多个包 / 改目录结构** | CHANGELOG 显示 **2026-10 刚做过一次大规模迁移**（root 63→22 文件，`tests_*` 全部下沉）。再迁一次是净负收益：迁移本身产出了 3 个 bug（锚点常量、pytest 收集、嵌套 mark 重映射），刚修完就该稳定。 |
| **引入新的状态管理/依赖注入框架** | 同上。 |
| **为 `server.py` 写完整的异步任务队列** | B4 的 403 是 token 传递问题，一个 if + header 即可。队列是杀鸡用牛刀。 |
| **做完整的 a11y 认证/审计（WCAG 全量）** | 科研工具的读屏需求是长尾。但**要修** `docs/UI-REVIEW-WEB-2026-10-17.md:110` 已点名的"视觉中文 / 读屏英文"双通道不一致 —— 那个便宜。 |
| **引入数据库服务（Postgres 等）** | SQLite + WAL 已够（`rca_core/db.py` 有 schema 版本表、并发写保护）。历史记录是单机个人数据量。 |
| **自动给 ICS 打补丁 / 从网络自动更新标尺** | `scripts/update_ics.py` 刻意要求 `--write-canonical` 显式晋级、拒绝 fixture 数据。**这个设计是对的**，不要为了"自动化"而放宽。标尺更新必须有人类裁定（Roadian 就是例证）。 |
| **追求覆盖率数字 / 补"为了覆盖率而存在"的测试** | 4,360 pytest + 2,576 断言已足够。CHANGELOG 自己记录过"一个测试永远不可能失败"的问题 —— 该修的是**测试有效性**，不是数量。 |
| **把 overlay 做成独立 3D/新前端特性** | overlay 的价值 100% 来自"贴在原图上、一眼对得上"。用任何新技术栈实现，只要不是原图叠加，就没有价值。 |
| **多语言扩到 5+ 语种** | 中英日三语已覆盖主要地层学期刊社区。扩语种的边际价值远低于修 overlay。 |

---

## 6. 待确认事项

1. **Roadian 底界**：二叠纪分会有争议（Permophiles #78）。采纳 2026-06 版会让 ICS 标尺与部分已发表文献不一致 —— **需要业务裁定，不是工程判断**。谁拍板？
2. **上游 RLPE 许可证能否内嵌/改写**？当前 LICENSE 指向外部仓库，"研究/教育用途"条款对部分期刊是硬门槛。**需要项目所有者决策**，我不替业务方定。
3. **`v0.1.5` 是否包含 ICS 2026-06**？如果 Roadian 短期无定论，建议 v0.1.5 **不带** ICS 更新（标尺版本变更应在独立 release 里做，避免与数据正确性修复混在一起，论文引用时可分别注明）。
4. **target 用户是否真的需要批量**？B1（CLI）的 Reach 我按 6 估的 —— 若目标用户是"单篇论文 3-5 张图"的实验室，CLI 的价值降到 Should；若目标含"大型数据集/教科书项目"，它应升到 Must 第一位。**这个假设需要项目所有者确认**。
5. **overlay 的形态**：桌面端（PySide6）与浏览器端**都**要做，还是先做一端？考虑到 M4 的已知盲区（"新功能只在三端之一落地"），我倾向两端都做，但这是成本 2 倍的决定。
6. **`server.py` 的部署目标形态**（自建服务器 / 内网多用户 / 云 VM）？决定 B4 的修法，也决定是否需要考虑多用户并发下的历史库隔离。

---

## 附：本次探查用到的证据命令（可复现）

```bash
git log --oneline v0.1.4..HEAD                    # 22 commits
git diff --stat v0.1.4..HEAD                     # 105 files, +7119/-694
git show v0.1.4:rca_core/standards/pbdb.py | grep -c range_base_label   # 0 → bug 在发布版
git show v0.1.4:rca_core/standards/pbdb.py | grep -c not_drawn          # 0 → bug 在发布版
grep -n "pip install" .github/workflows/*.yml     # 无 requirements.txt
grep -rln "sv_ttk" tests/                        # 0
grep -rn "fuzzy" gui_fluent.py gui.py            # 0（Web 端 js/ 有命中）
grep -rn "def .*batch" --include=*.py .          # 0
sed -n '222,258p' main.py                        # argparse 无 extract 子命令
ls *.md                                          # 仅 CHANGELOG.md README.md
ls .github/ISSUE_TEMPLATE                        # 不存在
grep -n "ICS_VERSION" rca_core/standards/ics.py # ICS v2024/12
```
