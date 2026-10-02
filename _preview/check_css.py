# -*- coding: utf-8 -*-
"""样式表自检：括号配平、令牌引用是否都存在（仅本地开发用）。

改 CSS 最容易出的两类错：少一个 `}` 导致后面整片样式失效、
写了 `var(--xxx)` 却没定义过这个令牌（浏览器静默回退，肉眼很难发现）。
这个脚本把两类都查一遍。

用法：python _preview/check_css.py
"""

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSS = os.path.join(ROOT, "static", "style.css")

# 页面内联与外链都要看，但令牌只在 style.css 里定义
TARGETS = [
    os.path.join(ROOT, "static", "style.css"),
    os.path.join(ROOT, "classroot", "index.html"),
    os.path.join(ROOT, "classroot", "admin.html"),
    os.path.join(ROOT, "classroot", "rollcall.html"),
    os.path.join(ROOT, "classroot", "404.html"),
]


def main():
    css = open(CSS, encoding="utf-8").read()
    body = re.sub(r"/\*.*?\*/", "", css, flags=re.S)

    fails = []
    ok = True

    # 1. 括号配平
    opens, closes = body.count("{"), body.count("}")
    print("CSS 括号：{ = %d ，} = %d  ->  %s" % (opens, closes, "OK" if opens == closes else "不匹配"))
    if opens != closes:
        ok = False
        fails.append("括号不配平")

    # 2. var(--x) 引用是否都有定义
    #    注意 --peach 与 --peach-ink 这类同前缀令牌：
    #    声明名的匹配必须排除后面的值（用 [^;{}]+ 吃掉冒号到分号之间的内容），
    #    否则 --peach 会被 --peach-ink 的声明"顺带匹配"掉，误报成未使用。
    defined = set(re.findall(r"(--[a-z0-9-]+)\s*:[^;{}]+;", body))
    used = set()
    for path in TARGETS:
        text = open(path, encoding="utf-8").read()
        # 末尾的 (?![a-z0-9-]) 很关键：没有它时 `var(--peach-ink)` 会同时匹配出
        # `--peach`，于是真正的 --peach 被算成"已使用"，而没被用到的短名又互相掩盖，
        # 反向的"未使用"清单同样会失真。
        used |= set(re.findall(r"var\((--[a-z0-9-]+)(?![a-z0-9-])", text))
    missing = used - defined
    print("CSS 令牌：定义 %d 个，引用 %d 个  ->  %s"
          % (len(defined), len(used), "OK" if not missing else "缺定义: " + ", ".join(sorted(missing))))
    if missing:
        ok = False
        fails.append("引用了未定义的令牌")

    # 3. 定义了但从未使用的令牌（只提示，不算失败）
    unused = defined - used
    if unused:
        print("提示：定义了但未使用 -> %s" % ", ".join(sorted(unused)))

    # 4. 模板里有没有残留的 Font Awesome 图标标记
    for path in TARGETS[1:]:
        text = open(path, encoding="utf-8").read()
        leftovers = re.findall(r'class="[^"]*\bfa[srb]?\b[^"]*"', text)
        if leftovers:
            ok = False
            fails.append("%s 仍有 Font Awesome 标记 %s" % (os.path.basename(path), leftovers[:3]))

    print("\n结论：" + ("全部通过" if ok else "有问题 -> " + "；".join(fails)))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
