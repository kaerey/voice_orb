#!/usr/bin/env python3
"""
voice_orb_bridge.py
───────────────────
Bridges Home Assistant voice satellite state to the WebSocket served
to the Pepper's Ghost orb display.

Architecture:
  HA WebSocket ──► this bridge ──► WebSocket :8765 ──► orb_display.html
                                        │
                                  pyaudio RMS ──► audio_level (particle reactivity)

State source:
  Subscribes to HA's state_changed events for the assist_satellite entity.
  HA tracks the full pipeline lifecycle (idle → listening → processing → responding).

Usage:
  python3 voice_orb_bridge.py

Requirements:
  pip install websockets pyaudio numpy
"""

import asyncio
import json
import logging
import threading
from pathlib import Path

# ── Optional: audio level analysis ──────────────────────────────────────────
try:
    import numpy as np
    import pyaudio
    AUDIO_AVAILABLE = True
except ImportError:
    AUDIO_AVAILABLE = False
    print("⚠  pyaudio/numpy not found — audio-reactive mode disabled.")
    print("   Install with: pip install pyaudio numpy")

# ── WebSocket ────────────────────────────────────────────────────────────────
try:
    import websockets
    WS_AVAILABLE = True
except ImportError:
    WS_AVAILABLE = False
    print("⚠  websockets not found — install with: pip install websockets")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger("voice_orb")

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG — defaults, overridden by bridge_config.json if present.
# Run setup.py to generate bridge_config.json interactively.
# ─────────────────────────────────────────────────────────────────────────────

CONFIG = {
    # WebSocket server (orb display connects here)
    "ws_host": "0.0.0.0",
    "ws_port": 8765,

    # Audio input device index (-1 = auto-detect).
    # Run setup.py or pass --list-devices to find your mic index.
    "audio_device_index": -1,
    "audio_chunk": 512,
    "audio_rate": 48000,
    "audio_channels": 1,

    # How often to push audio level to the display (seconds)
    "audio_push_interval": 0.05,

    # ── Home Assistant ──────────────────────────────────────────────────────
    # These are set by setup.py → bridge_config.json (gitignored).
    "ha_host": "",
    "ha_port": 8123,
    "ha_token": "",
    "ha_satellite_entity": "",
}

# Load local overrides from bridge_config.json (contains secrets, not in git)
_config_path = Path(__file__).parent / "bridge_config.json"
if _config_path.exists():
    with open(_config_path, "r") as _f:
        CONFIG.update(json.load(_f))

# ─────────────────────────────────────────────────────────────────────────────
# SHARED STATE
# ─────────────────────────────────────────────────────────────────────────────

class OrbState:
    VALID_STATES = {"idle", "wake", "listening", "thinking", "speaking", "error"}

    def __init__(self):
        self._state       = "idle"
        self._audio_level = 0.0
        self._lock        = threading.Lock()
        self._listeners: list = []

    @property
    def state(self):
        return self._state

    def set_state(self, new_state: str):
        if new_state not in self.VALID_STATES:
            log.warning(f"Unknown state: {new_state!r}")
            return
        with self._lock:
            if self._state == new_state:
                return
            self._state = new_state
        icon = {
            "idle": "💤", "wake": "✨", "listening": "👂",
            "thinking": "🤔", "speaking": "🔊", "error": "❌"
        }.get(new_state, "•")
        log.info(f"{icon}  State → {new_state}")
        self._broadcast({"state": new_state})

    def set_audio_level(self, level: float):
        self._audio_level = max(0.0, min(1.0, level))

    def get_audio_level(self):
        return self._audio_level

    def add_listener(self, q: asyncio.Queue):
        self._listeners.append(q)

    def remove_listener(self, q: asyncio.Queue):
        try:
            self._listeners.remove(q)
        except ValueError:
            pass

    def _broadcast(self, msg: dict):
        data = json.dumps(msg)
        for q in list(self._listeners):
            try:
                q.put_nowait(data)
            except asyncio.QueueFull:
                pass


