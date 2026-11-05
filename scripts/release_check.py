#!/usr/bin/env python3
"""Release-readiness assertions, stdlib only.

WHY THIS EXISTS
===============
Three files carry the version number and nothing used to compare them:

  rca_core/__init__.py   __version__ = "0.1.4"
  CITATION.cff           version: 0.1.4
                         preferred-citation.version: 0.1.4
  CHANGELOG.md           ## [Unreleased] / ## [0.1.4] -- 2026-10-21

``scripts/repo_health.py`` checks only __init__.py vs the newest CHANGELOG
section, and runs WEEKLY (repo-health.yml cron). CITATION.cff -- the file a
researcher actually cites -- has no guard at all. A version bump that forgot
it would publish a self-contradictory citation file and CI would stay green
for up to seven days.

  --check           lightweight, runs on EVERY commit (ci.yml deps job).
                    Version agreement + section structure + date format.
  --release-ready   pre-publish gate. Adds: the tag exists, points at HEAD,
                    and actually contains the data-correctness fixes.

The second mode exists because of the worst finding in
GAP-ANALYSIS-2026-11-02: v0.1.4 shipped without range_base_label / not_drawn,
so the PBDB export published the SECTION's age span as every taxon's FAD/LAD
and treated "model looked but found nothing" as a real occurrence. HEAD had
both fixed for 22 commits. A tag is not evidence that the release contains
the fixes -- this check reads the tag's own bytes.

No third-party YAML library: CITATION.cff is a flat enough document here that
a tiny line parser is safer than adding a CI dependency.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _fail(msg: str) -> "NoReturn":  # type: ignore[valid-type]
    print(f"RELEASE CHECK FAILED: {msg}", file=sys.stderr)
    raise SystemExit(1)


def _version_from_init() -> str:
    text = (ROOT / "rca_core" / "__init__.py").read_text(encoding="utf-8")
    m = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', text, re.M)
    if not m:
        _fail("__version__ not found in rca_core/__init__.py")
    return m.group(1)


def _citation_fields() -> dict[str, str]:
    """Parse only the three scalar fields we need, ignoring the prose."""
    text = (ROOT / "CITATION.cff").read_text(encoding="utf-8")
    out: dict[str, str] = {}
    for line in text.splitlines():
        m = re.match(r'^(version|date-released):\s*["\']?([^"\']+?)["\']?\s*$', line)
        if m:
            out[m.group(1)] = m.group(2).strip()
        # preferred-citation.version is indented under that block.
        m2 = re.match(r'^\s{2}version:\s*["\']?([^"\']+?)["\']?\s*$', line)
        if m2:
            out["preferred-citation.version"] = m2.group(1).strip()
    return out


def _changelog_sections() -> list[str]:
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    return re.findall(r"^## \[([^\]]+)\]", text, re.M)


def check_basic() -> str:
    version = _version_from_init()
    cit = _citation_fields()

    for field in ("version", "preferred-citation.version"):
        got = cit.get(field)
        if not got:
            _fail(f"CITATION.cff is missing {field}")
        if got != version:
            _fail(f"CITATION.cff {field}={got!r} != __version__={version!r}")

    date = cit.get("date-released")
    if not date:
        _fail("CITATION.cff is missing date-released")
    try:
        _dt.date.fromisoformat(date)
    except ValueError:
        _fail(f"CITATION.cff date-released={date!r} is not an ISO date")

    sections = _changelog_sections()
    if not sections:
        _fail("CHANGELOG.md has no ## [section] headings")
    # The first section is either [Unreleased] (normal) or the release itself.
    released = [s for s in sections if s != "Unreleased"]
    if not released:
        _fail("CHANGELOG.md has no released version section")
    if sections[0] == "Unreleased":
        # Newest released section must still equal the code version.
        if released[0] != version:
            _fail(f"newest CHANGELOG release [{released[0]}] != __version__ {version!r}")
    elif sections[0] != version:
        _fail(f"newest CHANGELOG section [{sections[0]}] != __version__ {version!r}")

    print(f"release check: OK (version {version}, date {date}, "
          f"sections {sections[:2]})")
    return version


def check_release_ready(version: str | None = None) -> None:
    version = version or _version_from_init()
    tag = f"v{version}"

    def git(*args: str) -> str:
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True,
                              text=True, check=True).stdout.strip()

    tags = git("tag", "--list", tag)
    if not tags:
        _fail(f"tag {tag} does not exist")

    head = git("rev-parse", "HEAD")
    tag_commit = git("rev-parse", f"{tag}^{{commit}}")
    # A tag is a SNAPSHOT, and commits land after a release (docs, fixes,
    # housekeeping). Requiring tag == HEAD would make this gate red forever
    # the moment anyone committed again. What actually matters:
    #   (a) the tag is on HEAD's history, not a divergent branch, and
    #   (b) the tag's own bytes contain the fixes (checked below).
    ancestor = subprocess.run(
        ["git", "merge-base", "--is-ancestor", tag_commit, head],
        cwd=ROOT, capture_output=True, text=True)
    if ancestor.returncode != 0:
        _fail(f"{tag} ({tag_commit[:8]}) is not an ancestor of HEAD "
              f"({head[:8]}) — the tag belongs to a different history")
    behind = git("rev-list", "--count", f"{tag_commit}..{head}")
    if behind and behind != "0":
        print(f"note: {behind} commit(s) landed after {tag} "
              "(normal post-release work)")

    # The released bytes must contain the two data-correctness fixes.
    pbdb = subprocess.run(
        ["git", "show", f"{tag}:rca_core/standards/pbdb.py"],
        cwd=ROOT, capture_output=True, text=True, check=True).stdout
    for symbol in ("range_base_label", "not_drawn"):
        if symbol not in pbdb:
            _fail(f"{tag}:rca_core/standards/pbdb.py lacks {symbol!r} -- "
                  "this tag would publish the known PBDB data error")

    # date-released must match the annotated tag's date, not a hand-typed day.
    cit_date = _citation_fields()["date-released"]
    tag_date = git("for-each-ref", f"refs/tags/{tag}",
                   "--format=%(creatordate:short)")
    if tag_date != cit_date:
        _fail(f"CITATION.cff date-released={cit_date} != {tag} date {tag_date}")

    print(f"release ready: {tag} at {head[:8]} contains both PBDB fixes")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true",
                    help="lightweight per-commit checks")
    ap.add_argument("--release-ready", action="store_true",
                    help="full pre-publish gate (tag + released bytes)")
    args = ap.parse_args()
    if not (args.check or args.release_ready):
        ap.error("pass --check or --release-ready")
    version = check_basic()
    if args.release_ready:
        check_release_ready(version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
