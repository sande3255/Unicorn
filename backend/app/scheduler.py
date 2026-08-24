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

from . import db, price_feed, external_markets, sports_feed, odds_feed, amm, surveillance
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

# Five geographically-spread major metros, 15-min only — same "will it be
# above this reading" fast format as everything else on the board, just
# against a live temperature instead of a live price. 15-min rather than
# 5-min: ambient temperature moves too slowly for a 5-minute clock to add a
# meaningfully different bet from the 15-minute one. Coordinates for each
# live in price_feed.WEATHER_CITIES, not here — this list only needs the
# short key + display label.
_WEATHER = [("nyc", "New York City"), ("la", "Los Angeles"), ("chi", "Chicago"),
            ("mia", "Miami"), ("den", "Denver")]

# The menu of recurring markets. Add/remove entries here to change what's
# offered — kept to a fixed, definitive roster (59 templates total: 12
# stocks x2 durations, 10 crypto x2 durations, 4 indices, 3 commodities,
# 3 forex pairs, and 5 weather cities, all but the first two x1 duration).
# Sports markets are NOT part of this fixed roster — see sports_tick()
# below, which opens/closes them dynamically around real live MLB games
# instead of on a fixed template+duration clock.
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
    ] + [
        {"market_type": "weather", "symbol": key, "label": label, "duration_minutes": 15,
         "category": "Weather · 15 min", "b": 40}
        for key, label in _WEATHER
    ]
)


def _fmt_price(price: float) -> str:
    return f"${price:,.4f}" if price < 1 else f"${price:,.2f}"


def _fmt_reading(cfg, price: float) -> str:
    """Same idea as _fmt_price but market_type-aware, since weather's
    "price" is a plain Fahrenheit reading rather than a dollar figure."""
    if cfg["market_type"] == "weather":
        return f"{price:.0f}°F"
    return _fmt_price(price)


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
    if cfg["market_type"] == "weather":
        return f"Will it be above {_fmt_reading(cfg, price)} in {cfg['label']} in {cfg['duration_minutes']} minutes?"
    return f"Will {cfg['label']} be above {_fmt_reading(cfg, price)} in {cfg['duration_minutes']} minutes?"


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
                           f"{cfg['label']} {'temperature' if cfg['market_type'] == 'weather' else 'price'} "
                           f"{cfg['duration_minutes']} minutes after open.",
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


# Sports markets are opened/closed around real live half-innings rather
# than a fixed duration clock (see the module docstring in sports_feed.py),
# so this cap is the only thing keeping the board bounded during a busy
# slate of simultaneous MLB games — there are only ever ~15 games live at
# once league-wide, so this is rarely the binding constraint in practice.
SPORTS_MAX_OPEN = 20

_ORDINALS = {1: "1st", 2: "2nd", 3: "3rd"}


def _ordinal(n: int) -> str:
    return _ORDINALS.get(n, f"{n}th")


