#!/usr/bin/env python3
"""Repo hygiene checks that no test covers, for the scheduled health bot.

WHY THIS EXISTS
---------------
Every check here is something that has actually rotted in this repository and
that the 4,000-test suite structurally cannot catch, because the suite only
sees the files it is told to run:

  * ci.yml's own comment records the recurring failure -- "AUDIT-2026-09-27: a
    root *.py added later must be added here too ... This is the third
    recurrence of that exact rot (Python side REVIEW-2026-09-10, JS side this
    round, and root .py files here)." A test file that nothing invokes is
    green forever and protects nothing. This is the highest-value check here.
  * `rca_core.__version__` and the newest CHANGELOG section drifted apart at
    least once; only a human reading the diff noticed.
  * Docs kept pointing at files that had been renamed.

Design rules:
  * Standard library only. This runs on a bare runner with no install step,
    so it must never import third-party code, and it must not import
    rca_core either (that would drag in 228 modules for one string).
  * Every finding says what to do, not just what is wrong.
  * Exit 0 = clean, 1 = findings. The workflow turns findings into one
    deduplicated issue rather than one per run.
"""

from __future__ import annotations

import fnmatch
import os
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Root-level tracked files above this are reported. The reorganisation of
# 2026-11-01 took the root from 63 tracked files to ~21; the budget keeps it
# there instead of letting scratch scripts accumulate again.
ROOT_BUDGET = 28

# Harnesses that deliberately have no CI job, each with the reason. Listing
# them explicitly means a NEW unwired harness is still flagged, while these
# stop generating the same weekly noise.
UNWIRED_ALLOWED = {
    "tests_real_corpus.py": (
        "needs outputs/lit2020*/ — 299 MB of cropped figures from copyrighted "
        "papers, gitignored — plus a live API key"
    ),
}

findings: list[str] = []
notes: list[str] = []


def finding(check: str, detail: str, fix: str) -> None:
    findings.append(f"**[{check}]** {detail}\n  → {fix}")


# --------------------------------------------------------------------------
# 1. Every test harness must be reachable from a workflow.
#
# A harness nobody runs is worse than no harness: it costs maintenance and
# returns zero signal. This is the check that would have caught all three
# historical recurrences on the day they happened.
# --------------------------------------------------------------------------
def tracked_files() -> list[Path]:
    """Tracked files only, so gitignored scratch scripts never count.

    Falls back to a filesystem walk when git is unavailable (the bot runs in
    a checkout, but the script must also work from an exported tarball).
    """
    import subprocess

    try:
        out = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=REPO, capture_output=True, check=True,
        ).stdout.decode("utf-8", "replace")
        return [REPO / p for p in out.split("\0") if p]
    except (OSError, subprocess.CalledProcessError):
        return [p for p in REPO.rglob("*") if p.is_file() and ".git" not in p.parts]


def _split_shell_text(text: str) -> tuple[str, list[str]]:
    """Split workflow text into (runnable body, case-block exclusions).

    Two things in a workflow look like coverage but are not, and both made an
    earlier version of this check vacuous:

      * a `case ... continue ;;` arm is an EXCLUSION. `tests_real_corpus.py` is
        skipped by CI on purpose and was reported as wired because its own
        skip pattern counted as a mention.
      * a `#` comment is prose. ci.yml discusses several suites by name —
        including the ones it deliberately does not run — so comments must not
        satisfy "this harness is invoked".

    Returns the text with case blocks and comment lines removed, plus the
    exclusion patterns found inside the case blocks.
    """
    body, excludes, depth = [], [], 0
    for line in text.splitlines():
        stripped = line.strip()
        if depth == 0:
            if re.match(r'case\s+.*\bin$', stripped):
                depth = 1
                continue
            if stripped.startswith('#'):
                continue
            body.append(line)
            continue
        if stripped.startswith('esac'):
            depth = 0
            continue
        # `*tests_core.py|*tests_onboarding.py) continue ;;`
        m = re.match(r'([^\s()]+)\)', stripped)
        if m:
            excludes.extend(part for part in m.group(1).split('|') if part)
    return '\n'.join(body), excludes


