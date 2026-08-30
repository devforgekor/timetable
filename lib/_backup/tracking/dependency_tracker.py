#!/usr/bin/env python3
# Status: production
# Path: imported by — production scripts
"""Reference tracking — external project watch + internal code observation.

Two sources, one output:
  1. External: GitHub releases API poll → new versions, breaking changes
  2. Internal: git grep patterns → usage trends over time

Integrated by state_collector every 15min → state.yaml#references
"""

import json
import subprocess
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REF_ROOT = Path("/opt/projects/server")
NOW = lambda: datetime.now(timezone.utc).isoformat()


WATCHLIST = {
    "llama.cpp": {
        "url": "https://github.com/ggml-org/llama.cpp",
        "api": "https://api.github.com/repos/ggml-org/llama.cpp/releases?per_page=2",
        "grep_patterns": ["llama.cpp", "ggml-org/llama.cpp", "--mlock", "--cache-ram", "IQ4_XS", "GGUF"],
        "category": "inference",
    },
    "DSPy": {
        "url": "https://github.com/stanfordnlp/dspy",
        "api": "https://api.github.com/repos/stanfordnlp/dspy/releases?per_page=2",
        "grep_patterns": ["stanfordnlp/dspy", "MIPROv2", "dspy\\."],
        "category": "prompt-optimization",
    },
    "LLMLingua": {
        "url": "https://github.com/microsoft/LLMLingua",
        "api": "https://api.github.com/repos/microsoft/LLMLingua/releases?per_page=2",
        "grep_patterns": ["LLMLingua", "microsoft/LLMLingua"],
        "category": "prompt-optimization",
    },
    "tenacity": {
        "url": "https://github.com/jd/tenacity",
        "api": "https://api.github.com/repos/jd/tenacity/releases?per_page=2",
        "grep_patterns": ["import tenacity", "from tenacity", "@retry\\(", "tenacity\\.retry"],
        "category": "dependency",
    },
    "Podman": {
        "url": "https://github.com/containers/podman",
        "api": "https://api.github.com/repos/containers/podman/releases?per_page=2",
        "grep_patterns": ["containers/podman", "Quadlet"],
        "category": "infrastructure",
    },
}


def _gh_api(url: str):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "DevForge/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


def poll_external() -> list[dict]:
    """Poll GitHub releases API for watched projects. Returns list of findings."""
    findings = []
    for name, cfg in WATCHLIST.items():
        releases = _gh_api(cfg["api"])
        if not releases:
            continue
        latest = releases[0]
        finding = {
            "project": name,
            "category": cfg["category"],
            "url": cfg["url"],
            "latest_version": latest.get("tag_name", "?"),
            "published_at": latest.get("published_at", "?"),
            "checked_at": NOW(),
        }
        # Check if pre-release / breaking
        finding["prerelease"] = bool(latest.get("prerelease", False))
        body = (latest.get("body") or "").lower()
        finding["mentions_breaking"] = any(w in body for w in ["breaking", "deprecated", "removed", "cve"])
        findings.append(finding)
    return findings


def scan_internal() -> list[dict]:
    """Scan codebase for watched project patterns. Returns usage stats."""
    results = []
    for name, cfg in WATCHLIST.items():
        files = set()
        for pat in cfg["grep_patterns"]:
            try:
                r = subprocess.run(
                    ["git", "-C", str(REF_ROOT), "grep", "-l", pat, "--",
                     ":!*_archive*", ":!*/__pycache__/*",
                     "*.py", "*.yaml", "*.sh", "*.container", "Dockerfile"],
                    capture_output=True, text=True, timeout=10,
                )
                if r.returncode == 0:
                    for line in r.stdout.strip().splitlines():
                        if line:
                            files.add(line)
            except subprocess.TimeoutExpired:
                pass
        results.append({
            "project": name,
            "category": cfg["category"],
            "files_using": sorted(files),
            "usage_count": len(files),
            "checked_at": NOW(),
        })
    return results


def collect_references() -> dict:
    """Return {project_name: {external, internal}} for state.yaml#references."""
    external = {r["project"]: r for r in poll_external()}
    internal = {r["project"]: r for r in scan_internal()}

    combined = {}
    for name, cfg in WATCHLIST.items():
        ext = external.get(name, {})
        int_ = internal.get(name, {})
        entry = {
            "category": cfg["category"],
            "url": cfg["url"],
            "external": {
                "latest": ext.get("latest_version", "?"),
                "published": ext.get("published_at", "?"),
                "prerelease": ext.get("prerelease", None),
                "breaking_mentioned": ext.get("mentions_breaking", None),
            },
            "internal": {
                "usage_count": int_.get("usage_count", 0),
                "files": int_.get("files_using", [])[:5],
            },
            "checked_at": NOW(),
        }
        combined[name] = entry

    return combined


# Backward-compat alias
collect = collect_references

