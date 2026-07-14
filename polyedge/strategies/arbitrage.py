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

Both are risk-free IF all legs fill. Fill sizing is limited to the depth of
the thinnest leg so the reported edge is actually executable, not headline.
"""
from typing import Dict, List, Optional

from .. import config
from ..models import Leg, Market, Opportunity, OrderBook, days_to_resolution


def _set_size_limit(books: List[OrderBook]) -> float:
    """Max number of complete payout sets executable at best-ask levels.

    Conservative: uses only the BEST ask level of each leg, so the fill
    price never exceeds the price used in the edge calculation.
    """
    sets = float("inf")
    for b in books:
        if not b.asks:
            return 0.0
        sets = min(sets, b.asks[0].size)
    return 0.0 if sets == float("inf") else sets


def scan_event(markets: List[Market], books: Dict[str, OrderBook]) -> List[Opportunity]:
    """Check one mutually-exclusive event for YES-side and NO-side locks."""
    if len(markets) < 2:
        return []
    out: List[Opportunity] = []
    ev = markets[0]
    n = len(markets)

    # horizon cap: a lock ties up capital until resolution with no early
    # exit, so far-dated locks are skipped entirely — capital velocity
    # beats a guaranteed edge that pays out in a year
    if days_to_resolution(ev.end_date) > config.ARB_MAX_DAYS:
        return []

    # ---------------- YES-side lock ----------------
    yes_books = [books.get(m.yes_token) for m in markets]
    if all(b and b.best_ask() is not None for b in yes_books):
        cost = sum(b.best_ask() for b in yes_books)
        edge = (1.0 - cost) - config.FEE_RATE * cost
        if edge >= config.ARB_MIN_EDGE:
            sets = _set_size_limit(yes_books)
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
                         f"{sets:.0f} sets executable at best ask",
                ))

    # ---------------- NO-side lock ----------------
    no_books = [books.get(m.no_token) for m in markets]
    if all(b and b.best_ask() is not None for b in no_books):
        cost = sum(b.best_ask() for b in no_books)
        payout = float(n - 1)
        edge_total = (payout - cost) - config.FEE_RATE * cost
        # normalise edge per $1 of cost so strategies are comparable
        edge = edge_total / cost if cost > 0 else 0.0
        if edge_total >= config.ARB_MIN_EDGE:
            sets = _set_size_limit(no_books)
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
                         f"{sets:.0f} sets executable",
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
