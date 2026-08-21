#!/usr/bin/env python3
"""
################################################################################
#  DANGER — THIS SCRIPT PLACES A REAL ORDER WITH REAL MONEY ($1) THE MOMENT   #
#  IT RUNS TO COMPLETION.                                                     #
#                                                                              #
#    - It is NOT a dry run.                                                   #
#    - It is NOT connected to any of live.py's safety gates -- ARMED,         #
#      POLYEDGE_DRY_RUN, and HALTED do NOT apply to this script at all.       #
#    - It places exactly ONE order and stops. It never loops, never          #
#      retries, and never places a second order under any circumstance.      #
#    - Run it manually, ONCE, with a human directly watching the output.      #
#      NEVER run it from cron, systemd, run_forever.py, or any other          #
#      automation.                                                            #
################################################################################

Purpose
-------
Verify Polymarket's deposit-wallet flow (signature_type=3 / POLY_1271)
actually works end-to-end for this account, via the OFFICIAL
`polymarket-client` SDK (imports as `polymarket`, GitHub Polymarket/py-sdk)
-- NOT py-clob-client-v2, whose deposit-wallet support is confirmed broken
(see LIVE.md and Polymarket/py-clob-client-v2 issues #64, #70, #75, all
still open). This script is a standalone diagnostic, not a rewrite of
live.py -- nothing here is wired into the bot.

What was verified against py-sdk's actual source before writing this
(not guessed):
  - AsyncSecureClient.create() signs LOCALLY: it builds an eth_account
    LocalAccount directly from the private key you give it
    (`Account.from_key(private_key)`) and makes no network call to any
    Privy/Magic signing service. If your exported Magic private key is a
    genuine standalone secp256k1 key (which is the working assumption --
    this bot has been using the same key for years of successful local
    ECDSA signing via py-clob-client-v2's own Signer), this works exactly
    the same way for a deposit wallet as it always has for the proxy
    wallet -- there is no fundamentally different signing requirement.
  - setup_trading_approvals() is confirmed idempotent from source: it
    calls resolve_missing_trading_approval_calls() first and skips
    allowances that are already in place, so it is safe to call again on
    an account that may have partial approvals from an earlier failed
    migration attempt.
  - AsyncSecureClient.create() with no `wallet=` argument derives the
    account's deterministic deposit wallet address and deploys it
    on-chain if it isn't deployed yet.
  - Deposit wallets are gasless (relayed) accounts -- create() raises
    UserInputError unless it's given an `api_key=` credential for that
    relayed-transaction path. Confirmed from source: `api_key` takes an
    `ApiKey` (a `BuilderApiKey | RelayerApiKey` union); `BuilderApiKey`
    is a frozen dataclass with exactly three fields -- `key`, `secret`,
    `passphrase` -- matching the Builder API Key/Secret/Passphrase
    generated at polymarket.com/settings?tab=builder.

Requires (deliberately NOT added to requirements-live.txt -- this is a
one-off manual diagnostic, not a bot dependency):
    pip install polymarket-client==0.2.0

Reads from the environment (the SAME polybert.env already used for the
bot -- does not create a new account, does not ask for a new key, does
not read or write POLYEDGE_FUNDER_ADDRESS, since the deposit wallet is a
DIFFERENT address from the old POLY_PROXY funder, derived fresh by the SDK):
    POLYEDGE_PRIVATE_KEY        the same private key already in use
    POLYEDGE_BUILDER_API_KEY    from polymarket.com/settings?tab=builder
    POLYEDGE_BUILDER_SECRET     (generate a fresh Builder API Key there --
    POLYEDGE_BUILDER_PASSPHRASE  it gives you all three values together)

Usage (source your env file first so the above are all set):
    set -a; source polybert.env; set +a
    python scripts/test_deposit_wallet.py [market-slug]

Optional [market-slug]: the part after /event/ in a Polymarket market's
URL (e.g. for https://polymarket.com/event/some-market, pass
'some-market'). STRONGLY RECOMMENDED -- pick a market you can visually
see is actively trading right now (deep order book, both sides quoted).
Without this, the script falls back to Gamma's "highest 24hr volume"
sort, which already once returned a stale/dead market with no resting
liquidity on one side despite being ranked #1 -- volume sort is not the
same as "has liquidity right now."
"""
import asyncio
import dataclasses
import json
import os
import sys

