"""PolyEdge 95 v2 test suite.

Run:  pytest tests/ -v

Covers:
  * order book math (fills, depth)
  * ARB: YES-lock and NO-lock detection, edges, depth limiting, no false positives
  * REL: IMPLIES and EXCLUSIVE lock payoffs verified over ALL outcome combos
  * LONGSHOT: filters, EV math, one-fade-per-event
  * CONVERGE: yield filters
  * risk: Kelly correctness, every cap enforced
  * paper engine: accounting invariant equity == cash + marked value,
    settlement payouts, atomic persistence round-trip
"""
import itertools
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from polyedge import config
from polyedge.models import BookLevel, Leg, Market, Opportunity, OrderBook
from polyedge.paper import PaperEngine
from polyedge.risk import kelly_fraction, size_opportunities
from polyedge.strategies import arbitrage, convergence, correlated, longshot


# ------------------------------------------------------------------ helpers
def book(token, ask, size=1000.0, bid=None):
    return OrderBook(token,
                     asks=[BookLevel(ask, size)],
                     bids=[BookLevel(bid if bid is not None else max(0.01, ask - 0.02), size)])


def market(mid, yes_price, *, neg_risk=False, event="EV1", liq=50000.0,
           end="2026-07-20T00:00:00Z", question=None):
    return Market(market_id=mid, question=question or f"Q{mid}",
                  yes_token=f"{mid}-Y", no_token=f"{mid}-N",
                  yes_price=yes_price, liquidity=liq, end_date=end,
                  event_id=event, event_title=f"Event {event}", neg_risk=neg_risk)


# ------------------------------------------------------------------ order book
class TestOrderBook:
    def test_buyable_shares_walks_levels(self):
        b = OrderBook("t", asks=[BookLevel(0.40, 100), BookLevel(0.50, 100)])
        # $40 buys the first level exactly
        assert b.buyable_shares(40.0) == pytest.approx(100.0)
        # $65 buys 100 @0.40 + 50 @0.50
        assert b.buyable_shares(65.0) == pytest.approx(150.0)

    def test_avg_fill_price(self):
        b = OrderBook("t", asks=[BookLevel(0.40, 100), BookLevel(0.50, 100)])
        assert b.avg_fill_price(150) == pytest.approx((100 * .4 + 50 * .5) / 150)
        assert b.avg_fill_price(300) is None      # book too thin


# ------------------------------------------------------------------ ARB
class TestArbitrage:
    def _mk_event(self, yes_asks, no_asks, sizes=None):
        n = len(yes_asks)
        sizes = sizes or [1000.0] * n
        ms = [market(f"M{i}", yes_asks[i], neg_risk=True) for i in range(n)]
        books = {}
        for i, m in enumerate(ms):
            books[m.yes_token] = book(m.yes_token, yes_asks[i], sizes[i])
            books[m.no_token] = book(m.no_token, no_asks[i], sizes[i])
        return ms, books

    def test_yes_lock_detected_and_edge_exact(self):
        # YES asks sum to 0.95 -> guaranteed edge 0.05 per set
        ms, books = self._mk_event([0.30, 0.35, 0.30], [0.72, 0.67, 0.72])
        opps = [o for o in arbitrage.scan(ms, books) if o.key.startswith("ARB-YES")]
        assert len(opps) == 1
        o = opps[0]
        assert o.edge == pytest.approx(0.05)
        assert o.guaranteed and o.guaranteed_payout == 1.0
        # verify the lock really pays $1 whichever outcome wins
        cost = o.total_cost()
        sets = o.legs[0].shares
        for winner in range(3):
            payout = sum(l.shares * (1.0 if i == winner else 0.0)
                         for i, l in enumerate(o.legs))
            assert payout == pytest.approx(sets * 1.0)
        assert sets * 1.0 - cost == pytest.approx(sets * 0.05)

    def test_no_lock_detected_and_pays_n_minus_1(self):
        # 3 outcomes, NO asks sum 1.90 < 2.0 payout -> lock
        ms, books = self._mk_event([0.40, 0.35, 0.30], [0.62, 0.64, 0.64])
        opps = [o for o in arbitrage.scan(ms, books) if o.key.startswith("ARB-NO")]
        assert len(opps) == 1
        o = opps[0]
        sets = o.legs[0].shares
        # whichever single outcome wins, exactly N-1 NOs pay $1
        for winner in range(3):
            payout = sum(l.shares * (0.0 if i == winner else 1.0)
                         for i, l in enumerate(o.legs))
            assert payout == pytest.approx(sets * 2.0)
        assert o.guaranteed_payout == pytest.approx(2.0)
        assert sets * 2.0 - o.total_cost() == pytest.approx(sets * (2.0 - 1.90))

    def test_no_false_positive_when_sum_fair_or_above(self):
        ms, books = self._mk_event([0.34, 0.34, 0.34], [0.67, 0.67, 0.67])
        assert arbitrage.scan(ms, books) == []      # 1.02 and 2.01: no locks

    def test_edge_below_threshold_ignored(self):
        # sum 0.995 -> edge 0.005 < ARB_MIN_EDGE (0.01)
        ms, books = self._mk_event([0.33, 0.33, 0.335], [0.7, 0.7, 0.7])
        assert not [o for o in arbitrage.scan(ms, books) if "YES" in o.key]

    def test_size_limited_by_thinnest_leg(self):
        ms, books = self._mk_event([0.30, 0.35, 0.30], [0.9, 0.9, 0.9],
                                   sizes=[1000, 40, 1000])
        o = [x for x in arbitrage.scan(ms, books) if "YES" in x.key][0]
        assert all(l.shares == pytest.approx(40) for l in o.legs)

    def test_single_market_event_skipped(self):
        ms, books = self._mk_event([0.5], [0.5])
        assert arbitrage.scan(ms, books) == []


