"""`encryption_status()` is a cross-module string contract with no guard.

rca_core/secrets_store.py:400 says it plainly:

    REVIEW-2026-09-20: the ``"fingerprint"`` VALUE IS KEPT even though the
    source it names is now the random key file ... because ``gui.py`` /
    ``gui_fluent.py`` branch on that exact string to show their "at-rest
    protection is weak" notice -- renaming it would silently drop the warning.

Two GUI files, one tuple literal in each, and a producer on the other side of
the module boundary. Nothing tests it. Rename the returned value and BOTH
security notices disappear with a green build.

Three gaps, one contract:

  1. encryption_status() has no test at all -- neither what it can return, nor
     that the GUIs recognise the weak values.
  2. The no-keyring fallback never runs in CI. CI installs `keyring`, so
     _HAS_KEYRING is True there and `_get_or_create_fernet_key` -- the 0600
     key file, the 0700 directory, the "protection is only as strong as the
     file permissions" warning -- is dead code in every CI run. It is only
     ever executed on a machine without keyring installed.
  3. Its docstring lists ``"passphrase"`` among the return values. No code path
     returns it; the passphrase path is a parameter of _active_fernet_key, not
     a state of encryption_status.

The scope assertions here matter more than the individual checks: the first
test enumerates the value set by driving both feature flags rather than
hardcoding it, and the second reads the accepted weak values out of the GUI
sources rather than restating them, so a future edit on any side of the
boundary has to update something that fails.
"""

from __future__ import annotations

import os
import re

import pytest

from rca_core import secrets_store as S

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The values the GUIs treat as "protection is weak, tell the user". Read from
# the GUI sources so this test and the code cannot drift apart silently.
#
# The search is anchored to the `encryption_status` call rather than scanning
# the whole file: a loose `in (...)` search finds an unrelated tuple first --
# gui.py has an earlier `in ("chk_show_top", "chk_show_sel")` -- and a guard
# that reads the wrong tuple is worse than no guard, because it passes.
_WEAK_TUPLE_RE = re.compile(r"""in\s*\(\s*((?:"[a-z_]+"\s*,\s*)*"[a-z_]+")\s*\)""")


def _gui_accepted_weak_values():
    found = {}
    for name in ("gui.py", "gui_fluent.py"):
        src = open(os.path.join(REPO, name), encoding="utf-8").read()
        call = src.find("encryption_status()")
        assert call > 0, f"{name} no longer calls encryption_status()"
        m = _WEAK_TUPLE_RE.search(src, call)
        assert m, (
            f"{name} calls encryption_status() but the following lines have no "
            "`in (\"...\", \"...\")` tuple, so the weak-value notice this guard "
            "is protecting appears to be gone. If it was reworked on purpose, "
            "update this guard deliberately rather than deleting it."
        )
        found[name] = set(re.findall(r'"([a-z_]+)"', m.group(1)))
    return found


# --------------------------------------------------------------------------
# 1. the value set, enumerated rather than assumed
# --------------------------------------------------------------------------
def test_every_reachable_status_is_either_weak_and_shown_or_strong(monkeypatch):
    """Drive both feature flags and require each reachable value to be
    classified. Hardcoding the expected strings is what made the docstring go
    stale in the first place."""
    reachable = {}
    for has_fernet in (True, False):
        for has_keyring in (True, False):
            monkeypatch.setattr(S, "_HAS_FERNET", has_fernet)
            monkeypatch.setattr(S, "_HAS_KEYRING", has_keyring)
            reachable[(has_fernet, has_keyring)] = S.encryption_status()

    assert reachable, "no state produced a value"
    accepted = _gui_accepted_weak_values()

    weak_sets = {name: set(vals) for name, vals in accepted.items()}
    # every GUI must agree on what counts as weak
    assert len({frozenset(s) for s in weak_sets.values()}) == 1, (
        f"gui.py and gui_fluent.py disagree on the weak-value set: {accepted}"
    )
    weak = next(iter(weak_sets.values()))
    assert "fingerprint" in weak and "plaintext" in weak, (
        f"the weak set should be the two states that leave a key at rest in "
        f"this process's reach; got {sorted(weak)}"
    )

    for state, value in sorted(reachable.items()):
        # The only value that needs no notice is the keyring one.
        assert value in weak or value == "keyring", (
            f"encryption_status() returned {value!r} for "
            f"(fernet, keyring)={state}, which is neither a value the GUIs "
            f"warn about nor the strong 'keyring' case. A value nobody handles "
            f"means the security notice is silently skipped."
        )


