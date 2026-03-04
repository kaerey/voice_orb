#!/usr/bin/env python3
"""
setup.py — Interactive setup for Samantha Voice Orb Bridge
───────────────────────────────────────────────────────────
Guides you through:
  1. Home Assistant connection (host, port, long-lived token)
  2. Voice satellite entity selection
  3. Audio input device selection

Writes bridge_config.json with your settings.
That file is gitignored and loaded by voice_orb_bridge.py at startup.

Usage:
  python3 setup.py
"""

import json
import sys
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

CONFIG_FILE = Path(__file__).parent / "bridge_config.json"


def hr(char="─", width=55):
    print(char * width)


def prompt(question, default=None, password=False):
    suffix = f" [{default}]" if default is not None else ""
    try:
        if password:
            import getpass
            val = getpass.getpass(f"  {question}{suffix}: ").strip()
        else:
            val = input(f"  {question}{suffix}: ").strip()
    except (KeyboardInterrupt, EOFError):
        print("\nAborted.")
        sys.exit(0)
    return val if val else (default if default is not None else "")


def prompt_int(question, default=None, min_val=None, max_val=None):
    while True:
        raw = prompt(question, default=str(default) if default is not None else None)
        try:
            val = int(raw)
            if min_val is not None and val < min_val:
                print(f"  ✗ Must be >= {min_val}")
                continue
            if max_val is not None and val > max_val:
                print(f"  ✗ Must be <= {max_val}")
                continue
            return val
        except ValueError:
            print("  ✗ Please enter a number.")


def ha_request(host, port, token, path):
    url = f"http://{host}:{port}{path}"
    req = Request(url, headers={"Authorization": f"Bearer {token}"})
    with urlopen(req, timeout=5) as resp:
        return json.loads(resp.read())


def test_ha(host, port, token):
    try:
        data = ha_request(host, port, token, "/api/")
        return True, data.get("message", "OK")
    except HTTPError as e:
        if e.code == 401:
            return False, "Invalid token (401 Unauthorized)"
        return False, f"HTTP {e.code}"
    except URLError as e:
        return False, f"Cannot reach HA at {host}:{port} — {e.reason}"
    except Exception as e:
        return False, str(e)


def list_satellite_entities(host, port, token):
    try:
        states = ha_request(host, port, token, "/api/states")
        return [
            s for s in states
            if s.get("entity_id", "").startswith("assist_satellite.")
        ]
    except Exception as e:
        print(f"  ✗ Could not fetch entities: {e}")
        return []


def list_audio_input_devices():
    try:
        import pyaudio
        pa = pyaudio.PyAudio()
        devices = []
        for i in range(pa.get_device_count()):
            info = pa.get_device_info_by_index(i)
            if info["maxInputChannels"] > 0:
                devices.append((i, info["name"], int(info["defaultSampleRate"])))
        pa.terminate()
        return devices
    except ImportError:
        return None
    except Exception as e:
        print(f"  ✗ Audio device enumeration failed: {e}")
        return None


# ─────────────────────────────────────────────────────────────────────────────

