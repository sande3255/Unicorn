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
    last_used_at TEXT DEFAULT CURRENT_TIMESTAMP,
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

-- One row per in-app notification (market resolved, achievement earned,
-- weekly challenge completed, referral bonus earned). Deliberately a flat
-- feed rather than typed sub-tables — `type` + a pre-rendered `message`
-- string is enough for a simple bell-icon dropdown, and it means every
-- new notification-worthy event just needs one INSERT, not a schema
-- change. See _notify() in server.py.
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    type TEXT NOT NULL,
    message TEXT NOT NULL,
    market_id INTEGER,
    is_read INTEGER NOT NULL DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id),
    FOREIGN KEY (market_id) REFERENCES markets(id)
);

-- A row per issued "forgot password" link. token_hash, not the plaintext
-- token, is stored — same reasoning as api_keys.key_hash (see
-- security.py's hash_api_key): a high-entropy random token nobody
-- memorizes, so a fast SHA-256 lookup is both secure and cheap to check.
-- used_at is set the moment the token is redeemed so it can never be
-- replayed even if someone captures the reset email in transit; expiry
-- (RESET_TOKEN_TTL_MINUTES in server.py) is enforced against created_at
-- at check time rather than a stored expires_at, same style as session
-- expiry.
CREATE TABLE IF NOT EXISTS password_reset_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    token_hash TEXT UNIQUE NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    used_at TEXT,
    FOREIGN KEY (user_id) REFERENCES users(id)
);

-- Real-money mode scaffolding (see backend/app/realmoney.py and README's
-- "Real-money mode" section). Inert today — nothing writes to these tables
-- unless UNICORN_REAL_MONEY_ENABLED is set, which it isn't anywhere by
-- default. One row per identity-verification attempt; a user can have more
-- than one if a rejection is followed by a resubmission, so this is a log,
-- not a single mutable field on users (kyc_status below is the derived
-- "current" state for fast checks).
CREATE TABLE IF NOT EXISTS kyc_verifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    legal_name TEXT NOT NULL,
    date_of_birth TEXT NOT NULL,
    address TEXT NOT NULL,
    state TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    provider TEXT NOT NULL DEFAULT 'manual',
    provider_reference TEXT,
    reviewed_by_user_id INTEGER,
    rejection_reason TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    reviewed_at TEXT,
    FOREIGN KEY (user_id) REFERENCES users(id),
    FOREIGN KEY (reviewed_by_user_id) REFERENCES users(id)
);

-- One row per real-money deposit/withdrawal attempt, separate from the
-- play-money `transactions` table on purpose — real cash needs its own
-- auditable trail (provider + provider_reference + status transitions)
-- that shouldn't be mixed in with daily-bonus and trade rows.
CREATE TABLE IF NOT EXISTS real_money_transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    type TEXT NOT NULL,
    amount REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    provider TEXT NOT NULL,
    provider_reference TEXT,
    real_balance_after REAL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    completed_at TEXT,
    FOREIGN KEY (user_id) REFERENCES users(id)
);

-- Generic compliance/audit trail — one row per event worth being able to
-- answer "who did what, when" about later (KYC submitted/approved/rejected,
-- real-money deposit/withdrawal attempted, admin actions on someone else's
-- account). Deliberately as flat as `notifications` (see that table's
-- comment above) for the same reason: one INSERT per event, no schema
-- change needed for a new event type.
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    actor_user_id INTEGER,
    event_type TEXT NOT NULL,
    detail TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id),
    FOREIGN KEY (actor_user_id) REFERENCES users(id)
);

-- Market surveillance / anti-manipulation flags (see backend/app/surveillance.py).
-- One row per candidate manipulation pattern a detector has surfaced;
-- nothing here is automatically acted on — an admin reviews each flag and
-- moves it out of 'open' via the review endpoints in server.py. Runs
-- against ordinary play-money trading today, not gated behind real-money
-- mode, since the whole point is to have this exercised and trustworthy
-- well before real money is involved.
CREATE TABLE IF NOT EXISTS surveillance_flags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    flag_type TEXT NOT NULL,
    market_id INTEGER,
    user_id INTEGER,
    related_user_id INTEGER,
    detail TEXT NOT NULL,
    severity TEXT NOT NULL DEFAULT 'info',
    status TEXT NOT NULL DEFAULT 'open',
    reviewed_by_user_id INTEGER,
    reviewed_at TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (market_id) REFERENCES markets(id),
    FOREIGN KEY (user_id) REFERENCES users(id),
    FOREIGN KEY (related_user_id) REFERENCES users(id),
    FOREIGN KEY (reviewed_by_user_id) REFERENCES users(id)
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
    # Optional, lowercased. Not collected at signup (signup is still just
    # username+password) — a user adds one later from the Account page if
    # they want password-reset-by-email to work for their account. NULL
    # here just means "forgot password" has nothing to send to; it's not
    # an error state. See POST /api/account/email and POST
    # /api/forgot-password in server.py.
    ("email", "TEXT"),
    # Derived "current" KYC state, kept in sync with the latest row in
    # kyc_verifications so every check that needs it (gating real-money
    # deposit/withdrawal, the account page) is a single indexed column read
    # instead of a subquery. The kyc_verifications table remains the source
    # of truth / audit trail; this is a cache of its latest status.
    ("kyc_status", "TEXT NOT NULL DEFAULT 'unverified'"),
    # Real-dollar balance, entirely separate from the existing play-money
    # `balance` column — deliberately never touched by any play-money code
    # path (daily bonus, referral bonus, demo deposit, trading) so the two
    # can never bleed into each other. Stays 0 for every account unless and
    # until real-money mode is enabled and a real deposit clears. See
    # realmoney.py and the real-money endpoints in server.py.
    ("real_balance", "REAL NOT NULL DEFAULT 0"),
]

