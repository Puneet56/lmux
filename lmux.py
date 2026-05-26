#!/usr/bin/env python3
"""lmux — a tiny Linux take on cmux's tab UI.

Vertical workspace sidebar + horizontal terminal tabs with splits.
"""
from __future__ import annotations

import os
import sys
from collections import deque
from urllib.parse import unquote, urlparse

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Vte", "3.91")
from gi.repository import Gdk, Gio, GLib, Gtk, Pango, Vte  # noqa: E402

APP_ID = "dev.lmux.Lmux"
FONT = "monospace 11"
SCROLLBACK = 10_000
SIDEBAR_WIDTH = 220
URL_PATTERN = (
    r"(?:https?|ftp|file)://"
    r"[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+"
)
CLOSED_TAB_HISTORY = 16


def parse_cwd_uri(uri: str | None) -> str | None:
    if not uri:
        return None
    p = urlparse(uri)
    if p.scheme != "file":
        return None
    return unquote(p.path) or None


KITTY_USER_CONF = os.path.expanduser("~/.config/kitty/kitty.conf")
OMARCHY_THEME_NAME = os.path.expanduser("~/.config/omarchy/current/theme.name")


def parse_kitty_conf(path: str, _seen: set[str] | None = None) -> dict[str, str]:
    """Parse kitty.conf into a flat dict, resolving `include` recursively."""
    _seen = _seen or set()
    path = os.path.realpath(os.path.expanduser(path))
    if path in _seen or not os.path.exists(path):
        return {}
    _seen.add(path)
    result: dict[str, str] = {}
    base = os.path.dirname(path)
    try:
        with open(path) as f:
            lines = f.readlines()
    except OSError:
        return {}
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        key, val = parts[0], parts[1].strip()
        if key == "include":
            inc = os.path.expanduser(val)
            if not os.path.isabs(inc):
                inc = os.path.join(base, inc)
            for k, v in parse_kitty_conf(inc, _seen).items():
                result[k] = v
        else:
            result[key] = val
    return result


def _parse_color(s: str | None):
    if not s:
        return None
    rgba = Gdk.RGBA()
    if rgba.parse(s.strip()):
        return rgba
    return None


def apply_kitty_theme(term: "Vte.Terminal", cfg: dict[str, str]) -> None:
    fg = _parse_color(cfg.get("foreground"))
    bg = _parse_color(cfg.get("background"))
    palette = []
    for i in range(16):
        c = _parse_color(cfg.get(f"color{i}"))
        if c is not None:
            palette.append(c)
    if len(palette) == 16:
        term.set_colors(fg, bg, palette)
    elif fg or bg:
        term.set_colors(fg, bg, None)
    cur = _parse_color(cfg.get("cursor"))
    if cur is not None:
        term.set_color_cursor(cur)
    cur_fg = _parse_color(cfg.get("cursor_text_color"))
    if cur_fg is not None:
        term.set_color_cursor_foreground(cur_fg)
    sel_fg = _parse_color(cfg.get("selection_foreground"))
    sel_bg = _parse_color(cfg.get("selection_background"))
    if sel_bg is not None:
        term.set_color_highlight(sel_bg)
    if sel_fg is not None:
        term.set_color_highlight_foreground(sel_fg)

    family = cfg.get("font_family")
    size = cfg.get("font_size")
    if family or size:
        desc_str = family or "monospace"
        if size:
            try:
                desc_str = f"{desc_str} {float(size)}"
            except ValueError:
                pass
        term.set_font(Pango.FontDescription.from_string(desc_str))

    shape_map = {
        "block": Vte.CursorShape.BLOCK,
        "beam": Vte.CursorShape.IBEAM,
        "underline": Vte.CursorShape.UNDERLINE,
    }
    shape = (cfg.get("cursor_shape") or "").lower()
    if shape in shape_map:
        term.set_cursor_shape(shape_map[shape])

    blink = (cfg.get("cursor_blink_interval") or "").strip()
    if blink == "0":
        term.set_cursor_blink_mode(Vte.CursorBlinkMode.OFF)

    bell = (cfg.get("enable_audio_bell") or "yes").lower()
    term.set_audible_bell(bell in ("yes", "true", "1"))


