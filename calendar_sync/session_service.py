#!/usr/bin/env python3
# Status: experimental
# Path: calendar_sync/router.py
"""DB-backed session management for calendar sync."""

import secrets
from datetime import datetime, timezone, timedelta
from typing import Optional

from lib.db import psql_json, psql_ok, esc_sql


class SessionService:
    """Manages user sessions in PostgreSQL."""

    def __init__(self):
        self._ensure_table()

    def _ensure_table(self):
        """Create sessions table if not exists."""
        from lib.db import psql_ok
        psql_ok("""
            CREATE TABLE IF NOT EXISTS web_sessions (
                session_id TEXT PRIMARY KEY,
                user_id TEXT,
                email TEXT,
                name TEXT,
                oauth_state TEXT,
                sheets_mode BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                expires_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() + INTERVAL '24 hours'
            )
        """)

    def create_session(self, session_data: dict, ttl_hours: int = 24) -> str:
        """Create a new session and return session ID."""
        session_id = secrets.token_urlsafe(32)
        now = datetime.now(timezone.utc)
        expires = now + timedelta(hours=ttl_hours)

        user_id = session_data.get("user_id", "")
        email = session_data.get("email", "")
        name = session_data.get("name", "")
        oauth_state = session_data.get("oauth_state", "")
        sheets_mode = session_data.get("sheets_mode", False)

        sql = f"""
        INSERT INTO web_sessions (session_id, user_id, email, name, oauth_state, sheets_mode, created_at, expires_at)
        VALUES (
            '{esc_sql(session_id)}',
            '{esc_sql(user_id)}',
            '{esc_sql(email)}',
            '{esc_sql(name)}',
            '{esc_sql(oauth_state)}',
            {str(sheets_mode).upper()},
            '{esc_sql(now.isoformat())}',
            '{esc_sql(expires.isoformat())}'
        )
        """
        psql_ok(sql)
        return session_id

    def get_session(self, session_id: str) -> Optional[dict]:
        """Retrieve session data by ID. Returns None if expired or not found."""
        rows = psql_json(
            f"SELECT * FROM web_sessions WHERE session_id = '{esc_sql(session_id)}' "
            f"AND expires_at > NOW()"
        )
        if not rows:
            return None
        r = rows[0]
        return {
            "user_id": r.get("user_id", ""),
            "email": r.get("email", ""),
            "name": r.get("name", ""),
            "oauth_state": r.get("oauth_state", ""),
            "sheets_mode": r.get("sheets_mode", False),
        }

    def update_session(self, session_id: str, session_data: dict) -> bool:
        """Update existing session data."""
        user_id = session_data.get("user_id", "")
        email = session_data.get("email", "")
        name = session_data.get("name", "")
        oauth_state = session_data.get("oauth_state", "")
        sheets_mode = session_data.get("sheets_mode", False)

        sql = f"""
        UPDATE web_sessions SET
            user_id = '{esc_sql(user_id)}',
            email = '{esc_sql(email)}',
            name = '{esc_sql(name)}',
            oauth_state = '{esc_sql(oauth_state)}',
            sheets_mode = {str(sheets_mode).upper()}
        WHERE session_id = '{esc_sql(session_id)}'
        """
        return psql_ok(sql)

    def delete_session(self, session_id: str) -> bool:
        """Delete a session."""
        return psql_ok(f"DELETE FROM web_sessions WHERE session_id = '{esc_sql(session_id)}'")

    def cleanup_expired(self) -> int:
        """Remove expired sessions. Returns count of deleted sessions."""
        try:
            rows = psql_json("SELECT COUNT(*) as cnt FROM web_sessions WHERE expires_at < NOW()")
            expired_count = rows[0]["cnt"] if rows else 0
            if expired_count > 0:
                psql_ok("DELETE FROM web_sessions WHERE expires_at < NOW()")
            return expired_count
        except Exception:
            return 0


# Singleton instance
session_service = SessionService()
