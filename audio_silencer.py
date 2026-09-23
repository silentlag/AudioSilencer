import base64
import ctypes
import json
import os
import struct
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
import websocket
from pycaw.pycaw import AudioUtilities
TOSU_HTTP = "http://127.0.0.1:24050"
TOSU_WS = "ws://127.0.0.1:24050/ws"
DEFAULT_THRESHOLD = 1000
DEBUG = os.environ.get("OSU_DEBUG") == "1"
GUI = "--gui" in sys.argv or len(sys.argv) == 1
CONSOLE = "--console" in sys.argv
TRAY = "--tray" in sys.argv
def _app_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))

CONFIG_FILE = os.path.join(_app_dir(), "config.json")
TARGET = ("discordptb.exe",)
MUTE_MODE = "map"
MUTE_METHOD = "mixer"
DISCORD = {}  # client_id, client_secret, access_token, refresh_token, expires_at
THRESHOLD = DEFAULT_THRESHOLD
_muted = False
_map_locked = False
_fc_clean = True
_fc_miss = 0
_fc_prev_combo = 0
_fc_prev_time = 0
_fc_sb = 0
_fc_in_map = False
_stop = threading.Event()
_ws_app = None
_tray_icon = None
_icon_green = None
_icon_red = None
_gui_state = {"combo": 0, "muted": False, "log": []}

def _icon_path():
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    p = os.path.join(base, "app.ico")
    return p if os.path.isfile(p) else None

def _is_running(exe: str) -> bool:
    try:
        r = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {exe}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return exe.lower() in r.stdout.lower()
    except Exception:
        return False

def _find_target_exe() -> str:
    import glob as _glob

    name = TARGET[0].replace(".exe", "")
    base = os.path.join(os.environ.get("LOCALAPPDATA", ""), name)
    cands = _glob.glob(os.path.join(base, "app-*", f"{name}.exe"))
    if not cands and name.lower() != "discord":
        base = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Discord")
        cands = _glob.glob(os.path.join(base, "app-*", "Discord.exe"))
    return sorted(cands)[-1] if cands else ""

def _ensure_target_running() -> None:
    if any(_is_running(n) for n in TARGET):
        _log(", ".join(TARGET) + ": running")
        return
    if len(TARGET) != 1 or TARGET[0] not in ("discord.exe", "discordptb.exe"):
        _log(", ".join(TARGET) + ": not running, start it manually")
        return
    exe = _find_target_exe()
    if not exe:
        _log("target exe not found, start it manually")
        return
    _log("launching " + os.path.basename(exe) + "...")
    subprocess.Popen([exe])
    for _ in range(40):
        if _is_running(TARGET[0]):
            _log("target started")
            return
        time.sleep(0.5)
    _log("target did not start")

def _dbg(*a):
    if DEBUG:
        print(time.strftime("[%H:%M:%S]"), *a)

def _log(msg):
    _gui_state["log"].append(time.strftime("[%H:%M:%S] ") + msg)
    _gui_state["log"] = _gui_state["log"][-200:]
    print(msg)

def _dpapi(data: bytes, protect: bool) -> bytes:
    """Windows DPAPI crypting for token security"""
    from ctypes import wintypes

    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    def mkbuf(d):
        b = BLOB()
        buf = ctypes.create_string_buffer(d, len(d))
        b.cbData = len(d)
        b.pbData = ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))
        return b, buf

    c = ctypes.windll.crypt32
    src, keep = mkbuf(data)
    out = BLOB()
    if protect:
        ok = c.CryptProtectData(ctypes.byref(src), None, None, None, None, 1, ctypes.byref(out))
    else:
        ok = c.CryptUnprotectData(ctypes.byref(src), None, None, None, None, 1, ctypes.byref(out))
    if not ok:
        raise OSError(f"dpapi error {ctypes.get_last_error()}")
    raw = ctypes.string_at(out.pbData, out.cbData)
    ctypes.windll.kernel32.LocalFree(out.pbData)
    return raw


def _discord_cfg_out() -> dict:
    out = {k: v for k, v in DISCORD.items()
           if k not in ("access_token", "refresh_token", "access_enc", "refresh_enc")}
    if DISCORD.get("access_token"):
        out["access_enc"] = base64.b64encode(
            _dpapi(DISCORD["access_token"].encode(), True)).decode()
    if DISCORD.get("refresh_token"):
        out["refresh_enc"] = base64.b64encode(
            _dpapi(DISCORD["refresh_token"].encode(), True)).decode()
    return out


def _discord_tokens_load() -> None:
    """access_enc/refresh_enc -> access_token/refresh_token (runtime)."""
    for enc, plain in (("access_enc", "access_token"), ("refresh_enc", "refresh_token")):
        blob = DISCORD.pop(enc, None)
        if blob:
            try:
                DISCORD[plain] = _dpapi(base64.b64decode(blob), False).decode()
            except Exception:
                _log("Discord: encrypted tokens failed to decrypt (config from another machine?), login required")
                DISCORD.pop(plain, None)


def _save_config():
    cfg = _load_config() or {}
    cfg.update(
        {
            "threshold": THRESHOLD,
            "target": list(TARGET),
            "mute_mode": MUTE_MODE,
            "mute_method": MUTE_METHOD,
            "discord": _discord_cfg_out(),
        }
    )
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f)
    except Exception as e:
        _dbg("config save failed:", e)

def _load_config():
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None

_vol_cache = {"target": None, "vols": []}