def sports_tick(conn, games_fetcher=sports_feed.get_live_games,
                 status_fetcher=sports_feed.get_inning_status, log=print):
    """Auto-resolves any open sports markets whose half-inning has been
    decided (a run scored, or it ended scoreless), then opens a fresh
    market for any live half-inning that doesn't have one yet. Every
    sports market's `symbol` column encodes "{game_pk}:{inning}:{half}" —
    the same "stash an external identifier in `symbol`" pattern
    kalshi/polymarket imports already use elsewhere in this file."""
    open_rows = conn.execute(
        "SELECT * FROM markets WHERE is_auto = 1 AND status = 'open' AND market_type = 'sports'"
    ).fetchall()
    for row in open_rows:
        try:
            game_pk_str, inning_str, half = row["symbol"].split(":")
            status = status_fetcher(int(game_pk_str), int(inning_str), half)
        except (ValueError, sports_feed.SportsFeedError) as e:
            log(f"[scheduler] sports status fetch failed for market #{row['id']}: {e}")
            continue
        if status is None:
            continue
        if status["runs"] > 0:
            result = apply_resolution(conn, row["id"], "YES", settlement_price=1.0)
            if result is not None:
                log(f"[scheduler] resolved sports market #{row['id']} as YES "
                    f"({status['runs']} run(s) scored)")
        elif status["concluded"]:
            result = apply_resolution(conn, row["id"], "NO", settlement_price=0.0)
            if result is not None:
                log(f"[scheduler] resolved sports market #{row['id']} as NO (half-inning ended scoreless)")

    currently_open = conn.execute(
        "SELECT COUNT(*) c FROM markets WHERE is_auto = 1 AND status = 'open' AND market_type = 'sports'"
    ).fetchone()["c"]
    if currently_open >= SPORTS_MAX_OPEN:
        return
    try:
        games = games_fetcher()
    except sports_feed.SportsFeedError as e:
        log(f"[scheduler] sports schedule fetch failed: {e}")
        return
    for game in games:
        if currently_open >= SPORTS_MAX_OPEN:
            break
        symbol = f"{game['game_pk']}:{game['inning']}:{game['half']}"
        exists = conn.execute(
            "SELECT id FROM markets WHERE market_type = 'sports' AND symbol = ? LIMIT 1", (symbol,)
        ).fetchone()
        if exists:
            continue  # already covered this half-inning before (open or resolved) — don't re-open it
        team = game["away_name"] if game["half"] == "Top" else game["home_name"]
        try:
            status = status_fetcher(game["game_pk"], game["inning"], game["half"])
        except sports_feed.SportsFeedError as e:
            log(f"[scheduler] sports status fetch failed opening market for {symbol}: {e}")
            continue
        if status is None or status["runs"] > 0 or status["concluded"]:
            continue  # already decided (or not enough of the feed yet) by the time we looked — skip
        half_word = "top" if game["half"] == "Top" else "bottom"
        question = f"Will the {team} score in the {half_word} of the {_ordinal(game['inning'])}?"
        b = 40
        cur = conn.execute(
            "INSERT INTO markets (question, description, category, status, b, q_yes, q_no, "
            "is_auto, market_type, symbol, symbol_label) "
            "VALUES (?, ?, 'Sports · Live', 'open', ?, 0, 0, 1, 'sports', ?, ?)",
            (question,
             f"Auto-generated live MLB market — resolves YES the moment the {team} score in this "
             f"half-inning, or NO once the half-inning ends scoreless. No fixed clock; settlement "
             f"just follows the real game.",
             b, symbol, team),
        )
        market_id = cur.lastrowid
        conn.execute("INSERT INTO price_points (market_id, price_yes) VALUES (?, 0.5)", (market_id,))
        conn.commit()
        currently_open += 1
        log(f"[scheduler] opened sports market #{market_id}: {question}")


# Needs ODDS_API_KEY (see odds_feed.py) — quietly does nothing if it isn't
# set, same as EXTERNAL_IMPORT_MAX_OPEN_TOTAL = 0 disabling Kalshi/
# Polymarket imports. Bounded like SPORTS_MAX_OPEN, for the same reason.
ODDS_MAX_OPEN = 20
ODDS_LIQUIDITY_B = 120


def odds_tick(conn, odds_fetcher=odds_feed.get_upcoming_odds, scores_fetcher=odds_feed.get_scores, log=print):
    """Auto-resolves open moneyline ('odds') markets once their game is
    final, then opens fresh ones for upcoming games at each sport's real,
    de-vigged moneyline probability. Every odds market's `symbol` column
    holds the odds-API event id — same "external id lives in `symbol`"
    convention as sports_tick()/kalshi/polymarket imports elsewhere in
    this file. Silently does nothing if ODDS_API_KEY isn't configured.

    This is the one feed in this app backed by a metered API — see the
    cadence comment on ODDS_SYNC_EVERY_N_TICKS below before tightening it."""
    if not odds_feed.ODDS_API_KEY:
        return

    open_rows = conn.execute(
        "SELECT * FROM markets WHERE is_auto = 1 AND status = 'open' AND market_type = 'odds'"
    ).fetchall()
    # Which sport a given open market belongs to isn't stored on the row
    # (no spare column for it), so rather than track that, just check
    # every configured sport's scores once per tick and match by event id.
    open_by_event = {row["symbol"]: row for row in open_rows}

    if open_by_event:
        for sport_key in odds_feed.SPORT_KEYS:
            try:
                scores = scores_fetcher(sport_key)
            except odds_feed.OddsFeedError as e:
                log(f"[scheduler] odds scores fetch failed for {sport_key}: {e}")
                continue
            for event_id, result in scores.items():
                row = open_by_event.get(event_id)
                if row is None or not result["completed"]:
                    continue
                if result["home_score"] is None or result["away_score"] is None:
                    continue  # completed but no final score posted yet — wait for next tick
                outcome = "YES" if result["home_score"] > result["away_score"] else "NO"
                res = apply_resolution(conn, row["id"], outcome,
                                        settlement_price=1.0 if outcome == "YES" else 0.0)
                if res is not None:
                    log(f"[scheduler] resolved odds market #{row['id']} as {outcome} "
                        f"({result['home_score']}-{result['away_score']})")

    currently_open = conn.execute(
        "SELECT COUNT(*) c FROM markets WHERE is_auto = 1 AND status = 'open' AND market_type = 'odds'"
    ).fetchone()["c"]
    if currently_open >= ODDS_MAX_OPEN:
        return
    for sport_key, league_label in odds_feed.SPORT_KEYS.items():
        if currently_open >= ODDS_MAX_OPEN:
            break
        try:
            games = odds_fetcher(sport_key)
        except odds_feed.OddsFeedError as e:
            log(f"[scheduler] odds fetch failed for {sport_key}: {e}")
            continue
        for game in games:
            if currently_open >= ODDS_MAX_OPEN:
                break
            exists = conn.execute(
                "SELECT id FROM markets WHERE market_type = 'odds' AND symbol = ? LIMIT 1",
                (game["event_id"],),
            ).fetchone()
            if exists:
                continue
            prob = game["home_prob"]
            if prob is None or not (0.01 < prob < 0.99):
                continue
            question = f"Will the {game['home_team']} beat the {game['away_team']}?"
            description = (
                f"{league_label} moneyline market, seeded at the real de-vigged implied "
                f"probability from live sportsbook odds. Resolves YES if the {game['home_team']} "
                f"win, NO otherwise — no fixed clock; settlement just follows the real final score."
            )
            q_yes, q_no = amm.seed_shares_for_price(prob, ODDS_LIQUIDITY_B)
            cur = conn.execute(
                "INSERT INTO markets (question, description, category, status, b, q_yes, q_no, "
                "is_auto, market_type, symbol, symbol_label) "
                "VALUES (?, ?, ?, 'open', ?, ?, ?, 1, 'odds', ?, ?)",
                (question, description, f"{league_label} · Moneyline", ODDS_LIQUIDITY_B,
                 q_yes, q_no, game["event_id"], game["home_team"]),
            )
            market_id = cur.lastrowid
            conn.execute(
                "INSERT INTO price_points (market_id, price_yes) VALUES (?, ?)",
                (market_id, amm.price_yes(q_yes, q_no, ODDS_LIQUIDITY_B)),
            )
            conn.commit()
            currently_open += 1
            log(f"[scheduler] opened odds market #{market_id} at {prob*100:.0f}% home-win: {question}")


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


