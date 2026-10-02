# -*- coding: utf-8 -*-
"""
班级事务管理系统 —— 数据访问层

设计目标：
1. 线上（Linux 服务器）使用 MySQL，**账密只从同目录的 db.conf 读取**，
   代码里不留任何默认账号密码（宝塔注入的环境变量常传不到 gunicorn worker，故不再依赖）。
2. 本机离线开发环境（未安装 MySQL）自动降级到同目录下的 SQLite 文件，
   保证代码路径、接口、页面与线上一致，无需为开发环境维护第二套实现。

优先级规则（v1.2.0 起）：
    「级别」与「权重」两个字段已合并为一个 **截止时间 deadline**（精确到分钟）。
    level 不再入库，而是每次查询时按 deadline 与当前时间实时计算：
      - 班级通知 / 作业提醒：距截止 ≤ 24 小时 → 紧急；> 24 小时 → 普通
      - 网课提醒　　　　　：距截止 ≤ 72 小时 → 紧急；> 72 小时 → 普通
      - 截止时间已过　　　：一律 → 已完成
      - 未填截止时间　　　：→ 普通（无法判断，按最低紧急度处理）
    排序同样由 deadline 决定：未过期按时间升序（越近越靠前），
    未填时间的排在其后，已完成的沉底。

接龙规则（v1.6.0 起）：
    notices.rollcall = 1 表示该事项开启接龙，首页卡片可点、进入 /rollcall/{id}；
    报名明细存在同库的 rollcalls 表（一条一行，序号 = 按 id 升序的名次）。
    项目没有账号体系，用 cookie 里的随机令牌认"这台设备"：接龙后首页那条
    显示「已接龙」并按已完成处理。名单本身对所有人可见。

接龙截止规则（v1.9.0 起）：
    事项填了截止时间，且截止时间已过，就不再收新的报名（add_rollcall 直接拒绝），
    接龙页也不再渲染报名表单 —— 名单到此为止，班委才好照着名单办事。
    没填截止时间的事项永远开着；撤销不受影响（那是纠错，不是新的报名）。

接龙的「附加信息」规则（v1.8.0 起，v1.9.0 去掉「打勾叉」）：
    原来接龙表单里那个自由填写的「备注」，现在由事项自己决定怎么填：
      · roll_note_label    附加信息叫什么（管理页自己起名，如「学号」「能否到场」）
      · roll_note_mode     怎么填：text 输入文本 / select 从选项里挑
      · roll_note_required 是否强制填写（不强制就可以留空）
      · roll_note_options  select 用的候选项（一行一个，存成字符串）
    两类在库里**都是同一个 rollcalls.note 字符串列**：选择存选中那一项的文字，
    文本框存原文。类别只是"填的时候省点事"，落库与导出（复制接龙结果）
    完全不区分，所以以后加类别也不用改表。
    v1.9.0 删掉了原来的 check（打勾叉）：它只是"√ / × 两个选项的选择"，
    用 select + 两个候选项就能表达，留着反而让管理页多一个要理解的概念。
    旧库里配成 check 的事项在启动迁移时回退成 text（原来填的 √ / × 原样留在名单里）。

对上层只暴露统一函数，SQL 全部使用参数化占位符，避免注入。
"""

import decimal
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta

# ---------------------------------------------------------------- 配置区
# 数据库账密的唯一来源：项目根目录（与本文件同级）的 db.conf
#     [mysql]
#     host     = 127.0.0.1
#     port     = 3306
#     user     = 你的数据库用户名
#     password = 你的数据库密码
#     db       = 你的数据库名
# 代码里不留任何默认账号/密码：缺配置就明确报错，而不是拿空值去撞 1045。
_CONF_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "db.conf")


def _load_conf_file():
    """
    读 db.conf（INI 格式），返回 dict；文件缺失或格式不对返回空 dict，不影响启动。

    为什么用文件而不是宝塔环境变量：面板注入的 env 经常传不到 gunicorn worker
    （worker 由 gunicorn 自己 fork），结果程序读到的是默认值，报 1045。
    """
    if not os.path.exists(_CONF_PATH):
        return {}
    try:
        import configparser

        parser = configparser.ConfigParser()
        parser.read(_CONF_PATH, encoding="utf-8")
        if not parser.has_section("mysql"):
            return {}
        return dict(parser.items("mysql"))
    except Exception:
        return {}


_CONF_FILE = _load_conf_file()


# db.conf 里的键名 -> MYSQL_CONF 里的键名，允许写 db 或 database
_CONF_ALIASES = {
    "host": ("host",),
    "port": ("port",),
    "user": ("user",),
    "password": ("password",),
    "database": ("db", "database"),
}


def _conf(key, default=""):
    """只从 db.conf 取值。空字符串一律视为未设置。"""
    for alias in _CONF_ALIASES.get(key, ()):
        value = _CONF_FILE.get(alias)
        if value is not None and str(value).strip():
            return str(value).strip()
    return default


MYSQL_CONF = {
    "host": _conf("host", "127.0.0.1"),
    "port": int(_conf("port", "3306")),
    "user": _conf("user"),
    "password": _conf("password"),
    "database": _conf("database"),
    "charset": "utf8mb4",
}


def _missing_conf_keys():
    """db.conf 里缺了哪些必填项，用于给出明确报错而不是让人去猜 1045"""
    required = [("user", "user"), ("password", "password"), ("db", "database")]
    return [label for label, key in required if not MYSQL_CONF.get(key)]

# 本机降级用的 SQLite 文件
SQLITE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "classroom.db")

# 当前实际使用的后端："mysql" 或 "sqlite"，init_db() 后确定
BACKEND = None

# MySQL 连接失败时的降级原因，供管理页提示条展示
LAST_ERROR = None

# MySQL 用 %s，SQLite 用 ?，统一由 PH 提供
PH = "?"

# 分类定义：key 入库，value 为中文名（管理页下拉、首页栏目标题共用）
CATEGORIES = {
    "notice": "班级通知",
    "homework": "作业提醒",
    "online": "网课提醒",
}

# 分类展示顺序：管理页页签、首页栏目一律按此顺序渲染。
# 单独列出来而不是依赖 CATEGORIES 的键顺序，避免以后往字典里加分类时页面次序被动改变。
CATEGORY_ORDER = ("notice", "homework", "online")

# 各分类判定「紧急」的小时阈值
URGENT_HOURS = {
    "notice": 24,    # 班级通知：1 天以内为紧急
    "homework": 24,  # 作业提醒：1 天以内为紧急
    "online": 72,    # 网课提醒：3 天以内为紧急
}

