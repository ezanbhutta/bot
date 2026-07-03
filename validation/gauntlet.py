"""The validation gauntlet (VALIDATION_GAUNTLET.md stages 1-6).

Design note: NET (friction-included) returns are used in every stage, so no
stage can pass on frictionless numbers; Stage 5 additionally reports the
gross-vs-net gap so a human can see exactly what friction ate.

H-B is evaluated as the SHORT side of buying the listing (HYPOTHESIS.md
spot-only interpretation): strategy return = net short return, so positive
numbers mean the fade has edge. Spot execution of a REAL verdict here means
"do not buy listings"; short execution would be a separate futures project.
"""
import math

import numpy as np

from . import config, returns, stats


def _cell_table(matrix, names, mask=None):
    """Per-config summary rows: (name, n, mean, sr, win_rate)."""
    rows = []
    for j, name in enumerate(names):
        col = matrix[:, j] if mask is None else matrix[mask, j]
        col = col[~np.isnan(col)]
        if len(col) == 0:
            rows.append((name, 0, float("nan"), float("nan"), float("nan")))
            continue
        rows.append(
            (
                name, len(col), float(col.mean()), stats.sharpe(col),
                float((col > 0).mean()),
            )
        )
    return rows


def stage1_sanity(mat):
    M, names = mat["net_short"], mat["config_names"]
    n_events = int(np.sum(~np.all(np.isnan(M), axis=1)))
    n_no_data = len(mat.get("no_data_symbols", []))
    res = {
        "name": "S1 in-sample sanity",
        "n_events": n_events,
        "n_no_data": n_no_data,
        "pass": None,
        "insufficient": n_events < config.N_MIN_EVENTS,
    }
    if n_no_data:
        # Qualifying events with zero klines are a DATA failure, not a
        # market outcome — they must be visible, never silently dropped.
        res["no_data_symbols"] = mat["no_data_symbols"]
    if res["insufficient"]:
        res["key_stat"] = f"N={n_events} < N_min={config.N_MIN_EVENTS}"
        res["pass"] = False
        return res
    split = int(n_events * config.TRAIN_FRACTION)
    train_mask = np.zeros(len(M), dtype=bool)
    train_mask[:split] = True
    table = _cell_table(M, names, train_mask)
    valid = [r for r in table if r[1] >= 10]
    best = max(valid, key=lambda r: (r[3] if not math.isnan(r[3]) else -9e9),
               default=None)
    n_pos = sum(1 for r in valid if r[2] > 0)
    res.update(
        {
            "train_table": table,
            "train_n": split,
            "best_train_cell": best,
            "n_cells_positive_mean": n_pos,
            "n_cells_valid": len(valid),
            "pass": True,  # gate only fails on sample size; merits die in S2-5
            "key_stat": (
                (f"N={n_events}"
                 + (f" (+{n_no_data} events MISSING KLINES — data gap!)"
                    if n_no_data else "")
                 + f"; train cells with mean>0: {n_pos}/{len(valid)}; "
                   f"best train SR={best[3]:.3f} ({best[0]})")
                if best else f"N={n_events}; no valid cells"
            ),
        }
    )
    return res


def stage2_walk_forward(mat):
    """Chronological expanding-window folds; select best grid cell on train,
    evaluate the SAME cell on the untouched next block."""
    M, names = mat["net_short"], mat["config_names"]
    N = len(M)
    t0s = np.array([ev["first_trade_time"] for ev in mat["events"]])
    purge_ms = config.WF_PURGE_DAYS * 86_400_000
    k = config.WF_N_FOLDS
    edges = np.linspace(0, N, k + 1, dtype=int)
    folds = []
    pooled_test, pooled_train_sr = [], []
    for f in range(1, k):
        test_start_ms = t0s[edges[f]]
        # Purged train: drop events whose forward window is still open at
        # the test fold's start (their returns embed test-period prices).
        tr = np.arange(0, edges[f])
        tr = tr[t0s[tr] < test_start_ms - purge_ms]
        te = np.arange(edges[f], edges[f + 1])
        cand = []
        for j in range(M.shape[1]):
            col = M[tr, j]
            col = col[~np.isnan(col)]
            if len(col) >= 10:
                s = stats.sharpe(col)
                if not math.isnan(s):
                    cand.append((s, float(col.mean()), j))
        sel = max(cand, default=None)
        if sel is None or sel[0] <= 0:
            folds.append(
                {"fold": f, "selected": None, "train_sr": sel[0] if sel else None,
                 "note": "no cell with positive train SR -> rule stays flat"}
            )
            continue
        tr_sr, tr_mean, j = sel
        te_col = M[te, j]
        te_col = te_col[~np.isnan(te_col)]
        folds.append(
            {
                "fold": f, "selected": names[j], "train_sr": tr_sr,
                "train_mean": tr_mean,
                "test_n": len(te_col),
                "test_sr": stats.sharpe(te_col),
                "test_mean": float(te_col.mean()) if len(te_col) else float("nan"),
            }
        )
        pooled_test.extend(te_col.tolist())
        pooled_train_sr.append(tr_sr)

    pooled_test = np.asarray(pooled_test)
    active = [f for f in folds if f.get("selected")]
    pos_folds = sum(1 for f in active if f.get("test_mean", 0) > 0)
    pooled_sr = stats.sharpe(pooled_test) if len(pooled_test) > 1 else float("nan")
    pooled_mean = float(pooled_test.mean()) if len(pooled_test) else float("nan")
    mean_train_sr = float(np.mean(pooled_train_sr)) if pooled_train_sr else float("nan")

    passed = (
        len(active) > 0
        and len(pooled_test) >= 10
        and pooled_mean > 0
        and not math.isnan(pooled_sr)
        and pooled_sr > 0
        and pooled_sr >= config.WF_MIN_TEST_TRAIN_SR_RATIO * mean_train_sr
        and pos_folds * 2 >= len(active)
    )
    return {
        "name": "S2 walk-forward",
        "folds": folds,
        "pooled_test_n": int(len(pooled_test)),
        "pooled_test_mean": pooled_mean,
        "pooled_test_sr": pooled_sr,
        "mean_train_sr": mean_train_sr,
        "pooled_test_returns": pooled_test,
        "pass": bool(passed),
        "key_stat": (
            f"OOS SR={pooled_sr:.3f} vs train SR={mean_train_sr:.3f} "
            f"(n={len(pooled_test)}, {pos_folds}/{len(active)} folds positive)"
            if len(pooled_test)
            else "no fold ever selected a tradeable cell"
        ),
    }


