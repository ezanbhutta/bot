"""Forward paper-test tracker — docs/DECISIONS.md D4.

Records what the validated rule (F1: short the perp at spot_t0+1h if a
day-one perp exists, hold 336h) WOULD have done on every listing after the
frozen forward-start date. Pure accounting on public read-only data: no
orders, no keys, no capital. The book is reconstructable from scratch on a
fresh clone — every state transition is derived from the archives, so the
tracker can run on any cadence and simply advances whatever became
knowable since the last run.

Archive lag is respected honestly: perp 1m day-zips appear ~T+1, monthly
funding files land after month-end. A position's price legs close first
(CLOSED_PRICE) and it only SETTLES when funding is complete.
"""
import json
import os
import time
from datetime import datetime, timezone

import numpy as np

from . import config, events, friction, futures_data, futures_exec, market_data, stats, storage

# Frozen baseline from the validated backtest run (2026-07-03, n=127):
# used ONLY to ask "is forward performance consistent with the backtest?".
F1_BASELINE = {"mean": 0.1323, "sd": 0.447, "n": 127, "win": 0.76}

H_MS = 3_600_000
DAY_MS = 86_400_000


def _now_ms():
    return int(time.time() * 1000)


def _fwd_start_ms():
    return int(datetime.fromisoformat(
        config.FORWARD_TEST_START_UTC.replace("Z", "+00:00")).timestamp() * 1000)


