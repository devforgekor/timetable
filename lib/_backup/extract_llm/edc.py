# Status: production
# Path: imported by — lib/extract_llm (extraction subpackage)
"""EDC normalization pipeline: predicate grouping, entity resolution, quality checks.

Three-tier merge (SeqMatcher → Embed → LLM-as-judge), causal direction
fixing, numerical completeness, tautology detection, status hallucination
correction, and composite quality scoring (0-100).
"""

import json
import re
import urllib.request
from difflib import SequenceMatcher
from typing import Optional

_definition_cache_edc: dict[str, str] = {}
_DEFINITION_PROMPT_EDC = (
    "Define this predicate briefly — what relation does it express?\n\nPredicate: {}"
)

_llm_judge_stats_edc: dict[str, int] = {"calls": 0, "merged": 0, "split": 0, "uncertain": 0}
_embed_cache_edc: dict[str, list[float]] = {}


def _get_predicate_definition(raw_predicate: str) -> str:
    if not raw_predicate:
        return ""
    if raw_predicate in _definition_cache_edc:
        return _definition_cache_edc[raw_predicate]
    prompt = _DEFINITION_PROMPT_EDC.format(raw_predicate)
    try:
        body = json.dumps(
            {"model": "test", "messages": [{"role": "user", "content": prompt}]}
        ).encode()
        req = urllib.request.Request(
            "http://127.0.0.1:8082/v1/chat/completions",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
            definition = data["choices"][0]["message"]["content"].strip().strip("\"'")
            if definition and len(definition) > 5:
                _definition_cache_edc[raw_predicate] = definition
                return definition
    except Exception as e:
        print(f"    [def-gen] error '{raw_predicate[:40]}': {e}")
    return raw_predicate


def _snake_case(text: str) -> str:
    s = text.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "_", s)
    return s.strip("_")


def _raw_pred(fact: dict) -> str:
    return fact.get("predicate_raw", fact.get("predicate", ""))


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    return dot / (na * nb) if na * nb > 0 else 0.0


