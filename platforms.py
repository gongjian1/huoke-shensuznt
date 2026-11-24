"""
One adapter per platform. Each adapter turns data-service responses into two plain shapes:

  post    = {pid, title, author, comment_count (int|None), url, extra}
  comment = {cid, nickname, user_id, user_handle, user_link, text, likes, replies, region}

search(keyword, cursor, max_age_days) -> (posts, next_cursor | None)   max_age_days=0 表示不限发布时间
comments_page(post, cursor)          -> (comments, next_cursor | None)
replies_page(post, parent, cursor)   -> (comments, next_cursor | None)   only when supports_replies
parse_manual(text)                   -> post | None   (ID or share link pasted by the user)
"""
import html
import json
import re
import time
import urllib.parse

from api import ApiError, get, post as api_post, request as api_request

_TAG = re.compile(r"<[^>]+>")


def clean(s):
    return re.sub(r"\s+", " ", html.unescape(_TAG.sub("", str(s or "")))).strip()


def to_int(v):
    try:
        return int(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


def epoch(v):
    """秒或毫秒时间戳 → 秒；不是有效时间返回 None。"""
    v = to_int(v)
    if not v or v <= 0:
        return None
    return v // 1000 if v > 10**11 else v


# 浏览器解不了的私有图片格式（抖音 HEIC、快手 KVIF/KPG），遇到就不存，页面会用文字海报代替
_BAD_IMG = (".heic", ".heif", ".kvif", ".kpg", ".hevc")


def pick_cover(urls):
    cands = []
    for it in urls or []:
        u = it if isinstance(it, str) else (it or {}).get("url")
        if u:
            u = "https:" + u if u.startswith("//") else u
            cands.append(u.replace("http://", "https://", 1) if u.startswith("http://") else u)
    return next((u for u in cands if not u.split("?")[0].lower().endswith(_BAD_IMG)), "")


def parse_cn_time(s, now=None):
    """微博搜索里的相对时间：'今天21:10'、'昨天08:00'、'5分钟前'、'11月15日 16:25'、'2025-06-01 10:00'。"""
    from datetime import datetime, timedelta
    now = now or datetime.now()
    s = (s or "").strip()
    try:
        m = re.match(r"^(\d+)\s*分钟前$", s)
        if m:
            return int((now - timedelta(minutes=int(m.group(1)))).timestamp())
        m = re.match(r"^(\d+)\s*小时前$", s)
        if m:
            return int((now - timedelta(hours=int(m.group(1)))).timestamp())
        m = re.match(r"^(今天|昨天|前天)\s*(\d{1,2}):(\d{2})$", s)
        if m:
            d = now - timedelta(days={"今天": 0, "昨天": 1, "前天": 2}[m.group(1)])
            return int(d.replace(hour=int(m.group(2)), minute=int(m.group(3)), second=0, microsecond=0).timestamp())
        m = re.match(r"^(?:(\d{4})年)?(\d{1,2})月(\d{1,2})日\s*(\d{1,2}):(\d{2})$", s)
        if m:
            y = int(m.group(1) or now.year)
            t = datetime(y, int(m.group(2)), int(m.group(3)), int(m.group(4)), int(m.group(5)))
            if not m.group(1) and t > now + timedelta(days=1):
                t = t.replace(year=y - 1)
            return int(t.timestamp())
        m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})(?:\s+(\d{1,2}):(\d{2}))?", s)
        if m:
            return int(datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4) or 0), int(m.group(5) or 0)).timestamp())
    except ValueError:
        pass
    return None


def parse_weibo_time(s):
    """微博评论/帖子的 created_at，如 'Fri Oct 23 18:47:54 +0800 2026'。"""
    from datetime import datetime
    try:
        return int(datetime.strptime(s or "", "%a %b %d %H:%M:%S %z %Y").timestamp())
    except ValueError:
        return None


def P(pid, title="", author="", comment_count=None, url="", cover="", published=None, **extra):
    return {"pid": str(pid), "title": clean(title)[:300], "author": clean(author)[:80],
            "comment_count": to_int(comment_count), "url": url or "", "cover": cover or "",
            "published": epoch(published), "extra": extra}