def _target_volumes(force: bool = False):
    if not force and _vol_cache["target"] == TARGET and _vol_cache["vols"]:
        return _vol_cache["vols"]
    try:
        import comtypes
        comtypes.CoInitialize()
    except Exception:
        pass
    vols = []
    try:
        for s in AudioUtilities.GetAllSessions():
            if s.Process and s.Process.name().lower() in TARGET:
                vols.append(s.SimpleAudioVolume)
    except Exception as e:
        _log(f"mixer error: {e}")
    if vols:
        _vol_cache["target"] = TARGET
        _vol_cache["vols"] = vols
    else:
        _vol_cache["target"] = None
        _vol_cache["vols"] = []
    return vols

# ---------------- Discord IPC (self-deafen) ----------------
_K32 = ctypes.WinDLL("kernel32")
_K32.CreateFileW.restype = ctypes.c_void_p
_INVALID_HANDLE = 2**64 - 1
_ipc = {"h": None, "authed": False, "warned": False}
_OAUTH_PORT = 8000
_OAUTH_HOST = "https://discord.com"
def _creds_valid() -> bool:
    cid = DISCORD.get("client_id") or ""
    sec = DISCORD.get("client_secret") or ""
    return bool(cid) and not cid.startswith("YOUR_") and bool(sec) and not sec.startswith("YOUR_")

def _ipc_send(op, obj):
    payload = json.dumps(obj).encode()
    data = struct.pack("<II", op, len(payload)) + payload
    w = ctypes.c_ulong()
    if not _K32.WriteFile(_ipc["h"], data, len(data), ctypes.byref(w), None):
        raise OSError(ctypes.get_last_error())

def _ipc_available():
    n = ctypes.c_ulong()
    if not _K32.PeekNamedPipe(_ipc["h"], None, 0, None, ctypes.byref(n), None):
        return -1
    return n.value

def _ipc_recv(timeout_s=3.0):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        avail = _ipc_available()
        if avail < 0:
            raise OSError("pipe dead")
        if avail >= 8:
            break
        time.sleep(0.01)
    else:
        return None
    hdr = ctypes.create_string_buffer(8)
    n = ctypes.c_ulong()
    _K32.ReadFile(_ipc["h"], hdr, 8, ctypes.byref(n), None)
    op, ln = struct.unpack("<II", hdr.raw)
    buf = ctypes.create_string_buffer(ln)
    if ln:
        _K32.ReadFile(_ipc["h"], buf, ln, ctypes.byref(n), None)
    return op, buf.raw.decode(errors="replace")

def _ipc_drain():
    while _ipc_available() >= 8:
        if _ipc_recv(0.2) is None:
            break

def _ipc_connect() -> bool:
    _ipc_close()
    cid = DISCORD.get("client_id")
    tok = DISCORD.get("access_token")
    if not cid or not tok:
        if not _ipc["warned"]:
            _log("Discord IPC: not logged in (press Login Discord)")
            _ipc["warned"] = True
        return False
    for i in range(10):
        cand = _K32.CreateFileW(
            f"\\\\.\\pipe\\discord-ipc-{i}", 0xC0000000, 0, None, 3, 0, None
        )
        if cand is None or cand == _INVALID_HANDLE:
            continue
        _ipc["h"] = cand
        try:
            _ipc_send(0, {"v": 1, "client_id": cid})
            hs = _ipc_recv(5)
            if hs is None:
                _ipc_close()
                continue
            if hs[0] == 2:
                _dbg(f"pipe {i} rejected: {hs[1][:100]}")
                _ipc_close()
                continue
            try:
                user = json.loads(hs[1]).get("data", {}).get("user", {}).get("username", "")
            except Exception:
                user = ""
            if user.lower() == "arrpc":
                _dbg(f"skipping arRPC pipe {i}")
                _ipc_close()
                continue
            _ipc_send(
                1, {"cmd": "AUTHENTICATE", "args": {"access_token": tok}, "nonce": "auth"}
            )
            while True:
                fr = _ipc_recv(8)
                if fr is None:
                    _log("Discord IPC: authenticate timeout (Discord did not respond; "
                         "is it running and logged in?)")
                    _ipc_close()
                    return False
                _dbg("ipc auth frame:", str(fr)[:200])
                d = json.loads(fr[1]) if fr[0] in (1, 2) else {}
                if fr[0] == 2:
                    _log(f"Discord IPC: connection closed by Discord: {fr[1][:150]}")
                    _ipc_close()
                    return False
                if d.get("nonce") == "auth" or d.get("evt") == "ERROR":
                    if d.get("evt") == "ERROR":
                        code = d.get("data", {}).get("code")
                        _log(
                            f"Discord IPC: authenticate failed ({code}), refreshing token..."
                        )
                        if _token_refresh():
                            return _ipc_connect()
                        return False
                    break
            _ipc["authed"] = True
            _ipc["warned"] = False
            _ipc_drain()
            _log("Discord IPC: connected")
            return True
        except Exception as e:
            _log(f"Discord IPC: {e}")
            _ipc_close()
            continue
    _log("Discord IPC: no usable pipe found (is Discord running? "
         "only arRPC/emulator pipes present?)")
    return False

def _ipc_close():
    if _ipc["h"]:
        _K32.CloseHandle(_ipc["h"])
    _ipc["h"] = None
    _ipc["authed"] = False

def _ipc_mute(deaf: bool) -> bool:
    if not _ipc["authed"] and not _ipc_connect():
        return False
    try:
        _ipc_drain()
        _ipc_send(
            1, {"cmd": "SET_VOICE_SETTINGS", "args": {"deaf": deaf}, "nonce": "df"}
        )
        while True:
            fr = _ipc_recv(3)
            if fr is None:
                return False
            d = json.loads(fr[1]) if fr[0] == 1 else {}
            if d.get("evt") == "ERROR":
                _log(f"Discord IPC error: {d.get('data', {}).get('message')}")
                _ipc_close()
                return False
            if d.get("nonce") == "df":
                return True
    except Exception:
        _ipc_close()
        return False

