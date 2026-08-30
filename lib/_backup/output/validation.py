#!/usr/bin/env python3
# Status: production
# Path: imported by — production scripts
"""Validation — compare CLAUDE.yaml + blueprint.yaml claims against live data."""
import re
from pathlib import Path

from lib.infra.containers import collect_container_flags
from lib.infra.subprocess import run_subprocess
from lib.output.memory_line import build_memory_line
from lib.output.yaml_io import load_yaml


def run_validation(structural, claude_file: Path, server_dir: Path):
    """Compare CLAUDE.yaml + blueprint.yaml claims against live data.
    Returns (status, mismatches) where status is 'pass' or 'fail'."""
    mismatches = []
    claude = load_yaml(claude_file) or {}
    blueprint = load_yaml(server_dir / "blueprint.yaml") or {}

    # 1. CLAUDE.yaml services vs live systemd
    claude_services = {s["name"]: s.get("status") for s in claude.get("services", [])}
    for s in structural.get("services", []):
        claude_status = claude_services.get(s["name"])
        if claude_status and claude_status != s["status"]:
            mismatches.append({
                "item": f"CLAUDE.yaml service {s['name']}",
                "expected": claude_status,
                "actual": s["status"],
            })

    # 2. CLAUDE.yaml memory vs live
    live_memory = build_memory_line()
    claude_memory = claude.get("overview", {}).get("memory", "")
    if claude_memory and claude_memory != live_memory:
        mismatches.append({
            "item": "CLAUDE.yaml overview.memory",
            "expected": claude_memory,
            "actual": live_memory,
        })

    # 3. CLAUDE.yaml container flags vs live unit file (only for llama-server container)
    claude_flags = claude.get("containers", {}).get("flags", "")
    if claude_flags:
        for c in structural.get("containers", []):
            if not c.get("model"):
                continue
            live_flags = collect_container_flags(c["name"])
            if live_flags and claude_flags != live_flags:
                mismatches.append({
                    "item": f"CLAUDE.yaml containers.flags ({c['name']})",
                    "expected": claude_flags,
                    "actual": live_flags,
                })

    # 4. CLAUDE.yaml model vs actual gguf file (strip size annotation like "(8.1GB)")
    claude_model = claude.get("containers", {}).get("model", "")
    claude_model_base = re.sub(r"\s*\([^)]*\)", "", claude_model).strip()
    if claude_model_base:
        for c in structural.get("containers", []):
            live_model = c.get("model", "")
            if live_model and claude_model_base != live_model:
                mismatches.append({
                    "item": "CLAUDE.yaml containers.model",
                    "expected": claude_model,
                    "actual": live_model,
                })

    # 5. Listening ports — check that claude network claims match actual ports
    live_ports = structural.get("network", {}).get("ports", [])
    claude_llm = claude.get("network", {}).get("llm_api", "")
    if "8080" in claude_llm and "8080" not in live_ports:
        mismatches.append({
            "item": "CLAUDE.yaml network.llm_api claims port 8080",
            "expected": "port 8080 listening",
            "actual": f"listening ports: {live_ports}",
        })

    # 6. Blueprint Phase completed items vs live
    for phase in blueprint.get("phases", []):
        if phase.get("status") != "complete":
            continue
        for item in (phase.get("completed") or []):
            ctx_match = re.search(r"ctx-size (\d+)", item)
            if ctx_match:
                claimed_ctx = ctx_match.group(1)
                for c in structural.get("containers", []):
                    if not c.get("model"):
                        continue
                    live_flags = collect_container_flags(c["name"])
                    if f"--ctx-size {claimed_ctx}" not in live_flags:
                        actual_ctx = re.search(r"--ctx-size (\d+)", live_flags)
                        mismatches.append({
                            "item": f"blueprint Phase {phase['phase']}: ctx-size {claimed_ctx}",
                            "expected": f"--ctx-size {claimed_ctx}",
                            "actual": f"--ctx-size {actual_ctx.group(1)}" if actual_ctx else "not found",
                        })
            mnt_match = re.search(r"mounted at (\S+)", item)
            if mnt_match:
                mnt_path = mnt_match.group(1).rstrip(",;")
                if not run_subprocess(["findmnt", mnt_path]):
                    mismatches.append({
                        "item": f"blueprint Phase {phase['phase']}: {mnt_path} mounted",
                        "expected": "mounted",
                        "actual": "not mounted",
                    })

    status = "fail" if mismatches else "pass"
    return status, mismatches

