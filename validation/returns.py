"""Forward-return matrices for H-B (no lookahead by construction).

For a decision at time T the fill bar is the FIRST bar whose open_time >= T.
No feature or fill ever reads a bar that closes before the decision would be
made; conditioning information (e.g. the reversion drawdown trigger) uses
only bars with close_time <= T.

Delisting handling (survivorship rule 3): if a token stops trading before an
exit horizon, the position is force-exited on the LAST available bar and the
cell is marked truncated — the event is never dropped.
"""
import numpy as np

from . import config, friction

BAR_TOL_MS = {"1m": 30 * 60_000, "1h": 6 * 3_600_000}


def _load_klines(conn, symbol, interval):
    rows = conn.execute(
        "SELECT open_time, open, high, low, close, quote_volume, close_time "
        "FROM klines WHERE symbol=? AND interval=? ORDER BY open_time",
        (symbol, interval),
    ).fetchall()
    if not rows:
        return None
    return np.asarray(rows, dtype=np.float64)


def _bar_at_or_after(arr, ts, tol_ms=None):
    """First bar with open_time >= ts (within tolerance, if given)."""
    idx = np.searchsorted(arr[:, 0], ts, side="left")
    if idx >= len(arr):
        return None
    if tol_ms is not None and arr[idx, 0] - ts > tol_ms:
        return None
    return arr[idx]


def _fill_bar(m1, h1, ts, t0):
    """Pick the fill bar for a decision at ts: 1m while covered, else 1h.
    Returns (bar_tuple, res) or (None, None)."""
    if m1 is not None and ts <= t0 + (config.KLINE_1M_HOURS - 1) * 3_600_000:
        row = _bar_at_or_after(m1, ts, BAR_TOL_MS["1m"])
        if row is not None:
            return (row[1], row[2], row[3], row[4], row[5]), "1m"
    if h1 is not None:
        row = _bar_at_or_after(h1, ts, BAR_TOL_MS["1h"])
        if row is not None:
            return (row[1], row[2], row[3], row[4], row[5]), "1h"
    return None, None


def _exit_bar(m1, h1, t_exit, t0):
    """Exit fill: first TRADEABLE bar at/after t_exit at the natural
    resolution; on a data gap or dust-volume stretch (halt, thin trading)
    walk forward to the first later tradeable bar of any resolution rather
    than pretending the symbol died or covering in nonexistent liquidity.
    Only if no tradeable bar exists at/after t_exit is the position
    force-closed on the LAST tradeable bar before it (genuine end of
    trading). Returns (bar, res, truncated) or (None, None, None)."""
    bar, res = _fill_bar(m1, h1, t_exit, t0)
    if bar is not None and friction.tradeable(bar):
        return bar, res, False
    best = None
    for arr, r in ((m1, "1m"), (h1, "1h")):
        if arr is None:
            continue
        idx = int(np.searchsorted(arr[:, 0], t_exit, side="left"))
        while idx < len(arr):
            row = arr[idx]
            b = (row[1], row[2], row[3], row[4], row[5])
            if friction.tradeable(b):
                if best is None or row[0] < best[0]:
                    best = (row[0], b, r)
                break
            idx += 1
    if best is not None:
        return best[1], best[2], False
    # nothing tradeable at/after t_exit: symbol stopped trading -> forced
    # exit on the last tradeable bar we have.
    lasts = []
    for arr, r in ((m1, "1m"), (h1, "1h")):
        if arr is None:
            continue
        for i in range(len(arr) - 1, -1, -1):
            row = arr[i]
            b = (row[1], row[2], row[3], row[4], row[5])
            if friction.tradeable(b):
                lasts.append((row[0], b, r))
                break
    if not lasts:
        return None, None, None
    _, b, r = max(lasts, key=lambda c: c[0])
    return b, r, True


def grid_config_names():
    return [
        f"{e}|{h}"
        for e in config.ENTRY_OFFSETS_MS
        for h in config.HORIZONS_MS
    ]