def _best_grid_cell(mat):
    """Full-sample best cell of the fade grid (the claim under test)."""
    M, names = mat["net_short"], mat["config_names"]
    best = None
    for j in range(M.shape[1]):
        col = M[:, j]
        col = col[~np.isnan(col)]
        if len(col) < 10:
            continue
        s = stats.sharpe(col)
        if not math.isnan(s) and (best is None or s > best[0]):
            best = (s, j, col)
    return best  # (sr, col_index, returns) or None


def stage3_deflated_sharpe(mat, rev):
    """Deflated Sharpe with the hurdle computed under the theorem's own
    null: all trials have true SR = 0, per-trial estimation variance from
    each trial's actual n, independence assumed (conservative for our
    positively-correlated grid).

    The paper's plug-in estimator (V = cross-sectional variance of OBSERVED
    trial SRs) is also computed and reported, but it is only consistent for
    homogeneous same-idea trials. Our pre-declared grid intentionally
    contains control cells with strongly NEGATIVE true SR (shorting the
    listing-minute open), which inflate the plug-in variance with true-SR
    dispersion the null does not have; mechanically, adding more honest bad
    controls to the report would push ANY strategy to GHOST under the
    plug-in, which punishes full reporting rather than selection luck.
    Adjudicated by an outcome-blind methodology panel (3 independent
    reviews): the null-calibrated hurdle governs; the plug-in is disclosed.
    """
    best = _best_grid_cell(mat)
    if best is None:
        return {"name": "S3 deflated Sharpe", "pass": False,
                "key_stat": "no cell with enough observations"}
    sr_hat, j, col = best
    names = mat["config_names"]
    # Trials = EVERY variant this project evaluated: 24 grid cells + the
    # pre-declared reversion exhibit configs. Understating this count is the
    # classic way DSR gets gamed, so both matrices contribute.
    trial_cols = [mat["net_short"][:, k]
                  for k in range(mat["net_short"].shape[1])]
    if rev is not None and rev["net_long"].size:
        trial_cols += [rev["net_long"][:, k]
                       for k in range(rev["net_long"].shape[1])]
    trial_srs = [stats.sharpe(c) for c in trial_cols]
    trial_ns = [int(np.sum(~np.isnan(c))) for c in trial_cols]
    n, _, _, skew, kurt = stats.moments(col)
    # Governing hurdle: null-calibrated expected max (per-trial variances).
    sr_star = stats.expected_max_sharpe_null_mc(trial_ns)
    dsr = stats.psr(sr_hat, sr_star, n, skew, kurt)
    # Disclosed alternative: the paper's plug-in estimator.
    dsr_plugin, sr_star_plugin, n_trials = stats.deflated_sharpe(
        sr_hat, trial_srs, n, skew, kurt
    )
    passed = (not math.isnan(dsr)) and dsr >= config.DSR_CONFIDENCE
    return {
        "name": "S3 deflated Sharpe",
        "best_cell": names[j],
        "sr": sr_hat, "skew": skew, "kurt": kurt, "n": n,
        "sr_star": sr_star, "n_trials": n_trials, "dsr": dsr,
        "sr_star_plugin": sr_star_plugin, "dsr_plugin": dsr_plugin,
        "pass": bool(passed),
        "key_stat": (
            f"DSR={dsr:.3f} vs null-calibrated hurdle SR*={sr_star:.3f} "
            f"over {n_trials} trials (need >= {config.DSR_CONFIDENCE}); "
            f"best cell {names[j]} SR={sr_hat:.3f}; paper plug-in variant: "
            f"SR*={sr_star_plugin:.3f} -> DSR={dsr_plugin:.3f} (disclosed, "
            f"not governing: trial family has heterogeneous true SRs by design)"
        ),
    }


