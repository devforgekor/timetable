#!/usr/bin/env python3
# Status: production
# Path: imported by — cli.py, notice/telegram_bot.py, pipelines/extract.py
"""file_registry.py — DevForge File Management System.

CRUD for file_registry table + local upload management.
All LLM/Agent entry points use this single library.

Flow:
  register_file(path, source) -> UUID  (store metadata in DB)
  search_files(query) -> list           (description/tags ILIKE search)
  update_metadata(id, desc, tags)       (LLM-generated description)
  receive_telegram_file(file_id) -> UUID (getFile -> save -> register)
"""

import hashlib
import json
import os
import subprocess
import uuid as _uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# --- Config ---
UPLOADS_DIR = Path("/opt/ai_data/uploads")
DB_PSQL = ["podman", "exec", "-i", "postgres", "psql", "-U", "postgres",
           "-d", "devforge_app", "--no-align", "--tuples-only", "--quiet"]


def _psql(sql: str, timeout: int = 30) -> str:
    try:
        r = subprocess.run(DB_PSQL + ["-c", sql], capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            print(f"  [file_registry] SQL ERROR: {r.stderr.strip()[:200]}", flush=True)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception as e:
        print(f"  [file_registry] SQL ERROR: {e}", flush=True)
        return ""


def _psql_json(sql: str, timeout: int = 30) -> List[Dict[str, Any]]:
    wrapped = f"SELECT row_to_json(r) FROM ({sql}) r"
    try:
        r = subprocess.run(DB_PSQL + ["-c", wrapped], capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            print(f"  [file_registry] SQL ERROR: {r.stderr.strip()[:200]}", flush=True)
            return []
        raw = r.stdout.strip()
        if not raw:
            return []
        result = []
        for line in raw.split("\n"):
            line = line.strip()
            if line:
                result.append(json.loads(line))
        return result
    except Exception as e:
        print(f"  [file_registry] SQL ERROR: {e}", flush=True)
        return []


def _psql_ok(sql: str, timeout: int = 30) -> bool:
    """Execute SQL, return True if no error (for UPDATE/DELETE without RETURNING)."""
    try:
        r = subprocess.run(DB_PSQL + ["-c", sql], capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            print(f"  [file_registry] SQL ERROR: {r.stderr.strip()[:200]}", flush=True)
        return r.returncode == 0
    except Exception as e:
        print(f"  [file_registry] SQL ERROR: {e}", flush=True)
        return False


def _hash_file(path: Path) -> str:
    """SHA256 of file contents (chunked for large files)."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _detect_mime(path: Path) -> str:
    """Best-effort MIME detection. Falls back to application/octet-stream."""
    ext = path.suffix.lower()
    mime_map = {
        ".txt": "text/plain",
        ".md": "text/markdown",
        ".py": "text/x-python",
        ".json": "application/json",
        ".yaml": "application/x-yaml",
        ".yml": "application/x-yaml",
        ".csv": "text/csv",
        ".log": "text/plain",
        ".html": "text/html",
        ".css": "text/css",
        ".js": "application/javascript",
        ".sh": "application/x-sh",
        ".toml": "application/toml",
        ".pdf": "application/pdf",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".svg": "image/svg+xml",
        ".zip": "application/zip",
        ".gz": "application/gzip",
        ".tar": "application/x-tar",
        ".gguf": "application/octet-stream",
    }
    return mime_map.get(ext, "application/octet-stream")


def _exists_by_hash(file_hash: str) -> bool:
    """Check if a file with this SHA256 already exists in registry."""
    result = _psql(f"SELECT 1 FROM file_registry WHERE hash = '{file_hash}' LIMIT 1")
    return result == "1"


# ═══════════════════════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════════════════════

def register_file(
    src_path: str,
    source: str,
    filename: Optional[str] = None,
    description: Optional[str] = None,
    tags: Optional[List[str]] = None,
    blob_url: Optional[str] = None,
    sender: Optional[str] = None,
    turn_id: Optional[str] = None,
) -> Optional[str]:
    """Copy file to uploads/ and register in DB.

    Args:
        src_path: Path to source file.
        source: 'telegram_upload', 'pipeline_output', or 'agent_generate'.
        filename: Display name (defaults to source file basename).
        description: Human-readable description (LLM fills later if None).
        tags: Keyword list for search.
        blob_url: Azure Blob SAS URL if already uploaded.
        sender: Who sent the file (Telegram user ID, etc.).
        turn_id: Associated turn UUID.

    Returns:
        UUID string of the registered file, or None on error.
    """
    src = Path(src_path)
    if not src.exists():
        print(f"  [file_registry] File not found: {src_path}", flush=True)
        return None

    file_hash = _hash_file(src)

    # Dedup check
    if _exists_by_hash(file_hash):
        print(f"  [file_registry] File already registered (hash match): {src.name}", flush=True)
        return None

    display_name = (filename or src.name).replace("'", "''")
    mime_type = _detect_mime(src)
    file_size = src.stat().st_size
    date_prefix = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    dest_dir = UPLOADS_DIR / date_prefix
    dest_dir.mkdir(parents=True, exist_ok=True)

    # Copy to destination with UUID name
    dest_path = dest_dir / f"{_uuid.uuid4().hex}{src.suffix}"
    import shutil
    shutil.copy2(str(src), str(dest_path))

    # SQL-escape all string values
    src_safe = source.replace("'", "''")
    desc_safe = description.replace("'", "''") if description else None
    blob_safe = blob_url.replace("'", "''") if blob_url else None
    sender_safe = sender.replace("'", "''") if sender else None
    turn_safe = turn_id.replace("'", "''") if turn_id else None

    tags_arr = "ARRAY[" + ",".join(f"'{t.strip().replace(chr(39), chr(39)+chr(39))}'" for t in (tags or []) if t.strip()) + "]" if tags else "'{}'"
    desc_val = f"'{desc_safe}'" if desc_safe else "NULL"
    blob_val = f"'{blob_safe}'" if blob_safe else "NULL"
    sender_val = f"'{sender_safe}'" if sender_safe else "NULL"
    turn_val = f"'{turn_safe}'" if turn_safe else "NULL"
    mime_val = f"'{mime_type}'"

    result = _psql(
        f"INSERT INTO file_registry "
        f"(filename, path, size, hash, mime_type, source, description, tags, blob_url, sender, turn_id) "
        f"VALUES ('{display_name}', '{dest_path}', {file_size}, '{file_hash}', "
        f"{mime_val}, '{src_safe}', {desc_val}, {tags_arr}, {blob_val}, {sender_val}, {turn_val}) "
        f"RETURNING id"
    )
    if result:
        fid = result.strip()
        print(f"  [file_registry] Registered: {display_name} (id={fid})", flush=True)
        return fid
    return None


def search_files(query: str, limit: int = 20) -> List[Dict[str, Any]]:
    """Search file_registry by description, filename, and tags (ILIKE + trigram).

    Args:
        query: Free-text search keyword.
        limit: Max results.

    Returns:
        List of {id, filename, description, tags, source, size, created_at}.
    """
    safe = query.replace("'", "''")
    results = _psql_json(
        f"SELECT id, filename, description, tags, source, size, "
        f"mime_type, sender, created_at, blob_url, path "
        f"FROM file_registry "
        f"WHERE description ILIKE '%{safe}%' "
        f"OR filename ILIKE '%{safe}%' "
        f"OR EXISTS (SELECT 1 FROM unnest(tags) t WHERE t ILIKE '%{safe}%') "
        f"ORDER BY created_at DESC LIMIT {limit}"
    )
    return results


def list_files(source: Optional[str] = None, limit: int = 20) -> List[Dict[str, Any]]:
    """List recent files, optionally filtered by source."""
    safe = source.replace("'", "''") if source else None
    where = f"WHERE source = '{safe}'" if safe else ""
    return _psql_json(
        f"SELECT id, filename, description, tags, source, size, "
        f"mime_type, sender, created_at, blob_url "
        f"FROM file_registry {where} ORDER BY created_at DESC LIMIT {limit}"
    )


def get_file(file_id: str) -> Optional[Dict[str, Any]]:
    """Get single file record by UUID."""
    results = _psql_json(
        f"SELECT id, filename, path, description, tags, source, size, "
        f"mime_type, sender, created_at, blob_url "
        f"FROM file_registry WHERE id = '{file_id}' LIMIT 1"
    )
    return results[0] if results else None


def update_metadata(file_id: str, description: str, tags: Optional[List[str]] = None) -> bool:
    """Update description and tags (called by extract phase after LLM summary)."""
    desc_safe = description.replace("'", "''")
    tags_arr = "ARRAY[" + ",".join(f"'{t.strip().replace(chr(39), chr(39)+chr(39))}'" for t in (tags or []) if t.strip()) + "]" if tags else "'{}'"
    ok = _psql_ok(
        f"UPDATE file_registry SET description = '{desc_safe}', tags = {tags_arr} "
        f"WHERE id = '{file_id}'"
    )
    if ok:
        print(f"  [file_registry] Updated metadata: {file_id}", flush=True)
    return ok


def set_blob_url(file_id: str, blob_url: str) -> bool:
    """Set Azure Blob URL after upload."""
    safe = blob_url.replace("'", "''")
    return _psql_ok(
        f"UPDATE file_registry SET blob_url = '{safe}' WHERE id = '{file_id}'"
    )


def delete_file(file_id: str, remove_local: bool = False) -> bool:
    """Remove DB record, optionally also delete local file."""
    if remove_local:
        rec = get_file(file_id)
        if rec and rec.get("path"):
            p = Path(rec["path"])
            if p.exists():
                p.unlink()
                print(f"  [file_registry] Deleted local file: {p}", flush=True)
    ok = _psql_ok(f"DELETE FROM file_registry WHERE id = '{file_id}'")
    if ok:
        print(f"  [file_registry] Deleted record: {file_id}", flush=True)
    return ok


def scan_undescribed() -> List[Dict[str, Any]]:
    """Find files with NULL description — for extract phase to describe."""
    return _psql_json(
        "SELECT id, filename, path, size, mime_type, source, created_at "
        "FROM file_registry WHERE description IS NULL OR description = '' "
        "ORDER BY created_at ASC LIMIT 20"
    )


def receive_telegram_file(file_id: str, sender: str = "", filename: str = "") -> Optional[str]:
    """Download file from Telegram, save to uploads/, register in DB.

    Args:
        file_id: Telegram file_id from message.document or message.photo.
        sender: Telegram user ID.
        filename: Original filename from Telegram.

    Returns:
        UUID string of registered file, or None on failure.
    """
    secrets_path = Path.home() / ".config/devforge/secrets.env"
    token = ""
    if secrets_path.exists():
        for line in secrets_path.read_text().split("\n"):
            line = line.strip()
            if line.startswith("TELEGRAM_TOKEN="):
                token = line.split("=", 1)[1].strip().strip('"').strip("'")
                break
    if not token:
        print("  [file_registry] TELEGRAM_TOKEN not found", flush=True)
        return None

    import urllib.request as _ur

    # Step 1: getFile to get file path
    get_url = f"https://api.telegram.org/bot{token}/getFile"
    req_data = json.dumps({"file_id": file_id}).encode()
    try:
        req = _ur.Request(get_url, data=req_data, headers={"Content-Type": "application/json"})
        with _ur.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read())
            if not result.get("ok"):
                print(f"  [file_registry] getFile failed: {result.get('description', '?')}", flush=True)
                return None
            tg_path = result["result"]["file_path"]
    except Exception as e:
        print(f"  [file_registry] getFile error: {e}", flush=True)
        return None

    # Step 2: download file
    dl_url = f"https://api.telegram.org/file/bot{token}/{tg_path}"
    date_prefix = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    dest_dir = UPLOADS_DIR / date_prefix
    dest_dir.mkdir(parents=True, exist_ok=True)

    suffix = Path(tg_path).suffix or ""
    dest_file = dest_dir / f"{_uuid.uuid4().hex}{suffix}"

    try:
        with _ur.urlopen(dl_url, timeout=60) as resp:
            data = resp.read()
        dest_file.write_bytes(data)
        print(f"  [file_registry] Downloaded: {dest_file} ({len(data)} bytes)", flush=True)
    except Exception as e:
        print(f"  [file_registry] Download error: {e}", flush=True)
        return None

    # Step 3: register
    return register_file(
        src_path=str(dest_file),
        source="telegram_upload",
        filename=filename or tg_path.split("/")[-1],
        sender=sender,
    )
