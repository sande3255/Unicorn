import sqlite3
import os
from flask import g

# Overridable via UNICORN_DB_PATH so a production deploy (e.g. Railway) can
# point this at a mounted persistent volume instead of the container's
# ephemeral filesystem — see README's "Deploying to Railway" section.
DB_PATH = os.environ.get(
    "UNICORN_DB_PATH",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "predictmarket.db"),
)

STARTING_BALANCE = 10_000.0
DEFAULT_LIQUIDITY_B = 100.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    balance REAL NOT NULL DEFAULT 10000,
    is_admin INTEGER NOT NULL DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS markets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    question TEXT NOT NULL,
    description TEXT DEFAULT '',
    category TEXT DEFAULT 'General',
    creator_id INTEGER,
    status TEXT NOT NULL DEFAULT 'open',
    resolved_outcome TEXT,
    b REAL NOT NULL DEFAULT 100,
    q_yes REAL NOT NULL DEFAULT 0,
    q_no REAL NOT NULL DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    resolved_at TEXT,
    close_time TEXT,
    is_auto INTEGER NOT NULL DEFAULT 0,
    market_type TEXT,
    symbol TEXT,
    symbol_label TEXT,
    duration_minutes INTEGER,
    strike_price REAL,
    current_price REAL,
    settlement_price REAL,
    source_url TEXT,
    FOREIGN KEY (creator_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    market_id INTEGER NOT NULL,
    shares_yes REAL NOT NULL DEFAULT 0,
    shares_no REAL NOT NULL DEFAULT 0,
    UNIQUE(user_id, market_id),
    FOREIGN KEY (user_id) REFERENCES users(id),
    FOREIGN KEY (market_id) REFERENCES markets(id)
);

CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    market_id INTEGER,
    type TEXT NOT NULL,
    outcome TEXT,
    shares REAL DEFAULT 0,
    amount REAL DEFAULT 0,
    fee_amount REAL DEFAULT 0,
    balance_after REAL DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS price_points (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id INTEGER NOT NULL,
    price_yes REAL NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (market_id) REFERENCES markets(id)
);

