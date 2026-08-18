# Range-chart Analyzer — 前端修复报告（FRONTEND-FIX-2026-07-27）

修复阶段，对应审查报告 `docs/FRONTEND-REVIEW-2026-07-27.md`。仅改前端相关文件与 `gui_fluent.py` 的 Web 设置，**未改动** Python 后端逻辑与 `rca_core/`。所有改动均通过语法检查（`node --check` / `py_compile`）。

---

### 1. 🔴 CSS 暗色 alert/pill 文字不可读 — 已修复
- 文件：`css/style.css:126-155`
- 改法：删除原非法的「逗号选择器列表内嵌 `@media`」整段（浏览器整体丢弃）。重写为两段合法规则：
  - 显式主题路径 `:root[data-theme="dark"] .alert-success/.alert-warning/.alert-danger/.alert-info/.pill-good/.pill-mid/.pill-low` 分别上色（`#6ee7b7`/`#fcd34d`/`#fca5a5`/`#a5b4fc`）。
  - 独立 `@media (prefers-color-scheme: dark) { :root:not([data-theme]) … }` 块处理「跟随系统暗色」。
  - 浅色模式规则（`.alert-*` / `.pill-*` 在 834-837、1050-1052 行）保留不动。

### 2. 🟡 i18n 缺失 key / 误导性 hint — 已修复
- 文件：`js/i18n.js`（zh/en/ja 三字典）
- 新增 `err.noEndpoint`：zh「请先选择并配置一个端点。」/ en「Please select and configure an endpoint first.」/ ja「先にエンドポイントを選択して設定してください。」（意图对齐 `rca_core/i18n.py`）。
- 新增 `quality.biozone_order_violation`，保留占位符 `{species}/{younger_biozone}/{older_biozone}`，与 `js/quality.js:617-621` 实际传入参数一致。
- 修正 `settings.remember.hint`（zh/en/ja）：改为如实说明密钥仅存于当前会话 `sessionStorage`、关闭标签页即清除，"记住"仅在本会话内保留（与 `js/config.js:62` `_storageFor()` 的强制 sessionStorage 行为一致）。

### 3. 🟡 「图像增强」静默空操作 — 已修复
- 文件：`js/minimax.js` 的 `rcaLoadAndMaybeResize` 与新增辅助函数 `rcaUnsharpMask`
- 改法：当 `opts.enhance` 为真时，绘制到 canvas 后执行保守增强：① 放大到约 2×（`MAX_INTERMEDIATE=4096` 封顶）的临时 canvas，再下采样回目标尺寸以获得平滑+细节；② 对最终 canvas 做 3×3 轻量 unsharp-mask（`amount=0.4, threshold=1`，>16M 像素跳过以防卡顿），`putImageData` 回写。非 enhance 路径行为不变。`rcaUnsharpMask` 全程 try/catch，失败静默跳过。

### 4. 🟡 柱状图额外顶层字段被静默丢弃 — 已修复
- 文件：`js/minimax.js` 的 `rcaNormalizeColumnarResult`（约 315-320 行）
- 改法：先 `const original = arguments[0] || {};` 保存改写前对象，再 `for (const k of Object.keys(original)) { if (k==='_array_root' || k in dist) continue; parsed[k]=original[k]; }`，把原结果中不在 `dist` 的额外顶层字段拷回 `parsed`。

### 5. 🟡 质量计数恒为 '1' — 已修复
- 文件：`js/quality.js:356`
- 改法：`quality.ages_inconsistent` 的 `params.count` 由 `'1'` 改为 `String(crossEraCount2)`（该变量在入栈前自增，值即真实跨纪剖面数）。

### 6. 🟡 Cloudflare Worker 限流无上限 — 已修复
- 文件：`proxy/cloudflare-worker.js`（`rateCheck` 及 `fetch` 处理）
- 改法：对齐 `proxy/deno-proxy.js`：加 `MAX_RATE_MAP_SIZE = 10_000` 与最旧键 LRU 淘汰（达上限时 `delete` 首个键），并在命中时「touch」以标记新鲜。同时把 `rateCheck` 调用移至 `authorized` 判定**之后**——被拒（未授权）请求不再消耗配额槽，避免伪造请求撑爆限流表。

