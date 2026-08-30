# Status: production
import re
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))
BATCH_SIZE = 5
MAX_BATCHES = 2
MAX_RETRIES = 1

_KOREAN_SUMMARY_RE = re.compile(
    r"^(적용|분석|구현|수정|확인|테스트|배포|설정|제거|추가|변경|최적화|리팩토링|디버깅|문서화|통합|마이그레이션|롤백)"
    r"\s*(완료|종료|끝|마침|됨|했음|하였음|했습니다|하였습니다)",
)

WORKLOG_SYSTEM = """You are a worklog generator. From AI coding session turns, extract completed work items.
CRITICAL — Evidence rules:
1. Evidence = copy-paste a sentence verbatim from the turn text. Do NOT rewrite. Do NOT summarize.
2. Open the turn text, find the exact line, copy it with your cursor, paste it into the evidence field.
3. Evidence MUST be short: a command line, error message, log line, or decision sentence. Max 150 characters.
... (truncated for brevity)
"""

REVIEW_SYSTEM = """You are a worklog reviewer. A Python script flagged entries because the evidence string was not found as an exact substring in the turn text.
... (truncated for brevity)
"""

def today_kst() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d")

def verify_evidence(evidence: str, turn_text: str) -> tuple:
    if not evidence or not turn_text:
        return False, "empty evidence or turn text"
    if len(evidence) > 200:
        return False, f"evidence too long ({len(evidence)} chars)"
    if _KOREAN_SUMMARY_RE.match(evidence):
        return False, "evidence looks like Korean summary"
    if "\n" in evidence and len(evidence) > 120:
        return False, "multi-line evidence"
    if evidence in turn_text:
        return True, "evidence verified (substring match)"
    return False, "evidence NOT found in turn text"
