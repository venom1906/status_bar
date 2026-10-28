"""
Windows status bar: CPU, GPU, RAM, VRAM, disk, network, volume, media, weather, timer, clock.

Setup:   pip install psutil pywin32 pycaw winrt-runtime winrt-Windows.Media.Control
                     winrt-Windows.Foundation winrt-Windows.Foundation.Collections
                     winrt-Windows.Storage.Streams
Run:     pythonw statusbar.py      (pythonw = no console window)
Config:  statusbar.json next to this file (created on first run; saving it reloads the bar)
Menu:    right-click the bar
Mouse:   hover = details, click CPU/RAM/GPU = Task Manager, click clock = calendar,
         click weather = refresh, scroll on volume = change, click volume = mute,
         click timer = start / pause
"""
import asyncio
import calendar
import copy
import ctypes
import datetime
import json
import math
import os
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.parse
import urllib.request
from ctypes import wintypes

import psutil

# ---------------- config: statusbar.json (next to this script / the exe) ----------------
DEFAULTS = {
    "city": "Ankara",
    "units": "celsius",                 # "celsius" or "fahrenheit"
    "position": "top",                  # "top" or "bottom"
    "bar_height": 28,                   # pixels at 100% display scaling
    "use_appbar": True,                 # reserve screen space so maximized windows don't overlap the bar
    "hide_on_fullscreen": True,         # hide the bar while a fullscreen app is in front
    "no_focus_steal": True,             # clicking the bar doesn't take keyboard focus from your app
    "auto_reload": True,                # restart the bar automatically when this file is saved
    "weather_refresh_minutes": 15,
    "font": "Consolas",
    "font_size": 10,
    "media_chars": 40,                  # width of the now-playing text
    "title_chars": 30,                  # width of the active-window title
    "time_format": "%a %d %b  %H:%M:%S",
    "volume_step": 2,                   # percent per scroll notch
    "disk_drive": "C:",
    "timer_minutes": 25,                # default countdown when you click the timer
    "week_starts_monday": True,         # calendar popup
    "show": {
        "cpu": True,
        "gpu": True,
        "ram": True,
        "vram": True,
        "disk": False,
        "window_title": False,
        "network": True,
        "volume": True,
        "media": True,
        "weather": True,
        "timer": True,
        "time": True,
    },
    "colors": {
        "background": "#1e1e2e",
        "cpu": "#89b4fa",
        "gpu": "#a6e3a1",
        "ram": "#f9e2af",
        "vram": "#fab387",
        "disk": "#f5c2e7",
        "title": "#b4befe",
        "down": "#94e2d5",
        "up": "#f38ba8",
        "volume": "#74c7ec",
        "media": "#cba6f7",
        "weather": "#89dceb",
        "timer": "#eba0ac",
        "time": "#cdd6f4",
        "dim": "#6c7086",
        "tooltip_bg": "#313244",
        "tooltip_fg": "#cdd6f4",
    },
}


def app_dir():
    if getattr(sys, "frozen", False):          # running as a PyInstaller exe
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


CONFIG_PATH = os.path.join(app_dir(), "statusbar.json")


def _merge(base, user, problems, path=""):
    """Copy valid values from the user's file into base; note anything that looks wrong."""
    for k, v in user.items():
        name = path + k
        if k not in base:
            problems.append(f"unknown setting: {name}")
            continue
        b = base[k]
        if isinstance(b, dict):
            if isinstance(v, dict):
                _merge(b, v, problems, name + ".")
            else:
                problems.append(f"{name}: expected an object {{ }}")
        elif isinstance(b, bool):
            if isinstance(v, bool):
                base[k] = v
            else:
                problems.append(f"{name}: expected true or false")
        elif isinstance(b, (int, float)):
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                base[k] = v
            else:
                problems.append(f"{name}: expected a number")
        elif isinstance(v, str):
            base[k] = v
        else:
            problems.append(f"{name}: expected text in quotes")


def _validate(cfg, problems):
    d = DEFAULTS
    if cfg["units"] not in ("celsius", "fahrenheit"):
        problems.append('units: use "celsius" or "fahrenheit"')
        cfg["units"] = d["units"]
    if cfg["position"] not in ("top", "bottom"):
        problems.append('position: use "top" or "bottom"')
        cfg["position"] = d["position"]
    for k, v in cfg["colors"].items():
        if not re.fullmatch(r"#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})", v):
            problems.append(f'colors.{k}: use a hex color like "#89b4fa"')
            cfg["colors"][k] = d["colors"][k]
    try:
        time.strftime(cfg["time_format"])
    except Exception:
        problems.append("time_format: not a valid time format")
        cfg["time_format"] = d["time_format"]
    # quietly keep numbers in a sane range
    cfg["bar_height"] = max(16, min(100, int(cfg["bar_height"])))
    cfg["font_size"] = max(6, min(30, int(cfg["font_size"])))
    cfg["media_chars"] = max(10, min(120, int(cfg["media_chars"])))
    cfg["title_chars"] = max(10, min(120, int(cfg["title_chars"])))
    cfg["volume_step"] = max(1, min(20, int(cfg["volume_step"])))
    cfg["timer_minutes"] = max(1, int(cfg["timer_minutes"]))
    cfg["weather_refresh_minutes"] = max(1, cfg["weather_refresh_minutes"])


def _warn(problems):
    shown = "\n".join("  - " + p for p in problems[:8])
    more = "\n  ..." if len(problems) > 8 else ""
    msg = f"Problems in statusbar.json:\n\n{shown}{more}\n\nThose settings use their defaults."
    try:
        ctypes.windll.user32.MessageBoxW(None, msg, "StatusBar", 0x30)
    except Exception:
        pass


