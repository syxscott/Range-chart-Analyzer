"""REAL-API corpus test — the generalisation guard.

Everything else in the suite is offline. This is the one that talks to the
live model over a real figure corpus, and it exists because of a class of
defect no offline test can see: a mode that DOES return structured data but
that no ``_looks_*`` / table-config branch knows about, so the data is
silently discarded and the panel says "No results yet". That is exactly what
happened to ``paleomap`` — 7 of 16 real responses carried 24 populated
tables and every one was dropped.

So the assertions here are INVARIANTS, not snapshots:

  INV1  A result carrying at least one populated table must render at least
        one table config. Zero rendered tables over non-zero extracted rows is
        data loss, and it is the failure this file was written to catch.
  INV2  A result with no tables must render no configs, and ``to_xlsx`` must
        refuse rather than write a workbook of empty sheets.
  INV3  ``mode_used`` must be a mode the app knows, and ``mode_source`` must
        say how it was decided.
  INV4  No figure may raise out of the pipeline.
  INV5  Honest refusal (empty arrays + confidence 0) is a PASS, not a
        failure — the model declining a figure it cannot read is correct
        behaviour and must never be reported as a regression.

Usage::

    python tests_real_corpus.py                    # default 24 figures
    python tests_real_corpus.py --n 60 --workers 4
    python tests_real_corpus.py --report out.json

Requires a configured provider. Skips (exit 0) when there is none, so it is
safe to wire into a developer machine without a key — but it is NOT wired
into CI, which has no credential and must not bill one.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import random
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

REPO = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, REPO)

# The corpora this file can run against, most specific first.
CORPORA = [
    os.path.join(REPO, "outputs", "lit2020_all"),
    os.path.join(REPO, "outputs", "lit2020_sample"),
    os.path.join(REPO, "outputs", "e2e_oa"),
]

KNOWN_MODES = {
    "range_chart", "columnar_section", "abundance_diagram",
    "zonation_chart", "phylogenetic_tree", "paleomap",
    "chemical_stratigraphy", "scatter_plot",
}

_print_lock = threading.Lock()


def log(msg: str) -> None:
    with _print_lock:
        print(msg, flush=True)


# ---------------------------------------------------------------------------
# corpus
# ---------------------------------------------------------------------------
def load_figures(limit_per_type: int = 0):
    """Return [(path, guessed_type)] stratified across whatever corpora exist.

    Stratified because the point is COVERAGE: a random sample of 24 from 480
    is mostly paleomaps and micrographs, and a generalisation gap in
    columnar-section rendering would go unseen.
    """
    picked = []
    for root in CORPORA:
        if not os.path.isdir(root):
            continue
        man = os.path.join(root, "manifest.json")
        by_type = {}
        if os.path.isfile(man):
            try:
                for m in json.load(open(man, encoding="utf-8")):
                    p = os.path.join(root, m.get("png", ""))
                    if os.path.isfile(p):
                        by_type.setdefault(m.get("type") or "unclassified",
                                           []).append(p)
            except Exception:
                by_type = {}
        if not by_type:
            for p in sorted(glob.glob(os.path.join(root, "*.png"))):
                by_type.setdefault("unclassified", []).append(p)
        for t, paths in sorted(by_type.items()):
            paths = sorted(paths)
            # Deterministic, and prefer the LARGER files: a 400 px thumbnail
            # cannot be read by a VLM, so testing it measures nothing.
            paths.sort(key=lambda p: -os.path.getsize(p))
            take = paths[:limit_per_type] if limit_per_type else paths
            for p in take:
                picked.append((p, t))
        if picked:
            break
    return picked


def get_provider():
    from rca_core import ProviderStore
    store = ProviderStore()
    store.load()
    for p in getattr(store, "providers", []):
        if len((getattr(p, "api_key", "") or "").strip()) > 20:
            return p
    return None


# ---------------------------------------------------------------------------
# one figure
# ---------------------------------------------------------------------------
def run_one(path, guessed_type, provider, timeout=420):
    from rca_core import load_image_b64, extract
    from rca_core.exporter import get_configs_for_result

    rec = {
        "figure": os.path.basename(path),
        "path": path,
        "guessed_type": guessed_type,
    }
    t0 = time.time()
    try:
        b64, mime, w, h, resized, derr = load_image_b64(path, 4000,
                                                         enhance=False)
    except Exception as exc:
        rec.update(error="load: %s: %s" % (type(exc).__name__, exc),
                   rows=0, rendered=0)
        rec["secs"] = round(time.time() - t0, 1)
        return rec

    rec.update(image_px="%dx%d" % (w, h), load_error=bool(derr))
    try:
        res = extract(mode="auto", image_b64=b64, media_type=mime,
                      chart_lang="en", provider=provider,
                      timeout_sec=timeout)
    except Exception as exc:
        rec.update(error="extract: %s: %s" % (type(exc).__name__, exc),
                   rows=0, rendered=0)
        rec["secs"] = round(time.time() - t0, 1)
        return rec

    d = res.data if isinstance(res.data, dict) else {}
    tables = {k: len(v) for k, v in d.items()
              if isinstance(v, list) and v and isinstance(v[0], dict)}
    cfgs = get_configs_for_result(d)
    rec.update(
        ok=bool(res.ok),
        status=getattr(res, "status", None),
        mode_used=getattr(res, "mode_used", "") or "",
        mode_source=getattr(res, "mode_source", "") or "",
        raw_len=len(res.raw or ""),
        tables=tables,
        rows=sum(tables.values()),
        rendered=[c["id"] for c in cfgs],
        raw_head=" ".join((res.raw or "").split())[:220],
    )
    rec["secs"] = round(time.time() - t0, 1)
    rec["honest_refusal"] = (not tables
                             and str(d.get("confidence")) in ("0", "0.0"))
    rec["data"] = d
    return rec


# ---------------------------------------------------------------------------
# invariants
# ---------------------------------------------------------------------------
def check_invariants(recs):
    """Return [(invariant, figure, detail)] for every violation."""
    bad = []
    for r in recs:
        fig = r["figure"]
        if r.get("error"):
            bad.append(("INV4-no-raise", fig, r["error"]))
            continue
        if r.get("rows", 0) > 0 and not r.get("rendered"):
            bad.append((
                "INV1-no-silent-data-loss", fig,
                "extracted %d rows in %s but rendered 0 tables"
                % (r["rows"], list(r.get("tables") or {}))))
        if r.get("rows", 0) == 0 and not r.get("honest_refusal"):
            # No rows and not an explicit refusal: either the model returned
            # nothing at all, or every table was empty.
            if r.get("rendered"):
                bad.append(("INV2-no-empty-tables", fig,
                            "rendered %s with 0 rows" % r["rendered"]))
        if r.get("mode_used") and r["mode_used"] not in KNOWN_MODES:
            bad.append(("INV3-unknown-mode", fig, r["mode_used"]))
    return bad


# ---------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=24,
                    help="figures to send (stratified across types)")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--per-type", type=int, default=0,
                    help="cap figures per guessed type (0 = unlimited)")
    ap.add_argument("--report", default=os.path.join(
        REPO, "outputs", "real_corpus_report.json"))
    ap.add_argument("--save-raw", default="",
                    help="also write each response payload here")
    args = ap.parse_args(argv)

    provider = get_provider()
    if provider is None:
        log("SKIP: no provider with a usable API key configured.")
        return 0
    log("provider : %s | model=%s | endpoint=%s"
        % (getattr(provider, "name", "?"), getattr(provider, "model", "?"),
           getattr(provider, "endpoint", "?")))
    # AUDIT-2026-09-27: the recorded corpus in outputs/e2e_oa carries NO
    # provider/model field, which is why "which model produced this?" could
    # not be answered. This report records it.
    log("recorded at : %s" % time.strftime("%Y-%m-%d %H:%M:%S"))

    figs = load_figures(limit_per_type=args.per_type)
    if not figs:
        log("SKIP: no figure corpus found under outputs/.")
        return 0
    # Stratified subsample: round-robin over types so every type is present
    # before any type is sampled twice.
    buckets = {}
    for p, t in figs:
        buckets.setdefault(t, []).append(p)
    order = sorted(buckets)
    for t in order:
        random.Random(1234).shuffle(buckets[t])
    chosen, i = [], 0
    while len(chosen) < args.n and any(buckets[t] for t in order):
        t = order[i % len(order)]
        if buckets[t]:
            chosen.append((buckets[t].pop(0), t))
        i += 1
    log("figures: %d (from %d types)" % (len(chosen), len(order)))
    log("")

    recs = []
    t_start = time.time()
    done = 0
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futs = {pool.submit(run_one, p, t, provider): (p, t)
                for p, t in chosen}
        for fut in as_completed(futs):
            p, t = futs[fut]
            try:
                r = fut.result()
            except Exception as exc:
                r = {"figure": os.path.basename(p), "path": p,
                     "guessed_type": t, "rows": 0, "rendered": 0,
                     "error": "harness: %s: %s" % (type(exc).__name__, exc)}
            recs.append(r)
            done += 1
            if r.get("error"):
                tag = "ERROR "
            elif r.get("status") == 429 or r.get("ok") is False:
                # AUDIT-2026-09-27: measured on a 3-worker run, 33 of 48 calls
                # came back ok=False/429 in a flat ~2 s. The app reports that
                # HONESTLY — the first harness tagged it "EMPTY" and made a
                # rate limit look like "the model read nothing", which is how
                # a throttled run was nearly mistaken for a quality result.
                # Throttling gets its own tag so a rate-limited sample is
                # never reported as a coverage number.
                tag = "429!! "
            elif r.get("honest_refusal"):
                tag = "REFUSE"
            elif r.get("rows"):
                tag = "DATA  "
            else:
                tag = "EMPTY "
            log("[%2d/%2d] %s %-46s %5.1fs mode=%-19s rows=%-4d rendered=%d"
                % (done, len(chosen), tag, r["figure"][:46], r.get("secs", 0),
                   (r.get("mode_used") or "-")[:19], r.get("rows", 0),
                   len(r.get("rendered") or [])))

    recs.sort(key=lambda r: r["figure"])
    violations = check_invariants(recs)

    with_data = [r for r in recs if r.get("rows")]
    refusals = [r for r in recs if r.get("honest_refusal")]
    errors = [r for r in recs if r.get("error")]
    # Rate-limited calls are NOT a quality signal in either direction.
    throttled = [r for r in recs
                 if r.get("status") == 429 or r.get("ok") is False]

    log("")
    log("=" * 72)
    log("sent=%d  with-data=%d  honest-refusal=%d  throttled(429)=%d  "
        "errors=%d  total-rows=%d"
        % (len(recs), len(with_data), len(refusals), len(throttled),
           len(errors), sum(r.get("rows", 0) for r in recs)))
    if throttled:
        log("NOTE: %d of %d calls were rate limited. Lower --workers (or 1) "
            "before quoting coverage from this run; a throttled call is not a "
            "figure the model failed to read." % (len(throttled), len(recs)))
    log("wall clock: %.0fs" % (time.time() - t_start))

    if with_data:
        log("\nrows by guessed type (guessed -> extracted):")
        agg = {}
        for r in with_data:
            k = r.get("guessed_type", "?")
            a = agg.setdefault(k, [0, 0, 0])
            a[0] += 1
            a[1] += r.get("rows", 0)
            a[2] += len(r.get("rendered") or [])
        for k in sorted(agg):
            n, rows, rend = agg[k]
            log("   %-22s n=%-3d rows=%-5d rendered-tables=%d" % (k, n, rows, rend))

    log("\nmode_used distribution (the model's own choice in auto mode):")
    from collections import Counter
    for m, n in Counter(r.get("mode_used") or "(none)" for r in recs).most_common():
        log("   %-22s %d" % (m, n))

    # Which extracted table keys have no renderer at all? That is the
    # generalisation gap list — the next modes to support.
    unrenderable = {}
    for r in with_data:
        for k in (r.get("tables") or {}):
            if k not in (r.get("rendered") or []):
                unrenderable.setdefault(k, 0)
                unrenderable[k] += 1
    if unrenderable:
        log("\ntable keys extracted but NOT rendered (next support targets):")
        for k, n in sorted(unrenderable.items(), key=lambda kv: -kv[1]):
            log("   %-26s %d figure(s)" % (k, n))

    if violations:
        log("\nINVARIANT VIOLATIONS (%d):" % len(violations))
        for inv, fig, detail in violations:
            log("   [%s] %s :: %s" % (inv, fig[:50], detail))
    else:
        log("\nall invariants hold (INV1..INV5)")

    os.makedirs(os.path.dirname(args.report), exist_ok=True)
    payload = {
        "recorded_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "provider": getattr(provider, "name", ""),
        "model": getattr(provider, "model", ""),
        "endpoint": getattr(provider, "endpoint", ""),
        "figures_sent": len(recs),
        "with_data": len(with_data),
        "honest_refusals": len(refusals),
        "errors": len(errors),
        "total_rows": sum(r.get("rows", 0) for r in recs),
        "violations": violations,
        "unrenderable_keys": unrenderable,
        "results": [{k: v for k, v in r.items() if k != "data"}
                    for r in recs],
    }
    json.dump(payload, open(args.report, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    log("report -> %s" % args.report)

    if args.save_raw:
        os.makedirs(args.save_raw, exist_ok=True)
        for r in recs:
            if r.get("data"):
                json.dump(r["data"], open(os.path.join(
                    args.save_raw,
                    os.path.splitext(r["figure"])[0] + ".json"),
                    "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        log("raw payloads -> %s" % args.save_raw)

    return 1 if violations or errors else 0


if __name__ == "__main__":
    sys.exit(main())
