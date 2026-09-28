"""Static audit: does every js/ mirror accept what its Python original accepts?

WHY THIS EXISTS
---------------
All three real cross-engine bugs found in the 2026-09-27 review were the same
shape: a Python function gained a parameter and the JS mirror was not updated.

  * rcaDictRows(raw) vs _dict_rows(raw, kind, warnings) -- a bare-string row
    was silently DROPPED in the browser. `{"sections": ["Section 1"]}` gave a
    table on the desktop and an empty table in the browser.
  * rcaRowFromString lost the branches _row_from_string gained.
  * rcaPyDumps' own comment named ensure_ascii=True while the Python it
    mirrors passes ensure_ascii=False.

None of them is findable by reading either file alone: each mirror comment
still said "Mirror of ...", so the code LOOKED synchronised. What gave them
away was a differential run, and a differential run only covers the inputs
someone thought to try.

So this is the cheap static complement. For every `// Mirror of
rca_core/x.py:name` comment in js/, compare the Python signature against the
JavaScript one. A mirror that takes strictly FEWER parameters is a strong
signal that it was not updated; that is exactly the shape of the bug above.

It is a POINTER, not a verdict. Some divergence is deliberate (documented in
the comment), and a JS function may legitimately have extra options. Read the
call sites before changing anything.

Run:  python audit_mirror_signatures.py
"""
import ast
import io
import os
import re
import sys

REPO = os.path.dirname(os.path.abspath(__file__))

# "Mirror of rca_core/aggregate.py:_recombination_ballots", "Mirrors
# rca_core/extractor.py normalize_result", "Mirror of _dict_rows" (no module
# path at all -- the most common form), "mirror of rca_core/reason_codes.py".
# The module path is OPTIONAL: requiring it silently dropped the one pair that
# matters most here (rcaDictRows / _dict_rows), because that comment is just
# "Mirror of _dict_rows".
MIRROR_RE = re.compile(
    r"mirror(?:s|ed)?\s+(?:of\s+)?(?:the\s+)?"
    r"(?:rca_core[/\\][\w/\\]+\.py[:\s]+)?"
    r"([A-Za-z_]\w*)",
    re.IGNORECASE)
FUNC_RE_TMPL = r"function\s+%s\s*\(([^)]*)\)"

# Findings already investigated and dismissed, kept here so the next run does
# not send someone re-proving them. Key = (js file, js function name).
KNOWN_BENIGN = {
    ("js/quality.js", "rcaCoverageFor"):
        "coverage_for(data, *, expected_units=None) -- keyword-only with a "
        "None default and the single call site (quality.py:1194) never passes "
        "it, so the parameter is dead. The JS mirror is free to omit it.",
}


def is_known_benign(jsfile, jsname):
    for (f, n), why in KNOWN_BENIGN.items():
        if f == jsfile and n == jsname:
            return why
    return None

# JS prefixes every mirror with `rca`/`RCA`; Python's private helpers lead with
# `_`. Strip both so the two sides can be lined up by name.
PREFIXES = ("rca_", "RCA_")


def name_variants(name):
    """Every spelling under which one concept appears in this repo.

    The three conventions collide: Python's private helpers lead with `_`, the
    JS renames them to `rca` + CamelCase (`_dict_rows` -> `rcaDictRows`,
    `_row_from_string` -> `rcaRowFromString`), and the mirror COMMENTS quote
    the Python spelling while the FUNCTION below carries the JS one. Without
    the snake->camel step the name-consistency check rejects the very pair it
    is supposed to verify, which silently suppresses the true positive.
    """
    out = [name]
    bases = [name]

    def camel(base):
        parts = [p for p in base.split("_") if p]
        if len(parts) < 2:
            return None
        return parts[0] + "".join(p[:1].upper() + p[1:] for p in parts[1:])

    # strip the rca/RCA prefix and the leading underscore, recording each stage
    changed = True
    while changed:
        changed = False
        for p in PREFIXES:
            if name.startswith(p) and len(name) > len(p):
                name = name[len(p):]
                bases.append(name)
                bases.append(name[:1].lower() + name[1:])
                changed = True
        if name.startswith("_") and not name.startswith("__"):
            name = name.lstrip("_")
            bases.append(name)
            changed = True

    for b in list(bases):
        c = camel(b)
        if c:
            out.append(c)
            out.append("rca" + c[:1].upper() + c[1:])
            out.append(c[:1].lower() + c[1:])

    seen, uniq = set(), []
    for v in out:
        if v not in seen:
            seen.add(v)
            uniq.append(v)
    return uniq


def py_signatures():
    """module path -> {func name: [param names]} for every rca_core module."""
    out = {}
    root = os.path.join(REPO, "rca_core")
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for fn in files:
            if not fn.endswith(".py"):
                continue
            path = os.path.join(dirpath, fn)
            rel = "rca_core/" + os.path.relpath(path, root).replace(os.sep, "/")
            try:
                tree = ast.parse(io.open(path, encoding="utf-8").read())
            except SyntaxError as e:
                print("SYNTAX ERROR %s: %s" % (rel, e))
                continue
            sigs = {}
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    a = node.args
                    names = [x.arg for x in a.posonlyargs] + [x.arg for x in a.args]
                    names += [x.arg for x in a.kwonlyargs]
                    if a.vararg:
                        names.append("*" + a.vararg.arg)
                    if a.kwarg:
                        names.append("**" + a.kwarg.arg)
                    sigs[node.name] = names
            out[rel] = sigs
    return out


