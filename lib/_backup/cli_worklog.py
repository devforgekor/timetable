#!/usr/bin/env python3
# Status: production
# Path: imported by — cli.py
"""CLI worklog management — add, recent, search commands."""
import json

from lib.db import psql as _sql, esc_sql
from lib.tracking.agent_names import normalize as normalize_agent


def cmd_worklog_add(args):
    """Insert a new worklog entry directly into PostgreSQL."""
    tags = [t.strip() for t in args.tags.split(",") if t.strip()] if args.tags else []
    files = [f.strip() for f in args.files.split(",") if f.strip()] if args.files else []
    details_json = json.dumps(args.details.split(",") if args.details else [])
    files_json = json.dumps(files)
    tags_array = "{" + ",".join(tags) + "}"
    agent_val = esc_sql(normalize_agent(args.agent)) if args.agent else ""
    model_val = esc_sql(args.model) if args.model else ""

    columns = "date, title, summary, details, files, tags, status, kind"
    values = f"CURRENT_DATE, '{esc_sql(args.title)}', '{esc_sql(args.summary)}', '{details_json}'::jsonb, '{files_json}'::jsonb, '{tags_array}', 'done', 'task'"
    if agent_val:
        columns += ", agent"
        values += f", '{agent_val}'"
    if model_val:
        columns += ", model"
        values += f", '{model_val}'"

    sql = f"INSERT INTO worklog_entries ({columns}) VALUES ({values}) RETURNING id"
    result = _sql(sql)
    if not result or not result.strip().isdigit():
        return
    print(f"+ {args.title[:60]}")
    if agent_val:
        print(f"  agent: {agent_val}")
    if args.model:
        print(f"  model: {args.model}")
    if tags:
        print(f"  tags: {', '.join(tags)}")


def cmd_worklog_recent(args):
    """Show recent worklog entries."""
    limit = args.limit or 3
    sql = f"SELECT date, title, summary, tags, agent, model FROM worklog_entries ORDER BY created_at DESC LIMIT {limit}"
    result = _sql(sql)
    if not result:
        return
    for line in result.split("\n"):
        if not line:
            continue
        parts = line.split("|", 5)
        if len(parts) < 3:
            continue
        date, title, summary = parts[0], parts[1], parts[2]
        tags_str = parts[3] if len(parts) > 3 else ""
        agent_str = parts[4] if len(parts) > 4 else ""
        model_str = parts[5] if len(parts) > 5 else ""
        header = f"[{date}] {title}"
        if agent_str:
            header += f"  ({agent_str}"
            if model_str:
                header += f"/{model_str}"
            header += ")"
        print(header)
        print(f"  {summary[:120]}")
        if tags_str and tags_str != "{}":
            print(f"  tags: {tags_str}")
        print()


def cmd_worklog_search(args):
    """Search worklog entries by keyword or tag."""
    conditions = []
    if args.tag:
        tag_esc = esc_sql(args.tag)
        conditions.append(f"tags @> '{{{tag_esc}}}'")
    if args.query:
        query_esc = esc_sql(args.query)
        conditions.append(f"(title ILIKE '%{query_esc}%' OR summary ILIKE '%{query_esc}%')")

    where = " AND ".join(conditions) if conditions else "TRUE"
    limit = args.limit or 20
    sql = f"SELECT date, title, summary, tags, agent, model FROM worklog_entries WHERE {where} ORDER BY created_at DESC LIMIT {limit}"
    result = _sql(sql)
    if not result:
        return

    count = 0
    for line in result.split("\n"):
        if not line:
            continue
        parts = line.split("|", 5)
        if len(parts) < 3:
            continue
        date, title, summary = parts[0], parts[1], parts[2]
        tags_str = parts[3] if len(parts) > 3 else ""
        agent_str = parts[4] if len(parts) > 4 else ""
        model_str = parts[5] if len(parts) > 5 else ""
        header = f"[{date}] {title}"
        if agent_str:
            header += f"  ({agent_str}"
            if model_str:
                header += f"/{model_str}"
            header += ")"
        print(header)
        print(f"  {summary[:120]}")
        if tags_str and tags_str != "{}":
            print(f"  tags: {tags_str}")
        print()
        count += 1
    print(f"{count} results")
