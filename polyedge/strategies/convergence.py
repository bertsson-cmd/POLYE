"""CONVERGE — near-resolution convergence ("last-cent yield").

Markets that are effectively decided often still trade at 94–98c days
before formal resolution, because holders pay for early liquidity.
Buying YES at 0.96 that resolves in 5 days yields 4.17% in 5 days
(≈ 300%+ annualized) IF it resolves YES.

The risk is precisely the "actually not decided" surprise, so:
  * only high liquidity markets (crowd conviction filter),
  * only short horizons (CV_MAX_DAYS),
  * an annualized-yield floor so capital isn't parked for pennies,
  * treated as probabilistic, never guaranteed,
  * six categories excluded (each independently toggleable) where a
    price near $1 does NOT mean "effectively decided" -- it means
    "priced against a discrete, hard-to-predict reveal", which is a
    different (and historically costly) kind of risk than a market
    that's simply drifted toward its obvious outcome over time:

    - CV_EXCLUDE_SPORTS: a live sports MATCH at 96c is open event risk
      priced against sharp bookmaker lines, not a decided event.
    - CV_EXCLUDE_EARNINGS: "will X beat earnings" resolves on a single
      discrete report, not a gradual convergence -- confirmed in real
      trading data as a 0-for-2 category (two losses, no wins) that
      erased most of CONVERGE's cumulative gains from 110 other wins.
    - CV_EXCLUDE_BRACKETS: numeric-range markets ("40-64 tweets") ask
      whether a count lands in a narrow bucket, which is a coin-flip-
      shaped question dressed up as a "near-certain" price -- also
      confirmed as a real loss in production data.
    - CV_EXCLUDE_ELECTIONS: a primary/election candidate polling under
      1% is usually a safe "won't win" bet, but carries its own tail
      risk (late dropouts redistributing votes, reporting delays,
      resolve-to-"Other" edge cases) that the certainty-premium thesis
      doesn't account for.
    - CV_EXCLUDE_RANKINGS: "largest company by market cap" / "#1" /
      "second-biggest" superlatives compare volatile continuous
      quantities; 96c only means "currently true" and the ranking can
      flip in one trading day -- confirmed twice in paper data, where
      two legs of the same market-cap reshuffle went from ~96c to near
      zero together.
    - CV_EXCLUDE_WEATHER: "highest temperature between 72-73°F" is a
      narrow bracket on a continuously-moving quantity -- the same
      coin-flip-shaped risk as CV_EXCLUDE_BRACKETS, just not caught by
      that detector's patterns (no $ or % sign, not a countable noun).
      Real evidence: LS-3412924, an actual live LONGSHOT position with
      exactly this structure (see longshot.py -- this exclusion also
      applies there, unlike sports).
"""
import logging
import re
from typing import Dict, List

from .. import config, fees
from ..models import Leg, Market, Opportunity, OrderBook, days_to_resolution

log = logging.getLogger("polyedge.converge")

# --- sports match detection (heuristic, deliberately conservative) ---------
# Title patterns typical of match-outcome markets. Chosen to catch O/U,
# spreads, moneylines, half markets and head-to-head matchups WITHOUT
# false-positiving on sports-adjacent event markets ("Will X attend the
# final?"), which are legitimate CONVERGE material.
_SPORTS_TITLE_PATTERNS = re.compile(
    r"(\bvs\.?\s|\bO/U\b|\bover/under\b|\bspread\b|\bmoneyline\b|"
    r"\(\s*[+-]\d+(\.\d+)?\s*\)|"                       # handicap "(-1.5)"
    r"\b(1st|2nd|first|second)\s+half\b|\bhalf\s+result\b|"
    r"\bto\s+score\b|\bboth\s+teams\b|\bbtts\b|"
    r"\bshootout\b|\bextra\s+time\b|\bcorners?\b|\bred\s+card\b|"
    r"\byellow\s+card\b|\bclean\s+sheet\b|\bhat[- ]?trick\b)",
    re.IGNORECASE)

_SPORTS_CATEGORY_KEYWORDS = (
    "sport", "soccer", "football", "nba", "nfl", "mlb", "nhl", "wnba",
    "epl", "la liga", "serie a", "bundesliga", "ligue 1", "mls",
    "champions league", "uefa", "fifa", "tennis", "golf", "nascar", "f1",
    "cricket", "mma", "ufc", "boxing", "esports", "hockey", "baseball",
    "basketball", "rugby",
)


