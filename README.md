# C20 Smartwatch → VRChat OSC Heart Rate Bridge

Streams live heart rate from a **C20 smartwatch** to VRChat as avatar parameters and Magic Chatbox text.

## Quick Start

### Windows

1. Install [Python 3.11+](https://www.python.org/downloads/) (check "Add to PATH")
2. Open Command Prompt or PowerShell in this folder:
   ```
   pip install -r requirements.txt
   python hr_bridge.py
   ```
3. Make sure your C20 watch is awake and nearby

### Linux

```
pip install -r requirements.txt
python hr_bridge.py [optional:watch-address]
```

## OSC Parameters Sent

| OSC Address | Type | Description |
|---|---|---|
| `/avatar/parameters/isHRConnected` | bool | True while connected |
| `/avatar/parameters/HR` | int | Heart rate in BPM |
| `/avatar/parameters/floatHR` | float | HR mapped 0.0–1.0 |
| `/avatar/parameters/HRBattery` | int | Watch battery 0–100 |
| `/avatar/parameters/HRBatteryFloat` | float | Battery 0.0–1.0 |
| `/chatbox/input` | string | "❤️ {BPM} BPM" for Magic Chatbox |

## Troubleshooting

- **"Device not found"** — restart the watch and try again
- **No HR appearing** — try opening the heart rate app on the watch
- **Disconnects** — the bridge auto-reconnects automatically

## How It Works

The C20 watch exposes a standard BLE Heart Rate Service (0x180D) with the HR measurement characteristic (0x2A37). The bridge subscribes to notifications on this characteristic and forwards every reading to VRChat via OSC (port 9000). It also uses the proprietary MOYOUNG V2 protocol (FEE2/FEE3) to initialize continuous HR monitoring and as a fallback polling method.

## Technical Details

- Watch BLE address: `96:D6:AF:D0:2B:6E` (name: `C 20`)
- Protocol: MOYOUNG V2 (Gadgetbridge-compatible)
- Standard HR characteristic: 0x2A37 (notify)
- MOYOUNG write: 0xFEE2 / notify: 0xFEE3
- Battery: 0x2A19
