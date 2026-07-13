# 设计文档：多 LLM 提供商切换（移植自 cc-switch）

参考 `D:/GIthub/cc-switch-main` 的多 LLM 移植架构，将其移植到 Range Chart Analyzer。

## 1. cc-switch 的关键架构（来自调研）

| 概念 | cc-switch 实现 |
|------|---------------|
| **Provider** | `{id, name, settingsConfig: JSON, meta: JSON, is_current, category, sort_index}` |
| **Preset** | 纯数据模板 `{name, endpoint, apiKeyField, apiFormat, models[], headers[], defaultModel, cost}` — 新建 Provider 时从中 seed |
| **存储** | SQLite via Tauri/rusqlite，主键 `(id, app_type)` |
| **切换** | `is_current` 布尔位，单 active per app |
| **格式抽象** | `apiFormat ∈ {anthropic, openai_chat, openai_responses, gemini_native}` |
| **模型** | 每个 Provider 带 `models[]`（含 contextWindow、cost、modalities）；默认模型在 `settingsConfig` 里 |
| **故障转移** | `in_failover_queue` + `provider_health`（consecutive_failures, is_healthy）|

**我们要移植的**：Provider 数据模型、Preset 列表、格式抽象、切换机制。
**我们不移植的**：OAuth、MCP、per-session 模型覆盖、多 Electron app 管理（我们只有一个 app）。

---

## 2. 移植后的目标架构

### 2.1 Provider 数据模型

```python
@dataclass
class LlmProvider:
    id: str                          # uuid
    name: str                        # 显示名
    api_format: ApiFormat            # anthropic | openai | gemini
    endpoint: str                    # base URL
    api_key: str
    model: str                       # 当前选中的 model id
    extra_headers: dict[str, str] = field(default_factory=dict)
    extra_body: dict[str, Any] = field(default_factory=dict)   # 并入请求体
    is_current: bool = False
    created_at: float = 0.0
    sort_index: int = 0

class ApiFormat(Enum):
    ANTHROPIC = "anthropic"          # /v1/messages, x-api-key, anthropic-version
    OPENAI = "openai"                # /v1/chat/completions, Authorization: Bearer
    GEMINI = "gemini"                # :generateContent, key in query/header
```

**为什么存储用 JSON 文件而不是 SQLite**：我们单用户、单表、读多写少，`~/.range_chart_analyzer/providers.json` 足够。cc-switch 用 SQLite 是因为要管理跨多 app 的百万级请求日志、多 provider 健康追踪。

### 2.2 多 API 格式的提取调度

现有的 `extract_range_chart` 硬编码 Anthropic 兼容。重构为：

```python
def call_llm_api(
    *,
    provider: LlmProvider,
    system_prompt: str,
    image_b64: str,
    media_type: str,
    user_text: str,
    max_tokens: int,
    timeout_sec: int,
) -> tuple[str, bool, int]:   # (raw_text, truncated, status)
```

内部按 `provider.api_format` 分发：

| ApiFormat | 端点 | Auth Header | 请求体结构 | 响应解析 |
|-----------|------|-------------|-----------|---------|
| `anthropic` | `{endpoint}/v1/messages` | `x-api-key` + `anthropic-version: 2023-06-01` | `{model, max_tokens, system, messages:[{role,content:[image+text]}]}` | `content[0].text`, `stop_reason=max_tokens` |
| `openai` | `{endpoint}/v1/chat/completions` | `Authorization: Bearer {key}` | `{model, max_tokens, messages:[{role:"system",content:system_prompt},{role:"user",content:[{type:"image_url",...},{type:"text",...}]}]}` | `choices[0].message.content`, `finish_reason=length` |
| `gemini` | `{endpoint}/v1beta/models/{model}:generateContent?key={key}` | （key 走 query）| `{contents:[{role:"user",parts:[{inline_data:{mime_type,data}},{text}]}], system_instruction:{parts:[{text}]}, generation_config:{max_output_tokens}}` | `candidates[0].content.parts[0].text`, `finishReason=MAX_TOKENS` |

返回契约**统一为** `(raw_text, truncated, status)`，下游 `safe_json_loads` + `normalize_result` 不变。

### 2.3 预设提供商（PROVIDER_PRESETS）

纯数据列表，每个 preset 是 `LlmProvider` 的模板。加总约 15–20 个，按类别分组：

| 类别 | 预设 |
|------|------|
| 官方直连 | **Anthropic** (`claude-opus-4-1`)、**OpenAI** (`gpt-4o`)、**Google Gemini** (`gemini-2.5-pro`)、**MiniMax** (`MiniMax-M3`)、**Mistral** (`mistral-large`) |
| 国产官方 | **DeepSeek** (`deepseek-chat`)、**通义千问/Qwen** (`qwen-max`)、**Kimi/Moonshot** (`moonshot-v1`)、**智谱 GLM** (`glm-4-plus`)、**豆包 ByteDance** (`doubao-pro`) |
| 聚合网关 | **OpenRouter**、**Together AI**、**Groq**、**SiliconFlow硅基流动**、**OpenAI One API / NewAPI** |

每个 preset 字段：`{name, api_format, endpoint, model, api_key_placeholder, extra_headers, doc_url}`。

### 2.4 存储与切换

- 存储路径：`~/.range_chart_analyzer/providers.json`，格式：`{version: 1, providers: [...], current_id: str}`
- 切换：改 `current_id` 并重载。无需 SQLite 事务。
- 单文件读/写：已 json + atomic write（写 tmp 再 rename），防半写。

