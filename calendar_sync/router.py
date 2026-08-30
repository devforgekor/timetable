#!/usr/bin/env python3
# Status: experimental
# Path: devforge_fastapi/app.py (mounted)
"""Calendar sync routes for FastAPI."""

import os
import secrets
from datetime import datetime
from typing import Optional

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from google.oauth2.credentials import Credentials

from .calendar_service import CalendarService
from .excel_parser import ExcelParser
from .models import CalendarEvent, SyncConfig, SyncMode, SyncResult
from .oauth_service import GoogleOAuthService
from .sheets_parser import SheetsParser

# Templates
TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "templates")
templates = Jinja2Templates(directory=TEMPLATES_DIR)

router = APIRouter(tags=["calendar"])

# OAuth service (initialized per request with dynamic redirect_uri)
def get_oauth_service(request: Request, include_sheets: bool = False) -> GoogleOAuthService:
    # Use the request's base URL to build redirect_uri
    base_url = str(request.base_url).rstrip("/")
    redirect_uri = f"{base_url}/auth/google/callback"
    return GoogleOAuthService(redirect_uri=redirect_uri, include_sheets=include_sheets)


# In-memory session store (in production, use Redis or database)
_sessions: dict[str, dict] = {}


def get_session(request: Request) -> Optional[dict]:
    """Get session data from cookie."""
    session_id = request.cookies.get("calendar_session")
    print(f"[DEBUG] get_session: cookie={session_id}, sessions={list(_sessions.keys())[:3]}...")
    if session_id and session_id in _sessions:
        return _sessions[session_id]
    return None


def set_session(response: RedirectResponse, session_data: dict, request: Request = None) -> str:
    """Create session and set cookie."""
    session_id = secrets.token_urlsafe(32)
    _sessions[session_id] = session_data
    # secure=True only for HTTPS; allow HTTP for localhost dev
    is_https = request and request.url.scheme == "https"
    response.set_cookie(
        "calendar_session",
        session_id,
        httponly=True,
        secure=is_https,
        samesite="lax",
        max_age=86400 * 7,  # 7 days
    )
    return session_id


def clear_session(response: RedirectResponse):
    """Clear session."""
    session_id = response.headers.get("set-cookie", "")
    for sid, data in _sessions.items():
        if sid in session_id:
            del _sessions[sid]
            break
    response.delete_cookie("calendar_session")


# Routes
@router.get("/auth/google/login", response_class=HTMLResponse)
async def google_login(request: Request, sheets: bool = False):
    """Initiate Google OAuth flow."""
    oauth = get_oauth_service(request, include_sheets=sheets)
    state = secrets.token_urlsafe(32)
    auth_url = oauth.get_auth_url(state=state)

    # Store state in session
    session_data = {"oauth_state": state, "sheets_mode": sheets}
    response = RedirectResponse(url=auth_url)
    set_session(response, session_data, request)
    return response


@router.get("/auth/google/callback")
async def google_callback(request: Request, code: str = None, state: str = None, error: str = None):
    """Handle Google OAuth callback."""
    session = get_session(request)
    if not session:
        return RedirectResponse(url="/calendar/login?error=session_expired")

    if error:
        return RedirectResponse(url=f"/calendar/login?error={error}")

    if not code or not state:
        return RedirectResponse(url="/calendar/login?error=missing_params")

    # Verify state
    if state != session.get("oauth_state"):
        return RedirectResponse(url="/calendar/login?error=invalid_state")

    oauth = get_oauth_service(request, include_sheets=session.get("sheets_mode", False))
    credentials = oauth.exchange_code_for_tokens(code)

    if not credentials:
        return RedirectResponse(url="/calendar/login?error=token_exchange_failed")

    # Get user info
    user_info = oauth.get_user_info(credentials.token)
    if not user_info:
        return RedirectResponse(url="/calendar/login?error=user_info_failed")

    # Store tokens
    if not oauth.store_tokens(credentials, user_info):
        return RedirectResponse(url="/calendar/login?error=token_store_failed")

    # Update session with user info
    session["user_id"] = user_info.get("id")
    session["email"] = user_info.get("email")
    session["name"] = user_info.get("name")
    session["picture"] = user_info.get("picture")

    response = RedirectResponse(url="/calendar/upload")
    set_session(response, session, request)
    return response


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str = None):
    """Show login page."""
    return templates.TemplateResponse(
        "login.html",
        {"request": request, "error": error},
    )


@router.get("/upload", response_class=HTMLResponse)
async def upload_page(request: Request):
    """Show upload page (requires login)."""
    session = get_session(request)
    if not session or not session.get("user_id"):
        return RedirectResponse(url="/calendar/login")

    return templates.TemplateResponse(
        "upload.html",
        {"request": request, "user": session},
    )


