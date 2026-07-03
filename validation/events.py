"""Event calendar construction (DATA_PIPELINE.md Step 1) and event-window
kline ingestion (Step 2).

Survivorship completeness: the candidate universe is the UNION of current
exchangeInfo symbols and the data.binance.vision archive (which retains
delisted symbols forever). Nothing is dropped silently — every excluded
candidate is written to `events` with included=0 and an exclude_reason.
"""
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from . import announcements, config, market_data, storage


def _utc_ms(iso: str) -> int:
    return int(
        datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000
    )


def _now_ms() -> int:
    return int(time.time() * 1000)


def _is_leveraged(base: str, all_bases: set) -> bool:
    for suffix in ("UP", "DOWN", "BULL", "BEAR"):
        if base.endswith(suffix) and base[: -len(suffix)] in all_bases:
            return True
    return False


def build_event_calendar(conn, progress=print):
    """Build the survivorship-complete listing-event table."""
    sess = market_data._session()

    progress("[events] fetching exchangeInfo + archive universe ...")
    ei = market_data.get_exchange_info(sess)
    archive_syms = market_data.get_archive_symbols(sess)
    archive_set = set(archive_syms)
    all_syms = sorted(set(ei) | archive_set)
    storage.log_step(
        conn, "universe",
        f"exchange_info={len(ei)} archive={len(archive_set)} union={len(all_syms)}",
    )

    # Parse (base, quote) for the whole union; collect the set of base assets
    # so the leveraged-token heuristic has something to check prefixes against.
    parsed = {}
    for sym in all_syms:
        b, q = market_data.split_symbol(sym, ei)
        if b:
            parsed[sym] = (b, q)
    all_bases = {b for b, _ in parsed.values()}

    usdt_pairs = {
        sym: bq for sym, bq in parsed.items() if bq[1] == config.PRIMARY_QUOTE
    }
    progress(f"[events] union symbols={len(all_syms)}, USDT pairs={len(usdt_pairs)}")

    # First-kline for every USDT pair ever (delisted included).
    progress("[events] resolving first-kline timestamps for all USDT pairs ...")
    known = dict(
        conn.execute(
            "SELECT symbol, first_kline_ms FROM symbols WHERE first_kline_ms IS NOT NULL"
        )
    )
    todo = [s for s in usdt_pairs if s not in known]
    fk = market_data.first_kline_many(todo, workers=12)
    fk.update(known)
    for sym, ms in fk.items():
        if sym not in usdt_pairs:
            continue
        b, q = usdt_pairs[sym]
        st = ei[sym][2] if sym in ei else "ARCHIVE_ONLY"
        storage.upsert_symbol(
            conn, sym, b, q, st, int(sym in ei), int(sym in archive_set), ms
        )
    conn.commit()

    start_ms = _utc_ms(config.EVENT_START_UTC)
    cutoff_ms = _now_ms() - config.FORWARD_MARGIN_DAYS * 86_400_000

    anns = announcements.parsed_listing_announcements(conn)
    ann_by_ticker = {}
    for a in anns:
        ann_by_ticker.setdefault(a["ticker"], []).append(a)

    # Pre-window/quote-addition check needs the base's OTHER pairs' first
    # kline, at month granularity is enough — use exact first kline anyway.
    progress("[events] checking earlier non-USDT pairs (quote-addition filter) ...")
    in_window = {
        sym: fk[sym]
        for sym in usdt_pairs
        if fk.get(sym) and start_ms <= fk[sym] <= cutoff_ms
    }
    other_pairs_by_base = {}
    for sym, (b, q) in parsed.items():
        if q != config.PRIMARY_QUOTE:
            other_pairs_by_base.setdefault(b, []).append(sym)
    check_syms = []
    for sym in in_window:
        base = usdt_pairs[sym][0]
        check_syms.extend(other_pairs_by_base.get(base, []))
    known2 = dict(
        conn.execute(
            "SELECT symbol, first_kline_ms FROM symbols WHERE first_kline_ms IS NOT NULL"
        )
    )
    todo2 = [s for s in set(check_syms) if s not in known2]
    fk2 = market_data.first_kline_many(todo2, workers=12)
    fk2.update(known2)
    for sym in set(check_syms):
        b, q = parsed[sym]
        st = ei[sym][2] if sym in ei else "ARCHIVE_ONLY"
        storage.upsert_symbol(
            conn, sym, b, q, st, int(sym in ei), int(sym in archive_set),
            fk2.get(sym),
        )
    conn.commit()

    tol_ms = config.QUOTE_ADDITION_TOLERANCE_DAYS * 86_400_000
    n_included = 0
    counts = {}

    def exclude(reason):
        counts[reason] = counts.get(reason, 0) + 1

    for sym, (base, quote) in sorted(usdt_pairs.items()):
        ft = fk.get(sym)
        ev = {
            "symbol": sym, "base_asset": base, "quote_asset": quote,
            "first_trade_time": ft or 0,
            "listing_status": "TRADING"
            if (sym in ei and ei[sym][2] == "TRADING") else "DELISTED",
            "included": 0,
        }
        if ft is None:
            ev["exclude_reason"] = "no_kline_data"
        elif base in config.STABLE_OR_PEGGED_BASES:
            ev["exclude_reason"] = "stable_or_pegged"
        elif _is_leveraged(base, all_bases):
            ev["exclude_reason"] = "leveraged_token"
        elif ft < start_ms:
            ev["exclude_reason"] = "before_event_window"
        elif ft > cutoff_ms:
            ev["exclude_reason"] = "forward_window_incomplete"
        else:
            earlier = [
                fk2.get(s)
                for s in other_pairs_by_base.get(base, [])
                if fk2.get(s)
            ]
            if earlier and min(earlier) < ft - tol_ms:
                ev["exclude_reason"] = "quote_pair_addition_not_listing"
            else:
                # Genuine new listing event. Attach announcement if any.
                cands = [
                    a for a in ann_by_ticker.get(base, [])
                    if ft - config.ANNOUNCE_MATCH_BEFORE_MS
                    <= a["time"]
                    <= ft + config.ANNOUNCE_MATCH_AFTER_MS
                ]
                if cands:
                    first = min(cands, key=lambda a: a["time"])
                    ev["announcement_time"] = first["time"]
                    ev["announcement_title"] = first["title"]
                    ev["announcement_type"] = first["type"]
                ev["included"] = 1
                n_included += 1
        if not ev["included"]:
            exclude(ev["exclude_reason"])
        storage.insert_event(conn, ev)
    conn.commit()
    storage.log_step(
        conn, "event_calendar",
        f"included={n_included} excluded={counts}",
    )
    progress(f"[events] included={n_included} excluded={counts}")
    return n_included