def load_config():
    cfg = copy.deepcopy(DEFAULTS)
    problems = []
    try:
        with open(CONFIG_PATH, encoding="utf-8-sig") as f:    # utf-8-sig tolerates a Notepad BOM
            user = json.load(f)
        if isinstance(user, dict):
            _merge(cfg, user, problems)
        else:
            problems.append("the file must contain one { } object")
    except FileNotFoundError:
        try:                                                  # first run: write the defaults
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(DEFAULTS, f, indent=2, ensure_ascii=False)
        except OSError:
            pass
    except json.JSONDecodeError as e:
        problems.append(f"invalid JSON: {e.msg} (line {e.lineno})")
    except (OSError, UnicodeDecodeError) as e:
        problems.append(f"could not read the file: {e}")
    _validate(cfg, problems)
    if problems:
        _warn(problems)
    return cfg


def config_mtime():
    try:
        return os.path.getmtime(CONFIG_PATH)
    except OSError:
        return None


CFG = load_config()
CITY = CFG["city"]
UNITS = CFG["units"]
POSITION = CFG["position"]
BAR_HEIGHT = CFG["bar_height"]
USE_APPBAR = CFG["use_appbar"]
HIDE_ON_FULLSCREEN = CFG["hide_on_fullscreen"]
NO_FOCUS_STEAL = CFG["no_focus_steal"]
AUTO_RELOAD = CFG["auto_reload"]
WEATHER_REFRESH_S = int(CFG["weather_refresh_minutes"] * 60)
FONT = (CFG["font"], CFG["font_size"])
MEDIA_CHARS = CFG["media_chars"]
TITLE_CHARS = CFG["title_chars"]
TIME_FORMAT = CFG["time_format"]
VOLUME_STEP = CFG["volume_step"] / 100
DISK_DRIVE = CFG["disk_drive"].rstrip("\\/")
TIMER_MINUTES = CFG["timer_minutes"]
WEEK_MONDAY = CFG["week_starts_monday"]
SHOW = CFG["show"]
C = CFG["colors"]
BG = C["background"]
TIP_BG, TIP_FG = C["tooltip_bg"], C["tooltip_fg"]
# ----------------------------------------


# ---------------- small formatting helpers ----------------
def fmt_rate(bps):
    for unit in ("B/s", "KB/s", "MB/s"):
        if bps < 1024:
            return f"{bps:6.1f} {unit:>4}"
        bps /= 1024
    return f"{bps:6.1f} GB/s"


def fmt_bytes(b):
    return f"{b / 2**30:5.1f} GB" if b >= 2**30 else f"{b / 2**20:5.0f} MB"


# ---------------- GPU / VRAM (Windows performance counters: AMD, NVIDIA, Intel) ----------------
gpu = {"util": None, "used": None, "total": None, "top_load": [], "top_vram": []}
_names = {}


def _proc_name(pid):
    if pid not in _names:
        try:
            _names[pid] = psutil.Process(pid).name()
        except Exception:
            _names[pid] = f"pid {pid}"
    return _names[pid]


def _total_vram_bytes():
    """Largest adapter memory size from the registry (the discrete GPU), or None."""
    import winreg

    path = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
    best = 0
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as k:
            i = 0
            while True:
                try:
                    sub = winreg.EnumKey(k, i)
                except OSError:
                    break
                i += 1
                if not sub.isdigit():
                    continue
                try:
                    with winreg.OpenKey(k, sub) as sk:
                        val, _ = winreg.QueryValueEx(sk, "HardwareInformation.qwMemorySize")
                except OSError:
                    continue
                if isinstance(val, bytes):
                    val = int.from_bytes(val[:8], "little")
                best = max(best, int(val))
    except OSError:
        pass
    return best or None


def gpu_worker():
    """Samples GPU load and VRAM once a second in a background thread."""
    try:
        import win32pdh
    except Exception:
        return
    flags = win32pdh.PDH_FMT_DOUBLE | getattr(win32pdh, "PDH_FMT_NOCAP100", 0)

    def add(query, path):
        try:
            return win32pdh.AddEnglishCounter(query, path)   # works on any Windows language
        except Exception:
            return win32pdh.AddCounter(query, path)

    try:
        query = win32pdh.OpenQuery()
        eng = add(query, r"\GPU Engine(*)\Utilization Percentage")
        mem = add(query, r"\GPU Adapter Memory(*)\Dedicated Usage")
        try:
            pmem = add(query, r"\GPU Process Memory(*)\Dedicated Usage")
        except Exception:
            pmem = None
        win32pdh.CollectQueryData(query)
    except Exception:
        return

    total = _total_vram_bytes()
    while True:
        time.sleep(1)
        try:
            win32pdh.CollectQueryData(query)
            mem_vals = win32pdh.GetFormattedCounterArray(mem, flags)
            eng_vals = win32pdh.GetFormattedCounterArray(eng, flags)
        except Exception:
            continue

        # adapter id (luid) -> dedicated VRAM in use
        used_by_luid = {}
        for name, v in mem_vals.items():
            luid = name.lower().split("_phys")[0].replace("luid_", "")
            used_by_luid[luid] = used_by_luid.get(luid, 0) + v
        if not used_by_luid:
            continue
        # assume the discrete GPU is the adapter with the most VRAM in use
        luid = max(used_by_luid, key=used_by_luid.get)

        # like Task Manager: sum each engine type (3D, Compute, Copy...), show the busiest
        busy = {}
        per_pid = {}
        for name, v in eng_vals.items():
            n = name.lower()
            if f"luid_{luid}_" not in n:
                continue
            kind = n.rsplit("engtype_", 1)[-1]
            busy[kind] = busy.get(kind, 0) + v
            m = re.match(r"pid_(\d+)_", n)
            if m:
                d = per_pid.setdefault(int(m.group(1)), {})
                d[kind] = d.get(kind, 0) + v

        # per-process GPU load, merged by program name (browsers have many processes)
        agg = {}
        for pid, d in sorted(per_pid.items(), key=lambda kv: max(kv[1].values()), reverse=True)[:8]:
            v = max(d.values())
            if v >= 0.5:
                nm = _proc_name(pid)
                agg[nm] = agg.get(nm, 0) + v
        gpu["top_load"] = sorted(agg.items(), key=lambda kv: kv[1], reverse=True)[:5]

        # per-process VRAM
        if pmem is not None:
            try:
                pm = win32pdh.GetFormattedCounterArray(pmem, flags)
            except Exception:
                pm = {}
            vr = {}
            for name, v in pm.items():
                n = name.lower()
                m = re.match(r"pid_(\d+)_", n)
                if m and f"luid_{luid}_" in n:
                    vr[int(m.group(1))] = vr.get(int(m.group(1)), 0) + v
            agg = {}
            for pid, v in sorted(vr.items(), key=lambda kv: kv[1], reverse=True)[:8]:
                if v >= 50 * 2**20:
                    nm = _proc_name(pid)
                    agg[nm] = agg.get(nm, 0) + v
            gpu["top_vram"] = sorted(agg.items(), key=lambda kv: kv[1], reverse=True)[:5]

        gpu["util"] = min(max(busy.values(), default=0), 100)
        gpu["used"] = used_by_luid[luid] / 2**30
        gpu["total"] = total / 2**30 if total else None


