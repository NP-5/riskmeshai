"""Environment-driven configuration with startup validation.

Safe defaults: development mode, no demo-mode webhook bypass unless explicitly enabled, and in production the
service refuses to start without API tokens, a PII pepper and a persistent database.
"""
from __future__ import annotations

import os
import re
import secrets
from dataclasses import dataclass, field
from typing import Mapping, Optional

ROLES = ("viewer", "analyst", "reviewer", "admin")


@dataclass(frozen=True)
class TokenEntry:
    token: str
    identity: str
    role: str


@dataclass(frozen=True)
class Settings:
    env: str = "development"                    # development | production
    demo_mode: bool = False                     # unsigned webhooks + simulated payout actions (never in production)
    db_url: str = "sqlite:///riskmesh.db"       # sqlite:///path | sqlite:///:memory: | postgresql://...
    paypal_base: str = "https://api-m.sandbox.paypal.com"
    paypal_client_id: str = ""
    paypal_client_secret: str = ""
    paypal_webhook_id: str = ""
    tokens: tuple = ()
    pepper: str = ""
    rate_limit_per_min: int = 120
    http_timeout: float = 15.0
    http_retries: int = 2
    agent_timeout: float = 20.0
    agent_retries: int = 1
    max_revision_rounds: int = 2
    llm_provider: str = "none"                  # none | gemini | openai | anthropic
    llm_url: str = ""
    llm_key: str = ""
    llm_model: str = ""
    generated_token: Optional[str] = field(default=None, compare=False, repr=False)

    @property
    def production(self) -> bool:
        return self.env == "production"

    @property
    def paypal_configured(self) -> bool:
        return bool(self.paypal_client_id and self.paypal_client_secret)

    @property
    def sandbox(self) -> bool:
        return "sandbox" in self.paypal_base

    def public_view(self) -> dict:
        """Environment configuration safe to show in the dashboard: never any secret value."""
        return {
            "env": self.env, "demo_mode": self.demo_mode, "database": self.db_url.split("://")[0],
            "paypal_base": self.paypal_base, "paypal_client_id_set": bool(self.paypal_client_id),
            "paypal_client_secret_set": bool(self.paypal_client_secret), "paypal_webhook_id_set": bool(self.paypal_webhook_id),
            "api_tokens_configured": len(self.tokens), "pepper_set": bool(self.pepper),
            "llm_provider": self.llm_provider, "llm_model": self.llm_model or None, "llm_key_set": bool(self.llm_key),
            "rate_limit_per_min": self.rate_limit_per_min, "agent_timeout_s": self.agent_timeout,
            "max_revision_rounds": self.max_revision_rounds,
        }


def _flag(v: str) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def parse_tokens(raw: str) -> tuple:
    """RISKMESH_API_TOKENS="token:identity:role,token2:identity2:role2". Tokens must be at least 16 characters."""
    out = []
    for part in filter(None, (p.strip() for p in raw.split(","))):
        bits = part.split(":")
        if len(bits) != 3:
            raise ValueError("RISKMESH_API_TOKENS entries must look like token:identity:role")
        tok, ident, role = (b.strip() for b in bits)
        if role not in ROLES:
            raise ValueError(f"unknown role {role!r}; allowed: {', '.join(ROLES)}")
        if len(tok) < 16:
            raise ValueError("API tokens must be at least 16 characters")
        out.append(TokenEntry(tok, ident, role))
    return tuple(out)


