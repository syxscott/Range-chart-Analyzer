// minimax.js - call MiniMax M3 vision API and normalize the result.
// Mirrors RLPE range_chart_extractor.extract_range_chart. Never throws:
// returns { ok, data, error, errorKey, raw, truncated }.
'use strict';

// Downscale an image File to a JPEG/PNG data URL whose long edge <= maxEdge.
// Returns { dataUrl, mime, width, height, resized }.
//
// REVIEW-2026-11-07 (low): optional opts.signal — when aborted, the
// FileReader is stopped and the promise rejects with Error('aborted').
// Before, a superseded file-selection let the FileReader + Image decode
// run to completion in the background (the caller's token check already
// dropped the stale result, but the CPU/memory work was wasted).
function rcaLoadAndMaybeResize(file, maxEdge, opts) {
  opts = opts || {};
  const signal = opts.signal || null;
  return new Promise((resolve, reject) => {
    let settled = false;
    const reader = new FileReader();
    const cleanup = () => { if (signal) signal.removeEventListener('abort', onAbort); };
    const fail = (msg) => { if (!settled) { settled = true; cleanup(); reject(new Error(msg)); } };
    const onAbort = () => {
      if (settled) return;
      settled = true;
      cleanup();
      try { reader.abort(); } catch (_e) { /* ignore */ }
      reject(new Error('aborted'));
    };
    reader.onerror = () => fail('imageRead');
    reader.onabort = () => fail('aborted');
    reader.onload = () => {
      const originalDataUrl = reader.result;
      const img = new Image();
      img.onerror = () => fail('imageRead');
      // FE-BORROW-2026-09-20 (domain W): this handler is now async because the
      // `enhance` step awaits the sharpen Worker. An async DOM event handler
      // swallows its own rejections, which would leave the caller awaiting
      // forever, so it is wired through `fail()` below.
      const onImgLoaded = async () => {
        if (signal && signal.aborted) { fail('aborted'); return; }
        const w = img.naturalWidth;
        const h = img.naturalHeight;
        const longEdge = Math.max(w, h);
        if (!maxEdge || longEdge <= maxEdge) {
          resolve({
            dataUrl: originalDataUrl,
            mime: file.type || 'image/png',
            width: w,
            height: h,
            resized: false,
          });
          return;
        }
        const scale = maxEdge / longEdge;
        const nw = Math.round(w * scale);
        const nh = Math.round(h * scale);
        const canvas = document.createElement('canvas');
        canvas.width = nw;
        canvas.height = nh;
        const ctx = canvas.getContext('2d');
        ctx.imageSmoothingEnabled = true;
        ctx.imageSmoothingQuality = 'high';
        if (opts.enhance) {
          // FE-FIX-2026-09-21 (audit MED): the sharpen request cycle starts
          // right here (one enhance per file selection), so this is where the
          // worker failure budget is re-armed for the new run. Before, the
          // budget that pins the worker path OFF was page-session sticky and
          // rcaSharpenWorkerReset had no production caller.
          rcaSharpenRunBegin();
          // Front-end image enhancement for thin lines / small text.
          // 1) Supersample: draw into a ~2x temp canvas (capped so memory
          //    stays bounded), then downsample back to the target size. This
          //    recovers smoother detail than a single direct downscale.
          // 2) Light unsharp-mask (3x3 blur) on the final canvas to sharpen
          //    edges without over-exposing or ringing. Without `enhance`
          //    the path below is unchanged.
          //    FE-BORROW-2026-09-20 (domain W): `radius: 1` is the legacy 3x3
          //    box and `0.4` is the old `amount`, so the OUTPUT PIXELS ARE
          //    BIT-IDENTICAL to the pre-change loop (tests_sharpen.js pins
          //    that against a copy of the old code); only WHERE the loop runs
          //    changed — inside js/sharpen_worker.js when a Worker can be
          //    built, on the main thread otherwise. rcaUnsharpMaskAsync never
          //    rejects, so the await below cannot strand the load promise.
          const up = 2;
          let uw = Math.round(nw * up);
          let uh = Math.round(nh * up);
          const MAX_INTERMEDIATE = 4096;
          if (uw > MAX_INTERMEDIATE) {
            const r = MAX_INTERMEDIATE / uw; uw = MAX_INTERMEDIATE; uh = Math.round(uh * r);
          }
          if (uh > MAX_INTERMEDIATE) {
            const r = MAX_INTERMEDIATE / uh; uh = MAX_INTERMEDIATE; uw = Math.round(uw * r);
          }
          const tmp = document.createElement('canvas');
          tmp.width = uw; tmp.height = uh;
          const tctx = tmp.getContext('2d');
          tctx.imageSmoothingEnabled = true;
          tctx.imageSmoothingQuality = 'high';
          tctx.drawImage(img, 0, 0, uw, uh);
          ctx.drawImage(tmp, 0, 0, nw, nh);
          await rcaUnsharpMaskAsync(ctx, nw, nh, 0.4, 1, { radius: 1 });
          // The Worker hop yields to the event loop: a file selection
          // superseded mid-sharpen (or an abort) must not continue to encode
          // and resolve — the caller drops the result anyway.
          if (signal && signal.aborted) { fail('aborted'); return; }
        } else {
          ctx.drawImage(img, 0, 0, nw, nh);
        }
        // Prefer lossless PNG for downscaled charts so the small italic
        // species names stay sharp — JPEG re-compression blurs dense chart
        // text and is a known cause of OCR misreads. Only keep JPEG when the
        // source is JPEG and the resized image is large (a lossless PNG would
        // be excessively big).
        const resizedIsLarge = (nw * nh) > (2500 * 2500);
        const outMime = (file.type === 'image/jpeg' && resizedIsLarge) ? 'image/jpeg' : 'image/png';
        const dataUrl = outMime === 'image/jpeg'
          ? canvas.toDataURL('image/jpeg', 0.95)
          : canvas.toDataURL('image/png');
        resolve({ dataUrl, mime: outMime, width: nw, height: nh, resized: true });
      };
      img.onload = () => {
        Promise.resolve().then(onImgLoaded).catch((e) => fail((e && e.message) || 'imageRead'));
      };
      img.src = originalDataUrl;
    };
    if (signal) {
      if (signal.aborted) { onAbort(); return; }
      signal.addEventListener('abort', onAbort);
    }
    reader.readAsDataURL(file);
  });
}

// ---------------------------------------------------------------------------
// FE-BORROW-2026-09-20 (domain W): unsharp-mask sharpening — pure kernel +
// Worker offload + synchronous fallback.
//
// The legacy `rcaUnsharpMask` ran the whole w*h*3*9 tap loop on the main
// thread. At the 4096px supersample ceiling used by the `enhance` path that
// is ~50M taps and it froze the UI for seconds on a scanned figure. The work
// is now split in three layers:
//
//   rcaUnsharpMaskCore(src, opts, dst)
//       the pure math. TypedArray (or plain Array) in, same kind out, no DOM
//       reference at all. THIS is the single source of truth for the kernel.
//   rcaUnsharpMask(ctx | ImageData, w, h, amount, threshold, extra?)
//       the legacy synchronous signature, unchanged for existing callers; it
//       now delegates to the core instead of carrying its own copy of the loop.
//   rcaUnsharpMaskAsync(ctx | ImageData, ...) -> Promise
//       runs the core inside js/sharpen_worker.js when a Worker can be built,
//       falls back to the synchronous core when it cannot. Never rejects.
//
// Single-source rule for the worker: js/sharpen_worker.js holds ONLY the
// message glue, never a second copy of the kernel. minimax.js fetches that
// glue as text, prepends `'use strict'` + the two cap constants (emitted from
// the live values, so the kernel's own free identifiers are all in scope) +
// `String(rcaUnsharpMaskCore)`, and instantiates the result as a `blob:`
// Worker. Blob URL rather than `new Worker('js/sharpen_worker.js')` because
// (a) it survives cache-busting query strings and a relocated js/ dir — the
// URL is derived from minimax.js's own <script src> — and (b) it never needs
// the worker file to be reachable as a worker *script path* by CSP/whitelist,
// only as a same-origin GET. Under `file://` (no fetch) or a pre-Worker
// environment the build fails once, is remembered, and every later call takes
// the synchronous path — i.e. exactly the pre-change behaviour.
// ---------------------------------------------------------------------------

// Kernel guard rails. `rcaSharpenWorkerSource` re-declares BOTH from these
// live values inside the composed worker script; keep that list in sync if a
// new free identifier is ever introduced in the kernel (tests_sharpen.js
// executes the composed text in a bare worker-like scope, so a missed
// identifier fails there rather than in production).
const RCA_UNSHARP_MAX_PIXELS = 16_000_000;   // legacy 16M-pixel skip guard
const RCA_UNSHARP_MAX_RADIUS = 8;            // 17x17 taps: beyond this the O(k^2) loop stops being a good idea

// The unsharp kernel. Pure: no canvas, no `ctx`, no globals but the two caps.
// src      RGBA pixel bytes, width*height*4 entries. In the browser this IS
//          `ImageData.data` (a Uint8ClampedArray), so the rounding + clamping
//          of the written float is the element type's own business — exactly
//          what the legacy loop relied on. Any TypedArray or plain Array works.
// opts     { width, height, radius?, percent?, threshold? }
//            radius    box half-extent k; the blur is a (2k+1)^2 box, /((2k+1)^2).
//                      default 1 == the legacy 3x3 blur divided by 9.
//            percent   high-pass add-back factor — this is the legacy `amount`
//                      (0.4 = 40% of the blur delta added back). default 0.4.
//            threshold minimum |orig - blur| before a pixel is touched. default 1.
// dst      optional output buffer; MAY alias src (in place, like the legacy
//          call). A fresh buffer of src's own kind is allocated when omitted.
// Returns dst. Throws RangeError on non-integer/non-positive dimensions, on
// width*height > RCA_UNSHARP_MAX_PIXELS, on radius > RCA_UNSHARP_MAX_RADIUS,
// on non-finite options and on short buffers — the wrappers turn that back
// into "skip silently", which is the legacy contract.
function rcaUnsharpMaskCore(src, opts, dst) {
  const o = opts || {};
  const num = (v, dflt, name) => {
    if (v === undefined || v === null) return dflt;
    const n = Number(v);
    if (!Number.isFinite(n)) throw new RangeError('rcaUnsharpMask: ' + name + ' must be a finite number');
    return n;
  };
  const width = num(o.width, null, 'width');
  const height = num(o.height, null, 'height');
  if (!Number.isInteger(width) || !Number.isInteger(height) || width <= 0 || height <= 0) {
    throw new RangeError('rcaUnsharpMask: width/height must be positive integers');
  }
  const pixels = width * height;
  // Checked before anything is allocated — that is what makes the guard
  // usable on a pathological size instead of OOM-ing on the way to it.
  if (pixels > RCA_UNSHARP_MAX_PIXELS) {
    throw new RangeError('rcaUnsharpMask: image too large (' + pixels + ' px > ' + RCA_UNSHARP_MAX_PIXELS + ')');
  }
  const need = pixels * 4;
  if (!src || typeof src.length !== 'number' || src.length < need) {
    throw new RangeError('rcaUnsharpMask: src needs width*height*4 entries');
  }
  const radius = num(o.radius, 1, 'radius');
  const percent = num(o.percent, 0.4, 'percent');
  const threshold = Math.abs(num(o.threshold, 1, 'threshold'));
  const k = Math.round(radius);
  if (k < 0 || k > RCA_UNSHARP_MAX_RADIUS) {
    throw new RangeError('rcaUnsharpMask: radius out of range (0..' + RCA_UNSHARP_MAX_RADIUS + ')');
  }
  const size = 2 * k + 1;
  const div = size * size;
  // A fresh buffer of src's own kind when the caller did not supply one.
  // NOTE: this expression is deliberately inline rather than a call into a
  // helper — the worker script is composed from `String(rcaUnsharpMaskCore)`
  // alone, so the kernel may not reference any binding but its own two caps
  // (tests_sharpen.js runs the composed text in a bare scope, which fails
  // loudly if a free identifier is ever introduced here).
  let out = dst;
  if (out === undefined || out === null) {
    out = (src.buffer !== undefined && typeof src.constructor === 'function')
      ? new src.constructor(need)
      : new Array(need);
  }
  if (!out || typeof out.length !== 'number' || out.length < need) {
    throw new RangeError('rcaUnsharpMask: dst needs width*height*4 entries');
  }
  // Pass 0: seed the output with the input. Pixels below the threshold and
  // the whole alpha channel are then simply carried through — with a FRESH
  // `dst` they would otherwise stay zeroed, and `dst === src` (the in-place
  // legacy shape) skips the copy entirely.
  if (out !== src) {
    for (let i = 0; i < need; i++) out[i] = src[i];
  }
  // Pass 1: box blur into three channels. The tap loop keeps the legacy
  // ky-outer / kx-inner order and, more importantly, accumulates INTEGERS
  // only (<= 289*255 << 2^24), so the sum is exact and the /div result is
  // bit-identical to the old `s / 9` for k == 1.
  const blur = new Float32Array(pixels * 3);
  for (let y = 0; y < height; y++) {
    for (let x = 0; x < width; x++) {
      const pi = (y * width + x) * 3;
      for (let c = 0; c < 3; c++) {
        let s = 0;
        for (let ky = -k; ky <= k; ky++) {
          const sy = y + ky;
          const ry = (sy < 0 ? 0 : (sy >= height ? height - 1 : sy)) * width;
          for (let kx = -k; kx <= k; kx++) {
            const sx = x + kx;
            s += src[(ry + (sx < 0 ? 0 : (sx >= width ? width - 1 : sx))) * 4 + c];
          }
        }
        blur[pi + c] = s / div;
      }
    }
  }
  // Pass 2: add the high-pass signal back where it clears the threshold.
  for (let y = 0; y < height; y++) {
    for (let x = 0; x < width; x++) {
      const p = y * width + x;
      const oi = p * 4;
      const bi = p * 3;
      for (let c = 0; c < 3; c++) {
        const orig = src[oi + c];
        const diff = orig - blur[bi + c];
        if (Math.abs(diff) >= threshold) {
          const v = orig + percent * diff;
          out[oi + c] = v < 0 ? 0 : (v > 255 ? 255 : v);
        }
      }
    }
  }
  return out;
}

// Same-kind copy — the worker job transfers its input, so the caller's
// ImageData must not be the buffer that gets detached.
function rcaClonePixels(src) {
  if (src && src.buffer !== undefined && typeof src.constructor === 'function') {
    try { return new src.constructor(src); } catch (_e) { /* fall through */ }
  }
  return Array.prototype.slice.call(src);
}

function rcaCopyPixels(src, dst) {
  for (let i = 0, n = dst.length; i < n; i++) dst[i] = src[i];
}

// Legacy positional args -> core options bag. `extra` may be a bare radius
// number or { radius, width, height, percent, threshold }.
//
// The three defaults below MIRROR rcaUnsharpMaskCore's own defaults; they are
// resolved here so that a Worker message always carries a fully-specified
// option set (the two engines then cannot disagree about an omitted field).
// tests_sharpen.js `core-defaults-agree-with-wrapper-defaults` pins the pair.
function rcaUnsharpArgs(w, h, amount, threshold, extra) {
  const ex = typeof extra === 'number' ? { radius: extra } : (extra || {});
  return {
    width: ex.width !== undefined ? ex.width : w,
    height: ex.height !== undefined ? ex.height : h,
    radius: (ex.radius === undefined || ex.radius === null) ? 1 : ex.radius,
    percent: (amount === undefined || amount === null)
      ? ((ex.percent === undefined || ex.percent === null) ? 0.4 : ex.percent)
      : amount,
    threshold: (threshold === undefined || threshold === null)
      ? ((ex.threshold === undefined || ex.threshold === null) ? 1 : ex.threshold)
      : threshold,
  };
}

// Accepts a 2D context (getImageData/putImageData) or a bare ImageData
// (mutated in place — the caller owns the write-back).
function rcaUnsharpPickPixels(target, o) {
  // Cheap pre-check BEFORE getImageData: pulling 16M+ pixels off a canvas
  // allocates ~64 MB just so the kernel can reject them a moment later — the
  // legacy guard did it on the raw dimensions, and so does this one.
  if (Number.isFinite(o.width) && Number.isFinite(o.height)
      && o.width * o.height > RCA_UNSHARP_MAX_PIXELS) {
    throw new RangeError('rcaUnsharpMask: image too large ('
      + (o.width * o.height) + ' px > ' + RCA_UNSHARP_MAX_PIXELS + ')');
  }
  if (target && target.data && typeof target.data.length === 'number') {
    return { img: target, ctx: null };
  }
  if (target && typeof target.getImageData === 'function') {
    return { img: target.getImageData(0, 0, o.width, o.height), ctx: target };
  }
  throw new TypeError('rcaUnsharpMask: expected a 2D canvas context or an ImageData');
}

function rcaUnsharpWriteBack(target, picked) {
  const ctx = picked.ctx || target;
  if (ctx && typeof ctx.putImageData === 'function') ctx.putImageData(picked.img, 0, 0);
}

// Synchronous path — the pre-change behaviour, now expressed through the core
// so there is exactly one sharpening implementation in the tree.
// Wrapped in try/catch: a tainted or oversized canvas skips sharpening rather
// than throwing.
function rcaUnsharpMaskSync(target, w, h, amount, threshold, extra) {
  try {
    const o = rcaUnsharpArgs(w, h, amount, threshold, extra);
    const picked = rcaUnsharpPickPixels(target, o);
    rcaUnsharpMaskCore(picked.img.data, o, picked.img.data);   // in place
    rcaUnsharpWriteBack(target, picked);
    return true;
  } catch (_e) { /* sharpening is best-effort */ return false; }
}

// Public legacy signature: rcaUnsharpMask(ctx, w, h, amount, threshold).
function rcaUnsharpMask(target, w, h, amount, threshold, extra) {
  return rcaUnsharpMaskSync(target, w, h, amount, threshold, extra);
}

// --- worker plumbing -------------------------------------------------------

const RCA_SHARPEN_WORKER_PATH = 'js/sharpen_worker.js';
const RCA_SHARPEN_JOB_TIMEOUT_MS = 30_000;
const RCA_SHARPEN_MAX_JOB_FAILURES = 2;

let _rcaSharpenWorker = null;            // live Worker, or null
let _rcaSharpenWorkerPromise = null;     // in-flight build / cached "unavailable"
let _rcaSharpenGluePromise = null;       // in-flight/completed glue fetch
let _rcaSharpenBlobUrl = null;           // revoke on teardown, NOT right after new Worker()
let _rcaSharpenJobSeq = 0;
let _rcaSharpenJobFailures = 0;
const _rcaSharpenPending = new Map();    // id -> { resolve, reject, timer }

// Cheap capability gate — the branch the tests drive by stubbing `typeof`.
// `fetch` is required because the kernel reaches the worker as *text*; a
// browser without Worker, without fetch (file:// in some engines) or without
// object URLs simply cannot take this path.
function rcaSharpenWorkerSupported() {
  return typeof Worker === 'function'
    && typeof fetch === 'function'
    && typeof Blob === 'function'
    && typeof URL !== 'undefined' && typeof URL.createObjectURL === 'function';
}

// Which path an async call would take, for diagnostics and tests.
function rcaSharpenPath() {
  return rcaSharpenWorkerSupported() ? 'worker' : 'sync';
}

// Resolve the glue URL from minimax.js's OWN script tag so a cache-busting
// `?v=` query or a relocated js/ dir keeps working; plain relative path
// otherwise.
function rcaSharpenWorkerUrl() {
  try {
    if (typeof document === 'undefined' || !document || !document.querySelectorAll) {
      return RCA_SHARPEN_WORKER_PATH;
    }
    const tags = document.querySelectorAll('script[src]');
    for (let i = 0; i < tags.length; i++) {
      const src = tags[i].src || tags[i].getAttribute('src') || '';
      if (src.indexOf('minimax.js') === -1) continue;
      // Swap the basename only — a `?v=` cache-buster or `#hash` suffix and a
      // relocated js/ directory carry over to the worker request unchanged.
      return src.replace('minimax.js', 'sharpen_worker.js');
    }
  } catch (_e) { /* fall back to the relative path */ }
  return RCA_SHARPEN_WORKER_PATH;
}

// Compose the worker script from the fetched glue. `glueText` is the verbatim
// body of js/sharpen_worker.js; the kernel is appended as TEXT, never copied,
// which is what keeps the two engines from drifting.
function rcaSharpenWorkerSource(glueText) {
  const glue = typeof glueText === 'string' ? glueText : '';
  const kernel = String(rcaUnsharpMaskCore);
  // A classic-script function declaration stringifies to its own declaration;
  // only defend against an engine handing back a bare expression.
  const kernelText = /^\s*function\b/.test(kernel)
    ? kernel
    : 'const rcaUnsharpMaskCore = ' + kernel + ';';
  return "'use strict';\n"
    + 'const RCA_UNSHARP_MAX_PIXELS = ' + RCA_UNSHARP_MAX_PIXELS + ';\n'
    + 'const RCA_UNSHARP_MAX_RADIUS = ' + RCA_UNSHARP_MAX_RADIUS + ';\n'
    + kernelText + '\n'
    + glue + '\n';
}

function rcaSharpenGlueText() {
  if (_rcaSharpenGluePromise) return _rcaSharpenGluePromise;
  _rcaSharpenGluePromise = Promise.resolve()
    .then(() => fetch(rcaSharpenWorkerUrl()))
    .then((res) => (res && res.ok)
      ? res.text()
      : Promise.reject(new Error('sharpen worker script HTTP ' + ((res && res.status) || 0))))
    .then((text) => (typeof text === 'string' && text.indexOf('onmessage') !== -1
      ? text
      : Promise.reject(new Error('sharpen worker script unusable'))))
    .catch((e) => {
      _rcaSharpenGluePromise = null;   // let a later call retry once
      throw e;
    });
  return _rcaSharpenGluePromise;
}

