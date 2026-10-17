# 对抗审查报告 — 2026-10-17（对当日修复的复检）

对象：`docs/CODE-REVIEW-2026-10-17.md` 所列 15 项缺陷的**修复本身**。
方法：修复后测试全绿，因此「测试没红」被当作无信息量；改为对每处改动构造**修复前会通过、修复后可能出错**的输入，实测两端（Python / Node）行为，必要时临时注入回归验证测试有效性。

结论：**发现 4 个真问题，全部已修**（其中 2 个是我在上一轮亲手引入的，A1 与 A4 是同一处代码的三轮返工）。另 5 项经实测排除。

审查由两个独立的 fresh 审查者分别负责 Python 端与 JS 端，加上主审自己的实测与变异验证。**A4 是审查者报告、主审实测确认的**——它没有出现在主审的第一轮自查里，原因是那个输入形状（同一 merge 字段跨 run 同时有 padded 与 unpadded）差分 fixture 恰好一个都没有。

---

## 一、发现并修复的问题

### A1 [P1] `js/aggregate.js` — 类型保留改动在 JS 侧引入顺序依赖与 parity 破缺

**发现经过**：上一轮我同步 JS 端时，把「返回原始值」写成「按 `String(v).trim()` 反查第一个原始值」。实测两端：

| 输入 | 修复前（两端一致） | 我改的 Python | 我改的 JS | 问题 |
|---|---|---|---|---|
| `[1.0, 1]` | `"1"` / `"1"` | `1.0` (float) | `1` (int) | **两端分歧** |
| `[1, 1.0]` | `"1"` / `"1"` | `1` (int) | `1` | Python 侧**顺序依赖** |
| `[NaN, NaN]` | `"NaN"` / `"NaN"` | `nan` | `null` | JS 侧**值被吞** |
| `[Infinity]` | `"Infinity"` / `"Infinity"` | `inf` | `null` | 同上 |

**根因**：`String(v).trim()` **不是投票键**。真正的键是 `rcaStrForMerge`（它把积分浮点归一为 `"1"`、非有限值归一为 `"NaN"`）。用未规范化的键反查，积分浮点和非有限值永远匹配不上。

**为什么差分测试没抓到**：1340 个 fixture 用例里全部一致，`tests_diff_frontend_parity.js` 报 `0 diverged`。而且 `[1.0, 1]` **本来就在 fixture 里**——它当时通过，恰恰因为两端都还返回投票键。

**修复**：`rcaAggMode` 本身就持有 `representative` map 并返回原始值，我那层反查是多余的。删掉后函数只剩「收集原始值 → 交给 `rcaAggMode`」：

```js
const pool = [];
...
  pool.push(v);                       // ORIGINAL value, not its key
...
const winning = rcaAggMode(pool);
return (sawText && sawNumber) ? String(winning) : winning;
```

**修复后实测**（两端一致）：

```
3.7,3.7       -> 3.7      /  3.7
1,'1','1'     -> "1"      /  "1"
NaN           -> NaN      /  NaN      (真实数值，非 null)
Infinity      -> Infinity /  Infinity
1.0,1         -> 1.0      /  1        ← JSON 不可区分，见下
2,10          -> 10       /  10
```

**残留差异的定性**（不是 bug）：
- `1.0` vs `1`：JSON 无 int/float 之分，`JSON.stringify({a:1.0})` 与 `{b:1}` 字节相同，只在 Python 内存态可观测。
- `1e20` / `1e-7` 的字符串化：既有两端差异，`rcaStrForMerge` 上方注释已文档化，且投票键两端一致，不影响合并决策。

> **本节在同一天被二次修正，见 A4。** 上面这个「修复后」状态仍然丢了一个 trim，
> 是在 A4 里由第二个审查者发现的。保留此处原样是因为它记录了真实的修复轨迹：
> 同一个分支被改了三轮才正确，每一轮的错误都不一样。

### A4 [P0] `js/aggregate.js` — 类型保留同时丢掉了 trim（审查者发现，已修）

**这一条比 A1 更隐蔽，且是 A1 的直接后果。** 第二个审查者（`js-adversary`，独立于 A1 的调查）报告，经我实测确认属实：

| 输入 | 修复前（两端一致） | A1「修复后」Python | A1「修复后」JS |
|---|---|---|---|
| `["Ml", " Ml "]` | `"Ml"` / `"Ml"` | `"Ml"` | `" Ml "` |
| `[" 7 ", 7]` | `"7"` / `"7"` | `"7"` | `" 7 "` |
| `["-1", " -1 "]` | `"-1"` / `"-1"` | `"-1"` | `" -1 "` |

**根因**：`_merge_scalar_field` 用 `s = _str_for_merge(v).strip()` 建投票池，**trim 后的字符串才参与投票**。我把 `pool.push(v)` 推的是原始值，于是 `"Ml"` 与 `" Ml "` 成了两个候选、平票、排序破平取到带空格的那个。`rcaAggMode` 的键来自 `rcaStrForMerge(v)`，**它不 trim**——所以修法不是让 `rcaAggMode` trim，而是让**投票键** trim。

