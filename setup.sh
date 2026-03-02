#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# setup.sh  —  Samantha Voice Orb  —  Pi Zero 2 W setup script
#
# Run this once on a fresh Raspberry Pi OS Lite (64-bit) install:
#   chmod +x setup.sh && sudo ./setup.sh
#
# What it does:
#   1. Installs system packages (Chromium, X11, audio, Python deps)
#   2. Clones & sets up linux-voice-assistant
#   3. Copies voice-orb files to ~/voice-orb/
#   4. Installs & enables systemd services
#   5. Configures Chromium kiosk autostart
# ─────────────────────────────────────────────────────────────────────────────

set -e

# ── Must run as root ──────────────────────────────────────────────────────────
if [[ $EUID -ne 0 ]]; then
  echo "Please run as root: sudo ./setup.sh"
  exit 1
fi

# ── Detect the actual user (not root) ────────────────────────────────────────
ACTUAL_USER="${SUDO_USER:-pi}"
ACTUAL_HOME=$(eval echo "~$ACTUAL_USER")
VOICE_ORB_DIR="$ACTUAL_HOME/voice-orb"
LVA_DIR="$ACTUAL_HOME/linux-voice-assistant"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo ""
echo "════════════════════════════════════════════════"
echo "  🔮  Samantha Voice Orb — Setup"
echo "  User: $ACTUAL_USER  |  Home: $ACTUAL_HOME"
echo "════════════════════════════════════════════════"
echo ""

# ─────────────────────────────────────────────────────────────────────────────
# 1. System packages
# ─────────────────────────────────────────────────────────────────────────────
echo "▶ Installing system packages…"
apt-get update -qq
apt-get install -y \
  chromium \
  xserver-xorg xinit x11-xserver-utils xdotool \
  unclutter \
  python3-pip python3-venv python3-dev \
  portaudio19-dev \
  git \
  pulseaudio \
  alsa-utils \
  --no-install-recommends

echo "✓ System packages installed"

# ─────────────────────────────────────────────────────────────────────────────
# 2. Python dependencies for the bridge
# ─────────────────────────────────────────────────────────────────────────────
echo "▶ Installing Python dependencies…"
sudo -u "$ACTUAL_USER" pip3 install --user --break-system-packages websockets pyaudio numpy
echo "✓ Python deps installed"

# ─────────────────────────────────────────────────────────────────────────────
# 3. linux-voice-assistant
# ─────────────────────────────────────────────────────────────────────────────
if [ ! -d "$LVA_DIR" ]; then
  echo "▶ Cloning linux-voice-assistant…"
  sudo -u "$ACTUAL_USER" git clone https://github.com/OHF-Voice/linux-voice-assistant.git "$LVA_DIR"
  echo "▶ Running LVA setup…"
  sudo -u "$ACTUAL_USER" python3 "$LVA_DIR/script/setup"
  echo "✓ LVA installed at $LVA_DIR"
else
  echo "✓ LVA already exists at $LVA_DIR — skipping clone"
fi

# ─────────────────────────────────────────────────────────────────────────────
# 4. Copy voice-orb files
# ─────────────────────────────────────────────────────────────────────────────
echo "▶ Installing voice-orb files to $VOICE_ORB_DIR…"
sudo -u "$ACTUAL_USER" mkdir -p "$VOICE_ORB_DIR/models"

sudo -u "$ACTUAL_USER" cp "$SCRIPT_DIR/orb_display.html"   "$VOICE_ORB_DIR/"
sudo -u "$ACTUAL_USER" cp "$SCRIPT_DIR/voice_orb_bridge.py" "$VOICE_ORB_DIR/"
sudo -u "$ACTUAL_USER" cp "$SCRIPT_DIR/models/samantha.json" "$VOICE_ORB_DIR/models/"

# Copy samantha.tflite if present next to this script
if [ -f "$SCRIPT_DIR/models/samantha.tflite" ]; then
  sudo -u "$ACTUAL_USER" cp "$SCRIPT_DIR/models/samantha.tflite" "$VOICE_ORB_DIR/models/"
  echo "✓ samantha.tflite copied"
else
  echo ""
  echo "  ⚠  models/samantha.tflite not found next to setup.sh"
  echo "     Copy it manually: cp samantha.tflite $VOICE_ORB_DIR/models/"
  echo ""
fi

echo "✓ Voice-orb files installed"

