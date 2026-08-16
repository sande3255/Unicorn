"""Wallet-connect login: link a MetaMask (or any EIP-191-compatible wallet)
address to a UNICORN account, and optionally log in with that wallet
instead of a password.

This is the "login only" version of wallet support — connecting a wallet
never moves real money and never gives UNICORN custody of anything. It
proves the caller controls a given address (via a signed challenge) and
uses that as an alternate identity check, the same way a password does.
See the README/API.md for why UNICORN doesn't do real crypto deposits.

Why this uses `eth_account` instead of hand-rolled crypto: verifying an
Ethereum signature means recovering a public key from an ECDSA signature
over a Keccak-256 hash — real, security-sensitive cryptography. Anthropic's
sandbox this was built in has no PyPI access, so this code could not be
executed against a real MetaMask signature before reaching your machine.
Hand-rolling secp256k1/Keccak from scratch with no way to test it against
known-good vectors is exactly how subtle, silent crypto bugs happen —
so this uses `eth-account` (added to requirements.txt), a mature, widely
used library, instead. **Run `pip install -r requirements.txt` and test
an actual MetaMask connect+sign+link round trip on your machine before
relying on this** — it's the first real-world exercise this code gets.

Nonce challenges are kept in memory (not the database), consistent with
the rest of the app's single-worker-process assumption (see
ratelimit.py's docstring for why that's already a hard requirement here).
"""
import re
import secrets
import time

RE_ETH_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
CHALLENGE_TTL_SECONDS = 5 * 60

# address (lowercased) -> (message, expires_at_monotonic)
_pending_challenges = {}


def is_valid_address(address: str) -> bool:
    return bool(address) and bool(RE_ETH_ADDRESS.match(address))


def create_challenge(address: str) -> str:
    """Generates and stores a one-time sign-in message for this address,
    returning the exact text the wallet should sign via personal_sign."""
    nonce = secrets.token_hex(16)
    message = (
        "Sign in to UNICORN\n\n"
        f"Wallet: {address.lower()}\n"
        f"Nonce: {nonce}\n"
        "This request will not trigger a blockchain transaction or cost any gas."
    )
    _pending_challenges[address.lower()] = (message, time.monotonic() + CHALLENGE_TTL_SECONDS)
    return message


def _pop_valid_challenge(address: str):
    """Returns the pending message for this address if one exists and
    hasn't expired, consuming it either way (a challenge is single-use —
    once checked, it's gone, whether verification ultimately succeeds or
    not, so a captured signature can't be replayed)."""
    entry = _pending_challenges.pop(address.lower(), None)
    if entry is None:
        return None
    message, expires_at = entry
    if time.monotonic() > expires_at:
        return None
    return message


def verify_and_consume(address: str, signature: str):
    """Verifies `signature` was produced by `address` signing its current
    pending challenge (via personal_sign / EIP-191). Returns True/False.
    Always consumes the challenge (see _pop_valid_challenge) so a given
    nonce can only ever be used once, whichever way this comes out."""
    if not is_valid_address(address):
        return False
    message = _pop_valid_challenge(address)
    if message is None:
        return False  # no pending challenge, or it expired

    try:
        from eth_account import Account
        from eth_account.messages import encode_defunct
    except ImportError as e:
        raise RuntimeError(
            "Wallet login needs the 'eth-account' package (pip install -r requirements.txt) "
            "— it's listed in requirements.txt but doesn't look like it's installed yet."
        ) from e

    try:
        recovered = Account.recover_message(encode_defunct(text=message), signature=signature)
    except Exception:  # noqa: BLE001 - any malformed signature just fails verification
        return False
    return recovered.lower() == address.lower()
