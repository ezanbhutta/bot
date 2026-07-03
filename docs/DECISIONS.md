# DECISIONS.md — Frozen methodological rulings

Rules adjudicated during the build, frozen here BEFORE any further
hypothesis (H-C, H-A, combinations) is run. Changing these after seeing a
future result would be the re-tuning ACCEPTANCE.md forbids.

## D1 — Stage 3 Deflated Sharpe: variance estimator (2026-07-03)

**Question.** Bailey & López de Prado's DSR needs V[SR] — the dispersion of
trial Sharpe estimates under the null that every trial has true SR = 0. The
paper's convenience plug-in (cross-sectional variance of the observed trial
SRs) is only a consistent estimator of that quantity when the trial family
is homogeneous. Our pre-declared H-B grid intentionally contains control
cells (shorting the first-candle open) whose TRUE SR is strongly negative;
they inflate the plug-in with true-SR dispersion the null does not have.
Observed cross-trial sd was 7.3x the null noise sd — dispersion with
probability ~1e-290 under the DSR's own null, i.e. the data annihilate the
homogeneity premise the plug-in requires.

**Ruling** (three independent methodology reviews — statistical-theory,
practitioner, adversarial-skeptic lenses — unanimous, high confidence,
conducted with the code and both fork outcomes visible; see mitigations):

- **Governing:** null-calibrated hurdle. SR* = E[max of N SR estimates],
  every trial true SR = 0, per-trial variance 1/(n_i - 1) (Mertens at
  SR = 0), independence assumed, N = ALL trials conducted (controls
  included, correlation NOT discounted — both choices raise the hurdle).
  Computed by seeded Monte Carlo (`stats.expected_max_sharpe_null_mc`)
  because per-trial n is heterogeneous.
- **Disclosed, not governing:** the paper plug-in, printed alongside in
  every Stage 3 row. Rationale for demotion: with designed controls it
  measures grid composition, not selection luck (hurdle grows without bound
  as honest controls are added — it punishes disclosure); with near-clone
  grids it collapses to ~0 (it can be gamed in BOTH directions). A gate no
  true strategy can pass (SR* = 0.73/event ~ t = 15) is uninformative and
  inverts the honesty incentives this project exists to protect.
- **Cross-check printed in every Stage 3 row:** Bonferroni over all N
  trials on the non-normality-adjusted single-test p — valid under
  arbitrary trial correlation.

**Mitigations for the post-hoc timing of this ruling:** the governing rule
uses only (N, n_i) — no observed outcome enters it; it is the STRICTER fork
in high-correlation regimes (it binds both directions); and for H-B the
pass was overdetermined by dependence-free checks (Bonferroni p ~ 1e-3,
walk-forward OOS PSR 0.9999, PBO 0.21).

**Trial-ledger attestation (H-B).** N = 28: the 24-cell entry x horizon
grid plus 4 reversion exhibit configs, all declared in `config.py` before
any result was computed. No exploratory variants were run during
development; no thresholds were changed after results except via the
outcome-blind rulings recorded in this file.

**Standing rule for future hypotheses.** Declare every variant (and its
role: selection-eligible vs control) in config BEFORE computing results.
Controls are fully reported and count toward N, but are never selectable
and never enter any variance plug-in.

## D3 — H-B short-side FUTURES execution validation: pre-declaration (2026-07-03)

Declared BEFORE any futures data was downloaded or any result computed.

**Question.** The spot-price REAL EDGE for the fade signal (+1h entry, 14d
hold) ignores what shorting actually costs. Does the edge survive on the
venue where a short can exist — USDⓈ-M perpetuals — after (a) perp
availability, (b) futures taker fees + adverse fills on perp bars,
(c) FUNDING transfers over the full holding period?

**Exactly TWO configs, both fully reported, both added to the trial ledger
(N: 28 -> 30 for all future DSR deflation):**
- **F1 (primary — the validated cell executed literally):** short the perp
  at spot_t0 + 1h; if no tradeable perp bar exists there, the event is
  UNTRADEABLE (reported, not dropped); hold 336h; exit on the first
  tradeable perp bar at/after entry + 336h (delisted perp -> forced exit at
  last bar, truncated flag).
- **F2 (exhibit — availability-adjusted):** entry = max(spot_t0 + 1h, first
  perp bar) if that is within spot_t0 + 7d, else untradeable; hold 336h.

**Fixed parameters:** futures taker fee 5 bps per side; the same
range-scaled adverse-fill model as spot (open basis, capped half-spread) on
the perp's own 1m bars; funding return for a short = +sum(r_i * P_i/P_entry)
over funding timestamps inside the hold (positive funding pays shorts,
negative charges them), P_i marked from the spot 1h path (perp tracks spot
to bps; documented approximation). Delisted-perp forced exits use the last
bar, not the settlement print (approximation, disclosed).

**Verdict rule (fixed now):** H-B-short(futures) inherits the H-B verdict
machinery where selection is absent: sample gate n >= 30 tradeable events;
expectancy must be positive net of funding; PSR >= 0.95 vs 0 with the
non-normality correction; MinTRL satisfied; walk-forward fold means
reported (no re-selection — the cell was fixed in advance); PBO not
applicable (no variant selection — stated, not skipped silently). Any
failure -> the short side is NOT validated (spot verdict unaffected).
Coverage bias must be reported: perp-covered events vs all events compared
on their SPOT fade returns, so a "perps only exist for big listings"
selection effect is visible instead of silent.

## D2 — Event-definition rulings (2026-07-03)

- USDT pair anchors the event; if the base traded on ANY other Binance pair
  more than 12h earlier, the event is a quote-pair addition, not a listing
  (first-candle semantics would be fake). Ruled while H-B results existed;
  direction of effect on the verdict was unknown at ruling time.
- Redenominations/pegged/fiat bases (LUNC, USTC, BNSOL, AUD, UST, VAI, ...)
  are not listings.
- Walk-forward folds are purged: train events whose 16d forward window
  overlaps the test fold are dropped from train.
