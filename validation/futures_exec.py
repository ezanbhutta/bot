"""H-B short-side FUTURES execution validation — docs/DECISIONS.md D3.

Pre-declared, exactly two configs (F1 primary, F2 availability-adjusted),
both fully reported, both in the trial ledger. This leg answers one
question: does the spot-validated fade cell (+1h entry, 14d hold) survive
on the venue where a short can actually exist — USDS-M perps — after perp
availability, futures fees, adverse fills on the perp's own bars, and
FUNDING transfers over the full hold?
"""
import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import numpy as np

from . import config, friction, futures_data, stats, storage

BAR_TOL_MS = 30 * 60_000
WINDOW_MS = 3 * 3_600_000


def _date_of(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------

def ingest(conn, events, progress=print, workers=10):
    sess = futures_data._session()
    progress("[futures] enumerating perp universe from the archive ...")
    universe = futures_data.get_perp_universe(sess)
    progress(f"[futures] {len(universe)} perp symbols ever listed")

    known_meta = dict(
        (r[0], r[1:]) for r in conn.execute(
            "SELECT spot_symbol, perp_symbol, first_date, last_date FROM perp_meta"
        )
    )
    have_fund = {s for (s,) in conn.execute(
        "SELECT DISTINCT symbol FROM funding_rates")}
    have_kl = {s for (s,) in conn.execute(
        "SELECT DISTINCT symbol FROM futures_klines")}

    def job(ev):
        s = futures_data._session()
        sym, t0 = ev["symbol"], ev["first_trade_time"]
        try:
            if sym in known_meta and known_meta[sym][0] is None:
                return sym, None, None, [], [], None
            perp = (known_meta.get(sym) or (None,))[0]
            if perp is None:
                perp = next(
                    (c for c in futures_data.perp_candidates(ev["base_asset"])
                     if c in universe), None
                )
            if perp is None:
                return sym, None, None, [], [], None
            dates = futures_data.perp_daily_dates(s, perp)
            if not dates:
                return sym, None, None, [], [], None
            meta = (dates[0], dates[-1])
            entry1 = t0 + config.FUT_ENTRY_OFFSET_MS
            targets = {entry1, entry1 + config.FUT_HOLD_MS}
            # F2 entry day (perp launched after entry1 but within 7d):
            first_day_ms = int(datetime.strptime(dates[0], "%Y-%m-%d")
                               .replace(tzinfo=timezone.utc).timestamp() * 1000)
            if entry1 < first_day_ms <= t0 + config.FUT_F2_MAX_DELAY_MS:
                targets.add(first_day_ms + 12 * 3_600_000)  # cover launch day
                targets.add(first_day_ms + config.FUT_HOLD_MS)
            # forced-exit day for delisted perps:
            last_day_ms = int(datetime.strptime(dates[-1], "%Y-%m-%d")
                              .replace(tzinfo=timezone.utc).timestamp() * 1000)
            if last_day_ms < max(targets):
                targets.add(last_day_ms + 12 * 3_600_000)
            klines = []
            if perp not in have_kl:
                seen_days = set()
                for tgt in sorted(targets):
                    key = tgt // 86_400_000
                    if key in seen_days:
                        continue
                    seen_days.add(key)
                    klines.extend(futures_data.fetch_perp_1m_window(
                        s, perp, tgt, WINDOW_MS))
            funding = []
            if perp not in have_fund:
                funding = futures_data.fetch_funding(
                    s, perp, t0,
                    t0 + config.FUT_F2_MAX_DELAY_MS + config.FUT_HOLD_MS
                    + 86_400_000,
                )
            return sym, perp, meta, klines, funding, None
        except Exception as e:  # noqa: BLE001 — surfaced below
            return sym, None, None, [], [], f"{type(e).__name__}: {e}"

    errors = []
    n_kl = n_f = 0
    done = 0
    import time as _t
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(job, ev) for ev in events]
        for f in as_completed(futs):
            sym, perp, meta, klines, funding, err = f.result()
            if err:
                errors.append((sym, err))
            else:
                conn.execute(
                    "INSERT OR REPLACE INTO perp_meta VALUES (?,?,?,?,?)",
                    (sym, perp, meta[0] if meta else None,
                     meta[1] if meta else None, int(_t.time() * 1000)),
                )
                if klines:
                    n_kl += storage.insert_klines_table(
                        conn, "futures_klines", perp, "1m", klines, "archive_zip")
                if funding:
                    conn.executemany(
                        "INSERT OR IGNORE INTO funding_rates VALUES (?,?,?)",
                        [(perp, ts, r) for ts, r in funding],
                    )
                    n_f += len(funding)
            done += 1
            if done % 25 == 0:
                conn.commit()
                progress(f"[futures] {done}/{len(events)} events, "
                         f"{n_kl} kline rows, {n_f} funding rows")
    conn.commit()
    storage.log_step(conn, "futures_ingest",
                     f"events={len(events)} klines={n_kl} funding={n_f} "
                     f"errors={len(errors)}")
    if errors:
        progress(f"[futures] {len(errors)} errors (first 5): {errors[:5]}")
    progress(f"[futures] done: {n_kl} kline rows, {n_f} funding rows")
    return errors


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _load_perp_bars(conn, perp):
    rows = conn.execute(
        "SELECT open_time, open, high, low, close, quote_volume, close_time "
        "FROM futures_klines WHERE symbol=? AND interval='1m' ORDER BY open_time",
        (perp,),
    ).fetchall()
    return np.asarray(rows, dtype=np.float64) if rows else None


