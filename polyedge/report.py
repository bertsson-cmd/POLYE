"""Dashboard generator — PolyBert.

Writes docs/index.html — a self-contained dark terminal dashboard:
  * stat cards: equity, return, cash, capital in open positions, realized P/L
  * equity & realized P/L chart (glowing line, gradient fill)
  * trade map: every open/close plotted over time, colored by strategy
  * open positions (entry vs current, unrealized P/L), trade log, latest scan

The state JSON is embedded directly, so the page needs no server and can be
published on GitHub Pages as-is.
"""
import json
import os

from . import config

_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PolyBert — Paper Trading</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;700&family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root{
    --bg:#070b16; --panel:#0d1424; --panel2:#101a30; --line:#1c2a47;
    --text:#e8eeff; --muted:#7d8db0;
    --cyan:#38e1ff; --blue:#4f7cff; --green:#2ee6a8; --red:#ff5c7a;
    --amber:#ffc857; --pink:#ff6bd6;
  }
  *{box-sizing:border-box}
  body{background:var(--bg);color:var(--text);margin:0;padding:24px;
       font-family:Inter,system-ui,sans-serif;font-size:13px;
       background-image:radial-gradient(1200px 500px at 70% -10%,rgba(79,124,255,.10),transparent),
                        radial-gradient(900px 400px at 10% 0%,rgba(56,225,255,.06),transparent)}
  .wrap{max-width:1100px;margin:0 auto}
  header{display:flex;align-items:center;justify-content:space-between;
         flex-wrap:wrap;gap:10px;margin-bottom:22px}
  .brand{display:flex;align-items:center;gap:12px}
  .logo{width:34px;height:34px;border-radius:9px;
        background:linear-gradient(135deg,var(--cyan),var(--blue));
        display:flex;align-items:center;justify-content:center;
        font-family:"Space Grotesk";font-weight:700;color:#04121e;font-size:15px;
        box-shadow:0 0 18px rgba(56,225,255,.35)}
  h1{font-family:"Space Grotesk",sans-serif;font-weight:700;font-size:21px;
     margin:0;letter-spacing:.2px}
  h1 small{color:var(--muted);font-family:Inter;font-weight:500;font-size:12px;
           margin-left:10px;letter-spacing:.4px}
  .badge{border:1px solid var(--line);border-radius:999px;padding:5px 12px;
         color:var(--muted);font-size:11px;letter-spacing:.12em;text-transform:uppercase}
  .badge b{color:var(--cyan);font-weight:600}
  .stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
         gap:12px;margin-bottom:18px}
  .stat{background:var(--panel);border:1px solid var(--line);border-radius:14px;
        padding:14px 16px}
  .stat .l{color:var(--muted);font-size:10.5px;letter-spacing:.14em;
           text-transform:uppercase;margin-bottom:7px}
  .stat .v{font-size:20px;font-weight:600;font-variant-numeric:tabular-nums;
           letter-spacing:-.2px}
  .stat .s{color:var(--muted);font-size:11px;margin-top:4px;
           font-variant-numeric:tabular-nums}
  .up{color:var(--green)} .dn{color:var(--red)}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:16px;
        padding:18px 18px 14px;margin-bottom:18px}
  .card h2{font-family:"Space Grotesk";font-size:14px;font-weight:500;margin:0 0 4px;
           letter-spacing:.2px}
  .card .sub{color:var(--muted);font-size:11.5px;margin-bottom:12px}
  canvas{display:block;width:100%;border-radius:10px}
  .legend{display:flex;flex-wrap:wrap;gap:14px;color:var(--muted);
          font-size:11.5px;margin-top:10px}
  .dot{display:inline-block;width:8px;height:8px;border-radius:50%;
       margin-right:6px;vertical-align:1px}
  .pill{display:inline-block;padding:2px 9px;border-radius:999px;font-size:10.5px;
        font-weight:600;letter-spacing:.04em}
  .pill.ARB{background:rgba(56,225,255,.12);color:var(--cyan)}
  .pill.REL{background:rgba(255,200,87,.12);color:var(--amber)}
  .pill.LONGSHOT{background:rgba(255,107,214,.12);color:var(--pink)}
  .pill.CONVERGE{background:rgba(46,230,168,.12);color:var(--green)}
  .scroll{max-height:320px;overflow:auto;border-radius:10px;border:1px solid var(--line)}
  table{border-collapse:collapse;width:100%;font-size:12px}
  th{position:sticky;top:0;background:var(--panel2);color:var(--muted);
     font-weight:500;text-align:left;padding:9px 12px;font-size:10.5px;
     letter-spacing:.1em;text-transform:uppercase;border-bottom:1px solid var(--line)}
  td{padding:9px 12px;border-bottom:1px solid rgba(28,42,71,.5);
     font-variant-numeric:tabular-nums;vertical-align:top}
  tr:hover td{background:rgba(79,124,255,.05)}
  td small{color:var(--muted)}
  .tip{position:fixed;display:none;background:#0b1222;border:1px solid var(--line);
       border-radius:10px;padding:9px 12px;font-size:12px;max-width:330px;
       pointer-events:none;z-index:9;box-shadow:0 10px 30px rgba(0,0,0,.5)}
  footer{color:var(--muted);font-size:11px;text-align:center;margin:26px 0 6px;
         letter-spacing:.06em}
  @media(max-width:700px){body{padding:12px}.stat .v{font-size:17px}}
