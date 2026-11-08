import io
import json
import urllib.error
import urllib.request
from asgi_client import request
from helpers import ADMIN, ANALYST, REVIEWER, VIEWER, H, make_app, raises, settings
from riskmesh import config as cfg
from riskmesh import llm
from riskmesh.agents.specialists import pseudonymise

KEY = "AIzaSy-TEST-SECRET-0123456789"


class FakeGemini:
    """Replaces urllib.request.urlopen with Gemini's generateContent response shape."""

    def __init__(self, text='{"ok": true}', fail=None, raw=None):
        self.requests, self.text, self.fail, self.raw = [], text, fail, raw
        self.orig = urllib.request.urlopen

    def __enter__(self):
        urllib.request.urlopen = self
        return self

    def __exit__(self, *a):
        urllib.request.urlopen = self.orig

    def __call__(self, req, timeout=None):
        self.requests.append({"url": req.full_url, "headers": {k.lower(): v for k, v in req.header_items()}, "body": json.loads(req.data)})
        if self.fail:
            raise urllib.error.HTTPError(req.full_url, self.fail[0], "err", {}, io.BytesIO(json.dumps({"error": {"code": self.fail[0], "message": self.fail[1]}}).encode()))
        if self.raw is not None:
            return io.BytesIO(json.dumps(self.raw).encode())
        t = self.text(self.requests[-1]["body"]) if callable(self.text) else self.text
        return io.BytesIO(json.dumps({"candidates": [{"content": {"parts": [{"text": "hidden reasoning", "thought": True}, {"text": t}]}, "finishReason": "STOP"}],
                                      "usageMetadata": {"promptTokenCount": 21, "candidatesTokenCount": 5, "thoughtsTokenCount": 3}}).encode())


def gem(**kw):
    return settings(RISKMESH_LLM_PROVIDER="gemini", GEMINI_API_KEY=KEY, **kw)


def test_gemini_settings_defaults_and_key_sources():
    s = gem()
    assert s.llm_provider == "gemini" and s.llm_key == KEY and s.llm_model == "gemini-2.5-flash" and not cfg.validate(s)[0]
    assert cfg.load_settings({"GEMINI_API_KEY": KEY}).llm_provider == "gemini"                                  # key alone selects Gemini
    assert cfg.load_settings({"GEMINI_API_KEY": KEY, "RISKMESH_LLM_MODEL": "gemini-x"}).llm_model == "gemini-x"
    assert any("Gemini API key" in e for e in cfg.validate(cfg.load_settings({"RISKMESH_LLM_PROVIDER": "gemini"}))[0])
    assert any("model name" in e for e in cfg.validate(cfg.load_settings({"RISKMESH_LLM_PROVIDER": "gemini", "GEMINI_API_KEY": KEY, "RISKMESH_LLM_MODEL": "../x?y=1"}))[0])
    assert KEY not in str(s.public_view())


def test_gemini_request_shape_key_in_header_only_and_usage():
    p = llm.get_provider(gem())
    with FakeGemini() as f:
        out = p.complete("be strict", "hello")
    r = f.requests[0]
    assert out == '{"ok": true}'                                           # the thought part is ignored
    assert r["url"] == "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent" and KEY not in r["url"]
    assert r["headers"]["x-goog-api-key"] == KEY
    assert r["body"]["systemInstruction"] == {"parts": [{"text": "be strict"}]} and r["body"]["contents"] == [{"role": "user", "parts": [{"text": "hello"}]}]
    assert r["body"]["generationConfig"] == {"temperature": 0, "responseMimeType": "application/json"}
    assert (p.calls, p.tokens_in, p.tokens_out) == (1, 21, 8) and p.name == "gemini"


