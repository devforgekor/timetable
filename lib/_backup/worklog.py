#!/usr/bin/env python3
# Status: production
# Path: imported by — production scripts
"""Git commit → worklog sync.

Used by review_worker.py to record git commits into worklog_entries
with idempotent INSERT ON CONFLICT DO NOTHING.
"""
import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path

from lib.db import psql, esc_sql

SERVER = Path("/opt/projects/server")
KST = timezone(timedelta(hours=9))


def _git(args):
    try:
        r = subprocess.run(["git"] + args, capture_output=True, text=True, timeout=15, cwd=str(SERVER))
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def log_commits_to_worklog():
    """Scan git log for commits in last 24h, INSERT to worklog_entries.

    Idempotent: ON CONFLICT (date, git_commit_hash) DO NOTHING.
    Returns count of newly recorded commits.
    """
    commits = _git(["log", "--since=24 hours ago", "--format=%H|%s|%an|%aI"])
    if not commits:
        return 0

    saved = 0
    for line in commits.split("\n"):
        if not line.strip():
            continue
        parts = line.split("|", 4)
        if len(parts) < 4:
            continue
        sha = parts[0].strip()
        message = parts[1].strip()
        author = parts[2].strip()
        date_str = parts[3].strip()[:10] if len(parts) > 3 else datetime.now(KST).strftime("%Y-%m-%d")

        title = esc_sql(message[:200])
        summary = esc_sql(message[:500])
        agent_name = esc_sql(author)
        sha_esc = esc_sql(sha)

        result = psql(
            f"INSERT INTO worklog_entries (date, title, summary, git_commit_hash, agent, kind) "
            f"VALUES ('{date_str}', '{title}', '{summary}', '{sha_esc}', '{agent_name}', 'git') "
            f"ON CONFLICT (date, git_commit_hash) DO NOTHING "
            f"RETURNING id"
        )
        if result.strip().isdigit():
            saved += 1

        # Dual-write to activity_log (Phase A migration)
        psql(
            f"INSERT INTO activity_log (type, source, title, summary, git_commit_hash, agent, summary_status) "
            f"VALUES ('commit', 'git', '{title}', '{summary}', '{sha_esc}', '{agent_name}', 'raw') "
            f"ON CONFLICT (git_commit_hash) WHERE git_commit_hash IS NOT NULL AND type = 'commit' "
            f"DO NOTHING"
        )

    if saved:
        print(f"  log_commits: {saved} new commit(s) recorded")
    return saved