# ------------------------------------------------------------------ REL
class TestCorrelated:
    def test_implies_lock_payoff_all_cases(self):
        # A => B. ask YES(B)=0.55, ask NO(A)=0.40 -> cost 0.95, lock 0.05
        a, b = market("A", 0.35), market("B", 0.55)
        books = {b.yes_token: book(b.yes_token, 0.55),
                 a.no_token: book(a.no_token, 0.40)}
        rels = [{"type": "IMPLIES", "a_market_id": "A", "b_market_id": "B"}]
        opps = correlated.scan([a, b], books, rels)
        assert len(opps) == 1
        o = opps[0]
        assert o.edge == pytest.approx(0.05)
        sets = o.legs[0].shares
        # enumerate logically POSSIBLE worlds (A yes & B no is impossible)
        for a_yes, b_yes in [(1, 1), (0, 1), (0, 0)]:
            payout = 0.0
            for l in o.legs:
                if l.market_id == "B" and l.side == "YES":
                    payout += l.shares * b_yes
                if l.market_id == "A" and l.side == "NO":
                    payout += l.shares * (1 - a_yes)
            assert payout >= sets * 1.0 - 1e-9   # never below the lock

    def test_exclusive_lock_payoff_all_cases(self):
        a, b = market("A", 0.55), market("B", 0.50)
        books = {a.no_token: book(a.no_token, 0.46),
                 b.no_token: book(b.no_token, 0.50)}
        rels = [{"type": "EXCLUSIVE", "a_market_id": "A", "b_market_id": "B"}]
        o = correlated.scan([a, b], books, rels)[0]
        sets = o.legs[0].shares
        # possible worlds: at most one of A,B yes
        for a_yes, b_yes in [(1, 0), (0, 1), (0, 0)]:
            payout = sum(l.shares * (1 - (a_yes if l.market_id == "A" else b_yes))
                         for l in o.legs)
            assert payout >= sets * 1.0 - 1e-9

    def test_no_lock_when_prices_coherent(self):
        a, b = market("A", 0.35), market("B", 0.55)
        books = {b.yes_token: book(b.yes_token, 0.60),
                 a.no_token: book(a.no_token, 0.66)}   # sum 1.26 > 1
        rels = [{"type": "IMPLIES", "a_market_id": "A", "b_market_id": "B"}]
        assert correlated.scan([a, b], books, rels) == []

    def test_missing_market_or_book_is_safe(self):
        a = market("A", 0.35)
        rels = [{"type": "IMPLIES", "a_market_id": "A", "b_market_id": "GONE"}]
        assert correlated.scan([a], {}, rels) == []


