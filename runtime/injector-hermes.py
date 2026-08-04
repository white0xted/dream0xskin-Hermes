#!/usr/bin/env python3
"""
Hermes Skin Injector — CDP-based skin injection for Hermes Desktop.

Connects to Hermes Desktop's CDP port (9334), discovers page targets,
and injects CSS + background art overlay via Runtime.evaluate.

Unlike the Codex skin injector (Node.js/mjs), this uses Python with
the `websockets` library (available in the Hermes venv) since Hermes
does not bundle a Node.js runtime.

Usage:
  python3 injector-hermes.py --port 9334 --theme-dir runtime/themes-hermes/linda
  python3 injector-hermes.py --port 9334 --theme-dir ... --watch
  python3 injector-hermes.py --port 9334 --remove
  python3 injector-hermes.py --port 9334 --probe-only
"""

import argparse
import asyncio
import base64
import hashlib
import http.client
import json
import os
import signal
import sys
import time
from pathlib import Path

try:
    import websockets
except ImportError:
    print("ERROR: websockets library not found. Use Hermes venv Python:", file=sys.stderr)
    print("  ~/.hermes/hermes-agent/venv/bin/python3", file=sys.stderr)
    sys.exit(1)

# ─── Constants ───────────────────────────────────────────────

SKIN_VERSION = "1.0.0"
STYLE_ID = "hermes-skin-style"
ROOT_ID = "hermes-skin-root"
ACTIVE_CLASS = "hermes-skin-active"
STATE_KEY = "__HERMES_SKIN_STATE__"
CDP_TARGET_TYPE = "page"
CDP_MAX_MESSAGE_SIZE = 50 * 1024 * 1024  # 50MB for screenshots
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "[::1]"}

# ─── Theme Loading ───────────────────────────────────────────

def load_theme(theme_dir: str) -> dict:
    """Load theme.json and background image from theme directory."""
    theme_path = Path(theme_dir) / "theme.json"
    if not theme_path.exists():
        raise FileNotFoundError(f"Theme not found: {theme_path}")

    with open(theme_path, "r", encoding="utf-8") as f:
        theme = json.load(f)

    # Load background image as data URL
    image_name = theme.get("image", "")
    if image_name:
        image_path = Path(theme_dir) / image_name
        if image_path.exists():
            with open(image_path, "rb") as f:
                image_data = f.read()
            ext = image_path.suffix.lower()
            mime = {
                ".png": "image/png",
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".webp": "image/webp",
            }.get(ext, "image/png")
            theme["_artDataUrl"] = f"data:{mime};base64,{base64.b64encode(image_data).decode()}"
        else:
            theme["_artDataUrl"] = None
    else:
        theme["_artDataUrl"] = None

    return theme


def load_css(css_path: str) -> str:
    """Load the skin CSS file."""
    with open(css_path, "r", encoding="utf-8") as f:
        return f.read()


def load_selectors(selectors_path: str) -> dict:
    """Load the selector contract."""
    with open(selectors_path, "r", encoding="utf-8") as f:
        return json.load(f)


# ─── CDP Client ──────────────────────────────────────────────

class CDPSession:
    """Chrome DevTools Protocol WebSocket session."""

    def __init__(self, ws, target_id: str, target_url: str):
        self.ws = ws
        self.target_id = target_id
        self.target_url = target_url
        self.msg_id = 0
        self.stopping = False
        self.inject_scheduled = None

    async def eval(self, expression: str, return_by_value: bool = True, await_promise: bool = True) -> dict:
        """Evaluate a JavaScript expression in the renderer context."""
        self.msg_id += 1
        msg = {
            "id": self.msg_id,
            "method": "Runtime.evaluate",
            "params": {
                "expression": expression,
                "returnByValue": return_by_value,
                "awaitPromise": await_promise,
            },
        }
        await self.ws.send(json.dumps(msg))
        # Read responses until we get our reply
        while True:
            resp = json.loads(await self.ws.recv())
            if resp.get("id") == self.msg_id:
                return resp

    async def eval_value(self, expression: str) -> any:
        """Evaluate and return the value directly."""
        resp = await self.eval(expression)
        result = resp.get("result", {}).get("result", {})
        return result.get("value")

    async def add_script_to_new_document(self, script: str) -> str:
        """Register script to run on every new document (survives SPA reloads)."""
        self.msg_id += 1
        msg = {
            "id": self.msg_id,
            "method": "Page.addScriptToEvaluateOnNewDocument",
            "params": {"source": script},
        }
        await self.ws.send(json.dumps(msg))
        while True:
            resp = json.loads(await self.ws.recv())
            if resp.get("id") == self.msg_id:
                return resp.get("result", {}).get("identifier", "")

    async def remove_script_from_new_document(self, identifier: str):
        """Remove a previously registered script."""
        self.msg_id += 1
        msg = {
            "id": self.msg_id,
            "method": "Page.removeScriptToEvaluateOnNewDocument",
            "params": {"identifier": identifier},
        }
        await self.ws.send(json.dumps(msg))
        while True:
            resp = json.loads(await self.ws.recv())
            if resp.get("id") == self.msg_id:
                return


