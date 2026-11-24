import sqlite3
from config import DB_PATH, DEFAULT_SETTINGS

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    phone           TEXT UNIQUE NOT NULL,
    nickname        TEXT,
    password_hash   TEXT NOT NULL DEFAULT '',
    is_admin        INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT '正常',
    balance         INTEGER NOT NULL DEFAULT 0,
    total_spent     INTEGER NOT NULL DEFAULT 0,
    total_recharged INTEGER NOT NULL DEFAULT 0,
    note            TEXT,
    created_at      TEXT DEFAULT (datetime('now','localtime')),
    last_login      TEXT
);

CREATE TABLE IF NOT EXISTS points_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL,
    delta         INTEGER NOT NULL,
    balance_after INTEGER NOT NULL,
    kind          TEXT NOT NULL,
    note          TEXT,
    task_id       INTEGER,
    operator_id   INTEGER,
    created_at    TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS tasks (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id          INTEGER NOT NULL DEFAULT 1,
    points_cost       INTEGER DEFAULT 0,
    mode              TEXT DEFAULT 'search',
    project           TEXT,
    keywords          TEXT,
    platforms         TEXT,
    posts_per_keyword INTEGER DEFAULT 5,
    comments_per_post INTEGER DEFAULT 50,
    min_comments      INTEGER DEFAULT 3,
    extra_intent      TEXT,
    status            TEXT DEFAULT '排队中',
    progress          TEXT,
    posts             INTEGER DEFAULT 0,
    comments          INTEGER DEFAULT 0,
    leads_a           INTEGER DEFAULT 0,
    leads_b           INTEGER DEFAULT 0,
    errors            INTEGER DEFAULT 0,
    balance_before    REAL,
    balance_after     REAL,
    created_at        TEXT DEFAULT (datetime('now','localtime')),
    finished_at       TEXT
);

CREATE TABLE IF NOT EXISTS videos (
    id            TEXT PRIMARY KEY,
    owner_id      INTEGER NOT NULL DEFAULT 1,
    platform      TEXT NOT NULL,
    native_id     TEXT NOT NULL,
    title         TEXT,
    author        TEXT,
    url           TEXT,
    comment_count INTEGER,
    keyword       TEXT,
    project       TEXT,
    task_id       INTEGER,
    collected_at  TEXT DEFAULT (datetime('now','localtime')),
    last_scraped  TEXT
);