def _glob_match(rel: str, pattern: str) -> bool:
    """Glob match that does NOT let `*` cross a path separator.

    fnmatch does cross it, and that turned `*.js` — which appears in this
    repo's workflows as prose about the tests — into "matches every JavaScript
    file anywhere", a vacuous pass for the whole check. Real pathname expansion
    is the point here: `tests/js/tests_*.js` must cover tests/js and must not
    cover js/.
    """
    r, p = rel.split('/'), pattern.split('/')
    if len(r) != len(p):
        return False
    return all(fnmatch.fnmatchcase(a, b) for a, b in zip(r, p))


def _standalone_collector_excludes() -> set[str]:
    """File names ``tests/standalone/conftest.py`` refuses to collect.

    The directory-level pytest step is only wiring for the files that hook
    actually collects. Parsed with AST instead of imported: importing a
    conftest from this script would drag pytest into a stdlib-only sweep.
    """
    import ast as _ast

    path = REPO / "tests" / "standalone" / "conftest.py"
    if not path.is_file():
        return set()
    try:
        tree = _ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        return set()
    for node in tree.body:
        if not isinstance(node, _ast.Assign):
            continue
        if not any(isinstance(t, _ast.Name) and t.id == "_EXCLUDED"
                   for t in node.targets):
            continue
        if isinstance(node.value, _ast.Dict):
            return {k.value for k in node.value.keys
                    if isinstance(k, _ast.Constant) and isinstance(k.value, str)}
    return set()


def check_harnesses_are_wired() -> None:
    workflow_dir = REPO / ".github" / "workflows"
    if not workflow_dir.is_dir():
        finding("wiring", "no .github/workflows directory", "restore CI")
        return
    ci_text = "\n".join(
        p.read_text(encoding="utf-8", errors="replace")
        for p in workflow_dir.glob("*.yml")
    )

    # Harnesses are the script-style suites that no runner discovers by
    # convention: `tests_*.py` (pytest's python_files=test_*.py does not
    # match them) and `tests_*.js` (plain node scripts). They live wherever
    # the layout puts them, so the scan is name-based across the whole tree.
    harnesses = sorted(
        p for p in tracked_files()
        if re.fullmatch(r"tests_.*\.(py|js)", p.name)
    )

    body, excludes = _split_shell_text(ci_text)
    globs = set(re.findall(
        r"[A-Za-z0-9_./-]*\*[A-Za-z0-9_./*-]*\.(?:py|js)", body))
    directory_excludes = _standalone_collector_excludes()

    def covered_by_directory_pytest(path: Path) -> bool:
        """A directory-level pytest run wires every file the hook collects.

        2026-11-07 (P1-6): `tests/standalone/conftest.py` collects the
        script-style suites via ``pytest_collect_file``, so
        ``python -m pytest tests/standalone/`` runs them without naming any of
        them — the old "filename must appear in a workflow" rule then reported
        ten wired suites as unwired. Only lines WITHOUT ``-m`` / ``-k`` /
        ``--deselect`` / ``--ignore`` count: the full-suite step runs
        ``-m "not standalone_script"``, i.e. it excludes exactly these items,
        and treating that as coverage would make this check vacuous again.
        """
        if path.name in directory_excludes:
            return False
        rel = path.relative_to(REPO).as_posix()
        parent = rel.rsplit("/", 1)[0] + "/"
        for line in body.splitlines():
            if "pytest" not in line:
                continue
            # Everything AFTER the word `pytest`: `python -m pytest` carries
            # its own `-m` (the module flag), which is not an exclusion.
            after = line.split("pytest", 1)[1]
            if re.search(r"(?:^|\s)-[mk](?:\s|$)", after):
                continue
            if "--deselect" in after or "--ignore" in after:
                continue
            # Positional arguments only. This must be an EXACT directory
            # argument: `pytest tests/standalone/tests_core.py` contains the
            # same prefix but collects one file, and treating it as directory
            # coverage made every standalone suite look wired (caught by the
            # mutation test that removes the dedicated step).
            positional = [t for t in after.split() if not t.startswith("-")]
            if any(t.rstrip("/") + "/" == parent for t in positional):
                return True
        return False

    def wired(path: Path) -> bool:
        rel = path.relative_to(REPO).as_posix()
        # 1. Named outright in a run line — `python tests/standalone/tests_core.py`
        #    counts even though a loop elsewhere excludes it.
        if path.name in body or rel in body:
            return True
        # 2. Fed to a loop but then skipped by a case arm. Shell `case`
        #    matches with `*` crossing `/`, unlike the `for f in` glob above,
        #    so this uses plain fnmatch on the whole relative path.
        if any(fnmatch.fnmatchcase(rel, x) for x in excludes):
            return False
        # 3. Covered by a pathname-expansion glob.
        if any(_glob_match(rel, g) for g in globs):
            return True
        # 4. Collected by a directory-level pytest hook (conftest.py).
        return covered_by_directory_pytest(path)

    unwired = [p for p in harnesses if not wired(p)]
    for p in unwired:
        reason = UNWIRED_ALLOWED.get(p.name)
        if reason:
            notes.append(f"{p.name}: not wired, allowed — {reason}")
            continue
        rel = p.relative_to(REPO).as_posix()
        finding(
            "wiring",
            f"`{rel}` is a harness that no workflow invokes — it can never fail.",
            f"add it to the appropriate job in .github/workflows/ci.yml",
        )
    notes.append(f"harnesses checked: {len(harnesses)}, wired: {len(harnesses) - len(unwired)}")


