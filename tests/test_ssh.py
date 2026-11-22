"""SSH 命令拼接与表单校验。"""
import pytest

from workbench.core.errors import BadRequest
from workbench.services.connections import validate
from workbench.ssh import commands as cmds

BASE = {"name": "n", "host": "h", "port": 0, "user": "", "identity_file": "", "proxy_jump": "", "socks_proxy": "",
        "remote_dir": "", "note": "", "share_with_ai": True, "remote_claude": "", "remote_codex": "",
        "allow_root_bypass": False, "auth": "key", "password": None}


def test_args_and_command(env):
    c = {**BASE, "id": 1, "user": "u", "port": 22, "socks_proxy": "127.0.0.1:1080", "remote_dir": "~/w"}
    a = cmds.args(c)
    assert a[-1] == "u@h" and "-p" in a and "ProxyCommand=nc -X 5 -x 127.0.0.1:1080 %h %p" in a
    assert cmds.interactive_command(c).endswith("'cd ~/w && exec $SHELL -l'")
    pw = {**c, "auth": "password"}
    assert cmds.interactive_command(pw).startswith("env SSH_ASKPASS=")
    assert cmds.remote_dir_expr("") == "~" and cmds.remote_dir_expr("/a b") == "'/a b'"


def test_validate():
    d, pw = validate({**BASE, "socks_proxy": "socks5://10.0.0.1:1080"})
    assert d["socks_proxy"] == "10.0.0.1:1080" and pw is None
    for bad in ({"host": "-x"}, {"name": ""}, {"port": 70000}, {"socks_proxy": "h:1", "proxy_jump": "j"},
                {"auth": "token"}):
        with pytest.raises(BadRequest):
            validate({**BASE, **bad})
