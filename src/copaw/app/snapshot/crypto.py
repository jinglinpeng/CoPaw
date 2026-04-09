# -*- coding: utf-8 -*-
"""AES-256-GCM encryption for snapshot export/import (password-protected packages)."""
from __future__ import annotations

import secrets
from typing import Final

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes

# Design: docs/v2/snapshot-design-v2.md §6.4
FORMAT_VERSION: Final[int] = 0x01
SALT_LEN: Final[int] = 16
NONCE_LEN: Final[int] = 12
KEY_LEN: Final[int] = 32
PBKDF2_ITERATIONS: Final[int] = 600_000

# User-facing unified message (GCM cannot distinguish wrong key vs tampering)
DECRYPT_FAILURE_MESSAGE: Final[str] = (
    "解密失败：密码错误或文件已损坏/被篡改"
)

MIN_ENCRYPTED_LEN: Final[int] = 1 + SALT_LEN + NONCE_LEN + 16  # + empty ciphertext tag


def is_encrypted_package(data: bytes) -> bool:
    """Return True if buffer looks like a CoPaw encrypted snapshot blob."""
    if len(data) < MIN_ENCRYPTED_LEN:
        return False
    return data[0] == FORMAT_VERSION


def encrypt_plaintext(plaintext: bytes, password: str) -> bytes:
    """Encrypt arbitrary bytes; output includes version, salt, nonce, ciphertext+tag."""
    if not password:
        raise ValueError("password must be non-empty for encryption")
    salt = secrets.token_bytes(SALT_LEN)
    nonce = secrets.token_bytes(NONCE_LEN)
    key = _derive_key(password, salt)
    aesgcm = AESGCM(key)
    ciphertext = aesgcm.encrypt(nonce, plaintext, None)
    return bytes([FORMAT_VERSION]) + salt + nonce + ciphertext


def decrypt_to_plaintext(blob: bytes, password: str) -> bytes:
    """Decrypt blob from :func:`encrypt_plaintext`. Raises ValueError on failure."""
    if len(blob) < MIN_ENCRYPTED_LEN:
        raise ValueError(DECRYPT_FAILURE_MESSAGE)
    if blob[0] != FORMAT_VERSION:
        raise ValueError(DECRYPT_FAILURE_MESSAGE)
    salt = blob[1 : 1 + SALT_LEN]
    nonce = blob[1 + SALT_LEN : 1 + SALT_LEN + NONCE_LEN]
    ct = blob[1 + SALT_LEN + NONCE_LEN :]
    if len(ct) < 16:
        raise ValueError(DECRYPT_FAILURE_MESSAGE)
    key = _derive_key(password, salt)
    aesgcm = AESGCM(key)
    try:
        return aesgcm.decrypt(nonce, ct, None)
    except InvalidTag:
        raise ValueError(DECRYPT_FAILURE_MESSAGE) from None


def _derive_key(password: str, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=KEY_LEN,
        salt=salt,
        iterations=PBKDF2_ITERATIONS,
    )
    return kdf.derive(password.encode("utf-8"))