# --------------------------------------------------------------------------
# 2. Version / tag / CHANGELOG agreement.
# --------------------------------------------------------------------------
def check_version_agreement() -> None:
    init = (REPO / "rca_core" / "__init__.py").read_text(encoding="utf-8")
    m = re.search(r'__version__\s*=\s*"([^"]+)"', init)
    if not m:
        finding("version", "rca_core.__version__ not found", "restore the literal")
        return
    version = m.group(1)

    changelog = REPO / "CHANGELOG.md"
    if not changelog.is_file():
        finding("version", "CHANGELOG.md missing", "restore it")
        return
    text = changelog.read_text(encoding="utf-8")

    # The released version must have its own section; an unreleased one must
    # sit under [Unreleased]. Either way the number has to appear somewhere,
    # otherwise a bump shipped without notes.
    if f"## [{version}]" not in text and f"## {version}" not in text:
        if version not in text:
            finding(
                "version",
                f"`__version__ = \"{version}\"` has no CHANGELOG entry at all.",
                "add a `## [{v}]` section".format(v=version),
            )
        else:
            notes.append(
                f"version {version} appears in CHANGELOG but not as a section "
                "(fine while unreleased)"
            )

    # Newest section heading, as a second opinion on "what shipped last".
    headings = re.findall(r"^## \[(\d+\.\d+\.\d+)\]", text, re.M)
    newest = headings[0] if headings else None
    if newest and newest != version:
        finding(
            "version",
            f"newest CHANGELOG section is {newest} but `__version__` is {version}.",
            "bump one of them so the released notes match the code",
        )
    notes.append(f"version: {version}, newest CHANGELOG section: {newest}")


