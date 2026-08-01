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
import requests

from polyedge import config, fees, reconcile
from polyedge.api import PolymarketClient
from polyedge.models import BookLevel, Leg, Market, Opportunity, OrderBook
from polyedge.paper import PaperEngine
from polyedge.risk import kelly_fraction, size_opportunities
from polyedge.strategies import arbitrage, convergence, correlated, longshot


@pytest.fixture(autouse=True)
def _zero_fees_by_default(monkeypatch):
    """Most of this suite predates fee-awareness and asserts exact pre-fee
    edge/payoff math (Kelly values, lock payouts, guard thresholds...).
    Force fees to 0 by default so those tests stay meaningful without
    hand-computing a fee delta into every expected value. Tests that
    specifically exercise fee math (TestFees, the per-strategy
    fee-wiring tests below) override POLYEDGE_FEE_RATE_OVERRIDE back to
    None (real category table) or a specific rate within their own body."""
    monkeypatch.setattr(config, "FEE_RATE_OVERRIDE", 0.0)


# ------------------------------------------------------------------ helpers
def book(token, ask, size=1000.0, bid=None):
    return OrderBook(token,
                     asks=[BookLevel(ask, size)],
                     bids=[BookLevel(bid if bid is not None else max(0.01, ask - 0.02), size)])


def _future(days=5):
    import datetime as _d
    return (_d.datetime.now(_d.timezone.utc) + _d.timedelta(days=days)
            ).strftime("%Y-%m-%dT%H:%M:%SZ")


