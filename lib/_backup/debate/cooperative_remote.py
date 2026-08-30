#!/usr/bin/env python3
# Status: experimental
# Path: lib/debate/cooperative_remote.py — imported by cooperative_debate.py
"""SSH tunnel management and remote model activation for cooperative debate mode.

Handles persistent SSH tunnels to Azure spot VMs running llama-server instances.
Tunnels are opened once per session and reused across debate rounds.
"""
import os
import re
import subprocess
import time
from typing import Set

from .debate_data import MODELS, REMOTE_HOSTS
from .debate_llm import _poll_health


def _kill_ssh_tunnel(local_port: int) -> None:
    """Kill SSH tunnel process bound to local_port. Only kills ssh processes."""
    try:
        result = subprocess.run(
            ["ss", "-tlnp"], capture_output=True, text=True, timeout=5
        )
        for line in result.stdout.splitlines():
            if f":{local_port}" in line and "ssh" in line.lower():
                m = re.search(r"pid=(\d+)", line)
                if m:
                    pid = int(m.group(1))
                    try:
                        os.kill(pid, 15)
                        print(f"  [tunnel] Closed :{local_port} (pid={pid})")
                    except Exception:
                        pass
    except Exception:
        pass


def remote_activate(model_id: str, session_tunnels: Set[str]) -> bool:
    """Ensure remote model is accessible via SSH tunnel.

    Tunnel is opened once per session and reused across rounds.
    Model is always-on (OpenAI-compatible API on remote).
    """
    cfg = MODELS[model_id]
    host = cfg["host"]
    rh = REMOTE_HOSTS[host]
    ssh_host = rh["ssh_host"]
    local_port = cfg["local_port"]
    remote_port = cfg["port"]

    if host in session_tunnels:
        if _poll_health(port=local_port, timeout=3):
            print(f"  [tunnel] Reusing :{local_port} -> {host}:{remote_port}")
            return True
        _kill_ssh_tunnel(local_port)
        session_tunnels.discard(host)

    print(f"  [tunnel] Opening :{local_port} -> {host}:{remote_port}")
    try:
        tunnel_result = subprocess.run(
            [
                "ssh", "-f", "-N",
                "-o", "StrictHostKeyChecking=no",
                "-o", "ExitOnForwardFailure=yes",
                "-o", "ServerAliveInterval=60",
                "-o", "ServerAliveCountMax=3",
                "-L", f"{local_port}:localhost:{remote_port}",
                ssh_host,
            ],
            capture_output=True, timeout=10,
        )
        if tunnel_result.returncode != 0:
            print(f"  [tunnel] ERROR: {tunnel_result.stderr.decode()}")
            return False
    except subprocess.TimeoutExpired:
        print(f"  [tunnel] ERROR: SSH tunnel timeout to {host}")
        return False
    except Exception as e:
        print(f"  [tunnel] ERROR: SSH tunnel failed to {host}: {e}")
        return False

    time.sleep(1)
    timeout = cfg.get("bench_load_s", 10) + 5
    if not _poll_health(port=local_port, timeout=timeout):
        print(f"  [remote] ERROR: health check failed through tunnel :{local_port}")
        return False
    session_tunnels.add(host)
    return True


def remote_deactivate(model_id: str) -> None:
    """Deactivate spot model — kill SSH tunnel only (model stays running on remote)."""
    cfg = MODELS[model_id]
    host = cfg.get("host", "local")
    if host == "local":
        return

    local_port = cfg["local_port"]
    _kill_ssh_tunnel(local_port)
    print(f"  [remote] Deactivated {model_id} tunnel :{local_port}")


def close_all_tunnels(tunnels_open: Set[str]) -> None:
    """Close all SSH tunnels tracked in tunnels_open set."""
    for host in list(tunnels_open):
        for model_id, cfg in MODELS.items():
            if cfg.get("host") == host:
                _kill_ssh_tunnel(cfg["local_port"])
                break
    print(f"  [tunnel] Session tunnels closed ({len(tunnels_open)} were open)")
    tunnels_open.clear()

