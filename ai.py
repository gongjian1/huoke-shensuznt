"""阿里云百炼（DashScope）封装：文案用通义千问，配图用千问 / 万相文生图。

用的是**每个客户自己的阿里云 Key**（加密保存在用户账户里，见 vault.py），费用由阿里云直接向客户收取，
我们不加价、不扣评论额度。后台「系统设置」里只配默认模型名，客户没单独选模型时用默认的。
图片链接是临时的（几小时到几天就失效），所以生成后立刻下载到本机 media/ 目录。
"""
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass

import vault
from config import BASE_DIR
from database import get_db, get_setting

MEDIA_DIR = os.path.join(BASE_DIR, "media")
BASES = {"cn": "https://dashscope.aliyuncs.com", "intl": "https://dashscope-intl.aliyuncs.com"}
REGION_LABEL = {"cn": "国内站（北京，aliyun.com 注册的账号）", "intl": "国际站（新加坡，alibabacloud.com 注册的账号）"}
SIZES = {"3:4": "1104*1472", "1:1": "1280*1280", "16:9": "1664*928"}
RATIO_LABEL = {"3:4": "竖版 3:4", "1:1": "方图 1:1", "16:9": "横版 16:9"}
MAX_IMAGES = 4
IMG_MAX_BYTES = 15_000_000
VIDEO_MAX_BYTES = 150_000_000
VIDEO_RESOLUTIONS = ("720P", "1080P")
VIDEO_RATIOS = {"9:16": "竖屏 9:16", "16:9": "横屏 16:9", "1:1": "方形 1:1", "3:4": "竖版 3:4", "4:3": "横版 4:3"}
VIDEO_DURATIONS = (2, 3, 5, 8, 10, 15)
# 官方价（国内站，美元/秒）× 约 7.2 折成人民币，仅用于页面上的费用估算，实际以阿里云账单为准
VIDEO_CNY_PER_SEC = {"720P": 0.62, "1080P": 1.03}
MAX_STORED_VIDEOS = 30
VIDEO_POLL_SECONDS = 8
VIDEO_TIMEOUT_SECONDS = 900
_video_gate = threading.Semaphore(3)
MODEL_NAME = re.compile(r"[A-Za-z0-9._\-]{1,64}")
_gate = threading.Semaphore(3)  # 同时最多 3 个创作在跑，保护服务器

STYLES = {
    "xhs": ("小红书图文笔记",
            "标题 20 字以内；正文 300~500 字，分 4~6 小段，开头用一个大家常遇到的场景或痛点切入（用\"很多车主都会遇到……\"的口吻，不要虚构某个具体的人或某一天发生的事），中间给可操作的方法和注意点，结尾自然引导互动；"
            "适量使用表情符号；话题标签 5~8 个（不带 # 号）。"),
    "douyin": ("抖音口播文案",
               "标题 18 字以内；正文是一段 30~60 秒的口播脚本（200~300 字），开头 3 秒抛出钩子，全部口语化短句，每句不超过 20 个字；"
               "话题标签 4~6 个（不带 # 号）。"),
    "moments": ("朋友圈文案",
                "正文不超过 150 字，像朋友聊天，从一个大家常遇到的场景切入（不要虚构具体的人和事），不要硬广；标题用 10 字以内概括；话题标签返回空数组。"),
}


class AIError(Exception):
    pass


# 编造出来的亲身经历 / 具体数字，一旦出现就让 AI 重写一次
_FAKE = re.compile(r"\d+\s*%|\d+\s*成的|昨天|上周|前几天|有个朋友|有位|一位客户|一位车主|一个客户|邻居")


@dataclass
class Cred:
    key: str
    region: str = "cn"
    text_model: str = "qwen-plus"
    image_model: str = "qwen-image-2.0"


def user_cred(user):
    """用户行（sqlite3.Row）→ 他自己的阿里云凭据；没填 Key 返回 None。"""
    key = vault.decrypt(user["ai_api_key"] or "")
    if not key:
        return None
    return Cred(key=key, region=user["ai_region"] if user["ai_region"] in BASES else "cn",
                text_model=user["ai_text_model"] or get_setting("ai_text_model") or "qwen-plus",
                image_model=user["ai_image_model"] or get_setting("ai_image_model") or "qwen-image-2.0")


