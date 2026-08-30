# Status: experimental
# Path: imported by — cli.py, orchestrator.py
"""Multi-agent debate package — LocalDebate + CooperativeDebate.

Submodules:
  local_debate.py       — LocalDebate, LocalDebateReview (inference+B local debate)
  cooperative_debate.py — CooperativeDebate (Azure spot VM orchestration)
  cooperative_remote.py — SSH tunnel management
  debate_data.py        — Model catalogue, prompts, config
  debate_llm.py         — LLM calling, JSON parsing, model switching
"""
