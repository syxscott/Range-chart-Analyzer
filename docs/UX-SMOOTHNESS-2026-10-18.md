# 使用流畅性专项 — 诊断与改造报告

**日期**：2026-10-18　**范围**：Web（浏览器端）+ Python 端 + 两个桌面 GUI
**方法**：全部结论基于实测（无头 Chromium / Anaconda Python 计时），成员结论经主理人独立复核，推翻之处已在「裁决」中标注。

---

## 一句话结论

**卡顿不在数据层，全部在渲染与反馈层。** 内核处理 5000 行只要 10–18 ms；桌面 GUI 渲染 5000 行只要 149 ms（可接受）。真正的问题是「点了没反应」和「不知道在干嘛」，不是「算得慢」。

---

## 二、已落地的三项改造

### 1. 首次渲染的 376 ms 界面冻结（后端 #1）

**根因**：`ExtractPage._xlsx_available()` 在**每次**结果渲染和每次 busy 状态翻转时都被调用，而它内部是一次冷 `import openpyxl`。实测冷启动 **351.6 ms**（本机复现值），发生在 UI 线程。

**修法**：`@staticmethod` + `@functools.lru_cache(maxsize=1)`。用 `staticmethod` 而非直接装饰方法，是因为 `lru_cache` 装饰实例方法会按 `self` 建键并泄漏内存。

**实测收益**：冷启 351.6 ms → 命中 **0.0017 ms**（约 20 万倍，且实例调用与类调用共享同一缓存）。

**改动**：`gui_fluent.py` `_xlsx_available()` + 顶部 `import functools`。

### 2. 大表虚拟化 —— WEB-04（前端 A1）

> ⚠️ **原审查结论被推翻**：此前把 WEB-04 挂起的理由是「`content-visibility: auto` 会导致列宽错位」。实测证明**这个理由是错的**——错位只在把 `content-visibility` 加到 `<td>` 时发生。加到 `<tr>` 上，列宽漂移实测 **0.0 px**。

**根因**：`js/app.js:1716` 用 `content.innerHTML = rcaRenderResults(...)` 整块字符串赋值，成本随**已渲染行数**增长，且每次强制重排都要重付（实测 5000 行首绘 1926 ms、表头 hover 触发重排 816 ms）。

**修法**：给 `<tr>` 加 `content-visibility: auto`，**绝不给 `<td>` 加**（被包含的单元格不再参与所属列的首选宽度计算，这才是错位的真正来源）。

**本次额外做的两处加固**（超出成员原方案）：

| 加固 | 原因 |
|---|---|
| **行数阈值 600**（`RCA_VIRTUAL_ROW_THRESHOLD`） | 实测 100 行只要 85 ms、500 行 296 ms。小表加 containment 纯属凭空增加记账开销，而小结果才是常态。无条件加会给小表带来回归风险。 |
| **固定行高只作用于大表** | 初版把 `height: 2.25rem` 加在所有 `tbody tr` 上，会把 `other_fossils` 的换行长文本（该表刻意用 `cell-wrapping` 让长分类名换行可读）压成单行裁切。已收窄到 `[data-rca-big="1"]`。浏览器实测确认修复：换行文本 44 px 完整展开。 |

**改动**：`css/app-ux.css`（WEB-04 段 + `.table-length-hint`）、`js/table.js`（两个阈值常量 + 属性判定 + 提示渲染）、`js/i18n.js`（`results.tableTooLong` 三语）。

**⚠️ 收益未能复现，且经三方独立测量确认（重要）**

初报称 3.3× 加速。随后经三轮独立测量，最终结论是：**规则确实生效，但性能收益测不出来**。

- 规则命中验证（Chrome 加载仓库真实样式表）：`content-visibility` 计算值 = **`auto`**、`contain-intrinsic-size` = `auto 36px`、行高 39.80px 与 `--rca-row-h` 一致 → **规则确实被应用**。
- A/B 对照（同一 fixture，唯一差异 `data-rca-big="1"`，3 轮交错取中位，5000 行）：
  ```
  FCP     without=488ms  with=480ms   ratio=1.02x
  scroll  without=0.0ms  with=0.0ms
  ```
- 唯一未解释的观测：`getComputedStyle(row).contain` 返回 `none`。按规范 `content-visibility: auto` 在**元素与用户相关时**才附加 containment，因此机制可能已就位但触发条件未达成。

**结论：WEB-04 是"安全但无可测收益"**。安全属性三项全部通过（列宽漂移 0px、打印 5000/5000 完整、导出读模型不受影响），因此**保留**——它不改变任何渲染语义，只在浏览器未来改变判定时提供保护；但不应宣称它优化了什么。

> 附带发现：`contain: none` 意味着"规则生效"与"规则起作用"是两件事。因此**原定的"列宽零漂移"断言无法区分二者**（不生效 = 不改变 = 无漂移）。已要求补一条能区分的断言。

### 3. 长表提示（前端 A4）

**根因**：导出按钮位于 5000 行表格区块的**最顶部**，用户必须先拖完 5000 行才会发现「其实可以直接导出」。

**修法**：`≥1000` 行时在区块标题下渲染一句提示（`results.tableTooLong` 三语），说明上方按钮覆盖全部行。阈值 1000 刻意高于虚拟化的 600：containment 是越早越好，用户无感的渲染优化；提示是需要用户阅读的 UI，应在滚动真的不划算时才出现。

