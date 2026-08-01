"""Wallet reconciliation — compares the bot's own bookkeeping (state cash
+ open positions) against Polymarket's actual on-chain record for the
funder wallet, using Polymarket's public Data API for positions and a
direct Polygon RPC read for the pUSD balance.

This automates the manual habit of "check the dashboard against the real
wallet by hand" — if the bot's internal model of reality ever splits
from actual reality (a bug, a partial fill it mis-recorded, a missed
resolution event), this catches it within one scan cycle instead of
relying on someone remembering to look. A large enough divergence trips
the same HALTED mechanism as the daily-loss circuit breaker: continuing
to trade on state that no longer matches the real wallet is exactly the
kind of thing that should stop and wait for a human, not push forward.

Network failures here are treated as "couldn't check this cycle", never
as a reason to raise/crash a trading cycle — this is a safety net, and a
safety net that can itself take down the system on a network hiccup is
worse than no safety net.

Deliberately stays on a plain, keyless requests.Session/RPC read rather
than py-clob-client-v2's own get_balance_allowance() -- that call needs a
fully authenticated client (private key + derived L1/L2 API creds), which
is a meaningfully bigger dependency for what's supposed to be an
independent, low-privilege sanity check. live.py's own pre-trade check
(_check_pusd_balance in live.py) uses get_balance_allowance() instead,
since it already has an authenticated client at that point anyway and
that call also verifies exchange-contract allowance, not just balance.
"""
import logging
import os
from typing import Optional

import requests

log = logging.getLogger("polyedge.reconcile")

DATA_API_POSITIONS_URL = "https://data-api.polymarket.com/positions"

# polygon-rpc.com (the original Polygon-Foundation-run public endpoint)
# was deprecated on July 31, 2026 -- confirmed via the official Polygon
# forum deprecation notice, and started 401ing the day after. A second
# attempt at a replacement (polygon.llamarpc.com) turned out to be a
# dead hostname (NXDOMAIN) despite appearing correct in third-party
# documentation -- search results are not verification. This one
# (polygon.publicnode.com) was confirmed differently: curl'd directly
# from the VPS and got a real HTTP 405 with `allow: OPTIONS, POST` --
# the actual fingerprint of a live JSON-RPC endpoint responding to a
# HEAD request, not just a plausible-looking URL from a search result.
# Made env-overridable regardless, since free public endpoints are
# exactly the kind of thing that keeps changing -- if reconcile logs
# start showing "could not fetch real pUSD balance" again, curl the
# current URL directly from the VPS before assuming anything else is
# wrong, the same way this one was actually verified.
POLYGON_RPC_URL = os.environ.get("POLYEDGE_POLYGON_RPC_URL", "https://polygon.publicnode.com")

# pUSD (Polymarket USD) on Polygon -- the V2 collateral token that
# replaced USDC.e at the CLOB V2 cutover (April 28, 2026). Address
# confirmed against the pUSD token's own PolygonScan listing during the
# V2 rewrite of this module. VERIFY THIS against your own wallet's actual
# balance the first time reconciliation runs after any Polymarket
# collateral migration: a wrong contract address here won't error, it
# will just silently report a $0 balance forever, which would look like
# total divergence.
PUSD_CONTRACT_ADDRESS = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
_BALANCE_OF_SELECTOR = "0x70a08231"  # keccak4("balanceOf(address)")
# pUSD's decimal count could NOT be independently confirmed during the V2
# research pass (Polymarket's own docs were unreachable from the research
# environment). USDC.e used 6 decimals and pUSD is described as a 1:1
# continuity token for it, so this assumes 6 as the most likely value --
# cross-check the very first real reconcile result against your wallet's
# actual pUSD balance shown in Polymarket's own UI before trusting the
# halt-threshold math, and fix this constant if they don't match.
_PUSD_DECIMALS = 6


def fetch_real_positions(funder_address: str,
                         session: Optional[requests.Session] = None) -> Optional[list]:
    """Real, current on-chain positions for this wallet from Polymarket's
    public Data API (no auth required). Returns None (not []) on any
    network/parse error, so callers can tell "checked, has zero
    positions" apart from "couldn't check right now"."""
    http = session or requests.Session()
    try:
        r = http.get(DATA_API_POSITIONS_URL, params={"user": funder_address}, timeout=10)
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else None
    except Exception as e:
        log.warning("reconcile: could not fetch real positions -- %s", e)
        return None


def fetch_real_pusd_balance(funder_address: str,
                            session: Optional[requests.Session] = None) -> Optional[float]:
    """Real pUSD balance for this wallet, read directly from the Polygon
    chain via a raw eth_call -- a plain read call, no API key needed."""
    http = session or requests.Session()
    padded = funder_address.lower().replace("0x", "").rjust(64, "0")
    call_data = _BALANCE_OF_SELECTOR + padded
    payload = {
        "jsonrpc": "2.0", "id": 1, "method": "eth_call",
        "params": [{"to": PUSD_CONTRACT_ADDRESS, "data": call_data}, "latest"],
    }
    try:
        r = http.post(POLYGON_RPC_URL, json=payload, timeout=10)
        r.raise_for_status()
        result = r.json().get("result")
        if not result or result == "0x":
            return None
        return int(result, 16) / (10 ** _PUSD_DECIMALS)
    except Exception as e:
        log.warning("reconcile: could not fetch real pUSD balance -- %s", e)
        return None


def check(state: dict, funder_address: str,
         session: Optional[requests.Session] = None,
         halt_threshold_pct: float = 15.0) -> dict:
    """Compare the bot's own bookkeeping against the real wallet.

    Always returns a plain, serializable dict -- 'ok': False means the
    check itself couldn't complete (network issue), which is NOT the
    same as 'exceeded_threshold': True (checked successfully, found a
    real divergence). Callers should only act on the latter."""
    bot_cash = state.get("cash", 0.0)
    bot_open_value = sum(p.get("current_value", 0.0) for p in state.get("positions", []))
    bot_equity = round(bot_cash + bot_open_value, 4)

    real_positions = fetch_real_positions(funder_address, session)
    real_pusd = fetch_real_pusd_balance(funder_address, session)

    if real_positions is None or real_pusd is None:
        return {"ok": False, "reason": "could not reach network",
                "bot_equity": bot_equity}

    real_positions_value = sum(p.get("currentValue", 0.0) or 0.0 for p in real_positions)
    real_equity = round(real_pusd + real_positions_value, 4)

    diff = round(bot_equity - real_equity, 4)
    if real_equity > 0:
        diff_pct = round(abs(diff) / real_equity * 100.0, 2)
    else:
        # real wallet reads as empty -- any bot equity at all is 100%
        # divergence, not a divide-by-zero to paper over
        diff_pct = 0.0 if abs(diff) < 0.01 else 100.0

    exceeded = diff_pct > halt_threshold_pct
    if exceeded:
        log.error("reconcile: DIVERGENCE %.1f%% (threshold %.1f%%) -- bot "
                  "equity $%.2f vs real wallet $%.2f (pusd $%.2f + "
                  "positions $%.2f across %d real position(s))",
                  diff_pct, halt_threshold_pct, bot_equity, real_equity,
                  real_pusd, real_positions_value, len(real_positions))

    return {
        "ok": True, "exceeded_threshold": exceeded,
        "bot_equity": bot_equity, "real_equity": real_equity,
        "real_pusd": round(real_pusd, 4),
        "real_positions_value": round(real_positions_value, 4),
        "diff": diff, "diff_pct": diff_pct,
        "real_position_count": len(real_positions),
        "bot_position_count": len(state.get("positions", [])),
    }
