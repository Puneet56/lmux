# lmux

A minimal Linux take on [cmux](https://github.com/manaflow-ai/cmux)'s tab UI — vertical workspace sidebar + horizontal terminal tabs with splits.

GTK4 + VTE + Python, single file. Built for running [Claude Code](https://docs.anthropic.com/en/docs/claude-code) agents in parallel without losing track of which one needs you.

![lmux screenshot](docs/screenshot.png)

## Features

- **Workspaces** in a vertical sidebar, each with its own set of **horizontal tabs**
- **Splits** within a tab — split right (`Ctrl+Shift+D`) or down (`Ctrl+Shift+E`), focus-move with `Alt`+arrows, equalize with `Ctrl+Shift+0`
- **Rename tabs and workspaces** — double-click any tab title or sidebar workspace name, Enter to commit, Esc to cancel. Custom titles override the VTE-driven one and survive restart.
- **Scrollback search** — `Ctrl+Shift+F` opens a search bar with a live hit count; Enter jumps to the previous (older) match, Shift+Enter to the next (newer), Esc closes
- **Jump to next bell** — `Ctrl+Shift+J` cycles you to the next tab waiting on you, across workspaces
- **Command palette** — `Ctrl+Shift+P` opens a fuzzy-searchable list of every action plus "Go to workspace…" / "Go to tab…" entries. Esc / click-outside closes
- **Project picker (tmux-sessionizer style)** — `Alt+Shift+O` (or `Ctrl+Shift+O`, or `lmux open-project [basename]` from any shell) opens a fuzzy picker over directories under `~/Projects` and `~/Work`. Hitting Enter creates a new workspace named after the project with three tabs — **editor** (`nvim`, focused), **claude** (`claude --dangerously-skip-permissions`), and **shell** — or switches to an already-open workspace with that name. Override the search roots via `LMUX_PROJECT_DIRS=path1:path2`. Pass a `basename` positional to skip the picker
- **Workspace picker** — `Alt+Shift+F` (or `lmux switch-workspace [name]`) opens the palette in workspace-only mode, sorted most-recently-active first with the current workspace at the bottom (tmux-session-picker behavior). Both internal binds are GTK-level so they only fire while lmux is the focused window. Pass a `name` positional to switch directly
- **Persisted layout** — workspaces, tabs, splits, cwds, and custom titles are saved on window close to `~/.cache/lmux/state.json` and restored on next launch. Editor (`nvim`) and claude tabs in a project workspace are detected via `/proc` session scan at save time and re-spawned automatically on restore
- **Claude session auto-resume** — panes that had `claude` as their foreground process at save time are restored with `claude --continue --dangerously-skip-permissions` typed in automatically, so you land back in your conversation. Override the command via `LMUX_CLAUDE_RESUME_CMD=...`; set it to empty to disable
- **Notifications via DBus, routed per pane** (cmux-style) — every pane gets a `LMUX_PANE_ID` UUID exposed to its shell env. Claude Code hooks (`Notification`, `Stop`, `SessionStart`, `SessionEnd`, `UserPromptSubmit`) call back via the `lmux` CLI subcommands (`notify`, `claude-session`, `prompt-submit`), which dispatch through the session bus to the running lmux's `notify`/`claude-session`/`prompt-submit` actions — landing on the exact pane claude is running in. No OSC parsing, no terminal scraping, no idle heuristics. Hooks are wired by an auto-installed `~/.cache/lmux/bin/claude` wrapper (see the Claude Code section below)
- **Audible bell** (`message-new-instant.oga` via `canberra-gtk-play`, 500 ms throttle) + **counted badge on tab and sidebar row** + **mako desktop toast** + **bouncy tab flash** — toast suppressed when the ringing pane is focused-here; sound + flash + count always fire (subject to the sound throttle when many panes ring at once)
- **Debug logging** — `LMUX_DEBUG=1 ./lmux.py 2>/tmp/lmux.log` traces every step from VTE signal to action dispatch to UI update
- **Sidebar metadata** — each workspace row shows its current pane's `cwd` and git branch (`.git/HEAD` walk, no subprocess). Branch changes (e.g. `git checkout`) are picked up live via 1.2 s poll without leaving the pane focused
- **Live kitty theme reload** — reads `~/.config/kitty/kitty.conf` (resolving `include`), follows omarchy theme switches via a file monitor on `theme.name`
- **Ctrl+click URLs** to open in `xdg-open`
- **Restore closed tab** (`Ctrl+Shift+Z`) with a 16-deep ring buffer
- `cwd` tracking via `tcgetpgrp(pty_fd)` → `/proc/<pgrp>/cwd` (works regardless of shell OSC 7 setup)

## Install

```bash
sudo pacman -S --needed vte4 gtk4 python-gobject libcanberra sound-theme-freedesktop

git clone https://github.com/Puneet56/lmux.git
cd lmux

# Run directly:
./lmux.py

# Install for current user (real copy, not symlink — see "Hacking on lmux" below for why):
mkdir -p ~/.local/bin ~/.local/share/applications
install -m 0755 lmux.py ~/.local/bin/lmux
cp lmux.desktop ~/.local/share/applications/

# To upgrade after pulling new changes:
install -m 0755 lmux.py ~/.local/bin/lmux
```

## Hacking on lmux

The installed copy at `~/.local/bin/lmux` is a real file, not a symlink — so the lmux you run every day (launched from the desktop entry) stays stable even when you're editing the source tree.

To iterate without disturbing your running daily driver, launch the working tree in dev mode:

```bash
LMUX_DEV=1 ./lmux.py
```

`LMUX_DEV=1` switches the `APP_ID` (so GApplication single-instance won't merge dev windows into your stable one), uses a separate `~/.cache/lmux/state-dev.json` (so dev sessions don't clobber your daily layout), and tags the window title with `(dev)`. You can run a dev instance alongside the stable one happily.

When the dev version is good, "release" it to your daily driver:

```bash
install -m 0755 lmux.py ~/.local/bin/lmux
```

Next launch of the desktop entry picks up the new code. Old stable windows keep running the previous version until you close them.

## Claude Code setup

**No configuration required.** lmux follows [cmux](https://github.com/manaflow-ai/cmux)'s notification model: a small bash wrapper at `~/.cache/lmux/bin/claude` (auto-installed on every launch) shadows the real `claude` binary inside lmux panes and injects five hooks via Claude Code's `--settings` flag. Outside lmux panes the wrapper passes through unchanged, and `--settings` merges additively with your own `~/.claude/settings.json` so nothing in your config gets clobbered.

End-to-end:

1. lmux launches → writes `~/.cache/lmux/bin/claude` and prepends that dir to every pane shell's `PATH`. Each pane's shell env carries `LMUX_PANE_ID=<uuid>` so the wrapper (and the CLI it calls) can identify exactly which pane is asking.
2. You type `claude` in a pane → the wrapper resolves the real claude (walks PATH, skips its own dir), then `exec`s it with `--settings '{"hooks":{...}}'` containing entries for **Notification**, **Stop**, **SessionStart**, **SessionEnd**, and **UserPromptSubmit**.
3. Each hook command calls a lmux CLI subcommand:
   - `Notification` → `lmux notify --title Claude --body "needs input"` — bell + badge + toast (if not focused).
   - `Stop` → `lmux notify --title Claude --body done` — same.
   - `SessionStart` → `lmux claude-session --state started` — marks the pane authoritatively as claude-running (tab title gains the `claude:` prefix, save-time auto-resume becomes reliable).
   - `SessionEnd` → `lmux claude-session --state ended` — clears the marker.
   - `UserPromptSubmit` → `lmux prompt-submit` — clears the pane's unread badge (you're at the keyboard, so there's nothing left to flag).
4. Every CLI subcommand reads `$LMUX_PANE_ID` from the inherited env and dispatches via DBus to the running lmux's app-level action (`notify`, `claude-session`, `prompt-submit`). The action handler looks the pane up by id and runs the in-app pipeline on the exact pane claude is running in.

Manual testing: `lmux notify --title Test --body hi` from any shell (inside or outside lmux). The CLI just dispatches via DBus — there's no OSC 777 or `/dev/tty` path involved (VTE 0.84 stopped recognizing OSC 777 anyway).

If lmux isn't running when a hook fires, the CLI exits 0 silently — the hook doesn't fail and claude doesn't penalize it.

## Keymap

| Shortcut | Action |
|---|---|
| `Ctrl+Shift+P` | Command palette (fuzzy search every action + workspaces + tabs) |
| `Alt+Shift+O` / `Ctrl+Shift+O` | Open project… — fuzzy-pick a directory under `~/Projects` / `~/Work`, create-or-switch a workspace with editor + shell tabs (tmux-sessionizer equivalent) |
| `Alt+Shift+F` | Switch workspace… — fuzzy-pick an existing workspace (tmux-session-picker equivalent). Only fires while an lmux window is focused so it doesn't interfere with other apps' Alt+Shift+F handlers |
| `Ctrl+B` | Toggle sidebar |
| `Ctrl+T` / `Ctrl+Shift+T` | New tab (inherits cwd of focused pane). `Ctrl+T` matches the cross-app new-tab convention so a WM bind that sends `Ctrl+T` (e.g. `Super+T → sendshortcut CTRL,T`) works here |
| `Ctrl+Shift+N` | New workspace |
| `Ctrl+W` / `Ctrl+Shift+W` / `Ctrl+Shift+Q` | Close pane / tab. Same cross-app convention rationale as `Ctrl+T` |
| `Ctrl+Shift+Z` | Restore last closed tab |
| `Ctrl+Shift+J` | Jump to next belling tab (across workspaces) |
| `Ctrl+Shift+D` | Split right |
| `Ctrl+Shift+E` | Split down |
| `Ctrl+Shift+0` | Equalize splits |
| `Alt+←/→/↑/↓` | Move focus between splits |
| `Ctrl+Tab` / `Ctrl+Shift+Tab` | Next / prev tab |
| `Alt+]` / `Alt+[` | Next / prev tab |
| `Ctrl+1`–`Ctrl+9` | Select tab N |
| `Alt+1`–`Alt+9` | Select workspace N |
| `Ctrl+Alt+↑/↓` | Cycle workspaces |
| `Ctrl+=` / `Ctrl+-` / `Ctrl+0` | Font zoom in / out / reset |
| `Ctrl+Shift+C` / `Ctrl+Shift+V` | Copy / paste |
| `Ctrl+Shift+F` | Search scrollback (Enter = older, Shift+Enter = newer, Esc closes) |
| Double-click tab title / sidebar name | Rename (Enter commits, Esc cancels) |
| `Ctrl+click` URL | Open in `xdg-open` |

## CLI

`lmux` doubles as a small DBus client. All subcommands dispatch to the running lmux's `org.gtk.Actions` interface:

| Subcommand | Action |
|---|---|
| `lmux notify --title T --body B [--pane-id ID]` | Fire an attention event on a pane (defaults to `$LMUX_PANE_ID`) |
| `lmux claude-session --state started\|ended [--pane-id ID]` | Mark the pane as claude-running (used by the SessionStart/SessionEnd hooks) |
| `lmux prompt-submit [--pane-id ID]` | Clear the pane's unread badge (UserPromptSubmit hook) |
| `lmux open-project [BASENAME \| --path PATH]` | Open / switch to a project workspace. No args opens the picker |
| `lmux switch-workspace [NAME]` | Switch to a workspace by name. No args opens the picker |
| `lmux command-palette` | Open the command palette |

The CLI exits 0 silently if lmux isn't running — safe to call from Claude Code hooks without breaking the session.

## Recommended WM binds (Hyprland example)

```ini
# Lmux pickers — focused or not
bindd = SUPER, P, Lmux command palette, exec, lmux command-palette
bindd = SUPER ALT, F, Lmux workspace picker, exec, lmux switch-workspace
bindd = SUPER ALT, O, Lmux project picker, exec, lmux open-project

# Cross-app tab convention — Super+T / Super+W act like browser/editor tab ops
unbind = SUPER, T
unbind = SUPER, W
bindd = SUPER, T, New tab, sendshortcut, CTRL, T, activewindow
bindd = SUPER, W, Close tab, sendshortcut, CTRL, W, activewindow
```

If you also use tmux, a small `lmux-or-tmux-popup` wrapper script that picks the lmux CLI when an lmux window is focused (and falls through to `tmux display-popup ... tmux-sessionizer` otherwise) keeps both workflows on the same keys.

## Theme honored from kitty.conf

`font_family`, `font_size`, `foreground`, `background`, `cursor`, `cursor_text_color`, `cursor_shape`, `cursor_blink_interval`, `selection_foreground`, `selection_background`, `color0`–`color15`, `enable_audio_bell`. Anything kitty-only (font ligatures, `tab_bar_style`, hyperlinks-as-buttons) is silently ignored — VTE doesn't support those.

## Not in scope (vs cmux)

cmux is the real product. lmux is a thin Linux take on the tab UI + Claude bell. It deliberately doesn't have:

- In-app browser, SSH workspaces, Claude Teams orchestration
- Notification panel, focus history with preview, persistent closed-item history
- PR status / listening-port scanner in the sidebar
- The full cmux JSON-RPC over a Unix socket — lmux's CLI is a small DBus dispatcher, not a generic API

## License

MIT
