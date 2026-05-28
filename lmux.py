#!/usr/bin/env python3
"""lmux — a tiny Linux take on cmux's tab UI.

Vertical workspace sidebar + horizontal terminal tabs with splits.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import uuid
from collections import deque
from urllib.parse import unquote, urlparse

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Vte", "3.91")
from gi.repository import Gdk, Gio, GLib, GObject, Gtk, Pango, Vte  # noqa: E402

# Dev mode (LMUX_DEV=1) isolates a working-tree run from the installed copy:
# different APP_ID so single-instance doesn't merge with stable, different
# state file so dev sessions don't trash the daily layout, and a window
# title suffix so you can tell them apart at a glance.
DEV_MODE = os.environ.get("LMUX_DEV") == "1"
APP_ID = "dev.lmux.LmuxDev" if DEV_MODE else "dev.lmux.Lmux"
WINDOW_TITLE = "lmux (dev)" if DEV_MODE else "lmux"

FONT = "monospace 11"
SCROLLBACK = 10_000
SIDEBAR_WIDTH = 260
URL_PATTERN = (
    r"(?:https?|ftp|file)://"
    r"[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+"
)
CLOSED_TAB_HISTORY = 16
STATE_PATH = os.path.expanduser(
    "~/.cache/lmux/state-dev.json" if DEV_MODE else "~/.cache/lmux/state.json"
)
STATE_VERSION = 1

DEBUG = os.environ.get("LMUX_DEBUG") == "1"

# When a pane that was running `claude` is restored from state.json, lmux types
# this command into the freshly-spawned shell so the conversation resumes.
# Override via env if you don't want --dangerously-skip-permissions, or set
# empty to disable auto-resume entirely.
CLAUDE_RESUME_CMD = os.environ.get(
    "LMUX_CLAUDE_RESUME_CMD",
    "claude --continue --dangerously-skip-permissions",
)

# Commands spawned in a freshly-opened project workspace. Three tabs:
# editor (nvim, focused), claude (--dangerously-skip-permissions), shell.
PROJECT_EDITOR_CMD = "nvim"
PROJECT_CLAUDE_CMD = "claude --dangerously-skip-permissions"

# Where the "Open project…" picker looks for project directories. Colon-
# separated, expanded via ~. Default matches the tmux-sessionizer layout.
PROJECT_ROOTS_DEFAULT = "~/Projects:~/Work"

# Where the auto-generated `claude` wrapper lives. Prepended to PATH for every
# pane shell so `claude` inside lmux resolves to the wrapper, which then
# injects Notification/Stop hooks via `--settings` before exec'ing the real
# binary. cmux uses the same pattern; see cmux/Resources/bin/claude.
LMUX_BIN_DIR = os.path.expanduser("~/.cache/lmux/bin")


def dlog(*args):
    if DEBUG:
        print("[lmux]", *args, file=sys.stderr, flush=True)


def _strip_nerd_glyphs(s: str) -> str:
    """Drop characters from the Unicode Private Use Area — Nerd Font glyphs
    live in U+E000–F8FF (BMP) and U+F0000–FFFFD (Plane 15). Collapses any
    whitespace runs left behind so we don't end up with "  ·  text".
    """
    out = "".join(
        c for c in s
        if not (0xE000 <= ord(c) <= 0xF8FF or 0xF0000 <= ord(c) <= 0xFFFFD)
    )
    return " ".join(out.split())


def _plain_title(decorated: str, is_claude: bool = False) -> str:
    """Strip Nerd Font glyphs from a tab title, substituting a 'claude:'
    prefix when the original carried the robot icon. Empty string after
    stripping degrades to 'shell'.
    """
    plain = _strip_nerd_glyphs(decorated)
    if is_claude:
        return f"claude: {plain}" if plain else "claude"
    return plain or "shell"


def list_project_dirs() -> list[tuple[str, str]]:
    """Return [(basename, abspath), ...] for every immediate-child directory
    under each configured project root. Sorted alphabetically; missing roots
    are skipped silently.
    """
    roots = os.environ.get("LMUX_PROJECT_DIRS") or PROJECT_ROOTS_DEFAULT
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for raw in roots.split(":"):
        root = os.path.expanduser(raw.strip())
        if not root or not os.path.isdir(root):
            continue
        try:
            entries = sorted(os.listdir(root))
        except OSError:
            continue
        for name in entries:
            if name.startswith("."):
                continue
            full = os.path.join(root, name)
            if not os.path.isdir(full):
                continue
            if full in seen:
                continue
            seen.add(full)
            out.append((name, full))
    return out


def _resolve_lmux_binary() -> str:
    """Absolute path to the running lmux binary, for embedding in hook
    commands. Prefer the running argv[0] so dev-mode runs route to the dev
    instance; fall back to PATH-resolved `lmux` for the installed copy."""
    arg0 = sys.argv[0]
    if arg0 and os.path.isabs(arg0) and os.access(arg0, os.X_OK):
        return arg0
    resolved = shutil.which("lmux")
    if resolved:
        return resolved
    return os.path.abspath(arg0 or "lmux")


def _claude_wrapper_script() -> str:
    """Bash source for `~/.cache/lmux/bin/claude`.

    Mirrors cmux/Resources/bin/claude in shape: find the real claude by
    walking PATH (skipping our own dir), skip non-session subcommands, then
    exec real claude with `--settings '<json>'`. Claude Code merges
    `--settings` additively with the user's own ~/.claude/settings.json, so
    this only adds our Notification/Stop hooks — user config stays intact.
    """
    lmux_bin = _resolve_lmux_binary()

    def hook(cmd: str, timeout: int = 10) -> str:
        # JSON string fragment for one hook entry. Wrapped in matcher="".
        return (
            '{"matcher":"","hooks":[{"type":"command","command":"'
            + cmd + f'","timeout":{timeout}' + '}]}'
        )

    notif = f'{lmux_bin} notify --title Claude --body \\"needs input\\"'
    stop = f"{lmux_bin} notify --title Claude --body done"
    sess_start = f"{lmux_bin} claude-session --state started"
    sess_end = f"{lmux_bin} claude-session --state ended"
    prompt = f"{lmux_bin} prompt-submit"
    hooks_json = (
        '{"hooks":{'
        f'"Notification":[{hook(notif)}],'
        f'"Stop":[{hook(stop)}],'
        f'"SessionStart":[{hook(sess_start)}],'
        f'"SessionEnd":[{hook(sess_end, timeout=2)}],'
        f'"UserPromptSubmit":[{hook(prompt, timeout=5)}]'
        '}}'
    )
    return f"""#!/usr/bin/env bash
# lmux claude wrapper - autogenerated, rewritten on every lmux launch.
# Intercepts `claude` invocations from inside an lmux pane (detected via
# LMUX_PANE_ID) and injects Notification, Stop, SessionStart, SessionEnd,
# and UserPromptSubmit hooks via --settings so attention events and
# claude-state transitions route back into lmux via the `lmux` CLI.
# Outside an lmux pane, passes through unchanged.

find_real_claude() {{
    local self_dir IFS=:
    self_dir="$(cd "$(dirname "$0")" && pwd)"
    for d in $PATH; do
        [[ "$d" == "$self_dir" ]] && continue
        [[ -x "$d/claude" ]] && printf '%s' "$d/claude" && return 0
    done
    return 1
}}

REAL_CLAUDE="$(find_real_claude)" || {{ echo "lmux: real claude not found in PATH" >&2; exit 127; }}

# Outside an lmux pane: pass through unchanged.
[[ -z "$LMUX_PANE_ID" ]] && exec "$REAL_CLAUDE" "$@"

# Skip injection for non-session subcommands (config, mcp, --version, etc.)
case "${{1:-}}" in
    config|mcp|migrate*|help|--help|-h|--version|-v|update|setup-token|install|doctor)
        exec "$REAL_CLAUDE" "$@"
        ;;
esac

HOOKS_JSON='{hooks_json}'
exec "$REAL_CLAUDE" --settings "$HOOKS_JSON" "$@"
"""


def install_claude_wrapper() -> None:
    """Write/refresh ~/.cache/lmux/bin/claude. Idempotent, cheap to repeat."""
    try:
        os.makedirs(LMUX_BIN_DIR, exist_ok=True)
        path = os.path.join(LMUX_BIN_DIR, "claude")
        desired = _claude_wrapper_script()
        try:
            with open(path) as f:
                if f.read() == desired:
                    dlog(f"claude wrapper up to date at {path}")
                    return
        except FileNotFoundError:
            pass
        with open(path, "w") as f:
            f.write(desired)
        os.chmod(path, 0o755)
        dlog(f"claude wrapper installed at {path}")
    except OSError as e:
        dlog(f"claude wrapper install failed: {e}")


def build_pane_env(pane_id: str | None = None) -> list[str]:
    """Env array for pane shells.

    - Prepends LMUX_BIN_DIR to PATH (deduping any existing entry) so the
      claude wrapper always shadows the real binary.
    - Exposes LMUX_PANE_ID so the CLI / claude hooks route DBus calls
      back to the exact pane.
    """
    env = dict(os.environ)
    existing_path = env.get("PATH", "")
    parts = [p for p in existing_path.split(":") if p and p != LMUX_BIN_DIR]
    env["PATH"] = ":".join([LMUX_BIN_DIR] + parts)
    if pane_id:
        env["LMUX_PANE_ID"] = pane_id
    return [f"{k}={v}" for k, v in env.items()]


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


def _session_of(pid: int) -> int | None:
    """Read the session id of a process from /proc/<pid>/stat."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            stat = f.read()
    except OSError:
        return None
    # stat format: pid (comm) state ppid pgrp session ...
    # comm can contain spaces and ')', so anchor on the LAST ')'.
    rp = stat.rfind(")")
    if rp == -1:
        return None
    fields = stat[rp + 1:].split()
    if len(fields) < 4:
        return None
    try:
        return int(fields[3])
    except ValueError:
        return None


def _session_has_comm(sid: int, *substrings: str) -> set[str]:
    """Walk /proc, return the subset of `substrings` matched by any process
    in session `sid`. One scan, multi-match — cheaper than re-walking per
    program when we need both 'claude' and 'nvim' at save time.
    """
    try:
        entries = os.listdir("/proc")
    except OSError:
        return set()
    sid_str = str(sid)
    found: set[str] = set()
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat") as f:
                stat = f.read()
        except OSError:
            continue
        rp = stat.rfind(")")
        if rp == -1:
            continue
        fields = stat[rp + 1:].split()
        if len(fields) < 4 or fields[3] != sid_str:
            continue
        lp = stat.find("(")
        if lp == -1 or lp >= rp:
            continue
        comm = stat[lp + 1:rp]
        for s in substrings:
            if s in comm:
                found.add(s)
        if len(found) == len(substrings):
            break
    return found


