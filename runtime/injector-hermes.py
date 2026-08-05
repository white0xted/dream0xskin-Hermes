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
    """Build the renderer injection script as a single JS expression.

    Implements the codex skin architecture:
    - Art analysis engine (downsample to 96px, brightness, saliency, accent via hue binning)
    - Theme variable system (--hs-* solid + --hs-*-rgb triples on documentElement)
    - Scrim layer system (hero-scrim, task-fade, task-shade composable gradients)
    - Immersive mode for wide images (ratio >= 1.75)
    - Safe-area positioning (left/right/center/none)
    - Blob URL for art image (preferred over data URL for large images)
    - SPA self-healing (MutationObserver + periodic scan)
    - Gateway interference defense (Hermes gateway re-applies --theme-* vars)
    """
    css_escaped = json.dumps(css_text)
    art_escaped = json.dumps(art_data_url) if art_data_url else "null"
    theme_escaped = json.dumps(theme)
    selectors_escaped = json.dumps(selectors)

    return f"""(() => {{
  // ─── Clean up any previous instance ─────────────────────────
  // If a prior renderer's state still exists, call its cleanup to
  // disconnect MutationObservers and clear intervals.  Without this,
  // re-injection leaves orphaned observers that re-create the old
  // <style> element with stale CSS every time the DOM changes.
  if (window.{STATE_KEY} && typeof window.{STATE_KEY}.cleanup === "function") {{
    try {{ window.{STATE_KEY}.cleanup(); }} catch (e) {{}}
  }}
  // Belt-and-suspenders: remove any lingering DOM elements even if
  // the state object was already overwritten.
  document.getElementById("{STYLE_ID}")?.remove();
  document.getElementById("{ROOT_ID}")?.remove();

  const CSS_TEXT = {css_escaped};
  const ART_DATA_URL = {art_escaped};
  const THEME = {theme_escaped};
  const SELECTORS = {selectors_escaped};
  const STATE_KEY = "{STATE_KEY}";
  const STYLE_ID = "{STYLE_ID}";
  const ROOT_ID = "{ROOT_ID}";
  const ACTIVE_CLASS = "{ACTIVE_CLASS}";
  const VERSION = "{SKIN_VERSION}";

  // ─── Constants ─────────────────────────────────────────────
  const ROOT_ATTRS = [
    "data-hs-art-wide", "data-hs-art-safe", "data-hs-task-mode",
    "data-hs-art-safe-area", "data-hs-art-task-mode", "data-hs-art-aspect",
    "data-hs-art-ready",
  ];
  const THEME_VARIABLES = [
    "--hs-bg", "--hs-panel", "--hs-panel-2", "--hs-accent", "--hs-accent-alt",
    "--hs-secondary", "--hs-highlight", "--hs-text", "--hs-muted", "--hs-line",
    "--hs-bg-rgb", "--hs-panel-rgb", "--hs-panel-2-rgb", "--hs-accent-rgb",
    "--hs-accent-alt-rgb", "--hs-secondary-rgb", "--hs-highlight-rgb",
    "--hs-text-rgb", "--hs-muted-rgb",
    "--hs-art", "--hs-focus-x", "--hs-focus-y", "--hs-art-position",
    "--hs-hero-scrim", "--hs-task-fade", "--hs-task-shade",
    "--hs-immersive-edge", "--hs-immersive-mid", "--hs-immersive-far",
    "--hs-immersive-sidebar", "--hs-immersive-composer", "--hs-immersive-line",
  ];
  const ART = (THEME.art && typeof THEME.art === "object") ? THEME.art : {{}};
  const ART_METADATA = (THEME.artMetadata && typeof THEME.artMetadata === "object")
    ? THEME.artMetadata : null;

  // ─── Cleanup previous injection ────────────────────────────
  const previous = window[STATE_KEY];
  if (typeof previous?.cleanup === "function") previous.cleanup();
  window[STATE_KEY] = null;

  // ─── Blob URL for art image ────────────────────────────────
  let artUrl = null;
  if (ART_DATA_URL && typeof ART_DATA_URL === "string" && ART_DATA_URL.indexOf(",") > 0) {{
    try {{
      const comma = ART_DATA_URL.indexOf(",");
      const mime = (/^data:([^;,]+)/.exec(ART_DATA_URL) || [])[1] || "image/png";
      const binary = atob(ART_DATA_URL.slice(comma + 1));
      const bytes = new Uint8Array(binary.length);
      for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
      artUrl = URL.createObjectURL(new Blob([bytes], {{ type: mime }}));
    }} catch (e) {{
      artUrl = null;
    }}
  }}

  let artAnalysis = null;
  let analysisTimer = null;

  // ─── Color utilities ───────────────────────────────────────
  const clamp = (v, min, max) => Math.min(max, Math.max(min, v));

  const parseRgb = (value) => {{
    if (!value || value === "transparent") return null;
    const hex = String(value).trim().match(/^#([0-9a-f]{{3,4}}|[0-9a-f]{{6}}|[0-9a-f]{{8}})$/i);
    if (hex) {{
      const rgbHex = hex[1].length <= 4
        ? hex[1].slice(0, 3).split("").map((d) => d + d).join("")
        : hex[1].slice(0, 6);
      const n = Number.parseInt(rgbHex, 16);
      return {{ r: n >> 16, g: (n >> 8) & 255, b: n & 255 }};
    }}
    const m = String(value).match(/rgba?\\(\\s*([\\d.]+)\\s*,\\s*([\\d.]+)\\s*,\\s*([\\d.]+)/i);
    if (!m) return null;
    return {{ r: Number(m[1]), g: Number(m[2]), b: Number(m[3]) }};
  }};

  const rgbString = (value) => {{
    const rgb = parseRgb(value);
    return rgb ? [rgb.r, rgb.g, rgb.b]
      .map((c) => Math.round(clamp(c, 0, 255)))
      .join(" ") : null;
  }};

  const rgbToHex = ({{ r, g, b }}) => "#" + [r, g, b]
    .map((v) => clamp(Math.round(v), 0, 255).toString(16).padStart(2, "0"))
    .join("");

  const rgbToHsl = ({{ r, g, b }}) => {{
    const vals = [r, g, b].map((v) => v / 255);
    const max = Math.max(...vals), min = Math.min(...vals);
    const l = (max + min) / 2;
    if (max === min) return {{ h: 0, s: 0, l }};
    const d = max - min;
    const s = l > 0.5 ? d / (2 - max - min) : d / (max + min);
    let h;
    if (max === vals[0]) h = (vals[1] - vals[2]) / d + (vals[1] < vals[2] ? 6 : 0);
    else if (max === vals[1]) h = (vals[2] - vals[0]) / d + 2;
    else h = (vals[0] - vals[1]) / d + 4;
    return {{ h: h * 60, s, l }};
  }};

  const hslToRgb = ({{ h, s, l }}) => {{
    const hue = (((h % 360) + 360) % 360) / 360;
    if (s === 0) {{
      const n = Math.round(l * 255);
      return {{ r: n, g: n, b: n }};
    }}
    const q = l < 0.5 ? l * (1 + s) : l + s - l * s;
    const p = 2 * l - q;
    const ch = (off) => {{
      let t = hue + off;
      if (t < 0) t += 1;
      if (t > 1) t -= 1;
      if (t < 1/6) return p + (q - p) * 6 * t;
      if (t < 1/2) return q;
      if (t < 2/3) return p + (q - p) * (2/3 - t) * 6;
      return p;
    }};
    return {{ r: ch(1/3) * 255, g: ch(0) * 255, b: ch(-1/3) * 255 }};
  }};

  // ─── Adaptive palette (from art accent or fallback) ────────
  const makeAdaptivePalette = (sample) => {{
    const src = sample || {{ r: 108, g: 126, b: 136 }};
    const hsl = rgbToHsl(src);
    const hue = hsl.s < 0.12 ? 214 : hsl.h;
    const sat = clamp(hsl.s, 0.38, 0.72);
    const accent = hslToRgb({{ h: hue, s: sat, l: 0.66 }});
    const accentAlt = hslToRgb({{ h: hue + 12, s: sat * 0.82, l: 0.73 }});
    const secondary = hslToRgb({{ h: hue - 24, s: sat * 0.64, l: 0.62 }});
    const highlight = hslToRgb({{ h: hue + 24, s: sat * 0.76, l: 0.58 }});
    const neutral = (l, c = 0.08) => rgbToHex(hslToRgb({{ h: hue, s: c, l }}));
    return {{
      background: neutral(0.055, 0.045),
      panel: neutral(0.085, 0.04),
      panelAlt: neutral(0.125, 0.05),
      accent: rgbToHex(accent),
      accentAlt: rgbToHex(accentAlt),
      secondary: rgbToHex(secondary),
      highlight: rgbToHex(highlight),
      text: neutral(0.93, 0.025),
      muted: neutral(0.69, 0.03),
      line: "rgba(" + Math.round(accent.r) + ", " + Math.round(accent.g) + ", " + Math.round(accent.b) + ", .28)",
    }};
  }};

  // ─── Style element ─────────────────────────────────────────
  const existingStyle = document.getElementById(STYLE_ID);
  if (existingStyle) existingStyle.remove();
  const style = document.createElement("style");
  style.id = STYLE_ID;
  style.textContent = CSS_TEXT;

  const existingRoot = document.getElementById(ROOT_ID);
  if (existingRoot) existingRoot.remove();

  // Root div: transparent scrim container (keeps health-check working)
  const root = document.createElement("div");
  root.id = ROOT_ID;
  root.style.cssText = "position:fixed;inset:0;z-index:0;pointer-events:none;";

  // ─── Helpers ───────────────────────────────────────────────
  const setStyleProperty = (el, name, value) => {{
    if (el.style.getPropertyValue(name) !== value) {{
      el.style.setProperty(name, value);
    }}
  }};
  const setAttribute = (el, name, value) => {{
    const n = String(value);
    if (el.getAttribute(name) !== n) el.setAttribute(name, n);
  }};

  // ─── Apply theme variables to documentElement ──────────────
  const applyTheme = (el) => {{
    const declared = (THEME.colors && typeof THEME.colors === "object") ? THEME.colors : {{}};
    const adaptive = makeAdaptivePalette(artAnalysis?.accentRgb);
    const pick = (name, adaptiveKey) => {{
      if (typeof declared[name] === "string" && declared[name]) return declared[name];
      return adaptive[adaptiveKey || name];
    }};

    const vars = {{
      "--hs-bg": pick("background"),
      "--hs-panel": pick("panel"),
      "--hs-panel-2": pick("panelAlt"),
      "--hs-accent": pick("accent"),
      "--hs-accent-alt": pick("accentAlt"),
      "--hs-secondary": pick("secondary"),
      "--hs-highlight": pick("highlight"),
      "--hs-text": pick("text"),
      "--hs-muted": pick("muted"),
      "--hs-line": (typeof declared.line === "string" && declared.line) ? declared.line : adaptive.line,
    }};
    for (const [k, v] of Object.entries(vars)) {{
      if (typeof v === "string" && v) setStyleProperty(el, k, v);
    }}

    // RGB triples for rgb(var() / alpha) composition
    const rgbVars = {{
      "--hs-bg-rgb": vars["--hs-bg"],
      "--hs-panel-rgb": vars["--hs-panel"],
      "--hs-panel-2-rgb": vars["--hs-panel-2"],
      "--hs-accent-rgb": vars["--hs-accent"],
      "--hs-accent-alt-rgb": vars["--hs-accent-alt"],
      "--hs-secondary-rgb": vars["--hs-secondary"],
      "--hs-highlight-rgb": vars["--hs-highlight"],
      "--hs-text-rgb": vars["--hs-text"],
      "--hs-muted-rgb": vars["--hs-muted"],
    }};
    for (const [k, v] of Object.entries(rgbVars)) {{
      const rgb = rgbString(v);
      if (rgb) setStyleProperty(el, k, rgb);
    }}

    // Art URL as CSS variable
    if (artUrl) setStyleProperty(el, "--hs-art", 'url("' + artUrl + '")');

    // Scrim gradients (composable readability layers)
    setStyleProperty(el, "--hs-hero-scrim",
      "linear-gradient(90deg, rgb(var(--hs-bg-rgb) / .90) 0%, rgb(var(--hs-bg-rgb) / .76) 50%, rgb(var(--hs-bg-rgb) / .18) 84%, transparent 100%)");
    setStyleProperty(el, "--hs-task-fade",
      "linear-gradient(180deg, rgb(var(--hs-bg-rgb) / .10) 0%, rgb(var(--hs-bg-rgb) / .18) 32%, rgb(var(--hs-bg-rgb) / .76) 68%, rgb(var(--hs-bg-rgb) / 1) 100%)");
    setStyleProperty(el, "--hs-task-shade",
      "linear-gradient(90deg, rgb(var(--hs-bg-rgb) / .56) 0%, rgb(var(--hs-bg-rgb) / .36) 48%, rgb(var(--hs-bg-rgb) / .12) 100%)");

    // Immersive mode panel opacity (thin glass, codex aesthetics;
    // extra-thin now — reading cards carry content legibility)
    setStyleProperty(el, "--hs-immersive-edge", "rgb(var(--hs-bg-rgb) / .20)");
    setStyleProperty(el, "--hs-immersive-mid", "rgb(var(--hs-bg-rgb) / .12)");
    setStyleProperty(el, "--hs-immersive-far", "rgb(var(--hs-bg-rgb) / .06)");
    setStyleProperty(el, "--hs-immersive-sidebar", "rgb(var(--hs-panel-rgb) / .22)");
    setStyleProperty(el, "--hs-immersive-composer", "rgb(var(--hs-panel-2-rgb) / .32)");
    setStyleProperty(el, "--hs-immersive-line", "rgb(var(--hs-muted-rgb) / .34)");
  }};

  // ─── Apply art metadata (focus, safe-area, immersive) ──────
  const applyArtMetadata = (el) => {{
    const profile = artAnalysis || ART_METADATA;
    const inferredSafe = profile?.safeArea || "center";
    const safeArea = (ART.safeArea && ART.safeArea !== "auto") ? ART.safeArea : inferredSafe;
    const canonicalSafe = ["left", "right", "center", "none"].includes(safeArea) ? safeArea : "center";
    const focusX = (typeof ART.focusX === "number") ? ART.focusX
      : (profile?.focusX ?? (safeArea === "left" ? 0.72 : safeArea === "right" ? 0.28 : 0.5));
    const focusY = (typeof ART.focusY === "number") ? ART.focusY : (profile?.focusY ?? 0.5);
    const taskMode = (ART.taskMode && ART.taskMode !== "auto") ? ART.taskMode : (profile?.taskMode || "ambient");
    const wide = true;  // Always use body background for art (unified mode)
    const aspect = profile?.aspect || "unknown";
    const fx = (clamp(focusX, 0, 1) * 100).toFixed(2) + "%";
    const fy = (clamp(focusY, 0, 1) * 100).toFixed(2) + "%";

    setAttribute(el, "data-hs-art-wide", wide ? "true" : "false");
    setAttribute(el, "data-hs-art-safe", canonicalSafe);
    setAttribute(el, "data-hs-task-mode", taskMode);
    setAttribute(el, "data-hs-art-safe-area", safeArea);
    setAttribute(el, "data-hs-art-task-mode", taskMode);
    setAttribute(el, "data-hs-art-aspect", aspect);
    setAttribute(el, "data-hs-art-ready", artAnalysis ? "true" : "false");

    setStyleProperty(el, "--hs-focus-x", fx);
    setStyleProperty(el, "--hs-focus-y", fy);
    setStyleProperty(el, "--hs-art-position", fx + " " + fy);
  }};

  // ─── Art analysis engine ───────────────────────────────────
  const analyzeArt = () => new Promise((resolve) => {{
    if (!artUrl || typeof window.Image !== "function") {{ resolve(null); return; }}
    const image = new window.Image();
    let settled = false;
    const finish = (v) => {{
      if (settled) return;
      settled = true;
      if (analysisTimer) {{ clearTimeout(analysisTimer); analysisTimer = null; }}
      resolve(v);
    }};
    analysisTimer = setTimeout(() => finish(null), 6000);
    image.onerror = () => finish(null);
    image.onload = () => {{
      try {{
        const ratio = image.naturalWidth / image.naturalHeight;
        if (!Number.isFinite(ratio) || ratio <= 0) throw new Error("bad dims");
        const maxDim = 96;
        const w = Math.max(16, Math.round(ratio >= 1 ? maxDim : maxDim * ratio));
        const h = Math.max(16, Math.round(ratio >= 1 ? maxDim / ratio : maxDim));
        const canvas = document.createElement("canvas");
        canvas.width = w; canvas.height = h;
        const ctx = canvas.getContext("2d", {{ willReadFrequently: true }});
        if (!ctx) throw new Error("no canvas");
        ctx.drawImage(image, 0, 0, w, h);
        const data = ctx.getImageData(0, 0, w, h).data;
        const samples = new Array(w * h);
        const bins = Array.from({{ length: 24 }}, () => ({{ weight: 0, r: 0, g: 0, b: 0 }}));
        let lightTotal = 0, count = 0;

        for (let y = 0; y < h; y++) {{
          for (let x = 0; x < w; x++) {{
            const o = (y * w + x) * 4;
            if (data[o + 3] < 32) continue;
            const rgb = {{ r: data[o], g: data[o + 1], b: data[o + 2] }};
            const light = (0.2126 * rgb.r + 0.7152 * rgb.g + 0.0722 * rgb.b) / 255;
            const hsl = rgbToHsl(rgb);
            samples[y * w + x] = {{ light, saturation: hsl.s }};
            lightTotal += light;
            count++;
            if (hsl.s >= 0.16 && hsl.l >= 0.16 && hsl.l <= 0.86) {{
              const bin = bins[Math.min(23, Math.floor(hsl.h / 15))];
              const wt = hsl.s * (1 - Math.abs(hsl.l - 0.52) * 0.85);
              bin.weight += wt;
              bin.r += rgb.r * wt;
              bin.g += rgb.g * wt;
              bin.b += rgb.b * wt;
            }}
          }}
        }}
        if (!count) throw new Error("no visible pixels");
        const brightness = lightTotal / count;

        // Information density for safe-area detection
        const information = (start, end) => {{
          let total = 0, totalSq = 0, edges = 0, edgeCount = 0, pixels = 0;
          for (let y = 0; y < h; y++) {{
            for (let x = start; x < end; x++) {{
              const s = samples[y * w + x];
              if (!s) continue;
              total += s.light;
              totalSq += s.light * s.light;
              pixels++;
              const prev = x > start ? samples[y * w + x - 1] : null;
              const above = y > 0 ? samples[(y - 1) * w + x] : null;
              if (prev) {{ edges += Math.abs(s.light - prev.light); edgeCount++; }}
              if (above) {{ edges += Math.abs(s.light - above.light); edgeCount++; }}
            }}
          }}
          const mean = pixels ? total / pixels : 0;
          const variance = pixels ? Math.max(0, totalSq / pixels - mean * mean) : 1;
          return Math.sqrt(variance) * 0.58 + (edgeCount ? edges / edgeCount : 1) * 0.42;
        }};
        const zoneW = Math.max(1, Math.floor(w * 0.38));
        const leftInfo = information(0, zoneW);
        const rightInfo = information(w - zoneW, w);
        let safeArea = "center";
        if (leftInfo < rightInfo * 0.86) safeArea = "left";
        else if (rightInfo < leftInfo * 0.86) safeArea = "right";

        // Saliency map for focus point
        let salTotal = 0, salX = 0, salY = 0;
        for (let y = 0; y < h; y++) {{
          for (let x = 0; x < w; x++) {{
            const s = samples[y * w + x];
            if (!s) continue;
            const prev = x > 0 ? samples[y * w + x - 1] : null;
            const above = y > 0 ? samples[(y - 1) * w + x] : null;
            const edge = (prev ? Math.abs(s.light - prev.light) : 0) +
              (above ? Math.abs(s.light - above.light) : 0);
            const wt = 0.01 + Math.abs(s.light - brightness) * 0.48 +
              s.saturation * 0.34 + edge * 0.28;
            salTotal += wt;
            salX += (x + 0.5) / w * wt;
            salY += (y + 0.5) / h * wt;
          }}
        }}
        let focusX = salTotal ? salX / salTotal : 0.5;
        let focusY = salTotal ? salY / salTotal : 0.5;
        if (safeArea === "left") focusX = Math.max(0.64, focusX);
        if (safeArea === "right") focusX = Math.min(0.36, focusX);
        focusX = clamp(focusX, 0.12, 0.88);
        focusY = clamp(focusY, 0.18, 0.82);

        // Accent color via hue binning
        const accentBin = bins.reduce((best, c) => c.weight > best.weight ? c : best, bins[0]);
        const accentRgb = accentBin.weight > 0 ? {{
          r: accentBin.r / accentBin.weight,
          g: accentBin.g / accentBin.weight,
          b: accentBin.b / accentBin.weight,
        }} : null;

        const aspect = ratio >= 2.25 ? "ultrawide" : ratio >= 1.45 ? "wide"
          : ratio >= 1.08 ? "landscape" : ratio >= 0.9 ? "square" : "portrait";

        finish({{
          width: image.naturalWidth,
          height: image.naturalHeight,
          ratio,
          wide: ratio >= 1.75,
          aspect,
          brightness,
          safeArea,
          focusX,
          focusY,
          taskMode: ratio >= 2.25 ? "banner" : "ambient",
          accentRgb,
        }});
      }} catch (e) {{
        finish(null);
      }}
    }};
    image.src = artUrl;
  }});

  // ─── Apply root state ──────────────────────────────────────
  const applyRootState = (el) => {{
    if (!document.getElementById(STYLE_ID)) {{
      (document.head || document.documentElement).appendChild(style);
    }}
    if (!document.getElementById(ROOT_ID)) {{
      (document.body || document.documentElement).prepend(root);
    }}
    el.classList.add(ACTIVE_CLASS, "dark");
    el.setAttribute("data-hermes-mode", "dark");
    applyTheme(el);
    applyArtMetadata(el);
    // Body transparent + elevated (use background-color, NOT background shorthand,
    // so that CSS background-image rules for immersive mode still apply)
    document.body.style.setProperty("position", "relative", "important");
    document.body.style.setProperty("z-index", "1", "important");
    document.body.style.setProperty("background-color", "transparent", "important");
  }};

  // ─── Apply (initial) ───────────────────────────────────────
  function apply() {{
    applyRootState(document.documentElement);
  }}
  apply();

  // ─── Self-healing ──────────────────────────────────────────
  function healIfNeeded() {{
    let healed = false;
    const el = document.documentElement;
    if (!document.getElementById(STYLE_ID)) {{
      (document.head || document.documentElement).appendChild(style);
      healed = true;
    }}
    if (!document.getElementById(ROOT_ID)) {{
      (document.body || document.documentElement).prepend(root);
      healed = true;
    }}
    if (!el.classList.contains(ACTIVE_CLASS)) {{
      el.classList.add(ACTIVE_CLASS, "dark");
      el.setAttribute("data-hermes-mode", "dark");
      healed = true;
    }}
    if (healed) {{
      applyTheme(el);
      applyArtMetadata(el);
      document.body.style.setProperty("background-color", "transparent", "important");
    }}
    injectBranding();
    return healed;
  }}

  // ─── Dream0xSkin branding badge ────────────────────────────
  // Inject a real DOM element above the HERMES AGENT title on the
  // welcome screen.  CSS ::before is unreliable on dynamically
  // inserted DOM in some Electron builds, so we use a JS-injected
  // <span> with an MutationObserver to catch the intro appearing.
  // Nothing here is hardcoded to a specific theme:
  //   - label: THEME.brandSubtitle (the launcher writes the theme
  //     name in uppercase there when a theme is created), falling
  //     back to THEME.name
  //   - color: var(--hs-accent-rgb) — the active theme's accent
  //     triple set by applyTheme() (theme.json colors, or the
  //     art-derived adaptive palette when the theme has no accent)
  const BRAND_ID = "hs-brand-badge";
  const BRAND_TEXT = (THEME.brandSubtitle || THEME.name || "Dream0xSkin") + " \\u00B7 Powered by Dream0xSkin";

  function injectBranding() {{
    const intro = document.querySelector('[data-slot="aui_intro"]');
    if (!intro) return;
    const innerDiv = intro.querySelector(":scope > div");
    if (!innerDiv) return;
    if (innerDiv.querySelector("#" + BRAND_ID)) return;  // already there

    const badge = document.createElement("span");
    badge.id = BRAND_ID;
    badge.textContent = BRAND_TEXT;
    // Entrance animation lives in hermes-skin.css (#hs-brand-badge) so it
    // can be themed/reduced-motion aware; the keyframes are the only place
    // that ever sets opacity 0, so a broken animation never hides the badge.
    badge.setAttribute("style",
      "display:block;width:100%;text-align:center;" +
      "font-family:-apple-system,BlinkMacSystemFont,'SF Pro Text','PingFang SC',system-ui,sans-serif;" +
      "font-size:13px;font-weight:500;letter-spacing:0.12em;text-transform:uppercase;" +
      "color:rgb(var(--hs-accent-rgb) / .72);" +
      "margin-bottom:0.75rem;" +
      "text-shadow:0 1px 3px rgb(var(--hs-bg-rgb) / .88),0 0 12px rgb(var(--hs-bg-rgb) / .50);" +
      "pointer-events:none;"
    );
    innerDiv.insertBefore(badge, innerDiv.firstChild);
  }}

  // ─── MutationObserver for SPA DOM rebuilds ─────────────────
  const observer = new MutationObserver(() => {{ healIfNeeded(); }});
  observer.observe(document.body || document.documentElement, {{ childList: true, subtree: true }});
  observer.observe(document.documentElement, {{ attributes: true, attributeFilter: ["class", "style"] }});

  // ─── Dedicated branding observer ───────────────────────────
  // The main MutationObserver calls healIfNeeded which calls
  // injectBranding, but React may render the intro's children
  // asynchronously.  This dedicated observer watches specifically
  // for aui_intro appearing and retries badge injection on every
  // mutation within it until the badge sticks.
  const brandObserver = new MutationObserver(() => {{ injectBranding(); }});
  brandObserver.observe(document.body || document.documentElement, {{
    childList: true, subtree: true,
  }});

  // ─── Periodic safety scan ──────────────────────────────────
  const scanInterval = setInterval(() => {{ healIfNeeded(); }}, 1500);

  // ─── Gateway interference defense ──────────────────────────
  // Hermes gateway periodically re-applies --theme-* variables.
  // Our !important CSS rules win, but if the gateway sets inline
  // styles on documentElement with !important, re-assert our class.
  const styleObserver = new MutationObserver(() => {{
    if (!document.documentElement.classList.contains(ACTIVE_CLASS)) {{
      document.documentElement.classList.add(ACTIVE_CLASS, "dark");
    }}
  }});
  styleObserver.observe(document.documentElement, {{ attributes: true, attributeFilter: ["style"] }});

  // ─── Async art analysis ────────────────────────────────────
  if (artUrl) {{
    analyzeArt().then((analysis) => {{
      if (!analysis) return;
      if (!window[STATE_KEY] || window[STATE_KEY].version !== VERSION) return;
      artAnalysis = analysis;
      applyRootState(document.documentElement);
    }}).catch(() => {{}});
  }}

  // ─── Cleanup ───────────────────────────────────────────────
  function cleanup() {{
    observer.disconnect();
    styleObserver.disconnect();
    brandObserver.disconnect();
    clearInterval(scanInterval);
    if (analysisTimer) {{ clearTimeout(analysisTimer); analysisTimer = null; }}
    document.getElementById(ROOT_ID)?.remove();
    document.getElementById(STYLE_ID)?.remove();
    document.getElementById(BRAND_ID)?.remove();
    const el = document.documentElement;
    el.classList.remove(ACTIVE_CLASS);
    for (const name of ROOT_ATTRS) el.removeAttribute(name);
    for (const name of THEME_VARIABLES) el.style.removeProperty(name);
    // Clean any remaining --hs-* properties
    for (const prop of [...(el.style || [])]) {{
      if (prop.startsWith("--hs-")) el.style.removeProperty(prop);
    }}
    // Restore body styles
    document.body.style.removeProperty("position");
    document.body.style.removeProperty("z-index");
    document.body.style.removeProperty("background-color");
    // Revoke blob URL
    if (artUrl) {{ try {{ URL.revokeObjectURL(artUrl); }} catch (e) {{}} artUrl = null; }}
    try {{ delete window[STATE_KEY]; }} catch (e) {{ window[STATE_KEY] = undefined; }}
  }}

  // ─── Store state ───────────────────────────────────────────
  window[STATE_KEY] = {{
    version: VERSION,
    cleanup,
    healIfNeeded,
    injectBranding,
    apply,
    artUrl,
    analysis: artAnalysis,
  }};

  // ─── Return status ─────────────────────────────────────────
  return JSON.stringify({{
    success: true,
    version: VERSION,
    styleInDom: !!document.getElementById(STYLE_ID),
    rootInDom: !!document.getElementById(ROOT_ID),
    activeClass: document.documentElement.classList.contains(ACTIVE_CLASS),
    htmlClass: document.documentElement.className,
    artLoaded: !!artUrl,
    artAnalyzed: !!artAnalysis,
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
        f'  document.body.style.removeProperty("background-color");'
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
