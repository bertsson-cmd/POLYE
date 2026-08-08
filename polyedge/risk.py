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
import time
from typing import Dict, List, Optional

from . import config
from .models import OrderBook, Opportunity

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
                       open_longshots: int = 0,
                       rejected_cooldown: Optional[Dict[str, float]] = None,
                       books: Optional[Dict[str, OrderBook]] = None) -> List[Opportunity]:
    """Return a list of opportunities with leg sizes set, respecting all caps.

    strategy_exposure / total_exposure = current open cost basis.
    Mutates nothing; returns new sized list (skips zero-size results).

    rejected_cooldown: optional {token_id: time.time() of its most recent
    BUY-leg rejection}, as tracked by LiveEngine.rejected_cooldown (see
    live.py for why that's in-memory only, not persisted). Any candidate
    with a leg whose token was rejected within POLYEDGE_REJECTED_COOLDOWN_MIN
    minutes is skipped entirely -- BEFORE the cap/Kelly sizing below, so a
    dead candidate never consumes cash/exposure/LONGSHOT-slot budget that a
    real candidate further down the sorted list could have used instead.
    Confirmed in production: without this, a single token whose FOK order
    kept getting rejected scored as the best candidate cycle after cycle
    for over an hour, since nothing about a rejection changes its edge/
    price/liquidity inputs. None/empty (the default, and always the case
    for PaperEngine, which has no order-book-depth concept to fail
    against) makes this check a no-op.

    books: the same {token_id: OrderBook} dict main.run_cycle() already
    fetched for the strategy scans, passed through here so a candidate's
    real min_order_size (in SHARES, not dollars -- confirmed from
    Polymarket's own /book response schema) can be checked before it's
    sized. Applies to BOTH CONVERGE and LONGSHOT (they share this sizing
    branch) -- deliberately NOT scoped to LiveEngine only, unlike
    rejected_cooldown above: PaperEngine's fills are meant to simulate
    what live trading could actually achieve, so silently sizing below an
    exchange's real minimum would make paper an inaccurate simulator, not
    just a live-only correctness issue. A candidate missing from `books`,
    or a book with no min_order_size, makes the check a no-op for that
    candidate (be conservative -- "can't check it" is not "no floor", but
    there's nothing to check it against). The comparison and any bump both
    use min_order_size inflated by config.MIN_ORDER_SIZE_MARGIN_PCT, not
    the raw value -- a real order was rejected at a razor-thin margin the
    raw comparison judged as fine (see that config's comment). Whenever a
    book's min_order_size is found for a leg's token, it's also stashed on
    `leg.min_order_size` -- diagnostic only, not used in any further
    accounting math -- purely so live.py can log the real number a real
    order was actually sized against.
    """
    open_keys = open_keys or set()
    rejected_cooldown = rejected_cooldown or {}
    books = books or {}
    cooldown_cutoff = time.time() - config.LIVE_REJECTED_COOLDOWN_MIN * 60.0
    sized: List[Opportunity] = []
    reasons = {"sized": 0, "in_cooldown": 0, "already_held": 0, "ls_slots_full": 0,
               "caps_exhausted": 0, "kelly_zero": 0, "below_min_ticket": 0,
               "below_min_order_size": 0}
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
        if any(rejected_cooldown.get(leg.token_id, 0.0) > cooldown_cutoff
              for leg in opp.legs):
            reasons["in_cooldown"] += 1
            continue                      # recently rejected -- give a real candidate the slot
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
            # hard backstop: never let a single guaranteed position exceed
            # the position cap OR a sane absolute ceiling, regardless of what
            # depth the strategy claimed (defends against phantom-arb sizing)
            ceiling = min(cap, config.MAX_POSITION_ABS_USD)
            scale = min(1.0, ceiling / cost_full)
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

            # exchange min_order_size floor, in SHARES -- confirmed real:
            # a $5 CONVERGE ticket near 0.95-0.99 buys only ~5.05-5.3
            # shares, right at typical 1-5 share floors, and gets
            # rejected outright even against a deep book. Price-aware:
            # recomputed at THIS candidate's own entry price, since a
            # flat dollar bump that clears the floor at 95c would not
            # clear it at 99c.
            #
            # A MARGIN, not the raw min_order_size, is what's actually
            # compared and bumped to -- a real rejection (CV-3290748)
            # happened at a razor-thin ~0.1% margin ABOVE the exact floor
            # (5.005 shares against min_order_size=5) that this exact "<"
            # comparison, without a margin, judged as already fine. See
            # config.MIN_ORDER_SIZE_MARGIN_PCT's comment for what's known
            # (and not known) about why that razor-thin case still failed.
            min_order_size = getattr(books.get(leg.token_id), "min_order_size", None)
            if min_order_size:
                leg.min_order_size = min_order_size   # diagnostic only -- see Leg's field comment
                effective_min = min_order_size * (1.0 + config.MIN_ORDER_SIZE_MARGIN_PCT / 100.0)
                if (budget / a) < effective_min:
                    min_dollar = effective_min * a
                    if not config.MIN_ORDER_SIZE_BUMP or min_dollar > cap:
                        reasons["below_min_order_size"] += 1
                        continue
                    budget = min_dollar

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
