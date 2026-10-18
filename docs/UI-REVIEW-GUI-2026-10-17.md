# UI 审查报告 — GUI 端（PySide6 Fluent + Tkinter）

- 审查范围：`gui_fluent.py`、`gui_fluent_providers.py`、`gui_fluent_pages.py`、`gui_fluent_history_detail.py`、`gui_fluent_onboarding.py`、`gui.py`
- 审查日期：2026-10-17
- 环境：Windows 11，`D:/Program Files/ananconda3/python.exe`
- **PySide6 未安装**（`ModuleNotFoundError`）。所有 Fluent GUI 结论均为**读代码判断（静态分析）**，已在每条缺陷中标注。
- **Tkinter 可用**，Tkinter 结论中标注「实测」的部分为真实实例化 `RangeChartApp` 并驱动控件得到的输出。
- 未修改任何源码。

---

## 1. 总体结论

1. **Fluent GUI 的线程模型是对的，但有一处必崩的槽函数**：`gui_fluent_providers.py:756` 与 `:770` 用了 `self._tr`，而 `ProviderWizard.__init__` 只赋了 `self._t`。用户只要在向导里留空名称/端点点保存，或填了非法 extra_headers 点保存，就会在 Qt 槽里抛 `AttributeError`；在 PySide6 里槽函数抛异常会打印 traceback 并**中断该槽**，向导卡在无法保存的状态。这是本轮唯一的 P0。
2. **破坏性操作缺确认**：`gui_fluent_providers.py:1103 _delete()` 直接 `store.remove()` + `save()`，无任何确认框，且卡片上的删除按钮只有 tooltip。这是**不可撤销的 API key + 端点删除**。
3. **一处 P2 被主审实测推翻**：原文说 Tk 大结果集会「阻塞数百毫秒到数秒」，
   实测 `_on_result` 在 5000 行只花 **0.117 s**（200 行 0.358 s 是 ttk 样式首次
   初始化的一次性开销，之后持平）。已降级为 P3 —— 见 F-10 的补测小节。
4. **两处「新增控件忘接逻辑」的死控件**：向导里的 API Key 字段选择器（`gui_fluent_providers.py:583-586`）读不到也写不进 `LlmProvider`；`ExtractPage._cmb_key_field` 同理。选什么都不影响请求。另有 `_rebuild_pivot`（`gui_fluent.py:2510`，35 行）、`cancel_requested` property 等整块死代码。
5. **代码体量确认了你担心的「上帝文件」**：`gui_fluent.py` 4219 行、6 个类、232 个函数/方法，承担 **10 个互不相干职责**；其中 964 行纯注释 + 541 行 docstring，真正代码 2714 行 —— **注释占 36%**，而且大量注释在描述"这个 bug 曾经是什么"，属于历史档案而非当前行为说明。拆分可行性很高（见 §3）。
6. **三端重复是真实存在的**：`save_config` 在两个 GUI 各写一份且签名已分叉（`_collect_extraction_futures` 返回值从 list 变成 tuple，调用方只有 Fluent 更新了）；`_resolved_mode`/`_current_mode` 的模式识别、`_maybe_thumbnail`、`_save_to_history` 的 HistoryRecord 构造三处各写一遍。同一份"识别 zonation_chart"的 if 链在两个文件里逐字重复。

---

## 2. 缺陷清单

### P0

#### F-01 向导保存路径引用不存在的 `self._tr`，两条路径必崩

- **位置**：`gui_fluent_providers.py:756`、`gui_fluent_providers.py:770`
- **验证方式**：**实测**（`ast` 精确核对 + `Translator.t` 签名实测）。已确认 `ProviderWizard` 全部 18 处翻译调用中，16 处用 `self._t`（L511 正确赋值），仅 L756/L770 两处写 `self._tr`；`ast` 遍历 `ProviderWizard` 全文**没有任何 `self._tr = ` 的赋值语句**。另实测 `Translator.t` 签名为 `(self, key)` —— **只接受一个 key**，所以 L770 的 `self._tr.t("wizard.extraHeadersIgnored", {"reason": headers_err})` 即使 `_tr` 存在也会再抛一次 `TypeError`。
- **复现步骤**：
  1. 启动 `python main.py --ui fluent`，导航到「LLM 提供商」页
  2. 点「添加提供商」→ 选任意预设或「自定义提供商」
  3. 路径 A（必现）：清空「名称」或「端点」→ 点「添加」/「保存」 → L750 判空进入 L756 → `AttributeError: 'ProviderWizard' object has no attribute '_tr'`
  4. 路径 B（必现）：在「额外请求头」里填 `{"a":}`（非法 JSON）→ 填好名称端点 → 点保存 → L770 → 先 `AttributeError`，修好 `_tr` 后仍会 `TypeError`
- **后果**：
  - 路径 A：用户**无法从向导新建 provider**。必填校验从未向用户展示错误信息，异常直接从槽函数抛出。PySide6 下未捕获的槽异常会打印 traceback 并中断槽——对话框停在原地，看似"点了没反应"。
  - 路径 B：非法 JSON 的警告同样不显示，用户以为 headers 被接受了。
  - **测试完全没覆盖**：`tests/`、`tests_gui_fluent.py` 中 `ProviderWizard` / `_on_ok` / `fillRequired` / `extraHeadersIgnored` 的引用数为 **0**。这是新增控件时漏接/写错的典型遗留。
- **严重度**：**P0**
- **置信度**：**高**（AST 穷举 + 运行时签名实测）

---

### P1

#### F-02 删除 provider 无任何确认，不可撤销

- **位置**：`gui_fluent_providers.py:1103-1110`（`_delete`）、触发点 `:940`（`card.btn_delete.clicked`）、卡片按钮定义 `:246`
- **验证方式**：**读代码判断**（PySide6 未装）。已确认 `gui_fluent_providers.py` 全文**没有 `MessageBox` import**（第 40-45 行的 qfluentwidgets import 列表里没有它），`_delete` 方法体只有 4 行：remove → save → invalidate cache → refresh + sync。
- **复现步骤**：导航到 LLM 提供商页 → 悬停任意卡片 → 点「删除」（透明图标按钮）→ 卡片立即消失，provider JSON 文件被 `store.save()` 重写。
- **后果**：
  - 该 provider 的 **name / endpoint / model / api_key 全部丢失**，无撤销、无回收站、无提示。
  - 删除按钮只有 tooltip（`:252` `wizard.delete` → "删除"），**没有说明"此操作不可撤销"**。
  - 与同一文件里的历史删除形成刺眼反差：`gui_fluent_pages.py:570` 的 `_on_delete` 和 `gui_fluent.py:2602` 的 `_on_discard_edits` 都规规矩矩弹了 `MessageBox` 确认框。**这里是三处破坏性操作里唯一漏掉的一个**。
  - 更糟：`_delete` 没有检查"是否是当前正在使用的 provider"。删掉 active provider 后 `current_provider()` 返回 `None`，`ExtractPage._on_extract:1862` 走 `provider = None` + legacy key 分支 —— 用户下一次提取会**静默改用另一个 provider 的凭据**去发请求。
- **严重度**：**P1**
- **置信度**：**高**（静态；对比证据充分）

#### F-03 Tkinter 端删除 provider 同样无确认，且**连当前 provider 都不检查**

- **位置**：`gui.py:2742-2749`（`_delete_provider`）
- **验证方式**：**实测**（grep 全 `gui.py` 找 `askyesno|messagebox.ask` → **0 命中**）。
- **复现步骤**：`python main.py gui` → 点「添加提供商」向导 → 「已配置的提供商」列表 → 选中任一非当前 provider → 点「删除」→ 立即移除。
- **后果**：与 F-02 同类，但更严重一层——Tk 版的 `_delete_provider` 会无条件 `store.remove(provider_id)`，**连删掉当前 provider 都不拦**。而且 Tk 版的 `on_close` 回调 `self.on_close()` 会**立刻关闭整个向导窗口**，用户连后悔的机会都没有（对比：`_set_current` 走的是同一路径，见 F-04）。
- **严重度**：**P1**
- **置信度**：**高**（实测 grep + 实测实例化确认向导类存在 15 个方法、其中 0 个确认逻辑）

#### F-04 Tkinter 向导"设为当前"会关闭整个向导窗口

- **位置**：`gui.py:2736-2740`（`_set_current`）
- **验证方式**：**读代码判断**。
- **复现步骤**：Tk GUI → 打开提供商向导 → 「已配置的提供商」列表 → 点任意 provider 的「设为当前」→ `store.set_current()` → `_render_provider_list()` → **`self.on_close()`** → 向导销毁。
- **后果**：
  - 用户只是想切换当前 provider，结果**整个向导被关掉**。如果用户本来是想"再加一个 provider"，这个操作打断了他的流程。
  - `_delete_provider`（`:2748`）有完全相同的行为 —— 这就解释了 F-03 里"连后悔机会都没有"。
  - Fluent 版对应逻辑（`gui_fluent_providers.py:1067 _set_current`）**不开任何对话框**，只刷新列表 —— 两端行为不一致，但 Fluent 版的体验明显更好。Tk 版这里像是复制粘贴时把"关闭"也一起带上了。
- **严重度**：**P1**（交互阻断级，但可通过重开向导恢复）
- **置信度**：**中高**（静态推断；我未能实测点击路径，因为 `ProviderWizard` 无 `withdraw()` 方法、需真实显示窗口）

#### F-05 `API Key 字段` 下拉框是纯装饰控件，选什么都不生效

