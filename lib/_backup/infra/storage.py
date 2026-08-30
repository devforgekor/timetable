#!/usr/bin/env python3
# Status: production
# Path: imported by — production scripts
"""Zram cycle tracking."""
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

from lib.infra.subprocess import run_lines


def track_zram_cycles(state_file: Path):
    """Track zram active/inactive cycles. Returns (current_active, daily_cycles, total_cycles, zram_parts)."""
    try:
        lines = run_lines(["zramctl"])
        if len(lines) >= 2:
            parts = lines[1].split()
            if len(parts) >= 4:
                data_str = parts[3]
                current_active = 0 if data_str in ("0B", "0") else 1

                kst = timezone(timedelta(hours=9))
                today = datetime.now(kst).strftime("%Y-%m-%d")

                prev_active = None
                daily_cycles = 0
                total_cycles = 0
                last_reset_date = None
                try:
                    state_file.parent.mkdir(parents=True, exist_ok=True)
                    with open(state_file) as f:
                        state = json.load(f)
                        prev_active = state.get("prev_active")
                        daily_cycles = state.get("daily_cycles", 0)
                        total_cycles = state.get("total_cycles", 0)
                        last_reset_date = state.get("last_reset_date")
                except Exception:
                    pass

                if last_reset_date != today:
                    daily_cycles = 0
                    last_reset_date = today

                if prev_active is not None and prev_active != current_active:
                    daily_cycles += 1
                    total_cycles += 1

                try:
                    with open(state_file, "w") as f:
                        json.dump({
                            "prev_active": current_active,
                            "daily_cycles": daily_cycles,
                            "total_cycles": total_cycles,
                            "last_reset_date": last_reset_date
                        }, f)
                except Exception:
                    pass

                return current_active, daily_cycles, total_cycles, parts
    except Exception:
        pass

    return None, 0, 0, []