def C(cid, nickname, text, user_id="", handle="", link="", likes=0, replies=0, region="", created=None):
    return {"cid": str(cid), "nickname": clean(nickname)[:80], "text": clean(text),
            "user_id": str(user_id or ""), "user_handle": str(handle or ""),
            "user_link": link or "", "likes": to_int(likes) or 0,
            "replies": to_int(replies) or 0, "region": clean(region), "created": epoch(created)}


def _digits(text, n=15):
    m = re.search(r"\d{%d,}" % n, text)
    return m.group(0) if m else None


class Platform:
    key = ""
    label = ""
    searchable = True
    supports_replies = False
    hint = ""
    id_hint = ""

    def search(self, keyword, cursor=None, max_age_days=0):
        raise NotImplementedError

    def share_link(self, post):
        """搜索结果里没有作品链接的平台，可以在这里按需生成（会产生接口费用）。默认没有。"""
        return ""

    def comments_page(self, post, cursor=None):
        raise NotImplementedError

    def replies_page(self, post, parent, cursor=None):
        raise NotImplementedError

    def parse_manual(self, text):
        return None


class Douyin(Platform):
    key, label = "douyin", "抖音"
    supports_replies = True
    id_hint = "视频链接或作品ID（19位数字）"

    @staticmethod
    def _post(a):
        v = a.get("video") or {}
        return P(a["aweme_id"], a.get("desc"), (a.get("author") or {}).get("nickname"),
                 (a.get("statistics") or {}).get("comment_count"),
                 f"https://www.douyin.com/video/{a['aweme_id']}",
                 cover=pick_cover((v.get("cover") or {}).get("url_list")) or pick_cover((v.get("origin_cover") or {}).get("url_list")),
                 published=a.get("create_time"))

    def search(self, keyword, cursor=None, max_age_days=0):
        # The data service's Douyin search versions go down independently; remember which one works.
        cur = cursor or {"ver": None, "page": 1, "cursor": 0, "search_id": "", "backtrace": ""}
        versions = [cur["ver"]] if cur["ver"] else ["v2", "v5", "v4"]
        err = None
        for ver in versions:
            try:
                return getattr(self, f"_search_{ver}")(keyword, cur, max_age_days)
            except ApiError as e:
                err = e
        raise err

    def _search_v5(self, keyword, cur, max_age_days=0):
        d = api_post("/v1/douyin/search-a", keyword=keyword,
                     offset=(cur["page"] - 1) * 10, page=cur["page"],
                     search_id=cur["search_id"] or None, backtrace=cur["backtrace"] or None).get("data") or {}
        posts = [self._post(it.get("aweme_info") or it) for it in d.get("items") or []
                 if (it.get("aweme_info") or it).get("aweme_id")]
        pg = d.get("pagination") or {}
        nxt = dict(cur, ver="v5", page=cur["page"] + 1, search_id=pg.get("search_id", ""),
                   backtrace=pg.get("backtrace", "")) if pg.get("has_more") else None
        return posts, nxt

    def _search_v2(self, keyword, cur, max_age_days=0):
        # 接口只支持 1 天 / 7 天 / 180 天三档，取够用的最小一档，更精确的范围由采集器再筛
        window = "0" if not max_age_days or max_age_days > 180 else ("1" if max_age_days <= 1 else "7" if max_age_days <= 7 else "180")
        d = api_post("/v1/douyin/search-b", keyword=keyword, cursor=cur["cursor"],
                     sort_type="0", publish_time=window, filter_duration="0", content_type="0",
                     search_id=cur["search_id"] or None, backtrace=cur["backtrace"] or None).get("data") or {}
        posts = []
        for it in d.get("business_data") or []:
            a = (it.get("data") or {}).get("aweme_info") or {}
            if a.get("aweme_id"):
                posts.append(self._post(a))
        bc = d.get("business_config") or {}
        npg = bc.get("next_page") or {}
        nxt = dict(cur, ver="v2", cursor=npg.get("cursor", 0), search_id=npg.get("search_id", ""),
                   backtrace=bc.get("backtrace", "")) if bc.get("has_more") and posts else None
        return posts, nxt

    def _search_v4(self, keyword, cur, max_age_days=0):
        d = (api_post("/v1/douyin/search-c", keyword=keyword,
                      cursor=cur["cursor"]).get("data") or {}).get("data") or {}
        posts = []
        for it in d.get("itemList") or []:
            a = it.get("AwemeInfo") or {}
            if a.get("AwemeId"):
                posts.append(P(a["AwemeId"], a.get("Desc"), (a.get("Author") or {}).get("Nickname"),
                               (a.get("Statistics") or {}).get("CommentCount"),
                               f"https://www.douyin.com/video/{a['AwemeId']}",
                               cover=pick_cover(((a.get("Video") or {}).get("Cover") or {}).get("UrlList")),
                               published=a.get("CreateTime")))
        return posts, (dict(cur, ver="v4", cursor=d.get("cursor", 0)) if d.get("hasMore") and posts else None)

    @staticmethod
    def _comment(c):
        u = c.get("user") or {}
        sec = u.get("sec_uid") or ""
        return C(c.get("cid"), u.get("nickname"), c.get("text"), u.get("uid"),
                 u.get("unique_id") or u.get("short_id"), f"https://www.douyin.com/user/{sec}" if sec else "",
                 c.get("digg_count"), c.get("reply_comment_total"), c.get("ip_label"), c.get("create_time"))

    def comments_page(self, post, cursor=None):
        try:
            d = get("/v1/douyin/comments-a", aweme_id=post["pid"],
                    cursor=cursor or 0, count=20).get("data") or {}
        except ApiError:
            d = get("/v1/douyin/comments-b", aweme_id=post["pid"],
                    cursor=cursor or 0, count=20).get("data") or {}
        return [self._comment(c) for c in d.get("comments") or []], (d.get("cursor") if d.get("has_more") else None)

    def replies_page(self, post, parent, cursor=None):
        params = dict(item_id=post["pid"], comment_id=parent["cid"], cursor=cursor or 0, count=20)
        try:
            d = get("/v1/douyin/replies-a", **params).get("data") or {}
        except ApiError:
            d = get("/v1/douyin/replies-b", **params).get("data") or {}
        return [self._comment(c) for c in d.get("comments") or []], (d.get("cursor") if d.get("has_more") else None)

    def parse_manual(self, text):
        m = re.search(r"(?:video|note|modal_id=)/?(\d{15,})", text) or re.search(r"\d{17,}", text)
        if m:
            vid = m.group(1) if m.groups() else m.group(0)
            return P(vid, url=f"https://www.douyin.com/video/{vid}")
        return None