def js_functions():
    """file -> {func name: [param names]} for every function declaration."""
    out = {}
    jsdir = os.path.join(REPO, "js")
    for fn in sorted(os.listdir(jsdir)):
        if not fn.endswith(".js"):
            continue
        src = io.open(os.path.join(jsdir, fn), encoding="utf-8").read()
        sigs = {}
        for m in re.finditer(r"function\s+([A-Za-z_$][\w$]*)\s*\(([^)]*)\)", src):
            params = [p.strip().split("=")[0].strip()
                      for p in m.group(2).split(",") if p.strip()]
            sigs[m.group(1)] = params
        out["js/" + fn] = sigs
    return out


def main():
    pys = py_signatures()
    jss = js_functions()
    all_js = {}
    for sigs in jss.values():
        all_js.update(sigs)

    pairs = []          # (js file, line, declared py name, js func name)
    unresolved = []
    for jsfile in sorted(jss):
        src = io.open(os.path.join(REPO, jsfile), encoding="utf-8").read()
        lines = src.splitlines()
        for lineno, line in enumerate(lines, 1):
            m = MIRROR_RE.search(line)
            if not m:
                continue
            name = m.group(1)
            if name.lower() in ("of", "the", "a", "it", "this", "that", "and"):
                continue
            # A mirror comment DESCRIBES the function that follows it, so the
            # reliable way to find the JS side is to walk forward to the next
            # function declaration. Matching by name does not work here: the
            # comments name the PYTHON original (_dict_rows, _mode) while the
            # JS carries a decorated name (rcaDictRows, rcaAggMode).
            jsname = None
            for ahead in lines[lineno:lineno + 40]:
                fm = re.match(r"\s*function\s+([A-Za-z_$][\w$]*)\s*\(([^)]*)\)",
                              ahead)
                if fm:
                    jsname = fm.group(1)
                    break
                if re.match(r"\s*(function|const|class)\s", ahead):
                    break
            if jsname is None:
                unresolved.append((jsfile, lineno, name,
                                   "no function declaration after the comment"))
                continue
            pairs.append((jsfile, lineno, name, jsname))

    print("mirror attributions found: %d" % len(pairs))
    narrow = []
    dismissed = []
    for jsfile, lineno, name, jsname in pairs:
        jsp = all_js.get(jsname, [])
        pyp = None
        mod = None
        for m2, sigs in pys.items():
            for n in name_variants(name):
                if n in sigs:
                    pyp, mod = sigs[n], m2
                    break
            if pyp is not None:
                break
        if pyp is None:
            unresolved.append((jsfile, lineno, name,
                               "declared %r has no Python counterpart "
                               "(prompt string / key map / renamed)" % name))
            continue
        # The forward walk finds "the next function", which is wrong when a
        # comment block declares several mirrors before one function. Require
        # the two NAMES to correspond before comparing signatures, otherwise a
        # constant's comment gets paired with an unrelated function and the
        # audit invents a divergence that is not there.
        if not (set(name_variants(name)) & set(name_variants(jsname))):
            unresolved.append((jsfile, lineno, name,
                               "comment declares %r but the next function is %s "
                               "-- misattributed, not compared" % (name, jsname)))
            continue
        py_named = [p for p in pyp if not p.startswith("*")]
        py_var = [p for p in pyp if p.startswith("*") and not p.startswith("**")]
        js_named = [p for p in jsp if not p.startswith("...")]
        if len(js_named) < len(py_named) or (py_var and not js_named):
            benign = is_known_benign(jsfile, jsname)
            if benign:
                dismissed.append((jsfile, lineno, jsname, benign))
            else:
                narrow.append((jsfile, lineno, jsname, mod, pyp, jsp))

    print("=" * 78)
    print("MIRRORS THAT ACCEPT FEWER PARAMETERS THAN THEIR PYTHON ORIGINAL")
    print("=" * 78)
    if not narrow:
        print("  (none)")
    if dismissed:
        print()
        print("  dismissed as investigated-and-benign:")
        for jsfile, lineno, jsname, why in dismissed:
            print("    %s:%d  %s -- %s" % (jsfile, lineno, jsname, why))
    for jsfile, lineno, name, mod, pyp, jsp in narrow:
        print("  %s:%d" % (jsfile, lineno))
        print("      py  %s(%s)" % (name, ", ".join(pyp)))
        print("      js  %s(%s)" % (name, ", ".join(jsp)))
    print()
    print("=" * 78)
    print("ATTRIBUTIONS THAT DID NOT RESOLVE (renames, or the mirror moved)")
    print("=" * 78)
    seen = set()
    for jsfile, lineno, name, why in unresolved:
        k = (jsfile, name)
        if k in seen:
            continue
        seen.add(k)
        print("  %s:%d  %s  -- %s" % (jsfile, lineno, name, why))
    return 1 if narrow else 0


if __name__ == "__main__":
    raise SystemExit(main())