# 级别的中文显示名。pinned 由管理页手动设定，优先级高于按时间算出的三种级别。
LEVEL_NAME = {
    "pinned": "置顶",
    "urgent": "紧急",
    "normal": "普通",
    "done": "已完成",
}

# 表结构：两种方言共用字段定义，差异只在自增主键写法
# 注：level / sort_weight 为 v1.1.0 遗留列，已废弃不再写入，仅为兼容旧库保留
#     rollcall 为 v1.6.0 新增：1 = 该事项开启接龙（点卡片进入接龙页面）
#     roll_note_* 为 v1.8.0 新增：接龙报名时「附加信息」的名称 / 类别 / 是否必填 / 候选项
#     （v1.9.0 起类别只剩 text / select 两种，check 在迁移里回退成 text）
_COLUMNS = """
    id          {pk},
    category    {str16}  NOT NULL DEFAULT 'notice',
    title       {str255} NOT NULL,
    tag         {str32},
    note        {text},
    date_label  {str32},
    time_label  {str64},
    location    {str64},
    link        {str255},
    deadline    {dt},
    pinned      {int}    NOT NULL DEFAULT 0,
    level       {str16}  NOT NULL DEFAULT 'normal',
    sort_weight {int}    NOT NULL DEFAULT 0,
    rollcall    {int}    NOT NULL DEFAULT 0,
    roll_note_label    {str32},
    roll_note_mode     {str16} NOT NULL DEFAULT 'text',
    roll_note_required {int}   NOT NULL DEFAULT 0,
    roll_note_options  {text},
    created_at  {ts}
"""

# 接龙明细表（v1.6.0 新增）。一条接龙一行，序号就是「按 id 升序的名次」。
# 刻意不加外键约束：两种后端在级联删除上的默认行为不一致，
# 删事项时由 delete_notice 顺手清掉它的接龙明细，行为完全可控。
# note 列（v1.8.0 起叫「附加信息」）两种类别共用：文本原文 / 选中的那一项文字。
_ROLLCALL_COLUMNS = """
    id         {pk},
    notice_id  {ref}    NOT NULL,
    name       {str32}  NOT NULL,
    note       {str255},
    token      {str64}  NOT NULL DEFAULT '',
    joined_at  {ts}
"""


def _dialect(backend):
    """两种方言的列类型对照表，notices 与 rollcalls 共用"""
    if backend == "mysql":
        return {
            "pk": "INT AUTO_INCREMENT PRIMARY KEY",
            "str16": "VARCHAR(16)",
            "str32": "VARCHAR(32)",
            "str64": "VARCHAR(64)",
            "str255": "VARCHAR(255)",
            "text": "TEXT",
            # int 是"开关"用的窄整型（TINYINT 只到 127），关联主键要用 ref（INT），
            # 否则事项超过 127 条之后接龙就写不进库了
            "int": "TINYINT",
            "ref": "INT",
            "dt": "DATETIME NULL",
            "ts": "TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP",
        }
    return {
        "pk": "INTEGER PRIMARY KEY AUTOINCREMENT",
        "str16": "TEXT",
        "str32": "TEXT",
        "str64": "TEXT",
        "str255": "TEXT",
        "text": "TEXT",
        "int": "INTEGER",
        "ref": "INTEGER",
        "dt": "TEXT",
        # 与 MySQL 的 DEFAULT CURRENT_TIMESTAMP 对齐，否则 SQLite 下 created_at 恒为 NULL，
        # 两端表现不一致（MySQL 有时间、SQLite 没有）
        "ts": "TEXT DEFAULT (datetime('now','localtime'))",
    }


def _tail(backend):
    return ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4" if backend == "mysql" else ")"


def _schema(backend):
    """按后端方言生成建表语句"""
    return ("CREATE TABLE IF NOT EXISTS notices ("
            + _COLUMNS.format(**_dialect(backend)) + _tail(backend))


def _rollcall_schema(backend):
    """接龙明细建表语句。MySQL 的索引写在建表语句里，SQLite 走 _rollcall_index"""
    sql = ("CREATE TABLE IF NOT EXISTS rollcalls ("
           + _ROLLCALL_COLUMNS.format(**_dialect(backend))).rstrip()
    if backend == "mysql":
        return sql + ",\n    KEY idx_notice (notice_id)\n) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
    return sql + "\n)"


def _rollcall_index(backend):
    """SQLite 下补一个 notice_id 索引（MySQL 已写在建表语句里）；重复执行无副作用"""
    if backend == "mysql":
        return None
    return "CREATE INDEX IF NOT EXISTS idx_rollcalls_notice ON rollcalls (notice_id)"


# ---------------------------------------------------------------- 连接
def _connect_mysql():
    """尝试建立 MySQL 连接，失败抛出原始异常"""
    import pymysql  # 延迟导入：本机未装驱动时不影响降级逻辑

    return pymysql.connect(
        host=MYSQL_CONF["host"],
        port=MYSQL_CONF["port"],
        user=MYSQL_CONF["user"],
        password=MYSQL_CONF["password"],
        database=MYSQL_CONF["database"],
        charset=MYSQL_CONF["charset"],
        autocommit=True,
        cursorclass=pymysql.cursors.DictCursor,
    )


def _connect_sqlite():
    conn = sqlite3.connect(SQLITE_PATH)
    conn.row_factory = sqlite3.Row
    conn.isolation_level = None  # 自动提交，行为与 MySQL autocommit 对齐
    return conn


def _safe_close(conn):
    """
    幂等关闭连接：已经关过或连接本就异常时静默返回。
    PyMySQL 的 close() 对已关闭连接会抛 Error("Already closed")，
    所有关闭动作都必须走这里，避免把"连接早已正常关闭"误报成故障。
    """
    try:
        conn.close()
    except Exception:
        pass


def get_conn():
    """按当前后端取连接。MySQL 每请求新建（线上 gunicorn 多进程下最简单可靠）。
    若尚未初始化（某些 ASGI 客户端不触发 startup 事件），这里兜底补一次。"""
    if BACKEND is None:
        init_db()
    if BACKEND == "mysql":
        return _connect_mysql()
    return _connect_sqlite()


def status():
    """确保数据库已初始化，返回 (后端, 降级原因)，供管理页展示"""
    if BACKEND is None:
        init_db()
    return BACKEND, LAST_ERROR


def _rows(cur):
    """把游标结果统一成 list[dict]，SQLite 的 Row 也需要转换"""
    data = cur.fetchall()
    return [dict(r) for r in data]