class Xiaohongshu(Platform):
    key, label = "xiaohongshu", "小红书"
    supports_replies = True
    id_hint = "笔记链接或笔记ID（24位字母数字）"

    def search(self, keyword, cursor=None, max_age_days=0):
        cur = cursor or {"page": 1, "search_id": "", "search_session_id": ""}
        window = "不限" if not max_age_days or max_age_days > 180 else ("一天内" if max_age_days <= 1 else "一周内" if max_age_days <= 7 else "半年内")
        resp = get("/v1/xiaohongshu/search", keyword=keyword, page=cur["page"],
                   search_id=cur["search_id"], search_session_id=cur["search_session_id"], time_filter=window)
        d = resp.get("data") or {}
        posts = []
        for it in (d.get("data") or {}).get("items") or []:
            n = it.get("note")
            if it.get("model_type") != "note" or not n:
                continue
            tok = urllib.parse.quote(n.get("xsec_token") or "")
            imgs = n.get("images_list") or []
            posts.append(P(n["id"], n.get("title") or n.get("desc"), (n.get("user") or {}).get("nickname"),
                           n.get("comments_count"),
                           f"https://www.xiaohongshu.com/explore/{n['id']}?xsec_token={tok}&xsec_source=pc_search",
                           cover=pick_cover([imgs[0].get("url") or imgs[0].get("url_size_large")]) if imgs else "",
                           published=n.get("timestamp")))
        nxt = None
        if posts and d.get("next_page"):
            nxt = {"page": int(d["next_page"]), "search_id": d.get("search_id", ""),
                   "search_session_id": d.get("search_session_id", "")}
        return posts, nxt

    @staticmethod
    def _comment(c):
        u = c.get("user") or {}
        uid = u.get("userid") or ""
        return C(c.get("id"), u.get("nickname"), c.get("content"), uid, u.get("red_id"),
                 f"https://www.xiaohongshu.com/user/profile/{uid}" if uid else "",
                 c.get("like_count"), c.get("sub_comment_count"), c.get("ip_location"), c.get("time"))

    def comments_page(self, post, cursor=None):
        d = (get("/v1/xiaohongshu/comments", note_id=post["pid"],
                 cursor=cursor or "").get("data") or {}).get("data") or {}
        return [self._comment(c) for c in d.get("comments") or []], (d.get("cursor") if d.get("has_more") else None)

    def replies_page(self, post, parent, cursor=None):
        d = (get("/v1/xiaohongshu/replies", note_id=post["pid"],
                 comment_id=parent["cid"], cursor=cursor or "").get("data") or {}).get("data") or {}
        return [self._comment(c) for c in d.get("comments") or []], (d.get("cursor") if d.get("has_more") else None)

    def parse_manual(self, text):
        m = re.search(r"(?:explore|discovery/item|item)/([0-9a-f]{24})", text) or re.search(r"\b[0-9a-f]{24}\b", text)
        if m:
            nid = m.group(1) if m.groups() else m.group(0)
            return P(nid, url=f"https://www.xiaohongshu.com/explore/{nid}")
        return None