def test_gemini_failures_are_clean_and_never_leak_the_key():
    p = lambda: llm.get_provider(gem())
    with FakeGemini(fail=(400, "API key not valid. Please pass a valid API key.")):
        e = raises(lambda: p().complete("s", "u"), llm.LLMError, "400")
    assert KEY not in str(e)
    with FakeGemini(fail=(429, "Resource has been exhausted")):
        raises(lambda: p().complete("s", "u"), llm.LLMError, "429")
    with FakeGemini(raw={"promptFeedback": {"blockReason": "SAFETY"}}):
        raises(lambda: p().complete("s", "u"), llm.LLMError, "SAFETY")
    with FakeGemini(raw={"candidates": [{"finishReason": "MAX_TOKENS"}]}):
        raises(lambda: p().complete("s", "u"), llm.LLMError, "MAX_TOKENS")
    with FakeGemini(raw={"candidates": [{"content": {"parts": [{"text": "  "}]}}]}):
        raises(lambda: p().complete("s", "u"), llm.LLMError, "empty")


def test_end_to_end_gemini_brief_and_inferences_cannot_move_the_decision():
    from riskmesh import demo_data
    app, s = make_app(RISKMESH_LLM_PROVIDER="gemini", GEMINI_API_KEY=KEY)
    ids = demo_data.seed(app.db, s.pepper)
    state = {"cid": None}

    def answer(body):
        sysmsg = body["systemInstruction"]["parts"][0]["text"]
        if "case brief" in sysmsg:
            c = state["cid"]
            return json.dumps({"sentences": [{"text": t, "claim_ids": [c]} for t in ("The rules computed a HOLD decision for this beneficiary.", "This claim was verified against the records.", "No payout action has been taken yet.", "The model asserts 999 fraudulent payers.")]})
        return json.dumps({"inferences": [{"text": "Mule network confirmed.", "depends_on": [], "evidence": [{"kind": "capture", "ref": "CAP-NOPE"}]}]})
    state["cid"] = "CLM-001"
    with FakeGemini(text=answer) as f:
        r = request(app, "POST", "/api/investigations", {"subject_type": "beneficiary", "subject_id": ids["ring"]}, H(ANALYST))
    assert r.status == 200
    j = r.json()
    b = j["report"]["ai_brief"]
    assert j["recommendation"] == "HOLD" and b["available"] and b["provider"] == "gemini" and b["model"] == "gemini-2.5-flash"
    assert len(b["sentences"]) == 3 and any("999" in d["text"] for d in b["dropped"])            # invented number was dropped
    assert any(c["type"] == "INFERENCE" and c["status"] == "REJECTED" for c in j["claims"])          # fabricated evidence was rejected
    assert all(KEY not in json.dumps(q["body"]) for q in f.requests) and all(q["headers"]["x-goog-api-key"] == KEY for q in f.requests)
    # identifiers in the prompts were tokenised
    sent = json.dumps([q["body"] for q in f.requests])
    assert "dev-A" not in sent and "198.51.100.7" not in sent and "PAYER-R1" not in sent
    assert j["trace"]["usage"]["llm_calls"] if "trace" in j else True


def test_gemini_outage_does_not_block_an_investigation():
    from riskmesh import demo_data
    app, s = make_app(RISKMESH_LLM_PROVIDER="gemini", GEMINI_API_KEY=KEY)
    ids = demo_data.seed(app.db, s.pepper)
    with FakeGemini(fail=(503, "The model is overloaded")):
        r = request(app, "POST", "/api/investigations", {"subject_type": "beneficiary", "subject_id": ids["ring"]}, H(ANALYST))
    assert r.status == 200 and r.json()["recommendation"] == "HOLD" and r.json()["report"]["ai_brief"]["available"] is False


def test_ai_test_endpoint_with_gemini():
    app, _ = make_app(RISKMESH_LLM_PROVIDER="gemini", GEMINI_API_KEY=KEY)
    with FakeGemini():
        r = request(app, "POST", "/api/ai/test", {}, H(ADMIN))
    j = r.json()
    assert r.status == 200 and j["provider"] == "gemini" and j["model"] == "gemini-2.5-flash" and KEY not in r.body.decode()
    with FakeGemini(fail=(400, "API key not valid")):
        r = request(app, "POST", "/api/ai/test", {}, H(ADMIN))
    assert r.status == 502 and KEY not in r.body.decode() and "400" in r.json()["error"]
    assert request(app, "POST", "/api/ai/test", {}, H(REVIEWER)).status == 403
    cfgv = request(app, "GET", "/api/config", headers=H(VIEWER)).body.decode()
    assert KEY not in cfgv and '"llm_provider": "gemini"' in cfgv
