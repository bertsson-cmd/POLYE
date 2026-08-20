"""Continuous polling loop -- the live-deployment replacement for the
GitHub Actions cron (`.github/workflows/polyedge.yml`). That workflow is
tied to committing state/docs back to the repo for GitHub Pages, which is
the right shape for paper trading but not for a VPS running real money.
Run this under systemd (see polybert.service) instead.

    python run_forever.py                   # paper engine (default)
    POLYEDGE_LIVE=1 python run_forever.py    # select the live engine

Selecting the live engine here is only ONE of the gates live.py requires
before it will place a real order -- you also need an ARMED file in the
working directory and POLYEDGE_DRY_RUN=0. See LIVE.md before touching any
of this on a real wallet.

Env vars:
    POLYEDGE_INTERVAL_SEC   seconds between scan cycles (default 300, i.e.
                             5 minutes, matching the paper cadence)
    POLYEDGE_LIVE           "1" selects LiveEngine, anything else PaperEngine
"""
import logging
import os
import re
import signal
import sys
import time

from polyedge import config
from polyedge.api import PolymarketClient
from polyedge.main import run_cycle
from polyedge.paper import PaperEngine

log = logging.getLogger("polyedge.run_forever")

_stop = False
_cycle_count = 0
_ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


def _handle_signal(signum, _frame):
    global _stop
    log.info("received signal %d, stopping after the current cycle", signum)
    _stop = True


def _make_engine():
    if os.environ.get("POLYEDGE_LIVE") == "1":
        from polyedge.live import LiveEngine
        log.warning("LIVE engine selected (POLYEDGE_LIVE=1) -- see LIVE.md for "
                   "the three-gate safety model before this can place a real order")
        return LiveEngine()
    return PaperEngine()


def _geoblock_startup_check_passes() -> bool:
    """Refuse to start live trading if Polymarket reports this machine's
    outbound connection as geoblocked -- real incident, see LIVE.md
    section 3: a VPS's outbound HTTPS was resolving polymarket.com over
    IPv6 by default, and that specific IPv6 address geolocated to a
    blocked region even though the server's real (IPv4) location was
    fine, silently rejecting every live order with "Trading restricted
    in your region" until diagnosed by hand. This check exists so that
    failure mode is loud, at startup, instead of silent and per-order.

    Checks BOTH default resolution and forced-IPv4 so the log message
    can distinguish "this looks like the known IPv6 routing issue" from
    "this may be a genuine account/region restriction" -- see
    polyedge.api.check_geoblock's docstring. Returns True (proceed) if
    the check itself couldn't be completed at all (a network failure at
    startup is not, by itself, proof of geoblocking) or if neither
    result reports blocked=True."""
    from polyedge.api import check_geoblock
    default_result = check_geoblock()
    ipv4_result = check_geoblock(force_ipv4=True)
    if default_result is None and ipv4_result is None:
        log.warning("geoblock check could not be completed (network failure on "
                   "both attempts) -- proceeding, but this could not confirm "
                   "outbound trading isn't region-blocked; see LIVE.md section 3")
        return True
    default_blocked = bool(default_result and default_result.get("blocked"))
    ipv4_blocked = bool(ipv4_result and ipv4_result.get("blocked"))
    country = (default_result or ipv4_result or {}).get("country")
    if not default_blocked and not ipv4_blocked:
        log.info("geoblock check passed (country=%s, not blocked)", country)
        return True
    if default_blocked and ipv4_result is not None and not ipv4_blocked:
        log.error(
            "REFUSING TO START LIVE TRADING: default outbound resolution is "
            "geoblocked (country=%s) but forcing IPv4 is NOT blocked "
            "(country=%s) -- this is the exact IPv6 routing/geolocation "
            "mismatch documented in LIVE.md section 3. Fix the server's "
            "IPv4 resolution precedence (see LIVE.md) and restart.",
            country, ipv4_result.get("country"))
    else:
        log.error(
            "REFUSING TO START LIVE TRADING: outbound connection is "
            "geoblocked (country=%s) -- this does NOT clearly look like the "
            "IPv6 routing issue documented in LIVE.md section 3 (forcing "
            "IPv4 did not confirm the connection is unblocked); this may be "
            "a genuine account/region restriction. Investigate before "
            "restarting.", country)
    return False


def _apply_controls(engine, client):
    """No-op until the control panel (controls.py / control_server.py) is
    wired up. Kept as a separate call so that landing does not require
    touching the main loop again."""
    try:
        from polyedge.controls import apply_controls
    except ImportError:
        return
    apply_controls(engine, client)


def _maybe_reconcile(engine):
    """Compare the bot's own bookkeeping against the real wallet, every
    RECONCILE_EVERY_N_CYCLES cycles. No-op entirely if no funder address
    is configured, OR if it doesn't look like a real 0x-prefixed 40-hex
    address (e.g. still the "0xREPLACE_ME" template placeholder) -- that
    is "not set up yet", not a network failure, and should not generate
    a failed API call and a warning log line every single cycle."""
    funder = os.environ.get("POLYEDGE_FUNDER_ADDRESS", "")
    if not config.RECONCILE_ENABLED or not _ADDR_RE.match(funder):
        return None
    if _cycle_count % max(1, config.RECONCILE_EVERY_N_CYCLES) != 0:
        return None
    from polyedge import reconcile, live
    result = reconcile.check(engine.state, funder,
                             halt_threshold_pct=config.RECONCILE_HALT_THRESHOLD_PCT)
    engine.state["last_reconcile"] = result
    engine.save()
    if result.get("exceeded_threshold"):
        live.write_halt(
            f"wallet reconciliation divergence {result['diff_pct']:.1f}% exceeds "
            f"{config.RECONCILE_HALT_THRESHOLD_PCT:.1f}% threshold -- bot equity "
            f"${result['bot_equity']:.2f} vs real wallet ${result['real_equity']:.2f}")
    return result


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    interval = float(os.environ.get("POLYEDGE_INTERVAL_SEC", 300))
    client = PolymarketClient()
    engine = _make_engine()
    log.info("run_forever starting: engine=%s interval=%.0fs",
            type(engine).__name__, interval)

    if os.environ.get("POLYEDGE_LIVE") == "1" and not _geoblock_startup_check_passes():
        return 1

    global _cycle_count
    while not _stop:
        cycle_start = time.time()
        try:
            _apply_controls(engine, client)
            summary = run_cycle(client, engine)
            log.info("cycle done: %s", summary)
            recon = _maybe_reconcile(engine)
            if recon is not None:
                log.info("reconcile: %s", recon)
        except Exception:
            log.exception("cycle failed -- will retry next interval")
        _cycle_count += 1
        elapsed = time.time() - cycle_start
        sleep_for = max(1.0, interval - elapsed)
        remaining = sleep_for
        while remaining > 0 and not _stop:
            step = min(1.0, remaining)
            time.sleep(step)
            remaining -= step

    log.info("run_forever stopped cleanly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
