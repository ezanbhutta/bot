"""Market data layer.

Sources (all public, read-only, keyless — PROJECT_BRIEF scope fence):
  * REST: data-api.binance.vision /api/v3/* — official mirror of the Binance
    spot API. Empirically it serves historical klines for DELISTED symbols
    too, which is what makes survivorship-complete ingestion possible here.
  * S3 archive: data.binance.vision — used to enumerate every symbol that
    EVER traded (the survivorship-complete universe) and as a kline fallback
    (daily zip files) for anything the REST mirror won't return.
"""
import csv
import io
import time
import xml.etree.ElementTree as ET
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

import requests

from . import config

S3NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"


def _session():
    s = requests.Session()
    s.headers.update({"User-Agent": config.USER_AGENT})
    return s


def _get(sess, url, params=None, timeout=30, retries=5):
    for attempt in range(retries):
        try:
            r = sess.get(url, params=params, timeout=timeout)
            if r.status_code in (429, 418):
                wait = int(r.headers.get("Retry-After", 2 ** (attempt + 1)))
                time.sleep(min(wait, 60))
                continue
            if 400 <= r.status_code < 500:
                r.raise_for_status()  # client error: retrying won't help
            r.raise_for_status()
            return r
        except requests.HTTPError as e:
            if e.response is not None and 400 <= e.response.status_code < 500:
                raise
            if attempt == retries - 1:
                raise
            time.sleep(2 ** attempt)
        except requests.RequestException:
            if attempt == retries - 1:
                raise
            time.sleep(2 ** attempt)
    raise RuntimeError(f"unreachable: {url}")


# ---------------------------------------------------------------------------
# Universe
# ---------------------------------------------------------------------------

def get_exchange_info(sess=None):
    """Current spot symbols -> {symbol: (base, quote, status)}."""
    sess = sess or _session()
    r = _get(sess, f"{config.REST_BASE}/api/v3/exchangeInfo")
    out = {}
    for s in r.json()["symbols"]:
        out[s["symbol"]] = (s["baseAsset"], s["quoteAsset"], s["status"])
    return out


def get_archive_symbols(sess=None):
    """Every symbol directory ever created in the public archive (includes
    delisted/failed tokens — this is the anti-survivorship source)."""
    sess = sess or _session()
    symbols = []
    marker = ""
    prefix = "data/spot/monthly/klines/"
    while True:
        params = {"delimiter": "/", "prefix": prefix}
        if marker:
            params["marker"] = marker
        r = _get(sess, config.ARCHIVE_S3, params=params, timeout=60)
        root = ET.fromstring(r.content)
        for cp in root.findall(f"{S3NS}CommonPrefixes"):
            p = cp.find(f"{S3NS}Prefix").text
            symbols.append(p[len(prefix):].strip("/"))
        truncated = root.find(f"{S3NS}IsTruncated")
        if truncated is None or truncated.text != "true":
            break
        nm = root.find(f"{S3NS}NextMarker")
        marker = nm.text if nm is not None else (prefix + symbols[-1] + "/")
    return symbols


def split_symbol(symbol: str, exchange_info=None):
    """(base, quote) via exchangeInfo when available, else suffix matching."""
    if exchange_info and symbol in exchange_info:
        b, q, _ = exchange_info[symbol]
        return b, q
    for q in sorted(config.QUOTE_ASSETS, key=len, reverse=True):
        if symbol.endswith(q) and len(symbol) > len(q):
            return symbol[: -len(q)], q
    return None, None


# ---------------------------------------------------------------------------
# Klines — REST first, archive zip fallback
# ---------------------------------------------------------------------------

def fetch_klines_rest(sess, symbol, interval, start_ms, end_ms):
    """Paginated /api/v3/klines. Returns raw kline lists (Binance format)."""
    out = []
    cursor = start_ms
    while cursor < end_ms:
        r = _get(
            sess,
            f"{config.REST_BASE}/api/v3/klines",
            params={
                "symbol": symbol,
                "interval": interval,
                "startTime": cursor,
                "endTime": end_ms - 1,
                "limit": 1000,
            },
        )
        batch = r.json()
        if not batch:
            break
        out.extend(batch)
        last_open = batch[-1][0]
        if len(batch) < 1000:
            break
        cursor = last_open + 1
    return out


