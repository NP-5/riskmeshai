"""Assemble web/index.html from the original dashboard UI (web/legacy/) plus the RiskMesh adapter.

    python web/build_ui.py        # writes web/index.html (keeps any embedded snapshot already in it)
    python build_demo.py          # then refresh the embedded snapshot and copy to ./index.html

legacy/head.html  original page head, fonts, Tailwind admin styling, splash screen
legacy/ui.js      original UI script (landing, dashboard with ring gauges, case view, chat bot)
legacy/adapter.js maps the RiskMesh AI API (investigations/claims/actions) to what ui.js expects
The patches below change only wording and panels that described the old case-based workflow.
"""
import re
from pathlib import Path

W = Path(__file__).resolve().parent
ui = (W / "legacy" / "ui.js").read_text()
miss = []


def rep(old, new, count=1):
    global ui
    if old not in ui:
        miss.append(old[:70])
        return
    ui = ui.replace(old, new, count)


def cut(start, end, new):
    """Replace the slice from `start` through the first `end` after it."""
    global ui
    i = ui.find(start)
    if i < 0:
        miss.append(start[:70]); return
    j = ui.find(end, i)
    if j < 0:
        miss.append(end[:70]); return
    ui = ui[:i] + new + ui[j + len(end):]


# ---- currency, landing copy ----
rep('const inr=n=>"₹"+Math.round(n).toLocaleString("en-IN");',
    'const inr=n=>"$"+Number(n||0).toLocaleString("en-US",{minimumFractionDigits:2,maximumFractionDigits:2});')
m = re.search(r"const FEATS=\[.*?\];\s*const STEPS=\[.*?\];", ui, re.S)
if m:
    ui = ui[:m.start()] + '''const FEATS=[["Evidence-bound rules","Every indicator is a computed rule result tied to real PayPal records. A FAIL is an indicator, never a verdict."],["Multi-agent investigation","Network, payout, dispute and transaction specialists add claims; nothing is stated without a source record."],["Independent reviewers","An evidence auditor, numeric verifier and consistency reviewer reject unsupported claims before they reach the report."],["Deterministic decision","HOLD, REVIEW or RELEASE is computed from verified rule results only, never from model text."],["Human approval","A reviewer approves each proposed hold; approval re-checks the evidence before the sandbox payout item is cancelled."],["Audit and integrity","Append-only audit trail, idempotent actions and signature-verified webhooks."]];
const STEPS=["Detect","Investigate","Verify","Propose","Approve"];''' + ui[m.end():]
else:
    miss.append("FEATS/STEPS")
for k in ("ring", "legit", "ambiguous"):
    rep('href="#/case/${sc.%s}"' % k, 'href="${sc.%s?"#/case/"+sc.%s:"#/dashboard"}"' % (k, k), 2 if k == "ring" else 1)
rep(">Suspicious Payment Ring<", ">Suspicious payout ring<")
rep(">Legitimate Merchant Network<", ">Clean recipient<")
rep(">Ambiguous Case<", ">Needs review<")
rep('${kpi("AI agreement",m.ai_recommendation_agreement??"–","#13DEB9")}',
    '${kpi("Reviewer agreement",m.ai_recommendation_agreement==null?"–":Math.round(m.ai_recommendation_agreement*100)+"%","#13DEB9")}')

# ---- dashboard ----
rep('["Case","Scenario","Members","Structural","Escalation","Top beneficiary","Status",""]',
    '["Investigation","Decision","Payers","Risk score","Indicators","Recipient","Status",""]')
rep('${kpi("FPR (active rule, backtest)",m.false_positive_rate_active_rule_backtest,"#13DEB9")}${kpi("Avg investigation (s)",m.avg_investigation_seconds??"–")}',
    '${kpi("Clean cases flagged (eval)",m.clean_flag,"#13DEB9")}${kpi("Actions awaiting review",m.pending_actions,"#FFAE1F")}')
rep('Top beneficiary concentration', 'Payout value by recipient')
rep('Rules ${m.active_rule_version} · ${m.dataset.transactions} transactions · ${m.dataset.events_superseded} superseded revisions',
    'Rule set ${m.active_rule_version} · ${m.dataset.transactions} captures analysed')
