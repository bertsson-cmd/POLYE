"""Seed the paper engine with a simulated 30-day run and build the dashboard.

    python demo.py          # writes state/paper_state.json + docs/index.html

This is FAKE data for demonstration: it shows what the terminal looks like
after a month of scanning, including a longshot that lands (a loss) so the
dashboard honestly displays both sides. Delete state/paper_state.json to
start your real paper run from zero.
"""
import random
import time

from polyedge.models import Leg, Opportunity
from polyedge.paper import PaperEngine
from polyedge.report import write_dashboard

random.seed(95)
NOW = time.time()
DAY = 86400.0


def leg(tok, mid, label, side, px, sh):
    return Leg(tok, mid, label, side, px, sh)


def main():
    import os
    if os.path.exists("state/paper_state.json"):
        os.remove("state/paper_state.json")
    e = PaperEngine()
    t = NOW - 30 * DAY

    script = [
        # (day_offset, kind, ...)
        (0.2, "ARB", "YES-lock: World Cup R16 — Match winner set", 0.968, 48, 2),
        (1.1, "CV", "Converge: Fed holds rates in July", 0.962, 40, 3),
        (2.3, "LS", "Fade: Wildcard team reaches semi-final", 0.958, 30, 9, True),
        (3.0, "LS", "Fade: Manager sacked before quarter-final", 0.952, 25, 6, True),
        (4.6, "REL", "IMPLIES lock: wins group / qualifies", 0.973, 45, 4),
        (6.2, "CV", "Converge: Album releases before Aug 1", 0.955, 42, 4),
        (7.8, "LS", "Fade: 0-0 draw in final", 0.949, 28, 5, True),
        (9.5, "ARB", "NO-lock: Golden Boot top scorer set", 0.940, 30, 5),
        (11.0, "LS", "Fade: Hat-trick in semi-final", 0.951, 30, 8, False),  # LANDS -> loss
        (13.2, "CV", "Converge: Bill passes committee", 0.968, 45, 5),
        (15.4, "LS", "Fade: Red card in first half", 0.954, 26, 7, True),
        (17.1, "REL", "EXCLUSIVE lock: two rival winners", 0.976, 40, 6),
        (19.0, "CV", "Converge: Rocket launch by window close", 0.958, 44, 4),
        (21.3, "LS", "Fade: Both keepers score", 0.960, 22, 10, True),
        (23.7, "ARB", "YES-lock: Best Young Player set", 0.972, 46, 3),
        (25.2, "CV", "Converge: Court ruling published", 0.966, 42, 3),
        (27.0, "LS", "Fade: Extra time in both semis", 0.947, 24, 8, True),
    ]

    open_ts = {}
    for row in script:
        d, kind = row[0], row[1]
        ts = t + d * DAY
        if kind == "ARB":
            _, _, title, cost_per_set, sets, resolve_days = row
            # two-leg lock paying $1/set
            p1 = round(random.uniform(0.25, 0.55), 2)
            p2 = round(cost_per_set - p1, 2)
            opp = Opportunity("ARB", f"ARB-{title[:18]}-{d}", title,
                              1 - cost_per_set, True,
                              legs=[leg("a"+str(d), "ma"+str(d), "YES leg A", "YES", p1, sets),
                                    leg("b"+str(d), "mb"+str(d), "YES leg B", "YES", p2, sets)],
                              guaranteed_payout=1.0,
                              note=f"asks sum {cost_per_set:.3f}")
            e.open_position(opp, ts=ts)
            open_ts[opp.key] = (ts + resolve_days * DAY,
                                {"ma"+str(d): "YES", "mb"+str(d): "NO"})
        elif kind == "REL":
            _, _, title, cost, sets, resolve_days = row
            pa = round(cost / 2, 3)
            opp = Opportunity("REL", f"REL-{d}", title, 1 - cost, True,
                              legs=[leg("ra"+str(d), "rma"+str(d), "YES B", "YES", pa, sets),
                                    leg("rb"+str(d), "rmb"+str(d), "NO A", "NO", cost - pa, sets)],
                              guaranteed_payout=1.0, note=f"legs sum {cost:.3f}")
            e.open_position(opp, ts=ts)
            # settle at the lock's MINIMUM payout case (A no, B no):
            # only the NO leg pays — realistic, not the lucky double-payout
            open_ts[opp.key] = (ts + resolve_days * DAY,
                                {"rma"+str(d): "NO", "rmb"+str(d): "NO"})
        elif kind == "LS":
            _, _, title, ask, shares, resolve_days, no_wins = row
            opp = Opportunity("LONGSHOT", f"LS-{d}", title,
                              (0.97 - ask) / ask, False, est_p_win=0.97,
                              legs=[leg("l"+str(d), "lm"+str(d), "NO", "NO", ask, shares)],
                              note=f"NO ask {ask:.3f}")
            e.open_position(opp, ts=ts)
            open_ts[opp.key] = (ts + resolve_days * DAY,
                                {"lm"+str(d): "NO" if no_wins else "YES"})
        elif kind == "CV":
            _, _, title, ask, shares, resolve_days = row
            opp = Opportunity("CONVERGE", f"CV-{d}", title,
                              (1 - ask) / ask, False, est_p_win=ask,
                              legs=[leg("c"+str(d), "cm"+str(d), "YES", "YES", ask, shares)],
                              note=f"YES ask {ask:.3f}")
            e.open_position(opp, ts=ts)
            open_ts[opp.key] = (ts + resolve_days * DAY, {"cm"+str(d): "YES"})

    # replay time: twice-daily marks, settle when due
    settle_queue = sorted((v[0], v[1]) for v in open_ts.values())
    tick = t
    qi = 0
    while tick <= NOW:
        while qi < len(settle_queue) and settle_queue[qi][0] <= tick:
            e.resolve(settle_queue[qi][1], ts=settle_queue[qi][0])
            qi += 1
        # mark open positions with slight noise around entry
        marks = {}
        for p in e.state["positions"]:
            for l in p["legs"]:
                marks[l["token_id"]] = min(0.995, max(0.005,
                    l["entry_price"] + random.uniform(-0.01, 0.02)))
        e.mark_to_market(marks, ts=tick)
        tick += DAY / 2

    e.save()
    demo_opps = [
        {"strategy": "ARB", "title": "YES-lock: QF winner set (demo)", "edge": 0.021,
         "guaranteed": True, "note": "4 outcomes, YES asks sum 0.9790, 210 sets executable"},
        {"strategy": "LONGSHOT", "title": "Fade: keeper scores in QF (demo)", "edge": 0.017,
         "guaranteed": False, "note": "YES at 0.030, assumed true 0.018, NO ask 0.955"},
        {"strategy": "CONVERGE", "title": "Converge: treaty signed by Friday (demo)", "edge": 0.031,
         "guaranteed": False, "note": "YES ask 0.970, 3.5d to resolution, 322% annualized if YES"},
    ]
    write_dashboard(e.state, opportunities=demo_opps)
    print("stats:", e.stats())


if __name__ == "__main__":
    main()
