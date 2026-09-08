"""Render-only injection regressions; never execute generated installers/units."""
import re
import shlex
import shutil
import subprocess

import pytest

from app.core.config import NodePool, StreamNode
from app.modules.provisioning import enroll_command, install_script, nginx_site, rclone_mount_unit


def node_with(**changes):
    node = StreamNode(name="node-a", base_url="https://node.example.com", probe_url="http://127.0.0.1/load",
                      pools=[NodePool(name="main", emby_prefix="/media", url_prefix="/s/main",
                                      node_path="/srv/media", rclone_remote="drive:")])
    return node.model_copy(update=changes)


def test_shell_arguments_remain_single_literals_and_comments_cannot_inject():
    path = "/tmp/cache; printf audit-marker\n$(printf unsafe)"
    script = install_script(node_with(cache_dir=path), "https://panel.example.com/;printf marker")
    # shlex parsing only, no shell execution. The cache remains one argument.
    command = script[script.index("mkdir -p '" ):].split(" /opt/mediadeck-agent", 1)[0] + " /opt/mediadeck-agent"
    assert shlex.split(command) == ["mkdir", "-p", path, "/opt/mediadeck-agent"]
    assert "\n$(printf unsafe) 所在磁盘" not in script
    assert subprocess.run(["bash", "-n"], input=script, text=True, capture_output=True, check=False).returncode == 0


def test_opaque_rclone_config_cannot_end_its_heredoc():
    material = "[drive]\ntype = drive\nMEDIADECK_RCLONE_EOF\nprintf audit-marker\n# preserve $quotes \" exactly"
    script = install_script(node_with(rclone_conf=material), "https://panel.example.com")
    match = re.search(r"install -m 600 /dev/stdin /root/\.config/rclone/rclone\.conf <<'([^']+)'\n", script)
    assert match
    delimiter = match[1]
    assert delimiter not in material.splitlines()
    body = script[match.end():].split("\n" + delimiter + "\n", 1)[0]
    assert body == material
    assert subprocess.run(["bash", "-n"], input=script, text=True, capture_output=True, check=False).returncode == 0


def test_systemd_arguments_escape_specifiers_environment_and_control_lines():
    path = '/srv/media $HOME %n "quote"\nExecStart=/bin/false'
    node = node_with(cache_dir="/tmp/cache %n $HOME", cache_size="500G\nExecStart=/bin/false")
    pool = node.pools[0].model_copy(update={"node_path": path})
    unit = rclone_mount_unit(node, pool)
    assert '\nExecStart=/bin/false' not in unit
    assert "$$HOME" in unit and "%%n" in unit
    assert '\\nExecStart=/bin/false' in unit
    assert 'mount "drive:"' in unit  # a remote root must not become local path 'drive'
    assert '\\"quote\\"' in unit


def test_nginx_literals_do_not_become_directives_or_recursive_variables():
    secret = 'opaque "$request_uri";\nreturn 200; #\\value'
    path = '/srv/media "$host";\nreturn 200; #'
    node = node_with(sign_secret=secret)
    node.pools[0] = node.pools[0].model_copy(update={"node_path": path, "url_prefix": '/s/";\nreturn 200; #'})
    text = nginx_site(node)
    assert '\nreturn 200;' not in text
    assert 'geo $mediadeck_sign_secret' in text
    assert 'secure_link_md5 "$secure_link_expires$uri$arg_r$arg_u $mediadeck_sign_secret";' in text
    assert '\\"$request_uri\\"' in text
    assert 'alias $mediadeck_pool_path_0/;' in text


@pytest.mark.skipif(not shutil.which("nginx"), reason="nginx parser unavailable")
def test_nginx_opaque_literals_pass_parser_without_running_server(tmp_path):
    node = node_with(sign_secret='opaque "$must_not_expand";\nreturn 200; #\\value')
    node.pools[0] = node.pools[0].model_copy(update={"node_path": '/srv/"$also_literal";\nreturn 200; #'})
    text = nginx_site(node)
    literals = "\n".join(line for line in text.splitlines() if line.startswith("geo "))
    config = tmp_path / "nginx.conf"
    config.write_text(f'''pid {tmp_path}/nginx.pid;
error_log {tmp_path}/error.log;
events {{}}
http {{
{literals}
server {{
listen 127.0.0.1:18999;
location /s/ {{
alias $mediadeck_pool_path_0/;
secure_link $arg_k,$arg_e;
secure_link_md5 "$secure_link_expires$uri$arg_r$arg_u $mediadeck_sign_secret";
}}
}}
}}
''')
    result = subprocess.run([shutil.which("nginx"), "-t", "-p", str(tmp_path), "-c", str(config)],
                            capture_output=True, text=True, timeout=10, check=False)
    assert result.returncode == 0, result.stderr


def test_enrollment_url_and_token_are_shell_quoted():
    panel, token = "https://panel.example.com/;printf marker", "token';printf marker;#"
    command = enroll_command(panel, token)
    words = shlex.split(command)
    assert words[:3] == ["curl", "-fsSL", panel + "/api/enroll/" + token + "/script"]
    assert words[3:] == ["|", "sudo", "env", "MEDIADECK_ENROLL_TOKEN=" + token, "bash"]
