#!/usr/bin/env python3
# Status: production
# Path: imported by — production scripts
"""Container and service discovery for state_collector."""

import json
import os
import re
import shlex
import urllib.request
from pathlib import Path


def discover_services() -> list:
    """Auto-discover tracked services from systemd user + system units.
    Returns list of (unit_name, display_name, scope) tuples. Scope: 'user' or 'system'."""
    services = []

    def is_podman_transient_unit(name):
        return bool(re.search(r"healthcheck|conmon|run-[0-9a-f]{8,}", name))

    # Auto-discover user services: container pods + devforge-* services with timer
    timer_names = set()
    for line in _run_lines(
        ["systemctl", "--user", "list-unit-files", "--no-legend", "--type=timer"]
    ):
        if line.strip():
            timer_names.add(line.strip().split()[0].replace(".timer", ""))

    for line in _run_lines(
        ["systemctl", "--user", "list-unit-files", "--no-legend", "--type=service"]
    ):
        if not line.strip():
            continue
        name = line.strip().split()[0].replace(".service", "")
        if is_podman_transient_unit(name):
            continue
        if name.startswith("container-"):
            display = name.removeprefix("container-")
            services.append((name, display, "user"))
        elif name.startswith("devforge-") and name in timer_names:
            display = name.removeprefix("devforge-").replace("-", " ")
            services.append((name, display, "user"))

    known_system = {"caddy", "netdata"}
    for line in _run_lines(["systemctl", "list-unit-files", "--no-legend", "--type=service"]):
        if not line.strip():
            continue
        name = line.strip().split()[0].replace(".service", "")
        if name in known_system:
            services.append((name, name, "system"))

    return services


def query_inference_model(port: int = 8081) -> str:
    """Query running inference server for active model name. Returns empty string on failure."""
    try:
        req = urllib.request.Request(f"http://localhost:{port}/v1/models")
        resp = urllib.request.urlopen(req, timeout=5)
        data = json.loads(resp.read())
        models = data.get("data", [])
        if models:
            return models[0].get("id", "")
    except Exception:
        pass
    return ""


def collect_container_flags(name: str) -> str:
    """Parse llama-server flags from a podman systemd user unit file."""
    user_dir = Path(os.path.expanduser("~/.config/systemd/user"))
    candidates = [
        user_dir / f"{name}.service",
        user_dir / f"container-{name}.service",
    ]
    unit_path = next((str(p) for p in candidates if p.exists()), "")
    if not unit_path:
        return ""
    content = Path(unit_path).read_text()
    match = re.search(
        r"ExecStart=/usr/bin/podman run\s+(.+?)(?:^[A-Z]\S+=|\Z)", content, re.MULTILINE | re.DOTALL
    )
    if not match:
        return ""
    args_block = match.group(1)
    args_block = args_block.replace("\\\n", " ").replace("\n", " ").strip()
    image_match = re.search(r"\S+\.io/\S+:\S+", args_block)
    if not image_match:
        return ""
    server_args = args_block[image_match.end() :].strip()
    flags = []
    try:
        tokens = shlex.split(server_args)
        i = 0
        while i < len(tokens):
            if tokens[i].startswith("-"):
                flags.append(tokens[i])
                if i + 1 < len(tokens) and not tokens[i + 1].startswith("-"):
                    flags.append(tokens[i + 1])
                    i += 1
            i += 1
    except ValueError:
        pass
    return " ".join(flags)


def _run_lines(cmd, timeout=15):
    import subprocess

    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout.split("\n")
    except Exception:
        return []
