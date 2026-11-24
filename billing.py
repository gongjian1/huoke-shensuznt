"""评论额度计费。

额度的单位就是「条评论」：用户余额 = 还能抓多少条评论。库里一律存整数。
采集时只按**新抓到**的评论条数扣（重复的评论不扣）；微信类平台每条评论按 wechat_weight 条额度算，
因为数据接口对它们收费更高。管理员自己采集不扣。
"""
from database import get_setting


class OutOfPoints(Exception):
    """评论额度用完（沿用旧名字，各处都在用）。"""


def to_units(value):
    """把表单里填的数转成整数条数；填得不对抛 ValueError。"""
    return int(round(float(str(value).replace(",", "").strip())))


def fmt(units):
    """12345 → '12,345'"""
    return f"{int(units or 0):,}"


def _setting_int(key, default):
    try:
        return max(0, to_units(get_setting(key) or default))
    except ValueError:
        return default


def unit_cost(platform_key):
    """这个平台每抓 1 条评论，扣几条额度（普通平台 1，微信类 wechat_weight）。"""
    return _setting_int("wechat_weight", 2) if platform_key.startswith("wechat") else 1


def comments_per_yuan():
    """自定义金额充值时，1 元换多少条评论额度。"""
    return max(1, _setting_int("comments_per_yuan", 100))


def adjust(conn, user_id, delta, kind, note="", operator_id=None, task_id=None):
    """改用户的额度并写一条流水，返回新余额。"""
    recharged = delta if kind == "充值" and delta > 0 else 0
    conn.execute("UPDATE users SET balance = balance + ?, total_recharged = total_recharged + ? WHERE id = ?",
                 (delta, recharged, user_id))
    bal = conn.execute("SELECT balance FROM users WHERE id=?", (user_id,)).fetchone()[0]
    conn.execute("INSERT INTO points_log (user_id, delta, balance_after, kind, note, task_id, operator_id) "
                 "VALUES (?, ?, ?, ?, ?, ?, ?)", (user_id, delta, bal, kind, note, task_id, operator_id))
    return bal


def charge_task(conn, user_id, task_id, units):
    """给一个任务扣额度；同一个任务的多次扣费合并成一行流水。"""
    conn.execute("UPDATE users SET balance = balance - ?, total_spent = total_spent + ? WHERE id = ?",
                 (units, units, user_id))
    bal = conn.execute("SELECT balance FROM users WHERE id=?", (user_id,)).fetchone()[0]
    row = conn.execute("SELECT id FROM points_log WHERE task_id=? AND kind='扣费'", (task_id,)).fetchone()
    if row:
        conn.execute("UPDATE points_log SET delta = delta - ?, balance_after = ? WHERE id = ?", (units, bal, row[0]))
    else:
        conn.execute("INSERT INTO points_log (user_id, delta, balance_after, kind, note, task_id) "
                     "VALUES (?, ?, ?, '扣费', ?, ?)", (user_id, -units, bal, f"采集任务 #{task_id}", task_id))
    conn.execute("UPDATE tasks SET points_cost = points_cost + ? WHERE id = ?", (units, task_id))
    conn.commit()
    return bal