def _fmt(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M")


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def _discover(conn, sess, progress):
    """New forward listings -> paper_trades rows (status DETECTED)."""
    fwd_start = _fwd_start_ms()
    known = {s for (s,) in conn.execute("SELECT symbol FROM paper_trades")}
    ei = market_data.get_exchange_info(sess)

    # Seed 1: events the backtest excluded only for recency.
    cands = {
        sym: base for sym, base in conn.execute(
            "SELECT symbol, base_asset FROM events "
            "WHERE exclude_reason='forward_window_incomplete'")
        if sym not in known
    }
    # Seed 2: brand-new USDT pairs not seen by the last calendar build.
    seen_events = {s for (s,) in conn.execute("SELECT symbol FROM events")}
    all_bases = set()
    for sym, (b, q, _st) in ei.items():
        all_bases.add(b)
    for sym, (b, q, _st) in ei.items():
        if (q != config.PRIMARY_QUOTE or sym in seen_events or sym in known
                or sym in cands):
            continue
        if (b in config.STABLE_OR_PEGGED_BASES
                or b in config.TOKENIZED_EQUITY_BASES
                or events._is_leveraged(b, all_bases)):
            continue
        cands[sym] = b

    added = 0
    now = _now_ms()
    for sym, base in sorted(cands.items()):
        try:
            ft = market_data.first_kline_ms(sess, sym)
        except Exception:
            continue
        if ft is None or ft < fwd_start:
            continue
        # Quote-addition guard: did the base trade anywhere on Binance
        # earlier than 12h before this USDT pair opened?
        earlier = [
            ms for (ms,) in conn.execute(
                "SELECT first_kline_ms FROM symbols WHERE base_asset=? "
                "AND quote_asset != ? AND first_kline_ms IS NOT NULL",
                (base, config.PRIMARY_QUOTE))
        ]
        if earlier and min(earlier) < ft - config.QUOTE_ADDITION_TOLERANCE_HOURS * H_MS:
            continue
        conn.execute(
            "INSERT OR IGNORE INTO paper_trades (symbol, base_asset, "
            "first_trade_time, status, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?)",
            (sym, base, ft, "DETECTED", now, now),
        )
        added += 1
    conn.commit()
    if added:
        progress(f"[track] {added} new forward listing(s) detected")
    return added


# ---------------------------------------------------------------------------
# State advancement
# ---------------------------------------------------------------------------

def _ensure_spot_klines(conn, sess, sym, t0):
    now = _now_ms()
    for interval, hi in (
        ("1m", t0 + int(config.KLINE_1M_HOURS * H_MS)),
        ("1h", t0 + int(config.KLINE_1H_DAYS * DAY_MS)),
    ):
        hi = min(hi, now)
        got = conn.execute(
            "SELECT MAX(open_time) FROM klines WHERE symbol=? AND interval=?",
            (sym, interval)).fetchone()[0]
        lo = t0 if got is None else got + 1
        if hi - lo < 2 * market_data._interval_ms(interval):
            continue
        rows, source = market_data.fetch_klines(sess, sym, interval, lo, hi)
        if rows:
            storage.insert_klines(conn, sym, interval, rows, source)
    conn.commit()


def _ensure_perp(conn, sess, sym, base, t0):
    row = conn.execute(
        "SELECT perp_symbol FROM perp_meta WHERE spot_symbol=?", (sym,)
    ).fetchone()
    if row is not None:
        return row[0]
    universe = futures_data.get_perp_universe(sess)
    perp = next((c for c in futures_data.perp_candidates(base)
                 if c in universe), None)
    if perp is None and _now_ms() < t0 + 2 * DAY_MS:
        return None  # archive may not show a brand-new perp yet; retry later
    conn.execute(
        "INSERT OR REPLACE INTO perp_meta VALUES (?,?,?,?,?)",
        (sym, perp, None, None, _now_ms()))
    conn.commit()
    return perp


def _ensure_perp_bars(conn, sess, perp, center_ms):
    have = conn.execute(
        "SELECT COUNT(*) FROM futures_klines WHERE symbol=? "
        "AND open_time BETWEEN ? AND ?",
        (perp, center_ms - futures_exec.WINDOW_MS,
         center_ms + futures_exec.WINDOW_MS)).fetchone()[0]
    if have:
        return
    rows = futures_data.fetch_perp_1m_window(
        sess, perp, center_ms, futures_exec.WINDOW_MS)
    if rows:
        storage.insert_klines_table(
            conn, "futures_klines", perp, "1m", rows, "archive_zip")
        conn.commit()


def _spot_fade_return(conn, sym, t0):
    """Spot +1h|14d net short (the validated cell) for signal monitoring."""
    from . import returns as R
    m1, h1 = R._load_klines(conn, sym, "1m"), R._load_klines(conn, sym, "1h")
    if m1 is None and h1 is None:
        return None
    t_entry = t0 + H_MS
    e_bar, e_res = R._fill_bar(m1, h1, t_entry, t0)
    if e_bar is None or not friction.tradeable(e_bar):
        return None
    x_bar, x_res, _tr = R._exit_bar(m1, h1, t_entry + 336 * H_MS, t0)
    if x_bar is None:
        return None
    return friction.net_short_return(e_bar, e_res, x_bar, x_res)


def _advance(conn, sess, row, progress):
    (sym, base, t0, perp, status, entry_time, exit_time, fill_ret,
     fund_ret, fund_done, total, spot_ret) = row
    now = _now_ms()
    entry_ts = t0 + config.FUT_ENTRY_OFFSET_MS
    exit_ts = entry_ts + config.FUT_HOLD_MS
    _ensure_spot_klines(conn, sess, sym, t0)

    if status == "DETECTED":
        perp = _ensure_perp(conn, sess, sym, base, t0)
        if perp is None:
            meta = conn.execute(
                "SELECT 1 FROM perp_meta WHERE spot_symbol=?", (sym,)).fetchone()
            if meta:  # confirmed: no perp exists -> F1 untradeable
                status = "UNTRADEABLE"
        else:
            # entry-day zip appears ~T+1
            if now > entry_ts + DAY_MS + 6 * H_MS:
                _ensure_perp_bars(conn, sess, perp, entry_ts)
                arr = futures_exec._load_perp_bars(conn, perp)
                if arr is None:
                    status = "UNTRADEABLE"  # perp exists but not on entry day
                else:
                    e_bar, e_ot = futures_exec._entry_bar(arr, entry_ts)
                    if e_bar is None:
                        status = "UNTRADEABLE"
                    else:
                        status, entry_time = "OPEN", int(e_ot)

    if status == "OPEN" and now > exit_ts + DAY_MS + 6 * H_MS:
        _ensure_perp_bars(conn, sess, perp, exit_ts)
        arr = futures_exec._load_perp_bars(conn, perp)
        e_bar, e_ot = futures_exec._entry_bar(arr, entry_ts)
        x_bar, x_ot, _tr = futures_exec._exit_bar(arr, exit_ts)
        if x_bar is not None and e_bar is not None and x_ot > e_ot:
            fill_ret = friction.net_short_return(
                e_bar, "1m", x_bar, "1m", fee=config.FUT_TAKER_FEE)
            exit_time, status = int(x_ot), "CLOSED_PRICE"

    if status == "CLOSED_PRICE" and not fund_done:
        funding = futures_data.fetch_funding(sess, perp, t0, exit_time + DAY_MS)
        if funding:
            conn.executemany(
                "INSERT OR IGNORE INTO funding_rates VALUES (?,?,?)",
                [(perp, ts, r) for ts, r in funding])
            conn.commit()
        marks = futures_exec._spot_marks(conn, sym)
        arr = futures_exec._load_perp_bars(conn, perp)
        e_bar, e_ot = futures_exec._entry_bar(arr, entry_ts)
        fund_ret, n_f = futures_exec._funding_return(
            conn, perp, marks, e_bar[0], entry_time, exit_time)
        latest = conn.execute(
            "SELECT MAX(funding_time) FROM funding_rates WHERE symbol=?",
            (perp,)).fetchone()[0] or 0
        if latest >= exit_time - int(8.5 * H_MS):
            fund_done = 1
            total = fill_ret + fund_ret
            status = "SETTLED"

    if spot_ret is None and now > exit_ts + DAY_MS:
        spot_ret = _spot_fade_return(conn, sym, t0)

    conn.execute(
        "UPDATE paper_trades SET perp_symbol=?, status=?, entry_time=?, "
        "exit_time=?, fill_return=?, funding_return=?, funding_complete=?, "
        "total_return=?, spot_return=?, updated_at=? WHERE symbol=?",
        (perp, status, entry_time, exit_time, fill_ret, fund_ret,
         int(fund_done or 0), total, spot_ret, now, sym))
    conn.commit()


def refresh(conn, progress=print):
    sess = market_data._session()
    _discover(conn, sess, progress)
    rows = conn.execute(
        "SELECT symbol, base_asset, first_trade_time, perp_symbol, status, "
        "entry_time, exit_time, fill_return, funding_return, "
        "funding_complete, total_return, spot_return FROM paper_trades "
        "WHERE status NOT LIKE 'EXCLUDED%' "
        "AND (status != 'SETTLED' OR spot_return IS NULL) "
        "ORDER BY first_trade_time").fetchall()
    for row in rows:
        try:
            _advance(conn, sess, row, progress)
        except Exception as e:  # noqa: BLE001
            progress(f"[track] {row[0]}: advance error {type(e).__name__}: {e}")


# ---------------------------------------------------------------------------
# State persistence (git-committed, so a fresh clone resumes the book)
# ---------------------------------------------------------------------------

STATE_PATH = "state/paper_state.json"

_PT_COLS = ("symbol", "base_asset", "first_trade_time", "perp_symbol",
            "status", "entry_time", "exit_time", "fill_return",
            "funding_return", "funding_complete", "total_return",
            "spot_return", "note", "created_at", "updated_at")


def export_state(conn, path=STATE_PATH):
    state = {
        "paper_trades": [
            dict(zip(_PT_COLS, r)) for r in conn.execute(
                f"SELECT {','.join(_PT_COLS)} FROM paper_trades")
        ],
        "symbols": conn.execute(
            "SELECT symbol, base_asset, quote_asset, first_kline_ms "
            "FROM symbols WHERE first_kline_ms IS NOT NULL").fetchall(),
        "perp_meta": conn.execute(
            "SELECT spot_symbol, perp_symbol, first_date, last_date "
            "FROM perp_meta").fetchall(),
    }
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w") as fh:
        json.dump(state, fh, indent=0, sort_keys=True)


def import_state(conn, path=STATE_PATH):
    """Seed an empty/older DB from the committed state. INSERT OR IGNORE:
    live rows in the local DB always win over the imported snapshot."""
    if not os.path.exists(path):
        return 0
    with open(path) as fh:
        state = json.load(fh)
    n = 0
    for row in state.get("paper_trades", []):
        conn.execute(
            f"INSERT OR IGNORE INTO paper_trades ({','.join(_PT_COLS)}) "
            f"VALUES ({','.join('?' * len(_PT_COLS))})",
            tuple(row.get(c) for c in _PT_COLS))
        n += 1
    now = _now_ms()
    for sym, base, quote, fk in state.get("symbols", []):
        conn.execute(
            "INSERT OR IGNORE INTO symbols (symbol, base_asset, quote_asset, "
            "status, in_exchange_info, in_archive, first_kline_ms, fetched_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (sym, base, quote, "IMPORTED", 0, 0, fk, now))
    for spot, perp, fd, ld in state.get("perp_meta", []):
        conn.execute(
            "INSERT OR IGNORE INTO perp_meta VALUES (?,?,?,?,?)",
            (spot, perp, fd, ld, now))
    conn.commit()
    return n


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def report(conn):
    rows = conn.execute(
        "SELECT symbol, first_trade_time, perp_symbol, status, fill_return, "
        "funding_return, total_return, spot_return FROM paper_trades "
        "ORDER BY first_trade_time").fetchall()
    lines = [
        "FORWARD PAPER BOOK (D4; rule F1 frozen; accounting only — forward "
        "results never change the rule)",
        f"  forward window starts {config.FORWARD_TEST_START_UTC}; "
        f"{len(rows)} forward listings so far",
        f"  {'symbol':<16}{'listed (UTC)':<18}{'perp':<16}{'status':<13}"
        f"{'price leg':>10}{'funding':>9}{'total':>9}{'spot fade':>10}",
    ]
    settled, spot_all = [], []
    for sym, ft, perp, status, fr, fu, tot, sp in rows:
        def pct(x):
            return f"{x*100:+.1f}%" if x is not None else "-"
        lines.append(
            f"  {sym:<16}{_fmt(ft):<18}{(perp or '-'):<16}{status:<13}"
            f"{pct(fr):>10}{pct(fu):>9}{pct(tot):>9}{pct(sp):>10}")
        if tot is not None:
            settled.append(tot)
        if sp is not None:
            spot_all.append(sp)
    b = F1_BASELINE
    if settled:
        s = np.asarray(settled)
        band = 1.96 * b["sd"] / np.sqrt(len(s))
        consistent = abs(s.mean() - b["mean"]) <= band
        lines += [
            "",
            f"  settled forward trades: n={len(s)}  mean={s.mean()*100:+.2f}%"
            f"  win={(s>0).mean()*100:.0f}%",
            f"  backtest baseline: mean={b['mean']*100:+.2f}% (n={b['n']}, "
            f"win {b['win']*100:.0f}%); 95% band for n={len(s)}: "
            f"{(b['mean']-band)*100:+.1f}% .. {(b['mean']+band)*100:+.1f}%",
            f"  consistency: {'CONSISTENT with backtest' if consistent else 'OUTSIDE the backtest band — investigate before trusting either number'}"
            f" (underpowered until n >= ~{int(np.ceil(b['n']/2))}; standalone "
            f"verdict requires its own MinTRL)",
        ]
    else:
        lines += ["", "  no settled forward trades yet (positions need "
                      "14d + archive lag to close and settle)"]
    if spot_all:
        s = np.asarray(spot_all)
        lines.append(
            f"  signal monitor (spot fade, ALL forward events incl. "
            f"untradeable): n={len(s)} mean={s.mean()*100:+.2f}% "
            f"win={(s>0).mean()*100:.0f}%")
    return "\n".join(lines)