function rcaOnSharpenWorkerMessage(ev) {
  const msg = (ev && ev.data) || null;
  if (!msg || typeof msg.id !== 'number') return;
  const job = _rcaSharpenPending.get(msg.id);
  if (!job) return;                 // answered after a timeout-teardown: drop
  _rcaSharpenPending.delete(msg.id);
  if (job.timer) clearTimeout(job.timer);
  if (msg.ok && msg.dst) {
    // FE-FIX-2026-09-21 (audit MED): a finished job is proof the worker is
    // healthy — a single 30 s timeout (huge/slow image) must not stay on
    // the books, or two unlucky images would condemn a perfectly good
    // worker for the whole run. The budget now measures CONSECUTIVE
    // failures.
    _rcaSharpenJobFailures = 0;
    job.resolve(msg.dst);
  }
  else job.reject(new Error(msg.error || 'sharpen worker rejected the job'));
}

function rcaSharpenTeardown(reason) {
  const worker = _rcaSharpenWorker;
  _rcaSharpenWorker = null;
  // The cached build verdict pointed at this worker; once it is gone the
  // verdict is stale. Re-arm a rebuild, except when the job-failure budget is
  // spent — then this deployment is pinned to the synchronous path, which is
  // the whole point of the budget (no rebuild-per-image loop).
  _rcaSharpenWorkerPromise = _rcaSharpenJobFailures >= RCA_SHARPEN_MAX_JOB_FAILURES
    ? Promise.resolve(null)
    : null;
  if (_rcaSharpenBlobUrl && typeof URL !== 'undefined' && typeof URL.revokeObjectURL === 'function') {
    try { URL.revokeObjectURL(_rcaSharpenBlobUrl); } catch (_e) { /* best effort */ }
  }
  _rcaSharpenBlobUrl = null;
  _rcaSharpenGluePromise = null;
  for (const id of Array.from(_rcaSharpenPending.keys())) {
    const job = _rcaSharpenPending.get(id);
    _rcaSharpenPending.delete(id);
    if (job.timer) clearTimeout(job.timer);
    job.reject(new Error(reason || 'sharpen worker torn down'));
  }
  if (worker) { try { worker.terminate(); } catch (_e) { /* ignore */ } }
}

// Forget the cached worker (and the failed-build verdict) so the next call
// rebuilds it. Exported for tests and for a future "retry sharpening" UI hook.
function rcaSharpenWorkerReset() {
  rcaSharpenTeardown('reset');
  _rcaSharpenWorkerPromise = null;
  _rcaSharpenJobFailures = 0;
}

// FE-FIX-2026-09-21 (audit MED): the job-failure budget used to be
// page-session STICKY — once 2 failures spent it, the worker path stayed
// pinned OFF forever and rcaSharpenWorkerReset (the only un-pinner) had no
// production caller. The budget is now PER RUN: this is called at the start
// of every sharpen request cycle (rcaLoadAndMaybeResize's enhance branch)
// to re-arm it. Unlike rcaSharpenWorkerReset it deliberately does NOT tear
// down a healthy cached worker (no needless glue re-fetch / rebuild for
// well-behaved deployments); it only undoes the budget's own pin. A
// capability or glue-fetch "unavailable" verdict (failures === 0, e.g. a
// file:// deployment) is left cached exactly as before — that pin is by
// design permanent, see tests_sharpen.js 'fetch-fail-caches-verdict'.
function rcaSharpenRunBegin() {
  // Only the budget-caused pin is cleared: teardown records it as
  // failures >= MAX with the worker gone and the cached verdict already
  // resolved to null. An in-flight build (failures < MAX) is untouched.
  const pinnedByBudget = _rcaSharpenJobFailures >= RCA_SHARPEN_MAX_JOB_FAILURES
    && !_rcaSharpenWorker;
  _rcaSharpenJobFailures = 0;
  if (pinnedByBudget) _rcaSharpenWorkerPromise = null;   // let this run rebuild
}

function rcaSharpenCreateWorker(glueText) {
  const blob = new Blob([rcaSharpenWorkerSource(glueText)], { type: 'application/javascript' });
  const url = URL.createObjectURL(blob);
  _rcaSharpenBlobUrl = url;
  let worker;
  try {
    worker = new Worker(url);
  } catch (e) {
    // FE-FIX-2026-09-21 (audit MED): `new Worker(url)` can THROW
    // SYNCHRONOUSLY (CSP refusing the blob: URL, worker quota). The throw
    // used to escape into the .then() chain of rcaGetSharpenWorker, which
    // cached the REJECTED promise forever: every later sharpen silently
    // reused the broken promise, the Blob URL was never revoked, and
    // rcaSharpenTeardown — the only revoker — never ran. Revoke the URL
    // here, count the attempt against the (now per-run, see
    // rcaSharpenRunBegin) failure budget so a hostile deployment stops
    // rebuilding, and rethrow: rcaGetSharpenWorker turns this into a
    // RETRYABLE null verdict instead of a poisoned cache.
    if (typeof URL !== 'undefined' && typeof URL.revokeObjectURL === 'function') {
      try { URL.revokeObjectURL(url); } catch (_e) { /* best effort */ }
    }
    if (_rcaSharpenBlobUrl === url) _rcaSharpenBlobUrl = null;
    _rcaSharpenJobFailures += 1;
    throw e;
  }
  worker.onmessage = rcaOnSharpenWorkerMessage;
  // A worker that dies (CSP refused the blob, a syntax error in the composed
  // script, an unhandled throw) counts against the same small budget the
  // timeout and postMessage failures use, so a hostile deployment falls back
  // to the synchronous core for good instead of rebuilding per image.
  worker.onerror = () => { _rcaSharpenJobFailures += 1; rcaSharpenTeardown('worker error'); };
  _rcaSharpenWorker = worker;
  return worker;
}

// -> Promise<Worker|null>. `null` means "no worker here, use the synchronous
// core"; that verdict is cached so a file:// deployment pays for one fetch,
// not one per image.
function rcaGetSharpenWorker() {
  if (_rcaSharpenWorker) return Promise.resolve(_rcaSharpenWorker);
  if (_rcaSharpenWorkerPromise) return _rcaSharpenWorkerPromise;
  const unavailable = () => { _rcaSharpenWorkerPromise = Promise.resolve(null); return _rcaSharpenWorkerPromise; };
  if (!rcaSharpenWorkerSupported()) return unavailable();
  if (_rcaSharpenJobFailures >= RCA_SHARPEN_MAX_JOB_FAILURES) return unavailable();
  _rcaSharpenWorkerPromise = rcaSharpenGlueText().then(
    (glue) => {
      try {
        return rcaSharpenCreateWorker(glue);
      } catch (_e) {
        // FE-FIX-2026-09-21 (audit MED): a synchronous `new Worker`
        // constructor throw (cleaned up inside rcaSharpenCreateWorker) must
        // NOT be cached as a rejected promise — that poisoned every later
        // sharpen for the whole page session. Resolve THIS call with null
        // (= synchronous core, the never-rejects contract holds) and leave
        // the verdict empty, so a later call retries — until the per-run
        // failure budget spent above pins this path OFF at the entry check.
        _rcaSharpenWorkerPromise = null;
        return null;
      }
    },
    () => unavailable()
  );
  return _rcaSharpenWorkerPromise;
}

// One sharpen job. `src` is transferred (detached here, reborn in the worker),
// which is why the caller hands over a private clone.
function rcaSharpenJob(worker, src, o) {
  return new Promise((resolve, reject) => {
    const id = ++_rcaSharpenJobSeq;
    let timer = null;
    if (typeof setTimeout === 'function') {
      timer = setTimeout(() => {
        if (!_rcaSharpenPending.has(id)) return;
        _rcaSharpenPending.delete(id);
        _rcaSharpenJobFailures += 1;
        rcaSharpenTeardown('sharpen worker timed out');
        reject(new Error('sharpen worker timed out'));
      }, RCA_SHARPEN_JOB_TIMEOUT_MS);
    }
    _rcaSharpenPending.set(id, { resolve, reject, timer });
    try {
      const transfer = (src && src.buffer !== undefined) ? [src.buffer] : [];
      worker.postMessage({ id: id, src: src, opts: o }, transfer);
    } catch (e) {
      if (timer) clearTimeout(timer);
      _rcaSharpenPending.delete(id);
      _rcaSharpenJobFailures += 1;
      rcaSharpenTeardown('postMessage failed');
      reject(e);
    }
  });
}

// Asynchronous sharpening. Always resolves with
//   { applied: boolean, via: 'worker' | 'sync' | 'none', error?: string }
// and NEVER rejects: sharpening is best-effort, and a rejection here would
// turn a preview that used to work into a failed upload. `via: 'sync'` covers
// both "no worker available" and "the worker failed", so the pixel output is
// identical either way; `applied: false` only on a canvas/CSP/oversize error,
// i.e. the legacy silent skip.
function rcaUnsharpMaskAsync(target, w, h, amount, threshold, extra) {
  let o, picked;
  try {
    o = rcaUnsharpArgs(w, h, amount, threshold, extra);
    picked = rcaUnsharpPickPixels(target, o);
  } catch (e) {
    return Promise.resolve({ applied: false, via: 'none', error: (e && e.message) || String(e) });
  }
  const runSync = () => {
    try {
      rcaUnsharpMaskCore(picked.img.data, o, picked.img.data);
      rcaUnsharpWriteBack(target, picked);
      return { applied: true, via: 'sync' };
    } catch (e) {
      return { applied: false, via: 'none', error: (e && e.message) || String(e) };
    }
  };
  if (!rcaSharpenWorkerSupported()) return Promise.resolve(runSync());
  return rcaGetSharpenWorker().then((worker) => {
    if (!worker) return runSync();
    let clone;
    try { clone = rcaClonePixels(picked.img.data); } catch (e) { return runSync(); }
    return rcaSharpenJob(worker, clone, o).then(
      (dst) => {
        try {
          rcaCopyPixels(dst, picked.img.data);
          rcaUnsharpWriteBack(target, picked);
          return { applied: true, via: 'worker' };
        } catch (e) {
          return { applied: false, via: 'none', error: (e && e.message) || String(e) };
        }
      },
      () => runSync()          // worker broke: same math, main thread, no freeze unless unavoidable
    );
  }, () => runSync());
}

// Split a data URL into { mediaType, base64 }.
function rcaSplitDataUrl(dataUrl) {
  const m = /^data:([^;]+);base64,(.*)$/.exec(dataUrl);
  if (!m) return { mediaType: 'image/png', base64: '' };
  return { mediaType: m[1], base64: m[2] };
}

// ---------------------------------------------------------------------------
// Scalar coercion helpers — mirrors of the module-level helpers in
// rca_core/extractor.py. Every normalizer below goes through these so the
// browser and the server agree on what "stringify this field" means.
// ---------------------------------------------------------------------------

// Mirror of _stringify_scalar: stringify scalars only; containers collapse to
// "" instead of leaking a repr into a scientific identifier field.
//
// REVIEW-2026-09-20 #14 — this is the ONE stringifier in this file (the
// normalizers used to carry five local `asStr`/`s` closures with three
// different null/bool/container rules). Known deliberate divergences from
// Python, all of them cosmetic and none on a well-formed payload:
//   1. Container values: `_stringify_scalar` returns "" on both sides, but
//      the range-chart (`normalize_result`'s local `s`) and columnar
//      (`normalize_columnar_result`'s local `s`) normalizers still call bare
//      `str(v)` in Python, so a dict in one of THOSE scalar fields yields
//      "{'a': 1}" server-side where JS yields "". JS is lossless-or-empty,
//      never a fabricated Python repr; no test pins the Python repr.
//   2. Float repr: Python str(1.0) == "1.0" and str(1e-7) == "1e-07", JS
//      String(1.0) == "1" and String(1e-7) == "1e-7". Only reachable when the
//      model emits a raw number in a string field; the aggregate layer
//      re-normalizes both spellings through rcaPyFloat before comparing.
//   3. Booleans: Python str(True) == "True", _stringify_scalar == "true";
//      both engines use the lowercase "true"/"false" form here (rule 1 means
//      the two range-chart str() fields are again the exception).
function rcaStringifyScalar(value) {
  if (value === null || value === undefined) return '';
  if (typeof value === 'boolean') return value ? 'true' : 'false';
  if (Array.isArray(value) || typeof value === 'object') return '';
  return String(value);
}

// Python truthiness for JSON-shaped values — `bool(v)`. Distinct from JS
// truthiness in exactly two ways that matter here: an empty list/dict is
// FALSY in Python ({} / [] are the model's "nothing here" spelling) and NaN
// never occurs in JSON.
function rcaPyTruthy(value) {
  if (value === null || value === undefined) return false;
  if (typeof value === 'boolean') return value;
  if (typeof value === 'number') return value !== 0;
  if (typeof value === 'string') return value.length > 0;
  if (Array.isArray(value)) return value.length > 0;
  if (typeof value === 'object') return Object.keys(value).length > 0;
  return Boolean(value);
}

// Mirror of Python's `a or b` on a value that may be 0 / "" / [] / {} / false.
// `a || b` in JS differs on the container and (importantly) the numeric 0
// cases, e.g. `_normalize_biozone_into`'s `bz.get("zone_type") or inferred_zt`
// must take `inferred_zt` for an explicit 0, and `s(item.get("name") or "")`
// must take "" for an empty list.
function rcaPyOr(value, fallback) {
  return rcaPyTruthy(value) ? value : fallback;
}

// Mirror of Python `float()` for the confidence/numeric fields: returns null
// where Python raises (TypeError on a container, ValueError on "1.4x"),
// which is what makes `float("90%")` a 0.0 fallback rather than JS
// `parseFloat("90%")` === 90. Same rule as rcaPyFloat in js/aggregate.js —
// kept as a local copy because aggregate.js and minimax.js each have to run
// standalone (tests_aggregate.js loads aggregate.js alone), and the two are
// pinned against the same Python oracle by tests/test_parity.py.
const _RCA_PY_FLOAT_RE = /^[+-]?(?:\d[\d_]*(?:\.[\d_]*)?|\.\d[\d_]*)(?:[eE][+-]?\d+)?$/;
function rcaPyFloatOrNull(value) {
  if (typeof value === 'number') return value;
  if (typeof value === 'boolean') return value ? 1 : 0;   // float(True) == 1.0
  if (typeof value !== 'string') return null;             // dict / list -> TypeError
  const s = value.trim();
  if (!s) return null;
  const low = s.toLowerCase();
  if (low === 'inf' || low === '+inf' || low === 'infinity' || low === '+infinity') return Infinity;
  if (low === '-inf' || low === '-infinity') return -Infinity;
  if (low === 'nan' || low === '+nan' || low === '-nan') return NaN;
  if (!_RCA_PY_FLOAT_RE.test(s)) return null;
  return Number(s.replace(/_/g, ''));
}

// Python `max(0.0, min(1.0, float(x)))` with the try/except the normalizers
// wrap it in. `x or 0.0` in two of the modes means an explicit 0/null/""
// collapses to 0.0 BEFORE the float() call — same result either way.
function rcaConfidenceClamped(value) {
  const n = rcaPyFloatOrNull(value === undefined ? 0.0 : value);
  if (n === null || Number.isNaN(n)) return 0.0;
  return Math.max(0.0, Math.min(1.0, n));
}

// Mirror of _normalize_optional_int: an EXACT integer or null. Note the two
// differences from Number()+isInteger: a numeric STRING "9" is accepted (it
// goes through float()), and a non-integral float (8.5) is rejected — while
// the columnar bed-index field below (rcaBedIndex, mirror of fi()) truncates
// and flags instead.
function rcaOptionalInt(value) {
  if (value === null || value === undefined || value === '' || typeof value === 'boolean') return null;
  const n = rcaPyFloatOrNull(value);
  if (n === null || Number.isNaN(n) || !Number.isFinite(n)) return null;
  return Number.isInteger(n) ? n : null;
}

// Mirror of _normalize_confidence (the per-ROW one — null on unparseable).
function rcaOptionalConfidence(value) {
  if (value === null || value === undefined || value === '' || typeof value === 'boolean') return null;
  const n = rcaPyFloatOrNull(value);
  if (n === null || Number.isNaN(n)) return null;
  return Math.max(0.0, Math.min(1.0, n));
}

// Mirror of _coerce_bool_flag: parse the textual spellings instead of bool(),
// which inverted "false" (non-empty string -> True) and turned null into
// False. Anything unrecognisable falls back to `defaultValue`.
function rcaCoerceBoolFlag(value, defaultValue) {
  if (value === null || value === undefined) return defaultValue;
  if (typeof value === 'boolean') return value;
  if (typeof value === 'number') return rcaPyTruthy(value);
  if (Array.isArray(value) || typeof value === 'object') return defaultValue;
  const text = String(value).trim().toLowerCase();
  if (['false', '0', 'no', 'n', 'off', 'none', 'null'].indexOf(text) !== -1) return false;
  if (['true', '1', 'yes', 'y', 'on', ''].indexOf(text) !== -1) return true;
  return defaultValue;
}

// Mirror of _carry_extras (H8). Two rules the JS version used to miss:
//   * keys already written explicitly on the row are skipped (`k not in out`),
//     so an explicit first-class field never doubles as an _extras entry;
//   * an existing `_extras` dict is MERGED into rather than replaced, which is
//     how the caller's structural hint (wrapper_key) and the model's own extra
//     keys coexist.
function rcaCarryExtras(item, known, out) {
  if (!item || typeof item !== 'object' || Array.isArray(item)) return;
  if (!out || typeof out !== 'object') return;
  const extras = {};
  let count = 0;
  for (const k of Object.keys(item)) {
    if (known.indexOf(k) !== -1) continue;
    if (Object.prototype.hasOwnProperty.call(out, k)) continue;
    extras[k] = item[k];
    count += 1;
  }
  if (!count) return;
  const existing = out._extras;
  if (existing && typeof existing === 'object' && !Array.isArray(existing)) {
    const merged = { ...existing };
    for (const k of Object.keys(extras)) merged[k] = extras[k];
    out._extras = merged;
  } else {
    out._extras = extras;
  }
}

// Mirror of _pop_array_root_extras: the wrapper keys are diagnostics, not
// data — leaving them in duplicated the whole raw payload into every export
// and inflated quality.py's `_extras`-ratio check.
function rcaPopArrayRootExtras(extras) {
  if (!extras || typeof extras !== 'object') return extras;
  delete extras._array_root;
  delete extras._note;
  return extras;
}

// Top-level extras: every key the mode does not document, with the array-root
// wrapper keys stripped (per-mode: columnar deliberately keeps them, which is
// what `keepWrapper` encodes — mirror of the one normalizer in Python that
// builds root_extras without _pop_array_root_extras).
function rcaTopExtras(parsed, knownRootKeys, keepWrapper) {
  const extras = {};
  let count = 0;
  for (const k of Object.keys(parsed || {})) {
    if (knownRootKeys.indexOf(k) !== -1) continue;
    if (!keepWrapper && (k === '_array_root' || k === '_note')) continue;
    extras[k] = parsed[k];
    count += 1;
  }
  return count ? extras : null;
}

// Mirror of _append_warning / the `if tag not in warnings` idiom every
// normalizer uses to keep the root warning list duplicate-free.
function rcaPushWarning(warnings, tag) {
  if (warnings.indexOf(tag) === -1) warnings.push(tag);
}

// Row-level `_warning`: one flag stays a bare string, several become a list —
// the same convention rca_core/aggregate.js's _add_row_warning writes, so
// rcaWarningFlags / the UI badges read both engines uniformly.
function rcaSetRowWarning(row, flags) {
  const list = Array.isArray(flags) ? flags.filter(Boolean) : [flags].filter(Boolean);
  if (!list.length) return;
  row._warning = list.length === 1 ? list[0] : list;
}

// ---------------------------------------------------------------------------
// FIX-2026-09-22 (A2): browser mirror of the extractor's coverage contract +
// geometry sidecar (rca_core/extractor.py attach_row_contract /
// geometry_from_row / _fold_axis_calibration). js/prompt.js marks
// response_kind / reason_codes / *pos_0_999 REQUIRED, yet minimax.js knew
// none of these keys, so rcaNormalizeResult swept every contract field into
// `_extras`: aggregate.js's priority-vote / union merge never fired and
// quality.js saw row.response_kind === undefined (no frontend ledger ever).
// The helpers below mirror the CURRENT Python behavior (source of truth),
// including the FIX-2026-09-22 backend items: zero-span axis_calibration ->
// unusable (no calibrated:true, contradiction guards still apply), chemical
// top/base_pos_0_999 semantic pairing (15% contradiction check), and bool
// rejection in the position / calibration parsers.
// ---------------------------------------------------------------------------

// Mirror of _add_row_flag (extractor.py): same single-string / list convention
// as rcaSetRowWarning but ADDITIVE — the contract can flag a row
// (response_kind_conflict) before the order repair flags it, and a plain
// assignment destroys the first fact (FIX-2026-09-22 audit item 2 family).
function rcaAddRowFlag(row, tag) {
  const existing = row._warning;
  if (existing === null || existing === undefined || existing === '') {
    row._warning = tag;
    return;
  }
  const flags = Array.isArray(existing) ? existing.slice() : [existing];
  if (flags.indexOf(tag) === -1) flags.push(tag);
  row._warning = flags.length === 1 ? flags[0] : flags;
}

