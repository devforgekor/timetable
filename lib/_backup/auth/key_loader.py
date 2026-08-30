#!/usr/bin/env python3
# Status: production
# Path: imported by — production scripts
"""Unified API key loader — loads encrypted or plaintext keys from secrets.env.

Used by: gemini_rotate.py, search_manager.py, proxies/gemini_openai.py.
"""
import os
import sys
from pathlib import Path

STATE_FILE = os.path.expanduser("~/.cache/devforge/gemini_rotator_state.json")


def load_api_keys(provider_prefix: str = "GEMINI") -> list:
    """Load API keys from secrets.env or env vars. Returns [(name, plaintext_key), ...].

    provider_prefix: env var prefix — "GEMINI" loads GEMINI_API_KEYS or GEMINI_API_KEY,
                     "BRAVE" loads BRAVE_API_KEY, etc.
    """
    secrets_path = os.path.expanduser("~/.config/devforge/secrets.env")
    keys_str = ""

    # 1. Try secrets.env (encrypted keys list)
    if os.path.exists(secrets_path):
        with open(secrets_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith(f"{provider_prefix}_API_KEYS="):
                    keys_str = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break

    # 2. Fallback to GEMINI_API_KEYS env var
    if not keys_str:
        keys_str = os.getenv(f"{provider_prefix}_API_KEYS", "")

    # 3. Fallback to single key
    if not keys_str:
        single = os.getenv(f"{provider_prefix}_API_KEY", "")
        if single:
            return [("default", single)]
        return []

    # Import here to avoid circular dependency — decrypt lives in same package
    from lib.auth.api_key_cipher import decrypt_data

    keys = []
    for item in keys_str.split(","):
        item = item.strip()
        if not item:
            continue
        if ":" in item:
            name, cipher = item.split(":", 1)
            name = name.strip()
            cipher = cipher.strip()
            plain = decrypt_data(cipher)
            if plain is None:
                print(f"경고: 키 복호화 실패 — {name} (평문으로 시도)", file=sys.stderr)
                plain = cipher
            keys.append((name, plain))
        else:
            cipher = item.strip()
            plain = decrypt_data(cipher)
            if plain is None:
                plain = cipher
            keys.append((f"key-{len(keys)}", plain))

    return keys

