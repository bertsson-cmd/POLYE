"""Risk — position sizing and exposure caps.

Rules, applied in order:
  1. Guaranteed locks (ARB/REL): size to executable depth, then cap.
  2. Probabilistic trades: fractional Kelly, then cap.
  3. Caps: per-position, per-strategy exposure, total exposure, cash.
  4. Anything below MIN_TICKET is dropped (dust trades aren't worth noise).

Kelly for a binary bet paying b-to-1 net odds with win prob p:
    f* = (p * b - (1 - p)) / b        (fraction of bankroll)
We use KELLY_FRACTION * f*  (default quarter-Kelly) because the win
probability is itself an estimate — full Kelly on a wrong p is ruin.
"""
import logging
from typing import Dict, List, Optional

from . import config
from .models import Opportunity

log = logging.getLogger("polyedge.risk")


def kelly_fraction(p_win: float, net_odds: float) -> float:
    """Fraction of bankroll for a binary bet. Never negative."""
    if net_odds <= 0 or not (0.0 < p_win < 1.0):
        return 0.0
    f = (p_win * net_odds - (1.0 - p_win)) / net_odds
    return max(0.0, f)


def size_opportunities(opps: List[Opportunity], bankroll: float, cash: float,
                       strategy_exposure: Dict[str, float],
                       total_exposure: float,
                       open_keys: Optional[set] = None,
                       open_longshots: int = 0) -> List[Opportunity]:
    """Return a list of opportunities with leg sizes set, respecting all caps.

    strategy_exposure / total_exposure = current open cost basis.
    Mutates nothing; returns new sized list (skips zero-size results).
    """
    open_keys = open_keys or set()
    sized: List[Opportunity] = []
    reasons = {"sized": 0, "already_held": 0, "ls_slots_full": 0,
               "caps_exhausted": 0, "kelly_zero": 0, "below_min_ticket": 0}
    cash_left = cash
    expo_left = max(0.0, bankroll * config.MAX_TOTAL_EXPOSURE_PCT - total_exposure)
    strat_left = {
        s: max(0.0, bankroll * pct - strategy_exposure.get(s, 0.0))
        for s, pct in config.MAX_STRATEGY_EXPOSURE_PCT.items()
    }
    ls_slots = max(0, config.LS_MAX_OPEN - open_longshots)

    # funding priority: guaranteed locks first (free money before speculative),
    # then soonest-resolving (near-term capital cycling), then best edge
    from .models import days_to_resolution
    for opp in sorted(opps, key=lambda o: (not o.guaranteed,
                                           days_to_resolution(o.resolve_by),
                                           -o.edge)):
        if opp.key in open_keys:
            reasons["already_held"] += 1
            continue                      # already holding this
        if opp.strategy == "LONGSHOT" and ls_slots <= 0:
            reasons["ls_slots_full"] += 1
            continue

        cap = min(
            bankroll * config.MAX_POSITION_PCT,
            strat_left.get(opp.strategy, 0.0),
            expo_left,
            cash_left,
        )
        if cap < config.MIN_TICKET:
            reasons["caps_exhausted"] += 1
            continue

        if opp.guaranteed:
            # legs already sized to executable depth; scale down to cap
            cost_full = opp.total_cost()
            if cost_full <= 0:
                reasons["below_min_ticket"] += 1
                continue
            scale = min(1.0, cap / cost_full)
            for leg in opp.legs:
                leg.shares *= scale
            budget = opp.total_cost()
        else:
            leg = opp.legs[0]
            a = leg.entry_price
            net_odds = (1.0 - a) / a          # win (1-a) risking a, per share
            f = kelly_fraction(opp.est_p_win or 0.0, net_odds)
            if f <= 0:
                reasons["kelly_zero"] += 1
                continue
            budget = min(cap, bankroll * f * config.KELLY_FRACTION)
            if budget < config.MIN_TICKET:
                reasons["below_min_ticket"] += 1
                continue
            leg.shares = budget / a

        if budget < config.MIN_TICKET:
            reasons["below_min_ticket"] += 1
            continue

        cash_left -= budget
        expo_left -= budget
        strat_left[opp.strategy] = strat_left.get(opp.strategy, 0.0) - budget
        if opp.strategy == "LONGSHOT":
            ls_slots -= 1
        reasons["sized"] += 1
        sized.append(opp)
    if opps:
        log.info("sizing: %s", {k: v for k, v in reasons.items() if v})
    return sized
