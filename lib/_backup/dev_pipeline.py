#!/usr/bin/env python3
# Status: production
# Path: imported by — cli.py (dev subcommand)
"""GitHub Issue -> PR pipeline (DevForge Devin-like)."""

import datetime
import json
import os
import subprocess
from typing import Any, Dict, List, Optional

SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SECRETS_PATH = os.path.expanduser("~/.config/devforge/secrets.env")
STATE_PATH = os.path.join(SCRIPTS_DIR, "data", "dev_pipeline_state.json")
AUTO_TASKS_PATH = os.path.join(SCRIPTS_DIR, "data", "auto_tasks.md")
DEFAULT_REPO = "devforgekor/devforge"


def _read_token() -> Optional[str]:
    """Extract GITHUB_TOKEN from secrets.env (handles optional quotes)."""
    if not os.path.isfile(SECRETS_PATH):
        return None
    import re

    with open(SECRETS_PATH) as f:
        for line in f:
            line = line.strip()
            m = re.match(r"^GITHUB_TOKEN=(.*)$", line)
            if m:
                raw = m.group(1)
                raw = raw.strip('"').strip("'").strip()
                return raw if raw else None
    return None


def _gh_env() -> Dict[str, str]:
    """Build env dict with GH_TOKEN for gh CLI auth."""
    env = {**os.environ}
    token = _read_token()
    if token:
        # GH_TOKEN is the modern env var (supersedes GITHUB_TOKEN for gh)
        env["GH_TOKEN"] = token
    return env


def _run_gh(args: List[str], timeout: int = 30) -> subprocess.CompletedProcess:
    """Run gh CLI with auth via GH_TOKEN."""
    return subprocess.run(
        ["gh"] + args,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=_gh_env(),
    )


def _load_state() -> Dict[str, Any]:
    """Load pipeline state from JSON file."""
    if not os.path.isfile(STATE_PATH):
        return {"seen_issues": [], "claimed": {}, "pr_created": {}}
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"seen_issues": [], "claimed": {}, "pr_created": {}}


def _save_state(data: Dict[str, Any]) -> None:
    """Save pipeline state to JSON file."""
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w") as f:
        json.dump(data, f, indent=2)


def poll_issues(
    label: Optional[str] = None,
    repo: str = DEFAULT_REPO,
    auto_safe: bool = False,
) -> List[Dict[str, Any]]:
    """List unassigned issues not yet seen in state.

    Args:
        label: Optional label filter (e.g. 'bug', 'enhancement').
        repo: GitHub repo slug (default: devforgekor/devforge).
        auto_safe: If True, only return issues with 'auto-safe' label.

    Returns:
        List of issue dicts with keys: number, title, labels, body, url.
    """
    state = _load_state()
    seen = set(state.get("seen_issues", []))

    cmd = [
        "issue",
        "list",
        "--repo",
        repo,
        "--json",
        "number,title,labels,body,url,state,assignees",
    ]
    if label:
        cmd += ["--label", label]

    result = _run_gh(cmd)
    if result.returncode != 0:
        # gh not authenticated or no issues
        return []

    try:
        all_issues = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []

    unassigned = [
        i
        for i in all_issues
        if i.get("state") == "open"
        and not i.get("assignees")
        and i["number"] not in seen
        and (not auto_safe or any(lb.get("name") == "auto-safe" for lb in i.get("labels", [])))
    ]
    return unassigned


