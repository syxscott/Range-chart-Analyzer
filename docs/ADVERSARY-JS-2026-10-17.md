# ADVERSARY-JS-2026-10-17 — JS 侧修复的对抗性审查

审查对象：`js/aggregate.js`（`mergeScalarField` 类型保留）、`js/app.js`（换图清理时序 + 导出门禁）、
以及两端 parity 是否真的守住。

**测试全绿不构成正确性证据**：`tests_frontend.js` 846 passed / 0 failed，
`tests_diff_frontend_parity.js` 1345 matched / 8 documented / **0 diverged**，
其余 10 个 JS 套件全绿。本文档报告的问题全部是**测试覆盖不到、且由本次修复引入**的。

> 审查期间 `js/aggregate.js` 被其他成员在 20:08 改过一次（我 20:0x 读到的第一版 diff 与磁盘上的
> 最终版不同）。本文所有实测均针对**磁盘当前版本**。

---

## 1. 结论

| 项 | 结果 |
|---|---|
| `js/app.js` 换图清理 + 导出门禁（Part B，6 项时序） | **6/6 通过**，未发现缺陷 |
| `js/minimax.js` occurrence_matrix / generic_table（Part C） | **无 parity 风险**，浏览器端有显式硬拒绝 |
| 12 个 JS 套件 + parity 套件 | 全部通过，无回归 |
| `js/aggregate.js` `mergeScalarField` 类型保留（Part A） | **发现 1 个 P0 回归** |

**找到的真问题：1 个**（1 × P0 高置信度 + 2 个 P3 观察项）。

P0 是本次修复自身引入的 **JS↔Python parity 回归**：改动把「投票前 trim」丢掉了，
导致浏览器端对带首尾空白的字段合并出**带空白**的值，而 HEAD 与 Python 都返回 trim 后的值。
`tests_diff_frontend_parity.js` 报 0 diverged 的原因是 **fixture 从未构造「同一字段、
一个 run 带空白、另一个 run 不带」的竞争用例**（详见 §2.1 的 fixture 缺口分析）。

---

## 2. 真问题清单

### 2.1 【P0 · 高置信度】`mergeScalarField` 丢失投票前 trim，JS 与 Python 对带空白字段给出不同合并值

**位置**：`js/aggregate.js:258-275`（`mergeScalarField` 的投票池构建 + 返回）
**对照**：`rca_core/aggregate.py:358-383`（`_merge_scalar_field`）

**根因（实测）**：

改动前，JS 用 `String(v).trim()` 作为**投票键**，所以 `" Ml "` 和 `"Ml"` 是**同一个候选**，
票数 2:0，trim 后的值胜出。改动后（`js/aggregate.js:265`）把**原始值**推进池子：

```js
if (!rcaStrForMerge(v).trim()) continue;   // 只用 trim 后的结果做「是否为空」判断
pool.push(v);                              // ← 推的是原始值，键因此不再 trim
```

而 `rcaAggMode` 内部按 `rcaStrForMerge(v)` 建键，`rcaStrForMerge` **不 trim**：

```
rcaStrForMerge(" Ml ") = " Ml "
rcaStrForMerge("Ml")   = "Ml"
=> 两个不同的 Map 键 => 1:1 平票 => 排序后 " Ml "（空格 < M）胜出
```

Python 侧 `aggregate.py:364` 是 `s = _str_for_merge(v).strip()`——**投票键是 trim 过的**，
并且 `strs` 里存的也是 trim 后的键。两端因此在同一个输入上选出不同的合并值。

**触发输入 → 实测输出**（最小复现，三路对照）：

```
$ node -e "...mergeFieldAcrossRuns([' 7 ','7'])..."      # 当前 JS
$ python -c "..._merge_scalar_field([' 7 ',7])..."
```

| 输入 | HEAD JS（改动前） | 当前 JS | Python |
|---|---|---|---|
| `["Ml", " Ml "]` | `"Ml"` | **`" Ml "`** | `"Ml"` |
| `["-1", " -1 "]` | `"-1"` | **`" -1 "`** | `"-1"` |
| `[" 2 ", 2]` | `"2"` | **`" 2 "`** | `"2"` |
| `[2, " 2 "]` | `"2"` | **`" 2 "`** | `"2"` |
| `[" 7 ", 7]` | `"7"` | **`" 7 "`** | `"7"` |

