import datetime
import mimetypes
import os
import re
from functools import wraps

from flask import Flask, request, jsonify, g, send_from_directory

from . import db, amm, scheduler, wallet_auth, email_feed, realmoney, surveillance
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

# ---------- CORS for /api/* ----------
#
# The web frontend never needed this — it's always served from the same
# Railway origin as the API, so its fetch() calls are same-origin and
# browsers don't apply CORS at all. Two things do need it: the native
# mobile wrapper (mobile/www/index.html sets UNICORN_API_BASE to the full
# Railway URL and calls it from inside the app's own WebView origin —
# capacitor://localhost on iOS, https://localhost on Android — which IS
# cross-origin from the API's point of view), and any third-party bot
# dashboard or browser-based tool someone builds against the public API
# documented in API.md.
#
# Wide open (Access-Control-Allow-Origin: *) is a deliberate, reasoned
# choice, not an oversight: every /api/ route authenticates via an
# `Authorization: Bearer <token>` header, never cookies, so there's no
# ambient-credential/CSRF angle here the way there would be for a
# cookie-session API — a page on another origin can't make an
# authenticated request against this API unless its own JS already has
# the caller's token in hand, which CORS does nothing to prevent or allow
# either way. No third-party dependency (e.g. flask-cors) needed for
# something this small.
CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, PUT, PATCH, DELETE, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, Authorization",
    "Access-Control-Max-Age": "86400",
}


@app.before_request
def handle_api_cors_preflight():
    if request.method == "OPTIONS" and request.path.startswith("/api/"):
        response = app.make_default_options_response()
        response.headers.update(CORS_HEADERS)
        return response


@app.after_request
def add_api_cors_headers(response):
    if request.path.startswith("/api/"):
        response.headers.update(CORS_HEADERS)
    return response


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


# Session tokens previously never expired — a leaked token worked forever,
# which is fine for a localhost demo but not once this is on a public URL
# (see README "Known limitations"). Two independent caps, whichever is hit
# first ends the session: an idle timeout (token unused this long) and an
# absolute lifetime (token exists this long, active or not) so a token
# that's used constantly still eventually forces a fresh login.
SESSION_IDLE_TIMEOUT_DAYS = 30
SESSION_ABSOLUTE_TIMEOUT_DAYS = 90


def _parse_db_timestamp(value):
    """Session created_at/last_used_at are stored as SQLite
    CURRENT_TIMESTAMP strings ('YYYY-MM-DD HH:MM:SS', UTC, no offset) or
    (for rows created via new_token() elsewhere) datetime.isoformat()
    output — accept both rather than assuming one format."""
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.datetime.strptime(value, fmt)
        except (ValueError, TypeError):
            continue
    return None


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
        "SELECT users.*, sessions.created_at AS session_created_at, "
        "sessions.last_used_at AS session_last_used_at "
        "FROM sessions JOIN users ON users.id = sessions.user_id WHERE sessions.token = ?",
        (token,),
    ).fetchone()
    if row is None:
        return None

    now = datetime.datetime.utcnow()
    created = _parse_db_timestamp(row["session_created_at"])
    last_used = _parse_db_timestamp(row["session_last_used_at"]) or created
    idle_expired = last_used is not None and (now - last_used).days >= SESSION_IDLE_TIMEOUT_DAYS
    absolute_expired = created is not None and (now - created).days >= SESSION_ABSOLUTE_TIMEOUT_DAYS
    if idle_expired or absolute_expired:
        # Clean up the dead row rather than just rejecting it, so an
        # abandoned token doesn't sit in the table forever.
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
        conn.commit()
        return None

    g.rate_limit_identity = f"user:{row['id']}"
    g.auth_method = "session"
    g.auth_can_trade = True
    try:
        # Best-effort, same pattern as the api_keys.last_used_at touch
        # above — sliding the idle-timeout window forward shouldn't be
        # able to break auth if it fails.
        conn.execute("UPDATE sessions SET last_used_at = ? WHERE token = ?", (now_iso(), token))
        conn.commit()
    except Exception:  # noqa: BLE001
        pass
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


# ---------- notifications ----------
#
# A flat, pre-rendered feed (see the `notifications` table comment in
# db.py) fed by every other event that's worth telling a user about after
# the fact: a market they held a position in resolving, a new achievement,
# a completed weekly challenge, a referral signing up. _notify() is the
# single insertion point every one of those call sites uses, so the shape
# of a notification row never has to be duplicated.

def _notify(conn, user_id, ntype, message, market_id=None):
    conn.execute(
        "INSERT INTO notifications (user_id, type, message, market_id) VALUES (?, ?, ?, ?)",
        (user_id, ntype, message, market_id),
    )


# ---------- compliance audit log (see audit_log table in db.py) ----------
#
# Single insertion point for anything a regulator or a future incident
# review would want a record of: KYC submitted/approved/rejected,
# real-money deposit/withdrawal attempted, an admin acting on someone
# else's account. user_id is who the event is about; actor_user_id is who
# performed it (equal to user_id for a self-service action like submitting
# your own KYC, the admin's id for an admin approving/rejecting someone
# else's).

def _audit(conn, user_id, actor_user_id, event_type, detail=None):
    conn.execute(
        "INSERT INTO audit_log (user_id, actor_user_id, event_type, detail) VALUES (?, ?, ?, ?)",
        (user_id, actor_user_id, event_type, detail),
    )


@app.get("/api/notifications")
@require_auth
def notifications():
    conn = db.get_db()
    rows = conn.execute(
        "SELECT id, type, message, market_id, is_read, created_at FROM notifications "
        "WHERE user_id = ? ORDER BY created_at DESC LIMIT 50",
        (g.user["id"],),
    ).fetchall()
    unread_count = conn.execute(
        "SELECT COUNT(*) c FROM notifications WHERE user_id = ? AND is_read = 0", (g.user["id"],)
    ).fetchone()["c"]
    return jsonify({
        "unread_count": unread_count,
        "notifications": [
            {
                "id": r["id"], "type": r["type"], "message": r["message"],
                "market_id": r["market_id"], "is_read": bool(r["is_read"]), "created_at": r["created_at"],
            }
            for r in rows
        ],
    })


