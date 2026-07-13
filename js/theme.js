// js/theme.js — three-position theme toggle (system / light / dark).
//
// Persistence: localStorage key 'rca.theme' — values 'system' | 'light' | 'dark'.
// System mode follows prefers-color-scheme and reactively re-themes when the
// OS preference changes (so a user toggling dark mode in OS settings sees
// the app update while it's open).
//
// Loaded synchronously before app.js so the initial paint uses the correct
// theme attribute (avoids a flash-of-wrong-theme on dark-mode reload).
'use strict';

(function () {
  var STORAGE_KEY = 'rca.theme';
  var ALLOWED = ['system', 'light', 'dark'];

  function readStored() {
    try {
      var v = window.localStorage.getItem(STORAGE_KEY);
      if (!v) return 'system';
      return ALLOWED.indexOf(v) !== -1 ? v : 'system';
    } catch (_e) {
      return 'system';
    }
  }
  function writeStored(mode) {
    try { window.localStorage.setItem(STORAGE_KEY, mode); }
    catch (_e) { /* private mode / quota — no-op */ }
  }

  var current = readStored();

  function effectiveMode(mode) {
    if (mode === 'dark') return true;
    if (mode === 'light') return false;
    // 'system' → reflect OS preference
    try {
      return !!(window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches);
    } catch (_e) {
      return false;
    }
  }

  function apply() {
    var dark = effectiveMode(current);
    // data-theme is omitted when system+light so prefers-color-scheme wins.
    if (current === 'system' && !dark) {
      document.documentElement.removeAttribute('data-theme');
    } else {
      document.documentElement.setAttribute('data-theme', dark ? 'dark' : 'light');
    }
    // Sync UI toggles (if already wired).
    var btns = document.querySelectorAll('[data-theme-choice]');
    for (var i = 0; i < btns.length; i++) {
      var isActive = btns[i].getAttribute('data-theme-choice') === current;
      btns[i].setAttribute('aria-pressed', isActive ? 'true' : 'false');
    }
  }

  function set(mode) {
    if (ALLOWED.indexOf(mode) === -1) return false;
    current = mode;
    writeStored(mode);
    apply();
    return true;
  }

  function get() { return current; }

  // React to system change only when on 'system' so user's manual override
  // isn't clobbered when they briefly toggle the OS preference.
  var mql = null;
  function wireSystemListener() {
    if (!window.matchMedia) return;
    mql = window.matchMedia('(prefers-color-scheme: dark)');
    var onChange = function () { if (current === 'system') apply(); };
    if (mql.addEventListener) mql.addEventListener('change', onChange);
    else if (mql.addListener) mql.addListener(onChange);  // legacy Safari
  }

  function wireToggleButtons() {
    var btns = document.querySelectorAll('[data-theme-choice]');
    for (var i = 0; i < btns.length; i++) {
      (function (btn) {
        btn.addEventListener('click', function () {
          set(btn.getAttribute('data-theme-choice'));
          // ARIA-pressed already synced inside apply(); refresh focused state.
        });
      })(btns[i]);
    }
  }

  // First paint happens immediately (document.head is parsed before any
  // body content), then we wire the buttons.
  apply();
  wireSystemListener();
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', wireToggleButtons);
  } else {
    wireToggleButtons();
  }

  window.rcaTheme = { set: set, get: get, apply: apply };
})();
