# Status: production
import glob
import re
import os
import subprocess as sp
import sys
from typing import Any, Dict, List, Optional

# Constants from original mcp_enrich.py
TIMEOUT_ENRICH = 900
MAX_TOKENS_ENRICH = 512
TEMP_ENRICH = 0.1
BATCH_LIMIT = 20

# Embedding/NLI thresholds
COSINE_ENTITY_RELEVANCE = 0.25
TLDR_COSINE_MIN = 0.30
MINICHECK_SUPPORT = 0.3

_VALID_INTENTS = {"question", "request", "report", "clarification",
                  "code_change", "debug", "design", "other"}
_VALID_CATEGORIES = {"requirement", "decision", "explanation",
                     "code", "reasoning", "other"}

_ENTITY_REJECT_PATTERNS = [
    re.compile(r'https?://\S+'),
    re.compile(r'ftp://\S+'),
    re.compile(r'ftp\b'),
    re.compile(r'[\[\](){}]'),
    re.compile(r'^[\d\s]+$'),
]
_MAX_ENTITY_WORDS = 8
_MIN_ENTITY_LEN = 2
_ENTITY_SPECIAL_CHARS = re.compile(r'[@#$%^&*+=<>|\\~`;]')

# Tag-intent consistency
_INTENT_TAG_BLOCKED = {
    "question": {"code_change", "implementation", "refactor", "deploy"},
    "request": {"debug", "bug"},
    "clarification": {"implementation", "code_change", "deploy", "bug"},
    "code_change": {"question", "help", "howto", "debug"},
    "debug": {"feature", "design", "proposal"},
    "design": {"bug", "debug", "hotfix"},
    "report": {"question", "howto"},
    "other": set(),
}

def clean_markdown(text: str) -> str:
    """Strip all markdown formatting from text."""
    if not text:
        return text
    text = re.sub(r'```[\s\S]*?```', '', text)
    text = re.sub(r'``.*?``', '', text)
    text = re.sub(r'`([^`]+)`', r'\1', text)
    text = re.sub(r'\*{2,}([^*]+)\*{2,}', r'\1', text)
    text = re.sub(r'_{2,}([^_]+)_{2,}', r'\1', text)
    text = re.sub(r'~{2,}([^~]+)~{2,}', r'\1', text)
    text = text.replace('`', '')
    text = re.sub(r'\s+', ' ', text).strip()
    return text

def find_symbol(symbol: str, project_root: str = "/opt/projects/server") -> bool:
    """Search for a Python function/class definition using grep."""
    import subprocess as sp
    try:
        r = sp.run(
            ["grep", "-Erq", f"^(def |class |async def ){re.escape(symbol)}[( ]",
             "--include=*.py", project_root],
            capture_output=True, timeout=15,
        )
        return r.returncode == 0
    except Exception:
        return False

def verify_entities(enrich_data: Optional[Dict],
                     project_root: str = "/opt/projects/server") -> Dict:
    """Verify entities.files exist and entities.functions can be found."""
    entities = enrich_data.get("entities", {}) if enrich_data else {}
    if not isinstance(entities, dict):
        entities = {}
    verified: Dict[str, list] = {"files": [], "symbols": []}

    for filepath in entities.get("files", []):
        full = os.path.join(project_root, filepath)
        exists = os.path.exists(full)
        if not exists:
            alt = os.path.join(project_root, "scripts", filepath.lstrip("./"))
            if os.path.exists(alt):
                exists = True
        if not exists:
            basename = os.path.basename(filepath)
            if glob.glob(f"{project_root}/**/{basename}", recursive=True):
                exists = True
        verified["files"].append({"path": filepath, "exists": exists})

    for sym in entities.get("functions", []):
        found = find_symbol(sym, project_root)
        verified["symbols"].append({"name": sym, "found": found})

    return verified

def post_process_enrich(enrich_data: Optional[Dict[str, Any]],
                      user_turn: str = "", text: str = ""
                      ) -> Optional[Dict[str, Any]]:
    """Post-processing for enrichment fields: validate, clean, trim, structural filter."""
    if not enrich_data:
        return enrich_data

    # tldr
    tldr = enrich_data.get("tldr", "")
    if tldr:
        tldr = clean_markdown(tldr)
        words = tldr.split()
        if len(words) > 20:
            tldr = " ".join(words[:20]) + "..."
    enrich_data["tldr"] = tldr[:200] if tldr else ""

    # intent
    intent = enrich_data.get("intent", "").lower()
    if intent not in _VALID_INTENTS:
        enrich_data["intent"] = "other"

    # category
    category = enrich_data.get("category", "").lower()
    if category not in _VALID_CATEGORIES:
        enrich_data["category"] = "other"

    # entities
    entities = enrich_data.get("entities", {})
    if not isinstance(entities, dict):
        entities = {}
    for key in ("files", "technologies", "functions", "mentioned_users"):
        items = entities.get(key, [])
        if not isinstance(items, list):
            items = []
        seen: set = set()
        clean_items = []
        for item in items:
            s = str(item).strip()
            if not s or s in seen:
                continue
            if len(s) < _MIN_ENTITY_LEN:
                continue
            if key != "files" and _ENTITY_SPECIAL_CHARS.search(s):
                continue
            if any(p.search(s) for p in _ENTITY_REJECT_PATTERNS):
                continue
            if len(s.split()) > _MAX_ENTITY_WORDS:
                continue
            seen.add(s)
            if key == "files":
                s = s.lstrip("./")
            clean_items.append(s)
        entities[key] = clean_items[:10]
    enrich_data["entities"] = entities

    # tags
    tags = enrich_data.get("tags", [])
    if not isinstance(tags, list):
        tags = []
    seen_tags: set = set()
    clean_tags = []
    for tag in tags:
        t = str(tag).strip().lower()
        if t and t not in seen_tags:
            seen_tags.add(t)
            clean_tags.append(t)
    enrich_data["tags"] = clean_tags[:5]

    # Tag-intent consistency
    intent = enrich_data.get("intent", "other")
    blocked = _INTENT_TAG_BLOCKED.get(intent, set())
    if blocked:
        enrich_data["tags"] = [t for t in enrich_data.get("tags", []) if t not in blocked]

    return enrich_data
