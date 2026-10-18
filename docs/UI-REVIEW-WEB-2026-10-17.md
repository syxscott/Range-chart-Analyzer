# UI 审查报告 — 纯前端（Web）套件

**审查范围**：`index.html` + `css/`（4 个文件）+ `js/`（17 个文件，共约 27,900 行）
**审查日期**：2026-10-17
**审查者**：web-reviewer
**源码改动**：无。本报告为只读审查，未修改任何 `.js` / `.css` / `.html` 文件。

**基线（本次审查开始时实测，全部通过）**

| 套件 | 结果 |
|---|---|
| `tests_frontend.js` | **846 passed, 0 failed** |
| `tests_viz.js` | 161 passed, 0 failed |
| `tests_aggregate.js` | 60 passed, 0 failed |
| `tests_contrast.js` | contrast: 327 passed, 0 failed |
| `tests_edit_history.js` | 334 passed, 0 failed |
| `tests_export_parity.js` | 91/91 checks passed |
| `tests_contract_mirror_2026_09_22.js` | 58 passed, 0 failed |
| `tests_diff_frontend_parity.js` | 1360 matched, 8 documented, 0 diverged |
| `tests_history_fixes_2026_09_21.js` | 23 passed, 0 failed |
| `tests_names_i18n_fixes_2026_09_22.js` | 137 passed, 0 failed |
| `tests_sharpen.js` | 175 checks passed |
| `tests_ui_fixes_2026_09_22.js` | 64 passed, 0 failed |
| `tests_ui_fixes_2026_09_27.js` | 75 passed, 0 failed |

`node --check` 对 `app.js` / `table.js` / `viz.js` / `i18n.js` / `history.js` 全部通过。

---

## 1. 总体结论

这套前端在**稳健性**上做得相当扎实，本次审查的三个猜测性高发区**均未发现缺陷**：HTML 里 57 个 id 与 20 个可点击元素全部接上了事件或委托；`rcaEsc()` 在 `table.js` 的 HTML 拼接中没有漏点（11 条注入用例实测全部转义）；连点 Extract / 提取中取消 / 提取中重置 / 提取中切语言 / 提取中切主题等竞态路径全部有正确的不变量保护，**没有发现一个"UI 卡在 busy 态"的路径**。全部 13 个测试套件 0 失败。

真正的缺陷集中在**国际化完整性与可访问性文案**这一层，且有两个是**用户可见的**：(1) 编辑表格单元格期间切换语言，结果面板会**永久停留在旧语言**（`app.js:1920` 的早退分支没有留下待刷新标记，编辑结束也没有补偿路径）；(2) 图表联动区（`#viz-heading` 与 `.viz-hint`，即延限图上方的标题和那行操作提示）在 `index.html` 里是硬编码中文，既没有 `data-i18n`，也不在 `js/viz.js` 自己的三语文案表里 —— 英/日语用户看到的图表区标题永远是中文。除此之外，表格编辑器的全部 28 条界面文案和历史栈的 11 条文案只做了 zh/en 双语，**日语用户在这些位置会看到英文**（代码注释把它标为"accepted residual"，但对用户是实打实的降级）。视觉与主题层面没有发现硬编码颜色问题：`viz.js` 的 canvas 全部走 `rcaVizToken()` 读 CSS 变量并带 MutationObserver 主题翻转重绘；深浅两套主题的关键文本对比度全部达标。

`table.js` 4349 行确实偏大，但**可拆分性意外地好**：37 个章节边界清晰（编辑器层 vs 各图表模式配置层 vs Python 语义镜像层互相独立，只有加载顺序耦合），最大单块 832 行（`range-chart mode`）。详见第 5 节。

**本次共报 6 条缺陷：P0 = 0，P1 = 3，P2 = 2，P3 = 1。**

---

## 2. 缺陷清单

### WEB-01 · P1 · 编辑表格单元格期间切换语言，结果面板永久停留在旧语言

**位置**：`js/app.js:1918-1925`（`rcaRetranslateResult` 的早退分支）

```js
function rcaRetranslateResult() {
  if (!state.result) return;
  const content = $('results-content');
  if (!content) return;
  const ae = document.activeElement;
  const editing = _rcaIsTextTarget(ae) && !!(ae && (ae.isContentEditable
    || (typeof ae.getAttribute === 'function' && ae.getAttribute('contenteditable') !== null
      && ae.getAttribute('contenteditable') !== 'false')));
  if (editing) {
    // Unsaved edit in flight: swap only the static page chrome ...
    // and leave the result panel in the previous language until the next render.
    return;                     // ← 问题：没有留下任何"待刷新"状态
  }
```

**复现步骤**
1. 打开页面（默认中文），上传任意图表，点「开始提取」，等结果出来。
2. 点击结果区任意一个表格单元格的文本（该单元格 `contenteditable="true"`，获得焦点，光标在其中，**先不要改内容**）。
3. 点右上角语言切换的「EN」（或「JA」）。注意：这一步只切换了页面静态 chrome（页头、设置面板、按钮标签），**结果区的表头 / 区段标题 / 空态文案仍是中文**。
4. 点击别处结束编辑（focusout → `rcaEditCommitCell`），或按 Esc（→ `rcaEditRevertCell`）。
5. 观察：结果面板**仍然是中文**。用户已明确选了英文，却要等到下一次完整提取（重新提取 / 点「强制重新提取」/ 刷新页面）才会换成英文。

**后果**
- 表头、区段标题、编辑器的操作按钮全部停留在旧语言，而同一页面上的设置面板已经是新语言 —— 用户看到的是一个**中英混排**的结果面板。
- 三语用户中只有中文用户不会遇到；英/日语用户在"编辑单元格 + 切语言"这个组合下会误以为切语言功能坏了。
- 代码注释说 "until the next render"，但**没有任何机制触发那次 render**：`rcaEditCommitCell`（`table.js:3484-3525`）与 `rcaEditRevertCell`（`table.js:3529+`）的函数体内都不调用 `rcaRetranslateResult` / `renderCurrentResult`，也不写任何 pending 标记。

**实测证据**（`lang_stale_test.js`，复用 `tests_frontend.js` 的 vm 沙箱构造方式）

```
切换前 panel.innerHTML            = "<h3>种属延限 (Species Ranges)</h3>"
切到 EN 后 panel.innerHTML        = "<h3>种属延限 (Species Ranges)</h3>"
renderCurrentResult 调用次数      = 0        (0 = 面板未被重译)
结束编辑后 panel.innerHTML        = "<h3>种属延限 (Species Ranges)</h3>"
renderCurrentResult 累计调用次数  = 0
→ 面板在编辑结束后仍未重译 = true
```

**严重度**：P1。数据不会错，但用户可见的语言状态永久不一致，且没有自愈路径。
**置信度**：高（实测复现）。
**验证方式**：可执行。修法方向建议：在早退分支写一个 `state._pendingRetranslate = true`，然后在 `rcaEditCommitCell` / `rcaEditRevertCell` 之后（或 `rcaEditOnFocusOut` 末尾）检查该标记并调用 `rcaRetranslateResult()`。

---

### WEB-02 · P1 · 图表联动区的标题与操作提示是硬编码中文，任何语言下都不翻译

**位置**：`index.html:367` 与 `index.html:376`

```html
<span class="viz-heading-text" id="viz-heading">延限—层位联动图</span>
...
<span class="viz-hint">悬停画布行＝高亮表格行 · 点击＝固定聚焦 · 再点/Esc＝取消 · ↑↓＝键盘移动 · Enter＝固定</span>
```