class Theme:
    """Tracks the kitty config + reloads on theme switch."""

    def __init__(self, on_change):
        self.cfg: dict[str, str] = {}
        self.on_change = on_change
        self._pending = 0
        self._monitors: list[Gio.FileMonitor] = []
        self.reload()
        self._watch(KITTY_USER_CONF)
        self._watch(OMARCHY_THEME_NAME)

    def reload(self):
        self.cfg = parse_kitty_conf(KITTY_USER_CONF)

    def _watch(self, path: str):
        f = Gio.File.new_for_path(path)
        try:
            m = f.monitor_file(Gio.FileMonitorFlags.NONE, None)
        except GLib.Error:
            return
        m.connect("changed", self._on_file_event)
        self._monitors.append(m)

    def _on_file_event(self, *_):
        if self._pending:
            GLib.source_remove(self._pending)
        self._pending = GLib.timeout_add(150, self._fire)

    def _fire(self):
        self._pending = 0
        self.reload()
        if self.on_change:
            self.on_change(self.cfg)
        return False


def git_branch(cwd: str | None) -> str | None:
    if not cwd:
        return None
    d = os.path.abspath(cwd)
    for _ in range(40):
        head = os.path.join(d, ".git", "HEAD")
        try:
            with open(head, "r") as f:
                line = f.readline().strip()
        except OSError:
            parent = os.path.dirname(d)
            if parent == d:
                return None
            d = parent
            continue
        if line.startswith("ref: refs/heads/"):
            return line[len("ref: refs/heads/"):]
        return line[:7] if line else None
    return None


class Pane(Gtk.Box):
    """One terminal."""

    def __init__(
        self,
        cwd: str | None = None,
        theme_cfg: dict[str, str] | None = None,
        font_scale: float = 1.0,
    ):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.term = Vte.Terminal.new()
        self.term.set_hexpand(True)
        self.term.set_vexpand(True)
        self.term.set_font(Pango.FontDescription.from_string(FONT))
        self.term.set_scrollback_lines(SCROLLBACK)
        self.term.set_mouse_autohide(True)
        self.term.set_audible_bell(True)
        self.term.set_font_scale(font_scale)
        self.append(self.term)

        start_cwd = cwd or os.path.expanduser("~")
        self.cwd: str | None = start_cwd
        self._wm_title: str | None = None
        self.last_notification: str | None = None

        self.on_changed = None
        self.on_bell = None
        self.on_exited = None
        self.on_focused = None

        self.term.connect("window-title-changed", self._on_wm_title)
        self.term.connect("current-directory-uri-changed", self._on_cwd_uri)
        self.term.connect("bell", self._on_bell)
        self.term.connect("child-exited", self._on_exited)
        try:
            self.term.connect("notification-received", self._on_notification)
        except TypeError:
            pass

        focus_ctrl = Gtk.EventControllerFocus.new()
        focus_ctrl.connect("enter", self._on_focus_enter)
        self.term.add_controller(focus_ctrl)

        self._url_tag = self._install_url_matcher()
        self._install_url_click()

        shell = os.environ.get("SHELL", "/bin/bash")
        self.term.spawn_async(
            Vte.PtyFlags.DEFAULT,
            start_cwd,
            [shell],
            [],
            GLib.SpawnFlags.DEFAULT,
            None,
            None,
            -1,
            None,
            None,
            None,
        )

        self._cwd_poll_id = GLib.timeout_add(1200, self._poll_cwd)

        if theme_cfg:
            apply_kitty_theme(self.term, theme_cfg)

    def _install_url_matcher(self) -> int:
        try:
            # PCRE2_UTF (0x00080000) | PCRE2_MULTILINE (0x00000400)
            regex = Vte.Regex.new_for_match(URL_PATTERN, -1, 0x00080400)
        except (TypeError, GLib.Error):
            return -1
        try:
            tag = self.term.match_add_regex(regex, 0)
            self.term.match_set_cursor_name(tag, "pointer")
            return tag
        except (TypeError, GLib.Error):
            return -1

    def _install_url_click(self):
        gesture = Gtk.GestureClick.new()
        gesture.set_button(0)
        gesture.connect("pressed", self._on_term_click)
        self.term.add_controller(gesture)

    def _on_term_click(self, gesture, n_press, x, y):
        state = gesture.get_current_event_state()
        if not (state & Gdk.ModifierType.CONTROL_MASK):
            return
        if self._url_tag < 0:
            return
        try:
            match, _tag = self.term.check_match_at(x, y)
        except Exception:
            return
        if not match:
            return
        try:
            Gio.Subprocess.new(
                ["xdg-open", match],
                Gio.SubprocessFlags.STDOUT_SILENCE | Gio.SubprocessFlags.STDERR_SILENCE,
            )
        except GLib.Error:
            pass

    def apply_theme(self, cfg: dict[str, str]):
        apply_kitty_theme(self.term, cfg)

    def set_font_scale(self, scale: float):
        self.term.set_font_scale(scale)

    def _poll_cwd(self) -> bool:
        pty = self.term.get_pty()
        if pty is None:
            return True
        fd = pty.get_fd()
        if fd < 0:
            return False
        try:
            pgrp = os.tcgetpgrp(fd)
        except OSError:
            return False
        if pgrp <= 0:
            return True
        try:
            new = os.readlink(f"/proc/{pgrp}/cwd")
        except OSError:
            return True
        if new and new != self.cwd:
            self.cwd = new
            if self.on_changed:
                self.on_changed(self)
        return True

    @property
    def title(self) -> str:
        if self._wm_title:
            return self._wm_title
        if self.cwd:
            base = os.path.basename(self.cwd.rstrip("/"))
            if base:
                return base
        return "shell"

    def _on_wm_title(self, term):
        self._wm_title = term.get_window_title() or None
        if self.on_changed:
            self.on_changed(self)

    def _on_cwd_uri(self, term):
        uri = term.get_current_directory_uri()
        cwd = parse_cwd_uri(uri)
        if cwd:
            self.cwd = cwd
            if self.on_changed:
                self.on_changed(self)

    def _foreground_is_claude(self) -> bool:
        pty = self.term.get_pty()
        if pty is None:
            return False
        fd = pty.get_fd()
        if fd < 0:
            return False
        try:
            pgrp = os.tcgetpgrp(fd)
        except OSError:
            return False
        if pgrp <= 0:
            return False
        try:
            with open(f"/proc/{pgrp}/comm") as f:
                comm = f.read().strip()
        except OSError:
            return False
        return "claude" in comm

    def _on_bell(self, term):
        if not self._foreground_is_claude():
            return
        if self.on_bell:
            self.on_bell(self, None, None)

    def _on_notification(self, term, summary, body):
        self.last_notification = body or summary or None
        if self.on_changed:
            self.on_changed(self)
        if not self._foreground_is_claude():
            return
        if self.on_bell:
            self.on_bell(self, summary, body)

    def _on_exited(self, term, status):
        if self._cwd_poll_id:
            GLib.source_remove(self._cwd_poll_id)
            self._cwd_poll_id = None
        if self.on_exited:
            self.on_exited(self)

    def _on_focus_enter(self, _ctrl):
        if self.on_focused:
            self.on_focused(self)

    def copy(self):
        self.term.copy_clipboard_format(Vte.Format.TEXT)

    def paste(self):
        self.term.paste_clipboard()

    def focus_term(self):
        self.term.grab_focus()


