import pytest

from ssh_portal.config import ConfigError, Host, load_hosts


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
        ('name = "has space"\naddress = "x"', "name must be"),
        ('name = "a"', "address is required"),
        ('name = "a"\naddress = "x"\nport = 70000', "port must be"),
        ('name = "a"\naddress = "x"\ndisplay = ":0; rm -rf ~"', "display must"),
        ('name = "a"\naddress = "x"\npassword = "hunter2"', "unknown keys"),
    ],
)
def test_rejects_bad_entries(tmp_path, entry, message):
    with pytest.raises(ConfigError, match=message):
        load_hosts(write(tmp_path, f"[[host]]\n{entry}\n"))


def test_rejects_duplicate_names(tmp_path):
    path = write(tmp_path, '[[host]]\nname = "a"\naddress = "x"\n[[host]]\nname = "a"\naddress = "y"\n')
    with pytest.raises(ConfigError, match="duplicate"):
        load_hosts(path)


def test_reports_toml_syntax_errors(tmp_path):
    with pytest.raises(ConfigError, match="hosts.toml"):
        load_hosts(write(tmp_path, "[[host]\n"))