const RCA_POS_SCALE_KEY = 'pos_0_999';
const RCA_POS_SUFFIX = '_pos_0_999';
const RCA_POS_MIN = 0;
const RCA_POS_MAX = 999;
const RCA_GEOMETRY_VERSION = 1;
const RCA_GEOMETRY_SCALE = 'pos_0_999';
const RCA_GEOMETRY_TOLERANCE = 0.15;
// Mirror of _GEOMETRY_AXIS_BY_FIELD / _GEOMETRY_VERTICAL_FIELDS.
const RCA_GEOMETRY_AXIS_BY_FIELD = { x_pos_0_999: 'x', y_pos_0_999: 'y' };
const RCA_GEOMETRY_VERTICAL_FIELDS = [
  'pos_0_999', 'depth_pos_0_999', 'level_pos_0_999', 'age_pos_0_999',
  'range_top_pos_0_999', 'range_base_pos_0_999',
  'top_pos_0_999', 'base_pos_0_999',
];
// Mirror of _GEOMETRY_SEMANTIC_KEYS, INCLUDING the FIX-2026-09-22 (audit
// item 5) chemical pairs: without top/base_pos_0_999 the anti-self-echo
// contradiction test silently never ran for that mode.
const RCA_GEOMETRY_SEMANTIC_KEYS = {
  pos_0_999: ['depth', 'depth_m', 'age_ma', 'y', 'level'],
  y_pos_0_999: ['y'],
  x_pos_0_999: ['x'],
  range_top_pos_0_999: ['range_top_idx', 'range_top'],
  range_base_pos_0_999: ['range_base_idx', 'range_base'],
  depth_pos_0_999: ['depth', 'depth_m'],
  age_pos_0_999: ['age_ma', 'age'],
  level_pos_0_999: ['level', 'depth', 'depth_m'],
  top_pos_0_999: ['top_depth_m', 'top_age_ma'],
  base_pos_0_999: ['base_depth_m', 'base_age_ma'],
};
// Mirror of _CONTRACT_SOURCE_KEYS: the row-level source keys the contract
// consumes; handed to rcaCarryExtras beside the consumed pos fields.
const RCA_CONTRACT_SOURCE_KEYS = ['response_kind', 'reason_codes', 'reason_code'];

// Mirror of _to_float_opt. NOT rcaPyFloatOrNull: Python rejects a bool here
// (bool is an int subclass and `True` is not a scientific value), and maps
// European decimal commas before float().
function rcaToFloatOpt(value) {
  if (value === null || value === undefined || typeof value === 'boolean') return null;
  if (typeof value === 'number') return value;
  if (typeof value !== 'string') return null;
  const text = value.trim().replace(/,/g, '.');
  if (!text) return null;
  return rcaPyFloatOrNull(text);
}

// Mirror of normalize_pos_0_999: strictly an INTEGER in [0, 999] ("712" is
// fine; 712.5 / 1000 / -1 / true are not — FIX-2026-09-22 audit item 8: a
// bool is never a position).
function rcaNormalizePos0999(value) {
  if (value === null || value === undefined || typeof value === 'boolean') return null;
  let parsed = null;
  if (typeof value === 'number') {
    parsed = value;
  } else if (typeof value === 'string') {
    const text = value.trim();
    if (!text) return null;
    parsed = rcaPyFloatOrNull(text);
  } else {
    return null;
  }
  if (parsed === null || !Number.isFinite(parsed) || !Number.isInteger(parsed)) return null;
  if (parsed < RCA_POS_MIN || parsed > RCA_POS_MAX) return null;
  return parsed;
}

// Mirror of _axis_domain: {at_0, at_999, unit} or null unless BOTH ends are
// numeric. Accepted shapes: [lo, hi], {at_0|bottom|base|oldest|min|start|0},
// {at_999|top|ceiling|youngest|max|end|999}.
function rcaAxisDomain(raw) {
  if (Array.isArray(raw) && raw.length === 2) {
    const low = rcaToFloatOpt(raw[0]);
    const high = rcaToFloatOpt(raw[1]);
    return (low !== null && high !== null) ? { at_0: low, at_999: high, unit: '' } : null;
  }
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null;
  let low = null;
  let high = null;
  for (const key of ['at_0', 'bottom', 'base', 'oldest', 'min', 'start', '0']) {
    if (key in raw) {
      low = rcaToFloatOpt(raw[key]);
      if (low !== null) break;
    }
  }
  for (const key of ['at_999', 'top', 'ceiling', 'youngest', 'max', 'end', '999']) {
    if (key in raw) {
      high = rcaToFloatOpt(raw[key]);
      if (high !== null) break;
    }
  }
  if (low === null || high === null) return null;
  const unit = raw.unit;
  return {
    at_0: low, at_999: high,
    unit: (typeof unit === 'string' || typeof unit === 'number') ? String(unit).trim() : '',
  };
}

// Mirror of _axis_calibration_blocks: root, then metadata, then _extras.
function rcaAxisCalibrationBlocks(payload) {
  const blocks = [];
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) return blocks;
  for (const holder of [payload, payload.metadata, payload._extras]) {
    if (!holder || typeof holder !== 'object' || Array.isArray(holder)) continue;
    const candidate = holder.axis_calibration;
    if (candidate && typeof candidate === 'object' && !Array.isArray(candidate)
        && Object.keys(candidate).length) {
      blocks.push(candidate);
    }
  }
  return blocks;
}

// Mirror of axis_domains_from: first definition of an axis name wins; unknown
// names survive verbatim.
function rcaAxisDomainsFrom(payload) {
  const out = {};
  for (const candidate of rcaAxisCalibrationBlocks(payload)) {
    for (const name of Object.keys(candidate)) {
      const domain = rcaAxisDomain(candidate[name]);
      if (domain && !(name in out)) out[name] = domain;
    }
  }
  return out;
}

// Mirror of _geometry_axis_for: an unknown `<prefix>_pos_0_999` maps to the
// axis named <prefix> ONLY when the result actually calibrates it, so a
// bar-length read can never silently borrow the vertical axis.
function rcaGeometryAxisFor(field, axes) {
  if (Object.prototype.hasOwnProperty.call(RCA_GEOMETRY_AXIS_BY_FIELD, field)) {
    return RCA_GEOMETRY_AXIS_BY_FIELD[field];
  }
  if (RCA_GEOMETRY_VERTICAL_FIELDS.indexOf(field) !== -1 || field === RCA_POS_SCALE_KEY) {
    return 'vertical';
  }
  if (field.endsWith(RCA_POS_SUFFIX)) {
    const prefix = field.slice(0, -RCA_POS_SUFFIX.length);
    // Own-property check only: model data must never resolve inherited keys
    // like "constructor" / "toString" through the prototype chain (Python
    // dicts only ever see their own keys).
    if (prefix && axes && Object.prototype.hasOwnProperty.call(axes, prefix)) {
      return prefix;
    }
  }
  return null;
}

// Mirror of pos_to_axis_value (with the FIX-2026-09-22 audit item 8 bool
// rejection: a model-emitted `true` must not map as position 1).
function rcaPosToAxisValue(pos, domain) {
  if (!domain || pos === null || pos === undefined) return null;
  if (typeof pos === 'boolean' || typeof pos !== 'number') return null;
  const low = domain.at_0;
  const high = domain.at_999;
  if (typeof low === 'boolean' || typeof high === 'boolean') return null;
  if (typeof low !== 'number' || typeof high !== 'number') return null;
  return low + (pos / RCA_POS_MAX) * (high - low);
}

// Mirror of _geometry_point -> {entry, rejected, axis}. Rejected means the
// position cannot be trusted (bad int, out of scale, zero-span calibration
// (audit item 4: keep raw pos as evidence, NEVER calibrated:true via a
// value), off-domain, or contradicting the row's own transcribed boundary by
// more than GEOMETRY_TOLERANCE of the axis span (audit item 5 covers the
// chemical top/base pairing)).
function rcaGeometryPoint(field, raw, srcRow, axes) {
  const pos = rcaNormalizePos0999(raw);
  if (pos === null) return { entry: null, rejected: true, axis: null };
  const axis = rcaGeometryAxisFor(field, axes);
  let domain = (axis && axes && Object.prototype.hasOwnProperty.call(axes, axis))
    ? axes[axis] : null;
  if ((domain === null || domain === undefined) && axes
      && Object.prototype.hasOwnProperty.call(axes, field)) domain = axes[field];
  if (domain === undefined) domain = null;
  const entry = { pos: pos, axis: axis || 'uncalibrated' };
  if (!domain) {
    // No calibration: the 0-999 integer survives as evidence, nothing more.
    return { entry: entry, rejected: false, axis: null };
  }
  const value = rcaPosToAxisValue(pos, domain);
  if (value === null) return { entry: null, rejected: true, axis: null };
  const lo = Math.min(Number(domain.at_0), Number(domain.at_999));
  const hi = Math.max(Number(domain.at_0), Number(domain.at_999));
  const span = hi - lo;
  if (span <= 0) {
    // FIX-2026-09-22 (audit item 4): zero-span carries no information — the
    // raw integer stays, the point is rejected (=> low_confidence) and no
    // `value` (hence never `calibrated: true`) is written.
    return { entry: entry, rejected: true, axis: null };
  }
  const tol = RCA_GEOMETRY_TOLERANCE * span;
  if (!(lo - tol <= value && value <= hi + tol)) {
    return { entry: null, rejected: true, axis: null };
  }
  const semKeys = Object.prototype.hasOwnProperty.call(RCA_GEOMETRY_SEMANTIC_KEYS, field)
    ? RCA_GEOMETRY_SEMANTIC_KEYS[field] : [];
  for (const semKey of semKeys) {
    const sem = rcaToFloatOpt(srcRow ? srcRow[semKey] : null);
    if (sem === null || !(lo <= sem && sem <= hi)) continue;
    if (Math.abs(value - sem) > tol) {
      return { entry: null, rejected: true, axis: null };
    }
    break;
  }
  entry.value = (typeof rcaPyRound === 'function') ? rcaPyRound(value, 6) : value;
  if (domain.unit) entry.unit = domain.unit;
  return { entry: entry, rejected: false, axis: axis };
}

// Mirror of geometry_from_row -> {geometry, rejected, consumed}. A bad point
// is dropped, the good ones survive; nothing survives -> no geometry key.
function rcaGeometryFromRow(src, axes, row) {
  const srcRow = (row && typeof row === 'object' && !Array.isArray(row))
    ? row : ((src && typeof src === 'object' && !Array.isArray(src)) ? src : {});
  const points = {};
  const usedAxes = {};
  const consumed = [];
  let rejected = false;
  if (!src || typeof src !== 'object' || Array.isArray(src)) {
    return { geometry: null, rejected: false, consumed: consumed };
  }
  for (const field of Object.keys(src)) {
    if (!(field === RCA_POS_SCALE_KEY || field.endsWith(RCA_POS_SUFFIX))) continue;
    consumed.push(field);
    const r = rcaGeometryPoint(field, src[field], srcRow, axes);
    if (r.rejected) rejected = true;
    if (r.entry === null) continue;
    if (r.axis && r.entry.value !== undefined && !(r.axis in usedAxes)) {
      let domain = (axes && axes[r.axis]) || (axes && axes[field]) || null;
      if (domain) usedAxes[r.axis] = domain;
    }
    points[field] = r.entry;
  }
  if (!Object.keys(points).length) {
    return { geometry: null, rejected: rejected, consumed: consumed };
  }
  const usedAxisNames = Object.keys(usedAxes);
  const geometry = {
    version: RCA_GEOMETRY_VERSION,
    scale: RCA_GEOMETRY_SCALE,
    calibrated: usedAxisNames.length > 0,
    points: points,
  };
  if (usedAxisNames.length) {
    for (const name of usedAxisNames) geometry.axes = geometry.axes || {}, geometry.axes[name] = usedAxes[name];
  }
  return { geometry: geometry, rejected: rejected, consumed: consumed };
}

// Mirror of apply_coverage_contract. Depends on the reason-codes.js globals
// (rcaNormalizeResponseKind / rcaNormalizeReasonCodes / rcaRowHasValue /
// RCA_RESPONSE_*), loaded before any extraction runs (index.html script
// order; the node suites load the same set).
function rcaApplyCoverageContract(src, row, extraCodes) {
  if (!src || typeof src !== 'object' || Array.isArray(src)) return;
  if (!row || typeof row !== 'object' || Array.isArray(row)) return;
  const kind = rcaNormalizeResponseKind(src.response_kind);
  const codes = rcaNormalizeReasonCodes(rcaPyOr(src.reason_codes, src.reason_code));
  for (const code of (extraCodes || [])) {
    const slugList = rcaNormalizeReasonCodes([code]);
    const slug = slugList.length ? slugList[0] : null;
    if (slug && codes.indexOf(slug) === -1) codes.push(slug);
  }
  if (kind === RCA_RESPONSE_NOT_DRAWN && rcaRowHasValue(row)) {
    // "not drawn" plus a value = it WAS drawn. extracted wins; flag the lie.
    row.response_kind = RCA_RESPONSE_EXTRACTED;
    rcaAddRowFlag(row, 'response_kind_conflict');
  } else if (kind) {
    row.response_kind = kind;
  }
  if (codes.length) row.reason_codes = codes;
}

// Mirror of attach_row_contract: returns the consumed pos keys so the caller
// keeps them out of _extras.
function rcaAttachRowContract(src, row, axes) {
  const g = rcaGeometryFromRow(src, axes, row);
  if (g.geometry !== null) row.geometry = g.geometry;
  rcaApplyCoverageContract(src, row, g.rejected ? [RCA_LOW_CONFIDENCE_CODE] : []);
  return g.consumed;
}

// Mirror of axis_calibration_unusable (FIX-2026-09-22 audit item 7): a block
// exists but nothing survives _axis_domain, or every surviving axis is
// degenerate (at_0 == at_999 — the zero-span shape of audit item 4).
function rcaAxisCalibrationUnusable(parsed, axesIn) {
  if (!rcaAxisCalibrationBlocks(parsed).length) return false;
  const axes = (axesIn === null || axesIn === undefined) ? rcaAxisDomainsFrom(parsed) : axesIn;
  const names = Object.keys(axes);
  if (!names.length) return true;
  return names.every((k) => Number(axes[k].at_0) === Number(axes[k].at_999));
}

// Mirror of _fold_axis_calibration: usable -> hoist; unusable -> no hoist, an
// `axis_calibration_unusable` root warning and the raw block kept under the
// extras source so the operator still sees what the model claimed.
function rcaFoldAxisCalibration(parsed, axes, out, extrasSrc, warnings) {
  if (rcaAxisCalibrationUnusable(parsed, axes)) {
    rcaPushWarning(warnings, 'axis_calibration_unusable');
    const raw = (parsed && typeof parsed === 'object' && !Array.isArray(parsed))
      ? parsed.axis_calibration : null;
    if (raw && typeof raw === 'object' && !Array.isArray(raw) && Object.keys(raw).length) {
      extrasSrc.axis_calibration = raw;
    }
    return;
  }
  if (axes && Object.keys(axes).length) out.axis_calibration = axes;
}


// Python's bare `str()` — used where the oracle calls str() directly instead of
// _stringify_scalar (phylogenetic `root_ids` / node id normalisation). The two
// spellings differ on containers (repr vs "") and on booleans ("True" vs
// "true"), so the call sites are kept separate on purpose.
function rcaPyStr(value) {
  if (value === null || value === undefined) return 'None';
  if (typeof value === 'boolean') return value ? 'True' : 'False';
  if (typeof value === 'number') return String(value);
  if (typeof value === 'string') return value;
  if (Array.isArray(value) || typeof value === 'object') return JSON.stringify(value);
  return String(value);
}

// Mirror of normalize_columnar_result's local `fi()` — the bed-index coercion
// with the LOSSY path reported, `{"value": int|null, "lossy": bool}`.
//
// The three-way contract matters: `(null, false)` for unparseable / empty /
// bool input, `(v, false)` for a clean integer, `(trunc(v), true)` whenever the
// value had to go through `float()` ("8.5" -> 8, 8.5 -> 8). `parseInt("8.5")`
// silently returned 8 in the old JS mirror, so the row-level
// `range_top_idx_truncated` / `bed_idx_truncated` flags the Python side emits
// (and the UI badge reads) never appeared in the browser.
function rcaBedIndex(value) {
  if (value === null || value === undefined || value === '') return { value: null, lossy: false };
  if (typeof value === 'boolean') return { value: null, lossy: false };
  if (typeof value === 'number') {
    if (!Number.isFinite(value)) return { value: null, lossy: false };
    if (Number.isInteger(value)) return { value, lossy: false };
    // int(float) TRUNCATES toward zero, exactly like Python's int(v).
    return { value: Math.trunc(value), lossy: true };
  }
  if (typeof value !== 'string') return { value: null, lossy: false };
  const n = rcaPyFloatOrNull(value);
  if (n === null || Number.isNaN(n) || !Number.isFinite(n)) return { value: null, lossy: false };
  if (Number.isInteger(n)) {
    // Python tries `int(v)` first, which succeeds for "8" but raises for
    // "8.0" — the latter falls through to the float path and is lossy.
    const direct = /^[+-]?\d+$/.test(value.trim());
    return { value: n, lossy: !direct };
  }
  return { value: Math.trunc(n), lossy: true };
}

// Python's `str(float)` keeps a trailing `.0` on integral values and prints
// exponents as `1e-07`; `String(1.0)` gives "1". Only reachable through the
// Newick serializer (support / branch_length), where the two spellings produce
// two different tree files from the same payload.
function rcaPyFloatStr(value) {
  if (typeof value !== 'number' || !Number.isFinite(value)) return String(value);
  if (Number.isInteger(value) && Math.abs(value) < 1e21) return value.toFixed(1);
  return String(value);
}