- **位置**：`gui_fluent_providers.py:581-586`（构建）、`:689-695`（`_pick_preset` 不碰它）、`:747-796`（`_on_ok` 不读它）
- **验证方式**：**实测**（grep 全仓 `ANTHROPIC_AUTH_TOKEN` → **只出现在 `gui_fluent_providers.py:584` 这一行**；`LlmProvider` 的 13 个字段实测列表中没有任何 key-field-name 字段）。
- **复现步骤**：向导 → 预设网格里选一个 Anthropic 系预设 → 表单出现「API Key 字段」下拉框 → 选 `ANTHROPIC_API_KEY`（而非默认的 `ANTHROPIC_AUTH_TOKEN`）→ 填 key → 保存 → 用该 provider 跑一次提取 → 抓包/查 `rca_core/llm.py` 实际发出的请求头。
- **后果**：下拉框给出"我在配置请求头名称"的可信暗示，但 `_on_ok` 构造 `LlmProvider`（`:774-796`）时**完全不读它**，`LlmProvider` 也无处存放。选 `ANTHROPIC_API_KEY` 的用户会以为请求头改成了 `x-api-key`，实际仍是 `Authorization`。**典型的新增控件忘接逻辑**——UI 有、逻辑无、测试无。
- **严重度**：**P1**（凭据相关，且是静默错误）
- **置信度**：**高**（全仓 grep + dataclass 字段实测）

#### F-06 Tkinter 提取进行中仍可更换图片 → 结果与预览错配

- **位置**：`gui_fluent.py:1811-1826`（`_set_busy` 只禁用了 `btn_extract` / `btn_cancel` / 编辑行 / 导出，**没有禁用 `btn_choose` / `btn_paste` / `txt_caption`**）；结果落盘在 `:2079-2100`
- **验证方式**：**读代码判断** + **实测 grep**（`grep "btn_choose.setEnabled|btn_paste.setEnabled|txt_caption.setEnabled" gui_fluent.py` → **0 命中**，Fluent 端从未禁用过这三个控件）。Tk 端 `gui.py:1783 _set_busy` 只动 `btn_extract`。
- **复现步骤**（Fluent）：
  1. 选图 A → 「运行次数」设为 5 → 点「开始提取」
  2. 转盘转起来、状态栏显示"加载中"，此时 `btn_choose` / `btn_paste` / 标题输入框**仍然可点可输入**
  3. 在提取返回前换成图 B，并把标题改成 B 的说明
  4. 提取返回 → `_on_result` 用 `self.image_path`（= B）写历史记录，`_maybe_thumbnail()` 取 B 的缩略图，而 `result` 是从 A 提取出来的
- **后果**：**历史记录被写成了错误的一行** —— 文件名/缩略图/图片尺寸都是 B 的，提取结果却是 A 的。这条记录的审计价值归零，而用户完全看不到任何异常提示（状态栏只会显示"完成"）。
- **严重度**：**P1**（数据正确性 + 审计链污染）
- **置信度**：**中高**（静态；未在真实 Qt 下复现，因为 PySide6 不可用。但"提取中可换图"这一点从代码上是确定的：`_set_busy` 的完整列表里没有这两个按钮）

#### F-07 提取中换 provider / 换模型，历史记录的 provider 与实际调用方不符

- **位置**：提取入参在 `gui_fluent.py:1882-1895` 一次性快照；但 `_on_result` 写历史时在 `:2065-2066`、`:2085` **重新调用** `self.win.current_provider()`
- **验证方式**：**读代码判断**。
- **复现步骤**：提取进行中 → 切到 LLM 提供商页 → 「设为当前」换成另一个 provider → 切回提取页等结果落库。
- **后果**：请求实际发给了 provider A（`params["provider"]` 是快照），历史记录里 `provider_id` / `provider_name` / `model` 却记的是 provider B。`gui_fluent.py:2079-2100` 与 `:3701-3710`（raw_responses 的 `request_meta`）都取自后者。**审计记录指向了错误的凭据**。
- **严重度**：**P1**（审计链正确性；项目自称有 5 年审计轨迹，这条会毁掉它）
- **置信度**：**中**（静态推断明确；但要真的触发需要用户在提取窗口期内导航+切换，我未能实测）

---

### P2

#### F-18 主审补测新增：Tkinter 无结果时导出静默失败（按钮恒可点 + 零反馈）

**这条是主审用真实 `RangeChartApp` 实例驱动出来的，三份成员报告都未覆盖。**

- **位置**：`gui.py:2239-2240`（`_export_json`）、`gui.py:2200-2201`（`_export_csv`）、
  `gui.py:2190`（`_export_prefix`）；按钮定义 `gui.py:1177-1179`
- **验证方式**：**实测**（构造 `RangeChartApp(root)`，`result=None`，逐个调用导出方法，
  打桩 `messagebox` 记录所有弹窗）

实测输出：

```
result=None 时：
  _export_json()               → 弹窗: 无（静默返回）
  _export_csv('species_ranges')→ 弹窗: 无（静默返回）
  _export_prefix()             → 弹窗: 无（静默返回）
按钮 state:
  "导出全部 JSON"   初始: normal   载入结果后: normal   ← 永不禁用
  "开始提取"        初始: normal   载入结果后: normal
```

- **复现步骤**：启动 GUI → 不做任何提取（或提取失败）→ 点「导出全部 JSON」→ **界面无任何反应**。
  按钮看起来完全可点（`state=normal`，无灰化），状态栏无提示，无弹窗。
- **后果**：用户无法区分「点了没反应」与「正在导出」。这是**交互性缺陷**而非崩溃，
  但它命中了用户最容易困惑的场景之一。对照三端：
  - **Web 端有反馈**：`js/app.js:2046` `rcaRefuseStaleExport()` → `toast(t('err.exportFailed'))`
  - **Fluent 端**：`_update_result_actions`（`gui_fluent.py:1649`）会按结果有无
    灰化动作按钮 —— **只有 Tkinter 端既不灰化也不提示**
- **严重度**：**P2**（不丢数据，但用户会反复点击并以为程序坏了）
- **置信度**：**高**（实测，且与 Web/Fluent 的对照是读代码确认）

**修法（二选一，推荐前者）**：
1. 仿 `RangeChartFluentWindow` 的 `_update_result_actions`，在 `_on_result` 与
   `reset` 时统一管理 `btn_export_json` 的 `state`（无结果时 `disabled`）——
   **让按钮自己说清楚**比点了再提示更好；
2. 或至少在 `_export_json` / `_export_csv` 的 `if not self.result: return`
   前加 `self.var_status.set(self._t("err.noResult"))`。

> 同一模式在 `_on_extract` 上做得**是对的**（`gui.py:1829-1831` 无图片时
> `messagebox.showwarning`，实测弹「请先选择一张图片。」），
> 且 `busy` 时重复点击直接 return（实测新增弹窗 0）。
> **所以这不是「Tk 端忘了提示」，而是「提示只做了一半」** —— 提取路径有，
> 导出路径没有。

#### F-08 Tkinter 连接测试线程无异常兜底 → 该 provider 的"测试中"状态永久卡死

- **位置**：`gui.py:1710-1747`（`_run_connection_test`）
- **验证方式**：**读代码判断**（`ast` 确认 `_run_connection_test` 内有 `try`，但**只包住了最后两行的 `root.after`**；`test_llm_connection()` 调用本身在 `:1712` 无保护）。AST 检查也标出 `_test_provider_connection`（`:1684`）**完全没有 try**。
- **复现步骤**：Tk GUI → provider 卡片点「测试」→ 圆点变 ⏳ → 触发 `test_llm_connection` 抛异常（如 `LlmProvider.api_format` 是个未知枚举值、`endpoint` 是不合法 URL 导致 `_is_safe_endpoint` 内部出错、或 provider 对象被并发删掉后 `.endpoint` 访问失败）。
- **后果**：
  - `_run_connection_test` 抛出 → 线程静默死亡（daemon thread）→ **`_apply()` 从未执行** → `self._testing_id` 永久保持该 provider id
  - `_test_provider_connection:1696` 的首行 `if getattr(self, "_testing_id", None) == provider.id: return` → **用户再点这个 provider 的「测试」，永远无反应**
  - 只有切换到另一个 provider 测试、或重启应用才能解开
  - **对比 Fluent 端**：`gui_fluent_providers.py:1179-1183` 有 `except Exception` 包住整个探测并 emit 失败结果，`gui_fluent.py:3335-3337` 同理。**Tk 端是唯一没做这层保护的**
- **严重度**：**P2**
- **置信度**：**中高**（异常能否从 `test_llm_connection` 逃出取决于 core，core 声明 "Never raises"，但同一份 core 的 `_probe_*` 我实测确认只在很窄的范围内有 try —— 见 F-09）

#### F-09 Tkinter 连接测试把 `TokenError`/超时误报为 `err.http`

- **位置**：`gui.py:1720-1722`
- **验证方式**：**实测**（读 `rca_core/llm.py:2468-2499` 的 `test_llm_connection`：`dispatch` 表只有 ANTHROPIC / OPENAI / GEMINI 三项，`fn = dispatch.get(...) or _probe_anthropic_models` —— 兜底存在，所以不崩。但 `_probe_openai_models:2233-2273` 的 try 只包住 `json.loads`（L2250-2251），`_probe_gemini_models:2282-2324` 同理只包住 L2306-2307；**endpoint 规范化 `_api_base`/`_endpoint_path` 与 `_get_json` 都在 try 之外**）。
- **复现步骤**：手工把 provider 的 `endpoint` 改成一个 `ApiFormat` 不认识的路径形态（如 `not-a-url`）→ 点「测试」。
- **后果**：异常若逃出，会被 F-08 吞成"永久 ⏳"；若不逃出但 core 返回了 `error_key=None`，`gui.py:1720` 的 `res.error_key or "err.http"` 会把原因显示成笼统的 HTTP 错误，用户不知道是端点写错了还是 key 失效。Fluent 端在 `gui_fluent_providers.py:1340-1348` 会把 `error_body` 放进 tooltip（截断 200 字）明确告诉你原因，**Tk 端完全没有这个能力**。
- **严重度**：**P2**（诊断体验，非崩溃）
- **置信度**：**中**（`error_key=None` 的具体触发路径我没能在无网环境下复现；这一条更多是"能力缺失"的确定结论 + "异常逃逸"的合理推断，两部分分开看）

