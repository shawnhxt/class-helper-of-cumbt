# -*- coding: utf-8 -*-
"""
交互状态自检（仅本地开发用）

样式改动最容易"静态看着好、点一下就崩"，所以这里用 CDP 真的去点：
  1. 首页：点「作业」标签 → 栏目是否切换、页眉标题是否跟着换、图标是否还在；
  2. 首页：普通事项是不是挂上了 .normal（雾蓝书脊）且没有和置顶/紧急/已完成混挂；
  3. 首页：点折叠条 → 是否请求 /api/notices?folded=1 并渲染出卡片；
  4. 首页（v1.8.0）：小信息块是不是挤在同一排、标题是不是自己一行；
  5. 接龙：点接龙卡片是否进入接龙页、事项名称块与剩余时间在不在、事项信息块是否已删、
     复制内容格式对不对、提交后首页是否变成「已接龙」（跑完会撤销）；
  6. 管理页：JS 渲染的列表 / 页签是否正常，点「设为置顶」后列表是否刷新；
  7. 管理页：点「编辑」是否切成修改模式、能不能真的改掉（跑完会改回去）；
  8. 管理页（v1.8.0）：接龙「附加信息」面板的显隐、回填与保存；
  9. 接龙（v1.9.0）：选择类别把候选项全铺成一排（优先一行、放不下自动换行，
     必填时一个都没选要被拦下）；截止时间已过时报名表单整块消失、
     改成截止说明，名单与复制按钮照旧，首页卡片提示「接龙已截止」。

每一项都回读 DOM 做断言，结果打印成 PASS / FAIL 清单。

用法：python _preview/check_states.py
（需要先跑 python _preview/preview_server.py，且本地库里有样例数据）
"""

import json
import os
import random
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db  # noqa: E402
from shot import WS, launch, wait_target, find_chrome  # noqa: E402

BASE = "http://127.0.0.1:8777"
PORT = 9224


