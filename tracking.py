"""对标账号追踪：识别账号 → 定时拉最新作品 → 记录数据变化。

profile = {account_id, nickname, handle, avatar, profile_url, bio, followers, posts_count}
post    = {pid, title, url, cover, published (epoch secs | None), pinned, plays, likes, comments, shares, collects}
"""
import re
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

from api import ApiError, request
from billing import to_units
from database import get_db, get_setting
from platforms import clean, pick_cover, to_int

INTERVALS = {6: "每 6 小时", 12: "每 12 小时", 24: "每天"}
# Only these short-link hosts are followed server-side, so a pasted URL can never point the server elsewhere.
SHORT_HOSTS = {"v.douyin.com", "xhslink.com", "www.xhslink.com", "v.kuaishou.com", "b23.tv"}


class AccountError(Exception):
    pass


def get(path, _retries=2, **params):
    # Adding an account is interactive, so fail fast instead of the collector's long retry budget.
    return request("GET", path, params=params, retries=_retries, timeout=20)


def _https(url):
    return ("https://" + url[7:]) if url and url.startswith("http://") else (url or "")


def _expand_short(text):
    m = re.search(r"https?://[^\s，。]+", text)
    if not m:
        return text
    url = m.group(0)
    if urllib.parse.urlparse(url).hostname not in SHORT_HOSTS:
        return url
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.geturl()
    except Exception:
        return url


