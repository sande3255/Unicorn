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

import requests

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

    def create_deposit(self, user_id, amount, **kwargs):
        raise NotImplementedError

    def create_withdrawal(self, user_id, amount, **kwargs):
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

    def create_deposit(self, user_id, amount, **kwargs):
        self._check()
        return {"status": "completed", "provider": self.name, "provider_reference": None}

    def create_withdrawal(self, user_id, amount, **kwargs):
        self._check()
        return {"status": "completed", "provider": self.name, "provider_reference": None}


class BraintreePaymentsProvider(PaymentsProvider):
    """Real payment processing via Braintree (a PayPal company).

    Deposits go through Braintree's Drop-in UI, which — once Venmo and
    PayPal are enabled on the Braintree control panel and Apple Pay is
    verified there — supports cards, Apple Pay, Venmo, and PayPal in a
    single integration. The frontend collects a one-time
    `payment_method_nonce` from Drop-in and sends it here; this class
    turns that nonce into an actual charge via Transaction.sale().

    Withdrawals are a different story worth being explicit about: Braintree
    itself has no general "send money to a customer" API — it's built to
    accept payments, not originate them. Paying real users back out uses a
    separate PayPal product, PayPal Payouts, which sends funds to a PayPal
    or Venmo account identified by email/phone. It cannot push money to an
    arbitrary card — that would require Visa Direct / Mastercard Send,
    which needs its own underwriting approval from Braintree/PayPal and
    isn't wired in here. So today: users can deposit with card, Apple Pay,
    Venmo, or PayPal, but can only withdraw to a PayPal or Venmo account
    they provide by email — not straight back to a card.

    Requires env vars: BRAINTREE_MERCHANT_ID, BRAINTREE_PUBLIC_KEY,
    BRAINTREE_PRIVATE_KEY, BRAINTREE_ENVIRONMENT ("sandbox" or
    "production"), and for withdrawals: PAYPAL_PAYOUTS_CLIENT_ID,
    PAYPAL_PAYOUTS_SECRET, PAYPAL_ENVIRONMENT ("sandbox" or "live").
    """

    name = "braintree"

    def __init__(self):
        import braintree as bt

        self._bt = bt
        env_name = os.environ.get("BRAINTREE_ENVIRONMENT", "sandbox").strip().lower()
        bt_environment = bt.Environment.Production if env_name == "production" else bt.Environment.Sandbox
        merchant_id = os.environ.get("BRAINTREE_MERCHANT_ID", "").strip()
        public_key = os.environ.get("BRAINTREE_PUBLIC_KEY", "").strip()
        private_key = os.environ.get("BRAINTREE_PRIVATE_KEY", "").strip()
        if not (merchant_id and public_key and private_key):
            raise PaymentsNotConfiguredError(
                "Braintree isn't configured — BRAINTREE_MERCHANT_ID, BRAINTREE_PUBLIC_KEY, "
                "and BRAINTREE_PRIVATE_KEY must all be set."
            )
        self.gateway = bt.BraintreeGateway(
            bt.Configuration(
                environment=bt_environment,
                merchant_id=merchant_id,
                public_key=public_key,
                private_key=private_key,
            )
        )

        self._paypal_client_id = os.environ.get("PAYPAL_PAYOUTS_CLIENT_ID", "").strip()
        self._paypal_secret = os.environ.get("PAYPAL_PAYOUTS_SECRET", "").strip()
        paypal_env = os.environ.get("PAYPAL_ENVIRONMENT", "sandbox").strip().lower()
        self._paypal_base = (
            "https://api-m.paypal.com" if paypal_env == "live" else "https://api-m.sandbox.paypal.com"
        )

    def client_token(self):
        """For the frontend to initialize Braintree's Drop-in UI with."""
        return self.gateway.client_token.generate()

    def create_deposit(self, user_id, amount, payment_method_nonce=None, **kwargs):
        if not payment_method_nonce:
            raise PaymentsNotConfiguredError("payment_method_nonce is required to process a deposit.")
        result = self.gateway.transaction.sale({
            "amount": f"{amount:.2f}",
            "payment_method_nonce": payment_method_nonce,
            "options": {"submit_for_settlement": True},
        })
        if result.is_success:
            txn = result.transaction
            status = "completed" if txn.status in ("submitted_for_settlement", "settled") else "pending"
            return {"status": status, "provider": self.name, "provider_reference": txn.id}
        message = result.message or "Deposit was declined."
        return {"status": "failed", "provider": self.name, "provider_reference": None, "detail": message}

    def _paypal_access_token(self):
        resp = requests.post(
            f"{self._paypal_base}/v1/oauth2/token",
            auth=(self._paypal_client_id, self._paypal_secret),
            data={"grant_type": "client_credentials"},
            timeout=15,
        )
        if resp.status_code != 200:
            raise PaymentsNotConfiguredError(f"PayPal Payouts auth failed ({resp.status_code}): {resp.text[:300]}")
        return resp.json()["access_token"]

    def create_withdrawal(self, user_id, amount, payout_email=None, **kwargs):
        if not (self._paypal_client_id and self._paypal_secret):
            raise PaymentsNotConfiguredError(
                "PayPal Payouts isn't configured — PAYPAL_PAYOUTS_CLIENT_ID and "
                "PAYPAL_PAYOUTS_SECRET must be set to send withdrawals."
            )
        if not payout_email:
            raise PaymentsNotConfiguredError(
                "A PayPal or Venmo email is required to receive a withdrawal — "
                "Braintree/PayPal can't push money to an arbitrary card."
            )
        token = self._paypal_access_token()
        sender_batch_id = f"unicorn-w-{user_id}-{int(amount * 100)}-{os.urandom(4).hex()}"
        resp = requests.post(
            f"{self._paypal_base}/v1/payments/payouts",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={
                "sender_batch_header": {
                    "sender_batch_id": sender_batch_id,
                    "email_subject": "UNICORN withdrawal",
                },
                "items": [{
                    "recipient_type": "EMAIL",
                    "amount": {"value": f"{amount:.2f}", "currency": "USD"},
                    "receiver": payout_email,
                    "note": "UNICORN account withdrawal",
                    "sender_item_id": sender_batch_id,
                }],
            },
            timeout=15,
        )
        if resp.status_code not in (200, 201):
            return {
                "status": "failed", "provider": self.name, "provider_reference": None,
                "detail": f"PayPal Payouts rejected the request ({resp.status_code}): {resp.text[:300]}",
            }
        body = resp.json()
        batch_status = (body.get("batch_header") or {}).get("batch_status", "PENDING")
        status = "completed" if batch_status in ("SUCCESS", "PROCESSING", "PENDING") else "pending"
        # Payouts are asynchronous on PayPal's side even when accepted here — PENDING/PROCESSING
        # still means the batch was accepted, not that cash has landed yet. Treat all three as
        # "completed" from UNICORN's side (money is committed), matching how the deposit path
        # treats settlement as fire-and-forget once Braintree accepts the transaction.
        return {
            "status": status, "provider": self.name,
            "provider_reference": (body.get("batch_header") or {}).get("payout_batch_id"),
        }


def _build_payments_provider():
    """Prefers a real Braintree integration when it's configured; falls back to
    the inert stub otherwise (local dev, or a deploy that hasn't set up a
    processor yet). Never silently prefers the stub once real credentials are
    present — if Braintree env vars are set but something's wrong with them,
    that should surface as an error, not a silent fallback to fake payments."""
    if os.environ.get("BRAINTREE_MERCHANT_ID", "").strip():
        return BraintreePaymentsProvider()
    return StubPaymentsProvider()


kyc_provider = ManualKYCProvider()
payments_provider = _build_payments_provider()