**复现步骤**
1. 打开页面，点右上角「EN」（或「JA」）。页头、设置面板、上传卡片全部切成英文/日文。
2. 上传图表并提取，等结果出来，滚到顶部的「延限—层位联动图」区块。
3. 观察：区块标题仍是中文「延限—层位联动图」，其下方的操作提示行仍是中文「悬停画布行＝高亮表格行 · 点击＝固定聚焦 · 再点/Esc＝取消 · ↑↓＝键盘移动 · Enter＝固定」。

**后果**
- 这是图表联动区唯一的**可见标题**和唯一的**操作说明**。英文/日语用户只能读懂其中文。
- 该提示行是被写进代码注释的**功能契约**（"click to pin, Esc to unpin, ↑↓ to move, Enter to pin"），配合同一区域上方的 `#viz-summary`（`role="status" aria-live="polite"`，负责读屏播报）。对使用读屏的用户，播报内容会正确切到英文/日文（那部分走 `js/viz.js:1118` 的 `RCA_VIZ_STRINGS` 三语表），但**看得见的说明文字是中文** —— 视觉与听觉两条通道给出的语言不一致。
- 与 `index.html` 里其他所有文案的处理方式不一致：文件里其余每一个中文文案元素都带 `data-i18n`，且 `index.html:30-39` 的注释明确说明了这个约定。这两个元素是唯一的例外。

**为什么不是"漏加 `data-i18n` 那么简单"**：`js/viz.js` 有自己的独立三语文案表 `RCA_VIZ_STRINGS`（`viz.js:1118-1167`，`en`/`zh`/`ja` 各 10 个 key，三语 key 集合已验证一致），并通过 `rcaVizT()` 查表。它**不使用** `js/i18n.js` 的 `t()`，也没有 `viz.*` 前缀的 key 可供 `data-i18n` 使用。所以补 `data-i18n` 还需要在 `RCA_VIZ_STRINGS` 里同时加 key 并让 `viz.js` 在渲染时回写这两个节点 —— 两处改动，不是加个属性就完事。

**证据**：`grep -rn "viz-heading|viz-hint" js/ css/ tests_*.js` → `js/` 与 `tests_*.js` **零命中**，只有 `css/viz.css:65,72,208` 和 `css/app-ux.css:247` 引用了这两个 class。JS 完全不碰这两个节点。逐元素扫描 `index.html` 找出"含中日韩字符但元素本身无任何 `data-i18n*` 属性"的文本节点，全文件仅此 2 处。

**严重度**：P1（一个完整功能区块对 2/3 的界面语言不可读）。
**置信度**：高（脚本穷举 + grep 双向确认）。
**验证方式**：可执行。

---

### WEB-03 · P1 · 表格编辑器与历史栈的界面文案只做了 zh/en 双语，日语用户看到英文

**位置**：`js/table.js:1616-1655`（`RCA_EDIT_STRINGS`）、`js/ics_table.js` 无、`js/history.js:45-60`（`RCA_HISTORY_STRINGS`）

`js/table.js:1616` 起的 28 条 `edit.*` 文案，每条只有 `{ zh, en }`，**没有 `ja`**。回退逻辑 `rcaEditT`（`table.js:1665-1683`）：

```js
s = entry[rcaEditLang()] || entry.en || entry.zh;   // ja 落不到 entry.ja → entry.en
```

`js/history.js:63-70` 的 `rcaHistoryT` 同样：

```js
let s = entry ? (rcaHistoryLang() === 'zh' ? entry.zh : entry.en) : key;   // 非 zh 一律 en
```

**受影响的全部界面文案（`table.js` 侧 28 条）**
`edit.addRow`、`edit.addRowHint`、`edit.exportSelected`、`edit.deleteSelected`、`edit.clearSelection`、`edit.locate`、`edit.locateColumn`、`edit.locateHint`、`edit.selectAll`、`edit.selectRow`、`edit.selectedCount`、`edit.cellNotNumber`、`edit.cellNotYesNo`、`edit.badStage`、`edit.rangeInverted`、`edit.ageInverted`、`edit.idxInverted`、`edit.undo`、`edit.redo`、`edit.editsPending`、`edit.rowsDeleted`、`edit.noViz`、`edit.notEditable`、`edit.noRow`、`edit.addRowScalar`

**历史栈侧 11 条**：`edit.undo`、`edit.redo`、`edit.undoCell`、`edit.redoCell`、`edit.undoRowDelete`、`edit.redoRowDelete`、`edit.undoRowAdd`、`edit.redoRowAdd`、`edit.historyEmpty`、`edit.historyEnd`、`edit.historyFailed`

**复现步骤**
1. 点右上角语言切换的「JA」。
2. 上传图表并提取，等结果出来。
3. 观察结果区：表格编辑 chrome（每行的「新增行」按钮、每行的「定位」按钮、表头的「操作」「选择全部行」、选择工具条的「导出所选 / 删除所选 / 清除选择」以及「已选 N 行」计数）全部是**英文**。
4. 点任何一个单元格进入编辑模式，往一个数值列里输入 `"abc"`，看校验提示 —— 报的是 **"A number is required: abc"**，不是日文。
5. 点顶部的撤销/重做按钮 —— 悬停提示与读屏播报是 **"Undo" / "Redo"**。
6. 把某行的 `range_base` 改得比 `range_top` 老，校验提示是 **"Range inverted: top 200 is older than base 300"**。

**后果**
- 与同页面的语言不一致：设置面板、按钮、区段标题都是日文，唯独表格编辑器的按钮、计数、**以及所有校验错误消息**是英文。
- 校验消息这一类尤其关键：这是用户"我填错了"的第一反馈面，而 `rcaEditAnnounce`（`table.js:3404`）会把它播报到 `role="status"` live region，读屏用户也只能听到英文。
- 与 `js/viz.js:1148-1156` 的处理形成鲜明对比 —— 那里明确记录了"F-02: 这个文案表原来只有 en 和 zh，日语用户会在 role=status live region 里听到错误语言"，并已补齐三语。同一类问题在 `table.js` / `history.js` 侧仍未处理。

**代码里已知的现状**：`table.js:1648-1650` 与 `table.js:1650` 附近的注释写明 "the ja locale keeps falling back to en — **accepted residual**"。也就是说这是**已知未修**，不是本次新发现。但它确实是当前用户可见的语言缺陷，故列出。

**严重度**：P1（日语界面有 39 条关键操作/错误文案降级为英文）。
**置信度**：高（脚本逐 key 提取语言集合 + 回退逻辑读码确认）。
**验证方式**：可执行。

---

### WEB-04 · P2 · 5000 行以上结果无虚拟滚动，单次渲染产生 1.6 MB HTML 字符串与 3 万个单元格节点

**位置**：`js/table.js:1197-1515`（`rcaRenderResults`），无任何视窗裁剪逻辑

**实测数据**（`perf.js`，直接调 `rcaRenderResults` 并计时；注意这只测**字符串拼接**成本，不含浏览器解析与布局）

| 行数 | 拼接耗时 | HTML 长度 | `<td>` 数量 |
|---|---|---|---|
| 500 | 8.6 ms | 0.22 MB | 4,500 |
| 2,000 | 18.1 ms | 0.68 MB | 13,500 |
| 5,000 | 36.2 ms | 1.62 MB | 31,500 |
| 20,000 | 131.5 ms | 6.42 MB | 121,500 |