def _interval_ms(interval):
    return {"1m": 60_000, "1h": 3_600_000}[interval]


def fetch_klines_archive(sess, symbol, interval, start_ms, end_ms):
    """Daily zip fallback from data.binance.vision for rows REST won't serve."""
    out = []
    day = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    end_dt = datetime.fromtimestamp(end_ms / 1000, tz=timezone.utc)
    while day <= end_dt:
        ds = day.strftime("%Y-%m-%d")
        url = (
            f"{config.ARCHIVE_HTTP}/data/spot/daily/klines/"
            f"{symbol}/{interval}/{symbol}-{interval}-{ds}.zip"
        )
        try:
            r = sess.get(url, timeout=60)
        except requests.RequestException:
            day += timedelta(days=1)
            continue
        if r.status_code == 200:
            with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
                name = zf.namelist()[0]
                text = zf.read(name).decode()
            for row in csv.reader(io.StringIO(text)):
                if not row or not row[0].isdigit():
                    continue  # newer files carry a header row
                ot = int(row[0])
                if ot >= 10**14:   # some archive files use microseconds
                    ot //= 1000
                if start_ms <= ot < end_ms:
                    out.append(
                        [ot, row[1], row[2], row[3], row[4], row[5],
                         int(row[6]) // (1000 if int(row[6]) >= 10**14 else 1),
                         row[7], row[8], row[9], row[10]]
                    )
        day += timedelta(days=1)
    return out


def fetch_klines(sess, symbol, interval, start_ms, end_ms):
    """REST, falling back to archive zips if REST returns nothing/partial.

    Returns (klines, source) with klines in Binance list format.
    """
    rows = fetch_klines_rest(sess, symbol, interval, start_ms, end_ms)
    if rows:
        return rows, "rest"
    rows = fetch_klines_archive(sess, symbol, interval, start_ms, end_ms)
    return rows, "archive_zip"


def first_kline_ms(sess, symbol):
    """Open time of the first 1m kline ever for `symbol` (ms), or None."""
    try:
        r = _get(
            sess,
            f"{config.REST_BASE}/api/v3/klines",
            params={"symbol": symbol, "interval": "1m", "startTime": 0,
                    "limit": 1},
        )
        batch = r.json()
        if batch:
            return int(batch[0][0])
    except requests.RequestException:
        pass  # REST refuses some long-delisted symbols; archive still has them
    # Fallback: earliest monthly zip in the archive, then exact via zip content.
    prefix = f"data/spot/monthly/klines/{symbol}/1m/"
    resp = _get(sess, config.ARCHIVE_S3,
                params={"prefix": prefix, "max-keys": "5"}, timeout=60)
    root = ET.fromstring(resp.content)
    keys = sorted(
        c.find(f"{S3NS}Key").text for c in root.findall(f"{S3NS}Contents")
        if c.find(f"{S3NS}Key").text.endswith(".zip")
    )
    if not keys:
        return None
    url = f"{config.ARCHIVE_HTTP}/{keys[0]}"
    r2 = sess.get(url, timeout=120)
    if r2.status_code != 200:
        return None
    with zipfile.ZipFile(io.BytesIO(r2.content)) as zf:
        name = zf.namelist()[0]
        with zf.open(name) as fh:
            for raw in io.TextIOWrapper(fh):
                first = raw.split(",")[0]
                if first.isdigit():
                    ot = int(first)
                    return ot // 1000 if ot >= 10**14 else ot
    return None


def first_kline_many(symbols, workers=12):
    """Parallel first-kline lookup: {symbol: ms or None}."""
    results = {}

    def job(sym):
        s = _session()
        try:
            return sym, first_kline_ms(s, sym)
        except Exception:
            return sym, None

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(job, s) for s in symbols]
        for f in as_completed(futs):
            sym, ms = f.result()
            results[sym] = ms
    return results
