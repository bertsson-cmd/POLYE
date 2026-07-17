"""PolyEdge 95 v2 — central configuration.

Every tunable knob lives here. Edit this file (or set environment variables
with the same names) — you should never need to touch strategy code.
"""
import os


def _f(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def _i(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


# ---------------------------------------------------------------- general
MODE = os.environ.get("POLYEDGE_MODE", "paper")   # "paper" only (live = manual)
STARTING_BANKROLL = _f("POLYEDGE_BANKROLL", 1000.0)  # USD (paper money)
FEE_RATE = _f("POLYEDGE_FEE", 0.0)                # Polymarket charges no trading fee today; keep configurable
STATE_DIR = os.environ.get("POLYEDGE_STATE_DIR", "state")
DOCS_DIR = os.environ.get("POLYEDGE_DOCS_DIR", "docs")

# ---------------------------------------------------------------- API
GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"
HTTP_TIMEOUT = 15          # seconds per request
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

# ---------------------------------------------------------------- strategy: ARB (Dutch book)
ARB_MIN_EDGE = _f("POLYEDGE_ARB_MIN_EDGE", 0.01)     # require >= 1 cent per $1 payout set
ARB_MIN_DEPTH_USD = _f("POLYEDGE_ARB_MIN_DEPTH", 25.0)  # ignore books thinner than this
ARB_MAX_DAYS = _i("POLYEDGE_ARB_MAX_DAYS", 60)       # skip locks resolving further out than this

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

# ---------------------------------------------------------------- take-profit (early exit)
# Sell a position back into the live bid BEFORE resolution, once enough of
# its remaining upside (the gap to $1) has been captured. Only applies to
# single-leg, non-guaranteed strategies — selling one leg of an ARB/REL lock
# early breaks the guarantee, so those are never touched here.
TAKE_PROFIT_STRATEGIES = {"CONVERGE"}                      # which strategies allow early exit
TAKE_PROFIT_UPSIDE_CAPTURE = _f("POLYEDGE_TP_CAPTURE", 0.25)  # sell at 25% of remaining upside captured (max cycling speed)
TAKE_PROFIT_MIN_GAIN = _f("POLYEDGE_TP_MIN_GAIN", 0.005)   # ignore moves smaller than 0.5c/share (noise)