@app.post("/api/notifications/<int:notification_id>/read")
@require_auth
def mark_notification_read(notification_id):
    conn = db.get_db()
    row = conn.execute(
        "SELECT id FROM notifications WHERE id = ? AND user_id = ?", (notification_id, g.user["id"])
    ).fetchone()
    if not row:
        return jsonify({"detail": "Notification not found"}), 404
    conn.execute("UPDATE notifications SET is_read = 1 WHERE id = ?", (notification_id,))
    conn.commit()
    return jsonify({"marked_read": True})


@app.post("/api/notifications/read_all")
@require_auth
def mark_all_notifications_read():
    conn = db.get_db()
    conn.execute("UPDATE notifications SET is_read = 1 WHERE user_id = ? AND is_read = 0", (g.user["id"],))
    conn.commit()
    return jsonify({"marked_read": True})


# ---------- auth routes ----------

# Flat play-money bonus credited to BOTH sides of a referral the moment the
# referred account signs up — no cap, no streak math (unlike the daily
# bonus), since a referral can only ever fire once per new account. Kept
# equal for both sides so "invite a friend" reads as a mutual benefit
# rather than the referrer skimming off the new signup.
REFERRAL_BONUS_REFEREE = 250.0
REFERRAL_BONUS_REFERRER = 250.0


