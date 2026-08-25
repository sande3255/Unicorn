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


class StripeIdentityKYCProvider(KYCProvider):
    """Real automated identity verification via Stripe Identity —
    deliberately reuses STRIPE_SECRET_KEY, the same credential already set
    for StripePaymentsProvider (deposits), rather than asking for a second
    processor account. $1.50/document+selfie check, pay-as-you-go, per
    Stripe's published pricing as of when this was wired up.

    Unlike ManualKYCProvider, submit() can't hand back a final answer by
    itself — nothing has actually verified anything yet at that point. It
    creates a VerificationSession and returns its client_secret so the
    frontend can launch Stripe's own hosted document+selfie capture flow
    (see getStripeJsInstance()/verifyIdentity() in app.js). The real status
    change happens later, out of band, when Stripe's webhook posts
    identity.verification_session.verified (or .requires_input for a
    failed/retryable attempt) to /api/webhooks/stripe — see
    _finalize_stripe_kyc_session() in server.py. Until that webhook
    arrives, status stays 'pending' exactly like the manual path, so
    nothing else in the app needs to know or care which KYC provider is
    active."""

    name = "stripe_identity"

    def __init__(self):
        secret_key = os.environ.get("STRIPE_SECRET_KEY", "").strip()
        if not secret_key:
            raise PaymentsNotConfiguredError("STRIPE_SECRET_KEY isn't set — Stripe Identity needs it.")
        import stripe
        stripe.api_key = secret_key
        self._stripe = stripe

    def submit(self, user_id, legal_name, date_of_birth, address, state):
        session = self._stripe.identity.VerificationSession.create(
            type="document",
            options={"document": {"require_matching_selfie": True}},
            metadata={"unicorn_user_id": str(user_id)},
        )
        return {
            "status": "pending",
            "provider": self.name,
            "provider_reference": session.id,
            "client_secret": session.client_secret,
        }


def _build_kyc_provider():
    """Stripe Identity whenever this deploy already has STRIPE_SECRET_KEY
    set for payments — no separate credential to configure — otherwise
    the manual admin-review placeholder. Falls back to manual rather than
    raising if something about Stripe's SDK/API rejects at construction
    time, since KYC still needs to work (via a human reviewer) even on a
    deploy that hasn't set up Stripe at all."""
    if os.environ.get("STRIPE_SECRET_KEY", "").strip():
        try:
            return StripeIdentityKYCProvider()
        except Exception:
            pass
    return ManualKYCProvider()


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


