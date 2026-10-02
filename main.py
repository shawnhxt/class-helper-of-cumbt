import re
import secrets

from fastapi import FastAPI, HTTPException, Request
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.responses import Response
from markupsafe import Markup

import db

app = FastAPI()
templates = Jinja2Templates(directory="classroot")

app.mount("/static", StaticFiles(directory="static"), name="static")

# 接龙认人用的 cookie：项目没有账号体系，给每台设备发一个随机令牌，
# 「我接龙了没有」这件事靠它认。令牌不需要被脚本读到，所以设成 HttpOnly；
# same_site=lax 保证从别处点进来时也带着它，同时挡住跨站表单提交。
# 注意：这里不设 secure，因为线上是普通 http，设了浏览器会直接不保存。
ROLLCALL_COOKIE = "rc_token"
ROLLCALL_COOKIE_MAX_AGE = 400 * 24 * 3600     # 约 13 个月，够一学年

# cookie 是客户端提交的，属于不可信输入：先卡死格式再进 SQL 参数，
# 避免以后有人改动这里时把任意长字符串带下去。
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")


def _visitor_token(request: Request, create: bool = False):
    """取当前访客的接龙令牌；格式不对一律当没有。create=True 时新发一个。"""
    token = (request.cookies.get(ROLLCALL_COOKIE) or "").strip()
    if not _TOKEN_RE.match(token):
        token = ""
    if not token and create:
        token = secrets.token_urlsafe(16)
    return token


@app.on_event("startup")
async def _startup():
    # 启动时选择数据库后端（MySQL 优先，连不上降级 SQLite）并建表
    db.init_db()


def _json(payload, status_code=200):
    """
    统一的 JSON 响应出口。

    不用 JSONResponse 的原因：它内部调 json.dumps 且没有 default 兜底，
    MySQL 驱动返回的 datetime 等类型会直接抛 TypeError → 接口 500 →
    前端列表区一片空白。db.json_dumps 带了类型兜底，任何列类型都安全。
    """
    return Response(
        content=db.json_dumps(payload),
        status_code=status_code,
        media_type="application/json",
    )


# ---------------------------------------------------------------- 页面
@app.get("/")
async def index_html(request: Request):
    """
    首页只服务端渲染「未折叠」的事项（进行中 + 刚完成 + 未填截止时间 + 全部置顶项）。
    已完成且超过阈值天数的不查、不渲染，只把条数传给模板显示在折叠条上，
    用户点开时再由前端调 /api/notices?folded=1 按需加载。

    接龙状态按访客的 cookie 令牌现算：三个栏目共用一次 roll_context 查询结果。
    """
    categories = db.CATEGORY_ORDER
    ctx = db.roll_context(_visitor_token(request))
    return templates.TemplateResponse(
        request, "index.html",
        {
            "notices": db.list_active("notice", ctx=ctx),
            "homeworks": db.list_active("homework", ctx=ctx),
            "onlines": db.list_active("online", ctx=ctx),
            "folded_counts": {c: db.count_folded(c) for c in categories},
            "categories": db.CATEGORIES,
            "category_order": categories,
            "level_name": db.LEVEL_NAME,
            "second_precision_minutes": db.SECOND_PRECISION_MINUTES,
            # 客户端倒计时刷新需要知道紧急阈值与折叠天数，全部由后端注入，
            # 避免前后端各写一份数字导致判定不一致。
            # 这些是后端自己生成的常量字典（不含用户输入），用 Markup 标记，
            # 否则 Jinja 自动转义会把引号变成 &quot; 而破坏 JS 语法。
            "urgent_hours_json": Markup(db.json_dumps(db.URGENT_HOURS)),
            "level_name_json": Markup(db.json_dumps(db.LEVEL_NAME)),
            "fold_done_days": db.FOLD_DONE_DAYS,
        },
    )


