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


def safe_json_loads(text: str) -> dict[str, Any]:
    """Lenient JSON object parse. Strips fences, tries strict, then falls
    back to the first balanced {...} object. Raises ValueError on failure.
    """
    if not text:
        raise ValueError("empty text")
    s = str(text).strip()
    s = re.sub(r"^```(?:json)?\s*", "", s, flags=re.MULTILINE)
    s = re.sub(r"\s*```$", "", s, flags=re.MULTILINE)
    # Models occasionally emit raw control characters (0x00-0x1F except \t
    # \r \n) inside string values which makes json.loads raise. Strip them
    # defensively before the strict parse.
    _ctrl_re = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
    s = _ctrl_re.sub("", s)
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
    candidate = extract_balanced_json_object(s)
    if candidate is not None:
        return json.loads(candidate)
    # H3: some models occasionally emit a top-level array instead of an
    # object. Try to recover that too rather than giving up. The caller's
    # normalize_* functions all expect a dict, so we wrap the array in a
    # synthetic key so the downstream pipeline still has something to
    # inspect (and can surface a friendlier error than "missing sections").
    candidate = extract_balanced_json_array(s)
    if candidate is not None:
        parsed = json.loads(candidate)
        if isinstance(parsed, list):
            return {"_array_root": parsed, "_note": "model returned a top-level array; wrapping for diagnostics"}
    raise ValueError(f"no JSON object found in {s[:120]!r}")