def _epoch_ms(v):
    v = to_int(v)
    return (v // 1000 if v and v > 10**11 else v) or None


def _first_url(lst):
    """First browser-displayable image URL (HEIC / Kuaishou KVIF are skipped, see platforms.pick_cover)."""
    return pick_cover(lst)


def _bili_thumb(url, size="400w_250h_1c"):
    # hdslb serves full-size originals (~1 MB) unless a thumbnail suffix is appended.
    return f"{url}@{size}.webp" if url and "hdslb.com" in url and "@" not in url else url


class Tracker:
    key = label = ""
    example = ""

    def resolve(self, text):
        raise NotImplementedError

    def refresh(self, account):
        raise NotImplementedError


class DouyinT(Tracker):
    key, label = "douyin", "抖音"
    example = "主页链接（douyin.com/user/…）、分享短链，或抖音号"

    def resolve(self, text):
        text = text.strip()
        m = re.search(r"douyin\.com/user/([\w-]{20,})", text) or re.search(r"\b(MS4w[\w-]{20,})", text)
        if m:
            return m.group(1)
        if re.search(r"https?://", text):
            sec = get("/v1/douyin/user-id", url=re.search(r"https?://\S+", text).group(0)).get("data")
            if isinstance(sec, str) and sec.startswith("MS4w"):
                return sec
            raise AccountError("没能从链接里识别出抖音账号，请复制主页链接")
        info = (get("/v1/douyin/profile-a", unique_id=text).get("data") or {}).get("user_info") or {}
        if info.get("sec_uid"):
            return info["sec_uid"]
        raise AccountError("没找到这个抖音号")

    def refresh(self, a):
        sec = a["account_id"]
        u = (get("/v1/douyin/profile-b", sec_user_id=sec).get("data") or {}).get("user") or {}
        try:
            d = get("/v1/douyin/posts-a", sec_user_id=sec, max_cursor=0, count=20).get("data") or {}
        except ApiError:
            d = get("/v1/douyin/posts-b", sec_user_id=sec, max_cursor=0, count=20).get("data") or {}
        profile = dict(nickname=u.get("nickname"), handle=u.get("unique_id") or u.get("short_id"),
                       avatar=_first_url((u.get("avatar_thumb") or {}).get("url_list")),
                       profile_url=f"https://www.douyin.com/user/{sec}", bio=u.get("signature"),
                       followers=to_int(u.get("follower_count")), posts_count=to_int(u.get("aweme_count")))
        posts = []
        for x in d.get("aweme_list") or []:
            s = x.get("statistics") or {}
            posts.append(dict(pid=x["aweme_id"], title=x.get("desc"), url=f"https://www.douyin.com/video/{x['aweme_id']}",
                              cover=_first_url(((x.get("video") or {}).get("cover") or {}).get("url_list")),
                              published=to_int(x.get("create_time")), pinned=1 if x.get("is_top") else 0,
                              plays=to_int(s.get("play_count")) or None, likes=to_int(s.get("digg_count")),
                              comments=to_int(s.get("comment_count")), shares=to_int(s.get("share_count")),
                              collects=to_int(s.get("collect_count"))))
        return profile, posts


class XiaohongshuT(Tracker):
    key, label = "xiaohongshu", "小红书"
    example = "主页链接（xiaohongshu.com/user/profile/…）或分享短链"

    def resolve(self, text):
        text = _expand_short(text.strip())
        m = re.search(r"user/profile/([0-9a-f]{24})", text) or re.fullmatch(r"\s*([0-9a-f]{24})\s*", text)
        if m:
            return m.group(1)
        raise AccountError("请粘贴小红书主页链接（xiaohongshu.com/user/profile/…）")

    def refresh(self, a):
        uid = a["account_id"]
        u = (get("/v1/xiaohongshu/profile", user_id=uid).get("data") or {}).get("data") or {}
        d = (get("/v1/xiaohongshu/posts", user_id=uid, cursor="").get("data") or {}).get("data") or {}
        profile = dict(nickname=u.get("nickname"), handle=u.get("red_id"), avatar=u.get("imageb") or _first_url(u.get("images")),
                       profile_url=f"https://www.xiaohongshu.com/user/profile/{uid}", bio=u.get("desc"),
                       followers=to_int(u.get("fans")), posts_count=None)
        posts = []
        for n in d.get("notes") or []:
            posts.append(dict(pid=n["id"], title=n.get("display_title") or n.get("title") or n.get("desc"),
                              url=f"https://www.xiaohongshu.com/explore/{n['id']}",
                              cover=_first_url(n.get("images_list")), published=to_int(n.get("create_time")),
                              pinned=1 if n.get("sticky") else 0, plays=to_int(n.get("view_count")) or None,
                              likes=to_int(n.get("likes")), comments=to_int(n.get("comments_count")),
                              shares=to_int(n.get("share_count")), collects=to_int(n.get("collected_count"))))
        return profile, posts


class KuaishouT(Tracker):
    key, label = "kuaishou", "快手"
    example = "主页链接（kuaishou.com/profile/…）、分享短链，或用户数字 ID"

    def _profile(self, uid):
        return (get("/v1/kuaishou/profile", user_id=uid).get("data") or {}).get("userProfile") or {}

    def resolve(self, text):
        text = _expand_short(text.strip())
        m = re.search(r"profile/([\w-]+)", text) or re.search(r"userId=([\w-]+)", text) or re.fullmatch(r"\s*(\w+)\s*", text)
        if not m:
            raise AccountError("请粘贴快手主页链接（kuaishou.com/profile/…）")
        uid = (self._profile(m.group(1)).get("profile") or {}).get("user_id")
        if not uid:
            raise AccountError("没找到这个快手账号")
        return str(uid)

    def refresh(self, a):
        uid = a["account_id"]
        p = self._profile(uid)
        prof, cnt = p.get("profile") or {}, p.get("ownerCount") or {}
        d = get("/v1/kuaishou/posts", user_id=uid, pcursor="").get("data") or {}
        eid = ""
        posts = []
        for f in d.get("feeds") or []:
            share = dict(urllib.parse.parse_qsl(f.get("share_info") or ""))
            eid = eid or share.get("userId", "")
            posts.append(dict(pid=str(f["photo_id"]), title=f.get("caption"),
                              url=f"https://www.kuaishou.com/short-video/{share['photoId']}" if share.get("photoId") else "",
                              cover=_first_url(f.get("cover_thumbnail_urls")), published=_epoch_ms(f.get("timestamp")),
                              pinned=0, plays=to_int(f.get("view_count")), likes=to_int(f.get("like_count")),
                              comments=to_int(f.get("comment_count")), shares=to_int(f.get("forward_count")), collects=None))
        profile = dict(nickname=prof.get("user_name"), handle=eid, avatar=prof.get("headurl"),
                       profile_url=f"https://www.kuaishou.com/profile/{eid}" if eid else "", bio=prof.get("user_text"),
                       followers=to_int(cnt.get("fan")), posts_count=to_int(cnt.get("photo_public")))
        return profile, posts


class BilibiliT(Tracker):
    key, label = "bilibili", "B站"
    example = "空间链接（space.bilibili.com/…）、b23.tv 短链，或 UID"

    def resolve(self, text):
        text = text.strip()
        m = re.search(r"space\.bilibili\.com/(\d+)", text) or re.fullmatch(r"\s*(\d{3,})\s*", text)
        if m:
            return m.group(1)
        if "b23.tv" in text:
            uid = (get("/v1/bilibili/user-id", share_link=re.search(r"https?://\S+", text).group(0))
                   .get("data") or {}).get("user_id")
            if uid:
                return str(uid)
        raise AccountError("请粘贴 B站 空间链接（space.bilibili.com/…）或 UID")

    def refresh(self, a):
        uid = a["account_id"]
        rel = get("/v1/bilibili/stats", uid=uid).get("data") or {}
        try:  # the web list has comment counts but is flaky; the app list is reliable but has no comment counts
            d = (get("/v1/bilibili/posts-a", _retries=1, uid=uid, pn=1, ps=20, order="pubdate")
                 .get("data") or {}).get("data") or {}
            rows, total = (d.get("list") or {}).get("vlist") or [], (d.get("page") or {}).get("count")
            posts = [dict(pid=v["bvid"], title=v.get("title"), url=f"https://www.bilibili.com/video/{v['bvid']}",
                          cover=_bili_thumb(v.get("pic")), published=to_int(v.get("created")), pinned=0,
                          plays=to_int(v.get("play")), likes=None, comments=to_int(v.get("comment")),
                          shares=None, collects=None) for v in rows]
        except ApiError:
            d = (get("/v1/bilibili/posts-b", user_id=uid, page=1, ps=20).get("data") or {}).get("data") or {}
            rows, total = d.get("item") or [], d.get("count")
            posts = [dict(pid=v["bvid"], title=v.get("title"), url=f"https://www.bilibili.com/video/{v['bvid']}",
                          cover=_bili_thumb(v.get("cover")), published=to_int(v.get("ctime")), pinned=0, plays=to_int(v.get("play")),
                          likes=None, comments=None, shares=None, collects=None) for v in rows if v.get("bvid")]
        profile = dict(nickname=(rows[0].get("author") if rows else None), handle=uid, avatar=None,
                       profile_url=f"https://space.bilibili.com/{uid}", bio=None,
                       followers=to_int(rel.get("follower")), posts_count=to_int(total))
        if not a.get("nickname") or not a.get("avatar"):
            info = (get("/v1/bilibili/profile", uid=uid).get("data") or {}).get("data") or {}
            profile.update(nickname=info.get("name") or profile["nickname"], avatar=_bili_thumb(info.get("face"), "96w_96h_1c"),
                           bio=info.get("sign"))
        return profile, posts


class WeiboT(Tracker):
    key, label = "weibo", "微博"
    example = "主页链接（weibo.com/u/…）或数字 UID"

    def resolve(self, text):
        m = re.search(r"weibo\.(?:com|cn)/(?:u/)?(\d{5,})", text) or re.fullmatch(r"\s*(\d{5,})\s*", text)
        if m:
            return m.group(1)
        raise AccountError("请粘贴微博主页链接（weibo.com/u/数字）或数字 UID")

    @staticmethod
    def _mblogs(items):
        for it in items or []:
            if it.get("category") == "feed" and (it.get("data") or {}).get("idstr"):
                yield it["data"]
            elif it.get("items"):
                yield from WeiboT._mblogs(it["items"])

    def refresh(self, a):
        uid = a["account_id"]
        d = get("/v1/weibo/posts", uid=uid, page=1).get("data") or {}
        blogs = [b for b in self._mblogs(d.get("items")) if str((b.get("user") or {}).get("id")) == str(uid)]
        u = (blogs[0].get("user") if blogs else None) or {}
        profile = dict(nickname=u.get("screen_name"), handle=uid, avatar=u.get("profile_image_url"),
                       profile_url=f"https://weibo.com/u/{uid}", bio=u.get("description"),
                       followers=to_int(u.get("followers_count")), posts_count=to_int(u.get("statuses_count")))
        posts = []
        for b in blogs:
            try:
                ts = int(datetime.strptime(b.get("created_at", ""), "%a %b %d %H:%M:%S %z %Y").timestamp())
            except ValueError:
                ts = None
            posts.append(dict(pid=b["idstr"], title=clean(b.get("text_raw") or b.get("text")),
                              url=f"https://weibo.com/{uid}/{b.get('mblogid') or b['idstr']}",
                              cover=b.get("thumbnail_pic") or b.get("bmiddle_pic"), published=ts,
                              pinned=1 if b.get("isTop") else 0, plays=None, likes=to_int(b.get("attitudes_count")),
                              comments=to_int(b.get("comments_count")), shares=to_int(b.get("reposts_count")), collects=None))
        if not profile["nickname"]:
            raise AccountError("这个微博账号没有公开内容，或 UID 不对")
        return profile, posts


TRACKERS = {t.key: t for t in [DouyinT(), XiaohongshuT(), KuaishouT(), BilibiliT(), WeiboT()]}


# ---------------- refresh ----------------

def track_price():
    try:
        return max(0, to_units(get_setting("price_track") or 0))
    except ValueError:
        return 0


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _later(hours):
    return (datetime.now() + timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")


def _fmt_ts(ts):
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if ts else None


def refresh_account(conn, acc_id, initial=False):
    """Fetch and store one account. Raises AccountError with a user-facing message on failure."""
    a = conn.execute("SELECT a.*, u.is_admin, u.balance FROM accounts a JOIN users u ON u.id = a.owner_id "
                     "WHERE a.id=?", (acc_id,)).fetchone()
    if not a:
        raise AccountError("账号不存在")
    price = 0 if a["is_admin"] else track_price()
    if price and a["balance"] < price:
        conn.execute("UPDATE accounts SET active=0, last_error=?, next_check=NULL WHERE id=?",
                     ("评论额度不足，已暂停追踪，充值后点「恢复」", acc_id))
        conn.commit()
        raise AccountError("评论额度不足，已暂停追踪")
    try:
        profile, posts = TRACKERS[a["platform"]].refresh(dict(a))
    except ApiError as e:
        conn.execute("UPDATE accounts SET last_error=?, next_check=? WHERE id=?",
                     ("平台接口暂时不可用，1 小时后自动重试", _later(1), acc_id))
        conn.commit()
        raise AccountError("平台接口暂时不可用，稍后再试") from e

    first_fetch = initial or not conn.execute("SELECT 1 FROM account_posts WHERE account_ref=? LIMIT 1", (acc_id,)).fetchone()
    now = _now()
    conn.execute("UPDATE account_posts SET is_new=0 WHERE account_ref=?", (acc_id,))
    for p in posts:
        pid = f"{acc_id}:{p['pid']}"
        old = conn.execute("SELECT plays, likes, comments FROM account_posts WHERE id=?", (pid,)).fetchone()
        delta = lambda k: (p[k] - old[k]) if old and p[k] is not None and old[k] is not None else 0
        title = clean(p["title"])[:300]
        if old:
            conn.execute("""UPDATE account_posts SET title=?, url=?, cover=?, pinned=?, plays=COALESCE(?, plays),
                            likes=COALESCE(?, likes), comments=COALESCE(?, comments), shares=COALESCE(?, shares),
                            collects=COALESCE(?, collects), d_plays=?, d_likes=?, d_comments=?, last_updated=? WHERE id=?""",
                         (title, p["url"], _https(p["cover"]), p["pinned"], p["plays"], p["likes"], p["comments"], p["shares"],
                          p["collects"], delta("plays"), delta("likes"), delta("comments"), now, pid))
        else:
            conn.execute("""INSERT INTO account_posts (id, account_ref, owner_id, platform, native_id, title, url, cover,
                            published_at, pinned, plays, likes, comments, shares, collects, is_new, last_updated)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                         (pid, acc_id, a["owner_id"], a["platform"], p["pid"], title, p["url"], _https(p["cover"]),
                          _fmt_ts(p["published"]), p["pinned"], p["plays"], p["likes"], p["comments"], p["shares"],
                          p["collects"], 0 if first_fetch else 1, now))
        conn.execute("INSERT INTO post_snapshots (post_ref, at, plays, likes, comments, shares) VALUES (?, ?, ?, ?, ?, ?)",
                     (pid, now, p["plays"], p["likes"], p["comments"], p["shares"]))
    conn.execute("""UPDATE accounts SET nickname=COALESCE(?, nickname), handle=COALESCE(NULLIF(?, ''), handle),
                    avatar=COALESCE(NULLIF(?, ''), avatar), profile_url=COALESCE(NULLIF(?, ''), profile_url),
                    bio=COALESCE(?, bio), followers_prev=followers, followers=COALESCE(?, followers),
                    posts_count=COALESCE(?, posts_count), last_checked=?, next_check=?, last_error=NULL WHERE id=?""",
                 (profile["nickname"], profile["handle"], _https(profile["avatar"]), profile["profile_url"], profile["bio"],
                  profile["followers"], profile["posts_count"], now, _later(a["interval_hours"]), acc_id))
    conn.commit()
    if price:
        charge_track(conn, a["owner_id"], acc_id, price)
    return len(posts)


def charge_track(conn, user_id, acc_id, units):
    """All tracking charges of one user on one day share a single ledger row."""
    conn.execute("UPDATE users SET balance = balance - ?, total_spent = total_spent + ? WHERE id = ?", (units, units, user_id))
    bal = conn.execute("SELECT balance FROM users WHERE id=?", (user_id,)).fetchone()[0]
    note = f"对标账号追踪 {datetime.now():%m-%d}"
    row = conn.execute("SELECT id FROM points_log WHERE user_id=? AND kind='扣费' AND note=?", (user_id, note)).fetchone()
    if row:
        conn.execute("UPDATE points_log SET delta = delta - ?, balance_after = ? WHERE id = ?", (units, bal, row[0]))
    else:
        conn.execute("INSERT INTO points_log (user_id, delta, balance_after, kind, note) VALUES (?, ?, ?, '扣费', ?)",
                     (user_id, -units, bal, note))
    conn.execute("UPDATE accounts SET points_cost = points_cost + ? WHERE id=?", (units, acc_id))
    conn.commit()


# ---------------- scheduler ----------------

_lock = threading.Lock()


def run_due():
    """Refresh every account whose next_check has passed. Claims each row first so two runners never double-charge."""
    with _lock:
        conn = get_db()
        due = [r[0] for r in conn.execute(
            "SELECT id FROM accounts WHERE active=1 AND next_check IS NOT NULL AND next_check <= ? ORDER BY next_check LIMIT 50",
            (_now(),))]
        for acc_id in due:
            claimed = conn.execute("UPDATE accounts SET next_check=? WHERE id=? AND next_check <= ?",
                                   (_later(1), acc_id, _now())).rowcount
            conn.commit()
            if not claimed:
                continue
            try:
                refresh_account(conn, acc_id)
            except AccountError:
                pass
            except Exception as e:  # keep the scheduler alive no matter what one account does
                conn.execute("UPDATE accounts SET last_error=? WHERE id=?", (f"刷新出错：{e}"[:200], acc_id))
                conn.commit()
            time.sleep(1)
        conn.close()


def start_scheduler(every_seconds=120):
    def loop():
        while True:
            try:
                run_due()
            except Exception:
                pass
            time.sleep(every_seconds)
    threading.Thread(target=loop, daemon=True, name="tracking-scheduler").start()
