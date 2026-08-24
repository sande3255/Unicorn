"""
Market surveillance / anti-manipulation scanning.

This is UNICORN's answer to the single biggest gap flagged in the
licensing punch list: a platform seeking real CFTC registration has to
show it can actually detect manipulation and settlement-integrity
problems, not just settle contracts correctly. Nothing here blocks a
trade or resolves a market automatically — real regulators expect a
documented human review process, not full automation, so this module
only ever *flags* activity into surveillance_flags (see db.py) for an
admin to look at and resolve. It runs continuously (wired into
scheduler.py) against whatever trading volume exists today, including
play money, so the detection logic gets exercised for real long before
any of it matters for real money.

Every detector below is a heuristic, not a proof of wrongdoing — a flag
means "worth a human look," not "confirmed manipulation." Thresholds are
deliberately conservative (documented inline) to keep false positives
manageable; tune them once there's a real trading volume baseline to
tune against.
"""

import datetime
from collections import defaultdict


def _parse_ts(value):
    """Same dual-format tolerance as server.py's _parse_db_timestamp —
    SQLite CURRENT_TIMESTAMP strings and Python isoformat() output both
    show up in these tables depending on which code path wrote the row."""
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.datetime.strptime(value, fmt)
        except (ValueError, TypeError):
            continue
    return None


# ---------- detectors ----------
#
# Each detector returns a list of candidate flags:
#   {"flag_type": str, "market_id": int|None, "user_id": int|None,
#    "related_user_id": int|None, "detail": str, "severity": "info"|"warning"|"high"}
# run_surveillance_scan() below dedupes candidates against recently-opened
# flags before inserting anything.

WASH_TRADE_LOOKBACK_MINUTES = 60
WASH_TRADE_PAIR_WINDOW_MINUTES = 5
WASH_TRADE_MIN_OCCURRENCES = 3


def detect_wash_trading(conn, lookback_minutes=WASH_TRADE_LOOKBACK_MINUTES,
                         pair_window_minutes=WASH_TRADE_PAIR_WINDOW_MINUTES,
                         min_occurrences=WASH_TRADE_MIN_OCCURRENCES):
    """Flags pairs of distinct accounts that repeatedly take opposite
    sides of the same market within a few minutes of each other — the
    basic shape of wash trading (two accounts, possibly controlled by the
    same person, trading against each other to fake volume or move price
    without real opposing conviction)."""
    since = (datetime.datetime.utcnow() - datetime.timedelta(minutes=lookback_minutes)).isoformat()
    rows = conn.execute(
        "SELECT user_id, market_id, outcome, created_at FROM transactions "
        "WHERE type = 'trade' AND created_at >= ? ORDER BY market_id, created_at",
        (since,),
    ).fetchall()

    by_market = defaultdict(list)
    for r in rows:
        ts = _parse_ts(r["created_at"])
        if ts is not None:
            by_market[r["market_id"]].append((ts, r["user_id"], r["outcome"]))

    flags = []
    window = datetime.timedelta(minutes=pair_window_minutes)
    for market_id, trades in by_market.items():
        pair_counts = defaultdict(int)
        for i, (ts_a, user_a, outcome_a) in enumerate(trades):
            for ts_b, user_b, outcome_b in trades[i + 1:]:
                if ts_b - ts_a > window:
                    break  # trades sorted by time; nothing further can be in-window
                if user_a == user_b or outcome_a == outcome_b:
                    continue  # need two *different* accounts on *opposite* sides
                pair_key = tuple(sorted((user_a, user_b)))
                pair_counts[pair_key] += 1
        for (user_a, user_b), count in pair_counts.items():
            if count >= min_occurrences:
                flags.append({
                    "flag_type": "wash_trading",
                    "market_id": market_id,
                    "user_id": user_a,
                    "related_user_id": user_b,
                    "detail": f"{count} opposite-side trades between these two accounts in market #{market_id} "
                              f"within {pair_window_minutes}-minute windows over the last {lookback_minutes} minutes.",
                    "severity": "warning",
                })
    return flags


SELF_REVERSAL_WINDOW_MINUTES = 10
SELF_REVERSAL_MIN_REVERSALS = 3


