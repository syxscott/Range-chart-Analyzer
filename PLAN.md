# 重构计划：Range Chart Analyzer（纯前端 Web）

将 RLPE 项目中"识别 + 提取地层沿线图（range chart）"的能力，抽离并重构为一个**独立、纯前端**的 Web 应用。

- **源项目**：`D:\GIthub\RLPE-Radiolarian-Plate-Extractor`
- **目标目录**：`D:\GIthub\Range-chart Analyzer`（已存在，空）
- **架构决策**（已与用户确认）：
  1. **纯静态站 + 可选代理开关** —— 默认浏览器直连 MiniMax；界面提供可选"代理地址"输入框，CORS 不通时用户填入自建轻量代理即可。核心保持零后端。
  2. **多文件原生 HTML / CSS / JS** —— 沿用源项目风格（`index.html` + `css/style.css` + `js/app.js`），零构建、零依赖，复用现有 CSS 设计 tokens。

---

## 需求重述

用户在浏览器里：
1. 填入自己的 MiniMax API Key（存在本地，不经任何服务器）；
2. 上传一张地层沿线图图片（PNG/JPG）；
3. 浏览器把图片 base64 编码后直接 POST 到 MiniMax `/v1/messages`（Anthropic 兼容接口）；
4. 解析返回的严格 JSON，在页面上以**表格**形式展示：剖面（sections）、种属延限（species_ranges）、生物带（biozones）、其他化石（other_fossils）；
5. 用户可**复制**（TSV，粘进 Excel）或**下载**（CSV / JSON）表格。

**移植范围**：仅 `range_chart_extractor.py` 中 `extract_range_chart` 这一条链路 + 其 System Prompt + JSON 容错解析。**不移植**面板关联（`build_geology_links_for_panels`）、图类型分类（用户已明确上传的就是 range chart）、PDF/OCR/图版分割等 Python 专属逻辑。

---

## 从源项目精确复用的资产

| 资产 | 源位置 | 处理方式 |
|------|--------|----------|
| System Prompt | `range_chart_extractor.py:435` `_RANGE_CHART_SYSTEM_PROMPT` | **逐字复制**为 JS 字符串常量 |
| JSON 容错解析 | `range_chart_extractor.py:368-432` `_extract_balanced_json_object` + `_safe_json_loads` | **移植为 JS**（括号深度扫描，~40 行） |
| 请求结构 | `range_chart_extractor.py:554-583` | headers: `x-api-key`, `anthropic-version: 2023-06-01`, `content-type`；body: `model / max_tokens:4000 / system / messages[image+text]` |
| 结果字段结构 | `RangeChartSection / SpeciesRange / BiozoneRecord` dataclasses | 映射为 JS 表格列定义 |
| API 默认值 | endpoint `https://api.minimaxi.com/anthropic`、model `MiniMax-M3` | 作为默认值填入设置 |
| 视觉设计 | `web/css/style.css` `:root` 设计 tokens（颜色/圆角/阴影/字体） | 复用 tokens，精简掉无关样式 |

---

## 目标目录结构

```
D:\GIthub\Range-chart Analyzer\
├── index.html              # 单页：上传 + 设置 + 结果表格
├── css/
│   └── style.css           # 复用 RLPE 设计 tokens，精简版
├── js/
│   ├── config.js           # 默认 endpoint/model、localStorage 读写封装
│   ├── prompt.js           # RANGE_CHART_SYSTEM_PROMPT 常量（逐字移植）
│   ├── json-utils.js       # _safeJsonLoads / _extractBalancedJsonObject（移植）
│   ├── minimax.js          # extractRangeChart()：fetch MiniMax，超时+错误处理
│   ├── table.js            # 结果 → HTML 表格渲染
│   ├── export.js           # 复制 TSV / 下载 CSV / 下载 JSON
│   └── app.js              # 事件绑定、流程编排、状态管理
├── proxy/
│   ├── cloudflare-worker.js # 可选：~20 行 CORS 透明代理
│   └── README.md            # 代理部署说明（Cloudflare/Vercel/Deno）
└── README.md               # 使用说明 + CORS 说明 + 安全提示
```

---

## 实施阶段

### Phase 1 — 项目骨架与静态页面
- 创建目录结构与 `index.html`
- 三个区块：① 设置卡片（API Key / endpoint / model / 可选代理地址，带 localStorage 记忆）；② 上传区（拖拽 + 点击选择 + 图片预览 + 客户端缩放压缩）；③ 结果区（占位）
- 从 `web/css/style.css` 提取设计 tokens，写精简版 `css/style.css`
- 中文界面，沿用 RLPE 视觉风格