CREATE TABLE IF NOT EXISTS api_keys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    key_hash TEXT UNIQUE NOT NULL,
    key_prefix TEXT NOT NULL,
    can_trade INTEGER NOT NULL DEFAULT 1,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    last_used_at TEXT,
    revoked_at TEXT,
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS user_achievements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    achievement_key TEXT NOT NULL,
    earned_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, achievement_key),
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS user_challenge_claims (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    challenge_key TEXT NOT NULL,
    week_key TEXT NOT NULL,
    claimed_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, challenge_key, week_key),
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    body TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (market_id) REFERENCES markets(id),
    FOREIGN KEY (user_id) REFERENCES users(id)
);
"""


def get_db():
    if "db" not in g:
        # timeout=10: wait up to 10s for a write lock to clear before raising
        # "database is locked", instead of sqlite3's 5s default — cheap
        # insurance under real concurrent traffic (gunicorn's --threads 8
        # means multiple requests can hit SQLite at once).
        g.db = sqlite3.connect(DB_PATH, timeout=10)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
        # WAL lets readers proceed without blocking on an in-progress
        # writer (SQLite's default rollback-journal mode locks the whole
        # file for the duration of a write) — the single biggest lever
        # against "database is locked" once more than one connection is
        # hitting this file at a time, which is exactly the situation a
        # live multi-threaded deployment (vs. a lone local dev process) is
        # in one hundred percent of the time. A no-op if already WAL from
        # a previous run (persists in the db file itself, not per-connection).
        g.db.execute("PRAGMA journal_mode = WAL")
    return g.db


def close_db(e=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


MARKET_COLUMN_MIGRATIONS = [
    ("is_auto", "INTEGER NOT NULL DEFAULT 0"),
    ("market_type", "TEXT"),
    ("symbol", "TEXT"),
    ("symbol_label", "TEXT"),
    ("duration_minutes", "INTEGER"),
    ("strike_price", "REAL"),
    ("current_price", "REAL"),
    ("settlement_price", "REAL"),
    ("source_url", "TEXT"),
]


USER_COLUMN_MIGRATIONS = [
    # Lowercased 0x-prefixed hex address, nullable — a user who hasn't
    # linked a wallet just has NULL here, same as before this column
    # existed. See app/wallet_auth.py for the link/login flow.
    ("wallet_address", "TEXT"),
    # Flipped to 1 the first time this account ever places a trade
    # authenticated via an API key rather than a session token (see
    # trade() in server.py). Drives the public bot leaderboard — an
    # account only ever needs to trade programmatically once to count as
    # a "bot" from then on, even if a human later also logs in and trades
    # the same account by hand.
    ("is_bot_trader", "INTEGER NOT NULL DEFAULT 0"),
    # ISO timestamp of this account's most recent daily-bonus claim, or
    # NULL if it's never claimed one. See claim_daily_bonus() in
    # server.py — both the "already claimed today" check and the streak
    # math key off comparing this to "now".
    ("last_daily_claim_at", "TEXT"),
    # Consecutive days (including today, once claimed) this account has
    # claimed the daily bonus without missing a day. Reset to 1 (not 0)
    # on any claim that follows a gap of more than ~48h since the last
    # one — see claim_daily_bonus().
    ("daily_streak", "INTEGER NOT NULL DEFAULT 0"),
    # user_id of the account whose referral link this account signed up
    # through, or NULL if they signed up unreferred / the referral code
    # they entered didn't match a real account. Set once at signup and
    # never changed afterward. See signup() and GET /api/referrals in
    # server.py.
    ("referred_by_user_id", "INTEGER"),
]

TRANSACTION_COLUMN_MIGRATIONS = [
    # How much of a 'deposit' transaction's gross amount was kept as a
    # platform fee rather than credited to the user's balance. 0 for every
    # other transaction type. See the "deposit fee" section of server.py —
    # this is play-money only, simulating the mechanic, not real revenue.
    ("fee_amount", "REAL DEFAULT 0"),
]


def _migrate(conn):
    """Add any new columns to existing tables (safe to re-run)."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(markets)").fetchall()}
    for name, coltype in MARKET_COLUMN_MIGRATIONS:
        if name not in existing:
            conn.execute(f"ALTER TABLE markets ADD COLUMN {name} {coltype}")
    # Only one open system-managed market per (market_type, symbol, duration)
    # at a time. duration_minutes distinguishes e.g. a BTCUSDT/5min market
    # from a BTCUSDT/15min market (both legitimately open at once); it's
    # always NULL for Kalshi/Polymarket imports, where COALESCE collapses
    # those to a consistent key so duplicate imports of the same ticker are
    # still blocked. DROP+CREATE (not IF NOT EXISTS) so an older, narrower
    # version of this index from a previous run gets replaced, not left in
    # place under the same name.
    conn.execute("DROP INDEX IF EXISTS idx_one_open_auto_market")
    conn.execute(
        "CREATE UNIQUE INDEX idx_one_open_auto_market "
        "ON markets(market_type, symbol, COALESCE(duration_minutes, -1)) "
        "WHERE is_auto = 1 AND status = 'open'"
    )

    existing_user_cols = {row[1] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
    for name, coltype in USER_COLUMN_MIGRATIONS:
        if name not in existing_user_cols:
            conn.execute(f"ALTER TABLE users ADD COLUMN {name} {coltype}")
    # A wallet address can only ever be linked to one account. Partial
    # index (not a plain UNIQUE column constraint) so multiple users with
    # no wallet linked — the common case — don't collide on NULL.
    conn.execute("DROP INDEX IF EXISTS idx_users_wallet_address")
    conn.execute(
        "CREATE UNIQUE INDEX idx_users_wallet_address ON users(wallet_address) "
        "WHERE wallet_address IS NOT NULL"
    )
    # GET /api/referrals looks up "who did this account refer" by this
    # column on every call.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_users_referred_by ON users(referred_by_user_id) "
        "WHERE referred_by_user_id IS NOT NULL"
    )

    existing_txn_cols = {row[1] for row in conn.execute("PRAGMA table_info(transactions)").fetchall()}
    for name, coltype in TRANSACTION_COLUMN_MIGRATIONS:
        if name not in existing_txn_cols:
            conn.execute(f"ALTER TABLE transactions ADD COLUMN {name} {coltype}")

    # Every comment list/insert filters or sorts by (market_id, created_at) —
    # see list_comments()/create_comment() in server.py.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_comments_market ON comments(market_id, created_at)")

    # activity() (server.py) counts recent 'trade' rows by created_at on
    # every poll (every few seconds, from every open tab) — worth an index
    # once there's any real transaction volume.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_transactions_type_created ON transactions(type, created_at)")

    # sync_challenges() (server.py) checks "has this user already claimed
    # this challenge this week" on every /api/challenges call.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_challenge_claims_user_week "
        "ON user_challenge_claims(user_id, week_key)"
    )

    conn.commit()


def init_db(app):
    with app.app_context():
        conn = sqlite3.connect(DB_PATH)
        conn.executescript(SCHEMA)
        conn.commit()
        _migrate(conn)
        conn.close()
    app.teardown_appcontext(close_db)