class Kuaishou(Platform):
    key, label = "kuaishou", "快手"
    supports_replies = True
    id_hint = "作品ID（纯数字，搜索结果里的 photo_id）"

    def search(self, keyword, cursor=None, max_age_days=0):
        d = get("/v1/kuaishou/search", keyword=keyword, pcursor=cursor or "").get("data") or {}
        posts = []
        for it in d.get("mixFeeds") or []:
            f = it.get("feed") or {}
            if not f.get("photo_id"):
                continue
            share = dict(urllib.parse.parse_qsl(f.get("share_info") or ""))
            url = f"https://www.kuaishou.com/short-video/{share['photoId']}" if share.get("photoId") else ""
            # 快手搜索接口的封面大多是它自家的 KVIF/KPG 格式，浏览器打不开，pick_cover 会跳过，页面用文字海报代替
            posts.append(P(f["photo_id"], f.get("caption"), f.get("user_name"), f.get("comment_count"), url,
                           cover=pick_cover(f.get("cover_thumbnail_urls")), published=f.get("timestamp")))
        pc = d.get("pcursor")
        return posts, (pc if pc and pc != "no_more" else None)

    @staticmethod
    def _comment(c):
        return C(c.get("comment_id"), c.get("author_name"), c.get("content"), c.get("author_id"),
                 "", "", c.get("likedCount"), c.get("subCommentCount"), c.get("authorArea"), c.get("timestamp"))

    def comments_page(self, post, cursor=None):
        d = get("/v1/kuaishou/comments", photo_id=post["pid"], pcursor=cursor or "").get("data") or {}
        pc = d.get("pcursor")
        return [self._comment(c) for c in d.get("rootComments") or []], (pc if pc and pc != "no_more" else None)

    def replies_page(self, post, parent, cursor=None):
        d = get("/v1/kuaishou/replies", photo_id=post["pid"], root_comment_id=parent["cid"],
                pcursor=cursor or "", count=20).get("data") or {}
        pc = d.get("pcursor")
        return [self._comment(c) for c in d.get("subComments") or []], (pc if pc and pc != "no_more" else None)

    def parse_manual(self, text):
        pid = _digits(text, 15)
        return P(pid) if pid else None