### Phase 2 — 核心提取逻辑移植（无 UI）
- `prompt.js`：逐字移植 `_RANGE_CHART_SYSTEM_PROMPT`
- `json-utils.js`：移植 `_extractBalancedJsonObject`（括号深度 + 字符串转义状态机）与 `_safeJsonLoads`（去 markdown 围栏 → 严格 parse → 回退到平衡对象提取）
- `minimax.js`：`extractRangeChart({apiKey, baseUrl, model, imageDataUrl, caption})`
  - 用 `FileReader` 得到 base64；正确处理 `media_type`
  - `fetch` 到 `{baseUrl}/v1/messages`；若填了代理地址则改打代理
  - `AbortController` 实现 120s 超时
  - **永不 throw**：返回 `{ok, data, error}` 结构，对齐 Python 版"失败返回空结果"的契约
  - 从 `content[].type==="text"` 取文本 → `_safeJsonLoads`
  - 检测 `stop_reason==="max_tokens"` 截断并提示

### Phase 3 — 结果表格渲染
- `table.js`：把解析结果渲染成 4 张表
  - **Sections**：name / age_range / formations / thickness / coordinates
  - **Species Ranges**：species / section / range_base(老) / range_top(新) / biozone
  - **Biozones**：name / age / thickness
  - **Other Fossils**：自由文本列表
- 顶部显示整体 `confidence` 徽章（复用 RLPE 的 badge 配色）
- 空字段友好占位（"—"）

### Phase 4 — 导出功能
- `export.js`：
  - **复制**：`navigator.clipboard.writeText` 生成 TSV（Excel 直接粘贴）
  - **下载 CSV**：`Blob` + `URL.createObjectURL`，正确转义逗号/引号/换行，加 UTF-8 BOM 防 Excel 中文乱码
  - **下载 JSON**：完整结构化结果
  - 每张表独立导出 + "全部导出"

### Phase 5 — 流程编排与状态
- `app.js`：串起 上传 → 校验(有无 key/图) → loading 态 → 调 `extractRangeChart` → 渲染/报错
- 错误分类提示：401(key 无效) / 429(限流) / 网络或 CORS(提示启用代理) / JSON 解析失败(展示原始返回供排查)
- localStorage 记忆 key/endpoint/model（key 存储附风险提示）

### Phase 6 — 可选代理与文档
- `proxy/cloudflare-worker.js`：~20 行无状态透明代理（转发 + 补 CORS 头，不存 key）
- `proxy/README.md`：Cloudflare Worker / Vercel Edge / Deno Deploy 三种一键部署说明
- 根 `README.md`：使用步骤、CORS 说明、**安全提示**（key 存浏览器、请求客户端发起的风险与适用场景）

---

## 依赖关系

- Phase 2 依赖 Phase 1（页面容器）
- Phase 3、4 依赖 Phase 2（有数据结构）
- Phase 5 串联 1-4
- Phase 6 独立，可最后做

---

## 风险与对策

| 风险 | 等级 | 对策 |
|------|------|------|
| **CORS**：MiniMax 可能不返回 `Access-Control-Allow-Origin` 头，浏览器直连被拦 | **高** | 内置"代理地址"开关 + 提供 ~20 行 Worker 代理与部署文档；错误信息明确引导 |
| API Key 暴露在浏览器 | 中 | 明确 UI 提示；key 仅存 localStorage/sessionStorage，绝不上传任何服务器；适用"用户用自己 key"场景 |
| `max_tokens:4000` 截断（种属很多的大图） | 中 | 检测 `stop_reason` 并提示；可选允许用户调高 max_tokens |
| MiniMax JSON 带围栏/尾部杂质 | 中 | 已移植 `_safeJsonLoads` 容错，与 Python 版行为一致 |
| 超大扫描图请求体过大 / base64 膨胀 33% | 低 | 客户端 canvas 缩放压缩（如长边限制 2000px） |
| Excel 打开 CSV 中文乱码 | 低 | 导出加 UTF-8 BOM |

---

## 复杂度评估：**中等偏低**

- Phase 1（页面+样式）：~1.5h
- Phase 2（核心移植）：~2h（含 JSON 容错精确翻译）
- Phase 3（表格）：~1h
- Phase 4（导出）：~1h
- Phase 5（编排+错误处理）：~1.5h
- Phase 6（代理+文档）：~1h
- **合计：约 8 小时**

关键前提：**CORS 实测**。建议实现后第一件事就是用真实 key 打一次 MiniMax，确认走直连还是需代理——这不影响代码结构（开关已内置），只影响 README 里默认推荐哪条路。

---

## 不做的事（明确边界）

- 不移植面板关联 / 图类型分类 / PDF 摄取 / OCR / 图版分割
- 不引入任何构建工具、npm 依赖或框架
- 不做后端持久化（无数据库、无任务队列）
- 不做批量多图（首版聚焦单图；如需批量可作为后续增量）
