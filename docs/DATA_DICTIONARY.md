# 导出数据字典

> 适用版本：v0.3.0+。`rca_core/export_notes.py::DATA_DICTIONARY` 是**唯一数据源**，本文件由它人工同步；`result_to_json()` 与 `main.py extract` 都把字典**内联**写进各自产出的 JSON。

## 为什么需要它

CSV / XLSX 里有一列 `confidence`，但它**不是概率**。它是模型对自己答案的自评，本项目没有任何把它与正确率对齐的校准曲线。把它们当"可信度阈值"筛数据去投稿，是科研上会出事的误读。同理 `reason_codes`、`range_base_label`、`not_drawn` 都有精确含义，猜错会改变结论。

## 列释义

| 列 | 含义 | 取值域 / 来源 |
|---|---|---|
| `confidence` | 模型对该行的自评置信度 | 0.0–1.0 浮点或 `null`（未作答）。**不是校准概率，无校准曲线** |
| `reason_codes` | 覆盖台账里某格为何未答 / 存疑 / 明确未画 | 稳定 slug，见 `js/reason-codes.js` 的 `RCA_REASON_CODES`；由 `rca_core/reason_codes.py` 生成 |
| `range_base_label` | 该延限**底界**在图上的原始地层标签 | 自由文本；保留图上拼写，不要替换成标准阶名 |
| `range_top_label` | 该延限**顶界**在图上的原始地层标签 | 自由文本 |
| `not_drawn` | 模型看过该格但**没有画**（明确不存在），不是漏读 | 布尔 / 计数；与 `gap`（根本没问）和实存分布三者互异。⚠️ v0.1.4 曾把它当实存发布 |
| `image_sha256` | 原图字节的 SHA-256 | 64 位小写十六进制；图片离开本机前本地计算 |
| `ics_version` | 年代换算所用国际地层表版本 | 当前 `ICS v2024/12`；论文引用请写明具体版本 |
| `agreement_count` | 多轮合并中有几轮同意该行 | 整数 ≤ `total_runs`；由 `rca_core/aggregate.py` 产生 |
| `edit_count` | 人工复核改动了多少格 | 非负整数；来自可编辑结果 / 历史记录 |

## 各导出格式携带说明的方式

| 格式 | 方式 |
|---|---|
| XLSX | `provenance` 工作表里的 `confidence_note` 行 |
| JSON（`result_to_json`） | 顶层 `confidence_note` + `data_dictionary` |
| `main.py extract` | 每个 `<stem>.json` 顶层同样内联 `confidence_note` + `data_dictionary`，**一张图一个文件**，无 sidecar |
| WPD | bundle 元数据（见 `to_wpd`） |
| CSV / TSV | **字节保持不变**。这两个格式被 `tests/test_export_render_parity.js` 逐字节钉死（含尾部 CRLF），加注释行会破坏下游解析器。列语义由本文件提供（文档级 sidecar） |

## 版本与溯源

每条导出都应能回答三件事：哪张图（`image_sha256`）、哪套提示词与年代标尺（`prompt_version` / `ics_version`）、哪次人工修订（`edit_count`）。缺任一项时请把它当作**未校验数据**，不要直接进论文附表。

若你用的是 **v0.1.4 或更早版本导出的 PBDB 数据**，请重新导出：`range_base_label` / `not_drawn` 分流在那些版本里缺失，会把整个 section 的年龄区间当成每个分类单元的 FAD/LAD。
