"""The provider store must be redirectable AND recoverable.

REVIEW-2026-10-20: a probe script emptied the user's live
``providers.json`` -- five providers, two of them paid MiniMax keys --
and the only way back was a backup somebody had taken by hand the day
before. Two things were missing, and both are now here:

1. A supported way to point the store somewhere else. Monkeypatching the
   module attribute ``_default_store_path`` does NOT work, because
   ``ProviderStore.path`` is declared with
   ``field(default_factory=_default_store_path)`` and the factory is
   bound AT CLASS-CREATION TIME. The probe therefore wrote to the real
   store while believing it had redirected it.

2. A history of the store's own, so an accidental wipe is recoverable
   from the store rather than from a lucky manual copy.

The first test below PINS THE TRAP ITSELF: it asserts that the
monkeypatch does not work, so that nobody reaches for it again, and that
the environment variable does. A test that only asserted the new
mechanism would leave the trap open and documented nowhere.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rca_core.llm import LlmProvider, ProviderStore      # noqa: E402


@pytest.fixture()
def tmp_store(tmp_path, monkeypatch):
    """A store in a temp directory, isolated from the user's real one."""
    path = tmp_path / "providers.json"
    monkeypatch.setenv("RCA_PROVIDER_STORE", str(path))
    # Start from an EMPTY store. A store whose file does not exist SEEDS a
    # default provider and saves it (ProviderStore._seed_defaults) -- that
    # is correct behaviour, but it is not what these tests are about, and
    # it would leave a provider in every count.
    store = ProviderStore().load()
    store.providers = []
    store.current_id = ""
    store.save()
    for name in os.listdir(str(tmp_path)):
        if name.startswith("providers.json.bak"):
            os.remove(os.path.join(str(tmp_path), name))
    return str(path)


# ---------------------------------------------------------------------------
# 1. The redirect
# ---------------------------------------------------------------------------

def test_the_environment_variable_redirects_the_store(tmp_store):
    """The supported way to isolate a probe or a test."""
    assert ProviderStore().path == os.path.abspath(tmp_store)


def test_the_environment_variable_wins_over_the_home_directory(tmp_store,
                                                              monkeypatch):
    """Even with a HOME that points somewhere else."""
    monkeypatch.setenv("HOME", "/nonexistent-home")
    monkeypatch.setenv("USERPROFILE", "/nonexistent-home")
    assert ProviderStore().path == os.path.abspath(tmp_store)


def test_monkeypatching_the_module_attribute_does_NOT_work(tmp_store,
                                                            monkeypatch):
    """THE TRAP, pinned.

    ``ProviderStore.path`` is declared as
    ``field(default_factory=_default_store_path)``, and the factory is
    bound when the class is created -- so replacing the module attribute
    afterwards leaves the class calling the ORIGINAL function. A probe
    that redirects this way believes it is writing to a temp file and is
    in fact writing to the user's real one. That is how five providers
    were lost on 2026-10-20.
    """
    import rca_core.llm as llm
    monkeypatch.setattr(llm, "_default_store_path",
                        lambda: os.path.join("SHOULD-NOT-BE-USED",
                                             "providers.json"))
    assert ProviderStore().path == os.path.abspath(tmp_store), (
        "the module attribute is now consulted -- if this test fails, the "
        "monkeypatch that silently wrote to the real store has become "
        "live again, and every probe that used it is dangerous again"
    )


def test_an_explicit_path_still_wins(tmp_store):
    """The constructor argument is still the strongest statement."""
    explicit = os.path.join(os.path.dirname(tmp_store), "explicit.json")
    assert ProviderStore(path=explicit).path == explicit


# ---------------------------------------------------------------------------
# 2. The history
# ---------------------------------------------------------------------------

def _backups(path: str) -> list[str]:
    """The store's backup chain, newest first.

    The shape is the one ``scripts/update_ics.py`` already uses for the
    canonical table: ``<name>.bak`` is the newest and ``<name>.bak.1``
    ... ``<name>.bak.4`` are the older generations.
    """
    d = os.path.dirname(path)
    base = os.path.basename(path)
    found = []
    for n in sorted(os.listdir(d)):
        if n == base + ".bak" or n.startswith(base + ".bak."):
            found.append(n)
    return sorted(found)


def _newest_backup(path: str) -> str:
    return os.path.join(os.path.dirname(path), os.path.basename(path) + ".bak")


def test_a_change_leaves_a_backup(tmp_store):
    """Every content change keeps the file it replaced.

    The rule is "the content changed", not "the file existed": a store
    that was emptied is worth keeping too, because that is the state a
    user would want back after an accidental wipe.
    """
    store = ProviderStore().load()
    before = len(_backups(tmp_store))
    store.add(LlmProvider(name="First", endpoint="https://a.example",
                          api_key="sk-one", model="m"))
    assert len(_backups(tmp_store)) == before + 1

    store.add(LlmProvider(name="Second", endpoint="https://b.example",
                          api_key="sk-two", model="m"))
    assert len(_backups(tmp_store)) == before + 2, \
        "the second change did not keep the file it replaced"


