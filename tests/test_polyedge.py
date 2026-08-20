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


class _FakeAcceptedOrder:
    """Stand-in for polymarket-client's AcceptedOrder (ok=True, status in
    {"live", "matched", "delayed"} -- confirmed from source)."""
    def __init__(self, status="matched", order_id="fake-order-id"):
        self.ok = True
        self.status = status
        self.order_id = order_id


class _FakeTrade:
    """Stand-in for polymarket-client's ClobTrade -- only the field
    LiveEngine._resolve_fill actually reads (taker_order_id "identifies
    the originating order" -- confirmed from source)."""
    def __init__(self, taker_order_id):
        self.taker_order_id = taker_order_id


class _FakeTradesPaginator:
    """Stand-in for the AsyncPaginator[ClobTrade] list_account_trades()
    returns -- only .iter_items() (an async generator) is used by
    LiveEngine._resolve_fill."""
    def __init__(self, items=(), error=None):
        self._items = items
        self._error = error

    def iter_items(self):
        async def _gen():
            if self._error is not None:
                raise self._error
            for item in self._items:
                yield item
        return _gen()


class _FakeRejectedOrder:
    """Stand-in for polymarket-client's RejectedOrder (ok=False, code e.g.
    "fok_not_filled" -- confirmed from source)."""
    def __init__(self, code="fok_not_filled"):
        self.ok = False
        self.code = code


class _FakePolymarketError(Exception):
    """Stand-in for polymarket.errors.PolymarketError, the base class for
    the SDK's whole error hierarchy -- confirmed from source."""


