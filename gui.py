"""Range Chart Analyzer - Tkinter desktop GUI.

A native desktop frontend over the shared rca_core. Runs the MiniMax call
in a background thread (no CORS, since this is server-side urllib), shows
the four result tables in tabbed Treeviews, and supports copy / CSV / JSON
export plus Chinese / English / Japanese UI switching.

Run:  python gui.py
Requires: Python 3.9+ with tkinter (bundled). Pillow optional (preview +
image downscale); without it the app still works but shows no thumbnail.
"""

from __future__ import annotations

import json
import logging
import os
import concurrent.futures
import queue
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

# Bug-12 fix: a module-level logger so the dozens of bare `except
# Exception:` blocks below have somewhere to report what they swallowed.
# Without this, a button silently failing leaves no trace and the user
# has no idea why "nothing happened". The logger writes to stderr by
# default; redirect via the standard logging config if you want a file.
log = logging.getLogger("rca.gui")

# UI-Mod-2: optional Windows 11 Fluent theme (sv_ttk). If unavailable,
# fall back to ttk's default clam theme.
try:
    import sv_ttk
    _HAS_SVTTK = True
except ImportError:
    sv_ttk = None
    _HAS_SVTTK = False

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from rca_core import (  # noqa: E402
    TABLE_CONFIGS,
    Translator,
    build_table_export,
    extract,
    get_configs_for_result,
    load_image_b64,
    to_csv,
    to_tsv,
)
from rca_core.aggregate import (
    COLUMNAR_SECTION_SCHEMA,
    RANGE_CHART_SCHEMA,
    SCHEMA_BY_MODE,
    merge_results,
)
from rca_core.extractor import (  # noqa: E402
    DEFAULT_ENDPOINT,
    DEFAULT_MAX_TOKENS,
    DEFAULT_MAX_EDGE,
    DEFAULT_MODEL,
    ExtractResult,
    clamp_max_tokens,
)
from rca_core import ProviderStore  # noqa: E402
from rca_core.llm import ApiFormat, LlmProvider, PROVIDER_PRESETS  # noqa: E402

try:
    from PIL import Image, ImageTk  # type: ignore
    HAS_PIL = True
except Exception:
    HAS_PIL = False

CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".range_chart_analyzer.json")

# UI-Mod-1: 极简现代风 Palette (Morandi / "性冷淡"色系).
# 深层石板灰主按钮 + 蓝灰点缀 + 冷白背景. 配合 sv_ttk 主题后整体
# 呈现 Windows 11 Fluent + 莫兰迪柔感的高级感.
COLORS = {
    "primary": "#0f172a",        # 极深石板灰（几乎黑），主按钮底色
    "primary_hover": "#334155",  # 悬浮时的深灰
    "primary_active": "#1e293b", # 按下时
    "accent": "#2563eb",         # 亮蓝强调色（链接/小图标）
    "accent_hover": "#1d4ed8",
    "accent_active": "#1e40af",
    "bg": "#f8fafc",             # 整体背景（带一点冷调的白）
    "surface": "#ffffff",        # 卡片表面（纯白）
    "card": "#ffffff",
    "card_border": "#e2e8f0",    # 更浅更柔和的边框
    # Foreground
    "text": "#334155",           # 正文（不要用纯黑）
    "heading": "#0f172a",        # 标题
    "muted": "#64748b",          # 辅助文字
    "muted_alt": "#94a3b8",
    # Borders & fields
    "border": "#cbd5e1",         # 默认边框
    "border_focus": "#3b82f6",   # 聚焦时的蓝色
    "field_bg": "#f1f5f9",       # 输入框背景（极浅灰）
    "field_disabled": "#f8fafc",
    # Semantic
    "success": "#10b981", "success_soft": "#d1fae5",
    "warning": "#f59e0b", "warning_soft": "#fef3c7",
    "danger": "#ef4444", "danger_soft": "#fee2e2",
    "info": "#3b82f6", "info_soft": "#eff6ff",
    # States
    "row_alt": "#f8fafc",        # 斑马线极淡
    "row_hover": "#f1f5f9",      # 表格悬浮
    "row_low": "#fffbeb",
    "primary_soft": "#f1f5f9",
    "sel": "#e2e8f0",            # 表格选中（柔和灰，不要瞎眼蓝）
    # CTA
    "ghost": "#64748b",
    "ghost_hover": "#f1f5f9",
}

# Font family with graceful fallback; "Segoe UI" on Windows matches the skill's
# Inter recommendation (clean, functional, neutral) closest among system fonts.
FONT_FAMILY = "Segoe UI"
# UI-Mod-4: more whitespace for "breathing room" (per expert review).
# Same-field / same-group / section gap all bumped.
PAD_S = 14   # same-field
PAD_M = 24   # same-group
PAD_L = 36   # section gap


def load_config() -> dict:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(cfg: dict) -> None:
    # Atomic write: write to a sibling tmp file, fsync, then os.replace. Mirrors
    # ProviderStore.save() so a crash mid-write can never truncate the config
    # file (which used to lose the API key + settings together).
    try:
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
            f.flush()
            try:
                os.fsync(f.fileno())
            except (AttributeError, OSError):
                pass
        os.replace(tmp, CONFIG_PATH)
    except Exception:
        pass


class ScrollableFrame:
    """Canvas-backed vertical scroll container for ttk children.

    Exposes:
      - .container : the outer ttk.Frame placed in the parent grid (use this for grid())
      - .inner     : the inner ttk.Frame that children should pack into
    """

    def __init__(self, parent, bg=None, border=0, padx=0, pady=0):
        bg = bg or COLORS["bg"]
        canvas = tk.Canvas(parent, bg=bg, highlightthickness=0, borderwidth=0)
        vsb = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        canvas.grid(row=0, column=0, sticky="nsew", padx=padx, pady=pady)
        vsb.grid(row=0, column=1, sticky="ns")
        parent.rowconfigure(0, weight=1)
        parent.columnconfigure(0, weight=1)
        inner = ttk.Frame(canvas, style="Card.TFrame")
        window_id = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(window_id, width=e.width))

        def _on_wheel(event, c=canvas):
            # Cross-platform wheel/trackpad scrolling.
            delta = 0
            if hasattr(event, "delta") and event.delta:
                delta = -1 if event.delta > 0 else 1
            elif event.num == 4:
                delta = -1
            elif event.num == 5:
                delta = 1
            if delta:
                c.yview_scroll(delta, "units")

        # MouseWheel is only on Windows/macOS; X11 uses Button-4/5.
        canvas.bind_all("<MouseWheel>", _on_wheel)
        canvas.bind_all("<Button-4>", _on_wheel)
        canvas.bind_all("<Button-5>", _on_wheel)

        self.container = parent
        self.canvas = canvas
        self.vsb = vsb
        self.inner = inner


class ToastNotification:
    """A small, transient notification window that mimics cc-switch toasts.

    Auto-dismisses after 5 s or on click. Non-modal, non-blocking.
    """

    ACTIVE = []

    def __init__(self, parent, message, kind="info"):
        self.win = tk.Toplevel(parent)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        bg = {"success": "#d1fae5", "error": "#fee2e2"}.get(kind, "#e0e7ff")
        fg = {"success": "#065f46", "error": "#991b1b"}.get(kind, "#3730a3")
        frame = tk.Frame(self.win, bg=bg, bd=0, highlightthickness=0)
        frame.pack(fill="both", expand=True)
        self.lbl = tk.Label(frame, text=message, bg=bg, fg=fg,
                            font=("Segoe UI", 10, "bold"),
                            padx=14, pady=8, anchor="w", justify="left")
        self.lbl.pack(side="left", fill="both", expand=True)
        btn_x = tk.Label(frame, text="✕", bg=bg, fg=fg,
                          font=("Segoe UI", 10), padx=8, cursor="hand2")
        btn_x.pack(side="right")
        btn_x.bind("<Button-1>", lambda _e: self.dismiss())
        self.lbl.bind("<Button-1>", lambda _e: self.dismiss())
        self._reposition()
        ToastNotification.ACTIVE.append(self)
        self.win.after(5000, self.dismiss)

    def _reposition(self):
        self.win.update_idletasks()
        sw = self.win.winfo_screenwidth()
        sh = self.win.winfo_screenheight()
        # Stack new toasts above existing ones; clamp to screen so a
        # long stack of toasts never falls off the bottom of the screen.
        y_offset = 24 + len(ToastNotification.ACTIVE) * 52
        max_y = max(24, sh - self.win.winfo_reqheight() - 24)
        y_offset = min(y_offset, max_y)
        self.win.geometry(f"+{sw - self.win.winfo_reqwidth() - 24}+{y_offset}")

    def dismiss(self):
        if self in ToastNotification.ACTIVE:
            ToastNotification.ACTIVE.remove(self)
        try:
            self.win.destroy()
        except Exception:
            pass

