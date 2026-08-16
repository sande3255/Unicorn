"""Background scheduler for auto-generated, timed crypto/commodity
markets. Runs in a daemon thread started at app boot; on each tick it:

  1. Makes sure each configured template has one open market live.
  2. Refreshes the current live price on every open auto-market.
  3. Resolves any auto-market whose close_time has passed, using the
     price at settlement vs. the strike price it opened at.

Uses its own sqlite3 connection (not Flask's request-scoped `g`), since it
runs outside any HTTP request.
"""
import datetime
import sqlite3
import threading
import time

from . import db, price_feed, external_markets, amm
from .resolution import apply_resolution

TICK_SECONDS = 15

# Import/resolution-sync against Kalshi & Polymarket runs on a slower cadence
# than the crypto/commodity price ticks — these are slow-moving real-world
# questions, not fast numeric feeds, so there's no need to hit their APIs
# every 15 seconds. 20 ticks * 15s = ~5 minutes.
EXTERNAL_SYNC_EVERY_N_TICKS = 20
EXTERNAL_LIQUIDITY_B = 150

# UNICORN's board is a fixed, curated roster of well-known American names —
# top US stocks, the US stock indices, a handful of major commodities and
# currency pairs, and the cryptocurrencies most Americans have actually
# heard of — each offered as a fast 5-minute and/or 15-minute "will it be
# above or below this price" market. No long tail of obscure altcoins, no
# slow-moving real-world event markets that take hours or days to settle
# (nobody's sitting around waiting on a baseball game). Every open slot on
# the board is a definitive, short-clock win-or-lose call. Kalshi/Polymarket
# real-world imports are disabled entirely (see EXTERNAL_IMPORT_MAX_OPEN_TOTAL)
# to keep the board 100% fast markets. This list is intentionally capped
# under 60 templates total — add/remove symbols here to keep it that way.
EXTERNAL_IMPORT_MAX_OPEN_TOTAL = 0

# Top 10 cryptocurrencies by name recognition among US traders — same list
# at both cadences (keeps price-feed calls to one per symbol per tick).
_CRYPTO = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT",
           "BNBUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT", "LTCUSDT"]
_CRYPTO_LABELS = {
    "BTCUSDT": "Bitcoin", "ETHUSDT": "Ethereum", "SOLUSDT": "Solana", "XRPUSDT": "XRP",
    "DOGEUSDT": "Dogecoin", "BNBUSDT": "BNB", "ADAUSDT": "Cardano", "AVAXUSDT": "Avalanche",
    "LINKUSDT": "Chainlink", "LTCUSDT": "Litecoin",
}
_FIVE_MIN_CRYPTO = _CRYPTO
_FIFTEEN_MIN_CRYPTO = _CRYPTO

# The four headline US stock indices.
_FIFTEEN_MIN_INDICES = [
    ("^GSPC", "S&P 500"), ("^IXIC", "Nasdaq Composite"),
    ("^DJI", "Dow Jones"), ("^RUT", "Russell 2000"),
]

# A handful of the most widely-recognized commodities and currency pairs —
# both already fully supported by price_feed.py's generic Yahoo Finance
# lookup (get_commodity_price/get_forex_price), just never wired into the
# board's roster until now. 15-min only: both move more slowly tick-to-tick
# than crypto, so a 5-min clock would mostly just recreate the same
# strike/settlement pair back to back.
_COMMODITIES = [("GC=F", "Gold"), ("SI=F", "Silver"), ("CL=F", "Crude Oil")]
_FOREX = [("EURUSD=X", "EUR/USD"), ("GBPUSD=X", "GBP/USD"), ("USDJPY=X", "USD/JPY")]

# Top 12 US mega-cap stocks, used for both the 5-min and 15-min stock
# boards (same symbol list both cadences, mirroring the crypto list above).
_STOCKS = ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AVGO",
           "JPM", "V", "NFLX", "DIS"]