def build_matrices(conn, events):
    """Returns dict with events kept (chronological), config names, and
    matrices net_short, gross_long, truncated (N x C, NaN = no fill)."""
    names = grid_config_names()
    C = len(names)
    kept, net_short, gross_long, truncated = [], [], [], []
    no_data = []

    for ev in events:
        sym, t0 = ev["symbol"], ev["first_trade_time"]
        m1 = _load_klines(conn, sym, "1m")
        h1 = _load_klines(conn, sym, "1h")
        if m1 is None and h1 is None:
            no_data.append(sym)
            continue
        row_ns = np.full(C, np.nan)
        row_gl = np.full(C, np.nan)
        row_tr = np.zeros(C, dtype=bool)
        j = 0
        for _, e_off in config.ENTRY_OFFSETS_MS.items():
            t_entry = t0 + e_off
            e_bar, e_res = _fill_bar(m1, h1, t_entry, t0)
            for _, hz in config.HORIZONS_MS.items():
                if e_bar is None or not friction.tradeable(e_bar):
                    j += 1
                    continue
                t_exit = t_entry + hz
                x_bar, x_res, trunc = _exit_bar(m1, h1, t_exit, t0)
                if x_bar is None:
                    j += 1
                    continue
                row_tr[j] = bool(trunc)
                row_ns[j] = friction.net_short_return(e_bar, e_res, x_bar, x_res)
                row_gl[j] = friction.gross_long_return(e_bar, x_bar)
                j += 1
        kept.append(ev)
        net_short.append(row_ns)
        gross_long.append(row_gl)
        truncated.append(row_tr)

    return {
        "events": kept,
        "config_names": names,
        "net_short": np.asarray(net_short) if kept else np.empty((0, C)),
        "gross_long": np.asarray(gross_long) if kept else np.empty((0, C)),
        "truncated": np.asarray(truncated) if kept else np.empty((0, C), bool),
        "no_data_symbols": no_data,
    }


def build_reversion_exhibit(conn, events):
    """Pre-declared long-side mean-reversion configs (HYPOTHESIS.md H-B spot
    form). Trigger uses ONLY bars with close_time <= entry time. Reported as
    an exhibit; its configs count toward the DSR trials total."""
    names = [c[0] for c in config.REVERSION_CONFIGS]
    rows, kept = [], []
    for ev in events:
        sym, t0 = ev["symbol"], ev["first_trade_time"]
        m1 = _load_klines(conn, sym, "1m")
        h1 = _load_klines(conn, sym, "1h")
        if m1 is None and h1 is None:
            continue
        out = np.full(len(names), np.nan)
        for i, (_, e_off, dd_min, hold) in enumerate(config.REVERSION_CONFIGS):
            t_entry = t0 + e_off
            # Trigger inputs come ONLY from bars fully closed by t_entry:
            # running high, and last close as the "current" price. The fill
            # then happens on the bar starting at t_entry (order executes
            # during that bar) — fills may use that bar, signals may not.
            run_high, last_close, last_ct = 0.0, None, -1
            for arr in (m1, h1):
                if arr is None:
                    continue
                closed = arr[arr[:, 6] <= t_entry]
                if len(closed):
                    run_high = max(run_high, closed[:, 2].max())
                    if closed[-1, 6] > last_ct:
                        last_ct = closed[-1, 6]
                        last_close = closed[-1, 4]
            if last_close is None or run_high <= 0:
                continue
            e_bar, e_res = _fill_bar(m1, h1, t_entry, t0)
            if e_bar is None or not friction.tradeable(e_bar):
                continue
            drawdown = 1.0 - last_close / run_high
            if drawdown < dd_min:
                continue  # trigger not met -> no trade (NaN, not zero)
            x_bar, x_res, _trunc = _exit_bar(m1, h1, t_entry + hold, t0)
            if x_bar is None:
                continue
            out[i] = friction.net_long_return(e_bar, e_res, x_bar, x_res)
        kept.append(ev)
        rows.append(out)
    return {
        "events": kept,
        "config_names": names,
        "net_long": np.asarray(rows) if rows else np.empty((0, len(names))),
    }


def descriptive_stats(conn, events):
    """Base-rate facts to challenge/confirm the research claims behind H-B
    (46% peak at listing, ~98% eventually dump). Descriptive only."""
    n = 0
    below_entry_14d = 0
    peak_in_first_24h = 0
    max_dd = []
    for ev in events:
        h1 = _load_klines(conn, ev["symbol"], "1h")
        if h1 is None or len(h1) < 2:
            continue
        t0 = ev["first_trade_time"]
        ref = h1[0, 1]  # first 1h bar open
        if ref <= 0:
            continue
        n += 1
        closes = h1[:, 4]
        highs = h1[:, 2]
        if closes[-1] < ref:
            below_entry_14d += 1
        t_peak = h1[np.argmax(highs), 0]
        if t_peak <= t0 + 24 * 3_600_000:
            peak_in_first_24h += 1
        run_max = np.maximum.accumulate(highs)
        dd = 1.0 - (h1[:, 3] / run_max)  # low vs running high
        max_dd.append(float(dd.max()))
    return {
        "n": n,
        "frac_below_entry_at_14d": below_entry_14d / n if n else float("nan"),
        "frac_peak_within_24h": peak_in_first_24h / n if n else float("nan"),
        "median_max_drawdown_14d": float(np.median(max_dd)) if max_dd else float("nan"),
    }
