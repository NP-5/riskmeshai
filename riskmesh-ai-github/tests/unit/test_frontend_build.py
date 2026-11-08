import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _html():
    return (ROOT / "web" / "index.html").read_text()


def test_ui_is_the_original_design_with_embedded_snapshot():
    h = _html()
    assert "Plus Jakarta Sans" in h and "id=\"splash\"" in h and "circle[data-d]" in h
    m = re.search(r"/\*DATA_START\*/(.*?)/\*DATA_END\*/", h, re.S)
    assert m and m.group(1).strip() not in ("", "null"), "snapshot missing: run python build_demo.py"


def test_ui_has_no_secrets_or_persistent_token_storage():
    h = _html()
    assert "localStorage" not in h
    assert not re.search(r"(client_secret|PAYPAL_CLIENT_SECRET|sk-[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_-]{30,})", h)


def test_served_index_matches_root_copy():
    assert _html() == (ROOT / "index.html").read_text(), "run python build_demo.py"