async def discover_targets(port: int) -> list:
    """Discover CDP page targets."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", "/json/list")
    targets = json.loads(conn.getresponse().read())
    conn.close()
    # Filter for page-type targets that are Hermes renderer pages
    page_targets = []
    for t in targets:
        if t.get("type") != CDP_TARGET_TYPE:
            continue
        url = t.get("url", "")
        # Hermes page targets have file: protocol with app.asar/dist/index.html
        if url.startswith("file:") or "app.asar" in url or "hermes" in url.lower():
            page_targets.append(t)
    return page_targets


async def connect_target(target: dict) -> CDPSession:
    """Connect to a CDP target via WebSocket."""
    ws_url = target["webSocketDebuggerUrl"]
    ws = await websockets.connect(ws_url, max_size=CDP_MAX_MESSAGE_SIZE)
    return CDPSession(ws, target.get("id", ""), target.get("url", ""))


# ─── Injection Logic ─────────────────────────────────────────

def build_renderer_script(css_text: str, art_data_url, theme: dict, selectors: dict) -> str:
    """Build the renderer injection script as a single JS expression."""
    # Escape CSS for embedding in JS template literal
    css_escaped = json.dumps(css_text)
    art_escaped = json.dumps(art_data_url) if art_data_url else "null"
    theme_escaped = json.dumps(theme)
    selectors_escaped = json.dumps(selectors)

    return f"""
