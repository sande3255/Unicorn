"""
Logarithmic Market Scoring Rule (LMSR) automated market maker for binary
(YES/NO) prediction markets. This is the same style of mechanism used by
real prediction-market and forecasting platforms.

Cost function:   C(q_yes, q_no) = b * ln(exp(q_yes/b) + exp(q_no/b))
Price of YES:     exp(q_yes/b) / (exp(q_yes/b) + exp(q_no/b))
Price of NO:      1 - Price(YES)

`b` is the liquidity parameter: larger b = deeper liquidity = prices move
less per trade, but the market maker's maximum possible loss (b * ln 2) is
also larger.
"""
import math


def _log_sum_exp(q_yes: float, b: float, q_no: float) -> float:
    a = q_yes / b
    c = q_no / b
    m = max(a, c)
    return m + math.log(math.exp(a - m) + math.exp(c - m))


def cost(q_yes: float, q_no: float, b: float) -> float:
    return b * _log_sum_exp(q_yes, b, q_no)


def price_yes(q_yes: float, q_no: float, b: float) -> float:
    a = q_yes / b
    c = q_no / b
    m = max(a, c)
    ea = math.exp(a - m)
    ec = math.exp(c - m)
    return ea / (ea + ec)


def trade_cost(q_yes: float, q_no: float, b: float, outcome: str, delta_shares: float) -> float:
    """Cost (positive = user pays, negative = user receives) to move the
    market by `delta_shares` of `outcome` (use a negative delta to sell)."""
    old_c = cost(q_yes, q_no, b)
    if outcome == "YES":
        new_c = cost(q_yes + delta_shares, q_no, b)
    else:
        new_c = cost(q_yes, q_no + delta_shares, b)
    return new_c - old_c


def max_market_maker_loss(b: float) -> float:
    return b * math.log(2)


def seed_shares_for_price(target_price: float, b: float) -> tuple[float, float]:
    """Returns (q_yes, q_no) so a freshly-created market opens trading at
    `target_price` instead of always starting at 50%. Used to seed imported
    markets at whatever probability the source platform currently shows.
    Clamped away from 0/1 since the logit blows up at the extremes."""
    p = min(max(target_price, 0.02), 0.98)
    q_yes = b * math.log(p / (1 - p))
    return q_yes, 0.0
