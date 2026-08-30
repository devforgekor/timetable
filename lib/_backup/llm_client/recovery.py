#!/usr/bin/env python3
# Status: production
"""8082 auto-recovery — reload model on connection error."""

import threading

_8082_RECOVERY_LOCK = threading.Lock()

_CONNECTION_ERROR_SUBSTRINGS = (
    "Remote end closed", "Connection reset", "Connection refused",
    "Broken pipe", "RemoteDisconnected",
)


def is_8082_connection_error(e: Exception) -> bool:
    err = str(e)
    if "8082" not in err and "extractor" not in err:
        return False
    return any(s in err for s in _CONNECTION_ERROR_SUBSTRINGS)


def recover_8082(model_key: str = "day-extractor") -> None:
    if not _8082_RECOVERY_LOCK.acquire(blocking=False):
        print("  [recovery] Another recovery in progress, waiting...", flush=True)
        _8082_RECOVERY_LOCK.acquire(blocking=True)
        print("  [recovery] Recovery finished by other thread", flush=True)
        _8082_RECOVERY_LOCK.release()
        return
    try:
        print(f"  [recovery] Reloading 8082 → {model_key}...", flush=True)
        from lib.pod_manager import ensure_model
        ensure_model(model_key, skip_if_healthy=False)
        print("  [recovery] 8082 ready", flush=True)
    except Exception as recover_err:
        print(f"  [recovery] 8082 reload failed: {recover_err}", flush=True)
    finally:
        _8082_RECOVERY_LOCK.release()


def _model_key_for_8082(model: str) -> str:
    mapping = {
        "day-verify": "day-verifier",
        "day_extract": "day-extractor",
        "extractor": "day-extractor",
        "day-enricher": "day-enricher",
        "day_enrich": "day-enricher",
    }
    return mapping.get(model, "day-extractor")