_STOCK_LABELS = {
    "AAPL": "Apple", "MSFT": "Microsoft", "NVDA": "Nvidia", "AMZN": "Amazon",
    "GOOGL": "Alphabet (Google)", "META": "Meta", "TSLA": "Tesla", "AVGO": "Broadcom",
    "JPM": "JPMorgan Chase", "V": "Visa", "NFLX": "Netflix", "DIS": "Disney",
}

# The menu of recurring markets. Add/remove entries here to change what's
# offered — kept to a fixed, definitive roster (54 templates total: 12
# stocks x2 durations, 10 crypto x2 durations, 4 indices, 3 commodities,
# and 3 forex pairs, the last three all x1 duration).
AUTO_MARKET_CONFIGS = (
    [
        {"market_type": "stock", "symbol": sym, "label": _STOCK_LABELS[sym], "duration_minutes": 5,
         "category": "Stocks · 5 min", "b": 60}
        for sym in _STOCKS
    ] + [
        {"market_type": "stock", "symbol": sym, "label": _STOCK_LABELS[sym], "duration_minutes": 15,
         "category": "Stocks · 15 min", "b": 100}
        for sym in _STOCKS
    ] + [
        {"market_type": "crypto", "symbol": sym, "label": _CRYPTO_LABELS[sym], "duration_minutes": 5,
         "category": "Crypto · 5 min", "b": 60}
        for sym in _FIVE_MIN_CRYPTO
    ] + [
        {"market_type": "crypto", "symbol": sym, "label": _CRYPTO_LABELS[sym], "duration_minutes": 15,
         "category": "Crypto · 15 min", "b": 100}
        for sym in _FIFTEEN_MIN_CRYPTO
    ] + [
        {"market_type": "index", "symbol": sym, "label": label, "duration_minutes": 15,
         "category": "Indices · 15 min", "b": 120}
        for sym, label in _FIFTEEN_MIN_INDICES
    ] + [
        {"market_type": "commodity", "symbol": sym, "label": label, "duration_minutes": 15,
         "category": "Commodities · 15 min", "b": 100}
        for sym, label in _COMMODITIES
    ] + [
        {"market_type": "forex", "symbol": sym, "label": label, "duration_minutes": 15,
         "category": "Forex · 15 min", "b": 100}
        for sym, label in _FOREX
    ]
)


def _fmt_price(price: float) -> str:
    return f"${price:,.4f}" if price < 1 else f"${price:,.2f}"


