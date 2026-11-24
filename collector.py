import json
import threading
import time
from datetime import datetime

import api
from api import ApiError
from billing import OutOfPoints, charge_task, unit_cost
from database import get_db, get_setting, set_setting, word_list
from platforms import PLATFORMS
from regions import normalize_region, region_ok

_run_lock = threading.Lock()

# Reply threads are fetched one API call per parent, so only open threads that are worth a call.
REPLY_MIN = 3
REPLY_PAGES_PER_THREAD = 2
TOP_LEVEL_SHARE = 0.6
SEARCH_PAGES = 5
PLATFORM_FAIL_TTL = 6 * 3600   # 平台"接口异常"标记多久后视为过期


def record_health(platform_key, ok, msg=""):
    set_setting(f"health:{platform_key}", json.dumps(
        {"ok": ok, "at": time.strftime("%Y-%m-%d %H:%M"), "msg": msg[:120]}, ensure_ascii=False))


def platform_health():
    out = {}
    for key in PLATFORMS:
        try:
            h = json.loads(get_setting(f"health:{key}") or "null")
        except ValueError:
            h = None
        # 只在真的搜索时才会更新这条记录。"异常"超过 6 小时没复查，多半是过期信息，不再把平台标成异常
        if h and not h.get("ok"):
            try:
                age = time.time() - time.mktime(time.strptime(h.get("at", ""), "%Y-%m-%d %H:%M"))
            except ValueError:
                age = 0
            if age > PLATFORM_FAIL_TTL:
                h = None
        out[key] = h
    return out


def intent_words(extra="", owner=None):
    """A 级 / B 级 / 排除词。用户在「我的意向词」里设置过就用他自己的，没设置就用系统默认（管理员后台里那份）。"""
    own = None
    if owner:
        conn = get_db()
        own = conn.execute("SELECT intent_strong, intent_medium, intent_exclude FROM users WHERE id=?", (owner,)).fetchone()
        conn.close()

    def pick(key):
        v = own[key] if own is not None else None
        return word_list(v if v is not None else get_setting(key))

    return pick("intent_strong") + word_list(extra), pick("intent_medium"), pick("intent_exclude")


def score(text, words):
    strong, medium, exclude = words
    if not text or any(w in text for w in exclude):
        return None, []
    hit = [w for w in strong if w in text]
    if hit:
        return "A", hit
    hit = [w for w in medium if w in text]
    if hit:
        return "B", hit
    return None, []


def _update(conn, tid, **fields):
    sets = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE tasks SET {sets} WHERE id=?", (*fields.values(), tid))
    conn.commit()


def _bump(conn, tid, **inc):
    sets = ", ".join(f"{k}={k}+?" for k in inc)
    conn.execute(f"UPDATE tasks SET {sets} WHERE id=?", (*inc.values(), tid))
    conn.commit()


def _stop_requested(conn, tid):
    return conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()["status"] == "停止中"


def _fmt_ts(ts):
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if ts else None


def save_post(conn, owner, plat, post, keyword, project, tid):
    vid = f"{owner}:{plat.key}:{post['pid']}"
    conn.execute("""
        INSERT INTO videos (id, owner_id, platform, native_id, title, author, url, comment_count, keyword, project,
                            task_id, cover, published_at, last_scraped)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now','localtime'))
        ON CONFLICT(id) DO UPDATE SET
            title=COALESCE(NULLIF(excluded.title,''), videos.title),
            author=COALESCE(NULLIF(excluded.author,''), videos.author),
            url=COALESCE(NULLIF(excluded.url,''), videos.url),
            comment_count=COALESCE(excluded.comment_count, videos.comment_count),
            cover=COALESCE(NULLIF(excluded.cover,''), videos.cover),
            published_at=COALESCE(excluded.published_at, videos.published_at),
            last_scraped=excluded.last_scraped
    """, (vid, owner, plat.key, post["pid"], post["title"], post["author"], post["url"], post["comment_count"],
          keyword, project, tid, post.get("cover") or "", _fmt_ts(post.get("published"))))
    return vid