@app.post("/api/signup")
# Unauthenticated, so this limits by IP (see ratelimit.py's
# _client_identity) — capped tighter than most endpoints since the only
# thing behind this one is spam account creation.
@rate_limit(5, 60)
def signup():
    data = request.get_json(force=True, silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    referral_code = (data.get("referral_code") or "").strip()
    if len(username) < 3 or len(username) > 32:
        return jsonify({"detail": "Username must be 3-32 characters"}), 400
    if len(password) < 6:
        return jsonify({"detail": "Password must be at least 6 characters"}), 400

    conn = db.get_db()
    existing = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
    if existing:
        return jsonify({"detail": "Username already taken"}), 400

    # A referral code is just the referrer's username. Silently ignore it
    # if it doesn't match a real account or matches the new username
    # itself (can't refer yourself) — a bad/stale referral link should
    # never block signup, it should just not pay out.
    referrer = None
    if referral_code and referral_code.lower() != username.lower():
        referrer = conn.execute(
            "SELECT id, username, balance FROM users WHERE username = ?", (referral_code,)
        ).fetchone()

    starting_balance = db.STARTING_BALANCE + (REFERRAL_BONUS_REFEREE if referrer else 0)

    pw_hash = hash_password(password)
    cur = conn.execute(
        "INSERT INTO users (username, password_hash, balance, is_admin, referred_by_user_id) "
        "VALUES (?, ?, ?, 0, ?)",
        (username, pw_hash, starting_balance, referrer["id"] if referrer else None),
    )
    user_id = cur.lastrowid
    conn.execute(
        "INSERT INTO transactions (user_id, type, amount, balance_after) VALUES (?, 'signup_bonus', ?, ?)",
        (user_id, db.STARTING_BALANCE, db.STARTING_BALANCE),
    )
    if referrer:
        conn.execute(
            "INSERT INTO transactions (user_id, type, amount, balance_after) VALUES (?, 'referral_bonus', ?, ?)",
            (user_id, REFERRAL_BONUS_REFEREE, starting_balance),
        )
        new_referrer_balance = referrer["balance"] + REFERRAL_BONUS_REFERRER
        conn.execute("UPDATE users SET balance = ? WHERE id = ?", (new_referrer_balance, referrer["id"]))
        conn.execute(
            "INSERT INTO transactions (user_id, type, amount, balance_after) VALUES (?, 'referral_bonus', ?, ?)",
            (referrer["id"], REFERRAL_BONUS_REFERRER, new_referrer_balance),
        )
        _notify(
            conn, referrer["id"], "referral_signup",
            f"{username} signed up using your referral link — you earned ${REFERRAL_BONUS_REFERRER:,.2f}.",
        )
    token = new_token()
    # created_at/last_used_at set explicitly rather than relying on the
    # column defaults — those only apply on a freshly created database; a
    # database that went through the sessions.last_used_at migration has
    # no default on that column at all (see SESSION_COLUMN_MIGRATIONS),
    # so an INSERT that omitted it would silently write NULL there.
    _session_now = now_iso()
    conn.execute(
        "INSERT INTO sessions (token, user_id, created_at, last_used_at) VALUES (?, ?, ?, ?)",
        (token, user_id, _session_now, _session_now),
    )
    conn.commit()

    return jsonify({
        "token": token, "username": username, "balance": starting_balance,
        "is_admin": False, "wallet_address": None,
        "referral_bonus_applied": bool(referrer),
        "referred_by_username": referrer["username"] if referrer else None,
    })


@app.post("/api/login")
# Unauthenticated (by IP), and the one endpoint where rate limiting
# actually matters for security, not just abuse prevention — without this,
# nothing stops an unlimited password-guessing loop against any username.
@rate_limit(10, 60)
def login():
    data = request.get_json(force=True, silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""

    conn = db.get_db()
    user = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    if not user or not verify_password(password, user["password_hash"]):
        return jsonify({"detail": "Invalid username or password"}), 401

    token = new_token()
    _session_now = now_iso()
    conn.execute(
        "INSERT INTO sessions (token, user_id, created_at, last_used_at) VALUES (?, ?, ?, ?)",
        (token, user["id"], _session_now, _session_now),
    )
    conn.commit()
    return jsonify({
        "token": token, "username": user["username"],
        "balance": user["balance"], "is_admin": bool(user["is_admin"]),
        "wallet_address": user["wallet_address"],
    })


# ---------- password reset (email-based) ----------
#
# forgot_password() always returns the same generic response whether or
# not the username/email matched an account, and whether or not that
# account has an email on file — a different response for "no such
# account" vs "account exists but no email set" would let anyone probe
# which usernames/emails are registered. The only place real information
# ever appears is in the email itself, sent to an address the requester
# already had to know.

RESET_TOKEN_TTL_MINUTES = 60
_FORGOT_PASSWORD_GENERIC_RESPONSE = {
    "detail": "If that account has an email on file, we've sent a password reset link to it.",
}


@app.post("/api/forgot-password")
# Tight limit, by IP — this endpoint's whole job is "look up an account by
# untrusted input", exactly the shape of thing brute-forcing/enumeration
# abuses.
@rate_limit(5, 60)
def forgot_password():
    data = request.get_json(force=True, silent=True) or {}
    identifier = (data.get("username") or "").strip()

    conn = db.get_db()
    user = conn.execute(
        "SELECT * FROM users WHERE username = ? OR email = ?",
        (identifier, identifier.lower()),
    ).fetchone()

    if user is not None and user["email"]:
        token = new_token()
        conn.execute(
            "INSERT INTO password_reset_tokens (user_id, token_hash) VALUES (?, ?)",
            (user["id"], hash_api_key(token)),  # SHA-256 — see hash_api_key's docstring for why that's the right call for a random token, not PBKDF2
        )
        conn.commit()
        base_url = os.environ.get("APP_BASE_URL", request.url_root).rstrip("/")
        reset_url = f"{base_url}/#/reset-password?token={token}"
        try:
            email_feed.send_password_reset_email(user["email"], reset_url)
        except email_feed.EmailFeedError as e:
            # Logged, not surfaced — the caller gets the same generic
            # response either way (see module note above), and a
            # misconfigured/down email provider shouldn't turn into a 500
            # for someone who just wants to reset their password, or a
            # signal that distinguishes "account exists" from "doesn't".
            print(f"[forgot-password] failed to send reset email to user {user['id']}: {e}")

    return jsonify(_FORGOT_PASSWORD_GENERIC_RESPONSE)


@app.post("/api/reset-password")
@rate_limit(10, 60)
def reset_password():
    data = request.get_json(force=True, silent=True) or {}
    token = (data.get("token") or "").strip()
    new_password = data.get("new_password") or ""
    if not token:
        return jsonify({"detail": "Missing reset token"}), 400
    if len(new_password) < 6:
        return jsonify({"detail": "Password must be at least 6 characters"}), 400

    conn = db.get_db()
    row = conn.execute(
        "SELECT * FROM password_reset_tokens WHERE token_hash = ?", (hash_api_key(token),)
    ).fetchone()
    if row is None:
        return jsonify({"detail": "This reset link is invalid or has already been used"}), 400
    if row["used_at"] is not None:
        return jsonify({"detail": "This reset link has already been used"}), 400
    created = _parse_db_timestamp(row["created_at"])
    if created is None or (datetime.datetime.utcnow() - created).total_seconds() > RESET_TOKEN_TTL_MINUTES * 60:
        return jsonify({"detail": "This reset link has expired — request a new one"}), 400

    pw_hash = hash_password(new_password)
    conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (pw_hash, row["user_id"]))
    conn.execute("UPDATE password_reset_tokens SET used_at = ? WHERE id = ?", (now_iso(), row["id"]))
    # A password reset is exactly the moment to force every existing
    # session for this account to log out — if the reset was prompted by
    # a leaked password, whoever had it may also be sitting on a live
    # session token that would otherwise keep working right past the
    # password change.
    conn.execute("DELETE FROM sessions WHERE user_id = ?", (row["user_id"],))
    conn.commit()
    return jsonify({"detail": "Password reset — log in with your new password."})


@app.get("/api/me")
@require_auth
def me():
    u = g.user
    claimable, _, streak = _daily_bonus_status(u)
    return jsonify({
        "username": u["username"], "balance": u["balance"], "is_admin": bool(u["is_admin"]),
        "wallet_address": u["wallet_address"] if "wallet_address" in u.keys() else None,
        "email": u["email"] if "email" in u.keys() else None,
        "daily_streak": streak, "daily_bonus_claimable": claimable,
        # Real-money mode is off everywhere by default (see realmoney.py) —
        # these fields are harmless to always send; the frontend only acts
        # on them when real_money_enabled is true.
        "real_money_enabled": realmoney.REAL_MONEY_ENABLED,
        "kyc_status": u["kyc_status"] if "kyc_status" in u.keys() else "unverified",
        "real_balance": u["real_balance"] if "real_balance" in u.keys() else 0,
    })


@app.get("/api/config")
def public_config():
    """Unauthenticated — the frontend calls this before login to know
    whether to show any real-money copy/UI at all."""
    return jsonify({"real_money_enabled": realmoney.REAL_MONEY_ENABLED})


# ---------- account email (for password-reset-by-email) ----------
#
# Not collected at signup — signup is still just username+password, so
# existing accounts aren't broken by this column showing up. A user opts
# in later from the Account page if they want "forgot password" to work
# for their account. See password reset section below.

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@app.post("/api/account/email")
@require_session_auth  # account-security-sensitive, same reasoning as api-keys/wallet management
@rate_limit(10, 60)
def set_account_email():
    data = request.get_json(force=True, silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    if not email or not _EMAIL_RE.match(email):
        return jsonify({"detail": "Enter a valid email address"}), 400

    conn = db.get_db()
    existing = conn.execute(
        "SELECT id FROM users WHERE email = ? AND id != ?", (email, g.user["id"])
    ).fetchone()
    if existing:
        return jsonify({"detail": "That email is already attached to another account"}), 400

    conn.execute("UPDATE users SET email = ? WHERE id = ?", (email, g.user["id"]))
    conn.commit()
    return jsonify({"email": email})


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
    _session_now = now_iso()
    conn.execute(
        "INSERT INTO sessions (token, user_id, created_at, last_used_at) VALUES (?, ?, ?, ?)",
        (token, user["id"], _session_now, _session_now),
    )
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


# ---------- real-money mode (see realmoney.py — off by default everywhere) ----------
#
# Everything below is scaffolding for turning real cash on *after*
# licensing/registration is actually in place, not a live payment system.
# realmoney.REAL_MONEY_ENABLED gates every endpoint here; it's false unless
# UNICORN_REAL_MONEY_ENABLED is deliberately set in the environment, which
# it isn't anywhere by default (see README's "Real-money mode" section).

REAL_MONEY_DEPOSIT_MIN = 1.00
REAL_MONEY_DEPOSIT_MAX = 10_000.00
REAL_MONEY_WITHDRAW_MIN = 1.00


def _require_real_money_enabled():
    if not realmoney.REAL_MONEY_ENABLED:
        return jsonify({
            "detail": "Real-money mode isn't enabled on this deploy yet. "
                       "It goes live only after licensing/registration is in place — "
                       "see README's \"Real-money mode\" section.",
        }), 404
    return None


def _latest_kyc_state(conn, user_id):
    """Returns the most recent kyc_verifications row for a user, or None if
    they've never submitted one."""
    return conn.execute(
        "SELECT * FROM kyc_verifications WHERE user_id = ? ORDER BY created_at DESC LIMIT 1",
        (user_id,),
    ).fetchone()


@app.post("/api/kyc/submit")
@require_session_auth  # identity-sensitive, same reasoning as account/email — not for API keys
@rate_limit(5, 60)
def submit_kyc():
    blocked = _require_real_money_enabled()
    if blocked:
        return blocked

    data = request.get_json(force=True, silent=True) or {}
    legal_name = (data.get("legal_name") or "").strip()
    date_of_birth = (data.get("date_of_birth") or "").strip()
    address = (data.get("address") or "").strip()
    state = (data.get("state") or "").strip().upper()

    if not legal_name or not date_of_birth or not address or not state:
        return jsonify({"detail": "legal_name, date_of_birth, address, and state are all required"}), 400
    if len(state) != 2:
        return jsonify({"detail": "state must be a two-letter code, e.g. ND"}), 400
    if realmoney.state_is_blocked(state):
        return jsonify({
            "detail": f"Real-money trading isn't available in {state} yet.",
        }), 403

    result = realmoney.kyc_provider.submit(g.user["id"], legal_name, date_of_birth, address, state)

    conn = db.get_db()
    conn.execute(
        "INSERT INTO kyc_verifications "
        "(user_id, legal_name, date_of_birth, address, state, status, provider, provider_reference) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (g.user["id"], legal_name, date_of_birth, address, state,
         result["status"], result["provider"], result.get("provider_reference")),
    )
    conn.execute("UPDATE users SET kyc_status = ? WHERE id = ?", (result["status"], g.user["id"]))
    _audit(conn, g.user["id"], g.user["id"], "kyc_submitted", f"provider={result['provider']} state={state}")
    conn.commit()
    return jsonify({"status": result["status"], "provider": result["provider"]})


@app.get("/api/kyc/status")
@require_auth
def kyc_status():
    conn = db.get_db()
    latest = _latest_kyc_state(conn, g.user["id"])
    return jsonify({
        "kyc_status": g.user["kyc_status"] if "kyc_status" in g.user.keys() else "unverified",
        "latest_submission": None if latest is None else {
            "status": latest["status"],
            "state": latest["state"],
            "provider": latest["provider"],
            "created_at": latest["created_at"],
            "reviewed_at": latest["reviewed_at"],
            "rejection_reason": latest["rejection_reason"],
        },
    })


@app.get("/api/admin/kyc")
@require_admin
def admin_kyc_queue():
    """?status=pending (default) | verified | rejected | all"""
    status = (request.args.get("status") or "pending").strip().lower()
    conn = db.get_db()
    if status == "all":
        rows = conn.execute(
            "SELECT kyc_verifications.*, users.username FROM kyc_verifications "
            "JOIN users ON users.id = kyc_verifications.user_id "
            "ORDER BY kyc_verifications.created_at DESC LIMIT 200"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT kyc_verifications.*, users.username FROM kyc_verifications "
            "JOIN users ON users.id = kyc_verifications.user_id "
            "WHERE kyc_verifications.status = ? ORDER BY kyc_verifications.created_at ASC LIMIT 200",
            (status,),
        ).fetchall()
    return jsonify({"submissions": [
        {
            "id": r["id"], "user_id": r["user_id"], "username": r["username"],
            "legal_name": r["legal_name"], "date_of_birth": r["date_of_birth"],
            "address": r["address"], "state": r["state"], "status": r["status"],
            "provider": r["provider"], "created_at": r["created_at"],
            "reviewed_at": r["reviewed_at"], "rejection_reason": r["rejection_reason"],
        }
        for r in rows
    ]})


@app.post("/api/admin/kyc/<int:submission_id>/approve")
@require_admin
def admin_kyc_approve(submission_id):
    conn = db.get_db()
    row = conn.execute("SELECT * FROM kyc_verifications WHERE id = ?", (submission_id,)).fetchone()
    if row is None:
        return jsonify({"detail": "Not found"}), 404
    conn.execute(
        "UPDATE kyc_verifications SET status = 'verified', reviewed_by_user_id = ?, reviewed_at = ? WHERE id = ?",
        (g.user["id"], now_iso(), submission_id),
    )
    conn.execute("UPDATE users SET kyc_status = 'verified' WHERE id = ?", (row["user_id"],))
    _notify(conn, row["user_id"], "kyc", "Your identity verification was approved — real-money features are now unlocked.")
    _audit(conn, row["user_id"], g.user["id"], "kyc_approved", f"submission_id={submission_id}")
    conn.commit()
    return jsonify({"status": "verified"})


@app.post("/api/admin/kyc/<int:submission_id>/reject")
@require_admin
def admin_kyc_reject(submission_id):
    data = request.get_json(force=True, silent=True) or {}
    reason = (data.get("reason") or "").strip() or "No reason given"
    conn = db.get_db()
    row = conn.execute("SELECT * FROM kyc_verifications WHERE id = ?", (submission_id,)).fetchone()
    if row is None:
        return jsonify({"detail": "Not found"}), 404
    conn.execute(
        "UPDATE kyc_verifications SET status = 'rejected', reviewed_by_user_id = ?, reviewed_at = ?, "
        "rejection_reason = ? WHERE id = ?",
        (g.user["id"], now_iso(), reason, submission_id),
    )
    conn.execute("UPDATE users SET kyc_status = 'rejected' WHERE id = ?", (row["user_id"],))
    _notify(conn, row["user_id"], "kyc", f"Your identity verification wasn't approved: {reason}")
    _audit(conn, row["user_id"], g.user["id"], "kyc_rejected", f"submission_id={submission_id} reason={reason}")
    conn.commit()
    return jsonify({"status": "rejected"})


def _require_kyc_verified(conn):
    latest = _latest_kyc_state(conn, g.user["id"])
    current_status = g.user["kyc_status"] if "kyc_status" in g.user.keys() else "unverified"
    if current_status != "verified":
        return jsonify({"detail": "Identity verification is required before real-money deposits or withdrawals."}), 403
    if latest is not None and realmoney.state_is_blocked(latest["state"]):
        return jsonify({"detail": f"Real-money trading isn't available in {latest['state']} yet."}), 403
    return None


@app.get("/api/braintree/client-token")
@require_session_auth
def braintree_client_token():
    """The frontend calls this to initialize Braintree's Drop-in UI, which
    is what actually shows the card / Apple Pay / Venmo / PayPal picker.
    404s with the same "not enabled yet" message as the other real-money
    routes if this deploy isn't configured for real payments yet."""
    blocked = _require_real_money_enabled()
    if blocked:
        return blocked
    if not hasattr(realmoney.payments_provider, "client_token"):
        return jsonify({
            "detail": "No real payment processor is connected on this deploy yet.",
        }), 503
    try:
        token = realmoney.payments_provider.client_token()
    except realmoney.PaymentsNotConfiguredError as e:
        return jsonify({"detail": str(e)}), 503
    return jsonify({"client_token": token})


@app.post("/api/real-money/deposit")
@require_session_auth
@rate_limit(10, 60)
def real_money_deposit():
    blocked = _require_real_money_enabled()
    if blocked:
        return blocked

    conn = db.get_db()
    kyc_blocked = _require_kyc_verified(conn)
    if kyc_blocked:
        return kyc_blocked

    data = request.get_json(force=True, silent=True) or {}
    try:
        amount = round(float(data.get("amount")), 2)
    except (TypeError, ValueError):
        return jsonify({"detail": "amount must be a number"}), 400
    if amount < REAL_MONEY_DEPOSIT_MIN or amount > REAL_MONEY_DEPOSIT_MAX:
        return jsonify({
            "detail": f"amount must be between ${REAL_MONEY_DEPOSIT_MIN:.2f} and ${REAL_MONEY_DEPOSIT_MAX:,.2f}",
        }), 400

    payment_method_nonce = (data.get("payment_method_nonce") or "").strip() or None
    try:
        result = realmoney.payments_provider.create_deposit(
            g.user["id"], amount, payment_method_nonce=payment_method_nonce,
        )
    except realmoney.PaymentsNotConfiguredError as e:
        return jsonify({"detail": str(e)}), 503

    user = conn.execute("SELECT * FROM users WHERE id = ?", (g.user["id"],)).fetchone()
    new_balance = round(user["real_balance"] + amount, 2) if result["status"] == "completed" else user["real_balance"]
    if result["status"] == "completed":
        conn.execute("UPDATE users SET real_balance = ? WHERE id = ?", (new_balance, user["id"]))
    conn.execute(
        "INSERT INTO real_money_transactions "
        "(user_id, type, amount, status, provider, provider_reference, real_balance_after, completed_at) "
        "VALUES (?, 'deposit', ?, ?, ?, ?, ?, ?)",
        (user["id"], amount, result["status"], result["provider"], result.get("provider_reference"),
         new_balance, now_iso() if result["status"] == "completed" else None),
    )
    _audit(conn, user["id"], user["id"], "real_money_deposit", f"amount={amount} status={result['status']}")
    conn.commit()
    return jsonify({"status": result["status"], "real_balance": new_balance, "amount": amount})


@app.post("/api/real-money/withdraw")
@require_session_auth
@rate_limit(10, 60)
def real_money_withdraw():
    blocked = _require_real_money_enabled()
    if blocked:
        return blocked

    conn = db.get_db()
    kyc_blocked = _require_kyc_verified(conn)
    if kyc_blocked:
        return kyc_blocked

    data = request.get_json(force=True, silent=True) or {}
    try:
        amount = round(float(data.get("amount")), 2)
    except (TypeError, ValueError):
        return jsonify({"detail": "amount must be a number"}), 400
    if amount < REAL_MONEY_WITHDRAW_MIN:
        return jsonify({"detail": f"amount must be at least ${REAL_MONEY_WITHDRAW_MIN:.2f}"}), 400

    payout_email = (data.get("payout_email") or "").strip() or None
    if payout_email and "@" not in payout_email:
        return jsonify({"detail": "payout_email must be a valid email address"}), 400

    user = conn.execute("SELECT * FROM users WHERE id = ?", (g.user["id"],)).fetchone()
    if amount > user["real_balance"]:
        return jsonify({"detail": "Insufficient real-money balance"}), 400

    try:
        result = realmoney.payments_provider.create_withdrawal(
            g.user["id"], amount, payout_email=payout_email,
        )
    except realmoney.PaymentsNotConfiguredError as e:
        return jsonify({"detail": str(e)}), 503

    new_balance = round(user["real_balance"] - amount, 2) if result["status"] == "completed" else user["real_balance"]
    if result["status"] == "completed":
        conn.execute("UPDATE users SET real_balance = ? WHERE id = ?", (new_balance, user["id"]))
    conn.execute(
        "INSERT INTO real_money_transactions "
        "(user_id, type, amount, status, provider, provider_reference, real_balance_after, completed_at) "
        "VALUES (?, 'withdrawal', ?, ?, ?, ?, ?, ?)",
        (user["id"], amount, result["status"], result["provider"], result.get("provider_reference"),
         new_balance, now_iso() if result["status"] == "completed" else None),
    )
    _audit(conn, user["id"], user["id"], "real_money_withdrawal", f"amount={amount} status={result['status']}")
    conn.commit()
    return jsonify({"status": result["status"], "real_balance": new_balance, "amount": amount})


@app.get("/api/real-money/transactions")
@require_auth
def real_money_transactions():
    conn = db.get_db()
    rows = conn.execute(
        "SELECT type, amount, status, provider, real_balance_after, created_at, completed_at "
        "FROM real_money_transactions WHERE user_id = ? ORDER BY created_at DESC LIMIT 200",
        (g.user["id"],),
    ).fetchall()
    return jsonify({"transactions": [dict(r) for r in rows]})


# ---------- surveillance (market integrity) routes ----------
#
# Deliberately gated only by @require_admin, not by real-money mode — see
# surveillance.py's module docstring. An admin should be able to see and
# review flags against ordinary play-money trading today, since that's how
# the detection logic gets exercised and trusted before it ever matters for
# real money.

@app.get("/api/admin/surveillance")
@require_admin
def admin_surveillance_queue():
    """?status=open (default) | resolved | dismissed | all"""
    status = (request.args.get("status") or "open").strip().lower()
    conn = db.get_db()
    if status == "all":
        rows = conn.execute(
            "SELECT surveillance_flags.*, "
            "u.username AS username, ru.username AS related_username, "
            "m.question AS market_question "
            "FROM surveillance_flags "
            "LEFT JOIN users u ON u.id = surveillance_flags.user_id "
            "LEFT JOIN users ru ON ru.id = surveillance_flags.related_user_id "
            "LEFT JOIN markets m ON m.id = surveillance_flags.market_id "
            "ORDER BY surveillance_flags.created_at DESC LIMIT 200"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT surveillance_flags.*, "
            "u.username AS username, ru.username AS related_username, "
            "m.question AS market_question "
            "FROM surveillance_flags "
            "LEFT JOIN users u ON u.id = surveillance_flags.user_id "
            "LEFT JOIN users ru ON ru.id = surveillance_flags.related_user_id "
            "LEFT JOIN markets m ON m.id = surveillance_flags.market_id "
            "WHERE surveillance_flags.status = ? "
            "ORDER BY surveillance_flags.created_at ASC LIMIT 200",
            (status,),
        ).fetchall()
    return jsonify({"flags": [
        {
            "id": r["id"], "flag_type": r["flag_type"], "severity": r["severity"], "status": r["status"],
            "detail": r["detail"], "market_id": r["market_id"], "market_question": r["market_question"],
            "user_id": r["user_id"], "username": r["username"],
            "related_user_id": r["related_user_id"], "related_username": r["related_username"],
            "created_at": r["created_at"], "reviewed_at": r["reviewed_at"],
            "reviewed_by_user_id": r["reviewed_by_user_id"],
        }
        for r in rows
    ]})


def _review_surveillance_flag(flag_id, new_status, event_type):
    conn = db.get_db()
    row = conn.execute("SELECT * FROM surveillance_flags WHERE id = ?", (flag_id,)).fetchone()
    if row is None:
        return jsonify({"detail": "Not found"}), 404
    if row["status"] != "open":
        return jsonify({"detail": f"Flag #{flag_id} was already {row['status']}"}), 409
    conn.execute(
        "UPDATE surveillance_flags SET status = ?, reviewed_by_user_id = ?, reviewed_at = ? WHERE id = ?",
        (new_status, g.user["id"], now_iso(), flag_id),
    )
    _audit(conn, row["user_id"], g.user["id"], event_type,
           f"flag_id={flag_id} flag_type={row['flag_type']} market_id={row['market_id']}")
    conn.commit()
    return jsonify({"status": new_status})


@app.post("/api/admin/surveillance/<int:flag_id>/resolve")
@require_admin
def admin_surveillance_resolve(flag_id):
    """Marks a flag as reviewed and acted on (e.g. the account was warned or
    restricted as a result)."""
    return _review_surveillance_flag(flag_id, "resolved", "surveillance_flag_resolved")


@app.post("/api/admin/surveillance/<int:flag_id>/dismiss")
@require_admin
def admin_surveillance_dismiss(flag_id):
    """Marks a flag as reviewed and judged a false positive / no action
    needed."""
    return _review_surveillance_flag(flag_id, "dismissed", "surveillance_flag_dismissed")


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


def _resolved_market_pnls(conn, user_id):
    """One entry per market this user ever traded that has since resolved:
    {market_id, question, pnl, resolved_at}, sorted oldest-resolved-first.
    net P&L per market is just every trade/payout amount for that market
    summed (trade rows are already stored negative, as the cost paid;
    payout rows are positive, credited only to winning positions — see
    apply_resolution() in resolution.py). A market still open contributes
    nothing here since its outcome isn't known yet. Shared by
    /api/portfolio/stats (win rate, P&L, biggest win/loss) and
    /api/achievements (first-win / hot-streak checks) so both agree on
    what counts as a "win" for a given market."""
    rows = conn.execute(
        "SELECT transactions.market_id, transactions.amount, "
        "markets.question AS market_question, markets.status AS market_status, "
        "markets.resolved_at AS market_resolved_at "
        "FROM transactions JOIN markets ON markets.id = transactions.market_id "
        "WHERE transactions.user_id = ? AND transactions.type IN ('trade', 'payout')",
        (user_id,),
    ).fetchall()
    per_market = {}
    for r in rows:
        if r["market_status"] != "resolved":
            continue
        entry = per_market.setdefault(r["market_id"], {
            "market_id": r["market_id"], "question": r["market_question"],
            "pnl": 0.0, "resolved_at": r["market_resolved_at"],
        })
        entry["pnl"] += r["amount"]
    return sorted(per_market.values(), key=lambda m: m["resolved_at"] or "")


@app.get("/api/portfolio/stats")
@require_auth
def portfolio_stats():
    """Realized performance stats — win rate, total P&L, biggest single-market
    win/loss — computed only from *resolved* markets, plus a balance-over-time
    series for a sparkline chart. Deliberately separate from /api/portfolio
    (which lists current open positions): this endpoint is about looking
    backward at what already happened, that one's about what's still live."""
    conn = db.get_db()
    market_pnls = _resolved_market_pnls(conn, g.user["id"])
    per_market = {m["market_id"]: m for m in market_pnls}

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


# ---------- achievements ----------
#
# A fixed catalog (not a DB table) of badges, each with a `check` predicate
# run against fresh-computed stats. Earning is one-way and persisted in
# user_achievements the first time a check passes (see sync_achievements)
# — deliberately NOT re-derived from scratch on every request, so a badge
# that depends on a value that can go back down (daily_streak resets after
# a missed day; is_bot_trader is the only monotonic one already) doesn't
# flicker on and off. Once earned, always earned.

ACHIEVEMENTS = [
    {
        "key": "first_trade", "label": "First Trade",
        "description": "Place your first trade.",
    },
    {
        "key": "century_club", "label": "Century Club",
        "description": "Place 100 trades.",
    },
    {
        "key": "first_win", "label": "First Win",
        "description": "Win your first resolved market.",
    },
    {
        "key": "hot_streak", "label": "Hot Streak",
        "description": "Win 3 resolved markets in a row.",
    },
    {
        "key": "high_roller", "label": "High Roller",
        "description": "Spend $250 or more on a single trade.",
    },
    {
        "key": "big_winner", "label": "Big Winner",
        "description": "Net $100 or more in profit on a single market.",
    },
    {
        "key": "market_explorer", "label": "Market Explorer",
        "description": "Trade in 5 different market categories.",
    },
    {
        "key": "daily_devotee", "label": "Daily Devotee",
        "description": "Reach a 7-day daily-bonus streak.",
    },
    {
        "key": "bot_trader", "label": "Bot Trader",
        "description": "Place a trade using an API key.",
    },
    {
        "key": "networker", "label": "Networker",
        "description": "Refer 3 friends who sign up.",
    },
]


def _currently_qualifying_achievements(conn, user):
    """Returns the set of achievement keys `user` qualifies for *right now*
    — a superset of what's persisted, since this re-checks everything on
    every call. sync_achievements() below is what actually reconciles this
    against user_achievements and makes earning permanent."""
    user_id = user["id"]
    earned = set()

    trade_count = conn.execute(
        "SELECT COUNT(*) c FROM transactions WHERE user_id = ? AND type = 'trade'", (user_id,)
    ).fetchone()["c"]
    if trade_count >= 1:
        earned.add("first_trade")
    if trade_count >= 100:
        earned.add("century_club")

    market_pnls = _resolved_market_pnls(conn, user_id)
    if any(m["pnl"] > 1e-9 for m in market_pnls):
        earned.add("first_win")
    if any(m["pnl"] >= 100 - 1e-9 for m in market_pnls):
        earned.add("big_winner")

    # Longest run of consecutive wins, in resolution order — a breakeven
    # market (pnl ~= 0, vanishingly rare in practice) neither extends nor
    # breaks a streak, only an actual loss does.
    run = best_run = 0
    for m in market_pnls:
        if m["pnl"] > 1e-9:
            run += 1
            best_run = max(best_run, run)
        elif m["pnl"] < -1e-9:
            run = 0
    if best_run >= 3:
        earned.add("hot_streak")

    high_roller_row = conn.execute(
        "SELECT 1 FROM transactions WHERE user_id = ? AND type = 'trade' AND amount <= -250 LIMIT 1",
        (user_id,),
    ).fetchone()
    if high_roller_row:
        earned.add("high_roller")

    category_count = conn.execute(
        "SELECT COUNT(DISTINCT markets.category) c FROM transactions "
        "JOIN markets ON markets.id = transactions.market_id "
        "WHERE transactions.user_id = ? AND transactions.type = 'trade'",
        (user_id,),
    ).fetchone()["c"]
    if category_count >= 5:
        earned.add("market_explorer")

    streak = user["daily_streak"] if "daily_streak" in user.keys() else 0
    if streak >= 7:
        earned.add("daily_devotee")

    if "is_bot_trader" in user.keys() and user["is_bot_trader"]:
        earned.add("bot_trader")

    referral_count = conn.execute(
        "SELECT COUNT(*) c FROM users WHERE referred_by_user_id = ?", (user_id,)
    ).fetchone()["c"]
    if referral_count >= 3:
        earned.add("networker")

    return earned


def sync_achievements(conn, user):
    """Persists any newly-qualifying achievements for `user` and returns
    the full set of keys they've ever earned (existing + newly unlocked).
    Safe to call on every /api/achievements request — INSERT OR IGNORE
    means re-syncing an already-earned achievement is a no-op."""
    qualifying = _currently_qualifying_achievements(conn, user)
    if not qualifying:
        return set()
    existing = {
        r["achievement_key"] for r in
        conn.execute("SELECT achievement_key FROM user_achievements WHERE user_id = ?", (user["id"],)).fetchall()
    }
    new_keys = qualifying - existing
    if new_keys:
        now = now_iso()
        by_key = {a["key"]: a for a in ACHIEVEMENTS}
        for key in new_keys:
            conn.execute(
                "INSERT OR IGNORE INTO user_achievements (user_id, achievement_key, earned_at) VALUES (?, ?, ?)",
                (user["id"], key, now),
            )
            _notify(
                conn, user["id"], "achievement_earned",
                f"Achievement unlocked: {by_key[key]['label']} — {by_key[key]['description']}",
            )
        conn.commit()
    return existing | new_keys


@app.get("/api/achievements")
@require_auth
def achievements():
    conn = db.get_db()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (g.user["id"],)).fetchone()
    sync_achievements(conn, user)
    earned_rows = {
        r["achievement_key"]: r["earned_at"] for r in
        conn.execute("SELECT achievement_key, earned_at FROM user_achievements WHERE user_id = ?", (user["id"],)).fetchall()
    }
    return jsonify([
        {
            "key": a["key"], "label": a["label"], "description": a["description"],
            "earned": a["key"] in earned_rows,
            "earned_at": earned_rows.get(a["key"]),
        }
        for a in ACHIEVEMENTS
    ])