(() => {{
  const CSS_TEXT = {css_escaped};
  const ART_URL = {art_escaped};
  const THEME = {theme_escaped};
  const SELECTORS = {selectors_escaped};
  const STATE_KEY = "{STATE_KEY}";
  const STYLE_ID = "{STYLE_ID}";
  const ROOT_ID = "{ROOT_ID}";
  const ACTIVE_CLASS = "{ACTIVE_CLASS}";
  const VERSION = "{SKIN_VERSION}";

  // Clean up any previous injection
  const previous = window[STATE_KEY];
  if (typeof previous?.cleanup === "function") previous.cleanup();
  window[STATE_KEY] = null;

  // === 1. Create background overlay ===
  const existingRoot = document.getElementById(ROOT_ID);
  if (existingRoot) existingRoot.remove();

  const root = document.createElement('div');
  root.id = ROOT_ID;
  root.style.cssText = `
    position: fixed; inset: 0; z-index: 0;
    pointer-events: none;
    ${{ART_URL
      ? `background-image: url(${{ART_URL}}); background-size: cover; background-position: center;`
      : `background: radial-gradient(ellipse at 70% 30%, rgba(122,139,148,0.12) 0%, transparent 55%),
         linear-gradient(135deg, #080b10 0%, #11151c 45%, #1a1f28 100%);`
    }}
  `;

  // === 2. Create style element ===
  const existingStyle = document.getElementById(STYLE_ID);
  if (existingStyle) existingStyle.remove();

  const style = document.createElement('style');
  style.id = STYLE_ID;
  style.textContent = CSS_TEXT;

  // === 3. Apply ===
  function apply() {{
    if (!document.getElementById(STYLE_ID)) {{
      document.head.appendChild(style);
    }}
    if (!document.getElementById(ROOT_ID)) {{
      document.body.prepend(root);
    }}
    document.documentElement.classList.add(ACTIVE_CLASS, 'dark');
    document.documentElement.setAttribute('data-hermes-mode', 'dark');

    // Make body transparent and elevated
    document.body.style.setProperty('position', 'relative', 'important');
    document.body.style.setProperty('z-index', '1', 'important');
    document.body.style.setProperty('background', 'transparent', 'important');
  }}

  apply();

  // === 4. Self-healing (critical for SPA) ===
  function healIfNeeded() {{
    let healed = false;
    if (!document.getElementById(STYLE_ID)) {{
      document.head.appendChild(style);
      healed = true;
    }}
    if (!document.getElementById(ROOT_ID)) {{
      document.body.prepend(root);
      healed = true;
    }}
    if (!document.documentElement.classList.contains(ACTIVE_CLASS)) {{
      document.documentElement.classList.add(ACTIVE_CLASS, 'dark');
      healed = true;
    }}
    if (healed) {{
      // Re-assert body transparency
      document.body.style.setProperty('background', 'transparent', 'important');
    }}
    return healed;
  }}

  // === 5. MutationObserver for SPA DOM rebuilds ===
  const observer = new MutationObserver(() => {{
    healIfNeeded();
  }});
  observer.observe(document.body, {{ childList: true, subtree: true }});
  observer.observe(document.documentElement, {{ attributes: true, attributeFilter: ['class', 'style'] }});

  // === 6. Periodic safety scan ===
  const scanInterval = setInterval(() => {{
    healIfNeeded();
  }}, 3000);

  // === 7. Gateway interference defense ===
  // Hermes gateway periodically re-applies --theme-* variables via Ne().
  // Our !important CSS rules have higher specificity and survive.
  // But if the gateway sets inline styles on documentElement, we need to
  // re-assert our overrides.
  const styleObserver = new MutationObserver(() => {{
    // The gateway just set inline styles. Our !important CSS still wins
    // because !important in a stylesheet beats non-!important inline styles.
    // But if the gateway used setProperty with !important, we need to re-assert.
    // Check if our active class is still present
    if (!document.documentElement.classList.contains(ACTIVE_CLASS)) {{
      document.documentElement.classList.add(ACTIVE_CLASS, 'dark');
    }}
  }});
  styleObserver.observe(document.documentElement, {{ attributes: true, attributeFilter: ['style'] }});

  // === 8. Cleanup function ===
  function cleanup() {{
    observer.disconnect();
    styleObserver.disconnect();
    clearInterval(scanInterval);
    document.getElementById(ROOT_ID)?.remove();
    document.getElementById(STYLE_ID)?.remove();
    document.documentElement.classList.remove(ACTIVE_CLASS);
    // Restore body styles
    document.body.style.removeProperty('position');
    document.body.style.removeProperty('z-index');
    document.body.style.removeProperty('background');
    try {{ delete window[STATE_KEY]; }} catch(e) {{ window[STATE_KEY] = undefined; }}
  }}

  // Store state
  window[STATE_KEY] = {{
    version: VERSION,
    cleanup,
    healIfNeeded,
    apply,
  }};

  // Return status
  return JSON.stringify({{
    success: true,
    version: VERSION,
    styleInDom: !!document.getElementById(STYLE_ID),
    rootInDom: !!document.getElementById(ROOT_ID),
    activeClass: document.documentElement.classList.contains(ACTIVE_CLASS),
    htmlClass: document.documentElement.className,
    artLoaded: !!ART_URL,
  }});
}})()
"""


async def inject_into_session(session: CDPSession, css_text: str, art_url,
                               theme: dict, selectors: dict, force: bool = False) -> bool:
    """Inject skin into a single CDP session."""
    if not force:
        # Check if already injected
        status = await session.eval_value(
            f'window.{STATE_KEY} ? JSON.stringify({{'
            f'  version: window.{STATE_KEY}.version,'
            f'  styleEl: Boolean(document.getElementById("{STYLE_ID}")),'
            f'  rootEl: Boolean(document.getElementById("{ROOT_ID}")),'
            f'  activeClass: document.documentElement.classList.contains("{ACTIVE_CLASS}")'
            f'}}) : null'
        )
        if status:
            try:
                data = json.loads(status)
                if data.get("version") == SKIN_VERSION and data.get("styleEl") and data.get("rootEl"):
                    # Already injected — check if DOM elements still exist
                    if data.get("activeClass"):
                        return True  # All good
                    else:
                        # Heal
                        await session.eval_value(f'window.{STATE_KEY}?.healIfNeeded?.()')
                        return True
            except (json.JSONDecodeError, TypeError):
                pass

    script = build_renderer_script(css_text, art_url, theme, selectors)
    result = await session.eval_value(script)

    if result:
        try:
            data = json.loads(result)
            return data.get("success", False)
        except (json.JSONDecodeError, TypeError):
            pass
    return False


async def remove_from_session(session: CDPSession) -> bool:
    """Remove skin from a CDP session."""
    result = await session.eval_value(
        f'(() => {{'
        f'  const state = window.{STATE_KEY};'
        f'  if (typeof state?.cleanup === "function") {{ state.cleanup(); return "cleaned"; }}'
        f'  // Fallback: direct DOM cleanup'
        f'  document.getElementById("{ROOT_ID}")?.remove();'
        f'  document.getElementById("{STYLE_ID}")?.remove();'
        f'  document.documentElement.classList.remove("{ACTIVE_CLASS}");'
        f'  document.body.style.removeProperty("position");'
        f'  document.body.style.removeProperty("z-index");'
        f'  document.body.style.removeProperty("background");'
        f'  try {{ delete window.{STATE_KEY}; }} catch(e) {{ window.{STATE_KEY} = undefined; }}'
        f'  return "fallback-cleaned";'
        f'}})()'
    )
    return result is not None


async def probe_session(session: CDPSession) -> dict:
    """Probe a session for skin status and DOM health."""
    return await session.eval_value(
        f'(() => {{'
        f'  const slots = document.querySelectorAll("[data-slot]");'
        f'  const slotNames = [...new Set([...slots].map(e => e.getAttribute("data-slot")))];'
        f'  return JSON.stringify({{'
        f'    hasSkin: !!window.{STATE_KEY},'
        f'    skinVersion: window.{STATE_KEY}?.version || null,'
        f'    styleEl: !!document.getElementById("{STYLE_ID}"),'
        f'    rootEl: !!document.getElementById("{ROOT_ID}"),'
        f'    activeClass: document.documentElement.classList.contains("{ACTIVE_CLASS}"),'
        f'    dataSlots: slotNames,'
        f'    slotCount: slots.length,'
        f'    url: location.href,'
        f'    htmlTheme: document.documentElement.getAttribute("data-hermes-theme"),'
        f'    htmlMode: document.documentElement.getAttribute("data-hermes-mode"),'
        f'  }});'
        f'}})()'
    )


# ─── Main Loop ───────────────────────────────────────────────

async def run_injector(port: int, theme_dir: str, css_path: str, selectors_path: str,
                       watch: bool = False, remove: bool = False, probe_only: bool = False):
    """Main injector loop."""
    here = Path(__file__).parent.resolve()
    root = here.parent

    # Resolve paths
    if not theme_dir:
        theme_dir = str(root / "runtime" / "themes-hermes" / "linda")
    if not css_path:
        css_path = str(root / "runtime" / "hermes-skin.css")
    if not selectors_path:
        selectors_path = str(root / "runtime" / "selectors-hermes.json")

    # Load assets
    selectors = load_selectors(selectors_path)

    if probe_only:
        # Just probe and exit
        targets = await discover_targets(port)
        if not targets:
            print(f"[probe] No Hermes page targets found on port {port}")
            return
        for t in targets:
            session = await connect_target(t)
            result = await probe_session(session)
            print(f"[probe] Target: {t.get('url', '?')}")
            if result:
                print(json.dumps(json.loads(result), indent=2))
            await session.ws.close()
        return

    if remove:
        # Remove skin and exit
        targets = await discover_targets(port)
        if not targets:
            print(f"[remove] No Hermes page targets found on port {port}")
            return
        for t in targets:
            session = await connect_target(t)
            await remove_from_session(session)
            print(f"[remove] Cleaned: {t.get('url', '?')}")
            await session.ws.close()
        print("[remove] Skin removed from all targets.")
        return

    # Load theme and CSS for injection
    theme = load_theme(theme_dir)
    css_text = load_css(css_path)
    art_url = theme.get("_artDataUrl")

    print(f"[inject] Theme: {theme.get('name', '?')} (v{SKIN_VERSION})")
    print(f"[inject] CSS: {len(css_text)} bytes")
    print(f"[inject] Art: {'loaded' if art_url else 'gradient fallback'}")

    if not watch:
        # Single injection
        targets = await discover_targets(port)
        if not targets:
            print(f"[inject] No Hermes page targets found on port {port}")
            sys.exit(1)
        for t in targets:
            session = await connect_target(t)
            success = await inject_into_session(session, css_text, art_url, theme, selectors, force=True)
            print(f"[inject] Target {t.get('url', '?')}: {'✓ injected' if success else '✗ failed'}")
            await session.ws.close()
        print("[inject] Done.")
        return

    # Watch mode: continuous monitoring and re-injection
    print(f"[watch] Monitoring port {port}...")
    sessions = {}  # target_id -> CDPSession
    stopping = False

    def signal_handler(sig, frame):
        nonlocal stopping
        stopping = True
        print(f"\n[watch] Stopping... (signal {sig})")

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    while not stopping:
        try:
            # Discover current targets
            targets = await discover_targets(port)
            current_ids = {t.get("id") for t in targets}

            # Prune disappeared sessions
            for tid in list(sessions.keys()):
                if tid not in current_ids:
                    print(f"[watch] Target disappeared: {tid}")
                    await sessions[tid].ws.close()
                    del sessions[tid]

            # Connect new sessions
            for t in targets:
                tid = t.get("id")
                if tid not in sessions:
                    print(f"[watch] New target: {t.get('url', '?')}")
                    session = await connect_target(t)
                    sessions[tid] = session
                    # Inject immediately
                    success = await inject_into_session(session, css_text, art_url, theme, selectors, force=True)
                    print(f"[watch] Injected: {'✓' if success else '✗'}")

                    # Also register for SPA reloads via Page.addScriptToEvaluateOnNewDocument
                    # (Tier 1 persistence)
                    try:
                        script = build_renderer_script(css_text, art_url, theme, selectors)
                        identifier = await session.add_script_to_new_document(script)
                        print(f"[watch] Registered persistent script: {identifier[:20]}...")
                    except Exception as e:
                        print(f"[watch] Warning: could not register persistent script: {e}")
                else:
                    # Check existing session health
                    session = sessions[tid]
                    if session.stopping:
                        continue
                    try:
                        result = await session.eval_value(
                            f'JSON.stringify({{'
                            f'  hasSkin: !!window.{STATE_KEY},'
                            f'  styleEl: !!document.getElementById("{STYLE_ID}"),'
                            f'  rootEl: !!document.getElementById("{ROOT_ID}"),'
                            f'  activeClass: document.documentElement.classList.contains("{ACTIVE_CLASS}")'
                            f'}})'
                        )
                        if result:
                            data = json.loads(result)
                            if not data.get("activeClass") or not data.get("styleEl"):
                                # Needs re-injection
                                print(f"[watch] Re-injecting (heal needed): {tid}")
                                await inject_into_session(session, css_text, art_url, theme, selectors, force=True)
                    except Exception as e:
                        print(f"[watch] Session error for {tid}: {e}")
                        # Session likely dead, remove it
                        try:
                            await session.ws.close()
                        except:
                            pass
                        del sessions[tid]

            # Sleep before next poll
            await asyncio.sleep(1.5)

        except Exception as e:
            if not stopping:
                print(f"[watch] Error: {e}")
            await asyncio.sleep(2)

    # Cleanup
    print("[watch] Cleaning up sessions...")
    for tid, session in sessions.items():
        try:
            await remove_from_session(session)
            await session.ws.close()
        except:
            pass
    print("[watch] Done.")


# ─── CLI ─────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Hermes Desktop Skin Injector")
    parser.add_argument("--port", type=int, default=9334, help="CDP port (default: 9334)")
    parser.add_argument("--theme-dir", default="", help="Theme directory path")
    parser.add_argument("--css", default="", help="CSS file path")
    parser.add_argument("--selectors", default="", help="Selectors JSON path")
    parser.add_argument("--watch", action="store_true", help="Watch mode: continuous monitoring")
    parser.add_argument("--remove", action="store_true", help="Remove skin and exit")
    parser.add_argument("--probe-only", action="store_true", help="Probe targets and exit")
    parser.add_argument("--timeout-ms", type=int, default=5000, help="Timeout for operations")

    args = parser.parse_args()

    try:
        asyncio.run(run_injector(
            port=args.port,
            theme_dir=args.theme_dir,
            css_path=args.css,
            selectors_path=args.selectors,
            watch=args.watch,
            remove=args.remove,
            probe_only=args.probe_only,
        ))
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(0)
    except ConnectionRefusedError:
        print(f"ERROR: Cannot connect to CDP port {args.port}.", file=sys.stderr)
        print("Make sure Hermes is running with --remote-debugging-port=9334", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
