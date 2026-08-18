# Range-chart Analyzer — 前端代码审查报告

审查范围：`index.html`、`css/style.css`、`js/*.js`、`proxy/*.js` 以及 `gui_fluent.py` 中 Web 嵌入相关部分。
审查方式：**只读**，未修改任何源码。所有结论标注严重度（🔴 高 / 🟡 中 / 🟢 低）。
日期：2026-07-27

---

## 一、严重 Bug（🔴）

### 1. 暗色模式下 alert / pill 文字颜色整段失效（对比度崩坏）
- **文件 / 行号**：`css/style.css:128-155`
- **现象**：在暗色主题（`[data-theme="dark"]` 或 `prefers-color-scheme: dark` 且无显式主题）下，alert-info/success/warning/danger 以及 pill-good/mid/low 的**文字仍为浅色模式硬编码的深色**（如 `#065f46` 深绿），叠在深色 soft 底色（`--success-soft: #064e3b`）上几乎不可读。
- **根因**：作者想用「逗号选择器列表 + `@media`」复用同一条规则，但 CSS 规范不允许 `@media` 出现在选择器列表内：
  ```css
  :root[data-theme="dark"] .alert-success,
  @media (prefers-color-scheme: dark) {          /* 非法：@media 不能作为选择器 */
      :root:not([data-theme]) .alert-success { color: #6ee7b7; }
  }
  ```
  解析器把整条 qualified rule（含其 `{ }` 块）判为无效并**整体丢弃**，于是这些深色覆盖规则全部不生效。代码注释明确写了「dark-mode alert/pill text must be LIGHT」，意图正确但实现被破坏。
- **建议修法**：拆成两条独立规则：
  ```css
  :root[data-theme="dark"] .alert-success,
  :root[data-theme="dark"] .alert-warning, /* … 其余同理 */
  :root:not([data-theme]) .alert-success,
  :root:not([data-theme]) .alert-warning { color: #6ee7b7; /* … */ }
  ```
  或分别写 `:root[data-theme="dark"] …` 与 `@media (prefers-color-scheme: dark) { :root:not([data-theme]) … }` 两条。注意 `prefers-color-scheme` 块需放在单独的 `@media` 内，不能嵌在逗号选择器里。
- **影响**：所有暗色模式用户（含之前 Qt GUI 报告过的纯黑底可读性场景）的提示文字/一致性药丸可读性严重下降。这是本次审查中唯一会直接导致「功能可见性失败」的缺陷。

---

## 二、一般问题（逻辑隐患 / 边界，🟡）

### 2. `err.noEndpoint` 在 i18n 中缺失 → 占位符乱码
- **文件 / 行号**：`js/app.js:394`（`t('err.noEndpoint')`）；`js/i18n.js` 中无此 key（Python 端 `rca_core/i18n.py` 有，说明 JS↔Python 文案未同步）。
- **现象**：当用户清空 endpoint 字段并点击提取时，弹窗显示 `[?err.noEndpoint]` 而非正常文案。
- **根因**：`runExtraction` 在 `if (!endpoint) showAlert('warning', t('err.noEndpoint'))`，但 `js/i18n.js` 三个语种均未定义该 key。`t()` 在 key 缺失时返回 `[?key]`。
- **建议**：在 `js/i18n.js` 的 zh/en/ja 三个字典补 `err.noEndpoint`（直接复用 `rca_core/i18n.py` 对应文案）。

### 3. `quality.biozone_order_violation` 在 i18n 中缺失 → 质量徽章乱码
- **文件 / 行号**：`js/quality.js:616`（`msg_key: 'quality.biozone_order_violation'`）；`js/i18n.js` 无此 key。
- **现象**：当图表存在生物带层序违反（Steno 定律）时，`scoreConsistency` 产出该 issue，`table.js` 的 `t(iss.msg_key)` 渲染出 `[?quality.biozone_order_violation]`。
- **根因**：同 #2，JS 侧漏定义该 key（Python `rca_core/i18n.py:309/627/943` 有）。
- **建议**：补三个语种的 `quality.biozone_order_violation`，并建议加一个「i18n key 一致性」测试（比对 JS 与 Python 两份字典的 key 集合）。

