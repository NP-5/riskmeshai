"""Build a static, read-only snapshot of real engine output and inline it into web/index.html (and index.html for a static Space).

    python eval/generate_cases.py && python eval/calibrate.py && python eval/run_eval.py && python build_demo.py

The snapshot comes from the same code the API runs, over synthetic demo data. It contains no secrets and no real PayPal data.
"""
import json
import re
from pathlib import Path

from riskmesh import demo_data
from riskmesh.config import load_settings
from riskmesh.db import Database
from riskmesh.paypal import PayPalClient
from riskmesh.service import Service

ROOT = Path(__file__).resolve().parent
s = load_settings({"RISKMESH_ENV": "development", "RISKMESH_DEMO_MODE": "1", "DATABASE_URL": "sqlite:///:memory:", "RISKMESH_PEPPER": "snapshot-pepper-not-secret"})
db = Database(s.db_url)
svc = Service(s, db, PayPalClient(s))
ids = demo_data.seed(db, s.pepper)
out = []
for name in ("ring", "nosession", "clean"):
    d = svc.investigate("beneficiary", ids[name], "snapshot", "admin", "static snapshot")
    summary = {k: d[k] for k in ("id", "subject_type", "subject_id", "status", "created_by", "created_at", "risk_level", "risk_score", "recommendation", "summary", "ring")}
    out.append({"id": d["id"], "summary": summary, "detail": d, "actions": []})
all_actions = svc.list_actions()
for o in out:
    o["actions"] = [a for a in all_actions if a["investigation_id"] == o["id"]]
ev = json.loads((ROOT / "eval" / "results.json").read_text()) if (ROOT / "eval" / "results.json").exists() else None
blob = json.dumps({"investigations": out, "eval": ev}, separators=(",", ":")).replace("</", "<\\/")
html = (ROOT / "web" / "index.html").read_text()
html = re.sub(r"/\*DATA_START\*/.*?/\*DATA_END\*/", lambda m: "/*DATA_START*/" + blob + "/*DATA_END*/", html, flags=re.S)
(ROOT / "web" / "index.html").write_text(html)
(ROOT / "index.html").write_text(html)
print("snapshot:", len(out), "investigations,", len(blob) // 1024, "KB")
