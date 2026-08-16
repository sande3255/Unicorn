"""A minimal example bot built on unicorn_sdk.py.

This is intentionally simple, not a strategy worth actually running —
it's here to show the shape of a bot: poll, decide, trade, sleep, repeat.

Strategy (for demonstration only): for every open 5-minute market, if the
live underlying price has moved more than MOMENTUM_THRESHOLD away from the
strike in one direction, buy a small, fixed number of shares betting the
move continues ("momentum"). This is a toy heuristic, not investment
advice, and there's no reason to expect it to be profitable even in
play money — swap in your own logic.

Usage:
    pip install requests
    export UNICORN_API_KEY=unicorn_live_...
    export UNICORN_BASE_URL=https://your-app.up.railway.app   # or leave unset for localhost
    python example_bot.py
"""
import os
import time

from unicorn_sdk import UnicornAPIError, UnicornClient

POLL_SECONDS = 20
STAKE_SHARES = 5
MOMENTUM_THRESHOLD = 0.01  # current price >1% away from strike, in either direction
MAX_OPEN_POSITIONS_PER_RUN = 10  # simple safety cap so a bug can't spend the whole balance in one pass


def decide(market):
    """Returns 'YES', 'NO', or None (no trade) for one open, is_auto market."""
    if not market.get("is_auto") or market.get("duration_minutes") != 5:
        return None
    strike = market.get("strike_price")
    current = market.get("current_price")
    if strike is None or current is None or strike == 0:
        return None
    pct_move = (current - strike) / strike
    if pct_move > MOMENTUM_THRESHOLD:
        return "YES"  # betting the move up continues
    if pct_move < -MOMENTUM_THRESHOLD:
        return "NO"  # betting the move down continues
    return None


def run_once(client):
    traded = 0
    for market in client.list_open_markets():
        if traded >= MAX_OPEN_POSITIONS_PER_RUN:
            print(f"Hit MAX_OPEN_POSITIONS_PER_RUN ({MAX_OPEN_POSITIONS_PER_RUN}) for this pass, stopping.")
            break
        side = decide(market)
        if side is None:
            continue
        try:
            result = client.trade(market_id=market["id"], outcome=side, shares=STAKE_SHARES)
        except UnicornAPIError as e:
            if e.status_code == 429:
                print(f"Rate limited, backing off {e.retry_after_seconds}s")
                time.sleep(e.retry_after_seconds or 1)
            else:
                print(f"Trade failed for market #{market['id']}: {e}")
            continue
        traded += 1
        print(f"Bought {STAKE_SHARES} {side} shares in #{market['id']} {market['question']!r} "
              f"-> cost {result['cost']:.2f}, new balance {result['balance']:.2f}")


def main():
    api_key = os.environ.get("UNICORN_API_KEY")
    base_url = os.environ.get("UNICORN_BASE_URL", "http://localhost:8000")
    client = UnicornClient(api_key=api_key, base_url=base_url)

    me = client.me()
    print(f"Logged in as {me['username']} (balance: {me['balance']:.2f}). Polling every {POLL_SECONDS}s. Ctrl+C to stop.")

    while True:
        try:
            run_once(client)
        except UnicornAPIError as e:
            print(f"API error this pass: {e}")
        except Exception as e:  # noqa: BLE001 - keep the bot alive
            print(f"Unexpected error this pass: {e}")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