// Mirror of _quote_newick_label: Newick tokens `( ) [ ] ; ,` (and a `:`, which
// would otherwise split `name:branch_length`) force single quotes, embedded
// single quotes are doubled per the Newick spec, and a label with surrounding
// or embedded whitespace is quoted too. The old JS mirror substituted `_` for
// `(`, `)` and `:`, which destroyed the taxon name ("(A)" -> "_A_").
function rcaQuoteNewickLabel(label) {
  const text = label === null || label === undefined ? '' : String(label);
  if (!text) return "''";
  const needsQuote = /[(),[\];:]/.test(text);
  if (!needsQuote && text.trim() === text && text.indexOf(' ') === -1) return text;
  return "'" + text.replace(/'/g, "''") + "'";
}

// Mirror of _extracted_any: did the normalizer salvage anything at all?
function rcaExtractedAny(out) {
  if (!out || typeof out !== 'object') return false;
  for (const k of Object.keys(out)) {
    const value = out[k];
    if (Array.isArray(value) || (value && typeof value === 'object')) {
      if (Object.keys(value).length > 0) return true;
    } else if (value !== null && value !== undefined && value !== ''
               && value !== 0 && value !== false) {
      return true;
    }
  }
  return false;
}

// Mirror of _stringify_scalar's caller in _other_fossils_from: lift the first
// readable label out of a dict-shaped fossil entry, else keep a scalar's text,
// else drop the record (a dict with real content but no label is noise the
// table cannot render).
function rcaOtherFossilLabel(item) {
  if (item === null || item === undefined) return '';
  if (typeof item === 'string') return item.trim();
  if (Array.isArray(item)) return '';
  if (typeof item === 'object') {
    for (const key of ['label', 'species', 'taxon', 'name', 'fossil', 'text']) {
      const value = item[key];
      if (typeof value === 'string' && value.trim()) return value.trim();
    }
    const extras = Object.keys(item).filter(
      (k) => ['label', 'species', 'taxon', 'name'].indexOf(k) === -1);
    return extras.length ? '' : rcaStringifyScalar(item);
  }
  return rcaStringifyScalar(item);
}

function rcaOtherFossilsFrom(raw) {
  const out = [];
  const push = (text) => { if (text) out.push(text); };
  if (typeof raw === 'string') {
    const t = raw.trim();
    return t ? [t] : [];
  }
  if (raw && typeof raw === 'object') {
    const values = Array.isArray(raw) ? raw : Object.values(raw);
    for (const item of values) push(rcaOtherFossilLabel(item));
    return out;
  }
  return out;
}

// Mirror of _merge_other_fossils (REVIEW-2026-09-20 #1): the `_array_root`
// unwrap may already have salvaged bare-string labels into
// out.other_fossils; a plain assignment dropped them as soon as the payload
// also carried an `other_fossils` field. Merge instead — order preserved,
// exact duplicates skipped.
function rcaMergeOtherFossils(existing, raw) {
  const already = Array.isArray(existing) ? existing.filter((x) => typeof x === 'string') : [];
  const merged = [];
  const seen = new Set();
  for (const text of already.concat(rcaOtherFossilsFrom(raw))) {
    const t = (text || '').trim();
    if (t && !seen.has(t)) {
      seen.add(t);
      merged.push(t);
    }
  }
  return merged;
}

// Mirror of _row_from_string: a bare-string list entry becomes one row whose
// primary identifier is the string. Deliberately NOT for `abundances` (a taxon
// row needs a level/abundance to mean anything; tests_core pins the drop).
function rcaRowFromString(item, kind) {
  const text = String(item).trim();
  if (!text) return null;
  if (['sections', 'biozones', 'sites', 'zones', 'zonations'].indexOf(kind) !== -1) {
    return { name: text };
  }
  if (kind === 'species_ranges') return { species: text };
  return null;
}

// Mirror of _PRIMARY_ID_KEYS — the identifier a dict-shaped array's wrapper key
// fills in when the record does not carry one.
const RCA_PRIMARY_ID_KEYS = {
  abundances: 'taxon',
  sections: 'name',
  biozones: 'name',
  species_ranges: 'species',
  sites: 'name',
  zones: 'name',
  zonations: 'name',
  correlations: 'from_zone',
  nodes: 'id',
  data_points: 'sample_id',
};

// Mirror of _dict_rows: dict-shaped named arrays recover their values; bare
// strings are skipped (the caller decides whether the key has a natural
// single-field row shape). Returns an array rather than a generator — the
// Python generator exists only to avoid an intermediate list.
function rcaDictRows(raw) {
  const out = [];
  if (raw && typeof raw === 'object') {
    if (Array.isArray(raw)) {
      for (const item of raw) {
        if (item && typeof item === 'object' && !Array.isArray(item)) out.push(item);
      }
    } else {
      for (const value of Object.values(raw)) {
        if (value && typeof value === 'object' && !Array.isArray(value)) out.push(value);
      }
    }
  }
  return out;
}

// Mirror of _iter_rows: like _dict_rows but a LIST entry that is a bare string
// is coerced to a row and the repair is flagged; a DICT-shaped array is always
// flagged (`dict_shaped_array`, REVIEW-2026-09-20 #5: the shape change used to
// go unreported whenever every record carried its own id), and the wrapper key
// fills a MISSING primary identifier only (a present-but-empty value is the
// model honestly saying "unreadable" and must not be overwritten).
function rcaIterRows(raw, kind, warnings) {
  const flag = (tag) => { if (warnings) rcaPushWarning(warnings, tag); };
  const out = [];
  if (raw && typeof raw === 'object' && !Array.isArray(raw)) {
    flag('dict_shaped_array');
    for (const [wrapperKey, inner] of Object.entries(raw)) {
      if (!inner || typeof inner !== 'object' || Array.isArray(inner)) continue;
      const row = { ...inner };
      const idKey = RCA_PRIMARY_ID_KEYS[kind];
      if (idKey && !(idKey in row)) row[idKey] = String(wrapperKey);
      out.push(row);
    }
  } else if (Array.isArray(raw)) {
    for (const item of raw) {
      if (item && typeof item === 'object' && !Array.isArray(item)) { out.push(item); continue; }
      if (typeof item === 'string') {
        const row = rcaRowFromString(item, kind);
        if (row !== null) { out.push(row); flag('string_row_coerced'); }
      }
    }
  }
  return out;
}


// ---------------------------------------------------------------------------
// Per-mode documented root keys — verbatim mirrors of the _KNOWN_*_ROOT_KEYS
// tuples declared next to each Python normalizer, as resolved by
// rca_core/extractor.py:_mode_root_keys. When a parsed payload has none of
// these and no _array_root, the model returned a structurally foreign object
// (truncated mid-stream, schema swap, hallucinated shape) and the result has
// to be flagged rather than served as a confident-looking empty table.
//
// REVIEW-2026-09-20 #13: `_extras` used to be listed for columnar_section and
// abundance_diagram. It is an artifact the normalizers ATTACH to their output
// and is never a root key of a raw model payload on either engine, so those two
// entries made the browser accept `_extras`-only payloads that Python flags.
//
// REVIEW-2026-09-20 #7: chemical_stratigraphy / paleomap / scatter_plot added.
// The browser has no normalizer for those three (they are backend-only), but
// the shared unusable-payload guard resolves root keys BY MODE, and an empty
// list means "never flag" (see rcaRootUnrelated) — so a request that arrives
// with mode="paleomap" on the direct path fell through to the range-chart
// normalizer and came back ok=true with an empty table. The lists below are
// verbatim so the guard is honest for a mode the browser cannot serve.
const RCA_MODE_ROOT_KEYS = {
  // FIX-2026-09-22 (A2): the four contract modes carry the optional root
  // `axis_calibration` in their documented root-key set (mirror of Python
  // _mode_root_keys -> _KNOWN_*_ROOT_KEYS, which gained it under
  // BORROW-2026-09-20 (B)), so a calibration-only payload is not mistaken
  // for an unrelated root by rcaRootUnrelated.
  range_chart:        ['sections', 'species_ranges', 'biozones', 'other_fossils', 'confidence',
                       'axis_calibration'],
  columnar_section:   ['sections', 'fossil_legend', 'lithology_legend', 'cross_beds',
                       'overall_confidence', 'confidence'],
  abundance_diagram:  ['sites', 'abundances', 'zones', 'confidence', 'axis_calibration'],
  phylogenetic_tree:  ['metadata', 'nodes', 'root_ids', 'legend', 'confidence'],
  chemical_stratigraphy: ['metadata', 'data_points', 'events', 'intervals', 'confidence',
                          'axis_calibration'],
  paleomap:           ['metadata', 'continents', 'oceans_seas', 'tectonic_features',
                       'biogeographic_realms', 'fossil_sites', 'paleolatitude_indicators',
                       'confidence'],
  scatter_plot:       ['metadata', 'groups', 'points', 'outliers', 'statistics', 'confidence',
                       'axis_calibration'],
  zonation_chart:     ['zonations', 'zones', 'correlations', 'confidence'],
};

// Mirror of the "truncated rescue" guard inside normalize_result (and the
// same shape each other mode's normalizer builds): returns the flag list to
// attach, or null. Python's rule is `parsed and not ROOTS & parsed.keys() and
// "_array_root" not in parsed` — an EMPTY payload is not foreign, it is an
// honest empty result, so it is not flagged here.
function rcaTruncatedWarningIfForeign(parsed, mode) {
  // normalize_result's non-dict guard: Python returns
  // {"sections": [], ..., "_warnings": ["normalize_non_dict_input"]} rather
  // than the truncation flag, so the operator sees the real cause.
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    return ['normalize_non_dict_input'];
  }
  const roots0 = RCA_MODE_ROOT_KEYS[mode];
  if (!roots0 || !Object.keys(parsed).length) return null;
  // FIX-2026-09-22 (A2): normalize_result's inline foreign check mirrors
  // Python's hardcoded RANGE_CHART_ROOTS (5 keys, WITHOUT axis_calibration),
  // which is deliberately narrower than _KNOWN_RANGE_CHART_KEYS used by the
  // _ok_result root-unrelated guard below (rcaRootUnrelated). A payload that
  // carries only a calibration block still answers nothing.
  const roots = (mode === 'range_chart')
    ? roots0.filter((k) => k !== 'axis_calibration') : roots0;
  const keys = Object.keys(parsed);
  const matched = keys.some((k) => roots.indexOf(k) !== -1);
  if (matched) return null;
  if ('_array_root' in parsed) return null;
  return ['truncated_or_unrecognized_payload'];
}

// Mirror of rca_core/extractor._payload_mismatch: the non-empty reason a
// normalized payload is unusable, "" when it is not. Deliberately narrow
// (REVIEW-2026-09-20 #7): an empty result WITH an explanatory note is an
// honest degradation the prompt asks for ("do not invent data") and stays
// ok=True; so is a truncated response that still produced rows.
function rcaPayloadMismatch(data, resultTruncated) {
  if (!data || typeof data !== 'object' || Array.isArray(data)) return '';
  if (rcaExtractedAny(data)) return '';
  const warnings = Array.isArray(data._warnings) ? data._warnings : [];
  if (warnings.indexOf('truncated_or_unrecognized_payload') !== -1) {
    return 'rescued inner object: unusable';
  }
  if (resultTruncated || warnings.indexOf('truncated') !== -1 || data.truncated) {
    return 'truncated output rescued no usable records';
  }
  return '';
}

// Mirror of the `root_unrelated` clause of _ok_result: the payload parsed, but
// none of its root keys belong to this mode's contract and it produced nothing.
function rcaRootUnrelated(parsed, data, mode) {
  const known = RCA_MODE_ROOT_KEYS[mode] || [];
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return false;
  if (!Object.keys(parsed).length || !known.length) return false;
  const keys = Object.keys(parsed);
  if (keys.some((k) => known.indexOf(k) !== -1)) return false;
  if (keys.indexOf('_array_root') !== -1) return false;
  return !rcaExtractedAny(data);
}

// The prose every Python `extract_*` mode keeps in a local `warning` and then
// puts into `ExtractResult.warning` (extractor.py:1359-1360, and `_ok_result`
// 856-857 / 881 / 886). Two shapes use it:
//   * success while truncated -> the prose verbatim; not truncated -> "";
//   * the unusable-payload error -> `prose + " | " + reason`.
// REVIEW-2026-09-20 #7: the browser used to report the bare tag
// `truncated_or_unrecognized_payload` in `warning` instead, so the direct
// transport and the backend transport (`rcaCallBackend`, which forwards the
// server's `warning` verbatim) surfaced two different strings for one run.
// The tag still travels — inside `data._warnings`, exactly like Python.
const RCA_TRUNCATION_WARNING = 'Result may be truncated (model hit max_tokens). '
  + 'Try raising the max_tokens setting and re-running.';

// Mirror of the shared `_ok_result` contract: given the normalized data and the
// raw parse, return the reason the run must be reported as an error, or "" for
// a success. Every mode goes through this, not only range_chart (the guard used
// to exist on the range-chart path alone, which is why seven modes could serve
// an unrelated or truncated object as a confident empty table).
function rcaUnusablePayloadReason(parsed, data, mode, truncated) {
  let reason = rcaPayloadMismatch(data, !!truncated);
  const unrelated = rcaRootUnrelated(parsed, data, mode);
  if (!reason && unrelated) {
    reason = 'payload root keys match no field of this chart type';
  }
  if (reason && unrelated) {
    if (!Array.isArray(data._warnings)) data._warnings = [];
    rcaPushWarning(data._warnings, 'truncated_or_unrecognized_payload');
  }
  return reason;
}

// P0-5 (REVIEW-2026-07-25): the range-chart `_array_root` unwrap. Bare
// non-empty strings go to `other_fossils`, classifiable dicts are routed
// through the SAME row normalizers as the named arrays (Python 1136-1148:
// pushing the raw dict left string-where-list-was-expected fields such as
// `formations` uncoerced), and unclassifiable dicts survive under
// `_unclassified` instead of vanishing.
function rcaArrayRootIntoRangeChart(parsed, out, rootWarnings, axes) {
  const items = parsed && parsed._array_root;
  if (!Array.isArray(items)) return;
  for (const item of items) {
    if (!item || typeof item !== 'object' || Array.isArray(item)) {
      // Non-dict items (e.g. bare strings) go to other_fossils.
      if (typeof item === 'string' && item.trim()) {
        out.other_fossils.push(item.trim());
      }
      continue;
    }
    const key = rcaClassifyArrayItem(item);
    if (key === 'sections') rcaNormalizeSectionInto(item, out.sections, rootWarnings);
    else if (key === 'species_ranges') rcaNormalizeSpeciesInto(item, out.species_ranges, rootWarnings, axes);
    else if (key === 'biozones') rcaNormalizeBiozoneInto(item, out.biozones, rootWarnings);
    else if (key) (out[key] = out[key] || []).push(item);
    else (out._unclassified = out._unclassified || []).push(item);
  }
}

// Mirror of Python _IRON_RULE_ZONE_RE (extractor.py:421) — the prompt's iron
// rule markers: a name reading like one of these belongs in `biozones`, never
// in `species_ranges`. One shared regex, used by _classify_array_item, the
// species row builder and the post-normalize flagging pass.
const _RCA_IRON_RULE_ZONE_RE = /\b(zone|zonule|assemblage|oppel|interval|lineage|range|acme)\b/i;

// Python `(item.get("name") or "").strip()` — the `or` matters: a 0 / [] / {} /
// false value reads as "" on the server, while `String(item.name || "")` in JS
// would yield "0" / "" / "[object Object]".
function rcaNameStr(item, key) {
  return rcaStringifyScalar(rcaPyOr(item ? item[key] : null, '')).trim();
}

// Mirror of rca_core/extractor._classify_array_item.
// P0-4: explicit zone_type wins over all heuristics; the remaining ladder uses
// KEY PRESENCE (Python `"species" in item`), not truthiness, so `{species: ""}`
// still classifies as a species row.
// REVIEW-2026-09-20 #12: the biozone probe read `item.name || item.label`, so a
// `{label: "X Zone", age: ...}` item became a biozone server-side and a section
// browser-side; Python only ever looks at `name`.
function rcaClassifyArrayItem(item) {
  if (!item || typeof item !== 'object' || Array.isArray(item)) return null;
  const zt = item.zone_type;
  if (typeof zt === 'string') {
    const ztl = zt.trim().toLowerCase();
    if (['biozone', 'zone', 'assemblage_zone', 'interval_zone', 'lineage_zone',
         'acme_zone', 'oppel_zone', 'range_zone', 'subzone', 'zonule'].includes(ztl)) {
      return 'biozones';
    }
    if (['species_range', 'taxon_range', 'fad_lad'].includes(ztl)) {
      return 'species_ranges';
    }
    if (['section', 'measured_section', 'locality'].includes(ztl)) {
      return 'sections';
    }
  }
  if ('species' in item || ('range_top' in item && 'range_base' in item)) {
    return 'species_ranges';
  }
  const nameStr = rcaNameStr(item, 'name');
  if ('name' in item && 'age' in item && _RCA_IRON_RULE_ZONE_RE.test(nameStr)) {
    return 'biozones';
  }
  if ('name' in item && ('age_range' in item || 'formations' in item || 'age' in item)) {
    return 'sections';
  }
  if ('name' in item) return 'sections';
  return null;
}

const _RCA_KNOWN_SECTION_KEYS = ['name', 'age_range', 'formations',
                                 'formation_thickness_m', 'coordinates'];
// Verbatim mirror of _KNOWN_SPECIES_KEYS. The bed/idx/endpoint/occurrence/
// confidence/note fields are deliberately ABSENT: they are first-class row keys
// written explicitly below, and rcaCarryExtras already skips any key the row
// itself carries ("k not in out"), so listing them here would be a second
// source of truth that drifted (it is exactly how the old 17-field JS list lost
// the `reworked` semantics).
const _RCA_KNOWN_SPECIES_KEYS = ['species', 'section', 'range_top', 'range_base',
                                 'biozone', 'author', 'year', 'author_year', 'reworked'];
const _RCA_KNOWN_BIOZONE_KEYS = ['name', 'section', 'age', 'thickness_m', 'zone_type'];
// Verbatim mirror of _KNOWN_RANGE_CHART_KEYS — the root keys whose leftovers
// become the result's `_extras`.
// FIX-2026-09-22 (A2): `axis_calibration` joins the whitelist (mirror of the
// BORROW-2026-09-20 (B) Python key): it is a hoisted root key, not an
// _extras leftover — the fold below decides hoist vs warn-and-keep-raw.
const _RCA_KNOWN_RANGE_CHART_KEYS = ['sections', 'species_ranges', 'biozones',
                                     'other_fossils', 'confidence',
                                     'axis_calibration'];
const _RCA_VALID_OCCURRENCE_MODES = ['unknown', 'in_situ', 'reworked', 'transported',
                                     'cavity_fill', 'bioturbated', 'derived', 'lag_deposit'];
const _RCA_VALID_ENDPOINT_KINDS = ['unknown', 'observed', 'projected', 'truncated'];

function rcaNormalizeOccurrenceMode(sp) {
  const raw = sp ? sp.occurrence_mode : null;
  if (typeof raw === 'string') {
    const normalized = raw.trim().toLowerCase();
    if (_RCA_VALID_OCCURRENCE_MODES.indexOf(normalized) !== -1) return normalized;
  }
  const reworked = sp ? sp.reworked : null;
  if (typeof reworked === 'boolean') return reworked ? 'reworked' : 'in_situ';
  return 'unknown';
}

function rcaNormalizeEndpointKind(value) {
  if (typeof value === 'string') {
    const normalized = value.trim().toLowerCase();
    if (_RCA_VALID_ENDPOINT_KINDS.indexOf(normalized) !== -1) return normalized;
  }
  return 'unknown';
}

// Mirror of _normalize_section_into: every array-root / named / dict-shaped
// section row goes through this one builder, so string-where-list-was-expected
// fields (`formations`) and missing keys get a uniform shape.
function rcaNormalizeSectionInto(sec, target, warnings) {
  let formationsOut = [];
  const formations = sec ? sec.formations : null;
  if (Array.isArray(formations)) {
    formationsOut = formations
      .filter((x) => typeof x === 'string')
      .map((x) => x.trim())
      .filter((x) => x.length > 0);
  } else if (typeof formations === 'string' && formations.trim()) {
    formationsOut = [formations.trim()];
  }
  const row = {
    name: rcaStringifyScalar(sec ? sec.name : null),
    age_range: rcaStringifyScalar(sec ? sec.age_range : null),
    formations: formationsOut,
    formation_thickness_m: rcaStringifyScalar(sec ? sec.formation_thickness_m : null),
    coordinates: rcaStringifyScalar(sec ? sec.coordinates : null),
  };
  rcaCarryExtras(sec, _RCA_KNOWN_SECTION_KEYS, row);
  target.push(row);
  return row;
}

// Mirror of _normalize_species_into.
// REVIEW-2026-09-20 #11: the bed indices now get the same order repair the
// columnar block rows already had. An inverted (range_top_idx < range_base_idx)
// pair used to be exported as a valid range and nothing downstream caught it —
// quality.py's FAD/LAD check reads the STRING fields, not the index fields — so
// the pair is swapped and the row carries `index_order_swap`.
// FIX-2026-09-22 (A2): takes the figure-level `axes` and runs the row through
// rcaAttachRowContract (mirror of Python attach_row_contract), so
// response_kind / reason_codes / *pos_0_999 land as FIRST-CLASS row fields
// (geometry sidecar + coverage contract) instead of sinking into `_extras`,
// where the merge priority-vote and the coverage ledger could never see them.
function rcaNormalizeSpeciesInto(sp, target, warnings, axes) {
  let topIdx = rcaOptionalInt(sp ? sp.range_top_idx : null);
  let baseIdx = rcaOptionalInt(sp ? sp.range_base_idx : null);
  const idxWarnings = [];
  if (topIdx !== null && baseIdx !== null && topIdx < baseIdx) {
    const t = topIdx; topIdx = baseIdx; baseIdx = t;
    idxWarnings.push('index_order_swap');
  }
  const row = {
    species: rcaStringifyScalar(sp ? sp.species : null),
    section: rcaStringifyScalar(sp ? sp.section : null),
    range_top: rcaStringifyScalar(sp ? sp.range_top : null),
    range_base: rcaStringifyScalar(sp ? sp.range_base : null),
    biozone: rcaStringifyScalar(sp ? sp.biozone : null),
    author: rcaStringifyScalar(sp ? sp.author : ''),
    year: rcaStringifyScalar(sp ? sp.year : ''),
    author_year: rcaStringifyScalar(rcaPyOr(sp ? sp.author_year : null, '')),
    range_top_bed: rcaStringifyScalar(sp ? sp.range_top_bed : ''),
    range_base_bed: rcaStringifyScalar(sp ? sp.range_base_bed : ''),
    range_top_idx: topIdx,
    range_base_idx: baseIdx,
    endpoint_kind: rcaNormalizeEndpointKind(sp ? sp.endpoint_kind : null),
    occurrence_mode: rcaNormalizeOccurrenceMode(sp),
    confidence: rcaOptionalConfidence(sp ? sp.confidence : null),
    note: rcaStringifyScalar(sp ? sp.note : ''),
  };
  // FIX-2026-09-22 (A2): geometry + coverage contract, additively — mirror of
  // `consumed = attach_row_contract(sp, row, axes=axes)`; the consumed pos
  // keys plus the contract source keys join the known list so _carry_extras
  // never duplicates them under _extras (Python: _KNOWN_SPECIES_KEYS +
  // _CONTRACT_SOURCE_KEYS + consumed).
  const consumed = rcaAttachRowContract(sp, row, axes);
  rcaCarryExtras(sp, _RCA_KNOWN_SPECIES_KEYS.concat(RCA_CONTRACT_SOURCE_KEYS, consumed), row);
  // P0-4: defensive — if the species name reads like a zone, flag & annotate.
  const spName = row.species;
  if (spName && _RCA_IRON_RULE_ZONE_RE.test(spName)) {
    row.note = (row.note + ' [zone-mislabel-warning]').trim();
  }
  // Same single-string / list convention as the columnar block rows, so
  // rcaWarningFlags reads every `_warning` the same way.
  // FIX-2026-09-22 (audit item 2 family, mirror of extractor.py): ADDITIVE —
  // rcaAttachRowContract above may already have flagged this very row
  // (response_kind_conflict) and a plain assignment destroyed it.
  for (const w of idxWarnings) rcaAddRowFlag(row, w);
  target.push(row);
  return row;
}

// Mirror of _normalize_biozone_into.
// REVIEW-2026-09-20 #10: two divergences fixed at once.
//   * Ladder ORDER: Python tests `interval` BEFORE the `\bzonule\b` /
//     `\bsubzone\b` regexes and `oppel` after them. The JS order put
//     zonule/subzone first, so "Interval zonule" inferred `zonule` in the
//     browser and `interval_zone` on the server.
//   * The `or` on zone_type is PYTHON's `or`: `bz.get("zone_type") or
//     inferred_zt` falls through for 0 / false / "" / [] / {} — while
//     `asStr(bz.zone_type) || inferredZt` stringified FIRST, so an explicit
//     numeric 0 became the string "0" (truthy in JS) and was kept as the zone
//     type. rcaPyOr evaluates the raw value, then the result is stringified.
function rcaNormalizeBiozoneInto(bz, target, warnings) {
  const name = rcaStringifyScalar(bz ? bz.name : null).trim();
  const nl = name.toLowerCase();
  let inferredZt = 'biozone';
  if (nl.includes('assemblage') || nl.includes('ass.')) inferredZt = 'assemblage_zone';
  else if (nl.includes('acme')) inferredZt = 'acme_zone';
  else if (nl.includes('lineage')) inferredZt = 'lineage_zone';
  else if (nl.includes('interval')) inferredZt = 'interval_zone';
  else if (/\bzonule\b/.test(nl)) inferredZt = 'zonule';
  else if (/\bsubzone\b/.test(nl)) inferredZt = 'subzone';
  else if (nl.includes('oppel')) inferredZt = 'oppel_zone';
  else if (nl.includes('range zone') || nl.includes('taxon-range')) inferredZt = 'range_zone';
  const row = {
    name: name,
    // PARITY: `section` is a top-level field on the biozone row so the
    // aggregation layer can fold it into the row label; demoting it to
    // _extras hides it from rcaAggNorm and collapses two same-named biozones
    // from different sections into one merged row.
    section: rcaStringifyScalar(bz ? bz.section : ''),
    age: rcaStringifyScalar(bz ? bz.age : ''),
    thickness_m: rcaStringifyScalar(bz ? bz.thickness_m : ''),
    zone_type: rcaStringifyScalar(rcaPyOr(bz ? bz.zone_type : null, inferredZt)),
  };
  rcaCarryExtras(bz, _RCA_KNOWN_BIOZONE_KEYS, row);
  target.push(row);
  return row;
}

