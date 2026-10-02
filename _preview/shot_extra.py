# -*- coding: utf-8 -*-
"""
补充截图：首页「我的」栏目、作业栏目、桌面宽度首页（仅本地开发用）
用法：python _preview/shot_extra.py
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from shot import WS, launch, wait_target, find_chrome  # noqa: E402

BASE = "http://127.0.0.1:8777"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "shots")
PORT = 9225


def main():
    profile = os.path.join(os.path.dirname(os.path.abspath(__file__)), "chrome-profile")
    proc = launch(find_chrome(), PORT, profile)
    try:
        ws = WS(wait_target(PORT))
        ws.call("Page.enable")
        ws.call("Runtime.enable")

        jobs = [
            ("final-mine", "/", 430, 900, "document.querySelector('[data-target=page-mine]').click()"),
            ("final-homework", "/", 430, 900, "document.querySelector('[data-target=page-homework]').click()"),
            ("final-index-desktop", "/", 900, 1200, None),
        ]
        for name, path, w, h, js in jobs:
            ws.call("Emulation.setDeviceMetricsOverride",
                    {"width": w, "height": h, "deviceScaleFactor": 2, "mobile": w < 600})
            ws.call("Page.navigate", {"url": BASE + path})
            time.sleep(2.5)
            if js:
                ws.call("Runtime.evaluate", {"expression": js})
                time.sleep(1.0)
            shot = ws.call("Page.captureScreenshot",
                           {"format": "png", "captureBeyondViewport": True, "fromSurface": True})
            import base64
            dest = os.path.join(OUT, name + ".png")
            with open(dest, "wb") as fh:
                fh.write(base64.b64decode(shot["data"]))
            print("saved", dest)
    finally:
        proc.terminate()


if __name__ == "__main__":
    main()