虚拟化检查：`table.js` 中 `IntersectionObserver` **0 次**、`requestIdleCallback` **0 次**、`content-visibility` **0 次**、`DocumentFragment` **0 次**；`slice(0,` 仅 4 次且都用于**截断内容**（raw text 预览等）而非分页渲染。

**复现步骤**
1. 用一张种属极多的密排大图（数百到上千个种属延限），配合「运行次数 1–5」与「最大输出 Token 32000」，提取出 5000 行以上 `species_ranges`。
2. 提取完成瞬间观察界面。

**后果**
- `app.js:1703` 一次性执行 `content.innerHTML = rcaRenderResults(...)`。字符串拼接只占 36 ms，但**浏览器解析 1.62 MB HTML + 布局 31,500 个 `<td>`** 是后续成本，主线程在这段时间无法响应滚动与点击，表现为**提取完成后界面短暂冻住**（无进度提示、无 `requestAnimationFrame` 让帧）。
- 更严重的连带：`rcaViz` 的画布绘制（`viz.js`）与结果面板的重渲染在同一帧竞争主线程；`rcaEditRerender`（`table.js:3640`，每次增删行都触发）在 5000 行时会重跑同一条全量路径。
- 内存：31,500 个 `<td>` + 各自的 `title` 属性字符串，稳态占用可观；每次语言切换（走 `rcaRetranslateResult` 的原地文本改写路径）还要额外持有一份 `state._resultTexts` 快照（`app.js:1813`）。
- 注意：这是**已知的规模上限问题，不是回归**。`tests_diff_frontend_parity.js` 等测试用的都是几十行量级，测试不会暴露此问题。

**严重度**：P2（数据量大时才触发，且用户有可感知的等待，但不会崩溃或丢数据）。
**置信度**：高（拼接耗时与节点数实测；浏览器解析/布局成本为读码判断，未在真实浏览器测量 —— 见第 7 节）。
**验证方式**：拼接层已实测；渲染层为静态分析。

---

### WEB-05 · P2 · 浅色主题下「记住 API Key」开关的开启态为 2.54:1，边框色低于 WCAG 1.4.11 的 3:1 非文本对比度

**位置**：`css/style.css:16`（`--success-color: #10b981`）、`css/style.css:793-795`（`.ios-switch[aria-checked="true"] { background: var(--success-color); }`）、`css/style.css:975`（置信度环 stroke）、`css/style.css:43-44`（边框 token）

**实测对比度**（`contrast.js`，从 `css/style.css` 解析 `:root` 与 `:root[data-theme="dark"]` 两套 token 后计算 WCAG 2.x 比值）

浅色主题：

| 前景 | 背景 | 比值 | 判定 | 用途 |
|---|---|---|---|---|
| `--text-muted #57534e` | `--bg-white #ffffff` | 7.63:1 | PASS | 正文次级文字 |
| `--text-light #78716c` | `--bg-white #ffffff` | 4.80:1 | PASS | `.field-hint` / `.caption-counter` / `.cell-empty` / `.footer` / `.empty-state` |
| `--text-light #78716c` | `--bg-lighter #efedeb` | **4.11:1** | 正文阈值 FAIL | 仅在 `thead th` 的 `@supports not (backdrop-filter)` 回退路径上 |
| `--primary-color #2563eb` | `--bg-white` | 5.17:1 | PASS | 主色文字 |
| `--warning-color #b45309` | `--bg-white` | 5.02:1 | PASS | 警告文字 |
| `--danger-color #dc2626` | `--bg-white` | 4.83:1 | PASS | 危险文字 |
| `--info-color #6366f1` | `--bg-white` | 4.47:1 | 正文阈值 FAIL（差 0.03） | — |
| `--success-color #10b981` | `--bg-white` | **2.54:1** | **FAIL 3.0** | `.ios-switch[aria-checked=true]` 背景（`style.css:794`）、`.confidence-ring.high .bar` 的 `stroke`（`style.css:975`） |
| `--border-color #e7e5e4` | `--bg-white` | **1.26:1** | FAIL 3.0（非文本） | 所有卡片 / 输入框 / 表格边框 |
| `--border-strong #d6d3d1` | `--bg-white` | **1.49:1** | FAIL 3.0（非文本） | `.empty-state svg`、开关轨道底色、多种分隔线 |

深色主题：全部文本配对 **PASS**（`--text-muted #a8a29e` on `--bg-white #221f1d` = 6.50:1，`--primary-color` = 6.44:1，`--warning-color` = 9.81:1，`--danger-color` = 5.92:1）。仅边框同样低于 3:1（`#292524` = 1.08:1、`#3f3a36` = 1.46:1）。

**复现步骤**
1. 切到浅色主题（点主题切换的太阳图标，或系统为浅色时用默认 AUTO）。
2. 滚到设置卡片底部的「在本浏览器记住 API Key」开关（`.ios-switch`，`index.html:203`）。点一下让 `aria-checked="true"` —— 整个开关轨道变成 `#10b981`，画在卡片白底上，**2.54:1**。这是**开关的开/关状态本身**，不是装饰：轨道底色（关闭态 `--border-strong` #d6d3d1 = 1.49:1 与开启态 #10b981 = 2.54:1 的对比也仅约 1.7:1）就是状态的唯一视觉编码，白色滑块在两种状态下都能看清，所以**开关的状态本身靠滑块位置传达**——这降低了严重度，但轨道色本身作为"控件边界"仍低于 3:1。
3. 打开浏览器 DevTools → 检查任意 `.card` 的边框 —— `1.26:1`，几乎与卡片背景同色。窄边框 + 大圆角 + 浅投影的组合下，**卡片的边界在视觉上非常弱**。
4. 提取出置信度 ≥ 70% 的结果，看左上角置信度环 —— `stroke: var(--success-color)` 即 `#10b981` 画在白底上（环内 track 也是极浅色），同样 2.54:1，浅色主题下这个"高置信度"的绿色环读起来偏淡。

**后果**
- 边框低于 3:1 意味着**低视力用户在浅色主题下难以分辨卡片的边界与输入框的框线**（WCAG 1.4.11 非文本对比度）。这是两套主题里唯一系统性的对比度短板，深色主题反而没这个问题。
- `--success-color` 2.54:1 低于 WCAG 1.4.11 的 3:1 阈值。它当前用作 `background` 与 `stroke`（非文本），所以**不构成 1.4.3 的正文失败**，但确实是 1.4.11 的失败。
- 深色主题下同一 token 无此问题：`#10b981` 画在 `#221f1d` 上 = 5.62:1。

**为什么这不是回归**：代码里已有明确的对比度整改历史 —— `style.css:18` 的注释写着 `--warning-color: #b45309; /* amber-700: ~4.7:1 on white (was #f59e0b ≈ 2.9:1, failed WCAG AA) */`，`style.css:132-135` 为 dark 模式重调了 `--primary-dark` / `--warning-color` / `--danger-color`，`style.css:206-212` 为 dark 模式补了 alert/pill/badge 的文字色。**遗留的两处正是 `--success-color` 与边框 token 未纳入同一轮整改**（注意 dark 侧把 `--success-color` 也留在了 light 值上，但因 dark 底色深反而达标，所以整改轮次把它跳过了）。

**严重度**：P2。
**置信度**：高（比值由脚本从 CSS 源解析计算，受影响选择器 `style.css:793-795` 与 `:975` 已逐一确认）。**但 WCAG 1.4.11 是否适用于卡片边框，取决于设计意图（是否"理解内容所必需"）——见第 7 节第 3 条，建议人工判定后再决定是否整改。**
**验证方式**：对比度计算与选择器身份已确认；WCAG 适用性判定为读码判断。