// Mirror of normalize_result's inner _coerce_list_or_dict: named-array recovery
// for the three range-chart tables.
//   * dict-shaped ({"Pingdingshan": {...}}): iterate the VALUES, inject the
//     wrapper key as the primary identifier when that identifier is ABSENT
//     (never overwrite a present-but-empty value: that is the model saying
//     "unreadable"), drop any pre-existing `_extras` so the normalizer rebuilds
//     it cleanly, then stamp `wrapper_key` on the row it appended.
//   * list-shaped: dicts pass through; a bare string coerces to a one-field row
//     and the repair is flagged `string_row_coerced` (both engines used to
//     discard those entries in silence).
function rcaCoerceRangeChartList(raw, kind, out, rootWarnings, axes) {
  const append = (row) => {
    if (kind === 'sections') rcaNormalizeSectionInto(row, out.sections, rootWarnings);
    else if (kind === 'species_ranges') rcaNormalizeSpeciesInto(row, out.species_ranges, rootWarnings, axes);
    else if (kind === 'biozones') rcaNormalizeBiozoneInto(row, out.biozones, rootWarnings);
  };
  if (raw && typeof raw === 'object' && !Array.isArray(raw)) {
    for (const [wrapperKey, inner] of Object.entries(raw)) {
      if (!inner || typeof inner !== 'object' || Array.isArray(inner)) continue;
      const inner2 = { ...inner };
      if ((kind === 'sections' || kind === 'biozones') && !('name' in inner2)) {
        inner2.name = wrapperKey;
      } else if (kind === 'species_ranges' && !('species' in inner2)) {
        inner2.species = wrapperKey;
      }
      delete inner2._extras;
      const before = out[kind].length;
      append(inner2);
      if (out[kind].length > before) {
        const row = out[kind][out[kind].length - 1];
        if (!row._extras || typeof row._extras !== 'object' || Array.isArray(row._extras)) {
          row._extras = {};
        }
        row._extras.wrapper_key = wrapperKey;
      }
    }
  } else if (Array.isArray(raw)) {
    for (const item of raw) {
      if (item && typeof item === 'object' && !Array.isArray(item)) { append(item); continue; }
      if (typeof item === 'string') {
        const row = rcaRowFromString(item, kind);
        if (row !== null) {
          append(row);
          rcaPushWarning(rootWarnings, 'string_row_coerced');
        }
      }
    }
  }
}

// Mirror of rca_core/extractor.normalize_result.
function rcaNormalizeResult(parsed) {
  // MEDIUM fix: a non-dict payload must not crash the normalizer (Python
  // returns the empty shape with `normalize_non_dict_input`).
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    return {
      sections: [], species_ranges: [], biozones: [], other_fossils: [],
      confidence: 0.0,
      _warnings: ['normalize_non_dict_input'],
    };
  }
  const out = {
    sections: [],
    species_ranges: [],
    biozones: [],
    other_fossils: [],
    confidence: 0.0,
  };
  const rootWarnings = [];

  // FIX-2026-09-22 (A2): BORROW-2026-09-20 (B) mirror — one figure-level axis
  // calibration per result; the rows' *pos_0_999 reads convert against it,
  // absent it they stay raw evidence. Read once so every row builder shares
  // the same domains.
  const axes = rcaAxisDomainsFrom(parsed);

  // MEDIUM fix (truncated rescue): a rescued partial / inner object that matches
  // none of the documented range-chart root keys is flagged so the operator is
  // not silently handed an empty ok=True result.
  const foreign = rcaTruncatedWarningIfForeign(parsed, 'range_chart');
  if (foreign) foreign.forEach((tag) => rcaPushWarning(rootWarnings, tag));

  // H3-fix: unwrap _array_root and distribute items to the known keys — by
  // APPENDING to the same buckets the named arrays feed, never by replacing
  // them (REVIEW-2026-09-20 #12: the old pre-pass dropped a payload's real
  // `sections` list because the wrapper keys shadowed it).
  rcaArrayRootIntoRangeChart(parsed, out, rootWarnings, axes);

  rcaCoerceRangeChartList(parsed.sections, 'sections', out, rootWarnings, axes);
  rcaCoerceRangeChartList(parsed.species_ranges, 'species_ranges', out, rootWarnings, axes);
  rcaCoerceRangeChartList(parsed.biozones, 'biozones', out, rootWarnings, axes);
  if (Array.isArray(parsed._unclassified)) {
    (out._unclassified = out._unclassified || []).push(...parsed._unclassified);
  }

  // MEDIUM fix (iron rule): post-normalize pass flagging any species whose name
  // reads like a zone label. The row is NOT moved (that would be silently
  // destructive) — the operator sees the slip-through and decides.
  for (const sp of out.species_ranges) {
    const name = rcaNameStr(sp, 'species');
    if (name && _RCA_IRON_RULE_ZONE_RE.test(name)) {
      // FIX-2026-09-22 (audit item 2 family, mirror of extractor.py):
      // additive — the row can already carry index_order_swap /
      // response_kind_conflict and a plain assignment wiped both.
      rcaAddRowFlag(sp, 'iron_rule_zone_label');
      rcaPushWarning(rootWarnings, 'iron_rule_zone_label');
    }
  }

  // Fix B-3 / REVIEW-2026-09-20 #1: `other_fossils` may be a string, a dict or
  // a list, and entries may be dicts carrying label/species/taxon — and the
  // merge must not drop what the array unwrap already salvaged.
  out.other_fossils = rcaMergeOtherFossils(out.other_fossils, rcaPyOr(parsed.other_fossils, []));
  // Python: float(parsed.get("confidence", 0.0)) — an ABSENT key defaults to
  // 0.0, an explicit null/"" raises and is caught to 0.0.
  out.confidence = rcaConfidenceClamped('confidence' in parsed ? parsed.confidence : 0.0);

  // LOW fix: `_array_root` / `_note` are diagnostics, not data.
  // FIX-2026-09-22 (A2): hoist a usable axis_calibration to a root key; an
  // unusable one warns (`axis_calibration_unusable`) and its raw block stays
  // under `_extras` (mirror of _fold_axis_calibration, audit item 7).
  const extras = rcaTopExtras(parsed, _RCA_KNOWN_RANGE_CHART_KEYS, false) || {};
  rcaFoldAxisCalibration(parsed, axes, out, extras, rootWarnings);
  if (Object.keys(extras).length) out._extras = extras;
  if (rootWarnings.length) out._warnings = rootWarnings;
  return out;
}


function rcaNormalizeResultOldDupA() {
  // M1 (REVIEW-2026-08-19): other_fossils may be a string OR a dict
  // shape (label/species/taxon). The previous asStr map silently turned
  // dicts into '[object Object]'. Lift the first available label so
  // researchers see the actual fossil name in the export.
  const conf = 0;
  return conf;
}



// Normalize the parsed columnar-section JSON into the strict result shape.
// Mirror of rca_core.extractor.normalize_columnar_result.
//
// REVIEW-2026-09-20 #5/#3 (parity pass): this was the one normalizer the
// previous round never migrated off the pre-_dict_rows shape, so four Python
// behaviours were missing in the browser:
//   * `_array_root` bucketing used JS truthiness on a different key set
//     (`item.id || item.group || item.samples ...`), so the same bare-array
//     reply landed in `sections` in the browser and in `_unclassified` on the
//     server. Python tests KEY PRESENCE for lithology_blocks/age_units/id,
//     then meaning+(marker|pattern), then from_section|from_bed_idx.
//   * every sub-list went through `items.filter(...)`, which THROWS on the
//     dict-shaped emission ({"lithology_blocks": {"b1": {...}}}) that
//     `_dict_rows` exists to repair — normalize failed, the whole run came
//     back err.extract, and the same image worked server-side.
//   * `fi()`'s lossy path ("8.5" -> 8, 9.7 -> 9) was a silent `parseInt`, so
//     the row-level `range_top_idx_truncated` / `range_base_idx_unparseable` /
//     `bed_idx_truncated` flags the Python side emits (and the UI badge reads)
//     never appeared browser-side.
//   * `age_units` had no order repair at all while Python swaps an inverted
//     (top < base) pair and flags `index_order_swap`.
// Also: `_carry_extras(item, known, row)` is the 3-argument form — the old
// 2-argument `const ex = rcaCarryExtras(...)` call silently returned undefined
// once rcaCarryExtras moved to the out-parameter shape, which dropped EVERY
// columnar `_extras` (row-level and root-level).
function rcaNormalizeColumnarResult(parsed) {
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    // Python reaches for parsed.get() on a non-dict and raises; the caller's
    // try/except turns that into err.extract — and so does this.
    throw new Error('columnar payload is not an object');
  }
  // H3-fix: unwrap `_array_root` in place, exactly like Python's
  // `parsed.setdefault(key, []).append(item)`. The shallow copy keeps the
  // caller's object untouched while producing the identical payload.
  if (Array.isArray(parsed._array_root)) {
    parsed = Object.assign({}, parsed);
    const push = (key, item) => {
      const list = Array.isArray(parsed[key]) ? parsed[key].slice() : [];
      list.push(item);
      parsed[key] = list;
    };
    for (const item of parsed._array_root) {
      if (!item || typeof item !== 'object' || Array.isArray(item)) continue;
      if ('lithology_blocks' in item || 'age_units' in item || 'id' in item) {
        push('sections', item);
      } else if ('meaning' in item && ('marker' in item || 'pattern' in item)) {
        push('marker' in item ? 'fossil_legend' : 'lithology_legend', item);
      } else if ('from_section' in item || 'from_bed_idx' in item) {
        push('cross_beds', item);
      } else {
        push('_unclassified', item);
      }
    }
  }
  const asStr = rcaStringifyScalar;
  const idx = (v) => rcaBedIndex(v);
  const BLOCK_KNOWN = ['pattern', 'range_top_idx', 'range_base_idx'];
  const UNIT_KNOWN = ['label', 'range_top_idx', 'range_base_idx'];
  const SAMPLE_KNOWN = ['bed_idx', 'fossil_marker', 'ref'];
  const LEGEND_KNOWN = ['marker', 'pattern', 'meaning'];
  const CROSS_KNOWN = ['from_section', 'from_bed_idx', 'to_section', 'to_bed_idx'];
  const SECTION_KNOWN = ['id', 'group', 'lithology_blocks', 'age_units', 'samples',
                         'coordinates_text', 'thickness_m', 'confidence_by_section'];

  const normBlocks = (items) => {
    const rows = [];
    for (const b of rcaDictRows(items)) {
      const rawTop = b.range_top_idx;
      const rawBase = b.range_base_idx;
      const top = idx(rawTop);
      const base = idx(rawBase);
      let topIdx = top.value;
      let baseIdx = base.value;
      // B-3 fix: the prompt numbers beds from the bottom (oldest = 1) and asks
      // for top >= base; a reversed pair is swapped and flagged.
      let swapped = false;
      if (topIdx !== null && baseIdx !== null && topIdx < baseIdx) {
        const t = topIdx; topIdx = baseIdx; baseIdx = t;
        swapped = true;
      }
      const row = {
        pattern: asStr(b.pattern),
        range_top_idx: topIdx,
        range_base_idx: baseIdx,
      };
      const warnings = [];
      if (topIdx === null && rawTop !== null && rawTop !== undefined && rawTop !== '') {
        warnings.push('range_top_idx_unparseable');
      } else if (top.lossy) {
        warnings.push('range_top_idx_truncated');
      }
      if (baseIdx === null && rawBase !== null && rawBase !== undefined && rawBase !== '') {
        warnings.push('range_base_idx_unparseable');
      } else if (base.lossy) {
        warnings.push('range_base_idx_truncated');
      }
      if (swapped) warnings.push('index_order_swap');
      rcaSetRowWarning(row, warnings);
      rcaCarryExtras(b, BLOCK_KNOWN, row);
      rows.push(row);
    }
    return rows;
  };

  const normUnits = (items) => {
    const rows = [];
    for (const u of rcaDictRows(items)) {
      const top = idx(u.range_top_idx);
      const base = idx(u.range_base_idx);
      let topIdx = top.value;
      let baseIdx = base.value;
      let swapped = false;
      if (topIdx !== null && baseIdx !== null && topIdx < baseIdx) {
        const t = topIdx; topIdx = baseIdx; baseIdx = t;
        swapped = true;
      }
      const row = {
        label: asStr(u.label),
        range_top_idx: topIdx,
        range_base_idx: baseIdx,
      };
      // NOTE the order: units report the swap FIRST while blocks report the
      // unparseable/truncated pair first — Python's list order is what the UI
      // badge prints, so the two helpers are not interchangeable.
      const warnings = [];
      if (swapped) warnings.push('index_order_swap');
      if (top.lossy) warnings.push('range_top_idx_truncated');
      if (base.lossy) warnings.push('range_base_idx_truncated');
      rcaSetRowWarning(row, warnings);
      rcaCarryExtras(u, UNIT_KNOWN, row);
      rows.push(row);
    }
    return rows;
  };

  const normSamples = (items) => {
    const rows = [];
    for (const s of rcaDictRows(items)) {
      const bed = idx(s.bed_idx);
      const row = {
        bed_idx: bed.value,
        fossil_marker: asStr(s.fossil_marker),
        ref: asStr(s.ref),
      };
      if (bed.lossy) row._warning = 'bed_idx_truncated';
      rcaCarryExtras(s, SAMPLE_KNOWN, row);
      rows.push(row);
    }
    return rows;
  };

  // Returns {rows, warning} — Python's norm_legend tuple. A string legend
  // (Fix B-5) is not iterated character-wise, it is dropped WITH a flag.
  const normLegend = (items) => {
    let warning = null;
    let raw = items;
    if (typeof raw === 'string') {
      warning = 'legend_input_is_string';
      raw = [];
    }
    const rows = [];
    // REVIEW-2026-09-20 #3: a dict-shaped legend ({"ammonite": {"meaning": …}})
    // is what the model emits when it keys the entries by marker; the old
    // `items.filter(...)` shape yielded [] for it in the browser.
    for (const x of rcaDictRows(raw)) {
      // fossil_legend uses marker+meaning, lithology_legend pattern+meaning.
      // Carry both columns so neither legend's primary field is demoted into
      // _extras (which the exporter never reads) and rendered blank.
      const row = {
        marker: asStr(x.marker),
        pattern: asStr(x.pattern),
        meaning: asStr(x.meaning),
      };
      rcaCarryExtras(x, LEGEND_KNOWN, row);
      rows.push(row);
    }
    return { rows, warning };
  };

  const normCross = (items) => {
    const rows = [];
    for (const x of rcaDictRows(items)) {
      const from = idx(x.from_bed_idx);
      const to = idx(x.to_bed_idx);
      const row = {
        from_section: asStr(x.from_section),
        from_bed_idx: from.value,
        to_section: asStr(x.to_section),
        to_bed_idx: to.value,
      };
      const trunc = [];
      if (from.lossy) trunc.push('from_bed_idx_truncated');
      if (to.lossy) trunc.push('to_bed_idx_truncated');
      rcaSetRowWarning(row, trunc);
      rcaCarryExtras(x, CROSS_KNOWN, row);
      rows.push(row);
    }
    return rows;
  };

  const sections = [];
  for (const sec of rcaDictRows(parsed.sections)) {
    const row = {
      id: asStr(sec.id),
      group: asStr(sec.group),
      lithology_blocks: normBlocks(sec.lithology_blocks),
      age_units: normUnits(sec.age_units),
      samples: normSamples(sec.samples),
      coordinates_text: asStr(sec.coordinates_text),
      thickness_m: asStr(sec.thickness_m),
      confidence_by_section: rcaConfidenceClamped(
        'confidence_by_section' in sec ? sec.confidence_by_section : 0.0),
    };
    rcaCarryExtras(sec, SECTION_KNOWN, row);
    sections.push(row);
  }

  // Some models emit root `confidence` instead of `overall_confidence`; fall
  // back so the value is not silently zeroed. Python falls back when the raw
  // value is None as well as when the key is ABSENT (REVIEW-2026-09-10), and
  // `float()` — not Number() — decides what a bad value means.
  let overallRaw = parsed.overall_confidence;
  if (overallRaw === null || overallRaw === undefined) {
    overallRaw = 'confidence' in parsed ? parsed.confidence : 0.0;
  }
  const confidence = rcaConfidenceClamped(overallRaw);

  const fossil = normLegend(parsed.fossil_legend);
  const litho = normLegend(parsed.lithology_legend);
  const legendWarnings = [fossil.warning, litho.warning].filter(Boolean);

  const out = {
    sections,
    fossil_legend: fossil.rows,
    lithology_legend: litho.rows,
    cross_beds: normCross(parsed.cross_beds),
    confidence,
  };
  // Python keeps BOTH legends' identical `legend_input_is_string` flags (no
  // dedup), so a plain array — not rcaPushWarning — is the faithful mirror.
  if (legendWarnings.length) out._warnings = legendWarnings;
  // Columnar is the one mode whose root extras KEEP the array-root wrapper
  // keys (Python builds `root_extras` without _pop_array_root_extras).
  // H5/REVIEW-2026-09-20 #7: the foreign-payload flag is NOT raised here any
  // more — Python raises it in the shared `_ok_result`, which the extraction
  // dispatch mirrors (see rcaUnusablePayloadReason there).
  const rootEx = rcaTopExtras(parsed, RCA_MODE_ROOT_KEYS.columnar_section, true);
  if (rootEx) out._extras = rootEx;
  return out;
}

// Normalize the parsed abundance-diagram JSON into the strict result shape.
// Mirrors rca_core.extractor.normalize_abundance_result.
//
// REVIEW-2026-09-20 #4 (this round): the whole body was rebuilt on the Python
// rules; the browser copy had drifted in three separate ways.
//   * BUCKETING: the old JS classified a site by `site_id || site_name ||
//     location` and an abundance by `abundance || count || percentage`. Python
//     classifies on KEY PRESENCE (`"name" in item and ("location" in item or
//     "depth_unit" in item or "age_range" in item)`, then
//     `"taxon" in item or ("abundance" in item and "level" in item)`, then
//     `"age" in item and "name" in item`). A `{site_id, name}` row is a *site*
//     for Python (it has name+location? no → so it is _unclassified) and a site
//     for the old JS — so the same payload produced rows on one engine and an
//     empty table on the other.
//   * SILENT DROP: JS rebuilt `parsed` as a fresh `{sites, abundances, zones}`
//     object. Python mutates IN PLACE with `setdefault(...).append(item)`, so a
//     payload carrying BOTH `_array_root` and a named `sites` list keeps every
//     record, and anything unclassifiable lands in `_unclassified`, which then
//     survives into the root `_extras` (that is what
//     `sprintb-abundance-unclassified-in-extras` pins).
//   * `_warnings`: JS attached `truncated_or_unrecognized_payload` from inside
//     the normalizer. That flag is not a normalizer concern on the Python side
//     — `_ok_result` raises it for all eight modes — so abundance payloads
//     flipped ok=false in the browser only. Removed; the `dict_shaped_array` /
//     `string_row_coerced` flags from rcaIterRows replace it.
const _RCA_KNOWN_ABUNDANCE_SITE_KEYS = ['name', 'location', 'age_range', 'depth_unit'];
const _RCA_KNOWN_ABUNDANCE_ABUNDANCE_KEYS = ['taxon', 'site', 'level', 'depth',
                                             'abundance', 'abundance_unit'];
const _RCA_KNOWN_ABUNDANCE_ZONE_KEYS = ['name', 'age', 'level_range'];
// FIX-2026-09-22 (A2): mirror of _KNOWN_ABUNDANCE_ROOT_KEYS — `axis_calibration`
// is a hoisted root key, not an _extras leftover.
const _RCA_KNOWN_ABUNDANCE_ROOT_KEYS = ['sites', 'abundances', 'zones', 'confidence',
                                        'axis_calibration'];

function rcaNormalizeAbundanceResult(parsed) {
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) parsed = {};
  // H3-fix: unwrap the `_array_root` wrapper IN PLACE (Python 1941-1955),
  // appending to any bucket the payload already declared. Non-dict items are
  // skipped outright (unlike the zonation mode, which unclassifies them).
  if (Array.isArray(parsed._array_root)) {
    const bucket = (key) => {
      if (!Array.isArray(parsed[key])) parsed[key] = [];
      return parsed[key];
    };
    for (const item of parsed._array_root) {
      if (!item || typeof item !== 'object' || Array.isArray(item)) continue;
      if ('name' in item && ('location' in item || 'depth_unit' in item || 'age_range' in item)) {
        bucket('sites').push(item);
      } else if ('taxon' in item || ('abundance' in item && 'level' in item)) {
        bucket('abundances').push(item);
      } else if ('age' in item && 'name' in item) {
        bucket('zones').push(item);
      } else {
        bucket('_unclassified').push(item);
      }
    }
  }
  const warnings = [];
  const out = { sites: [], abundances: [], zones: [], confidence: 0.0 };
  // FIX-2026-09-22 (A2): BORROW-2026-09-20 (B) mirror — the optional
  // figure-level calibration for the continuous axis, read once for all rows.
  const axes = rcaAxisDomainsFrom(parsed);
  for (const site of rcaIterRows(parsed.sites, 'sites', warnings)) {
    const row = {
      name: rcaStringifyScalar(site.name),
      location: rcaStringifyScalar(site.location),
      age_range: rcaStringifyScalar(site.age_range),
      depth_unit: rcaStringifyScalar(site.depth_unit),
    };
    rcaCarryExtras(site, _RCA_KNOWN_ABUNDANCE_SITE_KEYS, row);
    out.sites.push(row);
  }
  for (const ab of rcaIterRows(parsed.abundances, 'abundances', warnings)) {
    const row = {
      taxon: rcaStringifyScalar(ab.taxon),
      site: rcaStringifyScalar(ab.site),
      level: rcaStringifyScalar(ab.level),
      depth: rcaStringifyScalar(ab.depth),
      abundance: rcaStringifyScalar(ab.abundance),
      abundance_unit: rcaStringifyScalar(ab.abundance_unit),
    };
    // FIX-2026-09-22 (A2): coverage contract + geometry sidecar, mirror of
    // `consumed = attach_row_contract(ab, row, axes=axes)` followed by the
    // extended _carry_extras known list.
    const consumed = rcaAttachRowContract(ab, row, axes);
    rcaCarryExtras(ab, _RCA_KNOWN_ABUNDANCE_ABUNDANCE_KEYS
      .concat(RCA_CONTRACT_SOURCE_KEYS, consumed), row);
    out.abundances.push(row);
  }
  for (const z of rcaIterRows(parsed.zones, 'zones', warnings)) {
    const row = {
      name: rcaStringifyScalar(z.name),
      age: rcaStringifyScalar(z.age),
      level_range: rcaStringifyScalar(z.level_range),
    };
    rcaCarryExtras(z, _RCA_KNOWN_ABUNDANCE_ZONE_KEYS, row);
    out.zones.push(row);
  }
  // Python: `float(parsed.get("confidence", 0.0))` in a try/except — NO `or`,
  // so "90%" raises ValueError and yields 0.0 rather than JS parseFloat's 90.
  out.confidence = rcaConfidenceClamped(
    Object.prototype.hasOwnProperty.call(parsed, 'confidence') ? parsed.confidence : undefined);
  // Root extras = every undocumented key, `_array_root`/`_note` stripped, and
  // `_unclassified` deliberately INCLUDED (it is not a root key), which is how
  // the unclassifiable records stay visible to the reviewer.
  // FIX-2026-09-22 (A2): then fold the calibration — hoist when usable,
  // warn + keep the raw block under _extras when not (mirror of
  // _fold_axis_calibration / audit item 7).
  const rootEx = rcaTopExtras(parsed, _RCA_KNOWN_ABUNDANCE_ROOT_KEYS, false) || {};
  rcaFoldAxisCalibration(parsed, axes, out, rootEx, warnings);
  if (Object.keys(rootEx).length) out._extras = rootEx;
  if (warnings.length) out._warnings = warnings;
  return out;
}