@app.get("/rollcall/{notice_id}")
async def rollcall_page(request: Request, notice_id: int, joined: int = 0, undo: int = 0):
    """
    接龙页面（v1.6.0 新增）。整页服务端渲染：
    名单、我的状态、"已接龙"标记全部来自数据库，
    提交接龙走 /api/rollcall/{id}（JSON），成功后带 ?joined=1 跳回本页。

    v1.7.0 起本页只渲染「事项名称 + 名单 + 报名表单」：分类、级别、截止时间、
    地点、备注、外链这些在首页卡片上已经有了，接龙页重复一遍只是干扰。

    v1.8.0 起本页多了两样东西：
      · 事项名称外面套一个高亮块，并在旁边显示剩余时间（截至截止时间，
        由客户端每秒刷新；本人已接龙也照样显示 —— 那是给还没报名的同学看的）；
      · 报名表单的第二栏不再是写死的「备注」，而是该事项配置的「附加信息」：
        名称、类别（文本 / 选择）、是否必填、候选项全部来自 notices 表。

    v1.9.0 起截止时间一过就关门：不再渲染报名表单，改为一条「已于 … 截止」的提示，
    名单与「复制接龙结果」照旧（班委要照着名单办事）。判定与接口同源，
    都由 db.rollcall_open 给出，页面藏起来只是省得同学白填一遍。

    成功提示用 query 参数而不是 session 或 cookie：跳回来显示一次，
    刷新页面不会重复提示，也不需要额外存状态（提交成功 → 跳回本页带 ?joined=1）。
    未开启接龙或记录不存在，一律按 404 页处理。
    """
    token = _visitor_token(request)
    row = db.get_notice(notice_id, db.roll_context(token))
    if row is None or not row.get("rollcall"):
        raise HTTPException(status_code=404)

    entries, mine = db.list_rollcalls(notice_id, token)
    # 关门提示语在这里就算好（而不是在模板里拼日期）：与接口返回给前端的
    # 那句 400 提示出自同一个 db.rollcall_closed_text，不会各说各的。
    roll_open = db.rollcall_open(row)
    return templates.TemplateResponse(
        request, "rollcall.html",
        {
            "n": row,
            "entries": entries,
            "mine": mine,
            # 同一台设备可能替同学/家人一起报，页面上提一句"本设备共登记 N 条"
            "my_count": sum(1 for e in entries if e["mine"]),
            "just_joined": bool(joined),
            "just_undone": bool(undo),
            "level_name": db.LEVEL_NAME,
            # 剩余时间：单独算一遍而不是用 n.countdown —— 后者在本人已接龙时是空的
            # （首页对已接龙的不再倒数），但接龙页的剩余时间是给还没报名的人看的。
            "countdown": db.countdown_text(row),
            "second_precision_minutes": db.SECOND_PRECISION_MINUTES,
            # v1.9.0：还收不收新的报名 + 「已于 … 截止」的整句提示
            "roll_open": roll_open,
            "closed_text": "" if roll_open else db.rollcall_closed_text(row),
            "name_max": db.ROLLCALL_NAME_MAX,
            "note_max": db.ROLLCALL_NOTE_MAX,
        },
    )


@app.get("/admin")
async def admin_page(request: Request):
    # 管理页面（暂不鉴权）
    backend, err = db.status()
    return templates.TemplateResponse(
        request, "admin.html",
        {
            "backend": backend,
            "db_error": err,
            "categories": db.CATEGORIES,
            "category_order": db.CATEGORY_ORDER,
            "urgent_hours": db.URGENT_HOURS,
            "level_name": db.LEVEL_NAME,
            # 接龙「附加信息」的类别清单（v1.8.0）：key 入库，value 是管理页下拉的中文名
            "roll_note_modes": db.ROLL_NOTE_MODE_NAME,
            "roll_note_mode_order": db.ROLL_NOTE_MODES,
            "roll_note_label_default": db.ROLL_NOTE_LABEL_DEFAULT,
            "roll_note_options_max": db.ROLL_NOTE_OPTIONS_MAX,
            "roll_note_label_max": db.ROLL_NOTE_LABEL_MAX,
            "note_max": db.ROLLCALL_NOTE_MAX,
            "name_max": db.ROLLCALL_NAME_MAX,
        },
    )


# ---------------------------------------------------------------- 接口
@app.get("/api/notices")
async def api_list_notices(request: Request, category: str = "", folded: int = 0):
    """
    事项列表。
      folded=0（默认）→ 未折叠部分，管理页与首页折叠区之外的内容
      folded=1        → 已折叠部分，仅在用户点开折叠区时才请求
    这样折叠着的事项平时完全不查库，减轻服务器负担。
    """
    cat = category if category in db.CATEGORIES else None
    ctx = db.roll_context(_visitor_token(request))
    rows = (db.list_folded(cat, ctx=ctx) if folded
            else db.list_active(cat, ctx=ctx))
    return _json({"ok": True, "backend": db.BACKEND,
                  "folded": bool(folded), "count": len(rows), "data": rows})


@app.get("/api/counts")
async def api_counts():
    """各分类的「进行中 / 已折叠」条数，管理页分栏用，只查 COUNT"""
    return _json({"ok": True, "groups": db.group_counts()})