def detect_rapid_self_reversal(conn, lookback_minutes=WASH_TRADE_LOOKBACK_MINUTES,
                                window_minutes=SELF_REVERSAL_WINDOW_MINUTES,
                                min_reversals=SELF_REVERSAL_MIN_REVERSALS):
    """Flags a single account rapidly flipping direction (buy-then-sell or
    sell-then-buy) in the same market's same outcome — a pattern
    consistent with trying to manufacture price movement rather than
    trading on genuine conviction. `shares` is signed (negative = sell,
    per trade() in server.py), so a sign flip between consecutive trades
    is a reversal."""
    since = (datetime.datetime.utcnow() - datetime.timedelta(minutes=lookback_minutes)).isoformat()
    rows = conn.execute(
        "SELECT user_id, market_id, outcome, shares, created_at FROM transactions "
        "WHERE type = 'trade' AND created_at >= ? ORDER BY user_id, market_id, outcome, created_at",
        (since,),
    ).fetchall()

    by_key = defaultdict(list)
    for r in rows:
        ts = _parse_ts(r["created_at"])
        if ts is not None:
            by_key[(r["user_id"], r["market_id"], r["outcome"])].append((ts, r["shares"]))

    flags = []
    window = datetime.timedelta(minutes=window_minutes)
    for (user_id, market_id, outcome), trades in by_key.items():
        reversals = 0
        for i in range(1, len(trades)):
            ts_prev, shares_prev = trades[i - 1]
            ts_cur, shares_cur = trades[i]
            if ts_cur - ts_prev <= window and (shares_prev > 0) != (shares_cur > 0):
                reversals += 1
        if reversals >= min_reversals:
            flags.append({
                "flag_type": "rapid_self_reversal",
                "market_id": market_id,
                "user_id": user_id,
                "related_user_id": None,
                "detail": f"{reversals} buy/sell direction flips on {outcome} in market #{market_id} "
                          f"within {window_minutes}-minute windows over the last {lookback_minutes} minutes.",
                "severity": "info",
            })
    return flags


LARGE_TRADE_WINDOW_MINUTES = 5
LARGE_TRADE_MULTIPLE_OF_MEDIAN = 5
LARGE_TRADE_MIN_MARKET_TRADES = 5  # need enough history for "median size" to mean anything


