"""PolyEdge 95 v2 — one full scan cycle.

    python -m polyedge.main            # live data scan (paper fills)
    python -m polyedge.main --selftest # offline sanity check, no network

Cycle:
  1. fetch open events (Gamma) and parse markets
  2. fetch order books (CLOB) only for tokens the strategies care about
  3. run all four strategy scans
  4. size candidates through the risk module
  5. open paper positions, mark to market, settle resolved markets
  6. write state and regenerate the dashboard
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
    """token_id -> why we need its book. Keeps CLOB calls bounded."""
    need: Dict[str, str] = {}
    rel_ids = {str(r.get("a_market_id")) for r in relations} | \
              {str(r.get("b_market_id")) for r in relations}
    for m in markets:
        if m.neg_risk:
            need[m.yes_token] = "arb"
            need[m.no_token] = "arb"
        if m.market_id in rel_ids:
            need[m.yes_token] = "rel"
            need[m.no_token] = "rel"
        if config.LS_MIN_YES_PRICE <= m.yes_price <= config.LS_MAX_YES_PRICE:
            need[m.no_token] = "longshot"
        if config.CV_MIN_YES_PRICE <= m.yes_price <= config.CV_MAX_YES_PRICE:
            need[m.yes_token] = "converge"
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
    books: Dict[str, OrderBook] = {}
    for tid in tokens:
        b = client.fetch_book(tid)
        if b:
            books[tid] = b
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

    # 5) trade + mark
    opened = [p for p in (engine.open_position(o) for o in sized) if p]
    marks = {}
    for m in markets:
        marks[m.yes_token] = m.yes_price
        marks[m.no_token] = 1.0 - m.yes_price
    engine.mark_to_market(marks)

    # settle anything Gamma reports as resolved
    outcomes = {}
    for ev in events:
        for raw in ev.get("markets", []) or []:
            if raw.get("closed") and raw.get("umaResolutionStatus") in ("resolved", "settled"):
                prices = raw.get("outcomePrices")
                if isinstance(prices, str):
                    import json as _j
                    try:
                        prices = _j.loads(prices)
                    except ValueError:
                        prices = None
                if isinstance(prices, list) and len(prices) == 2:
                    try:
                        yes_won = float(prices[0]) > 0.5
                        outcomes[str(raw.get("id"))] = "YES" if yes_won else "NO"
                    except (TypeError, ValueError):
                        pass
    settled = engine.resolve(outcomes) if outcomes else []

    # 6) persist + report
    engine.save()
    write_dashboard(engine.state, opportunities=[o.to_dict() for o in opps])
    return {"markets": len(markets), "opportunities": len(opps),
            "opened": len(opened), "settled": len(settled),
            "stats": engine.stats()}


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