@contextmanager
def _session():
    """
    统一两种后端的用法：产出一个游标，退出时自动关闭连接。
    pymysql 游标与 sqlite 游标都支持 execute(sql, args)。
    """
    conn = get_conn()
    cur = conn.cursor()
    try:
        yield cur
    finally:
        try:
            cur.close()
        except Exception:
            pass
        _safe_close(conn)


# ---------------------------------------------------------------- 初始化
def _conf_summary():
    """
    拼一段用于排错的配置摘要，只暴露账号/库名，密码仅显示"是否已填"，不泄露明文。
    这段信息会出现在管理页提示条里，用于快速判断：
      - 账号显示成 root     -> 服务器跑的是旧代码，新 db.py 没上传上去
      - 密码显示"未填"      -> 环境变量没传进 gunicorn worker，走了默认值
      - 两者都正确仍报 1045 -> 密码真的错了，或该账号没有本库权限
    """
    return "user=%s, host=%s:%s, db=%s, password=%s" % (
        MYSQL_CONF["user"],
        MYSQL_CONF["host"],
        MYSQL_CONF["port"],
        MYSQL_CONF["database"],
        "已填" if MYSQL_CONF["password"] else "未填",
    )


def init_db():
    """
    选择后端并建表。
    先探 MySQL，连不上（或本机没装服务/驱动、db.conf 缺配置）则降级 SQLite，
    并把降级原因 + 当前生效的连接配置记录到 LAST_ERROR，供管理页提示条展示。
    """
    global BACKEND, PH, LAST_ERROR
    LAST_ERROR = None

    try:
        missing = _missing_conf_keys()
        if missing:
            raise RuntimeError(
                "db.conf 缺少配置项：%s（路径：%s）" % ("、".join(missing), _CONF_PATH)
            )

        conn = _connect_mysql()
        try:
            # 注意：不要用 `with conn`。PyMySQL 1.x 的 Connection.__exit__ 会关闭连接，
            # 再手动 conn.close() 就会抛 Error("Already closed")，
            # 而建表其实已经成功 —— 异常被下面的 except 吞掉后会误判成"连不上 MySQL"。
            cur = conn.cursor()
            cur.execute(_schema("mysql"))
            cur.execute(_rollcall_schema("mysql"))
            cur.close()
        finally:
            _safe_close(conn)

        BACKEND, PH = "mysql", "%s"

    except Exception as exc:  # 未装驱动 / 未装服务 / 网络不通 / 凭据错误 / 缺配置
        # 带上生效的配置摘要，避免"不知道到底用了哪个账号连"这种盲区
        LAST_ERROR = "%s: %s  [%s]" % (type(exc).__name__, exc, _conf_summary())
        conn = _connect_sqlite()
        try:
            conn.execute(_schema("sqlite"))
            conn.execute(_rollcall_schema("sqlite"))
            conn.execute(_rollcall_index("sqlite"))
        finally:
            _safe_close(conn)
        BACKEND, PH = "sqlite", "?"

    _migrate()
    return BACKEND


def _has_column(name):
    """判断 notices 表是否已有某列，用于旧库升级"""
    with _session() as cur:
        if BACKEND == "mysql":
            cur.execute("SHOW COLUMNS FROM notices LIKE " + PH, [name])
            return cur.fetchone() is not None
        cur.execute("PRAGMA table_info(notices)")
        return any(r["name"] == name for r in _rows(cur))


def _add_column(name, ddl):
    """缺列时补上，已有则跳过"""
    if _has_column(name):
        return
    with _session() as cur:
        cur.execute(ddl)


def _migrate():
    """
    旧库升级：按顺序补齐后续版本新增的列，并把废弃的取值改写掉。
      - deadline：v1.1.0 的表没有，v1.2.0 起用于级别计算
      - pinned　：v1.3.0 起用于置顶
      - rollcall：v1.6.0 起用于接龙（1 = 开启接龙）
      - roll_note_*：v1.8.0 起用于接龙「附加信息」的名称 / 类别 / 必填 / 候选项
      - roll_note_mode 的 'check'（打勾叉）：v1.9.0 起废弃，回退成 text
    新库建表时已含这些列（且不会有 check 行），此函数基本是空操作。
    """
    if BACKEND == "mysql":
        _add_column("deadline", "ALTER TABLE notices ADD COLUMN deadline DATETIME NULL")
        _add_column("pinned", "ALTER TABLE notices ADD COLUMN pinned TINYINT NOT NULL DEFAULT 0")
        _add_column("rollcall", "ALTER TABLE notices ADD COLUMN rollcall TINYINT NOT NULL DEFAULT 0")
        _add_column("roll_note_label", "ALTER TABLE notices ADD COLUMN roll_note_label VARCHAR(32)")
        _add_column("roll_note_mode",
                    "ALTER TABLE notices ADD COLUMN roll_note_mode VARCHAR(16) NOT NULL DEFAULT 'text'")
        _add_column("roll_note_required",
                    "ALTER TABLE notices ADD COLUMN roll_note_required TINYINT NOT NULL DEFAULT 0")
        _add_column("roll_note_options", "ALTER TABLE notices ADD COLUMN roll_note_options TEXT")
    else:
        _add_column("deadline", "ALTER TABLE notices ADD COLUMN deadline TEXT")
        # SQLite 不允许给已有行的表加带默认值的 NOT NULL 列，先加可空列再回填 0
        _add_column("pinned", "ALTER TABLE notices ADD COLUMN pinned INTEGER")
        _add_column("rollcall", "ALTER TABLE notices ADD COLUMN rollcall INTEGER")
        _add_column("roll_note_label", "ALTER TABLE notices ADD COLUMN roll_note_label TEXT")
        _add_column("roll_note_mode", "ALTER TABLE notices ADD COLUMN roll_note_mode TEXT")
        _add_column("roll_note_required", "ALTER TABLE notices ADD COLUMN roll_note_required INTEGER")
        _add_column("roll_note_options", "ALTER TABLE notices ADD COLUMN roll_note_options TEXT")
        with _session() as cur:
            cur.execute("UPDATE notices SET pinned = 0 WHERE pinned IS NULL")
            cur.execute("UPDATE notices SET rollcall = 0 WHERE rollcall IS NULL")
            # 旧行的类别一律按「输入文本」处理，与 v1.7.0 之前那个自由填写的备注行为一致
            cur.execute("UPDATE notices SET roll_note_mode = 'text' WHERE roll_note_mode IS NULL")
            cur.execute("UPDATE notices SET roll_note_required = 0 WHERE roll_note_required IS NULL")

    # v1.9.0：'check'（打勾叉）类别已删除。两种后端都要改写，所以放在分支外面 ——
    # 配了 check 的事项改回 text 后，报名页就是一个普通文本框，
    # 而名单里已经存下的 √ / × 是历史数据，原样保留、照常展示与复制。
    with _session() as cur:
        cur.execute("UPDATE notices SET roll_note_mode = 'text' WHERE roll_note_mode = 'check'")


