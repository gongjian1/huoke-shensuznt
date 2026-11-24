"""
获客管理系统 - 网页后台
运行: python app.py   然后打开 http://localhost:5000
"""
import csv
import hashlib
import json
import io
import os
import random
import re
import math
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from functools import wraps

from flask import (Flask, Response, abort, flash, g, jsonify, redirect, render_template, request,
                   send_from_directory, session, url_for)
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

import ai
import api
import billing
import dm
import tracking
import vault
from collector import platform_health, rescore_all, start_task
from database import get_db, get_setting, init_db, set_setting, word_list
from icons import icon, logo
from platforms import LABELS, PLATFORMS
from regions import GROUPS, OVERSEAS, PROVINCES

app = Flask(__name__)


def wan(n):
    if n is None:
        return "—"
    n = int(n)
    if abs(n) >= 100_000_000:
        return f"{n / 100_000_000:.1f}".rstrip("0").rstrip(".") + "亿"
    if abs(n) >= 10_000:
        return f"{n / 10_000:.1f}".rstrip("0").rstrip(".") + "万"
    return f"{n:,}"


def ago(ts):
    if not ts:
        return "—"
    try:
        t = datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return ts
    s = (datetime.now() - t).total_seconds()
    if s < 0:
        m = int(-s // 60)
        return f"{m} 分钟后" if m < 60 else f"{m // 60} 小时后"
    if s < 3600:
        return f"{max(1, int(s // 60))} 分钟前"
    if s < 86400:
        return f"{int(s // 3600)} 小时前"
    if s < 86400 * 30:
        return f"{int(s // 86400)} 天前"
    return ts[:10]


def imgp(url):
    return f"/img?u={urllib.parse.quote(url, safe='')}" if url else ""


def qs(**override):
    """当前页面的查询参数，覆盖指定项（值为空则去掉），用于翻页 / 切换排序时保留其他筛选。"""
    args = {k: v for k, v in request.args.items() if v != ""}
    for k, v in override.items():
        if v in (None, ""):
            args.pop(k, None)
        else:
            args[k] = v
    return "?" + urllib.parse.urlencode(args) if args else "?"


app.jinja_env.filters["datefmt"] = lambda ts: time.strftime("%Y-%m-%d", time.localtime(ts)) if ts else "—"
app.jinja_env.globals.update(icon=icon, logo=logo, pts=billing.fmt,
                             yuan=lambda cents: f"{(cents or 0) / 100:,.2f}".rstrip("0").rstrip("."), wan=wan, ago=ago, imgp=imgp, qs=qs,
                             REGION_GROUPS=GROUPS, PROVINCES=PROVINCES, OVERSEAS=OVERSEAS)
init_db()
if os.environ.get("HUOKE_SCHEDULER") == "1":
    tracking.start_scheduler()
    ai.resume_videos()

if not get_setting("secret_key"):
    set_setting("secret_key", secrets.token_hex(32))
app.secret_key = get_setting("secret_key")
app.config.update(
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("HUOKE_HTTPS") == "1",
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
)
if os.environ.get("HUOKE_BEHIND_PROXY") == "1":
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

STATUSES = ["未联系", "已私信", "已加微信", "跟进中", "已转化", "无效"]
EDITABLE_LEAD_FIELDS = {"status", "wechat", "phone", "notes", "tier", "dm_public", "dm_private"}
USER_STATUSES = ["正常", "待审核", "禁用"]
POINT_KINDS = {"充值": 1, "赠送": 1, "退款": 1, "扣减": -1}
PHONE_RE = re.compile(r"^1[3-9]\d{9}$")
PUBLIC_ENDPOINTS = {"login", "register", "static"}

_failed_logins = {}  # ip -> (count, first_failure_ts)
_register_log = {}  # ip -> [timestamps]
REGISTER_PER_HOUR = 5
LOCK_AFTER, LOCK_SECONDS = 5, 600


# ---------------- auth ----------------

def _load_user(uid):
    conn = get_db()
    row = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    conn.close()
    return row


@app.before_request
def guard():
    g.user = _load_user(session["uid"]) if session.get("uid") else None
    if g.user and g.user["status"] != "正常":
        session.clear()
        g.user = None
    if request.endpoint in PUBLIC_ENDPOINTS or g.user:
        return None
    if request.path.startswith("/api/") or request.path.endswith(".csv"):
        return jsonify(ok=False, message="请先登录"), 401
    return redirect(url_for("login"))


@app.after_request
def security_headers(resp):
    """基础安全响应头：防被嵌进别人的网页（点击劫持）、防浏览器猜文件类型、控制 Referer、HTTPS 下启用 HSTS。"""
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    if os.environ.get("HUOKE_HTTPS") == "1":
        resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
    return resp


def admin_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not g.user["is_admin"]:
            abort(403)
        return fn(*a, **kw)
    return wrapper


@app.errorhandler(403)
def forbidden(_e):
    if request.path.startswith("/api/"):
        return jsonify(ok=False, message="没有权限"), 403
    return render_template("error.html", code=403), 403


@app.errorhandler(404)
def not_found(_e):
    if request.path.startswith(("/api/", "/img")) or not g.get("user"):
        return (jsonify(ok=False, message="不存在"), 404) if request.path.startswith("/api/") else ("", 404)
    return render_template("error.html", code=404), 404


def _too_many_failures(ip):
    count, first = _failed_logins.get(ip, (0, time.time()))
    return count >= LOCK_AFTER and time.time() - first < LOCK_SECONDS


def _record_failure(ip):
    count, first = _failed_logins.get(ip, (0, time.time()))
    if time.time() - first >= LOCK_SECONDS:
        count, first = 0, time.time()
    _failed_logins[ip] = (count + 1, first)


@app.route("/login", methods=["GET", "POST"])
def login():
    error, account = "", request.form.get("account", "").strip()
    if request.method == "POST":
        ip = request.remote_addr or "?"
        conn = get_db()
        user = conn.execute("SELECT * FROM users WHERE phone=?", (account,)).fetchone()
        if _too_many_failures(ip):
            error = "密码错误次数太多，请 10 分钟后再试"
        elif not user or not user["password_hash"] or \
                not check_password_hash(user["password_hash"], request.form.get("password", "")):
            _record_failure(ip)
            error = "账号或密码不对"
        elif user["status"] == "待审核":
            error = "账号正在审核中，请联系管理员开通"
        elif user["status"] == "禁用":
            error = "账号已停用，请联系管理员"
        else:
            _failed_logins.pop(ip, None)
            conn.execute("UPDATE users SET last_login=datetime('now','localtime') WHERE id=?", (user["id"],))
            conn.commit()
            session.clear()
            session.permanent = True
            session["uid"] = user["id"]
            conn.close()
            return redirect(url_for("dashboard"))
        conn.close()
    return render_template("login.html", error=error, account=account,
                           register_mode=get_setting("register_mode"))


@app.route("/register", methods=["GET", "POST"])
def register():
    mode = get_setting("register_mode")
    form = {k: request.form.get(k, "").strip() for k in ("phone", "nickname")}
    error, done = "", False
    if request.method == "POST" and mode != "closed":
        pw, pw2 = request.form.get("password", ""), request.form.get("password2", "")
        ip = request.remote_addr or "?"
        recent = [t for t in _register_log.get(ip, []) if time.time() - t < 3600]
        _register_log[ip] = recent
        if len(recent) >= REGISTER_PER_HOUR:
            error = "注册太频繁了，请一小时后再试"
        elif not PHONE_RE.match(form["phone"]):
            error = "请输入正确的 11 位手机号"
        elif len(pw) < 6:
            error = "密码至少 6 位"
        elif pw != pw2:
            error = "两次输入的密码不一致"
        else:
            conn = get_db()
            if conn.execute("SELECT 1 FROM users WHERE phone=?", (form["phone"],)).fetchone():
                error = "这个手机号已经注册过了，直接登录即可"
            else:
                status = "正常" if mode == "open" else "待审核"
                uid = conn.execute(
                    "INSERT INTO users (phone, nickname, password_hash, status) VALUES (?, ?, ?, ?)",
                    (form["phone"], form["nickname"][:20] or form["phone"][-4:], generate_password_hash(pw), status),
                ).lastrowid
                bonus = billing.to_units(get_setting("register_bonus") or 0)
                if bonus > 0:
                    billing.adjust(conn, uid, bonus, "赠送", "注册赠送")
                conn.commit()
                _register_log[ip].append(time.time())
                if status == "正常":
                    session.clear()
                    session.permanent = True
                    session["uid"] = uid
                    conn.close()
                    return redirect(url_for("dashboard"))
                done = True
            conn.close()
    return render_template("register.html", mode=mode, form=form, error=error, done=done)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.context_processor
def inject_globals():
    css_v = int(os.path.getmtime(os.path.join(app.static_folder, "style.css")))
    ctx = {"LABELS": LABELS, "PLATFORMS": PLATFORMS, "STATUSES": STATUSES, "css_v": css_v,
           "me": g.get("user"), "projects": [], "pending_recharges": 0}
    if g.get("user"):
        conn = get_db()
        if g.user["is_admin"]:
            ctx["pending_recharges"] = conn.execute("SELECT COUNT(*) FROM recharge_orders WHERE status='待确认'").fetchone()[0]
        ctx["projects"] = [r[0] for r in conn.execute(
            "SELECT project FROM videos WHERE owner_id=? AND project IS NOT NULL AND project != '' "
            "UNION SELECT project FROM tasks WHERE owner_id=? AND project IS NOT NULL AND project != '' "
            "ORDER BY 1", (g.user["id"], g.user["id"]))]
        conn.close()
    return ctx


def _prices():
    return {"normal": billing.unit_cost("douyin"), "wechat": billing.unit_cost("wechat_channels"),
            "track": tracking.track_price(), "per_yuan": billing.comments_per_yuan()}


def _filters():
    keys = ("project", "platform", "tier", "status", "keyword", "q", "province", "days", "video", "sort", "page")
    return {k: request.args.get(k, "").strip() for k in keys}


def _where(f, alias="l"):
    sql, params = [f"{alias}.owner_id = ?"], [g.user["id"]]
    for col in ("project", "platform", "tier", "status", "keyword"):
        if f.get(col):
            sql.append(f"{alias}.{col} = ?")
            params.append(f[col])
    if f.get("province"):
        if f["province"] == "未知":
            sql.append(f"({alias}.province IS NULL OR {alias}.province = '')")
        else:
            sql.append(f"{alias}.province = ?")
            params.append(f["province"])
    if f.get("days", "").isdigit() and int(f["days"]) > 0:
        sql.append(f"{alias}.commented_at >= datetime('now','localtime', ?)")
        params.append(f"-{int(f['days'])} days")
    if f.get("video"):
        sql.append(f"{alias}.video_id = ?")
        params.append(f["video"])
    if f.get("q"):
        sql.append(f"({alias}.nickname LIKE ? OR {alias}.text LIKE ? OR {alias}.user_handle LIKE ? "
                   f"OR {alias}.wechat LIKE ? OR {alias}.notes LIKE ?)")
        params += [f"%{f['q']}%"] * 5
    return " WHERE " + " AND ".join(sql), params


def _page(f, per_page):
    return max(1, int(f["page"])) if f.get("page", "").isdigit() else 1


LEAD_SORTS = {"tier": "CASE l.tier WHEN 'A' THEN 0 WHEN 'B' THEN 1 ELSE 2 END, l.created_at DESC",
              "comment": "l.commented_at IS NULL, l.commented_at DESC, l.created_at DESC",
              "new": "l.created_at DESC"}
VIDEO_SORTS = {"recent": "v.last_scraped DESC, v.collected_at DESC",
               "leads": "(lead_a * 3 + lead_b) DESC, v.last_scraped DESC",
               "comments": "v.comment_count IS NULL, v.comment_count DESC",
               "new": "v.published_at IS NULL, v.published_at DESC"}
LEADS_PER_PAGE, VIDEOS_PER_PAGE = 100, 48
AGE_CHOICES = [(30, "近 1 个月"), (90, "近 3 个月"), (180, "近 6 个月"), (365, "近 1 年"), (0, "不限")]
DEFAULT_AGE_DAYS = 180


# ---------------- pages ----------------

@app.route("/")
def dashboard():
    project = request.args.get("project", "")
    uid = g.user["id"]
    extra = " AND project = ?" if project else ""
    p = [uid] + ([project] if project else [])
    conn = get_db()
    one = lambda sql, args=p: conn.execute(sql, args).fetchone()[0]
    stats = {
        "a": one(f"SELECT COUNT(*) FROM leads WHERE owner_id=?{extra} AND tier='A'"),
        "b": one(f"SELECT COUNT(*) FROM leads WHERE owner_id=?{extra} AND tier='B'"),
        "a_new": one(f"SELECT COUNT(*) FROM leads WHERE owner_id=?{extra} AND tier='A' AND status='未联系'"),
        "contacted": one(f"SELECT COUNT(*) FROM leads WHERE owner_id=?{extra} AND status NOT IN ('未联系','无效')"),
        "converted": one(f"SELECT COUNT(*) FROM leads WHERE owner_id=?{extra} AND status='已转化'"),
        "comments": one(f"SELECT COUNT(*) FROM comments c JOIN videos v ON v.id=c.video_id "
                        f"WHERE c.owner_id=?{extra.replace('project', 'v.project')}"),
        "videos": one(f"SELECT COUNT(*) FROM videos WHERE owner_id=?{extra}"),
    }
    by_platform = conn.execute(f"""
        SELECT v.platform, COUNT(DISTINCT v.id) AS videos, COUNT(c.id) AS comments,
               (SELECT COUNT(*) FROM leads l WHERE l.owner_id = ? AND l.platform = v.platform
                    {'AND l.project = ?' if project else ''}) AS leads
        FROM videos v LEFT JOIN comments c ON c.video_id = v.id
        WHERE v.owner_id = ?{extra.replace('project', 'v.project')}
        GROUP BY v.platform ORDER BY leads DESC
    """, p + p).fetchall()
    by_region = conn.execute(f"""
        SELECT province, COUNT(*) AS n FROM leads WHERE owner_id=?{extra} AND province IS NOT NULL AND province != ''
        GROUP BY province ORDER BY n DESC LIMIT 8
    """, p).fetchall()
    region_total = one(f"SELECT COUNT(*) FROM leads WHERE owner_id=?{extra} AND province IS NOT NULL AND province != ''")
    recent = conn.execute(f"""
        SELECT l.*, v.title AS video_title, v.url AS video_url, v.cover AS video_cover FROM leads l
        LEFT JOIN videos v ON v.id = l.video_id
        WHERE l.owner_id = ?{extra.replace('project', 'l.project')}
        ORDER BY CASE l.tier WHEN 'A' THEN 0 ELSE 1 END, l.created_at DESC LIMIT 15
    """, p).fetchall()
    tasks = conn.execute("SELECT * FROM tasks WHERE owner_id=? ORDER BY id DESC LIMIT 5", (uid,)).fetchall()
    conn.close()
    return render_template("dashboard.html", stats=stats, by_platform=by_platform, by_region=by_region,
                           region_total=region_total, recent=recent, tasks=tasks, project=project)


@app.route("/collect")
def collect_page():
    conn = get_db()
    recent_kw = []
    for r in conn.execute("SELECT keywords FROM tasks WHERE owner_id=? AND mode='search' ORDER BY id DESC LIMIT 20",
                          (g.user["id"],)):
        for w in word_list(r[0]):
            if w not in recent_kw:
                recent_kw.append(w)
    conn.close()
    return render_template("collect.html", recent_kw=recent_kw[:20], prices=_prices(), health=platform_health(),
                           AGE_CHOICES=AGE_CHOICES, DEFAULT_AGE_DAYS=DEFAULT_AGE_DAYS)


@app.route("/leads")
def leads_page():
    f = _filters()
    where, params = _where(f)
    sort = f["sort"] if f["sort"] in LEAD_SORTS else "tier"
    page = _page(f, LEADS_PER_PAGE)
    conn = get_db()
    total = conn.execute(f"SELECT COUNT(*) FROM leads l {where}", params).fetchone()[0]
    counts = conn.execute(f"SELECT l.tier, COUNT(*) FROM leads l {where} GROUP BY l.tier", params).fetchall()
    leads = conn.execute(f"""
        SELECT l.*, v.title AS video_title, v.url AS video_url, v.author AS video_author, v.cover AS video_cover
        FROM leads l LEFT JOIN videos v ON v.id = l.video_id {where}
        ORDER BY {LEAD_SORTS[sort]} LIMIT ? OFFSET ?
    """, [*params, LEADS_PER_PAGE, (page - 1) * LEADS_PER_PAGE]).fetchall()
    keywords = [r[0] for r in conn.execute(
        "SELECT DISTINCT keyword FROM leads WHERE owner_id=? AND keyword IS NOT NULL AND keyword != '' ORDER BY 1",
        (g.user["id"],))]
    provinces = conn.execute(
        "SELECT CASE WHEN province IS NULL OR province = '' THEN '未知' ELSE province END AS p, COUNT(*) AS n "
        "FROM leads WHERE owner_id=? GROUP BY p ORDER BY n DESC", (g.user["id"],)).fetchall()
    video = conn.execute("SELECT title FROM videos WHERE id=? AND owner_id=?", (f["video"], g.user["id"])).fetchone() if f["video"] else None
    conn.close()
    tier_counts = {r[0]: r[1] for r in counts}
    return render_template("leads.html", leads=leads, f=f, keywords=keywords, provinces=provinces, total=total,
                           page=page, pages=max(1, math.ceil(total / LEADS_PER_PAGE)), sort=sort,
                           tier_counts=tier_counts, video_title=video["title"] if video else "",
                           per_page=LEADS_PER_PAGE)


@app.route("/leads.csv")
def leads_csv():
    where, params = _where(_filters())
    conn = get_db()
    rows = conn.execute(f"""
        SELECT l.tier, l.platform, l.project, l.keyword, l.nickname, l.user_handle, l.user_id, l.user_link,
               l.province, l.ip_region, l.commented_at, l.text, l.keywords, l.status, l.wechat, l.phone, l.notes,
               v.title, v.url, l.created_at, l.dm_public, l.dm_private
        FROM leads l LEFT JOIN videos v ON v.id = l.video_id {where}
        ORDER BY l.tier, l.created_at DESC
    """, params).fetchall()
    conn.close()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["等级", "平台", "项目", "搜索词", "昵称", "账号", "用户ID", "主页", "地区", "IP属地原文", "评论时间", "评论",
                "命中词", "状态", "微信", "电话", "备注", "来源作品", "作品链接", "采集时间", "公开回复话术", "私信话术"])
    for r in rows:
        r = list(r)
        r[1] = LABELS.get(r[1], r[1])
        w.writerow(r)
    return Response("\ufeff" + buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=leads.csv"})


@app.route("/videos")
def videos_page():
    f = _filters()
    sort = f["sort"] if f["sort"] in VIDEO_SORTS else "recent"
    page = _page(f, VIDEOS_PER_PAGE)
    sql, params = ["v.owner_id = ?"], [g.user["id"]]
    for col in ("project", "platform"):
        if f[col]:
            sql.append(f"v.{col} = ?")
            params.append(f[col])
    where = " AND ".join(sql)
    conn = get_db()
    total = conn.execute(f"SELECT COUNT(*) FROM videos v WHERE {where}", params).fetchone()[0]
    videos = conn.execute(f"""
        SELECT v.*,
               (SELECT COUNT(*) FROM comments c WHERE c.video_id = v.id) AS scraped,
               (SELECT COUNT(*) FROM leads l WHERE l.video_id = v.id AND l.tier = 'A') AS lead_a,
               (SELECT COUNT(*) FROM leads l WHERE l.video_id = v.id AND l.tier = 'B') AS lead_b
        FROM videos v WHERE {where}
        ORDER BY {VIDEO_SORTS[sort]} LIMIT ? OFFSET ?
    """, [*params, VIDEOS_PER_PAGE, (page - 1) * VIDEOS_PER_PAGE]).fetchall()
    conn.close()
    return render_template("videos.html", videos=videos, f=f, sort=sort, total=total, page=page,
                           pages=max(1, math.ceil(total / VIDEOS_PER_PAGE)))


# ---------------- AI 仿写 ----------------

MAX_LIVE_CREATIONS = 2
MEDIA_NAME = re.compile(r"^(\d+)-\d+-[0-9a-f]{8}\.(png|jpg|webp)$")
VIDEO_NAME = re.compile(r"^v(\d+)-[0-9a-f]{8}\.mp4$")


def _creation_source(conn, d):
    """把请求里的参考内容整理成统一的结构；返回 (source, 错误信息)。"""
    uid = g.user["id"]
    if d.get("video"):
        v = conn.execute("SELECT * FROM videos WHERE id=? AND owner_id=?", (d["video"], uid)).fetchone()
        if not v:
            return None, "找不到这个作品"
        return dict(kind="video", ref=v["id"], platform=LABELS.get(v["platform"], v["platform"]), author=v["author"] or "",
                    title=v["title"] or "", cover=v["cover"] or "", url=v["url"] or ""), ""
    if d.get("apost"):
        p = conn.execute("SELECT p.*, a.nickname FROM account_posts p JOIN accounts a ON a.id = p.account_ref "
                         "WHERE p.id=? AND p.owner_id=?", (d["apost"], uid)).fetchone()
        if not p:
            return None, "找不到这个作品"
        return dict(kind="apost", ref=p["id"], platform=LABELS.get(p["platform"], p["platform"]), author=p["nickname"] or "",
                    title=p["title"] or "", cover=p["cover"] or "", url=p["url"] or ""), ""
    text = (d.get("source_text") or "").strip()
    if len(text) < 4:
        return None, "请先粘贴要仿写的文案，或者从作品库里选一个作品"
    return dict(kind="text", ref="", platform="", author="", title=text[:1500], cover="", url=""), ""


@app.route("/create")
def create_page():
    conn = get_db()
    source, err = (None, "")
    if request.args.get("video") or request.args.get("apost"):
        source, err = _creation_source(conn, request.args)
    conn.close()
    return render_template("create.html", source=source, err=err, prices=_prices(), STYLES=ai.STYLES,
                           ai_ready=bool(ai.user_cred(g.user)), ai_info=_ai_info(), RATIOS=ai.RATIO_LABEL, MAX_IMAGES=ai.MAX_IMAGES,
                           args={"video": request.args.get("video", ""), "apost": request.args.get("apost", "")})


def _validate_creation(d):
    style = d.get("style") if d.get("style") in ai.STYLES else "xhs"
    ratio = d.get("ratio") if d.get("ratio") in ai.SIZES else "3:4"
    return style, ratio, _int(d.get("image_count"), 3, 0, ai.MAX_IMAGES)


def _new_creation(conn, source, style, ratio, n, product, extra):
    """检查 Key 和并发后新建一条创作并开始生成；返回 (id, 错误信息, 状态码)。费用由客户自己的阿里云账号结算。"""
    if not ai.user_cred(g.user):
        return None, "请先在「我的账户」里填写你自己的阿里云 API Key", 400
    live = conn.execute("SELECT COUNT(*) FROM creations WHERE owner_id=? AND status='生成中'", (g.user["id"],)).fetchone()[0]
    if live >= MAX_LIVE_CREATIONS:
        return None, f"同时最多生成 {MAX_LIVE_CREATIONS} 个，请等前面的完成", 429
    cid = conn.execute(
        """INSERT INTO creations (owner_id, source_kind, source_ref, source_platform, source_author, source_title, source_cover,
                                  source_url, style, product_info, extra, image_count, ratio)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (g.user["id"], source["kind"], source["ref"], source["platform"], source["author"], source["title"], source["cover"],
         source["url"], style, product, extra, n, ratio)).lastrowid
    conn.commit()
    if (product or "").strip():  # 业务介绍记下来，私信助手和下次创作自动带出
        conn.execute("UPDATE users SET product_info=? WHERE id=?", (product.strip()[:dm.MAX_PRODUCT], g.user["id"]))
        conn.commit()
    ai.start_creation(cid)
    return cid, "", 200


@app.post("/api/creations")
def create_creation():
    d = request.get_json(force=True) or {}
    conn = get_db()
    source, err = _creation_source(conn, d)
    if err:
        conn.close()
        return jsonify(ok=False, message=err), 400
    style, ratio, n = _validate_creation(d)
    cid, err, code = _new_creation(conn, source, style, ratio, n, (d.get("product_info") or "").strip()[:600],
                                   (d.get("extra") or "").strip()[:300])
    conn.close()
    if err:
        return jsonify(ok=False, message=err), code
    return jsonify(ok=True, id=cid)


def _own_creation(conn, cid):
    row = conn.execute("SELECT * FROM creations WHERE id=? AND owner_id=?", (cid, g.user["id"])).fetchone()
    if not row:
        abort(404)
    return row


@app.route("/creations")
def creations_page():
    page = _page({"page": request.args.get("page", "")}, 24)
    conn = get_db()
    total = conn.execute("SELECT COUNT(*) FROM creations WHERE owner_id=?", (g.user["id"],)).fetchone()[0]
    rows = conn.execute("SELECT * FROM creations WHERE owner_id=? ORDER BY id DESC LIMIT 24 OFFSET ?",
                        (g.user["id"], (page - 1) * 24)).fetchall()
    conn.close()
    return render_template("creations.html", rows=rows, total=total, page=page, pages=max(1, math.ceil(total / 24)),
                           STYLES=ai.STYLES, ai_ready=bool(ai.user_cred(g.user)), loads=json.loads)


@app.route("/creations/<int:cid>")
def creation_detail(cid):
    conn = get_db()
    c = _own_creation(conn, cid)
    conn.close()
    return render_template("creation_detail.html", c=c, STYLES=ai.STYLES, RATIOS=ai.RATIO_LABEL,
                           tags=json.loads(c["tags"] or "[]"), images=json.loads(c["images"] or "[]"),
                           prompts=json.loads(c["image_prompts"] or "[]"), prices=_prices())


@app.get("/api/creations/<int:cid>")
def creation_status(cid):
    conn = get_db()
    c = _own_creation(conn, cid)
    conn.close()
    return jsonify(status=c["status"], progress=c["progress"], error=c["error"], images=len(json.loads(c["images"] or "[]")),
                   has_text=bool(c["title"] or c["body"]))


@app.post("/api/creations/<int:cid>")
def edit_creation(cid):
    d = request.get_json(force=True) or {}
    conn = get_db()
    _own_creation(conn, cid)
    tags = [str(t).strip().lstrip("#") for t in (d.get("tags") or []) if str(t).strip()][:12]
    conn.execute("UPDATE creations SET title=?, body=?, tags=? WHERE id=?",
                 ((d.get("title") or "").strip()[:80], (d.get("body") or "").strip()[:5000],
                  json.dumps(tags, ensure_ascii=False), cid))
    conn.commit()
    conn.close()
    return jsonify(ok=True)


@app.post("/api/creations/<int:cid>/redo")
def redo_creation(cid):
    conn = get_db()
    c = _own_creation(conn, cid)
    source = dict(kind=c["source_kind"], ref=c["source_ref"], platform=c["source_platform"], author=c["source_author"],
                  title=c["source_title"], cover=c["source_cover"], url=c["source_url"])
    new_id, err, code = _new_creation(conn, source, c["style"], c["ratio"], c["image_count"], c["product_info"], c["extra"])
    conn.close()
    if err:
        return jsonify(ok=False, message=err), code
    return jsonify(ok=True, id=new_id)


@app.post("/api/creations/<int:cid>/delete")
def delete_creation(cid):
    conn = get_db()
    c = _own_creation(conn, cid)
    if c["status"] == "生成中":
        conn.close()
        return jsonify(ok=False, message="还在生成中，等它完成再删除"), 400
    for name in json.loads(c["images"] or "[]"):
        if MEDIA_NAME.match(name):
            try:
                os.remove(os.path.join(ai.MEDIA_DIR, name))
            except OSError:
                pass
    conn.execute("DELETE FROM creations WHERE id=?", (cid,))
    conn.commit()
    conn.close()
    return jsonify(ok=True)


@app.get("/media/<name>")
def media_file(name):
    m, v = MEDIA_NAME.match(name), VIDEO_NAME.match(name)
    if not (m or v):
        abort(404)
    conn = get_db()
    if m:
        row = conn.execute("SELECT owner_id FROM creations WHERE id=?", (int(m.group(1)),)).fetchone()
    else:
        row = conn.execute("SELECT owner_id FROM ai_videos WHERE id=? AND video_file=?", (int(v.group(1)), name)).fetchone()
    conn.close()
    if not row or row["owner_id"] != g.user["id"]:
        abort(404)
    resp = send_from_directory(ai.MEDIA_DIR, name, max_age=86400, conditional=True)
    resp.headers["Cache-Control"] = "private, max-age=86400"
    return resp


# ---------------- AI 视频 ----------------

MAX_LIVE_VIDEOS = 2


def _video_form_args(creation_id=None):
    """新建视频页用的创作信息：标题、配图（可当首帧）和每张图对应的画面描述。"""
    if not creation_id:
        return None
    conn = get_db()
    c = conn.execute("SELECT * FROM creations WHERE id=? AND owner_id=?", (creation_id, g.user["id"])).fetchone()
    conn.close()
    if not c:
        return None
    return {"id": c["id"], "title": c["title"] or "", "images": json.loads(c["images"] or "[]"),
            "prompts": json.loads(c["image_prompts"] or "[]"), "ratio": c["ratio"]}


@app.route("/ai-videos")
def ai_videos_page():
    page = _page({"page": request.args.get("page", "")}, 24)
    conn = get_db()
    total = conn.execute("SELECT COUNT(*) FROM ai_videos WHERE owner_id=?", (g.user["id"],)).fetchone()[0]
    rows = conn.execute("SELECT * FROM ai_videos WHERE owner_id=? ORDER BY id DESC LIMIT 24 OFFSET ?",
                        (g.user["id"], (page - 1) * 24)).fetchall()
    conn.close()
    return render_template("ai_videos.html", rows=rows, total=total, page=page, pages=max(1, math.ceil(total / 24)),
                           ai_ready=bool(ai.user_cred(g.user)))


@app.route("/ai-videos/new")
def ai_video_new():
    cid = request.args.get("creation", type=int)
    creation = _video_form_args(cid)
    ref = ""  # 从作品库 / 对标账号点「做视频」进来：带入参考作品的标题当描述草稿
    if not creation and (request.args.get("video") or request.args.get("apost")):
        conn = get_db()
        src, _err = _creation_source(conn, request.args)
        conn.close()
        if src:
            ref = re.sub(r"\s+", " ", re.sub(r"[#＃]\S+", "", src["title"])).strip()[:60]
    return render_template("ai_video_new.html", creation=creation, ref=ref, ai_ready=bool(ai.user_cred(g.user)), ai_info=_ai_info(),
                           RESOLUTIONS=ai.VIDEO_RESOLUTIONS, RATIOS=ai.VIDEO_RATIOS, DURATIONS=ai.VIDEO_DURATIONS,
                           PRICE=ai.VIDEO_CNY_PER_SEC, first_index=request.args.get("image", type=int))


def _new_video(conn, prompt, resolution, ratio, duration, creation_id, first_frame):
    """校验并新建一条视频任务；返回 (id, 错误信息, 状态码)。"""
    if not ai.user_cred(g.user):
        return None, "请先在「我的账户」里填写你自己的阿里云 API Key", 400
    if len(prompt) < 4:
        return None, "请描述一下想要的视频画面（至少几个字）", 400
    uid = g.user["id"]
    if conn.execute("SELECT COUNT(*) FROM ai_videos WHERE owner_id=? AND status='生成中'", (uid,)).fetchone()[0] >= MAX_LIVE_VIDEOS:
        return None, f"同时最多生成 {MAX_LIVE_VIDEOS} 条视频，请等前面的完成", 429
    if conn.execute("SELECT COUNT(*) FROM ai_videos WHERE owner_id=?", (uid,)).fetchone()[0] >= ai.MAX_STORED_VIDEOS:
        return None, f"最多保存 {ai.MAX_STORED_VIDEOS} 条视频，请先删除一些旧的", 400
    vid = conn.execute(
        """INSERT INTO ai_videos (owner_id, creation_id, mode, prompt, first_frame, resolution, ratio, duration, model)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (uid, creation_id, "i2v" if first_frame else "t2v", prompt, first_frame, resolution, ratio, duration,
         f"{ai.video_family()}-{'i2v' if first_frame else 't2v'}")).lastrowid
    conn.commit()
    ai.start_video(vid)
    return vid, "", 200


def _clean_video_params(d):
    resolution = d.get("resolution") if d.get("resolution") in ai.VIDEO_RESOLUTIONS else "720P"
    ratio = d.get("ratio") if d.get("ratio") in ai.VIDEO_RATIOS else "9:16"
    duration = _int(d.get("duration"), 5, 2, 15)
    return resolution, ratio, duration


@app.post("/api/ai-videos")
def create_ai_video():
    d = request.get_json(force=True) or {}
    resolution, ratio, duration = _clean_video_params(d)
    creation_id = _int(d.get("creation"), 0, 0, 10**9) or None
    first_frame = ""
    if d.get("image") not in (None, "", -1) and creation_id:
        info = _video_form_args(creation_id)
        idx = _int(d.get("image"), -1, -1, 50)
        if not info or not (0 <= idx < len(info["images"])):
            return jsonify(ok=False, message="找不到选中的配图"), 400
        first_frame = info["images"][idx]
    conn = get_db()
    vid, err, code = _new_video(conn, (d.get("prompt") or "").strip()[:1500], resolution, ratio, duration, creation_id, first_frame)
    conn.close()
    if err:
        return jsonify(ok=False, message=err), code
    return jsonify(ok=True, id=vid)


def _own_video(conn, vid):
    row = conn.execute("SELECT * FROM ai_videos WHERE id=? AND owner_id=?", (vid, g.user["id"])).fetchone()
    if not row:
        abort(404)
    return row


@app.route("/ai-videos/<int:vid>")
def ai_video_detail(vid):
    conn = get_db()
    v = _own_video(conn, vid)
    conn.close()
    return render_template("ai_video_detail.html", v=v, RATIOS=ai.VIDEO_RATIOS,
                           est=ai.estimate_video_cny(v["duration"], v["resolution"]))


@app.get("/api/ai-videos/<int:vid>")
def ai_video_status(vid):
    conn = get_db()
    v = _own_video(conn, vid)
    conn.close()
    return jsonify(status=v["status"], progress=v["progress"], error=v["error"])


@app.post("/api/ai-videos/<int:vid>/redo")
def redo_ai_video(vid):
    conn = get_db()
    v = _own_video(conn, vid)
    new_id, err, code = _new_video(conn, v["prompt"], v["resolution"], v["ratio"], v["duration"], v["creation_id"], v["first_frame"])
    conn.close()
    if err:
        return jsonify(ok=False, message=err), code
    return jsonify(ok=True, id=new_id)


@app.post("/api/ai-videos/<int:vid>/delete")
def delete_ai_video(vid):
    conn = get_db()
    v = _own_video(conn, vid)
    if v["status"] == "生成中":
        conn.close()
        return jsonify(ok=False, message="还在生成中，等它完成再删除"), 400
    if v["video_file"] and VIDEO_NAME.match(v["video_file"]):
        try:
            os.remove(os.path.join(ai.MEDIA_DIR, v["video_file"]))
        except OSError:
            pass
    conn.execute("DELETE FROM ai_videos WHERE id=?", (vid,))
    conn.commit()
    conn.close()
    return jsonify(ok=True)


def _ai_info():
    """当前用户的 AI 设置（用于「我的账户」和仿写页），Key 只给出遮挡后的样子。"""
    key = vault.decrypt(g.user["ai_api_key"] or "")
    return {"has_key": bool(key), "masked": vault.mask(key) if key else "", "region": g.user["ai_region"] or "cn",
            "text_model": g.user["ai_text_model"] or "", "image_model": g.user["ai_image_model"] or "",
            "default_text": get_setting("ai_text_model") or "qwen-plus", "default_image": get_setting("ai_image_model") or "qwen-image-2.0",
            "regions": ai.REGION_LABEL}


_ai_key_log = {}  # user id -> [timestamps]，限制试 Key 的频率
AI_KEY_PER_HOUR = 12


@app.post("/api/ai/key")
def save_ai_key():
    d = request.get_json(force=True) or {}
    uid = g.user["id"]
    recent = [t for t in _ai_key_log.get(uid, []) if time.time() - t < 3600]
    if len(recent) >= AI_KEY_PER_HOUR:
        return jsonify(ok=False, message="操作太频繁，请一小时后再试"), 429
    conn = get_db()
    if d.get("remove"):
        conn.execute("UPDATE users SET ai_api_key='' WHERE id=?", (uid,))
        conn.commit()
        conn.close()
        return jsonify(ok=True, message="已删除你的阿里云 Key")
    region = d.get("region") if d.get("region") in ai.BASES else "cn"
    models = {}
    for field in ("text_model", "image_model"):
        v = (d.get(field) or "").strip()
        if v and not ai.MODEL_NAME.fullmatch(v):
            conn.close()
            return jsonify(ok=False, message="模型名只能包含字母、数字和 . _ -"), 400
        models[field] = v
    key = (d.get("key") or "").strip()
    if key:
        if not re.fullmatch(r"sk-[A-Za-z0-9._\-]{16,200}", key):
            conn.close()
            return jsonify(ok=False, message="Key 的格式不对，应该是以 sk- 开头的一长串字符"), 400
        _ai_key_log[uid] = recent + [time.time()]
        probe = ai.Cred(key=key, region=region, text_model=models["text_model"] or get_setting("ai_text_model") or "qwen-plus")
        try:  # 先用这个 Key 发一次最小的请求，确认能用再保存
            ai.chat_json(probe, "你是助手，只输出 JSON。", '请输出 {"ok": true}', max_tokens=20)
        except ai.AIError as e:
            conn.close()
            return jsonify(ok=False, message=f"这个 Key 没能通过检测：{e}"), 400
        conn.execute("UPDATE users SET ai_api_key=? WHERE id=?", (vault.encrypt(key), uid))
    elif not vault.decrypt(g.user["ai_api_key"] or ""):
        conn.close()
        return jsonify(ok=False, message="请先粘贴你的阿里云 API Key"), 400
    conn.execute("UPDATE users SET ai_region=?, ai_text_model=?, ai_image_model=? WHERE id=?",
                 (region, models["text_model"], models["image_model"], uid))
    conn.commit()
    conn.close()
    return jsonify(ok=True, message="已保存，Key 通过检测，可以使用了" if key else "设置已保存")


@app.post("/api/ai/test")
def test_ai_key():
    kind = (request.get_json(force=True) or {}).get("kind", "text")
    cred = ai.user_cred(g.user)
    if not cred:
        return jsonify(ok=False, message="还没有填写阿里云 Key"), 400
    t0 = time.time()
    try:
        if kind == "image":
            ai.download_image(ai.generate_image(cred, "一只白色的家用充电桩挂在干净的灰色墙面上，柔和光线", "1024*1024"))
            msg = f"配图模型 {cred.image_model} 正常，生成并下载成功（{time.time() - t0:.1f} 秒；这次测试会在你的阿里云账号上产生一张图的费用）"
        else:
            out = ai.chat_json(cred, "你是助手，只输出 JSON。", '请输出 {"ok": true, "reply": "一句话打个招呼"}', max_tokens=60)
            msg = f"文字模型 {cred.text_model} 正常（{time.time() - t0:.1f} 秒）：{str(out.get('reply') or out)[:40]}"
        return jsonify(ok=True, message=msg)
    except ai.AIError as e:
        return jsonify(ok=False, message=str(e)), 400


@app.route("/account", methods=["GET", "POST"])
def account_page():
    uid = g.user["id"]
    if request.method == "POST":
        conn = get_db()
        action = request.form.get("action")
        if action == "profile":
            conn.execute("UPDATE users SET nickname=? WHERE id=?",
                         (request.form.get("nickname", "").strip()[:20] or g.user["phone"][-4:], uid))
            flash("资料已保存", "ok")
        elif action == "password":
            old, pw, pw2 = (request.form.get(k, "") for k in ("old", "password", "password2"))
            if not check_password_hash(g.user["password_hash"], old):
                flash("当前密码不对，没有修改", "err")
            elif len(pw) < 6 or pw != pw2:
                flash("新密码至少 6 位且两次一致，没有修改", "err")
            else:
                conn.execute("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(pw), uid))
                flash("密码已修改", "ok")
        conn.commit()
        conn.close()
        return redirect(url_for("account_page"))
    conn = get_db()
    ledger = conn.execute("SELECT * FROM points_log WHERE user_id=? ORDER BY id DESC LIMIT 100", (uid,)).fetchall()
    usage = conn.execute("SELECT COUNT(*) AS tasks, COALESCE(SUM(points_cost),0) AS spent FROM tasks WHERE owner_id=?",
                         (uid,)).fetchone()
    conn.close()
    return render_template("account.html", ledger=ledger, usage=usage, prices=_prices(), ai_info=_ai_info())


MAX_INTENT_WORDS, MAX_WORD_LEN = 300, 30


def _clean_words(text):
    words = [w for w in word_list(text) if len(w) <= MAX_WORD_LEN]
    return words[:MAX_INTENT_WORDS]


@app.route("/intent", methods=["GET", "POST"])
def intent_page():
    """每个客户自己的意向词。没设置过就用系统默认（管理员在「系统设置」里维护的那份）。"""
    if g.user["is_admin"]:
        return redirect(url_for("admin_settings"))     # 管理员改的就是系统默认
    uid = g.user["id"]
    if request.method == "POST":
        conn = get_db()
        if request.form.get("action") == "reset":
            conn.execute("UPDATE users SET intent_strong=NULL, intent_medium=NULL, intent_exclude=NULL WHERE id=?", (uid,))
            flash("已恢复成系统默认意向词", "ok")
        else:
            lists = {k: _clean_words(request.form.get(k, "")) for k in ("intent_strong", "intent_medium", "intent_exclude")}
            if not lists["intent_strong"] and not lists["intent_medium"]:
                flash("A 级和 B 级至少要有一个词，不然什么评论都不会被筛成线索，没有保存", "err")
            else:
                conn.execute("UPDATE users SET intent_strong=?, intent_medium=?, intent_exclude=? WHERE id=?",
                             ("\n".join(lists["intent_strong"]), "\n".join(lists["intent_medium"]),
                              "\n".join(lists["intent_exclude"]), uid))
                flash("已保存。以后的采集按你的意向词筛；已经采到的评论，到「线索」页点「重新筛选」就会按新词再筛一遍", "ok")
        conn.commit()
        conn.close()
        return redirect(url_for("intent_page"))
    conn = get_db()
    row = conn.execute("SELECT intent_strong, intent_medium, intent_exclude FROM users WHERE id=?", (uid,)).fetchone()
    conn.close()
    custom = row["intent_strong"] is not None or row["intent_medium"] is not None or row["intent_exclude"] is not None
    pick = lambda k: row[k] if row[k] is not None else get_setting(k)
    return render_template("intent.html", custom=custom, strong=pick("intent_strong"), medium=pick("intent_medium"),
                           exclude=pick("intent_exclude"), MAX_WORDS=MAX_INTENT_WORDS)


MAX_ACCOUNTS = 50
FEED_SORTS = {"latest": "p.published_at DESC", "rising": "(COALESCE(p.d_likes,0) + COALESCE(p.d_comments,0) * 3) DESC, p.published_at DESC",
              "comments": "COALESCE(p.comments, 0) DESC, COALESCE(p.likes, p.plays, 0) DESC"}


def _own_account(conn, acc_id):
    row = conn.execute("SELECT * FROM accounts WHERE id=? AND owner_id=?", (acc_id, g.user["id"])).fetchone()
    if not row:
        abort(404)
    return row


@app.route("/accounts")
def accounts_page():
    uid = g.user["id"]
    platform, acc, sort = request.args.get("platform", ""), request.args.get("account", ""), request.args.get("sort", "latest")
    conn = get_db()
    accounts = conn.execute("""
        SELECT a.*, (SELECT COUNT(*) FROM account_posts p WHERE p.account_ref = a.id AND p.is_new = 1) AS new_posts
        FROM accounts a WHERE a.owner_id=? ORDER BY a.active DESC, a.created_at DESC
    """, (uid,)).fetchall()
    sql, params = ["p.owner_id = ?"], [uid]
    if platform:
        sql.append("p.platform = ?")
        params.append(platform)
    if acc:
        sql.append("p.account_ref = ?")
        params.append(acc)
    feed = conn.execute(f"""
        SELECT p.*, a.nickname, a.avatar FROM account_posts p JOIN accounts a ON a.id = p.account_ref
        WHERE {' AND '.join(sql)} ORDER BY {FEED_SORTS.get(sort, FEED_SORTS['latest'])} LIMIT 60
    """, params).fetchall()
    week_new = conn.execute("SELECT COUNT(*) FROM account_posts WHERE owner_id=? AND published_at >= datetime('now','localtime','-7 days')",
                            (uid,)).fetchone()[0]
    conn.close()
    return render_template("accounts.html", accounts=accounts, feed=feed, f={"platform": platform, "account": acc, "sort": sort},
                           TRACKERS=tracking.TRACKERS, INTERVALS=tracking.INTERVALS, price=tracking.track_price(),
                           week_new=week_new, max_accounts=MAX_ACCOUNTS)


@app.route("/accounts/<int:acc_id>")
def account_detail(acc_id):
    conn = get_db()
    acc = _own_account(conn, acc_id)
    posts = conn.execute("SELECT * FROM account_posts WHERE account_ref=? ORDER BY pinned DESC, published_at DESC", (acc_id,)).fetchall()
    trend = {}
    for r in conn.execute("""SELECT s.post_ref, s.likes, s.comments, s.plays FROM post_snapshots s
                             JOIN account_posts p ON p.id = s.post_ref WHERE p.account_ref=? ORDER BY s.at""", (acc_id,)):
        trend.setdefault(r["post_ref"], []).append(r["likes"] if r["likes"] is not None else (r["plays"] or r["comments"] or 0))
    conn.close()
    return render_template("account_detail.html", acc=acc, posts=posts, trend=trend, TRACKERS=tracking.TRACKERS,
                           INTERVALS=tracking.INTERVALS)


@app.post("/api/accounts")
def add_account():
    d = request.get_json(force=True)
    platform, text = d.get("platform", ""), (d.get("text") or "").strip()
    interval = _int(d.get("interval"), 12, 6, 24)
    if interval not in tracking.INTERVALS:
        interval = 12
    if platform not in tracking.TRACKERS:
        return jsonify(ok=False, message="请选择平台"), 400
    if not text:
        return jsonify(ok=False, message="请粘贴主页链接或账号"), 400
    conn = get_db()
    if conn.execute("SELECT COUNT(*) FROM accounts WHERE owner_id=?", (g.user["id"],)).fetchone()[0] >= MAX_ACCOUNTS:
        conn.close()
        return jsonify(ok=False, message=f"最多追踪 {MAX_ACCOUNTS} 个账号"), 400
    price = 0 if g.user["is_admin"] else tracking.track_price()
    if price and g.user["balance"] < price:
        conn.close()
        return jsonify(ok=False, message=f"评论额度不足（当前 {billing.fmt(g.user['balance'])} 条），请到「充值」页充值"), 402
    try:
        account_id = tracking.TRACKERS[platform].resolve(text)
    except tracking.AccountError as e:
        conn.close()
        return jsonify(ok=False, message=str(e)), 400
    except api.ApiError:
        conn.close()
        return jsonify(ok=False, message="平台接口暂时不可用，稍后再试"), 502
    if conn.execute("SELECT 1 FROM accounts WHERE owner_id=? AND platform=? AND account_id=?",
                    (g.user["id"], platform, account_id)).fetchone():
        conn.close()
        return jsonify(ok=False, message="这个账号已经在追踪列表里了"), 400
    acc_id = conn.execute("INSERT INTO accounts (owner_id, platform, account_id, interval_hours, note) VALUES (?, ?, ?, ?, ?)",
                          (g.user["id"], platform, account_id, interval, (d.get("note") or "").strip()[:100])).lastrowid
    conn.commit()
    try:
        n = tracking.refresh_account(conn, acc_id, initial=True)
    except tracking.AccountError as e:
        conn.execute("DELETE FROM account_posts WHERE account_ref=?", (acc_id,))
        conn.execute("DELETE FROM accounts WHERE id=?", (acc_id,))
        conn.commit()
        conn.close()
        return jsonify(ok=False, message=str(e)), 400
    nick = conn.execute("SELECT nickname FROM accounts WHERE id=?", (acc_id,)).fetchone()[0]
    conn.close()
    return jsonify(ok=True, id=acc_id, message=f"已添加「{nick}」，拉到 {n} 个作品")


@app.post("/api/accounts/<int:acc_id>/refresh")
def refresh_account_api(acc_id):
    conn = get_db()
    _own_account(conn, acc_id)
    try:
        n = tracking.refresh_account(conn, acc_id)
    except tracking.AccountError as e:
        conn.close()
        return jsonify(ok=False, message=str(e)), 400
    conn.close()
    return jsonify(ok=True, message=f"已刷新，{n} 个作品")


@app.post("/api/accounts/<int:acc_id>")
def update_account(acc_id):
    d = request.get_json(force=True)
    conn = get_db()
    acc = _own_account(conn, acc_id)
    if "interval" in d and _int(d["interval"], 12, 6, 24) in tracking.INTERVALS:
        hours = _int(d["interval"], 12, 6, 24)
        conn.execute("UPDATE accounts SET interval_hours=?, next_check=CASE WHEN active=1 THEN ? ELSE next_check END WHERE id=?",
                     (hours, (datetime.now() + timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S"), acc_id))
    if "note" in d:
        conn.execute("UPDATE accounts SET note=? WHERE id=?", ((d["note"] or "").strip()[:100], acc_id))
    if "active" in d:
        on = 1 if d["active"] else 0
        conn.execute("UPDATE accounts SET active=?, next_check=?, last_error=CASE WHEN ?=1 THEN NULL ELSE last_error END WHERE id=?",
                     (on, datetime.now().strftime("%Y-%m-%d %H:%M:%S") if on else None, on, acc["id"]))
    conn.commit()
    conn.close()
    return jsonify(ok=True)


@app.post("/api/accounts/<int:acc_id>/delete")
def delete_account(acc_id):
    conn = get_db()
    _own_account(conn, acc_id)
    conn.execute("DELETE FROM post_snapshots WHERE post_ref IN (SELECT id FROM account_posts WHERE account_ref=?)", (acc_id,))
    conn.execute("DELETE FROM account_posts WHERE account_ref=?", (acc_id,))
    conn.execute("DELETE FROM accounts WHERE id=?", (acc_id,))
    conn.commit()
    conn.close()
    return jsonify(ok=True)


# ---------------- image proxy ----------------
# Platform CDNs block hotlinking (Weibo needs a Referer, Douyin/Kuaishou fail in browsers), so covers are
# fetched server-side. Only these CDN domains are allowed, including on every redirect hop.
IMG_HOSTS = {"douyinpic.com": "https://www.douyin.com/", "hdslb.com": "https://www.bilibili.com/",
             "sinaimg.cn": "https://weibo.com/", "xhscdn.com": "https://www.xiaohongshu.com/",
             "rednotecdn.com": "https://www.xiaohongshu.com/", "yximgs.com": "https://www.kuaishou.com/",
             "kwimgs.com": "https://www.kuaishou.com/", "kwaicdn.com": "https://www.kuaishou.com/"}
IMG_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "img_cache")
IMG_MAX_BYTES, IMG_TTL = 3_000_000, 3 * 86400
# Kuaishou's kvif and Douyin's heic are not decodable by browsers; those fall back to the placeholder.
IMG_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif", "image/avif"}


def _img_referer(url):
    p = urllib.parse.urlparse(url)
    host = (p.hostname or "").lower()
    if p.scheme not in ("http", "https"):
        return None
    return next((ref for suffix, ref in IMG_HOSTS.items() if host == suffix or host.endswith("." + suffix)), None)


class _AllowlistRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _img_referer(newurl):
            raise urllib.error.HTTPError(newurl, 403, "redirect outside image allowlist", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_img_opener = urllib.request.build_opener(_AllowlistRedirect)


def _prune_img_cache():
    cutoff = time.time() - IMG_TTL * 2
    for name in os.listdir(IMG_CACHE):
        path = os.path.join(IMG_CACHE, name)
        if os.path.getmtime(path) < cutoff:
            os.remove(path)


@app.get("/img")
def img_proxy():
    url = request.args.get("u", "")
    referer = _img_referer(url)
    if not referer:
        abort(404)
    os.makedirs(IMG_CACHE, exist_ok=True)
    key = os.path.join(IMG_CACHE, hashlib.sha1(url.encode()).hexdigest())
    headers = {"Cache-Control": "private, max-age=86400"}
    if os.path.exists(key) and time.time() - os.path.getmtime(key) < IMG_TTL:
        with open(key, "rb") as f:
            ctype = f.readline().decode().strip()
            return Response(f.read(), mimetype=ctype, headers=headers)
    req = urllib.request.Request(url, headers={"Referer": referer,
                                               "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126 Safari/537.36"})
    try:
        with _img_opener.open(req, timeout=10) as resp:
            ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip()
            data = resp.read(IMG_MAX_BYTES + 1)
    except (urllib.error.URLError, OSError, ValueError):
        abort(502)
    if ctype not in IMG_TYPES or len(data) > IMG_MAX_BYTES:
        abort(415)
    with open(key, "wb") as f:
        f.write(ctype.encode() + b"\n" + data)
    if random.random() < 0.02:
        _prune_img_cache()
    return Response(data, mimetype=ctype, headers=headers)


@app.route("/settings")
def settings_redirect():
    return redirect(url_for("admin_settings" if g.user["is_admin"] else "account_page"))


# ---------------- admin ----------------

@app.route("/admin")
@admin_required
def admin_users():
    q, status = request.args.get("q", "").strip(), request.args.get("status", "")
    sql, params = [], []
    if q:
        sql.append("(u.phone LIKE ? OR u.nickname LIKE ? OR u.note LIKE ?)")
        params += [f"%{q}%"] * 3
    if status:
        sql.append("u.status = ?")
        params.append(status)
    conn = get_db()
    users = conn.execute(f"""
        SELECT u.*,
               (SELECT COUNT(*) FROM tasks t WHERE t.owner_id = u.id) AS task_count,
               (SELECT COUNT(*) FROM leads l WHERE l.owner_id = u.id) AS lead_count
        FROM users u {'WHERE ' + ' AND '.join(sql) if sql else ''}
        ORDER BY CASE u.status WHEN '待审核' THEN 0 ELSE 1 END, u.id DESC
    """, params).fetchall()
    one = lambda s: conn.execute(s).fetchone()[0]
    stats = {
        "users": one("SELECT COUNT(*) FROM users WHERE is_admin=0"),
        "pending": one("SELECT COUNT(*) FROM users WHERE status='待审核'"),
        "balance": one("SELECT COALESCE(SUM(balance),0) FROM users WHERE is_admin=0"),
        "recharged": one("SELECT COALESCE(SUM(total_recharged),0) FROM users"),
        "spent_today": one("SELECT COALESCE(-SUM(delta),0) FROM points_log WHERE kind='扣费' "
                           "AND date(created_at)=date('now','localtime')"),
        "spent": one("SELECT COALESCE(SUM(total_spent),0) FROM users"),
    }
    conn.close()
    return render_template("admin_users.html", users=users, stats=stats, q=q, status=status,
                           USER_STATUSES=USER_STATUSES)


@app.route("/admin/users/<int:uid>", methods=["GET", "POST"])
@admin_required
def admin_user(uid):
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if not user:
        conn.close()
        abort(404)
    if request.method == "POST":
        action = request.form.get("action")
        if action == "points":
            kind = request.form.get("kind", "充值")
            try:
                amount = billing.to_units(request.form.get("amount", "0"))
            except ValueError:
                amount = 0
            if kind not in POINT_KINDS or not 0 < amount <= 100_000_000:
                flash("请输入正确的评论条数（整数）", "err")
            elif POINT_KINDS[kind] < 0 and amount > user["balance"]:
                flash(f"额度只有 {billing.fmt(user['balance'])} 条，不能扣减这么多", "err")
            else:
                bal = billing.adjust(conn, uid, amount * POINT_KINDS[kind], kind,
                                     request.form.get("note", "").strip()[:100], operator_id=g.user["id"])
                flash(f"已{kind} {billing.fmt(amount)} 条评论额度，当前剩余 {billing.fmt(bal)} 条", "ok")
        elif action == "status":
            st = request.form.get("status")
            if st not in USER_STATUSES or (uid == g.user["id"] and st != "正常"):
                flash("不能这样修改", "err")
            else:
                conn.execute("UPDATE users SET status=? WHERE id=?", (st, uid))
                flash(f"账号状态已改为：{st}", "ok")
        elif action == "profile":
            conn.execute("UPDATE users SET nickname=?, note=? WHERE id=?",
                         (request.form.get("nickname", "").strip()[:20] or user["nickname"],
                          request.form.get("note", "").strip()[:200], uid))
            flash("资料已保存", "ok")
        elif action == "password":
            pw = request.form.get("password", "")
            if len(pw) < 6:
                flash("新密码至少 6 位", "err")
            else:
                conn.execute("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(pw), uid))
                flash("密码已重置，请把新密码告诉用户", "ok")
        conn.commit()
        conn.close()
        return redirect(request.form.get("next") or url_for("admin_user", uid=uid))
    ledger = conn.execute("""
        SELECT p.*, o.nickname AS operator FROM points_log p LEFT JOIN users o ON o.id = p.operator_id
        WHERE p.user_id=? ORDER BY p.id DESC LIMIT 200
    """, (uid,)).fetchall()
    tasks = conn.execute("SELECT * FROM tasks WHERE owner_id=? ORDER BY id DESC LIMIT 30", (uid,)).fetchall()
    counts = conn.execute("""
        SELECT (SELECT COUNT(*) FROM leads WHERE owner_id=:u) AS leads,
               (SELECT COUNT(*) FROM leads WHERE owner_id=:u AND tier='A') AS leads_a,
               (SELECT COUNT(*) FROM videos WHERE owner_id=:u) AS videos,
               (SELECT COUNT(*) FROM comments WHERE owner_id=:u) AS comments
    """, {"u": uid}).fetchone()
    conn.close()
    return render_template("admin_user.html", user=user, ledger=ledger, tasks=tasks, counts=counts,
                           USER_STATUSES=USER_STATUSES, POINT_KINDS=POINT_KINDS)


@app.route("/admin/ledger")
@admin_required
def admin_ledger():
    kind = request.args.get("kind", "")
    conn = get_db()
    rows = conn.execute(f"""
        SELECT p.*, u.phone, u.nickname, o.nickname AS operator FROM points_log p
        JOIN users u ON u.id = p.user_id LEFT JOIN users o ON o.id = p.operator_id
        {'WHERE p.kind = ?' if kind else ''} ORDER BY p.id DESC LIMIT 500
    """, [kind] if kind else []).fetchall()
    conn.close()
    return render_template("admin_ledger.html", rows=rows, kind=kind, kinds=["扣费", *POINT_KINDS])


@app.route("/admin/settings", methods=["GET", "POST"])
@admin_required
def admin_settings():
    if request.method == "POST":
        for k in ("intent_strong", "intent_medium", "intent_exclude"):
            set_setting(k, "\n".join(word_list(request.form.get(k, ""))))
        key = request.form.get("api_key", "").strip()
        if key and "•" not in key:
            set_setting("api_key", key)
        base = request.form.get("api_base", "").strip().rstrip("/")
        if re.fullmatch(r"https://[A-Za-z0-9.\-]+(:\d{2,5})?(/[A-Za-z0-9._\-/]*)?", base) or \
                re.fullmatch(r"http://(localhost|127\.0\.0\.1)(:\d{2,5})?(/[A-Za-z0-9._\-/]*)?", base) or not base:
            set_setting("api_base", base)
        else:
            flash("接口地址格式不对，需要以 https:// 开头，没有修改", "err")
        errors = []
        for k in ("ai_text_model", "ai_image_model", "ai_video_model"):
            v = request.form.get(k, "").strip()
            if re.fullmatch(r"[A-Za-z0-9._\-]{1,64}", v):
                set_setting(k, v)
        for k, lo, hi in (("wechat_weight", 1, 100), ("price_track", 0, 100000),
                          ("register_bonus", 0, 10_000_000), ("comments_per_yuan", 1, 100000)):
            try:
                v = billing.to_units(request.form.get(k, ""))
                if not lo <= v <= hi:
                    raise ValueError
                set_setting(k, str(v))
            except ValueError:
                errors.append(k)
        if request.form.get("register_mode") in ("open", "approval", "closed"):
            set_setting("register_mode", request.form["register_mode"])
        flash("额度相关的设置要填整数（微信倍数至少 1），填错的那几项没有修改" if errors else "已保存", "err" if errors else "ok")
        return redirect(url_for("admin_settings"))
    key = get_setting("api_key")
    masked = (key[:6] + "•" * 10 + key[-4:]) if len(key) > 12 else ""
    return render_template("admin_settings.html", masked_key=masked, api_base=get_setting("api_base"),
                           ai_text_model=get_setting("ai_text_model"), ai_image_model=get_setting("ai_image_model"),
                           ai_video_model=get_setting("ai_video_model"),
                           strong=get_setting("intent_strong"), medium=get_setting("intent_medium"),
                           exclude=get_setting("intent_exclude"), wechat_weight=get_setting("wechat_weight"),
                           price_track=get_setting("price_track"), comments_per_yuan=get_setting("comments_per_yuan"),
                           register_mode=get_setting("register_mode"), register_bonus=get_setting("register_bonus"))


# ---------------- 接口 Key（数据接口中转网关）----------------

GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://127.0.0.1:5002").rstrip("/")
GATEWAY_TOKEN = os.environ.get("GATEWAY_ADMIN_TOKEN", "")


def _gw(method, path, body=None):
    """调用本机网关的管理接口，返回 (数据 dict, 错误信息)。"""
    if not GATEWAY_TOKEN:
        return None, "not_configured"
    req = urllib.request.Request(
        GATEWAY_URL + path, method=method, data=json.dumps(body).encode() if body is not None else None,
        headers={"X-Admin-Token": GATEWAY_TOKEN, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.loads(r.read().decode("utf-8")), ""
    except urllib.error.HTTPError as e:
        try:
            return None, json.loads(e.read().decode("utf-8")).get("message") or f"网关返回 {e.code}"
        except ValueError:
            return None, f"网关返回 {e.code}"
    except (urllib.error.URLError, OSError, ValueError):
        return None, "unreachable"


def _int_or_none(v, lo, hi):
    v = (v or "").strip()
    if not v:
        return None
    if not v.isdigit() or not lo <= int(v) <= hi:
        raise ValueError
    return int(v)


@app.route("/admin/gateway", methods=["GET", "POST"])
@admin_required
def admin_gateway():
    new_key = None
    if request.method == "POST":
        action, f = request.form.get("action"), request.form
        try:
            if action == "price":
                v = float(f.get("price", ""))
                if not 0.1 <= v <= 10000:
                    raise ValueError
                set_setting("gateway_price_per_k", f"{v:g}")
                flash(f"批发价已改为每 1000 条评论 ¥{v:g}", "ok")
            elif action == "create":
                unlimited = f.get("unlimited") == "on"
                quota = None if unlimited else _int_or_none(f.get("quota"), 1, 100_000_000)
                if not unlimited and quota is None:
                    raise ValueError
                body = {"note": f.get("note", "").strip()[:60], "quota": quota,
                        "days": _int_or_none(f.get("days"), 1, 3650), "rate": _int_or_none(f.get("rate"), 1, 6000) or 120}
                d, err = _gw("POST", "/_admin/keys", body)
                if d and d.get("ok"):
                    new_key = {"key": d["key"], "id": d["id"], "note": body["note"], "quota": quota, "days": body["days"]}
                else:
                    flash(err or (d or {}).get("message") or "创建失败", "err")
            elif action in ("enable", "disable", "delete", "topup", "extend"):
                kid = int(f.get("id", 0))
                body = None
                if action == "topup":
                    body = {"n": _int_or_none(f.get("n"), 1, 100_000_000)}
                elif action == "extend":
                    body = {"days": _int_or_none(f.get("n"), 1, 3650)}
                d, err = _gw("POST", f"/_admin/keys/{kid}/{action}", body or {})
                if d and d.get("ok"):
                    flash({"enable": "已启用", "disable": "已停用", "delete": "已删除", "topup": "已加额度", "extend": "已续期"}[action], "ok")
                else:
                    flash(err or (d or {}).get("message") or "操作失败", "err")
        except ValueError:
            flash("批发价要填 0.1 到 10000 之间的数" if action == "price" else "评论条数、天数要填 1 以上的整数", "err")
        if not new_key:
            return redirect(url_for("admin_gateway"))
    data, err = _gw("GET", "/_admin/keys")
    return render_template("admin_gateway.html", keys=(data or {}).get("keys", []), err=err, new_key=new_key,
                           upstream_ok=bool((data or {}).get("upstream_key_set")),
                           public_url=request.host_url.rstrip("/") + "/gw", now=int(time.time()),
                           price_k=get_setting("gateway_price_per_k") or "10")


# ---------------- 充值 ----------------

PAY_METHODS = {"wechat": "微信", "alipay": "支付宝"}
PAY_QR_EXT = ("png", "jpg", "webp")
MAX_QR_BYTES = 2 * 1024 * 1024
MAX_PENDING_ORDERS = 3
CUSTOM_MIN, CUSTOM_MAX = 10, 50000  # 自定义金额（元）


def _qr_path(kind):
    for ext in PAY_QR_EXT:
        p = os.path.join(ai.MEDIA_DIR, f"pay-{kind}.{ext}")
        if os.path.exists(p):
            return p
    return None


def _qr_ext(data):
    """按文件头判断图片格式，不信任用户给的文件名。"""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def _parse_packages(text):
    """每行「付款金额（元） 到账评论条数」→ [(金额分, 条数)]，最多 8 档，金额去重。
    只写金额不写条数的，按「1 元换多少条」自动算。"""
    out, seen = [], set()
    for line in (text or "").splitlines():
        parts = re.split(r"[\s,，:：]+", line.strip())
        if not parts or not parts[0]:
            continue
        try:
            yuan = float(parts[0])
            amount = int(round(yuan * 100))
            credits = billing.to_units(parts[1]) if len(parts) > 1 else int(yuan * billing.comments_per_yuan())
        except ValueError:
            continue
        if not 100 <= amount <= 5_000_000 or not 1 <= credits <= 100_000_000 or amount in seen:
            continue
        seen.add(amount)
        out.append((amount, credits))
    return sorted(out)[:8]


def _pay_info():
    return {"packages": _parse_packages(get_setting("recharge_packages")),
            "qr": {k: _qr_path(k) is not None for k in PAY_METHODS},
            "qr_v": {k: int(os.path.getmtime(p)) for k in PAY_METHODS if (p := _qr_path(k))},
            "contact": get_setting("recharge_contact"), "note": get_setting("recharge_note")}


@app.route("/recharge")
def recharge_page():
    conn = get_db()
    orders = conn.execute("SELECT * FROM recharge_orders WHERE user_id=? ORDER BY id DESC LIMIT 20",
                          (g.user["id"],)).fetchall()
    conn.close()
    return render_template("recharge.html", pay=_pay_info(), orders=orders, METHODS=PAY_METHODS, per_yuan=billing.comments_per_yuan(), prices=_prices(),
                           CUSTOM_MIN=CUSTOM_MIN, CUSTOM_MAX=CUSTOM_MAX)


@app.get("/pay-qr/<kind>")
def pay_qr(kind):
    p = _qr_path(kind) if kind in PAY_METHODS else None
    if not p:
        abort(404)
    resp = send_from_directory(os.path.dirname(p), os.path.basename(p), max_age=300)
    resp.headers["Cache-Control"] = "private, max-age=300"
    return resp


@app.post("/api/recharge")
def create_recharge():
    d = request.get_json(force=True, silent=True) or {}
    pay = _pay_info()
    method = d.get("method")
    if method not in PAY_METHODS or not pay["qr"].get(method):
        return jsonify(ok=False, message="这种付款方式暂时不能用，请换一种"), 400
    note = str(d.get("note") or "").strip()
    if not 2 <= len(note) <= 40:
        return jsonify(ok=False, message="请填一下付款备注（你的微信昵称或支付宝姓名，方便核对）"), 400
    if d.get("custom"):
        try:
            yuan = float(d.get("amount"))
        except (TypeError, ValueError):
            return jsonify(ok=False, message="金额填写有误"), 400
        if not CUSTOM_MIN <= yuan <= CUSTOM_MAX:
            return jsonify(ok=False, message=f"自定义金额要在 {CUSTOM_MIN} 到 {CUSTOM_MAX} 元之间"), 400
        amount, credits = int(round(yuan * 100)), int(yuan * billing.comments_per_yuan())
    else:
        amount_c = _int(d.get("amount"), 0, 0, 10 ** 9)
        match = [p for p in pay["packages"] if p[0] == amount_c]
        if not match:
            return jsonify(ok=False, message="套餐已经变了，请刷新页面重新选择"), 400
        amount, credits = match[0]
    conn = get_db()
    pending = conn.execute("SELECT COUNT(*) FROM recharge_orders WHERE user_id=? AND status='待确认'",
                           (g.user["id"],)).fetchone()[0]
    if pending >= MAX_PENDING_ORDERS:
        conn.close()
        return jsonify(ok=False, message=f"你还有 {pending} 笔付款在等管理员确认，确认后再提交新的"), 429
    cur = conn.execute("INSERT INTO recharge_orders (user_id, amount, bonus, credits, method, payer_note) VALUES (?,?,?,?,?,?)",
                       (g.user["id"], amount, 0, credits, PAY_METHODS[method], note))
    conn.commit()
    conn.close()
    return jsonify(ok=True, id=cur.lastrowid)


@app.post("/api/recharge/<int:oid>/cancel")
def cancel_recharge(oid):
    conn = get_db()
    cur = conn.execute("DELETE FROM recharge_orders WHERE id=? AND user_id=? AND status='待确认'", (oid, g.user["id"]))
    conn.commit()
    conn.close()
    return jsonify(ok=bool(cur.rowcount), message="" if cur.rowcount else "这笔已经被处理，不能撤回了")


@app.route("/admin/recharges", methods=["GET", "POST"])
@admin_required
def admin_recharges():
    if request.method == "POST":
        action = request.form.get("action")
        if action == "pay_settings":
            _save_pay_settings()
        elif action in ("confirm", "reject"):
            _handle_order(action)
        return redirect(url_for("admin_recharges"))
    conn = get_db()
    pending = conn.execute("""SELECT o.*, u.phone, u.nickname, u.balance FROM recharge_orders o
                              JOIN users u ON u.id=o.user_id WHERE o.status='待确认' ORDER BY o.id""").fetchall()
    history = conn.execute("""SELECT o.*, u.phone, u.nickname FROM recharge_orders o
                              JOIN users u ON u.id=o.user_id WHERE o.status!='待确认' ORDER BY o.id DESC LIMIT 100""").fetchall()
    conn.close()
    return render_template("admin_recharges.html", pending=pending, history=history, pay=_pay_info(),
                           packages_text=get_setting("recharge_packages"), METHODS=PAY_METHODS)


def _save_pay_settings():
    text = request.form.get("recharge_packages", "")
    pk = _parse_packages(text)
    if text.strip() and not pk:
        flash("套餐一行都没识别出来，格式是「付款金额 到账条数」，例如 45 5000，没有修改", "err")
    else:
        set_setting("recharge_packages", "\n".join(f"{a / 100:g} {c}" for a, c in pk))
    set_setting("recharge_contact", request.form.get("recharge_contact", "").strip()[:60])
    set_setting("recharge_note", request.form.get("recharge_note", "").strip()[:300])
    os.makedirs(ai.MEDIA_DIR, exist_ok=True)
    problems = []
    for kind, label in PAY_METHODS.items():
        if request.form.get(f"remove_{kind}"):
            while (p := _qr_path(kind)):
                os.remove(p)
        f = request.files.get(f"qr_{kind}")
        if not f or not f.filename:
            continue
        data = f.read(MAX_QR_BYTES + 1)
        ext = _qr_ext(data)
        if len(data) > MAX_QR_BYTES:
            problems.append(f"{label}收款码超过 2MB")
        elif not ext:
            problems.append(f"{label}收款码不是 PNG / JPG / WEBP 图片")
        else:
            while (p := _qr_path(kind)):
                os.remove(p)
            with open(os.path.join(ai.MEDIA_DIR, f"pay-{kind}.{ext}"), "wb") as out:
                out.write(data)
    flash("；".join(problems) + "，那几张没有更新" if problems else "收款设置已保存", "err" if problems else "ok")


def _handle_order(action):
    oid = _int(request.form.get("order_id"), 0, 0, 10 ** 9)
    conn = get_db()
    try:
        order = conn.execute("SELECT * FROM recharge_orders WHERE id=?", (oid,)).fetchone()
        if not order or order["status"] != "待确认":
            flash("这笔订单已经处理过了", "err")
            return
        if action == "reject":
            conn.execute("UPDATE recharge_orders SET status='已拒绝', reject_note=?, handled_at=datetime('now','localtime'), "
                         "handled_by=? WHERE id=? AND status='待确认'",
                         (request.form.get("reject_note", "").strip()[:100] or "未收到这笔付款", g.user["id"], oid))
            conn.commit()
            flash(f"订单 #{oid} 已拒绝", "ok")
            return
        try:  # 管理员可以按实际收到的钱、实际要给的条数来改
            amount = int(round(float(request.form.get("amount", "")) * 100))
            credits = billing.to_units(request.form.get("credits", ""))
        except ValueError:
            flash("金额或条数填写有误", "err")
            return
        if not 0 < amount <= 100_000_000 or not 0 < credits <= 1_000_000_000:
            flash("金额或条数填写有误", "err")
            return
        cur = conn.execute("UPDATE recharge_orders SET status='已到账', amount=?, credits=?, handled_at=datetime('now','localtime'), "
                           "handled_by=? WHERE id=? AND status='待确认'", (amount, credits, g.user["id"], oid))
        if cur.rowcount != 1:  # 并发点了两次，只入账一次
            flash("这笔订单已经处理过了", "err")
            return
        billing.adjust(conn, order["user_id"], credits, "充值", f"{order['method']}付款 ¥{amount / 100:g} · 订单 #{oid}",
                       operator_id=g.user["id"])
        conn.commit()
        flash(f"订单 #{oid} 已到账：{billing.fmt(credits)} 条评论额度", "ok")
    finally:
        conn.close()


# ---------------- JSON API ----------------

def _int(v, default, lo, hi):
    try:
        return max(lo, min(int(v), hi))
    except (TypeError, ValueError):
        return default


def _check_points(platform_keys):
    if g.user["is_admin"]:
        return None
    cheapest = min(billing.unit_cost(k) for k in platform_keys)
    if g.user["balance"] < cheapest:
        return f"评论额度不足（当前 {billing.fmt(g.user['balance'])} 条），请到「充值」页充值"
    return None


@app.post("/api/tasks")
def create_task():
    d = request.get_json(force=True)
    keywords = word_list(d.get("keywords", ""))
    platforms = [p for p in d.get("platforms", []) if p in PLATFORMS and PLATFORMS[p].searchable]
    if not keywords:
        return jsonify(ok=False, message="请至少输入一个关键词"), 400
    if not platforms:
        return jsonify(ok=False, message="请至少选择一个平台"), 400
    if msg := _check_points(platforms):
        return jsonify(ok=False, message=msg), 402
    tid = start_task(
        owner_id=g.user["id"], mode="search", project=(d.get("project") or "默认").strip()[:40],
        keywords="\n".join(keywords), platforms=",".join(platforms),
        posts_per_keyword=_int(d.get("posts_per_keyword"), 5, 1, 30),
        comments_per_post=_int(d.get("comments_per_post"), 100, 10, 500),
        min_comments=_int(d.get("min_comments"), 0, 0, 100000),
        extra_intent="\n".join(word_list(d.get("extra_intent", ""))),
        fetch_replies=1 if d.get("fetch_replies", True) else 0,
        max_age_days=_age_days(d.get("max_age_days")),
        regions=",".join(_regions(d.get("regions"))),
        keep_unknown_region=1 if d.get("keep_unknown_region", True) else 0,
        skip_seen=1 if d.get("skip_seen", True) else 0,
    )
    return jsonify(ok=True, task_id=tid)


RERUN_FIELDS = ("mode", "project", "keywords", "platforms", "posts_per_keyword", "comments_per_post", "min_comments",
                "extra_intent", "fetch_replies", "max_age_days", "regions", "keep_unknown_region", "skip_seen")


@app.post("/api/tasks/<int:tid>/rerun")
def rerun_task(tid):
    conn = get_db()
    t = conn.execute("SELECT * FROM tasks WHERE id=? AND owner_id=?", (tid, g.user["id"])).fetchone()
    conn.close()
    if not t:
        abort(404)
    if msg := _check_points([k for k in (t["platforms"] or "").split(",") if k in PLATFORMS] or ["douyin"]):
        return jsonify(ok=False, message=msg), 402
    new_id = start_task(owner_id=g.user["id"], **{k: t[k] for k in RERUN_FIELDS})
    return jsonify(ok=True, task_id=new_id)


def _age_days(v):
    allowed = {d for d, _ in AGE_CHOICES}
    try:
        v = int(v)
    except (TypeError, ValueError):
        return DEFAULT_AGE_DAYS
    return v if v in allowed else DEFAULT_AGE_DAYS


def _regions(v):
    valid = set(PROVINCES) | {OVERSEAS}
    return [r for r in dict.fromkeys(v or []) if r in valid]


@app.post("/api/tasks/manual")
def create_manual_task():
    d = request.get_json(force=True)
    platform = d.get("platform", "")
    lines = [l.strip() for l in (d.get("links") or "").splitlines() if l.strip()]
    if platform not in PLATFORMS:
        return jsonify(ok=False, message="请选择平台"), 400
    if not lines:
        return jsonify(ok=False, message="请粘贴链接或ID"), 400
    bad = [l for l in lines if not PLATFORMS[platform].parse_manual(l)]
    if bad:
        return jsonify(ok=False, message=f"识别不了：{bad[0][:60]}"), 400
    if msg := _check_points([platform]):
        return jsonify(ok=False, message=msg), 402
    tid = start_task(
        owner_id=g.user["id"], mode="manual", project=(d.get("project") or "默认").strip()[:40],
        keywords="\n".join(lines), platforms=platform,
        comments_per_post=_int(d.get("comments_per_post"), 100, 10, 500),
        posts_per_keyword=len(lines), min_comments=0,
        extra_intent="\n".join(word_list(d.get("extra_intent", ""))),
        fetch_replies=1 if d.get("fetch_replies", True) else 0,
    )
    return jsonify(ok=True, task_id=tid)


@app.post("/api/tasks/<int:tid>/stop")
def stop_task(tid):
    conn = get_db()
    conn.execute("UPDATE tasks SET status='停止中' WHERE id=? AND owner_id=? AND status IN ('进行中','排队中')",
                 (tid, g.user["id"]))
    conn.commit()
    conn.close()
    return jsonify(ok=True)


@app.get("/api/tasks")
def list_tasks():
    conn = get_db()
    rows = [dict(r) for r in conn.execute("SELECT * FROM tasks WHERE owner_id=? ORDER BY id DESC LIMIT 30",
                                          (g.user["id"],))]
    conn.close()
    if not g.user["is_admin"]:
        for r in rows:
            r.pop("balance_before", None)
            r.pop("balance_after", None)
    return jsonify(rows)


_dm_log = {}  # uid -> [timestamps]
DM_PER_HOUR = 300


@app.post("/api/leads/<int:lead_id>/dm")
def lead_dm(lead_id):
    """给这条线索生成「公开回复」和「私信开场白」。只生成文字，不发送。"""
    d = request.get_json(force=True, silent=True) or {}
    cred = ai.user_cred(g.user)
    if not cred:
        return jsonify(ok=False, message="请先在「我的账户」里填写你自己的阿里云 API Key", need_key=True), 400
    product = str(d.get("product_info") or "").strip()[:dm.MAX_PRODUCT]
    if len(product) < 10:
        return jsonify(ok=False, message="请先写一下你的业务介绍（至少 10 个字）。AI 只会用你写的信息，不会编造"), 400
    tone = d.get("tone") if d.get("tone") in dm.TONES else "friendly"
    now = time.time()
    log = [t for t in _dm_log.get(g.user["id"], []) if now - t < 3600]
    if len(log) >= DM_PER_HOUR:
        return jsonify(ok=False, message="这一小时生成得太多了，请稍后再试"), 429
    _dm_log[g.user["id"]] = log + [now]
    conn = get_db()
    lead = conn.execute("SELECT l.*, v.title AS video_title FROM leads l LEFT JOIN videos v ON v.id = l.video_id "
                        "WHERE l.id=? AND l.owner_id=?", (lead_id, g.user["id"])).fetchone()
    if lead and product != (g.user["product_info"] or ""):
        conn.execute("UPDATE users SET product_info=? WHERE id=?", (product, g.user["id"]))
        conn.commit()
    conn.close()
    if not lead:
        return jsonify(ok=False, message="找不到这条线索"), 404
    info = dict(lead, platform_label=LABELS.get(lead["platform"], lead["platform"] or ""))
    try:
        pub, priv = dm.generate(cred, info, product, tone)
    except ai.AIError as e:
        return jsonify(ok=False, message=str(e)), 400
    conn = get_db()
    conn.execute("UPDATE leads SET dm_public=?, dm_private=?, dm_at=datetime('now','localtime') WHERE id=? AND owner_id=?",
                 (pub, priv, lead_id, g.user["id"]))
    conn.commit()
    conn.close()
    return jsonify(ok=True, public=pub, private=priv)


@app.post("/api/lead/<int:lead_id>")
def update_lead(lead_id):
    d = {k: (str(v)[:500] if k in ("dm_public", "dm_private") else v)
         for k, v in (request.get_json(force=True) or {}).items() if k in EDITABLE_LEAD_FIELDS}
    if d.get("status") and d["status"] not in STATUSES:
        return jsonify(ok=False), 400
    if d:
        conn = get_db()
        sets = ", ".join(f"{k} = ?" for k in d)
        conn.execute(f"UPDATE leads SET {sets}, updated_at = datetime('now','localtime') WHERE id = ? AND owner_id = ?",
                     (*d.values(), lead_id, g.user["id"]))
        conn.commit()
        conn.close()
    return jsonify(ok=True)


@app.post("/api/rescore")
def api_rescore():
    return jsonify(ok=True, added=rescore_all(g.user["id"]))


@app.get("/api/me")
def api_me():
    return jsonify(balance=g.user["balance"], balance_text=billing.fmt(g.user["balance"]),
                   is_admin=bool(g.user["is_admin"]))


@app.get("/api/balance")
@admin_required
def api_balance():
    real = api.upstream_balance()
    if real is not None:
        return jsonify(balance=real, unit="usd")
    value, unit = api.balance_info()
    return jsonify(balance=value, unit=unit)


if __name__ == "__main__":
    print("=" * 50)
    print("  获客管理系统已启动")
    print("  浏览器打开: http://localhost:5000")
    print("  关闭此窗口即停止系统")
    print("=" * 50)
    if os.environ.get("HUOKE_SCHEDULER") != "1":
        tracking.start_scheduler()
        ai.resume_videos()
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