def detect_large_trade_near_settlement(conn, window_minutes=LARGE_TRADE_WINDOW_MINUTES,
                                        multiple_of_median=LARGE_TRADE_MULTIPLE_OF_MEDIAN,
                                        min_market_trades=LARGE_TRADE_MIN_MARKET_TRADES):
    """Flags a trade placed shortly before a market's close_time whose
    size is far larger than that market's typical trade — worth a human
    look even though today's market types settle against an external
    price/outcome rather than the market's own trading price, since the
    same heuristic matters the moment any market type settles by
    admin judgment or by a mechanism trading activity could influence."""
    now = datetime.datetime.utcnow()
    open_markets = conn.execute(
        "SELECT id, close_time FROM markets WHERE status = 'open' AND close_time IS NOT NULL"
    ).fetchall()

    flags = []
    for m in open_markets:
        close_time = _parse_ts(m["close_time"])
        if close_time is None:
            continue
        minutes_to_close = (close_time - now).total_seconds() / 60.0
        if not (0 <= minutes_to_close <= window_minutes):
            continue

        trades = conn.execute(
            "SELECT id, user_id, shares, created_at FROM transactions "
            "WHERE type = 'trade' AND market_id = ? ORDER BY created_at",
            (m["id"],),
        ).fetchall()
        if len(trades) < min_market_trades:
            continue

        sizes = sorted(abs(t["shares"]) for t in trades)
        median = sizes[len(sizes) // 2]
        if median <= 0:
            continue

        window_start = (close_time - datetime.timedelta(minutes=window_minutes)).isoformat()
        for t in trades:
            if t["created_at"] < window_start:
                continue
            if abs(t["shares"]) >= median * multiple_of_median:
                flags.append({
                    "flag_type": "large_trade_near_settlement",
                    "market_id": m["id"],
                    "user_id": t["user_id"],
                    "related_user_id": None,
                    "detail": f"Trade of {abs(t['shares']):.1f} shares placed within {window_minutes} minutes "
                              f"of settlement — {multiple_of_median}x+ this market's median trade size "
                              f"({median:.1f} shares).",
                    "severity": "warning",
                })
    return flags


COORDINATED_ACCOUNTS_WINDOW_MINUTES = 30
COORDINATED_ACCOUNTS_MIN_COUNT = 3


def detect_coordinated_new_accounts(conn, window_minutes=COORDINATED_ACCOUNTS_WINDOW_MINUTES,
                                     min_count=COORDINATED_ACCOUNTS_MIN_COUNT):
    """Flags several newly-created accounts (created close together in
    time) all trading the same market within a short window of each other
    — a proxy for one person operating multiple accounts, since the app
    doesn't currently track IP/device fingerprints to detect that more
    directly."""
    lookback = (datetime.datetime.utcnow() - datetime.timedelta(minutes=window_minutes * 4)).isoformat()
    rows = conn.execute(
        "SELECT transactions.user_id, transactions.market_id, transactions.created_at AS traded_at, "
        "users.created_at AS account_created_at "
        "FROM transactions JOIN users ON users.id = transactions.user_id "
        "WHERE transactions.type = 'trade' AND transactions.created_at >= ? "
        "AND users.created_at >= ?",
        (lookback, lookback),
    ).fetchall()

    by_market = defaultdict(list)
    for r in rows:
        traded_at = _parse_ts(r["traded_at"])
        account_created_at = _parse_ts(r["account_created_at"])
        if traded_at is not None and account_created_at is not None:
            by_market[r["market_id"]].append((r["user_id"], traded_at, account_created_at))

    window = datetime.timedelta(minutes=window_minutes)
    flags = []
    for market_id, entries in by_market.items():
        seen_users = {}
        for user_id, traded_at, account_created_at in entries:
            seen_users[user_id] = (traded_at, account_created_at)  # last trade per user is enough for this heuristic
        users = list(seen_users.items())
        for i, (user_a, (traded_a, created_a)) in enumerate(users):
            cluster = {user_a}
            for user_b, (traded_b, created_b) in users[i + 1:]:
                if abs((created_b - created_a).total_seconds()) <= window.total_seconds() and \
                   abs((traded_b - traded_a).total_seconds()) <= window.total_seconds():
                    cluster.add(user_b)
            if len(cluster) >= min_count:
                flags.append({
                    "flag_type": "coordinated_new_accounts",
                    "market_id": market_id,
                    "user_id": user_a,
                    "related_user_id": None,
                    "detail": f"{len(cluster)} accounts created within {window_minutes} minutes of each other "
                              f"all traded market #{market_id} within {window_minutes} minutes of each other. "
                              f"Account ids: {sorted(cluster)}.",
                    "severity": "info",
                })
    return flags


DETECTORS = [
    detect_wash_trading,
    detect_rapid_self_reversal,
    detect_large_trade_near_settlement,
    detect_coordinated_new_accounts,
]

# Don't re-flag the same (flag_type, market_id, user_id, related_user_id)
# combination while an open flag for it already exists — a detector that
# re-runs every tick would otherwise spam identical rows.
DEDUPE_COOLDOWN_HOURS = 24


def run_surveillance_scan(conn, detectors=DETECTORS, log=print):
    """Runs every detector and inserts any genuinely-new flags. Safe to
    call repeatedly (e.g. every scheduler tick) — existing open flags for
    the same combination suppress duplicate inserts. Returns the number of
    new flags inserted."""
    cooldown_start = (datetime.datetime.utcnow() - datetime.timedelta(hours=DEDUPE_COOLDOWN_HOURS)).isoformat()
    inserted = 0
    for detector in detectors:
        try:
            candidates = detector(conn)
        except Exception as e:  # noqa: BLE001 - one bad detector shouldn't kill the scan
            log(f"[surveillance] {detector.__name__} failed: {e}")
            continue
        for flag in candidates:
            existing = conn.execute(
                "SELECT id FROM surveillance_flags WHERE flag_type = ? "
                "AND market_id IS ? AND user_id IS ? AND related_user_id IS ? "
                "AND status = 'open' AND created_at >= ?",
                (flag["flag_type"], flag["market_id"], flag["user_id"], flag["related_user_id"], cooldown_start),
            ).fetchone()
            if existing:
                continue
            conn.execute(
                "INSERT INTO surveillance_flags "
                "(flag_type, market_id, user_id, related_user_id, detail, severity) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (flag["flag_type"], flag["market_id"], flag["user_id"], flag["related_user_id"],
                 flag["detail"], flag["severity"]),
            )
            inserted += 1
    if inserted:
        conn.commit()
        log(f"[surveillance] {inserted} new flag(s) raised")
    return inserted
