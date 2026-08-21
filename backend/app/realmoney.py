"""
Real-money mode scaffolding.

UNICORN is play-money only today: every account gets db.STARTING_BALANCE in
fake currency at signup, and the /api/deposit endpoint in server.py is an
explicit simulation (see its comment block) — no payment processor is
connected, no identity verification happens, and balances are just a number
in this app's own SQLite file. This module is the seam for turning real
cash on *after* licensing/registration is actually in place — see the
"Real-money mode" section of README.md for the full checklist of what's
still needed (a real KYC vendor, a real payment processor, a custody or
banking partner, and the CFTC/state registration itself, per
UNICORN_Licensing_Punch_List) before REAL_MONEY_ENABLED should ever be set
true in a production deploy that real people can reach.

Nothing in this file moves real money on its own. KYCProvider and
PaymentsProvider below are deliberately swappable interfaces: the stub
implementations that ship here just record state in UNICORN's own database
so the rest of the app — endpoints, the ledger, the audit log, the frontend
— can be built and tested end-to-end today, before any real vendor account
exists. Wiring in a real vendor later means writing one new class per
interface and pointing kyc_provider / payments_provider at it; nothing else
in the app should need to change.
"""

import os

# Master switch. Defaults to false everywhere (local dev, Railway, anywhere)
# until someone deliberately sets it — flipping this alone does NOT make the
# app move real money; see PaymentsProvider below, which has its own
# separate, even-more-locked-down gate.
REAL_MONEY_ENABLED = os.environ.get("UNICORN_REAL_MONEY_ENABLED", "false").strip().lower() == "true"

# Comma-separated two-letter state codes where real-money trading is
# blocked outright, e.g. "NJ,NY,MA,NV" — see the licensing punch list's
# section on Kalshi's ongoing state-by-state cease-and-desist fights over
# sports contracts specifically. Empty by default (nothing blocked) since
# nothing is live yet; this should only ever be set to a real list handed
# down by counsel, never guessed at.
_BLOCKED_STATES = {
    s.strip().upper()
    for s in os.environ.get("UNICORN_REAL_MONEY_BLOCKED_STATES", "").split(",")
    if s.strip()
}


def state_is_blocked(state_code):
    if not state_code:
        return False
    return state_code.strip().upper() in _BLOCKED_STATES


class PaymentsNotConfiguredError(Exception):
    """Raised whenever code tries to actually move money and no real
    processor is wired in yet. Callers should surface this as a clear
    'not available yet' error, never silently swallow it."""


# ---------- KYC ----------

class KYCProvider:
    """Interface a real identity-verification vendor (Persona, Onfido,
    Stripe Identity, and similar) implements later. submit() is called once
    per user's verification attempt and must return a dict with at least
    {"status": "pending" | "verified" | "rejected", "provider": <name>,
    "provider_reference": <external id or None>}."""

    name = "base"

    def submit(self, user_id, legal_name, date_of_birth, address, state):
        raise NotImplementedError


class ManualKYCProvider(KYCProvider):
    """No real identity-verification vendor is connected yet. This records
    exactly what the user submitted and leaves it 'pending' for a human
    admin to review from the admin KYC queue (see GET/POST
    /api/admin/kyc/* in server.py). This is a placeholder for building and
    testing the surrounding flow, not a substitute for real KYC/AML checks
    at any real scale — replace with a real vendor's API before real money
    is ever actually accepted from a stranger."""

    name = "manual"

    def submit(self, user_id, legal_name, date_of_birth, address, state):
        return {"status": "pending", "provider": self.name, "provider_reference": None}


# ---------- payments ----------

class PaymentsProvider:
    """Interface a real payment processor / banking partner implements
    later. Both methods return a dict with at least {"status": "completed"
    | "pending" | "failed", "provider": <name>, "provider_reference": <id>}
    or raise PaymentsNotConfiguredError if no real integration exists."""

    name = "base"

    def create_deposit(self, user_id, amount):
        raise NotImplementedError

    def create_withdrawal(self, user_id, amount):
        raise NotImplementedError


class StubPaymentsProvider(PaymentsProvider):
    """No real payment processor is connected. This exists purely so the
    real-money deposit/withdrawal endpoints, the ledger, and the frontend
    can be built and exercised end-to-end before a processor account
    exists — it refuses to do anything at all unless
    UNICORN_ALLOW_STUB_PAYMENTS=true is set explicitly, on top of
    REAL_MONEY_ENABLED already being on, so it can never be mistaken for a
    real integration or left on by accident in a production deploy real
    users can reach."""

    name = "stub"

    def __init__(self):
        self.allow = os.environ.get("UNICORN_ALLOW_STUB_PAYMENTS", "false").strip().lower() == "true"

    def _check(self):
        if not self.allow:
            raise PaymentsNotConfiguredError(
                "No real payment processor is connected yet. UNICORN_ALLOW_STUB_PAYMENTS "
                "is for internal testing only and must never be set true in a production "
                "deploy real users can reach."
            )

    def create_deposit(self, user_id, amount):
        self._check()
        return {"status": "completed", "provider": self.name, "provider_reference": None}

    def create_withdrawal(self, user_id, amount):
        self._check()
        return {"status": "completed", "provider": self.name, "provider_reference": None}


kyc_provider = ManualKYCProvider()
payments_provider = StubPaymentsProvider()
