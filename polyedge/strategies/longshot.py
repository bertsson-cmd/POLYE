"""LONGSHOT — fade overpriced longshots (favorite-longshot bias).

The bias, documented across betting markets for decades: low-probability
outcomes trade ABOVE their true probability, because buyers of lottery
tickets outnumber sellers.

Trade: buy NO on markets whose YES trades at 3–5 cents. The band
deliberately EXCLUDES the extreme tail (below 3c): recent large-sample
Polymarket research is contested exactly there — one major study found
the cheapest tokens actually land more often than priced, the opposite
of this strategy's assumption. We only fade where the overpricing
evidence is consistent.

This is NOT arbitrage. Each trade usually wins a little; occasionally a
longshot lands and the position loses most of its cost. The strategy only
works with (a) diversification across UNCORRELATED events, (b) strict
sizing, (c) a haircut assumption about how overpriced the YES really is.

Sizing uses fractional Kelly (see risk.py); here we compute the estimated
true probability and the edge:

  market YES price = q          (e.g. 0.04)
  assumed true P(yes) = q * LS_BIAS_HAIRCUT   (e.g. 0.04 * 0.6 = 0.024)
  buy NO at ask a (≈ 1-q):  win (1-a) per share with prob (1 - true_p)
                            lose a per share with prob true_p
  EV per $1 cost = [(1-true_p) * 1 - a] / a

LS_EXCLUDE_WEATHER (default on) excludes weather markets (temperature
ranges, rain/snow, etc.), reusing convergence.is_weather_market() --
real evidence: LS-3412924, an actual live position ("Fade: Will the
highest temperature in Seattle be between 72-73°F...") -- a narrow
bracket on a continuously-moving quantity, the same risk shape as the
tweet-count bracket that caused CONVERGE's original documented loss.
This is deliberately UNLIKE sports, which LONGSHOT does NOT exclude
(fading cheap sports outcomes is core to its edge, a strategy-specific
judgment call) -- a narrow bracket on a volatile continuous quantity is
a real risk regardless of which strategy is trading it, not a judgment
call the way sports exclusion was.
"""
import logging
from typing import Dict, List

from .. import config, fees
from ..models import Leg, Market, Opportunity, OrderBook, days_to_resolution
from .convergence import is_weather_market

log = logging.getLogger("polyedge.longshot")


def scan(all_markets: List[Market], books: Dict[str, OrderBook]) -> List[Opportunity]:
    out: List[Opportunity] = []
    seen_events = set()
    skipped_weather = 0
    for m in all_markets:
        q = m.yes_price
        if not (config.LS_MIN_YES_PRICE <= q <= config.LS_MAX_YES_PRICE):
            continue
        if m.liquidity < config.LS_MIN_LIQUIDITY:
            continue
        d = days_to_resolution(m.end_date)
        if d < config.MIN_DAYS_TO_RESOLUTION or d > config.LS_MAX_DAYS:
            continue
        # one fade per event — fading 5 outcomes of the same event is one bet
        if m.event_id in seen_events:
            continue
        # Unlike sports (deliberately NOT excluded here -- fading cheap
        # sports outcomes is core to LONGSHOT's edge, a strategy-specific
        # judgment call), a narrow bracket on a volatile continuous
        # quantity is a real risk regardless of which strategy is trading
        # it -- not a judgment call. Real evidence: LS-3412924, an actual
        # live LONGSHOT position ("Fade: Will the highest temperature in
        # Seattle be between 72-73°F...") -- structurally the same
        # "narrow band on a continuously-moving quantity" risk pattern as
        # the tweet-count bracket that caused CONVERGE's documented loss.
        # Reuses convergence.is_weather_market() rather than duplicating
        # the detection logic.
        if config.LS_EXCLUDE_WEATHER and is_weather_market(m):
            skipped_weather += 1
            continue

        book = books.get(m.no_token)
        if not book or book.best_ask() is None:
            continue
        a = book.best_ask()
        if a >= 1.0 or a <= 0.0:
            continue

        true_p_yes = min(1.0, q * config.LS_BIAS_HAIRCUT)
        p_win = 1.0 - true_p_yes
        fee = fees.fee_per_share(a, m.category)
        # real cash outlay per share is (a + fee), not just a -- both the
        # profit numerator and the cost denominator must reflect that
        ev_per_cost = (p_win - a - fee) / (a + fee)
        if ev_per_cost <= 0:
            continue

        seen_events.add(m.event_id)
        out.append(Opportunity(
            strategy="LONGSHOT", key=f"LS-{m.market_id}",
            title=f"Fade: {m.question[:60]}",
            edge=ev_per_cost, guaranteed=False,
            est_p_win=p_win,
            legs=[Leg(m.no_token, m.market_id, f"NO {m.question}", "NO",
                      a, 0.0, fee_per_share=fee)],   # shares set by risk module
            resolve_by=m.end_date,
            note=f"YES at {q:.3f}, assumed true {true_p_yes:.3f}, "
                 f"NO ask {a:.3f}, fee {fee:.4f}",
        ))
    # soonest-resolving first (near-term capital cycling), edge as tiebreak
    out.sort(key=lambda o: (days_to_resolution(o.resolve_by), -o.edge))
    if skipped_weather:
        log.info("longshot: excluded %d weather market(s)", skipped_weather)
    return out
