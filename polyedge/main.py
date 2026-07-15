"""PolyEdge 95 v2 — one full scan cycle.

    python -m polyedge.main            # live data scan (paper fills)
    python -m polyedge.main --selftest # offline sanity check, no network

Cycle:
  1. fetch open events (Gamma) and parse markets
  2. fetch order books (CLOB) only for tokens the strategies care about
  3. run all four strategy scans
  4. size candidates through the risk module
  5. open paper positions
  6. check take-profit on eligible open positions (live bids, not indicative marks)
  7. mark to market, settle resolved markets
  8. write state and regenerate the dashboard
"""
import argparse
import logging
import sys
from typing import Dict, List

from . import config
from .api import PolymarketClient
from .models import Market, OrderBook
from .paper import PaperEngine
from .report import write_dashboard
from .strategies import arbitrage, convergence, correlated, longshot

log = logging.getLogger("polyedge")


def _interesting_tokens(markets: List[Market], relations: List[dict]) -> Dict[str, str]:
    """token_id -> why we need its book, within MAX_BOOKS_PER_SCAN.

    Budget allocation (in order):
      1. CONVERGE candidates get a RESERVED share (CV_BOOK_RESERVE_PCT) —
         this is the primary many-small-wins strategy and must never be
         starved of books by large ARB events. Soonest-resolving first.
      2. REL tokens (user-declared relations are few and precious).
      3. ARB tokens, grouped by WHOLE events (a partial event is useless —
         the arb scan needs every leg's book), soonest-resolving events
         first, skipping events beyond ARB_MAX_DAYS entirely.
      4. LONGSHOT candidates fill whatever budget is left, soonest first.
    """
    from .models import days_to_resolution
    budget = config.MAX_BOOKS_PER_SCAN
    need: Dict[str, str] = {}

    # --- 1) CONVERGE: reserved share, soonest-resolving first
    cv = [(days_to_resolution(m.end_date), m.yes_token) for m in markets
          if config.CV_MIN_YES_PRICE <= m.yes_price <= config.CV_MAX_YES_PRICE
          and m.liquidity >= config.CV_MIN_LIQUIDITY
          and days_to_resolution(m.end_date) <= config.CV_MAX_DAYS]
    cv.sort()
    cv_quota = max(1, int(budget * config.CV_BOOK_RESERVE_PCT)) if cv else 0
    for _, tok in cv[:cv_quota]:
        need.setdefault(tok, "converge")

    # --- 2) REL: always include declared relation markets (both sides)
    rel_ids = {str(r.get("a_market_id")) for r in relations} | \
              {str(r.get("b_market_id")) for r in relations}
    for m in markets:
        if m.market_id in rel_ids:
            need.setdefault(m.yes_token, "rel")
            need.setdefault(m.no_token, "rel")

    # --- 3) ARB: whole events only, within horizon, soonest first
    groups: Dict[str, List[Market]] = {}
    for m in markets:
        if m.neg_risk and m.event_id:
            groups.setdefault(m.event_id, []).append(m)
    ev_sorted = sorted(
        (g for g in groups.values()
         if len(g) >= 2
         and days_to_resolution(g[0].end_date) <= config.ARB_MAX_DAYS),
        key=lambda g: days_to_resolution(g[0].end_date))
    for g in ev_sorted:
        tokens = []
        for m in g:
            for tok in (m.yes_token, m.no_token):
                if tok not in need:
                    tokens.append(tok)
        if len(need) + len(tokens) > budget:
            continue          # this event doesn't fit whole; try smaller ones
        for tok in tokens:
            need[tok] = "arb"

    # --- 4) LONGSHOT: fill the remainder, soonest first
    ls = [(days_to_resolution(m.end_date), m.no_token, m.event_id) for m in markets
          if config.LS_MIN_YES_PRICE <= m.yes_price <= config.LS_MAX_YES_PRICE
          and m.liquidity >= config.LS_MIN_LIQUIDITY
          and days_to_resolution(m.end_date) <= config.LS_MAX_DAYS]
    ls.sort()
    seen_events = set()
    for _, tok, ev_id in ls:
        if len(need) >= budget:
            break
        if ev_id in seen_events:      # one fade per event anyway
            continue
        if tok not in need:
            need[tok] = "longshot"
            seen_events.add(ev_id)

    counts = {}
    for why in need.values():
        counts[why] = counts.get(why, 0) + 1
    log.info("book budget: %d/%d used — %s", len(need), budget, counts)
    return need


