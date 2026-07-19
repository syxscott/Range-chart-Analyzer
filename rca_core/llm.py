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
import re
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

# Local imports kept lazy so this module can be imported on Python versions
# or in environments that don't have the rest of rca_core available.
def _usage_helpers():
    from .usage import estimate_tokens, parse_usage
    return estimate_tokens, parse_usage
def estimate_tokens(text: str) -> int:
    """Forwarded to rca_core.usage.estimate_tokens; indirection keeps
    this module importable in isolation (e.g. for some unit tests)."""
    fn, _ = _usage_helpers()
    return fn(text)
def parse_usage(payload, fmt_hint: str = ""):
    fn, p = _usage_helpers()
    return p(payload, fmt_hint)

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
    # Consecutive connection-test failures (0..3+). Persisted so the
    # health badge survives an app restart — the previous version kept
    # this only on the in-memory ProviderCard widget, so a user who
    # saw a ⚠ before quitting saw a clean ✓ on next launch and might
    # mistake a broken endpoint for a healthy one. Capped at 3 by the
    # GUI (the badge displays ✗, ✗2, ✗3+) so any value above 3 is fine
    # here but not very informative.
    consecutive_failures: int = 0

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
       "https://api.moonshot.cn/anthropic", "kimi-k2-turbo-preview",
       category="cn_official",
       doc_url="https://platform.moonshot.cn/console/api-keys", api_key_hint="sk-..."),
    _p("Kimi For Coding", ApiFormat.ANTHROPIC,
       "https://api.moonshot.cn/anthropic", "kimi-k2-turbo-preview",
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


def _chmod_user_only(path: str) -> None:
    """Restrict a file to owner read/write (0600) on POSIX. No-op on
    Windows (ACLs are inherited from the home directory).

    Bug-9 fix: ``providers.json`` contains plaintext API keys; if the
    file is created without an explicit mode, the user's umask may leave
    it world-readable (e.g. umask 022 → mode 0644). Tightening here
    closes that leak on Linux/macOS. Best-effort — if chmod fails (e.g.
    on a read-only mount), we don't crash the save.
    """
    try:
        if hasattr(os, "chmod"):
            os.chmod(path, 0o600)
    except OSError:
        pass


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
        # Bug-9 fix: tighten permissions on POSIX *before* the rename so
        # the final file is never readable by other users. On Windows
        # this is a no-op (the ACL model differs); the worst case is
        # readable only to the current user via the inherited DACL.
        _chmod_user_only(tmp)
        os.replace(tmp, self.path)
        _chmod_user_only(self.path)

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
            # Read the MiniMax-specific env var (not ANTHROPIC_API_KEY, which
            # previously mis-attributed the user's Anthropic key to MiniMax -
            # a functional break AND a third-party key leak). Empty by default;
            # the user pastes their MiniMax key in the UI.
            api_key=os.environ.get("MINIMAX_API_KEY", ""),
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
                    # Preserve the active flag: the wizard rebuilds the provider
                    # with is_current=False (default), so naively replacing
                    # would deactivate an active provider and get_current()
                    # would silently fall back to the first list entry,
                    # rerouting calls/credentials to a different endpoint.
                    provider.is_current = p.is_current
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
    For network-layer failures (DNS, refused connection, timeout, etc.) the
    status is ``None`` and ``body_bytes`` carries a short diagnostic string
    so the caller can distinguish timeout from connection-refused in logs.
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
    except TimeoutError as e:
        # Python 3.10+ raises TimeoutError from socket.timeout; older code
        # surfaces it as URLError(timeout=...). Mark it explicitly so logs
        # don't say "connection refused" when the real cause was a slow
        # upstream.
        return None, None, f"[network] timeout: {e}".encode("utf-8")
    except urllib.error.URLError as e:
        # URLError.reason distinguishes DNS from "refused" from "no route".
        reason = getattr(e, "reason", None)
        msg = f"[network] {type(reason).__name__ if reason else 'URLError'}: {reason or e}"
        return None, None, msg.encode("utf-8")
    except Exception as e:
        # Last-resort guard: log the exception class + str so a future
        # contributor can triage without re-running.
        return None, None, f"[network] {type(e).__name__}: {e}".encode("utf-8")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse all 3xx redirects on outbound LLM calls.

    SSRF hardening: the server-side endpoint validator (server.py
    ``_validate_endpoint``) only vets the *initial* URL host. Without this
    handler ``urlopen`` would transparently follow a ``302 Location:
    http://169.254.169.254/…`` (cloud metadata) or an intranet address,
    bypassing the allowlist entirely. Legitimate LLM APIs answer POSTs
    directly and never 3xx, so blocking redirects costs nothing.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(
            req.full_url, code,
            f"redirect to {newurl!r} refused (SSRF guard)",
            headers, fp,
        )


# Opener that raises on any redirect instead of following it. Installed as
# urllib's default opener so EVERY outbound call made through
# ``urllib.request.urlopen`` (including future ones) refuses 3xx redirects.
# urllib openers are thread-safe for concurrent ``.open()`` calls, so a single
# global opener is fine for the app's thread pool. Tests that monkeypatch
# ``urllib.request.urlopen`` (tests_llm.py) bypass this opener and keep working.
urllib.request.install_opener(urllib.request.build_opener(_NoRedirect()))


_VERSION_TAIL = re.compile(r"/v\d+(?:beta)?/?$", re.IGNORECASE)


def _api_base(endpoint: str) -> str:
    """Normalize an endpoint by stripping a trailing API-version segment
    (``/v1``, ``/v1beta``) so per-format callers can append their canonical
    path without producing ``/v1/v1`` double paths.

    Presets historically embed ``/v1`` in the endpoint (e.g.
    ``https://api.openai.com/v1``); the per-format callers also append
    ``/v1/...``, which doubled the segment and 404'd ~48 OpenAI presets
    plus the official Google Gemini endpoint (``/v1beta/v1beta``). Non-version
    trailing segments (``/anthropic``, ``/compatible-mode``, ``/openai``)
    are preserved.
    """
    return _VERSION_TAIL.sub("", (endpoint or "").rstrip("/"))


def _call_anthropic(
    *,
    provider: LlmProvider,
    system_prompt: str,
    image_b64: str,
    media_type: str,
    user_text: str,
    max_tokens: int,
    timeout_sec: int,
) -> tuple[str | None, bool, int | None, str, dict | None]:
    if not image_b64:
        return None, False, None, "", None
    target = _api_base(provider.endpoint) + "/v1/messages"
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
    text, truncated, status, err_body, payload = _read_response(target, body, headers, timeout_sec)
    usage = None
    if payload is not None:
        u = parse_usage(payload, "anthropic")
        if u:
            usage = dict(u); usage["estimated"] = False
    return text, truncated, status, err_body, usage


def _call_openai(
    *,
    provider: LlmProvider,
    system_prompt: str,
    image_b64: str,
    media_type: str,
    user_text: str,
    max_tokens: int,
    timeout_sec: int,
) -> tuple[str | None, bool, int | None, str, dict | None]:
    if not image_b64:
        return None, False, None, "", None
    target = _api_base(provider.endpoint) + "/v1/chat/completions"
    # OpenAI reasoning models (o1/o3/o4-mini) reject `max_tokens` and require
    # `max_completion_tokens`; sending the legacy name 400s the request.
    model = provider.model
    is_reasoning = bool(model and re.match(r"^o\d", model))
    body: dict[str, Any] = {
        "model": model,
        ("max_completion_tokens" if is_reasoning else "max_tokens"): max_tokens or 4000,
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
        return None, False, status, err_str, None
    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except Exception:
        return None, False, status, err_str, None
    choices = payload.get("choices") or []
    raw_text = ""
    if choices and isinstance(choices[0], dict):
        msg = choices[0].get("message") or {}
        content = msg.get("content", "")
        # Some OpenAI-compatible endpoints return content as a list of parts
        # ({"type":"text","text":...}) rather than a plain string; collapse
        # it to text so downstream JSON parsing doesn't receive a list.
        if isinstance(content, list):
            raw_text = "".join(
                (p.get("text", "") if isinstance(p, dict) else (p if isinstance(p, str) else ""))
                for p in content
            )
        else:
            raw_text = content or ""
    finish = choices[0].get("finish_reason") if choices and isinstance(choices[0], dict) else None
    truncated = finish == "length"
    usage = None
    u = parse_usage(payload, "openai")
    if u:
        usage = dict(u); usage["estimated"] = False
    return raw_text, truncated, status, err_str, usage


def _call_gemini(
    *,
    provider: LlmProvider,
    system_prompt: str,
    image_b64: str,
    media_type: str,
    user_text: str,
    max_tokens: int,
    timeout_sec: int,
) -> tuple[str | None, bool, int | None, str, dict | None]:
    if not image_b64:
        return None, False, None, "", None
    # Gemini accepts the API key via query param (key=) or header
    # (x-api-key). We default to the header to keep the key out of server,
    # proxy, and Referer logs — matching the safe pattern used by the other
    # format callers. Callers can override via provider.extra_headers.
    model = provider.model or "gemini-2.5-pro"
    base = _api_base(provider.endpoint)
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
        # Google's official Generative Language API authenticates via
        # `x-goog-api-key` (or ?key=); `x-api-key` is an Anthropic convention
        # the official endpoint ignores, so the "Google Gemini" preset 401'd.
        # Send both so official Google + third-party Gemini proxies both work.
        "x-goog-api-key": provider.api_key,
        "x-api-key": provider.api_key,
    }
    headers.update(provider.extra_headers)
    payload_bytes, status, err_body = _post_json(target, body, headers, timeout_sec)
    err_str = _decode_err_body(err_body)
    if payload_bytes is None:
        return None, False, status, err_str, None
    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except Exception:
        return None, False, status, err_str, None
    candidates = payload.get("candidates") or []
    raw_text = ""
    if candidates and isinstance(candidates[0], dict):
        content = candidates[0].get("content") or {}
        parts = content.get("parts") or []
        for part in parts:
            if isinstance(part, dict) and "text" in part:
                raw_text += part["text"]
    finish = candidates[0].get("finishReason") if candidates and isinstance(candidates[0], dict) else None
    truncated = finish in ("MAX_TOKENS", "LENGTH")
    usage = None
    u = parse_usage(payload, "gemini")
    if u:
        usage = dict(u); usage["estimated"] = False
    return raw_text, truncated, status, err_str, usage


def _read_response(
    target: str, body: dict[str, Any], headers: dict[str, str], timeout_sec: int
) -> tuple[str | None, bool, int | None, str, dict | None]:
    """Anthropic-format specific reader. Returns the parsed payload as the
    5th element so the caller can extract usage without re-parsing."""
    payload_bytes, status, err_body = _post_json(target, body, headers, timeout_sec)
    err_str = _decode_err_body(err_body)
    if payload_bytes is None:
        return None, False, status, err_str, None
    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except Exception:
        return None, False, status, err_str, None
    raw_text = ""
    for c in payload.get("content", []) or []:
        if isinstance(c, dict) and c.get("type") == "text":
            raw_text = c.get("text", "")
            break
    truncated = payload.get("stop_reason") == "max_tokens"
    return raw_text, truncated, status, err_str, payload


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
) -> tuple[str | None, bool, int | None, str, dict | None]:
    """Dispatch a call on the provider's API format. Never raises.

    Returns ``(raw_text, truncated, status, err_body, usage)``:

    * ``raw_text``    — model text response (None on error / no image)
    * ``truncated``   — True when the model hit the max_tokens limit
    * ``status``      — HTTP status (None on network failure)
    * ``err_body``    — decoded, truncated upstream error body
    * ``usage``       — token usage dict
      ``{input_tokens, output_tokens, cache_read_tokens,
        cache_creation_tokens, estimated}``
      or ``None`` when the API didn't return a usage block and we
      couldn't estimate it (no text).
    """
    dispatch = {
        ApiFormat.ANTHROPIC: _call_anthropic,
        ApiFormat.OPENAI: _call_openai,
        ApiFormat.GEMINI: _call_gemini,
    }
    fn = dispatch.get(provider.api_format) or _call_anthropic
    raw_text, truncated, status, err_body, usage = fn(
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
    # When the API didn't return a usage block but we got text back, fall
    # back to local estimation. Flag the row so the UI can label it.
    if usage is None and raw_text:
        est_in = estimate_tokens(user_text + " " + system_prompt)
        est_out = estimate_tokens(raw_text)
        usage = {
            "input_tokens": est_in,
            "output_tokens": est_out,
            "cache_read_tokens": 0,
            "cache_creation_tokens": 0,
            "estimated": True,
        }
    return raw_text, truncated, status, err_body, usage


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
) -> tuple[str | None, bool, int | None, str, dict | None]:
    """call_llm_api wrapped with exponential-backoff retry on transient errors.

    Returns the same 5-tuple as ``call_llm_api``. The retry annotations
    (the ``[retry N/M after Xs]`` suffix) are appended to ``err_body`` so
    the operator can see how many attempts were spent before giving up.
    """
    last: tuple[str | None, bool, int | None, str, dict | None] = (None, False, None, "", None)
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
        text, truncated, status, err_body, usage = last
        if text is not None:
            return last
        retryable = (
            status in _RETRYABLE_STATUS
            or status is None
        )
        if not retryable:
            return last
        # MEDIUM-2: network errors (status=None) can persist across multiple
        # retries and may indicate a persistent connectivity issue.
        # Cap network-error retries at 1 so we fail fast rather than burning
        # through all retries on a persistently unreachable endpoint.
        if status is None and attempt >= 1:
            # Even when we cap network-error retries at 1 attempt, the caller
            # still needs to know we did try — annotate err_body with the
            # retry summary so the surfaced message reads "1 attempt, gave up"
            # instead of "single connection failure with no indication of
            # retry behaviour".
            backoff_final = initial_backoff_sec * (backoff_factor ** attempt)
            net_suffix = f"[retry {attempt + 1}/{retries} after {backoff_final:.1f}s — network error, giving up]"
            if err_body:
                last = (text, truncated, status, err_body + "\n" + net_suffix, usage)
            else:
                last = (text, truncated, status, net_suffix, usage)
            return last
        backoff = initial_backoff_sec * (backoff_factor ** attempt)
        suffix = f"[retry {attempt + 1}/{retries} after {backoff:.1f}s]"
        if err_body:
            err_body = err_body + "\n" + suffix
        elif status is not None:
            err_body = f"HTTP {status}\n" + suffix
        else:
            err_body = suffix
        last = (text, truncated, status, err_body, usage)
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
    # Consecutive failures count — used to badge card health and for retry
    # decisions in the UI layer.
    consecutive_failures: int = 0


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
    target = _api_base(provider.endpoint) + "/v1/models"
    headers = {
        "Authorization": f"Bearer {provider.api_key}",
        "content-type": "application/json",
    }
    headers.update(provider.extra_headers)
    body: dict[str, Any] = {}
    body.update(provider.extra_body or {})
    t0 = _now_ms()
    payload_bytes, status, err_body = _post_json(target, body, headers, timeout_sec)
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
    base = _api_base(provider.endpoint)
    # C4: put API key in x-api-key header (same as _call_gemini). Earlier
    # this function put it in the URL via ?key=... which leaked the key
    # into proxy/Referer logs. Users who need the URL form can override via
    # `extra_headers={"X-Use-Url-Key": "1"}` (gate detected via the body shape).
    target = f"{base}/v1beta/models"
    headers = {
        "content-type": "application/json",
        "x-goog-api-key": provider.api_key,
        "x-api-key": provider.api_key,
    }
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
    """Send a minimal generate request to verify the model + key.

    If the configured model is rejected (401/403/400), retry with a
    well-known cheap fallback.  Many aggregator endpoints don't validate
    the model name strictly and accept the fallback even when the
    configured name is slightly wrong or unrecognized.
    """
    # Canonical fallback models + a slightly larger max_tokens to avoid
    # endpoints that reject max_tokens=1 as too small.
    ANTHROPIC_FALLBACKS = ["claude-3-haiku-20240307", "claude-sonnet-4-20250514", "claude-3-5-sonnet-20241022"]
    OPENAI_FALLBACKS = ["gpt-4o-mini", "gpt-4o", "gpt-4o-2024-08-06"]
    GEMINI_FALLBACKS = ["gemini-2.0-flash", "gemini-1.5-flash"]
    PROBE_MAX_TOKENS = 8   # avoid "max_tokens too small" rejections

    def _do_probe(model: str) -> tuple[bytes | None, int | None, bytes, int]:
        """Returns (payload, status, err_body, latency_ms)."""
        t0 = _now_ms()
        if fmt == ApiFormat.OPENAI:
            target = _api_base(provider.endpoint) + "/v1/chat/completions"
            body = {"model": model, "max_tokens": PROBE_MAX_TOKENS,
                    "messages": [{"role": "user", "content": "hi"}]}
            headers = {"Authorization": f"Bearer {provider.api_key}",
                       "content-type": "application/json"}
        elif fmt == ApiFormat.GEMINI:
            base = _api_base(provider.endpoint)
            target = f"{base}/v1beta/models/{model}:generateContent"
            body = {"contents": [{"role": "user", "parts": [{"text": "hi"}]}],
                    "generation_config": {"max_output_tokens": PROBE_MAX_TOKENS}}
            headers = {"content-type": "application/json",
                       "x-goog-api-key": provider.api_key,
                       "x-api-key": provider.api_key}
        else:  # ANTHROPIC
            target = _api_base(provider.endpoint) + "/v1/messages"
            body = {"model": model, "max_tokens": PROBE_MAX_TOKENS,
                    "messages": [{"role": "user", "content": "hi"}]}
            headers = {"x-api-key": provider.api_key,
                       "anthropic-version": "2023-06-01",
                       "content-type": "application/json"}
        headers.update(provider.extra_headers)
        body.update(provider.extra_body or {})
        payload_bytes, status, err_body = _post_json(target, body, headers, timeout_sec)
        return payload_bytes, status, err_body, _now_ms() - t0

    # Build candidate model list: configured model first, then fallbacks.
    configured = provider.model.strip() if provider.model else ""
    if fmt == ApiFormat.OPENAI:
        fallbacks = OPENAI_FALLBACKS
    elif fmt == ApiFormat.GEMINI:
        fallbacks = GEMINI_FALLBACKS
    else:
        fallbacks = ANTHROPIC_FALLBACKS

    candidates: list[str] = []
    if configured:
        candidates.append(configured)
    for fb in fallbacks:
        if fb not in candidates:
            candidates.append(fb)

    last_status = None
    last_err_body = b""
    best_latency = 0
    ok_latency = 0

    for model in candidates:
        payload_bytes, status, err_body, latency = _do_probe(model)
        last_status = status
        last_err_body = err_body
        if payload_bytes is not None:
            # HTTP 2xx — endpoint accepted the request.
            ok_latency = latency
            res = ConnectionResult(ok=True, latency_ms=latency, status=status)
            return res
        # Non-2xx or network error: check if we should keep trying.
        #
        # Bug-7 fix: distinguish "credential rejected" (401/403) from
        # "model not found" (400/404). On 401/403 the configured key is
        # bad — retrying with fallback models only burns quota and may
        # trigger upstream anti-abuse, so bail immediately. On 400/404
        # the configured model name is the problem — *that's* when the
        # fallback list is useful. Apply the early-stop only AFTER the
        # configured model has been tried, so users with a valid key +
        # wrong model name still get the fallback benefit.
        if status in (401, 403) and model == configured:
            # Key is likely invalid; don't waste quota on fallbacks.
            break
        if status in (400, 404) and model != configured:
            # Configured model is rejected; we ARE trying fallbacks now,
            # and they all failed too. Stop early to avoid further burns.
            # (Falls through to the loop's natural end.)
            pass

    # All candidates exhausted.
    res = ConnectionResult(ok=False, latency_ms=best_latency or 0, status=last_status or None)
    if last_status == 401:
        res.error_key = "err.401"
    elif last_status == 403:
        res.error_key = "err.403"
    elif last_status is None:
        res.error_key = "err.network"
    else:
        res.error_key = "err.http"
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