def is_sports_match(m: Market) -> bool:
    """True if this market looks like a sports MATCH-outcome market."""
    text = f"{m.question} {m.event_title}"
    if _SPORTS_TITLE_PATTERNS.search(text):
        return True
    # category tags alone (from Gamma) also mark a market as sports —
    # combined with the CONVERGE price band this is almost always a
    # match-outcome market, not an event market
    cat = (m.category or "").lower()
    return any(k in cat for k in _SPORTS_CATEGORY_KEYWORDS)


# --- earnings/guidance-beat detection ---------------------------------------
# Deliberately narrow to "beat" framing specifically -- CONVERGE should
# still be free to trade e.g. "Will X report Q3 results by date Y?" (a
# genuine calendar/logistics question), just not "will the number beat
# consensus", which is the actual surprise-shaped question.
_EARNINGS_TITLE_PATTERNS = re.compile(
    r"\bbeat\s+(quarterly\s+|q[1-4]\s+)?earnings\b|"
    r"\bbeat\s+eps\b|\bbeat\s+revenue(\s+estimates?)?\b|"
    r"\bearnings\s+beat\b|\b(raise|cut|miss)\s+guidance\b",
    re.IGNORECASE)


def is_earnings_market(m: Market) -> bool:
    """True if this market resolves on a single discrete earnings/guidance
    reveal rather than something that gradually converges toward certain."""
    text = f"{m.question} {m.event_title}"
    return bool(_EARNINGS_TITLE_PATTERNS.search(text))


# --- numeric-bracket / range detection --------------------------------------
# Catches "will X post 40-64 tweets", "between 3 and 5 rate cuts", AND
# narrow price/value bands like "between $64,000 and $66,000" or
# "$4.25-4.50%" -- confirmed in production data as the same risk shape:
# a continuously-moving value clipping just outside a narrow band is a
# coin-flip-shaped question, whatever the current price implies. An
# earlier version of this only caught countable-noun ranges (tweets,
# goals, etc.) and explicitly treated dollar ranges as safe -- live
# trading surfaced a Bitcoin "$64,000-$66,000" CONVERGE candidate that
# slipped through, so that assumption was wrong and is corrected here.
_BRACKET_COUNT_NOUNS = (
    "tweets?", "posts?", "goals?", "points?", "games?", "wins?", "losses?",
    "seats?", "votes?", "cuts?", "hikes?", "releases?", "episodes?",
    "launches?", "games?", "matches?", "appearances?", "times?",
)
_BRACKET_TITLE_PATTERNS = re.compile(
    r"\b\d{1,4}\s*[-–]\s*\d{1,4}\s+(" + "|".join(_BRACKET_COUNT_NOUNS) + r")\b|"
    r"\bbetween\s+\d+\s+and\s+\d+\s+(\w+\s+)?(" + "|".join(_BRACKET_COUNT_NOUNS) + r")\b|"
    # narrow value bands: "between $X and $Y", "$X-$Y", "X%-Y%" -- a range
    # on a continuous, volatile quantity, not a single-sided threshold
    r"\bbetween\s+\$[\d,]+(\.\d+)?\s+and\s+\$[\d,]+(\.\d+)?\b|"
    r"\$[\d,]+(\.\d+)?\s*[-–]\s*\$?[\d,]+(\.\d+)?\b|"
    r"\b\d+(\.\d+)?%\s*[-–]\s*\d+(\.\d+)?%",
    re.IGNORECASE)


def is_bracket_market(m: Market) -> bool:
    """True if this market asks whether a count falls in a narrow numeric
    bracket -- a coin-flip-shaped question, whatever the current price."""
    text = f"{m.question} {m.event_title}"
    return bool(_BRACKET_TITLE_PATTERNS.search(text))


# --- election / primary detection -------------------------------------------
# Deliberately title-based, not category-based -- "politics" as a Gamma
# category is far too broad (would exclude legitimate policy/logistics
# CONVERGE markets too) whereas "X Primary Winner" / "X Election Winner"
# framing specifically flags a multi-candidate race with real tail risk
# even when one candidate is priced as an overwhelming favorite.
_ELECTION_TITLE_PATTERNS = re.compile(
    r"\bprimary\s+winner\b|\belection\s+winner\b|"
    r"\b(democratic|republican)\s+primary\b|\bsenate\s+primary\b|"
    r"\bgubernatorial\s+(election|primary)\b|\bpresidential\s+(election|primary|nominee)\b",
    re.IGNORECASE)


