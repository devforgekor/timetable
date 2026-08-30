# Status: production
import os, sys, json
from lib.common import log
try:
    from sentence_transformers import SentenceTransformer
except ImportError:
    SentenceTransformer = None

VERIFY_SCHEMA = {
    "required": ["final_verdict", "confidence", "summary"],
    "additionalProperties": False,
    "properties": {
        "final_verdict":  {"type": "string", "enum": ["PASS", "FAIL", "NEEDS_REVIEW", "ESCALATE"]},
        "confidence":     {"type": "integer"},
        "summary":        {"type": "string"},
        "reasoning":      {"type": "string"},
        "action":         {"type": "string"},
        "verification_items": {"type": "array"},
        "schema_version": {"type": "integer"},
    },
}

PRJ_RESULT_SCHEMA = {
    "required": ["P_score", "R_score", "consensus", "decision"],
    "additionalProperties": True,
    "properties": {
        "P_score":    {"type": "integer"},
        "R_score":    {"type": "integer"},
        "consensus":  {"type": "integer"},
        "decision":   {"type": "string", "enum": ["APPROVED", "REJECT"]},
        "approved":   {"type": "array"},
        "rejected":   {"type": "array"},
        "schema_version": {"type": "integer"},
    },
}

HANDOFF_SCHEMA = {
    "required": ["source"],
    "additionalProperties": True,
    "properties": {
        "source":         {"type": "string"},
        "approved_ids":   {"type": "array"},
        "rejected_ids":   {"type": "array"},
        "schema_version": {"type": "integer"},
        "checksum":       {"type": "string"},
    },
}

def load_input(INPUT_OVERRIDE=None):
    if INPUT_OVERRIDE and os.path.exists(INPUT_OVERRIDE):
        try:
            with open(INPUT_OVERRIDE, "r") as f:
                return json.load(f)
        except Exception as e:
            log(f"Failed to load override input: {e}")
    
    SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    EXPER_DIR = os.path.join(SCRIPTS_DIR, "..", "data", "experiment")
    EVENTS_DIR = os.path.join(EXPER_DIR, "events")
    
    # Try to find last extracted event
    evs = sorted([f for f in os.listdir(EVENTS_DIR) if f.startswith("event_") and f.endswith(".json")])
    if not evs:
        log("No events found in " + EVENTS_DIR)
        return {}
    
    latest = os.path.join(EVENTS_DIR, evs[-1])
    try:
        with open(latest, "r") as f:
            return json.load(f)
    except Exception as e:
        log(f"Failed to load latest event {latest}: {e}")
        return {}

def _schema_for_label(label: str) -> dict:
    if label == "P" or label == "R" or label == "J":
        return PRJ_RESULT_SCHEMA
    if "verify" in label.lower():
        return VERIFY_SCHEMA
    if "handoff" in label.lower():
        return HANDOFF_SCHEMA
    return {}

def _log_schema_warnings(data: dict, label: str, model: str) -> None:
    schema = _schema_for_label(label)
    if not schema: return
    # Basic check (original had more complex logic probably, but this is a placeholder)
    missing = [k for k in schema.get("required", []) if k not in data]
    if missing:
        log(f"  [SCHEMA WARNING] {model} {label} missing fields: {missing}")

def _dedup_findings(findings, threshold=0.92):
    if len(findings) < 2:
        return findings
    try:
        if SentenceTransformer is None:
             return findings
        embedder = SentenceTransformer("all-MiniLM-L6-v2")
        texts = [f"{f.get('description','')} {f.get('file','')}" for f in findings]
        embs = embedder.encode(texts, normalize_embeddings=True)
        keep = []
        for i in range(len(findings)):
            import numpy as np
            if all(np.dot(embs[i], embs[j]) < threshold for j in keep):
                keep.append(i)
        return [findings[i] for i in keep]
    except Exception as e:
        log(f"  Dedup failed (proceeding without): {e}")
        return findings

def _trim_handoff(text, label=""):
    # Mock implementation of the original trim logic
    if not text: return ""
    lines = text.splitlines()
    if len(lines) > 500:
        return "\n".join(lines[:250]) + "\n... [TRUNCATED] ...\n" + "\n".join(lines[-250:])
    return text