ui, n = re.subn(r'Baseline evaluation</div><div class="grid grid-cols-2 gap-3 text-sm">\$\{\[\[.*?\]\]\.map\(',
                'Held-out evaluation (synthetic)</div><div class="grid grid-cols-2 gap-3 text-sm">${m.eval_items.map(', ui, count=1, flags=re.S)
if not n:
    miss.append("Baseline evaluation")
rep('<div class="${H}">AI tools</div><button class="${B1} mr-2" onclick="runEval()">Run AI evaluation</button><button class="${B2}" onclick="rules()">Rule re-engineering</button><div id="extra" class="mt-4"></div>',
    '<div class="${H}">Investigations</div><div class="flex flex-wrap gap-2 items-center"><select id="subj" class="rounded-lg border border-[#e5eaef] bg-[#f6f9fc] px-3 py-2.5 text-sm"><option>Loading recipients…</option></select><button class="${B1}" onclick="newInv()">Investigate</button><button class="${B2}" onclick="runEval()">Show held-out evaluation</button></div><div id="extra" class="mt-4"></div>')
rep('strokeDasharray=c.dataset.d),80)}', 'strokeDasharray=c.dataset.d),80);loadSubj()}')
rep('async function runEval(){', '''async function loadSubj(){const s=$("#subj");if(!s)return;try{const l=await api("/subjects");s.innerHTML=l.length?l.map(x=>`<option value="${esc(x.subject_id)}">${esc(x.display||x.subject_id)} · ${x.items??"?"} payout items</option>`).join(""):`<option value="">No recipients yet</option>`}catch(e){s.innerHTML=`<option value="">${esc(e.message)}</option>`}}
async function newInv(){const v=$("#subj")&&$("#subj").value;if(!v){alert("Pick a recipient first.");return}try{const r=await post("/cases",{subject_id:v});location.hash="#/case/"+r.id}catch(e){alert(e.message)}}
async function actDec(a,k){try{await post("/actions/"+a+"/"+k,{reason:($("#why")||{}).value||""});route()}catch(e){alert(e.message)}}
async function runEval(){''')

# ---- case view ----
rep('${v.case_id} · Candidate Risk Ring', '${v.case_id} · ${esc(v.title)}')
rep('${r.n_members} customers</span>', '${r.n_members} linked payers</span>')
rep('${kpi("Structural risk",r.structural+"/100","#FA896B")}', '${kpi("Risk score",r.structural+"/100","#FA896B")}')
rep('${kpi("6-hour escalation signal",r.escalation+"/100","#FFAE1F")}', '${kpi("Indicators fired",r.escalation+" of "+v.rule_results.length+" rules","#FFAE1F")}')
rep('${kpi("7-day value to "+esc(r.top_beneficiary),inr(r.exposure_inr))}', '${kpi("Payout value to "+esc(r.top_beneficiary),inr(r.exposure_inr))}')
rep('${kpi("Beneficiary concentration",', '${kpi("Payout concentration",')
rep('Members: ${r.members.join(", ")}', 'Linked captures: ${r.members.join(", ")||"–"}')
rep('const cols=["customer","account","device","ip","instrument","transaction","beneficiary","merchant"]',
    'const cols=["capture","customer","account","device","ip","instrument","payout_item","transaction","dispute","beneficiary","merchant"]')
rep('{customer:"#5D87FF",device:"#FFAE1F",ip:"#7a5af8",instrument:"#13DEB9",beneficiary:"#FA896B",transaction:"#A9B6CC",account:"#5A6A85"}',
    '{customer:"#5D87FF",capture:"#5D87FF",device:"#FFAE1F",ip:"#7a5af8",instrument:"#13DEB9",payout_item:"#13DEB9",dispute:"#FFAE1F",beneficiary:"#FA896B",transaction:"#A9B6CC",account:"#5A6A85"}')
rep('fill="#2A3547">${t}</text>', 'fill="#2A3547">${t.replace("_"," ")}</text>')
cut('<div class="${CARD}"><div class="${H}">Transaction timeline (last 72h)</div>', '</pre></div>', '')
rep('<button class="${B1}" onclick="step(\'${id}\',\'investigate\')">1 · Conversation + Fusion</button><button class="${B1}" onclick="step(\'${id}\',\'critic\')">2 · Critic Agent</button><button class="${B1}" onclick="step(\'${id}\',\'simulate\')">3 · Simulate interventions</button>',
    '<button class="${B1}" onclick="step(\'${id}\',\'investigate\')">Re-run investigation</button>')
