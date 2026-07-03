"""USDⓈ-M perpetual-futures data layer (read-only, public S3 archive).

fapi.binance.com is geo-blocked here (HTTP 451, same as the spot API), but
data.binance.vision archives the full futures history — klines AND funding
rates — including delisted perps, which keeps this leg survivorship-complete
too. Everything comes from:
    data/futures/um/monthly/klines/<PERP>/...      (universe enumeration)
    data/futures/um/daily/klines/<PERP>/1m/...     (fill bars, exact launch)
    data/futures/um/monthly/fundingRate/<PERP>/... (funding transfers)
"""
import csv
import io
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timedelta, timezone

from . import config
from .market_data import S3NS, _get, _session  # noqa: F401  (shared helpers)

PERP_PREFIXES = ["", "1000", "1M", "1000000"]  # 1000PEPE, 1MBABYDOGE, ...


def perp_candidates(base: str):
    return [f"{p}{base}USDT" for p in PERP_PREFIXES]


def get_perp_universe(sess=None):
    """Every UM perp symbol that ever traded (S3 listing, paginated)."""
    sess = sess or _session()
    symbols = []
    marker = ""
    prefix = "data/futures/um/monthly/klines/"
    while True:
        params = {"delimiter": "/", "prefix": prefix}
        if marker:
            params["marker"] = marker
        r = _get(sess, config.ARCHIVE_S3, params=params, timeout=60)
        root = ET.fromstring(r.content)
        for cp in root.findall(f"{S3NS}CommonPrefixes"):
            symbols.append(cp.find(f"{S3NS}Prefix").text[len(prefix):].strip("/"))
        trunc = root.find(f"{S3NS}IsTruncated")
        if trunc is None or trunc.text != "true":
            break
        nm = root.find(f"{S3NS}NextMarker")
        marker = nm.text if nm is not None else (prefix + symbols[-1] + "/")
    return set(symbols)


def perp_daily_dates(sess, perp):
    """Sorted list of YYYY-MM-DD dates with a daily 1m zip for this perp.
    First date ~ launch day; last date < today means the perp was delisted."""
    dates = []
    marker = ""
    prefix = f"data/futures/um/daily/klines/{perp}/1m/"
    while True:
        params = {"prefix": prefix, "max-keys": "1000"}
        if marker:
            params["marker"] = marker
        r = _get(sess, config.ARCHIVE_S3, params=params, timeout=60)
        root = ET.fromstring(r.content)
        keys = [c.find(f"{S3NS}Key").text for c in root.findall(f"{S3NS}Contents")]
        for k in keys:
            if k.endswith(".zip"):
                dates.append(k.rsplit("-1m-", 1)[1][:-4])
        trunc = root.find(f"{S3NS}IsTruncated")
        if trunc is None or trunc.text != "true" or not keys:
            break
        marker = keys[-1]
    return sorted(dates)


def _parse_kline_csv(text, start_ms, end_ms):
    out = []
    for row in csv.reader(io.StringIO(text)):
        if not row or not row[0].isdigit():
            continue
        ot = int(row[0])
        if ot >= 10**14:
            ot //= 1000
        if start_ms <= ot < end_ms:
            ct = int(row[6])
            if ct >= 10**14:
                ct //= 1000
            out.append([ot, row[1], row[2], row[3], row[4], row[5], ct,
                        row[7], row[8], row[9], row[10]])
    return out


def fetch_perp_1m_window(sess, perp, center_ms, half_window_ms):
    """1m perp bars in [center-half, center+half] via the daily zip(s)."""
    lo, hi = center_ms - half_window_ms, center_ms + half_window_ms
    out = []
    day_ms = 86_400_000
    day = (lo // day_ms) * day_ms
    while day <= hi:
        ds = datetime.fromtimestamp(day / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        url = (f"{config.ARCHIVE_HTTP}/data/futures/um/daily/klines/"
               f"{perp}/1m/{perp}-1m-{ds}.zip")
        try:
            r = _get(sess, url, timeout=90)
        except Exception:
            day += day_ms
            continue  # missing day (pre-launch / post-delist) is legitimate
        with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
            text = zf.read(zf.namelist()[0]).decode()
        out.extend(_parse_kline_csv(text, lo, hi + 1))
        day += day_ms
    return out


def fetch_funding(sess, perp, start_ms, end_ms):
    """[(funding_time_ms, rate), ...] within (start, end] from monthly zips."""
    out = []
    dt = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0)
    end_dt = datetime.fromtimestamp(end_ms / 1000, tz=timezone.utc)
    while dt <= end_dt:
        ym = dt.strftime("%Y-%m")
        url = (f"{config.ARCHIVE_HTTP}/data/futures/um/monthly/fundingRate/"
               f"{perp}/{perp}-fundingRate-{ym}.zip")
        try:
            r = _get(sess, url, timeout=60)
            with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
                text = zf.read(zf.namelist()[0]).decode()
            for row in csv.reader(io.StringIO(text)):
                if not row or not row[0].strip().isdigit():
                    continue  # header
                ts = int(row[0])
                if ts >= 10**14:
                    ts //= 1000
                rate = float(row[-1])
                if start_ms < ts <= end_ms:
                    out.append((ts, rate))
        except Exception:
            pass  # month before launch / after delist
        dt = (dt.replace(day=28) + timedelta(days=5)).replace(day=1)
    return sorted(out)