### 4. 直接 / 代理模式下「图像增强（enhance）」是静默空操作
- **文件 / 行号**：`js/minimax.js:8`（`rcaLoadAndMaybeResize(file, maxEdge, opts)`）；`js/app.js:330, 442` 传入 `enhance`。
- **现象**：勾选「图像增强」后，在浏览器直连/代理模式下图片仅按 max-edge 下采样，**并未执行任何 3× 上采样 / 去噪**；而 UI 文案（`settings.enhance.hint`）声称会提升识别率。后端模式（`rcaCallBackend` 转发 `enhance: true`）由服务器处理，故仅直连路径失效。
- **根因**：`rcaLoadAndMaybeResize` 的第三个参数 `opts` 在函数体内从未被读取/使用；前端没有实现该预处理。
- **建议**：要么在前端 canvas 流程实现真正的增强（超分/锐化），要么在直连模式下显式提示「增强需经后端代理」或禁用该开关，避免与文案矛盾。

### 5. 「记住 API Key」提示文案与实际存储机制不一致（误导性安全说明）
- **文件 / 行号**：`js/i18n.js` `settings.remember.hint`（zh:「勾选后密钥保存在 localStorage；共用电脑请勿勾选」、en/ja 同理）；`js/config.js:60-64`、`js/app.js:199-211`。
- **现象**：i18n 称密钥存入 `localStorage`；但 `config.js` 的 `_storageFor()` 强制 `apiKey` 永远走 `sessionStorage`（标签页关闭即清除），「记住」开关只控制是否把 key 写入 sessionStorage，**从不写入 localStorage**。
- **根因**：实现其实比文案更安全（sessionStorage），但文案写错，会误导用户：在共用电脑上误以为「勾选=持久化到 localStorage 很危险」而拒绝使用，或反之误以为更安全。
- **建议**：将 hint 文案改为「密钥仅保存在当前会话（sessionStorage），关闭标签页即清除」；并明确「记住」开关的作用。

### 6. `rcaNormalizeColumnarResult` 中「携带其他顶层字段」的循环是空操作
- **文件 / 行号**：`js/minimax.js:316-320`
- **现象**：当柱状图结果被 `_array_root` 包裹时，`parsed` 已被改写为 `{ ...dist }`，随后 `for (const k of Object.keys(parsed)) { if (k in dist) continue; parsed[k] = (arguments[0]||{})[k]; }` 中 `k in dist` 永远为真，循环体从不执行，原 `parsed` 的额外顶层字段被丢弃。
- **根因**：循环遍历的是新对象 `parsed`（== `dist`），而非原始参数 `arguments[0]`；应遍历 `arguments[0]` 的键并拷贝不在 dist 中的字段。
- **影响**：仅当模型返回「顶层数组被包裹 + 另有额外顶层字段」的极少见形态时丢失字段，常规柱状图不受影响。
- **建议**：改为 `for (const k of Object.keys(arguments[0]||{})) { if (k==='_array_root'||k in dist) continue; parsed[k]=(arguments[0])[k]; }`。

### 7. `quality.ages_inconsistent` 的 count 参数恒为 '1'
- **文件 / 行号**：`js/quality.js:356`（`params: {count: '1'}`）。
- **现象**：跨纪剖面数量可能 >1，但提示永远显示「{count} 个剖面」中的 `1`。
- **建议**：累计 `crossEraCount2` 后传入真实数量。

### 8. Cloudflare Worker 限流表无上限（与 Deno 版不一致）
- **文件 / 行号**：`proxy/cloudflare-worker.js:102-125`（`rateCheck`）对比 `proxy/deno-proxy.js:57-86`（后者有 `MAX_RATE_MAP_SIZE` 与 LRU 淘汰）。
- **现象**：Cloudflare 版 `_rateMap` 无容量上限与 LRU 淘汰，长期运行存在内存增长；且其 `rateCheck` 在鉴权判断之前就消耗一个配额槽，被拒请求也会占额度。
- **建议**：对齐 Deno 版加 `MAX_RATE_MAP_SIZE` + 最旧键淘汰；并考虑仅在 `authorized` 通过后才计费率。