def _entry_bar(arr, ts):
    """First TRADEABLE perp bar in [ts, ts+tol] — else untradeable."""
    idx = int(np.searchsorted(arr[:, 0], ts, side="left"))
    while idx < len(arr) and arr[idx, 0] - ts <= BAR_TOL_MS:
        b = tuple(arr[idx, 1:6])
        if friction.tradeable(b):
            return b, arr[idx, 0]
        idx += 1
    return None, None


def _exit_bar(arr, ts):
    """First tradeable bar at/after ts; else last tradeable bar (truncated)."""
    idx = int(np.searchsorted(arr[:, 0], ts, side="left"))
    while idx < len(arr):
        b = tuple(arr[idx, 1:6])
        if friction.tradeable(b):
            return b, arr[idx, 0], False
        idx += 1
    for i in range(len(arr) - 1, -1, -1):
        b = tuple(arr[i, 1:6])
        if friction.tradeable(b) and arr[i, 0] < ts:
            return b, arr[i, 0], True
    return None, None, None


def _spot_marks(conn, symbol):
    rows = conn.execute(
        "SELECT open_time, close FROM klines WHERE symbol=? AND interval='1h' "
        "ORDER BY open_time", (symbol,),
    ).fetchall()
    return np.asarray(rows, dtype=np.float64) if rows else None


def _funding_return(conn, perp, spot_marks, p_ref, t_in, t_out):
    """Short-side funding return on entry notional: +sum(r_i * P_i / P_ref).
    Positive funding pays shorts; negative charges them. P_i marked from the
    spot 1h path (perp tracks spot to bps — documented approximation)."""
    rows = conn.execute(
        "SELECT funding_time, rate FROM funding_rates "
        "WHERE symbol=? AND funding_time > ? AND funding_time <= ?",
        (perp, t_in, t_out),
    ).fetchall()
    if not rows:
        return 0.0, 0
    total = 0.0
    for ts, rate in rows:
        p_i = p_ref
        if spot_marks is not None:
            idx = int(np.searchsorted(spot_marks[:, 0], ts, side="right")) - 1
            if 0 <= idx < len(spot_marks):
                p_i = spot_marks[idx, 1]
        total += rate * (p_i / p_ref)
    return total, len(rows)


def run(conn, events):
    meta = {r[0]: r[1:] for r in conn.execute(
        "SELECT spot_symbol, perp_symbol, first_date, last_date FROM perp_meta")}
    per_event = []
    for ev in events:
        sym, t0 = ev["symbol"], ev["first_trade_time"]
        m = meta.get(sym)
        rec = {"event": ev, "perp": m[0] if m else None,
               "f1": np.nan, "f2": np.nan,
               "f1_funding": np.nan, "f2_funding": np.nan,
               "f1_n_fund": 0, "truncated": False}
        per_event.append(rec)
        if not m or m[0] is None:
            continue
        perp = m[0]
        arr = _load_perp_bars(conn, perp)
        if arr is None:
            continue
        marks = _spot_marks(conn, sym)
        entry1_ts = t0 + config.FUT_ENTRY_OFFSET_MS

        def leg(entry_ts):
            e_bar, e_ot = _entry_bar(arr, entry_ts)
            if e_bar is None:
                return np.nan, np.nan, 0, False
            x_bar, x_ot, trunc = _exit_bar(arr, entry_ts + config.FUT_HOLD_MS)
            if x_bar is None or x_ot <= e_ot:
                return np.nan, np.nan, 0, False
            fill = friction.net_short_return(
                e_bar, "1m", x_bar, "1m", fee=config.FUT_TAKER_FEE)
            fr, nf = _funding_return(
                conn, perp, marks, e_bar[0], e_ot, x_ot)
            return fill + fr, fr, nf, bool(trunc)

        rec["f1"], rec["f1_funding"], rec["f1_n_fund"], rec["truncated"] = \
            leg(entry1_ts)
        # F2: first perp bar if it arrives after entry1 but within 7d.
        first_ot = arr[0, 0]
        if first_ot <= entry1_ts:
            rec["f2"], rec["f2_funding"] = rec["f1"], rec["f1_funding"]
        elif first_ot <= t0 + config.FUT_F2_MAX_DELAY_MS:
            rec["f2"], rec["f2_funding"], _, _ = leg(first_ot)
    return per_event


