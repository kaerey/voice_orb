# Samantha — Pepper's Ghost Voice Orb

Home Assistant voice satellite with an audio-reactive holographic orb display, designed for the **Raspberry Pi Zero 2 W** with the **Google AIY Voice Kit v1**.

---

## Architecture

```
Mic ──► LVA (local wake word + HA pipeline) ──► Home Assistant
                                                       │
                                              state_changed events
                                                       │
                                               voice_orb_bridge.py ──► WebSocket :8765 ──► orb_display.py
                                                       │
                                                pyaudio RMS ──► audio_level (particle reactivity)
```

- **LVA** (`linux-voice-assistant`) — local wake word detection + Home Assistant voice pipeline
- **voice_orb_bridge.py** — subscribes to HA's WebSocket API for satellite state changes, pushes them over WebSocket to the display
- **orb_display.py** — pygame/OpenGL ES 2.0 renderer with GLSL particle shaders (Pepper's Ghost kiosk)

---

## Display States

| State       | Behavior                                        |
|-------------|------------------------------------------------|
| `idle`      | Particle ring, hollow center, slow drift        |
| `wake`      | Center glow activates, orb expands              |
| `listening` | Orb grows, particles react to mic RMS           |
| `thinking`  | Orb focuses, particles tighten                  |
| `speaking`  | Full-screen edge-ring glow, organic breathing   |
| `error`     | Dim pulse                                       |

---

## Files

```
voice_orb/
├── orb_display.py              ← Pygame/GLES orb renderer (kiosk display)
├── voice_orb_bridge.py         ← HA WebSocket → state bridge → orb display
├── setup.py                    ← Interactive setup: HA token, satellite entity, audio device
├── bridge_config.json          ← Your local config (gitignored — contains token)
├── bridge_config.example.json  ← Template for bridge_config.json
├── models/
│   ├── samantha.json           ← OpenWakeWord model config
│   └── samantha.tflite         ← Samantha wake word model
├── systemd/
│   ├── voice-orb-bridge.service
│   └── linux-voice-assistant.service
├── config/
│   └── labwc-autostart         ← Labwc kiosk autostart config
└── README.md
```

---

## Quick Setup (Pi Zero 2 W)

### 1. Prerequisites

- Raspberry Pi OS **64-bit** with desktop (Bookworm)
- Google AIY Voice Kit v1 (or any mic/speaker supported by PipeWire)
- Home Assistant with a Voice Assistant pipeline configured
- LVA (`linux-voice-assistant`) installed and connected to HA

### 2. Clone the repo

```bash
git clone https://github.com/kaerey/voice_orb ~/voice_orb
cd ~/voice_orb
```

### 3. Install Python dependencies

```bash
pip install websockets pyaudio numpy
```

### 4. Run the interactive setup

```bash
python3 setup.py
```

This will prompt you for:
- **HA host and port** (e.g. `192.168.1.176`, `8123`)
- **Long-Lived Access Token** — create one in HA: Settings → Profile → Long-Lived Access Tokens → Create
- **Voice satellite entity** — lists all `assist_satellite.*` entities in your HA and lets you pick the right one
- **Audio input device** — lists available mic devices and lets you pick by index

The setup script tests your HA connection and writes `bridge_config.json` (gitignored — never committed).

### 5. Install and enable services

```bash
sudo cp systemd/linux-voice-assistant.service /etc/systemd/system/
sudo cp systemd/voice-orb-bridge.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable voice-orb-bridge linux-voice-assistant
sudo systemctl start voice-orb-bridge linux-voice-assistant
```

### 6. Configure autostart (labwc kiosk)

Copy the labwc autostart config:

```bash
mkdir -p ~/.config/labwc
cp config/labwc-autostart ~/.config/labwc/autostart
```

Or add manually to `~/.config/labwc/autostart`:

```bash
sleep 5
/usr/bin/python3 ~/voice_orb/orb_display.py >> /tmp/orb_display.log 2>&1 &
```

### 7. Verify

```bash
# Bridge logs — should show:
# ✓ Authenticated with Home Assistant
# Subscribed to state_changed events (satellite: assist_satellite.xxx)
journalctl -fu voice-orb-bridge

# LVA logs
journalctl -fu linux-voice-assistant
```

---

## Google AIY Voice Kit v1 Setup

Add to `/boot/firmware/config.txt`:

```
dtoverlay=googlevoicehat-soundcard
dtparam=audio=off
```

The AIY hat exposes:
- **Mic**: `Built-in Audio Stereo`
- **Speaker**: `pipewire/alsa_output.platform-soc_sound.stereo-fallback`

These are set in `systemd/linux-voice-assistant.service`.

> **Note on audio processing**: Do not use PulseAudio's `module-echo-cancel` with WebRTC — it degrades Whisper ASR quality. The AIY kit hardware handles mic/speaker separation adequately without software AEC.

---

## Wake Word Models

**Samantha** (`models/samantha.tflite`) — OpenWakeWord model (custom trained).
**Hey Jarvis** — bundled with LVA in `linux-voice-assistant/wakewords/`.

Both are configured via HA's ESPHome integration. Active wake words are saved to:

```
~/.config/linux_voice_assistant/preferences.json
```

---

## Pepper's Ghost Display Build

The orb is designed to float inside a **glass dome** using the Pepper's Ghost illusion — a classic stage trick that makes a 2D image appear as a solid, luminous 3D object suspended in mid-air.

### How it works

A small **polycarbonate reflector panel** (cut to ~45°) sits inside the dome and catches the screen image reflecting it toward the viewer. Because the background is pure black, only the glowing orb is visible in the reflection — the screen itself disappears.

```
        ┌─────────────────────┐
        │     Glass Dome      │
        │                     │
        │      ✦ orb ✦        │  ← reflected image appears to float here
        │         ↗           │
        │   ╱ reflector ╲     │  ← 45° polycarbonate panel
        │                     │
        └──────────┬──────────┘
                   │
           ┌───────┴───────┐
           │  7" Pi Screen  │  ← face-up, displaying orb_display.py
           └───────────────┘
```

### Components

| Part | Notes |
|------|-------|
| **Glass dome** | Bell jar or cloche style; diameter sets how large the floating orb appears |
| **Polycarbonate sheet** | ~2–3mm, cut to a circle that fits inside the dome at 45°; thin acrylic also works |
| **7" Raspberry Pi display** | Placed face-up beneath the dome; HDMI or DSI connection to the Pi |
| **Raspberry Pi Zero 2 W** | Sits alongside or beneath the display |

### Tips

- Background must be **pure black** — anything non-black becomes visible in the reflection
- Orb is centered with a wide black border to give the illusion room to breathe
- Reflector angle: **45°** is ideal; small deviations shift where the image appears to float
- Screen brightness: **80–100%** — brighter = more vivid reflection
- Lower ambient light = more convincing illusion; works best in a dim room
- A matte black interior on the dome base eliminates stray reflections

---

## Troubleshooting

**Orb stuck in one state**
→ Watchdog resets to idle after: speaking 45s, thinking 30s, listening 20s, wake 5s
→ Check bridge: `journalctl -fu voice-orb-bridge`

**Bridge not authenticating with HA**
→ Re-run `python3 setup.py` to update `bridge_config.json` with a fresh token
→ Create a Long-Lived Access Token in HA: Settings → Profile → Long-Lived Access Tokens

**Wrong satellite entity being tracked**
→ Re-run `python3 setup.py` and select the correct entity from the list

**Wake word not detected**
→ Check LVA log: `journalctl -fu linux-voice-assistant`
→ Hey Jarvis threshold is `0.97` — speak clearly and close to mic
→ Samantha threshold is `0.5` — more permissive but model quality varies

**Audio not reactive**
→ Ensure `pyaudio` installed: `pip install pyaudio numpy`
→ Re-run `python3 setup.py` to pick the correct audio input device