# ---------------------------------------------------------------- 时间处理
def _to_int(value, default=0):
    """
    把各后端可能返回的数值统一成 int。
    MySQL 的 TINYINT 列 pymysql 通常给 int，但 DECIMAL 列会给 Decimal；
    SQLite 给 int；列缺失时是 None。这里统一兜住，避免比较/序列化出问题。
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return int(value)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def parse_deadline(value):
    """把各种来源的截止时间统一成 datetime；空值/无法解析返回 None"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    if not text:
        return None
    text = text.replace("T", " ")  # datetime-local 控件提交的是 "YYYY-MM-DDTHH:MM"
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def compute_level(category, deadline, now=None):
    """
    按截止时间实时计算级别：urgent / normal / done。
    这是「级别 + 权重合并」的核心，level 不入库，每次查询现算。
    """
    if deadline is None:
        return "normal"          # 未填截止时间，无法判断，按普通处理
    now = now or datetime.now()
    if deadline <= now:
        return "done"            # 时间已过 → 已完成
    hours = (deadline - now).total_seconds() / 3600.0
    threshold = URGENT_HOURS.get(category, 24)
    return "urgent" if hours <= threshold else "normal"


# 剩余时间小于该分钟数时，倒计时精确到秒（首页客户端刷新用同一阈值）
SECOND_PRECISION_MINUTES = 10


def _duration_text(delta):
    """
    把 timedelta 渲染成「X天Y小时」/「X小时Y分」/「Y分钟」；
    不足 SECOND_PRECISION_MINUTES 分钟时精确到秒，如「9分58秒」「45秒」。
    """
    total_sec = int(delta.total_seconds())
    if total_sec < 0:
        total_sec = 0
    days, rem = divmod(total_sec, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)
    if days:
        return "%d天%d小时" % (days, hours)
    if hours:
        return "%d小时%d分" % (hours, minutes)
    if minutes >= SECOND_PRECISION_MINUTES:
        return "%d分钟" % minutes
    if minutes:
        return "%d分%d秒" % (minutes, seconds)
    return "%d秒" % seconds


def _countdown(deadline, level, now):
    """
    剩余/超时文案，首页卡片与管理列表都会用到。

    这里用「截止时间是否已过」而不是 level 来判断方向：
    置顶事项过期后 level 是 pinned 而非 done，若按 level 判断会走到"剩"分支，
    负值又被 _duration_text 夹成 0，最终显示成误导人的「剩 0分钟」。
    """
    if deadline is None:
        return ""
    if deadline <= now:
        return "已过 " + _duration_text(now - deadline)
    return "剩 " + _duration_text(deadline - now)


def countdown_text(row, now=None):
    """
    对整行直接算剩余/超时文案，**不看 joined**（接龙页专用）。

    与 _enrich 里的 row["countdown"] 的区别：
      首页卡片是"给我看的"—— 已接龙就说明这件事对我办完了，不再倒数；
      接龙页是"给还没报名的同学看的"—— 本人接完了，剩余时间照样得显示，
      后来的人一进页面才知道还剩多久，所以这里单独算一遍。
    没填截止时间就返回空串，页面据此不渲染那颗胶囊。
    """
    deadline = parse_deadline(row.get("deadline"))
    if deadline is None:
        return ""
    return _countdown(deadline, row.get("level"), now or datetime.now())


def rollcall_open(row, now=None):
    """
    这件事项现在还收不收新的报名（v1.9.0）。

    判据只有一个：事项填了截止时间、且已经过去 → 关门。
    没填截止时间的永远开着（没期限就是随到随报，不能拿"没设时间"当已截止）。
    已过期之后不是整个接龙页失效 —— 名单照样能看、能复制、能撤销自己那条，
    只是不再接受新的报名（见 add_rollcall 与 rollcall.html 的表单分支）。
    """
    deadline = parse_deadline(row.get("deadline"))
    if deadline is None:
        return True
    return deadline > (now or datetime.now())


def rollcall_closed_text(row, now=None):
    """
    接龙关门时的中文提示（接龙页、报名接口、首页卡片共用同一句），
    把截止时间原样带出来，用户才知道是"到点了"而不是"页面坏了"。
    """
    deadline = parse_deadline(row.get("deadline"))
    if deadline is None:
        return "接龙已截止，不再接受新的报名。"
    return "接龙已于 %s 截止，不再接受新的报名。" % deadline.strftime("%m-%d %H:%M")


def _jsonable(value):
    """
    把数据库返回的非 JSON 原生类型转成字符串。

    这是 admin 列表在 MySQL 下加载失败、SQLite 下却正常的根因：
      - MySQL 的 DATETIME/TIMESTAMP 列，pymysql 返回的是 Python datetime 对象；
      - SQLite 里同样的列是 TEXT，返回的就是字符串。
    而 /api/notices 用 JSONResponse（底层 json.dumps）序列化，
    datetime 不可序列化 → 抛 TypeError → 接口 500 → 列表区一片空白。
    首页是 Jinja 服务端渲染，直接 str() 输出，所以只有 admin 受影响。
    """
    if isinstance(value, (datetime, date)):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, timedelta):
        return str(value)
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", "replace")
    return value


def json_default(value):
    """
    给 json.dumps 用的 default 回调：任何遗漏的非 JSON 类型都兜底转字符串。
    main.py 序列化接口响应时传入，避免以后加字段再踩同一个坑。
    """
    converted = _jsonable(value)
    if converted is value:
        return str(value)
    return converted


def json_dumps(payload):
    """统一的 JSON 序列化入口，带上面的兜底"""
    return json.dumps(payload, ensure_ascii=False, default=json_default)


