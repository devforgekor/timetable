#!/usr/bin/env python3
# Status: experimental
# Path: calendar_sync/router.py
"""Google Calendar API service with sync modes."""

import time
from datetime import datetime, timezone
from typing import Optional

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.oauth2.credentials import Credentials

from .models import CalendarEvent, SyncConfig, SyncResult, SyncMode


class CalendarService:
    """Google Calendar API service with batch operations and sync modes."""

    BATCH_SIZE = 50
    MAX_RETRIES = 3
    RETRY_DELAY = 1  # seconds, exponential backoff

    def __init__(self, credentials: Credentials, calendar_id: str = "primary"):
        self.credentials = credentials
        self.calendar_id = calendar_id
        self.service = build("calendar", "v3", credentials=credentials, cache_discovery=False)
        self.errors: list[str] = []

    def _execute_with_retry(self, request):
        """Execute API request with exponential backoff retry."""
        for attempt in range(self.MAX_RETRIES):
            try:
                return request.execute()
            except HttpError as e:
                if e.resp.status in (403, 429, 500, 502, 503, 504):
                    if attempt < self.MAX_RETRIES - 1:
                        wait = self.RETRY_DELAY * (2 ** attempt)
                        time.sleep(wait)
                        continue
                raise
            except Exception as e:
                if attempt < self.MAX_RETRIES - 1:
                    time.sleep(self.RETRY_DELAY * (2 ** attempt))
                    continue
                raise

    def _batch_execute(self, requests: list) -> list:
        """Execute multiple requests sequentially (batch-compatible).

        google-api-python-client의 new_batch_http_request()는 구버전 전용이며
        신버전(httpx 기반)에서 동작 방식이 달라 중복/실패가 발생한다.
        BATCH_SIZE 단위로 순차 실행하되 개별 요청 실패를 격리한다.
        """
        results = []
        for i in range(0, len(requests), self.BATCH_SIZE):
            batch = requests[i:i + self.BATCH_SIZE]
            batch_results = [None] * len(batch)

            for idx, req in enumerate(batch):
                try:
                    resp = self._execute_with_retry(req)
                    batch_results[idx] = (resp, None)
                except Exception as e:
                    batch_results[idx] = (None, e)

            results.extend(batch_results)

        return results

    def insert_events(self, events: list[CalendarEvent]) -> SyncResult:
        """Insert multiple events sequentially."""
        self.errors = []
        result = SyncResult()
        requests = []

        for event in events:
            g_event = event.to_google_event()
            req = self.service.events().insert(calendarId=self.calendar_id, body=g_event)
            requests.append((req, event))  # Keep event reference for result

        for req, event in requests:
            try:
                self._execute_with_retry(req)
                result.created += 1
                result.events.append(event)
            except Exception as e:
                err_msg = f"Insert failed for {event.title}: {e}"
                self.errors.append(err_msg)
                result.errors.append(str(e))

        return result

    def delete_events_in_range(self, start_date: str, end_date: str) -> int:
        """Delete all events in date range."""
        self.errors = []
        deleted = 0

        # List events in range
        time_min = f"{start_date}T00:00:00+09:00"
        time_max = f"{end_date}T23:59:59+09:00"

        try:
            page_token = None
            while True:
                events_result = self._execute_with_retry(
                    self.service.events().list(
                        calendarId=self.calendar_id,
                        timeMin=time_min,
                        timeMax=time_max,
                        singleEvents=True,
                        orderBy="startTime",
                        pageToken=page_token,
                        maxResults=250,
                    )
                )

                items = events_result.get("items", [])
                if not items:
                    break

                # Batch delete
                delete_requests = [
                    self.service.events().delete(calendarId=self.calendar_id, eventId=item["id"])
                    for item in items
                ]
                self._batch_execute(delete_requests)
                deleted += len(items)

                page_token = events_result.get("nextPageToken")
                if not page_token:
                    break

        except HttpError as e:
            self.errors.append(f"Delete failed: {e}")
        except Exception as e:
            self.errors.append(f"Delete error: {e}")

        return deleted

    def list_events_in_range(self, start_date: str, end_date: str) -> list[dict]:
        """List all events in date range."""
        time_min = f"{start_date}T00:00:00+09:00"
        time_max = f"{end_date}T23:59:59+09:00"

        events = []
        try:
            page_token = None
            while True:
                events_result = self._execute_with_retry(
                    self.service.events().list(
                        calendarId=self.calendar_id,
                        timeMin=time_min,
                        timeMax=time_max,
                        singleEvents=True,
                        orderBy="startTime",
                        pageToken=page_token,
                        maxResults=250,
                    )
                )

                items = events_result.get("items", [])
                events.extend(items)

                page_token = events_result.get("nextPageToken")
                if not page_token:
                    break
        except Exception as e:
            self.errors.append(f"List events failed: {e}")

        return events

    @staticmethod
    def _event_key(event: dict) -> str:
        """Generate matching key for incremental sync: title + start date + end date.

        P0-2: end_date를 key에 포함해 동일제목·동일시작일 다른종료일 이벤트 구분.
        """
        summary = event.get("summary", "").strip()
        start = event.get("start", {})
        start_dt = start.get("dateTime") or start.get("date", "")
        start_date = start_dt[:10] if start_dt else ""
        end = event.get("end", {})
        end_dt = end.get("dateTime") or end.get("date", "")
        end_date = end_dt[:10] if end_dt else ""
        return f"{summary}|{start_date}|{end_date}"

    @staticmethod
    def _new_event_key(event: CalendarEvent) -> str:
        """Generate matching key for a CalendarEvent object."""
        return f"{event.title.strip()}|{event.start_date}|{event.end_date}"

    def _events_match(self, existing: dict, new_event: CalendarEvent) -> bool:
        """Check if existing event matches new event (same title, date, similar content)."""
        existing_key = self._event_key(existing)
        new_key = self._new_event_key(new_event)
        if existing_key != new_key:
            return False

        # Check if content differs
        existing_desc = existing.get("description", "") or ""
        existing_loc = existing.get("location", "") or ""
        new_desc = new_event.description or ""
        new_loc = new_event.location or ""

        if existing_desc.strip() != new_desc.strip():
            return False
        if existing_loc.strip() != new_loc.strip():
            return False

        # Compare start/end time
        existing_start = existing.get("start", {})
        existing_end = existing.get("end", {})
        existing_start_dt = existing_start.get("dateTime", "")
        existing_end_dt = existing_end.get("dateTime", "")

        if existing_start_dt:
            existing_start_time = existing_start_dt[11:19]  # HH:MM:SS
            new_start_time = new_event.start_time or "00:00:00"
            if existing_start_time != new_start_time:
                return False

        if existing_end_dt:
            existing_end_time = existing_end_dt[11:19]  # HH:MM:SS
            new_end_time = new_event.end_time or "23:59:59"
            if existing_end_time != new_end_time:
                return False

        # P1-2: attendees 비교 추가
        existing_attendees = existing.get("attendees", []) or []
        existing_attendee_emails = sorted(a.get("email", "") for a in existing_attendees)
        new_attendee_emails = sorted(new_event.attendees or [])
        if existing_attendee_emails != new_attendee_emails:
            return False

        return True

    def sync_events(self, events: list[CalendarEvent], config: SyncConfig) -> SyncResult:
        """Sync events using specified mode."""
        self.errors = []
        result = SyncResult()
        result.events = events

        if config.mode == SyncMode.FULL_REPLACE:
            return self._full_replace_sync(events, config)
        elif config.mode == SyncMode.INCREMENTAL_UPDATE:
            return self._incremental_sync(events, config)
        else:
            self.errors.append(f"Unknown sync mode: {config.mode}")
            result.errors.append(f"Unknown sync mode: {config.mode}")
            return result

    def _full_replace_sync(self, events: list[CalendarEvent], config: SyncConfig) -> SyncResult:
        """Full replace: delete all events in range, then insert new."""
        result = SyncResult()
        result.events = events

        # Determine date range
        start_date = config.date_range_start or min(e.start_date for e in events)
        end_date = config.date_range_end or max(e.end_date for e in events)

        # Delete existing events in range
        deleted = self.delete_events_in_range(start_date, end_date)
        result.deleted = deleted

        # P0-1: delete 실패 시 insert 중단 (중복 이벤트 대량 생성 방지)
        if self.errors:
            result.errors = list(self.errors)
            return result

        # Insert new events
        insert_result = self.insert_events(events)
        result.created = insert_result.created
        result.errors.extend(insert_result.errors)

        return result

    def _incremental_sync(self, events: list[CalendarEvent], config: SyncConfig) -> SyncResult:
        """Incremental update: match by title+date, update changed, insert new, delete removed."""
        result = SyncResult()
        result.events = events

        # Determine date range
        start_date = config.date_range_start or min(e.start_date for e in events)
        end_date = config.date_range_end or max(e.end_date for e in events)

        # Get existing events in range
        existing_events = self.list_events_in_range(start_date, end_date)

        # Build lookup maps (P0-2: end_date를 key에 포함)
        existing_map = {self._event_key(e): e for e in existing_events}
        new_map = {self._new_event_key(e): e for e in events}

        # Process new/updated events
        for key, new_event in new_map.items():
            if key in existing_map:
                existing = existing_map[key]
                if not self._events_match(existing, new_event):
                    # Update changed event
                    g_event = new_event.to_google_event()
                    try:
                        self._execute_with_retry(
                            self.service.events().update(
                                calendarId=self.calendar_id,
                                eventId=existing["id"],
                                body=g_event,
                            )
                        )
                        result.updated += 1
                    except Exception as e:
                        self.errors.append(f"Update failed for {new_event.title}: {e}")
                        result.errors.append(str(e))
                # else: no change needed
            else:
                # Insert new event
                g_event = new_event.to_google_event()
                try:
                    self._execute_with_retry(
                        self.service.events().insert(
                            calendarId=self.calendar_id,
                            body=g_event,
                        )
                    )
                    result.created += 1
                except Exception as e:
                    self.errors.append(f"Insert failed for {new_event.title}: {e}")
                    result.errors.append(str(e))

        # Delete events that are in existing but not in new
        for key, existing in existing_map.items():
            if key not in new_map:
                try:
                    self._execute_with_retry(
                        self.service.events().delete(
                            calendarId=self.calendar_id,
                            eventId=existing["id"],
                        )
                    )
                    result.deleted += 1
                except Exception as e:
                    self.errors.append(f"Delete failed for {existing.get('summary')}: {e}")
                    result.errors.append(str(e))

        return result