/* RiskMesh adapter. The dashboard UI below was written for a case-based API. This layer presents the RiskMesh AI API
   (investigations, verified claims, approval-gated actions) in the shape that UI expects.
   Live mode: calls the backend at CFG.base with a bearer token. Static mode: reads the embedded SNAPSHOT (read-only demo). */
(()=>{
const realFetch=window.fetch.bind(window);
const SNAPSHOT=/*DATA_START*/null/*DATA_END*/;
const ss=(k,v)=>{try{if(v===undefined)return sessionStorage.getItem(k)||"";if(v===null)sessionStorage.removeItem(k);else sessionStorage.setItem(k,v)}catch(e){return""}return""};
const CFG={base:"",token:ss("rm_tok")};
(()=>{let q="";try{q=new URLSearchParams(location.search).get("api")||""}catch(e){}CFG.base=(window.RISKMESH_API||q||ss("rm_api")||"").replace(/\/+$/,"")})();
class HttpErr extends Error{constructor(s,m){super(m);this.status=s}}
const rs=(b,c=200)=>new Response(JSON.stringify(b),{status:c,headers:{"Content-Type":"application/json"}});
const isLive=()=>!!(CFG.base&&CFG.token);
const RM={who:null};
async function A(path,opt={}){
 let r;try{r=await realFetch(CFG.base+path,{...opt,headers:{"Authorization":"Bearer "+CFG.token,"Content-Type":"application/json"}})}
 catch(e){throw new HttpErr(0,"Could not reach the backend. Check the URL, that it is https, and that this page's address is in RISKMESH_CORS_ORIGINS.")}
 let j=null;try{j=await r.json()}catch(e){}
 if(!r.ok){const m=j&&(j.detail||j.error||j.message);throw new HttpErr(r.status,typeof m==="string"?m:m?JSON.stringify(m):"HTTP "+r.status)}
 return j}
const post=(p,b)=>A(p,{method:"POST",body:JSON.stringify(b||{})});

/* ---------- data layer ---------- */
const LOCAL={acts:null};
const dcache={};let DATA=null;
const invalidate=()=>{DATA=null};
async function load(){
 if(DATA)return DATA;
 if(isLive()){
  const [li,ac]=await Promise.all([A("/api/investigations"),A("/api/actions")]);
  const sums=li.investigations||[];
  const invs=await Promise.all(sums.map(async s=>dcache[s.id]||(dcache[s.id]=await A("/api/investigations/"+encodeURIComponent(s.id)))));
  DATA={invs,acts:ac.actions||[],eval:null};
  try{DATA.eval=await A("/api/eval")}catch(e){}
 }else{
  const S=SNAPSHOT||{investigations:[],eval:null};
  if(!LOCAL.acts)LOCAL.acts=S.investigations.flatMap(x=>(x.actions||[]).map(a=>({...a})));
  DATA={invs:S.investigations.map(x=>x.detail),acts:LOCAL.acts,eval:S.eval||null};
 }
 return DATA}

/* ---------- mapping: investigation -> case ---------- */
const figure=(C,f,s)=>C.find(c=>c.type==="FIGURE"&&c.status==="VERIFIED"&&c.assertion&&c.assertion.figure===f&&c.assertion.scope===s);
function mkGraph(inv){
 const F=(inv.report||{}).findings||[],nodes=new Map(),edges=[],B="beneficiary:"+inv.subject_id;
 const add=(id,type,label,x)=>{if(!nodes.has(id))nodes.set(id,{id,type,label,...x})};
 add(B,"beneficiary",inv.subject_id);
 F.forEach(f=>{const refs=f.refs||[];
  refs.forEach(x=>{const id=x.kind+":"+x.ref;add(id,x.kind,x.ref);
   const rel={capture:"funds",payout_item:"pays",dispute:"disputes"}[x.kind]||"refers";
   if(!edges.some(e=>e.source===id&&e.target===B))edges.push({source:id,target:B,rel})});
  if(f.rule_id==="shared_device"||f.rule_id==="shared_ip"){const dev=f.rule_id==="shared_device",gid=(dev?"device:":"ip:")+inv.id;
   add(gid,dev?"device":"ip",dev?"shared device":"shared IP",{shared:true});
   refs.forEach(x=>edges.push({source:x.kind+":"+x.ref,target:gid,rel:dev?"uses_device":"seen_on_ip"}))}});
 return{nodes:[...nodes.values()],edges}}
function mapCase(inv,acts){
 const r=inv.report||{},risk=r.risk||{},F=r.findings||[],C=inv.claims||[],tr=r.traceability||{},ai=r.ai_brief||{};
 const my=acts.filter(a=>a.investigation_id===inv.id),pend=my.filter(a=>a.status==="PENDING"),dec=my.filter(a=>a.status!=="PENDING");
 const fan=F.find(f=>f.rule_id==="fan_in_verified"),conc=F.find(f=>f.rule_id==="payout_concentration"),payers=figure(C,"distinct_payers","linked_captures"),total=figure(C,"sum_amount","payout_items");
 const members=[...new Set(((fan&&fan.refs)||[]).map(x=>x.ref))],fails=F.filter(f=>f.outcome==="FAIL");
 const nMem=payers?Number(payers.data.value):fan?Number(fan.value):(inv.bundle_counts||{}).captures||0;
 const ring={members,n_members:nMem,structural:risk.score!=null?risk.score:inv.risk_score,escalation:fails.length,top_beneficiary:inv.subject_id,
  exposure_inr:total?Number(total.data.value):0,concentration:conc&&conc.value!=null?Number(conc.value):0};
 const claimFor=rid=>C.find(c=>c.rule_id===rid);
 const ev=F.map(f=>{const c=claimFor(f.rule_id);return{id:c?c.id:f.rule_id,family:f.domain,statement:f.statement+(f.why?" ("+f.why+")":""),
  polarity:f.outcome==="FAIL"?"supports":f.outcome==="PASS"?"contradicts":"neutral",strength:+(f.weight/100).toFixed(2),
  signal:f.rule_id+" · "+f.outcome+(c&&c.status==="REJECTED"?" · claim rejected by reviewers":"")}});
 C.filter(c=>c.type==="FIGURE"&&c.status==="VERIFIED").forEach(c=>ev.push({id:c.id,family:"figure",statement:c.text,polarity:"neutral",strength:0,signal:null}));
 const rules=F.map(f=>{const c=claimFor(f.rule_id);return{rule_id:f.rule_id,decision:f.outcome,confidence:+(f.weight/100).toFixed(2),reason:f.statement+(f.why?". "+f.why:""),evidence:c?[c.id]:[]}});
 const missing=(risk.missing||[]).map(m=>{const f=F.find(x=>x.rule_id===m);return m+": "+((f&&(f.why||f.statement))||"check could not run (data not available)")});
 const rej=r.rejected_claims||[];
 const aiText=ai.available?(ai.brief||ai.summary||ai.text||(typeof ai==="string"?ai:"")):"";
 const fusion={risk_summary:aiText||r.summary||inv.summary,confidence:(ring.structural)+"/100",missing_information:missing.concat(ai.available?[]:["AI brief: "+(ai.reason||"not available")+". The decision does not depend on it."])};
 const critic={verdict:rej.length?"CLAIMS_REJECTED_AND_EXCLUDED":"ALL_CLAIMS_VERIFIED",recommended_confidence:tr.all_traced?"· every statement traced to a record":"· traceability incomplete",
  alternative_explanation:"Three reviewers (evidence auditor, numeric verifier, consistency reviewer) checked "+C.length+" claims. "+rej.length+" claim(s) were rejected after revision rounds and kept out of the report.",
  weaknesses:rej.map(x=>x.claim_id+": "+x.text+" — "+(((x.objections||[]).slice(-1)[0]||{}).text||"rejected"))};
 const rec=inv.recommendation;
 const recommendation={action:rec,scope:rec==="HOLD"?"unclaimed payout items of this recipient only; nothing runs until a reviewer approves each action":rec==="REVIEW"?"manual review; no payout action proposed":"no action; payouts are not touched",
  human_review_required:rec!=="RELEASE",rationale:inv.summary};
 let human=null;
 if(dec.length&&!pend.length){const rj=dec.filter(a=>a.status==="REJECTED");
  human={automated_decision:rec,human_decision:rj.length===dec.length?"REJECTED":rj.length?"PARTIAL ("+(dec.length-rj.length)+" approved, "+rj.length+" rejected)":"APPROVED",actor:dec[0].decided_by||"reviewer",override:rj.length>0,human_reason:dec[0].decision_reason||"",decision_version:1}}
 const status=pend.length?"PENDING_REVIEW":my.length?"RESOLVED":rec==="HOLD"?"ACTIONS_ALREADY_OPEN":rec==="REVIEW"?"NEEDS_REVIEW":"NO_ACTION_NEEDED";
 return{case_id:inv.id,title:"Payout recipient investigation",demo_scenario:rec,status,ring,evidence:ev,rule_results:rules,active_rule:"rules",
  established:C.filter(c=>c.status==="VERIFIED"&&(c.type==="FACT"||c.type==="FIGURE")).map(c=>({statement:c.text,evidence:[c.id]})),
  not_established:C.filter(c=>c.type==="ASSUMPTION").map(c=>c.text).concat(risk.confidence_limitations||[]),
  timeline:[],conversation_signals:null,fusion,critic,simulation:{},recommendation,human,ai_provider:{fusion:ai.available?(ai.provider||"AI model"):"deterministic rules"},
  acts:my.map(a=>({id:a.id,type:a.action_type,target:a.target_id,status:a.status,by:a.decided_by||"",mode:a.mode||""})),pending_n:pend.length,
  sim_note:isLive()?"Approval re-checks the evidence, then cancels the unclaimed payout item in the PayPal sandbox. A rejected action changes nothing.":"Static demo: approving only updates this page; nothing is sent to PayPal.",
  created_at:inv.created_at,subject_id:inv.subject_id,subject_type:inv.subject_type}}
const listRow=(c)=>({case_id:c.case_id,demo_scenario:c.demo_scenario,members:c.ring.n_members,structural_risk:c.ring.structural,escalation_signal:c.ring.escalation,top_beneficiary:c.ring.top_beneficiary,status:c.status});

/* ---------- audit, metrics, eval, chat ---------- */
async function auditFor(inv,acts){
 const ids=new Set([inv.id,...acts.filter(a=>a.investigation_id===inv.id).map(a=>a.id)]);
 if(isLive()){try{const j=await A("/api/audit");return(j.audit||[]).filter(a=>ids.has(a.target)).reverse().map(a=>({timestamp:a.ts,actor:a.actor,action:a.action,reason:(a.outcome||"")+(a.detail?" "+JSON.stringify(a.detail):"")}))}catch(e){return[{timestamp:inv.created_at,actor:"system",action:"audit",reason:e.status===403?"The audit log needs an admin token.":e.message}]}}
 const rows=[{timestamp:inv.created_at,actor:inv.created_by||"snapshot",action:"investigation.create",reason:inv.summary}];
 acts.filter(a=>a.investigation_id===inv.id&&a.decided_at).forEach(a=>rows.push({timestamp:a.decided_at,actor:a.decided_by,action:"action."+(a.status==="REJECTED"?"reject":"approve"),reason:a.id+" "+(a.decision_reason||"")}));
 return rows}
const pct=x=>Math.round(x*1000)/10+"%";
function evalItems(e){const s=e&&e.systems&&e.systems.riskmesh,n=e&&e.systems&&e.systems.naive_fan_in;
 if(!s)return[["Held-out evaluation","not available"]];
 return[["Held-out accuracy",pct(s.accuracy)+" ("+s.correct+"/"+s.n+")"],["Ring holds found",s.ring_holds_found+"/"+s.ring_holds_n],["Wrong holds",s.wrong_holds],["Dangerous releases",s.dangerous_releases],["Naive fan-in only",n?pct(n.accuracy):"–"],["Claims rejected by reviewers",e.integrity?e.integrity.claims_rejected_by_reviewers:"–"]]}
function metrics(D,cases){
 const decided=cases.filter(c=>c.human),agree=decided.filter(c=>!c.human.override),s=D.eval&&D.eval.systems&&D.eval.systems.riskmesh;
 const exp=cases.map(c=>({beneficiary:c.ring.top_beneficiary,exposure_inr:c.ring.exposure_inr})).sort((a,b)=>b.exposure_inr-a.exposure_inr).slice(0,5);
 return{active_cases:cases.length,high_risk_networks:D.invs.filter(i=>i.risk_level==="High").length,pending_reviews:cases.filter(c=>c.pending_n).length,pending_actions:cases.reduce((n,c)=>n+c.pending_n,0),
  ai_recommendation_agreement:decided.length?agree.length/decided.length:null,eval_items:evalItems(D.eval),clean_flag:s?s.clean_wrongly_flagged+"/"+s.clean_n:"–",
  beneficiary_concentration:exp,active_rule_version:"current",dataset:{transactions:D.invs.reduce((n,i)=>n+((i.bundle_counts||{}).captures||0),0)}}}
function chatAns(q,cid,cases,m){q=q.toLowerCase();const k=a=>a.some(x=>q.includes(x)),R=x=>"$"+Number(x).toFixed(2),n=q.replace(/ /g,"-"),hit=cases.find(c=>n.includes(c.case_id.toLowerCase()))||cases.find(c=>c.case_id===cid);
 if(hit&&!k(["pending","all cases","overview","agree"])){const g=hit.ring,r=hit.recommendation;return hit.case_id+": "+g.n_members+" linked payers, risk score "+g.structural+"/100, "+g.escalation+" indicator(s) fired. Payout value to "+g.top_beneficiary+" is "+R(g.exposure_inr)+" ("+(g.concentration*100).toFixed(0)+"% concentration). Status: "+hit.status.replace(/_/g," ")+". Decision: "+r.action+" (human review "+(r.human_review_required?"required":"not required")+"). "+(hit.not_established[0]?"Not established: "+hit.not_established[0]:"")}
 if(k(["pending","review"])){const p=cases.filter(c=>c.pending_n);return p.length+" investigation(s) await a reviewer: "+(p.map(c=>c.case_id+" ("+c.pending_n+" action(s))").join(", ")||"none")+"."}
 if(k(["agree","recommend"]))return m.ai_recommendation_agreement==null?"No reviewer decisions yet, so there is no agreement figure.":"Reviewers accepted the recommendation in "+Math.round(m.ai_recommendation_agreement*100)+"% of decided investigations.";
 if(k(["highest","riskiest","high-risk","high risk","ring"])){const t=[...cases].sort((a,b)=>b.ring.structural-a.ring.structural)[0];return t?m.high_risk_networks+" high-risk investigation(s). Highest risk score: "+t.case_id+" at "+t.ring.structural+"/100 ("+t.recommendation.action+").":"No investigations yet."}
 if(k(["beneficiar","exposure","concentration","payout"]))return m.beneficiary_concentration.length?"Payout value by recipient: "+m.beneficiary_concentration.map(x=>x.beneficiary+" "+R(x.exposure_inr)).join("; "):"No investigations yet.";
 if(k(["active","how many","summary","cases"]))return cases.length+" investigation(s), "+m.high_risk_networks+" high-risk, "+m.pending_reviews+" with actions awaiting review.";
 return"I can summarise investigations, pending reviews, reviewer agreement, payout exposure, or any investigation by its ID. I only report data; people approve every action."}

/* ---------- actions ---------- */
async function decide(actId,kind,reason){
 if(isLive()){const r=await post("/api/actions/"+encodeURIComponent(actId)+"/"+kind,{reason:reason||(kind==="approve"?"Evidence reviewed":"Rejected after review")});invalidate();return r}
 const a=(LOCAL.acts||[]).find(x=>x.id===actId);if(!a||a.status!=="PENDING")throw new HttpErr(409,"Action is not pending.");
 a.status=kind==="approve"?"APPROVED":"REJECTED";a.decided_by="demo-reviewer";a.decided_at=new Date().toISOString();a.decision_reason=reason||"";a.mode="static demo";invalidate();return{ok:true}}
async function bulk(id,kind,reason){const D=await load(),mine=D.acts.filter(a=>a.investigation_id===id&&a.status==="PENDING");
 if(!mine.length)throw new HttpErr(409,"No actions are awaiting a decision.");
 const errs=[];for(const a of mine){try{await decide(a.id,kind,reason)}catch(e){errs.push(a.id+": "+e.message)}}
 if(errs.length)throw new HttpErr(409,errs.join("\n"));return{ok:true}}

/* ---------- request router (what the UI believes is its API) ---------- */
async function dispatch(p,o){
 const M=(o.method||"GET").toUpperCase(),b=o.body?JSON.parse(o.body):{};let m;
 if(p==="/chat"){const D=await load(),cases=D.invs.map(i=>mapCase(i,D.acts));return rs({reply:chatAns(b.message||"",b.case_id,cases,metrics(D,cases))})}
 if(p==="/demo/seed"&&M==="POST"){if(!isLive())throw new HttpErr(409,"Connect a backend first.");const r=await post("/api/demo/seed");invalidate();return rs(r)}
 if(p==="/subjects"){if(isLive()){const j=await A("/api/subjects");return rs(j.subjects||[])}const D=await load();return rs(D.invs.map(i=>({subject_id:i.subject_id,display:i.subject_id,items:(i.bundle_counts||{}).payout_items})))}
 const D=await load(),cases=D.invs.map(i=>mapCase(i,D.acts));
 if(p==="/demo/scenarios"){const by=r=>(cases.find(c=>c.recommendation.action===r)||cases[0]||{}).case_id||"";return rs({ring:by("HOLD"),legit:by("RELEASE"),ambiguous:by("REVIEW")})}
 if(p==="/metrics")return rs(metrics(D,cases));
 if(p==="/evaluation/run"){const e=D.eval||{};return rs({dataset:e.dataset,systems:e.systems,confusion:e.confusion,integrity:e.integrity,injection:e.injection})}
 if(p==="/cases"){
  if(M==="POST"){if(!isLive())throw new HttpErr(409,"Connect a backend to create investigations.");const n=await post("/api/investigations",{subject_type:"beneficiary",subject_id:b.subject_id});invalidate();return rs({ok:true,id:n.id})}
  return rs(cases.map(listRow))}
 if(m=p.match(/^\/actions\/([^/]+)\/(approve|reject)$/)){await decide(decodeURIComponent(m[1]),m[2],b.reason);return rs({ok:true})}
 if(m=p.match(/^\/cases\/([^/]+)(?:\/(\w+))?$/)){
  const id=decodeURIComponent(m[1]),a=m[2],inv=D.invs.find(i=>i.id===id),c=cases.find(x=>x.case_id===id);
  if(!inv)return rs({detail:"investigation not found"},404);
  if(M==="GET"){if(!a)return rs(c);if(a==="graph")return rs(mkGraph(inv));if(a==="audit")return rs(await auditFor(inv,D.acts));if(a==="export")return rs({manifest:{investigation:inv.id,risk:inv.report&&inv.report.risk,summary:inv.summary,statements:(inv.report||{}).statements,traceability:(inv.report||{}).traceability,rejected_claims:(inv.report||{}).rejected_claims,findings:(inv.report||{}).findings}})}
  if(a==="investigate"){if(!isLive())throw new HttpErr(409,"Connect a backend to re-run an investigation.");const n=await post("/api/investigations",{subject_type:inv.subject_type,subject_id:inv.subject_id});invalidate();return rs({ok:true,id:n.id})}
  if(a==="approve"||a==="reject"){await bulk(id,a,b.reason);return rs({ok:true})}}
 return rs({detail:"not available"},404)}
window.fetch=async(p,o={})=>{
 if(typeof p!=="string"||p[0]!=="/")return realFetch(p,o);
 try{await ready;return await dispatch(p,o)}catch(e){return rs({detail:e.message||String(e)},e.status||500)}};

/* ---------- connection panel ---------- */
async function autodetect(){if(CFG.base)return;try{const r=await realFetch("/healthz",{cache:"no-store"});if(r.ok){const j=await r.json();if(j&&j.status==="ok")CFG.base=location.origin}}catch(e){}}
const esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
let open=false,msg="";
function panel(){let d=document.getElementById("rmconn");if(!d){d=document.createElement("div");d.id="rmconn";document.body.appendChild(d)}
 const live=isLive(),dot=live?"#13DEB9":"#FFAE1F",label=live?"Live API · "+esc(RM.who?RM.who.role:"connected"):"Static demo";
 d.innerHTML=`<div style="position:fixed;left:24px;bottom:24px;z-index:60;font-family:inherit">${open?`<div class="bg-white rounded-2xl border border-[#e5eaef] shadow-2xl p-5 mb-3" style="width:21rem;max-width:calc(100vw - 3rem)"><div class="font-bold mb-1">Backend connection</div><p class="text-xs text-[#5A6A85] mb-3">${live?"Connected to "+esc(CFG.base)+". Actions you approve run against the PayPal sandbox.":"This page shows a read-only snapshot. Connect your RiskMesh backend to use live data."}</p>
 <input id="rmu" placeholder="https://your-backend.onrender.com" value="${esc(CFG.base)}" class="w-full rounded-lg border border-[#e5eaef] bg-[#f6f9fc] px-3 py-2 text-sm mb-2 focus:outline-none focus:border-[#5D87FF]"><input id="rmt" type="password" placeholder="API token" autocomplete="off" value="${esc(CFG.token)}" class="w-full rounded-lg border border-[#e5eaef] bg-[#f6f9fc] px-3 py-2 text-sm mb-3 focus:outline-none focus:border-[#5D87FF]">
 ${msg?`<p class="text-xs mb-3" style="color:#d9573a">${esc(msg)}</p>`:""}<div class="flex gap-2"><button id="rmgo" class="inline-flex items-center justify-center rounded-lg bg-[#5D87FF] hover:bg-[#4570EA] text-white text-sm font-semibold px-4 py-2 cursor-pointer">Connect</button>${live?`<button id="rmoff" class="inline-flex items-center justify-center rounded-lg border border-[#5D87FF] text-[#5D87FF] bg-white text-sm font-semibold px-4 py-2 cursor-pointer">Disconnect</button>`:""}</div><p class="text-xs text-[#5A6A85] mt-3">The token stays in this browser tab only.</p></div>`:""}
 <button id="rmtog" class="inline-flex items-center gap-2 rounded-full bg-white border border-[#e5eaef] shadow-lg px-4 py-2 text-xs font-semibold text-[#2A3547] cursor-pointer"><i style="width:8px;height:8px;border-radius:9px;background:${dot};display:inline-block"></i>${label}</button></div>`;
 d.querySelector("#rmtog").onclick=()=>{open=!open;msg="";panel()};
 const go=d.querySelector("#rmgo");if(go)go.onclick=async()=>{const u=d.querySelector("#rmu").value.trim().replace(/\/+$/,""),t=d.querySelector("#rmt").value.trim();
  try{if(!/^https?:\/\//.test(u))throw new Error("Enter the backend URL, for example https://your-app.onrender.com");if(!t)throw new Error("Enter an API token.");
   CFG.base=u;CFG.token=t;RM.who=null;try{RM.who=await A("/api/me")}catch(e){CFG.token="";throw new Error(e.status===401?"The backend rejected that token.":e.message)}
   ss("rm_api",u);ss("rm_tok",t);invalidate();msg="";open=false;panel();if(window.route)window.route()}catch(e){msg=e.message;panel()}};
 const off=d.querySelector("#rmoff");if(off)off.onclick=()=>{CFG.base="";CFG.token="";RM.who=null;ss("rm_api",null);ss("rm_tok",null);invalidate();open=false;panel();if(window.route)window.route()}}
const ready=autodetect().then(async()=>{if(isLive()){try{RM.who=await A("/api/me")}catch(e){CFG.token="";ss("rm_tok",null)}}panel()});
window.RM=RM;
})();
