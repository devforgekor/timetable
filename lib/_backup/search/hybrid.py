#!/usr/bin/env python3
# Status: experimental
# Path: imported by CLI search, MCP search tools
"""Hybrid search — BM25 + Dense RRF fusion, optional cross-encoder reranker.

Pipeline:
  Stage 1 (parallel): BM25 (FTS5) + Dense (pgvector ANN) → RRF fusion → top N
  Stage 2 (optional):  Cross-encoder reranker (Qwen3-Reranker-4B) → re-score top K
  Stage 3:             Token-budgeted output for LLM

RRF formula: score(d) = Σ 1 / (k + rank_i(d))  where k = 60 (default)

Requires:
  - lib/search/local_index.FTS5Index (rebuild first)
  - turns.embedding populated via embeddings table with embed model vectors
  - pgvector HNSW index on embedding
  - (optional) inference reranker on :8080 for Stage 2

Usage:
  python3 -c "from lib.search.hybrid import hybrid_search; print(hybrid_search('질문'))"
"""

from __future__ import annotations

import json
import re
import time
import urllib.request
from typing import Dict, List, Optional, Tuple

from lib.db import psql, psql_json, esc_sql
from lib.search.local_index import FTS5Index

# Embed API (same endpoint as embed_batch.py)
from lib.llm_client import MODEL_REGISTRY
EMBED_URL = f"http://127.0.0.1:{MODEL_REGISTRY['embeder']['port']}/v1/embeddings"
EMBED_TIMEOUT = 30
EMBED_DIMS = 2048

# RRF constant (k=60: industry default, Cormack et al. 2009)
RRF_K = 60

# Dense ANN search limit per call
DENSE_SEARCH_LIMIT = 100

# Reranker (inference, same network namespace as MCP container)
RERANKER_URL = "http://127.0.0.1:8080/v1/rerank"
RERANKER_TIMEOUT = 120

# LLM listwise reranker (RankGPT-style via Gemini 2.5 Flash, Stage 3)
LLM_RERANK_URL = "http://127.0.0.1:4431/v1/chat/completions"
LLM_RERANK_TIMEOUT = 120
LLM_RERANK_WINDOW = 10
LLM_RERANK_MAX_CANDIDATES = 30


def _get_query_vector(query: str) -> Optional[List[float]]:
    """Get embedding vector for a query string via embed API."""
    body = json.dumps({"input": query, "model": "default"}).encode()
    try:
        req = urllib.request.Request(
            EMBED_URL, data=body,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=EMBED_TIMEOUT) as resp:
            data = json.loads(resp.read())
        vec = data["data"][0]["embedding"]
        truncated = vec[:EMBED_DIMS]
        norm = sum(x * x for x in truncated) ** 0.5
        return [x / norm for x in truncated] if norm > 0 else truncated
    except Exception:
        return None


def _bm25_rank(query: str, limit: int = 50) -> Dict[str, int]:
    """BM25 search via FTS5. Returns {turn_id: rank} (0-based, lower=better)."""
    idx = FTS5Index()
    results = idx.bm25_search(query, limit=limit)
    return {r["turn_id"]: i for i, r in enumerate(results)}


def _dense_rank(query: str, limit: int = DENSE_SEARCH_LIMIT) -> Tuple[Dict[str, int], Optional[str]]:
    """Dense ANN search via pgvector cosine distance. Returns {turn_id: rank}, error_msg.

    Handles multi-chunk turns: fetches 2x headroom for dedup, keeps best (first) chunk per turn.
    """
    vec = _get_query_vector(query)
    if vec is None:
        return {}, "embed API unavailable"

    vec_str = "[" + ",".join(f"{v:.8f}" for v in vec) + "]"

    sql = (
        f"SELECT t.id, (e.embedding <=> '{esc_sql(vec_str)}'::vector) as dist "
        f"FROM turns t "
        f"JOIN embeddings e ON e.source_type = 'turn' AND e.source_id = t.id "
        f"  AND e.model_name = 'qwen3-embedding-8b-v1' "
        f"ORDER BY e.embedding <=> '{esc_sql(vec_str)}'::vector "
        f"LIMIT {limit * 2}"
    )
    rows = psql_json(sql) or []

    seen: set = set()
    deduped: list = []
    for row in rows:
        if row["id"] not in seen:
            seen.add(row["id"])
            deduped.append(row)
            if len(deduped) >= limit:
                break

    return {row["id"]: i for i, row in enumerate(deduped)}, None