</style>
</head>
<body>
<div class="tip" id="tip"></div>
<div class="wrap">

<header>
  <div class="brand">
    <div class="logo">PB</div>
    <h1>PolyBert<small>Polymarket paper-trading terminal</small></h1>
  </div>
  <div style="display:flex;gap:8px;flex-wrap:wrap">
    <span class="badge">Mode <b>Paper</b></span>
    <span class="badge" id="b-updated">—</span>
  </div>
</header>

<div class="stats" id="stats"></div>

<div class="card">
  <h2>Equity &amp; realized P/L</h2>
  <div class="sub">Equity = cash + open positions marked to market · dashed line = starting bankroll</div>
  <canvas id="equity" height="250"></canvas>
  <div class="legend">
    <span><span class="dot" style="background:var(--cyan)"></span>equity</span>
    <span><span class="dot" style="background:var(--amber)"></span>realized P/L (re-based to bankroll)</span>
  </div>
</div>

<div class="card">
  <h2>Trade map</h2>
  <div class="sub">Every fill over time · squares open on the zero line · circles close at realized P/L · hover for detail</div>
  <canvas id="map" height="250"></canvas>
  <div class="legend">
    <span><span class="pill ARB">ARB</span> Dutch-book lock</span>
    <span><span class="pill REL">REL</span> correlated lock</span>
    <span><span class="pill LONGSHOT">LS</span> longshot fade</span>
    <span><span class="pill CONVERGE">CV</span> convergence</span>
    <span style="color:var(--muted)">ring: <span class="up">profit</span> · <span class="dn">loss</span> · gray = voided</span>
  </div>
</div>

<div class="card">
  <h2>Open positions</h2>
  <div class="sub" id="open-sub"></div>
  <div class="scroll"><table id="open"></table></div>
</div>

<div class="card">
  <h2>Trade log</h2>
  <div class="sub">Settled and voided positions, most recent first</div>
  <div class="scroll"><table id="closed"></table></div>
</div>

<div class="card">
  <h2>Latest scan</h2>
  <div class="sub">Every candidate the last scan found, including ones the risk engine declined to fund</div>
  <div class="scroll"><table id="opps"></table></div>
</div>

<footer>POLYBERT · PAPER MONEY ONLY · UPDATES EVERY 5 MIN VIA GITHUB ACTIONS</footer>
</div>

<script>
const STATE = __STATE_JSON__;
const OPPS  = __OPPS_JSON__;
const CSS = getComputedStyle(document.documentElement);
const C = n => CSS.getPropertyValue(n).trim();
const COLORS = {ARB:C('--cyan'), REL:C('--amber'), LONGSHOT:C('--pink'), CONVERGE:C('--green')};
const money = v => (v<0?"-$":"$") + Math.abs(v).toFixed(2);
const dt = ts => new Date(ts*1000).toISOString().slice(0,16).replace("T"," ");