**为什么是 bug**：改动前 JS 与 Python 一致（HEAD 列 == Python 列），改动后 JS 单方面偏离。
注意最后两行——**这是混合类型场景**，`js/aggregate.js:275` 的
`return (sawText && sawNumber) ? String(winning) : winning;` 本应把它收敛成字符串 `"7"`，
但因为 `String(" 7 ")` 仍是 `" 7 "`，连这条兜底也失效了。

**端到端可达性（用生产形状载荷实测，非单元级）**：

```
runs = [ {notes:"bed 9", _extras:{note:"bed 9"}},
         {notes:" bed 9 ", _extras:{note:" bed 9 "}} ]   // 两次运行，同一字段
```

```
JS    : merged notes = " bed 9 "        PY : merged notes = "bed 9"        ← 分歧
JS    : merged _extras.note = " bed 9 " PY : merged _extras.note = "bed 9"  ← 分歧
JS    : _extras.thick [7," 7 "] = " 7 " PY : _extras.thick [7," 7 "] = "7" ← 分歧
```

影响面：任何**非 strModeField** 的标量字段走 `mergeFieldAcrossRuns` → `mergeScalarField`
（`js/aggregate.js:376-378`）。`strModeFields`（species / section / range_base / range_top /
biozone 等）走 `rcaAggNorm`（`js/aggregate.js:17-22`）**它会 trim**，所以这些字段不受影响 ——
实测 `range_base: [" bed 9 ", "bed 9"]` 两端都给 `" bed 9 "`，一致。受害的是 `_extras`、
`notes` 这类自由字段，以及 chart_mode 的非 strMode 列。

**影响用户**：合并后的值带上了模型输出的原始空白，随后进入 JSON/CSV/XLSX 导出。
两端对同一次多运行提取给出**不同的导出文件**，这是 parity 项目要消灭的那类问题，
而且方向正好和本次修复的目标（让两端一致）相反。

**规模**：4000 例差分 fuzz（确定性 PRNG）：

```
JS current divergences vs PY : 902 /4000
JS HEAD divergences vs PY    : 1975 /4000     ← HEAD 更差（这就是本次修复的动机）
其中「HEAD 一致、当前不一致」= 699 例（本次引入）
其中「当前一致、HEAD 不一致」= 1772 例（本次修好的）
```

699 例回归**全部**是同一形态：`JS 保留了空白而 Python trim 掉了`（0 例其他形态）。

**严重程度**：P0。
**置信度**：高。最小复现、三路对照、端到端载荷、4000 例 fuzz 四重印证。

**fixture 为什么没抓到（这是本次最值得记的一点）**：

```
$ python -c "统计 tests/fixtures/frontend_parity_2026_09_20.json 里的带空白字符串"
whitespace-padded non-blank strings in fixture: 23
```

fixture **有** 23 处带空白字符串，但**没有一个**让「同一 merge 字段跨 run 同时出现
padded 与 unpadded 两个变体」。最接近的 4 处（case796 / 1192 / 1193 / 1194）：

- case1192-1194 的 `range_base: [" 7 ", "7"]` 看起来正是这个场景，但它走的是
  `ballots`（`rcaRecombinationBallots`，`js/aggregate.js:685-696`）——该函数用
  `rcaAggNorm` 归一，**会 trim**，所以两端一致，测不到本 bug。
- 也就是说：**触发路径存在，触发条件（generic scalar path + padded/unpadded 竞争）
  不存在**。parity 套件的 0 diverged 是真的绿，但它绿在别的分支上。

**建议修复方向（已实测验证，未改动仓库源码）**：
让 JS 与 Python 一样，**投票键用 trim 后的字符串，另存 (key, original) 二元组**，
返回 original 而不是 key。候选补丁（临时副本 `d:/tmp/rcadv/aggregate_fixed.js`）
在 4000 例 fuzz 上：

```
JS CANDIDATE-FIX divergences vs PY: 106 /4000    （当前 902，HEAD 1975）
候选修复新破坏的用例: 0
候选修复修好的用例:   796
```