### 9. 直连多跑（runs>1）的 phylogenetic_tree 合并会误用 range_chart keymap
- **文件 / 行号**：`js/app.js:505`（`rcaAutoDetectKeymap(okDatas)`）；`js/aggregate.js:201-205` 无 phylogenetic keymap。
- **现象**：phylogenetic_tree 多次直连运行时，自动检测回退到 `RCA_DEFAULT_KEYMAP`（species_ranges 为主），会把 nodes 当作 species 合并，得到错误结果。
- **影响**：边缘功能（树状图多跑合并）。
- **建议**：为 phylogenetic_tree 增加专用 keymap 或在 `rcaAutoDetectKeymap` 中识别 `nodes`/`root_ids` 形状。

---

## 三、安全隐患

- 🟡 **#5（记住 Key 文案误导性）**：实现本身安全（apiKey 仅 sessionStorage），但文案声称 localStorage，属安全说明错误，应修正（见二/5）。
- 🟢 **API Key 在代理转发**：`proxy/*.js` 把 `x-api-key` 透传给 MiniMax 是设计需要；代理无日志、无落盘，仅白名单转发到硬编码的 MiniMax 端点，不存在「任意 URL 转发」风险。两版代理默认 `ALLOWED_ORIGINS` 为空 → 拒绝所有（除非配置 `PROXY_SHARED_SECRET`），已正确关闭开放中继。符合预期。
- 🟢 **XSS**：已审查。`table.js`/`app.js` 中用户/模型内容一律经 `rcaEsc()`/`textContent` 注入；`innerHTML` 仅用于重置空容器或拼接已转义片段；导出用了 `rcaFormulaSafe`/`rcaCsvCell` 防公式注入（Excel）。未发现直接 innerHTML 未转义用户数据的点。
- 🟢 **CORS / fetch 错误处理**：`extractRangeChart` 与 `rcaCallBackend` 对 `AbortError`、网络错误、`!resp.ok` 均有分类处理（401/403/413/429/超时/取消），并优先采用服务端 `error_key`。健壮性良好。
- 🟢 **localStorage 禁用场景**：`rcaStoreGet/Set` 均有 try/catch 回退，私密模式不会崩溃（代价是设置不持久，见六）。

---

## 四、性能与优化建议

- 🟢 **重复 DOM 查询**：`app.js` 大量使用 `$('id')` 即时查询，虽可接受，但 `syncRangeOutputs`/`syncSegmentedFromSelect` 在每次 `loadSettings` 重查；可对高频元素（extract-btn、conn-mode 等）做一次性缓存。
- 🟢 **未防抖输入**：caption 计数器、`range` 的 `<output>` 更新绑在 `input` 事件上，开销极低，无需防抖；仅作说明，非问题。
- 🟢 **大图 base64 常驻内存**：`state.dataUrl` 一直持有原始（可能下采样后的）data URL，直到 reset；20MB 图片 base64 约 26MB 字符串。单次提取无碍，但建议在 reset 时显式置 `null` 并 `preview-img.src=''` 以释放（当前已做）。
- 🟢 **`rcaApplyI18n(document)` 全量重扫**：语言切换时整页重扫 `data-i18n`，DOM 规模小，可接受。
- 🟢 **`IntersectionObserver` / `requestAnimationFrame`**：生命周期为页面级，无泄漏；confidence ring 的 rAF 在 `p>=1` 后自停。无明显内存泄漏。

---

## 五、可维护性建议

- 🟢 **魔法字符串 `'rca.maxTokens'`**：`app.js` 用字面量 `'rca.maxTokens'`，未纳入 `config.js` 的 `RCA_STORE`（该对象缺少 maxTokens 键）。load/save 两侧虽一致，但易在重构时失配，建议补到 `RCA_STORE`。
- 🟢 **`arguments[0]` 取原始参数**：`rcaNormalizeColumnarResult` 用 `arguments[0]` 取改写前的 `parsed`（见二/6），可读性差且易错，建议显式保留 `const original = parsed;` 再改写。
- 🟢 **i18n 双源不一致**：JS（`js/i18n.js`）与 Python（`rca_core/i18n.py`）两份字典各自维护，已出现 #2/#3 漏 key。建议加 CI 校验两份 key 集合一致。
- 🟢 **暗色 alert/pill 覆盖规则冗余且失效**：`style.css:128-155` 整段（8 个块）因语法错误无效，应删除并重写为合法规则；当前是死代码（见一/1）。
- 🟢 **theme.js 与 app.js 初始化顺序**：`theme.js` 在 `app.js` 之前同步加载并立即 `apply()`，规避了暗色闪烁；`app.js` 内 `loadSettings` 又调用 `syncFooterRuntime()`。依赖关系靠 `<script>` 顺序保证，已在 HTML 注释说明，属可控。