def load_settings(environ: Optional[Mapping[str, str]] = None) -> Settings:
    e = os.environ if environ is None else environ
    env = e.get("RISKMESH_ENV", "development").strip().lower()
    tokens = parse_tokens(e.get("RISKMESH_API_TOKENS", ""))
    generated = None
    if not tokens and env != "production":
        # Development convenience: a random admin token, printed once at startup and never persisted.
        generated = secrets.token_urlsafe(24)
        tokens = (TokenEntry(generated, "dev-admin", "admin"),)
    provider = e.get("RISKMESH_LLM_PROVIDER", "").strip().lower()
    gemini_key = e.get("GEMINI_API_KEY") or e.get("GOOGLE_API_KEY") or ""
    if not provider:
        if gemini_key and not e.get("RISKMESH_LLM_KEY"):
            provider = "gemini"
        else:
            provider = "openai" if e.get("RISKMESH_LLM_KEY") or e.get("RISKMESH_LLM_URL") else "none"
    llm_key = e.get("RISKMESH_LLM_KEY", "") or (gemini_key if provider == "gemini" else "")
    llm_model = e.get("RISKMESH_LLM_MODEL", "") or ("gemini-2.5-flash" if provider == "gemini" else "")   # example model from Google's own docs; set RISKMESH_LLM_MODEL to override
    db_url = e.get("DATABASE_URL") or e.get("RISKMESH_DB_URL") or "sqlite:///riskmesh.db"
    if db_url.startswith("postgres://"):               # Render/Heroku style
        db_url = "postgresql://" + db_url[len("postgres://"):]
    return Settings(
        env=env, demo_mode=_flag(e.get("RISKMESH_DEMO_MODE", "")), db_url=db_url,
        paypal_base=e.get("PAYPAL_BASE", "https://api-m.sandbox.paypal.com").rstrip("/"),
        paypal_client_id=e.get("PAYPAL_CLIENT_ID", ""), paypal_client_secret=e.get("PAYPAL_CLIENT_SECRET", ""),
        paypal_webhook_id=e.get("PAYPAL_WEBHOOK_ID", ""), tokens=tokens, pepper=e.get("RISKMESH_PEPPER", ""),
        rate_limit_per_min=int(e.get("RISKMESH_RATE_LIMIT_PER_MIN", "120")),
        http_timeout=float(e.get("RISKMESH_HTTP_TIMEOUT", "15")), http_retries=int(e.get("RISKMESH_HTTP_RETRIES", "2")),
        agent_timeout=float(e.get("RISKMESH_AGENT_TIMEOUT", "20")), agent_retries=int(e.get("RISKMESH_AGENT_RETRIES", "1")),
        max_revision_rounds=min(2, int(e.get("RISKMESH_MAX_REVISION_ROUNDS", "2"))),
        llm_provider=provider, llm_url=e.get("RISKMESH_LLM_URL", ""), llm_key=llm_key,
        llm_model=llm_model, generated_token=generated,
    )


def validate(s: Settings) -> tuple:
    """Return (errors, warnings). Errors must stop startup."""
    errors, warnings = [], []
    if s.env not in ("development", "production"):
        errors.append("RISKMESH_ENV must be 'development' or 'production'")
    if s.production:
        if s.demo_mode:
            errors.append("RISKMESH_DEMO_MODE must not be enabled in production")
        if not s.tokens:
            errors.append("RISKMESH_API_TOKENS must be set in production")
        if len(s.pepper) < 16:
            errors.append("RISKMESH_PEPPER (>=16 chars) is required in production to hash recipient identifiers")
        if s.db_url.startswith("sqlite") and ":memory:" in s.db_url:
            errors.append("an in-memory database cannot be used in production")
        if not s.paypal_base.startswith("https://"):
            errors.append("PAYPAL_BASE must be https")
        if not (s.paypal_configured and s.paypal_webhook_id):
            warnings.append("PayPal credentials/webhook id missing: /webhook will fail closed (503) and the service reports not-ready")
        if s.db_url.startswith("sqlite"):
            warnings.append("SQLite in production needs a persistent disk; use DATABASE_URL=postgresql://... for deployment")
    elif not s.pepper:
        warnings.append("RISKMESH_PEPPER not set: using an insecure development pepper")
    if s.llm_provider not in ("none", "gemini", "openai", "anthropic"):
        errors.append("RISKMESH_LLM_PROVIDER must be none, gemini, openai or anthropic")
    if s.llm_model and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,80}", s.llm_model):
        errors.append("RISKMESH_LLM_MODEL contains characters that are not allowed in a model name")
    if s.llm_provider != "none" and not s.llm_model:
        errors.append("RISKMESH_LLM_MODEL is required when an LLM provider is enabled")
    if s.llm_provider == "gemini" and not s.llm_key:
        errors.append("a Gemini API key is required: set GEMINI_API_KEY (or RISKMESH_LLM_KEY)")
    elif s.llm_provider != "none" and not s.llm_key and not s.llm_url:
        errors.append("RISKMESH_LLM_KEY (GEMINI_API_KEY for Gemini; or RISKMESH_LLM_URL for a local OpenAI-style endpoint) is required when an LLM provider is enabled")
    if s.rate_limit_per_min < 1:
        errors.append("RISKMESH_RATE_LIMIT_PER_MIN must be >= 1")
    return errors, warnings
