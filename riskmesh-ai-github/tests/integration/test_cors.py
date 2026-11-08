import os
from asgi_client import request
from helpers import H, VIEWER, make_app

HF = "https://nareshpavan-riskmesh-ai.hf.space"


def _with_origins(value):
    old = os.environ.get("RISKMESH_CORS_ORIGINS")
    if value is None:
        os.environ.pop("RISKMESH_CORS_ORIGINS", None)
    else:
        os.environ["RISKMESH_CORS_ORIGINS"] = value
    return old


def _restore(old):
    if old is None:
        os.environ.pop("RISKMESH_CORS_ORIGINS", None)
    else:
        os.environ["RISKMESH_CORS_ORIGINS"] = old


def test_listed_origin_gets_cors_headers_and_preflight_works():
    old = _with_origins(HF)
    try:
        app, _ = make_app()
        pre = request(app, "OPTIONS", "/api/me", headers={"Origin": HF, "Access-Control-Request-Method": "GET"})
        assert pre.status == 204 and pre.headers["access-control-allow-origin"] == HF
        assert "Authorization" in pre.headers["access-control-allow-headers"]
        r = request(app, "GET", "/api/me", headers={**H(VIEWER), "Origin": HF})
        assert r.status == 200 and r.headers["access-control-allow-origin"] == HF
    finally:
        _restore(old)


def test_unlisted_origin_and_wildcard_get_nothing():
    old = _with_origins("*")
    try:
        app, _ = make_app()
        r = request(app, "GET", "/api/me", headers={**H(VIEWER), "Origin": "https://evil.example"})
        assert "access-control-allow-origin" not in r.headers
        pre = request(app, "OPTIONS", "/api/me", headers={"Origin": HF})
        assert pre.status == 204 and "access-control-allow-origin" not in pre.headers
    finally:
        _restore(old)


def test_no_env_means_no_cors():
    old = _with_origins(None)
    try:
        app, _ = make_app()
        r = request(app, "GET", "/healthz", headers={"Origin": HF})
        assert "access-control-allow-origin" not in r.headers
    finally:
        _restore(old)
