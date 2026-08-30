#!/usr/bin/env python3
# Status: production
"""Pipeline schemas and validation helpers."""

import json

from lib.llm.json_parser import save_dlq, validate_schema
from lib.common import log


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


def _schema_for_label(label: str) -> dict:
    if "day_verify" in label or "night_verify" in label or "verify" in label:
        return VERIFY_SCHEMA
    if label.startswith("P_") or label.startswith("J_") or label.startswith("R_"):
        return PRJ_RESULT_SCHEMA
    if "handoff" in label:
        return HANDOFF_SCHEMA
    return {}


def _log_schema_warnings(data: dict, label: str, model: str) -> None:
    schema = _schema_for_label(label)
    if not schema:
        return
    errs = validate_schema(data, schema)
    if errs:
        log(f"  Schema warnings ({label}): {'; '.join(errs[:5])}")
        save_dlq(json.dumps(data, ensure_ascii=False), stage=label + "_schema",
                 model=model, error="; ".join(errs[:3]), attempt=1)


def _dedup_findings(findings, threshold=0.92):
    if len(findings) < 2:
        return findings
    try:
        from sentence_transformers import SentenceTransformer
        embedder = SentenceTransformer("all-MiniLM-L6-v2")
        texts = [f"{f.get('description','')} {f.get('file','')}" for f in findings]
        embs = embedder.encode(texts, normalize_embeddings=True)
        keep = []
        for i in range(len(findings)):
            if all(sum(embs[i] * embs[j]) < threshold for j in keep):
                keep.append(i)
        return [findings[i] for i in keep]
    except Exception as e:
        log(f"  Dedup failed (proceeding without): {e}")
        return findings
