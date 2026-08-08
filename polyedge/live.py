"""LiveEngine — real-money order execution.

Same accounting semantics as PaperEngine (it subclasses it: same state
file shape, same mark/resolve/close_early math), but every fill is a real
Polymarket order placed through `polymarket-client` (imports as
`polymarket`, GitHub Polymarket/py-sdk -- Polymarket's official unified
SDK) instead of a simulated one.

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
USDC.e collateral, and `py-clob-client` package were retired at the V2
cutover on April 28, 2026). `py-clob-client-v2` is fully retired for this
account: its deposit-wallet (signature_type=3/POLY_1271) order placement
is confirmed broken (Polymarket/py-clob-client-v2 issues #64, #70, #75,
#105, all open), and this account's exchange started hard-rejecting
signature_type=1 (POLY_PROXY) orders outright ("maker address not
allowed, please use the deposit wallet flow" -- a real production error,
reproduced repeatedly, not a guess). `polymarket-client`'s
`AsyncSecureClient` was verified end-to-end for this exact account via
`scripts/test_deposit_wallet.py` before this switch -- see LIVE.md.

Things worth knowing about the polymarket-client switch specifically:
  * This account's deposit wallet (signature_type=3/POLY_1271) and its
    already-funded `POLYEDGE_FUNDER_ADDRESS` are the SAME address
    (confirmed directly by the operator) -- no new funding step was
    needed, only the client swap. `AsyncSecureClient.create()` derives
    the deposit wallet address from the signing private key itself (its
    `wallet=` param defaults to "the signer's Deposit Wallet", confirmed
    from source); there is no `signature_type` or `chain_id` constructor
    argument at all -- `create()`'s full parameter list, confirmed from
    source, is `private_key`, `wallet`, `environment`, `credentials`,
    `api_key`, `nonce`, `logger`. `POLYEDGE_SIGNATURE_TYPE` and
    `POLYEDGE_CHAIN_ID` are therefore no longer read by this module.
  * Deposit wallets are gasless (relayed) accounts -- `create()` raises
    `UserInputError` unless given an `api_key=` credential for that
    relayed-transaction path (confirmed by hitting this exact error while
    testing `scripts/test_deposit_wallet.py`). `_builder_api_key()` below
    reads `POLYEDGE_BUILDER_API_KEY`/`_SECRET`/`_PASSPHRASE` and builds a
    `BuilderApiKey` from them, the same construction already proven
    working by that diagnostic script.
  * `polymarket-client`'s client is async; the rest of this module (and
    `run_forever.py`'s plain synchronous polling loop that drives it) is
    not. Rather than convert `PaperEngine`, `main.run_cycle`,
    `run_forever.py`'s loop, and `control_server.py`'s Flask routes to
    async for a ~5-minute polling cycle that doesn't need it, `_place_order`
    wraps a single `asyncio.run()` call per order: a fresh
    `AsyncSecureClient` is built, used, and closed within that one
    event-loop run. It is deliberately NOT cached across calls -- each
    `asyncio.run()` gets its own event loop, and an async client's
    underlying HTTP session bound to one loop breaks if reused from
    another. This was judged the smaller, safer change.
  * Orders are placed via `place_market_order(..., order_type="FOK")` --
    explicitly overriding the SDK's own "FAK" default (fill some, kill the
    rest), which would silently reintroduce partial fills. Confirmed from
    source (`polymarket/models/clob/order_response.py`): `OrderResponse`
    is `AcceptedOrder | RejectedOrder`; a FOK order that can't fully fill
    comes back as a `RejectedOrder` (`ok=False`,
    `code="fok_not_filled"`), not an accepted-but-unfilled order sitting
    on the book.
  * `AcceptedOrder.status` is one of `"live"`, `"matched"`, `"delayed"`
    (confirmed from source) -- **and `status == "matched"` alone is NOT a
    reliable "filled" signal.** A real order placed against this account
    came back `AcceptedOrder(ok=True, status='delayed', making_amount=0,
    taking_amount=0, trade_ids=(), transactions_hashes=())` and was
    independently confirmed to have actually filled -- the synchronous
    response for a "delayed" order carries NO fill data at all (not even
    `trade_ids`, despite that field's own docstring saying it's "empty
    when the order did not match"). polymarket-client's source has no
    special-casing or documentation for what "delayed" means; `status` is
    passed straight through from the raw CLOB API response
    (`normalize_order_response()` in `order_response.py`) with no
    SDK-side interpretation. The working theory -- inferred, not proven --
    is that "delayed" is specific to gasless/relayed (deposit wallet)
    accounts: the CLOB accepted the order but genuinely cannot report the
    outcome synchronously. `_resolve_fill()` handles this by polling
    `list_account_trades()` for a trade whose `taker_order_id` matches
    the order (confirmed from source: `ClobTrade.taker_order_id`
    "identifies the originating order"; there is no order-id filter on
    that endpoint itself -- its own `id` query param maps to a trade's
    own id, confirmed against the request-builder source -- so this
    scans recent trades for the token and matches client-side). See
    `_resolve_fill`'s docstring for the exact poll parameters and their
    caveats.
  * `place_market_order()` can RAISE instead of returning a `RejectedOrder`
    -- confirmed in production: a FOK order that couldn't fill raised
    `RequestRejectedError`, and with no handling that crashed the entire
    `run_cycle()`, skipping every other queued opportunity that cycle
    too. `_aplace_order` now wraps that call in a try/except for
    `polymarket.errors.PolymarketError` (the base class for the SDK's
    whole flat error hierarchy, not just `RequestRejectedError` -- see
    `_aplace_order`'s docstring), treating a caught error the same as a
    `RejectedOrder`: logged, not filled, that leg's `open_position`/
    `close_early` call aborts gracefully. Anything outside that hierarchy
    (an actual bug in this module) still propagates and crashes the cycle
    -- that's deliberate, not something this fix papers over.
  * A rejected order does NOT change the rejected token's edge/price/
    liquidity inputs, so risk.py's sizing would otherwise keep re-picking
    it as the best candidate cycle after cycle -- confirmed in production:
    one token was retried for over an hour straight, starving every other
    real candidate that cycle of a sizing slot. `open_position()` now
    records `token_id: time.time()` in `self.rejected_cooldown` on a
    not-filled BUY leg; `risk.size_opportunities()` skips any candidate
    with a leg still inside `POLYEDGE_REJECTED_COOLDOWN_MIN` minutes of
    that timestamp, before it can consume a sizing slot. See
    `rejected_cooldown`'s own comment in `__init__` for why this is
    in-memory only, not persisted across restarts.
  * pUSD (Polymarket's V2 collateral token, replacing USDC.e) does NOT
    get wrapped automatically when placing an order through the API --
    only Polymarket's own UI does that for you. `open_position()` checks
    tradeable pUSD balance via a direct on-chain read
    (`_check_pusd_balance`, unchanged by this switch -- see its own
    docstring for why) before ever attempting a real order, and halts
    loudly (same mechanism as the daily-loss breaker) rather than
    repeatedly trying and failing if the funder wallet's USDC.e was never
    wrapped. This bot does not call wrap() itself -- see LIVE.md. Note
    this only checks balance, not allowance -- see LIVE.md's
    known-limitations note.
"""
import asyncio
import logging
import os
import time
from typing import Dict, Optional

