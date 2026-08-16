"""Resets the 'admin' account's password in predictmarket.db directly,
using the same PBKDF2 scheme as backend/app/security.py. Run this with the
server stopped to avoid any database lock contention.

Usage:  python reset_admin_password.py [new_password] [username]
Defaults: new_password = admin123, username = admin
"""
import hashlib
import os
import sqlite3
import sys

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "predictmarket.db")
PBKDF2_ITERATIONS = 200_000


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"{salt.hex()}${digest.hex()}"


def main():
    new_password = sys.argv[1] if len(sys.argv) > 1 else "admin123"
    username = sys.argv[2] if len(sys.argv) > 2 else "admin"

    if not os.path.exists(DB_PATH):
        print(f"ERROR: no database found at {DB_PATH}")
        print("Run the server at least once first so it creates the database.")
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT id, is_admin FROM users WHERE username = ?", (username,)).fetchone()

    if row is None:
        print(f"No user named '{username}' exists yet.")
        print("Creating it fresh as an admin account with the requested password...")
        pw_hash = hash_password(new_password)
        conn.execute(
            "INSERT INTO users (username, password_hash, balance, is_admin) VALUES (?, ?, 10000, 1)",
            (username, pw_hash),
        )
    else:
        pw_hash = hash_password(new_password)
        conn.execute("UPDATE users SET password_hash = ? WHERE username = ?", (pw_hash, username))
        print(f"Existing user '{username}' found (is_admin={bool(row[1])}). Password updated.")

    conn.commit()
    conn.close()
    print(f"\nDone. You can now log in as:\n  username: {username}\n  password: {new_password}")


if __name__ == "__main__":
    main()
