"""The portal must only answer your own browser tab, holding the secret from startup."""

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from ssh_portal.config import Host
from ssh_portal.ssh import Connections
from ssh_portal.web import create_app

PORT = 8765
TOKEN = "s3cret-token"
COOKIE = f"ssh_portal_{PORT}"


@pytest.fixture
def client():
    app = create_app(Connections([Host(name="box", address="192.0.2.1")]), token=TOKEN, port=PORT)
    with TestClient(app, base_url=f"http://127.0.0.1:{PORT}", follow_redirects=False) as c:
        yield c


def test_refuses_requests_without_the_secret(client):
    res = client.get("/api/hosts")
    assert res.status_code == 401
    assert "link printed in the terminal" in res.text


def test_link_with_token_sets_cookie_and_strips_token(client):
    res = client.get(f"/?token={TOKEN}")
    assert res.status_code == 303
    assert res.headers["location"] == "/"
    cookie = res.headers["set-cookie"]
    assert f"{COOKIE}={TOKEN}" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=strict" in cookie

    client.cookies.set(COOKIE, TOKEN)
    assert client.get("/api/hosts").json()["hosts"][0]["name"] == "box"


def test_wrong_token_is_refused(client):
    assert client.get("/?token=guess").status_code == 401
    assert client.get("/?token=güess").status_code == 401  # non-ASCII mustn't crash compare_digest
    client.cookies.set(COOKIE, "guess")
    assert client.get("/api/hosts").status_code == 401


def test_dns_rebinding_host_is_refused(client):
    client.cookies.set(COOKIE, TOKEN)
    assert client.get("/api/hosts", headers={"host": f"evil.example:{PORT}"}).status_code == 400


def test_cross_origin_websocket_is_refused(client):
    client.cookies.set(COOKIE, TOKEN)
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/ws/term/box", headers={"origin": "http://evil.example"}):
            pass
    assert exc.value.code == 1008


def test_websocket_needs_cookie_even_from_the_right_origin(client):
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/term/box", headers={"origin": f"http://127.0.0.1:{PORT}"}):
            pass


def test_responses_carry_a_strict_content_security_policy(client):
    client.cookies.set(COOKIE, TOKEN)
    res = client.get("/")
    assert res.status_code == 200
    csp = res.headers["content-security-policy"]
    assert "script-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp


def test_unknown_host_is_404(client):
    client.cookies.set(COOKIE, TOKEN)
    assert client.get("/api/hosts/nope/status").status_code == 404
    res = client.post("/api/run", json={"hosts": ["nope"], "command": "true"})
    assert res.status_code == 404


def test_javascript_modules_are_served_as_javascript(client):
    client.cookies.set(COOKIE, TOKEN)
    for path in ("/static/app.js", "/static/vendor/xterm/xterm.mjs", "/static/vendor/novnc/core/rfb.js"):
        res = client.get(path)
        assert res.status_code == 200, path
        assert res.headers["content-type"].startswith("text/javascript"), path
