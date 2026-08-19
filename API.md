# UNICORN API — for bots

UNICORN exposes the same HTTP API its own web frontend uses. There's no
separate "bot API" — a bot authenticates with a long-lived **API key**
instead of a session token, and calls the same endpoints. This document
covers auth, every endpoint a bot needs, rate limits, and the Python SDK
in `sdk/`.

If you just want to get a bot running: see `sdk/unicorn_sdk.py` and
`sdk/example_bot.py`. This document is the reference for everything
those files use, plus a couple of account-management endpoints they
don't (creating/revoking keys).

## Authentication

Every authenticated request sends `Authorization: Bearer <token>`. Two
kinds of token work there:

- **Session token** — from `/api/login` or `/api/signup`. What the web
  UI uses. Conceptually short-lived (tied to being logged in).
- **API key** — from `POST /api/api-keys`, generated once while logged
  in, meant to live in a bot's config/environment indefinitely. Prefixed
  `unicorn_live_` so it's recognizable at a glance (and greppable if one
  ever leaks into a log by accident).

Get an API key from the **API keys** page in the UI (top nav, once
logged in), or by calling `POST /api/api-keys` with a session token (see
below). **The plaintext key is shown exactly once**, at creation — UNICORN
only stores a hash of it, so if you don't copy it down immediately
there's no way to recover it; generate a new one instead.

**Read-only keys**: when creating a key you choose whether it `can_trade`.
A read-only key can call every `GET` endpoint (list markets, get a
market, check your portfolio/history) but a trade attempt returns
`403`. Useful for a bot that only watches and alerts, or while testing a
new strategy before trusting it to actually spend your balance.

**API-key management requires a session, not a key**: `GET/POST
/api/api-keys` and `DELETE /api/api-keys/<id>` all reject API-key auth
(`403 "This action requires a logged-in session, not an API key"`) — a
leaked bot key can trade with your play money, but it can't mint itself
sibling keys or revoke the key you'd use to notice the leak and shut it
down. Manage keys from the logged-in web UI, or script it with a session
token from `/api/login`.

## Base URL

