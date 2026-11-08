"""Configurable LLM providers (environment variables, no SDK dependency).

    RISKMESH_LLM_PROVIDER = none | openai | anthropic      (openai = any OpenAI-compatible /chat/completions endpoint)
    RISKMESH_LLM_URL / RISKMESH_LLM_KEY / RISKMESH_LLM_MODEL
    RISKMESH_LLM_PRICE_IN_PER_MTOK / RISKMESH_LLM_PRICE_OUT_PER_MTOK   optional, for cost reporting

A model never decides anything here: it may only propose INFERENCE claims, which the reviewers must verify, and its text is
treated as untrusted data. With provider ``none`` the system runs fully deterministically.
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request

from .config import Settings


def extract_json(text: str):
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    a, b = text.find("{"), text.rfind("}")
    if a < 0 or b <= a:
        raise ValueError("no JSON object in model output")
    return json.loads(text[a:b + 1])


class LLMError(Exception):
    pass


class NoLLM:
    name, model = "none", ""

    def __init__(self):
        self.tokens_in = self.tokens_out = self.calls = 0

    @property
    def enabled(self):
        return False

    def complete(self, system: str, user: str) -> str:
        raise LLMError("no LLM configured")

    def cost_usd(self):
        return None


class _HTTP(NoLLM):
    def __init__(self, s: Settings, timeout=45):
        super().__init__()
        self.s, self.model, self.timeout = s, s.llm_model, timeout

    @property
    def enabled(self):
        return True

    def _post(self, url, body, headers):
        req = urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": "application/json", **headers}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:                  # keep the status and the provider's error text; never the request (it carries the key)
            try:
                detail = str((json.load(e).get("error") or {}).get("message", ""))[:200]
            except Exception:
                detail = ""
            raise LLMError(f"HTTP {e.code}: {detail}".strip()) from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
            raise LLMError(type(e).__name__) from e

    def cost_usd(self):
        try:
            pi, po = float(os.getenv("RISKMESH_LLM_PRICE_IN_PER_MTOK", "")), float(os.getenv("RISKMESH_LLM_PRICE_OUT_PER_MTOK", ""))
        except ValueError:
            return None                          # prices not configured: report tokens only, never guess a cost
        return round(self.tokens_in / 1e6 * pi + self.tokens_out / 1e6 * po, 6)


class OpenAICompat(_HTTP):
    """OpenAI Chat Completions (api.openai.com by default, or any compatible endpoint via RISKMESH_LLM_URL).

    * temperature 0 is sent first; some newer models reject it (HTTP 400 mentioning temperature), so it is retried once without.
    * JSON mode is requested only on the official endpoint; every caller's prompt asks for a JSON object explicitly.
    """
    name = "openai"
    OFFICIAL = "https://api.openai.com/v1"

    def __init__(self, s, timeout=45):
        super().__init__(s, timeout)
        self._no_temp = False

    def complete(self, system, user):
        base = (self.s.llm_url or self.OFFICIAL).rstrip("/")
        body = {"model": self.model, "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        if base == self.OFFICIAL:
            body["response_format"] = {"type": "json_object"}
        if not self._no_temp:
            body["temperature"] = 0
        hdr = {"Authorization": "Bearer " + self.s.llm_key} if self.s.llm_key else {}
        try:
            j = self._post(base + "/chat/completions", body, hdr)
        except LLMError as e:
            if "temperature" in str(e).lower() and "temperature" in body:
                self._no_temp = True
                body.pop("temperature")
                j = self._post(base + "/chat/completions", body, hdr)
            else:
                raise
        u = j.get("usage", {})
        self.calls += 1
        self.tokens_in += u.get("prompt_tokens", 0)
        self.tokens_out += u.get("completion_tokens", 0)
        try:
            return j["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError("malformed response") from e


class AnthropicMessages(_HTTP):
    name = "anthropic"

    def complete(self, system, user):
        base = (self.s.llm_url or "https://api.anthropic.com").rstrip("/")
        j = self._post(base + "/v1/messages", {"model": self.model, "max_tokens": 1500, "temperature": 0, "system": system, "messages": [{"role": "user", "content": user}]},
                       {"x-api-key": self.s.llm_key, "anthropic-version": "2023-06-01"})
        u = j.get("usage", {})
        self.calls += 1
        self.tokens_in += u.get("input_tokens", 0)
        self.tokens_out += u.get("output_tokens", 0)
        try:
            return "".join(b.get("text", "") for b in j["content"] if b.get("type") == "text")
        except (KeyError, TypeError) as e:
            raise LLMError("malformed response") from e


class GeminiGenerate(_HTTP):
    """Google Gemini API, native ``generateContent`` (https://generativelanguage.googleapis.com/v1beta).

    * The API key goes in the ``x-goog-api-key`` header, never in the URL, so it cannot leak through error messages or logs.
    * JSON output is requested with ``generationConfig.responseMimeType`` and every caller's prompt also asks for a JSON object.
    * Thinking parts (``thought: true``) are ignored; a blocked or empty answer raises LLMError instead of returning text.
    """
    name = "gemini"
    BASE = "https://generativelanguage.googleapis.com/v1beta"

    def complete(self, system, user):
        base = (self.s.llm_url or self.BASE).rstrip("/")
        body = {"systemInstruction": {"parts": [{"text": system}]}, "contents": [{"role": "user", "parts": [{"text": user}]}],
                "generationConfig": {"temperature": 0, "responseMimeType": "application/json"}}
        j = self._post(f"{base}/models/{self.model}:generateContent", body, {"x-goog-api-key": self.s.llm_key})
        u = j.get("usageMetadata", {})
        self.calls += 1
        self.tokens_in += u.get("promptTokenCount", 0)
        self.tokens_out += u.get("candidatesTokenCount", 0) + u.get("thoughtsTokenCount", 0)
        try:
            parts = j["candidates"][0]["content"]["parts"]
            text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        except (KeyError, IndexError, TypeError) as e:
            reason = (j.get("promptFeedback") or {}).get("blockReason") or ((j.get("candidates") or [{}])[0].get("finishReason"))
            raise LLMError(f"no usable answer ({reason or 'malformed response'})") from e
        if not text.strip():
            raise LLMError("empty answer")
        return text


def get_provider(s: Settings):
    if s.llm_provider == "none":
        return NoLLM()
    return {"openai": OpenAICompat, "anthropic": AnthropicMessages, "gemini": GeminiGenerate}.get(s.llm_provider, lambda *_: NoLLM())(s)
