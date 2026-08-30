#!/usr/bin/env python3
# Status: production
# Path: imported by — production scripts
"""Subprocess helpers — run commands and collect output lines."""
import subprocess


def run_subprocess(cmd, timeout=15):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip()
    except Exception:
        return ""


def run_lines(cmd, timeout=15):
    out = run_subprocess(cmd, timeout)
    return [l for l in out.split("\n") if l.strip()] if out else []

