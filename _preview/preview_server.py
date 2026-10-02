# -*- coding: utf-8 -*-
"""
本机 UI 预览服务器（仅本地开发用，部署时不需要上传本目录）

它不复制任何页面逻辑：直接 import 项目里的 main.app，用 TestClient 走真实路由拿
到渲染后的 HTML，再把这些 HTML 原样吐给浏览器。因此预览里看到的样式 / 图标 /
交互，跟线上跑起来完全一致，不会出现"预览好看、上线走样"。
GET / POST / PUT / DELETE 都会转发（管理页的新增/修改/删除、接龙页的报名/撤销
在预览里也能真的写库），Cookie 双向透传。

启动：
    python _preview/preview_server.py            # 默认端口 8777
    python _preview/preview_server.py 9000       # 指定端口

访问：
    http://127.0.0.1:8777/                 首页
    http://127.0.0.1:8777/admin            管理后台
    http://127.0.0.1:8777/rollcall/{id}    接龙页（id 见管理页「接龙名单」链接）
    http://127.0.0.1:8777/nope            404 页
    http://127.0.0.1:8777/sample           给本地库灌几条样例事项（覆盖各种级别）
"""

import os
import sys
import traceback
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import db  # noqa: E402
import main  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

CLIENT = TestClient(main.app)


def sample_rows():
    """覆盖各种视觉状态：置顶 / 紧急 / 普通 / 已完成 / 无截止时间 / 带链接 / 接龙。"""
    now = datetime.now()
    fmt = "%Y-%m-%d %H:%M"
    return [
        dict(title="国庆放假调课安排", category="notice", tag="重要", pinned=True,
             note="10 月 1 日至 7 日放假，10 月 11 日（周六）补上周三的课，请提前调整作息。",
             deadline=(now + timedelta(hours=30)).strftime(fmt),
             date_label="10-01", time_label="08:00 开始", location="全校"),
        dict(title="新生心理测评", category="notice", tag="必做",
             note="在学工系统完成 SCL-90 量表，约 15 分钟，务必如实作答。",
             deadline=(now + timedelta(hours=6)).strftime(fmt),
             date_label=(now + timedelta(hours=6)).strftime("%m-%d"),
             time_label=(now + timedelta(hours=6)).strftime("%H:%M 截止"),
             location="线上"),
        # 开启接龙：首页卡片整块可点，进入 /rollcall/{id}
        dict(title="班级团建报名", category="notice", rollcall=True,
             note="本周六下午香山公园，班费出车费，请点击本卡片接龙报名。",
             deadline=(now + timedelta(days=9)).strftime(fmt),
             date_label=(now + timedelta(days=9)).strftime("%m-%d"), time_label="18:00 截止"),
        dict(title="班委例会记录归档", category="notice",
             note="上周三的例会纪要已经上传到班级群文件，有需要的同学自取。",
             deadline=(now - timedelta(days=4)).strftime(fmt),
             date_label=(now - timedelta(days=4)).strftime("%m-%d")),
        dict(title="高等数学 第三章 习题", category="homework", tag="作业",
             note="P88 第 3、5、7、11 题，写到作业本上，下周一课上交。",
             deadline=(now + timedelta(hours=20)).strftime(fmt),
             date_label=(now + timedelta(hours=20)).strftime("%m-%d"), time_label="课前交"),
        dict(title="大学物理实验报告（二）", category="homework", tag="实验",
             note="《用惠斯通电桥测电阻》，报告模板见课程群，要求手写数据处理。",
             deadline=(now + timedelta(days=4)).strftime(fmt),
             date_label=(now + timedelta(days=4)).strftime("%m-%d")),
        dict(title="程序设计上机作业 02", category="homework",
             note="完成链表的插入、删除与反转，提交 .c 文件到教学平台。",
             deadline=(now - timedelta(days=2)).strftime(fmt),
             date_label=(now - timedelta(days=2)).strftime("%m-%d")),
        dict(title="数据结构 第 7 讲（树与二叉树）", category="online",
             note="录播已上传，课时 1.5h，看完记得在讨论区回复签到。",
             deadline=(now + timedelta(hours=60)).strftime(fmt),
             date_label=(now + timedelta(hours=60)).strftime("%m-%d"), time_label="23:59 前"),
        dict(title="形势与政策 线上直播", category="online", tag="直播",
             note="腾讯会议 会议号 823-118-772，提前 5 分钟进会场。",
             deadline=(now + timedelta(hours=40)).strftime(fmt),
             date_label=(now + timedelta(hours=40)).strftime("%m-%d"), time_label="19:00 开播",
             location="腾讯会议"),
        dict(title="英语视听说 Unit 4 测验", category="online",
             note="在超星学习通完成，只有一次机会，请在网络稳定时作答。",
             deadline=(now + timedelta(days=6)).strftime(fmt),
             date_label=(now + timedelta(days=6)).strftime("%m-%d")),
    ]


