#!/usr/bin/env python3
# Status: production
# Path: imported by — production scripts
"""Memory overview line for CLAUDE.yaml — generated from live system data."""
from lib.infra.subprocess import run_subprocess, run_lines


def build_memory_line():
    """Generate memory overview line from live system data."""
    mem_total = "?"
    for line in run_subprocess(["free", "-h"]).split("\n"):
        if line.startswith("Mem:"):
            mem_total = line.split()[1]
            break

    swap_total = 0
    for line in run_lines(["swapon", "--show", "--noheadings", "--bytes"]):
        parts = line.split()
        if len(parts) >= 3:
            swap_total += int(parts[2])
    swap_str = f"{swap_total / (1024**3):.1f}G" if swap_total else "0G"
    if swap_str.endswith(".0G"):
        swap_str = swap_str.replace(".0G", "G")

    zram = ""
    zram_lines = run_lines(["zramctl"])
    if len(zram_lines) >= 2:
        parts = zram_lines[1].split()
        if len(parts) >= 5:
            zram = f" + {parts[2]} zram ({parts[4]})"

    swappiness = run_subprocess(["sysctl", "-n", "vm.swappiness"]).strip() or "?"

    return f"{mem_total}{zram} + {swap_str} swap (swappiness={swappiness})"

