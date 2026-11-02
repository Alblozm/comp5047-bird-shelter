#!/usr/bin/env python3
"""Shelter console: the bird shelter's control desk for demo and final use.

Runs on macOS and Windows with only Python 3.8+ (pyserial and certifi are bundled
in vendor/), or as the standalone executables built by tools/build_executables.py.
  python shelter_console.py              find the two boards over USB or WiFi
  python shelter_console.py --simulate   no hardware: simulated boards for rehearsal

Pipeline: perch pressure -> camera photo -> AI says whether there is a bird
-> the FireBeetle checks daylight, temperature, humidity and water -> mist.
The FireBeetle owns the mister and its safety rules; this console only tells
it whether the AI saw a bird, stores visits and shows everything in a browser.
"""
from __future__ import annotations

import argparse
import base64
import collections
import datetime as dt
import json
import math
import os
import queue
import re
import secrets
import socket
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

FROZEN = bool(getattr(sys, "frozen", False))  # running as a PyInstaller executable
APP = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
sys.path.insert(0, str(APP / "vendor"))
import serial  # noqa: E402  (bundled pyserial)
from serial.tools import list_ports  # noqa: E402

try:
    import certifi  # bundled CA list: HTTPS works even where the OS store is missing
    SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except (ImportError, OSError):
    SSL_CONTEXT = ssl.create_default_context()

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

# Executables keep data in the user's home folder (their own folder may be read-only).
DATA = Path.home() / "ShelterConsole-data" if FROZEN else APP / "data"
FIRMWARES = ("shelter-1", "shelter-2", "shelter-3", "shelter-4")
FIRMWARE = "shelter-4"
MIST_MAX_S = 30  # firmware hard limit for one continuous spray
ANNOUNCE_PORT = 5048  # boards broadcast {"shelter": role, ...} here once on WiFi
VID = {"climate": 0x1A86, "camera": 0x303A}  # CH34x on FireBeetle, native USB on DFR1154

# Board settings: key -> (label, unit, real default, demo default). None = not part of presets.
BOARD_SETTINGS = {
    "t_on": ("开始降温温度", "°C", 30, 26),
    "t_off": ("停止降温温度", "°C", 28, 25),
    "rh_max": ("湿度上限：低于才开始喷", "%", 75, 90),
    "rh_stop": ("湿度上限：达到就停", "%", 80, 95),
    "lux_day": ("变成白天：光照高于", "lux", 150, 50),
    "lux_night": ("变成夜晚：光照低于", "lux", 100, 30),
    "perch_mode": ("栖木判定方式（1 = 突然加压，0 = 固定阈值）", "", None, None),
    "fsr_jump": ("栖木压力：比平时突然多出", "ADC", None, None),
    "fsr_on": ("栖木压力：判定有鸟", "ADC", None, None),
    "fsr_off": ("栖木压力：判定离开", "ADC", None, None),
    "mist_s": ("每次喷雾", "秒", 20, 5),
    "pause_s": ("两次喷雾间隔", "秒", 60, 10),
    "linger_s": ("鸟离开后继续降温", "秒", 30, 15),
    "ai_required": ("需要 AI 确认有鸟（否=只看压力片）", "", None, None),
    "fallback": ("控制台断开时只看压力片", "", None, None),
    "water_req": ("没水禁止喷雾", "", None, None),
    "mist_hour_s": ("每小时最多累计喷雾", "秒", 300, 180),
}
DEFAULT_MODEL = "gemini-2.5-flash"  # tested on the relay 2026-10-29: correct and ~2 s per photo
HOST_DEFAULTS = {"ai_enabled": True, "ai_model": DEFAULT_MODEL, "confidence": 0.5, "ai_fail": "perch",
                 "location": "演示桌面", "visit_gap_s": 60, "language": "zh", "demo_bird": True}

AI_PROMPT = """You are the camera of a bird shelter in an Australian city.
Look at the photo and answer with JSON only:
{"bird": true|false, "count": int, "species_en": str, "species_zh": str,
 "confidence": 0.0-1.0, "is_model": true|false, "note": str}
- bird: true if a bird is visible. Realistic toy birds, models or printed/phone
  photos of birds used for testing also count; then set is_model true.
- species: the most likely species, preferring Australian urban birds. Use these
  exact names when they fit: rainbow lorikeet 彩虹吸蜜鹦鹉, noisy miner 噪钟吸蜜鸟,
  Australian magpie 澳洲喜鹊, sulphur-crested cockatoo 葵花鹦鹉, Australian white ibis
  澳洲白鹮, laughing kookaburra 笑翠鸟, common myna 家八哥, rock dove 原鸽,
  crested pigeon 冠鸠, galah 粉红凤头鹦鹉, red wattlebird 红垂蜜鸟, superb fairywren
  华丽细尾鹩莺, magpie-lark 鹊鹨, little corella 小凤头鹦鹉, welcome swallow 洋斑燕.
  For other birds give the common English name and the standard Chinese name.
  Photos shown on a phone or tablet screen count as birds (is_model true).
  Use "unknown" / "未知" if a bird is visible but the species is unclear,
  and "none" / "无" when there is no bird.
- confidence: your confidence that a bird is present.
- note: under 20 words, what you see."""

