"""Shared manual-control state for live trading, plus the orchestration
that acts on it each cycle.

state/controls.json is the single source of truth the control panel
(control_server.py) writes to and run_forever.py reads from before every
scan cycle via apply_controls(). Atomic writes (write temp, rename), safe
defaults on a missing or corrupt file -- a mid-edit or truncated control
file must never crash the bot or be silently misread as "everything is
fine."

Every liquidation path here (kill switch, the manual queue, the
allocation cap, per-position stop-loss) only ever touches single-leg
positions. Multi-leg ARB/REL locks are never force-unwound -- selling one
leg of a guarantee strands the other -- and that limitation is always
logged plainly rather than silently skipped.
"""
import json
import logging
import os
import tempfile
from typing import Optional

from . import config

log = logging.getLogger("polyedge.controls")

DEFAULTS = {
    "paused": False,
    "kill_switch": False,
    "max_allocation_usd": None,   # None = no cap
    "liquidate_queue": [],        # position keys queued for manual liquidation
    "stop_loss_pct": {},          # position key -> loss threshold, 0-100 (percent of cost basis)
}


def _path(state_dir: Optional[str] = None) -> str:
    state_dir = state_dir or config.STATE_DIR
    return os.path.join(state_dir, "controls.json")


def load(state_dir: Optional[str] = None) -> dict:
    path = _path(state_dir)
    if os.path.exists(path):
        try:
            with open(path) as f:
                data = json.load(f)
            if isinstance(data, dict):
                out = {k: v for k, v in DEFAULTS.items()}
                out.update({k: v for k, v in data.items() if k in DEFAULTS})
                return out
        except (json.JSONDecodeError, OSError):
            log.warning("controls.json unreadable/corrupt -- using safe defaults")
    return {k: (dict(v) if isinstance(v, dict) else list(v) if isinstance(v, list) else v)
           for k, v in DEFAULTS.items()}