# 给接龙事项预置的报名样例：(姓名, 备注)
SAMPLE_ROLLCALLS = [("王小明", "学号 20240101"), ("李思思", "带家属 1 人"), ("赵子龙", "")]


def topup_rollcalls(rows):
    """
    样例接龙事项还在、名单却是空的，就把样例报名补回去（幂等）。

    为什么需要：check_states.py 会真接一次龙再撤销，手工清过库之后也可能只剩下事项。
    名单为空时「接龙页有多少行」这类断言就没法跑了，所以这里顺手补齐。
    """
    target = [r for r in rows if r.get("rollcall") and r.get("title") == "班级团建报名"]
    if not target:
        return
    nid = target[0]["id"]
    if db.list_rollcalls(nid)[0]:
        return
    for i, (name, note) in enumerate(SAMPLE_ROLLCALLS, 1):
        db.add_rollcall(nid, name, note, "preview-device-token-%02d" % i)
    print("[preview] 给「班级团建报名」补了 %d 条样例报名。" % len(SAMPLE_ROLLCALLS))


def seed():
    db.init_db()
    existing = db.list_active(None) + db.list_folded(None)
    if existing:
        print("[preview] 本地库已有 %d 条事项，不再灌样例。" % len(existing))
        topup_rollcalls(existing)
        return
    ids = {}
    for row in sample_rows():
        ids[row["title"]] = db.add_notice(row)
    # 接龙事项灌几条报名：预览时能直接看到名单、人数胶囊与「已接龙」样式。
    # 令牌是随手造的假值，所以这些行在浏览器里都不会被标成"我那条"；
    # 想看到高亮，直接在接龙页填个名字提交即可（cookie 会跟着发下来）。
    rid = ids.get("班级团建报名")
    if rid:
        for i, (name, note) in enumerate(SAMPLE_ROLLCALLS, 1):
            db.add_rollcall(rid, name, note, "preview-device-token-%02d" % i)
    print("[preview] 已灌入 %d 条样例事项（含 1 条接龙，%d 人已报名）。"
          % (len(sample_rows()), len(SAMPLE_ROLLCALLS)))


def render(method, path, body=None, headers=None):
    """
    把请求原样交给真正的 FastAPI 应用（TestClient 走的就是线上同一套路由），
    再把响应原样吐回浏览器。

    两个必须做的转换：
      1. TestClient 会把 url_for 生成的地址写成绝对地址 http://testserver/...，
         浏览器拿到这个域名只会去解析外网主机、必然失败（样式和图标全都加载不到），
         这里统一还原成以 / 开头的相对地址；
      2. Cookie 要双向透传 —— 接龙靠 cookie 认设备，不透传的话
         「接龙后首页显示已接龙」在预览里永远看不到（线上是浏览器直连，不存在这问题）。
    """
    headers = headers or {}
    r = CLIENT.request(method, path, content=body, headers=headers)
    out = r.content
    ctype = r.headers.get("content-type", "text/html; charset=utf-8")
    if "html" in ctype or "css" in ctype or "javascript" in ctype:
        out = out.replace(b"http://testserver/", b"/")
    return r, out, ctype


