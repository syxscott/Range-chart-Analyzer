// js/sharpen_worker.js — FE-BORROW-2026-09-20 (domain W): the unsharp-mask
// Worker GLUE.
//
// It deliberately contains NO copy of the sharpening kernel. The single
// source of truth is `rcaUnsharpMaskCore` in js/minimax.js, and minimax.js
// builds this Worker as a `blob:` URL whose text is
//
//     'use strict'  +  RCA_UNSHARP_MAX_PIXELS / RCA_UNSHARP_MAX_RADIUS
//                   +  String(rcaUnsharpMaskCore)  +  <this file, verbatim>
//
// so the kernel exists exactly once in the tree and cannot drift. That also
// makes this file Node-readable: tests_sharpen.js reads it off disk, runs it
// against a fake `self` in the same scope the production composition uses,
// and asserts both that it produces the legacy pixels and that it never
// grows its own copy of the math (see the "single source" checks there).
//
// Two supported instantiation routes:
//   1. the blob Worker built by rcaUnsharpMaskAsync — kernel already present,
//      the importScripts fallback below is then a no-op;
//   2. `new Worker('js/sharpen_worker.js')` straight from the same-origin
//      static server — importScripts('minimax.js') resolves next to this
//      file and brings in the kernel. (A blob Worker cannot resolve a
//      relative importScripts, hence the try/catch.)
//
// Protocol
//   in : { id, src, opts } with src an RGBA buffer of width*height*4 entries
//        (transferred), opts = { width, height, radius, percent, threshold }
//   out: { id, ok: true,  dst }   dst is transferred back (zero copy)
//        { id, ok: false, error } the main thread falls back to its own copy
//                                 of the same core, so pixels never change
//
'use strict';

if (typeof rcaUnsharpMaskCore !== 'function') {
  try {
    if (typeof importScripts === 'function') importScripts('minimax.js');
  } catch (_e) { /* blob worker: relative import is not allowed, ignore */ }
}

self.onmessage = function (ev) {
  const msg = (ev && ev.data) || {};
  const id = msg.id;
  const out = { id: id };
  try {
    if (typeof rcaUnsharpMaskCore !== 'function') {
      throw new Error('sharpen kernel unavailable in worker scope');
    }
    const dst = rcaUnsharpMaskCore(msg.src, msg.opts);
    out.ok = true;
    out.dst = dst;
    self.postMessage(out, (dst && dst.buffer !== undefined) ? [dst.buffer] : []);
  } catch (e) {
    out.ok = false;
    out.error = (e && e.message) ? String(e.message) : String(e);
    try { self.postMessage(out); } catch (_e) { /* channel gone: caller times out */ }
  }
};