class _FakeRequestRejectedError(_FakePolymarketError):
    """Stand-in for polymarket.errors.RequestRejectedError -- the class a
    real FOK order that couldn't fill actually raised in production,
    instead of coming back as a RejectedOrder response object."""
    def __init__(self, message, status=None, retry_after=None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


class _FakeInsufficientLiquidityError(_FakePolymarketError):
    """Stand-in for polymarket.errors.InsufficientLiquidityError -- another
    PolymarketError subclass a FOK order could plausibly raise for, which
    catching only RequestRejectedError would have missed."""


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

    # ---- weather exclusion -- real evidence: LS-3412924, an actual live
    # LONGSHOT position that motivated this whole task. LONGSHOT had NONE
    # of CONVERGE's exclusion filters before this -- a real gap, not just
    # a CONVERGE-side one, since a narrow bracket on a volatile continuous
    # quantity is the same risk regardless of which strategy trades it.
    def test_ls3412924_regression_real_title_now_excluded(self):
        """The exact real title, verbatim, from the live position that
        motivated this task."""
        m = market("L1", 0.04, end=_future(5),
                   question="Fade: Will the highest temperature in "
                            "Seattle be between 72-73°F...")
        assert longshot.is_weather_market(m)   # reused from convergence.py, not duplicated
        books = {m.no_token: book(m.no_token, 0.965)}
        assert longshot.scan([m], books) == []

    def test_weather_excluded_alongside_a_real_fade(self):
        wx = market("L1", 0.04, end=_future(5),
                   question="Fade: Will the highest temperature in "
                            "Seattle be between 72-73°F...")
        real = market("L2", 0.04, end=_future(5), event="OTHER",
                      question="Will X post fewer than 10 statements this week?")
        books = {m.no_token: book(m.no_token, 0.965) for m in (wx, real)}
        out = longshot.scan([wx, real], books)
        assert [o.key for o in out] == ["LS-L2"]

    def test_weather_exclusion_toggle_off(self, monkeypatch):
        monkeypatch.setattr(config, "LS_EXCLUDE_WEATHER", False)
        m = market("L1", 0.04, end=_future(5),
                   question="Fade: Will the highest temperature in "
                            "Seattle be between 72-73°F...")
        books = {m.no_token: book(m.no_token, 0.965)}
        assert [o.key for o in longshot.scan([m], books)] == ["LS-L1"]

    # ---- sports exclusion history: LONGSHOT originally left sports
    # deliberately UN-excluded, then excluded it after real production
    # results showed sports-category positions accounting for the large
    # majority of LONGSHOT's real losses, then RE-INCLUDED it (reverted)
    # once the default stop-loss became adjustable via the control panel
    # -- see longshot.py's module docstring for the full reasoning.
    # Sports is back in scope now; these tests confirm real LONGSHOT
    # candidates that actually appeared in production logs are valid
    # candidates again, so a future re-add of a sports filter can't
    # silently reintroduce the exclusion without a test noticing.
    def test_ls_exact_score_regression_real_title_is_valid_candidate(self):
        m = market("L1", 0.04, end=_future(5),
                   question="Exact Score: Ilves Tampere 0 - 0 UMF Stjarnan?")
        m.category = "sports soccer"   # realistic Gamma tag for a match market
        assert convergence.is_sports_match(m)   # reused from convergence.py, not duplicated
        books = {m.no_token: book(m.no_token, 0.965)}
        assert [o.key for o in longshot.scan([m], books)] == ["LS-L1"]

    def test_ls_spread_regression_real_title_is_valid_candidate(self):
        m = market("L1", 0.04, end=_future(5),
                   question="Spread: ACF Fiorentina (-2.5)")
        assert convergence.is_sports_match(m)
        books = {m.no_token: book(m.no_token, 0.965)}
        assert [o.key for o in longshot.scan([m], books)] == ["LS-L1"]

    def test_ls_non_sports_candidate_not_over_excluded(self):
        """A genuine, realistic non-sports LONGSHOT candidate -- a crypto
        price-threshold fade -- must still be a valid candidate too."""
        m = market("L1", 0.04, end=_future(5),
                   question="Will Bitcoin fall below $40,000 by August 1?")
        m.category = "crypto"
        assert not convergence.is_sports_match(m)
        books = {m.no_token: book(m.no_token, 0.965)}
        assert [o.key for o in longshot.scan([m], books)] == ["LS-L1"]

    def test_ls_sports_included_weather_still_excluded_alongside_a_real_fade(self):
        sport = market("L1", 0.04, end=_future(5),
                       question="Spread: ACF Fiorentina (-2.5)")
        wx = market("L2", 0.04, end=_future(5), event="OTHER",
                   question="Fade: Will the highest temperature in "
                            "Seattle be between 72-73°F...")
        real = market("L3", 0.04, end=_future(5), event="OTHER2",
                      question="Will Bitcoin fall below $40,000 by August 1?")
        books = {m.no_token: book(m.no_token, 0.965) for m in (sport, wx, real)}
        out = longshot.scan([sport, wx, real], books)
        assert {o.key for o in out} == {"LS-L1", "LS-L3"}


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

    # ---- weather exclusion ----
    # Real evidence: LS-3412924, an actual live LONGSHOT position ("Fade:
    # Will the highest temperature in Seattle be between 72-73°F...") --
    # a narrow bracket on a continuously-moving quantity, the same risk
    # shape as the tweet-count bracket that caused CONVERGE's original
    # documented loss. is_bracket_market() does NOT catch it (no $ or %
    # sign, no countable noun), so it's tested here as its own detector.
    def test_weather_titles_detected(self):
        weather_titles = [
            "Will the highest temperature in Seattle be between 72-73°F "
            "on August 15?",                      # LS-3412924's real title, CONVERGE-shaped
            "Will NYC see over 6 inches of snowfall by January?",
            "Will it rain in Miami on July 4th?",
            "Chance of snow in Denver exceeds 50% tomorrow?",
            "Will humidity in Phoenix exceed 40% today?",
            "Will a hurricane make landfall in Florida this season?",
            "Highest temp in Chicago on July 20?",
        ]
        for t in weather_titles:
            m = market("W1", 0.96, question=t)
            assert convergence.is_weather_market(m), f"should detect: {t}"

    def test_weather_category_tag_detection(self):
        m = market("W2", 0.96, question="Chicago high on July 20?")
        m.category = "weather culture"
        assert convergence.is_weather_market(m)

    def test_weather_false_positive_guard_proper_noun_title(self):
        """CRITICAL false-positive guard: bare "rain"/"snow" word matching
        was tried first and confirmed (by hand) to false-positive on this
        exact title shape -- a legitimate culture-category market whose
        title happens to quote a proper noun that collides with weather
        vocabulary. The detector deliberately requires recognizable
        weather-question phrasing (rainfall/rainy/"will it rain"/"chance
        of snow"/etc.) rather than the bare noun, specifically to avoid this."""
        non_weather_titles = [
            "Will Kanye's new album 'Rain' go platinum?",
            "Will 'Purple Rain' reissue chart at #1 on Billboard?",
            "Will the movie 'Snow' win Best Picture?",
        ]
        for t in non_weather_titles:
            m = market("W3", 0.96, question=t)
            assert not convergence.is_weather_market(m), f"false positive: {t}"

    def test_scan_excludes_weather(self):
        wx = market("WX", 0.96, end=_future(3), liq=99999,
                   question="Will the highest temperature in Seattle be "
                            "between 72-73°F on August 15?")
        news = market("NW6", 0.96, end=_future(3), liq=99999,
                      question="Israeli parliament dissolved by July 17?")
        books = {m.yes_token: book(m.yes_token, 0.96) for m in (wx, news)}
        out = convergence.scan([wx, news], books)
        assert [o.key for o in out] == ["CV-NW6"]

    def test_scan_includes_weather_when_disabled(self, monkeypatch):
        monkeypatch.setattr(config, "CV_EXCLUDE_WEATHER", False)
        wx = market("WX2", 0.96, end=_future(3), liq=99999,
                   question="Will the highest temperature in Seattle be "
                            "between 72-73°F on August 15?")
        books = {wx.yes_token: book(wx.yes_token, 0.96)}
        out = convergence.scan([wx], books)
        assert [o.key for o in out] == ["CV-WX2"]

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

    # ---- rejected_cooldown: a real production incident where a single
    # token whose FOK order kept getting rejected (RequestRejectedError --
    # see live.py) was re-selected as risk.py's best candidate cycle after
    # cycle for over an hour, starving every other real candidate of a
    # sizing slot -- nothing about a rejection changes the token's edge/
    # price/liquidity inputs. Uses the EXACT real token_id/opportunity key
    # from that incident as a literal regression case.
    _REAL_REJECTED_TOKEN_ID = ("24179142020748386308900785500935095928275106"
                               "841229237369829185074319749593959")

    def _real_ls_opp(self):
        return Opportunity(strategy="LONGSHOT", key="LS-3144825", title="LS-3144825",
                           edge=0.10, guaranteed=False, est_p_win=0.97,
                           legs=[Leg(self._REAL_REJECTED_TOKEN_ID, "m", "NO x",
                                    "NO", 0.95, 0.0)])

    def test_rejected_cooldown_excludes_recently_rejected_real_token(self):
        import time
        opp = self._real_ls_opp()
        # rejected_cooldown stores the EXPIRY timestamp, not the rejection
        # time -- see live.py's open_position()/risk.py's docstring
        cooldown = {self._REAL_REJECTED_TOKEN_ID:
                   time.time() + config.LIVE_REJECTED_COOLDOWN_MIN * 60}
        sized = size_opportunities([opp], bankroll=1000, cash=1000,
                                   strategy_exposure={}, total_exposure=0,
                                   rejected_cooldown=cooldown)
        assert sized == []

    def test_rejected_cooldown_expires_after_the_configured_window(self):
        import time
        opp = self._real_ls_opp()
        expired_ts = time.time() - 60   # expiry already in the past
        cooldown = {self._REAL_REJECTED_TOKEN_ID: expired_ts}
        sized = size_opportunities([opp], bankroll=1000, cash=1000,
                                   strategy_exposure={}, total_exposure=0,
                                   rejected_cooldown=cooldown)
        assert sized and sized[0].key == "LS-3144825"

    def test_rejected_cooldown_none_or_empty_is_a_noop(self):
        opp = self._real_ls_opp()
        for cooldown in (None, {}):
            sized = size_opportunities([opp], bankroll=1000, cash=1000,
                                       strategy_exposure={}, total_exposure=0,
                                       rejected_cooldown=cooldown)
            assert sized and sized[0].key == "LS-3144825"

    def test_in_cooldown_counted_and_logged(self, caplog):
        import logging as _logging
        import time
        opp = self._real_ls_opp()
        cooldown = {self._REAL_REJECTED_TOKEN_ID:
                   time.time() + config.LIVE_REJECTED_COOLDOWN_MIN * 60}
        with caplog.at_level(_logging.INFO, logger="polyedge.risk"):
            size_opportunities([opp], bankroll=1000, cash=1000,
                               strategy_exposure={}, total_exposure=0,
                               rejected_cooldown=cooldown)
        assert any("in_cooldown" in r.message for r in caplog.records)

    # ---- min_order_size: a real production rejection. Confirmed from
    # Polymarket's own /book response schema and independently from a
    # third-party writeup: min_order_size is in SHARES, not dollars, and
    # applies to FOK/FAK orders (which never rest, so the larger 5-share
    # GTC/GTD-only minimum does NOT apply to them). Real reproduction: a
    # flat ~$5 CONVERGE ticket on a market trading near 0.95-0.99 buys
    # only ~5.05-5.3 shares -- right at/barely above typical min_order_size
    # floors -- and got rejected ("order couldn't be fully filled") even
    # against a confirmed-deep book (49,885 shares resting at 0.999).
    #
    # A CONVERGE opportunity's real est_p_win sits only slightly above its
    # own price (see convergence.py's p_assumed formula), so its raw Kelly
    # edge -- and therefore whether the position cap or Kelly itself binds
    # -- is sensitive to the exact price/probability pairing. Rather than
    # hand-pick a p_win and hope the resulting dollar figure lands
    # somewhere useful, this solves for the est_p_win that makes Kelly
    # sizing land at an exact target budget (here, the real ~$5 ticket),
    # so the tests below are robust to config changes rather than tied to
    # one brittle hand-picked probability.
    def _convergence_leg_at_target_budget(self, price, target_budget, bankroll, key):
        net_odds = (1.0 - price) / price
        f_target = target_budget / (bankroll * config.KELLY_FRACTION)
        p_win = (1.0 + f_target * net_odds) / (1.0 + net_odds)
        assert 0.0 < p_win < 1.0, "test setup: target_budget not achievable at this price"
        return Opportunity(strategy="CONVERGE", key=key, title=key,
                           edge=0.03, guaranteed=False, est_p_win=p_win,
                           legs=[Leg(f"tok-{key}", f"m-{key}", "YES q", "YES", price, 0.0)])

    def test_ticket_bumped_to_clear_min_order_size_regression_real_numbers(self, monkeypatch):
        # isolates the BUMP MECHANIC itself from POLYEDGE_MIN_ORDER_SIZE_MARGIN_PCT
        # (added in a later task, default 2% -- tested on its own below,
        # including the CV-3290748 regression that motivated it) so this
        # test keeps proving the original, simpler claim: price-aware
        # bumping to exactly min_order_size, within the position cap.
        monkeypatch.setattr(config, "MIN_ORDER_SIZE_MARGIN_PCT", 0.0)
        price = 0.99            # high end of the real 0.95-0.99 reproduction range
        bankroll = 1000.0       # cap = bankroll * MAX_POSITION_PCT = $25 -- headroom for the bump
        target_budget = 5.02     # the real "~$5 ticket" (5.02 not 5.00 -- keeps the
        # solved budget comfortably clear of MIN_TICKET despite float noise
        # in the kelly_fraction round-trip)
        opp = self._convergence_leg_at_target_budget(price, target_budget, bankroll, "CV-REAL")

        shares_before_bump = target_budget / price   # the real ~5.05-5.3 share range
        assert 5.0 <= shares_before_bump <= 5.5        # sanity: matches the real report
        min_order_size = 5.15   # "right at/barely above" what the $5 ticket buys at 0.99

        book = OrderBook("tok-CV-REAL", min_order_size=min_order_size)
        sized = size_opportunities([opp], bankroll=bankroll, cash=1000,
                                   strategy_exposure={}, total_exposure=0,
                                   books={"tok-CV-REAL": book})
        assert sized and sized[0].legs[0].shares == pytest.approx(min_order_size)
        # price-aware: the bumped dollar cost is min_order_size * price, not
        # some flat bump that could still fall short (or overshoot) elsewhere
        assert sized[0].legs[0].shares * price == pytest.approx(min_order_size * price)
        assert sized[0].legs[0].shares * price <= bankroll * config.MAX_POSITION_PCT + 1e-9

    def test_lower_price_in_the_real_range_clears_without_bumping(self, monkeypatch):
        """Price-aware: the SAME min_order_size (5.15) that forces a bump
        at 0.99 (buys only ~5.05 shares from the same $5 ticket) must NOT
        force one at 0.95 (buys ~5.26 shares from that same $5 ticket) --
        a flat, non-price-aware bump could not distinguish these two real
        points in the reproduction's 0.95-0.99 price range."""
        monkeypatch.setattr(config, "MIN_ORDER_SIZE_MARGIN_PCT", 0.0)   # see test above
        bankroll = 1000.0
        target_budget = 5.02
        min_order_size = 5.15   # same floor used in the 0.99 regression test above

        price = 0.95
        shares_at_95 = target_budget / price
        assert shares_at_95 >= min_order_size   # clears the SAME floor unaided
        opp = self._convergence_leg_at_target_budget(price, target_budget, bankroll, "CV-REAL-95")

        book = OrderBook("tok-CV-REAL-95", min_order_size=min_order_size)
        sized = size_opportunities([opp], bankroll=bankroll, cash=1000,
                                   strategy_exposure={}, total_exposure=0,
                                   books={"tok-CV-REAL-95": book})
        assert sized and sized[0].legs[0].shares == pytest.approx(shares_at_95)

    # ---- POLYEDGE_MIN_ORDER_SIZE_MARGIN_PCT: a SECOND real rejection.
    # CV-3290748 (token
    # 15001217761569713800908278763658478996071325020582957014060305276373009757713)
    # failed "order couldn't be fully filled" seven times over six hours
    # against a CONFIRMED-DEEP book (51,917 shares resting at 0.999) -- not
    # a liquidity problem. At price 0.999, a $5 ticket computes to ~5.005
    # shares against min_order_size=5 -- risk.py's exact "<" comparison
    # (before this margin existed) judged that as already fine, yet the
    # real order still got rejected. Read from polymarket-client's actual
    # order-building source: for a max_price-protected BUY, the dollar
    # amount is floored (round DOWN) to whole cents BEFORE being divided
    # by price to get the share count -- a real, source-confirmed
    # mechanism that could shave a razor-thin margin below the floor,
    # though source alone couldn't fully rule out ordinary price drift
    # between sizing and execution as an alternate/contributing cause
    # (see live.py's new pre-order logging for that). Either way, a margin
    # applied to both the comparison and the bump target closes the gap.
    def test_cv3290748_regression_now_gets_bumped_instead_of_slipping_through(self):
        """The exact real case: price ~0.999, min_order_size 5, ~$5 ticket
        -> ~5.005 shares. Under the old exact "<" comparison this passed
        the check untouched and still failed for real. With the default
        2% margin it must now be bumped."""
        price = 0.999
        bankroll = 1000.0
        target_budget = 5.02   # the real "~$5 ticket"
        min_order_size = 5.0   # the real observed floor

        opp = self._convergence_leg_at_target_budget(price, target_budget, bankroll, "CV-3290748")
        shares_before_bump = target_budget / price
        assert 5.0 <= shares_before_bump <= 5.05   # the real razor-thin ~5.005-5.03 margin
        assert shares_before_bump >= min_order_size   # -- and the OLD exact check called it fine

        book = OrderBook("tok-CV-3290748", min_order_size=min_order_size)
        sized = size_opportunities([opp], bankroll=bankroll, cash=1000,
                                   strategy_exposure={}, total_exposure=0,
                                   books={"tok-CV-3290748": book})
        effective_min = min_order_size * (1.0 + config.MIN_ORDER_SIZE_MARGIN_PCT / 100.0)
        assert effective_min > shares_before_bump, \
            "test setup: the default margin must actually change the outcome here"
        assert sized and sized[0].legs[0].shares == pytest.approx(effective_min)
        assert sized[0].legs[0].shares >= min_order_size * 1.01   # comfortably clear, not razor-thin again

    def test_margin_pct_zero_reproduces_the_original_razor_thin_failure_mode(self):
        """Confirms the margin is what changed the outcome above -- with it
        explicitly disabled, CV-3290748's exact real numbers reproduce the
        original bug (passes the check untouched at ~5.005 shares)."""
        price = 0.999
        bankroll = 1000.0
        opp = self._convergence_leg_at_target_budget(price, 5.02, bankroll, "CV-3290748-NOMARGIN")
        book = OrderBook("tok-CV-3290748-NOMARGIN", min_order_size=5.0)
        old = config.MIN_ORDER_SIZE_MARGIN_PCT
        config.MIN_ORDER_SIZE_MARGIN_PCT = 0.0
        try:
            sized = size_opportunities([opp], bankroll=bankroll, cash=1000,
                                       strategy_exposure={}, total_exposure=0,
                                       books={"tok-CV-3290748-NOMARGIN": book})
            assert sized and sized[0].legs[0].shares == pytest.approx(5.02 / price)
        finally:
            config.MIN_ORDER_SIZE_MARGIN_PCT = old

    def test_skipped_entirely_when_bump_would_exceed_position_cap(self):
        bankroll = 1000.0
        price = 0.97
        opp = self._convergence_leg_at_target_budget(price, 5.02, bankroll, "CV-HUGE")
        huge_min_order_size = 100000.0   # nothing could bump this within any sane cap
        book = OrderBook("tok-CV-HUGE", min_order_size=huge_min_order_size)
        sized = size_opportunities([opp], bankroll=bankroll, cash=1000,
                                   strategy_exposure={}, total_exposure=0,
                                   books={"tok-CV-HUGE": book})
        assert sized == []

    def test_bump_disabled_via_config_skips_instead_of_bumping(self):
        bankroll = 1000.0
        price = 0.99
        opp = self._convergence_leg_at_target_budget(price, 5.02, bankroll, "CV-NOBUMP")
        min_order_size = 5.15   # same floor as the regression test -- bump WOULD fit in cap
        book = OrderBook("tok-CV-NOBUMP", min_order_size=min_order_size)
        old = config.MIN_ORDER_SIZE_BUMP
        config.MIN_ORDER_SIZE_BUMP = False
        try:
            sized = size_opportunities([opp], bankroll=bankroll, cash=1000,
                                       strategy_exposure={}, total_exposure=0,
                                       books={"tok-CV-NOBUMP": book})
            assert sized == []
        finally:
            config.MIN_ORDER_SIZE_BUMP = old

    def test_missing_book_or_min_order_size_is_a_noop(self):
        bankroll = 1000.0
        price = 0.99
        opp = self._convergence_leg_at_target_budget(price, 5.02, bankroll, "CV-NOBOOK")
        expected_shares = 5.02 / price
        # no books= at all, and a book present but with min_order_size=None
        for books in (None, {}, {"tok-CV-NOBOOK": OrderBook("tok-CV-NOBOOK")}):
            sized = size_opportunities([opp], bankroll=bankroll, cash=1000,
                                       strategy_exposure={}, total_exposure=0,
                                       books=books)
            assert sized and sized[0].legs[0].shares == pytest.approx(expected_shares)

    def test_longshot_range_price_never_triggers_the_min_order_size_check(self):
        """LONGSHOT-range price (3-5c): the same ~$5 ticket buys 100+
        shares, comfortably clearing any realistic min_order_size floor
        (1-5 shares seen in real examples) -- confirms the general fix
        doesn't change LONGSHOT behavior, since it was never broken here."""
        price = 0.04
        opp = Opportunity(strategy="LONGSHOT", key="LS-RANGE", title="LS-RANGE",
                          edge=0.10, guaranteed=False, est_p_win=0.97,
                          legs=[Leg("tok-ls", "m-ls", "NO x", "NO", price, 0.0)])
        book = OrderBook("tok-ls", min_order_size=5.0)   # top of the real 1-5 range
        sized = size_opportunities([opp], bankroll=1000, cash=1000,
                                   strategy_exposure={}, total_exposure=0,
                                   books={"tok-ls": book})
        assert sized
        assert sized[0].legs[0].shares >= 100.0

    # ---- leg.min_order_size: diagnostic only (not accounting), stashed so
    # live.py can log the real number a real order was actually sized
    # against without needing another manual book-fetch session.
    def test_leg_min_order_size_stashed_for_diagnostics(self):
        opp = self._convergence_leg_at_target_budget(0.999, 5.02, 1000.0, "CV-DIAG")
        book = OrderBook("tok-CV-DIAG", min_order_size=5.0)
        sized = size_opportunities([opp], bankroll=1000, cash=1000,
                                   strategy_exposure={}, total_exposure=0,
                                   books={"tok-CV-DIAG": book})
        assert sized and sized[0].legs[0].min_order_size == 5.0

    def test_leg_min_order_size_left_none_when_book_missing(self):
        opp = self._convergence_leg_at_target_budget(0.999, 5.02, 1000.0, "CV-NODIAG")
        sized = size_opportunities([opp], bankroll=1000, cash=1000,
                                   strategy_exposure={}, total_exposure=0, books=None)
        assert sized and sized[0].legs[0].min_order_size is None


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


# ------------------------------------------------------------------ geoblock check (IPv6 routing incident)
class _FakeGeoblockSession:
    """Routes GET to a canned geoblock response, or raises, so
    check_geoblock's real HTTP call never actually happens in tests."""
    def __init__(self, response=None, raise_error=None):
        self.response = response
        self.raise_error = raise_error
        self.calls = []

    def get(self, url, timeout=None):
        self.calls.append(url)
        if self.raise_error:
            raise self.raise_error
        return self.response


class TestGeoblockCheck:
    """Real production incident: a VPS's outbound HTTPS to polymarket.com
    resolved over IPv6 by default, and that specific IPv6 address
    geolocated to a Polymarket-blocked region even though the server's
    real (IPv4) location was fine -- confirmed directly: default `curl`
    returned blocked:true/country:DE, `curl -4` returned
    blocked:false/country:FI. Every live order was silently rejected with
    "Trading restricted in your region" until diagnosed by hand. See
    LIVE.md section 3."""

    def test_returns_parsed_json_when_not_blocked(self):
        from polyedge.api import check_geoblock
        session = _FakeGeoblockSession(
            response=_FakeResponse({"blocked": False, "country": "FI"}))
        result = check_geoblock(session=session)
        assert result == {"blocked": False, "country": "FI"}
        assert session.calls == ["https://polymarket.com/api/geoblock"]

    def test_regression_default_resolution_reports_blocked_de(self):
        """Literal regression case: the real reported values from the
        incident."""
        from polyedge.api import check_geoblock
        session = _FakeGeoblockSession(
            response=_FakeResponse({"blocked": True, "country": "DE"}))
        assert check_geoblock(session=session) == {"blocked": True, "country": "DE"}

    def test_returns_none_on_network_failure(self):
        from polyedge.api import check_geoblock
        session = _FakeGeoblockSession(raise_error=requests.ConnectionError("boom"))
        assert check_geoblock(session=session) is None

    def test_returns_none_on_http_error_status(self):
        from polyedge.api import check_geoblock
        session = _FakeGeoblockSession(response=_FakeResponse({}, status=503))
        assert check_geoblock(session=session) is None

    def test_force_ipv4_still_completes_the_request(self):
        """force_ipv4=True must not itself break a request that doesn't
        actually touch DNS (a fake session, here) -- the _force_ipv4()
        wrapper must be transparent to anything other than
        socket.getaddrinfo."""
        from polyedge.api import check_geoblock
        session = _FakeGeoblockSession(
            response=_FakeResponse({"blocked": False, "country": "FI"}))
        assert check_geoblock(session=session, force_ipv4=True) == \
            {"blocked": False, "country": "FI"}

    def test_force_ipv4_context_manager_filters_to_af_inet_only(self):
        import socket as _socket
        from polyedge.api import _force_ipv4
        fake_results = [
            (_socket.AF_INET, None, None, "", ("1.2.3.4", 443)),
            (_socket.AF_INET6, None, None, "", ("::1", 443, 0, 0)),
        ]
        orig = _socket.getaddrinfo
        _socket.getaddrinfo = lambda *a, **k: fake_results
        try:
            with _force_ipv4():
                filtered = _socket.getaddrinfo("polymarket.com", 443)
            assert filtered == [fake_results[0]]      # AF_INET6 entry dropped
            # restored to the (test-patched) original after the block
            assert _socket.getaddrinfo("polymarket.com", 443) == fake_results
        finally:
            _socket.getaddrinfo = orig

    def test_force_ipv4_context_manager_restores_even_on_exception(self):
        import socket as _socket
        from polyedge.api import _force_ipv4
        orig = _socket.getaddrinfo
        try:
            with pytest.raises(RuntimeError):
                with _force_ipv4():
                    assert _socket.getaddrinfo is not orig
                    raise RuntimeError("boom mid-request")
            assert _socket.getaddrinfo is orig
        finally:
            _socket.getaddrinfo = orig


class TestGeoblockStartupCheck:
    """run_forever.py's startup gate -- refuses to start live trading
    when the geoblock check confirms this machine is region-blocked,
    instead of silently failing every live order the way the real
    incident did. See LIVE.md section 3."""

    def _run_forever(self):
        import run_forever
        return run_forever

    def test_passes_when_not_blocked(self, monkeypatch):
        rf = self._run_forever()
        import polyedge.api as api_mod
        monkeypatch.setattr(api_mod, "check_geoblock",
                            lambda force_ipv4=False: {"blocked": False, "country": "FI"})
        assert rf._geoblock_startup_check_passes() is True

    def test_passes_when_check_cannot_complete_at_all(self, monkeypatch):
        """A startup network hiccup is not, by itself, proof of
        geoblocking -- must not block startup on its own."""
        rf = self._run_forever()
        import polyedge.api as api_mod
        monkeypatch.setattr(api_mod, "check_geoblock", lambda force_ipv4=False: None)
        assert rf._geoblock_startup_check_passes() is True

    def test_regression_ipv6_routing_mismatch_refuses_to_start(self, monkeypatch):
        """The literal real incident: default resolution blocked (DE),
        forced IPv4 not blocked (FI) -- must refuse to start."""
        rf = self._run_forever()
        import polyedge.api as api_mod

        def fake_check(force_ipv4=False):
            if force_ipv4:
                return {"blocked": False, "country": "FI"}
            return {"blocked": True, "country": "DE"}
        monkeypatch.setattr(api_mod, "check_geoblock", fake_check)
        assert rf._geoblock_startup_check_passes() is False

    def test_blocked_even_over_ipv4_also_refuses_to_start(self, monkeypatch):
        """A genuine account/region restriction (not the IPv6 routing
        issue, since IPv4 is blocked too) must still refuse to start --
        just with a different diagnostic message."""
        rf = self._run_forever()
        import polyedge.api as api_mod
        monkeypatch.setattr(api_mod, "check_geoblock",
                            lambda force_ipv4=False: {"blocked": True, "country": "DE"})
        assert rf._geoblock_startup_check_passes() is False

    def test_default_check_fails_but_forced_ipv4_reports_blocked_still_refuses(
            self, monkeypatch):
        """Edge case: the default-resolution check itself couldn't
        complete (e.g. a connection-level failure over the misrouted
        IPv6 path), but the forced-IPv4 check DID complete and reports
        blocked=True -- must still refuse to start rather than treating
        an unrelated None result as "not blocked"."""
        rf = self._run_forever()
        import polyedge.api as api_mod

        def fake_check(force_ipv4=False):
            return {"blocked": True, "country": "DE"} if force_ipv4 else None
        monkeypatch.setattr(api_mod, "check_geoblock", fake_check)
        assert rf._geoblock_startup_check_passes() is False

    def test_only_runs_when_live_engine_selected(self, tmp_path, monkeypatch):
        """Paper trading never places a real order, so it must not be
        gated by (or even call) the geoblock check at all."""
        import run_forever
        import polyedge.api as api_mod
        calls = []
        monkeypatch.setattr(api_mod, "check_geoblock",
                            lambda force_ipv4=False: calls.append(force_ipv4) or
                            {"blocked": True, "country": "DE"})
        monkeypatch.delenv("POLYEDGE_LIVE", raising=False)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(run_forever, "_stop", True)   # exit the loop immediately
        run_forever.main()
        assert calls == []


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
        # no live network in tests -- None means "refresh unavailable, fall
        # back to leg.entry_price", exactly like a real failed fetch would;
        # see TestLivePriceRefresh below for the refresh behavior itself
        e._fetch_fresh_ask = lambda token_id: None
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

    def test_default_stop_loss_auto_applied_on_fill(self, tmp_path, monkeypatch):
        """The actual point of this feature: a live position gets a
        standing stop-loss the moment it opens, with no separate manual
        step in the control panel needed."""
        from polyedge import controls
        e = self._engine(tmp_path, monkeypatch)
        pos = e.open_position(self._opp("CV-SL"))
        assert pos is not None
        ctrl = controls.load(e.state_dir)
        assert ctrl["stop_loss_pct"].get("CV-SL") == config.LIVE_DEFAULT_STOP_LOSS_PCT

    def test_default_stop_loss_disabled_when_zero(self, tmp_path, monkeypatch):
        from polyedge import controls
        e = self._engine(tmp_path, monkeypatch)
        old = config.LIVE_DEFAULT_STOP_LOSS_PCT
        config.LIVE_DEFAULT_STOP_LOSS_PCT = 0
        try:
            pos = e.open_position(self._opp("CV-NOSL"))
            assert pos is not None
            ctrl = controls.load(e.state_dir)
            assert "CV-NOSL" not in ctrl["stop_loss_pct"]
        finally:
            config.LIVE_DEFAULT_STOP_LOSS_PCT = old

    def test_no_stop_loss_registered_when_order_not_filled(self, tmp_path, monkeypatch):
        from polyedge import controls
        e = self._engine(tmp_path, monkeypatch, fill=False)
        assert e.open_position(self._opp("CV-UNFILLED")) is None
        ctrl = controls.load(e.state_dir)
        assert "CV-UNFILLED" not in ctrl["stop_loss_pct"]

    # ---- control-panel-adjustable default stop-loss (controls.
    # default_stop_loss_pct), overriding config.LIVE_DEFAULT_STOP_LOSS_PCT
    def test_control_panel_default_stop_loss_overrides_config(self, tmp_path, monkeypatch):
        from polyedge import controls
        e = self._engine(tmp_path, monkeypatch)
        controls.set_default_stop_loss_pct(55, e.state_dir)
        pos = e.open_position(self._opp("CV-OVERRIDE"))
        assert pos is not None
        ctrl = controls.load(e.state_dir)
        assert ctrl["stop_loss_pct"]["CV-OVERRIDE"] == 55.0

    def test_falls_back_to_config_when_no_control_panel_override_set(self, tmp_path, monkeypatch):
        from polyedge import controls
        e = self._engine(tmp_path, monkeypatch)
        assert controls.load(e.state_dir)["default_stop_loss_pct"] is None  # never set
        pos = e.open_position(self._opp("CV-FALLBACK"))
        assert pos is not None
        ctrl = controls.load(e.state_dir)
        assert ctrl["stop_loss_pct"]["CV-FALLBACK"] == config.LIVE_DEFAULT_STOP_LOSS_PCT

    def test_control_panel_default_change_is_not_retroactive(self, tmp_path, monkeypatch):
        """The task's explicit requirement: changing the slider must only
        affect positions opened AFTER the change, never rewrite the
        stop-loss already snapshotted onto a currently-open position."""
        from polyedge import controls
        e = self._engine(tmp_path, monkeypatch)
        controls.set_default_stop_loss_pct(20, e.state_dir)
        assert e.open_position(self._opp("CV-BEFORE")) is not None
        controls.set_default_stop_loss_pct(70, e.state_dir)   # slider moved later
        assert e.open_position(self._opp("CV-AFTER")) is not None
        ctrl = controls.load(e.state_dir)
        assert ctrl["stop_loss_pct"]["CV-BEFORE"] == 20.0   # untouched by the later change
        assert ctrl["stop_loss_pct"]["CV-AFTER"] == 70.0    # picked up the new default

    # ---- rejected_cooldown: real production incident where a token whose
    # FOK order kept getting rejected was re-selected as the best candidate
    # cycle after cycle for over an hour. Uses the EXACT real token_id/
    # opportunity key from that incident as a literal regression case.
    _REAL_REJECTED_TOKEN_ID = ("24179142020748386308900785500935095928275106"
                               "841229237369829185074319749593959")

    def test_rejected_cooldown_recorded_on_unfilled_buy_regression_real_token(
            self, tmp_path, monkeypatch):
        import time as _t
        e = self._engine(tmp_path, monkeypatch, fill=False)
        opp = self._opp("LS-3144825", "LONGSHOT",
                        legs=[Leg(self._REAL_REJECTED_TOKEN_ID, "m1", "NO x",
                                 "NO", 0.95, 10.0)])
        before = _t.time()
        assert e.open_position(opp) is None
        assert self._REAL_REJECTED_TOKEN_ID in e.rejected_cooldown
        assert e.rejected_cooldown[self._REAL_REJECTED_TOKEN_ID] >= before

    def test_rejected_cooldown_not_recorded_on_successful_fill(self, tmp_path, monkeypatch):
        e = self._engine(tmp_path, monkeypatch, fill=True)
        opp = self._opp("LS-3144825", "LONGSHOT",
                        legs=[Leg(self._REAL_REJECTED_TOKEN_ID, "m1", "NO x",
                                 "NO", 0.95, 10.0)])
        assert e.open_position(opp) is not None
        assert e.rejected_cooldown == {}

    # ---- dead-market cooldown: confirmed production root cause -- two real
    # tokens whose markets had genuinely resolved kept getting re-selected
    # as candidates and re-attempted over a 10+ hour stretch, each attempt
    # failing with "No orderbook exists for the requested token id"
    # (confirmed directly, twice, via the CLOB /book endpoint) -- and the
    # normal 30-minute rejected_cooldown was nowhere near long enough for
    # a market that is never coming back. Literal regression cases using
    # the exact real token ids from that incident. `_last_rejection_no_
    # orderbook` is set directly (rather than driving this through the
    # real SDK) since _place_order is overridden wholesale here, same as
    # every other test in this class -- see live.py's _is_no_orderbook_
    # error for the message-matching logic that would set this in
    # production, covered separately in TestLiveEnginePolymarketClientWiring.
    _REAL_DEAD_TOKEN_IDS = (
        "94614398813432851798269018361285084876491745849461170982859277819931464993110",
        "61878077034939631499810076559734026691334391752935602121591808349124991762735",
    )

    @pytest.mark.parametrize("dead_token", _REAL_DEAD_TOKEN_IDS)
    def test_no_orderbook_rejection_triggers_long_dead_market_cooldown(
            self, tmp_path, monkeypatch, dead_token):
        import time as _t
        e = self._engine(tmp_path, monkeypatch, fill=False)
        e._last_rejection_no_orderbook = True
        opp = self._opp("LS-DEAD", "LONGSHOT",
                        legs=[Leg(dead_token, "m1", "NO x", "NO", 0.95, 10.0)])
        before = _t.time()
        assert e.open_position(opp) is None
        assert dead_token in e.rejected_cooldown
        expiry = e.rejected_cooldown[dead_token]
        assert expiry == pytest.approx(
            before + config.LIVE_DEAD_MARKET_COOLDOWN_MIN * 60, abs=5)

    def test_no_orderbook_cooldown_is_longer_than_ordinary_rejection_cooldown(
            self, tmp_path, monkeypatch):
        """The core requirement: a confirmed dead market must be
        blacklisted for much longer than a merely-rejected one."""
        dead_token, ordinary_token = self._REAL_DEAD_TOKEN_IDS[0], "tok-ordinary"
        e = self._engine(tmp_path, monkeypatch, fill=False)

        e._last_rejection_no_orderbook = True
        e.open_position(self._opp("LS-DEAD", "LONGSHOT",
                                  legs=[Leg(dead_token, "m1", "NO x", "NO",
                                           0.95, 10.0)]))

        e._last_rejection_no_orderbook = False
        e.open_position(self._opp("LS-ORDINARY", "LONGSHOT",
                                  legs=[Leg(ordinary_token, "m2", "NO y", "NO",
                                           0.95, 10.0)]))

        assert config.LIVE_DEAD_MARKET_COOLDOWN_MIN > config.LIVE_REJECTED_COOLDOWN_MIN
        assert e.rejected_cooldown[dead_token] > e.rejected_cooldown[ordinary_token]

    def test_no_orderbook_cooldown_respects_a_later_shortened_config_value(
            self, tmp_path, monkeypatch):
        """Task requirement: a token blacklisted under the dead-market
        cooldown must not get permanently stuck if the config value is
        later shortened -- the expiry is computed once, at write time,
        from whatever the config said then, not re-derived at read time
        from whatever the config says NOW."""
        import time as _t
        dead_token = self._REAL_DEAD_TOKEN_IDS[0]
        e = self._engine(tmp_path, monkeypatch, fill=False)
        e._last_rejection_no_orderbook = True
        old = config.LIVE_DEAD_MARKET_COOLDOWN_MIN
        config.LIVE_DEAD_MARKET_COOLDOWN_MIN = 1.0   # 1 minute, way shorter
        try:
            opp = self._opp("LS-DEAD", "LONGSHOT",
                            legs=[Leg(dead_token, "m1", "NO x", "NO", 0.95, 10.0)])
            assert e.open_position(opp) is None
            expiry = e.rejected_cooldown[dead_token]
        finally:
            config.LIVE_DEAD_MARKET_COOLDOWN_MIN = old
        # respects the shortened 1-minute window -- nowhere near the old 6h default
        assert expiry <= _t.time() + 90

        # and risk.py's reader correctly treats a since-expired dead-market
        # entry as no longer in cooldown, same as any other expired entry
        opp2 = Opportunity(strategy="LONGSHOT", key="LS-DEAD-2", title="t",
                           edge=0.1, guaranteed=False, est_p_win=0.97,
                           legs=[Leg(dead_token, "m1", "NO x", "NO", 0.95, 0.0)])
        expired_cooldown = {dead_token: _t.time() - 1}
        sized = size_opportunities([opp2], bankroll=1000, cash=1000,
                                   strategy_exposure={}, total_exposure=0,
                                   rejected_cooldown=expired_cooldown)
        assert sized and sized[0].key == "LS-DEAD-2"

    # ---- pre-order logging: a real rejection (CV-3290748) happened at a
    # razor-thin margin risk.py's own check judged as fine, and diagnosing
    # it required a manual book-fetch-and-reason-through-it session. The
    # next one should be diagnosable from a single log line instead.
    def test_order_inputs_logged_before_placement_including_min_order_size(
            self, tmp_path, monkeypatch, caplog):
        import logging as _logging
        e = self._engine(tmp_path, monkeypatch, fill=True)
        # 5.02 shares (not the razor-thin 5.005), so PaperEngine's OWN
        # MIN_TICKET check (cost >= $5.00) doesn't separately reject this --
        # unrelated to what this test is verifying (the log line's content)
        leg = Leg("tok-CV-3290748", "m1", "YES q", "YES", 0.999, 5.02)
        leg.min_order_size = 5.0   # what risk.py would have stashed
        opp = self._opp("CV-3290748", "CONVERGE", legs=[leg])
        with caplog.at_level(_logging.INFO, logger="polyedge.live"):
            assert e.open_position(opp) is not None
        msgs = [r.message for r in caplog.records]
        assert any("tok-CV-3290748" in m and "min_order_size=5.0" in m
                  and "0.999" in m for m in msgs)

    def test_not_filled_warning_includes_price_shares_and_min_order_size(
            self, tmp_path, monkeypatch, caplog):
        import logging as _logging
        e = self._engine(tmp_path, monkeypatch, fill=False)
        leg = Leg("tok-CV-3290748", "m1", "YES q", "YES", 0.999, 5.005)
        leg.min_order_size = 5.0
        opp = self._opp("CV-3290748", "CONVERGE", legs=[leg])
        with caplog.at_level(_logging.WARNING, logger="polyedge.live"):
            assert e.open_position(opp) is None
        msgs = [r.message for r in caplog.records]
        assert any("not filled" in m and "tok-CV-3290748" in m
                  and "min_order_size=5.0" in m for m in msgs)

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


# ------------------------------------------------------------------ live.py polymarket-client wiring
class TestLiveEnginePolymarketClientWiring:
    """Covers the polymarket-client (AsyncSecureClient) switch specifically:
    client construction (private_key/environment/api_key), the FOK
    market-order call shape, fill determination from AcceptedOrder/
    RejectedOrder, and the pUSD balance pre-trade check -- all against a
    fake `polymarket` package injected into sys.modules, so nothing here
    needs the real package installed or touches the network. Does NOT
    touch the three-gate safety logic itself (see TestLiveEngine above,
    which passes unmodified)."""

    def _install_fake_sdk(self, monkeypatch, order_response=None, trades=(),
                          trades_error=None):
        import sys
        import types

        calls = {"create_kwargs": None, "setup_trading_approvals_calls": 0,
                 "place_market_order_kwargs": None, "closed": False,
                 "list_account_trades_kwargs": []}
        resp = order_response if order_response is not None else \
            _FakeAcceptedOrder("matched")

        class FakeBuilderApiKey:
            def __init__(self, key, secret, passphrase):
                self.key, self.secret, self.passphrase = key, secret, passphrase

        class FakeAsyncSecureClient:
            @classmethod
            async def create(cls, **kwargs):
                calls["create_kwargs"] = kwargs
                return cls()

            async def setup_trading_approvals(self):
                calls["setup_trading_approvals_calls"] += 1

            async def place_market_order(self, **kwargs):
                calls["place_market_order_kwargs"] = kwargs
                if isinstance(resp, Exception):
                    raise resp
                return resp

            def list_account_trades(self, **kwargs):
                calls["list_account_trades_kwargs"].append(kwargs)
                return _FakeTradesPaginator(trades, error=trades_error)

            async def close(self):
                calls["closed"] = True

        polymarket_mod = types.ModuleType("polymarket")
        polymarket_mod.PRODUCTION = "PRODUCTION"
        polymarket_mod.AsyncSecureClient = FakeAsyncSecureClient
        polymarket_mod.BuilderApiKey = FakeBuilderApiKey
        monkeypatch.setitem(sys.modules, "polymarket", polymarket_mod)

        errors_mod = types.ModuleType("polymarket.errors")
        errors_mod.PolymarketError = _FakePolymarketError
        monkeypatch.setitem(sys.modules, "polymarket.errors", errors_mod)
        return calls

    def _engine(self, tmp_path, monkeypatch, key="0xabc", funder="0xdef",
               builder_creds=("bkey", "bsecret", "bpass")):
        from polyedge import live as lv
        monkeypatch.chdir(tmp_path)
        if key is not None:
            monkeypatch.setenv("POLYEDGE_PRIVATE_KEY", key)
        else:
            monkeypatch.delenv("POLYEDGE_PRIVATE_KEY", raising=False)
        if funder is not None:
            monkeypatch.setenv("POLYEDGE_FUNDER_ADDRESS", funder)
        else:
            monkeypatch.delenv("POLYEDGE_FUNDER_ADDRESS", raising=False)
        if builder_creds is not None:
            bkey, bsecret, bpass = builder_creds
            monkeypatch.setenv("POLYEDGE_BUILDER_API_KEY", bkey)
            monkeypatch.setenv("POLYEDGE_BUILDER_SECRET", bsecret)
            monkeypatch.setenv("POLYEDGE_BUILDER_PASSPHRASE", bpass)
        else:
            monkeypatch.delenv("POLYEDGE_BUILDER_API_KEY", raising=False)
            monkeypatch.delenv("POLYEDGE_BUILDER_SECRET", raising=False)
            monkeypatch.delenv("POLYEDGE_BUILDER_PASSPHRASE", raising=False)
        e = lv.LiveEngine(state_dir=str(tmp_path))
        # no live network in tests -- see TestLiveEngine._engine's identical
        # comment; only the two open_position()-driving tests below reach
        # this at all, everything else in this class calls _place_order/
        # _aplace_order directly against the fake SDK
        e._fetch_fresh_ask = lambda token_id: None
        return e

    def test_client_created_with_private_key_environment_and_builder_api_key(self, tmp_path, monkeypatch):
        calls = self._install_fake_sdk(monkeypatch)
        e = self._engine(tmp_path, monkeypatch)
        e._place_order("tok-1", 0.965, 10.0, "BUY")
        sent = calls["create_kwargs"]
        assert sent["private_key"] == "0xabc"
        assert sent["environment"] == "PRODUCTION"
        assert sent["api_key"].key == "bkey"
        assert sent["api_key"].secret == "bsecret"
        assert sent["api_key"].passphrase == "bpass"
        # no signature_type/chain_id knob -- AsyncSecureClient.create()
        # has no such parameter (confirmed from source, see live.py's
        # module docstring)
        assert "signature_type" not in sent and "chain_id" not in sent

    def test_missing_builder_credentials_raises(self, tmp_path, monkeypatch):
        self._install_fake_sdk(monkeypatch)
        e = self._engine(tmp_path, monkeypatch, builder_creds=None)
        with pytest.raises(RuntimeError, match="POLYEDGE_BUILDER_API_KEY"):
            e._place_order("tok-1", 0.965, 10.0, "BUY")

    def test_setup_trading_approvals_called_before_every_order(self, tmp_path, monkeypatch):
        calls = self._install_fake_sdk(monkeypatch)
        e = self._engine(tmp_path, monkeypatch)
        e._place_order("tok-1", 0.965, 10.0, "BUY")
        assert calls["setup_trading_approvals_calls"] == 1

    def test_client_closed_after_a_successful_order(self, tmp_path, monkeypatch):
        calls = self._install_fake_sdk(monkeypatch)
        e = self._engine(tmp_path, monkeypatch)
        e._place_order("tok-1", 0.965, 10.0, "BUY")
        assert calls["closed"] is True

    def test_client_closed_even_when_the_order_call_raises(self, tmp_path, monkeypatch):
        calls = self._install_fake_sdk(monkeypatch, order_response=RuntimeError("boom"))
        e = self._engine(tmp_path, monkeypatch)
        with pytest.raises(RuntimeError, match="boom"):
            e._place_order("tok-1", 0.965, 10.0, "BUY")
        assert calls["closed"] is True

    # ---- a real production bug: place_market_order() RAISED for a FOK
    # order that couldn't fill (RequestRejectedError), instead of coming
    # back as a RejectedOrder response object. With no handling, that
    # exception propagated all the way up through open_position() and
    # crashed the whole run_cycle() -- not just the one rejected order,
    # every other queued opportunity that cycle too. _aplace_order now
    # catches the SDK's whole PolymarketError hierarchy around that call.
    def test_request_rejected_error_caught_and_returns_false(self, tmp_path, monkeypatch):
        calls = self._install_fake_sdk(
            monkeypatch,
            order_response=_FakeRequestRejectedError("no liquidity", status=400))
        e = self._engine(tmp_path, monkeypatch)
        assert e._place_order("tok-1", 0.965, 10.0, "BUY") is False
        # graceful failure, not a crash -- the client is still closed
        assert calls["closed"] is True

    # ---- "no orderbook exists": a DIFFERENT confirmed production failure
    # mode from ordinary FOK rejections above -- two real tokens whose
    # markets had genuinely resolved kept getting re-selected as
    # candidates and re-attempted over a 10+ hour stretch, real order
    # rejected every time with this exact message. It does not surface as
    # its own exception type (confirmed from polymarket-client's source --
    # see live.py's _is_no_orderbook_error), just a RequestRejectedError
    # whose message happens to say this. Exercises the REAL _aplace_order
    # detection path end-to-end (not a directly-set flag, unlike
    # TestLiveEngine's cooldown-duration tests), through open_position()
    # so the resulting cooldown is genuinely long.
    _REAL_DEAD_TOKEN_ID = ("946143988134328517982690183612850848764917458"
                          "49461170982859277819931464993110")

    def test_no_orderbook_message_detected_and_returns_false(self, tmp_path, monkeypatch):
        self._install_fake_sdk(
            monkeypatch,
            order_response=_FakeRequestRejectedError(
                "No orderbook exists for the requested token id", status=404))
        e = self._engine(tmp_path, monkeypatch)
        assert e._place_order(self._REAL_DEAD_TOKEN_ID, 0.95, 10.0, "BUY") is False
        assert e._last_rejection_no_orderbook is True

    def test_no_orderbook_end_to_end_records_the_long_cooldown(self, tmp_path, monkeypatch):
        import time as _t
        from polyedge import live as lv
        monkeypatch.setenv("POLYEDGE_LIVE", "1")
        monkeypatch.setenv("POLYEDGE_DRY_RUN", "0")
        self._install_fake_sdk(
            monkeypatch,
            order_response=_FakeRequestRejectedError(
                "No orderbook exists for the requested token id", status=404))
        e = self._engine(tmp_path, monkeypatch)
        open(lv.ARMED_FILE, "w").write("armed")
        e._check_pusd_balance = lambda: True
        opp = Opportunity("LONGSHOT", "LS-DEAD", "LS-DEAD", 0.10, False,
                          est_p_win=0.97,
                          legs=[Leg(self._REAL_DEAD_TOKEN_ID, "m1", "NO x", "NO",
                                   0.95, 10.0)],
                          resolve_by="2026-07-21T00:00:00Z")
        before = _t.time()
        assert e.open_position(opp) is None
        expiry = e.rejected_cooldown[self._REAL_DEAD_TOKEN_ID]
        assert expiry == pytest.approx(
            before + config.LIVE_DEAD_MARKET_COOLDOWN_MIN * 60, abs=5)
        assert expiry > before + config.LIVE_REJECTED_COOLDOWN_MIN * 60

    def test_other_polymarket_error_subclasses_also_caught_not_just_request_rejected(
            self, tmp_path, monkeypatch):
        # catching only RequestRejectedError (the one class actually
        # observed in production) would still leave this class of crash
        # open for every OTHER PolymarketError subclass -- the fix must
        # catch the base class, not one hardcoded sibling
        calls = self._install_fake_sdk(
            monkeypatch, order_response=_FakeInsufficientLiquidityError("no liquidity"))
        e = self._engine(tmp_path, monkeypatch)
        assert e._place_order("tok-1", 0.965, 10.0, "SELL") is False
        assert calls["closed"] is True

    def test_polymarket_error_does_not_abort_the_whole_open_position_call(self, tmp_path, monkeypatch):
        """The actual point of the fix: one rejected leg must not raise
        out of open_position() and abort processing of other opportunities
        in the same run_cycle(). Exercises the real _place_order/
        _aplace_order path (not mocked out, unlike TestLiveEngine) with
        the gates open, same as a real live cycle would."""
        from polyedge import live as lv
        monkeypatch.setenv("POLYEDGE_LIVE", "1")
        monkeypatch.setenv("POLYEDGE_DRY_RUN", "0")
        self._install_fake_sdk(monkeypatch, order_response=_FakeRequestRejectedError("rejected"))
        e = self._engine(tmp_path, monkeypatch)   # chdir(tmp_path) happens here
        open(lv.ARMED_FILE, "w").write("armed")
        e._check_pusd_balance = lambda: True
        opp = Opportunity("CONVERGE", "CV-X", "CV-X", 0.04, False, est_p_win=0.98,
                          legs=[Leg("tok-1", "m1", "YES q", "YES", 0.96, 10.0)],
                          resolve_by="2026-07-21T00:00:00Z")
        # open_position must return None (order not filled) rather than
        # letting the SDK exception propagate out of this call
        assert e.open_position(opp) is None

    def test_buy_sends_fok_market_order_with_amount_and_max_price(self, tmp_path, monkeypatch):
        calls = self._install_fake_sdk(monkeypatch)
        e = self._engine(tmp_path, monkeypatch)
        filled = e._place_order("tok-1", 0.965, 10.0, "BUY")
        assert filled is True
        sent = calls["place_market_order_kwargs"]
        assert sent["token_id"] == "tok-1"
        assert sent["side"] == "BUY"
        assert sent["order_type"] == "FOK"
        assert sent["amount"] == pytest.approx(9.65)     # price * shares
        assert sent["max_price"] == pytest.approx(0.965)
        assert "shares" not in sent and "min_price" not in sent

    def test_order_inputs_after_rounding_are_logged_before_submission(self, tmp_path, monkeypatch, caplog):
        """Task: the next real min_order_size-margin rejection should show
        exact numbers from logs alone, not require another manual
        book-fetch-and-reason-through-it session."""
        import logging as _logging
        self._install_fake_sdk(monkeypatch)
        e = self._engine(tmp_path, monkeypatch)
        with caplog.at_level(_logging.INFO, logger="polyedge.live"):
            e._place_order("tok-CV-3290748", 0.999, 5.005, "BUY")
        msgs = [r.message for r in caplog.records]
        assert any("order inputs after local rounding" in m and "tok-CV-3290748" in m
                  and "price=0.999000" in m for m in msgs)

    def test_sell_sends_fok_market_order_with_shares_and_min_price(self, tmp_path, monkeypatch):
        calls = self._install_fake_sdk(monkeypatch)
        e = self._engine(tmp_path, monkeypatch)
        filled = e._place_order("tok-1", 0.90, 10.0, "SELL")
        assert filled is True
        sent = calls["place_market_order_kwargs"]
        assert sent["side"] == "SELL"
        assert sent["order_type"] == "FOK"
        assert sent["shares"] == pytest.approx(10.0)
        assert sent["min_price"] == pytest.approx(0.90)
        assert "amount" not in sent and "max_price" not in sent

    def test_place_order_true_when_accepted_and_matched(self, tmp_path, monkeypatch):
        self._install_fake_sdk(monkeypatch, order_response=_FakeAcceptedOrder("matched"))
        e = self._engine(tmp_path, monkeypatch)
        assert e._place_order("tok-1", 0.965, 10.0, "BUY") is True

    def test_place_order_false_when_rejected_fok_not_filled(self, tmp_path, monkeypatch):
        self._install_fake_sdk(monkeypatch, order_response=_FakeRejectedOrder("fok_not_filled"))
        e = self._engine(tmp_path, monkeypatch)
        assert e._place_order("tok-1", 0.965, 10.0, "SELL") is False

    def test_place_order_false_when_accepted_but_status_live(self, tmp_path, monkeypatch):
        # defensive case -- a FOK order should never come back resting
        # ("live") per source, but _place_order must not treat it as filled
        self._install_fake_sdk(monkeypatch, order_response=_FakeAcceptedOrder("live"))
        e = self._engine(tmp_path, monkeypatch)
        assert e._place_order("tok-1", 0.965, 10.0, "BUY") is False

    # ---- status="delayed": a REAL production bug. An order placed against
    # this account returned AcceptedOrder(ok=True, status='delayed',
    # making_amount=0, taking_amount=0, trade_ids=(), transactions_hashes=())
    # and was independently confirmed to have actually filled -- under the
    # original status=="matched"-only check this real fill would have been
    # (and was) treated as NOT filled. _resolve_fill polls
    # list_account_trades() for a trade whose taker_order_id matches the
    # order instead of trusting the synchronous response for this status.
    def test_delayed_status_polls_and_confirms_fill_via_matching_trade(self, tmp_path, monkeypatch):
        from polyedge import live as lv
        monkeypatch.setattr(lv, "_DELAYED_FILL_POLL_DELAY_S", 0)
        resp = _FakeAcceptedOrder("delayed", order_id="0xorder-1")
        calls = self._install_fake_sdk(monkeypatch, order_response=resp,
                                       trades=[_FakeTrade("0xorder-1")])
        e = self._engine(tmp_path, monkeypatch)
        assert e._place_order("tok-1", 0.965, 10.0, "BUY") is True
        assert calls["list_account_trades_kwargs"][0]["token_id"] == "tok-1"

    def test_delayed_status_regression_real_production_response(self, tmp_path, monkeypatch):
        """The exact real response captured tonight -- verbatim field
        values, not a synthetic guess -- as a regression case."""
        from decimal import Decimal
        from polyedge import live as lv
        monkeypatch.setattr(lv, "_DELAYED_FILL_POLL_DELAY_S", 0)

        class _RealAcceptedOrderRepro:
            ok = True
            order_id = ("0x797207817e6152494a283e8b981e343856b19d872d04c2f0"
                       "71c0575273c01c4f")
            status = "delayed"
            making_amount = Decimal("0")
            taking_amount = Decimal("0")
            trade_ids = ()
            transactions_hashes = ()

        resp = _RealAcceptedOrderRepro()
        self._install_fake_sdk(monkeypatch, order_response=resp,
                               trades=[_FakeTrade(resp.order_id)])
        e = self._engine(tmp_path, monkeypatch)
        assert e._place_order("tok-1", 0.965, 10.0, "BUY") is True

    def test_delayed_status_no_matching_trade_returns_false_after_polling(self, tmp_path, monkeypatch):
        from polyedge import live as lv
        monkeypatch.setattr(lv, "_DELAYED_FILL_POLL_DELAY_S", 0)
        resp = _FakeAcceptedOrder("delayed", order_id="0xorder-1")
        # a trade for a DIFFERENT order is present -- must not false-match
        calls = self._install_fake_sdk(monkeypatch, order_response=resp,
                                       trades=[_FakeTrade("0xsome-other-order")])
        e = self._engine(tmp_path, monkeypatch)
        assert e._place_order("tok-1", 0.965, 10.0, "BUY") is False
        assert len(calls["list_account_trades_kwargs"]) == lv._DELAYED_FILL_POLL_ATTEMPTS

    def test_delayed_status_poll_error_logged_and_treated_as_not_filled(self, tmp_path, monkeypatch):
        from polyedge import live as lv
        monkeypatch.setattr(lv, "_DELAYED_FILL_POLL_DELAY_S", 0)
        resp = _FakeAcceptedOrder("delayed", order_id="0xorder-1")
        self._install_fake_sdk(monkeypatch, order_response=resp,
                               trades_error=RuntimeError("network blip"))
        e = self._engine(tmp_path, monkeypatch)
        assert e._place_order("tok-1", 0.965, 10.0, "BUY") is False

    def test_delayed_status_without_order_id_returns_false(self, tmp_path, monkeypatch):
        # defensive: can't poll for a trade without an order_id to match on
        self._install_fake_sdk(monkeypatch,
                               order_response=_FakeAcceptedOrder("delayed", order_id=""))
        e = self._engine(tmp_path, monkeypatch)
        assert e._place_order("tok-1", 0.965, 10.0, "BUY") is False

    # ---- _check_pusd_balance(): rewritten to a direct on-chain read after
    # a real bug was found -- get_balance_allowance() returned balance=0,
    # allowance=0 for a signature_type=1 (POLY_PROXY) funder wallet that
    # genuinely held pUSD. It sends no funder/address at all (only
    # signature_type, sourced from the client's own construction-time
    # value), so there was no parameter to "pass through" that would have
    # fixed it -- see the method's docstring and LIVE.md for the full
    # writeup and the tracking issue. These tests patch
    # reconcile.fetch_real_pusd_balance (what _check_pusd_balance now
    # actually calls) rather than the SDK.
    def test_pusd_check_true_and_passes_funder_through_correctly(self, tmp_path, monkeypatch):
        from polyedge import reconcile
        captured = {}

        def fake_fetch(funder_address, session=None):
            captured["funder"] = funder_address
            return 111.50

        monkeypatch.setattr(reconcile, "fetch_real_pusd_balance", fake_fetch)
        e = self._engine(tmp_path, monkeypatch, funder="0xProxyWalletHoldingRealFunds")
        assert e._check_pusd_balance() is True
        # the actual bug: the funder/proxy address, not the EOA signer
        # (key="0xabc" in _engine's default), must be what gets checked
        assert captured["funder"] == "0xProxyWalletHoldingRealFunds"

    def test_pusd_check_false_on_zero_balance(self, tmp_path, monkeypatch):
        from polyedge import reconcile
        monkeypatch.setattr(reconcile, "fetch_real_pusd_balance",
                            lambda funder_address, session=None: 0.0)
        e = self._engine(tmp_path, monkeypatch)
        assert e._check_pusd_balance() is False

    def test_pusd_check_false_when_rpc_unreachable(self, tmp_path, monkeypatch):
        # fetch_real_pusd_balance returns None (not 0.0) on network failure --
        # must be treated as "cannot verify, refuse to trade", not as "fine"
        from polyedge import reconcile
        monkeypatch.setattr(reconcile, "fetch_real_pusd_balance",
                            lambda funder_address, session=None: None)
        e = self._engine(tmp_path, monkeypatch)
        assert e._check_pusd_balance() is False

    def test_pusd_check_false_when_funder_not_set(self, tmp_path, monkeypatch):
        e = self._engine(tmp_path, monkeypatch, funder=None)
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


# ------------------------------------------------------------------ live price refresh (CV-3565421)
class TestLivePriceRefresh:
    """Confirmed production root cause: CV-3565421 (asset_id
    67630859659224760080054121446583082801681970585283198673478937279213434161100)
    failed to fill 4 times in one day (05:54, 07:19, 08:19, 08:59 UTC) at
    increasingly stale max_price ceilings (0.942, 0.960, 0.980, 0.978) --
    confirmed via direct book fetch that the market was genuinely deep and
    actively trading (64,593 shares resting at 0.999, last_trade_price
    0.986), not a dead market. leg.entry_price becomes the order's
    max_price ceiling verbatim (confirmed from polymarket-client's
    source), with zero cushion and zero re-fetch before this fix -- on a
    market trending toward $1 (CONVERGE's whole profile), the real ask
    can climb past a stale scan-time price before the order is actually
    submitted."""

    _CV_3565421_TOKEN = ("67630859659224760080054121446583082801681970585"
                        "283198673478937279213434161100")

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
        e._check_pusd_balance = lambda: True
        return e

    def _opp(self, key, strategy, token, entry_price, shares=10.0):
        return Opportunity(strategy, key, key, 0.04, False, est_p_win=0.98,
                           legs=[Leg(token, "m1", "YES q", "YES", entry_price, shares)],
                           resolve_by="2026-07-21T00:00:00Z")

    def test_cv_3565421_regression_uses_fresh_price_not_stale_scan_price(
            self, tmp_path, monkeypatch):
        """The literal regression case: the 4th real attempt's stale
        max_price (0.978) would still fail against the real market, which
        had climbed further by the time of submission (consistent with
        the confirmed last_trade_price of 0.986). The fresh re-fetch must
        replace the stale price before the order is built, or this
        continues failing exactly like production did."""
        e = self._engine(tmp_path, monkeypatch)
        e._fetch_fresh_ask = lambda token_id: 0.987
        opp = self._opp("CV-3565421", "CONVERGE", self._CV_3565421_TOKEN, 0.978)
        pos = e.open_position(opp)
        assert pos is not None
        _, token_id, _, submitted_price = e._orders[0]
        assert token_id == self._CV_3565421_TOKEN
        # submitted price must be the FRESH one (plus CONVERGE's cushion),
        # never the stale 0.978 that kept failing in production
        expected = round(min(0.999, 0.987 * (1 + config.CV_MAX_PRICE_CUSHION_PCT / 100)), 3)
        assert submitted_price == pytest.approx(expected)
        assert submitted_price != pytest.approx(0.978)

    def test_recorded_cost_basis_reflects_fresh_price(self, tmp_path, monkeypatch):
        """The position's recorded entry_price/cost must match what was
        actually risked, not the stale scan-time number -- otherwise
        paper accounting would silently understate real cash spent."""
        e = self._engine(tmp_path, monkeypatch)
        e._fetch_fresh_ask = lambda token_id: 0.987
        opp = self._opp("CV-3565421", "CONVERGE", self._CV_3565421_TOKEN, 0.978)
        pos = e.open_position(opp)
        expected = min(0.999, 0.987 * (1 + config.CV_MAX_PRICE_CUSHION_PCT / 100))
        assert pos["legs"][0]["entry_price"] == pytest.approx(expected, abs=1e-6)

    def test_cushion_only_applied_to_converge_not_longshot(self, tmp_path, monkeypatch):
        """LONGSHOT has no comparable persistent directional drift toward
        $1, so it gets the freshness fix but not the extra cushion."""
        e = self._engine(tmp_path, monkeypatch)
        e._fetch_fresh_ask = lambda token_id: 0.04
        opp = self._opp("LS-X", "LONGSHOT", "tok-ls", 0.038)
        e.open_position(opp)
        _, _, _, submitted_price = e._orders[0]
        assert submitted_price == pytest.approx(0.04)   # no cushion added

    def test_fetch_failure_falls_back_to_scan_time_price(self, tmp_path, monkeypatch):
        """A refresh failure must never block the order -- it just means
        falling back to the original scan-time price, exactly the
        behavior before this fix existed."""
        e = self._engine(tmp_path, monkeypatch)
        e._fetch_fresh_ask = lambda token_id: None
        opp = self._opp("CV-Y", "CONVERGE", "tok-cv-y", 0.96)
        pos = e.open_position(opp)
        assert pos is not None
        _, _, _, submitted_price = e._orders[0]
        assert submitted_price == pytest.approx(0.96)

    def test_favorable_price_move_downward_is_still_used(self, tmp_path, monkeypatch):
        """The refresh isn't a one-directional safety valve -- a price
        that moved DOWN since scan time is used too, not just up-moves."""
        e = self._engine(tmp_path, monkeypatch)
        e._fetch_fresh_ask = lambda token_id: 0.95   # cheaper than scan time
        opp = self._opp("CV-Z", "CONVERGE", "tok-cv-z", 0.96)
        e.open_position(opp)
        _, _, _, submitted_price = e._orders[0]
        expected = round(min(0.999, 0.95 * (1 + config.CV_MAX_PRICE_CUSHION_PCT / 100)), 3)
        assert submitted_price == pytest.approx(expected)
        assert submitted_price < 0.96

    def test_fetch_fresh_ask_returns_none_on_failure(self, tmp_path, monkeypatch):
        """Exercises the REAL _fetch_fresh_ask (not a test double) against
        a PolymarketClient.fetch_book that raises, confirming the
        None-on-failure contract without an actual network call."""
        from polyedge import live as lv
        from polyedge.api import PolymarketClient

        def raise_boom(self, token_id):
            raise RuntimeError("boom")
        monkeypatch.setattr(PolymarketClient, "fetch_book", raise_boom)
        e = lv.LiveEngine(state_dir=str(tmp_path))
        assert e._fetch_fresh_ask("tok-1") is None

    def test_fetch_fresh_ask_returns_best_ask_when_book_available(self, tmp_path, monkeypatch):
        from polyedge import live as lv
        from polyedge.api import PolymarketClient
        fake_book = OrderBook("tok-1", asks=[BookLevel(0.987, 100)], bids=[])
        monkeypatch.setattr(PolymarketClient, "fetch_book",
                            lambda self, token_id: fake_book)
        e = lv.LiveEngine(state_dir=str(tmp_path))
        assert e._fetch_fresh_ask("tok-1") == pytest.approx(0.987)


# ------------------------------------------------------------------ controls (control panel)
class TestControls:
    def test_load_defaults_when_missing(self, tmp_path):
        from polyedge import controls
        st = controls.load(str(tmp_path))
        assert st == {"paused": False, "kill_switch": False,
                      "max_allocation_usd": None, "liquidate_queue": [],
                      "stop_loss_pct": {}, "default_stop_loss_pct": None}

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

    # ---- default_stop_loss_pct: runtime control-panel override of
    # config.LIVE_DEFAULT_STOP_LOSS_PCT
    def test_set_default_stop_loss_pct_and_clear(self, tmp_path):
        from polyedge import controls
        controls.set_default_stop_loss_pct(25, str(tmp_path))
        assert controls.load(str(tmp_path))["default_stop_loss_pct"] == 25.0
        controls.set_default_stop_loss_pct(None, str(tmp_path))
        assert controls.load(str(tmp_path))["default_stop_loss_pct"] is None

    def test_set_default_stop_loss_pct_clamped_to_5_90(self, tmp_path):
        from polyedge import controls
        controls.set_default_stop_loss_pct(500, str(tmp_path))
        assert controls.load(str(tmp_path))["default_stop_loss_pct"] == 90.0
        controls.set_default_stop_loss_pct(0, str(tmp_path))
        assert controls.load(str(tmp_path))["default_stop_loss_pct"] == 5.0

    def test_set_default_stop_loss_pct_does_not_touch_per_position_dict(self, tmp_path):
        """Changing the default must never retroactively rewrite an
        already-open position's own snapshotted stop_loss_pct entry."""
        from polyedge import controls
        controls.set_stop_loss("CV-OLD", 20, str(tmp_path))
        controls.set_default_stop_loss_pct(60, str(tmp_path))
        st = controls.load(str(tmp_path))
        assert st["stop_loss_pct"] == {"CV-OLD": 20.0}
        assert st["default_stop_loss_pct"] == 60.0


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
        e._fetch_fresh_ask = lambda token_id: None   # no live network in tests
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


# ------------------------------------------------------------------ report (dashboard rendering)
class TestReport:
    """"Latest scan" on the dashboard always rendered empty. Root cause:
    opportunities were computed fresh in main.run_cycle()'s local scope
    and handed to write_dashboard() as a plain function argument -- never
    written into `state` itself. That's fine for the SAME process's own
    call (main.py's public GitHub Pages dashboard), but control_server.py's
    /dashboard route runs as a SEPARATE process (run_forever.py's live/
    dry-run engine writes state.json; the Flask app reads a fresh copy of
    it later) that had no way to see them -- a state-persistence gap, not
    a rendering bug. These test render_dashboard_html() directly, in
    isolation; TestControlServer below covers the same fix through the
    real Flask route."""

    def test_falls_back_to_state_last_scan_opportunities_when_none_given(self):
        from polyedge.report import render_dashboard_html
        state = {"cash": 100.0, "starting_bankroll": 100.0, "positions": [],
                 "closed": [], "history": [], "trades": [],
                 "last_scan_opportunities": [
                     {"strategy": "CONVERGE", "title": "Converge: real one",
                      "edge": 0.04, "guaranteed": False, "note": ""}]}
        html = render_dashboard_html(state)   # opportunities= not passed at all
        assert "Converge: real one" in html

    def test_missing_key_defaults_to_empty_not_a_crash(self):
        """Pre-fix state.json (or state from any caller that never sets
        this key) must not raise a KeyError."""
        from polyedge.report import render_dashboard_html
        state = {"cash": 100.0, "starting_bankroll": 100.0, "positions": [],
                 "closed": [], "history": [], "trades": []}   # no key at all
        html = render_dashboard_html(state)
        assert "const OPPS  = []" in html

    def test_explicit_empty_list_overrides_state_data(self):
        """Maintenance scripts (cleanup_phantom_arbs.py, cancel_stale_locks.py)
        deliberately pass opportunities=[] since no scan ran -- the OPPS
        table must stay empty even though stale data sits in state from a
        much earlier real scan (state itself is still shown verbatim
        elsewhere on the page -- that's unrelated to what's being checked
        here, so this asserts on the OPPS embed specifically, not a blanket
        full-page text search)."""
        from polyedge.report import render_dashboard_html
        state = {"cash": 100.0, "starting_bankroll": 100.0, "positions": [],
                 "closed": [], "history": [], "trades": [],
                 "last_scan_opportunities": [
                     {"strategy": "CONVERGE", "title": "Converge: stale one",
                      "edge": 0.04, "guaranteed": False, "note": ""}]}
        html = render_dashboard_html(state, opportunities=[])
        assert "const OPPS  = []" in html
        assert "const OPPS  = []" in html

    def test_explicit_opportunities_still_used_when_given(self):
        """The original direct-pass-through path (main.py used to call it
        this way) must keep working."""
        from polyedge.report import render_dashboard_html
        state = {"cash": 100.0, "starting_bankroll": 100.0, "positions": [],
                 "closed": [], "history": [], "trades": []}
        opps = [{"strategy": "LONGSHOT", "title": "Fade: explicit one",
                "edge": 0.10, "guaranteed": False, "note": ""}]
        html = render_dashboard_html(state, opportunities=opps)
        assert "Fade: explicit one" in html


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

    def test_default_stop_loss_route(self, tmp_path, monkeypatch):
        c = self._make_client(tmp_path, monkeypatch)
        h = {"X-Control-Token": "secret-token"}
        r = c.post("/api/default_stop_loss", json={"pct": 45}, headers=h)
        assert r.status_code == 200 and r.get_json()["default_stop_loss_pct"] == 45.0
        r2 = c.get("/api/state", headers=h)
        assert r2.get_json()["controls"]["default_stop_loss_pct"] == 45.0
        assert r2.get_json()["effective_default_stop_loss_pct"] == 45.0

    def test_effective_default_stop_loss_falls_back_to_config_when_unset(self, tmp_path, monkeypatch):
        c = self._make_client(tmp_path, monkeypatch)
        h = {"X-Control-Token": "secret-token"}
        r = c.get("/api/state", headers=h)
        assert r.get_json()["controls"]["default_stop_loss_pct"] is None
        assert r.get_json()["effective_default_stop_loss_pct"] == config.LIVE_DEFAULT_STOP_LOSS_PCT

    # ---- "Latest scan" always rendering empty: opportunities were computed
    # fresh in main.run_cycle()'s local scope and handed straight to
    # write_dashboard() as a function argument -- never persisted to
    # state.json -- so this route (a SEPARATE process from run_forever.py)
    # had no way to ever see them, structurally, regardless of whether a
    # real scan had actually found candidates. Confirms the actual fix:
    # state.json is now the single source of truth this route reads.
    def test_dashboard_shows_latest_scan_opportunities_regression(self, tmp_path, monkeypatch):
        e = PaperEngine(state_dir=str(tmp_path / "state"))
        e.state["last_scan_opportunities"] = [{
            "strategy": "CONVERGE", "title": "Converge: a real candidate",
            "edge": 0.04, "guaranteed": False, "note": "96c, 4d to resolution",
        }]
        e.save()
        c = self._make_client(tmp_path, monkeypatch)
        r = c.get("/dashboard?token=secret-token")
        assert r.status_code == 200
        assert "Converge: a real candidate" in r.get_data(as_text=True)

    def test_dashboard_latest_scan_empty_when_state_has_no_key_yet(self, tmp_path, monkeypatch):
        """Pre-fix state.json (or a freshly-initialized one) has no
        last_scan_opportunities key at all -- must default to empty, not
        raise a KeyError."""
        self._seed_position(tmp_path)
        c = self._make_client(tmp_path, monkeypatch)
        r = c.get("/dashboard?token=secret-token")
        assert r.status_code == 200


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
        assert url == reconcile.POLYGON_RPC_URL
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
