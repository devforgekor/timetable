#!/usr/bin/env python3
# Status: production
# Path: imported by — production scripts
"""System state checks — systemd, podman, filesystem.

Usage:
    from lib.infra.health_checks import svc_active, svc_enabled, timer_active, container_running, file_exists

    if svc_active("devforge-turn-watcher"):
        ...
"""

import subprocess
from pathlib import Path


def svc_active(unit: str) -> bool:
    """Check if a systemd user unit is active."""
    try:
        r = subprocess.run(
            ["systemctl", "--user", "is-active", unit],
            capture_output=True, text=True, timeout=5,
        )
        return r.returncode == 0 and "active" in r.stdout
    except Exception:
        return False


def svc_enabled(unit: str) -> bool:
    """Check if a systemd user timer/service is enabled."""
    try:
        r = subprocess.run(
            ["systemctl", "--user", "is-enabled", unit],
            capture_output=True, text=True, timeout=5,
        )
        return r.returncode == 0
    except Exception:
        return False


def timer_active(timer: str) -> bool:
    """Check if a systemd user timer is active (running + waiting)."""
    try:
        r = subprocess.run(
            ["systemctl", "--user", "is-active", timer],
            capture_output=True, text=True, timeout=5,
        )
        return r.returncode == 0
    except Exception:
        return False


def container_running(name: str) -> bool:
    """Check if a Podman container is running."""
    try:
        r = subprocess.run(
            ["podman", "ps", "--format", "{{.Names}}"],
            capture_output=True, text=True, timeout=5,
        )
        return name in r.stdout.split("\n")
    except Exception:
        return False


def file_exists(path: str) -> bool:
    return Path(path).exists()

