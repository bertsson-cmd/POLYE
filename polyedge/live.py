"""LiveEngine — real-money order execution.

Same accounting semantics as PaperEngine (it subclasses it: same state
file shape, same mark/resolve/close_early math), but every fill is a real
Polymarket order placed through py-clob-client instead of a simulated one.

Safety model (do not weaken any of this without the owner explicitly
asking for it in those terms):

  * THREE independent gates are required before any real order is even
    attempted: the POLYEDGE_LIVE=1 env var, an ARMED file physically
    present in the working directory, and POLYEDGE_DRY_RUN=0 (the env var
    defaults to dry-run, i.e. missing/anything-but-"0" means "do not touch
    the exchange").
  * A daily realized-loss circuit breaker: once today's (UTC calendar day)
    realized P/L from closed positions reaches -LIVE_MAX_DAILY_LOSS, a
    HALTED file is written and `live_gates_open()` returns False from then
    on -- for EVERY kind of order, including closes -- until a human
    deletes the file. This is deliberate: a fast string of losses can mean
    a data or logic bug, and an automatic unwind on possibly-bad prices
    could make things worse. Clear it only after checking positions by
    hand.
  * Multi-leg locks (ARB/REL) are refused live by default
    (config.LIVE_ALLOW_MULTILEG=0) -- legging into a lock without atomic
    fills can strand an unhedged position. Only single-leg CONVERGE/
    LONGSHOT trade live out of the box.
  * Orders are FOK (fill-or-kill) only. Nothing is ever recorded in state
    unless the order came back fully filled -- no partial fills, no
    guessing.

This module was written against py-clob-client's documented API surface
but has never been run against live Polymarket (no network access in the
sandbox that built it). It is fully unit-tested with `_place_order`
mocked out -- expect a possible small day-one fix once it hits the real
API. Do not skip the dry-run stage in LIVE.md.
"""
import logging
import os
from typing import Dict, Optional

from . import config, controls
from .models import Opportunity
from .paper import PaperEngine

log = logging.getLogger("polyedge.live")

ARMED_FILE = "ARMED"
HALTED_FILE = "HALTED"


# ---------------------------------------------------------------- gates
def live_enabled() -> bool:
    return os.environ.get("POLYEDGE_LIVE") == "1"


def armed() -> bool:
    return os.path.exists(ARMED_FILE)


def halted() -> bool:
    return os.path.exists(HALTED_FILE)


def dry_run() -> bool:
    """Defaults to True (dry-run) -- must be explicitly set to "0" to allow
    real order placement. Missing, "1", or any other value all mean dry-run."""
    return os.environ.get("POLYEDGE_DRY_RUN", "1") != "0"


def live_gates_open() -> bool:
    """LIVE=1 + ARMED file present + not halted. Does NOT include dry_run()
    -- callers check that separately, since dry-run is "gates open but
    don't actually pull the trigger", not "gates closed"."""
    return live_enabled() and armed() and not halted()


def write_halt(reason: str) -> None:
    with open(HALTED_FILE, "w") as f:
        f.write(reason + "\n")
    log.error("LIVE TRADING HALTED: %s", reason)


def clear_halt() -> None:
    if os.path.exists(HALTED_FILE):
        os.remove(HALTED_FILE)
        log.warning("halt cleared manually")


