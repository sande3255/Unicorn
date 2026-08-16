"""
Live price lookups for auto-generated markets, using only the standard
library (urllib) so no extra dependency (like `requests`) is required.

Crypto: CoinGecko's public "simple price" endpoint (no API key needed).
NOT Binance — api.binance.com blocks requests from US IP addresses (a
Binance policy; Binance.US is a legally separate entity/API), which
silently zeroed out every crypto market for any US-based user running
this app. CoinGecko doesn't have that restriction. One batched request
covers every crypto symbol this app tracks, refreshed on a short TTL
cache (see below) — much friendlier to CoinGecko's free-tier rate limit
than firing off a separate request per symbol per tick would be.
Commodities/stocks/indices/forex: Yahoo Finance's public chart endpoint
(no API key needed; this is an unofficial but widely-used endpoint — the
same one the popular `yfinance` library scrapes. It can occasionally
rate-limit or change shape; for anything beyond a demo, swap in a paid
data vendor instead.)
"""
import json
import time
import urllib.request
import urllib.error
import urllib.parse

USER_AGENT = "Mozilla/5.0 (compatible; UnicornDemo/1.0)"
TIMEOUT_SECONDS = 10

COINGECKO_URL = "https://api.coingecko.com/api/v3/simple/price?ids={ids}&vs_currencies=usd"
YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
# Open-Meteo: free, no API key, generous rate limits — current conditions
# only (no historical/forecast fields requested), keyed by lat/lon rather
# than a city-name lookup so there's no geocoding step or ambiguity.
OPEN_METEO_URL = (
    "https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
    "&current=temperature_2m&temperature_unit=fahrenheit&timezone=UTC"
)

# Internal symbol (e.g. "nyc") -> (display label, latitude, longitude). Same
# role as SYMBOL_TO_COINGECKO_ID below: the only place a new weather city
# needs to be taught its coordinates. Five geographically-spread major
# metros — enough variety that at least one is usually experiencing some
# weather movement, without turning this into a full city directory.
WEATHER_CITIES = {
    "nyc": ("New York City", 40.7128, -74.0060),
    "la": ("Los Angeles", 34.0522, -118.2437),
    "chi": ("Chicago", 41.8781, -87.6298),
    "mia": ("Miami", 25.7617, -80.1918),
    "den": ("Denver", 39.7392, -104.9903),
}

# Internal symbols (e.g. "BTCUSDT") stay in the Binance-style trading-pair
# format everywhere else in the app (DB rows, question text, category
# labels) since that's a familiar, compact convention — this table is the
# only place that needs to know the equivalent CoinGecko coin id. If a coin
# gets added to AUTO_MARKET_CONFIGS without an entry here, it just fails
# with a clear PriceFeedError (caught and logged by the scheduler) rather
# than crashing anything.
SYMBOL_TO_COINGECKO_ID = {
    "BTCUSDT": "bitcoin", "ETHUSDT": "ethereum", "SOLUSDT": "solana",
    "XRPUSDT": "ripple", "DOGEUSDT": "dogecoin", "BNBUSDT": "binancecoin",
    "ADAUSDT": "cardano", "AVAXUSDT": "avalanche-2", "LINKUSDT": "chainlink",
    "LTCUSDT": "litecoin", "DOTUSDT": "polkadot", "TRXUSDT": "tron",
    "ATOMUSDT": "cosmos", "UNIUSDT": "uniswap", "SHIBUSDT": "shiba-inu",
    "MATICUSDT": "matic-network", "NEARUSDT": "near", "APTUSDT": "aptos",
    "ARBUSDT": "arbitrum", "OPUSDT": "optimism", "INJUSDT": "injective-protocol",
    "FILUSDT": "filecoin", "ICPUSDT": "internet-computer", "ETCUSDT": "ethereum-classic",
    "XLMUSDT": "stellar",
}

# Short-TTL cache for the batched CoinGecko response, keyed by nothing (one
# global cache — this app only ever needs "current" prices for one fixed
# symbol set). Keeps repeated get_crypto_price() calls within the same
# scheduler tick (and across the next few seconds) from each firing a new
# request — one CoinGecko call roughly every tick instead of one per symbol.
_CRYPTO_CACHE_TTL_SECONDS = 12
_crypto_cache = {"fetched_at": 0.0, "prices": {}}


class PriceFeedError(Exception):
    pass


def _fetch_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
        raise PriceFeedError(f"Network error fetching {url}: {e}") from e
    except (json.JSONDecodeError, KeyError) as e:
        raise PriceFeedError(f"Unexpected response shape from {url}: {e}") from e


