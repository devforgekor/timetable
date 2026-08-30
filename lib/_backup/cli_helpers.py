# Status: production
# Path: imported by — cli.py (status command helpers)
"""CLI helper functions — system queries used by cmd_status and others."""

import json
import re
import subprocess
from typing import Any

from lib.db import psql as _sql
from lib.db import psql_json


def _run(cmd, timeout=10):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.stdout.strip(), p.stderr.strip(), p.returncode
    except (subprocess.TimeoutExpired, subprocess.FileNotFoundError, OSError) as e:
        return "", str(e), 1


def _get_containers():
    out, _, rc = _run(["podman", "ps", "--format", "{{.Names}}|{{.Status}}|{{.Ports}}|{{.Image}}"])
    if rc != 0:
        return {"error": out or "podman not available"}
    containers = {}
    for line in out.split("\n"):
        parts = line.split("|", 3)
        if len(parts) < 2:
            continue
        name, status, ports, image = (
            parts[0],
            parts[1],
            parts[2] if len(parts) > 2 else "",
            parts[3] if len(parts) > 3 else "",
        )
        containers[name] = {
            "status": status,
            "ports": ports,
            "image": image.split("/")[-1] if image else "",
        }
    return containers


def _get_models():
    import urllib.request

    models = {}
    for label, port in [("inference", 8082)]:
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/models", method="GET")
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode())
                models[label] = [
                    m.get("name", m.get("model", "?"))
                    for m in data.get("models", data.get("data", []))
                ]
        except Exception as e:
            models[label] = f"unreachable: {e}"
    return models


def _get_timers():
    out, _, rc = _run(["systemctl", "--user", "list-timers", "--no-pager", "--no-legend"])
    if rc != 0:
        return {"error": out}
    timers = {"active": [], "inactive": [], "other": []}
    for line in out.split("\n"):
        line = line.strip()
        if not line:
            continue
        m = re.search(r"(\S+\.timer)\s+\S+\.service", line)
        if not m:
            continue
        timer_name = m.group(1)
        if re.match(r"^(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s", line):
            timers["active"].append(timer_name)
        elif line.startswith("n/a"):
            timers["inactive"].append(timer_name)
        else:
            timers["other"].append(timer_name)
    return timers


def _get_services():
    out, _, rc = _run(
        ["systemctl", "--user", "list-units", "--type=service", "--no-pager", "--no-legend"]
    )
    if rc != 0:
        return {"error": out}
    services = {}
    for line in out.split("\n"):
        parts = line.split()
        if len(parts) < 4:
            continue
        name = parts[0].replace(".service", "")
        load, active, sub_state = parts[1], parts[2], parts[3]
        desc_start = line.find(parts[3]) + len(parts[3])
        description = line[desc_start:].strip() if desc_start < len(line) else ""
        if active in ("active", "failed"):
            services[name] = {"state": active, "sub": sub_state, "desc": description}
    return services


def _get_resources() -> dict[str, Any]:
    resources: dict[str, Any] = {}
    out, _, _ = _run(["free", "-h"])
    if out:
        for line in out.split("\n"):
            if line.startswith("Mem:"):
                parts = line.split()
                resources["memory"] = (
                    {"total": parts[1], "used": parts[2], "free": parts[3], "available": parts[6]}
                    if len(parts) >= 7
                    else {}
                )
            elif line.startswith("Swap:"):
                parts = line.split()
                resources["swap"] = (
                    {"total": parts[1], "used": parts[2], "free": parts[3]}
                    if len(parts) >= 4
                    else {}
                )
    out, _, _ = _run(
        [
            "df",
            "-h",
            "/",
            "/opt/ai_data",
            "/mnt/lv_db",
            "/mnt/secure_meta",
            "/var/log",
            "/var/tmp",
            "/opt/projects",
        ]
    )
    disks = {}
    if out:
        for line in out.split("\n")[1:]:
            parts = line.split()
            if len(parts) >= 6:
                disks[parts[5]] = {
                    "size": parts[1],
                    "used": parts[2],
                    "avail": parts[3],
                    "use_pct": parts[4],
                }
    resources["disks"] = disks
    try:
        with open("/proc/loadavg") as f:
            lavg = f.read().split()
            resources["load"] = {
                "1min": float(lavg[0]),
                "5min": float(lavg[1]),
                "15min": float(lavg[2]),
            }
        with open("/proc/uptime") as f:
            up_sec = float(f.read().split()[0])
            d = int(up_sec) // 86400
            h = (int(up_sec) % 86400) // 3600
            m = (int(up_sec) % 3600) // 60
            resources["uptime"] = f"{d}d {h}h {m}m"
    except Exception:
        pass
    return resources


