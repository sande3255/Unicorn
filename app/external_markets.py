"""
Read-only imports from Kalshi's and Polymarket's public market-data APIs.

This does NOT place any real trades and is not connected to either
platform in any authenticated way — it only reads public question text,
current odds, and resolution status, then mirrors that as a normal UNICORN
market that UNICORN's own users trade against with UNICORN's own play money.
UNICORN is not affiliated with, endorsed by, or operated by Kalshi or
Polymarket; every imported market is labeled with its source and links
back to the original.

Both are public, unauthenticated, undocumented-for-our-purposes endpoints
that community tools commonly read from — they can change shape or start
requiring auth without notice. Every parse below is defensive: a market
that doesn't fit the expected shape is skipped and logged, not allowed to
crash the sync.

Uses only the standard library (urllib), same as price_feed.py.
"""
import json
import urllib.request
import urllib.error

USER_AGENT = "Mozilla/5.0 (compatible; UnicornDemo/1.0)"
TIMEOUT_SECONDS = 12

KALSHI_LIST_URL = "https://api.elections.kalshi.com/trade-api/v2/markets?status=open&limit={limit}"
KALSHI_MARKET_URL = "https://api.elections.kalshi.com/trade-api/v2/markets/{ticker}"
KALSHI_WEB_URL = "https://kalshi.com/markets/{ticker}"

POLYMARKET_LIST_URL = "https://gamma-api.polymarket.com/markets?active=true&closed=false&limit={limit}&order=volume24hr&ascending=false"
POLYMARKET_MARKET_URL = "https://gamma-api.polymarket.com/markets/{market_id}"
POLYMARKET_WEB_URL = "https://polymarket.com/event/{slug}"


class ExternalMarketError(Exception):
    pass


def _fetch_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
        raise ExternalMarketError(f"Network error fetching {url}: {e}") from e
    except json.JSONDecodeError as e:
        raise ExternalMarketError(f"Bad JSON from {url}: {e}") from e


# ---------- Kalshi ----------

def fetch_kalshi_markets(limit=15, log=print):
    """Returns a list of normalized dicts for currently-open Kalshi markets."""
    try:
        data = _fetch_json(KALSHI_LIST_URL.format(limit=limit))
    except ExternalMarketError as e:
        log(f"[external_markets] Kalshi list fetch failed: {e}")
        return []

    out = []
    for m in data.get("markets", []):
        try:
            ticker = m["ticker"]
            title = m.get("title") or m.get("subtitle") or ticker
            # Kalshi prices are in cents (0-100); prefer last_price, fall back to bid/ask midpoint.
            last = m.get("last_price")
            if last is None:
                bid, ask = m.get("yes_bid"), m.get("yes_ask")
                if bid is not None and ask is not None:
                    last = (bid + ask) / 2
            if last is None:
                continue
            prob_yes = max(0.0, min(1.0, float(last) / 100.0))
            out.append({
                "source": "kalshi",
                "source_id": ticker,
                "question": title,
                "category": m.get("category") or "Kalshi",
                "prob_yes": prob_yes,
                "close_time": m.get("close_time"),
                "source_url": KALSHI_WEB_URL.format(ticker=ticker),
            })
        except (KeyError, TypeError, ValueError) as e:
            log(f"[external_markets] skipping malformed Kalshi market: {e}")
            continue
    return out


def check_kalshi_market(ticker, log=print):
    """Returns {"resolved": bool, "outcome": "YES"/"NO"/None, "prob_yes": float}
    for a single previously-imported Kalshi market, or None on failure."""
    try:
        data = _fetch_json(KALSHI_MARKET_URL.format(ticker=ticker))
    except ExternalMarketError as e:
        log(f"[external_markets] Kalshi status check failed for {ticker}: {e}")
        return None
    m = data.get("market", data)
    status = m.get("status")
    result = (m.get("result") or "").lower()
    if status in ("finalized", "settled", "closed") and result in ("yes", "no"):
        return {"resolved": True, "outcome": result.upper(), "prob_yes": 1.0 if result == "yes" else 0.0}
    last = m.get("last_price")
    prob_yes = max(0.0, min(1.0, float(last) / 100.0)) if last is not None else None
    return {"resolved": False, "outcome": None, "prob_yes": prob_yes}


# ---------- Polymarket ----------

def fetch_polymarket_markets(limit=15, log=print):
    """Returns a list of normalized dicts for currently-open Polymarket markets."""
    try:
        data = _fetch_json(POLYMARKET_LIST_URL.format(limit=limit))
    except ExternalMarketError as e:
        log(f"[external_markets] Polymarket list fetch failed: {e}")
        return []

    out = []
    for m in data:
        try:
            market_id = m["id"]
            question = m.get("question") or m.get("title")
            if not question:
                continue
            prices = m.get("outcomePrices")
            if isinstance(prices, str):
                prices = json.loads(prices)
            if not prices:
                continue
            prob_yes = max(0.0, min(1.0, float(prices[0])))
            slug = m.get("slug") or m.get("eventSlug")
            out.append({
                "source": "polymarket",
                "source_id": str(market_id),
                "question": question,
                "category": (m.get("category") or "Polymarket"),
                "prob_yes": prob_yes,
                "close_time": m.get("endDate"),
                "source_url": POLYMARKET_WEB_URL.format(slug=slug) if slug else "https://polymarket.com",
            })
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as e:
            log(f"[external_markets] skipping malformed Polymarket market: {e}")
            continue
    return out


def check_polymarket_market(market_id, log=print):
    """Returns {"resolved": bool, "outcome": "YES"/"NO"/None, "prob_yes": float}
    for a single previously-imported Polymarket market, or None on failure."""
    try:
        data = _fetch_json(POLYMARKET_MARKET_URL.format(market_id=market_id))
    except ExternalMarketError as e:
        log(f"[external_markets] Polymarket status check failed for {market_id}: {e}")
        return None
    m = data[0] if isinstance(data, list) else data
    prices = m.get("outcomePrices")
    if isinstance(prices, str):
        try:
            prices = json.loads(prices)
        except json.JSONDecodeError:
            prices = None
    prob_yes = max(0.0, min(1.0, float(prices[0]))) if prices else None

    closed = bool(m.get("closed"))
    if closed and prob_yes is not None:
        # Only treat it as confidently resolved if the price has actually
        # converged near an extreme — otherwise wait and check again later
        # rather than guess.
        if prob_yes >= 0.98:
            return {"resolved": True, "outcome": "YES", "prob_yes": 1.0}
        if prob_yes <= 0.02:
            return {"resolved": True, "outcome": "NO", "prob_yes": 0.0}
    return {"resolved": False, "outcome": None, "prob_yes": prob_yes}