def is_election_market(m: Market) -> bool:
    """True if this market is a multi-candidate primary/election race --
    even a heavy favorite carries real tail risk (dropouts, reporting
    delays, resolve-to-'Other' edge cases) the certainty-premium thesis
    doesn't cover."""
    text = f"{m.question} {m.event_title}"
    return bool(_ELECTION_TITLE_PATTERNS.search(text))


# --- ranking / superlative detection ----------------------------------------
# "Will Apple be the largest company by market cap", "Will NVIDIA be the
# second-largest ...", "most valuable", "#1 ..." -- a comparison between
# volatile continuous quantities. The 95-96c price only means "currently
# true", and the ranking can flip in a single trading day. Confirmed in
# paper data: two such positions (Apple largest / NVIDIA second-largest,
# opened in the same second -- two legs of the same underlying reshuffle)
# both went from ~96c to near zero. Deliberately narrow: requires the
# superlative framing itself, so ordinary threshold markets ("above
# $62,000", "close above $540") -- which are the bread and butter of
# CONVERGE's winning inventory -- are NOT touched.
_RANKING_TITLE_PATTERNS = re.compile(
    r"\b(the\s+)?(largest|biggest|most\s+valuable|top|best[- ]selling|"
    r"highest[- ]grossing|most\s+watched|most\s+streamed)\s+\w+|"
    r"\b(second|third|fourth|fifth|2nd|3rd|4th|5th)[- ]largest\b|"
    r"\brank(ed)?\s+#?\d\b|#1\s",
    re.IGNORECASE)


def is_ranking_market(m: Market) -> bool:
    """True if this market asks whether something holds a RANKING position
    (largest, #1, second-biggest...) -- a comparison between volatile
    quantities that can flip in one step, not a decided outcome."""
    text = f"{m.question} {m.event_title}"
    return bool(_RANKING_TITLE_PATTERNS.search(text))


# --- weather detection --------------------------------------------------
# Real evidence: LS-3412924, an actual live LONGSHOT position ("Fade: Will
# the highest temperature in Seattle be between 72-73°F...") -- a narrow
# bracket on a continuously-moving quantity, the same risk shape as the
# tweet-count bracket that caused CONVERGE's original documented loss
# (-$26.21, see the module docstring). is_bracket_market() above does NOT
# catch this: its numeric-range patterns require $ or % signs, or one of a
# fixed set of countable nouns (tweets, goals, ...) -- "72-73°F" matches
# neither, so a dedicated detector is needed rather than widening that one.
#
# Research done before writing this (per the task): fees.py's
# CATEGORY_FEE_RATES table already carries a "weather" entry with its own
# fee rate, cross-checked against Polymarket's own published "$ per 100
# shares" fee schedule during the V2 rewrite -- confirming "weather" is a
# real, populated Gamma category/tag value on this bot's actual data, not
# a guess. That makes category a MORE reliable signal here than for
# is_election_market()/is_ranking_market() above (whose comments explain
# why a bare category would be too broad or too unreliable for those
# specific cases) -- so, like is_sports_match() above (the one other
# detector with a confirmed-reliable category signal), this checks BOTH
# title patterns AND category, not title alone.
#
# "rain"/"snow" are deliberately NOT matched as bare words -- tried first,
# and confirmed by hand to false-positive on a plausible real title shape
# ("Will Kanye's new album 'Rain' go platinum?", a legitimate culture-
# category CONVERGE/LONGSHOT candidate with no weather content at all).
# Real weather markets asking about rain/snow overwhelmingly use one of
# the recognizable question phrasings below ("will it rain", "chance of
# snow", "rain tomorrow") rather than the bare noun as a title subject --
# matching those instead avoids the proper-noun collision while still
# catching "will it rain in X" style markets that rainfall/rainy alone
# would miss.
_WEATHER_TITLE_PATTERNS = re.compile(
    r"\btemperature\b|\bweather\b|\brainfall\b|\bsnowfall\b|"
    r"\bprecipitation\b|\bhumidity\b|\bwind\s*speed\b|\bheat\s*wave\b|"
    r"\bhurricane\b|\btornado\b|\bblizzard\b|\bdrought\b|\bwildfire\b|"
    r"\bhigh(est)?\s+temp\b|\blow(est)?\s+temp\b|"
    r"\brainy\b|\bsnowy\b|"
    r"\bwill\s+it\s+(rain|snow)\b|\bchance\s+of\s+(rain|snow)\b|"
    r"\b(rain|snow)\s+(today|tomorrow|this\s+week|next\s+week)\b|"
    r"°\s*[FC]\b|\d+\s*[-–]\s*\d+\s*°\s*[FC]\b",
    re.IGNORECASE)

