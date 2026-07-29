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
- **`live.py` has never touched the real Polymarket API.** It was built
  against `py-clob-client`'s documented surface with no network access in
  the sandbox that wrote it, and is fully unit-tested with the order
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
2. Fund it on **Polygon** with USDC (Polymarket settles in USDC on
   Polygon) — plus a small amount of POL/MATIC for gas on approvals.
3. Note the wallet's private key and its address (the `funder` address —
   for a plain EOA wallet these are the same address; if you're using
   Polymarket's proxy-wallet / email-login flow, the funder address is
   the proxy address, not the EOA — check Polymarket's own docs for your
   specific account type before assuming).
4. Approve USDC spending for Polymarket's exchange contracts once, from
   that wallet, via Polymarket's own UI — `py-clob-client` does not do
   this for you.
5. Factor in on-ramp costs and any FX exposure converting to USDC, and
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
.venv/bin/pip install -r requirements-live.txt   # adds py-clob-client + flask
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
- `py-clob-client`'s exact response shape for a FOK order is assumed from
  its published API (`status` in `{"matched", "filled"}` means fully
  filled); if a real response uses different field names, the first live
  order will show it in the journal as a fill that silently doesn't
  record — annoying but safe, since nothing is recorded without a
  confirmed fill.
- Gas costs, USDC on/off-ramp costs, and any FX exposure are not modeled
  anywhere in the risk engine — they are real costs on top of whatever the
  dashboard shows.
