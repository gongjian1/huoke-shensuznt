import json
import os
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

from database import get_setting

_ctx = ssl.create_default_context()


class ApiError(Exception):
    pass


def request(method, path, params=None, body=None, retries=3, timeout=45):
    key = (get_setting("api_key") or "").strip()
    if not key:
        raise ApiError("还没有配置数据接口 Key：请管理员在「管理后台 → 系统设置」里填写（获取方式见 README）")
    base = (get_setting("api_base") or "").strip().rstrip("/")
    if not base:
        raise ApiError("还没有配置数据接口地址：请管理员在「管理后台 → 系统设置」里填写（获取方式见 README）")
    url = base + path
    if params:
        url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    data = json.dumps(body).encode() if body is not None else None
    headers = {
        "Authorization": f"Bearer {key}",
        "User-Agent": "huoke-client/1.0",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    last = ""
    for attempt in range(retries):
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, context=_ctx, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:200]}"
            if e.code in (401, 402, 403, 404, 422):
                raise ApiError(last)
        except (urllib.error.URLError, OSError, ValueError) as e:
            last = str(e)
        time.sleep(1.5 * (attempt + 1))
    raise ApiError(last)


def get(path, **params):
    return request("GET", path, params=params)


def post(path, **body):
    return request("POST", path, body={k: v for k, v in body.items() if v is not None})


def balance_info():
    """(余额, 单位)。单位 comments = 条评论额度；余额为 None 且单位是 comments 表示不限额度；读取失败返回 (None, None)。"""
    try:
        d = get("/v1/account/info").get("user_data", {})
        return d.get("balance"), d.get("unit") or "comments"
    except ApiError:
        return None, None


def upstream_balance():
    """管理员用：网关主人在上游的真实余额（美元）。只能经本机网关的管理接口读到，读不到返回 None。"""
    token = os.environ.get("GATEWAY_ADMIN_TOKEN", "")
    if not token:
        return None
    base = os.environ.get("GATEWAY_URL", "http://127.0.0.1:5002").rstrip("/")
    try:
        req = urllib.request.Request(base + "/_admin/upstream", headers={"X-Admin-Token": token})
        with urllib.request.urlopen(req, timeout=15) as r:
            v = json.loads(r.read().decode("utf-8")).get("balance")
        return float(v) if v is not None else None
    except (urllib.error.URLError, OSError, ValueError):
        return None


def balance():
    """采集任务前后记录的上游余额（美元），用来算每个任务的接口成本；没有网关管理权限就是 None。"""
    return upstream_balance()