class Bilibili(Platform):
    key, label = "bilibili", "B站"
    supports_replies = True
    id_hint = "视频链接或BV号"

    def search(self, keyword, cursor=None, max_age_days=0):
        page = cursor or 1
        window = {}
        if max_age_days:
            now = int(time.time())
            window = {"pubtime_begin_s": now - max_age_days * 86400, "pubtime_end_s": now}
        d = (get("/v1/bilibili/search", keyword=keyword, order="totalrank",
                 page=page, page_size=20, **window).get("data") or {}).get("data") or {}
        posts = [P(r["bvid"], r.get("title"), r.get("author"), r.get("review"),
                   f"https://www.bilibili.com/video/{r['bvid']}", cover=pick_cover([r.get("pic")]),
                   published=r.get("pubdate"))
                 for r in d.get("result") or [] if r.get("type") == "video" and r.get("bvid")]
        return posts, (page + 1 if posts and page < (to_int(d.get("numPages")) or 0) else None)

    @staticmethod
    def _page(d, pn):
        out = []
        for r in d.get("replies") or []:
            m = r.get("member") or {}
            mid = r.get("mid_str") or m.get("mid") or ""
            out.append(C(r.get("rpid_str") or r.get("rpid"), m.get("uname"), (r.get("content") or {}).get("message"),
                         mid, "", f"https://space.bilibili.com/{mid}" if mid else "",
                         r.get("like"), r.get("rcount"),
                         ((r.get("reply_control") or {}).get("location") or "").replace("IP属地：", ""), r.get("ctime")))
        page = d.get("page") or {}
        more = out and (to_int(page.get("num")) or pn) * (to_int(page.get("size")) or 20) < (to_int(page.get("count")) or 0)
        return out, (pn + 1 if more else None)

    def comments_page(self, post, cursor=None):
        pn = cursor or 1
        return self._page((get("/v1/bilibili/comments", bv_id=post["pid"], pn=pn)
                           .get("data") or {}).get("data") or {}, pn)

    def replies_page(self, post, parent, cursor=None):
        pn = cursor or 1
        return self._page((get("/v1/bilibili/replies", bv_id=post["pid"], pn=pn, rpid=parent["cid"])
                           .get("data") or {}).get("data") or {}, pn)

    def parse_manual(self, text):
        m = re.search(r"BV[0-9A-Za-z]{10}", text)
        return P(m.group(0), url=f"https://www.bilibili.com/video/{m.group(0)}") if m else None


class Weibo(Platform):
    key, label = "weibo", "微博"
    supports_replies = True
    id_hint = "微博ID（16位数字）"

    def search(self, keyword, cursor=None, max_age_days=0):
        page = cursor or 1
        scope = None
        if max_age_days:
            from datetime import datetime, timedelta
            scope = f"custom:{(datetime.now() - timedelta(days=max_age_days)):%Y-%m-%d}-0:{datetime.now():%Y-%m-%d}-23"
        d = ((get("/v1/weibo/search", q=keyword, search_type="hot",
                  page=page, timescope=scope).get("data") or {}).get("parsed_data") or {})
        posts = []
        for r in d.get("results") or []:
            if not r.get("weibo_id"):
                continue
            url = r.get("post_url") or ""
            images = (r.get("media") or {}).get("images") or []
            posts.append(P(r["weibo_id"], r.get("content"), r.get("user_nick") or r.get("user_name"),
                           (r.get("interaction") or {}).get("comment_count") or None,
                           ("https:" + url.split("?")[0]) if url.startswith("//") else url,
                           cover=pick_cover(images[:1]), published=parse_cn_time(r.get("publish_time"))))
        return posts, (page + 1 if len(posts) >= 10 and page < 5 else None)

    @staticmethod
    def _page(d):
        out = []
        for c in d.get("data") or []:
            u = c.get("user") or {}
            uid = u.get("idstr") or u.get("id") or ""
            out.append(C(c.get("idstr") or c.get("id"), u.get("screen_name"), c.get("text_raw") or c.get("text"),
                         uid, "", f"https://weibo.com/u/{uid}" if uid else "",
                         c.get("like_counts"), c.get("total_number"), (c.get("source") or "").replace("来自", ""),
                         parse_weibo_time(c.get("created_at"))))
        mx = d.get("max_id")
        return out, (mx if out and mx and str(mx) != "0" else None)

    def comments_page(self, post, cursor=None):
        return self._page(get("/v1/weibo/comments", id=post["pid"], count=20,
                              max_id=cursor or None).get("data") or {})

    def replies_page(self, post, parent, cursor=None):
        return self._page(get("/v1/weibo/replies", id=parent["cid"], count=20,
                              max_id=cursor or None).get("data") or {})

    def parse_manual(self, text):
        pid = _digits(text, 16)
        return P(pid) if pid else None


