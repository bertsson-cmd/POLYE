"""Polymarket taker-fee model (2026 fee schedule).

Verified against Polymarket's own fee documentation as of this writing.
THIS SCHEDULE HAS CHANGED SEVERAL TIMES IN 2026 (0% -> crypto-only ->
broad rollout -> sports rate revised) and will likely change again --
verify CATEGORY_FEE_RATES against Polymarket's current published fee
schedule periodically, especially before increasing position sizes.

RE-CONFIRMED during the CLOB V2 rewrite (V2 cutover April 28, 2026):
fees are unchanged in formula and roughly unchanged in rate -- this was
cross-checked two ways, since Polymarket's docs site itself was
unreachable from the research environment: (1) the published "$ per 100
shares at peak price" figures (crypto $1.75, sports/economics/culture/
weather $1.25, politics/finance/tech/mentions $1.00, geopolitics $0) back
out to EXACTLY the feeRate values below via fee=feeRate*0.25 at the 50c
peak; (2) an independent community migration cheatsheet quotes the same
"fee = C * feeRate * p * (1-p)" formula verbatim. One open question: V2's
py-clob-client-v2 SDK models each market's fee as a
{fee_rate, exponent, taker_only} triple (see FeeDetails in
py_clob_client_v2/clob_types.py), and an "exponent" that isn't always 1
would change this curve's shape. No source found during this research
pass gave a non-default exponent value for any category, and the "$ per
100 shares" cross-check above is only consistent with the simple formula
below (exponent=1) -- but if Polymarket starts varying it per-market,
this static table would silently go stale. Fetching each market's actual
FeeDetails via the API instead of this hardcoded table would be more
robust; out of scope for the V2 rewrite that added this note, worth
revisiting if position sizes increase enough for fee precision to matter.

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