def _enrich(row, now, ctx=None):
    """
    给一行原始记录补上派生字段（不改动库里的值）：
      pinned     —— 是否置顶（库里的 0/1 转成布尔）
      rollcall   —— 是否开启接龙（前端据此把卡片变成可点）
      rollcount  —— 已接龙人数（列表上显示「接龙 N 人」）
      joined     —— 当前访客是否已接龙（v1.6.0）
      roll_open  —— 现在还收不收新的报名（v1.9.0：截止时间已过就不再收）
      level      —— 实时计算的级别；置顶时为 pinned；已接龙时为 done
      countdown  —— 剩余/超时文案；已接龙的不再倒数
      date_label / time_label —— 留空时按截止时间自动生成
    并把所有日期时间列统一成字符串，保证接口能安全 JSON 序列化。

    ctx 是 roll_context() 的返回值（已接龙 id 集合 + 各事项人数），
    由调用方查一次传进来，避免每行都去查一遍接龙表（N+1）。
    """
    joined_ids, roll_counts = ctx or ((), {})
    nid = _to_int(row.get("id"))

    deadline = parse_deadline(row.get("deadline"))
    category = row.get("category") or "notice"
    # pinned 在不同后端可能是 int / Decimal / None，统一成布尔
    pinned = bool(_to_int(row.get("pinned")))
    rollcall = bool(_to_int(row.get("rollcall")))
    is_joined = bool(rollcall and nid in joined_ids)

    # 置顶优先于一切：不再按截止时间降级成"已完成"，也不会被折叠
    if pinned:
        level = "pinned"
    elif is_joined:
        # 接龙 = 这件事对该同学已经办完，直接按「已完成」处理
        level = "done"
    else:
        level = compute_level(category, deadline, now)

    row["deadline"] = deadline.strftime("%Y-%m-%d %H:%M") if deadline else ""
    row["pinned"] = pinned
    row["rollcall"] = rollcall
    row["rollcount"] = roll_counts.get(nid, 0)
    row["joined"] = is_joined
    # v1.9.0：接龙是否还收新的报名。只有开着接龙的事项才谈得上"关门"，
    # 没开接龙的一律是 False，模板与前端都不用再判一次 rollcall。
    row["roll_open"] = bool(rollcall and rollcall_open(row, now))
    row["level"] = level
    row["level_name"] = LEVEL_NAME.get(level, "普通")
    row["category_name"] = CATEGORIES.get(category, category)
    # 已接龙的不再显示倒计时：事情办完了，倒数只会干扰阅读
    row["countdown"] = "" if is_joined else _countdown(deadline, level, now)

    # 接龙「附加信息」的配置（v1.8.0）：把库里的 0/1 与两行文本转成
    # 模板/JSON 直接好用的形状（布尔 + 可选列表），非法值一律退回默认。
    note_mode = (row.get("roll_note_mode") or "").strip()
    if note_mode not in ROLL_NOTE_MODES:
        note_mode = "text"
    row["roll_note_label"] = (row.get("roll_note_label") or "").strip() or ROLL_NOTE_LABEL_DEFAULT
    row["roll_note_mode"] = note_mode
    row["roll_note_mode_name"] = ROLL_NOTE_MODE_NAME[note_mode]
    row["roll_note_required"] = bool(_to_int(row.get("roll_note_required")))
    row["roll_note_options"] = _clean_options(row.get("roll_note_options"))

    # created_at 等日期列在 MySQL 下是 datetime 对象，必须转字符串，否则接口 500
    row["created_at"] = _jsonable(row.get("created_at"))

    if deadline:
        if not (row.get("date_label") or "").strip():
            row["date_label"] = deadline.strftime("%m-%d")
        if not (row.get("time_label") or "").strip():
            row["time_label"] = deadline.strftime("%H:%M") + " 截止"

    # 折叠标记：已完成且超过阈值天数的事项默认折叠。
    # 置顶的事项永不折叠 —— 它必须始终可见，否则会从两个列表里同时消失。
    row["folded"] = (not pinned and bool(deadline) and level == "done"
                     and deadline <= _done_cutoff(now))

    # 排序用的原始时间，不外泄给模板/JSON
    row["_ts"] = deadline.timestamp() if deadline else None
    return row


def _sort_key(row):
    """
    排序：置顶最优先（多个置顶按 id 倒序，后置顶的在前）
          → 未过期按截止时间升序（越近越靠前）
          → 未填截止时间的排其后
          → 已完成的沉底（最近过期的在前）
    """
    level, ts, nid = row["level"], row["_ts"], row["id"]
    if level == "pinned":
        return (-1, 0, -nid)
    if level == "done":
        return (2, -(ts or 0), -nid)
    if ts is None:
        return (1, 0, -nid)
    return (0, ts, -nid)


# ---------------------------------------------------------------- CRUD
def list_notices(category=None, ctx=None):
    """查询通知列表，附加实时级别并按截止时间排序"""
    sql = "SELECT * FROM notices"
    args = []
    if category:
        sql += " WHERE category = " + PH
        args.append(category)

    with _session() as cur:
        cur.execute(sql, args)
        rows = _rows(cur)

    now = datetime.now()
    rows = [_enrich(r, now, ctx) for r in rows]
    rows.sort(key=_sort_key)
    for r in rows:
        r.pop("_ts", None)
    return rows


def get_notice(notice_id, ctx=None):
    with _session() as cur:
        cur.execute("SELECT * FROM notices WHERE id = " + PH, [notice_id])
        rows = _rows(cur)
    if not rows:
        return None
    row = _enrich(rows[0], datetime.now(), ctx)
    row.pop("_ts", None)
    return row


def _notice_fields(payload):
    """
    新增与修改共用同一套字段校验与清洗，避免两处规则各自演化。
    校验不通过抛 ValueError，由接口层转成 400 + 提示语。
    """
    category = payload.get("category") or "notice"
    if category not in CATEGORIES:
        raise ValueError("未知的分类")

    fields = {
        "category": category,
        "title": (payload.get("title") or "").strip(),
        "tag": (payload.get("tag") or "").strip(),
        "note": (payload.get("note") or "").strip(),
        "date_label": (payload.get("date_label") or "").strip(),
        "time_label": (payload.get("time_label") or "").strip(),
        "location": (payload.get("location") or "").strip(),
        "link": (payload.get("link") or "").strip(),
    }
    if not fields["title"]:
        raise ValueError("标题不能为空")

    # 截止时间可选；填了就必须能解析，避免静默存成空值让人误以为已设置
    raw_deadline = payload.get("deadline")
    if raw_deadline is not None and str(raw_deadline).strip():
        deadline = parse_deadline(raw_deadline)
        if deadline is None:
            raise ValueError("截止时间格式无法识别，请用 YYYY-MM-DD HH:MM")
        fields["deadline"] = deadline.strftime("%Y-%m-%d %H:%M:%S")
    else:
        fields["deadline"] = None

    # 两个开关都默认为 False。前端复选框未勾选时不提交该字段，
    # 勾选时可能提交成 "on" / "1" / "true" 等多种写法，这里统一判定。
    fields["pinned"] = 1 if _truthy(payload.get("pinned")) else 0
    fields["rollcall"] = 1 if _truthy(payload.get("rollcall")) else 0
    # 接龙「附加信息」的四项配置（v1.8.0）。即使这次没开启接龙也照存不误：
    # 管理页可以先配好附加信息、再勾开接龙，两个设置互不干扰。
    fields.update(_note_config_fields(payload))
    return fields