# ------------------------------------------------------------------ LONGSHOT
class TestLongshot:
    def test_fade_detected_with_correct_ev(self):
        m = market("L1", 0.04, end="2026-07-20T00:00:00Z")
        books = {m.no_token: book(m.no_token, 0.965)}
        o = longshot.scan([m], books)[0]
        true_p_yes = 0.04 * config.LS_BIAS_HAIRCUT
        expect = ((1 - true_p_yes) - 0.965) / 0.965
        assert o.edge == pytest.approx(expect)
        assert o.est_p_win == pytest.approx(1 - true_p_yes)

    def test_filters(self):
        # price out of band
        m1 = market("L1", 0.08)
        # too illiquid
        m2 = market("L2", 0.04, liq=10.0)
        # too far out
        m3 = market("L3", 0.04, end="2027-12-31T00:00:00Z")
        books = {m.no_token: book(m.no_token, 0.96) for m in (m1, m2, m3)}
        assert longshot.scan([m1, m2, m3], books) == []

    def test_one_fade_per_event(self):
        m1 = market("L1", 0.04, event="SAME")
        m2 = market("L2", 0.03, event="SAME")
        books = {m.no_token: book(m.no_token, 0.96) for m in (m1, m2)}
        assert len(longshot.scan([m1, m2], books)) == 1

    def test_negative_ev_skipped(self):
        # NO ask so high there's no edge even with haircut
        m = market("L1", 0.05)
        books = {m.no_token: book(m.no_token, 0.995)}
        # EV = (1 - 0.03) - 0.995 = -0.025 < 0
        assert longshot.scan([m], books) == []


# ------------------------------------------------------------------ CONVERGE
class TestConvergence:
    def test_pick_and_yield(self):
        m = market("C1", 0.96, end="2026-07-18T00:00:00Z", liq=99999)
        books = {m.yes_token: book(m.yes_token, 0.96)}
        o = convergence.scan([m], books)[0]
        assert o.edge == pytest.approx((1 - 0.96) / 0.96)
        assert not o.guaranteed

    def test_low_annualized_yield_rejected(self):
        # 0.984 with ~2 weeks left under a high APY floor? craft a clear reject:
        # price 0.984, 14 days -> apy = (0.016/0.984)*365/14 = 42% -> passes 25%
        # so use far date within window? use max days with tiny yield via config override
        m = market("C1", 0.984, end="2026-07-27T23:00:00Z", liq=99999)
        books = {m.yes_token: book(m.yes_token, 0.984)}
        old = config.CV_MIN_ANNUAL_YIELD
        config.CV_MIN_ANNUAL_YIELD = 1.0     # demand 100% APY
        try:
            assert convergence.scan([m], books) == []
        finally:
            config.CV_MIN_ANNUAL_YIELD = old


# ------------------------------------------------------------------ risk
class TestRisk:
    def test_kelly_zero_for_fair_and_negative_edges(self):
        assert kelly_fraction(0.5, 1.0) == 0.0
        assert kelly_fraction(0.3, 1.0) == 0.0
        assert kelly_fraction(0.0, 2.0) == 0.0
        assert kelly_fraction(1.0, 2.0) == 0.0   # degenerate p rejected

    def test_kelly_known_value(self):
        # p=0.6, even odds -> f* = 0.2
        assert kelly_fraction(0.6, 1.0) == pytest.approx(0.2)

    def _ls_opp(self, key="LS-1", ask=0.95, p_win=0.97):
        return Opportunity(strategy="LONGSHOT", key=key, title=key,
                           edge=(p_win - ask) / ask, guaranteed=False,
                           est_p_win=p_win,
                           legs=[Leg("t", "m", "NO x", "NO", ask, 0.0)])

    def test_position_cap_enforced(self):
        opp = self._ls_opp(p_win=0.999)   # huge Kelly, must be capped
        sized = size_opportunities([opp], bankroll=1000, cash=1000,
                                   strategy_exposure={}, total_exposure=0)
        assert sized and sized[0].total_cost() <= 1000 * config.MAX_POSITION_PCT + 1e-6

    def test_cash_and_exposure_caps(self):
        opps = [self._ls_opp(f"LS-{i}", p_win=0.999) for i in range(30)]
        sized = size_opportunities(opps, bankroll=1000, cash=100,
                                   strategy_exposure={}, total_exposure=0)
        assert sum(o.total_cost() for o in sized) <= 100 + 1e-6

    def test_strategy_cap_enforced(self):
        opps = [self._ls_opp(f"LS-{i}", p_win=0.999) for i in range(30)]
        sized = size_opportunities(opps, bankroll=1000, cash=1000,
                                   strategy_exposure={}, total_exposure=0)
        cap = 1000 * config.MAX_STRATEGY_EXPOSURE_PCT["LONGSHOT"]
        assert sum(o.total_cost() for o in sized) <= cap + 1e-6

    def test_longshot_slots_enforced(self):
        opps = [self._ls_opp(f"LS-{i}", p_win=0.999) for i in range(30)]
        sized = size_opportunities(opps, bankroll=100000, cash=100000,
                                   strategy_exposure={}, total_exposure=0)
        assert len(sized) <= config.LS_MAX_OPEN

    def test_open_keys_deduped(self):
        opp = self._ls_opp("LS-DUP", p_win=0.999)
        sized = size_opportunities([opp], bankroll=1000, cash=1000,
                                   strategy_exposure={}, total_exposure=0,
                                   open_keys={"LS-DUP"})
        assert sized == []

    def test_guaranteed_scaled_to_cap(self):
        legs = [Leg("a", "ma", "YES a", "YES", 0.30, 1000),
                Leg("b", "mb", "YES b", "YES", 0.65, 1000)]
        opp = Opportunity(strategy="ARB", key="ARB-1", title="lock",
                          edge=0.05, guaranteed=True, legs=legs,
                          guaranteed_payout=1.0)
        sized = size_opportunities([opp], bankroll=1000, cash=1000,
                                   strategy_exposure={}, total_exposure=0)
        cost = sized[0].total_cost()
        assert cost <= 1000 * config.MAX_POSITION_PCT + 1e-6
        # legs scaled equally -> still a complete set
        assert sized[0].legs[0].shares == pytest.approx(sized[0].legs[1].shares)


