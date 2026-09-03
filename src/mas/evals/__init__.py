"""Evaluation harness: deterministic metrics plus seeded-error probes."""

from .cases import CASES, SMOKE, Case
from .metrics import Metrics, aggregate, score_run
from .runner import compare, run_case, run_suite
from .label import LabelSet, Pair, pairs_from_runs, score_against_judge
from .seeded import PROBES, run_probes, score_probes

__all__ = [
    "Case", "CASES", "SMOKE",
    "Metrics", "score_run", "aggregate",
    "run_case", "run_suite", "compare",
    "PROBES", "run_probes", "score_probes",
    "LabelSet", "Pair", "pairs_from_runs", "score_against_judge",
]