---

## 六、与 PySide6 嵌入相关的注意点

- 🟡 **`file://` 下「auto」模式解析为 `direct`**：`gui_fluent.py` 通过 `setUrl(file://…)` 加载 `index.html`（`gui_fluent.py:371`）。`rcaResolveMode()`（`app.js:32-40`）在协议为 `file:` 时走 `direct`，即**嵌入的 QWebEngine 直接以 `file://`/`null` 源向 `https://api.minimaxi.com` 发起跨域请求**。若 MiniMax 的 Anthropic 兼容端点不返回允许 `null`/任意源的 CORS 头，提取会失败报网络/CORS 错误。
  - **建议**：桌面端若希望走本地后端或稳定直连，应在加载时显式把 `#conn-mode` 设为 `backend`，或在 `rcaResolveMode` 中识别 Qt 嵌入来源（如特定 `baseUrl`/用户代理）默认走 backend；否则需在文档中告知用户：Qt 内嵌下如遇 CORS，需自建代理并填「代理地址」。
- 🟡 **`localStorage`/`sessionStorage` 在 `file://` 下可能不可用**：QWebEngine 默认对 `file://` 的存储可能禁用或分区。后果：
  - 设置（endpoint/model/主题/apiKey 等）无法跨启动持久化；
  - `theme.js` 写 `rca.theme` 会被 try/catch 吞掉，主题不记忆；
  - `saveSettings()` 中 `rcaStoreSet` 返回 `false` → 弹出「设置保存失败」。
  - **建议**：对 `QWebEngineSettings` 开启 `LocalStorageEnabled`；或引入 `registerJsObject` 把 key/设置桥接到 Python 端 `~/.range_chart_analyzer.json`（代码已注释「Phase L」对齐 Python GUI 持久化，但 `gui_fluent.py` 当前**未**使用 `registerJsObject`/`setWebChannel`，仅作 file:// 加载）。
- 🟢 **空 CSS 变量导致纯黑底**：任务特别担心的「`--bg` 等变量在嵌入环境为空 → 纯黑底」场景**并未出现**——所有设计令牌都在 `:root`/媒体查询中显式声明，不存在未定义变量。唯一暗色缺陷是上述一/1 的覆盖规则失效（已修复则无黑底问题）。
- 🟢 **`prefers-color-scheme` 在 QWebEngine**：嵌入式不一定反映操作系统暗色偏好，theme 默认回退到 light，可接受。
- 🟢 **经典 `<script src>` 加载**：本地 JS 以同目录相对路径、经典脚本方式加载，`file://` 下不受 ES module CORS 限制，正常。

---

## 附：已验证「无问题」的点（避免重复告警）
- `max-tokens` 的 `<output>` 初值 4000 落在 `[500,32000]` 合法区间内，与 `config.js` 的 `defaultMaxTokens=4000 / maxMaxTokens=32000` 不矛盾；`loadSettings` 经 `syncRangeOutputs` 会正确回填。
- `index.html` 中全部 `data-i18n` / `data-i18n-ph` 键在 `js/i18n.js` 中均存在（zh/en/ja），无缺失引用。
- 异步竞态：多次提取/换图/重置通过 `expectedToken`/`loadToken`/`extractToken` + `AbortController` 正确串行化，旧请求结果不会覆盖新结果（代码注释与实现一致）。
- 取消逻辑：`cancel-btn` 中止 `AbortController`，`runExtraction` 在 `abort.signal.aborted` 时静默丢弃，不弹错误。
- 质量徽章 `data.quality` 在直连模式下由 `globalThis.scoreRangeChart` 计算并写入，后端模式则本地重算，均有兜底 try/catch。