# ---------------- volume (pycaw / Windows Core Audio) ----------------
class Volume:
    """System master volume. Create and use it from the same thread (COM)."""

    def __init__(self):
        self.ep, self.t, self.ok = None, 0.0, True
        try:
            from pycaw.pycaw import AudioUtilities  # noqa: F401
        except Exception:
            self.ok = False

    def _endpoint(self):
        from comtypes import CLSCTX_ALL
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

        dev = AudioUtilities.GetSpeakers()
        ep = getattr(dev, "EndpointVolume", None)       # newer pycaw (2024+)
        if ep is not None:
            return ep
        iface = dev.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)   # older pycaw
        return iface.QueryInterface(IAudioEndpointVolume)

    def get(self):
        """(level 0..1, muted) or None."""
        if not self.ok:
            return None
        if self.ep is not None and time.time() - self.t > 10:
            self.ep = None                              # re-pick the default output now and then
        for _ in range(2):
            try:
                if self.ep is None:
                    self.ep, self.t = self._endpoint(), time.time()
                return self.ep.GetMasterVolumeLevelScalar(), bool(self.ep.GetMute())
            except Exception:
                self.ep = None
        return None

    def change(self, delta):
        cur = self.get()
        if cur is None:
            return
        try:
            self.ep.SetMasterVolumeLevelScalar(max(0.0, min(1.0, cur[0] + delta)), None)
            if cur[1] and delta > 0:
                self.ep.SetMute(0, None)                # turning it up un-mutes, like Windows
        except Exception:
            self.ep = None

    def toggle_mute(self):
        cur = self.get()
        if cur is None:
            return
        try:
            self.ep.SetMute(0 if cur[1] else 1, None)
        except Exception:
            self.ep = None


# ---------------- media ("now playing" from Spotify, browsers, etc.) ----------------
media = {"text": "", "playing": False, "loop": None, "manager": None}


def media_worker():
    try:
        try:
            from winrt.windows.media.control import (
                GlobalSystemMediaTransportControlsSessionManager as Manager,
                GlobalSystemMediaTransportControlsSessionPlaybackStatus as Status,
            )
        except ImportError:   # older "winsdk" package
            from winsdk.windows.media.control import (
                GlobalSystemMediaTransportControlsSessionManager as Manager,
                GlobalSystemMediaTransportControlsSessionPlaybackStatus as Status,
            )
    except ImportError:
        media["text"] = "media: package missing"
        return

    async def run():
        manager = await Manager.request_async()
        media["manager"] = manager
        while True:
            try:
                session = manager.get_current_session()
                if session is None:
                    media["text"], media["playing"] = "", False
                else:
                    props = await session.try_get_media_properties_async()
                    status = session.get_playback_info().playback_status
                    media["text"] = " \u2013 ".join(p for p in (props.artist, props.title) if p)
                    media["playing"] = status == Status.PLAYING
            except Exception:
                media["text"], media["playing"] = "", False
            await asyncio.sleep(1)

    loop = asyncio.new_event_loop()
    media["loop"] = loop
    try:
        loop.run_until_complete(run())
    except Exception:
        media["text"] = "media n/a"


def media_cmd(action):
    """action: "prev", "toggle" or "next". Runs on the media thread's event loop."""
    loop, manager = media["loop"], media["manager"]
    if loop is None or manager is None:
        return

    async def go():
        session = manager.get_current_session()
        if session is None:
            return
        if action == "toggle":
            await session.try_toggle_play_pause_async()
        elif action == "next":
            await session.try_skip_next_async()
        else:
            await session.try_skip_previous_async()

    asyncio.run_coroutine_threadsafe(go(), loop)


# ---------------- weather (Open-Meteo, no API key) ----------------
weather = {"text": "weather ...", "detail": ""}
weather_refresh = threading.Event()      # set it to refresh right now


def wmo(code):
    if code == 0:
        return "☀ Clear"
    if code in (1, 2):
        return "⛅ Partly cloudy"
    if code == 3:
        return "☁ Overcast"
    if code in (45, 48):
        return "≋ Fog"
    if 51 <= code <= 67 or 80 <= code <= 82:
        return "☂ Rain"
    if 71 <= code <= 77 or code in (85, 86):
        return "❄ Snow"
    if code >= 95:
        return "⚡ Storm"
    return "?"


def fetch_json(url):
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.load(r)


