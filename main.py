#!/usr/bin/env python3
# Status: experimental
# Path: standalone — /opt/workspace/timetable/main.py
"""Timetable — Google Calendar Sync from Excel/Sheets.

Standalone FastAPI app for calendar sync feature.
"""

import os
import sys
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# Add calendar_sync to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from calendar_sync.router import router as calendar_router
from calendar_sync.oauth_service import GoogleOAuthService

# Load secrets
_SECRETS: dict[str, str] = {}
_SF = os.path.expanduser("~/.config/devforge/secrets.env")
if os.path.exists(_SF):
    for _line in open(_SF).read().split("\n"):
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            _SECRETS[_k.strip()] = _v.strip().strip('"').strip("'")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    print("Timetable starting...")
    yield
    # Shutdown
    print("Timetable stopping...")


app = FastAPI(
    title="Timetable — Google Calendar Sync",
    description="Upload Excel or Google Sheets to sync events to Google Calendar",
    version="1.0.0",
    lifespan=lifespan,
)

# Mount calendar routes
app.include_router(calendar_router, prefix="/calendar")

# Static files (if needed)
static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/")
async def root():
    return {
        "name": "Timetable",
        "description": "Google Calendar Sync from Excel/Sheets",
        "version": "1.0.0",
        "endpoints": {
            "login": "/calendar/login",
            "upload": "/calendar/upload",
            "status": "/calendar/status",
            "health": "/health",
        },
    }


@app.get("/health")
async def health():
    return {"status": "ok", "service": "timetable"}


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Timetable — Google Calendar Sync")
    parser.add_argument("--port", "-p", type=int, default=8003, help="Port (default: 8003)")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="Host (default: 127.0.0.1)")
    parser.add_argument("--reload", action="store_true", help="Enable auto-reload")
    args = parser.parse_args()

    print(f"Starting Timetable on {args.host}:{args.port}")
    uvicorn.run("main:app", host=args.host, port=args.port, reload=args.reload, log_level="info")


if __name__ == "__main__":
    main()