def save(state: dict, state_dir: Optional[str] = None) -> None:
    state_dir = state_dir or config.STATE_DIR
    os.makedirs(state_dir, exist_ok=True)
    clean = dict(DEFAULTS)
    clean.update({k: v for k, v in state.items() if k in DEFAULTS})
    fd, tmp = tempfile.mkstemp(dir=state_dir, suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(clean, f, indent=1)
    os.replace(tmp, _path(state_dir))


# ---------------------------------------------------------------- mutators
# Each of these loads, mutates one field, and saves atomically -- callers
# (the Flask routes) never touch the file shape directly.

def set_paused(paused: bool, state_dir: Optional[str] = None) -> dict:
    st = load(state_dir)
    st["paused"] = bool(paused)
    save(st, state_dir)
    return st


def set_kill_switch(on: bool, state_dir: Optional[str] = None) -> dict:
    st = load(state_dir)
    st["kill_switch"] = bool(on)
    save(st, state_dir)
    return st


def set_max_allocation(usd: Optional[float], state_dir: Optional[str] = None) -> dict:
    st = load(state_dir)
    st["max_allocation_usd"] = float(usd) if usd is not None else None
    save(st, state_dir)
    return st


def queue_liquidate(key: str, state_dir: Optional[str] = None) -> dict:
    st = load(state_dir)
    if key not in st["liquidate_queue"]:
        st["liquidate_queue"].append(key)
    save(st, state_dir)
    return st


def clear_liquidate_queue(state_dir: Optional[str] = None) -> dict:
    st = load(state_dir)
    st["liquidate_queue"] = []
    save(st, state_dir)
    return st


def set_stop_loss(key: str, pct: Optional[float], state_dir: Optional[str] = None) -> dict:
    """pct is a percentage (0-100) of cost-basis loss that triggers an
    automatic single-position liquidation -- e.g. 20 means "liquidate if
    this position is down 20% from its entry cost." pct None (or <= 0)
    clears/disables the stop-loss for that position."""
    st = load(state_dir)
    if pct is None or float(pct) <= 0:
        st["stop_loss_pct"].pop(key, None)
    else:
        st["stop_loss_pct"][key] = max(0.0, min(float(pct), 100.0))
    save(st, state_dir)
    return st


# ---------------------------------------------------------------- orchestration
def _eligible_single_leg(engine):
    return [p for p in engine.state["positions"] if len(p["legs"]) == 1]


def _fresh_bids(client, positions) -> dict:
    """token_id -> best live bid, for the single leg of each given position."""
    token_ids = {p["legs"][0]["token_id"] for p in positions}
    if not token_ids:
        return {}
    books = client.fetch_books(token_ids)
    out = {}
    for tid, book in books.items():
        bid = book.best_bid()
        if bid is not None:
            out[tid] = bid
    return out


def apply_controls(engine, client, state_dir: Optional[str] = None) -> dict:
    """Run once per run_forever.py cycle, BEFORE the normal scan/trade
    cycle. Acts on state/controls.json: kill-switch liquidation, draining
    the manual liquidate queue, enforcing the allocation cap, and
    enforcing per-position stop-losses.

    `engine` must expose LiveEngine's liquidate_position/liquidate_all --
    this is only meaningful against a live deployment. `client` must
    expose fetch_books(token_ids) -> {token_id: OrderBook}.
    """
    state_dir = state_dir or engine.state_dir
    ctrl = load(state_dir)
    summary = {"killed": [], "liquidated": [], "stop_loss": [], "skipped": []}

    # ---- kill switch: liquidate everything eligible ----
    if ctrl["kill_switch"]:
        eligible = _eligible_single_leg(engine)
        bids = _fresh_bids(client, eligible)
        exit_prices = {p["legs"][0]["token_id"]: bids[p["legs"][0]["token_id"]]
                       for p in eligible if p["legs"][0]["token_id"] in bids}
        closed, skipped = engine.liquidate_all(exit_prices, reason="kill_switch")
        summary["killed"] = [c["key"] for c in closed]
        summary["skipped"].extend(skipped)
        if skipped:
            log.warning("kill switch: %d position(s) could not be liquidated: %s",
                       len(skipped), skipped)
        engine.save()

    # ---- drain the manual per-position liquidate queue ----
    if ctrl["liquidate_queue"]:
        queue = list(ctrl["liquidate_queue"])
        positions = {p["key"]: p for p in engine.state["positions"]}
        candidates = [positions[k] for k in queue
                     if k in positions and len(positions[k]["legs"]) == 1]
        bids = _fresh_bids(client, candidates)
        for key in queue:
            pos = positions.get(key)
            if pos is None:
                summary["skipped"].append((key, "position not open"))
                continue
            if len(pos["legs"]) > 1:
                summary["skipped"].append((key, "multi-leg lock -- cannot be forced"))
                continue
            tid = pos["legs"][0]["token_id"]
            if tid not in bids:
                summary["skipped"].append((key, "no current price available"))
                continue
            c = engine.liquidate_position(key, {tid: bids[tid]}, reason="manual_liquidate")
            if c:
                summary["liquidated"].append(key)
            else:
                summary["skipped"].append((key, "order not filled or gates closed"))
        clear_liquidate_queue(state_dir)
        engine.save()

    # ---- allocation cap: liquidate largest-cost-first until back under it ----
    cap = ctrl["max_allocation_usd"]
    if cap is not None:
        total = engine.total_open_cost()
        if total > cap:
            eligible = sorted(_eligible_single_leg(engine), key=lambda p: -p["cost"])
            bids = _fresh_bids(client, eligible)
            for pos in eligible:
                if total <= cap:
                    break
                tid = pos["legs"][0]["token_id"]
                if tid not in bids:
                    continue
                c = engine.liquidate_position(pos["key"], {tid: bids[tid]}, reason="allocation_cap")
                if c:
                    total -= pos["cost"]
                    summary["liquidated"].append(pos["key"])
            if total > cap:
                log.warning("allocation cap $%.2f still exceeded (now $%.2f) -- "
                           "remaining exposure is locked in multi-leg positions, "
                           "which are never forced under the cap", cap, total)
        engine.save()

    # ---- per-position stop-loss ----
    if ctrl["stop_loss_pct"]:
        watched = [p for p in _eligible_single_leg(engine)
                  if p["key"] in ctrl["stop_loss_pct"]]
        bids = _fresh_bids(client, watched)
        for pos in watched:
            tid = pos["legs"][0]["token_id"]
            bid = bids.get(tid)
            if bid is None:
                continue
            entry = pos["legs"][0]["entry_price"]
            if entry <= 0:
                continue
            loss_pct = (1.0 - bid / entry) * 100.0
            threshold = ctrl["stop_loss_pct"][pos["key"]]
            if loss_pct >= threshold:
                c = engine.liquidate_position(pos["key"], {tid: bid}, reason="stop_loss")
                if c:
                    summary["stop_loss"].append(pos["key"])
                    log.warning("stop-loss triggered for %s: down %.1f%% (threshold %.1f%%)",
                               pos["key"], loss_pct, threshold)
        engine.save()

    return summary