rep('Fusion analysis · confidence ${f.confidence}', 'Investigation summary · risk ${f.confidence}')
cut('${s?`<div class="${CARD}"><div class="${H}">Intervention simulator</div>', '"optional"}.</p></div>`:""}', r'''<div class="${CARD} border-l-4 border-l-[#5D87FF]"><div class="${H}">Recommended action</div><p class="text-sm"><b>${v.recommendation.action}</b> — ${esc(v.recommendation.scope)}. Human review ${v.recommendation.human_review_required?"required":"not required"}.</p></div>
 ${v.acts.length?`<div class="${CARD}"><div class="${H}">Proposed actions · human approval required</div><div class="overflow-x-auto"><table class="w-full text-sm">${TH(["Action","Target","Status","Decided by",""])}${v.acts.map(a=>`<tr><td class="${TD}">${esc(a.type)}</td><td class="${TD} font-bold">${esc(a.target)}</td><td class="${TD}">${pill(a.status,a.status==="REJECTED"?"supports":a.status==="PENDING"?"warn":"contradicts")}</td><td class="${TD} text-[#5A6A85]">${esc(a.by)}${a.mode?" · "+esc(a.mode):""}</td><td class="${TD}">${a.status==="PENDING"?`<button class="${B1} !px-3 !py-1.5" onclick="actDec('${a.id}','approve')">Approve</button> <button class="${B3} !px-3 !py-1.5" onclick="actDec('${a.id}','reject')">Reject</button>`:""}</td></tr>`).join("")}</table></div><p class="text-xs text-[#5A6A85] mt-3">${esc(v.sim_note)}</p></div>`:""}''')
rep('(s?`<input id="why"', '(v.pending_n?`<input id="why"')
rep('placeholder="Reason (required to override)"', 'placeholder="Reason (recorded in the audit log)"')
rep('>Approve recommendation</button>', '>Approve all pending actions</button>')
rep('>Reject → monitor / no action</button>', '>Reject all pending actions</button>')
rep('Run investigation, critic and simulation first.', 'Nothing is awaiting a decision for this investigation.')
cut('<div class="mt-5 pt-5 border-t border-[#e5eaef]"><input id="note"', 'Add note</button></div>', '')
rep('async function step(id,s){try{await post(`/cases/${id}/${s}`);show(id)}catch(e){alert(e.message)}}',
    'async function step(id,s){try{const r=await post(`/cases/${id}/${s}`);if(r&&r.id&&r.id!==id)location.hash="#/case/"+r.id;else show(id)}catch(e){alert(e.message)}}')
# CSV labels
rep('row(["Customers",r.n_members])', 'row(["Linked payers",r.n_members])')
rep('row(["Structural risk (out of 100)",r.structural])', 'row(["Risk score (out of 100)",r.structural])')
rep('row(["6-hour escalation signal (out of 100)",r.escalation])', 'row(["Indicators fired",r.escalation])')
rep('row(["Top beneficiary",r.top_beneficiary])', 'row(["Recipient",r.top_beneficiary])')
rep('row(["7-day exposure (INR)",Math.round(r.exposure_inr)])', 'row(["Payout value (USD)",r.exposure_inr])')

if miss:
    raise SystemExit("patch anchors not found:\n  " + "\n  ".join(miss))

old = (W / "index.html")
snap = None
if old.exists():
    mm = re.search(r"/\*DATA_START\*/.*?/\*DATA_END\*/", old.read_text(), re.S)
    snap = mm.group(0) if mm else None
adapter = (W / "legacy" / "adapter.js").read_text()
if snap:
    adapter = re.sub(r"/\*DATA_START\*/.*?/\*DATA_END\*/", lambda _: snap, adapter, flags=re.S)
head = (W / "legacy" / "head.html").read_text()
out = head + "<script>\n" + adapter + "\n</script>\n<script>\n" + ui + "</script></body></html>\n"
(W / "index.html").write_text(out)
print("wrote web/index.html", len(out) // 1024, "KB", "(snapshot kept)" if snap else "(no snapshot yet: run build_demo.py)")
