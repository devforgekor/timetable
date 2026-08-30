#!/usr/bin/env python3
# Status: production
# Path: imported by — production scripts
"""YAML load/save helpers."""
from pathlib import Path

import yaml


def load_yaml(path: Path) -> dict:
    if path.exists():
        with open(path) as f:
            return yaml.safe_load(f) or {}
    return None


def save_yaml(path: Path, data) -> None:
    path.write_text(yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False, width=120))

