"""What a model can actually do: the context window and the output ceiling.

REVIEW-2026-10-20. The provider presets carried 109 entries and NONE of
them said how big the model's window is or how much it can answer in one
response. The user typed a ``max_tokens`` by guessing, and a guess that
is too large is a 400 -- which is how the ``max_tokens=1000000`` request
that started this round became "does not support max tokens > 524288".

The catalogue is keyed by model, with a family fallback (a model id that
STARTS WITH a known family inherits the family's limits), because the
presets name 109 things that are really about thirty models -- sixty-nine
of them are the same ``claude-sonnet-4-20250514``.

Two rules the numbers follow:

* **Every number carries its source.** A number nobody can trace is a
  number the user cannot check, and a wrong one is worse than no one:
  it tells the user their 200000-token request is fine when it is not.
* **Unknown stays unknown.** A model that is not in the catalogue has no
  entry, and the UI says so. Silence is not a wrong number.

The vendors do not publish this: MiniMax's ``/v1/models`` answers with
``id``, ``display_name`` and ``created_at`` and nothing else, so the
numbers have to come from the documentation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class ModelCapabilities:
    """What one model can do.

    ``context_window`` and ``max_output`` are token counts. Zero means
    "not known", which is a different statement from "zero" and is shown
    as such.
    """

    #: The largest context the model accepts, in tokens.
    context_window: int = 0
    #: The largest single answer the model will produce, in tokens.
    max_output: int = 0
    #: An i18n key for a note about this model, if it has one.
    note: str = ""
    #: Where the numbers came from, so they can be checked and refreshed.
    source: str = ""


#: The catalogue, keyed by model id or by family prefix.
#:
#: The keys are matched exactly first, and then by the LONGEST matching
#: prefix -- so ``MiniMax-M3.1-Flash`` beats ``MiniMax-M3``.
MODEL_CAPABILITIES: dict[str, ModelCapabilities] = {
    # ---- MiniMax (measured against the live endpoint) -------------------
    "MiniMax-M3.1-Flash": ModelCapabilities(
        context_window=1_000_000, max_output=524_288,
        note="model.noteThinking",
        source="measured 2026-10-20: the endpoint answers a larger "
               "max_tokens with 'does not support max tokens > 524288 (2013)'",
    ),
    "MiniMax-M3.1-Flash-Preview": ModelCapabilities(
        context_window=1_000_000, max_output=524_288,
        note="model.noteThinking",
        source="measured 2026-10-20 (same endpoint as M3.1-Flash)",
    ),
    "MiniMax-M3": ModelCapabilities(
        context_window=512_000, max_output=128_000,
        note="model.noteThinkingSwitch",
        source="MiniMax platform documentation",
    ),
    "MiniMax-M2.7": ModelCapabilities(
        context_window=200_000, max_output=128_000,
        source="MiniMax platform documentation",
    ),
    "MiniMax-M2.5": ModelCapabilities(
        context_window=200_000, max_output=128_000,
        source="MiniMax platform documentation",
    ),
    # ---- Anthropic ------------------------------------------------------
    "claude-sonnet-4": ModelCapabilities(
        context_window=200_000, max_output=64_000,
        source="Anthropic's model comparison table (Sonnet 4 generation)",
    ),
    "claude-opus-4": ModelCapabilities(
        context_window=200_000, max_output=32_000,
        source="Anthropic's model comparison table (Opus 4 generation)",
    ),
    # ---- OpenAI ---------------------------------------------------------
    "gpt-4o": ModelCapabilities(
        context_window=128_000, max_output=16_384,
        source="OpenAI's model documentation",
    ),
    # ---- Google ---------------------------------------------------------
    "gemini-2.5": ModelCapabilities(
        context_window=1_048_576, max_output=65_536,
        source="Google's Generative Language API documentation",
    ),
    # ---- DeepSeek (official docs) --------------------------------------
    "deepseek-chat": ModelCapabilities(
        context_window=131_072, max_output=8_192,
        source="api-docs.deepseek.com pricing page (DeepSeek-V3.2: 128K "
               "context, output max 8K)",
    ),
    "deepseek-reasoner": ModelCapabilities(
        context_window=131_072, max_output=65_536,
        source="api-docs.deepseek.com pricing page (output max 64K, thinking "
               "and answer share the budget)",
    ),
    # ---- The Chinese vendors -------------------------------------------
    "kimi-k2.7-code": ModelCapabilities(
        context_window=262_144, max_output=16_384,
        source="Aliyun Bailian model page for kimi/kimi-k2.7-code "
               "(context 262144, max output 16384)",
    ),
    "kimi-k2-turbo-preview": ModelCapabilities(
        context_window=262_144, max_output=16_384,
        source="Moonshot's Kimi K2 series (the Turbo preview shares the "
               "K2.7 Code limits)",
    ),
    "LongCat-2.0": ModelCapabilities(
        context_window=1_000_000, max_output=131_072,
        source="LongCat API documentation (1M context, 128K output)",
    ),
    "doubao-seed-1.6": ModelCapabilities(
        context_window=262_144, max_output=16_384,
        source="Doubao 1.6 model card (256K context, 224K max input, 16K "
               "max output)",
    ),
    # The same model, spelled the way the API's model id spells it.
    "doubao-seed-1-6": ModelCapabilities(
        context_window=262_144, max_output=16_384,
        source="the same Doubao 1.6 model card",
    ),
    "doubao-pro-32k": ModelCapabilities(
        context_window=32_768, max_output=4_096,
        source="the 32k in the model id is the context; Volcengine's "
               "Doubao-Pro default output ceiling",
    ),
    "glm-4-plus": ModelCapabilities(
        context_window=131_072, max_output=4_096,
        source="Zhipu's model table (GLM-4-Plus: 128K context, 4K output)",
    ),
    "qwen-max": ModelCapabilities(
        context_window=32_768, max_output=8_192,
        source="Alibaba's model table (Qwen-Max: 32K context, 8K output)",
    ),
    "moonshot-v1-auto": ModelCapabilities(
        context_window=131_072, max_output=4_096,
        source="Moonshot's model table (Moonshot-v1 128K, 4K output)",
    ),
    "mimo-v2": ModelCapabilities(
        context_window=262_144, max_output=8_192,
        source="Xiaomi MiMo's model table (MiMo-v2-Pro: 262144 context, "
               "8192 output)",
    ),
    "mistral-large-latest": ModelCapabilities(
        context_window=131_072, max_output=32_768,
        source="Mistral's model documentation",
    ),
    "Llama-3.3-70B": ModelCapabilities(
        context_window=131_072, max_output=32_768,
        source="Meta's Llama 3.3 model card",
    ),
    "llama-3.1-70b-instruct": ModelCapabilities(
        context_window=131_072, max_output=32_768,
        source="Meta's Llama 3.1 model card",
    ),
    # The 8k / 32k in these model ids IS the context, so they are
    # self-documenting.
    "step-1-8k": ModelCapabilities(
        context_window=8_192, max_output=4_096,
        source="the 8k in the model id is the context; StepFun's default "
               "output ceiling",
    ),
    "step-2-8k": ModelCapabilities(
        context_window=8_192, max_output=4_096,
        source="the 8k in the model id is the context; StepFun's default "
               "output ceiling",
    ),
    "ernie-4.0-8k": ModelCapabilities(
        context_window=8_192, max_output=4_096,
        source="the 8k in the model id is the context; Baidu's default "
               "output ceiling",
    ),
}


def capabilities_for(model: object) -> Optional[ModelCapabilities]:
    """The capabilities of ``model``, or None when it is not catalogued.

    An exact model id wins; otherwise the LONGEST matching family
    prefix does, so ``MiniMax-M3.1-Flash`` takes MiniMax's entry rather
    than the looser ``MiniMax-M3`` one.
    """
    if not isinstance(model, str):
        return None
    name = model.strip().lower()
    if not name:
        return None
    meta = MODEL_CAPABILITIES.get(model.strip())
    if meta is not None:
        return meta
    best_key = ""
    for key in MODEL_CAPABILITIES:
        k = key.lower()
        if name.startswith(k) and len(k) > len(best_key):
            best_key = key
    if best_key:
        return MODEL_CAPABILITIES[best_key]
    # A catalogue key may also be a SUFFIX of the id, as with the
    # "Qwen/Qwen3-8B" and "meta-llama/..." spellings the aggregators use.
    best_key = ""
    for key in MODEL_CAPABILITIES:
        k = key.lower()
        if k in name and len(k) > len(best_key):
            best_key = key
    if best_key:
        return MODEL_CAPABILITIES[best_key]
    return None


def known_model_count() -> int:
    """How many models the catalogue knows about."""
    return len(MODEL_CAPABILITIES)
