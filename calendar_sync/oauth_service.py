#!/usr/bin/env python3
# Status: experimental
# Path: calendar_sync/router.py
"""Google OAuth 2.0 service for calendar sync."""

import os
import sys
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import urlencode

import httpx
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleRequest
from google_auth_oauthlib.flow import Flow

SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, SCRIPTS_DIR)

from lib.db import psql_json, psql_ok, esc_sql

import re

from .models import UserToken

# Input validation patterns for SQL safety
_USER_ID_RE = re.compile(r"^[a-zA-Z0-9@._-]+$")
_EMAIL_RE = re.compile(r"^[a-zA-Z0-9@._+-]+$")


def _validate_user_id(user_id: str) -> str:
    """Validate user_id contains only safe characters."""
    if not user_id or not _USER_ID_RE.match(user_id):
        raise ValueError(f"Invalid user_id: {user_id!r}")
    return user_id


def _validate_email(email: str) -> str:
    """Validate email contains only safe characters."""
    if not email or not _EMAIL_RE.match(email):
        raise ValueError(f"Invalid email: {email!r}")
    return email


# Load secrets - support both local file and Vercel environment variables
_SECRETS: dict[str, str] = {}

# First check environment variables (Vercel)
for key in ["GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"]:
    val = os.environ.get(key)
    if val:
        _SECRETS[key] = val

# Then check local secrets file (if exists)
_SF = os.path.expanduser("~/.config/devforge/secrets.env")
if os.path.exists(_SF):
    for _line in open(_SF).read().split("\n"):
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            _k = _k.strip()
            if _k not in _SECRETS:  # Environment variables take precedence
                _SECRETS[_k] = _v.strip().strip('"').strip("'")


# OAuth Configuration
GOOGLE_CLIENT_ID = _SECRETS.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = _SECRETS.get("GOOGLE_CLIENT_SECRET", "")

# Default redirect URI - will be overridden by environment
DEFAULT_REDIRECT_URI = "http://localhost:8002/auth/google/callback"

# Scopes for calendar + user info + optional sheets
DEFAULT_SCOPES = [
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
]

SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets.readonly"