def run_cycle(client: PolymarketClient = None, engine: PaperEngine = None) -> dict:
    client = client or PolymarketClient()
    engine = engine or PaperEngine()

    # 1) markets
    events = client.fetch_events()
    markets: List[Market] = []
    for ev in events:
        markets.extend(client.parse_event(ev))
    log.info("parsed %d markets from %d events (%d skipped)",
             len(markets), len(events), client.skipped_markets)

    # 2) books
    relations = correlated.load_relations()
    tokens = _interesting_tokens(markets, relations)
    books: Dict[str, OrderBook] = client.fetch_books(tokens.keys())
    log.info("fetched %d/%d order books", len(books), len(tokens))

    # 3) strategies
    opps = []
    opps += arbitrage.scan(markets, books)
    opps += correlated.scan(markets, books, relations)
    opps += longshot.scan(markets, books)
    opps += convergence.scan(markets, books)
    log.info("found %d candidate opportunities", len(opps))

    # 4) sizing
    from .risk import size_opportunities
    stats = engine.stats()
    sized = size_opportunities(
        opps,
        bankroll=stats["equity"],
        cash=engine.cash,
        strategy_exposure=engine.open_cost_by_strategy(),
        total_exposure=engine.total_open_cost(),
        open_keys=engine.open_keys(),
        open_longshots=engine.open_longshot_count(),
    )

    # 5) trade
    opened = [p for p in (engine.open_position(o) for o in sized) if p]

    # 6) take-profit — sell eligible open positions into LIVE bids, before
    # marking/settling, so the equity curve reflects the actual exit
    tp_tokens = {leg["token_id"]
                for pos in engine.state["positions"]
                if pos["strategy"] in config.TAKE_PROFIT_STRATEGIES
                for leg in pos["legs"]}
    tp_books = client.fetch_books(tp_tokens) if tp_tokens else {}
    tp_closed = engine.scan_take_profits(tp_books)
    if tp_closed:
        log.info("take-profit closed %d position(s) early: %s",
                 len(tp_closed), [c["key"] for c in tp_closed])

    # 7) mark + settle
    marks = {}
    for m in markets:
        marks[m.yes_token] = m.yes_price
        marks[m.no_token] = 1.0 - m.yes_price
    engine.mark_to_market(marks)

    # settle: first take anything visible in this scan's active-events feed
    # (cheap, no extra requests), THEN directly check any open position's
    # market that's still unresolved — because Gamma's active/open feed
    # stops returning a market the moment it closes, so a resolved trade
    # would otherwise sit "open" forever waiting for a feed that will
    # never show it again.
    outcomes = {}
    for ev in events:
        for raw in ev.get("markets", []) or []:
            r = client.parse_resolution(raw)
            if r:
                outcomes[str(raw.get("id"))] = r

    open_market_ids = {leg["market_id"] for pos in engine.state["positions"]
                       for leg in pos["legs"]} - set(outcomes)
    if open_market_ids:
        straggler_outcomes = client.fetch_resolutions(open_market_ids)
        if straggler_outcomes:
            log.info("resolved %d position(s) that had dropped out of the "
                     "active feed: %s", len(straggler_outcomes),
                     list(straggler_outcomes.keys()))
        outcomes.update(straggler_outcomes)

    settled = engine.resolve(outcomes) if outcomes else []

    # 8) persist + report
    engine.save()
    write_dashboard(engine.state, opportunities=[o.to_dict() for o in opps])
    return {"markets": len(markets), "opportunities": len(opps),
            "opened": len(opened), "take_profit_closed": len(tp_closed),
            "settled": len(settled), "stats": engine.stats()}


def selftest() -> int:
    """Offline check that all modules import and core math holds."""
    from .models import BookLevel
    b = OrderBook("t", asks=[BookLevel(0.4, 100)], bids=[BookLevel(0.38, 50)])
    assert abs(b.buyable_shares(20.0) - 50.0) < 1e-9
    from .risk import kelly_fraction
    assert kelly_fraction(0.5, 1.0) == 0.0          # fair coin, no edge
    assert 0.0 < kelly_fraction(0.6, 1.0) <= 0.2 + 1e-9
    print("selftest OK")
    return 0


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        sys.exit(selftest())
    summary = run_cycle()
    log.info("cycle done: %s", summary)


if __name__ == "__main__":
    main()
