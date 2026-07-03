"""Verdict table + plain-English readout (VALIDATION_GAUNTLET "Output" and
ACCEPTANCE.md "What the engine must print alongside every verdict")."""
import math
from datetime import datetime, timezone

import numpy as np

from . import config


def _ts(ms):
    if not ms:
        return "-"
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def data_summary(conn):
    rows = conn.execute(
        "SELECT COUNT(*), MIN(first_trade_time), MAX(first_trade_time), "
        "SUM(CASE WHEN announcement_time IS NOT NULL THEN 1 ELSE 0 END), "
        "SUM(CASE WHEN listing_status='DELISTED' THEN 1 ELSE 0 END) "
        "FROM events WHERE included=1"
    ).fetchone()
    n, lo, hi, with_ann, delisted = rows
    excl = conn.execute(
        "SELECT exclude_reason, COUNT(*) FROM events WHERE included=0 "
        "GROUP BY exclude_reason ORDER BY COUNT(*) DESC"
    ).fetchall()
    nk = conn.execute("SELECT COUNT(*) FROM klines").fetchone()[0]
    lines = [
        "DATA SUMMARY (survivorship-complete listing events, Binance SPOT)",
        f"  qualifying events        : {n}",
        f"  date range (first trade) : {_ts(lo)} .. {_ts(hi)}",
        f"  with announcement_time   : {with_ann} "
        f"({(with_ann / n * 100) if n else 0:.0f}%)  [rest labeled first-kline-anchor only]",
        f"  now delisted/dead tokens : {delisted} "
        f"({(delisted / n * 100) if n else 0:.0f}%)  [kept — survivorship rule]",
        f"  kline rows stored        : {nk}",
        "  excluded candidates (nothing silently dropped):",
    ]
    for reason, cnt in excl:
        lines.append(f"    - {reason}: {cnt}")
    return "\n".join(lines)


def grid_table(mat):
    """Full 24-cell net-short grid — ACCEPTANCE.md forbids reporting only the
    best cell."""
    M, names = mat["net_short"], mat["config_names"]
    lines = [
        "FULL H-B GRID — net SHORT-side return per event (positive = fade edge)",
        f"  {'cell':<12}{'n':>5}{'mean':>9}{'median':>9}{'SR':>8}{'win%':>7}",
    ]
    for j, name in enumerate(names):
        col = M[:, j]
        col = col[~np.isnan(col)]
        if len(col) == 0:
            lines.append(f"  {name:<12}{0:>5}{'-':>9}{'-':>9}{'-':>8}{'-':>7}")
            continue
        sr = col.mean() / col.std(ddof=1) if len(col) > 1 and col.std(ddof=1) > 0 else float("nan")
        lines.append(
            f"  {name:<12}{len(col):>5}{col.mean()*100:>8.2f}%"
            f"{np.median(col)*100:>8.2f}%{sr:>8.3f}{(col>0).mean()*100:>6.1f}%"
        )
    return "\n".join(lines)


def regime_table(result):
    """Per-year expectancy of the selected cell + walk-forward fold detail —
    a REAL verdict must show WHERE the edge lived, not just that it exists
    on average (a single-regime artifact would decay, not persist)."""
    mat = result["matrices"]
    s3 = result["stages"][2]
    cell = s3.get("best_cell")
    if not cell or cell not in mat["config_names"]:
        return "REGIME EXHIBIT: n/a"
    j = mat["config_names"].index(cell)
    col = mat["net_short"][:, j]
    years = np.array([
        datetime.fromtimestamp(e["first_trade_time"] / 1000, tz=timezone.utc).year
        for e in mat["events"]
    ])
    lines = [f"REGIME EXHIBIT — net short {cell} by listing year",
             f"  {'year':<6}{'n':>4}{'mean':>9}{'median':>9}{'win%':>7}"]
    for y in sorted(set(years)):
        c = col[(years == y) & ~np.isnan(col)]
        if len(c) == 0:
            continue
        lines.append(
            f"  {y:<6}{len(c):>4}{c.mean()*100:>8.2f}%"
            f"{np.median(c)*100:>8.2f}%{(c>0).mean()*100:>6.0f}%"
        )
    s2 = result["stages"][1]
    lines.append("  walk-forward folds (selected cell -> unseen next block):")
    for f in s2.get("folds", []):
        if f.get("selected") is None:
            lines.append(f"    fold {f['fold']}: no positive-SR cell in train -> flat")
            continue
        lines.append(
            f"    fold {f['fold']}: {f['selected']:<9} train SR="
            f"{f['train_sr']:.3f} -> test mean={f['test_mean']*100:.2f}% "
            f"SR={f['test_sr']:.3f} (n={f['test_n']})"
        )
    return "\n".join(lines)


