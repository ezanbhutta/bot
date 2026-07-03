# HYPOTHESIS.md — What to test, in what order, and how to judge it

The belief "coins pump before/around listings" is NOT one testable claim. It is
three. They must be tested **one at a time, in isolation.** Do not combine them
until each has an independent verdict. Combining unproven signals is the #1
documented cause of systematic-strategy failure (overfitting): a stacked
strategy backtests beautifully and dies live because you cannot tell which part
was real and which was luck.

Test in this priority order (best base rate first):

---

## H-B — FADE THE LISTING (test FIRST) [priority]
**Claim:** Newly listed Binance spot tokens systematically decline after the
listing event. Fading or avoiding the listing euphoria has positive expectancy
after fees, versus buying it.

**Why first:** strongest base rate in the research. ~98% of Binance-listed
tokens eventually dump; ~46% hit their all-time high at listing and never
surpass it; positive momentum is short-lived (~half give back gains within ~2
weeks). This is also the *least crowded* side — retail instinct is to buy the
pump, so the fade side is under-competed.

**Spot-only interpretation:** since we cannot short on spot, H-B is tested as an
*avoidance/expectancy* signal: "what is the forward return of buying at
listing?" If forward returns after fees are systematically negative, that is a
REAL (short-side) edge — logged as such, to be executed later on futures ONLY
after separate validation. On spot, the actionable form is "do not buy listings"
plus any measurable mean-reversion entry after the initial dump.

**Test construction:**
- Event = Binance listing timestamp (from DATA_PIPELINE.md).
- Measure forward returns at multiple horizons (1h, 4h, 24h, 3d, 7d, 14d) from
  several entry points (at announcement, at first candle open, at +1h, etc.).
- Net every return of realistic fees + slippage (see gauntlet).
- Edge exists if forward-return distribution is systematically, significantly
  negative (fade edge) with acceptable variance.

---

## H-C — PRE-LISTING DRIFT WINDOW (test SECOND)
**Claim:** There is tradeable upward price drift in the accumulation window
BEFORE the public listing spike.

**Why second:** research shows frequent trading and gradual price rise beginning
~57 hours before the pump time in organized events. Real signal, but hard to
trade because it requires knowing (or predicting) the listing in advance.

**Test construction:**
- For each listing, look BACKWARD from the event: is there abnormal
  price/volume drift in the T-72h to T-0 window vs the token's own baseline?
- Critical: this is only tradeable if the drift is detectable WITHOUT
  foreknowledge of the listing. Test both: (a) does drift exist pre-event
  [descriptive], and (b) could a rule detect it in real time without lookahead
  [tradeable]. Only (b) counts as an edge.

---

## H-A — ACCUMULATION PREDICTS LISTING (test LAST)
**Claim:** Abnormal volume/price accumulation in a not-yet-listed token predicts
an imminent listing.

**Why last / lowest odds:** this is detecting insider footprints. The edge
decays the instant it is known, and informed money actively hides it. Expect
GHOST, but test to confirm rather than assume.

**Test construction:**
- Requires a universe of candidate tokens and their pre-listing behavior vs a
  control group of tokens that were NOT listed. Without a proper control group
  this test is invalid (survivorship bias). Build the control group or output
  INCONCLUSIVE.

---

## COMBINATION (only after all three have verdicts)
- Combine ONLY the hypotheses that individually returned REAL EDGE.
- Re-run the FULL gauntlet on the combined strategy. Two real edges stacked can
  still overfit as a whole. If the combination fails, ship the single best
  standalone edge instead.
- If zero hypotheses return REAL, the system's honest output is: "No validated
  edge in listing events on spot. Do not deploy capital." That is a valid,
  valuable result — it saves real money.

## Expected outcomes (state these, then let the data overrule you)
- H-B: plausibly REAL on the short/fade side; on pure spot, likely "don't buy"
  plus a possible post-dump reversion entry.
- H-C: possibly REAL descriptively, likely weak-or-GHOST as a real-time
  tradeable rule.
- H-A: likely GHOST.
Do not force these outcomes. Report what the data says.
