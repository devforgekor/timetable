#!/usr/bin/env python3
# Status: production
# Path: imported by — production scripts
"""MOTD generation — formatted system overview for /etc/motd."""
import re
from pathlib import Path

GREEN = "\033[0;32m"
CYAN = "\033[0;36m"
YELLOW = "\033[1;33m"
RED = "\033[0;31m"
WHITE = "\033[0;37m"
BOLD = "\033[1m"
NC = "\033[0m"


def generate_motd(structural, metrics, motd_file: Path, token_usage_base: int = 1_000_000):
    lines = []

    lines.append(f"{CYAN}{'='*64}{NC}")

    # System
    sys_m = metrics.get("system", {})
    mem = sys_m.get("memory", {})
    cpu = sys_m.get("cpu_load", [])
    zram = sys_m.get("zram", "N/A")
    cpu_str = ", ".join(f"{v:.2f}" for v in cpu) if cpu else "N/A"

    cpu_color = WHITE
    if cpu and cpu[0] >= 3.5:
        cpu_color = RED
    elif cpu and cpu[0] >= 2.5:
        cpu_color = YELLOW

    mem_pct = mem.get("percent", 0)
    mem_color = WHITE
    if mem_pct >= 90:
        mem_color = RED
    elif mem_pct >= 80:
        mem_color = YELLOW

    lines.append(f"{GREEN}[SYSTEM]{NC}")
    col1_w, col2_w = 26, 25
    lines.append(f"  {WHITE}{'Spec:':<10} {'OCI ARM Flex 4ocpu 24ram':<{col1_w}}   {'OS:':<5} {'Oracle Linux Server 9.7':<{col2_w}}   {'Arch:':<7} aarch64{NC}")
    mem_val = f"{mem.get('used','?')}/{mem.get('total','?')}"
    lines.append(f"  {cpu_color}{'CPU Load:':<10} {cpu_str:<{col1_w}}{NC}   {mem_color}{'Mem:':<5} {mem_val:<{col2_w}}{NC}   {'Zram:':<7} {zram if zram else 'none'}")

    # Storage — LVM
    lines.append(f"\n{GREEN}[STORAGE — LVM]{NC}")
    lines.append(f"  {BOLD}{'Device':<21} {'Size':>6} {'Used':>6} {'Avail':>6} {'Use%':>5}  Mounted{NC}")
    for s in metrics.get("storage_use", []):
        pct = s.get("use_pct", 0)
        if pct >= 90:
            color = RED
        elif pct >= 80:
            color = YELLOW
        else:
            color = WHITE
        lines.append(f"  {color}{s['device']:<21} {s['size']:>6} {s['used']:>6} {s['avail']:>6} {s['use_pct']:>4}%  {s['mount']}{NC}")

    # Containers
    lines.append(f"\n{GREEN}[CONTAINERS]{NC}")
    containers = structural.get("containers", [])

    def _simple_port(c):
        name = c.get("name", "")
        if name.startswith("pod:"):
            return "-"
        ports_str = c.get("ports", "")
        if not ports_str:
            return ""
        host_ports = []
        for mapping in ports_str.split(", "):
            # Extract host port from "host_ip:host_port->container_port/proto"
            m = re.search(r":(\d+)->", mapping)
            if not m:
                # Fallback: extract container port number
                m = re.search(r"(\d+)", mapping)
            if m:
                host_ports.append(m.group(1))
        return ", ".join(host_ports) if host_ports else ""

    if containers:
        max_name = max((len(c['name']) for c in containers), default=12)
        name_w = max(max_name + 2, 14)
        lines.append(f"  {BOLD}{'NAMES':<{name_w}} {'UPTIME':<20} {'HEALTH':<10} {'PORT'}{NC}")
        for c in containers:
            status = c.get("status", "")
            if status.startswith("Up "):
                uptime = status[3:]
            elif status.startswith("Up"):
                uptime = status
            else:
                uptime = status
            if "(healthy)" in status:
                health = f"{GREEN}healthy{NC}"
            elif "(unhealthy)" in status:
                health = f"{RED}unhealthy{NC}"
            elif "starting" in status.lower():
                health = f"{YELLOW}starting{NC}"
            else:
                health = "-"
            port = _simple_port(c)
            color = GREEN if "Up" in status else RED
            lines.append(f"  {color}{c['name']:<{name_w}} {uptime:<20} {health:<10} {port}{NC}")

    # Network
    net = structural.get("network", {})
    ip = net.get("ip", "")
    llm_port = ""
    for c in containers:
        if c.get("model") or any(kw in c.get("name", "").lower() for kw in ("llm", "ollama", "llama")):
            port = _simple_port(c)
            if port and port != "-":
                llm_port = port
                break
    lines.append(f"\n{GREEN}[NETWORK]{NC}")
    net_parts = [f"IP: {ip}", "SSH: 22"]
    if llm_port:
        net_parts.append(f"LLM: {llm_port}")
    net_parts.append("Netdata: 19999")
    lines.append("   ".join(net_parts))

    # Tokens
    token = metrics.get("tokens", {})
    lines.append(f"\n{GREEN}[TOKENS]{NC}")
    total = token.get("total", {})
    total_tokens = total.get("total_tokens", 0)
    if total_tokens:
        pct = round(total_tokens / token_usage_base * 100)
        lines.append(f"  copilot: {pct}%")
    else:
        lines.append("  copilot: no token data")

    # Services
    svc_parts = []
    for svc in structural.get("services", []):
        status = svc.get("status", "unknown")
        if status == "active":
            color = WHITE
        elif status == "inactive":
            color = YELLOW
        else:
            color = RED
        svc_parts.append(f"{svc['name']}: {color}{status}{NC}")
    lines.append(f"\n{GREEN}[SERVICES]{NC}")
    lines.append("  " + "   ".join(svc_parts))

    # Validation alerts
    validation = metrics.get("validation")
    if validation and validation.get("status") == "fail":
        lines.append(f"\n{RED}[VALIDATION ALERTS — {validation.get('last_run', '?')}]{NC}")
        for m in validation.get("mismatches", []):
            lines.append(f"  {RED}[MISMATCH]{NC} {m['item']}")
            lines.append(f"          expected: {YELLOW}{m['expected']}{NC}  actual: {RED}{m['actual']}{NC}")
        lines.append(f"{RED}  Fix CLAUDE.yaml or blueprint.yaml and re-run --validate{NC}")

    lines.append(f"{CYAN}{'='*64}{NC}")

    motd_file.write_text("\n".join(lines) + "\n")

