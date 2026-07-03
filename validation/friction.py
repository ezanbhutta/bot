"""Fill and friction model (feeds Stage 5, but applied in EVERY stage).

No mid-price-fill fantasy (VALIDATION_GAUNTLET Stage 5): fills happen at a
bar-VWAP proxy shifted AGAINST the trader by an estimated half-spread +
impact, scaled to the actual volatility of the fill bar. On the listing
minute the bar range is often enormous, so the model correctly makes
listing-minute fills brutal.

Conventions:
  * Fill basis = the OPEN of the fill bar: the only price observable at the
    decision instant. (An OHLC-average basis would leak post-decision
    intra-bar drift INTO the fill — e.g. a short covering into a downtrend
    would buy back below the decision-time price. Open-basis + adverse
    shift cannot flatter the trader.)
  * hs   = half-spread + impact, always applied AGAINST the trader.
           1m bars: max(floor, frac * range/open) — the bar's own realized
           range only ever worsens the fill, never improves it.
           1h bars: range rescaled by sqrt(60) (Brownian scaling) with a
           higher floor, since we can't see intrabar microstructure.
  * Fees: spot taker both sides.
  * Short returns are computed on proceeds notional: r = 1 - B/S. Losses are
    NOT capped at -100% — a 3x pump against the short shows up as -200%.
    That overstates short risk vs a real margined venue (which liquidates),
    i.e. it is conservative AGAINST the fade edge. Funding/borrow costs are
    NOT modeled (flagged in the report; they typically hurt shorts further
    on fresh listings).
"""
import math

from . import config


def half_spread(bar, res: str) -> float:
    o, h, l = bar[0], bar[1], bar[2]
    if o <= 0:
        return float("nan")
    rng = (h - l) / o
    if res == "1m":
        hs = max(config.HALF_SPREAD_MIN, config.HALF_SPREAD_RANGE_FRAC * rng)
    else:
        hs = max(
            config.HALF_SPREAD_LATE,
            config.HALF_SPREAD_RANGE_FRAC * rng / math.sqrt(60.0),
        )
    return min(hs, config.HALF_SPREAD_CAP)


def tradeable(bar) -> bool:
    """bar = (open, high, low, close, quote_volume)."""
    return bar[4] >= config.MIN_BAR_QUOTE_VOLUME and bar[0] > 0


def buy_price(bar, res: str) -> float:
    return bar[0] * (1.0 + half_spread(bar, res))


def sell_price(bar, res: str) -> float:
    return bar[0] * (1.0 - half_spread(bar, res))


def net_long_return(entry_bar, entry_res, exit_bar, exit_res) -> float:
    f = config.TAKER_FEE
    cost = buy_price(entry_bar, entry_res) * (1.0 + f)
    proceeds = sell_price(exit_bar, exit_res) * (1.0 - f)
    return proceeds / cost - 1.0


def net_short_return(entry_bar, entry_res, exit_bar, exit_res) -> float:
    f = config.TAKER_FEE
    s = sell_price(entry_bar, entry_res) * (1.0 - f)   # short-sale proceeds
    b = buy_price(exit_bar, exit_res) * (1.0 + f)      # buy-back cost
    if s <= 0 or b <= 0:
        return float("nan")  # corrupted fill must never masquerade as P&L
    return 1.0 - b / s


def gross_long_return(entry_bar, exit_bar) -> float:
    """Frictionless open-to-open return — the naive backtest number, kept
    only so Stage 5 can show exactly how much friction eats."""
    if entry_bar[0] <= 0:
        return float("nan")
    return exit_bar[0] / entry_bar[0] - 1.0
