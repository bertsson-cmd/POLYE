"""Paper trading engine.

Simulates fills at the ask prices captured during the scan, tracks open
positions, marks them to market each cycle, settles resolutions, and keeps
a full equity-curve history.

Accounting invariant (tested):  equity = cash + open positions marked value
and after every resolution:      cash_after = cash_before + payout.

State is persisted as JSON with an atomic write (write temp, rename) so a
crashed run can never corrupt the state file.
"""
import json
import os
import tempfile
import time
from typing import Dict, List, Optional

from . import config
from .models import Leg, Opportunity


def _now() -> float:
    return time.time()


class PaperEngine:
    def __init__(self, state_dir: str = None):
        self.state_dir = state_dir or config.STATE_DIR
        self.path = os.path.join(self.state_dir, "paper_state.json")
        self.state = self._load()

    # ------------------------------------------------------------ state io
    def _load(self) -> dict:
        if os.path.exists(self.path):
            with open(self.path) as f:
                return json.load(f)
        return {
            "cash": config.STARTING_BANKROLL,
            "starting_bankroll": config.STARTING_BANKROLL,
            "positions": [],       # open positions
            "closed": [],          # settled positions
            "history": [],         # equity curve points
            "trades": [],          # every open/close event (for the trade map)
        }

    def save(self):
        os.makedirs(self.state_dir, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.state_dir, suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(self.state, f, indent=1)
        os.replace(tmp, self.path)

    # ------------------------------------------------------------ queries
    @property
    def cash(self) -> float:
        return self.state["cash"]

    def open_keys(self) -> set:
        return {p["key"] for p in self.state["positions"]}

    def open_cost_by_strategy(self) -> Dict[str, float]:
        out: Dict[str, float] = {}
        for p in self.state["positions"]:
            out[p["strategy"]] = out.get(p["strategy"], 0.0) + p["cost"]
        return out

    def total_open_cost(self) -> float:
        return sum(p["cost"] for p in self.state["positions"])

    def open_longshot_count(self) -> int:
        return sum(1 for p in self.state["positions"] if p["strategy"] == "LONGSHOT")

    # ------------------------------------------------------------ trading
    def open_position(self, opp: Opportunity, ts: Optional[float] = None) -> Optional[dict]:
        """Fill a sized opportunity. Returns the position dict, or None."""
        cost = opp.total_cost()
        if cost < config.MIN_TICKET or cost > self.state["cash"] + 1e-9:
            return None
        if opp.key in self.open_keys():
            return None
        ts = ts or _now()
        pos = {
            "key": opp.key, "strategy": opp.strategy, "title": opp.title,
            "opened": ts, "cost": round(cost, 6),
            "edge": round(opp.edge, 6), "guaranteed": opp.guaranteed,
            "guaranteed_payout_sets": opp.guaranteed_payout,
            "est_p_win": opp.est_p_win, "resolve_by": opp.resolve_by,
            "note": opp.note,
            "legs": [{
                "token_id": l.token_id, "market_id": l.market_id,
                "label": l.label, "side": l.side,
                "entry_price": round(l.entry_price, 6),
                "shares": round(l.shares, 6),
            } for l in opp.legs],
        }
        self.state["cash"] = round(self.state["cash"] - cost, 6)
        self.state["positions"].append(pos)
        self.state["trades"].append({
            "ts": ts, "type": "OPEN", "key": opp.key, "strategy": opp.strategy,
            "title": opp.title, "amount": round(cost, 2), "pl": None,
        })
        return pos

    # ------------------------------------------------------------ marking
    @staticmethod
    def _mark_position(pos: dict, prices: Dict[str, float]) -> float:
        """Current value of a position given token->price marks.

        Guaranteed sets are floored at their locked payout value: the lock
        is worth its guaranteed payout at resolution regardless of interim
        quote noise (number of sets = min shares across legs).

        As a side effect, annotates pos in place with per-leg current
        prices and unrealized P/L so the dashboard can show live position
        performance (entry vs current) without recomputing anything.
        """
        value = 0.0
        current_prices = {}
        for leg in pos["legs"]:
            px = prices.get(leg["token_id"], leg["entry_price"])
            current_prices[leg["token_id"]] = round(px, 6)
            value += leg["shares"] * px
        if pos.get("guaranteed") and pos.get("guaranteed_payout_sets"):
            sets = min(l["shares"] for l in pos["legs"])
            value = max(value, sets * pos["guaranteed_payout_sets"])
        pos["current_prices"] = current_prices
        pos["current_value"] = round(value, 6)
        pos["unrealized_pl"] = round(value - pos["cost"], 6)
        pos["unrealized_pl_pct"] = (
            round((value - pos["cost"]) / pos["cost"] * 100, 3) if pos["cost"] > 0 else 0.0)
        return value

    def mark_to_market(self, prices: Dict[str, float], ts: Optional[float] = None) -> dict:
        ts = ts or _now()
        open_value = sum(self._mark_position(p, prices) for p in self.state["positions"])
        realized = sum(c["pl"] for c in self.state["closed"])
        point = {
            "ts": ts,
            "cash": round(self.state["cash"], 4),
            "open_value": round(open_value, 4),
            "equity": round(self.state["cash"] + open_value, 4),
            "realized_total": round(realized, 4),
            "open_positions": len(self.state["positions"]),
        }
        self.state["history"].append(point)
        return point

    # ------------------------------------------------------------ settlement
    def resolve(self, market_outcomes: Dict[str, str], ts: Optional[float] = None) -> List[dict]:
        """Settle positions whose EVERY leg's market has a known outcome.

        market_outcomes: market_id -> "YES" or "NO" (the winning side).
        A leg pays $1/share if its held side equals the winning side.
        """
        ts = ts or _now()
        settled, still_open = [], []
        for pos in self.state["positions"]:
            legs_resolved = all(l["market_id"] in market_outcomes for l in pos["legs"])
            if not legs_resolved:
                still_open.append(pos)
                continue
            payout = sum(
                l["shares"] * (1.0 if market_outcomes[l["market_id"]] == l["side"] else 0.0)
                for l in pos["legs"])
            pl = payout - pos["cost"]
            self.state["cash"] = round(self.state["cash"] + payout, 6)
            closed = dict(pos)
            closed.update({"closed_ts": ts, "payout": round(payout, 6),
                           "pl": round(pl, 6)})
            self.state["closed"].append(closed)
            self.state["trades"].append({
                "ts": ts, "type": "CLOSE", "key": pos["key"],
                "strategy": pos["strategy"], "title": pos["title"],
                "amount": round(payout, 2), "pl": round(pl, 2),
            })
            settled.append(closed)
        self.state["positions"] = still_open
        return settled

    # ------------------------------------------------------------ take-profit (early exit)
    def close_early(self, key: str, exit_prices: Dict[str, float],
                    ts: Optional[float] = None, reason: str = "take_profit") -> Optional[dict]:
        """Close an open position early by selling into live bid prices.

        Unlike resolve(), this doesn't require the market to have actually
        resolved — it simulates selling the position back into the order
        book at prices given in exit_prices (token_id -> price). If any
        leg's exit price is missing, the position is left untouched rather
        than guessed at.
        """
        ts = ts or _now()
        for i, pos in enumerate(self.state["positions"]):
            if pos["key"] != key:
                continue
            payout = 0.0
            for leg in pos["legs"]:
                px = exit_prices.get(leg["token_id"])
                if px is None:
                    return None
                payout += leg["shares"] * px
            pl = payout - pos["cost"]
            self.state["cash"] = round(self.state["cash"] + payout, 6)
            closed = dict(pos)
            closed.update({"closed_ts": ts, "payout": round(payout, 6),
                           "pl": round(pl, 6), "close_reason": reason})
            self.state["closed"].append(closed)
            self.state["trades"].append({
                "ts": ts, "type": "CLOSE", "key": pos["key"], "strategy": pos["strategy"],
                "title": pos["title"], "amount": round(payout, 2), "pl": round(pl, 2),
                "reason": reason,
            })
            del self.state["positions"][i]
            return closed
        return None

    def scan_take_profits(self, books: Dict[str, "OrderBook"],
                          ts: Optional[float] = None) -> List[dict]:
        """Close eligible open positions early, per config.TAKE_PROFIT_STRATEGIES.

        Rule: sell once (current_bid - entry_price) / (1 - entry_price) —
        the fraction of remaining upside to $1 already captured — reaches
        TAKE_PROFIT_UPSIDE_CAPTURE. Uses the live bid (what you could
        actually sell into), never the indicative mark. Multi-leg positions
        are always skipped: unwinding one leg of a lock breaks the guarantee.
        """
        ts = ts or _now()
        closed = []
        for pos in list(self.state["positions"]):
            if pos["strategy"] not in config.TAKE_PROFIT_STRATEGIES:
                continue
            if len(pos["legs"]) != 1:
                continue
            leg = pos["legs"][0]
            book = books.get(leg["token_id"])
            if not book:
                continue
            bid = book.best_bid()
            if bid is None:
                continue
            entry = leg["entry_price"]
            gain = bid - entry
            if gain < config.TAKE_PROFIT_MIN_GAIN:
                continue
            upside = 1.0 - entry
            if upside <= 0:
                continue
            captured = gain / upside
            if captured >= config.TAKE_PROFIT_UPSIDE_CAPTURE:
                c = self.close_early(pos["key"], {leg["token_id"]: bid}, ts=ts)
                if c:
                    closed.append(c)
        return closed

    # ------------------------------------------------------------ stats
    def stats(self) -> dict:
        closed = self.state["closed"]
        wins = [c for c in closed if c["pl"] > 0]
        realized = sum(c["pl"] for c in closed)
        hist = self.state["history"]
        equity = hist[-1]["equity"] if hist else self.state["cash"]
        peak, max_dd = 0.0, 0.0
        for h in hist:
            peak = max(peak, h["equity"])
            if peak > 0:
                max_dd = max(max_dd, (peak - h["equity"]) / peak)
        per_strategy: Dict[str, dict] = {}
        for c in closed:
            s = per_strategy.setdefault(c["strategy"],
                                        {"n": 0, "wins": 0, "pl": 0.0})
            s["n"] += 1
            s["wins"] += 1 if c["pl"] > 0 else 0
            s["pl"] = round(s["pl"] + c["pl"], 4)
        return {
            "equity": round(equity, 2),
            "cash": round(self.state["cash"], 2),
            "starting_bankroll": self.state["starting_bankroll"],
            "total_return_pct": round(
                (equity / self.state["starting_bankroll"] - 1) * 100, 2)
                if self.state["starting_bankroll"] else 0.0,
            "realized_pl": round(realized, 2),
            "open_positions": len(self.state["positions"]),
            "closed_trades": len(closed),
            "win_rate_pct": round(100 * len(wins) / len(closed), 1) if closed else None,
            "max_drawdown_pct": round(max_dd * 100, 2),
            "per_strategy": per_strategy,
        }
