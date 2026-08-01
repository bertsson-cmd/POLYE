"""LiveEngine — real-money order execution.

Same accounting semantics as PaperEngine (it subclasses it: same state
file shape, same mark/resolve/close_early math), but every fill is a real
Polymarket order placed through py-clob-client-v2 instead of a simulated one.

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

This module talks to Polymarket's CLOB V2 (the V1 exchange contracts,
USDC.e collateral, and `py-clob-client` package were retired at the
V2 cutover on April 28, 2026 -- V1 clients do not work against
production at all). It was rewritten against `py-clob-client-v2`'s
actual source (fetched during the rewrite, not guessed from memory) but
has still never been run against live Polymarket (no network access in
the sandbox that did the rewrite). It is fully unit-tested with
`_place_order` mocked out -- expect a possible small day-one fix once it
hits the real API, same as before. Do not skip the dry-run stage in
LIVE.md.

Two things worth knowing about the V2 rewrite specifically:
  * `signature_type` stays at 1 (POLY_PROXY) by default -- matching the
    Magic/email-wallet proxy account setup this repo has always assumed.
    V2 also offers signature_type=3 (POLY_1271, a "deposit wallet" with
    its own distinct address, separate from the Magic-exported proxy
    wallet) for NEW integrations, but at the time of this rewrite it has
    open, unresolved bugs in py-clob-client-v2 for programmatic order
    placement (the CLOB rejects the order because the API key binds to
    the owner EOA while POLY_1271 requires the signer to be the deposit
    wallet itself) -- see LIVE.md. Switching to it is a deliberate future
    decision, not something to do by default.
  * pUSD (Polymarket's V2 collateral token, replacing USDC.e) does NOT
    get wrapped automatically when placing an order through the API --
    only Polymarket's own UI does that for you. `open_position()` checks
    tradeable pUSD balance/allowance via the SDK's own
    `get_balance_allowance()` before ever attempting a real order, and
    halts loudly (same mechanism as the daily-loss breaker) rather than
    repeatedly trying and failing if the funder wallet's USDC.e was never
    wrapped. This bot does not call wrap() itself -- see LIVE.md.
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
        """Lazily build the py-clob-client-v2 instance. Only ever reached
        from the real `_place_order`/pUSD-check code below -- tests replace
        `_place_order` wholesale, so the suite never needs credentials or
        the package installed to import this module.

        chain_id (not "chain") is the real V2 constructor kwarg, confirmed
        directly against py_clob_client_v2/client.py's source -- some
        secondary migration write-ups claim a "chain" rename, but that
        did not hold up against the actual current source.
        """
        if self._client is None:
            from py_clob_client_v2.client import ClobClient
            key = os.environ["POLYEDGE_PRIVATE_KEY"]
            funder = os.environ.get("POLYEDGE_FUNDER_ADDRESS")
            chain_id = int(os.environ.get("POLYEDGE_CHAIN_ID", "137"))
            # POLY_PROXY (Magic/email-wallet) by default -- see the module
            # docstring for why this stays at 1 rather than the newer
            # deposit-wallet signature_type=3.
            signature_type = int(os.environ.get("POLYEDGE_SIGNATURE_TYPE", "1"))
            client = ClobClient(config.CLOB_BASE, chain_id=chain_id, key=key,
                                signature_type=signature_type, funder=funder)
            client.set_api_creds(client.create_or_derive_api_key())
            self._client = client
        return self._client

    def _check_pusd_balance(self) -> bool:
        """Verify the funder wallet has tradeable pUSD balance/allowance
        BEFORE attempting a real order. API/programmatic traders must wrap
        USDC.e into pUSD via the Collateral Onramp's wrap() themselves --
        unlike Polymarket's own UI, the CLOB does not do this for you when
        an order comes in through the API. This bot never calls wrap()
        itself (an infrequent, operator-driven action, not something to
        automate blind) -- it only checks and fails loudly, pointing the
        operator at LIVE.md, rather than silently assuming funds are ready.

        Uses the SDK's own get_balance_allowance() (COLLATERAL = pUSD)
        rather than a raw wallet balance: that's the only check that also
        catches "wrapped but the exchange contract's allowance was never
        approved," which a plain balanceOf() would miss entirely.
        """
        try:
            from py_clob_client_v2.clob_types import AssetType, BalanceAllowanceParams
            client = self._clob_client()
            resp = client.get_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)) or {}
            balance = float(resp.get("balance", 0) or 0)
            allowance = float(resp.get("allowance", 0) or 0)
        except Exception as e:
            log.error("pUSD balance/allowance check failed: %s", e)
            return False
        if balance <= 0 or allowance <= 0:
            log.error("pUSD balance/allowance check failed: balance=%s allowance=%s -- "
                     "the funder wallet has no tradeable pUSD. USDC.e deposits must be "
                     "wrapped into pUSD via the Collateral Onramp's wrap() before this "
                     "bot can trade -- it does not do that automatically. See LIVE.md.",
                     balance, allowance)
            return False
        return True

    def _place_order(self, token_id: str, price: float, shares: float,
                     side: str) -> bool:
        """Submit a real fill-or-kill order. Returns True iff fully filled.

        The only method in this module that ever talks to the network --
        overridden wholesale in tests.

        The response shape assumed here (a "status" field with "matched"/
        "filled" meaning fully filled) is carried over unverified from the
        V1 rewrite, since research for the V2 rewrite could not confirm the
        exact V2 response body -- same "expect a possible day-one fix"
        caveat as everywhere else in this module.
        """
        from py_clob_client_v2.clob_types import OrderArgsV2, OrderType
        from py_clob_client_v2.order_builder.constants import BUY, SELL
        client = self._clob_client()
        args = OrderArgsV2(token_id=token_id, price=round(price, 3),
                           size=round(shares, 2), side=BUY if side == "BUY" else SELL)
        resp = client.create_and_post_order(args, order_type=OrderType.FOK) or {}
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
        if not self._check_pusd_balance():
            write_halt("pUSD balance/allowance check failed before opening "
                      f"{opp.key} -- see log for details")
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
        # no pUSD balance check here on purpose: selling an existing
        # position needs CONDITIONAL (outcome-token) balance, not COLLATERAL
        # -- that check is specific to opening new exposure with pUSD cash.
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