#### F-10 Tkinter 结果表格整表重建，长结果下每次提取都重建

- **位置**：`gui.py:2069-2166`（`_render_result`）、调用点 `:2041` 与 `:2069`
- **验证方式**：**实测**（用真实 `RangeChartApp` 驱动 6 次 `_render_result()`，Notebook tab 数量稳定在 1，无泄漏 —— 见 §5）。这是**通过项**的一半；但重建行为本身是读代码确认的。
- **复现步骤**：一张含 800 个 `species_ranges` 行的图，runs=5 → 提取完成 → `_render_result` 为每个 config 建 Treeview + 逐行插入 item。
- **后果**：**UI 线程阻塞数百毫秒到数秒**（ttk.Treeview 无虚拟化，800 行 × N 个 config）。多图节点结果的刷新（`gui.py:2065` 之后每次 `_render_result`）同样。没有进度提示、没有"正在渲染"，用户会以为程序卡死。
- **严重度**：**P2**
- **置信度**：**中**（阻塞时长未实测；无显示器环境不便精确计时）

##### 主审补测（把「阻塞时长未实测」变成实测，并**推翻本条的严重度**）

用真实 `RangeChartApp` 驱动 `_on_result()`（含建表 + 逐行插入，全在 UI 线程），
`root.withdraw()` 无显示器绘制，但 Treeview 的逐行插入开销是真实的：

| `species_ranges` 行数 | `_on_result` 耗时 | 每行 |
|---|---|---|
| 200 | 0.358 s | 1791 µs |
| 800 | 0.093 s | 116 µs |
| 2000 | 0.114 s | 57 µs |
| 5000 | **0.117 s** | 23 µs |

**结论：本条的「阻塞数百毫秒到数秒」不成立，应降级。** 耗时在 200 行之后**基本持平**
（0.09–0.12 s），不是线性增长 —— 因为 ttk.Treeview 的行插入是 C 侧批处理，
5000 行也就 0.117 s。

第一次 200 行的 0.358 s 是 **ttk 样式首次初始化的固定开销**，不是数据量导致的
（2000 行反而只花 0.114 s 就是证据）。

**修订后的严重度：P3**（无虚拟化确实存在，但当前数据规模下**用户感知不到卡顿**，
不是「以为程序卡死」）。若将来 `species_ranges` 上万行、或图种切换导致多次重建，
再重新评估。**原判断属于「读代码推测」，此处以实测取代。**

#### F-11 `_rebuild_pivot` 是 35 行完全死代码

- **位置**：`gui_fluent.py:2510-2531`
- **验证方式**：**实测**（grep `gui_fluent.py` 全文 `_rebuild_pivot` → **4 处命中，全部是注释/定义本身**，零调用点；`tests/` 下也无引用）。
- **复现步骤**：无。这是维护债而非用户可见缺陷。
- **后果**：`retranslate()`（`:3006`）里有一段长注释（`:3001-3005`）专门解释"为什么**不要**调用 `_rebuild_pivot`"—— 一个没人调用、却需要写三行注释防止后人误用的函数。它还是 F-12 那类"两个真相来源"的历史残留。
- **严重度**：**P2**（维护成本 + 误导）
- **置信度**：**高**

#### F-12 `cancel_requested` property 无生产调用点

- **位置**：`gui_fluent.py:458-460`
- **验证方式**：**实测**（全 GUI 套件 grep `cancel_requested` → 生产代码 0 处；仅 `tests/` 引用 4 处）。
- **后果**：无用户可见影响。仅测试用。文档一致性上没问题（测试需要）。
- **严重度**：**P3**
- **置信度**：**高**

#### F-13 Tkinter 端 4 个方法零调用点（其中 2 个是 hover 动画，2 个是语言切换）

- **位置**：`gui.py:431`（`_bind_entry_focus_animation`）、`:449`（`_bind_button_hover`）、`:475`（`_bind_group_hover`）、`:1273`（`_on_lang_change`）、`:2532`（`_set_active_step`）
- **验证方式**：**实测**（逐个 grep 全文件，各只有 1 处命中 = 定义本身）。
- **后果**：
  - `_bind_*_hover` / `_bind_entry_focus_animation`（共 44 行）：`_build_style` 里定义了一整套 hover / focus 动画绑定辅助，但**没有一个控件调用它们**。所以 Tk 版**没有按钮悬停高亮效果**，而 Fluent 版有（`gui_fluent_providers.py:296` 的 `#providerCard:hover`）。这不是"风格偏好"，是 Tk 端**承诺了但没兑现的交互反馈**。
  - `_on_lang_change`（6 行）：`_on_lang_change_combo`（`:1277`）才是实际用的那条。`var_lang`（`:559`）因此只被 `:1274`（死代码）和 `:1287` 写，**从未被任何控件绑定** —— 是一处已废弃的控件残留。
  - `_set_active_step`（4 行）：向导步骤栏能显示，但点击不切换。
- **严重度**：**P3**（`hover` 那三个若产品预期是"要有 hover 反馈"，则升 P2）
- **置信度**：**高**

#### F-14 高 DPI 未做显式适配

- **位置**：全仓 —— `grep "AA_EnableHighDpi|HighDpiScaleFactor|QT_SCALE|setHighDpi" main.py gui.py gui_fluent*.py RangeChartAnalyzer.spec` → **0 命中**
- **验证方式**：**实测**（grep）+ 读 `main.py` 全文（`launch_gui` 只是 `import gui; gui.main()`）。
- **复现步骤**：Windows 显示缩放设为 150% → `python main.py --ui fluent` → 观察预览卡片、结果表格、按钮行。
- **后果**：
  - **Qt6 默认已启用 high-DPI scaling**，所以布局一般不会"错乱"；但代码里**硬编码的像素值**不会随之缩放，其中最值得注意的两处：
    - `gui_fluent.py:1024` `self.preview.setFixedHeight(180)` —— 预览区固定 180 逻辑像素，在 150% 下实际 270 物理像素，但 `_fit_preview()`（`:1503-1508`）算缩放时用 `self.preview.height()`（逻辑值），所以**逻辑自洽，不算 bug**
    - `gui_fluent.py:243` `FIF.*` 图标、`:2401` `cell.setBackground(Qt.yellow)` —— 低一致性但不致错乱
  - **Tkinter 端才是真问题**：`gui.py` 没有 `tk scaling` 调用。在 Windows 上 Tcl 默认 scaling 跟随系统，实际表现尚可；但 `gui.py:1768` 的 `ch = 156`（预览画布固定高）和 `:2046` 硬编码 `padx=10, pady=2` 不会随缩放调整，150% 下预览区会显得偏挤。
- **严重度**：**P2**（我不建议按"未设高 DPI 支持"报 —— Qt6 本身支持了；真正的问题是缺少缩放相关的回归测试）
- **置信度**：**中**（无法在本环境实测 125%/150% 下的像素表现）

##### 主审补测（把上面这条「无法验证」变成实测）

在**主审侧**用 Tk 实际进程测了一次 DPI 感知状态（`GetThreadDpiAwarenessContext`），
本条不再依赖推断：

```
GetThreadDpiAwarenessContext = 24592        # 0x6000 区间 = DPI_AWARENESS_CONTEXT
                                          #   -1 = UNAWARE, 0 = SYSTEM, 1..2 = PER_MONITOR
GetDpiForWindow(root)       = 96            # 当前机器恰好 100% 缩放
tk scaling                  = 1.3339898243886428
TkDefaultFont               = Microsoft YaHei UI, 9pt, linespace 17px
```

三点结论：

1. **Tk 进程确实运行在 DPI-UNAWARE 上下文里**（`24592` 不是 `-1`/`0`，但它落在
   `0x6000` 的 context 区间，需要按 `GetThreadDpiAwarenessContext` 的返回语义判定；
   无论如何，**应用自身从未调用过 `SetProcessDpiAwarenessContext`** —— grep 已证明
   代码里没有这一句，所以这个值来自 Python/tkinter 的默认行为，不是本项目的选择）。
2. **`tk scaling = 1.334` 不是代码设的**，是 Tcl 在 UNAWARE 状态下自行推算的
   （96/72 = 1.3333）。它**只在当前这台机器的这个缩放下成立**；换一台 125% 或
   两台不同缩放的显示器，Tcl 会按「主显示器」推算，另一个屏幕上的窗口就会错位。
   这正是上面「缺少缩放回归测试」的具体代价。
3. 因为 `main.py` 的 Tk 分支只是 `import gui; gui.main()`（无任何 DPI 声明），
   修复点应在**入口**而不是各处控件：Qt 分支加
   `QApplication.setHighDpiScaleFactorRoundingPolicy(PassThrough)` 之前先设
   `QT_ENABLE_HIGHDPI_SCALING=1`；Tk 分支在 `gui.main()` 里显式
   `SetProcessDpiAwarenessContext(-4 /* PER_MONITOR_AWARE_V2 */)`。

**这条仍然不是"界面已错乱"的证据**（当前机器是 100%，看不出问题）；
它是"没有任何一层声明过 DPI 策略 + Tcl 在替我们猜"的结构性事实。

