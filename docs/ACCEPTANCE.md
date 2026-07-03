# ACCEPTANCE.md — Acceptance criteria and the honesty lock

The purpose of this file is to make it hard for the engine (or the person reading
its output) to fool themselves. In strategy validation, optimism is the enemy. A
false "REAL EDGE" is the only expensive failure — it authorizes real money on a
fake signal. Every rule below biases toward honesty over hope.

## Default posture
- Default verdict is GHOST / INCONCLUSIVE. A hypothesis is guilty until proven
  innocent. REAL EDGE must be earned by clearing every gauntlet stage.

## Hard blocks (any one → cannot output REAL)
1. Fewer than N_min qualifying events (default 30) → INCONCLUSIVE.
2. Test-fold performance not consistent with train-fold (walk-forward) → GHOST.
3. Deflated Sharpe not significantly > 0 → GHOST.
4. PBO above threshold (default 0.5) → GHOST.
5. Edge does not survive modeled fees + spread + slippage → GHOST.
6. Available history shorter than minimum-track-record-length for the observed
   Sharpe → INCONCLUSIVE.
7. Any lookahead leak detected in the feature pipeline → the result is VOID; fix
   the leak and re-run. A leaked result is worse than no result.
8. H-A specifically: no valid control group of non-listed tokens → INCONCLUSIVE
   (a run-up means nothing without a baseline of tokens that did NOT get listed).

## What the engine must print alongside every verdict
- The number of events/trades the verdict is based on.
- The key statistic at each stage (so a human can sanity-check, not just trust).
- An explicit "confidence" note: is this a strong result or a marginal one?
- For any REAL EDGE: a plain-English description of exactly what the rule is and
  what would have to be true live for it to keep working (i.e., why hasn't it
  been arbitraged away — is it uncrowded, capacity-limited, etc.).

## Anti-optimism rules for the implementing agent
- Do not tune parameters until a GHOST becomes REAL. If your first honest test
  says GHOST, that is the answer. Re-tuning to force REAL is the exact overfitting
  this project exists to prevent.
- Do not drop "inconvenient" losing events. Every qualifying event stays in.
- Do not report only the best horizon/entry. Report the full grid; a single
  cherry-picked winning cell among many losers is noise.
- If results are ambiguous, say so. "Marginal / needs more data" is a valid,
  honest output and is more valuable than a confident wrong answer.

## Definition of project success
Success is NOT "found a profitable strategy." Success is "produced a TRUSTWORTHY
verdict for each hypothesis." A confident, correct GHOST is a full success — it
saved real capital. The only failure is a verdict that can't be trusted.
