# Общая логика enc:v1 для llm_token (используется globals и скрипты ротации пароля БД).
from __future__ import annotations

import base64
import hashlib
import hmac
import os

ENC_PREFIX = "enc:v1:"


def token_key_from_secret(secret: str) -> bytes | None:
    if not secret or not str(secret).strip():
        return None
    return hashlib.sha256(str(secret).strip().encode("utf-8")).digest()


def _keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < length:
        block = hmac.new(key, nonce + counter.to_bytes(8, "big"), hashlib.sha256).digest()
        out.extend(block)
        counter += 1
    return bytes(out[:length])


def is_encrypted_token(value: str | None) -> bool:
    return isinstance(value, str) and value.startswith(ENC_PREFIX)


def decrypt_token_with_secret(value: str | None, secret: str) -> str | None:
    if value is None or value == "":
        return value
    if not is_encrypted_token(value):
        return value

    key = token_key_from_secret(secret)
    if not key:
        return None

    raw = value[len(ENC_PREFIX) :]
    padding = "=" * (-len(raw) % 4)
    blob = base64.urlsafe_b64decode(raw + padding)
    if len(blob) < 32:
        raise ValueError("Encrypted token payload is too short")

    nonce = blob[:16]
    tag = blob[16:32]
    cipher = blob[32:]
    expected = hmac.new(key, ENC_PREFIX.encode("ascii") + nonce + cipher, hashlib.sha256).digest()[:16]
    if not hmac.compare_digest(tag, expected):
        raise ValueError("Encrypted token integrity check failed")
    plain = bytes([a ^ b for a, b in zip(cipher, _keystream(key, nonce, len(cipher)))])
    return plain.decode("utf-8")


def encrypt_token_with_secret(value: str | None, secret: str) -> str | None:
    """Шифрует открытый текст. Уже зашифрованное enc:v1 значение возвращает без изменений (как в globals.encrypt_token)."""
    if value is None or value == "":
        return value
    if is_encrypted_token(value):
        return value

    key = token_key_from_secret(secret)
    if not key:
        return value

    plain = value.encode("utf-8")
    nonce = os.urandom(16)
    cipher = bytes([a ^ b for a, b in zip(plain, _keystream(key, nonce, len(plain)))])
    tag = hmac.new(key, ENC_PREFIX.encode("ascii") + nonce + cipher, hashlib.sha256).digest()[:16]
    packed = base64.urlsafe_b64encode(nonce + tag + cipher).decode("ascii").rstrip("=")
    return ENC_PREFIX + packed
