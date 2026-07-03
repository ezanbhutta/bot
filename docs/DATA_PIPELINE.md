# DATA_PIPELINE.md — How to get the data, honestly

The validity of every verdict depends on this layer. A leak here fakes every
result downstream. Build it carefully and defensively.

## Step 1 — Listing events (the event calendar)
Produce a table of past Binance SPOT listings: `symbol`, `announcement_time_utc`,
`first_trade_time_utc`.

Sources, in order of preference:
1. Binance announcement history (the "New Cryptocurrency Listing" category on the
   Binance support/announcement site). Scrape title + timestamp. Titles follow
   patterns like "Binance Will List <TOKEN> (<TICKER>)".
2. Cross-check against the first available kline timestamp for that symbol via
   the public Binance REST API (`/api/v3/klines`) — the first candle is a proxy
   for `first_trade_time`.
3. If announcement scraping is unreliable, fall back to first-kline-timestamp as
   the event anchor, and clearly label that announcement_time is unavailable for
   those rows (this matters: pre-announcement drift tests REQUIRE announcement
   time, so H-A/H-C on those rows become INCONCLUSIVE, not silently wrong).

Distinguish `announcement_time` from `first_trade_time`. They are different
events and different hypotheses attach to each:
- H-A / H-C (pre-listing) key off `announcement_time` (and earlier).
- H-B (post-listing fade) keys off `first_trade_time`.

## Step 2 — Price/volume data (OHLCV)
For each listed symbol, pull historical klines from the public Binance REST API:
- Endpoint: `GET /api/v3/klines` (public, no key, read-only).
- Granularity: 1m and 1h. 1m for the tight windows around the event, 1h for the
  multi-day horizons.
- Windows: from `announcement_time - 96h` (to capture pre-listing drift) through
  `first_trade_time + 14d` (to capture the full pump-and-dump arc).
- For H-A's control group: also pull data for a matched set of tokens that were
  trading on Binance but NOT newly listed in the same period.

## Step 3 — Storage
- SQLite. One table for events, one for klines (keyed by symbol + open_time).
- Append-only. Store raw. Do all transformation downstream so raw is auditable.

## Anti-leakage rules (NON-NEGOTIABLE — this is where backtests lie)
1. **No lookahead.** When testing a real-time rule, at decision time T the engine
   may ONLY use data with timestamp < T. Never let a feature peek at a candle
   that closes after the decision.
2. **Point-in-time event knowledge.** For "could a rule have detected this live"
   tests, the engine must NOT know a listing is coming. Feed data forward in
   time; the rule fires or it doesn't, using only past data.
3. **Survivorship bias.** Delisted/failed tokens must be included. If your
   listing set only contains tokens that still exist and trade, every result is
   biased upward. Pull the full historical set, including tokens that later died.
4. **Fees & slippage belong in the gauntlet, not here** — but preserve enough
   granularity (1m) that realistic slippage can be modeled downstream.
5. **Timezones:** everything UTC, everything millisecond epoch internally.

## Output of this layer
A clean, time-ordered, survivorship-complete dataset of listing events + their
surrounding price/volume, in SQLite, ready for the gauntlet. Print a summary:
how many events, date range, how many have announcement_time vs first_trade_time
only, how many failed/delisted tokens included.
