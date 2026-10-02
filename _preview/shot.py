# -*- coding: utf-8 -*-
"""
用 Chrome DevTools Protocol 抓「整页」截图（仅本地开发用）

为什么不直接用 `chrome --screenshot`：
    那个参数只能截「视口高度」那一块，页面比视口长时下半截会被丢掉，
    想截全就得把 --window-size 猜得很高，而窗口尺寸在无头模式下并不总被采纳。

这里改用 CDP：
    1. Emulation.setDeviceMetricsOverride 精确设定视口宽高（手机 430 / 桌面 1200）；
    2. Page.captureScreenshot + captureBeyondViewport 一次拿到完整长图；
    3. 顺带把页面里的 layout 指标（scrollWidth 等）打印出来，方便核对有没有横向溢出。

用法：
    python _preview/shot.py <url> <输出png> [宽] [高]
"""

import base64
import json
import os
import socket
import struct
import subprocess
import sys
import time
import urllib.request

CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]


class WS:
    """够用就好的 WebSocket 客户端（只处理 CDP 用到的文本帧）。"""

    def __init__(self, url):
        # ws://127.0.0.1:9222/devtools/page/XXXX
        rest = url.split("://", 1)[1]
        hostport, path = rest.split("/", 1)
        host, port = hostport.split(":")
        self.sock = socket.create_connection((host, int(port)), timeout=30)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            "GET /%s HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n" % (path, hostport, key)
        )
        self.sock.sendall(req.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            buf += self.sock.recv(4096)
        self.buf = buf.split(b"\r\n\r\n", 1)[1]
        self.msg_id = 0

    def _recv(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("websocket closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def send(self, payload):
        data = json.dumps(payload).encode()
        header = bytearray([0x81])
        mask = os.urandom(4)
        n = len(data)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        header += mask
        self.sock.sendall(bytes(header) + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def recv(self):
        b1, b2 = self._recv(2)
        length = b2 & 0x7F
        if length == 126:
            length = struct.unpack(">H", self._recv(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", self._recv(8))[0]
        return json.loads(self._recv(length).decode("utf-8", "replace"))

    def call(self, method, params=None, timeout=60):
        self.msg_id += 1
        mid = self.msg_id
        self.send({"id": mid, "method": method, "params": params or {}})
        deadline = time.time() + timeout
        while time.time() < deadline:
            msg = self.recv()
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError("%s -> %s" % (method, msg["error"]))
                return msg.get("result", {})
        raise TimeoutError(method)


def find_chrome():
    for p in CHROME_CANDIDATES:
        if os.path.exists(p):
            return p
    raise SystemExit("找不到 Chrome / Edge 可执行文件")


def kill_stale_headless(profile):
    """
    清掉上一轮没退干净的、用同一个 profile 的无头 Chrome。

    为什么必须做：脚本结束时只 terminate() 了自己启动的那个父进程，
    Chrome 的子进程经常留了下来，继续占着 user-data-dir 与调试端口。
    下一轮再启动时，两套浏览器会抢同一个 profile / 端口，
    表现是「页面点不动」「Runtime.evaluate 连接被重置」，很难往这上面想。

    只杀命令行里带本目录 chrome-profile 的进程，用户自己开的 Chrome
    （profile 在 AppData 下）不会被动到。非 Windows 直接跳过。
    """
    if os.name != "nt":
        return
    marker = os.path.basename(profile)          # chrome-profile
    ps = (
        "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe' or Name='msedge.exe'\" | "
        "Where-Object { $_.CommandLine -like '*%s*' } | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
        % marker
    )
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=25)
    except Exception:
        pass


def launch(chrome, port, profile):
    kill_stale_headless(profile)
    time.sleep(0.6)          # 等端口真正释放，否则新实例可能绑不上调试端口
    args = [
        chrome, "--headless=new", "--disable-gpu", "--hide-scrollbars",
        "--no-first-run", "--no-default-browser-check", "--disable-extensions",
        "--remote-debugging-port=%d" % port,
        "--user-data-dir=%s" % profile,
        "about:blank",
    ]
    return subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def wait_target(port, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            raw = urllib.request.urlopen("http://127.0.0.1:%d/json/list" % port, timeout=2).read()
            for t in json.loads(raw):
                if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
                    return t["webSocketDebuggerUrl"]
        except Exception:
            pass
        time.sleep(0.4)
    raise SystemExit("Chrome 调试端口没有就绪")


def main():
    url = sys.argv[1]
    out = sys.argv[2]
    width = int(sys.argv[3]) if len(sys.argv) > 3 else 430
    height = int(sys.argv[4]) if len(sys.argv) > 4 else 900
    port = 9223

    chrome = find_chrome()
    profile = os.path.join(os.path.dirname(os.path.abspath(__file__)), "chrome-profile")
    proc = launch(chrome, port, profile)
    try:
        ws = WS(wait_target(port))
        ws.call("Page.enable")
        ws.call("Emulation.setDeviceMetricsOverride", {
            "width": width, "height": height, "deviceScaleFactor": 2, "mobile": width < 600,
        })
        ws.call("Page.navigate", {"url": url})
        time.sleep(3.0)  # 等样式 / 图标 / fetch 都落地
        metrics = ws.call("Runtime.evaluate", {
            "expression": "JSON.stringify({sw:document.documentElement.scrollWidth,"
                          "vw:window.innerWidth,sh:document.documentElement.scrollHeight})",
            "returnByValue": True,
        })
        info = json.loads(metrics["result"]["value"])
        shot = ws.call("Page.captureScreenshot", {
            "format": "png", "captureBeyondViewport": True, "fromSurface": True,
        })
        with open(out, "wb") as fh:
            fh.write(base64.b64decode(shot["data"]))
        print("%s  viewport=%d scrollWidth=%d scrollHeight=%d -> %s" %
              (os.path.basename(out), info["vw"], info["sw"], info["sh"], out))
    finally:
        proc.terminate()


if __name__ == "__main__":
    main()