import requests

GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"
CLOB_BOOK_URL = "https://clob.polymarket.com/book"


def _status(step: str, msg: str) -> None:
    print(f"\n[{step}] {msg}")


def _fail(step: str, msg: str) -> None:
    print(f"\n[{step}] STOPPED: {msg}", file=sys.stderr)
    sys.exit(1)


def _confirm(prompt: str) -> None:
    """Blocking confirmation -- anything other than exactly 'yes' aborts.
    This is a human-watched, one-shot script; there is no retry path."""
    answer = input(f"{prompt} Type 'yes' to continue, anything else to abort: ")
    if answer.strip().lower() != "yes":
        _fail("ABORTED", "user did not confirm -- no order was placed.")


async def main() -> None:
    # ---------------------------------------------------------------- 1/6
    _status("1/6", "Loading credentials from the environment...")
    private_key = os.environ.get("POLYEDGE_PRIVATE_KEY")
    if not private_key or private_key == "0xREPLACE_ME":
        _fail("1/6", "POLYEDGE_PRIVATE_KEY is not set (or is still the template "
             "placeholder). Source polybert.env first:\n"
             "    set -a; source polybert.env; set +a")
    builder_key = os.environ.get("POLYEDGE_BUILDER_API_KEY")
    builder_secret = os.environ.get("POLYEDGE_BUILDER_SECRET")
    builder_passphrase = os.environ.get("POLYEDGE_BUILDER_PASSPHRASE")
    if not builder_key or not builder_secret or not builder_passphrase:
        _fail("1/6", "POLYEDGE_BUILDER_API_KEY / POLYEDGE_BUILDER_SECRET / "
             "POLYEDGE_BUILDER_PASSPHRASE must all be set -- deposit wallets are "
             "gasless (relayed) accounts, and AsyncSecureClient.create() requires "
             "a Builder API Key for that. Generate one at "
             "polymarket.com/settings?tab=builder, then source polybert.env:\n"
             "    set -a; source polybert.env; set +a")
    print("    private key and builder API credentials loaded from environment "
         "(values not printed)")

    try:
        from polymarket import PRODUCTION, AsyncSecureClient, BuilderApiKey
    except ImportError:
        _fail("1/6", "polymarket-client is not installed. Run:\n"
             "    pip install polymarket-client==0.2.0")
        return  # unreachable, keeps type-checkers happy

    # ---------------------------------------------------------------- 2/6
    _status("2/6", "Creating AsyncSecureClient and deriving the deposit wallet "
                   "address (private-key signing happens LOCALLY -- no Privy/Magic "
                   "network call; the builder key below is only for the gasless/"
                   "relayed-transaction path, not for signing)...")
    client = None
    try:
        client = await AsyncSecureClient.create(
            private_key=private_key, environment=PRODUCTION,
            api_key=BuilderApiKey(key=builder_key, secret=builder_secret,
                                  passphrase=builder_passphrase))
    except Exception as e:
        _fail("2/6", f"AsyncSecureClient.create() raised: {e!r}")

    deposit_wallet = client.wallet
    print(f"    Deposit wallet address: {deposit_wallet}")
    _confirm(
        f"    >>> Does '{deposit_wallet}' match the address Polymarket's own "
        f"'Upgrade your account' popup showed you earlier (0x4e198C22...A0266868)?")

    # ---------------------------------------------------------------- 3/6
    _status("3/6", "Calling setup_trading_approvals() (confirmed idempotent from "
                   "source -- already-approved allowances are skipped, so this is "
                   "safe even if an earlier migration attempt partially ran)...")
    try:
        handle = await client.setup_trading_approvals()
    except Exception as e:
        await client.close()
        _fail("3/6", f"setup_trading_approvals() raised: {e!r}")
    print(f"    setup_trading_approvals() returned: {handle!r}")

    # ---------------------------------------------------------------- 4/6
    slug = sys.argv[1] if len(sys.argv) > 1 else None
    if slug:
        _status("4/6", f"Fetching market by slug override: {slug!r} "
                       "(from command-line argument -- skipping auto-selection)...")
        try:
            r = requests.get(GAMMA_EVENTS_URL, params={"slug": slug}, timeout=15)
            r.raise_for_status()
            events = r.json()
        except Exception as e:
            await client.close()
            _fail("4/6", f"Gamma API request for slug {slug!r} failed: {e!r}")
        if not events:
            await client.close()
            _fail("4/6", f"No event found for slug {slug!r} -- double-check the "
                 "slug from the market's URL (the part after /event/).")
    else:
        _status("4/6", "No market slug given -- auto-selecting the highest-volume "
                       "active market from the public Gamma API. NOTE: this "
                       "already once returned a stale/illiquid market (best bid "
                       "0.001, no ask at all) despite being '#1 by volume24hr' -- "
                       "prefer passing a slug you can see is actively trading:\n"
                       "    python scripts/test_deposit_wallet.py <slug-from-url>\n"
                       "e.g. for https://polymarket.com/event/some-market, pass "
                       "'some-market'. Proceeding with auto-selection since none "
                       "was given...")
        try:
            r = requests.get(GAMMA_EVENTS_URL, params={
                "closed": "false", "active": "true", "archived": "false",
                "limit": 5, "order": "volume24hr", "ascending": "false",
            }, timeout=15)
            r.raise_for_status()
            events = r.json()
        except Exception as e:
            await client.close()
            _fail("4/6", f"Gamma API request failed: {e!r}")

    market = None
    for ev in events if isinstance(events, list) else []:
        for m in ev.get("markets", []) or []:
            if m.get("closed") or not m.get("active", True):
                continue
            market = m
            break
        if market:
            break
    if market is None:
        await client.close()
        _fail("4/6", "No open, active market found in the Gamma results.")

    raw_tokens = market.get("clobTokenIds")
    if isinstance(raw_tokens, str):
        try:
            raw_tokens = json.loads(raw_tokens)
        except ValueError:
            raw_tokens = None
    if not raw_tokens:
        await client.close()
        _fail("4/6", f"Market {market.get('question')!r} has no usable clobTokenIds.")
    yes_token = str(raw_tokens[0])
    print(f"    Market: {market.get('question')!r}")
    print(f"    YES token_id: {yes_token}")

    try:
        br = requests.get(CLOB_BOOK_URL, params={"token_id": yes_token}, timeout=15)
        br.raise_for_status()
        book = br.json()
        bids, asks = book.get("bids") or [], book.get("asks") or []
        best_bid = bids[0].get("price", "?") if bids else "NONE"
        best_ask = asks[0].get("price", "?") if asks else "NONE"
        print(f"    Current best bid/ask: {best_bid} / {best_ask} "
             "(informational only -- the market order below sweeps the book "
             "near this price, it does not use these values directly)")
        if not bids or not asks:
            print("    !! WARNING: one side of the book is completely empty -- "
                 "this looks like a dead/illiquid market. A market order here "
                 "will very likely fail with InsufficientLiquidityError, same as "
                 "the auto-selected one did. Consider Ctrl+C now and re-running "
                 "with a slug from a market you can see is actively trading.")
    except Exception as e:
        print(f"    (could not fetch order book for display -- {e!r} -- "
             "continuing anyway, this is not fatal)")

    # ---------------------------------------------------------------- 5/6
    _status("5/6", f"About to place ONE real $1 market BUY order (FAK -- "
                   f"fill-and-kill, py-sdk's recommended fast-fill order type) "
                   f"on token {yes_token}.")
    _confirm("    >>> THIS IS A REAL ORDER WITH REAL MONEY. Confirm you want to proceed.")
    try:
        resp = await client.place_market_order(token_id=yes_token, side="BUY", amount=1)
    except Exception as e:
        print(f"\n[5/6] place_market_order() raised (this IS the real result -- "
             f"not swallowed or reinterpreted): {e!r}")
        await client.close()
        sys.exit(1)

    # ---------------------------------------------------------------- 6/6
    _status("6/6", "Raw response from place_market_order (exactly as returned):")
    try:
        print(dataclasses.asdict(resp))
    except TypeError:
        print(repr(resp))

    await client.close()
    print("\nDone. This script does not loop or retry -- run it again manually "
         "if you want to test another single order.")


if __name__ == "__main__":
    asyncio.run(main())
