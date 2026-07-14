# PolyEdge 95 v2 — Operator's Manual

*Polymarket opportunity scanner + paper-trading engine with a Windows 95 terminal dashboard.*

---

## 1. What this is

PolyEdge 95 v2 scans Polymarket every 30 minutes (via GitHub Actions), hunts for four kinds of edge, simulates the trades with paper money, and publishes a live dashboard to GitHub Pages showing every trade and the running profit/loss.

It is deliberately a **paper trader**. It uses real live market data and real order-book prices, but fills are simulated. This is the correct first phase for any strategy: you get months of honest evidence about whether the edges are real before a single króna is at risk. Section 9 covers what going live would involve.

### What's new versus PolyEdge 95 v1

| | v1 | v2 |
|---|---|---|
| Strategies | favorite-longshot bias | + Dutch-book arbitrage, correlated-market locks, convergence yield |
| Sizing | fixed | fractional Kelly + layered exposure caps |
| Fills | indicative prices | real order-book asks, depth-limited |
| Accounting | basic | full equity curve, per-strategy P/L, drawdown, invariant-tested |
| Dashboard | static stats | System Monitor equity chart + interactive Trade Map |
| Tests | none | 32 automated tests + accounting audit |

---

## 2. The four strategies

### ARB — Dutch-book arbitrage 🔒 (guaranteed if filled)

Polymarket groups mutually exclusive outcomes into events ("Who wins the World Cup?" = one market per team, exactly one resolves YES). Two locks exist:

**YES-side lock.** Buy YES on *every* outcome. Exactly one pays $1, so a set of one share of each is worth exactly $1 at resolution. If the YES asks sum to less than $1 — say 0.97 — you pay 97c for a guaranteed $1. Edge = 3c per set, no matter who wins.

**NO-side lock.** Buy NO on every outcome. With N outcomes, exactly one resolves YES, so exactly N−1 NOs pay $1. If the NO asks sum to less than N−1, the difference is locked profit.

The scanner only reports arbs that are **executable**: size is capped at the *thinnest leg's best-ask depth*, so the reported edge is at the prices used in the math, not a headline number that evaporates when you try to fill it. A $25 minimum depth floor filters out dust.

*Reality check:* true Dutch books are rare and close in seconds to minutes. A 30-minute GitHub Actions cadence will catch the slow ones (often around chaotic news moments) but miss most. That's fine for the paper phase — what you're measuring is how often they appear at all.

### REL — correlated-market locks 🔒 (guaranteed if the relation holds)

Two markets can logically constrain each other:

**IMPLIES** — "Argentina wins the cup" (A) implies "Argentina reaches the final" (B). So P(A) ≤ P(B) must hold. If prices violate it, buy **YES(B) + NO(A)**: if A happens, B must have happened (YES B pays $1); if A doesn't, NO(A) pays $1. Minimum payout $1 per set — profitable whenever ask(YES B) + ask(NO A) < $1.

**EXCLUSIVE** — "France wins" and "Brazil wins" can't both happen. So P(A) + P(B) ≤ 1. If violated, buy **NO(A) + NO(B)**: at most one can win, so at least one NO pays $1.

