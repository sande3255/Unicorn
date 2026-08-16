"""Shared market-resolution/payout logic, used by both the admin "resolve"
API endpoint and the background scheduler's auto-resolve. Takes a raw
sqlite3 connection so it works the same whether called from a Flask
request (via db.get_db()) or from the background thread (its own
connection, since Flask's `g` only exists inside a request context)."""
import datetime


def now_iso():
    return datetime.datetime.utcnow().isoformat()


def apply_resolution(conn, market_id: int, outcome: str, settlement_price: float | None = None):
    """Resolves a market and pays out winning positions. Returns the
    updated market row, or None if the market didn't exist or was already
    resolved (caller should treat that as a no-op, not an error, since the
    scheduler may race with a manual admin resolve)."""
    m = conn.execute("SELECT * FROM markets WHERE id = ?", (market_id,)).fetchone()
    if not m or m["status"] == "resolved":
        return None

    conn.execute(
        "UPDATE markets SET status = 'resolved', resolved_outcome = ?, resolved_at = ?, settlement_price = ? "
        "WHERE id = ?",
        (outcome, now_iso(), settlement_price, market_id),
    )

    positions = conn.execute("SELECT * FROM positions WHERE market_id = ?", (market_id,)).fetchall()
    for pos in positions:
        winning_shares = pos["shares_yes"] if outcome == "YES" else pos["shares_no"]
        if winning_shares > 1e-9:
            user = conn.execute("SELECT * FROM users WHERE id = ?", (pos["user_id"],)).fetchone()
            new_balance = user["balance"] + winning_shares
            conn.execute("UPDATE users SET balance = ? WHERE id = ?", (new_balance, user["id"]))
            conn.execute(
                "INSERT INTO transactions (user_id, market_id, type, outcome, shares, amount, balance_after) "
                "VALUES (?, ?, 'payout', ?, ?, ?, ?)",
                (user["id"], market_id, outcome, winning_shares, winning_shares, new_balance),
            )
    conn.commit()
    return conn.execute("SELECT * FROM markets WHERE id = ?", (market_id,)).fetchone()
