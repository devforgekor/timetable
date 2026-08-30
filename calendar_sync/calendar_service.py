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
        """Execute multiple requests in batches."""
        results = []
        for i in range(0, len(requests), self.BATCH_SIZE):
            batch = requests[i:i + self.BATCH_SIZE]
            batch_request = self.service.new_batch_http_request()
            batch_results = [None] * len(batch)

            def make_callback(idx):
                def callback(request_id, response, exception):
                    batch_results[idx] = (response, exception)
                return callback

            for idx, req in enumerate(batch):
                batch_request.add(req, callback=make_callback(idx))

            batch_request.execute()
            results.extend(batch_results)

        return results

    def insert_events(self, events: list[CalendarEvent]) -> SyncResult:
        """Insert multiple events using batch requests."""
        self.errors = []
        result = SyncResult()
        requests = []

        for event in events:
            g_event = event.to_google_event()
            req = self.service.events().insert(calendarId=self.calendar_id, body=g_event)
            requests.append(req)

        responses = self._batch_execute(requests)
        for response, exception in responses:
            if exception:
                self.errors.append(f"Insert failed: {exception}")
                result.errors.append(str(exception))
            else:
                result.created += 1
                result.events.append(event)

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

    def _event_key(self, event: dict) -> str:
        """Generate matching key for incremental sync: title + start date."""
        summary = event.get("summary", "").strip()
        start = event.get("start", {})
        start_dt = start.get("dateTime") or start.get("date", "")
        start_date = start_dt[:10] if start_dt else ""
        return f"{summary}|{start_date}"

    def _events_match(self, existing: dict, new_event: CalendarEvent) -> bool:
        """Check if existing event matches new event (same title, date, similar content)."""
        existing_key = self._event_key(existing)
        new_key = f"{new_event.title.strip()}|{new_event.start_date}"
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

        # Build lookup maps
        existing_map = {self._event_key(e): e for e in existing_events}
        new_map = {f"{e.title.strip()}|{e.start_date}": e for e in events}

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