def claim_issue(issue_number: int, repo: str = DEFAULT_REPO) -> bool:
    """Claim an issue: assign self, create branch, write auto task.

    Returns True if all steps succeeded.
    """
    state = _load_state()

    # 1. Fetch issue details
    result = _run_gh(
        [
            "issue",
            "view",
            str(issue_number),
            "--repo",
            repo,
            "--json",
            "number,title,body,labels,url",
        ]
    )
    if result.returncode != 0:
        return False

    try:
        issue = json.loads(result.stdout)
    except json.JSONDecodeError:
        return False

    title = issue.get("title", f"Issue #{issue_number}")
    body = issue.get("body", "")

    # 2. Assign issue to self
    _run_gh(
        [
            "issue",
            "edit",
            str(issue_number),
            "--repo",
            repo,
            "--add-assignee",
            "@me",
        ]
    )

    # 3. Track with dev:in-progress label (non-fatal)
    _label_issue(issue_number, add_labels=["dev:in-progress"])

    # 4. Create branch via gh issue develop
    _run_gh(
        [
            "issue",
            "develop",
            str(issue_number),
            "--repo",
            repo,
            "--name",
            f"issue-{issue_number}-auto",
            "--base",
            "main",
        ]
    )

    # 4. Write auto task to auto_tasks.md
    _write_auto_task(issue_number, title, body)

    # 5. Update state
    seen = state.get("seen_issues", [])
    if issue_number not in seen:
        seen.append(issue_number)
    state["seen_issues"] = seen
    state["claimed"][str(issue_number)] = {
        "title": title,
        "claimed_at": datetime.datetime.now(datetime.timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
    }
    _save_state(state)

    return True


def _write_auto_task(issue_number: int, title: str, body: str) -> None:
    """Append a task section to auto_tasks.md."""
    task_title = f"#{issue_number} {title}"
    task_body = (
        f"{body}\n\n"
        f"---\n"
        f"GitHub Issue #{issue_number} — implement the description above.\n"
        f"Work autonomously. After implementation: git add -A && git commit -m "
        f'"feat: resolve #{issue_number} {title}" && git push.\n'
        f"Then run: python3 cli.py dev pr {issue_number}"
    )
    os.makedirs(os.path.dirname(AUTO_TASKS_PATH), exist_ok=True)
    with open(AUTO_TASKS_PATH, "a") as f:
        f.write(f"\n## {task_title}\n{task_body}\n")


def _label_issue(
    issue_number: int,
    add_labels: Optional[List[str]] = None,
    remove_labels: Optional[List[str]] = None,
    repo: str = DEFAULT_REPO,
) -> None:
    """Add/remove labels on an issue. Non-fatal on failure (label may not exist in repo)."""
    cmd = ["issue", "edit", str(issue_number), "--repo", repo]
    if add_labels:
        cmd += ["--add-label", ",".join(add_labels)]
    if remove_labels:
        cmd += ["--remove-label", ",".join(remove_labels)]
    try:
        _run_gh(cmd)
    except Exception:
        pass


def create_pr(issue_number: int, repo: str = DEFAULT_REPO) -> Optional[str]:
    """Create a PR from the issue's branch.

    Returns the PR URL or None on failure.
    """
    branch = f"issue-{issue_number}-auto"

    result = _run_gh(
        [
            "pr",
            "create",
            "--repo",
            repo,
            "--fill",
            "--base",
            "main",
            "--head",
            branch,
        ],
        timeout=60,
    )
    if result.returncode != 0:
        return None

    pr_url = result.stdout.strip()

    # Update labels: in-progress → pending-review (non-fatal)
    _label_issue(
        issue_number,
        add_labels=["dev:pending-review"],
        remove_labels=["dev:in-progress"],
    )

    state = _load_state()
    pr_created = state.get("pr_created", {})
    pr_created[str(issue_number)] = {
        "url": pr_url,
        "created_at": datetime.datetime.now(datetime.timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
    }
    state["pr_created"] = pr_created
    _save_state(state)

    return pr_url


def claim_pending_existing(state: Dict[str, Any], repo: str = DEFAULT_REPO) -> List[Dict[str, Any]]:
    """(Future) Detect existing unassigned issues that predate state tracking.
    Kept for extensibility; currently unused."""
    return []


def _dev_claimed_issues(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return all claimed issues as a list for monitoring/reporting."""
    claimed = state.get("claimed", {})
    return [{"number": int(k), **v} for k, v in claimed.items()]