_WEATHER_CATEGORY_KEYWORDS = ("weather", "climate")


def is_weather_market(m: Market) -> bool:
    """True if this market asks about a weather quantity (temperature,
    rain/snowfall, wind, etc.) -- narrow brackets on these are the same
    coin-flip-shaped risk as is_bracket_market() catches for counts, just
    not phrased in a way that detector's patterns match."""
    text = f"{m.question} {m.event_title}"
    if _WEATHER_TITLE_PATTERNS.search(text):
        return True
    cat = (m.category or "").lower()
    return any(k in cat for k in _WEATHER_CATEGORY_KEYWORDS)


def scan(all_markets: List[Market], books: Dict[str, OrderBook]) -> List[Opportunity]:
    out: List[Opportunity] = []
    skipped = {"sports": 0, "earnings": 0, "brackets": 0, "elections": 0,
               "rankings": 0, "weather": 0}
    for m in all_markets:
        if not (config.CV_MIN_YES_PRICE <= m.yes_price <= config.CV_MAX_YES_PRICE):
            continue
        if m.liquidity < config.CV_MIN_LIQUIDITY:
            continue
        days = days_to_resolution(m.end_date)
        if days < config.MIN_DAYS_TO_RESOLUTION or days > config.CV_MAX_DAYS:
            continue
        if config.CV_EXCLUDE_SPORTS and is_sports_match(m):
            skipped["sports"] += 1
            continue
        if config.CV_EXCLUDE_EARNINGS and is_earnings_market(m):
            skipped["earnings"] += 1
            continue
        if config.CV_EXCLUDE_BRACKETS and is_bracket_market(m):
            skipped["brackets"] += 1
            continue
        if config.CV_EXCLUDE_ELECTIONS and is_election_market(m):
            skipped["elections"] += 1
            continue
        if config.CV_EXCLUDE_RANKINGS and is_ranking_market(m):
            skipped["rankings"] += 1
            continue
        if config.CV_EXCLUDE_WEATHER and is_weather_market(m):
            skipped["weather"] += 1
            continue

        book = books.get(m.yes_token)
        if not book or book.best_ask() is None:
            continue
        a = book.best_ask()
        if a >= 0.999 or a < config.CV_MIN_YES_PRICE:
            continue

        fee = fees.fee_per_share(a, m.category)
        yield_pct = (1.0 - a - fee) / a             # NET return if resolves YES
        annual = yield_pct * 365.0 / days
        if annual < config.CV_MIN_ANNUAL_YIELD:
            continue

        # the strategy's edge assumption, made explicit for the sizing:
        # true P(yes) is assumed to sit CV_TRUE_P_UPLIFT of the way from
        # the market price to 1.0 (near-certain markets are underpriced).
        # If est_p_win were just the market price, Kelly would see zero
        # edge and never fund a single convergence trade.
        p_assumed = m.yes_price + (1.0 - m.yes_price) * config.CV_TRUE_P_UPLIFT

        out.append(Opportunity(
            strategy="CONVERGE", key=f"CV-{m.market_id}",
            title=f"Converge: {m.question[:60]}",
            edge=yield_pct, guaranteed=False,
            est_p_win=p_assumed,
            legs=[Leg(m.yes_token, m.market_id, f"YES {m.question}", "YES",
                      a, 0.0, fee_per_share=fee)],
            resolve_by=m.end_date,
            note=f"YES ask {a:.3f}, fee {fee:.4f}, {days:.1f}d to resolution, "
                 f"{annual*100:.0f}% annualized net if YES, assumed true P {p_assumed:.3f}",
        ))
    # sort by annualized yield: same edge resolving sooner ranks higher,
    # which is exactly the near-term, fast-cycling preference
    out.sort(key=lambda o: -(o.edge * 365.0 / max(0.02, days_to_resolution(o.resolve_by))))
    skipped_total = sum(skipped.values())
    if skipped_total:
        log.info("converge: excluded %d market(s) -- %s",
                 skipped_total,
                 ", ".join(f"{k}={v}" for k, v in skipped.items() if v))
    return out