# The team's demo prop is not a realistic bird, so it is described explicitly.
DEMO_BIRD_PROMPT = """
Demo bird: the team tests with a stylised bird figure. It is solid black, stands
upright like a short column or pillar, and its side view has the outline of the
Twitter bird logo: a rounded body and head, a small pointed beak, and a short
raised tail or wing tip. It may be 3D printed, matte or glossy, seen from any side,
partly out of frame, or slightly blurred. When you see this figure:
bird true, is_model true, species_en "demo bird (black model)",
species_zh "演示鸟（黑色模型）", confidence 0.8 or higher if the bird outline is clear.
A black object without a bird outline (a hand, a phone, a cable, a box, a plain
cylinder, a shadow) is not the demo bird: bird false."""


def ai_prompt(demo_bird=True):
    return AI_PROMPT + (DEMO_BIRD_PROMPT if demo_bird else "")


def clean(value):
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {k: clean(v) for k, v in value.items()}
    if isinstance(value, list):
        return [clean(v) for v in value]
    return value


def ports():
    return [{"port": p.device, "description": p.description, "vid": p.vid}
            for p in list_ports.comports() if p.vid is not None]


def stamp(t=None):
    return dt.datetime.fromtimestamp(t or time.time()).strftime("%Y-%m-%d %H:%M:%S")


# ---------------- AI configuration (embedded bundle, .env, environment) ----------------
_MASK = b"shelter-comp5047"


def encode_bundle(config: dict) -> str:
    raw = json.dumps(config).encode()
    return base64.b64encode(bytes(b ^ _MASK[i % len(_MASK)] for i, b in enumerate(raw))).decode()


def decode_bundle(text: str) -> dict:
    raw = base64.b64decode(text.strip())
    return json.loads(bytes(b ^ _MASK[i % len(_MASK)] for i, b in enumerate(raw)))


def load_ai_config() -> dict:
    """Embedded shelter.key < .env next to the app or project < environment."""
    cfg = {}
    bundle = APP / "shelter.key"
    if bundle.exists():
        try:
            cfg.update(decode_bundle(bundle.read_text()))
        except (ValueError, OSError):
            pass
    candidates = [Path(sys.executable).parent / ".env"] if FROZEN else [APP / ".env", APP.parent / ".env"]
    for env in candidates:
        if env.exists():
            for line in env.read_text(encoding="utf-8").splitlines():
                key, sep, value = line.partition("=")
                if sep and key.strip() in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL") and value.strip():
                    cfg[key.strip()] = value.strip().strip('"').strip("'")
            break
    for key in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL"):
        if os.environ.get(key):
            cfg[key] = os.environ[key]
    cfg.setdefault("OPENAI_BASE_URL", "https://api.openai.com/v1")
    return cfg