def _session_has_claude(sid: int) -> bool:
    return "claude" in _session_has_comm(sid, "claude")


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
        initial_command: str | None = None,
    ):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        # Unique pane id, exported to the shell as LMUX_PANE_ID so the
        # CLI / claude hooks can DBus back to this exact pane.
        self.pane_id: str = uuid.uuid4().hex
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

        self.on_changed = None
        self.on_attention = None  # callback (pane, source) — fires from VTE bell
        self.on_exited = None
        self.on_focused = None
        self.unread_count: int = 0

        self._last_cursor_pos: tuple[int, int] = (-1, -1)
        self._last_branch: str | None = None
        self._is_claude: bool = False
        # Authoritative-at-save-time flag, set by refresh_resume_markers().
        # When True, the saved layout records this pane as an editor tab so
        # restore re-spawns nvim.
        self._is_editor: bool = False
        # Sticky: claude often spawns bash subprocesses to run tool calls, which
        # briefly steal the foreground process group. Without a hold window we'd
        # flip _is_claude off mid-conversation and miss the auto-resume on close.
        # Also primed explicitly by the SessionStart hook.
        self._claude_seen_mono: float = 0.0
        self._pending_command: str | None = initial_command or None
        if self._pending_command:
            dlog(f"Pane init: queued initial_command={self._pending_command!r}")

        self.term.connect("window-title-changed", self._on_wm_title)
        self.term.connect("current-directory-uri-changed", self._on_cwd_uri)
        self.term.connect("bell", self._on_bell)
        self.term.connect("child-exited", self._on_exited)
        self.term.connect("contents-changed", self._on_contents_changed)

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
            build_pane_env(pane_id=self.pane_id),
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
        # Self-terminate if the widget has been torn down (defensive — pane
        # shutdown should have cancelled us, but if it didn't, don't keep
        # polling /proc for a dead pgrp every 1.2s forever).
        if not self.term.get_realized():
            self._cwd_poll_id = None
            return False
        pty = self.term.get_pty()
        if pty is None:
            return True
        fd = pty.get_fd()
        if fd < 0:
            self._cwd_poll_id = None
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
        cwd_changed = bool(new and new != self.cwd)
        if cwd_changed:
            self.cwd = new
        # Branch may change without cwd changing (e.g. `git checkout` in same dir).
        br = git_branch(self.cwd)
        branch_changed = br != self._last_branch
        if branch_changed:
            self._last_branch = br
        # Claude detection — `comm` of the foreground process group, plus a
        # short sticky window so brief tool-call subprocesses (bash) don't
        # flip the flag off mid-conversation.
        try:
            with open(f"/proc/{pgrp}/comm") as f:
                fg_comm = f.read().strip()
        except OSError:
            fg_comm = ""
        fg_is_claude = "claude" in fg_comm
        now_t = GLib.get_monotonic_time() / 1_000_000.0
        if fg_is_claude:
            self._claude_seen_mono = now_t
            is_claude = True
        else:
            # Hold for 30s after last seeing claude as foreground.
            is_claude = (now_t - self._claude_seen_mono) < 30.0 if self._claude_seen_mono else False
        claude_changed = is_claude != self._is_claude
        if claude_changed:
            self._is_claude = is_claude
            dlog(f"pane claude flag: fg_comm={fg_comm!r} fg_is_claude={fg_is_claude} -> is_claude={is_claude}")
        if (cwd_changed or branch_changed or claude_changed) and self.on_changed:
            self.on_changed(self)
        return True

    @property
    def title(self) -> str:
        base = ""
        if self.cwd:
            base = os.path.basename(self.cwd.rstrip("/"))
        if self._is_claude:
            # Plain "claude:" prefix — Nerd Font glyphs squish or fall back
            # to tofu boxes in GTK / mako / palette fonts inconsistently.
            return f"claude: {base}" if base else "claude"
        if self._wm_title:
            return self._wm_title
        return base or "shell"

    def _on_wm_title(self, term):
        try:
            value = term.dup_termprop_string(Vte.TERMPROP_XTERM_TITLE)
            title = value[0] if isinstance(value, tuple) else value
        except (AttributeError, TypeError):
            title = term.get_window_title()
        self._wm_title = title or None
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
            dlog("gate: no pty")
            return False
        fd = pty.get_fd()
        if fd < 0:
            dlog("gate: bad fd", fd)
            return False
        try:
            pgrp = os.tcgetpgrp(fd)
        except OSError as e:
            dlog("gate: tcgetpgrp failed", e)
            return False
        if pgrp <= 0:
            dlog("gate: pgrp <= 0", pgrp)
            return False
        try:
            with open(f"/proc/{pgrp}/comm") as f:
                comm = f.read().strip()
        except OSError as e:
            dlog("gate: comm read failed", e)
            return False
        ok = "claude" in comm
        dlog(f"gate: pgrp={pgrp} comm={comm!r} ok={ok}")
        return ok

    def _on_bell(self, term):
        # VTE BEL is unsolicited — filter shell tab-completion bells by
        # requiring claude to be the foreground process.
        dlog("vte bell signal fired")
        if not self._foreground_is_claude():
            return
        if self.on_attention is not None:
            self.on_attention(self, "bell")

    def clear_unread(self) -> None:
        if self.unread_count == 0:
            return
        self.unread_count = 0
        if self.on_changed:
            self.on_changed(self)

    def mark_claude_active(self) -> None:
        """Explicit signal that claude is running here. Called by the
        SessionStart hook; cheaper and more reliable than the /proc poll.
        """
        self._claude_seen_mono = GLib.get_monotonic_time() / 1_000_000.0
        if not self._is_claude:
            self._is_claude = True
            dlog("pane: marked claude-active via SessionStart hook")

    def _on_contents_changed(self, term):
        # VTE also fires contents-changed on focus-in/out cursor redraws. Filter
        # those out by checking the cursor position — real PTY output moves the
        # cursor, focus redraws do not.
        try:
            pos = term.get_cursor_position()
        except Exception:
            pos = (-1, -1)
        if pos == self._last_cursor_pos:
            return
        self._last_cursor_pos = pos
        # Inject the queued startup command (claude --continue ...) on first
        # real shell output, when we know the prompt is up and reading stdin.
        if self._pending_command:
            cmd = self._pending_command
            self._pending_command = None
            dlog(f"first-output trigger: feeding {cmd!r} to child")
            def _feed():
                try:
                    self.term.feed_child((cmd + "\n").encode())
                    dlog("feed_child OK")
                except Exception as e:
                    dlog(f"feed_child failed: {e}")
                return False
            GLib.idle_add(_feed)

    def _on_exited(self, term, status):
        dlog(f"pane child-exited status={status}")
        self.shutdown()
        if self.on_exited:
            self.on_exited(self)

    def shutdown(self) -> None:
        """Drop all GLib timer references so the Pane (and its Vte) can be gc'd.

        Safe to call multiple times; safe to call after child-exited has
        already fired (the timer ids will be None and we no-op).
        """
        if self._cwd_poll_id:
            GLib.source_remove(self._cwd_poll_id)
            self._cwd_poll_id = None

    def refresh_resume_markers(self) -> None:
        """Authoritative check via /proc session scan: sets _is_claude and
        _is_editor based on what's actually running in the pty's session.

        Foreground-pgrp polling can miss either program when a transient
        bash holds the foreground (tool calls, :! escapes from nvim). One
        full /proc walk at save time catches everything.
        """
        pty = self.term.get_pty()
        if pty is None:
            return
        fd = pty.get_fd()
        if fd < 0:
            return
        try:
            pgrp = os.tcgetpgrp(fd)
        except OSError:
            return
        if pgrp <= 0:
            return
        sid = _session_of(pgrp)
        if sid is None:
            return
        found = _session_has_comm(sid, "claude", "nvim")
        dlog(f"refresh_resume_markers: sid={sid} found={found} "
             f"(was is_claude={self._is_claude} is_editor={self._is_editor})")
        if "claude" in found:
            self._is_claude = True
            self._claude_seen_mono = GLib.get_monotonic_time() / 1_000_000.0
        self._is_editor = "nvim" in found

    # Back-compat alias for any external callers.
    refresh_claude_marker = refresh_resume_markers

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
    """Tab label: notification dot + title (+ split-count). Close via Ctrl+Shift+Q.

    Double-click the title to rename; Enter commits, Esc cancels.
    """

    def __init__(self, title: str, on_close):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        # Attention badge: hidden at 0, bell glyph at 1, decimal count at 2+.
        self.badge = Gtk.Label(label="")
        self.badge.add_css_class("lmux-bell")
        self.badge.set_visible(False)
        self.append(self.badge)

        self.label = Gtk.Label(label=title)
        self.label.set_xalign(0)
        self.label.set_ellipsize(Pango.EllipsizeMode.END)
        self.label.set_width_chars(10)
        self.label.set_max_width_chars(22)
        self.append(self.label)

        self.entry = Gtk.Entry()
        self.entry.set_max_width_chars(22)
        self.entry.set_visible(False)
        self.entry.add_css_class("lmux-tab-entry")
        self.append(self.entry)

        self.count_badge = Gtk.Label(label="")
        self.count_badge.add_css_class("lmux-split-count")
        self.count_badge.set_visible(False)
        self.append(self.count_badge)

        self.close_btn = Gtk.Button()
        self.close_btn.set_icon_name("window-close-symbolic")
        self.close_btn.add_css_class("lmux-tab-close")
        self.close_btn.add_css_class("flat")
        self.close_btn.set_valign(Gtk.Align.CENTER)
        self.close_btn.set_tooltip_text("Close tab (Ctrl+Shift+W)")
        if on_close is not None:
            self.close_btn.connect("clicked", lambda _b: on_close())
        self.append(self.close_btn)

        self.on_rename = None

        # Double-click → start edit. Use n_press=2 on the label.
        click = Gtk.GestureClick.new()
        click.set_button(1)
        click.connect("pressed", self._on_label_click)
        self.label.add_controller(click)

        self.entry.connect("activate", self._commit_edit)
        key = Gtk.EventControllerKey.new()
        key.connect("key-pressed", self._on_entry_key)
        self.entry.add_controller(key)
        focus = Gtk.EventControllerFocus.new()
        focus.connect("leave", lambda *_: self._cancel_edit())
        self.entry.add_controller(focus)

    def set_title(self, title: str):
        self.label.set_text(title)

    def set_unread(self, n: int):
        if n <= 0:
            self.badge.set_visible(False)
            return
        self.badge.set_text("99+" if n > 99 else str(n))
        self.badge.set_visible(True)

    def set_pane_count(self, n: int):
        if n > 1:
            self.count_badge.set_text(str(n))
            self.count_badge.set_visible(True)
        else:
            self.count_badge.set_visible(False)

    def _on_label_click(self, _gesture, n_press, _x, _y):
        if n_press == 2:
            self._start_edit()

    def _start_edit(self):
        if self.entry.get_visible():
            return
        self.entry.set_text(self.label.get_text())
        self.label.set_visible(False)
        self.entry.set_visible(True)
        self.entry.grab_focus()
        self.entry.select_region(0, -1)

    def _commit_edit(self, *_):
        if not self.entry.get_visible():
            return
        new = self.entry.get_text().strip()
        self._finish_edit()
        if self.on_rename:
            self.on_rename(new)

    def _cancel_edit(self):
        if not self.entry.get_visible():
            return
        self._finish_edit()

    def _finish_edit(self):
        self.entry.set_visible(False)
        self.label.set_visible(True)

    def _on_entry_key(self, _ctrl, keyval, _kc, _state):
        if keyval == Gdk.KEY_Escape:
            self._cancel_edit()
            return True
        return False


class WorkspaceRow(Gtk.ListBoxRow):
    """Sidebar entry: name on top, cwd · branch underneath, READY chip when bell pending.

    Double-click the name to rename; Enter commits, Esc cancels.
    """

    def __init__(self, name: str):
        super().__init__()
        outer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        outer.set_margin_top(2)
        outer.set_margin_bottom(2)
        outer.set_margin_start(10)
        outer.set_margin_end(10)

        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        text.set_hexpand(True)

        self.name_label = Gtk.Label(label=name)
        self.name_label.set_xalign(0)
        self.name_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.name_label.add_css_class("lmux-ws-name")
        text.append(self.name_label)

        self.name_entry = Gtk.Entry()
        self.name_entry.add_css_class("lmux-ws-entry")
        self.name_entry.set_visible(False)
        text.append(self.name_entry)

        self.sub_label = Gtk.Label(label="")
        self.sub_label.set_xalign(0)
        self.sub_label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        self.sub_label.add_css_class("lmux-ws-sub")
        text.append(self.sub_label)

        outer.append(text)

        # Attention badge mirroring TabLabel's: bell glyph for 1, count for 2+.
        self.badge = Gtk.Label(label="")
        self.badge.add_css_class("lmux-chip-ready")
        self.badge.set_visible(False)
        self.badge.set_valign(Gtk.Align.CENTER)
        outer.append(self.badge)

        self.close_btn = Gtk.Button()
        self.close_btn.set_icon_name("window-close-symbolic")
        self.close_btn.add_css_class("lmux-ws-close")
        self.close_btn.add_css_class("flat")
        self.close_btn.set_valign(Gtk.Align.CENTER)
        self.close_btn.set_tooltip_text("Close workspace")
        self.close_btn.connect("clicked", lambda _b: self._on_close_clicked())
        outer.append(self.close_btn)

        self.set_child(outer)

        self.on_rename = None
        self.on_close = None
        self.on_reorder = None

        click = Gtk.GestureClick.new()
        click.set_button(1)
        click.connect("pressed", self._on_name_click)
        self.name_label.add_controller(click)

        # Drag-to-reorder. Source emits this row's listbox index;
        # drop target on each row receives, and we let the window reshuffle.
        drag_source = Gtk.DragSource.new()
        drag_source.set_actions(Gdk.DragAction.MOVE)
        drag_source.connect("prepare", self._on_drag_prepare)
        self.add_controller(drag_source)

        drop_target = Gtk.DropTarget.new(GObject.TYPE_INT, Gdk.DragAction.MOVE)
        drop_target.connect("drop", self._on_drop)
        drop_target.connect("enter", self._on_drop_enter)
        drop_target.connect("leave", self._on_drop_leave)
        self.add_controller(drop_target)

        self.name_entry.connect("activate", self._commit_edit)
        key = Gtk.EventControllerKey.new()
        key.connect("key-pressed", self._on_entry_key)
        self.name_entry.add_controller(key)
        focus = Gtk.EventControllerFocus.new()
        focus.connect("leave", lambda *_: self._cancel_edit())
        self.name_entry.add_controller(focus)

    def _on_name_click(self, _gesture, n_press, _x, _y):
        if n_press == 2:
            self.start_rename()

    def start_rename(self):
        if self.name_entry.get_visible():
            return
        self.name_entry.set_text(self.name_label.get_text())
        self.name_label.set_visible(False)
        self.name_entry.set_visible(True)
        self.name_entry.grab_focus()
        self.name_entry.select_region(0, -1)

    def _commit_edit(self, *_):
        if not self.name_entry.get_visible():
            return
        new = self.name_entry.get_text().strip()
        self._finish_edit()
        if self.on_rename:
            self.on_rename(new)

    def _cancel_edit(self):
        if not self.name_entry.get_visible():
            return
        self._finish_edit()

    def _finish_edit(self):
        self.name_entry.set_visible(False)
        self.name_label.set_visible(True)

    def _on_entry_key(self, _ctrl, keyval, _kc, _state):
        if keyval == Gdk.KEY_Escape:
            self._cancel_edit()
            return True
        return False

    def _on_close_clicked(self):
        if self.on_close is not None:
            self.on_close()

    # --- drag-and-drop reorder ---

    def _on_drag_prepare(self, _source, _x, _y):
        val = GObject.Value()
        val.init(GObject.TYPE_INT)
        val.set_int(self.get_index())
        return Gdk.ContentProvider.new_for_value(val)

    def _on_drop(self, _target, value, _x, _y):
        try:
            src_idx = int(value)
        except (TypeError, ValueError):
            return False
        target_idx = self.get_index()
        self.remove_css_class("lmux-drop-target")
        if src_idx == target_idx:
            return False
        if self.on_reorder is not None:
            self.on_reorder(src_idx, target_idx)
        return True

    def _on_drop_enter(self, _target, _x, _y):
        self.add_css_class("lmux-drop-target")
        return Gdk.DragAction.MOVE

    def _on_drop_leave(self, _target):
        self.remove_css_class("lmux-drop-target")

    def set_metadata(self, cwd: str | None, branch: str | None):
        base = os.path.basename(cwd.rstrip("/")) if cwd else ""
        if base and branch:
            self.sub_label.set_text(f"{base}   {branch}")
        elif branch:
            self.sub_label.set_text(f" {branch}")
        elif base:
            self.sub_label.set_text(base)
        else:
            self.sub_label.set_text("")

    def set_unread(self, n: int):
        if n <= 0:
            self.badge.set_visible(False)
            return
        self.badge.set_text("99+" if n > 99 else str(n))
        self.badge.set_visible(True)