def stage4_pbo(mat, rev):
    M = mat["net_short"]
    if rev is not None and rev["net_long"].size and len(rev["events"]) == len(mat["events"]):
        M = np.hstack([M, rev["net_long"]])
    pbo, n_splits, _ = stats.pbo_cscv(M, n_blocks=config.PBO_N_BLOCKS)
    passed = (not math.isnan(pbo)) and pbo <= config.PBO_MAX
    return {
        "name": "S4 PBO (CSCV)",
        "pbo": pbo, "n_splits": n_splits, "n_variants": M.shape[1],
        "pass": bool(passed),
        "key_stat": f"PBO={pbo:.3f} over {n_splits} CSCV splits "
                    f"(need <= {config.PBO_MAX})",
    }


def stage5_friction(mat):
    """All stages already ran on net returns; this stage makes friction's
    bite explicit and fails the run if the selected edge's NET expectancy
    is not positive."""
    best = _best_grid_cell(mat)
    if best is None:
        return {"name": "S5 friction", "pass": False,
                "key_stat": "no valid cell"}
    sr_hat, j, col = best
    gross_short = -mat["gross_long"][:, j]  # frictionless short return
    gross_short = gross_short[~np.isnan(gross_short)]
    net_mean = float(col.mean())
    gross_mean = float(gross_short.mean()) if len(gross_short) else float("nan")
    drag = gross_mean - net_mean
    passed = net_mean > 0
    return {
        "name": "S5 friction",
        "best_cell": mat["config_names"][j],
        "gross_mean": gross_mean, "net_mean": net_mean, "friction_drag": drag,
        "pass": bool(passed),
        "key_stat": (
            f"net mean={net_mean*100:.2f}%/event vs gross "
            f"{gross_mean*100:.2f}% (friction eats {drag*100:.2f} pp)"
        ),
    }


def stage6_min_trl(mat):
    best = _best_grid_cell(mat)
    if best is None:
        return {"name": "S6 MinTRL", "pass": False, "insufficient": True,
                "key_stat": "no valid cell"}
    sr_hat, j, col = best
    n, _, _, skew, kurt = stats.moments(col)
    mtrl = stats.min_track_record_length(
        sr_hat, config.SHARPE_BENCHMARK, skew, kurt, config.MINTRL_CONFIDENCE
    )
    passed = n >= mtrl
    return {
        "name": "S6 MinTRL",
        "n": n, "min_trl": mtrl,
        "pass": bool(passed),
        "insufficient": not passed,
        "key_stat": (
            f"have n={n} events, need >= {mtrl:.0f} for "
            f"{int(config.MINTRL_CONFIDENCE*100)}% confidence"
            if math.isfinite(mtrl)
            else f"MinTRL infinite (SR <= benchmark); n={n}"
        ),
    }


def verdict_from_stages(s1, s2, s3, s4, s5, s6):
    if s1.get("insufficient"):
        return "INCONCLUSIVE", "sample below N_min at Stage 1"
    for s, label in ((s2, "walk-forward"), (s3, "deflated Sharpe"),
                     (s4, "PBO"), (s5, "friction")):
        if not s.get("pass"):
            return "GHOST", f"failed {label} ({s.get('key_stat')})"
    if not s6.get("pass"):
        return "INCONCLUSIVE", f"history shorter than MinTRL ({s6.get('key_stat')})"
    return "REAL EDGE", "cleared all six stages"


def run_hb(conn, events):
    mat = returns.build_matrices(conn, events)
    rev = returns.build_reversion_exhibit(conn, events)
    desc = returns.descriptive_stats(conn, mat["events"])

    s1 = stage1_sanity(mat)
    if s1.get("insufficient"):
        empty = {"name": "-", "pass": False, "key_stat": "not reached"}
        v, why = "INCONCLUSIVE", "sample below N_min at Stage 1"
        return {"hypothesis": "H-B", "stages": [s1] + [empty] * 5,
                "verdict": v, "why": why, "matrices": mat, "reversion": rev,
                "descriptive": desc}
    s2 = stage2_walk_forward(mat)
    s3 = stage3_deflated_sharpe(mat, rev)
    s4 = stage4_pbo(mat, rev)
    s5 = stage5_friction(mat)
    s6 = stage6_min_trl(mat)
    v, why = verdict_from_stages(s1, s2, s3, s4, s5, s6)
    return {
        "hypothesis": "H-B", "stages": [s1, s2, s3, s4, s5, s6],
        "verdict": v, "why": why, "matrices": mat, "reversion": rev,
        "descriptive": desc,
    }