@app.post("/api/notices")
async def api_add_notice(request: Request):
    payload = await request.json()
    try:
        new_id = db.add_notice(payload)
    except ValueError as exc:
        return _json({"ok": False, "msg": str(exc)}, status_code=400)
    return _json({"ok": True, "id": new_id})


@app.put("/api/notices/{notice_id}")
async def api_update_notice(notice_id: int, request: Request):
    """
    修改事项信息（v1.6.0）。请求体与新增完全一致，字段校验也共用同一套，
    所以管理页把表单原样提交上来即可，不存在"编辑能绕过校验"的口子。
    """
    payload = await request.json()
    try:
        hit = db.update_notice(notice_id, payload)
    except ValueError as exc:
        return _json({"ok": False, "msg": str(exc)}, status_code=400)
    if not hit:
        return _json({"ok": False, "msg": "记录不存在"}, status_code=404)
    return _json({"ok": True})


@app.put("/api/notices/{notice_id}/pin")
async def api_set_pin(notice_id: int, request: Request):
    """
    设置/取消置顶。请求体 {"pinned": true|false}。

    每个板块只允许一条置顶：置为 true 时后端会自动把同分类的其他置顶清掉，
    并返回被顶替掉的条数，前端据此提示。取消置顶只能从这里发起，没有其他自动途径。
    """
    payload = await request.json()
    want = payload.get("pinned")
    if not isinstance(want, bool):
        return _json({"ok": False, "msg": "pinned 必须是 true 或 false"}, status_code=400)

    hit, category_name, displaced = db.set_pinned(notice_id, want)
    if not hit:
        return _json({"ok": False, "msg": "记录不存在"}, status_code=404)

    return _json({"ok": True, "pinned": want, "category_name": category_name,
                  "displaced": displaced})


@app.delete("/api/notices/{notice_id}")
async def api_delete_notice(notice_id: int):
    deleted = db.delete_notice(notice_id)
    if not deleted:
        return _json({"ok": False, "msg": "记录不存在"}, status_code=404)
    return _json({"ok": True})


# ---------------------------------------------------------------- 接龙接口
@app.post("/api/rollcall/{notice_id}")
async def api_join_rollcall(notice_id: int, request: Request):
    """
    接龙。请求体 {"name": "...", "note": "..."}。

    note 就是该事项的「附加信息」（v1.8.0）：类别可以自由填文本 /
    从管理页配好的选项里挑一个，但传上来的永远是一个字符串，
    数据库里也只存这一个字符串列。姓名与附加信息的长度、类别合法性
    全部由 db.add_rollcall 按该事项的配置校验（前端 maxlength 只是顺手）。

    v1.9.0：事项的截止时间一过，db.add_rollcall 直接拒绝（400 + 「已于 … 截止」），
    接龙页也早就把表单收起来了。页面上的隐藏不算数 —— 同学可以在截止前
    打开页面、截止后才点提交，所以拦截必须落在这一层。

    成功后把随机令牌写进 cookie：首页下一次渲染就知道"这台设备接过了"，
    那条事项会显示「已接龙」并按已完成处理（登记的是设备，不是人 ——
    没有账号体系时这是最省事又不需要登录的做法）。

    令牌只用来认"名单里哪些行是本设备报的"（决定高亮与能否撤销），
    **不用来限制次数**：同一台设备可以连续替多位同学代报名，报多少条都行；
    唯一的限制是同一个姓名在同一事项里只能报一次（代报名时改姓名即可）。
    """
    payload = await request.json()
    token = _visitor_token(request, create=True)
    ok, msg = db.add_rollcall(notice_id, payload.get("name"), payload.get("note"), token)
    if not ok:
        return _json({"ok": False, "msg": msg}, status_code=400)

    resp = _json({"ok": True, "msg": msg})
    resp.set_cookie(
        ROLLCALL_COOKIE, token,
        max_age=ROLLCALL_COOKIE_MAX_AGE, path="/",
        httponly=True, samesite="lax",
    )
    return resp


@app.delete("/api/rollcall/{notice_id}/{entry_id}")
async def api_cancel_rollcall(notice_id: int, entry_id: int, request: Request):
    """撤销接龙：只允许撤销自己设备登记的那条，别人的记录删不动"""
    token = _visitor_token(request)
    if not db.cancel_rollcall(notice_id, entry_id, token):
        return _json({"ok": False, "msg": "只能撤销本设备登记的接龙"}, status_code=400)
    return _json({"ok": True})


@app.exception_handler(404)
async def custom_404_handler(request, exc):
    # 状态码保持 404（原来是 200，会让搜索引擎和调用方以为页面存在）
    return templates.TemplateResponse(
        request, "404.html", {}, status_code=404
    )