---

### WEB-06 · P3 · 全部界面字号使用绝对 px，浏览器"放大文字"设置不生效

**位置**：`css/style.css`（32 处 `font-size: …px`）、`css/table-edit.css`（6 处）

**实测**：`grep -c "font-size:.*px" css/*.css` → `style.css` 32 处、`table-edit.css` 6 处、`app-ux.css` 0 处、`viz.css` 0 处。全项目 **0 处** `font-size` 使用 `rem` 或 `em`，`html` 选择器上也没有 `font-size` 基准。因此没有任何一处字号依赖根字号。

**复现步骤**
1. Chrome/Edge：设置 → 外观 → 缩放，或直接 Ctrl`+` 放大到 150% / 200%。
2. 或：设置 → 辅助功能 → 视觉 → 文本大小，拖到 200%。
3. 观察：**基于浏览器缩放的场景下布局正常放大**（因为整个视口被缩放），但**基于"文本大小"（只改根字号）的场景下，所有文字大小完全不变** —— 页面的文字与它在真实印刷品上的相对大小关系被固定住了。

**后果**
- 依赖根字号的低视力用户，其"放大文字"偏好对这套界面的**文字部分无效**（图标与间距因为用 px/百分比，同样不变）。整套界面实际只支持"整页缩放"一种放大方式。
- 最小的字号是 `table-edit.css:100` 的 `8px`（`.rca-edit-cell.rca-cell-dirty::after` 的 `content: "●"` 未保存标记），但它是纯装饰圆点，不承载信息，因此**不构成信息不可读**。承载信息的最小字号是 `style.css` 的 `--text-2xs: 11px`（图注计数器）与 `.field-hint` 的 `11.5px`，在 200% 整页缩放下有效。

**严重度**：P3（可访问性便利性，非功能缺陷）。
**置信度**：高（CSS 源穷举）。
**验证方式**：静态分析（需真实浏览器确认各浏览器的具体行为差异，见第 7 节）。

---

## 3. 死元素与 ID 对账

### 3.1 ID 对账（脚本 `id_recon.js` + `dyn_id.js`）

- `index.html` 中的 `id` 总数：**57**
- `js/` 中通过 `$('#x')` / `getElementById('x')` / `querySelector('#x')` 引用到的 id 总数：**41**

**B1 · `js/` 引用了但 `index.html` 中不存在的 id（会导致 `$()` 返回 `null`，随后 `.xxx` 抛 `TypeError`）**

```
names-verify-slot   refs=2
    js/app.js:1206   [getElementById('x')]    const host = document.getElementById('names-verify-slot');
    js/table.js:1545 [getElementById('x')]    const host = document.getElementById('names-verify-slot');
```

**判定：误报，非缺陷。** `names-verify-slot` 是 `table.js:1308` 在渲染结果时动态创建的（`parts.push('<div id="names-verify-slot"></div>')`），所以不在 `index.html` 里。两处调用点都紧跟 `if (!host) return;` 守卫，slot 不存在时静默跳过 —— 这正是它在 `index.html` 里不存在的场景下应有的行为。`app.js:1843` 还把它登记进 `_RCA_FOREIGN_RESULT_SLOTS`（`{ 'viz-host': 1, 'names-verify-slot': 1 }`），在每次 `innerHTML` 重写后重新挂回。

**B2 · `index.html` 中存在但 `js/` 从未用 `$()` 引用过的 id（16 个）—— 全部为误报，逐条核对如下**

```
conn-mode-hint      → 被 aria-describedby / 视觉提示用，无 JS 必要
conn-mode-label     → 仅作为 #conn-mode-seg 的 aria-labelledby 目标（index.html:182）
dz-aria             → #dropzone 的 aria-describedby 目标（index.html:220）
enhance-hint        → #enhance 的 aria-describedby 目标（index.html:153）
extract-btn-label   → 仅 CSS/标记用；app.js:925 重建按钮时重新生成该 span
main                → skip-link 的 href="#main" 目标（index.html:29/92）
max-edge-hint       → #max-edge 的 aria-describedby 目标
max-edge-out        → <output for="max-edge">；app.js:590 用数组 ['max-edge','max-edge-out'] 同步
max-tokens-hint     → #max-tokens 的 aria-describedby 目标
max-tokens-out      → <output for="max-tokens">；同上
remember-key-label  → #remember-key 的 aria-labelledby 目标（index.html:203）
runs-hint           → #runs 的 aria-describedby 目标
runs-out            → <output for="runs">；同上
settings-card       → 结构性/滚动定位用，无 JS 引用
upload-card         → 同上
viz-heading         → #viz-host 的 aria-labelledby 目标（index.html:365）— 但见 WEB-02：文案本身是硬编码中文
viz-summary         → 视觉可见的 aria-live 区域，由 viz.js 用 querySelector('.rca-viz-summary') 定位 class 而非 id
```

**结论：无死元素。** 16 个"未被 `$()` 引用"的 id 全部是 ARIA 关联目标（`aria-labelledby` / `aria-describedby`）、`<output for>` 关联、`<a href="#...">` 锚点，或由 JS 以 class 选择器间接操作。

### 3.2 可点击元素审计（脚本 `click_audit.js`）

`index.html` + `js/` 中生成的 `<button>` / `<a>` 共 **20 个**。

**已接事件监听器（9 个，逐一核对 `addEventListener` 调用点）**

| 元素 | 位置 | 绑定点 |
|---|---|---|
| `theme-choice` × 3 | `index.html:57/58/59` | `js/theme.js:84`（`wireToggleButtons`，对 `[data-theme-choice]` 循环绑定） |
| `lang-switch` × 3 | `index.html:81/82/83` | `js/app.js:2312`（事件委托，`closest('button')` + `data-lang`） |
| `#conn-mode-seg` × 3 | `index.html:183/184/185` | `js/app.js:2555`（委托，`closest('button[role=radio]')` + `data-value`） |
| `#api-key-toggle` | `index.html:104` | `js/app.js:2334` |
| `#save-settings` | `index.html:210` | `js/app.js:2345` |
| `#extract-btn` | `index.html:279` | `js/app.js:2402` |
| `#reset-btn` | `index.html:282` | `js/app.js:2408` |
| `#cancel-btn` | `index.html:283` | `js/app.js:2423` |
| `#force-rerun-btn` | `index.html:284` | `js/app.js:2435` |

**事件委托绑定**

| 元素 | 位置 | 绑定点 |
|---|---|---|
| `#btn-export-all` | `js/table.js:1303` 生成 | `js/app.js:2041`（`if (target.id === 'btn-export-all')`） |
| `data-copy="{safeId}"` | `js/table.js:1334` 生成 | `js/app.js` 结果区委托 |
| `data-csv="{safeId}"` | `js/table.js:1335` 生成 | `js/app.js` 结果区委托 |
| `history` 删除按钮 | `js/history.js:527` 生成 | `js/history.js:473`（`el.addEventListener('keydown', handler)`） |
| `.skip-link` | `index.html:29` | 锚点原生跳转（`href="#main"`），`#main` 有 `tabindex="-1"` |

**结论：无死按钮。20 个可点击元素全部有对应的监听器或委托处理器。**

### 3.3 动态生成的 id（脚本 `dyn_id.js`）