@app.get("/api/referrals")
@require_auth
def referrals():
    conn = db.get_db()
    user_id = g.user["id"]
    rows = conn.execute(
        "SELECT username, created_at FROM users WHERE referred_by_user_id = ? ORDER BY created_at DESC",
        (user_id,),
    ).fetchall()
    total_bonus_earned = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) s FROM transactions "
        "WHERE user_id = ? AND type = 'referral_bonus'",
        (user_id,),
    ).fetchone()["s"]
    return jsonify({
        "referral_code": g.user["username"],
        "referral_count": len(rows),
        "total_bonus_earned": total_bonus_earned,
        "referred_users": [{"username": r["username"], "created_at": r["created_at"]} for r in rows],
    })


# ---------- weekly challenges ----------
#
# Unlike ACHIEVEMENTS (permanent, earned once) these reset every week: the
# reward is claimable again once a new week_key rolls over, same challenge
# key and all. user_challenge_claims is keyed on (user_id, challenge_key,
# week_key) so a stale claim from a previous week never blocks — or
# double-pays — the current week's version of the same challenge.

CHALLENGES = [
    {
        "key": "trade_5", "label": "Active Trader", "reward": 50.0,
        "description": "Place 5 trades this week.",
    },
    {
        "key": "diversify", "label": "Diversify", "reward": 75.0,
        "description": "Trade in 3 different market categories this week.",
    },
    {
        "key": "big_trade", "label": "Swing for the Fences", "reward": 75.0,
        "description": "Place a single trade costing $100 or more this week.",
    },
]


