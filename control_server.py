"""Manual control panel -- Flask app.

A small dark-themed web UI + JSON API for controlling a live LiveEngine
deployment from a browser: pause new trades, force a full kill-switch
liquidation, liquidate one position, cap total allocation, and set a
per-position stop-loss. This process only ever reads/writes
state/controls.json (polyedge/controls.py) -- it never talks to the
exchange itself. run_forever.py's apply_controls() is what actually acts
on the file, once per scan cycle.

Run standalone:
    POLYBERT_CONTROL_TOKEN=... python control_server.py

Every route that changes state requires the X-Control-Token header to
match POLYBERT_CONTROL_TOKEN exactly (constant-time compare via
hmac.compare_digest, never plain ==). The server refuses to start at all
if no token is configured -- there is no "run wide open" mode.

Bind to 127.0.0.1 (the default below) and reach it over an SSH tunnel or
Tailscale, not an open port -- see LIVE.md.
"""
import hmac
import os

from flask import Flask, Response, jsonify, request

from polyedge import controls
from polyedge.paper import PaperEngine

app = Flask(__name__)

_HERE = os.path.dirname(os.path.abspath(__file__))


def _token() -> str:
    return os.environ.get("POLYBERT_CONTROL_TOKEN", "")


def _authorized(req) -> bool:
    expected = _token()
    if not expected:
        return False   # never authorize against an unconfigured token
    got = req.headers.get("X-Control-Token", "")
    return hmac.compare_digest(got, expected)


def _authorized_browser(req) -> bool:
    """Same check, but also accepts the token as a ?token= query param --
    for a plain <a href> page load (like /dashboard), a browser can't be
    made to send a custom header the way fetch() can. NEVER accept this
    for POST/mutating routes -- those stay header-only, since a token in
    a URL ends up in browser history and server access logs."""
    if _authorized(req):
        return True
    expected = _token()
    if not expected:
        return False
    got = req.args.get("token", "")
    return hmac.compare_digest(got, expected)


def _require_auth_browser():
    if not _authorized_browser(request):
        return jsonify({"error": "unauthorized"}), 401
    return None


def _require_auth():
    if not _authorized(request):
        return jsonify({"error": "unauthorized"}), 401
    return None


def _engine() -> PaperEngine:
    # Read/mutate the same state/ directory LiveEngine uses. A plain
    # PaperEngine is enough here -- this process only reads state and
    # writes controls.json; it is apply_controls() (run inside
    # run_forever.py, against the real LiveEngine) that ever places an
    # order.
    return PaperEngine()


@app.route("/")
def index():
    with open(os.path.join(_HERE, "control_panel.html")) as f:
        return Response(f.read(), mimetype="text/html")


@app.route("/dashboard")
def dashboard():
    """This VPS's own live/dry-run dashboard -- separate from the public
    GitHub Pages dashboard, which only ever shows the GitHub-Actions-run
    paper bot and never sees anything that happens here. Reuses exactly
    the same rendering as report.py's paper dashboard for visual
    consistency, just fed this process's own state and mode instead."""
    err = _require_auth_browser()
    if err:
        return err
    from polyedge import live, report
    engine = _engine()
    if live.halted():
        mode = "HALTED"
    elif live.armed() and not live.dry_run() and live.live_enabled():
        mode = "LIVE"
    elif live.live_enabled():
        mode = "Dry-run"
    else:
        mode = "Paper"
    html = report.render_dashboard_html(
        engine.state, opportunities=[],
        control_panel_url=f"/?token={request.args.get('token', '')}",
        mode_label=mode)
    return Response(html, mimetype="text/html")


@app.route("/api/state")
def api_state():
    err = _require_auth()
    if err:
        return err
    engine = _engine()
    ctrl = controls.load(engine.state_dir)
    stats = engine.stats()
    positions = []
    for pos in engine.state["positions"]:
        positions.append({
            "key": pos["key"],
            "strategy": pos["strategy"],
            "title": pos["title"],
            "cost": pos["cost"],
            "current_value": pos.get("current_value"),
            "unrealized_pl": pos.get("unrealized_pl"),
            "unrealized_pl_pct": pos.get("unrealized_pl_pct"),
            "multi_leg": len(pos["legs"]) > 1,
            "stop_loss_pct": ctrl["stop_loss_pct"].get(pos["key"]),
            "queued_for_liquidation": pos["key"] in ctrl["liquidate_queue"],
        })
    return jsonify({
        "controls": ctrl,
        "stats": stats,
        "positions": positions,
        "halted": os.path.exists("HALTED"),
        "armed": os.path.exists("ARMED"),
    })


@app.route("/api/pause", methods=["POST"])
def api_pause():
    err = _require_auth()
    if err:
        return err
    body = request.get_json(force=True, silent=True) or {}
    return jsonify(controls.set_paused(bool(body.get("paused"))))


@app.route("/api/killswitch", methods=["POST"])
def api_killswitch():
    err = _require_auth()
    if err:
        return err
    body = request.get_json(force=True, silent=True) or {}
    return jsonify(controls.set_kill_switch(bool(body.get("on"))))


@app.route("/api/liquidate", methods=["POST"])
def api_liquidate():
    err = _require_auth()
    if err:
        return err
    body = request.get_json(force=True, silent=True) or {}
    key = body.get("key")
    if not key:
        return jsonify({"error": "missing key"}), 400
    return jsonify(controls.queue_liquidate(key))


@app.route("/api/allocation", methods=["POST"])
def api_allocation():
    err = _require_auth()
    if err:
        return err
    body = request.get_json(force=True, silent=True) or {}
    usd = body.get("max_allocation_usd")
    return jsonify(controls.set_max_allocation(usd))


@app.route("/api/stop_loss", methods=["POST"])
def api_stop_loss():
    err = _require_auth()
    if err:
        return err
    body = request.get_json(force=True, silent=True) or {}
    key = body.get("key")
    if not key:
        return jsonify({"error": "missing key"}), 400
    return jsonify(controls.set_stop_loss(key, body.get("pct")))


if __name__ == "__main__":
    if not _token():
        raise SystemExit("POLYBERT_CONTROL_TOKEN must be set in the environment "
                         "-- refusing to start unauthenticated")
    port = int(os.environ.get("POLYBERT_CONTROL_PORT", 8787))
    # Flask's built-in server is fine here: single operator, low traffic,
    # bound to localhost and reached over a tunnel (see LIVE.md). Swap for
    # waitress/gunicorn only if that stops being true.
    host = os.environ.get("POLYBERT_CONTROL_HOST", "127.0.0.1")
    app.run(host=host, port=port)