class Zhihu(Platform):
    key, label = "zhihu", "知乎"
    supports_replies = True
    id_hint = "回答链接或回答ID"

    def search(self, keyword, cursor=None, max_age_days=0):
        offset = cursor or 0
        d = get("/v1/zhihu/search", keyword=keyword, offset=offset,
                limit=20, vertical="answer").get("data") or {}
        posts = []
        for it in d.get("data") or []:
            o = it.get("object") or {}
            if o.get("type") != "answer" or not o.get("id"):
                continue
            title = (o.get("question") or {}).get("name") or o.get("title") or o.get("excerpt")
            thumbs = ((o.get("thumbnail_info") or {}).get("thumbnails") or [])[:1]
            posts.append(P(o["id"], title, (o.get("author") or {}).get("name"), o.get("comment_count"),
                           f"https://www.zhihu.com/answer/{o['id']}", cover=pick_cover(thumbs),
                           published=o.get("created_time")))
        paging = d.get("paging") or {}
        return posts, (offset + 20 if posts and not paging.get("is_end") else None)

    @staticmethod
    def _page(d):
        out = []
        for c in d.get("data") or []:
            a = c.get("author") or {}
            tok = a.get("url_token") or ""
            out.append(C(c.get("id"), a.get("name"), c.get("content"), a.get("id"), tok,
                         f"https://www.zhihu.com/people/{tok}" if tok else "",
                         c.get("like_count"), c.get("child_comment_count"),
                         next((t.get("text") for t in c.get("comment_tag") or [] if t.get("type") == "ip_info"), ""),
                         c.get("created_time")))
        paging = d.get("paging") or {}
        nxt = None
        if out and not paging.get("is_end") and paging.get("next"):
            nxt = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(paging["next"]).query)).get("offset")
        return out, nxt

    def comments_page(self, post, cursor=None):
        return self._page(get("/v1/zhihu/comments", answer_id=post["pid"], order_by="score",
                              limit=20, offset=cursor or "").get("data") or {})

    def replies_page(self, post, parent, cursor=None):
        return self._page(get("/v1/zhihu/replies", comment_id=parent["cid"], order_by="score",
                              limit=20, offset=cursor or "").get("data") or {})

    def parse_manual(self, text):
        m = re.search(r"answer/(\d+)", text)
        pid = m.group(1) if m else _digits(text, 8)
        return P(pid, url=f"https://www.zhihu.com/answer/{pid}") if pid else None


