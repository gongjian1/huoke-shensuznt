"""用户填写的第三方 API Key 的加密保存（Fernet 对称加密）。

主密钥优先取环境变量 HUOKE_SECRET（和数据库分开放，只拿到数据库文件或备份是解不开的），
没有设置时退回用库里的 secret_key。库里已有的明文值（不带 enc1: 前缀）也能正常读出。
"""
import base64
import hashlib
import os

from cryptography.fernet import Fernet, InvalidToken

from database import get_setting

PREFIX = "enc1:"


def _fernet():
    material = os.environ.get("HUOKE_SECRET") or get_setting("secret_key")
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(("huoke-vault|" + material).encode()).digest()))


def encrypt(plain):
    return PREFIX + _fernet().encrypt(plain.encode()).decode() if plain else ""


def decrypt(stored):
    if not stored:
        return ""
    if not stored.startswith(PREFIX):
        return stored
    try:
        return _fernet().decrypt(stored[len(PREFIX):].encode()).decode()
    except InvalidToken:
        return ""


def mask(plain):
    return (plain[:6] + "•" * 10 + plain[-4:]) if len(plain) > 12 else ("•" * len(plain))