```
btn-export-all    生成于 js/table.js:1303   $() 引用：NONE
                                          → 委托绑定见 3.2，非死元素
names-verify-slot 生成于 js/table.js:1308   $() 引用：js/app.js:1206, js/table.js:1545
                                          → 正常
模板字符串中的动态 id（含 ${}）：0 个
```

---

## 4. 主题与对比度

### 4.1 主题机制评估（`js/theme.js`，103 行）

三态切换（system / light / dark），`localStorage` key `rca.theme`。机制正确：

- `apply()`（`theme.js:43-57`）在 system + 浅色时**移除** `data-theme` 属性，让 `prefers-color-scheme` 媒体查询生效；否则显式设置。这避免了"显式 light 覆盖 OS dark"的经典 bug。
- `wireSystemListener()`（`theme.js:72-78`）仅在 `current === 'system'` 时响应 OS 变化，手动切换后不被 OS 偏好冲掉。同时兼容 `addEventListener`（现代）与 `addListener`（旧 Safari）。
- `wireToggleButtons()`（`theme.js:80-90`）用 IIFE 闭包捕获 `btn`，避免 `var` 循环闭包陷阱。
- `aria-pressed` 在 `apply()` 内同步。
- `theme.js` 在 `index.html:437` 于 `app.js` 之前加载，且首行就调 `apply()`（`theme.js:94`），避免暗色模式重载时的白屏闪烁。

**canvas 主题响应**：`js/viz.js:1867-1877` 用 `MutationObserver` 监听 `documentElement` 的 `data-theme` / `class` 属性变化，回调里 `S.tokens = null; rcaVizDraw();` —— 这解决了 `rcaVizToken()`（`viz.js:1251-1264`）缓存 `getComputedStyle` 的经典失效问题。**实测确认这是正确的**。

### 4.2 硬编码颜色清单

**`js/` 中的颜色字面量共 21 处**（脚本 `colors.js`，排除纯注释行）

**canvas 绘图上下文（9 处）—— 全部走 `rcaVizToken()`，第二参数只是 fallback**

| 色值 | 位置 | 用途 | 对应 token |
|---|---|---|---|
| `#2563eb` | `viz.js:1435, 1501, 1563` | 奇数阶带 / 焦点描边 / hover 描边 | `--primary-color` / `--primary-active` |
| `#78716c` | `viz.js:1508` | 不可定位条虚线 | `--text-light` |
| `#b45309` | `viz.js:1514` | 不可定位条焦点描边 | `--warning-color` |
| `#d6d3d1` | `viz.js:1588` | tooltip 边框 | `--border-strong` |
| `#eff6ff` | `viz.js:1560` | hover 十字带 | `--primary-soft` |
| `#ffffff` | `viz.js:1584` | tooltip 背景 | `--bg-white` |
| `#0c0a09` | `viz.js:1590` | tooltip 文字 | `--text-dark` |

**其他位置（12 处）**

| 色值 | 位置 | 判定 |
|---|---|---|
| `#2563eb` / `#10b981` / `#6366f1` / `#78716c` / `#6b7280` | `viz.js:1269-1273`（`rcaVizBarColor`）、`viz.js:1436` | **fallback 值**，`rcaVizToken()` 有真实 CSS 变量时不会被用。CSS 变量一定存在（`:root` 必定义），因此实际不可达。 |
| `#57534e` / `#d6d3d1` | `viz.js:1443-1444` | 同上，fallback |
| `#003366` / `#00BFFF` / `#90EE90` | `prompt.js:509-511` | 提示词模板里的**示例图注描述文字**，发给模型的 prompt 文本，不渲染到 DOM |
| `#FF5733` | `prompt.js:772` | 同上 |

**内联 `.style.*` 赋值中的颜色字面量：0 处。** 这是重要结论 —— 所有 `.style.xxx = '…'` 赋值（`viz.js` 的 `style.height`、`run-progress-fill` 的 `style.width` 等）都不涉及颜色，颜色一律走 CSS 类或 CSS 变量。

**结论：无硬编码颜色缺陷。** 两套主题覆盖了所有组件：`css/style.css` 中 `[data-theme="dark"]` 选择器出现 42 次，`viz.css` 2 次，`table-edit.css` 4 次，`app-ux.css` 3 次。

### 4.3 低对比度组合清单

见 **WEB-05** 的实测表。要点：
- 浅色主题：所有**文本**配对达标（最低 `--text-light` on `--bg-white` = 4.80:1）。唯一擦边的是 `--info-color` 4.47:1（差 0.03）。
- 浅色主题非文本：`--success-color` 2.54:1、`--border-color` 1.26:1、`--border-strong` 1.49:1 低于 3:1。
- 深色主题：所有**文本**配对达标（最低 `--danger-color` 5.92:1）。非文本同样只有边框低于 3:1。

**深色主题未覆盖的 `:root` 变量（30 个）—— 逐个判定为无问题**

```
--primary-light / --success-color / --info-color / --secondary-color  → 未被任何 color: 用作前景
--surface (= var(--bg-lighter))                                        → 跟随 dark 覆盖，OK
--shadow-sm / --shadow / --shadow-md / --shadow-xl / --shadow-primary → dark 覆盖了 --shadow-soft/--shadow-pop/--shadow-lg
--transition / --transition-fast / --radius-* / --text-* / --tracking-* / --font-* → 非颜色，OK
```

`--success-color` / `--info-color` / `--secondary-color` 在 dark 下不覆盖，但它们当前只作 `background`（如 `style.css:794`）与 `stroke`（`:975`），不作前景文字，因此 dark 下 `#10b981` 画在 `#221f1d` 上 = 5.62:1，反而比浅色更好。**不是缺陷。**

### 4.4 i18n 缺失 key 清单

**`js/i18n.js` 主字典：三个语言 key 集合完全一致。**（脚本 `i18n_diff.js`，实际在 vm 沙箱里跑 `i18n.js` 取出真实字典后做集合运算）

```
各语言 key 数: zh=322  en=322  ja=322
并集 key 数: 322
缺失 key（存在于并集但在某语言缺失）: （三语 key 集合完全一致）
空字符串值: 0 个
占位符 {xxx} 集合三语不一致的 key: 0 个
```

**使用点覆盖检查**（扫描 `js/*.js` 的 `t('x')` / `i18nKey: 'x'` 与 `index.html` 的 `data-i18n*`）

```
使用点 key 总数: 112
在字典里查不到的 key: 0 个
```

**结论：`js/i18n.js` 层面没有任何缺失翻译，没有任何 fallback 到 key 本身的情况。** 另有 210 个字典 key 在扫描方式下"看似无使用点"，逐一核对确认全部通过运行时拼接可达（`col.*` 73 个走列配置表、`sec.*` 26 个走区段配置表、`err.*` 25 个走错误码映射、`quality.*` 30 个走质量问题列表、`reason_code.*` 13 个走 reason code 表），**不是死翻译**。

**散落的迷你文案表才是缺陷所在** —— 见 **WEB-03**：

| 文件 | 表名 | key 数 | 缺 en | 缺 ja |
|---|---|---|---|---|
| `js/table.js:1616` | `RCA_EDIT_STRINGS` | 28 | 0 | **28** |
| `js/history.js:45` | `RCA_HISTORY_STRINGS` | 11 | **0**（6 个走 `table.js` 共享表） | **11** |
| `js/viz.js:1118` | `RCA_VIZ_STRINGS` | 10 | 0 | 0 ✓ 三语齐备 |

**未被任何文案表覆盖的界面文案** —— 见 **WEB-02**（`#viz-heading` + `.viz-hint`，2 处）。

