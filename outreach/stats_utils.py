"""
Minimal statistics helpers for outreach/variant_optimizer_job.py — no scipy
dependency (not currently in requirements.txt, and this is the only place
in the codebase that would need it). A two-proportion z-test is ~15 lines
of real math on top of the standard-library erf function; adding a heavy
numerical dependency for that alone isn't worth it.
"""
import math


def two_proportion_z_test(successes_a, n_a, successes_b, n_b):
    """
    Two-tailed two-proportion z-test (pooled variance) — the standard test
    for "is variant B's rate genuinely different from variant A's rate,"
    given each is a count of successes out of a count of trials.

    Returns (z, p_value). Returns (0.0, 1.0) — "no evidence of a
    difference" — if either sample is empty, since the test is undefined
    there (division by zero) and "no data" should never be reported as
    "significant."
    """
    if n_a <= 0 or n_b <= 0:
        return 0.0, 1.0

    p_a = successes_a / n_a
    p_b = successes_b / n_b
    p_pool = (successes_a + successes_b) / (n_a + n_b)

    se = math.sqrt(p_pool * (1 - p_pool) * (1 / n_a + 1 / n_b))
    if se == 0:
        # Both rates identical (often both exactly 0 or both exactly 1) —
        # genuinely no difference, not an error case to special-case away.
        return 0.0, 1.0

    z = (p_b - p_a) / se
    # Two-tailed p-value from the standard normal CDF via erf.
    p_value = 2 * (1 - _standard_normal_cdf(abs(z)))
    return z, p_value


def _standard_normal_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def is_significant(p_value, alpha=0.05):
    return p_value < alpha