残留 106 例是**既有问题、本次未引入**（`[true, ""]` 这类 bool-pool 兜底：JS 返回
`NO_MERGE` 而 Python 返回 `True`；HEAD 行为与当前完全相同），不在本次修复范围。
`NaN` 在 JSON 里变 `null` 是序列化假象，非缺陷。

---

### 2.2 【P3 · 高置信度】导出门禁复用了语义不符的 i18n key，拒绝时提示误导

**位置**：`js/app.js:2025-2032`（`rcaRefuseStaleExport`）

代码注释声称复用 `err.exportFailed` 是因为「an export refusal is an export failure,
the key reads correctly in every language」。实测三语文案：

```
js/i18n.js:22   'err.exportFailed': '无法写入文件，请检查目标路径是否可写：'
js/i18n.js:418  'err.exportFailed': 'Could not write the file — check that the target path is writable:'
js/i18n.js:782  'err.exportFailed': 'ファイルに書き込めません。保存先に書き込み権限があるか確認してください：'
```

实测拒绝时的 toast：

```
B2 null-file+result: downloads= 0 | toast= "无法写入文件，请检查目标路径是否可写："
```

**为什么是 bug**：门禁拒绝的原因是**来源图与结果不匹配**（根本没写文件），
提示却说**写入失败/路径不可写**。用户会去检查文件夹权限，而真实原因是「这个结果属于另一张图，
请重新提取」。三语全都误导。

**为什么测试没抓到**：`tests_frontend.js:4866` 只断言
`localizedExportFailure.indexOf(toastAfter) !== -1`，即 toast **等于该 key 的任一语言值**就通过 ——
它校验的是「用了这个 key」，不是「这个 key 语义对」。key 被复用就必然通过。

**附带事实**：全库搜索确认 `err.exportFailed` 除本处外**没有任何其他调用点**
（`js/app.js:2031` 是唯一一处），注释中「already present」的措辞暗示它本来服务于某个
导出失败路径，实际是孤立的。

**严重程度**：P3（文案误导，不影响数据正确性；门禁本身行为正确）。
**置信度**：高（三语文案 + 实测 toast + 孤立的调用点搜索）。

---

### 2.3 【P3 · 实测观察，非本次引入】`normalize_generic_table_result` 在列无 `key` 字段时丢弃所有单元格

**位置**：`rca_core/extractor.py:6162`（`normalize_generic_table_result`）
**是否本次引入**：**否** —— 用 HEAD 版 extractor 实测，输出完全相同：

```
WORKING: [{"index": 0, "label": "", "cells": ["", ""]}]
HEAD   : [{"index": 0, "label": "", "cells": ["", ""]}]
```

即无论 `columns` 有无 `label`、有无 `key`，只要行数据是 `{"a":1,"b":2}` 而
`columns` 用 `key`/`name` 字段索引，单元格一律变空：

```
labels_present (label='A','B' + key) -> cells ["",""]
label_empty    (label='','B' + key) -> cells ["",""]
no_key_field   (label='','B' + name)-> cells ["",""]
```

同时 `rows` 的 `label` 也恒为 `""`（我喂的 `columns` 没有行 label 字段，
所以这一项可能只是我的载荷形状问题，不作为缺陷报告）。

**为什么仍写进来**：这是 Part C 明确要求抽查的「generic_table 的空 label 列」，
实测结果是**空 label 列会被丢弃而不是保留位置**，与该 normalizer 自己的警告文案
（`"column N has no readable header; kept at its position so the row cells stay aligned"`）
所承诺的行为相反 —— 警告说保住了，数据却没了。

**严重程度**：P3，且**不在本次修复范围内**（属 extractor.py 既有行为）。
建议转给 py-adversary 或另开条目，避免被误认为本次改动引入。
**置信度**：中高（单元格丢失已实测复现且 HEAD 相同；`label` 恒空可能是我载荷形状问题）。

---

## 3. 验证通过的清单

以下均为**实测**（命令与输出见各节），明确判定为「改动是对的」。

### 3.1 `js/aggregate.js` —— Part A 用例逐条（两端一致）

用 42 例定向用例 + 类型感知比较器（区分「值不同」与「Python int vs JS number 的表示差异」，
后者不是分歧——JSON 无 int/float 类型）逐条对比 `mergeScalarField`：