/* ---------- stats ---------- */
(function(){
  const h = STATE.history, last = h.length? h[h.length-1] : {equity:STATE.cash,cash:STATE.cash,open_value:0};
  const allClosed = STATE.closed||[];
  const isVoid = c => (c.close_reason||"").startsWith("voided");
  const closed = allClosed.filter(c=>!isVoid(c));
  const nVoided = allClosed.length - closed.length;
  const realized = closed.reduce((a,c)=>a+c.pl,0);
  const wins = closed.filter(c=>c.pl>0).length;
  const start = STATE.starting_bankroll||1;
  const ret = (last.equity/start-1)*100;
  const openCost = (STATE.positions||[]).reduce((a,p)=>a+p.cost,0);
  const openVal = last.open_value!=null ? last.open_value : openCost;
  const cells = [
    ["Equity", money(last.equity), ret>=0?"up":"dn", ""],
    ["Return", (ret>=0?"+":"")+ret.toFixed(2)+"%", ret>=0?"up":"dn", "vs $"+start.toFixed(0)+" start"],
    ["Cash", money(last.cash), "", "available to trade"],
    ["In positions", money(openCost), "", "marked value "+money(openVal)],
    ["Realized P/L", money(realized), realized>=0?"up":"dn", closed.length+" settled trades"],
    ["Open", String((STATE.positions||[]).length), "", ""],
    ["Settled", closed.length + (closed.length? " · "+(100*wins/closed.length).toFixed(0)+"% won":""), "",
      nVoided? nVoided+" voided (refunded)":""],
  ];
  document.getElementById("stats").innerHTML = cells.map(c=>
    `<div class="stat"><div class="l">${c[0]}</div><div class="v ${c[2]}">${c[1]}</div>`+
    (c[3]?`<div class="s">${c[3]}</div>`:"")+`</div>`).join("");
  document.getElementById("b-updated").innerHTML =
    "Last scan <b>" + (h.length? dt(h[h.length-1].ts)+" UTC" : "never") + "</b>";
  document.getElementById("open-sub").textContent =
    money(openCost)+" allocated across "+(STATE.positions||[]).length+" positions";
})();

/* ---------- canvas helpers ---------- */
function fitCanvas(c){
  const w = c.parentElement.clientWidth - 4;
  const scale = window.devicePixelRatio||1;
  c.style.height = c.getAttribute("height")+"px";
  c.width = w*scale; c.height = parseInt(c.getAttribute("height"))*scale;
  const g = c.getContext("2d"); g.scale(scale,scale);
  return {g, W:w, H:parseInt(c.getAttribute("height"))};
}
function grid(g,W,H){
  g.clearRect(0,0,W,H);
  g.strokeStyle="rgba(28,42,71,.55)"; g.lineWidth=1;
  for(let y=0;y<H;y+=42){g.beginPath();g.moveTo(0,y);g.lineTo(W,y);g.stroke();}
}

/* ---------- equity chart ---------- */
(function(){
  const c=document.getElementById("equity");
  const {g,W,H}=fitCanvas(c); grid(g,W,H);
  const h=STATE.history;
  if(h.length<2){g.fillStyle=C('--muted');g.font="12px Inter";
    g.fillText("Not enough history yet — more scans coming.",12,24);return;}
  const P={l:58,r:14,t:14,b:26};
  const t0=h[0].ts, t1=h[h.length-1].ts||t0+1;
  const base = STATE.starting_bankroll;
  const eq=h.map(p=>p.equity), rl=h.map(p=>base+p.realized_total);
  let lo=Math.min(...eq,...rl), hi=Math.max(...eq,...rl);
  const pad=(hi-lo)*0.12||1; lo-=pad; hi+=pad;
  const X=ts=>P.l+(W-P.l-P.r)*(ts-t0)/Math.max(1,(t1-t0));
  const Y=v=>H-P.b-(H-P.t-P.b)*(v-lo)/(hi-lo);
  g.fillStyle=C('--muted'); g.font="10.5px Inter";
  for(let i=0;i<=4;i++){const v=lo+(hi-lo)*i/4; g.fillText("$"+v.toFixed(0),8,Y(v)+3);}
  g.fillText(dt(t0),P.l,H-8);
  const eLbl=dt(t1); g.fillText(eLbl,W-P.r-eLbl.length*5.6,H-8);
  // baseline
  g.strokeStyle="rgba(125,141,176,.5)"; g.setLineDash([5,4]);
  g.beginPath(); g.moveTo(P.l,Y(base)); g.lineTo(W-P.r,Y(base)); g.stroke(); g.setLineDash([]);
  // realized
  g.strokeStyle=C('--amber'); g.lineWidth=1.4; g.beginPath();
  h.forEach((p,i)=>{const x=X(p.ts),y=Y(base+p.realized_total); i?g.lineTo(x,y):g.moveTo(x,y);});
  g.stroke();
  // equity gradient fill
  const grad=g.createLinearGradient(0,P.t,0,H-P.b);
  grad.addColorStop(0,"rgba(56,225,255,.22)"); grad.addColorStop(1,"rgba(56,225,255,0)");
  g.beginPath();
  h.forEach((p,i)=>{const x=X(p.ts),y=Y(p.equity); i?g.lineTo(x,y):g.moveTo(x,y);});
  g.lineTo(X(t1),H-P.b); g.lineTo(X(t0),H-P.b); g.closePath();
  g.fillStyle=grad; g.fill();
  // equity line with glow
  g.strokeStyle=C('--cyan'); g.lineWidth=2; g.shadowColor="rgba(56,225,255,.8)"; g.shadowBlur=10;
  g.beginPath();
  h.forEach((p,i)=>{const x=X(p.ts),y=Y(p.equity); i?g.lineTo(x,y):g.moveTo(x,y);});
  g.stroke(); g.shadowBlur=0;
})();