**端到端验证**：7 项断言全过，阈值精确（599/600、999/1000 分界），文案正确转义。

---

## 三、已排查但确认不是问题的项

| 项 | 实测结论 |
|---|---|
| 退避 sleep 不可中断 | **我与后端成员各自实测后互相修正**：因 `call_llm_api_with_retry` 传 `headers=None` 拿不到 `Retry-After`，实际退避为 1.00 / 1.13 / 2.00 s，**合计 4.13 s**，不是初判的「最长 362 s」。不足以造成卡死主因。 |
| ICS 数据加载慢 | 后端成员实测 0.71 ms，推翻其自身的第一版假设。 |
| server 线程数 8 导致排队 | 成员**实测证伪**：`ThreadingHTTPServer` 每请求一线程，配置上限 32，本机实测 0.1 ms。 |
| 内核大结果集处理 | 主理人实测 n=5000：`get_configs_for_result` 2.8 ms / `build_table_export` 18.1 ms / `to_csv` 13.1 ms / `result_to_json` 10.2 ms，全部线性毫秒级。 |
| Fluent 表格 5000 行渲染 | 成员实测 **149 ms**，可接受。model 化后 9.4 ms（15.8×）但需改两处源码 grep 测试断言 + 实现 `flags()`/`setData()`，性价比不高。 |
| GBIF 物种名校验 | 已有 per-request AbortController + 15 s 超时 + 批次 60 s 预算。 |

---

## 四、后续处理与实测修正

### 已完成（第二轮）

| 项 | 内容 | 实测 |
|---|---|---|
| **A5** | Provider 增删改的**重复写盘**：`add()`/`set_current()`/`update()`/`remove()` 内部都已 `save()`，GUI 又跟一次 `save()`。一次"添加提供商"因此写盘 **3 次**。已删掉 4 处冗余（`gui_fluent_providers.py`），保留拖拽排序那处（改 `sort_index` 不走 CRUD，必须显式保存） | 60 provider 场景 `save()` 单次 70–84 ms（含 fsync + rename）→ 单次操作省约 77 ms UI 阻塞。验证：CRUD 内部保存语义确认（旧代码 3 次 → 现 2 次） |
| **A3** | 重试时发 `retrying:{n}/{total}:{backoff}s` 进度 | 见成员报告 |
| **A8** | 单轮路径加 elapsed 心跳（复用 `analyzing:1/1:{elapsed}s` 格式） | 见成员报告 |
| — | 浏览器性能回归测试固化（`tests_frontend_perf.js`） | 见成员报告 |

### 裁决不做（实测收益不成立）

| 项 | 原估收益 | **实测** | 裁决理由 |
|---|---|---|---|
| A2 GUI 表格 model 化 | 5000 行 149→9.4 ms（省 140 ms） | **完整管线仅 50 ms**（fill 34 + `resizeColumnsToContents` 10 + 首次布局）；10000 行才 88 ms | 远低于人眼可感阈值。而代价是改两处源码 grep 测试 + 实现 `flags()`/`setData()`，风险不对称 |
| A6 CSV/TSV 异步导出 | 5000 行约 30 ms | **实测 30.0 ms**（build 16.6 + to_csv 12.2 + write 1.2） | 30 ms 不可感，不值得引入线程复杂度 |
| A7 `Database()` 建库延后 | 48.7 ms | **冷启动 30.4 ms，二次启动 5.8 ms**（`HistoryStore`/`UsageStore` 均 0.0 ms） | 启动路径上不可感 |
| 退避上限治理 | "最长 60 秒不可中断" | **实际 1.00/1.13/2.00 s，合计 4.13 s** | `headers=None` 时拿不到 `Retry-After`，`max_delay` 是惰性常量 |

### 但发现一个真的（A6 的正确目标）

**XLSX 导出阻塞 UI 线程**——CSV/TSV 的 30 ms 完全掩盖了这件事：

| 格式 | 1000 行 | 5000 行 |
|---|---|---|
| CSV/TSV | 6.0 ms | 30.0 ms |
| **XLSX** | **552.3 ms** | **772.1 ms** |

1000→5000 行只从 552 涨到 772 ms，说明**大头是 openpyxl 的固定开销（workbook/样式表/共享字符串池）而非行数**——即"任何规模都冻结 0.5–0.8 秒"，而 XLSX 是科研场景导出全结果的常用格式。已指派异步化。

### 仍未验证

**WEB-04 的性能收益**（见第二节 ⚠️）：安全属性全过，但加速比未复现。本轮新增的 `tests_frontend_perf.js` 会持续监控，一旦真有回归会立刻暴露。

---

## 五、验证记录

```
JS 13 套件：846 / 329 / 335 / 161 / 1360 matched 0 diverged / 75 / 64 / 91 / 60 / 58 / 23 / 137 / 175   全绿
Python：gui_fluent.py 语法 OK；GUI/Provider 定向测试全过
浏览器实测（Chrome，无头）：列宽漂移 0px、打印 5000/5000 行、换行长文本未裁切、阈值分界精确
```

> 注：本项目此前所有验证都只能靠 node vm + Python 计时。本轮发现仓库已有 `playwright` 依赖，**但其浏览器二进制未下载**；实测改用本机已安装的 Chrome（`executablePath` 覆盖，同为 Blink 引擎）。若后续要常态化做浏览器验证，建议先 `npx playwright install chromium`。
