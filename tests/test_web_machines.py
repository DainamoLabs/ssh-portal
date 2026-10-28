"""Adding and removing machines through the API updates hosts.toml and the live portal."""

import pytest
from fastapi.testclient import TestClient

from ssh_portal.config import Host, load_hosts
from ssh_portal.discover import Nearby
from ssh_portal.ssh import Connections
from ssh_portal.web import create_app

PORT = 8765
TOKEN = "machines-token"


def client_for(config_path, hosts=(), nearby=None):
    async def found():
        return nearby

    app = create_app(Connections(list(hosts)), token=TOKEN, port=PORT, config_path=config_path, nearby=found)
    client = TestClient(app, base_url=f"http://127.0.0.1:{PORT}")
    client.cookies.set(f"ssh_portal_{PORT}", TOKEN)
    return client


@pytest.fixture
def config(tmp_path):
    return tmp_path / "hosts.toml"


def test_add_saves_to_the_file_and_shows_up_immediately(config):
    with client_for(config) as client:
        res = client.post("/api/machines", json={"name": "office", "address": "192.168.1.20", "user": "you"})
        assert res.status_code == 200
        assert res.json() == {"name": "office", "address": "192.168.1.20", "user": "you", "port": None, "display": ":0"}
        assert [h["name"] for h in client.get("/api/hosts").json()["hosts"]] == ["office"]
    assert load_hosts(config) == [Host(name="office", address="192.168.1.20", user="you")]


def test_add_rejects_what_the_file_would_reject(config):
    with client_for(config) as client:
        res = client.post("/api/machines", json={"name": "x", "address": "-oProxyCommand=sh"})
        assert res.status_code == 422
        assert res.json()["detail"] == "Address must be an IP address or hostname"
        res = client.post("/api/machines", json={"name": "bad name", "address": "x"})
        assert res.status_code == 422
    assert not config.exists()


def test_add_refuses_duplicates(config):
    with client_for(config) as client:
        client.post("/api/machines", json={"name": "office", "address": "a"})
        res = client.post("/api/machines", json={"name": "office", "address": "b"})
        assert res.status_code == 409
        assert "already exists" in res.json()["detail"]
    assert len(load_hosts(config)) == 1


def test_remove_deletes_from_the_file_and_the_portal(config):
    config.write_text('[[host]]\nname = "a"\naddress = "x"\n\n[[host]]\nname = "b"\naddress = "y"\n')
    with client_for(config, load_hosts(config)) as client:
        assert client.delete("/api/machines/a").json() == {"ok": True}
        assert [h["name"] for h in client.get("/api/hosts").json()["hosts"]] == ["b"]
        assert client.delete("/api/machines/a").status_code == 404
    assert [h.name for h in load_hosts(config)] == ["b"]


def test_check_validates_before_connecting_anywhere(config):
    with client_for(config) as client:
        res = client.post("/api/machines/check", json={"address": "x; reboot", "user": "you"})
        assert res.status_code == 422


def test_without_a_hosts_file_editing_is_off():
    with client_for(None) as client:
        assert client.get("/api/hosts").json()["editable"] is False
        assert client.post("/api/machines", json={"name": "a", "address": "x"}).status_code == 403


def test_nearby_lists_machines_not_added_yet(config):
    nearby = [
        Nearby("den", "10.0.0.68", "william", None, "den.local"),
        Nearby("attic", "attic.local", None, 2222, "attic.local"),
        Nearby("office", "10.0.0.20", "you", None, "office.local"),
    ]
    added = [Host(name="office", address="office.local")]  # added by its .local name
    with client_for(config, added, nearby) as client:
        assert client.get("/api/machines/nearby").json() == {
            "available": True,
            "machines": [
                {"name": "den", "address": "10.0.0.68", "user": "william", "port": None},
                {"name": "attic", "address": "attic.local", "user": None, "port": 2222},
            ],
        }


def test_nearby_says_when_it_cannot_look(config):
    with client_for(config, nearby=None) as client:
        assert client.get("/api/machines/nearby").json() == {"available": False, "machines": []}
