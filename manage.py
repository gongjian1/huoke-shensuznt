"""
命令行工具（管理员账号的登录名是 admin）
  python manage.py set-admin-password      交互式设置管理员密码
  python manage.py set-admin-hash HASH     直接写入管理员密码哈希（部署时用，明文密码不经过服务器）
"""
import getpass
import sys

from werkzeug.security import generate_password_hash

from database import get_db, init_db


def _set_admin_hash(h):
    conn = get_db()
    conn.execute("UPDATE users SET password_hash=? WHERE id=(SELECT MIN(id) FROM users WHERE is_admin=1)", (h,))
    conn.commit()
    conn.close()
    print("管理员密码已设置（登录名 admin）")


init_db()
cmd = sys.argv[1] if len(sys.argv) > 1 else ""
if cmd == "set-admin-password":
    pw = getpass.getpass("新密码: ")
    if len(pw) < 8 or pw != getpass.getpass("再输一次: "):
        sys.exit("两次不一致或少于 8 位，没有修改")
    _set_admin_hash(generate_password_hash(pw))
elif cmd == "set-admin-hash" and len(sys.argv) == 3:
    _set_admin_hash(sys.argv[2])
else:
    print(__doc__)
