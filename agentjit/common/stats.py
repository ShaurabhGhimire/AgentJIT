"""One-sided Clopper-Pearson bounds on a binomial rate (section 7.6)."""
from scipy.stats import beta


def cp_upper(k: int, n: int, confidence: float = 0.95) -> float:
    """One-sided upper bound on the rate after k events in n trials."""
    if n <= 0:
        return 1.0
    if k >= n:
        return 1.0
    return float(beta.ppf(confidence, k + 1, n - k))


def cp_lower(k: int, n: int, confidence: float = 0.95) -> float:
    """One-sided lower bound on the rate after k events in n trials."""
    if n <= 0 or k <= 0:
        return 0.0
    return float(beta.ppf(1 - confidence, k, n - k + 1))


def cp_interval(k: int, n: int, confidence: float = 0.95) -> tuple[float, float]:
    """Two-sided interval, for reporting eval metrics."""
    tail = (1 - confidence) / 2
    return cp_lower(k, n, 1 - tail), cp_upper(k, n, 1 - tail)