# ─────────────────────────────────────────────────────────────────────────────
# 5. Systemd services
# ─────────────────────────────────────────────────────────────────────────────
echo "▶ Installing systemd services…"
cp "$SCRIPT_DIR/systemd/voice-orb-bridge.service"        /etc/systemd/system/
cp "$SCRIPT_DIR/systemd/linux-voice-assistant.service"   /etc/systemd/system/

# Patch user in service files if not 'pi'
if [ "$ACTUAL_USER" != "pi" ]; then
  sed -i "s|User=pi|User=$ACTUAL_USER|g" /etc/systemd/system/voice-orb-bridge.service
  sed -i "s|User=pi|User=$ACTUAL_USER|g" /etc/systemd/system/linux-voice-assistant.service
  sed -i "s|/home/pi|$ACTUAL_HOME|g"     /etc/systemd/system/voice-orb-bridge.service
  sed -i "s|/home/pi|$ACTUAL_HOME|g"     /etc/systemd/system/linux-voice-assistant.service
fi

systemctl daemon-reload
systemctl enable voice-orb-bridge.service
# Note: linux-voice-assistant.service is NOT auto-enabled until you set your
# audio device names in the service file. See README.md step 4.
echo "✓ voice-orb-bridge enabled (will start on boot)"
echo "  ℹ  linux-voice-assistant.service installed but NOT enabled yet"
echo "     Edit the service file first, then: sudo systemctl enable linux-voice-assistant"

# ─────────────────────────────────────────────────────────────────────────────
# 6. Chromium kiosk autostart (LXDE)
# ─────────────────────────────────────────────────────────────────────────────
echo "▶ Configuring Chromium kiosk autostart…"
AUTOSTART_DIR="$ACTUAL_HOME/.config/lxsession/LXDE-pi"
sudo -u "$ACTUAL_USER" mkdir -p "$AUTOSTART_DIR"

cat > "$AUTOSTART_DIR/autostart" << EOF
@xset s off
@xset -dpms
@xset s noblank
@unclutter -idle 0 -root
@chromium-browser \\
  --noerrdialogs \\
  --kiosk \\
  --disable-infobars \\
  --disable-session-crashed-bubble \\
  --disable-restore-session-state \\
  --use-fake-ui-for-media-stream \\
  --disable-translate \\
  --no-first-run \\
  --fast \\
  --fast-start \\
  --disable-features=TranslateUI \\
  http://localhost:8080/orb_display.html
EOF

chown "$ACTUAL_USER:$ACTUAL_USER" "$AUTOSTART_DIR/autostart"
echo "✓ Chromium kiosk configured"

# ─────────────────────────────────────────────────────────────────────────────
# 7. GPU memory for smoother canvas rendering
# ─────────────────────────────────────────────────────────────────────────────
if ! grep -q "^gpu_mem=" /boot/config.txt 2>/dev/null; then
  echo "gpu_mem=128" >> /boot/config.txt
  echo "✓ GPU memory set to 128MB in /boot/config.txt"
else
  echo "ℹ  gpu_mem already set in /boot/config.txt"
fi

# ─────────────────────────────────────────────────────────────────────────────
# Done
# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo "════════════════════════════════════════════════"
echo "  ✅  Setup complete!"
echo ""
echo "  Next steps:"
echo ""
echo "  1. Copy your samantha.tflite into:"
echo "     $VOICE_ORB_DIR/models/"
echo ""
echo "  2. Find your audio device names:"
echo "     python3 voice_orb_bridge.py --list-devices"
echo "     cd $LVA_DIR && source .venv/bin/activate"
echo "     python3 -m linux_voice_assistant --list-input-devices"
echo ""
echo "  3. Edit the LVA service file with your device names:"
echo "     sudo nano /etc/systemd/system/linux-voice-assistant.service"
echo "     (replace YOUR_MIC_DEVICE_NAME and YOUR_SPEAKER_DEVICE_NAME)"
echo ""
echo "  4. Enable and start everything:"
echo "     sudo systemctl daemon-reload"
echo "     sudo systemctl enable linux-voice-assistant"
echo "     sudo systemctl start voice-orb-bridge linux-voice-assistant"
echo ""
echo "  5. Test the display now (before rebooting):"
echo "     cd $VOICE_ORB_DIR && python3 voice_orb_bridge.py"
echo "     Open http://localhost:8080/orb_display.html"
echo "     Press i/w/l/t/s/e keys to cycle orb states"
echo ""
echo "  6. Reboot when ready:"
echo "     sudo reboot"
echo "════════════════════════════════════════════════"
echo ""
