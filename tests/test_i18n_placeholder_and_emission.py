"""The i18n contract, as three properties measured rather than assumed.

AUDIT-2026-10-01. The catalogs in rca_core/i18n.py and js/i18n.js had never been
compared as anything but a set of key NAMES outside the handful of namespaces
tests_frontend.js::test_i18n_parity calls "shared". Three properties were
measured, and the first two are the ones worth guarding:

1. PLACEHOLDERS. ``t()`` substitutes only the placeholders a string actually
   contains, so a key whose zh text has ``{sum}`` and whose en text does not
   renders the value in Chinese and silently loses it in English. The review
   recorded one instance of that shape
   (docs/FRONTEND-REVIEW-2026-08-19.json, "Quality issues carry params that
   several translations never interpolate"). MEASURED: zero drift -- all 415
   keys carry an identical ``{placeholder}`` set in all three languages, in both
   files. The fix landed and stayed. Pinned below.

2. EMISSION. A key that exists in both files is not the question; the question
   is whether a key some code EMITS can be RENDERED where it is shown. The two
   catalogs are deliberately split by surface and the split is large -- 415 keys
   on the Python side, 302 on the JS side, an overlap of 204, so 211 are
   backend-only and 98 are browser-only. Measured over emit SITES (a ``t()``
   call, a ``msg_key=`` / ``msgKey:`` field) rather than over every dotted string
   literal, because a literal that is not an emit site renders nothing:

     * 10 keys emitted by gui_fluent_onboarding.py / gui_fluent_providers.py
       are absent from the JS catalog -- correct, they are desktop-only and are
       rendered by rca_core/i18n.py, which has all of them;
     * 24 keys emitted by js/app.js / js/table.js (upload panel, results
       toolbar, copy-to-clipboard, settings show/hide) are absent from the
       Python catalog -- correct, the desktop has its own widgets for those;
     * keys emitted by Python and missing from BOTH catalogs: 0;
     * keys emitted by JS and missing from BOTH catalogs: 0.

   So the asymmetry is by design, and the property that would actually catch a
   real misrender is "every key this side emits exists in THIS side's catalog".
   That is what the two emission tests below check. Copying 309 keys across to
   make the catalogs symmetric would be a maintenance burden with no user
   benefit, and is explicitly not done here.

3. The JS catalog is read by evaluating js/i18n.js in a vm and handing RCA_I18N
   out from a trailing script, because RCA_I18N is a `const` and a const never
   attaches to a vm context object. Reading ctx.RCA_I18N yields undefined and
   makes every row look empty.
"""
import ast
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from rca_core.i18n import TRANSLATIONS  # noqa: E402

LANGS = ("zh", "en", "ja")
PLACEHOLDER = re.compile(r"\{(\w+)\}")
KEY_RE = re.compile(r"\b([a-z][a-zA-Z0-9_]*(?:\.[a-z][a-zA-Z0-9_]*)+)\b")
JS_CALL = re.compile(r"(?<![\w.])t\(\s*'([a-z][\w.]*)'\s*(?:,|\))")
JS_FIELD = re.compile(r"msg_?[Kk]ey\s*:\s*'([a-z][\w.]*)'")
SKIP_TOP = {"__pycache__", "node_modules", "outputs", "proxy", "references",
            ".git", ".pytest_cache", ".ruff_cache", "app", "assets", "css",
            "docs", ".claude", ".idea", ".superpowers", ".workbuddy"}


def _js_catalog():
    script = (
        "const vm=require('vm'),fs=require('fs');"
        f"const ctx=vm.createContext({{console}});ctx.window=ctx;ctx.globalThis=ctx;"
        f"vm.runInContext(fs.readFileSync({str(REPO / 'js' / 'i18n.js')!r},'utf8'),ctx);"
        # A const never attaches to a vm context object; hand it out explicitly.
        "vm.runInContext('globalThis.__i= RCA_I18N;',ctx);"
        "const I=ctx.__i;"
        "if(!I||!I.zh||!I.en||!I.ja){console.error('ENV');process.exit(3);}"
        "if(typeof I.zh['quality.abundance_sum_violation']!=='string'){"
        "console.error('ENV');process.exit(3);}"
        "const o={};for(const l of ['zh','en','ja'])"
        "for(const k of Object.keys(I[l]))o[l+'|'+k]=1;"
        "console.log(JSON.stringify(o));"
    )
    r = subprocess.run(["node", "-e", script], capture_output=True, text=True,
                       encoding="utf-8")
    assert r.returncode == 0, f"js/i18n.js did not evaluate: {r.stderr[:400]}"
    flat = __import__("json").loads(r.stdout)
    return {lang: {k.split("|", 1)[1] for k in flat if k.startswith(lang + "|")}
            for lang in LANGS}


JS_CAT = _js_catalog()
PY_CAT = {lang: set(TRANSLATIONS.get(lang, {})) for lang in LANGS}