def _get_experiments():
    """Query experiment_registry from DB."""
    sql = "SELECT experiment_id, category, verdict, substring(rationale,1,100) as excerpt, created_at FROM experiment_registry ORDER BY created_at DESC LIMIT 7"
    out = _sql(sql)
    if not out:
        return []
    exps = []
    for line in out.split("\n"):
        line = line.strip()
        if not line:
            continue
        parts = line.split("|")
        if len(parts) < 5:
            continue
        exps.append(
            {
                "id": parts[0].strip(),
                "category": parts[1].strip(),
                "verdict": parts[2].strip(),
                "excerpt": parts[3].strip(),
                "created_at": parts[4].strip(),
            }
        )
    return exps


def _get_active_config():
    sql = "SELECT component, config, rationale FROM active_config"
    out = _sql(sql)
    if not out:
        return []
    configs = []
    for line in out.split("\n"):
        line = line.strip()
        if not line:
            continue
        parts = line.split("|", 2)
        if len(parts) < 3:
            continue
        configs.append(
            {
                "component": parts[0].strip(),
                "config": parts[1].strip()[:120],
                "rationale": parts[2].strip()[:120],
            }
        )
    return configs


def _get_tasks():
    rows = psql_json(
        "SELECT id, title, status, priority FROM tasks "
        "WHERE status IN ('in_progress', 'pending', 'blocked', 'completed')"
    )
    if not rows:
        return {"error": "tasks table empty"}
    summary = {"in_progress": [], "pending": [], "blocked": [], "completed_count": 0, "total": 0}
    for t in rows:
        summary["total"] += 1
        sid = t.get("id", 0)
        if t["status"] == "in_progress":
            summary["in_progress"].append({"id": sid, "title": t["title"]})
        elif t["status"] == "pending":
            summary["pending"].append(
                {"id": sid, "priority": t.get("priority", ""), "title": t["title"]}
            )
        elif t["status"] == "blocked":
            summary["blocked"].append({"id": sid, "title": t["title"]})
        elif t["status"] == "completed":
            summary["completed_count"] += 1
    return summary


def _get_alerts(containers, resources):
    """Derive alerts from thresholds."""
    alerts = []
    # container down
    expected = ["postgres", "devforge-inference"]
    for name in expected:
        if name not in containers:
            alerts.append(f"Container {name} is DOWN")
    # disk > 90%
    for mount, info in resources.get("disks", {}).items():
        pct = info.get("use_pct", "0%").replace("%", "")
        try:
            if int(pct) > 90:
                alerts.append(f"Disk {mount} at {pct}%")
        except ValueError:
            pass
    # memory > 95%
    mem = resources.get("memory", {})
    if mem:
        try:
            import re

            used = re.sub(r"[^0-9.]", "", mem.get("used", "0"))
            total = re.sub(r"[^0-9.]", "", mem.get("total", "1"))
            if float(used) / float(total) > 0.95:
                alerts.append(f"Memory {used}/{total}")
        except (ValueError, ZeroDivisionError):
            pass
    return alerts


def _get_rule_status():
    """Run lint_rules and return summary + violation counts."""
    from lint_rules import SCRIPTS_DIR as LINT_DIR
    from lint_rules import find_python_files, run_all_checks

    try:
        result = run_all_checks(find_python_files(LINT_DIR))
        return {
            "passed": result["passed"],
            "status": result["status"],
            "files_checked": result["total_files"],
            "violations": result["violations_by_severity"],
            "p0_violations": [
                {"file": v["file"], "line": v.get("line", ""), "message": v["message"]}
                for v in result["violations"]
                if v["severity"] == "P0"
            ][:10],
        }
    except Exception as e:
        return {"error": str(e)}


def _get_glossary():
    """Return glossary terms with bounded context names."""
    return psql_json(
        "SELECT gt.term, gt.definition, bc.name as context "
        "FROM glossary_terms gt LEFT JOIN bounded_contexts bc ON gt.bounded_context_id = bc.id "
        "ORDER BY bc.id, gt.term"
    )


def _get_references():
    """Return static references grouped by category."""
    return psql_json(
        "SELECT category, name, url, description FROM static_references ORDER BY category, name"
    )