def _ipc_ensure():
    if not _creds_valid():
        return
    exp = DISCORD.get("expires_at", 0)
    if time.time() > exp - 60 and DISCORD.get("refresh_token"):
        _token_refresh()
    _ipc_connect()

def _token_request(params: dict) -> dict:
    req = urllib.request.Request(
        _OAUTH_HOST + "/api/oauth2/token",
        data=urllib.parse.urlencode(params).encode(),
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AudioSilencer/1.0",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())

def _token_save(d: dict):
    DISCORD["access_token"] = d["access_token"]
    DISCORD["refresh_token"] = d.get("refresh_token", DISCORD.get("refresh_token", ""))
    DISCORD["expires_at"] = int(time.time()) + int(d.get("expires_in", 604800))
    _save_config()

def _token_refresh() -> bool:
    try:
        d = _token_request(
            {
                "client_id": DISCORD["client_id"],
                "client_secret": DISCORD["client_secret"],
                "grant_type": "refresh_token",
                "refresh_token": DISCORD["refresh_token"],
                "redirect_uri": f"http://localhost:{_OAUTH_PORT}",
            }
        )
        _token_save(d)
        _log("Discord token refreshed")
        return True
    except Exception as e:
        _log(f"Discord token refresh failed: {e}")
        return False


def _oauth_login() -> bool:
    if not DISCORD.get("client_id") or not DISCORD.get("client_secret"):
        _log("Discord IPC: client_id/secret missing - fill YOUR_CLIENT_ID / "
             "YOUR_CLIENT_SECRET in config.json or the GUI")
        return False
    got = {"code": None}
    ev = threading.Event()
    from http.server import BaseHTTPRequestHandler, HTTPServer
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.urlparse(self.path).query
            got["code"] = urllib.parse.parse_qs(q).get("code", [None])[0]
            body = b"<h2>AudioSilencer authorized!</h2><p>Close this tab.</p>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *a):
            pass
    srv = HTTPServer(("127.0.0.1", _OAUTH_PORT), H)
    threading.Thread(target=srv.handle_request, daemon=True).start()

    url = (
        _OAUTH_HOST
        + "/oauth2/authorize?"
        + urllib.parse.urlencode(
            {
                "client_id": DISCORD["client_id"],
                "redirect_uri": f"http://localhost:{_OAUTH_PORT}",
                "response_type": "code",
                "scope": "rpc rpc.voice.write",
            }
        )
    )
    _log("opening browser for Discord login...")
    webbrowser.open(url)
    deadline = time.time() + 120
    while got["code"] is None and time.time() < deadline:
        time.sleep(0.2)
    srv.server_close()
    if got["code"] is None:
        _log("Discord login: timeout / cancelled")
        return False
    try:
        d = _token_request(
            {
                "client_id": DISCORD["client_id"],
                "client_secret": DISCORD["client_secret"],
                "grant_type": "authorization_code",
                "code": got["code"],
                "redirect_uri": f"http://localhost:{_OAUTH_PORT}",
            }
        )
        _token_save(d)
        _log("Discord login OK")
    except Exception as e:
        _log(f"Discord login failed: {e}")
        return False
    return _ipc_connect()

def _mixer_mute(mute: bool) -> bool:
    vols = _target_volumes()
    if not vols:
        _log(", ".join(TARGET) + ": no audio session yet")
        return False
    try:
        for v in vols:
            v.SetMute(mute, None)
    except Exception:
        vols = _target_volumes(force=True)
        if not vols:
            _log(", ".join(TARGET) + ": no audio session yet")
            return False
        try:
            for v in vols:
                v.SetMute(mute, None)
        except Exception as e:
            _log(f"mixer error: {e}")
            return False
    return True

def set_discord_mute(mute: bool) -> None:
    global _muted
    if mute == _muted:
        return
    if MUTE_METHOD == "ipc":
        ok = _ipc_mute(mute)
    else:
        ok = _mixer_mute(mute)
    if not ok:
        return
    _muted = mute
    label = "MUTED" if mute else "UNMUTED"
    method = "ipc" if MUTE_METHOD == "ipc" else ", ".join(TARGET)
    _log(method + ": " + label)
    _gui_state["muted"] = mute
    if _tray_icon:
        try:
            _tray_icon.icon = _icon_red if mute else _icon_green
            _tray_icon.title = f"AudioSilencer - {label}"
            _tray_icon.update_menu()
        except Exception:
            pass

def _ws_alive() -> bool:
    try:
        urllib.request.urlopen(TOSU_HTTP, timeout=1)
        return True
    except Exception:
        return False

def _launch_tosu_hidden() -> bool:
    here = _app_dir()
    default = os.path.join(here, "tosu", "tosu.exe")
    if not os.path.isfile(default):
        return False
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0
    subprocess.Popen([default], startupinfo=si)
    for _ in range(30):
        if _ws_alive():
            return True
        time.sleep(0.5)
    return False

def _ensure_tosu() -> bool:
    if _ws_alive():
        print("tosu already running")
        return True
    here = _app_dir()
    default = os.path.join(here, "tosu", "tosu.exe")
    p = input(f"tosu.exe path [Enter = {default}]: ").strip() or default
    if not os.path.isfile(p):
        print("not found:", p)
        return False
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0
    subprocess.Popen([p], startupinfo=si)
    print("tosu launched hidden")
    for _ in range(30):
        if _ws_alive():
            print("tosu connected")
            return True
        time.sleep(0.5)
    print("tosu did not respond within 15s")
    return False

def _map_progress_pct(d) -> float:
    t = d.get("menu", {}).get("bm", {}).get("time", {})
    try:
        cur = float(t.get("current"))
        first = float(t.get("firstObj"))
        full = float(t.get("full"))
    except (TypeError, ValueError):
        return -1.0
    span = full - first
    if span <= 0:
        return -1.0
    return max(0.0, min(100.0, (cur - first) / span * 100.0))

def _gp_miss(d) -> int:
    """Счётчик миссов из tosu (4.26: hits['0']; старые: hits.miss/counts.miss)."""
    gp = d.get("gameplay", {})
    for src in (gp.get("hits"), gp.get("counts")):
        if isinstance(src, dict):
            for key in ("0", "miss", "Miss", "MISS"):
                v = src.get(key)
                if isinstance(v, (int, float)):
                    return int(v)
    return 0


def _gp_slider_breaks(d):
    """Счётчик слайдербрейков из tosu, или None если не отдаётся."""
    gp = d.get("gameplay", {})
    for src in (gp.get("hits"), gp.get("counts")):
        if isinstance(src, dict):
            for key in ("sliderBreaks", "slider_breaks", "sliderbreaks"):
                v = src.get(key)
                if isinstance(v, (int, float)):
                    return int(v)
    return None


def on_message(_ws, message):
    global _map_locked
    try:
        d = json.loads(message)
        combo = int(d.get("gameplay", {}).get("combo", {}).get("current") or 0)
        state = d.get("menu", {}).get("state", 0)
    except (ValueError, TypeError, json.JSONDecodeError) as e:
        _dbg("skip:", e)
        return
    _gui_state["combo"] = combo

    if MUTE_MODE == "progress":
        pct = _map_progress_pct(d) if state == 2 else 0.0
        _gui_state["pct"] = pct
        if state != 2 and _map_locked:
            set_discord_mute(False)
            _map_locked = False
        elif state == 2 and not _map_locked:
            if pct < 0:
                if not _gui_state.get("pct_warned"):
                    _log("progress mode: tosu sends no map time, can't compute %")
                    _gui_state["pct_warned"] = True
            elif pct >= THRESHOLD:
                set_discord_mute(True)
                _map_locked = True
    elif MUTE_MODE == "fc":
        global _fc_clean, _fc_miss, _fc_prev_combo, _fc_in_map, _fc_prev_time, _fc_sb
        miss = _gp_miss(d)
        sb = _gp_slider_breaks(d)
        if sb is None:
            sb = _fc_sb
        pct = _map_progress_pct(d) if state == 2 else 0.0
        _gui_state["pct"] = pct
        _gui_state["fc_miss"] = miss
        if state != 2:
            if _map_locked:
                set_discord_mute(False)
                _map_locked = False
            _fc_in_map = False
            _fc_prev_time = 0
        else:
            cur_time = d.get("menu", {}).get("bm", {}).get("time", {}).get("current")
            if not _fc_in_map:
                _fc_in_map = True
                _fc_clean = True
                _fc_miss = miss
                _fc_sb = sb or 0
                _fc_prev_combo = 0
                _fc_prev_time = cur_time or 0
                if _map_locked:
                    set_discord_mute(False)
                    _map_locked = False
            elif (isinstance(cur_time, (int, float))
                    and isinstance(_fc_prev_time, (int, float))
                    and cur_time < _fc_prev_time - 2000):
                _fc_clean = True
                _fc_miss = miss
                _fc_sb = sb or 0
                _fc_prev_combo = 0
                _fc_prev_time = cur_time
                if _map_locked:
                    set_discord_mute(False)
                    _map_locked = False
            else:
                _fc_prev_time = cur_time if isinstance(cur_time, (int, float)) else _fc_prev_time
            if miss > _fc_miss or (sb is not None and sb > _fc_sb):
                if miss > _fc_miss:
                    _fc_miss = miss
                    _fc_clean = False
                    if _map_locked:
                        set_discord_mute(False)
                        _map_locked = False
                elif sb is not None and sb > _fc_sb:
                    _fc_sb = sb
                    if _fc_clean:
                        _log("fc: slider break - FC lost")
                    _fc_clean = False
            else:
                if _fc_prev_combo > 0 and combo == 0:
                    if _fc_clean:
                        _log("fc: combo break (slider break) - FC lost")
                    _fc_clean = False
                _fc_prev_combo = combo
                if not _map_locked and _fc_clean and pct >= THRESHOLD:
                    set_discord_mute(True)
                    _map_locked = True
    elif MUTE_MODE == "map":
        if state != 2 and _map_locked:
            _dbg("map ended (state:", state, ") -> unmute")
            set_discord_mute(False)
            _map_locked = False
        elif state == 2 and not _map_locked and combo >= THRESHOLD:
            _dbg("combo:", combo, "-> MUTED until map ends")
            set_discord_mute(True)
            _map_locked = True
    else:
        _dbg(
            "combo:",
            combo,
            "threshold:",
            THRESHOLD,
            "->",
            "MUTE" if combo >= THRESHOLD else "ok",
        )
        set_discord_mute(combo >= THRESHOLD)

def _ws_loop():
    global _ws_app
    try:
        import comtypes

        comtypes.CoInitialize()
    except Exception:
        pass
    while not _stop.is_set():
        try:
            _ws_app = websocket.WebSocketApp(TOSU_WS, on_message=on_message)
            _ws_app.run_forever()
        except Exception as e:
            print("ws error:", e)
        if _stop.is_set():
            break
        _log("tosu unavailable, reconnecting in 2s...")
        _stop.wait(2)
    set_discord_mute(False)

def _run_tray():
    global _tray_icon, _icon_green, _icon_red
    try:
        import pystray
        from PIL import Image, ImageDraw
    except ImportError:
        print("tray mode requires: pip install pystray Pillow")
        sys.exit(1)

    def make_image(color):
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        ImageDraw.Draw(img).ellipse((4, 4, 60, 60), fill=color)
        return img

    ip = _icon_path()
    if ip:
        try:
            img = Image.open(ip).convert("RGBA").resize((64, 64), Image.LANCZOS)
            _icon_green = _icon_red = img
        except Exception:
            pass
    if _icon_green is None:
        _icon_green = make_image((0, 200, 0, 255))
        _icon_red = make_image((200, 0, 0, 255))

    def on_quit(icon, item):
        icon.stop()

    _tray_icon = pystray.Icon(
        "osu_mute",
        _icon_green,
        f"AudioSilencer - unmuted (threshold {THRESHOLD})",
        pystray.Menu(pystray.MenuItem("Quit", on_quit)),
    )
    threading.Thread(target=_ws_loop, daemon=True).start()
    _tray_icon.run()

# theme
def _run_gui():
    import tkinter as tk
    from tkinter import ttk, scrolledtext
    BG = "#1e1f24"
    FG = "#e8e8ec"
    ENTRY_BG = "#2a2c34"
    ACCENT = "#5865f2"
    RED = "#ed4245"
    GREEN = "#57f287"
    root = tk.Tk()
    root.title("AudioSilencer")
    ip = _icon_path()
    if ip:
        try:
            root.iconbitmap(ip)
        except Exception:
            pass
    root.configure(bg=BG)
    try:
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        val = ctypes.c_int(1)
        for attr in (20, 19):
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(val), 4) == 0:
                break
    except Exception:
        pass
    root.resizable(False, False)
    root.update_idletasks()
    x = (root.winfo_screenwidth() - root.winfo_reqwidth()) // 2
    y = (root.winfo_screenheight() - root.winfo_reqheight()) // 2
    root.geometry(f"+{x}+{y}")
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except Exception:
        pass
    style.configure("TLabel", background=BG, foreground=FG, font=("Segoe UI", 10))
    style.configure("TRadiobutton", background=BG, foreground=FG, font=("Segoe UI", 10))
    style.map("TRadiobutton", background=[("active", BG)])
    style.configure("TButton", font=("Segoe UI", 10))
    style.configure("Start.TButton", font=("Segoe UI", 10, "bold"))

    body = tk.Frame(root, bg=BG)
    body.grid(row=0, column=0, columnspan=3, sticky="ew", padx=16, pady=(14, 0))

    ttk.Label(body, text="Threshold (combo or %)").grid(
        row=0, column=0, sticky="w", pady=4
    )
    thr = tk.Entry(
        body,
        width=12,
        bg=ENTRY_BG,
        fg=FG,
        relief="flat",
        insertbackground=FG,
        font=("Segoe UI", 10),
    )
    thr.insert(0, str(THRESHOLD))
    thr.grid(row=0, column=1, sticky="w", padx=(8, 0), pady=4)
    proc_choice = tk.StringVar(
        value="ptb"
        if "ptb" in TARGET[0]
        else ("discord" if TARGET[0] == "discord.exe" else "other")
    )
    pframe = tk.Frame(body, bg=BG)
    pframe.grid(row=1, column=0, columnspan=2, sticky="ew", pady=4)
    ttk.Radiobutton(pframe, text="Discord PTB", variable=proc_choice, value="ptb").pack(
        side="left"
    )
    ttk.Radiobutton(pframe, text="Discord", variable=proc_choice, value="discord").pack(
        side="left", padx=(10, 0)
    )
    ttk.Radiobutton(pframe, text="Other", variable=proc_choice, value="other").pack(
        side="left", padx=(10, 0)
    )
    proc = tk.Entry(
        pframe,
        width=20,
        bg=ENTRY_BG,
        fg=FG,
        relief="flat",
        insertbackground=FG,
        font=("Segoe UI", 10),
    )
    proc.insert(0, TARGET[0])
    mode = tk.StringVar(value="1")
    ttk.Label(body, text="Mute mode").grid(row=2, column=0, sticky="nw", pady=4)
    mframe = tk.Frame(body, bg=BG)
    mframe.grid(row=2, column=1, sticky="w", padx=(8, 0), pady=4)
    ttk.Radiobutton(mframe, text="1 · until map ends", variable=mode, value="1").pack(
        anchor="w"
    )
    ttk.Radiobutton(
        mframe, text="2 · instant  (unmute on combo drop)", variable=mode, value="2"
    ).pack(anchor="w")
    ttk.Radiobutton(
        mframe, text="3 · progress %  (mute at % of map)", variable=mode, value="3"
    ).pack(anchor="w")
    ttk.Radiobutton(
        mframe, text="4 · FC %  (mute on clean FC, unmute on miss)", variable=mode, value="4"
    ).pack(anchor="w")
    method = tk.StringVar(value="1")
    ttk.Label(body, text="Mute method").grid(row=3, column=0, sticky="nw", pady=4)
    gmframe = tk.Frame(body, bg=BG)
    gmframe.grid(row=3, column=1, sticky="w", padx=(8, 0), pady=4)
    rb_mixer = ttk.Radiobutton(
        gmframe, text="Windows mixer (any process)", variable=method, value="1"
    )
    rb_ipc = ttk.Radiobutton(
        gmframe, text="Discord deafen (IPC Method)", variable=method, value="2"
    )
    rb_ipc.pack(anchor="w")
    def _refresh_proc_ui(*_):
        other = proc_choice.get() == "other"
        if other:
            proc.pack(side="left", padx=(10, 0))
            rb_mixer.pack(anchor="w", before=rb_ipc)
            rb_ipc.pack_forget()
            method.set("1")
        else:
            proc.pack_forget()
            rb_mixer.pack_forget()
            rb_ipc.pack(anchor="w")
            method.set("2")
    proc_choice.trace_add(
        "write",
        lambda *a: (_refresh_proc_ui(), live_apply()) if live_apply_ready[0] else None,
    )
    live_apply_ready = [False]
    # Discord app credentials
    credframe = tk.Frame(body, bg=BG)
    credframe.grid(row=4, column=0, columnspan=3, sticky="w", pady=(6, 0))
    ttk.Label(credframe, text="App Client ID").pack(side="left")
    cid_entry = tk.Entry(credframe, width=22, bg=ENTRY_BG, fg=FG, relief="flat",
                         insertbackground=FG, font=("Segoe UI", 9))
    cid_entry.insert(0, DISCORD.get("client_id", ""))
    cid_entry.pack(side="left", padx=(6, 12))
    ttk.Label(credframe, text="Client Secret").pack(side="left")
    csec_entry = tk.Entry(credframe, width=30, bg=ENTRY_BG, fg=FG, relief="flat",
                          insertbackground=FG, font=("Segoe UI", 9), show="*")
    csec_entry.insert(0, DISCORD.get("client_secret", ""))
    csec_entry.pack(side="left", padx=6)

    login_btn = tk.Button(
        body,
        text="Login Discord",
        width=14,
        bg="#2a2c34",
        fg=FG,
        activebackground="#1e1f24",
        activeforeground=FG,
        relief="flat",
        font=("Segoe UI", 10),
        cursor="hand2",
        command=lambda: threading.Thread(target=_oauth_login, daemon=True).start(),
    )
    login_btn.grid(row=3, column=2, sticky="w", padx=(8, 0), pady=4)
    def to_tray():
        global _tray_icon, _icon_green, _icon_red
        try:
            import pystray
            from PIL import Image, ImageDraw
        except ImportError:
            _log("tray requires: pip install pystray Pillow")
            return
        def make_image(color):
            img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
            ImageDraw.Draw(img).ellipse((4, 4, 60, 60), fill=color)
            return img
        ip = _icon_path()
        if ip:
            try:
                img = Image.open(ip).convert("RGBA").resize((64, 64), Image.LANCZOS)
                _icon_green = _icon_red = img
            except Exception:
                pass
        if _icon_green is None:
            _icon_green = make_image((0, 200, 0, 255))
            _icon_red = make_image((200, 0, 0, 255))
        def on_show(icon, item):
            icon.stop()
            root.after(0, root.deiconify)
        def on_quit(icon, item):
            icon.stop()
            root.after(0, root.destroy)
        _tray_icon = pystray.Icon(
            "osu_mute",
            _icon_green,
            "AudioSilencer",
            pystray.Menu(
                pystray.MenuItem("Show", on_show), pystray.MenuItem("Quit", on_quit)
            ),
        )
        root.withdraw()
        _tray_icon.run_detached()
    btn = tk.Button(
        root,
        text="Start",
        width=14,
        bg=ACCENT,
        fg="white",
        activebackground="#4752c4",
        activeforeground="white",
        relief="flat",
        font=("Segoe UI", 10, "bold"),
        cursor="hand2",
    )
    btn.grid(row=3, column=0, sticky="w", padx=16, pady=(10, 4))
    tk.Button(
        root,
        text="Hide to tray",
        width=14,
        bg="#2a2c34",
        fg=FG,
        activebackground="#1e1f24",
        activeforeground=FG,
        relief="flat",
        font=("Segoe UI", 10),
        cursor="hand2",
        command=to_tray,
    ).grid(row=3, column=1, sticky="w", padx=8, pady=(10, 4))
    tk.Button(
        root,
        text="Quit",
        width=12,
        bg="#2a2c34",
        fg=RED,
        activebackground="#c0392b",
        activeforeground="white",
        relief="flat",
        font=("Segoe UI", 10),
        cursor="hand2",
        command=root.destroy,
    ).grid(row=3, column=2, sticky="w", padx=8, pady=(10, 4))
    status = tk.Label(
        root, text="stopped", bg=BG, fg="#9aa0ac", font=("Segoe UI", 10, "bold")
    )
    status.grid(row=4, column=0, columnspan=3, sticky="w", padx=16, pady=(8, 2))
    log = scrolledtext.ScrolledText(
        root,
        width=54,
        height=10,
        bg="#16171b",
        fg="#b9bbbe",
        relief="flat",
        font=("Consolas", 9),
        insertbackground=FG,
    )
    log.grid(row=5, column=0, columnspan=3, sticky="ew", padx=16, pady=(2, 14))
    log.configure(state="disabled")
    running = [False]
    last_len = [0]
    def apply_fields():
        global TARGET, THRESHOLD, MUTE_MODE, MUTE_METHOD
        MUTE_MODE = {"1": "map", "2": "instant", "3": "progress", "4": "fc"}.get(mode.get(), "map")
        THRESHOLD = _parse_threshold(thr.get())
        ch = proc_choice.get()
        if ch == "ptb":
            TARGET = ("discordptb.exe",)
        elif ch == "discord":
            TARGET = ("discord.exe",)
        else:
            name = proc.get().strip().lower() or "discordptb.exe"
            if not name.endswith(".exe"):
                name += ".exe"
            TARGET = (name,)
        MUTE_METHOD = "mixer" if method.get() == "1" else "ipc"
        DISCORD["client_id"] = cid_entry.get().strip()
        DISCORD["client_secret"] = csec_entry.get().strip()

    def live_apply(*_):
        was = (TARGET, THRESHOLD, MUTE_MODE)
        apply_fields()
        if (TARGET, THRESHOLD, MUTE_MODE) != was:
            _save_config()
    thr.bind("<KeyRelease>", live_apply)
    proc.bind("<KeyRelease>", live_apply)
    cid_entry.bind("<KeyRelease>", live_apply)
    csec_entry.bind("<KeyRelease>", live_apply)
    for frame_ in (mframe, gmframe, pframe):
        for rb in frame_.winfo_children():
            if "Radiobutton" in rb.winfo_class():
                rb.configure(command=live_apply)
    _refresh_proc_ui()
    live_apply_ready[0] = True
    apply_fields()

    def toggle():
        if not running[0]:
            apply_fields()
            _save_config()
            _ensure_target_running()
            if MUTE_METHOD == "ipc":
                threading.Thread(target=_ipc_ensure, daemon=True).start()
            if not _ws_alive() and not _launch_tosu_hidden():
                _log("tosu not found -- start it manually")
            _stop.clear()
            _gui_state["log"] = []
            last_len[0] = 0
            threading.Thread(target=_ws_loop, daemon=True).start()
            running[0] = True
            btn.config(text="Stop", bg=RED, activebackground="#c0392b")
        else:
            _stop.set()
            if _ws_app:
                try:
                    _ws_app.close()
                except Exception:
                    pass
            running[0] = False
            btn.config(text="Start", bg=ACCENT, activebackground="#4752c4")
    btn.config(command=toggle)

    def refresh():
        logged = (
            bool(DISCORD.get("access_token"))
            and DISCORD.get("expires_at", 0) > time.time()
        )
        login_btn.config(
            text="Logged in" if logged else "Login Discord",
            fg=GREEN if logged else "#9aa0ac",
        )
        if running[0]:
            color = "red" if _gui_state["muted"] else "green"
            dot = "MUTED" if _gui_state["muted"] else "sound on"
            mid = (
                f"progress {_gui_state.get('pct', -1):.0f}% / {THRESHOLD}%"
                if MUTE_MODE in ("progress", "fc")
                else f"combo {_gui_state['combo']} / {THRESHOLD}"
            )
            if MUTE_MODE == "fc":
                mid += f"   miss {_gui_state.get('fc_miss', 0)}"
                if not _fc_clean:
                    mid += "   FC broken"
            status.config(
                text=f"{dot}   {mid}   mode {MUTE_MODE}   {', '.join(TARGET)}",
                fg=color,
            )
        else:
            status.config(text="stopped", fg="#9aa0ac")
        if len(_gui_state["log"]) != last_len[0]:
            last_len[0] = len(_gui_state["log"])
            log.configure(state="normal")
            log.delete("1.0", "end")
            log.insert("1.0", "\n".join(_gui_state["log"][-100:]))
            log.see("end")
            log.configure(state="disabled")
        root.after(80, refresh)
    root.after(80, refresh)
    root.mainloop()