orb = OrbState()

# ─────────────────────────────────────────────────────────────────────────────
# CLI: list audio devices
# ─────────────────────────────────────────────────────────────────────────────

def list_audio_devices():
    if not AUDIO_AVAILABLE:
        print("pyaudio not installed.")
        return
    pa = pyaudio.PyAudio()
    print("\nAvailable audio input devices:")
    print("─" * 50)
    for i in range(pa.get_device_count()):
        info = pa.get_device_info_by_index(i)
        if info["maxInputChannels"] > 0:
            print(f"  [{i}] {info['name']}  ({int(info['defaultSampleRate'])}Hz)")
    print()
    pa.terminate()

# ─────────────────────────────────────────────────────────────────────────────
# AUDIO CAPTURE THREAD
# ─────────────────────────────────────────────────────────────────────────────

def audio_capture_thread():
    if not AUDIO_AVAILABLE:
        return

    pa = pyaudio.PyAudio()
    dev_idx = CONFIG["audio_device_index"]
    if dev_idx < 0:
        dev_idx = pa.get_default_input_device_info()["index"]

    info = pa.get_device_info_by_index(dev_idx)
    log.info(f"🎙  Audio capture: [{dev_idx}] {info['name']}")

    stream = pa.open(
        format=pyaudio.paInt16,
        channels=CONFIG["audio_channels"],
        rate=CONFIG["audio_rate"],
        input=True,
        input_device_index=dev_idx,
        frames_per_buffer=CONFIG["audio_chunk"],
    )

    try:
        while True:
            data = stream.read(CONFIG["audio_chunk"], exception_on_overflow=False)
            samples = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
            rms = float(np.sqrt(np.mean(samples ** 2)))
            orb.set_audio_level(min(1.0, rms * 8.0))
    except Exception as e:
        log.error(f"Audio thread error: {e}")
    finally:
        stream.stop_stream()
        stream.close()
        pa.terminate()

# ─────────────────────────────────────────────────────────────────────────────
# HOME ASSISTANT WEBSOCKET LISTENER
#
# Subscribes to state_changed events for the assist_satellite entity.
# HA tracks the full voice pipeline lifecycle.
#
# HA satellite states → orb states:
#   idle       → idle
#   listening  → wake (brief flash) then listening
#   processing → thinking
#   responding → speaking
# ─────────────────────────────────────────────────────────────────────────────

HA_STATE_MAP = {
    "idle":       "idle",
    "listening":  "listening",
    "processing": "thinking",
    "responding": "speaking",
}

# How long (seconds) to show the "wake" flash before switching to "listening"
WAKE_FLASH_DURATION = 0.35


async def _discover_satellite_entity(ws, msg_id: int) -> str:
    """Query HA for all states and return the first assist_satellite entity ID."""
    await ws.send(json.dumps({"id": msg_id, "type": "get_states"}))
    while True:
        raw = await ws.recv()
        msg = json.loads(raw)
        if msg.get("id") == msg_id:
            break

    if not (msg.get("type") == "result" and msg.get("success")):
        log.warning("get_states failed — cannot auto-discover satellite entity")
        return ""

    for state in msg.get("result", []):
        eid = state.get("entity_id", "")
        if eid.startswith("assist_satellite."):
            return eid

    log.warning("No assist_satellite.* entity found in HA")
    return ""


