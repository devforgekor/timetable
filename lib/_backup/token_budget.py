#!/usr/bin/env python3
# Status: production
# Path: imported by — pipelines/prj_cycle.py
"""Token budget manager for P-R-J pipeline context allocation."""


CHARS_PER_TOKEN = 2.5

PHASE_BUDGET = {
    "day_verify": 1500,
    "prj_proposer": 2000,
    "prj_reflector": 1200,
    "prj_judge": 1200,
    "handoff": 1500,
    "final_verify": 2000,
    # night.py v3.0 phases
    "night_initial_verify": 1500,    # Phase 2: initial verification — findings summary
    "rubric": 2000,              # Rubric evaluation — score context
    "night_proposer": 3000,             # Phase 4 P: proposer (budget increased for richer context)
    "night_reflector": 1200,             # Phase 4 R: per-finding refuter
    "night_judge": 1500,             # Phase 4 J: judge
    "night_final_verify": 2000,           # Phase 5: final verify
    # Enrich pipelines
    "enrich": 4000,
    "enrich_verify": 2500,
}


def _estimate_tokens(text: str) -> int:
    return int(len(text) / CHARS_PER_TOKEN)


class TokenBudget:
    """Allocate context tokens by priority. Highest priority fills first."""

    def __init__(self, phase: str):
        self.limit = PHASE_BUDGET.get(phase, 1500)
        self.soft_limit = int(self.limit * 0.8)
        self.used = 0
        self.sections: list[dict] = []
        self._over = False

    def add_section(self, label: str, text: str, priority: int = 5) -> bool:
        if self._over:
            return False
        tok = _estimate_tokens(text)
        if self.used + tok > self.soft_limit:
            allowed = max(0, self.limit - self.used - 10)
            if allowed > 80:
                self.sections.append({
                    "label": label, "tok": allowed + 10,
                    "truncated": True, "original_tok": tok,
                })
                self.used = self.limit
                self._over = True
                return False
            self._over = True
            return False
        self.used += tok
        self.sections.append({"label": label, "tok": tok, "truncated": False})
        return True

    def remaining(self) -> int:
        return max(0, self.limit - self.used)

    def summary(self) -> str:
        n_total = len(self.sections)
        n_cut = sum(1 for s in self.sections if s.get("truncated"))
        cut_str = f", {n_cut} truncated" if n_cut else ""
        return f"context budget: {self.used}/{self.limit} tok ({n_total} sections{cut_str})"

    def __repr__(self) -> str:
        return f"<TokenBudget {self.used}/{self.limit} tok>"