class WechatChannels(Platform):
    key, label = "wechat_channels", "视频号"
    hint = "单价较高、速度较慢，微信接口偶尔不稳定"
    id_hint = "视频号分享短链（https://weixin.qq.com/sph/…）或 objectId"

    def search(self, keyword, cursor=None, max_age_days=0):
        window = None if not max_age_days or max_age_days > 180 else (1 if max_age_days <= 1 else 2 if max_age_days <= 7 else 3)
        d = api_post("/v1/wechat_search/search-a", keyword=keyword, sort="hot",
                     publish_time=window, cursor=cursor, raw=False).get("data") or {}
        posts = []
        for it in d.get("items") or []:
            if not it.get("exportId"):
                continue
            try:
                nonce = str(json.loads((it.get("jumpInfo") or {}).get("extInfo") or "{}").get("feedNonceId") or "")
            except ValueError:
                nonce = ""
            posts.append(P(it["exportId"], it.get("title"), (it.get("source") or {}).get("title"), None, "",
                           published=it.get("pubTime"), export_id=it["exportId"], nonce=nonce))
        return posts, (d.get("cursor") if posts and str(d.get("continue_flag")) == "1" else None)

    def _object_id(self, post):
        ex = post.get("extra") or {}
        if ex.get("object_id"):
            return ex["object_id"]
        body = {"raw": False}
        if ex.get("share_url"):
            body["share_url"] = ex["share_url"]
        else:
            body.update(export_id=ex.get("export_id", ""), object_nonce_id=ex.get("nonce", ""))
        d = api_post("/v1/wechat_channels/detail", **body).get("data") or {}
        ex["object_id"] = str(d.get("id") or "")
        ex["detail_comments"] = to_int(d.get("comment_count"))
        return ex["object_id"]

    def comments_page(self, post, cursor=None):
        oid = self._object_id(post)
        if not oid or post["extra"].get("detail_comments") == 0:
            return [], None
        d = api_post("/v1/wechat_channels/comments", object_id=oid,
                     last_buffer=cursor or "", raw=False).get("data") or {}
        out = [C(c.get("comment_id"), c.get("nickname"), c.get("content"), c.get("username"), "", "",
                 c.get("like_count"), c.get("reply_count"), c.get("ip_region"), c.get("create_time"))
               for c in d.get("comments") or []]
        return out, (d.get("last_buffer") if out and d.get("down_continue") else None)

    def share_link(self, post):
        """视频号没有网页链接，只有微信里能打开的分享短链（weixin.qq.com/sph/…）。每次约 $0.01，
        响应很慢；超时可能已经扣费，所以只请求一次，不重试。"""
        oid = self._object_id(post)
        if not oid:
            return ""
        d = api_request("POST", "/v1/wechat_channels/share",
                        body={"object_id": oid, "raw": False}, retries=1, timeout=40).get("data") or {}
        url = str(d.get("share_url") or "")
        return url if url.startswith("https://weixin.qq.com/sph/") else ""

    def parse_manual(self, text):
        m = re.search(r"https?://weixin\.qq\.com/sph/\w+", text)
        if m:
            return P(m.group(0), url=m.group(0), share_url=m.group(0))
        oid = _digits(text, 15)
        return P(oid, object_id=oid) if oid else None


class WechatMP(Platform):
    key, label = "wechat_mp", "公众号"
    hint = "单价较高，只返回作者精选的评论"
    id_hint = "公众号文章链接（https://mp.weixin.qq.com/s/…）"

    def search(self, keyword, cursor=None, max_age_days=0):
        window = None if not max_age_days or max_age_days > 180 else (1 if max_age_days <= 1 else 2 if max_age_days <= 7 else 3)
        d = api_post("/v1/wechat_search/search-b", keyword=keyword, business_type="article",
                     publish_time=window, cursor=cursor, raw=False).get("data") or {}
        posts = [P(it.get("docID") or it["doc_url"], it.get("title"), (it.get("source") or {}).get("title"),
                   None, it["doc_url"].replace("http://", "https://", 1))
                 for it in d.get("items") or [] if it.get("doc_url")]
        return posts, (d.get("cursor") if posts and str(d.get("continue_flag")) in ("1", "True", "true") else None)

    def comments_page(self, post, cursor=None):
        d = api_post("/v1/wechat_mp/comments", url=post["url"],
                     buffer=cursor or "", raw=False).get("data") or {}
        out = [C(c.get("content_id"), c.get("nick_name"), c.get("content"), c.get("identity_name"), "", "",
                 c.get("like_num"), c.get("reply_total"), (c.get("ip_wording") or {}).get("province_name"))
               for c in d.get("comments") or []]
        more = str(d.get("has_more")).lower() == "true" or d.get("has_more") is True
        return out, (d.get("buffer") if out and more else None)

    def parse_manual(self, text):
        m = re.search(r"https?://mp\.weixin\.qq\.com/s[^\s\"'<>]*", text)
        return P(m.group(0), url=m.group(0)) if m else None