// P0-3 / REVIEW-2026-09-10: phylogenetic tree array-root unwrap.
// The old JS form REPLACED the payload with the inner item, so
// {"_array_root":[{nodes:[]}], "confidence":0.9} normalized to confidence 0.0
// and dropped the outer `legend` / `metadata` — exactly the bug Python fixed by
// switching to a setdefault merge. Wrapper bookkeeping keys
// (`_array_root`, `_note`) never participate in the merge.
function rcaUnwrapArrayRootPhylo(raw) {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return raw;
  if (!Array.isArray(raw._array_root)) return raw;
  for (const item of raw._array_root) {
    if (!item || typeof item !== 'object' || Array.isArray(item)) continue;
    if (!Array.isArray(item.nodes)) continue;
    const merged = { ...item };
    for (const key of Object.keys(raw)) {
      if (key === '_array_root' || key === '_note') continue;
      if (!(key in merged)) merged[key] = raw[key];
    }
    return merged;
  }
  return raw;
}

const _RCA_PHYLO_NODE_KEYS = ['id', 'parent', 'name', 'is_leaf',
                              'branch_length', 'node_age_ma', 'support'];
const _RCA_PHYLO_NEW_META_KEYS = ['title', 'extraction_timestamp', 'tree_type',
                                  'scale', 'rooted', 'source'];
const _RCA_PHYLO_ROOT_KEYS = ['metadata', 'nodes', 'root_ids', 'legend', 'confidence'];

// Mirror of rca_core/extractor.py:_normalize_phylogenetic_tree_into.
// REVIEW-2026-09-20 #4: rebuilt against the current Python body. The browser
// copy still had the pre-2026-09-10 shape:
//   * a FIXED metadata block (version/taxon_group/root_name/total_nodes/
//     image_source always written), while Python writes only the six canonical
//     keys and then PRESERVES whatever unknown keys the payload carried — so
//     JS invented `image_source: ""` for every tree and lost the raw values;
//   * `rooted: metaRaw.rooted !== false`, which read the string "false" as
//     rooted — Python uses _coerce_bool_flag(..., True);
//   * no synthetic-id repair, so an id-less node vanished and the topology
//     silently collapsed (Python flags `node_missing_id_synthesised`);
//   * `raw_ids` compared RAW against str-normalised node ids, so
//     {"root_ids":[1],"nodes":[{"id":"1"}]} threw
//     "Non-root node 1 must have a parent" in the browser only;
//   * `_is_leaf_corrected` never written;
//   * nodes read from `parsed.nodes` only, so a dict-shaped
//     {"nodes": {"r": {...}}} produced no rows at all.
function rcaNormalizePhylogeneticTreeResult(parsed) {
  let raw = rcaUnwrapArrayRootPhylo(parsed);
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) raw = {};

  const phyloWarnings = [];
  const fv = (v) => (v === null || v === undefined
    ? null : rcaPyFloatOrNull(v));

  const nodesIn = rcaDictRows(raw.nodes);

  // Stable synthetic ids for nodes the model left unnamed (Python 2190-2203).
  // Note the check STRIPS, the assignment does not: "  " is an empty id here.
  const existingIds = new Set();
  for (const n of nodesIn) {
    existingIds.add(rcaPyStr(rcaPyOr(n.id, '')).trim());
  }
  let anon = 0;
  for (const n of nodesIn) {
    const current = rcaPyStr(rcaPyOr(n.id, ''));
    if (current.trim()) continue;
    let candidate = '';
    for (;;) {
      anon += 1;
      candidate = '_anon' + anon;
      if (!existingIds.has(candidate)) break;
    }
    existingIds.add(candidate);
    n.id = candidate;
    // Python APPENDS without dedup: two repaired nodes mean two flags.
    phyloWarnings.push('node_missing_id_synthesised');
  }

  const idToNode = {};
  const childrenCount = {};
  for (const n of nodesIn) {
    const nid = rcaPyStr(rcaPyOr(n.id, ''));
    if (nid) {
      idToNode[nid] = n;
      childrenCount[nid] = 0;
    }
  }
  for (const n of nodesIn) {
    const parent = n.parent === undefined ? null : n.parent;
    if (parent !== null) {
      const pid = rcaPyStr(parent);
      if (Object.prototype.hasOwnProperty.call(childrenCount, pid)) {
        childrenCount[pid] = (childrenCount[pid] || 0) + 1;
      }
    }
  }

  // root_ids normalised to strings ONCE, up front (Sprint B REVIEW-2026-09-04).
  const rootIdsRaw = rcaPyOr(raw.root_ids, []);
  let rootIds = [];
  if (Array.isArray(rootIdsRaw)) {
    rootIds = rootIdsRaw.map(rcaPyStr);
  } else if (typeof rootIdsRaw === 'string') {
    rootIds = Array.from(rootIdsRaw).map(rcaPyStr);
  } else if (rootIdsRaw && typeof rootIdsRaw === 'object') {
    rootIds = Object.keys(rootIdsRaw).map(rcaPyStr);
  }
  if (!rootIds.length) throw new Error('root_ids is empty');
  for (const rid of rootIds) {
    if (!(rid in idToNode)) throw new Error('root_ids contains unknown node id: ' + rid);
  }

  const nodesOut = [];
  for (const n of nodesIn) {
    const nid = rcaPyStr(rcaPyOr(n.id, ''));
    if (!nid) continue;
    const parentVal = n.parent === undefined ? null : n.parent;
    if (rootIds.indexOf(nid) === -1) {
      if (parentVal === null) throw new Error('Non-root node ' + nid + ' must have a parent');
      if (!(rcaPyStr(parentVal) in idToNode)) {
        throw new Error('Node ' + nid + ' references parent ' + rcaPyStr(parentVal) + ' not in node ids');
      }
    } else if (parentVal !== null) {
      throw new Error('Root node ' + nid + ' must have parent == None, got ' + rcaPyStr(parentVal));
    }

    const isLeafInput = rcaPyTruthy(n.is_leaf);
    const actualIsLeaf = (childrenCount[nid] || 0) === 0;
    const leafCorrected = isLeafInput !== actualIsLeaf;

    const support = fv(n.support === undefined ? null : n.support);
    if (support !== null && !(support >= 0 && support <= 100)) {
      throw new Error('support must be in [0, 100] or None, got ' + rcaPyFloatStr(support));
    }

    const row = {
      id: nid,
      parent: parentVal !== null ? rcaPyStr(parentVal) : null,
      name: rcaStringifyScalar(n.name),
      is_leaf: actualIsLeaf,
      branch_length: fv(n.branch_length === undefined ? null : n.branch_length),
      node_age_ma: fv(n.node_age_ma === undefined ? null : n.node_age_ma),
      support: support,
    };
    const extras = {};
    let hasExtras = false;
    for (const k of Object.keys(n)) {
      if (_RCA_PHYLO_NODE_KEYS.indexOf(k) === -1) { extras[k] = n[k]; hasExtras = true; }
    }
    if (leafCorrected) { extras._is_leaf_corrected = true; hasExtras = true; }
    if (hasExtras) row.metadata = extras;
    nodesOut.push(row);
  }

  const metaRaw = rcaPyOr(raw.metadata, {});
  const metadata = {
    title: rcaStringifyScalar(Object.prototype.hasOwnProperty.call(metaRaw, 'title')
      ? metaRaw.title : ''),
    extraction_timestamp: rcaStringifyScalar(
      Object.prototype.hasOwnProperty.call(metaRaw, 'extraction_timestamp')
        ? metaRaw.extraction_timestamp : ''),
    tree_type: rcaStringifyScalar(Object.prototype.hasOwnProperty.call(metaRaw, 'tree_type')
      ? metaRaw.tree_type : ''),
    scale: rcaStringifyScalar(Object.prototype.hasOwnProperty.call(metaRaw, 'scale')
      ? metaRaw.scale : ''),
    rooted: rcaCoerceBoolFlag(
      Object.prototype.hasOwnProperty.call(metaRaw, 'rooted') ? metaRaw.rooted : undefined, true),
    // `metadata_raw.get("source", metadata_raw.get("image_source", ""))` is a
    // KEY-PRESENCE default, not an `or`: an explicit "source": null yields ""
    // and does NOT fall back to image_source.
    source: rcaStringifyScalar(
      Object.prototype.hasOwnProperty.call(metaRaw, 'source')
        ? metaRaw.source
        : (Object.prototype.hasOwnProperty.call(metaRaw, 'image_source') ? metaRaw.image_source : '')),
  };
  for (const k of Object.keys(metaRaw)) {
    if (_RCA_PHYLO_NEW_META_KEYS.indexOf(k) === -1) metadata[k] = metaRaw[k];
  }

  const legendRaw = raw.legend;
  const legend = (legendRaw && typeof legendRaw === 'object' && !Array.isArray(legendRaw))
    ? { ...legendRaw } : {};
  const conf = rcaConfidenceClamped(
    Object.prototype.hasOwnProperty.call(raw, 'confidence') ? raw.confidence : undefined);

  const rootEx = rcaTopExtras(raw, _RCA_PHYLO_ROOT_KEYS, false);
  const out = {
    metadata: metadata,
    root_ids: rootIds,
    nodes: nodesOut,
    legend: legend,
    confidence: conf,
  };
  if (rootEx) out._extras = rootEx;
  if (phyloWarnings.length) out._warnings = phyloWarnings;
  return out;
}

// UI-REVIEW-2026-09-05 / REVIEW-2026-09-20 #4: zonation-correlation normalizer,
// mirror of rca_core/extractor.py:normalize_zonation_chart_result.
// Three things the browser copy got wrong until this round:
//   * the `_array_root` unwrap REBUILT `parsed`, so a payload with both a named
//     `zones` list and a bare array lost one of the two — Python appends in
//     place with setdefault;
//   * Python routes NON-DICT array items into `_unclassified` (they stay
//     visible under `_extras`), JS dropped them on the floor;
//   * `parsed._note` (safe_json_loads' own wrapper note) leaked into the root
//     `_extras`; Python runs the leftovers through `_pop_array_root_extras`.
// The row fields themselves now come from rcaIterRows, so dict-shaped fields
// ({"zones": {"Z1": {...}}}) and bare-string rows are recovered and flagged
// instead of collapsing to [] in silence.
const _RCA_KNOWN_ZONATIONS_KEYS = ['name', 'region', 'framework', 'reference'];
const _RCA_KNOWN_ZONATION_ZONE_KEYS = ['name', 'zonation', 'rank', 'age_span',
                                       'base_age', 'top_age', 'stage', 'defined_by', 'note'];
const _RCA_KNOWN_CORRELATION_KEYS = ['from_zone', 'to_zone', 'from_zonation',
                                     'to_zonation', 'basis', 'note'];
const _RCA_KNOWN_ZONATION_ROOT_KEYS = ['zonations', 'zones', 'correlations', 'confidence'];

function rcaNormalizeZonationChartResult(parsed) {
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    return { zonations: [], zones: [], correlations: [], confidence: 0.0 };
  }
  if (Array.isArray(parsed._array_root)) {
    const bucket = (key) => {
      if (!Array.isArray(parsed[key])) parsed[key] = [];
      return parsed[key];
    };
    for (const item of parsed._array_root) {
      if (!item || typeof item !== 'object' || Array.isArray(item)) {
        bucket('_unclassified').push(item);
        continue;
      }
      if ('from_zone' in item || 'to_zone' in item) {
        bucket('correlations').push(item);
      } else if ('name' in item && ('rank' in item || 'zonation' in item || 'age_span' in item
                                    || 'defined_by' in item || 'base_age' in item)) {
        bucket('zones').push(item);
      } else if ('name' in item && ('region' in item || 'framework' in item || 'reference' in item)) {
        bucket('zonations').push(item);
      } else {
        bucket('_unclassified').push(item);
      }
    }
  }
  const warnings = [];
  const out = { zonations: [], zones: [], correlations: [], confidence: 0.0 };
  for (const z of rcaIterRows(parsed.zonations, 'zonations', warnings)) {
    const row = {
      name: rcaStringifyScalar(z.name),
      region: rcaStringifyScalar(z.region),
      framework: rcaStringifyScalar(z.framework),
      reference: rcaStringifyScalar(z.reference),
    };
    rcaCarryExtras(z, _RCA_KNOWN_ZONATIONS_KEYS, row);
    out.zonations.push(row);
  }
  for (const z of rcaIterRows(parsed.zones, 'zones', warnings)) {
    const row = {
      name: rcaStringifyScalar(z.name),
      zonation: rcaStringifyScalar(z.zonation),
      rank: rcaStringifyScalar(z.rank),
      age_span: rcaStringifyScalar(z.age_span),
      base_age: rcaStringifyScalar(z.base_age),
      top_age: rcaStringifyScalar(z.top_age),
      stage: rcaStringifyScalar(z.stage),
      defined_by: rcaStringifyScalar(z.defined_by),
      note: rcaStringifyScalar(z.note),
    };
    rcaCarryExtras(z, _RCA_KNOWN_ZONATION_ZONE_KEYS, row);
    out.zones.push(row);
  }
  for (const c of rcaIterRows(parsed.correlations, 'correlations', warnings)) {
    const row = {
      from_zone: rcaStringifyScalar(c.from_zone),
      to_zone: rcaStringifyScalar(c.to_zone),
      from_zonation: rcaStringifyScalar(c.from_zonation),
      to_zonation: rcaStringifyScalar(c.to_zonation),
      basis: rcaStringifyScalar(c.basis),
      note: rcaStringifyScalar(c.note),
    };
    rcaCarryExtras(c, _RCA_KNOWN_CORRELATION_KEYS, row);
    out.correlations.push(row);
  }
  // `float(parsed.get("confidence", 0.0) or 0.0)` — Python `or`, so "" / null /
  // [] / 0 collapse to 0.0 first, and "90%" raises and lands on 0.0 (the old
  // parseFloat gave 90 → clamped to 1).
  out.confidence = rcaConfidenceClamped(
    rcaPyOr(Object.prototype.hasOwnProperty.call(parsed, 'confidence')
              ? parsed.confidence : undefined, 0.0));
  const rootEx = rcaTopExtras(parsed, _RCA_KNOWN_ZONATION_ROOT_KEYS, false);
  if (rootEx) out._extras = rootEx;
  if (warnings.length) out._warnings = warnings;
  return out;
}

// Build a Newick string from a normalized phylogenetic tree.
// Mirrors Python rca_core.extractor._build_newick_node().
// REVIEW-2026-09-20 #5: two serializer divergences.
//   * ESCAPING: the old mirror rewrote `( ) :` to `_`, so "A(B)" exported as
//     "A_B_" — the taxon name was destroyed and re-importing the file gave a
//     different tree. rcaQuoteNewickLabel now applies the Newick single-quote
//     convention (quote on `()[];,` / whitespace, double embedded quotes).
//   * NUMBER FORM: `f":{bl}"` / `f"{support}"` are Python `str(float)`, which
//     keeps the trailing `.0`; `String(95)` gives "95", so the same tree
//     serialized to two different files per engine. rcaPyFloatStr restores it.
function rcaBuildNewickNode(nodeId, idToChildren, nodesDict) {
  const children = idToChildren[nodeId] || [];
  const n = (nodesDict && nodesDict[nodeId]) || {};
  if (!children.length) {
    const name = Object.prototype.hasOwnProperty.call(n, 'name') ? n.name : '';
    const bl = Object.prototype.hasOwnProperty.call(n, 'branch_length') ? n.branch_length : null;
    const blStr = bl !== null && bl !== undefined ? ':' + rcaPyFloatStr(bl) : '';
    return rcaQuoteNewickLabel(name) + blStr;
  }
  const childParts = children.map((cid) => rcaBuildNewickNode(cid, idToChildren, nodesDict));
  const support = Object.prototype.hasOwnProperty.call(n, 'support') ? n.support : null;
  const supportStr = support !== null && support !== undefined ? rcaPyFloatStr(support) : '';
  const bl = Object.prototype.hasOwnProperty.call(n, 'branch_length') ? n.branch_length : null;
  const blStr = bl !== null && bl !== undefined ? ':' + rcaPyFloatStr(bl) : '';
  return '(' + childParts.join(',') + ')' + supportStr + blStr;
}

function rcaToNewick(tree) {
  const nodes = rcaPyOr(tree && tree.nodes, []);
  const rootIds = rcaPyOr(tree && tree.root_ids, []);
  const nodeList = Array.isArray(nodes) ? nodes : [];
  const rootList = Array.isArray(rootIds) ? rootIds.map(rcaPyStr) : [];

  const nodesDict = {};
  for (const n of nodeList) {
    if (n && typeof n === 'object' && !Array.isArray(n)) {
      const nid = rcaPyStr(rcaPyOr(n.id, ''));
      if (nid) nodesDict[nid] = n;
    }
  }

  const idToChildren = {};
  for (const rid of rootList) idToChildren[rid] = [];
  for (const n of nodeList) {
    if (!n || typeof n !== 'object' || Array.isArray(n)) continue;
    const pid = n.parent === undefined ? null : n.parent;
    if (pid !== null) {
      const pidStr = rcaPyStr(pid);
      if (!(pidStr in idToChildren)) idToChildren[pidStr] = [];
      idToChildren[pidStr].push(rcaPyStr(rcaPyOr(n.id, '')));
    }
  }

  // Sprint B (REVIEW-2026-09-04): nodes unreachable from any root are still
  // dropped from the output, but the drop is reported rather than silent —
  // Python raises a RuntimeWarning naming them, this is the browser mirror.
  const reachable = new Set();
  const stack = rootList.filter((rid) => Object.prototype.hasOwnProperty.call(nodesDict, rid));
  while (stack.length) {
    const cur = stack.pop();
    if (reachable.has(cur)) continue;
    reachable.add(cur);
    const kids = idToChildren[cur] || [];
    for (const kid of kids) stack.push(kid);
  }
  const unreachable = Object.keys(nodesDict).filter((nid) => !reachable.has(nid)).sort();
  if (unreachable.length && typeof console !== 'undefined' && console.warn) {
    console.warn('to_newick: ' + unreachable.length + ' node(s) unreachable from root_ids '
                 + JSON.stringify(rootList) + ' were dropped from the Newick output: '
                 + JSON.stringify(unreachable));
  }

  const parts = rootList.map((rid) => rcaBuildNewickNode(rid, idToChildren, nodesDict));
  return '(' + parts.join(',') + ');';
}

// ---------------------------------------------------------------------------
// Network layer: transport error contract, request serialization, CSRF
// refresh, direct-mode SSRF list.
//
// REVIEW-2026-09-20 (frontend parity, network round): these rules mirror the
// Python side — server.py (CSRF mint + verification, error_key emission),
// rca_core/error_utils.py (error normalization + retry) and rca_core/ssrf.py
// (private-address policy). Browsers cannot resolve or pin DNS, so the SSRF
// half here is necessarily the "best effort" version of that policy; see
// rcaIsSsrfBlockedHost() for exactly what it can and cannot catch.
// ---------------------------------------------------------------------------

/**
 * Whitelist of transport `error_key` values this client is allowed to adopt
 * from a RESPONSE BODY.
 *
 * REVIEW-2026-09-20: the direct (proxy / provider) path lifts `error_key` out
 * of an attacker-influenced JSON body and app.js renders `t(errorKey)` — with
 * no gate, an upstream (or a malicious proxy the user pasted into settings)
 * could push any string into the UI, where it is either shown verbatim or
 * used as a lookup key. Only keys the Python side actually emits —
 * `grep -o '"err\\.[A-Za-z0-9_]*"' server.py rca_core/*.py` — plus the keys
 * this transport produces itself are accepted. `err.extract` is the Python
 * extractor's "normalizer refused the payload / unknown mode" answer
 * (rca_core/extractor.py, and the fallback the eight extract modes share).
 *
 * NOTE: `err.badRequest`, `err.serverBusy`, `err.imageInvalidBase64` and
 * `err.mode` are emitted by Python but have no entry in js/i18n.js yet, so
 * those four currently render as the bare key. Kept in the whitelist because
 * dropping them would be a worse lie; the translations belong to i18n.js.
 */
