"""Central configuration for the validation engine.

Every threshold that affects a verdict lives here, so a human can audit
exactly what bar a hypothesis had to clear. Changing these to make a GHOST
become REAL is the overfitting this project exists to prevent — see
ACCEPTANCE.md "Anti-optimism rules".
"""

# ---------------------------------------------------------------------------
# Data sources (read-only, public, no API keys — see PROJECT_BRIEF scope fence)
# ---------------------------------------------------------------------------
# api.binance.com is geo-blocked (HTTP 451) from this environment;
# data-api.binance.vision is Binance's official public market-data mirror of
# /api/v3/* and serves klines for delisted symbols too (verified empirically).
REST_BASE = "https://data-api.binance.vision"
# Announcement CMS (catalogId=48 = "New Cryptocurrency Listing")
CMS_URL = (
    "https://www.binance.com/bapi/apex/v1/public/apex/cms/article/list/query"
)
CMS_CATALOG_ID = 48
CMS_PAGE_SIZE = 50
# S3 bucket behind data.binance.vision — lists every symbol that EVER traded,
# including delisted ones (survivorship-complete universe source).
ARCHIVE_S3 = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
ARCHIVE_HTTP = "https://data.binance.vision"

DB_PATH = "data/validation.db"

# ---------------------------------------------------------------------------
# Event calendar
# ---------------------------------------------------------------------------
# Earliest event admitted. The CMS announcement catalog only reaches back to
# mid-2019, so events before this date would systematically lack
# announcement_time and pollute coverage stats. H-B keys off first_trade_time
# only, but we keep one clean window for all hypotheses.
EVENT_START_UTC = "2019-09-01T00:00:00Z"
# An event needs a complete +14d forward window; events younger than this
# margin are excluded (counted and reported, never silently dropped).
# Margin exceeds the 15.5d kline window so no in-progress candle can enter
# the raw store.
FORWARD_WINDOW_DAYS = 14
FORWARD_MARGIN_DAYS = 16

# Quote asset used to define "the" listing pair for an event.
PRIMARY_QUOTE = "USDT"

# Known quote assets, longest-first, for parsing archive-only (delisted)
# symbols where exchangeInfo can't tell us base/quote.
QUOTE_ASSETS = [
    "FDUSD", "USDT", "USDC", "TUSD", "BUSD", "BIDR", "IDRT", "AEUR", "USDP",
    "BVND", "USDS", "BKRW", "BRL", "EUR", "GBP", "TRY", "RUB", "UAH", "NGN",
    "ZAR", "PLN", "RON", "ARS", "MXN", "CZK", "COP", "JPY", "AUD", "DAI",
    "PAX", "BTC", "ETH", "BNB", "XRP", "TRX", "DOGE", "DOT", "SOL",
]

# Base assets that are never "new listing" events for our purposes:
# stables/pegged/wrapped/fiat (no listing-pump dynamics) and redenominations
# of already-trading tokens (LUNC/USTC are renamed LUNA/UST, BNSOL is staked
# SOL — their first USDT kline is not a listing event).
STABLE_OR_PEGGED_BASES = {
    "USDT", "USDC", "TUSD", "BUSD", "FDUSD", "DAI", "PAX", "USDP", "USDS",
    "SUSD", "EUR", "GBP", "AEUR", "TRY", "BRL", "WBTC", "WBETH", "BETH",
    "BTTC", "USD1", "USDE", "XUSD", "FDUSDT", "AUD", "UST", "VAI",
    "LUNC", "USTC", "BNSOL",
}

# Announcement window: an announcement matches an event if it falls in
# [first_trade - 30d, first_trade + 12h].
ANNOUNCE_MATCH_BEFORE_MS = 30 * 24 * 3600 * 1000
ANNOUNCE_MATCH_AFTER_MS = 12 * 3600 * 1000

# If a base asset traded on Binance (any pair) more than this long before its
# USDT pair opened, the USDT-pair start is a quote-pair addition, not a new
# listing event. Tight on purpose: H-B's entry grid starts at the FIRST
# CANDLE, which is meaningless if the token already traded on a BTC/BNB pair
# the day before (2019-2021 listings often staggered quote pairs).
QUOTE_ADDITION_TOLERANCE_HOURS = 12

