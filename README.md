# UNICORN (play-money demo)

A working prediction-market application in the style of Kalshi/Polymarket:
users trade YES/NO shares on event outcomes, prices move automatically via
an AMM, and markets settle for $1 (winning side) or $0 (losing side).
Alongside markets an admin creates by hand, UNICORN also runs a menu of
**auto-generated crypto, stock, and commodity markets** on a rolling 5- and
15-minute clock, settled against real live prices, and **imports trending
markets from Kalshi and Polymarket** — real questions, seeded at their real
current odds, auto-resolved here once the real market settles.

**This runs entirely on play money. There is no payment processor, no real
currency, and no connection to any bank or wallet.** Read "Before this can
take real money" below before you even think about changing that.

## What's included

- `backend/` — a Flask + SQLite API (Python standard library only, plus
  Flask — no other third-party dependencies). Implements:
  - Signup/login with hashed passwords and session tokens
  - A Logarithmic Market Scoring Rule (LMSR) automated market maker — the
    same style of pricing mechanism real prediction/forecasting markets use,
    so prices move up/down automatically as people trade
  - Manual market creation and resolution (admin only)
  - **A background scheduler** (`app/scheduler.py`) that opens new timed
    markets automatically and resolves them at expiry against a live price
  - **A live price feed** (`app/price_feed.py`) — CoinGecko's public API
    for crypto, Yahoo Finance's public chart endpoint for stocks/
    commodities/indices/forex, neither needing an API key (crypto
    intentionally isn't Binance — see "Why CoinGecko, not Binance" below)
  - **A Kalshi/Polymarket importer** (`app/external_markets.py`) — reads
    both platforms' public market-data APIs (no API key, no real trading
    against either platform) and mirrors real questions + real odds as
    ordinary UNICORN markets
  - Portfolio and trade history per user
  - **A REST API + API keys for bots** — generate a long-lived key from the
    "API keys" page and trade programmatically against the same endpoints
    the web UI uses. See `API.md` and the `sdk/` folder.
  - **Wallet-connect login** — link a MetaMask address from the "Account"
    page and log in with it instead of a password. Proves wallet
    ownership only; no real crypto ever moves. See "Wallet-connect login"
    below.
- `frontend/` — a single-page app in plain HTML/CSS/JS. No build step, no
  npm, no external CDN dependencies — open it and it works. Markets list,
  market detail with a live price chart, live underlying price + countdown
  for timed markets, a trade widget, portfolio, leaderboard, trade history,
  an API keys page, and an admin panel.
- `sdk/` — a small Python client (`unicorn_sdk.py`, `requests` is its only
  dependency) plus a runnable example bot (`example_bot.py`). See `API.md`.

## Running it locally

Requires Python 3.9+, pip, and **outbound internet access** (needed for the
scheduler to reach CoinGecko, Yahoo Finance, Kalshi, and Polymarket — without
it, the timed and imported markets just won't appear, but manual markets and
everything else still work fine).

```bash
cd backend
pip install -r requirements.txt   # just Flask
python3 run.py
```

Then open **http://localhost:8000** — the Flask server serves both the API
and the frontend, so there's nothing else to start.

A default admin account is created automatically on first run:

- username: `admin`
- password: `admin123`

