# Status: production
# Path: imported by — pipelines/extract.py, pipelines/enrich.py, pipelines/extract_verify.py, pipelines/post_extract_supplement.py, tests/
"""Extract LLM subpackage — re-exports from all submodules.

Submodules:
  chunking.py — text splitting, paragraph grouping, compound expansion
  edc.py — EDC predicate/entity normalization, QC pipeline
  parser.py — JSON error recovery and parsing
  _core.py — constants, prompts, token calc, embed mgmt, _extract_edcr_freeform
"""

from ._core import (
    _SIGTERM_RECEIVED,
    _SYSTEM_TEXT_EXTRACT_FREE,
    _SYSTEM_TEXT_EXTRACT_FREE_8B,
    _SYSTEM_USER_EXTRACT_FREE,
    _SYSTEM_USER_EXTRACT_FREE_8B,
    SYSTEM_DAY_EXTRACT,
    SYSTEM_DESCRIBE_FILE,
    SYSTEM_FALLBACK,
    _calc_max_tokens,
    _calc_timeout,
    _call_with_8082_retry,
    _checkpoint_sections,
    _cleanup_all_llms,
    _extract_edcr_freeform,
    _merge_usage,
    _sigterm_handler,
)
from .chunking import (
    _build_section_prefix,
    _parse_heading_level,
    _split_atomic,
)
from .edc import (
    _fix_status_hallucination,
    _quality_check_facts,
)
from .parser import (
    _clean_extraction_json,
    _parse_json,
)

__all__ = [
    "SYSTEM_DAY_EXTRACT",
    "SYSTEM_DESCRIBE_FILE",
    "SYSTEM_FALLBACK",
    "_SIGTERM_RECEIVED",
    "_SYSTEM_TEXT_EXTRACT_FREE",
    "_SYSTEM_TEXT_EXTRACT_FREE_8B",
    "_SYSTEM_USER_EXTRACT_FREE",
    "_SYSTEM_USER_EXTRACT_FREE_8B",
    "_build_section_prefix",
    "_calc_max_tokens",
    "_calc_timeout",
    "_call_with_8082_retry",
    "_checkpoint_sections",
    "_clean_extraction_json",
    "_cleanup_all_llms",
    "_extract_edcr_freeform",
    "_fix_status_hallucination",
    "_merge_usage",
    "_parse_heading_level",
    "_parse_json",
    "_quality_check_facts",
    "_sigterm_handler",
    "_split_atomic",
]