def reversion_table(rev):
    lines = [
        "REVERSION EXHIBIT — pre-declared long-side post-dump entries "
        "(net LONG return; counted in DSR trials; NOT part of the verdict)",
        f"  {'config':<18}{'n':>5}{'mean':>9}{'median':>9}{'win%':>7}",
    ]
    M = rev["net_long"]
    for j, name in enumerate(rev["config_names"]):
        col = M[:, j] if M.size else np.array([])
        col = col[~np.isnan(col)]
        if len(col) == 0:
            lines.append(f"  {name:<18}{0:>5}{'-':>9}{'-':>9}{'-':>7}")
            continue
        lines.append(
            f"  {name:<18}{len(col):>5}{col.mean()*100:>8.2f}%"
            f"{np.median(col)*100:>8.2f}%{(col>0).mean()*100:>6.1f}%"
        )
    return "\n".join(lines)


def verdict_table(results):
    """One row per hypothesis, one column per stage, verdict last."""
    headers = ["Hyp", "S1 sanity", "S2 walk-fwd", "S3 DSR", "S4 PBO",
               "S5 friction", "S6 MinTRL", "VERDICT"]
    rows = []
    for r in results:
        if r.get("not_tested"):
            rows.append([r["hypothesis"]] + ["—"] * 6 + [r["verdict"]])
            continue
        cells = []
        for s in r["stages"]:
            if s.get("pass") is None:
                cells.append("—")
            else:
                cells.append("PASS" if s["pass"] else "FAIL")
        rows.append([r["hypothesis"]] + cells + [r["verdict"]])
    widths = [max(len(h), max((len(row[i]) for row in rows), default=0)) + 2
              for i, h in enumerate(headers)]
    out = ["".join(h.ljust(w) for h, w in zip(headers, widths))]
    out.append("".join("-" * (w - 1) + " " for w in widths))
    for row in rows:
        out.append("".join(c.ljust(w) for c, w in zip(row, widths)))
    return "\n".join(out)


def stage_details(result):
    lines = [f"STAGE DETAIL — {result['hypothesis']}"]
    for s in result["stages"]:
        status = "—" if s.get("pass") is None else ("PASS" if s["pass"] else "FAIL")
        lines.append(f"  [{status:>4}] {s['name']}: {s.get('key_stat', '-')}")
    return "\n".join(lines)


def confidence_note(result):
    """Explicit strong-vs-marginal call (ACCEPTANCE.md)."""
    v = result["verdict"]
    s2, s3, s4, s6 = (result["stages"][1], result["stages"][2],
                      result["stages"][3], result["stages"][5])
    if v == "REAL EDGE":
        margin = []
        if s3.get("dsr", 0) < 0.99:
            margin.append(f"DSR {s3['dsr']:.3f} is close to the 0.95 bar")
        if s4.get("pbo", 0) > 0.3:
            margin.append(f"PBO {s4['pbo']:.2f} is not far from 0.5")
        if isinstance(s6.get("min_trl"), float) and math.isfinite(s6["min_trl"]) \
                and s6["n"] < 2 * s6["min_trl"]:
            margin.append("sample barely clears MinTRL")
        active = [f for f in s2.get("folds", []) if f.get("selected")]
        neg = [f["fold"] for f in active if f.get("test_mean", 0) <= 0]
        if neg:
            margin.append(
                f"walk-forward fold(s) {neg} were flat/negative out-of-sample "
                f"— the edge has soft regimes (see REGIME EXHIBIT)"
            )
        return ("MARGINAL result: " + "; ".join(margin)) if margin else \
            "Strong result: clears every stage with room to spare."
    fails = [s["name"] for s in result["stages"] if s.get("pass") is False]
    n_fail = len(fails)
    if n_fail >= 3:
        return f"High-confidence {v}: failed {n_fail} independent stages ({', '.join(fails)})."
    return f"{v} on {n_fail} stage(s): {', '.join(fails)}. See stage detail."


