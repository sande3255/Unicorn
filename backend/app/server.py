import datetime
import mimetypes
import os
from functools import wraps

from flask import Flask, request, jsonify, g, send_from_directory

from . import db, amm, scheduler, wallet_auth
from .security import (
    hash_password, verify_password, new_token,
    generate_api_key, hash_api_key, API_KEY_PREFIX,
)
from .resolution import apply_resolution
from .ratelimit import rate_limit

# Flask/Werkzeug's send_from_directory guesses each file's Content-Type via
# Python's mimetypes module, which on Windows reads from the registry
# (HKEY_CLASSES_ROOT). That registry mapping is frequently broken or
# missing on real Windows machines (antivirus software and Windows updates
# both commonly mangle it) — when that happens, .js/.css/.html get served
# with the wrong (or a generic octet-stream) Content-Type, and browsers
# respond to that by downloading the file instead of rendering/running it,
# which looks exactly like "every link just downloads a file" from the
# outside. Registering these explicitly means the app never depends on
# whatever state that registry happens to be in on any given machine.
mimetypes.add_type("text/html", ".html")
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/css", ".css")

FRONTEND_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "frontend")

app = Flask(__name__, static_folder=None)
db.init_db(app)


def seed_admin():
    import sqlite3
    admin_username = os.environ.get("UNICORN_ADMIN_USERNAME", "admin")
    admin_password = os.environ.get("UNICORN_ADMIN_PASSWORD", "admin123")
    conn = sqlite3.connect(db.DB_PATH)
    conn.row_factory = sqlite3.Row
    existing = conn.execute("SELECT id FROM users WHERE username = ?", (admin_username,)).fetchone()
    if not existing:
        pw_hash = hash_password(admin_password)
        conn.execute(
            "INSERT INTO users (username, password_hash, balance, is_admin) VALUES (?, ?, ?, 1)",
            (admin_username, pw_hash, db.STARTING_BALANCE),
        )
        conn.commit()
        if admin_password == "admin123":
            print(f"Seeded default admin user -> username: {admin_username} / password: admin123 "
                  f"(CHANGE THIS — set UNICORN_ADMIN_PASSWORD before deploying anywhere public)")
        else:
            print(f"Seeded admin user -> username: {admin_username} (password set via UNICORN_ADMIN_PASSWORD)")
    conn.close()


seed_admin()
scheduler.start_background_scheduler(app)


def now_iso():
    return datetime.datetime.utcnow().isoformat()


# ---------- auth helpers ----------
#
# Two credential types share one Authorization header scheme (`Bearer
# <token>`): an interactive session token (from /api/login or /api/signup)
# or a long-lived API key (from /api/api-keys, meant for bots — see
# API.md). They're told apart by the API key's fixed prefix, so both can
# flow through the same require_auth-style decorators used everywhere
# else. get_current_user() additionally sets g.rate_limit_identity (used
# by ratelimit.py) and g.auth_method ("session" or "api_key") so callers
# can tell which one authenticated the request.

def get_current_user():
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    token = auth[len("Bearer "):].strip()
    conn = db.get_db()

    if token.startswith(API_KEY_PREFIX):
        key_hash = hash_api_key(token)
        row = conn.execute(
            "SELECT users.*, api_keys.id AS api_key_id, api_keys.can_trade AS api_key_can_trade "
            "FROM api_keys JOIN users ON users.id = api_keys.user_id "
            "WHERE api_keys.key_hash = ? AND api_keys.revoked_at IS NULL",
            (key_hash,),
        ).fetchone()
        if row is None:
            return None
        g.rate_limit_identity = f"apikey:{row['api_key_id']}"
        g.auth_method = "api_key"
        g.auth_can_trade = bool(row["api_key_can_trade"])
        try:
            # Best-effort visibility for the user's API Keys page — not on
            # the critical path, so a failure here shouldn't break auth.
            conn.execute("UPDATE api_keys SET last_used_at = ? WHERE id = ?", (now_iso(), row["api_key_id"]))
            conn.commit()
        except Exception:  # noqa: BLE001
            pass
        return row

    row = conn.execute(
        "SELECT users.* FROM sessions JOIN users ON users.id = sessions.user_id WHERE sessions.token = ?",
        (token,),
    ).fetchone()
    if row is not None:
        g.rate_limit_identity = f"user:{row['id']}"
        g.auth_method = "session"
        g.auth_can_trade = True
    return row


