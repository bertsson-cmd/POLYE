# Going live — deployment guide

Read this fully before wiring a real wallet to anything. It assumes you've
already read `MANUAL.md` section 9 ("Going live") and have a paper record
you trust. Nothing in this repo makes the bot trade real money by itself —
every step below is something you do on purpose.

---

## 0. Before you start

- **Evidence-gated, not calendar-gated.** The original bar was "100 settled
  trades under the CURRENT config, or a target date, whichever comes
  first" — the spirit of that (don't go live on a thin or contaminated
  record) matters more than the exact number. If `state/paper_state.json`
  looks freshly reset or thin, wait.
- **`live.py` has never touched the real Polymarket API.** It targets CLOB
  V2 (`py-clob-client-v2`) — Polymarket cut over to V2 on April 28, 2026;
  the old V1 client does not work against production at all — built
  against the V2 SDK's actual source with no network access in the
  sandbox that wrote it, and is fully unit-tested with the order
  placement call mocked out. Expect a possible small fix on day one — this
  is exactly why the dry-run stage below exists. Do not skip it.
- **Never put the private key anywhere but the VPS's own `chmod 600` env
  file.** Not in the repo, not in a commit, not in a chat message, not in
  a log line.

---

## 1. Wallet setup

1. Create a **dedicated** wallet for this bot — do not reuse a wallet that
   holds other funds. If the bot has a bug, you want the blast radius
   capped at exactly what you funded it with.
2. Fund it on **Polygon** with USDC.e (bridged USDC — Polymarket's
   on-ramp token) — plus a small amount of POL/MATIC for gas on approvals.
3. Note the wallet's private key and its address (the `funder` address —
   for a plain EOA wallet these are the same address; if you're using
   Polymarket's proxy-wallet / email-login flow, the funder address is
   the proxy address, not the EOA — check Polymarket's own docs for your
   specific account type before assuming).
4. Approve USDC.e spending for Polymarket's exchange contracts once, from
   that wallet, via Polymarket's own UI — `py-clob-client-v2` does not do
   this for you.
5. **Wrap USDC.e into pUSD.** Since CLOB V2, Polymarket's own collateral
   token is pUSD, not raw USDC.e — the UI wraps automatically when you
   deposit through it, but **API/programmatic traders (this bot) must call
   the Collateral Onramp's `wrap()` function themselves**; the CLOB does
   not do it for you when an order comes in through the API. Do this once,
   by hand, via Polymarket's own UI or a wallet transaction — this bot
   deliberately never calls `wrap()` itself (an infrequent, operator-driven
   step, not something worth automating blind). `live.py` checks tradeable
   pUSD balance/allowance before every live order and halts loudly with a
   clear log message if this step was skipped, rather than repeatedly
   trying and failing.
6. **Signature type: POLY_PROXY (1) vs. deposit wallet (3).** This repo
   defaults to `signature_type=1` (POLY_PROXY), matching the Magic/
   email-login proxy-wallet setup above. CLOB V2 also offers
   `signature_type=3` (POLY_1271, a "deposit wallet" pattern with its own
   distinct address, recommended by Polymarket for brand-new API
   integrations) — this bot does **not** use it by default: as of this
   bot's V2 rewrite, POLY_1271 has open, unresolved bugs in
   `py-clob-client-v2` for programmatic order placement (the CLOB rejects
   the order because the API key binds to the owner EOA, not the deposit
   wallet POLY_1271 requires as the order signer). If you want to switch
   to it anyway, that's a deliberate decision requiring its own
   separately-funded wallet — don't flip `POLYEDGE_SIGNATURE_TYPE` without
   re-verifying that bug is fixed first.
7. Factor in on-ramp costs and any FX exposure converting to USDC.e, and
   check the current legal/tax treatment of prediction-market trading in
   your jurisdiction before funding anything. This is not financial
   advice — it's a measurement instrument.

---

## 2. VPS setup

A small VPS is enough — this is a lightweight polling loop, not a
compute-heavy service. Hetzner's cheapest shared vCPU box (~€4/mo) is
plenty. Ubuntu 24.04 LTS assumed below.

```bash
# as root, once
adduser --disabled-password --gecos "" polybert
usermod -aG sudo polybert       # optional, only if you want it to sudo
```

Then as `polybert`:

```bash
sudo apt update && sudo apt install -y python3-venv python3-pip git
git clone https://github.com/bertsson-cmd/POLYE.git ~/polye
cd ~/polye
python3 -m venv .venv
.venv/bin/pip install -r requirements-live.txt   # adds py-clob-client-v2 + flask
                                                  # on top of requirements.txt

cp polybert.env.example polybert.env
chmod 600 polybert.env
$EDITOR polybert.env             # fill in the real private key, funder
                                  # address, and control-panel token
```