def _rrf_score(rank: int) -> float:
    """Compute RRF score from rank (0-based)."""
    return 1.0 / (RRF_K + rank + 1)


def _rerank_results(query: str, candidates: List[Tuple], top_k: int) -> Tuple[List[Tuple], Optional[str]]:
    """Cross-encoder reranking via inference reranker on :8080.

    Args:
        query: Original search query.
        candidates: List of (rrf_score, turn_id, bm25_rank, dense_rank, text).
        top_k: How many candidates to rerank.

    Returns:
        (reordered_candidates, error_msg)
        Each candidate: (rerank_score, rrf_score, turn_id, bm25_rank, dense_rank)
    """
    pool = candidates[:top_k]
    if not pool:
        return [], "no candidates"

    docs = [c[4][:2000] for c in pool]
    body = json.dumps({
        "model": "reranker",
        "query": query[:2000],
        "documents": docs,
        "top_n": len(docs),
    }).encode()
    req = urllib.request.Request(
        RERANKER_URL, data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=RERANKER_TIMEOUT) as resp:
            data = json.loads(resp.read().decode())
    except Exception as e:
        return [(0.0, c[0], c[1], c[2], c[3]) for c in candidates[:top_k]], f"reranker call failed: {e}"

    scores = [0.0] * len(pool)
    for r in data.get("results", []):
        idx = r.get("index")
        if idx is not None and 0 <= idx < len(pool):
            scores[idx] = float(r.get("relevance_score", 0.0))

    ordered = sorted(
        [(scores[i], pool[i][0], pool[i][1], pool[i][2], pool[i][3]) for i in range(len(pool))],
        key=lambda x: (-x[0], -x[1]),
    )
    return ordered, None


def _llm_listwise_rerank(
    query: str,
    candidates: List[Tuple],
    text_map: Dict[str, str],
    window_size: int = 10,
    top_k: int = 30,
) -> Tuple[List[Tuple], Optional[str]]:
    pool = candidates[:top_k]
    if not pool:
        return [], "no candidates for LLM rerank"

    n = len(pool)
    step = window_size // 2 or 1
    windows = []
    end = n
    while end > 0:
        start = max(0, end - window_size)
        windows.append((start, end))
        end -= step
    windows.reverse()

    result = list(pool)

    for start, end in windows:
        subset = result[start:end]
        if len(subset) <= 1:
            continue

        texts = []
        for item in subset:
            tid = item[2]
            raw = text_map.get(tid, "")
            texts.append(" ".join(raw.split())[:800])

        lines = [f"[{i+1}] {t}" for i, t in enumerate(texts)]
        prompt = (
            f"Query: {query[:300]}\n\n"
            f"Rank passages by relevance. Output only a JSON array of indices "
            f"from most to least relevant.\n\n"
            + "\n".join(lines)
        )

        body = json.dumps({
            "model": "gemini-2.5-flash",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1,
            "max_tokens": 256,
        }).encode()
        req = urllib.request.Request(
            LLM_RERANK_URL, data=body,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=LLM_RERANK_TIMEOUT) as resp:
                data = json.loads(resp.read().decode())
            content = data["choices"][0]["message"]["content"]
        except Exception as e:
            return pool[:top_k], f"LLM rerank call failed: {e}"

        match = re.search(r'\[[\d,\s]+\]', content)
        if not match:
            continue

        try:
            ranked = json.loads(match.group())
        except json.JSONDecodeError:
            continue

        ordered = []
        seen = set()
        for idx in ranked:
            if isinstance(idx, int) and 1 <= idx <= len(subset) and idx - 1 not in seen:
                ordered.append(subset[idx - 1])
                seen.add(idx - 1)
        for i in range(len(subset)):
            if i not in seen:
                ordered.append(subset[i])

        result[start:end] = ordered

    output = []
    for i, item in enumerate(result):
        new_score = 1.0 / (1 + i)
        output.append((new_score, item[0], item[1], item[2], item[3], item[4]))

    return output[:top_k], None


