#!/usr/bin/env python3
# Status: production
# Path: imported by — production scripts
"""CLAUDE.yaml auto-update — sync storage/services/network from live data."""

import re
from pathlib import Path

from lib.infra.subprocess import run_lines
from lib.output.memory_line import build_memory_line
from lib.output.yaml_io import load_yaml, save_yaml


def update_claude_yaml(structural, claude_file: Path):
    """Auto-update storage/services/network in CLAUDE.yaml from live data.
    Preserves manual sections: overview, rules, entry_points, tracked, handover."""
    claude = load_yaml(claude_file)
    if not claude:
        return

    storage_list = structural.get("storage", [])
    vgs = {}
    for s in storage_list:
        vg = s["vg"]
        if vg not in vgs:
            vgs[vg] = {"lvs": [], "total_lv": 0.0}
        vgs[vg]["lvs"].append(
            {
                "lv": s["lv"],
                "size": s["size"],
                "mount": s.get("mount") or "unmounted",
            }
        )
        sz_str = s["size"].rstrip("G")
        vgs[vg]["total_lv"] += float(sz_str)

    for line in run_lines(["sudo", "vgs", "--noheadings", "-o", "vg_name,vg_size", "--units", "g"]):
        parts = line.strip().split()
        if len(parts) >= 2 and parts[0] in vgs:
            vg_total = float(parts[1].rstrip("g"))
            free = vg_total - vgs[parts[0]]["total_lv"]
            vgs[parts[0]]["free"] = f"{free:.1f}G" if free != int(free) else f"{int(free)}G"

    old_storage = claude.get("storage", {})
    for disk_key, disk_data in old_storage.items():
        vg = disk_data.get("vg")
        if vg and vg in vgs:
            disk_data["lvs"] = vgs[vg]["lvs"]
            disk_data["free"] = vgs[vg].get("free", disk_data.get("free", "?"))

    new_services = structural.get("services", [])
    old_services = claude.get("services", [])
    old_by_name = {s["name"]: s for s in old_services}
    merged = []
    for ns in new_services:
        entry = {"name": ns["name"], "status": ns["status"]}
        old = old_by_name.get(ns["name"], {})
        if "type" in old:
            entry["type"] = old["type"]
        merged.append(entry)
    claude["services"] = merged

    new_net = structural.get("network", {})
    old_net = claude.get("network", {})
    if old_net and new_net.get("ip"):
        old_net["ip"] = new_net["ip"]
    claude["network"] = old_net

    claude["overview"]["memory"] = build_memory_line()

    new_containers = structural.get("containers", [])
    old_containers = claude.get("containers", {})
    if new_containers and isinstance(old_containers, dict):
        llm_c = None
        for c in new_containers:
            if c["name"].startswith("pod:"):
                continue
            ports = c.get("ports", "")
            img = c.get("image", "")
            if "8081" in ports or "llama.cpp" in img:
                llm_c = c
                break
        if llm_c:
            port_match = re.search(r"(\d+)(?:-\d+)?->\d+", llm_c.get("ports", ""))
            llm_port = int(port_match.group(1)) if port_match else None
            model_name = query_inference_model(llm_port) if llm_port else ""
            old_containers["model"] = model_name or old_containers.get("model", "")
            old_containers["image"] = llm_c.get("image") or old_containers.get("image", "")
            if llm_c.get("flags"):
                old_containers["flags"] = llm_c["flags"]
            elif model_name:
                old_containers["flags"] = "(dynamic entrypoint — query /v1/models for live config)"
        claude["containers"] = old_containers

    new_containers = structural.get("containers", [])
    old_net = claude.get("network", {})
    for c in new_containers:
        if c["name"].startswith("pod:"):
            continue
        ports = c.get("ports", "")
        img = c.get("image", "")
        if "llama.cpp" in img or "8081" in ports:
            m = re.search(r"(\d+)(?:-\d+)?->\d+", ports)
            if m:
                old_net["llm_api"] = f"localhost:{m.group(1)}"
            elif "llm_api" not in old_net:
                old_net["llm_api"] = "localhost:8081"
            break
    claude["network"] = old_net

    save_yaml(claude_file, claude)