/* ---------- trade map ---------- */
(function(){
  const c=document.getElementById("map");
  const {g,W,H}=fitCanvas(c); grid(g,W,H);
  const T=(STATE.trades||[]).slice().sort((a,b)=>a.ts-b.ts);
  if(!T.length){g.fillStyle=C('--muted');g.font="12px Inter";
    g.fillText("No trades yet.",12,24);return;}
  const P={l:58,r:14,t:16,b:26};
  const t0=T[0].ts, t1=T[T.length-1].ts||t0+1;
  const pls=T.map(t=>t.pl==null?0:t.pl);
  let lo=Math.min(0,...pls), hi=Math.max(0,...pls);
  const pad=(hi-lo)*0.15||1; lo-=pad; hi+=pad;
  const X=ts=>P.l+(W-P.l-P.r)*(ts-t0)/Math.max(1,(t1-t0));
  const Y=v=>H-P.b-(H-P.t-P.b)*(v-lo)/(hi-lo);
  g.fillStyle=C('--muted'); g.font="10.5px Inter";
  for(let i=0;i<=4;i++){const v=lo+(hi-lo)*i/4;
    g.fillText((v<0?"-$":"$")+Math.abs(v).toFixed(0),8,Y(v)+3);}
  g.strokeStyle="rgba(125,141,176,.5)";g.setLineDash([5,4]);
  g.beginPath();g.moveTo(P.l,Y(0));g.lineTo(W-P.r,Y(0));g.stroke();g.setLineDash([]);
  g.fillText(dt(t0),P.l,H-8);
  const eLbl=dt(t1); g.fillText(eLbl,W-P.r-eLbl.length*5.6,H-8);
  const pts=[];
  T.forEach(t=>{
    const x=X(t.ts);
    const col=COLORS[t.strategy]||"#fff";
    const r=Math.max(3,Math.min(9,Math.sqrt(t.amount||1)));
    if(t.type==="OPEN"){
      const y=Y(0);
      g.fillStyle=col; g.globalAlpha=.9;
      g.fillRect(x-r/1.5,y-r/1.5,r*1.33,r*1.33); g.globalAlpha=1;
      pts.push({x,y,r:r+2,t});
    }else{
      const y=Y(t.pl||0);
      g.fillStyle=col; g.beginPath(); g.arc(x,y,r,0,7); g.fill();
      g.strokeStyle=(t.type==="VOID")?"rgba(125,141,176,.9)":((t.pl>=0)?C('--green'):C('--red'));
      g.lineWidth=2; g.beginPath(); g.arc(x,y,r+2.5,0,7); g.stroke();
      pts.push({x,y,r:r+3,t});
    }
  });
  const tip=document.getElementById("tip");
  c.onmousemove=e=>{
    const b=c.getBoundingClientRect();
    const mx=(e.clientX-b.left)*( (c.width/(window.devicePixelRatio||1)) /b.width);
    const my=(e.clientY-b.top)*( (c.height/(window.devicePixelRatio||1)) /b.height);
    const hit=pts.slice().reverse().find(p=>Math.hypot(p.x-mx,p.y-my)<=p.r+3);
    if(hit){tip.style.display="block";
      tip.style.left=(e.clientX+14)+"px"; tip.style.top=(e.clientY+14)+"px";
      const t=hit.t;
      tip.innerHTML=`<b>${t.type}</b> <span class="pill ${t.strategy}">${t.strategy}</span><br>`+
        `${t.title}<br><small>${dt(t.ts)} UTC · ${t.type==="OPEN"?"cost":"payout"} ${money(t.amount)}</small>`+
        (t.pl==null?"":`<br><b class="${t.pl>=0?'up':'dn'}">P/L ${money(t.pl)}</b>`);
    } else tip.style.display="none";
  };
  c.onmouseleave=()=>tip.style.display="none";
})();

