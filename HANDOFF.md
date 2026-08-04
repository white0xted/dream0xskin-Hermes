# Hermes Skin — Technical Handoff

## Project State

**Date**: 2026-08-05
**Phase**: 1-6 complete (standalone Hermes skin). Phase 7 (unified launcher with codex skin) pending.

## Architecture

### CDP Injection Pipeline

```
User launches Hermes with --remote-debugging-port=9334
    ↓
injector-hermes.py discovers page targets via HTTP /json/list
    ↓
Connects WebSocket to each page target's webSocketDebuggerUrl
    ↓
Runtime.evaluate injects renderer script (CSS + overlay + observers)
    ↓
Renderer script:
  1. Creates <style#hermes-skin-style> with --theme-* overrides
  2. Creates <div#hermes-skin-root> at z-index:0 (background art)
  3. Elevates body to z-index:1 with transparent background
  4. MutationObserver heals DOM on SPA rebuilds
  5. Style observer defends against gateway skin.changed events
```

### Key Design Decisions

1. **Python injector (not Node.js)**: Hermes doesn't bundle Node.js like Codex does (`cua_node`). System Python 3.9+ with `websockets` library is universally available.

2. **`!important` CSS overrides**: Hermes gateway periodically re-applies `--theme-*` variables via `Ne()`. Our `!important` stylesheet rules have higher specificity than inline `setProperty()` calls without `!important`, so the skin survives gateway interference.

