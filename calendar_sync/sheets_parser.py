#!/usr/bin/env python3
# Status: experimental
# Path: calendar_sync/router.py
"""Google Sheets parser for calendar events."""

import re
from datetime import datetime, timezone, timedelta
from typing import Optional

import gspread
import pandas as pd
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleRequest

from .excel_parser import ExcelParser
from .models import CalendarEvent


class SheetsParser:
    """Parse Google Sheets to extract calendar events."""

    def __init__(self):
        self.errors: list[str] = []
        self.excel_parser = ExcelParser()

    def _ensure_valid_credentials(self, credentials: Credentials) -> Optional[Credentials]:
        """Ensure credentials are valid, refresh if expired."""
        if not credentials:
            return None

        # Check if token is expired or expiring soon (5 min buffer)
        if credentials.expiry:
            expiry = credentials.expiry
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            if expiry < datetime.now(timezone.utc) + timedelta(minutes=5):
                try:
                    credentials.refresh(GoogleRequest())
                except Exception as e:
                    self.errors.append(f"Token refresh failed: {e}")
                    return None

        return credentials

    def parse_sheets_url(self, url: str, access_token: str = None, credentials: Credentials = None) -> list[CalendarEvent]:
        """Parse Google Sheets from URL using user's credentials.

        Args:
            url: Google Sheets URL
            access_token: Legacy parameter (used if credentials not provided)
            credentials: Google OAuth Credentials object (preferred)
        """
        self.errors = []

        try:
            # Extract spreadsheet ID from URL
            spreadsheet_id = self._extract_spreadsheet_id(url)
            if not spreadsheet_id:
                self.errors.append("Invalid Google Sheets URL")
                return []

            # Create or validate credentials
            if credentials is None:
                if access_token is None:
                    self.errors.append("No credentials provided")
                    return []
                credentials = Credentials(token=access_token, scopes=[
                    "https://www.googleapis.com/auth/spreadsheets.readonly"
                ])

            # Ensure credentials are valid (refresh if needed)
            credentials = self._ensure_valid_credentials(credentials)
            if not credentials:
                self.errors.append("Failed to obtain valid credentials")
                return []

            # Authorize gspread
            gc = gspread.authorize(credentials)

            # Open spreadsheet
            try:
                spreadsheet = gc.open_by_key(spreadsheet_id)
            except gspread.exceptions.SpreadsheetNotFound:
                self.errors.append("Spreadsheet not found or no access")
                return []
            except gspread.exceptions.APIError as e:
                self.errors.append(f"Google Sheets API error: {e}")
                return []

            # Get first worksheet (or all worksheets)
            worksheets = spreadsheet.worksheets()
            if not worksheets:
                self.errors.append("No worksheets found in spreadsheet")
                return []

            all_events = []
            for ws in worksheets:
                try:
                    events = self._parse_worksheet(ws)
                    all_events.extend(events)
                except Exception as e:
                    self.errors.append(f"Worksheet '{ws.title}': {e}")

            return all_events

        except Exception as e:
            self.errors.append(f"Failed to parse Google Sheets: {e}")
            return []

    def _extract_spreadsheet_id(self, url: str) -> Optional[str]:
        """Extract spreadsheet ID from Google Sheets URL."""
        # Patterns: /d/{id}/edit, /d/{id}/, /spreadsheets/d/{id}
        patterns = [
            r"/d/([a-zA-Z0-9-_]+)",
            r"spreadsheets/d/([a-zA-Z0-9-_]+)",
        ]
        for pattern in patterns:
            match = re.search(pattern, url)
            if match:
                return match.group(1)
        return None

    def _parse_worksheet(self, worksheet) -> list[CalendarEvent]:
        """Parse a single worksheet into CalendarEvent objects."""
        # Get all values
        data = worksheet.get_all_values()
        if not data:
            return []

        # Convert to DataFrame (first row as header)
        headers = data[0]
        rows = data[1:] if len(data) > 1 else []

        if not rows:
            return []

        df = pd.DataFrame(rows, columns=headers)

        # Normalize column names (same as ExcelParser)
        df.columns = [self.excel_parser._normalize_col(c) for c in df.columns]

        # Map columns
        col_mapping = self.excel_parser._detect_columns(df.columns)
        df = df.rename(columns=col_mapping)

        # Validate required columns
        required = ["title", "start_date", "end_date"]
        missing = [c for c in required if c not in df.columns]
        if missing:
            raise ValueError(f"Missing required columns: {', '.join(missing)}")

        events = []
        for idx, row in df.iterrows():
            try:
                event = self.excel_parser._parse_row(row, idx)
                if event:
                    events.append(event)
            except Exception as e:
                raise ValueError(f"Row {idx + 2}: {e}")

        return events