```
cases: 42 | VALUE divergences: 1 | JSON-REPR: 0
唯一 1 例即 §2.1 的空白回归
```

通过项（简表）：

| 输入 | JS | Python | 结论 |
|---|---|---|---|
| `[3.7, 3.7]` | `3.7` (number) | `3.7` | ✅ 类型保留生效 |
| `[0.1, 0.1, 0.2]` | `0.1` | `0.1` | ✅ |
| `[1, "1", "1"]` | `"1"` | `"1"` | ✅ mixed 收敛为 str |
| `["", 5]` | `5` (number) | `5` | ✅ |
| `[1, null, 1]` | `1` | `1` | ✅ |
| `[1e20, 1e20]` | `1e20` (number) | `1e20` | ✅ **brief 担心的分歧不存在**：返回原始值，两端都保留数值，不再经过字符串键 |
| `[NaN, NaN]` | `NaN` (number) | `nan` | ✅ 两端都返回数值型 NaN（JS 侧 `Number.isNaN` 为真，JSON 里都变 `null`，等价） |
| `[Infinity, Infinity]` | `Infinity` | `inf` | ✅ 同上 |
| `[-0, 0]` | `-0` | `0.0` | ✅ 数值相等（`-0` vs `0.0` 仅符号位差异，JSON 都序列化为 `0`） |
| `[1.0, 1]` / `[1, 1.0]` | `1` | `1` | ✅ **顺序无关**，两端一致 |
| `[2, 10]` / `[10, 2]` | `10` / `10` | `10` / `10` | ✅ tie-break 一致 |
| `[Infinity, "info"]` | `"Infinity"` | `"Infinity"` | ✅ 非有限值 tie-break 仍一致 |
| `[NaN, "info"]` | `"NaN"` | `"NaN"` | ✅ |
| `[false, false, false]` | `false` | `False` | ✅ bool 类型保留 |
| `[true, 1]` | `1` | `1` | ✅ bool 被排除出字符串池 |
| `[[1], 1]`（容器） | `1` | `1` | ✅ 容器不投票也不抛异常 |
| 仅容器 / 仅空白 | `NO_MERGE` | `NO_MERGE` | ✅ |

`rcaAggMode` / `rcaStrForMerge` 直接对比：

```
JS    : 2,10→"10"  10,2→"10"  B,B,A,A→"A"  1e20→"100000000000000000000"  NaN→"NaN"  Infinity→"Infinity"  -0→"0"
Python: 2,10→"10"  10,2→"10"  B,B,A,A→"A"  1e20→"100000000000000000000"  NaN→"NaN"  Infinity→"Infinity"  -0→"0"
```

**brief 里担心的 `[1e20,1e20]` / `[NaN,NaN]` 分歧均不存在** —— 原因正是本次改动的正确部分：
返回原始值而不再返回字符串键，`String(1e20)` / `str(1e20)` 的差异被绕开了。

**极长数组**（brief 明确要求）：

```
JS  : 50000 ints -> 0 | 45ms      PY : 50000 ints -> 0 | 43ms
JS  : 50000 distinct padded -> " 0 " | 43ms   PY : ' 0 ' | 45ms
```

无栈溢出、无退化，两端耗时同一量级。

### 3.2 `js/app.js` —— Part B 六项时序（全部通过）

复用 `tests_frontend.js` 自己的 DOM/ctx harness（`buildContext` + `loadAllScripts`，
1-431 行原样提取），驱动**真实的** `js/app.js`：

