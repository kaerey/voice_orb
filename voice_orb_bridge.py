#!/usr/bin/env python3
"""
voice_orb_bridge.py
───────────────────
Bridges the OHF linux-voice-assistant state events to the WebSocket
served to the Pepper's Ghost orb display (orb_display.html).

Architecture:
  Mic ──► LVA (samantha wake word + HA pipeline) ──► this bridge ──► WebSocket ──► orb_display.html
                                                            │
                                                      pyaudio RMS ──► audio_level (particle reactivity)

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

# ── WebSocket server ─────────────────────────────────────────────────────────
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
# CONFIG — edit these to match your setup
# ─────────────────────────────────────────────────────────────────────────────

CONFIG = {
    # WebSocket server (orb display connects here)
    "ws_host": "0.0.0.0",
    "ws_port": 8765,

    # Audio input — -1 = system default mic
    # Run with --list-devices to find your mic index
    "audio_device_index": -1,
    "audio_chunk": 512,
    "audio_rate": 48000,
    "audio_channels": 1,

    # LVA event socket — LVA publishes pipeline events here
    "lva_event_host": "localhost",
    "lva_event_port": 6053,

    # LVA log file fallback — set to "/tmp/lva.log" if event socket isn't working
    # Run LVA with:  python3 -m linux_voice_assistant ... 2>&1 | tee /tmp/lva.log
    "lva_log_file": "/tmp/lva.log",

    # How often to push audio level to the display (seconds)
    "audio_push_interval": 0.05,

    # Wake word name (used in log messages only)
    "wake_word_name": "samantha",
}

# ─────────────────────────────────────────────────────────────────────────────
# SHARED STATE
# ─────────────────────────────────────────────────────────────────────────────

class OrbState:
    VALID_STATES = {"idle", "wake", "listening", "thinking", "speaking", "error"}

    def __init__(self):
        self._state      = "idle"
        self._audio_level = 0.0
        self._lock       = threading.Lock()
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
# LVA EVENT SOCKET LISTENER (primary integration)
#
# LVA emits Wyoming pipeline events over a TCP socket.
# Connect LVA with:  --event-uri tcp://localhost:6053
# ─────────────────────────────────────────────────────────────────────────────

LVA_EVENT_MAP = {
    # Wyoming pipeline events
    "run-start":            "idle",
    "wake-word-start":      "idle",
    "wake-word-detected":   "wake",
    "asr-start":            "listening",
    "asr-stop":             "thinking",
    "asr-end":              "thinking",
    "intent-start":         "thinking",
    "tts-start":            "speaking",
    "tts-end":              "idle",
    "run-end":              "idle",
    "error":                "error",
    # ESPHome numeric event codes
    "1": "idle",
    "2": "wake",
    "3": "listening",
    "4": "thinking",
    "5": "speaking",
    "6": "idle",
    "7": "error",
}


async def lva_event_listener():
    host  = CONFIG["lva_event_host"]
    port  = CONFIG["lva_event_port"]
    delay = 5
    max_delay = 60
    attempt = 0
    silent  = False

    while True:
        try:
            if not silent:
                log.info(f"Connecting to LVA event socket {host}:{port}…")
            reader, writer = await asyncio.open_connection(host, port)

            # Reset on successful connect
            delay   = 5
            attempt = 0
            silent  = False
            log.info(f"✓ LVA event socket connected — listening for '{CONFIG['wake_word_name']}'")

            while True:
                line = await reader.readline()
                if not line:
                    log.warning("LVA event socket closed — will reconnect.")
                    break
                text = line.decode().strip()
                try:
                    msg = json.loads(text)
                    event_type = msg.get("type", "")
                except json.JSONDecodeError:
                    event_type = text

                mapped = LVA_EVENT_MAP.get(event_type)
                if mapped:
                    orb.set_state(mapped)
                else:
                    log.debug(f"Unmapped LVA event: {event_type!r}")

        except (ConnectionRefusedError, OSError):
            attempt += 1
            if not silent:
                log.info(
                    f"LVA not running yet on port {port} — retrying silently in background.\n"
                    f"          Start LVA when ready; the bridge will auto-connect."
                )
                silent = True
            elif attempt % 12 == 0:
                log.info(f"Still waiting for LVA on port {port}… (attempt {attempt})")
            await asyncio.sleep(delay)
            delay = min(delay * 1.5, max_delay)

        except Exception as e:
            log.error(f"LVA event listener error: {e}")
            await asyncio.sleep(delay)
            delay = min(delay * 1.5, max_delay)

# ─────────────────────────────────────────────────────────────────────────────
# FALLBACK: LOG FILE WATCHER
#
# If the event socket isn't available, watch LVA's stdout log.
# Pipe LVA output:  python3 -m linux_voice_assistant ... 2>&1 | tee /tmp/lva.log
# ─────────────────────────────────────────────────────────────────────────────

LOG_KEYWORD_MAP = {
    # LVA debug log patterns (with --debug flag enabled)
    "wake_word_triggered":            "wake",     # INFO: wake sound playing = wake detected
    "voice_assistant_stt_start":      "listening", # DEBUG: STT recording started
    "voice_assistant_stt_end":        "thinking",  # DEBUG: STT finished, processing intent
    "voice_assistant_intent_start":   "thinking",  # DEBUG: intent processing
    "voice_assistant_tts_start":      "speaking",  # DEBUG: TTS audio starting
    "tts response finished":          "idle",      # DEBUG: audio playback actually done
    "voice_assistant_error":          "error",     # DEBUG: pipeline error
    "stt-no-text-recognized":         "idle",      # DEBUG: nothing heard, back to idle
    "disconnected from home assistant": "idle",    # LVA lost HA connection — reset state
}


async def lva_log_watcher():
    path = CONFIG.get("lva_log_file")
    if not path:
        return

    p = Path(path)
    log.info(f"📄 Log watcher armed — watching {path} (waiting for file…)")

    while not p.exists():
        await asyncio.sleep(2)

    log.info(f"📄 Log watcher active: {path}")

    with open(p, "r") as f:
        f.seek(0, 2)  # seek to end — don't replay old logs
        while True:
            line = f.readline()
            if line:
                lower = line.lower()
                for kw, state in LOG_KEYWORD_MAP.items():
                    if kw in lower:
                        orb.set_state(state)
                        break
            else:
                await asyncio.sleep(0.1)

# ─────────────────────────────────────────────────────────────────────────────
# STATE WATCHDOG — force idle if stuck in an active state too long
# ─────────────────────────────────────────────────────────────────────────────

WATCHDOG_TIMEOUTS = {
    "speaking":  45,   # TTS should never take longer than 45s
    "thinking":  30,   # LLM should respond within 30s
    "listening": 20,   # STT window
}

async def state_watchdog():
    import time
    last_state     = orb.state
    state_since    = time.monotonic()

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
# WEBSOCKET SERVER
# ─────────────────────────────────────────────────────────────────────────────

async def ws_handler(websocket):
    q = asyncio.Queue(maxsize=20)
    orb.add_listener(q)
    addr = getattr(websocket, 'remote_address', ('?', '?'))
    log.info(f"🌐 Display connected: {addr[0]}:{addr[1]}")

    # Push current state immediately on connect
    try:
        await websocket.send(json.dumps({
            "state":       orb.state,
            "audio_level": orb.get_audio_level(),
        }))

        # Continuous audio level push task
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

        # Forward state change messages
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
                # Allow cross-origin for local dev
                self.send_header("Access-Control-Allow-Origin", "*")
                super().end_headers()

        def serve():
            # Allow fast restart by reusing port
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
    log.info(f"  Wake word : {CONFIG['wake_word_name']}")
    log.info(f"  Display   : http://localhost:8080/orb_display.html")
    log.info(f"  WebSocket : ws://localhost:{CONFIG['ws_port']}")
    log.info("━" * 55)

    # Start audio capture thread
    if AUDIO_AVAILABLE:
        t = threading.Thread(target=audio_capture_thread, daemon=True)
        t.start()
    else:
        log.warning("Audio capture disabled — orb will not react to sound.")

    await asyncio.gather(
        run_ws_server(),
        run_http_server(),
        lva_event_listener(),
        lva_log_watcher(),
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
