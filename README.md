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
- **Persisted layout** — workspaces, tabs, splits, cwds, and custom titles are saved on window close to `~/.cache/lmux/state.json` and restored on next launch
- **Claude session auto-resume** — panes that had `claude` as their foreground process at save time are restored with `claude --continue --dangerously-skip-permissions` typed in automatically, so you land back in your conversation. Override the command via `LMUX_CLAUDE_RESUME_CMD=...`; set it to empty to disable
- **Claude Code bell only** — `\a` and OSC 777 notifications are gated on `/proc/<fg-pgrp>/comm` containing `claude`, so shell tab-completion bells don't trigger anything
- **Output-idle fallback** — Claude Code v2.x rarely emits a raw BEL because its notification dispatcher suppresses `push_notification` events whenever the user appears "present" (terminal focused, or last keystroke <60 s ago). So lmux additionally treats a pane as needing attention if `comm=claude` *and* the pane has been silent for `LMUX_CLAUDE_IDLE_SEC` seconds (default 5) after recent output. Set `LMUX_CLAUDE_IDLE_SEC=0` to disable.
- **Audible bell** (`message-new-instant.oga` via `canberra-gtk-play`) + **blue ● on tab and sidebar row** + **mako desktop toast** + **window border flash** (~1 s blue pulse, à la cmux) — but only when the ringing pane isn't the one you're looking at
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

For Claude Code to actually emit the terminal bell (vs the macOS-style iTerm2 OSC 9 it picks by default on many setups), add to `~/.claude/settings.json`:

```json
{
  "preferredNotifChannel": "terminal_bell"
}
```

## Keymap

| Shortcut | Action |
|---|---|
| `Ctrl+Shift+P` | Command palette (fuzzy search every action + workspaces + tabs) |
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