class _ByteDanceComments:
    """Xigua and Toutiao share the same comment list format."""
    comment_path = ""
    id_param = ""

    def comments_page(self, post, cursor=None):
        params = {self.id_param: post["pid"], "offset": cursor or 0}
        if self.key == "xigua":
            params["count"] = 20
        d = get(self.comment_path, **params).get("data") or {}
        out = []
        for row in d.get("data") or []:
            c = row.get("comment") or row
            out.append(C(c.get("id_str") or c.get("id"), c.get("user_name"), c.get("text"), c.get("user_id"),
                         "", "", c.get("digg_count"), c.get("reply_count"), "", c.get("create_time")))
        return out, (d.get("offset") if out and d.get("has_more") else None)


class Xigua(_ByteDanceComments, Platform):
    key, label = "xigua", "西瓜视频"
    id_hint = "视频链接或视频ID（19位数字）"
    comment_path, id_param = "/v1/xigua/comments", "item_id"

    def search(self, keyword, cursor=None, max_age_days=0):
        d = get("/v1/xigua/search", keyword=keyword, offset=cursor or 0).get("data") or {}
        posts = []
        for r in d.get("results") or []:
            x = r.get("data") or {}
            if not x.get("item_id"):
                continue
            posts.append(P(x["item_id"], x.get("title"), x.get("source") or (x.get("user_info") or {}).get("name"),
                           None, f"https://www.ixigua.com/{x['item_id']}",
                           cover=pick_cover([((x.get("video_detail_info") or {}).get("detail_video_large_image") or {}).get("url")]),
                           published=x.get("publish_time")))
        return posts, (d.get("offset") if posts and d.get("has_more") else None)

    def parse_manual(self, text):
        pid = _digits(text, 17)
        return P(pid, url=f"https://www.ixigua.com/{pid}") if pid else None


class Toutiao(_ByteDanceComments, Platform):
    key, label = "toutiao", "今日头条"
    searchable = False
    hint = "头条没有关键词搜索接口，只能手动粘贴文章/视频链接"
    id_hint = "文章链接或文章ID（19位数字）"
    comment_path, id_param = "/v1/toutiao/comments", "group_id"

    def parse_manual(self, text):
        pid = _digits(text, 17)
        return P(pid, url=f"https://www.toutiao.com/article/{pid}/") if pid else None


class Pipixia(Platform):
    key, label = "pipixia", "皮皮虾"
    id_hint = "作品ID（19位数字）"

    def search(self, keyword, cursor=None, max_age_days=0):
        d = (get("/v1/pipixia/search", keyword=keyword, offset=cursor or 0,
                 search_type=1).get("data") or {}).get("data") or {}
        posts = []
        for cell in d.get("data") or []:
            it = cell.get("item")
            if cell.get("cell_type") != 1 or not it:
                continue
            pid = cell.get("cell_id_str") or it.get("item_id_str")
            posts.append(P(pid, it.get("content"), (it.get("author") or {}).get("name"),
                           (it.get("stats") or {}).get("comment_count"), f"https://h5.pipix.com/item/{pid}",
                           cover=pick_cover(((it.get("cover") or {}).get("url_list") or [])[:2]),
                           published=it.get("create_time")))
        off = (cursor or 0) + len(d.get("data") or [])
        return posts, (off if posts and d.get("has_more") else None)

    def comments_page(self, post, cursor=None):
        d = (get("/v1/pipixia/comments", cell_id=post["pid"], cell_type=1,
                 offset=cursor or 0).get("data") or {}).get("data") or {}
        out = []
        for cell in d.get("cell_comments") or []:
            c = cell.get("comment_info") or {}
            u = c.get("user") or {}
            out.append(C(c.get("comment_id_str"), u.get("name"), c.get("text"), u.get("id_str"), "", "",
                         c.get("like_count"), c.get("reply_count"), "", c.get("create_time")))
        return out, (d.get("offset") if out and d.get("has_more") else None)

    def parse_manual(self, text):
        pid = _digits(text, 17)
        return P(pid, url=f"https://h5.pipix.com/item/{pid}") if pid else None


PLATFORMS = {p.key: p for p in [Douyin(), Xiaohongshu(), Kuaishou(), WechatChannels(), Bilibili(), Weibo(),
                                Zhihu(), WechatMP(), Xigua(), Toutiao(), Pipixia()]}

LABELS = {k: p.label for k, p in PLATFORMS.items()}
