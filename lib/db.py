#!/usr/bin/env python3
# Status: production
# Path: imported by — production scripts
"""Shared PostgreSQL helpers for DevForge scripts.

Supports two modes:
1. psycopg2 direct connection (Vercel Postgres, Neon, etc.) - when POSTGRES_URL or DATABASE_URL is set
2. psql subprocess (local development with podman) - fallback

Usage:
    from lib.db import psql, psql_ok, esc_sql, db_table_exists, db_row_exists

    rows = psql("SELECT * FROM turns LIMIT 5")
    ok = psql_ok("INSERT INTO ...")
    safe = esc_sql("O'Reilly\nquote")
    has_turns = db_table_exists("turns")
    has_pgvector = db_row_exists("SELECT 1 FROM pg_extension WHERE extname='vector'")
"""

import os
import json
import subprocess
from typing import Optional, Any

# Database connection mode detection
# Vercel Postgres는 POSTGRES_URL 외에 POSTGRES_CONNECTION_STRING 형식으로도 노출됨
_db_url = (
    os.environ.get("POSTGRES_URL")
    or os.environ.get("DATABASE_URL")
    or os.environ.get("POSTGRES_CONNECTION_STRING")
)
_use_psycopg = bool(_db_url)

if _use_psycopg:
    # psycopg2 mode (Vercel, Neon, etc.)
    import psycopg2
    import psycopg2.extras

    def _get_conn():
        """Get psycopg2 connection."""
        return psycopg2.connect(_db_url)

    def psql(sql: str, timeout: int = 30) -> str:
        """Execute SQL via psycopg2, return stripped result. Empty on error."""
        try:
            conn = _get_conn()
            try:
                with conn.cursor() as cur:
                    cur.execute(sql)
                    if cur.description:
                        result = cur.fetchall()
                        # Pipe-delimited output for backward compatibility
                        if result and len(result[0]) == 1:
                            return str(result[0][0])
                        return "|".join(str(row[0]) for row in result)
                    return ""
            finally:
                conn.close()
        except Exception as e:
            print(f"  SQL ERROR: {e}")
            return ""

    def psql_json(sql: str, timeout: int = 30) -> list[dict]:
        """Execute SQL via psycopg2, return list of dicts."""
        try:
            conn = _get_conn()
            try:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute(sql)
                    if cur.description:
                        return [dict(row) for row in cur.fetchall()]
                    return []
            finally:
                conn.close()
        except Exception as e:
            print(f"  SQL ERROR: {e}")
            return []

    def psql_ok(sql: str, timeout: int = 30) -> bool:
        """Execute SQL via psycopg2, return True if succeeded."""
        try:
            conn = _get_conn()
            try:
                with conn.cursor() as cur:
                    cur.execute(sql)
                    conn.commit()
                    return True
            finally:
                conn.close()
        except Exception as e:
            print(f"  SQL ERROR: {e}")
            return False

    def db_table_exists(table: str) -> bool:
        """Check if table exists."""
        try:
            conn = _get_conn()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT 1 FROM pg_tables WHERE tablename = %s",
                        (table,)
                    )
                    return cur.fetchone() is not None
            finally:
                conn.close()
        except Exception:
            return False

    def db_row_exists(sql: str) -> bool:
        """Check if query returns any rows."""
        try:
            conn = _get_conn()
            try:
                with conn.cursor() as cur:
                    cur.execute(sql)
                    return cur.fetchone() is not None
            finally:
                conn.close()
        except Exception:
            return False

else:
    # psql subprocess mode (local development)
    if os.environ.get("DEVFORGE_DB_TCP"):
        PSQL = [
            "psql",
            "-h", "127.0.0.1",
            "-U", "postgres",
            "-d", "devforge_app",
            "--no-align",
            "--tuples-only",
            "--quiet",
        ]
        PSQL_CHECK = ["psql", "-h", "127.0.0.1", "-U", "postgres", "-d", "devforge_app", "-t"]
    else:
        PSQL = [
            "podman", "exec", "-i", "postgres", "psql",
            "-U", "postgres",
            "-d", "devforge_app",
            "--no-align",
            "--tuples-only",
            "--quiet",
        ]
        PSQL_CHECK = [
            "podman", "exec", "postgres", "psql",
            "-U", "postgres",
            "-d", "devforge_app",
            "-t",
        ]

    def _stdin_sql(sql: str) -> str:
        """Prefix SQL so backslash escapes in literals work as esc_sql expects."""
        return f"SET standard_conforming_strings = off;\n{sql}"

    def psql(sql: str, timeout: int = 30) -> str:
        """Execute SQL via stdin (-f -), return stripped stdout. Empty on error."""
        try:
            r = subprocess.run(
                PSQL + ["-f", "-"], input=_stdin_sql(sql), capture_output=True, text=True, timeout=timeout
            )
            if r.returncode != 0:
                print(f"  SQL ERROR: {r.stderr.strip()[:200]}")
            return r.stdout.strip() if r.returncode == 0 else ""
        except Exception as e:
            print(f"  SQL ERROR: {e}")
            return ""

    def psql_json(sql: str, timeout: int = 30) -> list[dict]:
        """Execute SQL with row_to_json wrapping, return list of dicts."""
        wrapped = f"SELECT row_to_json(r) FROM ({sql}) r"
        try:
            r = subprocess.run(
                PSQL + ["-f", "-"], input=_stdin_sql(wrapped), capture_output=True, text=True, timeout=timeout
            )
            if r.returncode != 0:
                print(f"  SQL ERROR: {r.stderr.strip()[:200]}")
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
            print(f"  SQL ERROR: {e}")
            return []

    def psql_ok(sql: str, timeout: int = 30) -> bool:
        """Execute SQL via stdin (-f -), return True if statement succeeded."""
        try:
            r = subprocess.run(
                PSQL + ["-f", "-"], input=_stdin_sql(sql), capture_output=True, text=True, timeout=timeout
            )
            if r.returncode != 0:
                print(f"  SQL ERROR: {r.stderr.strip()[:200]}")
            return r.returncode == 0
        except Exception as e:
            print(f"  SQL ERROR: {e}")
            return False

    def db_table_exists(table: str) -> bool:
        try:
            r = subprocess.run(
                PSQL_CHECK + ["-c", f"SELECT 1 FROM pg_tables WHERE tablename='{esc_sql(table)}'"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            return "1" in r.stdout
        except Exception:
            return False

    def db_row_exists(sql: str) -> bool:
        try:
            r = subprocess.run(
                PSQL_CHECK + ["-c", sql],
                capture_output=True,
                text=True,
                timeout=10,
            )
            return "1" in r.stdout
        except Exception:
            return False


def escape_sql_string(s: str) -> str:
    """Escape string for safe SQL literal interpolation."""
    return (
        s.replace("\x00", "")
        .replace("\\", "\\\\")
        .replace("'", "''")
        .replace("\n", " ")
        .replace("\r", " ")
    )


esc_sql = escape_sql_string