@router.post("/upload/excel")
async def upload_excel(
    request: Request,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    sync_mode: str = Form("full_replace"),
    date_range_start: str = Form(""),
    date_range_end: str = Form(""),
):
    """Upload and parse Excel file, then sync to Calendar."""
    session = get_session(request)
    if not session or not session.get("user_id"):
        raise HTTPException(status_code=401, detail="Not authenticated")

    # Read file
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Empty file")

    # Parse Excel
    parser = ExcelParser()
    events = parser.parse_excel(content)

    if parser.errors:
        return templates.TemplateResponse(
            "result.html",
            {
                "request": request,
                "success": False,
                "message": "Excel parsing failed",
                "errors": parser.errors,
                "events": [],
            },
        )

    if not events:
        return templates.TemplateResponse(
            "result.html",
            {
                "request": request,
                "success": False,
                "message": "No valid events found in Excel",
                "errors": ["No valid events"],
                "events": [],
            },
        )

    # Sync to Calendar
    oauth = get_oauth_service(request)
    credentials = oauth.get_valid_credentials(session["user_id"])
    if not credentials:
        return RedirectResponse(url="/calendar/login?error=token_expired")

    config = SyncConfig(
        mode=SyncMode(sync_mode),
        date_range_start=date_range_start or None,
        date_range_end=date_range_end or None,
    )

    calendar = CalendarService(credentials, config.calendar_id)
    result = calendar.sync_events(events, config)

    return templates.TemplateResponse(
        "result.html",
        {
            "request": request,
            "success": len(result.errors) == 0,
            "message": f"Sync completed: {result.created} created, {result.updated} updated, {result.deleted} deleted",
            "errors": result.errors,
            "events": result.events,
            "stats": result,
        },
    )


@router.post("/upload/sheets")
async def upload_sheets(
    request: Request,
    background_tasks: BackgroundTasks,
    sheets_url: str = Form(...),
    sync_mode: str = Form("full_replace"),
    date_range_start: str = Form(""),
    date_range_end: str = Form(""),
):
    """Upload Google Sheets URL, parse, and sync to Calendar."""
    session = get_session(request)
    if not session or not session.get("user_id"):
        raise HTTPException(status_code=401, detail="Not authenticated")

    if not sheets_url:
        raise HTTPException(status_code=400, detail="Sheets URL required")

    # Get valid credentials (need sheets scope)
    oauth = get_oauth_service(request, include_sheets=True)
    credentials = oauth.get_valid_credentials(session["user_id"])
    if not credentials:
        return RedirectResponse(url="/calendar/auth/google/login?sheets=true")

    # Parse Sheets
    parser = SheetsParser()
    events = parser.parse_sheets_url(sheets_url, credentials.token)

    if parser.errors:
        return templates.TemplateResponse(
            "result.html",
            {
                "request": request,
                "success": False,
                "message": "Google Sheets parsing failed",
                "errors": parser.errors,
                "events": [],
            },
        )

    if not events:
        return templates.TemplateResponse(
            "result.html",
            {
                "request": request,
                "success": False,
                "message": "No valid events found in Google Sheets",
                "errors": ["No valid events"],
                "events": [],
            },
        )

    # Sync to Calendar
    config = SyncConfig(
        mode=SyncMode(sync_mode),
        date_range_start=date_range_start or None,
        date_range_end=date_range_end or None,
    )

    calendar = CalendarService(credentials, config.calendar_id)
    result = calendar.sync_events(events, config)

    return templates.TemplateResponse(
        "result.html",
        {
            "request": request,
            "success": len(result.errors) == 0,
            "message": f"Sync completed: {result.created} created, {result.updated} updated, {result.deleted} deleted",
            "errors": result.errors,
            "events": result.events,
            "stats": result,
        },
    )


@router.get("/status", response_class=HTMLResponse)
async def status_page(request: Request):
    """Show sync status page."""
    session = get_session(request)
    if not session or not session.get("user_id"):
        return RedirectResponse(url="/calendar/login")

    oauth = get_oauth_service(request)
    token_data = oauth.get_stored_tokens(session["user_id"])

    return templates.TemplateResponse(
        "status.html",
        {"request": request, "user": session, "token": token_data},
    )


@router.get("/logout")
async def logout(request: Request):
    """Logout and clear session."""
    session = get_session(request)
    if session and session.get("user_id"):
        oauth = get_oauth_service(request)
        oauth.revoke_tokens(session["user_id"])

    response = RedirectResponse(url="/calendar/login")
    clear_session(response)
    return response