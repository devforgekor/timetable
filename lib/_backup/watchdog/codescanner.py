# Status: experimental
# Path: called by — watchdog.py main loop (periodic code quality scan)
"""Code scanner — DB-backed auto-fix feedback loop for silent exception patterns.

Automatically detects and fixes silent error swallowing in pipeline code:

  except Exception:       ->  except Exception as e:
      pass                     print(f"  [{component}] ... {e}")

Designed to prevent P1/P6-class bugs from recurring: any new pipeline code
that introduces silent `except Exception: pass` will be detected within ~10min
and automatically patched with proper logging.

Persistence:
  - watchdog_code_fixes table (PostgreSQL) tracks fixed/failed fingerprints
  - 3 consecutive failures -> 24h cooldown before retry
  - Previously fixed patterns are detected on regression: if file mtime changed
    since fix was applied, status transitions from 'fixed' -> 'regressed' for
    re-attempt
  - In-memory mtime cache prevents re-reading unchanged files per session

Scope: pipelines/ directory only (where silent LLM failures cause data loss).
Proxies, infra, tests, and lib are excluded — their broad catches are
intentional for resilience and not safe to auto-fix.

Detection patterns:
  - except Exception: \\n<whitespace>pass         (silent swallow)
  - except Exception: \\n<whitespace>return None   (silent degradation)
  - except Exception: \\n<whitespace>return$       (silent abort)
"""

import os
import re
import time
from datetime import datetime, timezone
from typing import Optional

# Patterns to detect — ordered by severity
SILENT_PATTERNS: list[dict] = [
    {
        "id": "except-pass",
        "regex": re.compile(r"except Exception:\s*\n\s+pass", re.MULTILINE),
        "label": "except Exception: pass -> silent swallow",
        "severity": "high",
    },
    {
        "id": "except-return-none",
        "regex": re.compile(r"except Exception:\s*\n\s+return None", re.MULTILINE),
        "label": "except Exception: return None -> silent degradation",
        "severity": "high",
    },
    {
        "id": "except-return-bare",
        "regex": re.compile(r"except Exception:\s*\n\s+return\s*$", re.MULTILINE),
        "label": "except Exception: return -> silent abort",
        "severity": "high",
    },
]

# Directories to exclude from scan
EXCLUDE_DIRS = {"__pycache__", "_archive", "__old", ".git", ".venv", "node_modules"}

# Only scan pipelines/ — proxies, infra, tests use broad catches intentionally
SCAN_PREFIXES = ("pipelines/",)

MAX_FINDINGS_PER_CYCLE = 3
MAX_RETRIES = 3
COOLDOWN_HOURS = 24

# Per-session file mtime cache: {rel_path: mtime} -> skip if unchanged
_last_scan_mtimes: dict[str, float] = {}

# DB fingerprint cache: {fp: record} — loaded once per scan cycle
_known_fingerprints: dict[str, dict] = {}


def log(msg: str):
    print(f"[{time.strftime('%H:%M:%S')}] [codescanner] {msg}", flush=True)


def _fingerprint(finding: dict) -> str:
    """Deterministic fingerprint for dedup across cycles + DB."""
    return f"{finding['file']}:{finding['line']}:{finding['pattern_id']}"


# ── DB helpers ─────────────────────────────────────────────────────


def _load_known():
    """Load all fix records from DB into _known_fingerprints cache."""
    global _known_fingerprints
    try:
        from lib.db import psql_json

        rows = psql_json(
            "SELECT fingerprint, status, attempt_count, last_attempt_at, "
            "fixed_at, file_path FROM watchdog_code_fixes"
        )
        _known_fingerprints = {r["fingerprint"]: r for r in rows} if rows else {}
    except Exception as e:
        log(f"DB load failed: {e}")
        _known_fingerprints = {}


def _db_upsert(
    fp: str,
    file_path: str,
    line: int,
    pattern_id: str,
    status: str,
    attempt_count: int,
    detail: str = "",
):
    """Upsert a fix record into watchdog_code_fixes."""
    try:
        from lib.db import esc_sql, psql_ok

        detail_esc = esc_sql(detail[:200])
        sql = f"""
        INSERT INTO watchdog_code_fixes (fingerprint, file_path, line, pattern_id,
                                          status, attempt_count, last_attempt_at,
                                          fixed_at, detail)
        VALUES ('{esc_sql(fp)}', '{esc_sql(file_path)}', {line},
                '{esc_sql(pattern_id)}', '{status}', {attempt_count}, NOW(),
                CASE WHEN '{status}' = 'fixed' THEN NOW() ELSE NULL END,
                '{detail_esc}')
        ON CONFLICT (fingerprint) DO UPDATE SET
            status = EXCLUDED.status,
            attempt_count = EXCLUDED.attempt_count,
            last_attempt_at = NOW(),
            fixed_at = CASE WHEN EXCLUDED.status = 'fixed'
                           THEN NOW() ELSE watchdog_code_fixes.fixed_at END,
            detail = CASE WHEN EXCLUDED.detail != ''
                          THEN EXCLUDED.detail ELSE watchdog_code_fixes.detail END
        """
        psql_ok(sql)
    except Exception as e:
        log(f"DB upsert failed ({fp}): {e}")


def _ensure_mtime_cache(fpath: str) -> Optional[float]:
    """Get current mtime, updating cache."""
    try:
        return os.path.getmtime(fpath)
    except OSError:
        return None