def load_events(conn, included_only=True):
    q = (
        "SELECT symbol, base_asset, first_trade_time, announcement_time, "
        "announcement_type, listing_status FROM events "
    )
    if included_only:
        q += "WHERE included=1 "
    q += "ORDER BY first_trade_time"
    cols = ["symbol", "base_asset", "first_trade_time", "announcement_time",
            "announcement_type", "listing_status"]
    return [dict(zip(cols, r)) for r in conn.execute(q)]


def ingest_event_klines(conn, progress=print, workers=10):
    """Pull 1m (T0..+49h) and 1h (T0..+15d) klines for every included event."""
    evs = load_events(conn)
    have = {
        (s, i): (lo, hi)
        for s, i, lo, hi in conn.execute(
            "SELECT symbol, interval, MIN(open_time), MAX(open_time) "
            "FROM klines GROUP BY symbol, interval"
        )
    }
    jobs = []
    for ev in evs:
        t0 = ev["first_trade_time"]
        want = [
            ("1m", t0, t0 + config.KLINE_1M_HOURS * 3_600_000),
            ("1h", t0, t0 + config.KLINE_1H_DAYS * 86_400_000),
        ]
        for interval, lo, hi in want:
            got = have.get((ev["symbol"], interval))
            if got and got[0] <= lo and got[1] >= hi - 2 * market_data._interval_ms(interval):
                continue  # already ingested (or symbol died — re-check is cheap to skip)
            jobs.append((ev["symbol"], interval, lo, hi))

    progress(f"[klines] {len(jobs)} fetch jobs for {len(evs)} events")

    def job(spec):
        sym, interval, lo, hi = spec
        s = market_data._session()
        try:
            rows, source = market_data.fetch_klines(s, sym, interval, lo, hi)
            return spec, rows, source, None
        except Exception as e:  # noqa: BLE001 — recorded, surfaced in summary
            return spec, [], "error", str(e)

    n_rows = 0
    errors = []
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(job, j) for j in jobs]
        for f in as_completed(futs):
            (sym, interval, lo, hi), rows, source, err = f.result()
            if err:
                errors.append((sym, interval, err))
            elif rows:
                n_rows += storage.insert_klines(conn, sym, interval, rows, source)
            done += 1
            if done % 50 == 0:
                conn.commit()
                progress(f"[klines] {done}/{len(jobs)} jobs, {n_rows} rows")
    conn.commit()
    storage.log_step(
        conn, "kline_ingest", f"jobs={len(jobs)} rows={n_rows} errors={len(errors)}"
    )
    if errors:
        progress(f"[klines] {len(errors)} fetch errors (first 5): {errors[:5]}")
    progress(f"[klines] done: {n_rows} new rows")
    return n_rows, errors
