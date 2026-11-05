"""The err.* contract has THREE producers/consumers and must stay in sync.

WHY THIS EXISTS
===============
``server.py`` emits error keys. ``js/minimax.js`` gates them through
RCA_KNOWN_ERROR_KEYS (an unknown key is dropped, so the user sees a blank
banner). ``js/i18n.js`` must have the key in zh / en / ja, otherwise the
banner shows the raw key. The repo's own history records a miss:
``err.methodNotAllowed`` was added to server.py without the other two, and
was repaired later. A comment in server.py asked the next person to
remember; this test removes the need to remember.

Measured at ARCH-REVIEW-2026-11-05: server 17 keys, whitelist 33 (the extra
16 are browser-local errors such as err.network / err.cancelled), i18n 38.
All three were consistent. That consistency is now enforced.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ERR_KEY = r"""["'](err\.[A-Za-z0-9.]+)["']"""


def _server_keys() -> set[str]:
    src = (ROOT / "server.py").read_text(encoding="utf-8")
    return set(re.findall(ERR_KEY, src))


def _whitelist_keys() -> set[str]:
    src = (ROOT / "js" / "minimax.js").read_text(encoding="utf-8")
    m = re.search(r"RCA_KNOWN_ERROR_KEYS\s*=\s*new Set\(\[(.*?)\]\)", src, re.S)
    assert m, "RCA_KNOWN_ERROR_KEYS is no longer a Set([...]) literal"
    return set(re.findall(ERR_KEY, m.group(1)))


def _locale_keys() -> dict[str, set[str]]:
    """Three locale dictionaries, written in three different forms.

    i18n.js declares ``RCA_I18N = { zh: {...} }`` and then appends the
    other two with ``RCA_I18N.en = {...}`` / ``RCA_I18N.ja = {...}``. An
    earlier version of this helper assumed all three were ``  xx: {`` keys
    and found only zh -- hence the explicit anchors below.
    """
    src = (ROOT / "js" / "i18n.js").read_text(encoding="utf-8")
    anchors = [
        ("zh", r"^  zh: \{"),
        ("en", r"^RCA_I18N\.en = \{"),
        ("ja", r"^RCA_I18N\.ja = \{"),
    ]
    starts: list[tuple[str, int]] = []
    for lang, pattern in anchors:
        m = re.search(pattern, src, re.M)
        assert m, f"locale anchor for {lang} not found in js/i18n.js"
        starts.append((lang, m.start()))
    starts.sort(key=lambda pair: pair[1])
    out: dict[str, set[str]] = {}
    for i, (lang, start) in enumerate(starts):
        end = starts[i + 1][1] if i + 1 < len(starts) else len(src)
        out[lang] = set(re.findall(r"['\"](err\.[A-Za-z0-9.]+)['\"]\s*:",
                                   src[start:end]))
    return out


def test_every_server_error_key_is_whitelisted() -> None:
    missing = sorted(_server_keys() - _whitelist_keys())
    assert not missing, (
        "server.py emits err.* keys that js/minimax.js will silently drop: "
        + ", ".join(missing)
    )
    assert len(_server_keys()) >= 10, "extraction of server keys went stale"


def test_every_server_error_key_is_translated_in_all_three_locales() -> None:
    server = _server_keys()
    locales = _locale_keys()
    for lang, keys in sorted(locales.items()):
        missing = sorted(server - keys)
        assert not missing, (
            f"js/i18n.js [{lang}] is missing translations for server error "
            "keys: " + ", ".join(missing)
        )


def test_browser_local_error_keys_are_translated_too() -> None:
    """The whitelist's browser-only keys still reach the user as a banner."""
    whitelist = _whitelist_keys()
    locales = _locale_keys()
    for lang, keys in sorted(locales.items()):
        missing = sorted(whitelist - keys)
        assert not missing, (
            f"js/i18n.js [{lang}] is missing browser-local error keys: "
            + ", ".join(missing)
        )