def plain_english(result):
    v = result["verdict"]
    d = result["descriptive"]
    s5 = result["stages"][4]
    base = (
        f"H-B claimed newly listed tokens systematically decline after listing, "
        f"so fading/avoiding the listing has positive expectancy. "
        f"Descriptively the dump is real: {d['frac_below_entry_at_14d']*100:.0f}% of "
        f"listings trade below their first-hour price 14 days later, "
        f"{d['frac_peak_within_24h']*100:.0f}% peak within the first 24h, and the "
        f"median max drawdown inside 14 days is "
        f"{d['median_max_drawdown_14d']*100:.0f}%. "
    )
    if v == "REAL EDGE":
        s3 = result["stages"][2]
        action = (
            f"The tested rule — short at {s3.get('best_cell','?')} — survived "
            f"walk-forward, deflation, PBO, friction and the sample-size gate. "
            f"READ THE VERDICT NARROWLY. It validates the fade SIGNAL as "
            f"measured on spot prices; it does NOT authorize deploying "
            f"capital. (1) On SPOT the only actionable form is: do not buy "
            f"listings; there is nothing to short. (2) Short execution lives "
            f"on futures, and this run does NOT model funding/borrow: fresh "
            f"listings often trade with deeply negative perp funding (shorts "
            f"PAY, at extremes ~0.1-1% per 8h), which over a 14d hold could "
            f"consume the entire measured edge — per HYPOTHESIS.md the short "
            f"side requires a separate futures-market validation with funding "
            f"data before any deployment. (3) A perp must EXIST at entry "
            f"time; the shortable universe is a subset of these events. "
            f"(4) The return profile is short-squeeze-shaped (skew "
            f"{s3.get('skew', float('nan')):.1f}): most events win, rare "
            f"events lose multiples — position sizing is the difference "
            f"between this edge and ruin. Why it may persist: the fade side "
            f"is capacity-limited and uncrowded — retail flow is structurally "
            f"on the buy side at listings, and the borrow/funding cost that "
            f"protects it from arbitrage is exactly the cost that must be "
            f"validated next."
        )
    elif v == "GHOST":
        action = (
            f"But as a TRADEABLE rule the fade did not survive the gauntlet "
            f"({result['why']}). Friction on thin listing-day books "
            f"(S5: {s5.get('key_stat','-')}) and non-generalizing cell selection "
            f"are the usual killers. Actionable content that DOES survive: the "
            f"descriptive base rates above justify 'do not BUY listings' as a "
            f"defensive rule, but do not authorize deploying capital on the "
            f"short side."
        )
    else:
        action = (
            f"The evidence is insufficient to call it either way "
            f"({result['why']}). Treat as do-not-deploy; revisit when more "
            f"listing events have accrued."
        )
    return base + action


def futures_section(fx):
    """H-B short-side futures execution (docs/DECISIONS.md D3)."""
    f1, f2, cov = fx["f1"], fx["f2"], fx["coverage"]

    def fmt(b, label):
        if b.get("n", 0) < 2:
            return f"  {label}: n={b.get('n', 0)} — not computable"
        return (
            f"  {label}: n={b['n']}  mean={b['mean']*100:+.2f}%/event  "
            f"median={b['median']*100:+.2f}%  win={b['win']*100:.0f}%  "
            f"SR={b['sr']:.3f}  PSR={b['psr']:.4f}  MinTRL={b['mtrl']:.0f}\n"
            f"      chronological quarter-means: "
            + "  ".join(f"{m*100:+.1f}%" for m in b["fold_means"])
        )

    lines = [
        "H-B SHORT-SIDE FUTURES EXECUTION (pre-declared D3; funding included)",
        fmt(f1, "F1 short perp at spot_t0+1h, hold 14d (primary)"),
        fmt(f2, "F2 entry at perp launch if within 7d (exhibit)"),
        f"  funding contribution (F1): mean {fx['funding_mean']*100:+.2f}% "
        f"per event; funding NET-NEGATIVE for the short in "
        f"{fx['funding_negative_frac']*100:.0f}% of events",
        f"  coverage bias check: perp-tradeable events' SPOT fade mean "
        f"{cov['covered_spot_mean']*100:+.2f}% (n={cov['covered_n']}) vs "
        f"untradeable {cov['uncovered_spot_mean']*100:+.2f}% "
        f"(n={cov['uncovered_n']}) — if these differ materially, the "
        f"shortable universe is not the measured universe",
        f"  PBO: not applicable (no variant selection — config was fixed by "
        f"the spot verdict before any futures data was read)",
        f"  VERDICT (short side, futures): {fx['verdict']} — {fx['why']}",
    ]
    return "\n".join(lines)


def not_tested(hyp, reason):
    return {"hypothesis": hyp, "verdict": f"NOT TESTED", "not_tested": True,
            "reason": reason}


def full_report(conn, results, futures_result=None):
    parts = [data_summary(conn), ""]
    parts.append(verdict_table(results))
    parts.append("")
    if futures_result is not None:
        parts.append(futures_section(futures_result))
        parts.append("")
    for r in results:
        if r.get("not_tested"):
            parts.append(f"{r['hypothesis']}: NOT TESTED — {r['reason']}")
            parts.append("")
            continue
        parts.append(stage_details(r))
        parts.append("")
        parts.append("Confidence: " + confidence_note(r))
        parts.append("")
        parts.append(plain_english(r))
        parts.append("")
        parts.append(grid_table(r["matrices"]))
        parts.append("")
        parts.append(regime_table(r))
        parts.append("")
        parts.append(reversion_table(r["reversion"]))
        parts.append("")
    return "\n".join(parts)