_SVG_SPLIT_RIGHT = b"""<?xml version="1.0"?>
<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 16 16">
  <rect x="2.5" y="3.5" width="11" height="9" rx="1.5" fill="none" stroke="#9aa3b2" stroke-width="1.3"/>
  <line x1="8" y1="3.5" x2="8" y2="12.5" stroke="#9aa3b2" stroke-width="1.3"/>
</svg>"""

_SVG_SPLIT_DOWN = b"""<?xml version="1.0"?>
<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 16 16">
  <rect x="2.5" y="3.5" width="11" height="9" rx="1.5" fill="none" stroke="#9aa3b2" stroke-width="1.3"/>
  <line x1="2.5" y1="8" x2="13.5" y2="8" stroke="#9aa3b2" stroke-width="1.3"/>
</svg>"""


def _ensure_custom_icons() -> str:
    base = os.path.expanduser("~/.cache/lmux/icons")
    os.makedirs(base, exist_ok=True)
    for name, data in (("split-right.svg", _SVG_SPLIT_RIGHT),
                       ("split-down.svg", _SVG_SPLIT_DOWN)):
        path = os.path.join(base, name)
        try:
            with open(path, "rb") as f:
                if f.read() == data:
                    continue
        except OSError:
            pass
        with open(path, "wb") as f:
            f.write(data)
    return base


def _make_tab_actions() -> Gtk.Box:
    """Build the right-aligned button row that sits at the end of the tab strip."""
    box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
    box.add_css_class("lmux-tab-actions")
    box.set_valign(Gtk.Align.CENTER)

    icon_dir = _ensure_custom_icons()
    specs = [
        ("name:tab-new-symbolic", "win.new-tab", "New tab (Ctrl+Shift+T)"),
        (f"file:{icon_dir}/split-right.svg", "win.split-right", "Split right (Ctrl+Shift+D)"),
        (f"file:{icon_dir}/split-down.svg", "win.split-down", "Split down (Ctrl+Shift+E)"),
    ]
    for ref, action, tooltip in specs:
        btn = Gtk.Button()
        if ref.startswith("name:"):
            img = Gtk.Image.new_from_icon_name(ref[5:])
        else:
            img = Gtk.Image.new_from_file(ref[5:])
        img.set_pixel_size(16)
        btn.set_child(img)
        btn.set_action_name(action)
        btn.set_tooltip_text(tooltip)
        btn.add_css_class("flat")
        btn.add_css_class("lmux-tab-btn")
        btn.set_can_focus(False)
        box.append(btn)
    dlog(f"made tab-actions box with {len(specs)} buttons")
    return box


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