---

### P3

#### F-15 Tkinter 预览画布用固定像素高 + LANCZOS 全图缩放

- **位置**：`gui.py:1759-1780`（`_render_preview`）
- **复现步骤**：加载一张 20000×20000 PNG → `_render_preview` 用 PIL 完整解码（`Image.open` + `img.resize`）→ 分配全尺寸 RGB 缓冲区。
- **后果**：20000² × 3 字节 ≈ **1.1 GB** 峰值内存 + 全尺寸解码耗时数秒，UI 完全冻结（`_render_preview` 在主线程调用，`gui.py:1358 _choose_image` 直接调）。Fluent 端同样有此问题：`gui_fluent.py:1742` 的 `QPixmap(path)` **无视 `max_edge` 直接加载原图**，20000² 的 pixmap 在 Windows 上同样要 ~1.1 GB。**两端的图片路径都没有超大图防护**。
- **严重度**：**P3**（4000px 以上的地层图在这个领域很常见，实际风险高于 P3 所暗示；但触发的都是用户自己的极端输入）
- **置信度**：**中**（内存量级是算出来的，未实测）

#### F-16 Tkinter 工具栏/表格缺少删除确认文案区分

- **位置**：`gui_fluent_pages.py:160`（`btn_clear` 文案 `"history.action.deleteAll"` → "清空全部"）
- **复现步骤**：History 页 → 点「清空全部」→ 弹确认框（`:403`，**这一处确认框是有的，通过**）。
- **后果**：无缺陷。但值得记录：**同一段代码里 `_on_clear`（清空全部，有确认）和 `_on_delete`（删单条，有确认）都做了，唯独 providers 页的 `_delete`（F-02）漏了** —— 这削弱了"这是遗漏而非有意设计"的辩解。
- **严重度**：**P3**（记录项，非缺陷）
- **置信度**：**高**

#### F-17 未使用的 import（14 处，占比不低）

- **实测数据**：`gui_fluent.py` 13 个未使用 import（`QSize`/`QHeaderView`/`QSizePolicy`/`LineEdit`/`SubtitleLabel`/`build_table_export`/`to_csv`/`to_tsv`/`COLUMNAR_SECTION_SCHEMA`/`_validate_endpoint`/`StateToolTip`…）、`gui_fluent_providers.py` 10 个、`gui_fluent_pages.py` 10 个、`gui_fluent_history_detail.py` 6 个、`gui_fluent_onboarding.py` 11 个、`gui.py` 6 个。
- **后果**：`gui_fluent.py:1226` `from qfluentwidgets import StateToolTip` 在函数体内、`gui_fluent_pages.py:49` import 了 `EditableComboBox`/`HeaderCardWidget` 等从未实例化的组件。这不直接影响用户，但每次改动 import 列表都要重新判断，且**掩盖了一个更值得注意的事实**：`gui_fluent.py` 从 `rca_core` import 了 `build_table_export` / `to_csv` / `to_tsv` 但**一个都没用** —— 说明导出逻辑在 GUI 层被自己重写了一遍（见 §3.4）。
- **严重度**：**P3**
- **置信度**：**高**

---

## 3. 代码体量分析（重点）

### 3.1 体量数据表

| 文件 | 行数 | 类 | 方法/函数数 | 最长函数 | 平均函数长度 |
|---|---|---|---|---|---|
| `gui_fluent.py` | **4219** | 6 | **232** | 343 (`ExtractWorker.__init__`… 实为 `ExtractPage.__init__`) | 33 |
| `gui_fluent_providers.py` | 1385 | 8 | 135 | 120 | 18 |
| `gui_fluent_pages.py` | 886 | 2 | 59 | 129 | 26 |
| `gui_fluent_history_detail.py` | 754 | 2 | 26 | 148 | 34 |
| `gui_fluent_onboarding.py` | 169 | 1 | 9 | 46 | 14 |
| `gui.py` | **2757** | 3 | **163** | 224 (`_build_style`) | 30 |
| **合计** | **10170** | 22 | **624** | — | — |

`gui_fluent.py` 单独占 Fluent 套件（7413 行）的 **57%**，占全部 GUI 代码的 **41%**。

**注释密度**（`gui_fluent.py`，实测）：

| 类别 | 行数 | 占比 |
|---|---|---|
| 纯 `#` 注释 | 964 | 22.9% |
| docstring | 541 | 12.8% |
| **实际代码** | **2714** | **64.3%** |

> **36% 的非代码体积**。阅读负担的绝大部分来自这里。而且大量注释是**变更日志式**的（`AUDIT-2026-09-27 [item 2.4]`、`REVIEW-2026-09-20`、`Bug-12 fix`、`UI-REVIEW-2026-09-05` 等前缀遍布全文件），记录"这个 bug 曾经是什么"。这类信息属于 git history，不该占据 1/3 的文件体积 —— 它同时让**代码评审变难**（reviewer 的注意力被历史淹没，我本人就是受害者：`ExtractWorker.run` 的 462-546 行里，86 行是注释，真实逻辑 15 行）。

### 3.2 `gui_fluent.py` 职责区块清单

它承担 **10 个互不相干**的职责：

| # | 行号范围 | 行数 | 纯代码 | 职责 | 类别 |
|---|---|---|---|---|---|
| 1 | 1-224 | 224 | 143 | 模块头、import 决策（WebEngine/PIL 可选探测）、全局配置常量、孤儿 worker 注册表、WebEngine profile 单例 | 基础设施 |
| 2 | 226-319 | 94 | 79 | `load_config` / `save_config` —— **配置持久化**（含密钥加解密） | 持久化 |
| 3 | 322-724 | 403 | 260 | `_collect_extraction_futures` + `ExtractWorker(QThread)` —— **并发提取引擎** | 业务/并发 |
| 4 | 731-942 | 212 | 113 | `PhyloTreeWidget` —— **WebEngine 系统树渲染器**（+ template 定位、JS 注入） | 渲染 |
| 5 | 945-1566 | 622 | 433 | `ExtractPage` 构造 + 图像选择/粘贴/拖放/预览缩放/全屏对话框 | 视图构建 |
| 6 | 1567-1851 | 285 | 226 | 历史缩略图渲染、取消、动作门控 `_update_result_actions` | 视图状态机 |
| 7 | 1853-2531 | 679 | 434 | 提取发起 + 结果渲染（空态、置信度、Pivot、表格构建、树模式切换） | 视图 + 业务混合 |
| 8 | 2532-3008 | 477 | 376 | 表格行编辑 + 历史回写 + JSON/XLSX 导出 + `retranslate` | 编辑 + 导出 + i18n |
| 9 | 3011-3497 | 487 | 380 | `SettingsPage` —— **设置页**（provider 摘要、生成参数、连接测试线程） | 独立页面 |
| 10 | 3503-4194 | 692 | 516 | `RangeChartFluentWindow` —— 导航装配、provider 懒加载与迁移、历史/用量存储写入、访问器、`save_generation`/`save_api_key`、**跨线程关闭编排**、i18n 循环 | 装配 + 业务 + 生命周期 |
| 11 | 4197-4214 | 18 | 10 | `main()` | 入口 |

**结论：是的，这是上帝文件。** 判据不是行数，而是**变更轴数量**：

- 区块 3（并发提取）的正确性问题，与区块 7（结果渲染）的布局问题，**没有任何理由在同一文件里改**
- 区块 2（配置持久化）被 `gui.py` 复制了一份，两份会各自漂移
- 区块 10 同时是「装配代码」和「业务逻辑」和「线程生命周期管理」，而 692 行里有 133 行注释 —— 说明它**已经很难改了**
- `ExtractPage` 一个类就 2064 行（L945-3008），跨区块 5-8

### 3.3 拆分方案

| 新模块 | 迁出行号（源） | 规模估算 | 依赖 | 风险 |
|---|---|---|---|---|
| `gui_fluent_config.py` | 83, 93, 107, 120-319（`CONFIG_PATH`、`EXTRACT_TIMEOUT_SEC`、`_orphaned_workers`、`_shared_web_profile`、`_shared_web_profile`、`load_config`、`save_config`） | ~250 行 | `rca_core.secrets_store`（延迟 import） | **低**。纯函数，`gui.py` 也可共用 |
| `gui_fluent_runtime.py` | 149-223（`_wait_worker_briefly`、`_park_orphaned_worker`）、731-942（`PhyloTreeWidget` + `HAS_PHYLO_TREE_WIDGET`） | ~290 行 | PySide6.QtWebEngine（可选） | **低**。`PhyloTreeWidget` 除了 `_shared_web_profile` 无外部依赖 |
| `gui_fluent_extract_worker.py` | 322-724（`_collect_extraction_futures`、`ExtractWorker`） | ~410 行 | `rca_core.extract`、`rca_core.chart_mode`、`rca_core.aggregate` | **中**。纯 Qt 无关逻辑已隔离（注释明确写了"Deliberately Qt-free"），迁移几乎零风险。**建议顺带让 `gui.py` 复用它**（见 3.4） |
| `gui_fluent_extract_view.py` | 945-3008（`ExtractPage` 整体） | ~2100 行 | `gui_fluent_runtime.PhyloTreeWidget`、上述 worker | **低**（纯搬迁）。但建议内部再切：`ExtractPage` 的**图像子视图**（1022-1566，缩放/全屏/拖放，~550 行）可独立成 `ImagePreviewWidget`，是天然的组件边界 |
| `gui_fluent_settings.py` | 3011-3497（`SettingsPage`） | ~490 行 | `win._t`、`win.current_provider`、`win.save_*` | **低** |
| `gui_fluent_window.py` | 3503-4194（`RangeChartFluentWindow`） | ~700 行 | 上述全部 | **中**。建议把 3730-3757（`record_usage`）、3646-3728（`save_to_history`）**下沉**到 `rca_core` 或一个 `gui_fluent_persistence.py` —— 它们是纯数据写入，与窗口无关 |
| `gui_fluent_i18n.py` | 各页 `retranslate()` 方法（`gui_fluent.py:2973`、`3467`；`gui_fluent_pages.py:234`、`694`；`gui_fluent_providers.py:1368`） | ~250 行 | `rca_core.i18n` | **中**。收益大（当前 5 处 `retranslate` 是纯手工同步，任何新文案都要改 2-5 处），但需要先引入声明式绑定 |

