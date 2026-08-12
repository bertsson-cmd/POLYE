"""PolyEdge 95 v2 — central configuration.

Every tunable knob lives here. Edit this file (or set environment variables
with the same names) — you should never need to touch strategy code.
"""
import os
from typing import Optional


def _f(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def _i(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def _f_or_none(name: str, default: Optional[float]) -> Optional[float]:
    raw = os.environ.get(name)
    return default if raw is None else float(raw)


# ---------------------------------------------------------------- general
MODE = os.environ.get("POLYEDGE_MODE", "paper")   # "paper" only (live = manual)
STARTING_BANKROLL = _f("POLYEDGE_BANKROLL", 1000.0)  # USD (paper money)
# Per-category taker fee rates live in polyedge/fees.py (Polymarket's real
# fee schedule). This is an ESCAPE HATCH for stress-testing "what if fees
# rise to X%" without touching the category table -- unset (None) means
# "use fees.py's category table", not "no fee".
FEE_RATE_OVERRIDE = _f_or_none("POLYEDGE_FEE_RATE_OVERRIDE", None)
STATE_DIR = os.environ.get("POLYEDGE_STATE_DIR", "state")
DOCS_DIR = os.environ.get("POLYEDGE_DOCS_DIR", "docs")

# ---------------------------------------------------------------- API
GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"
HTTP_TIMEOUT = 15          # seconds per request
# Reject markets resolving sooner than this many days from now. Guards
# against past-dated / stale markets (negative days) and markets so close
# to resolution they're effectively already decided-and-frozen. A small
# positive floor also protects the CONVERGE annualized-yield division.
MIN_DAYS_TO_RESOLUTION = _f("POLYEDGE_MIN_DAYS", 0.05)  # ~1.2 hours
HTTP_RETRIES = 3
MAX_EVENTS_PER_SCAN = _i("POLYEDGE_MAX_EVENTS", 600)
BOOK_FETCH_WORKERS = _i("POLYEDGE_BOOK_WORKERS", 20)      # concurrent CLOB requests
MAX_BOOKS_PER_SCAN = _i("POLYEDGE_MAX_BOOKS", 800)        # hard cap so a scan can't run forever
CV_BOOK_RESERVE_PCT = _f("POLYEDGE_CV_BOOK_RESERVE", 0.40)  # share of book budget reserved for CONVERGE candidates

# ---------------------------------------------------------------- risk
MAX_POSITION_PCT = _f("POLYEDGE_MAX_POS_PCT", 0.025)      # max 2.5% of bankroll per position (smaller tickets, more of them)
MAX_TOTAL_EXPOSURE_PCT = _f("POLYEDGE_MAX_EXPO_PCT", 0.60) # max 60% of bankroll deployed
MAX_STRATEGY_EXPOSURE_PCT = {                              # per-strategy caps
    "ARB": 0.30,
    "REL": 0.20,
    "LONGSHOT": 0.05,     # reduced from 0.15 — fewer, smaller longshot fades
    "CONVERGE": 0.35,     # raised from 0.25 — this is now the "many small wins" workhorse
}
KELLY_FRACTION = _f("POLYEDGE_KELLY_FRACTION", 0.25)       # quarter-Kelly (conservative)
MIN_TICKET = _f("POLYEDGE_MIN_TICKET", 5.0)                # skip trades smaller than $5
MAX_POSITION_ABS_USD = _f("POLYEDGE_MAX_POS_ABS", 100.0)   # absolute $ ceiling on ANY single position (final backstop)
# Confirmed in production: Polymarket's exchange-enforced min_order_size is
# denominated in SHARES, not dollars -- a flat $5 CONVERGE ticket near
# 0.95-0.99 buys only ~5.05-5.3 shares, right at or barely above typical
# 1-5 share floors seen on real books, and gets rejected outright even
# against a deep book (FOK/FAK orders don't rest, so the larger 5-share
# GTC/GTD-only minimum doesn't apply -- only this smaller per-market one).
# When a Kelly-sized ticket would fall short, this bumps it up to the
# minimum dollar amount that actually clears min_order_size AT THAT
# CANDIDATE'S OWN PRICE (price-aware, since the same flat bump that clears
# the floor at 95c would not clear it at 99c) -- but never bumps past the
# position cap; if the bump itself would exceed the cap, the candidate is
# skipped instead (see risk.py's "below_min_order_size" sizing reason).
# Set to 0/false to always skip a too-small ticket rather than bump it --
# a reasonable choice for an operator who wants strict Kelly-fraction
# discipline over squeezing every candidate above the exchange floor.
MIN_ORDER_SIZE_BUMP = os.environ.get("POLYEDGE_MIN_ORDER_SIZE_BUMP", "1") not in ("0", "false", "no")
# A real rejection (CV-3290748, seven times over six hours, against a
# CONFIRMED-DEEP book -- not a liquidity problem) happened at a razor-thin
# ~0.1% margin ABOVE min_order_size: a $5 ticket at price 0.999 computes to
# ~5.005 shares against a min_order_size of 5 -- risk.py's exact "<"
# comparison concluded this was already fine and never bumped it, yet the
# real order still got rejected. Read from polymarket-client's actual
# order-building source (polymarket/_internal/actions/orders/market.py):
# for a max_price-protected BUY (our case), the dollar amount gets floored
# (round DOWN, never to-nearest) to whole cents BEFORE being divided by
# price to compute the share count -- confirmed via RoundingConfig(amount=5,
# price=3, size=2) for a 0.001 tick size. The share count itself then
# rounds UP if it has excess precision, but that only protects against
# LOSING more from that step -- it does not undo the initial floor. Separately,
# risk.py's own check uses the book's raw (possibly >3-decimal) entry price
# while live.py rounds price to 3dp before actually submitting the order --
# two independent roundings of what should be the same number. Either
# mechanism, or ordinary price drift between when sizing ran and when the
# order actually executes, is plausible and cannot be fully distinguished
# from source alone -- see live.py's new pre-order logging for that.
# effective_min = min_order_size * (1 + this/100) is applied to BOTH the
# comparison and the bump target, so a razor-thin case like CV-3290748 now
# gets bumped instead of silently passing the check and failing for real.
MIN_ORDER_SIZE_MARGIN_PCT = _f("POLYEDGE_MIN_ORDER_SIZE_MARGIN_PCT", 2.0)

# ---------------------------------------------------------------- strategy: ARB (Dutch book)
ARB_MIN_EDGE = _f("POLYEDGE_ARB_MIN_EDGE", 0.01)     # require >= 1 cent per $1 payout set
ARB_MIN_DEPTH_USD = _f("POLYEDGE_ARB_MIN_DEPTH", 25.0)  # ignore books thinner than this
ARB_MAX_DAYS = _i("POLYEDGE_ARB_MAX_DAYS", 60)       # skip locks resolving further out than this
# --- guards against phantom arbs on illiquid multi-outcome markets ---
# A near-zero YES-sum (e.g. 0.006 across 6 exact-score outcomes) implies a
# ludicrous edge and lets the sizer buy tens of thousands of "sets" that
# don't really exist. These bound the damage:
ARB_MIN_LEG_PRICE = _f("POLYEDGE_ARB_MIN_LEG_PRICE", 0.02)  # every leg's ask must be >= this
ARB_MIN_COST = _f("POLYEDGE_ARB_MIN_COST", 0.50)     # total lock cost per set must be >= this (YES side)
ARB_MAX_POSITION_USD = _f("POLYEDGE_ARB_MAX_POS_USD", 100.0)  # hard $ cap on any single lock, regardless of "depth"
ARB_MIN_LIQUIDITY = _f("POLYEDGE_ARB_MIN_LIQ", 2000.0)  # each market's Gamma liquidity floor
ARB_EXCLUDE_SPORTS = os.environ.get("POLYEDGE_ARB_EXCLUDE_SPORTS", "1") not in ("0", "false", "no")

# ---------------------------------------------------------------- strategy: REL (correlated markets)
REL_MIN_EDGE = _f("POLYEDGE_REL_MIN_EDGE", 0.015)
REL_MAX_DAYS = _i("POLYEDGE_REL_MAX_DAYS", 60)       # skip locks resolving further out than this
RELATIONS_FILE = os.environ.get("POLYEDGE_RELATIONS", "relations.json")

# ---------------------------------------------------------------- strategy: LONGSHOT (favorite-longshot bias)
LS_MAX_YES_PRICE = _f("POLYEDGE_LS_MAX_YES", 0.05)   # only fade YES priced <= 5c
# Floor raised from 1c to 3c: recent large-sample Polymarket research is
# CONTESTED specifically at the extreme tail — one 124M-trade study found
# extreme longshots (cheapest tokens) actually perform WELL, i.e. the
# opposite of the bias this strategy fades. The 3-5c band is where the
# classic overpricing evidence is more consistent. Do not lower this
# without evidence from your own settled-trade record.
LS_MIN_YES_PRICE = _f("POLYEDGE_LS_MIN_YES", 0.03)
LS_BIAS_HAIRCUT = _f("POLYEDGE_LS_HAIRCUT", 0.60)    # assume true P(yes) = 60% of market price
LS_MAX_DAYS = _i("POLYEDGE_LS_MAX_DAYS", 21)         # near-dated only (was 45) — capital shouldn't sit for months
LS_MIN_LIQUIDITY = _f("POLYEDGE_LS_MIN_LIQ", 1000.0) # market liquidity floor (USD)
LS_MAX_OPEN = _i("POLYEDGE_LS_MAX_OPEN", 3)          # reduced from 10 — fewer tail-risk bets
# Real evidence: LS-3412924, an actual live LONGSHOT position ("Fade: Will
# the highest temperature in Seattle be between 72-73°F...") -- a narrow
# temperature bracket, structurally the same "narrow band on a continuously-
# moving quantity" risk pattern as the tweet-count bracket that caused
# CONVERGE's original documented loss. Unlike CV_EXCLUDE_SPORTS (a
# strategy-specific judgment call -- fading cheap sports outcomes is core to
# LONGSHOT's edge, so LONGSHOT deliberately does NOT exclude sports), this
# is NOT a judgment call: a narrow bracket on a volatile continuous quantity
# is a real risk regardless of which strategy is trading it. Reuses
# convergence.is_weather_market() rather than duplicating the detection logic.
LS_EXCLUDE_WEATHER = os.environ.get("POLYEDGE_LS_EXCLUDE_WEATHER", "1") not in ("0", "false", "no")

# ---------------------------------------------------------------- strategy: CONVERGE (near-resolution yield)
CV_MIN_YES_PRICE = _f("POLYEDGE_CV_MIN_YES", 0.94)
CV_MAX_YES_PRICE = _f("POLYEDGE_CV_MAX_YES", 0.985)
CV_MAX_DAYS = _i("POLYEDGE_CV_MAX_DAYS", 14)         # resolution must be near
CV_MIN_ANNUAL_YIELD = _f("POLYEDGE_CV_MIN_APY", 0.25)  # 25%+ annualized or skip
CV_MIN_LIQUIDITY = _f("POLYEDGE_CV_MIN_LIQ", 5000.0)
# The strategy's core assumption, made explicit: a heavily-favored market
# near resolution is UNDERpriced — the true P(yes) sits between the market
# price and 1.0. CV_TRUE_P_UPLIFT is how far toward 1.0 we assume it sits.
# Lowered from 0.50 to 0.20: measured realized returns on high-probability
# Polymarket tokens in large-sample research are small (fractions of a
# percent to ~1%), not the multi-percent edge a 0.5 uplift implies. A
# market at 0.96 is now assumed true 0.968, not 0.98 — Kelly sizes
# accordingly smaller. Raise only if your own settled CONVERGE record
# shows wins landing more often than the assumption predicts.
CV_TRUE_P_UPLIFT = _f("POLYEDGE_CV_UPLIFT", 0.20)
# Exclude live sports MATCH markets (O/U, spreads, "X vs. Y" outcomes) from
# CONVERGE. The strategy's thesis is "effectively decided, awaiting formal
# resolution" — a match at 94-98c is NOT decided, it's genuinely live event
# risk priced against sharp bookmaker lines (a 0-0 grinding out is how a
# single loss erases ~30 small wins). Detection is heuristic (event tags +
# title patterns) and won't catch 100%. ARB on sports events is deliberately
# unaffected: locks don't care who wins.
CV_EXCLUDE_SPORTS = os.environ.get("POLYEDGE_CV_EXCLUDE_SPORTS", "1") not in ("0", "false", "no")
CV_EXCLUDE_EARNINGS = os.environ.get("POLYEDGE_CV_EXCLUDE_EARNINGS", "1") not in ("0", "false", "no")
CV_EXCLUDE_BRACKETS = os.environ.get("POLYEDGE_CV_EXCLUDE_BRACKETS", "1") not in ("0", "false", "no")
CV_EXCLUDE_ELECTIONS = os.environ.get("POLYEDGE_CV_EXCLUDE_ELECTIONS", "1") not in ("0", "false", "no")
CV_EXCLUDE_RANKINGS = os.environ.get("POLYEDGE_CV_EXCLUDE_RANKINGS", "1") not in ("0", "false", "no")
# "Will the highest temperature in Seattle be between 72-73°F" -- a narrow
# bracket on a continuously-moving quantity, the same risk shape as the
# tweet-count bracket CV_EXCLUDE_BRACKETS already covers, but is_bracket_market()
# doesn't catch it (its numeric-range patterns require $ or % signs, or one
# of a fixed set of countable nouns -- temperature/°F/°C match neither).
# See is_weather_market() in convergence.py.
CV_EXCLUDE_WEATHER = os.environ.get("POLYEDGE_CV_EXCLUDE_WEATHER", "1") not in ("0", "false", "no")

# ---------------------------------------------------------------- reconciliation
# Compares the bot's own bookkeeping against Polymarket's real on-chain
# record for the funder wallet every RECONCILE_EVERY_N_CYCLES cycles.
# Only runs at all if POLYEDGE_FUNDER_ADDRESS is set -- there is nothing
# to reconcile against in pure paper mode. A divergence beyond the
# threshold halts live trading via the same mechanism as the daily-loss
# circuit breaker (see live.py) -- state that no longer matches the real
# wallet should stop and wait for a human, not keep trading on it.
RECONCILE_ENABLED = os.environ.get("POLYEDGE_RECONCILE_ENABLED", "1") not in ("0", "false", "no")
RECONCILE_HALT_THRESHOLD_PCT = float(os.environ.get("POLYEDGE_RECONCILE_HALT_THRESHOLD_PCT", "15.0"))
RECONCILE_EVERY_N_CYCLES = int(os.environ.get("POLYEDGE_RECONCILE_EVERY_N_CYCLES", "1"))

# ---------------------------------------------------------------- take-profit (early exit)
# Sell a position back into the live bid BEFORE resolution, once enough of
# its remaining upside (the gap to $1) has been captured. Only applies to
# single-leg, non-guaranteed strategies — selling one leg of an ARB/REL lock
# early breaks the guarantee, so those are never touched here.
TAKE_PROFIT_STRATEGIES = {"CONVERGE"}                      # which strategies allow early exit
TAKE_PROFIT_UPSIDE_CAPTURE = _f("POLYEDGE_TP_CAPTURE", 0.25)  # sell at 25% of remaining upside captured (max cycling speed)
TAKE_PROFIT_MIN_GAIN = _f("POLYEDGE_TP_MIN_GAIN", 0.005)   # ignore moves smaller than 0.5c/share (noise)

# ---------------------------------------------------------------- LIVE MODE (real money)
# All gates documented in polyedge/live.py and LIVE.md. Nothing here makes
# the bot trade real money by itself: POLYEDGE_LIVE=1 + ARMED file +
# POLYEDGE_DRY_RUN=0 are all required.
LIVE_ALLOW_MULTILEG = os.environ.get("POLYEDGE_LIVE_MULTILEG", "0") == "1"  # ARB/REL locks live: OFF by default
LIVE_MAX_DAILY_LOSS = _f("POLYEDGE_LIVE_MAX_DAILY_LOSS", 15.0)  # USD realized loss/day before auto-halt
# Automatically applied to every live position the moment it opens (via
# controls.set_stop_loss()), so "keep it at 30%" is a real, standing
# behavior rather than something that has to be re-clicked in the
# control panel for every new position. None/0 disables auto-stop-loss
# entirely -- positions would then only stop out via a manually-set
# per-position override in the control panel, same as before this existed.
LIVE_DEFAULT_STOP_LOSS_PCT = _f("POLYEDGE_DEFAULT_STOP_LOSS_PCT", 30.0)
# Confirmed in production: a token whose FOK order gets rejected
# (RequestRejectedError -- see live.py) can keep scoring as risk.py's best
# candidate cycle after cycle, since nothing about the rejection itself
# changes its edge/price/liquidity inputs -- one dead token was retried for
# over an hour straight, starving every other real candidate that cycle of
# a sizing slot. LiveEngine.rejected_cooldown records the token_id/timestamp
# of a BUY leg that didn't fill; risk.size_opportunities() skips any
# candidate with a leg still inside this window (age < this many minutes)
# entirely, before it can consume a slot. Paper mode has no order-book-depth
# concept to fail against, so this never applies there.
LIVE_REJECTED_COOLDOWN_MIN = _f("POLYEDGE_REJECTED_COOLDOWN_MIN", 30.0)

# URL for the "Control Panel" button on the generated dashboard. Defaults
# to localhost -- the control panel is meant to be reached through an SSH
# tunnel or Tailscale (see LIVE.md), never a public URL. If you tunnel a
# different local port, override with POLYEDGE_CONTROL_PANEL_URL.
CONTROL_PANEL_URL = os.environ.get("POLYEDGE_CONTROL_PANEL_URL", "http://localhost:8787/")

# Suggested $100-bankroll live profile (set these as env vars on the VPS —
# they override the paper defaults above without editing this file):
#   POLYEDGE_BANKROLL=100  POLYEDGE_MAX_POS_PCT=0.10  POLYEDGE_MIN_TICKET=5
#   POLYEDGE_MAX_EXPO_PCT=0.50  POLYEDGE_LS_MAX_OPEN=1
# Rationale: 10% positions = $10 tickets (above the $5 minimum, so trades
# actually fire), 50% max deployed keeps half the bankroll as buffer, and
# one longshot slot keeps tail risk to a single fade at a time.