def _find_any_pane(w):
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
    """A notebook tab's root. Holds one pane or a Gtk.Paned tree of panes."""

    def __init__(self, pane):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.active_pane = pane
        self.on_active_changed = None
        self.custom_title: str | None = None
        self.append(pane)
        self._update_active_decoration()

    def panes(self) -> list:
        result: list = []

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

    def set_active(self, pane):
        if pane is self.active_pane:
            return
        self.active_pane = pane
        self._update_active_decoration()
        if self.on_active_changed:
            self.on_active_changed(self, pane)

    def _update_active_decoration(self):
        for p in self.panes():
            if p is self.active_pane and len(self.panes()) > 1:
                p.add_css_class("lmux-active-pane")
            else:
                p.remove_css_class("lmux-active-pane")

    def split(self, new_pane, orientation: Gtk.Orientation):
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

    def close_pane(self, pane) -> bool:
        """Return True if tab is now empty and should close."""
        # Drop pane timers before unparenting so the dying Pane (and its Vte)
        # can be garbage-collected. Without this the timers pin the object
        # forever and keep polling /proc on a dead pgrp.
        try:
            pane.shutdown()
        except Exception:
            pass
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
            self._update_active_decoration()
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
        on_pane_attention,
        on_tab_closed,
        theme_cfg=None,
        font_scale: float = 1.0,
    ):
        self.name = name
        # Auto-named workspaces follow the active pane's cwd. Once the user
        # double-clicks-and-renames (or palette-renames), this flips to True
        # and we stop overwriting it on cwd changes.
        self.name_is_custom: bool = False
        # Monotonic time of last activity (focus / sidebar select / current-
        # pane-changed). Used to sort the workspace picker most-recent-first.
        self.last_active_mono: float = GLib.get_monotonic_time() / 1_000_000.0
        self.on_empty = on_empty
        self.on_current_pane_changed = on_current_pane_changed
        self.on_pane_attention = on_pane_attention  # (ws, tab_root, pane, source)
        self.on_tab_closed = on_tab_closed
        self.theme_cfg = theme_cfg or {}
        self.font_scale = font_scale
        self._tabs: dict[TabRoot, TabLabel] = {}
        self.notebook = Gtk.Notebook()
        self.notebook.set_scrollable(True)
        self.notebook.set_show_border(False)
        self.notebook.set_hexpand(True)
        self.notebook.set_vexpand(True)
        self.notebook.connect("switch-page", self._on_switch_page)
        self.notebook.set_action_widget(_make_tab_actions(), Gtk.PackType.END)

    def _label_title(self, tab_root: "TabRoot", pane: "Pane") -> str:
        return tab_root.custom_title or pane.title

    def tabs(self) -> list[TabRoot]:
        return list(self._tabs.keys())

    def add_tab(self, cwd: str | None = None, initial_command: str | None = None,
                custom_title: str | None = None, focus: bool = True):
        pane = self._make_pane(cwd=cwd, initial_command=initial_command)
        tab_root = TabRoot(pane)
        tab_root.on_active_changed = self._on_active_changed
        if custom_title:
            tab_root.custom_title = custom_title
        label = TabLabel(custom_title or pane.title,
                         on_close=lambda: self._close_tab(tab_root))
        label.on_rename = lambda new: self._rename_tab(tab_root, new)
        self._tabs[tab_root] = label
        self._wire_pane(pane, tab_root)
        self.notebook.append_page(tab_root, label)
        self.notebook.set_tab_reorderable(tab_root, True)
        if focus:
            self.notebook.set_current_page(self.notebook.get_n_pages() - 1)
            GLib.idle_add(pane.focus_term)

    def _rename_tab(self, tab_root: TabRoot, new_title: str):
        tab_root.custom_title = new_title or None
        label = self._tabs.get(tab_root)
        if label:
            pane = tab_root.active_pane
            label.set_title(self._label_title(tab_root, pane))

    def add_tab_from_layout(self, layout: dict, custom_title: str | None = None) -> bool:
        """Restore a tab tree from a saved layout dict. Returns False if invalid."""
        collected: list[tuple[Pane, bool, int | None]] = []  # (pane, is_active, position)

        def build(node):
            t = node.get("type")
            if t == "pane":
                was_claude = bool(node.get("claude"))
                was_editor = bool(node.get("editor"))
                # Claude wins if both flags are set (a pane can't host both
                # foreground programs simultaneously, but `was_claude` is the
                # one we go out of our way to preserve via auto-resume).
                if was_claude and CLAUDE_RESUME_CMD:
                    cmd = CLAUDE_RESUME_CMD
                elif was_editor:
                    cmd = PROJECT_EDITOR_CMD
                else:
                    cmd = None
                dlog(f"restore: pane cwd={node.get('cwd')!r} was_claude={was_claude} "
                     f"was_editor={was_editor} queued_cmd={cmd!r}")
                pane = self._make_pane(cwd=node.get("cwd"), initial_command=cmd)
                collected.append((pane, bool(node.get("active")), None))
                return pane
            if t == "split":
                orient = (Gtk.Orientation.HORIZONTAL if node.get("orientation") == "h"
                          else Gtk.Orientation.VERTICAL)
                paned = _make_paned(orient)
                start = build(node["start"])
                end = build(node["end"])
                paned.set_start_child(start)
                paned.set_end_child(end)
                pos = node.get("position")
                if isinstance(pos, int) and pos > 0:
                    GLib.idle_add(paned.set_position, pos)
                return paned
            return None

        try:
            root_widget = build(layout)
        except Exception as e:
            dlog(f"restore: bad layout: {e}")
            return False
        if root_widget is None or not collected:
            return False

        first_pane = collected[0][0]
        tab_root = TabRoot(first_pane)
        tab_root.on_active_changed = self._on_active_changed
        tab_root.custom_title = custom_title or None
        if root_widget is not first_pane:
            tab_root.remove(first_pane)
            tab_root.append(root_widget)

        for pane, _is_active, _ in collected:
            self._wire_pane(pane, tab_root)

        active = next((p for p, a, _ in collected if a), first_pane)
        tab_root.active_pane = active
        tab_root._update_active_decoration()

        label = TabLabel(
            self._label_title(tab_root, active),
            on_close=lambda: self._close_tab(tab_root),
        )
        label.on_rename = lambda new: self._rename_tab(tab_root, new)
        label.set_pane_count(len(collected))
        self._tabs[tab_root] = label
        self.notebook.append_page(tab_root, label)
        self.notebook.set_tab_reorderable(tab_root, True)
        return True

    def _make_pane(self, cwd: str | None = None, initial_command: str | None = None) -> Pane:
        return Pane(
            cwd=cwd,
            theme_cfg=self.theme_cfg,
            font_scale=self.font_scale,
            initial_command=initial_command,
        )

    def _wire_pane(self, pane: Pane, tab_root: TabRoot):
        def changed(p):
            label = self._tabs.get(tab_root)
            if label and tab_root.active_pane is p:
                label.set_title(self._label_title(tab_root, p))
                label.set_unread(p.unread_count)
            if self.current_tab_root() is tab_root and tab_root.active_pane is p:
                self.on_current_pane_changed(self, p)

        def focused(p):
            tab_root.set_active(p)
            p.clear_unread()

        pane.on_changed = changed
        pane.on_attention = lambda p, src: self.on_pane_attention(self, tab_root, p, src)
        pane.on_exited = lambda p: self._on_pane_exit(tab_root, p)
        pane.on_focused = focused

    def _on_active_changed(self, tab_root: TabRoot, pane: Pane):
        label = self._tabs.get(tab_root)
        if label:
            label.set_title(self._label_title(tab_root, pane))
            label.set_pane_count(len(tab_root.panes()))
        if self.current_tab_root() is tab_root:
            self.on_current_pane_changed(self, pane)

    def _on_pane_exit(self, tab_root: TabRoot, pane: Pane):
        empty = tab_root.close_pane(pane)
        if empty:
            self._close_tab(tab_root)
        else:
            label = self._tabs.get(tab_root)
            if label:
                label.set_pane_count(len(tab_root.panes()))

    def _close_tab(self, tab_root: TabRoot):
        # Capture cwd of last-active pane for restore
        cwd = tab_root.active_pane.cwd if tab_root.active_pane else None
        # Cancel timers on any panes still alive in this tab (split siblings).
        for p in tab_root.panes():
            try:
                p.shutdown()
            except Exception:
                pass
        n = self.notebook.page_num(tab_root)
        if n != -1:
            self.notebook.remove_page(n)
        self._tabs.pop(tab_root, None)
        if cwd and self.on_tab_closed:
            self.on_tab_closed(cwd)
        if self.notebook.get_n_pages() == 0:
            self.on_empty(self)

    def unread_total(self) -> int:
        """Sum unread counts across all panes in this workspace."""
        total = 0
        for tr in self._tabs:
            for p in tr.panes():
                total += p.unread_count
        return total

    def _on_switch_page(self, _nb, page_widget, _idx):
        if isinstance(page_widget, TabRoot):
            # Clearing the active pane's unread fires on_changed →
            # LmuxWindow recomputes the sidebar count.
            page_widget.active_pane.clear_unread()
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
        else:
            label = self._tabs.get(tr)
            if label:
                label.set_pane_count(len(tr.panes()))

    def split(self, orientation: Gtk.Orientation):
        tr = self.current_tab_root()
        if not tr:
            return
        cwd = tr.active_pane.cwd if tr.active_pane else None
        new = self._make_pane(cwd=cwd)
        self._wire_pane(new, tr)
        tr.split(new, orientation)
        label = self._tabs.get(tr)
        if label:
            label.set_pane_count(len(tr.panes()))

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
        super().__init__(application=app, title=WINDOW_TITLE)
        self.set_default_size(1280, 800)
        self._ws_counter = 0
        self.workspaces: list[Workspace] = []
        self._rows: dict[Workspace, WorkspaceRow] = {}
        self._font_scale = 1.0
        self._closed_cwds: deque[str] = deque(maxlen=CLOSED_TAB_HISTORY)
        self._flash_tick_id: int | None = None
        self._flash_target: "TabRoot | None" = None
        self._restoring: bool = False
        self._force_close: bool = False
        self._last_bell_mono: float = 0.0
        self.theme = Theme(on_change=self._apply_theme_all)

        self.main_paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self.main_paned.set_position(SIDEBAR_WIDTH)
        self.main_paned.set_resize_start_child(False)
        self.main_paned.set_shrink_start_child(False)

        self.sidebar = self._build_sidebar()
        self.main_paned.set_start_child(self.sidebar)

        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.NONE)

        self.search_bar = Gtk.SearchBar()
        self.search_entry = Gtk.SearchEntry()
        self.search_entry.set_placeholder_text("Search scrollback  ·  Enter older  ·  Shift+Enter newer  ·  Esc close")
        self.search_entry.set_hexpand(True)
        self.search_count_label = Gtk.Label(label="")
        self.search_count_label.add_css_class("lmux-search-count")
        search_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        search_box.append(self.search_entry)
        search_box.append(self.search_count_label)
        self.search_bar.set_child(search_box)
        self.search_bar.connect_entry(self.search_entry)
        self.search_entry.connect("search-changed", self._on_search_changed)
        self.search_entry.connect("activate", lambda *_: self._search_step(forward=False))
        sk = Gtk.EventControllerKey.new()
        sk.connect("key-pressed", self._on_search_key)
        self.search_entry.add_controller(sk)
        self.search_bar.connect("notify::search-mode-enabled", self._on_search_mode_changed)
        self._search_target_pane: Pane | None = None

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        content.append(self.search_bar)
        content.append(self.stack)
        self.stack.set_vexpand(True)
        self.main_paned.set_end_child(content)

        self.overlay = Gtk.Overlay()
        self.overlay.set_child(self.main_paned)
        self._build_palette_overlay()
        self.set_child(self.overlay)

        self.connect("notify::is-active", self._on_is_active_changed)
        self.connect("close-request", self._on_close_request)

        self._install_actions(app)
        if not self._restore_state():
            self.new_workspace()
        # After init/restore, make sure typing goes straight to the active pane —
        # the focus chain through Overlay + Stack + Notebook doesn't always pick it.
        GLib.idle_add(self._focus_current_pane)

    def _focus_current_pane(self) -> bool:
        ws = self._current_workspace()
        if ws is not None:
            pane = ws.current_pane()
            if pane is not None:
                pane.focus_term()
        return False

    def _serialize_layout(self, widget, active_pane):
        if isinstance(widget, Pane):
            dlog(f"save: pane cwd={widget.cwd!r} is_claude={widget._is_claude} "
                 f"is_editor={widget._is_editor} "
                 f"last_seen_age={(GLib.get_monotonic_time()/1_000_000.0 - widget._claude_seen_mono):.1f}s")
            return {
                "type": "pane",
                "cwd": widget.cwd,
                "active": widget is active_pane,
                "claude": widget._is_claude,
                "editor": widget._is_editor,
            }
        if isinstance(widget, Gtk.Paned):
            return {
                "type": "split",
                "orientation": ("h" if widget.get_orientation() == Gtk.Orientation.HORIZONTAL
                                else "v"),
                "position": max(1, widget.get_position()),
                "start": self._serialize_layout(widget.get_start_child(), active_pane),
                "end": self._serialize_layout(widget.get_end_child(), active_pane),
            }
        return None

    def _save_state(self) -> None:
        # Authoritative pre-save refresh of each pane's claude marker via
        # /proc session scan — so a transient bash subprocess at the moment
        # of close doesn't mask a still-running claude.
        for ws in self.workspaces:
            for tr in ws.tabs():
                for p in tr.panes():
                    p.refresh_resume_markers()
        try:
            workspaces_data = []
            current_ws = self._current_workspace()
            active_ws_index = 0
            for i, ws in enumerate(self.workspaces):
                if ws is current_ws:
                    active_ws_index = i
                tabs_data = []
                for tr in ws.tabs():
                    root = tr.get_first_child()
                    layout = self._serialize_layout(root, tr.active_pane)
                    if layout is None:
                        continue
                    tab_blob = {"layout": layout}
                    if tr.custom_title:
                        tab_blob["title"] = tr.custom_title
                    tabs_data.append(tab_blob)
                if not tabs_data:
                    continue
                workspaces_data.append({
                    "name": ws.name,
                    "name_is_custom": ws.name_is_custom,
                    "active_tab_index": max(0, ws.notebook.get_current_page()),
                    "tabs": tabs_data,
                })
            width = self.get_width() if self.get_width() > 0 else 1280
            height = self.get_height() if self.get_height() > 0 else 800
            state = {
                "version": STATE_VERSION,
                "window": {"width": width, "height": height},
                "active_workspace_index": active_ws_index,
                "workspaces": workspaces_data,
                "closed_cwds": list(self._closed_cwds),
            }
            os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
            tmp = STATE_PATH + ".tmp"
            with open(tmp, "w") as f:
                json.dump(state, f, indent=2)
            os.replace(tmp, STATE_PATH)
            dlog(f"state saved: {len(workspaces_data)} workspaces")
        except OSError as e:
            dlog(f"state save failed: {e}")

    def _restore_state(self) -> bool:
        try:
            with open(STATE_PATH, "r") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            dlog(f"no state to restore: {e}")
            return False
        if data.get("version") != STATE_VERSION:
            dlog(f"state version mismatch: {data.get('version')}")
            return False
        wins = data.get("workspaces") or []
        if not wins:
            return False

        win = data.get("window") or {}
        w = int(win.get("width") or 0)
        h = int(win.get("height") or 0)
        if w > 200 and h > 200:
            self.set_default_size(w, h)

        self._restoring = True
        try:
            for ws_data in wins:
                ws = self._make_workspace(ws_data.get("name"))
                # Default True for backwards-compat with older state files —
                # if we can't tell, assume the saved name was deliberate.
                ws.name_is_custom = bool(ws_data.get("name_is_custom", True))
                added = 0
                for tab in ws_data.get("tabs") or []:
                    layout = tab.get("layout")
                    title = tab.get("title")
                    if layout and ws.add_tab_from_layout(layout, custom_title=title):
                        added += 1
                if added == 0:
                    ws.add_tab()
                idx = ws_data.get("active_tab_index", 0)
                if 0 <= idx < ws.notebook.get_n_pages():
                    ws.notebook.set_current_page(idx)
        finally:
            self._restoring = False

        active_idx = data.get("active_workspace_index", 0)
        if 0 <= active_idx < len(self.workspaces):
            row = self.sidebar_list.get_row_at_index(active_idx)
            if row is not None:
                self.sidebar_list.select_row(row)
        # Replay the closed-tab history so Ctrl+Shift+Z works across restarts.
        for cwd in (data.get("closed_cwds") or [])[-CLOSED_TAB_HISTORY:]:
            if isinstance(cwd, str) and cwd:
                self._closed_cwds.append(cwd)
        dlog(f"state restored: {len(self.workspaces)} workspaces, "
             f"{len(self._closed_cwds)} closed-tab entries")
        return True

    def _on_close_request(self, _w) -> bool:
        if self._force_close:
            self._teardown_for_quit()
            return False  # allow close
        self._show_quit_confirmation()
        return True  # block close until user confirms

    def _teardown_for_quit(self):
        self._save_state()
        if self._flash_tick_id is not None:
            GLib.source_remove(self._flash_tick_id)
            self._flash_tick_id = None
        # Drop every pane's timer refs so the Vte widgets can be released and
        # the GLib main loop has no remaining sources keeping the process alive.
        for ws in self.workspaces:
            for tr in ws.tabs():
                for p in tr.panes():
                    try:
                        p.shutdown()
                    except Exception:
                        pass

    def _show_quit_confirmation(self):
        n_panes = 0
        n_claude = 0
        for ws in self.workspaces:
            for tr in ws.tabs():
                for p in tr.panes():
                    n_panes += 1
                    if p._is_claude:
                        n_claude += 1
        parts = [f"{n_panes} pane{'s' if n_panes != 1 else ''} open"]
        if n_claude:
            parts.append(f"{n_claude} running claude")
        detail = " · ".join(parts) + "."
        dialog = Gtk.AlertDialog()
        dialog.set_modal(True)
        dialog.set_message("Quit lmux?")
        dialog.set_detail(detail)
        dialog.set_buttons(["Cancel", "Quit"])
        dialog.set_cancel_button(0)
        dialog.set_default_button(0)
        dialog.choose(self, None, self._on_quit_choice)

    def _on_quit_choice(self, dialog, result):
        try:
            choice = dialog.choose_finish(result)
        except GLib.Error:
            return  # dismissed (Esc, etc.)
        if choice == 1:
            self._force_close = True
            self.close()

    def _on_is_active_changed(self, *_):
        active = self.is_active()
        dlog(f"window is-active changed -> {active}")
        if not active:
            return
        ws = self._current_workspace()
        if ws is None:
            return
        tr = ws.current_tab_root()
        if tr is not None:
            tr.active_pane.clear_unread()
            # Pull focus back to the terminal unless an overlay surface owns it.
            if (not self._palette_is_open()
                    and not self.search_bar.get_search_mode()):
                pane = tr.active_pane
                if pane is not None:
                    GLib.idle_add(pane.focus_term)

    def _build_sidebar(self) -> Gtk.Box:
        sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        sidebar.add_css_class("sidebar")

        new_btn = Gtk.Button(label="+  New workspace")
        new_btn.set_halign(Gtk.Align.FILL)
        new_btn.add_css_class("flat")
        new_btn.add_css_class("lmux-new-ws")
        new_btn.set_tooltip_text("New workspace (Ctrl+Shift+W)")
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
        self._make_workspace(None)

    def _make_workspace(self, name: str | None, *, with_default_tab: bool = True) -> Workspace:
        auto_name = name is None
        if auto_name:
            self._ws_counter += 1
            name = f"ws-{self._ws_counter}"  # placeholder; overridden once the pane reports a cwd
        else:
            # Bump counter so future generated names don't collide with restored ones.
            if name.startswith("ws-"):
                try:
                    n = int(name[3:])
                    if n > self._ws_counter:
                        self._ws_counter = n
                except ValueError:
                    pass
        existing = {w.name for w in self.workspaces}
        if name in existing:
            base = name
            n = 2
            while f"{base} ({n})" in existing:
                n += 1
            name = f"{base} ({n})"
        ws = Workspace(
            name,
            on_empty=self._remove_workspace,
            on_current_pane_changed=self._on_current_pane_changed,
            on_pane_attention=self._notify,
            on_tab_closed=self._on_tab_closed,
            theme_cfg=self.theme.cfg,
            font_scale=self._font_scale,
        )
        self.workspaces.append(ws)
        self.stack.add_named(ws.notebook, name)

        row = WorkspaceRow(name)
        row.workspace = ws  # type: ignore[attr-defined]
        row.on_rename = lambda new, ws=ws: self._rename_workspace(ws, new)
        row.on_close = lambda ws=ws: self._close_workspace(ws)
        row.on_reorder = self._reorder_workspace
        self._rows[ws] = row
        self.sidebar_list.append(row)
        self.sidebar_list.select_row(row)

        # Default new workspace starts with one fresh tab. Callers restoring
        # from state, or constructing a multi-tab workspace (e.g. the project
        # picker's editor + shell layout), pass with_default_tab=False.
        if with_default_tab and not self._restoring:
            ws.add_tab()

        pane = ws.current_pane()
        if pane is not None:
            # If the workspace name was auto-generated, derive it from the
            # initial pane cwd so we don't show "ws-1" briefly.
            if auto_name and pane.cwd:
                derived = os.path.basename(pane.cwd.rstrip("/"))
                if derived:
                    self._set_workspace_name(ws, derived, mark_custom=False)
            self._refresh_row(ws, pane)
        return ws

    def _rename_workspace(self, ws: Workspace, new_name: str):
        # Manual rename — locks the workspace name against further auto-updates.
        self._set_workspace_name(ws, new_name, mark_custom=True)

    def open_project(self, path: str) -> None:
        """tmux-sessionizer equivalent: if a workspace already exists for
        this project, switch to it; otherwise create a fresh workspace at
        the project root with an editor tab and a shell tab.
        """
        path = os.path.expanduser(path)
        if not os.path.isdir(path):
            dlog(f"open_project: not a directory: {path!r}")
            return
        name = os.path.basename(path.rstrip("/")) or path
        # Switch to an existing workspace if its name matches the project.
        for ws in self.workspaces:
            if ws.name == name:
                self.sidebar_list.select_row(self._rows[ws])
                return
        # Otherwise build a new workspace with three tabs: editor (focused),
        # claude with --dangerously-skip-permissions, and a plain shell.
        ws = self._make_workspace(name, with_default_tab=False)
        ws.add_tab(cwd=path, initial_command=PROJECT_EDITOR_CMD, custom_title="editor", focus=True)
        ws.add_tab(cwd=path, initial_command=PROJECT_CLAUDE_CMD, custom_title="claude", focus=False)
        ws.add_tab(cwd=path, custom_title="shell", focus=False)
        ws.notebook.set_current_page(0)
        first = ws.current_pane()
        if first is not None:
            GLib.idle_add(first.focus_term)

    def _set_workspace_name(self, ws: Workspace, new_name: str, *, mark_custom: bool):
        new_name = (new_name or "").strip()
        if not new_name:
            if mark_custom:
                ws.name_is_custom = True
            return
        if new_name == ws.name:
            if mark_custom:
                ws.name_is_custom = True
            return
        existing = {w.name for w in self.workspaces if w is not ws}
        if new_name in existing:
            base = new_name
            n = 2
            while f"{base} ({n})" in existing:
                n += 1
            new_name = f"{base} ({n})"
        old = ws.name
        was_visible = self.stack.get_visible_child_name() == old
        self.stack.remove(ws.notebook)
        ws.name = new_name
        self.stack.add_named(ws.notebook, new_name)
        if was_visible:
            self.stack.set_visible_child_name(new_name)
        row = self._rows.get(ws)
        if row is not None:
            row.name_label.set_text(new_name)
        if mark_custom:
            ws.name_is_custom = True

    def _reorder_workspace(self, src_idx: int, target_idx: int):
        n = len(self.workspaces)
        if src_idx == target_idx:
            return
        if not (0 <= src_idx < n) or not (0 <= target_idx < n):
            return
        ws = self.workspaces.pop(src_idx)
        self.workspaces.insert(target_idx, ws)
        row = self._rows[ws]
        self.sidebar_list.remove(row)
        self.sidebar_list.insert(row, target_idx)
        self.sidebar_list.select_row(row)

    def _close_workspace(self, ws: Workspace):
        # Close every tab — that walks the existing _close_tab path which
        # cancels per-pane timers and (when the last tab is gone) calls
        # on_empty → _remove_workspace, so we don't have to.
        for tr in list(ws.tabs()):
            ws._close_tab(tr)

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
        ws.last_active_mono = GLib.get_monotonic_time() / 1_000_000.0
        self.stack.set_visible_child_name(ws.name)
        tr = ws.current_tab_root()
        if tr is not None:
            tr.active_pane.clear_unread()
            GLib.idle_add(tr.active_pane.focus_term)

    def _refresh_row(self, ws: Workspace, pane: Pane):
        row = self._rows.get(ws)
        if row is None:
            return
        row.set_metadata(pane.cwd, git_branch(pane.cwd))
        row.set_unread(ws.unread_total())

    def _on_current_pane_changed(self, ws: Workspace, pane: Pane):
        # Auto-derive workspace name from the active pane's cwd unless the
        # user has explicitly renamed it.
        if not ws.name_is_custom and pane.cwd:
            derived = os.path.basename(pane.cwd.rstrip("/"))
            if derived and derived != ws.name:
                self._set_workspace_name(ws, derived, mark_custom=False)
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

    def _notify(self, ws: Workspace, tab_root: TabRoot, pane: Pane, source: str):
        """Fire attention for a pane: sound + flash + unread bump, plus a
        desktop toast when the pane isn't currently focused.
        """
        focused_here = self._is_visible(ws, tab_root, pane) and self.is_active()
        dlog(f"notify: ws={ws.name} source={source} focused_here={focused_here}")
        pane.unread_count += 1
        if pane.on_changed is not None:
            pane.on_changed(pane)
        self._refresh_row(ws, pane)
        self._play_bell_sound()
        self._flash_tab(tab_root)
        if not focused_here:
            self._send_desktop_notification(ws, tab_root, pane)

    def cli_notify(self, title: str, body: str, pane_id: str | None = None) -> None:
        """DBus `notify` entry. Routes to the pane named by pane_id, or
        falls back to the focused pane.
        """
        target = self._resolve_pane(pane_id)
        if target is None:
            dlog(f"cli_notify: no pane (pane_id={pane_id!r})")
            return
        ws, tr, pane = target
        self._notify(ws, tr, pane, "cli")

    def claude_session(self, pane_id: str | None, state: str) -> None:
        """DBus `claude-session` entry. SessionStart/SessionEnd hooks call
        in to mark a pane's claude-active state authoritatively, replacing
        the /proc-foreground polling heuristic for routine UI decisions.
        """
        target = self._resolve_pane(pane_id)
        if target is None:
            return
        _ws, _tr, pane = target
        if state == "started":
            pane.mark_claude_active()
        elif state == "ended":
            pane._is_claude = False
            pane._claude_seen_mono = 0.0
            dlog(f"pane: claude-ended via hook id={pane_id!r}")

    def prompt_submit(self, pane_id: str | None) -> None:
        """DBus `prompt-submit` entry. UserPromptSubmit hook clears the
        pane's unread badge — you're back at the keyboard, so there's
        nothing left to notify about.
        """
        target = self._resolve_pane(pane_id)
        if target is None:
            return
        _ws, _tr, pane = target
        pane.clear_unread()

    def _resolve_pane(self, pane_id: str | None):
        """Find (ws, tr, pane) for an explicit pane_id, else the focused one."""
        if pane_id:
            for ws in self.workspaces:
                for tr in ws.tabs():
                    for p in tr.panes():
                        if getattr(p, "pane_id", None) == pane_id:
                            return (ws, tr, p)
        ws = self._current_workspace() or (self.workspaces[0] if self.workspaces else None)
        if ws is None:
            return None
        tr = ws.current_tab_root()
        if tr is None or tr.active_pane is None:
            return None
        return (ws, tr, tr.active_pane)

    def _flash_tab(self, tab_root: "TabRoot | None"):
        if tab_root is None:
            return
        # If a different tab is mid-flash, end it before retargeting.
        if self._flash_target is not None and self._flash_target is not tab_root:
            self._flash_target.remove_css_class("lmux-flash")
        self._flash_target = tab_root
        # Retrigger the CSS keyframe animation: remove first, re-add on the
        # next idle. (Re-adding within the same frame is a no-op.)
        tab_root.remove_css_class("lmux-flash")
        GLib.idle_add(lambda tr=tab_root: tr.add_css_class("lmux-flash") or False)
        if self._flash_tick_id is not None:
            GLib.source_remove(self._flash_tick_id)
        # Cleanup slightly after the 900 ms animation finishes.
        self._flash_tick_id = GLib.timeout_add(1000, self._flash_done)
        dlog("flash: started")

    def _flash_done(self) -> bool:
        if self._flash_target is not None:
            self._flash_target.remove_css_class("lmux-flash")
            self._flash_target = None
        self._flash_tick_id = None
        return False

    def _play_bell_sound(self):
        # Throttle to 500 ms (seance-style) so N panes firing attention at
        # the same moment don't spawn N concurrent canberra processes.
        now_t = GLib.get_monotonic_time() / 1_000_000.0
        if now_t - self._last_bell_mono < 0.5:
            dlog(f"sound: throttled (last={now_t - self._last_bell_mono:.2f}s ago)")
            return
        self._last_bell_mono = now_t
        for argv in (
            ["canberra-gtk-play", "-i", "message-new-instant", "--description=lmux"],
            ["paplay", "/usr/share/sounds/freedesktop/stereo/message-new-instant.oga"],
        ):
            try:
                Gio.Subprocess.new(
                    argv,
                    Gio.SubprocessFlags.STDOUT_SILENCE | Gio.SubprocessFlags.STDERR_SILENCE,
                )
                dlog(f"sound: spawned {argv[0]}")
                return
            except GLib.Error as e:
                dlog(f"sound: {argv[0]} failed: {e}")
                continue
        dlog("sound: no player worked")

    def _send_desktop_notification(
        self, ws: Workspace, tab_root: "TabRoot", pane: Pane
    ):
        """Deterministic toast body — '{workspace} · {tab title}'.

        Never scrapes terminal text. The summary identifies lmux; the body
        identifies which workspace and tab are asking for attention. Nerd
        Font glyphs are stripped because mako / libnotify renders them
        with the system notification font, not lmux's terminal font, so
        they appear as squished boxes or eat the surrounding whitespace.
        """
        app = self.get_application()
        if app is None:
            dlog("toast: no application")
            return
        tab_title = _plain_title(ws._label_title(tab_root, pane), is_claude=pane._is_claude)
        ws_name = _strip_nerd_glyphs(ws.name) or "lmux"
        notif = Gio.Notification.new("lmux")
        body = f"{ws_name} · {tab_title}"
        notif.set_body(body)
        notif.set_priority(Gio.NotificationPriority.NORMAL)
        nid = f"lmux-{ws.name}-{id(pane)}"
        app.send_notification(nid, notif)
        dlog(f"toast: send_notification id={nid} body={body!r}")

    def _current_workspace(self) -> Workspace | None:
        row = self.sidebar_list.get_selected_row()
        if row is None:
            return None
        return row.workspace

    # --- actions ---

    def _install_actions(self, app: Gtk.Application):
        # Catalog backs the command palette. Entries: (label, action-name, accels, palette-visible)
        self._action_catalog: list[tuple[str, str, list[str], bool]] = []

        def add(name: str, fn, accels: list[str], label: str | None = None,
                in_palette: bool = True):
            act = Gio.SimpleAction.new(name, None)
            act.connect("activate", lambda *_: fn())
            self.add_action(act)
            app.set_accels_for_action(f"win.{name}", accels)
            if label is not None:
                self._action_catalog.append((label, name, accels, in_palette))

        # Primary binds match the cross-app convention the user's Hyprland
        # config relies on (Super+T / Super+W sendshortcut Ctrl+T / Ctrl+W to
        # the active window). The Ctrl+Shift+* variants stay as aliases.
        add("new-tab", self._new_tab,
            ["<Ctrl>t", "<Ctrl><Shift>t"], "New tab")
        add("new-workspace", self.new_workspace, ["<Ctrl><Shift>n"], "New workspace")
        add("close-pane", self._close_pane,
            ["<Ctrl>w", "<Ctrl><Shift>w", "<Ctrl><Shift>q"], "Close pane / tab")
        add("restore-tab", self._restore_closed_tab, ["<Ctrl><Shift>z"], "Restore last closed tab")

        add("split-right", self._split_right, ["<Ctrl><Shift>d"], "Split right")
        add("split-down", self._split_down, ["<Ctrl><Shift>e"], "Split down")
        add("equalize", self._equalize, ["<Ctrl><Shift>0"], "Equalize splits")

        add("focus-left", lambda: self._focus_dir(-1, 0), ["<Alt>Left"], "Focus left pane")
        add("focus-right", lambda: self._focus_dir(1, 0), ["<Alt>Right"], "Focus right pane")
        add("focus-up", lambda: self._focus_dir(0, -1), ["<Alt>Up"], "Focus pane above")
        add("focus-down", lambda: self._focus_dir(0, 1), ["<Alt>Down"], "Focus pane below")

        add("next-tab", self._next_tab,
            ["<Ctrl>Tab", "<Ctrl>Page_Down", "<Alt>bracketright"], "Next tab")
        add("prev-tab", self._prev_tab,
            ["<Ctrl><Shift>Tab", "<Ctrl>Page_Up", "<Alt>bracketleft"], "Previous tab")
        add("next-workspace", self._next_workspace, ["<Ctrl><Alt>Down"], "Next workspace")
        add("prev-workspace", self._prev_workspace, ["<Ctrl><Alt>Up"], "Previous workspace")

        # select-tab-N / select-workspace-N are pure index lookups; hide from palette
        # (the palette will show actual workspace/tab names dynamically).
        for i in range(1, 10):
            add(f"select-tab-{i}", lambda i=i: self._select_tab(i - 1), [f"<Ctrl>{i}"])
            add(f"select-workspace-{i}", lambda i=i: self._select_workspace(i - 1), [f"<Alt>{i}"])

        add("toggle-sidebar", self._toggle_sidebar, ["<Ctrl>b"], "Toggle sidebar")

        add("zoom-in", lambda: self._zoom(0.1), ["<Ctrl>equal", "<Ctrl>plus"], "Zoom in")
        add("zoom-out", lambda: self._zoom(-0.1), ["<Ctrl>minus"], "Zoom out")
        add("zoom-reset", self._zoom_reset, ["<Ctrl>0"], "Reset zoom")

        # Insert-key bindings cover universal-paste shortcuts that desktop
        # environments synthesize (omarchy's Super+V → Shift+Insert, Super+C →
        # Ctrl+Insert). VTE's default Shift+Insert pastes from the X11 primary
        # selection, which doesn't match what a clipboard-manager populated
        # via Super+C left there — so route both through our clipboard paste.
        add("copy", self._copy, ["<Ctrl><Shift>c", "<Ctrl>Insert"], "Copy")
        add("paste", self._paste, ["<Ctrl><Shift>v", "<Shift>Insert"], "Paste")

        add("find", self._start_find, ["<Ctrl><Shift>f"], "Find in scrollback")

        add("jump-to-bell", self._jump_next_bell, ["<Ctrl><Shift>j"], "Jump to next belling tab")

        # Palette-only — no default keybinds.
        add("rename-tab", self._rename_current_tab, [], "Rename current tab")
        add("rename-workspace", self._rename_current_workspace, [], "Rename current workspace")

        add("command-palette", self._open_palette, ["<Ctrl><Shift>p"], "Command palette…")
        # Alt+Shift+F/O mirror the tmux-sessionizer / session-picker chords
        # but at the GTK level so they only fire when lmux is focused —
        # other apps still get their own Alt+Shift handlers.
        add("open-project", self.open_project_picker,
            ["<Alt><Shift>o", "<Ctrl><Shift>o"], "Open project…")
        add("switch-workspace", self.switch_workspace_picker,
            ["<Alt><Shift>f"], "Switch workspace…")

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

    # --- rename helpers (palette-only) ---

    def _rename_current_tab(self):
        ws = self._current_workspace()
        if ws is None:
            return
        tr = ws.current_tab_root()
        if tr is None:
            return
        label = ws._tabs.get(tr)
        if label is not None:
            label._start_edit()

    def _rename_current_workspace(self):
        ws = self._current_workspace()
        if ws is None:
            return
        row = self._rows.get(ws)
        if row is not None:
            row.start_rename()

    # --- jump to next belling tab ---

    def _jump_next_bell(self):
        cur_ws = self._current_workspace()
        if cur_ws is None or not self.workspaces:
            return
        cur_ws_i = self.workspaces.index(cur_ws)
        cur_tab = cur_ws.notebook.get_current_page()

        scan: list[tuple[Workspace, int]] = []
        for i in range(cur_tab + 1, cur_ws.notebook.get_n_pages()):
            scan.append((cur_ws, i))
        for off in range(1, len(self.workspaces) + 1):
            ws = self.workspaces[(cur_ws_i + off) % len(self.workspaces)]
            if ws is cur_ws:
                for i in range(0, cur_tab + 1):
                    scan.append((ws, i))
            else:
                for i in range(ws.notebook.get_n_pages()):
                    scan.append((ws, i))

        for ws, idx in scan:
            page = ws.notebook.get_nth_page(idx)
            if isinstance(page, TabRoot) and page in ws._notif:
                if ws is not cur_ws:
                    row = self._rows.get(ws)
                    if row is not None:
                        self.sidebar_list.select_row(row)
                ws.notebook.set_current_page(idx)
                return

    # --- scrollback search ---

    def _start_find(self):
        ws = self._current_workspace()
        pane = ws.current_pane() if ws else None
        if pane is None:
            return
        self._search_target_pane = pane
        try:
            pane.term.search_set_wrap_around(True)
        except Exception:
            pass
        self.search_bar.set_search_mode(True)
        self.search_entry.grab_focus()
        if self.search_entry.get_text():
            self.search_entry.select_region(0, -1)

    def _on_search_changed(self, entry: Gtk.SearchEntry):
        pane = self._search_target_pane
        if pane is None:
            return
        text = entry.get_text()
        if not text:
            try:
                pane.term.search_set_regex(None, 0)
            except Exception:
                pass
            self.search_count_label.set_text("")
            return
        try:
            # PCRE2_UTF | PCRE2_MULTILINE | PCRE2_CASELESS
            regex = Vte.Regex.new_for_search(GLib.Regex.escape_string(text), -1, 0x00080408)
            pane.term.search_set_regex(regex, 0)
            pane.term.search_find_previous()
        except (TypeError, GLib.Error) as e:
            dlog(f"search regex failed: {e}")
        self._update_search_count()

    def _update_search_count(self) -> None:
        pane = self._search_target_pane
        text = self.search_entry.get_text()
        if pane is None or not text:
            self.search_count_label.set_text("")
            return
        try:
            buf = pane.term.get_text_format(Vte.Format.TEXT)
        except Exception:
            buf = None
        if not buf:
            self.search_count_label.set_text("")
            return
        n = buf.lower().count(text.lower())
        self.search_count_label.set_text(
            "no matches" if n == 0 else ("1 match" if n == 1 else f"{n} matches")
        )

    def _search_step(self, forward: bool):
        pane = self._search_target_pane
        if pane is None or not self.search_entry.get_text():
            return
        try:
            if forward:
                pane.term.search_find_next()
            else:
                pane.term.search_find_previous()
        except Exception as e:
            dlog(f"search step failed: {e}")

    def _on_search_key(self, _ctrl, keyval, _kc, state):
        shift = bool(state & Gdk.ModifierType.SHIFT_MASK)
        if keyval == Gdk.KEY_Escape:
            self.search_bar.set_search_mode(False)
            return True
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            self._search_step(forward=shift)
            return True
        if keyval == Gdk.KEY_Up:
            self._search_step(forward=False)
            return True
        if keyval == Gdk.KEY_Down:
            self._search_step(forward=True)
            return True
        return False

    # --- command palette ---

    def _build_palette_overlay(self):
        # Build the palette widget tree but DO NOT attach it to self.overlay
        # yet. We attach on open and detach on close — that way when the
        # palette isn't visible it has no parent and cannot keep logical
        # keyboard focus, which is the bug we kept hitting with visibility
        # toggling alone.
        self.palette_root = Gtk.Overlay()

        backdrop = Gtk.Box()
        backdrop.set_hexpand(True)
        backdrop.set_vexpand(True)
        backdrop.add_css_class("lmux-palette-backdrop")
        bclick = Gtk.GestureClick.new()
        bclick.connect("pressed", lambda *_: self._close_palette())
        backdrop.add_controller(bclick)
        self.palette_root.set_child(backdrop)

        panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        panel.add_css_class("lmux-palette")
        panel.set_size_request(560, 420)
        panel.set_halign(Gtk.Align.CENTER)
        panel.set_valign(Gtk.Align.START)
        panel.set_margin_top(72)
        panel.set_hexpand(False)
        panel.set_vexpand(False)

        self.palette_entry = Gtk.SearchEntry()
        self.palette_entry.set_placeholder_text("Type a command…")
        self.palette_entry.add_css_class("lmux-palette-entry")
        self.palette_entry.connect("search-changed", self._on_palette_changed)
        self.palette_entry.connect("activate", lambda *_: self._palette_activate_selected())
        pk = Gtk.EventControllerKey.new()
        pk.connect("key-pressed", self._on_palette_key)
        self.palette_entry.add_controller(pk)
        panel.append(self.palette_entry)

        self.palette_scroll = Gtk.ScrolledWindow()
        self.palette_scroll.set_vexpand(True)
        self.palette_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.palette_list = Gtk.ListBox()
        self.palette_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.palette_list.add_css_class("lmux-palette-list")
        self.palette_list.connect("row-activated", lambda _l, _r: self._palette_activate_selected())
        self.palette_list.set_filter_func(self._palette_filter_row)
        self.palette_scroll.set_child(self.palette_list)
        panel.append(self.palette_scroll)

        self.palette_root.add_overlay(panel)

        self._palette_entries: list[tuple[str, str, object]] = []  # (label, accel_text, callback)
        self._palette_query: str = ""
        self._palette_mode: str = "all"  # "all" | "projects" | "workspaces"

    def _palette_is_open(self) -> bool:
        return self.palette_root.get_parent() is not None

    @staticmethod
    def _format_accel(accel: str) -> str:
        try:
            res = Gtk.accelerator_parse(accel)
        except Exception:
            return ""
        # PyGObject returns (success, key, mods) in GTK4.
        if isinstance(res, tuple) and len(res) >= 3 and res[0]:
            return Gtk.accelerator_get_label(res[1], res[2])
        return ""

    def _open_palette(self, mode: str = "all"):
        # When already open in a different mode, rebuild rather than no-op
        # so a CLI invocation can switch from the full command list to the
        # project picker without forcing the user to close first.
        if self._palette_is_open() and self._palette_mode == mode:
            self.palette_entry.grab_focus()
            return
        self._palette_mode = mode
        self._rebuild_palette_entries()
        self.palette_entry.set_text("")
        self._palette_query = ""
        self.palette_list.invalidate_filter()
        self._palette_select_first_visible()
        if not self._palette_is_open():
            self.overlay.add_overlay(self.palette_root)
        # Placeholder text hints the mode.
        hint = {
            "projects": "open project…",
            "workspaces": "switch workspace…",
        }.get(mode, "type a command…")
        self.palette_entry.set_placeholder_text(hint)
        GLib.idle_add(self.palette_entry.grab_focus)

    def open_project_picker(self):
        self._open_palette(mode="projects")

    def switch_workspace_picker(self):
        self._open_palette(mode="workspaces")

    def _close_palette(self):
        if not self._palette_is_open():
            return
        # Move focus to the terminal first while the entry is still attached;
        # then detach the palette entirely. With no parent, the palette's
        # SearchEntry cannot keep logical focus.
        ws = self._current_workspace()
        pane = ws.current_pane() if ws is not None else None
        if pane is not None:
            pane.term.grab_focus()
        else:
            self.set_focus(None)
        self.overlay.remove_overlay(self.palette_root)
        GLib.idle_add(self._focus_current_pane)

    def _rebuild_palette_entries(self):
        entries: list[tuple[str, str, object]] = []

        mode = self._palette_mode
        cur_ws = self._current_workspace()

        # Static actions from the catalog (only in "all" mode).
        if mode == "all":
            for label, name, accels, in_palette in self._action_catalog:
                if not in_palette:
                    continue
                accel_text = self._format_accel(accels[0]) if accels else ""
                entries.append((label, accel_text,
                                lambda n=name: self.activate_action(n, None)))

        # Dynamic: workspaces. In picker mode, most-recently-active first,
        # current workspace moved to the bottom (tmux-session-picker style —
        # you rarely want to switch to where you already are).
        if mode in ("all", "workspaces"):
            ordered = list(self.workspaces)
            if mode == "workspaces":
                ordered.sort(key=lambda w: -w.last_active_mono)
                if cur_ws in ordered:
                    ordered.remove(cur_ws)
                    ordered.append(cur_ws)
            for ws in ordered:
                marker = "  ●" if ws is cur_ws else ""
                entries.append((f"Go to workspace: {ws.name}{marker}", "",
                                lambda w=ws: self._palette_goto_workspace(w)))

        # Dynamic: tabs in current workspace.
        if mode == "all" and cur_ws is not None:
            for ti in range(cur_ws.notebook.get_n_pages()):
                tab = cur_ws.notebook.get_nth_page(ti)
                if not isinstance(tab, TabRoot):
                    continue
                lbl = cur_ws._tabs.get(tab)
                raw_title = (lbl.label.get_text() if lbl
                             else cur_ws._label_title(tab, tab.active_pane))
                # Strip Nerd Font glyphs — GTK's palette label font often
                # lacks proper metrics for the robot/bell codepoints, which
                # eats the trailing space and squishes the text.
                is_claude = (tab.active_pane is not None and tab.active_pane._is_claude)
                title_text = _plain_title(raw_title, is_claude=is_claude)
                marker = "  ●" if ti == cur_ws.notebook.get_current_page() else ""
                entries.append((f"Go to tab: {title_text}{marker}", "",
                                lambda i=ti: self._palette_goto_tab(i)))

        # Dynamic: projects.
        if mode == "projects":
            open_ws_names = {w.name for w in self.workspaces}
            for name, path in list_project_dirs():
                marker = "  ●" if name in open_ws_names else ""
                entries.append((f"{name}{marker}", path,
                                lambda p=path: self._palette_open_project(p)))

        self._palette_entries = entries

        # Rebuild listbox rows to match entries.
        child = self.palette_list.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.palette_list.remove(child)
            child = nxt
        for label, accel_text, _cb in entries:
            row = Gtk.ListBoxRow()
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            box.set_margin_start(10)
            box.set_margin_end(10)
            box.set_margin_top(4)
            box.set_margin_bottom(4)
            name_lbl = Gtk.Label(label=label)
            name_lbl.set_xalign(0)
            name_lbl.set_hexpand(True)
            name_lbl.set_ellipsize(Pango.EllipsizeMode.END)
            box.append(name_lbl)
            if accel_text:
                accel_lbl = Gtk.Label(label=accel_text)
                accel_lbl.add_css_class("lmux-palette-accel")
                box.append(accel_lbl)
            row.set_child(box)
            self.palette_list.append(row)

    def _palette_matches(self, label: str, q: str) -> bool:
        if not q:
            return True
        qi = 0
        for ch in label.lower():
            if qi < len(q) and ch == q[qi]:
                qi += 1
                if qi == len(q):
                    return True
        return False

    def _palette_visible_indices(self) -> list[int]:
        q = self._palette_query.strip().lower()
        return [i for i, (label, _, _) in enumerate(self._palette_entries)
                if self._palette_matches(label, q)]

    def _palette_filter_row(self, row) -> bool:
        idx = row.get_index()
        if idx < 0 or idx >= len(self._palette_entries):
            return False
        return self._palette_matches(self._palette_entries[idx][0], self._palette_query.strip().lower())

    def _on_palette_changed(self, entry: Gtk.SearchEntry):
        self._palette_query = entry.get_text()
        self.palette_list.invalidate_filter()
        self._palette_select_first_visible()

    def _palette_select_first_visible(self):
        idxs = self._palette_visible_indices()
        if not idxs:
            self.palette_list.unselect_all()
            return
        row = self.palette_list.get_row_at_index(idxs[0])
        if row is not None:
            self.palette_list.select_row(row)
            self._palette_scroll_into_view(row)

    def _palette_move_selection(self, delta: int):
        idxs = self._palette_visible_indices()
        if not idxs:
            return
        cur = self.palette_list.get_selected_row()
        cur_idx = cur.get_index() if cur is not None else -1
        if cur_idx in idxs:
            pos = idxs.index(cur_idx)
        else:
            pos = 0 if delta > 0 else len(idxs) - 1
            pos = max(0, min(len(idxs) - 1, pos))
            row = self.palette_list.get_row_at_index(idxs[pos])
            if row is not None:
                self.palette_list.select_row(row)
                self._palette_scroll_into_view(row)
            return
        new_pos = max(0, min(len(idxs) - 1, pos + delta))
        target_row = self.palette_list.get_row_at_index(idxs[new_pos])
        if target_row is not None:
            self.palette_list.select_row(target_row)
            self._palette_scroll_into_view(target_row)

    def _palette_scroll_into_view(self, row: Gtk.ListBoxRow) -> None:
        # Defer to idle so the row has a valid allocation, then nudge the
        # scrolled window's vadjustment if the row is above/below the viewport.
        def do_scroll():
            alloc = row.get_allocation()
            if alloc.height <= 0:
                return False  # not allocated yet; try again next idle
            vadj = self.palette_scroll.get_vadjustment()
            if vadj is None:
                return False
            page = vadj.get_page_size()
            top = vadj.get_value()
            row_top = alloc.y
            row_bot = row_top + alloc.height
            if row_top < top:
                vadj.set_value(row_top)
            elif row_bot > top + page:
                vadj.set_value(row_bot - page)
            return False
        GLib.idle_add(do_scroll)

    def _on_palette_key(self, _ctrl, keyval, _kc, state):
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        if keyval == Gdk.KEY_Escape:
            self._close_palette()
            return True
        if keyval == Gdk.KEY_Down or (ctrl and keyval in (Gdk.KEY_n, Gdk.KEY_N)):
            self._palette_move_selection(1)
            return True
        if keyval == Gdk.KEY_Up or (ctrl and keyval in (Gdk.KEY_p, Gdk.KEY_P)):
            self._palette_move_selection(-1)
            return True
        return False

    def _palette_activate_selected(self):
        row = self.palette_list.get_selected_row()
        if row is None:
            return
        idx = row.get_index()
        if not (0 <= idx < len(self._palette_entries)):
            return
        _label, _accel, cb = self._palette_entries[idx]
        self._close_palette()
        cb()

    def _palette_goto_workspace(self, ws: Workspace):
        row = self._rows.get(ws)
        if row is not None:
            self.sidebar_list.select_row(row)

    def _palette_goto_tab(self, idx: int):
        ws = self._current_workspace()
        if ws is not None:
            ws.select_tab(idx)

    def _palette_open_project(self, path: str):
        self.open_project(path)

    def _on_search_mode_changed(self, *_):
        if not self.search_bar.get_search_mode():
            pane = self._search_target_pane
            self._search_target_pane = None
            self.search_count_label.set_text("")
            if pane is not None:
                try:
                    pane.term.search_set_regex(None, 0)
                except Exception:
                    pass
                pane.focus_term()


