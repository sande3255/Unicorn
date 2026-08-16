"""Password hashing and session tokens using only the standard library
(no bcrypt/passlib dependency needed)."""
import hashlib
import hmac
import os
import secrets

PBKDF2_ITERATIONS = 200_000


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"{salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt_hex, digest_hex = stored.split("$")
    except ValueError:
        return False
    salt = bytes.fromhex(salt_hex)
    expected = bytes.fromhex(digest_hex)
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return hmac.compare_digest(actual, expected)


def new_token() -> str:
    return secrets.token_hex(32)


# ---------- API keys (for bots / programmatic trading) ----------
#
# Separate from session tokens on purpose: sessions come from an interactive
# login and are meant to be short-lived-ish (well, this demo doesn't expire
# them either, but conceptually they are); API keys are long-lived
# credentials a user generates once and hands to a bot running unattended.
# Prefixed (like Stripe/GitHub keys) so a key is recognizable at a glance
# and greppable if one ever leaks into a log or a committed file by mistake.

API_KEY_PREFIX = "unicorn_live_"


def generate_api_key() -> tuple[str, str, str]:
    """Returns (plaintext_key, key_hash, display_prefix).

    plaintext_key is shown to the caller exactly once, at creation time,
    and never again — only key_hash is stored. display_prefix is a short,
    safe-to-store-and-show fragment (not secret on its own) so a user can
    recognize which key is which in a list without the full secret ever
    being persisted or re-displayed.
    """
    secret = secrets.token_hex(24)  # 192 bits — not brute-forceable
    plaintext = f"{API_KEY_PREFIX}{secret}"
    key_hash = hash_api_key(plaintext)
    display_prefix = plaintext[: len(API_KEY_PREFIX) + 6]
    return plaintext, key_hash, display_prefix


def hash_api_key(plaintext: str) -> str:
    """A fast hash (not PBKDF2) is the right call here: PBKDF2's deliberate
    slowness exists to resist brute-forcing *low-entropy* human passwords.
    An API key is a 192-bit random token nobody has to memorize — brute
    force is already infeasible regardless of hash speed, so a fast SHA-256
    lookup (indexable, cheap to check on every request) is both secure and
    the more practical choice for a credential checked on every API call."""
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()
