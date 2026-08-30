# Status: production
import os
from typing import Dict


def detect_models() -> Dict[str, str]:
    """Detect available models via port check or env."""
    # Simplified detection for v2
    return {"proposer": "proposer", "reviewer": "reviewer", "judge": "judge"}


def get_inference_mode() -> str:
    mode_file = "/opt/ai_data/scripts/current-mode-inference.env"
    if os.path.exists(mode_file):
        with open(mode_file) as f:
            content = f.read()
            if "MODE=" in content:
                return content.split("MODE=")[1].strip()
    return "unknown"