const RCA_KNOWN_ERROR_KEYS = new Set([
  // Emitted by server.py / rca_core (mirrors the Python "err.*" corpus).
  'err.401', 'err.403', 'err.429', 'err.badContentType', 'err.badEndpoint',
  'err.badMode', 'err.badRequest', 'err.bodyTooLarge', 'err.classify',
  'err.empty', 'err.exportFailed', 'err.extract', 'err.forbidden', 'err.http',
  'err.imageDecode', 'err.imageInvalidBase64', 'err.imageRead',
  'err.imageTooLarge', 'err.mode', 'err.network', 'err.noEndpoint',
  'err.noImage', 'err.noKey', 'err.parse', 'err.rateLimit', 'err.serverBusy',
  'err.timeout', 'err.truncated',
  // Produced by this JS transport only (never lifted from a body, but listed
  // so rcaKnownErrorKey() stays the single gate for every surfaced key).
  'err.cancelled', 'err.csrfFetch', 'err.fileTooBig', 'err.networkBackend',
]);

/**
 * Gate a candidate `error_key` through RCA_KNOWN_ERROR_KEYS.
 * @param {*} key - value read from a response body
 * @param {*} [fallback=null] - returned when the key is unknown
 * @returns {string|null} the key itself when known, else the fallback
 */
function rcaKnownErrorKey(key, fallback) {
  if (typeof key === 'string' && RCA_KNOWN_ERROR_KEYS.has(key)) return key;
  if (key && typeof console !== 'undefined' && console.warn) {
    // Never echo the raw value beyond a diagnostic prefix — it is attacker
    // controlled, which is exactly why it is being discarded.
    console.warn('[minimax] discarding untrusted error_key:',
      String(key).substring(0, 80));
  }
  return fallback === undefined ? null : fallback;
}

/**
 * Is this backend answer a recoverable CSRF rejection?
 *
 * server.py answers 403 + `err.forbidden` for both CSRF failures and
 * origin failures; only the CSRF half ("Missing CSRF token. Fetch
 * /api/extract (GET) to obtain a valid token.", "Invalid or expired CSRF
 * token.") is fixed by minting a new token, so the body marker is required.
 * @param {Object} payload - parsed backend response
 * @param {number} [httpStatus] - transport status
 * @returns {boolean}
 */
function rcaIsCsrfRejection(payload, httpStatus) {
  if (!payload || typeof payload !== 'object') return false;
  const forbidden = payload.error_key === 'err.forbidden' || httpStatus === 403;
  if (!forbidden) return false;
  const body = typeof payload.error_body === 'string' ? payload.error_body : '';
  return /csrf/i.test(body);
}

/**
 * Translate the backend's snake_case ExtractResult mirror into the shape
 * extractRangeChart returns to app.js.
 *
 * The only judgement call is the error key: it goes through
 * RCA_KNOWN_ERROR_KEYS (REVIEW-2026-09-20), so a body cannot install an
 * arbitrary i18n key. A failed extraction whose key was discarded still has
 * to render something translated — `err.http` is the same generic fallback
 * app.js itself uses when `errorKey` is missing, and the server's own status
 * text stays available in `errorBody` / `raw`.
 * @param {Object|null} payload - parsed backend answer
 * @param {number} [httpStatus] - transport status of the POST
 * @returns {Object} ExtractResult-shaped result for the UI
 */
function rcaBackendExtractResult(payload, httpStatus) {
  const p = (payload && typeof payload === 'object') ? payload : {};
  const ok = !!p.ok;
  const errorKey = rcaKnownErrorKey(p.error_key, ok ? null : 'err.http');
  return {
    ok: ok,
    data: p.data,
    errorKey: errorKey,
    // The server puts its status in the body; a transport-level 403/413/429
    // from _send_json does not, so fall back to what fetch reported.
    status: (typeof p.status === 'number') ? p.status : (httpStatus || p.status || null),
    raw: p.raw || '',
    truncated: !!p.truncated,
    errorBody: p.error_body || '',
    partialFailures: p.partial_failures || 0,
    usage: p.usage || null,
    latencyMs: p.latency_ms || 0,
    warning: p.warning || '',
  };
}

/**
 * Append a task to the CSRF-token serialization chain and return its promise.
 *
 * REVIEW-2026-09-20: `rcaCallBackend` used to build `myRequest` from
 * `_pending.then(...)` and then never write the new link back, so `_pending`
 * stayed the initial `Promise.resolve()` forever — every concurrent caller
 * took the "queue" branch off an already-resolved promise and the CSRF GETs
 * raced each other exactly as before the queue existed. The chain is now
 * real, AND it is failure-tolerant: the stored link swallows rejections so
 * one cancelled request cannot poison (or deadlock) the callers behind it.
 * @param {Function} task - async function to run after the previous link
 * @returns {Promise<*>} the task's own promise (rejections NOT swallowed)
 */
function rcaQueueBackendTask(task) {
  if (!rcaCallBackend._pending) rcaCallBackend._pending = Promise.resolve();
  const link = rcaCallBackend._pending.then(task, task);
  rcaCallBackend._pending = link.then(function () { /* settled */ },
                                     function () { /* settled */ });
  return link;
}

/** Hostnames that are always local/metadata, whatever they resolve to. */
const RCA_BLOCKED_HOST_NAMES = new Set([
  'localhost', 'ip6-localhost', 'ip6-loopback',
  'metadata', 'metadata.google.internal', 'metadata.goog',
  // Docker Desktop host aliases (resolve to the developer machine).
  'host.docker.internal', 'gateway.docker.internal',
  'docker.for.mac.localhost', 'docker.for.mac.host.internal',
]);

/**
 * True for every IPv4 literal the Python SSRF policy refuses
 * (rca_core/ssrf.py `_is_non_public_ip`, i.e. `not ip.is_global` plus the
 * prefixes that stdlib reports as global).
 * @param {string} host - dotted quad (already canonicalized by `new URL()`)
 * @returns {boolean}
 */
function rcaIsPrivateIpv4Host(host) {
  const m = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/.exec(String(host));
  if (!m) return false;
  const o = [Number(m[1]), Number(m[2]), Number(m[3]), Number(m[4])];
  if (o.some((v) => v > 255)) return false;   // not a literal — a DNS name
  if (o[0] === 0) return true;                // 0.0.0.0/8 "this network"
  if (o[0] === 10) return true;               // RFC1918
  if (o[0] === 127) return true;              // loopback
  if (o[0] === 169 && o[1] === 254) return true;   // link-local + cloud metadata
  if (o[0] === 172 && o[1] >= 16 && o[1] <= 31) return true;
  if (o[0] === 192 && o[1] === 168) return true;
  if (o[0] === 100 && o[1] >= 64 && o[1] <= 127) return true;  // CGNAT RFC6598
  // Deprecated 6to4 relay anycast: is_global == True in the stdlib, so the
  // Python side blocks it explicitly (REVIEW-2026-09-20 item 14).
  if (o[0] === 192 && o[1] === 88 && o[2] === 99) return true;
  if (o[0] === 198 && (o[1] === 18 || o[1] === 19)) return true;  // benchmarking
  if (o[0] === 192 && o[1] === 0 && (o[2] === 0 || o[2] === 2)) return true;
  if (o[0] === 198 && o[1] === 51 && o[2] === 100) return true;  // TEST-NET-2
  if (o[0] === 203 && o[1] === 0 && o[2] === 113) return true;  // TEST-NET-3
  // Multicast + reserved (240/4) + the limited broadcast address. CPython's
  // `is_global` still answers True for 224/4, so this is an intentional
  // fail-closed superset: no HTTP endpoint is addressed as a multicast group.
  if (o[0] >= 224) return true;
  return false;
}

/**
 * Expand a textual IPv6 literal into its eight 16-bit groups.
 * Handles `::` compression and a trailing dotted-quad; returns null when the
 * text is not a valid IPv6 literal.
 * @param {string} text - IPv6 literal WITHOUT the surrounding brackets
 * @returns {number[]|null}
 */
function rcaParseIpv6Groups(text) {
  let s = String(text || '').toLowerCase();
  if (!s) return null;
  // Trailing embedded IPv4 (e.g. "::ffff:127.0.0.1") -> two hextets. Browsers
  // normally re-serialize to hex, but a hand-typed baseUrl can arrive here.
  const v4tail = /:(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/.exec(s);
  if (v4tail) {
    const q = [Number(v4tail[1]), Number(v4tail[2]), Number(v4tail[3]), Number(v4tail[4])];
    if (q.some((v) => v > 255)) return null;
    s = s.slice(0, v4tail.index) + ':'
      + ((q[0] << 8) | q[1]).toString(16) + ':' + ((q[2] << 8) | q[3]).toString(16);
  }
  const halves = s.split('::');
  if (halves.length > 2) return null;
  const parse = (chunk) => {
    if (chunk === '') return [];
    return chunk.split(':').map((g) => (/^[0-9a-f]{1,4}$/.test(g) ? parseInt(g, 16) : NaN));
  };
  const left = parse(halves[0]);
  const right = halves.length === 2 ? parse(halves[1]) : null;
  if (left.some((v) => isNaN(v))) return null;
  if (right && right.some((v) => isNaN(v))) return null;
  if (right === null) {
    if (left.length !== 8) return null;
    return left;
  }
  const fill = 8 - left.length - right.length;
  if (fill < 0) return null;
  return left.concat(new Array(fill).fill(0), right);
}

/**
 * True for an IPv6 literal the Python SSRF policy refuses.
 * @param {string} inner - IPv6 literal WITHOUT the surrounding brackets
 * @returns {boolean}
 */
function rcaIsPrivateIpv6Host(inner) {
  const g = rcaParseIpv6Groups(inner);
  if (!g) return true;   // bracketed but unparseable -> fail closed
  /** True when groups [from, to) are all zero. */
  const zeros = (from, to) => g.slice(from, to).every((v) => v === 0);
  // IPv4-compatible IPv6 (::/96, the deprecated precursor of v4-mapped):
  // `::127.0.0.1` and `::169.254.169.254` are is_global == True in the
  // stdlib and Linux routes them to the embedded IPv4, so the WHOLE block is
  // rejected exactly like _IPV4_COMPATIBLE_V6 in rca_core/ssrf.py. Covers
  // `::` (unspecified) and `::1` (loopback) too.
  if (zeros(0, 6)) return true;
  const embeddedV4 = () => [
    (g[6] >> 8) & 255, g[6] & 255, (g[7] >> 8) & 255, g[7] & 255,
  ].join('.');
  // IPv4-mapped (::ffff:a.b.c.d) — judged by the embedded v4, like
  // _is_non_public_ip(ip.ipv4_mapped).
  if (zeros(0, 5) && g[5] === 0xffff) return rcaIsPrivateIpv4Host(embeddedV4());
  // NAT64 (RFC 6052): 64:ff9b::/96 and 64:ff9b:1::/48 embed a destination.
  // The /96 test compares groups 2..5 (the address' bits 32..95), NOT the
  // leading ones — `64:ff9b::169.254.169.254` must be rejected while
  // `64:ff9b:2::1` (outside both prefixes) stays reachable.
  if (g[0] === 0x0064 && g[1] === 0xff9b && (zeros(2, 6) || g[2] === 0x0001)) return true;
  // 6to4 (2002::/16) embeds an IPv4 destination; Python rejects the prefix.
  if (g[0] === 0x2002) return true;
  if ((g[0] & 0xfe00) === 0xfc00) return true;   // unique-local fc00::/7
  if ((g[0] & 0xffc0) === 0xfe80) return true;   // link-local fe80::/10
  // IANA special-purpose blocks that are not globally routable and that
  // `ipaddress` also reports as non-global (100::/64 discard-only,
  // 2001::/32 Teredo, 2001:2::/48 benchmarking, 2001:10::/28, 2001:db8::/32
  // documentation). No provider endpoint lives in any of them. Site-local
  // fec0::/10 is deliberately NOT listed: current CPython still calls it
  // global, so blocking it here would diverge from the Python half.
  if (g[0] === 0x0100 && zeros(1, 4)) return true;
  if (g[0] === 0x2001
      && (g[1] === 0x0000 || g[1] === 0x0002
          || (g[1] >= 0x0010 && g[1] <= 0x001f)
          || g[1] === 0x0db8)) return true;
  return false;
}

/**
 * Direct-mode SSRF gate for a hostname produced by `new URL(...).hostname`.
 *
 * LIMITS OF THE BROWSER HALF (documented deliberately, REVIEW-2026-09-20):
 *   * No DNS and no resolution: a public name that happens to resolve to a
 *     private address (`evil.example.com -> 127.0.0.1`, or a DNS-rebinding
 *     TTL-0 record) CANNOT be caught here — the Python side does that with
 *     `is_private_host()` + `pinned_endpoint_ip()` (server-side pinning).
 *   * `new URL()` normalizes the odd IPv4 spellings for us: single-label
 *     decimal (`http://2130706433`), hexadecimal (`http://0x7f000001`), and
 *     short forms (`http://127.1`, `http://0x7f.1`) all arrive as a canonical
 *     dotted quad, which is what makes the literal checks above sufficient.
 *     A name that is not numeric (`cafe`) is left alone by the parser and is
 *     therefore NOT misread as an address.
 *   * Redirects are not followed (`redirect: 'manual'` on both POSTs), so a
 *     `302 Location: http://169.254.169.254/` cannot bypass this list the way
 *     it bypassed urllib before _NoRedirect existed.
 * Fail-closed: an empty / unparseable hostname is blocked, matching the
 * "unresolvable name is unsafe" rule of rca_core/ssrf.py.
 * @param {string} hostname - lowercased `URL.hostname` (brackets kept for IPv6)
 * @returns {boolean} true when the direct path must not be used
 */
function rcaIsSsrfBlockedHost(hostname) {
  const host = String(hostname || '').toLowerCase();
  if (!host) return true;
  if (host.charAt(0) === '[' && host.slice(-1) === ']') {
    return rcaIsPrivateIpv6Host(host.slice(1, -1));
  }
  if (host.indexOf(':') !== -1) return true;   // bare IPv6 without brackets
  if (rcaIsPrivateIpv4Host(host)) return true;
  if (RCA_BLOCKED_HOST_NAMES.has(host)) return true;
  // RFC 6761: the whole *.localhost zone is loopback, not just the apex.
  if (host === 'localhost' || host.slice(-10) === '.localhost') return true;
  // Cloud metadata services also answer on internal-only DNS names.
  if (host === 'metadata' || host.slice(-9) === '.internal') return true;
  return false;
}

// Backend mode: POST to the same-origin Python server, which performs the
// MiniMax call server-side and returns an already-normalized result.
//
// FIXES applied:
// 1. JSON parse failure now captures HTTP status and response text
// 2. CSRF fetch failure shows better error (CSRF_FETCH_FAILED)
// 3. Concurrent requests are serialized via a pending queue to prevent token races
async function rcaCallBackend(opts, base64) {
  const controller = new AbortController();
  // Allow extra time when the server runs the extraction multiple times.
  const runs = Math.max(1, Math.min(parseInt(opts.runs, 10) || 1, 5));
  const timer = setTimeout(() => controller.abort(), RCA_CONFIG.requestTimeoutMs * runs + 5000);
  // FIX-6: honor a caller-supplied cancel signal (user pressed Cancel).
  const onExtAbort = () => controller.abort();
  if (opts.signal) {
    if (opts.signal.aborted) controller.abort();
    else opts.signal.addEventListener('abort', onExtAbort);
  }
  // Sprint B (REVIEW-2026-09-04): single cleanup path for the timer and the
  // external-abort listener. Previously only the happy path and the two
  // fetch-catch paths cleared them — the three early-error returns (aborted
  // CSRF GET, CSRF fetch failure, token-acquisition error object) leaked
  // both: the timer stayed armed and would abort an unrelated future
  // controller, and the listener kept the caller's AbortSignal alive.
  const cleanup = () => {
    clearTimeout(timer);
    if (opts.signal) opts.signal.removeEventListener('abort', onExtAbort);
  };
  // Fetch CSRF token before POST. Uses a persistent session token stored
  // in memory so subsequent requests reuse the same session.
  //
  // Serialize token acquisition through a shared pending-promise chain so a
  // fast user can fire two extractions without the second one's CSRF GET
  // racing the first's POST (the second will simply queue and execute after).
  // The chain (`_pending`) is written back by rcaQueueBackendTask() — see
  // REVIEW-2026-09-20 there, the queue was decorative before — so token
  // updates cannot interleave when extractions run concurrently.
  //
  // FIX: pass `signal: controller.signal` so a user cancel or the run-aware
  // timeout fires for the CSRF GET too.
  const acquireTokens = async () => {
    let sessionToken = rcaCallBackend._sessionToken || '';
    let csrfFetchFailed = false;
    let csrfErrorBody = '';
    try {
      const csrfResp = await fetch('/api/extract', {
        method: 'GET',
        headers: { 'X-Session-Token': sessionToken },
        signal: controller.signal,
      });
      if (csrfResp.ok) {
        const csrfData = await csrfResp.json();
        // Update stored tokens atomically after successful fetch
        rcaCallBackend._sessionToken = csrfData.session_token;
        sessionToken = csrfData.session_token;
        rcaCallBackend._csrfToken = csrfData.csrf_token;
      } else {
        // CSRF fetch returned non-OK - capture the error for better diagnostics
        csrfFetchFailed = true;
        try {
          const errText = await csrfResp.text();
          csrfErrorBody = errText.substring(0, 500);
        } catch (_e) { /* ignore */ }
      }
    } catch (_e) {
      // Network error on CSRF fetch - capture for better error message
      csrfFetchFailed = true;
      csrfErrorBody = _e && _e.message ? _e.message : 'network error';
      // REVIEW-2026-11-07 (low): an ABORTED CSRF GET (user pressed Cancel
      // mid-flight) is not a fetch failure. Without this the catch above
      // fell through to the err.csrfFetch branch below and the UI showed
      // "CSRF token failed" for what was really a user cancel.
      if (_e && _e.name === 'AbortError' && controller.signal.aborted) {
        return {
          ok: false,
          errorKey: (opts.signal && opts.signal.aborted) ? 'err.cancelled' : 'err.timeout',
          status: null,
          errorBody: 'CSRF fetch aborted',
        };
      }
    }

    // If CSRF fetch failed and we have no valid token, return error early
    if (csrfFetchFailed && !rcaCallBackend._csrfToken) {
      return {
        ok: false,
        errorKey: 'err.csrfFetch',
        status: null,
        errorBody: 'Failed to obtain CSRF token: ' + csrfErrorBody,
      };
    }

    const csrfToken = rcaCallBackend._csrfToken || '';
    return { sessionToken, csrfToken };
  };

  // One extraction POST. Returns { payload, status } on a parsed answer, or
  // { error: <result object> } for the transport-level failures the caller
  // used to `return` inline.
  const postExtract = async (sessionToken, csrfToken) => {
    let resp;
    try {
      resp = await fetch('/api/extract', {
        method: 'POST',
        headers: {
          'content-type': 'application/json',
          'X-CSRF-Token': csrfToken,
          'X-Session-Token': sessionToken,
        },
        body: JSON.stringify({
          api_key: opts.apiKey,
          image_b64: base64,
          media_type: opts.mediaType || 'image/png',
          caption: opts.caption || '',
          chart_lang: opts.chartLang || 'auto',
          endpoint: opts.baseUrl,
          model: opts.model,
          max_tokens: opts.maxTokens,
          mode: opts.mode || 'range_chart',
          runs: runs,
          force_rerun: !!opts.force_rerun,
          enhance: opts.enhance === true,
        }),
        signal: controller.signal,
      });
    } catch (err) {
      if (err && err.name === 'AbortError') {
        return {
          error: {
            ok: false,
            errorKey: (opts.signal && opts.signal.aborted) ? 'err.cancelled' : 'err.timeout',
          },
        };
      }
      return { error: { ok: false, errorKey: 'err.network', errorBody: String(err) } };
    }

    // FIX 1: JSON parse failure now captures HTTP status and response text
    try {
      const payload = await resp.json();
      return { payload, status: resp.status };
    } catch (_e) {
      // Try to capture the response text for better error diagnostics
      let parseErrorBody = '';
      try {
        const rawText = await resp.text();
        parseErrorBody = rawText.substring(0, 500);
      } catch (_e2) {
        parseErrorBody = 'could not read response body';
      }
      return {
        error: {
          ok: false,
          errorKey: 'err.parse',
          status: resp.status,
          errorBody: parseErrorBody,
          raw: parseErrorBody,
        },
      };
    }
  };

  try {
    // `payload` is the last parsed backend answer; `httpStatus` the transport
    // status that goes with it (the 403 CSRF body carries no status field).
    let payload = null;
    let httpStatus = 0;
    // CSRF silent retry: AT MOST ONE. server.py re-mints a CSRF token on every
    // GET /api/extract, keeps the session alive by sliding TTL
    // (_get_csrf_for_session refreshes last_seen), and — since
    // REVIEW-2026-09-20 — bills the mint to its own rate bucket
    // ("get:"+ip) instead of the extraction one, so a refresh cannot be
    // starved by the POST it is recovering for. That makes an automatic retry
    // cheap and correct where the old code only cleared the tokens and made
    // the USER retry. Bounding the loop at two attempts is what keeps a
    // mis-classified rejection from turning into a retry storm.
    for (let csrfAttempt = 0; csrfAttempt <= 1; csrfAttempt++) {
      let tokenResult;
      try {
        tokenResult = await rcaQueueBackendTask(acquireTokens);
      } catch (_e) {
        if (_e && _e.name === 'AbortError') {
          return {
            ok: false,
            errorKey: opts.signal && opts.signal.aborted ? 'err.cancelled' : 'err.timeout',
          };
        }
        return { ok: false, errorKey: 'err.network', errorBody: String(_e) };
      }

      // If token acquisition returned an error object, propagate it. On the
      // retry leg the refresh itself failed — report the ORIGINAL rejection
      // then, because that is the answer the server actually gave.
      if (tokenResult && tokenResult.errorKey) {
        if (csrfAttempt === 0 || !payload) return tokenResult;
        break;
      }

      const posted = await postExtract(tokenResult.sessionToken, tokenResult.csrfToken);
      if (posted.error) return posted.error;
      payload = posted.payload;
      httpStatus = posted.status;

      // FIX 2 (REVIEW-2026-09-20): recoverable CSRF/auth rejection — drop the
      // stale pair, mint a fresh one through the same queue, resend ONCE.
      // REVIEW-2026-11-07 (low): naming note — 'err.forbidden' is the BACKEND
      // mode's 403 (same-origin CSRF/origin rejection, token-clearable), while
      // 'err.403' (set in the direct-mode !resp.ok branch of
      // extractRangeChart) is a raw upstream 403 that needs no token
      // handling. Both are intentionally distinct keys; the similar names are
      // historical, don't merge them. An ORIGIN rejection also answers
      // err.forbidden but its body never says "CSRF", and a new token would
      // not fix it — hence rcaIsCsrfRejection() checks both.
      if (csrfAttempt === 0 && rcaIsCsrfRejection(payload, httpStatus)) {
        rcaCallBackend._csrfToken = null;
        rcaCallBackend._sessionToken = null;
        continue;
      }
      break;
    }

    // The server mirrors the ExtractResult shape with snake_case keys.
    return rcaBackendExtractResult(payload, httpStatus);
  } finally {
    cleanup();
  }
}

// Main entry. opts: { apiKey, baseUrl, model, maxTokens, proxyUrl, mode,
// dataUrl, mediaType, caption, chartLang }.
// mode defaults to 'range_chart'; 'columnar_section' switches prompt and
// normalizer to the columnar-section variants.
// UI-REVIEW-2026-09-07 / REVIEW-2026-09-20 #8: vision chart-type classifier
// mirror of rca_core/extractor.py:normalize_chart_classification.
// Confidence parsing was the drift: `parseFloat("90%")` is 90 in JS (clamped to
// 1 — the browser was CONFIDENT about a value Python rejects) while Python's
// `float("90%")` raises ValueError and yields 0.0, and `parseFloat([3])` is 3
// where `float([3])` raises a TypeError. rcaConfidenceClamped reproduces the
// raise-to-0.0 rule, rcaPyOr the `... or 0.0` prefix.
function rcaNormalizeChartClassification(parsed) {
  // Verbatim KNOWN_CHART_TYPES.
  const KNOWN = ['range_chart', 'columnar_section', 'abundance_diagram',
                 'phylogenetic_tree', 'zonation_chart', 'chemical_stratigraphy',
                 'paleomap', 'scatter_plot'];
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    return { chart_type: 'unknown', reason: '', confidence: 0.0 };
  }
  let chartType = rcaStringifyScalar(rcaPyOr(
    Object.prototype.hasOwnProperty.call(parsed, 'chart_type') ? parsed.chart_type : '', ''));
  chartType = chartType.trim().toLowerCase();
  if (KNOWN.indexOf(chartType) === -1) chartType = 'unknown';
  return {
    chart_type: chartType,
    reason: parsed.reason === null || parsed.reason === undefined
      ? '' : rcaStringifyScalar(parsed.reason),
    confidence: rcaConfidenceClamped(rcaPyOr(
      Object.prototype.hasOwnProperty.call(parsed, 'confidence') ? parsed.confidence : undefined,
      0.0)),
  };
}