def _parse_threshold(text: str) -> int:
    t = str(text).strip().replace("%", "").strip()
    try:
        v = int(t)
    except ValueError:
        v = DEFAULT_THRESHOLD
    if MUTE_MODE in ("progress", "fc"):
        v = max(0, min(100, v))
    return v

def _derive_mute_method():
    global MUTE_METHOD
    if any(t in ("discord.exe", "discordptb.exe") for t in TARGET) and _creds_valid():
        MUTE_METHOD = "ipc"
    else:
        MUTE_METHOD = "mixer"

def _parse_target(c):
    global TARGET
    c = c.strip().lower()
    if c in ("", "1", "discordptb"):
        TARGET = ("discordptb.exe",)
    elif c in ("2", "discord"):
        TARGET = ("discord.exe",)
    elif c == "process":
        TARGET = ("discord.exe", "discordptb.exe")
    else:
        if not c.endswith(".exe"):
            c += ".exe"
        TARGET = (c,)
    print("target:", ", ".join(TARGET))

def _parse_mode(m):
    global MUTE_MODE
    m = m.strip().lower()
    MUTE_MODE = (
        "progress"
        if m in ("3", "progress")
        else ("fc" if m in ("4", "fc") else ("instant" if m in ("2", "instant") else "map"))
    )
    print("mute mode:", MUTE_MODE)