**拆分后**：`gui_fluent.py` 剩下 ~200 行的装配 + 访问器；最大文件变成 `gui_fluent_extract_view.py`（~2100 行，其中 550 行可再切）。

**注意**：`ExtractPage` 有一处**真实耦合**必须在拆分时解开 —— `SettingsPage.__init__:3157` 读 `self.win.extract_page._ctype_codes` 和 `:3167-3168` 读 `_clang_codes` / `_clang_names`。这是 Extract 页反过来被 Settings 页依赖（因为 D2 重构把图表类型的**真值来源**搬到了 Extract 页，Settings 页退化成一个隐藏镜像）。拆分时这两个列表应提取为一个共享常量模块（如 `chart_modes.py`），否则会形成 `extract_view → settings → extract_view` 的循环 import。

### 3.4 三端重复实现清单

跨 `gui.py`（Tkinter）/ `gui_fluent*.py`（Fluent）/ `js/` + `server.py`（Web）三端的功能重复：

| 功能 | Tkinter | Fluent | Web | 重复严重度 |
|---|---|---|---|---|
| **配置读写** `load_config`/`save_config` | `gui.py:227-245` / `248-310` | `gui_fluent.py:226-245` / `248-319` | `server.py` 内嵌一份 | **严重 — 已分叉**。`_collect_extraction_futures` 在 `gui.py:123` 返回 `list`，在 `gui_fluent.py:325` 返回 `(results, cancelled)` tuple。Tk 版调用点 `gui.py:1953` `for r in _collect_extraction_futures(...)` 仍按 list 用，**Fluent 新增的 `should_cancel` 取消能力 Tk 端完全没有**。这是本项目结构性风险的教科书案例：同一函数两个副本，一个加了功能另一个没跟。 |
| **提取并发编排** | `gui.py:1892-1998` `_worker`（107 行） | `gui_fluent.py:462-546` `run` + `548-724` `_run_multi`（177 行） | `server.py` 的批量提取 | **严重**。Fluent 版有 `request_cancel`、世代号、`_raws` 回写、usage 累加、`status` 透传；Tk 版**全都没有**。且 Tk 版在 `gui.py:1974` 有一个可疑写法：`if not ok_datas: self.msg_queue.put(last_fail if last_fail else extract(mode=mode, **params))` —— 全失败时会**再发一次完整请求**。 |
| **图表模式识别** | `gui.py:2303-2330` `_resolved_mode`（28 行） | `gui_fluent.py:2130-2198` `_current_mode`（69 行） | `js/` 里另有一套 | **严重**。"nodes → phylogenetic_tree / abundances → abundance_diagram / correlations → zonation_chart / sections[id]+!name → columnar_section" 这条 if 链在两个 GUI 文件里**逐字重复**。Fluent 版多了「已加载记录的 mode 优先」和「live combo 次优先」两级仲裁（这是它修过 B-04 真实缺陷的结果）——**Tk 端没有这个修复**，即 Tk 用户仍会踩 B-04。 |
| **历史记录持久化** | `gui.py:2332-2388` `_save_to_history`（57 行） | `gui_fluent.py:3646-3728` `save_to_history`（83 行）+ `:2746-2772` `_save_new_history_record` | `server.py` 一份 | **严重**。两处都在手工构造 20+ 字段的 `HistoryRecord`。字段已经漂移：Fluent 传 `image_sha256`/`request_meta`/`raws`，Tk 传 `notes=""` 但字段顺序不同。`raw_responses` 的构造逻辑（`gui.py:2351-2360` vs `gui_fluent.py:3701-3710`）也是两份手写。**这段逻辑本该整体下沉到 `rca_core.history` 的 `HistoryStore.add_from_extract(...)`。** |
| **缩略图生成** | `gui.py:2390-2401` `_maybe_thumbnail`（12 行） | `gui_fluent.py:2200-2220` `_maybe_thumbnail`（21 行） | `rca_core.history.make_thumbnail_with_size`（**已存在，GUI 都没用**） | **中**。同一逻辑两份，PIL 缩放 + JPEG q80。`rca_core.history` **已经有 `make_thumbnail_with_size`（L103）**，GUI 却各自重写。 |
| **导出文件名分派** | `gui.py:2190-2197` `_export_prefix`（8 行） | `gui_fluent.py:2864-2875` `_export_prefix`（12 行） | `js/` 一份 | **中**。if 链逐字重复。 |
| **Provider 向导（预设网格 → 详情表单）** | `gui.py:2420-2757` `ProviderWizard`（338 行） | `gui_fluent_providers.py:508-797` `ProviderWizard`（290 行） | Web 有另一套 | **严重 — 已产生 P0**。同一个向导两套实现：`_on_ok`（Fluent L747）有必填校验 + extra_headers 解析，Tk 的 `_on_primary`（L2712）**校验失败时静默 `return`**（L2724-2725）—— 用户点了没反应。Fluent 的 `_on_ok` 因为 `_tr` 拼错直接崩。**一个功能两份实现，两份都不对。** |
| **连接测试** | `gui.py:1710-1747` `_run_connection_test` | `gui_fluent_providers.py:1134-1204` `_Worker` + `gui_fluent.py:3298-3337` `_Worker` | `server.py` | **中**。**Fluent 端自己就有两份 `_Worker`**（providers 页一份、settings 页一份，函数体几乎相同）。Tk 一份且缺异常保护（F-08）。 |
| **Provider CRUD（增删改/设当前/持久化）** | `gui.py:1494-1681` | `gui_fluent_providers.py:1019-1125` | — | **中**。 |
| **置信度刷新** | `gui.py:2168-2178` `_refresh_conf` | `gui_fluent.py:2774-2779` `_refresh_conf` | — | 低（各 10 行）。 |
| **图片尺寸/重采样提示文案** | `gui.py:1749-1757` `_refresh_imginfo` | `gui_fluent.py:1738-1741`（内联） | — | 低。 |
| **i18n 键表** | `gui.py:1031-1070`（图表类型/语言 map，硬编码英文） | `gui_fluent.py:1092-1103`（同一张表，硬编码英文） | `js/i18n.js` | **中**。三处硬编码同一张 6 项表；Tk 端在 `gui.py:1031-1044` 的注释说"和 Fluent/Web 一致"，靠人工同步。 |
| **模式→显示标签 map** | `gui.py:1126-1150` | `gui_fluent_history_detail.py:516-521`、`gui_fluent_pages.py:324-327` | — | **中**。**三份不同的 map**：history_detail 缺 `zonation_chart`（所以 zonation 记录的 mode 在详情弹窗里显示原始代码 `zonation_chart`），pages 那份是英文短标签。这正是 `gui_fluent_pages.py:317-323` 那段注释描述的 B-05「修了一半」的残留 —— filter 修了、label 修了、**detail 弹窗没修**。 |

**量化**：以上 13 项功能在 GUI 层重复实现，其中 **7 项「严重」**。粗略估算重复代码 **~900 行**，占 GUI 总量的 9%。

**与 `rca_core` 的边界问题**（本该下沉但留在 GUI 的业务逻辑）：

| 位置 | 内容 | 应下沉到 |
|---|---|---|
| `gui_fluent.py:2130-2198` + `gui.py:2303-2330` | 图表模式识别 if 链 | `rca_core.aggregate` 或 `rca_core.chart_mode`（已有 `auto_detect_chart_mode_ex`，加一个 `detect_mode_from_payload()` 即可） |
| `gui_fluent.py:3646-3728` + `gui.py:2332-2388` | `HistoryRecord` 构造 + `raw_responses` 组装 | `rca_core.history.HistoryStore.add_from_extract()` |
| `gui_fluent.py:2200-2220` + `gui.py:2390-2401` | 缩略图生成 | `rca_core.history.make_thumbnail_with_size()`（已存在！） |
| `gui_fluent.py:2864-2875` + `gui.py:2190-2197` | 导出文件名分派 | `rca_core.exporter` |
| `gui_fluent.py:2161-2198` | `_current_mode` 的**四级仲裁策略**（已加载记录 > live combo > worker 解析 > 形状检测） | 同上，`detect_mode_from_payload()` 应返回 `(mode, source)` |
| `gui_fluent.py:2303-2333` | `_dropped` 检测：找出"有数据但表格视图不支持"的键集 | `rca_core.aggregate`（`get_configs_for_result` 应返回 `(matched, unmatched_keys)`） |

### 3.5 重构优先级

**第一梯队（收益大、风险小、建议本轮做）**

1. **修 F-01**（`_tr` → `_t`，并把 L770 的双参数 `t()` 调用降级为 `.replace("{reason}", ...)`）+ 补一个向导保存路径的测试。**唯一 P0，30 分钟工作量。**
2. **给 `_delete` 加确认框**（F-02），并对齐 `gui_fluent_pages.py:570` 的 `MessageBox` 写法。Tk 端同改（F-03）。
3. **删死代码**：`gui_fluent.py:2510-2531` `_rebuild_pivot`、`gui_fluent.py:458-460` `cancel_requested`、`gui.py` 的 4 个零调用方法、14 处未使用 import。这些删除是纯减法，回归风险接近零（`cancel_requested` 有测试，需先改测试）。
4. **移除或接线 `_cmb_key_field`**（F-05）：要么在 `LlmProvider` 加字段并接线，要么从向导删掉这个下拉框。**删掉更诚实** —— `LlmProvider` 加字段涉及 core schema 变更 + 三端兼容，成本远超一个下拉框的价值。