def _check_regression(fp: str, rec: dict) -> Optional[str]:
    """If 'fixed' record's file mtime > fixed_at, return 'regressed'.

    Relifix (ICSE): Auto-repair systems must detect when a previously
    applied fix was invalidated by subsequent code changes.
    Detected by comparing file modification time against fix timestamp.
    """
    if rec.get("status") != "fixed":
        return None
    fpath_str = rec.get("file_path", "")
    if not fpath_str:
        return None
    abs_path = fpath_str
    if not abs_path.startswith("/"):
        abs_path = os.path.join("/opt/projects/server/scripts", fpath_str)
    mtime = _ensure_mtime_cache(abs_path)
    if mtime is None:
        return None

    fixed_raw = rec.get("fixed_at")
    if not fixed_raw:
        return None
    if isinstance(fixed_raw, str):
        fixed_dt = datetime.fromisoformat(fixed_raw.replace("Z", "+00:00"))
    else:
        fixed_dt = fixed_raw
    fixed_ts = fixed_dt.timestamp()

    if mtime > fixed_ts + 1:  # 1s tolerance for filesystem precision
        return "regressed"
    return None


def _can_retry(fp: str) -> bool:
    """Check if a finding should be included for fix attempt.

    Returns True if:
      - Never seen before
      - Status is 'regressed' (was fixed, file changed since)
      - Status is 'failed' and under retry limit or past cooldown

    Returns False if:
      - Status is 'fixed' and file unchanged (no regression)
      - Status is 'failed', at retry cap, and still in cooldown
    """
    rec = _known_fingerprints.get(fp)
    if not rec:
        return True  # never seen before

    # Regression check: fixed file that changed since fix
    regression = _check_regression(fp, rec)
    if regression == "regressed":
        # Auto-update cache status for this cycle
        rec["status"] = "regressed"
        _known_fingerprints[fp] = rec
        log(f"  regression detected: {fp} (mtime > fixed_at)")
        return True

    if rec["status"] in ("fixed",):
        return False  # still fixed, skip

    if rec.get("status") in ("failed", "regressed"):
        attempts = rec.get("attempt_count") or 0
        if attempts >= MAX_RETRIES:
            # Check cooldown
            last_raw = rec.get("last_attempt_at")
            if last_raw:
                if isinstance(last_raw, str):
                    last_dt = datetime.fromisoformat(last_raw.replace("Z", "+00:00"))
                else:
                    last_dt = last_raw
                elapsed = (datetime.now(timezone.utc) - last_dt).total_seconds()
                if elapsed < COOLDOWN_HOURS * 3600:
                    return False  # still in cooldown
        return True  # under limit or past cooldown

    return True


# ── Scan logic ──────────────────────────────────────────────────────


def scan_file(fpath: str, root_dir: str) -> list[dict]:
    """Scan a single .py file for silent catch patterns. Returns findings."""
    rel = os.path.relpath(fpath, root_dir)
    if not rel.startswith(SCAN_PREFIXES):
        return []
    if rel in _last_scan_mtimes:
        mtime = os.path.getmtime(fpath)
        if mtime <= _last_scan_mtimes[rel]:
            return []

    try:
        with open(fpath) as f:
            content = f.read()
    except (OSError, UnicodeDecodeError):
        return []

    findings = []
    for pat in SILENT_PATTERNS:
        for m in pat["regex"].finditer(content):
            line_num = content[: m.start()].count("\n") + 1
            lines = content.split("\n")
            ctx_start = max(0, line_num - 3)
            ctx_end = min(len(lines), line_num + 1)
            context = "\n".join(lines[ctx_start:ctx_end])

            finding = {
                "file": rel,
                "line": line_num,
                "pattern_id": pat["id"],
                "label": pat["label"],
                "severity": pat["severity"],
                "matched": m.group(),
                "context": context,
            }
            fp = _fingerprint(finding)
            if _can_retry(fp):
                findings.append(finding)

    _last_scan_mtimes[rel] = os.path.getmtime(fpath)
    return findings


def scan_all(root_dir: str) -> list[dict]:
    """Scan all .py files under root_dir for silent catch patterns."""
    all_findings = []
    for dirpath, dirnames, filenames in os.walk(root_dir):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            fpath = os.path.join(dirpath, fn)
            findings = scan_file(fpath, root_dir)
            all_findings.extend(findings)
            if len(all_findings) >= MAX_FINDINGS_PER_CYCLE:
                return all_findings[:MAX_FINDINGS_PER_CYCLE]
    return all_findings


def mark_fixed(finding: dict, detail: str = ""):
    """Record a finding as fixed — persists to DB + updates local cache."""
    fp = _fingerprint(finding)
    _db_upsert(fp, finding["file"], finding["line"], finding["pattern_id"], "fixed", 0, detail)
    _known_fingerprints[fp] = {"fingerprint": fp, "status": "fixed", "attempt_count": 0}


def mark_failed(finding: dict, detail: str = ""):
    """Record a finding as failed — increments attempt_count in DB."""
    fp = _fingerprint(finding)
    rec = _known_fingerprints.get(fp, {})
    attempts = (rec.get("attempt_count") or 0) + 1
    _db_upsert(
        fp, finding["file"], finding["line"], finding["pattern_id"], "failed", attempts, detail
    )
    _known_fingerprints[fp] = {
        "fingerprint": fp,
        "status": "failed",
        "attempt_count": attempts,
        "last_attempt_at": datetime.now(timezone.utc),
    }


def run_scan(scripts_dir: str = "/opt/projects/server/scripts") -> list[dict]:
    """Run full scan and log results. Returns new (unfixed, non-cooldown) findings."""
    _load_known()
    findings = scan_all(scripts_dir)

    if not findings:
        return []

    log(f"Found {len(findings)} new silent catch pattern(s) in pipelines/:")
    for f in findings:
        log(f"  {f['file']}:{f['line']} -- {f['label']}")
    return findings
