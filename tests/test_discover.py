"""Listing machines that announce themselves while setup.sh runs on them."""

import time

import pytest

from ssh_portal import discover
from ssh_portal.discover import Nearby, parse

# Real `avahi-browse --resolve --parsable` lines: one per interface and IP version.
MINT_DEN = """\
+;docker0;IPv4;mint-den;_ssh-portal._tcp;local
=;docker0;IPv4;mint-den;_ssh-portal._tcp;local;mint-den.local;172.17.0.2;22;"user=william"
=;enp4s0;IPv6;mint-den;_ssh-portal._tcp;local;mint-den.local;fe80::1c2b:3aff:fe4d:5e6f;22;"user=william"
=;enp4s0;IPv4;mint-den;_ssh-portal._tcp;local;mint-den.local;10.0.0.68;22;"user=william"
"""


def test_one_machine_per_announcement_with_its_ipv4_address():
    assert parse(MINT_DEN) == [Nearby("mint-den", "172.17.0.2", "william", None, "mint-den.local")]


def test_without_ipv4_the_local_name_is_used():
    line = '=;enp4s0;IPv6;attic;_ssh-portal._tcp;local;attic.local;fe80::1;2222;"user=pat"\n'
    assert parse(line) == [Nearby("attic", "attic.local", "pat", 2222, "attic.local")]


def test_names_are_unescaped_then_made_safe():
    lines = (
        '=;eth0;IPv4;mint\\032\\0352;_ssh-portal._tcp;local;mint-2.local;10.0.0.9;22;"user=a"\n'
        '=;eth0;IPv4;Pat\\226\\128\\153s\\032PC;_ssh-portal._tcp;local;pats-pc.local;10.0.0.7;22;"user=a"\n'
    )
    assert [m.name for m in parse(lines)] == ["mint-2", "Pat-s-PC"]


def test_announcements_cannot_smuggle_options_or_commands():
    lines = (
        # A user that would be an ssh option keeps the machine but drops the user.
        '=;eth0;IPv4;box;_ssh-portal._tcp;local;box.local;10.0.0.5;22;"user=-oProxyCommand=sh"\n'
        # An address that isn't one drops the machine.
        '=;eth0;IPv6;evil;_ssh-portal._tcp;local;-oProxyCommand=sh;fe80::1;22;"user=a"\n'
        # Not a resolved line at all.
        "=;eth0;IPv4;short\n"
        "Failed to resolve service 'x' of type '_ssh-portal._tcp' in domain 'local': Timeout reached\n"
    )
    assert parse(lines) == [Nearby("box", "10.0.0.5", None, None, "box.local")]


def test_txt_quoting_and_extra_records():
    line = '=;eth0;IPv4;box;_ssh-portal._tcp;local;box.local;10.0.0.5;22;"note=a \\"quoted\\" ;bit" "user=sam"\n'
    assert parse(line)[0].user == "sam"


@pytest.fixture
def fake_browse(tmp_path, monkeypatch):
    def install(script: str):
        path = tmp_path / "avahi-browse"
        path.write_text("#!/bin/sh\n" + script)
        path.chmod(0o755)
        monkeypatch.setattr(discover, "BROWSE", (str(path),))

    return install


async def test_nearby_runs_avahi_browse(fake_browse):
    fake_browse(f"cat <<'EOF'\n{MINT_DEN}EOF\n")
    assert [m.name for m in await discover.nearby()] == ["mint-den"]


async def test_nearby_without_avahi(monkeypatch, fake_browse):
    monkeypatch.setattr(discover, "BROWSE", ("/nonexistent/avahi-browse",))
    assert await discover.nearby() is None
    fake_browse("echo 'Failed to create client object: Daemon not running' >&2; exit 1\n")
    assert await discover.nearby() is None


async def test_nearby_gives_up_on_a_slow_network(monkeypatch, fake_browse):
    monkeypatch.setattr(discover, "BROWSE_TIMEOUT", 0.5)
    first = MINT_DEN.splitlines()[1]
    fake_browse(f"echo '{first}'\nexec sleep 30\n")
    started = time.monotonic()
    assert [m.name for m in await discover.nearby()] == ["mint-den"]
    assert time.monotonic() - started < 3
