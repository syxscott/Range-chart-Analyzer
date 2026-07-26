# Phylogenetic Tree Extraction — Design Specification

> **Date:** 2026-07-26
> **Feature:** 放射虫系统发育树提取与可视化

---

## Goal

支持从学术论文中的放射虫分子系统发育树图片提取结构化 JSON 数据，并在 GUI 中以可交互的 D3.js 树形图展示。

---

## Output JSON Schema

```json
{
  "version": "1",
  "metadata": {
    "taxon_group": "Radiolaria",
    "extraction_timestamp": "2026-07-26THH:MM:SS",
    "image_source": "<filename or path>",
    "total_nodes": 42,
    "root_name": "Spasmaria"
  },
  "nodes": [
    {
      "id": "n0",
      "name": "Spasmaria",
      "support": null,
      "support_confidence": null,
      "branch_length": null,
      "depth_range_m": null,
      "depth_confidence": null,
      "sequence_count": null,
      "is_leaf": false,
      "parent": null
    },
    {
      "id": "n1",
      "name": "Polycystinea",
      "support": 89,
      "support_confidence": 0.95,
      "branch_length": 0.12,
      "depth_range_m": "0-200",
      "depth_confidence": 0.88,
      "sequence_count": 15,
      "is_leaf": false,
      "parent": "n0"
    }
  ],
  "root_ids": ["n0"],
  "legend": {
    "depth_colors": {
      "epipelagic":   {"hex": "#003366", "depth_m": "0-200"},
      "mesopelagic":  {"hex": "#00BFFF", "depth_m": "200-1000"},
      "bathypelagic": {"hex": "#90EE90", "depth_m": "1000-4000"}
    },
    "sequence_count_min": 1,
    "sequence_count_max": 47
  }
}
```

**字段说明：**

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | string | 节点唯一标识（n0, n1, ...） |
| `name` | string | 分类群名称（拉丁学名） |
| `support` | float\|null | 支持率数值（BS值/后验概率） |
| `support_confidence` | float\|null | 支持率读取置信度 [0, 1] |
| `branch_length` | float\|null | 分支长度（进化距离） |
| `depth_range_m` | string\|null | 水层深度范围，如 "0-200" |
| `depth_confidence` | float\|null | 颜色→深度映射置信度 [0, 1] |
| `sequence_count` | int\|null | 该节点代表的序列数量（图例圆圈大小） |
| `is_leaf` | bool | 是否为叶节点（终端分类群） |
| `parent` | string\|null | 父节点 id，根节点为 null |

---

## Architecture

```
用户上传图片
    │
    ▼
extract_phylogenetic_tree()     ← 新增，rca_core/extractor.py
    │  progress_callback 透传进度信号
    ▼
call_llm_api(..., progress_callback)  ← 已有，llm.py，透传
    │
    ▼
_post_json()  →  "submitting" → "uploading" → "thinking"
    │
    ▼
LLM API 返回原始文本
    │
    ▼
safe_json_loads() → ExtractResult(data=<上述JSON>)
    │
    ▼
merge_results()  ← 新增 PHYLOGENETIC_TREE_SCHEMA，aggregate.py
    │
    ▼
GUI: D3.js TreeWidget  ← 新增，WebEngineView + D3.js
    │
    ▼
导出 JSON / SVG
```

---

## Module Map

| 文件 | 改动类型 | 说明 |
|---|---|---|
| `rca_core/extractor.py` | 修改 | `_MODE_DISPATCH` 新增 `phylogenetic_tree` 条目；新增 `extract_phylogenetic_tree()` |
| `rca_core/prompt.py` | 修改 | 新增 `PHYLOGENETIC_TREE_SYSTEM_PROMPT` |
| `rca_core/aggregate.py` | 修改 | 新增 `PHYLOGENETIC_TREE_SCHEMA`；`SCHEMA_BY_MODE` 新增条目 |
| `gui_fluent.py` | 修改 | 下拉框新增选项；`ExtractWorker` 透传 `progress_callback`；新增树渲染组件 |
| `rca_core/i18n.py` | 修改 | 新增相关翻译 key |

---

## Stages（进度条复用）

| Stage | Signal | 说明 |
|---|---|---|
| `submitting` | HTTP 请求构建 | 正在提交请求…… |
| `uploading` | 图像上传中 | 正在上传图像…… |
| `thinking` | 等待模型响应 | 正在分析图像，请稍候…… |
| `parsing` | 结果解析 | 正在解析结果…… |

---

## D3.js Tree Widget

- **布局**: `d3.tree()`，横向（左右展开）或纵向（上下展开）
- **节点颜色**: 按 `depth_range_m` 映射水层颜色（同原图右侧彩圈）
- **节点大小**: 按 `sequence_count` 映射圆圈半径
- **支持率标签**: 节点旁边显示，< 70 时标红
- **悬停浮窗**: hover 显示完整信息（置信度、序列数、深度等）
- **导出 SVG**: 用户可右键保存为矢量图

---

## Non-Goals（不做什么）

- 不支持 Newick 格式导出（用户选 B）
- 不做节点编辑（只读展示）
- 不修改任何现有函数签名（向后兼容）

---

## Backward Compatibility

- `call_llm_api()` 新增 `progress_callback=None` 参数，不传则行为完全不变
- `_MODE_DISPATCH` 只新增 key，不修改现有 key
- `MergeSchema` 只新增，不修改现有 schema
- GUI 树渲染组件完全独立于现有表格渲染代码
