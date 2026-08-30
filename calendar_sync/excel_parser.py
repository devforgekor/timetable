#!/usr/bin/env python3
# Status: experimental
# Path: calendar_sync/router.py
"""Excel file parser for calendar events."""

import io
from datetime import datetime
from typing import Optional

import pandas as pd

from .models import CalendarEvent


class ExcelParser:
    """Parse Excel files to extract calendar events."""

    # Column name mappings (case-insensitive, supports Korean/English)
    COLUMN_MAP = {
        "title": ["title", "제목", "subject", "일정", "event", "name"],
        "start_date": ["start_date", "startdate", "시작일", "시작날짜", "date", "날짜", "from"],
        "end_date": ["end_date", "enddate", "종료일", "종료날짜", "to", "까지"],
        "description": ["description", "내용", "memo", "메모", "details", "detail", "비고"],
        "location": ["location", "장소", "place", "위치", "where"],
        "attendees": ["attendees", "참석자", "참가자", "guests", "guest", "people", "인원"],
        "start_time": ["start_time", "starttime", "시작시간", "시간", "from_time"],
        "end_time": ["end_time", "endtime", "종료시간", "to_time"],
    }

    # Supported date formats
    DATE_FORMATS = [
        "%Y-%m-%d",
        "%Y/%m/%d",
        "%m/%d/%Y",
        "%m-%d-%Y",
        "%d/%m/%Y",
        "%d-%m-%Y",
        "%Y.%m.%d",
        "%m.%d.%Y",
    ]

    TIME_FORMATS = [
        "%H:%M:%S",
        "%H:%M",
        "%I:%M %p",
        "%I:%M%p",
    ]

    def __init__(self):
        self.errors: list[str] = []

    def parse_excel(self, file_bytes: bytes) -> list[CalendarEvent]:
        """Parse Excel file bytes and return list of CalendarEvent objects."""
        self.errors = []

        try:
            df = pd.read_excel(io.BytesIO(file_bytes), engine="openpyxl")
        except Exception as e:
            self.errors.append(f"Failed to read Excel file: {e}")
            return []

        if df.empty:
            self.errors.append("Excel file is empty")
            return []

        # Normalize column names
        df.columns = [self._normalize_col(c) for c in df.columns]

        # Map columns to standard names
        col_mapping = self._detect_columns(df.columns)
        df = df.rename(columns=col_mapping)

        # Validate required columns
        required = ["title", "start_date", "end_date"]
        missing = [c for c in required if c not in df.columns]
        if missing:
            self.errors.append(f"Missing required columns: {', '.join(missing)}")
            return []

        events = []
        for idx, row in df.iterrows():
            try:
                event = self._parse_row(row, idx)
                if event:
                    events.append(event)
            except Exception as e:
                self.errors.append(f"Row {idx + 2}: {e}")

        return events

    def _normalize_col(self, col: str) -> str:
        """Normalize column name: lowercase, strip, remove spaces/underscores."""
        return str(col).lower().strip().replace(" ", "_").replace("-", "_")

    def _detect_columns(self, columns: list[str]) -> dict[str, str]:
        """Detect which columns map to standard fields."""
        mapping = {}
        for std_name, aliases in self.COLUMN_MAP.items():
            for col in columns:
                if col in aliases:
                    mapping[col] = std_name
                    break
        return mapping

    def _parse_row(self, row: pd.Series, row_idx: int) -> Optional[CalendarEvent]:
        """Parse a single row into CalendarEvent."""
        # Required fields
        title = self._get_str(row, "title")
        if not title or pd.isna(title):
            raise ValueError("Title is required")

        start_date = self._parse_date(row, "start_date")
        end_date = self._parse_date(row, "end_date")

        if not start_date or not end_date:
            raise ValueError("Valid start_date and end_date are required")

        # Optional fields
        description = self._get_str(row, "description")
        location = self._get_str(row, "location")
        attendees = self._parse_attendees(row)
        start_time = self._parse_time(row, "start_time")
        end_time = self._parse_time(row, "end_time")

        return CalendarEvent(
            title=str(title).strip(),
            start_date=start_date,
            end_date=end_date,
            description=str(description).strip() if description else None,
            location=str(location).strip() if location else None,
            attendees=attendees,
            start_time=start_time,
            end_time=end_time,
        )

    def _get_str(self, row: pd.Series, col: str) -> Optional[str]:
        """Get string value from row, handling NaN."""
        if col not in row.index:
            return None
        val = row[col]
        if pd.isna(val):
            return None
        return str(val).strip()

    def _parse_date(self, row: pd.Series, col: str) -> Optional[str]:
        """Parse date from various formats, return YYYY-MM-DD."""
        val = self._get_str(row, col)
        if not val:
            return None

        # Try pandas datetime parsing first
        try:
            dt = pd.to_datetime(val, errors="raise")
            return dt.strftime("%Y-%m-%d")
        except Exception:
            pass

        # Try manual formats
        for fmt in self.DATE_FORMATS:
            try:
                dt = datetime.strptime(val, fmt)
                return dt.strftime("%Y-%m-%d")
            except ValueError:
                continue

        return None

    def _parse_time(self, row: pd.Series, col: str) -> Optional[str]:
        """Parse time from various formats, return HH:MM:SS."""
        val = self._get_str(row, col)
        if not val:
            return None

        for fmt in self.TIME_FORMATS:
            try:
                dt = datetime.strptime(val, fmt)
                return dt.strftime("%H:%M:%S")
            except ValueError:
                continue

        return None

    def _parse_attendees(self, row: pd.Series) -> Optional[list[str]]:
        """Parse attendees from comma/semicolon separated string."""
        val = self._get_str(row, "attendees")
        if not val:
            return None

        # Split by comma, semicolon, or newline
        import re
        emails = re.split(r"[,\n;]+", val)
        result = [e.strip() for e in emails if e.strip() and "@" in e]
        return result if result else None