def test_an_unchanged_save_leaves_no_backup(tmp_store):
    """The key is re-encrypted on every write, so the BYTES always differ.
    What must not differ is the CONTENT."""
    store = ProviderStore().load()
    store.add(LlmProvider(name="Only", endpoint="https://a.example",
                          api_key="sk-one", model="m"))
    before = _backups(tmp_store)
    store.save()
    store.save()
    assert _backups(tmp_store) == before, (
        "a save that changed nothing still left a backup -- the "
        "fingerprint is comparing the ciphertext, not the content")


def test_a_rename_leaves_a_backup(tmp_store):
    """A rename is a change, and it is the one a user would miss."""
    store = ProviderStore().load()
    prov = LlmProvider(name="Before", endpoint="https://a.example",
                       api_key="sk-one", model="m")
    store.add(prov)
    before = len(_backups(tmp_store))
    prov.name = "After"
    store.update(prov)
    assert len(_backups(tmp_store)) == before + 1


def test_the_backup_holds_the_previous_content_and_its_keys(tmp_store):
    """The backup has to be USABLE, not just present."""
    store = ProviderStore().load()
    prov = LlmProvider(name="Kept", endpoint="https://a.example",
                       api_key="sk-secret", model="m")
    store.add(prov)

    prov.name = "Renamed"
    store.update(prov)

    baks = _backups(tmp_store)
    assert baks
    with open(_newest_backup(tmp_store), encoding="utf-8") as f:
        old = json.load(f)
    names = [p["name"] for p in old["providers"]]
    assert names == ["Kept"], "the backup does not hold the old name: %r" % names

    # And the key in it still decrypts -- a backup that cannot be opened
    # is no backup at all.
    restored = ProviderStore(path=_newest_backup(tmp_store)).load()
    assert restored.providers[0].api_key == "sk-secret"


def test_switching_the_active_provider_does_not_spend_a_generation(tmp_store):
    """``current_id`` is a POINTER, not configuration.

    The store rewrites the file every time the user switches the active
    provider. If a switch counted as a change, five switches would push
    the user's real providers out of the backup chain entirely -- which
    is the chain they would need precisely after an accident.
    """
    store = ProviderStore().load()
    first = LlmProvider(name="First", endpoint="https://a.example",
                        api_key="sk-one", model="m")
    second = LlmProvider(name="Second", endpoint="https://b.example",
                         api_key="sk-two", model="m")
    store.add(first)
    store.add(second)
    before = len(_backups(tmp_store))

    for _ in range(ProviderStore.BACKUP_COUNT + 2):
        store.set_current(first.id)
        store.set_current(second.id)

    assert len(_backups(tmp_store)) == before, (
        "switching the active provider spent %d backup generations"
        % (len(_backups(tmp_store)) - before))


def test_the_history_is_pruned(tmp_store):
    """The directory must not fill up with copies of key material."""
    store = ProviderStore().load()
    keep = ProviderStore.BACKUP_COUNT + 3
    for i in range(keep):
        prov = LlmProvider(name="P%d" % i, endpoint="https://a.example",
                           api_key="sk-%d" % i, model="m")
        store.add(prov)
    baks = _backups(tmp_store)
    assert len(baks) == ProviderStore.BACKUP_COUNT, (
        "the history was not pruned: %d backups" % len(baks))


def test_a_failed_save_does_not_lose_the_live_file(tmp_store):
    """The backup is taken BEFORE the replace, so a failure in the middle
    leaves both the old file and its backup intact."""
    store = ProviderStore().load()
    store.add(LlmProvider(name="Kept", endpoint="https://a.example",
                          api_key="sk-one", model="m"))

    prov = LlmProvider(name="Second", endpoint="https://b.example",
                       api_key="sk-two", model="m")
    store.providers.append(prov)

    import rca_core.llm as llm
    real_json = llm.json

    class _Boom:
        """A json stand-in whose dump() fails and whose everything else is
        the real thing."""

        def dump(self, *args, **kwargs):
            raise OSError("disk full")

        def __getattr__(self, name):
            return getattr(real_json, name)

    llm.json = _Boom()
    try:
        with pytest.raises(OSError):
            store.save()
    finally:
        llm.json = real_json

    live = ProviderStore().load()
    assert len(live.providers) == 1, "the live file was damaged"
    assert live.providers[0].name == "Kept"
    assert _backups(tmp_store), "the backup was not taken before the write"
