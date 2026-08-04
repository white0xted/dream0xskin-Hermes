# Hermes Skin

CDP-based visual skin injection for **Hermes Agent Desktop**. Applies custom dark themes with background gradients and frosted-glass panels to the Hermes desktop app without modifying `app.asar`.

> **Architecture**: Forked from [dream0xskin](https://github.com/white0xted/dream0xskin) (Codex skin), adapted for Hermes Desktop's `data-slot` selector system and `--theme-*` CSS variable hierarchy.

## How It Works

```
Hermes.app (--remote-debugging-port=9334)
    ↓  CDP WebSocket
injector-hermes.py (Python + websockets)
    ↓  Runtime.evaluate
Renderer script (CSS + background overlay)
    ↓  --theme-* variable overrides + backdrop-filter
Hermes Desktop with custom skin
```

The injector connects to Hermes's CDP debugging port, discovers page targets, and injects:
1. A `<style>` element with CSS that overrides `--theme-*` variables
2. A `<div>` background overlay (gradient or image) at `z-index: 0`
3. A `MutationObserver` for SPA self-healing

## Quick Start

### 1. Launch Hermes with CDP

```bash
# Kill existing Hermes first (single-instance lock)
# Then launch with debugging port + separate user-data-dir
/Applications/Hermes.app/Contents/MacOS/Hermes \
  --remote-debugging-port=9334 \
  --user-data-dir=/tmp/hermes-skin-cdp
```

### 2. Inject skin

```bash
cd ~/Documents/"Hermes skin"
python3 runtime/injector-hermes.py \
  --port 9334 \
  --theme-dir runtime/themes-hermes/linda
```

### 3. Watch mode (continuous monitoring)

```bash
python3 runtime/injector-hermes.py \
  --port 9334 \
  --theme-dir runtime/themes-hermes/linda \
  --watch
```

### 4. Remove skin

```bash
python3 runtime/injector-hermes.py --port 9334 --remove
# or
bash scripts/stop-skin-macos.sh
```

## File Structure

```
Hermes skin/
├── runtime/
│   ├── injector-hermes.py          # Python CDP injector (main entry)
│   ├── hermes-skin.css             # Skin CSS (--theme-* overrides + panels)
│   ├── selectors-hermes.json       # DOM selector contract (24 data-slot values)
│   └── themes-hermes/
│       └── linda/
│           ├── theme.json          # Theme config (colors, art metadata)
│           └── linda.png           # Background image (optional)
├── scripts/
│   ├── stop-skin-macos.sh          # Stop injection + cleanup
│   └── doctor-macos.sh             # Diagnostics
├── launcher/                        # Swift menubar launcher (Phase 7)
├── HANDOFF.md                       # Technical documentation
└── LICENSE                          # MIT
```

## Themes

### Linda (default)

Cold blue-grey dark theme with:
- `#080b10` background (dark blue-black)
- `#7a8b94` accent (muted steel blue)
- Frosted glass sidebar (rgba 0.65 + blur 24px)
- Frosted glass composer (rgba 0.55 + blur 20px)
- Gradient fallback when no background image

To create a custom theme, add a new directory under `runtime/themes-hermes/` with a `theme.json` and optional background image.

## Hermes Desktop CSS Pipeline

Hermes maps YAML skin colors to `--theme-*` CSS variables via `ae()` + `Ne()` functions in `context-DxpVeXUj.js`:

```
~/.hermes/skins/<name>.yaml
  ↓ hermes config set display.skin <name>
  ↓ gateway resolve_skin() → WebSocket "skin.changed"
  ↓ ae(skin): CLI colors → 26-field palette
  ↓ Ne(palette): palette → --theme-* CSS variables
  ↓ documentElement.style.setProperty()
```

Our CDP skin overrides these with `!important` CSS rules. Key variables:

| CSS Variable | Effect |
|---|---|
| `--theme-background-seed` | App base color |
| `--theme-foreground` | Primary text |
| `--theme-primary` | Accent / focus |
| `--theme-sidebar-seed` | Sidebar base (we set transparent) |
| `--theme-card-seed` | Card base (we set transparent) |
| `--theme-mix-*` | Mix percentages (we set 0% for direct seed passthrough) |

## DOM Selectors

Hermes uses `data-slot` attributes (24 stable values) instead of CSS Modules or `data-testid`:

| Key selectors | Purpose |
|---|---|
| `[data-slot="sidebar-wrapper"]` | Root layout (background goes here) |
| `[data-slot="sidebar"]` | Left panel (frosted glass) |
| `[data-slot="composer-surface"]` | Input surface (frosted glass) |
| `[data-slot="composer-bounds"]` | Main content area |
| `.relative.h-\[34px\]` | Title bar (Tailwind class) |

See `runtime/selectors-hermes.json` for the full contract.

## Differences from Codex Skin (dream0xskin)

| Aspect | Codex Skin | Hermes Skin |
|---|---|---|
| Injector language | Node.js (injector.mjs) | Python (injector-hermes.py) |
| CDP port | 9333 | 9334 |
| Selector system | CSS Modules + `data-testid` | `data-slot` attributes |
| CSS variables | `--ds-*` | `--theme-*` / `--ui-*` |
| App path | `/Applications/Codex.app` or `ChatGPT.app` | `/Applications/Hermes.app` |
| Node binary | Bundled `cua_node/bin/node` | System Python + `websockets` |
| Target filter | `app://` protocol | `file:` protocol with `app.asar` |

## Requirements

- macOS with Hermes Desktop installed (`/Applications/Hermes.app`)
- Python 3.9+ with `websockets` library
  - System Python 3.9 works (has websockets)
  - Or Hermes venv: `~/.hermes/hermes-agent/venv/bin/python3`

## License

MIT — See [LICENSE](LICENSE)

## Acknowledgments

- [dream0xskin](https://github.com/white0xted/dream0xskin) — Codex skin project this is forked from
- [Hermes Agent](https://github.com/NousResearch/hermes-agent) — The desktop app being skinned