# ------------------------------------------------------------------ paper engine
class TestPaperEngine:
    def _engine(self, tmp_path):
        return PaperEngine(state_dir=str(tmp_path))

    def _arb_opp(self, sets=50.0):
        legs = [Leg("a", "ma", "YES a", "YES", 0.30, sets),
                Leg("b", "mb", "YES b", "YES", 0.65, sets)]
        return Opportunity(strategy="ARB", key="ARB-T", title="lock",
                           edge=0.05, guaranteed=True, legs=legs,
                           guaranteed_payout=1.0)

    def test_open_reduces_cash_exactly(self, tmp_path):
        e = self._engine(tmp_path)
        start = e.cash
        opp = self._arb_opp()
        pos = e.open_position(opp)
        assert pos is not None
        assert e.cash == pytest.approx(start - opp.total_cost())

    def test_cannot_overspend_or_duplicate(self, tmp_path):
        e = self._engine(tmp_path)
        big = self._arb_opp(sets=1e6)
        assert e.open_position(big) is None            # over cash
        small = self._arb_opp(sets=50)
        assert e.open_position(small) is not None
        assert e.open_position(self._arb_opp(sets=50)) is None   # dup key

    def test_equity_invariant_and_settlement(self, tmp_path):
        e = self._engine(tmp_path)
        start = e.cash
        opp = self._arb_opp(sets=100)                  # cost = 95
        e.open_position(opp)
        # mark: prices move but the lock floors value at 100 * $1
        pt = e.mark_to_market({"a": 0.10, "b": 0.10})
        assert pt["equity"] == pytest.approx(pt["cash"] + pt["open_value"])
        assert pt["open_value"] == pytest.approx(100.0)   # lock floor
        # settle: outcome a wins, b loses -> payout = 100 * 1
        settled = e.resolve({"ma": "YES", "mb": "NO"})
        assert len(settled) == 1
        assert settled[0]["payout"] == pytest.approx(100.0)
        assert settled[0]["pl"] == pytest.approx(5.0)
        assert e.cash == pytest.approx(start - 95.0 + 100.0)
        assert e.state["positions"] == []

    def test_partial_resolution_keeps_position_open(self, tmp_path):
        e = self._engine(tmp_path)
        e.open_position(self._arb_opp(sets=100))
        assert e.resolve({"ma": "YES"}) == []          # mb unknown
        assert len(e.state["positions"]) == 1

    def test_persistence_roundtrip(self, tmp_path):
        e = self._engine(tmp_path)
        e.open_position(self._arb_opp(sets=100))
        e.mark_to_market({"a": 0.3, "b": 0.65})
        e.save()
        e2 = PaperEngine(state_dir=str(tmp_path))
        assert e2.cash == pytest.approx(e.cash)
        assert len(e2.state["positions"]) == 1
        assert e2.state["history"] == e.state["history"]

    def test_losing_longshot_accounting(self, tmp_path):
        e = self._engine(tmp_path)
        start = e.cash
        opp = Opportunity(strategy="LONGSHOT", key="LS-X", title="fade",
                          edge=0.01, guaranteed=False, est_p_win=0.97,
                          legs=[Leg("n", "mx", "NO x", "NO", 0.95, 20.0)])
        e.open_position(opp)                           # cost 19
        settled = e.resolve({"mx": "YES"})             # longshot LANDS: NO pays 0
        assert settled[0]["payout"] == pytest.approx(0.0)
        assert settled[0]["pl"] == pytest.approx(-19.0)
        assert e.cash == pytest.approx(start - 19.0)
        s = e.stats()
        assert s["closed_trades"] == 1 and s["win_rate_pct"] == 0.0
