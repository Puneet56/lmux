# lmux

A minimal Linux take on [cmux](https://github.com/manaflow-ai/cmux)'s tab UI — vertical workspace sidebar + horizontal terminal tabs with splits.

GTK4 + VTE + Python, single file (~700 lines). Built for running [Claude Code](https://docs.anthropic.com/en/docs/claude-code) agents in parallel without losing track of which one needs you.

![lmux screenshot](docs/screenshot.png)

## Features

- **Workspaces** in a vertical sidebar, each with its own set of **horizontal tabs**
- **Splits** within a tab — split right (`Ctrl+Shift+D`) or down (`Ctrl+Shift+E`), focus-move with `Alt`+arrows, equalize with `Ctrl+Shift+0`
- **Claude Code bell only** — `\a` and OSC 777 notifications are gated on `/proc/<fg-pgrp>/comm` containing `claude`, so shell tab-completion bells don't trigger anything
- **Audible bell** (`message-new-instant.oga` via `canberra-gtk-play`) + **blue ● on tab and sidebar row** + **mako desktop toast** — but only when the ringing pane isn't the one you're looking at
- **Sidebar metadata** — each workspace row shows its current pane's `cwd` and git branch (`.git/HEAD` walk, no subprocess)
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

# Or install system-wide for current user:
mkdir -p ~/.local/bin ~/.local/share/applications
ln -sf "$PWD/lmux.py" ~/.local/bin/lmux
cp lmux.desktop ~/.local/share/applications/
```

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
| `Ctrl+B` | Toggle sidebar |
| `Ctrl+Shift+T` | New tab (inherits cwd of focused pane) |
| `Ctrl+Shift+W` | New workspace |
| `Ctrl+Shift+Q` | Close pane / tab |
| `Ctrl+Shift+Z` | Restore last closed tab |
| `Ctrl+Shift+D` | Split right |
| `Ctrl+Shift+E` | Split down |
| `Ctrl+Shift+0` | Equalize splits |
| `Alt+←/→/↑/↓` | Move focus between splits |
| `Ctrl+Tab` / `Ctrl+Shift+Tab` | Next / prev tab |
| `Ctrl+1`–`Ctrl+9` | Select tab N |
| `Alt+1`–`Alt+9` | Select workspace N |
| `Ctrl+Alt+↑/↓` | Cycle workspaces |
| `Ctrl+=` / `Ctrl+-` / `Ctrl+0` | Font zoom in / out / reset |
| `Ctrl+Shift+C` / `Ctrl+Shift+V` | Copy / paste |
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