/* ---------- open-position helpers ---------- */
function priceMove(p){
  if(!p.legs || !p.legs.length) return "—";
  if(p.legs.length===1){
    const entry=p.legs[0].entry_price;
    const cp=p.current_prices||{};
    const cur = (cp[p.legs[0].token_id]!=null) ? cp[p.legs[0].token_id] : entry;
    const arrow = cur>entry?"▲":(cur<entry?"▼":"→");
    const cls = cur>entry?"up":(cur<entry?"dn":"");
    return `${(entry*100).toFixed(1)}¢ <span class="${cls}">${arrow}</span> ${(cur*100).toFixed(1)}¢`;
  }
  const sets = p.legs[0].shares || 1;
  const costPerSet = p.cost / sets;
  const payout = p.guaranteed_payout_sets || 1;
  return costPerSet.toFixed(3)+" → locked @ "+payout.toFixed(2);
}
function plCell(p){
  if(p.unrealized_pl==null) return "<small>pending mark</small>";
  const pct = p.unrealized_pl_pct!=null
    ? ` <small>(${p.unrealized_pl_pct>=0?"+":""}${p.unrealized_pl_pct.toFixed(1)}%)</small>` : "";
  const cls = p.unrealized_pl>=0?"up":"dn";
  const tag = p.guaranteed ? " <small>locked</small>" : "";
  return `<span class="${cls}"><b>${money(p.unrealized_pl)}</b></span>${pct}${tag}`;
}

/* ---------- tables ---------- */
function fill(id, head, rows){
  document.getElementById(id).innerHTML =
    "<tr>"+head.map(h=>`<th>${h}</th>`).join("")+"</tr>"+
    (rows.length? rows.join("") : `<tr><td colspan="${head.length}"><small>— none —</small></td></tr>`);
}
fill("open", ["Strategy","Position","Opened","Entry → Current","Cost","Unrealized P/L","Resolves"],
  (STATE.positions||[]).map(p=>`<tr>
    <td><span class="pill ${p.strategy}">${p.strategy}</span></td>
    <td>${p.title}<br><small>${p.note||""}</small></td>
    <td>${dt(p.opened)}</td>
    <td>${priceMove(p)}${p.guaranteed?" 🔒":""}</td>
    <td>${money(p.cost)}</td>
    <td>${plCell(p)}</td>
    <td>${(p.resolve_by||"").slice(0,10)}</td></tr>`));
fill("closed", ["Strategy","Position","Closed","Cost","Payout","P/L"],
  (STATE.closed||[]).slice().reverse().map(p=>`<tr>
    <td><span class="pill ${p.strategy}">${p.strategy}</span></td>
    <td>${p.title}${p.close_reason==="take_profit"?" <small>· early exit</small>":""}${(p.close_reason||"").startsWith("voided")?" <small>· voided, cost refunded</small>":""}</td>
    <td>${dt(p.closed_ts)}</td>
    <td>${money(p.cost)}</td><td>${money(p.payout)}</td>
    <td class="${p.pl>=0?'up':'dn'}"><b>${money(p.pl)}</b></td></tr>`));
fill("opps", ["Strategy","Opportunity","Edge","Type","Note"],
  (OPPS||[]).map(o=>`<tr>
    <td><span class="pill ${o.strategy}">${o.strategy}</span></td>
    <td>${o.title}</td><td>${(o.edge*100).toFixed(2)}%</td>
    <td>${o.guaranteed?"lock 🔒":"probabilistic"}</td>
    <td><small>${o.note||""}</small></td></tr>`));
</script>
</body>
</html>
"""


def write_dashboard(state: dict, opportunities=None, docs_dir: str = None) -> str:
    docs_dir = docs_dir or config.DOCS_DIR
    os.makedirs(docs_dir, exist_ok=True)
    html = (_TEMPLATE
            .replace("__STATE_JSON__", json.dumps(state))
            .replace("__OPPS_JSON__", json.dumps(opportunities or [])))
    path = os.path.join(docs_dir, "index.html")
    with open(path, "w") as f:
        f.write(html)
    return path
