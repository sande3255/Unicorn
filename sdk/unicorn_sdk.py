"""Python SDK for the UNICORN prediction market API — for bots that trade
unattended against a self-hosted UNICORN instance.

Install:
    pip install requests

Quick start:
    from unicorn_sdk import UnicornClient

    client = UnicornClient(
        api_key="unicorn_live_...",       # from the "API keys" page while logged in
        base_url="https://your-app.up.railway.app",  # or http://localhost:8000 locally
    )

    for m in client.list_open_markets():
        print(m["question"], m["price_yes"])

    client.trade(market_id=m["id"], outcome="YES", shares=10)

See API.md at the repo root for the full endpoint reference, rate limits,
and the auth model (session tokens vs. API keys). This file has no
UNICORN-specific dependencies beyond `requests` — copy it into your bot's
project, or `pip install -e sdk/` if you added a setup.py for it.
"""
import requests

DEFAULT_TIMEOUT = 10


class UnicornAPIError(Exception):
    """Raised for any non-2xx response. `status_code` and `detail` mirror
    what the server sent; `retry_after_seconds` is set on 429s (rate
    limit exceeded) so a bot can back off precisely instead of guessing."""

    def __init__(self, status_code, detail, retry_after_seconds=None):
        self.status_code = status_code
        self.detail = detail
        self.retry_after_seconds = retry_after_seconds
        super().__init__(f"[{status_code}] {detail}")


class UnicornClient:
    def __init__(self, api_key, base_url="http://localhost:8000", timeout=DEFAULT_TIMEOUT):
        if not api_key:
            raise ValueError(
                "api_key is required — log in to your UNICORN account, open the "
                "'API keys' page, and generate one."
            )
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._session = requests.Session()
        self._session.headers["Authorization"] = f"Bearer {api_key}"

    # ---------- low-level ----------

    def _request(self, method, path, **kwargs):
        url = f"{self.base_url}{path}"
        resp = self._session.request(method, url, timeout=self.timeout, **kwargs)
        try:
            data = resp.json()
        except ValueError:
            data = None
        if not resp.ok:
            detail = (data or {}).get("detail", f"Request failed ({resp.status_code})")
            retry_after = (data or {}).get("retry_after_seconds")
            raise UnicornAPIError(resp.status_code, detail, retry_after)
        return data

    # ---------- account ----------

    def me(self):
        """Returns {username, balance, is_admin} for whoever this API key
        belongs to."""
        return self._request("GET", "/api/me")

    # ---------- markets ----------

    def list_markets(self):
        """All markets (open and resolved), newest first."""
        return self._request("GET", "/api/markets")

    def list_open_markets(self, category=None):
        """Convenience wrapper: only status == 'open', optionally filtered
        to one category (e.g. 'Stocks · 5 min', 'Crypto · 15 min',
        'Indices · 15 min' — see /api/markets for the exact strings
        currently live on your instance)."""
        markets = [m for m in self.list_markets() if m["status"] == "open"]
        if category is not None:
            markets = [m for m in markets if m.get("category") == category]
        return markets

    def get_market(self, market_id):
        """Full detail for one market, including description and price
        history."""
        return self._request("GET", f"/api/markets/{market_id}")

    # ---------- trading ----------

    def trade(self, market_id, outcome, shares):
        """Buy (shares > 0) or sell (shares < 0, requires an existing
        position) YES or NO shares in a market. Raises UnicornAPIError on
        insufficient balance, a closed/resolved market, or (for a
        read-only API key) a 403. Returns the new balance, trade cost, and
        resulting price."""
        if outcome not in ("YES", "NO"):
            raise ValueError("outcome must be 'YES' or 'NO'")
        return self._request(
            "POST", f"/api/markets/{market_id}/trade",
            json={"outcome": outcome, "shares": shares},
        )

    # ---------- read-only account views ----------
    # (API keys work fine here — it's specifically the /api/api-keys
    # management endpoints that require a logged-in session instead, so a
    # leaked bot key can't mint itself siblings or revoke your visibility
    # into it.)

    def portfolio(self):
        return self._request("GET", "/api/portfolio")

    def transactions(self):
        return self._request("GET", "/api/transactions")
