#!/usr/bin/env python3
# Status: production
# Path: imported by — production scripts
"""State file I/O — save and load state.yaml."""
from pathlib import Path

import yaml


def save_state(structural, metrics, state_file: Path, references=None, phases=None):
    doc = {"structural": structural, "metrics": metrics}
    if references:
        doc["references"] = references
    if phases:
        doc["phases"] = phases
    state_file.write_text(yaml.dump(doc, default_flow_style=False, allow_unicode=True, sort_keys=False, width=120))


def load_previous_state(state_file: Path):
    if state_file.exists():
        with open(state_file) as f:
            return yaml.safe_load(f) or {}
    return None

