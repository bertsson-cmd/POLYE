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
from datetime import datetime, timezone
from typing import Dict, List

from .. import config
from ..models import Leg, Market, Opportunity, OrderBook


def _days_to_end(end_date: str) -> float:
    if not end_date:
        return 1e9
    try:
        dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
        return max(0.02, (dt - datetime.now(timezone.utc)).total_seconds() / 86400)
    except ValueError:
        return 1e9


def scan(all_markets: List[Market], books: Dict[str, OrderBook]) -> List[Opportunity]:
    out: List[Opportunity] = []
    for m in all_markets:
        if not (config.CV_MIN_YES_PRICE <= m.yes_price <= config.CV_MAX_YES_PRICE):
            continue
        if m.liquidity < config.CV_MIN_LIQUIDITY:
            continue
        days = _days_to_end(m.end_date)
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

        out.append(Opportunity(
            strategy="CONVERGE", key=f"CV-{m.market_id}",
            title=f"Converge: {m.question[:60]}",
            edge=yield_pct, guaranteed=False,
            est_p_win=m.yes_price,
            legs=[Leg(m.yes_token, m.market_id, f"YES {m.question}", "YES",
                      a, 0.0)],
            resolve_by=m.end_date,
            note=f"YES ask {a:.3f}, {days:.1f}d to resolution, "
                 f"{annual*100:.0f}% annualized if YES",
        ))
    out.sort(key=lambda o: -o.edge)
    return out
