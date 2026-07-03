"""Scrape the Binance announcement CMS ("New Cryptocurrency Listing",
catalogId=48) and parse spot-listing announcements out of it.

The catalog mixes spot listings with futures launches, margin/earn additions
etc., so parsing is pattern-based and every title is classified, never
guessed. We extract (ticker, announcement_time, type) tuples; matching to
actual tradeable events happens in events.py against first-kline timestamps.
"""
import re
import time

import requests

from . import config, storage

# Titles that announce a SPOT listing (or a program that culminates in one,
# with the listing date inside the same announcement).
RE_WILL_LIST = re.compile(r"^\s*Binance Will List\b", re.I)
RE_LAUNCHPOOL = re.compile(r"^\s*Introducing\s+.+ on Binance Launchpool\b", re.I)
RE_HODLER = re.compile(r"^\s*Introducing\s+.+ on (?:the )?Binance HODLer Airdrops?\b", re.I)
RE_LAUNCHPAD = re.compile(r"^\s*Introducing\s+.+ on Binance Launchpad\b", re.I)
RE_MEGADROP = re.compile(r"^\s*Introducing\s+.+ on Binance Megadrop\b", re.I)
# Older title style, used until ~mid-2020: "Binance Lists Tezos (XTZ)",
# "Binance ... and Lists Swipe (SXP)". Published AT listing time (not in
# advance) — still a valid announcement anchor for coverage stats.
RE_LISTS = re.compile(r"^\s*Binance\b.*\bLists\b", re.I)

# Titles that are definitely NOT spot listing events.
RE_NOT_SPOT = re.compile(
    r"Futures|Perpetual|Margin|Options|Simple Earn|Dual Investment|"
    r"Binance Will Add|Binance Adds|Auto-Invest|Convert|Copy Trading|"
    r"Pre-Market|Binance Alpha|Leveraged Token|delist",
    re.I,
)

# Mixed case allowed ("XAUt") — base assets are uppercased for matching.
RE_TICKER = re.compile(r"\(([A-Z0-9][A-Za-z0-9]{1,14})\)")


def classify(title: str):
    """Return announcement type or None if this is not a spot listing."""
    # Anchored positive patterns first: "Binance Will List ..." and
    # "Introducing X on Binance <program>" are unambiguous spot-listing
    # announcements even when the title also mentions Simple Earn / futures
    # availability for the same token.
    if RE_WILL_LIST.search(title):
        return "will_list"
    if RE_LAUNCHPOOL.search(title):
        return "launchpool"
    if RE_HODLER.search(title):
        return "hodler_airdrop"
    if RE_LAUNCHPAD.search(title):
        return "launchpad"
    if RE_MEGADROP.search(title):
        return "megadrop"
    if RE_NOT_SPOT.search(title):
        return None
    if RE_LISTS.search(title):
        return "lists"
    return None


def extract_tickers(title: str):
    return [t.upper() for t in RE_TICKER.findall(title) if not t.isdigit()]


def fetch_all(conn, session: requests.Session = None, max_pages: int = 200):
    """Paginate the CMS catalog and store every article row (append-only raw)."""
    sess = session or requests.Session()
    sess.headers.update({"User-Agent": config.USER_AGENT})
    page = 1
    total_stored = 0
    total_expected = None
    while page <= max_pages:
        params = {
            "type": 1,
            "pageNo": page,
            "pageSize": config.CMS_PAGE_SIZE,
            "catalogId": config.CMS_CATALOG_ID,
        }
        for attempt in range(5):
            try:
                r = sess.get(config.CMS_URL, params=params, timeout=30)
                r.raise_for_status()
                payload = r.json()
                break
            except Exception:
                if attempt == 4:
                    raise
                time.sleep(2 ** attempt)
        catalogs = payload.get("data", {}).get("catalogs") or []
        if not catalogs:
            break
        cat = catalogs[0]
        total_expected = cat.get("total", total_expected)
        articles = cat.get("articles") or []
        if not articles:
            break
        total_stored += storage.insert_announcements(
            conn, articles, config.CMS_CATALOG_ID
        )
        # A missing 'total' must NOT truncate the crawl to one page; fall
        # back to paging until a short page.
        if total_expected is not None and page * config.CMS_PAGE_SIZE >= total_expected:
            break
        if total_expected is None and len(articles) < config.CMS_PAGE_SIZE:
            break
        page += 1
        time.sleep(0.35)  # polite pacing on a public CMS endpoint
    storage.log_step(
        conn, "announcements_fetch",
        f"pages={page} stored={total_stored} expected={total_expected}",
    )
    return total_stored, total_expected


def parsed_listing_announcements(conn):
    """Yield dicts {ticker, time, title, type} for spot-listing announcements."""
    out = []
    for title, release_time in conn.execute(
        "SELECT title, release_time FROM announcements ORDER BY release_time"
    ):
        ann_type = classify(title)
        if not ann_type:
            continue
        for ticker in extract_tickers(title):
            out.append(
                {"ticker": ticker, "time": release_time, "title": title,
                 "type": ann_type}
            )
    return out
