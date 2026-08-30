#!/usr/bin/env python3
# Status: production
# Path: imported by — pipelines/exp_runner.py, day_runner.py, night_runner.py
"""Metrics extraction from pipeline stdout and Slack reporting."""

import json, os, re, urllib.request
from datetime import datetime, timezone
from pathlib import Path

SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SLACK_TOKEN = ""
SLACK_CHANNEL = "U0APJGD8CBW"
_sf = Path.home() / ".config/devforge/secrets.env"
if _sf.exists():
    for _line in _sf.read_text().split("\n"):
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            if _k.strip() == "SLACK_BOT_TOKEN":
                SLACK_TOKEN = _v.strip().strip('"').strip("'")
            elif _k.strip() == "SLACK_CHANNEL":
                SLACK_CHANNEL = _v.strip().strip('"').strip("'")

EXPER_DIR = os.path.join(SCRIPTS_DIR, "..", "data", "experiment")


def log(msg):
    t = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{t}] {msg}", flush=True)


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def slack_send(text):
    if not SLACK_TOKEN:
        return
    payload = json.dumps({"channel": SLACK_CHANNEL, "text": text, "mrkdwn": True}).encode()
    req = urllib.request.Request(
        "https://slack.com/api/chat.postMessage", data=payload,
        headers={"Authorization": f"Bearer {SLACK_TOKEN}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            r = json.loads(resp.read())
            if not r.get("ok"):
                log(f"Slack API error: {r.get('error','?')}")
    except Exception as e:
        log(f"Slack send failed: {e}")


def extract_metrics(stdout, phase, elapsed):
    metrics = {
        "phase": phase, "timestamp": utc_timestamp(),
        "elapsed_seconds": round(elapsed, 1),
    }

    for line in stdout.split("\n"):
        if "Python verify:" in line and "issues" in line:
            m = re.search(r'(\d+)\s+issues.*?(\d+)\s+findings', line)
            if m:
                metrics["python_issues"] = int(m.group(1))
                metrics["python_findings"] = int(m.group(2))

        if "day_verify verdict=" in line:
            m = re.search(r'verdict=(\S+)\s+confidence=(\S+)', line)
            if m:
                metrics["day_verify_verdict"] = m.group(1)
                metrics["day_verify_confidence"] = m.group(2)

        if "avg weighted_score=" in line:
            m = re.search(r'avg weighted_score=([\d.]+)', line)
            if m:
                metrics["rubric_avg_score"] = float(m.group(1))
        if "Evaluated" in line and "findings" in line:
            m = re.search(r'Evaluated (\d+) findings', line)
            if m:
                metrics["rubric_evaluated_count"] = int(m.group(1))

        if "P_score=" in line and "R_score=" in line and "decision=" in line and "J_classify" not in line:
            m = re.search(r'P_score=(\S+)\s+R_score=(\S+).*?decision=(\S+)', line)
            if m:
                metrics["P_score"] = m.group(1)
                metrics["R_score"] = m.group(2)
                metrics["decision"] = m.group(3)
            m2 = re.search(r'consensus=(\S+)?', line)
            if m2 and m2.group(1):
                metrics["consensus"] = m2.group(1)

        if "night_verify verdict=" in line:
            m = re.search(r'verdict=(\S+)\s+confidence=(\S+)', line)
            if m:
                metrics["night_verify_verdict"] = m.group(1)
                metrics["night_verify_confidence"] = m.group(2)
        if "night_verify (feedback)" in line:
            m = re.search(r'verdict=(\S+)\s+confidence=(\S+)', line)
            if m:
                metrics["feedback_nv_verdict"] = m.group(1)
                metrics["feedback_nv_confidence"] = m.group(2)

        if "feedback loop executed" in line.lower():
            metrics["feedback_executed"] = True

        if "handoff preference:" in line:
            m = re.search(r'handoff preference:\s+(\S+)', line)
            if m:
                metrics["handoff_preference"] = m.group(1)

        if "processed," in line and "failed" in line:
            m = re.search(r'(\d+)\s+processed,\s+(\d+)\s+failed', line)
            if m:
                metrics["processed"] = int(m.group(1))
                metrics["failed"] = int(m.group(2))

    return metrics


def send_phase_report(phase, metrics_path):
    if not os.path.exists(metrics_path):
        slack_send(f":warning: *Phase {phase}* — metrics file not found")
        return
    with open(metrics_path) as f:
        m = json.load(f)
    elapsed = m.get("elapsed_seconds", 0)
    elapsed_min = round(elapsed / 60, 1)

    lines = [f"*Phase {phase}* ({elapsed_min}분)"]
    if "python_issues" in m:
        lines.append(f"▸ Python verify: {m['python_issues']} issues / {m['python_findings']} findings")
    if "day_verify_verdict" in m:
        lines.append(f"▸ day_verify: *{m['day_verify_verdict']}* (conf={m['day_verify_confidence']})")
    if "rubric_evaluated_count" in m:
        lines.append(f"▸ Rubric: {m['rubric_evaluated_count']} findings evaluated, avg={m.get('rubric_avg_score','?')}")
    if "P_score" in m:
        lines.append(f"▸ P-R-J: P={m['P_score']} R={m['R_score']} → *{m['decision']}*")
    if "night_verify_verdict" in m:
        lines.append(f"▸ night_verify: *{m['night_verify_verdict']}* (conf={m['night_verify_confidence']})")
    if m.get("feedback_executed"):
        fb_v = m.get("feedback_nv_verdict", "?")
        fb_c = m.get("feedback_nv_confidence", "?")
        lines.append(f"▸ Feedback loop: night_verify re-verify *{fb_v}* (conf={fb_c})")
    if m.get("success"):
        lines.append(f":white_check_mark: Phase {phase} 성공")
    else:
        lines.append(f":x: Phase {phase} 실패 (exit={m.get('returncode','?')})")
    slack_send("\n".join(lines))


def generate_comparison_report():
    """Build and send 2x2 factorial comparison across 5 phases."""
    report = {"timestamp": utc_timestamp(), "phases": {}}
    for phase in range(5):
        mp = os.path.join(EXPER_DIR, f"phase{phase}_metrics.json")
        if os.path.exists(mp):
            with open(mp) as f:
                report["phases"][f"phase{phase}"] = json.load(f)

    cp = os.path.join(EXPER_DIR, "experiment_comparison.json")
    with open(cp, "w") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    labels_display = ["기준선", "구조개선", "+루브릭", "+피드백", "풀스택"]
    header = "메트릭 | " + " | ".join(labels_display)
    separator = "---|" + "|".join("---" for _ in range(5))

    lines = [":chart_with_upwards_trend: *2x2 실험 비교*"]
    lines.append(header)
    lines.append(separator)

    for label, key in [
        ("소요시간(min)", None), ("P score", "P_score"), ("R score", "R_score"),
        ("결정", "decision"), ("day_verify conf", "day_verify_confidence"),
        ("night_verify verdict", "night_verify_verdict"), ("night_verify conf", "night_verify_confidence"),
        ("루브릭 평가", "rubric_evaluated_count"), ("피드백 실행", "feedback_executed"),
    ]:
        vals = []
        for p in range(5):
            pd = report["phases"].get(f"phase{p}", {})
            if key is None:
                v = f"{pd.get('elapsed_seconds',0)/60:.0f}m" if pd.get('elapsed_seconds') else "-"
            else:
                v = pd.get(key, "-")
                if isinstance(v, float):
                    v = f"{v:.1f}"
            vals.append(str(v))
        lines.append(f"{label} | {' | '.join(vals)}")

    lines.append("")
    lines.append("*2x2 효과 분석 (소요시간 기준)*")
    elapsed = {}
    for p in range(5):
        pd = report["phases"].get(f"phase{p}", {})
        elapsed[p] = pd.get("elapsed_seconds", 0)

    p0, p1 = elapsed.get(0, 0), elapsed.get(1, 0)
    p2, p3, p4 = elapsed.get(2, 0), elapsed.get(3, 0), elapsed.get(4, 0)

    e_structural = (p1 - p0) / 60 if p0 else 0
    e_rubric_off = (p2 - p1) / 60 if p1 else 0
    e_feedback_off = (p3 - p1) / 60 if p1 else 0
    e_feedback_on = (p4 - p2) / 60 if p2 else 0
    e_rubric_on = (p4 - p3) / 60 if p3 else 0

    effects = [
        ("구조개선 (P1-P0)", f"{e_structural:+.1f}분"),
        ("Rubric (P2-P1, F=OFF)", f"{e_rubric_off:+.1f}분"),
        ("Feedback (P3-P1, R=OFF)", f"{e_feedback_off:+.1f}분"),
        ("Feedback (P4-P2, R=ON)", f"{e_feedback_on:+.1f}분"),
        ("Rubric (P4-P3, F=ON)", f"{e_rubric_on:+.1f}분"),
    ]
    for label, val in effects:
        lines.append(f"▸ {label}: {val}")

    slack_send("\n".join(lines))
    log(f"Comparison: {cp}")
    return cp
