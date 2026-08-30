# Status: production
import glob
import json
import os
from datetime import datetime, timezone
from typing import Dict

from lib.llm_client import call_llm
from lib.rubric import utils


def call_llm_json(
    messages, model, max_tokens=1024, timeout=utils.TIMEOUT_LLM, label="", dry_run=False
):
    """Call LLM, return parsed JSON with metadata."""
    if dry_run:
        utils.log(f"  [DRY] Mocking LLM call for {model} ({label})")
        return {
            "result": {
                "findings": [],
                "verdicts": [],
                "P_score": 25,
                "R_score": 25,
                "consensus_score": 90,
                "decision": "APPROVED",
            },
            "usage": {"total_tokens": 0},
            "timings": {},
            "elapsed_ms": 100,
            "model": model,
        }
    try:
        result = call_llm(
            messages,
            model=model,
            max_tokens=max_tokens,
            timeout=timeout,
            json_mode=True,
            return_meta=True,
        )
        content = result["content"]
        parsed = json.loads(content) if isinstance(content, str) else content
        return {
            "result": parsed,
            "usage": result.get("usage", {}),
            "timings": result.get("timings", {}),
            "elapsed_ms": result.get("elapsed_ms", 0),
            "model": result.get("model", model),
        }
    except Exception as e:
        utils.log(f"  [error] {label} failed: {e}")
        return None


def run_primary_verify(
    input_data: Dict,
    experiment_dir: str,
    with_rubric: bool = False,
    output_suffix: str = "",
    dry_run: bool = False,
) -> Dict:
    utils.log("\n=== 30B Verify (:8081) ===")
    suffix = f"_rubric{output_suffix}" if with_rubric else output_suffix
    findings_text = json.dumps(input_data["findings"], ensure_ascii=False)[:4000]
    system = (
        utils.VERIFY_SYSTEM_PROMPT
        if not with_rubric
        else utils.inject_rubric(utils.VERIFY_SYSTEM_PROMPT, "verify_primary")
    )
    messages = [
        {"role": "system", "content": system},
        {
            "role": "user",
            "content": f"Review {len(input_data['findings'])} findings from pipeline audit.\n\n"
            f"## Summary\n{json.dumps(input_data.get('severity_breakdown', {}), ensure_ascii=False)}\n"
            f"From {input_data['total_files_merged']} evaluation files.\n\n"
            f"## Findings\n{findings_text}",
        },
    ]
    utils.log("  [llm] Calling 30B...")
    response = call_llm_json(
        messages, "proposer", max_tokens=2048, label=f"30B_verify{suffix}", dry_run=dry_run
    )
    if not response:
        return {"error": "30B call failed"}
    result = {
        "phase": "primary_verify",
        "round": suffix or "norubric",
        "model": "proposer",
        "port": 8080,
        "with_rubric": with_rubric,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "usage": response["usage"],
        "timings": response["timings"],
        "elapsed_ms": response["elapsed_ms"],
        "result": response["result"],
    }
    fpath = os.path.join(experiment_dir, f"primary_verify{suffix}.json")
    with open(fpath, "w") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    utils.log(f"  [save] {fpath}")
    return result