class LiveEngine(PaperEngine):
    def __init__(self, state_dir: str = None):
        super().__init__(state_dir=state_dir)
        self._client = None

    # ------------------------------------------------------------ CLOB client
    def _clob_client(self):
        """Lazily build the py-clob-client instance. Only ever reached from
        the real `_place_order` below -- tests replace `_place_order`
        wholesale, so the suite never needs credentials or the package
        installed to import this module."""
        if self._client is None:
            from py_clob_client.client import ClobClient
            key = os.environ["POLYEDGE_PRIVATE_KEY"]
            funder = os.environ.get("POLYEDGE_FUNDER_ADDRESS")
            chain_id = int(os.environ.get("POLYEDGE_CHAIN_ID", "137"))
            signature_type = int(os.environ.get("POLYEDGE_SIGNATURE_TYPE", "1"))
            client = ClobClient(config.CLOB_BASE, key=key, chain_id=chain_id,
                                signature_type=signature_type, funder=funder)
            client.set_api_creds(client.create_or_derive_api_creds())
            self._client = client
        return self._client

    def _place_order(self, token_id: str, price: float, shares: float,
                     side: str) -> bool:
        """Submit a real fill-or-kill order. Returns True iff fully filled.

        The only method in this module that ever talks to the network --
        overridden wholesale in tests.
        """
        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY, SELL
        client = self._clob_client()
        args = OrderArgs(price=round(price, 3), size=round(shares, 2),
                         side=BUY if side == "BUY" else SELL, token_id=token_id)
        signed = client.create_order(args)
        resp = client.post_order(signed, OrderType.FOK) or {}
        status = str(resp.get("status", "")).lower()
        return status in ("matched", "filled")

    # ------------------------------------------------------------ daily halt
    def _today_realized_pl(self) -> float:
        import datetime as dt
        today = dt.datetime.now(dt.timezone.utc).date()
        total = 0.0
        for c in self.state.get("closed", []):
            ts = c.get("closed_ts")
            if ts is None:
                continue
            if dt.datetime.fromtimestamp(ts, dt.timezone.utc).date() == today:
                total += c.get("pl", 0.0)
        return total

    # ------------------------------------------------------------ trading
    def open_position(self, opp: Opportunity, ts: Optional[float] = None) -> Optional[dict]:
        ctrl = controls.load(self.state_dir)
        if ctrl["paused"]:
            log.info("trading paused via control panel -- refusing to open %s", opp.key)
            return None
        if ctrl["kill_switch"]:
            log.info("kill switch active via control panel -- refusing to open %s", opp.key)
            return None
        if not live_gates_open():
            log.info("live gates closed -- refusing to open %s (%s)", opp.key, opp.title)
            return None
        today_pl = self._today_realized_pl()
        if today_pl <= -config.LIVE_MAX_DAILY_LOSS:
            write_halt(f"realized loss ${-today_pl:.2f} today >= "
                      f"LIVE_MAX_DAILY_LOSS ${config.LIVE_MAX_DAILY_LOSS:.2f}")
            return None
        if len(opp.legs) > 1 and not config.LIVE_ALLOW_MULTILEG:
            log.warning("refusing multi-leg lock %s live (LIVE_ALLOW_MULTILEG=0)",
                       opp.key)
            return None
        if dry_run():
            log.info("[DRY RUN] would open %s: %d leg(s), cost $%.2f",
                     opp.key, len(opp.legs), opp.total_cost())
            return None
        for leg in opp.legs:
            filled = self._place_order(leg.token_id, leg.entry_price, leg.shares, "BUY")
            if not filled:
                log.warning("open_position: BUY order for %s not filled, aborting %s",
                           leg.token_id, opp.key)
                return None
        return super().open_position(opp, ts=ts)

    def close_early(self, key: str, exit_prices: Dict[str, float],
                    ts: Optional[float] = None, reason: str = "manual_close") -> Optional[dict]:
        if not live_gates_open() or dry_run():
            log.info("live gates closed or dry-run -- refusing to close %s", key)
            return None
        pos = next((p for p in self.state["positions"] if p["key"] == key), None)
        if pos is None:
            return None
        for leg in pos["legs"]:
            px = exit_prices.get(leg["token_id"])
            if px is None:
                return None
            filled = self._place_order(leg["token_id"], px, leg["shares"], "SELL")
            if not filled:
                log.warning("close_early: SELL order for %s not filled, aborting close of %s",
                           leg["token_id"], key)
                return None
        return super().close_early(key, exit_prices, ts=ts, reason=reason)

    # ------------------------------------------------------------ manual/control-panel liquidation
    def liquidate_position(self, key: str, exit_prices: Dict[str, float],
                           reason: str = "manual_liquidate") -> Optional[dict]:
        """Force-close a single open position. Refuses multi-leg locks
        outright -- unwinding one leg of an ARB/REL guarantee strands the
        other -- logging a clear warning instead of pretending to unwind
        them safely. Subject to the same gates as close_early (which this
        delegates to): live_gates_open() and not dry_run()."""
        pos = next((p for p in self.state["positions"] if p["key"] == key), None)
        if pos is None:
            return None
        if len(pos["legs"]) > 1:
            log.warning("liquidate_position: refusing to force-unwind multi-leg "
                       "lock %s (would strand the other leg(s))", key)
            return None
        return self.close_early(key, exit_prices, reason=reason)

    def liquidate_all(self, exit_prices: Dict[str, float],
                      reason: str = "kill_switch"):
        """Liquidate every eligible (single-leg) open position.

        Returns (closed, skipped) -- `skipped` is a list of (key, reason)
        pairs for anything left open, most importantly multi-leg locks,
        which this NEVER force-unwinds.
        """
        closed, skipped = [], []
        for pos in list(self.state["positions"]):
            key = pos["key"]
            if len(pos["legs"]) > 1:
                skipped.append((key, "multi-leg lock -- cannot be forced without stranding a leg"))
                continue
            token_id = pos["legs"][0]["token_id"]
            if token_id not in exit_prices:
                skipped.append((key, "no current price available"))
                continue
            c = self.liquidate_position(key, exit_prices, reason=reason)
            if c:
                closed.append(c)
            else:
                skipped.append((key, "order not filled or gates closed"))
        return closed, skipped