def _refresh_crypto_cache():
    ids = ",".join(sorted(set(SYMBOL_TO_COINGECKO_ID.values())))
    url = COINGECKO_URL.format(ids=urllib.parse.quote(ids, safe=","))
    data = _fetch_json(url)
    prices = {}
    for symbol, coingecko_id in SYMBOL_TO_COINGECKO_ID.items():
        entry = data.get(coingecko_id)
        if entry and "usd" in entry:
            prices[symbol] = float(entry["usd"])
    _crypto_cache["prices"] = prices
    _crypto_cache["fetched_at"] = time.monotonic()


def get_crypto_price(symbol: str) -> float:
    """symbol like 'BTCUSDT', 'ETHUSDT' — internal Binance-style trading
    pair name, translated to a CoinGecko coin id via SYMBOL_TO_COINGECKO_ID
    under the hood."""
    if symbol not in SYMBOL_TO_COINGECKO_ID:
        raise PriceFeedError(f"No CoinGecko id mapping for symbol {symbol}")
    stale = (time.monotonic() - _crypto_cache["fetched_at"]) > _CRYPTO_CACHE_TTL_SECONDS
    if stale or not _crypto_cache["prices"]:
        _refresh_crypto_cache()
    if symbol not in _crypto_cache["prices"]:
        raise PriceFeedError(f"CoinGecko response didn't include a USD price for {symbol}")
    return _crypto_cache["prices"][symbol]


def get_commodity_price(symbol: str) -> float:
    """symbol like 'GC=F' (gold futures), 'CL=F' (crude oil), 'SI=F' (silver)."""
    data = _fetch_json(YAHOO_URL.format(symbol=symbol))
    try:
        result = data["chart"]["result"][0]
        price = result["meta"]["regularMarketPrice"]
    except (KeyError, IndexError, TypeError) as e:
        raise PriceFeedError(f"Unexpected Yahoo Finance response for {symbol}: {data}") from e
    if price is None:
        raise PriceFeedError(f"No regularMarketPrice for {symbol}")
    return float(price)


def get_stock_price(symbol: str) -> float:
    """symbol like 'AAPL', 'MSFT' — same Yahoo Finance chart endpoint as
    commodities, since it's a generic quote lookup that works for any
    ticker Yahoo covers. Note: outside regular market hours this returns
    the last traded price (which may be a pre/post-market or prior-close
    print), not a live intraday tick — Yahoo's endpoint doesn't
    distinguish market-hours state in a way this demo checks."""
    return get_commodity_price(symbol)


def get_index_price(symbol: str) -> float:
    """symbol like '^GSPC' (S&P 500), '^IXIC' (Nasdaq Composite) — same
    generic Yahoo chart endpoint. Same outside-market-hours caveat as
    get_stock_price."""
    return get_commodity_price(symbol)


def get_forex_price(symbol: str) -> float:
    """symbol like 'EURUSD=X' — same generic Yahoo chart endpoint. Forex
    trades ~24/5 (closed weekends), so this is live most of the time
    unlike stocks/indices."""
    return get_commodity_price(symbol)


def get_weather_temp(city_key: str) -> float:
    """city_key like 'nyc' — internal short key, translated to lat/lon via
    WEATHER_CITIES. Returns current temperature in Fahrenheit."""
    if city_key not in WEATHER_CITIES:
        raise PriceFeedError(f"No coordinates mapping for weather city {city_key}")
    _, lat, lon = WEATHER_CITIES[city_key]
    data = _fetch_json(OPEN_METEO_URL.format(lat=lat, lon=lon))
    try:
        temp = data["current"]["temperature_2m"]
    except (KeyError, TypeError) as e:
        raise PriceFeedError(f"Unexpected Open-Meteo response for {city_key}: {data}") from e
    if temp is None:
        raise PriceFeedError(f"No temperature_2m for {city_key}")
    return float(temp)


def get_price(market_type: str, symbol: str) -> float:
    if market_type == "crypto":
        return get_crypto_price(symbol)
    if market_type == "commodity":
        return get_commodity_price(symbol)
    if market_type == "stock":
        return get_stock_price(symbol)
    if market_type == "index":
        return get_index_price(symbol)
    if market_type == "forex":
        return get_forex_price(symbol)
    if market_type == "weather":
        return get_weather_temp(symbol)
    raise PriceFeedError(f"Unknown market_type: {market_type}")
