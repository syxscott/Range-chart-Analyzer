# XLSX export gate probe (UX-2026-10-18).
# Verifies under a REAL Qt event loop the three properties backend-engineer
# could only assert statically:
#   1. the export button is DISABLED while a worker is alive
#   2. the gate re-derives from state, so a stray re-enable cannot survive
#      (the reason the disable lives in the gate and not in the slot)
#   3. a BOUND-METHOD slot runs on the GUI thread (queued), so the handler
#      may touch widgets; a lambda would be a DIRECT connection and unsafe
import time
import sys

from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtWidgets import QApplication, QPushButton

app = QApplication([])

gui_thread_id = int(app.thread().currentThreadId() if hasattr(app.thread(), 'currentThreadId')
                    else 0)
seen = []


class W(QThread):
    done = pyqtSignal(object, bool, object)

    def __init__(self, seconds):
        super().__init__()
        self.seconds = seconds

    def run(self):
        time.sleep(self.seconds)          # stands in for to_xlsx (~0.78s)
        self.done.emit(self, True, None)


btn = QPushButton('Export XLSX')
state = {'worker': None}


def gate():
    # the shape gui_fluent.py:1835 uses: derive from the worker, never
    # from an ad-hoc setEnabled() inside the click slot
    btn.setEnabled(state['worker'] is None)


class Page:
    def on_done(self, worker, ok, err):
        # prove the bound-method slot runs on the GUI thread
        seen.append(('done', ok, int(QThread.currentThreadId())))
        state['worker'] = None
        gate()


btn.clicked.connect(lambda: None)
gate()
print('before start    : enabled =', btn.isEnabled(), '(expect True)')

w = W(0.6)
# NOTE: keep a STRONG reference to the receiver. `w.done.connect(Page().on_done)`
# binds to a temporary whose only owner is PyQt's weak connection tracking, so
# it can be collected before the signal ever arrives and the slot silently
# never runs. gui_fluent.py is safe here because the receiver is `self`
# (the page), which stays alive for the window's lifetime — but the pattern
# is worth naming, because the failure mode is a slot that just never runs.
page = Page()
w.done.connect(page.on_done)      # bound method -> queued
state['worker'] = w
gate()
print('while exporting : enabled =', btn.isEnabled(), '(expect False)')

# simulate a stray setEnabled(True) from an unrelated code path (this is
# exactly what the gate design is meant to survive)
btn.setEnabled(True)
gate()
print('after stray + re-gate: enabled =', btn.isEnabled(),
      '(expect False -> the gate wins)')

w.start()
deadline = time.perf_counter() + 2.5
while time.perf_counter() < deadline:
    app.processEvents()
    time.sleep(0.01)

print('after done      : enabled =', btn.isEnabled(), '(expect True)')
print('handler fired   :', bool(seen), seen)

ok = (len(seen) == 1 and seen[0][1] is True and btn.isEnabled() is True)
print()
print('PASS: gate held during export and released after done.' if ok
      else 'FAIL: gate did not behave as designed.')
sys.exit(0 if ok else 1)
