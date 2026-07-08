#!/usr/bin/env python3
"""C20 Smartwatch → VRChat OSC Heart Rate Bridge — GUI Edition."""
import asyncio
import json
import os
import platform
import queue
import threading
import tkinter as tk
from tkinter import ttk, scrolledtext
from typing import Optional

from bleak import BleakClient, BleakScanner
from pythonosc.udp_client import SimpleUDPClient

# ── try media detection ─────────────────────────────────────
try:
    import winrt.windows.media.control as wmc
    HAS_MEDIA = True
except ImportError:
    HAS_MEDIA = False

# ── BLE UUIDs ───────────────────────────────────────────────
BLE_HR_MEASURE = "00002a37-0000-1000-8000-00805f9b34fb"
BLE_BATTERY = "00002a19-0000-1000-8000-00805f9b34fb"
BLE_FEE2_OUT = "0000fee2-0000-1000-8000-00805f9b34fb"
BLE_FEE3_IN = "0000fee3-0000-1000-8000-00805f9b34fb"
CMD_START_DYNAMIC_HR = 104
CMD_TRIGGER_HR = 109
CMD_SET_HR_INTERVAL = 31
CMD_SET_QUICK_VIEW = 24
IS_LINUX = platform.system() == "Linux"
DEFAULT_ADDR = "96:D6:AF:D0:2B:6E"
DEFAULT_TEMPLATE = "❤️ {bpm} BPM  🔋 {battery}%"


def make_packet(cmd, payload=bytes()):
    data = bytearray([0xFE, 0xEA, 0x10, 0x00, cmd]) + payload
    data[3] = len(data)
    return bytes(data)


if HAS_MEDIA:
    async def get_media_info():
        session = await wmc.GlobalSystemMediaTransportControlsSessionManager.request_async()
        s = session.get_current_session()
        if s is None:
            return None
        info = await s.try_get_media_properties_async()
        return {"title": info.title, "artist": info.artist}
else:
    async def get_media_info():
        return None


# ── Bridge Engine ───────────────────────────────────────────
class HRBridge:
    def __init__(self, address: str, template: str, log_cb, media_enabled: bool):
        self.address = address
        self.template = template
        self.log = log_cb
        self.media_enabled = media_enabled
        self.osc = SimpleUDPClient("127.0.0.1", 9000)
        self.bpm = 0
        self.battery = 0
        self.running = False
        self.song = ""
        self.artist = ""
        self._client: Optional[BleakClient] = None

    def log_msg(self, msg: str):
        self.log(msg)

    def send_osc(self):
        text = self.template.replace("{bpm}", str(self.bpm)).replace("{battery}", str(self.battery))
        if self.media_enabled and (self.song or self.artist):
            sep = " — " if self.song and self.artist else ""
            media_text = f"{self.song}{sep}{self.artist}".strip()
            text = text.replace("{song}", media_text).replace("{artist}", self.artist).replace("{title}", self.song)
        else:
            text = text.replace("{song}", "").replace("{artist}", "").replace("{title}", "")
        text = text.strip()
        self.osc.send_message("/avatar/parameters/isHRConnected", True)
        self.osc.send_message("/avatar/parameters/HR", int(self.bpm))
        self.osc.send_message("/avatar/parameters/floatHR", min(self.bpm / 255.0, 1.0))
        self.osc.send_message("/avatar/parameters/HRBattery", self.battery)
        self.osc.send_message("/avatar/parameters/HRBatteryFloat", self.battery / 100.0)
        if text:
            self.osc.send_message("/chatbox/input", [text, True])
        self.log_msg(f"❤️ {self.bpm} BPM  🔋 {self.battery}%{'  🎵 ' + media_text if self.media_enabled and (self.song or self.artist) else ''}")

    def on_hr(self, _h, data):
        if len(data) < 2:
            return
        flags = data[0]
        bpm = data[2] if (flags & 1) else data[1]
        if 20 <= bpm <= 250:
            self.bpm = bpm
            self.send_osc()

    def on_fee3(self, _h, data):
        if len(data) < 5 or data[0] != 0xFE or data[1] != 0xEA:
            return
        cmd = data[4]
        if cmd == CMD_TRIGGER_HR and len(data) >= 6:
            bpm = data[5]
            if 20 <= bpm <= 250:
                self.bpm = bpm
                self.send_osc()

    async def _cache_services_linux(self):
        proc = await asyncio.create_subprocess_exec(
            "bluetoothctl", "connect", self.address,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await asyncio.wait_for(proc.wait(), timeout=8)
        except asyncio.TimeoutError:
            proc.kill()

    async def run_once(self):
        if IS_LINUX:
            self.log_msg("🔄 Caching…")
            await self._cache_services_linux()
        self.log_msg(f"📡 Connecting to {self.address}…")
        kwargs = {"timeout": 20.0}
        if IS_LINUX:
            kwargs["dangerous_use_bleak_cache"] = True
        async with BleakClient(self.address, **kwargs) as client:
            self._client = client
            self.log_msg("  ✅ Connected")
            try:
                batt = await client.read_gatt_char(BLE_BATTERY)
                self.battery = batt[0]
                self.log_msg(f"  🔋 {self.battery}%")
            except Exception:
                pass
            await client.start_notify(BLE_FEE3_IN, self.on_fee3)
            await client.write_gatt_char(BLE_FEE2_OUT, make_packet(CMD_START_DYNAMIC_HR, bytes([0x00])), response=False)
            await asyncio.sleep(0.2)
            await client.write_gatt_char(BLE_FEE2_OUT, make_packet(CMD_SET_HR_INTERVAL, bytes([0x01])), response=False)
            await asyncio.sleep(0.2)
            await client.write_gatt_char(BLE_FEE2_OUT, make_packet(CMD_TRIGGER_HR, bytes([0x00])), response=False)
            await asyncio.sleep(0.2)
            await client.write_gatt_char(BLE_FEE2_OUT, make_packet(CMD_SET_QUICK_VIEW, bytes([0x01])), response=False)
            await client.start_notify(BLE_HR_MEASURE, self.on_hr)
            self.log_msg("  ✅ Streaming!")
            last_notify = asyncio.get_event_loop().time()
            last_keepalive = last_notify
            poll_count = 0
            while self.running and client.is_connected:
                await asyncio.sleep(3)
                now = asyncio.get_event_loop().time()
                if now - last_notify >= 2:
                    await client.write_gatt_char(BLE_FEE2_OUT, make_packet(CMD_TRIGGER_HR, bytes([0x00])), response=False)
                if now - last_keepalive >= 30:
                    await client.write_gatt_char(BLE_FEE2_OUT, make_packet(CMD_START_DYNAMIC_HR, bytes([0x00])), response=False)
                    last_keepalive = now
                poll_count += 1
                if poll_count >= 3:
                    await client.write_gatt_char(BLE_FEE2_OUT, make_packet(47, bytes([])), response=False)
                    poll_count = 0
                if self.media_enabled and HAS_MEDIA and poll_count % 1 == 0:
                    media = await get_media_info()
                    if media:
                        self.song = media.get("title", "")
                        self.artist = media.get("artist", "")
            self.log_msg("  ⚠️ Disconnected")

    async def run_forever(self):
        while self.running:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.log_msg(f"  ⚠️ {e}")
            if self.running:
                self.log_msg("  🔄 Reconnecting in 5s…")
                await asyncio.sleep(5)

    def start(self):
        self.running = True
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def _run_loop(self):
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self.run_forever())
        except Exception as e:
            self.log_msg(f"  ❌ {e}")

    def stop(self):
        self.running = False
        if self._client and self._client.is_connected:
            asyncio.run_coroutine_threadsafe(self._client.disconnect(), self._loop)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self.log_msg("  ⏹️ Stopped")


