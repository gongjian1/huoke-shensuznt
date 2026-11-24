"""给封面功能上线前采集的老作品补抓封面和发布时间（每个作品调一次详情接口，花费很小）。
用法：  python backfill.py            补全所有用户的老作品
        python backfill.py 20         最多处理 20 个（试跑）
"""
import sys
import time
from datetime import datetime

from api import ApiError, get
from database import get_db, init_db
from platforms import parse_weibo_time, pick_cover

# 这些平台的详情接口能给到可显示的封面；其余只补发布时间（或什么都补不了，直接跳过）
WITH_COVER = {"douyin", "xiaohongshu", "bilibili", "weibo"}
SUPPORTED = WITH_COVER | {"kuaishou"}


def _ts(v):
    return datetime.fromtimestamp(v // 1000 if v > 10**11 else v).strftime("%Y-%m-%d %H:%M:%S") if v else None


def fetch_meta(platform, nid):
    """返回 (cover, published_at)；拿不到的部分是空串 / None。"""
    if platform == "douyin":
        a = (get("/v1/douyin/detail", aweme_id=nid).get("data") or {}).get("aweme_detail") or {}
        v = a.get("video") or {}
        cover = pick_cover((v.get("cover") or {}).get("url_list")) or pick_cover((v.get("origin_cover") or {}).get("url_list"))
        return cover, _ts(a.get("create_time"))
    if platform == "xiaohongshu":
        d = (get("/v1/xiaohongshu/detail-a", note_id=nid).get("data") or {}).get("data") or []
        note = ((d[0].get("note_list") or [None])[0]) if d else None
        if note and (note.get("images_list") or note.get("time")):
            imgs = note.get("images_list") or []
            return (pick_cover([imgs[0].get("url") or imgs[0].get("url_size_large")]) if imgs else ""), _ts(note.get("time"))
        d = (get("/v1/xiaohongshu/detail-b", note_id=nid).get("data") or {}).get("data") or []
        n = d[0] if d else {}
        img = (n.get("video_info_v2") or {}).get("image") or {}
        return pick_cover([img.get("first_frame") or img.get("thumbnail")]), _ts(n.get("time"))
    if platform == "bilibili":
        d = (get("/v1/bilibili/detail", bv_id=nid).get("data") or {}).get("data") or {}
        return pick_cover([d.get("pic")]), _ts(d.get("pubdate"))
    if platform == "weibo":
        d = get("/v1/weibo/detail", id=nid).get("data") or {}
        ids, infos = d.get("pic_ids") or [], d.get("pic_infos") or {}
        first = infos.get(ids[0]) if ids else None
        cover = pick_cover([((first or {}).get("bmiddle") or (first or {}).get("large") or {}).get("url")]) if first else ""
        return cover, _ts(parse_weibo_time(d.get("created_at")))
    if platform == "kuaishou":
        p = ((get("/v1/kuaishou/detail", photo_id=nid).get("data") or {}).get("photos") or [{}])[0]
        return "", _ts(p.get("timestamp"))
    return "", None


def run(limit=0, pause=0.3):
    init_db()
    conn = get_db()
    rows = conn.execute("""
        SELECT id, platform, native_id FROM videos
        WHERE platform IN ({}) AND (published_at IS NULL OR (cover = '' AND platform IN ({})))
        ORDER BY last_scraped DESC
    """.format(",".join("?" * len(SUPPORTED)), ",".join("?" * len(WITH_COVER))), [*SUPPORTED, *WITH_COVER]).fetchall()
    if limit:
        rows = rows[:limit]
    done = covered = failed = 0
    for r in rows:
        try:
            cover, published = fetch_meta(r["platform"], r["native_id"])
        except (ApiError, KeyError, IndexError, TypeError, ValueError):
            failed += 1
            continue
        conn.execute("UPDATE videos SET cover=COALESCE(NULLIF(?, ''), cover), published_at=COALESCE(?, published_at) WHERE id=?",
                     (cover, published, r["id"]))
        conn.commit()
        done += 1
        covered += bool(cover)
        time.sleep(pause)
    conn.close()
    print(f"处理 {len(rows)} 个作品：成功 {done}，其中拿到封面 {covered}，失败 {failed}")


if __name__ == "__main__":
    run(int(sys.argv[1]) if len(sys.argv) > 1 else 0)
