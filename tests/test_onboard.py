"""Adding a never-seen machine: trust its identity, install your key, then use it."""

import getpass

import asyncssh
import pytest

import fakesshd
from ssh_portal.onboard import Onboarding, OnboardingError

ADDRESS = "127.0.0.1"
USER = getpass.getuser()


@pytest.fixture
async def fresh(tmp_path):
    """A machine straight after setup-target.sh: unknown host key, no key installed, passwords on."""
    server = await fakesshd.start(tmp_path, password="hunter2", authorize_client=False, trust_host=False)
    yield server
    await server.close()


def onboarding_for(server, public_key="default"):
    conns = server.connections()
    line = server.client_key.export_public_key().decode().strip() if public_key == "default" else public_key

    async def find():
        return line

    return conns, Onboarding(conns, public_key=find, timeout=5)


async def test_new_machine_from_first_contact_to_key_login(fresh):
    conns, onboarding = onboarding_for(fresh)

    first = await onboarding.check(ADDRESS, fresh.port, USER)
    assert first["identity"] == "unknown"
    assert first["fingerprint"] == fresh.host_key.get_fingerprint("sha256")
    assert first["key_type"] == "ED25519"
    assert "login" not in first  # nothing is tried until you've confirmed who it is

    with pytest.raises(OnboardingError, match="different key"):
        await onboarding.trust(ADDRESS, fresh.port, "SHA256:not-the-one-you-were-shown")
    assert fresh.known_hosts.read_text() == ""

    await onboarding.trust(ADDRESS, fresh.port, first["fingerprint"])
    entry = fresh.known_hosts.read_text()
    assert entry.startswith("|1|") and ADDRESS not in entry  # hashed, like HashKnownHosts

    second = await onboarding.check(ADDRESS, fresh.port, USER)
    assert (second["identity"], second["login"]) == ("trusted", "needs_key")

    with pytest.raises(OnboardingError, match="Password not accepted"):
        await onboarding.install_key(ADDRESS, fresh.port, USER, "wrong")

    await onboarding.install_key(ADDRESS, fresh.port, USER, "hunter2")
    await onboarding.install_key(ADDRESS, fresh.port, USER, "hunter2")  # running it twice adds nothing
    keys = fresh.home / ".ssh" / "authorized_keys"
    assert keys.read_text() == fresh.client_key.export_public_key().decode().strip() + "\n"
    assert keys.stat().st_mode & 0o777 == 0o600

    third = await onboarding.check(ADDRESS, fresh.port, USER)
    assert (third["identity"], third["login"]) == ("trusted", "ok")
    assert "x11vnc" in third

    # And the portal's own connections now accept the machine.
    assert await conns.status("local") == {"online": True}


async def test_key_is_appended_after_a_last_line_without_newline(fresh):
    keys = fresh.home / ".ssh" / "authorized_keys"
    keys.write_text("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl other")
    _, onboarding = onboarding_for(fresh)
    first = await onboarding.check(ADDRESS, fresh.port, USER)
    await onboarding.trust(ADDRESS, fresh.port, first["fingerprint"])
    await onboarding.install_key(ADDRESS, fresh.port, USER, "hunter2")
    lines = keys.read_text().splitlines()
    assert lines[0].endswith(" other")
    assert lines[1] == fresh.client_key.export_public_key().decode().strip()


async def test_a_changed_host_key_is_refused_not_replaced(tmp_path):
    server = await fakesshd.start(tmp_path, trust_host=False)
    impostor = asyncssh.generate_private_key("ssh-ed25519").export_public_key().decode()
    server.known_hosts.write_text(f"[{ADDRESS}]:{server.port} {impostor}")
    _, onboarding = onboarding_for(server)

    result = await onboarding.check(ADDRESS, server.port, USER)
    assert result["identity"] == "changed"
    assert "login" not in result
    with pytest.raises(OnboardingError, match=r"ssh-keygen -R '\[127.0.0.1\]:\d+'"):
        await onboarding.trust(ADDRESS, server.port, result["fingerprint"])
    assert server.known_hosts.read_text() == f"[{ADDRESS}]:{server.port} {impostor}"
    await server.close()


async def test_without_a_local_key_it_says_how_to_make_one(fresh):
    _, onboarding = onboarding_for(fresh, public_key=None)
    first = await onboarding.check(ADDRESS, fresh.port, USER)
    assert first["have_key"] is False
    await onboarding.trust(ADDRESS, fresh.port, first["fingerprint"])
    with pytest.raises(OnboardingError, match="ssh-keygen -t ed25519"):
        await onboarding.install_key(ADDRESS, fresh.port, USER, "hunter2")


async def test_unreachable_machine(fresh, free_port):
    _, onboarding = onboarding_for(fresh)
    result = await onboarding.check(ADDRESS, free_port, USER)
    assert result["reachable"] is False
    assert "Connection refused" in result["error"]