def add_notice(payload):
    """新增一条班级事项，返回新记录 id"""
    fields = _notice_fields(payload)
    category = fields["category"]

    cols = ", ".join(fields.keys())
    marks = ", ".join([PH] * len(fields))
    sql = "INSERT INTO notices (" + cols + ") VALUES (" + marks + ")"

    with _session() as cur:
        # 同一连接内先清掉该分类原有的置顶，再插入新行，保证「每板块最多一条置顶」
        if fields["pinned"]:
            cur.execute(
                "UPDATE notices SET pinned = 0 WHERE category = " + PH, [category]
            )
        cur.execute(sql, list(fields.values()))
        return cur.lastrowid


def update_notice(notice_id, payload):
    """
    修改一条班级事项（v1.6.0）。字段校验与 add_notice 完全共用，返回是否命中记录。

    这里的 UPDATE 是「整行覆盖」：管理页表单每次都会把所有字段提交上来，
    没填的就是空值，所以不存在"漏传字段被旧值保留"的歧义。
    """
    fields = _notice_fields(payload)

    with _session() as cur:
        # 先确认记录存在：MySQL 下 UPDATE 命中但值没变化时 rowcount 是 0，
        # 直接用 rowcount 判断会把"改了个一模一样的内容"误报成"记录不存在"。
        cur.execute("SELECT id FROM notices WHERE id = " + PH, [notice_id])
        if cur.fetchone() is None:
            return False

        # 改完仍是置顶时，同样要保证该分类只有这一条置顶
        if fields["pinned"]:
            cur.execute(
                "UPDATE notices SET pinned = 0 WHERE category = " + PH
                + " AND id <> " + PH + " AND " + _IS_PINNED,
                [fields["category"], notice_id],
            )
        sets = ", ".join(k + " = " + PH for k in fields)
        cur.execute("UPDATE notices SET " + sets + " WHERE id = " + PH,
                    list(fields.values()) + [notice_id])
    return True


def set_pinned(notice_id, pinned):
    """
    设置/取消某条事项的置顶状态，返回 (是否命中记录, 该分类中文名, 被顶替掉的条数)。

    「每个板块只允许有一个置顶事项」：置为 True 时，先把同分类的其他置顶清零，
    再置本条为 1。两条语句走同一个连接，避免中途失败留下两条置顶。

    取消置顶只能在管理页进行，没有任何自动取消逻辑：
    置顶事项即使截止时间已过，也不会被折叠、不会降级成「已完成」。
    """
    row = get_notice(notice_id)
    if not row:
        return False, None, 0

    category = row.get("category") or "notice"
    flag = 1 if pinned else 0
    displaced = 0
    with _session() as cur:
        if flag:
            cur.execute(
                "UPDATE notices SET pinned = 0 WHERE category = " + PH
                + " AND id <> " + PH + " AND " + _IS_PINNED,
                [category, notice_id],
            )
            displaced = cur.rowcount or 0
        cur.execute("UPDATE notices SET pinned = " + PH + " WHERE id = " + PH,
                    [flag, notice_id])
    return True, CATEGORIES.get(category, category), displaced


