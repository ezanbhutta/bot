"""Statistical machinery for the gauntlet.

References:
  * Bailey & López de Prado (2012), "The Sharpe Ratio Efficient Frontier"
    — Probabilistic Sharpe Ratio (PSR) and Minimum Track Record Length.
  * Bailey & López de Prado (2014), "The Deflated Sharpe Ratio".
  * Bailey, Borwein, López de Prado, Zhu (2017), "The Probability of
    Backtest Overfitting" — CSCV.

All Sharpe ratios here are PER-EVENT (per listing trade), never annualized:
annualizing an event strategy invites flattering assumptions about capital
redeployment, and every formula below (PSR/DSR/MinTRL) is expressed in the
same per-observation units, so nothing needs annualization.
"""
import itertools
import math

import numpy as np
from scipy import stats as sps

EULER_GAMMA = 0.5772156649015329


def _clean(x):
    x = np.asarray(x, dtype=np.float64)
    return x[~np.isnan(x)]


def sharpe(returns) -> float:
    r = _clean(returns)
    if len(r) < 2:
        return float("nan")
    sd = r.std(ddof=1)
    if sd == 0:
        return float("nan")
    return float(r.mean() / sd)


def moments(returns):
    """(n, mean, std, skew, kurtosis) — kurtosis is NON-excess (normal=3),
    as used in the PSR/DSR papers."""
    r = _clean(returns)
    n = len(r)
    if n < 4:
        return n, float("nan"), float("nan"), 0.0, 3.0
    return (
        n,
        float(r.mean()),
        float(r.std(ddof=1)),
        float(sps.skew(r, bias=False)),
        float(sps.kurtosis(r, bias=False, fisher=False)),
    )


def psr(sr_hat, sr_benchmark, n, skew, kurt) -> float:
    """Probabilistic Sharpe Ratio: P[true SR > sr_benchmark]."""
    if n < 2 or math.isnan(sr_hat):
        return float("nan")
    denom_sq = 1.0 - skew * sr_hat + (kurt - 1.0) / 4.0 * sr_hat**2
    if denom_sq <= 0:
        return float("nan")
    z = (sr_hat - sr_benchmark) * math.sqrt(n - 1) / math.sqrt(denom_sq)
    return float(sps.norm.cdf(z))


def expected_max_sharpe(n_trials, var_sr) -> float:
    """E[max SR] across n_trials of zero-true-SR strategies (DSR paper eq. 4).

    Using the raw trial count is CONSERVATIVE here: our grid cells are
    positively correlated, which would lower the true expected maximum, so
    the deflation hurdle we set is at least as high as the honest one.
    """
    if n_trials <= 1 or var_sr <= 0:
        return 0.0
    sd = math.sqrt(var_sr)
    return sd * (
        (1.0 - EULER_GAMMA) * sps.norm.ppf(1.0 - 1.0 / n_trials)
        + EULER_GAMMA * sps.norm.ppf(1.0 - 1.0 / (n_trials * math.e))
    )


def expected_max_sharpe_null_mc(trial_ns, n_draws=200_000, seed=20260703):
    """E[max SR-hat] under the DSR's own null — every trial has TRUE SR = 0 —
    with each trial's estimation variance 1/(n_i - 1) taken from its actual
    observation count (Mertens variance at SR=0). Monte Carlo because the
    closed-form expected-max assumes a COMMON variance, which heterogeneous
    per-trial n violates. Independence across trials is assumed, which
    OVERSTATES effective multiplicity for our positively-correlated grid —
    i.e. this hurdle is conservative. Deterministic seed for reproducibility.
    """
    ns = np.asarray([n for n in trial_ns if n and n > 1], dtype=np.float64)
    if len(ns) == 0:
        return 0.0
    sds = np.sqrt(1.0 / (ns - 1.0))
    rng = np.random.default_rng(seed)
    draws = rng.standard_normal((n_draws, len(sds))) * sds
    return float(draws.max(axis=1).mean())


def deflated_sharpe(sr_hat, trial_sharpes, n, skew, kurt):
    """DSR = PSR evaluated against the expected-max-SR benchmark.

    trial_sharpes: the observed SR of EVERY variant tried (full grid +
    exhibits), used both for the trial count and for V[SR].
    N counts every trial CONDUCTED — a variant whose SR is undefined (too
    few observations) was still a trial; dropping it would lower the
    deflation hurdle, which is the anti-conservative direction.
    Returns (dsr_probability, sr_star_benchmark, n_trials).
    """
    n_trials = len(list(trial_sharpes))
    trials = _clean(trial_sharpes)
    var_sr = float(trials.var(ddof=1)) if len(trials) > 1 else 0.0
    sr_star = expected_max_sharpe(n_trials, var_sr)
    return psr(sr_hat, sr_star, n, skew, kurt), sr_star, n_trials


def min_track_record_length(sr_hat, sr_benchmark, skew, kurt,
                            confidence=0.95) -> float:
    """Observations needed for PSR(sr_benchmark) >= confidence (MinTRL)."""
    if math.isnan(sr_hat) or sr_hat <= sr_benchmark:
        return float("inf")
    z = sps.norm.ppf(confidence)
    num = 1.0 - skew * sr_hat + (kurt - 1.0) / 4.0 * sr_hat**2
    if num <= 0:
        return float("inf")
    return 1.0 + num * (z / (sr_hat - sr_benchmark)) ** 2


def pbo_cscv(matrix, n_blocks=12, metric=sharpe):
    """Probability of Backtest Overfitting via CSCV.

    matrix: (T x N) per-event returns, rows time-ordered, one column per
    strategy variant. NaNs allowed (nan-aware metric).
    Returns (pbo, n_splits, logits).
    """
    M = np.asarray(matrix, dtype=np.float64)
    T, N = M.shape
    if T < 2 * n_blocks or N < 2:
        return float("nan"), 0, []
    # S chronological blocks of (near-)equal size.
    edges = np.linspace(0, T, n_blocks + 1, dtype=int)
    blocks = [np.arange(edges[i], edges[i + 1]) for i in range(n_blocks)]
    logits = []
    half = n_blocks // 2
    for combo in itertools.combinations(range(n_blocks), half):
        train_idx = np.concatenate([blocks[i] for i in combo])
        test_mask = np.ones(n_blocks, dtype=bool)
        test_mask[list(combo)] = False
        test_idx = np.concatenate(
            [blocks[i] for i in range(n_blocks) if test_mask[i]]
        )
        perf_is = np.array([metric(M[train_idx, j]) for j in range(N)])
        perf_oos = np.array([metric(M[test_idx, j]) for j in range(N)])
        if np.all(np.isnan(perf_is)) or np.all(np.isnan(perf_oos)):
            continue
        best = int(np.nanargmax(perf_is))
        oos_best = perf_oos[best]
        if np.isnan(oos_best):
            # The IS-best variant produced no OOS observations at all —
            # count it as a failure to generalize (rank 0), the pessimistic
            # and honest treatment.
            omega = 1.0 / (N + 1)
        else:
            rank = float(np.sum(perf_oos[~np.isnan(perf_oos)] <= oos_best))
            omega = rank / (np.sum(~np.isnan(perf_oos)) + 1)
        omega = min(max(omega, 1e-9), 1 - 1e-9)
        logits.append(math.log(omega / (1.0 - omega)))
    if not logits:
        return float("nan"), 0, []
    pbo = float(np.mean(np.asarray(logits) <= 0.0))
    return pbo, len(logits), logits