def test_the_docstring_does_not_claim_a_value_the_code_cannot_return():
    """The contract lists "passphrase"; no code path returns it. That is how a
    reader ends up trusting four states when there are three."""
    doc = S.encryption_status.__doc__ or ""
    # Only the clause that PROMISES the return values. Later prose in the same
    # docstring legitimately names strings that are NOT return values -- the
    # "a passphrase is a parameter, not a state" note, and the proposed
    # "keyfile" rename -- and reading the whole docstring would flag the
    # explanation for being an explanation.
    start = doc.find("returns")
    assert start > 0, "the docstring no longer states what it returns"
    clause = doc[start:doc.find(".", start) + 1 if doc.find(".", start) > 0
                 else len(doc)]
    claimed = set(re.findall(r'``"([a-z_]+)"``', clause))
    reachable = set()
    for has_fernet in (True, False):
        for has_keyring in (True, False):
            orig_f, orig_k = S._HAS_FERNET, S._HAS_KEYRING
            try:
                S._HAS_FERNET, S._HAS_KEYRING = has_fernet, has_keyring
                reachable.add(S.encryption_status())
            finally:
                S._HAS_FERNET, S._HAS_KEYRING = orig_f, orig_k
    assert claimed, "the docstring stopped naming its return values"
    phantom = claimed - reachable
    assert not phantom, (
        f"encryption_status()'s docstring promises {sorted(phantom)} but no "
        f"state produces it; reachable values are {sorted(reachable)}"
    )


# --------------------------------------------------------------------------
# 2. the no-keyring fallback, which CI never executes
# --------------------------------------------------------------------------
def test_without_keyring_the_status_is_weak_and_the_key_file_is_used(
        tmp_path, monkeypatch):
    """This is the branch CI cannot reach: it installs `keyring`, so
    _HAS_KEYRING is True and the key file is never touched. Here it runs."""
    monkeypatch.setattr(S, "_HAS_FERNET", True)
    monkeypatch.setattr(S, "_HAS_KEYRING", False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(os.path, "expanduser",
                        lambda p: p.replace("~", str(tmp_path), 1))
    monkeypatch.setattr(S, "_warned_obfuscation", False)

    weak = _gui_accepted_weak_values()["gui.py"]
    assert S.encryption_status() in weak, (
        "with no keyring the status must be one the GUIs warn about, or the "
        "user is told their keys are protected when they are not"
    )

    with pytest.warns(RuntimeWarning, match="keyring unavailable"):
        key = S._active_fernet_key()
    assert isinstance(key, bytes) and key

    key_file = tmp_path / ".range_chart_analyzer" / "fernet_key.fek"
    assert key_file.exists(), (
        "the fallback is supposed to persist a random key so a later process "
        "can still decrypt what this one wrote"
    )
    # Same key on a second call, i.e. it is read back rather than regenerated.
    assert S._active_fernet_key() == key


def test_encrypt_decrypt_roundtrips_through_the_key_file(tmp_path, monkeypatch):
    """End to end on the fallback path: what the file-based branch actually
    exists for is that an envelope written now is readable next launch."""
    if not S._HAS_FERNET:
        pytest.skip("cryptography not installed")
    monkeypatch.setattr(S, "_HAS_FERNET", True)
    monkeypatch.setattr(S, "_HAS_KEYRING", False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(os.path, "expanduser",
                        lambda p: p.replace("~", str(tmp_path), 1))
    monkeypatch.setattr(S, "_warned_obfuscation", False)

    secret = "sk-fallback-0123456789-ABCDEF"
    envelope = S.encrypt(secret)
    assert envelope.startswith(S._FER_TAG)
    assert S.decrypt(envelope) == secret

    # A second store instance, as if the app were restarted, must still read it.
    assert S.decrypt_or_none(envelope) == secret


def test_a_keyring_that_raises_still_falls_back_to_the_file(tmp_path, monkeypatch):
    """`except Exception: pass` around keyring.get_password covers a broken
    backend (no D-Bus, no credential store), which is a realistic Linux
    outcome and a silent-downgrade path worth pinning."""
    if not S._HAS_FERNET:
        pytest.skip("cryptography not installed")
    monkeypatch.setattr(S, "_HAS_FERNET", True)
    monkeypatch.setattr(S, "_HAS_KEYRING", True)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(os.path, "expanduser",
                        lambda p: p.replace("~", str(tmp_path), 1))
    monkeypatch.setattr(S, "_warned_obfuscation", False)

    class _BrokenKeyring:
        @staticmethod
        def get_password(*a, **k):
            raise RuntimeError("no D-Bus session bus")

    monkeypatch.setattr(S, "keyring", _BrokenKeyring, raising=False)

    with pytest.warns(RuntimeWarning, match="keyring unavailable"):
        key = S._active_fernet_key()
    assert isinstance(key, bytes) and key
    assert (tmp_path / ".range_chart_analyzer" / "fernet_key.fek").exists()