---

## 5. `table.js` 体量分析

**总行数 4,349；顶层符号 152 个；`// ===` 章节注释 37 处。**

### 5.1 职责区块表

| 行号区间 | 行数 | 职责 | 层次 |
|---|---|---|---|
| 5–23 | 19 | `rcaEsc` / `rcaEscAttr` HTML 转义原语 | 基础设施 |
| 25–168 | 144 | chart-shape 判定谓词（`rcaLooks*`） | 模式层 |
| 169–451 | 283 | occurrence-matrix 模式（`rcaLooksOccurrenceMatrix` / `rcaStratValues` / `rcaOccurrenceMatrixConfigs`） | 模式层 |
| 452–489 | 38 | 可编辑列元数据（`rcaEC` / `rcaECro`） | 模式层 |
| 490–530 | 41 | abundance-diagram 模式配置 | 模式层 |
| 531–652 | 122 | columnar-section 模式配置 | 模式层 |
| 653–685 | 33 | phylogenetic-tree 模式配置 | 模式层 |
| 686–750 | 65 | zonation / correlation 模式配置 | 模式层 |
| **751–1582** | **832** | **range-chart 模式（默认）：`rcaRangeChartConfigs` 146 行 + `rcaColumnarSubTableRows` 61 行 + `rcaRowsForTable` + `rcaExportCellText` + `rcaRowAgreementBand` + `rcaRenderResults` 319 行** | **模式 + 渲染 + 导出** |
| 1583–1606 | 24 | 注释块（i18n 前言） | — |
| 1607–1684 | 78 | 编辑器专用 i18n（`RCA_EDIT_STRINGS` / `rcaEditT`） | 编辑器层 |
| 1685–1691 | 7 | 编辑模式开关（`rcaEditFlag`） | 编辑器层 |
| 1692–1845 | 154 | Python 语义镜像原语（`rcaPyEqual` / `rcaPyFloatStr` / `rcaCoerceVal` / `rcaPyParseNumber`） | 语义层 |
| 1846–2183 | 338 | diff 遍历的列表键（`RCA_EDIT_LIST_KEYS` / `rcaEditAlignRows` / `rcaCaptureListEdits` / `rcaApplyEdits` / `rcaNewRowTemplate`） | 编辑器层 |
| 2184–2368 | 185 | 校验器（`rcaParseBedN` / `rcaIcsLookupStage` / `rcaValidateCell` 等） | 语义层 |
| 2369–2579 | 211 | 低置信行与 J/K 跳转（`rcaTableIsLowConfidenceRow` / `rcaNextLowConfidenceRow` / `rcaEditKeepsEmptyKey` 609 行） | 编辑器层 |
| 2580–2674 | 95 | 脏标记注册表（`rcaTableEdits`：setCellEdited / getEditedCells / clear） | 编辑器层 |
| 2675–2914 | 240 | 写入路径（`rcaTableEdits.editCell` / `editRow` 等） | 编辑器层 |
| 2915–3009 | 95 | 结构化编辑（增删行） | 编辑器层 |
| 3010–3047 | 38 | 选择状态（导出/删除所选） | 编辑器层 |
| 3048–3186 | 139 | diff 导出 + `rcaEditSelectionBar` + `rcaColEditSpec` + `rcaEditDataAttrs` | 编辑器层 |
| 3187–3222 | 36 | 注释块 + `RCA_EDIT_DOM` 状态单例 | — |
| 3223–3305 | 83 | 微小 DOM 助手（stub-friendly，`rcaEditAttr` / `rcaEditMatches` / `rcaEditClosest` / `rcaEditText` 等） | DOM 层 |
| 3306–3401 | 96 | 单元格定位（`rcaEditRefFromCell` / `rcaEditNeighbourCell`） | DOM 层 |
| 3402–3420 | 19 | 提示 / live region（`rcaEditAnnounce`） | DOM 层 |
| 3421–3557 | 137 | 写入 / 绘制路径（`rcaEditPaintCell` / `rcaEditCommitCell` / `rcaEditRevertCell`） | 编辑器层 |
| 3558–3690 | 133 | 结构化重渲染（`RCA_EDIT_FOREIGN_SLOT_IDS` / `rcaEditCaptureForeign` / `rcaEditRerender`） | 编辑器层 |
| 3691–3791 | 101 | 选择（DOM 侧） | 编辑器层 |
| 3792–3840 | 49 | viz 守卫（`rcaVizNs` / `rcaVizCall` / `rcaEditVizFocus` 等） | 集成层 |
| 3841–3894 | 54 | 行高亮 / 定位（`rcaTableHighlightRow` / `rcaEditLocateRow`） | 集成层 |
| 3895–3918 | 24 | J/K 跳转（`rcaEditStepLowConfidence`） | 集成层 |
| 3919–3940 | 22 | history 桥（`rcaEditPublishAction`） | 集成层 |
| 3941–3976 | 36 | 键盘（`rcaEditHandleKey`） | 交互层 |
| 3977–4196 | 220 | 委托事件处理器（focusin/focusout/click/change/mouseover/mouseout/paste） | 交互层 |
| 4197–4299 | 103 | attach / detach（`rcaTableEditAttach` / `rcaTableEditDetach` / `rcaTableEditAfterHistory`） | 交互层 |
| 4300–4349 | 50 | 全局导出（`globalThis.*` 37 个符号） | — |

### 5.2 最大的三个函数

```
609 行  js/table.js:2478  rcaEditKeepsEmptyKey
319 行  js/table.js:1197  rcaRenderResults
146 行  js/table.js:758   rcaRangeChartConfigs
```

### 5.3 可拆分性评估

**结论：可拆分性好，且已在实践中被验证（模式层就是拆出来的）。**

依据：

1. **已有明确的拆分先例。** 行 169–750 已经把 6 种图表模式（occurrence-matrix / abundance / columnar-section / phylogenetic-tree / zonation / range-chart）的配置表拆成了独立区块，每个都有独立的 `rca*Configs` 函数。这意味着"按图表模式拆"是本项目已确立的拆分轴。

2. **三层的依赖方向是单向的，可解耦。**
   - **语义层**（`rcaPyEqual` / `rcaPyFloatStr` / `rcaValidateCell` / `rcaIcsLookupStage`，行 1692–2368 + 2184–2368）**不依赖任何 DOM**，纯粹是 Python `editable.py` 语义的 JS 镜像。这是最干净的一块，可独立成 `js/py-semantics.js`。
   - **编辑器层**（行 1607–1606 以外的全部 `rcaEdit*` + `rcaTableEdits`）依赖语义层与 DOM 层，但不依赖任何具体图表模式（它通过 `rcaColEditSpec` / `rcaRowCellsFor` 间接拿到列元数据）。可独立成 `js/table-edit.js`。
   - **模式配置层**（行 25–903）只导出配置表函数，不碰 DOM 与编辑器。
   - **渲染层**（`rcaRenderResults`，行 1197–1515）只依赖模式配置层。

3. **唯一的真实耦合是加载顺序**（`index.html:433` 的 `<script defer src="js/table.js">`）。这是 `<script>` 标签顺序，不是模块依赖 —— 拆成多个文件只需在 `index.html` 里按依赖顺序多列几个 `<script defer>` 即可，不需要引入 ES module。

