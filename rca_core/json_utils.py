"""Lenient JSON extraction, ported from RLPE range_chart_extractor."""

from __future__ import annotations

import json
import re
from typing import Any


def extract_balanced_json_object(text: str) -> str | None:
    """Return the first balanced {...} JSON object substring, or None.

    Handles nested braces and braces inside string literals correctly.
    """
    start = text.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escape = False
        for i in range(start, len(text)):
            c = text[i]
            if in_string:
                if escape:
                    escape = False
                elif c == "\\":
                    escape = True
                elif c == '"':
                    in_string = False
                continue
            if c == '"':
                in_string = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return text[start : i + 1]
        start = text.find("{", start + 1)
    return None


def extract_balanced_json_array(text: str) -> str | None:
    """Mirror of `extract_balanced_json_object` for top-level arrays.

    Some models occasionally wrap the response in `[…]` instead of `{…}`
    — usually a malformed attempt at returning multiple records. We still
    want to surface something useful rather than raise ValueError and
    leave the caller with nothing. The extracted substring is fed back
    to `json.loads`; non-array payloads will fail there with a clear
    error rather than silently being treated as an object.
    """
    start = text.find("[")
    while start != -1:
        depth = 0
        in_string = False
        escape = False
        for i in range(start, len(text)):
            c = text[i]
            if in_string:
                if escape:
                    escape = False
                elif c == "\\":
                    escape = True
                elif c == '"':
                    in_string = False
                continue
            if c == '"':
                in_string = True
            elif c == "[":
                depth += 1
            elif c == "]":
                depth -= 1
                if depth == 0:
                    return text[start : i + 1]
        start = text.find("[", start + 1)
    return None


def strip_markdown_fence(text: str) -> str:
    """Strip markdown code fences (```json ... ```) from a model response.

    Models frequently wrap their JSON in fenced code blocks. This handles
    several variants:
      - ```json ... ```  (fence with language tag)
      - ``` ... ```       (bare fence)
      - leading ``` with no trailing fence (truncated response)
      - multiple fences (take content between first opening and last closing)
    Returns the cleaned string unchanged if no fence is present.
    """
    if not text:
        return text
    s = str(text).strip()
    # If the whole text is one fenced block, extract its inner content.
    fence_block = re.match(
        r"^```(?:json)?\s*\n?(.*?)\n?```\s*$", s, re.DOTALL | re.IGNORECASE
    )
    if fence_block:
        return fence_block.group(1).strip()
    # FIX (fenced+prose): also handle a fenced block surrounded by prose on
    # either side, e.g. "Here:\n```json\n{...}\n```\nThanks". The stricter
    # regex above requires the fence to span the whole string; this one
    # locates the first fenced block anywhere in the text.
    fence_block = re.search(
        r"```(?:json)?\s*\n?(.*?)\n?```", s, re.DOTALL | re.IGNORECASE
    )
    if fence_block:
        return fence_block.group(1).strip()
    # Otherwise strip any leading/trailing fence lines defensively.
    s = re.sub(r"^```(?:json)?\s*", "", s, flags=re.MULTILINE | re.IGNORECASE)
    s = re.sub(r"\s*```$", "", s, flags=re.MULTILINE)
    return s


def extract_json_like(text: str) -> str | None:
    """Find the first JSON object or array inside prose.

    Models sometimes embed JSON inside explanatory text, e.g.
      "Here is the result:\n{\"a\":1}\nLet me know if you need more."
    or wrap it in markdown. This locates the first `{` or `[` and extracts
    the balanced substring from there. Returns None if nothing balanced is
    found.
    """
    if not text:
        return None
    s = str(text).strip()
    # Prefer the first '{' (object); fall back to first '[' (array).
    for opener, extractor in (("{", extract_balanced_json_object),
                               ("[", extract_balanced_json_array)):
        start = s.find(opener)
        if start != -1:
            candidate = extractor(s[start:])
            if candidate is not None:
                return candidate
    return None


def safe_json_loads(text: str) -> dict[str, Any]:
    """Lenient JSON object parse with a 6-level fallback chain.

    The chain (each step only runs if the previous one failed):
      1. Strip markdown fences (```json ... ```).
      2. Strip raw control characters (0x00-0x1F except \\t\\r\\n).
      3. Strict ``json.loads`` — handles clean JSON and top-level arrays.
      4. Balanced-brace object extraction (first ``{...}``).
      5. Balanced-bracket array extraction (first ``[...]``) → wrapped.
      6. Prose-embedded JSON (``extract_json_like``) — last resort.

    Raises ValueError only if every level fails.
    """
    if not text:
        raise ValueError("empty text")
    s = str(text).strip()

    # Level 1: strip markdown fences.
    s = strip_markdown_fence(s)

    # Level 2: strip raw control characters that json.loads rejects.
    _ctrl_re = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
    s = _ctrl_re.sub("", s)

    # Level 3: strict parse.
    try:
        parsed = json.loads(s)
        if isinstance(parsed, dict):
            return parsed
        # H3: a top-level array IS valid JSON; json.loads accepted it.
        # Surface it as a synthetic wrapper so the caller's normalize_*
        # functions (all of which assume a dict) still get something
        # they can introspect instead of crashing with TypeError on
        # `parsed.get(...)`.
        if isinstance(parsed, list):
            return {"_array_root": parsed,
                    "_note": "model returned a top-level array; wrapping for diagnostics"}
    except Exception:
        pass

    # Level 4: balanced-brace object extraction.
    candidate = extract_balanced_json_object(s)
    if candidate is not None:
        try:
            return json.loads(candidate)
        except Exception:
            pass

    # Level 5: balanced-bracket array extraction → wrapped.
    candidate = extract_balanced_json_array(s)
    if candidate is not None:
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, list):
                return {"_array_root": parsed,
                        "_note": "model returned a top-level array; wrapping for diagnostics"}
        except Exception:
            pass

    # Level 6: prose-embedded JSON (last resort).
    candidate = extract_json_like(s)
    if candidate is not None:
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
            if isinstance(parsed, list):
                return {"_array_root": parsed,
                        "_note": "model returned a top-level array; wrapping for diagnostics"}
        except Exception:
            pass

    raise ValueError(f"no JSON object found in {s[:120]!r}")
