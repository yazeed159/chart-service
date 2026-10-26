"""Tests for POST /client-errors (run: python -m pytest tests/)."""
import json, logging, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("POLYGON_API_KEY", "test")
import pytest
from flask import Flask
import client_errors as ce


@pytest.fixture
def client(monkeypatch):
    ce._hits.clear()
    monkeypatch.setattr(ce, "resolve_user_id", lambda h: "user-1234567890" if h == "Bearer good" else None)
    app = Flask(__name__)
    app.register_blueprint(ce.bp)
    return app.test_client()


def post(c, body, token="Bearer good", **kw):
    headers = {"Authorization": token} if token else {}
    return c.post("/client-errors", data=json.dumps(body), content_type="application/json", headers=headers, **kw)


def test_valid_report_is_logged_as_one_json_line(client, caplog):
    with caplog.at_level(logging.WARNING, logger="client_errors"):
        r = post(client, {"kind": "error", "msg": "boom", "page": "/journal.html", "stack": "at x"})
    assert r.status_code == 204
    line = [m for m in caplog.messages if m.startswith("client-error ")]
    assert len(line) == 1
    rec = json.loads(line[0].split(" ", 1)[1])
    assert rec["msg"] == "boom" and rec["page"] == "/journal.html" and rec["user"] == "user-123"   # 8 chars only


def test_no_or_bad_token_is_401_and_nothing_logged(client, caplog):
    with caplog.at_level(logging.WARNING, logger="client_errors"):
        assert post(client, {"msg": "x"}, token=None).status_code == 401
        assert post(client, {"msg": "x"}, token="Bearer nope").status_code == 401
    assert not [m for m in caplog.messages if m.startswith("client-error ")]


def test_oversized_body_is_413(client):
    assert post(client, {"msg": "x" * (ce.MAX_BODY_BYTES + 10)}).status_code == 413


def test_fields_are_truncated_and_control_chars_cannot_forge_log_lines(client, caplog):
    with caplog.at_level(logging.WARNING, logger="client_errors"):
        post(client, {"msg": "a" * 5000, "stack": "line1\nclient-error {\"fake\":1}\r\nline3"})
    line = [m for m in caplog.messages if m.startswith("client-error ")][0]
    assert "\n" not in line and "\r" not in line
    rec = json.loads(line.split(" ", 1)[1])
    assert len(rec["msg"]) == ce.FIELD_MAX["msg"]


def test_non_object_body_is_400(client):
    assert post(client, ["not", "an", "object"]).status_code == 400
    assert client.post("/client-errors", data="garbage", content_type="text/plain",
                       headers={"Authorization": "Bearer good"}).status_code == 400


def test_per_user_limit_returns_429(client, monkeypatch):
    monkeypatch.setattr(ce, "IP_LIMIT", 10_000)
    codes = [post(client, {"msg": str(i)}).status_code for i in range(ce.USER_LIMIT + 3)]
    assert codes[:ce.USER_LIMIT] == [204] * ce.USER_LIMIT and codes[-1] == 429


def test_per_ip_limit_applies_before_the_token_is_checked(client, monkeypatch):
    calls = []
    monkeypatch.setattr(ce, "resolve_user_id", lambda h: calls.append(h) or None)
    for _ in range(ce.IP_LIMIT):
        assert post(client, {"msg": "x"}, token="Bearer junk").status_code == 401
    n = len(calls)
    assert post(client, {"msg": "x"}, token="Bearer junk").status_code == 429
    assert len(calls) == n   # the over-limit request never reached Supabase