# ── Settings Persistence ────────────────────────────────────
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bridge_config.json")

def load_config():
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except:
        return {}

def save_config(data):
    with open(CONFIG_PATH, "w") as f:
        json.dump(data, f)


# ── GUI ─────────────────────────────────────────────────────
class BridgeGUI:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("C20 HR Bridge")
        self.root.resizable(False, False)
        self.bridge: Optional[HRBridge] = None
        cfg = load_config()
        self._build_ui(cfg)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self, cfg):
        main = ttk.Frame(self.root, padding=12)
        main.grid()

        # Address
        ttk.Label(main, text="Watch Address:").grid(row=0, column=0, sticky="w")
        self.addr_var = tk.StringVar(value=cfg.get("address", DEFAULT_ADDR))
        ttk.Entry(main, textvariable=self.addr_var, width=20).grid(row=0, column=1, padx=6, pady=3, sticky="ew")

        # Template
        ttk.Label(main, text="Chatbox Format:").grid(row=1, column=0, sticky="w")
        self.template_var = tk.StringVar(value=cfg.get("template", DEFAULT_TEMPLATE))
        ttk.Entry(main, textvariable=self.template_var, width=40).grid(row=1, column=1, padx=6, pady=3, sticky="ew")
        ttk.Label(main, text="Placeholders: {bpm} {battery} {song} {artist} {title}", font=("", 8)).grid(row=2, column=1, sticky="w")

        # Media toggle
        self.media_var = tk.BooleanVar(value=cfg.get("media", False))
        ttk.Checkbutton(main, text="Show media info ({song}/{artist})", variable=self.media_var).grid(row=3, column=1, sticky="w", pady=3)
        if not HAS_MEDIA:
            ttk.Label(main, text="  (Windows only)", font=("", 8), foreground="gray").grid(row=3, column=1, sticky="e")

        # Start/stop
        btn_frame = ttk.Frame(main)
        btn_frame.grid(row=4, column=0, columnspan=2, pady=8)
        self.start_btn = ttk.Button(btn_frame, text="▶ Start", command=self._toggle)
        self.start_btn.pack(side="left", padx=4)

        # Log
        self.log_area = scrolledtext.ScrolledText(main, width=60, height=16, font=("Consolas", 9))
        self.log_area.grid(row=5, column=0, columnspan=2)
        self.log_area.insert("end", "Ready. Press Start to begin.\n")
        self.log_area.see("end")

        # Placeholders hint
        ttk.Label(main, text="Tip: Templates support {bpm}, {battery}, {song}, {artist}, {title}", font=("", 8), foreground="gray").grid(row=6, column=0, columnspan=2, pady=2)

    def log(self, msg: str):
        self.root.after(0, self._do_log, msg)

    def _do_log(self, msg: str):
        self.log_area.insert("end", msg + "\n")
        self.log_area.see("end")

    def _toggle(self):
        if self.bridge and self.bridge.running:
            self.bridge.stop()
            self.start_btn.config(text="▶ Start")
            save_config({
                "address": self.addr_var.get(),
                "template": self.template_var.get(),
                "media": self.media_var.get(),
            })
        else:
            self.log_area.delete("1.0", "end")
            self.bridge = HRBridge(
                address=self.addr_var.get(),
                template=self.template_var.get(),
                log_cb=self.log,
                media_enabled=self.media_var.get(),
            )
            self.bridge.start()
            self.start_btn.config(text="■ Stop")

    def _on_close(self):
        if self.bridge and self.bridge.running:
            self.bridge.stop()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    BridgeGUI().run()
