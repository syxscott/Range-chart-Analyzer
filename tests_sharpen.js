/**
 * tests_sharpen.js — FE-BORROW-2026-09-20 (domain W): the sharpen kernel and
 * its Worker offload.
 *
 * Two claims are under test, and they are deliberately different in kind:
 *
 *   1. PIXEL PARITY. `rcaUnsharpMaskCore` must reproduce the pre-change
 *      `rcaUnsharpMask` loop byte-for-byte. `legacyUnsharp3x3` below is that
 *      old loop transcribed verbatim (js/minimax.js:116-155 as of commit
 *      8f9ed23), and every parity case compares against it — so "we only moved
 *      the work off the main thread" is a checked fact, not a promise.
 *
 *   2. SINGLE SOURCE. js/sharpen_worker.js must NOT grow its own copy of the
 *      math. minimax.js composes the worker script at runtime as
 *      'use strict' + the two cap constants + String(rcaUnsharpMaskCore) +
 *      the verbatim glue file, and the integration cases below EXECUTE that
 *      composed text in a bare worker-like scope, so a drift between the two
 *      engines fails here rather than in a browser.
 *
 * Run with:  node tests_sharpen.js
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = __dirname;
const MINIMAX = path.join(ROOT, 'js', 'minimax.js');
const WORKER = path.join(ROOT, 'js', 'sharpen_worker.js');
const MAX_PIXELS = 16000000;   // mirrors RCA_UNSHARP_MAX_PIXELS

// ---------------------------------------------------------------------------
// fixtures
// ---------------------------------------------------------------------------

// The old loop, transcribed verbatim from the pre-change minimax.js (mutates
// `data`, returns it). 3x3 box blur, `amount` add-back, `threshold` gate.
function legacyUnsharp3x3(data, w, h, amount, threshold) {
  const blur = new Float32Array(w * h * 3);
  const at = (x, y, c) => {
    const cx = x < 0 ? 0 : (x >= w ? w - 1 : x);
    const cy = y < 0 ? 0 : (y >= h ? h - 1 : y);
    return data[(cy * w + cx) * 4 + c];
  };
  for (let y = 0; y < h; y++) {
    for (let x = 0; x < w; x++) {
      for (let c = 0; c < 3; c++) {
        let s = 0;
        for (let ky = -1; ky <= 1; ky++)
          for (let kx = -1; kx <= 1; kx++)
            s += at(x + kx, y + ky, c);
        blur[(y * w + x) * 3 + c] = s / 9;
      }
    }
  }
  for (let y = 0; y < h; y++) {
    for (let x = 0; x < w; x++) {
      const oi = (y * w + x) * 4;
      for (let c = 0; c < 3; c++) {
        const orig = data[oi + c];
        const diff = orig - blur[(y * w + x) * 3 + c];
        if (Math.abs(diff) >= threshold) {
          const v = orig + amount * diff;
          data[oi + c] = v < 0 ? 0 : (v > 255 ? 255 : v);
        }
      }
    }
  }
  return data;
}

// Independent generalized reference for radii the legacy loop never had:
// written from the definition (explicit clamp + tap list) rather than from
// either production copy, so an index error in the kernel shows up here.
function refBoxUnsharp(src, w, h, k, amount, threshold) {
  const size = 2 * k + 1;
  const div = size * size;
  const out = new src.constructor(src);
  const idx = [];
  for (let ky = -k; ky <= k; ky++) for (let kx = -k; kx <= k; kx++) idx.push([kx, ky]);
  for (let y = 0; y < h; y++) {
    for (let x = 0; x < w; x++) {
      for (let c = 0; c < 3; c++) {
        let s = 0;
        for (const [kx, ky] of idx) {
          const sx = Math.min(Math.max(x + kx, 0), w - 1);
          const sy = Math.min(Math.max(y + ky, 0), h - 1);
          s += src[(sy * w + sx) * 4 + c];
        }
        const diff = src[(y * w + x) * 4 + c] - s / div;
        if (Math.abs(diff) >= threshold) {
          const v = src[(y * w + x) * 4 + c] + amount * diff;
          out[(y * w + x) * 4 + c] = v < 0 ? 0 : (v > 255 ? 255 : v);
        }
      }
    }
  }
  return out;
}

let seed = 20260920;
function rndByte() { seed = (seed * 1103515245 + 12345) & 0x7fffffff; return seed % 256; }

// Deterministic pseudo-random RGBA image; alpha varies so the "alpha must be
// carried through untouched" property is actually observable.
function makeImage(w, h, mode) {
  const data = new Uint8ClampedArray(w * h * 4);
  for (let y = 0; y < h; y++) {
    for (let x = 0; x < w; x++) {
      const i = (y * w + x) * 4;
      if (mode === 'gray') {
        const g = ((x * 7 + y * 13) % 2) ? 255 : 0;   // hard checker: max deltas
        data[i] = data[i + 1] = data[i + 2] = g;
      } else if (mode === 'flat') {
        data[i] = 120; data[i + 1] = 121; data[i + 2] = 119;
      } else if (mode === 'ramp') {
        data[i] = (x * 255) / Math.max(1, w - 1);
        data[i + 1] = (y * 255) / Math.max(1, h - 1);
        data[i + 2] = 128;
      } else {
        data[i] = rndByte(); data[i + 1] = rndByte(); data[i + 2] = rndByte();
      }
      data[i + 3] = mode === 'alpha' ? ((x + y) % 3) * 90 : 255;
    }
  }
  return data;
}

function makeImageData(w, h, mode) {
  return { width: w, height: h, data: makeImage(w, h, mode) };
}

// A 2D-context stand-in: getImageData hands out a FRESH copy (like a real
// canvas, whose ImageData is detached from the bitmap until putImageData).
function makeFakeCtx(w, h, mode, opts) {
  const o = opts || {};
  const state = { getImageDataCalls: 0, putImageDataCalls: 0, last: null };
  const ctx = {
    canvas: { width: w, height: h },
    getImageData(_x, _y, cw, ch) {
      state.getImageDataCalls += 1;
      if (o.throwOnGet) throw new Error('tainted origin');
      if (cw !== w || ch !== h) throw new Error('getImageData out of bounds');
      return { width: w, height: h, data: new Uint8ClampedArray(o.pixels || makeImage(w, h, mode)) };
    },
    putImageData(img) {
      state.putImageDataCalls += 1;
      state.last = img;
      if (o.throwOnPut) throw new Error('putImageData rejected');
    },
  };
  return { ctx, state };
}

function loadMinimax(globals) {
  const ctx = Object.assign({
    console,
    setTimeout, clearTimeout,
    Promise,
    Float32Array, Uint8ClampedArray, Uint8Array, Float64Array, Int32Array,
  }, globals || {});
  ctx.globalThis = ctx;
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync(MINIMAX, 'utf8'), ctx, { filename: 'js/minimax.js' });
  return ctx;
}

function bytesEqual(a, b) {
  if (!a || !b || a.length !== b.length) return false;
  for (let i = 0; i < a.length; i++) if (a[i] !== b[i]) return false;
  return true;
}

function firstDiff(a, b) {
  const n = Math.min(a.length, b.length);
  for (let i = 0; i < n; i++) if (a[i] !== b[i]) return i + ': ' + a[i] + ' vs ' + b[i];
  return a.length === b.length ? 'same' : 'length ' + a.length + ' vs ' + b.length;
}

// ---------------------------------------------------------------------------
// tiny harness
// ---------------------------------------------------------------------------
let passed = 0;
const failures = [];
function check(label, ok, detail) {
  if (ok) { passed += 1; return; }
  failures.push(label + (detail === undefined || detail === '' ? '' : ' — ' + detail));
}
function checkEq(label, got, want) { check(label, got === want, 'got ' + JSON.stringify(got) + ', want ' + JSON.stringify(want)); }
async function test(name, fn) {
  try { await fn(); }
  catch (e) { failures.push(name + ' threw: ' + ((e && e.stack) || e)); }
}
function throws(fn, msgPart) {
  try { fn(); }
  catch (e) {
    // The kernel runs inside a vm context, so its RangeError is a different
    // constructor than the host's — compare by name.
    if (!e || e.name !== 'RangeError') return 'wrong error: ' + ((e && (e.name + ' ' + e.message)) || e);
    if (msgPart && String(e.message).indexOf(msgPart) === -1) return 'wrong message: ' + e.message;
    return true;
  }
  return 'did not throw';
}

// ---------------------------------------------------------------------------
// a fake browser Worker that runs the COMPOSED production script for real
// ---------------------------------------------------------------------------
function makeWorkerEnv(opts) {
  const o = opts || {};
  const glue = o.glue === undefined ? fs.readFileSync(WORKER, 'utf8') : o.glue;
  const env = {
    // FE-FIX-2026-09-21 (test harness): expose the options object so a case
    // can flip failure modes mid-run (a worker that timed out once and is
    // healthy again must not stay condemned), and count constructor throws
    // separately from built workers.
    opts: o,
    stats: { fetches: 0, blobs: 0, workers: 0, constructThrows: 0, transfers: [], revoked: 0, urls: [] },
    posted: [],        // messages the main thread sent INTO the worker
    workerSelf: null,  // the fake worker's global object, for direct pokes
  };
  let nextUrl = 0;
  const scripts = new Map();
  env.fetch = async (url) => {
    env.stats.fetches += 1;
    env.stats.lastUrl = url;
    if (o.fetchFails) return { ok: false, status: 500, text: async () => '' };
    if (o.textFails) throw new Error('Failed to fetch ' + url);
    return { ok: true, status: 200, text: async () => glue };
  };
  env.Blob = class Blob {
    constructor(parts) { this.parts = parts.join(''); env.stats.blobs += 1; }
  };
  env.URL = {
    createObjectURL(blob) { const u = 'blob:fake/' + (++nextUrl); scripts.set(u, blob.parts); env.stats.urls.push(u); return u; },
    revokeObjectURL() { env.stats.revoked += 1; },
  };
  env.Worker = class Worker {
    constructor(url) {
      // FE-FIX-2026-09-21 (test harness): `new Worker(blobUrl)` can throw
      // SYNCHRONOUSLY in a real browser (CSP refusing blob:, worker quota).
      // Throw before any state is touched, so the production cleanup path
      // (revoke + non-poisoned cache) is what the tests observe.
      if (o.throwOnConstruct) {
        env.stats.constructThrows += 1;
        throw new Error('Worker constructor refused');
      }
      env.stats.workers += 1;
      this.onmessage = null;
      this.onerror = null;
      const scope = {
        console,
        setTimeout, clearTimeout, Promise, Float32Array, Uint8ClampedArray, Uint8Array,
        postMessage: (msg, transfer) => {
          env.stats.transfers.push(transfer || []);
          if (this.onmessage) setImmediate(() => this.onmessage({ data: msg }));
        },
      };
      // A real DedicatedWorkerGlobalScope is its own `self`.
      scope.self = scope;
      if (o.importScripts) scope.importScripts = () => { throw new Error('blocked'); };
      env.workerSelf = scope;
      const text = scripts.get(url) || '';
      if (!o.skipScript) {
        vm.createContext(scope);
        vm.runInContext(text, scope, { filename: 'sharpen_worker.js (composed)' });
      }
    }
    postMessage(msg, transfer) {
      env.posted.push({ msg, transfer });
      // FE-FIX-2026-09-21 (test harness): a worker that accepts the job and
      // NEVER replies is how the 30 s timeout path becomes testable without
      // waiting 30 s (pair with makeFakeTimers below).
      if (o.silentWorker) return;
      if (o.deadWorker) { if (this.onerror) setImmediate(() => this.onerror({ message: 'dead' })); return; }
      const self = env.workerSelf;
      if (self && typeof self.onmessage === 'function') {
        setImmediate(() => self.onmessage({ data: msg }));
      }
    }
    terminate() { env.stats.terminated = (env.stats.terminated || 0) + 1; }
  };
  env.globals = { fetch: env.fetch, Blob: env.Blob, URL: env.URL, Worker: env.Worker };
  return env;
}

// ---------------------------------------------------------------------------
// FE-FIX-2026-09-21 (test harness): controllable timers so the 30 s worker
// job-timeout budget is testable without waiting 30 s. minimax.js uses
// setTimeout in exactly one place on this path (the sharpen job timer), so
// replacing the globals lets a case fire the expiry deterministically.
// ---------------------------------------------------------------------------
function makeFakeTimers() {
  const pending = new Map();
  let seq = 0;
  return {
    setTimeout: (fn, ms) => { const id = ++seq; pending.set(id, { fn, ms }); return id; },
    clearTimeout: (id) => { pending.delete(id); },
    pending: () => pending.size,
    fireLatest() {
      let last = null;
      for (const [id, t] of pending) last = { id, t };
      if (!last) throw new Error('fake timers: nothing pending');
      pending.delete(last.id);
      last.t.fn();
      return last.t.ms;
    },
  };
}
// Let every queued microtask (worker build chain, job postMessage, fake
// worker reply) run to completion before poking the timers.
const tick = () => new Promise((r) => setImmediate(r));

// ---------------------------------------------------------------------------
// 1-11: the pure kernel
// ---------------------------------------------------------------------------
async function kernelTests() {
  const m = loadMinimax();
  const core = m.rcaUnsharpMaskCore;
  check('core-exported', typeof core === 'function');

  const shapes = [[1, 1], [3, 2], [7, 5], [16, 9], [33, 17]];
  const knobs = [[0.4, 1], [1, 1], [0.4, 0], [0.4, 500], [4, 1], [0.4, 0.5]];

  // (1) parity against the verbatim legacy loop, across sizes and knobs
  let parityOk = true, parityDetail = '';
  let defaultOk = true;
  let inplaceOk = true;
  for (const [w, h] of shapes) {
    for (const mode of ['gray', 'color', 'flat', 'ramp', 'alpha']) {
      const src = makeImage(w, h, mode);
      for (const [amount, threshold] of knobs) {
        const ref = legacyUnsharp3x3(new Uint8ClampedArray(src), w, h, amount, threshold);
        const fresh = core(src, { width: w, height: h, radius: 1, percent: amount, threshold });
        if (!bytesEqual(ref, fresh)) { parityOk = false; parityDetail = mode + ' ' + w + 'x' + h + ' a=' + amount + ' t=' + threshold + ' @' + firstDiff(ref, fresh); }
        const dflt = core(src, { width: w, height: h, percent: amount, threshold });
        if (!bytesEqual(ref, dflt)) defaultOk = false;
        const aliased = new Uint8ClampedArray(src);
        core(aliased, { width: w, height: h, radius: 1, percent: amount, threshold }, aliased);
        if (!bytesEqual(ref, aliased)) { inplaceOk = false; parityDetail = 'in place ' + mode + ' ' + w + 'x' + h + ' @' + firstDiff(ref, aliased); }
      }
    }
  }
  check('core-parity-legacy-3x3', parityOk, parityDetail);
  check('core-radius-default-is-1', defaultOk);
  check('core-dst-may-alias-src', inplaceOk, parityDetail);

  // (2) alpha channel carried through untouched
  {
    const w = 9, h = 6;
    const src = makeImage(w, h, 'alpha');
    const before = new Uint8ClampedArray(src);
    const out = core(src, { width: w, height: h, percent: 4, threshold: 0 });
    let alphaOk = true;
    for (let i = 3; i < out.length; i += 4) if (out[i] !== src[i]) alphaOk = false;
    check('core-alpha-preserved', alphaOk);
    check('core-does-not-mutate-input', bytesEqual(src, before));
  }

  // (3) threshold boundary semantics: exactly at the gate it sharpens
  {
    const w = 5, h = 1;
    // Mid-gray field with one brighter pixel: small enough that the add-back
    // cannot saturate, so a threshold change is actually observable.
    const src = new Uint8ClampedArray(w * h * 4);
    for (let x = 0; x < w; x++) {
      src[x * 4] = x === 2 ? 200 : 100;
      src[x * 4 + 1] = 100; src[x * 4 + 2] = 100; src[x * 4 + 3] = 255;
    }
    const r0 = core(src, { width: w, height: h, percent: 1, threshold: 0 });
    const rBig = core(src, { width: w, height: h, percent: 1, threshold: 500 });
    check('core-threshold-zero-sharpens', !bytesEqual(r0, src), firstDiff(src, r0));
    check('core-threshold-huge-is-noop', bytesEqual(rBig, src), firstDiff(src, rBig));
    const refT = legacyUnsharp3x3(new Uint8ClampedArray(src), w, h, 1, 1);
    check('core-threshold-1-parity', bytesEqual(core(src, { width: w, height: h, percent: 1, threshold: 1 }), refT));
    check('core-threshold-abs-is-mirrored', bytesEqual(
      core(src, { width: w, height: h, percent: 1, threshold: -1 }), refT));
    const gate = core(src, { width: w, height: h, percent: 1, threshold: 70 });
    check('core-threshold-gates-flat-neighbours',
      !bytesEqual(gate, r0) && gate[4] === src[4], firstDiff(r0, gate));
  }

  // (4) 1px image is a pure identity (blur == orig => diff 0)
  {
    const src = new Uint8ClampedArray([17, 99, 200, 128]);
    const out = core(src, { width: 1, height: 1, percent: 4, threshold: 0 });
    check('core-1px-identity', bytesEqual(out, src), firstDiff(src, out));
  }

  // (5) radii beyond the legacy 3x3, against the independent reference
  {
    const w = 11, h = 7;
    const src = makeImage(w, h, 'color');
    let radiiOk = true;
    for (const k of [0, 2, 3, 8]) {
      const ref = refBoxUnsharp(src, w, h, k, 0.4, 1);
      const got = core(src, { width: w, height: h, radius: k, percent: 0.4, threshold: 1 });
      if (!bytesEqual(ref, got)) radiiOk = false;
    }
    check('core-radius-generalization', radiiOk);
    check('core-radius-0-is-identity',
      bytesEqual(core(src, { width: w, height: h, radius: 0, percent: 4, threshold: 0 }), src));
    check('core-radius-rounds', bytesEqual(
      core(src, { width: w, height: h, radius: 2.4, percent: 0.4, threshold: 1 }),
      core(src, { width: w, height: h, radius: 2, percent: 0.4, threshold: 1 })));
  }

  // (6) output element type follows the input, and values stay in range
  {
    const w = 8, h = 8;
    const src = makeImage(w, h, 'gray');
    const asClamped = core(src, { width: w, height: h, percent: 4, threshold: 0 });
    check('core-returns-same-kind-typed', asClamped instanceof Uint8ClampedArray);
    const plain = core(Array.prototype.slice.call(src), { width: w, height: h, percent: 4, threshold: 0 });
    check('core-plain-array-in-plain-out', Array.isArray(plain));
    const asFloat = core(new Float32Array(src), { width: w, height: h, percent: 4, threshold: 0 });
    let inRange = true;
    for (let i = 0; i < asFloat.length; i++) {
      if (asFloat[i] < 0 || asFloat[i] > 255) inRange = false;
      if (i % 4 === 3 && asFloat[i] !== src[i]) inRange = false;
    }
    check('core-float-dst-stays-in-range', inRange);
    check('core-clamps-both-ends',
      Math.max.apply(null, Array.prototype.slice.call(asClamped, 0, 12)) <= 255
      && Math.min.apply(null, Array.prototype.slice.call(asClamped, 0, 12)) >= 0);
  }

  // (7) rejections
  {
    const src = makeImage(4, 4, 'color');
    const bad = { getImageData: () => ({ width: 4001, height: 4000, data: new Uint8ClampedArray(4) }), putImageData: () => {} };
    const ct = (label, fn, msgPart) => { const r = throws(fn, msgPart); check(label, r === true, r); };
    ct('core-rejects-oversize', () => core(src, { width: 4001, height: 4000 }), 'too large');
    ct('core-rejects-zero-dims', () => core(src, { width: 0, height: 4 }), 'positive');
    ct('core-rejects-fractional-dims', () => core(src, { width: 4.5, height: 4 }), 'positive');
    ct('core-rejects-negative-dims', () => core(src, { width: -4, height: 4 }), 'positive');
    ct('core-rejects-missing-dims', () => core(src, {}));
    ct('core-rejects-short-src', () => core(src, { width: 99, height: 99 }), 'src needs');
    ct('core-rejects-short-dst', () => core(src, { width: 4, height: 4 }, new Uint8ClampedArray(8)), 'dst needs');
    ct('core-rejects-huge-radius', () => core(src, { width: 4, height: 4, radius: 9 }), 'radius');
    ct('core-rejects-nan-percent', () => core(src, { width: 4, height: 4, percent: NaN }), 'finite');
    ct('core-rejects-null-src', () => core(null, { width: 4, height: 4 }));
    checkEq('sync-oversize-rejected-by-the-cap', m.rcaUnsharpMask(bad, 4001, 4000, 0.4, 1), false);
  }

  // (8) the 16M cap is checked BEFORE anything is allocated
  {
    const t0 = Date.now();
    let threw = false;
    try { core(new Uint8ClampedArray(4), { width: 50000, height: 50000 }); } catch (e) { threw = true; }
    check('core-oversize-guard-is-cheap', threw && Date.now() - t0 < 500);
  }
}

// ---------------------------------------------------------------------------
// 12-14: the synchronous wrapper (legacy signature)
// ---------------------------------------------------------------------------
async function syncWrapperTests() {
  const m = loadMinimax();
  {
    const w = 13, h = 9;
    const pixels = makeImage(w, h, 'alpha');
    const { ctx, state } = makeFakeCtx(w, h, null, { pixels });
    checkEq('sync-returns-true', m.rcaUnsharpMask(ctx, w, h, 0.4, 1), true);
    const ref = legacyUnsharp3x3(new Uint8ClampedArray(pixels), w, h, 0.4, 1);
    check('sync-canvas-pixels-parity', state.last && bytesEqual(ref, state.last.data),
      state.last ? firstDiff(ref, state.last.data) : 'putImageData never called');
    checkEq('sync-getimagedata-once', state.getImageDataCalls, 1);
    checkEq('sync-putimagedata-once', state.putImageDataCalls, 1);
  }
  {
    // ImageData target: mutated in place, no context involved.
    const img = makeImageData(6, 4, 'color');
    const before = new Uint8ClampedArray(img.data);
    const ok = m.rcaUnsharpMask(img, 6, 4, 0.4, 1);
    const ref = legacyUnsharp3x3(new Uint8ClampedArray(before), 6, 4, 0.4, 1);
    checkEq('sync-imagedata-returns-true', ok, true);
    check('sync-imagedata-in-place', bytesEqual(ref, img.data));
  }
  {
    // Oversize and unusable targets degrade to the legacy silent skip.
    const { ctx } = makeFakeCtx(4, 4);
    let canvasTouched = 0;
    const oversize = {
      getImageData: () => {
        canvasTouched += 1;
        return { width: 4001, height: 4000, data: new Uint8ClampedArray(4) };
      },
      putImageData: () => { throw new Error('must not be reached'); },
    };
    checkEq('sync-oversize-skips', m.rcaUnsharpMask(oversize, 4001, 4000, 0.4, 1), false);
    checkEq('sync-oversize-before-getimagedata', canvasTouched, 0);
    checkEq('sync-out-of-bounds-skips', m.rcaUnsharpMask(ctx, 40, 40, 0.4, 1), false);
    checkEq('sync-bad-target-skips', m.rcaUnsharpMask({}, 4, 4, 0.4, 1), false);
    const tainted = makeFakeCtx(4, 4, 'color', { throwOnGet: true });
    checkEq('sync-tainted-skips', m.rcaUnsharpMask(tainted.ctx, 4, 4, 0.4, 1), false);
  }
  {
    // radius may be passed positionally as the 5th arg (extension, not a break)
    const w = 9, h = 5;
    const pixels = makeImage(w, h, 'color');
    const a = makeFakeCtx(w, h, null, { pixels });
    const b = makeFakeCtx(w, h, null, { pixels });
    m.rcaUnsharpMask(a.ctx, w, h, 0.4, 1, 2);
    m.rcaUnsharpMask(b.ctx, w, h, 0.4, 1, { radius: 2 });
    check('sync-radius-object-and-number-agree',
      bytesEqual(a.state.last.data, b.state.last.data));
    check('sync-radius-2-differs-from-legacy-3x3',
      !bytesEqual(a.state.last.data, legacyUnsharp3x3(new Uint8ClampedArray(pixels), w, h, 0.4, 1)));
    check('sync-radius-2-parity-with-core',
      bytesEqual(a.state.last.data, m.rcaUnsharpMaskCore(pixels, { width: w, height: h, radius: 2, percent: 0.4, threshold: 1 })));
  }
}

// ---------------------------------------------------------------------------
// 15-20: the fallback selector and the async API
// ---------------------------------------------------------------------------
async function asyncTests() {
  // (15) no Worker in the environment -> synchronous core, same pixels
  {
    const m = loadMinimax();
    checkEq('gate-no-worker-unsupported', m.rcaSharpenWorkerSupported(), false);
    checkEq('gate-no-worker-path', m.rcaSharpenPath(), 'sync');
    const w = 12, h = 8;
    const pixels = makeImage(w, h, 'alpha');
    const { ctx, state } = makeFakeCtx(w, h, null, { pixels });
    const res = await m.rcaUnsharpMaskAsync(ctx, w, h, 0.4, 1);
    checkEq('fallback-sync-via', res.via, 'sync');
    checkEq('fallback-sync-applied', res.applied, true);
    const ref = legacyUnsharp3x3(new Uint8ClampedArray(pixels), w, h, 0.4, 1);
    check('fallback-sync-pixels-parity', bytesEqual(ref, state.last.data));
  }
  {
    // partially capable environments: each missing global forces the sync path
    for (const missing of ['Worker', 'fetch', 'Blob', 'URL']) {
      const globals = makeWorkerEnv().globals;
      delete globals[missing];
      const m = loadMinimax(globals);
      checkEq('gate-missing-' + missing + '-unsupported', m.rcaSharpenWorkerSupported(), false);
    }
    const m = loadMinimax(makeWorkerEnv().globals);
    checkEq('gate-all-present-supported', m.rcaSharpenWorkerSupported(), true);
    checkEq('gate-all-present-path', m.rcaSharpenPath(), 'worker');
  }

  // (16) worker path: real glue + real kernel text, real reply
  {
    const env = makeWorkerEnv();
    const m = loadMinimax(env.globals);
    const w = 12, h = 8;
    const pixels = makeImage(w, h, 'alpha');
    const { ctx, state } = makeFakeCtx(w, h, null, { pixels });
    const res = await m.rcaUnsharpMaskAsync(ctx, w, h, 0.4, 1);
    checkEq('worker-via', res.via, 'worker');
    checkEq('worker-applied', res.applied, true);
    const ref = legacyUnsharp3x3(new Uint8ClampedArray(pixels), w, h, 0.4, 1);
    check('worker-pixels-parity', bytesEqual(ref, state.last.data),
      firstDiff(ref, state.last.data));
    checkEq('worker-built-once', env.stats.workers, 1);
    checkEq('worker-glue-fetched-once', env.stats.fetches, 1);
    checkEq('worker-reused-across-idle', (await m.rcaUnsharpMaskAsync(ctx, w, h, 0.4, 1)).via, 'worker');
    checkEq('worker-not-rebuilt', env.stats.workers, 1);
    const job = env.posted[0];
    checkEq('worker-job-opts-radius', job.msg.opts.radius, 1);
    checkEq('worker-job-opts-percent', job.msg.opts.percent, 0.4);
    checkEq('worker-job-opts-width', job.msg.opts.width, w);
    check('worker-job-transfers-src', job.transfer.length === 1 && job.transfer[0] === job.msg.src.buffer,
      JSON.stringify({ n: job.transfer.length }));
    check('worker-src-is-a-clone', job.msg.src.length === pixels.length
      && job.msg.src !== state.last.data && bytesEqual(job.msg.src, pixels));
    checkEq('worker-script-hands-over-one-blob', env.stats.blobs, 1);
    check('worker-pixel-output-is-clamped-bytes', state.last.data instanceof Uint8ClampedArray);
    // The wire opts are fully resolved, and their defaults are the core's own
    // defaults — otherwise the worker and the fallback could disagree.
    const dflt = m.rcaUnsharpArgs(5, 4, undefined, undefined, undefined);
    checkEq('wrapper-resolves-radius', dflt.radius, 1);
    const probe = makeImage(5, 4, 'color');
    check('wrapper-defaults-match-core-defaults', bytesEqual(
      m.rcaUnsharpMaskCore(probe, { width: 5, height: 4 }),
      m.rcaUnsharpMaskCore(probe, dflt)));
  }

  // (17) worker rejects the job -> synchronous fallback, same pixels
  {
    const env = makeWorkerEnv({ glue: fs.readFileSync(WORKER, 'utf8').replace('rcaUnsharpMaskCore(msg.src, msg.opts)', 'throw new Error("kernel exploded")') });
    const m = loadMinimax(env.globals);
    const w = 7, h = 5;
    const pixels = makeImage(w, h, 'color');
    const { ctx, state } = makeFakeCtx(w, h, null, { pixels });
    const res = await m.rcaUnsharpMaskAsync(ctx, w, h, 0.4, 1);
    checkEq('worker-reject-falls-back-via', res.via, 'sync');
    checkEq('worker-reject-falls-back-applied', res.applied, true);
    check('worker-reject-pixels-parity',
      bytesEqual(legacyUnsharp3x3(new Uint8ClampedArray(pixels), w, h, 0.4, 1), state.last.data));
  }

  // (17b) a worker that dies mid-job (onerror) is torn down and the job runs
  // synchronously — the load pipeline still gets sharpened pixels.
  {
    const env = makeWorkerEnv({ deadWorker: true });
    const m = loadMinimax(env.globals);
    const w = 7, h = 4;
    const pixels = makeImage(w, h, 'ramp');
    const { ctx, state } = makeFakeCtx(w, h, null, { pixels });
    const res = await m.rcaUnsharpMaskAsync(ctx, w, h, 0.4, 1);
    checkEq('dead-worker-via', res.via, 'sync');
    checkEq('dead-worker-applied', res.applied, true);
    check('dead-worker-pixels-parity',
      bytesEqual(legacyUnsharp3x3(new Uint8ClampedArray(pixels), w, h, 0.4, 1), state.last.data));
    check('dead-worker-terminated', (env.stats.terminated || 0) >= 1);
    // A torn-down worker must never be reused: the next job rebuilds it...
    const res2 = await m.rcaUnsharpMaskAsync(ctx, w, h, 0.4, 1);
    checkEq('dead-worker-rebuilds-via', res2.via, 'sync');
    checkEq('dead-worker-rebuilt', env.stats.workers, 2);
    // ... until the failure budget is spent, after which the deployment pins
    // to the synchronous core instead of rebuilding per image.
    const res3 = await m.rcaUnsharpMaskAsync(ctx, w, h, 0.4, 1);
    checkEq('dead-worker-gives-up-via', res3.via, 'sync');
    checkEq('dead-worker-stops-rebuilding', env.stats.workers, 2);
    check('dead-worker-still-sharpens', bytesEqual(
      legacyUnsharp3x3(new Uint8ClampedArray(pixels), w, h, 0.4, 1), state.last.data));
  }

  // (18) glue fetch fails (file:// / 404) -> sync, and the verdict is cached
  {
    const env = makeWorkerEnv({ fetchFails: true });
    const m = loadMinimax(env.globals);
    const w = 6, h = 6;
    const pixels = makeImage(w, h, 'gray');
    const { ctx } = makeFakeCtx(w, h, null, { pixels });
    const r1 = await m.rcaUnsharpMaskAsync(ctx, w, h, 0.4, 1);
    const r2 = await m.rcaUnsharpMaskAsync(ctx, w, h, 0.4, 1);
    checkEq('fetch-fail-via', r1.via, 'sync');
    checkEq('fetch-fail-applied', r1.applied, true);
    checkEq('fetch-fail-caches-verdict', r2.via, 'sync');
    checkEq('fetch-fail-fetches-once', env.stats.fetches, 1);
    checkEq('fetch-fail-no-worker-built', env.stats.workers, 0);
  }
  {
    const env = makeWorkerEnv({ textFails: true });
    const m = loadMinimax(env.globals);
    const { ctx } = makeFakeCtx(4, 4, 'gray');
    checkEq('throwing-fetch-falls-back', (await m.rcaUnsharpMaskAsync(ctx, 4, 4, 0.4, 1)).via, 'sync');
    checkEq('throwing-fetch-no-worker', env.stats.workers, 0);
  }
  {
    // an unusable script body (no onmessage) is treated as "no worker here"
    const env = makeWorkerEnv({ glue: '// nothing useful\n' });
    const m = loadMinimax(env.globals);
    const { ctx } = makeFakeCtx(4, 4, 'gray');
    checkEq('bad-glue-falls-back', (await m.rcaUnsharpMaskAsync(ctx, 4, 4, 0.4, 1)).via, 'sync');
    checkEq('bad-glue-no-worker-built', env.stats.workers, 0);
  }

  // (19) reset re-arms the build
  {
    const env = makeWorkerEnv();
    const m = loadMinimax(env.globals);
    const { ctx } = makeFakeCtx(5, 5, 'gray');
    checkEq('reset-first-worker', (await m.rcaUnsharpMaskAsync(ctx, 5, 5, 0.4, 1)).via, 'worker');
    m.rcaSharpenWorkerReset();
    checkEq('reset-stats-terminated', env.stats.terminated, 1);
    checkEq('reset-revokes-blob-url', env.stats.revoked, 1);
    checkEq('reset-rebuilds', (await m.rcaUnsharpMaskAsync(ctx, 5, 5, 0.4, 1)).via, 'worker');
    checkEq('reset-refetched-glue', env.stats.fetches, 2);
  }

  // (20) the worker URL follows minimax.js's own script tag
  {
    const env = makeWorkerEnv();
    const m = loadMinimax(Object.assign({}, env.globals, {
      document: {
        querySelectorAll: () => [{ src: 'https://x.test/static/js/minimax.js?v=7' }],
      },
    }));
    const { ctx } = makeFakeCtx(4, 4, 'gray');
    await m.rcaUnsharpMaskAsync(ctx, 4, 4, 0.4, 1);
    checkEq('url-derived-from-script-tag', env.stats.lastUrl, 'https://x.test/static/js/sharpen_worker.js?v=7');
  }
  {
    const env = makeWorkerEnv();
    const m = loadMinimax(env.globals);
    const { ctx } = makeFakeCtx(4, 4, 'gray');
    await m.rcaUnsharpMaskAsync(ctx, 4, 4, 0.4, 1);
    checkEq('url-default-relative', env.stats.lastUrl, 'js/sharpen_worker.js');
  }

  // (21) a bare ImageData target works through the worker too (no putImageData)
  {
    const env = makeWorkerEnv();
    const m = loadMinimax(env.globals);
    const img = makeImageData(9, 3, 'color');
    const before = new Uint8ClampedArray(img.data);
    const res = await m.rcaUnsharpMaskAsync(img, 9, 3, 0.4, 1);
    checkEq('imagedata-async-via', res.via, 'worker');
    check('imagedata-async-in-place',
      bytesEqual(legacyUnsharp3x3(new Uint8ClampedArray(before), 9, 3, 0.4, 1), img.data));
  }

  // (22) unusable targets resolve instead of rejecting
  {
    const env = makeWorkerEnv();
    const m = loadMinimax(env.globals);
    let rejected = false;
    const p = m.rcaUnsharpMaskAsync({}, 4, 4, 0.4, 1).then((r) => r, () => { rejected = true; });
    const res = await p;
    checkEq('async-never-rejects', rejected, false);
    checkEq('async-bad-target-applied', res.applied, false);
    checkEq('async-bad-target-via', res.via, 'none');
    const tainted = makeFakeCtx(4, 4, 'gray', { throwOnGet: true });
    const t = await m.rcaUnsharpMaskAsync(tainted.ctx, 4, 4, 0.4, 1);
    checkEq('async-tainted-applied', t.applied, false);
    checkEq('async-tainted-via', t.via, 'none');
    let bigReads = 0;
    const big = {
      getImageData: () => { bigReads += 1; return { width: 4001, height: 4000, data: new Uint8ClampedArray(4) }; },
      putImageData: () => {},
    };
    const b = await m.rcaUnsharpMaskAsync(big, 4001, 4000, 0.4, 1);
    checkEq('async-oversize-applied', b.applied, false);
    checkEq('async-oversize-via', b.via, 'none');
    checkEq('async-oversize-skips-the-canvas', bigReads, 0);
    check('async-oversize-reports-why', /too large/.test(b.error || ''), b.error);
    const b2 = await m.rcaUnsharpMaskAsync(big, 4001, 4000, 0.4, 1, { radius: 1 });
    checkEq('async-oversize-no-worker-job', b2.applied, false);
    checkEq('async-oversize-posted-nothing', env.posted.length, 0);
    checkEq('async-oversize-built-no-worker', env.stats.workers, 0);
  }
}

// ---------------------------------------------------------------------------
// 23-27: the worker script itself (composition + single source + behaviour)
// ---------------------------------------------------------------------------
async function workerFileTests() {
  const glue = fs.readFileSync(WORKER, 'utf8');
  const m = loadMinimax();

  // (23) the glue carries NO copy of the math — the single-source rule
  check('glue-has-no-kernel-definition', !/function\s+rcaUnsharpMaskCore/.test(glue));
  check('glue-has-no-blur-math', !/Float32Array|s \/ 9|blur\[/.test(glue));
  check('glue-wires-onmessage', /self\.onmessage\s*=/.test(glue));
  check('glue-transfers-dst', /\[dst\.buffer\]/.test(glue));

  // (24) the composed script
  const composed = m.rcaSharpenWorkerSource(glue);
  check('composed-starts-strict', composed.indexOf("'use strict';") === 0);
  check('composed-carries-live-pixel-cap', composed.indexOf('RCA_UNSHARP_MAX_PIXELS = 16000000') !== -1);
  check('composed-carries-live-radius-cap', composed.indexOf('RCA_UNSHARP_MAX_RADIUS = 8') !== -1);
  checkEq('composed-defines-kernel-exactly-once',
    (composed.match(/function rcaUnsharpMaskCore/g) || []).length, 1);
  check('composed-ends-with-glue', composed.indexOf(glue.trimRight()) !== -1);
  check('composed-kernel-text-comes-from-minimax-source',
    composed.indexOf(String(m.rcaUnsharpMaskCore)) !== -1);
  check('composed-rejects-non-string-glue', typeof m.rcaSharpenWorkerSource(undefined) === 'string');

  // (25) EXECUTE the production composition in a bare worker scope
  {
    const scope = { console, setTimeout, clearTimeout, postMessage: () => {} };
    scope.self = scope;
    vm.createContext(scope);
    vm.runInContext(composed, scope, { filename: 'sharpen_worker.js (composed)' });
    const handler = scope.onmessage;
    check('composed-handler-is-a-function', typeof handler === 'function');
    const w = 15, h = 11;
    const src = makeImage(w, h, 'alpha');
    let reply = null;
    scope.postMessage = (msg, transfer) => { reply = { msg, transfer }; };
    handler({ data: { id: 7, src: new Uint8ClampedArray(src), opts: { width: w, height: h, radius: 1, percent: 0.4, threshold: 1 } } });
    check('composed-job-ok', reply && reply.msg.ok === true, reply && reply.msg.error);
    check('composed-job-id-echoed', reply && reply.msg.id === 7);
    const ref = legacyUnsharp3x3(new Uint8ClampedArray(src), w, h, 0.4, 1);
    check('composed-job-pixels-parity', reply && bytesEqual(ref, reply.msg.dst),
      reply ? firstDiff(ref, reply.msg.dst) : 'no reply');
    check('composed-job-transfers-dst', reply && reply.transfer.length === 1 && reply.transfer[0] === reply.msg.dst.buffer);
    // oversize inside the worker: the caps really are in scope
    let big = null;
    scope.postMessage = (msg, transfer) => { big = { msg, transfer }; };
    handler({ data: { id: 8, src: new Uint8ClampedArray(4), opts: { width: 4001, height: 4000 } } });
    check('composed-oversize-rejects', big && big.msg.ok === false && /too large/.test(big.msg.error || ''), big && big.msg.error);
    // missing input: an error reply, never a throw out of the handler
    let bad = null;
    scope.postMessage = (msg) => { bad = msg; };
    handler({ data: { id: 9 } });
    check('composed-garbage-job-replies-error', bad && bad.ok === false && bad.id === 9, bad && bad.error);
  }

  // (26) glue alone, with no kernel and a failing importScripts -> honest error
  {
    const scope = { console, setTimeout, clearTimeout, postMessage: () => {}, importScripts: () => { throw new Error('not allowed'); } };
    scope.self = scope;
    vm.createContext(scope);
    vm.runInContext(glue, scope, { filename: 'js/sharpen_worker.js' });
    let reply = null;
    scope.postMessage = (msg) => { reply = msg; };
    scope.onmessage({ data: { id: 1, src: new Uint8ClampedArray(4), opts: { width: 1, height: 1 } } });
    check('bare-glue-reports-missing-kernel', reply && reply.ok === false && /kernel unavailable/.test(reply.error || ''), reply && reply.error);
  }

  // (27) the call site in minimax.js is the async one, with legacy knobs
  {
    const src = fs.readFileSync(MINIMAX, 'utf8');
    check('enhance-await-wired', src.indexOf('await rcaUnsharpMaskAsync(ctx, nw, nh, 0.4, 1, { radius: 1 })') !== -1);
    check('enhance-abort-checked-after-await',
      src.indexOf("if (signal && signal.aborted) { fail('aborted'); return; }", src.indexOf('await rcaUnsharpMaskAsync')) !== -1);
    check('async-handler-cannot-strand-promise',
      src.indexOf('Promise.resolve().then(onImgLoaded).catch') !== -1);
    check('legacy-sync-name-still-exported', typeof m.rcaUnsharpMask === 'function');
    check('no-second-copy-of-the-loop-in-minimax',
      (src.match(/s \+= at\(|blur\[\(y \* w \+ x\)/g) || []).length === 0);
  }
}

// ---------------------------------------------------------------------------
// 29-32: FE-FIX-2026-09-21 — the per-run failure budget and the synchronous
// `new Worker(url)` constructor-throw path (audit MED #1 and #2).
// ---------------------------------------------------------------------------
async function budgetAndConstructTests() {
  // (29) constructor throws ONCE (quota / transient CSP): the rejected
  // promise must not be cached — the next call rebuilds and succeeds, the
  // Blob URL is revoked, and the never-rejects contract holds.
  {
    const env = makeWorkerEnv({ throwOnConstruct: true });
    const m = loadMinimax(env.globals);
    const w = 8, h = 6;
    const pixels = makeImage(w, h, 'gray');
    const { ctx, state } = makeFakeCtx(w, h, null, { pixels });
    const ref = legacyUnsharp3x3(new Uint8ClampedArray(pixels), w, h, 0.4, 1);
    const r1 = await m.rcaUnsharpMaskAsync(ctx, w, h, 0.4, 1);
    checkEq('ctor-throw-via', r1.via, 'sync');
    checkEq('ctor-throw-applied', r1.applied, true);
    check('ctor-throw-pixels-parity', bytesEqual(ref, state.last.data),
      state.last ? firstDiff(ref, state.last.data) : 'putImageData never called');
    checkEq('ctor-throw-counted', env.stats.constructThrows, 1);
    checkEq('ctor-throw-no-worker-live', env.stats.workers, 0);
    checkEq('ctor-throw-revokes-blob-url', env.stats.revoked, 1);
    // the failure clears (transient quota)…
    env.opts.throwOnConstruct = false;
    const r2 = await m.rcaUnsharpMaskAsync(ctx, w, h, 0.4, 1);
    checkEq('ctor-throw-not-poisoned-via', r2.via, 'worker');
    checkEq('ctor-throw-rebuilds', env.stats.workers, 1);
    checkEq('ctor-throw-glue-fetched-once', env.stats.fetches, 1);
    check('ctor-throw-worker-pixels-parity', bytesEqual(ref, state.last.data),
      firstDiff(ref, state.last.data));
  }

  // (30) constructor throws PERSISTENTLY: build attempts stop at the budget,
  // each throw revokes its own Blob URL, and the next run retries once.
  {
    const env = makeWorkerEnv({ throwOnConstruct: true });
    const m = loadMinimax(env.globals);
    const w = 6, h = 5;
    const pixels = makeImage(w, h, 'ramp');
    const { ctx, state } = makeFakeCtx(w, h, null, { pixels });
    const ref = legacyUnsharp3x3(new Uint8ClampedArray(pixels), w, h, 0.4, 1);
    const r1 = await m.rcaUnsharpMaskAsync(ctx, w, h, 0.4, 1);   // throw 1
    const r2 = await m.rcaUnsharpMaskAsync(ctx, w, h, 0.4, 1);   // throw 2 -> budget spent
    const r3 = await m.rcaUnsharpMaskAsync(ctx, w, h, 0.4, 1);   // pinned for the run
    checkEq('ctor-throw-pin-via', r3.via, 'sync');
    checkEq('ctor-throw-pin-applied', r3.applied, true);
    check('ctor-throw-pin-pixels-parity', bytesEqual(ref, state.last.data));
    checkEq('ctor-throw-budget-stops-retrying', env.stats.constructThrows, 2);
    checkEq('ctor-throw-revokes-every-url', env.stats.revoked, 2);
    checkEq('ctor-throw-glue-cached-across-retries', env.stats.fetches, 1);
    m.rcaSharpenRunBegin();                                        // new run
    const r4 = await m.rcaUnsharpMaskAsync(ctx, w, h, 0.4, 1);
    checkEq('run-begin-retries-ctor-throw', env.stats.constructThrows, 3);
    checkEq('run-begin-retry-still-sync-via', r4.via, 'sync');
    checkEq('run-begin-retry-revokes-too', env.stats.revoked, 3);
  }

  // (31) REAL worker failures (onerror): 2 consecutive failures still flip
  // the path to sync for the current run, rcaSharpenRunBegin re-arms for the
  // next one, and a worker that recovers mid-run is adopted again.
  {
    const env = makeWorkerEnv({ deadWorker: true });
    const m = loadMinimax(env.globals);
    const { ctx } = makeFakeCtx(6, 4, 'gray');
    await m.rcaUnsharpMaskAsync(ctx, 6, 4, 0.4, 1);   // fail 1 (rebuilds)
    await m.rcaUnsharpMaskAsync(ctx, 6, 4, 0.4, 1);   // fail 2 -> pinned
    await m.rcaUnsharpMaskAsync(ctx, 6, 4, 0.4, 1);   // pinned: no rebuild
    checkEq('budget-pin-holds-at-two', env.stats.workers, 2);
    m.rcaSharpenRunBegin();
    const r4 = await m.rcaUnsharpMaskAsync(ctx, 6, 4, 0.4, 1);
    checkEq('run-begin-rebuilds-dead-worker', env.stats.workers, 3);
    checkEq('run-begin-fail-still-sync-via', r4.via, 'sync');
    // the ONE failure so far must not condemn a worker that now behaves:
    env.opts.deadWorker = false;
    const r5 = await m.rcaUnsharpMaskAsync(ctx, 6, 4, 0.4, 1);
    checkEq('recovered-worker-adopted-mid-run', r5.via, 'worker');
    checkEq('recovered-worker-rebuilt-once-more', env.stats.workers, 4);
  }

  // (32) timeout semantics with fake timers: a worker that times out once
  // but later WORKS is not condemned — the success clears the budget, so a
  // second timeout much later is streak-failure #1, not the pin.
  {
    const timers = makeFakeTimers();
    const env = makeWorkerEnv({ silentWorker: true });
    const m = loadMinimax(Object.assign({}, env.globals, {
      setTimeout: timers.setTimeout, clearTimeout: timers.clearTimeout,
    }));
    const w = 7, h = 5;
    const pixels = makeImage(w, h, 'color');
    const { ctx, state } = makeFakeCtx(w, h, null, { pixels });
    const ref = legacyUnsharp3x3(new Uint8ClampedArray(pixels), w, h, 0.4, 1);
    const p1 = m.rcaUnsharpMaskAsync(ctx, w, h, 0.4, 1);
    await tick();                                   // let the job get posted
    checkEq('job-arms-exactly-one-timer', timers.pending(), 1);
    checkEq('job-timeout-is-30s', timers.fireLatest(), 30000);
    const r1 = await p1;
    checkEq('timeout-via', r1.via, 'sync');
    checkEq('timeout-applied', r1.applied, true);
    check('timeout-pixels-parity', bytesEqual(ref, state.last.data));
    // recovered worker is adopted and SUCCEEDS (which clears the budget)…
    env.opts.silentWorker = false;
    const r2 = await m.rcaUnsharpMaskAsync(ctx, w, h, 0.4, 1);
    checkEq('timeout-then-recovered-via', r2.via, 'worker');
    check('recovered-pixel-roundtrip-parity', bytesEqual(ref, state.last.data),
      firstDiff(ref, state.last.data));
    checkEq('success-clears-job-timer', timers.pending(), 0);
    // …so a SECOND timeout later is failure #1 of a new streak…
    const p3 = m.rcaUnsharpMaskAsync(ctx, w, h, 0.4, 1);
    env.opts.silentWorker = true;
    await tick();
    timers.fireLatest();
    const r3 = await p3;
    checkEq('second-timeout-via', r3.via, 'sync');
    // …not the pin: without the success-clearing fix the budget (2) would
    // already be spent here and this call would go 'sync' forever.
    env.opts.silentWorker = false;
    const r4 = await m.rcaUnsharpMaskAsync(ctx, w, h, 0.4, 1);
    checkEq('not-condemned-after-later-success', r4.via, 'worker');
    check('final-worker-pixels-parity', bytesEqual(ref, state.last.data),
      firstDiff(ref, state.last.data));
  }

  // (33) wiring: the per-run re-arm runs where the production sharpen cycle
  // starts — rcaLoadAndMaybeResize's enhance branch, before the sharpen.
  {
    const src = fs.readFileSync(MINIMAX, 'utf8');
    const iEnh = src.indexOf('if (opts.enhance) {');
    const iRun = src.indexOf('rcaSharpenRunBegin();', iEnh);
    const iAwait = src.indexOf('await rcaUnsharpMaskAsync');
    check('run-begin-wired-into-enhance-cycle',
      iEnh !== -1 && iRun !== -1 && iRun < iAwait,
      JSON.stringify({ iEnh, iRun, iAwait }));
  }
}

// ---------------------------------------------------------------------------
// 34: performance sanity — the kernel is the same work, the thread is not
// ---------------------------------------------------------------------------
async function perfSanity() {
  const m = loadMinimax();
  const w = 128, h = 128;
  const src = makeImage(w, h, 'color');
  const t0 = Date.now();
  m.rcaUnsharpMaskCore(src, { width: w, height: h, percent: 0.4, threshold: 1 });
  const dt = Date.now() - t0;
  check('kernel-16kpx-under-a-second', dt < 1000, dt + 'ms');
}

// ---------------------------------------------------------------------------
async function main() {
  await kernelTests();
  await syncWrapperTests();
  await asyncTests();
  await workerFileTests();
  await budgetAndConstructTests();
  await perfSanity();
  if (failures.length) {
    for (const f of failures) console.log('FAIL', f);
    console.log('tests_sharpen: ' + passed + ' passed, ' + failures.length + ' FAILED');
    process.exitCode = 1;
    return;
  }
  console.log('tests_sharpen: ' + passed + ' checks passed (kernel parity + worker offload + sync fallback)');
}

main();