class TabLabel(Gtk.Box):
    """Tab label: notification dot + title. Close via Ctrl+Shift+Q."""

    def __init__(self, title: str, on_close):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self.dot = Gtk.Label(label="●")
        self.dot.add_css_class("lmux-bell")
        self.dot.set_visible(False)
        self.append(self.dot)

        self.label = Gtk.Label(label=title)
        self.label.set_xalign(0)
        self.label.set_ellipsize(Pango.EllipsizeMode.END)
        self.label.set_width_chars(10)
        self.label.set_max_width_chars(22)
        self.append(self.label)

    def set_title(self, title: str):
        self.label.set_text(title)

    def set_notification(self, on: bool):
        self.dot.set_visible(on)


class WorkspaceRow(Gtk.ListBoxRow):
    """Sidebar entry: name on top, cwd · branch underneath, notification dot."""

    def __init__(self, name: str):
        super().__init__()
        outer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        outer.set_margin_top(3)
        outer.set_margin_bottom(3)
        outer.set_margin_start(8)
        outer.set_margin_end(8)

        self.dot = Gtk.Label(label="●")
        self.dot.add_css_class("lmux-bell")
        self.dot.set_visible(False)
        self.dot.set_valign(Gtk.Align.CENTER)
        outer.append(self.dot)

        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        text.set_hexpand(True)

        self.name_label = Gtk.Label(label=name)
        self.name_label.set_xalign(0)
        self.name_label.set_ellipsize(Pango.EllipsizeMode.END)
        text.append(self.name_label)

        self.sub_label = Gtk.Label(label="")
        self.sub_label.set_xalign(0)
        self.sub_label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        self.sub_label.add_css_class("dim-label")
        self.sub_label.add_css_class("caption")
        text.append(self.sub_label)

        outer.append(text)
        self.set_child(outer)

    def set_metadata(self, cwd: str | None, branch: str | None):
        base = os.path.basename(cwd.rstrip("/")) if cwd else ""
        if base and branch:
            self.sub_label.set_text(f"{base}  ⎇ {branch}")
        elif branch:
            self.sub_label.set_text(f"⎇ {branch}")
        elif base:
            self.sub_label.set_text(base)
        else:
            self.sub_label.set_text("")

    def set_notification(self, on: bool):
        self.dot.set_visible(on)


