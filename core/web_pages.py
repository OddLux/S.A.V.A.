"""Inline HTML/CSS/JS for the LAN web remote (dp-267).

Kept as Python string constants on purpose: PyInstaller bundles imported
Python automatically, whereas a separate asset directory would need SAVA.spec
changes. Everything is self-contained -- no CDN, no external fonts -- so the
pages work on an offline LAN.
"""

_LANDING = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SAVA Remote</title>
<style>
body{margin:0;padding:24px 16px;background:#241a14;color:#f5ece4;
font-family:system-ui,Segoe UI,Arial,sans-serif;text-align:center}
h1{color:#ff7a2e;font-size:22px;letter-spacing:2px}
a{display:block;margin:14px auto;max-width:320px;padding:18px;background:#3a2a1e;
border:1px solid #b5531c;border-radius:8px;color:#ff7a2e;text-decoration:none;
font-size:18px}
</style></head><body><h1>SAVA</h1>__LINKS__</body></html>"""

_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>SAVA __TITLE__</title>
<style>
:root{--bg:#241a14;--mid:#2e2017;--light:#3a2a1e;--acc:#ff7a2e;--dim:#b5531c;
--txt:#f5ece4;--txd:#b89a86;--wave:#ff8a3d;--wbg:#1a120c}
*{box-sizing:border-box}
html,body{margin:0;height:100%}
body{background:var(--bg);color:var(--txt);font-family:system-ui,Segoe UI,Arial,sans-serif;
display:flex;flex-direction:column;max-width:900px;margin:0 auto;padding:8px 12px;gap:8px}
#top{display:flex;justify-content:space-between;align-items:center;color:var(--txd);font-size:12px}
#conn{width:10px;height:10px;border-radius:50%;background:#dc2f3c;display:inline-block}
#conn.ok{background:#3beb00}
#title{font-size:20px;font-weight:600;color:var(--acc);overflow:hidden;
text-overflow:ellipsis;white-space:nowrap}
#info{color:var(--txd);font-size:13px}
#tc{font-family:Consolas,Menlo,monospace;font-size:34px;text-align:center;
background:var(--wbg);border:1px solid var(--light);border-radius:6px;padding:6px}
#tc small{display:block;font-size:15px;color:var(--txd)}
#wave{width:100%;height:110px;background:var(--wbg);border:1px solid var(--light);
border-radius:6px;touch-action:none;display:block}
.ctl{display:flex;gap:8px}
.ctl button{flex:1;min-height:52px;font-size:22px;background:var(--light);color:var(--txt);
border:1px solid var(--dim);border-radius:8px}
.ctl button:active{background:var(--dim)}
#vol{display:flex;align-items:center;gap:10px;font-size:14px;color:var(--txd)}
#vol input{flex:1;height:36px;accent-color:var(--acc)}
#list{flex:1;min-height:120px;overflow:auto;border:1px solid var(--light);
border-radius:6px;background:var(--mid)}
.row{display:flex;gap:8px;padding:12px 10px;border-bottom:1px solid var(--bg);font-size:15px}
.row .n{width:28px;color:var(--txd);text-align:right}
.row .t{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.row .d{color:var(--txd)}
.row.cur{background:var(--dim);color:#fff}
body.ctrl .row{cursor:pointer}
.only-ctrl{display:none}
body.ctrl .only-ctrl{display:flex}
</style></head>
<body class="__BODYCLASS__">
<div id="top"><span>SAVA __TITLE__</span><span><span id="st">stopped</span> <span id="conn"></span></span></div>
<div id="title">-</div>
<div id="info"></div>
<div id="tc">0:00 / 0:00<small>-0:00</small></div>
<canvas id="wave"></canvas>
<div class="ctl only-ctrl">
<button data-c="prev" aria-label="Previous">&#9198;</button>
<button data-c="play" aria-label="Play">&#9654;</button>
<button data-c="pause" aria-label="Pause">&#10074;&#10074;</button>
<button data-c="stop" aria-label="Stop">&#9632;</button>
<button data-c="next" aria-label="Next">&#9197;</button>
</div>
<div id="vol" class="only-ctrl"><span>Vol</span><input id="volr" type="range" min="0" max="100" value="80"><span id="volv">80</span></div>
<div id="list"></div>
<script>
(function(){
var CTRL=__CTRL__;
var st={state:"stopped",pos:0,dur:0,volume:0,index:-1,title:"",info:""};
var recv=performance.now(),wf=[],pl=[],wfRev=-1,plRev=-1,drag=null,volDrag=false;
function $(i){return document.getElementById(i)}
function fmt(s){s=Math.max(0,s);return Math.floor(s/60)+":"+("0"+Math.floor(s%60)).slice(-2)}
function curPos(){
 if(drag!==null)return drag;
 var p=st.pos;
 if(st.state==="playing")p+=(performance.now()-recv)/1000;
 return st.dur>0?Math.min(p,st.dur):p;
}
function getJ(u,cb){fetch(u).then(function(r){return r.json()}).then(cb).catch(function(){})}
function cmd(c,a){
 fetch("/api/command",{method:"POST",headers:{"Content-Type":"application/json"},
 body:JSON.stringify({cmd:c,arg:a})}).catch(function(){});
}
function renderList(){
 var h="";
 for(var i=0;i<pl.length;i++){
  var t=pl[i];
  h+='<div class="row'+(i===st.index?" cur":"")+'" data-i="'+i+'"><span class="n">'+(i+1)+
  '</span><span class="t"></span><span class="d">'+(t.d>0?fmt(t.d):"")+'</span></div>';
 }
 var el=$("list");el.innerHTML=h;
 var rows=el.children;
 for(var j=0;j<rows.length;j++)rows[j].children[1].textContent=pl[j].t;
 markCur();
}
var lastIdx=-2;
function markCur(){
 var rows=$("list").children;
 for(var i=0;i<rows.length;i++)rows[i].className="row"+(i===st.index?" cur":"");
 if(st.index!==lastIdx&&rows[st.index]){
  lastIdx=st.index;
  if(rows[st.index].scrollIntoView)rows[st.index].scrollIntoView({block:"nearest"});
 }
}
function onState(s){
 st=s;recv=performance.now();
 $("st").textContent=s.state;
 $("title").textContent=s.title||"-";
 $("info").textContent=s.info||"";
 if(s.wf_rev!==wfRev){wfRev=s.wf_rev;getJ("/api/waveform",function(w){wf=w})}
 if(s.pl_rev!==plRev){plRev=s.pl_rev;getJ("/api/playlist",function(p){pl=p;renderList()})}
 else markCur();
 if(!volDrag){$("volr").value=s.volume;$("volv").textContent=s.volume}
}
function connect(){
 var es=new EventSource("/api/events");
 es.onopen=function(){$("conn").className="ok"};
 es.onmessage=function(e){$("conn").className="ok";onState(JSON.parse(e.data))};
 es.onerror=function(){$("conn").className=""};
}
var cv=$("wave"),cx=cv.getContext("2d");
function draw(){
 var dpr=window.devicePixelRatio||1,w=cv.clientWidth,h=cv.clientHeight;
 if(cv.width!==Math.round(w*dpr)||cv.height!==Math.round(h*dpr)){cv.width=Math.round(w*dpr);cv.height=Math.round(h*dpr)}
 cx.setTransform(dpr,0,0,dpr,0,0);
 cx.clearRect(0,0,w,h);
 var cs=getComputedStyle(document.documentElement);
 cx.fillStyle=cs.getPropertyValue("--wave");
 var n=wf.length;
 if(n){var bw=w/n;for(var i=0;i<n;i++){var bh=Math.max(1,wf[i]/100*(h-6));
  cx.fillRect(i*bw,(h-bh)/2,Math.max(1,bw-0.5),bh)}}
 var p=curPos();
 if(st.dur>0){
  var x=p/st.dur*w;
  cx.fillStyle="rgba(0,0,0,0.35)";cx.fillRect(0,0,x,h);
  cx.fillStyle="#fff";cx.fillRect(Math.min(w-2,x),0,2,h);
 }
 var rem=Math.max(0,st.dur-p);
 var tc=$("tc");
 tc.firstChild.nodeValue=fmt(p)+" / "+fmt(st.dur);
 tc.lastChild.textContent=rem>=1?"-"+fmt(rem):"0:00";
 requestAnimationFrame(draw);
}
function seekFromEvent(e){
 var r=cv.getBoundingClientRect();
 var f=Math.max(0,Math.min(1,(e.clientX-r.left)/r.width));
 return f*st.dur;
}
if(CTRL){
 cv.addEventListener("pointerdown",function(e){
  if(st.dur<=0)return;
  cv.setPointerCapture(e.pointerId);drag=seekFromEvent(e);
 });
 cv.addEventListener("pointermove",function(e){if(drag!==null)drag=seekFromEvent(e)});
 cv.addEventListener("pointerup",function(e){
  if(drag===null)return;
  var t=seekFromEvent(e);drag=null;
  st.pos=t;recv=performance.now();cmd("seek",t);
 });
 cv.addEventListener("pointercancel",function(){drag=null});
 var bs=document.querySelectorAll(".ctl button");
 for(var i=0;i<bs.length;i++)bs[i].addEventListener("click",function(){cmd(this.getAttribute("data-c"))});
 var vr=$("volr"),vt=0;
 vr.addEventListener("input",function(){
  volDrag=true;$("volv").textContent=vr.value;
  var now=Date.now();
  if(now-vt>80){vt=now;cmd("volume",parseInt(vr.value,10))}
 });
 vr.addEventListener("change",function(){cmd("volume",parseInt(vr.value,10));volDrag=false});
 $("list").addEventListener("click",function(e){
  var el=e.target;
  while(el&&el!==this&&!el.hasAttribute("data-i"))el=el.parentNode;
  if(el&&el.hasAttribute&&el.hasAttribute("data-i"))cmd("play_index",parseInt(el.getAttribute("data-i"),10));
 });
}
connect();requestAnimationFrame(draw);
})();
</script></body></html>"""


def render_page(control):
    """Return the viewer page, or the control page when `control` is true."""
    return (
        _PAGE.replace("__TITLE__", "Control" if control else "Viewer")
        .replace("__BODYCLASS__", "ctrl" if control else "")
        .replace("__CTRL__", "true" if control else "false")
    )


def render_landing(viewer, control):
    """Return the landing page linking whichever interfaces are enabled."""
    links = []
    if viewer:
        links.append('<a href="/view">Viewer</a>')
    if control:
        links.append('<a href="/control">Control</a>')
    return _LANDING.replace("__LINKS__", "".join(links))