TRANSACTION_COLUMN_MIGRATIONS = [
    # How much of a 'deposit' transaction's gross amount was kept as a
    # platform fee rather than credited to the user's balance. 0 for every
    # other transaction type. See the "deposit fee" section of server.py —
    # this is play-money only, simulating the mechanic, not real revenue.
    ("fee_amount", "REAL DEFAULT 0"),
]

SESSION_COLUMN_MIGRATIONS = [
    # Bumped on every authenticated request this token makes (best-effort,
    # same pattern as api_keys.last_used_at). Backs the idle-timeout half
    # of session expiry in get_current_user() (server.py) — a token that
    # hasn't been used in SESSION_IDLE_TIMEOUT_DAYS is treated as expired
    # even if it's well within the absolute lifetime.
    #
    # No DEFAULT CURRENT_TIMESTAMP here — SQLite's ALTER TABLE ADD COLUMN
    # only accepts constant defaults, not CURRENT_TIMESTAMP ("Cannot add a
    # column with non-constant default"). New rows get it explicitly via
    # the INSERT in signup()/login(); existing rows are backfilled from
    # created_at just below.
    ("last_used_at", "TEXT"),
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
    # Same partial-unique pattern as idx_users_wallet_address just above —
    # an email can only ever be attached to one account, but most rows
    # have NULL here (email is optional, added after signup), and a plain
    # UNIQUE column constraint would collide every NULL against every
    # other NULL.
    conn.execute("DROP INDEX IF EXISTS idx_users_email")
    conn.execute("CREATE UNIQUE INDEX idx_users_email ON users(email) WHERE email IS NOT NULL")

    existing_txn_cols = {row[1] for row in conn.execute("PRAGMA table_info(transactions)").fetchall()}
    for name, coltype in TRANSACTION_COLUMN_MIGRATIONS:
        if name not in existing_txn_cols:
            conn.execute(f"ALTER TABLE transactions ADD COLUMN {name} {coltype}")

    existing_session_cols = {row[1] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()}
    for name, coltype in SESSION_COLUMN_MIGRATIONS:
        if name not in existing_session_cols:
            conn.execute(f"ALTER TABLE sessions ADD COLUMN {name} {coltype}")
    # Backfill: every pre-existing session (and any inserted before this
    # column existed) starts the idle-timeout clock from its created_at
    # rather than NULL, so upgrading doesn't instantly expire everyone's
    # session on the next request.
    conn.execute("UPDATE sessions SET last_used_at = created_at WHERE last_used_at IS NULL")
    # get_current_user() looks up a session by token (already the primary
    # key, so that half is free) and, on every authenticated request,
    # deletes rows that fail the idle/absolute expiry check — an index on
    # last_used_at keeps a future batch cleanup job (or the expiry check
    # itself, if it's ever rewritten to scan instead of point-check) from
    # doing a full table scan once sessions accumulate.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_last_used ON sessions(last_used_at)")

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

    # GET /api/notifications orders a user's feed by created_at; the
    # partial unread index keeps "how many unread" (polled from the header
    # bell on every page) cheap even once a user has thousands of read
    # notifications piled up behind it.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_notifications_user_created "
        "ON notifications(user_id, created_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_notifications_user_unread "
        "ON notifications(user_id, is_read) WHERE is_read = 0"
    )

    # Admin KYC queue (GET /api/admin/kyc) filters by status; a user's own
    # KYC history (GET /api/kyc/status) filters by user_id.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_kyc_status ON kyc_verifications(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_kyc_user ON kyc_verifications(user_id, created_at)")

    # A user's real-money transaction history (GET /api/real-money/transactions)
    # filters and sorts by (user_id, created_at).
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_real_money_txn_user "
        "ON real_money_transactions(user_id, created_at)"
    )

    conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_log_user ON audit_log(user_id, created_at)")

    # run_surveillance_scan()'s dedupe check (surveillance.py) filters by
    # (flag_type, market_id, user_id, related_user_id, status, created_at)
    # on every detector hit; the admin queue (GET /api/admin/surveillance)
    # filters by status and sorts by created_at.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_surveillance_flags_dedupe "
        "ON surveillance_flags(flag_type, status, created_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_surveillance_flags_status_created "
        "ON surveillance_flags(status, created_at)"
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
