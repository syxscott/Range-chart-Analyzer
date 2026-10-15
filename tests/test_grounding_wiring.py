"""Is the name-grounding check actually WIRED, or just implemented?

The module shipped with 72 tests of its own and a `score_range_chart(data,
grounding=...)` parameter -- and nothing called it. A check that is implemented,
tested, documented and unreachable is worse than an absent one, because the
documentation reads like a guarantee. These tests exist to pin the CALL PATH,
which is the part the module's own tests cannot see.

Three properties, in order of how badly they fail:

  1. both production scoring paths go through the grounded helper
  2. ``ground_names`` is bound where the multi-run path can see it
  3. ``ground_names: false`` really skips the second call
  4. a broken/absent check degrades to a score, never to a failed request
"""
import ast
import inspect
import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

SERVER = REPO / "server.py"


@pytest.fixture(scope="module")
def server_ast():
    return ast.parse(SERVER.read_text(encoding="utf-8"))


def _handler(server_ast):
    for fn in ast.walk(server_ast):
        if isinstance(fn, ast.FunctionDef) and fn.name == "_handle_extract_body":
            return fn
    raise AssertionError("_handle_extract_body not found in server.py")


class TestTheCheckIsReachableAtAll:
    def test_the_helper_exists_and_is_exported(self):
        import server
        assert hasattr(server, "_safe_ground_and_score"), (
            "server.py lost _safe_ground_and_score; name grounding is now "
            "implemented but unreachable again")
        sig = inspect.signature(server._safe_ground_and_score)
        assert "enabled" in sig.parameters, (
            "no opt-out switch -- a second model call per extraction must be "
            "disableable by the caller")

    def test_both_production_paths_call_the_grounded_helper(self, server_ast):
        """Extraction sites are grounded; cache sites are marked, never bare.

        Two of each, and the distinction is the point:
          * single-run and merged runs>=2 scored a FRESH model result, so they
            can and must run the check;
          * the two cache back-fills score an entry written before the quality
            scorer existed. They must not run a second billable call (a cache
            hit is free by contract) but must not leave the verdict silently
            absent either.

        The failure this pins is not hypothetical: the first version of this
        wiring left both cache sites on the bare helper, so a re-run of an old
        figure showed a clean-looking badge with no grounding verdict at all.
        """
        fn = _handler(server_ast)
        names = lambda f: [n for n in ast.walk(fn)          # noqa: E731
                           if isinstance(n, ast.Call)
                           and isinstance(n.func, ast.Name)
                           and n.func.id == f]
        assert len(names("_safe_ground_and_score")) == 2, (
            "expected the grounded helper at both extraction scoring sites "
            "(single-run and merged runs>=2), found %d"
            % len(names("_safe_ground_and_score")))
        assert len(names("_safe_score_cached")) == 2, (
            "expected the explicitly-marked cached helper at both cache "
            "back-fill sites, found %d" % len(names("_safe_score_cached")))
        assert len(names("_safe_score_range_chart")) == 0, (
            "a scoring site still uses the bare ungrounded helper, so its "
            "result reports no grounding verdict: lines %s"
            % sorted(n.lineno for n in names("_safe_score_range_chart")))

    def test_a_cache_backfill_never_reports_clean(self):
        """Absence must be spelled out, not implied.

        `_safe_score_cached` is the only route that legitimately skips the
        check. If it returned a plain score, the UI would have no way to tell
        "not checked" from "checked, nothing missing" -- which is the
        false-assurance bug this whole feature was built to remove.
        """
        import server
        out = server._safe_score_cached({"species_ranges": [
            {"species": "Alveolinella camposi"}]})
        assert out["grounding"]["status"] == "not_performed", (
            "a cached result with no grounding verdict must say so explicitly")
        assert "clean" not in out["grounding"]
        assert out["grounding"]["reason"], "say WHY it was not checked"

    def test_ground_names_is_bound_outside_the_single_run_branch(self, server_ast):
        """The NameError trap.

        ``ground_names`` was first bound inside ``if runs == 1:`` and then read
        by the runs>=2 scoring site. That compiles fine, passes a lint, and
        raises NameError on the first multi-run request -- the exact request
        shape nobody tests by hand.
        """
        fn = _handler(server_ast)
        run_branches = []
        for node in ast.walk(fn):
            if (isinstance(node, ast.If)
                    and isinstance(node.test, ast.Compare)
                    and getattr(node.test.left, "id", None) == "runs"):
                run_branches.append(node)

        assert run_branches, "the `runs == 1` branch is gone; re-derive this test"
        for br in run_branches:
            in_branch = {t.id for stmt in br.body for n in ast.walk(stmt)
                         for t in ast.walk(n) if isinstance(t, ast.Name)}
            in_else = {t.id for stmt in br.orelse for n in ast.walk(stmt)
                       for t in ast.walk(n) if isinstance(t, ast.Name)}
            both = in_branch & in_else
            assert not both, (
                "bound only inside one branch of `if runs == ...` but used in "
                f"the other: {sorted(both)}")

        before_branches = {t.id for stmt in fn.body[:fn.body.index(run_branches[0])]
                           for n in ast.walk(stmt)
                           for t in ast.walk(n) if isinstance(t, ast.Name)}
        assert "ground_names" in before_branches, (
            "ground_names is not bound before the runs branch, so at least one "
            "scoring path reads an unbound name")


