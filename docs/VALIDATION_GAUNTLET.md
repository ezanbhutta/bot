# VALIDATION_GAUNTLET.md — The actual product

This is the whole point of the project. Any amateur can compute "did this
strategy make money on past data." The gauntlet answers the only question that
matters: "will this edge survive on data it has never seen, after real-world
friction?" Most strategies that pass a naive backtest fail here. That is by
design — the gauntlet exists to kill ghosts cheaply.

Every hypothesis passes through ALL stages, in order. A failure at any stage =
GHOST (or INCONCLUSIVE if the failure is insufficient data). Only a strategy that
clears every stage earns the verdict REAL EDGE.

---

## Stage 1 — In-sample backtest (the sanity gate)
Run the strategy on the training portion of history. Compute: total return, win
rate, average win/loss, max drawdown, number of trades, Sharpe.
- Reject if too few trades (set N_min, e.g. < 30 events) → INCONCLUSIVE (not
  enough sample to say anything).
- This stage only proves the idea isn't dead on arrival. Passing it means
  nothing on its own — the next stages are where truth lives.

## Stage 2 — Walk-forward validation (does it generalize forward in time?)
Split history chronologically into rolling train/test folds. Optimize (if any
params) on each train fold, evaluate on the immediately following unseen test
fold. Never optimize on the test fold.
- Report performance on TEST folds only.
- Reject if test-fold performance collapses vs train-fold performance → that gap
  IS overfitting → GHOST.

## Stage 3 — Deflated Sharpe Ratio (is the return real or luck?)
Adjust the Sharpe ratio for (a) the number of strategy variants/parameters
tried, and (b) non-normal returns (skew/kurtosis — crypto is fat-tailed).
- Reference: Bailey & López de Prado, "The Deflated Sharpe Ratio."
- The more variants you tested, the higher the bar a Sharpe must clear to be
  real. A raw Sharpe of 2 across 500 tried variants is probably noise.
- Reject if the deflated Sharpe is not significantly > 0 → GHOST.

## Stage 4 — Probability of Backtest Overfitting (PBO)
Compute PBO via combinatorially-symmetric cross-validation (CSCV): across many
train/test splits, how often does the best in-sample config underperform the
median out-of-sample?
- Reference: Bailey, Borwein, López de Prado, Zhu — "The Probability of Backtest
  Overfitting."
- High PBO (e.g. > 0.5) means the selection process is fitting noise → GHOST.

## Stage 5 — Realistic friction simulation (paper trade with costs)
Re-run the surviving strategy applying real Binance SPOT costs:
- Taker fee (use current public Binance spot taker fee; make it a config
  constant so it's easy to update).
- Slippage model: for thin, newly-listed tokens slippage is LARGE and asymmetric.
  Do NOT assume mid-price fills. Model slippage from the actual 1m
  high/low/volume around the intended entry. A listing-fade edge can be entirely
  eaten by slippage on illiquid tokens — this stage catches that.
- Include the spread. Newly listed tokens have wide spreads.
- Reject if the edge does not survive friction → GHOST (this is the most common
  killer for listing strategies specifically).

## Stage 6 — Statistical sufficiency gate (the honesty lock)
Before emitting ANY verdict, confirm the sample was large enough for the result
to be statistically meaningful. Compute (or approximate) a minimum-track-record
length for the observed Sharpe. If the available history is shorter than what
significance requires → verdict is INCONCLUSIVE, never REAL.
- Reference: López de Prado, "Minimum Track Record Length."
- This is the single most important stage. A false REAL authorizes real capital
  on a fake edge. When in doubt, INCONCLUSIVE.

---

## Verdict rules
- REAL EDGE: cleared Stages 1–6, positive expectancy after friction, deflated
  Sharpe significantly > 0, PBO acceptably low, sample sufficient.
- GHOST: failed any of Stages 2–5 on the merits (edge doesn't generalize / is
  luck / dies on costs).
- INCONCLUSIVE: insufficient sample at Stage 1 or Stage 6. Not a failure of the
  idea — a failure of evidence. Treat as "do not deploy," same as GHOST, but
  labeled honestly so it can be revisited with more data.

## Output
A verdict table: one row per hypothesis (H-B, H-C, H-A), each column a stage,
final verdict in the last column, with the key statistic at each stage. Then a
one-paragraph plain-English readout per hypothesis: what it means and what to do.
