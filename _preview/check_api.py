# -*- coding: utf-8 -*-
"""
接口自检（仅本地开发用）

check_states.py 管的是「点一下会不会崩」的 UI 自检，这里管接口与数据：
  1. 新增事项时能开启接龙，接龙页可访问、未开启接龙的按 404 处理；
  2. 接龙写进数据库，同名不能重复，空姓名被拦，撤销只能撤自己的；
  3. **一台设备可以连续报多条（代报名）**，设备层面没有任何次数限制；
  4. 接龙后首页那条显示「已接龙」并按已完成处理（同一 cookie 会话内）；
  5. 接龙页只保留事项名称，其余字段不重复展示，并带「复制接龙结果」按钮；
  6. 修改事项（PUT /api/notices/{id}）能改全部字段，关掉接龙后名单入口消失；
  7. 删除事项时接龙明细一并清掉，不留孤儿行；
  8. v1.8.0：接龙「附加信息」的名称 / 类别（文本 / 选择）/ 是否必填 / 候选项
     能存能回填，两种类别落库都是同一个字符串，非法值一律被后端拦下；
     姓名 ≤ 4 个字、附加信息 ≤ 20 个字；接龙页显示剩余时间；
     首页卡片的小信息块合并进 .item-tags，已接龙的不再显示「已接龙，点击查看名单」。
  9. v1.9.0：截止时间一过就不再收新的报名（接口 400 +「已于 … 截止」、
     接龙页不再渲染报名表单、首页卡片的提示改成「接龙已截止」），
     未填截止时间的照旧随到随报；「打勾叉」类别已删除，旧库里配成 check 的事项
     在启动迁移时回退成 text；「进行选择」在接龙页把候选项全部铺成一排
     （原生 radio，非必填时多一颗「不填」），不再是下拉框。

所有断言只读写本机 SQLite，跑完会把测试数据删干净。
用法：python _preview/check_api.py
"""

import os
import sqlite3
import sys
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import db  # noqa: E402
import main  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

CLIENT = TestClient(main.app)

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(("  PASS  " if ok else "  FAIL  ") + name + (("   <- " + str(detail)) if detail and not ok else ""))


def notice_html(nid):
    return CLIENT.get("/").text