**为什么差分测试完全没抓到**：fixture 里本来就有 23 处带空白字符串，但**没有一个**让同一 merge 字段跨 run 同时出现 padded + unpadded。触发路径存在，触发条件不存在。最接近的 `range_base: [" 7 ","7"]` 用例走的是 `rcaAggNorm`（那条路径本来就 trim），两端一致。**绿是真的绿，但绿在别的分支上。**

**修复过程中的两次错误尝试**（都留下了痕迹，因为它们揭示了 Python 规则的精细程度）：

1. **第一次修正：只把字符串 trim 后入池。** 结果 `[" a "," a"]` 两端分歧——Python 给 `" a "`，我给 `"a"`。因为 Python 平票时返回的是**代表值（未 trim 的原值）**，不是 trim 后的键。
2. **第二次修正：改存 `(trimmedKey, original)` 对，但把类型判断放在循环内。** 结果 `[" 7 ", 7]` 两端分歧——Python 在 `saw_text and saw_number` 时**提前返回 winning key**（字符串），只有类型一致才返回原值。判断的位置必须与 Python 一致。

**最终形态**（与 Python 的 `votes` 列表逐行等价）：

```js
const key = rcaStrForMerge(v).trim();   // Python: _str_for_merge(v).strip()
if (!key) continue;
keys.push(key);  votes.push([key, v]);  // 键投票、代表值保值
...
const winning = rcaAggMode(keys);
if (sawText && sawNumber) return winning;      // 位置与 Python 相同
for (const [key, original] of votes) {
  if (key === winning) return original;
}
```

**修复后两端逐行一致**（15 个边界全部对齐）：

```
[" a "," a"]  -> " a "    [" 7 ","7"]  -> " 7 "
["\tx","\tx"] -> "\tx"    [" 7 ",7]    -> "7"
["  "," "]    -> NO_MERGE ["7",7]      -> "7"
["x"," x ","x"]-> "x"     [3.7,3.7]    -> 3.7
["Ml"," Ml "] -> "Ml"     ["-1"," -1 "]-> "-1"
                           [" 2 ",2]    -> "2"
```

**fixture 已补 15 个 trim 用例**（1353 → 1368），**变异测试确认有效**：把 `key` 改回不 trim，差分立刻报 `FAIL agscal_318`（`py="0" js=" 0 "`）。

### A2 [P1] `rca_core/bed_parser.py` — 区间判定误杀真实测井标签

P2-2 的两条新规则写得太宽，把放射虫测井图的标准标注当成区间/无效：

| 标签 | 修复前 | 修复后（**回归**） | 应为 |
|---|---|---|---|
| `87 B1-4C` | `4` | `None` | **4** |
| `87 JED 8` | `8` | `None` | **8** |
| `CAASP-84-20/15` | `15` | `None` | None（15 是编号不是位置，`None` 正确） |

**后果是静默且彻底的**：`label_ordinal` 的两个消费方——导出唯一的**阻断性**不变量 `range_base_le_range_top`，以及 extractor 的端点交换启发式——对这一整类图全部停摆。一个从不运行的检查不报任何错，看起来干净。

**两个根因**：
1. `_ASCII_RANGE_RE` 允许两数字之间夹任意字母，于是 `87` 与 `4` 之间的 `B1-` 被当成区间中点。
2. `_has_foreign_semantic_word` 用的是**开放式**规则（"任何不在位置词表里的词都算外来词"），于是井名 `JED`、机构缩写 `CAASP` 被判为"这个数字另有含义"。

**修复**：
1. 区间要求两数字相邻（可选同一标记词），另加一条**同前缀**模式处理 `A1-A5`（区间写法，与复合编号的区别在于连字符两侧前缀相同）。
2. 外来词改为**封闭清单** `_NON_POSITION_WORDS`（zone/fig/section/formation/stage/plate…）。未收录的词一律视为标识符——这是保守方向：漏检不报，误判才制造假倒置。

**修复后实测**：`87 B1-4C`→4、`87 JED 8`→8、`Bed 3-4`→None、`A1-A5`→None、`23-25`→None、`zone Z1`→None、`Zone 3A`→None，全部正确。

### A3 [P2] 差分 fixture 无法承载非有限值 — 该盲区当时无人看守

`tests/gen_frontend_parity_fixtures.py` 拒绝写入合并值为非有限的用例（JSON.parse 拒绝裸 `NaN`/`Infinity`），这是正确设计。但正因如此，**NaN / Infinity 永久在差分体系之外**——而 A1 破坏的恰恰是它们。

已新增 `tests/test_aggregate_scalar_type_2026_10_17.py`（15 条）直接对两端探测这些输入，并用 JS 源文本传参（`repr(float('nan'))` 是 `nan`，JS 需要 `NaN`）。同时给 fixture 补了 13 个可承载的边界用例，加上 A4 的 15 个 trim 用例，共 1340 → 1368。

---

## 二、经实测排除的疑点