async def ha_ws_listener():
    """Connect to HA WebSocket, authenticate, and subscribe to satellite state changes."""
    ha_url  = f"ws://{CONFIG['ha_host']}:{CONFIG['ha_port']}/api/websocket"
    token   = CONFIG.get("ha_token", "")
    entity_id = CONFIG.get("ha_satellite_entity", "")
    delay   = 5
    msg_id  = 1

    if not token:
        log.error("ha_token is not set in CONFIG — cannot connect to Home Assistant.")
        log.error("Create a Long-Lived Access Token in HA: Settings → Profile → Long-Lived Access Tokens")
        return

    while True:
        try:
            log.info(f"Connecting to HA WebSocket: {ha_url}")
            async with websockets.connect(ha_url) as ws:

                # ── Auth handshake ────────────────────────────────────────
                msg = json.loads(await ws.recv())
                if msg.get("type") != "auth_required":
                    log.error(f"Unexpected HA WS open message: {msg}")
                    await asyncio.sleep(delay)
                    continue

                await ws.send(json.dumps({"type": "auth", "access_token": token}))
                msg = json.loads(await ws.recv())

                if msg.get("type") == "auth_invalid":
                    log.error("HA auth failed — check ha_token in CONFIG.")
                    log.error("The token must be a valid Long-Lived Access Token.")
                    await asyncio.sleep(60)
                    continue

                if msg.get("type") != "auth_ok":
                    log.error(f"HA auth unexpected response: {msg}")
                    await asyncio.sleep(delay)
                    continue

                log.info(f"✓ Authenticated with Home Assistant (v{msg.get('ha_version', '?')})")

                # ── Auto-discover entity ──────────────────────────────────
                if not entity_id:
                    entity_id = await _discover_satellite_entity(ws, msg_id)
                    msg_id += 1
                    if entity_id:
                        log.info(f"Auto-discovered satellite entity: {entity_id}")
                    else:
                        log.warning("Set ha_satellite_entity in CONFIG to specify one manually.")

                # ── Subscribe to state_changed ────────────────────────────
                await ws.send(json.dumps({
                    "id": msg_id,
                    "type": "subscribe_events",
                    "event_type": "state_changed",
                }))
                sub_id = msg_id
                msg_id += 1

                msg = json.loads(await ws.recv())
                if not (msg.get("type") == "result" and msg.get("success")):
                    log.error(f"subscribe_events failed: {msg}")
                    await asyncio.sleep(delay)
                    continue

                log.info(f"Subscribed to state_changed events (satellite: {entity_id or 'any assist_satellite.*'})")
                delay = 5  # reset backoff on successful connect

                # ── Event loop ────────────────────────────────────────────
                async for raw in ws:
                    try:
                        event = json.loads(raw)
                    except json.JSONDecodeError:
                        continue

                    if event.get("type") != "event":
                        continue

                    data = event.get("event", {}).get("data", {})
                    eid  = data.get("entity_id", "")

                    # Filter to our satellite entity
                    if entity_id and eid != entity_id:
                        continue
                    if not entity_id and not eid.startswith("assist_satellite."):
                        continue

                    new_state = data.get("new_state", {}).get("state", "")
                    old_state = (data.get("old_state") or {}).get("state", "")

                    log.debug(f"HA state: {eid}  {old_state!r} → {new_state!r}")

                    mapped = HA_STATE_MAP.get(new_state)
                    if mapped is None:
                        continue

                    # Flash "wake" briefly when transitioning idle → listening
                    # (wake word was just detected)
                    if new_state == "listening" and old_state == "idle":
                        orb.set_state("wake")
                        await asyncio.sleep(WAKE_FLASH_DURATION)

                    orb.set_state(mapped)

        except Exception as e:
            log.error(f"HA WebSocket error: {e}")

        log.info(f"Reconnecting to HA in {delay:.0f}s…")
        await asyncio.sleep(delay)
        delay = min(delay * 1.5, 60)

# ─────────────────────────────────────────────────────────────────────────────
# STATE WATCHDOG — force idle if stuck in an active state too long
# ─────────────────────────────────────────────────────────────────────────────

WATCHDOG_TIMEOUTS = {
    "speaking":  45,
    "thinking":  30,
    "listening": 20,
    "wake":       5,
}

