#!/usr/bin/env python3
"""Shelter hardware test bench. Run with .venv/bin/python hardware_test.py.

Only dependency: pyserial. Uses a local browser for bars, charts and photos.
No API calls, cloud connections, AI credentials or business logic are used.
"""
from __future__ import annotations

import argparse
import collections
import json
import math
import queue
import secrets
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    raise SystemExit("缺少 pyserial。请使用项目 .venv/bin/python 运行，或执行 python3 -m pip install pyserial。")


ROOT = Path(__file__).resolve().parent


def clean(value):
    """Keep invalid/missing measurements unknown instead of displaying zero."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {k: clean(v) for k, v in value.items()}
    if isinstance(value, list):
        return [clean(v) for v in value]
    return value


def ports():
    return [{"port": p.device, "description": p.description, "vid": p.vid,
             "pid": p.pid} for p in list_ports.comports() if p.vid is not None]


class Bench:
    def __init__(self):
        self.lock = threading.RLock()
        self.token = secrets.token_urlsafe(24)
        self.links = {}
        self.latest = {"climate": {}, "camera": {}}
        self.connection = {r: {"connected": False, "port": None, "error": "等待 USB 设备"}
                           for r in ("climate", "camera")}
        self.received = {"climate": 0.0, "camera": 0.0}
        self.history = collections.deque(maxlen=90)
        self.logs = collections.deque(maxlen=180)
        self.jpeg = None
        self.photo_time = 0.0
        self.photo_busy = False
        self.photo_requested = 0.0

    def log(self, role, message):
        with self.lock:
            self.logs.append({"at": time.time(), "role": role, "text": str(message)[:600]})

    def update(self, role, value):
        value = clean(value)
        if not isinstance(value, dict):
            return
        with self.lock:
            if "event" not in value:
                self.latest[role] = value
                self.received[role] = time.time()
                if role == "climate":
                    self.history.append({"at": time.time(), **value})
            self.log(role, json.dumps(value, ensure_ascii=False, allow_nan=False))

    def snapshot(self):
        now = time.time()
        with self.lock:
            if self.photo_busy and now - self.photo_requested > 18:
                self.photo_busy = False
                self.log("camera", "拍照超时；检查摄像头固件和串口")
            return clean({"devices": {r: {**self.connection[r], "data": dict(self.latest[r]),
                                          "age": now - self.received[r] if self.received[r] else None}
                                       for r in ("climate", "camera")},
                          "history": list(self.history), "logs": list(self.logs),
                          "photo_at": self.photo_time, "photo_busy": self.photo_busy})

    def command(self, role, action, value=None):
        with self.lock:
            if role not in self.links or not self.connection[role]["connected"]:
                raise ValueError("对应的板子尚未连接 USB 串口")
            if action == "port":
                if value and value not in {p["port"] for p in ports()}:
                    raise ValueError("请选择列表中存在的串口")
                self.links[role].desired = value or None
                self.links[role].reconnect.set()
                return
            if role == "camera":
                commands = {"photo": "p", "status": "?", "led": "l",
                            "mic_on": "m", "mic_off": "n", "speaker": "b", "ir": "i"}
                if action not in commands:
                    raise ValueError("未知摄像头操作")
                if action not in ("photo", "status") and not self.latest["camera"].get("audio_supported"):
                    raise ValueError("此按钮需要先上传新的摄像头配件测试固件")
                if action != "status" and self.photo_busy:
                    raise ValueError("正在接收照片，请稍等")
                if action == "photo":
                    self.photo_busy = True
                    self.photo_requested = time.time()
                self.links[role].out.put(commands[action])
                return
            state = self.latest["climate"]
            if state.get("fw") != "hardware-test-2":
                raise ValueError("FireBeetle 需要先上传 hardware_test 测试固件；当前只能查看已有读数")
            commands = {"scan": "SCAN", "led": "LED", "stop": "STOP", "status": "STATUS"}
            if action in commands:
                command = commands[action]
            elif action == "pins" and value in ("21,22", "22,21"):
                command = "PINS " + value.replace(",", " ")
            elif action in ("water", "fsr", "arm") and type(value) is bool:
                if action == "arm" and value:
                    if time.time() - self.received["climate"] > 3:
                        raise ValueError("等待主板实时连接后，再确认雾化测试准备")
                command = {"water": "WATER", "fsr": "FSR", "arm": "ARM"}[action] + (" 1" if value else " 0")
            elif action == "mist":
                if time.time() - self.received["climate"] > 3 or not state.get("armed"):
                    raise ValueError("雾化测试未准备好：需要主板在线，并手动确认已加水、棉芯湿润")
                command = "MIST 1000"
            else:
                raise ValueError("未知操作或参数")
            self.links[role].out.put(command)


class Link(threading.Thread):
    def __init__(self, bench, role, desired=None):
        super().__init__(daemon=True)
        self.bench, self.role, self.desired = bench, role, desired
        self.out = queue.SimpleQueue()
        self.reconnect = threading.Event()
        self.stop = threading.Event()
        bench.links[role] = self

    def choose(self):
        if self.desired:
            return self.desired
        wanted = 0x1A86 if self.role == "climate" else 0x303A
        return next((p["port"] for p in ports() if p["vid"] == wanted), None)

    def read_jpeg(self, device, line):
        length = int(line.split()[1])
        if not 100 <= length <= 2_000_000:
            raise ValueError("摄像头返回的图片长度无效")
        data = bytearray()
        deadline = time.monotonic() + 15
        while len(data) < length and time.monotonic() < deadline and not self.stop.is_set():
            data.extend(device.read(length - len(data)))
        if len(data) != length or not data.startswith(b"\xff\xd8") or not data.endswith(b"\xff\xd9"):
            raise ValueError("图片接收不完整，请重试")
        folder = ROOT / "data" / "hardware_test"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "latest.jpg").write_bytes(data)
        with self.bench.lock:
            self.bench.jpeg = bytes(data)
            self.bench.photo_time = time.time()
            self.bench.photo_busy = False
        self.bench.log("camera", f"真实照片已收到：{len(data)} bytes，保存至 data/hardware_test/latest.jpg")

    def run(self):
        while not self.stop.is_set():
            port = self.choose()
            if not port:
                self.stop.wait(1)
                continue
            device = None
            try:
                device = serial.Serial(port=None, baudrate=115200, timeout=.2, write_timeout=1, exclusive=True)
                device.dtr = False
                device.rts = False
                device.port = port
                device.open()
                self.reconnect.clear()
                # Never replay commands queued before a USB reconnection.
                while not self.out.empty():
                    self.out.get_nowait()
                with self.bench.lock:
                    self.bench.connection[self.role] = {"connected": True, "port": port, "error": None}
                self.bench.log(self.role, f"串口已打开：{port} @ 115200")
                if self.role == "climate":
                    device.write(b"STOP\nSTATUS\n")
                else:
                    device.write(b"?\n")
                last_query = time.monotonic()
                pending = bytearray()
                ready = False
                while not self.stop.is_set() and not self.reconnect.is_set():
                    while ready and not self.out.empty():
                        command = self.out.get_nowait()
                        device.write((command + "\n").encode())
                        self.bench.log(self.role, "> " + command)
                    if self.role == "camera" and time.monotonic() - last_query > 4:
                        device.write(b"?\n")
                        last_query = time.monotonic()
                    raw = device.read_until(b"\n", 4096)
                    if not raw:
                        continue
                    pending.extend(raw)
                    if len(pending) > 8192:
                        pending.clear()
                        self.bench.log(self.role, "串口文本帧过长，已跳过")
                        continue
                    if not pending.endswith(b"\n"):
                        continue
                    line = pending.decode("utf-8", errors="replace").strip()
                    pending.clear()
                    if self.role == "camera" and line.startswith("JPEG_BEGIN "):
                        self.read_jpeg(device, line)
                        continue
                    try:
                        value = json.loads(line)
                        self.bench.update(self.role, value)
                        if not ready and isinstance(value, dict) and "event" not in value:
                            ready = True
                            if self.role == "climate":
                                device.write(b"STOP\nSTATUS\n")
                    except ValueError:
                        if line:
                            self.bench.log(self.role, line)
            except (serial.SerialException, OSError, ValueError) as error:
                message = str(error)
                if "Resource busy" in message or "exclusively lock" in message:
                    message = "串口被占用：在 VS Code Monitor 按 Ctrl+C 退出，再等待自动重连"
                with self.bench.lock:
                    self.bench.connection[self.role] = {"connected": False, "port": port, "error": message}
                    if self.role == "camera":
                        self.bench.photo_busy = False
                self.bench.log(self.role, message)
            finally:
                if device and device.is_open:
                    try:
                        device.write(b"STOP\n" if self.role == "climate" else b"n\n")
                    except (serial.SerialException, OSError):
                        pass
                    device.close()
                with self.bench.lock:
                    self.bench.connection[self.role]["connected"] = False
                self.stop.wait(1)


HTML = r'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Shelter 配件测试台</title><style>
:root{color-scheme:light;--bg:#f3f5ef;--card:#fff;--ink:#17352c;--muted:#68786f;--line:#dce5da;--green:#2c7755;--amber:#b37420;--red:#bd4639}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}main{max-width:1390px;margin:auto;padding:30px}header{display:flex;justify-content:space-between;align-items:center;gap:20px;margin-bottom:22px}.eyebrow{font-size:11px;letter-spacing:3px;color:var(--green);font-weight:800}h1{font-size:30px;line-height:1.2;margin:7px 0}h2{font-size:18px;margin:0}p{margin:8px 0}.muted,.tech{color:var(--muted);font-size:13px}.pill{border-radius:30px;background:#e6eee2;padding:5px 12px;font-size:12px}.grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px}.card{background:var(--card);border:1px solid var(--line);border-radius:18px;padding:22px;min-width:0}.head{display:flex;align-items:center;justify-content:space-between;gap:8px}.state{font-size:12px;color:var(--amber)}.ok{color:var(--green)}.bad{color:var(--red)}.value{font-size:38px;font-weight:650;line-height:1.4;letter-spacing:-1px}.unit{font-size:16px;color:var(--muted);letter-spacing:0}.meter{height:13px;background:#eaf0e7;border-radius:12px;overflow:hidden;margin:8px 0}.fill{height:100%;width:0;background:linear-gradient(90deg,#78b690,#2b7251);border-radius:12px;transition:width .3s}.amber{background:linear-gradient(90deg,#efd69b,#c08a33)}details{border-top:1px solid var(--line);margin-top:18px;padding-top:12px;font-size:13px}summary{cursor:pointer;font-weight:600}.pin{font:12px/1.8 ui-monospace,monospace;background:#f2f5ef;padding:9px 12px;border-radius:9px;margin:8px 0;white-space:pre-wrap}button,select{font:inherit;border:1px solid var(--line);border-radius:9px;padding:8px 12px;background:white;color:var(--ink);cursor:pointer}button:hover{background:#edf3e9}button.primary{background:var(--green);color:white;border-color:var(--green)}button.danger{color:var(--red);border-color:#edc9c2}button:disabled{opacity:.4;cursor:not-allowed}.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:10px 0}label{font-size:13px;display:block;margin:9px 0}input{accent-color:var(--green)}canvas{width:100%;height:85px;margin-top:8px}.wide{grid-column:span 2}.camera{background:#edf2e9;border-radius:12px;min-height:190px;display:flex;align-items:center;justify-content:center;overflow:hidden;margin:12px 0}.camera img{max-width:100%;max-height:350px;object-fit:contain}.camera .empty{padding:25px;color:var(--muted)}.devices{display:flex;gap:15px;flex-wrap:wrap;margin:18px 0 22px}.device{flex:1;min-width:270px;border-left:3px solid var(--green);padding-left:12px}.device select{max-width:100%;font-size:12px}.logs{height:220px;overflow:auto;background:#10281f;color:#bdd5bf;padding:15px;border-radius:12px;white-space:pre-wrap;font:12px/1.7 ui-monospace,monospace}.note{background:#fff5dd;border:1px solid #ebd79f;padding:12px 16px;border-radius:12px;margin:14px 0;color:#755823}.toast{position:fixed;bottom:24px;left:50%;transform:translateX(-50%);padding:12px 20px;background:#17352c;color:white;border-radius:10px;z-index:10;display:none;max-width:90vw}.tests{display:flex;gap:16px;flex-wrap:wrap;margin-top:8px}.tests label{margin:0}.wiring table{border-collapse:collapse;width:100%;font-size:13px}.wiring td,.wiring th{padding:9px;border-bottom:1px solid var(--line);text-align:left}.footer{margin:20px 0;color:var(--muted);font-size:12px}.stale{opacity:.55}.badge{background:#eef3e9;padding:3px 8px;border-radius:6px;font-size:11px}.signal{font-size:22px;font-weight:650}a{color:var(--green)}@media(max-width:1000px){.grid{grid-template-columns:repeat(2,minmax(0,1fr))}.wide{grid-column:span 2}}@media(max-width:640px){main{padding:16px}.grid{grid-template-columns:1fr}.wide{grid-column:span 1}header{align-items:flex-start}h1{font-size:25px}.value{font-size:32px}}
</style><main><header><div><div class="eyebrow">SHELTER · COMPONENT LAB</div><h1>配件测试台</h1><div class="muted">先确认每个部件能读、能响应，再组合项目功能。</div></div><div class="pill">本机串口 · 无 AI 调用</div></header>
<div class="devices"><div class="device"><strong>FireBeetle 主板</strong> <span id="climateState" class="state">等待连接</span><div id="climateInfo" class="tech">—</div><div class="row"><select id="climatePort"><option value="">自动识别 USB</option></select><button onclick="choosePort('climate')">重连</button><button onclick="act('climate','led')">板载 LED 闪一下</button></div></div><div class="device"><strong>DFR1154 摄像头板</strong> <span id="cameraState" class="state">等待连接</span><div id="cameraInfo" class="tech">—</div><div class="row"><select id="cameraPort"><option value="">自动识别 USB</option></select><button onclick="choosePort('camera')">重连</button></div></div></div>
<div id="firmwareNote" class="note" hidden>FireBeetle 当前运行旧固件。独立配件测试按钮需要先上传新版 <b>hardware_test</b> 固件。</div>
<div class="grid">
<section class="card sensor"><div class="head"><h2>温度与湿度</h2><span id="shtState" class="state">等待数据</span></div><div class="tech">SHT31-F · DFRobot SEN0334</div><div class="value"><span id="temp">—</span> <span class="unit">°C</span></div><div class="row"><span>湿度 <b id="humidity">—</b> % RH</span></div><div class="meter"><div id="humidityBar" class="fill"></div></div><canvas id="tempChart"></canvas><details><summary>型号、接线、工作方式</summary><div class="pin" id="shtPins">VCC→3V3  GND→GND<br>SDA→GPIO21  SCL→GPIO22</div><p>I2C 数字传感器，地址 0x44 或 0x45。温湿度数据带 CRC 校验，校验失败显示“—”。曲线为最近 90 条温度数据。</p><p>测试：用手靠近模块观察温度变化；不要把水滴到模块上。</p><a href="https://wiki.dfrobot.com/sen0334/" target="_blank" rel="noreferrer">官方资料</a></details></section>
<section class="card sensor"><div class="head"><h2>光照</h2><span id="luxState" class="state">等待数据</span></div><div class="tech">BH1750 · DFRobot SEN0097</div><div class="value"><span id="lux">—</span> <span class="unit">lux</span></div><div class="meter"><div id="luxBar" class="fill amber"></div></div><div class="tech">对数条：0 → 10 → 100 → 1,000 → 10,000 lux</div><canvas id="luxChart"></canvas><div id="lux2" class="tech">第二块 BH1750：未检测到 0x5C</div><details><summary>型号、接线、工作方式</summary><div class="pin" id="lightPins">VCC→3V3  GND→GND<br>SDA→GPIO21  SCL→GPIO22</div><p>I2C 光照计，默认地址 0x23，另一个可选地址为 0x5C。不能把两块同地址模块同时并在同一总线上，先逐块测试即可。</p><p>测试：遮住芯片后条形应降低，移开手或照灯后升高。没读到数据时条形留空。</p><a href="https://wiki.dfrobot.com/sen0097/" target="_blank" rel="noreferrer">官方资料</a></details></section>
<section class="card sensor"><div class="head"><h2>压力片</h2><span id="fsrState" class="state">未确认接线</span></div><div class="tech">Interlink FSR 402 · 电阻随受力变化</div><div class="value"><span id="fsr">—</span> <span class="unit">/ 4095</span></div><div class="meter"><div id="fsrBar" class="fill"></div></div><div class="tech">ADC 电压 <span id="fsrMv">—</span> mV · 相对基线 <span id="fsrDelta">—</span></div><div class="row"><button onclick="setBaseline()">记录空载基线</button></div><label><input id="fsrEnabled" type="checkbox" onchange="act('climate','fsr',this.checked)"> 压力片与 10kΩ 电阻已接好</label><details><summary>型号、接线、工作方式</summary><div class="pin">3V3 ─ FSR ─┬─ GPIO34 / A2<br>            └─ 10kΩ ─ GND</div><p>两端无极性。压力增大时电阻通常降低，以上接法使 ADC 数值增大。GPIO34 是 ADC1 输入，不能省掉 10kΩ 分压电阻。</p><p>显示的是原始 ADC，不是克或牛顿。未接分压电阻时输入可能漂浮，不代表有压力。</p></details></section>
<section class="card sensor"><div class="head"><h2>水位</h2><span id="waterState" class="state">未确认接线</span></div><div class="tech">非接触电容式 · DFRobot SEN0508</div><div class="value" id="waterValue">—</div><div class="tech">原始电平 <span id="waterRaw">—</span> · LOW=检测到水，HIGH=无水或未接</div><label><input id="waterEnabled" type="checkbox" onchange="act('climate','water',this.checked)"> 水位传感器已接好并贴在杯壁外侧</label><details><summary>型号、接线、工作方式</summary><div class="pin">VCC / +→3V3<br>GND / −→GND<br>OUT / 信号→GPIO26 / D3</div><p>贴在薄的非金属杯壁外侧，以电容变化检测该高度是否有水。显示有水前需连续 LOW 约 300ms。此处只测试水位读数，不控制雾化器。</p><p>测试：分别用空杯和有水杯验证电平会改变。HIGH 不能用来证明传感器已经接好。</p><a href="https://wiki.dfrobot.com/sen0508/" target="_blank" rel="noreferrer">官方资料</a></details></section>
<section class="card"><div class="head"><h2>雾化器</h2><span id="mistState" class="state">关闭</span></div><div class="tech">Grove Water Atomization · 5V / 最大约 2W</div><div class="value" id="mistValue">OFF</div><div class="tech">独立测试：无需连接或通过水位传感器。</div><label><input id="mistArm" type="checkbox" onchange="act('climate','arm',this.checked)"> 已接好测试电源、加水并浸湿棉芯</label><div class="row"><button id="mistButton" class="primary" onclick="act('climate','mist')" disabled>测试出雾 1 秒</button><button class="danger" onclick="act('climate','stop')">停止 / 解除准备</button></div><details><summary>接线、供电、测试条件</summary><div class="pin">驱动板 SIG / EN→GPIO25 / D2<br>驱动板 GND→FireBeetle GND<br>驱动板 VCC→FireBeetle VCC（USB 供电，临时短测）<br>雾化片→驱动板专用两针接口</div><p>当前按用户选择，从 USB 供电的 FireBeetle VCC 脚临时取电。官方说明该脚约 4.7V；雾化模块标称5V，本方案带载能力尚未验证，仅先短测1秒。若主板复位、串口掉线或导线异常发热，立即断开USB，停止该供电方案。以后使用独立稳压5V时，与主板共地，不把外部5V并到主板VCC。</p><p>人工确认已加水并浸湿棉芯后，可独立测试出雾；水位读数不参与此测试。独立任务限制单次最多 1 秒，随时可点停止。驱动板的雾化片端存在约 65V 峰值，通电时不触碰端子和发热电感。</p><a href="https://wiki.seeedstudio.com/Grove-Water_Atomization/" target="_blank" rel="noreferrer">官方安装图</a></details></section>
<section class="card"><div class="head"><h2>摄像头照片</h2><span id="photoState" class="state">未拍摄</span></div><div class="tech">DFRobot DFR1154 · OV3660 · USB JPEG</div><div class="camera"><span id="photoEmpty" class="empty">接上摄像头 USB，点击拍照</span><img id="photo" alt="真实摄像头照片" hidden></div><div class="row"><button id="photoButton" class="primary" onclick="act('camera','photo')">拍一张照片</button><button onclick="act('camera','led')">LED</button></div><details><summary>型号、接线、工作方式</summary><div class="pin">DFR1154 USB-C→电脑 USB<br>不需要与 FireBeetle 直接接 GPIO</div><p>ESP32-S3、16MB Flash、8MB PSRAM。照片经 USB 串口传到电脑，保存至 data/hardware_test/latest.jpg。本测试不需要 SD 卡、WiFi 或 AI。</p><a href="https://wiki.dfrobot.com/dfr1154/" target="_blank" rel="noreferrer">官方资料</a></details></section>
<section class="card"><div class="head"><h2>摄像头音频与补光</h2><span id="audioState" class="state">需扩展测试固件</span></div><div class="tech">板载 PDM 麦克风 · MAX98357 扬声器接口</div><div class="value"><span id="mic">—</span> <span class="unit">RMS</span></div><div class="meter"><div id="micBar" class="fill"></div></div><div class="row" id="audioButtons"><button onclick="act('camera','mic_on')">开始麦克风测试</button><button onclick="act('camera','mic_off')">停止</button><button onclick="act('camera','speaker')">喇叭短音</button><button onclick="act('camera','ir')">红外补光 1 秒</button></div><details><summary>接线与工作方式</summary><div class="pin">MIC：板载，无需外接<br>附带喇叭：接摄像头板 SPK 两针口<br>IR：板载 GPIO47；LED：GPIO3</div><p>扩展固件按官方音频例程使用：麦克风 CLK=38、DATA=39；扬声器 BCLK=45、LRCLK=46、DATA=42。RMS 是相对声音幅度，不是经过校准的分贝值。</p><p>原拍照固件不支持音频按钮，页面会根据固件状态显示支持情况。</p></details></section>
<section class="card wide wiring"><div class="head"><h2>各配件独立接线与验收</h2><span class="badge">先断开电源接线</span></div><table><tr><th>配件</th><th>FireBeetle 连接</th><th>如何确认</th></tr><tr><td>温湿度 SHT31-F</td><td class="i2cWire">3V3 / GND / SDA→21 / SCL→22</td><td>出现温度与湿度，手靠近后观察变化</td></tr><tr><td>光照 BH1750</td><td class="i2cWire">3V3 / GND / SDA→21 / SCL→22</td><td>遮光后 lux 与条形下降</td></tr><tr><td>水位 SEN0508</td><td>3V3 / GND / OUT→26（D3）</td><td>空杯 HIGH，有水 LOW</td></tr><tr><td>压力 FSR 402</td><td>3V3—FSR—34（A2）—10kΩ—GND</td><td>按压时原始 ADC 明显变化</td></tr><tr><td>Grove 雾化</td><td>EN→25（D2），GND→GND，VCC→主板VCC（USB临时短测）</td><td>人工确认加水、棉芯湿润后单独测试 1 秒</td></tr><tr><td>DFR1154</td><td>单独 USB 接电脑；喇叭接该板 SPK</td><td>照片、声音幅度及短音</td></tr></table><p class="tech">每项可单独接线测试，不需全部接齐。I2C 两块传感器共用数据/时钟和电源，使用面包板分线。Grove 线按端子标识对应，未使用的 NC 线留空。主板丝印 D2=GPIO25、D3=GPIO26、A2=GPIO34。</p><div class="row"><span>测试固件 I2C 配置：</span><select id="pinChoice"><option value="21,22">SDA=21，SCL=22（接线表）</option><option value="22,21">SDA=22，SCL=21（互换）</option></select><button onclick="act('climate','pins',document.getElementById('pinChoice').value)">应用</button><button onclick="act('climate','scan')">扫描地址</button></div><div class="tests"><label><input type="checkbox">温湿度变化</label><label><input type="checkbox">遮光变化</label><label><input type="checkbox">空杯 / 有水</label><label><input type="checkbox">按压变化</label><label><input type="checkbox">真实照片</label><label><input type="checkbox">独立出雾 1 秒</label><label><input type="checkbox">麦克风幅度</label><label><input type="checkbox">喇叭短音</label><label><input type="checkbox">补光响应</label></div></section>
<section class="card wide"><div class="head"><h2>原始串口日志</h2><span class="tech">保留读数和错误信息</span></div><div id="logs" class="logs"></div></section><section class="card"><h2>连接说明</h2><p>一块主板只能同时被一个程序读取。运行本测试台前，请退出 VS Code 的 Serial Monitor。</p><p>插拔 USB 后会自动重连；串口名称变化时可在顶部重新选择。</p><p class="tech">页面标记“读数有效”只说明当前通信成功；测试复选框由你观察实物后勾选。缺失数据不会替换成 0 或模拟数据。</p></section>
</div><div class="footer">Shelter · 单文件 Python 硬件测试台 · 默认仅监听本机 127.0.0.1</div></main><div id="toast" class="toast"></div>
<script>
const token='__TOKEN__';let current=null,lastPhoto=0,baseline=null,lastLogs='';const $=id=>document.getElementById(id);
function message(t){$('toast').textContent=t;$('toast').style.display='block';clearTimeout(window.toastTimer);window.toastTimer=setTimeout(()=>$('toast').style.display='none',5000)}
async function act(role,action,value){try{const r=await fetch('/api/command',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token,role,action,value})});const v=await r.json();if(!r.ok)throw Error(v.error);message(action==='photo'?'正在拍照…':'操作已发送；查看读数或日志');}catch(e){message(e.message)}}
async function choosePort(role){try{const r=await fetch('/api/port',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token,role,value:$(role+'Port').value})});const v=await r.json();if(!r.ok)throw Error(v.error);message('正在重新连接');}catch(e){message(e.message)}}
function number(v,d=1){return typeof v==='number'&&Number.isFinite(v)?v.toFixed(d):'—'}
function bar(id,v,max){$(id).style.width=(typeof v==='number'&&Number.isFinite(v)?Math.max(0,Math.min(100,v/max*100)):0)+'%'}
function badge(id,text,ok){$(id).textContent=text;$(id).className='state '+(ok?'ok':'')}
function setBaseline(){const v=current?.devices.climate.data.fsr_raw;if(typeof v!=='number')return message('还没有压力读数');baseline=v;message('已记录当前空载 ADC：'+v)}
function chart(id,history,key,color){const c=$(id),ctx=c.getContext('2d'),w=c.clientWidth||300,h=85;c.width=w*2;c.height=h*2;ctx.scale(2,2);ctx.strokeStyle='#e2e9de';ctx.beginPath();ctx.moveTo(0,h-12);ctx.lineTo(w,h-12);ctx.stroke();const vals=history.map(x=>x[key]).filter(x=>typeof x==='number'&&Number.isFinite(x));if(!vals.length)return;let lo=Math.min(...vals),hi=Math.max(...vals);if(hi-lo<1){lo-=.5;hi+=.5}ctx.strokeStyle=color;ctx.lineWidth=2;ctx.beginPath();let open=false;history.forEach((p,i)=>{const v=p[key];if(typeof v!=='number'||!Number.isFinite(v)){open=false;return}const x=i/Math.max(1,history.length-1)*w,y=9+(hi-v)/(hi-lo)*(h-25);if(open)ctx.lineTo(x,y);else ctx.moveTo(x,y);open=true});ctx.stroke();ctx.fillStyle='#68786f';ctx.font='10px sans-serif';ctx.fillText(number(hi,1),3,10)}
async function refresh(){try{const r=await fetch('/api/state',{cache:'no-store'});if(!r.ok)throw Error('server');const s=await r.json();current=s;const c=s.devices.climate,k=s.devices.camera,d=c.data,fresh=c.connected&&c.age!==null&&c.age<3;['climate','camera'].forEach(role=>{const x=s.devices[role];badge(role+'State',x.connected?'USB 已连接':'未连接',x.connected);$(role+'Info').textContent=x.connected?(x.port+' · '+(x.data.fw||'固件识别中')):(x.error||'等待 USB 设备')});$('firmwareNote').hidden=!c.connected||!d.fw||d.fw==='hardware-test-2';document.querySelectorAll('.sensor').forEach(x=>x.classList.toggle('stale',!fresh));$('temp').textContent=number(d.t);$('humidity').textContent=number(d.rh);bar('humidityBar',d.rh,100);badge('shtState',fresh&&d.sht_ok?'读数有效':fresh?'未读到数据':'等待实时数据',fresh&&d.sht_ok);$('lux').textContent=number(d.lux);bar('luxBar',typeof d.lux==='number'?Math.log10(Math.max(0,d.lux)+1):null,4);badge('luxState',fresh&&d.lux_ok?'读数有效':fresh?'未读到数据':'等待实时数据',fresh&&d.lux_ok);$('lux2').textContent=typeof d.lux2==='number'?'第二块 BH1750（0x5C）：'+number(d.lux2)+' lux':'第二块 BH1750：未检测到 0x5C';$('fsr').textContent=number(d.fsr_raw,0);$('fsrMv').textContent=number(d.fsr_mv,0);bar('fsrBar',d.fsr_raw,4095);$('fsrDelta').textContent=baseline!==null&&typeof d.fsr_raw==='number'?(d.fsr_raw-baseline):'未记录';$('fsrEnabled').checked=!!d.fsr_enabled;badge('fsrState',d.fsr_enabled?'已标记接线 · 请按压验证':'未确认接线',!!d.fsr_enabled);$('waterEnabled').checked=!!d.water_enabled;$('waterRaw').textContent=d.water_raw===0?'0 / LOW':d.water_raw===1?'1 / HIGH':'—';$('waterValue').textContent=!d.water_enabled?'未启用':d.water_wet===1?'检测到水':'无水';badge('waterState',d.water_enabled?'观察空杯与有水变化':'未确认接线',d.water_enabled&&d.water_wet===1);$('mistValue').textContent=d.mist?'ON':'OFF';$('mistArm').checked=!!d.armed;badge('mistState',d.mist?'输出中':d.armed?'已准备':'关闭',!!d.armed);$('mistButton').disabled=!(fresh&&d.armed);$('photoButton').disabled=!k.connected||s.photo_busy;badge('photoState',s.photo_busy?'正在拍摄':s.photo_at?'真实照片已收到':'未拍摄',!!s.photo_at);if(s.photo_at&&s.photo_at!==lastPhoto){lastPhoto=s.photo_at;$('photo').src='/api/photo?t='+lastPhoto;$('photo').hidden=false;$('photoEmpty').hidden=true}const a=k.data;badge('audioState',a.audio_supported?'音频测试固件已就绪':'需扩展测试固件',!!a.audio_supported);document.querySelectorAll('#audioButtons button').forEach(b=>b.disabled=!k.connected||!a.audio_supported||s.photo_busy);$('mic').textContent=number(a.mic_rms,0);bar('micBar',a.mic_rms,5000);const sp=d.sda??21,cp=d.scl??22;const pin='VCC→3V3  GND→GND\nSDA→GPIO'+sp+'  SCL→GPIO'+cp;$('shtPins').textContent=pin;$('lightPins').textContent=pin;document.querySelectorAll('.i2cWire').forEach(x=>x.textContent='3V3 / GND / SDA→'+sp+' / SCL→'+cp);chart('tempChart',s.history,'t','#2c7755');chart('luxChart',s.history,'lux','#b88636');const text=s.logs.slice(-55).map(x=>new Date(x.at*1000).toLocaleTimeString('zh-CN',{timeZone:'Asia/Taipei'})+' ['+x.role+'] '+x.text).join('\n');if(text!==lastLogs){lastLogs=text;$('logs').textContent=text;$('logs').scrollTop=$('logs').scrollHeight}}catch(e){badge('climateState','测试台连接中断',false)}finally{setTimeout(refresh,500)}}
async function loadPorts(){try{const p=await(await fetch('/api/ports')).json();['climate','camera'].forEach(role=>{const select=$(role+'Port'),old=select.value;select.textContent='';const auto=document.createElement('option');auto.value='';auto.textContent='自动识别 USB';select.append(auto);p.forEach(x=>{const o=document.createElement('option');o.value=x.port;o.textContent=x.port+' · '+x.description;select.append(o)});select.value=old})}catch(e){}}
loadPorts();setInterval(loadPorts,6000);refresh();
</script></html>'''


def handler_for(bench):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def send(self, status, data, mime="application/json; charset=utf-8"):
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
                self.send(200, HTML.replace("__TOKEN__", bench.token), "text/html; charset=utf-8")
            elif path == "/api/state":
                self.send(200, bench.snapshot())
            elif path == "/api/ports":
                self.send(200, ports())
            elif path == "/api/photo":
                with bench.lock:
                    jpeg = bench.jpeg
                self.send(200, jpeg, "image/jpeg") if jpeg else self.send(404, {"error": "尚未拍照"})
            else:
                self.send(404, {"error": "not found"})

        def do_POST(self):
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size < 2048:
                    raise ValueError("请求大小无效")
                body = json.loads(self.rfile.read(size))
                if not isinstance(body, dict) or body.get("token") != bench.token:
                    return self.send(403, {"error": "请刷新本机测试页面"})
                role = body.get("role")
                if role not in ("climate", "camera"):
                    raise ValueError("未知设备")
                path = urlparse(self.path).path
                if path == "/api/port":
                    value = body.get("value") or None
                    if value and value not in {p["port"] for p in ports()}:
                        raise ValueError("串口不存在")
                    bench.links[role].desired = value
                    bench.links[role].reconnect.set()
                elif path == "/api/command":
                    bench.command(role, body.get("action"), body.get("value"))
                else:
                    return self.send(404, {"error": "not found"})
                self.send(202, {"ok": True})
            except (ValueError, TypeError, KeyError, json.JSONDecodeError) as error:
                self.send(400, {"error": str(error)})
    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=5047, help="本机网页端口")
    parser.add_argument("--climate-port", help="手动指定 FireBeetle 串口")
    parser.add_argument("--camera-port", help="手动指定 DFR1154 串口")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    bench = Bench()
    links = [Link(bench, "climate", args.climate_port), Link(bench, "camera", args.camera_port)]
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler_for(bench))
    for link in links:
        link.start()
    url = f"http://127.0.0.1:{args.port}"
    print(f"Shelter 配件测试台：{url}\n保持此终端运行；Ctrl+C 退出。先关闭其他串口监视器。", flush=True)
    if not args.no_browser:
        threading.Timer(.7, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        for link in links:
            link.stop.set()
        server.server_close()
        for link in links:
            link.join(timeout=2)


if __name__ == "__main__":
    main()
