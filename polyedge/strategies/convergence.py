"""CONVERGE — near-resolution convergence ("last-cent yield").

Markets that are effectively decided often still trade at 94–98c days
before formal resolution, because holders pay for early liquidity.
Buying YES at 0.96 that resolves in 5 days yields 4.17% in 5 days
(≈ 300%+ annualized) IF it resolves YES.

The risk is precisely the "actually not decided" surprise, so:
  * only high liquidity markets (crowd conviction filter),
  * only short horizons (CV_MAX_DAYS),
  * an annualized-yield floor so capital isn't parked for pennies,
  * treated as probabilistic (est_p_win = market price), never guaranteed.
"""
from typing import Dict, List

from .. import config
from ..models import Leg, Market, Opportunity, OrderBook, days_to_resolution


def scan(all_markets: List[Market], books: Dict[str, OrderBook]) -> List[Opportunity]:
    out: List[Opportunity] = []
    for m in all_markets:
        if not (config.CV_MIN_YES_PRICE <= m.yes_price <= config.CV_MAX_YES_PRICE):
            continue
        if m.liquidity < config.CV_MIN_LIQUIDITY:
            continue
        days = max(0.02, days_to_resolution(m.end_date))
        if days > config.CV_MAX_DAYS:
            continue

        book = books.get(m.yes_token)
        if not book or book.best_ask() is None:
            continue
        a = book.best_ask()
        if a >= 0.999 or a < config.CV_MIN_YES_PRICE:
            continue

        yield_pct = (1.0 - a) / a                  # return if resolves YES
        annual = yield_pct * 365.0 / days
        if annual < config.CV_MIN_ANNUAL_YIELD:
            continue

        # the strategy's edge assumption, made explicit for the sizing:
        # true P(yes) is assumed to sit CV_TRUE_P_UPLIFT of the way from
        # the market price to 1.0 (near-certain markets are underpriced).
        # If est_p_win were just the market price, Kelly would see zero
        # edge and never fund a single convergence trade.
        p_assumed = m.yes_price + (1.0 - m.yes_price) * config.CV_TRUE_P_UPLIFT

        out.append(Opportunity(
            strategy="CONVERGE", key=f"CV-{m.market_id}",
            title=f"Converge: {m.question[:60]}",
            edge=yield_pct, guaranteed=False,
            est_p_win=p_assumed,
            legs=[Leg(m.yes_token, m.market_id, f"YES {m.question}", "YES",
                      a, 0.0)],
            resolve_by=m.end_date,
            note=f"YES ask {a:.3f}, {days:.1f}d to resolution, "
                 f"{annual*100:.0f}% annualized if YES, assumed true P {p_assumed:.3f}",
        ))
    # sort by annualized yield: same edge resolving sooner ranks higher,
    # which is exactly the near-term, fast-cycling preference
    out.sort(key=lambda o: -(o.edge * 365.0 / max(0.02, days_to_resolution(o.resolve_by))))
    return out
