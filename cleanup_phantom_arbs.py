"""One-off cleanup after the phantom-arb fix.

The pre-fix bot opened ARB "locks" on illiquid multi-outcome markets
(exact-score etc.) that were sized to tens of thousands of non-existent
sets and marked at $1 each — inflating equity to fantasy levels (the
$31k / +3053% you saw). This script removes that contamination.

Two modes:

  python cleanup_phantom_arbs.py --dry-run
      Show what's phantom without changing anything.

  python cleanup_phantom_arbs.py
      Void every open ARB position whose cost basis implies a phantom
      (entry cost per set below the new ARB_MIN_COST, i.e. the near-zero
      YES-sum signature), refund their cost, and rebuild the equity
      history so the curve reflects reality going forward.

  python cleanup_phantom_arbs.py --full-reset
      Nuke paper_state.json entirely and start fresh at the starting
      bankroll. RECOMMENDED if you want a clean measurement of the fixed
      bot, since some SETTLED phantom trades may also be inflated and
      those can't be surgically un-settled.

Honest note: --dry-run first. If the phantom settled trades are a large
share of your 133, --full-reset is the more trustworthy path — a clean
record of the fixed bot is worth more than preserving a contaminated one.
"""
import argparse
import os
import sys

from polyedge import config
from polyedge.paper import PaperEngine
from polyedge.report import write_dashboard


def _is_phantom(pos: dict) -> bool:
    """A locked ARB position whose per-set cost is below ARB_MIN_COST, or
    whose marked value wildly exceeds its cost (the phantom signature)."""
    if pos.get("strategy") != "ARB":
        return False
    legs = pos.get("legs", [])
    if not legs:
        return False
    sets = min((l.get("shares", 0) for l in legs), default=0)
    if sets <= 0:
        return False
    cost_per_set = pos.get("cost", 0) / sets if sets else 0
    # phantom if the lock was bought at an absurdly low per-set cost
    if 0 < cost_per_set < config.ARB_MIN_COST:
        return True
    # or if current marked value is >5x the cost (impossible for a real lock)
    if pos.get("current_value", 0) > pos.get("cost", 0) * 5:
        return True
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--full-reset", action="store_true")
    args = ap.parse_args()

    engine = PaperEngine()

    if args.full_reset:
        if args.dry_run:
            print("--full-reset would delete paper_state.json and start fresh "
                  f"at ${config.STARTING_BANKROLL:.0f}.")
            return 0
        if os.path.exists(engine.path):
            os.remove(engine.path)
        fresh = PaperEngine()
        fresh.mark_to_market({})
        fresh.save()
        write_dashboard(fresh.state, opportunities=[])
        print(f"Full reset done. Fresh bankroll ${config.STARTING_BANKROLL:.0f}, "
              "clean history. The fixed bot starts measuring from here.")
        return 0

    phantoms = [p for p in engine.state["positions"] if _is_phantom(p)]
    if not phantoms:
        print("No phantom ARB positions found in open positions.")
        print("NOTE: settled phantom trades (already closed) can't be detected "
              "here — if your equity still looks inflated, use --full-reset.")
        return 0

    freed = sum(p["cost"] for p in phantoms)
    print(f"Found {len(phantoms)} phantom ARB position(s):")
    for p in phantoms:
        sets = min(l["shares"] for l in p["legs"])
        print(f"  {p['title'][:50]} — cost ${p['cost']:.2f}, "
              f"{sets:.0f} sets, marked ${p.get('current_value', 0):.0f}")
    print(f"\nTotal cost basis to refund: ${freed:.2f}")

    if args.dry_run:
        print("\n--dry-run: nothing changed.")
        print("Tip: given settled phantoms may also be inflated, consider "
              "--full-reset for a clean measurement of the fixed bot.")
        return 0

    for p in phantoms:
        engine.void_position(p["key"], reason="voided_phantom_arb")
    # rebuild a clean current snapshot
    engine.mark_to_market({})
    engine.save()
    write_dashboard(engine.state, opportunities=[])
    print(f"\nVoided {len(phantoms)} phantom position(s), refunded ${freed:.2f}.")
    print("Open positions are clean now. If the equity curve still shows the "
          "old spike in its HISTORY, that's the pre-fix record — use "
          "--full-reset if you want a clean baseline for the fixed bot.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