CSS = b"""
/* ---- accents ----------------------------------------------------------- */
.lmux-bell {
    font-family: monospace;
    color: #4c8bf2;
    font-size: 0.85em;
    font-weight: 600;
    min-width: 14px;
    margin: 0 2px 0 0;
}
/* Tab titles can contain Nerd Font glyphs (e.g. the claude prefix) -
   force the same font family for the whole label so the glyph and
   surrounding text share metrics and align on the baseline. */
notebook header tab label {
    font-family: monospace;
}
.lmux-chip-ready {
    font-family: monospace;
}
.lmux-active-pane { box-shadow: inset 0 0 0 1px #4c8bf2; }

/* Bouncy attention flash on the tab content (not the whole window).
   Box-shadow oscillates between large/small inset rings to give a
   pronounced bounce instead of a steady glow. */
@keyframes lmux-flash {
    0%   { box-shadow: inset 0 0 0 0px  alpha(#4c8bf2, 0.0); }
    14%  { box-shadow: inset 0 0 0 9px  alpha(#4c8bf2, 0.95); }
    32%  { box-shadow: inset 0 0 0 2px  alpha(#4c8bf2, 0.30); }
    50%  { box-shadow: inset 0 0 0 7px  alpha(#4c8bf2, 0.85); }
    68%  { box-shadow: inset 0 0 0 3px  alpha(#4c8bf2, 0.45); }
    84%  { box-shadow: inset 0 0 0 5px  alpha(#4c8bf2, 0.65); }
    100% { box-shadow: inset 0 0 0 0px  alpha(#4c8bf2, 0.0); }
}
.lmux-flash {
    animation: lmux-flash 900ms ease-out 1;
}

/* ---- sidebar shell ----------------------------------------------------- */
.sidebar {
    background-color: mix(alpha(currentColor, 0.04), alpha(#4c8bf2, 0.08), 0.5);
    border-right: 1px solid alpha(#4c8bf2, 0.30);
}

.lmux-new-ws {
    padding: 7px 10px;
    margin: 6px 6px 4px 6px;
    border-radius: 6px;
    min-height: 0;
    color: alpha(currentColor, 0.55);
    background: transparent;
    border: 1px solid alpha(currentColor, 0.12);
    font-size: 0.85em;
}
.lmux-new-ws:hover {
    color: currentColor;
    background-color: alpha(currentColor, 0.06);
    border-color: alpha(currentColor, 0.25);
}

/* ---- sidebar rows ------------------------------------------------------ */
.navigation-sidebar > row {
    padding: 7px 4px;
    margin: 1px 6px;
    border-radius: 6px;
    background: transparent;
    transition: background-color 120ms;
}
.navigation-sidebar > row:hover {
    background-color: alpha(currentColor, 0.05);
}
.navigation-sidebar > row:selected {
    background-color: alpha(currentColor, 0.10);
    box-shadow: inset 2px 0 0 0 #4c8bf2;
}
.navigation-sidebar > row:selected:hover {
    background-color: alpha(currentColor, 0.14);
}
.navigation-sidebar > row.lmux-drop-target {
    box-shadow: inset 0 2px 0 0 #4c8bf2;
}
.navigation-sidebar > row:focus,
.navigation-sidebar > row:focus-visible {
    outline: none;
}

.lmux-ws-name {
    font-weight: 500;
    font-size: 0.96em;
}
.lmux-ws-sub {
    opacity: 0.55;
    font-size: 0.78em;
    margin-top: 1px;
}
.lmux-ws-entry {
    font-weight: 500;
    font-size: 0.96em;
    min-height: 0;
    padding: 0 4px;
}
.lmux-tab-entry {
    min-height: 0;
    padding: 0 4px;
    font-size: 0.9em;
}
.lmux-search-count {
    opacity: 0.6;
    font-size: 0.85em;
    padding: 0 6px;
}

/* ---- command palette --------------------------------------------------- */
.lmux-palette-backdrop {
    background-color: alpha(#000000, 0.35);
}
.lmux-palette {
    background-color: mix(@theme_bg_color, #1a1d23, 0.6);
    border: 1px solid alpha(#4c8bf2, 0.45);
    border-radius: 8px;
    box-shadow: 0 8px 24px alpha(#000000, 0.5);
    padding: 4px;
}
.lmux-palette-entry {
    margin: 4px;
    min-height: 28px;
}
.lmux-palette-list { background: transparent; }
.lmux-palette-list > row {
    padding: 4px 6px;
    margin: 1px 4px;
    border-radius: 5px;
    background: transparent;
}
.lmux-palette-list > row:hover {
    background-color: alpha(currentColor, 0.07);
}
.lmux-palette-list > row:selected {
    background-color: alpha(#4c8bf2, 0.25);
    color: currentColor;
}
.lmux-palette-accel {
    opacity: 0.55;
    font-size: 0.8em;
    background-color: alpha(currentColor, 0.10);
    padding: 1px 6px;
    border-radius: 4px;
}

/* ---- chips & badges ---------------------------------------------------- */
.lmux-chip-ready {
    font-family: monospace;
    background-color: #4c8bf2;
    color: #ffffff;
    font-size: 0.78em;
    font-weight: 600;
    padding: 1px 7px;
    border-radius: 9px;
    min-width: 12px;
}

.lmux-split-count {
    background-color: alpha(currentColor, 0.14);
    color: alpha(currentColor, 0.85);
    font-size: 0.68em;
    font-weight: 600;
    padding: 0 5px;
    border-radius: 8px;
    margin-left: 2px;
}

/* ---- close buttons (x) on tabs and sidebar rows ----------------------- */
.lmux-tab-close,
.lmux-ws-close {
    min-width: 18px;
    min-height: 18px;
    padding: 0;
    margin: 0 0 0 4px;
    background: transparent;
    border: none;
    color: alpha(currentColor, 0.35);
    opacity: 0;
    transition: opacity 100ms, color 100ms, background-color 100ms;
}
notebook header tab:hover .lmux-tab-close,
.lmux-tab-close:hover,
.lmux-tab-close:focus {
    opacity: 1;
}
.navigation-sidebar > row:hover .lmux-ws-close,
.lmux-ws-close:hover,
.lmux-ws-close:focus {
    opacity: 1;
}
.lmux-tab-close:hover,
.lmux-ws-close:hover {
    color: currentColor;
    background-color: alpha(currentColor, 0.14);
    border-radius: 4px;
}

/* ---- tab-strip action buttons (top-right) ----------------------------- */
.lmux-tab-actions {
    padding: 2px 8px 2px 4px;
}
.lmux-tab-btn {
    min-height: 24px;
    min-width: 28px;
    padding: 2px 4px;
    margin: 0;
    border: none;
    border-radius: 4px;
    background: transparent;
    color: alpha(currentColor, 0.55);
}
.lmux-tab-btn:hover {
    color: currentColor;
    background-color: alpha(currentColor, 0.08);
}
.lmux-tab-btn:active {
    background-color: alpha(currentColor, 0.14);
}

/* ---- notebook tab strip ----------------------------------------------- */
notebook header {
    padding: 0;
    background-color: alpha(currentColor, 0.04);
    border-bottom: 1px solid alpha(#4c8bf2, 0.30);
}
notebook header.top tabs { padding: 2px 2px 0 2px; }
notebook header tab {
    padding: 5px 12px;
    min-height: 0;
    margin: 0 1px;
    border: none;
    border-radius: 4px 4px 0 0;
    background: transparent;
    color: alpha(currentColor, 0.55);
    transition: color 100ms, background-color 100ms;
}
notebook header tab:hover {
    color: alpha(currentColor, 0.9);
    background-color: alpha(currentColor, 0.06);
}
notebook header tab:checked {
    color: currentColor;
    font-weight: 500;
    background-color: alpha(currentColor, 0.10);
    box-shadow: inset 0 -2px 0 0 #4c8bf2;
}
notebook header tab label { padding: 0; }
notebook header tab button {
    min-height: 0;
    min-width: 0;
    padding: 0 2px;
    background: transparent;
    border: none;
    color: alpha(currentColor, 0.45);
}
notebook header tab button:hover {
    color: currentColor;
    background-color: alpha(currentColor, 0.08);
    border-radius: 4px;
}

/* ---- paned separators -------------------------------------------------- */
paned > separator {
    min-width: 2px;
    min-height: 2px;
    background-color: alpha(#4c8bf2, 0.25);
    transition: background-color 120ms;
}
paned > separator:hover,
paned > separator:active {
    background-color: #4c8bf2;
}
"""