def save_comments(conn, owner, plat, vid, comments, keyword, project, words, is_reply=0, region_cfg=None):
    new = a = b = 0
    regions_csv, keep_unknown = region_cfg or ("", 1)
    for c in comments:
        if not c["cid"] or not c["text"]:
            continue
        cid = f"{owner}:{plat.key}:{c['cid']}"
        province, commented_at = normalize_region(c["region"]), _fmt_ts(c.get("created"))
        cur = conn.execute("""
            INSERT OR IGNORE INTO comments (id, owner_id, video_id, platform, nickname, user_id, user_handle,
                                            user_link, text, digg_count, reply_count, ip_region, is_reply,
                                            province, commented_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (cid, owner, vid, plat.key, c["nickname"], c["user_id"], c["user_handle"], c["user_link"],
              c["text"], c["likes"], c["replies"], c["region"], is_reply, province, commented_at))
        if not cur.rowcount:
            continue
        new += 1
        tier, hits = score(c["text"], words)
        if tier and region_ok(province, regions_csv, keep_unknown):
            conn.execute("""
                INSERT OR IGNORE INTO leads (owner_id, comment_id, video_id, platform, project, keyword, nickname,
                                             user_id, user_handle, user_link, ip_region, text, tier, keywords, is_reply,
                                             province, commented_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (owner, cid, vid, plat.key, project, keyword, c["nickname"], c["user_id"], c["user_handle"],
                  c["user_link"], c["region"], c["text"], tier, ",".join(hits), is_reply, province, commented_at))
            a += tier == "A"
            b += tier == "B"
    conn.commit()
    return new, a, b


def _fill_link(conn, plat, post, vid):
    """没有作品链接的平台（视频号）：只给真正产生了线索的作品补一条分享链接，省接口费。"""
    if not conn.execute("SELECT 1 FROM leads WHERE video_id=? LIMIT 1", (vid,)).fetchone():
        return
    if (conn.execute("SELECT url FROM videos WHERE id=?", (vid,)).fetchone() or [""])[0]:
        return
    try:
        url = plat.share_link(post)
    except ApiError:
        return
    if url:
        conn.execute("UPDATE videos SET url=? WHERE id=? AND COALESCE(url,'')=''", (url, vid))
        conn.commit()


def collect_post(conn, t, plat, post, keyword, project, words, billed):
    tid, owner, limit = t["id"], t["owner_id"], t["comments_per_post"]
    unit = unit_cost(plat.key) if billed else 0      # 每抓 1 条新评论扣几条额度
    if unit:
        balance = conn.execute("SELECT balance FROM users WHERE id=?", (owner,)).fetchone()[0]
        affordable = balance // unit
        if affordable < 1:
            raise OutOfPoints
        limit = min(limit, affordable)
    vid = save_post(conn, owner, plat, post, keyword, project, tid)
    replies = bool(t["fetch_replies"]) and plat.supports_replies
    state = {"got": 0, "new": 0, "cursor": None, "more": True}
    parents = []

    region_cfg = (t["regions"] or "", t["keep_unknown_region"] if t["keep_unknown_region"] is not None else 1)

    def store(batch, is_reply):
        new, a, b = save_comments(conn, owner, plat, vid, batch, keyword, project, words, is_reply, region_cfg)
        state["got"] += len(batch)
        state["new"] += new
        _bump(conn, tid, comments=new, leads_a=a, leads_b=b)

    def top_level(budget):
        while state["more"] and state["got"] < budget:
            try:
                batch, state["cursor"] = plat.comments_page(post, state["cursor"])
            except ApiError:
                _bump(conn, tid, errors=1)
                state["more"] = False
                return
            store(batch, 0)
            parents.extend(c for c in batch if c["replies"] >= REPLY_MIN)
            state["more"] = bool(batch) and state["cursor"] not in (None, "", 0)
            time.sleep(0.3)

    top_level(max(25, int(limit * TOP_LEVEL_SHARE)) if replies else limit)
    if replies:
        for par in sorted(parents, key=lambda c: -c["replies"]):
            rcur = None
            for _ in range(REPLY_PAGES_PER_THREAD):
                if state["got"] >= limit:
                    break
                try:
                    batch, rcur = plat.replies_page(post, par, rcur)
                except ApiError:
                    _bump(conn, tid, errors=1)
                    break
                store(batch, 1)
                time.sleep(0.3)
                if not batch or rcur in (None, "", 0):
                    break
            if state["got"] >= limit:
                break
        top_level(limit)
    _bump(conn, tid, posts=1)
    got = state["got"]
    if unit and state["new"]:    # 只为新抓到的评论付费，重复的不扣；最多扣到设定的上限
        charge_task(conn, owner, tid, min(state["new"], limit) * unit)
    if got and not post.get("url"):
        _fill_link(conn, plat, post, vid)
    return got


def _search_posts(conn, tid, plat, keyword, want, min_comments, max_age_days=0, is_seen=None):
    """Returns (posts, error_message, skipped_old, skipped_seen). Pulls several pages, drops works older than max_age_days
    (the platform's own filter is coarse or missing, so it is checked here as well), keeps the most-commented."""
    found, cursor, error, old, seen = {}, None, "", 0, set()
    cutoff = time.time() - max_age_days * 86400 if max_age_days else 0
    for page in range(SEARCH_PAGES + (2 if max_age_days else 0)):
        try:
            batch, cursor = plat.search(keyword, cursor, max_age_days)
        except ApiError as e:
            _bump(conn, tid, errors=1)
            if page == 0:
                error = "搜索接口暂时不可用"
                record_health(plat.key, False, str(e)[:120])
            break
        if page == 0:
            record_health(plat.key, True)
        for p in batch:
            if cutoff and p.get("published") and p["published"] < cutoff:
                old += 1
                continue
            if is_seen and is_seen(p):      # 以前采过：不再抓，改采后面的新作品
                seen.add(p["pid"])
                continue
            found.setdefault(p["pid"], p)
        if not cursor or len(found) >= want * 3:
            break
    posts = [p for p in found.values() if p["comment_count"] is None or p["comment_count"] >= min_comments]
    posts.sort(key=lambda p: -(p["comment_count"] or 0))
    return posts[:want], error, old, len(seen)


def _log_platform(conn, tid, log, key, **inc):
    entry = log.setdefault(key, {"posts": 0, "comments": 0, "old": 0, "seen": 0, "err": ""})
    for k, v in inc.items():
        entry[k] = (entry[k] + v) if isinstance(v, int) else (v or entry[k])
    _update(conn, tid, platform_log=json.dumps(log, ensure_ascii=False))


def run_task(tid):
    with _run_lock:
        conn = get_db()
        t = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        if t["status"] == "停止中":
            _update(conn, tid, status="已停止", finished_at=time.strftime("%Y-%m-%d %H:%M:%S"))
            return
        _update(conn, tid, status="进行中", balance_before=api.balance())
        words = intent_words(t["extra_intent"], owner=t["owner_id"])
        project = t["project"] or "默认"
        billed = not conn.execute("SELECT is_admin FROM users WHERE id=?", (t["owner_id"],)).fetchone()[0]
        status = "已完成"
        log = {}
        owner = t["owner_id"]
        # 以前已经抓到过评论的作品（只要有评论入库就算采过）；没抓到评论的作品下次还会再试
        seen_vids = ({r[0] for r in conn.execute("SELECT DISTINCT video_id FROM comments WHERE owner_id=?", (owner,))}
                     if t["skip_seen"] is None or t["skip_seen"] else set())
        try:
            if t["mode"] == "manual":
                plat = PLATFORMS[t["platforms"]]
                for i, line in enumerate(l for l in t["keywords"].splitlines() if l.strip()):
                    if _stop_requested(conn, tid):
                        status = "已停止"
                        break
                    post = plat.parse_manual(line.strip())
                    if not post:
                        _bump(conn, tid, errors=1)
                        continue
                    _update(conn, tid, progress=f"{plat.label} · 手动第{i + 1}条")
                    got = collect_post(conn, t, plat, post, "手动添加", project, words, billed)
                    _log_platform(conn, tid, log, plat.key, posts=1, comments=got)
            else:
                keywords = word_list(t["keywords"])
                platforms = [PLATFORMS[k] for k in t["platforms"].split(",") if k in PLATFORMS]
                for kw in keywords:
                    for plat in platforms:
                        if not plat.searchable:
                            continue
                        if _stop_requested(conn, tid):
                            raise InterruptedError
                        _update(conn, tid, progress=f"{plat.label} · 「{kw}」搜索中")
                        posts, err, old, seen = _search_posts(
                            conn, tid, plat, kw, t["posts_per_keyword"], t["min_comments"], t["max_age_days"] or 0,
                            is_seen=(lambda p, plat=plat: f"{owner}:{plat.key}:{p['pid']}" in seen_vids) if seen_vids else None)
                        _log_platform(conn, tid, log, plat.key, err=err, old=old, seen=seen)
                        for i, post in enumerate(posts, 1):
                            if _stop_requested(conn, tid):
                                raise InterruptedError
                            _update(conn, tid, progress=f"{plat.label} · 「{kw}」第{i}/{len(posts)}个作品")
                            got = collect_post(conn, t, plat, post, kw, project, words, billed)
                            if got:
                                seen_vids.add(f"{owner}:{plat.key}:{post['pid']}")
                            _log_platform(conn, tid, log, plat.key, posts=1, comments=got)
        except InterruptedError:
            status = "已停止"
        except OutOfPoints:
            status = "余额不足"
            _update(conn, tid, progress="评论额度用完，任务已自动停止，充值后可重新采集")
        except Exception as e:
            status = "出错"
            _update(conn, tid, progress=f"出错：{e}"[:300])
        _update(conn, tid, status=status, balance_after=api.balance(),
                finished_at=time.strftime("%Y-%m-%d %H:%M:%S"))
        conn.close()


def start_task(**fields):
    conn = get_db()
    cols = ", ".join(fields)
    marks = ", ".join("?" for _ in fields)
    tid = conn.execute(f"INSERT INTO tasks ({cols}) VALUES ({marks})", tuple(fields.values())).lastrowid
    conn.commit()
    conn.close()
    threading.Thread(target=run_task, args=(tid,), daemon=True).start()
    return tid


def rescore_all(owner):
    """Apply the current intent words to this user's stored comments that are not yet leads.
    Each comment follows the region setting of the task it came from."""
    words = intent_words(owner=owner)
    conn = get_db()
    rows = conn.execute("""
        SELECT c.*, v.keyword, v.project, t.regions AS t_regions, t.keep_unknown_region AS t_keep
        FROM comments c
        LEFT JOIN videos v ON v.id = c.video_id
        LEFT JOIN tasks t ON t.id = v.task_id
        WHERE c.owner_id = ? AND c.id NOT IN (SELECT comment_id FROM leads WHERE owner_id = ?)
    """, (owner, owner)).fetchall()
    added = 0
    for c in rows:
        tier, hits = score(c["text"], words)
        if tier and region_ok(c["province"], c["t_regions"], 1 if c["t_keep"] is None else c["t_keep"]):
            conn.execute("""
                INSERT OR IGNORE INTO leads (owner_id, comment_id, video_id, platform, project, keyword, nickname,
                                             user_id, user_handle, user_link, ip_region, text, tier, keywords, is_reply,
                                             province, commented_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (owner, c["id"], c["video_id"], c["platform"], c["project"], c["keyword"], c["nickname"],
                  c["user_id"], c["user_handle"], c["user_link"], c["ip_region"], c["text"], tier, ",".join(hits),
                  c["is_reply"] or 0, c["province"] or "", c["commented_at"]))
            added += 1
    conn.commit()
    conn.close()
    return added
