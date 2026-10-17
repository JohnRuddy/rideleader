"""Encryption of OAuth tokens at rest (Fernet: AES-128-CBC + HMAC).

TOKEN_ENCRYPTION_KEY may hold several comma-separated keys: the first encrypts, all decrypt, so a key can be
rotated without logging every athlete out. In development a key is derived from SECRET_KEY; production
refuses to start without a real one (see create_app).
"""
import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from flask import current_app


def generate_key() -> str:
    return Fernet.generate_key().decode()


def _keys() -> list[bytes]:
    raw = (current_app.config.get("TOKEN_ENCRYPTION_KEY") or "").strip()
    if raw:
        return [k.strip().encode() for k in raw.split(",") if k.strip()]
    derived = base64.urlsafe_b64encode(hashlib.sha256(("rlk-token-key:" + current_app.config["SECRET_KEY"]).encode()).digest())
    return [derived]


def _fernet() -> MultiFernet:
    return MultiFernet([Fernet(k) for k in _keys()])


def encrypt(text: str) -> str:
    return _fernet().encrypt(text.encode()).decode()


class DecryptionError(RuntimeError):
    pass


def decrypt(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as e:
        raise DecryptionError("stored token can't be decrypted (was the key changed?)") from e