| 疑点 | 排除依据 |
|---|---|
| `_NON_CONTENT_KEYS` 漏掉 `_extras` / `_array_root` 等 | `_extras` **刻意排除**：外来键进 `_extras` 本身就是内容信号，`tests/test_frontend_parity_fixtures_2026_09_20.py::test_foreign_other_modes_are_not_flagged` 钉死了这一点。第一版把它也排除 → 12 个测试红，已回滚 |
| `cell_read="unread"` 在浏览器端被丢弃造成 parity 分裂 | **不成立**。`occurrence_matrix` 与 `generic_table` 同在两端 `BROWSER_UNSUPPORTED_MODES`（`js/minimax.js:3700` 与 `rca_core/modes.py` 列表完全一致），浏览器根本不走这条路径 |
| `cell_read` 在 merge / export 中丢失 | 实测：merge 后仍在；导出时 code 列为空且该行保留，语义正确（存在但未读出，不是空白） |
| 换图清理放在 token 校验前会清掉新图结果 | 代码确认在 `if (myToken !== state.loadToken) return;` **之后**，且 `tests_frontend.js` 有专门用例 `p15-late-superseded-load-kept-the-result` |
| 同名文件（A.png → A.png）导出门禁失效 | **正常路径不可达**：`handleFile` 已把 `state.result` 置 null，门禁第一句即拦截；文件名比对只是第二道防线 |
| 拖入非图片/超大文件后旧结果被清 | **正确行为**：`handleFile` 开头 early return，`state.file` 未变，图还是原来那张 |
| `is_loopback_bind("")` 改动影响 `populate_expected_hosts` | 两者各有独立判断，无耦合 |
| 畸形 endpoint 的 `except Exception` 掩盖编程错误 | **有意的取舍**，注释已写明理由；且 `ssrf` 层已先行把这类输入转成正常拒绝，兜底只是第二道 |

---

## 三、测试有效性验证（变异测试）

新写的测试必须能抓到它要防的 bug，否则只是装饰。逐个注入回归：

| 注入的回归 | 应变红的机制 | 结果 |
|---|---|---|
| JS 端退回「返回投票键」 | 新探针测试 + `tests_diff_frontend_parity.js` | **两者都红** ✅ |
| bed_parser 退回开放式外来词规则 | `tests/test_bed_parser.py` | **2 条红** ✅ |

第一轮变异我注入的是「等价写法」（`rcaAggMode(pool.map(rcaStrForMerge))`），行为与正确版相同，测试**没有变红**——这说明变异本身无效，不是测试无效。改成真正的语义回归（返回投票键而非原始值）后两者都红。

---

## 四、最终验证

| 项 | 结果 |
|---|---|
| pytest | 3959+ 收集，**0 失败**，exit 0 |
| `tests_diff_frontend_parity.js` | **1360 matched, 8 documented, 0 diverged**（用例 1340 → 1368） |
| `tests_frontend.js` | 846 passed / 0 failed |
| 其余 11 个 JS 套件 | 全部 0 失败 |
| `ruff --select E9,F63,F7,F82,F821,F841` | 仅 `server.py:1862 host_hdr`（既有、代码注释说明是刻意的安全意图标记） |

---

## 五、这一轮的方法教训

1. **同步 parity 的两端，比回退更容易，也更对**——A1 里我第一反应是回退成员（或我自己）的改动，实际补齐另一端才是正解。
2. **「测试全绿」对「修复是否正确」零信息量**。A2 的两个误杀、A1 的三个分歧、A4 的三类分歧，全部在测试全绿的状态下存在。
3. **fixture 会锁定契约，也会锁定遗漏**。A3 暴露差分体系有一个结构性盲区（JSON 承载不了非有限值）；A4 暴露另一个：fixture 有带空白字符串，却没有一个让同一 merge 字段跨 run 同时出现 padded + unpadded。**触发路径存在 ≠ 触发条件存在**，前者能通过不代表后者测过。
4. **变异测试是检验「测试有没有用」的唯一手段**。写完回归测试不验证它能否变红，等于没写。本轮两次变异：A1 的第一轮变异是「等价写法」，测试**没变红**——那是变异无效而非测试无效；改成真回归后两套机制都红。A4 的变异（去掉 trim）立刻报 `FAIL agscal_318`。
5. **修法要按真实数据取样**。A2 的两条规则是照着几个虚构标签写的；换成真实测井标签（`87 B1-4C`、`87 JED 8`）立刻暴露。
6. **跨语言复刻一个函数，要逐行对照对方的控制流，不是逐句抄**。A4 的三次返工分别错在：键函数用错、代表值取错、类型判断位置错。三次都是「看起来等价」，三次都要靠实测的对照表才能看出来。Python 的 `_merge_scalar_field` 里那句 `if saw_text and saw_number: return winning` **必须**在代表值查找之前——这个顺序在代码里看不出重要性，但换到 JS 就是 `[" 7 ", 7]` 两端给不同答案。
7. **自己的修复需要第二双眼睛。** A1 与 A4 是同一处代码被我改了三轮才正确，而 A4 的形状在我第一轮自查的输入集里根本不存在。审查者报告 + 主审实测确认的组合，比单人自查更可靠——但前提是**采信前必须自己复现**，本轮 A4 就是我先跑了两端对照才认的。