def _battery(returns):
    r = np.asarray(returns, dtype=np.float64)
    r = r[~np.isnan(r)]
    n = len(r)
    if n < 2:
        return {"n": n}
    n_, mean, sd, skew, kurt = stats.moments(r)
    sr = stats.sharpe(r)
    psr = stats.psr(sr, 0.0, n, skew, kurt)
    mtrl = stats.min_track_record_length(sr, 0.0, skew, kurt,
                                         config.MINTRL_CONFIDENCE)
    k = 4
    edges = np.linspace(0, n, k + 1, dtype=int)
    folds = [float(np.mean(r[edges[i]:edges[i+1]])) for i in range(k)
             if edges[i+1] > edges[i]]
    return {"n": n, "mean": mean, "median": float(np.median(r)),
            "win": float((r > 0).mean()), "sr": sr, "skew": skew,
            "kurt": kurt, "psr": psr, "mtrl": mtrl, "fold_means": folds}


def verdict(per_event, spot_mat):
    """D3 verdict battery for F1 (primary) + F2 exhibit + coverage bias."""
    f1 = _battery([r["f1"] for r in per_event])
    f2 = _battery([r["f2"] for r in per_event])
    fund1 = np.asarray([r["f1_funding"] for r in per_event], dtype=np.float64)
    fund1 = fund1[~np.isnan(fund1)]

    # Coverage bias: SPOT fade returns of perp-covered vs uncovered events.
    j = spot_mat["config_names"].index("+1h|14d")
    by_sym = {ev["symbol"]: spot_mat["net_short"][i, j]
              for i, ev in enumerate(spot_mat["events"])}
    cov, uncov = [], []
    for r in per_event:
        v = by_sym.get(r["event"]["symbol"], np.nan)
        if np.isnan(v):
            continue
        (cov if not np.isnan(r["f1"]) else uncov).append(v)

    # Pessimistic sensitivity (disclosed, not governing): re-deflate F1
    # against the full 30-trial ledger even though the cell was fixed by the
    # spot phase before any futures data was read. Over-harsh — it charges
    # the discovery-phase multiplicity tax twice on the same sample — but a
    # maximally suspicious reader deserves the number.
    dsr_pessimistic = float("nan")
    worst5 = []
    if f1.get("n", 0) >= 2:
        hurdle = stats.expected_max_sharpe_null_mc([f1["n"]] * 30)
        dsr_pessimistic = stats.psr(f1["sr"], hurdle, f1["n"],
                                    f1["skew"], f1["kurt"])
        r = np.asarray([x["f1"] for x in per_event], dtype=np.float64)
        worst5 = [float(x) for x in np.sort(r[~np.isnan(r)])[:5]]

    if f1["n"] < config.N_MIN_EVENTS:
        v, why = "INCONCLUSIVE", f"only {f1['n']} tradeable events (< {config.N_MIN_EVENTS})"
    elif f1.get("mean", 0) <= 0:
        v, why = "GHOST", "expectancy not positive net of funding+friction"
    elif f1.get("psr", 0) < config.MINTRL_CONFIDENCE:
        v, why = "GHOST", f"PSR {f1.get('psr', float('nan')):.3f} < 0.95"
    elif not (f1["n"] >= f1.get("mtrl", math.inf)):
        v, why = "INCONCLUSIVE", "history shorter than MinTRL"
    else:
        v, why = "REAL EDGE", "positive net-of-funding expectancy, PSR and MinTRL cleared"
    return {
        "f1": f1, "f2": f2,
        "dsr_pessimistic": dsr_pessimistic, "worst5": worst5,
        "funding_mean": float(fund1.mean()) if len(fund1) else float("nan"),
        "funding_negative_frac": float((fund1 < 0).mean()) if len(fund1) else float("nan"),
        "coverage": {
            "covered_n": len(cov),
            "covered_spot_mean": float(np.mean(cov)) if cov else float("nan"),
            "uncovered_n": len(uncov),
            "uncovered_spot_mean": float(np.mean(uncov)) if uncov else float("nan"),
        },
        "verdict": v, "why": why,
    }