class LmuxApp(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        # Exposed on the session bus via GApplication's standard
        # /<APP_ID-as-path> object so the `lmux` CLI (and Claude Code hooks
        # via the wrapper) can dispatch attention events back to us.
        for name, handler in (
            ("notify", self._on_notify_action),
            ("claude-session", self._on_claude_session_action),
            ("prompt-submit", self._on_prompt_submit_action),
            ("open-project", self._on_open_project_action),
            ("open-project-picker", self._on_open_project_picker_action),
            ("switch-workspace", self._on_switch_workspace_action),
            ("switch-workspace-picker", self._on_switch_workspace_picker_action),
            ("command-palette", self._on_command_palette_action),
        ):
            act = Gio.SimpleAction.new(name, GLib.VariantType.new("a{sv}"))
            act.connect("activate", handler)
            self.add_action(act)

    def _on_notify_action(self, _act, param):
        win = self.get_active_window()
        if win is None:
            return
        d = param.unpack() if param is not None else {}
        title = str(d.get("title", "lmux") or "lmux")
        body = str(d.get("body", "") or "")
        pane_id = d.get("pane_id")
        win.cli_notify(title, body, pane_id=str(pane_id) if pane_id else None)

    def _on_claude_session_action(self, _act, param):
        win = self.get_active_window()
        if win is None:
            return
        d = param.unpack() if param is not None else {}
        pane_id = d.get("pane_id")
        state = str(d.get("state", "") or "")
        win.claude_session(str(pane_id) if pane_id else None, state)

    def _on_prompt_submit_action(self, _act, param):
        win = self.get_active_window()
        if win is None:
            return
        d = param.unpack() if param is not None else {}
        pane_id = d.get("pane_id")
        win.prompt_submit(str(pane_id) if pane_id else None)

    def _on_open_project_action(self, _act, param):
        win = self.get_active_window()
        if win is None:
            return
        d = param.unpack() if param is not None else {}
        path = d.get("path")
        if path:
            win.present()
            win.open_project(str(path))

    def _on_open_project_picker_action(self, _act, _param):
        win = self.get_active_window()
        if win is None:
            return
        win.present()
        win.open_project_picker()

    def _on_switch_workspace_picker_action(self, _act, _param):
        win = self.get_active_window()
        if win is None:
            return
        win.present()
        win.switch_workspace_picker()

    def _on_switch_workspace_action(self, _act, param):
        win = self.get_active_window()
        if win is None:
            return
        d = param.unpack() if param is not None else {}
        name = str(d.get("name", "") or "")
        if not name:
            return
        win.present()
        for ws in win.workspaces:
            if ws.name == name:
                win.sidebar_list.select_row(win._rows[ws])
                return
        dlog(f"switch-workspace: no workspace named {name!r}")

    def _on_command_palette_action(self, _act, _param):
        win = self.get_active_window()
        if win is None:
            return
        win.present()
        win._open_palette()

    def do_activate(self):
        install_claude_wrapper()
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


def _dbus_call_action(action: str, payload: dict[str, "GLib.Variant"]) -> bool:
    """Activate an app-level action on the running LmuxApp over the
    session bus. Returns True iff the call succeeded.
    """
    try:
        conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        if conn is None:
            return False
        obj_path = "/" + APP_ID.replace(".", "/")
        params = GLib.Variant(
            "(sava{sv})",
            (action, [GLib.Variant("a{sv}", payload)], {}),
        )
        conn.call_sync(
            APP_ID, obj_path, "org.gtk.Actions", "Activate", params,
            None, Gio.DBusCallFlags.NONE, 2000, None,
        )
        return True
    except GLib.Error as e:
        dlog(f"DBus call {action} failed: {e}")
        return False


def _resolve_cli_pane_id(explicit: str | None) -> str | None:
    return explicit or os.environ.get("LMUX_PANE_ID") or None


def _parse_kv_args(args: list[str], known_flags: set[str]) -> tuple[dict[str, str], int]:
    """Tiny long-flag parser: returns ({flag: value}, exit_code_or_0).

    Unknown bare flags (no value) are accepted silently so claude sessions
    launched with an older wrapper (e.g. one that passed --from-hook) keep
    working across an lmux upgrade.
    """
    out: dict[str, str] = {}
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-h", "--help"):
            out["__help"] = "1"
            return out, 0
        if a in known_flags and i + 1 < len(args):
            out[a.lstrip("-").replace("-", "_")] = args[i + 1]
            i += 2
            continue
        if a.startswith("--"):
            # Skip unknown bare flag for forward/backward compat.
            i += 1
            continue
        sys.stderr.write(f"unknown argument {a!r}\n")
        return out, 2
    return out, 0


def _cli_notify(args: list[str]) -> int:
    flags, rc = _parse_kv_args(args, {"--title", "--body", "--pane-id"})
    if rc:
        return rc
    if "__help" in flags:
        sys.stdout.write(
            "usage: lmux notify [--title TITLE] [--body BODY] [--pane-id ID]\n"
            "Fires an attention event on the targeted pane (defaults to $LMUX_PANE_ID).\n"
        )
        return 0
    payload = {
        "title": GLib.Variant("s", flags.get("title", "lmux")),
        "body": GLib.Variant("s", flags.get("body", "")),
    }
    pane_id = _resolve_cli_pane_id(flags.get("pane_id"))
    if pane_id:
        payload["pane_id"] = GLib.Variant("s", pane_id)
    _dbus_call_action("notify", payload)
    return 0


def _cli_claude_session(args: list[str]) -> int:
    flags, rc = _parse_kv_args(args, {"--state", "--pane-id"})
    if rc:
        return rc
    if "__help" in flags:
        sys.stdout.write(
            "usage: lmux claude-session --state <started|ended> [--pane-id ID]\n"
        )
        return 0
    state = flags.get("state", "")
    if state not in ("started", "ended"):
        sys.stderr.write("lmux claude-session: --state must be started or ended\n")
        return 2
    payload = {"state": GLib.Variant("s", state)}
    pane_id = _resolve_cli_pane_id(flags.get("pane_id"))
    if pane_id:
        payload["pane_id"] = GLib.Variant("s", pane_id)
    _dbus_call_action("claude-session", payload)
    return 0


def _cli_prompt_submit(args: list[str]) -> int:
    flags, rc = _parse_kv_args(args, {"--pane-id"})
    if rc:
        return rc
    if "__help" in flags:
        sys.stdout.write("usage: lmux prompt-submit [--pane-id ID]\n")
        return 0
    payload: dict[str, "GLib.Variant"] = {}
    pane_id = _resolve_cli_pane_id(flags.get("pane_id"))
    if pane_id:
        payload["pane_id"] = GLib.Variant("s", pane_id)
    _dbus_call_action("prompt-submit", payload)
    return 0


def _split_flags_and_positional(
    args: list[str], known_flags: set[str]
) -> tuple[dict[str, str], list[str]]:
    """Single-pass split: returns (flag_dict, [positional, ...]). Unknown
    bare --flags are skipped (forward-compat with older wrappers).
    """
    flags: dict[str, str] = {}
    positional: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-h", "--help"):
            flags["__help"] = "1"
            return flags, positional
        if a in known_flags and i + 1 < len(args):
            flags[a.lstrip("-").replace("-", "_")] = args[i + 1]
            i += 2
            continue
        if a.startswith("--"):
            i += 1  # unknown bare flag, drop silently
            continue
        positional.append(a)
        i += 1
    return flags, positional


def _cli_open_project(args: list[str]) -> int:
    flags, positional = _split_flags_and_positional(args, {"--path"})
    if "__help" in flags:
        sys.stdout.write(
            "usage: lmux open-project [BASENAME | --path PATH]\n"
            "\n"
            "BASENAME: look up under $LMUX_PROJECT_DIRS and open.\n"
            "--path PATH: explicit absolute / ~-rooted path.\n"
            "No args: open the fuzzy picker.\n"
            f"Search roots default to {PROJECT_ROOTS_DEFAULT}.\n"
        )
        return 0
    target = flags.get("path")
    if target is None and positional:
        wanted = positional[0]
        for name, full in list_project_dirs():
            if name == wanted:
                target = full
                break
        if target is None:
            sys.stderr.write(f"lmux open-project: no project named {wanted!r}\n")
            return 1
    if target:
        _dbus_call_action("open-project", {"path": GLib.Variant("s", target)})
    else:
        _dbus_call_action("open-project-picker", {})
    return 0


def _cli_switch_workspace(args: list[str]) -> int:
    flags, positional = _split_flags_and_positional(args, set())
    if "__help" in flags:
        sys.stdout.write(
            "usage: lmux switch-workspace [NAME]\n"
            "\nNAME: switch directly to that workspace (no picker).\n"
            "No args: open the workspace picker.\n"
        )
        return 0
    if positional:
        _dbus_call_action("switch-workspace",
                          {"name": GLib.Variant("s", positional[0])})
    else:
        _dbus_call_action("switch-workspace-picker", {})
    return 0


def _cli_command_palette(args: list[str]) -> int:
    flags, rc = _parse_kv_args(args, set())
    if rc:
        return rc
    if "__help" in flags:
        sys.stdout.write("usage: lmux command-palette\n")
        return 0
    _dbus_call_action("command-palette", {})
    return 0


CLI_HANDLERS = {
    "notify": _cli_notify,
    "claude-session": _cli_claude_session,
    "prompt-submit": _cli_prompt_submit,
    "open-project": _cli_open_project,
    "switch-workspace": _cli_switch_workspace,
    "command-palette": _cli_command_palette,
}


def main() -> int:
    argv = sys.argv
    if len(argv) >= 2 and argv[1] in CLI_HANDLERS:
        return CLI_HANDLERS[argv[1]](argv[2:])
    return LmuxApp().run(argv)


if __name__ == "__main__":
    sys.exit(main())