def _fetch_turn_metadata(tid_list: List[str]) -> Dict[str, dict]:
    """Fetch enriched metadata for a list of turn IDs.

    Returns dict keyed by turn_id with:
      conversation_id, created_at, agent, seq,
      user_turn, text, text_clean,
      conversation title, source, model
    """
    if not tid_list:
        return {}
    tid_quoted = ",".join(f"'{esc_sql(t)}'::uuid" for t in tid_list)
    rows = psql_json(
        f"SELECT t.id, t.conversation_id, t.created_at::text, t.agent, t.seq, "
        f"  t.user_turn, t.text, t.text_clean, "
        f"  c.title AS conv_title, c.source AS conv_source, c.model AS conv_model "
        f"FROM turns t "
        f"JOIN conversations c ON c.id = t.conversation_id "
        f"WHERE t.id IN ({tid_quoted})"
    ) or []
    return {r["id"]: r for r in rows}


def hybrid_search(query: str, limit: int = 20, *,
                  rerank: bool = True,
                  rerank_candidates: int = 50,
                  llm_rerank: bool = False,
                  llm_rerank_candidates: int = 30) -> Dict:
    """Hybrid BM25 + Dense search via RRF fusion, optional cross-encoder + LLM listwise rerank.

    Args:
        query: Natural language query.
        limit: Max results to return.
        rerank: Whether to apply cross-encoder reranker on RRF results.
        rerank_candidates: How many RRF top candidates to cross-encode (default 50).
        llm_rerank: Whether to apply LLM listwise reranker (RankGPT-style).
                    Requires Gemini OpenAI proxy on :4431. Incurs API cost.
        llm_rerank_candidates: How many cross-encoder top candidates to LLM-rerank (default 30).

    Returns:
        {
            results: [{turn_id, conversation_id, created_at, agent, seq,
                       user_turn, text, text_clean,
                       conv_title, conv_source, conv_model,
                       bm25_rank, dense_rank, rrf_score, rerank_score, llm_rerank_score}],
            meta: {bm25_count, dense_count, bm25_time, dense_time,
                   rerank_time, rerank_error, llm_rerank_time, llm_rerank_error, embed_error}
        }
    """
    meta: Dict = {"bm25_count": 0, "dense_count": 0,
                  "bm25_time": 0.0, "dense_time": 0.0}

    # --- Stage 1: BM25 ---
    t0 = time.monotonic()
    bm25_ranks = _bm25_rank(query, limit=50)
    meta["bm25_time"] = round(time.monotonic() - t0, 3)
    meta["bm25_count"] = len(bm25_ranks)

    # --- Stage 1: Dense ---
    t0 = time.monotonic()
    dense_ranks, embed_error = _dense_rank(query)
    meta["dense_time"] = round(time.monotonic() - t0, 3)
    meta["dense_count"] = len(dense_ranks)
    if embed_error:
        meta["embed_error"] = embed_error

    # --- RRF Fusion ---
    all_turn_ids = set(bm25_ranks.keys()) | set(dense_ranks.keys())
    scored: List[tuple] = []
    for tid in all_turn_ids:
        score = 0.0
        b_rank = bm25_ranks.get(tid)
        d_rank = dense_ranks.get(tid)
        if b_rank is not None:
            score += _rrf_score(b_rank)
        if d_rank is not None:
            score += _rrf_score(d_rank)
        scored.append((score, tid, b_rank, d_rank))

    # Sort by RRF score descending
    scored.sort(key=lambda x: (-x[0], x[1]))

    if not scored:
        return {"results": [], "meta": meta}

    # --- Fetch text for reranker candidates ---
    rr_candidates = min(rerank_candidates, len(scored))
    cand_tids = [r[1] for r in scored[:rr_candidates]]
    meta_map = _fetch_turn_metadata(cand_tids)

    # Build candidate list with text for reranker
    rerank_pool = []
    for score, tid, b_rank, d_rank in scored[:rr_candidates]:
        info = meta_map.get(tid, {})
        text = info.get("text_clean") or info.get("text") or ""
        rerank_pool.append((score, tid, b_rank, d_rank, text))

    # --- Stage 2: Cross-encoder reranker (optional) ---
    rerank_error = None
    reranked = None
    if rerank and len(rerank_pool) >= 1:
        t0 = time.monotonic()
        reranked, rerank_error = _rerank_results(query, rerank_pool, rr_candidates)
        meta["rerank_time"] = round(time.monotonic() - t0, 3)
        if rerank_error:
            meta["rerank_error"] = rerank_error
        # Merge reranked results with remaining unreranked tail
        reranked_set = {c[2] for c in reranked}
        tail = [r for r in scored[rr_candidates:] if r[1] not in reranked_set]
        # Add tail with rerank_score=None
        ordered = list(reranked) + [(None, r[0], r[1], r[2], r[3]) for r in tail]
    else:
        meta["rerank_time"] = 0.0
        ordered = [(None, r[0], r[1], r[2], r[3]) for r in scored]

    # --- Stage 3: LLM listwise reranker (optional, RankGPT-style) ---
    llm_rerank_error = None
    if llm_rerank and reranked:
        t0 = time.monotonic()
        text_map = {c[1]: c[4] for c in rerank_pool}
        llm_in = reranked[:llm_rerank_candidates]
        llm_out, llm_rerank_error = _llm_listwise_rerank(
            query, llm_in, text_map,
            top_k=llm_rerank_candidates,
        )
        meta["llm_rerank_time"] = round(time.monotonic() - t0, 3)
        if llm_rerank_error:
            meta["llm_rerank_error"] = llm_rerank_error
        llm_set = {c[3] for c in llm_out}
        tail = [r for r in ordered[len(llm_out):] if (r[3] if len(r) == 6 else r[2]) not in llm_set]
        ordered = list(llm_out) + tail

    # --- Build results ---
    top = ordered[:limit]
    # 6-element LLM tuples have tid at pos 3, 5-element at pos 2
    result_tids = [r[3] if len(r) == 6 else r[2] for r in top]
    # Fetch full metadata for all top results (some may not be in meta_map yet)
    missing = [tid for tid in result_tids if tid not in meta_map]
    if missing:
        meta_map.update(_fetch_turn_metadata(missing))

    results = []
    for entry in top:
        if len(entry) == 6:
            llm_score_t = entry[0]
            rerank_score_t = entry[1]
            rrf_score_t = entry[2]
            tid = entry[3]
            b_rank = entry[4]
            d_rank = entry[5]
        else:
            llm_score_t = None
            rerank_score_t = entry[0]
            rrf_score_t = entry[1]
            tid = entry[2]
            b_rank = entry[3]
            d_rank = entry[4]
        info = meta_map.get(tid, {})

        text_raw = info.get("text") or ""
        text_clean = info.get("text_clean") or ""

        results.append({
            "turn_id": tid,
            "conversation_id": info.get("conversation_id", ""),
            "created_at": info.get("created_at", ""),
            "agent": info.get("agent", ""),
            "seq": info.get("seq"),
            "conv_title": info.get("conv_title") or "",
            "conv_source": info.get("conv_source") or "",
            "conv_model": info.get("conv_model") or "",
            "user_turn": (info.get("user_turn") or "")[:500],
            "text": text_raw[:1000],
            "text_clean": text_clean[:1000],
            "bm25_rank": b_rank,
            "dense_rank": d_rank,
            "rrf_score": round(rrf_score_t, 4),
            "rerank_score": round(rerank_score_t, 4) if rerank_score_t is not None else None,
            "llm_rerank_score": round(llm_score_t, 4) if llm_score_t is not None else None,
        })

    return {"results": results, "meta": meta}


def bm25_only(query: str, limit: int = 20) -> Dict:
    """BM25-only search (no dense component)."""
    t0 = time.monotonic()
    idx = FTS5Index()
    results = idx.bm25_search(query, limit=limit)
    elapsed = round(time.monotonic() - t0, 3)
    return {
        "results": [{
            "turn_id": r["turn_id"],
            "conversation_id": r["conversation_id"],
            "created_at": r["created_at"],
            "agent": r["agent"],
            "seq": r["seq"],
            "text_clean": (r.get("text_clean") or "")[:200],
            "rank": round(r["rank"], 4),
        } for r in results],
        "meta": {"count": len(results), "elapsed_s": elapsed},
    }