4. **`rcaEditKeepsEmptyKey` 单函数 609 行**（行 2478–3086）是最大的单体。它覆盖 ICS 阶段名查找、层号/年龄/延限三类倒置判定、以及"编辑后该保留哪个空值"的决策 —— 逻辑上是 3 件不同的事，但因为共用同一批中间量（`cleaned` / `normBed` / `ageMa`）而被写在一起。**这是最值得先拆的单一目标**，拆成 `rcaKeepEmptyIcs` / `rcaKeepEmptyBed` / `rcaKeepEmptyAge` 三块预计各 150–250 行、彼此无交叉调用。

5. **`rcaRenderResults` 319 行**（行 1197–1515）内部结构其实很规整：工具栏 → 置信环 → 质量徽章 → 证据链 chip → 逐表渲染 → 原文 `<details>`。拆成 `rcaRenderToolbar` / `rcaRenderTable` / `rcaRenderRaw` 三块后，**每次表格渲染所需的上下文（`safeId` / `editOn` / `rows` / `idx`）可以用一个显式对象传递**（现在靠 `forEach` 闭包隐式捕获）。这个拆分能直接缓解 WEB-04：有了 `rcaRenderTable` 的边界，就能对单个大表加"首屏渲染 + 剩余惰性追加"。

**建议拆分顺序**（按投入产出比）：
1. `rcaEditKeepsEmptyKey` → 3 个函数（单文件内，零风险）
2. `rcaRenderResults` → 3 个函数（单文件内，直接为虚拟化铺路）
3. `table.js` → `py-semantics.js` + `table-edit.js` + `table-render.js`（多文件，需动 `index.html` 的 script 顺序 + 4 个测试文件的加载列表）

**不拆的理由（现状可接受）**：152 个顶层符号中 25 个最大的加起来占全文件约 60%，且文件末尾的 37 个 `globalThis.*` 导出（行 4300–4349）构成了一个清晰的公共面 —— 这正是拆分的抓手。在真正需要改这个文件的人手上有明确入口。

---

## 6. 验证通过清单

以下维度**明确检查并通过**，不列缺陷：

**死元素与 ID 对账**
- ✅ `index.html` 57 个 id 与 `js/` 的引用双向对账，无未引用死元素，无缺失 id（唯一命中项 `names-verify-slot` 为动态生成，见 3.1）
- ✅ 20 个 `<button>` / `<a>` 全部有事件监听器或委托处理器（9 个直接绑定 + 5 类委托 + 1 个原生锚点）
- ✅ 动态生成的 id 仅 2 个（`btn-export-all`、`names-verify-slot`），均已接上委托/直接调用
- ✅ 模板字符串中无动态 id（含 `${}` 的 id 拼接：0 处）

**XSS / 转义**
- ✅ `rcaEsc()`（`table.js:5-13`）同时转义 `& < > " '`，`rcaEscAttr()`（`table.js:21-23`）复用同一实现，属性上下文安全
- ✅ `table.js` 全部 HTML 拼接点（23 处未走白名单的拼接项）逐一核对：全部是 `Math.round` 计算出的数字（`confPct` / `qpct`）、枚举常量（`confLevel` / `qLevel` / `pillClass` / `rowCls`）、由 `rcaHumanize` 生成的 slug（`safeId`）、或已自行拼好引号的属性串（`titleAttr` / `rcaEditDataAttrs`）
- ✅ 单元格渲染双路径（`table.js:1470-1476`）都过 `rcaEsc(val)`，`title` 属性走 `rcaEscAttr(val)`
- ✅ **11 条注入用例实测**（`xss_test.js`）：向 `species` / `authority` / `section` / `confidence` / `range_base` / `quality.grade` / `quality.issues[].msg_key` / `report.empty_tables[].reason` / `_auto_mode.mode` 注入 `<img src=x onerror=alert(1)>` 与 `" onmouseover="alert(2)"`，**全部被转义**
- ✅ `app.js` 的 `preview-meta`（`:874`、`:2285`）对 `file.name` 走 `rcaEsc`
- ✅ `app.js:669` 的注释确认告警条用 `createElement` + `textContent` 构建，从不 `innerHTML`

**内存与生命周期**
- ✅ `URL.createObjectURL` 仅 2 处，均有对应 revoke：`js/export.js:107/115`（`setTimeout` 撤销）、`js/minimax.js:525/541`
- ✅ `_rcaSharpenBlobUrl`（`minimax.js:375`）有专门的生命周期注释 + `teardown` 撤销（`:480-481`）
- ✅ `Worker.terminate()`（`minimax.js:491`）有 teardown 路径
- ✅ `new Worker(url)` 抛异常时有同步兜底（`minimax.js:531-542`），且 `sharpen_worker.js:34-38` 在 kernel 缺失时尝试 `importScripts`，`:52-56` 在 kernel 仍缺失时回 `{ ok: false }` 让主线程用自己的副本 —— `tests_sharpen.js` 的 175 项检查覆盖了"worker offload + sync fallback"
- ✅ `setInterval` **0 处**（无轮询泄漏）
- ✅ `viz.js` 的 `MutationObserver` / `resize` 监听有对应 `detach`（`viz.js:2124`），且注释（`:2105-2106`）明确记录了"匿名 touchstart/keydown/resize 闭包和 theme MutationObserver 曾泄漏"
- ✅ `table.js` 的委托监听有 `rcaTableEditAttach` / `rcaTableEditDetach`（行 4199 / 4256）成对管理，事件清单集中在 `RCA_EDIT_EVENTS`（行 4184）
- ✅ 空 `catch {}` 块 **0 处**

**状态与竞态（全部实测，`race_test.js` + `dblclick_probe.js`）**
- ✅ 快速连点 Extract 两次 → 只发 1 次 `POST /api/extract`（另 1 次是 CSRF `GET /api/extract`，属正常预取），`state.busy` 正确守住（`app.js:1291` `if (state.busy) return;`）
- ✅ 提取中点 Cancel → `busy` 归位、`loading-slot` 隐藏、`results-empty` 恢复、Extract 重新可用、Cancel 隐藏（`updateActionButtons` 的三条不变量：`app.js:966-980`）
- ✅ 提取中点 Reset → `busy`/`loading` 归零、`results-empty` 恢复、Extract 可用、Force-rerun 隐藏，且悬挂的提取最终态 `busy=false`（`app.js:2139` 清 `_resultTexts`、`:2172` 调 `updateActionButtons`）
- ✅ 提取中切语言 → 不崩，`setBusyLabel` 的 `_busyLabelFmt` 缓存让 loading 文案跟着切换（`app.js:2261-2263`）
- ✅ 提取中切主题 → 不崩，`viz.js` 的 MutationObserver 正确失效 token 缓存并重绘
- ✅ `state.loading=true` 期间 `runExtraction` 提前返回（`app.js:1298-1301`），防止用上一张图的 `dataUrl` 配新文件名发请求
- ✅ `Ctrl+Enter` 快捷键同样受 `state.busy || state.loading` 守卫（`app.js:2661`），且放行 `<textarea>` 内的 Ctrl+Enter

**误操作确认**
- ✅ Reset 有 `rcaConfirm` 确认框，且仅在"真有数据可丢"（`state.result || state.dataUrl`）时才弹（`app.js:2409-2419`）
- ✅ Force-rerun（丢弃缓存 + 再次付费调用）有确认框（`app.js:2440-2443`）
- ✅ 确认框实现了焦点陷阱（Tab 在两个按钮间弹跳）、Esc 关闭、背景点击关闭、焦点归还打开者（`app.js:352-411`）
- ✅ 导出全部用 `btn-export-all` 单次点击，无"覆盖已有文件"的无确认覆盖路径（导出走 `<a download>` + `revokeObjectURL`，不写用户选定的路径）

