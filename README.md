# listing_validator — Binance listing-event strategy VALIDATION engine

Answers one question per hypothesis about Binance spot listings:
**REAL EDGE, GHOST, or INCONCLUSIVE.**

This is **not** a trading bot. It never places orders, never holds keys with
trade permissions, never touches capital. It reads public market data and
prints a verdict table. The scope fence is in `docs/PROJECT_BRIEF.md`; the
hypotheses in `docs/HYPOTHESIS.md`; the data rules in `docs/DATA_PIPELINE.md`;
the test battery in `docs/VALIDATION_GAUNTLET.md`; the honesty rules in
`docs/ACCEPTANCE.md`.

## Run

```bash
pip install -r requirements.txt
python listing_validator.py ingest     # ~10 min: announcements + events + klines -> data/validation.db
python listing_validator.py validate   # gauntlet + verdict table (also saved to data/verdict_report.txt)
python -m pytest tests/                # stats-machinery + anti-leakage tests
```

## Data sources (all public, read-only, keyless)

| What | Source |
|---|---|
| Listing announcements | Binance CMS catalog 48 ("New Cryptocurrency Listing") |
| Klines (incl. delisted symbols) | `data-api.binance.vision` `/api/v3/klines` |
| Symbol universe incl. dead tokens | `data.binance.vision` S3 archive listing |

`api.binance.com` itself is geo-blocked in some environments (HTTP 451);
`data-api.binance.vision` is Binance's official mirror of the same endpoints
and — verified empirically — serves full kline history for delisted symbols,
which is what makes the event set survivorship-complete.

## Architecture

```
listing_validator.py        CLI: ingest | validate | all
validation/
  config.py                 every verdict-affecting threshold, in one place
  storage.py                SQLite, raw tables append-only (auditable)
  announcements.py          CMS scrape + title classification
  market_data.py            REST mirror + S3 archive fallback
  events.py                 survivorship-complete event calendar + kline ingest
  returns.py                forward-return grids, no-lookahead by construction
  friction.py               fee/spread/slippage model (adverse fills only)
  stats.py                  PSR, Deflated Sharpe, MinTRL, PBO via CSCV
  gauntlet.py               stages 1-6, hard-block verdict logic
  report.py                 verdict table + full grid + plain-English readout
tests/                      unit tests incl. lookahead-leak guards
```

## Honesty properties (enforced in code, tested)

- **Survivorship**: universe is the union of live `exchangeInfo` and the S3
  archive (which retains delisted symbols forever); dead tokens stay in the
  sample with forced exits at their last bar. Every excluded candidate is
  stored with an `exclude_reason` — nothing is silently dropped.
- **No lookahead**: fills use the first bar at/after decision time; signals
  (e.g. the reversion trigger) may only read bars fully closed before the
  decision. `tests/test_no_lookahead.py` proves both by diverging synthetic
  price paths after the decision instant.
- **Friction everywhere**: every gauntlet stage runs on net returns (taker
  fee both sides + half-spread scaled to the actual fill-bar range — brutal
  on listing-minute bars, as it should be). Stage 5 additionally reports the
  gross-vs-net gap.
- **Deflation**: the Deflated Sharpe hurdle counts every variant tried (the
  full grid + the pre-declared reversion exhibit), and per-event Sharpe is
  never annualized.
- **Defaults**: verdict logic can only emit REAL EDGE if all six stages pass;
  every ambiguity resolves to GHOST or INCONCLUSIVE.
- **Frozen rulings**: methodological forks encountered during the build
  (Stage 3 variance estimator, event-definition rules, purging) are recorded
  in `docs/DECISIONS.md` and frozen before any further hypothesis runs.
  Stage 3 prints BOTH the governing null-calibrated hurdle and the paper
  plug-in variant, plus a dependence-free Bonferroni cross-check.

## Current output

`reports/verdict_report.txt` holds the latest full run (429 events,
2019-2026): H-B = REAL EDGE (marginal) as a fade *signal*; spot-actionable
form is "do not buy listings". The pre-declared futures execution leg
(docs/DECISIONS.md D3; `ingest-futures`) tests the short side where it can
actually exist: on the 127 listings with a day-one perp, shorting at
spot+1h for 14d nets +13.2%/event AFTER futures fees, adverse fills and
funding (funding costs shorts ~5%/event on average) — REAL EDGE with
marginal confidence and a violent squeeze tail (worst event -236% of
stake). H-C and H-A are NOT TESTED yet, by design.