| # | 场景 | 实测 | 结论 |
|---|---|---|---|
| B1 | **同名文件**：`A.png` → 另一个也叫 `A.png` 的不同图 | 第二次 load 后 `result=null, _resultFileName=null`；重放「旧 A 的 rows + 新 A 的名字」时 `downloads=1, source_file="A.png", rows="FROM_FIRST_A"` | **通过**（有条件）。`handleFile` 的清理是主防线，门禁只按名字比对，理论上同名不同图仍会漏；但要触发必须先让清理失效（第二条上传路径 / 历史恢复），当前代码无此路径 |
| B2 | `state.file=null` 但 `state.result` 存在 | `downloads=0`，正确拒绝 | **通过**。`resetUpload` 同时置空三者（实测 `file=null result=null _resultFileName=null`），**无死锁**：真实流程不会产生该分裂；且即使产生，行为是「拒绝导出」而非「静默写错文件」 |
| B3 | 拖入无效文件（非图片 / 超限） | 非图片：`file=A.png result=KEPT _resultFileName=A.png`；超限：`result=KEPT` | **通过（设计正确）**。`handleFile` 开头的两个 early-return 在清理之前（`js/app.js:733-744`），旧结果与旧文件仍配对，导出门禁照常放行 —— 没有错配。若改成在这里清理，用户拖错文件就会丢掉刚做完的提取，是更糟的行为 |
| B4 | `rcaTableEditDetach()` 后的编辑层脏状态 | swap 后 export/copy/csv 三条路径均 `=0`（正确拒绝）；在 B.png 上重新提取后 `downloads=1, source_file=B.png` | **通过**。detach 后无残留，换图后重新提取一切正常 |
| B5 | `updateActionButtons()` 在 `busy=true` | 提取中换图：`busy=true` 两面板隐藏；提取 settle 后 `busy=false, content.hidden=false empty.hidden=false` | **通过（成员担心的「永久卡死」不成立）**。`setBusy(false)` 走同一个 `updateActionButtons`，`js/app.js:889-899` 的 `finally` 也 token-guarded 地调它，面板必然恢复 |
| B6 | `_resultTexts`/`_resultAttrs` 置 null 后 `rcaRetranslateResult` | 换图后实测 `_resultTexts=null _resultAttrs=null result=null`；源码确认 `js/app.js:1914` 有 `if (!state.result) return;` 早退，`js/app.js:1927` 的 null 快照分支调 `renderCurrentResult()` | **通过**。`state.result` 同时被清空，retranslate 在第一行就返回，不会去 diff 已失效的节点列表；面板此时已是 `updateActionButtons` 设定的空状态，无需重建 |

另外确认**所有上传入口都走 `handleFile`**（`js/app.js:2372` dropzone、`:2376` file input、
`:2397` paste、`:2632` window drop），所以清理没有旁路。

`state.file` 的全部赋值点只有 3 处：`js/app.js:801`（handleFile 内，token 校验后）、
`:2119`（resetUpload，置 null），加初始化 —— 印证 `_resultFileName` 在这两处同步清理是完备的。

### 3.3 Part C —— parity 与 normalizer

```
$ node tests_diff_frontend_parity.js
--- differential parity: 1345 matched, 8 documented, 0 diverged ---
```

**其余 12 个 JS 套件全绿**：

```
tests_aggregate.js                          60 passed, 0 failed
tests_contract_mirror_2026_09_22.js        58 passed, 0 failed
tests_contrast.js                         327 passed, 0 failed
tests_edit_history.js                     334 passed, 0 failed
tests_export_parity.js                    91/91 checks passed
tests_history_fixes_2026_09_21.js          23 passed, 0 failed
tests_names_i18n_fixes_2026_09_22.js      137 passed, 0 failed
tests_sharpen.js                          175 checks passed
tests_ui_fixes_2026_09_22.js               64 passed, 0 failed
tests_ui_fixes_2026_09_27.js               75 passed, 0 failed
tests_viz.js                              161 passed, 0 failed
tests_frontend.js                         846 passed, 0 failed
```

**独立抽查 6 个自建载荷**（fixture 未覆盖的形状）喂给 Python normalizer：

occurrence_matrix + `cell_read="unread"`：
```
occ_unread_only       -> occurrences: []  + warning "dropped 1 row(s) for blank cells..."
occ_unread_mixed      -> occurrences: [A] + warning "dropped 1 row(s)..."
occ_unread_every_row  -> occurrences: []  + warning "dropped 2 row(s)..."
```
带 `cell_read` 的行被当作空 cell 丢弃，但**留下 warning**，不是静默丢失 —— 行为自洽。

generic_table 空 label 列：见 §2.3（单元格丢失，但为 HEAD 既有行为）。

**关于 brief 的关键问题：「`js/minimax.js` 里有没有 occurrence_matrix 的 normalizer？」**

**没有，且这是有意为之的硬拒绝，不是遗漏**：

