# Status: production
# Path: imported by scripts/ modules
"""LLM integration — unified client and rate estimators."""
from lib.llm.endpoint import call_llm_endpoint, _enable_keepalive
from lib.llm.rate_estimator import PromptCompletionRateEstimator, TimingsBasedRateEstimator
