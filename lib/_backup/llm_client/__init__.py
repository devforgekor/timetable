#!/usr/bin/env python3
# Status: production
"""Unified LLM client — single entry point for all pipeline scripts."""

import json
import socket
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

from lib.llm_client.feedback import _inject_feedback
from lib.llm_client.recovery import _model_key_for_8082, is_8082_connection_error, recover_8082

MODEL_REGISTRY: Dict[str, Dict[str, Any]] = {
    "extractor": {"port": 8082, "temp": 0.12, "max_tokens": 2048, "timeout": 300},
    "extractor-b": {"port": 8083, "temp": 0.0, "max_tokens": 1024, "timeout": 300},
    "cleaner": {"port": 8080, "temp": 0.0, "max_tokens": 512, "timeout": 600},
    "proposer": {"port": 8081, "temp": 0.22, "max_tokens": 2048, "timeout": 600},
    "reviewer": {"port": 8083, "temp": 0.10, "max_tokens": 400, "timeout": 480},
    "day-verify": {"port": 8082, "temp": 0.0, "max_tokens": 512, "timeout": 120},
    "day-verify-b": {"port": 8083, "temp": 0.0, "max_tokens": 512, "timeout": 120},
    "day-verify-q8": {"port": 8082, "temp": 0.0, "max_tokens": 512, "timeout": 120},
    "day-verify-q8-b": {"port": 8083, "temp": 0.0, "max_tokens": 512, "timeout": 120},
    "day-verify-q4": {"port": 8082, "temp": 0.0, "max_tokens": 512, "timeout": 120},
    "day-verify-q4-b": {"port": 8083, "temp": 0.0, "max_tokens": 512, "timeout": 120},
    "day-enricher": {"port": 8082, "temp": 0.1, "max_tokens": 512, "timeout": 900},
    "day-enricher-b": {"port": 8083, "temp": 0.1, "max_tokens": 512, "timeout": 900},
    "reflector": {"port": 8082, "temp": 0.10, "max_tokens": 2048, "timeout": 600},
    "verifier": {"port": 8084, "temp": 0.10, "max_tokens": 4096, "timeout": 1200},
    "judge": {"port": 8083, "temp": 0.10, "max_tokens": 4096, "timeout": 7200},
    "reranker": {"port": 8080},
    "tiny": {"port": 8080},
    "embeder": {"port": 8081},
    "day_extract": {"_model": "extractor"},
    "day_extract_b": {"_model": "extractor-b"},
    "day_enrich": {"_model": "day-enricher"},
    "day_enrich_b": {"_model": "day-enricher-b"},
    "day_verify": {"_model": "day-verify"},
    "day_verify_b": {"_model": "day-verify-b"},
    "day_verify_q8": {"_model": "day-verify-q8"},
    "day_verify_q8_b": {"_model": "day-verify-q8-b"},
    "day_verify_q4": {"_model": "day-verify-q4"},
    "day_verify_q4_b": {"_model": "day-verify-q4-b"},
    "day_proposer": {"_model": "reviewer"},
    "day_reviewer": {"_model": "reviewer"},
    "day_judge": {"_model": "reviewer"},
    "night_proposer": {"_model": "proposer"},
    "night_reflector": {"_model": "reflector"},
    "night_judge": {"_model": "judge"},
    "night_verify": {"_model": "verifier"},
}


def resolve_model(name: str) -> str:
    cfg = MODEL_REGISTRY.get(name)
    return cfg["_model"] if cfg and "_model" in cfg else name