def weather_worker():
    coords = None
    while True:
        ok = False
        try:
            if coords is None:
                q = urllib.parse.urlencode({"name": CITY, "count": 1})
                g = fetch_json(f"https://geocoding-api.open-meteo.com/v1/search?{q}")
                coords = (g["results"][0]["latitude"], g["results"][0]["longitude"])
            q = urllib.parse.urlencode({
                "latitude": coords[0],
                "longitude": coords[1],
                "current": "temperature_2m,apparent_temperature,relative_humidity_2m,"
                           "wind_speed_10m,weather_code",
                "daily": "weather_code,temperature_2m_max,temperature_2m_min,"
                         "precipitation_probability_max",
                "forecast_days": 4,
                "timezone": "auto",
                "temperature_unit": UNITS,
            })
            data = fetch_json(f"https://api.open-meteo.com/v1/forecast?{q}")
            cur = data["current"]
            deg = "C" if UNITS == "celsius" else "F"
            weather["text"] = f"{CITY} {wmo(cur['weather_code'])} {cur['temperature_2m']:.0f}°{deg}"
            ok = True
            try:                                    # details for the hover tip; optional
                day = data["daily"]
                lines = [
                    CITY,
                    f"Feels like {cur['apparent_temperature']:.0f}°{deg} · "
                    f"humidity {cur['relative_humidity_2m']:.0f}% · wind {cur['wind_speed_10m']:.0f} km/h",
                    "",
                ]
                for i, iso in enumerate(day["time"]):
                    d = datetime.date.fromisoformat(iso)
                    name = "Today" if i == 0 else "Tomorrow" if i == 1 else d.strftime("%a %d")
                    p = day["precipitation_probability_max"][i]
                    rain = f"  {p:.0f}% rain" if p is not None else ""
                    lines.append(
                        f"{name:<9} {wmo(day['weather_code'][i]):<16} "
                        f"{day['temperature_2m_min'][i]:.0f}° / {day['temperature_2m_max'][i]:.0f}°{rain}"
                    )
                weather["detail"] = "\n".join(lines + ["", "Click: refresh"])
            except Exception:
                weather["detail"] = ""
        except Exception:
            weather["text"] = "weather n/a"
        weather_refresh.wait(WEATHER_REFRESH_S if ok else 60)
        weather_refresh.clear()


# ---------------- timer (countdown or stopwatch) ----------------
class Timer:
    def __init__(self):
        self.reset()

    def reset(self):
        self.mode, self.running, self.done = None, False, False   # mode: None, "down" or "up"
        self.base, self.mark, self.done_at = 0.0, 0.0, 0.0

    def value(self):
        if not self.running:
            return self.base
        run = time.time() - self.mark
        return max(self.base - run, 0.0) if self.mode == "down" else self.base + run

    def start_down(self, minutes):
        self.mode, self.base, self.mark = "down", minutes * 60.0, time.time()
        self.running, self.done = True, False

    def start_up(self):
        self.mode, self.base, self.mark = "up", 0.0, time.time()
        self.running, self.done = True, False

    def pause_resume(self):
        if self.mode is None or self.done:
            return
        if self.running:
            self.base, self.running = self.value(), False
        else:
            self.mark, self.running = time.time(), True

    def click(self, default_minutes):
        if self.mode is None:
            self.start_down(default_minutes)
        elif self.done:
            self.reset()
        else:
            self.pause_resume()

    def tick(self):
        now = time.time()
        if self.mode == "down" and self.running and self.value() <= 0:
            self.base, self.running, self.done, self.done_at = 0.0, False, True, now
        if self.done:
            if now - self.done_at < 3:
                try:
                    ctypes.windll.user32.MessageBeep(0x30)
                except Exception:
                    pass
            elif now - self.done_at > 10:
                self.reset()

    def text(self):
        if self.done:
            return "TMR done!"
        if self.mode is None:
            return "TMR --:--"
        v = self.value()
        s = math.ceil(v) if self.mode == "down" else int(v)
        h, rem = divmod(s, 3600)
        m, sec = divmod(rem, 60)
        clock = f"{h}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"
        return f"TMR {clock}" + ("" if self.running else " \u23f8")


# ---------------- hover-tip texts ----------------
def top_procs(key, n=5):
    """Top programs by CPU % ("cpu") or memory ("mem"), merged by name."""
    ncpu = psutil.cpu_count() or 1
    agg = {}
    for p in psutil.process_iter(["name"]):
        if p.pid == 0:
            continue
        try:
            v = p.cpu_percent(None) / ncpu if key == "cpu" else p.memory_info().rss
        except (psutil.Error, OSError):
            continue
        nm = p.info.get("name") or "?"
        agg[nm] = agg.get(nm, 0) + v
    return sorted(agg.items(), key=lambda kv: kv[1], reverse=True)[:n]


def cpu_tip():
    loads = psutil.cpu_percent(percpu=True)
    rows = ["  ".join(f"{j:>2}:{loads[j]:3.0f}%" for j in range(i, min(i + 4, len(loads))))
            for i in range(0, len(loads), 4)]
    procs = [f"{n[:22]:<22} {v:5.1f}%" for n, v in top_procs("cpu")]
    return "\n".join(["CPU per core", *rows, "", "Top processes", *procs, "", "Click: Task Manager"])


def ram_tip():
    vm, sm = psutil.virtual_memory(), psutil.swap_memory()
    procs = [f"{n[:22]:<22} {fmt_bytes(v)}" for n, v in top_procs("mem")]
    return "\n".join([
        f"Memory    {fmt_bytes(vm.used)} / {fmt_bytes(vm.total)}  ({vm.percent:.0f}%)",
        f"Free      {fmt_bytes(vm.available)}",
        f"Page file {fmt_bytes(sm.used)} / {fmt_bytes(sm.total)}",
        "", "Top processes", *procs, "", "Click: Task Manager",
    ])


def gpu_tip():
    if gpu["util"] is None:
        return "GPU counters unavailable"
    rows = [f"{n[:22]:<22} {v:5.1f}%" for n, v in gpu["top_load"]] or ["(no GPU activity)"]
    return "\n".join([f"GPU load {gpu['util']:.0f}%", "", "Top processes", *rows, "", "Click: Task Manager"])