You declare relations yourself in `relations.json` (section 7) — the bot does not guess logical relationships from question text, because a wrong guess turns a "lock" into an unhedged bet. **The lock is only as good as your relation.** "Wins the cup implies reaches the final" is airtight; be careful with anything where resolution criteria could diverge (different resolution sources, weird edge cases in the market rules — read both markets' resolution rules before adding a relation).

### LONGSHOT — favorite-longshot bias fading (probabilistic)

Documented for decades across betting markets: low-probability outcomes trade *above* their true probability because lottery-ticket buyers outnumber sellers. The bot buys NO on markets whose YES trades at 1–5c, assuming the true probability is only `LS_BIAS_HAIRCUT` (default 60%) of the market price.

This is the strategy that makes the demo data instructive: **it wins ~95% of the time in small amounts and occasionally loses big.** One landed longshot can erase weeks of grinding. Its survival depends on the guardrails: max 10 concurrent fades, one fade per event (fading five outcomes of the same match is one bet, not five), quarter-Kelly sizing, liquidity and date filters.

The haircut (60%) is an *assumption*, not a measurement. The entire point of the paper phase is to calibrate it: if after 100+ settled fades your longshots land more often than `price × 0.6` predicted, the haircut is too generous and the strategy's true EV may be negative.

### CONVERGE — near-resolution yield (probabilistic)

Markets that are effectively decided often trade at 94–98c days before formal resolution, because holders pay for early liquidity. Buying YES at 0.96 five days before resolution yields 4.2% in five days if it resolves YES. Filters: high liquidity only (crowd conviction), ≤14 days out, and a 25% minimum *annualized* yield so capital isn't parked for pennies. The risk is exactly the "actually not decided" surprise — a VAR-review-style reversal. It is treated as probabilistic, never guaranteed.

---

## 3. Sizing and risk (risk.py)

Applied in strict order, every scan:

1. **Guaranteed locks are funded first** — free money before speculative money. Among speculative candidates, **sooner-resolving markets are funded first** (near-term capital cycling — a 3¢ edge resolving Friday beats a 3.5¢ edge resolving in two weeks, because the capital comes back and goes again).
2. **Probabilistic trades get fractional Kelly.** Kelly formula `f* = (p·b − q)/b`, then multiplied by `KELLY_FRACTION` (default 0.25 = quarter-Kelly). Full Kelly is only optimal when your probability estimate is *exactly right*; quarter-Kelly costs little growth and survives being wrong.
3. **Caps**, all enforced simultaneously:
   - max 5% of bankroll per position
   - per-strategy ceilings: CONVERGE 35%, ARB 30%, REL 20%, LONGSHOT 5%
   - max 60% of bankroll deployed in total
   - never exceed available cash
   - max 3 open longshot fades
   - trades under $5 dropped (dust)
4. **Dedup** — one open position per opportunity key; a re-detected arb isn't bought twice.

### Take-profit (early exit)

CONVERGE positions don't have to wait for formal resolution. Each scan, the bot fetches the **live bid** for every open CONVERGE position and sells once **40% of the remaining upside** has been captured — e.g. entered at 96¢ (4¢ of upside to $1), it sells as soon as the bid reaches ~97.6¢. The freed cash immediately becomes available for the next opportunity, so capital cycles through many small wins instead of sitting in nearly-done markets for the last cent.

Design guarantees: exits price against the actual bid (what a real sale would fetch), never the indicative mark; ARB/REL locks are **never** unwound early (selling one leg breaks the guarantee); missing books leave positions untouched. Tunable via `TAKE_PROFIT_UPSIDE_CAPTURE` — raise it toward 1.0 to hold longer for more per trade, lower it for even faster cycling. Add `"LONGSHOT"` to `TAKE_PROFIT_STRATEGIES` to give fades the same early exit.

---

## 4. File map

```
polyedge95/
├── polyedge/
│   ├── config.py            ← ALL settings (the only file you should edit)
│   ├── api.py               Gamma + CLOB client, retries, defensive parsing
│   ├── models.py            OrderBook / Market / Opportunity / Leg
│   ├── risk.py              Kelly + caps
│   ├── paper.py             paper engine: fills, marking, settlement, stats
│   ├── report.py            dashboard generator (docs/index.html)
│   ├── main.py              one scan cycle end-to-end
│   └── strategies/
│       ├── arbitrage.py     ARB
│       ├── correlated.py    REL
│       ├── longshot.py      LONGSHOT
│       └── convergence.py   CONVERGE
├── tests/test_polyedge.py   32 tests
├── relations.json           your declared market relations (REL)
├── demo.py                  seeds 30 days of fake data for the dashboard
├── state/paper_state.json   the bot's memory (auto-created)
├── docs/index.html          the dashboard (auto-generated)
└── .github/workflows/polyedge.yml   runs every 30 min
```

---

## 5. Setup (GitHub web, no local tools needed)

1. **Create a new GitHub repository** (e.g. `polyedge95-v2`), or a fresh branch of your existing PolyEdge repo. Upload the contents of the zip (drag-and-drop onto "uploading an existing file" works; keep the folder structure, including the hidden `.github` folder — if drag-and-drop skips it, create `.github/workflows/polyedge.yml` manually in the web editor and paste the file's contents).
2. **Enable Actions write access:** repo → Settings → Actions → General → Workflow permissions → select **Read and write permissions** → Save. (Without this the bot can't commit its state.)
3. **Enable GitHub Pages:** Settings → Pages → Source: *Deploy from a branch* → Branch: `main`, folder: `/docs` → Save. Your dashboard will live at `https://<you>.github.io/<repo>/`.
4. **First run:** Actions tab → "PolyEdge 95 v2 scan" → *Run workflow*. After it finishes, the dashboard updates. It then repeats every 30 minutes automatically.
5. **Start clean:** the zip ships with demo data so you can see the dashboard populated. To begin your real paper run, delete `state/paper_state.json` in the web editor and trigger a run.

> Note: GitHub disables scheduled workflows on repos with no activity for ~60 days — a nudge commit revives it. Scheduled runs are also best-effort; "every 30 min" sometimes runs late. Neither matters for paper trading.

---

## 6. Reading the dashboard

**Header window** — equity (cash + open positions marked to market), total return vs the $1,000 starting bankroll, cash, realized P/L (settled trades only), open/settled counts with win rate.

**System Monitor** (green CRT) — the equity curve. The dashed line is your starting bankroll; green above it is profit. The dark-yellow line is *realized* P/L re-based to the bankroll line, so the vertical gap between the two lines is the unrealized value sitting in open positions.

**Trade Map** — every fill, plotted over time:
- **Squares on the zero line** = position openings (no P/L yet). Color = strategy: cyan ARB, yellow REL, magenta LONGSHOT, green CONVERGE.
- **Circles** = settlements, placed at their realized P/L. Green ring = profit, red ring = loss. Size scales with dollar amount.
- **Hover any marker** for the full detail (what, when, cost/payout, P/L).
- The characteristic pattern to expect: a carpet of small green closes with the occasional deep red LONGSHOT circle. Whether the green outweighs the red *is* the strategy verdict.

**Tables** — open positions (🔒 marks guaranteed locks), the settled trade log, and every candidate the last scan found (including ones the risk module declined to fund).

---

## 7. relations.json — feeding the REL strategy

The file ships with an inactive placeholder. To arm it:

1. Find two logically linked markets on Polymarket. World Cup examples: *team wins tournament* → *team reaches final*; *player wins Golden Boot* → *player's team reaches at least X*(careful!); two different teams both winning = EXCLUSIVE.
2. Get each market's numeric ID: the scanner logs market IDs, or open `https://gamma-api.polymarket.com/events?slug=<event-slug>` in a browser and read the `id` field of each market.
3. Edit `relations.json`:

```json
[
  {"type": "IMPLIES",
   "a_market_id": "512345",
   "b_market_id": "512399",
   "note": "ARG wins WC => ARG reaches final"},
  {"type": "EXCLUSIVE",
   "a_market_id": "512345",
   "b_market_id": "512346",
   "note": "ARG wins WC / FRA wins WC"}
]
```

Rules of thumb: `a` is always the *narrower* claim in IMPLIES ("A happening forces B"). Read both markets' resolution criteria first — a relation that's 99% logical and 1% resolution-rule-quirk is not a lock.

---

## 8. Configuration & testing

Everything tunable is in `polyedge/config.py`, commented inline. The ones you'll most likely touch:

| Setting | Default | Meaning |
|---|---|---|
| `STARTING_BANKROLL` | 1000 | paper bankroll (USD) |
| `KELLY_FRACTION` | 0.25 | fraction of full Kelly |
| `MAX_POSITION_PCT` | 0.05 | per-position cap |
| `TAKE_PROFIT_UPSIDE_CAPTURE` | 0.40 | sell CONVERGE once this share of remaining upside is captured |
| `LS_BIAS_HAIRCUT` | 0.60 | assumed true P(yes) as share of price |
| `LS_MAX_YES_PRICE` | 0.05 | fade only YES ≤ 5c |
| `LS_MAX_DAYS` | 21 | fade only markets resolving within 3 weeks |
| `LS_MAX_OPEN` | 3 | max concurrent longshot fades |
| `CV_MIN_ANNUAL_YIELD` | 0.25 | convergence APY floor |
| `ARB_MIN_EDGE` | 0.01 | ignore locks thinner than 1c/set |

Run the tests any time you change logic (Actions can do it too, or locally `pip install pytest && pytest tests/ -v`). The suite verifies the arbitrage payoffs across every possible outcome, the Kelly math against known values, every risk cap, and the accounting invariant *equity = cash + marked open value* through open→mark→settle cycles, including a losing longshot.

`python -m polyedge.main --selftest` is a quick no-network sanity check.

---

## 9. Going live — read this before wiring money

The engine is built so the paper record answers the question "is there an edge?" honestly. If, after **at least 2–3 months and 100+ settled trades**, the dashboard shows positive realized P/L *concentrated in strategies whose logic you understand*, the sane path is:

1. **Manual execution first.** The dashboard's opportunity table tells you exactly what to buy and at what price; place a few trades by hand on Polymarket and compare your real fills with the paper assumptions. Paper trading has zero slippage and always gets the best ask — reality will be worse, and you want to measure *how much* worse.
2. Automated live execution would use Polymarket's `py-clob-client` with a wallet private key. That is deliberately **not** included: untested live-money code is how bankrolls die, keys in GitHub repos get drained, and it shouldn't be bolted on until the paper evidence justifies it.
3. Practical notes for you specifically: Polymarket operates on Polygon with USDC — factor in on-ramp costs and ISK/USD exposure; check the current legal/tax treatment of prediction-market trading for Icelandic residents before funding anything; and never deploy money you can't afford to lose entirely. This is not financial advice — it's a measurement instrument.

---

## 10. Honest limitations

- **The live APIs were not called during development** (built in a sandboxed environment). All logic is tested against realistic fixtures, and the API layer parses defensively — any market it can't parse is skipped and counted, never fatal. If Polymarket has changed a field shape, the first real Actions run will show it in the logs; expect possibly one small fix on day one.
- **Paper fills are optimistic**: best ask, no slippage, no partial-fill risk, no gas. Treat paper P/L as an upper bound.
- **30-minute cadence misses most true arbs.** The ARB scan is best understood as measuring how often free money appears, not capturing all of it.
- **LONGSHOT's edge rests on the 0.60 haircut assumption** until your own settled-trade data calibrates it. It can be negative-EV if the assumption is wrong.
- **REL locks are only as sound as your declared relations** and the markets' resolution rules.
- **Demo data is fake.** Delete `state/paper_state.json` to start the real record.

*Windows 95 aesthetic non-negotiable. It has been preserved.*