def current_week_start():
    """UTC-Monday-00:00 start of the current week, as a naive datetime —
    the boundary every challenge's "this week" activity is measured
    against. Computed from wall-clock time rather than stored anywhere,
    so it's always correct without a scheduled job to roll it over."""
    now = datetime.datetime.utcnow()
    return (now - datetime.timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)


def current_week_key():
    """A short stable string identifying the current week, e.g.
    '2026-08-17' for its Monday — used as the reset key in
    user_challenge_claims, not shown to users."""
    return current_week_start().strftime("%Y-%m-%d")


def _currently_qualifying_challenges(conn, user_id, week_start_iso):
    qualifying = set()

    trade_count = conn.execute(
        "SELECT COUNT(*) c FROM transactions WHERE user_id = ? AND type = 'trade' AND created_at >= ?",
        (user_id, week_start_iso),
    ).fetchone()["c"]
    if trade_count >= 5:
        qualifying.add("trade_5")

    category_count = conn.execute(
        "SELECT COUNT(DISTINCT markets.category) c FROM transactions "
        "JOIN markets ON markets.id = transactions.market_id "
        "WHERE transactions.user_id = ? AND transactions.type = 'trade' AND transactions.created_at >= ?",
        (user_id, week_start_iso),
    ).fetchone()["c"]
    if category_count >= 3:
        qualifying.add("diversify")

    # 'trade' transactions store amount as the negative cost paid.
    big_trade_row = conn.execute(
        "SELECT 1 FROM transactions WHERE user_id = ? AND type = 'trade' "
        "AND created_at >= ? AND amount <= -100 LIMIT 1",
        (user_id, week_start_iso),
    ).fetchone()
    if big_trade_row:
        qualifying.add("big_trade")

    return qualifying