### 7. 🟡 Qt 嵌入持久化（gui_fluent.py）— 已修复（含待确认）
- 文件：`gui_fluent.py` 的 `PhyloTreeWidget.__init__` 的 QWebEngineSettings 设置段
- 改法：对该 `QWebEngineView` 创建命名持久 `QWebEngineProfile("RangeChartAnalyzer")` 并 `setPersistentStoragePath(AppDataLocation/web)` + `setPage`，同时 `settings.setAttribute(LocalStorageEnabled, True)`，使嵌入页 `localStorage/sessionStorage` 在 `file://` 下可用、跨启动持久化。未引入 `registerJsObject`/`QWebChannel`。
- **待确认**：审查报告称「主窗口加载 `index.html` 的 QWebEngine（gui_fluent.py:371）」，但实际代码中 `index.html` 主前端是通过 `app.py` 经 pywebview / Flask 以 `http://localhost` 提供，**并非** `gui_fluent.py` 内的 QWebEngineView；`gui_fluent.py` 里唯一加载 `file://` 的 QWebEngineView 是 `PhyloTreeWidget`（第 371 行即其模板）。故本次修复作用于该嵌入视图。若本意是让主前端在 Qt 内嵌下持久化，正确落点应是 pywebview 设置（非 QWebEngine），需进一步确认。

---

### 验证
- `node --check` 通过：`js/minimax.js`、`js/i18n.js`、`js/quality.js`、`proxy/cloudflare-worker.js`。
- `python -m py_compile gui_fluent.py` 通过。
- 未执行 git commit（按要求）。

---

## QA 验证（严过关 / 2026-07-27）

1. **JS 语法**：`i18n.js`/`minimax.js`/`quality.js`/`cloudflare-worker.js` 四个文件 `node --check` 全通过。✅
2. **CSS 合法性**：`style.css:126-150` 已拆为两段合法规则——显式 `:root[data-theme="dark"] .alert*/.pill-*` 选择器列表（132-138）+ 独立 `@media (prefers-color-scheme: dark)` 块（142-150）。`@media` 已不在逗号选择器列表内。✅
3. **i18n key 齐全**：`err.noEndpoint`（zh164/en379/ja593）、`quality.biozone_order_violation`（zh216/en431/ja645）、`settings.remember.hint`（zh32/en247/ja461）三语全部存在，且 hint 三语均改述 sessionStorage、不再提 localStorage。✅
4. **enhance/柱状图**：`minimax.js:40` 真正读取 `opts.enhance`，为真走 2× 上采样（≤4096）+`rcaUnsharpMask`，否则走原 `ctx.drawImage`（:67），非 enhance 路径不变；`rcaNormalizeColumnarResult:391` 保存 `original = arguments[0]`，:396 遍历 original 把额外顶层字段拷回 `parsed`。✅
5. **质量计数**：`quality.js:356` 用 `String(crossEraCount2)`（真实自增计数），非硬编码 `'1'`。✅
6. **Cloudflare 限流**：`worker:102` `MAX_RATE_MAP_SIZE=10_000`、:108-110 达上限淘汰最旧键（LRU）、:118-119 touch 保鲜；`rateCheck`（:273）置于 `authorized` 判定（:251 / :267-269 拒绝）之后，被拒请求不占配额。✅
7. **gui_fluent.py 语法**：`py_compile` 通过。✅
8. **「待确认」核实**：主前端由 `app.py` + pywebview 经 `http://localhost` 提供（`app.py:4-5`），非 QWebEngine；`gui_fluent.py` 中唯一加载 `file://` 的 QWebEngineView 是 `PhyloTreeWidget`（:288 / :401 `setUrl`），本次 `LocalStorageEnabled` + 持久 `QWebEngineProfile`（:339-341 / :366）仅作用于它。结论：修复只覆盖 PhyloTreeWidget，**未覆盖主前端**；审查报告「Qt 嵌入下设置无法跨启动持久化」的担忧若指主前端则本次未覆盖（主前端本就非 Qt 嵌入）。该缺口工程师已自披露，事实如上，不为其背书。

**路由判定：NoOne**。7 项修复全部通过、无语法错误、无逻辑错误。但第 8 项揭示主前端持久化范围未被覆盖，属架构误判而非漏改，建议 team-lead 确认是否需另行处理 pywebview 持久化。
