"""Anti-leakage tests (DATA_PIPELINE.md non-negotiable rule 1 and
ACCEPTANCE.md hard block 7: any lookahead leak -> results VOID).

Strategy: build two synthetic datasets that are IDENTICAL up to a decision
time T and wildly different after it. Every decision-time quantity (fills,
triggers) must be identical across the two; anything else is a leak.
"""
import numpy as np

from validation import config, friction, returns, storage

T0 = 1_600_000_000_000  # arbitrary ms epoch
MIN = 60_000
HOUR = 3_600_000


def _mk_conn():
    return storage.connect(":memory:")


def _kline(open_time, o, h, l, c, interval_ms, qvol=1e6):
    return [open_time, o, h, l, c, 1000.0, open_time + interval_ms - 1,
            qvol, 100, 500.0, qvol / 2]


def _insert_path(conn, symbol, price_fn, m1_hours=49, h1_days=15):
    m1 = []
    for i in range(m1_hours * 60):
        t = T0 + i * MIN
        p = price_fn(t)
        m1.append(_kline(t, p, p * 1.01, p * 0.99, p, MIN))
    h1 = []
    for i in range(h1_days * 24):
        t = T0 + i * HOUR
        p = price_fn(t)
        h1.append(_kline(t, p, p * 1.02, p * 0.98, p, HOUR))
    storage.insert_klines(conn, symbol, "1m", m1, "test")
    storage.insert_klines(conn, symbol, "1h", h1, "test")
    conn.commit()


def _event(symbol):
    return {"symbol": symbol, "base_asset": symbol[:-4],
            "first_trade_time": T0, "announcement_time": None,
            "announcement_type": None, "listing_status": "TRADING"}


def test_entry_fill_uses_only_bars_at_or_after_decision():
    """Two price paths identical until +24h, divergent after: every entry
    fill at <= +24h must be identical."""
    def path_a(t):
        return 1.0 + 0.1 * ((t - T0) / HOUR)

    def path_b(t):
        if t < T0 + 24 * HOUR:
            return path_a(t)
        return 100.0  # violent divergence strictly after +24h

    conn = _mk_conn()
    _insert_path(conn, "AAAUSDT", path_a)
    _insert_path(conn, "BBBUSDT", path_b)
    ma = returns.build_matrices(conn, [_event("AAAUSDT")])
    mb = returns.build_matrices(conn, [_event("BBBUSDT")])
    names = ma["config_names"]
    for j, name in enumerate(names):
        entry, horizon = name.split("|")
        e_off = config.ENTRY_OFFSETS_MS[entry]
        h_ms = config.HORIZONS_MS[horizon]
        if e_off + h_ms < 24 * HOUR:  # trade fully completes before divergence
            a, b = ma["net_short"][0, j], mb["net_short"][0, j]
            assert np.isclose(a, b, equal_nan=True), (
                f"leak: {name} differs though trade ends before divergence"
            )


def test_reversion_trigger_ignores_future_bars():
    """The drawdown trigger at +24h must not see bars after +24h."""
    def pump_dump(t):
        h = (t - T0) / HOUR
        if h < 1:
            return 2.0          # pump
        return 1.0              # dumped 50% from high -> trigger fires at +24h

    def pump_dump_future_spike(t):
        h = (t - T0) / HOUR
        if h < 1:
            return 2.0
        if h < 24:
            return 1.0
        return 500.0            # absurd future spike AFTER the decision

    conn = _mk_conn()
    _insert_path(conn, "CCCUSDT", pump_dump)
    _insert_path(conn, "DDDUSDT", pump_dump_future_spike)
    ra = returns.build_reversion_exhibit(conn, [_event("CCCUSDT")])
    rb = returns.build_reversion_exhibit(conn, [_event("DDDUSDT")])
    # config 0/1 enter at +24h: triggered (not NaN) in BOTH or NEITHER.
    for i, (name, e_off, dd, hold) in enumerate(config.REVERSION_CONFIGS):
        if e_off == 24 * HOUR:
            assert np.isnan(ra["net_long"][0, i]) == np.isnan(rb["net_long"][0, i]), (
                f"leak: future bars changed the {name} trigger decision"
            )
    # dd40 must trigger (drawdown 50% >= 40%), dd60 must not (50% < 60%):
    assert not np.isnan(ra["net_long"][0, 0])
    assert np.isnan(ra["net_long"][0, 1])