class PayPalPayoutsClient:
    """Shared withdrawal path for every real deposit provider below.

    Neither Braintree nor Stripe has a general "send money to a customer"
    API in the mode we use them (they're built to accept payments, not
    originate them) — Stripe's equivalent (Connect payouts) is a whole
    separate onboarding flow per recipient that isn't built here. PayPal
    Payouts is the one product that just sends money to an email address,
    so both BraintreePaymentsProvider and StripePaymentsProvider delegate
    withdrawals to one instance of this class, regardless of which
    processor a user deposited through. It cannot push money to an
    arbitrary card — only to a PayPal or Venmo account identified by
    email/phone. Requires PAYPAL_PAYOUTS_CLIENT_ID, PAYPAL_PAYOUTS_SECRET,
    PAYPAL_ENVIRONMENT ("sandbox" or "live")."""

    name = "paypal_payouts"

    def __init__(self):
        self._client_id = os.environ.get("PAYPAL_PAYOUTS_CLIENT_ID", "").strip()
        self._secret = os.environ.get("PAYPAL_PAYOUTS_SECRET", "").strip()
        env_name = os.environ.get("PAYPAL_ENVIRONMENT", "sandbox").strip().lower()
        self._base = "https://api-m.paypal.com" if env_name == "live" else "https://api-m.sandbox.paypal.com"

    def _access_token(self):
        resp = requests.post(
            f"{self._base}/v1/oauth2/token",
            auth=(self._client_id, self._secret),
            data={"grant_type": "client_credentials"},
            timeout=15,
        )
        if resp.status_code != 200:
            raise PaymentsNotConfiguredError(f"PayPal Payouts auth failed ({resp.status_code}): {resp.text[:300]}")
        return resp.json()["access_token"]

    def send_payout(self, user_id, amount, payout_email=None):
        if not (self._client_id and self._secret):
            raise PaymentsNotConfiguredError(
                "PayPal Payouts isn't configured — PAYPAL_PAYOUTS_CLIENT_ID and "
                "PAYPAL_PAYOUTS_SECRET must be set to send withdrawals."
            )
        if not payout_email:
            raise PaymentsNotConfiguredError(
                "A PayPal or Venmo email is required to receive a withdrawal — "
                "neither Braintree nor Stripe here can push money to an arbitrary card."
            )
        token = self._access_token()
        sender_batch_id = f"unicorn-w-{user_id}-{int(amount * 100)}-{os.urandom(4).hex()}"
        resp = requests.post(
            f"{self._base}/v1/payments/payouts",
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
        # "completed" from UNICORN's side (money is committed), matching how deposits treat
        # settlement as fire-and-forget once the processor accepts the transaction.
        return {
            "status": status, "provider": self.name,
            "provider_reference": (body.get("batch_header") or {}).get("payout_batch_id"),
        }


class BraintreePaymentsProvider(PaymentsProvider):
    """Real payment processing via Braintree (a PayPal company).

    Deposits go through Braintree's Drop-in UI, which — once Venmo and
    PayPal are enabled on the Braintree control panel and Apple Pay is
    verified there — supports cards, Apple Pay, Venmo, and PayPal in a
    single integration. The frontend collects a one-time
    `payment_method_nonce` from Drop-in and sends it here; this class
    turns that nonce into an actual charge via Transaction.sale().

    Withdrawals go through PayPalPayoutsClient (see above) — not anything
    Braintree-specific.

    Requires env vars: BRAINTREE_MERCHANT_ID, BRAINTREE_PUBLIC_KEY,
    BRAINTREE_PRIVATE_KEY, BRAINTREE_ENVIRONMENT ("sandbox" or
    "production").
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
        self._payouts = PayPalPayoutsClient()

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

    def create_withdrawal(self, user_id, amount, payout_email=None, **kwargs):
        return self._payouts.send_payout(user_id, amount, payout_email)


class StripePaymentsProvider(PaymentsProvider):
    """Real payment processing via Stripe.

    Stripe's payment-methods list (checked directly against their current
    docs) covers cards, Apple Pay, Google Pay, and Link — notably not
    Venmo, which is why Braintree stays in this app too rather than being
    replaced. Deposits use Stripe's modern "deferred" Payment Element
    flow, which is a two-step handshake rather than Braintree's one-shot
    nonce:

      1. Frontend asks the backend for a PaymentIntent (create_payment_intent
         below) *before* the user has entered payment details, and gets back
         a client_secret. A `real_money_transactions` row is inserted
         'pending' at this point (see server.py) so nothing is lost if the
         browser tab closes mid-payment.
      2. Frontend collects card/Apple Pay/Google Pay/Link details with
         Stripe.js's Payment Element and confirms the PaymentIntent
         client-side (stripe.confirmPayment) — this is also where Stripe
         handles any 3D Secure / SCA challenge, entirely in the browser.
      3. Frontend calls the backend's finalize step, which re-checks the
         PaymentIntent's status directly with Stripe (check_deposit_status
         below) rather than trusting whatever the client reports, and only
         then credits real_balance.

    Step 3 has a gap on its own: if the browser tab closes right after
    paying but before that follow-up call fires, the deposit would sit
    'pending' forever even though Stripe actually has the money.
    verify_webhook_event below (used by /api/webhooks/stripe in server.py)
    covers that — Stripe calls that endpoint directly whenever a
    PaymentIntent's status changes, independent of whatever the browser
    does afterward. Both paths funnel into the same idempotent row-update
    logic (_finalize_stripe_deposit_row in server.py), so it doesn't matter
    which one gets there first or if both do.

    Withdrawals go through PayPalPayoutsClient (see above), exactly like
    Braintree's — Stripe's own payout mechanism (Connect) requires each
    recipient to complete their own onboarding with Stripe and isn't wired
    in here.

    Requires env vars: STRIPE_SECRET_KEY, STRIPE_PUBLISHABLE_KEY, and for
    the webhook specifically: STRIPE_WEBHOOK_SECRET (from the webhook
    endpoint's settings in the Stripe Dashboard — this is what lets
    verify_webhook_event tell a real Stripe request apart from anyone else
    POSTing to that public URL claiming a payment succeeded).
    """

    name = "stripe"

    def __init__(self):
        import stripe

        secret_key = os.environ.get("STRIPE_SECRET_KEY", "").strip()
        if not secret_key:
            raise PaymentsNotConfiguredError("Stripe isn't configured — STRIPE_SECRET_KEY must be set.")
        self._stripe = stripe
        self._stripe.api_key = secret_key
        self._publishable_key = os.environ.get("STRIPE_PUBLISHABLE_KEY", "").strip()
        self._webhook_secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip()
        self._payouts = PayPalPayoutsClient()

    def publishable_key(self):
        """For the frontend to initialize Stripe.js with. Safe to expose
        publicly by design — it's Stripe's publishable, not secret, key."""
        if not self._publishable_key:
            raise PaymentsNotConfiguredError("STRIPE_PUBLISHABLE_KEY isn't set.")
        return self._publishable_key

    def verify_webhook_event(self, payload, sig_header):
        """Verifies an incoming webhook request is actually signed by
        Stripe with this deploy's STRIPE_WEBHOOK_SECRET before trusting
        anything in it — without this check, /api/webhooks/stripe would be
        a public URL anyone could POST to and claim a deposit succeeded.
        Returns the verified event (raises stripe's own
        SignatureVerificationError on a bad signature — the caller in
        server.py treats any exception here as "reject the request")."""
        if not self._webhook_secret:
            raise PaymentsNotConfiguredError(
                "STRIPE_WEBHOOK_SECRET isn't set — can't verify webhook requests are really from Stripe."
            )
        return self._stripe.Webhook.construct_event(payload, sig_header, self._webhook_secret)

    def create_payment_intent(self, user_id, amount):
        intent = self._stripe.PaymentIntent.create(
            amount=int(round(amount * 100)),
            currency="usd",
            automatic_payment_methods={"enabled": True},
            metadata={"unicorn_user_id": str(user_id)},
        )
        return {"client_secret": intent.client_secret, "payment_intent_id": intent.id}

    def check_deposit_status(self, payment_intent_id):
        """Called after the frontend finishes Stripe.js confirmation, to
        verify server-side (never trust the client's word alone) before
        crediting real_balance."""
        intent = self._stripe.PaymentIntent.retrieve(payment_intent_id)
        if intent.status == "succeeded":
            return {"status": "completed", "provider": self.name, "provider_reference": intent.id}
        if intent.status in ("requires_action", "requires_confirmation", "processing"):
            return {"status": "pending", "provider": self.name, "provider_reference": intent.id}
        return {
            "status": "failed", "provider": self.name, "provider_reference": intent.id,
            "detail": f"Stripe PaymentIntent status: {intent.status}",
        }

    def create_deposit(self, user_id, amount, **kwargs):
        # Deposits are two-step for Stripe (see class docstring) — server.py
        # calls create_payment_intent + check_deposit_status directly rather
        # than this single-call method, which only exists to satisfy the
        # PaymentsProvider interface.
        raise NotImplementedError(
            "StripePaymentsProvider uses create_payment_intent + check_deposit_status, "
            "not create_deposit — see /api/real-money/stripe/* in server.py."
        )

    def create_withdrawal(self, user_id, amount, payout_email=None, **kwargs):
        return self._payouts.send_payout(user_id, amount, payout_email)


def _build_deposit_providers():
    """Builds one entry per configured real processor — both can be active
    side by side, so users see a choice (e.g. "Venmo? use Braintree" vs
    "Google Pay? use Stripe") rather than the app picking one for them.
    Falls back to the inert stub only when NEITHER is configured. Never
    silently swaps in the stub when a processor's env vars are present but
    broken — that should surface as an error, not fake success."""
    providers = {}
    if os.environ.get("BRAINTREE_MERCHANT_ID", "").strip():
        providers["braintree"] = BraintreePaymentsProvider()
    if os.environ.get("STRIPE_SECRET_KEY", "").strip():
        providers["stripe"] = StripePaymentsProvider()
    if not providers:
        providers["stub"] = StubPaymentsProvider()
    return providers


kyc_provider = _build_kyc_provider()
deposit_providers = _build_deposit_providers()

# Withdrawals aren't processor-specific (both real providers delegate to the
# same PayPalPayoutsClient) — this just picks whichever real provider is
# configured to handle /api/real-money/withdraw, preferring Braintree
# arbitrarily when both are present since either behaves identically for
# withdrawal purposes. Kept as a single name, `payments_provider`, since
# server.py's withdraw endpoint only ever needs one.
payments_provider = deposit_providers.get("braintree") or deposit_providers.get("stripe") or deposit_providers["stub"]