def _make_paned(orientation: Gtk.Orientation) -> Gtk.Paned:
    p = Gtk.Paned(orientation=orientation)
    p.set_resize_start_child(True)
    p.set_resize_end_child(True)
    p.set_shrink_start_child(False)
    p.set_shrink_end_child(False)
    p.set_wide_handle(True)
    p.set_hexpand(True)
    p.set_vexpand(True)
    return p


def _find_any_pane(w) -> Pane | None:
    if isinstance(w, Pane):
        return w
    if isinstance(w, Gtk.Paned):
        for child in (w.get_start_child(), w.get_end_child()):
            if child is not None:
                r = _find_any_pane(child)
                if r is not None:
                    return r
    return None


class TabRoot(Gtk.Box):
    """A notebook tab's root. Holds one Pane or a Gtk.Paned tree of Panes."""

    def __init__(self, pane: Pane):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.active_pane: Pane = pane
        self.on_active_changed = None  # (tab_root, pane)
        self.append(pane)

    def panes(self) -> list[Pane]:
        result: list[Pane] = []

        def walk(w):
            if isinstance(w, Pane):
                result.append(w)
            elif isinstance(w, Gtk.Paned):
                for c in (w.get_start_child(), w.get_end_child()):
                    if c is not None:
                        walk(c)

        w = self.get_first_child()
        while w is not None:
            walk(w)
            w = w.get_next_sibling()
        return result

    def set_active(self, pane: Pane):
        if pane is self.active_pane:
            return
        self.active_pane = pane
        if self.on_active_changed:
            self.on_active_changed(self, pane)

    def split(self, new_pane: Pane, orientation: Gtk.Orientation):
        active = self.active_pane
        parent = active.get_parent()
        paned = _make_paned(orientation)
        if isinstance(parent, Gtk.Paned):
            is_start = parent.get_start_child() is active
            if is_start:
                parent.set_start_child(None)
            else:
                parent.set_end_child(None)
            paned.set_start_child(active)
            paned.set_end_child(new_pane)
            if is_start:
                parent.set_start_child(paned)
            else:
                parent.set_end_child(paned)
        elif parent is self:
            self.remove(active)
            paned.set_start_child(active)
            paned.set_end_child(new_pane)
            self.append(paned)
        else:
            return
        self.set_active(new_pane)
        GLib.idle_add(new_pane.focus_term)

    def close_pane(self, pane: Pane) -> bool:
        """Return True if tab is now empty and should close."""
        parent = pane.get_parent()
        if isinstance(parent, Gtk.Paned):
            start = parent.get_start_child()
            end = parent.get_end_child()
            sibling = end if pane is start else start
            parent.set_start_child(None)
            parent.set_end_child(None)
            gp = parent.get_parent()
            if isinstance(gp, Gtk.Paned):
                gp_is_start = gp.get_start_child() is parent
                if gp_is_start:
                    gp.set_start_child(None)
                    gp.set_start_child(sibling)
                else:
                    gp.set_end_child(None)
                    gp.set_end_child(sibling)
            elif gp is self:
                self.remove(parent)
                if sibling is not None:
                    self.append(sibling)
            new_active = _find_any_pane(sibling) if sibling is not None else None
            if new_active is not None:
                self.set_active(new_active)
                GLib.idle_add(new_active.focus_term)
            return False
        elif parent is self:
            self.remove(pane)
            return True
        return False

    def focus_direction(self, dx: int, dy: int):
        """Move focus to the spatially nearest pane in the given direction."""
        active = self.active_pane
        a_alloc = active.get_allocation()
        if a_alloc.width <= 0:
            return
        cx = a_alloc.x + a_alloc.width // 2
        cy = a_alloc.y + a_alloc.height // 2
        best = None
        best_dist = None
        for p in self.panes():
            if p is active:
                continue
            al = p.get_allocation()
            px = al.x + al.width // 2
            py = al.y + al.height // 2
            ddx = px - cx
            ddy = py - cy
            if dx > 0 and ddx <= 0:
                continue
            if dx < 0 and ddx >= 0:
                continue
            if dy > 0 and ddy <= 0:
                continue
            if dy < 0 and ddy >= 0:
                continue
            dist = ddx * ddx + ddy * ddy
            if best_dist is None or dist < best_dist:
                best_dist = dist
                best = p
        if best is not None:
            best.focus_term()

    def equalize(self):
        def walk(w):
            if isinstance(w, Gtk.Paned):
                alloc = w.get_allocation()
                if w.get_orientation() == Gtk.Orientation.HORIZONTAL:
                    size = alloc.width
                else:
                    size = alloc.height
                if size > 0:
                    w.set_position(size // 2)
                for c in (w.get_start_child(), w.get_end_child()):
                    if c is not None:
                        walk(c)

        w = self.get_first_child()
        while w is not None:
            walk(w)
            w = w.get_next_sibling()


class Workspace:
    """A workspace = one row in the vertical sidebar; owns a Notebook of TabRoots."""

    def __init__(
        self,
        name: str,
        on_empty,
        on_current_pane_changed,
        on_bell,
        on_tab_closed,
        theme_cfg=None,
        font_scale: float = 1.0,
    ):
        self.name = name
        self.on_empty = on_empty
        self.on_current_pane_changed = on_current_pane_changed
        self.on_bell = on_bell
        self.on_tab_closed = on_tab_closed
        self.theme_cfg = theme_cfg or {}
        self.font_scale = font_scale
        self._tabs: dict[TabRoot, TabLabel] = {}
        self._notif: set[TabRoot] = set()
        self.notebook = Gtk.Notebook()
        self.notebook.set_scrollable(True)
        self.notebook.set_show_border(False)
        self.notebook.set_hexpand(True)
        self.notebook.set_vexpand(True)
        self.notebook.connect("switch-page", self._on_switch_page)
        self.add_tab()

    def tabs(self) -> list[TabRoot]:
        return list(self._tabs.keys())

    def add_tab(self, cwd: str | None = None):
        pane = self._make_pane(cwd)
        tab_root = TabRoot(pane)
        tab_root.on_active_changed = self._on_active_changed
        label = TabLabel(pane.title, on_close=lambda: self._close_tab(tab_root))
        self._tabs[tab_root] = label
        self._wire_pane(pane, tab_root)
        self.notebook.append_page(tab_root, label)
        self.notebook.set_tab_reorderable(tab_root, True)
        self.notebook.set_current_page(self.notebook.get_n_pages() - 1)
        GLib.idle_add(pane.focus_term)

    def _make_pane(self, cwd: str | None) -> Pane:
        return Pane(cwd=cwd, theme_cfg=self.theme_cfg, font_scale=self.font_scale)

    def _wire_pane(self, pane: Pane, tab_root: TabRoot):
        def changed(p):
            label = self._tabs.get(tab_root)
            if label and tab_root.active_pane is p:
                label.set_title(p.title)
            if self.current_tab_root() is tab_root and tab_root.active_pane is p:
                self.on_current_pane_changed(self, p)

        pane.on_changed = changed
        pane.on_bell = lambda p, s, b: self.on_bell(self, tab_root, p, s, b)
        pane.on_exited = lambda p: self._on_pane_exit(tab_root, p)
        pane.on_focused = lambda p: tab_root.set_active(p)

    def _on_active_changed(self, tab_root: TabRoot, pane: Pane):
        label = self._tabs.get(tab_root)
        if label:
            label.set_title(pane.title)
        if self.current_tab_root() is tab_root:
            self.on_current_pane_changed(self, pane)

    def _on_pane_exit(self, tab_root: TabRoot, pane: Pane):
        empty = tab_root.close_pane(pane)
        if empty:
            self._close_tab(tab_root)

    def _close_tab(self, tab_root: TabRoot):
        # Capture cwd of last-active pane for restore
        cwd = tab_root.active_pane.cwd if tab_root.active_pane else None
        n = self.notebook.page_num(tab_root)
        if n != -1:
            self.notebook.remove_page(n)
        self._tabs.pop(tab_root, None)
        self._notif.discard(tab_root)
        if cwd and self.on_tab_closed:
            self.on_tab_closed(cwd)
        if self.notebook.get_n_pages() == 0:
            self.on_empty(self)

    def mark_bell(self, tab_root: TabRoot):
        if tab_root in self._tabs:
            self._notif.add(tab_root)
            self._tabs[tab_root].set_notification(True)

    def clear_bell(self, tab_root: TabRoot):
        if tab_root in self._notif:
            self._notif.discard(tab_root)
            lbl = self._tabs.get(tab_root)
            if lbl:
                lbl.set_notification(False)

    def has_bell(self) -> bool:
        return bool(self._notif)

    def _on_switch_page(self, _nb, page_widget, _idx):
        if isinstance(page_widget, TabRoot):
            self.clear_bell(page_widget)
            self.on_current_pane_changed(self, page_widget.active_pane)
            GLib.idle_add(page_widget.active_pane.focus_term)

    def apply_theme(self, cfg: dict[str, str]):
        self.theme_cfg = cfg
        for tab_root in self._tabs:
            for p in tab_root.panes():
                p.apply_theme(cfg)

    def apply_font_scale(self, scale: float):
        self.font_scale = scale
        for tab_root in self._tabs:
            for p in tab_root.panes():
                p.set_font_scale(scale)

    def close_current_pane(self):
        tr = self.current_tab_root()
        if not tr:
            return
        pane = tr.active_pane
        empty = tr.close_pane(pane)
        if empty:
            self._close_tab(tr)

    def split(self, orientation: Gtk.Orientation):
        tr = self.current_tab_root()
        if not tr:
            return
        cwd = tr.active_pane.cwd if tr.active_pane else None
        new = self._make_pane(cwd)
        self._wire_pane(new, tr)
        tr.split(new, orientation)

    def equalize(self):
        tr = self.current_tab_root()
        if tr:
            tr.equalize()

    def focus_direction(self, dx: int, dy: int):
        tr = self.current_tab_root()
        if tr:
            tr.focus_direction(dx, dy)

    def current_tab_root(self) -> TabRoot | None:
        n = self.notebook.get_current_page()
        if n == -1:
            return None
        w = self.notebook.get_nth_page(n)
        return w if isinstance(w, TabRoot) else None

    def current_pane(self) -> Pane | None:
        tr = self.current_tab_root()
        return tr.active_pane if tr else None

    def next_tab(self):
        n = self.notebook.get_n_pages()
        if n <= 1:
            return
        cur = self.notebook.get_current_page()
        self.notebook.set_current_page((cur + 1) % n)

    def prev_tab(self):
        n = self.notebook.get_n_pages()
        if n <= 1:
            return
        cur = self.notebook.get_current_page()
        self.notebook.set_current_page((cur - 1) % n)

    def select_tab(self, idx: int):
        if 0 <= idx < self.notebook.get_n_pages():
            self.notebook.set_current_page(idx)


class LmuxWindow(Gtk.ApplicationWindow):
    def __init__(self, app: Gtk.Application):
        super().__init__(application=app, title="lmux")
        self.set_default_size(1280, 800)
        self._ws_counter = 0
        self.workspaces: list[Workspace] = []
        self._rows: dict[Workspace, WorkspaceRow] = {}
        self._font_scale = 1.0
        self._closed_cwds: deque[str] = deque(maxlen=CLOSED_TAB_HISTORY)
        self.theme = Theme(on_change=self._apply_theme_all)

        self.main_paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self.main_paned.set_position(SIDEBAR_WIDTH)
        self.main_paned.set_resize_start_child(False)
        self.main_paned.set_shrink_start_child(False)

        self.sidebar = self._build_sidebar()
        self.main_paned.set_start_child(self.sidebar)

        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.NONE)
        self.main_paned.set_end_child(self.stack)

        self.set_child(self.main_paned)

        self._install_actions(app)
        self.new_workspace()

    def _build_sidebar(self) -> Gtk.Box:
        sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        sidebar.add_css_class("sidebar")

        new_btn = Gtk.Button(label="+ workspace")
        new_btn.add_css_class("flat")
        new_btn.set_tooltip_text("New workspace (Ctrl+Shift+W)")
        new_btn.set_margin_top(4)
        new_btn.set_margin_bottom(2)
        new_btn.set_margin_start(6)
        new_btn.set_margin_end(6)
        new_btn.connect("clicked", lambda _b: self.new_workspace())
        sidebar.append(new_btn)

        self.sidebar_list = Gtk.ListBox()
        self.sidebar_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.sidebar_list.connect("row-selected", self._on_sidebar_select)
        self.sidebar_list.add_css_class("navigation-sidebar")

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_child(self.sidebar_list)
        scroll.set_vexpand(True)
        sidebar.append(scroll)
        return sidebar

    def _set_sidebar_visible(self, visible: bool):
        self.sidebar.set_visible(visible)
        self.main_paned.set_position(SIDEBAR_WIDTH if visible else 0)

    def _toggle_sidebar(self):
        self._set_sidebar_visible(not self.sidebar.get_visible())

    # --- workspace management ---

    def new_workspace(self):
        self._ws_counter += 1
        name = f"ws-{self._ws_counter}"
        ws = Workspace(
            name,
            on_empty=self._remove_workspace,
            on_current_pane_changed=self._on_current_pane_changed,
            on_bell=self._on_bell,
            on_tab_closed=self._on_tab_closed,
            theme_cfg=self.theme.cfg,
            font_scale=self._font_scale,
        )
        self.workspaces.append(ws)
        self.stack.add_named(ws.notebook, name)

        row = WorkspaceRow(name)
        row.workspace = ws  # type: ignore[attr-defined]
        self._rows[ws] = row
        self.sidebar_list.append(row)
        self.sidebar_list.select_row(row)

        pane = ws.current_pane()
        if pane is not None:
            self._refresh_row(ws, pane)

    def _remove_workspace(self, ws: Workspace):
        if ws not in self.workspaces:
            return
        self.workspaces.remove(ws)
        self.stack.remove(ws.notebook)
        row = self._rows.pop(ws, None)
        if row is not None:
            self.sidebar_list.remove(row)
        if not self.workspaces:
            self.new_workspace()
        else:
            first = self.sidebar_list.get_row_at_index(0)
            if first is not None:
                self.sidebar_list.select_row(first)

    def _on_sidebar_select(self, _list, row):
        if row is None:
            return
        ws: Workspace = row.workspace
        self.stack.set_visible_child_name(ws.name)
        tr = ws.current_tab_root()
        if tr is not None:
            ws.clear_bell(tr)
            row.set_notification(ws.has_bell())
            GLib.idle_add(tr.active_pane.focus_term)

    def _refresh_row(self, ws: Workspace, pane: Pane):
        row = self._rows.get(ws)
        if row is None:
            return
        row.set_metadata(pane.cwd, git_branch(pane.cwd))

    def _on_current_pane_changed(self, ws: Workspace, pane: Pane):
        self._refresh_row(ws, pane)

    def _on_tab_closed(self, cwd: str):
        self._closed_cwds.append(cwd)

    def _is_visible(self, ws: Workspace, tab_root: TabRoot, pane: Pane) -> bool:
        if self.stack.get_visible_child_name() != ws.name:
            return False
        if ws.current_tab_root() is not tab_root:
            return False
        return tab_root.active_pane is pane

    def _apply_theme_all(self, cfg: dict[str, str]):
        for ws in self.workspaces:
            ws.apply_theme(cfg)

    def _on_bell(
        self,
        ws: Workspace,
        tab_root: TabRoot,
        pane: Pane,
        summary: str | None,
        body: str | None,
    ):
        self._play_bell_sound()
        focused_here = self._is_visible(ws, tab_root, pane) and self.is_active()
        if not focused_here:
            ws.mark_bell(tab_root)
            row = self._rows.get(ws)
            if row is not None:
                row.set_notification(True)
            self._send_desktop_notification(ws, pane, summary, body)

    def _play_bell_sound(self):
        for argv in (
            ["canberra-gtk-play", "-i", "message-new-instant", "--description=lmux"],
            ["paplay", "/usr/share/sounds/freedesktop/stereo/message-new-instant.oga"],
        ):
            try:
                Gio.Subprocess.new(
                    argv,
                    Gio.SubprocessFlags.STDOUT_SILENCE | Gio.SubprocessFlags.STDERR_SILENCE,
                )
                return
            except GLib.Error:
                continue

    def _send_desktop_notification(
        self, ws: Workspace, pane: Pane, summary: str | None, body: str | None
    ):
        app = self.get_application()
        if app is None:
            return
        notif = Gio.Notification.new(summary or f"lmux · {pane.title}")
        text = body or pane.last_notification or f"Activity in {ws.name}"
        notif.set_body(text)
        notif.set_priority(Gio.NotificationPriority.NORMAL)
        app.send_notification(f"lmux-{ws.name}-{id(pane)}", notif)

    def _current_workspace(self) -> Workspace | None:
        row = self.sidebar_list.get_selected_row()
        if row is None:
            return None
        return row.workspace

    # --- actions ---

    def _install_actions(self, app: Gtk.Application):
        def add(name: str, fn, accels: list[str]):
            act = Gio.SimpleAction.new(name, None)
            act.connect("activate", lambda *_: fn())
            self.add_action(act)
            app.set_accels_for_action(f"win.{name}", accels)

        add("new-tab", self._new_tab, ["<Ctrl><Shift>t"])
        add("new-workspace", self.new_workspace, ["<Ctrl><Shift>w"])
        add("close-pane", self._close_pane, ["<Ctrl><Shift>q"])
        add("restore-tab", self._restore_closed_tab, ["<Ctrl><Shift>z"])

        add("split-right", self._split_right, ["<Ctrl><Shift>d"])
        add("split-down", self._split_down, ["<Ctrl><Shift>e"])
        add("equalize", self._equalize, ["<Ctrl><Shift>0"])

        add("focus-left", lambda: self._focus_dir(-1, 0), ["<Alt>Left"])
        add("focus-right", lambda: self._focus_dir(1, 0), ["<Alt>Right"])
        add("focus-up", lambda: self._focus_dir(0, -1), ["<Alt>Up"])
        add("focus-down", lambda: self._focus_dir(0, 1), ["<Alt>Down"])

        add("next-tab", self._next_tab, ["<Ctrl>Tab", "<Ctrl>Page_Down"])
        add("prev-tab", self._prev_tab, ["<Ctrl><Shift>Tab", "<Ctrl>Page_Up"])
        add("next-workspace", self._next_workspace, ["<Ctrl><Alt>Down"])
        add("prev-workspace", self._prev_workspace, ["<Ctrl><Alt>Up"])

        for i in range(1, 10):
            add(
                f"select-tab-{i}",
                lambda i=i: self._select_tab(i - 1),
                [f"<Ctrl>{i}"],
            )
            add(
                f"select-workspace-{i}",
                lambda i=i: self._select_workspace(i - 1),
                [f"<Alt>{i}"],
            )

        add("toggle-sidebar", self._toggle_sidebar, ["<Ctrl>b"])

        add("zoom-in", lambda: self._zoom(0.1), ["<Ctrl>equal", "<Ctrl>plus"])
        add("zoom-out", lambda: self._zoom(-0.1), ["<Ctrl>minus"])
        add("zoom-reset", self._zoom_reset, ["<Ctrl>0"])

        add("copy", self._copy, ["<Ctrl><Shift>c"])
        add("paste", self._paste, ["<Ctrl><Shift>v"])

    def _new_tab(self):
        ws = self._current_workspace()
        if not ws:
            return
        cwd = None
        pane = ws.current_pane()
        if pane is not None:
            cwd = pane.cwd
        ws.add_tab(cwd=cwd)

    def _close_pane(self):
        ws = self._current_workspace()
        if ws:
            ws.close_current_pane()

    def _restore_closed_tab(self):
        if not self._closed_cwds:
            return
        cwd = self._closed_cwds.pop()
        ws = self._current_workspace()
        if ws:
            ws.add_tab(cwd=cwd)

    def _split_right(self):
        ws = self._current_workspace()
        if ws:
            ws.split(Gtk.Orientation.HORIZONTAL)

    def _split_down(self):
        ws = self._current_workspace()
        if ws:
            ws.split(Gtk.Orientation.VERTICAL)

    def _equalize(self):
        ws = self._current_workspace()
        if ws:
            ws.equalize()

    def _focus_dir(self, dx: int, dy: int):
        ws = self._current_workspace()
        if ws:
            ws.focus_direction(dx, dy)

    def _next_tab(self):
        ws = self._current_workspace()
        if ws:
            ws.next_tab()

    def _prev_tab(self):
        ws = self._current_workspace()
        if ws:
            ws.prev_tab()

    def _select_tab(self, idx: int):
        ws = self._current_workspace()
        if ws:
            ws.select_tab(idx)

    def _select_workspace(self, idx: int):
        row = self.sidebar_list.get_row_at_index(idx)
        if row is not None:
            self.sidebar_list.select_row(row)

    def _shift_workspace(self, delta: int):
        row = self.sidebar_list.get_selected_row()
        if row is None or not self.workspaces:
            return
        idx = row.get_index()
        n = len(self.workspaces)
        target = self.sidebar_list.get_row_at_index((idx + delta) % n)
        if target is not None:
            self.sidebar_list.select_row(target)

    def _next_workspace(self):
        self._shift_workspace(1)

    def _prev_workspace(self):
        self._shift_workspace(-1)

    def _zoom(self, delta: float):
        new = max(0.5, min(3.0, self._font_scale + delta))
        if abs(new - self._font_scale) < 1e-6:
            return
        self._font_scale = new
        for ws in self.workspaces:
            ws.apply_font_scale(new)

    def _zoom_reset(self):
        self._font_scale = 1.0
        for ws in self.workspaces:
            ws.apply_font_scale(1.0)

    def _copy(self):
        ws = self._current_workspace()
        if ws:
            pane = ws.current_pane()
            if pane:
                pane.copy()

    def _paste(self):
        ws = self._current_workspace()
        if ws:
            pane = ws.current_pane()
            if pane:
                pane.paste()


CSS = b"""
.lmux-bell { color: #4c8bf2; font-size: 0.9em; }
paned > separator { min-width: 2px; min-height: 2px; }
notebook header { padding: 0; }
notebook header tabs { padding: 0; }
notebook header tab { padding: 2px 8px; min-height: 0; }
notebook header tab label { padding: 0; }
row.workspace { padding: 0; }
"""


class LmuxApp(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)

    def do_activate(self):
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        display = Gdk.Display.get_default()
        if display is not None:
            Gtk.StyleContext.add_provider_for_display(
                display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )
        win = self.get_active_window()
        if win is None:
            win = LmuxWindow(self)
        win.present()


def main() -> int:
    return LmuxApp().run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