async function extractRangeChart(opts) {
  // UI-REVIEW-2026-09-07: "auto" may be re-assigned from the vision
  // classifier on the direct path (backend path resolves server-side).
  let mode = (opts && opts.mode) || 'range_chart';
  const {
    apiKey,
    baseUrl,
    model,
    maxTokens,
    proxyUrl,
    dataUrl,
    mediaType,
    caption,
    chartLang,
  } = opts;

  const { base64 } = rcaSplitDataUrl(dataUrl);
  if (!base64) {
    return { ok: false, errorKey: 'err.imageRead' };
  }

  // Backend mode: when served by the Python server (http/https origin),
  // call the same-origin /api/extract so the outbound MiniMax request is
  // made server-side. This avoids the browser CORS restriction entirely.
  //
  // We dispatch on `opts.transport` (set by app.js), NOT on `opts.mode`,
  // because `mode` is the chart kind (range_chart / columnar_section).
  // Mixing them up was Bug C1 and made the backend path dead code.
  if (opts.transport === 'backend') {
    return rcaCallBackend(opts, base64);
  }

  const target = (proxyUrl && proxyUrl.trim())
    ? proxyUrl.trim().replace(/\/+$/, '')
    : String(baseUrl).replace(/\/+$/, '');
  // F-8: Enforce HTTPS for proxy URLs to prevent API key leakage via HTTP MITM
  // REVIEW-2026-09-10: the scheme check was case-sensitive, so "HTTP://…" (or
  // "Http://…") reached the direct path and was then validated by a guard
  // whose https requirement used a different comparison — an insecure
  // plaintext proxy could slip through. Schemes are case-insensitive per
  // RFC 3986 §3.1.
  if (/^http:\/\//i.test(target)) {
    console.error('Insecure proxy URL: HTTP is not allowed, falling back to direct connection');
    return rcaCallBackend(opts, base64);
  }
  // SSRF protection: block private/internal hostnames and cloud metadata
  // endpoints in direct mode. The rules live in rcaIsSsrfBlockedHost() above
  // (mirrors rca_core/ssrf.py `_is_non_public_ip` + `validate_endpoint`),
  // including this round's additions: 0.0.0.0/8, the CGNAT range
  // 100.64.0.0/10, the deprecated 6to4 relay anycast 192.88.99.0/24, the
  // whole *.localhost zone, the Docker host aliases, IPv4-compatible IPv6
  // (::/96, i.e. [::127.0.0.1] / [::169.254.169.254]) and 6to4 / NAT64.
  // `new URL()` normalizes the odd IPv4 spellings (decimal "2130706433", hex
  // "0x7f.1", short "127.1") to a canonical dotted quad, so the IPv4 rules
  // see a normalized string; IPv6 keeps its brackets and hex form and gets
  // its own pass. A host we cannot parse is blocked (fail closed, like the
  // "DNS failure is unsafe" rule on the Python side).
  let targetHostname;
  try {
    targetHostname = new URL(target).hostname.toLowerCase();
  } catch (_) {
    targetHostname = '';
  }
  if (rcaIsSsrfBlockedHost(targetHostname)) {
    console.error('Insecure endpoint: private/internal URLs are not allowed in direct mode, falling back to backend');
    return rcaCallBackend(opts, base64);
  }
  const url = target + '/v1/messages';

  // UI-REVIEW-2026-09-07 (auto mode, direct transport): the caption
  // heuristic matched nothing (app.js only forwards "auto" when that is
  // the case), so classify the image itself with a cheap small-token
  // call, then continue extraction with the detected chart type.
  // Confidence < 0.5 or "unknown" falls back to range_chart. The backend
  // transport never reaches here — rcaCallBackend sent mode:"auto" and
  // the server resolved it.
  if (mode === 'auto') {
    // UI-REVIEW-2026-09-07: surface the classification stage so the busy
    // label can show "Detecting chart type…" during the extra round-trip.
    if (opts.onStage) opts.onStage('classifying');
    try {
      const clsBody = {
        model: model,
        max_tokens: 500,
        system: (typeof CHART_CLASSIFY_SYSTEM_PROMPT !== 'undefined')
          ? CHART_CLASSIFY_SYSTEM_PROMPT : '',
        messages: [{
          role: 'user',
          content: [
            { type: 'image',
              source: { type: 'base64', media_type: mediaType || 'image/png', data: base64 } },
            { type: 'text',
              text: 'Caption:\n' + (caption && caption.trim() ? caption.trim() : '(no caption)')
                    + '\n\nClassify the chart type as the strict JSON contract.' },
          ],
        }],
      };
      const clsResp = await fetch(url, {
        method: 'POST',
        headers: {
          'x-api-key': apiKey,
          'anthropic-version': '2023-06-01',
          'content-type': 'application/json',
        },
        body: JSON.stringify(clsBody),
        redirect: 'manual',
      });
      if (clsResp && clsResp.ok) {
        const clsJson = await clsResp.json();
        const clsText = clsJson && clsJson.content && clsJson.content.length
          ? clsJson.content.filter((b) => b.type === 'text').map((b) => b.text).join('')
          : '';
        const cls = rcaNormalizeChartClassification(safeJsonLoads(clsText));
        if (cls.chart_type !== 'unknown' && cls.confidence >= 0.5) {
          mode = cls.chart_type;
        }
      }
      // classification failure -> keep "auto"; the modeInstruction fallback
      // below treats any non-listed mode as range_chart, matching Python.
    } catch (_e) {
      // swallow — fall back to range_chart prompt, same as Python.
    }
    if (mode === 'auto') mode = 'range_chart';
  }

  const langHint = (CHART_LANG_HINT && CHART_LANG_HINT[chartLang]) || '';
  let modeInstruction;
  if (mode === 'columnar_section') {
    modeInstruction = 'Extract the columnar-section information as the strict JSON contract.';
  } else if (mode === 'abundance_diagram') {
    modeInstruction = 'Extract the abundance-diagram information as the strict JSON contract.';
  } else if (mode === 'phylogenetic_tree') {
    modeInstruction = 'Extract the phylogenetic-tree information as the strict JSON contract.';
  } else if (mode === 'zonation_chart') {
    modeInstruction = 'Extract the biozonation / correlation chart information as the strict JSON contract.';
  } else {
    modeInstruction = 'Extract the geological information as the strict JSON contract.';
  }
  const userPrompt =
    'Caption:\n' + (caption && caption.trim() ? caption.trim() : '(no caption)') + '\n\n' +
    langHint + modeInstruction;

  let sysPrompt = RANGE_CHART_SYSTEM_PROMPT;
  if (mode === 'columnar_section' && typeof COLUMNAR_SECTION_SYSTEM_PROMPT !== 'undefined') {
    sysPrompt = COLUMNAR_SECTION_SYSTEM_PROMPT;
  } else if (mode === 'abundance_diagram' && typeof ABUNDANCE_DIAGRAM_SYSTEM_PROMPT !== 'undefined') {
    sysPrompt = ABUNDANCE_DIAGRAM_SYSTEM_PROMPT;
  } else if (mode === 'phylogenetic_tree' && typeof PHYLOGENETIC_TREE_SYSTEM_PROMPT !== 'undefined') {
    sysPrompt = PHYLOGENETIC_TREE_SYSTEM_PROMPT;
  } else if (mode === 'zonation_chart' && typeof ZONATION_CHART_SYSTEM_PROMPT !== 'undefined') {
    sysPrompt = ZONATION_CHART_SYSTEM_PROMPT;
  }

  const body = {
    model: model,
    max_tokens: maxTokens || 4000,
    system: sysPrompt,
    messages: [
      {
        role: 'user',
        content: [
          {
            type: 'image',
            source: { type: 'base64', media_type: mediaType || 'image/png', data: base64 },
          },
          { type: 'text', text: userPrompt },
        ],
      },
    ],
  };

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), RCA_CONFIG.requestTimeoutMs);
  // FIX-6: honor a caller-supplied cancel signal (user pressed Cancel).
  const onExtAbort = () => controller.abort();
  if (opts.signal) {
    if (opts.signal.aborted) controller.abort();
    else opts.signal.addEventListener('abort', onExtAbort);
  }

  // M11 (REVIEW-2026-08-19): wrap the direct-mode fetch in retryWithBackoff
  // so transient network failures (5xx, TypeError from fetch, broker reset)
  // get up to 3 retries before failing. AbortSignal still controls
  // cancellation at the retry-loop level, so a user Cancel stops the
  // retry chain mid-flight. Err.parse / err.cancelled are surfaced by
  // downstream branches — only TRANSPORT errors are retried.
  const tryOnce = async () => {
    const r = await fetch(url, {
      method: 'POST',
      headers: {
        'x-api-key': apiKey,
        'anthropic-version': '2023-06-01',
        'content-type': 'application/json',
      },
      body: JSON.stringify(body),
      signal: controller.signal,
      // M3 (REVIEW-2026-08-19): explicit `redirect: 'manual'` so a 3xx
      // response from the upstream doesn't auto-follow and leak the
      // `x-api-key` header to the redirect target. Browsers default to
      // `follow` which can send the same Authorization header to an
      // attacker-controlled endpoint.
      redirect: 'manual',
    });
    if (r && r.ok === false && r.status >= 500 && r.status < 600) {
      // 5xx is transient — throw so retryWithBackoff retries.
      const err = new Error('HTTP ' + r.status);
      err.status = r.status;
      throw err;
    }
    return r;
  };
  const ErrUtils = (typeof window !== 'undefined' && window.RCAErrorUtils) || null;

  let resp;
  try {
    if (ErrUtils && typeof ErrUtils.retryWithBackoff === 'function') {
      resp = await ErrUtils.retryWithBackoff(tryOnce, {
        maxRetries: 3,
        initialDelay: 0.8,
        backoffFactor: 1.6,
        maxDelay: 30.0,
        signal: opts.signal || null,
        onRetry: (attempt, delay, err) => {
          if (typeof console !== 'undefined' && console.warn) {
            console.warn('[extractRangeChart] retry', attempt, 'after', delay.toFixed(2), 's —', err && err.message);
          }
        },
      });
    } else {
      resp = await tryOnce();
    }
  } catch (err) {
    clearTimeout(timer);
    if (opts.signal) opts.signal.removeEventListener('abort', onExtAbort);
    if (err && err.name === 'AbortError') {
      return { ok: false, errorKey: (opts.signal && opts.signal.aborted) ? 'err.cancelled' : 'err.timeout' };
    }
    // CORS/TypeError from fetch, or transient 5xx that exhausted retries.
    return { ok: false, errorKey: 'err.network' };
  }
  clearTimeout(timer);
  if (opts.signal) opts.signal.removeEventListener('abort', onExtAbort);

  if (!resp.ok) {
    let detail = '';
    try { detail = await resp.text(); } catch (_e) { /* ignore */ }
    // REVIEW-2026-09-20 #110 mirror: bound the body BEFORE parsing it, so the
    // lifted error_key can only ever come from text the user is actually
    // shown — exactly what rca_core/error_utils.py `_extract_error_code` does
    // with MAX_ERROR_BODY_CHARS. A body truncated mid-JSON yields no key and
    // falls through to the status-derived one.
    const maxBodyChars = (typeof window !== 'undefined' && window.RCAErrorUtils
      && window.RCAErrorUtils.MAX_ERROR_BODY_CHARS) || 2000;
    if (detail.length > maxBodyChars) detail = detail.substring(0, maxBodyChars);
    // Phase M fix: try to surface the server's structured error_key
    // from the JSON body when the status is one of the documented
    // ones. Previously resp.text() was used unconditionally, which
    // discarded the server's `error_key: 'err.bodyTooLarge'` hint.
    let errorKey = 'err.http';
    if (resp.status === 401) errorKey = 'err.401';
    else if (resp.status === 403) errorKey = 'err.403';
    // REVIEW-2026-11-07 (low): direct-mode 403 → 'err.403' (upstream
    // rejected the key/endpoint). Distinct from backend-mode
    // 'err.forbidden' (CSRF/origin), see the comment in rcaCallBackend.
    else if (resp.status === 413) errorKey = 'err.bodyTooLarge';
    else if (resp.status === 429) errorKey = 'err.429';
    // If the body is JSON, lift the server's error_key (when it
    // matches a known key) so the i18n string is correct.
    // REVIEW-2026-09-20: "known" is now enforced. This body comes from an
    // upstream provider or a user-pasted proxy — attacker-shaped — and used
    // to be copied into `errorKey` verbatim, which app.js then fed to t() and
    // rendered. Anything outside RCA_KNOWN_ERROR_KEYS is dropped and the
    // status-derived key above stands.
    let serverKey = null;
    try {
      const j = JSON.parse(detail);
      if (j && typeof j.error_key === 'string') serverKey = j.error_key;
    } catch (_e) { /* not JSON, ignore */ }
    if (serverKey) {
      const known = rcaKnownErrorKey(serverKey, null);
      if (known) errorKey = known;
    }
    return { ok: false, errorKey, status: resp.status, raw: detail };
  }

  let payload;
  try {
    payload = await resp.json();
  } catch (_e) {
    return { ok: false, errorKey: 'err.parse', raw: '' };
  }

  // Extract ALL text content blocks (Anthropic-compatible shape).
  // REVIEW-2026-07-31: Anthropic splits long replies into multiple text
  // blocks; taking only the first one dropped the tail of the JSON, so
  // multi-block replies failed to parse in the browser-only path while
  // the Python side (_read_response) concatenated them and succeeded.
  // Concatenate all text blocks in order — identical to the Python side.
  let rawText = '';
  const content = Array.isArray(payload.content) ? payload.content : [];
  for (const c of content) {
    if (c && c.type === 'text' && typeof c.text === 'string') {
      rawText += c.text;
    }
  }
  // M10: detect truncation across API shapes. Anthropic uses
  // `stop_reason: "max_tokens"`; OpenAI uses `finish_reason: "length"`;
  // Gemini uses `candidates[].finishReason: "MAX_TOKENS" / "LENGTH"`.
  let truncated = false;
  if (payload && typeof payload === 'object') {
    if (payload.stop_reason === 'max_tokens') truncated = true;
    if (Array.isArray(payload.choices) && payload.choices[0] && payload.choices[0].finish_reason === 'length') truncated = true;
    if (Array.isArray(payload.candidates) && payload.candidates[0]) {
      const fr = payload.candidates[0].finishReason;
      if (fr === 'MAX_TOKENS' || fr === 'LENGTH') truncated = true;
    }
  }

  if (!rawText) {
    return { ok: false, errorKey: 'err.empty', raw: JSON.stringify(payload).slice(0, 2000), truncated };
  }

  let parsed;
  try {
    parsed = safeJsonLoads(rawText);
  } catch (_e) {
    return { ok: false, errorKey: 'err.parse', raw: rawText, truncated };
  }

  let data;
  // REVIEW-2026-07-31: the normalizers enforce invariants by throwing
  // (mirroring rca_core/extractor.py), but the Python caller catches the
  // exception and returns ok=False — the JS side previously let the throw
  // escape, violating the never-throws contract. Mirror the Python
  // try/except here.
  try {
    if (mode === 'columnar_section' && typeof rcaNormalizeColumnarResult === 'function') {
      data = rcaNormalizeColumnarResult(parsed);
    } else if (mode === 'abundance_diagram' && typeof rcaNormalizeAbundanceResult === 'function') {
      data = rcaNormalizeAbundanceResult(parsed);
    } else if (mode === 'phylogenetic_tree' && typeof rcaNormalizePhylogeneticTreeResult === 'function') {
      data = rcaNormalizePhylogeneticTreeResult(parsed);
    } else if (mode === 'zonation_chart' && typeof rcaNormalizeZonationChartResult === 'function') {
      data = rcaNormalizeZonationChartResult(parsed);
    } else {
      data = rcaNormalizeResult(parsed);
    }
  } catch (err) {
    const why = err && err.message ? err.message : String(err);
    return { ok: false, errorKey: 'err.extract', raw: rawText, truncated, warning: 'normalize failed: ' + why };
  }
  // REVIEW-2026-09-20 #7 (H5 was mode-conditional): mirror the SHARED
  // `_ok_result` contract, which every one of the eight Python extract modes
  // now returns through. Two rules matter:
  //   * `truncated` is an internal marker the array-root rescue may leave on the
  //     data; it is read, then DELETED, so it never reaches an export;
  //   * the flag is only appended to `_warnings` when the ROOT is unrelated
  //     (rcaUnusablePayloadReason does that), and every mode whose payload is
  //     unusable flips ok=false — not just range_chart. Before this the browser
  //     served a foreign or truncated columnar/abundance/phylo/zonation object
  //     as a confident ok=true empty table.
  // `warning` carries the Python PROSE (plus the reason) on both the error and
  // the truncated-success path, exactly like `ExtractResult.warning`, so the
  // direct and the backend transport answer identically; the
  // 'truncated_or_unrecognized_payload' token itself lives in `data._warnings`,
  // where the Python side puts it.
  let rescuedTruncated = false;
  if (data && typeof data === 'object') {
    rescuedTruncated = !!data.truncated;
    delete data.truncated;
  }
  // extractor.py:1391-1406 — a SECOND, range-chart-only guard that runs BEFORE
  // the shared `_ok_result`: when `normalize_result` itself raised the
  // foreign-payload flag, the run is a hard error. `_ok_result` alone would
  // have called it a success, because its `_extracted_any` test counts the
  // `_warnings` / `_extras` entries the normalizer just attached (Python
  // oracle, mode=range_chart + {"totally_unrelated_key": 1}: ok=False,
  // error_key=err.parse, warning="<prose> | rescued inner object: unusable").
  // The other seven modes have NO such pre-check — their normalizers never set
  // the tag, so a foreign columnar/abundance payload stays ok=True, which is
  // exactly what `rcaUnusablePayloadReason` reproduces.
  if (mode === 'range_chart' && data && Array.isArray(data._warnings)
      && data._warnings.indexOf('truncated_or_unrecognized_payload') !== -1) {
    return {
      ok: false,
      errorKey: 'err.parse',
      data,
      raw: rawText,
      truncated: !!truncated,
      warning: RCA_TRUNCATION_WARNING + ' | rescued inner object: unusable',
      reason: 'rescued inner object: unusable',
    };
  }
  const unusable = rcaUnusablePayloadReason(parsed, data, mode, truncated || rescuedTruncated);
  if (unusable) {
    return {
      ok: false,
      errorKey: 'err.parse',
      data,
      raw: rawText,
      truncated: !!truncated,
      warning: RCA_TRUNCATION_WARNING + ' | ' + unusable,
      reason: unusable,
    };
  }
  return {
    ok: true, data, raw: rawText, truncated,
    warning: truncated ? RCA_TRUNCATION_WARNING : '',
  };
}