def vram_tip():
    if gpu["util"] is None:
        return "GPU counters unavailable"
    total = f" / {gpu['total']:.1f} GB" if gpu["total"] else ""
    rows = [f"{n[:22]:<22} {fmt_bytes(v)}" for n, v in gpu["top_vram"]] or ["(nothing above 50 MB)"]
    return "\n".join([f"VRAM {gpu['used']:.1f} GB{total}", "", "Top processes", *rows,
                      "", "Click: Task Manager"])


def disk_tip():
    lines = ["Drives"]
    for part in psutil.disk_partitions(all=False):
        if "cdrom" in part.opts or not part.fstype:
            continue
        try:
            u = psutil.disk_usage(part.mountpoint)
        except OSError:
            continue
        lines.append(f"{part.mountpoint:<4} {fmt_bytes(u.used)} / {fmt_bytes(u.total)}  {u.percent:3.0f}%")
    return "\n".join(lines)


def net_tip():
    n = psutil.net_io_counters()
    return (f"Since boot\nDownloaded {fmt_bytes(n.bytes_recv)}\nUploaded   {fmt_bytes(n.bytes_sent)}"
            "\n\nClick: network settings")


def time_tip():
    week = datetime.date.today().isocalendar()[1]
    return time.strftime("%A, %d %B %Y") + f"\nWeek {week}\n\nClick: calendar"


# ---------------- AppBar (reserve screen space like the taskbar) ----------------
ABM_NEW, ABM_REMOVE, ABM_QUERYPOS, ABM_SETPOS = 0, 1, 2, 3
ABE_TOP, ABE_BOTTOM = 1, 3


class APPBARDATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uCallbackMessage", wintypes.UINT),
        ("uEdge", wintypes.UINT),
        ("rc", wintypes.RECT),
        ("lParam", wintypes.LPARAM),
    ]


def _shell32():
    fn = ctypes.windll.shell32.SHAppBarMessage
    fn.argtypes = [wintypes.DWORD, ctypes.POINTER(APPBARDATA)]
    fn.restype = ctypes.c_size_t
    return fn


def register_appbar(hwnd, edge, screen_w, screen_h, height):
    """Registers the window as an AppBar. Returns (abd, x, y, width)."""
    send = _shell32()
    abd = APPBARDATA()
    abd.cbSize = ctypes.sizeof(APPBARDATA)
    abd.hWnd = hwnd
    send(ABM_NEW, ctypes.byref(abd))

    abd.uEdge = edge
    abd.rc.left, abd.rc.right = 0, screen_w
    if edge == ABE_TOP:
        abd.rc.top, abd.rc.bottom = 0, height
    else:
        abd.rc.top, abd.rc.bottom = screen_h - height, screen_h
    send(ABM_QUERYPOS, ctypes.byref(abd))   # let Windows adjust for other bars
    if edge == ABE_TOP:
        abd.rc.bottom = abd.rc.top + height
    else:
        abd.rc.top = abd.rc.bottom - height
    send(ABM_SETPOS, ctypes.byref(abd))
    return abd, abd.rc.left, abd.rc.top, abd.rc.right - abd.rc.left


def unregister_appbar(abd):
    _shell32()(ABM_REMOVE, ctypes.byref(abd))


# ---------------- Windows helpers: window handles, focus, fullscreen, autostart ----------------
SHELL_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"}
SW_HIDE, SW_SHOWNOACTIVATE = 0, 4
GWL_EXSTYLE = -20
WS_EX_NOACTIVATE, WS_EX_TOOLWINDOW = 0x08000000, 0x00000080


def _user32():
    u = ctypes.windll.user32
    u.GetForegroundWindow.restype = wintypes.HWND
    u.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    u.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    u.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    u.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    u.GetParent.argtypes = [wintypes.HWND]
    u.GetParent.restype = wintypes.HWND
    u.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    u.GetWindowLongW.restype = ctypes.c_long
    u.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
    u.SetWindowLongW.restype = ctypes.c_long
    u.SetForegroundWindow.argtypes = [wintypes.HWND]
    return u


def toplevel_hwnd(u, widget):
    return u.GetParent(widget.winfo_id()) or widget.winfo_id()


def set_noactivate(u, widget):
    """Clicking this window no longer takes keyboard focus away from the app you're using."""
    try:
        h = toplevel_hwnd(u, widget)
        style = u.GetWindowLongW(h, GWL_EXSTYLE)
        u.SetWindowLongW(h, GWL_EXSTYLE, style | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW)
    except Exception:
        pass


def fullscreen_in_front(u, screen_w, screen_h):
    """True if the foreground window covers the whole primary screen (game, YouTube/F11 fullscreen)."""
    hwnd = u.GetForegroundWindow()
    if not hwnd:
        return False
    cls = ctypes.create_unicode_buffer(64)
    u.GetClassNameW(hwnd, cls, 64)
    if cls.value in SHELL_CLASSES:     # the desktop itself covers the screen too
        return False
    r = wintypes.RECT()
    if not u.GetWindowRect(hwnd, ctypes.byref(r)):
        return False
    return r.left <= 0 and r.top <= 0 and r.right >= screen_w and r.bottom >= screen_h


def foreground_title(u, own_hwnd):
    """Title of the window in front, or None if it's the bar itself or has no title."""
    hwnd = u.GetForegroundWindow()
    if not hwnd or hwnd == own_hwnd:
        return None
    buf = ctypes.create_unicode_buffer(256)
    u.GetWindowTextW(hwnd, buf, 256)
    return buf.value or None


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "StatusBar"


def launch_command():
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    exe = sys.executable
    pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")     # no console window at login
    if os.path.exists(pyw):
        exe = pyw
    return f'"{exe}" "{os.path.abspath(__file__)}"'


def autostart_enabled():
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, RUN_NAME)
            return True
    except OSError:
        return False


def set_autostart(on):
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, RUN_NAME, 0, winreg.REG_SZ, launch_command())
        else:
            try:
                winreg.DeleteValue(k, RUN_NAME)
            except FileNotFoundError:
                pass