# --------------------------------------------------------------------------
# 3. Relative links in user-facing docs must resolve.
#
# Docs rot silently: a rename leaves the link intact and every reader gets a
# 404 from GitHub. Only relative links that look like paths are checked --
# bare words and anchors are ignored, and `docs/` is included because that is
# where the long reports live.
#
# KNOW THE LIMIT (measured 2026-11-01): this covers TWO links in the whole
# repository. That is not a bug in the regex -- the docs simply reference files
# in backticks, not as links. There are 1,099 backticked path-like tokens, 340
# of which do not resolve, and almost all of those are bare basenames
# (`table.js` for `js/table.js`) that are legitimate prose shorthand. Widening
# the check to cover them would drown the weekly issue in false positives.
#
# `docs/` is also, deliberately, NOT checked for stale PATHS: those files are
# dated review records. `js/table-edit.js` in a 2026-09 report was correct when
# it was written, and rewriting it would falsify the record. The live document
# is README.md, and its five explicit paths do resolve.
# --------------------------------------------------------------------------
LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")


def check_doc_links() -> None:
    docs = [REPO / "README.md"] + sorted((REPO / "docs").glob("*.md"))
    broken: list[tuple[str, str]] = []
    checked = 0

    for doc in docs:
        if not doc.is_file():
            continue
        text = doc.read_text(encoding="utf-8", errors="replace")
        for target in LINK_RE.findall(text):
            target = target.strip()
            if not target or target.startswith(("#", "http://", "https://", "mailto:")):
                continue
            # Strip an anchor and a title, e.g. `foo.md#bar "title"`.
            path_part = target.split("#", 1)[0].split(" ", 1)[0]
            if not path_part:
                continue
            # Ignore links that are not file references (no dot, no slash).
            if "." not in path_part and "/" not in path_part:
                continue
            if path_part.startswith("file://") or path_part.endswith(".exe"):
                continue
            checked += 1
            resolved = (doc.parent / path_part).resolve()
            if not resolved.exists():
                broken.append((doc.relative_to(REPO).as_posix(), target))

    for doc, target in broken:
        finding(
            "docs",
            f"`{doc}` links to `{target}`, which does not exist.",
            "fix the path or drop the link",
        )
    notes.append(f"relative doc links checked: {checked}, broken: {len(broken)}")


# --------------------------------------------------------------------------
# 4. Root directory budget.
# --------------------------------------------------------------------------
def check_root_budget() -> None:
    root_files = sorted(
        p.name for p in tracked_files()
        if p.parent == REPO and p.name != ".gitignore"
    )
    if len(root_files) > ROOT_BUDGET:
        finding(
            "layout",
            f"{len(root_files)} tracked files sit at the repo root "
            f"(budget {ROOT_BUDGET}): "
            + ", ".join(f"`{n}`" for n in root_files[:12])
            + (" …" if len(root_files) > 12 else ""),
            "move test harnesses to tests/{js,standalone}/ and tools to scripts/",
        )
    notes.append(f"root files: {len(root_files)} / budget {ROOT_BUDGET}")


def check_release_contains_the_shipped_fixes() -> None:
    """M1 (ACCEPTANCE-REVIEW): a published tag must actually carry the fixes.

    v0.1.4 shipped without ``range_base_label`` / ``not_drawn`` in
    rca_core/standards/pbdb.py, so its PBDB export published the SECTION's
    age span as every taxon's FAD/LAD and treated "model looked, found
    nothing" as a real occurrence. HEAD had both fixed for 22 commits while
    the release page still offered the broken build. A tag existing is not
    evidence that the tag contains the fixes -- read the tag's own bytes.

    Gracefully skips on a shallow clone (no tags fetched).
    """
    import subprocess

    def git(*args: str) -> str:
        r = subprocess.run(["git", *args], cwd=REPO, capture_output=True,
                           text=True, timeout=30)
        return r.stdout.strip() if r.returncode == 0 else ""

    tag = git("describe", "--tags", "--abbrev=0")
    if not tag:
        notes.append("release freshness: skipped (no tag reachable in this clone)")
        return
    head = git("rev-parse", "HEAD")
    tag_commit = git("rev-parse", tag + "^{commit}")
    if head == tag_commit:
        notes.append(f"release freshness: {tag} == HEAD")
        return
    pbdb = git("show", tag + ":rca_core/standards/pbdb.py")
    if not pbdb:
        notes.append("release freshness: could not read tagged pbdb.py")
        return
    missing = [s for s in ("range_base_label", "not_drawn") if s not in pbdb]
    if missing:
        finding(
            "release",
            f"{tag} is behind HEAD and its pbdb.py lacks {missing} -- the "
            "published build still has the known PBDB data-correctness defect",
            "cut a new release; verify with "
            "`python scripts/release_check.py --release-ready` after tagging",
        )
    else:
        notes.append(f"release freshness: {tag} behind HEAD but has both fixes")