def _run_diag():
    lines = []
    def out(s):
        lines.append(str(s))
        try:
            print(s)
        except Exception:
            pass
    cfg = _load_config() or {}
    DISCORD.update(cfg.get("discord", {}))
    _discord_tokens_load()
    out("=== AudioSilencer IPC diagnostics ===")
    pipes = [n for n in os.listdir("\\\\.\\pipe\\") if n.startswith("discord-ipc")]
    out(f"pipes found: {pipes}")
    cid = DISCORD.get("client_id", "")
    out(f"client_id: {cid[:6]}...{cid[-4:] if len(cid) > 10 else cid} len={len(cid)} "
        f"placeholder={cid.startswith('YOUR_')}")
    out(f"client_secret set: {bool(DISCORD.get('client_secret'))}")
    out(f"access_token set: {bool(DISCORD.get('access_token'))} "
        f"expires_at={DISCORD.get('expires_at')} now={int(time.time())}")
    if DISCORD.get("access_token"):
        done = False
        for i in range(10):
            cand = _K32.CreateFileW(f"\\\\.\\pipe\\discord-ipc-{i}", 0xC0000000, 0, None, 3, 0, None)
            if cand is None or cand == _INVALID_HANDLE:
                continue
            _ipc["h"] = cand
            out(f"connected: discord-ipc-{i}")
            try:
                _ipc_send(0, {"v": 1, "client_id": cid})
                fr = _ipc_recv(8)
                out(f"handshake frame: op={fr[0] if fr else None} "
                    f"payload={str(fr[1])[:300] if fr else 'TIMEOUT'}")
                if fr is None or fr[0] == 2:
                    out("pipe unusable, trying next...")
                    _ipc_close()
                    continue
                try:
                    user = json.loads(fr[1]).get("data", {}).get("user", {}).get("username", "")
                except Exception:
                    user = ""
                if user.lower() == "arrpc":
                    out(f"pipe {i} is arRPC (emulator, no AUTHENTICATE) - skipping")
                    _ipc_close()
                    continue
                _ipc_send(1, {"cmd": "AUTHENTICATE",
                              "args": {"access_token": DISCORD["access_token"]},
                              "nonce": "auth"})
                for n in range(5):
                    fr = _ipc_recv(8)
                    if fr is None:
                        out("authenticate: TIMEOUT (no frame in 8s)")
                        break
                    out(f"auth frame {n+1}: op={fr[0]} payload={str(fr[1])[:300]}")
                    d = json.loads(fr[1]) if fr[0] in (1, 2) else {}
                    if fr[0] == 2:
                        out("connection CLOSED by discord")
                        break
                    if d.get("evt") == "ERROR":
                        out(f"AUTHENTICATE ERROR: {d.get('data')}")
                        break
                    if d.get("nonce") == "auth":
                        out("AUTHENTICATE OK - deafen should work")
                        break
                done = True
            except Exception as e:
                out(f"exception: {type(e).__name__} {e}")
            _ipc_close()
            if done:
                break
        else:
            out("no usable pipe found")
    else:
        out("NO TOKEN - press Login Discord first")
    out("=== end ===")
    with open(os.path.join(_app_dir(), "diag.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print("written: diag.txt")

def main():
    global THRESHOLD, TARGET, MUTE_MODE, MUTE_METHOD
    if "--diag" in sys.argv:
        _run_diag()
        return
    a = [x for x in sys.argv[1:] if x not in ("--gui", "--tray")]
    if GUI and not TRAY and not CONSOLE:
        cfg = _load_config() if not a else None
        if a and a[0].isdigit():
            THRESHOLD = int(a[0])
        elif cfg:
            THRESHOLD = cfg.get("threshold", DEFAULT_THRESHOLD)
            TARGET = tuple(cfg.get("target", ("discordptb.exe",)))
            MUTE_MODE = cfg.get("mute_mode", "map")
            MUTE_METHOD = cfg.get("mute_method", "mixer")
            DISCORD.clear()
            DISCORD.update(cfg.get("discord", {}))
            _discord_tokens_load()
        _run_gui()
        return

    if TRAY:
        cfg = _load_config() if not a else None
        if a and a[0].isdigit():
            THRESHOLD = int(a[0])
        elif cfg:
            THRESHOLD = cfg.get("threshold", DEFAULT_THRESHOLD)
        c = (
            a[1]
            if len(a) > 1
            else (
                cfg.get("target", ("discordptb.exe",))[0] if cfg else "discordptb.exe"
            )
        )
        _parse_target(c)
        m = a[2] if len(a) > 2 else (cfg.get("mute_mode", "map") if cfg else "map")
        _parse_mode(m)
        if cfg:
            DISCORD.clear()
            DISCORD.update(cfg.get("discord", {}))
            _discord_tokens_load()
        _derive_mute_method()
        print("mute method:", MUTE_METHOD)
        if not _ws_alive() and not _launch_tosu_hidden():
            print("tosu not found, tray will wait for manual launch")
        _ensure_target_running()
        if MUTE_METHOD == "ipc":
            _ipc_ensure()
        _run_tray()
        return
    print("=== AudioSilencer ===")

    cfg = _load_config()
    if cfg:
        DISCORD.clear()
        DISCORD.update(cfg.get("discord", {}))
        _discord_tokens_load()
        if not a:
            THRESHOLD = cfg.get("threshold", DEFAULT_THRESHOLD)
            TARGET = tuple(cfg.get("target", ("discordptb.exe",)))
            MUTE_MODE = cfg.get("mute_mode", "map")

    if a and a[0].isdigit():
        THRESHOLD = _parse_threshold(a[0])
    else:
        try:
            THRESHOLD = _parse_threshold(
                input(f"Combo threshold [{DEFAULT_THRESHOLD}]: ")
            )
        except ValueError:
            THRESHOLD = DEFAULT_THRESHOLD

    print("Target:")
    print("  1 - Discord PTB")
    print("  2 - Discord (regular)")
    print("  or type any process name (e.g. chrome.exe)")
    c = a[1] if len(a) > 1 else input("Choice [1]: ")
    _parse_target(c)
    if len(a) > 2:
        _parse_mode(a[2])
    else:
        print("\nMute mode:")
        print("  1 - mute until map ends (keeps muted even if combo resets)")
        print("  2 - instant (unmute when combo drops below threshold)")
        print("  3 - progress % (mute at % of map, until map ends)")
        print("  4 - fc % (mute on clean FC, unmute on miss)")
        _parse_mode(input("Choice [1]: "))
    _save_config()
    _derive_mute_method()
    print("mute method:", MUTE_METHOD)

    if not _ensure_tosu():
        sys.exit(1)
    _ensure_target_running()

    if MUTE_MODE == "map":
        print(f"threshold {THRESHOLD} | combo >= threshold -> MUTED until map ends")
    else:
        print(f"threshold {THRESHOLD} | combo >= threshold -> MUTED, below -> UNMUTED")
    print("other modes: --gui (window), --tray (tray icon)")
    _ws_loop()
    
if __name__ == "__main__":
    main()
