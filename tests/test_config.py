import pytest

from ssh_portal.config import ConfigError, Host, add_host, load_hosts, remove_host


def write(tmp_path, text):
    path = tmp_path / "hosts.toml"
    path.write_text(text)
    return path


def test_missing_file_means_no_hosts(tmp_path):
    assert load_hosts(tmp_path / "nope.toml") == []


def test_reads_hosts_with_defaults(tmp_path):
    path = write(tmp_path, """
[[host]]
name = "office"
address = "192.168.1.20"
user = "you"

[[host]]
name = "media-pc"
address = "media"
port = 2222
display = ":1"
""")
    assert load_hosts(path) == [
        Host(name="office", address="192.168.1.20", user="you"),
        Host(name="media-pc", address="media", port=2222, display=":1"),
    ]


def test_target_matches_ssh_command_line():
    assert Host(name="a", address="10.0.0.2", user="w").target == "w@10.0.0.2"
    assert Host(name="a", address="box", port=2222).target == "box -p 2222"


@pytest.mark.parametrize(
    "entry, message",
    [
        ('name = "has space"\naddress = "x"', "Name can only use"),
        ('name = "a"', "Address is required"),
        ('name = "a"\naddress = "-oProxyCommand=sh"', "Address must be"),
        ('name = "a"\naddress = "x; rm -rf ~"', "Address must be"),
        ('name = "a"\naddress = "x"\nuser = "you me"', "User must be"),
        ('name = "a"\naddress = "x"\nport = 70000', "Port must be"),
        ('name = "a"\naddress = "x"\nport = true', "Port must be"),
        ('name = "a"\naddress = "x"\ndisplay = ":0; rm -rf ~"', "Display must"),
        ('name = "a"\naddress = "x"\npassword = "hunter2"', "Unknown settings: password"),
    ],
)
def test_rejects_bad_entries(tmp_path, entry, message):
    with pytest.raises(ConfigError, match=message):
        load_hosts(write(tmp_path, f"[[host]]\n{entry}\n"))


def test_accepts_ipv6_and_ssh_config_aliases(tmp_path):
    path = write(tmp_path, '[[host]]\nname = "a"\naddress = "fe80::1%eth0"\n[[host]]\nname = "b"\naddress = "my_box"\n')
    assert [h.address for h in load_hosts(path)] == ["fe80::1%eth0", "my_box"]


def test_rejects_duplicate_names(tmp_path):
    path = write(tmp_path, '[[host]]\nname = "a"\naddress = "x"\n[[host]]\nname = "a"\naddress = "y"\n')
    with pytest.raises(ConfigError, match="already exists"):
        load_hosts(path)


def test_reports_toml_syntax_errors(tmp_path):
    with pytest.raises(ConfigError, match="hosts.toml"):
        load_hosts(write(tmp_path, "[[host]\n"))


# ---- editing from the UI ----

HAND_WRITTEN = """\
# My machines

# The desk in the office
[[host]]
name = "office"
address = "192.168.1.20"  # static lease
user = "you"

# Living room
[[host]]
name = "media"
address = "media.lan"
"""


def test_add_creates_a_private_file_with_a_header(tmp_path):
    path = tmp_path / "config" / "ssh-portal" / "hosts.toml"
    add_host(path, Host(name="office", address="192.168.1.20", user="you"))
    assert load_hosts(path) == [Host(name="office", address="192.168.1.20", user="you")]
    assert path.read_text().startswith("# Machines for ssh-portal.")
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700


def test_add_appends_and_keeps_hand_written_comments(tmp_path):
    path = write(tmp_path, HAND_WRITTEN)
    add_host(path, Host(name="garage", address="10.0.0.9", port=2222, display=":1"))
    text = path.read_text()
    assert text.startswith(HAND_WRITTEN)
    assert text.endswith('\n\n[[host]]\nname = "garage"\naddress = "10.0.0.9"\nport = 2222\ndisplay = ":1"\n')
    assert [h.name for h in load_hosts(path)] == ["office", "media", "garage"]


def test_add_refuses_a_duplicate_name(tmp_path):
    path = write(tmp_path, HAND_WRITTEN)
    with pytest.raises(ConfigError, match="already exists"):
        add_host(path, Host(name="office", address="elsewhere"))
    assert path.read_text() == HAND_WRITTEN


def test_remove_takes_the_block_and_its_comment_and_nothing_else(tmp_path):
    path = write(tmp_path, HAND_WRITTEN)
    remove_host(path, "office")
    assert path.read_text() == "# My machines\n\n# Living room\n[[host]]\nname = \"media\"\naddress = \"media.lan\"\n"

    path = write(tmp_path, HAND_WRITTEN)
    remove_host(path, "media")
    assert "Living room" not in path.read_text()
    assert "# static lease" in path.read_text()
    assert path.read_text().endswith('user = "you"\n')  # no stray blank lines left behind
    assert [h.name for h in load_hosts(path)] == ["office"]


def test_remove_unknown_name_leaves_the_file_alone(tmp_path):
    path = write(tmp_path, HAND_WRITTEN)
    with pytest.raises(ConfigError, match="no machine named 'nope'"):
        remove_host(path, "nope")
    assert path.read_text() == HAND_WRITTEN


def test_remove_refuses_layouts_it_cant_edit_safely(tmp_path):
    # Inline tables don't have a [[host]] header to cut at.
    path = write(tmp_path, 'host = [{ name = "a", address = "x" }, { name = "b", address = "y" }]\n')
    with pytest.raises(ConfigError, match="by hand"):
        remove_host(path, "a")
    assert len(load_hosts(path)) == 2