3. **`data-slot` selectors (not CSS Modules)**: Hermes uses 24 stable `data-slot` attribute values for semantic DOM elements. These are more stable than CSS Module class names (which change hash per build) and more reliable than `data-testid` (Hermes doesn't use testids at all).

4. **Mix percentages set to 0%**: Hermes derives `--ui-*` colors from `--theme-*` seeds via `color-mix(in srgb, seed, neutral, mix%)`. Setting `--theme-mix-*` to `0%` makes seeds pass through directly, giving us full control over the color pipeline.

5. **Transparent seeds for panels**: Setting `--theme-sidebar-seed` and `--theme-card-seed` to `transparent` (instead of a color) allows the background overlay to show through, then we apply `rgba()` + `backdrop-filter` directly on the `data-slot` elements.

## Verified Test Results (2026-08-05)

### CDP Connectivity
- ✅ Hermes launches with `--remote-debugging-port=9334 --user-data-dir=/tmp/...`
- ✅ Page target discovered: `file:///Applications/Hermes.app/Contents/Resources/app.asar/dist/index.html`
- ✅ Chrome/144.0.7559.236, Protocol 1.3

### DOM Structure
- ✅ All 24 `data-slot` values detected (47 elements with data-slot attributes)
- ✅ `<html>` has `data-hermes-theme="nous"` and `data-hermes-mode="light"`
- ✅ Title bar uses Tailwind class `.relative.h-[34px]` (no data-slot)

### Injection Results
- ✅ `hermes-skin-active` class applied to `<html>`
- ✅ `--theme-background-seed` overridden to `#080b10`
- ✅ Body background transparent (`rgba(0, 0, 0, 0)`)
- ✅ Sidebar: `rgba(17, 21, 28, 0.65)` with `backdrop-filter: blur(24px)`
- ✅ Composer: `rgba(26, 31, 40, 0.55)` with `backdrop-filter: blur(20px)`
- ✅ Vision verification: dark background gradient visible, text readable

### Known Limitations
- `backdrop-filter` blur may not render in probe instances (throwaway `--user-data-dir` lacks GPU compositing). The `rgba()` semi-transparent backgrounds provide a usable fallback. Real Hermes instances with GPU acceleration will show the full frosted-glass effect.
- The title bar selector `.relative.h-\[34px\]` is a Tailwind arbitrary value class that may change between Hermes versions. If it breaks, target the first child of `.flex.h-screen` instead.

## Hermes Desktop CSS Variable Hierarchy

### Seed Layer (`--theme-*`)
Set by `Ne()` function on `document.documentElement.style`:

| Variable | Source | Our Override |
|---|---|---|
| `--theme-background-seed` | `palette.background` | `#080b10` |
| `--theme-foreground` | `palette.foreground` | `#eaf1f5` |
| `--theme-primary` | `palette.primary` | `#7a8b94` |
| `--theme-midground` | `palette.midground` (=primary) | `#7a8b94` |
| `--theme-sidebar-seed` | `palette.sidebarBackground` | `transparent` |
| `--theme-card-seed` | `palette.card` | `transparent` |
| `--theme-elevated-seed` | `palette.popover` | `transparent` |
| `--theme-bubble-seed` | `palette.userBubble` | `rgba(17,21,28,0.6)` |

### Mix Percentages (`--theme-mix-*`)
Control how much "neutral" is mixed into seeds for `--ui-*` derivation:

| Variable | Dark (default) | Our Override |
|---|---|---|
| `--theme-mix-chrome` | 74% | 0% |
| `--theme-mix-card` | 38% | 0% |
| `--theme-mix-elevated` | 46% | 0% |
| `--theme-mix-bubble` | 46% | 0% |
| `--theme-mix-sidebar` | (varies) | 0% |

Setting these to 0% means `color-mix(seed, neutral, 0%)` = seed color directly.

### UI Layer (`--ui-*`)
Derived from seeds via CSS `color-mix()` (not JS). Automatically adapts when seeds change:
- `--ui-bg-chrome` = `color-mix(--theme-background-seed, ...)`
- `--ui-bg-editor` = `color-mix(--theme-card-seed, ...)`
- `--ui-bg-sidebar` = `color-mix(--theme-sidebar-seed, ...)`
- `--ui-accent` = `--theme-midground`
- `--ui-text-primary` = `color-mix(--ui-base, ...)`

## Theme File Format

```json
{
  "schemaVersion": 1,
  "id": "linda",
  "name": "Linda",
  "brandSubtitle": "LINDA",
  "tagline": "Cold blue-grey elegance",
  "image": "linda.png",         // optional, falls back to CSS gradient
  "appearance": "dark",
  "colors": {
    "background": "#080b10",
    "panel": "#11151c",
    "panelAlt": "#1a1f28",
    "accent": "#7a8b94",
    "accentAlt": "#5a6b74",
    "secondary": "#1a1f28",
    "highlight": "#7a8b94",
    "text": "#eaf1f5",
    "muted": "#aeb8bf",
    "line": "rgba(122,139,148,.18)"
  },
  "art": {
    "safeArea": "72%",
    "focusX": "50%",
    "focusY": "35%",
    "taskMode": "dim"
  }
}
```

## Debugging Commands

```bash
# Probe CDP targets and skin status
python3 runtime/injector-hermes.py --port 9334 --probe-only

# Run diagnostics
bash scripts/doctor-macos.sh
bash scripts/doctor-macos.sh --live

# Stop and clean up
bash scripts/stop-skin-macos.sh

# Manual CDP inspection (Python)
python3 -c "
import json, http.client, asyncio, websockets
async def check():
    conn = http.client.HTTPConnection('127.0.0.1', 9334, timeout=5)
    conn.request('GET', '/json/list')
    targets = json.loads(conn.getresponse().read())
    conn.close()
    ws_url = [t for t in targets if t['type']=='page'][0]['webSocketDebuggerUrl']
    async with websockets.connect(ws_url, max_size=50*1024*1024) as ws:
        await ws.send(json.dumps({'id':1,'method':'Runtime.evaluate','params':{
            'expression': 'JSON.stringify({hasSkin: !!window.__HERMES_SKIN_STATE__, activeClass: document.documentElement.classList.contains(\"hermes-skin-active\")})',
            'returnByValue': True
        }}))
        print(json.loads(await ws.recv()))
asyncio.run(check())
"
```

## Phase 7 Roadmap (Unified Launcher)

Phase 7 merges this project with dream0xskin into a single Swift menubar launcher that manages both Codex and Hermes skins separately (not simultaneously).

### Key Changes Needed
1. Add `SkinTarget` enum to `main.swift` (`.codex` / `.hermes`)
2. Parameterize all methods: `startInjector(for:)`, `launchAndAttach(for:)`, etc.
3. Add target switcher menu item at top of menubar
4. Isolate state: `CodexNightWorkshopSkin/` vs `HermesNightWorkshopSkin/`
5. Isolate themes: `runtime/themes/` vs `runtime/themes-hermes/`
6. Parameterize scripts: `stop-skin-macos.sh --target hermes`

### Port Assignments
- Codex: 9333 (existing, unchanged)
- Hermes: 9334 (new)

### Dependencies
Phase 7 requires all Phase 1-6 work to be verified and accepted first. The Hermes skin must be visually confirmed working on a real Hermes instance (not just a probe).

## Source References

- Hermes Desktop source: https://github.com/NousResearch/hermes-agent/tree/main/apps/desktop
- Codex skin project: https://github.com/white0xted/dream0xskin
- CDP skinning skill: `~/.hermes/skills/software-development/electron-cdp-skinning/`
- Hermes DOM inspection skill: `~/.hermes/skills/software-development/inspecting-hermes-desktop-dom/`
- Research session: @session:default/20260804_021247_0a695c