Whatever your UNICORN instance's URL is — `http://localhost:8000` when
running locally, or your `*.up.railway.app` domain once deployed (see the
README's "Deploying to Railway" section). There's no separate hosted
UNICORN API; each deployment is your own instance with its own users,
balances, and markets.

## Endpoints

All request/response bodies are JSON. All responses include a `detail`
field on errors.

### `POST /api/api-keys` — create a key (session auth only)

```json
// request
{ "label": "momentum bot", "can_trade": true }

// response (200) — "key" is the ONLY time the plaintext is ever returned
{
  "id": 3, "label": "momentum bot", "key_prefix": "unicorn_live_9d1684",
  "can_trade": true, "created_at": "...", "last_used_at": null,
  "revoked_at": null, "key": "unicorn_live_9d1684...full secret..."
}
```

### `GET /api/api-keys` — list your keys (session auth only)

Returns an array of the same shape minus `key` (never re-shown).

### `DELETE /api/api-keys/<id>` — revoke a key (session auth only)

Immediately stops that key from authenticating anything.
`{"revoked": true}` on success, `404` if it's not yours or doesn't exist.

### `GET /api/markets` — list all markets

No auth required (public), but sending a key/token gets you your own
rate-limit bucket instead of sharing one keyed by IP — worth doing even
for read-only calls if you're polling. **120 requests / 60s** per
identity.

Returns an array; each market has (among other fields — see
`serialize_market` in `backend/app/server.py` for the exact list):

```json
{
  "id": 101, "question": "Will Nvidia be above $186.20 in 5 minutes?",
  "category": "Stocks · 5 min", "status": "open", "price_yes": 0.5125,
  "is_auto": true, "market_type": "stock", "symbol_label": "Nvidia",
  "duration_minutes": 5, "strike_price": 186.20, "current_price": 187.01,
  "close_time": "2026-08-15T19:20:00", "resolved_outcome": null
}
```

`is_auto: true` markets are the timed win/lose format (the current
roster: top US stocks, top cryptocurrencies, and the four headline US
indices — see the README for the exact list and `AUTO_MARKET_CONFIGS` in
`backend/app/scheduler.py`). `strike_price` is the price it opened at;
`current_price` is the live price right now; it resolves YES if
`current_price > strike_price` at `close_time`, NO otherwise (a tie
resolves NO). `status` becomes `"resolved"` once that happens, and
`price_yes` becomes exactly `1.0` or `0.0`.

### `GET /api/markets/<id>` — one market's detail

Same auth/rate-limit as above (120/60s). Adds `description` and
`price_history` (a list of `{t, price}` points) that the list endpoint
omits for size.

### `POST /api/markets/<id>/trade` — buy or sell shares

Auth required (session or API key — with a read-only key this returns
403). **30 requests / 60s** per identity.

```json
// request — shares > 0 buys, shares < 0 sells (requires an existing position)
{ "outcome": "YES", "shares": 10 }

// response (200)
{
  "balance": 9987.45, "cost": 5.12, "price_yes": 0.5310,
  "position_shares_yes": 10.0, "position_shares_no": 0.0
}
```

Errors: `400` for a bad outcome/shares value, a market that's not
`status: "open"`, selling more than you hold, or insufficient balance;
`403` if a read-only API key tries this; `404` if the market doesn't
exist.

### `GET /api/portfolio` — your open positions

Auth required (session or API key). Array of
`{market_id, question, status, price_yes, shares_yes, shares_no,
resolved_outcome}`, one entry per market you hold a nonzero position in.

### `GET /api/portfolio/stats` — your realized performance

Auth required (session or API key). `{resolved_markets_traded, wins,
losses, win_rate, total_realized_pnl, biggest_win, biggest_loss,
balance_history}`. Only counts markets that have actually resolved —
open positions don't affect win/loss/P&L yet, since the outcome isn't
known. `biggest_win`/`biggest_loss` are each either `null` or
`{question, pnl}`. `balance_history` is `[{t, balance}, ...]`, downsampled
to at most 200 points for accounts with a long transaction history.

### `GET /api/achievements` — your badges

Auth required (session or API key). Array of
`{key, label, description, earned, earned_at}`, one entry per badge in
the fixed catalog (first trade, century club, first win, hot streak,
high roller, big winner, market explorer, daily devotee, bot trader).
`earned_at` is `null` until the badge is earned, then stays fixed —
badges are permanent once earned even if the underlying condition
later stops being true (e.g. `daily_devotee` stays earned after a
streak later resets).

### `GET /api/challenges` — this week's challenges

Auth required (session or API key). `{balance, resets_at, challenges}`,
where `challenges` is `[{key, label, description, reward, completed,
completed_at}, ...]` for a fixed catalog of 3 (place 5 trades, trade in
3 categories, place a single $100+ trade). Unlike achievements these
reset every UTC week (Monday 00:00) — `completed`/`completed_at` only
ever describe the *current* week, and the reward is paid out
automatically the moment a call to this endpoint notices you newly
qualify (same auto-claim pattern as achievements, just scoped to the
week). `balance` reflects any reward this call just credited, so it's
worth reading even if you only care about your balance.

### `GET /api/referrals` — your referral stats

Auth required (session or API key). `{referral_code, referral_count,
total_bonus_earned, referred_users}`. `referral_code` is just your own
username — a referral link is `<site>/#/login?ref=<referral_code>`.
`referred_users` is `[{username, created_at}, ...]` for everyone who
signed up through it, newest first. `POST /api/signup` accepts an
optional `referral_code` field; a code that doesn't match a real
account (or matches your own new username) is silently ignored rather
than rejected. When it does match, both accounts are credited a flat
$250 play-money bonus the moment the new account is created — see
`REFERRAL_BONUS_REFEREE`/`REFERRAL_BONUS_REFERRER` in `server.py`.

### `GET /api/markets/<id>/comments` — a market's discussion thread

No auth required. Last 200 comments on the market, newest first:
`{id, market_id, user_id, username, body, created_at}`. 404s if the
market doesn't exist.

### `POST /api/markets/<id>/comments` — post a comment

Auth required (session or API key). Body: `{body}`, 1–500 characters
after trimming whitespace; empty or over-length bodies are rejected
with 400. Returns the created comment in the same shape as the list
endpoint above. 404s if the market doesn't exist.

### `DELETE /api/comments/<id>` — delete a comment

Auth required (session or API key). You can only delete your own
comments — admins can delete any comment. 403 if it's someone else's
and you're not an admin, 404 if the comment doesn't exist.

### `GET /api/transactions` — your trade history

Auth required (session or API key). Last 500 rows, newest first:
`{id, market_id, market_question, type, outcome, shares, amount,
balance_after, created_at}`.

### `GET /api/me` — who am I

Auth required (session or API key). `{username, balance, is_admin}`.

### `GET /api/leaderboard` — public, no auth

Top traders by net worth (cash + open-position value). Not
bot-relevant, listed here for completeness.

## Rate limits

Enforced per authenticated identity (falls back to IP if unauthenticated)
— see `backend/app/ratelimit.py`. Exceeding a limit returns:

```json
// 429
{ "detail": "Rate limit exceeded: max 30 requests per 60s for this endpoint.",
  "retry_after_seconds": 4.2 }
```

`sdk/unicorn_sdk.py`'s `UnicornAPIError.retry_after_seconds` surfaces
this directly so a bot can back off precisely instead of guessing —
`sdk/example_bot.py` shows the pattern.

Current limits: **120/60s** on `GET /api/markets` and
`GET /api/markets/<id>`, **30/60s** on trading. These are process-local
(the rate limiter's own docstring explains why — it only works correctly
under the single-worker-process constraint the Railway Procfile already
pins for an unrelated reason). If you're running many bots against the
same instance, budget accordingly.

## A note on what this is

UNICORN is a play-money demo, not a licensed exchange (see the README's
"Before this can take real money" section). The API/SDK model here — generate a key,
run your own bot against a documented REST API — mirrors how real
exchanges expose programmatic trading, which is exactly why it's useful
as a demo/practice surface: the same client code patterns (auth headers,
rate-limit backoff, position sizing) transfer directly, without any of
the stakes.