def _llm_judge(pred_a: str, pred_b: str) -> float:
    """LLM-as-judge via 8082."""
    global _llm_judge_stats_edc
    prompt = (
        f"Do these two predicates mean the same thing?\n\n"
        f"A: '{pred_a}'\nB: '{pred_b}'\n\n"
        f"Answer ONLY: equivalent | different | uncertain"
    )
    body = json.dumps({"model": "test", "messages": [{"role": "user", "content": prompt}]}).encode()
    try:
        req = urllib.request.Request(
            "http://127.0.0.1:8082/v1/chat/completions",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
            raw = data["choices"][0]["message"]["content"].strip().lower()
    except Exception:
        _llm_judge_stats_edc["uncertain"] += 1
        return 0.5
    _llm_judge_stats_edc["calls"] += 1
    if "equivalent" in raw:
        _llm_judge_stats_edc["merged"] += 1
        return 0.85
    if "different" in raw:
        _llm_judge_stats_edc["split"] += 1
        return 0.0
    _llm_judge_stats_edc["uncertain"] += 1
    return 0.5


def _embed_text_8081(text: str) -> Optional[list[float]]:
    """Embed text via 4B embed on :8081. Returns None on failure."""
    body = json.dumps({"input": text, "model": "default"}).encode()
    try:
        req = urllib.request.Request(
            "http://127.0.0.1:8081/v1/embeddings",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
            return data["data"][0]["embedding"]
    except Exception as e:
        print(f"    [embed] embedding failed: {e}", flush=True)
        return None


def _cached_embed_edc(text: str) -> Optional[list[float]]:
    if not text:
        return None
    if text in _embed_cache_edc:
        return _embed_cache_edc[text]
    vec = _embed_text_8081(text)
    if vec:
        _embed_cache_edc[text] = vec
    return vec


def _group_predicates(facts: list[dict]) -> list[dict]:
    """Group similar predicates via 3-tier: SeqMatcher → Embedding → LLM-as-judge.

    Tier 1: SequenceMatcher 0.85 blocking (non-transitive)
    Tier 2: Embedding definition cosine (0.85 merge / 0.65-0.85 LLM / <0.65 split)
    Tier 3: LLM-as-judge for all 0.65-0.85 pairs (no margin skip)

    Falls back gracefully when embed server (:8081) is unavailable.
    """
    if not facts:
        return facts

    global _llm_judge_stats_edc
    _llm_judge_stats_edc = {"calls": 0, "merged": 0, "split": 0, "uncertain": 0}

    # Stage 1: Non-transitive SequenceMatcher grouping
    groups = []
    for i, fa in enumerate(facts):
        pa = _snake_case(_raw_pred(fa))
        matched = False
        for g in groups:
            rep_i = min(g)
            pb = _snake_case(_raw_pred(facts[rep_i]))
            if SequenceMatcher(None, pa, pb).ratio() >= 0.85:
                g.add(i)
                matched = True
                break
        if not matched:
            groups.append({i})

    # Stage 2+3: Embedding 3-tier → LLM-as-judge for ambiguous
    rep_map: dict[int, tuple[int, set]] = {}
    for g in groups:
        if not g:
            continue
        members = [facts[i] for i in g]
        raw_counts: dict[str, int] = {}
        for m in members:
            raw = _raw_pred(m)
            raw_counts[raw] = raw_counts.get(raw, 0) + 1
        best = max(raw_counts, key=raw_counts.get)
        rep_idx = next(i for i in g if _raw_pred(facts[i]) == best)
        rep_map[id(g)] = (rep_idx, g)

    rep_ids = list(rep_map.keys())
    merged_group_ids: set[int] = set()
    embed_ok = False
    embed_count = 0
    if len(rep_ids) >= 2:
        # Try embed — if :8081 unavailable, skip to LLM-as-judge for all pairs
        test_vec = _embed_text_8081("test")
        if test_vec:
            embed_ok = True
            print(f"    [embed] checking {len(rep_ids)} group representatives...")

        for i in range(len(rep_ids)):
            if rep_ids[i] in merged_group_ids:
                continue
            ri, gi = rep_map[rep_ids[i]]
            fi = facts[ri]
            if embed_ok:
                ei = _cached_embed_edc(_get_predicate_definition(_raw_pred(fi)))
                if ei is None:
                    continue
                embed_count += 1
            for j in range(i + 1, len(rep_ids)):
                if rep_ids[j] in merged_group_ids:
                    continue
                rj, gj = rep_map[rep_ids[j]]
                fj = facts[rj]

                if embed_ok:
                    ej = _cached_embed_edc(_get_predicate_definition(_raw_pred(fj)))
                    if ej is None:
                        continue
                    embed_count += 1
                    sim = _cosine_similarity(ei, ej)
                else:
                    sim = 0.70  # force all pairs through LLM-as-judge

                if embed_ok and sim >= 0.85:
                    gi |= gj
                    merged_group_ids.add(rep_ids[j])
                    print(
                        f"    [def-embed] merged '{_raw_pred(fi)}' -> '{_raw_pred(fj)}' (cos={sim:.3f}, tier-1)"
                    )
                elif sim >= 0.65:
                    verdict = _llm_judge(_raw_pred(fi), _raw_pred(fj))
                    if verdict == 0.85:
                        gi |= gj
                        merged_group_ids.add(rep_ids[j])
                        print(
                            f"    [judge] merged '{_raw_pred(fi)}' -> '{_raw_pred(fj)}' (LLM: equivalent)"
                        )
                    elif verdict == 0.0:
                        print(
                            f"    [judge] split '{_raw_pred(fi)}' != '{_raw_pred(fj)}' (LLM: different)"
                        )
                    else:
                        print(
                            f"    [judge] uncertain '{_raw_pred(fi)}' vs '{_raw_pred(fj)}' (LLM uncertain, keeping separate)"
                        )

    final_groups = [g for g in groups if id(g) not in merged_group_ids]
    merged_count = sum(1 for g in final_groups if len(g) > 1)
    if embed_ok:
        tier = "3-tier" if embed_count >= 2 else "0-tier (all definitions failed)"
        print(f"    [embed] {tier}: {merged_count} groups formed (embed+judge)")
    elif merged_count:
        print(f"    [embed] {merged_count} groups formed (judge)")
    if _llm_judge_stats_edc["calls"] > 0:
        print(
            f"    [judge] calls={_llm_judge_stats_edc['calls']} "
            f"merged={_llm_judge_stats_edc['merged']} split={_llm_judge_stats_edc['split']} "
            f"uncertain={_llm_judge_stats_edc['uncertain']}"
        )

    # Assign canonical
    for group in final_groups:
        members = [facts[i] for i in group]
        rc = {}
        for m in members:
            r = _raw_pred(m)
            rc[r] = rc.get(r, 0) + 1
        canonical = max(rc, key=rc.get)
        canonical_snake = _snake_case(canonical)
        for idx in group:
            facts[idx]["predicate"] = canonical_snake
            facts[idx]["predicate_group"] = canonical
    return facts


def _group_entities(facts: list[dict], field: str = "subject") -> list[dict]:
    """Group equivalent entity surface forms via 3-tier: SeqMatcher → Embed → LLM.

    Normalizes subjects/objects so 'ETL pipeline processing time' and
    'etl_pipeline' resolve to the same canonical form.
    """
    if not facts:
        return facts

    entities = sorted({f.get(field, "") for f in facts if f.get(field, "")})
    if len(entities) <= 1:
        return facts

    global _llm_judge_stats_edc
    _llm_judge_stats_edc = {"calls": 0, "merged": 0, "split": 0, "uncertain": 0}

    # Stage 1: SequenceMatcher blocking (case-sensitive — embed stage handles case variants)
    groups = []
    for i, ea in enumerate(entities):
        matched = False
        for g in groups:
            rep = entities[min(g)]
            if SequenceMatcher(None, ea, rep).ratio() >= 0.85:
                g.add(i)
                matched = True
                break
        if not matched:
            groups.append({i})

    def _same_len_single_char_diff(a: str, b: str) -> bool:
        """True if two strings same length, differ by 1 character."""
        if len(a) != len(b):
            return False
        return sum(1 for ca, cb in zip(a, b) if ca != cb) == 1

    # Stage 2+3: Embed 3-tier for groups with multiple distinct forms
    embed_ok = False
    embed_count = 0
    merged_group_ids: set[int] = set()
    if len(groups) >= 2:
        test_vec = _embed_text_8081("test")
        if test_vec:
            embed_ok = True

        for i in range(len(groups)):
            if id(groups[i]) in merged_group_ids:
                continue
            ea = entities[min(groups[i])]
            if embed_ok:
                ei = _cached_embed_edc(ea)
                if ei is None:
                    continue
                embed_count += 1
            for j in range(i + 1, len(groups)):
                if id(groups[j]) in merged_group_ids:
                    continue
                eb = entities[min(groups[j])]
                if embed_ok:
                    ej = _cached_embed_edc(eb)
                    if ej is None:
                        continue
                    embed_count += 1
                    sim = _cosine_similarity(ei, ej)
                else:
                    sim = 0.70

                if embed_ok and sim >= 0.85 and not _same_len_single_char_diff(ea, eb):
                    groups[i] |= groups[j]
                    merged_group_ids.add(id(groups[j]))
                elif sim >= 0.65 and not _same_len_single_char_diff(ea, eb):
                    verdict = _llm_judge_entity(ea, eb)
                    if verdict == 0.85:
                        groups[i] |= groups[j]
                        merged_group_ids.add(id(groups[j]))

    # Build entity → canonical mapping (skip dead groups)
    entity_to_canonical = {}
    for g in groups:
        if id(g) in merged_group_ids:
            continue
        members = [entities[i] for i in g]
        # Pick shortest form as canonical (most concise)
        canonical = min(members, key=lambda x: (len(x), x))
        for m in members:
            entity_to_canonical[m] = canonical

    # Apply mapping
    for f in facts:
        original = f.get(field, "")
        canonical = entity_to_canonical.get(original, original)
        if canonical != original:
            f[field] = canonical
            f[f"{field}_original"] = original

    merged = sum(1 for g in groups if len(g) > 1)
    if merged:
        print(f"    [entity-{field}] {merged} groups canonicalized ({len(entities)}→{len(groups)})")
    return facts


def _llm_judge_entity(name_a: str, name_b: str) -> float:
    """LLM-as-judge for entity equivalence via 8082."""
    global _llm_judge_stats_edc
    prompt = (
        f"Do these two entity names refer to the same real-world entity?\n\n"
        f"A: '{name_a}'\nB: '{name_b}'\n\n"
        f"Answer ONLY: equivalent | different | uncertain"
    )
    body = json.dumps({"model": "test", "messages": [{"role": "user", "content": prompt}]}).encode()
    try:
        req = urllib.request.Request(
            "http://127.0.0.1:8082/v1/chat/completions",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
            raw = data["choices"][0]["message"]["content"].strip().lower()
    except Exception:
        _llm_judge_stats_edc["uncertain"] += 1
        return 0.5
    _llm_judge_stats_edc["calls"] += 1
    if "equivalent" in raw:
        _llm_judge_stats_edc["merged"] += 1
        return 0.85
    if "different" in raw:
        _llm_judge_stats_edc["split"] += 1
        return 0.0
    _llm_judge_stats_edc["uncertain"] += 1
    return 0.5


# ── Post-processing: Multi-value Expansion ──────────────────────

_FIXED_PHRASES = frozenset(
    {
        "research and development",
        "rock and roll",
        "back and forth",
        "up and down",
        "left and right",
        "black and white",
        "pros and cons",
        "dos and don ts",
        "by and large",
    }
)


def _is_splittable_conjunction(obj: str) -> list[str]:
    """Split object on ' and ' if right side starts with an action verb.

    Returns [original] (no split) or [part1, part2, ...].
    """
    low = obj.lower().strip()
    if low in _FIXED_PHRASES:
        return [obj]
    if " and " not in obj:
        return [obj]
    idx = obj.index(" and ")
    left, right = obj[:idx].strip(), obj[idx + 5 :].strip()
    if not left or not right:
        return [obj]
    right_words = right.split()
    _ACTION_STARTS = frozenset({"tuned", "added", "removed", "fixed", "set", "configured"})
    if right_words and right_words[0].lower() in _ACTION_STARTS:
        return [left, right]
    return [obj]


def _expand_multi_value(facts: list[dict]) -> list[dict]:
    """Expand facts whose object contains ' and ' + action into separate facts.

    No LLM calls. Expanded facts keep predicate_raw untouched; caller
    must re-run _normalize_predicate and _group_predicates.
    """
    expanded = []
    for f in facts:
        obj = f.get("object", "")
        parts = _is_splittable_conjunction(obj)
        if len(parts) <= 1:
            expanded.append(f)
            continue
        # First part keeps original fact
        first = dict(f)
        first["object"] = parts[0]
        expanded.append(first)
        # Subsequent parts become new facts
        for part in parts[1:]:
            new_f = dict(f)
            new_f["object"] = part
            expanded.append(new_f)
    return expanded


# ── Post-processing: Qualifier Splitting ────────────────────────

_QUALIFIER_PATTERNS: list[tuple[str, str]] = [
    (r",?\s*for\s+(\d+\s*%[^,]*)$", "percentage"),
    (r",?\s*during\s+(.+?)$", "context"),
    (r",?\s*of\s+(\w+\s*%)$", "percentage"),
    (r",?\s*with\s+(.+?)$", "condition"),
]


def _split_qualifiers(facts: list[dict]) -> list[dict]:
    """Extract trailing qualifier phrases from objects into qualifiers dict.

    Modifies facts in place. No LLM calls.
    """
    for f in facts:
        obj = f.get("object", "")
        if not obj:
            continue
        quals = f.get("qualifiers", {}) or {}
        for pattern, qual_key in _QUALIFIER_PATTERNS:
            m = re.search(pattern, obj, re.IGNORECASE)
            if m:
                quals[qual_key] = m.group(1).strip()
                obj = obj[: m.start()].strip().rstrip(",").strip()
        f["object"] = obj
        f["qualifiers"] = quals
    return facts


def _normalize_predicate(fact: dict) -> dict:
    """Normalize predicate: store raw, write snake_case canonical form."""
    raw = fact.get("predicate", "")
    fact["predicate_raw"] = raw
    fact["predicate"] = _snake_case(raw)
    return fact


def _fix_status_hallucination(facts: list[dict], source_text: str) -> list[dict]:
    """Fix status=active hallucination by cross-referencing source text.

    Qwen3-8B has a training bias where it extracts ALL services as
    status=active regardless of source text. This function reads the
    actual status from the source text's services table and corrects
    extracted facts.
    """
    status_pattern = re.compile(
        r"\|[^\S\n]*([\w][\w\s-]*[\w])[^\S\n]*\|[^\S\n]*[\w\s-]+[^\S\n]*\|[^\S\n]*(\w+)[^\S\n]*\|",
        re.MULTILINE,
    )
    source_statuses = {}
    for m in status_pattern.finditer(source_text):
        service = m.group(1).strip()
        if service.lower() in ("service", "항목", "조치", "시나리오", "세션"):
            continue
        status = m.group(2).strip().lower()
        if status in ("active", "inactive", "activating", "failed"):
            source_statuses[service] = status

    if not source_statuses:
        return facts

    fixed = 0
    for f in facts:
        subj = f.get("subject", "").strip()
        obj = f.get("object", "").strip()
        pred = f.get("predicate", "").strip().lower()

        # The source table stores bare entity names (e.g. "devforge-pod-a"),
        # but _expand_compounds prepends "Service " to table row conversions,
        # so the LLM may extract "Service devforge-pod-a" as the subject.
        subj_key = subj.removeprefix("Service ")
        if subj_key not in source_statuses:
            continue

        actual = source_statuses[subj_key]
        obj_lower = obj.lower()

        if "status=active" in obj_lower and actual != "active":
            f["object"] = re.sub(r"status=active", f"status={actual}", obj, flags=re.IGNORECASE)
            fixed += 1
        elif "status=inactive" in obj_lower and actual != "inactive":
            f["object"] = re.sub(r"status=inactive", f"status={actual}", obj, flags=re.IGNORECASE)
            fixed += 1
        elif obj_lower in ("active", "inactive") and obj_lower != actual:
            pred = f.get("predicate", "").lower()
            if "status" in pred:
                f["object"] = actual
                fixed += 1

    # Inject missing facts for services not extracted at all.
    # Qwen3-8B hallucinates ALL services as status=active, but sometimes
    # skips extracting certain services entirely (e.g. devforge-pod-a).
    # For any source service with non-active status that has no extracted
    # fact, inject a corrected fact.
    extracted_services = set()
    for f in facts:
        subj = f.get("subject", "").strip()
        extracted_services.add(subj.removeprefix("Service "))

    for service, actual_status in source_statuses.items():
        if actual_status == "active":
            continue
        if service not in extracted_services:
            facts.append(
                {
                    "subject": f"Service {service}",
                    "predicate": "status_is",
                    "object": actual_status,
                    "evidence": f"{service} status is {actual_status}.",
                    "category": "other",
                    "source_context": "services table",
                }
            )
            fixed += 1

    if fixed:
        print(
            f"    [status-fix] corrected {fixed} fact(s) via source text cross-reference",
            flush=True,
        )
    return facts


def _dedup_post_norm(facts: list[dict]) -> list[dict]:
    """Re-dedup after normalization: collapse (subject, normalized_pred, object)."""
    seen = {}
    for f in facts:
        key = (f.get("subject", ""), f.get("predicate", ""), f.get("object", ""))
        conf = f.get("qualifiers", {}).get("confidence", 1.0)
        if key not in seen or conf > seen[key].get("qualifiers", {}).get("confidence", 0):
            seen[key] = f
    result = list(seen.values())
    result.sort(key=lambda f: f.get("qualifiers", {}).get("confidence", 1.0), reverse=True)
    return result


# ── Quality Checks ──────────────────────────────────────────────


def _parse_causal_evidence(evidence: str) -> tuple[Optional[str], Optional[str]]:
    """Parse evidence for causal direction. Returns (cause, effect) or (None, None)."""
    if not evidence:
        return None, None

    # "Y (was) caused by X" — check BEFORE active "caused" to avoid "caused by" false match
    m = re.search(
        r"\b(\w[\w\s]*\w)\s+(?:was\s+)?caused\s+by\s+(\w[\w\s]*\w)\b", evidence, re.IGNORECASE
    )
    if m:
        return m.group(2).strip(), m.group(1).strip()  # cause=m2, effect=m1

    # "X caused Y" (active, but NOT "caused by")
    m = re.search(r"\b(\w[\w\s]+\w)\s+caused\s+(?!by\b)(\w[\w\s]*\w)\b", evidence, re.IGNORECASE)
    if m:
        return m.group(1).strip(), m.group(2).strip()

    # "X causing Y" (present participle)
    m = re.search(r"\b(\w[\w\s]+\w)\s+causing\s+(\w[\w\s]*\w)\b", evidence, re.IGNORECASE)
    if m:
        return m.group(1).strip(), m.group(2).strip()

    # "X resulted in Y"
    m = re.search(r"\b(\w[\w\s]+\w)\s+resulted\s+in\s+(\w[\w\s]*\w)\b", evidence, re.IGNORECASE)
    if m:
        return m.group(1).strip(), m.group(2).strip()

    # "X leading to Y"
    m = re.search(r"\b(\w[\w\s]+\w)\s+leading\s+to\s+(\w[\w\s]*\w)\b", evidence, re.IGNORECASE)
    if m:
        return m.group(1).strip(), m.group(2).strip()

    return None, None


def _fix_causal_direction(fact: dict) -> dict:
    """Fix predicate for caused/caused_by based on parsed evidence direction."""
    pred = fact.get("predicate", "").strip().lower()
    if pred not in ("caused", "caused_by"):
        fact.setdefault("_qc_checks", {})["causal_direction"] = "n/a"
        return fact
    evidence = fact.get("evidence", "")
    cause, effect = _parse_causal_evidence(evidence)
    if cause is None or effect is None:
        fact.setdefault("_qc_checks", {})["causal_direction"] = "unparseable"
        return fact
    subject = (fact.get("subject") or "").strip().lower()
    cause_lower = cause.lower()
    effect_lower = effect.lower()

    def _subj_in(a: str, b: str) -> bool:
        return a in b or b in a

    subj_is_cause = _subj_in(subject, cause_lower)
    subj_is_effect = _subj_in(subject, effect_lower)

    if subj_is_cause and not subj_is_effect:
        correct_pred = "caused"
    elif subj_is_effect and not subj_is_cause:
        correct_pred = "caused_by"
    else:
        fact.setdefault("_qc_checks", {})["causal_direction"] = "ambiguous"
        return fact

    if pred != correct_pred:
        print(
            f"    [qc-causal] '{pred}' -> '{correct_pred}' (subj={'cause' if subj_is_cause else 'effect'})"
        )
        fact["predicate"] = correct_pred
        fact.setdefault("_qc_checks", {})["causal_direction"] = "fixed"
    else:
        fact.setdefault("_qc_checks", {})["causal_direction"] = "passed"

    return fact


_NUM_RE = re.compile(
    r"\b(\d+(?:\.\d+)?\s*(?:%|hours?|minutes?|seconds?|GiB?|MiB?|KiB?|GB|MB|KB|G|M|K)?)",
    re.IGNORECASE,
)

_NUM_UNIT_RE = re.compile(
    r"^(.*?)(%|hours?|minutes?|seconds?|GiB?|MiB?|KiB?|GB|MB|KB|G|M|K)$", re.IGNORECASE
)


def _has_numerical_unit(val: str) -> bool:
    """Check if a numerical value has a meaningful unit suffix."""
    return bool(_NUM_UNIT_RE.match(val.strip()))


def _fix_numerical_completeness(fact: dict) -> dict:
    """Append numerical values from evidence to object if missing.

    Only appends values with meaningful units (%, hours, Gi, etc.)
    to avoid polluting objects with bare error codes or IDs.
    """
    evidence = fact.get("evidence", "")
    obj = fact.get("object", "")
    if not evidence or not obj:
        fact.setdefault("_qc_checks", {})["numerical"] = "n/a"
        return fact
    ev_nums = _NUM_RE.findall(evidence)
    if not ev_nums:
        fact.setdefault("_qc_checks", {})["numerical"] = "passed"
        return fact
    obj_lower = obj.lower()
    missing = []
    for n in ev_nums:
        n_stripped = n.strip()
        if not n_stripped:
            continue
        if n_stripped.lower() in obj_lower:
            continue
        # Only add numbers with clear units
        if _has_numerical_unit(n_stripped):
            missing.append(n_stripped)
    if missing:
        old_obj = obj
        new_obj = obj.rstrip(".,")
        for m in missing:
            if m not in new_obj:
                new_obj += f" {m}"
        new_obj = new_obj.strip()
        if new_obj != old_obj:
            fact["object"] = new_obj
            fact.setdefault("_qc_checks", {})["numerical"] = "fixed"
            print(f"    [qc-num] +{missing}")
        else:
            fact.setdefault("_qc_checks", {})["numerical"] = "passed"
    else:
        fact.setdefault("_qc_checks", {})["numerical"] = "passed"
    return fact


def _fix_subject_object_tautology(fact: dict) -> dict:
    """Fix subject==object tautology for resolved_via-type predicates."""
    pred = fact.get("predicate", "").strip().lower()
    if pred not in ("resolved_via", "resolved_by", "fixed_by"):
        fact.setdefault("_qc_checks", {})["tautology"] = "n/a"
        return fact
    subject = (fact.get("subject") or "").strip()
    obj = (fact.get("object") or "").strip()
    if not subject or not obj or subject.lower() != obj.lower():
        fact.setdefault("_qc_checks", {})["tautology"] = "passed"
        return fact
    evidence = fact.get("evidence", "")
    if not evidence:
        fact["_qc_remove"] = True
        fact.setdefault("_qc_checks", {})["tautology"] = "removed"
        print(f"    [qc-tauto] removed tautology: '{subject[:40]}'")
        return fact

    m = re.search(r"\b(\w[\w\s]+)\s+resolved\s+(\w[\w\s]*)\b", evidence, re.IGNORECASE)
    if m:
        solution = m.group(1).strip()
        issue_word = m.group(2).strip()
        if issue_word.lower() in ("it", "this", "the issue", "the problem"):
            issue = subject
        else:
            issue = issue_word
        fact["subject"] = issue
        fact["object"] = solution
        fact.setdefault("_qc_checks", {})["tautology"] = "fixed"
        print(f"    [qc-tauto] fixed: subj='{issue[:30]}' obj='{solution[:30]}'")
    else:
        fact["_qc_remove"] = True
        fact.setdefault("_qc_checks", {})["tautology"] = "removed_unresolvable"
        print(f"    [qc-tauto] removed unresolvable: '{subject[:40]}'")
    return fact


_ENTITY_PREFIXES = ("Service ", "Pod ", "Container ")


def _strip_known_prefix(name: str) -> str:
    """Strip entity type prefixes that _expand_compounds prepends."""
    for prefix in _ENTITY_PREFIXES:
        if name.startswith(prefix):
            return name[len(prefix) :]
    return name


def _fix_subject_grounding(fact: dict, source_text: str) -> dict:
    """Flag facts whose subject doesn't appear in source text."""
    subject = (fact.get("subject") or "").strip()
    source_lower = source_text.lower()
    subj_lower = _strip_known_prefix(subject).lower()
    if not subject or not source_text or subj_lower in source_lower:
        fact.setdefault("_qc_checks", {})["grounding"] = "passed"
        return fact
    words = [w for w in subject.replace("_", " ").split() if len(w) > 1]
    if words:
        word_matches = sum(1 for w in words if w.lower() in source_lower)
        if word_matches / len(words) >= 0.6:
            fact.setdefault("_qc_checks", {})["grounding"] = "passed"
            return fact
    fact["_qc_low_confidence"] = True
    fact.setdefault("_qc_checks", {})["grounding"] = "low_confidence"
    print(f"    [qc-ground] low conf: '{subject[:40]}' not in source")
    return fact


def _fix_direction_swaps(facts: list[dict]) -> list[dict]:
    """Cross-fact scan: detect reversed (subject, predicate, object) pairs.

    For each pair of facts sharing the same predicate where subject/object
    are swapped (Fact A: X-P-Y, Fact B: Y-P-X), flags the one whose
    subject doesn't appear in the other's evidence as a suspected reversal.
    """
    if len(facts) < 2:
        return facts
    idx_by_key: dict[tuple[str, str, str], int] = {}
    swap_pairs: list[tuple[int, int, str]] = []
    for i, f in enumerate(facts):
        subj = (f.get("subject") or "").strip().lower()
        pred = (f.get("predicate") or "").strip().lower()
        obj = (f.get("object") or "").strip().lower()
        if not subj or not pred or not obj:
            continue
        if subj == obj:
            continue
        key = (subj, pred, obj)
        idx_by_key[key] = i
        rev_key = (obj, pred, subj)
        j = idx_by_key.get(rev_key)
        if j is not None:
            swap_pairs.append((j, i, pred))
    for j, i, pred in swap_pairs:
        fj = facts[j]
        fi = facts[i]
        ev_j = (fj.get("evidence") or "").lower()
        ev_i = (fi.get("evidence") or "").lower()
        subj_j = (fj.get("subject") or "").strip().lower()
        subj_i = (fi.get("subject") or "").strip().lower()
        j_subj_in_ev_i = subj_j in ev_i
        i_subj_in_ev_j = subj_i in ev_j
        if i_subj_in_ev_j and not j_subj_in_ev_i:
            facts[i].setdefault("_qc_checks", {})["direction_swap"] = "suspected_reversal"
            facts[j].setdefault("_qc_checks", {})["direction_swap"] = "passed"
            print(f"    [qc-dirswap] fact[{i}] suspected reversal vs fact[{j}] (pred='{pred}')")
        elif j_subj_in_ev_i and not i_subj_in_ev_j:
            facts[j].setdefault("_qc_checks", {})["direction_swap"] = "suspected_reversal"
            facts[i].setdefault("_qc_checks", {})["direction_swap"] = "passed"
            print(f"    [qc-dirswap] fact[{j}] suspected reversal vs fact[{i}] (pred='{pred}')")
        else:
            facts[j].setdefault("_qc_checks", {})["direction_swap"] = "ambiguous"
            facts[i].setdefault("_qc_checks", {})["direction_swap"] = "ambiguous"
            print(f"    [qc-dirswap] ambiguous pair fact[{j}] <-> fact[{i}] (pred='{pred}')")
    return facts


_QC_WEIGHTS = {
    "causal_direction": 0.15,
    "tautology": 0.10,
    "numerical": 0.10,
    "grounding": 0.25,
    "direction_swap": 0.15,
    "faithful": 0.25,
}


def _compute_quality_score(fact: dict) -> float:
    """Compute 0-100 composite quality score from QC checks + faithful_score."""
    checks = fact.get("_qc_checks", {}) or {}

    def _axis_score(
        key: str, pass_vals: tuple[str, ...], partial_vals: tuple[str, ...] = ()
    ) -> float:
        val = checks.get(key, "n/a")
        if val in pass_vals or val == "n/a":
            return 1.0
        if val in partial_vals:
            return 0.7
        return 0.0

    cd = _axis_score("causal_direction", ("passed", "fixed"), ("ambiguous",))
    tt = _axis_score("tautology", ("passed", "n/a"), ("fixed",))
    nu = _axis_score("numerical", ("passed", "n/a"), ("fixed",))
    gr = _axis_score("grounding", ("passed",))
    ds = _axis_score("direction_swap", ("passed", "n/a"), ("ambiguous",))

    faithful = fact.get("faithful_score")
    faithful = float(faithful) if faithful is not None else 1.0
    faithful = max(0.0, min(1.0, faithful))

    scores = [cd, tt, nu, gr, ds, faithful]
    total = sum(w * s for w, s in zip(_QC_WEIGHTS.values(), scores))
    return round(total * 100, 1)


_QC_FILTER_THRESHOLD = 20.0


def _quality_check_facts(facts: list[dict], source_text: str = "") -> list[dict]:
    """Run all post-extraction quality checks. Annotates each fact with _qc_checks dict."""
    if not facts:
        return facts
    checked = []
    for f in facts:
        f["_qc_checks"] = {}
        subj = (f.get("subject") or "").strip()
        pred = (f.get("predicate") or "").strip()
        obj = (f.get("object") or "").strip()
        if not subj and not pred and not obj:
            f["_qc_remove"] = True
            f["_qc_checks"]["empty_triple"] = "removed"
        else:
            f = _fix_causal_direction(f)
            f = _fix_subject_object_tautology(
                f
            )  # run BEFORE numerical to preserve subject/obj equality
            f = _fix_numerical_completeness(f)
            f = _fix_subject_grounding(f, source_text)
        checked.append(f)
    checked = _fix_direction_swaps(checked)
    for f in checked:
        score = _compute_quality_score(f)
        f["_qc_checks"]["quality_score"] = score
        if score < _QC_FILTER_THRESHOLD:
            f["_qc_remove"] = True
            f["_qc_checks"]["auto_filtered"] = f"score={score} < {_QC_FILTER_THRESHOLD}"
    result = [f for f in checked if not f.get("_qc_remove")]
    low_conf = sum(1 for f in checked if f.get("_qc_low_confidence"))
    if low_conf:
        print(f"    [qc] {low_conf} low-confidence fact(s)")
    changed = len(facts) - len(result)
    if changed:
        print(f"    [qc] removed {changed} fact(s)")
    # strip meta keys from removed facts too (they won't be stored)
    for f in checked:
        f.pop("_qc_remove", None)
        f.pop("_qc_low_confidence", None)
    # Merge verifier #2 info into _qc_checks (don't pop — QC2 runs later)
    for f in checked:
        v2 = f.get("_verifier2")
        if v2:
            f["_qc_checks"]["verifier2"] = v2
    return result


def _normalize_freeform_pipeline(facts: list[dict], source_text: str = "") -> list[dict]:
    """Full EDC normalization pipeline: subject → predicate → post-process."""
    if not facts:
        return facts
    before = len(facts)

    if source_text:
        facts = _fix_status_hallucination(facts, source_text)

    facts = _quality_check_facts(facts, source_text)

    facts = _group_entities(facts, field="subject")
    facts = _group_entities(facts, field="object")
    for f in facts:
        _normalize_predicate(f)
    raw_preds = sorted({_raw_pred(f) for f in facts})
    print(f"    Unique raw predicates ({len(raw_preds)}): {raw_preds}")
    facts = _group_predicates(facts)
    norm_preds = sorted({f.get("predicate", "") for f in facts})
    print(f"    After grouping: {len(norm_preds)} unique predicates")
    facts = _dedup_post_norm(facts)

    # ── Post-processing: multi-value expansion + qualifier split ──
    pp_before = len(facts)
    facts = _expand_multi_value(facts)
    facts = _split_qualifiers(facts)
    # Re-normalize predicates for expanded facts (no LLM — just snake_case)
    for f in facts:
        _normalize_predicate(f)
    facts = _group_predicates(facts)
    facts = _dedup_post_norm(facts)
    pp_added = len(facts) - pp_before
    if pp_added:
        print(f"    Post-process: +{pp_added} facts (multi-value + qualifier split)")
    # ───────────────────────────────────────────────────────────────

    print(f"    Dedup: {before} -> {len(facts)} (ent+pred+dedup removed {before - len(facts)})")
    return facts