def sync_challenges(conn, user):
    """Same INSERT-OR-IGNORE-then-credit pattern as sync_achievements(),
    scoped to the current week_key. Returns {key: claimed_at} for every
    challenge claimed this week (existing + newly claimed just now)."""
    user_id = user["id"]
    week_key = current_week_key()
    week_start_iso = current_week_start().isoformat()

    existing = {
        r["challenge_key"]: r["claimed_at"] for r in conn.execute(
            "SELECT challenge_key, claimed_at FROM user_challenge_claims "
            "WHERE user_id = ? AND week_key = ?",
            (user_id, week_key),
        ).fetchall()
    }
    qualifying = _currently_qualifying_challenges(conn, user_id, week_start_iso)
    new_keys = qualifying - existing.keys()
    if new_keys:
        now = now_iso()
        balance = user["balance"]
        by_key = {c["key"]: c for c in CHALLENGES}
        for key in new_keys:
            reward = by_key[key]["reward"]
            balance += reward
            conn.execute(
                "INSERT OR IGNORE INTO user_challenge_claims (user_id, challenge_key, week_key, claimed_at) "
                "VALUES (?, ?, ?, ?)",
                (user_id, key, week_key, now),
            )
            conn.execute(
                "INSERT INTO transactions (user_id, type, amount, balance_after) "
                "VALUES (?, 'weekly_challenge', ?, ?)",
                (user_id, reward, balance),
            )
            _notify(
                conn, user_id, "challenge_completed",
                f"Weekly challenge completed: {by_key[key]['label']} — you earned ${reward:,.2f}.",
            )
            existing[key] = now
        conn.execute("UPDATE users SET balance = ? WHERE id = ?", (balance, user_id))
        conn.commit()
    return existing


@app.get("/api/challenges")
@require_auth
def challenges():
    conn = db.get_db()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (g.user["id"],)).fetchone()
    claimed = sync_challenges(conn, user)
    refreshed_balance = conn.execute("SELECT balance FROM users WHERE id = ?", (user["id"],)).fetchone()["balance"]
    week_start = current_week_start()
    reset_at = (week_start + datetime.timedelta(days=7)).isoformat()
    return jsonify({
        "balance": refreshed_balance,
        "resets_at": reset_at,
        "challenges": [
            {
                "key": c["key"], "label": c["label"], "description": c["description"],
                "reward": c["reward"],
                "completed": c["key"] in claimed,
                "completed_at": claimed.get(c["key"]),
            }
            for c in CHALLENGES
        ],
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
