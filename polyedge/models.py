"""Shared data models. Plain dataclasses, JSON-serialisable via to_dict()."""
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional


def days_to_resolution(end_date: str, default: float = 1e9) -> float:
    """Days from now until an ISO end date. `default` for missing/bad dates
    (a large number, so unknown dates sort as 'far away', never 'soon')."""
    if not end_date:
        return default
    try:
        dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
        return max(0.0, (dt - datetime.now(timezone.utc)).total_seconds() / 86400)
    except ValueError:
        return default


@dataclass
class BookLevel:
    price: float
    size: float  # number of shares at this price


@dataclass
class OrderBook:
    """Order book for one outcome token. asks/bids sorted best-first."""
    token_id: str
    asks: list = field(default_factory=list)  # list[BookLevel], lowest price first
    bids: list = field(default_factory=list)  # list[BookLevel], highest price first

    def best_ask(self) -> Optional[float]:
        return self.asks[0].price if self.asks else None

    def best_bid(self) -> Optional[float]:
        return self.bids[0].price if self.bids else None

    def ask_depth_usd(self, max_price: Optional[float] = None) -> float:
        """USD available to buy at or below max_price (all asks if None)."""
        total = 0.0
        for lvl in self.asks:
            if max_price is not None and lvl.price > max_price + 1e-12:
                break
            total += lvl.price * lvl.size
        return total

    def buyable_shares(self, budget_usd: float) -> float:
        """How many shares can be bought (walking the asks) with budget_usd."""
        shares = 0.0
        remaining = budget_usd
        for lvl in self.asks:
            cost_level = lvl.price * lvl.size
            if cost_level <= remaining:
                shares += lvl.size
                remaining -= cost_level
            else:
                shares += remaining / lvl.price
                remaining = 0.0
                break
        return shares

    def avg_fill_price(self, shares_wanted: float) -> Optional[float]:
        """Volume-weighted average price to buy shares_wanted, or None if book too thin."""
        remaining = shares_wanted
        cost = 0.0
        for lvl in self.asks:
            take = min(lvl.size, remaining)
            cost += take * lvl.price
            remaining -= take
            if remaining <= 1e-9:
                return cost / shares_wanted
        return None


@dataclass
class Market:
    """One binary market (one question) inside an event."""
    market_id: str
    question: str
    yes_token: str
    no_token: str
    yes_price: float          # last/mid price from Gamma (indicative)
    liquidity: float = 0.0
    end_date: str = ""        # ISO string
    event_id: str = ""
    event_title: str = ""
    neg_risk: bool = False    # True if part of a mutually-exclusive outcome set


@dataclass
class Leg:
    """One leg of a (possibly multi-leg) position."""
    token_id: str
    market_id: str
    label: str               # human-readable, e.g. "YES Argentina wins"
    side: str                # "YES" or "NO" (which outcome token we hold)
    entry_price: float
    shares: float

    @property
    def cost(self) -> float:
        return self.entry_price * self.shares


@dataclass
class Opportunity:
    """A trade suggestion produced by a strategy scan."""
    strategy: str            # ARB | REL | LONGSHOT | CONVERGE
    key: str                 # dedup key (one open position per key)
    title: str
    edge: float              # expected profit per $1 of cost (or per payout set for ARB)
    guaranteed: bool         # True for pure arbitrage
    legs: list = field(default_factory=list)   # list[Leg] (sizes filled by risk module)
    guaranteed_payout: Optional[float] = None  # per payout-set, for ARB/REL locks
    est_p_win: Optional[float] = None          # for probabilistic strategies
    resolve_by: str = ""
    note: str = ""

    def total_cost(self) -> float:
        return sum(l.cost for l in self.legs)

    def to_dict(self) -> dict:
        d = asdict(self)
        return d
