#!/usr/bin/env python3
# Status: production
# Path: imported by — pipelines/exp_runner.py
"""Phase snapshot save/restore and code transformation for 5-phase experiments."""

import os, shutil, subprocess, sys
from datetime import datetime, timezone

SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ARCHIVE_DIR = os.path.join(SCRIPTS_DIR, "_archive")


def log(msg):
    t = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{t}] {msg}", flush=True)


KEY_FILES = ["pipelines/prj_cycle.py", "pipelines/extract.py",
             "night_cycle.sh", "day_cycle.sh"]


def save_snapshot(phase):
    d = os.path.join(ARCHIVE_DIR, f"phase{phase}")
    for fname in KEY_FILES:
        src = os.path.join(SCRIPTS_DIR, fname)
        if os.path.exists(src):
            dst = os.path.join(d, fname)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
    log(f"Snapshot saved: _archive/phase{phase}/")


def restore_snapshot(phase):
    d = os.path.join(ARCHIVE_DIR, f"phase{phase}")
    for fname in KEY_FILES:
        src = os.path.join(d, fname)
        dst = os.path.join(SCRIPTS_DIR, fname)
        if os.path.exists(src):
            shutil.copy2(src, dst)
    log(f"Restored from _archive/phase{phase}/")


PHASE_FLAGS = {
    0: ["--rubric-off", "--feedback-off"],
    1: ["--structural", "--rubric-off", "--feedback-off"],
    2: ["--structural", "--feedback-off"],
    3: ["--structural", "--rubric-off"],
    4: ["--structural"],
}


def apply_transform(phase):
    """Apply phase transformation via external transform_prj.py script."""
    fp = os.path.join(SCRIPTS_DIR, "pipelines", "prj_cycle.py")
    tscript = os.path.join(SCRIPTS_DIR, "transform_prj.py")
    flags = PHASE_FLAGS.get(phase, [])

    if not os.path.exists(tscript):
        log("ERROR: transform_prj.py not found")
        return False

    with open(fp) as f:
        original = f.read()

    cmd = [sys.executable, tscript] + flags
    result = subprocess.run(cmd, input=original, capture_output=True, text=True, timeout=30)

    if result.returncode != 0:
        log(f"transform_prj {' '.join(flags)} failed: {result.stderr}")
        return False

    with open(fp, "w") as f:
        f.write(result.stdout)
    log(f"Phase {phase} transformation applied ({' '.join(flags)})")
    return True
