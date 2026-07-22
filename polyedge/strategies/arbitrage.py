"""ARB — Dutch-book arbitrage on mutually-exclusive outcome sets.

A Polymarket "negRisk" event groups N binary markets where EXACTLY ONE
resolves YES (e.g. "Who wins the 2026 World Cup?").

Two locks exist:

  YES-side lock:  buy YES on every outcome.
      Cost  = sum of YES asks.  Payout = exactly $1 per set.
      Edge  = 1 - cost   (profit per $1 payout set, if cost < 1)

  NO-side lock:   buy NO on every outcome.
      Cost  = sum of NO asks.   Payout = exactly $(N-1) per set
      (every outcome except the winner resolves NO).
      Edge  = (N-1) - cost      (if cost < N-1)

Both are risk-free IF all legs fill AND the outcome set is genuinely
complete and mutually exclusive.

SAFETY GUARDS (added after a paper-mode blowup): illiquid multi-outcome
markets like "exact score" produce PHANTOM arbs — YES asks summing to
0.006 imply a 166x edge and let the sizer "buy" 50,000+ sets that don't
really exist, then mark them at locked $1 each for fantasy equity. Guards:
  * every leg's ask must be >= ARB_MIN_LEG_PRICE (no near-zero legs)
  * YES-lock cost per set must be >= ARB_MIN_COST (a real lock is ~0.9x,
    not 0.006x — a tiny sum means missing/unlisted outcomes, i.e. the
    "exactly one resolves YES" assumption is violated)
  * each market needs ARB_MIN_LIQUIDITY (thin books aren't executable)
  * sports MATCH markets excluded (exact-score / O-U outcome sets are
    the main phantom source and often aren't a complete listed set)
  * any single lock is hard-capped at ARB_MAX_POSITION_USD regardless of
    nominal "depth", so a mispriced book can't create a giant order
"""
import logging
from typing import Dict, List, Optional

from .. import config
from ..models import Leg, Market, Opportunity, OrderBook, days_to_resolution

log = logging.getLogger("polyedge.arb")


def _set_size_limit(books: List[OrderBook]) -> float:
    """Max complete payout sets executable at best-ask levels (thinnest leg)."""
    sets = float("inf")
    for b in books:
        if not b.asks:
            return 0.0
        sets = min(sets, b.asks[0].size)
    return 0.0 if sets == float("inf") else sets


def _is_sports_match(ev_title: str, question: str, category: str) -> bool:
    """Reuse the CONVERGE sports detector for consistency."""
    from .convergence import is_sports_match
    from ..models import Market as _M
    probe = _M("_", question, "_", "_", 0.5, 0.0, "", "", ev_title, False, category)
    return is_sports_match(probe)