def _truthy(value):
    """
    把前端各种"真"的写法统一成布尔。
    复选框勾选时可能提交 "on" / "true" / "1"，也可能是布尔 True；
    取消勾选时表单常常干脆不提交该字段（None）。
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in ("1", "true", "on", "yes", "y")


def delete_notice(notice_id):
    """删除一条班级事项（连同它的接龙明细），返回是否真的删掉了"""
    with _session() as cur:
        cur.execute("DELETE FROM notices WHERE id = " + PH, [notice_id])
        hit = cur.rowcount > 0
        if hit:
            # 没有外键约束，明细必须自己清，否则 rollcalls 里会攒下孤儿行
            cur.execute("DELETE FROM rollcalls WHERE notice_id = " + PH, [notice_id])
        return hit


# ---------------------------------------------------------------- 接龙
# 有的事项需要"接龙"报名（团建、领书、聚餐……）。设计取舍：
#   · notices.rollcall = 1 表示开启接龙，首页那条卡片变成可点，进入 /rollcall/{id}；
#   · 明细存在从表 rollcalls，一条一行，序号 = 按 id 升序的名次（不存库，撤销后自动连号）；
#   · 没有账号体系，用浏览器 cookie 里的随机 token 认"这台设备"：
#     接龙后首页那条显示「已接龙」并按已完成处理；换设备/清 cookie 就认不出来了，
#     但这不影响名单本身（名单是所有人的），只影响"我接龙了吗"这个标记。
#
# 报名限制有两条（见 add_rollcall）：
#   1. 「同一个姓名在同一事项里只能报一次」；
#   2. v1.9.0 起「事项的截止时间过了就不再收新的报名」（见 rollcall_open）。
# **设备层面没有次数限制**：一台设备（一个 token）可以为多位同学连续代报名，
# 名单里这些行都算"我登记的"，都能撤销。所以 token 只用来认"哪些行是我报的"，
# 绝不能拿它做"这台设备已经报过了，不许再报"的拦截，也不能拿它绕过截止时间。
#
# 长度限制（v1.8.0 收紧）：姓名 4 个字、附加信息 20 个字。名单是给人名看的，
# 太长会把一行撑成两行；限死之后前端 maxlength 与后端校验用的是同一对数字。
ROLLCALL_NAME_MAX = 4
ROLLCALL_NOTE_MAX = 20
ROLLCALL_TOKEN_MAX = 64

# 接龙「附加信息」的类别（v1.8.0，v1.9.0 去掉 check）。两种类别**落库都是同一个
# rollcalls.note 字符串**：
#   text   自由输入（原来的「备注」）
#   select 从管理页预先填好的选项里挑一个，存选中的那项文字
# 类别只决定"报名时表单长什么样"，不决定存什么、怎么导出 ——
# 以后再加类别（比如日期选择）也不用动数据库。
# 原来的 check（打勾叉）只是"√ / × 二选一的选择"，用 select + 两个候选项就能表达，
# v1.9.0 删掉；旧库里配成 check 的事项由 _migrate() 回退成 text。
ROLL_NOTE_LABEL_DEFAULT = "附加信息"
ROLL_NOTE_LABEL_MAX = 16
ROLL_NOTE_MODES = ("text", "select")
ROLL_NOTE_MODE_NAME = {
    "text": "输入文本",
    "select": "进行选择",
}
ROLL_NOTE_OPTIONS_MAX = 12          # 选择类别最多几个候选项


def _clean_options(value):
    """
    把候选项统一成 list[str]，允许两种来源：
      · 管理页那个 textarea 提交的「一行一个」字符串；
      · 接口调用方直接给的 list/tuple。
    逐项去空白、丢空行、去重复，并限制条数与单项长度 ——
    前端 maxlength 只是为了少一次往返，接口必须自己就能兜住。
    """
    if isinstance(value, (list, tuple)):
        raw = list(value)
    else:
        raw = str(value or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")

    out = []
    for item in raw:
        text = str(item).strip()[:ROLLCALL_NOTE_MAX]
        if not text or text in out:
            continue
        out.append(text)
        if len(out) >= ROLL_NOTE_OPTIONS_MAX:
            break
    return out


def _note_config_fields(payload):
    """
    接龙「附加信息」四项配置的校验与清洗，新增与修改共用（由 _notice_fields 调用）。
    校验不通过抛 ValueError，接口层转成 400 + 中文提示。
    """
    mode = (payload.get("roll_note_mode") or "").strip() or "text"
    if mode not in ROLL_NOTE_MODES:
        raise ValueError("未知的附加信息类别")

    label = (payload.get("roll_note_label") or "").strip() or ROLL_NOTE_LABEL_DEFAULT
    if len(label) > ROLL_NOTE_LABEL_MAX:
        raise ValueError("附加信息名称太长了（最多 %d 个字）" % ROLL_NOTE_LABEL_MAX)

    # 只有「进行选择」才需要候选项；换成别的类别时顺手清空，
    # 免得改回选择类别时又冒出上一次那批早就删掉的旧选项
    options = _clean_options(payload.get("roll_note_options")) if mode == "select" else []
    if mode == "select" and not options:
        raise ValueError("附加信息选「进行选择」时，至少要有一个选项")

    return {
        "roll_note_label": label,
        "roll_note_mode": mode,
        # 复选框未勾选时表单不提交该字段，_truthy 统一兜住
        "roll_note_required": 1 if _truthy(payload.get("roll_note_required")) else 0,
        "roll_note_options": "\n".join(options),
    }


def roll_context(token=None):
    """
    一次取齐当前访客的接龙状态，供 _enrich 使用：
      joined —— 该 token 已接龙的事项 id 集合（没 token 就是空集）
      counts —— {事项 id: 接龙人数}

    首页要渲染三个栏目，只在这里查一次再由调用方传下去，
    避免每个栏目、每条记录各查一遍接龙表。
    """
    counts = {}
    joined = set()
    with _session() as cur:
        cur.execute("SELECT notice_id, COUNT(*) AS c FROM rollcalls GROUP BY notice_id")
        for r in _rows(cur):
            counts[_to_int(r.get("notice_id"))] = _to_int(r.get("c"))
        if token:
            cur.execute("SELECT DISTINCT notice_id FROM rollcalls WHERE token = " + PH, [token])
            joined = {_to_int(r.get("notice_id")) for r in _rows(cur)}
    return joined, counts


def list_rollcalls(notice_id, token=None):
    """
    某事项的接龙名单，按接龙先后（id 升序）返回。

    同时把 token 列剔掉再返回：它相当于"这台设备的钥匙"，
    虽然只有本人能看到自己的页面，但没有必要把它送进模板或 JSON。
    返回 (名单列表, 我那一行或 None)。
    """
    with _session() as cur:
        cur.execute("SELECT * FROM rollcalls WHERE notice_id = " + PH + " ORDER BY id ASC",
                    [notice_id])
        rows = _rows(cur)

    mine = None
    for seq, r in enumerate(rows, 1):
        r["seq"] = seq
        r["mine"] = bool(token) and r.get("token") == token
        r["joined_at"] = _jsonable(r.get("joined_at")) or ""
        # 名单里只显示「月-日 时:分」，完整时间戳太长会把一行撑爆
        r["joined_at_short"] = r["joined_at"][5:16]
        r.pop("token", None)
        # 同一台设备可能替好几个人报名（家长、室友），"我那条"取最早的一条，
        # 页面上的「你已经接龙了（第 N 位）」才对得上本人
        if r["mine"] and mine is None:
            mine = r
    return rows, mine


def add_rollcall(notice_id, name, note, token):
    """
    新增一条接龙，返回 (是否成功, 提示语)。

    校验在数据层再做一遍：前端校验只是为了少一次往返，接口必须自己拦得住。
    只拦「已截止 / 同名重复 / 姓名与附加信息不合规」，
    **不拦同一个 token 报第二次** —— 一台设备替多位同学代报名是正常用法，
    老师/班委一次报十几个人也合情合理。

    v1.9.0：事项的截止时间一过就拒绝新的报名（rollcall_open）。
    接龙页会把表单收起来，但接口必须自己也拦 —— 页面可以在截止前打开、
    截止后才点提交（表单早就渲染好了），只靠前端隐藏等于没拦。
    注意这里只拦"新的报名"，撤销（cancel_rollcall）不受影响。
    """
    name = (name or "").strip()
    note = (note or "").strip()
    if not name:
        return False, "请填写姓名"
    if len(name) > ROLLCALL_NAME_MAX:
        return False, "姓名最多 %d 个字" % ROLLCALL_NAME_MAX
    if len(note) > ROLLCALL_NOTE_MAX:
        return False, "附加信息最多 %d 个字" % ROLLCALL_NOTE_MAX

    row = get_notice(notice_id)
    if row is None:
        return False, "事项不存在（可能已被删除）"
    if not row.get("rollcall"):
        return False, "该事项未开启接龙"
    # 截止时间已过 → 关门。排在姓名/附加信息校验之后，是为了让"写错字"这种
    # 随手就能改的问题先报出来，用户不必先怀疑是不是到点了。
    if not rollcall_open(row):
        return False, rollcall_closed_text(row)

    # 附加信息的收口（v1.8.0）：类别由事项配置决定，选择只收候选项里的那一个。
    # 前端已经把控件限制住了，这里防的是绕过页面直接打接口
    # （以及改配置后留在页面上的旧表单）。
    label = row.get("roll_note_label") or ROLL_NOTE_LABEL_DEFAULT
    mode = row.get("roll_note_mode") or "text"
    if mode == "select" and note and note not in (row.get("roll_note_options") or []):
        return False, "%s只能从页面给出的选项里挑一个" % label
    if row.get("roll_note_required") and not note:
        return False, "请填写%s" % label

    with _session() as cur:
        # 同名只允许接一次：名字是名单上唯一的身份标识，重复了看不出谁是谁。
        # 注意这里卡的是「姓名 + 事项」，不是「设备」：
        # 同一台设备换个人名继续报多少条都行（代报名），见本函数上面的说明。
        cur.execute("SELECT id FROM rollcalls WHERE notice_id = " + PH + " AND name = " + PH,
                    [notice_id, name])
        if cur.fetchone() is not None:
            return False, "「%s」已经接龙过了，看看名单里是不是你～" % name

        cur.execute(
            "INSERT INTO rollcalls (notice_id, name, note, token) VALUES ("
            + ", ".join([PH] * 4) + ")",
            [notice_id, name, note, token or ""],
        )
    return True, "接龙成功"


def cancel_rollcall(notice_id, entry_id, token):
    """撤销接龙，返回是否真的删掉。只认自己设备的令牌，删不动别人的记录"""
    if not token:
        return False
    with _session() as cur:
        cur.execute(
            "DELETE FROM rollcalls WHERE id = " + PH
            + " AND notice_id = " + PH + " AND token = " + PH,
            [entry_id, notice_id, token],
        )
        return cur.rowcount > 0


# ---------------------------------------------------------------- 折叠相关
# 折叠阈值：已完成且超过该天数的事项默认折叠，仅展开时才查库
FOLD_DONE_DAYS = 1


def _done_cutoff(now=None):
    """已完成事项的折叠分界线：早于此时间的算"过期已久"，默认折叠"""
    now = now or datetime.now()
    return now - timedelta(days=FOLD_DONE_DAYS)


# 折叠/未折叠的判定条件抽成共用片段，四个查询都引用它。
# 这样两份 SQL 不可能各自演化出分歧 —— 一旦出现偏差，事项会同时出现在两个列表，
# 或者更糟：两个列表都查不到，直接从页面上消失。
#
#   已折叠 = 截止时间已过阈值 且 未置顶
#   未折叠 = 截止时间未过阈值，或没有截止时间，或已置顶（置顶永不折叠）
# 用 COALESCE 兜住 pinned 为 NULL 的旧行；用 > 0 而非 = 1 判断，避免异常值两边都不命中。
_NOT_PINNED = "COALESCE(pinned, 0) = 0"
_IS_PINNED = "COALESCE(pinned, 0) > 0"


# 注意：这两个条件片段里的占位符必须用函数在「运行时」拼接，
# 不能在模块加载时写成常量。因为 PH 在 init_db() 之后才由 '?' 变成 '%s'，
# 若在 import 时就把 PH 固化进字符串，MySQL 后端下会带着 '?' 去执行，
# 占位符数量对不上直接报错（表现为 SQLite 正常、MySQL 挂）。
def _folded_cond():
    return "deadline IS NOT NULL AND deadline <= " + PH + " AND " + _NOT_PINNED


def _active_cond():
    # 括号必须有：SQL 里 AND 优先级高于 OR，
    # 不括起来会被解析成 "deadline IS NULL OR deadline > ? OR (pinned>0 AND category = ?)"，
    # 导致分类过滤对未填截止时间的事项失效（一条通知同时出现在三个栏目）。
    return "(deadline IS NULL OR deadline > " + PH + " OR " + _IS_PINNED + ")"


def _folded_sql(select, category):
    """拼折叠区 SQL，返回 (sql, args)"""
    sql = "SELECT " + select + " FROM notices WHERE " + _folded_cond()
    args = [_done_cutoff().strftime("%Y-%m-%d %H:%M:%S")]
    if category:
        sql += " AND category = " + PH
        args.append(category)
    return sql, args


def _active_sql(select, category):
    """拼未折叠区 SQL，返回 (sql, args)"""
    sql = "SELECT " + select + " FROM notices WHERE " + _active_cond()
    args = [_done_cutoff().strftime("%Y-%m-%d %H:%M:%S")]
    if category:
        sql += " AND category = " + PH
        args.append(category)
    return sql, args


def count_folded(category=None, now=None):
    """统计某分类下"已折叠"的事项条数，只查 COUNT，不取明细"""
    sql, args = _folded_sql("COUNT(*) AS c", category)
    with _session() as cur:
        cur.execute(sql, args)
        row = cur.fetchone()
        return row["c"] if isinstance(row, dict) else row[0]


def list_folded(category=None, now=None, ctx=None):
    """
    取"已折叠"的事项明细（已完成且超过 FOLD_DONE_DAYS 天，且未置顶）。
    仅在用户点开折叠区时调用，平时不查，减轻服务器负担。
    """
    sql, args = _folded_sql("*", category)
    with _session() as cur:
        cur.execute(sql, args)
        rows = _rows(cur)

    rows = [_enrich(r, now or datetime.now(), ctx) for r in rows]
    rows.sort(key=_sort_key)
    for r in rows:
        r.pop("_ts", None)
    return rows


def list_active(category=None, now=None, ctx=None):
    """
    取"未折叠"的事项：进行中 + 刚完成（未超过 FOLD_DONE_DAYS 天）+ 未填截止时间 + 全部置顶项。
    首页默认只渲染这部分，折叠区的内容不在此列。
    """
    sql, args = _active_sql("*", category)
    with _session() as cur:
        cur.execute(sql, args)
        rows = _rows(cur)

    rows = [_enrich(r, now or datetime.now(), ctx) for r in rows]
    rows.sort(key=_sort_key)
    for r in rows:
        r.pop("_ts", None)
    return rows


def count_active(category=None, now=None):
    """未折叠事项的条数，管理页分栏计数用"""
    sql, args = _active_sql("COUNT(*) AS c", category)
    with _session() as cur:
        cur.execute(sql, args)
        row = cur.fetchone()
        return row["c"] if isinstance(row, dict) else row[0]


def group_counts(now=None):
    """
    管理页顶部分栏计数：每个分类的「进行中 / 已折叠」条数。
    只查 COUNT，不拉明细。按 CATEGORY_ORDER 输出，页签次序由此决定。
    """
    result = {}
    for key in CATEGORY_ORDER:
        result[key] = {
            "name": CATEGORIES[key],
            "active": count_active(key, now),
            "folded": count_folded(key, now),
        }
    return result
