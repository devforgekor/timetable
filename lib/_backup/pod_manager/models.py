#!/usr/bin/env python3
# Status: production
# Path: lib/pod_manager/models.py — re-export from lib.model_registry (SSOT)
"""Model metadata — re-exported from lib.model_registry for backward compat.

SSOT is lib.model_registry. All new code should import directly from there.
"""

from lib.model_registry import (  # noqa: F401 — re-export for backward compat
    DAY_PHASE_MODELS,
    MODEL_METADATA,
    NIGHT_MODELS,
)