class TestOptOutAndDegradation:
    def _helper(self):
        import server
        return server._safe_ground_and_score

    def test_disabled_skips_the_second_call_entirely(self, monkeypatch):
        """`enabled=False` must not reach the transport.

        The reason this is asserted rather than assumed: the whole reason the
        flag exists is to avoid a second billable model call, and a flag that
        still called out would cost money while reporting that it had not.
        """
        import rca_core.grounding as G

        def boom(**_kw):
            raise AssertionError("the check ran while disabled")

        monkeypatch.setattr(G, "check_name_grounding", boom)
        out = self._helper()({"species_ranges": [{"species": "A"}]},
                             provider=object(), enabled=False)
        assert "grounding" not in out, "disabled must not report a grounding key"

    def test_enabled_runs_the_check_and_reports_its_status(self, monkeypatch):
        import rca_core.grounding as G
        from rca_core.grounding import GroundingResult

        seen = {}

        def fake(**kw):
            seen.update(kw)
            return GroundingResult(status="verified", checked=2,
                                   unverified=("Ghostia",))

        monkeypatch.setattr(G, "check_name_grounding", fake)
        out = self._helper()({"species_ranges": [{"species": "A"}]},
                             image_b64="QUJD", media_type="image/png",
                             provider=object(), enabled=True)
        assert out["grounding"]["status"] == "verified"
        assert out["grounding"]["unverified"] == ["Ghostia"]
        assert seen["image_b64"] == "QUJD", (
            "the check was not given the image it is supposed to verify "
            "against -- it would have reported everything absent")

    def test_a_raising_check_degrades_to_a_plain_score(self, monkeypatch):
        """Never a failed extraction, never a silent pass."""
        import rca_core.grounding as G

        def boom(**_kw):
            raise RuntimeError("transport died")

        monkeypatch.setattr(G, "check_name_grounding", boom)
        out = self._helper()({"species_ranges": [{"species": "A"}]},
                             provider=object(), enabled=True)
        assert "score" in out and "grade" in out
        assert "grounding" not in out, (
            "an exception was swallowed into a result that looks like a "
            "check passed; absence must not read as clean")

    def test_import_failure_degrades_instead_of_raising(self, monkeypatch):
        """A missing/broken grounding module must not take the endpoint down."""
        import builtins
        real_import = builtins.__import__

        def blocked(name, *a, **kw):
            if name == "rca_core.grounding":
                raise ImportError("simulated missing module")
            return real_import(name, *a, **kw)

        monkeypatch.setattr(builtins, "__import__", blocked)
        out = self._helper()({"species_ranges": [{"species": "A"}]},
                             provider=object(), enabled=True)
        assert "score" in out and "grade" in out


class TestScoreStaysNeutralThroughTheServer:
    def test_the_server_helper_cannot_move_score_or_grade(self, monkeypatch):
        """Same contract as quality's own tests, checked at the wiring seam.

        Cheap insurance against a future refactor moving the call from "append
        an issue" to "adjust a dimension" without touching quality.py.
        """
        import rca_core.grounding as G
        from rca_core.grounding import GroundingResult
        from rca_core.quality import score_range_chart

        data = {"species_ranges": [
            {"species": "Alveolinella camposi", "section": "S1",
             "range_base": "A", "range_top": "B"},
            {"species": "Pseudobolivina major", "section": "S1",
             "range_base": "A", "range_top": "B"},
        ]}
        baseline = score_range_chart(data)
        monkeypatch.setattr(
            G, "check_name_grounding",
            lambda **kw: GroundingResult(status="verified", checked=2,
                                         unverified=tuple(
                                             r["species"] for r in
                                             data["species_ranges"])))
        import server
        out = server._safe_ground_and_score(data, provider=object(),
                                            enabled=True)
        assert out["score"] == baseline["score"]
        assert out["grade"] == baseline["grade"]
        assert out["grounding"]["unverified"], "the check found nothing to report"