class TestPlaceholdersAreIdenticalAcrossLanguages:
    """The property that silently drops information when it fails."""

    @pytest.mark.parametrize("lang", LANGS)
    def test_python_side(self, lang):
        drifted = []
        for key, text in TRANSLATIONS[lang].items():
            mine = frozenset(PLACEHOLDER.findall(text))
            others = {frozenset(PLACEHOLDER.findall(TRANSLATIONS[o][key]))
                      for o in LANGS if o != lang and key in TRANSLATIONS[o]}
            if others and mine not in others:
                drifted.append((key, sorted(mine)))
        assert not drifted, (
            "a placeholder exists in one language and not the others, so t() drops "
            f"it there: {drifted}")

    def test_js_side(self):
        # Same check against the evaluated JS table, which is the copy the browser
        # actually renders from.
        tables = _js_tables()
        drifted = []
        for key in set().union(*[set(t) for t in tables.values()]):
            sets = {frozenset(PLACEHOLDER.findall(tables[l][key])) for l in LANGS
                    if key in tables[l]}
            if len(sets) > 1:
                drifted.append((key, [sorted(s) for s in sets]))
        assert not drifted, drifted

    def test_every_key_is_present_in_all_three_languages(self):
        for side, cats in (("python", PY_CAT), ("js", JS_CAT)):
            for lang in LANGS:
                union = set().union(*[set(cats[o]) for o in LANGS])
                assert not (union - cats[lang]), (
                    f"{side}/{lang} is missing {sorted(union - cats[lang])[:10]}")


class TestEveryEmittedKeyCanBeRendered:
    """Emission, not existence: a key nobody emits cannot misrender."""

    def test_python_emit_sites_all_exist_in_the_python_catalog(self):
        emitted = _py_emit_sites()
        assert emitted, "the scanner found nothing -- it would pass vacuously"
        known = set().union(*PY_CAT.values())
        missing = {k: sorted(v) for k, v in emitted.items() if k not in known}
        assert not missing, (
            "rca_core emits msg_keys that its own catalog does not define, so the "
            f"GUI renders the raw dotted key: {missing}")

    def test_js_emit_sites_all_exist_in_the_js_catalog(self):
        emitted = _js_emit_sites()
        assert emitted, "the scanner found nothing -- it would pass vacuously"
        known = set().union(*JS_CAT.values())
        missing = {k: sorted(v) for k, v in emitted.items() if k not in known}
        assert not missing, (
            "js/ emits msg_keys that its own catalog does not define, so the "
            f"browser renders the raw dotted key: {missing}")

    def test_the_scanner_reaches_both_sides(self):
        # A scanner that only ever finds one side would make the two tests above
        # half-blind, so the floor is asserted explicitly.
        assert len(_py_emit_sites()) >= 20
        assert len(_js_emit_sites()) >= 40


def _py_emit_sites():
    found = {}

    def add(key, where):
        if KEY_RE.fullmatch(key):
            found.setdefault(key, set()).add(where)

    for path in sorted(REPO.rglob("*.py")):
        rel = path.relative_to(REPO)
        if rel.parts[0] in SKIP_TOP:
            continue
        if set(rel.parts) & {"tests"} or path.name.startswith("test") \
                or path.name == "tests_core.py" \
                or path.name == "gen_frontend_parity_fixtures.py":
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                for kw in node.keywords:
                    if kw.arg in ("msg_key", "msgKey") \
                            and isinstance(kw.value, ast.Constant) \
                            and isinstance(kw.value.value, str):
                        add(kw.value.value, rel.as_posix())
                fn = node.func
                name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
                if name == "t" and node.args and isinstance(node.args[0], ast.Constant) \
                        and isinstance(node.args[0].value, str):
                    add(node.args[0].value, rel.as_posix())
            if isinstance(node, ast.Dict):
                for k, v in zip(node.keys, node.values):
                    if isinstance(k, ast.Constant) and isinstance(k.value, str) \
                            and k.value in ("msg_key", "msgKey") \
                            and isinstance(v, ast.Constant) and isinstance(v.value, str):
                        add(v.value, rel.as_posix())
    return found


def _js_emit_sites():
    found = {}
    for path in sorted((REPO / "js").glob("*.js")):
        rel = path.relative_to(REPO).as_posix()
        text = re.sub(r"/\*.*?\*/", "", path.read_text(encoding="utf-8"), flags=re.S)
        text = re.sub(r"(?m)^\s*//.*$", "", text)
        for rx in (JS_CALL, JS_FIELD):
            for m in rx.finditer(text):
                found.setdefault(m.group(1), set()).add(rel)
    return found


def _js_tables():
    script = (
        "const vm=require('vm'),fs=require('fs');"
        f"const ctx=vm.createContext({{console}});ctx.window=ctx;ctx.globalThis=ctx;"
        f"vm.runInContext(fs.readFileSync({str(REPO / 'js' / 'i18n.js')!r},'utf8'),ctx);"
        "vm.runInContext('globalThis.__i= RCA_I18N;',ctx);"
        "const I=ctx.__i;const o={zh:I.zh,en:I.en,ja:I.ja};"
        "console.log(JSON.stringify(o));"
    )
    r = subprocess.run(["node", "-e", script], capture_output=True, text=True,
                       encoding="utf-8")
    assert r.returncode == 0, r.stderr[:400]
    return __import__("json").loads(r.stdout)
