"""每日备份 SQLite 数据库，保留最近 14 份。用 SQLite 自带的在线备份，服务运行中也能安全执行。
服务器上由 cron 每天 03:30 执行：  30 3 * * *  cd /opt/huoke && /opt/huoke/venv/bin/python backup_db.py
"""
import glob
import os
import sqlite3
import time

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "leads.db")
DEST = os.path.join(BASE, "backup")
KEEP = 14


def main():
    os.makedirs(DEST, exist_ok=True)
    path = os.path.join(DEST, f"leads-daily-{time.strftime('%Y%m%d')}.db")
    src, dst = sqlite3.connect(DB, timeout=60), sqlite3.connect(path)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    old = sorted(glob.glob(os.path.join(DEST, "leads-daily-*.db")))[:-KEEP]
    for f in old:
        os.remove(f)
    print(f"备份完成：{path}（{os.path.getsize(path) // 1024} KB），保留 {min(KEEP, len(old) + KEEP)} 份以内")


if __name__ == "__main__":
    main()
