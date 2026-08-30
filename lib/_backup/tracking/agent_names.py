#!/usr/bin/env python3
# Status: production
# Path: imported by — production scripts
"""Single source of truth for agent name normalization.

All code paths that set or read agent names MUST go through normalize().
"""
AGENT_MAP = {
    "claude": "claude-code",
    "copilot": "copilot",
    "copilot-cli": "copilot",
    "gemini": "gemini",
    "qwen": "qwen",
    "deepseek": "deepseek",
    "aider": "aider",
    "cline": "cline",
    "opencode": "opencode",
}


def normalize(name: str) -> str:
    """Return canonical agent name, or the original if not mapped."""
    return AGENT_MAP.get(name.lower(), name.lower())

