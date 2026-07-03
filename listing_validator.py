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

from validation import announcements, config, events, gauntlet, report, storage


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


def cmd_validate(conn):
    evs = events.load_events(conn)
    if not evs:
        print("no events ingested — run `python listing_validator.py ingest` first",
              file=sys.stderr)
        return 1

    print(f"[validate] H-B (fade the listing) on {len(evs)} events "
          f"(tested FIRST, in isolation — HYPOTHESIS.md order)")
    hb = gauntlet.run_hb(conn, evs)

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
    out = report.full_report(conn, results)
    print()
    print(out)
    with open("data/verdict_report.txt", "w") as fh:
        fh.write(out + "\n")
    print("[validate] report saved to data/verdict_report.txt")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("command", choices=["ingest", "validate", "all"])
    ap.add_argument("--db", default=config.DB_PATH)
    args = ap.parse_args()
    conn = storage.connect(args.db)
    try:
        if args.command in ("ingest", "all"):
            cmd_ingest(conn)
        if args.command in ("validate", "all"):
            return cmd_validate(conn) or 0
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
