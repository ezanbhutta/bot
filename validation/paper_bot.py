"""Paper-trading bot — watch every step it takes.

PAPER ONLY. This never places a live order, never holds a key, never touches
capital (PROJECT_BRIEF scope fence). It reproduces, step by step and out
loud, exactly what an execution bot WOULD do for each forward listing under
the validated F1 rule (short the perp at spot_t0 + 1h, hold 14d), then logs
the paper order and the realized paper P&L including funding.

Two honesty rules baked in:
  * No lookahead. Every DECISION (trade / skip, size, funding guard) uses
    only information available at the entry minute. Realized funding is used
    only to score the outcome AFTER the fact, never to decide.
  * The entry rule is frozen (F1). The funding guard is an explicitly
    labelled execution overlay, not a change to the validated verdict — it
    is shown so you can see the effect of the "stay safe" instinct, not
    because it has been validated.
"""
from datetime import datetime, timezone

from . import config, friction, futures_exec, storage

H_MS = 3_600_000
DAY_MS = 86_400_000


def _fmt(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M") if ms else "–"


def _usd(x):
    return "–" if x is None else f"${x:,.2f}"


def _pct(x):
    return "–" if x is None else f"{x * 100:+.2f}%"


def _position_sizing():
    """Survival-first sizing. Returns (margin, notional) in USD."""
    margin = (config.BOT_ACCOUNT_USD * config.BOT_MAX_ACCOUNT_LOSS_PER_TRADE
              / config.BOT_WORST_CASE_LOSS_FRAC)
    notional = margin  # 1x isolated: notional == margin
    return margin, notional


def _funding_rate_at_entry(conn, perp, entry_ts):
    """Most recent funding rate with funding_time <= entry_ts (no lookahead).
    Returns (rate, funding_time) or (None, None)."""
    row = conn.execute(
        "SELECT rate, funding_time FROM funding_rates "
        "WHERE symbol=? AND funding_time <= ? ORDER BY funding_time DESC LIMIT 1",
        (perp, entry_ts)).fetchone()
    return (row[0], row[1]) if row else (None, None)


def decide(conn, ev):
    """Build a full step-by-step decision trace for one forward listing."""
    sym, t0, base = ev["symbol"], ev["first_trade_time"], ev["base_asset"]
    steps = []
    ticket = None

    def step(name, ok, detail):
        steps.append({"name": name, "ok": ok, "detail": detail})

    step("1 · DETECT listing", True,
         f"{sym} first traded {_fmt(t0)} UTC (base {base})")

    # 2 · Universe filter — is this a real crypto listing we would ever touch?
    if base in config.STABLE_OR_PEGGED_BASES:
        step("2 · UNIVERSE filter", False,
             "stablecoin/pegged — no listing-pump dynamics, SKIP")
        return {"symbol": sym, "t0": t0, "steps": steps, "decision": "SKIP",
                "reason": "not a tradeable listing", "ticket": None}
    if base in config.TOKENIZED_EQUITY_BASES:
        step("2 · UNIVERSE filter", False,
             "tokenized equity (xStock) — pegged to a real stock, SKIP")
        return {"symbol": sym, "t0": t0, "steps": steps, "decision": "SKIP",
                "reason": "tokenized equity", "ticket": None}
    step("2 · UNIVERSE filter", True, "genuine crypto spot listing — proceed")

    # 3 · Perp check — the edge is short-only, so a perp must exist & be
    #     tradeable at the entry minute (spot_t0 + 1h). No perp -> nothing to do.
    perp = conn.execute(
        "SELECT perp_symbol FROM perp_meta WHERE spot_symbol=?", (sym,)
    ).fetchone()
    perp = perp[0] if perp else None
    entry_ts = t0 + config.FUT_ENTRY_OFFSET_MS
    if not perp:
        step("3 · PERP available?", False,
             "no USDⓈ-M perpetual exists — cannot short on spot, SKIP "
             "(discipline: no venue = no trade)")
        return {"symbol": sym, "t0": t0, "steps": steps, "decision": "SKIP",
                "reason": "no perp to short", "ticket": None}
    arr = futures_exec._load_perp_bars(conn, perp)
    e_bar = e_ot = None
    if arr is not None:
        e_bar, e_ot = futures_exec._entry_bar(arr, entry_ts)
    if e_bar is None:
        step("3 · PERP available?", False,
             f"perp {perp} exists but no tradeable bar at entry "
             f"{_fmt(entry_ts)} — SKIP")
        return {"symbol": sym, "t0": t0, "steps": steps, "decision": "SKIP",
                "reason": "perp not tradeable at entry", "ticket": None}
    step("3 · PERP available?", True,
         f"{perp} tradeable at entry {_fmt(entry_ts)} (entry price "
         f"{e_bar[0]:.6g})")

    # 4 · Funding guard (labelled execution overlay; no-lookahead).
    rate, r_ts = _funding_rate_at_entry(conn, perp, entry_ts)
    guard_blocks = False
    if rate is None:
        step("4 · FUNDING guard [overlay]", True,
             "no funding rate published yet at entry — proceed, flag as "
             "unknown cost")
        projected = None
    else:
        projected = rate * config.BOT_FUNDING_INTERVALS_14D  # short PAYS +rate
        detail = (f"entry funding {rate * 100:+.4f}%/8h → projected 14d cost "
                  f"to a short ≈ {projected * 100:+.2f}% of notional")
        if config.BOT_FUNDING_GUARD and projected > config.BOT_FUNDING_MAX_PROJECTED:
            guard_blocks = True
            step("4 · FUNDING guard [overlay]", False,
                 detail + f" — exceeds {config.BOT_FUNDING_MAX_PROJECTED*100:.0f}% "
                 f"cap, GUARD SAYS SKIP (the 'stay safe' rule)")
        else:
            step("4 · FUNDING guard [overlay]", True, detail + " — within cap")

    # 5 · Sizing (survival-first).
    margin, notional = _position_sizing()
    fee_cost = notional * config.FUT_TAKER_FEE * 2
    step("5 · SIZE (survival-first)", True,
         f"account {_usd(config.BOT_ACCOUNT_USD)}, risk cap "
         f"{config.BOT_MAX_ACCOUNT_LOSS_PER_TRADE*100:.0f}%/trade, worst-case "
         f"{config.BOT_WORST_CASE_LOSS_FRAC:.1f}× margin ⇒ margin {_usd(margin)}, "
         f"1× short notional {_usd(notional)}; round-trip taker fees ≈ "
         f"{_usd(fee_cost)}")

    # 6 · Paper entry ticket (what a live bot WOULD send — never sent).
    ticket = {
        "action": "OPEN SHORT (paper)", "perp": perp,
        "entry_time": e_ot, "entry_price": e_bar[0],
        "margin": margin, "notional": notional, "leverage": "1x isolated",
        "fee_cost": fee_cost,
    }
    if guard_blocks:
        step("6 · ORDER", False,
             "funding guard vetoed the trade → NO paper order placed")
        decision = "SKIP (funding guard)"
    else:
        step("6 · ORDER (paper — not sent)", True,
             f"would OPEN SHORT {perp}: notional {_usd(notional)} @ "
             f"{e_bar[0]:.6g}, 1× isolated, hold 14d")
        decision = "TRADE (paper)"

    # 7 · Outcome (realized; only scores the trade, never decides it).
    outcome = _score(conn, perp, arr, e_bar, e_ot, notional, fee_cost)
    if decision.startswith("TRADE"):
        if outcome["status"] == "SETTLED":
            step("7 · HOLD 14d → EXIT (paper)", True,
                 f"bought back {_fmt(outcome['exit_time'])} @ "
                 f"{outcome['exit_price']:.6g}; price P&L "
                 f"{_pct(outcome['price_ret'])}, funding "
                 f"{_pct(outcome['funding_ret'])}")
            step("8 · RESULT", outcome["net_usd"] >= 0,
                 f"net {_pct(outcome['net_ret'])} on notional = "
                 f"{_usd(outcome['net_usd'])}  →  "
                 f"{'WIN' if outcome['net_usd'] >= 0 else 'LOSS'}")
        elif outcome["status"] == "CLOSED_PRICE":
            so_far = outcome["price_ret"] + outcome["funding_ret"]
            step("7 · HOLD 14d → EXIT (paper)", True,
                 f"bought back {_fmt(outcome['exit_time'])} @ "
                 f"{outcome['exit_price']:.6g}; price P&L "
                 f"{_pct(outcome['price_ret'])}, funding so far "
                 f"{_pct(outcome['funding_ret'])} (still publishing)")
            step("8 · RESULT so far (not final)", so_far >= 0,
                 f"net {_pct(so_far)} on {_usd(notional)} = "
                 f"{_usd(so_far * notional)} → "
                 f"{'WINNING' if so_far >= 0 else 'LOSING'} "
                 f"(final once funding fully settles)")
        else:
            step("7 · HOLD 14d", True,
                 f"position still open — {outcome.get('detail','')}")
    return {"symbol": sym, "t0": t0, "perp": perp, "steps": steps,
            "decision": decision, "reason": "",
            "ticket": None if guard_blocks else ticket, "outcome": outcome,
            "guard_blocks": guard_blocks}


def _score(conn, perp, arr, e_bar, e_ot, notional, fee_cost):
    """Realized paper P&L for an executed short (scoring only)."""
    exit_ts = e_ot + config.FUT_HOLD_MS
    x_bar, x_ot, trunc = futures_exec._exit_bar(arr, exit_ts)
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    if x_bar is None or x_ot <= e_ot:
        return {"status": "OPEN", "detail": "no exit bar yet (still holding)"}
    if x_ot < exit_ts - DAY_MS:
        # exit bar is well before the intended 14d — data not caught up
        if now_ms < exit_ts + DAY_MS:
            return {"status": "HOLDING",
                    "detail": f"exits ~{_fmt(exit_ts)} (in the future)"}
    price_ret = friction.net_short_return(e_bar, "1m", x_bar, "1m",
                                          fee=config.FUT_TAKER_FEE)
    marks = futures_exec._spot_marks(
        conn, conn.execute("SELECT spot_symbol FROM perp_meta WHERE perp_symbol=?",
                           (perp,)).fetchone()[0])
    funding_ret, n_f = futures_exec._funding_return(
        conn, perp, marks, e_bar[0], e_ot, x_ot)
    # Funding completeness: is the last funding stamp near the exit?
    latest = conn.execute(
        "SELECT MAX(funding_time) FROM funding_rates WHERE symbol=?",
        (perp,)).fetchone()[0] or 0
    if latest < x_ot - int(8.5 * H_MS):
        return {"status": "CLOSED_PRICE", "price_ret": price_ret,
                "funding_ret": funding_ret, "exit_time": x_ot,
                "exit_price": x_bar[0],
                "detail": "price final; funding still publishing"}
    net_ret = price_ret + funding_ret
    return {"status": "SETTLED", "price_ret": price_ret,
            "funding_ret": funding_ret, "net_ret": net_ret,
            "net_usd": net_ret * notional, "exit_time": x_ot,
            "exit_price": x_bar[0]}


def run(conn, only_symbol=None):
    rows = conn.execute(
        "SELECT symbol, base_asset, first_trade_time FROM paper_trades "
        "ORDER BY first_trade_time").fetchall()
    events = [{"symbol": s, "base_asset": b, "first_trade_time": t}
              for s, b, t in rows]
    if only_symbol:
        events = [e for e in events if e["symbol"] == only_symbol]
    return [decide(conn, e) for e in events]


def render(traces):
    lines = [
        "PAPER-TRADING BOT — step-by-step decision log",
        "PAPER ONLY · no live orders · no keys · no capital · rule frozen (F1)",
        f"account {_usd(config.BOT_ACCOUNT_USD)} · risk cap "
        f"{config.BOT_MAX_ACCOUNT_LOSS_PER_TRADE*100:.0f}%/trade · funding guard "
        f"{'ON' if config.BOT_FUNDING_GUARD else 'OFF'} "
        f"(≤{config.BOT_FUNDING_MAX_PROJECTED*100:.0f}% projected)",
        "=" * 72,
    ]
    traded = settled = wins = 0
    net_usd = 0.0
    for tr in traces:
        lines.append("")
        lines.append(f"▶ {tr['symbol']}  —  decision: {tr['decision']}")
        for s in tr["steps"]:
            mark = "✓" if s["ok"] else "✗"
            lines.append(f"   {mark} {s['name']}: {s['detail']}")
        o = tr.get("outcome") or {}
        if tr["decision"].startswith("TRADE") and o.get("status") == "SETTLED":
            traded += 1
            settled += 1
            net_usd += o["net_usd"]
            wins += 1 if o["net_usd"] >= 0 else 0
        elif tr["decision"].startswith("TRADE"):
            traded += 1
    lines.append("")
    lines.append("=" * 72)
    lines.append(
        f"SUMMARY: {len(traces)} listings seen · {traded} paper trades placed "
        f"· {settled} settled · {wins} wins")
    if settled:
        lines.append(
            f"         net paper P&L on settled trades: {_usd(net_usd)} "
            f"(win rate {wins/settled*100:.0f}%)")
    else:
        lines.append("         no settled paper trades yet — nothing has "
                     "completed its 14-day hold + funding settlement")
    lines.append(
        "NOTE: the rule is frozen on purpose. Watching it is the test; "
        "re-tuning it on these results is the overfitting trap the whole "
        "project exists to avoid (docs/DECISIONS.md D4).")
    return "\n".join(lines)
