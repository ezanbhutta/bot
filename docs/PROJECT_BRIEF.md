# PROJECT_BRIEF.md — Read this first, obey it literally

## What this is
A **strategy VALIDATION engine**. Its only job is to test whether a market
hypothesis is a real, tradeable edge or a statistical ghost. It answers one
question per hypothesis: **REAL EDGE, GHOST, or INCONCLUSIVE.**

## What this is NOT
- NOT a trading bot.
- NOT an order-execution system.
- It never places a live trade.
- It never holds an API key with trade permissions.
- It never touches capital.
- It never connects to a live account for anything except READ-ONLY public
  market data.

## Hard scope fence
If you (the implementing agent) find yourself writing:
- order placement / `POST /order` / any buy or sell call
- private-key or trade-permission handling
- live position management
- anything that moves money

**STOP.** That is out of scope. This project ends at the verdict. Execution is
a separate future project that only begins AFTER a hypothesis passes validation.

## Market
Binance **SPOT** only. No futures, no margin, no leverage. Reason: we are proving
whether an edge exists. The worst acceptable failure is "the signal was a ghost
and we didn't trade." We do not add leverage or liquidation risk to an unproven
idea.

## The one belief being tested
"Coins move in a predictable, tradeable way around major Binance listings."
Naive intuition says *buy the listing*. Prior research says that makes you exit
liquidity for insiders (46% of listings peak at listing and never recover; ~98%
eventually dump). So the intuition must be decomposed and each version tested in
isolation. See HYPOTHESIS.md.

## Definition of done
The engine ingests real Binance listing history + price data, runs each
hypothesis through the full gauntlet (VALIDATION_GAUNTLET.md), and prints a
verdict table. That is the entire deliverable. No bot. No dashboard beyond the
verdict output. No execution.

## Prime directive for the implementing agent
Challenge the hypotheses where the data is weak. Do NOT rationalize a weak edge
into a strong one. Default to GHOST / INCONCLUSIVE. A false REAL is the only
truly expensive bug here — it would authorize real capital on a fake edge.