# ---------------------------------------------------------------------------
# Kline windows (DATA_PIPELINE.md: 1m for tight event windows, 1h for the
# multi-day horizons)
# ---------------------------------------------------------------------------
KLINE_1M_HOURS = 49          # 1m bars from T0 .. T0+49h (covers +24h entry -> +24h horizon exit)
KLINE_1H_DAYS = 15.5         # 1h bars past T0+15d: the +24h|14d exit lands
                             # exactly AT +15d and needs a bar there, not
                             # a spurious force-exit one hour early

# ---------------------------------------------------------------------------
# H-B test grid (pre-declared; the FULL grid is always reported — ACCEPTANCE.md)
# ---------------------------------------------------------------------------
H = 3600 * 1000
ENTRY_OFFSETS_MS = {          # measured from first_trade_time
    "open": 0,
    "+1h": 1 * H,
    "+4h": 4 * H,
    "+24h": 24 * H,
}
HORIZONS_MS = {               # measured from entry time
    "1h": 1 * H,
    "4h": 4 * H,
    "24h": 24 * H,
    "3d": 72 * H,
    "7d": 168 * H,
    "14d": 336 * H,
}

# Pre-declared long-side mean-reversion exhibit (HYPOTHESIS.md H-B spot form:
# "possible post-dump reversion entry"). Fixed BEFORE seeing results; counted
# in the DSR trials total; reported as an exhibit, never tuned.
REVERSION_CONFIGS = [
    # (entry offset ms, min drawdown from post-listing high, holding ms)
    ("rev_24h_dd40_7d", 24 * H, 0.40, 168 * H),
    ("rev_24h_dd60_7d", 24 * H, 0.60, 168 * H),
    ("rev_72h_dd40_7d", 72 * H, 0.40, 168 * H),
    ("rev_72h_dd60_7d", 72 * H, 0.60, 168 * H),
]

# ---------------------------------------------------------------------------
# Friction model (Stage 5 inputs — but NET returns are used in EVERY stage;
# Stage 5 additionally reports gross-vs-net so a human sees friction's bite)
# ---------------------------------------------------------------------------
TAKER_FEE = 0.001            # Binance spot taker, VIP0, no BNB discount (conservative)
HALF_SPREAD_MIN = 0.0005     # 5 bps floor on half-spread + impact, 1m bars
HALF_SPREAD_RANGE_FRAC = 0.25  # + 25% of the fill bar's (high-low)/open
# Cap: listing first-minutes can have (high-low)/open > 4, which would push
# the adverse shift past 100% and flip fill-price signs (a corrupted fill,
# not a conservative one). A fill 50% worse than the decision price is the
# model's ceiling of adversity.
HALF_SPREAD_CAP = 0.50
HALF_SPREAD_LATE = 0.0010    # 10 bps when only 1h bars exist (3d+ exits) — conservative
MIN_BAR_QUOTE_VOLUME = 1000.0  # USDT; below this the bar is untradeable -> no fill

# ---------------------------------------------------------------------------
# Gauntlet thresholds (ACCEPTANCE.md hard blocks)
# ---------------------------------------------------------------------------
N_MIN_EVENTS = 30            # Stage 1 / hard block 1
TRAIN_FRACTION = 0.70        # Stage 1 in-sample portion
WF_N_FOLDS = 5               # Stage 2 chronological folds
# Purge: a train event whose forward window (entry +24h + horizon 14d) is
# still open when the test fold begins leaks test-period prices into cell
# selection (Lopez de Prado purged CV). Such events are dropped from train.
WF_PURGE_DAYS = 16
WF_MIN_TEST_TRAIN_SR_RATIO = 0.25  # test SR must retain at least this share of train SR
DSR_CONFIDENCE = 0.95        # Stage 3: deflated Sharpe must clear this probability
PBO_MAX = 0.50               # Stage 4 / hard block 4
PBO_N_BLOCKS = 12            # CSCV S (C(12,6)=924 splits)
MINTRL_CONFIDENCE = 0.95     # Stage 6
SHARPE_BENCHMARK = 0.0       # SR* reference for PSR/DSR/MinTRL

USER_AGENT = "listing-validator/1.0 (research; read-only public data)"
