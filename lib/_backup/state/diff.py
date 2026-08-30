#!/usr/bin/env python3
# Status: production
# Path: imported by — production scripts
"""Structural hashing and diff for state change detection."""
import hashlib
import json


def structural_hash(data) -> str:
    payload = json.dumps(data, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


def diff_structural(prev, curr) -> list:
    """Recursive diff between two structural dicts. Returns list of {path, type, before?, after?}."""
    changes = []

    def _walk(prefix, a, b):
        if isinstance(a, dict) and isinstance(b, dict):
            for k in sorted(set(a.keys()) | set(b.keys())):
                _walk(f"{prefix}.{k}", a.get(k), b.get(k))
        elif isinstance(a, list) and isinstance(b, list):
            a_idx = {}
            b_idx = {}
            for item in a:
                if isinstance(item, dict):
                    key = item.get("name") or item.get("device") or json.dumps(item, sort_keys=True)
                    a_idx[key] = item
            for item in b:
                if isinstance(item, dict):
                    key = item.get("name") or item.get("device") or json.dumps(item, sort_keys=True)
                    b_idx[key] = item
            for k in sorted(set(a_idx.keys()) | set(b_idx.keys())):
                if k not in a_idx:
                    changes.append({"path": f"{prefix}[{k}]", "type": "added", "after": b_idx[k]})
                elif k not in b_idx:
                    changes.append({"path": f"{prefix}[{k}]", "type": "removed", "before": a_idx[k]})
                else:
                    _walk(f"{prefix}[{k}]", a_idx[k], b_idx[k])
        elif a != b:
            changes.append({"path": prefix, "type": "changed", "before": a, "after": b})

    _walk("structural", prev, curr)
    return changes