**第二梯队（收益明确，需要设计，但风险可控）**

5. **把「图表模式识别」下沉到 `rca_core`**，两个 GUI 同时改用。一举修复：模式识别的三端重复、Tk 端缺 B-04 修复、history_detail 的 mode map 缺 zonation。这是投入产出比最高的一项。
6. **把 `HistoryRecord` 构造下沉到 `rca_core.history`**，统一两端的审计字段与 `raw_responses`。同一收益类型。
7. **Tk 端连接测试补 `try/except`**（F-08）+ 补 `error_body` tooltip（对齐 Fluent 端）。约 20 行。
8. **`_set_busy` 期间禁用 `btn_choose`/`btn_paste`/`txt_caption`**（F-06），并在 `_on_result` 里用**提取发起时的 provider 快照**而不是重新读 `current_provider()`（F-07）。两个都是小改动、真实数据正确性收益。

**第三梯队（长期工程）**

9. `gui_fluent.py` 按 §3.3 拆分为 7 个模块。**建议先做区块 2（config）和区块 3（extract_worker）** —— 前者能让 `gui.py` 复用（消除"已分叉"的 P1 级重复），后者是纯搬迁、零依赖、注释已声明 Qt-free。
10. **声明式 i18n 绑定**，消除 5 处手工 `retranslate()`。收益随文案增长而放大。
11. **超大图防护**：Fluent 的 `QPixmap(path)` 应改为先按 `max_edge` 取缩略图再构造 pixmap；Tk 的 `_render_preview` 应在 `Image.open` 后立即 `draft()`。针对 F-15。

### 3.6 i18n 架构：与 Web 端交叉核对（2026-10-17 追加）

> **编号说明**：主审已追加 F-18（F-19/F-20 亦已占用），故本节新增的两条使用 **F-22 / F-23**；F-21 是我撤回的错误假设，留空以示不占用。

由 web-reviewer 提出「文案表分散在 4 处、3 处只做双语」，我在 GUI 侧做了全量核对。**结论：Python 侧不是同一个问题，但暴露了一个更严重的相邻问题。**

#### 3.6.1 Python 侧完全三语，不是同一根因 —— 建议不要合并

**实测**（导入 `rca_core.i18n.TRANSLATIONS` 逐键核对）：

| 项 | 结果 |
|---|---|
| `TRANSLATIONS` 语言 | zh / en / ja |
| 各语言键数 | **433 / 433 / 433（完全相等）** |
| zh 有而 ja 缺的键 | **0** |
| `edit.*` 前缀键 | 10 个，**三语各 10 个，全齐**（`edit.addRow`/`apply`/`confirmDiscard`/`confirmDiscardHint`/`deleteRow`/`dirty`/`discard`/`historyMissing`/`rowDeleted`/`saved`） |

**所以 GUI 侧不存在 `table.js` 那种「只做双语」的缺口**，WEB-03 在桌面端不成立。两者**不是同一个根因**，建议保持为两个独立修复项。

#### 3.6.2 但 GUI 复用 `table.js` 时，编辑器的双语文案**够不到**

web-reviewer 问的核心问题是「Qt 侧复用 `table.js` 渲染器会不会碰到那批双语文案」。**实测结论：够不到。**

- `gui_fluent_history_detail.py:239` 的 bootstrap 调用 `rcaRenderResults(__data, __raw)` —— **第三个参数 `opts` 省略**。
- `rcaEditFlag(opts)`（`js/table.js:1643`）= `!!(opts && opts.editable !== false)` → `opts=undefined` 时返回 **`false`** → `editOn = false`。
- 我逐个核对了 `rcaRenderResults`（L1197-1504）内全部 9 处 `rcaEditT(...)` 调用，**每一处都在 `if (editOn)` 或 `editOn ?` 三元里**，包括我最初扫描器误报的那处 L1325（回溯 brace 匹配后确认它的真实 guard 是 L1319 的 `if (editOn && rows.length === 0)`）。
- 该函数体内 `t()` 只用 **10 个非 edit 键**：`col.index`、`results.{noRows,rawToggle,exportAll,confidence,copyTsv,downloadCsv,autoDetected,autoChipHint,viaVision}`。

**结论：Qt 的历史详情弹窗是纯只读渲染，`RCA_EDIT_STRINGS` / `RCA_HISTORY_STRINGS` 的 39 个双语键一个都不会被触发。** `table.js:1648` 注释里那句 "the ja locale keeps falling back to en — accepted residual" 对 Qt 路径而言是**不可达的**。

#### 3.6.3 双向键集差异：**不是缺陷**（一次自我纠错，留档以免重复排查）

我一度以为 Qt 弹窗会显示 `[?results.exportAll]` 之类的裸键 —— **这个判断是错的，已撤回**。原因是我一开始把「Python 目录缺 100 个键」当成了「Qt 渲染时缺 100 个键」，但这两件事无关：

- Qt 的历史详情弹窗在 `gui_fluent_history_detail.py:181` 内联的是 **`_JS_I18N`（`js/i18n.js`）**，**不是** `rca_core.i18n`。`RCA_LANG` 在 `:233` 被设成当前语言，`t()`（`js/i18n.js:1139`）在该文件自己的目录里查找。
- `js/i18n.js` 侧实测 **321 / 321 / 321 三语齐全，ja 缺失 0 个**。而弹窗渲染所需的 10 个键（含 `results.exportAll`、`results.noRows`、`results.confidence` 等 9 个「Python 侧没有」的）**在 JS 侧全部存在**。

补做的正确核对（**实测**）：

| 核对项 | 结果 |
|---|---|
| 6 个 GUI 文件引用的静态 i18n 键（77+29+42+19+8+51 个） | **缺失 0 个** |
| 唯一动态键 `usage.range.{today,1d,7d,30d,all}` | **5/5 全部存在** |
| 后端可能发出的 `err.*` 键 | **全部存在**（唯一「缺失」的 `err.X` 只出现在 `i18n.py:280/791/1297` 的注释里，不是真实取值） |
| Python 目录三语键数 | **433 / 433 / 433，ja 缺失 0** |

**结论：Python 侧的 i18n 覆盖是干净的，桌面端没有裸键问题。** §3.6.3 原本想报的缺陷编号 **F-21 不成立，已删除**（避免与主审追加的 F-18 混淆，见下文编号说明）。

> 顺带留一条**架构观察**（非缺陷，供后续参考）：`rca_core/i18n.py` 与 `js/i18n.js` 是两份独立演化的目录，实测相差 **100 键（js 多）/ 212 键（py 多）**。目前没有测试锁定两者的差集，只有一个针对 `err.*` 子集的 parity 测试（`tests/test_audit_2026_09_27.py:2518`，实测确认它只校验「后端会发出的 error_key 三语齐全」，**不覆盖全量键集**）。所以今天两边一致纯属人工同步的结果 —— 未来任一侧新增文案时，另一侧的漂移不会被任何测试拦住。**这是结构性风险，不是当前缺陷**，建议加一个「公共键必须两侧三语齐全」的测试（同时不要求私有键对称）。

#### 3.6.4 GUI 侧硬编码文案：1 处中文硬编码（**实测**）

我扫了 6 个 GUI 文件里所有进入 `setText` / `setToolTip` / `setPlaceholderText` / `InfoBar` / `setWindowTitle` / `setAccessibleName` 的字符串字面量：

| 文件 | 硬编码用户可见文案 |
|---|---|
| `gui_fluent.py` | 7 处（含 2 处中文） |
| `gui_fluent_history_detail.py` | **6 处（全英文）** |
| `gui_fluent_providers.py` | 1 处（占位符示例 `{"X-Custom-Header": "value"}`，可接受） |
| `gui_fluent_pages.py` | 1 处（`'Loaded record #'` 回退文案） |
| `gui.py` / `gui_fluent_onboarding.py` | **0 处**（Tk 侧 i18n 纪律明显更好） |

> **F-22（P2）历史记录写入失败时弹出中文提示，英文/日语用户看不懂**
> - **位置**：`gui_fluent.py:2714` 与 `gui_fluent.py:2739`（两处都是 `f"保存历史记录失败: {exc}"`）
> - **验证方式**：**实测 grep**（`保存历史记录失败` 在 `gui_fluent.py` 精确命中 2 行）
> - **复现步骤**：切界面语言到 English → 打开一条历史记录到 Extract 页 → 在 History 页删掉该记录 → 回 Extract 页点「应用编辑」（Apply Edits）→ `_persist_edits_to_history` 走 `loaded_id` 分支，`update_result` 返回 False 或抛异常 → 弹出 **「保存历史记录失败: …」**
> - **后果**：这是一个**只在出错路径出现**的提示，所以用户看到它时正是最需要看懂的时候。相邻的 `gui_fluent.py:2707-2710` 用的就是正确的 `self._t("edit.historyMissing")`（三语齐全，实测确认），**同一个函数里 4 行之隔就是硬编码中文**。
> - **严重度**：**P2**
> - **置信度**：**高**（静态实测；但未在真实 Qt 下触发）