def _get_conn():
    # Same timeout/WAL reasoning as db.get_db() — this connection is the
    # scheduler's own, long-lived and separate from Flask's per-request
    # ones, but it's writing to the same file, so it needs the same
    # tolerance for a concurrent writer elsewhere.
    conn = sqlite3.connect(db.DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def _make_question(cfg, price):
    return f"Will {cfg['label']} be above {_fmt_price(price)} in {cfg['duration_minutes']} minutes?"


def tick(conn, price_fetcher=price_feed.get_price, log=print):
    now = datetime.datetime.utcnow()
    # Cache one fetched price per (market_type, symbol) this tick, to avoid
    # redundant API calls when the same symbol backs multiple configs/rows.
    price_cache = {}

    def get_cached_price(market_type, symbol):
        key = (market_type, symbol)
        if key not in price_cache:
            price_cache[key] = price_fetcher(market_type, symbol)
        return price_cache[key]

    # 1 & 2: ensure a live instance per template, and refresh current_price.
    for cfg in AUTO_MARKET_CONFIGS:
        open_row = conn.execute(
            "SELECT * FROM markets WHERE is_auto = 1 AND market_type = ? AND symbol = ? "
            "AND duration_minutes = ? AND status = 'open' ORDER BY created_at DESC LIMIT 1",
            (cfg["market_type"], cfg["symbol"], cfg["duration_minutes"]),
        ).fetchone()

        try:
            live_price = get_cached_price(cfg["market_type"], cfg["symbol"])
        except price_feed.PriceFeedError as e:
            log(f"[scheduler] price fetch failed for {cfg['symbol']}: {e}")
            continue

        if open_row is None:
            close_time = (now + datetime.timedelta(minutes=cfg["duration_minutes"])).isoformat()
            question = _make_question(cfg, live_price)
            cur = conn.execute(
                "INSERT INTO markets (question, description, category, status, b, q_yes, q_no, "
                "close_time, is_auto, market_type, symbol, symbol_label, duration_minutes, "
                "strike_price, current_price) "
                "VALUES (?, ?, ?, 'open', ?, 0, 0, ?, 1, ?, ?, ?, ?, ?, ?)",
                (question, f"Auto-generated {cfg['label']} market, settles against the live "
                           f"{cfg['label']} price {cfg['duration_minutes']} minutes after open.",
                 cfg["category"], cfg["b"], close_time, cfg["market_type"], cfg["symbol"],
                 cfg["label"], cfg["duration_minutes"], live_price, live_price),
            )
            market_id = cur.lastrowid
            conn.execute("INSERT INTO price_points (market_id, price_yes) VALUES (?, 0.5)", (market_id,))
            conn.commit()
            log(f"[scheduler] opened market #{market_id}: {question}")
        else:
            conn.execute("UPDATE markets SET current_price = ? WHERE id = ?", (live_price, open_row["id"]))
            conn.commit()

    # 3: resolve anything past its close_time.
    due = conn.execute(
        "SELECT * FROM markets WHERE is_auto = 1 AND status = 'open' AND close_time <= ?",
        (now.isoformat(),),
    ).fetchall()
    for m in due:
        try:
            settlement_price = get_cached_price(m["market_type"], m["symbol"])
        except price_feed.PriceFeedError as e:
            log(f"[scheduler] settlement price fetch failed for market #{m['id']} ({m['symbol']}): {e}")
            continue
        outcome = "YES" if settlement_price > m["strike_price"] else "NO"
        result = apply_resolution(conn, m["id"], outcome, settlement_price=settlement_price)
        if result is not None:
            log(f"[scheduler] resolved market #{m['id']} as {outcome} "
                f"(strike {m['strike_price']:.2f} -> settlement {settlement_price:.2f})")


def _import_source(conn, source_name, listed_markets, budget, log=print):
    """Imports at most `budget` new markets from this source. Returns how
    many it actually imported, so the caller can debit a shared budget."""
    imported = 0
    for ext in listed_markets:
        if imported >= budget:
            break
        exists = conn.execute(
            "SELECT id FROM markets WHERE market_type = ? AND symbol = ? LIMIT 1",
            (source_name, ext["source_id"]),
        ).fetchone()
        if exists:
            continue  # already imported before (open, or previously resolved) — don't re-import
        prob = ext["prob_yes"]
        if prob is None or not (0.0 < prob < 1.0):
            continue
        q_yes, q_no = amm.seed_shares_for_price(prob, EXTERNAL_LIQUIDITY_B)
        description = (
            f"Imported from {source_name.capitalize()} at {prob*100:.0f}% YES. UNICORN is not "
            f"affiliated with {source_name.capitalize()}; this trades with UNICORN's own play money "
            f"and settles automatically once the real market resolves. "
            f"Original: {ext.get('source_url') or ''}"
        )
        cur = conn.execute(
            "INSERT INTO markets (question, description, category, status, b, q_yes, q_no, "
            "close_time, is_auto, market_type, symbol, symbol_label, strike_price, current_price, source_url) "
            "VALUES (?, ?, ?, 'open', ?, ?, ?, ?, 1, ?, ?, ?, NULL, ?, ?)",
            (ext["question"], description, ext.get("category") or source_name.capitalize(),
             EXTERNAL_LIQUIDITY_B, q_yes, q_no, ext.get("close_time"), source_name,
             ext["source_id"], source_name.capitalize(), prob, ext.get("source_url")),
        )
        market_id = cur.lastrowid
        conn.execute(
            "INSERT INTO price_points (market_id, price_yes) VALUES (?, ?)",
            (market_id, amm.price_yes(q_yes, q_no, EXTERNAL_LIQUIDITY_B)),
        )
        conn.commit()
        imported += 1
        log(f"[scheduler] imported {source_name} market #{market_id} at {prob*100:.0f}% YES: {ext['question']!r}")
    return imported


def _sync_open_imports(conn, source_name, checker, log=print):
    open_rows = conn.execute(
        "SELECT * FROM markets WHERE is_auto = 1 AND status = 'open' AND market_type = ?",
        (source_name,),
    ).fetchall()
    for row in open_rows:
        status = checker(row["symbol"])
        if status is None:
            continue  # network/parse failure this round; try again next sync
        if status["resolved"]:
            result = apply_resolution(conn, row["id"], status["outcome"],
                                       settlement_price=status.get("prob_yes"))
            if result is not None:
                log(f"[scheduler] auto-resolved imported {source_name} market #{row['id']} "
                    f"as {status['outcome']} (source has settled)")
        elif status.get("prob_yes") is not None:
            conn.execute("UPDATE markets SET current_price = ? WHERE id = ?",
                         (status["prob_yes"], row["id"]))
            conn.commit()


def sync_external_markets(
    conn,
    kalshi_fetcher=external_markets.fetch_kalshi_markets,
    polymarket_fetcher=external_markets.fetch_polymarket_markets,
    kalshi_checker=external_markets.check_kalshi_market,
    polymarket_checker=external_markets.check_polymarket_market,
    log=print,
):
    """Imports new open Kalshi/Polymarket markets as UNICORN markets seeded at
    the real current odds, and resolves already-imported UNICORN markets once
    their source market has settled for real."""
    if EXTERNAL_IMPORT_MAX_OPEN_TOTAL <= 0:
        # Real-world imports are switched off (see the comment on
        # EXTERNAL_IMPORT_MAX_OPEN_TOTAL) — still sync/resolve any imports
        # that were opened in a prior run before this cap was introduced,
        # but skip the network calls that would look for new ones.
        _sync_open_imports(conn, "kalshi", kalshi_checker, log=log)
        _sync_open_imports(conn, "polymarket", polymarket_checker, log=log)
        return
    try:
        kalshi_list = kalshi_fetcher(log=log)
    except Exception as e:  # noqa: BLE001
        log(f"[scheduler] Kalshi import failed: {e}")
        kalshi_list = []
    try:
        polymarket_list = polymarket_fetcher(log=log)
    except Exception as e:  # noqa: BLE001
        log(f"[scheduler] Polymarket import failed: {e}")
        polymarket_list = []

    currently_open = conn.execute(
        "SELECT COUNT(*) c FROM markets WHERE is_auto = 1 AND status = 'open' "
        "AND market_type IN ('kalshi', 'polymarket')"
    ).fetchone()["c"]
    budget = max(0, EXTERNAL_IMPORT_MAX_OPEN_TOTAL - currently_open)

    budget -= _import_source(conn, "kalshi", kalshi_list, budget, log=log)
    _import_source(conn, "polymarket", polymarket_list, budget, log=log)
    _sync_open_imports(conn, "kalshi", kalshi_checker, log=log)
    _sync_open_imports(conn, "polymarket", polymarket_checker, log=log)


def run_loop(interval_seconds=TICK_SECONDS, price_fetcher=price_feed.get_price, log=print, stop_event=None,
             sync_externals=sync_external_markets, external_sync_every=EXTERNAL_SYNC_EVERY_N_TICKS):
    conn = _get_conn()
    tick_count = 0
    while stop_event is None or not stop_event.is_set():
        try:
            tick(conn, price_fetcher=price_fetcher, log=log)
            if tick_count % external_sync_every == 0:
                sync_externals(conn, log=log)
        except Exception as e:  # noqa: BLE001 - keep the loop alive no matter what
            log(f"[scheduler] tick error: {e}")
        tick_count += 1
        (stop_event.wait(interval_seconds) if stop_event else time.sleep(interval_seconds))


def start_background_scheduler(app):
    """Start the scheduler in a daemon thread, once per real server
    process (Flask's debug reloader spawns a launcher + a worker process;
    only the worker should run it)."""
    import os
    if app.debug and os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        return None
    thread = threading.Thread(target=run_loop, daemon=True, name="predictmarket-scheduler")
    thread.start()
    return thread