async def state_watchdog():
    import time
    last_state  = orb.state
    state_since = time.monotonic()

    while True:
        await asyncio.sleep(5)
        now     = time.monotonic()
        current = orb.state

        if current != last_state:
            last_state  = current
            state_since = now
            continue

        timeout = WATCHDOG_TIMEOUTS.get(current)
        if timeout and (now - state_since) > timeout:
            log.warning(f"⏱  Watchdog: '{current}' for {now - state_since:.0f}s — forcing idle")
            orb.set_state("idle")
            last_state  = "idle"
            state_since = now

# ─────────────────────────────────────────────────────────────────────────────
# WEBSOCKET SERVER (to orb display)
# ─────────────────────────────────────────────────────────────────────────────

async def ws_handler(websocket):
    q = asyncio.Queue(maxsize=20)
    orb.add_listener(q)
    addr = getattr(websocket, 'remote_address', ('?', '?'))
    log.info(f"🌐 Display connected: {addr[0]}:{addr[1]}")

    try:
        # Push current state immediately on connect
        await websocket.send(json.dumps({
            "state":       orb.state,
            "audio_level": orb.get_audio_level(),
        }))

        async def push_audio():
            while True:
                await asyncio.sleep(CONFIG["audio_push_interval"])
                try:
                    await websocket.send(json.dumps({
                        "audio_level": orb.get_audio_level()
                    }))
                except Exception:
                    break

        audio_task = asyncio.create_task(push_audio())

        while True:
            try:
                msg = await asyncio.wait_for(q.get(), timeout=25)
                await websocket.send(msg)
            except asyncio.TimeoutError:
                await websocket.send(json.dumps({"ping": 1}))

    except Exception:
        pass
    finally:
        audio_task.cancel()
        orb.remove_listener(q)
        log.info(f"🌐 Display disconnected: {addr[0]}:{addr[1]}")


async def run_ws_server():
    if not WS_AVAILABLE:
        log.error("websockets library not installed. Run: pip install websockets")
        return

    async with websockets.serve(
        ws_handler,
        CONFIG["ws_host"],
        CONFIG["ws_port"],
        ping_interval=20,
        ping_timeout=10,
    ):
        log.info(f"🔌 WebSocket server: ws://{CONFIG['ws_host']}:{CONFIG['ws_port']}")
        await asyncio.Future()

# ─────────────────────────────────────────────────────────────────────────────
# HTTP FILE SERVER — serves orb_display.html on port 8080
# ─────────────────────────────────────────────────────────────────────────────

async def run_http_server():
    try:
        from http.server import SimpleHTTPRequestHandler
        import socketserver
        import os

        os.chdir(Path(__file__).parent)

        class QuietHandler(SimpleHTTPRequestHandler):
            def log_message(self, *args): pass
            def end_headers(self):
                self.send_header("Access-Control-Allow-Origin", "*")
                super().end_headers()

        def serve():
            socketserver.TCPServer.allow_reuse_address = True
            with socketserver.TCPServer(("", 8080), QuietHandler) as httpd:
                log.info("🌍 HTTP server:  http://localhost:8080/orb_display.html")
                httpd.serve_forever()

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, serve)
    except Exception as e:
        log.warning(f"HTTP server error: {e}")

# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

async def main():
    log.info("━" * 55)
    log.info("  🔮  Samantha Voice Orb Bridge")
    log.info(f"  HA        : {CONFIG['ha_host']}:{CONFIG['ha_port']}")
    log.info(f"  Display   : http://localhost:8080/orb_display.html")
    log.info(f"  WebSocket : ws://localhost:{CONFIG['ws_port']}")
    log.info("━" * 55)

    if AUDIO_AVAILABLE:
        t = threading.Thread(target=audio_capture_thread, daemon=True)
        t.start()
    else:
        log.warning("Audio capture disabled — orb will not react to sound.")

    await asyncio.gather(
        run_ws_server(),
        run_http_server(),
        ha_ws_listener(),
        state_watchdog(),
    )


if __name__ == "__main__":
    import sys

    if "--list-devices" in sys.argv:
        list_audio_devices()
        sys.exit(0)

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Shutting down.")