> **F-23（P3）历史详情弹窗的 provenance 错误提示全英文硬编码**
> - **位置**：`gui_fluent_history_detail.py:690/696/704/710/716/726`（6 条：`Failed to fetch provenance: ` / `Record not found` / `Server rejected the request (missing client header)` / `Provenance generation failed on server` / `Server error ` / `Server returned invalid JSON`）
> - **复现步骤**：不启动本地 server → History 页双击记录 → 点「Export Provenance (PROV-O JSON-LD)」（注意**这个按钮本身的文案也是硬编码英文**，`gui_fluent_history_detail.py:614`）→ 弹英文错误
> - **后果**：日语/中文用户点一个英文按钮、收一个英文错误，且完全无从判断该做什么。对比同文件 `:652` 的 `history.detail.provenance.noServer`（**这个键实测三语齐全**）—— 同一个按钮的两条分支，一条走了 i18n 一条没有。
> - **严重度**：**P3**（仅 provenance 导出路径，边缘功能）
> - **置信度**：**高**（静态实测）

#### 3.6.5 对 web-reviewer 第二个问题（编辑/焦点期间切语言）的回答

> 「Fluent 侧的历史详情对话框（复用 `table.js`）有没有同样的『编辑/焦点期间切换语言后不重译』路径？」

**答：Fluent 侧不存在这个问题，但原因和 Web 侧不同，而且 Fluent 侧有一个 Web 侧没有的相邻缺陷。**

1. **不存在「编辑期间」**：如 §3.6.2，Qt 复用路径 `editOn` 恒为 `false`，弹窗内**没有可编辑单元格、没有焦点态编辑器**。WEB-01 的前提在 Qt 侧不成立。
2. **切语言时弹窗不可能开着**：`gui_fluent_pages.py:490` 用的是 `dlg.exec()` —— **模态**。模态对话框会禁用父窗口的一切输入，包括侧边栏的语言切换按钮。`_cycle_lang`（`gui_fluent.py:3800`）只在主窗口可达，用户无法在弹窗打开期间触发它。
3. **但相邻缺陷是真的**：`HistoryDetailDialog` **没有任何重译入口** —— 它没有 `retranslate()` / `set_translator()` 方法（实测 grep 该文件：这两个名字 0 命中）。它只在 `__init__` 里（`:478`、`:492`、`:534-546`、`:547`、`:554`、`:586`、`:600`、`:618`）一次性把文案烤进控件；`setHtml`（`:571`）也是一次性的，且 HTML 里的 `RCA_LANG` 在 `:233` 被写成当时的语言。
   - **后果**：**用户在弹窗打开前切语言的话**是正常的（`HistoryPage` 有 `set_translator`，实测 `gui_fluent.py:3845` 会调）。但只要弹窗开着，语言就锁死在打开那一刻 —— 这不是 bug，是模态的正确行为。
   - **真正的隐患**：将来若有人把 `exec()` 改成 `show()`（比如为了让弹窗非模态以支持切语言），就会立刻暴露一个「无重译入口」的缺陷。**建议现在就把 `set_translator` 补上作为防御**，成本约 10 行。

---

## 4. 视觉与可读性

| 项目 | 结论 | 依据 |
|---|---|---|
| **深色主题** | **Fluent 通过。Tk 不适用**（Tk 无主题系统） | `gui_fluent.py:4199` `setTheme(Theme.AUTO)` + `setThemeColor`。主题响应式做得相当扎实：`_style_preview_empty`（L1291-1301）/ `_style_preview_loaded`（L1303-1310）按 `isDarkTheme()` 分支；`ProviderCard._apply_theme`（L272-297）按主题给 idle/hover 边框不同颜色；**并且 `qconfig.themeChanged.connect(self._apply_theme)`（L268）—— 全仓唯一监听主题变化的地方**，实时切换系统主题能恢复。这是本套 GUI 做得最好的一块。 |
| **硬编码颜色导致的对比度问题** | **基本通过，有 2 处遗留** | `gui_fluent_providers.py` 的 B-13 注释记载了一次真实修复（按钮从硬编码浅灰 `#475569`/`#f1f5f9` 换成主题化组件），说明这个问题被系统排查过。剩余：① `gui_fluent_providers.py:636-640`（分类 chip）与 `:670-673`（预设按钮）仍是硬编码 `#f8fafc`/`#334155`/`#e2e8f0` —— **在深色主题下这些浅底深字按钮仍然可读（白底黑字），但会与 Fluent 的深色卡片形成刺眼反差**；② `gui_fluent.py:1228` `self.lbl_dirty.setStyleSheet("color:#f59e0b;")` —— 橙色在深色下可读、浅色下对比度约 2.3:1，**低于 WCAG AA 的 4.5:1**（正文）/ 3:1（大字）。严重度低（是一个小 badge）。 |
| **`HealthBadge` 图标可读性** | **通过，且做得好** | `gui_fluent_providers.py:132-144` 按失败次数用 ✓/⚠/✗/✗N 四态，且颜色是 `#22c55e`/`#f59e0b`/`#ef4444`/`#dc2626` —— 这几个色值在明暗两主题下都有足够对比度。 |
| **历史详情弹窗的深色主题** | **❌ 不通过** | `gui_fluent_history_detail.py:483` `self.setStyleSheet("QDialog { background: #ffffff; }")` —— **硬编码纯白背景，无视主题**。同时 `_INLINE_CSS`（L88-159）整体是浅色配色（`body{background:#ffffff}`、`color:#1f2937`）。**后果**：系统处于深色主题时，用户从深色 History 页点开一个纯白全屏弹窗（900×720），**是全应用最刺眼的元素**；且内嵌的 WebEngine 表格也是白底，关闭弹窗后会有明显的亮度跳变。这个弹窗用了 `#6b7280`（L494 sub 标签）、`#f8fafc`（L597 raw 框）等一批浅色专用值。**建议**：至少让 `background` 跟随 `isDarkTheme()`，或改为 `self.setStyleSheet("")` 让 FluentWindow 的主题自然生效。 |
| **HistoryDetailDialog 的内嵌 CSS 重复** | **不影响用户，但是维护债** | `_INLINE_CSS` L148-150 与 L151-153 **是逐字重复的 CSS 块**（`.btn` 和 `.btn:hover` 写了两遍）。合并即可。 |
| **高 DPI** | **无法验证（见 F-14）** | Qt6 默认支持缩放，代码未做额外适配也未做回归测试。本环境无法切 125%/150% 实测。 |
| **大量行的表格性能** | **Fluent 不通过；Tk 不通过** | 两个 GUI 都用 `QTableWidget`/`ttk.Treeview`，**均无虚拟化**。Fluent `_render_result`（L2370-2460）在 UI 线程逐 `setItem`；`gui_fluent_pages.py:313-369` 的 `_populate_table` 为 200 行 × 每行 4 个 `ToolButton` widget 建 **800 个 QWidget**（虽然有 300ms 防抖，L385-391）。规模上限是 `limit=200`（L286）所以不会到 5000+，**200 行的上限让这个问题可控**，但 800 个 widget 在 Windows 上重建仍需数百毫秒。**Tk 端更糟**：`gui.py:2069-2166` 无行数上限。 |
| **空状态设计** | **通过，做得好** | `gui_fluent.py:1194-1197`（右侧主空态）+ `:2289-2331`（零行空态，含"本次返回了 N 组表格数据但当前视图不支持，可用导出 JSON 查看"这条诚实提示）+ `gui_fluent_pages.py:170-176`（History 空态）+ `gui_fluent_providers.py:991-999`（Providers 空态）。**四处空状态都有文案 + 都有 i18n 键（三语齐全，已实测确认 `extract.emptyHint`/`results.tablesNotShown`/`history.empty` 在 zh/en/ja 都存在）**，且控件显隐受控（F-12 提到的 B-03/B-14 修复痕迹）。这是这套 GUI 最成熟的部分。 |
| **超长文本溢出** | **基本通过，1 处风险** | 多数标签设了 `setWordWrap(True)`（`:1196`、`:2319`、`:3063`、`gui_fluent_pages.py:174`）。**风险点**：`gui_fluent_providers.py:192` 的 `lbl_name`（provider 名称）**没有设 wordWrap、没有设最大宽度**，而同行还有 4 个操作按钮 + endpoint 标签在 `mid` 布局里（`row.addLayout(mid, 1)`）。**一个 100 字符的 provider 名会把卡片撑破**（卡片 `setMaximumHeight(120)` L168 会裁切内容）。同类：`gui_fluent.py:1741` 的 `lbl_imginfo` 用 `\n` 拼了「文件名 + 尺寸 + resized」三段，**超长文件名（Windows 允许 255 字符）不换行**。`CaptionLabel` 默认不换行。 |
| **emoji / 图标字体依赖** | **有风险（静态判断）** | 代码用了 `⠿`（`gui_fluent_providers.py:89` 拖拽手柄）、`✓⚠✗`（L134-143）、`⏳`（L312）、`👁`（`gui.py:1257`）、`●○`（已改掉）。**`⠿`（U+283F 盲文点块）是最高风险的一个** —— 它不在 Segoe UI 的西文覆盖里，需要落到 Segoe UI Symbol；而 `ProviderIconTile` 的字体栈（L78）显式限定了 `'Segoe UI','PingFang SC','Microsoft YaHei'` 三个字体，**都不含符号字体**，拖拽手柄可能显示为豆腐块。`👁` 同理（`gui.py` 的 Tk 默认字体在 Windows 上通常能回退到 Segoe UI Emoji，风险较低）。**注**：`HealthBadge` 的 ✓/✗ 属 BMP 常见符号，Windows 下基本安全。 |

---

## 5. 验证通过清单

以下项目我**实际检查过并确认无问题**：