# ---------------- UI ----------------
def main():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)   # crisp text, real pixel sizes
    except Exception:
        pass

    root = tk.Tk()
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.configure(bg=BG)

    scale = root.winfo_fpixels("1i") / 96
    height = int(BAR_HEIGHT * scale)
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    x, y, w = 0, (0 if POSITION == "top" else sh - height), sw
    root.geometry(f"{w}x{height}+{x}+{y}")
    root.update()

    user32 = _user32()
    hwnd = toplevel_hwnd(user32, root)
    if NO_FOCUS_STEAL:
        set_noactivate(user32, root)

    appbar = None
    if USE_APPBAR:
        try:
            edge = ABE_TOP if POSITION == "top" else ABE_BOTTOM
            appbar, x, y, w = register_appbar(hwnd, edge, sw, sh, height)
            root.geometry(f"{w}x{height}+{x}+{y}")
        except Exception:
            appbar = None

    volume = Volume() if SHOW["volume"] else None
    timer = Timer()

    # ----- small actions -----
    def open_taskmgr(_e=None):
        try:
            os.startfile("taskmgr")
        except Exception:
            pass

    def open_network(_e=None):
        try:
            os.startfile("ms-settings:network-status")
        except Exception:
            pass

    # ----- popups (tooltips, calendar) -----
    def place_popup(win, anchor, align="left"):
        """Put a popup just below the bar (or above it when the bar is at the bottom)."""
        win.update_idletasks()
        pw, ph = win.winfo_reqwidth(), win.winfo_reqheight()
        ax, aw = anchor.winfo_rootx(), anchor.winfo_width()
        px = ax if align == "left" else ax + aw - pw
        px = max(0, min(px, sw - pw))
        bar_top, bar_h = root.winfo_rooty(), root.winfo_height()
        py = bar_top + bar_h + 4 if POSITION == "top" else bar_top - ph - 4
        win.geometry(f"+{px}+{max(0, py)}")

    def new_popup():
        win = tk.Toplevel(root)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.configure(bg=TIP_BG, highlightthickness=1, highlightbackground=C["dim"])
        win.withdraw()
        return win

    def prime_popup(win):
        """Map the popup once, off-screen, so its no-focus style is in place before it is ever shown."""
        win.geometry("+-3000+-3000")
        win.deiconify()
        win.update()
        set_noactivate(user32, win)
        win.withdraw()

    tip_win = new_popup()
    tip_lbl = tk.Label(tip_win, justify="left", anchor="w", bg=TIP_BG, fg=TIP_FG,
                       font=FONT, padx=10, pady=6)
    tip_lbl.pack()
    prime_popup(tip_win)
    cal_win = new_popup()
    prime_popup(cal_win)

    # one shared tooltip window; whichever widget the mouse is over owns it
    tip = {"owner": None, "fn": None, "align": "left", "show": None, "tick": None}

    def tip_hide(widget=None):
        if widget is not None and tip["owner"] is not widget:
            return
        for k in ("show", "tick"):
            if tip[k] is not None:
                root.after_cancel(tip[k])
                tip[k] = None
        tip["owner"] = None
        tip_win.withdraw()

    def tip_text():
        try:
            return tip["fn"]() if tip["fn"] else ""
        except Exception:
            return ""

    def tip_refresh():
        tip["tick"] = None
        if tip["owner"] is None:
            return
        t = tip_text()
        if not t:
            tip_hide(tip["owner"])
            return
        tip_lbl.config(text=t)
        place_popup(tip_win, tip["owner"], tip["align"])
        tip["tick"] = root.after(1000, tip_refresh)      # live-update while hovered

    def tip_show():
        tip["show"] = None
        if tip["owner"] is None:
            return
        t = tip_text()
        if not t:
            return
        tip_lbl.config(text=t)
        place_popup(tip_win, tip["owner"], tip["align"])
        tip_win.deiconify()
        tip["tick"] = root.after(1000, tip_refresh)

    def add_tip(widget, text_fn, align="left"):
        def enter(_e):
            tip_hide()
            tip["owner"], tip["fn"], tip["align"] = widget, text_fn, align
            tip["show"] = root.after(350, tip_show)

        widget.bind("<Enter>", enter, add="+")
        widget.bind("<Leave>", lambda e: tip_hide(widget), add="+")
        widget.bind("<ButtonPress>", lambda e: tip_hide(widget), add="+")

    # ----- widgets -----
    def label(key, side, color, width=None, click=None):
        lb = tk.Label(root, bg=BG, fg=color, font=FONT)
        if width:
            lb.config(width=width, anchor="w")
        if click:
            lb.config(cursor="hand2")
            lb.bind("<Button-1>", click)
        if SHOW[key]:
            lb.pack(side=side, padx=10)
        return lb

    # left side
    cpu_l = label("cpu", "left", C["cpu"], click=open_taskmgr)
    gpu_l = label("gpu", "left", C["gpu"], click=open_taskmgr)
    ram_l = label("ram", "left", C["ram"], click=open_taskmgr)
    vram_l = label("vram", "left", C["vram"], click=open_taskmgr)
    disk_l = label("disk", "left", C["disk"])
    title_l = label("window_title", "left", C["title"], width=TITLE_CHARS)

    # right side (first packed = rightmost)
    def on_time_click(_e):
        toggle_calendar()

    def on_weather_click(_e):
        weather_refresh.set()

    def on_timer_click(_e):
        timer.click(TIMER_MINUTES)

    def on_volume_click(_e):
        if volume:
            volume.toggle_mute()
            refresh_volume()

    def on_volume_wheel(e):
        if volume:
            volume.change(VOLUME_STEP if e.delta > 0 else -VOLUME_STEP)
            refresh_volume()

    time_l = label("time", "right", C["time"], click=on_time_click)
    weather_l = label("weather", "right", C["weather"], click=on_weather_click)
    timer_l = label("timer", "right", C["timer"], width=12, click=on_timer_click)
    vol_l = label("volume", "right", C["volume"], click=on_volume_click)
    vol_l.bind("<MouseWheel>", on_volume_wheel)
    up_l = label("network", "right", C["up"], click=open_network)
    down_l = label("network", "right", C["down"], click=open_network)

    # centre: media controls (fills the space between the left and right groups)
    mid = tk.Frame(root, bg=BG)
    if SHOW["media"]:
        mid.pack(side="left", expand=True)

    def button(text, action):
        b = tk.Label(mid, text=text, bg=BG, fg=C["media"],
                     font=("Segoe UI Symbol", 11), cursor="hand2")
        b.pack(side="left", padx=6)
        b.bind("<Button-1>", lambda e: media_cmd(action))
        b.bind("<Enter>", lambda e: b.config(fg=C["time"]))
        b.bind("<Leave>", lambda e: b.config(fg=C["media"]))
        return b

    button("\u23ee", "prev")
    play_b = button("\u25b6", "toggle")
    button("\u23ed", "next")
    media_l = tk.Label(mid, bg=BG, fg=C["media"], font=FONT, width=MEDIA_CHARS, anchor="w")
    media_l.pack(side="left", padx=(8, 0))

    # ----- hover tips -----
    add_tip(cpu_l, cpu_tip)
    add_tip(gpu_l, gpu_tip)
    add_tip(ram_l, ram_tip)
    add_tip(vram_l, vram_tip)
    add_tip(disk_l, disk_tip)
    add_tip(title_l, lambda: state["title"] if len(state["title"]) > TITLE_CHARS else "")
    add_tip(down_l, net_tip, "right")
    add_tip(up_l, net_tip, "right")
    add_tip(vol_l, lambda: "Scroll: volume\nClick: mute", "right")
    add_tip(timer_l, lambda: "Click: start / pause\nMenu > Timer: other options", "right")
    add_tip(weather_l, lambda: weather["detail"], "right")
    add_tip(time_l, time_tip, "right")
    add_tip(media_l, lambda: media["text"] if len(media["text"]) > MEDIA_CHARS else "")

    # ----- calendar popup (click the clock) -----
    cal = {"open": False, "y": 0, "m": 0, "idle": 0.0}

    def close_calendar():
        if cal["open"]:
            cal["open"] = False
            cal_win.withdraw()

    def shift_month(delta):
        m = cal["m"] - 1 + delta
        cal["y"] += m // 12
        cal["m"] = m % 12 + 1
        root.after(1, draw_calendar)         # redraw after this click has finished

    def draw_calendar():
        for child in cal_win.winfo_children():
            child.destroy()
        head = tk.Frame(cal_win, bg=TIP_BG)
        head.pack(fill="x", padx=8, pady=(6, 2))
        for side, text, delta in (("left", "\u25c0", -1), ("right", "\u25b6", 1)):
            b = tk.Label(head, text=text, bg=TIP_BG, fg=C["media"], cursor="hand2", font=FONT)
            b.pack(side=side, padx=4)
            b.bind("<Button-1>", lambda e, d=delta: shift_month(d))
        tk.Label(head, text=f"{calendar.month_name[cal['m']]} {cal['y']}", bg=TIP_BG, fg=TIP_FG,
                 font=FONT).pack(side="left", expand=True)
        grid = tk.Frame(cal_win, bg=TIP_BG)
        grid.pack(padx=8, pady=(0, 8))
        names = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]
        if not WEEK_MONDAY:
            names = names[-1:] + names[:-1]
        for c, name in enumerate(names):
            tk.Label(grid, text=name, bg=TIP_BG, fg=C["dim"], font=FONT, width=3).grid(row=0, column=c)
        today = datetime.date.today()
        weeks = calendar.Calendar(0 if WEEK_MONDAY else 6).monthdayscalendar(cal["y"], cal["m"])
        for r, week in enumerate(weeks, start=1):
            for c, day in enumerate(week):
                if day == 0:
                    continue
                is_today = (cal["y"], cal["m"], day) == (today.year, today.month, today.day)
                tk.Label(grid, text=str(day), width=3, font=FONT,
                         fg=BG if is_today else TIP_FG,
                         bg=C["time"] if is_today else TIP_BG).grid(row=r, column=c)
        place_popup(cal_win, time_l, "right")

    def watch_calendar():
        if not cal["open"]:
            return
        px, py = root.winfo_pointerxy()
        inside = False
        for wd in (cal_win, time_l):
            x0, y0 = wd.winfo_rootx(), wd.winfo_rooty()
            if x0 <= px <= x0 + wd.winfo_width() and y0 <= py <= y0 + wd.winfo_height():
                inside = True
        now = time.time()
        if inside:
            cal["idle"] = now
        elif now - cal["idle"] > 1.5:        # mouse has been away for a moment: close
            close_calendar()
            return
        root.after(250, watch_calendar)

    def toggle_calendar():
        if cal["open"]:
            close_calendar()
            return
        tip_hide()
        today = datetime.date.today()
        cal["y"], cal["m"], cal["idle"], cal["open"] = today.year, today.month, time.time(), True
        draw_calendar()
        cal_win.deiconify()
        root.after(250, watch_calendar)

    # ----- periodic refresh -----
    state = {"net": psutil.net_io_counters(), "t": time.time(), "title": ""}
    psutil.cpu_percent()                    # prime; the first call always returns 0.0
    psutil.cpu_percent(percpu=True)
    threading.Thread(target=top_procs, args=("cpu",), daemon=True).start()   # prime per-process CPU

    def refresh_volume():
        cur = volume.get() if volume else None
        if cur is None:
            vol_l.config(text="VOL n/a")
        elif cur[1]:
            vol_l.config(text="VOL mute")
        else:
            vol_l.config(text=f"VOL {cur[0] * 100:3.0f}%")

    def update():
        now = time.time()
        net = psutil.net_io_counters()
        dt = max(now - state["t"], 1e-6)
        down = (net.bytes_recv - state["net"].bytes_recv) / dt
        up = (net.bytes_sent - state["net"].bytes_sent) / dt
        state["net"], state["t"] = net, now

        vm = psutil.virtual_memory()
        cpu_l.config(text=f"CPU {psutil.cpu_percent():3.0f}%")
        ram_l.config(text=f"RAM {vm.used / 2**30:4.1f}/{vm.total / 2**30:.0f} GB")

        if gpu["util"] is not None:
            gpu_l.config(text=f"GPU {gpu['util']:3.0f}%")
            of_total = f"/{gpu['total']:.0f}" if gpu["total"] else ""
            vram_l.config(text=f"VRAM {gpu['used']:4.1f}{of_total} GB")
        else:
            gpu_l.config(text="GPU n/a")
            vram_l.config(text="VRAM n/a")

        if SHOW["disk"]:
            try:
                pct = psutil.disk_usage(DISK_DRIVE + "\\").percent
                disk_l.config(text=f"DSK {DISK_DRIVE} {pct:3.0f}%")
            except OSError:
                disk_l.config(text=f"DSK {DISK_DRIVE} n/a")

        if SHOW["window_title"]:
            t = foreground_title(user32, hwnd)
            if t is not None:
                state["title"] = t
            shown = state["title"]
            if len(shown) > TITLE_CHARS:
                shown = shown[: TITLE_CHARS - 1] + "\u2026"
            title_l.config(text=shown)

        if SHOW["volume"]:
            refresh_volume()

        down_l.config(text=f"\u2193 {fmt_rate(down)}")
        up_l.config(text=f"\u2191 {fmt_rate(up)}")

        text = media["text"]
        if len(text) > MEDIA_CHARS:
            text = text[: MEDIA_CHARS - 1] + "\u2026"
        media_l.config(text=text or "no media", fg=C["media"] if text else C["dim"])
        play_b.config(text="\u23f8" if media["playing"] else "\u25b6")

        weather_l.config(text=weather["text"])

        timer.tick()
        if timer.done:
            color = "#f38ba8" if int(now) % 2 else C["timer"]       # flash when finished
        else:
            color = C["dim"] if timer.mode is None else C["timer"]
        timer_l.config(text=timer.text(), fg=color)

        time_l.config(text=time.strftime(TIME_FORMAT))
        root.after(1000, update)

    # ----- menu, restart, autostart -----
    def quit_app():
        if appbar is not None:
            try:
                unregister_appbar(appbar)
            except Exception:
                pass
        root.destroy()

    def open_config():
        try:
            os.startfile(CONFIG_PATH)
        except Exception:
            pass

    def restart():
        quit_app()
        env = dict(os.environ)
        if getattr(sys, "frozen", False):
            env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"    # a onefile exe must unpack itself again
            cmd = [sys.executable]
        else:
            cmd = [sys.executable, os.path.abspath(__file__)]
        subprocess.Popen(cmd, env=env)

    autostart_var = tk.BooleanVar(root, value=autostart_enabled())

    def toggle_autostart():
        try:
            set_autostart(autostart_var.get())
        except Exception:
            autostart_var.set(not autostart_var.get())     # couldn't write the registry; undo the tick

    menu = tk.Menu(root, tearoff=0)
    menu.add_command(label="Open config file", command=open_config)
    menu.add_command(label="Restart (apply changes)", command=restart)
    menu.add_checkbutton(label="Start with Windows", variable=autostart_var, command=toggle_autostart)
    timer_menu = tk.Menu(menu, tearoff=0)
    for mins in sorted({5, 10, TIMER_MINUTES}):
        timer_menu.add_command(label=f"Countdown {mins} min", command=lambda m=mins: timer.start_down(m))
    timer_menu.add_command(label="Stopwatch", command=timer.start_up)
    timer_menu.add_command(label="Pause / resume", command=timer.pause_resume)
    timer_menu.add_command(label="Reset", command=timer.reset)
    menu.add_cascade(label="Timer", menu=timer_menu)
    menu.add_separator()
    menu.add_command(label="Quit", command=quit_app)

    def show_menu(e):
        prev_front = user32.GetForegroundWindow()
        try:
            user32.SetForegroundWindow(hwnd)      # a popup menu needs its owner in front to close properly
        except Exception:
            pass
        try:
            menu.tk_popup(e.x_root, e.y_root)
        finally:
            menu.grab_release()
        if prev_front and prev_front != hwnd:     # hand keyboard focus back to what you were using
            try:
                user32.SetForegroundWindow(prev_front)
            except Exception:
                pass

    root.bind("<Button-3>", show_menu)

    # ----- watchers -----
    hidden = {"v": False}

    def check_fullscreen():
        try:
            fs = fullscreen_in_front(user32, sw, sh)
        except Exception:
            fs = False
        if fs != hidden["v"]:
            hidden["v"] = fs
            if fs:
                tip_hide()
                close_calendar()
            # SW_SHOWNOACTIVATE: reappear without stealing keyboard focus from your app
            user32.ShowWindow(hwnd, SW_HIDE if fs else SW_SHOWNOACTIVATE)
        root.after(500, check_fullscreen)

    cfg_stamp = {"v": config_mtime()}

    def watch_config():
        m = config_mtime()
        if m is not None and m != cfg_stamp["v"]:
            cfg_stamp["v"] = m
            root.after(400, restart)              # give the editor a moment to finish writing
            return
        root.after(2000, watch_config)

    # ----- start everything -----
    if SHOW["weather"]:
        threading.Thread(target=weather_worker, daemon=True).start()
    if SHOW["gpu"] or SHOW["vram"]:
        threading.Thread(target=gpu_worker, daemon=True).start()
    if SHOW["media"]:
        threading.Thread(target=media_worker, daemon=True).start()
    if HIDE_ON_FULLSCREEN:
        check_fullscreen()
    if AUTO_RELOAD:
        root.after(2000, watch_config)
    update()
    root.mainloop()


if __name__ == "__main__":
    main()