def check_ci_covers_the_documented_dependency_set() -> None:
    """M2/M3: CI must install what the user installs."""
    ci = REPO / ".github" / "workflows" / "ci.yml"
    if not ci.is_file():
        finding("deps", "ci.yml missing", "restore the workflow")
        return
    text = ci.read_text(encoding="utf-8")
    if "pip install -r requirements.txt" not in text:
        finding(
            "deps",
            "no CI job installs requirements.txt, so the optional dependency "
            "set (sv_ttk / pywebview / matplotlib / numpy) is unverified and "
            "green Dependabot PRs prove only that the YAML is valid",
            "add a deps job that runs pip install -r requirements.txt and "
            "smoke-tests each optional package",
        )
    else:
        notes.append("deps: CI installs the documented requirements.txt")


def check_community_and_citation_files() -> None:
    """M6: the minimum collaboration surface, plus CITATION.cff agreement."""
    import re

    required = (
        "CONTRIBUTING.md", "SECURITY.md", "CITATION.cff",
        ".github/ISSUE_TEMPLATE/bug_report.yml",
        ".github/ISSUE_TEMPLATE/config.yml",
    )
    missing = [rel for rel in required if not (REPO / rel).is_file()]
    if missing:
        finding("community", "missing: " + ", ".join(missing),
                "add the files; see docs/ACCEPTANCE-REVIEW-2026-11-02.md M6")
    else:
        notes.append("community: CONTRIBUTING / SECURITY / ISSUE_TEMPLATE / CITATION present")

    init = (REPO / "rca_core" / "__init__.py").read_text(encoding="utf-8")
    m = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', init, re.M)
    cit_path = REPO / "CITATION.cff"
    cit = cit_path.read_text(encoding="utf-8") if cit_path.is_file() else ""
    if m:
        version = m.group(1)
        cit_versions = re.findall(r'^\s*version:\s*["\']?([^"\']+?)["\']?\s*$',
                                  cit, re.M)
        wrong = [v for v in cit_versions if v != version]
        if wrong:
            finding("citation",
                    f"CITATION.cff version(s) {wrong} != __version__ {version}; "
                    "a published citation would be self-contradictory",
                    "sync CITATION.cff and run scripts/release_check.py --check")
        else:
            notes.append(f"citation: CITATION.cff agrees with __version__ {version}")


def main() -> int:
    for check in (
        check_harnesses_are_wired,
        check_version_agreement,
        check_doc_links,
        check_root_budget,
        check_release_contains_the_shipped_fixes,
        check_ci_covers_the_documented_dependency_set,
        check_community_and_citation_files,
    ):
        try:
            check()
        except Exception as exc:  # noqa: BLE001 - a broken check must not mask the others
            finding(check.__name__, f"crashed: {exc!r}", "fix the checker")

    print("## Repository health sweep\n")
    if findings:
        print(f"**{len(findings)} finding(s).**\n")
        for item in findings:
            print(f"- {item}\n")
    else:
        print("All checks passed.\n")

    print("<details><summary>Checks run</summary>\n")
    for note in notes:
        print(f"- {note}")
    print("\n</details>")

    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
