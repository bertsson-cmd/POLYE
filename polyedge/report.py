"""Dashboard generator.

Writes docs/index.html — a fully self-contained Windows 95-styled page:
  * SYSTEM MONITOR window: equity curve + realized P/L (CRT green-on-black)
  * TRADE MAP window: every open/close plotted over time, colored by
    strategy, sized by amount; closes ring green (profit) or red (loss)
  * stats, open positions, trade log, and latest scan opportunities

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
<title>PolyEdge 95 v2 — Paper Trading Terminal</title>
<style>
  :root{
    --desk:#008080; --face:#c0c0c0; --hl:#ffffff; --sh:#808080; --dk:#000000;
    --title:#000080; --title2:#1084d0; --crt:#00ff66; --crtdim:#00802f;
    --arb:#00ffff; --rel:#ffff00; --ls:#ff00ff; --cv:#00ff66;
  }
  *{box-sizing:border-box}
  body{background:var(--desk);margin:0;padding:14px;
       font-family:Tahoma,"MS Sans Serif",Geneva,sans-serif;font-size:12px;color:#000}
  .win{background:var(--face);border:2px solid;
       border-color:var(--hl) var(--dk) var(--dk) var(--hl);
       box-shadow:1px 1px 0 var(--dk);margin:0 auto 14px;max-width:980px}
  .tbar{background:linear-gradient(90deg,var(--title),var(--title2));color:#fff;
        font-weight:bold;padding:3px 6px;display:flex;justify-content:space-between;align-items:center}
  .tbar .btns span{display:inline-block;width:16px;height:14px;background:var(--face);
        border:1px solid;border-color:var(--hl) var(--dk) var(--dk) var(--hl);
        margin-left:2px;text-align:center;line-height:12px;color:#000;font-size:10px}
  .body{padding:10px}
  .inset{background:#fff;border:2px solid;border-color:var(--sh) var(--hl) var(--hl) var(--sh);padding:6px}
  .crt{background:#000;border:2px solid;border-color:var(--sh) var(--hl) var(--hl) var(--sh);display:block;width:100%}
  .statgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:8px}
  .stat{border:2px solid;border-color:var(--sh) var(--hl) var(--hl) var(--sh);
        background:#fff;padding:6px;text-align:center}
  .stat .v{font-size:18px;font-weight:bold;font-family:"Courier New",monospace}
  .green{color:#008000}.red{color:#c00000}
  table{border-collapse:collapse;width:100%;background:#fff;font-size:11px}
  th{background:var(--face);border:1px solid var(--sh);padding:3px 5px;text-align:left;
     position:sticky;top:0}
  td{border:1px solid #d0d0d0;padding:3px 5px}
  tr:nth-child(even) td{background:#f2f2f2}
  .scroll{max-height:260px;overflow:auto;border:2px solid;
          border-color:var(--sh) var(--hl) var(--hl) var(--sh)}
  .tag{display:inline-block;padding:0 4px;border:1px solid #000;font-size:10px;font-weight:bold}
  .tag.ARB{background:var(--arb)}.tag.REL{background:var(--rel)}
  .tag.LONGSHOT{background:var(--ls);color:#fff}.tag.CONVERGE{background:var(--cv)}
  .legend span{margin-right:14px;white-space:nowrap}
  .sbar{display:flex;gap:4px;border-top:2px solid var(--hl);padding:3px 6px;font-size:11px}
  .sbar div{border:1px solid;border-color:var(--sh) var(--hl) var(--hl) var(--sh);padding:1px 8px;flex:1}
  .tip{position:fixed;display:none;background:#ffffe1;border:1px solid #000;
       padding:4px 6px;font-size:11px;max-width:320px;pointer-events:none;z-index:9}
  h3{margin:4px 0 8px;font-size:12px}
  @media(max-width:700px){body{padding:6px}.statgrid{grid-template-columns:repeat(2,1fr)}}
</style>
</head>
<body>
<div class="tip" id="tip"></div>

<div class="win">
  <div class="tbar"><span>■ PolyEdge 95 v2 — Paper Trading Terminal</span>
    <span class="btns"><span>_</span><span>□</span><span>X</span></span></div>
  <div class="body">
    <div class="statgrid" id="stats"></div>
  </div>
  <div class="sbar"><div id="sb-mode">MODE: PAPER</div><div id="sb-updated"></div><div id="sb-count"></div></div>
</div>

<div class="win">
  <div class="tbar"><span>▦ System Monitor — Equity &amp; Realized P/L</span>
    <span class="btns"><span>_</span><span>□</span><span>X</span></span></div>
  <div class="body">
    <canvas id="equity" class="crt" height="240"></canvas>
    <div class="legend" style="margin-top:6px">
      <span style="color:#006400">■ equity (cash + open marked value)</span>
      <span style="color:#808000">■ realized P/L (closed trades only)</span>
    </div>
  </div>
</div>

<div class="win">
  <div class="tbar"><span>◆ Trade Map — every fill, mapped by time and P/L</span>
    <span class="btns"><span>_</span><span>□</span><span>X</span></span></div>
  <div class="body">
    <canvas id="map" class="crt" height="260"></canvas>
    <div class="legend" style="margin-top:6px">
      <span><span class="tag ARB">ARB</span> Dutch-book lock</span>
      <span><span class="tag REL">REL</span> correlated lock</span>
      <span><span class="tag LONGSHOT">LS</span> longshot fade</span>
      <span><span class="tag CONVERGE">CV</span> convergence</span>
      <span>□ open &nbsp; ○ close (green ring = profit, red = loss)</span>
    </div>
  </div>
</div>

<div class="win">
  <div class="tbar"><span>▤ Open Positions</span><span class="btns"><span>_</span><span>□</span><span>X</span></span></div>
  <div class="body"><div class="scroll"><table id="open"></table></div></div>
</div>

<div class="win">
  <div class="tbar"><span>▥ Trade Log (settled)</span><span class="btns"><span>_</span><span>□</span><span>X</span></span></div>
  <div class="body"><div class="scroll"><table id="closed"></table></div></div>
</div>

<div class="win">
  <div class="tbar"><span>☰ Latest Scan — candidate opportunities</span><span class="btns"><span>_</span><span>□</span><span>X</span></span></div>
  <div class="body"><div class="scroll"><table id="opps"></table></div></div>
</div>

<script>
const STATE = __STATE_JSON__;
const OPPS  = __OPPS_JSON__;
const COLORS = {ARB:"#00ffff", REL:"#ffff00", LONGSHOT:"#ff00ff", CONVERGE:"#00ff66"};
const money = v => (v<0?"-$":"$") + Math.abs(v).toFixed(2);
const dt = ts => new Date(ts*1000).toISOString().slice(0,16).replace("T"," ");

/* ---------- stats ---------- */
(function(){
  const h = STATE.history, last = h.length? h[h.length-1] : {equity:STATE.cash,cash:STATE.cash};
  const closed = STATE.closed||[];
  const realized = closed.reduce((a,c)=>a+c.pl,0);
  const wins = closed.filter(c=>c.pl>0).length;
  const start = STATE.starting_bankroll||1;
  const ret = (last.equity/start-1)*100;
  const cells = [
    ["EQUITY", money(last.equity), ret>=0?"green":"red"],
    ["RETURN", (ret>=0?"+":"")+ret.toFixed(2)+"%", ret>=0?"green":"red"],
    ["CASH", money(last.cash), ""],
    ["REALIZED P/L", money(realized), realized>=0?"green":"red"],
    ["OPEN POS", (STATE.positions||[]).length, ""],
    ["SETTLED", closed.length + (closed.length? " ("+(100*wins/closed.length).toFixed(0)+"% won)" : ""), ""],
  ];
  document.getElementById("stats").innerHTML = cells.map(c=>
    `<div class="stat"><div>${c[0]}</div><div class="v ${c[2]}">${c[1]}</div></div>`).join("");
  document.getElementById("sb-updated").textContent =
    "LAST SCAN: " + (h.length? dt(h[h.length-1].ts) : "never") + " UTC";
  document.getElementById("sb-count").textContent =
    "HISTORY: " + h.length + " scans";
})();

/* ---------- canvas helpers ---------- */
function fitCanvas(c){
  const w = c.parentElement.clientWidth - 4;
  c.width = w; // logical px; crisp retro look is fine
  return {W:w, H:c.height};
}
function crtGrid(g,W,H){
  g.fillStyle="#000"; g.fillRect(0,0,W,H);
  g.strokeStyle="#003318"; g.lineWidth=1;
  for(let x=0;x<W;x+=Math.max(30,W/20)){g.beginPath();g.moveTo(x,0);g.lineTo(x,H);g.stroke();}
  for(let y=0;y<H;y+=30){g.beginPath();g.moveTo(0,y);g.lineTo(W,y);g.stroke();}
}

/* ---------- equity chart ---------- */
(function(){
  const c=document.getElementById("equity"), g=c.getContext("2d");
  const {W,H}=fitCanvas(c); crtGrid(g,W,H);
  const h=STATE.history;
  if(h.length<2){g.fillStyle="#00ff66";g.font="12px monospace";
    g.fillText("Not enough history yet — run more scans.",10,20);return;}
  const P={l:56,r:12,t:12,b:24};
  const t0=h[0].ts, t1=h[h.length-1].ts||t0+1;
  const eq=h.map(p=>p.equity), rl=h.map(p=>p.realized_total);
  const base = STATE.starting_bankroll;
  let lo=Math.min(...eq,...rl.map(v=>v+base)), hi=Math.max(...eq,...rl.map(v=>v+base));
  const pad=(hi-lo)*0.1||1; lo-=pad; hi+=pad;
  const X=ts=>P.l+(W-P.l-P.r)*(ts-t0)/Math.max(1,(t1-t0));
  const Y=v=>H-P.b-(H-P.t-P.b)*(v-lo)/(hi-lo);
  // axis labels
  g.fillStyle="#00b34d"; g.font="10px monospace";
  for(let i=0;i<=4;i++){const v=lo+(hi-lo)*i/4;
    g.fillText("$"+v.toFixed(0), 4, Y(v)+3);}
  g.fillText(dt(t0), P.l, H-8); 
  const endLbl=dt(t1); g.fillText(endLbl, W-P.r-endLbl.length*6, H-8);
  // baseline (starting bankroll)
  g.strokeStyle="#006633"; g.setLineDash([4,3]);
  g.beginPath(); g.moveTo(P.l,Y(base)); g.lineTo(W-P.r,Y(base)); g.stroke();
  g.setLineDash([]);
  // realized P/L (offset to bankroll baseline so lines share a scale)
  g.strokeStyle="#b3b300"; g.lineWidth=1.5; g.beginPath();
  h.forEach((p,i)=>{const x=X(p.ts),y=Y(base+p.realized_total); i?g.lineTo(x,y):g.moveTo(x,y);});
  g.stroke();
  // equity line with CRT glow
  g.strokeStyle="#00ff66"; g.lineWidth=2; g.shadowColor="#00ff66"; g.shadowBlur=6;
  g.beginPath();
  h.forEach((p,i)=>{const x=X(p.ts),y=Y(p.equity); i?g.lineTo(x,y):g.moveTo(x,y);});
  g.stroke(); g.shadowBlur=0;
})();

/* ---------- trade map ---------- */
(function(){
  const c=document.getElementById("map"), g=c.getContext("2d");
  const {W,H}=fitCanvas(c); crtGrid(g,W,H);
  const T=(STATE.trades||[]).slice().sort((a,b)=>a.ts-b.ts);
  if(!T.length){g.fillStyle="#00ff66";g.font="12px monospace";
    g.fillText("No trades yet.",10,20);return;}
  const P={l:56,r:12,t:14,b:24};
  const t0=T[0].ts, t1=T[T.length-1].ts||t0+1;
  const pls=T.map(t=>t.pl==null?0:t.pl);
  let lo=Math.min(0,...pls), hi=Math.max(0,...pls);
  const pad=(hi-lo)*0.15||1; lo-=pad; hi+=pad;
  const X=ts=>P.l+(W-P.l-P.r)*(ts-t0)/Math.max(1,(t1-t0));
  const Y=v=>H-P.b-(H-P.t-P.b)*(v-lo)/(hi-lo);
  g.fillStyle="#00b34d"; g.font="10px monospace";
  for(let i=0;i<=4;i++){const v=lo+(hi-lo)*i/4;
    g.fillText((v<0?"-$":"$")+Math.abs(v).toFixed(0),4,Y(v)+3);}
  g.strokeStyle="#006633";g.setLineDash([4,3]);
  g.beginPath();g.moveTo(P.l,Y(0));g.lineTo(W-P.r,Y(0));g.stroke();g.setLineDash([]);
  g.fillText(dt(t0),P.l,H-8);
  const eLbl=dt(t1); g.fillText(eLbl,W-P.r-eLbl.length*6,H-8);
  const pts=[];
  T.forEach(t=>{
    const x=X(t.ts);
    const col=COLORS[t.strategy]||"#fff";
    const r=Math.max(3,Math.min(9,Math.sqrt(t.amount||1)));
    if(t.type==="OPEN"){
      const y=Y(0)-0; // opens sit on the zero line: no P/L yet
      g.fillStyle=col; g.fillRect(x-r/1.4,y-r/1.4,r*1.4,r*1.4);
      pts.push({x,y,r:r+2,t});
    }else{
      const y=Y(t.pl||0);
      g.fillStyle=col; g.beginPath(); g.arc(x,y,r,0,7); g.fill();
      g.strokeStyle=(t.pl>=0)?"#00ff66":"#ff3333"; g.lineWidth=2;
      g.beginPath(); g.arc(x,y,r+2,0,7); g.stroke();
      pts.push({x,y,r:r+3,t});
    }
  });
  const tip=document.getElementById("tip");
  c.onmousemove=e=>{
    const b=c.getBoundingClientRect();
    const mx=(e.clientX-b.left)*(c.width/b.width), my=(e.clientY-b.top)*(c.height/b.height);
    const hit=pts.slice().reverse().find(p=>Math.hypot(p.x-mx,p.y-my)<=p.r+3);
    if(hit){tip.style.display="block";
      tip.style.left=(e.clientX+12)+"px"; tip.style.top=(e.clientY+12)+"px";
      const t=hit.t;
      tip.innerHTML=`<b>${t.type}</b> <span class="tag ${t.strategy}">${t.strategy}</span><br>`+
        `${t.title}<br>${dt(t.ts)} UTC · ${t.type==="OPEN"?"cost":"payout"} ${money(t.amount)}`+
        (t.pl==null?"":`<br><b>P/L ${money(t.pl)}</b>`);
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
    return (entry*100).toFixed(1)+"¢ "+arrow+" "+(cur*100).toFixed(1)+"¢";
  }
  // multi-leg lock: show combined entry cost per set vs the locked payout
  const sets = p.legs[0].shares || 1;
  const costPerSet = p.cost / sets;
  const payout = p.guaranteed_payout_sets || 1;
  return costPerSet.toFixed(3)+" \u2192 locked @ "+payout.toFixed(2);
}
function plCell(p){
  if(p.unrealized_pl==null) return "<small>pending mark</small>";
  const pct = p.unrealized_pl_pct!=null
    ? ` (${p.unrealized_pl_pct>=0?"+":""}${p.unrealized_pl_pct.toFixed(1)}%)` : "";
  const cls = p.unrealized_pl>=0?"green":"red";
  const tag = p.guaranteed ? " <small>[locked]</small>" : "";
  return `<span class="${cls}"><b>${money(p.unrealized_pl)}</b>${pct}</span>${tag}`;
}

/* ---------- tables ---------- */
function fill(id, head, rows){
  document.getElementById(id).innerHTML =
    "<tr>"+head.map(h=>`<th>${h}</th>`).join("")+"</tr>"+
    (rows.length? rows.join("") : `<tr><td colspan="${head.length}">— none —</td></tr>`);
}
fill("open", ["Strategy","Position","Opened","Entry \u2192 Current","Cost","Unrealized P/L","Resolves by"],
  (STATE.positions||[]).map(p=>`<tr>
    <td><span class="tag ${p.strategy}">${p.strategy}</span></td>
    <td>${p.title}<br><small>${p.note||""}</small></td>
    <td>${dt(p.opened)}</td>
    <td>${priceMove(p)}${p.guaranteed?" \ud83d\udd12":""}</td>
    <td>${money(p.cost)}</td>
    <td>${plCell(p)}</td>
    <td>${(p.resolve_by||"").slice(0,10)}</td></tr>`));
fill("closed", ["Strategy","Position","Closed","Cost","Payout","P/L"],
  (STATE.closed||[]).slice().reverse().map(p=>`<tr>
    <td><span class="tag ${p.strategy}">${p.strategy}</span></td>
    <td>${p.title}${p.close_reason==="take_profit"?" <small>[early exit ✂]</small>":""}</td>
    <td>${dt(p.closed_ts)}</td>
    <td>${money(p.cost)}</td><td>${money(p.payout)}</td>
    <td class="${p.pl>=0?'green':'red'}"><b>${money(p.pl)}</b></td></tr>`));
fill("opps", ["Strategy","Opportunity","Edge","Type","Note"],
  (OPPS||[]).map(o=>`<tr>
    <td><span class="tag ${o.strategy}">${o.strategy}</span></td>
    <td>${o.title}</td><td>${(o.edge*100).toFixed(2)}%</td>
    <td>${o.guaranteed?"LOCK 🔒":"probabilistic"}</td>
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
