#!/usr/bin/env python3
# Status: production
# Path: imported by — production scripts
"""
AES-256-GCM encryption for API keys and secrets.
PBKDF2-HMAC-SHA256 key derivation from ENCRYPTION_PASSPHRASE.

Usage:
    from lib.auth.api_key_cipher import encrypt_data, decrypt_data
    cipher = encrypt_data("AIza...")
    plain = decrypt_data(cipher)  # or None on failure
"""

import base64
import hashlib
import os
from typing import Optional

_ENCRYPTION_KEY: Optional[bytes] = None


def _get_encryption_key() -> bytes:
    global _ENCRYPTION_KEY
    if _ENCRYPTION_KEY is not None:
        return _ENCRYPTION_KEY

    passphrase = os.getenv("ENCRYPTION_PASSPHRASE", "")
    if not passphrase:
        raise RuntimeError("ENCRYPTION_PASSPHRASE not set")

    salt = b"devforge-aes-256-gcm"  # fixed per-project salt
    key = hashlib.pbkdf2_hmac("sha256", passphrase.encode(), salt, 100_000, dklen=32)
    _ENCRYPTION_KEY = key
    return key


def encrypt_data(plaintext: str) -> str:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    key = _get_encryption_key()
    aesgcm = AESGCM(key)
    nonce = os.urandom(12)
    ciphertext = aesgcm.encrypt(nonce, plaintext.encode(), None)
    return base64.b64encode(nonce + ciphertext).decode()


def decrypt_data(cipher_b64: str) -> Optional[str]:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    try:
        key = _get_encryption_key()
        raw = base64.b64decode(cipher_b64)
        nonce = raw[:12]
        ciphertext = raw[12:]
        aesgcm = AESGCM(key)
        plaintext = aesgcm.decrypt(nonce, ciphertext, None)
        return plaintext.decode()
    except Exception:
        return None