def main():
    hr("━")
    print("  🔮  Samantha Voice Orb Bridge — Setup")
    hr("━")
    print()

    # Load existing config as defaults
    existing = {}
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE) as f:
                existing = json.load(f)
            print(f"  Found existing config: {CONFIG_FILE}")
            print("  (Press Enter to keep current values)\n")
        except Exception:
            pass

    config = dict(existing)

    # ── Step 1: Home Assistant ───────────────────────────────────────────────
    hr()
    print("  Step 1 of 3 — Home Assistant connection")
    hr()
    print()
    print("  You need a Long-Lived Access Token from HA:")
    print("  Settings → Profile → scroll to bottom → Long-Lived Access Tokens → Create")
    print()

    while True:
        ha_host = prompt("HA IP address or hostname", default=existing.get("ha_host", "192.168.1.x"))
        ha_port = prompt_int("HA port", default=existing.get("ha_port", 8123))
        ha_token = prompt("Long-Lived Access Token", password=True)
        if not ha_token and existing.get("ha_token"):
            ha_token = existing["ha_token"]
            print("  (keeping existing token)")

        if not ha_token:
            print("  ✗ Token is required.")
            continue

        print(f"\n  Testing connection to {ha_host}:{ha_port}…")
        ok, msg = test_ha(ha_host, ha_port, ha_token)
        if ok:
            print(f"  ✓ Connected — {msg}\n")
            break
        else:
            print(f"  ✗ Failed — {msg}")
            retry = prompt("Try again? (y/n)", default="y")
            if retry.lower() != "y":
                print("  Saving what we have and continuing.\n")
                break

    config["ha_host"] = ha_host
    config["ha_port"] = ha_port
    config["ha_token"] = ha_token

    # ── Step 2: Satellite entity ─────────────────────────────────────────────
    hr()
    print("  Step 2 of 3 — Voice satellite entity")
    hr()
    print()

    satellites = list_satellite_entities(ha_host, ha_port, ha_token)
    chosen_entity = existing.get("ha_satellite_entity", "")

    if satellites:
        print(f"  Found {len(satellites)} assist_satellite entity/entities:\n")
        for i, s in enumerate(satellites):
            state = s.get("state", "?")
            name = s.get("attributes", {}).get("friendly_name", s["entity_id"])
            marker = " ◄ current" if s["entity_id"] == chosen_entity else ""
            print(f"  [{i}] {name}  ({s['entity_id']})  state={state}{marker}")
        print()

        if len(satellites) == 1 and not chosen_entity:
            chosen_entity = satellites[0]["entity_id"]
            print(f"  Auto-selected: {chosen_entity}\n")
        else:
            idx = prompt_int(
                "Select entity number",
                default=next(
                    (i for i, s in enumerate(satellites) if s["entity_id"] == chosen_entity),
                    0,
                ),
                min_val=0,
                max_val=len(satellites) - 1,
            )
            chosen_entity = satellites[idx]["entity_id"]
            print(f"  ✓ Selected: {chosen_entity}\n")
    else:
        print("  No assist_satellite entities found automatically.")
        chosen_entity = prompt("Enter entity ID manually", default=chosen_entity or "assist_satellite.")
        print()

    config["ha_satellite_entity"] = chosen_entity

    # ── Step 3: Audio input device ───────────────────────────────────────────
    hr()
    print("  Step 3 of 3 — Audio input device (for orb sound reactivity)")
    hr()
    print()

    devices = list_audio_input_devices()
    current_idx = existing.get("audio_device_index", -1)

    if devices is None:
        print("  ⚠  pyaudio not installed — skipping audio device selection.")
        print("     Install with: pip install pyaudio numpy")
        print("     Audio reactivity will be disabled until configured.\n")
        config.setdefault("audio_device_index", -1)
    elif not devices:
        print("  ⚠  No audio input devices found.\n")
        config["audio_device_index"] = -1
    else:
        print("  Available audio input devices:\n")
        for idx, name, rate in devices:
            marker = " ◄ current" if idx == current_idx else ""
            print(f"  [{idx}] {name}  ({rate} Hz){marker}")
        print()
        print("  Note: LVA uses the mic exclusively. The bridge opens it in")
        print("  read-only mode for level metering only — conflicts are rare")
        print("  with PipeWire but may occur with raw ALSA.\n")

        chosen_idx = prompt_int(
            "Enter device index (-1 = auto)",
            default=current_idx if current_idx in [d[0] for d in devices] else -1,
            min_val=-1,
            max_val=max(d[0] for d in devices),
        )
        config["audio_device_index"] = chosen_idx
        if chosen_idx >= 0:
            name = next((d[1] for d in devices if d[0] == chosen_idx), "?")
            print(f"  ✓ Selected: [{chosen_idx}] {name}\n")
        else:
            print("  ✓ Auto-detect enabled\n")

    # ── Write config ─────────────────────────────────────────────────────────
    hr()
    print("  Writing bridge_config.json…")

    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)
        f.write("\n")

    print(f"  ✓ Saved: {CONFIG_FILE}")
    print()
    hr("━")
    print("  Setup complete!")
    print()
    print("  To apply: sudo systemctl restart voice-orb-bridge")
    print("  To verify: journalctl -fu voice-orb-bridge")
    hr("━")


if __name__ == "__main__":
    main()
