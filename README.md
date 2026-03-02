# Samantha — Pepper's Ghost Voice Orb

Home Assistant voice satellite with an audio-reactive holographic orb display, designed for the **Raspberry Pi Zero 2 W**.

---

## Architecture

```
Mic ──► LVA (local wake word) ──► voice_orb_bridge.py ──► WebSocket :8765 ──► orb_display.py
                                         │
                                  pyaudio RMS ──► audio_level (particle reactivity)
```

- **LVA** (`linux-voice-assistant`) — local wake word detection + Home Assistant pipeline
- **voice_orb_bridge.py** — reads LVA log events, pushes state over WebSocket
- **orb_display.py** — pygame/OpenGL ES 2.0 renderer with GLSL particle shaders (Pepper's Ghost kiosk)
- **satellite.py patch** — keeps wake words local; ignores HA's VoiceAssistantSetConfiguration overrides

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
├── orb_display.py            ← Pygame/GLES orb renderer (kiosk display)
├── voice_orb_bridge.py       ← State bridge: LVA log → WebSocket → orb
├── orb_display.html          ← Legacy browser display (kept for reference)
├── models/
│   ├── samantha.json         ← OpenWakeWord model config
│   └── samantha.tflite       ← Samantha wake word model
├── patches/
│   └── satellite.patch       ← Patch for linux-voice-assistant/satellite.py
├── systemd/
│   ├── voice-orb-bridge.service
│   └── linux-voice-assistant.service
├── setup.sh                  ← One-shot Pi setup script
└── README.md
```

---

## Quick Setup (Pi Zero 2 W)

### 1. Prerequisites

- Raspberry Pi OS **64-bit** with desktop (Bookworm)
- USB microphone + speaker (or HDMI audio)
- Home Assistant with a Voice Assistant pipeline configured

### 2. Clone and run setup

```bash
git clone <this-repo> ~/voice_orb
cd ~/voice_orb
chmod +x setup.sh
sudo ./setup.sh
```

### 3. Apply the satellite.py patch

```bash
cd ~/linux-voice-assistant
patch -p1 < ~/voice_orb/patches/satellite.patch
```

This makes wake word detection fully local — HA can no longer override which wake words are active.

### 4. Configure audio devices

```bash
# List input devices
cd ~/linux-voice-assistant
.venv/bin/python3 -m linux_voice_assistant --list-input-devices
.venv/bin/python3 -m linux_voice_assistant --list-output-devices
```

Edit `systemd/linux-voice-assistant.service` and replace `YOUR_MIC_DEVICE_NAME` / `YOUR_SPEAKER_DEVICE_NAME`.

### 5. Enable services

```bash
sudo systemctl daemon-reload
sudo systemctl enable voice-orb-bridge linux-voice-assistant
sudo systemctl start voice-orb-bridge linux-voice-assistant
```

### 6. Configure autostart (labwc kiosk)

Add to `~/.config/labwc/autostart`:

```bash
sleep 5
/usr/bin/python3 ~/voice_orb/orb_display.py >> /tmp/orb_display.log 2>&1 &
# Boost audio output (software gain — adjust sink names for your hardware)
pactl set-sink-volume alsa_output.platform-3f902000.hdmi.hdmi-stereo 150% 2>/dev/null || true
```

---

## Wake Word Models

**Samantha** (`models/samantha.tflite`) — OpenWakeWord model (custom trained).  
**Hey Jarvis** — bundled with LVA in `linux-voice-assistant/wakewords/`.

Both are listed in `~/.config/linux_voice_assistant/preferences.json`:

```json
{
  "active_wake_words": ["samantha", "hey_jarvis"],
  "volume": 0.71
}
```

To retrain the Samantha model, replace `models/samantha.tflite` with a new `.tflite` and restart LVA.

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
→ Watchdog resets to idle after: speaking 45s, thinking 30s, listening 20s  
→ Check bridge: `journalctl -fu voice-orb-bridge`

**Wake word not detected**  
→ Check LVA log: `tail -f /tmp/lva.log | grep -i "wake\|detect"`  
→ Hey Jarvis threshold is `0.97` — speak clearly and close to mic  
→ Samantha threshold is `0.5` — more permissive but model quality varies

**Wake words flip-flopping**  
→ Apply `patches/satellite.patch` to ignore HA's SetConfiguration messages

**Audio not reactive**  
→ Ensure `pyaudio` installed: `pip install pyaudio`  
→ Check mic index in `voice_orb_bridge.py` → `CONFIG["audio_device_index"]`