def chat(cfg: dict, messages: list, max_tokens=400, timeout=45) -> str:
    if not cfg.get("OPENAI_API_KEY"):
        raise RuntimeError("未配置 AI 密钥")
    cfg = {**cfg, "OPENAI_MODEL": cfg.get("OPENAI_MODEL") or DEFAULT_MODEL}
    body = json.dumps({"model": cfg["OPENAI_MODEL"], "messages": messages,
                       "temperature": 0.2, "max_tokens": max_tokens}).encode()
    request = urllib.request.Request(cfg["OPENAI_BASE_URL"].rstrip("/") + "/chat/completions", body, {
        "Content-Type": "application/json", "Authorization": "Bearer " + cfg["OPENAI_API_KEY"]})
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=SSL_CONTEXT) as response:
            reply = json.loads(response.read())
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")[:200]
        raise RuntimeError(f"AI 服务返回 {error.code}: {detail}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise RuntimeError(f"连不上 AI 服务：{error}") from None
    try:
        return reply["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        raise RuntimeError("AI 返回格式异常") from None


def parse_ai_json(text: str) -> dict:
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        raise ValueError("AI 没有返回 JSON")
    data = json.loads(match.group(0))
    try:
        confidence = float(data.get("confidence", 0))
    except (TypeError, ValueError):
        confidence = 0.0
    return {"bird": bool(data.get("bird")), "count": int(data.get("count") or 0),
            "species_en": str(data.get("species_en") or "unknown")[:60],
            "species_zh": str(data.get("species_zh") or "未知")[:30],
            "confidence": max(0.0, min(1.0, confidence)),
            "is_model": bool(data.get("is_model")), "note": str(data.get("note") or "")[:160]}


def identify(cfg: dict, jpeg: bytes, demo_bird=True) -> dict:
    image = "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()
    text = chat(cfg, [{"role": "system", "content": ai_prompt(demo_bird)},
                      {"role": "user", "content": [
                          {"type": "text", "text": "Is there a bird on the perch? JSON only."},
                          {"type": "image_url", "image_url": {"url": image}}]}])
    return parse_ai_json(text)


# ---------------- visits ----------------
class Visits:
    """Group detections into visits: a new visit starts after `gap` seconds without a bird."""

    def __init__(self, path: Path):
        self.path = path
        self.items = []
        if path.exists():
            try:
                self.items = json.loads(path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                self.items = []

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.items, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    def seen(self, now, gap, species, confidence, source, photo, env, location, names=None):
        last = self.items[-1] if self.items else None
        if last and now - last["last_seen"] <= gap and last["location"] == location:
            last["last_seen"] = now
            last["end"] = max(last["end"], now)
            last["detections"] += 1
            if confidence >= last.get("confidence", 0) and species not in ("unknown", "none"):
                last.update(species=species, names=names, confidence=confidence, photo=photo or last.get("photo"))
        else:
            self.items.append({"id": len(self.items) + 1, "start": now, "end": now, "last_seen": now,
                               "species": species, "names": names, "confidence": confidence, "source": source,
                               "photo": photo, "detections": 1, "misted": False,
                               "location": location, **env})
        self.save()

    def left(self, now):
        if self.items and self.items[-1]["end"] < now and now - self.items[-1]["last_seen"] < 600:
            self.items[-1]["end"] = now
            self.save()

    def misted(self, now, gap):
        if self.items and now - self.items[-1]["end"] <= gap:
            self.items[-1]["misted"] = True
            self.save()


# ---------------- the console ----------------
class Console:
    def __init__(self, simulate=False):
        self.lock = threading.RLock()
        self.token = secrets.token_urlsafe(24)
        self.simulate = simulate
        self.links = {}
        self.latest = {"climate": {}, "camera": {}}
        self.received = {"climate": 0.0, "camera": 0.0}
        self.connection = {r: {"connected": False, "port": None, "via": None, "error": "等待设备"} for r in VID}
        self.found = {}  # role -> board seen on WiFi: ip, port, at, fw, rssi
        self.board_cfg = {}
        self.history = collections.deque(maxlen=120)
        self.logs = collections.deque(maxlen=200)
        self.ai = load_ai_config()
        DATA.mkdir(parents=True, exist_ok=True)
        (DATA / "photos").mkdir(exist_ok=True)
        self.settings_path = DATA / "settings.json"
        self.host = dict(HOST_DEFAULTS)
        if self.settings_path.exists():
            try:
                self.host.update(json.loads(self.settings_path.read_text(encoding="utf-8")))
            except (ValueError, OSError):
                pass
        self.visits = Visits(DATA / "visits.json")
        self.jpeg, self.photo_time, self.photo_name = None, 0.0, None
        self.photo_wait = threading.Event()
        self.busy = False
        self.pipeline = {"stage": "idle", "trigger": None, "at": None, "result": None, "error": None,
                         "decision": None}
        self.diary = {}
        diary = DATA / "diary.json"
        if diary.exists():
            try:
                self.diary = json.loads(diary.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                pass

    def ai_config(self):
        return {**self.ai, "OPENAI_MODEL": self.host.get("ai_model") or self.ai.get("OPENAI_MODEL") or DEFAULT_MODEL}

    # ---- logging and state ----
    def log(self, role, message):
        with self.lock:
            self.logs.append({"at": time.time(), "role": role, "text": str(message)[:500]})

    def fresh(self, role):
        return self.connection[role]["connected"] and time.time() - self.received[role] < 4

    def update(self, role, value):
        value = clean(value)
        if not isinstance(value, dict):
            return
        event = value.get("event")
        with self.lock:
            if not event:
                self.latest[role] = value
                self.received[role] = time.time()
                if role == "climate":
                    self.history.append({"at": time.time(), "t": value.get("t"), "rh": value.get("rh"),
                                         "lux": value.get("lux")})
                return
            if event == "cfg":
                self.board_cfg = {k: v for k, v in value.items() if k in BOARD_SETTINGS}
        self.log(role, json.dumps(value, ensure_ascii=False))
        if role == "climate" and event == "perch_on":
            self.check("perch")
        elif role == "climate" and event == "perch_off":
            self.visits.left(time.time())
        elif role == "climate" and event == "mist" and value.get("reason") == "cooling":
            self.visits.misted(time.time(), self.host["visit_gap_s"])

    def snapshot(self):
        now = time.time()
        with self.lock:
            return clean({
                "devices": {r: {**self.connection[r], "data": dict(self.latest[r]),
                                "age": now - self.received[r] if self.received[r] else None}
                            for r in VID},
                "firmware_ok": self.latest["climate"].get("fw") in (None,) + FIRMWARES,
                "wifi_seen": {r: {**f, "age": now - f["at"]} for r, f in self.found.items()},
                "board_cfg": self.board_cfg, "host": self.host, "settings_meta": BOARD_SETTINGS,
                "ai": {"configured": bool(self.ai.get("OPENAI_API_KEY")), "model": self.ai_config()["OPENAI_MODEL"]},
                "history": list(self.history)[-90:], "logs": list(self.logs)[-60:],
                "photo_at": self.photo_time, "pipeline": dict(self.pipeline), "busy": self.busy,
                "visits": self.visits.items[-30:][::-1], "diary": self.diary, "simulate": self.simulate,
            })

    def send(self, role, line):
        link = self.links.get(role)
        if not link or not self.connection[role]["connected"]:
            raise ValueError("主板未连接" if role == "climate" else "摄像头未连接")
        link.out.put(line)

    # ---- photo ----
    def received_jpeg(self, data: bytes):
        name = dt.datetime.now().strftime("%Y%m%d-%H%M%S") + ".jpg"
        (DATA / "photos" / name).write_bytes(data)
        with self.lock:
            self.jpeg, self.photo_time, self.photo_name = bytes(data), time.time(), name
        self.photo_wait.set()
        self.log("camera", f"照片已收到：{len(data)} bytes → data/photos/{name}")

    def capture(self, timeout=18):
        self.photo_wait.clear()
        self.send("camera", "p")
        if not self.photo_wait.wait(timeout):
            raise RuntimeError("拍照超时")
        return self.jpeg, self.photo_name

    # ---- the pipeline: perch -> photo -> AI -> tell the board ----
    def check(self, trigger):
        with self.lock:
            if self.busy:
                return False
            self.busy = True
            self.pipeline = {"stage": "photo", "trigger": trigger, "at": time.time(), "result": None,
                             "error": None, "decision": None, "photo": None}
        threading.Thread(target=self._check, args=(trigger,), daemon=True).start()
        return True

    def _stage(self, **values):
        with self.lock:
            self.pipeline.update(values)

    def _check(self, trigger):
        jpeg = name = result = None
        problems = []
        try:
            try:
                jpeg, name = self.capture()
                self._stage(photo=name)
            except (RuntimeError, ValueError) as error:
                problems.append(str(error))
            if jpeg and self.host["ai_enabled"] and self.ai.get("OPENAI_API_KEY"):
                self._stage(stage="ai")
                try:
                    result = identify(self.ai_config(), jpeg, bool(self.host["demo_bird"]))
                    self._stage(result=result)
                    self.log("ai", json.dumps(result, ensure_ascii=False))
                    with open(DATA / "detections.jsonl", "a", encoding="utf-8") as f:
                        f.write(json.dumps({"at": stamp(), "photo": name, "trigger": trigger, **result},
                                           ensure_ascii=False) + "\n")
                except (RuntimeError, ValueError) as error:
                    problems.append(str(error))
            elif not self.host["ai_enabled"]:
                problems.append("AI 识别已在设置中关闭")
            elif jpeg:
                problems.append("未配置 AI 密钥")

            if result is not None:
                bird = result["bird"] and result["confidence"] >= float(self.host["confidence"])
                decision, source = ("bird" if bird else "no_bird"), "ai"
            else:
                bird = self.host["ai_fail"] == "perch" and trigger == "perch"
                decision, source = ("fallback_bird" if bird else "fallback_none"), "perch"
            self._stage(stage="decide", decision=decision, error="；".join(problems) or None)
            try:
                self.send("climate", "BIRD 1" if bird else "BIRD 0")
            except ValueError as error:
                self._stage(error="；".join(problems + [str(error)]))
            if bird:
                d = self.latest["climate"]
                env = {k: d.get(k) for k in ("t", "rh", "lux")}
                species = (result or {}).get("species_" + ("zh" if self.host["language"] == "zh" else "en"),
                                             "未经 AI 确认" if self.host["language"] == "zh" else "unverified")
                # both names, so the console can show the visit in either interface language
                names = {"zh": result["species_zh"], "en": result["species_en"]} if result else None
                self.visits.seen(time.time(), float(self.host["visit_gap_s"]), species,
                                 (result or {}).get("confidence", 0.0), source, name, env,
                                 self.host["location"], names)
        finally:
            with self.lock:
                self.busy = False
                self.pipeline["stage"] = "done"

    # ---- settings and actions ----
    def save_host(self, values: dict):
        for key, value in values.items():
            if key not in HOST_DEFAULTS:
                continue
            if key == "confidence":
                value = max(0.0, min(1.0, float(value)))
            elif key == "visit_gap_s":
                value = max(5, min(3600, int(float(value))))
            elif key in ("ai_enabled", "demo_bird"):
                value = bool(value)
            elif key == "ai_fail" and value not in ("perch", "none"):
                continue
            elif key == "language" and value not in ("zh", "en"):
                continue
            elif key == "ai_model":
                value = re.sub(r"[^A-Za-z0-9._:/-]", "", str(value))[:60] or DEFAULT_MODEL
            elif key == "location":
                value = str(value).strip()[:40] or "未命名地点"
            self.host[key] = value
        self.settings_path.write_text(json.dumps(self.host, ensure_ascii=False, indent=1), encoding="utf-8")

    def set_board(self, values: dict):
        for key, value in values.items():
            if key in BOARD_SETTINGS:
                self.send("climate", f"SET {key} {float(value):g}")

    def set_wifi(self, ssid, password):
        ssid, password = str(ssid or "").strip(), str(password or "")
        if not ssid or len(ssid) > 32 or len(password) > 63 or any(c in ssid + password for c in "\r\n"):
            raise ValueError("WiFi 名称 1–32 个字符，密码最多 63 个字符")
        written = []
        for role in VID:
            if self.connection[role]["connected"]:
                for line in ("WIFISSID " + ssid, ("WIFIPASS " + password) if password else "WIFIPASS", "WIFI APPLY"):
                    self.send(role, line)
                written.append("主板" if role == "climate" else "摄像头")
        if not written:
            raise ValueError("先用 USB 连上板子，再写入 WiFi")
        return written

    def preset(self, which):
        index = 3 if which == "demo" else 2
        self.set_board({k: v[index] for k, v in BOARD_SETTINGS.items() if v[index] is not None})

    def calibrate_perch(self):
        """Sudden-press mode: the empty reading becomes the usual reading. Fixed mode: set thresholds above it."""
        raw = self.latest["climate"].get("fsr")
        if not isinstance(raw, int) or not self.fresh("climate"):
            raise ValueError("还没有栖木压力读数")
        if self.board_cfg.get("perch_mode", 0) >= 0.5:
            self.send("climate", "REBASE")
            return raw, "sudden"
        self.set_board({"fsr_off": min(4095, raw + 200), "fsr_on": min(4095, raw + 500)})
        return raw, "fixed"

    def write_diary(self):
        today = dt.date.today().isoformat()
        visits = [v for v in self.visits.items if stamp(v["start"]).startswith(today)]
        temps = [h["t"] for h in self.history if isinstance(h.get("t"), (int, float))]
        facts = {"date": today, "location": self.host["location"], "visits": [
            {"start": stamp(v["start"])[11:], "minutes": round((v["end"] - v["start"]) / 60, 1),
             "species": v["species"], "misted": v["misted"], "temp_c": v.get("t"), "rh": v.get("rh")}
            for v in visits], "recent_temp_range_c": [min(temps), max(temps)] if temps else None}
        language = "Chinese" if self.host["language"] == "zh" else "English"
        text = chat(self.ai_config(), [{"role": "system", "content":
                               f"You write the daily diary of a smart bird shelter. Write in {language}, "
                               "under 120 words, friendly and factual, as plain prose without field names "
                               "or code words. Use only the facts given; if there were no visits, say so."},
                              {"role": "user", "content": json.dumps(facts, ensure_ascii=False)}], max_tokens=300)
        self.diary = {"at": time.time(), "date": today, "text": text.strip(), "visits": len(visits)}
        (DATA / "diary.json").write_text(json.dumps(self.diary, ensure_ascii=False), encoding="utf-8")
        return self.diary


# ---------------- WiFi: discovery and a socket that looks like a serial port ----------------
class Discovery(threading.Thread):
    """Listens for the boards' UDP announcements so no IP address has to be typed in."""

    def __init__(self, console):
        super().__init__(daemon=True)
        self.console, self.stop = console, threading.Event()

    def run(self):
        c = self.console
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if hasattr(socket, "SO_REUSEPORT"):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            sock.bind(("", ANNOUNCE_PORT))
            sock.settimeout(1)
        except OSError as error:
            c.log("wifi", f"无法监听 WiFi 广播（UDP {ANNOUNCE_PORT}）：{error}；只能用 USB")
            return
        while not self.stop.is_set():
            try:
                data, (ip, _) = sock.recvfrom(2048)
                info = json.loads(data)
            except socket.timeout:
                continue
            except (OSError, ValueError):
                continue
            role = info.get("shelter") if isinstance(info, dict) else None
            if role in VID:
                with c.lock:
                    first = role not in c.found or c.found[role]["ip"] != ip
                    c.found[role] = {"ip": ip, "port": int(info.get("port", 5049)), "at": time.time(),
                                     "fw": str(info.get("fw", "")), "rssi": info.get("rssi")}
                if first:
                    c.log("wifi", f"在 WiFi 上发现{'主板' if role == 'climate' else '摄像头'}：{ip}")


class NetDevice:
    """TCP connection to a board with the small part of the pyserial API that Link uses."""

    def __init__(self, ip, port):
        self.sock = socket.create_connection((ip, port), timeout=3)
        self.sock.settimeout(.2)
        self.buf = bytearray()
        self.is_open = True

    def _fill(self):
        try:
            data = self.sock.recv(65536)
        except socket.timeout:
            return
        if not data:
            raise OSError("WiFi 连接已断开")
        self.buf.extend(data)

    def _take(self, n):
        out = bytes(self.buf[:n])
        del self.buf[:n]
        return out

    def read_until(self, terminator=b"\n", size=4096):
        deadline = time.monotonic() + .2
        while True:
            i = self.buf.find(terminator)
            if i >= 0:
                return self._take(i + len(terminator))
            if len(self.buf) >= size or time.monotonic() > deadline:
                return self._take(min(size, len(self.buf)))
            self._fill()

    def read(self, n):
        deadline = time.monotonic() + .2
        while len(self.buf) < n and time.monotonic() < deadline:
            self._fill()
        return self._take(n)

    def write(self, data):
        self.sock.sendall(data)

    def close(self):
        self.is_open = False
        self.sock.close()


# ---------------- links to the boards (USB serial or WiFi) ----------------
class Link(threading.Thread):
    def __init__(self, console, role, desired=None):
        super().__init__(daemon=True)
        self.console, self.role, self.desired = console, role, desired
        self.out = queue.SimpleQueue()
        self.reconnect = threading.Event()
        self.stop = threading.Event()
        console.links[role] = self

    def wifi(self):
        found = self.console.found.get(self.role)
        if found and time.time() - found["at"] < 10:
            return ("wifi", (found["ip"], found["port"]))
        return None

    def choose(self):
        """USB if the board is plugged in, otherwise WiFi if it is announcing itself."""
        if self.desired == "wifi":
            return self.wifi()
        if self.desired:
            return ("usb", self.desired)
        port = next((p["port"] for p in ports() if p["vid"] == VID[self.role]), None)
        return ("usb", port) if port else self.wifi()

    def read_jpeg(self, device, line):
        length = int(line.split()[1])
        if not 100 <= length <= 2_000_000:
            raise ValueError("照片长度无效")
        data = bytearray()
        deadline = time.monotonic() + 15
        while len(data) < length and time.monotonic() < deadline and not self.stop.is_set():
            data.extend(device.read(length - len(data)))
        if len(data) != length or not data.startswith(b"\xff\xd8") or not data.endswith(b"\xff\xd9"):
            self.console.log("camera", "照片接收不完整")
            return
        self.console.received_jpeg(bytes(data))

    def run(self):
        c = self.console
        while not self.stop.is_set():
            target = self.choose()
            if not target:
                with c.lock:
                    c.connection[self.role] = {"connected": False, "port": None, "via": None,
                                               "error": "未找到：插 USB，或等板子连上 WiFi"}
                self.stop.wait(1)
                continue
            kind, where = target
            port = where if kind == "usb" else f"{where[0]}:{where[1]}"
            device = None
            try:
                if kind == "usb":
                    options = {"exclusive": True} if os.name == "posix" else {}
                    device = serial.Serial(port=None, baudrate=115200, timeout=.2, write_timeout=1, **options)
                    device.dtr = False
                    device.rts = False
                    device.port = where
                    device.open()
                else:
                    device = NetDevice(*where)
                self.reconnect.clear()
                while not self.out.empty():
                    self.out.get_nowait()
                via = "USB" if kind == "usb" else "WiFi"
                with c.lock:
                    c.connection[self.role] = {"connected": True, "port": port, "via": via, "error": None}
                c.log(self.role, f"已连接（{via}）：{port}")
                device.write(b"CFG?\nSTATUS\n" if self.role == "climate" else b"?\n")
                last_ping, pending = time.monotonic(), bytearray()
                while not self.stop.is_set() and not self.reconnect.is_set():
                    while not self.out.empty():
                        command = self.out.get_nowait()
                        device.write((command + "\n").encode())
                        if command.startswith("WIFIPASS"):
                            c.log(self.role, "> WIFIPASS ******")
                        elif command not in ("HB", "?"):
                            c.log(self.role, "> " + command)
                    if time.monotonic() - last_ping > (2 if self.role == "climate" else 4):
                        device.write(b"HB\n" if self.role == "climate" else b"?\n")
                        last_ping = time.monotonic()
                    raw = device.read_until(b"\n", 4096)
                    if not raw:
                        continue
                    pending.extend(raw)
                    if len(pending) > 8192:
                        pending.clear()
                        continue
                    if not pending.endswith(b"\n"):
                        continue
                    line = pending.decode("utf-8", errors="replace").strip()
                    pending.clear()
                    if self.role == "camera" and line.startswith("JPEG_BEGIN "):
                        self.read_jpeg(device, line)
                        continue
                    try:
                        c.update(self.role, json.loads(line))
                    except ValueError:
                        if line:
                            c.log(self.role, line)
            except (serial.SerialException, OSError, ValueError) as error:
                message = str(error)
                if "busy" in message.lower() or "lock" in message.lower() or "denied" in message.lower():
                    message = "串口被其他程序占用（关闭串口监视器/其他控制台后会自动重连）"
                with c.lock:
                    c.connection[self.role] = {"connected": False, "port": port, "via": None, "error": message}
                c.log(self.role, message)
            finally:
                if device and device.is_open:
                    try:
                        device.write(b"BIRD 0\n" if self.role == "climate" else b"\n")
                    except (serial.SerialException, OSError):
                        pass
                    try:
                        device.close()
                    except OSError:
                        pass
                with c.lock:
                    c.connection[self.role]["connected"] = False
                self.stop.wait(1)


# ---------------- simulator (no hardware) ----------------
class SimClimate(threading.Thread):
    """Mimics the shelter-4 firmware closely enough to rehearse the whole flow."""

    def __init__(self, console):
        super().__init__(daemon=True)
        self.console = console
        self.out = queue.SimpleQueue()
        self.stop, self.reconnect, self.desired = threading.Event(), threading.Event(), None
        console.links["climate"] = self
        self.cfg = {"t_on": 30, "t_off": 28, "rh_max": 75, "rh_stop": 80, "lux_day": 150, "lux_night": 100,
                    "perch_mode": 1, "fsr_jump": 300, "fsr_on": 600, "fsr_off": 300,
                    "mist_s": 20, "pause_s": 60, "linger_s": 30, "ai_required": 1, "fallback": 1,
                    "water_req": 1, "mist_hour_s": 300}
        self.t, self.rh, self.lux, self.wet, self.fsr = 24.0, 55.0, 400.0, True, 40
        self.base = float(self.fsr)
        self.perch = self.confirmed = self.cooling = self.mist = self.day = False
        self.present_until = self.mist_until = self.next_pulse = 0.0
        self.sprayed = collections.deque()  # one entry per 0.1 s of spraying, for the hourly cap
        self.last_host = time.time()

    def emit(self, value):
        self.console.update("climate", value)

    def used_s(self, now):
        while self.sprayed and self.sprayed[0] < now - 3600:
            self.sprayed.popleft()
        return len(self.sprayed) / 10

    def start_mist(self, now, seconds, reason):
        if not (self.wet or not self.cfg["water_req"]) or self.used_s(now) + 1 > self.cfg["mist_hour_s"]:
            return False
        seconds = min(seconds, MIST_MAX_S, self.cfg["mist_hour_s"] - self.used_s(now))
        self.mist, self.mist_until = True, now + seconds
        self.emit({"event": "mist", "on": 1, "ms": int(seconds * 1000), "reason": reason})
        return True

    def handle(self, line):
        self.last_host = time.time()
        parts = line.split()
        if line == "CFG?":
            self.emit({"event": "cfg", **self.cfg})
        elif line == "STOP":
            self.cooling = self.mist = False
        elif line == "REBASE":
            if self.perch:
                self.emit({"event": "perch_off", "fsr": self.fsr})
            self.base, self.perch = float(self.fsr), False
            self.emit({"event": "ack", "cmd": "REBASE", "base": self.fsr})
        elif parts[:1] == ["BIRD"]:
            self.confirmed = parts[1] == "1"
            if not self.confirmed:
                self.present_until = 0
        elif parts[:1] == ["MIST"]:
            if not self.start_mist(time.time(), min(int(parts[1]), 3000) / 1000, "manual"):
                self.emit({"event": "rejected", "reason": "no water or hourly mist limit reached"})
        elif parts[:1] == ["SET"] and parts[1] in self.cfg:
            self.cfg[parts[1]] = float(parts[2])
            self.emit({"event": "cfg", **self.cfg})

    def run(self):
        c = self.console
        with c.lock:
            c.connection["climate"] = {"connected": True, "port": "模拟主板", "via": "模拟", "error": None}
        self.emit({"event": "cfg", **self.cfg})
        last = 0
        while not self.stop.is_set():
            while not self.out.empty():
                self.handle(self.out.get_nowait())
            now = time.time()
            rise, jump = self.fsr - self.base, self.cfg["fsr_jump"]
            sudden = self.cfg["perch_mode"] >= .5
            if not self.perch and (rise >= jump if sudden else self.fsr > self.cfg["fsr_on"]):
                self.perch, self.confirmed = True, False
                self.emit({"event": "perch_on", "fsr": self.fsr, "base": int(self.base)})
            elif not self.perch and rise < jump:  # the usual reading follows slow changes only
                self.base += rise * min(1.0, .1 / (20 if rise > 0 else 1))
            elif self.perch and (rise < jump / 2 if sudden else self.fsr < self.cfg["fsr_off"]):
                self.perch = False
                self.emit({"event": "perch_off", "fsr": self.fsr})
            if self.perch and (self.confirmed or not self.cfg["ai_required"]):
                self.present_until = now + self.cfg["linger_s"] + 1
            bird = now < self.present_until
            if self.lux > self.cfg["lux_day"]:
                self.day = True
            elif self.lux < self.cfg["lux_night"]:
                self.day = False
            day = self.day
            ok = bird and day and (self.wet or not self.cfg["water_req"])
            if not self.cooling and ok and self.t >= self.cfg["t_on"] and self.rh < self.cfg["rh_max"]:
                self.cooling, self.next_pulse = True, now
            if self.cooling and not (ok and self.t > self.cfg["t_off"] and self.rh < self.cfg["rh_stop"]):
                self.cooling = self.mist = False
            if self.cooling and not self.mist and now >= self.next_pulse:
                if not self.start_mist(now, self.cfg["mist_s"], "cooling"):
                    self.emit({"event": "mist_limit", "used_s": self.used_s(now), "max_s": self.cfg["mist_hour_s"]})
                self.next_pulse = now + self.cfg["mist_s"] + self.cfg["pause_s"]
            if self.mist and (now >= self.mist_until or not self.wet or self.used_s(now) >= self.cfg["mist_hour_s"]):
                self.mist = False
            if self.mist:  # evaporative cooling, crudely
                self.sprayed.append(now)
                self.t, self.rh = self.t - 0.02, min(99, self.rh + 0.05)
            if now - last >= .75:
                last = now
                self.emit({"fw": FIRMWARE, "ip": "", "rssi": 0, "t": round(self.t, 2), "rh": round(self.rh, 1), "lux": self.lux,
                           "water": int(self.wet), "water_raw": 0 if self.wet else 1, "fsr": self.fsr,
                           "fsr_base": int(self.base), "perch": int(self.perch), "confirmed": int(self.confirmed),
                           "bird": int(bird), "day": int(day), "cooling": int(self.cooling), "mist": int(self.mist),
                           "mist_hour_s": round(self.used_s(now), 1),
                           "mist_limit": int(self.used_s(now) + 1 > self.cfg["mist_hour_s"]),
                           "host": 1, "uptime_ms": int(now * 1000) % 10**9})
            self.stop.wait(.1)


class SimCamera(threading.Thread):
    def __init__(self, console):
        super().__init__(daemon=True)
        self.console = console
        self.out = queue.SimpleQueue()
        self.stop, self.reconnect, self.desired = threading.Event(), threading.Event(), None
        self.image = None
        console.links["camera"] = self

    def run(self):
        c = self.console
        with c.lock:
            c.connection["camera"] = {"connected": True, "port": "模拟摄像头", "via": "模拟", "error": None}
        while not self.stop.is_set():
            c.update("camera", {"fw": "simulated-camera", "camera_ready": True})
            while not self.out.empty():
                if self.out.get_nowait() == "p":
                    sample = self.image or (APP / "sample.jpg").read_bytes()
                    time.sleep(.8)
                    c.received_jpeg(sample)
            self.stop.wait(.3)


# ---------------- web page ----------------
def load_page():
    return (APP / "console.html").read_text(encoding="utf-8")


def handler_for(console):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def reply(self, status, data, mime="application/json; charset=utf-8"):
            if mime.startswith("application/json"):
                data = json.dumps(clean(data), ensure_ascii=False, allow_nan=False).encode()
            elif isinstance(data, str):
                data = data.encode()
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            path = urlparse(self.path).path
            if path == "/":
                self.reply(200, load_page().replace("__TOKEN__", console.token), "text/html; charset=utf-8")
            elif path == "/api/state":
                self.reply(200, console.snapshot())
            elif path == "/api/ports":
                self.reply(200, ports())
            elif path == "/api/photo":
                jpeg = console.jpeg
                self.reply(200, jpeg, "image/jpeg") if jpeg else self.reply(404, {"error": "尚未拍照"})
            elif path.startswith("/photos/") and re.fullmatch(r"/photos/[0-9-]+\.jpg", path):
                file = DATA / "photos" / path.split("/")[-1]
                self.reply(200, file.read_bytes(), "image/jpeg") if file.exists() else self.reply(404, {})
            else:
                self.reply(404, {"error": "not found"})

        def do_POST(self):
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size < 3_000_000:
                    raise ValueError("请求大小无效")
                body = json.loads(self.rfile.read(size))
                if not isinstance(body, dict) or body.get("token") != console.token:
                    return self.reply(403, {"error": "请刷新页面"})
                action, value = body.get("action"), body.get("value")
                result = {"ok": True}
                if action == "check":
                    if not console.check("manual"):
                        raise ValueError("正在识别上一张照片，请稍等")
                elif action == "mist_test":
                    console.send("climate", "MIST 2000")
                elif action == "stop":
                    console.send("climate", "STOP")
                elif action == "board_settings" and isinstance(value, dict):
                    console.set_board(value)
                elif action == "host_settings" and isinstance(value, dict):
                    console.save_host(value)
                elif action == "preset" and value in ("demo", "real"):
                    console.preset(value)
                elif action == "calibrate":
                    result["raw"], result["mode"] = console.calibrate_perch()
                elif action == "diary":
                    result["diary"] = console.write_diary()
                elif action == "wifi" and isinstance(value, dict):
                    result["written"] = console.set_wifi(value.get("ssid"), value.get("password"))
                elif action == "port" and body.get("role") in VID:
                    if value not in (None, "", "wifi") and value not in {p["port"] for p in ports()}:
                        raise ValueError("请选择列表中的连接方式")
                    link = console.links[body["role"]]
                    link.desired = value or None
                    link.reconnect.set()
                elif action == "sim" and console.simulate:
                    sim = console.links["climate"]
                    changes = {"press": ("fsr", 2000), "release": ("fsr", 40), "hot": ("t", sim.t + 4),
                               "cool": ("t", sim.t - 4), "dark": ("lux", 5.0), "light": ("lux", 400.0),
                               "water": ("wet", True), "dry": ("wet", False)}
                    if value not in changes:
                        raise ValueError("未知模拟操作")
                    setattr(sim, *changes[value])
                elif action == "sim_photo" and console.simulate and isinstance(value, str):
                    console.links["camera"].image = base64.b64decode(value.split(",")[-1])
                else:
                    raise ValueError("未知操作")
                self.reply(200, result)
            except (ValueError, TypeError, KeyError, json.JSONDecodeError, RuntimeError) as error:
                self.reply(400, {"error": str(error)})
    return Handler


def main():
    parser = argparse.ArgumentParser(description="Shelter console")
    parser.add_argument("--port", type=int, default=5047, help="网页端口（默认 5047）")
    parser.add_argument("--simulate", action="store_true", help="没有硬件时用模拟板子演练")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    console = Console(simulate=args.simulate)
    links = ([SimClimate(console), SimCamera(console)] if args.simulate
             else [Discovery(console), Link(console, "climate"), Link(console, "camera")])
    try:
        server = ThreadingHTTPServer(("127.0.0.1", args.port), handler_for(console))
    except OSError:
        url = f"http://127.0.0.1:{args.port}"
        print(f"端口 {args.port} 已被占用：控制台可能已经在运行，打开 {url} 即可。")
        if not args.no_browser:
            webbrowser.open(url)
        return
    for link in links:
        link.start()
    url = f"http://127.0.0.1:{args.port}"
    ai = "已配置" if console.ai.get("OPENAI_API_KEY") else "未配置（只用压力片判断）"
    print(f"Shelter 控制台已启动：{url}\nAI：{ai}　模式：{'模拟' if args.simulate else 'USB / WiFi 硬件'}\n"
          "保持此窗口打开；关闭窗口或按 Ctrl+C 退出。", flush=True)
    if not args.no_browser:
        threading.Timer(.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        for link in links:
            link.stop.set()
        server.server_close()


if __name__ == "__main__":
    main()