# 布局自检页：把待测页面放进同源 iframe，注入 measure.js，
# 再把量到的结果写进文档 —— 用 --dump-dom 抓下来即可定位横向溢出等问题。
MEASURE_SHELL = """<!DOCTYPE html><html><head><meta charset="utf-8">
<style>html,body{margin:0}iframe{width:430px;height:1000px;border:0;display:block}</style>
</head><body>
<iframe id="f" src="__SRC__"></iframe>
<pre id="out">measuring...</pre>
<script>__MEASURE__</script>
<script>
document.getElementById('f').addEventListener('load', function () {
    var d = document.getElementById('f').contentDocument;
    var s = d.createElement('script');
    s.textContent = document.getElementById('measure-src').textContent;
    d.body.appendChild(s);
    setTimeout(function () {
        var dump = d.getElementById('layout-dump');
        document.getElementById('out').textContent = dump ? dump.textContent : 'NO DUMP';
    }, 500);
});
</script>
<script type="text/plain" id="measure-src">__MEASURE__</script>
</body></html>
"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # 安静一点
        pass

    def handle_error(self, request, client_address):
        """
        浏览器用完就走、keep-alive 连接被重置是常态（ConnectionResetError），
        默认实现会为每条这样的连接刷一屏 traceback，把真正的报错埋掉。
        这里只吞掉"对面关连接"这一类，其它异常照常打印。
        """
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)

    def _send(self, code, body, ctype):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else None

    def _forward_headers(self):
        """只透传应用真正需要的那几个头，别把 Host 之类的本地信息也带过去"""
        keep = ("Cookie", "Content-Type")
        return {k: self.headers[k] for k in keep if self.headers.get(k)}

    def _proxy(self, method):
        r, body, ctype = render(method, self.path, self._read_body(), self._forward_headers())
        self.send_response(r.status_code)
        self.send_header("Content-Type", ctype)
        # 逐条回传 Set-Cookie：接龙令牌就是靠它落进浏览器的
        for value in r.headers.get_list("set-cookie"):
            self.send_header("Set-Cookie", value)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _handle(self, method):
        path = self.path.split("?")[0]
        try:
            if path == "/sample" and method == "GET":
                seed()
                self._send(200, "<meta charset=utf-8>样例数据已就绪，<a href='/'>回首页</a>",
                           "text/html; charset=utf-8")
                return
            if path == "/measure" and method == "GET":
                measure = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "measure.js"),
                               encoding="utf-8").read()
                target = "/admin" if "admin" in self.path else "/"
                html = (MEASURE_SHELL
                        .replace("__SRC__", target)
                        .replace("__MEASURE__", measure))
                self._send(200, html, "text/html; charset=utf-8")
                return
            # 其余一律转给真实应用：/rollcall/{id}、/api/... 这些动态路由本来就该由它处理，
            # 不在预览服务器里再抄一份路由表（抄了就会和线上走样的地方不一样）。
            self._proxy(method)
        except Exception:  # 让错误直接显示在浏览器里，便于定位
            self._send(500, "<meta charset=utf-8><pre>" + traceback.format_exc() + "</pre>",
                       "text/html; charset=utf-8")

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_PUT(self):
        self._handle("PUT")

    def do_DELETE(self):
        self._handle("DELETE")


class Server(ThreadingHTTPServer):
    """
    「客户端正常走人」这类关连接的异常必须在**服务器**这一层吞掉。

    只在 Handler.handle_error 里吞是不够的：连接在解析请求行时就被重置的话，
    异常是从 BaseHTTPRequestHandler.__init__ 里抛出去的，交给 socketserver 的
    process_request_thread → BaseServer.handle_error 处理，会一屏接一屏地刷
    traceback（headless 截图脚本跑完即退出，每次都会留下十几条），
    真正的报错全被埋掉。
    """

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8777
    seed()
    srv = Server(("127.0.0.1", port), Handler)
    print("[preview] http://127.0.0.1:%d/  (admin: /admin , 接龙: /rollcall/{id} , 404: /404)" % port)
    srv.serve_forever()
