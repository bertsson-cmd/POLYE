"""Wallet reconciliation — compares the bot's own bookkeeping (state cash
+ open positions) against Polymarket's actual on-chain record for the
funder wallet, using Polymarket's public Data API for positions and a
direct Polygon RPC read for the USDC balance.

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
"""
import logging
from typing import Optional

import requests

log = logging.getLogger("polyedge.reconcile")

DATA_API_POSITIONS_URL = "https://data-api.polymarket.com/positions"
POLYGON_RPC_URL = "https://polygon-rpc.com"

# USDC.e (bridged USDC) on Polygon -- Polymarket's historical funding
# token. VERIFY THIS against your own wallet's actual balance the first
# time reconciliation runs: Polymarket has changed funding tokens before,
# and a wrong contract address here won't error, it will just silently
# report a $0 balance forever, which would look like total divergence.
USDC_CONTRACT_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
_BALANCE_OF_SELECTOR = "0x70a08231"  # keccak4("balanceOf(address)")


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


def fetch_real_usdc_balance(funder_address: str,
                            session: Optional[requests.Session] = None) -> Optional[float]:
    """Real USDC balance for this wallet, read directly from the Polygon
    chain via a raw eth_call -- a plain read call, no API key needed."""
    http = session or requests.Session()
    padded = funder_address.lower().replace("0x", "").rjust(64, "0")
    call_data = _BALANCE_OF_SELECTOR + padded
    payload = {
        "jsonrpc": "2.0", "id": 1, "method": "eth_call",
        "params": [{"to": USDC_CONTRACT_ADDRESS, "data": call_data}, "latest"],
    }
    try:
        r = http.post(POLYGON_RPC_URL, json=payload, timeout=10)
        r.raise_for_status()
        result = r.json().get("result")
        if not result or result == "0x":
            return None
        return int(result, 16) / 1e6   # USDC has 6 decimals
    except Exception as e:
        log.warning("reconcile: could not fetch real USDC balance -- %s", e)
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
    real_usdc = fetch_real_usdc_balance(funder_address, session)

    if real_positions is None or real_usdc is None:
        return {"ok": False, "reason": "could not reach network",
                "bot_equity": bot_equity}

    real_positions_value = sum(p.get("currentValue", 0.0) or 0.0 for p in real_positions)
    real_equity = round(real_usdc + real_positions_value, 4)

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
                  "equity $%.2f vs real wallet $%.2f (usdc $%.2f + "
                  "positions $%.2f across %d real position(s))",
                  diff_pct, halt_threshold_pct, bot_equity, real_equity,
                  real_usdc, real_positions_value, len(real_positions))

    return {
        "ok": True, "exceeded_threshold": exceeded,
        "bot_equity": bot_equity, "real_equity": real_equity,
        "real_usdc": round(real_usdc, 4),
        "real_positions_value": round(real_positions_value, 4),
        "diff": diff, "diff_pct": diff_pct,
        "real_position_count": len(real_positions),
        "bot_position_count": len(state.get("positions", [])),
    }