class RangeChartApp:
    """Top-level Tkinter app. The class body is intentionally re-opened
    below (after ToastNotification) so helpers + lifecycle methods all
    belong to this single class — without that reopen, the AST lumps
    everything into ToastNotification because the indents align."""

    def _bind_entry_focus_animation(self, entry):
        """Bind focus-in / focus-out events to give a vscode-like focus ring
        on ttK.Entry widgets (which do not natively support focus styling in
        ttk without re-styling the whole theme)."""
        def on_focus_in(_e):
            try:
                entry.configure(highlightthickness=2,
                                highlightbackground=COLORS["border_focus"])
            except Exception:
                pass
        def on_focus_out(_e):
            try:
                entry.configure(highlightthickness=1,
                                highlightbackground=COLORS["border"])
            except Exception:
                pass
        entry.bind("<FocusIn>", on_focus_in)
        entry.bind("<FocusOut>", on_focus_out)
    def _bind_button_hover(self, button, kind="primary"):
        """Add smooth cc-switch-style hover / pressed feedback to a button.

        Themed ttk buttons swap their style name on Enter / Leave so the
        engine re-renders with the right palette. The hover style bumping
        is deliberately small so the UI doesn't feel jumpy.
        """
        hover = self._style_for(kind, "hover")
        default = self._style_for(kind, "default")
        # Bind pre-computed style objects (not lambdas calling _style_for at
        # event-time, which would crash because ttk.Button.configure(style=...)
        # expects a string not a function reference).
        button.bind("<Enter>", lambda _e, b=button, s=hover: b.configure(style=s))
        button.bind("<Leave>", lambda _e, b=button, s=default: b.configure(style=s))

    @staticmethod
    def _style_for(kind, state):
        """Return a ttk style name for a button kind + state."""
        # ttk styles bound in _build_style — reuse existing ones; this is a
        # placeholder to avoid style-name sprawl. In practice we just
        # return the style set at creation.
        if kind == "primary":
            return "Primary.TButton"
        if kind == "secondary":
            return "TButton"
        return "Tertiary.TButton"
    def _bind_group_hover(self, frame):
        """Give a settings-group frame a subtle primary-tinted border on hover."""
        off_color = frame.cget("highlightbackground") if frame.cget("highlightbackground") else COLORS["card_border"]
        on_color = COLORS["primary"]
        frame.bind("<Enter>",
                   lambda _e, f=frame, c=on_color:
                   f.configure(highlightbackground=c, highlightthickness=1))
        frame.bind("<Leave>",
                   lambda _e, f=frame, c=off_color:
                   f.configure(highlightbackground=c,
                               highlightthickness=1 if f.cget("bg") == COLORS["primary_soft"] else 0))
    def _bind_dirty_traces(self):
        """Watch the settings vars; if user changes a value, mark dirty."""
        def _w(name):
            return lambda *_: self._mark_dirty()
        for v in (self.var_key, self.var_endpoint, self.var_model,
                   self.var_maxtok, self.var_maxedge, self.var_runs,
                   self.var_chartlang, self.var_chart_type, self.var_remember):
            try:
                v.trace_add("write", _w(str(v)))
            except Exception:
                pass

    def _dirty_lock_run(self, fn):
        """Run `fn` with `_dirty_lock=True` so programmatic var writes (init,
        language switch, widget restoration) don't flip the Save button.
        H2."""
        prev = self._dirty_lock
        self._dirty_lock = True
        try:
            return fn()
        finally:
            self._dirty_lock = prev

    def _mark_dirty(self):
        if self._dirty_lock:
            return
        self._dirty = True
        # Highlight Save button when there are unsaved changes.
        try:
            self.btn_save.configure(text=self._t("settings.save") + " •")
        except Exception:
            pass

    def _clear_dirty(self):
        self._dirty = False
        try:
            self.btn_save.configure(text=self._t("settings.save"))
        except Exception:
            pass



    def __init__(self, root: tk.Tk):
        self.root = root
        self.cfg = load_config()
        self.tr = Translator(self.cfg.get("lang", "zh"))
        self.image_path = None
        self.image_b64 = None
        self.media_type = None
        self.thumb = None  # keep a ref so the image is not GC'd
        self.result = None
        self.raw_text = ""
        self.busy = False
        self.msg_queue: queue.Queue = queue.Queue()
        self._poll_after_id = None  # _poll_queue timer handle (cancel on close)

        # tk variables
        self.var_key = tk.StringVar(value=self.cfg.get("api_key", ""))
        self.var_endpoint = tk.StringVar(value=self.cfg.get("endpoint", DEFAULT_ENDPOINT))
        self.var_model = tk.StringVar(value=self.cfg.get("model", DEFAULT_MODEL))
        self.var_maxtok = tk.StringVar(value=str(self.cfg.get("max_tokens", DEFAULT_MAX_TOKENS)))
        self.var_maxedge = tk.StringVar(value=str(self.cfg.get("max_edge", DEFAULT_MAX_EDGE)))
        self.var_runs = tk.StringVar(value=str(self.cfg.get("runs", 1)))
        self.var_chartlang = tk.StringVar(value=self.cfg.get("chart_lang", "auto"))
        self.var_chart_type = tk.StringVar(value=self.cfg.get("chart_type", "auto"))
        # Display-side vars for the comboboxes (so the combobox can show a
        # localized label while var_chart_type / var_chartlang hold the code).
        self._cmb_chart_type_disp = tk.StringVar(value="Auto")
        self._cmb_chartlang_disp = tk.StringVar(value="Auto")
        self.var_remember = tk.BooleanVar(value=bool(self.cfg.get("remember", True)))
        self.var_show_key = tk.BooleanVar(value=False)
        self.var_status = tk.StringVar(value="")
        self.var_lang = tk.StringVar(value=self.tr.lang)
        # Track unsaved changes to provider/settings so the Save button
        # can be highlighted (cc-switch "modified" treatment).
        self._dirty = False
        self._dirty_lock = False  # prevent var-trace loops
        self._bind_dirty_traces()

        # widget registry for i18n refresh: (widget, kind, key)
        self._i18n = []

        self._build_style()
        self._build_ui()
        self.retranslate()
        # Global Ctrl+V to paste a screenshot from the OS clipboard.
        self.root.bind("<Control-v>", self._paste_from_clipboard, add="+")
        self.root.after(120, self._poll_queue)

    # ---------- style ----------
    def _build_style(self):
        self.root.configure(bg=COLORS["bg"])
        style = ttk.Style()
        # UI-Mod-2: prefer sv_ttk (Windows 11 Fluent) when available; fall back
        # to ttk's built-in clam theme otherwise. Both branches are unit-
        # tested by the absence/presence of sv_ttk.
        if _HAS_SVTTK:
            try:
                sv_ttk.set_theme("light")
                # Re-acquire the Style object because sv_ttk rebuilt it.
                style = ttk.Style()
            except Exception:
                try:
                    style.theme_use("clam")
                except Exception:
                    pass
        else:
            try:
                style.theme_use("clam")
            except Exception:
                pass
        F = FONT_FAMILY

        # GUI-Mod2: when sv_ttk is loaded its Fluent theme already
        # provides button / input / combobox / notebook / entry colors
        # and modern flat shapes. We only override the elements sv_ttk
        # does-not style well (Treeview / Notebook tab indicator). When
        # sv_ttk is missing, fall back to applying our custom Morandi palette.
        if not _HAS_SVTTK:
            style.configure("TFrame", background=COLORS["bg"])
            style.configure("Card.TFrame", background=COLORS["card"])

            # --- type hierarchy: larger, more readable, better contrast.
            # Modern apps use 13-14px body, not 10-11px. We bump everything ~2pt.
            style.configure("TLabel", background=COLORS["bg"], foreground=COLORS["text"],
                            font=(F, 11))
            style.configure("Card.TLabel", background=COLORS["card"], foreground=COLORS["text"],
                            font=(F, 11))
            style.configure("Muted.TLabel", background=COLORS["card"],
                            foreground=COLORS["muted"], font=(F, 10))
            style.configure("Title.TLabel", background=COLORS["bg"],
                            foreground=COLORS["heading"], font=(F, 16, "bold"))
            style.configure("Sub.TLabel", background=COLORS["bg"],
                            foreground=COLORS["muted"], font=(F, 11))
            style.configure("Section.TLabel", background=COLORS["card"],
                            foreground=COLORS["heading"], font=(F, 14, "bold"))
            # vscode-style group headers: small uppercase muted label.
            style.configure("Group.TLabel", background=COLORS["bg"],
                            foreground=COLORS["muted"], font=(F, 10, "bold"))

            # --- secondary / default button: more padding, larger font ---
            style.configure("TButton", font=(F, 11), padding=(16, 9),
                            background=COLORS["card"], foreground=COLORS["primary"],
                            bordercolor=COLORS["border"], relief="flat",
                            focuscolor=COLORS["border_focus"])
            style.map(
                "TButton",
                background=[("pressed", COLORS["row_alt"]), ("active", COLORS["row_hover"]),
                            ("disabled", COLORS["field_disabled"])],
                foreground=[("disabled", COLORS["muted"])],
                bordercolor=[("active", COLORS["primary"])],
                relief=[("pressed", "flat"), ("!pressed", "flat")],
            )

            # --- primary CTA button (Extract): emerald accent, white text ---
            style.configure("Primary.TButton", font=(F, 12, "bold"), padding=(18, 11),
                            background=COLORS["accent"], foreground="#ffffff",
                            bordercolor=COLORS["accent"], relief="flat",
                            focuscolor=COLORS["accent_active"])
            style.map(
                "Primary.TButton",
                background=[("pressed", COLORS["accent_active"]),
                            ("active", COLORS["accent_hover"]),
                            ("disabled", "#9ca3af")],
                foreground=[("disabled", "#e5e7eb")],
                bordercolor=[("active", COLORS["accent_hover"])],
            )

            # --- small secondary button ---
            style.configure("Small.TButton", font=(F, 10), padding=(12, 7),
                            background=COLORS["card"], foreground=COLORS["primary"],
                            bordercolor=COLORS["border"], relief="flat")
            style.map(
                "Small.TButton",
                background=[("pressed", COLORS["row_alt"]), ("active", COLORS["row_hover"]),
                            ("disabled", COLORS["field_disabled"])],
                foreground=[("disabled", COLORS["muted"])],
            )

            # --- inputs: subtle border, navy focus ring, disabled state ---
            style.configure("TEntry", padding=2, relief="flat",
                            fieldbackground=COLORS["field_bg"], foreground=COLORS["text"],
                            bordercolor=COLORS["border"], insertcolor=COLORS["primary"])
            style.map(
                "TEntry",
                bordercolor=[("focus", COLORS["border_focus"])],
                fieldbackground=[("disabled", COLORS["field_disabled"])],
                lightcolor=[("focus", COLORS["border_focus"])],
                darkcolor=[("focus", COLORS["border_focus"])],
            )
            style.configure("TCombobox", padding=2, relief="flat",
                            fieldbackground=COLORS["field_bg"], foreground=COLORS["text"],
                            bordercolor=COLORS["border"], arrowcolor=COLORS["primary"])
            style.map(
                "TCombobox",
                fieldbackground=[("readonly", COLORS["field_bg"])],
                bordercolor=[("focus", COLORS["border_focus"])],
            )
            style.configure("TSpinbox", padding=2, relief="flat",
                            fieldbackground=COLORS["field_bg"], foreground=COLORS["text"],
                            bordercolor=COLORS["border"], arrowcolor=COLORS["primary"])
            style.map("TSpinbox", bordercolor=[("focus", COLORS["border_focus"])])

            # --- checkbutton / radiobutton on card + page surfaces ---
            style.configure("Card.TCheckbutton", background=COLORS["card"],
                            foreground=COLORS["text"], font=(F, 10), focuscolor=COLORS["card"])
            style.map("Card.TCheckbutton",
                      background=[("active", COLORS["card"])],
                      foreground=[("disabled", COLORS["muted"])])
            style.configure("TRadiobutton", background=COLORS["bg"],
                            foreground=COLORS["text"], font=(F, 10), focuscolor=COLORS["bg"])
            style.map("TRadiobutton", background=[("active", COLORS["bg"])],
                      foreground=[("selected", COLORS["primary"])])

            # --- progress bar in accent ---
            style.configure("TProgressbar", background=COLORS["accent"],
                            troughcolor=COLORS["row_alt"], bordercolor=COLORS["row_alt"],
                            lightcolor=COLORS["accent"], darkcolor=COLORS["accent"])

            # --- Treeview: taller rows, tabular feel, soft-gray heading (Notion-style),
            #     navy selection preserved. UI-Polish Phase 4: header switches from
            #     primary blue to a soft surface so the table reads as content-first
            #     rather than chrome.
            style.configure("Treeview", rowheight=38, font=(F, 11),
                            fieldbackground=COLORS["card"], background=COLORS["card"],
                            foreground=COLORS["text"], bordercolor=COLORS["card_border"],
                            relief="flat")
            style.configure("Treeview.Heading", font=(F, 10, "bold"),
                            background=COLORS["surface"], foreground=COLORS["text"],
                            relief="flat", padding=(10, 8))
            style.map(
                "Treeview.Heading",
                background=[("active", COLORS["row_hover"])],
            )
            style.map(
                "Treeview",
                background=[("selected", COLORS["sel"])],
                foreground=[("selected", "#ffffff")],
            )

            # --- notebook tabs ---
            style.configure("TNotebook", background=COLORS["bg"], borderwidth=0)
            style.configure("TNotebook.Tab", font=(F, 11), padding=(16, 9),
                            background=COLORS["row_alt"], foreground=COLORS["muted"])
            style.map(
                "TNotebook.Tab",
                background=[("selected", COLORS["card"])],
                foreground=[("selected", COLORS["primary"])],
                expand=[("selected", (0, 0, 0, 0))],
            )

            # --- separator ---
            style.configure("TSeparator", background=COLORS["card_border"])

            # --- page-level surfaces ---
            # Subtle bottom border on the header frame so the title block reads
            # as a page-level band rather than free-floating text.
            style.configure("Header.TFrame", background=COLORS["bg"], relief="flat", borderwidth=0)

            # Settings group container.
            style.configure("SettingsGroup.TLabelframe", background=COLORS["card"],
                            bordercolor=COLORS["border"], relief="solid", borderwidth=1)
            style.configure("SettingsGroup.TLabelframe.Label", background=COLORS["card"],
                            foreground=COLORS["primary"], font=(FONT_FAMILY, 10, "bold"))

            # Hover on Treeview rows (apart from selected) so they feel responsive.
            # FIX (style-map conflict): do NOT redefine the ("selected",
            # background) state here — it's already set above (line 543) to
            # COLORS["sel"]. Redefining it to COLORS["primary"] here silently
            # overrode the design intent (later .map() wins) and made every
            # selected row bright blue. Keep only the hover ("active") state.
            style.map("Treeview",
                      background=[("active", COLORS["row_hover"])],)

            # Status bar pieces.
            style.configure("Status.TFrame", background=COLORS["bg"])
            style.configure("Status.TLabel", background=COLORS["bg"],
                            foreground=COLORS["muted"], font=(FONT_FAMILY, 10))


        # Always apply: Treeview (Mac Finder header) + Notebook tabs
        # (modern bottom indicator). sv_ttk's default Treeview is plain;
        # its notebook tab style also benefits from the override.
        style.configure(
            "TNotebook", background=COLORS["bg"], borderwidth=0, padding=0,
            tabmargins=(0, 0, 0, 0),
        )
        style.configure(
            "TNotebook.Tab", font=(FONT_FAMILY, 11), padding=(20, 10),
            background=COLORS["bg"], foreground=COLORS["muted"],
            borderwidth=0, focuscolor=COLORS["bg"],
        )
        style.map(
            "TNotebook.Tab",
            background=[("selected", COLORS["surface"]), ("active", COLORS["row_hover"])],
            foreground=[("selected", COLORS["heading"]), ("active", COLORS["heading"])],
            font=[("selected", (FONT_FAMILY, 11, "bold"))],
        )

        # Structural styles that must exist in BOTH themes (sv_ttk doesn't
        # define our custom named frames/labels). These set backgrounds so
        # the section labels, header band, status bar, and grouped frames
        # render on the correct surface regardless of theme.
        style.configure("Header.TFrame", background=COLORS["bg"])
        style.configure("Status.TFrame", background=COLORS["bg"])
        style.configure("Status.TLabel", background=COLORS["bg"],
                        foreground=COLORS["muted"], font=(FONT_FAMILY, 10))
        style.configure("Group.TFrame", background=COLORS["card"])
        style.configure("Group.TLabel", background=COLORS["card"],
                        foreground=COLORS["muted"], font=(FONT_FAMILY, 10, "bold"))
        if not _HAS_SVTTK:
            # Card surface only needs an explicit bg in the fallback theme;
            # sv_ttk paints frames itself.
            style.configure("Card.TFrame", background=COLORS["card"])
    # ---------- helper to register translatable widgets ----------
    def _reg(self, widget, kind, key):
        self._i18n.append((widget, kind, key))
        return widget

    def _t(self, key):
        return self.tr.t(key)

    # ---------- UI ----------
    def _build_ui(self):
        self.root.title("Range Chart Analyzer")
        # Wider default + larger minimum so the left settings panel isn't cramped.
        self.root.geometry("1280x880")
        self.root.minsize(1040, 700)
        # Window + taskbar icon. Generated by rca_core.logo (Pillow).
        # Silent try/except so the GUI still runs even if Pillow is missing.
        try:
            import os as _os
            _base = _os.path.dirname(_os.path.abspath(__file__))
            _ico = _os.path.join(_base, "assets", "logo.ico")
            _png = _os.path.join(_base, "assets", "logo.png")
            if _os.path.isfile(_ico):
                self.root.iconbitmap(_ico)
            if _os.path.isfile(_png) and HAS_PIL:
                from PIL import Image as _Image, ImageTk as _ImageTk  # type: ignore
                _im = _Image.open(_png)
                self._app_icon = _ImageTk.PhotoImage(_im)
                self.root.iconphoto(True, self._app_icon)
        except Exception:
            pass

        # Header — a single fine band: compact title + language pill, separated
        # by a hairline rule from content. Mirrors cc-switch's thin top bar.
        header = ttk.Frame(self.root, style="Header.TFrame")
        header.pack(fill="x", padx=28, pady=(14, 0))

        self.lbl_title = ttk.Label(
            header, text="Range Chart Analyzer",
            style="Title.TLabel", padding=(0, 6))
        self.lbl_title.pack(side="left")

        # Language dropdown (right-aligned). cc-switch uses a segmented
        # control; here we use a readonly combobox so the active language is
        # always fully visible (中文 / English / 日本語) without abbreviation.
        lang_box = ttk.Frame(header, style="Header.TFrame")
        lang_box.pack(side="right")
        # ttk.Combobox values must be strings; we keep a parallel mapping.
        self._lang_display = {"zh": "中文", "en": "English", "ja": "日本語"}
        self._lang_codes = ["zh", "en", "ja"]
        self.cmb_lang = ttk.Combobox(
            lang_box, state="readonly", width=10,
            values=[self._lang_display[c] for c in self._lang_codes])
        self.cmb_lang.set(self._lang_display.get(self.tr.lang, "中文"))
        self.cmb_lang.pack(side="right")
        self.cmb_lang.bind("<<ComboboxSelected>>", self._on_lang_change_combo)

        # Hairline separator.
        ttk.Separator(self.root, orient="horizontal").pack(
            fill="x", padx=28, pady=(8, 0))

        # Main split: left controls, right results.
        # Left panel widened from 500 -> 600 so provider rows (icon + dot +
        # name + format + delete) aren't truncated.
        body = ttk.Frame(self.root, style="TFrame")
        body.pack(fill="both", expand=True, padx=20, pady=8)
        body.columnconfigure(0, weight=0, minsize=460)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)

        left_outer = ttk.Frame(body, style="TFrame")
        left_outer.grid(row=0, column=0, sticky="nsew", padx=(0, 16))
        right = ttk.Frame(body, style="TFrame")
        right.grid(row=0, column=1, sticky="nsew")

        # Wrap left_outer in a scrollable container so a tall settings card
        # remains usable on a short window ("开始提取" below the fold).
        scroller = ScrollableFrame(left_outer, bg=COLORS["card"], border=0, padx=0, pady=0)
        left = scroller.inner
        self._settings_scroller = scroller

        self._build_left(left)
        self._build_right(right)

        # Status bar — sits below a separator so the chrome reads as a
        # distinct bottom band.
        ttk.Separator(self.root, orient="horizontal").pack(
            fill="x", padx=28, pady=(4, 0))
        status = ttk.Frame(self.root, style="Status.TFrame")
        status.pack(fill="x", padx=28, pady=(8, 14))
        self.lbl_status = ttk.Label(status, textvariable=self.var_status, style="Status.TLabel")
        self.lbl_status.pack(side="left")
        self.progress = ttk.Progressbar(status, mode="indeterminate", length=200)

    # -- small helpers for building grouped sections -------------------------
    def _section_label(self, parent, text, *, pad_top=10):
        """A muted, uppercase section title that groups related settings
        (vscode-style: small bold caps, 9px)."""
        wrapper = ttk.Frame(parent, style="Group.TFrame")
        wrapper.pack(anchor="w", padx=16, pady=(pad_top, 6), fill="x")
        lbl = ttk.Label(wrapper, text=text.upper(), style="Group.TLabel")
        lbl.pack(anchor="w")
        sep = tk.Frame(wrapper, bg=COLORS["card_border"], height=1, bd=0, highlightthickness=0)
        sep.pack(fill="x", pady=(0, 4))
        # Register the label so retranslate() updates its text.
        self._reg(lbl, "text", text)
        return wrapper

    def _build_left(self, parent):
        pad = {"padx": PAD_M, "pady": PAD_S}

        # --- Section: API Key ---
        self._section_label(parent, "menu.settings", pad_top=10)
        self.lbl_key = self._reg(ttk.Label(parent, text="", style="Muted.TLabel"), "text", "settings.apiKey")
        self.lbl_key.pack(anchor="w", **pad)
        key_row = ttk.Frame(parent, style="Card.TFrame")
        key_row.pack(fill="x", **pad)
        # FIX (key-toggle): these are the TOP-LEVEL standalone key row.
        # _refresh_active_panel builds a SECOND key row inside each active
        # provider card (self.ent_key_sel) which the toggle must update too,
        # otherwise clicking "show" reveals the top-level row while the
        # active-card row stays masked. Distinct names avoid the old bug
        # where _refresh_active_panel reassigned self.ent_key and silently
        # broke this top-level row's toggle.
        self.ent_key_top = ttk.Entry(key_row, textvariable=self.var_key, show="*", width=30)
        self.ent_key_top.pack(side="left", fill="x", expand=True)
        self.chk_show_top = ttk.Checkbutton(key_row, variable=self.var_show_key, command=self._toggle_key,
                                            style="Card.TCheckbutton")
        self.chk_show_top.pack(side="left", padx=(6, 0))

        # --- Section: LLM Providers (the major feature) ---
        # Layout: a compact list of providers at the top, with the active
        # provider's full configuration expanded below.
        prov_dash = ttk.Frame(parent, style="Card.TFrame")
        prov_dash.pack(fill="both", expand=True, padx=12, pady=(10, 8))

        # Title row: title + search + add button.
        title_row = ttk.Frame(prov_dash, style="Card.TFrame")
        title_row.pack(fill="x", padx=10, pady=(10, 4))
        self.lbl_prov_header = self._reg(
            ttk.Label(title_row, text="", style="Section.TLabel"),
            "text", "settings.llmProvider")
        self.lbl_prov_header.pack(side="left")
        self.btn_add_prov = self._reg(
            ttk.Button(title_row, text="", style="TButton",
                       command=self._open_provider_wizard),
            "text", "settings.addProvider")
        self.btn_add_prov.pack(side="right")

        # Search box (filter providers by name).
        self.var_provider_search = tk.StringVar()
        self.var_provider_search.trace_add("write", lambda *_: self._refresh_provider_list())
        search_row = ttk.Frame(prov_dash, style="Card.TFrame")
        search_row.pack(fill="x", padx=10, pady=(0, 4))
        self.lbl_provider_search = self._reg(
            ttk.Label(search_row, text="🔍", style="Muted.TLabel"),
            "text", "settings.searchHint")
        self.lbl_provider_search.pack(side="left", padx=(0, 4))
        self.ent_provider_search = ttk.Entry(
            search_row, textvariable=self.var_provider_search)
        self.ent_provider_search.pack(side="left", fill="x", expand=True)

        # Provider list container (compact rows, one line each).
        self.prov_list_frame = ttk.Frame(prov_dash, style="Card.TFrame")
        self.prov_list_frame.pack(fill="x", padx=10, pady=(0, 6))

        # Active provider detail panel — glass-like card with subtle white
        # overlay + primary border accent (cc-switch active-card feel).
        self.lbl_active_title = self._reg(
            ttk.Label(prov_dash, text="", style="Section.TLabel"),
            "text", "settings.activeConfig")
        self.lbl_active_title.pack(anchor="w", padx=10, pady=(8, 4))

        # Outer frame: navy border gives the "colored glass" edge.
        self.active_card = tk.Frame(prov_dash, bg=COLORS["primary"],
                                     bd=0, highlightthickness=0)
        self.active_card.pack(fill="x", padx=10, pady=(0, 10))
        # Inner frame: light navy tint + 1px white overlay at the top for
        # a frosted-glass feel.
        self.active_inner = tk.Frame(self.active_card, bg=COLORS["primary_soft"],
                                      bd=0, highlightthickness=0)
        self.active_inner.pack(fill="both", padx=1, pady=(1, 1))
        # 1px frosted highlight at the top.
        tk.Frame(self.active_inner, bg="#ffffff", height=1, bd=0,
                 highlightthickness=0).pack(side="top", fill="x")

        self._refresh_provider_list()
        self._refresh_active_panel()

        # --- Section: API Connection (Endpoint + Model only) ---
        self._section_label(parent, "settings.endpoint", pad_top=4)
        self.lbl_endpoint = self._reg(ttk.Label(parent, text="", style="Muted.TLabel"), "text", "settings.endpoint")
        self.lbl_endpoint.pack(anchor="w", **pad)
        ttk.Entry(parent, textvariable=self.var_endpoint).pack(fill="x", **pad)

        self.lbl_model = self._reg(ttk.Label(parent, text="", style="Muted.TLabel"), "text", "settings.model")
        self.lbl_model.pack(anchor="w", **pad)
        ttk.Entry(parent, textvariable=self.var_model).pack(fill="x", **pad)

        # --- Section: Generation settings ---
        self._section_label(parent, "settings.advanced", pad_top=4)
        adv_frame = ttk.Frame(parent, style="Card.TFrame")
        adv_frame.pack(fill="x", padx=16, pady=(0, 4))

        self.lbl_maxtok = self._reg(ttk.Label(adv_frame, text="", style="Muted.TLabel"),
                                     "text", "settings.maxTokens")
        self.lbl_maxtok.grid(row=0, column=0, sticky="e", padx=(4, 8), pady=8)
        self.ent_maxtok = ttk.Entry(adv_frame, textvariable=self.var_maxtok, width=12)
        self.ent_maxtok.grid(row=0, column=1, sticky="w", padx=(0, 16), pady=8)

        self.lbl_runs = self._reg(ttk.Label(adv_frame, text="", style="Muted.TLabel"),
                                  "text", "settings.runs")
        self.lbl_runs.grid(row=0, column=2, sticky="e", padx=(4, 8), pady=8)
        self.ent_runs = ttk.Spinbox(adv_frame, from_=1, to=5, textvariable=self.var_runs, width=6)
        self.ent_runs.grid(row=0, column=3, sticky="w", padx=(0, 4), pady=8)

        self.lbl_maxedge = self._reg(ttk.Label(adv_frame, text="", style="Muted.TLabel"),
                                     "text", "settings.maxEdge")
        self.lbl_maxedge.grid(row=1, column=0, sticky="e", padx=(4, 8), pady=8)
        self.ent_maxedge = ttk.Entry(adv_frame, textvariable=self.var_maxedge, width=12)
        self.ent_maxedge.grid(row=1, column=1, sticky="w", padx=(0, 16), pady=8)

        # Chart type and language on a second row.
        self.lbl_chart_type = self._reg(ttk.Label(adv_frame, text="", style="Muted.TLabel"),
                                        "text", "settings.chartType")
        self.lbl_chart_type.grid(row=2, column=0, sticky="e", padx=(4, 8), pady=8)
        # Display labels map to stored values via _chart_type_map / _chart_lang_map.
        self._chart_type_map = [
            ("auto", "Auto"),
            ("range_chart", "Range Chart"),
            ("columnar_section", "Columnar Section"),
            ("abundance_diagram", "Abundance / Pollen"),
        ]
        self.cmb_chart_type = ttk.Combobox(
            adv_frame, state="readonly", width=14,
            textvariable=self._cmb_chart_type_disp,
            values=[d for _, d in self._chart_type_map])
        self.cmb_chart_type.grid(row=2, column=1, sticky="w", padx=(0, 16), pady=8)
        self._cmb_chart_type_disp.trace_add("write", lambda *_: self._on_cmb_chart_type_change())
        try:
            # H2: lock dirty during programmatic combobox.set so it doesn't
            # flip the Save button during init.
            def _set_init_type():
                self.cmb_chart_type.set(self._code_to_chart_type_display(self.var_chart_type.get()))
            self._dirty_lock_run(_set_init_type)
        except Exception:
            pass

        self.lbl_chartlang = self._reg(ttk.Label(adv_frame, text="", style="Muted.TLabel"),
                                        "text", "settings.chartLang")
        self.lbl_chartlang.grid(row=2, column=2, sticky="e", padx=(4, 8), pady=8)
        self._chart_lang_map = [
            ("auto", "Auto"),
            ("zh", "Chinese (中文)"),
            ("en", "English"),
            ("ja", "Japanese (日本語)"),
            ("ru", "Russian (Русский)"),
        ]
        self.cmb_chartlang = ttk.Combobox(
            adv_frame, state="readonly", width=14,
            textvariable=self._cmb_chartlang_disp,
            values=[d for _, d in self._chart_lang_map])
        self.cmb_chartlang.grid(row=2, column=3, sticky="w", padx=(0, 4), pady=8)
        self._cmb_chartlang_disp.trace_add("write", lambda *_: self._on_cmb_chartlang_change())
        try:
            def _set_init_lang():
                self.cmb_chartlang.set(self._code_to_chartlang_display(self.var_chartlang.get()))
            self._dirty_lock_run(_set_init_lang)
        except Exception:
            pass

        # Save and extract buttons.
        btn_row = ttk.Frame(parent, style="Card.TFrame")
        btn_row.pack(fill="x", **pad)
        self.btn_save = self._reg(ttk.Button(btn_row, text="", style="TButton",
                                  command=self._save_settings), "text", "settings.save")
        self.btn_save.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self.btn_extract = self._reg(ttk.Button(btn_row, text="", style="Primary.TButton",
                                     command=self._on_extract), "text", "action.extract")
        self.btn_extract.pack(side="left", fill="x", expand=True)

        self.chk_remember = self._reg(ttk.Checkbutton(parent, variable=self.var_remember,
                                      style="Card.TCheckbutton"), "text", "settings.remember")
        self.chk_remember.pack(anchor="w", padx=16, pady=(4, 2))

        # --- Section: Image ---
        self._section_label(parent, "menu.image", pad_top=10)
        img_btn_row = ttk.Frame(parent, style="Card.TFrame")
        img_btn_row.pack(fill="x", **pad)
        self.btn_choose = self._reg(ttk.Button(img_btn_row, text="", style="TButton",
                                    command=self._choose_image), "text", "image.choose")
        self.btn_choose.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self.btn_paste = self._reg(ttk.Button(img_btn_row, text="", style="TButton",
                                    command=self._paste_from_clipboard), "text", "image.paste")
        self.btn_paste.pack(side="left", fill="x", expand=True)

        self.lbl_imginfo = ttk.Label(parent, text="", style="Muted.TLabel", wraplength=400, justify="left")
        self.lbl_imginfo.pack(anchor="w", padx=16, pady=2)

        # 16:9 preview canvas that fills the available width.
        self.canvas_preview = tk.Canvas(parent, height=160, bg=COLORS["field_bg"],
                                        highlightthickness=1, highlightbackground=COLORS["border"])
        self.canvas_preview.pack(padx=16, pady=6, fill="x")

        self.lbl_caption = self._reg(ttk.Label(parent, text="", style="Muted.TLabel"), "text", "caption.label")
        self.lbl_caption.pack(anchor="w", **pad)
        self.txt_caption = tk.Text(parent, height=3, width=36, wrap="word",
                                   relief="flat", borderwidth=1,
                                   highlightthickness=1,
                                   highlightbackground=COLORS["border"],
                                   highlightcolor=COLORS["border_focus"])
        self.txt_caption.pack(fill="x", padx=16, pady=4)

    def _code_to_chart_type_display(self, code: str) -> str:
        for c, d in self._chart_type_map:
            if c == code:
                return d
        return self._chart_type_map[0][1]

    def _code_to_chartlang_display(self, code: str) -> str:
        for c, d in self._chart_lang_map:
            if c == code:
                return d
        return self._chart_lang_map[0][1]

    def _on_cmb_chart_type_change(self):
        # Combobox display var holds the localized label; map back to code.
        label = self._cmb_chart_type_disp.get()
        for c, d in self._chart_type_map:
            if d == label:
                self.var_chart_type.set(c)
                return

    def _on_cmb_chartlang_change(self):
        label = self._cmb_chartlang_disp.get()
        for c, d in self._chart_lang_map:
            if d == label:
                self.var_chartlang.set(c)
                return

    def _build_right(self, parent):
        # Top toolbar: confidence + export-all
        top = ttk.Frame(parent, style="TFrame")
        top.pack(fill="x", pady=(0, 6))
        # Confidence badge uses a distinct, slightly heavier label so the
        # result summary reads as a chip rather than body text.
        self.lbl_conf = ttk.Label(top, text="", style="Sub.TLabel",
                                  font=(FONT_FAMILY, 12, "bold"),
                                  foreground=COLORS["primary"])
        self.lbl_conf.pack(side="left")
        self.btn_export_json = self._reg(ttk.Button(top, text="", command=self._export_json),
                                         "text", "action.exportJson")
        self.btn_export_json.pack(side="right")

        # Results notebook. Tab content is rebuilt dynamically by
        # `_render_result` based on the active result shape (range-chart
        # vs columnar-section), so we only create the ttk.Notebook shell
        # here and a placeholder tab for a reasonable first size.
        self.nb = ttk.Notebook(parent)
        self.nb.pack(fill="both", expand=True)
        self.trees = {}
        self.tab_frames = {}
        self._placeholder_tab = ttk.Frame(self.nb, style="Card.TFrame")
        self.nb.add(self._placeholder_tab, text=self._t("results.empty"))
        # Populate the placeholder tab with a proper empty state that
        # includes a CTA button ("select image" guides the user action).
        empty = ttk.Frame(self._placeholder_tab, style="Card.TFrame")
        empty.pack(expand=True, fill="both")
        tk.Label(empty, text="📊", bg=COLORS["card"], fg=COLORS["muted"],
                 font=(FONT_FAMILY, 48)).pack(pady=(70, 6))
        self._reg(tk.Label(empty, text=self._t("results.empty"),
                            bg=COLORS["card"], fg=COLORS["heading"],
                            font=(FONT_FAMILY, 13, "bold"),
                            justify="center"),
                  "text", "results.empty").pack(pady=(0, 4))
        self._reg(tk.Label(empty, text=self._t("results.emptyHint"),
                            bg=COLORS["card"], fg=COLORS["muted"],
                            font=(FONT_FAMILY, 10),
                            justify="center", wraplength=420),
                  "text", "results.emptyHint").pack(pady=(0, 12))
        btn_open = self._reg(tk.Button(empty, text=self._t("image.choose"),
                                        command=self._choose_image,
                                        bg=COLORS["primary"], fg="#ffffff",
                                        activebackground=COLORS["primary_hover"],
                                        activeforeground="#ffffff",
                                        relief="flat", bd=0,
                                        padx=16, pady=8,
                                        cursor="hand2",
                                        font=(FONT_FAMILY, 10, "bold")),
                              "text", "image.choose").pack(pady=(0, 16))

    # ----------
    # NOTE: the compact header intentionally drops the subtitle to keep the
    # top bar thin. The 'app.subtitle' key is still translated for web mode.
    # ----------
    def retranslate(self):
        for widget, kind, key in self._i18n:
            try:
                widget.config(text=self._t(key))
            except Exception:
                pass
        # L4: wrap the eye-char update so a future rebuild that destroys
        # the checkbuttons doesn't crash with TclError on language change.
        # There are now two: top-level (chk_show_top) and active-card
        # (chk_show_sel). Update whichever currently exist.
        for attr in ("chk_show_top", "chk_show_sel"):
            try:
                getattr(self, attr, None).config(text="👁")
            except Exception:
                pass
        if hasattr(self, "cmb_lang"):
            self.cmb_lang.set(self._lang_display.get(self.tr.lang, "中文"))
        if hasattr(self, "_refresh_provider_list"):
            self._refresh_provider_list()
        # Notebook tabs are rebuilt dynamically in `_render_result` on the
        # active result shape; retranslate just refreshes the placeholder.
        # image info label
        self._refresh_imginfo()
        # confidence label
        self._refresh_conf()
        if not self.var_status.get():
            self.var_status.set(self._t("status.ready"))

    def _on_lang_change(self):
        self.tr.set_lang(self.var_lang.get())
        self.retranslate()

    def _on_lang_change_combo(self, _event=None):
        display = self.cmb_lang.get().strip()
        code = None
        for c, d in self._lang_display.items():
            if d == display:
                code = c
                break
        if code is None:
            code = "zh"
        # CRITICAL: also update var_lang so retranslate() picks the new lang.
        self.var_lang.set(code)
        self.tr.set_lang(code)
        self.retranslate()
        self._refresh_provider_list()

    # ---------- key toggle ----------
    def _toggle_key(self):
        # Toggle BOTH the top-level key row (ent_key_top) and the active-card
        # key row (ent_key_sel). They share var_show_key, so read the same
        # value; the conditional is just for clarity.
        show = "" if self.var_show_key.get() else "*"
        if getattr(self, "ent_key_top", None) is not None:
            self.ent_key_top.config(show=show)
        if getattr(self, "ent_key_sel", None) is not None:
            self.ent_key_sel.config(show=show)

    # ---------- settings ----------
    def _collect_cfg(self) -> dict:
        return {
            "lang": self.tr.lang,
            "endpoint": self.var_endpoint.get().strip() or DEFAULT_ENDPOINT,
            "model": self.var_model.get().strip() or DEFAULT_MODEL,
            "max_tokens": self._max_tokens(),
            "max_edge": self._max_edge(),
            "runs": self._runs(),
            "chart_lang": self.var_chartlang.get(),
            "chart_type": self.var_chart_type.get(),
            "remember": bool(self.var_remember.get()),
            "api_key": self.var_key.get().strip() if self.var_remember.get() else "",
        }

    def _save_settings(self):
        save_config(self._collect_cfg())
        # H2: clear the Save-button • indicator after a successful save.
        self._clear_dirty()
        self.var_status.set(self._t("settings.saved"))

    def _max_tokens(self) -> int:
        # M5: clamp to [MIN, MAX] to defend against typos / out-of-range edits.
        return clamp_max_tokens(self.var_maxtok.get())

    def _max_edge(self) -> int:
        try:
            v = int(self.var_maxedge.get())
            return max(0, v)
        except (TypeError, ValueError):
            return DEFAULT_MAX_EDGE

    def _runs(self) -> int:
        try:
            return max(1, min(int(self.var_runs.get()), 5))
        except (TypeError, ValueError):
            return 1

    # ---------- image ----------
    def _choose_image(self):
        path = filedialog.askopenfilename(
            title=self._t("dialog.chooseImage"),
            filetypes=[("Images", "*.png *.jpg *.jpeg *.webp *.bmp *.gif"), ("All files", "*.*")],
        )
        if not path:
            return
        self._cleanup_paste_tmp()
        self.image_path = path
        # Load + encode in a thread-free quick step (files are local).
        try:
            b64, mime, w, h, resized, decode_error = load_image_b64(path, self._max_edge())
        except Exception:
            self.var_status.set(self._t("err.imageRead"))
            return None
        if decode_error:
            # Bug-15 fix: surface a specific "corrupt image" message
            # instead of silently going through with a 0×0 thumbnail.
            self.var_status.set(self._t("err.imageDecode"))
            return None
        self.image_b64 = b64
        self.media_type = mime
        self._img_dims = (w, h, resized)
        self._refresh_imginfo()
        self._render_preview(path)

    def _cleanup_paste_tmp(self):
        """Delete any prior paste tmp file (H1: prevent leak on every paste).

        Tracks the most recently pasted temp path and removes it when a new
        image replaces it or when the app shuts down.
        """
        prev = getattr(self, "_last_paste_tmp", None)
        if prev and os.path.isfile(prev):
            try:
                os.unlink(prev)
            except OSError:
                pass
        self._last_paste_tmp = None

    def _paste_from_clipboard(self, event=None):
        """Paste an image from the OS clipboard into the upload panel.

        Silent when the clipboard has no image (e.g. user pasted plain
        text into the caption box). Returns without grabbing the event
        so the focused widget receives the default paste naturally.
        Returns "break" only when an image was actually consumed.
        """
        if not HAS_PIL:
            self.var_status.set(self._t("image.noClipboard"))
            return None
        try:
            from PIL import ImageGrab  # type: ignore
            img = ImageGrab.grabclipboard()
        except Exception:
            return None
        if img is None:
            self.var_status.set(self._t("image.noClipboard"))
            return None
        # Save to a temp file so the encode path is shared with file-based
        # loading (preview, downscale, encoding all go through load_image_b64).
        import tempfile
        suffix = ".png" if not getattr(img, "format", None) else "." + (img.format or "PNG").lower()
        fd, tmp_path = tempfile.mkstemp(prefix="rca_paste_", suffix=suffix)
        os.close(fd)
        try:
            if img.mode == "RGBA" and suffix == ".jpg":
                img = img.convert("RGB")
            img.save(tmp_path)
            # H1: delete the previous successful-paste tmp file BEFORE
            # reassigning self.image_path so repeated pastes don't leak.
            self._cleanup_paste_tmp()
            self.image_path = tmp_path
            self._last_paste_tmp = tmp_path
            b64, mime, w, h, resized, decode_error = load_image_b64(tmp_path, self._max_edge())
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            self.var_status.set(self._t("err.imageRead"))
            return None
        if decode_error:
            # Bug-15 fix: clipboard image is corrupt. Delete the tmp file
            # and surface a specific message rather than uploading 0×0.
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            self._last_paste_tmp = None
            self.image_path = None
            self.var_status.set(self._t("err.imageDecode"))
            return None
        self.image_b64 = b64
        self.media_type = mime
        self._img_dims = (w, h, resized)
        self._refresh_imginfo()
        self._render_preview(tmp_path)
        self.var_status.set(self._t("image.pasted"))
        return "break"  # only swallow the default paste if we actually got an image

    # NOTE: the per-provider summary line has been replaced by the full
    # provider list UI (see _refresh_provider_list).

    def _open_provider_wizard(self):
        """Open the provider management dialog (preset grid + CRUD)."""
        # L5: re-use an existing wizard instead of stacking multiple
        # modals on rapid clicks; raise to front if it's already open.
        existing = getattr(self, "_wizard_win", None)
        if existing is not None:
            try:
                if existing.winfo_exists():
                    existing.deiconify()
                    existing.lift()
                    existing.focus_force()
                    return
            except Exception:
                pass
        win = ProviderWizard(self.root, self._on_wizard_close, translate=self._t)
        self._wizard_win = win
        try:
            win.win.bind("<Destroy>", lambda *_: setattr(self, "_wizard_win", None))
        except Exception:
            pass

    def _on_wizard_close(self):
        """Refresh UI after the provider wizard closes."""
        self._refresh_provider_list()

    def _refresh_provider_list(self):
        """Render a compact one-line row per configured provider."""
        frame = getattr(self, "prov_list_frame", None)
        if frame is None:
            return
        for child in frame.winfo_children():
            child.destroy()

        try:
            store = ProviderStore().load()
        except Exception:
            store = None
        self._active_store = store

        if not store or not store.providers:
            ttk.Label(frame, text=self._t("settings.noProviders"),
                      style="Muted.TLabel").pack(anchor="w", padx=12, pady=8)
            return

        # Apply the search filter.
        query = self.var_provider_search.get().strip().lower() if hasattr(self, "var_provider_search") else ""
        provs = store.providers
        if query:
            provs = [p for p in provs
                     if query in (p.name or "").lower()
                     or query in (p.model or "").lower()
                     or query in (p.api_format.value if p.api_format else "")]

        for prov in provs:
            self._build_provider_row(frame, prov, store)

    def _refresh_active_panel(self):
        """Render the full configuration of the currently-active provider."""
        inner = getattr(self, "active_inner", None)
        if inner is None:
            return
        # Strip stale _i18n registrations whose widget was a child of
        # `inner` (about to be destroyed). Prevents accumulation of
        # TclError-prone stale widget references.
        if hasattr(self, "_i18n"):
            self._i18n = [t for t in self._i18n
                          if not (t and t[0].winfo_exists()
                                  and str(t[0].winfo_parent()) == str(inner))]
        for child in inner.winfo_children():
            child.destroy()

        store = getattr(self, "_active_store", None)
        if store is None:
            try:
                store = ProviderStore().load()
                self._active_store = store
            except Exception:
                store = None
        if not store:
            tk.Label(inner, text="—", bg=COLORS["primary_soft"],
                      fg=COLORS["muted"], font=(FONT_FAMILY, 10)).pack(
                padx=14, pady=20, anchor="w")
            return

        cur = store.get_current()
        if cur is None:
            self._reg(
                tk.Label(inner, text=self._t("settings.noProviders"),
                         bg=inner.cget("bg"), fg=COLORS["muted"],
                         font=(FONT_FAMILY, 10), justify="left"),
                "text", "settings.noProviders").pack(
                padx=14, pady=16, anchor="w", fill="x")
            return

        grid = ttk.Frame(inner, style="Card.TFrame")
        grid.pack(fill="x", padx=14, pady=14)

        # Header row with dot + name + format.
        header_row = ttk.Frame(grid, style="Card.TFrame")
        header_row.grid(row=0, column=0, columnspan=2, sticky="we", pady=(0, 8))
        dot_color = COLORS["success"] if cur.api_key else COLORS["warning"]
        tk.Label(header_row, text="●", bg=inner.cget("bg"), fg=dot_color,
                 font=(FONT_FAMILY, 14, "bold")).pack(side="left", padx=(0, 6))
        tk.Label(header_row, text=cur.name or "—", bg=inner.cget("bg"),
                 fg=COLORS["text"], font=(FONT_FAMILY, 13, "bold")).pack(side="left")
        if cur.api_format:
            tk.Label(header_row, text="  ·  " + cur.api_format.value,
                     bg=inner.cget("bg"), fg=COLORS["muted"],
                     font=(FONT_FAMILY, 10)).pack(side="left", padx=(6, 0))

        def _row(r, label_key, value, is_secret=False):
            self._reg(ttk.Label(grid, text="", style="Muted.TLabel"),
                      "text", label_key).grid(row=r, column=0, sticky="w",
                                            padx=(0, 8), pady=3)
            if label_key == "settings.apiKey":
                key_row = ttk.Frame(grid, style="Card.TFrame")
                key_row.grid(row=r, column=1, sticky="we", pady=3)
                # Active-card key row — distinct from the top-level
                # self.ent_key_top. _toggle_key updates BOTH rows.
                self.ent_key_sel = ttk.Entry(key_row, textvariable=self.var_key,
                                              show="" if self.var_show_key.get() else "*",
                                              width=40)
                self.ent_key_sel.pack(side="left", fill="x", expand=True)
                self.chk_show_sel = ttk.Checkbutton(key_row, variable=self.var_show_key,
                                                    command=self._toggle_key,
                                                    style="Card.TCheckbutton")
                self.chk_show_sel.pack(side="left", padx=(6, 0))
                return
            text = ("•" * 8) if (is_secret and value) else (value or "—")
            ttk.Label(grid, text=text, style="Card.TLabel",
                      font=(FONT_FAMILY, 10)).grid(row=r, column=1, sticky="w", pady=3)

        grid.columnconfigure(0, minsize=100)
        grid.columnconfigure(1, weight=1)
        _row(1, "settings.endpoint", cur.endpoint)
        _row(2, "settings.model", cur.model)
        _row(3, "settings.apiKey", cur.api_key, is_secret=True)

        # Test connection row.
        test_row = ttk.Frame(grid, style="Card.TFrame")
        test_row.grid(row=4, column=0, columnspan=2, sticky="we", pady=(8, 0))
        btn = self._reg(ttk.Button(test_row, text="", style="TButton",
                                command=lambda p=cur: self._test_provider_connection(p)),
                     "text", "settings.testConnection")
        btn.pack(side="left")
        self.lbl_test_result = ttk.Label(test_row, text="", style="Muted.TLabel",
                                           font=(FONT_FAMILY, 9, "bold"))
        self.lbl_test_result.pack(side="left", padx=(10, 0))

    def _build_provider_row(self, parent, prov: LlmProvider, store):
        """Compact one-line row: dot + name + format. Click to activate.

        Active row uses a primary-tinted background + 1px navy top
        accent + thin navy left stripe. Wrapper frame simulates a drop
        shadow (cc-switch style) with a 1px light-grey offset layer.
        """
        is_active = prov.is_current
        bg = COLORS["primary_soft"] if is_active else COLORS["card"]
        fg = COLORS["text"]
        muted = COLORS["muted"]
        dot = COLORS["success"] if prov.api_key else COLORS["warning"]
        border = COLORS["primary"] if is_active else COLORS["card_border"]

        # Pseudo-shadow wrapper: 1px left/top offset with a faint grey line.
        wrapper = tk.Frame(parent, bg=bg, bd=0, highlightthickness=0)
        wrapper.pack(fill="x", padx=1, pady=(0, 4))
        if is_active:
            tk.Frame(wrapper, bg="#c0c5cc", width=1, bd=0).pack(side="right", fill="y",
                                                               anchor="ne")
            tk.Frame(wrapper, bg="#c0c5cc", height=1, bd=0).pack(side="bottom", fill="x",
                                                                anchor="se")

        row = tk.Frame(wrapper, bg=bg, bd=0,
                        highlightthickness=1 if is_active else 0,
                        highlightbackground=border)
        row.pack(fill="x")
        row.pack_propagate(False)

        if is_active:
            # Top 1px accent + left stripe to make the active row feel raised.
            tk.Frame(row, bg=COLORS["primary"], height=1, bd=0).pack(
                side="top", fill="x")
            tk.Frame(row, bg=COLORS["primary"], width=3, bd=0).pack(
                side="left", fill="y")
        else:
            tk.Frame(row, bg=bg, width=8, bd=0).pack(side="left", fill="y")

        fmt = prov.api_format.value if prov.api_format else "anthropic"
        icon = {"anthropic": "🟣", "openai": "🟢", "gemini": "🔵"}.get(fmt, "⚪")
        tk.Label(row, text=icon, bg=bg, fg=fg,
                 font=(FONT_FAMILY, 12)).pack(side="left", padx=(6, 4))

        tk.Label(row, text="●", bg=bg, fg=dot,
                 font=(FONT_FAMILY, 10, "bold")).pack(side="left", padx=(4, 6))

        name_fmt = (prov.name or "Provider") + (
            "  ·  " + (prov.api_format.value if prov.api_format else "—"))
        click_lbl = tk.Label(row, text=name_fmt, bg=bg, fg=fg,
                              font=(FONT_FAMILY, 10, "bold"), cursor="hand2",
                              anchor="w", justify="left")
        click_lbl.pack(side="left", fill="x", expand=True, padx=(0, 8), pady=8)
        click_lbl.bind("<Button-1>",
                       lambda _e, pid=prov.id: self._activate_provider(pid))

        btn_del = tk.Label(row, text="✕", bg=bg, fg=muted,
                            cursor="hand2", font=(FONT_FAMILY, 10), padx=6)
        btn_del.bind("<Enter>",
                     lambda _e, b=btn_del: b.config(fg=COLORS["danger"]))
        btn_del.bind("<Leave>",
                     lambda _e, b=btn_del: b.config(fg=muted))
        btn_del.bind("<Button-1>",
                     lambda _e, pid=prov.id: self._delete_provider_simple(pid))
        btn_del.pack(side="right", padx=4, pady=2)


    def _test_provider_connection(self, provider: LlmProvider):
        """Spin up a thread that calls test_llm_connection. The button shows
        a "Testing..." spinner until the result arrives."""
        if getattr(self, "_testing_id", None) == provider.id:
            return
        self._testing_id = provider.id
        # Spinner state on the result label.
        if hasattr(self, "lbl_test_result"):
            self.lbl_test_result.configure(text="⏳ " + self._t("settings.testing"), foreground=COLORS["muted"])
        thread = threading.Thread(
            target=self._run_connection_test,
            args=(provider,),
            daemon=True)
        thread.start()

    def _run_connection_test(self, provider: LlmProvider):
        from rca_core.llm import test_llm_connection
        res = test_llm_connection(provider, timeout_sec=8)
        if res.ok:
            text = "✓  " + str(res.latency_ms) + " ms"
            if res.models_sample:
                text += "  ·  " + ", ".join(res.models_sample[:3])
            fg = COLORS["success"]
            bg = COLORS["success_soft"]
        else:
            err_key = res.error_key or "err.http"
            err_text = self._t(err_key)
            text = "✗  " + err_text
            if res.status:
                text += "  (HTTP " + str(res.status) + ")"
            fg = COLORS["danger"]
            bg = "#fef2f2"

        def _apply():
            self._testing_id = None
            lbl = getattr(self, "lbl_test_result", None)
            if lbl is None:
                return
            try:
                lbl.config(
                    text=text, foreground=fg, bg=bg,
                    font=(FONT_FAMILY, 10, "bold"),
                    padx=10, pady=2)
            except Exception:
                pass
        try:
            self.root.after(0, _apply)
        except Exception:
            pass

    def _refresh_imginfo(self):
        if not self.image_path:
            self.lbl_imginfo.config(text=self._t("image.none"))
            return
        w, h, resized = getattr(self, "_img_dims", (0, 0, False))
        name = os.path.basename(self.image_path)
        dims = f"{self._t('image.dims')}: {w} x {h}" if w else ""
        tail = " " + self._t("image.resized") if resized else ""
        self.lbl_imginfo.config(text=f"{name}\n{dims}{tail}")

    def _render_preview(self, path):
        self.canvas_preview.delete("all")
        if not HAS_PIL:
            self.canvas_preview.create_text(80, 80, text="(preview needs Pillow)",
                                            fill=COLORS["muted"])
            return
        try:
            img = Image.open(path)
            cw = max(80, self.canvas_preview.winfo_width() - 4)
            ch = 156
            iw, ih = img.size
            if iw <= 0 or ih <= 0:
                return
            scale = min(cw / iw, ch / ih)
            new_size = (max(1, int(iw * scale)), max(1, int(ih * scale)))
            img = img.resize(new_size, Image.LANCZOS)
            self.thumb = ImageTk.PhotoImage(img)
            x = (cw - new_size[0]) // 2 + 2
            y = (ch - new_size[1]) // 2 + 2
            self.canvas_preview.create_image(x, y, image=self.thumb, anchor="nw")
        except Exception:
            self.canvas_preview.create_text(80, 80, text="(preview error)", fill=COLORS["muted"])

    # ---------- extraction ----------
    def _set_busy(self, busy: bool):
        self.busy = busy
        self.btn_extract.config(state="disabled" if busy else "normal",
                                text=self._t("action.extracting") if busy else self._t("action.extract"))
        if busy:
            self.progress.pack(side="right")
            self.progress.start(12)
            self.var_status.set(self._t("status.loading"))
        else:
            self.progress.stop()
            self.progress.pack_forget()

    def _current_provider(self) -> LlmProvider | None:
        """Try to load the active provider from disk; fall back to None to
        signal legacy flat-field behavior."""
        try:
            store = ProviderStore().load()
            return store.get_current()
        except Exception:
            return None

    def _on_extract(self):
        if self.busy:
            return
        if not self.image_b64:
            messagebox.showwarning("Range Chart Analyzer", self._t("err.noImage"))
            return
        provider = self._current_provider()
        # H3: a configured provider with no API key must fall back to the
        # legacy textbox key — otherwise the request goes out with no auth
        # header and we surface a confusing 401.
        legacy_key = self.var_key.get().strip()
        if provider is not None and not (provider.api_key or "").strip():
            provider = None  # ignore the empty-key provider
        if provider is None and not legacy_key:
            messagebox.showwarning("Range Chart Analyzer", self._t("err.noKey"))
            return
        self._set_busy(True)
        params = {
            "api_key": self.var_key.get().strip(),
            "image_b64": self.image_b64,
            "media_type": self.media_type or "image/png",
            "caption": self.txt_caption.get("1.0", "end").strip(),
            "chart_lang": self.var_chartlang.get(),
            "base_url": self.var_endpoint.get().strip() or DEFAULT_ENDPOINT,
            "model": self.var_model.get().strip() or DEFAULT_MODEL,
            "max_tokens": self._max_tokens(),
            "provider": provider,
        }
        runs = self._runs()
        # Mode routing: explicit user selection wins; auto uses a lightweight
        # caption/path heuristic (column chart -> columnar_section).
        mode = (self.var_chart_type.get() or "auto").strip()
        if mode == "auto":
            cap = (self.txt_caption.get("1.0", "end").strip() + " " + (self.image_path or "")).lower()
            if any(k in cap for k in ("pollen", "abundance", "percentage diagram", "palyno", "孢粉", "花粉", "丰度", "百分比")):
                mode = "abundance_diagram"
            elif any(k in cap for k in ("column", "columns", "columnar", "col_section", "col_sections", "柱状", "柱状図", "柱状图")):
                mode = "columnar_section"
            else:
                mode = "range_chart"
        threading.Thread(target=self._worker, args=(params, mode, runs), daemon=True).start()

    def _worker(self, params, mode="range_chart", runs=1):
        if runs <= 1:
            self.msg_queue.put(extract(mode=mode, **params))
            return
        # Multi-run: extract N times IN PARALLEL via a thread pool, then
        # merge the successful runs. Concurrent execution trims total wait
        # from O(N * latency) to ~O(latency). urllib network I/O releases
        # the GIL so threads are effective for this workload.
        ok_datas = []
        last_fail = None
        partial_fails = 0
        any_truncated = False
        raws = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=runs) as ex:
            futures = [ex.submit(extract, mode=mode, **params) for _ in range(runs)]
            for fut in concurrent.futures.as_completed(futures):
                try:
                    r = fut.result()
                except Exception as exc:
                    # extract() never raises, but defend against unforeseen
                    # bugs in user code. Bug-12 fix: log so the exception
                    # class + traceback is recoverable when the user reports
                    # "nothing happened".
                    log.exception("extract future raised in worker thread")
                    r = ExtractResult(ok=False, error_key="err.http", raw=str(exc))
                if r.ok and r.data is not None:
                    ok_datas.append(r.data)
                    any_truncated = any_truncated or bool(r.truncated)
                    if r.raw:
                        raws.append(r.raw)
                else:
                    last_fail = r
                    partial_fails += 1
        if not ok_datas:
            self.msg_queue.put(last_fail if last_fail else extract(mode=mode, **params))
            return
        schema = SCHEMA_BY_MODE.get(mode, RANGE_CHART_SCHEMA)
        merged = merge_results(ok_datas, total_runs=runs, schema=schema)
        self.msg_queue.put(ExtractResult(
            ok=True, data=merged, raw="\n---RUN---\n".join(raws)[:8000],
            truncated=any_truncated or bool(partial_fails),
            partial_failures=partial_fails,
        ))

    def _poll_queue(self):
        try:
            while True:
                result = self.msg_queue.get_nowait()
                self._on_result(result)
        except queue.Empty:
            pass
        except Exception:
            # Bug-12 fix: log so a real exception in the UI pump isn't
            # silently dropped. The pump is what keeps the UI alive, so
            # any error here would otherwise freeze the app with no
            # breadcrumb.
            log.exception("UI message pump crashed in _poll_queue")
        # FIX (close-crash): only re-arm the timer if the root window still
        # exists. on_close() destroys the root without cancelling this
        # callback, so after the worker thread enqueues a late result the
        # next _poll_queue would operate on a destroyed Tcl widget →
        # silent TclError. Skip the reschedule entirely when shutting down.
        if getattr(self, "root", None) is not None and self.root.winfo_exists():
            self._poll_after_id = self.root.after(120, self._poll_queue)
        else:
            self._poll_after_id = None

    def _on_result(self, result):
        self._set_busy(False)
        if not result.ok:
            msg = self._t(result.error_key or "err.http")
            if result.status:
                msg += f" (HTTP {result.status})"
            self.var_status.set(msg)
            # H7: prefer the upstream error body when present (it carries the
            # real 5xx reason the server returned); fall back to the model's
            # raw text otherwise.
            detail = (result.error_body or result.raw or "")[:1500]
            if detail:
                messagebox.showerror("Range Chart Analyzer", msg + "\n\n" + detail)
            else:
                messagebox.showerror("Range Chart Analyzer", msg)
            return
        self.result = result.data
        self.raw_text = result.raw
        self._render_result()
        status = self._t("status.done")
        if result.partial_failures:
            # M2: explicit "N of M runs failed" so the operator can tell a
            # partial failure apart from a max_tokens cut-off.
            pf = result.partial_failures
            total_runs = int((self.result or {}).get("runs", 0) or 0)
            total_runs = max(pf + 1, total_runs)
            status += f" - {pf} run(s) failed (only {total_runs - pf}/{total_runs} succeeded)"
        elif result.truncated:
            status += " - " + self._t("err.truncated")
        self.var_status.set(status)

    def _render_result(self):
        multi = self.result and int(self.result.get("runs", 1) or 1) > 1
        n_runs = int((self.result or {}).get("runs", 1) or 1)
        configs = get_configs_for_result(self.result)

        # Rebuild the notebook tabs so the active result shape
        # (range-chart vs columnar-section) drives which tables show.
        for tab_id in list(self.nb.tabs()):
            self.nb.forget(tab_id)
        self.tab_frames = {}
        self.trees = {}
        for cfg in configs:
            frame = ttk.Frame(self.nb, style="Card.TFrame")
            self.nb.add(frame, text=self._t(cfg["title_key"]))
            self.tab_frames[cfg["id"]] = frame

            actions = ttk.Frame(frame, style="Card.TFrame")
            actions.pack(fill="x", padx=6, pady=6)
            self._reg(ttk.Button(actions, text="", style="Small.TButton",
                      command=lambda t=cfg["id"]: self._copy_table(t)),
                      "text", "action.copyTsv").pack(side="left", padx=(0, 6))
            self._reg(ttk.Button(actions, text="", style="Small.TButton",
                      command=lambda t=cfg["id"]: self._export_csv(t)),
                      "text", "action.exportCsv").pack(side="left")

            tree_wrap = ttk.Frame(frame, style="Card.TFrame")
            tree_wrap.pack(fill="both", expand=True, padx=6, pady=(0, 6))
            cols = ["#"] + cfg["cols"]
            tree = ttk.Treeview(tree_wrap, columns=cols, show="headings", selectmode="extended")
            vsb = ttk.Scrollbar(tree_wrap, orient="vertical", command=tree.yview)
            hsb = ttk.Scrollbar(tree_wrap, orient="horizontal", command=tree.xview)
            tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
            tree.grid(row=0, column=0, sticky="nsew")
            vsb.grid(row=0, column=1, sticky="ns")
            hsb.grid(row=1, column=0, sticky="ew")
            tree_wrap.rowconfigure(0, weight=1)
            tree_wrap.columnconfigure(0, weight=1)
            tree.tag_configure("odd", background=COLORS["row_alt"])
            tree.tag_configure("low", background=COLORS["row_low"])
            # Bug fix: configure each column's heading text + alignment so
            # the heading row actually renders content. Without this the
            # thead is rendered but empty, which the clam theme sometimes
            # collapses to 0 height on Windows DPI scaling.
            for col in cols:
                if col == "#":
                    tree.heading(col, text="#", anchor="center")
                else:
                    tree.heading(col, text=self._t(col), anchor="w")
            # FIX (tree columns): give each column a sensible default width
            # and stretch behavior. Without this all columns default to ~200px
            # and the rightmost columns vanish off-screen when there are many
            # (e.g. species_ranges with 6+ columns); the window can be resized
            # but columns don't follow. "#" is fixed-narrow and centered; data
            # columns share the stretch so they fill the viewport.
            for col in cols:
                if col == "#":
                    tree.column(col, width=40, minwidth=32, stretch=False, anchor="center")
                else:
                    tree.column(col, width=110, minwidth=60, stretch=True, anchor="w")
            self.trees[cfg["id"]] = tree

        # Fill the trees with data.
        for cfg in configs:
            tree = self.trees[cfg["id"]]
            items = (self.result or {}).get(cfg["id"], []) or []
            # M1: only range-chart sections have a meaningful agreement_count
            # to flag. Columnar sections get the same field stamped by the
            # merge routine but agreement across runs is much higher (every
            # row keeps its id) so painting them yellow is misleading.
            is_range_chart = "species_ranges" in (self.result or {})
            for idx, item in enumerate(items):
                if not isinstance(item, dict):
                    continue
                cells = cfg["row"](item)
                values = [str(idx + 1)] + ["" if c is None else str(c) for c in cells]
                tag = "odd" if idx % 2 else ""
                primary_field = "species" if cfg["id"] == "species_ranges" else "id"
                if multi and cfg["id"] == "species_ranges":
                    ac = int(item.get("agreement_count", 0) or 0)
                    values[1] = f"{values[1]}  [{item.get('agreement', '')}]"
                    if ac <= n_runs / 2:
                        tag = "low"
                tree.insert("", "end", values=values, tags=(tag,))
        self._refresh_conf()

    def _refresh_conf(self):
        if not self.result:
            self.lbl_conf.config(text="")
            return
        pct = round((self.result.get("confidence", 0) or 0) * 100)
        color = COLORS["danger"]
        if pct >= 70:
            color = COLORS["success"]
        elif pct >= 40:
            color = COLORS["warning"]
        self.lbl_conf.config(text=f"{self._t('status.confidence')}: {pct}%", foreground=color)

    # ---------- export ----------
    def _copy_table(self, table_id):
        if not self.result:
            return
        headers, rows = build_table_export(self.result, table_id, self._t)
        text = to_tsv(headers, rows)
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.var_status.set(self._t("status.copied"))

    def _export_prefix(self) -> str:
        """Filename prefix reflecting the current result's chart kind."""
        r = self.result or {}
        if isinstance(r.get("abundances"), list):
            return "abundance_diagram_"
        if isinstance(r.get("sections"), list) and "species_ranges" not in r:
            return "columnar_section_"
        return "range_chart_"

    def _export_csv(self, table_id):
        if not self.result:
            return
        path = filedialog.asksaveasfilename(
            title=self._t("dialog.saveCsv"), defaultextension=".csv",
            initialfile=f"{self._export_prefix()}{table_id}.csv",
            filetypes=[("CSV", "*.csv")],
        )
        if not path:
            return
        headers, rows = build_table_export(self.result, table_id, self._t)
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(to_csv(headers, rows))
        self.var_status.set(self._t("status.saved"))

    def _export_json(self):
        if not self.result:
            return
        path = filedialog.asksaveasfilename(
            title=self._t("dialog.saveJson"), defaultextension=".json",
            initialfile=f"{self._export_prefix()}result.json",
            filetypes=[("JSON", "*.json")],
        )
        if not path:
            return
        payload = {
            "source_file": os.path.basename(self.image_path) if self.image_path else None,
            "result": self.result,
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        self.var_status.set(self._t("status.saved"))

    def on_close(self):
        # Persist settings (respecting the remember flag) on exit.
        save_config(self._collect_cfg())
        # H1: drop any leftover paste tempfile on shutdown.
        try:
            self._cleanup_paste_tmp()
        except Exception:
            pass
        # FIX (close-crash): cancel the pending _poll_queue callback so the
        # worker thread's late result doesn't get delivered to a destroyed
        # root → TclError. _poll_queue itself also guards via winfo_exists(),
        # but cancelling here closes the race cleanly.
        if getattr(self, "_poll_after_id", None) is not None:
            try:
                self.root.after_cancel(self._poll_after_id)
            except Exception:
                pass
            self._poll_after_id = None
        self.root.destroy()


def main():
    root = tk.Tk()
    app = RangeChartApp(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()


if __name__ == "__main__":
    main()

# ---------------------------------------------------------------------------
# Provider management wizard (2-step: preset grid -> details form)
# Mirrors cc-switch's AddProviderDialog + ProviderPresetSelector layout.
# ---------------------------------------------------------------------------


class ProviderWizard:
    """A modal dialog for managing LLM providers.

    cc-switch-inspired layout with a left step rail (numbered circles +
    connector line) and a right content panel:
      Step 1 (presets):  search + category chips + scrollable preset grid.
      Step 2 (details): name / endpoint / api_key / model / api_format form.
      Step 2 (list):     configured providers with delete + set-current.
    """

    STEP_PRESETS = "presets"
    STEP_DETAILS = "details"

    def __init__(self, parent, on_close=None, translate=None):
        self.on_close = on_close
        # i18n: the wizard accepts a translate function (self._t from the
        # parent app) so every visible string follows the active language.
        # Falls back to identity when no translator is passed (defensive).
        self._t = translate or (lambda key: key)
        self.result = None
        self.store = ProviderStore().load()
        self.selected_preset: ProviderPreset | None = None

        self.win = tk.Toplevel(parent)
        self.win.title(self._t("settings.llmProvider"))
        self.win.geometry("860x620")
        self.win.minsize(720, 520)
        self.win.transient(parent)
        self.win.grab_set()
        self.win.configure(bg=COLORS["bg"])

        # Wizard state.
        self.var_search = tk.StringVar()
        self.var_search.trace_add("write", lambda *_: self._render_presets())
        self.var_name = tk.StringVar()
        self.var_endpoint = tk.StringVar()
        self.var_api_key = tk.StringVar()
        self.var_model = tk.StringVar()
        self.var_format = tk.StringVar(value=ApiFormat.ANTHROPIC.value)

        # Root split: left step rail | right (header + scroll-body + footer).
        self.win.columnconfigure(1, weight=1)
        self.win.rowconfigure(0, weight=1)

        # -- Left step rail --
        rail = ttk.Frame(self.win, style="Card.TFrame", width=180)
        rail.grid(row=0, column=0, sticky="nsew", padx=(16, 0), pady=16)
        rail.grid_propagate(False)
        rail.columnconfigure(0, weight=1)
        self.lbl_rail_title = ttk.Label(
            rail, text=self._t("wizard.steps"), style="Section.TLabel")
        self.lbl_rail_title.pack(anchor="w", padx=16, pady=(16, 12))

        self._build_step_rail(rail)

        # -- Right content panel --
        right = ttk.Frame(self.win, style="TFrame")
        right.grid(row=0, column=1, sticky="nsew", padx=16, pady=16)
        right.columnconfigure(0, weight=1)
        right.rowconfigure(1, weight=1)

        self.container = ttk.Frame(right, style="Card.TFrame")
        self.container.grid(row=0, column=0, sticky="nsew")
        self.container.columnconfigure(0, weight=1)
        self.container.rowconfigure(1, weight=1)

        # Footer actions.
        footer = ttk.Frame(right, style="TFrame")
        footer.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        self.btn_cancel = ttk.Button(footer, text=self._t("wizard.cancel"), style="Tertiary.TButton", command=self._on_cancel)
        self.btn_cancel.pack(side="right", padx=(6, 0))
        self.btn_primary = ttk.Button(footer, text=self._t("wizard.add"), style="Primary.TButton", command=self._on_primary)
        self.btn_primary.pack(side="right")

        self._show_presets()

    def _build_step_rail(self, rail):
        """Two numbered steps with a connector line between them."""
        self._rail_steps = []
        steps = [
            ("1", self._t("wizard.selectPreset")),
            ("2", self._t("wizard.configure")),
        ]
        step_frame = ttk.Frame(rail, style="Card.TFrame")
        step_frame.pack(fill="x", padx=16, pady=(0, 16))

        for i, (num, label) in enumerate(steps):
            row = ttk.Frame(step_frame, style="Card.TFrame")
            row.pack(fill="x", pady=0)

            # Numbered circle (Canvas for precise color control).
            circle = tk.Canvas(row, width=28, height=28, bg=COLORS["card"], highlightthickness=0)
            circle.pack(side="left", padx=(0, 10))
            self._draw_circle(circle, num, active=(i == 0))

            lbl = ttk.Label(row, text=label, style="Card.TLabel", font=("Segoe UI", 10))
            lbl.pack(side="left")
            self._rail_steps.append((circle, lbl))

            if i < len(steps) - 1:
                # Connector line.
                connector = tk.Canvas(step_frame, width=28, height=18, bg=COLORS["card"], highlightthickness=0)
                connector.pack(anchor="w", padx=0)
                connector.create_line(13, 0, 13, 18, fill=COLORS["border"], width=2)

    @staticmethod
    def _draw_circle(circle, num, active=False):
        color = COLORS["primary"] if active else COLORS["border"]
        fg = "#ffffff" if active else COLORS["muted"]
        circle.create_oval(2, 2, 26, 26, fill=color, outline=color)
        circle.create_text(14, 15, text=num, fill=fg, font=("Segoe UI", 10, "bold"))

    def _set_active_step(self, idx):
        for i, (circle, lbl) in enumerate(self._rail_steps):
            circle.delete("all")
            self._draw_circle(circle, str(i + 1), active=(i == idx))

    # ---- top-level views ----

    def _clear_container(self):
        for child in self.container.winfo_children():
            child.destroy()

    def _show_presets(self):
        self._clear_container()
        self.selected_preset = None

        # Header.
        ttk.Label(self.container, text=self._t("wizard.choosePreset"), style="WizardTitle.TLabel").pack(anchor="w")
        ttk.Label(self.container, text=self._t("wizard.choosePresetHint"),
                  style="WizardHint.TLabel").pack(anchor="w", pady=(2, 8))

        # Search + category chip bar (cc-switch pattern: search then segmented filter).
        search_row = ttk.Frame(self.container, style="Card.TFrame")
        search_row.pack(fill="x", pady=(0, 6))
        ttk.Entry(search_row, textvariable=self.var_search).pack(fill="x")

        # Category chips (segmented control feel). Selecting a chip filters the grid.
        chip_row = ttk.Frame(self.container, style="Card.TFrame")
        chip_row.pack(fill="x", pady=(0, 8))
        # Unique categories in display order.
        cats = []
        for p in PROVIDER_PRESETS:
            if p.category not in cats:
                cats.append(p.category)
        self._preset_cats = cats
        self.var_chip = tk.StringVar(value="")
        for cat in cats:
            disp = cat.replace("_", " ").capitalize()
            ttk.Radiobutton(
                chip_row, text=disp, value=cat, variable=self.var_chip,
                command=lambda: self._render_presets(), style="Pill.TButton").pack(
                side="left", padx=(0, 4))

        # Preset grid inside a scrollable canvas.
        grid_outer = ttk.Frame(self.container, style="Card.TFrame")
        grid_outer.pack(fill="both", expand=True)

        canvas = tk.Canvas(grid_outer, bg=COLORS["card"], highlightthickness=0)
        vsb = ttk.Scrollbar(grid_outer, orient="vertical", command=canvas.yview)
        self.grid_frame = ttk.Frame(canvas, style="Card.TFrame")
        self.grid_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        _grid_win_id = canvas.create_window((0, 0), window=self.grid_frame, anchor="nw")
        canvas.configure(yscrollcommand=vsb.set)
        # Keep the inner frame width synced to the canvas width so preset
        # rows don't overflow horizontally and get clipped on the right.
        canvas.bind("<Configure>",
                     lambda e: canvas.itemconfigure(_grid_win_id, width=e.width))
        canvas.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        self._grid_canvas = canvas
        self._render_presets()

        # Custom / manual entry button.
        custom_row = ttk.Frame(self.container, style="Card.TFrame")
        custom_row.pack(fill="x", pady=(8, 0))
        ttk.Button(custom_row, text=self._t("wizard.customProvider"), style="Small.TButton",
                   command=lambda: self._show_details(preset=None)).pack(side="left")

        # Configured providers list below the presets.
        ttk.Label(self.container, text=self._t("wizard.configuredProviders"), style="WizardTitle.TLabel").pack(anchor="w", pady=(12, 4))
        self.list_frame = ttk.Frame(self.container, style="Card.TFrame")
        self.list_frame.pack(fill="x")
        self._render_provider_list()

        # Footer button label.
        self.btn_primary.config(text=self._t("wizard.add"))

    def _render_presets(self):
        for child in self.grid_frame.winfo_children():
            child.destroy()

        query = self.var_search.get().strip().lower()
        active_cat = getattr(self, "var_chip", None)
        active_cat_val = active_cat.get().strip() if active_cat else ""

        # Group presets by category (after filtering by search + chip).
        categories: dict[str, list[ProviderPreset]] = {}
        for preset in PROVIDER_PRESETS:
            if query and query not in preset.name.lower() and query not in preset.category.lower():
                continue
            if active_cat_val and preset.category != active_cat_val:
                continue
            categories.setdefault(preset.category, []).append(preset)

        if not categories:
            ttk.Label(self.grid_frame, text=self._t("wizard.noMatching"), style="WizardHint.TLabel").pack(anchor="w", pady=12)
            return

        # Layout: wrap preset buttons into rows of PRESETS_PER_ROW so they
        # don't overflow the canvas width and get clipped on the right.
        # (Previously all presets in a category were packed into a single
        #  side="left" row, which hid everything past the viewport edge.)
        PRESETS_PER_ROW = 3
        for cat, presets in categories.items():
            row = None
            for i, preset in enumerate(presets):
                if i % PRESETS_PER_ROW == 0:
                    row = ttk.Frame(self.grid_frame, style="Card.TFrame")
                    row.pack(fill="x", pady=(0, 4))
                btn = ttk.Button(row, text=preset.name, style="Preset.TButton",
                                 command=lambda p=preset: self._show_details(preset=p))
                btn.pack(side="left", padx=(0, 6), pady=(0, 4))

    def _render_provider_list(self):
        for child in self.list_frame.winfo_children():
            child.destroy()

        if not self.store.providers:
            ttk.Label(self.list_frame, text=self._t("wizard.noProviders"),
                      style="WizardHint.TLabel").pack(anchor="w")
            return

        for prov in self.store.providers:
            row = ttk.Frame(self.list_frame, style="Card.TFrame")
            row.pack(fill="x", pady=(0, 4))

            marker = "● " if prov.is_current else "○ "
            label_text = f"{marker}{prov.name}  ·  {prov.model or '-'}  ·  {prov.api_format.value}"
            ttk.Label(row, text=label_text, style="Wizard.TLabel").pack(side="left", fill="x", expand=True)

            if not prov.is_current:
                ttk.Button(row, text=self._t("wizard.setActive"), style="Small.TButton",
                           command=lambda pid=prov.id: self._set_current(pid)).pack(side="right", padx=(4, 0))
            ttk.Button(row, text=self._t("wizard.delete"), style="Small.TButton",
                       command=lambda pid=prov.id: self._delete_provider(pid)).pack(side="right")

    def _show_details(self, preset: ProviderPreset | None):
        self._clear_container()
        self.selected_preset = preset

        title = self._t("wizard.configureName").format(name=preset.name) if preset else self._t("wizard.customProvider")
        ttk.Label(self.container, text=title, style="WizardTitle.TLabel").pack(anchor="w")
        ttk.Label(self.container, text=self._t("wizard.detailsHint"),
                  style="WizardHint.TLabel").pack(anchor="w", pady=(2, 8))

        # Form.
        form = ttk.Frame(self.container, style="Card.TFrame")
        form.pack(fill="both", expand=True)

        # Name.
        ttk.Label(form, text=self._t("wizard.fieldName"), style="Wizard.TLabel").pack(anchor="w")
        self.var_name.set(preset.name if preset else "My Provider")
        ttk.Entry(form, textvariable=self.var_name).pack(fill="x", pady=(0, 8))

        # API format.
        ttk.Label(form, text=self._t("wizard.fieldFormat"), style="Wizard.TLabel").pack(anchor="w")
        self.var_format.set(preset.api_format.value if preset else ApiFormat.ANTHROPIC.value)
        fmt_combo = ttk.Combobox(form, textvariable=self.var_format, state="readonly",
                                 values=[f.value for f in ApiFormat])
        fmt_combo.pack(fill="x", pady=(0, 8))

        # Endpoint.
        ttk.Label(form, text=self._t("wizard.fieldEndpoint"), style="Wizard.TLabel").pack(anchor="w")
        self.var_endpoint.set(preset.endpoint if preset else "https://")
        ttk.Entry(form, textvariable=self.var_endpoint).pack(fill="x", pady=(0, 8))

        # API Key.
        ttk.Label(form, text=self._t("settings.apiKey"), style="Wizard.TLabel").pack(anchor="w")
        self.var_api_key.set("")
        ttk.Entry(form, textvariable=self.var_api_key, show="*").pack(fill="x", pady=(0, 8))

        # Model.
        ttk.Label(form, text=self._t("settings.model"), style="Wizard.TLabel").pack(anchor="w")
        self.var_model.set(preset.model if preset else "")
        model_combo = ttk.Combobox(form, textvariable=self.var_model)
        model_combo.pack(fill="x", pady=(0, 8))

        # Footer button.
        self.btn_primary.config(text=self._t("settings.save"))

    def _on_primary(self):
        # Only the details step has a meaningful primary action; presets step does nothing.
        if self.selected_preset is None and self._current_step() == self.STEP_PRESETS:
            return
        name = self.var_name.get().strip()
        endpoint = self.var_endpoint.get().strip().rstrip("/")
        api_key = self.var_api_key.get().strip()
        model = self.var_model.get().strip()
        try:
            fmt = ApiFormat(self.var_format.get())
        except ValueError:
            fmt = ApiFormat.ANTHROPIC
        if not name or not endpoint:
            return

        provider = LlmProvider(
            name=name, api_format=fmt, endpoint=endpoint,
            api_key=api_key, model=model,
        )
        self.store.add(provider)
        self.store.set_current(provider.id)
        self._show_presets()
        self._render_provider_list()

    def _set_current(self, provider_id: str):
        self.store.set_current(provider_id)
        self._render_provider_list()
        if self.on_close:
            self.on_close()

    def _delete_provider(self, provider_id: str):
        # Clear stale test state so a fresh test can be started immediately.
        if getattr(self, "_testing_id", None) == provider_id:
            self._testing_id = None
        self.store.remove(provider_id)
        self._render_provider_list()
        if self.on_close:
            self.on_close()

    def _on_cancel(self):
        if self.on_close:
            self.on_close()
        self.win.destroy()

    def _current_step(self) -> str:
        return self.STEP_PRESETS if self.selected_preset is None else self.STEP_DETAILS
