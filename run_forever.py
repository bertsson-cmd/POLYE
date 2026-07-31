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