def call_llm(
    messages: List[Dict[str, str]],
    model: str = "reviewer",
    *,
    max_tokens: Optional[int] = None,
    temperature: Optional[float] = None,
    top_p: Optional[float] = None,
    top_k: Optional[int] = None,
    repeat_penalty: Optional[float] = None,
    presence_penalty: Optional[float] = None,
    frequency_penalty: Optional[float] = None,
    timeout: Optional[int] = None,
    json_mode: bool = False,
    chat_template_kwargs: Optional[Dict[str, Any]] = None,
    return_meta: bool = False,
    cache_prompt: Optional[bool] = None,
) -> Any:
    cfg = MODEL_REGISTRY.get(model)
    if not cfg:
        raise ValueError(f"Unknown model: {model}. Known: {list(MODEL_REGISTRY)}")
    if "_model" in cfg:
        model = cfg["_model"]
        cfg = MODEL_REGISTRY[model]

    messages = _inject_feedback(messages, model)

    port = cfg["port"]
    body: Dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens if max_tokens is not None else cfg["max_tokens"],
        "temperature": temperature if temperature is not None else cfg["temp"],
        "stream": False,
    }
    if top_p is not None:
        body["top_p"] = top_p
    if top_k is not None:
        body["top_k"] = top_k
    if repeat_penalty is not None:
        body["repeat_penalty"] = repeat_penalty
    if presence_penalty is not None:
        body["presence_penalty"] = presence_penalty
    if frequency_penalty is not None:
        body["frequency_penalty"] = frequency_penalty
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    if chat_template_kwargs is not None:
        body["chat_template_kwargs"] = chat_template_kwargs
    if cache_prompt is not None:
        body["cache_prompt"] = cache_prompt

    t_start = time.monotonic()
    url = f"http://127.0.0.1:{port}/v1/chat/completions"
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    _http_timeout = min(timeout or cfg["timeout"], 1800)
    print(
        f"  [call_llm] {model}:{port} timeout={_http_timeout}s max_tokens={body['max_tokens']}",
        flush=True,
    )
    try:
        with urllib.request.urlopen(req, timeout=_http_timeout) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as e:
        raise RuntimeError(f"LLM call to :{port} ({model}) failed: {e}")
    except socket.timeout:
        raise RuntimeError(f"LLM call to :{port} ({model}) timed out after {_http_timeout}s")
    elapsed_ms = (time.monotonic() - t_start) * 1000

    choices = result.get("choices", [])
    if not choices:
        raise RuntimeError(f"LLM ({model}) returned no choices: {result}")
    content = (choices[0]["message"].get("content") or "").strip()
    usage = result.get("usage", {})
    timings = result.get("timings", {})

    try:
        from lib.watchdog.messenger import heartbeat

        heartbeat(f"llm_{model}", detail=f"ok:{elapsed_ms:.0f}ms")
    except Exception:
        pass

    if return_meta:
        return {
            "content": content,
            "usage": usage,
            "timings": timings,
            "model": model,
            "elapsed_ms": elapsed_ms,
            "port": port,
        }
    return content


def reranker_score(query: str, document: str) -> float:
    query = query[:1500] if query else ""
    document = document[:1500] if document else ""
    reranker_port = MODEL_REGISTRY["reranker"]["port"]
    body = json.dumps(
        {
            "model": "reranker",
            "query": query,
            "documents": [document],
            "top_n": 1,
        }
    ).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{reranker_port}/v1/rerank",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode())
        return float(data["results"][0]["relevance_score"])
    except Exception as e:
        print(f"  [reranker] score call failed: {e}", flush=True)
        return -1.0


def reranker_nli_verdict(score: float) -> str:
    if score < 0:
        return "RERANKER_ERROR"
    if score >= 0.75:
        return "GROUNDED"
    elif score >= 0.40:
        return "AMBIGUOUS"
    return "UNGROUNDED"


def _call_nli_server(
    source: str, evidence: str, strict: bool = False, nli_port: int = 8085, timeout: int = 30
) -> str:
    body = json.dumps(
        {
            "source": source[:4000],
            "evidence": evidence[:1000],
            "strict": strict,
        }
    ).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{nli_port}/nli",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
        return data.get("label_3class", "NEUTRAL")
    except Exception as e:
        print(f"  [nli] call failed: {e}", flush=True)
        return "NEUTRAL"


def call_llm_json(
    messages: List[Dict[str, str]],
    model: str = "reviewer",
    **kwargs: Any,
) -> str:
    return call_llm(messages, model, json_mode=True, **kwargs)


def recall_tiny() -> None:
    try:
        messages = [{"role": "user", "content": "ping"}]
        call_llm(messages, model="tiny", max_tokens=2, temperature=0, timeout=15)
    except Exception:
        pass


def call_llm_with_retry(*args, **kwargs):
    try:
        return call_llm(*args, **kwargs)
    except Exception as e:
        if is_8082_connection_error(e):
            model = kwargs.get("model", "day-extractor")
            model_key = _model_key_for_8082(model)
            print(f"  [recovery] 8082 error ({model}): {type(e).__name__}", flush=True)
            recover_8082(model_key)
            return call_llm(*args, **kwargs)
        raise
