#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""UX-2026-10-18 — Qt thread-model regression probe (NOT a pytest file).

Why this exists
---------------
`_Heartbeat` in gui_fluent.py was implemented as a QTimer created inside
`ExtractWorker.run()` — i.e. on a QThread. MEASURED on this machine with
PyQt6 6.7.1 (same Qt6 C++ core as PySide6; QTimer lives in QtCore, not in
the binding layer):

    ticks received while the worker thread slept 1.2s at a 200ms interval: 0

A QTimer is driven by the event loop of its OWNING thread. ExtractWorker
runs on a QThread that never calls exec() — only the GUI thread does
(`app.exec()`, gui_fluent.py:4492). So the timer was structurally unable
to fire: the heartbeat existed, was correctly constructed and correctly
stopped, and did absolutely nothing. Its unit tests passed because they
exercised the class contract with a stub timer, which cannot answer
"does this fire in the real thread model?".

That is a whole class of bug: **any QTimer/QSocketNotifier created off the
GUI thread and driven from a blocking worker is a silent no-op.** The stub
suite is structurally blind to it.

What this probe checks
----------------------
Both candidate designs, under a REAL Qt event loop:

  A. QTimer on the worker thread      -> expected: 0 ticks (the bug)
  B. QTimer on the GUI thread         -> expected: ticks (the fix)

Usage (needs a Qt binding; this machine has PyQt6, the app uses PySide6):

    QT_QPA_PLATFORM=offscreen python qt_thread_probe.py
    QT_QPA_PLATFORM=offscreen python qt_thread_probe.py --binding pyside6

Exit code 0 = B fired and A did not, i.e. the design distinction this
probe exists to protect still holds. Non-zero = the distinction collapsed
and any heartbeat code in the tree needs re-checking.
"""

from __future__ import annotations

import argparse
import sys
import time


def _load_qt(binding: str):
    if binding == "pyside6":
        from PySide6.QtCore import QThread, QTimer  # type: ignore
        from PySide6.QtWidgets import QApplication  # type: ignore
    else:
        from PyQt6.QtCore import QThread, QTimer  # type: ignore
        from PyQt6.QtWidgets import QApplication  # type: ignore
    return QThread, QTimer, QApplication


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--binding", default="pyqt6", choices=("pyqt6", "pyside6"))
    ap.add_argument("--interval-ms", type=int, default=200)
    ap.add_argument("--block-sec", type=float, default=1.2)
    args = ap.parse_args()

    try:
        QThread, QTimer, QApplication = _load_qt(args.binding)
    except Exception as exc:
        print(f"SKIP: no {args.binding} available ({exc}).")
        print("      Install it, or run this on a machine that has it.")
        # A missing binding is not a pass and not a fail: it means the
        # question was not answered. Say so out loud rather than exiting 0
        # and letting CI believe the distinction is intact.
        return 2

    app = QApplication.instance() or QApplication([])

    # --- A. worker-thread QTimer (the shape that shipped) ----------------
    worker_ticks: list[float] = []

    class WorkerTimerProbe(QThread):
        def run(self) -> None:
            t = QTimer()
            t.setInterval(args.interval_ms)
            t.timeout.connect(lambda: worker_ticks.append(time.perf_counter()))
            t.start()
            time.sleep(args.block_sec)          # stands in for extract()
            t.stop()

    w = WorkerTimerProbe()
    w.start()
    w.wait()

    # --- B. GUI-thread QTimer (the shape that actually works) -------------
    gui_ticks: list[float] = []
    timer = QTimer()
    timer.setInterval(args.interval_ms)
    timer.timeout.connect(lambda: gui_ticks.append(time.perf_counter()))
    timer.start()
    deadline = time.perf_counter() + args.block_sec
    while time.perf_counter() < deadline:
        app.processEvents()
        time.sleep(0.01)
    timer.stop()

    expected = max(1, int(args.block_sec * 1000 / args.interval_ms) - 1)
    print(f"binding            : {args.binding}")
    print(f"interval / block   : {args.interval_ms}ms / {args.block_sec}s")
    print(f"expected GUI ticks : >= {expected}")
    print()
    print(f"A. QTimer on worker QThread : {len(worker_ticks)} ticks")
    print(f"B. QTimer on GUI thread     : {len(gui_ticks)} ticks")
    print()

    ok = True
    if worker_ticks:
        print("NOTE: A fired. Either this Qt build drives timers without an")
        print("      event loop, or something changed. Re-check the reasoning")
        print("      behind any worker-thread QTimer in the tree.")
    else:
        print("CONFIRMED: a worker-thread QTimer does NOT fire without an event")
        print("          loop. Any progress heartbeat built that way is a no-op.")
    if len(gui_ticks) >= expected:
        print(f"PASS: the GUI-thread timer fired {len(gui_ticks)} times.")
    else:
        print(f"FAIL: GUI-thread timer fired {len(gui_ticks)} times, expected")
        print(f"      >= {expected}. The event loop is not draining — a heartbeat")
        print("      on this thread would also be a no-op.")
        ok = False

    del app
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
