# -*- coding: utf-8 -*-
"""
页面脚本语法自检（仅本地开发用）

无头浏览器在这台机器上跑不起来时（沙箱不允许 Chrome 建命名管道），
至少要保证「渲染后的页面里那段 JS 没有语法错误」——
内联脚本一旦写错，整个管理页/接龙页会直接失去交互，而页面本身还是 200。

做法：用 TestClient 走真实路由拿到渲染后的 HTML（Jinja 已展开成最终 JS），
把每个 <script> 抽出来交给 node --check 过一遍，再顺手检查外链的 static/*.js。

用法：python _preview/check_js.py
"""

import json
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import db  # noqa: E402
import main  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

NODE = os.environ.get("DSH_NODE") or "node"
CLIENT = TestClient(main.app)

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(("  PASS  " if ok else "  FAIL  ") + name + (("   <- " + str(detail)) if detail and not ok else ""))


def node_check(code, label):
    """把一段 JS 交给 node --check；返回 (是否通过, 输出)"""
    fd, path = tempfile.mkstemp(suffix=".js", text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(code)
        proc = subprocess.run([NODE, "--check", path], capture_output=True, text=True)
        return proc.returncode == 0, (proc.stderr or proc.stdout).strip()
    finally:
        os.unlink(path)


def scripts_of(html):
    """抽出页面里所有内联 <script>（跳过带 src 的与 type 非 js 的）"""
    out = []
    for m in re.finditer(r"<script([^>]*)>(.*?)</script>", html, re.S | re.I):
        attrs, code = m.group(1), m.group(2)
        if "src=" in attrs or "text/plain" in attrs:
            continue
        if code.strip():
            out.append(code)
    return out


def main_check():
    db.init_db()

    # 造一条开启接龙的事项，让接龙页也能渲染出来
    nid = CLIENT.post("/api/notices", json={
        "title": "【JS 自检】接龙事项", "category": "notice",
        "deadline": "2026-10-20 18:00", "rollcall": 1,
    }).json()["id"]
    used_icons = set()
    try:
        pages = {"index.html": "/", "admin.html": "/admin", "rollcall.html": "/rollcall/%d" % nid,
                 "404.html": "/nope-not-here"}
        for name, path in pages.items():
            html = CLIENT.get(path).text
            used_icons |= set(re.findall(r"icons\.svg#(i-[a-z0-9-]+)", html))
            found = scripts_of(html)
            # 404 页没有内联脚本（只有一个 onclick 属性），只确认它渲染出来了
            check("%s 渲染成功" % name, ("error-card" in html) if name == "404.html" else len(html) > 500)
            for i, code in enumerate(found, 1):
                ok, out = node_check(code, name)
                check("%s 内联脚本 #%d 语法通过（node --check）" % (name, i), ok, out[:400])
    finally:
        CLIENT.delete("/api/notices/%d" % nid)

    for name in ("app.js",):
        path = os.path.join(ROOT, "static", name)
        ok, out = node_check(open(path, encoding="utf-8").read(), name)
        check("static/%s 语法通过" % name, ok, out[:400])

    # 图标精灵：页面里引用的每一个 #i-xxx 都必须在 icons.svg 里有定义。
    # 图标名写错不会报任何错，只会在页面上静默显示成空白，肉眼很容易漏。
    sprite = open(os.path.join(ROOT, "static", "icons.svg"), encoding="utf-8").read()
    missing = sorted(i for i in used_icons if ('id="%s"' % i) not in sprite)
    check("页面引用的 %d 个图标在 icons.svg 里都有定义" % len(used_icons), not missing, missing)
    for icon in ("i-users", "i-edit", "i-copy"):
        check("icons.svg 含 %s" % icon, ('id="%s"' % icon) in sprite)

    failed = [r for r in RESULTS if not r[1]]
    print("\n%d/%d passed" % (len(RESULTS) - len(failed), len(RESULTS)))
    if failed:
        print("failed: " + "; ".join(r[0] for r in failed))
        sys.exit(1)


if __name__ == "__main__":
    main_check()