def run_prj_combo(
    combo: Dict,
    input_data: Dict,
    experiment_dir: str,
    round_num: int,
    with_rubric: bool = False,
    dry_run: bool = False,
) -> Dict:
    suffix = f"_round{round_num}"
    rubric_tag = "_rubric" if with_rubric else ""
    utils.log(f"\n=== P-R-J {combo['name']} ===")
    findings_text = json.dumps(input_data["findings"], ensure_ascii=False)[:4000]

    # Proposer
    utils.log(f"  [llm] Proposer ({combo['proposer_model']})...")
    p_system = (
        utils.SYSTEM_P if not with_rubric else utils.inject_rubric(utils.SYSTEM_P, "proposer")
    )
    p_resp = call_llm_json(
        [{"role": "system", "content": p_system}, {"role": "user", "content": findings_text}],
        combo["proposer_model"],
        max_tokens=2048,
        label=f"P_{combo['proposer_model']}{rubric_tag}",
        dry_run=dry_run,
    )
    if not p_resp:
        return {"combo": combo["name"], "error": "Proposer failed"}
    with open(
        os.path.join(experiment_dir, f"proposer_{combo['name'][0]}{suffix}{rubric_tag}.json"), "w"
    ) as f:
        json.dump(p_resp, f, ensure_ascii=False, indent=2)

    # Refuter
    if not dry_run:
        utils.swap_inference(combo["refuter_mode"])
    p_findings = p_resp.get("result", {}).get("findings", [])
    utils.log(f"  [llm] Refuter ({combo['refuter_model']}) on {len(p_findings)} findings...")
    r_system = utils.SYSTEM_R if not with_rubric else utils.inject_rubric(utils.SYSTEM_R, "refuter")
    r_resp = call_llm_json(
        [
            {"role": "system", "content": r_system},
            {
                "role": "user",
                "content": f"# Findings\n{json.dumps(p_findings, ensure_ascii=False, indent=2)[:3000]}\n\n# Original data\n{findings_text[:2000]}",
            },
        ],
        combo["refuter_model"],
        label=f"R_{combo['refuter_model']}{rubric_tag}",
        dry_run=dry_run,
    )
    if not r_resp:
        return {"combo": combo["name"], "error": "Refuter failed"}
    with open(
        os.path.join(experiment_dir, f"refuter_{combo['name'][0]}{suffix}{rubric_tag}.json"), "w"
    ) as f:
        json.dump(r_resp, f, ensure_ascii=False, indent=2)

    # Judge
    if not dry_run:
        utils.swap_inference(combo["judge_mode"])
    r_verdicts = r_resp.get("result", {}).get("verdicts", [])
    utils.log(f"  [llm] Judge ({combo['judge_model']})...")
    j_system = utils.SYSTEM_J if not with_rubric else utils.inject_rubric(utils.SYSTEM_J, "judge")
    j_resp = call_llm_json(
        [
            {"role": "system", "content": j_system},
            {
                "role": "user",
                "content": f"## Findings\n{json.dumps(p_findings, ensure_ascii=False, indent=2)[:2000]}\n\n## Refuter Verdicts\n{json.dumps(r_verdicts, ensure_ascii=False, indent=2)[:2000]}",
            },
        ],
        combo["judge_model"],
        max_tokens=2048,
        label=f"J_{combo['judge_model']}{rubric_tag}",
        dry_run=dry_run,
    )
    if not j_resp:
        return {"combo": combo["name"], "error": "Judge failed"}
    with open(
        os.path.join(experiment_dir, f"judge_{combo['name'][0]}{suffix}{rubric_tag}.json"), "w"
    ) as f:
        json.dump(j_resp, f, ensure_ascii=False, indent=2)

    # Summary
    res = {
        "combo": combo["name"],
        "round": round_num,
        "with_rubric": with_rubric,
        "proposer": {"model": combo["proposer_model"], "result": p_resp.get("result", {})},
        "refuter": {"model": combo["refuter_model"], "result": r_resp.get("result", {})},
        "judge": {"model": combo["judge_model"], "result": j_resp.get("result", {})},
        "timings": {
            "total_ms": p_resp.get("elapsed_ms", 0)
            + r_resp.get("elapsed_ms", 0)
            + j_resp.get("elapsed_ms", 0)
        },
        "tokens": {
            "proposer": p_resp.get("usage", {}),
            "refuter": r_resp.get("usage", {}),
            "judge": j_resp.get("usage", {}),
        },
    }
    with open(
        os.path.join(experiment_dir, f"combo_{combo['name'][0]}{suffix}{rubric_tag}_summary.json"),
        "w",
    ) as f:
        json.dump(res, f, ensure_ascii=False, indent=2)
    return res


def compare_rounds(experiment_dir: str) -> Dict:
    utils.log("\n=== Comparison: Round 1 vs Round 2 ===")

    def load_scores(files):
        scores = []
        for f in files:
            d = json.load(open(f))
            j = d.get("judge", {}).get("result", {})
            scores.append(
                {
                    "combo": d["combo"],
                    "P_score": j.get("P_score", 0),
                    "R_score": j.get("R_score", 0),
                    "consensus": j.get("consensus_score", 0),
                }
            )
        return scores

    combos_nr = sorted(
        glob.glob(os.path.join(experiment_dir, "combo_*_round1_norubric_summary.json"))
    )
    combos_r = sorted(glob.glob(os.path.join(experiment_dir, "combo_*_rubric_round2_summary.json")))
    comparison = {"round1": load_scores(combos_nr), "round2": load_scores(combos_r)}
    with open(os.path.join(experiment_dir, "comparison_report.json"), "w") as f:
        json.dump(comparison, f, ensure_ascii=False, indent=2)
    return comparison
