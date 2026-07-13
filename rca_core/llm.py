"""Multi-LLM provider abstraction.

Providers carry the credentials + wire-format needed to talk to a variety of
LLM services. A provider's ``api_format`` selects one of the supported
transports (Anthropic / OpenAI / Gemini), each of which is implemented as a
single Python function. New formats are added by extending the ``ApiFormat``
enum and dispatching in ``call_llm_api``.

Preset catalog
--------------
``PROVIDER_PRESETS`` ships a curated list of common LLM endpoints ( Anthropic,
OpenAI, Gemini, MiniMax, DeepSeek, Qwen, Kimi, Zhipu, OpenRouter, Together,
Groq, SiliconFlow, Mistral, Anthropic-compatible aggregators, etc.). Each preset
is a template ``LlmProvider`` — the user picks one, fills in an API key, and the
provider is ready to use.

Storage
-------
``ProviderStore`` persists a list of user-configured providers to
``~/.range_chart_analyzer/providers.json``. The active provider is identified by
``current_id`` (single-active-per-app, mirroring cc-switch's ``is_current`` flag).
Atomic write (tmp + rename) guards against half-written files on crash.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

# ---------------------------------------------------------------------------
# API format
# ---------------------------------------------------------------------------


class ApiFormat(str, Enum):
    """Wire-format / auth scheme for a provider."""

    ANTHROPIC = "anthropic"  # /v1/messages, x-api-key + anthropic-version
    OPENAI = "openai"  # /v1/chat/completions, Authorization: Bearer
    GEMINI = "gemini"  # :generateContent, key in query or header


# ---------------------------------------------------------------------------
# Provider dataclass
# ---------------------------------------------------------------------------


@dataclass
class LlmProvider:
    """A single configured LLM endpoint.

    Mirrors the structure of cc-switch's ``Provider`` record but flattened for
    our single-app use case. ``extra_headers`` and ``extra_body`` let advanced
    users accommodate proxies or non-standard gateways.
    """

    id: str = ""
    name: str = "New provider"
    api_format: ApiFormat = ApiFormat.ANTHROPIC
    endpoint: str = ""
    api_key: str = ""
    model: str = ""
    extra_headers: dict[str, str] = field(default_factory=dict)
    extra_body: dict[str, Any] = field(default_factory=dict)
    is_current: bool = False
    created_at: float = 0.0
    sort_index: int = 0

    def __post_init__(self) -> None:
        if isinstance(self.api_format, str):
            try:
                self.api_format = ApiFormat(self.api_format)
            except ValueError:
                self.api_format = ApiFormat.ANTHROPIC
        if not self.id:
            self.id = str(uuid.uuid4())
        if not self.created_at:
            self.created_at = time.time()

    @property
    def display_label(self) -> str:
        fmt = self.api_format.value if isinstance(self.api_format, ApiFormat) else str(self.api_format)
        return f"{self.name}  ·  {self.model or '-'}  ·  {fmt}"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["api_format"] = self.api_format.value
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "LlmProvider":
        d = dict(d)
        fmt_raw = d.get("api_format", "anthropic")
        try:
            fmt = ApiFormat(fmt_raw)
        except ValueError:
            fmt = ApiFormat.ANTHROPIC
        d.pop("api_format", None)
        provider = cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})
        provider.api_format = fmt
        return provider


# ---------------------------------------------------------------------------
# Presets
# ---------------------------------------------------------------------------


@dataclass
class ProviderPreset:
    """Template for a new provider — presented in the preset grid."""

    name: str
    api_format: ApiFormat
    endpoint: str
    model: str
    category: str = "official"  # official | cn_official | aggregator | third_party
    doc_url: str = ""
    extra_headers: dict[str, str] = field(default_factory=dict)
    api_key_hint: str = "API Key"


def _p(
    name: str,
    fmt: ApiFormat,
    endpoint: str,
    model: str,
    *,
    category: str = "official",
    doc_url: str = "",
    extra_headers: dict[str, str] | None = None,
    api_key_hint: str = "API Key",
) -> ProviderPreset:
    return ProviderPreset(
        name=name,
        api_format=fmt,
        endpoint=endpoint,
        model=model,
        category=category,
        doc_url=doc_url,
        extra_headers=extra_headers or {},
        api_key_hint=api_key_hint,
    )


PROVIDER_PRESETS: list[ProviderPreset] = [
    # --- Anthropic format: official + aggregators ---
    _p("Claude Official", ApiFormat.ANTHROPIC,
       "https://api.anthropic.com", "claude-opus-4-1",
       category="official",
       doc_url="https://console.anthropic.com/settings/keys",
       api_key_hint="sk-ant-..."),
    _p("MiniMax M3", ApiFormat.ANTHROPIC,
       "https://api.minimaxi.com/anthropic", "MiniMax-M3",
       category="cn_official",
       doc_url="https://platform.minimaxi.com/user-center/payment/token-plan",
       api_key_hint="MiniMax API Key"),
    _p("Shengsuanyun (神算云)", ApiFormat.ANTHROPIC,
       "https://api.shengsuanyun.com", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://www.shengsuanyun.com", api_key_hint="..."),
    _p("PatewayAI", ApiFormat.ANTHROPIC,
       "https://api.patewayai.com", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://www.patewayai.com", api_key_hint="..."),
    _p("Huoshan Agentplan", ApiFormat.ANTHROPIC,
       "https://ark.cn-beijing.volces.com/api/v3", "claude-sonnet-4-20250514",
       category="cn_official",
       doc_url="https://console.volcengine.com/ark", api_key_hint="..."),
    _p("BytePlus", ApiFormat.ANTHROPIC,
       "https://ark.ap-southeast-1.bytepluses.com/api/v3", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://console.byteplus.com", api_key_hint="..."),
    _p("DouBaoSeed", ApiFormat.ANTHROPIC,
       "https://ark.cn-beijing.volces.com/api/v3", "doubao-seed-1-6-250615",
       category="cn_official",
       doc_url="https://console.volcengine.com/ark", api_key_hint="..."),
    _p("CCSub", ApiFormat.ANTHROPIC,
       "https://api.ccsub.io", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://ccsub.io/dashboard", api_key_hint="ccs-..."),
    _p("SubRouter", ApiFormat.ANTHROPIC,
       "https://api.subrouter.net", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://subrouter.net", api_key_hint="..."),
    _p("Unity2.ai", ApiFormat.ANTHROPIC,
       "https://api.unity2.ai", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://unity2.ai", api_key_hint="..."),
    _p("Qiniu (七牛)", ApiFormat.ANTHROPIC,
       "https://api.qiniu.com", "claude-sonnet-4-20250514",
       category="cn_official",
       doc_url="https://portal.qiniu.com", api_key_hint="..."),
    _p("FennoAI", ApiFormat.ANTHROPIC,
       "https://api.fenno.ai", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://fenno.ai", api_key_hint="..."),
    _p("ZetaAPI", ApiFormat.ANTHROPIC,
       "https://api.zetaapi.com", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://zetaapi.com", api_key_hint="..."),
    _p("TeamoRouter", ApiFormat.ANTHROPIC,
       "https://api.teamorouter.com", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://teamorouter.com", api_key_hint="..."),
    _p("Amux", ApiFormat.ANTHROPIC,
       "https://api.amux.ai", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://amux.ai", api_key_hint="..."),
    _p("PackyCode", ApiFormat.ANTHROPIC,
       "https://api.packycode.com", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://www.packycode.com/dashboard", api_key_hint="pk-..."),
    _p("CherryIN (樱鹿)", ApiFormat.ANTHROPIC,
       "https://api.cherryin.ai", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://cherryin.ai", api_key_hint="..."),
    _p("SiliconFlow (硅基流动)", ApiFormat.ANTHROPIC,
       "https://api.siliconflow.cn", "claude-sonnet-4-20250514",
       category="cn_official",
       doc_url="https://cloud.siliconflow.cn/account/ak", api_key_hint="sk-..."),
    _p("DMXAPI", ApiFormat.ANTHROPIC,
       "https://api.dmxapi.cn", "claude-sonnet-4-20250514",
       category="cn_official",
       doc_url="https://api.dmxapi.cn", api_key_hint="..."),
    _p("APIKEY.FUN", ApiFormat.ANTHROPIC,
       "https://apikey.fun", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://apikey.fun", api_key_hint="..."),
    _p("APINebula", ApiFormat.ANTHROPIC,
       "https://api.apinebula.com", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://apinebula.com", api_key_hint="..."),
    _p("AtlasCloud", ApiFormat.ANTHROPIC,
       "https://api.atlascloud.ai", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://atlascloud.ai", api_key_hint="..."),
    _p("SudoCode", ApiFormat.ANTHROPIC,
       "https://api.sudocode.ai", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://sudocode.ai", api_key_hint="..."),
    _p("ClaudeAPI", ApiFormat.ANTHROPIC,
       "https://api.claudeapi.com", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://claudeapi.com", api_key_hint="..."),
    _p("Code0", ApiFormat.ANTHROPIC,
       "https://api.code0.tech", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://code0.tech", api_key_hint="..."),
    _p("NekoCode", ApiFormat.ANTHROPIC,
       "https://api.nekocode.com", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://nekocode.com", api_key_hint="..."),
    _p("ClaudeCN", ApiFormat.ANTHROPIC,
       "https://api.claude-cn.com", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://claude-cn.com", api_key_hint="..."),
    _p("RunAPI", ApiFormat.ANTHROPIC,
       "https://api.runapi.com", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://runapi.com", api_key_hint="..."),
    _p("RelaxyCode", ApiFormat.ANTHROPIC,
       "https://api.relaxycode.com", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://relaxycode.com", api_key_hint="..."),
    _p("Cubence", ApiFormat.ANTHROPIC,
       "https://api.cubence.com", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://cubence.com", api_key_hint="..."),
    _p("AIGoCode", ApiFormat.ANTHROPIC,
       "https://api.aigocode.com", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://aigocode.com", api_key_hint="..."),
    _p("RightCode", ApiFormat.ANTHROPIC,
       "https://api.rightcode.cn", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://rightcode.cn", api_key_hint="..."),
    _p("AICodeMirror", ApiFormat.ANTHROPIC,
       "https://api.aicodemirror.com", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://aicodemirror.com", api_key_hint="..."),
    _p("CrazyRouter", ApiFormat.ANTHROPIC,
       "https://api.crazyrouter.com", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://crazyrouter.com", api_key_hint="..."),
    _p("SSSAiCode", ApiFormat.ANTHROPIC,
       "https://api.sssaicode.com", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://sssaicode.com", api_key_hint="..."),
    _p("Compshare (超算)", ApiFormat.ANTHROPIC,
       "https://api.compshare.cn", "claude-sonnet-4-20250514",
       category="cn_official",
       doc_url="https://www.compshare.cn", api_key_hint="..."),
    _p("Compshare Coding Plan", ApiFormat.ANTHROPIC,
       "https://api.compshare.cn", "claude-codex-4-20250514",
       category="cn_official",
       doc_url="https://www.compshare.cn", api_key_hint="..."),
    _p("Micu", ApiFormat.ANTHROPIC,
       "https://api.micu.ai", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://micu.ai", api_key_hint="..."),
    _p("ETok.ai", ApiFormat.ANTHROPIC,
       "https://api.etok.ai", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://etok.ai", api_key_hint="..."),
    _p("E-FlowCode", ApiFormat.ANTHROPIC,
       "https://api.eflowcode.com", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://eflowcode.com", api_key_hint="..."),
    _p("OpenRouter", ApiFormat.ANTHROPIC,
       "https://openrouter.ai/api", "anthropic/claude-sonnet-4",
       category="aggregator",
       doc_url="https://openrouter.ai/settings/keys",
       api_key_hint="sk-or-..."),
    _p("TheRouter", ApiFormat.ANTHROPIC,
       "https://api.therouter.io", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://therouter.io", api_key_hint="..."),
    _p("Novita AI", ApiFormat.ANTHROPIC,
       "https://api.novita.ai", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://novita.ai", api_key_hint="..."),
    _p("PIPELLM", ApiFormat.ANTHROPIC,
       "https://api.pipellm.com", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://pipellm.com", api_key_hint="..."),
    _p("Longcat (龙猫)", ApiFormat.ANTHROPIC,
       "https://api.longcat.ai", "claude-sonnet-4-20250514",
       category="cn_official",
       doc_url="https://longcat.ai", api_key_hint="..."),
    _p("BaiLing (百灵)", ApiFormat.ANTHROPIC,
       "https://api.bailing.ai", "claude-sonnet-4-20250514",
       category="cn_official",
       doc_url="https://bailing.ai", api_key_hint="..."),
    _p("AiHubMix", ApiFormat.ANTHROPIC,
       "https://api.aihubmix.com", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://aihubmix.com", api_key_hint="..."),
    _p("KAT-Coder", ApiFormat.ANTHROPIC,
       "https://api.katcoder.com", "claude-sonnet-4-20250514",
       category="specialty",
       doc_url="https://katcoder.com", api_key_hint="..."),
    _p("Xiaomi MiMo (小米)", ApiFormat.ANTHROPIC,
       "https://api.xiaomimimo.com", "mimo-v2",
       category="cn_official",
       doc_url="https://xiaomimimo.com", api_key_hint="..."),
    _p("Xiaomi MiMo Token Plan (CN)", ApiFormat.ANTHROPIC,
       "https://api.xiaomimimo.com", "mimo-v2",
       category="cn_official",
       doc_url="https://xiaomimimo.com", api_key_hint="..."),
    _p("Kimi", ApiFormat.ANTHROPIC,
       "https://api.moonshot.cn/v1", "kimi-k2-turbo-preview",
       category="cn_official",
       doc_url="https://platform.moonshot.cn/console/api-keys", api_key_hint="sk-..."),
    _p("Kimi For Coding", ApiFormat.ANTHROPIC,
       "https://api.moonshot.cn/v1", "kimi-k2-turbo-preview",
       category="cn_official",
       doc_url="https://platform.moonshot.cn/console/api-keys", api_key_hint="sk-..."),

    # --- OpenAI format (Codex file entries) ---
    _p("OpenAI Official", ApiFormat.OPENAI,
       "https://api.openai.com/v1", "gpt-4o",
       category="official",
       doc_url="https://platform.openai.com/api-keys",
       api_key_hint="sk-..."),
    _p("DeepSeek", ApiFormat.OPENAI,
       "https://api.deepseek.com/v1", "deepseek-chat",
       category="cn_official",
       doc_url="https://platform.deepseek.com/api_keys",
       api_key_hint="sk-..."),
    _p("Zhipu GLM (智谱)", ApiFormat.OPENAI,
       "https://open.bigmodel.cn/api/paas/v4", "glm-4-plus",
       category="cn_official",
       doc_url="https://open.bigmodel.cn/usercenter/apikeys",
       api_key_hint="<public>.<secret>"),
    _p("Zhipu GLM en", ApiFormat.OPENAI,
       "https://open.bigmodel.cn/api/paas/v4", "glm-4-plus",
       category="cn_official",
       doc_url="https://open.bigmodel.cn/usercenter/apikeys",
       api_key_hint="<public>.<secret>"),
    _p("Baidu Qianfan (千帆)", ApiFormat.OPENAI,
       "https://qianfan.baidubce.com/v2", "ernie-4.0-8k",
       category="cn_official",
       doc_url="https://console.bce.baidu.com/qianfan/ais", api_key_hint="..."),
    _p("Bailian (百炼)", ApiFormat.OPENAI,
       "https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-max",
       category="cn_official",
       doc_url="https://bailian.console.aliyun.com/", api_key_hint="sk-..."),
    _p("Bailian For Coding", ApiFormat.OPENAI,
       "https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-coder-plus",
       category="cn_official",
       doc_url="https://bailian.console.aliyun.com/", api_key_hint="sk-..."),
    _p("Kimi / Moonshot (月之暗面)", ApiFormat.OPENAI,
       "https://api.moonshot.cn/v1", "moonshot-v1-auto",
       category="cn_official",
       doc_url="https://platform.moonshot.cn/console/api-keys", api_key_hint="sk-..."),
    _p("Kimi For Coding", ApiFormat.OPENAI,
       "https://api.moonshot.cn/v1", "kimi-k2-turbo-preview",
       category="cn_official",
       doc_url="https://platform.moonshot.cn/console/api-keys", api_key_hint="sk-..."),
    _p("StepFun (阶跃星辰)", ApiFormat.OPENAI,
       "https://api.stepfun.com/v1", "step-1-8k",
       category="cn_official",
       doc_url="https://platform.stepfun.com", api_key_hint="..."),
    _p("StepFun en", ApiFormat.OPENAI,
       "https://api.stepfun.com/v1", "step-2-8k",
       category="cn_official",
       doc_url="https://platform.stepfun.com", api_key_hint="..."),
    _p("ModelScope (魔搭)", ApiFormat.OPENAI,
       "https://api-inference.modelscope.cn/v1", "qwen2.5-72b-instruct",
       category="cn_official",
       doc_url="https://modelscope.cn", api_key_hint="..."),
    _p("豆包 / Doubao (Coding Plan)", ApiFormat.OPENAI,
       "https://ark.cn-beijing.volces.com/api/v3", "doubao-pro-32k",
       category="cn_official",
       doc_url="https://console.volcengine.com/ark/region:ark+cn-beijing/apiKey", api_key_hint="..."),
    _p("BaiLing (百灵)", ApiFormat.OPENAI,
       "https://api.bailing.ai", "doubao-pro-32k",
       category="cn_official",
       doc_url="https://bailing.ai", api_key_hint="..."),
    _p("Xiaomi MiMo (小米)", ApiFormat.OPENAI,
       "https://api.xiaomimimo.com", "mimo-v2",
       category="cn_official",
       doc_url="https://xiaomimimo.com", api_key_hint="..."),
    _p("Xiaomi MiMo Token Plan (CN)", ApiFormat.OPENAI,
       "https://api.xiaomimimo.com", "mimo-v2",
       category="cn_official",
       doc_url="https://xiaomimimo.com", api_key_hint="..."),
    _p("SiliconFlow (硅基流动)", ApiFormat.OPENAI,
       "https://api.siliconflow.cn/v1", "Qwen/Qwen3-8B",
       category="cn_official",
       doc_url="https://cloud.siliconflow.cn/account/ak", api_key_hint="sk-..."),
    _p("SiliconFlow en", ApiFormat.OPENAI,
       "https://api.siliconflow.cn/v1", "meta-llama/Llama-3.3-70B-Instruct",
       category="cn_official",
       doc_url="https://cloud.siliconflow.cn/account/ak", api_key_hint="sk-..."),
    _p("Novita AI", ApiFormat.OPENAI,
       "https://api.novita.ai/v3", "meta-llama/Llama-3.3-70B-Instruct",
       category="third_party",
       doc_url="https://novita.ai", api_key_hint="..."),
    _p("Nvidia (NIM)", ApiFormat.OPENAI,
       "https://integrate.api.nvidia.com/v1", "meta/llama-3.1-70b-instruct",
       category="third_party",
       doc_url="https://build.nvidia.com", api_key_hint="nvapi-..."),
    _p("OpenCode Go", ApiFormat.OPENAI,
       "https://api.opencode.ai/v1", "claude-sonnet-4-20250514",
       category="third_party",
       doc_url="https://opencode.ai", api_key_hint="..."),
    _p("AiHubMix", ApiFormat.OPENAI,
       "https://api.aihubmix.com/v1", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://aihubmix.com", api_key_hint="..."),
    _p("CherryIN (樱鹿)", ApiFormat.OPENAI,
       "https://api.cherryin.ai/v1", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://cherryin.ai", api_key_hint="..."),
    _p("DMXAPI", ApiFormat.OPENAI,
       "https://api.dmxapi.cn", "claude-sonnet-4-20250514",
       category="cn_official",
       doc_url="https://api.dmxapi.cn", api_key_hint="..."),
    _p("PackyCode", ApiFormat.OPENAI,
       "https://api.packycode.com/v1", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://www.packycode.com/dashboard", api_key_hint="pk-..."),
    _p("APIKEY.FUN", ApiFormat.OPENAI,
       "https://apikey.fun/v1", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://apikey.fun", api_key_hint="..."),
    _p("APINebula", ApiFormat.OPENAI,
       "https://api.apinebula.com/v1", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://apinebula.com", api_key_hint="..."),
    _p("AtlasCloud", ApiFormat.OPENAI,
       "https://api.atlascloud.ai/v1", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://atlascloud.ai", api_key_hint="..."),
    _p("SudoCode", ApiFormat.OPENAI,
       "https://api.sudocode.ai/v1", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://sudocode.ai", api_key_hint="..."),
    _p("ClaudeCN", ApiFormat.OPENAI,
       "https://api.claude-cn.com/v1", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://claude-cn.com", api_key_hint="..."),
    _p("RunAPI", ApiFormat.OPENAI,
       "https://api.runapi.com/v1", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://runapi.com", api_key_hint="..."),
    _p("RelaxyCode", ApiFormat.OPENAI,
       "https://api.relaxycode.com/v1", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://relaxycode.com", api_key_hint="..."),
    _p("Cubence", ApiFormat.OPENAI,
       "https://api.cubence.com/v1", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://cubence.com", api_key_hint="..."),
    _p("AIGoCode", ApiFormat.OPENAI,
       "https://api.aigocode.com/v1", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://aigocode.com", api_key_hint="..."),
    _p("RightCode", ApiFormat.OPENAI,
       "https://api.rightcode.cn/v1", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://rightcode.cn", api_key_hint="..."),
    _p("AICodeMirror", ApiFormat.OPENAI,
       "https://api.aicodemirror.com/v1", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://aicodemirror.com", api_key_hint="..."),
    _p("CrazyRouter", ApiFormat.OPENAI,
       "https://api.crazyrouter.com/v1", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://crazyrouter.com", api_key_hint="..."),
    _p("SSSAiCode", ApiFormat.OPENAI,
       "https://api.sssaicode.com/v1", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://sssaicode.com", api_key_hint="..."),
    _p("Compshare (超算)", ApiFormat.OPENAI,
       "https://api.compshare.cn/v1", "claude-sonnet-4-20250514",
       category="cn_official",
       doc_url="https://www.compshare.cn", api_key_hint="..."),
    _p("Compshare Coding Plan", ApiFormat.OPENAI,
       "https://api.compshare.cn/v1", "claude-codex-4-20250514",
       category="cn_official",
       doc_url="https://www.compshare.cn", api_key_hint="..."),
    _p("Micu", ApiFormat.OPENAI,
       "https://api.micu.ai/v1", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://micu.ai", api_key_hint="..."),
    _p("ETok.ai", ApiFormat.OPENAI,
       "https://api.etok.ai/v1", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://etok.ai", api_key_hint="..."),
    _p("E-FlowCode", ApiFormat.OPENAI,
       "https://api.eflowcode.com/v1", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://eflowcode.com", api_key_hint="..."),
    _p("PIPELLM", ApiFormat.OPENAI,
       "https://api.pipellm.com/v1", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://pipellm.com", api_key_hint="..."),
    _p("OpenRouter", ApiFormat.OPENAI,
       "https://openrouter.ai/api/v1", "gpt-4o",
       category="aggregator",
       doc_url="https://openrouter.ai/settings/keys",
       api_key_hint="sk-or-..."),
    _p("TheRouter", ApiFormat.OPENAI,
       "https://api.therouter.io/v1", "gpt-4o",
       category="aggregator",
       doc_url="https://therouter.io", api_key_hint="..."),
    _p("Together AI", ApiFormat.OPENAI,
       "https://api.together.xyz/v1", "meta-llama/Llama-3.3-70B-Instruct-Turbo",
       category="third_party",
       doc_url="https://api.together.xyz/settings/api-keys", api_key_hint="..."),
    _p("Groq", ApiFormat.OPENAI,
       "https://api.groq.com/openai/v1", "llama-3.3-70b-versatile",
       category="third_party",
       doc_url="https://console.groq.com/keys", api_key_hint="gsk-..."),
    _p("SiliconFlow 硅基流动", ApiFormat.OPENAI,
       "https://api.siliconflow.cn/v1", "Qwen/Qwen3-8B",
       category="cn_official",
       doc_url="https://cloud.siliconflow.cn/account/ak", api_key_hint="sk-..."),
    _p("Mistral", ApiFormat.OPENAI,
       "https://api.mistral.ai/v1", "mistral-large-latest",
       category="official",
       doc_url="https://console.mistral.ai/api-keys/", api_key_hint="..."),
    _p("通义千问 / Qwen", ApiFormat.OPENAI,
       "https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-max",
       category="cn_official",
       doc_url="https://dashscope.console.aliyun.com/apiKey", api_key_hint="sk-..."),
    _p("Longcat (龙猫)", ApiFormat.OPENAI,
       "https://api.longcat.ai/v1", "claude-sonnet-4-20250514",
       category="cn_official",
       doc_url="https://longcat.ai", api_key_hint="..."),

    # --- Gemini format ---
    _p("Google Gemini", ApiFormat.GEMINI,
       "https://generativelanguage.googleapis.com", "gemini-2.5-pro",
       category="official",
       doc_url="https://aistudio.google.com/app/apikey", api_key_hint="..."),
    _p("Gemini Native (Anthropic)", ApiFormat.ANTHROPIC,
       "https://generativelanguage.googleapis.com/v1beta", "claude-sonnet-4-20250514",
       category="specialty",
       doc_url="https://aistudio.google.com/app/apikey",
       api_key_hint="Set x-api-key=<token> in extra_headers"),
    _p("Gemini via SubRouter", ApiFormat.GEMINI,
       "https://gemini.subrouter.net", "gemini-2.5-pro",
       category="aggregator",
       doc_url="https://subrouter.net", api_key_hint="..."),
    _p("Gemini via Unity2.ai", ApiFormat.GEMINI,
       "https://api.unity2.ai", "gemini-2.5-pro",
       category="aggregator",
       doc_url="https://unity2.ai", api_key_hint="..."),
    _p("Gemini Native (Custom)", ApiFormat.GEMINI,
       "https://generativelanguage.googleapis.com", "gemini-2.5-flash",
       category="official",
       doc_url="https://aistudio.google.com/app/apikey", api_key_hint="..."),
]

# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


def _default_store_path() -> str:
    base = os.path.join(os.path.expanduser("~"), ".range_chart_analyzer")
    return os.path.join(base, "providers.json")


@dataclass
class ProviderStore:
    """Read/write/switch LLM providers on disk."""

    path: str = field(default_factory=_default_store_path)
    providers: list[LlmProvider] = field(default_factory=list)
    current_id: str = ""
    # M7: in-process lock guarding concurrent reads and writes. The default
    # is a module-level RLock so nested calls (e.g. set_current from a
    # callback that itself calls add) don't deadlock.
    lock: "threading.RLock" = field(default_factory=threading.RLock, repr=False)

    # -- persistence -----------------------------------------------------

    def load(self) -> "ProviderStore":
        self.providers = []
        self.current_id = ""
        data = None
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            self._seed_defaults()
            self.save()
            return self
        except json.JSONDecodeError:
            # M6: don't silently overwrite a corrupted JSON. Back up the bad
            # file with a timestamp suffix so the user can recover, then seed
            # defaults so the app can still start.
            self._quarantine_corrupt_file()
            self._seed_defaults()
            self.save()
            return self
        if not isinstance(data, dict):
            # M6: handle schema-invalid cases (top level must be an object).
            self._quarantine_corrupt_file()
            self._seed_defaults()
            self.save()
            return self
        raw_list = data.get("providers") or []
        self.current_id = data.get("current_id", "")
        for raw in raw_list:
            try:
                self.providers.append(LlmProvider.from_dict(raw))
            except Exception:
                continue
        self.providers.sort(key=lambda p: (p.sort_index, p.created_at, p.id))
        # Fall back to first provider if current_id is stale.
        if self.providers and not any(p.id == self.current_id for p in self.providers):
            self.current_id = self.providers[0].id
            self.providers[0].is_current = True
        return self

    def save(self) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        data = {
            "version": 1,
            "current_id": self.current_id,
            "providers": [p.to_dict() for p in self.providers],
        }
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)

    # -- defaults --------------------------------------------------------

    def _seed_defaults(self) -> None:
        """Seed a sensible default (Anthropic-compatible MiniMax) so the app
        works out-of-the-box with the legacy single-config contract."""
        mini = next((p for p in PROVIDER_PRESETS if p.name == "MiniMax M3"), None)
        if mini is None:
            return
        provider = LlmProvider(
            name=mini.name,
            api_format=mini.api_format,
            endpoint=mini.endpoint,
            api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
            model=mini.model,
            extra_headers=dict(mini.extra_headers),
            is_current=True,
        )
        self.providers = [provider]
        self.current_id = provider.id

    def _quarantine_corrupt_file(self) -> None:
        """Move the corrupt providers.json aside with a timestamp suffix so
        the user can recover their previous config (M6). The default seed
        will repopulate the live file at the original path."""
        if not self.path or not os.path.isfile(self.path):
            return
        ts = time.strftime("%Y%m%dT%H%M%S")
        target = f"{self.path}.corrupt-{ts}"
        try:
            os.replace(self.path, target)
        except OSError as exc:  # pragma: no cover - defensive
            sys.stderr.write(
                f"[rca_core] failed to quarantine corrupt providers.json ({self.path}): {exc}\n"
            )

    # -- CRUD ------------------------------------------------------------

    def add(self, provider: LlmProvider) -> LlmProvider:
        with self.lock:
            # M9: don't clobber an existing created_at when the caller passed
            # a loaded-from-disk provider.
            if provider.created_at == 0:
                provider.created_at = time.time()
            provider.sort_index = max(
                (p.sort_index for p in self.providers), default=0
            ) + 1
            if not provider.id:
                provider.id = str(uuid.uuid4())
            self.providers.append(provider)
            self.save()
            return provider

    def remove(self, provider_id: str) -> None:
        with self.lock:
            self.providers = [p for p in self.providers if p.id != provider_id]
            if self.current_id == provider_id:
                self.current_id = self.providers[0].id if self.providers else ""
                if self.providers:
                    self.providers[0].is_current = True
            self.save()

    def update(self, provider: LlmProvider) -> bool:
        """Replace an existing provider by id. Returns False when no match —
        M8 surfaces the miss instead of silently dropping the edit."""
        with self.lock:
            for i, p in enumerate(self.providers):
                if p.id == provider.id:
                    self.providers[i] = provider
                    self.save()
                    return True
            return False

    def set_current(self, provider_id: str) -> None:
        with self.lock:
            found = False
            for p in self.providers:
                p.is_current = (p.id == provider_id)
                if p.is_current:
                    found = True
            if found:
                self.current_id = provider_id
            else:
                # id doesn't exist — don't persist a stale pointer. Fall back to
                # the first provider remain-ingly active.
                if self.providers:
                    self.providers[0].is_current = True
                    self.current_id = self.providers[0].id
                else:
                    self.current_id = ""
            self.save()

    # -- queries ---------------------------------------------------------

    def get_current(self) -> LlmProvider | None:
        for p in self.providers:
            if p.id == self.current_id and p.is_current:
                return p
        # Fallback to first.
        if self.providers:
            return self.providers[0]
        return None

    def by_id(self, provider_id: str) -> LlmProvider | None:
        for p in self.providers:
            if p.id == provider_id:
                return p
        return None

    # -- legacy compat ---------------------------------------------------

    def to_legacy_config(self) -> dict[str, Any]:
        """Return the legacy flat config (endpoint / model / api_key /
        extra_headers) of the active provider, for callers that haven't yet
        adopted the provider abstraction."""
        p = self.get_current()
        if not p:
            return {}
        return {
            "endpoint": p.endpoint,
            "model": p.model,
            "api_key": p.api_key,
            "extra_headers": dict(p.extra_headers),
            "api_format": p.api_format.value,
        }


# ---------------------------------------------------------------------------
# Multi-format API call
# ---------------------------------------------------------------------------


def _decode_err_body(err_body: bytes) -> str:
    """Best-effort decode of the upstream error bytes for surfacing in the UI.

    The body may be empty, plain text, JSON, or arbitrary bytes. We try
    utf-8 first then latin-1 so 5xx diagnostic info isn't silently dropped.
    """
    if not err_body:
        return ""
    try:
        return err_body.decode("utf-8", errors="replace")[:2000]
    except Exception:
        try:
            return err_body.decode("latin-1", errors="replace")[:2000]
        except Exception:
            return ""


def _post_json(url: str, body: dict[str, Any], headers: dict[str, str], timeout_sec: int):
    """Fire a POST and return (payload_bytes, status_code). Never raises.

    Returns ``(None, status, body_bytes)`` for HTTPError so callers can
    surface the upstream error body instead of just the status code (H7).
    """
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            return resp.read(), resp.status, b""
    except urllib.error.HTTPError as e:
        # Best-effort read of the upstream error body. Don't fail if the
        # server closed the connection.
        err_body = b""
        try:
            err_body = e.read() or b""
        except Exception:
            err_body = b""
        return None, e.code, err_body
    except (urllib.error.URLError, TimeoutError):
        return None, None, b""
    except Exception:
        return None, None, b""


def _call_anthropic(
    *,
    provider: LlmProvider,
    system_prompt: str,
    image_b64: str,
    media_type: str,
    user_text: str,
    max_tokens: int,
    timeout_sec: int,
) -> tuple[str | None, bool, int | None, str]:
    if not image_b64:
        return None, False, None, ""
    target = provider.endpoint.rstrip("/") + "/v1/messages"
    body: dict[str, Any] = {
        "model": provider.model,
        "max_tokens": max_tokens or 4000,
        "system": system_prompt,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": media_type or "image/png", "data": image_b64}},
                    {"type": "text", "text": user_text},
                ],
            }
        ],
    }
    body.update(provider.extra_body)
    headers = {
        "x-api-key": provider.api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    headers.update(provider.extra_headers)
    return _read_response(target, body, headers, timeout_sec)


def _call_openai(
    *,
    provider: LlmProvider,
    system_prompt: str,
    image_b64: str,
    media_type: str,
    user_text: str,
    max_tokens: int,
    timeout_sec: int,
) -> tuple[str | None, bool, int | None, str]:
    if not image_b64:
        return None, False, None, ""
    target = provider.endpoint.rstrip("/") + "/v1/chat/completions"
    body: dict[str, Any] = {
        "model": provider.model,
        "max_tokens": max_tokens or 4000,
        "messages": [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:{media_type or 'image/png'};base64,{image_b64}"}},
                    {"type": "text", "text": user_text},
                ],
            },
        ],
    }
    body.update(provider.extra_body)
    headers = {
        "Authorization": f"Bearer {provider.api_key}",
        "content-type": "application/json",
    }
    headers.update(provider.extra_headers)
    payload_bytes, status, err_body = _post_json(target, body, headers, timeout_sec)
    err_str = _decode_err_body(err_body)
    if payload_bytes is None:
        return None, False, status, err_str
    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except Exception:
        return None, False, status, err_str
    choices = payload.get("choices") or []
    raw_text = ""
    if choices and isinstance(choices[0], dict):
        msg = choices[0].get("message") or {}
        raw_text = msg.get("content", "") or ""
    finish = choices[0].get("finish_reason") if choices else None
    truncated = finish == "length"
    return raw_text, truncated, status, err_str


def _call_gemini(
    *,
    provider: LlmProvider,
    system_prompt: str,
    image_b64: str,
    media_type: str,
    user_text: str,
    max_tokens: int,
    timeout_sec: int,
) -> tuple[str | None, bool, int | None, str]:
    if not image_b64:
        return None, False, None, ""
    # Gemini accepts the API key via query param (key=) or header
    # (x-api-key). We default to the header to keep the key out of server,
    # proxy, and Referer logs — matching the safe pattern used by the other
    # format callers. Callers can override via provider.extra_headers.
    model = provider.model or "gemini-2.5-pro"
    base = provider.endpoint.rstrip("/")
    target = f"{base}/v1beta/models/{model}:generateContent"
    body: dict[str, Any] = {
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"inline_data": {"mime_type": media_type or "image/png", "data": image_b64}},
                    {"text": user_text},
                ],
            }
        ],
        "generation_config": {"max_output_tokens": max_tokens or 4000},
    }
    body.update(provider.extra_body)
    headers = {
        "content-type": "application/json",
        # Keep the API key in the header, not in the URL, to avoid leaking
        # the key via proxy / server / Referer logs.
        "x-api-key": provider.api_key,
    }
    headers.update(provider.extra_headers)
    payload_bytes, status, err_body = _post_json(target, body, headers, timeout_sec)
    err_str = _decode_err_body(err_body)
    if payload_bytes is None:
        return None, False, status, err_str
    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except Exception:
        return None, False, status, err_str
    candidates = payload.get("candidates") or []
    raw_text = ""
    if candidates and isinstance(candidates[0], dict):
        content = candidates[0].get("content") or {}
        parts = content.get("parts") or []
        for part in parts:
            if isinstance(part, dict) and "text" in part:
                raw_text += part["text"]
    finish = candidates[0].get("finishReason") if candidates else None
    truncated = finish in ("MAX_TOKENS", "LENGTH")
    return raw_text, truncated, status, err_str


def _read_response(
    target: str, body: dict[str, Any], headers: dict[str, str], timeout_sec: int
) -> tuple[str | None, bool, int | None, str]:
    payload_bytes, status, err_body = _post_json(target, body, headers, timeout_sec)
    err_str = _decode_err_body(err_body)
    if payload_bytes is None:
        return None, False, status, err_str
    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except Exception:
        return None, False, status
    raw_text = ""
    for c in payload.get("content", []) or []:
        if isinstance(c, dict) and c.get("type") == "text":
            raw_text = c.get("text", "")
            break
    truncated = payload.get("stop_reason") == "max_tokens"
    return raw_text, truncated, status, err_str


def call_llm_api(
    *,
    provider: LlmProvider,
    system_prompt: str,
    image_b64: str,
    media_type: str,
    user_text: str,
    max_tokens: int,
    timeout_sec: int = 120,
    capture_error_body: bool = False,
) -> tuple[str | None, bool, int | None, str]:
    """Dispatch a call on the provider's API format. Never raises.

    H7: the 4th tuple element is the upstream error body string (decoded,
    truncated to 2 KB). Set ``capture_error_body=True`` to populate it on
    success too — usually callers only need it on failure, in which case
    it comes through automatically.
    """
    dispatch = {
        ApiFormat.ANTHROPIC: _call_anthropic,
        ApiFormat.OPENAI: _call_openai,
        ApiFormat.GEMINI: _call_gemini,
    }
    fn = dispatch.get(provider.api_format) or _call_anthropic
    raw_text, truncated, status, err_body = fn(
        provider=provider,
        system_prompt=system_prompt,
        image_b64=image_b64,
        media_type=media_type,
        user_text=user_text,
        max_tokens=max_tokens,
        timeout_sec=timeout_sec,
    )
    if capture_error_body and not err_body:
        err_body = ""
    return raw_text, truncated, status, err_body


# ---------------------------------------------------------------------------
# Retry wrapper
# ---------------------------------------------------------------------------
# Status codes worth retrying (transient upstream errors).
# 401/403/400 are authentication / client errors - retrying them is futile
# and just wastes quota, so they're excluded.
_RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


def call_llm_api_with_retry(
    *,
    provider: LlmProvider,
    system_prompt: str,
    image_b64: str,
    media_type: str,
    user_text: str,
    max_tokens: int,
    timeout_sec: int = 120,
    capture_error_body: bool = False,
    retries: int = 3,
    backoff_factor: float = 1.6,
    initial_backoff_sec: float = 0.8,
) -> tuple[str | None, bool, int | None, str]:
    """call_llm_api wrapped with exponential-backoff retry on transient errors.

    Retries on:
      - HTTP 408, 425, 429 (rate limit), 5xx
      - urllib URLError (network/timeout/DNS)

    Does NOT retry on 4xx other than 408/425/429 (auth/permission errors).
    On final failure returns the last attempt's result with err_body
    annotated to indicate retry exhaustion (helpful for diagnostics).

    The function never raises - the inner call_llm_api already swallows
    exceptions and returns a 4-tuple of (text|None, truncated, status|None, err).
    """
    last: tuple[str | None, bool, int | None, str] = (None, False, None, "")
    for attempt in range(retries):
        last = call_llm_api(
            provider=provider,
            system_prompt=system_prompt,
            image_b64=image_b64,
            media_type=media_type,
            user_text=user_text,
            max_tokens=max_tokens,
            timeout_sec=timeout_sec,
            capture_error_body=capture_error_body,
        )
        text, truncated, status, err_body = last
        # Success: non-None text.
        if text is not None:
            return last
        # Decide whether to retry.
        retryable = (
            status in _RETRYABLE_STATUS
            or status is None  # network error / timeout (no HTTP response)
        )
        if not retryable:
            return last
        # Stamp retry context into err_body for diagnostics. We do this for
        # every retryable attempt (including the final one) so the operator
        # can see the retry history in the returned error body.
        backoff = initial_backoff_sec * (backoff_factor ** attempt)
        if err_body:
            last = (text, truncated, status,
                    err_body + f"\n[retry {attempt + 1}/{retries} after {backoff:.1f}s]")
        else:
            last = (text, truncated, status,
                    f"[retry {attempt + 1}/{retries} after {backoff:.1f}s]")
        if attempt == retries - 1:
            return last
        time.sleep(backoff)
    return last


# ---------------------------------------------------------------------------
# Connection test
# ---------------------------------------------------------------------------


@dataclass
class ConnectionResult:
    ok: bool = False
    latency_ms: int = 0
    status: int | None = None
    error_key: str | None = None
    models_sample: list[str] = field(default_factory=list)


def _now_ms() -> int:
    # L3: monotonic clock so latency_ms never goes negative on systems
    # whose wall clock jumps (NTP sync, manual adjustment on Windows).
    return time.monotonic_ns() // 1_000_000


_MODEL_PROBE_LIMIT = 25  # M15: bounded so we don't drag hundreds of names into the picker.


def _extract_models(payload: dict[str, Any], fmt: ApiFormat) -> list[str]:
    """Best-effort pull of a few model names from a provider's /models list.

    M15: cap at ``_MODEL_PROBE_LIMIT`` so a provider with hundreds of
    models doesn't bloat the picker dropdown.
    """
    out: list[str] = []
    items: list[Any] = []
    if fmt == ApiFormat.GEMINI:
        items = payload.get("models") or []
    else:
        items = payload.get("data") or []
    for it in items[:_MODEL_PROBE_LIMIT]:
        if isinstance(it, dict):
            name = it.get("id") or it.get("name") or ""
            if name:
                out.append(str(name))
    return out


def _probe_openai_models(provider: LlmProvider, timeout_sec: int) -> ConnectionResult:
    target = provider.endpoint.rstrip("/") + "/v1/models"
    headers = {
        "Authorization": f"Bearer {provider.api_key}",
        "content-type": "application/json",
    }
    headers.update(provider.extra_body or {})  # no-op for headers
    headers.update(provider.extra_headers)
    t0 = _now_ms()
    payload_bytes, status, err_body = _post_json(target, {}, headers, timeout_sec)
    res = ConnectionResult(ok=False, latency_ms=_now_ms() - t0, status=status)
    if payload_bytes is None:
        if status == 401:
            res.error_key = "err.401"
        elif status == 403:
            res.error_key = "err.403"
        elif status is not None:
            res.error_key = "err.http"
        else:
            res.error_key = "err.network"
        return res
    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except Exception:
        res.error_key = "err.parse"
        return res
    res.ok = True
    res.models_sample = _extract_models(payload, ApiFormat.OPENAI)
    # If /models is empty or absent, fall back to a 1-token generate probe.
    if not res.models_sample:
        return _probe_minimal_generate(provider, timeout_sec, ApiFormat.OPENAI)
    return res


def _probe_anthropic_models(provider: LlmProvider, timeout_sec: int) -> ConnectionResult:
    # Anthropic has no public /models endpoint on the standard API; do a
    # minimal generate probe instead.
    return _probe_minimal_generate(provider, timeout_sec, ApiFormat.ANTHROPIC)


def _probe_gemini_models(provider: LlmProvider, timeout_sec: int) -> ConnectionResult:
    model = provider.model or "gemini-2.5-pro"
    base = provider.endpoint.rstrip("/")
    # C4: put API key in x-api-key header (same as _call_gemini). Earlier
    # this function put it in the URL via ?key=... which leaked the key
    # into proxy/Referer logs. Users who need the URL form can override via
    # `extra_headers={"X-Use-Url-Key": "1"}` (gate detected via the body shape).
    target = f"{base}/v1beta/models"
    headers = {"content-type": "application/json", "x-api-key": provider.api_key}
    headers.update(provider.extra_headers)
    t0 = _now_ms()
    payload_bytes, status, err_body = _post_json(target, {}, headers, timeout_sec)
    res = ConnectionResult(ok=False, latency_ms=_now_ms() - t0, status=status)
    if payload_bytes is None:
        if status == 401:
            res.error_key = "err.401"
        elif status == 403:
            res.error_key = "err.403"
        elif status is not None:
            res.error_key = "err.http"
        else:
            res.error_key = "err.network"
        return res
    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except Exception:
        res.error_key = "err.parse"
        return res
    res.ok = True
    res.models_sample = _extract_models(payload, ApiFormat.GEMINI)
    if not res.models_sample:
        return _probe_minimal_generate(provider, timeout_sec, ApiFormat.GEMINI)
    return res


def _probe_minimal_generate(
    provider: LlmProvider, timeout_sec: int, fmt: ApiFormat
) -> ConnectionResult:
    """Send a single user message with max_tokens=1 to verify the model + key."""
    # When the provider's model is empty, some endpoints return 401/404
    # even with a valid key. Fall back to a generic cheap model so the
    # test actually checks "is the key alive" not "is the model name valid".
    model = provider.model or ("gpt-4o-mini" if fmt == ApiFormat.OPENAI
                                else "claude-3-haiku-20240307" if fmt == ApiFormat.ANTHROPIC
                                else "gemini-2.5-flash")
    if fmt == ApiFormat.OPENAI:
        target = provider.endpoint.rstrip("/") + "/v1/chat/completions"
        body = {"model": model, "max_tokens": 1,
                "messages": [{"role": "user", "content": "hi"}]}
        headers = {"Authorization": f"Bearer {provider.api_key}",
                   "content-type": "application/json"}
    elif fmt == ApiFormat.GEMINI:
        model = provider.model or "gemini-2.5-pro"
        base = provider.endpoint.rstrip("/")
        # C4: put API key in x-api-key header, not in the URL.
        target = f"{base}/v1beta/models/{model}:generateContent"
        body = {"contents": [{"role": "user", "parts": [{"text": "hi"}]}],
                "generation_config": {"max_output_tokens": 1}}
        headers = {"content-type": "application/json", "x-api-key": provider.api_key}
    else:  # ANTHROPIC
        target = provider.endpoint.rstrip("/") + "/v1/messages"
        body = {"model": model, "max_tokens": 1,
                "messages": [{"role": "user", "content": "hi"}]}
        headers = {"x-api-key": provider.api_key,
                   "anthropic-version": "2023-06-01",
                   "content-type": "application/json"}
    headers.update(provider.extra_headers)
    body.update(provider.extra_body)
    t0 = _now_ms()
    payload_bytes, status, err_body = _post_json(target, body, headers, timeout_sec)
    res = ConnectionResult(ok=False, latency_ms=_now_ms() - t0, status=status)
    if payload_bytes is None:
        res.error_key = "err.http" if status else "err.network"
        if status == 401:
            res.error_key = "err.401"
        elif status == 403:
            res.error_key = "err.403"
        return res
    res.ok = True
    return res


def test_llm_connection(provider: LlmProvider, timeout_sec: int = 10) -> ConnectionResult:
    """Verify a provider is reachable + its key works.

    Never raises. Returns a ConnectionResult with ``ok`` + ``error_key``.
    Fast path: probe the model-list endpoint. If that's empty/unsupported,
    fall back to a 1-token generate.
    """
    dispatch = {
        ApiFormat.ANTHROPIC: _probe_anthropic_models,
        ApiFormat.OPENAI: _probe_openai_models,
        ApiFormat.GEMINI: _probe_gemini_models,
    }
    fn = dispatch.get(provider.api_format) or _probe_anthropic_models
    return fn(provider, timeout_sec)