def scan_event(markets: List[Market], books: Dict[str, OrderBook]) -> List[Opportunity]:
    """Check one mutually-exclusive event for YES-side and NO-side locks."""
    if len(markets) < 2:
        return []
    out: List[Opportunity] = []
    ev = markets[0]
    n = len(markets)

    # horizon cap: locks have no early exit, don't tie capital up for months;
    # also reject past-dated / expired events (negative days)
    dtr = days_to_resolution(ev.end_date)
    if dtr < config.MIN_DAYS_TO_RESOLUTION or dtr > config.ARB_MAX_DAYS:
        return []

    # sports match exclusion: exact-score / O-U sets are the phantom-arb
    # source and frequently aren't a complete listed outcome set
    if config.ARB_EXCLUDE_SPORTS and _is_sports_match(
            ev.event_title, ev.question, ev.category):
        return []

    # liquidity floor: every market in the group must clear it
    if any(m.liquidity < config.ARB_MIN_LIQUIDITY for m in markets):
        return []

    def _cap_sets(sets: float, cost_per_set: float) -> float:
        """Clamp sets so the position never exceeds the hard USD cap."""
        if cost_per_set <= 0:
            return 0.0
        return min(sets, config.ARB_MAX_POSITION_USD / cost_per_set)

    # ---------------- YES-side lock ----------------
    yes_books = [books.get(m.yes_token) for m in markets]
    if all(b and b.best_ask() is not None for b in yes_books):
        asks = [b.best_ask() for b in yes_books]
        cost = sum(asks)
        edge = (1.0 - cost) - config.FEE_RATE * cost
        # GUARD 1: no near-zero legs. GUARD 2: cost per set must be realistic
        # (a genuine complete YES-lock sits just under 1.0, not near 0 — a
        # tiny sum means the outcome set is incomplete / not exhaustive).
        legs_ok = all(a >= config.ARB_MIN_LEG_PRICE for a in asks)
        if edge >= config.ARB_MIN_EDGE and legs_ok and cost >= config.ARB_MIN_COST:
            sets = _set_size_limit(yes_books)
            sets = _cap_sets(sets, cost)
            if sets > 0 and sets * cost >= config.ARB_MIN_DEPTH_USD:
                legs = [Leg(token_id=m.yes_token, market_id=m.market_id,
                            label=f"YES {m.question}", side="YES",
                            entry_price=b.best_ask(), shares=sets)
                        for m, b in zip(markets, yes_books)]
                out.append(Opportunity(
                    strategy="ARB", key=f"ARB-YES-{ev.event_id}",
                    title=f"YES-lock: {ev.event_title}",
                    edge=edge, guaranteed=True, legs=legs,
                    guaranteed_payout=1.0, resolve_by=ev.end_date,
                    note=f"{n} outcomes, YES asks sum {cost:.4f}, "
                         f"{sets:.0f} sets (capped ${config.ARB_MAX_POSITION_USD:.0f})",
                ))

    # ---------------- NO-side lock ----------------
    no_books = [books.get(m.no_token) for m in markets]
    if all(b and b.best_ask() is not None for b in no_books):
        asks = [b.best_ask() for b in no_books]
        cost = sum(asks)
        payout = float(n - 1)
        edge_total = (payout - cost) - config.FEE_RATE * cost
        edge = edge_total / cost if cost > 0 else 0.0
        # GUARD: no near-zero legs (a 0.001 NO ask is a phantom too). The
        # NO-lock cost naturally sums near N-1, so no separate cost floor,
        # but each leg must be a real price and the whole thing is $-capped.
        legs_ok = all(a >= config.ARB_MIN_LEG_PRICE for a in asks)
        if edge_total >= config.ARB_MIN_EDGE and legs_ok:
            sets = _set_size_limit(no_books)
            sets = _cap_sets(sets, cost)
            if sets > 0 and sets * cost >= config.ARB_MIN_DEPTH_USD:
                legs = [Leg(token_id=m.no_token, market_id=m.market_id,
                            label=f"NO {m.question}", side="NO",
                            entry_price=b.best_ask(), shares=sets)
                        for m, b in zip(markets, no_books)]
                out.append(Opportunity(
                    strategy="ARB", key=f"ARB-NO-{ev.event_id}",
                    title=f"NO-lock: {ev.event_title}",
                    edge=edge, guaranteed=True, legs=legs,
                    guaranteed_payout=payout, resolve_by=ev.end_date,
                    note=f"{n} outcomes, NO asks sum {cost:.4f} vs payout {payout:.0f}, "
                         f"{sets:.0f} sets (capped ${config.ARB_MAX_POSITION_USD:.0f})",
                ))
    return out


def scan(all_markets: List[Market], books: Dict[str, OrderBook]) -> List[Opportunity]:
    """Group markets by negRisk event and scan each group."""
    groups: Dict[str, List[Market]] = {}
    for m in all_markets:
        if m.neg_risk and m.event_id:
            groups.setdefault(m.event_id, []).append(m)
    out: List[Opportunity] = []
    for ms in groups.values():
        out.extend(scan_event(ms, books))
    return out