from . import config, controls
from .models import Opportunity
from .paper import PaperEngine

log = logging.getLogger("polyedge.live")

ARMED_FILE = "ARMED"
HALTED_FILE = "HALTED"

# How hard to poll list_account_trades() for a "delayed" order's real
# outcome before giving up and treating it as not filled (see
# LiveEngine._resolve_fill's docstring for why this polling exists at
# all). Module-level so tests can monkeypatch the delay to 0.
_DELAYED_FILL_POLL_ATTEMPTS = 5
_DELAYED_FILL_POLL_DELAY_S = 2.0
_DELAYED_FILL_SCAN_LIMIT = 50


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
        # token_id -> time.time() of its most recent BUY-leg rejection.
        # Read by risk.size_opportunities() (via main.run_cycle()'s
        # getattr(engine, "rejected_cooldown", None)) to skip re-selecting
        # a recently-dead token as the "best" candidate -- see
        # open_position()'s not-filled branch below for where this gets
        # written, and config.LIVE_REJECTED_COOLDOWN_MIN for the window.
        #
        # Deliberately in-memory only, NOT persisted to state.json:
        # the failure this fixes (the same token re-selected and
        # re-rejected every ~5-minute cycle for over an hour) is entirely
        # a same-process phenomenon -- an in-memory dict already covers
        # the bot's whole uptime between restarts, which is what actually
        # happened in production. A process restart is infrequent
        # (deploy/crash/reboot, not part of the reported failure pattern)
        # and losing this on restart just costs one possible wasted retry
        # of a since-cooled-down token before it re-enters cooldown on the
        # next rejection -- a minor efficiency cost, not a safety issue;
        # none of the three live-order gates or the daily-loss halt are
        # affected either way. Persisting would mean either growing
        # state.json's schema (shared with PaperEngine's accounting, and
        # rendered to the dashboard) or a whole separate file, for a
        # purely advisory, self-expiring 30-minute-default signal --
        # disproportionate for what it buys here.
        self.rejected_cooldown: Dict[str, float] = {}

    # ------------------------------------------------------------ polymarket-client
    def _builder_api_key(self):
        """BuilderApiKey for the deposit wallet's gasless/relayed order
        path -- read from the same three env vars, and built the same way,
        already proven working by scripts/test_deposit_wallet.py. Only
        ever reached from the real `_aplace_order` code below -- tests
        replace `_place_order` wholesale, so the suite never needs
        credentials or the package installed to import this module."""
        from polymarket import BuilderApiKey
        key = os.environ.get("POLYEDGE_BUILDER_API_KEY")
        secret = os.environ.get("POLYEDGE_BUILDER_SECRET")
        passphrase = os.environ.get("POLYEDGE_BUILDER_PASSPHRASE")
        if not key or not secret or not passphrase:
            raise RuntimeError(
                "POLYEDGE_BUILDER_API_KEY/POLYEDGE_BUILDER_SECRET/"
                "POLYEDGE_BUILDER_PASSPHRASE must all be set -- deposit "
                "wallets are gasless (relayed) accounts and "
                "AsyncSecureClient.create() requires a Builder API Key for "
                "that path. Generate one at polymarket.com/settings?tab=builder.")
        return BuilderApiKey(key=key, secret=secret, passphrase=passphrase)

    async def _aplace_order(self, token_id: str, price: float, shares: float,
                            side: str) -> bool:
        """The actual async order-placement call -- run inside a fresh
        `asyncio.run()` by `_place_order` below. Builds, uses, and closes
        one short-lived AsyncSecureClient per call; see the module
        docstring for why it isn't cached/reused across calls.

        `setup_trading_approvals()` is called before every order, not just
        once at startup -- confirmed idempotent from source (it skips
        allowances already in place), so after the first real call this is
        a cheap on-chain read, not a repeated approval transaction. This
        mirrors the exact sequence already proven end-to-end by
        scripts/test_deposit_wallet.py rather than trying to shortcut it.

        There is no exact price/size limit-order equivalent used here --
        `place_market_order` is called instead, with `max_price`/`min_price`
        set to the risk engine's computed entry price as a cap/floor, so a
        FOK fill can't happen at a worse price than what sized the
        position. This is a considered translation of the old
        OrderArgsV2(price, size, FOK) limit order, not a proven-identical
        one -- flag this if the first live fill's actual price looks off.

        The `place_market_order()` call itself is wrapped in its own
        try/except for `polymarket.errors.PolymarketError` -- confirmed in
        production: a FOK order that couldn't fill raised
        `RequestRejectedError` rather than coming back as a `RejectedOrder`
        response object, and with no handling here that exception
        propagated all the way up through `open_position()` and crashed
        the entire `run_cycle()` for a single rejected order, skipping
        every other queued opportunity that cycle too. `PolymarketError`
        is the base class for the SDK's ENTIRE error hierarchy (confirmed
        from `polymarket/errors.py`: `UserInputError`,
        `UnexpectedResponseError`, `TransportError`, `ConnectionLostError`,
        `RequestRejectedError`, `RateLimitError`, `TimeoutError`,
        `TransactionFailedError`, `CancelledSigningError`,
        `InsufficientLiquidityError`, `SigningError`,
        `InsufficientAllowanceError` -- a flat hierarchy, all direct
        subclasses, no deeper nesting) -- catching only `RequestRejectedError`
        would have left this exact class of crash open for
        `InsufficientLiquidityError` (an expected, common FOK-rejection
        reason) or any of the others. A caught error here is logged and
        treated as "not filled", exactly like a `RejectedOrder` response --
        `open_position`/`close_early` already handle a not-filled leg
        gracefully (log a warning, abort just that position). Anything NOT
        in this hierarchy (a plain `ValueError`/`TypeError`/etc, i.e. an
        actual bug in this module's own code) still propagates uncaught --
        that distinction is deliberate, not an oversight.

        What polymarket-client itself does internally with the price/amount
        we submit -- confirmed from its actual order-building source
        (`polymarket/_internal/actions/orders/market.py`), since a real
        rejection (CV-3290748) happened at a razor-thin ~0.1% margin above
        min_order_size that our own sizing math judged as fine: for a
        max_price-protected BUY (our case, since we always set `max_price`),
        `_compute_market_order_amounts()` first floors (`round_down`, never
        to-nearest) the dollar amount to `RoundingConfig.size` decimal
        places (2, i.e. whole cents, for a 0.001 tick size), THEN divides by
        price to get the share count -- and only rounds that share count UP
        if it has too many decimal places to represent, which protects
        against losing further precision at THAT step but does not restore
        anything already lost from the initial floor. Separately (and
        entirely within this codebase, not the SDK): `risk.py`'s
        min_order_size check runs against the book's raw entry price
        (whatever precision Polymarket's /book endpoint returned), while
        the `round(price, 3)` two lines below submits a SEPARATELY, possibly
        differently, rounded price for the actual order -- two independent
        roundings of what should be the same number. Either mechanism, or
        ordinary price drift between when sizing ran and when the order
        actually executes, is plausible; source alone could not fully
        distinguish between them (see config.MIN_ORDER_SIZE_MARGIN_PCT).
        The log line right after the rounding below exists so the next real
        rejection shows exact numbers instead of requiring another manual
        book-fetch-and-reason-through-it session like this one.
        """
        from polymarket import PRODUCTION, AsyncSecureClient
        from polymarket.errors import PolymarketError
        private_key = os.environ["POLYEDGE_PRIVATE_KEY"]
        client = await AsyncSecureClient.create(
            private_key=private_key, environment=PRODUCTION,
            api_key=self._builder_api_key())
        try:
            await client.setup_trading_approvals()
            price, shares = round(price, 3), round(shares, 2)
            amount = round(price * shares, 2)
            log.info("order inputs after local rounding: token=%s side=%s "
                    "price=%.6f shares=%.6f amount=$%.4f", token_id, side,
                    price, shares, amount)
            try:
                if side == "BUY":
                    resp = await client.place_market_order(
                        token_id=token_id, side="BUY",
                        amount=amount, max_price=price,
                        order_type="FOK")
                else:
                    resp = await client.place_market_order(
                        token_id=token_id, side="SELL",
                        shares=shares, min_price=price, order_type="FOK")
            except PolymarketError as e:
                log.warning("order not filled: token=%s side=%s price=%.6f "
                           "shares=%.6f amount=$%.4f rejected by SDK (%s): %s",
                           token_id, side, price, shares, amount,
                           type(e).__name__, e)
                return False
            filled = await self._resolve_fill(client, resp, token_id)
        finally:
            await client.close()
        if not filled:
            log.warning("order not filled: token=%s side=%s price=%.6f shares=%.6f "
                       "amount=$%.4f ok=%s detail=%s",
                       token_id, side, price, shares, amount,
                       getattr(resp, "ok", None),
                       getattr(resp, "code", None) or getattr(resp, "status", None))
        return filled

    async def _resolve_fill(self, client, resp, token_id: str) -> bool:
        """Decide whether `resp` (from `place_market_order`) represents a
        real fill. Called from inside `_aplace_order`'s still-open client,
        BEFORE `client.close()` -- the "delayed" branch below needs a live
        client to poll with.

        `status == "matched"` is a reliable "filled" signal. `status ==
        "delayed"` is NOT: a real order against this account came back
        `AcceptedOrder(ok=True, status='delayed', making_amount=0,
        taking_amount=0, trade_ids=(), transactions_hashes=())` and was
        independently confirmed to have actually filled -- the synchronous
        response for "delayed" carries no fill data at all, not even
        `trade_ids` (whose own docstring says it's "empty when the order
        did not match" -- that claim does not hold for "delayed"). There is
        no documented reason for this in polymarket-client's source; the
        working theory is that "delayed" means the CLOB accepted the order
        but genuinely can't report the outcome synchronously for this
        account's gasless/relayed (deposit wallet) order path.

        For "delayed", the only way found to positively confirm a fill is
        to poll `list_account_trades()` and look for a trade whose
        `taker_order_id` matches this order's `order_id` (confirmed from
        source: `ClobTrade.taker_order_id` "identifies the originating
        order"). That endpoint has no order-id filter of its own -- its
        `id` query parameter maps to a trade's own id, not an order id
        (confirmed against the request-builder source) -- so this scans
        up to `_DELAYED_FILL_SCAN_LIMIT` of the account's most recent
        trades for `token_id` per attempt, for up to
        `_DELAYED_FILL_POLL_ATTEMPTS` attempts, `_DELAYED_FILL_POLL_DELAY_S`
        apart. Sort order of that endpoint's results was not confirmed
        against source or production -- if it turns out to be oldest-first
        rather than newest-first, a very recent fill could be missed by the
        scan cap on a token with heavy trade volume. Any "live" status (or
        anything else unrecognized) is treated as not filled rather than
        assumed -- a FOK order should never come back resting on the book.

        Returns False (never True) on a poll that can't positively confirm
        a fill -- silently NOT recording a real fill is the failure mode
        this leans toward, since silently recording a fill that didn't
        happen would corrupt paper-accounting state against real money.
        """
        if not getattr(resp, "ok", False):
            return False
        status = getattr(resp, "status", None)
        if status == "matched":
            return True
        if status != "delayed":
            return False
        order_id = getattr(resp, "order_id", None)
        if not order_id:
            return False
        for _ in range(_DELAYED_FILL_POLL_ATTEMPTS):
            await asyncio.sleep(_DELAYED_FILL_POLL_DELAY_S)
            try:
                scanned = 0
                async for trade in client.list_account_trades(token_id=token_id).iter_items():
                    if getattr(trade, "taker_order_id", None) == order_id:
                        return True
                    scanned += 1
                    if scanned >= _DELAYED_FILL_SCAN_LIMIT:
                        break
            except Exception as e:
                log.warning("delayed-fill poll failed for order %s: %s", order_id, e)
        log.warning("delayed order %s never showed up in list_account_trades after %d "
                   "poll(s) -- treating as not filled", order_id, _DELAYED_FILL_POLL_ATTEMPTS)
        return False

    def _check_pusd_balance(self) -> bool:
        """Verify the funder wallet has tradeable pUSD balance BEFORE
        attempting a real order. API/programmatic traders must wrap
        USDC.e into pUSD via the Collateral Onramp's wrap() themselves --
        unlike Polymarket's own UI, the CLOB does not do this for you when
        an order comes in through the API. This bot never calls wrap()
        itself (an infrequent, operator-driven action, not something to
        automate blind) -- it only checks and fails loudly, pointing the
        operator at LIVE.md, rather than silently assuming funds are ready.

        Reads pUSD balance via the same direct on-chain call
        reconcile.fetch_real_pusd_balance() uses -- NOT the SDK's
        get_balance_allowance(), which was tried first and confirmed
        broken for this exact account shape: on a signature_type=1
        (POLY_PROXY) account it returned balance=0, allowance=0 for a
        funder wallet independently confirmed (via this same on-chain
        read) to hold real pUSD. Root cause, confirmed against the SDK's
        actual source: get_balance_allowance()'s request body only ever
        sends `signature_type` (read from self.builder.signature_type,
        i.e. whatever was configured when the client was constructed --
        already correct here) and, optionally, asset_type/token_id -- it
        never sends a funder/address at all.
        BalanceAllowanceParams.signature_type exists as a dataclass field
        but the method body never reads it, so passing it explicitly
        changes nothing; there is no parameter on this call that could
        have fixed this. This is the same SDK bug CLASS as
        Polymarket/py-clob-client-v2#70/#77/#64 (signature-type-aware
        address resolution not honoring the configured funder) showing up
        for POLY_PROXY (1) instead of POLY_1271 (3) -- see LIVE.md for
        the tracking issue.

        Known gap: this only verifies BALANCE, not allowance. There is no
        proven-correct way to check this account's exchange-contract
        pUSD allowance either -- the SDK response above is unreliable,
        and a direct on-chain allowance() call would need a confirmed
        exchange/spender contract address that hasn't been verified. If
        USDC.e was wrapped but the exchange was never approved, this
        check passes but the real order still fails -- that failure
        surfaces as an unfilled order in _place_order, not here. See
        LIVE.md's known-limitations note.

        Deliberately kept unchanged by the polymarket-client switch.
        `AsyncSecureClient.get_balance_allowance(asset_type="COLLATERAL")`
        looks architecturally sound from source (the client is bound to a
        single derived wallet, unlike py-clob-client-v2's broken
        funder-address handling) and would close the allowance gap noted
        above too, but it has never been exercised against this account --
        this switch already carries one unverified real-money surface
        (order placement); stacking a second one (the balance/allowance
        pre-trade gate) the same day was judged not worth it. Revisit once
        the new order-placement path has some real-world confirmation.
        """
        funder = os.environ.get("POLYEDGE_FUNDER_ADDRESS")
        if not funder:
            log.error("pUSD balance check failed: POLYEDGE_FUNDER_ADDRESS not set")
            return False
        try:
            from .reconcile import fetch_real_pusd_balance
            balance = fetch_real_pusd_balance(funder)
        except Exception as e:
            log.error("pUSD balance check failed: %s", e)
            return False
        if balance is None or balance <= 0:
            log.error("pUSD balance check failed: balance=%s -- the funder wallet has no "
                     "tradeable pUSD (or the on-chain check itself failed -- see the log "
                     "line above, if any). USDC.e deposits must be wrapped into pUSD via "
                     "the Collateral Onramp's wrap() before this bot can trade -- it does "
                     "not do that automatically. See LIVE.md.", balance)
            return False
        return True

    def _place_order(self, token_id: str, price: float, shares: float,
                     side: str) -> bool:
        """Submit a real fill-or-kill order. Returns True iff fully filled.

        The only method in this module that ever talks to the network --
        overridden wholesale in tests. Runs `_aplace_order` (the actual
        polymarket-client/AsyncSecureClient call) inside its own
        `asyncio.run()` -- see the module docstring for why a fresh event
        loop and client are used per call instead of caching one.

        A FOK order that can't fully fill comes back as
        `RejectedOrder(ok=False, code="fok_not_filled")` -- straightforward.
        A FOK order that DID fill can come back either as
        `AcceptedOrder(status="matched")` (straightforward) or, on this
        account's gasless/relayed deposit-wallet order path,
        `AcceptedOrder(status="delayed")` with no fill data in the
        response at all -- confirmed against a real production order.
        See `_resolve_fill`'s docstring for the full writeup and how the
        "delayed" case is confirmed via polling instead of trusted blind.
        Never raises a `polymarket.errors.PolymarketError` -- `_aplace_order`
        catches that whole hierarchy around the order-placement call and
        returns False instead; see its docstring for why.
        """
        return asyncio.run(self._aplace_order(token_id, price, shares, side))

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
            # exact numbers going into this specific order, including the
            # book's min_order_size AS SEEN AT SIZING TIME (leg.min_order_size,
            # stashed by risk.py -- see its comment) -- a real rejection
            # (CV-3290748) happened at a razor-thin margin risk.py's own
            # check judged as fine, and diagnosing that required a manual
            # book fetch after the fact; this line means the next one won't.
            log.info("opening %s: BUY token=%s price=%.6f shares=%.6f "
                    "cost=$%.4f min_order_size=%s", opp.key, leg.token_id,
                    leg.entry_price, leg.shares, leg.cost, leg.min_order_size)
            filled = self._place_order(leg.token_id, leg.entry_price, leg.shares, "BUY")
            if not filled:
                log.warning("open_position: BUY order for %s not filled, aborting %s "
                           "(price=%.6f shares=%.6f cost=$%.4f min_order_size=%s)",
                           leg.token_id, opp.key, leg.entry_price, leg.shares,
                           leg.cost, leg.min_order_size)
                self.rejected_cooldown[leg.token_id] = time.time()
                return None
        result = super().open_position(opp, ts=ts)
        if result and config.LIVE_DEFAULT_STOP_LOSS_PCT:
            controls.set_stop_loss(opp.key, config.LIVE_DEFAULT_STOP_LOSS_PCT, self.state_dir)
            log.info("auto-applied %.0f%% stop-loss to %s",
                     config.LIVE_DEFAULT_STOP_LOSS_PCT, opp.key)
        return result

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
