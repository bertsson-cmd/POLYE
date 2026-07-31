"""Polymarket taker-fee model (2026 fee schedule).

Verified against Polymarket's own fee documentation as of this writing.
THIS SCHEDULE HAS CHANGED SEVERAL TIMES IN 2026 (0% -> crypto-only ->
broad rollout -> sports rate revised) and will likely change again --
verify CATEGORY_FEE_RATES against Polymarket's current published fee
schedule periodically, especially before increasing position sizes.

Formula: fee = shares * feeRate * price * (1 - price)

This is a curve that PEAKS at a 50c price and shrinks toward the
extremes (0 or 1). It matters for this bot specifically: CONVERGE
trades at 94-98.5c and LONGSHOT trades at 3-5c -- both strategies
already operate right where this formula is cheapest, which is a lucky
structural fit, not something engineered for that reason.

Fees are charged on TAKER fills only (maker/resting limit orders that
get filled pay zero and earn rebates) -- this bot places FOK/market-
style taker orders, so every real fill here pays the taker fee.
"""
from typing import Optional

from . import config

CATEGORY_FEE_RATES = [
    ("geopolitics", 0.00),
    ("world", 0.00),
    ("crypto", 0.07),
    ("sport", 0.05),
    ("economics", 0.05),
    ("culture", 0.05),
    ("weather", 0.05),
    ("politics", 0.04),
    ("finance", 0.04),
    ("tech", 0.04),
    ("mentions", 0.04),
]
DEFAULT_FEE_RATE = 0.05


def fee_rate_for_category(category: str) -> float:
    if config.FEE_RATE_OVERRIDE is not None:
        return config.FEE_RATE_OVERRIDE
    cat = (category or "").lower()
    for needle, rate in CATEGORY_FEE_RATES:
        if needle in cat:
            return rate
    return DEFAULT_FEE_RATE


def fee_per_share(price: float, category: str) -> float:
    rate = fee_rate_for_category(category)
    p = max(0.0, min(1.0, price))
    return rate * p * (1.0 - p)


def net_of_fee_edge(gross_edge_per_share: float, price: float, category: str) -> float:
    return gross_edge_per_share - fee_per_share(price, category)