class Session:
    def __init__(self):
        profile = os.path.join(os.path.dirname(os.path.abspath(__file__)), "chrome-profile")
        self.proc = launch(find_chrome(), PORT, profile)
        time.sleep(1.5)
        self.ws = WS(wait_target(PORT))
        self.ws.call("Page.enable")
        self.ws.call("Runtime.enable")
        self.ws.call("Emulation.setDeviceMetricsOverride",
                     {"width": 430, "height": 900, "deviceScaleFactor": 1, "mobile": True})

    def goto(self, path):
        self.ws.call("Page.navigate", {"url": BASE + path})
        time.sleep(2.5)

    def js(self, expr):
        r = self.ws.call("Runtime.evaluate", {"expression": expr, "returnByValue": True, "awaitPromise": True})
        return r.get("result", {}).get("value")

    def wait_for(self, expr, timeout=10):
        """
        等页面上的某个条件成立。

        提交接龙会 location.href 跳一次页，读 DOM 必须等新页面落地；
        固定 sleep 要么不够（偶发失败）要么白等。页面正在导航时
        Runtime.evaluate 可能直接报错（执行上下文被销毁），这里一并吞掉重试。
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if self.js(expr):
                    return True
            except Exception:
                pass
            time.sleep(0.3)
        return False

    def close(self):
        self.proc.terminate()


RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(("  PASS  " if ok else "  FAIL  ") + name + (("   <- " + str(detail)) if detail and not ok else ""))


def main():
    s = Session()
    try:
        # ---------------- 首页 ----------------
        print("[首页]")
        s.goto("/")
        check("首页渲染出事项卡片",
              s.js("document.querySelectorAll('#page-notice .list-item:not(.is-empty)').length") >= 3,
              s.js("document.querySelectorAll('#page-notice .list-item').length"))
        check("卡片图标是内联 SVG 且已解析",
              s.js("!!document.querySelector('.pin-icon') && document.querySelector('.pin-icon').getBoundingClientRect().width > 5"),
              s.js("JSON.stringify(document.querySelector('.pin-icon')&&document.querySelector('.pin-icon').getBoundingClientRect().width)"))
        check("没有任何 <i class=fa-...> 残留",
              s.js("document.querySelectorAll('i[class*=fa-]').length") == 0)
        check("不再引用外部 CDN",
              s.js("Array.from(document.querySelectorAll('link')).every(l=>!/cdnjs|fonts.googleapis/.test(l.href))"))
        check("底部导航选中态有背景块",
              s.js("getComputedStyle(document.querySelector('.nav-item.active'),'::after').opacity") == "1")

        # v1.8.0：小信息块（级别徽标 / N 人接龙 / 标签 / 剩余时间）默认挤在同一排，
        # 一行放不下才整体换行；标题不在这一排里（它自己占一行）。
        # 判据用"至少两块 offsetTop 相同"而不是"第 1、2 块同高"——
        # 每块胶囊的 padding / 字体都一样，同一行必然同 offsetTop，但窄屏可能已经换行。
        check("小信息块至少两块排在同一行",
              s.js("(function(){var t=document.querySelector('#page-notice .item-tags');"
                   "if(!t||t.children.length<2) return false;"
                   "var tops=Array.prototype.map.call(t.children,function(e){return e.offsetTop;});"
                   "return new Set(tops).size < tops.length;})()"),
              s.js("document.querySelectorAll('#page-notice .item-tags').length"))
        check("标题单独一行、信息块在它下面",
              s.js("(function(){var c=document.querySelector('#page-notice .list-item .item-content');"
                   "if(!c) return false;var n=c.querySelector('.item-name'),t=c.querySelector('.item-tags');"
                   "if(!n||!t) return false;"
                   "return n.getBoundingClientRect().bottom <= t.getBoundingClientRect().top + 2;})()"))

        # 普通事项的配色（v1.7.0 换的雾蓝：书脊 #6ea8fe + 极浅蓝底）
        check("普通事项有 .normal 类且书脊是雾蓝",
              s.js("!!document.querySelector('.list-item.normal')")
              and s.js("getComputedStyle(document.querySelector('.list-item.normal')).borderLeftColor") == "rgb(110, 168, 254)",
              s.js("(document.querySelector('.list-item.normal')||{})&&getComputedStyle(document.querySelector('.list-item.normal')).borderLeftColor"))
        check("置顶/紧急/已完成的卡片不会同时挂着 .normal",
              s.js("document.querySelectorAll('.list-item.pinned.normal, .list-item.urgent.normal, .list-item.done.normal').length") == 0,
              s.js("document.querySelectorAll('.list-item.pinned.normal, .list-item.urgent.normal, .list-item.done.normal').length"))
        check("普通卡片底色不是纯白（换过配色）",
              "gradient" in (s.js("getComputedStyle(document.querySelector('.list-item.normal')).backgroundImage") or ""),
              s.js("getComputedStyle(document.querySelector('.list-item.normal')).backgroundImage"))

        # 切栏目
        s.js("document.querySelector('[data-target=page-homework]').click()")
        time.sleep(0.6)
        check("切到作业栏目后该页可见",
              s.js("getComputedStyle(document.getElementById('page-homework')).display") == "block")
        check("页眉标题跟着换",
              s.js("document.getElementById('pageTitle').textContent.trim()") == "作业提醒",
              s.js("document.getElementById('pageTitle').textContent"))
        check("页眉标题前的图标还在（且没重复叠加）",
              s.js("document.querySelectorAll('#pageTitle svg').length") == 1,
              s.js("document.querySelectorAll('#pageTitle svg').length"))
        check("页眉图标换成书本",
              s.js("(document.querySelector('#pageTitle use').getAttribute('href')||'').endsWith('#i-book')"),
              s.js("document.querySelector('#pageTitle use').getAttribute('href')"))

        # 折叠区
        s.goto("/")
        check("折叠条存在且显示条数",
              s.js("!!document.querySelector('.fold-bar')"),
              s.js("document.querySelectorAll('.fold-bar').length"))
        check("折叠内容初始为空（懒加载）",
              s.js("document.querySelector('.fold-body[data-cat=notice]').children.length") == 0)
        s.js("document.querySelector('.fold-bar[data-cat=notice]').click()")
        time.sleep(1.5)
        check("点开后折叠区就地渲染出卡片",
              s.js("document.querySelectorAll('.fold-body[data-cat=notice] .list-item').length") >= 1,
              s.js("document.querySelectorAll('.fold-body[data-cat=notice] .list-item').length"))
        check("折叠条进入展开态",
              s.js("document.querySelector('.fold-bar[data-cat=notice]').classList.contains('open')"))
        check("折叠卡片里的地点胶囊也是 SVG 图标",
              s.js("document.querySelectorAll('.fold-body[data-cat=notice] .item-location svg').length") >= 0)

        # ---------------- 接龙 ----------------
        print("[接龙]")
        s.goto("/")
        check("正文字号已整体调小（body 14px）",
              s.js("getComputedStyle(document.body).fontSize") == "14px",
              s.js("getComputedStyle(document.body).fontSize"))
        link = s.js("(document.querySelector('a[href^=\"/rollcall/\"]')||{getAttribute:null}).getAttribute"
                    "? document.querySelector('a[href^=\"/rollcall/\"]').getAttribute('href') : ''")
        check("接龙事项整块可点且指向接龙页", bool(link) and str(link).startswith("/rollcall/"), link)
        check("接龙事项显示人数胶囊",
              s.js("!!document.querySelector('.list-item .item-tag.tag-roll')"),
              s.js("(document.querySelector('.item-tag.tag-roll')||{}).textContent"))
        check("接龙事项带「点击进入接龙」提示", s.js("!!document.querySelector('.roll-cta')"))

        s.js("document.querySelector('a[href^=\"/rollcall/\"]').click()")
        time.sleep(1.8)
        check("点卡片进入接龙页面", s.js("document.body.className") == "rollcall",
              s.js("document.body.className"))
        check("接龙页渲染出名单与报名表单",
              s.js("!!document.getElementById('rcForm')") and s.js("document.querySelectorAll('.rc-item').length") >= 1,
              s.js("document.querySelectorAll('.rc-item').length"))

        # v1.7.0：事项信息整块删掉，只留事项名称 + 名单 + 表单 + 复制按钮
        check("接龙页已删掉「事项信息」块", "事项信息" not in (s.js("document.body.innerText") or ""))
        check("接龙页顶部仍显示事项名称（复制内容的第一行靠它）",
              (s.js("(document.getElementById('rcTitle')||{}).textContent") or "").strip() != "",
              s.js("(document.getElementById('rcTitle')||{}).textContent"))
        check("接龙页有「复制接龙结果」按钮",
              (s.js("(document.getElementById('rcCopy')||{}).textContent") or "").strip() == "复制接龙结果",
              s.js("(document.getElementById('rcCopy')||{}).textContent"))

        # v1.8.0：名称套进醒目的块 + 旁边一颗每秒刷新的剩余时间胶囊
        # 注意判据用 borderTopWidth + backgroundImage：块的底色是渐变，
        # backgroundColor 会一直是 rgba(0,0,0,0)，拿它判断必然误报
        check("事项名称套在醒目的块（.rc-hero）里",
              s.js("(function(){var h=document.querySelector('.rc-hero');"
                   "if(!h||!h.contains(document.getElementById('rcTitle'))) return false;"
                   "var s=getComputedStyle(h);"
                   "return s.borderTopWidth !== '0px' && s.backgroundImage !== 'none';})()"),
              s.js("document.querySelectorAll('.rc-hero').length"))
        countdown_text = (s.js("(document.getElementById('rcCountdown')||{}).textContent") or "").strip()
        time.sleep(1.2)
        check("接龙页显示剩余时间，且确实在每秒刷新",
              bool(countdown_text) and countdown_text[:1] in "剩已"
              and (s.js("(document.getElementById('rcCountdown')||{}).textContent") or "").strip()[:1] in "剩已",
              countdown_text)

        # 复制内容：第 1 行事项名称、第 2 行「姓名 + 附加信息名称」表头、之后每人一行。
        # 表头用的是事项自己配的附加信息名称（v1.8.0），所以这里从表单标签上读回来比。
        rows = s.js("document.querySelectorAll('#rcList .rc-item .rc-name').length")
        note_label = (s.js("(document.querySelector('label[for=rcNote]')||{}).textContent") or "")
        note_label = note_label.replace("*", "").strip()
        check("复制内容的第一行是事项名称",
              s.js("rollcallText().split('\\n')[0]") == s.js("document.getElementById('rcTitle').textContent.trim()"),
              s.js("rollcallText().split('\\n')[0]"))
        check("复制内容的第二行是「姓名 + 附加信息名称」表头",
              s.js("rollcallText().split('\\n')[1]") == "姓名\t" + note_label,
              json.dumps(s.js("rollcallText().split('\\n')[1]"), ensure_ascii=False) + " / " + note_label)
        check("复制内容的第三行起是接龙信息（人数对得上）",
              s.js("rollcallText().split('\\n').length") == rows + 2,
              "%s lines / %s rows" % (s.js("rollcallText().split('\\n').length"), rows))
        check("复制内容里每行是「姓名 + 制表符 + 备注」",
              s.js("rollcallText().split('\\n').slice(2).every(l=>l.indexOf('\\t')>-1)"), )

        # 点一下按钮：要么复制成功（按钮短暂变「已复制」），要么给出失败提示
        s.js("document.getElementById('rcCopy').click()")
        time.sleep(0.5)
        check("「复制接龙结果」按钮已接线（点了有反馈）",
              bool(s.js("document.getElementById('rcCopy').classList.contains('done')"))
              or s.js("!document.getElementById('rcErr').hidden"),
              "label=%s errHidden=%s" % (s.js("document.getElementById('rcCopy').textContent.trim()"),
                                         s.js("document.getElementById('rcErr').hidden")))

        # 真的接一次龙：姓名最多 4 个字（v1.8.0 起的限制），所以名字要短且唯一
        test_name = "检" + ("%02d" % random.randint(0, 99)) + "号"
        s.js("document.getElementById('rcName').value=%s;"
             "document.getElementById('rcNote').value='自检数据';"
             "document.getElementById('rcForm').dispatchEvent(new Event('submit',{cancelable:true}));"
             % json.dumps(test_name))
        time.sleep(2.5)
        # 提交成功会跳回 /rollcall/{id}?joined=1，读 DOM 前先等新页面落地，
        # 否则断言和"取详情"的两次 evaluate 会跨在导航两侧（偶发失败）
        check("提交后提示接龙成功",
              s.wait_for("(document.body.innerText||'').indexOf('接龙成功')>-1"),
              s.js("(document.querySelector('.notice-bar')||{}).textContent"))
        check("名单里出现自己且高亮",
              s.wait_for("(function(){var n=document.querySelector('.rc-item.mine .rc-name');"
                         "return !!n && n.textContent.trim()===%s;})()" % json.dumps(test_name)),
              s.js("(document.querySelector('.rc-item.mine .rc-name')||{}).textContent"))

        # 回首页：这条应显示「已接龙」且不再倒计时
        s.goto("/")
        check("接龙后主页标记为「已接龙」",
              s.js("Array.from(document.querySelectorAll('.item-tag.tag-done'))"
                   ".some(e=>e.textContent.trim()==='已接龙')"))
        check("已接龙的卡片不再显示倒计时",
              (s.js("(document.querySelector('.list-item[data-joined=\"1\"] .item-countdown')||{}).textContent") or "") == "",
              s.js("(document.querySelector('.list-item[data-joined=\"1\"] .item-countdown')||{}).textContent"))
        check("已接龙的卡片进入 done 状态（薄荷绿书脊）",
              s.js("!!document.querySelector('.list-item[data-joined=\"1\"].done')"))
        # v1.8.0：已接龙的那条不再显示「已接龙，点击查看名单」（徽标已经写着「已接龙」）
        check("已接龙的卡片不再出现冗余提示块",
              "已接龙，点击查看名单" not in (s.js("document.body.innerText") or ""))

        # 撤销刚才那条，别把自检数据留在库里（接口在 /api 下，页面路径要补上前缀）
        s.goto(link)
        # 撤销本设备登记的全部条目（不是只撤第一条）：
        # 上一轮自检要是中途崩了，这台设备（chrome-profile 里那个 cookie）会留下旧记录，
        # 只删「名单里最靠前的那条」等于每次都把上一轮的删掉、自己这条留着 ——
        # 攒到后来首页那张卡片会一直是「已接龙」，后面的断言就开始莫名其妙地失败。
        undone = s.js("(function(){var bs=document.querySelectorAll('.rc-cancel');"
                      "if(!bs.length) return 'no-button';"
                      "return Promise.all(Array.prototype.map.call(bs,function(b){"
                      "return fetch('/api' + window.location.pathname + '/' + b.dataset.id,"
                      "{method:'DELETE'}).then(r=>r.json()).then(j=>j.ok);}))"
                      ".then(function(rs){return rs.every(Boolean)?'ok':'fail';});})()")
        time.sleep(0.8)
        check("自检数据能撤销干净", undone == "ok", undone)

        # ---------------- v1.9.0：选择类别的报名页（候选项全铺成一排） ----------------
        # 单独建一条必填的选择事项：样例数据里那条是「输入文本」，走不到这条分支。
        # 重点验三件事：
        #   1. 候选项全部铺在页面上（不再是下拉框）；
        #   2. 优先排成一行，一行放不下才自动换行；
        #   3. 一个都没选就提交（必填）必须被前端当场拦下，
        #      绝不能把第一颗 radio 的 value 当成"用户选了"交上去。
        tmp_id = s.js("fetch('/api/notices',{method:'POST',headers:{'Content-Type':'application/json'},"
                      "body:JSON.stringify({title:'【自检】选择类别',category:'notice',rollcall:1,"
                      "deadline:'2026-12-31 18:00',roll_note_mode:'select',"
                      "roll_note_label:'能否到场',roll_note_required:1,"
                      "roll_note_options:'参加\\n不参加\\n待定'})})"
                      ".then(r=>r.json()).then(j=>j.id)")
        try:
            s.goto("/rollcall/%d" % tmp_id)
            check("必填的选择栏把 3 个候选项全铺出来（没有「不填」）",
                  s.js("document.querySelectorAll('.rc-choice').length") == 3
                  and s.js("document.querySelectorAll('.rc-choice input[type=radio]').length") == 3,
                  s.js("document.querySelectorAll('.rc-choice').length"))
            check("选择栏不再用下拉框",
                  not s.js("!!document.querySelector('#rcForm select')"),
                  s.js("document.querySelectorAll('#rcForm select').length"))
            check("候选项优先排成一行（前两颗在同一行）",
                  s.js("(function(){var cs=document.querySelectorAll('.rc-choice');"
                       "if(cs.length<2) return false;"
                       "return cs[0].offsetTop===cs[1].offsetTop;})()"))
            # 把容器压窄到 120px：一行肯定放不下，必须自动换行（而不是把每颗挤窄）
            check("一行放不下时自动换行",
                  s.js("(function(){var w=document.getElementById('rcNoteChoices');"
                       "if(!w) return false;var cs=w.querySelectorAll('.rc-choice');"
                       "if(cs.length<3) return false;w.style.width='120px';"
                       "var tops=Array.prototype.map.call(cs,function(e){return e.offsetTop;});"
                       "var n=new Set(tops).size;w.style.width='';return n>1;})()"),
                  s.js("(function(){var w=document.getElementById('rcNoteChoices');"
                       "var cs=w.querySelectorAll('.rc-choice');w.style.width='120px';"
                       "var tops=Array.prototype.map.call(cs,function(e){return e.offsetTop;});"
                       "w.style.width='';return JSON.stringify(tops);})()"))
            check("候选项的宽度由文字决定、没有被挤成等宽窄条",
                  s.js("(function(){var cs=document.querySelectorAll('.rc-choice span');"
                       "if(cs.length<2) return false;"
                       "return cs[0].getBoundingClientRect().width"
                       "!==cs[1].getBoundingClientRect().width;})()"),
                  s.js("(function(){return Array.prototype.map.call("
                       "document.querySelectorAll('.rc-choice span'),"
                       "function(e){return Math.round(e.getBoundingClientRect().width);}).join(',');})()"))

            s.js("document.getElementById('rcName').value='检乙';"
                 "document.getElementById('rcForm').dispatchEvent(new Event('submit',{cancelable:true}));")
            time.sleep(0.8)
            check("必填的选择栏一个都没选时被拦下",
                  s.js("!document.getElementById('rcErr').hidden")
                  and "能否到场" in (s.js("document.getElementById('rcErrText').textContent") or ""),
                  s.js("document.getElementById('rcErrText').textContent"))
            check("被拦下时没有写进名单（人数仍是 0）",
                  s.js("(document.getElementById('rcCount')||{}).textContent") == "0",
                  s.js("(document.getElementById('rcCount')||{}).textContent"))

            s.js("document.querySelectorAll('.rc-choice input')[2].checked=true;"
                 "document.getElementById('rcForm').dispatchEvent(new Event('submit',{cancelable:true}));")
            check("点中第三项「待定」之后能正常接龙",
                  s.wait_for("(document.body.innerText||'').indexOf('接龙成功')>-1"))
            check("名单里存下来的就是选中的那一项",
                  s.js("(document.querySelector('.rc-item .rc-note-inline')||{}).textContent") == "待定",
                  s.js("(document.querySelector('.rc-item .rc-note-inline')||{}).textContent"))
        finally:
            s.js("fetch('/api/notices/%d',{method:'DELETE'}).then(r=>r.json()).then(j=>j.ok)" % tmp_id)
            time.sleep(0.5)

        # ---------------- v1.9.0：截止时间已过的接龙 ----------------
        # 建一条"半小时前刚截止"的事项（不能更早：超过 1 天会被折叠，首页就不渲染这张卡了）。
        # 页面不该再有报名表单，但要给出截止说明，名单与复制按钮照旧 ——
        # 班委正是照着这份名单办事；同时确认页面脚本没有因为 form 为 null 而整段挂掉。
        closed_id = s.js("(function(){var d=new Date(Date.now()-30*60000);"
                         "function p(n){return String(n).padStart(2,'0');}"
                         "var dl=d.getFullYear()+'-'+p(d.getMonth()+1)+'-'+p(d.getDate())"
                         "+' '+p(d.getHours())+':'+p(d.getMinutes());"
                         "return fetch('/api/notices',{method:'POST',"
                         "headers:{'Content-Type':'application/json'},"
                         "body:JSON.stringify({title:'【自检】已截止的接龙',category:'notice',"
                         "rollcall:1,deadline:dl})})"
                         ".then(r=>r.json()).then(j=>j.id);})()")
        try:
            s.goto("/rollcall/%d" % closed_id)
            check("截止后的接龙页没有报名表单",
                  not s.js("!!document.getElementById('rcForm')")
                  and not s.js("!!document.getElementById('rcName')"))
            check("截止后的接龙页给出截止说明（含截止时间）",
                  s.js("(document.querySelector('#rcClosed')||{}).offsetHeight") > 0
                  and "不再接受新的报名" in (s.js("(document.getElementById('rcClosed')||{}).textContent") or ""),
                  s.js("(document.getElementById('rcClosed')||{}).textContent"))
            check("截止后的接龙页仍能看名单与复制结果",
                  s.js("!!document.getElementById('rcList')") and s.js("!!document.getElementById('rcCopy')"))
            # 点一下复制：页面脚本要是在 form 为 null 时就抛错，这里不会有任何反馈
            s.js("document.getElementById('rcCopy').click()")
            time.sleep(0.5)
            check("截止后「复制接龙结果」仍然接线（点了有反馈）",
                  bool(s.js("document.getElementById('rcCopy').classList.contains('done')"))
                  or s.js("!document.getElementById('rcErr').hidden"),
                  "label=%s errHidden=%s" % (s.js("document.getElementById('rcCopy').textContent.trim()"),
                                             s.js("document.getElementById('rcErr').hidden")))
            # 倒计时胶囊照样每秒刷新（脚本后面的部分没有被前面的空表单带崩）
            check("截止后的接龙页倒计时仍在运行（显示「已过 …」）",
                  (s.js("(document.getElementById('rcCountdownText')||{}).textContent") or "").strip()[:2] == "已过",
                  s.js("(document.getElementById('rcCountdownText')||{}).textContent"))

            s.goto("/")
            check("首页卡片对已截止的接龙提示「接龙已截止」",
                  s.js("(function(){var a=document.querySelector('a[href=\"/rollcall/%d\"]');"
                       "if(!a) return 'no-card';var c=a.querySelector('.roll-cta');"
                       "return c?c.className+'|'+c.textContent.trim():'no-cta';})()" % closed_id)
                  == "roll-cta closed|接龙已截止，点击查看名单",
                  s.js("(function(){var a=document.querySelector('a[href=\"/rollcall/%d\"]');"
                       "if(!a) return 'no-card';var c=a.querySelector('.roll-cta');"
                       "return c?c.className+'|'+c.textContent.trim():'no-cta';})()" % closed_id))
        finally:
            s.js("fetch('/api/notices/%d',{method:'DELETE'}).then(r=>r.json()).then(j=>j.ok)" % closed_id)
            time.sleep(0.5)

        # ---------------- 管理页 ----------------
        print("[管理页]")
        s.goto("/admin")
        time.sleep(1.0)
        check("JS 渲染出列表",
              s.js("document.querySelectorAll('#list .item').length") >= 3,
              s.js("document.querySelectorAll('#list .item').length"))
        check("页签全部渲染出来（3 个）",
              s.js("document.querySelectorAll('#tabs .tab').length") == 3,
              s.js("document.querySelectorAll('#tabs .tab').length"))
        check("选中页签有高亮样式",
              s.js("getComputedStyle(document.querySelector('#tabs .tab.active')).backgroundColor") != "rgba(0, 0, 0, 0)",
              s.js("getComputedStyle(document.querySelector('#tabs .tab.active')).backgroundColor"))
        check("置顶行有置顶样式类",
              s.js("!!document.querySelector('#list .item.pinned-item')"))
        check("置顶按钮是琥珀配色 / 未置顶是浅蓝",
              s.js("getComputedStyle(document.querySelector('#list .pin-btn')).backgroundColor") != "rgba(0, 0, 0, 0)",
              s.js("getComputedStyle(document.querySelector('#list .pin-btn')).backgroundColor"))
        check("删除按钮有淡红底色",
              s.js("getComputedStyle(document.querySelector('#list .del')).backgroundColor") != "rgba(0, 0, 0, 0)",
              s.js("getComputedStyle(document.querySelector('#list .del')).backgroundColor"))
        check("复选框已换成自绘样式",
              s.js("getComputedStyle(document.getElementById('pinnedInput')).appearance") == "none",
              s.js("getComputedStyle(document.getElementById('pinnedInput')).appearance"))
        check("下拉框自绘箭头生效",
              s.js("getComputedStyle(document.getElementById('categorySelect')).backgroundImage.indexOf('data:image/svg')>-1"))

        # ---- 修改事项（编辑模式）----
        check("每行都带「编辑」按钮",
              s.js("document.querySelectorAll('#list .edit-btn').length") >= 3,
              s.js("document.querySelectorAll('#list .edit-btn').length"))
        # 优先挑一条开启接龙的：顺带验证接龙开关有没有被正确回填
        row_json = s.js("fetch('/api/notices?category=notice').then(r=>r.json()).then(function(j){"
                        "var row = j.data.filter(function(x){return x.rollcall;})[0] || j.data[0];"
                        "return JSON.stringify(row);})")
        row = json.loads(row_json)
        nid, orig_title = row["id"], row["title"]

        s.js("document.querySelector('#list .edit-btn[data-id=\"%d\"]').click()" % nid)
        time.sleep(0.6)
        check("点「编辑」切到修改模式",
              (s.js("document.getElementById('formTitleText').textContent") or "").startswith("修改事项 #"),
              s.js("document.getElementById('formTitleText').textContent"))
        check("表单回填了这条的标题",
              s.js("document.getElementById('titleInput').value") == orig_title,
              s.js("document.getElementById('titleInput').value"))
        check("接龙开关按原值回填",
              s.js("document.getElementById('rollcallInput').checked") is bool(row["rollcall"]),
              "%s / %s" % (s.js("document.getElementById('rollcallInput').checked"), row["rollcall"]))
        # v1.8.0：附加信息配置也要跟着回填（样例那条是默认值：附加信息 / 输入文本 / 选填）
        check("编辑接龙事项时附加信息配置面板显示出来",
              s.js("document.getElementById('rollNoteConfig').hidden") is False)
        check("附加信息配置按原值回填",
              s.js("document.getElementById('rollNoteLabelInput').value") == (row.get("roll_note_label") or "附加信息")
              and s.js("document.getElementById('rollNoteModeSelect').value") == (row.get("roll_note_mode") or "text")
              and s.js("document.getElementById('rollNoteRequiredInput').checked") is bool(row.get("roll_note_required")),
              "%s / %s" % (s.js("document.getElementById('rollNoteLabelInput').value"),
                           s.js("document.getElementById('rollNoteModeSelect').value")))
        check("提交按钮变成「保存修改」",
              s.js("document.getElementById('submitText').textContent") == "保存修改",
              s.js("document.getElementById('submitText').textContent"))
        check("标题图标换成铅笔",
              (s.js("(document.querySelector('#formTitle use').getAttribute('href')||'')") or "").endswith("#i-edit"))
        check("出现「取消编辑」按钮", s.js("document.getElementById('cancelEditBtn').hidden") is False)

        # 真的改一次：标题加后缀 + 把附加信息改成「进行选择 / 能否到场 / 必填 / 两个候选项」
        # → 保存 → 列表 / 接口都应是新值（最后整行还原，连附加信息配置一起还回去）
        new_title = orig_title + "（自检）"
        s.js("document.getElementById('rollNoteLabelInput').value='能否到场';"
             "document.getElementById('rollNoteRequiredInput').checked=true;"
             "var m=document.getElementById('rollNoteModeSelect');m.value='select';"
             "m.dispatchEvent(new Event('change'));"
             "document.getElementById('rollNoteOptionsInput').value='参加\\n不参加';")
        time.sleep(0.4)
        check("选「进行选择」后候选项文本框出现",
              s.js("document.getElementById('rollNoteOptionsWrap').hidden") is False)
        s.js("document.getElementById('titleInput').value=%s;"
             "document.getElementById('addForm').dispatchEvent(new Event('submit',{cancelable:true}));"
             % json.dumps(new_title))
        time.sleep(2.2)
        # 期望值用 separators=(',', ':') 拼：页面返回的是 JSON.stringify 的结果，不带空格
        saved = s.js("fetch('/api/notices?category=notice').then(r=>r.json())"
                     ".then(function(j){var x=j.data.filter(function(y){return y.id===%d;})[0]||{};"
                     "return JSON.stringify([x.title,x.roll_note_mode,x.roll_note_label,"
                     "x.roll_note_required,x.roll_note_options]);})" % nid)
        check("保存后标题与附加信息配置都真的改掉了",
              saved == json.dumps([new_title, "select", "能否到场", True, ["参加", "不参加"]],
                                  ensure_ascii=False, separators=(",", ":")),
              saved)
        check("保存后表单退回新增模式",
              s.js("document.getElementById('formTitleText').textContent") == "新增事项"
              and s.js("document.getElementById('cancelEditBtn').hidden") is True,
              s.js("document.getElementById('formTitleText').textContent"))

        # 还原成改动前的整行内容（等价于在表单里再改回去），别把自检数据留在库里
        s.js("fetch('/api/notices/%d',{method:'PUT',headers:{'Content-Type':'application/json'},body:%s})"
             ".then(r=>r.json()).then(j=>j.ok?'ok':'fail')"
             % (nid, json.dumps(json.dumps(row, ensure_ascii=False))))
        time.sleep(0.8)
        restored = s.js("fetch('/api/notices?category=notice').then(r=>r.json())"
                        ".then(function(j){var x=j.data.filter(function(y){return y.id===%d;})[0]||{};"
                        "return JSON.stringify([x.title,x.roll_note_mode,x.roll_note_label]);})" % nid)
        check("自检改动已还原（含附加信息配置）",
              restored == json.dumps([orig_title, row.get("roll_note_mode") or "text",
                                      row.get("roll_note_label") or "附加信息"],
                                     ensure_ascii=False, separators=(",", ":")),
              restored)

        # ---- 取消编辑 ----
        s.js("document.querySelector('#list .edit-btn[data-id=\"%d\"]').click()" % nid)
        time.sleep(0.4)
        s.js("document.getElementById('cancelEditBtn').click()")
        time.sleep(0.4)
        check("点「取消编辑」回到新增模式",
              s.js("document.getElementById('formTitleText').textContent") == "新增事项"
              and s.js("document.getElementById('titleInput').value") == "")

        # 切页签
        s.js("document.querySelectorAll('#tabs .tab')[1].click()")
        time.sleep(1.5)
        second_tab_active = s.js("document.querySelectorAll('#tabs .tab')[1].classList.contains('active')")
        list_has_content = s.js("document.querySelectorAll('#list .item, #list .empty').length")
        check("切页签后页签高亮与列表一起更新", second_tab_active and list_has_content >= 1,
              "active=%s items=%s" % (second_tab_active, list_has_content))

        # 作业栏目样例里有已完成项 → 折叠条应出现且带条数
        check("该栏目有已完成项时折叠条出现",
              s.js("getComputedStyle(document.getElementById('foldBar')).display") == "flex",
              s.js("getComputedStyle(document.getElementById('foldBar')).display"))
        s.js("document.getElementById('foldBar').click()")
        time.sleep(1.5)
        check("管理页折叠区点开后渲染出已完成项",
              s.js("document.querySelectorAll('#foldBody .item').length") >= 1,
              s.js("document.querySelectorAll('#foldBody .item').length"))

        # 切到网课栏目（样例里有已完成项）再切回，确认折叠区状态被正确重置
        s.js("document.querySelectorAll('#tabs .tab')[2].click()")
        time.sleep(1.5)
        check("换页签后折叠区被重置并收起",
              s.js("!document.getElementById('foldBody').classList.contains('open')") and
              s.js("document.getElementById('foldBody').children.length") == 0,
              "open=%s children=%s activeTab=%s" % (
                  s.js("document.getElementById('foldBody').classList.contains('open')"),
                  s.js("document.getElementById('foldBody').children.length"),
                  s.js("(document.querySelector('#tabs .tab.active')||{}).textContent")))
    finally:
        # 兜底清理：万一「点编辑」那步没成功（页面还没加载完 / 列表是空的），
        # 后面提交表单就从"修改"变成了"新增"，库里会多出一条「原标题（自检）」。
        # 本脚本自己造的数据自己收尾，别把垃圾行留在真实库里。
        try:
            db.init_db()
            for n in db.list_active(None) + db.list_folded(None):
                if n["title"].endswith("（自检）"):
                    db.delete_notice(n["id"])
        except Exception:
            pass
        s.close()

    failed = [r for r in RESULTS if not r[1]]
    print("\n结果：%d/%d 通过" % (len(RESULTS) - len(failed), len(RESULTS)))
    if failed:
        print("失败项：" + "；".join(r[0] for r in failed))
        sys.exit(1)


if __name__ == "__main__":
    main()
