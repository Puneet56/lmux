# lmux

A minimal Linux take on [cmux](https://github.com/manaflow-ai/cmux)'s tab UI — vertical workspace sidebar + horizontal terminal tabs with splits.

GTK4 + VTE + Python, single file (~700 lines). Built for running [Claude Code](https://docs.anthropic.com/en/docs/claude-code) agents in parallel without losing track of which one needs you.

![lmux screenshot](docs/screenshot.png)

## Features

- **Workspaces** in a vertical sidebar, each with its own set of **horizontal tabs**
- **Splits** within a tab — split right (`Ctrl+Shift+D`) or down (`Ctrl+Shift+E`), focus-move with `Alt`+arrows, equalize with `Ctrl+Shift+0`
- **Rename tabs and workspaces** — double-click any tab title or sidebar workspace name, Enter to commit, Esc to cancel. Custom titles override the VTE-driven one and survive restart.
- **Scrollback search** — `Ctrl+Shift+F` opens a search bar with a live hit count; Enter jumps to the previous (older) match, Shift+Enter to the next (newer), Esc closes
- **Jump to next bell** — `Ctrl+Shift+J` cycles you to the next tab waiting on you, across workspaces
- **Command palette** — `Ctrl+Shift+P` opens a fuzzy-searchable list of every action plus "Go to workspace…" / "Go to tab…" entries. Esc / click-outside closes
- **Project picker (tmux-sessionizer style)** — `Alt+Shift+O` (or `Ctrl+Shift+O`, or `lmux open-project` from any shell) opens a fuzzy picker over directories under `~/Projects` and `~/Work`. Hitting Enter creates a new workspace named after the project with three tabs — **editor** (`nvim`, focused), **claude** (`claude --dangerously-skip-permissions`), and **shell** — or switches to an already-open workspace with that name. Override the search roots via `LMUX_PROJECT_DIRS=path1:path2`
- **Workspace picker** — `Alt+Shift+F` (or `lmux switch-workspace`) opens the palette in workspace-only mode. Both binds are GTK-internal so they only intercept while lmux is the focused window — other apps' Alt+Shift handlers are untouched. Wire a WM-level bind to the CLI subcommand if you want a global trigger
- **Persisted layout** — workspaces, tabs, splits, cwds, and custom titles are saved on window close to `~/.cache/lmux/state.json` and restored on next launch
- **Claude session auto-resume** — panes that had `claude` as their foreground process at save time are restored with `claude --continue --dangerously-skip-permissions` typed in automatically, so you land back in your conversation. Override the command via `LMUX_CLAUDE_RESUME_CMD=...`; set it to empty to disable
- **Explicit-trigger notifications** (cmux-style) — no idle heuristics or terminal scraping. Two channels:
  - **VTE BEL (`\a`)** — gated on `/proc/<fg-pgrp>/comm` containing `claude`, so shell tab-completion bells stay quiet.
  - **OSC 777** — anything that prints `\033]777;notify;TITLE;BODY\033\\` to a pane's tty fires an attention event. The `lmux notify` CLI is a thin wrapper that writes exactly that, so it auto-targets the pane it ran in.
- **Audible bell** (`message-new-instant.oga` via `canberra-gtk-play`) + **counted badge on tab and sidebar row** + **mako desktop toast** + **bouncy tab flash** — toast suppressed when the ringing pane is focused-here; sound + flash + count always fire
- **Debug logging** — `LMUX_DEBUG=1 ./lmux.py 2>/tmp/lmux.log` prints breadcrumbs at every step of the bell + idle pipeline (signal received → gate check → window handler → sound spawn → toast send)
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

**No configuration required.** lmux follows [cmux](https://github.com/manaflow-ai/cmux)'s notification model: a small shell wrapper at `~/.cache/lmux/bin/claude` (auto-installed on every launch) shadows the real `claude` binary inside lmux panes, and injects Notification/Stop hooks via Claude Code's `--settings` flag. Outside lmux panes the wrapper passes through unchanged, and `--settings` merges additively with your own `~/.claude/settings.json` so nothing in your config gets clobbered.

What this looks like end-to-end:

1. lmux launches → writes `~/.cache/lmux/bin/claude` and prepends that dir to every pane shell's `PATH`. Sets `LMUX_PANE=1` so the wrapper knows it's inside lmux.
2. You type `claude` in a pane → the wrapper resolves the real claude (walks PATH, skips its own dir), then `exec`s it with `--settings '{"hooks":{"Notification":[...],"Stop":[...]}}'`.
3. Claude needs input (Notification) or finishes a turn (Stop) → fires its hook, which calls `lmux notify --title Claude --body "..."`.
4. `lmux notify` detects it's running in a Claude Code hook context (stdin is not a tty — Claude Code v2.1.139+ strips `/dev/tty` from hooks) and emits `{"terminalSequence": "\033]777;notify;...\033\\"}` to stdout. Claude relays that OSC 777 through its own pty, where VTE catches it and lmux fires the attention pipeline on that exact pane.

Run `lmux notify --title Test --body hi` from anywhere — inside a pane shell, inside a claude session, even from another terminal window. The CLI tries three paths in order:

1. `/dev/tty` — direct OSC 777 write. Works in regular interactive shells, auto-targets the calling pane.
2. **DBus** — fires the `notify` action on the running `lmux` over the session bus. Lands on the focused pane. Used when `/dev/tty` is unavailable (e.g. inside a claude subprocess).
3. `terminalSequence` JSON — last resort; only useful when relayed by a Claude Code hook handler.

Hooks installed by the wrapper pass `--from-hook` to skip straight to path 3, so the hook-relay path and the DBus path don't double-fire.

## Keymap

| Shortcut | Action |
|---|---|
| `Ctrl+Shift+P` | Command palette (fuzzy search every action + workspaces + tabs) |
| `Alt+Shift+O` / `Ctrl+Shift+O` | Open project… — fuzzy-pick a directory under `~/Projects` / `~/Work`, create-or-switch a workspace with editor + shell tabs (tmux-sessionizer equivalent) |
| `Alt+Shift+F` | Switch workspace… — fuzzy-pick an existing workspace (tmux-session-picker equivalent). Only fires while an lmux window is focused so it doesn't interfere with other apps' Alt+Shift+F handlers |
| `Ctrl+B` | Toggle sidebar |
| `Ctrl+Shift+T` | New tab (inherits cwd of focused pane) |
| `Ctrl+Shift+N` | New workspace |
| `Ctrl+Shift+W` | Close pane / tab (`Ctrl+Shift+Q` also works) |
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

## Theme honored from kitty.conf

`font_family`, `font_size`, `foreground`, `background`, `cursor`, `cursor_text_color`, `cursor_shape`, `cursor_blink_interval`, `selection_foreground`, `selection_background`, `color0`–`color15`, `enable_audio_bell`. Anything kitty-only (font ligatures, `tab_bar_style`, hyperlinks-as-buttons) is silently ignored — VTE doesn't support those.

## Not in scope (vs cmux)

cmux is the real product. lmux is a thin Linux take on the tab UI + Claude bell. It deliberately doesn't have:

- In-app browser, SSH workspaces, Claude Teams orchestration
- Notification panel, focus history with preview, persistent closed-item history
- PR status / listening-port scanner in the sidebar
- A scriptable API

## License

MIT
