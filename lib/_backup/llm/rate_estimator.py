#!/usr/bin/env python3
# Status: production
# Path: imported by — production scripts
"""Rate estimators for LLM inference — token-count-based and timings-based.

PromptCompletionRateEstimator: EMA + median-based, takes token counts + elapsed.
  Uses 70/30 heuristic to split elapsed time between eval and generation.
  (formerly lib/estimator.py:RateEstimator)

TimingsBasedRateEstimator: EMA-based, takes timings dict from llama.cpp API.
  Uses prompt_per_second and predicted_per_second directly.
  (formerly review_worker.py:RateEstimator)
"""


class PromptCompletionRateEstimator:
    """EMA + median based rate tracker. Self-calibrating from live measurements."""

    def __init__(self, initial_prompt_eval: float = 2.0, initial_gen: float = 2.15,
                 alpha: float = 0.3):
        self.prompt_eval_rate = initial_prompt_eval
        self.gen_rate = initial_gen
        self.alpha = alpha
        self.prompt_samples: list = []
        self.gen_samples: list = []

    def update(self, prompt_tokens: int, completion_tokens: int, elapsed_s: float):
        """Update rates from live usage data.

        Uses 70/30 heuristic to split elapsed time between eval and generation
        since llama.cpp /v1/chat/completions doesn't expose per-phase timings.
        """
        if elapsed_s <= 0 or (prompt_tokens <= 0 and completion_tokens <= 0):
            return
        eval_time = elapsed_s * 0.7
        gen_time = elapsed_s * 0.3
        if eval_time > 0 and prompt_tokens > 0:
            rate = prompt_tokens / eval_time
            self.prompt_samples.append(rate)
            if len(self.prompt_samples) > 20:
                self.prompt_samples.pop(0)
            self.prompt_eval_rate = self.exponential_moving_average(self.prompt_eval_rate, rate)
        if gen_time > 0 and completion_tokens > 0:
            rate = completion_tokens / gen_time
            self.gen_samples.append(rate)
            if len(self.gen_samples) > 20:
                self.gen_samples.pop(0)
            self.gen_rate = self.exponential_moving_average(self.gen_rate, rate)

    def exponential_moving_average(self, old: float, new: float) -> float:
        return self.alpha * new + (1 - self.alpha) * old

    def median_prompt_rate(self) -> float:
        if len(self.prompt_samples) >= 3:
            return sorted(self.prompt_samples)[len(self.prompt_samples) // 2]
        return self.prompt_eval_rate

    def median_gen_rate(self) -> float:
        if len(self.gen_samples) >= 3:
            return sorted(self.gen_samples)[len(self.gen_samples) // 2]
        return self.gen_rate


class TimingsBasedRateEstimator:
    """EMA + median rate tracker, self-calibrating from timings field."""

    def __init__(self, label: str = "", initial_prompt: float = 15.0,
                 initial_gen: float = 5.0, alpha: float = 0.3):
        self.label = label
        self.prompt_rate = initial_prompt
        self.gen_rate = initial_gen
        self.alpha = alpha
        self._samples: list = []
        self.calls = 0
        self.cache_hits = 0
        self._total_prompt = 0
        self._total_gen = 0
        self._total_elapsed = 0.0

    def update(self, timings: dict):
        pr = timings.get("prompt_per_second", 0)
        gr = timings.get("predicted_per_second", 0)
        if pr > 0:
            self.prompt_rate = self.exponential_moving_average(self.prompt_rate, pr)
        if gr > 0:
            self.gen_rate = self.exponential_moving_average(self.gen_rate, gr)
            self._samples.append(gr)
            if len(self._samples) > 50:
                self._samples.pop(0)
        self.calls += 1
        self._total_prompt += timings.get("prompt_n", 0)
        self._total_gen += timings.get("predicted_n", 0)
        self._total_elapsed += timings.get("predicted_ms", 0) + timings.get("prompt_ms", 0)
        if timings.get("cache_n", 0) > 0:
            self.cache_hits += 1

    def exponential_moving_average(self, old: float, new: float) -> float:
        return self.alpha * new + (1 - self.alpha) * old

    def calc_timeout(self, prompt_tokens: int, max_tokens: int, buffer: int = 30) -> int:
        return int(prompt_tokens / max(self.prompt_rate, 0.5)
                   + max_tokens / max(self.gen_rate, 0.5) + buffer)

    def stats(self) -> dict:
        return {
            "prompt_rate": round(self.prompt_rate, 1),
            "gen_rate": round(self.gen_rate, 1),
            "calls": self.calls,
            "cache_hits": self.cache_hits,
            "total_prompt": self._total_prompt,
            "total_gen": self._total_gen,
            "total_elapsed_s": round(self._total_elapsed / 1000, 1),
        }