class GoogleOAuthService:
    """Handle Google OAuth 2.0 flow and token management."""

    def __init__(self, redirect_uri: str = DEFAULT_REDIRECT_URI, include_sheets: bool = False):
        self.redirect_uri = redirect_uri
        scopes = DEFAULT_SCOPES.copy()
        if include_sheets:
            scopes.append(SHEETS_SCOPE)
        self.scopes = scopes

        # Client config for Flow
        self.client_config = {
            "web": {
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [redirect_uri],
            }
        }

    def get_flow(self, state: Optional[str] = None) -> Flow:
        """Create OAuth Flow instance."""
        flow = Flow.from_client_config(
            self.client_config,
            scopes=self.scopes,
            state=state,
        )
        flow.redirect_uri = self.redirect_uri
        return flow

    def get_auth_url(self, state: Optional[str] = None) -> str:
        """Generate Google OAuth authorization URL."""
        flow = self.get_flow(state=state)
        auth_url, _ = flow.authorization_url(
            access_type="offline",
            include_granted_scopes="true",
            prompt="consent",
        )
        return auth_url

    def exchange_code_for_tokens(self, code: str) -> Optional[Credentials]:
        """Exchange authorization code for access/refresh tokens."""
        flow = self.get_flow()
        try:
            flow.fetch_token(code=code)
            return flow.credentials
        except Exception as e:
            print(f"Token exchange failed: {e}")
            return None

    def refresh_access_token(self, refresh_token: str) -> Optional[Credentials]:
        """Refresh expired access token using refresh token."""
        try:
            credentials = Credentials(
                token=None,
                refresh_token=refresh_token,
                token_uri="https://oauth2.googleapis.com/token",
                client_id=GOOGLE_CLIENT_ID,
                client_secret=GOOGLE_CLIENT_SECRET,
                scopes=self.scopes,
            )
            credentials.refresh(GoogleRequest())
            return credentials
        except Exception as e:
            print(f"Token refresh failed: {e}")
            return None

    def get_user_info(self, access_token: str) -> Optional[dict]:
        """Get user info from Google using access token."""
        try:
            response = httpx.get(
                "https://www.googleapis.com/oauth2/v2/userinfo",
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=10,
            )
            if response.status_code == 200:
                return response.json()
        except Exception as e:
            print(f"User info fetch failed: {e}")
        return None

    def store_tokens(self, credentials: Credentials, user_info: dict) -> bool:
        """Store or update user tokens in database."""
        email = user_info.get("email")
        user_id = user_info.get("id", email)

        if not email or not credentials.token:
            return False

        try:
            user_id = _validate_user_id(user_id)
            email = _validate_email(email)
        except ValueError as e:
            print(f"Token storage rejected: {e}")
            return False

        scopes = list(credentials.scopes) if credentials.scopes else self.scopes
        token_expiry = credentials.expiry
        if token_expiry and token_expiry.tzinfo is None:
            token_expiry = token_expiry.replace(tzinfo=None)

        sql = f"""
        INSERT INTO user_tokens (user_id, email, access_token, refresh_token, token_expiry, scopes, updated_at)
        VALUES (
            '{esc_sql(user_id)}',
            '{esc_sql(email)}',
            '{esc_sql(credentials.token)}',
            '{esc_sql(credentials.refresh_token or "")}',
            '{esc_sql(token_expiry.isoformat() if token_expiry else "")}',
            '{esc_sql("{" + ",".join(f'"{s}"' for s in scopes) + "}")}',
            NOW()
        )
        ON CONFLICT (user_id) DO UPDATE SET
            email = EXCLUDED.email,
            access_token = EXCLUDED.access_token,
            refresh_token = EXCLUDED.refresh_token,
            token_expiry = EXCLUDED.token_expiry,
            scopes = EXCLUDED.scopes,
            updated_at = NOW()
        """
        return psql_ok(sql)

    def get_stored_tokens(self, user_id: str) -> Optional[UserToken]:
        """Retrieve stored tokens for a user."""
        try:
            user_id = _validate_user_id(user_id)
        except ValueError:
            return None
        rows = psql_json(f"SELECT * FROM user_tokens WHERE user_id = '{esc_sql(user_id)}'")
        if not rows:
            return None
        r = rows[0]
        return UserToken(
            user_id=r["user_id"],
            email=r["email"],
            access_token=r["access_token"],
            refresh_token=r["refresh_token"],
            token_expiry=r["token_expiry"],
            scopes=r["scopes"],
            created_at=r["created_at"],
            updated_at=r["updated_at"],
        )

    def get_valid_credentials(self, user_id: str) -> Optional[Credentials]:
        """Get valid credentials for a user, refreshing if necessary."""
        token_data = self.get_stored_tokens(user_id)
        if not token_data:
            return None

        credentials = Credentials(
            token=token_data.access_token,
            refresh_token=token_data.refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=GOOGLE_CLIENT_ID,
            client_secret=GOOGLE_CLIENT_SECRET,
            scopes=token_data.scopes,
        )

        # Check if token is expired or expiring soon (5 min buffer)
        if credentials.expiry:
            expiry = credentials.expiry
            if expiry.tzinfo is None:
                from datetime import timezone
                expiry = expiry.replace(tzinfo=timezone.utc)
            if expiry < datetime.now(timezone.utc) + timedelta(minutes=5):
                if token_data.refresh_token:
                    refreshed = self.refresh_access_token(token_data.refresh_token)
                    if refreshed:
                        self.store_tokens(refreshed, {"email": token_data.email, "id": user_id})
                        return refreshed
                return None

        return credentials

    def revoke_tokens(self, user_id: str) -> bool:
        """Revoke and delete stored tokens."""
        try:
            user_id = _validate_user_id(user_id)
        except ValueError:
            return False
        token_data = self.get_stored_tokens(user_id)
        if token_data and token_data.access_token:
            try:
                httpx.post(
                    "https://oauth2.googleapis.com/revoke",
                    params={"token": token_data.access_token},
                    timeout=10,
                )
            except Exception:
                pass

        return psql_ok(f"DELETE FROM user_tokens WHERE user_id = '{esc_sql(user_id)}'")