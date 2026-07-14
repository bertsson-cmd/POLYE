"""One-off maintenance: void ARB/REL locks that violate the horizon cap.

Run this once after adding ARB_MAX_DAYS / REL_MAX_DAYS to config.py, if you
already had far-dated locks open from before the cap existed. It does NOT
touch anything else — CONVERGE/LONGSHOT positions, near-dated locks, and
your settled trade history are all left exactly as they are.

Cancelling a lock refunds its exact cost basis (no invented profit or
loss — it's paper money, so voiding just returns the capital that was
"spent" on it, as if the trade never happened) and frees that capital for
new trades on the next scan.

    python cancel_stale_locks.py            # void + show summary
    python cancel_stale_locks.py --dry-run  # show what WOULD be voided, change nothing
"""
import argparse
import sys

from polyedge import config
from polyedge.models import days_to_resolution
from polyedge.paper import PaperEngine
from polyedge.report import write_dashboard

HORIZON = {"ARB": config.ARB_MAX_DAYS, "REL": config.REL_MAX_DAYS}


def find_stale(engine: PaperEngine):
    stale = []
    for pos in engine.state["positions"]:
        cap = HORIZON.get(pos["strategy"])
        if cap is None:
            continue                      # not a lock strategy, leave alone
        days = days_to_resolution(pos.get("resolve_by", ""))
        if days > cap:
            stale.append((pos, days))
    return stale


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="show what would be voided without changing anything")
    args = ap.parse_args()

    engine = PaperEngine()
    stale = find_stale(engine)

    if not stale:
        print("No positions violate the current horizon cap. Nothing to do.")
        return 0

    total_freed = sum(p["cost"] for p, _ in stale)
    print(f"Found {len(stale)} position(s) beyond the horizon cap "
         f"(ARB > {config.ARB_MAX_DAYS}d, REL > {config.REL_MAX_DAYS}d):\n")
    for pos, days in stale:
        print(f"  [{pos['strategy']}] {pos['title']}  "
             f"resolves in {days:.0f}d, cost ${pos['cost']:.2f}")
    print(f"\nTotal capital that would be freed: ${total_freed:.2f}")

    if args.dry_run:
        print("\n--dry-run: no changes made.")
        return 0

    for pos, _ in stale:
        engine.void_position(pos["key"], reason="voided_horizon_cap")

    # record a fresh equity snapshot so the dashboard header shows the
    # refunded cash immediately (not the last pre-void mark)
    engine.mark_to_market({})
    engine.save()
    write_dashboard(engine.state, opportunities=[])
    print(f"\nVoided {len(stale)} position(s). ${total_freed:.2f} freed and "
         f"returned to cash. State saved and dashboard regenerated.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
