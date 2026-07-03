"""Unit tests for the gauntlet's statistical machinery.

These pin the behavior the verdict depends on: if any of these fail, no
verdict from the engine can be trusted (ACCEPTANCE.md: the only failure is a
verdict that can't be trusted).
"""
import math

import numpy as np

from validation import stats


def test_sharpe_known_value():
    r = np.array([0.01, 0.02, 0.03, 0.00, 0.04])
    assert abs(stats.sharpe(r) - r.mean() / r.std(ddof=1)) < 1e-12


def test_sharpe_nan_aware():
    r = np.array([0.01, np.nan, 0.03, np.nan, 0.02])
    assert not math.isnan(stats.sharpe(r))


def test_psr_zero_sr_is_half():
    # SR equal to the benchmark -> exactly 50% probability.
    assert abs(stats.psr(0.3, 0.3, 100, 0.0, 3.0) - 0.5) < 1e-9


def test_psr_grows_with_n():
    lo = stats.psr(0.2, 0.0, 30, 0.0, 3.0)
    hi = stats.psr(0.2, 0.0, 300, 0.0, 3.0)
    assert hi > lo > 0.5


def test_psr_fat_tails_penalized():
    normal = stats.psr(0.3, 0.0, 100, 0.0, 3.0)
    fat = stats.psr(0.3, 0.0, 100, -1.0, 10.0)  # left-skewed, fat-tailed
    assert fat < normal


def test_expected_max_sharpe_monotone_in_trials():
    v = 0.1
    e2 = stats.expected_max_sharpe(2, v)
    e28 = stats.expected_max_sharpe(28, v)
    e500 = stats.expected_max_sharpe(500, v)
    assert 0 < e2 < e28 < e500


def test_null_mc_hurdle_behavior():
    # More trials -> higher null max hurdle.
    few = stats.expected_max_sharpe_null_mc([400] * 5, n_draws=50_000)
    many = stats.expected_max_sharpe_null_mc([400] * 28, n_draws=50_000)
    assert 0 < few < many
    # Noisier trials (smaller n) -> higher hurdle.
    noisy = stats.expected_max_sharpe_null_mc([400] * 24 + [60] * 4,
                                              n_draws=50_000)
    assert noisy > many
    # Deterministic across calls (seeded).
    a = stats.expected_max_sharpe_null_mc([400] * 10)
    b = stats.expected_max_sharpe_null_mc([400] * 10)
    assert a == b
    # Sanity: hurdle for 28 trials of n=400 must sit near the closed-form
    # common-variance value.
    closed = stats.expected_max_sharpe(28, 1.0 / 399)
    assert abs(many - closed) < 0.02


def test_dsr_below_psr_when_many_trials():
    rng = np.random.default_rng(7)
    trials = rng.normal(0.0, 0.2, 28)  # SRs of 28 noise variants
    sr_hat = float(trials.max())
    plain = stats.psr(sr_hat, 0.0, 120, 0.0, 3.0)
    dsr, sr_star, n = stats.deflated_sharpe(sr_hat, trials, 120, 0.0, 3.0)
    assert n == 28
    assert sr_star > 0
    assert dsr < plain  # deflation must raise the bar


def test_mintrl_self_consistent_with_psr():
    sr, skew, kurt = 0.25, -0.5, 5.0
    mtrl = stats.min_track_record_length(sr, 0.0, skew, kurt, 0.95)
    # At exactly MinTRL observations, PSR should be ~= the confidence level.
    assert abs(stats.psr(sr, 0.0, mtrl, skew, kurt) - 0.95) < 1e-6


def test_mintrl_infinite_when_no_edge():
    assert math.isinf(stats.min_track_record_length(0.0, 0.0, 0.0, 3.0))
    assert math.isinf(stats.min_track_record_length(-0.2, 0.0, 0.0, 3.0))


def test_pbo_high_on_pure_noise():
    rng = np.random.default_rng(11)
    M = rng.normal(0.0, 0.05, size=(240, 20))
    pbo, n_splits, _ = stats.pbo_cscv(M, n_blocks=12)
    assert n_splits == 924
    # Pure noise: best in-sample config should be ~random out-of-sample.
    assert 0.30 <= pbo <= 0.70


def test_pbo_low_on_genuine_signal():
    rng = np.random.default_rng(13)
    M = rng.normal(0.0, 0.05, size=(240, 20))
    M[:, 3] += 0.06  # one variant with a real, large edge
    pbo, _, _ = stats.pbo_cscv(M, n_blocks=12)
    assert pbo <= 0.10


def test_pbo_needs_enough_rows():
    pbo, n, _ = stats.pbo_cscv(np.zeros((10, 5)), n_blocks=12)
    assert math.isnan(pbo) and n == 0
