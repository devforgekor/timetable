#!/usr/bin/env python3
# Status: experimental
"""SSH tunnel management for Azure Spot VMs."""

from __future__ import annotations

import subprocess
import time
from typing import Optional


def open_spot_tunnel(
    label: str, ip: str, remote_port: int, local_port: int,
    ssh_user: str = "azureuser",
) -> Optional[subprocess.Popen]:
    cmd = [
        "ssh", "-o", "StrictHostKeyChecking=no",
        "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=3",
        "-N", "-L", f"{local_port}:localhost:{remote_port}",
        f"{ssh_user}@{ip}",
    ]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(2)
        if proc.poll() is not None:
            print(f"  Tunnel for {label} failed to start (exit={proc.returncode})")
            return None
        print(f"  Tunnel for {label} localhost:{local_port} → {ip}:{remote_port} (pid={proc.pid})")
        return proc
    except Exception as e:
        print(f"  Tunnel for {label} error: {e}")
        return None


def close_spot_tunnel(local_port: int) -> None:
    try:
        r = subprocess.run(
            ["lsof", "-ti", f":{local_port}"],
            capture_output=True, text=True, timeout=5,
        )
        if r.stdout.strip():
            pids = r.stdout.strip().split()
            for pid in pids:
                subprocess.run(["kill", pid], timeout=3)
                print(f"  Killed process {pid} on port {local_port}")
    except Exception:
        pass