def _future(days=7):
    return (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d %H:%M")


def _past(hours=1):
    """
    刚刚过去的时间。**刻意只往前推 1 小时**：已完成且超过 1 天的事项会被折叠，
    首页根本不渲染那张卡片（那属于折叠自检的范围），拿不到卡片就没法断言提示语。
    """
    return (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M")


def card_block(html, nid):
    """
    从首页 HTML 里取出某个接龙事项的卡片片段。

    必须切到 `</a>` 为止，不能只取"href 之后的 1400 个字符" ——
    那样会把后面几张卡片一起框进来，断言"这张卡上没有某句话"时就会误判
    （相邻卡片上正好有那句话）。接龙卡片整块由 <a> 包裹，切到这里刚好一张卡。
    """
    parts = html.split('href="/rollcall/%d"' % nid, 1)
    if len(parts) != 2:
        return ""
    return parts[1].split("</a>", 1)[0]


def check_note_config():
    """
    接龙「附加信息」的配置与校验（v1.8.0 起，v1.9.0 去掉「打勾叉」）。

    两种类别各建一条事项跑一遍：配置能不能存取、接龙页渲染成什么控件、
    非法的值（超长 / 选择填了选项外的字）会不会被后端拦下。
    所有建的事项在 finally 里删干净。
    """
    # (类别, 候选项, 名称, 是否必填, 合法值, 非法值, 页面必须有的标记, 页面不该有的标记)
    # 非法值故意都选得比 20 个字短，这样被拦下时必然是"类别不对"而不是"太长"；
    # 太长那一条最后单独测（21 个字）。
    # v1.9.0：选择类别在页面上是「一排直接能点的单选项」（radio），不再是下拉框，
    # 所以断言里查 name="note" / value="候选" / 「不填」，并明确排除 id="rcNote"。
    cases = [
        ("text", "", "学号", 0, "20240101", "字" * 21,
         ['id="rcNote"', 'maxlength="20"', 'id="rcName"', 'maxlength="4"'],
         ['type="radio"', "不填"]),
        ("select", "参加\n不参加", "能否到场", 0, "参加", "看情况",
         ['name="note"', 'value="参加"', 'value="不参加"', "不填"],
         ['id="rcNote"', "<select"]),
        ("select", "参加\n不参加\n待定", "能否到场", 1, "待定", "看情况",
         ['value="参加"', 'value="不参加"', 'value="待定"'],
         ['id="rcNote"', "<select", "不填"]),
    ]
    made = []
    try:
        for mode, options, label, required, good, bad, marks, absent in cases:
            r = CLIENT.post("/api/notices", json={
                "title": "【自检】附加信息-%s" % mode, "category": "notice", "rollcall": 1,
                "deadline": _future(), "roll_note_mode": mode, "roll_note_options": options,
                "roll_note_label": label, "roll_note_required": required,
            })
            nid = r.json().get("id")
            made.append(nid)
            check("新增 %s 类附加信息的事项" % mode, r.status_code == 200 and nid, r.text[:200])

            row = db.get_notice(nid, db.roll_context(None))
            check("%s 类配置正确落库并回读" % mode,
                  row["roll_note_mode"] == mode and row["roll_note_label"] == label
                  and row["roll_note_required"] is bool(required)
                  and row["roll_note_options"] == ([x for x in options.split("\n")] if options else []),
                  {k: row[k] for k in ("roll_note_mode", "roll_note_label",
                                       "roll_note_required", "roll_note_options")})

            # 接龙页：控件按类别渲染，且带着配置的名称。
            # 控件类的断言只在 <form> 那一段里找 —— 页面脚本的注释里也会出现
            # 「不填」这类词，整页搜会把注释当成渲染结果。
            page = CLIENT.get("/rollcall/%d" % nid).text
            form = page.split('<form id="rcForm"', 1)[1].split('</form>', 1)[0]
            check("%s 类接龙页按类别渲染控件" % mode,
                  all(m in form for m in marks), [m for m in marks if m not in form])
            check("%s 类接龙页不渲染别的类别的控件" % mode,
                  all(m not in form for m in absent), [m for m in absent if m in form])
            check("%s 类接龙页显示配置的附加信息名称" % mode, label in page)

            # v1.9.0：选择类别的候选项**全部**铺在页面上，且都是 name="note" 的单选项；
            # 非必填时多一颗「不填」，必填时一颗都不预选（空着提交会被拦）。
            if mode == "select":
                opts = [x for x in options.split("\n")]
                check("选择类别的 %d 个候选项全部铺在页面上" % len(opts),
                      form.count('type="radio" name="note"') == len(opts) + (0 if required else 1),
                      form.count('type="radio" name="note"'))
                check("选择类别铺出来的是单选项、没有下拉框",
                      '<div class="rc-choices"' in form and "<select" not in form)
                check("非必填的选择类别默认选中「不填」（不填就是空串）",
                      (form.count('name="note" value="" checked') == 1) if not required
                      else ('name="note" value=""' not in form))

            # 剩余时间：事项填了截止时间 → 名称块旁边有倒计时胶囊
            check("%s 类接龙页显示剩余时间" % mode,
                  'id="rcCountdown"' in page and ("剩 " in page or "已过 " in page))

            # 合法值能报上（走接口，顺带验证路由确实把 note 传下去了）
            r = CLIENT.post("/api/rollcall/%d" % nid, json={"name": "张三", "note": good})
            check("%s 类收下合法值「%s」" % (mode, good), r.status_code == 200, r.text[:200])

            # 非法值被后端拦下：400 + 一句点名了附加信息的中文提示
            r = CLIENT.post("/api/rollcall/%d" % nid, json={"name": "李四", "note": bad})
            msg = r.json().get("msg", "") if r.status_code == 400 else r.text[:120]
            check("%s 类拦下非法值「%s」" % (mode, bad[:6]),
                  r.status_code == 400 and (label in msg or "附加信息" in msg), msg)

            # 长度限制：姓名正好 4 个字可以，5 个字不行（换个姓名，免得撞上同名校验）
            ok, msg = db.add_rollcall(nid, "欧阳小明", good, "v19-len-%s" % mode)
            check("%s 类收下 4 个字的姓名" % mode, ok, msg)
            ok, msg = db.add_rollcall(nid, "一二三四五", good, "v19-len-%s" % mode)
            check("%s 类拦下 5 个字的姓名" % mode, not ok and "4" in msg, msg)

            # 附加信息超过 20 个字：文本类别由长度校验拦，选择类别先被类别校验拦下
            ok, msg = db.add_rollcall(nid, "钱七", "字" * 21, "v19-len-%s" % mode)
            check("%s 类拦下超过 20 个字的附加信息" % mode, not ok, msg)
    finally:
        for nid in made:
            CLIENT.delete("/api/notices/%d" % nid)

    # v1.9.0：'check'（打勾叉）已经删除，配成它必须被拒绝（旧库由迁移回退成 text）
    r = CLIENT.post("/api/notices", json={
        "title": "【自检】非法配置", "category": "notice", "roll_note_mode": "check",
    })
    check("已删除的「打勾叉」类别 → 400", r.status_code == 400, r.text[:200])

    # 配置非法时新增/修改要被拦（避免把半截配置写进库）
    r = CLIENT.post("/api/notices", json={
        "title": "【自检】非法配置", "category": "notice",
        "roll_note_mode": "select", "roll_note_options": "",
    })
    check("选「进行选择」却没填候选项 → 400", r.status_code == 400, r.text[:200])
    r = CLIENT.post("/api/notices", json={
        "title": "【自检】非法配置", "category": "notice", "roll_note_mode": "随便填",
    })
    check("未知的附加信息类别 → 400", r.status_code == 400, r.text[:200])
    r = CLIENT.post("/api/notices", json={
        "title": "【自检】非法配置", "category": "notice",
        "roll_note_mode": "select", "roll_note_options": "参加\n\n参加\n  \n不参加\n待定",
    })
    nid = r.json().get("id")
    check("候选项会去空行去重后入库", bool(nid), r.text[:200])
    if nid:
        try:
            row = db.get_notice(nid, db.roll_context(None))
            check("候选项去重后的条数正确", len(row["roll_note_options"]) == 3,
                  row["roll_note_options"])
            # 换成非选择类别时，候选项应当被清空（免得改回选择时冒出旧选项）
            CLIENT.put("/api/notices/%d" % nid, json={
                "title": "【自检】非法配置", "category": "notice", "roll_note_mode": "text",
            })
            row = db.get_notice(nid, db.roll_context(None))
            check("改回文本类别时候选项被清空", row["roll_note_options"] == [], row["roll_note_options"])
        finally:
            CLIENT.delete("/api/notices/%d" % nid)


def check_rollcall_deadline():
    """
    v1.9.0：接龙的时间闸门。

    三件事必须同时成立，否则"过了点还能报"：
      1. 接口拦得住（页面可以在截止前打开、截止后才点提交）；
      2. 接龙页不再渲染报名表单，改成一条「已于 … 截止」的说明；
      3. 名单与「复制接龙结果」照旧可用（班委正是照着这份名单办事）。
    另外确认两件不该被牵连的事：没填截止时间的照旧随到随报；撤销不受影响。
    """
    nid = CLIENT.post("/api/notices", json={
        "title": "【自检】已截止的接龙", "category": "notice", "rollcall": 1,
        "deadline": _past(), "roll_note_mode": "text", "roll_note_label": "学号",
    }).json().get("id")
    open_id = CLIENT.post("/api/notices", json={
        "title": "【自检】不限时间的接龙", "category": "notice", "rollcall": 1,
    }).json().get("id")
    try:
        row = db.get_notice(nid, db.roll_context(None))
        check("截止时间已过的事务项 roll_open 为 False", row["roll_open"] is False, row["roll_open"])

        # 1. 接口拒绝（数据层与 HTTP 层都要拒绝）
        ok, msg = db.add_rollcall(nid, "张三", "01", "v19-closed")
        check("数据层拒绝截止后的报名", not ok and "截止" in msg, msg)
        r = CLIENT.post("/api/rollcall/%d" % nid, json={"name": "张三", "note": "01"})
        msg = r.json().get("msg", "") if r.status_code == 400 else r.text[:120]
        check("接口拒绝截止后的报名（400 + 提到截止）",
              r.status_code == 400 and "截止" in msg, msg)
        check("被拒绝的报名没有写进名单", len(db.list_rollcalls(nid)[0]) == 0)

        # 2. 接龙页：没有表单，有截止说明
        page = CLIENT.get("/rollcall/%d" % nid).text
        check("截止后的接龙页不再渲染报名表单",
              'id="rcForm"' not in page and "我要接龙" not in page and 'id="rcName"' not in page)
        check("截止后的接龙页给出截止说明（含截止时间）",
              'id="rcClosed"' in page and "不再接受新的报名" in page
              and row["deadline"] in page)
        check("截止后的接龙页仍可看名单与复制结果",
              'id="rcList"' in page and 'id="rcCopy"' in page)
        # 关闭态下 form / rcName 都不存在，页面脚本必须判空再接线（否则整段 JS 挂掉）
        check("截止后的接龙页脚本没有对着空表单接线",
              "if (form) form.addEventListener" in page)

        # 3. 未填截止时间的照旧随到随报。
        #    这里换一个客户端报名：同一个客户端报完就带上了 rc_token，
        #    首页那张卡片会按"已接龙"渲染（不再有 .roll-cta），下面的卡片断言就落空。
        row_open = db.get_notice(open_id, db.roll_context(None))
        check("没填截止时间的接龙一直开着", row_open["roll_open"] is True, row_open["roll_open"])
        other = TestClient(main.app)
        r = other.post("/api/rollcall/%d" % open_id, json={"name": "李四", "note": ""})
        check("没填截止时间时照旧能报名", r.status_code == 200, r.text[:200])

        # 4. 撤销不受影响：直接塞一条"截止前报的名"，再确认还能撤掉
        with db._session() as cur:
            cur.execute("INSERT INTO rollcalls (notice_id, name, note, token) VALUES ("
                        + ", ".join([db.PH] * 4) + ")", [nid, "王五", "02", "v19-undo"])
            entry_id = cur.lastrowid
        check("撤销截止前登记的条目仍然可以",
              db.cancel_rollcall(nid, entry_id, "v19-undo") is True)
        check("撤销后名单确实少了一条", len(db.list_rollcalls(nid)[0]) == 0)

        # 5. 首页卡片：过期接龙的提示换成「已截止」，不再说"点击卡片进入接龙"
        block = card_block(notice_html(nid), nid)
        check("首页卡片提示「接龙已截止」",
              "接龙已截止" in block and "roll-cta closed" in block, block[-400:])
        check("首页卡片不再说「点击卡片进入接龙」", "点击卡片进入接龙" not in block)
        open_block = card_block(notice_html(open_id), open_id)
        check("没截止的卡片照旧提示可点",
              "点击卡片进入接龙" in open_block and "closed" not in open_block,
              open_block[-400:])
    finally:
        CLIENT.delete("/api/notices/%d" % nid)
        CLIENT.delete("/api/notices/%d" % open_id)


def check_check_mode_migration():
    """
    v1.9.0 的旧库迁移：以前配成 check（打勾叉）的事项，启动时回退成 text。

    这里直接改库里的值再造一次迁移（而不是建新事项），因为新事项已经不可能
    配成 check 了 —— 要验的正是"旧库升级"这条路。
    """
    nid = CLIENT.post("/api/notices", json={
        "title": "【自检】旧 check 类别", "category": "notice", "rollcall": 1,
        "deadline": _future(), "roll_note_mode": "text", "roll_note_label": "是否参加",
    }).json().get("id")
    try:
        con = sqlite3.connect(db.SQLITE_PATH)
        con.execute("UPDATE notices SET roll_note_mode = 'check' WHERE id = ?", (nid,))
        con.commit()
        con.close()

        db._migrate()

        row = db.get_notice(nid, db.roll_context(None))
        check("旧库里的 check 类别被迁移成 text", row["roll_note_mode"] == "text",
              row["roll_note_mode"])
        page = CLIENT.get("/rollcall/%d" % nid).text
        form = page.split('<form id="rcForm"', 1)[1].split('</form>', 1)[0]
        check("迁移后的接龙页渲染成文本框（不再是 √ / × 两个按钮）",
              'id="rcNote"' in form and 'type="radio"' not in form)
    finally:
        CLIENT.delete("/api/notices/%d" % nid)


def check_card_blocks():
    """
    v1.8.0 的首页卡片布局：小信息块合并进 .item-tags；已接龙的那条不再显示
    「已接龙，点击查看名单」，但未接龙的仍要保留「点击卡片进入接龙」。
    """
    r = CLIENT.post("/api/notices", json={
        "title": "【自检】卡片信息块", "category": "notice", "tag": "标签",
        "deadline": _future(), "rollcall": 1,
    })
    nid = r.json().get("id")
    try:
        block = card_block(CLIENT.get("/").text, nid)
        check("首页卡片链接指向接龙页", bool(block))
        check("小信息块装在 .item-tags 里", 'class="item-tags"' in block)
        check("级别徽标 / 标签 / 剩余时间都在同一排",
              block.count('class="item-tag') >= 2 and "item-countdown" in block)
        check("未接龙时保留「点击卡片进入接龙」提示",
              "点击卡片进入接龙" in block and "已接龙，点击查看名单" not in block)

        CLIENT.post("/api/rollcall/%d" % nid, json={"name": "张三", "note": ""})
        joined = card_block(CLIENT.get("/").text, nid)
        check("已接龙后不再出现「已接龙，点击查看名单」", "已接龙，点击查看名单" not in joined)
        check("已接龙后卡片也不再提示「点击卡片进入接龙」", "点击卡片进入接龙" not in joined)
        check("已接龙后仍保留人数与「已接龙」徽标",
              "人接龙" in joined and "已接龙" in joined)
    finally:
        CLIENT.delete("/api/notices/%d" % nid)


def main_check():
    db.init_db()
    if db.BACKEND != "sqlite":
        print("[warn] 当前后端是 %s，本脚本会往真实库里写测试数据" % db.BACKEND)

    # ---------------- 新增：开启接龙 ----------------
    # 截止时间用相对时间（而不是写死 2026-10-20）：v1.9.0 起过了截止时间就不许报名了，
    # 写死的日期一到期，这里整段"接龙成功 / 同名去重 / 撤销"的断言会集体失败，
    # 而失败原因看起来像功能坏了，其实只是自检数据过期。
    r = CLIENT.post("/api/notices", json={
        "title": "【自检】团建报名", "category": "notice", "tag": "测试",
        "note": "点我接龙", "deadline": _future(days=18), "rollcall": 1,
    })
    nid = r.json().get("id")
    check("新增带接龙的事项", r.status_code == 200 and nid, r.text[:200])

    try:
        # 首页卡片整块指向接龙页，并显示人数与「点击进入接龙」
        html = notice_html(nid)
        check("首页卡片链接指向接龙页", ('href="/rollcall/%d"' % nid) in html)
        check("首页显示接龙人数胶囊", ("0 人接龙" in html) and ("点击卡片进入接龙" in html))

        # 接龙页：空名单提示 + 表单
        page = CLIENT.get("/rollcall/%d" % nid)
        check("接龙页可访问且渲染表单", page.status_code == 200 and 'id="rcForm"' in page.text)
        check("接龙页显示空名单提示", "还没有人接龙" in page.text)

        # 未开启接龙 / 不存在的记录 → 404
        check("未开启接龙的事项按 404 处理", CLIENT.get("/rollcall/1").status_code == 404)
        check("不存在的记录按 404 处理", CLIENT.get("/rollcall/999999").status_code == 404)

        # ---------------- 接龙 ----------------
        r = CLIENT.post("/api/rollcall/%d" % nid, json={"name": "张三", "note": "学号 01"})
        check("接龙成功并下发令牌 cookie", r.status_code == 200 and "rc_token" in r.cookies, r.text[:200])

        r = CLIENT.post("/api/rollcall/%d" % nid, json={"name": "张三"})
        check("同名不能重复接龙", r.status_code == 400 and "接龙过" in r.json()["msg"], r.text[:200])

        r = CLIENT.post("/api/rollcall/%d" % nid, json={"name": "   "})
        check("空姓名被拦下", r.status_code == 400, r.text[:200])

        # 第二个人用另一台设备（另一个 cookie 会话）报名，方便区分"我那条"
        other = TestClient(main.app)
        r = other.post("/api/rollcall/%d" % nid, json={"name": "李四", "note": "两人同行"})
        check("第二个人也能接龙", r.status_code == 200, r.text[:200])

        # ---------------- 一台设备连报多条（v1.7.0 明确的规则） ----------------
        # 同一个 cookie 会话继续替同学代报名：设备层面不该有任何次数限制，
        # 只有「同一个姓名在同一事项里只能报一次」这一条校验。
        for who in ("王五", "赵六"):
            r = CLIENT.post("/api/rollcall/%d" % nid, json={"name": who, "note": "代报名"})
            check("同一台设备可以再报一人（%s）" % who, r.status_code == 200, r.text[:200])

        # 数据库里真的有这四行
        entries, mine = db.list_rollcalls(nid, dict(CLIENT.cookies).get("rc_token"))
        check("接龙明细已写进数据库", len(entries) == 4, len(entries))
        check("名单按接龙先后编号", [e["seq"] for e in entries] == [1, 2, 3, 4])
        check("认得出本设备那条记录", mine is not None and mine["name"] == "张三", mine)
        check("本设备登记的三条都认得出来",
              sum(1 for e in entries if e["mine"]) == 3,
              [e["name"] for e in entries if e["mine"]])

        # 首页：同一 cookie 会话 → 已接龙 + 已完成，且不再倒计时
        row = [x for x in CLIENT.get("/api/notices").json()["data"] if x["id"] == nid][0]
        check("已接龙的事项级别为已完成",
              row["joined"] is True and row["level"] == "done" and row["countdown"] == "",
              row)
        check("人数统计正确", row["rollcount"] == 4, row["rollcount"])
        check("首页卡片标记为已接龙", "已接龙" in notice_html(nid))

        # 接龙页：自己的那条高亮 + 可撤销
        page = CLIENT.get("/rollcall/%d" % nid).text
        check("接龙页高亮自己那条", 'class="rc-item mine"' in page)
        check("接龙页提示本设备已登记的条数与名次",
              "这台设备已登记 3 条" in page and "第 1 位" in page)
        check("本设备登记的三条都能撤销，别人那条不能", page.count('class="rc-cancel"') == 3,
              page.count('class="rc-cancel"'))
        # v1.7.0：事项信息整块删掉，只剩标题 + 名单 + 表单 + 复制按钮
        check("接龙页不再渲染「事项信息」块", "事项信息" not in page)
        check("接龙页带「复制接龙结果」按钮", 'id="rcCopy"' in page and "复制接龙结果" in page)

        # 换一台设备（没有 cookie）看不到「已接龙」，但看得到人数
        fresh = TestClient(main.app)
        fresh_html = fresh.get("/").text
        fresh_row = [x for x in fresh.get("/api/notices").json()["data"] if x["id"] == nid][0]
        check("别的设备不会被标成已接龙", fresh_row["joined"] is False and fresh_row["rollcount"] == 4, fresh_row)
        check("别的设备也看得到接龙人数", "4 人接龙" in fresh_html)

        # 撤销：别人的记录删不动
        other_id = [e["id"] for e in entries if not e["mine"]][0]
        r = fresh.delete("/api/rollcall/%d/%d" % (nid, other_id))
        check("撤销别人的接龙被拒绝", r.status_code == 400, r.text[:200])
        check("拒绝后记录还在", len(db.list_rollcalls(nid)[0]) == 4)

        my_id = mine["id"]
        r = CLIENT.delete("/api/rollcall/%d/%d" % (nid, my_id))
        check("撤销自己的接龙", r.status_code == 200 and len(db.list_rollcalls(nid)[0]) == 3, r.text[:200])
        r = CLIENT.delete("/api/rollcall/%d/%d" % (nid, my_id))
        check("重复撤销失败", r.status_code == 400, r.text[:200])

        # ---------------- 修改事项 ----------------
        r = CLIENT.put("/api/notices/%d" % nid, json={
            "title": "【自检】团建报名（已改）", "category": "homework", "tag": "改",
            "note": "改过的备注", "deadline": "2026-10-21 09:30", "location": "香山",
            "pinned": 0, "rollcall": 0,
        })
        check("修改事项成功", r.status_code == 200, r.text[:200])
        row = db.get_notice(nid, db.roll_context(None))
        check("修改后字段全部生效",
              row["title"] == "【自检】团建报名（已改）" and row["category"] == "homework"
              and row["deadline"] == "2026-10-21 09:30" and row["location"] == "香山"
              and row["rollcall"] is False, row)
        check("关闭接龙后名单入口失效", CLIENT.get("/rollcall/%d" % nid).status_code == 404)
        check("关闭接龙但人数仍保留在库里", len(db.list_rollcalls(nid)[0]) == 3)

        r = CLIENT.put("/api/notices/999999", json={"title": "x"})
        check("修改不存在的记录返回 404", r.status_code == 404, r.text[:200])
        r = CLIENT.put("/api/notices/%d" % nid, json={"title": "   "})
        check("修改时标题为空同样被拦", r.status_code == 400, r.text[:200])
        r = CLIENT.put("/api/notices/%d" % nid, json={"title": "x", "category": "不存在的分类"})
        check("修改时非法分类被拦", r.status_code == 400, r.text[:200])

        # 置顶唯一性：改完仍是置顶时要顶替同栏目原来那条
        # 注意这是「抢置顶」，跑完必须把现场还原，否则会把真实数据里的置顶项顶掉
        prev_pinned = [dict(x) for x in db.list_active("notice") if x["pinned"]]
        CLIENT.put("/api/notices/%d" % nid, json={"title": "置顶测试", "category": "notice", "pinned": 1})
        pinned = [x for x in db.list_active("notice") if x["pinned"]]
        check("同栏目仍然只有一条置顶",
              len(pinned) == 1 and pinned[0]["id"] == nid,
              [x["title"] for x in pinned])
        # 还原：先把自己取消置顶，再把原来那条置顶回去（PUT 是整行覆盖，要回传整行）
        CLIENT.put("/api/notices/%d" % nid, json={"title": "置顶测试", "category": "notice", "pinned": 0})
        for row in prev_pinned:
            row["pinned"] = 1
            CLIENT.put("/api/notices/%d" % row["id"], json=row)
        check("自检抢走的置顶已还原",
              [x["id"] for x in db.list_active("notice") if x["pinned"]] == [x["id"] for x in prev_pinned],
              [x["title"] for x in db.list_active("notice") if x["pinned"]])

    finally:
        # ---------------- 清理 ----------------
        CLIENT.delete("/api/notices/%d" % nid)
        con = sqlite3.connect(db.SQLITE_PATH)
        orphans = con.execute("SELECT COUNT(*) FROM rollcalls WHERE notice_id = ?", (nid,)).fetchone()[0]
        con.close()
        check("删除事项时接龙明细一并清掉", orphans == 0, orphans)

    # ---------------- v1.8.0：附加信息配置 + 卡片信息块 ----------------
    check_note_config()
    check_card_blocks()

    # ---------------- v1.9.0：截止后不再收报名 + check 类别迁移 ----------------
    check_rollcall_deadline()
    check_check_mode_migration()

    failed = [r for r in RESULTS if not r[1]]
    print("\n%d/%d passed" % (len(RESULTS) - len(failed), len(RESULTS)))
    if failed:
        print("failed: " + "; ".join(r[0] for r in failed))
        sys.exit(1)


if __name__ == "__main__":
    main_check()
