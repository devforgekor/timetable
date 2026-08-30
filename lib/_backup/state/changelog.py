#!/usr/bin/env python3
# Status: production
# Path: imported by — production scripts
"""Changelog management — load, save, append, archive.

Extracted from gen_server_state.py. Paths default to /opt/projects/server/.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import yaml

SERVER_DIR = Path("/opt/projects/server")
CHANGELOG_FILE = SERVER_DIR / "changelog.yaml"
ARCHIVE_FILE = SERVER_DIR / "changelog_archive.yaml"
TZ = timezone.utc
ARCHIVE_DAYS = 90


def load_changelog(path: Optional[Path] = None) -> dict:
    f = path or CHANGELOG_FILE
    if f.exists():
        with open(f) as fh:
            return yaml.safe_load(fh) or {}
    return {"entries": []}


def save_changelog(data: dict, path: Optional[Path] = None) -> None:
    f = path or CHANGELOG_FILE
    f.write_text(yaml.dump(data, default_flow_style=False, allow_unicode=True,
                           sort_keys=False, width=120))


def append_changelog_entry(changes: list, decisions: Optional[list] = None) -> dict:
    data = load_changelog()
    summary_parts = []
    for c in changes[:5]:
        path = c["path"]
        if c["type"] == "changed":
            summary_parts.append(f"{path}: {c['before']} -> {c['after']}")
        elif c["type"] == "added":
            summary_parts.append(f"{path}: added")
        elif c["type"] == "removed":
            summary_parts.append(f"{path}: removed")
    summary = "; ".join(summary_parts)
    if len(changes) > 5:
        summary += f" (+{len(changes) - 5} more)"

    entry = {
        "time": datetime.now(TZ).isoformat(),
        "type": "auto",
        "summary": summary,
        "changed": changes,
        "decisions": decisions or [],
    }
    data["entries"].insert(0, entry)
    save_changelog(data)
    archive_old_entries()
    return entry


def archive_old_entries() -> None:
    cutoff = datetime.now(TZ) - timedelta(days=ARCHIVE_DAYS)
    data = load_changelog()

    active, archived = [], []
    for entry in data.get("entries", []):
        try:
            t = datetime.fromisoformat(entry.get("time", ""))
        except (ValueError, TypeError):
            active.append(entry)
            continue
        if t < cutoff:
            archived.append(entry)
        else:
            active.append(entry)

    if not archived:
        return

    data["entries"] = active
    save_changelog(data)

    archive_data = {"entries": []}
    if ARCHIVE_FILE.exists():
        with open(ARCHIVE_FILE) as f:
            archive_data = yaml.safe_load(f) or {"entries": []}
    archive_data["entries"] = archived + archive_data.get("entries", [])
    ARCHIVE_FILE.write_text(yaml.dump(archive_data, default_flow_style=False,
                                      allow_unicode=True, sort_keys=False, width=120))