CREATE TABLE IF NOT EXISTS comments (
    id           TEXT PRIMARY KEY,
    owner_id     INTEGER NOT NULL DEFAULT 1,
    video_id     TEXT NOT NULL,
    platform     TEXT NOT NULL,
    nickname     TEXT,
    user_id      TEXT,
    user_handle  TEXT,
    user_link    TEXT,
    text         TEXT,
    digg_count   INTEGER DEFAULT 0,
    reply_count  INTEGER DEFAULT 0,
    ip_region    TEXT,
    collected_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS leads (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id    INTEGER NOT NULL DEFAULT 1,
    comment_id  TEXT UNIQUE NOT NULL,
    video_id    TEXT,
    platform    TEXT,
    project     TEXT,
    keyword     TEXT,
    nickname    TEXT,
    user_id     TEXT,
    user_handle TEXT,
    user_link   TEXT,
    ip_region   TEXT,
    text        TEXT,
    tier        TEXT,
    keywords    TEXT,
    status      TEXT DEFAULT '未联系',
    wechat      TEXT,
    phone       TEXT,
    notes       TEXT,
    created_at  TEXT DEFAULT (datetime('now','localtime')),
    updated_at  TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS accounts (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id       INTEGER NOT NULL,
    platform       TEXT NOT NULL,
    account_id     TEXT NOT NULL,
    nickname       TEXT,
    handle         TEXT,
    avatar         TEXT,
    profile_url    TEXT,
    bio            TEXT,
    followers      INTEGER,
    followers_prev INTEGER,
    posts_count    INTEGER,
    note           TEXT,
    interval_hours INTEGER NOT NULL DEFAULT 12,
    active         INTEGER NOT NULL DEFAULT 1,
    last_checked   TEXT,
    next_check     TEXT,
    last_error     TEXT,
    points_cost    INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT DEFAULT (datetime('now','localtime')),
    UNIQUE (owner_id, platform, account_id)
);

CREATE TABLE IF NOT EXISTS account_posts (
    id           TEXT PRIMARY KEY,
    account_ref  INTEGER NOT NULL,
    owner_id     INTEGER NOT NULL,
    platform     TEXT NOT NULL,
    native_id    TEXT NOT NULL,
    title        TEXT,
    url          TEXT,
    cover        TEXT,
    published_at TEXT,
    pinned       INTEGER DEFAULT 0,
    plays        INTEGER,
    likes        INTEGER,
    comments     INTEGER,
    shares       INTEGER,
    collects     INTEGER,
    d_plays      INTEGER DEFAULT 0,
    d_likes      INTEGER DEFAULT 0,
    d_comments   INTEGER DEFAULT 0,
    is_new       INTEGER DEFAULT 0,
    first_seen   TEXT DEFAULT (datetime('now','localtime')),
    last_updated TEXT
);

CREATE TABLE IF NOT EXISTS post_snapshots (
    post_ref TEXT NOT NULL,
    at       TEXT DEFAULT (datetime('now','localtime')),
    plays    INTEGER,
    likes    INTEGER,
    comments INTEGER,
    shares   INTEGER
);

CREATE TABLE IF NOT EXISTS creations (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id       INTEGER NOT NULL,
    source_kind    TEXT,
    source_ref     TEXT,
    source_platform TEXT,
    source_author  TEXT,
    source_title   TEXT,
    source_cover   TEXT,
    source_url     TEXT,
    style          TEXT DEFAULT 'xhs',
    product_info   TEXT,
    extra          TEXT,
    image_count    INTEGER DEFAULT 0,
    ratio          TEXT DEFAULT '3:4',
    status         TEXT DEFAULT '生成中',
    progress       TEXT,
    error          TEXT,
    title          TEXT,
    body           TEXT,
    tags           TEXT,
    image_prompts  TEXT,
    images         TEXT,
    points_cost    INTEGER DEFAULT 0,
    created_at     TEXT DEFAULT (datetime('now','localtime')),
    finished_at    TEXT
);
CREATE TABLE IF NOT EXISTS ai_videos (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id     INTEGER NOT NULL,
    creation_id  INTEGER,
    mode         TEXT DEFAULT 't2v',
    prompt       TEXT,
    first_frame  TEXT DEFAULT '',
    resolution   TEXT DEFAULT '720P',
    ratio        TEXT DEFAULT '9:16',
    duration     INTEGER DEFAULT 5,
    model        TEXT,
    task_id      TEXT,
    status       TEXT DEFAULT '生成中',
    progress     TEXT,
    error        TEXT,
    video_file   TEXT DEFAULT '',
    size_bytes   INTEGER DEFAULT 0,
    created_at   TEXT DEFAULT (datetime('now','localtime')),
    finished_at  TEXT
);
CREATE TABLE IF NOT EXISTS recharge_orders (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL,
    amount       INTEGER NOT NULL,          -- 付款金额（分）
    bonus        INTEGER DEFAULT 0,         -- 套餐赠送（分）
    method       TEXT DEFAULT '',           -- 微信 / 支付宝
    payer_note   TEXT DEFAULT '',           -- 用户填的付款备注（昵称、尾号等）
    status       TEXT DEFAULT '待确认',      -- 待确认 / 已到账 / 已拒绝
    reject_note  TEXT DEFAULT '',
    created_at   TEXT DEFAULT (datetime('now','localtime')),
    handled_at   TEXT,
    handled_by   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_recharge_user ON recharge_orders(user_id, id);
CREATE INDEX IF NOT EXISTS idx_recharge_status ON recharge_orders(status, id);
CREATE INDEX IF NOT EXISTS idx_aivideos_owner ON ai_videos(owner_id, id);
CREATE INDEX IF NOT EXISTS idx_creations_owner ON creations(owner_id, id);
CREATE INDEX IF NOT EXISTS idx_accounts_owner ON accounts(owner_id);
CREATE INDEX IF NOT EXISTS idx_accounts_due ON accounts(active, next_check);
CREATE INDEX IF NOT EXISTS idx_aposts_account ON account_posts(account_ref);
CREATE INDEX IF NOT EXISTS idx_aposts_owner ON account_posts(owner_id, published_at);
CREATE INDEX IF NOT EXISTS idx_snap_post ON post_snapshots(post_ref, at);
CREATE INDEX IF NOT EXISTS idx_leads_tier ON leads(tier);
CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(status);
CREATE INDEX IF NOT EXISTS idx_leads_project ON leads(project);
CREATE INDEX IF NOT EXISTS idx_comments_video ON comments(video_id);
CREATE INDEX IF NOT EXISTS idx_videos_project ON videos(project);
CREATE INDEX IF NOT EXISTS idx_points_user ON points_log(user_id);
"""

# Indexes on columns added by _migrate_v2 must be created after it runs.
POST_SCHEMA = """
CREATE INDEX IF NOT EXISTS idx_tasks_owner ON tasks(owner_id);
CREATE INDEX IF NOT EXISTS idx_videos_owner ON videos(owner_id);
CREATE INDEX IF NOT EXISTS idx_comments_owner ON comments(owner_id);
CREATE INDEX IF NOT EXISTS idx_leads_owner ON leads(owner_id);
"""


def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _columns(conn, table):
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def _migrate_v1(conn):
    """Version 1 stored Douyin-only rows with bare IDs; prefix them with the platform."""
    conn.executescript("""
        ALTER TABLE videos   RENAME TO videos_v1;
        ALTER TABLE comments RENAME TO comments_v1;
        ALTER TABLE leads    RENAME TO leads_v1;
        DROP INDEX IF EXISTS idx_leads_tier;
        DROP INDEX IF EXISTS idx_leads_status;
        DROP INDEX IF EXISTS idx_comments_video;
    """)
    conn.executescript(SCHEMA)
    conn.executescript("""
        INSERT OR IGNORE INTO videos (id, platform, native_id, title, author, url, comment_count,
                                      keyword, project, collected_at, last_scraped)
        SELECT 'douyin:' || id, 'douyin', id, title, author,
               'https://www.douyin.com/video/' || id, comment_count, keyword, '充电桩',
               collected_at, last_scraped
        FROM videos_v1;

        INSERT OR IGNORE INTO comments (id, video_id, platform, nickname, user_id, user_handle,
                                        user_link, text, digg_count, reply_count, collected_at)
        SELECT 'douyin:' || id, 'douyin:' || video_id, 'douyin', nickname, uid, unique_id,
               CASE WHEN sec_uid != '' THEN 'https://www.douyin.com/user/' || sec_uid END,
               text, digg_count, reply_count, collected_at
        FROM comments_v1;

        INSERT OR IGNORE INTO leads (comment_id, video_id, platform, project, keyword, nickname,
                                     user_id, user_handle, user_link, text, tier, keywords,
                                     status, wechat, phone, notes, created_at, updated_at)
        SELECT 'douyin:' || l.comment_id, 'douyin:' || l.video_id, 'douyin', '充电桩', v.keyword,
               l.nickname, l.uid, l.unique_id,
               CASE WHEN c.sec_uid != '' THEN 'https://www.douyin.com/user/' || c.sec_uid END,
               l.text, l.tier, l.keywords, l.status, l.wechat, l.phone, l.notes,
               l.created_at, l.updated_at
        FROM leads_v1 l
        LEFT JOIN comments_v1 c ON c.id = l.comment_id
        LEFT JOIN videos_v1 v ON v.id = l.video_id;

        DROP TABLE leads_v1;
        DROP TABLE comments_v1;
        DROP TABLE videos_v1;
        DROP TABLE IF EXISTS collect_log;
    """)


def _migrate_v2(conn):
    """Single-user data becomes the admin's (user 1); row IDs get an owner prefix so users never collide."""
    legacy = conn.execute("SELECT value FROM settings WHERE key='login_password_hash'").fetchone()
    conn.execute("INSERT OR IGNORE INTO users (id, phone, nickname, password_hash, is_admin, status) "
                 "VALUES (1, 'admin', '管理员', ?, 1, '正常')", (legacy["value"] if legacy else "",))
    for table in ("tasks", "videos", "comments", "leads"):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN owner_id INTEGER NOT NULL DEFAULT 1")
    conn.execute("ALTER TABLE tasks ADD COLUMN points_cost INTEGER DEFAULT 0")
    conn.executescript("""
        UPDATE videos   SET id = '1:' || id;
        UPDATE comments SET id = '1:' || id, video_id = '1:' || video_id;
        UPDATE leads    SET comment_id = '1:' || comment_id, video_id = '1:' || video_id;
    """)


LATER_COLUMNS = {
    "users": {"ai_api_key": "TEXT DEFAULT ''", "ai_region": "TEXT DEFAULT 'cn'", "ai_text_model": "TEXT DEFAULT ''",
              "ai_image_model": "TEXT DEFAULT ''", "product_info": "TEXT DEFAULT ''",
              "intent_strong": "TEXT", "intent_medium": "TEXT", "intent_exclude": "TEXT"},   # NULL = 用系统默认意向词
    "tasks": {"fetch_replies": "INTEGER DEFAULT 1", "platform_log": "TEXT",
              "max_age_days": "INTEGER DEFAULT 0", "regions": "TEXT DEFAULT ''", "keep_unknown_region": "INTEGER DEFAULT 1",
              "skip_seen": "INTEGER DEFAULT 1"},
    "recharge_orders": {"credits": "INTEGER NOT NULL DEFAULT 0"},   # 这笔付款换来的评论额度（条）
    "videos": {"cover": "TEXT DEFAULT ''", "published_at": "TEXT"},
    "comments": {"is_reply": "INTEGER DEFAULT 0", "province": "TEXT DEFAULT ''", "commented_at": "TEXT"},
    "leads": {"is_reply": "INTEGER DEFAULT 0", "province": "TEXT DEFAULT ''", "commented_at": "TEXT",
              "dm_public": "TEXT DEFAULT ''", "dm_private": "TEXT DEFAULT ''", "dm_at": "TEXT"},
}


def _migrate_comment_units(conn):
    """积分（单位：分）→ 评论额度（单位：条）。只执行一次。

    按旧的「普通平台每 50 条评论 price_post 积分」折算：1 积分 = 50 / price_post 条，
    这样已经充值过的用户不会吃亏。余额、累计充值 / 消耗、流水、任务和账号的花费都按同一个系数转换。
    """
    if conn.execute("SELECT 1 FROM settings WHERE key='billing_unit'").fetchone():
        return
    row = conn.execute("SELECT value FROM settings WHERE key='price_post'").fetchone()
    try:
        price = float(row[0]) if row else 0.3
    except ValueError:
        price = 0.3
    per_point = 50 / price if price > 0 else 50 / 0.3     # 1 积分 = 多少条评论
    f = per_point / 100                                   # 库里存的是「分」
    for table, cols in (("users", ("balance", "total_spent", "total_recharged")),
                        ("points_log", ("delta", "balance_after")),
                        ("tasks", ("points_cost",)), ("accounts", ("points_cost",))):
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
            continue
        for col in cols:
            conn.execute(f"UPDATE {table} SET {col} = CAST(ROUND({col} * ?) AS INTEGER)", (f,))
    conn.execute("UPDATE recharge_orders SET credits = CAST(ROUND((amount + bonus) * ?) AS INTEGER) WHERE credits = 0", (f,))
    track = conn.execute("SELECT value FROM settings WHERE key='price_track'").fetchone()
    if track is not None:
        conn.execute("UPDATE settings SET value=? WHERE key='price_track'", ("20" if track[0] == "0.1" else str(int(round(float(track[0] or 0) * per_point))),))
    bonus = conn.execute("SELECT value FROM settings WHERE key='register_bonus'").fetchone()
    if bonus is not None:
        conn.execute("UPDATE settings SET value=? WHERE key='register_bonus'", (str(int(round(float(bonus[0] or 0) * per_point))),))
    pk = conn.execute("SELECT value FROM settings WHERE key='recharge_packages'").fetchone()
    if pk is not None and pk[0].strip() == "50 0\n200 20\n500 80\n1000 200":     # 还是旧的默认套餐 → 换成新的
        conn.execute("UPDATE settings SET value=? WHERE key='recharge_packages'", (DEFAULT_SETTINGS["recharge_packages"],))
    conn.execute("DELETE FROM settings WHERE key IN ('price_post', 'price_post_wechat')")
    conn.execute("INSERT INTO settings (key, value) VALUES ('billing_unit', 'comments')")


def _backfill_provinces(conn):
    """Existing comments/leads only have the raw ip_region text; derive the province once."""
    from regions import normalize_region
    cache = {}
    for table in ("comments", "leads"):
        for row in conn.execute(f"SELECT DISTINCT ip_region FROM {table} WHERE ip_region IS NOT NULL AND ip_region != ''").fetchall():
            raw = row[0]
            cache[raw] = normalize_region(raw)
            conn.execute(f"UPDATE {table} SET province=? WHERE ip_region=?", (cache[raw], raw))


def init_db():
    conn = get_db()
    tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "videos" in tables and "native_id" not in _columns(conn, "videos"):
        _migrate_v1(conn)
    else:
        conn.executescript(SCHEMA)
    if "owner_id" not in _columns(conn, "tasks"):
        _migrate_v2(conn)
    conn.executescript(POST_SCHEMA)
    added = set()
    for table, cols in LATER_COLUMNS.items():
        have = _columns(conn, table)
        for col, decl in cols.items():
            if col not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
                added.add((table, col))
    if ("comments", "province") in added:
        _backfill_provinces(conn)
    conn.executescript("""
        CREATE INDEX IF NOT EXISTS idx_leads_province ON leads(owner_id, province);
        CREATE INDEX IF NOT EXISTS idx_leads_commented ON leads(owner_id, commented_at);
        CREATE INDEX IF NOT EXISTS idx_videos_published ON videos(owner_id, published_at);
    """)
    if "account_posts" in {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}:
        # Kuaishou's KVIF/KPG covers cannot be shown by browsers; empty them so pages use the text poster.
        conn.execute("UPDATE account_posts SET cover='' WHERE cover LIKE '%.kvif%' OR cover LIKE '%.kpg%' OR cover LIKE '%.heic%'")
    if not conn.execute("SELECT 1 FROM users WHERE is_admin=1").fetchone():
        conn.execute("INSERT OR IGNORE INTO users (phone, nickname, is_admin, status) VALUES ('admin', '管理员', 1, '正常')")
    _migrate_comment_units(conn)
    for k, v in DEFAULT_SETTINGS.items():
        conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))
    conn.execute("UPDATE creations SET status='失败', error='服务重启，生成被中断，没有扣额度请重试' WHERE status='生成中'")
    # A task left "running" means the app was closed mid-run.
    conn.execute("UPDATE tasks SET status='已中断' WHERE status IN ('进行中','排队中','停止中')")
    conn.commit()
    conn.close()


def get_setting(key):
    conn = get_db()
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else DEFAULT_SETTINGS.get(key, "")


def set_setting(key, value):
    conn = get_db()
    conn.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    conn.commit()
    conn.close()


def word_list(text):
    out = []
    for line in (text or "").replace("，", "\n").replace(",", "\n").splitlines():
        w = line.strip()
        if w and w not in out:
            out.append(w)
    return out


if __name__ == "__main__":
    init_db()
    print("Database ready:", DB_PATH)