def require_auth(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        user = get_current_user()
        if user is None:
            return jsonify({"detail": "Not authenticated"}), 401
        g.user = user
        return f(*args, **kwargs)
    return wrapper


def require_session_auth(f):
    """Like require_auth, but rejects API-key credentials — for
    account-security-sensitive actions (managing the API keys themselves)
    that a leaked API key shouldn't be able to perform. A stolen key can
    trade with your play money; it shouldn't also be able to mint itself
    siblings or revoke your ability to notice it by deleting other keys."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        user = get_current_user()
        if user is None:
            return jsonify({"detail": "Not authenticated"}), 401
        if getattr(g, "auth_method", None) != "session":
            return jsonify({"detail": "This action requires a logged-in session, not an API key"}), 403
        g.user = user
        return f(*args, **kwargs)
    return wrapper


def require_admin(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        user = get_current_user()
        if user is None:
            return jsonify({"detail": "Not authenticated"}), 401
        if not user["is_admin"]:
            return jsonify({"detail": "Admin access required"}), 403
        g.user = user
        return f(*args, **kwargs)
    return wrapper


def optional_auth(f):
    """For public endpoints: doesn't reject unauthenticated calls, but if
    valid credentials are present, sets g.rate_limit_identity so rate
    limiting keys off the caller's account/API key instead of a shared IP
    bucket (useful when several bots run from the same server)."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        get_current_user()
        return f(*args, **kwargs)
    return wrapper


# ---------- serializers ----------

def market_price(m):
    if m["status"] == "resolved":
        return 1.0 if m["resolved_outcome"] == "YES" else 0.0
    return amm.price_yes(m["q_yes"], m["q_no"], m["b"])


def serialize_market(m, with_description=False, with_history=False, conn=None):
    keys = m.keys()
    out = {
        "id": m["id"],
        "question": m["question"],
        "category": m["category"],
        "status": m["status"],
        "resolved_outcome": m["resolved_outcome"],
        "price_yes": round(market_price(m), 4),
        "created_at": m["created_at"],
        "close_time": m["close_time"],
        "is_auto": bool(m["is_auto"]) if "is_auto" in keys else False,
        "market_type": m["market_type"] if "market_type" in keys else None,
        "symbol_label": m["symbol_label"] if "symbol_label" in keys else None,
        "duration_minutes": m["duration_minutes"] if "duration_minutes" in keys else None,
        "strike_price": m["strike_price"] if "strike_price" in keys else None,
        "current_price": m["current_price"] if "current_price" in keys else None,
        "settlement_price": m["settlement_price"] if "settlement_price" in keys else None,
        "source_url": m["source_url"] if "source_url" in keys else None,
    }
    if with_description:
        out["description"] = m["description"]
    if with_history and conn is not None:
        rows = conn.execute(
            "SELECT price_yes, created_at FROM price_points WHERE market_id = ? ORDER BY created_at ASC",
            (m["id"],),
        ).fetchall()
        out["price_history"] = [{"t": r["created_at"], "price": r["price_yes"]} for r in rows]
    return out


# ---------- auth routes ----------

@app.post("/api/signup")
def signup():
    data = request.get_json(force=True, silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    if len(username) < 3 or len(username) > 32:
        return jsonify({"detail": "Username must be 3-32 characters"}), 400
    if len(password) < 6:
        return jsonify({"detail": "Password must be at least 6 characters"}), 400

    conn = db.get_db()
    existing = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
    if existing:
        return jsonify({"detail": "Username already taken"}), 400

    pw_hash = hash_password(password)
    cur = conn.execute(
        "INSERT INTO users (username, password_hash, balance, is_admin) VALUES (?, ?, ?, 0)",
        (username, pw_hash, db.STARTING_BALANCE),
    )
    user_id = cur.lastrowid
    conn.execute(
        "INSERT INTO transactions (user_id, type, amount, balance_after) VALUES (?, 'signup_bonus', ?, ?)",
        (user_id, db.STARTING_BALANCE, db.STARTING_BALANCE),
    )
    token = new_token()
    conn.execute("INSERT INTO sessions (token, user_id) VALUES (?, ?)", (token, user_id))
    conn.commit()

    return jsonify({
        "token": token, "username": username, "balance": db.STARTING_BALANCE,
        "is_admin": False, "wallet_address": None,
    })


@app.post("/api/login")
def login():
    data = request.get_json(force=True, silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""

    conn = db.get_db()
    user = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    if not user or not verify_password(password, user["password_hash"]):
        return jsonify({"detail": "Invalid username or password"}), 401

    token = new_token()
    conn.execute("INSERT INTO sessions (token, user_id) VALUES (?, ?)", (token, user["id"]))
    conn.commit()
    return jsonify({
        "token": token, "username": user["username"],
        "balance": user["balance"], "is_admin": bool(user["is_admin"]),
        "wallet_address": user["wallet_address"],
    })


@app.get("/api/me")
@require_auth
def me():
    u = g.user
    claimable, _, streak = _daily_bonus_status(u)
    return jsonify({
        "username": u["username"], "balance": u["balance"], "is_admin": bool(u["is_admin"]),
        "wallet_address": u["wallet_address"] if "wallet_address" in u.keys() else None,
        "daily_streak": streak, "daily_bonus_claimable": claimable,
    })


# ---------- daily login bonus (play-money only, obviously) ----------
#
# A simple return-visit mechanic: claim once per COOLDOWN window for a
# small balance top-up, with the amount scaling up the longer an unbroken
# daily streak runs (capped so it doesn't run away forever). Streak state
# lives directly on the users row (last_daily_claim_at, daily_streak) —
# no separate table needed since there's only ever one "current" streak
# per account, not a history of them.

DAILY_BONUS_BASE = 25.0
DAILY_BONUS_PER_STREAK_DAY = 5.0
DAILY_BONUS_CAP = 100.0
# Must wait at least this long since the last claim before claiming again.
# Deliberately a bit under 24h (not exactly 24h) so a claim made a little
# earlier each day — someone who logs in at 8am one day and 7am the next —
# doesn't get blocked; a hard 24h:00m boundary would punish that.
DAILY_BONUS_COOLDOWN_HOURS = 20
# Claiming again within this many hours of the last claim keeps the streak
# alive; longer than this (i.e. a full day was skipped) resets it to 1.
DAILY_BONUS_STREAK_GRACE_HOURS = 48


def _daily_bonus_amount(streak):
    return min(DAILY_BONUS_CAP, DAILY_BONUS_BASE + DAILY_BONUS_PER_STREAK_DAY * max(streak - 1, 0))


def _daily_bonus_status(user):
    """Returns (claimable: bool, hours_until_claimable: float, current_streak: int).
    `user` just needs to be any row/dict with last_daily_claim_at + daily_streak —
    accepts both g.user (from an older cached session row) and a fresh SELECT."""
    keys = user.keys()
    last = user["last_daily_claim_at"] if "last_daily_claim_at" in keys else None
    streak = user["daily_streak"] if "daily_streak" in keys else 0
    if not last:
        return True, 0.0, streak
    elapsed_hours = (datetime.datetime.utcnow() - datetime.datetime.fromisoformat(last)).total_seconds() / 3600.0
    if elapsed_hours >= DAILY_BONUS_COOLDOWN_HOURS:
        return True, 0.0, streak
    return False, DAILY_BONUS_COOLDOWN_HOURS - elapsed_hours, streak


@app.get("/api/daily-bonus")
@require_auth
def daily_bonus_status():
    claimable, hours_left, streak = _daily_bonus_status(g.user)
    # What claiming right now would pay out (if claimable), or what the
    # *next* claim would pay assuming the streak survives (if not) — either
    # way it's "the amount attached to streak + 1", since a claim always
    # advances the streak by at least 1.
    return jsonify({
        "claimable": claimable,
        "hours_until_claimable": round(hours_left, 2),
        "current_streak": streak,
        "next_amount": _daily_bonus_amount(streak + 1),
    })


@app.post("/api/daily-bonus")
@require_auth
@rate_limit(10, 60)
def claim_daily_bonus():
    conn = db.get_db()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (g.user["id"],)).fetchone()
    claimable, hours_left, streak = _daily_bonus_status(user)
    if not claimable:
        return jsonify({
            "detail": f"Already claimed today's bonus — try again in about {hours_left:.1f}h.",
        }), 400

    last = user["last_daily_claim_at"] if "last_daily_claim_at" in user.keys() else None
    if last:
        gap_hours = (datetime.datetime.utcnow() - datetime.datetime.fromisoformat(last)).total_seconds() / 3600.0
        new_streak = streak + 1 if gap_hours <= DAILY_BONUS_STREAK_GRACE_HOURS else 1
    else:
        new_streak = 1

    amount = _daily_bonus_amount(new_streak)
    new_balance = round(user["balance"] + amount, 2)
    now = now_iso()
    conn.execute(
        "UPDATE users SET balance = ?, last_daily_claim_at = ?, daily_streak = ? WHERE id = ?",
        (new_balance, now, new_streak, user["id"]),
    )
    conn.execute(
        "INSERT INTO transactions (user_id, type, amount, balance_after) VALUES (?, 'daily_bonus', ?, ?)",
        (user["id"], amount, new_balance),
    )
    conn.commit()
    return jsonify({"balance": new_balance, "amount": amount, "streak": new_streak})


# ---------- wallet-connect login (no real crypto ever moves — see wallet_auth.py) ----------

@app.post("/api/wallet/nonce")
@rate_limit(20, 60)
def wallet_nonce():
    data = request.get_json(force=True, silent=True) or {}
    address = (data.get("address") or "").strip()
    if not wallet_auth.is_valid_address(address):
        return jsonify({"detail": "address must be a 0x-prefixed 40-hex-character Ethereum address"}), 400
    message = wallet_auth.create_challenge(address)
    return jsonify({"message": message})


@app.post("/api/wallet/link")
@require_session_auth
@rate_limit(20, 60)
def wallet_link():
    data = request.get_json(force=True, silent=True) or {}
    address = (data.get("address") or "").strip()
    signature = data.get("signature") or ""
    if not wallet_auth.is_valid_address(address):
        return jsonify({"detail": "address must be a 0x-prefixed 40-hex-character Ethereum address"}), 400

    conn = db.get_db()
    already = conn.execute(
        "SELECT id FROM users WHERE wallet_address = ? AND id != ?", (address.lower(), g.user["id"])
    ).fetchone()
    if already:
        return jsonify({"detail": "This wallet is already linked to a different UNICORN account"}), 400

    try:
        verified = wallet_auth.verify_and_consume(address, signature)
    except RuntimeError as e:
        return jsonify({"detail": str(e)}), 500
    if not verified:
        return jsonify({"detail": "Signature verification failed — request a fresh challenge and try again"}), 400

    conn.execute("UPDATE users SET wallet_address = ? WHERE id = ?", (address.lower(), g.user["id"]))
    conn.commit()
    return jsonify({"wallet_address": address.lower()})


@app.delete("/api/wallet")
@require_session_auth
def wallet_unlink():
    conn = db.get_db()
    conn.execute("UPDATE users SET wallet_address = NULL WHERE id = ?", (g.user["id"],))
    conn.commit()
    return jsonify({"wallet_address": None})


@app.post("/api/wallet/login")
@rate_limit(20, 60)
def wallet_login():
    data = request.get_json(force=True, silent=True) or {}
    address = (data.get("address") or "").strip()
    signature = data.get("signature") or ""
    if not wallet_auth.is_valid_address(address):
        return jsonify({"detail": "address must be a 0x-prefixed 40-hex-character Ethereum address"}), 400

    try:
        verified = wallet_auth.verify_and_consume(address, signature)
    except RuntimeError as e:
        return jsonify({"detail": str(e)}), 500
    if not verified:
        return jsonify({"detail": "Signature verification failed — request a fresh challenge and try again"}), 400

    conn = db.get_db()
    user = conn.execute("SELECT * FROM users WHERE wallet_address = ?", (address.lower(),)).fetchone()
    if user is None:
        return jsonify({
            "detail": "No UNICORN account is linked to this wallet yet. Log in normally and link "
                      "it from the Account page first.",
        }), 404

    token = new_token()
    conn.execute("INSERT INTO sessions (token, user_id) VALUES (?, ?)", (token, user["id"]))
    conn.commit()
    return jsonify({
        "token": token, "username": user["username"],
        "balance": user["balance"], "is_admin": bool(user["is_admin"]),
        "wallet_address": user["wallet_address"],
    })


# ---------- API keys (for bots — see API.md) ----------
#
# All three routes require an interactive session, not an API key
# (@require_session_auth) — otherwise a leaked key could mint itself
# siblings or revoke the very key a user would use to notice the leak.

def _serialize_api_key(row):
    return {
        "id": row["id"],
        "label": row["label"],
        "key_prefix": row["key_prefix"],
        "can_trade": bool(row["can_trade"]),
        "created_at": row["created_at"],
        "last_used_at": row["last_used_at"],
        "revoked_at": row["revoked_at"],
    }


@app.get("/api/api-keys")
@require_session_auth
def list_api_keys():
    conn = db.get_db()
    rows = conn.execute(
        "SELECT * FROM api_keys WHERE user_id = ? ORDER BY created_at DESC", (g.user["id"],)
    ).fetchall()
    return jsonify([_serialize_api_key(r) for r in rows])


@app.post("/api/api-keys")
@require_session_auth
def create_api_key():
    data = request.get_json(force=True, silent=True) or {}
    label = (data.get("label") or "").strip()[:64] or "Unnamed key"
    can_trade = bool(data.get("can_trade", True))

    plaintext, key_hash, display_prefix = generate_api_key()
    conn = db.get_db()
    cur = conn.execute(
        "INSERT INTO api_keys (user_id, label, key_hash, key_prefix, can_trade) VALUES (?, ?, ?, ?, ?)",
        (g.user["id"], label, key_hash, display_prefix, 1 if can_trade else 0),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM api_keys WHERE id = ?", (cur.lastrowid,)).fetchone()
    out = _serialize_api_key(row)
    # The only time the plaintext key is ever available — the hash is all
    # that's stored, so if this isn't copied down now it's gone for good.
    out["key"] = plaintext
    return jsonify(out)


@app.delete("/api/api-keys/<int:key_id>")
@require_session_auth
def revoke_api_key(key_id):
    conn = db.get_db()
    row = conn.execute(
        "SELECT * FROM api_keys WHERE id = ? AND user_id = ?", (key_id, g.user["id"])
    ).fetchone()
    if row is None:
        return jsonify({"detail": "API key not found"}), 404
    if row["revoked_at"] is None:
        conn.execute("UPDATE api_keys SET revoked_at = ? WHERE id = ?", (now_iso(), key_id))
        conn.commit()
    return jsonify({"revoked": True})


# ---------- deposits (play money — simulates the real-money fee mechanic) ----------
#
# UNICORN doesn't take real money (see README's "Before this can take real
# money"), so this can't actually charge anyone anything. What it *can* do
# is model the mechanic a real deposit flow would use — a user "deposits"
# an amount, a percentage + flat fee is taken off the top (the same shape
# most payment processors charge merchants, which is the natural place a
# real version of this would make money), and only the net lands in their
# play-money balance. Useful for prototyping the economics/UI before any
# of this touches real funds and real licensing requirements.

DEPOSIT_FEE_PCT = 0.03  # 3%
DEPOSIT_FEE_FLAT = 0.30  # + $0.30 flat, modeled after typical card-processor pricing
DEPOSIT_MIN_AMOUNT = 1.00
DEPOSIT_MAX_AMOUNT = 10_000.00  # sanity cap — it's play money, but unbounded numbers make for a silly demo


@app.post("/api/deposit")
@require_auth
@rate_limit(10, 60)
def deposit():
    data = request.get_json(force=True, silent=True) or {}
    try:
        amount = round(float(data.get("amount")), 2)
    except (TypeError, ValueError):
        return jsonify({"detail": "amount must be a number"}), 400
    if amount < DEPOSIT_MIN_AMOUNT or amount > DEPOSIT_MAX_AMOUNT:
        return jsonify({
            "detail": f"amount must be between ${DEPOSIT_MIN_AMOUNT:.2f} and ${DEPOSIT_MAX_AMOUNT:,.2f}",
        }), 400

    fee = round(amount * DEPOSIT_FEE_PCT + DEPOSIT_FEE_FLAT, 2)
    fee = min(fee, amount - 0.01)  # never let the fee eat the entire deposit
    net = round(amount - fee, 2)

    conn = db.get_db()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (g.user["id"],)).fetchone()
    new_balance = round(user["balance"] + net, 2)
    conn.execute("UPDATE users SET balance = ? WHERE id = ?", (new_balance, user["id"]))
    conn.execute(
        "INSERT INTO transactions (user_id, type, amount, fee_amount, balance_after) "
        "VALUES (?, 'deposit', ?, ?, ?)",
        (user["id"], net, fee, new_balance),
    )
    conn.commit()
    return jsonify({"balance": new_balance, "gross": amount, "fee": fee, "net": net})


@app.get("/api/admin/deposits-summary")
@require_admin
def deposits_summary():
    conn = db.get_db()
    row = conn.execute(
        "SELECT COUNT(*) AS c, COALESCE(SUM(amount), 0) AS net, COALESCE(SUM(fee_amount), 0) AS fees "
        "FROM transactions WHERE type = 'deposit'"
    ).fetchone()
    return jsonify({
        "deposit_count": row["c"],
        "total_net_credited": round(row["net"], 2),
        "total_fees_collected": round(row["fees"], 2),
        "total_gross": round(row["net"] + row["fees"], 2),
        "fee_pct": DEPOSIT_FEE_PCT,
        "fee_flat": DEPOSIT_FEE_FLAT,
    })


# ---------- market routes ----------

@app.get("/api/markets")
@optional_auth
@rate_limit(120, 60)
def list_markets():
    """Optional ?status=open or ?status=resolved narrows the result;
    omitting it returns every market ever created (unchanged default, so
    existing bots/scripts relying on "all markets" per API.md keep working).
    The frontend's own markets list defaults to ?status=open, since the
    timed-market roster churns every 5-15 minutes and an unfiltered fetch
    only grows without bound the longer an instance stays up."""
    conn = db.get_db()
    status = request.args.get("status")
    if status in ("open", "resolved"):
        rows = conn.execute(
            "SELECT * FROM markets WHERE status = ? ORDER BY created_at DESC", (status,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM markets ORDER BY created_at DESC").fetchall()
    return jsonify([serialize_market(m) for m in rows])


@app.post("/api/markets")
@require_admin
def create_market():
    data = request.get_json(force=True, silent=True) or {}
    question = (data.get("question") or "").strip()
    if len(question) < 5:
        return jsonify({"detail": "Question must be at least 5 characters"}), 400
    description = data.get("description") or ""
    category = data.get("category") or "General"
    liquidity_b = float(data.get("liquidity_b") or db.DEFAULT_LIQUIDITY_B)
    if liquidity_b <= 0:
        return jsonify({"detail": "liquidity_b must be positive"}), 400
    close_time = data.get("close_time")

    conn = db.get_db()
    cur = conn.execute(
        "INSERT INTO markets (question, description, category, creator_id, b, q_yes, q_no, close_time) "
        "VALUES (?, ?, ?, ?, ?, 0, 0, ?)",
        (question, description, category, g.user["id"], liquidity_b, close_time),
    )
    market_id = cur.lastrowid
    conn.execute(
        "INSERT INTO price_points (market_id, price_yes) VALUES (?, 0.5)", (market_id,)
    )
    conn.commit()
    m = conn.execute("SELECT * FROM markets WHERE id = ?", (market_id,)).fetchone()
    return jsonify(serialize_market(m))


@app.get("/api/markets/<int:market_id>")
@optional_auth
@rate_limit(120, 60)
def get_market(market_id):
    conn = db.get_db()
    m = conn.execute("SELECT * FROM markets WHERE id = ?", (market_id,)).fetchone()
    if not m:
        return jsonify({"detail": "Market not found"}), 404
    return jsonify(serialize_market(m, with_description=True, with_history=True, conn=conn))


@app.post("/api/markets/<int:market_id>/trade")
@require_auth
@rate_limit(30, 60)
def trade(market_id):
    if getattr(g, "auth_method", None) == "api_key" and not getattr(g, "auth_can_trade", True):
        return jsonify({"detail": "This API key is read-only and cannot place trades"}), 403

    data = request.get_json(force=True, silent=True) or {}
    outcome = data.get("outcome")
    try:
        shares = float(data.get("shares"))
    except (TypeError, ValueError):
        return jsonify({"detail": "shares must be a number"}), 400

    if outcome not in ("YES", "NO"):
        return jsonify({"detail": "outcome must be YES or NO"}), 400
    if shares == 0:
        return jsonify({"detail": "shares cannot be 0"}), 400

    conn = db.get_db()
    m = conn.execute("SELECT * FROM markets WHERE id = ?", (market_id,)).fetchone()
    if not m:
        return jsonify({"detail": "Market not found"}), 404
    if m["status"] != "open":
        return jsonify({"detail": "Market is not open for trading"}), 400

    user = conn.execute("SELECT * FROM users WHERE id = ?", (g.user["id"],)).fetchone()
    pos = conn.execute(
        "SELECT * FROM positions WHERE user_id = ? AND market_id = ?", (user["id"], market_id)
    ).fetchone()
    if pos is None:
        conn.execute(
            "INSERT INTO positions (user_id, market_id, shares_yes, shares_no) VALUES (?, ?, 0, 0)",
            (user["id"], market_id),
        )
        pos = conn.execute(
            "SELECT * FROM positions WHERE user_id = ? AND market_id = ?", (user["id"], market_id)
        ).fetchone()

    held = pos["shares_yes"] if outcome == "YES" else pos["shares_no"]
    if shares < 0 and abs(shares) > held + 1e-9:
        return jsonify({"detail": "Cannot sell more shares than you hold"}), 400

    cost = amm.trade_cost(m["q_yes"], m["q_no"], m["b"], outcome, shares)
    if cost > 0 and cost > user["balance"] + 1e-9:
        return jsonify({"detail": "Insufficient balance for this trade"}), 400

    new_q_yes = m["q_yes"] + shares if outcome == "YES" else m["q_yes"]
    new_q_no = m["q_no"] + shares if outcome == "NO" else m["q_no"]
    new_balance = user["balance"] - cost
    new_shares_yes = pos["shares_yes"] + (shares if outcome == "YES" else 0)
    new_shares_no = pos["shares_no"] + (shares if outcome == "NO" else 0)
    new_price = amm.price_yes(new_q_yes, new_q_no, m["b"])

    conn.execute("UPDATE markets SET q_yes = ?, q_no = ? WHERE id = ?", (new_q_yes, new_q_no, market_id))
    conn.execute("UPDATE users SET balance = ? WHERE id = ?", (new_balance, user["id"]))
    conn.execute(
        "UPDATE positions SET shares_yes = ?, shares_no = ? WHERE id = ?",
        (new_shares_yes, new_shares_no, pos["id"]),
    )
    conn.execute(
        "INSERT INTO transactions (user_id, market_id, type, outcome, shares, amount, balance_after) "
        "VALUES (?, ?, 'trade', ?, ?, ?, ?)",
        (user["id"], market_id, outcome, shares, -cost, new_balance),
    )
    conn.execute("INSERT INTO price_points (market_id, price_yes) VALUES (?, ?)", (market_id, new_price))
    # One-way flip, never cleared: the moment an account has ever placed a
    # trade over the API (bot credentials, not a logged-in session), it
    # counts as a "bot" for the public bot leaderboard from then on — see
    # is_bot_trader's migration comment in db.py.
    if getattr(g, "auth_method", None) == "api_key" and not user["is_bot_trader"]:
        conn.execute("UPDATE users SET is_bot_trader = 1 WHERE id = ?", (user["id"],))
    conn.commit()

    return jsonify({
        "balance": new_balance, "cost": cost, "price_yes": round(new_price, 4),
        "position_shares_yes": new_shares_yes, "position_shares_no": new_shares_no,
    })


@app.post("/api/markets/<int:market_id>/resolve")
@require_admin
def resolve_market(market_id):
    data = request.get_json(force=True, silent=True) or {}
    outcome = data.get("outcome")
    if outcome not in ("YES", "NO"):
        return jsonify({"detail": "outcome must be YES or NO"}), 400

    conn = db.get_db()
    existing = conn.execute("SELECT * FROM markets WHERE id = ?", (market_id,)).fetchone()
    if not existing:
        return jsonify({"detail": "Market not found"}), 404
    if existing["status"] == "resolved":
        return jsonify({"detail": "Market already resolved"}), 400

    m = apply_resolution(conn, market_id, outcome)
    return jsonify(serialize_market(m))


@app.get("/api/portfolio")
@require_auth
def portfolio():
    conn = db.get_db()
    rows = conn.execute(
        "SELECT positions.*, markets.question AS q_question, markets.status AS q_status, "
        "markets.q_yes AS q_qyes, markets.q_no AS q_qno, markets.b AS q_b, "
        "markets.resolved_outcome AS q_resolved_outcome "
        "FROM positions JOIN markets ON markets.id = positions.market_id "
        "WHERE positions.user_id = ?",
        (g.user["id"],),
    ).fetchall()
    out = []
    for r in rows:
        if abs(r["shares_yes"]) < 1e-9 and abs(r["shares_no"]) < 1e-9:
            continue
        if r["q_status"] == "resolved":
            price = 1.0 if r["q_resolved_outcome"] == "YES" else 0.0
        else:
            price = amm.price_yes(r["q_qyes"], r["q_qno"], r["q_b"])
        out.append({
            "market_id": r["market_id"], "question": r["q_question"], "status": r["q_status"],
            "price_yes": round(price, 4), "shares_yes": r["shares_yes"], "shares_no": r["shares_no"],
            "resolved_outcome": r["q_resolved_outcome"],
        })
    return jsonify(out)


BALANCE_HISTORY_MAX_POINTS = 200


@app.get("/api/portfolio/stats")
@require_auth
def portfolio_stats():
    """Realized performance stats — win rate, total P&L, biggest single-market
    win/loss — computed only from *resolved* markets, plus a balance-over-time
    series for a sparkline chart. Deliberately separate from /api/portfolio
    (which lists current open positions): this endpoint is about looking
    backward at what already happened, that one's about what's still live."""
    conn = db.get_db()
    rows = conn.execute(
        "SELECT transactions.market_id, transactions.amount, "
        "markets.question AS market_question, markets.status AS market_status "
        "FROM transactions JOIN markets ON markets.id = transactions.market_id "
        "WHERE transactions.user_id = ? AND transactions.type IN ('trade', 'payout')",
        (g.user["id"],),
    ).fetchall()

    # One entry per market this user ever traded that has since resolved —
    # net P&L is just every trade/payout amount for that market summed
    # (trade rows are already stored negative, as the cost paid; payout
    # rows are positive). A market still open contributes nothing here
    # since its outcome — and therefore whether it was a "win" — isn't
    # known yet.
    per_market = {}
    for r in rows:
        if r["market_status"] != "resolved":
            continue
        entry = per_market.setdefault(r["market_id"], {"question": r["market_question"], "pnl": 0.0})
        entry["pnl"] += r["amount"]

    wins = [m for m in per_market.values() if m["pnl"] > 1e-9]
    losses = [m for m in per_market.values() if m["pnl"] < -1e-9]
    decided = wins + losses  # exact-breakeven markets (rare) count toward neither
    win_rate = (len(wins) / len(decided)) if decided else None
    total_realized_pnl = sum(m["pnl"] for m in per_market.values())
    biggest_win = max(wins, key=lambda m: m["pnl"], default=None)
    biggest_loss = min(losses, key=lambda m: m["pnl"], default=None)

    balance_rows = conn.execute(
        "SELECT balance_after, created_at FROM transactions WHERE user_id = ? ORDER BY created_at ASC",
        (g.user["id"],),
    ).fetchall()
    # An active account can rack up thousands of transaction rows over
    # time; the chart just needs enough points to look right, not every
    # single one, so evenly downsample instead of shipping the whole log
    # over the wire on every portfolio page load.
    if len(balance_rows) > BALANCE_HISTORY_MAX_POINTS:
        step = len(balance_rows) / BALANCE_HISTORY_MAX_POINTS
        sampled = [balance_rows[int(i * step)] for i in range(BALANCE_HISTORY_MAX_POINTS)]
        if sampled[-1] is not balance_rows[-1]:
            sampled.append(balance_rows[-1])
    else:
        sampled = balance_rows

    return jsonify({
        "resolved_markets_traded": len(per_market),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(win_rate, 4) if win_rate is not None else None,
        "total_realized_pnl": round(total_realized_pnl, 2),
        "biggest_win": {"question": biggest_win["question"], "pnl": round(biggest_win["pnl"], 2)} if biggest_win else None,
        "biggest_loss": {"question": biggest_loss["question"], "pnl": round(biggest_loss["pnl"], 2)} if biggest_loss else None,
        "balance_history": [{"t": r["created_at"], "balance": r["balance_after"]} for r in sampled],
    })


@app.get("/api/leaderboard")
def leaderboard():
    """Optional ?board=humans or ?board=bots narrows to accounts that have
    (respectively) never or ever traded via an API key (see is_bot_trader).
    Omitting it keeps the original behavior — every non-admin account,
    human and bot alike — so existing bots/scripts polling this endpoint
    unchanged keep seeing what they've always seen."""
    board = request.args.get("board")
    conn = db.get_db()
    if board == "bots":
        users = conn.execute(
            "SELECT id, username, balance FROM users WHERE is_admin = 0 AND is_bot_trader = 1"
        ).fetchall()
    elif board == "humans":
        users = conn.execute(
            "SELECT id, username, balance FROM users WHERE is_admin = 0 AND is_bot_trader = 0"
        ).fetchall()
    else:
        users = conn.execute(
            "SELECT id, username, balance FROM users WHERE is_admin = 0"
        ).fetchall()
    pos_rows = conn.execute(
        "SELECT positions.user_id, positions.shares_yes, positions.shares_no, "
        "markets.status AS m_status, markets.resolved_outcome AS m_resolved_outcome, "
        "markets.q_yes AS m_qyes, markets.q_no AS m_qno, markets.b AS m_b "
        "FROM positions JOIN markets ON markets.id = positions.market_id"
    ).fetchall()

    position_value = {}
    for p in pos_rows:
        if p["m_status"] == "resolved":
            price = 1.0 if p["m_resolved_outcome"] == "YES" else 0.0
        else:
            price = amm.price_yes(p["m_qyes"], p["m_qno"], p["m_b"])
        value = p["shares_yes"] * price + p["shares_no"] * (1 - price)
        position_value[p["user_id"]] = position_value.get(p["user_id"], 0.0) + value

    ranked = []
    for u in users:
        net_worth = u["balance"] + position_value.get(u["id"], 0.0)
        ranked.append({
            "username": u["username"],
            "balance": round(u["balance"], 2),
            "net_worth": round(net_worth, 2),
        })
    ranked.sort(key=lambda r: r["net_worth"], reverse=True)
    for i, r in enumerate(ranked):
        r["rank"] = i + 1
    return jsonify(ranked[:100])


@app.get("/api/transactions")
@require_auth
def transactions():
    conn = db.get_db()
    rows = conn.execute(
        "SELECT transactions.*, markets.question AS market_question "
        "FROM transactions LEFT JOIN markets ON markets.id = transactions.market_id "
        "WHERE transactions.user_id = ? ORDER BY transactions.created_at DESC LIMIT 500",
        (g.user["id"],),
    ).fetchall()
    out = [{
        "id": r["id"],
        "type": r["type"],
        "outcome": r["outcome"],
        "shares": r["shares"],
        "amount": r["amount"],
        "fee_amount": r["fee_amount"] if "fee_amount" in r.keys() else 0,
        "balance_after": r["balance_after"],
        "created_at": r["created_at"],
        "market_id": r["market_id"],
        "market_question": r["market_question"],
    } for r in rows]
    return jsonify(out)


# ---------- comments (per-market discussion) ----------

COMMENT_MAX_LENGTH = 500


def _serialize_comment(row):
    return {
        "id": row["id"],
        "market_id": row["market_id"],
        "user_id": row["user_id"],
        "username": row["username"],
        "body": row["body"],
        "created_at": row["created_at"],
    }


@app.get("/api/markets/<int:market_id>/comments")
@rate_limit(120, 60)
def list_comments(market_id):
    conn = db.get_db()
    m = conn.execute("SELECT id FROM markets WHERE id = ?", (market_id,)).fetchone()
    if not m:
        return jsonify({"detail": "Market not found"}), 404
    rows = conn.execute(
        "SELECT comments.*, users.username FROM comments "
        "JOIN users ON users.id = comments.user_id "
        "WHERE comments.market_id = ? ORDER BY comments.created_at DESC LIMIT 200",
        (market_id,),
    ).fetchall()
    return jsonify([_serialize_comment(r) for r in rows])


@app.post("/api/markets/<int:market_id>/comments")
@require_auth
@rate_limit(10, 60)
def create_comment(market_id):
    data = request.get_json(force=True, silent=True) or {}
    body = (data.get("body") or "").strip()
    if not body:
        return jsonify({"detail": "Comment cannot be empty"}), 400
    if len(body) > COMMENT_MAX_LENGTH:
        return jsonify({"detail": f"Comment must be {COMMENT_MAX_LENGTH} characters or fewer"}), 400

    conn = db.get_db()
    m = conn.execute("SELECT id FROM markets WHERE id = ?", (market_id,)).fetchone()
    if not m:
        return jsonify({"detail": "Market not found"}), 404

    cur = conn.execute(
        "INSERT INTO comments (market_id, user_id, body) VALUES (?, ?, ?)",
        (market_id, g.user["id"], body),
    )
    conn.commit()
    row = conn.execute(
        "SELECT comments.*, users.username FROM comments JOIN users ON users.id = comments.user_id "
        "WHERE comments.id = ?",
        (cur.lastrowid,),
    ).fetchone()
    return jsonify(_serialize_comment(row))


@app.delete("/api/comments/<int:comment_id>")
@require_auth
def delete_comment(comment_id):
    conn = db.get_db()
    row = conn.execute("SELECT * FROM comments WHERE id = ?", (comment_id,)).fetchone()
    if row is None:
        return jsonify({"detail": "Comment not found"}), 404
    if row["user_id"] != g.user["id"] and not g.user["is_admin"]:
        return jsonify({"detail": "You can only delete your own comments"}), 403
    conn.execute("DELETE FROM comments WHERE id = ?", (comment_id,))
    conn.commit()
    return jsonify({"deleted": True})


@app.get("/api/activity")
@rate_limit(60, 60)
def activity():
    """Trade count in the trailing 60 seconds, site-wide — the only
    consumer today is the header's decorative sweep animation (see
    updateSweepSpeed() in app.js), which speeds up the faster trading is
    happening. Cheap enough to poll every few seconds: one indexed-ish
    COUNT over a small recent window, no joins."""
    conn = db.get_db()
    cutoff = (datetime.datetime.utcnow() - datetime.timedelta(seconds=60)).isoformat()
    row = conn.execute(
        "SELECT COUNT(*) c FROM transactions WHERE type = 'trade' AND created_at >= ?",
        (cutoff,),
    ).fetchone()
    return jsonify({"trades_last_60s": row["c"]})


@app.get("/api/health")
def health():
    return jsonify({"status": "ok"})


# ---------- serve frontend ----------

@app.get("/")
def index():
    return send_from_directory(FRONTEND_DIR, "index.html")


@app.get("/<path:path>")
def static_files(path):
    full = os.path.join(FRONTEND_DIR, path)
    if os.path.isfile(full):
        return send_from_directory(FRONTEND_DIR, path)
    return send_from_directory(FRONTEND_DIR, "index.html")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)