**Change this password before showing this to anyone else** — set the
`UNICORN_ADMIN_USERNAME` / `UNICORN_ADMIN_PASSWORD` environment variables
before first run (they only take effect the first time the admin account
is created; changing them later doesn't retroactively change an existing
admin's password). Log in as admin to create markets from the Admin page;
any account can sign up and trade (new signups start with $10,000 in play
money).

The database is a single SQLite file (`backend/predictmarket.db` by
default, or wherever `UNICORN_DB_PATH` points), created automatically.
Delete it to reset all data. If you're upgrading from an older copy of
this project, your existing database is migrated in place — new columns
are added automatically on startup and no data is lost.

## Deploying to Railway

The dev server (`python3 run.py`) is fine for your own machine but isn't
meant to be reachable by anyone else. To put UNICORN on a real URL:

1. **Push this project to a GitHub repo** (Railway deploys from a repo —
   or use `railway up` from the Railway CLI to deploy this folder
   directly without GitHub, if you'd rather skip that step).
2. **Create a new Railway project** from that repo (railway.app → New
   Project → Deploy from GitHub repo). Railway auto-detects this as a
   Python app from the root `requirements.txt` and runs the `Procfile`'s
   `web` process — nothing else to configure for the build.
3. **Add a Volume** (your service → Volumes → New Volume) mounted at
   `/data`. Without this, the SQLite database resets to empty on every
   redeploy — Railway's container filesystem is ephemeral by default and
   this app's database is just a file on disk.
4. **Set environment variables** (your service → Variables):
   - `UNICORN_DB_PATH` = `/data/predictmarket.db` — points the app at
     the persistent volume instead of the default in-container path.
   - `UNICORN_ADMIN_USERNAME` and `UNICORN_ADMIN_PASSWORD` — set a real
     password here. **Don't skip this once the app has a public URL** —
     the default `admin`/`admin123` goes from a harmless localhost
     convenience to anyone-can-log-in-as-admin-and-resolve-markets on a
     real internet address.
5. **Deploy, then generate a domain** (Settings → Networking → Generate
   Domain). Railway assigns a `*.up.railway.app` URL — that's your live
   site.

**Why the Procfile pins `--workers 1`:** the background scheduler (the
thing that opens/resolves the 48 timed markets and syncs Kalshi/Polymarket
imports) runs as an in-process thread that starts when the app module
loads. Multiple gunicorn worker processes would each start their own copy
of that thread — duplicate calls to CoinGecko/Yahoo/Kalshi/Polymarket every
tick, and threads racing to open the same market at once. One worker with
several threads (`--threads 8`) handles concurrent HTTP requests fine at
this scale without that problem. If you ever need more request throughput
than one worker gives you, move the scheduler into its own process/service
first (e.g. a second Railway service running just `scheduler.run_loop()`)
— don't just raise `--workers`, since that reintroduces the duplicate-
thread problem.

**One upside of deploying**: this project was built and tested in a sandbox
with outbound network access blocked, so the live price feeds and
Kalshi/Polymarket imports have been running in "fails gracefully, logs,
and skips" mode this whole time — the code path was verified with mocked
responses, but never against the real APIs. On Railway those calls will
actually reach CoinGecko/Yahoo/Kalshi/Polymarket, so the timed markets and
imports should start populating for real within the first tick or two
after deploy. Watch the deploy logs the first few minutes to confirm.

## The auto-generated timed markets

Defined in `backend/app/scheduler.py`'s `AUTO_MARKET_CONFIGS` list — a
**fixed, curated roster of 48 templates**, and (with Kalshi/Polymarket
imports switched off by default, see below) effectively the entire board.
The philosophy: a short, recognizable, "definitive" list of American names
rather than a sprawling long tail — nobody has time to research an obscure
altcoin or sit around waiting on a slow real-world event to resolve. It's
all the same format — "will this be above or below its current price in N
minutes" — across three asset classes:

| Group | Assets | Interval |
|---|---|---|
| Stocks · 5 min | AAPL, MSFT, NVDA, AMZN, GOOGL, META, TSLA, AVGO, JPM, V, NFLX, DIS | every 5 minutes |
| Stocks · 15 min | same 12 stocks as above | every 15 minutes |
| Crypto · 5 min | BTC, ETH, SOL, XRP, DOGE, BNB, ADA, AVAX, LINK, LTC | every 5 minutes |
| Crypto · 15 min | same 10 coins as above | every 15 minutes |
| Indices · 15 min | S&P 500, Nasdaq Composite, Dow Jones, Russell 2000 | every 15 minutes |

That's 24 + 20 + 4 = **48 open market slots**, each a clear win-or-lose
call inside 5 or 15 minutes. Every 15 seconds, the scheduler: makes sure each template has one open
market live (creating a new one — "Will X be above $current_price in N
minutes?" — if the last one closed), refreshes the live price shown on
each open market, and resolves anything past its close time by comparing
the price at settlement to the strike price it opened at. Edit the config
list to add, remove, or retime markets — nothing else needs to change.

**Stocks and indices both trade off Yahoo Finance's chart endpoint**
(`price_feed.py`'s `get_stock_price`/`get_index_price` are thin aliases
over the same lookup used for commodities, kept in the codebase even
though commodities/forex aren't in the default roster anymore — see
below). Outside US market hours (nights, weekends, holidays) stocks and
indices return the last traded price rather than a live tick, so a
5-minute stock market opened at 2am ET will likely settle at the same
price it opened at (a "tie" resolves NO here, i.e. price must be strictly
*above* strike to pay YES) — this is a demo data-feed limitation, not a
bug.

**16 unique symbols now hit Yahoo Finance's endpoint every 15-second
tick** (12 stocks + 4 indices) — comfortably fine for a demo, but if you
see rate-limit errors in the logs after adding more assets, either raise
`TICK_SECONDS` or trim `AUTO_MARKET_CONFIGS`.

**Trimmed from the default roster (still supported by the code, just not
listed in `AUTO_MARKET_CONFIGS` today):** the long tail of altcoins
(MATIC, NEAR, APT, ARB, OP, DOT, TRX, ATOM, UNI, SHIB, INJ, FIL, ICP, ETC,
XLM), commodities (Gold, Silver, Crude Oil, Brent Crude, Natural Gas,
Copper, Platinum, Palladium, Corn), forex pairs (EUR/USD, GBP/USD,
USD/JPY, USD/CHF, AUD/USD), and 3 stocks (WMT, BAC, AMD). `price_feed.py`
still knows how to price all of them — add any back into
`AUTO_MARKET_CONFIGS` (and bump `_STOCKS`/`_CRYPTO` or reintroduce the
commodity/forex config blocks) if you want a broader board again.

### Why CoinGecko, not Binance

Crypto prices come from CoinGecko's public API, not Binance, on purpose.
Binance's own site (`api.binance.com`) blocks requests from US IP
addresses — Binance.US is a legally separate entity with its own,
different API — so any US-based person running this app would see every
crypto price fetch fail silently and every crypto market template just
never open (caught and logged by the scheduler's error handling, not a
crash, but also not obvious unless you're watching the logs). CoinGecko
doesn't have that restriction. `price_feed.py` batches all 25 crypto
symbols into a single CoinGecko request on a ~12-second cache, rather than
firing one request per symbol per tick — friendlier to CoinGecko's free
rate limit, and it also means fewer round-trips overall than the old
per-symbol Binance calls did.

## Keeping the board 100% fast, definitive markets

The 48 timed templates always have exactly one open market each — churning
every 5-15 minutes keeps that count constant. Kalshi/Polymarket imports
don't churn the same way (a real-world market can stay open for weeks or
months, the "sit around for a baseball game" problem), so `scheduler.py`
sets `EXTERNAL_IMPORT_MAX_OPEN_TOTAL = 0` by default — imports are fully
switched off, and the open board is 100% the 5/15-minute up-or-down
format. Raise that constant above 0 (see "Markets imported from Kalshi &
Polymarket" below) if you want a slice of slower real-world markets back;
the import code itself is untouched and ready to go, it's just not called
with any budget by default.

**Worth knowing:** ultra-short-duration price markets like these sit close
to binary options, a product regulators scrutinize specifically because of
how it's structured (see the section below) — and the fast repeat-cycle
format is worth being mindful of for the same reason rapid-cycle betting
generally is. Nothing about that changes because it's automated instead of
manual.

## Markets imported from Kalshi & Polymarket (off by default)

**As of the curated-roster change, this is switched off** —
`EXTERNAL_IMPORT_MAX_OPEN_TOTAL = 0` in `scheduler.py`, so no network
calls to Kalshi/Polymarket are made and no imported markets ever open;
`sync_external_markets` still resolves any imports left over from before
this was disabled, but stops there. The rest of this section describes
what happens if you raise that constant back above 0.

Every ~5 minutes, `backend/app/external_markets.py` reads both platforms'
public, unauthenticated market-data endpoints and `scheduler.py` mirrors
what it finds:

- **New markets**: imported as ordinary UNICORN markets, seeded at the
  source's real current probability (via `amm.seed_shares_for_price`, so
  day one's price matches reality instead of always starting at 50%).
  Each one is clearly labeled with its source, links back to the original,
  and states plainly that trading it uses UNICORN's own play money and has no
  effect on the real market. Capped at `EXTERNAL_IMPORT_MAX_OPEN_TOTAL`
  open at a time, combined across both sources — see "Keeping the board
  100% fast, definitive markets" above for why that's 0 today.
- **Resolution**: every open imported market is checked against its source
  each sync. Once Kalshi marks a market finalized (or Polymarket's price
  has converged to within 2% of 0 or 100, since Polymarket's public API
  doesn't always expose a clean "resolved" flag the same way), UNICORN
  resolves its mirror to match and pays out automatically — same as any
  other market.
- **No duplicate imports**: each external market is only ever imported
  once (tracked by source + its ticker/ID), even after it resolves.

Tune it in `scheduler.py`: `EXTERNAL_SYNC_EVERY_N_TICKS` (how often to
sync), `EXTERNAL_IMPORT_MAX_OPEN_TOTAL` (how many can be open at once,
combined), `EXTERNAL_LIQUIDITY_B` (liquidity depth for imported markets).

**Worth knowing:** both endpoints are the same public, undocumented-for-
this-purpose APIs community dashboards commonly read from — not a
stable, versioned integration. They can change shape, rate-limit, or start
requiring auth without notice; every parse is defensive (skip and log
rather than crash) but don't build anything load-bearing on top of this
without checking it's still working. UNICORN is not affiliated with, endorsed
by, or operated by Kalshi or Polymarket — every imported market says so.

## Trading via the API (for bots)

Anyone can generate an API key (from the "API keys" page, once logged in)
and trade programmatically instead of clicking through the UI — the
standard "self-hosted bot against a documented REST API" model most real
exchanges offer, rather than UNICORN running arbitrary uploaded code
itself. Full endpoint reference, auth model, and rate limits are in
`API.md`; a small Python client and a runnable example bot are in `sdk/`:

```python
from unicorn_sdk import UnicornClient

client = UnicornClient(api_key="unicorn_live_...", base_url="http://localhost:8000")
for m in client.list_open_markets():
    print(m["question"], m["price_yes"])
client.trade(market_id=m["id"], outcome="YES", shares=10)
```

API keys can be marked read-only (`can_trade: false`) for a bot that
should only watch, and managing keys themselves (creating/listing/
revoking) always requires being logged in — an API key can't do that,
so a leaked key can trade with your play money but can't cover its
tracks by minting siblings or revoking the key you'd use to notice it.

## Wallet-connect login (no real crypto involved)

From the **Account** page, a user can link a MetaMask (or any injected
EIP-1193 wallet) address to their account, and from then on log in with
that wallet instead of a password — the standard "connect wallet, sign a
message, prove you control the address" flow, implemented with
`app/wallet_auth.py`.

**This is explicitly the login-only version, not real crypto deposits.**
Linking a wallet proves you control an address; it never gives UNICORN
custody of anything, and balances stay UNICORN's own play money either
way. See "Before this can take real money" below for why actual
deposits/withdrawals are a much bigger (and currently out of scope)
undertaking.

**One thing to know before you rely on this:** verifying a wallet's
signature is real cryptography (secp256k1 + Keccak-256), so
`wallet_auth.py` uses the well-audited `eth-account` library (now in
`requirements.txt`) rather than a hand-rolled implementation — that part
was a deliberate call, not a shortcut. What that means practically: this
project was built in a sandbox with no PyPI access, so `eth-account`
could never actually be installed or exercised against a real MetaMask
signature there — the endpoint wiring, database migration, and frontend
flow were all tested end-to-end (including what happens when the package
is missing, which fails with a clear error rather than crashing), but the
real signature verification is getting its first live test on your
machine. Run `pip install -r requirements.txt`, connect a real wallet,
and confirm link/login actually work before trusting this in front of
other people.

## How the pricing works

Each market has a liquidity parameter `b`. Buying shares of an outcome
moves that outcome's price up (and the other side's price down by the same
amount, since they sum to 100%); selling moves it back down. Higher `b`
means deeper liquidity — prices move less per trade, but the market itself
can lose more in the worst case (bounded by `b × ln 2`). This is set per
market when an admin creates it (or via `AUTO_MARKET_CONFIGS` for timed
markets).

## Before this can take real money

This app is a working *mechanism*, not a licensed business. Kalshi and
Polymarket US are legal only because they operate as CFTC-regulated
exchanges. Taking real bets on real-world events without that is operating
an unregistered derivatives exchange (or illegal bookmaking, depending on
structure) — a real legal problem regardless of code quality. Short-dated
crypto/commodity contracts specifically resemble binary options, a product
category the SEC and CFTC have targeted with extra scrutiny due to a
history of fraud — expect more regulatory attention here, not less. Before
connecting real money, at minimum you'd need:

- **Regulatory registration** — CFTC registration as a Designated Contract
  Market (or a similar path in your jurisdiction), or a white-label
  partnership with an already-licensed exchange. This is the big one and
  should come before any of the items below.
- **KYC/AML** — identity verification and anti-money-laundering checks on
  every user before they can deposit or withdraw real funds.
- **A real payment processor** — one willing to work with a regulated
  derivatives/prediction-market business; most general processors won't.
- **Geofencing / eligibility checks** — certain states currently restrict or
  litigate against prediction markets (particularly sports-related and
  short-dated contracts); you'd need to block ineligible users by
  jurisdiction and keep that list current.
- **Custody and solvency controls** — segregating user funds, proving the
  house can always cover its LMSR exposure, audited reserves.
- **A production-grade price feed** — CoinGecko and Yahoo Finance's free
  endpoints are fine for a demo; a real settlement engine needs a licensed,
  SLA-backed market data vendor so a feed outage or a manipulated price
  can't corrupt a real-money payout.
- **Legal counsel** — genuinely, before launch, not after.

None of that is implemented here, on purpose. Treat this as the product/UX
layer to build once the legal and financial infrastructure underneath it
actually exists.

## Known limitations of the demo

- **Session tokens don't expire and there's no rate limiting.** Harmless
  for a local demo; on a public Railway URL it means a leaked token works
  forever and there's nothing stopping someone from hammering `/api/*`.
  Worth fixing before pointing anyone beyond friends-testing-it at the
  deployed URL.
- No password reset flow.
- Yahoo Finance's chart endpoint is unofficial/undocumented; it can change
  shape or rate-limit without notice. The scheduler logs and skips a tick
  on failure rather than crashing, but don't rely on it for anything that
  matters.
- `python3 run.py` (Flask's built-in dev server) is for local use only.
  The Procfile runs gunicorn instead for a real deploy — see "Deploying to
  Railway" above.