### 2.5 GUI / Web / 后端改动

**GUI 新增区域**（设置卡片中新增"LLM 提供商"段）：
- 当前提供商下拉（ `<ttk.Combobox>` ）+ "切换"按钮
- "添加提供商"按钮 → 弹出模态对话框
  - 第一步：选预设（网格搜索，类似 cc-switch `ProviderPresetSelector`）或"自定义"
  - 第二步：填 api_key、确认 endpoint/model
- 已配置提供商列表：增删改、设当前、删除

**Web 前端**：
- Settings 新增区域：当前 provider 下拉、添加/编辑/切换、preset 网格

**后端**：暂时只服务 GUI + web 同步；multi-provider 路由在 web `minimax.js` 与 `server.py` 中按 `provider` 字段从 localStorage / 请求体取。

### 2.6 移植边界（明确不做）

- ❌ OAuth / API Key 轮换（cc-switch 的 Copilot OAuth FLOW — 超出范围）
- ❌ per-request failover 与 circuit-breaker（cc-switch 的核心但对单用户科研工具过度设计；列为未来增量）
- ❌ per-session 模型覆盖（我们 per-result 只跑一次，无需会话级覆盖）
- ❌ 多 app 管理（我们只有一个 app — Range Chart Analyzer）
- ❌ 用量日志 / model pricing 跟踪（未来可选）

---

## 3. 需求重述

在不破坏现有功能（GUI/后端/纯前端、模式 1 + 模式 2、三语、三端合约）前提下：
1. **多 API 格式支持**：Anthropic / OpenAI / Gemini 三种 wire format，由 `provider.api_format` 字段驱动。
2. **Provider CRUD**：用户可在 GUI 和 Web 添加/编辑/删除/切换 LLM 提供商。
3. **预设加速**：提供 15+ 常见提供商预设（含 endpoint + 默认 model + 推荐 key 字段），用户选预设只需填 key。
4. **配置持久化**：单文件 JSON + atomic write，单 `current_id` 决定激活 provider。
5. **安全**：api_key 明文存本地（与现在 `.env` 等做法一致，不引入 keyring 依赖）。

---

## 4. 实施阶段

| 阶段 | 范围 | 工作量 |
|------|------|--------|
| **A. 数据模型 + 多格式调度** | `rca_core/llm.py`（新）：`ApiFormat`、`LlmProvider`、`call_llm_api`、`PROVIDER_PRESETS`、`ProviderStore`（读/写/切换） | ~3h |
| **B. 现有提取器接入** | `rca_core/extractor.py`：`extract_range_chart` / `extract_columnar_section` 改调 `call_llm_api(provider, ...)`. `server.py` 透传 provider 字段由 JSON body 传`. GUI `gui.py` `_worker` 构造 provider 对象. | ~2h |
| **C. GUI 提供商管理** | 设置卡片加"LLM 提供商"段（下拉+添加按钮）；模态对话框（预设网格+自定义表单，2 步 wizard）| ~4h |
| **D. Web 前端** | `index.html`+`js/app.js`+`js/i18n.js` Settings 区加同样 CRUD + preset 网格 | ~4h |
| **E. 后端 `/api/extract` 透传** | 接收完整 provider 对象（id/format/endpoint/key/model/extra_*)，转发到 `call_llm_api` | ~1h |
| **F. 测试** | `tests_core.py` 加多格式 payload 测试、ProviderStore 读/写/切换测试、preset 完整性测试；端到端用 fake HTTP server 打三种格式 | ~3h |
| **G. 文档 + README** | 截图-less 文字说明 + 预设列表 | ~1h |
| **合计** | | ~18h |

---

## 5. 依赖 & 风险

- **无新增第三方依赖**：复用标准库 `urllib`/`json`；不用 SDK（OpenAI 等），避免膨胀。
- **风险 1**：部分 provider 虽自称 "Anthropic 兼容"但 header 名不同（如某些网关用 `Authorization: Bearer` 而非 `x-api-key`） → 解法：自定义 provider 支持 `extra_headers` 覆盖。
- **风险 2**：Gemini 的 `max_output_tokens` 与 OpenAI 的 `max_tokens` 字段名不同；Gemini 的 thinking/reasoning 返回结构不同 → 风险低（我们只看 text + truncated flag）。
- **风险 3**：多 provider 增加 GUI/Web 设置面板复杂度 → 解法：加"高级"折叠区，基础用户不受影响。

## 6. 明确不做

- 不做 OAuth
- 不做自动 failover / circuit-breaker
- 不做用量统计
- 不做 per-session 模型覆盖
- 不改现有 Anthropic 兼容提取语义（只加 dispatch 层）

---

## 7. 验收标准

1. GUI + Web 能添加 ≥ 3 种 provider（Anthropic、OpenAI、MiniMax），写同一张图注，三种都能跑通并成功提取。
2. Anthropic 格式走 `x-api-key` + `/v1/messages`；OpenAI 格式走 `Authorization: Bearer` + `/v1/chat/completions`；Gemini 格式走 `:generateContent` + key in query — 三种各过一个 fake server 回归测试。
3. Provider 列表持久化：重启 GUI/Web 后列表 + 当前 provider 不变。
4. 后向兼容：旧 config（无 provider 字段）自动 seed 默认 Anthropic 兼容（MiniMax）预设，行为不变。
5. 现有 78+9+27 个测试全部不破坏。
