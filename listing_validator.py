#!/usr/bin/env python3
"""listing_validator — Binance listing-event strategy VALIDATION engine.

Answers one question per hypothesis: REAL EDGE, GHOST, or INCONCLUSIVE.
It is NOT a trading bot: read-only public market data in, verdict table out
(PROJECT_BRIEF.md scope fence — no order code exists anywhere in this repo).

Usage:
    python listing_validator.py ingest      # scrape announcements, build the
                                            # survivorship-complete event
                                            # calendar, pull event klines
    python listing_validator.py validate    # run the gauntlet + print verdicts
    python listing_validator.py all         # both
"""
import argparse
import sys

from validation import (announcements, config, dashboard, events, futures_exec,
                        gauntlet, paper_bot, paper_tracker, report, storage)


def cmd_ingest(conn):
    print("[ingest] step 1: announcement history (Binance CMS catalog 48)")
    stored, expected = announcements.fetch_all(conn)
    total = conn.execute("SELECT COUNT(*) FROM announcements").fetchone()[0]
    print(f"[ingest] announcements in db: {total} (catalog reports {expected})")

    print("[ingest] step 2: event calendar (survivorship-complete)")
    events.build_event_calendar(conn)

    print("[ingest] step 3: event-window klines (1m + 1h)")
    events.ingest_event_klines(conn)

    print()
    print(report.data_summary(conn))


def cmd_ingest_futures(conn):
    evs = events.load_events(conn)
    if not evs:
        print("run `ingest` first", file=sys.stderr)
        return 1
    print(f"[futures] ingesting perp klines + funding for {len(evs)} events "
          f"(docs/DECISIONS.md D3)")
    futures_exec.ingest(conn, evs)
    return 0


def cmd_track(conn):
    print("[track] forward paper-test refresh (docs/DECISIONS.md D4)")
    n = paper_tracker.import_state(conn)
    if n:
        print(f"[track] seeded {n} paper-book rows from {paper_tracker.STATE_PATH}")
    paper_tracker.refresh(conn)
    out = paper_tracker.report(conn)
    paper_tracker.export_state(conn)
    print()
    print(out)
    with open("data/paper_book.txt", "w") as fh:
        fh.write(out + "\n")
    return 0


def cmd_bot(conn, only_symbol=None):
    traces = paper_bot.run(conn, only_symbol=only_symbol)
    out = paper_bot.render(traces)
    print(out)
    with open("data/paper_bot_log.txt", "w") as fh:
        fh.write(out + "\n")
    return 0


def cmd_dashboard(conn):
    print("[dashboard] rendering from DB ...")
    path = dashboard.generate(conn)
    print(f"[dashboard] written to {path} (+ reports/dashboard.html)")
    return 0


def cmd_validate(conn):
    evs = events.load_events(conn)
    if not evs:
        print("no events ingested — run `python listing_validator.py ingest` first",
              file=sys.stderr)
        return 1

    print(f"[validate] H-B (fade the listing) on {len(evs)} events "
          f"(tested FIRST, in isolation — HYPOTHESIS.md order)")
    hb = gauntlet.run_hb(conn, evs)

    fx = None
    n_meta = conn.execute("SELECT COUNT(*) FROM perp_meta").fetchone()[0]
    if n_meta:
        print("[validate] H-B short-side futures execution (D3)")
        per_event = futures_exec.run(conn, evs)
        fx = futures_exec.verdict(per_event, hb["matrices"])

    results = [
        hb,
        report.not_tested(
            "H-C",
            "pre-listing drift needs off-Binance price history (tokens do not "
            "trade on Binance before listing); tested separately once that "
            "data layer exists",
        ),
        report.not_tested(
            "H-A",
            "requires a control group of never-listed tokens "
            "(ACCEPTANCE.md hard block 8); tested last per HYPOTHESIS.md",
        ),
    ]
    out = report.full_report(conn, results, futures_result=fx)
    print()
    print(out)
    with open("data/verdict_report.txt", "w") as fh:
        fh.write(out + "\n")
    print("[validate] report saved to data/verdict_report.txt")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("command",
                    choices=["ingest", "ingest-futures", "validate", "track",
                             "dashboard", "bot", "all"])
    ap.add_argument("--db", default=config.DB_PATH)
    ap.add_argument("--symbol", default=None,
                    help="bot: trace only this symbol (e.g. REUSDT)")
    args = ap.parse_args()
    conn = storage.connect(args.db)
    try:
        if args.command in ("ingest", "all"):
            cmd_ingest(conn)
        if args.command in ("ingest-futures", "all"):
            cmd_ingest_futures(conn)
        if args.command == "track":
            rc = cmd_track(conn)
            cmd_dashboard(conn)   # keep the dashboard in step with the book
            return rc
        if args.command == "dashboard":
            return cmd_dashboard(conn)
        if args.command == "bot":
            return cmd_bot(conn, only_symbol=args.symbol)
        if args.command in ("validate", "all"):
            return cmd_validate(conn) or 0
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
