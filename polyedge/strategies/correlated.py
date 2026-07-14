"""REL — logical-bound violations between related markets.

Relations are declared by YOU in relations.json (auto-detecting logical
implication from question text is unreliable, so we don't pretend to).

Supported relation types and their risk-free locks:

  IMPLIES  (A => B, e.g. A = "Argentina wins the cup", B = "Argentina
            reaches the final"). Logic forces P(A) <= P(B).
      Lock: buy YES(B) + buy NO(A).
      Case A yes  -> B must be yes -> payout 1 (+ possibly more never less)
      Case A no              -> NO(A) pays 1
      Minimum payout = $1 per set. Profitable if ask_yes(B) + ask_no(A) < 1.

  EXCLUSIVE (A and B cannot both happen, but both may fail —
             e.g. "France wins the cup" / "Brazil wins the cup").
      Logic forces P(A) + P(B) <= 1.
      Lock: buy NO(A) + buy NO(B).
      At most one of A,B resolves YES, so AT LEAST one NO pays $1.
      Minimum payout = $1 per set. Profitable if ask_no(A) + ask_no(B) < 1.

relations.json format:
[
  {"type": "IMPLIES", "a_market_id": "123", "b_market_id": "456",
   "note": "wins cup => reaches final"},
  {"type": "EXCLUSIVE", "a_market_id": "111", "b_market_id": "222"}
]
"""
import json
import logging
import os
from typing import Dict, List

from .. import config
from ..models import Leg, Market, Opportunity, OrderBook, days_to_resolution

log = logging.getLogger("polyedge.rel")


def load_relations(path: str = None) -> List[dict]:
    path = path or config.RELATIONS_FILE
    if not os.path.exists(path):
        return []
    try:
        with open(path) as f:
            rels = json.load(f)
        return [r for r in rels if r.get("type") in ("IMPLIES", "EXCLUSIVE")
                and r.get("a_market_id") and r.get("b_market_id")]
    except (ValueError, OSError) as e:
        log.warning("could not load relations file %s: %s", path, e)
        return []


def _lock(kind: str, rel: dict, a: Market, b: Market,
          leg_a: Leg, leg_b: Leg, cost: float) -> Opportunity:
    edge = (1.0 - cost) - config.FEE_RATE * cost
    return Opportunity(
        strategy="REL",
        key=f"REL-{rel['type']}-{a.market_id}-{b.market_id}",
        title=f"{rel['type']} lock: {a.question[:40]} / {b.question[:40]}",
        edge=edge, guaranteed=True, legs=[leg_a, leg_b],
        guaranteed_payout=1.0,
        resolve_by=max(a.end_date, b.end_date),
        note=rel.get("note", "") + f" | legs sum {cost:.4f}",
    )


def scan(all_markets: List[Market], books: Dict[str, OrderBook],
         relations: List[dict] = None) -> List[Opportunity]:
    relations = load_relations() if relations is None else relations
    by_id = {m.market_id: m for m in all_markets}
    out: List[Opportunity] = []

    for rel in relations:
        a = by_id.get(str(rel["a_market_id"]))
        b = by_id.get(str(rel["b_market_id"]))
        if not a or not b:
            continue

        # horizon cap: capital is tied up until the LAST leg resolves,
        # so the later of the two end dates is what matters
        if max(days_to_resolution(a.end_date),
               days_to_resolution(b.end_date)) > config.REL_MAX_DAYS:
            continue

        if rel["type"] == "IMPLIES":
            # lock = YES(B) + NO(A)
            book_b_yes = books.get(b.yes_token)
            book_a_no = books.get(a.no_token)
            if not (book_b_yes and book_a_no):
                continue
            pb, pa = book_b_yes.best_ask(), book_a_no.best_ask()
            if pb is None or pa is None:
                continue
            cost = pb + pa
            if (1.0 - cost) < config.REL_MIN_EDGE:
                continue
            size = min(book_b_yes.asks[0].size, book_a_no.asks[0].size)
            if size <= 0:
                continue
            leg_b = Leg(b.yes_token, b.market_id, f"YES {b.question}", "YES", pb, size)
            leg_a = Leg(a.no_token, a.market_id, f"NO {a.question}", "NO", pa, size)
            out.append(_lock("IMPLIES", rel, a, b, leg_a, leg_b, cost))

        elif rel["type"] == "EXCLUSIVE":
            # lock = NO(A) + NO(B)
            book_a_no = books.get(a.no_token)
            book_b_no = books.get(b.no_token)
            if not (book_a_no and book_b_no):
                continue
            pa, pb = book_a_no.best_ask(), book_b_no.best_ask()
            if pa is None or pb is None:
                continue
            cost = pa + pb
            if (1.0 - cost) < config.REL_MIN_EDGE:
                continue
            size = min(book_a_no.asks[0].size, book_b_no.asks[0].size)
            if size <= 0:
                continue
            leg_a = Leg(a.no_token, a.market_id, f"NO {a.question}", "NO", pa, size)
            leg_b = Leg(b.no_token, b.market_id, f"NO {b.question}", "NO", pb, size)
            out.append(_lock("EXCLUSIVE", rel, a, b, leg_a, leg_b, cost))

    return out
