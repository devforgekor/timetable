# Status: production
# Path: imported by scripts/ modules
"""Tracking — phase detection, dependency tracking, agent name normalization."""
from lib.tracking.phase_tracker import (
    auto_update_phase_documents,
    auto_update,
    collect,
    collect_phase_summary,
    evaluate_phase_detection_rule,
    scan_phases_md,
    update_phases_md,
    update_blueprint_yaml,
)
from lib.tracking.dependency_tracker import collect_references
from lib.tracking.agent_names import normalize
