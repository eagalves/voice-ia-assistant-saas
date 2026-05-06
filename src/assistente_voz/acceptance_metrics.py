from __future__ import annotations

from dataclasses import dataclass


def _levenshtein(a: list[str], b: list[str]) -> int:
    if not a:
        return len(b)
    if not b:
        return len(a)

    dp = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i in range(len(a) + 1):
        dp[i][0] = i
    for j in range(len(b) + 1):
        dp[0][j] = j

    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            dp[i][j] = min(
                dp[i - 1][j] + 1,
                dp[i][j - 1] + 1,
                dp[i - 1][j - 1] + cost,
            )
    return dp[len(a)][len(b)]


def compute_wer(references: list[str], hypotheses: list[str]) -> float:
    """Compute corpus-level Word Error Rate."""
    if len(references) != len(hypotheses):
        raise ValueError("references and hypotheses must have same length")

    total_words = 0
    total_errors = 0
    for ref, hyp in zip(references, hypotheses):
        ref_words = ref.strip().lower().split()
        hyp_words = hyp.strip().lower().split()
        total_words += len(ref_words)
        total_errors += _levenshtein(ref_words, hyp_words)

    if total_words == 0:
        return 0.0
    return total_errors / total_words


def compute_p95(values: list[float]) -> float:
    """Compute p95 using nearest-rank method."""
    if not values:
        raise ValueError("values cannot be empty")
    ordered = sorted(values)
    rank = int(round(0.95 * len(ordered) + 0.5))
    rank = max(1, min(rank, len(ordered)))
    return ordered[rank - 1]


@dataclass(frozen=True)
class StabilityResult:
    continuous_runtime_minutes: float
    technical_error_rate: float
    passed: bool


def evaluate_stability(
    continuous_runtime_minutes: float,
    total_events: int,
    technical_errors: int,
    min_runtime_minutes: float = 30.0,
    max_error_rate: float = 0.02,
) -> StabilityResult:
    if total_events <= 0:
        raise ValueError("total_events must be > 0")
    if technical_errors < 0:
        raise ValueError("technical_errors must be >= 0")

    error_rate = technical_errors / total_events
    passed = (
        continuous_runtime_minutes >= min_runtime_minutes
        and error_rate < max_error_rate
    )
    return StabilityResult(
        continuous_runtime_minutes=continuous_runtime_minutes,
        technical_error_rate=error_rate,
        passed=passed,
    )