def test_bar_at_or_after_never_returns_earlier_bar():
    arr = np.array([[T0 + i * MIN, 1, 1, 1, 1, 1e6, T0 + (i + 1) * MIN - 1]
                    for i in range(100)], dtype=float)
    row = returns._bar_at_or_after(arr, T0 + 30 * MIN + 1, tol_ms=30 * MIN)
    assert row is not None and row[0] >= T0 + 30 * MIN + 1


def test_delisting_truncation_keeps_event():
    """A token that dies on day 5 must stay in the sample with a forced exit
    (survivorship rule), not disappear."""
    def flat(t):
        return 1.0

    conn = _mk_conn()
    _insert_path(conn, "EEEUSDT", flat, m1_hours=49, h1_days=5)
    m = returns.build_matrices(conn, [_event("EEEUSDT")])
    j = m["config_names"].index("open|14d")
    assert not np.isnan(m["net_short"][0, j]), "delisted event was dropped"
    assert m["truncated"][0, j], "forced exit not marked as truncated"


def test_data_gap_exits_at_next_bar_not_series_end():
    """A trading halt/data gap at the intended exit must exit on the first
    bar AFTER the gap, not jump to the end of the series (which would price
    a 1h-horizon trade at +15d)."""
    conn = _mk_conn()
    m1 = []
    for i in range(49 * 60):
        t = T0 + i * MIN
        h = (t - T0) / HOUR
        if 1.0 <= h < 3.0:
            continue  # two-hour gap covering the +1h..+3h exits
        p = 1.0 if h < 3.0 else 5.0  # price after the gap differs from before
        m1.append(_kline(t, p, p * 1.01, p * 0.99, p, MIN))
    h1 = []
    for i in range(15 * 24):
        t = T0 + i * HOUR
        h = (t - T0) / HOUR
        if 1.0 <= h < 3.0:
            continue
        p = 1.0 if h < 3.0 else 5.0
        h1.append(_kline(t, p, p * 1.02, p * 0.98, p, HOUR))
    storage.insert_klines(conn, "GGGUSDT", "1m", m1, "test")
    storage.insert_klines(conn, "GGGUSDT", "1h", h1, "test")
    conn.commit()
    m = returns.build_matrices(conn, [_event("GGGUSDT")])
    j = m["config_names"].index("open|1h")
    # exit lands in the gap -> must fill at ~+3h price (5.0), and NOT be
    # marked as a delisting truncation
    assert not m["truncated"][0, j]
    r = m["net_short"][0, j]
    assert r < -2.0, f"gap exit should buy back at ~5x, got short return {r}"


def test_untradeable_bar_produces_no_fill():
    def flat(t):
        return 1.0

    conn = _mk_conn()
    m1 = []
    for i in range(49 * 60):
        t = T0 + i * MIN
        # entry bars have ~zero volume -> untradeable
        m1.append(_kline(t, 1.0, 1.01, 0.99, 1.0, MIN, qvol=1.0))
    storage.insert_klines(conn, "FFFUSDT", "1m", m1, "test")
    conn.commit()
    m = returns.build_matrices(conn, [_event("FFFUSDT")])
    assert np.all(np.isnan(m["net_short"][0])), (
        "fills happened on bars below the minimum tradeable volume"
    )


def test_fills_adverse_to_decision_time_price():
    """Fills anchor on the bar OPEN (the only decision-time-observable
    price) and friction must always work AGAINST the trader."""
    bar = (1.0, 1.5, 0.9, 1.2, 1e6)  # violent listing-minute bar
    assert friction.sell_price(bar, "1m") < bar[0]
    assert friction.buy_price(bar, "1m") > bar[0]
    # The violent range must WIDEN the adverse shift, not shrink it.
    calm = (1.0, 1.001, 0.999, 1.0, 1e6)
    assert friction.sell_price(bar, "1m") < friction.sell_price(calm, "1m")
    assert friction.buy_price(bar, "1m") > friction.buy_price(calm, "1m")
    # Round-trip short at identical bars must LOSE money (spread+fees).
    r = friction.net_short_return(bar, "1m", bar, "1m")
    assert r < 0