def _request(cred, path, body=None, method="POST", timeout=120, retries=4, extra_headers=None):
    headers = {"Authorization": f"Bearer {cred.key}", "Content-Type": "application/json", "User-Agent": "huoke/1.0"}
    headers.update(extra_headers or {})
    data = json.dumps(body).encode() if body is not None else None
    last = ""
    for attempt in range(retries):
        req = urllib.request.Request(BASES.get(cred.region, BASES["cn"]) + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            try:
                j = json.loads(raw)
                msg = (j.get("error") or {}).get("message") if isinstance(j.get("error"), dict) else None
                msg = msg or j.get("message") or raw
            except ValueError:
                msg = raw
            last = f"HTTP {e.code}: {str(msg)[:200]}"
            if e.code == 401:
                raise AIError("你的阿里云 API Key 无效，请到「我的账户」里检查（国内站和国际站的 Key 不能混用）")
            if e.code == 403:
                if "free" in str(msg).lower() or "quota" in str(msg).lower():
                    raise AIError("你的阿里云免费额度用完了：请在阿里云百炼控制台充值，或关闭「仅使用免费额度」")
                raise AIError("你的阿里云账号没有权限使用这个模型，请到阿里云百炼控制台开通")
            if e.code == 400:
                raise AIError(f"阿里云拒绝了这次请求：{str(msg)[:160]}")
            if e.code not in (429, 500, 502, 503, 504):
                raise AIError(last)
        except (urllib.error.URLError, OSError, ValueError) as e:
            last = str(e)[:200]
        time.sleep(2 * (attempt + 1))
    raise AIError("阿里云接口暂时繁忙，请稍后再试" + (f"（{last}）" if last else ""))


def chat_json(cred, system, user, max_tokens=1800, temperature=0.7):
    model = cred.text_model
    body = {"model": model, "temperature": temperature, "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    if model.startswith("qwen3"):
        body["enable_thinking"] = False
    d = _request(cred, "/compatible-mode/v1/chat/completions", body)
    text = (((d.get("choices") or [{}])[0]).get("message") or {}).get("content") or ""
    try:
        return json.loads(text)
    except ValueError:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except ValueError:
                pass
    raise AIError("AI 返回的内容格式不对，请重试一次")


def generate_image(cred, prompt, size):
    d = _request(cred, "/api/v1/services/aigc/multimodal-generation/generation", {
        "model": cred.image_model, "input": {"messages": [{"role": "user", "content": [{"text": prompt}]}]},
        "parameters": {"size": size, "n": 1, "watermark": False, "prompt_extend": True}}, timeout=180)
    for c in ((((d.get("output") or {}).get("choices") or [{}])[0].get("message") or {}).get("content") or []):
        if c.get("image"):
            return c["image"]
    raise AIError("没有生成出图片")


def download_image(url):
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    if not (urllib.parse.urlparse(url).scheme == "https" and host.endswith("aliyuncs.com")):
        raise AIError("图片地址不在允许的范围内")
    req = urllib.request.Request(url, headers={"User-Agent": "huoke/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip()
            data = resp.read(IMG_MAX_BYTES + 1)
    except (urllib.error.URLError, OSError) as e:
        raise AIError(f"下载生成的图片失败：{str(e)[:80]}")
    ext = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}.get(ctype)
    if not ext or len(data) > IMG_MAX_BYTES:
        raise AIError("生成的图片格式不对")
    return data, ext


# ---------------- 仿写 ----------------

def build_prompt(c, comments):
    style_name, style_rules = STYLES.get(c["style"], STYLES["xhs"])
    n = c["image_count"] or 0
    liked = "\n".join(f"- {t[:60]}" for t in comments[:8])
    ref = f"平台：{c['source_platform'] or '未知'}　账号：{c['source_author'] or '未知'}\n标题 / 文案：{c['source_title']}"
    if liked:
        ref += f"\n评论区里用户最关心的问题：\n{liked}"
    image_rule = (f"配图提示词共 {n} 条，每条是对一张图片画面的中文描述（场景、主体、构图、光线、色调），整组风格统一；"
                  "画面里不要出现文字、水印、Logo，不要人脸特写。") if n else "不需要配图，image_prompts 返回空数组。"
    system = ("你是资深的新媒体内容运营，擅长拆解爆款内容，再用全新的表达写出原创内容。"
              "你写的东西真实、口语化、有细节，符合平台调性，从不夸大宣传。")
    user = f"""请参考下面这条对标内容的"选题角度、结构节奏和语气"，为我的业务写一篇全新的原创{style_name}。

【对标内容】
{ref}

【我的业务 / 产品】
{c['product_info'] or '未提供。请写通用内容，不要编造具体的品牌、价格、地址、联系方式。'}

【补充要求】
{c['extra'] or '无'}

写作要求：
1. 只借鉴结构和角度，不要照抄原文的句子，不要出现对方的账号名。
2. {style_rules}
3. 禁止：绝对化用语（最、第一、100%、保证）、虚假承诺、医疗功效、联系方式、"点击链接"之类的硬广引流。
4. 严禁编造事实：
   - 不要出现任何百分比、倍数、人数、年限等具体数字（"90%的人""3 年经验"这类都不行），除非【我的业务 / 产品】里明确给了；
   - 不要写"昨天/上周/有个朋友/一位客户"这类虚构的亲身经历和案例；
   - 不要杜撰具体小区、地址、政策和文件名称、资质荣誉、价格；
   - 需要举例时用"比如""常见的情况是"这样的通用说法，讲道理和方法，不讲故事。
5. {image_rule}

只输出 JSON，不要输出其他任何文字：
{{"title": "标题", "body": "正文", "tags": ["标签1", "标签2"], "image_prompts": ["画面描述1"]}}"""
    return system, user


def _save_progress(conn, cid, **fields):
    sets = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE creations SET {sets} WHERE id=?", (*fields.values(), cid))
    conn.commit()


def run_creation(cid):
    with _gate:
        conn = get_db()
        try:
            _run(conn, cid)
        except AIError as e:
            _save_progress(conn, cid, status="失败", error=str(e)[:300], finished_at=time.strftime("%Y-%m-%d %H:%M:%S"))
        except Exception as e:  # never leave a creation stuck in 生成中
            _save_progress(conn, cid, status="失败", error=f"出错了：{str(e)[:200]}", finished_at=time.strftime("%Y-%m-%d %H:%M:%S"))
        finally:
            conn.close()


def _run(conn, cid):
    c = conn.execute("SELECT * FROM creations WHERE id=?", (cid,)).fetchone()
    owner = conn.execute("SELECT * FROM users WHERE id=?", (c["owner_id"],)).fetchone()
    cred = user_cred(owner)
    if not cred:
        raise AIError("还没有填写你的阿里云 API Key，请到「我的账户」里设置")
    comments = []
    if c["source_kind"] == "video" and c["source_ref"]:
        comments = [r[0] for r in conn.execute(
            "SELECT text FROM comments WHERE video_id=? AND owner_id=? AND length(text) >= 6 ORDER BY digg_count DESC LIMIT 8",
            (c["source_ref"], c["owner_id"]))]

    _save_progress(conn, cid, progress="AI 正在写文案…")
    system, user = build_prompt(c, comments)
    out = chat_json(cred, system, user)
    if _FAKE.search(json.dumps(out, ensure_ascii=False)):
        retry_note = "注意：上一版里出现了虚构的亲身经历或具体数字。请完全重写，只讲道理和方法，不要出现任何编造的人、事、数字。"
        out = chat_json(cred, system, user + "\n\n" + retry_note)
    title = str(out.get("title") or "").strip()[:80]
    body = str(out.get("body") or "").strip()
    tags = [str(t).strip().lstrip("#") for t in (out.get("tags") or []) if str(t).strip()][:10]
    prompts = [str(p).strip() for p in (out.get("image_prompts") or []) if str(p).strip()][: c["image_count"] or 0]
    if not (title or body):
        raise AIError("AI 没有写出内容，请重试一次")
    _save_progress(conn, cid, title=title, body=body, tags=json.dumps(tags, ensure_ascii=False),
                   image_prompts=json.dumps(prompts, ensure_ascii=False),
                   progress=f"文案写好了，开始生成配图 0/{len(prompts)}" if prompts else "")

    images, failed, last_err = [], 0, ""
    os.makedirs(MEDIA_DIR, exist_ok=True)
    for i, p in enumerate(prompts, 1):
        _save_progress(conn, cid, progress=f"正在生成配图 {i}/{len(prompts)}…")
        try:
            url = generate_image(cred, f"{p}。高清，画面干净，真实自然，画面中不要出现任何文字、水印和标志。", SIZES.get(c["ratio"], SIZES["3:4"]))
            data, ext = download_image(url)
        except AIError as e:
            failed += 1
            last_err = str(e)
            continue
        name = f"{cid}-{i}-{uuid.uuid4().hex[:8]}.{ext}"
        with open(os.path.join(MEDIA_DIR, name), "wb") as f:
            f.write(data)
        images.append(name)
        _save_progress(conn, cid, images=json.dumps(images))
    note = f"有 {failed} 张配图没生成出来：{last_err}" if failed else ""
    _save_progress(conn, cid, status="完成", progress="", error=note, finished_at=time.strftime("%Y-%m-%d %H:%M:%S"))


def start_creation(creation_id):
    threading.Thread(target=run_creation, args=(creation_id,), daemon=True, name=f"creation-{creation_id}").start()


# ---------------- AI 视频（万相 wan2.7，异步任务） ----------------

_VPATH = "/api/v1/services/aigc/video-generation/video-synthesis"
_FRIENDLY_VIDEO_ERRORS = (
    ("DataInspection", "内容没有通过阿里云的安全审核，请换一种描述再试"),
    ("inappropriate", "内容没有通过阿里云的安全审核，请换一种描述再试"),
    ("Arrearage", "你的阿里云账号欠费了，请先充值"),
    ("Throttling", "阿里云当前请求太多，请稍后再试"),
)


def video_family():
    return get_setting("ai_video_model") or "wan2.7"


def estimate_video_cny(duration, resolution):
    return round(VIDEO_CNY_PER_SEC.get(resolution, 1.03) * duration, 1)


def submit_video(cred, prompt, resolution, ratio, duration, first_frame=None):
    """first_frame = (bytes, mime) 时走图生视频（比例跟着首帧图走），否则文生视频。返回 (task_id, 模型名)。"""
    family = video_family()
    params = {"resolution": resolution, "duration": duration, "prompt_extend": True, "watermark": False}
    if first_frame:
        import base64
        model = f"{family}-i2v"
        media = [{"type": "first_frame", "url": f"data:{first_frame[1]};base64,{base64.b64encode(first_frame[0]).decode()}"}]
        body = {"model": model, "input": {"prompt": prompt, "media": media}, "parameters": params}
    else:
        model = f"{family}-t2v"
        params["ratio"] = ratio
        body = {"model": model, "input": {"prompt": prompt}, "parameters": params}
    d = _request(cred, _VPATH, body, timeout=120, extra_headers={"X-DashScope-Async": "enable"})
    task_id = (d.get("output") or {}).get("task_id")
    if not task_id:
        raise AIError("阿里云没有返回任务编号，请重试")
    return task_id, model


def poll_video(cred, task_id):
    """返回 (状态, 视频地址, 失败原因)。状态：PENDING / RUNNING / SUCCEEDED / FAILED。"""
    d = _request(cred, f"/api/v1/tasks/{task_id}", method="GET", timeout=40)
    o = d.get("output") or {}
    status = o.get("task_status") or "UNKNOWN"
    if status in ("CANCELED", "UNKNOWN"):
        status = "FAILED"
    msg = f"{o.get('code') or ''} {o.get('message') or ''}".strip()
    for needle, friendly in _FRIENDLY_VIDEO_ERRORS:
        if needle.lower() in msg.lower():
            msg = friendly
            break
    return status, o.get("video_url"), msg or "视频生成失败"


def download_video(url):
    p = urllib.parse.urlparse(url)
    if not (p.scheme == "https" and (p.hostname or "").lower().endswith("aliyuncs.com")):
        raise AIError("视频地址不在允许的范围内")
    req = urllib.request.Request(url, headers={"User-Agent": "huoke/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            data = resp.read(VIDEO_MAX_BYTES + 1)
    except (urllib.error.URLError, OSError) as e:
        raise AIError(f"下载生成的视频失败：{str(e)[:80]}")
    if len(data) > VIDEO_MAX_BYTES or len(data) < 1000:
        raise AIError("生成的视频文件不正常")
    return data


def _vsave(conn, vid, **fields):
    sets = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE ai_videos SET {sets} WHERE id=?", (*fields.values(), vid))
    conn.commit()


def _read_first_frame(row):
    """首帧图必须是这个用户自己某条创作里的配图。"""
    name = row["first_frame"]
    if not name:
        return None
    m = re.match(r"^(\d+)-\d+-[0-9a-f]{8}\.(png|jpg|webp)$", name)
    conn = get_db()
    ok = bool(m) and conn.execute("SELECT 1 FROM creations WHERE id=? AND owner_id=?", (int(m.group(1)), row["owner_id"])).fetchone()
    conn.close()
    if not ok:
        raise AIError("首帧图片不存在")
    path = os.path.join(MEDIA_DIR, name)
    if not os.path.exists(path):
        raise AIError("首帧图片文件已经被删除了")
    with open(path, "rb") as f:
        return f.read(), {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp"}[m.group(2)]


def run_video(vid):
    with _video_gate:
        conn = get_db()
        try:
            _run_video(conn, vid)
        except AIError as e:
            _vsave(conn, vid, status="失败", error=str(e)[:300], progress="", finished_at=time.strftime("%Y-%m-%d %H:%M:%S"))
        except Exception as e:
            _vsave(conn, vid, status="失败", error=f"出错了：{str(e)[:200]}", progress="", finished_at=time.strftime("%Y-%m-%d %H:%M:%S"))
        finally:
            conn.close()


def _run_video(conn, vid):
    row = conn.execute("SELECT * FROM ai_videos WHERE id=?", (vid,)).fetchone()
    owner = conn.execute("SELECT * FROM users WHERE id=?", (row["owner_id"],)).fetchone()
    cred = user_cred(owner)
    if not cred:
        raise AIError("还没有填写你的阿里云 API Key，请到「我的账户」里设置")
    task_id = row["task_id"]
    if not task_id:  # 重启后接着轮询的任务已经有编号，不能再提交一次
        _vsave(conn, vid, progress="正在提交给阿里云…")
        task_id, model = submit_video(cred, row["prompt"], row["resolution"], row["ratio"], row["duration"], _read_first_frame(row))
        _vsave(conn, vid, task_id=task_id, model=model, progress="已提交，排队生成中…")
    started = time.time()
    while time.time() - started < VIDEO_TIMEOUT_SECONDS:
        time.sleep(VIDEO_POLL_SECONDS)
        status, url, msg = poll_video(cred, task_id)
        if status == "FAILED":
            raise AIError(msg)
        if status == "SUCCEEDED":
            _vsave(conn, vid, progress="生成完成，正在保存视频…")
            data = download_video(url)
            os.makedirs(MEDIA_DIR, exist_ok=True)
            name = f"v{vid}-{uuid.uuid4().hex[:8]}.mp4"
            with open(os.path.join(MEDIA_DIR, name), "wb") as f:
                f.write(data)
            _vsave(conn, vid, status="完成", video_file=name, size_bytes=len(data), progress="", error="",
                   finished_at=time.strftime("%Y-%m-%d %H:%M:%S"))
            return
        _vsave(conn, vid, progress="AI 正在生成视频，一般 30 秒到 2 分钟…" if status == "RUNNING" else "排队中…")
    raise AIError("等了太久阿里云还没出结果，这条视频没有生成成功，请重试")


def start_video(vid):
    threading.Thread(target=run_video, args=(vid,), daemon=True, name=f"video-{vid}").start()


def resume_videos():
    """服务重启时：已经提交给阿里云的任务（任务编号 24 小时内有效）接着轮询；还没提交的标记失败。"""
    conn = get_db()
    try:
        conn.execute("UPDATE ai_videos SET status='失败', error='服务重启，任务没来得及提交，请重试', progress='' "
                     "WHERE status='生成中' AND (task_id IS NULL OR task_id='')")
        conn.commit()
        for r in conn.execute("SELECT id FROM ai_videos WHERE status='生成中' AND task_id != ''").fetchall():
            start_video(r[0])
    finally:
        conn.close()