# Sports polls MLB's live-game feed once per open market every cycle (on
# top of one schedule call), so this runs on its own slower cadence rather
# than every 15s tick — still fast enough that a scoring play shows up
# within half a minute. 2 ticks * 15s = ~30s.
SPORTS_SYNC_EVERY_N_TICKS = 2

# The Odds API bills per request and most plans (including the free tier)
# have a modest monthly cap, unlike every other feed here (CoinGecko/
# Yahoo/MLB Stats/Open-Meteo are all free and effectively uncapped) — so
# this runs far less often on purpose. Each run costs up to 4 sports x 2
# endpoints (odds + scores) = 8 requests; 240 ticks * 15s = ~1 hour between
# runs works out to roughly 190 requests/day worst case. If your plan's
# quota is tighter than that, raise this number — nothing else in the app
# depends on odds markets refreshing quickly.
ODDS_SYNC_EVERY_N_TICKS = 240

# Surveillance is pure read-then-maybe-insert against tables that already
# exist (transactions/markets/users) — no external API calls, so there's no
# cost reason to run it as rarely as the external/odds syncs above. 4 ticks
# * 15s = ~1 minute: frequent enough that a wash-trading or large-trade
# pattern gets flagged within about a minute of happening, without adding
# meaningful load re-scanning the same recent window every single tick.
SURVEILLANCE_SYNC_EVERY_N_TICKS = 4


def run_loop(interval_seconds=TICK_SECONDS, price_fetcher=price_feed.get_price, log=print, stop_event=None,
             sync_externals=sync_external_markets, external_sync_every=EXTERNAL_SYNC_EVERY_N_TICKS,
             sync_sports=sports_tick, sports_sync_every=SPORTS_SYNC_EVERY_N_TICKS,
             sync_odds=odds_tick, odds_sync_every=ODDS_SYNC_EVERY_N_TICKS,
             sync_surveillance=surveillance.run_surveillance_scan,
             surveillance_sync_every=SURVEILLANCE_SYNC_EVERY_N_TICKS):
    conn = _get_conn()
    tick_count = 0
    while stop_event is None or not stop_event.is_set():
        try:
            tick(conn, price_fetcher=price_fetcher, log=log)
            if tick_count % external_sync_every == 0:
                sync_externals(conn, log=log)
            if tick_count % sports_sync_every == 0:
                sync_sports(conn, log=log)
            if tick_count % odds_sync_every == 0:
                sync_odds(conn, log=log)
            if tick_count % surveillance_sync_every == 0:
                sync_surveillance(conn, log=log)
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