Leave `POLYEDGE_DRY_RUN=1` in `polybert.env` for now — that's the next
section.

---

## 3. Install the systemd service

```bash
sudo cp polybert.service /etc/systemd/system/polybert.service
sudo systemctl daemon-reload
sudo systemctl enable polybert
sudo systemctl start polybert
journalctl -u polybert -f
```

At this point the bot is running with `POLYEDGE_LIVE=1` but
`POLYEDGE_DRY_RUN=1` and (assuming you haven't created one) no `ARMED`
file — two of the three live-order gates are already closed even with
`POLYEDGE_LIVE=1` set, so nothing real can happen yet. You should see scan
cycles in the journal and `[DRY RUN] would open ...` log lines for
anything the risk engine would have funded.

---

## 4. Dry-run stage — minimum 24-48 hours

With the service running as above:

1. Watch `journalctl -u polybert -f` periodically, not continuously.
   You're checking that: events parse, order books fetch, opportunities
   get found and sized, and the `[DRY RUN] would open ...` log lines look
   sane (same shape as the paper dashboard's opportunity table).
2. Compare a handful of `[DRY RUN]` entries against what you'd see by hand
   on Polymarket's site at roughly the same moment — same market, similar
   price. This is your only real signal that the API-parsing layer
   survived contact with the live network, since it was never tested
   against it.
3. Do **not** create the `ARMED` file or flip `POLYEDGE_DRY_RUN` during
   this stage, no matter how good it looks after a few hours. Let it run
   the full 24-48h+ window.
4. If you see repeated errors, tracebacks, or anything that looks like a
   parsing mismatch, stop and fix it before continuing — this is the
   "expect a small day-one fix" moment the rest of this doc warned about.

---

## 5. Going live — the actual sequence

Only after step 4 looks clean, and only with your own explicit go-ahead
(not something to do because a checklist said so):

**Before arming, verify pUSD — not USDC.e — is what's actually funded:**
check the funder wallet's pUSD balance directly in Polymarket's own UI
(or via `reconcile.py`'s next scheduled check, if `POLYEDGE_FUNDER_ADDRESS`
is already set). If §1 step 5's wrap never happened, `live.py` will refuse
to trade and halt loudly the moment it's armed — better to catch it here
first.

```bash
cd ~/polye
touch ARMED                      # physical presence in the working
                                  # directory -- gate #2 of 3
$EDITOR polybert.env             # set POLYEDGE_DRY_RUN=0 -- gate #3 of 3
sudo systemctl restart polybert  # picks up the env change
journalctl -u polybert -f
```

Start small on purpose, independent of whatever `polybert.env.example`'s
$100 profile already caps you at:

- Watch the very first real fill closely. Confirm in the journal that an
  order was placed, and separately confirm on Polymarket's own site (or
  block explorer) that it actually happened and at the price you expect.
- Leave `LIVE_MAX_DAILY_LOSS` (env: `POLYEDGE_LIVE_MAX_DAILY_LOSS`) at its
  conservative default ($15) until you have real fills to calibrate
  against, not paper ones.
- Multi-leg locks (ARB/REL) stay refused by default
  (`POLYEDGE_LIVE_MULTILEG=0`). Only single-leg CONVERGE/LONGSHOT trade
  live out of the box. Leave it that way unless you specifically
  understand and accept the legging risk of an unhedged partial fill.

### Stopping / pulling back

- **Immediate stop, no new orders:** `rm ~/polye/ARMED` (or `sudo
  systemctl stop polybert`). Existing open positions are untouched — this
  only blocks new opens/closes, it does not unwind anything.
- **Daily-loss halt fired on its own:** you'll see a `HALTED` file appear
  in the working directory and `LIVE TRADING HALTED: ...` in the journal.
  `live_gates_open()` returns `False` for *everything* — opens and closes
  — until you manually `rm HALTED` after checking positions by hand. This
  is deliberate: an automatic unwind on the same bad data that triggered
  the halt could make things worse.
- **Liquidating open positions:** use the control panel's kill switch or
  per-position liquidate button (see below) rather than editing state by
  hand.

---

## 6. The manual control panel

A small Flask app (`control_server.py`) and dark-themed web panel let you
pause new trades, force-liquidate everything, liquidate one position, cap
total allocation, and set a per-position stop-loss — all reachable from a
browser while the bot runs live. See `MANUAL.md`'s control-panel section
for the routes, and the security note below before exposing it anywhere.

Run it as its own systemd unit (`polybert-control.service`) alongside
`polybert.service`, sharing the same `polybert.env` for the control-panel
token:

```ini
[Unit]
Description=PolyBert manual control panel
After=network-online.target

[Service]
Type=simple
User=polybert
Group=polybert
WorkingDirectory=/home/polybert/polye
EnvironmentFile=/home/polybert/polye/polybert.env
ExecStart=/home/polybert/polye/.venv/bin/python /home/polybert/polye/control_server.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

**Security note:** a control panel that can liquidate real money needs
more than a shared token if it's exposed on the open internet. Bind Flask
to `127.0.0.1` (the default `control_server.py` ships with) and reach it
over an SSH tunnel (`ssh -L 8787:127.0.0.1:8787 polybert@your-vps`) or
Tailscale, not an open port.

---

## 7. Honest limitations, carried over from MANUAL.md

- The live order-placement code path (`LiveEngine._place_order`) has never
  been exercised against Polymarket's real API — see step 4's dry-run
  stage, which exists specifically to surface that.
- `py-clob-client-v2`'s exact response shape for `create_and_post_order`
  is assumed from its source (`status` in `{"matched", "filled"}` means
  fully filled), carried forward unverified from the V1 assumption since
  the actual V2 response body could not be confirmed during the rewrite;
  if a real response uses different field names, the first live order
  will show it in the journal as a fill that silently doesn't record —
  annoying but safe, since nothing is recorded without a confirmed fill.
- The exact decimal count for pUSD (used by `reconcile.py` to convert its
  raw on-chain balance read into dollars) could not be independently
  confirmed and is assumed to be 6, matching USDC.e — cross-check
  `reconcile.py`'s first real result against your wallet's actual pUSD
  balance shown in Polymarket's UI before trusting the divergence-halt
  math, and fix `_PUSD_DECIMALS` in `polyedge/reconcile.py` if they don't
  match.
- `polyedge/fees.py`'s per-category fee table was re-confirmed during the
  V2 rewrite, but `py-clob-client-v2`'s own `FeeDetails` type supports a
  per-market fee "exponent" beyond the simple parabola this bot assumes
  (see the comment at the top of `fees.py`) — no source found gave a
  non-default exponent for any category, but this is a static table, not
  a live per-market lookup, and could go stale if that changes.
- Gas costs, USDC on/off-ramp costs, the one-time pUSD wrap, and any FX
  exposure are not modeled anywhere in the risk engine — they are real
  costs on top of whatever the dashboard shows.
- **`py-clob-client-v2`'s `get_balance_allowance()` is confirmed broken
  for signature_type=1 (POLY_PROXY) accounts** — reproduced live: it
  returned `balance=0, allowance=0` for a funder/proxy wallet
  independently confirmed (via a direct on-chain read) to hold real
  pUSD. Root cause, confirmed against the SDK's own source: the
  request it sends only ever includes `signature_type` (read from the
  client's own construction-time value) — it never sends a funder or
  address at all, and `BalanceAllowanceParams.signature_type` is a
  dead field the method body never reads, so there was no parameter
  that could have fixed this from the caller's side.
  `_check_pusd_balance()` in `live.py` now reads pUSD balance via a
  direct on-chain call instead (the same proven-correct method
  `reconcile.py` uses) rather than trusting that SDK response. This is
  almost certainly the same bug class as
  [Polymarket/py-clob-client-v2#70](https://github.com/Polymarket/py-clob-client-v2/issues/70),
  [#77](https://github.com/Polymarket/py-clob-client-v2/issues/77), and
  [#64](https://github.com/Polymarket/py-clob-client-v2/issues/64)
  (signature-type-aware address resolution not honoring the configured
  funder) — but all three of those are filed specifically against
  signature_type=3 (POLY_1271/deposit wallets); a search of that repo's
  issues turned up nothing filed for this exact POLY_PROXY (1) variant,
  so it is likely a genuinely new report rather than a duplicate. Filing
  a new issue against `Polymarket/py-clob-client-v2` documenting this
  needs the repo owner's go-ahead, since it's a visible action on a
  third-party project — it has **not** been filed yet as of this note.
- **Consequence: `_check_pusd_balance()` now only verifies balance, not
  allowance.** There is no proven-correct way to check this account's
  exchange-contract pUSD allowance either — the SDK response above is
  unreliable for the same reason, and a direct on-chain `allowance()`
  call would need a confirmed exchange/spender contract address that
  has not been verified (do not guess at this — confirm it before ever
  relying on it). If USDC.e was wrapped into pUSD but the exchange
  contract was never approved, this check will pass but the real order
  will still fail — that failure surfaces as an unfilled order in
  `_place_order`, not as an earlier, clearer halt. Cross-check allowance
  by hand (Polymarket's own UI, or watching the first real order
  attempt closely during the dry-run stage) until this gap is closed.