**可访问性**
- ✅ 跳过链接 `.skip-link` → `#main`（`tabindex="-1"`，`index.html:29/92`）
- ✅ `#lang-switch` 与 `#conn-mode-seg` 是 `role="radiogroup"` + `role="radio"` + `aria-checked`，roving tabindex，方向键/Home/End 可操作（`app.js:2316` / `:2575`）
- ✅ 确认框 `role="dialog"` + `aria-modal="true"` + `aria-labelledby` + 焦点陷阱 + Esc
- ✅ `#loading-slot` 是唯一 live region；告警条不再嵌套 live region（`index.html:294-304` 注释记录了这个整改）
- ✅ 进度条 `#run-progress` 移出 live region，避免重复播报（`index.html:316-330`）
- ✅ 图注计数器不是 live region，只有跨越 warn/over 阈值时才写 polite region（`index.html:265-274`）
- ✅ viz hover canvas 有 `tabindex="0"` + ↑↓/Home/End/Enter/Space 键盘操作 + `#viz-summary` 文本替代
- ✅ 所有可编辑单元格有 `aria-label`；选择复选框有 `aria-label`；表格有 `aria-label` 与 `scope="col"` / `scope="row"`
- ✅ `prefers-reduced-motion` 在 4 个 CSS 文件里都有处理；JS 侧跳过置信环数字补间
- ✅ `forced-colors: active`（Windows 高对比度模式）在 `app-ux.css:179` 与 `table-edit.css:414` 有处理

**主题与对比度**
- ✅ `js/viz.js` 的 canvas 颜色全部走 `rcaVizToken()`，MutationObserver 在主题翻转时失效缓存并重绘
- ✅ 内联 `.style.*` 赋值零硬编码颜色
- ✅ 深浅两套主题的关键文本对比度全部 ≥ 4.47:1
- ✅ `:root[data-theme="dark"]` 与 `@media (prefers-color-scheme: dark) :root:not([data-theme])` 两条路径的 token 覆盖一致（各 27 个变量，已逐项比对）
- ✅ `theme.js` 的 system 模式正确让位给 `prefers-color-scheme`，无"显式 light 覆盖 OS dark"问题

**i18n**
- ✅ `js/i18n.js` 三语 key 集合完全一致（322 = 322 = 322），0 空值，0 占位符不一致
- ✅ 112 个使用点 key 全部在字典中，无一 fallback 到 `[?key]`
- ✅ `js/viz.js` 的 `RCA_VIZ_STRINGS` 三语 key 集合一致（有测试守护）
- ✅ 缺失翻译时 `t()` 返回带标记的 `[?key]` 而非裸 key（`i18n.js:1147-1155`），不会渲染成乱码

**响应式**
- ✅ `<meta name="viewport" content="width=device-width, initial-scale=1.0">`（`index.html:5`）
- ✅ 4 处断点：`max-width: 640px`（容器内边距 + 预览图 + viz 提示换行）、`max-width: 480px`（`.form-grid` 单列）、`max-width: 640px`（`style.css:1369`）
- ✅ 表格横向滚动：`.table-wrap` 容器 + sticky 表头/首列

**性能**
- ✅ Worker 卸载图像增强计算（`tests_sharpen.js` 175 项覆盖）
- ✅ `rcaVizToken` 缓存 computed style，不逐帧读样式
- ⚠️ 但**无虚拟滚动** —— 见 WEB-04

---

## 7. 无法验证的点

1. **浏览器解析与布局成本（WEB-04 的后半段）**
   实测只覆盖了 `rcaRenderResults` 的**字符串拼接**耗时（36 ms / 5,000 行）。把 1.62 MB HTML 塞进 `innerHTML` 后的**解析、样式计算、布局、绘制**成本必须在真实浏览器测量。我的沙箱是 DOM stub，不含布局引擎。结论中"主线程短暂冻住"是基于字符串体积与节点数量的推断（3 万个 `<td>`），**未经浏览器实测**。验证方法：在 DevTools Performance 面板录一段真实提取。

2. **WCAG 1.4.11 的适用性判定（WEB-05）**
   比值计算准确（脚本从 CSS 源解析），受影响的选择器也已逐一确认（`style.css:793-795` 的 `.ios-switch[aria-checked="true"]`、`style.css:975` 的 `.confidence-ring.high .bar`、`style.css:365` 的 `.card` 边框）。但 1.4.11 只约束**理解内容所必需的**非文本元素：
   - **开关轨道色**：状态本身靠白色滑块的位置传达，轨道色是辅助编码。整改必要性：**中等**。
   - **置信度环 stroke**：环内还有 `data-conf` 百分比数字（`table.js:1228` 渲染的 `.num`），所以颜色是冗余编码。整改必要性：**低**。
   - **卡片边框**：如果它是纯装饰（真正的分组靠间距与阴影），则豁免成立。整改必要性：**需设计确认**。

   **这一段我标为「需人工判定」，不建议直接按失败处理。** 报告里给出的比值是硬事实，是否构成失败取决于上述设计意图。

3. **DPI 缩放与浏览器字号放大的行为差异（WEB-06）**
   CSS 源层面可以确定「0 处 `rem`/`em` 字号」这个事实。但"浏览器文本大小设置在各浏览器/各平台的具体行为"我没有实测（Chrome/Edge/Firefox 对 `minimum font size` 与「文本缩放」的处理不同）。结论按"整页缩放有效、纯文本缩放无效"表述。

4. **`rcaVizToken()` 的缓存是否在所有主题变更路径上失效**
   已确认 `viz.js:1867-1877` 的 MutationObserver 监听 `data-theme` 与 `class`。但如果主题变化通过其他方式发生（例如未来引入 `<style>` 内联改写、或 `color-scheme` 属性切换而不改 `data-theme`），缓存不会失效。当前 `js/theme.js` 只改 `data-theme`（`theme.js:47/49`），所以**当前是正确的**。这属于"当前无缺陷，但对未来的耦合点"。

5. **GBIF 物种名校验的网络行为**
   `rcaVerifySpeciesNamesAsync`（`app.js:1195`）会向 `api.gbif.org` 发最多 20 个请求。我的沙箱里 `fetch` 全被 stub 成 reject，因此**只验证了 token 所有权保护与 fail-silent 行为，没有验证真实网络下的超时/预算路径**。代码里的 `NAME_VERIFY_TIMEOUT_MS` / `NAME_VERIFY_BUDGET_MS` 逻辑是读码判断。

6. **图片解码窗口内的实际竞态**
   `state.loading` 的守卫（`app.js:1298-1301`）在沙箱里验证了逻辑正确，但没有在真实浏览器里复现"20 MB 图片解码期间点 Extract"这一时序 —— 沙箱的图片加载是同步的。

7. **未审查的相邻文件**
   `js/prompt.js`（1081 行）我只检查了它对颜色的使用（4 处颜色字面量全部是发给模型的提示词文本，不渲染到 DOM），**没有审查它的提示词内容质量**。`js/reason-codes.js`（852 行）、`js/aggregate.js`（1512 行）、`js/quality.js`（1848 行）我只在 XSS 扫描与硬编码颜色扫描中扫过，**未做交互性审查**（这三个是纯数据变换模块，无 DOM，交互面为零，这是省略的理由而非遗漏）。

---

*审查者：web-reviewer | 2026-10-17 | 未修改任何源码*