def market(mid, yes_price, *, neg_risk=False, event="EV1", liq=50000.0,
           end=None, question=None):
    return Market(market_id=mid, question=question or f"Q{mid}",
                  yes_token=f"{mid}-Y", no_token=f"{mid}-N",
                  yes_price=yes_price, liquidity=liq,
                  end_date=end if end is not None else _future(5),
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


# ------------------------------------------------------------------ fees
class TestFees:
    def test_known_category_rates(self, monkeypatch):
        monkeypatch.setattr(config, "FEE_RATE_OVERRIDE", None)   # use the real table
        assert fees.fee_rate_for_category("crypto") == pytest.approx(0.07)
        assert fees.fee_rate_for_category("Sports") == pytest.approx(0.05)
        assert fees.fee_rate_for_category("weather") == pytest.approx(0.05)
        assert fees.fee_rate_for_category("politics") == pytest.approx(0.04)
        assert fees.fee_rate_for_category("tech") == pytest.approx(0.04)
        assert fees.fee_rate_for_category("mentions") == pytest.approx(0.04)
        assert fees.fee_rate_for_category("geopolitics") == pytest.approx(0.0)
        assert fees.fee_rate_for_category("world affairs") == pytest.approx(0.0)

    def test_unrecognized_category_falls_back_to_default(self, monkeypatch):
        monkeypatch.setattr(config, "FEE_RATE_OVERRIDE", None)
        assert fees.fee_rate_for_category("some made-up tag") == pytest.approx(fees.DEFAULT_FEE_RATE)
        assert fees.fee_rate_for_category("") == pytest.approx(fees.DEFAULT_FEE_RATE)
        assert fees.fee_rate_for_category(None) == pytest.approx(fees.DEFAULT_FEE_RATE)

    def test_fee_per_share_matches_documented_formula(self, monkeypatch):
        monkeypatch.setattr(config, "FEE_RATE_OVERRIDE", None)
        # fee = feeRate * price * (1 - price)
        assert fees.fee_per_share(0.5, "crypto") == pytest.approx(0.07 * 0.5 * 0.5)
        assert fees.fee_per_share(0.96, "politics") == pytest.approx(0.04 * 0.96 * 0.04)
        assert fees.fee_per_share(0.04, "sport") == pytest.approx(0.05 * 0.04 * 0.96)
        assert fees.fee_per_share(0.5, "geopolitics") == pytest.approx(0.0)

    def test_fee_peaks_at_50c_and_shrinks_at_extremes(self, monkeypatch):
        monkeypatch.setattr(config, "FEE_RATE_OVERRIDE", None)
        mid = fees.fee_per_share(0.50, "crypto")
        near_one = fees.fee_per_share(0.97, "crypto")
        near_zero = fees.fee_per_share(0.03, "crypto")
        assert mid > near_one and mid > near_zero

    def test_price_clamped_to_valid_range(self, monkeypatch):
        monkeypatch.setattr(config, "FEE_RATE_OVERRIDE", None)
        assert fees.fee_per_share(-0.5, "crypto") == pytest.approx(0.0)
        assert fees.fee_per_share(1.5, "crypto") == pytest.approx(0.0)

    def test_override_takes_priority_over_category_table(self, monkeypatch):
        monkeypatch.setattr(config, "FEE_RATE_OVERRIDE", 0.10)
        assert fees.fee_rate_for_category("crypto") == pytest.approx(0.10)
        assert fees.fee_rate_for_category("geopolitics") == pytest.approx(0.10)
        assert fees.fee_per_share(0.5, "crypto") == pytest.approx(0.10 * 0.5 * 0.5)

    def test_net_of_fee_edge(self, monkeypatch):
        monkeypatch.setattr(config, "FEE_RATE_OVERRIDE", 0.05)
        expect = 0.02 - fees.fee_per_share(0.5, "crypto")
        assert fees.net_of_fee_edge(0.02, 0.5, "crypto") == pytest.approx(expect)


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

    def test_phantom_arb_from_near_zero_legs_rejected(self):
        """The real blowup: 6 exact-score outcomes, YES asks ~0.001 each
        (sum 0.006). Old code saw a 166x edge and bought 50k+ sets marked
        at $1. Guards must reject it outright."""
        ms, books = self._mk_event(
            [0.001, 0.001, 0.001, 0.001, 0.001, 0.001],
            [0.99]*6, sizes=[60000]*6)
        for m in ms:
            m.question = f"Exact score {m.market_id}"
            m.event_title = "Neftçi PFK vs. FK Dynama-Minsk - Exact Score"
        assert arbitrage.scan(ms, books) == []

    def test_low_cost_yes_lock_rejected(self):
        # YES sum 0.30 (well under ARB_MIN_COST 0.50): implies missing
        # outcomes, not a real exhaustive set -> reject
        ms, books = self._mk_event([0.10, 0.10, 0.10], [0.9, 0.9, 0.9],
                                   sizes=[5000]*3)
        assert [o for o in arbitrage.scan(ms, books) if "YES" in o.key] == []

    def test_near_zero_leg_rejected_even_if_others_normal(self):
        # one 0.005 leg drags the sum down and is a phantom leg
        ms, books = self._mk_event([0.005, 0.48, 0.48], [0.9, 0.9, 0.9],
                                   sizes=[5000]*3)
        assert [o for o in arbitrage.scan(ms, books) if "YES" in o.key] == []

    def test_position_hard_capped_in_dollars(self):
        # legitimate lock but with huge fake depth: sizing must cap the
        # position at ARB_MAX_POSITION_USD, not buy the whole book
        ms, books = self._mk_event([0.30, 0.35, 0.30], [0.9, 0.9, 0.9],
                                   sizes=[500000]*3)
        opps = [o for o in arbitrage.scan(ms, books) if "YES" in o.key]
        assert opps
        cost = opps[0].total_cost()
        assert cost <= config.ARB_MAX_POSITION_USD + 1e-6

    # ---- event-completeness guard ----
    def test_dropped_sibling_skips_the_whole_event(self):
        """The real bug: a still-open negRisk event with one outcome that
        closed/delisted early. api.py's active-only filter would silently
        drop it, leaving `markets` undercounting the true partition. If
        the fetched count doesn't match the event's real total, the whole
        event must be skipped -- not traded on a partial, unverifiable set."""
        ms, books = self._mk_event([0.30, 0.35, 0.30], [0.9, 0.9, 0.9])
        for m in ms:
            m.event_total_markets = 4   # true event has 4 outcomes, only 3 fetched
        assert arbitrage.scan(ms, books) == []

    def test_complete_matching_count_still_trades(self):
        """Sanity check for the guard itself: when the fetched count DOES
        match the event's true total, nothing should be blocked."""
        ms, books = self._mk_event([0.30, 0.35, 0.30], [0.9, 0.9, 0.9])
        for m in ms:
            m.event_total_markets = 3   # matches len(ms) exactly
        opps = arbitrage.scan(ms, books)
        assert opps      # locks still found -- the guard didn't over-trigger

    def test_unknown_total_does_not_block_existing_behavior(self):
        """event_total_markets defaults to 0 ('unknown') for hand-built
        markets that never set it -- the guard must not treat 'unknown'
        as 'incomplete', or every existing test/fixture would break."""
        ms, books = self._mk_event([0.30, 0.35, 0.30], [0.9, 0.9, 0.9])
        assert all(m.event_total_markets == 0 for m in ms)
        opps = arbitrage.scan(ms, books)
        assert opps

    def test_api_parse_event_populates_true_total_before_filtering(self):
        """The other half of the fix: parse_event() must record the TRUE
        total from the raw event (4 markets), not just the count that
        survives the closed/inactive filter (3) -- otherwise ARB has no
        way to detect a dropped sibling at all."""
        client = PolymarketClient()
        raw_event = {
            "id": "EV9", "title": "Some negRisk event", "negRisk": True,
            "markets": [
                {"id": "M1", "question": "Q1", "clobTokenIds": '["M1-Y","M1-N"]',
                 "outcomePrices": '["0.3","0.7"]', "active": True, "closed": False},
                {"id": "M2", "question": "Q2", "clobTokenIds": '["M2-Y","M2-N"]',
                 "outcomePrices": '["0.3","0.7"]', "active": True, "closed": False},
                {"id": "M3", "question": "Q3", "clobTokenIds": '["M3-Y","M3-N"]',
                 "outcomePrices": '["0.3","0.7"]', "active": True, "closed": False},
                # the 4th outcome closed early -- must still count toward
                # the true total even though it's filtered out below
                {"id": "M4", "question": "Q4", "clobTokenIds": '["M4-Y","M4-N"]',
                 "outcomePrices": '["0.1","0.9"]', "active": False, "closed": True},
            ],
        }
        parsed = client.parse_event(raw_event)
        assert len(parsed) == 3                       # the closed one is dropped
        assert all(m.event_total_markets == 4 for m in parsed)  # but true total is 4

    def test_sports_exact_score_excluded(self):
        ms, books = self._mk_event([0.30, 0.35, 0.30], [0.9, 0.9, 0.9],
                                   sizes=[5000]*3)
        for m in ms:
            m.event_title = "Arsenal vs. Chelsea - Exact Score"
        assert arbitrage.scan(ms, books) == []

    def test_illiquid_event_excluded(self):
        ms, books = self._mk_event([0.30, 0.35, 0.30], [0.9, 0.9, 0.9],
                                   sizes=[5000]*3)
        for m in ms:
            m.liquidity = 10.0     # below ARB_MIN_LIQUIDITY
        assert arbitrage.scan(ms, books) == []

    def test_legit_arb_still_works(self):
        # a real, liquid, non-sports 3-outcome YES-lock summing 0.95 must
        # still be found (regression guard: fixes didn't kill the strategy)
        ms, books = self._mk_event([0.30, 0.35, 0.30], [0.72, 0.67, 0.72],
                                   sizes=[5000]*3)
        opps = [o for o in arbitrage.scan(ms, books) if "YES" in o.key]
        assert len(opps) == 1
        assert opps[0].edge == pytest.approx(0.05)

    def test_far_dated_lock_skipped_by_horizon_cap(self):
        # a clear 5c YES-lock, but resolving ~5 months out -> must be skipped
        ms, books = self._mk_event([0.30, 0.35, 0.30], [0.9, 0.9, 0.9])
        for m in ms:
            m.end_date = _future(150)
        assert arbitrage.scan(ms, books) == []
        # same lock inside the horizon -> detected
        for m in ms:
            m.end_date = _future(5)
        assert [o for o in arbitrage.scan(ms, books) if "YES" in o.key]

    def test_yes_lock_legs_carry_fee_and_reduce_edge(self, monkeypatch):
        # fixed rate so the expected fee is exact and easy to check
        monkeypatch.setattr(config, "FEE_RATE_OVERRIDE", 0.05)
        ms, books = self._mk_event([0.30, 0.35, 0.30], [0.72, 0.67, 0.72],
                                   sizes=[5000] * 3)
        opps = [o for o in arbitrage.scan(ms, books) if "YES" in o.key]
        assert len(opps) == 1
        o = opps[0]
        for leg, ask in zip(o.legs, [0.30, 0.35, 0.30]):
            assert leg.fee_per_share == pytest.approx(fees.fee_per_share(ask, ""))
            assert leg.fee_per_share > 0
        total_fee_per_set = sum(leg.fee_per_share for leg in o.legs)
        sets = o.legs[0].shares
        assert o.total_cost() == pytest.approx((sum([0.30, 0.35, 0.30]) + total_fee_per_set) * sets)
        # control run with fees off must show a strictly better edge and a
        # strictly lower total_cost for the same inputs
        monkeypatch.setattr(config, "FEE_RATE_OVERRIDE", 0.0)
        opps_no_fee = [o for o in arbitrage.scan(ms, books) if "YES" in o.key]
        assert opps_no_fee[0].edge > o.edge
        assert opps_no_fee[0].total_cost() < o.total_cost()

    def test_no_lock_legs_carry_fee_and_reduce_edge(self, monkeypatch):
        monkeypatch.setattr(config, "FEE_RATE_OVERRIDE", 0.05)
        ms, books = self._mk_event([0.40, 0.35, 0.30], [0.62, 0.64, 0.64])
        opps = [o for o in arbitrage.scan(ms, books) if "NO" in o.key]
        assert len(opps) == 1
        o = opps[0]
        for leg, ask in zip(o.legs, [0.62, 0.64, 0.64]):
            assert leg.fee_per_share == pytest.approx(fees.fee_per_share(ask, ""))
            assert leg.fee_per_share > 0
        monkeypatch.setattr(config, "FEE_RATE_OVERRIDE", 0.0)
        opps_no_fee = [o for o in arbitrage.scan(ms, books) if "NO" in o.key]
        assert opps_no_fee[0].edge > o.edge


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

    def test_far_dated_leg_skipped_by_horizon_cap(self):
        # profitable IMPLIES lock, but leg B resolves ~5 months out ->
        # capital tied until the LAST leg resolves, so the pair is skipped
        a = market("A", 0.35)
        b = market("B", 0.55, end=_future(150))
        books = {b.yes_token: book(b.yes_token, 0.55),
                 a.no_token: book(a.no_token, 0.40)}
        rels = [{"type": "IMPLIES", "a_market_id": "A", "b_market_id": "B"}]
        assert correlated.scan([a, b], books, rels) == []
        # same lock with both legs near-dated -> detected
        b.end_date = _future(5)
        assert len(correlated.scan([a, b], books, rels)) == 1

    def test_implies_lock_legs_carry_fee_and_reduce_edge(self, monkeypatch):
        monkeypatch.setattr(config, "FEE_RATE_OVERRIDE", 0.05)
        a, b = market("A", 0.35), market("B", 0.55)
        books = {b.yes_token: book(b.yes_token, 0.55),
                 a.no_token: book(a.no_token, 0.40)}
        rels = [{"type": "IMPLIES", "a_market_id": "A", "b_market_id": "B"}]
        opps = correlated.scan([a, b], books, rels)
        assert len(opps) == 1
        o = opps[0]
        leg_b = next(l for l in o.legs if l.side == "YES")
        leg_a = next(l for l in o.legs if l.side == "NO")
        assert leg_b.fee_per_share == pytest.approx(fees.fee_per_share(0.55, ""))
        assert leg_a.fee_per_share == pytest.approx(fees.fee_per_share(0.40, ""))
        assert leg_b.fee_per_share > 0 and leg_a.fee_per_share > 0
        sets = leg_a.shares
        total_fee_per_set = leg_a.fee_per_share + leg_b.fee_per_share
        assert o.total_cost() == pytest.approx((0.40 + 0.55 + total_fee_per_set) * sets)

        monkeypatch.setattr(config, "FEE_RATE_OVERRIDE", 0.0)
        opps_no_fee = correlated.scan([a, b], books, rels)
        assert opps_no_fee[0].edge > o.edge
        assert opps_no_fee[0].total_cost() < o.total_cost()


# ------------------------------------------------------------------ LONGSHOT
class TestLongshot:
    def test_fade_detected_with_correct_ev(self):
        m = market("L1", 0.04, end=_future(5))
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

    def test_sorted_soonest_resolving_first(self):
        soon = market("LS", 0.04, event="E1", end=_future(2))
        late = market("LL", 0.03, event="E2", end=_future(14))  # better edge, later
        books = {m.no_token: book(m.no_token, 0.955) for m in (soon, late)}
        out = longshot.scan([late, soon], books)
        assert [o.key for o in out] == ["LS-LS", "LS-LL"]

    def test_negative_ev_skipped(self):
        # NO ask so high there's no edge even with haircut
        m = market("L1", 0.05)
        books = {m.no_token: book(m.no_token, 0.995)}
        # EV = (1 - 0.03) - 0.995 = -0.025 < 0
        assert longshot.scan([m], books) == []

    def test_leg_carries_fee_and_ev_uses_price_plus_fee_denominator(self, monkeypatch):
        monkeypatch.setattr(config, "FEE_RATE_OVERRIDE", 0.05)
        m = market("L1", 0.04, end=_future(5))
        a = 0.965
        books = {m.no_token: book(m.no_token, a)}
        opps = longshot.scan([m], books)
        assert len(opps) == 1
        o = opps[0]
        leg = o.legs[0]
        expected_fee = fees.fee_per_share(a, "")
        assert leg.fee_per_share == pytest.approx(expected_fee)
        assert leg.fee_per_share > 0
        true_p_yes = 0.04 * config.LS_BIAS_HAIRCUT
        p_win = 1 - true_p_yes
        expect_edge = (p_win - a - expected_fee) / (a + expected_fee)
        assert o.edge == pytest.approx(expect_edge)
        # shares are filled in later by risk.py's sizing, but the fee is
        # already baked into Leg.cost, so total_cost() is fee-aware for
        # ANY size risk.py ends up choosing -- spot check at a nonzero size
        leg.shares = 20.0
        assert o.total_cost() == pytest.approx((a + expected_fee) * 20.0)

        # control run with fees off: strictly better edge for the same inputs
        monkeypatch.setattr(config, "FEE_RATE_OVERRIDE", 0.0)
        opps_no_fee = longshot.scan([m], books)
        assert opps_no_fee[0].edge > o.edge


# ------------------------------------------------------------------ CONVERGE
class TestConvergence:
    def test_pick_and_yield(self):
        m = market("C1", 0.96, end=_future(4), liq=99999)
        books = {m.yes_token: book(m.yes_token, 0.96)}
        o = convergence.scan([m], books)[0]
        assert o.edge == pytest.approx((1 - 0.96) / 0.96)
        assert not o.guaranteed
        # the uplift assumption must be applied: true P assumed halfway
        # between market price and 1.0 (0.96 -> 0.98 at default 0.5)
        assert o.est_p_win == pytest.approx(
            0.96 + (1 - 0.96) * config.CV_TRUE_P_UPLIFT)
        assert o.est_p_win > 0.96          # strictly above the price paid

    def test_converge_opportunity_actually_gets_funded(self):
        """Regression for the 'opened: 0' bug — a CONVERGE candidate whose
        assumed win probability equals its entry price has zero Kelly edge
        and silently never funds. With the uplift, it must fund."""
        m = market("C1", 0.96, end=_future(4), liq=99999)
        books = {m.yes_token: book(m.yes_token, 0.96)}
        o = convergence.scan([m], books)[0]
        sized = size_opportunities([o], bankroll=1000, cash=1000,
                                   strategy_exposure={}, total_exposure=0)
        assert len(sized) == 1
        assert sized[0].total_cost() >= config.MIN_TICKET

    def test_no_uplift_means_no_funding_old_bug(self):
        """Documents the failure mode the uplift exists to fix: with
        est_p_win == entry price, Kelly is 0 and nothing funds."""
        from polyedge.models import Leg, Opportunity
        o = Opportunity("CONVERGE", "CV-FLAT", "flat", 0.04, False,
                        est_p_win=0.96,       # equal to entry -> zero edge
                        resolve_by="2026-07-18T00:00:00Z",
                        legs=[Leg("t", "m", "YES q", "YES", 0.96, 0.0)])
        sized = size_opportunities([o], bankroll=1000, cash=1000,
                                   strategy_exposure={}, total_exposure=0)
        assert sized == []

    def test_low_annualized_yield_rejected(self):
        # far-dated but still inside CV_MAX_DAYS window won't work as time
        # passes, so force the reject via a very high APY floor instead —
        # and use _future() rather than a fixed calendar date so this stays
        # date-robust regardless of when the suite runs (a hardcoded date
        # eventually arrives, which is exactly what made this test flaky)
        m = market("C1", 0.984, end=_future(365), liq=99999)
        books = {m.yes_token: book(m.yes_token, 0.984)}
        old_apy = config.CV_MIN_ANNUAL_YIELD
        old_days = config.CV_MAX_DAYS
        config.CV_MIN_ANNUAL_YIELD = 100.0    # demand an absurd APY
        config.CV_MAX_DAYS = 3650             # keep it inside the horizon
        try:
            assert convergence.scan([m], books) == []
        finally:
            config.CV_MIN_ANNUAL_YIELD = old_apy
            config.CV_MAX_DAYS = old_days

    def test_sorted_by_annualized_yield_soonest_wins(self):
        # same price/edge, different horizons -> sooner one must rank first
        soon = market("CS", 0.96, end=_future(2), liq=99999)   # ~2d
        late = market("CL", 0.96, end=_future(13), liq=99999)   # ~13d
        books = {m.yes_token: book(m.yes_token, 0.96) for m in (soon, late)}
        out = convergence.scan([late, soon], books)
        assert [o.key for o in out] == ["CV-CS", "CV-CL"]

    # ---- sports match exclusion ----
    def test_sports_titles_detected(self):
        """Real titles from the live trade log (incl. the -$25.53 loss)."""
        sports_titles = [
            "CF Montréal vs. Toronto FC: O/U 0.5",
            "St. Louis City SC vs. Sporting Kansas City: O/U 0.5",
            "Seattle Sounders FC vs. Portland Timbers: O/U 0.5",
            "Spread: Ferencvárosi TC (-1.5)",
            "ÍF Vestri vs. Qarabağ Ağdam FK: O/U 0.5",
            "Pyunik FA vs. Marsaxlokk FC: O/U 0.5",
            "Lakers moneyline",
            "First half result: draw",
            "Both teams to score",
        ]
        for t in sports_titles:
            m = market("S1", 0.96, question=t)
            assert convergence.is_sports_match(m), f"should detect: {t}"

    def test_non_sports_titles_not_detected(self):
        """Legitimate CONVERGE material must NOT be excluded — including
        sports-ADJACENT event markets that aren't match outcomes."""
        ok_titles = [
            "Israeli parliament dissolved by July 17?",
            "Will the price of Bitcoin be above $62,000 on July 17?",
            "Will Trump attend 1 World Cup match?",
            "Fed decision in September?",
            "Will the bill pass committee this week?",
        ]
        for t in ok_titles:
            m = market("N1", 0.96, question=t)
            assert not convergence.is_sports_match(m), f"false positive: {t}"

    def test_category_tag_detection(self):
        m = market("S2", 0.96, question="Team A to win the title?")
        m.category = "sports epl"
        assert convergence.is_sports_match(m)

    def test_scan_excludes_sports_but_keeps_others(self):
        sport = market("SP", 0.96, end=_future(3), liq=99999,
                       question="CF Montréal vs. Toronto FC: O/U 0.5")
        news = market("NW", 0.96, end=_future(3), liq=99999,
                      question="Israeli parliament dissolved by July 17?")
        books = {m.yes_token: book(m.yes_token, 0.96) for m in (sport, news)}
        out = convergence.scan([sport, news], books)
        assert [o.key for o in out] == ["CV-NW"]

    def test_exclusion_can_be_disabled(self):
        sport = market("SP", 0.96, end=_future(3), liq=99999,
                       question="CF Montréal vs. Toronto FC: O/U 0.5")
        books = {sport.yes_token: book(sport.yes_token, 0.96)}
        old = config.CV_EXCLUDE_SPORTS
        config.CV_EXCLUDE_SPORTS = False
        try:
            assert len(convergence.scan([sport], books)) == 1
        finally:
            config.CV_EXCLUDE_SPORTS = old

    # ---- earnings-beat exclusion ----
    def test_earnings_titles_detected(self):
        """Real titles from the trade log — both lost (2 for 2)."""
        earnings_titles = [
            "Will Qualcomm (QCOM) beat quarterly earnings?",
            "Will Meta (META) beat quarterly earnings?",
            "Will Apple beat Q3 earnings?",
            "Will Amazon beat EPS estimates?",
            "Will Tesla raise guidance?",
        ]
        for t in earnings_titles:
            m = market("E1", 0.96, question=t)
            assert convergence.is_earnings_market(m), f"should detect: {t}"

    def test_non_earnings_titles_not_detected(self):
        ok_titles = [
            "Will Apple report Q3 results by August 1?",
            "Will the Fed decision come before September?",
        ]
        for t in ok_titles:
            m = market("E2", 0.96, question=t)
            assert not convergence.is_earnings_market(m), f"false positive: {t}"

    def test_scan_excludes_earnings(self):
        earn = market("EA", 0.96, end=_future(3), liq=99999,
                      question="Will Meta (META) beat quarterly earnings?")
        news = market("NW2", 0.96, end=_future(3), liq=99999,
                      question="Israeli parliament dissolved by July 17?")
        books = {m.yes_token: book(m.yes_token, 0.96) for m in (earn, news)}
        out = convergence.scan([earn, news], books)
        assert [o.key for o in out] == ["CV-NW2"]

    # ---- numeric-bracket exclusion ----
    def test_bracket_titles_detected(self):
        """Real titles from the trade log and live production data."""
        bracket_titles = [
            "Will Elon Musk post 40-64 tweets from July 27 to July 29?",
            "Will there be between 3 and 5 rate cuts this year?",
            "Will the team score 2-4 goals this match?",
            # confirmed slipping through in live dry-run before this fix:
            "Will the price of Bitcoin be between $64,000 and $66,000 on July 30?",
            "Will oil trade $40-45 this week?",
            "Will the Fed set rates at 4.25%-4.50%?",
        ]
        for t in bracket_titles:
            m = market("B1", 0.96, question=t)
            assert convergence.is_bracket_market(m), f"should detect: {t}"

    def test_non_bracket_titles_not_detected(self):
        """Single-sided thresholds and plain date ranges must NOT
        false-positive -- these are genuine CONVERGE material, not a
        narrow band on a volatile quantity."""
        ok_titles = [
            "Will the Fed decision come before 2026-2027?",
            "Will Bitcoin be above $62,000 on July 17?",
            "Will the price of ETH stay below $3,000?",
        ]
        for t in ok_titles:
            m = market("B2", 0.96, question=t)
            assert not convergence.is_bracket_market(m), f"false positive: {t}"

    def test_scan_excludes_brackets(self):
        bracket = market("BR", 0.96, end=_future(3), liq=99999,
                         question="Will Elon Musk post 40-64 tweets?")
        news = market("NW3", 0.96, end=_future(3), liq=99999,
                      question="Israeli parliament dissolved by July 17?")
        books = {m.yes_token: book(m.yes_token, 0.96) for m in (bracket, news)}
        out = convergence.scan([bracket, news], books)
        assert [o.key for o in out] == ["CV-NW3"]

    # ---- election/primary exclusion ----
    def test_election_titles_detected(self):
        election_titles = [
            "Michigan Democratic Senate Primary Winner",
            "Will Kristen McDonald Rivet win the Democratic primary?",
            "California Governor Election Winner",
            "Presidential Election Winner 2028",
        ]
        for t in election_titles:
            m = market("EL1", 0.96, question=t)
            assert convergence.is_election_market(m), f"should detect: {t}"

    def test_non_election_titles_not_detected(self):
        """Legitimate policy/legislative CONVERGE material must NOT be
        excluded just for being politics-adjacent."""
        ok_titles = [
            "Will the bill pass committee this week?",
            "Israeli parliament dissolved by July 17?",
            "Will the Fed decision come before September?",
        ]
        for t in ok_titles:
            m = market("EL2", 0.96, question=t)
            assert not convergence.is_election_market(m), f"false positive: {t}"

    def test_scan_excludes_elections(self):
        elec = market("EX", 0.999, end=_future(3), liq=99999,
                      question="Michigan Democratic Senate Primary Winner")
        news = market("NW4", 0.96, end=_future(3), liq=99999,
                      question="Israeli parliament dissolved by July 17?")
        books = {m.yes_token: book(m.yes_token, 0.96 if m.market_id == "NW4"
                                   else 0.999) for m in (elec, news)}
        out = convergence.scan([elec, news], books)
        assert [o.key for o in out] == ["CV-NW4"]

    # ---- ranking/superlative exclusion ----
    def test_ranking_titles_detected(self):
        """Real titles from paper data -- both went from ~96c to near zero
        when a market-cap reshuffle flipped the ranking."""
        ranking_titles = [
            "Will Apple be the largest company in the world by market cap",
            "Will NVIDIA be the second-largest company in the world by ma",
            "Will X be the most valuable startup by end of year?",
            "Will 'Dune 3' be the highest-grossing film of 2026?",
            "Will the song rank #1 on Billboard this week?",
        ]
        for t in ranking_titles:
            m = market("R1", 0.96, question=t)
            assert convergence.is_ranking_market(m), f"should detect: {t}"

    def test_ranking_does_not_catch_threshold_winners(self):
        """CRITICAL false-positive guard: these are real WINNING titles
        from the paper data -- ordinary threshold/deadline markets that
        are the bread and butter of CONVERGE's profitable inventory. The
        ranking filter must not touch any of them."""
        winning_titles = [
            "Will the price of Bitcoin be above $62,000 on July 31?",
            "Will Meta (META) close above $540 on July 31?",
            "SPY (SPY) Up or Down on July 31?",
            "Israel x Iran ceasefire continues through July 31?",
            "No change in Reserve Bank of Australia's interest rates",
            "Will the price of Ethereum be above $1,800 on July 31?",
        ]
        for t in winning_titles:
            m = market("R2", 0.96, question=t)
            assert not convergence.is_ranking_market(m), f"false positive: {t}"

    def test_scan_excludes_rankings(self):
        rank = market("RK", 0.96, end=_future(3), liq=99999,
                      question="Will Apple be the largest company in the "
                               "world by market cap on July 31?")
        news = market("NW5", 0.96, end=_future(3), liq=99999,
                      question="Israeli parliament dissolved by July 17?")
        books = {m.yes_token: book(m.yes_token, 0.96) for m in (rank, news)}
        out = convergence.scan([rank, news], books)
        assert [o.key for o in out] == ["CV-NW5"]

    def test_actual_won_market_not_excluded_by_any_new_filter(self):
        """Regression guard using a real market from production data that
        WON (a genuinely legitimate CONVERGE trade) -- none of the three
        new exclusions should touch it."""
        m = market("GEM", 0.96, end=_future(3), liq=99999,
                   question="Will there be no next Gemini Pro model "
                            "release by July 31?")
        assert not convergence.is_earnings_market(m)
        assert not convergence.is_bracket_market(m)
        assert not convergence.is_election_market(m)
        books = {m.yes_token: book(m.yes_token, 0.96)}
        assert len(convergence.scan([m], books)) == 1

    def test_leg_carries_fee_and_yield_is_net(self, monkeypatch):
        monkeypatch.setattr(config, "FEE_RATE_OVERRIDE", 0.05)
        m = market("C1", 0.96, end=_future(4), liq=99999)
        a = 0.96
        books = {m.yes_token: book(m.yes_token, a)}
        opps = convergence.scan([m], books)
        assert len(opps) == 1
        o = opps[0]
        leg = o.legs[0]
        expected_fee = fees.fee_per_share(a, "")
        assert leg.fee_per_share == pytest.approx(expected_fee)
        assert leg.fee_per_share > 0
        expect_yield = (1.0 - a - expected_fee) / a
        assert o.edge == pytest.approx(expect_yield)
        assert "fee" in o.note

        # control run with fees off must show a strictly better (higher)
        # net yield/edge for the exact same market and book
        monkeypatch.setattr(config, "FEE_RATE_OVERRIDE", 0.0)
        opps_no_fee = convergence.scan([m], books)
        assert len(opps_no_fee) == 1
        assert opps_no_fee[0].edge > o.edge
        assert opps_no_fee[0].legs[0].fee_per_share == 0.0
        assert opps_no_fee[0].note != o.note


# ------------------------------------------------------------------ past-date rejection
class TestPastDateRejection:
    """Regression for the screenshot bug: markets resolving in the PAST
    (June 2026, before 'now') were clamped to days=0 and treated as the
    MOST near-term opportunities, so they got funded first."""

    def test_days_to_resolution_negative_for_past(self):
        from polyedge.models import days_to_resolution
        assert days_to_resolution("2020-01-01T00:00:00Z") < 0
        assert days_to_resolution("2099-01-01T00:00:00Z") > 0

    def test_convergence_rejects_past_market(self):
        past = market("P1", 0.965, end="2020-06-01T00:00:00Z", liq=99999)
        books = {past.yes_token: book(past.yes_token, 0.965)}
        assert convergence.scan([past], books) == []

    def test_longshot_rejects_past_market(self):
        past = market("P2", 0.04, end="2020-06-17T00:00:00Z")
        books = {past.no_token: book(past.no_token, 0.955)}
        assert longshot.scan([past], books) == []

    def test_arb_rejects_past_event(self):
        ms = [market(f"P{i}", 0.30, neg_risk=True, event="PASTEV",
                     end="2020-06-01T00:00:00Z") for i in range(3)]
        books = {}
        for m in ms:
            books[m.yes_token] = book(m.yes_token, 0.30, 5000)
            books[m.no_token] = book(m.no_token, 0.72, 5000)
        assert arbitrage.scan(ms, books) == []

    def test_future_markets_still_work(self):
        import datetime as _d
        soon = (_d.datetime.now(_d.timezone.utc) + _d.timedelta(days=3)
                ).strftime("%Y-%m-%dT%H:%M:%SZ")
        fut = market("F2", 0.965, end=soon, liq=99999)
        books = {fut.yes_token: book(fut.yes_token, 0.965)}
        assert len(convergence.scan([fut], books)) == 1


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

    def test_near_term_resolution_funded_first(self):
        """With cash for only one trade, the sooner-resolving opportunity wins
        even when the later one has a (slightly) better edge."""
        def cv(key, resolve_by, edge):
            return Opportunity(strategy="CONVERGE", key=key, title=key,
                               edge=edge, guaranteed=False, est_p_win=0.99,
                               resolve_by=resolve_by,
                               legs=[Leg(f"t-{key}", f"m-{key}", "YES q", "YES",
                                         0.96, 0.0)])
        soon = cv("CV-SOON", "2026-07-16T00:00:00Z", edge=0.030)   # 2 days out
        late = cv("CV-LATE", "2026-07-27T00:00:00Z", edge=0.035)   # 13 days out, better edge
        # cash allows exactly ONE ticket (= MIN_TICKET), so priority decides
        sized = size_opportunities([late, soon], bankroll=200, cash=5,
                                   strategy_exposure={}, total_exposure=0)
        assert [o.key for o in sized] == ["CV-SOON"]

    def test_guaranteed_still_beats_near_term_speculative(self):
        legs = [Leg("a", "ma", "YES a", "YES", 0.45, 100),
                Leg("b", "mb", "YES b", "YES", 0.50, 100)]
        lock = Opportunity(strategy="ARB", key="ARB-L", title="lock",
                           edge=0.05, guaranteed=True, legs=legs,
                           guaranteed_payout=1.0,
                           resolve_by="2026-12-31T00:00:00Z")   # far away
        spec = Opportunity(strategy="CONVERGE", key="CV-S", title="soon",
                           edge=0.04, guaranteed=False, est_p_win=0.96,
                           resolve_by="2026-07-15T00:00:00Z",   # tomorrow
                           legs=[Leg("c", "mc", "YES q", "YES", 0.96, 0.0)])
        sized = size_opportunities([spec, lock], bankroll=200, cash=10,
                                   strategy_exposure={}, total_exposure=0)
        assert sized and sized[0].key == "ARB-L"   # lock funded first regardless


# ------------------------------------------------------------------ resolution / straggler recovery
class TestResolutionDetection:
    def test_parse_resolution_yes(self):
        raw = {"closed": True, "umaResolutionStatus": "resolved",
              "outcomePrices": ["1", "0"]}
        assert PolymarketClient.parse_resolution(raw) == "YES"

    def test_parse_resolution_no(self):
        raw = {"closed": True, "umaResolutionStatus": "resolved",
              "outcomePrices": ["0", "1"]}
        assert PolymarketClient.parse_resolution(raw) == "NO"

    def test_parse_resolution_string_encoded_prices(self):
        raw = {"closed": True, "umaResolutionStatus": "settled",
              "outcomePrices": '["1", "0"]'}
        assert PolymarketClient.parse_resolution(raw) == "YES"

    def test_parse_resolution_not_closed_is_none(self):
        raw = {"closed": False, "umaResolutionStatus": "resolved",
              "outcomePrices": ["1", "0"]}
        assert PolymarketClient.parse_resolution(raw) is None

    def test_parse_resolution_pending_status_is_none(self):
        # closed but still in the UMA dispute window — not settled yet
        raw = {"closed": True, "umaResolutionStatus": "proposed",
              "outcomePrices": ["1", "0"]}
        assert PolymarketClient.parse_resolution(raw) is None

    def test_parse_resolution_malformed_is_safe(self):
        assert PolymarketClient.parse_resolution({}) is None
        assert PolymarketClient.parse_resolution(
            {"closed": True, "umaResolutionStatus": "resolved",
             "outcomePrices": "not json"}) is None

    def test_fetch_resolutions_only_includes_actually_resolved(self, monkeypatch):
        client = PolymarketClient()
        fake_markets = {
            "M1": {"closed": True, "umaResolutionStatus": "resolved", "outcomePrices": ["1", "0"]},
            "M2": {"closed": False},                      # still open
            "M3": None,                                    # fetch failed
        }
        client.fetch_market = lambda mid: fake_markets.get(mid)
        out = client.fetch_resolutions(["M1", "M2", "M3"])
        assert out == {"M1": "YES"}

    def test_straggler_recovery_end_to_end(self, tmp_path):
        """The core bug: a market that closed drops out of the active-events
        feed entirely, so the settlement loop that only scans `events`
        would NEVER see it. Direct by-id lookup for open positions must
        catch it regardless.
        """
        from polyedge.paper import PaperEngine

        engine = PaperEngine(state_dir=str(tmp_path))
        opp = Opportunity(strategy="LONGSHOT", key="LS-STRAY", title="fade",
                          edge=0.02, guaranteed=False, est_p_win=0.97,
                          legs=[Leg("tok", "M-CLOSED", "NO q", "NO", 0.95, 20.0)])
        engine.open_position(opp)

        # simulate: this scan's active-events feed is EMPTY for M-CLOSED
        # (exactly what happens once Polymarket marks it closed)
        events = []
        outcomes = {}
        for ev in events:
            for raw in ev.get("markets", []) or []:
                r = PolymarketClient.parse_resolution(raw)
                if r:
                    outcomes[str(raw.get("id"))] = r
        assert outcomes == {}          # confirms the feed alone misses it

        # the straggler check must still find it via direct lookup
        client = PolymarketClient()
        client.fetch_market = lambda mid: (
            {"closed": True, "umaResolutionStatus": "resolved",
             "outcomePrices": ["0", "1"]} if mid == "M-CLOSED" else None)
        open_market_ids = {leg["market_id"] for pos in engine.state["positions"]
                           for leg in pos["legs"]} - set(outcomes)
        assert open_market_ids == {"M-CLOSED"}
        outcomes.update(client.fetch_resolutions(open_market_ids))
        assert outcomes == {"M-CLOSED": "NO"}

        settled = engine.resolve(outcomes)
        assert len(settled) == 1 and settled[0]["payout"] == pytest.approx(20.0)
        assert engine.state["positions"] == []
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

    def test_mark_to_market_annotates_positions(self, tmp_path):
        e = self._engine(tmp_path)
        opp = Opportunity(strategy="CONVERGE", key="CV-M", title="converge",
                          edge=0.04, guaranteed=False, est_p_win=0.96,
                          legs=[Leg("cv1", "m1", "YES q", "YES", 0.96, 50.0)])
        e.open_position(opp)                              # cost 48
        e.mark_to_market({"cv1": 0.985})
        pos = e.state["positions"][0]
        assert pos["current_prices"]["cv1"] == pytest.approx(0.985)
        assert pos["current_value"] == pytest.approx(50 * 0.985)
        assert pos["unrealized_pl"] == pytest.approx(50 * (0.985 - 0.96))
        assert pos["unrealized_pl_pct"] == pytest.approx(
            (50 * (0.985 - 0.96)) / 48 * 100, abs=0.01)

    def test_guaranteed_position_unrealized_pl_floored_at_lock(self, tmp_path):
        e = self._engine(tmp_path)
        e.open_position(self._arb_opp(sets=100))          # cost 95, lock pays 100
        e.mark_to_market({"a": 0.10, "b": 0.10})           # quotes collapse — irrelevant to the lock
        pos = e.state["positions"][0]
        assert pos["current_value"] == pytest.approx(100.0)
        assert pos["unrealized_pl"] == pytest.approx(5.0)  # the locked edge, unaffected by noise

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


# ------------------------------------------------------------------ take-profit
class TestTakeProfit:
    def _engine(self, tmp_path):
        return PaperEngine(state_dir=str(tmp_path))

    def _cv_opp(self, key="CV-T", entry=0.96, shares=50.0):
        return Opportunity(strategy="CONVERGE", key=key, title="converge",
                           edge=(1 - entry) / entry, guaranteed=False,
                           est_p_win=entry,
                           legs=[Leg("cv-tok", "cv-m", "YES q", "YES",
                                     entry, shares)])

    def test_exit_when_capture_threshold_reached(self, tmp_path):
        e = self._engine(tmp_path)
        start = e.cash
        e.open_position(self._cv_opp())               # entry 0.96, cost 48
        # upside = 0.04; 25% capture needs bid >= 0.96 + 0.010 = 0.970
        books = {"cv-tok": book("cv-tok", 0.995, bid=0.99)}
        closed = e.scan_take_profits(books)
        assert len(closed) == 1
        c = closed[0]
        assert c["close_reason"] == "take_profit"
        assert c["payout"] == pytest.approx(50 * 0.99)   # sold at BID, not ask/mark
        assert c["pl"] == pytest.approx(50 * (0.99 - 0.96))
        assert e.cash == pytest.approx(start - 48.0 + 49.5)
        assert e.state["positions"] == []

    def test_no_exit_below_threshold(self, tmp_path):
        e = self._engine(tmp_path)
        e.open_position(self._cv_opp())               # entry 0.96
        # bid 0.966 -> captured (0.006/0.04) = 15% < 25% threshold
        books = {"cv-tok": book("cv-tok", 0.995, bid=0.966)}
        assert e.scan_take_profits(books) == []
        assert len(e.state["positions"]) == 1

    def test_missing_book_or_bid_is_safe(self, tmp_path):
        e = self._engine(tmp_path)
        e.open_position(self._cv_opp())
        assert e.scan_take_profits({}) == []           # no book at all
        empty = OrderBook("cv-tok", asks=[BookLevel(0.99, 10)], bids=[])
        assert e.scan_take_profits({"cv-tok": empty}) == []   # no bid side
        assert len(e.state["positions"]) == 1

    def test_locks_never_exited_early(self, tmp_path):
        e = self._engine(tmp_path)
        legs = [Leg("a", "ma", "YES a", "YES", 0.30, 50),
                Leg("b", "mb", "YES b", "YES", 0.65, 50)]
        arb = Opportunity(strategy="ARB", key="ARB-T", title="lock",
                          edge=0.05, guaranteed=True, legs=legs,
                          guaranteed_payout=1.0)
        e.open_position(arb)
        # even absurdly favorable bids must not trigger an early unwind
        books = {"a": book("a", 0.999, bid=0.99), "b": book("b", 0.999, bid=0.99)}
        assert e.scan_take_profits(books) == []
        assert len(e.state["positions"]) == 1

    def test_strategy_filter_respected(self, tmp_path):
        e = self._engine(tmp_path)
        ls = Opportunity(strategy="LONGSHOT", key="LS-T", title="fade",
                         edge=0.02, guaranteed=False, est_p_win=0.97,
                         legs=[Leg("ls-tok", "ls-m", "NO q", "NO", 0.95, 20)])
        e.open_position(ls)
        # LONGSHOT not in TAKE_PROFIT_STRATEGIES by default -> untouched
        books = {"ls-tok": book("ls-tok", 0.999, bid=0.995)}
        assert e.scan_take_profits(books) == []
        # but if the user opts LONGSHOT in via config, it works
        old = config.TAKE_PROFIT_STRATEGIES
        config.TAKE_PROFIT_STRATEGIES = {"CONVERGE", "LONGSHOT"}
        try:
            closed = e.scan_take_profits(books)
            assert len(closed) == 1 and closed[0]["pl"] > 0
        finally:
            config.TAKE_PROFIT_STRATEGIES = old

    def test_equity_invariant_through_take_profit(self, tmp_path):
        e = self._engine(tmp_path)
        e.open_position(self._cv_opp())
        books = {"cv-tok": book("cv-tok", 0.995, bid=0.99)}
        e.scan_take_profits(books)
        pt = e.mark_to_market({})
        assert pt["equity"] == pytest.approx(pt["cash"] + pt["open_value"])
        s = e.stats()
        assert s["closed_trades"] == 1 and s["win_rate_pct"] == 100.0


# ------------------------------------------------------------------ void / cancel
class TestVoidPosition:
    def _engine(self, tmp_path):
        return PaperEngine(state_dir=str(tmp_path))

    def test_void_refunds_exact_cost_no_pl(self, tmp_path):
        e = self._engine(tmp_path)
        start = e.cash
        legs = [Leg("a", "ma", "YES a", "YES", 0.30, 100),
                Leg("b", "mb", "YES b", "YES", 0.65, 100)]
        opp = Opportunity(strategy="ARB", key="ARB-FAR", title="far lock",
                          edge=0.05, guaranteed=True, legs=legs,
                          guaranteed_payout=1.0, resolve_by="2027-12-31T00:00:00Z")
        e.open_position(opp)                       # cost 95
        assert e.cash == pytest.approx(start - 95.0)
        v = e.void_position("ARB-FAR")
        assert v is not None
        assert v["pl"] == 0.0
        assert v["payout"] == pytest.approx(95.0)
        assert e.cash == pytest.approx(start)       # fully restored
        assert e.state["positions"] == []
        assert e.state["closed"][0]["close_reason"] == "voided_manual"

    def test_void_unknown_key_is_safe(self, tmp_path):
        e = self._engine(tmp_path)
        assert e.void_position("NOPE") is None

    def test_void_does_not_touch_other_positions(self, tmp_path):
        e = self._engine(tmp_path)
        legs1 = [Leg("a", "ma", "YES a", "YES", 0.30, 50),
                Leg("b", "mb", "YES b", "YES", 0.65, 50)]
        legs2 = [Leg("c", "mc", "YES c", "YES", 0.30, 50),
                Leg("d", "md", "YES d", "YES", 0.65, 50)]
        e.open_position(Opportunity("ARB", "ARB-1", "t1", 0.05, True, legs1,
                                    guaranteed_payout=1.0))
        e.open_position(Opportunity("ARB", "ARB-2", "t2", 0.05, True, legs2,
                                    guaranteed_payout=1.0))
        e.void_position("ARB-1")
        assert len(e.state["positions"]) == 1
        assert e.state["positions"][0]["key"] == "ARB-2"

    def test_cancel_script_finds_only_stale_locks(self, tmp_path, monkeypatch):
        import cancel_stale_locks as csl
        e = PaperEngine(state_dir=str(tmp_path))
        far_legs = [Leg("a", "ma", "YES a", "YES", 0.30, 100),
                   Leg("b", "mb", "YES b", "YES", 0.65, 100)]
        near_legs = [Leg("c", "mc", "YES c", "YES", 0.30, 100),
                    Leg("d", "md", "YES d", "YES", 0.65, 100)]
        e.open_position(Opportunity("ARB", "ARB-FAR", "far", 0.05, True, far_legs,
                                    guaranteed_payout=1.0, resolve_by="2027-12-31T00:00:00Z"))
        e.open_position(Opportunity("ARB", "ARB-NEAR", "near", 0.05, True, near_legs,
                                    guaranteed_payout=1.0, resolve_by="2026-07-20T00:00:00Z"))
        cv_opp = Opportunity("CONVERGE", "CV-1", "cv", 0.04, False, est_p_win=0.96,
                             legs=[Leg("e", "me", "YES e", "YES", 0.96, 10)],
                             resolve_by="2027-12-31T00:00:00Z")  # far but not a lock strategy
        e.open_position(cv_opp)
        stale = csl.find_stale(e)
        assert [p["key"] for p, _ in stale] == ["ARB-FAR"]   # near lock + far CONVERGE untouched


# ------------------------------------------------------------------ live engine
class TestLiveEngine:
    def _opp(self, key="CV-L", strategy="CONVERGE", legs=None):
        legs = legs or [Leg("tok", "m1", "YES q", "YES", 0.96, 10.0)]
        return Opportunity(strategy, key, key, 0.04, strategy == "ARB",
                           est_p_win=0.98, legs=legs,
                           guaranteed_payout=1.0 if strategy == "ARB" else None,
                           resolve_by="2026-07-21T00:00:00Z")

    def _engine(self, tmp_path, monkeypatch, armed=True, live=True, dry=False,
                fill=True):
        from polyedge import live as lv
        monkeypatch.setenv("POLYEDGE_LIVE", "1" if live else "0")
        monkeypatch.setenv("POLYEDGE_DRY_RUN", "1" if dry else "0")
        monkeypatch.chdir(tmp_path)          # ARMED/HALTED files live in cwd
        if armed:
            open(lv.ARMED_FILE, "w").write("armed")
        e = lv.LiveEngine(state_dir=str(tmp_path))
        e._orders = []
        def fake_place(token_id, price, shares, side):
            e._orders.append((side, token_id, round(shares, 2), round(price, 3)))
            return fill and not lv.dry_run()
        e._place_order = fake_place
        e._check_pusd_balance = lambda: True   # exercised separately below
        return e

    def test_all_gates_required(self, tmp_path, monkeypatch):
        e = self._engine(tmp_path, monkeypatch, armed=False)
        assert e.open_position(self._opp()) is None and e._orders == []
        e2 = self._engine(tmp_path, monkeypatch, live=False)
        assert e2.open_position(self._opp()) is None and e2._orders == []

    def test_dry_run_records_nothing(self, tmp_path, monkeypatch):
        e = self._engine(tmp_path, monkeypatch, dry=True)
        assert e.open_position(self._opp()) is None
        assert e.state["positions"] == []

    def test_filled_order_recorded_with_paper_accounting(self, tmp_path, monkeypatch):
        e = self._engine(tmp_path, monkeypatch)
        start = e.cash
        pos = e.open_position(self._opp())
        assert pos is not None
        assert e._orders == [("BUY", "tok", 10.0, 0.96)]
        assert e.cash == pytest.approx(start - 9.6)

    def test_unfilled_order_records_nothing(self, tmp_path, monkeypatch):
        e = self._engine(tmp_path, monkeypatch, fill=False)
        start = e.cash
        assert e.open_position(self._opp()) is None
        assert e.state["positions"] == [] and e.cash == pytest.approx(start)

    def test_multileg_locks_refused_by_default(self, tmp_path, monkeypatch):
        e = self._engine(tmp_path, monkeypatch)
        legs = [Leg("a", "ma", "YES a", "YES", 0.30, 10),
                Leg("b", "mb", "YES b", "YES", 0.65, 10)]
        assert e.open_position(self._opp("ARB-L", "ARB", legs)) is None
        assert e._orders == []

    def test_daily_loss_halt(self, tmp_path, monkeypatch):
        from polyedge import live as lv
        import time as _t
        e = self._engine(tmp_path, monkeypatch)
        e.state["closed"].append({"pl": -20.0, "closed_ts": _t.time(),
                                  "strategy": "CONVERGE", "close_reason": ""})
        assert e.open_position(self._opp()) is None
        assert os.path.exists(lv.HALTED_FILE)
        assert not lv.live_gates_open()      # halt closes the gates entirely

    def test_take_profit_sell_only_on_fill(self, tmp_path, monkeypatch):
        e = self._engine(tmp_path, monkeypatch)
        e.open_position(self._opp())
        e._orders.clear()
        closed = e.close_early("CV-L", {"tok": 0.985})
        assert closed is not None and closed["pl"] == pytest.approx(0.25)
        assert e._orders == [("SELL", "tok", 10.0, 0.985)]
        e2 = self._engine(tmp_path, monkeypatch, fill=False)
        e2.open_position(self._opp("CV-L2"))
        assert e2.close_early("CV-L2", {"tok": 0.985}) is None

    def test_paused_blocks_open(self, tmp_path, monkeypatch):
        from polyedge import controls
        e = self._engine(tmp_path, monkeypatch)
        controls.set_paused(True, str(tmp_path))
        assert e.open_position(self._opp()) is None
        assert e._orders == []

    def test_kill_switch_blocks_open(self, tmp_path, monkeypatch):
        from polyedge import controls
        e = self._engine(tmp_path, monkeypatch)
        controls.set_kill_switch(True, str(tmp_path))
        assert e.open_position(self._opp()) is None
        assert e._orders == []

    def test_liquidate_position_refuses_multileg(self, tmp_path, monkeypatch):
        e = self._engine(tmp_path, monkeypatch)
        legs = [Leg("a", "ma", "YES a", "YES", 0.30, 10),
               Leg("b", "mb", "YES b", "YES", 0.65, 10)]
        # multi-leg opens are refused live anyway, so seed the position
        # directly into state to test liquidate_position's own guard
        e.state["positions"].append({
            "key": "ARB-SEED", "strategy": "ARB", "title": "seeded lock",
            "cost": 9.5, "legs": [
                {"token_id": "a", "market_id": "ma", "label": "YES a", "side": "YES",
                 "entry_price": 0.30, "shares": 10},
                {"token_id": "b", "market_id": "mb", "label": "YES b", "side": "YES",
                 "entry_price": 0.65, "shares": 10},
            ],
        })
        assert e.liquidate_position("ARB-SEED", {"a": 0.30, "b": 0.65}) is None
        assert e._orders == []
        assert len(e.state["positions"]) == 1

    def test_liquidate_position_single_leg_success(self, tmp_path, monkeypatch):
        e = self._engine(tmp_path, monkeypatch)
        e.open_position(self._opp())
        e._orders.clear()
        closed = e.liquidate_position("CV-L", {"tok": 0.90})
        assert closed is not None
        assert e._orders == [("SELL", "tok", 10.0, 0.90)]
        assert e.state["positions"] == []

    def test_liquidate_all_mixed(self, tmp_path, monkeypatch):
        e = self._engine(tmp_path, monkeypatch)
        e.open_position(self._opp("CV-A"))
        # seed a multi-leg lock directly -- open_position() itself already
        # refuses these live, so this exercises liquidate_all's OWN guard
        e.state["positions"].append({
            "key": "ARB-L", "strategy": "ARB", "title": "lock", "cost": 9.5,
            "legs": [
                {"token_id": "a", "market_id": "ma", "label": "YES a", "side": "YES",
                 "entry_price": 0.30, "shares": 10},
                {"token_id": "b", "market_id": "mb", "label": "YES b", "side": "YES",
                 "entry_price": 0.65, "shares": 10},
            ],
        })
        closed, skipped = e.liquidate_all({"tok": 0.97, "a": 0.30, "b": 0.65})
        assert [c["key"] for c in closed] == ["CV-A"]
        assert skipped == [("ARB-L", "multi-leg lock -- cannot be forced without stranding a leg")]
        assert [p["key"] for p in e.state["positions"]] == ["ARB-L"]


# ------------------------------------------------------------------ live.py V2 (py-clob-client-v2) wiring
class TestLiveEngineV2Wiring:
    """Covers the CLOB V2 rewrite specifically: the ClobClient construction
    args, order placement via OrderArgsV2/create_and_post_order, and the
    pUSD balance/allowance pre-trade check -- all against a fake
    py_clob_client_v2 package injected into sys.modules, so nothing here
    needs the real package installed or touches the network. Does NOT
    touch the three-gate safety logic itself (see TestLiveEngine above,
    which passes unmodified)."""

    def _install_fake_sdk(self, monkeypatch, balance_allowance=None, order_status="matched"):
        import sys
        import types

        calls = {"constructed": None, "set_api_creds": None,
                 "create_and_post_order": None, "get_balance_allowance": None}

        class FakeApiCreds:
            api_key = "fake-key"

        class FakeClobClient:
            def __init__(self, host, chain_id=None, key=None, signature_type=None, funder=None):
                calls["constructed"] = dict(host=host, chain_id=chain_id, key=key,
                                            signature_type=signature_type, funder=funder)

            def create_or_derive_api_key(self):
                return FakeApiCreds()

            def set_api_creds(self, creds):
                calls["set_api_creds"] = creds

            def create_and_post_order(self, order_args, order_type=None):
                calls["create_and_post_order"] = {"order_args": order_args, "order_type": order_type}
                return {"status": order_status}

            def get_balance_allowance(self, params):
                calls["get_balance_allowance"] = params
                return balance_allowance if balance_allowance is not None else \
                    {"balance": "50", "allowance": "50"}

        client_mod = types.ModuleType("py_clob_client_v2.client")
        client_mod.ClobClient = FakeClobClient

        class OrderArgsV2:
            def __init__(self, token_id, price, size, side):
                self.token_id, self.price, self.size, self.side = token_id, price, size, side

        class OrderType:
            FOK = "FOK"
            GTC = "GTC"

        class AssetType:
            COLLATERAL = "COLLATERAL"
            CONDITIONAL = "CONDITIONAL"

        class BalanceAllowanceParams:
            def __init__(self, asset_type=None):
                self.asset_type = asset_type

        clob_types_mod = types.ModuleType("py_clob_client_v2.clob_types")
        clob_types_mod.OrderArgsV2 = OrderArgsV2
        clob_types_mod.OrderType = OrderType
        clob_types_mod.AssetType = AssetType
        clob_types_mod.BalanceAllowanceParams = BalanceAllowanceParams

        constants_mod = types.ModuleType("py_clob_client_v2.order_builder.constants")
        constants_mod.BUY = "BUY"
        constants_mod.SELL = "SELL"
        order_builder_pkg = types.ModuleType("py_clob_client_v2.order_builder")

        monkeypatch.setitem(sys.modules, "py_clob_client_v2", types.ModuleType("py_clob_client_v2"))
        monkeypatch.setitem(sys.modules, "py_clob_client_v2.client", client_mod)
        monkeypatch.setitem(sys.modules, "py_clob_client_v2.clob_types", clob_types_mod)
        monkeypatch.setitem(sys.modules, "py_clob_client_v2.order_builder", order_builder_pkg)
        monkeypatch.setitem(sys.modules, "py_clob_client_v2.order_builder.constants", constants_mod)
        return calls

    def _engine(self, tmp_path, monkeypatch, key="0xabc", funder="0xdef"):
        from polyedge import live as lv
        monkeypatch.chdir(tmp_path)
        if key is not None:
            monkeypatch.setenv("POLYEDGE_PRIVATE_KEY", key)
        else:
            monkeypatch.delenv("POLYEDGE_PRIVATE_KEY", raising=False)
        monkeypatch.setenv("POLYEDGE_FUNDER_ADDRESS", funder)
        return lv.LiveEngine(state_dir=str(tmp_path))

    def test_clob_client_constructed_with_v2_args_and_default_signature_type(self, tmp_path, monkeypatch):
        calls = self._install_fake_sdk(monkeypatch)
        monkeypatch.delenv("POLYEDGE_SIGNATURE_TYPE", raising=False)
        e = self._engine(tmp_path, monkeypatch)
        e._clob_client()
        assert calls["constructed"]["host"] == config.CLOB_BASE
        assert calls["constructed"]["chain_id"] == 137
        assert calls["constructed"]["key"] == "0xabc"
        assert calls["constructed"]["funder"] == "0xdef"
        # signature_type=1 (POLY_PROXY) by default -- NOT the newer
        # signature_type=3 deposit-wallet pattern, per LIVE.md
        assert calls["constructed"]["signature_type"] == 1
        assert calls["set_api_creds"] is not None   # create_or_derive_api_key() wired through

    def test_clob_client_honors_signature_type_override(self, tmp_path, monkeypatch):
        self._install_fake_sdk(monkeypatch)
        monkeypatch.setenv("POLYEDGE_SIGNATURE_TYPE", "3")
        e = self._engine(tmp_path, monkeypatch)
        e._clob_client()
        # the override still works if an operator deliberately opts in --
        # this repo just never picks it by default (see module docstring)

    def test_place_order_sends_v2_order_args_with_fok(self, tmp_path, monkeypatch):
        calls = self._install_fake_sdk(monkeypatch, order_status="matched")
        e = self._engine(tmp_path, monkeypatch)
        filled = e._place_order("tok-1", 0.965, 10.0, "BUY")
        assert filled is True
        sent = calls["create_and_post_order"]
        assert sent["order_type"] == "FOK"
        assert sent["order_args"].token_id == "tok-1"
        assert sent["order_args"].side == "BUY"
        assert sent["order_args"].price == pytest.approx(0.965)
        assert sent["order_args"].size == pytest.approx(10.0)

    def test_place_order_false_when_not_matched(self, tmp_path, monkeypatch):
        self._install_fake_sdk(monkeypatch, order_status="cancelled")
        e = self._engine(tmp_path, monkeypatch)
        assert e._place_order("tok-1", 0.965, 10.0, "SELL") is False

    def test_pusd_check_true_when_funded(self, tmp_path, monkeypatch):
        calls = self._install_fake_sdk(monkeypatch,
                                       balance_allowance={"balance": "50", "allowance": "50"})
        e = self._engine(tmp_path, monkeypatch)
        assert e._check_pusd_balance() is True
        assert calls["get_balance_allowance"].asset_type == "COLLATERAL"

    def test_pusd_check_false_on_zero_balance(self, tmp_path, monkeypatch):
        self._install_fake_sdk(monkeypatch, balance_allowance={"balance": "0", "allowance": "50"})
        e = self._engine(tmp_path, monkeypatch)
        assert e._check_pusd_balance() is False

    def test_pusd_check_false_on_zero_allowance(self, tmp_path, monkeypatch):
        # wrapped into pUSD but never approved the exchange contract --
        # must be caught too, not just a literal zero balance
        self._install_fake_sdk(monkeypatch, balance_allowance={"balance": "50", "allowance": "0"})
        e = self._engine(tmp_path, monkeypatch)
        assert e._check_pusd_balance() is False

    def test_pusd_check_false_when_sdk_unavailable(self, tmp_path, monkeypatch):
        # no fake SDK installed at all -- the real package genuinely isn't
        # installed in this environment either, so this also exercises the
        # real "package not installed" path, not just a simulated one
        e = self._engine(tmp_path, monkeypatch)
        assert e._check_pusd_balance() is False

    def test_open_position_halts_when_pusd_check_fails(self, tmp_path, monkeypatch):
        from polyedge import live as lv
        monkeypatch.setenv("POLYEDGE_LIVE", "1")
        monkeypatch.setenv("POLYEDGE_DRY_RUN", "0")
        monkeypatch.chdir(tmp_path)
        open(lv.ARMED_FILE, "w").write("armed")
        e = lv.LiveEngine(state_dir=str(tmp_path))
        e._place_order = lambda *a, **k: pytest.fail("must not attempt an order")
        e._check_pusd_balance = lambda: False
        opp = Opportunity("CONVERGE", "CV-X", "CV-X", 0.04, False, est_p_win=0.98,
                          legs=[Leg("tok", "m1", "YES q", "YES", 0.96, 10.0)],
                          resolve_by="2026-07-21T00:00:00Z")
        assert e.open_position(opp) is None
        assert os.path.exists(lv.HALTED_FILE)
        assert not lv.live_gates_open()   # the halt closes the gates entirely, same as the daily-loss breaker


# ------------------------------------------------------------------ controls (control panel)
class TestControls:
    def test_load_defaults_when_missing(self, tmp_path):
        from polyedge import controls
        st = controls.load(str(tmp_path))
        assert st == {"paused": False, "kill_switch": False,
                      "max_allocation_usd": None, "liquidate_queue": [],
                      "stop_loss_pct": {}}

    def test_corrupt_file_returns_defaults(self, tmp_path):
        from polyedge import controls
        (tmp_path / "controls.json").write_text("{not json")
        st = controls.load(str(tmp_path))
        assert st["paused"] is False and st["kill_switch"] is False

    def test_save_load_roundtrip(self, tmp_path):
        from polyedge import controls
        controls.set_paused(True, str(tmp_path))
        controls.set_max_allocation(250.0, str(tmp_path))
        st = controls.load(str(tmp_path))
        assert st["paused"] is True
        assert st["max_allocation_usd"] == pytest.approx(250.0)

    def test_kill_switch_toggle(self, tmp_path):
        from polyedge import controls
        controls.set_kill_switch(True, str(tmp_path))
        assert controls.load(str(tmp_path))["kill_switch"] is True
        controls.set_kill_switch(False, str(tmp_path))
        assert controls.load(str(tmp_path))["kill_switch"] is False

    def test_liquidate_queue_dedupes_and_clears(self, tmp_path):
        from polyedge import controls
        controls.queue_liquidate("A", str(tmp_path))
        controls.queue_liquidate("A", str(tmp_path))
        controls.queue_liquidate("B", str(tmp_path))
        assert controls.load(str(tmp_path))["liquidate_queue"] == ["A", "B"]
        controls.clear_liquidate_queue(str(tmp_path))
        assert controls.load(str(tmp_path))["liquidate_queue"] == []

    def test_set_stop_loss_and_clear(self, tmp_path):
        from polyedge import controls
        controls.set_stop_loss("CV-X", 20, str(tmp_path))
        assert controls.load(str(tmp_path))["stop_loss_pct"] == {"CV-X": 20.0}
        controls.set_stop_loss("CV-X", None, str(tmp_path))
        assert controls.load(str(tmp_path))["stop_loss_pct"] == {}

    def test_set_stop_loss_clamped_to_100(self, tmp_path):
        from polyedge import controls
        controls.set_stop_loss("CV-X", 500, str(tmp_path))
        assert controls.load(str(tmp_path))["stop_loss_pct"]["CV-X"] == 100.0


# ------------------------------------------------------------------ apply_controls orchestration
class _FakeClient:
    """Stub for PolymarketClient.fetch_books, keyed by token_id -> bid price."""
    def __init__(self, bids):
        self.bids = bids

    def fetch_books(self, token_ids):
        out = {}
        for tid in token_ids:
            if tid in self.bids:
                out[tid] = book(tid, self.bids[tid] + 0.01, bid=self.bids[tid])
        return out


class TestApplyControls:
    def _engine(self, tmp_path, monkeypatch, fill=True):
        from polyedge import live as lv
        monkeypatch.setenv("POLYEDGE_LIVE", "1")
        monkeypatch.setenv("POLYEDGE_DRY_RUN", "0")
        monkeypatch.chdir(tmp_path)
        open(lv.ARMED_FILE, "w").write("armed")
        e = lv.LiveEngine(state_dir=str(tmp_path))
        e._orders = []

        def fake_place(token_id, price, shares, side):
            e._orders.append((side, token_id, round(shares, 2), round(price, 3)))
            return fill and not lv.dry_run()
        e._place_order = fake_place
        e._check_pusd_balance = lambda: True   # exercised separately in TestLiveEngine
        return e

    def _cv_opp(self, key, entry=0.96, shares=10.0, token=None):
        token = token or f"tok-{key}"
        return Opportunity("CONVERGE", key, key, 0.04, False, est_p_win=0.98,
                           legs=[Leg(token, f"m-{key}", "YES q", "YES", entry, shares)],
                           resolve_by="2026-07-21T00:00:00Z")

    def test_noop_when_no_controls_set(self, tmp_path, monkeypatch):
        from polyedge.controls import apply_controls
        e = self._engine(tmp_path, monkeypatch)
        e.open_position(self._cv_opp("CV-A"))
        summary = apply_controls(e, _FakeClient({}))
        assert summary == {"killed": [], "liquidated": [], "stop_loss": [], "skipped": []}
        assert len(e.state["positions"]) == 1

    def test_kill_switch_liquidates_single_leg_skips_multileg(self, tmp_path, monkeypatch):
        from polyedge import controls
        from polyedge.controls import apply_controls
        e = self._engine(tmp_path, monkeypatch)
        e.open_position(self._cv_opp("CV-A", token="tok-a"))
        e.state["positions"].append({
            "key": "ARB-L", "strategy": "ARB", "title": "lock", "cost": 9.5,
            "legs": [
                {"token_id": "x", "market_id": "mx", "label": "YES x", "side": "YES",
                 "entry_price": 0.30, "shares": 10},
                {"token_id": "y", "market_id": "my", "label": "YES y", "side": "YES",
                 "entry_price": 0.65, "shares": 10},
            ],
        })
        controls.set_kill_switch(True, str(tmp_path))
        summary = apply_controls(e, _FakeClient({"tok-a": 0.97}))
        assert summary["killed"] == ["CV-A"]
        assert any(k == "ARB-L" for k, _ in summary["skipped"])
        keys = {p["key"] for p in e.state["positions"]}
        assert keys == {"ARB-L"}

    def test_liquidate_queue_drains(self, tmp_path, monkeypatch):
        from polyedge import controls
        from polyedge.controls import apply_controls
        e = self._engine(tmp_path, monkeypatch)
        e.open_position(self._cv_opp("CV-A", token="tok-a"))
        controls.queue_liquidate("CV-A", str(tmp_path))
        summary = apply_controls(e, _FakeClient({"tok-a": 0.97}))
        assert summary["liquidated"] == ["CV-A"]
        assert e.state["positions"] == []
        assert controls.load(str(tmp_path))["liquidate_queue"] == []

    def test_allocation_cap_liquidates_largest_first(self, tmp_path, monkeypatch):
        from polyedge import controls
        from polyedge.controls import apply_controls
        e = self._engine(tmp_path, monkeypatch)
        e.open_position(self._cv_opp("CV-BIG", shares=50.0, token="tok-big"))   # cost 48
        e.open_position(self._cv_opp("CV-SMALL", shares=10.0, token="tok-small"))  # cost 9.6
        controls.set_max_allocation(20.0, str(tmp_path))
        summary = apply_controls(e, _FakeClient({"tok-big": 0.95, "tok-small": 0.95}))
        assert "CV-BIG" in summary["liquidated"]
        keys = {p["key"] for p in e.state["positions"]}
        assert "CV-BIG" not in keys
        assert "CV-SMALL" in keys

    def test_allocation_cap_cannot_force_multileg(self, tmp_path, monkeypatch):
        from polyedge import controls
        from polyedge.controls import apply_controls
        e = self._engine(tmp_path, monkeypatch)
        e.state["positions"].append({
            "key": "ARB-L", "strategy": "ARB", "title": "lock", "cost": 100.0,
            "legs": [
                {"token_id": "x", "market_id": "mx", "label": "YES x", "side": "YES",
                 "entry_price": 0.30, "shares": 10},
                {"token_id": "y", "market_id": "my", "label": "YES y", "side": "YES",
                 "entry_price": 0.65, "shares": 10},
            ],
        })
        controls.set_max_allocation(1.0, str(tmp_path))
        apply_controls(e, _FakeClient({}))
        assert len(e.state["positions"]) == 1     # never force-liquidated

    def test_stop_loss_triggers(self, tmp_path, monkeypatch):
        from polyedge import controls
        from polyedge.controls import apply_controls
        e = self._engine(tmp_path, monkeypatch)
        e.open_position(self._cv_opp("CV-A", entry=0.96, token="tok-a"))
        controls.set_stop_loss("CV-A", 5, str(tmp_path))   # 5% loss threshold
        # bid 0.90 vs entry 0.96 -> down ~6.25%, over the 5% threshold
        summary = apply_controls(e, _FakeClient({"tok-a": 0.90}))
        assert summary["stop_loss"] == ["CV-A"]
        assert e.state["positions"] == []

    def test_stop_loss_not_triggered_below_threshold(self, tmp_path, monkeypatch):
        from polyedge import controls
        from polyedge.controls import apply_controls
        e = self._engine(tmp_path, monkeypatch)
        e.open_position(self._cv_opp("CV-A", entry=0.96, token="tok-a"))
        controls.set_stop_loss("CV-A", 5, str(tmp_path))
        # bid 0.955 vs entry 0.96 -> down ~0.5%, under the 5% threshold
        summary = apply_controls(e, _FakeClient({"tok-a": 0.955}))
        assert summary["stop_loss"] == []
        assert len(e.state["positions"]) == 1


# ------------------------------------------------------------------ control_server (Flask)
class TestControlServer:
    def _make_client(self, tmp_path, monkeypatch, token="secret-token"):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("POLYBERT_CONTROL_TOKEN", token)
        import control_server
        control_server.app.testing = True
        return control_server.app.test_client()

    def _seed_position(self, tmp_path):
        e = PaperEngine(state_dir=str(tmp_path / "state"))
        opp = Opportunity("CONVERGE", "CV-A", "converge", 0.04, False, est_p_win=0.98,
                          legs=[Leg("tok-a", "m1", "YES q", "YES", 0.96, 10.0)])
        e.open_position(opp)
        e.mark_to_market({"tok-a": 0.97})
        e.save()

    def test_state_requires_auth(self, tmp_path, monkeypatch):
        c = self._make_client(tmp_path, monkeypatch)
        assert c.get("/api/state").status_code == 401
        assert c.get("/api/state", headers={"X-Control-Token": "wrong"}).status_code == 401
        r = c.get("/api/state", headers={"X-Control-Token": "secret-token"})
        assert r.status_code == 200

    def test_refuses_when_no_token_configured(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("POLYBERT_CONTROL_TOKEN", raising=False)
        import control_server
        control_server.app.testing = True
        c = control_server.app.test_client()
        r = c.get("/api/state", headers={"X-Control-Token": ""})
        assert r.status_code == 401

    def test_state_reports_seeded_position(self, tmp_path, monkeypatch):
        self._seed_position(tmp_path)
        c = self._make_client(tmp_path, monkeypatch)
        r = c.get("/api/state", headers={"X-Control-Token": "secret-token"})
        data = r.get_json()
        assert [p["key"] for p in data["positions"]] == ["CV-A"]
        assert data["positions"][0]["multi_leg"] is False

    def test_pause_route(self, tmp_path, monkeypatch):
        c = self._make_client(tmp_path, monkeypatch)
        h = {"X-Control-Token": "secret-token"}
        assert c.post("/api/pause", json={}, headers={"X-Control-Token": "wrong"}).status_code == 401
        r = c.post("/api/pause", json={"paused": True}, headers=h)
        assert r.status_code == 200 and r.get_json()["paused"] is True
        r2 = c.get("/api/state", headers=h)
        assert r2.get_json()["controls"]["paused"] is True

    def test_killswitch_route(self, tmp_path, monkeypatch):
        c = self._make_client(tmp_path, monkeypatch)
        h = {"X-Control-Token": "secret-token"}
        r = c.post("/api/killswitch", json={"on": True}, headers=h)
        assert r.status_code == 200 and r.get_json()["kill_switch"] is True

    def test_liquidate_route_queues_key(self, tmp_path, monkeypatch):
        self._seed_position(tmp_path)
        c = self._make_client(tmp_path, monkeypatch)
        h = {"X-Control-Token": "secret-token"}
        assert c.post("/api/liquidate", json={}, headers=h).status_code == 400
        r = c.post("/api/liquidate", json={"key": "CV-A"}, headers=h)
        assert r.status_code == 200 and r.get_json()["liquidate_queue"] == ["CV-A"]

    def test_dashboard_requires_auth(self, tmp_path, monkeypatch):
        c = self._make_client(tmp_path, monkeypatch)
        assert c.get("/dashboard").status_code == 401
        assert c.get("/dashboard?token=wrong").status_code == 401
        r = c.get("/dashboard", headers={"X-Control-Token": "secret-token"})
        assert r.status_code == 200

    def test_dashboard_renders_empty_state(self, tmp_path, monkeypatch):
        c = self._make_client(tmp_path, monkeypatch)
        r = c.get("/dashboard?token=secret-token")
        assert r.status_code == 200
        body = r.get_data(as_text=True)
        assert "PolyBert" in body
        assert "__MODE_LABEL__" not in body and "__STATE_JSON__" not in body

    def test_dashboard_renders_with_open_position(self, tmp_path, monkeypatch):
        self._seed_position(tmp_path)
        c = self._make_client(tmp_path, monkeypatch)
        r = c.get("/dashboard?token=secret-token")
        assert r.status_code == 200
        assert "CV-A" in r.get_data(as_text=True)

    def test_dashboard_mode_label_reflects_gates(self, tmp_path, monkeypatch):
        c = self._make_client(tmp_path, monkeypatch)
        r = c.get("/dashboard?token=secret-token")
        assert "Mode <b>Paper</b>" in r.get_data(as_text=True)
        monkeypatch.setenv("POLYEDGE_LIVE", "1")
        r2 = c.get("/dashboard?token=secret-token")
        assert "Mode <b>Dry-run</b>" in r2.get_data(as_text=True)

    def test_allocation_route(self, tmp_path, monkeypatch):
        c = self._make_client(tmp_path, monkeypatch)
        h = {"X-Control-Token": "secret-token"}
        r = c.post("/api/allocation", json={"max_allocation_usd": 75}, headers=h)
        assert r.status_code == 200 and r.get_json()["max_allocation_usd"] == pytest.approx(75.0)
        r2 = c.post("/api/allocation", json={"max_allocation_usd": None}, headers=h)
        assert r2.get_json()["max_allocation_usd"] is None

    def test_stop_loss_route(self, tmp_path, monkeypatch):
        self._seed_position(tmp_path)
        c = self._make_client(tmp_path, monkeypatch)
        h = {"X-Control-Token": "secret-token"}
        assert c.post("/api/stop_loss", json={"pct": 20}, headers=h).status_code == 400
        r = c.post("/api/stop_loss", json={"key": "CV-A", "pct": 20}, headers=h)
        assert r.status_code == 200 and r.get_json()["stop_loss_pct"] == {"CV-A": 20.0}
        r2 = c.post("/api/stop_loss", json={"key": "CV-A", "pct": 0}, headers=h)
        assert r2.get_json()["stop_loss_pct"] == {}


# ------------------------------------------------------------------ reconcile
class _FakeResponse:
    def __init__(self, json_data, status=200):
        self._json = json_data
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"status {self.status_code}")

    def json(self):
        return self._json


class _FakeSession:
    """Routes GET/POST to canned responses by URL, so reconcile.py's real
    HTTP calls never actually happen in tests."""
    def __init__(self, positions_response=None, balance_hex="0x0",
                 raise_on_get=False, raise_on_post=False):
        self.positions_response = positions_response if positions_response is not None else []
        self.balance_hex = balance_hex
        self.raise_on_get = raise_on_get
        self.raise_on_post = raise_on_post

    def get(self, url, params=None, timeout=None):
        if self.raise_on_get:
            raise requests.ConnectionError("simulated network failure")
        assert "data-api.polymarket.com/positions" in url
        return _FakeResponse(self.positions_response)

    def post(self, url, json=None, timeout=None):
        if self.raise_on_post:
            raise requests.ConnectionError("simulated network failure")
        assert "polygon-rpc.com" in url
        return _FakeResponse({"result": self.balance_hex})


def _pusd_hex(amount: float) -> str:
    """Encode a pUSD amount (assumed 6 decimals -- see reconcile.py's
    _PUSD_DECIMALS comment) as the hex string eth_call would return."""
    return hex(int(round(amount * 1e6)))


class TestReconcile:
    FUNDER = "0x" + "1" * 40

    def test_matching_wallet_does_not_exceed_threshold(self):
        state = {"cash": 60.0, "positions": [{"current_value": 40.0}]}
        session = _FakeSession(
            positions_response=[{"currentValue": 40.0}],
            balance_hex=_pusd_hex(60.0))
        result = reconcile.check(state, self.FUNDER, session=session)
        assert result["ok"] is True
        assert result["exceeded_threshold"] is False
        assert result["bot_equity"] == pytest.approx(100.0)
        assert result["real_equity"] == pytest.approx(100.0)
        assert result["diff_pct"] == pytest.approx(0.0)

    def test_large_divergence_flagged(self):
        # bot thinks it has $100, real wallet only has $50 -- 50% off
        state = {"cash": 100.0, "positions": []}
        session = _FakeSession(positions_response=[], balance_hex=_pusd_hex(50.0))
        result = reconcile.check(state, self.FUNDER, session=session,
                                 halt_threshold_pct=15.0)
        assert result["ok"] is True
        assert result["exceeded_threshold"] is True
        assert result["diff_pct"] == pytest.approx(100.0, abs=1.0)  # |100-50|/50*100

    def test_small_divergence_within_threshold(self):
        # $2 off on $100 -- normal fee/rounding noise, should NOT trip
        state = {"cash": 98.0, "positions": []}
        session = _FakeSession(positions_response=[], balance_hex=_pusd_hex(100.0))
        result = reconcile.check(state, self.FUNDER, session=session,
                                 halt_threshold_pct=15.0)
        assert result["ok"] is True
        assert result["exceeded_threshold"] is False

    def test_network_failure_on_positions_does_not_raise(self):
        state = {"cash": 100.0, "positions": []}
        session = _FakeSession(raise_on_get=True)
        result = reconcile.check(state, self.FUNDER, session=session)
        assert result["ok"] is False
        assert "bot_equity" in result

    def test_network_failure_on_balance_does_not_raise(self):
        state = {"cash": 100.0, "positions": []}
        session = _FakeSession(raise_on_post=True)
        result = reconcile.check(state, self.FUNDER, session=session)
        assert result["ok"] is False

    def test_empty_real_wallet_with_bot_equity_is_full_divergence(self):
        """Real wallet reads back completely empty (e.g. never funded, or
        funds withdrawn) while the bot still thinks it holds money -- must
        not divide-by-zero, and must clearly flag this as maximal, not 0%,
        divergence."""
        state = {"cash": 50.0, "positions": []}
        session = _FakeSession(positions_response=[], balance_hex=_pusd_hex(0.0))
        result = reconcile.check(state, self.FUNDER, session=session)
        assert result["ok"] is True
        assert result["exceeded_threshold"] is True
        assert result["diff_pct"] == 100.0

    def test_both_empty_is_not_a_divergence(self):
        """Bot has nothing, real wallet has nothing -- 0% divergence, not
        a false alarm from the same divide-by-zero edge case above."""
        state = {"cash": 0.0, "positions": []}
        session = _FakeSession(positions_response=[], balance_hex=_pusd_hex(0.0))
        result = reconcile.check(state, self.FUNDER, session=session)
        assert result["ok"] is True
        assert result["exceeded_threshold"] is False
        assert result["diff_pct"] == 0.0

    def test_queries_pusd_contract_not_usdc(self):
        """V2 regression: the eth_call must target the pUSD contract, not
        the retired USDC.e address -- a stale address wouldn't error, it
        would just silently read back $0 forever (see the module comment)."""
        captured = {}

        class _CapturingSession(_FakeSession):
            def post(self, url, json=None, timeout=None):
                captured["to"] = json["params"][0]["to"]
                return super().post(url, json=json, timeout=timeout)

        session = _CapturingSession(positions_response=[], balance_hex=_pusd_hex(10.0))
        reconcile.check({"cash": 10.0, "positions": []}, self.FUNDER, session=session)
        assert captured["to"] == reconcile.PUSD_CONTRACT_ADDRESS
        assert captured["to"] != "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"  # old USDC.e address

    def test_result_reports_real_pusd_key(self):
        state = {"cash": 25.0, "positions": []}
        session = _FakeSession(positions_response=[], balance_hex=_pusd_hex(25.0))
        result = reconcile.check(state, self.FUNDER, session=session)
        assert result["real_pusd"] == pytest.approx(25.0)
        assert "real_usdc" not in result