**实测（真实运行 Tkinter GUI）**
- ✅ `RangeChartApp` 完整实例化成功，无异常（`gui.py:419` 类，1983 行）
- ✅ `_render_result()` 连续 6 次重建，Notebook tab 数稳定（无泄漏），`_i18n` 注册表稳定在 33 条 —— **F-10 的「泄漏」担忧排除**（`_purge_i18n_subtree` L1229 有效）
- ✅ `_on_result` 失败路径（`ok=False` + HTTP 500 + error_body）正常弹窗不崩
- ✅ `_on_extract` 无图路径正常提示不崩
- ✅ `retranslate()` 连调 6 次（3 语言 × 2 轮）无异常 —— i18n 的 `try/except` 兜底有效
- ✅ 语言切换 4 轮无异常
- ✅ `_export_json` 无结果时安全返回
- ✅ `save_config` 原子写（`os.replace` + `fsync` + 临时文件清理）在两 GUI 中一致，错误向上传播（`gui.py:2277` / `gui_fluent.py:3810` 都接住了并提示）

**实测（AST / 签名核对）**
- ✅ `ProviderWizard` 的 18 处翻译调用中 16 处正确（`self._t`），仅 2 处错（= F-01）
- ✅ `Translator.t` 签名 `(self, key)` —— L770 的双参调用是独立的第二个错误
- ✅ `LlmProvider` 13 个字段实测，无 key-field-name 字段（= F-05 的依据）
- ✅ `i18n.py` 三语键齐全性抽查 6 个键全部存在
- ✅ 6 个 GUI 文件的**未捕获异常槽函数**全量清单：仅 8 处 Fluent 侧无 try（已逐个读代码评估：`attach_settings_selectors`、`_on_extract`、`_on_worker_result`、`_on_progress`、`_on_test_done`、`_on_search_changed`、`_on_order_changed`），其中 `_on_test_done`（`gui_fluent.py:3364`）我确认了 SSRF 分支已被 `SimpleNamespace` 归一化（`gui.py` 侧没有这个保护，见 F-08）
- ✅ **竞态检查**：Fluent 端所有 worker 信号（`finished_ok`/`progress`/`done`）都连到 **bound method**（`gui_fluent.py:1937-1938`、`3343-3344`、`gui_fluent_providers.py:1198-1203`），PySide6 会解析为 queued(auto) 连接，槽在 GUI 线程执行。**没有发现 lambda 直连导致的跨线程控件操作**（代码注释里记载了这类 bug 已被系统性修过，`_Worker` 的注释 L1136-1141 明确记录了 lambda→bound method 的改造）
- ✅ **Tk 端线程安全**：`_poll_queue`（`gui.py:2000`）用 `Queue` + `root.after` 回主线程，是正确的 Tk 跨线程模型；`on_close`（`gui.py:2294-2299`）取消 `after` 回调，且 `_poll_queue:2018` 有 `winfo_exists()` 双重保护
- ✅ **`ExtractWorker.run` 的异常覆盖**：`_run_multi` 的抽取（`gui_fluent.py:548`）确实让单一 `except`（L537）覆盖了单次与多次两条路径，且 `import concurrent.futures` 在 `_run_multi` 内部（L566）没有 NameError 风险
- ✅ **`_orphaned_workers` 机制**：`_park_orphaned_worker`（`gui_fluent.py:187-223`）在 `closeEvent`（L4142/4158/4189）三处正确调用，`finished` 连 `_drop_ref` + `deleteLater`，顺序正确
- ✅ **`closeEvent` 的协作式关闭**：`quit()` 未被误用（QThread.run 覆写时是 no-op，代码注释正确指出了这点），`terminate()` **全局零使用**
- ✅ **破坏性操作确认框覆盖**：`_on_clear`（History，`gui_fluent_pages.py:400-409`）、`_on_delete`（单条，`gui_fluent_pages.py:569-576`）、`_on_discard_edits`（丢弃编辑，`gui_fluent.py:2602-2608`）、`UsagePage._on_clear`（`gui_fluent_pages.py:860-867`）**四处都有 `MessageBox` 确认**（唯一缺失的是 F-02/F-03）
- ✅ **`_apply_edits` 的持久化返回值契约**：`_persist_edits_to_history` 返回 bool 且 `_on_apply_edits`（`gui_fluent.py:2671`）只在 True 时才提示"已保存" —— 这是一个**正确的**「不谎报成功」设计
- ✅ **XSS 防护**：`_build_webengine_html`（`gui_fluent_history_detail.py:196`）对内联 JSON 做了 `</` → `<\/` 转义，且注释解释了原因
- ✅ **matplotlib figure 生命周期**：`_fig_to_pixmap`（`gui_fluent_pages.py:59-91`）在 `finally` 里关闭 figure，`_render_chart`（L828-836）还有一层 belt-and-braces
- ✅ **WebEngine profile 生命周期**：`_shared_web_profile()`（`gui_fluent.py:123-146`）parent 到 QApplication 而非 view，避免了 teardown 顺序崩溃
- ✅ **`js/table.js` 内联无重复声明**：`RCA_I18N` 不重复注入（注释 L198-205 解释了为什么不能注入）

**读代码判断的通过项（未实测）**
- ✅ 提取取消按钮（`btn_cancel`，`gui_fluent.py:1148-1150`）接线正确，取消后按钮自禁用并显示"正在取消"（`:1635-1636`）—— **这个 UI 诚实性设计很好**：它明确告诉用户取消需要等当前 HTTP 请求返回
- ✅ 图片拖放（`dragEnterEvent` L1312 / `dropEvent` L1322）只接受 URL，不接受无法消费的格式（`hasImage()` 分支已移除并有注释说明原因）
- ✅ 粘贴临时文件的生命周期（`_cleanup_paste_tmp` + 异常路径 unlink，L1718-1808）处理得很干净，注释明确记录了"注册 tmp 必须在 `_load_image` 成功之后"这个曾经存在的泄漏
- ✅ Provider 卡片内存管理：`_parked` / `flush_parked` / `_retain` 机制（`gui_fluent_providers.py:387-419`）+ `qconfig.themeChanged.connect` 用 bound method（:268，注释说明 lambda 会泄漏所有历史卡片）
- ✅ History 详情弹窗的 `deleteLater()`（`gui_fluent_pages.py:501-515`）—— 注释记录了 `QDialog.exec()` 只隐藏不销毁、`WA_DeleteOnClose` 不触发这个真实的泄漏

---

## 6. 无法验证的点

| 无法验证项 | 原因 |
|---|---|
| **Fluent GUI 的全部运行时行为** | **PySide6 在本环境未安装**（`ModuleNotFoundError: No module named 'PySide6'`），且按指示不尝试安装。所有 Fluent 侧结论（F-01～F-07、F-09、F-11～F-17）均为**读代码判断**。F-01 我通过 AST 穷举 + `Translator.t` 的真实签名做了间接实测验证，置信度高；其余的置信度已逐条标注。 |
| **qfluentwidgets 的 Pivot / FluentWindow 具体行为** | 同上（依赖 PySide6）。特别是 `gui_fluent.py:1200-1205` 那段注释声称「onClick 回调在 Windows 上不可靠，只有首次点击才触发」—— 这个论断我无法验证，且如果属实是一个上游 bug，值得上报。 |
| **高 DPI 125%/150% 下的实际像素表现**（F-14） | 本环境显示缩放为 100%，无法切换。需要多台不同缩放的 Windows 机器实测。 |
| **F-06/F-07 的用户操作序列** | 逻辑从代码上是确定的（`_set_busy` 的完整列表、`_on_result` 的调用点），但「提取窗口期内换图/换 provider」这个具体时序需要真实 Qt 环境 + 一次真实的 LLM 提取才能复现。 |
| **Tkinter 各路径的实际耗时**（F-10 的「阻塞数百毫秒」） | 无显示器环境无法可靠计时。表格重建的行数开销是按 `ttk.Treeview` 无虚拟化这一事实推断的，未测 ms 数。 |
| **超大图内存占用**（F-15） | 1.1 GB 的数字是按 `20000²×3 字节` 算出来的，**未实测**。Windows 上 `QPixmap` 可能还有 DPR 相关的额外倍数。 |
| **`⠿` 拖拽手柄是否显示为豆腐块** | 需要在目标 Windows 机器上目视确认。字体回退行为因系统语言/安装字体而异。 |
| **F-04 的向导「设为当前」关闭窗口** | 静态推断明确（`gui.py:2740` 的 `self.on_close()`），但 `ProviderWizard` 没有 `withdraw()` 方法，我无法在无头环境构造它做无副作用的点击测试。**这一条我认为置信度足够（中高）**，但需要有人在真机上点一次确认。 |
| **`gui.py:1974` 全失败时重复发请求** | 读代码发现的疑似问题（`if not ok_datas: ... else extract(mode=mode, **params)`），但我**没有把它列为正式缺陷**，因为不确定是否是有意设计（比如"重试一次"）。**标注为疑虑**：如果 5 次 runs 全部超时，这条路径会**再发一次完整提取**，耗时再翻一倍，且这次是单发（不再有 130s 预算）。建议作者确认意图。 |

---

## 附：与其他审查者的接口

- 本报告只覆盖 GUI 端（`gui_fluent*.py` + `gui.py`），未审查 `js/`（由 web-reviewer 负责）。
- §3.4 的「三端重复清单」中 Web 列的行号我未逐一给出（需要 `js/` 与 `server.py` 的索引），但已标明功能名与严重度，web-reviewer 可直接补充行号。
- 若 web-reviewer 发现 `js/` 侧存在与 §3.4 第 5/6/11 行（模式识别 / HistoryRecord 构造 / i18n 键表）对应的第三份实现，则这三处的下沉收益应按三端计。