```
js/minimax.js:3700  const RCA_BROWSER_UNSUPPORTED_MODES = {
                    chemical_stratigraphy: true, paleomap: true,
                    scatter_plot: true, occurrence_matrix: true, generic_table: true };
js/minimax.js:3786  if (RCA_BROWSER_UNSUPPORTED_MODES[mode]) {
                      return { ok: false, errorKey: 'err.mode', warning: ... };
```

`js/minimax.js:1626` 保留了 `occurrence_matrix` 的 root-key 列表，仅用于
`rcaTruncatedWarningIfForeign` 的「不可用载荷」告警（注释明确说明「empty list means never flag,
so a mode missing here would let an unusable payload pass the guard silently」）。

所以浏览器端**不会**把含 `cell_read` 的载荷当 range chart 处理，而是明确失败并提示切到后端。
**该模式不存在两端结果不同的风险** —— 浏览器根本不会产出结果。
注释（`js/minimax.js:3686-3699`）把这条推理写得很清楚，判断正确。

---

## 4. 无法验证的点

| 项 | 原因 |
|---|---|
| **浏览器真实环境行为**（拖放、`Image` 解码、canvas 重绘） | 环境无浏览器。全部 DOM 行为跑在 `tests_frontend.js` 自带的 stub 上，其 `classList.toggle` 实现（`className` 拼接，空格不规范化）与真实 DOM 语义不同；B3/B5 的「面板可见性」结论依赖该 stub 的等价性 |
| **`rcaRetranslateResult` / `switchLang` 的实际执行** | 二者未被 `tests_frontend.js` 导出到测试 ctx（见其 350-370 行的 export 白名单），无法直接调用。B6 只能靠源码断言 + 「`state.result` 已清空」这一事实推证，未跑真实语言切换 |
| **`rcaViz.clear()` / `rcaTableEditDetach()` 的真实副作用** | 二者均由 app.js 的 `typeof` 守卫保护，在 Node stub 环境里 `js/viz.js` 与 table.js 编辑层的行为与浏览器不同。B4 只能验证「导出路径行为正确」，不能验证 canvas 是否真的清干净、编辑层是否真的没有残留监听器 |
| **`file.size` / `file.type` 的真实取值** | B3 用构造的字面量对象模拟 `File`，未走真实 FileReader |
| **§2.3 的 `rows[].label` 恒空** | 我喂的 `columns` 不含行 label 字段，无法区分「normalizer 缺陷」与「我的载荷形状不对」。已按此下调置信度 |
| **多运行提取的端到端网络路径** | 无 LLM/后端服务。B1/B4 的「提取成功」是 stub `extractRangeChart` 注入的 |
| **7000+ 例 fuzz 的统计显著性** | 4000 例由单一 LCG PRNG（固定种子）生成，原子集合是我手选的。绝对数量只用于判断「回归是否系统性」，不作为覆盖率声明。候选修复的 0 新破坏结论同样只在此分布下成立 |

---

## 5. 复现命令

所有脚本在仓库外的 `d:/tmp/rcadv/`，未改动任何源码。

```bash
NODE="C:/Users/Administrator/.workbuddy/binaries/node/versions/22.22.2-6/node.exe"
PY="D:/Program Files/ananconda3/python.exe"

# 三路对照最小复现（HEAD / 当前 JS / Python）
"$NODE" d:/tmp/rcadv/threeway.js
"$PY"   d:/tmp/rcadv/threeway_cmp.py

# 42 例定向用例 + 类型感知比较
"$NODE" d:/tmp/rcadv/adv_js2.js && "$PY" d:/tmp/rcadv/cmp2.py

# 4000 例差分 fuzz，区分「本次回归 / 本次修好 / 既有分歧」
"$NODE" d:/tmp/rcadv/fuzz_js.js
"$NODE" d:/tmp/rcadv/fuzz_js_head.js

# Part B 时序（复用 tests_frontend.js 的 harness）
"$NODE" d:/tmp/rcadv/host_b.js
"$NODE" d:/tmp/rcadv/guard_b2.js
"$NODE" d:/tmp/rcadv/b4.js
"$NODE" d:/tmp/rcadv/b5b6.js

# Part C normalizer 抽查
"$PY" d:/tmp/rcadv/norm_py.py
```
