#!/usr/bin/env python3
# Status: production
"""Shared pipeline utilities — PipelineState, llm_call, call_one, schema, prompts."""

import json
import os
import subprocess
import sys
import time

from lib.common import log, timestamp
from lib.pipeline_common.helpers import abort, slack_send, strip_code_fence

SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPTS_DIR)

from lib.db import psql_json
from lib.llm.json_parser import parse_llm_json as _extract_json
from lib.llm.json_parser import save_dlq, validate_schema
from lib.llm_client import call_llm, resolve_model
from lib.pod_manager import (
    MODEL_METADATA,
    NIGHT_MODELS,
    TIMEOUT,
    ensure_model,
    model_info,
)
from lib.token_budget import TokenBudget

EXPER_DIR = os.path.join(SCRIPTS_DIR, "..", "data", "experiment")
PIPELINE_DIR = os.path.join(SCRIPTS_DIR, "..", "data", "pipeline_run")
EVENTS_DIR = os.path.join(PIPELINE_DIR, "events")
os.makedirs(EXPER_DIR, exist_ok=True)
os.makedirs(PIPELINE_DIR, exist_ok=True)
os.makedirs(EVENTS_DIR, exist_ok=True)

from lib.pipeline_common.llm import JUDGE_MODEL, PROPOSER_MODEL, REFLECTOR_MODEL, call_one, llm_call
from lib.pipeline_common.prompts import (
    HANDOFF_SYSTEM_PROMPT,
    JUDGE_SYSTEM_PROMPT,
    MOCK_RESULT,
    PROPOSER_SYSTEM_PROMPT,
    REFLECTOR_SYSTEM_PROMPT,
    RUBRIC,
    RUBRIC_SYSTEM_PROMPT,
    VERIFIER_SYSTEM_PROMPT,
)
from lib.pipeline_common.schema import (
    HANDOFF_SCHEMA,
    PRJ_RESULT_SCHEMA,
    VERIFY_SCHEMA,
    _dedup_findings,
    _log_schema_warnings,
    _schema_for_label,
)
from lib.pipeline_common.state import PipelineState, compile_handoff, compile_handoff_single


def save(phase, tag, data):
    fname = f"exp_{phase}_{tag}.json"
    fpath = os.path.join(PIPELINE_DIR, fname)
    with open(fpath, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return fpath


def load_input(input_override=None):
    if input_override:
        fpath = input_override
        if not os.path.isabs(fpath):
            fpath = os.path.join(SCRIPTS_DIR, "..", fpath)
    else:
        fpath = os.path.join(SCRIPTS_DIR, "..", "pipeline_input", "consolidated_input_compact.json")
        if not os.path.exists(fpath):
            fpath = fpath.replace("_compact", "")
    with open(fpath) as f:
        return json.load(f)


def log_phase_header(title):
    log(f"\n{'=' * 60}")
    log(title)
    log(f"{'=' * 60}")
